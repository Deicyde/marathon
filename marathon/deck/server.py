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
* ``GET  /api/status``        → live conductor/landing events (poll)
* ``GET  /api/events``        → the same, as Server-Sent Events
* ``POST /api/verdict``       → route a verdict (IRREVERSIBLE — guarded)
* ``POST /api/decl-verdict``  → record one per-decl verdict (LOCAL — guarded)
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

import json
import os
import secrets
import webbrowser
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import TYPE_CHECKING, Optional, Sequence
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
    ):
        self.cfg = cfg
        self.token = token
        self.default_chapter = default_chapter
        # Demo mode: the API routes to marathon.deck.demo (synthetic cards,
        # in-memory verdicts, a SIMULATED ship that fires nothing). No cfg /
        # repo / GitHub / Aristotle is touched.
        self.demo = demo
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
            if path.startswith("/api/subdeck/"):
                return self._api_subdeck(path[len("/api/subdeck/"):], query)
            if path.startswith("/api/missing-suggestions/"):
                return self._api_missing_suggestions(
                    path[len("/api/missing-suggestions/"):]
                )
            if path == "/api/status":
                return self._api_status()
            if path == "/api/events":
                return self._api_events()
            if path == "/api/conductor-status":
                return self._api_conductor_status()
            if path.startswith("/api/run/"):
                return self._api_run_detail(path[len("/api/run/"):])
            if path == "/api/issues-overview":
                return self._api_issues_overview()
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

    @property
    def _demo(self) -> bool:
        return bool(getattr(self.server, "demo", False))  # type: ignore[attr-defined]

    def _api_queue(self, query: dict) -> None:
        if self._demo:
            from marathon.deck.demo import demo_queue
            return self._send_json(demo_queue())
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
        self._send_json(conductor_status(self.cfg))

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
        "/api/verdict", "/api/decl-verdict", "/api/missing", "/api/ship",
        "/api/refresh-issue",
    )

    def do_POST(self) -> None:  # noqa: N802 (stdlib handler name)
        parsed = urlsplit(self.path)
        if parsed.path not in self._POST_ROUTES:
            return self._send_error_json(
                HTTPStatus.NOT_FOUND, f"no POST route {parsed.path!r}"
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
                elif parsed.path == "/api/missing":
                    self._api_missing()
                elif parsed.path == "/api/refresh-issue":
                    self._api_refresh_issue()
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

        if self._demo:
            from marathon.deck.demo import demo_record_verdict
            try:
                return self._send_json(
                    demo_record_verdict(issue_num, decl, verdict, note)
                )
            except ValueError as exc:
                return self._send_error_json(HTTPStatus.BAD_REQUEST, str(exc))

        from marathon.deck.subdeck import (
            build_subdeck,
            next_unreviewed_card,
            record_decl_verdict,
        )

        try:
            record_decl_verdict(self.cfg, issue_num, decl, verdict, note)
        except ValueError as exc:  # unknown verdict / empty reject note
            return self._send_error_json(HTTPStatus.BAD_REQUEST, str(exc))
        except Exception as exc:  # noqa: BLE001 — surface, never crash the loop
            return self._send_error_json(
                HTTPStatus.INTERNAL_SERVER_ERROR,
                f"decl-verdict failed: {exc}",
            )

        subdeck = build_subdeck(self.cfg, issue_num)
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
        self._send_json({"ok": True, "missing": subdeck.get("missing")})

    def _api_ship(self) -> None:
        """Ship a subdeck's issue (the contract's POST /api/ship) — the ONLY
        endpoint besides /api/verdict that fires an irreversible action.
        Composes the per-issue outcome from the LOCAL subdeck and routes it
        through the committed verdict path (verify, or reject with the
        composed note carried verbatim). An incomplete subdeck returns
        ``{ok:false, reason}`` with NO side effect."""
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

        if self._demo:
            from marathon.deck.demo import demo_ship
            return self._send_json(demo_ship(issue_num, skip_api=skip_api))

        from marathon.deck.subdeck import ship_issue

        try:
            result = ship_issue(self.cfg, issue_num, skip_api=skip_api)
        except Exception as exc:  # noqa: BLE001 — surface, never crash the loop
            return self._send_error_json(
                HTTPStatus.INTERNAL_SERVER_ERROR, f"ship failed: {exc}"
            )
        self._send_json(result)

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
    demo: bool = False,
    trusted_origins: "Sequence[str]" = (),
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
    server = DeckServer((BIND_HOST, port), cfg, token, default_chapter, demo=demo)
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
) -> int:
    """Start the deck server and block in ``serve_forever`` until Ctrl-C.

    Prints the localhost URL, a LOUD note that verify/reject are real and
    irreversible, then (unless ``open_browser`` is False) opens the URL in
    the default browser. Returns 0 on a clean Ctrl-C shutdown. The CLI
    (``marathon deck``) is a thin wrapper over this. ``trusted_origins`` widen
    the same-origin POST guard for a fronting proxy (see ``--trusted-origin``)."""
    server = make_server(
        cfg, port=port, default_chapter=default_chapter, demo=demo,
        trusted_origins=trusted_origins,
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
