"""marathon.deck.server — the stdlib HTTP server for the deck.

A :class:`http.server.ThreadingHTTPServer` bound to **127.0.0.1 only**
(never 0.0.0.0 — the deck performs irreversible actions and must not be
reachable off the loopback), with a tiny hand-rolled router for the shared
API contract plus static-file serving of the other track's
``marathon/deck/static/`` directory. Stdlib only — no Flask/FastAPI, no
build step.

API surface (shared contract — the gate reconciles any drift):

* ``GET  /``                  → the rendered index (token injected)
* ``GET  /static/<path>``     → static assets (tolerates a missing dir)
* ``GET  /api/queue?chapter=N`` → ``{cards, building, landed_today}``
* ``GET  /api/card/{id}``     → the full :class:`CardDetail`
* ``GET  /api/subdeck/{id}``  → the issue's per-declaration SUBDECK (read)
* ``GET  /api/review-workspace`` → source-ordered file/chapter review (read)
* ``GET  /api/review-source`` → lazy repo-local whole-file Lean source (read)
* ``GET  /api/project-graph`` → project issue/declaration dependency graph (read)
* ``GET  /api/definition``    → current-source identifier lookup (read)
* ``GET  /api/definition-terms`` → compact project identifier index (read)
* ``GET  /api/verso-status``  → nonblocking semantic-doc build status
* ``GET  /api/verso-manifest`` → last-good Verso navigation manifest
* ``GET  /verso/<path>``      → last-good generated Verso artifact
* ``POST /api/verso-retry``   → explicitly retry a failed semantic-doc build
* ``GET  /api/reconciliation/{id}`` → pending identity proposals (read)
* ``GET  /api/ship-status``    → durable background ship status (read)
* ``GET  /api/status``        → live conductor/landing events (poll)
* ``GET  /api/events``        → the same, as Server-Sent Events
* ``POST /api/conductor-start`` → ensure the conductor is running (guarded,
                                  operator opt-in)
* ``POST /api/verdict``       → route a verdict (IRREVERSIBLE — guarded)
* ``POST /api/decl-verdict``  → record one per-decl verdict (LOCAL — guarded)
* ``POST /api/batch-decl-verdict`` → atomically record many (LOCAL — guarded)
* ``POST /api/reconcile-proposal`` → confirm/dismiss exact identity proposal
* ``POST /api/ship``          → ship a subdeck's issue (IRREVERSIBLE — guarded)
* ``POST /api/refresh-issue`` → push-refresh signatures to GitHub (WRITE — guarded)

BINDING SAFETY:

1. **Verdicts fire only on an explicit, deliberate POST.** Every GET is a
   pure read — assembling a card never mutates the ledger or touches
   git/gh beyond read-only queries; ``do_GET`` cannot reach
   :func:`marathon.deck.verdicts.apply_verdict`.
2. **Loopback bind + per-session token.** The server binds 127.0.0.1 and
   mints a fresh ``secrets.token_urlsafe`` per process. It is injected
   into the served index (replacing ``__MARATHON_SESSION_TOKEN__``); the
   browser echoes it on ``POST /api/verdict`` (an ``X-Marathon-Session-
   Token`` header or a ``token`` body field). A POST that lacks the token
   — a stray tab, another origin, a prefetch — gets ``403`` and NO side
   effect. A cross-origin ``Origin`` header is likewise rejected.
3. **Reject notes go verbatim to Aristotle.** The POST handler passes the
   note straight to :func:`~marathon.deck.verdicts.apply_verdict`, which
   routes it through the committed ``cmd_reject`` Claude-bypass.
"""

from __future__ import annotations

import hmac
import json
import os
import re
import secrets
import threading
import webbrowser
from contextvars import Context
from datetime import datetime, timezone
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path, PurePosixPath
from threading import Event, Lock, Thread
from time import monotonic
from typing import TYPE_CHECKING, Callable, Optional, Sequence
from urllib.parse import parse_qs, unquote, urlsplit

if TYPE_CHECKING:  # pragma: no cover — type-only
    from marathon.review.config import ReviewConfig

# Loopback only — the deck must never be reachable off the local machine
# (it merges PRs and dispatches Aristotle).
BIND_HOST = "127.0.0.1"

# The token placeholder the served index carries; the other track's
# index.html embeds this exact string in a meta tag + a bootstrap global.
TOKEN_PLACEHOLDER = "__MARATHON_SESSION_TOKEN__"

# The header the browser echoes the session token on for POST /api/verdict.
# A `token` field in the JSON body is also accepted (front-end convenience).
TOKEN_HEADER = "X-Marathon-Session-Token"

# When the deck is shared behind an authenticating proxy (e.g. oauth2-proxy with
# --pass-access-token), the proxy forwards the signed-in collaborator's own
# GitHub OAuth token on this header. The deck binds it as the acting GitHub
# identity for the request (see review.github.actor_github_token) so every
# GitHub write the verdict triggers is attributed to THEM, not the host. Absent
# (the local single-user path) => the host's ambient gh auth, unchanged.
ACCESS_TOKEN_HEADER = "X-Forwarded-Access-Token"

# Static assets live in the OTHER track's dir. Served read-only; tolerated
# absent (tests run without it; an early launch may pre-date app.js/style.css).
STATIC_DIR = Path(__file__).resolve().parent / "static"

# index.html references its cacheable assets as `app.js?v=__ASSET_VERSION__`;
# _serve_index substitutes the current value (newest asset mtime). A redeploy of
# app.js/style.css bumps it, so the browser fetches the new bundle instead of a
# stale cached copy — the index itself is served `no-cache`, so it always carries
# the current value. (Without this, a redeploy left the browser running the OLD
# app.js: new index.html, cached old JS — e.g. a new tab that did nothing.)
ASSET_VERSION_PLACEHOLDER = "__ASSET_VERSION__"

# ``None`` is a useful explicit value for tests/demo mode (no manager), so a
# private sentinel distinguishes it from the normal "construct one" path.
_DEFAULT_VERSO_MANAGER = object()
_VERSO_ARTIFACT_TOKEN_RE = re.compile(r"^[0-9a-f]{64}$")
# Keep generated documents scriptless, but retain their real origin so an
# authenticated reverse proxy receives the session cookie on Verso's CSS/font
# subrequests.  A bare ``sandbox`` gives the document an opaque origin; browsers
# then omit the oauth2-proxy identity and the Blueprint styles fail with 403.
_VERSO_HTML_CSP = (
    "sandbox allow-same-origin; default-src 'none'; script-src 'none'; connect-src 'none'; "
    "frame-src 'none'; form-action 'none'; base-uri 'self'; "
    "style-src 'self' 'unsafe-inline'; img-src 'self' data:; font-src 'self'"
)
_VERSO_ACTIVE_DOCUMENT_SUFFIXES = frozenset({".html", ".htm", ".xhtml", ".svg", ".xml"})


def _asset_version() -> str:
    newest = 0.0
    for _name in ("app.js", "style.css"):
        try:
            newest = max(newest, (STATIC_DIR / _name).stat().st_mtime)
        except OSError:
            pass
    return str(int(newest)) if newest else "0"


# A small static-asset content-type table (stdlib mimetypes is fine too,
# but this keeps the common deck assets explicit + dependency-free).
_CONTENT_TYPES = {
    ".html": "text/html; charset=utf-8",
    ".css": "text/css; charset=utf-8",
    ".js": "application/javascript; charset=utf-8",
    ".mjs": "application/javascript; charset=utf-8",
    ".json": "application/json; charset=utf-8",
    ".svg": "image/svg+xml",
    ".png": "image/png",
    ".ico": "image/x-icon",
    ".txt": "text/plain; charset=utf-8",
    ".xml": "application/xml; charset=utf-8",
    ".wasm": "application/wasm",
    ".webmanifest": "application/manifest+json",
    ".woff": "font/woff",
    ".woff2": "font/woff2",
    ".map": "application/json; charset=utf-8",
}

# Cap on a POST body the verdict endpoint will read (a verdict is tiny; a
# huge body is either a bug or an attack — refuse it, never buffer it).
MAX_POST_BYTES = 256 * 1024

# Files selected by the Add Chapter UI are copied into a model prompt.  Keep
# that read capability deliberately narrower than the deck process's ambient
# filesystem permissions: repo-local, regular, modest-sized text only.
MAX_INFORMAL_SOURCE_BYTES = 5 * 1024 * 1024
INFORMAL_SOURCE_SUFFIXES = frozenset({".md", ".markdown", ".txt"})
MAX_REVIEW_SOURCE_BYTES = 2 * 1024 * 1024

# Queue assembly may include GitHub and source/audit reads and can take longer
# than an HTTP proxy's request budget.  A completed result stays available as
# the last-known-good snapshot after this freshness TTL: the next read returns
# it immediately while one background refresh runs.  The last good snapshot is
# also persisted under the consumer repo's ignored ``.marathon`` runtime area,
# so restarting the deck does not turn an otherwise healthy review page into a
# long, empty cold start.
QUEUE_CACHE_TTL_SECONDS = 60.0

# A brand-new installation has no honest snapshot to serve.  Give its first
# background build a small bounded chance to finish; after that /api/queue
# returns a retryable 503 instead of hanging until Cloudflare/browser timeout.
QUEUE_COLD_START_WAIT_SECONDS = 3.0
QUEUE_REFRESH_ERROR_BACKOFF_SECONDS = 10.0
QUEUE_WORKER_SHUTDOWN_WAIT_SECONDS = 0.25

_QUEUE_CACHE_SCHEMA_VERSION = 1
_QUEUE_CACHE_DIRNAME = "deck-queue-cache"
_QUEUE_CACHE_MAX_BYTES = 16 * 1024 * 1024
_QUEUE_PAYLOAD_COUNT_KEYS = (
    "building",
    "landed_today",
    "view_building",
    "unresolved",
    "awaiting_refinement",
    "needs_attention",
    "needs_citation_mapping",
    "awaiting_audit",
    "dependency_blocked",
    "other_unresolved",
)

# The project graph is substantially more expensive than the queue: it spans
# every registered chapter, reconciles current source, and projects audit
# topology.  Keep one completed read around long enough for graph-tab reloads
# and coalesce concurrent browsers.  Like the queue cache this is process-local
# only, generation-guarded, and invalidated before a successful mutation is
# made visible to its caller.
PROJECT_GRAPH_CACHE_TTL_SECONDS = 30.0

# These successful POSTs change fields consumed by ``build_queue`` (issue
# verdict/state/body, active recovery jobs, or the chapter registry).  Other
# POST routes update declaration-review/intake/prover state that is not part of
# the queue payload and therefore must not detach an in-flight queue build.
_QUEUE_MUTATING_POST_ROUTES = frozenset({
    "/api/verdict",
    "/api/ship",
    "/api/reconcile-proposal",
    "/api/refresh-issue",
    "/api/drop-citation",
    "/api/requeue",
    "/api/recovery",
    "/api/approve-chapter",
})

_PROJECT_GRAPH_MUTATING_POST_ROUTES = frozenset({
    "/api/verdict",
    "/api/decl-verdict",
    "/api/batch-decl-verdict",
    "/api/reconcile-proposal",
    "/api/missing",
    "/api/ship",
    "/api/refresh-issue",
    "/api/drop-citation",
    "/api/requeue",
    "/api/recovery",
    "/api/approve-chapter",
})


class _QueueFlight:
    """One physical queue build shared by readers of the same chapter."""

    def __init__(self, generation: int, build: Callable[[], dict]):
        self.generation = generation
        self.build = build
        self.event = Event()
        self.payload: Optional[dict] = None
        self.error: Optional[BaseException] = None


class _QueueSnapshot:
    """One validated last-known-good payload and its freshness metadata."""

    def __init__(
        self,
        payload: dict,
        *,
        expires_at: float,
        saved_at: str,
        generation: int,
    ):
        self.payload = payload
        self.expires_at = expires_at
        self.saved_at = saved_at
        self.generation = generation


class QueueRefreshPending(RuntimeError):
    """The bounded cold-start wait elapsed while the queue is still building."""

    def __init__(self, message: str, *, refreshing: bool = True):
        super().__init__(message)
        self.refreshing = refreshing


class _ProjectGraphFlight:
    """One project-graph build shared by all concurrent readers."""

    def __init__(self, generation: int):
        self.generation = generation
        self.event = Event()
        self.payload: Optional[dict] = None
        self.error: Optional[BaseException] = None


def _queue_cache_path(repo_dir: Path, chapter: Optional[int]) -> Path:
    """Return the repo-local runtime path for one queue view."""
    name = "all" if chapter is None else f"chapter-{chapter}"
    return (
        Path(repo_dir) / ".marathon" / "review" / _QUEUE_CACHE_DIRNAME
        / f"{name}.json"
    )


def _valid_queue_payload(payload: object) -> bool:
    """Reject corrupt/foreign durable snapshots before presenting them.

    This cache is never authoritative for shipping, but a malformed runtime
    file must still not make the Review page claim that work is complete.
    Validate every counter and the minimum card identity/readiness shape.
    Unknown additive fields remain forwards-compatible.
    """
    if not isinstance(payload, dict) or not isinstance(payload.get("cards"), list):
        return False
    for key in _QUEUE_PAYLOAD_COUNT_KEYS:
        value = payload.get(key)
        if isinstance(value, bool) or not isinstance(value, int) or value < 0:
            return False
    for card in payload["cards"]:
        if not isinstance(card, dict):
            return False
        issue = card.get("id")
        if isinstance(issue, bool) or not isinstance(issue, int) or issue <= 0:
            return False
        if not isinstance(card.get("ready"), bool):
            return False
    return True


def _read_queue_snapshot(
    repo_dir: Path, chapter: Optional[int], generation: int
) -> Optional[_QueueSnapshot]:
    """Load one durable last-good queue as stale, failing closed on damage."""
    path = _queue_cache_path(repo_dir, chapter)
    try:
        if path.stat().st_size > _QUEUE_CACHE_MAX_BYTES:
            return None
        document = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        return None
    if not isinstance(document, dict):
        return None
    if document.get("schema_version") != _QUEUE_CACHE_SCHEMA_VERSION:
        return None
    stored_chapter = document.get("chapter")
    if isinstance(stored_chapter, bool) or stored_chapter != chapter:
        return None
    saved_at = document.get("saved_at")
    payload = document.get("payload")
    if not isinstance(saved_at, str) or not saved_at or not _valid_queue_payload(payload):
        return None
    return _QueueSnapshot(
        payload,
        expires_at=0.0,  # monotonic deadlines cannot survive a process restart
        saved_at=saved_at,
        generation=generation - 1,  # durable snapshots are always presented stale
    )


def _write_queue_snapshot(
    repo_dir: Path,
    chapter: Optional[int],
    payload: dict,
    saved_at: str,
) -> None:
    """Durably and atomically replace one validated last-good queue."""
    if not _valid_queue_payload(payload):
        return
    path = _queue_cache_path(repo_dir, chapter)
    path.parent.mkdir(parents=True, exist_ok=True)
    # This is droppable runtime state inside the consumer checkout.  Make the
    # directory self-ignoring so a first deck run cannot dirty the Lean repo or
    # accidentally include cached issue text in a future proof-worker bundle.
    gitignore = path.parent / ".gitignore"
    if not gitignore.is_file():
        gitignore.write_text("*\n", encoding="utf-8")
    tmp = path.with_name(
        f".{path.name}.{os.getpid()}.{threading.get_ident()}.tmp"
    )
    document = {
        "schema_version": _QUEUE_CACHE_SCHEMA_VERSION,
        "chapter": chapter,
        "saved_at": saved_at,
        "payload": payload,
    }
    try:
        encoded = json.dumps(
            document, ensure_ascii=False, indent=2, sort_keys=True
        ) + "\n"
        with tmp.open("w", encoding="utf-8") as handle:
            handle.write(encoded)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp, path)
        try:
            directory_fd = os.open(path.parent, os.O_RDONLY)
        except OSError:
            directory_fd = None
        if directory_fd is not None:
            try:
                os.fsync(directory_fd)
            except OSError:
                pass
            finally:
                os.close(directory_fd)
    finally:
        tmp.unlink(missing_ok=True)


def _repo_informal_source(repo_dir: Path, raw: str) -> Path:
    """Resolve a user-selected informal source without granting an arbitrary
    host-file read primitive.

    The HTTP caller must name an existing, repo-relative text file.  Resolving
    both sides before the containment check also closes symlink escapes.
    ``ValueError`` carries the safe message returned as HTTP 400.
    """
    supplied = Path(raw)
    if supplied.is_absolute():
        raise ValueError("informal-statements path must be repo-relative")
    repo = Path(repo_dir).resolve()
    try:
        candidate = (repo / supplied).resolve(strict=True)
    except (OSError, RuntimeError) as exc:
        raise ValueError(
            f"informal-statements file does not exist or cannot be resolved: {raw}"
        ) from exc
    try:
        candidate.relative_to(repo)
    except ValueError as exc:
        raise ValueError(
            "informal-statements path must stay inside the repository"
        ) from exc
    if not candidate.is_file():
        raise ValueError("informal-statements path must name a regular file")
    if candidate.suffix.lower() not in INFORMAL_SOURCE_SUFFIXES:
        raise ValueError(
            "informal-statements file must be Markdown or plain text "
            "(.md, .markdown, or .txt)"
        )
    try:
        size = candidate.stat().st_size
    except OSError as exc:
        raise ValueError("could not inspect informal-statements file") from exc
    if size > MAX_INFORMAL_SOURCE_BYTES:
        raise ValueError(
            "informal-statements file is too large "
            f"(maximum {MAX_INFORMAL_SOURCE_BYTES // (1024 * 1024)} MiB)"
        )
    return candidate


def _repo_lean_source(repo_dir: Path, raw: str) -> Path:
    """Resolve a repo-local ``.lean`` file for the whole-file reader.

    This GET is read-only but still must not become an arbitrary host-file read
    primitive. Absolute paths, traversal/symlink escapes, non-files, and
    non-Lean files are refused before reading.
    """
    supplied = Path(raw)
    if supplied.is_absolute():
        raise ValueError("source path must be repo-relative")
    repo = Path(repo_dir).resolve()
    try:
        candidate = (repo / supplied).resolve(strict=True)
    except (OSError, RuntimeError) as exc:
        raise ValueError(f"source file does not exist or cannot be resolved: {raw}") from exc
    try:
        candidate.relative_to(repo)
    except ValueError as exc:
        raise ValueError("source path must stay inside the repository") from exc
    if not candidate.is_file() or candidate.suffix.lower() != ".lean":
        raise ValueError("source path must name a repo-local .lean file")
    return candidate


def _truthy_flag(value) -> bool:
    """Interpret a ``skip_api`` flag from a query string (``"1"``/``"0"``/
    ``"true"``) or a JSON body (``True``/``1``/``"1"``). Anything else —
    ``None``, ``""``, ``"0"``, ``False`` — is falsy. Shared by the GET
    (?skip_api=) and POST (body) paths so both interpret the flag identically."""
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return value != 0
    if isinstance(value, str):
        return value.strip().lower() in ("1", "true", "yes", "on")
    return False


class DeckServer(ThreadingHTTPServer):
    """A ThreadingHTTPServer that carries the deck's request context (the
    ReviewConfig, the per-session token, the default chapter). The handler
    reads these off ``self.server`` so a fresh handler instance per request
    stays stateless."""

    daemon_threads = True
    allow_reuse_address = True

    def __init__(
        self,
        server_address,
        cfg: "ReviewConfig",
        token: str,
        default_chapter: Optional[int] = None,
        demo: bool = False,
        allow_host_conductor_control: bool = False,
        verso_manager=_DEFAULT_VERSO_MANAGER,
    ):
        self.cfg = cfg
        self.token = token
        self.default_chapter = default_chapter
        # Manual host lifecycle control is an explicit operator capability.
        # It is independent of MARATHON_NO_HOST_CONDUCTOR, which only prevents
        # automatic reject-triggered starts on shared decks.
        self.allow_host_conductor_control = bool(allow_host_conductor_control)
        self._conductor_start_lock = Lock()
        # Demo mode: the API routes to marathon.deck.demo (synthetic cards,
        # in-memory verdicts, a SIMULATED ship that fires nothing). No cfg /
        # repo / GitHub / Aristotle is touched.
        self.demo = demo
        # Per-deck registry of in-flight headless chapter-intake jobs (the
        # "Add Chapter" feature). Lazily exercised; harmless when unused.
        from marathon.deck.intake_jobs import IntakeRegistry
        from marathon.deck.ship_jobs import ShipJobRegistry

        self.intake = IntakeRegistry()
        # GitHub-backed shipping is accepted durably and completed off the
        # request thread.  This keeps a slow GitHub API from freezing the
        # review UI or colliding with the reverse proxy's request timeout.
        self.ship_jobs = ShipJobRegistry(
            acceptance_checker=self.cached_issue_reviewability
        )
        # The issue-level verdict fast path gets the same shape: durable
        # acceptance, background GitHub writes, browser polling.
        from marathon.deck.verdict_jobs import VerdictJobRegistry

        self.verdict_jobs = VerdictJobRegistry()
        # ThreadingHTTPServer gives every request its own thread.  Coalesce
        # expensive queue reads by chapter instead of letting those threads
        # stampede GitHub and the local audit store.  Builders run outside this
        # lock; only the tiny cache/flight registry is protected by it.
        self._queue_lock = Lock()
        self._queue_cache: dict[Optional[int], _QueueSnapshot] = {}
        self._queue_flights: dict[Optional[int], _QueueFlight] = {}
        self._queue_builders: dict[Optional[int], Callable[[], dict]] = {}
        self._queue_errors: dict[Optional[int], str] = {}
        self._queue_retry_after: dict[Optional[int], float] = {}
        self._queue_persisted_loaded: set[Optional[int]] = set()
        self._queue_generation = 0
        self._queue_shutdown = Event()
        self._queue_workers: set[Thread] = set()
        self._queue_persist_lock = Lock()
        self._project_graph_lock = Lock()
        self._project_graph_cache: Optional[tuple[float, dict]] = None
        self._project_graph_flight: Optional[_ProjectGraphFlight] = None
        self._project_graph_generation = 0

        # Verso elaboration is deliberately isolated behind its artifact
        # manager. Constructing the manager does no build work; ``serve``
        # prewarms its background single-flight builder before the first review
        # visit, and GET /api/verso-status remains a fail-safe trigger. Queue
        # and card routes never wait on it. Explicit ``None`` is used by demo
        # mode and focused server tests.
        self._verso_close_lock = Lock()
        self._verso_closed = False
        self._verso_unavailable_reason: Optional[str] = None
        if verso_manager is _DEFAULT_VERSO_MANAGER and not demo:
            try:
                from marathon.deck.verso import VersoArtifactManager

                verso_manager = VersoArtifactManager(cfg.repo_dir)
            except Exception:  # noqa: BLE001 — the deck must still start
                verso_manager = None
                self._verso_unavailable_reason = (
                    "Verso semantic documentation is not configured"
                )
        elif verso_manager is _DEFAULT_VERSO_MANAGER:
            verso_manager = None
            self._verso_unavailable_reason = (
                "Verso semantic documentation is unavailable in demo mode"
            )
        elif verso_manager is None:
            self._verso_unavailable_reason = (
                "Verso semantic documentation is not configured"
            )
        self.verso_manager = verso_manager
        super().__init__(server_address, DeckHandler)

    def prewarm_verso(self) -> Optional[dict]:
        """Ask the semantic artifact manager to catch up, without blocking.

        ``VersoArtifactManager.trigger`` only starts or joins its daemon worker.
        Keeping this tiny boundary fail-soft means a missing toolchain can never
        prevent the raw-source review deck from starting or syncing.
        """

        manager = self.verso_manager
        if manager is None or self._verso_closed:
            return None
        try:
            return manager.trigger()
        except Exception:  # noqa: BLE001 — raw source remains authoritative
            return None

    def server_close(self) -> None:
        """Close the HTTP socket and the server-owned artifact manager.

        ``server_close`` is commonly called from both fixture cleanup and a
        surrounding ``finally`` block.  Keep manager shutdown idempotent and
        nonblocking: stopping the review UI must not wait for a Lake/Verso
        subprocess that was already in flight.
        """
        self._stop_queue_workers()
        with self._verso_close_lock:
            if not self._verso_closed:
                self._verso_closed = True
                manager = self.verso_manager
                if manager is not None:
                    try:
                        manager.shutdown(wait=False)
                    except Exception:  # noqa: BLE001 — closing stays best-effort
                        pass
        super().server_close()

    def _stop_queue_workers(self) -> None:
        """Close queue publication and briefly reap cooperative workers.

        Queue builders may be blocked in GitHub or local source inspection, so
        shutdown has a single total wait budget rather than joining each worker
        indefinitely.  Workers that finish later see ``_queue_shutdown`` and
        cannot publish, persist, or launch a successor generation.
        """
        deadline = monotonic() + QUEUE_WORKER_SHUTDOWN_WAIT_SECONDS
        with self._queue_lock:
            self._queue_shutdown.set()
            self._queue_generation += 1
            workers = tuple(self._queue_workers)
            flights = tuple(self._queue_flights.values())
            self._queue_flights.clear()
            for flight in flights:
                flight.event.set()
        # Drain a persistence operation that began before the shutdown signal
        # when it fits inside the same total budget. No later writer may begin:
        # every writer rechecks the signal while holding this gate.
        remaining = deadline - monotonic()
        if remaining > 0 and self._queue_persist_lock.acquire(timeout=remaining):
            self._queue_persist_lock.release()
        for worker in workers:
            remaining = deadline - monotonic()
            if remaining <= 0:
                break
            worker.join(timeout=remaining)

    def queue_payload(
        self,
        chapter: Optional[int],
        build: Callable[[], dict],
        *,
        cold_wait_seconds: float = QUEUE_COLD_START_WAIT_SECONDS,
    ) -> dict:
        """Return fresh data or an immediate stale-while-refresh snapshot.

        Exactly one physical build runs per chapter.  When a last-known-good
        value exists, expiration and mutation only make it *stale*: callers get
        that clearly-labelled snapshot immediately while a daemon worker
        refreshes it.  With no snapshot, callers share the same worker and wait
        only ``cold_wait_seconds`` before receiving :class:`QueueRefreshPending`.
        """
        try:
            wait_budget = max(0.0, float(cold_wait_seconds))
        except (TypeError, ValueError):
            wait_budget = QUEUE_COLD_START_WAIT_SECONDS
        deadline = monotonic() + wait_budget

        while True:
            with self._queue_lock:
                # Keep the newest factory for an invalidation-triggered followup.
                # HTTP callers bind only cfg+chapter, never the request handler.
                self._queue_builders[chapter] = build
                if chapter not in self._queue_persisted_loaded:
                    persisted = _read_queue_snapshot(
                        self.cfg.repo_dir, chapter, self._queue_generation
                    )
                    if persisted is not None:
                        self._queue_cache[chapter] = persisted
                    self._queue_persisted_loaded.add(chapter)

                snapshot = self._queue_cache.get(chapter)
                now = monotonic()
                fresh = bool(
                    snapshot is not None
                    and snapshot.generation == self._queue_generation
                    and snapshot.expires_at > now
                )
                if fresh:
                    assert snapshot is not None
                    return self._queue_response_locked(
                        chapter, snapshot, state="fresh"
                    )

                flight = self._start_queue_refresh_locked(chapter)
                if snapshot is not None:
                    return self._queue_response_locked(
                        chapter, snapshot, state="stale"
                    )

            if flight is None:
                raise QueueRefreshPending(
                    "Queue has no saved snapshot and its refresh could not start",
                    refreshing=False,
                )
            remaining = deadline - monotonic()
            if remaining <= 0 or not flight.event.wait(timeout=remaining):
                raise QueueRefreshPending(
                    "Queue is preparing its first snapshot; retry shortly"
                )
            # If this was the current generation, propagate an immediate build
            # failure honestly.  A mutation can supersede a failed/finished
            # flight; in that case loop and wait for its generation's successor.
            with self._queue_lock:
                current_generation = self._queue_generation
            if flight.generation == current_generation and flight.error is not None:
                if isinstance(flight.error, Exception):
                    raise flight.error
                raise RuntimeError(f"queue refresh failed: {flight.error}")

    def _queue_response_locked(
        self,
        chapter: Optional[int],
        snapshot: _QueueSnapshot,
        *,
        state: str,
    ) -> dict:
        """Copy one payload and attach non-authoritative freshness metadata."""
        response = dict(snapshot.payload)
        if state == "fresh":
            return response
        flight = self._queue_flights.get(chapter)
        metadata = {
            "state": state,
            "refreshing": flight is not None,
            "saved_at": snapshot.saved_at,
        }
        error = self._queue_errors.get(chapter)
        if error and state != "fresh":
            metadata["last_refresh_error"] = error
        retry_at = self._queue_retry_after.get(chapter, 0.0)
        if flight is None and retry_at > monotonic():
            metadata["retry_after_seconds"] = max(
                1, int(retry_at - monotonic() + 0.999)
            )
        response["queue_snapshot"] = metadata
        return response

    def _start_queue_refresh_locked(
        self, chapter: Optional[int]
    ) -> Optional[_QueueFlight]:
        """Start (or join) the one physical background build for ``chapter``.

        Caller holds ``_queue_lock``.  A superseded generation is deliberately
        left running rather than detached: when it exits it schedules one build
        for the newest generation.  Rapid mutations therefore cannot create a
        pile-up of concurrent GitHub/source scans.
        """
        existing = self._queue_flights.get(chapter)
        if existing is not None:
            return existing
        if self._queue_shutdown.is_set():
            return None
        if self._queue_retry_after.get(chapter, 0.0) > monotonic():
            return None
        build = self._queue_builders.get(chapter)
        if build is None:
            return None
        flight = _QueueFlight(self._queue_generation, build)
        self._queue_flights[chapter] = flight
        # Queue snapshots are process-global and may outlive any one HTTP
        # request.  Always run under a deliberately empty context so a shared
        # cache neither depends on nor retains a collaborator's short-lived
        # OAuth token from a mutating POST.
        context = Context()
        worker = Thread(
            target=context.run,
            args=(self._run_queue_refresh, chapter, flight),
            name=f"marathon-queue-{chapter if chapter is not None else 'all'}",
            daemon=True,
        )
        self._queue_workers.add(worker)
        try:
            worker.start()
        except BaseException as exc:
            flight.error = exc
            flight.event.set()
            self._queue_flights.pop(chapter, None)
            self._queue_workers.discard(worker)
            self._queue_errors[chapter] = str(exc)[:500]
            self._queue_retry_after[chapter] = (
                monotonic() + QUEUE_REFRESH_ERROR_BACKOFF_SECONDS
            )
            return None
        return flight

    def _run_queue_refresh(
        self, chapter: Optional[int], flight: _QueueFlight
    ) -> None:
        """Complete one background build and publish only its own generation."""
        payload: Optional[dict] = None
        error: Optional[BaseException] = None
        try:
            candidate = flight.build()
            if not isinstance(candidate, dict):
                raise TypeError("queue builder returned a non-object payload")
            if not _valid_queue_payload(candidate):
                raise TypeError("queue builder returned a malformed payload")
            payload = candidate
        except BaseException as exc:  # always wake bounded cold-start readers
            error = exc

        persist: Optional[tuple[dict, str]] = None
        retry_new_generation = False
        with self._queue_lock:
            flight.payload = payload
            flight.error = error
            registered = self._queue_flights.get(chapter) is flight
            current = flight.generation == self._queue_generation
            shutting_down = self._queue_shutdown.is_set()
            if (
                registered and current and not shutting_down
                and error is None and payload is not None
            ):
                saved_at = datetime.now(timezone.utc).isoformat().replace(
                    "+00:00", "Z"
                )
                snapshot = _QueueSnapshot(
                    payload,
                    expires_at=monotonic() + QUEUE_CACHE_TTL_SECONDS,
                    saved_at=saved_at,
                    generation=self._queue_generation,
                )
                self._queue_cache[chapter] = snapshot
                self._queue_persisted_loaded.add(chapter)
                self._queue_errors.pop(chapter, None)
                self._queue_retry_after.pop(chapter, None)
                persist = (payload, saved_at)
            elif registered and current and not shutting_down and error is not None:
                self._queue_errors[chapter] = str(error)[:500]
                self._queue_retry_after[chapter] = (
                    monotonic() + QUEUE_REFRESH_ERROR_BACKOFF_SECONDS
                )

            if registered:
                self._queue_flights.pop(chapter, None)
                retry_new_generation = not current and not shutting_down
            flight.event.set()

        try:
            # Persistence is best-effort and never delays a caller: the cache
            # is a rendering fallback, while shipping performs a fresh gate.
            if persist is not None:
                with self._queue_persist_lock:
                    if not self._queue_shutdown.is_set():
                        try:
                            _write_queue_snapshot(
                                self.cfg.repo_dir,
                                chapter,
                                persist[0],
                                persist[1],
                            )
                        except Exception:
                            pass

            if retry_new_generation and not self._queue_shutdown.is_set():
                with self._queue_lock:
                    self._start_queue_refresh_locked(chapter)

            # Pre-warm the READY issues' assembled details so the issue the
            # reviewer opens next is already built (the plan's prefetch-depth
            # idea). Bounded, single-flight, and strictly best-effort.
            if persist is not None and not self._queue_shutdown.is_set():
                self._start_detail_prewarm(persist[0])
        finally:
            with self._queue_lock:
                self._queue_workers.discard(threading.current_thread())

    def invalidate_queue_cache(self) -> None:
        """Expire last-good views and request one post-mutation refresh.

        Existing snapshots remain available for rendering but are clearly
        stale.  A running older-generation build is allowed to finish without
        publishing and then launches exactly one successor for the newest
        generation; this is both mutation-safe and stampede-free.

        The deck-context memo and the dashboard's stale-while-revalidate
        entries are dropped on the same trigger: every call site here is a
        successful read-model-affecting mutation, exactly the moment those
        views must stop serving pre-mutation state.  Per-declaration swipes
        never call this, so the swipe hot path keeps its warm context.
        """
        from marathon.deck import cards as _cards
        from marathon.deck import dashboard as _dashboard

        _cards.invalidate_context_cache()
        _dashboard.invalidate_swr()
        with self._queue_lock:
            if self._queue_shutdown.is_set():
                return
            self._queue_generation += 1
            self._queue_retry_after.clear()
            for snapshot in self._queue_cache.values():
                snapshot.expires_at = 0.0
            for chapter in tuple(self._queue_builders):
                self._start_queue_refresh_locked(chapter)

    def _start_detail_prewarm(self, payload: dict) -> None:
        """Warm the ready issues' assembled details after a queue build.

        One background walker at a time, bounded to the first few ready
        rows; each build lands in the per-issue detail memo so the reviewer's
        next open is served instantly. Never raises, never blocks a caller.
        """
        if getattr(self, "cfg", None) is None:
            return
        try:
            ready_ids = [
                int(card.get("id"))
                for card in (payload.get("cards") or [])
                if card.get("ready") and card.get("id") is not None
            ][:5]
        except (TypeError, ValueError):
            return
        if not ready_ids:
            return
        with self._queue_lock:
            if getattr(self, "_detail_prewarm_running", False):
                return
            self._detail_prewarm_running = True

        def _warm() -> None:
            from marathon.deck.cards import build_card_detail_cached

            try:
                for num in ready_ids:
                    if self._queue_shutdown.is_set():
                        return
                    try:
                        build_card_detail_cached(self.cfg, num)
                    except Exception:  # noqa: BLE001 — warming is best-effort
                        pass
            finally:
                with self._queue_lock:
                    self._detail_prewarm_running = False

        Thread(
            target=_warm, name="deck-detail-prewarm", daemon=True
        ).start()

    def cached_issue_reviewability(
        self, _cfg: "ReviewConfig", issue_num: int
    ) -> Optional[dict]:
        """Return a nonblocking cached ship gate, or ``None`` when unknown.

        POST /api/ship must acknowledge quickly even when rebuilding the queue
        would need GitHub.  A still-live queue snapshot can reject an obviously
        dependency-blocked request immediately; a cache miss is accepted into
        the durable outbox and the worker performs the mandatory fresh global
        check before any irreversible action.
        """
        now = monotonic()
        chapter = self.cfg.chapter_of_issue(issue_num)
        with self._queue_lock:
            snapshots = []
            for key in (None, chapter):
                cached = self._queue_cache.get(key)
                if (
                    cached is not None
                    and cached.generation == self._queue_generation
                    and cached.expires_at > now
                ):
                    snapshots.append(cached.payload)
        for payload in snapshots:
            for card in payload.get("cards") or []:
                if card.get("id") != issue_num:
                    continue
                ready = bool(card.get("shippable", card.get("ready")))
                return {
                    "issue": issue_num,
                    "inspectable": bool(card.get("inspectable", ready)),
                    "shippable": ready,
                    "provisional": bool(card.get("provisional")),
                    "blocked_reason": card.get("blocked_reason"),
                    "inspection_blocked_reason": card.get(
                        "inspection_blocked_reason"
                    ),
                }
        return None

    def project_graph_payload(self, build: Callable[[], dict]) -> dict:
        """Return a fresh-enough graph, sharing one project-wide build."""
        with self._project_graph_lock:
            cached = self._project_graph_cache
            now = monotonic()
            if cached is not None and cached[0] > now:
                return cached[1]
            if cached is not None:
                self._project_graph_cache = None

            flight = self._project_graph_flight
            owner = flight is None
            if owner:
                flight = _ProjectGraphFlight(self._project_graph_generation)
                self._project_graph_flight = flight

        assert flight is not None
        if not owner:
            flight.event.wait()
            if flight.error is not None:
                raise flight.error
            assert flight.payload is not None
            return flight.payload

        try:
            payload = build()
        except BaseException as exc:
            with self._project_graph_lock:
                flight.error = exc
                if self._project_graph_flight is flight:
                    self._project_graph_flight = None
                flight.event.set()
            raise

        with self._project_graph_lock:
            flight.payload = payload
            if flight.generation == self._project_graph_generation:
                self._project_graph_cache = (
                    monotonic() + PROJECT_GRAPH_CACHE_TTL_SECONDS,
                    payload,
                )
            if self._project_graph_flight is flight:
                self._project_graph_flight = None
            flight.event.set()
        return payload

    def invalidate_project_graph_cache(self) -> None:
        """Make subsequent graph reads observe post-mutation project state."""
        with self._project_graph_lock:
            self._project_graph_generation += 1
            self._project_graph_cache = None
            self._project_graph_flight = None


class DeckHandler(BaseHTTPRequestHandler):
    """Routes the deck's API + static surface. Every ``do_GET`` route is a
    pure read; the single side-effecting route (``POST /api/verdict``) is
    token-guarded."""

    server_version = "marathon-deck/1.0"
    protocol_version = "HTTP/1.1"

    # Quieter logging: the default BaseHTTPRequestHandler logs every
    # request to stderr, which is noisy for a local app. Route through the
    # module's own minimal line so tests (and operators) aren't spammed.
    def log_message(self, fmt: str, *args) -> None:  # noqa: D401
        return

    # --- shared response helpers ------------------------------------------

    @property
    def cfg(self) -> "ReviewConfig":
        return self.server.cfg  # type: ignore[attr-defined]

    def _send_json(self, payload: dict, status: int = HTTPStatus.OK) -> None:
        # A successful read-model-affecting POST invalidates immediately after
        # its durable work and *before* the response is visible.  The browser
        # may refresh as soon as it receives this response; invalidating in
        # do_POST's finally block would leave a race where that read sees an old
        # queue/graph cache entry. Errors and read-only POSTs leave flights be.
        if (
            getattr(self, "_queue_invalidation_pending", False)
            and not getattr(self, "_queue_cache_invalidated", False)
            and HTTPStatus.OK <= int(status) < HTTPStatus.MULTIPLE_CHOICES
            and payload.get("ok") is not False
            and payload.get("updated") is not False
        ):
            self.server.invalidate_queue_cache()  # type: ignore[attr-defined]
            self._queue_cache_invalidated = True
        if (
            getattr(self, "_project_graph_invalidation_pending", False)
            and not getattr(self, "_project_graph_cache_invalidated", False)
            and HTTPStatus.OK <= int(status) < HTTPStatus.MULTIPLE_CHOICES
            and payload.get("ok") is not False
            and payload.get("updated") is not False
        ):
            self.server.invalidate_project_graph_cache()  # type: ignore[attr-defined]
            self._project_graph_cache_invalidated = True
        body = json.dumps(payload).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        # Lock the surface down: no caching of dynamic reads, no embedding.
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.end_headers()
        self.wfile.write(body)

    def _send_error_json(self, status: int, message: str) -> None:
        self._send_json({"ok": False, "error": message}, status=status)

    def _send_bytes(
        self, body: bytes, content_type: str, status: int = HTTPStatus.OK,
        cache_control: Optional[str] = None,
        extra_headers: Optional[dict[str, str]] = None,
    ) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("X-Content-Type-Options", "nosniff")
        if cache_control is not None:
            self.send_header("Cache-Control", cache_control)
        for name, value in (extra_headers or {}).items():
            self.send_header(name, value)
        self.end_headers()
        self.wfile.write(body)

    # --- GET (pure reads only) --------------------------------------------

    def do_GET(self) -> None:  # noqa: N802 (stdlib handler name)
        # A BaseHTTPRequestHandler instance can serve multiple keep-alive
        # requests; never carry a prior POST's invalidation marker into a GET.
        self._queue_invalidation_pending = False
        self._queue_cache_invalidated = False
        self._project_graph_invalidation_pending = False
        self._project_graph_cache_invalidated = False
        parsed = urlsplit(self.path)
        path = parsed.path
        query = parse_qs(parsed.query)
        try:
            if path in ("/", "/index.html"):
                return self._serve_index()
            if path.startswith("/static/"):
                return self._serve_static(path)
            if path.startswith("/verso/"):
                return self._serve_verso(path)
            if path == "/api/queue":
                return self._api_queue(query)
            if path.startswith("/api/card/"):
                return self._api_card(path[len("/api/card/"):])
            if path.startswith("/api/subdeck/"):
                return self._api_subdeck(path[len("/api/subdeck/"):], query)
            if path.startswith("/api/missing-suggestions/"):
                return self._api_missing_suggestions(
                    path[len("/api/missing-suggestions/"):]
                )
            if path == "/api/ship-status":
                return self._api_ship_status(query)
            if path == "/api/status":
                return self._api_status()
            if path == "/api/digest":
                return self._api_digest()
            if path == "/api/verdict-status":
                return self._api_verdict_status(query)
            if path == "/api/events":
                return self._api_events()
            if path == "/api/conductor-status":
                return self._api_conductor_status()
            if path.startswith("/api/run/"):
                return self._api_run_detail(path[len("/api/run/"):])
            if path == "/api/issues-overview":
                return self._api_issues_overview()
            if path == "/api/review-workspace":
                return self._api_review_workspace(query)
            if path == "/api/review-source":
                return self._api_review_source(query)
            if path == "/api/project-graph":
                return self._api_project_graph(query)
            if path == "/api/definition":
                return self._api_definition(query)
            if path == "/api/definition-terms":
                return self._api_definition_terms()
            if path == "/api/verso-status":
                return self._api_verso_status()
            if path == "/api/verso-manifest":
                return self._api_verso_manifest()
            if path.startswith("/api/reconciliation/"):
                return self._api_reconciliation(
                    path[len("/api/reconciliation/"):]
                )
            if path == "/api/stuck":
                return self._api_stuck()
            if path == "/api/history":
                return self._api_history(query)
            self._send_error_json(HTTPStatus.NOT_FOUND, f"no route {path!r}")
        except BrokenPipeError:  # client navigated away mid-stream
            return
        except Exception as exc:  # noqa: BLE001 — a read must never 500-crash
            self._send_error_json(
                HTTPStatus.INTERNAL_SERVER_ERROR, f"read failed: {exc}"
            )

    def _serve_index(self) -> None:
        """Serve index.html with the per-session token injected. A POST is
        only honored when it echoes this token, so injecting it here is what
        authorizes the session that loaded the page."""
        index = STATIC_DIR / "index.html"
        try:
            html = index.read_text()
        except OSError:
            # The other track owns index.html; tolerate it being absent
            # (early launch / tests) with a minimal placeholder page that
            # still carries the token hook so the contract holds.
            html = (
                "<!doctype html><meta charset=utf-8>"
                f'<meta name="marathon-session-token" '
                f'content="{TOKEN_PLACEHOLDER}">'
                "<title>marathon deck</title>"
                "<p>deck backend is up; the frontend assets are not "
                "installed yet.</p>"
            )
        token = self.server.token  # type: ignore[attr-defined]
        html = html.replace(TOKEN_PLACEHOLDER, token)
        html = html.replace(ASSET_VERSION_PLACEHOLDER, _asset_version())
        # no-cache: the index is tiny + dynamic (token + asset version); always
        # revalidate so a redeployed bundle's new ?v= reaches the browser.
        self._send_bytes(
            html.encode("utf-8"), _CONTENT_TYPES[".html"], cache_control="no-cache"
        )

    def _serve_static(self, path: str) -> None:
        """Serve a file under STATIC_DIR. Path-traversal-safe (the resolved
        path must stay within STATIC_DIR) and tolerant of a missing dir/file
        (404, never a crash). index.html is routed through ``_serve_index``
        so the token is always injected — a raw static index would leak the
        placeholder."""
        rel = path[len("/static/"):]
        if rel in ("index.html", ""):
            return self._serve_index()
        try:
            candidate = (STATIC_DIR / rel).resolve()
            candidate.relative_to(STATIC_DIR.resolve())
        except (ValueError, OSError):
            return self._send_error_json(
                HTTPStatus.FORBIDDEN, "path outside the static root"
            )
        if not candidate.is_file():
            return self._send_error_json(
                HTTPStatus.NOT_FOUND, f"no static asset {rel!r}"
            )
        content_type = _CONTENT_TYPES.get(
            candidate.suffix, "application/octet-stream"
        )
        self._send_bytes(candidate.read_bytes(), content_type)

    def _serve_verso(self, path: str) -> None:
        """Serve one file explicitly authorized by the artifact manager.

        Decode exactly once, reject every absolute/traversing spelling before
        consulting the manager, and never infer a filesystem path here.  The
        manager resolves only against the current/last-good artifact, so stale
        build directories and arbitrary repo files cannot become HTTP-visible.
        Generated pages are served ``no-store`` because the stable URL names
        follow the moving last-good artifact rather than containing a content
        digest.
        """
        manager = getattr(self.server, "verso_manager", None)
        if manager is None:
            return self._send_error_json(
                HTTPStatus.SERVICE_UNAVAILABLE,
                getattr(
                    self.server,
                    "_verso_unavailable_reason",
                    "Verso semantic documentation is unavailable",
                ),
            )

        try:
            raw = unquote(path[len("/verso/"):], errors="strict")
        except (UnicodeDecodeError, ValueError):
            return self._send_error_json(
                HTTPStatus.FORBIDDEN, "invalid Verso artifact path"
            )
        rel = PurePosixPath(raw)
        if (
            not raw
            or "\\" in raw
            or rel.is_absolute()
            or any(part in ("", ".", "..") for part in rel.parts)
        ):
            return self._send_error_json(
                HTTPStatus.FORBIDDEN, "path outside the Verso artifact"
            )
        # New clients pin every page, asset, and hover-table request to the
        # manifest's immutable artifact token. Keep accepting the legacy
        # moving mount for old tabs, but never silently fall back when an
        # explicit token was supplied.
        parts = rel.parts
        artifact_token = (
            parts[0]
            if parts and _VERSO_ARTIFACT_TOKEN_RE.fullmatch(parts[0])
            else None
        )
        artifact_path = raw
        if artifact_token is not None:
            artifact_path = "/".join(parts[1:])
            if raw.endswith("/") and artifact_path:
                artifact_path += "/"
        try:
            # Preserve a trailing slash: Verso's manifest uses directory URLs
            # and the manager deliberately resolves those to ``index.html``.
            candidate = manager.resolve_artifact(
                artifact_path, artifact_token=artifact_token
            )
        except (OSError, ValueError):
            candidate = None
        if candidate is None:
            return self._send_error_json(
                HTTPStatus.NOT_FOUND, f"no Verso artifact {rel.as_posix()!r}"
            )
        candidate = Path(candidate)
        if not candidate.is_file():
            return self._send_error_json(
                HTTPStatus.NOT_FOUND, f"no Verso artifact {rel.as_posix()!r}"
            )
        try:
            body = candidate.read_bytes()
        except OSError:
            return self._send_error_json(
                HTTPStatus.NOT_FOUND, f"no Verso artifact {rel.as_posix()!r}"
            )
        content_type = _CONTENT_TYPES.get(
            candidate.suffix.lower(), "application/octet-stream"
        )
        extra_headers = None
        if candidate.suffix.lower() in _VERSO_ACTIVE_DOCUMENT_SUFFIXES:
            # Generated pages may be opened for whole-file reading, but they
            # are never trusted application code.  A sandboxed, scriptless
            # document cannot read Marathon's injected session token or call
            # its privileged POST endpoints, even if an artifact is tampered.
            extra_headers = {
                "Content-Security-Policy": _VERSO_HTML_CSP,
                "Cross-Origin-Opener-Policy": "same-origin",
                "Referrer-Policy": "no-referrer",
                "X-Frame-Options": "DENY",
            }
        self._send_bytes(
            body,
            content_type,
            cache_control="no-store",
            extra_headers=extra_headers,
        )

    def _verso_status_payload(self, *, trigger: bool) -> dict:
        """Return the stable frontend status contract without blocking."""
        manager = getattr(self.server, "verso_manager", None)
        if manager is None:
            return {
                "state": "unavailable",
                "available": False,
                "stale": False,
                "source_token": None,
                "artifact_token": None,
                "error": getattr(
                    self.server,
                    "_verso_unavailable_reason",
                    "Verso semantic documentation is unavailable",
                ),
                "root_url": "/verso/",
            }

        raw = manager.status(trigger=trigger)
        state = str(raw.get("state") or "unavailable")
        error = raw.get("error")
        if error is None and state in {"error", "failed", "unavailable"}:
            error = raw.get("message")
        artifact_token = raw.get("artifact_token")
        root_url = (
            f"/verso/{artifact_token}/"
            if isinstance(artifact_token, str)
            and _VERSO_ARTIFACT_TOKEN_RE.fullmatch(artifact_token)
            else "/verso/"
        )
        return {
            "state": state,
            "available": bool(raw.get("available", False)),
            "stale": bool(raw.get("stale", False)),
            "source_token": raw.get("source_token"),
            "artifact_token": artifact_token,
            "error": error,
            "root_url": root_url,
        }

    def _api_verso_status(self) -> None:
        # ``status(trigger=True)`` may only enqueue the manager's background
        # work.  It never executes Lake/Verso in this request thread.
        self._send_json(self._verso_status_payload(trigger=True))

    def _api_verso_manifest(self) -> None:
        manager = getattr(self.server, "verso_manager", None)
        if manager is None:
            status = self._verso_status_payload(trigger=False)
            return self._send_json(
                {"ok": False, **status}, status=HTTPStatus.SERVICE_UNAVAILABLE
            )
        manifest = manager.manifest(trigger=False)
        if manifest is None:
            status = self._verso_status_payload(trigger=False)
            # A build/error is retryable; a genuinely absent artifact is a
            # normal 404.  Both responses stay machine-readable and truthful.
            code = (
                HTTPStatus.SERVICE_UNAVAILABLE
                if status["state"] in {
                    "building", "error", "failed", "unavailable"
                }
                else HTTPStatus.NOT_FOUND
            )
            return self._send_json({"ok": False, **status}, status=code)
        payload = dict(manifest)
        payload.setdefault("constants", {})
        payload.setdefault("modules", {})
        token = payload.get("artifact_token")
        payload.setdefault(
            "root_url",
            f"/verso/{token}/"
            if isinstance(token, str)
            and _VERSO_ARTIFACT_TOKEN_RE.fullmatch(token)
            else "/verso/",
        )
        self._send_json(payload)

    def _api_verso_retry(self) -> None:
        """Explicitly clear the sticky failed-token guard and retry once.

        Ordinary status polling intentionally leaves a failed build parked so
        one persistent Lean error cannot consume the machine forever.  This
        token-guarded local action is the deliberate recovery path for a
        transient timeout, dependency fetch, or tool failure.
        """

        data = self._read_guarded_body()
        if data is None:
            return
        manager = getattr(self.server, "verso_manager", None)
        if manager is None:
            return self._send_error_json(
                HTTPStatus.SERVICE_UNAVAILABLE,
                getattr(
                    self.server,
                    "_verso_unavailable_reason",
                    "Verso semantic documentation is unavailable",
                ),
            )
        manager.trigger(force=True)
        self._send_json({"ok": True, **self._verso_status_payload(trigger=False)})

    @property
    def _demo(self) -> bool:
        return bool(getattr(self.server, "demo", False))  # type: ignore[attr-defined]

    def _api_queue(self, query: dict) -> None:
        if self._demo:
            from marathon.deck.demo import demo_queue
            return self._send_json(demo_queue())
        from marathon.deck.cards import build_queue

        chapter = self._chapter_from_query(query)
        cfg = self.cfg
        try:
            payload = self.server.queue_payload(  # type: ignore[attr-defined]
                chapter,
                lambda cfg=cfg, chapter=chapter: build_queue(
                    cfg, chapter
                ).to_json(),
            )
        except QueueRefreshPending as exc:
            return self._send_json(
                {
                    "ok": False,
                    "error": str(exc),
                    "queue_snapshot": {
                        "state": "warming",
                        "refreshing": exc.refreshing,
                        "saved_at": None,
                    },
                },
                status=HTTPStatus.SERVICE_UNAVAILABLE,
            )
        self._send_json(payload)

    def _api_card(self, raw_id: str) -> None:
        from marathon.deck.cards import build_card_detail_cached

        try:
            issue_num = int(raw_id)
        except ValueError:
            return self._send_error_json(
                HTTPStatus.BAD_REQUEST, f"bad card id {raw_id!r}"
            )
        detail = build_card_detail_cached(self.cfg, issue_num)
        self._send_json(detail.to_json())

    def _api_subdeck(self, raw_id: str, query: dict) -> None:
        """The issue's per-declaration subdeck — a PURE read (the contract's
        GET /api/subdeck/{issue}?skip_api=0|1). Assembling it reads the issue's
        card detail + the LOCAL per-decl store; it records nothing and fires no
        verdict. The ``skip_api`` flag only changes the assembled VIEW (which
        decls land in ``cards`` vs the ``auto_approved`` LIST); it NEVER writes
        and NEVER auto-approves — auto-approval is realized only at ship."""
        try:
            issue_num = int(raw_id)
        except ValueError:
            return self._send_error_json(
                HTTPStatus.BAD_REQUEST, f"bad subdeck id {raw_id!r}"
            )
        skip_api = _truthy_flag((query.get("skip_api") or [None])[0])
        if self._demo:
            from marathon.deck.demo import demo_subdeck
            return self._send_json(demo_subdeck(issue_num, skip_api=skip_api))
        from marathon.deck.subdeck import build_subdeck

        self._send_json(build_subdeck(self.cfg, issue_num, skip_api=skip_api))

    def _api_missing_suggestions(self, raw_id: str) -> None:
        """GET /api/missing-suggestions/{issue} — PURE read. The declarations
        the issue's title-promised result may ALREADY be formalized as, shown
        when the reviewer opens "flag a missing declaration" so a false-missing
        doesn't dispatch a redundant Aristotle ADD. Records nothing, fires no
        verdict; advisory-only (a network/parse failure must never block Save)."""
        try:
            issue_num = int(raw_id)
        except ValueError:
            return self._send_error_json(
                HTTPStatus.BAD_REQUEST, f"bad issue id {raw_id!r}"
            )
        if self._demo:
            from marathon.deck.demo import demo_missing_suggestions
            return self._send_json(
                {"issue": issue_num, "candidates": demo_missing_suggestions(issue_num)}
            )
        from marathon.deck.cards import missing_suggestions_for_issue

        candidates = missing_suggestions_for_issue(self.cfg, issue_num)
        self._send_json({"issue": issue_num, "candidates": candidates})

    def _api_status(self) -> None:
        """Poll endpoint: the live conductor/landing events as one JSON
        blob. Pure read from jobs.json + landings.jsonl + bounce reports."""
        self._send_json(collect_status(self.cfg))

    def _api_digest(self) -> None:
        """The landing digest: a local-only, sub-second answer to "what
        happened while I was away, and does anything need me?" — conductor
        health, parked issues with failure evidence, pending reconcile
        confirmations, prover provenance, audit coverage, recent verdicts.
        Pure read; never touches GitHub."""
        if self._demo:
            return self._send_json({
                "generated_at": None,
                "conductor": {"alive": True, "ready": True, "pid": None,
                              "concurrency": 1, "updated_ts": None,
                              "degraded": None},
                "prover": {"enabled": True},
                "jobs": [], "needs_me": [], "in_progress": [],
                "audit": None, "verso": None, "recent_verdicts": [],
            })
        from marathon.deck.dashboard import boot_digest

        try:
            verso = self._verso_status_payload(trigger=False)
        except Exception:  # noqa: BLE001 — the digest renders without Verso
            verso = None
        self._send_json(boot_digest(self.cfg, verso_status=verso))

    def _api_verdict_status(self, query: dict) -> None:
        """Pure-read poll endpoint for one durably accepted async verdict."""
        if self._demo:
            return self._send_error_json(
                HTTPStatus.NOT_FOUND, "verdict jobs are unavailable in demo mode"
            )
        raw = (query.get("issue") or [None])[0]
        try:
            issue_num = int(raw)
        except (TypeError, ValueError):
            return self._send_error_json(
                HTTPStatus.BAD_REQUEST, f"bad or missing issue {raw!r}"
            )
        record = self.server.verdict_jobs.status(  # type: ignore[attr-defined]
            self.cfg, issue_num
        )
        if record is None:
            return self._send_error_json(
                HTTPStatus.NOT_FOUND, f"no verdict job for issue #{issue_num}"
            )
        self._send_json({"ok": True, "job": record})

    def _api_ship_status(self, query: dict) -> None:
        """Pure-read poll endpoint for one durably accepted ship job."""
        issue_raw = (query.get("issue") or [None])[0]
        try:
            issue_num = int(issue_raw)
        except (TypeError, ValueError):
            return self._send_error_json(
                HTTPStatus.BAD_REQUEST, f"bad or missing issue {issue_raw!r}"
            )
        operation_id = (query.get("operation_id") or [None])[0]
        request_id = (query.get("request_id") or [None])[0]
        if request_id is not None and (
            not isinstance(request_id, str)
            or re.fullmatch(r"[0-9a-f]{32}", request_id) is None
        ):
            return self._send_error_json(
                HTTPStatus.BAD_REQUEST, "bad ship request id"
            )
        snapshot = self.server.ship_jobs.status(  # type: ignore[attr-defined]
            self.cfg,
            issue_num,
            operation_id=operation_id,
            request_id=request_id,
        )
        if snapshot is None:
            return self._send_error_json(
                HTTPStatus.NOT_FOUND,
                f"no matching ship job for issue #{issue_num}",
            )
        self._send_json(snapshot)

    def _api_events(self) -> None:
        """Server-Sent Events: one ``data:`` frame carrying the same status
        blob, then the stream closes (the front-end re-opens to poll). A
        single-frame SSE keeps the threaded handler from holding a thread
        open forever while still speaking the EventSource protocol; the
        front-end's reconnect cadence is the poll interval."""
        payload = json.dumps(collect_status(self.cfg))
        body = f"event: status\ndata: {payload}\n\n".encode("utf-8")
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", "text/event-stream; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Connection", "close")
        self.end_headers()
        self.wfile.write(body)

    # --- dashboard tabs (read-only; pure reads, never a side effect) -------

    def _api_conductor_status(self) -> None:
        """The Status tab: is the global conductor alive, its concurrency +
        per-job table + recent landings. Pure read (jobs.json + lock + log)."""
        from marathon.deck.dashboard import conductor_status
        payload = conductor_status(self.cfg)
        payload["restart_allowed"] = bool(
            getattr(self.server, "allow_host_conductor_control", False)
            and not self._demo
        )
        self._send_json(payload)

    def _api_run_detail(self, raw_id: str) -> None:
        """Drill-down for one conductor run (clicking a Status-tab job): its live
        progress + iteration, auto-rater ratings, and refine-reviewer prompts.
        Pure read from the job's workdir; degrades to {found:false}."""
        try:
            issue_num = int(raw_id)
        except ValueError:
            return self._send_error_json(
                HTTPStatus.BAD_REQUEST, f"bad run id {raw_id!r}"
            )
        from marathon.deck.dashboard import run_detail
        self._send_json(run_detail(self.cfg, issue_num))

    def _api_issues_overview(self) -> None:
        """The Issues tab: every registered issue grouped by chapter, each with
        a combined status + tier. One ``gh`` bulk read (degrades local-only)."""
        from marathon.deck.dashboard import issues_overview
        self._send_json(issues_overview(self.cfg))

    def _api_review_workspace(self, query: dict) -> None:
        """Source-ordered declarations for one chapter (or ``chapter=all``).

        PURE read: the project-wide adapter bulk-loads issue metadata once and
        overlays only the local subdeck sidecar.  It never records a verdict.
        """
        if self._demo:
            return self._send_json({
                "repo": None, "chapter": None, "chapters": [], "files": [],
                "issues": [], "progress": {"reviewed": 0, "total": 0,
                "verified": 0, "rejected": 0, "deferred": 0},
                "audit": {"status": "missing", "created_at": None,
                "source_revision": None, "failures": []}, "errors": [],
            })
        raw = (query.get("chapter") or [None])[0]
        chapter = None if raw == "all" else self._chapter_from_query(query)
        from marathon.deck.workspace import build_review_workspace

        self._send_json(build_review_workspace(self.cfg, chapter))

    def _api_review_source(self, query: dict) -> None:
        """Lazy whole-file source for the Files reader — PURE, repo-local."""
        raw = (query.get("path") or [None])[0]
        if not isinstance(raw, str) or not raw:
            return self._send_error_json(HTTPStatus.BAD_REQUEST, "missing source path")
        if self._demo:
            return self._send_error_json(
                HTTPStatus.NOT_FOUND, "whole-file source is unavailable in demo mode"
            )
        try:
            path = _repo_lean_source(Path(self.cfg.repo_dir), raw)
            with path.open("rb") as handle:
                payload = handle.read(MAX_REVIEW_SOURCE_BYTES + 1)
        except ValueError as exc:
            return self._send_error_json(HTTPStatus.BAD_REQUEST, str(exc))
        except OSError as exc:
            return self._send_error_json(
                HTTPStatus.INTERNAL_SERVER_ERROR, f"could not read source: {exc}"
            )
        truncated = len(payload) > MAX_REVIEW_SOURCE_BYTES
        if truncated:
            payload = payload[:MAX_REVIEW_SOURCE_BYTES]
        relative = str(path.relative_to(Path(self.cfg.repo_dir).resolve()))
        self._send_json({
            "path": relative,
            "source": payload.decode("utf-8", errors="replace"),
            "truncated": truncated,
            "max_bytes": MAX_REVIEW_SOURCE_BYTES,
        })

    def _api_project_graph(self, query: Optional[dict] = None) -> None:
        """Project-wide issue/declaration dependency graph — PURE read."""
        if self._demo:
            return self._send_json({
                "repo": None, "chapters": [], "nodes": [], "edges": [],
                "unresolved_edges": [], "cyclic_edges": [],
                "audit": {"status": "missing", "created_at": None,
                "source_revision": None, "failures": []},
                "topology": {
                    "status": "unavailable", "available": False,
                    "source_revision": None, "current_revision": None,
                    "authoritative": False,
                    "notice": "Demo mode has no audited dependency topology.",
                    "targets": 0, "resolved_targets": 0,
                    "declaration_edges": 0, "issue_edges": 0,
                },
                "counts": {"nodes": 0, "edges": 0, "by_status": {},
                "by_type": {}, "by_build_status": {}, "changed": 0,
                "impacted": 0, "unresolved_edges": 0,
                "cyclic_edges": 0}, "errors": [],
            })
        from marathon.deck.workspace import build_project_graph

        if _truthy_flag(((query or {}).get("refresh") or [None])[0]):
            # The browser's explicit Refresh action is an observation point
            # for external GitHub/source/audit changes.  Evict only this
            # process-local read cache; the GET remains externally pure.
            self.server.invalidate_project_graph_cache()  # type: ignore[attr-defined]
        payload = self.server.project_graph_payload(  # type: ignore[attr-defined]
            lambda: build_project_graph(self.cfg)
        )
        self._send_json(payload)

    def _api_definition(self, query: dict) -> None:
        """Current-source definition/signature lookup — PURE read."""
        name = (query.get("name") or [None])[0]
        if not isinstance(name, str) or not name.strip():
            return self._send_error_json(
                HTTPStatus.BAD_REQUEST, "missing definition name"
            )
        issue_raw = (query.get("issue") or [None])[0]
        issue_num = None
        if issue_raw not in (None, ""):
            try:
                issue_num = int(issue_raw)
            except (TypeError, ValueError):
                return self._send_error_json(
                    HTTPStatus.BAD_REQUEST, f"bad issue {issue_raw!r}"
                )
        if self._demo:
            return self._send_json({
                "query": name, "status": "unresolved", "matches": [],
                "truncated": False,
                "message": "definition lookup is unavailable in demo mode",
            })
        from marathon.deck.definitions import lookup_definition

        self._send_json(
            lookup_definition(self.cfg, name, issue_num=issue_num)
        )

    def _api_definition_terms(self) -> None:
        """Compact current project identifier index — PURE read."""
        if self._demo:
            return self._send_json({
                "names": [], "short": {}, "count": 0, "truncated": False,
            })
        from marathon.deck.definitions import definition_terms

        self._send_json(definition_terms(self.cfg))

    def _api_reconciliation(self, raw_id: str) -> None:
        """Pending rename/split proposals reconstructed from the live cursor."""
        try:
            issue_num = int(raw_id)
        except (TypeError, ValueError):
            return self._send_error_json(
                HTTPStatus.BAD_REQUEST, f"bad issue id {raw_id!r}"
            )
        if self._demo:
            return self._send_json({
                "ok": True, "updated": False,
                "proposed_renames": [], "proposed_splits": [],
                "proposal_token": None,
                "message": "no live reconciliation in demo mode",
            })
        from marathon.deck.refresh import reconciliation_preview

        self._send_json(reconciliation_preview(self.cfg, issue_num))

    def _api_history(self, query: dict) -> None:
        """The History tab: recent issue-status changes (verdicts log) + merged
        PRs (one ``gh`` read). ``?limit=N`` caps each feed (default 30)."""
        from marathon.deck.dashboard import history
        try:
            limit = int((query.get("limit") or ["30"])[0])
        except (ValueError, TypeError):
            limit = 30
        self._send_json(history(self.cfg, limit=max(1, min(limit, 200))))

    # --- POST (the single irreversible route — token-guarded) -------------

    # The POST routes. Every one is token-guarded (see ``_read_guarded_body``);
    # ``/api/verdict``, ``/api/ship`` and ``/api/refresh-issue`` are
    # side-effecting (the last is the SOLE GitHub-write of the reconcile/refresh
    # feature), ``/api/decl-verdict`` is LOCAL-only. A path not here is a 404
    # with no side effect.
    _POST_ROUTES = (
        "/api/verdict", "/api/decl-verdict", "/api/batch-decl-verdict",
        "/api/reconcile-proposal",
        "/api/decl-lock", "/api/missing", "/api/ship",
        "/api/refresh-issue", "/api/drop-citation", "/api/requeue",
        "/api/recovery",
        "/api/add-chapter", "/api/chapter-intake-status", "/api/approve-chapter",
        "/api/prover-toggle", "/api/verso-retry", "/api/conductor-start",
    )

    def do_POST(self) -> None:  # noqa: N802 (stdlib handler name)
        self._queue_invalidation_pending = False
        self._queue_cache_invalidated = False
        self._project_graph_invalidation_pending = False
        self._project_graph_cache_invalidated = False
        parsed = urlsplit(self.path)
        if parsed.path not in self._POST_ROUTES:
            return self._send_error_json(
                HTTPStatus.NOT_FOUND, f"no POST route {parsed.path!r}"
            )
        self._queue_invalidation_pending = (
            parsed.path in _QUEUE_MUTATING_POST_ROUTES
        )
        self._project_graph_invalidation_pending = (
            parsed.path in _PROJECT_GRAPH_MUTATING_POST_ROUTES
        )
        # Bind the collaborator's forwarded GitHub OAuth token (if a proxy
        # supplied one) for the whole dispatch, so every GitHub write the handler
        # makes is attributed to them. No header (local use) => ambient gh auth.
        from marathon.review.github import actor_github_token

        actor_token = self.headers.get(ACCESS_TOKEN_HEADER)
        try:
            with actor_github_token(actor_token):
                if parsed.path == "/api/verdict":
                    self._api_verdict()
                elif parsed.path == "/api/decl-verdict":
                    self._api_decl_verdict()
                elif parsed.path == "/api/batch-decl-verdict":
                    self._api_batch_decl_verdict()
                elif parsed.path == "/api/reconcile-proposal":
                    self._api_reconcile_proposal()
                elif parsed.path == "/api/decl-lock":
                    self._api_decl_lock()
                elif parsed.path == "/api/missing":
                    self._api_missing()
                elif parsed.path == "/api/refresh-issue":
                    self._api_refresh_issue()
                elif parsed.path == "/api/drop-citation":
                    self._api_drop_citation()
                elif parsed.path == "/api/requeue":
                    self._api_requeue()
                elif parsed.path == "/api/recovery":
                    self._api_recovery()
                elif parsed.path == "/api/add-chapter":
                    self._api_add_chapter()
                elif parsed.path == "/api/chapter-intake-status":
                    self._api_chapter_intake_status()
                elif parsed.path == "/api/approve-chapter":
                    self._api_approve_chapter()
                elif parsed.path == "/api/prover-toggle":
                    self._api_prover_toggle()
                elif parsed.path == "/api/conductor-start":
                    self._api_conductor_start()
                elif parsed.path == "/api/verso-retry":
                    self._api_verso_retry()
                else:  # /api/ship
                    self._api_ship()
        except BrokenPipeError:
            return

    def _read_guarded_body(self) -> Optional[dict]:
        """Shared POST preamble for every side-effecting route: the
        cross-origin guard, the (small) JSON body read, and the per-session
        token check — in that order, BEFORE any handler logic runs.

        Returns the parsed JSON body (a dict) on success, or ``None`` after
        having already sent the appropriate 4xx (cross-origin / bad body /
        missing-or-invalid token). The token comes from the header (preferred)
        or the body's ``token`` field; a constant-time compare avoids leaking
        it via timing. NO token / wrong token / foreign Origin => the handler
        returns without firing any side effect."""
        # (1) Cross-origin guard: a POST may only come from a page this server
        #     served (same-origin). A present Origin that is not our own
        #     loopback origin is a stray/other-origin POST — refuse it BEFORE
        #     reading the body or touching the token.
        origin = self.headers.get("Origin")
        if origin is not None and not self._origin_is_self(origin):
            self._send_error_json(
                HTTPStatus.FORBIDDEN,
                "cross-origin request refused (this surface is loopback-only)",
            )
            return None

        # (2) Read the (small) body.
        try:
            length = int(self.headers.get("Content-Length", "0"))
        except ValueError:
            self._send_error_json(HTTPStatus.BAD_REQUEST, "bad Content-Length")
            return None
        if length > MAX_POST_BYTES:
            self._send_error_json(
                HTTPStatus.REQUEST_ENTITY_TOO_LARGE, "request body too large"
            )
            return None
        raw = self.rfile.read(length) if length > 0 else b""
        try:
            data = json.loads(raw.decode("utf-8")) if raw else {}
        except (ValueError, UnicodeDecodeError):
            self._send_error_json(
                HTTPStatus.BAD_REQUEST, "request body is not valid JSON"
            )
            return None
        if not isinstance(data, dict):
            self._send_error_json(
                HTTPStatus.BAD_REQUEST, "request body must be a JSON object"
            )
            return None

        # (3) Per-session token check — the gate on the side effect.
        supplied = self.headers.get(TOKEN_HEADER) or data.get("token")
        expected = self.server.token  # type: ignore[attr-defined]
        if not supplied or not secrets.compare_digest(str(supplied), expected):
            self._send_error_json(
                HTTPStatus.FORBIDDEN,
                "missing or invalid session token; request not applied",
            )
            return None
        return data

    def _api_verdict(self) -> None:
        data = self._read_guarded_body()
        if data is None:
            return  # the guard already sent the 4xx (origin / body / token)

        # (4) Validate the request shape, THEN route through the committed
        #     verdict path. apply_verdict raises VerdictError on a bad
        #     verdict / empty reject note BEFORE any side effect.
        issue_raw = data.get("id")
        verdict = data.get("verdict")
        note = data.get("note")
        try:
            issue_num = int(issue_raw)
        except (TypeError, ValueError):
            return self._send_error_json(
                HTTPStatus.BAD_REQUEST, f"bad or missing card id {issue_raw!r}"
            )

        from marathon.deck.verdicts import VERDICTS, VerdictError, apply_verdict

        # The async fast path: validate the request shape NOW (clean 4xx),
        # accept durably, run the GitHub writes off the request thread, and
        # let the browser poll /api/verdict-status. The synchronous form
        # remains for compatibility.
        if data.get("async"):
            if verdict not in VERDICTS:
                return self._send_error_json(
                    HTTPStatus.BAD_REQUEST,
                    f"unknown verdict {verdict!r}; expected one of {VERDICTS}",
                )
            if verdict == "reject" and (note is None or not str(note).strip()):
                return self._send_error_json(
                    HTTPStatus.BAD_REQUEST,
                    "a reject requires a non-empty note",
                )
            # Acceptance changes nothing readable yet; the worker invalidates
            # the read models when the verdict actually lands (on_complete).
            self._queue_cache_invalidated = True
            self._project_graph_cache_invalidated = True
            record = self.server.verdict_jobs.start(  # type: ignore[attr-defined]
                self.cfg,
                issue_num,
                verdict,
                str(note) if note is not None else None,
                actor_token=self.headers.get(ACCESS_TOKEN_HEADER),
                on_complete=self.server.invalidate_queue_cache,  # type: ignore[attr-defined]
            )
            return self._send_json(
                {"ok": True, "accepted": True, "job": record},
                status=HTTPStatus.ACCEPTED,
            )

        try:
            result = apply_verdict(self.cfg, issue_num, verdict, note)
        except VerdictError as exc:
            return self._send_error_json(HTTPStatus.BAD_REQUEST, str(exc))
        except Exception as exc:  # noqa: BLE001 — surface, never crash the loop
            return self._send_error_json(
                HTTPStatus.INTERNAL_SERVER_ERROR,
                f"verdict routing failed: {exc}",
            )
        self._send_json(result.to_json())

    # --- "Add Chapter" intake (F1): start a headless intake, poll it, approve --

    def _api_add_chapter(self) -> None:
        """Start a headless chapter-intake on a daemon thread. Returns the job
        snapshot immediately; the UI polls /api/chapter-intake-status until the
        job is ``ready`` (drafted sub-issues) or ``error``."""
        data = self._read_guarded_body()
        if data is None:
            return
        if self.server.demo:
            return self._send_error_json(
                HTTPStatus.BAD_REQUEST, "add-chapter is not available in demo mode"
            )
        try:
            chapter = int(data.get("chapter"))
        except (TypeError, ValueError):
            return self._send_error_json(
                HTTPStatus.BAD_REQUEST,
                f"bad or missing chapter {data.get('chapter')!r}",
            )
        # An explicit Approve click may already have opened part of the remote
        # batch before this deck process restarted. Restore that exact reviewed
        # snapshot; never rerun Claude and risk a different manifest being
        # spliced into the durable transaction.
        from marathon.review.subissues import chapter_approval_snapshot

        try:
            interrupted = chapter_approval_snapshot(self.cfg, chapter)
        except Exception as exc:  # noqa: BLE001 — malformed journal is explicit
            return self._send_error_json(
                HTTPStatus.CONFLICT,
                f"cannot resume chapter {chapter} approval: {exc}",
            )
        if interrupted is not None:
            return self._send_json(interrupted)
        # Legacy chapters may predate approval journals. They still get the
        # same fail-fast guard: do not spend a model call drafting a chapter
        # that the authoritative registry already contains.
        from marathon.review.config import load_config

        try:
            already_registered = chapter in load_config(
                repo_dir=self.cfg.repo_dir
            ).chapters
        except SystemExit:
            already_registered = False
        if already_registered:
            return self._send_error_json(
                HTTPStatus.CONFLICT,
                f"chapter {chapter} is already registered — refusing to start "
                "a duplicate intake",
            )
        source = str(data.get("informal_source") or "").strip().lower()
        raw = str(data.get("informal_statements") or "").strip()
        # Back-compat: a legacy client that sends only a path (no informal_source)
        # means "file"; an otherwise-empty request defaults to the tracker roadmap.
        if not source:
            source = "file" if raw else "tracker"
        informal_path = None
        use_tracker = False
        if source == "tracker":
            use_tracker = True
        elif source == "none":
            pass
        elif source == "file":
            if not raw:
                return self._send_error_json(
                    HTTPStatus.BAD_REQUEST,
                    "informal source 'file' selected but no path was given",
                )
            try:
                informal_path = _repo_informal_source(self.cfg.repo_dir, raw)
            except ValueError as exc:
                return self._send_error_json(HTTPStatus.BAD_REQUEST, str(exc))
        else:
            return self._send_error_json(
                HTTPStatus.BAD_REQUEST,
                f"unknown informal_source {source!r} (want tracker|file|none)",
            )
        snap = self.server.intake.start(
            self.cfg, chapter, informal_path=informal_path, use_tracker=use_tracker
        )
        self._send_json(snap)

    def _api_chapter_intake_status(self) -> None:
        """Poll one chapter's intake job (LOCAL read — no side effect)."""
        data = self._read_guarded_body()
        if data is None:
            return
        try:
            chapter = int(data.get("chapter"))
        except (TypeError, ValueError):
            return self._send_error_json(
                HTTPStatus.BAD_REQUEST,
                f"bad or missing chapter {data.get('chapter')!r}",
            )
        from marathon.review.subissues import chapter_approval_snapshot

        try:
            interrupted = chapter_approval_snapshot(self.cfg, chapter)
        except Exception as exc:  # noqa: BLE001 — surface durable-state damage
            return self._send_error_json(
                HTTPStatus.CONFLICT,
                f"cannot resume chapter {chapter} approval: {exc}",
            )
        if interrupted is not None:
            return self._send_json(interrupted)
        snap = self.server.intake.status(chapter) or {
            "chapter": chapter, "status": "none",
        }
        self._send_json(snap)

    def _api_approve_chapter(self) -> None:
        """Apply a READY intake: open the GitHub sub-issues + register the
        chapter in ``config.toml``. Approval is a durable, per-chapter
        single-flight transaction: partial GitHub success is journalled and a
        retry resumes the same issue IDs rather than opening duplicates."""
        data = self._read_guarded_body()
        if data is None:
            return
        if self.server.demo:
            return self._send_error_json(
                HTTPStatus.BAD_REQUEST, "approve-chapter is not available in demo mode"
            )
        try:
            chapter = int(data.get("chapter"))
        except (TypeError, ValueError):
            return self._send_error_json(
                HTTPStatus.BAD_REQUEST,
                f"bad or missing chapter {data.get('chapter')!r}",
            )
        res = self.server.intake.result(chapter)

        from marathon.review.subissues import (
            ChapterApprovalConflict,
            ChapterApprovalError,
            approve_chapter_drafts,
        )

        # Approve the in-memory snapshot the human actually reviewed, not a
        # drafts file that could have changed between preview and this POST. If
        # the process restarted, ``None`` resumes the exact snapshot already
        # persisted by the original Approve click; it never reruns intake.
        reviewed = (
            {s.index: (s.title, s.body) for s in res.subissues}
            if res is not None and res.ok
            else None
        )
        target = self.cfg.target_path(chapter)
        try:
            result = approve_chapter_drafts(
                self.cfg, chapter, reviewed, target=target
            )
        except ChapterApprovalConflict as exc:
            return self._send_json(
                {
                    "ok": False,
                    "chapter": chapter,
                    "error": str(exc),
                    "retryable": False,
                    "created": [
                        {"index": i, "issue": n, "url": u}
                        for i, n, u in exc.created
                    ],
                },
                status=HTTPStatus.CONFLICT,
            )
        except ChapterApprovalError as exc:
            # A retryable partial failure is not thrown away: the response and
            # durable journal both expose IDs that already exist on GitHub.
            return self._send_json(
                {
                    "ok": False,
                    "chapter": chapter,
                    "error": str(exc),
                    "incomplete": True,
                    "retryable": exc.retryable,
                    "created": [
                        {"index": i, "issue": n, "url": u}
                        for i, n, u in exc.created
                    ],
                },
                status=HTTPStatus.BAD_GATEWAY,
            )
        except Exception as exc:  # noqa: BLE001 — surface, never kill handler
            return self._send_error_json(
                HTTPStatus.INTERNAL_SERVER_ERROR,
                f"chapter approval failed: {exc}",
            )

        self._send_json({
            "ok": True,
            "chapter": chapter,
            "created": [
                {"index": i, "issue": n, "url": u}
                for i, n, u in result.created
            ],
            "resumed": result.resumed,
            "registry_error": None,
        })

    def _api_decl_verdict(self) -> None:
        """Record one per-(issue,decl) verdict — LOCAL ONLY (the contract's
        POST /api/decl-verdict). Token-guarded like every POST, but it fires
        NO irreversible action: it writes only the local subdeck sidecar (no
        cmd_verify / cmd_reject, no gh, no git, no Aristotle). A ``reject``
        without a non-empty note is a 400 (raised before any write)."""
        data = self._read_guarded_body()
        if data is None:
            return  # the guard already sent the 4xx (origin / body / token)

        issue_raw = data.get("issue")
        decl = data.get("decl")
        verdict = data.get("verdict")
        note = data.get("note")
        decl_fingerprint = data.get("decl_fingerprint")
        try:
            issue_num = int(issue_raw)
        except (TypeError, ValueError):
            return self._send_error_json(
                HTTPStatus.BAD_REQUEST, f"bad or missing issue {issue_raw!r}"
            )
        if not decl or not isinstance(decl, str):
            return self._send_error_json(
                HTTPStatus.BAD_REQUEST, f"bad or missing decl {decl!r}"
            )
        if decl_fingerprint is not None and (
            not isinstance(decl_fingerprint, str)
            or re.fullmatch(r"[0-9a-f]{64}", decl_fingerprint) is None
        ):
            return self._send_error_json(
                HTTPStatus.BAD_REQUEST, "bad declaration context fingerprint"
            )

        if self._demo:
            from marathon.deck.demo import demo_record_verdict
            try:
                return self._send_json(
                    demo_record_verdict(issue_num, decl, verdict, note)
                )
            except ValueError as exc:
                return self._send_error_json(HTTPStatus.BAD_REQUEST, str(exc))
        if decl_fingerprint is None:
            return self._send_error_json(
                HTTPStatus.CONFLICT,
                "This review request is missing the exact context that was "
                "shown. Reload the review and decide again.",
            )

        from marathon.deck.subdeck import (
            ReviewContextChanged,
            next_unreviewed_card,
            record_decl_verdict,
        )

        try:
            _stored, subdeck = record_decl_verdict(
                self.cfg,
                issue_num,
                decl,
                verdict,
                note,
                expected_fingerprint=decl_fingerprint,
                # Real declarations bind source + kernel; the informal slot
                # binds the exact normalized prose shown.
                require_current=True,
                return_subdeck=True,
            )
        except ReviewContextChanged as exc:
            return self._send_error_json(HTTPStatus.CONFLICT, str(exc))
        except ValueError as exc:  # unknown verdict / empty reject note
            return self._send_error_json(HTTPStatus.BAD_REQUEST, str(exc))
        except Exception as exc:  # noqa: BLE001 — surface, never crash the loop
            return self._send_error_json(
                HTTPStatus.INTERNAL_SERVER_ERROR,
                f"decl-verdict failed: {exc}",
            )
        self._send_json({
            "ok": True,
            "progress": subdeck["progress"],
            "complete": subdeck["complete"],
            "summary": subdeck["summary"],
            # The informal-approval slot rides the response so a __informal__
            # verdict (the leading approval card) is reflected authoritatively;
            # it is OUTSIDE progress/complete/summary (see _informal_slot).
            "informal": subdeck.get("informal"),
            "next_decl": next_unreviewed_card(subdeck),
            "review_token": subdeck.get("review_token"),
        })

    def _api_batch_decl_verdict(self) -> None:
        """Atomically record several LOCAL declaration verdicts.

        This is the file/chapter reader's batch seam.  The complete request is
        validated against fresh subdecks before one local sidecar transaction.
        This endpoint deliberately accepts only DEFER.  KEEP/FIX remain
        single-declaration actions so a filtered chapter cannot accidentally
        turn into bulk human approval.  No committed issue verdict, GitHub
        write, or Aristotle run occurs here.
        """
        data = self._read_guarded_body()
        if data is None:
            return
        items = data.get("items")
        if not isinstance(items, list) or not items:
            return self._send_error_json(
                HTTPStatus.BAD_REQUEST,
                "batch must contain a non-empty items list",
            )
        if len(items) > 500:
            return self._send_error_json(
                HTTPStatus.REQUEST_ENTITY_TOO_LARGE,
                "batch may contain at most 500 declaration verdicts",
            )
        if self._demo:
            return self._send_error_json(
                HTTPStatus.BAD_REQUEST,
                "batch declaration review is not available in demo mode",
            )

        from marathon.deck.subdeck import (
            DECL_VERDICTS,
            ReviewContextChanged,
            build_subdeck,
            record_decl_verdict_batch,
        )

        normalized: list[dict] = []
        seen: set[tuple[int, str]] = set()
        for raw in items:
            if not isinstance(raw, dict):
                return self._send_error_json(
                    HTTPStatus.BAD_REQUEST, "each batch item must be an object"
                )
            try:
                issue_num = int(raw.get("issue"))
            except (TypeError, ValueError):
                return self._send_error_json(
                    HTTPStatus.BAD_REQUEST,
                    f"bad or missing issue {raw.get('issue')!r}",
                )
            decl = raw.get("decl")
            verdict = raw.get("verdict")
            note = raw.get("note")
            expected_fingerprint = raw.get("decl_fingerprint")
            if not isinstance(decl, str) or not decl.strip():
                return self._send_error_json(
                    HTTPStatus.BAD_REQUEST, f"bad or missing decl {decl!r}"
                )
            decl = decl.strip()
            if verdict not in DECL_VERDICTS:
                return self._send_error_json(
                    HTTPStatus.BAD_REQUEST,
                    f"unknown decl verdict {verdict!r}; expected one of {DECL_VERDICTS}",
                )
            if verdict != "defer":
                return self._send_error_json(
                    HTTPStatus.BAD_REQUEST,
                    "batch review accepts only 'defer'; KEEP/FIX require an explicit per-declaration decision",
                )
            if verdict == "reject" and (note is None or not str(note).strip()):
                return self._send_error_json(
                    HTTPStatus.BAD_REQUEST,
                    f"a reject for {decl} requires a non-empty note",
                )
            if expected_fingerprint is not None and (
                not isinstance(expected_fingerprint, str)
                or re.fullmatch(r"[0-9a-f]{64}", expected_fingerprint) is None
            ):
                return self._send_error_json(
                    HTTPStatus.BAD_REQUEST,
                    f"bad declaration context fingerprint for {decl}",
                )
            if expected_fingerprint is None:
                return self._send_error_json(
                    HTTPStatus.CONFLICT,
                    f"#{issue_num}: declaration context missing for {decl}; "
                    "reload the review",
                )
            key = (issue_num, decl)
            if key in seen:
                return self._send_error_json(
                    HTTPStatus.BAD_REQUEST,
                    f"duplicate declaration target in batch: issue #{issue_num} {decl}",
                )
            seen.add(key)
            normalized.append({
                "issue": issue_num, "decl": decl,
                "verdict": verdict, "note": note,
                "expected_fingerprint": expected_fingerprint,
            })

        grouped: dict[int, list[dict]] = {}
        for item in normalized:
            grouped.setdefault(item["issue"], []).append(item)

        blocked: list[dict] = []
        for issue_num, proposed in grouped.items():
            try:
                subdeck = build_subdeck(self.cfg, issue_num)
            except Exception as exc:  # noqa: BLE001 - report, never partially write
                return self._send_error_json(
                    HTTPStatus.INTERNAL_SERVER_ERROR,
                    f"could not validate issue #{issue_num}: {exc}",
                )
            cards = subdeck.get("cards") or []
            by_name = {card.get("name"): card for card in cards if card.get("name")}
            for item in proposed:
                if item["decl"] not in by_name:
                    return self._send_error_json(
                        HTTPStatus.CONFLICT,
                        f"#{issue_num}: declaration {item['decl']!r} is not in the current code-backed subdeck",
                    )
                current_fingerprint = by_name[item["decl"]].get(
                    "context_fingerprint"
                )
                if (
                    item.get("expected_fingerprint") is not None
                    and item["expected_fingerprint"] != current_fingerprint
                ):
                    return self._send_error_json(
                        HTTPStatus.CONFLICT,
                        f"#{issue_num}: {item['decl']} changed after it was shown; reload the review",
                    )
                # Carry the server-observed fingerprint into the locked batch
                # transaction, which recomputes and compares it once more.
                item["expected_fingerprint"] = current_fingerprint

            projected = {name: card.get("verdict") for name, card in by_name.items()}
            wire = {"verify": "verified", "reject": "rejected", "defer": "deferred"}
            for item in proposed:
                projected[item["decl"]] = wire[item["verdict"]]

            names = list(by_name)
            for item in proposed:
                if item["verdict"] == "defer":
                    continue
                card = by_name[item["decl"]]
                rejected_deps: set[str] = set()
                for member in card.get("kernel") or []:
                    raw_name = member.get("name")
                    if not raw_name:
                        continue
                    matches = [
                        name for name in names
                        if name == raw_name
                        or name.endswith("." + raw_name)
                        or raw_name.endswith("." + name)
                    ]
                    if len(matches) == 1 and projected.get(matches[0]) == "rejected":
                        rejected_deps.add(matches[0])
                if rejected_deps:
                    blocked.append({
                        "issue": issue_num,
                        "decl": item["decl"],
                        "blocked_by": sorted(rejected_deps),
                    })
        # The chapter reader spans issue boundaries.  Apply its project-wide
        # guard too: a definition rejected in issue A pauses a theorem in issue
        # B even though build_subdeck(B) cannot see A's local sidecar cards.
        if any(item["verdict"] != "defer" for item in normalized):
            try:
                from marathon.deck.workspace import build_review_workspace

                wide = build_review_workspace(self.cfg, None)
                wide_targets = {
                    (target.get("issue"), target.get("name")): target
                    for file in wide.get("files") or []
                    for target in file.get("declarations") or []
                }
                for item in normalized:
                    if item["verdict"] == "defer":
                        continue
                    cross = (wide_targets.get((item["issue"], item["decl"])) or {}).get(
                        "blocked_by"
                    ) or []
                    if cross:
                        blocked.append({
                            "issue": item["issue"],
                            "decl": item["decl"],
                            "blocked_by": sorted(set(cross)),
                        })
            except Exception as exc:  # noqa: BLE001 - validation must fail closed
                return self._send_error_json(
                    HTTPStatus.INTERNAL_SERVER_ERROR,
                    f"could not validate project dependencies: {exc}",
                )
        if blocked:
            unique_blocked = {}
            for item in blocked:
                key = (item["issue"], item["decl"])
                unique_blocked.setdefault(key, set()).update(item["blocked_by"])
            return self._send_json(
                {
                    "ok": False,
                    "error": "later declarations are waiting on rejected predecessors",
                    "blocked": [
                        {"issue": key[0], "decl": key[1],
                         "blocked_by": sorted(deps)}
                        for key, deps in sorted(unique_blocked.items())
                    ],
                },
                status=HTTPStatus.CONFLICT,
            )

        try:
            stored = record_decl_verdict_batch(
                self.cfg, normalized, require_current=True
            )
        except ReviewContextChanged as exc:
            return self._send_error_json(HTTPStatus.CONFLICT, str(exc))
        except ValueError as exc:
            return self._send_error_json(HTTPStatus.BAD_REQUEST, str(exc))
        except Exception as exc:  # noqa: BLE001 - surface, never crash server
            return self._send_error_json(
                HTTPStatus.INTERNAL_SERVER_ERROR,
                f"batch decl-verdict failed: {exc}",
            )
        self._send_json({"ok": True, "count": len(stored), "items": stored})

    def _api_reconcile_proposal(self) -> None:
        """Confirm or dismiss one exact, token-bound rename/split proposal."""
        data = self._read_guarded_body()
        if data is None:
            return
        try:
            issue_num = int(data.get("issue"))
        except (TypeError, ValueError):
            return self._send_error_json(
                HTTPStatus.BAD_REQUEST,
                f"bad or missing issue {data.get('issue')!r}",
            )
        token = data.get("proposal_token")
        action = data.get("action")
        kind = data.get("kind")
        old = data.get("old")
        if not isinstance(token, str) or len(token) != 64:
            return self._send_error_json(
                HTTPStatus.BAD_REQUEST, "missing or invalid proposal token"
            )
        if action not in ("confirm", "dismiss"):
            return self._send_error_json(
                HTTPStatus.BAD_REQUEST, "action must be 'confirm' or 'dismiss'"
            )
        if kind not in ("rename", "split"):
            return self._send_error_json(
                HTTPStatus.BAD_REQUEST, "kind must be 'rename' or 'split'"
            )
        if not isinstance(old, str) or not old.strip():
            return self._send_error_json(
                HTTPStatus.BAD_REQUEST, "proposal is missing its old declaration"
            )

        confirmation = {
            "token": token,
            "renames": [], "splits": [],
            "dismiss_renames": [], "dismiss_splits": [],
        }
        if kind == "rename":
            new = data.get("new")
            if not isinstance(new, str) or not new.strip():
                return self._send_error_json(
                    HTTPStatus.BAD_REQUEST,
                    "rename proposal is missing its new declaration",
                )
            mapping = {"old": old.strip(), "new": new.strip()}
            confirmation[
                "renames" if action == "confirm" else "dismiss_renames"
            ].append(mapping)
        else:
            targets = data.get("targets")
            if (
                not isinstance(targets, list)
                or not targets
                or not all(isinstance(item, str) and item.strip() for item in targets)
                or len(targets) > 50
            ):
                return self._send_error_json(
                    HTTPStatus.BAD_REQUEST,
                    "split proposal requires 1-50 declaration targets",
                )
            mapping = {
                "old": old.strip(),
                "targets": [item.strip() for item in targets],
            }
            confirmation[
                "splits" if action == "confirm" else "dismiss_splits"
            ].append(mapping)

        if self._demo:
            return self._send_error_json(
                HTTPStatus.BAD_REQUEST,
                "reconciliation decisions are unavailable in demo mode",
            )
        from marathon.deck.refresh import confirm_reconciliation_proposal

        result = confirm_reconciliation_proposal(
            self.cfg, issue_num, confirmation
        )
        if result.get("ok", True):
            # The conductor's pending-human snapshot refreshes only when its
            # reconcile pass runs (a HEAD move); prune this issue now so the
            # digest stops asking for a decision the operator just made.
            self._prune_pending_human(issue_num)
        status = (
            HTTPStatus.CONFLICT
            if not result.get("ok", True)
            else HTTPStatus.OK
        )
        self._send_json(result, status=status)

    def _prune_pending_human(self, issue_num: int) -> None:
        """Drop one issue from the conductor's advisory pending-human file."""
        try:
            from marathon.conductor import PENDING_HUMAN_RELPATH

            path = Path(self.cfg.repo_dir) / PENDING_HUMAN_RELPATH
            data = json.loads(path.read_text())
            kept = [
                item for item in (data.get("reconcile") or [])
                if item.get("issue") != issue_num
            ]
            if len(kept) == len(data.get("reconcile") or []):
                return
            data["reconcile"] = kept
            tmp = path.with_suffix(".tmp")
            tmp.write_text(json.dumps(data, indent=2, sort_keys=True) + "\n")
            tmp.replace(path)
        except (OSError, ValueError):
            pass

    def _api_decl_lock(self) -> None:
        """Toggle the opt-in HARD-FREEZE lock on one declaration — LOCAL ONLY
        (POST /api/decl-lock). Token-guarded; writes only the subdeck sidecar.
        A locked decl's signature is frozen at reject-ship so no sibling-decl
        refine can change it; unlocked verified decls stay rewritable. Body:
        ``{issue, decl, locked: bool}``."""
        data = self._read_guarded_body()
        if data is None:
            return  # the guard already sent the 4xx (origin / body / token)

        issue_raw = data.get("issue")
        decl = data.get("decl")
        locked = bool(data.get("locked"))
        decl_fingerprint = data.get("decl_fingerprint")
        try:
            issue_num = int(issue_raw)
        except (TypeError, ValueError):
            return self._send_error_json(
                HTTPStatus.BAD_REQUEST, f"bad or missing issue {issue_raw!r}"
            )
        if not decl or not isinstance(decl, str):
            return self._send_error_json(
                HTTPStatus.BAD_REQUEST, f"bad or missing decl {decl!r}"
            )
        if decl_fingerprint is not None and (
            not isinstance(decl_fingerprint, str)
            or re.fullmatch(r"[0-9a-f]{64}", decl_fingerprint) is None
        ):
            return self._send_error_json(
                HTTPStatus.BAD_REQUEST, "bad declaration context fingerprint"
            )
        if self._demo:
            return self._send_error_json(
                HTTPStatus.BAD_REQUEST, "decl-lock is not available in demo mode"
            )
        if decl_fingerprint is None:
            return self._send_error_json(
                HTTPStatus.CONFLICT,
                "This lock request is missing the declaration context that "
                "was shown. Reload the review.",
            )

        from marathon.deck.subdeck import (
            ReviewContextChanged,
            set_decl_lock,
        )

        try:
            _stored, subdeck = set_decl_lock(
                self.cfg,
                issue_num,
                decl,
                locked,
                expected_fingerprint=decl_fingerprint,
                require_current=True,
                return_subdeck=True,
            )
        except ReviewContextChanged as exc:
            return self._send_error_json(HTTPStatus.CONFLICT, str(exc))
        except Exception as exc:  # noqa: BLE001 — surface, never crash the loop
            return self._send_error_json(
                HTTPStatus.INTERNAL_SERVER_ERROR, f"decl-lock failed: {exc}"
            )
        self._send_json({
            "ok": True,
            "decl": decl,
            "locked": locked,
            "summary": subdeck["summary"],
            "review_token": subdeck.get("review_token"),
        })

    def _api_missing(self) -> None:
        """Record (or CLEAR) the issue's "something is MISSING" note — LOCAL
        ONLY (POST /api/missing). Token-guarded like every POST, but fires NO
        irreversible action: it writes only the local sidecar. The note routes
        the issue to Aristotle with an ADD section at the explicit ship, never
        here. An empty note clears the flag."""
        data = self._read_guarded_body()
        if data is None:
            return  # the guard already sent the 4xx (origin / body / token)
        issue_raw = data.get("issue")
        note = data.get("note")
        try:
            issue_num = int(issue_raw)
        except (TypeError, ValueError):
            return self._send_error_json(
                HTTPStatus.BAD_REQUEST, f"bad or missing issue {issue_raw!r}"
            )
        if self._demo:
            from marathon.deck.demo import demo_record_missing
            return self._send_json(demo_record_missing(issue_num, note))
        from marathon.deck.subdeck import build_subdeck, record_missing

        try:
            record_missing(self.cfg, issue_num, note)
        except Exception as exc:  # noqa: BLE001 — surface, never crash the loop
            return self._send_error_json(
                HTTPStatus.INTERNAL_SERVER_ERROR, f"missing-note failed: {exc}"
            )
        subdeck = build_subdeck(self.cfg, issue_num)
        self._send_json({
            "ok": True,
            "missing": subdeck.get("missing"),
            "review_token": subdeck.get("review_token"),
        })

    def _api_ship(self) -> None:
        """Ship a subdeck's issue (the contract's POST /api/ship) — the ONLY
        endpoint besides /api/verdict that fires an irreversible action.
        Composes the per-issue outcome from the LOCAL subdeck and routes it
        through the committed verdict path (verify, or reject with the
        composed note carried verbatim). An incomplete all-verify subdeck
        returns ``{ok:false, reason}`` with NO side effect; a partial subdeck
        with an explicit rejection may ship that fix while leaving later
        declarations untouched."""
        data = self._read_guarded_body()
        if data is None:
            return  # the guard already sent the 4xx (origin / body / token)

        issue_raw = data.get("issue")
        try:
            issue_num = int(issue_raw)
        except (TypeError, ValueError):
            return self._send_error_json(
                HTTPStatus.BAD_REQUEST, f"bad or missing issue {issue_raw!r}"
            )
        # skip_api: when truthy the ship auto-approves the issue's easy-API
        # decls (the realization of the toggle). It is read from the BODY of
        # this explicit, token-bearing POST — never inferred from a GET.
        skip_api = _truthy_flag(data.get("skip_api"))
        review_token = data.get("review_token")
        if review_token is not None and (
            not isinstance(review_token, str)
            or re.fullmatch(r"[0-9a-f]{64}", review_token) is None
        ):
            return self._send_error_json(
                HTTPStatus.BAD_REQUEST, "bad subdeck review token"
            )
        request_id = data.get("request_id")
        if request_id is not None and (
            not isinstance(request_id, str)
            or re.fullmatch(r"[0-9a-f]{32}", request_id) is None
        ):
            return self._send_error_json(
                HTTPStatus.BAD_REQUEST, "bad ship request id"
            )

        if self._demo:
            from marathon.deck.demo import demo_ship
            return self._send_json(demo_ship(issue_num, skip_api=skip_api))
        if review_token is None:
            return self._send_error_json(
                HTTPStatus.CONFLICT,
                "This ship request is missing the exact review context. "
                "Reload the review before shipping.",
            )

        from marathon.deck.ship_jobs import ShipJobConflict

        try:
            result = self.server.ship_jobs.start(  # type: ignore[attr-defined]
                self.cfg,
                issue_num,
                skip_api=skip_api,
                review_token=review_token,
                request_id=request_id,
                actor_token=self.headers.get(ACCESS_TOKEN_HEADER),
                on_complete=lambda: (
                    self.server.invalidate_queue_cache(),  # type: ignore[attr-defined]
                    self.server.invalidate_project_graph_cache(),  # type: ignore[attr-defined]
                ),
            )
        except ShipJobConflict as exc:
            return self._send_error_json(HTTPStatus.CONFLICT, str(exc))
        except Exception as exc:  # noqa: BLE001 — surface, never crash the loop
            return self._send_error_json(
                HTTPStatus.INTERNAL_SERVER_ERROR,
                f"could not accept ship job: {exc}",
            )
        self._send_json(result, status=HTTPStatus.ACCEPTED)

    def _api_refresh_issue(self) -> None:
        """Push-refresh the issue body's ``### Lean signatures`` section to the
        CURRENT code/SHA (the contract's POST /api/refresh-issue) — the SOLE new
        GitHub WRITE of the reconcile/refresh feature.

        Token-guarded like every POST (a missing/invalid token => 403 with NO
        ``gh`` call). It routes through the MECHANICAL
        :func:`marathon.deck.refresh.refresh_issue_signatures` (NO Claude): that
        re-quotes ONLY the signatures section against the current source +
        HEAD-pinned permalinks (dropping stale citations, adding surfaced
        decls), preserving every other section verbatim, and writes the new body
        via a SINGLE ``gh issue edit``. The reconciling GET never reaches this —
        only this explicit, token-bearing POST mutates the issue."""
        data = self._read_guarded_body()
        if data is None:
            return  # the guard already sent the 4xx (origin / body / token)

        issue_raw = data.get("issue")
        try:
            issue_num = int(issue_raw)
        except (TypeError, ValueError):
            return self._send_error_json(
                HTTPStatus.BAD_REQUEST, f"bad or missing issue {issue_raw!r}"
            )

        if self._demo:
            from marathon.deck.demo import demo_refresh
            return self._send_json(demo_refresh(issue_num))

        from marathon.deck.refresh import refresh_issue_signatures

        try:
            result = refresh_issue_signatures(self.cfg, issue_num)
        except Exception as exc:  # noqa: BLE001 — surface, never crash the loop
            return self._send_error_json(
                HTTPStatus.INTERNAL_SERVER_ERROR, f"refresh failed: {exc}"
            )
        self._send_json(result)

    def _api_drop_citation(self) -> None:
        """HUMAN-TRIGGERED drop of a confirmed DEAD citation (the contract's
        POST /api/drop-citation) — a GitHub WRITE, token-gated like every POST.

        When an issue cites a decl that was deliberately REMOVED from the code
        (folded/duplicated away — not renamed, not missing work), the
        auto-reconciler conservatively KEEPS the stale citation (it can't tell a
        removal from genuinely-missing work). This is the one-click human escape:
        it routes through the MECHANICAL
        :func:`marathon.deck.refresh.drop_stale_citation` (NO Claude), which
        validates the citation is a confirmed dead-end (cited + gone-from-code +
        no replacement + another real anchor remains) BEFORE removing it from
        both body formats and writing the new body via a SINGLE ``gh issue
        edit``. A citation with a replacement, a live/uncited name, or no other
        anchor is REFUSED (``ok:False``) and NOT written."""
        data = self._read_guarded_body()
        if data is None:
            return  # the guard already sent the 4xx (origin / body / token)

        issue_raw = data.get("issue")
        try:
            issue_num = int(issue_raw)
        except (TypeError, ValueError):
            return self._send_error_json(
                HTTPStatus.BAD_REQUEST, f"bad or missing issue {issue_raw!r}"
            )
        decl = data.get("decl")
        if not isinstance(decl, str) or not decl.strip():
            return self._send_error_json(
                HTTPStatus.BAD_REQUEST, f"bad or missing decl {decl!r}"
            )

        if self._demo:
            return self._send_json(
                {"ok": False, "dropped": False,
                 "message": "drop-citation is not available in demo mode"}
            )

        from marathon.deck.refresh import drop_stale_citation

        try:
            result = drop_stale_citation(self.cfg, issue_num, decl.strip())
        except Exception as exc:  # noqa: BLE001 — surface, never crash the loop
            return self._send_error_json(
                HTTPStatus.INTERNAL_SERVER_ERROR, f"drop-citation failed: {exc}"
            )
        self._send_json(result)

    def _api_stuck(self) -> None:
        """The Stalled list (GET /api/stuck): issues parked OUTSIDE both the swipe
        queue and the refine queue — status 'stalled', or 'rejected' with no
        pending iteration (already iterated, no fix landed). Pure read; each row
        carries its stored rejection note so the deck can offer a one-click
        re-queue. Empty in demo mode."""
        if self._demo:
            return self._send_json({
                "issues": [],
                "repo": None,
                "conductor": {
                    "alive": False,
                    "ready": False,
                    "degraded": None,
                    "restart_allowed": False,
                },
            })
        from marathon.deck.dashboard import conductor_status, stuck_issues

        # Reuse one local-only runtime read for both recovery rows and Review's
        # empty-state health card. Excluding landings avoids a redundant GitHub
        # call on the queue's latency-sensitive path.
        conductor = conductor_status(self.cfg, include_landings=False)
        conductor["restart_allowed"] = bool(
            getattr(self.server, "allow_host_conductor_control", False)
        )
        self._send_json({
            "issues": stuck_issues(self.cfg, conductor=conductor),
            "repo": self.cfg.github_repo,
            "conductor": conductor,
        })

    def _api_recovery(self) -> None:
        """Queue one explicit, PR-preserving recovery operation."""
        data = self._read_guarded_body()
        if data is None:
            return  # the guard already sent the 4xx (origin / body / token)
        DeckHandler._queue_recovery(
            self, data, require_snapshot_token=True
        )

    def _api_requeue(self) -> None:
        """Deprecated safe alias for cached clients.

        Old JavaScript only sends ``{issue}``.  Never interpret that ambiguous
        request as destructive start-over: recompute the server recommendation
        and queue that action.  New clients use ``/api/recovery`` and name the
        action explicitly.
        """
        data = self._read_guarded_body()
        if data is None:
            return
        DeckHandler._queue_recovery(
            self, data, require_snapshot_token=False
        )

    def _queue_recovery(
        self, data: dict, *, require_snapshot_token: bool
    ) -> None:
        issue_raw = data.get("issue")
        try:
            if isinstance(issue_raw, bool):
                raise ValueError
            issue_num = int(issue_raw)
            if issue_num <= 0:
                raise ValueError
        except (TypeError, ValueError):
            return self._send_error_json(
                HTTPStatus.BAD_REQUEST, f"bad or missing issue {issue_raw!r}"
            )
        if self._demo:
            return self._send_json(
                {"ok": False, "accepted": False, "requeued": False,
                 "message": "recovery is not available in demo mode"}
            )
        from marathon.deck.dashboard import stuck_issues

        row = next(
            (r for r in stuck_issues(self.cfg) if r.get("num") == issue_num),
            None,
        )
        if row is None:
            return self._send_error_json(
                HTTPStatus.CONFLICT,
                f"#{issue_num} is no longer parked; recovery options changed",
            )
        supplied_token = str(data.get("recovery_token") or "")
        if require_snapshot_token and not hmac.compare_digest(
            supplied_token, str(row.get("recovery_token") or "")
        ):
            return self._send_error_json(
                HTTPStatus.CONFLICT,
                f"#{issue_num} changed since the recovery dialog opened; refresh",
            )
        # The deprecated endpoint has no snapshot token and historically sent
        # only ``{issue}``. Never honor a named action supplied to that route:
        # recompute the current recommendation so a cached/stale caller cannot
        # smuggle a destructive choice across a changed PR snapshot.
        action = (
            data.get("action")
            if require_snapshot_token
            else row.get("recommended_action")
        )
        if not require_snapshot_token and action is None:
            return self._send_error_json(
                HTTPStatus.CONFLICT,
                f"#{issue_num}: no safe recovery action is currently available",
            )
        if not isinstance(action, str):
            return self._send_error_json(
                HTTPStatus.BAD_REQUEST, "recovery action must be a string"
            )
        action_spec = (row.get("actions") or {}).get(action)
        if not action_spec or not action_spec.get("enabled"):
            return self._send_error_json(
                HTTPStatus.CONFLICT,
                f"#{issue_num}: recovery action {action!r} is unavailable — "
                f"{(action_spec or {}).get('reason', 'refresh recovery options')}",
            )
        # Start-over always requires an explicit destructive confirmation.
        # Even when the current lookup confirms no open PR, unpublished local
        # work may exist; cached legacy clients therefore cannot trigger it.
        if (
            action == "start_over"
            and data.get("confirm_destructive") is not True
        ):
            return self._send_error_json(
                HTTPStatus.BAD_REQUEST,
                "start_over requires confirm_destructive=true",
            )

        from marathon.review.state import (
            load_state,
            record_rejection_if_absent,
            request_recovery_if_current,
        )

        st = load_state(self.cfg).issues.get(issue_num)
        rendered_orphan = bool(row.get("orphaned"))
        if rendered_orphan != (st is None):
            return self._send_error_json(
                HTTPStatus.CONFLICT,
                f"#{issue_num}: recovery state changed after the snapshot was "
                "validated; refresh",
            )
        if st is not None:
            if (
                st.status != row.get("status")
                or st.verdict_ts != row.get("verdict_ts")
                or st.recovery_id != row.get("recovery_id")
                or st.recovery_mode != row.get("recovery_mode")
                or st.attempts != row.get("attempts")
            ):
                return self._send_error_json(
                    HTTPStatus.CONFLICT,
                    f"#{issue_num}: recovery state changed after the snapshot "
                    "was validated; refresh",
                )
            # A LIVE entry (any status): it needs a usable stored note AND a
            # parked status. A live entry with no note — e.g. a locally VERIFIED
            # issue still carrying a stale review:rejected label — is NOT an
            # orphan; refuse rather than silently re-reject a settled verdict.
            # (Orphan is defined as `st is None`, symmetric with stuck_issues.)
            if not (st.notes or "").strip() and action != "retry_build":
                return self._send_error_json(
                    HTTPStatus.BAD_REQUEST,
                    f"#{issue_num}: no stored rejection note to recover"
                )
            is_stuck = st.status == "stalled" or (
                st.status == "rejected" and not st.needs_iteration())
            if not is_stuck:
                return self._send_error_json(
                    HTTPStatus.CONFLICT,
                    f"#{issue_num} is not parked (status {st.status}) — "
                    "nothing to recover"
                )
            note = st.notes or ""
        else:
            # ORPHANED rejection — NO local state entry at all, because the
            # git-tracked state.json was reverted (e.g. a hard reset) while the
            # GitHub review:rejected label + reject comment survived. GitHub is
            # the durable truth: confirm the label and recover the verbatim note
            # from the issue's reject comment, then re-record.
            from marathon.review.github import (
                fetch_reject_note, list_rejected_issues,
            )

            rejected = list_rejected_issues(self.cfg)
            if not rejected or issue_num not in rejected:
                return self._send_error_json(
                    HTTPStatus.BAD_REQUEST,
                    f"#{issue_num}: no stored rejection note to recover"
                )
            note, _ts = fetch_reject_note(issue_num, self.cfg.github_repo)
            if not (note or "").strip():
                return self._send_error_json(
                    HTTPStatus.BAD_REQUEST,
                    f"#{issue_num}: rejected on GitHub but its reject note "
                    "could not be recovered — re-reject it in the deck"
                )
        # External projection first, then an exact local compare-and-swap. If a
        # concurrent verify/new rejection wins after our initial read, the CAS
        # refuses to overwrite it and we repair GitHub to the winner below.
        from marathon.review.review import (
            _ensure_conductor,
            _ensure_rejected_label,
            _ensure_unreviewed_labels,
            _ensure_verified_label,
        )

        if not _ensure_rejected_label(self.cfg, issue_num):
            return self._send_error_json(
                HTTPStatus.BAD_GATEWAY,
                f"#{issue_num}: could not confirm rejected-only GitHub labels; "
                 "nothing was queued locally",
            )
        operation_id = secrets.token_hex(16)
        recovery_kwargs = {
            "recovery_mode": action,
            "recovery_pr": row.get("fix_pr"),
            "recovery_branch": row.get("fix_pr_branch"),
            "recovery_head_sha": row.get("fix_pr_head_sha"),
            "recovery_base": (
                (row.get("fix_pr_base") or "main")
                if row.get("fix_pr")
                else None
            ),
        }
        try:
            if st is None:
                recorded = record_rejection_if_absent(
                    self.cfg, issue_num, note,
                    recovery_id=operation_id,
                    **recovery_kwargs,
                )
            else:
                recorded = request_recovery_if_current(
                    self.cfg,
                    issue_num,
                    expected_status=str(row.get("status")),
                    expected_verdict_ts=str(row.get("verdict_ts")),
                    expected_recovery_id=row.get("recovery_id"),
                    recovery_id=operation_id,
                    **recovery_kwargs,
                )
            if recorded is None:
                # A concurrent verdict/state transition won. Restore the remote
                # labels to that winner instead of leaving the rejected label
                # from our failed requeue attempt behind.
                winner = load_state(self.cfg).issues.get(issue_num)
                if winner is None:
                    repaired = _ensure_unreviewed_labels(self.cfg, issue_num)
                elif winner.status == "verified":
                    repaired = _ensure_verified_label(self.cfg, issue_num)
                else:
                    repaired = _ensure_rejected_label(self.cfg, issue_num)
                detail = "GitHub labels repaired" if repaired else (
                    "GitHub label repair could not be confirmed"
                )
                return self._send_error_json(
                    HTTPStatus.CONFLICT,
                    f"#{issue_num}: verdict changed while queuing recovery; "
                    f"the newer state was preserved ({detail})",
                )
        except Exception as exc:  # noqa: BLE001 — surface, never crash the loop
            return self._send_error_json(
                HTTPStatus.INTERNAL_SERVER_ERROR, f"recovery failed: {exc}"
            )
        launch_error = None
        try:
            _ensure_conductor(self.cfg)
        except Exception as exc:  # noqa: BLE001 — intent is already durable
            # The recovery operation was successfully committed to state. A
            # host launch failure must not turn that success into a misleading
            # 500 (or encourage a duplicate click); report it as durably queued
            # for an external/manually restarted conductor instead.
            launch_error = str(exc)
        from marathon.deck.dashboard import conductor_status

        conductor = conductor_status(self.cfg)
        online = bool(conductor.get("ready", conductor.get("alive")))
        waiting = not online
        message = (
            f"Recovery queued for #{issue_num}; waiting for an external conductor"
            if waiting and (
                os.environ.get("MARATHON_NO_HOST_CONDUCTOR") or launch_error
            )
            else f"Recovery queued for #{issue_num}; waiting for the conductor"
            if waiting
            else f"Recovery queued for #{issue_num}; conductor will dispatch it"
        )
        self._send_json(
            {
                "ok": True,
                "accepted": True,
                # Backward-compatibility for cached clients that only inspect
                # this boolean before refreshing their view.
                "requeued": True,
                "operation_id": operation_id,
                "action": action,
                "issue": issue_num,
                "pr": row.get("fix_pr"),
                "queue_state": "waiting_for_conductor" if waiting else "queued",
                "conductor_online": online,
                "conductor_launch_error": launch_error,
                "message": message,
            },
            status=HTTPStatus.ACCEPTED,
        )

    def _api_prover_toggle(self) -> None:
        """Enable/disable the DAG prover (POST /api/prover-toggle). Flips the
        control file a SEPARATELY-running conductor polls each tick, so the web
        app turns proving on/off without managing the conductor process. Proofs
        still land only on the prover branch, never main. Token-gated like every
        POST. Body: ``{"enabled": bool}``."""
        data = self._read_guarded_body()
        if data is None:
            return  # the guard already sent the 4xx (origin / body / token)
        enabled = data.get("enabled")
        if not isinstance(enabled, bool):
            return self._send_error_json(
                HTTPStatus.BAD_REQUEST,
                f"'enabled' must be a boolean, got {enabled!r}",
            )
        if self._demo:
            return self._send_json(
                {"ok": False, "prover_enabled": False,
                 "message": "the prover toggle is not available in demo mode"}
            )
        try:
            from marathon import conductor
            conductor.set_prover_enabled(self.cfg.repo_dir, enabled)
        except Exception as exc:  # noqa: BLE001 — surface, never crash the loop
            return self._send_error_json(
                HTTPStatus.INTERNAL_SERVER_ERROR, f"prover toggle failed: {exc}"
            )
        self._send_json(
            {"ok": True, "prover_enabled": enabled,
             "message": f"proving {'enabled' if enabled else 'disabled'} — the "
                        "conductor applies it within a tick (proofs land on the "
                        "prover branch only)"}
        )

    def _api_conductor_start(self) -> None:
        """Ensure the host conductor is running after an explicit UI click.

        The request cannot supply a command, PID, repository, or concurrency;
        all launch details come from this server's trusted configuration.  The
        normal origin + session-token guard applies before any lifecycle work.
        """

        data = self._read_guarded_body()
        if data is None:
            return
        if self._demo:
            return self._send_error_json(
                HTTPStatus.CONFLICT,
                "orchestrator restart is unavailable in demo mode",
            )
        if not getattr(self.server, "allow_host_conductor_control", False):
            return self._send_error_json(
                HTTPStatus.FORBIDDEN,
                "host orchestrator control is disabled; restart it outside the deck",
            )

        try:
            from marathon.review.review import start_conductor

            with self.server._conductor_start_lock:  # type: ignore[attr-defined]
                result = start_conductor(self.cfg)
        except Exception as exc:  # noqa: BLE001 — return a retryable failure
            return self._send_error_json(
                HTTPStatus.SERVICE_UNAVAILABLE,
                f"orchestrator start failed: {exc}",
            )

        started = bool(result.started)
        self._send_json(
            {
                "ok": True,
                "state": "starting" if started else "running",
                "started": started,
                "pid": result.pid,
                "message": (
                    f"orchestrator starting as pid {result.pid}"
                    if started
                    else f"orchestrator already running as pid {result.pid}"
                ),
            },
            status=HTTPStatus.ACCEPTED if started else HTTPStatus.OK,
        )

    # --- helpers ----------------------------------------------------------

    def _chapter_from_query(self, query: dict) -> Optional[int]:
        raw = (query.get("chapter") or [None])[0]
        if raw is None or raw == "":
            return self.server.default_chapter  # type: ignore[attr-defined]
        try:
            return int(raw)
        except ValueError:
            return self.server.default_chapter  # type: ignore[attr-defined]

    def _origin_is_self(self, origin: str) -> bool:
        """True iff ``origin`` is this server's own loopback origin (or a
        configured trusted origin). The host:port is the actual bound address,
        so a browser POSTing from the page we served passes and any other origin
        (file://, another site, another port) is rejected.

        When the deck is fronted by an authenticating proxy on a public host,
        the browser's Origin is that public origin — not loopback — so the
        operator may add it via ``--trusted-origin`` (stored on the server) for
        the same-origin guard to accept the proxied POSTs."""
        host, port = self.server.server_address[:2]  # type: ignore[attr-defined]
        ours = {
            f"http://{host}:{port}",
            f"http://127.0.0.1:{port}",
            f"http://localhost:{port}",
        }
        ours |= set(getattr(self.server, "trusted_origins", ()) or ())
        return origin in ours


# ---------------------------------------------------------------------------
# Status assembly (the live pane — pure reads from existing runtime files)
# ---------------------------------------------------------------------------


def collect_status(cfg: "ReviewConfig") -> dict:
    """Assemble the live status blob from the conductor + landing runtime
    files. Pure read: jobs.json (running/finished jobs), landings.jsonl
    (recent landings), and the latest bounce reports. Every source
    degrades to empty rather than raising, so the status pane never breaks
    the page."""
    jobs = _status_jobs(cfg)
    return {
        "jobs": jobs,
        "landings": _status_landings(cfg),
        "bounces": _status_bounces(cfg),
        "building": sum(1 for job in jobs if job.get("status") == "running"),
        "source_token": _status_source_token(cfg),
    }


def _status_source_token(cfg: "ReviewConfig") -> Optional[str]:
    """Current HEAD/worktree token for browser definition-index refreshes."""
    try:
        from marathon.deck.definitions import definition_source_token

        return definition_source_token(Path(cfg.repo_dir))
    except Exception:  # noqa: BLE001 - status remains fail-soft
        return None


def _status_jobs(cfg: "ReviewConfig") -> list[dict]:
    from marathon.conductor import load_jobs_snapshot, snapshot_job_is_live

    try:
        snap = load_jobs_snapshot(cfg.repo_dir)
    except Exception:  # noqa: BLE001
        return []
    if not snap:
        return []
    out: list[dict] = []
    for job in snap.get("jobs", []):
        status = job.get("status")
        if status == "running" and not snapshot_job_is_live(job):
            status = "running (pid dead)"
        out.append({
            "issue_num": job.get("issue_num"),
            "chapter": job.get("chapter"),
            "status": status,
            "project_id": job.get("project_id"),
            "aristotle_status": job.get("aristotle_status"),
            "started_ts": job.get("started_ts"),
        })
    return out


def _status_landings(cfg: "ReviewConfig", limit: int = 10) -> list[dict]:
    """The opt-in landing-queue record this read is gone (deleted feature). The
    real merged-PR landings are on the Status tab via conductor_status; this
    lightweight poll field is now always empty (it always was — the queue was
    default-off)."""
    return []


def _status_bounces(cfg: "ReviewConfig", limit: int = 5) -> list[str]:
    """Landing-queue bounce reports — the queue is deleted, so always empty.
    (A refine that fails to build never leaves the per-issue --auto-pr flow;
    its failure shows as a stalled issue, not a landing bounce.)"""
    return []


# ---------------------------------------------------------------------------
# Server construction + run
# ---------------------------------------------------------------------------


def make_server(
    cfg: "ReviewConfig",
    *,
    port: int = 0,
    token: Optional[str] = None,
    default_chapter: Optional[int] = None,
    demo: bool = False,
    trusted_origins: "Sequence[str]" = (),
    allow_host_conductor_control: bool = False,
    verso_manager=_DEFAULT_VERSO_MANAGER,
) -> DeckServer:
    """Construct (do not start) a :class:`DeckServer` bound to 127.0.0.1
    on ``port`` (0 = an ephemeral OS-assigned port, used by tests). A fresh
    per-session token is minted unless one is supplied. The caller starts
    it via ``serve_forever`` (or drives the handler directly in tests).
    ``demo`` serves synthetic cards (no cfg/repo needed). ``trusted_origins``
    are extra origins the same-origin POST guard accepts (for a fronting
    proxy on a public host)."""
    if token is None:
        token = secrets.token_urlsafe(32)
    server = DeckServer(
        (BIND_HOST, port), cfg, token, default_chapter, demo=demo,
        allow_host_conductor_control=allow_host_conductor_control,
        verso_manager=verso_manager,
    )
    server.trusted_origins = tuple(trusted_origins or ())
    return server


def serve(
    cfg: "ReviewConfig",
    *,
    port: int = 0,
    default_chapter: Optional[int] = None,
    open_browser: bool = True,
    demo: bool = False,
    trusted_origins: "Sequence[str]" = (),
    pull_every: int = 0,
    audit_from: Optional[str] = None,
    allow_host_conductor_control: bool = False,
) -> int:
    """Start the deck server and block in ``serve_forever`` until Ctrl-C.

    Prints the localhost URL, a LOUD note that verify/reject are real and
    irreversible, then (unless ``open_browser`` is False) opens the URL in
    the default browser. Returns 0 on a clean Ctrl-C shutdown. The CLI
    (``marathon deck``) is a thin wrapper over this. ``trusted_origins`` widen
    the same-origin POST guard for a fronting proxy (see ``--trusted-origin``).
    ``pull_every`` > 0 starts a background loop that fast-forwards the repo every
    that-many seconds, keeping a HOSTED deck's working tree current.
    ``allow_host_conductor_control`` exposes an authenticated, explicit
    ensure-running action; it never starts from a GET or status poll."""
    server = make_server(
        cfg, port=port, default_chapter=default_chapter, demo=demo,
        trusted_origins=trusted_origins,
        allow_host_conductor_control=allow_host_conductor_control,
    )
    host, bound_port = server.server_address[:2]
    url = f"http://{host}:{bound_port}/"

    print("=" * 70)
    print(f"marathon deck — http://127.0.0.1:{bound_port}/")
    if demo:
        print(
            "  DEMO MODE: synthetic cards, no consumer repo / GitHub / "
            "Aristotle.\n"
            "  Swipe each declaration; shipping a subdeck is SIMULATED "
            "(fires nothing).\n"
            "  Per-decl verdicts are in-memory and reset on restart."
        )
    else:
        print(
            "  WARNING: verify and reject are REAL, IRREVERSIBLE actions —\n"
            "  verify MERGES the marathon PR + flips the tracker; reject\n"
            "  DISPATCHES Aristotle with your note verbatim. Only the swipe\n"
            "  buttons (a deliberate POST) fire them; loading or navigating\n"
            "  the page never does."
        )
    print(f"  bound to {host} (loopback only); chapter="
          f"{default_chapter if default_chapter is not None else 'all'}")
    if trusted_origins:
        print("  trusted proxy origins (same-origin guard widened): "
              + ", ".join(trusted_origins))
    if allow_host_conductor_control:
        print(
            "  host orchestrator control: ENABLED — authenticated users may\n"
            "  explicitly start the conductor with this host's credentials."
        )
    # Start semantic documentation before a reviewer opens the first card.
    # This is nonblocking and single-flight; the review server is already bound
    # and raw source remains immediately available throughout the build.
    server.prewarm_verso()
    # A fronted (shared) deck without --no-host-conductor is an attribution
    # footgun: a collaborator's REJECT spawns/feeds the HOST's conductor, so the
    # PR merge that lands their refine + the Aristotle spend are billed to the
    # HOST, not them. The per-request forwarded token covers only the
    # synchronous issue comment/label writes, never the detached conductor.
    if trusted_origins and not os.environ.get("MARATHON_NO_HOST_CONDUCTOR"):
        print(
            "  WARNING: shared/fronted deck WITHOUT --no-host-conductor — a\n"
            "  collaborator's reject feeds the HOST conductor, so the refine's\n"
            "  PR merge + Aristotle spend are attributed to the HOST's key,\n"
            "  not the rejecting collaborator. Pass --no-host-conductor so each\n"
            "  collaborator's own conductor refines under their own key."
        )
    # Hosted deck: keep the working tree current so cards reflect merged code.
    if pull_every and pull_every > 0 and cfg is not None and not demo:
        from marathon.deck.sync import start_repo_sync

        print(f"  repo sync: fast-forward pull every {pull_every}s "
              f"(keeps the hosted deck's source current)"
              + (f"; audit from {audit_from}" if audit_from else ""))
        start_repo_sync(
            cfg.repo_dir,
            pull_every,
            audit_from=audit_from,
            on_change=server.prewarm_verso,
        )
    print("  press Ctrl-C to stop.")
    print("=" * 70)

    if open_browser:
        try:
            webbrowser.open(url)
        except Exception as exc:  # noqa: BLE001 — never fail to start on this
            print(f"  (could not open a browser automatically: {exc})")

    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nshutting down deck server.")
    finally:
        server.server_close()
    return 0
