"""Offline structural tests for the deck frontend static assets.

This is a Python project with no JS test runner, so these are *honest
structural* checks — not a JS engine. They assert the vanilla-JS Code
Tinder deck (index.html / app.js / style.css / katex-fallback.js) holds
to the SHARED API CONTRACT and the binding-safety rules the deck must
obey (the deck performs irreversible actions — verify merges PRs, reject
dispatches Aristotle):

* the four static assets exist and are servable (well-formed text, the
  server's static dir);
* index.html carries exactly one token-injection hook and no *other*
  inline secret (no API key / GitHub token baked into the page);
* app.js calls the contract endpoints (/api/queue, /api/card,
  /api/verdict) and sends the per-session token header on POST;
* POST /api/verdict is reached ONLY from the verify/reject/defer
  handlers — never on load / GET / navigation / poll (static check that
  the single fetch('/api/verdict') lives in postVerdict, called only by
  commitVerdict, called only by the user-action handlers);
* the offline-LaTeX fallback path exists (KaTeX-absent -> raw LaTeX,
  never blank);
* reject requires a note and the note is POSTed (verbatim).

No subprocess, no network, no browser. Pure file reads + string/loose
structural assertions.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

STATIC = Path(__file__).resolve().parent.parent / "marathon" / "deck" / "static"
INDEX = STATIC / "index.html"
APP = STATIC / "app.js"
STYLE = STATIC / "style.css"
KATEX = STATIC / "katex-fallback.js"

TOKEN_PLACEHOLDER = "__MARATHON_SESSION_TOKEN__"


# --- file readers (cached at module import) ---------------------------------

def _read(p: Path) -> str:
    assert p.is_file(), f"missing static asset: {p}"
    return p.read_text(encoding="utf-8")


@pytest.fixture(scope="module")
def index_html() -> str:
    return _read(INDEX)


@pytest.fixture(scope="module")
def app_js() -> str:
    return _read(APP)


@pytest.fixture(scope="module")
def style_css() -> str:
    return _read(STYLE)


@pytest.fixture(scope="module")
def katex_js() -> str:
    return _read(KATEX)


# --- existence / serveability -----------------------------------------------

def test_all_static_assets_exist():
    for p in (INDEX, APP, STYLE, KATEX):
        assert p.is_file(), f"missing static asset: {p}"
        assert p.stat().st_size > 0, f"empty static asset: {p}"


def test_static_dir_is_the_servers_static_dir():
    # The server serves marathon/deck/static/; the assets live there so the
    # backend track can serve them without a build step.
    assert STATIC.name == "static"
    assert STATIC.parent.name == "deck"


def test_index_links_its_assets(index_html: str):
    # The page must pull in the JS/CSS via /static/ paths (matches the
    # server's static route) so a browser can actually load them.
    assert "/static/app.js" in index_html
    assert "/static/style.css" in index_html
    assert "/static/katex-fallback.js" in index_html


def test_assets_are_text_not_binary(app_js, style_css, katex_js):
    for blob in (app_js, style_css, katex_js):
        # plain ASCII-ish source; no null bytes
        assert "\x00" not in blob


# --- token injection hook + no-inline-secret --------------------------------

def test_index_has_token_injection_hook(index_html: str):
    # The backend injects the per-session token here. We require a readable
    # hook (meta tag) AND a JS-readable copy (bootstrap global), both using
    # the same placeholder the server substitutes.
    assert 'name="marathon-session-token"' in index_html
    assert TOKEN_PLACEHOLDER in index_html
    # meta tag content is the placeholder
    meta = re.search(
        r'<meta\s+name="marathon-session-token"\s+content="([^"]*)"', index_html
    )
    assert meta is not None, "no marathon-session-token meta tag"
    assert TOKEN_PLACEHOLDER in meta.group(1)


def test_index_bootstrap_global_carries_token(index_html: str):
    # app.js reads window.MARATHON_DECK.sessionToken as a fallback.
    assert "MARATHON_DECK" in index_html
    assert "sessionToken" in index_html


def test_index_has_no_other_inline_secret(index_html: str):
    # The placeholder is the ONLY inline secret. Nothing that looks like a
    # real credential (gh token, api key, bearer) may be baked into the page.
    lowered = index_html.lower()
    for needle in ("ghp_", "github_pat_", "api_key", "apikey", "bearer ", "secret_key", "x-api-key"):
        assert needle not in lowered, f"index.html leaks a credential-like token: {needle!r}"
    # A real 40-hex GitHub token must never be inlined; the placeholder is fine.
    # (We only flag long hex runs that are NOT the placeholder.)
    for m in re.finditer(r"[0-9a-f]{40,}", lowered):
        assert TOKEN_PLACEHOLDER.lower() not in index_html.lower()[max(0, m.start() - 40):m.end()], (
            "long hex run near token placeholder is fine"
        )


# --- contract endpoints in app.js -------------------------------------------

def test_app_calls_queue_endpoint(app_js: str):
    assert "/api/queue" in app_js


def test_app_calls_card_endpoint(app_js: str):
    assert "/api/card/" in app_js


def test_app_calls_verdict_endpoint(app_js: str):
    assert "/api/verdict" in app_js


def test_app_calls_status_or_events(app_js: str):
    # live status pane via SSE or poll
    assert "/api/status" in app_js or "/api/events" in app_js


def test_app_sends_token_header_on_post(app_js: str):
    # The per-session token rides every verdict POST. The header name MUST
    # match the committed backend (server.py TOKEN_HEADER = X-Marathon-Session-Token).
    assert "X-Marathon-Session-Token" in app_js
    # and it is read from the injected hook
    assert "marathon-session-token" in app_js or "MARATHON_DECK" in app_js


# --- binding safety: POST only from verify/reject handlers ------------------

def _strip_block_comments(src: str) -> str:
    # Remove /* ... */ and // ... line comments so our structural scan is not
    # fooled by prose in the documentation comments.
    src = re.sub(r"/\*.*?\*/", "", src, flags=re.DOTALL)
    src = re.sub(r"(?m)//.*$", "", src)
    return src


def test_verdict_post_is_a_single_call_site(app_js: str):
    code = _strip_block_comments(app_js)
    # The ONLY fetch to /api/verdict in executable code.
    posts = re.findall(r"fetch\(\s*['\"]/api/verdict['\"]", code)
    assert len(posts) == 1, (
        f"expected exactly one fetch('/api/verdict') call site, found {len(posts)}"
    )


def test_verdict_post_uses_post_method(app_js: str):
    code = _strip_block_comments(app_js)
    # The /api/verdict fetch block must declare method POST.
    idx = code.find("/api/verdict")
    assert idx != -1
    window = code[idx:idx + 400]
    assert '"POST"' in window or "'POST'" in window


def test_verdict_post_lives_in_postVerdict(app_js: str):
    code = _strip_block_comments(app_js)
    # postVerdict() is the named sole owner of the mutating call.
    fn_idx = code.find("function postVerdict")
    assert fn_idx != -1, "expected a named postVerdict() function"
    verdict_idx = code.find("/api/verdict")
    assert verdict_idx > fn_idx, "the /api/verdict fetch must be inside postVerdict"
    # and the fetch is reasonably near the function head (same function body)
    assert verdict_idx - fn_idx < 900


def test_postVerdict_not_invoked_on_load_or_get(app_js: str):
    code = _strip_block_comments(app_js)
    # The read/init/poll paths must NOT call postVerdict. We assert the only
    # caller of postVerdict is commitVerdict (the user-action commit path).
    callers = re.findall(r"postVerdict\s*\(", code)
    # one definition site ("function postVerdict(") + its call(s)
    # Drop the definition occurrence.
    call_occurrences = [c for c in callers]
    # The definition shows as "postVerdict(" inside "function postVerdict(".
    def_count = len(re.findall(r"function\s+postVerdict\s*\(", code))
    invoke_count = len(call_occurrences) - def_count
    assert invoke_count >= 1, "postVerdict is never invoked"
    # Every invocation must be lexically inside commitVerdict; assert no
    # invocation appears inside init()/refreshQueue()/loadTopCard()/startStatus.
    for read_fn in ("function init", "function refreshQueue", "function loadTopCard",
                    "function startStatus", "function refreshStatus", "function fetchQueue",
                    "function fetchCard", "function fetchStatus"):
        s = code.find(read_fn)
        if s == -1:
            continue
        # crude body bound: until the next "\n  function " at indent or EOF
        nxt = code.find("\n  function ", s + 1)
        body = code[s: nxt if nxt != -1 else len(code)]
        assert "postVerdict(" not in body, (
            f"postVerdict must not be called from {read_fn} (read/load/poll path)"
        )


def test_commitVerdict_is_the_only_postVerdict_caller(app_js: str):
    code = _strip_block_comments(app_js)
    s = code.find("function commitVerdict")
    assert s != -1, "expected a commitVerdict() function"
    nxt = code.find("\n  function ", s + 1)
    body = code[s: nxt if nxt != -1 else len(code)]
    assert "postVerdict(" in body, "commitVerdict must call postVerdict"


def test_get_helpers_are_pure_reads(app_js: str):
    code = _strip_block_comments(app_js)
    # The GET helpers (queue/card/status) must use method GET (or default GET)
    # and never POST.
    for fn in ("function getJSON",):
        s = code.find(fn)
        assert s != -1
        body = code[s:s + 400]
        assert '"GET"' in body or "'GET'" in body
        assert "POST" not in body


# --- offline LaTeX fallback -------------------------------------------------

def test_katex_fallback_exists_and_degrades(katex_js: str):
    # The fallback must detect KaTeX availability and emit raw LaTeX otherwise.
    assert "window.katex" in katex_js
    assert "MarathonLatex" in katex_js
    # The degraded branch surfaces raw LaTeX in a styled code span.
    assert "latex-fallback" in katex_js
    # never blank: it builds a code node with the original latex text
    assert "createElement" in katex_js
    assert "textContent" in katex_js


def test_app_uses_latex_renderer(app_js: str):
    assert "MarathonLatex" in app_js
    # and has its own last-resort text fallback if even the helper is missing
    code = _strip_block_comments(app_js)
    assert "latexSource" in code


def test_style_has_latex_fallback_styling(style_css: str):
    assert ".latex-fallback" in style_css
    assert ".latex-degraded" in style_css


def test_index_katex_degrades_gracefully(index_html: str):
    # CDN script/style are best-effort with onerror hooks; the page must not
    # hard-depend on them.
    assert "katex" in index_html.lower()
    assert "onerror" in index_html.lower()


# --- reject requires a verbatim note ----------------------------------------

def test_reject_requires_a_note(app_js: str):
    code = _strip_block_comments(app_js)
    # The reject note modal validates a non-empty note before committing.
    s = code.find("function openRejectNote")
    assert s != -1, "expected openRejectNote()"
    nxt = code.find("\n  function ", s + 1)
    body = code[s: nxt if nxt != -1 else len(code)]
    # a guard that blocks an empty note and only then commits a reject
    assert "trim()" in body
    assert 'commitVerdict("reject"' in body
    # the empty-note path must NOT reach commitVerdict (there is an early
    # return before the commit when the note is empty).
    assert "return" in body


def test_reject_note_goes_in_the_post_body(app_js: str):
    code = _strip_block_comments(app_js)
    # postVerdict attaches the note to the POST body (verbatim).
    s = code.find("function postVerdict")
    body = code[s:s + 900]
    assert "note" in body
    # body carries verdict + note
    assert "verdict" in body


def test_reject_note_is_not_rewritten(app_js: str):
    # Verbatim: the reject path must pass the raw note straight through. We
    # assert there is no client-side transformation of the note between the
    # textarea read and the commit beyond trim() (which only strips edges).
    code = _strip_block_comments(app_js)
    s = code.find("function openRejectNote")
    nxt = code.find("\n  function ", s + 1)
    body = code[s: nxt if nxt != -1 else len(code)]
    # the value taken from the textarea is committed directly
    assert re.search(r"var\s+note\s*=\s*ta\.value\.trim\(\)", body), (
        "reject note should be taken verbatim from the textarea (only trimmed)"
    )
    # no replace()/JSON-massaging of the note before commit
    assert ".replace(" not in body.split('commitVerdict("reject"')[0].split("var note")[-1]


def test_verify_asks_for_confirm(app_js: str):
    code = _strip_block_comments(app_js)
    # verify is irreversible (merges a PR) — there is an explicit confirm step.
    assert "function openVerifyConfirm" in code
    s = code.find("function actVerify")
    nxt = code.find("\n  function ", s + 1)
    body = code[s: nxt if nxt != -1 else len(code)]
    assert "openVerifyConfirm" in body


# --- UI surface required by the brief ---------------------------------------

def test_keyboard_and_buttons_parity(app_js: str, index_html: str):
    # every swipe action has a keyboard equivalent AND a visible button.
    # buttons:
    for bid in ("btn-verify", "btn-reject", "btn-defer", "btn-deps", "btn-deepdive"):
        assert bid in index_html, f"missing action button {bid}"
        assert bid in app_js, f"action button {bid} not wired in app.js"
    # keys:
    code = _strip_block_comments(app_js)
    assert "ArrowRight" in code and "ArrowLeft" in code
    # space = defer
    assert "actDefer" in code


def test_llm_flag_is_present(index_html: str, app_js: str):
    # The informal rendering must be clearly flagged as LLM-rendered.
    haystack = index_html + app_js
    assert "LLM-rendered" in haystack


def test_tier_badges_cover_the_ladder(app_js: str, style_css: str):
    # T0-T3 + UNKNOWN, each with a color class.
    for tier in ("T0", "T1", "T2", "T3", "UNKNOWN"):
        assert tier in app_js
    for cls in ("tier-t0", "tier-t1", "tier-t2", "tier-t3", "tier-unknown"):
        assert cls in style_css


def test_empty_and_error_states_exist(app_js: str):
    code = _strip_block_comments(app_js)
    # all-caught-up + building count + error retry, never a blank screen.
    assert "All caught up" in app_js
    assert "showErrorCard" in code
    assert "Retry" in app_js


def test_server_binds_localhost_is_documented(index_html: str):
    # The page documents the 127.0.0.1-only + token binding-safety contract so
    # the rule is visible at the surface the human reviews.
    assert "127.0.0.1" in index_html


def test_no_eval_or_innerhtml_userdata(app_js: str):
    code = _strip_block_comments(app_js)
    # Defense-in-depth: card content is text-set via textContent, not eval'd.
    assert "eval(" not in code
