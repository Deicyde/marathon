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
* ``GET  /api/status``        → live conductor/landing events (poll)
* ``GET  /api/events``        → the same, as Server-Sent Events
* ``POST /api/verdict``       → route a verdict (IRREVERSIBLE — guarded)

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

import json
import secrets
import webbrowser
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import TYPE_CHECKING, Optional
from urllib.parse import parse_qs, urlsplit

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

# Static assets live in the OTHER track's dir. Served read-only; tolerated
# absent (tests run without it; an early launch may pre-date app.js/style.css).
STATIC_DIR = Path(__file__).resolve().parent / "static"

# A small static-asset content-type table (stdlib mimetypes is fine too,
# but this keeps the common deck assets explicit + dependency-free).
_CONTENT_TYPES = {
    ".html": "text/html; charset=utf-8",
    ".css": "text/css; charset=utf-8",
    ".js": "application/javascript; charset=utf-8",
    ".json": "application/json; charset=utf-8",
    ".svg": "image/svg+xml",
    ".png": "image/png",
    ".ico": "image/x-icon",
    ".woff": "font/woff",
    ".woff2": "font/woff2",
    ".map": "application/json; charset=utf-8",
}

# Cap on a POST body the verdict endpoint will read (a verdict is tiny; a
# huge body is either a bug or an attack — refuse it, never buffer it).
MAX_POST_BYTES = 256 * 1024


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
    ):
        self.cfg = cfg
        self.token = token
        self.default_chapter = default_chapter
        super().__init__(server_address, DeckHandler)


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
        self, body: bytes, content_type: str, status: int = HTTPStatus.OK
    ) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("X-Content-Type-Options", "nosniff")
        self.end_headers()
        self.wfile.write(body)

    # --- GET (pure reads only) --------------------------------------------

    def do_GET(self) -> None:  # noqa: N802 (stdlib handler name)
        parsed = urlsplit(self.path)
        path = parsed.path
        query = parse_qs(parsed.query)
        try:
            if path in ("/", "/index.html"):
                return self._serve_index()
            if path.startswith("/static/"):
                return self._serve_static(path)
            if path == "/api/queue":
                return self._api_queue(query)
            if path.startswith("/api/card/"):
                return self._api_card(path[len("/api/card/"):])
            if path == "/api/status":
                return self._api_status()
            if path == "/api/events":
                return self._api_events()
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
        self._send_bytes(html.encode("utf-8"), _CONTENT_TYPES[".html"])

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

    def _api_queue(self, query: dict) -> None:
        from marathon.deck.cards import build_queue

        chapter = self._chapter_from_query(query)
        queue = build_queue(self.cfg, chapter)
        self._send_json(queue.to_json())

    def _api_card(self, raw_id: str) -> None:
        from marathon.deck.cards import build_card_detail

        try:
            issue_num = int(raw_id)
        except ValueError:
            return self._send_error_json(
                HTTPStatus.BAD_REQUEST, f"bad card id {raw_id!r}"
            )
        detail = build_card_detail(self.cfg, issue_num)
        self._send_json(detail.to_json())

    def _api_status(self) -> None:
        """Poll endpoint: the live conductor/landing events as one JSON
        blob. Pure read from jobs.json + landings.jsonl + bounce reports."""
        self._send_json(collect_status(self.cfg))

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

    # --- POST (the single irreversible route — token-guarded) -------------

    def do_POST(self) -> None:  # noqa: N802 (stdlib handler name)
        parsed = urlsplit(self.path)
        if parsed.path != "/api/verdict":
            return self._send_error_json(
                HTTPStatus.NOT_FOUND, f"no POST route {parsed.path!r}"
            )
        try:
            self._api_verdict()
        except BrokenPipeError:
            return

    def _api_verdict(self) -> None:
        # (1) Cross-origin guard: a verdict may only come from a page this
        #     server served (same-origin). A present Origin that is not our
        #     own loopback origin is a stray/other-origin POST — refuse it
        #     BEFORE reading the body or touching the token.
        origin = self.headers.get("Origin")
        if origin is not None and not self._origin_is_self(origin):
            return self._send_error_json(
                HTTPStatus.FORBIDDEN,
                "cross-origin verdict refused (verdicts are loopback-only)",
            )

        # (2) Read the (small) body.
        try:
            length = int(self.headers.get("Content-Length", "0"))
        except ValueError:
            return self._send_error_json(
                HTTPStatus.BAD_REQUEST, "bad Content-Length"
            )
        if length > MAX_POST_BYTES:
            return self._send_error_json(
                HTTPStatus.REQUEST_ENTITY_TOO_LARGE, "verdict body too large"
            )
        raw = self.rfile.read(length) if length > 0 else b""
        try:
            data = json.loads(raw.decode("utf-8")) if raw else {}
        except (ValueError, UnicodeDecodeError):
            return self._send_error_json(
                HTTPStatus.BAD_REQUEST, "verdict body is not valid JSON"
            )
        if not isinstance(data, dict):
            return self._send_error_json(
                HTTPStatus.BAD_REQUEST, "verdict body must be a JSON object"
            )

        # (3) Per-session token check — the gate on the irreversible action.
        #     Accept the token from the header (preferred) or the body. A
        #     constant-time compare avoids leaking it via timing. NO token /
        #     wrong token => 403 and NO side effect.
        supplied = self.headers.get(TOKEN_HEADER) or data.get("token")
        expected = self.server.token  # type: ignore[attr-defined]
        if not supplied or not secrets.compare_digest(str(supplied), expected):
            return self._send_error_json(
                HTTPStatus.FORBIDDEN,
                "missing or invalid session token; verdict not applied",
            )

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

        from marathon.deck.verdicts import VerdictError, apply_verdict

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
        """True iff ``origin`` is this server's own loopback origin. The
        host:port is the actual bound address, so a browser POSTing from
        the page we served passes and any other origin (file://, another
        site, another port) is rejected."""
        host, port = self.server.server_address[:2]  # type: ignore[attr-defined]
        ours = {
            f"http://{host}:{port}",
            f"http://127.0.0.1:{port}",
            f"http://localhost:{port}",
        }
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
    return {
        "jobs": _status_jobs(cfg),
        "landings": _status_landings(cfg),
        "bounces": _status_bounces(cfg),
        "building": _count_running(cfg),
    }


def _status_jobs(cfg: "ReviewConfig") -> list[dict]:
    from marathon.conductor import load_jobs_snapshot

    try:
        snap = load_jobs_snapshot(cfg.repo_dir)
    except Exception:  # noqa: BLE001
        return []
    if not snap:
        return []
    out: list[dict] = []
    for job in snap.get("jobs", []):
        out.append({
            "issue_num": job.get("issue_num"),
            "chapter": job.get("chapter"),
            "status": job.get("status"),
            "project_id": job.get("project_id"),
            "aristotle_status": job.get("aristotle_status"),
            "started_ts": job.get("started_ts"),
        })
    return out


def _count_running(cfg: "ReviewConfig") -> int:
    return sum(1 for j in _status_jobs(cfg) if j.get("status") == "running")


def _status_landings(cfg: "ReviewConfig", limit: int = 10) -> list[dict]:
    from marathon.landing import LANDINGS_RELPATH

    path = Path(cfg.repo_dir) / LANDINGS_RELPATH
    if not path.is_file():
        return []
    rows: list[dict] = []
    try:
        for line in path.read_text().splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                rows.append(json.loads(line))
            except (ValueError, TypeError):
                continue
    except OSError:
        return []
    return rows[-limit:]


def _status_bounces(cfg: "ReviewConfig", limit: int = 5) -> list[str]:
    from marathon.landing import BOUNCES_RELPATH

    bdir = Path(cfg.repo_dir) / BOUNCES_RELPATH
    if not bdir.is_dir():
        return []
    try:
        reports = sorted(p.name for p in bdir.glob("*.md"))
    except OSError:
        return []
    return reports[-limit:]


# ---------------------------------------------------------------------------
# Server construction + run
# ---------------------------------------------------------------------------


def make_server(
    cfg: "ReviewConfig",
    *,
    port: int = 0,
    token: Optional[str] = None,
    default_chapter: Optional[int] = None,
) -> DeckServer:
    """Construct (do not start) a :class:`DeckServer` bound to 127.0.0.1
    on ``port`` (0 = an ephemeral OS-assigned port, used by tests). A fresh
    per-session token is minted unless one is supplied. The caller starts
    it via ``serve_forever`` (or drives the handler directly in tests)."""
    if token is None:
        token = secrets.token_urlsafe(32)
    return DeckServer((BIND_HOST, port), cfg, token, default_chapter)


def serve(
    cfg: "ReviewConfig",
    *,
    port: int = 0,
    default_chapter: Optional[int] = None,
    open_browser: bool = True,
) -> int:
    """Start the deck server and block in ``serve_forever`` until Ctrl-C.

    Prints the localhost URL, a LOUD note that verify/reject are real and
    irreversible, then (unless ``open_browser`` is False) opens the URL in
    the default browser. Returns 0 on a clean Ctrl-C shutdown. The CLI
    (``marathon deck``) is a thin wrapper over this."""
    server = make_server(cfg, port=port, default_chapter=default_chapter)
    host, bound_port = server.server_address[:2]
    url = f"http://{host}:{bound_port}/"

    print("=" * 70)
    print(f"marathon deck — http://127.0.0.1:{bound_port}/")
    print(
        "  WARNING: verify and reject are REAL, IRREVERSIBLE actions —\n"
        "  verify MERGES the marathon PR + flips the tracker; reject\n"
        "  DISPATCHES Aristotle with your note verbatim. Only the swipe\n"
        "  buttons (a deliberate POST) fire them; loading or navigating\n"
        "  the page never does."
    )
    print(f"  bound to {host} (loopback only); chapter="
          f"{default_chapter if default_chapter is not None else 'all'}")
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
