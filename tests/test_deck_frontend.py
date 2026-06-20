"""Offline structural tests for the deck frontend static assets.

This is a Python project with no JS test runner, so these are *honest
structural* checks — not a JS engine. They assert the vanilla-JS Code
Tinder deck (index.html / app.js / style.css / katex-fallback.js) holds
to the SHARED API CONTRACT for the SUBDECK flow and the binding-safety
rules the deck must obey (the deck performs irreversible actions — ship
either merges a PR or dispatches Aristotle).

The subdeck flow:

* the QUEUE is the ISSUE picker (GET /api/queue, unchanged);
* picking a ready issue opens its SUBDECK (GET /api/subdeck/{issue});
* the human reviews ONE DECLARATION per card; each swipe records a LOCAL
  per-decl verdict (POST /api/decl-verdict — merges nothing, dispatches no
  Aristotle, writes no ledger verdict);
* when every declaration is reviewed, a confirm-and-ship SUMMARY card
  appears; SHIP (POST /api/ship) composes the per-issue outcome and is the
  ONLY irreversible action.

What these tests enforce:

* the four static assets exist and are servable;
* index.html carries exactly one token-injection hook and no other inline
  secret;
* app.js calls the contract endpoints (/api/queue, /api/subdeck,
  /api/decl-verdict, /api/ship) and sends the per-session token header on
  every POST;
* POST /api/ship is reached ONLY from the ship-button handler (shipIssue) —
  never on load / GET / navigation / poll / a per-decl verdict (a static
  check that the single fetch('/api/ship') lives in postShip, whose sole
  caller is shipIssue);
* reject requires a non-empty note and the note is POSTed verbatim;
* the completion summary renders the KEEP (verified) + FIX (rejected) lists;
* the offline-LaTeX fallback path + source highlighting are still wired.

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


def _strip_block_comments(src: str) -> str:
    # Remove /* ... */ and // ... line comments so our structural scan is not
    # fooled by prose in the documentation comments.
    src = re.sub(r"/\*.*?\*/", "", src, flags=re.DOTALL)
    src = re.sub(r"(?m)//.*$", "", src)
    return src


def _fn_body(code: str, header: str) -> str:
    """Return the (crude) body of a top-level function `header` (e.g.
    'function shipIssue') up to the next top-level '  function ' at the
    module's 2-space indent, or EOF."""
    s = code.find(header)
    assert s != -1, f"expected {header!r} in app.js"
    nxt = code.find("\n  function ", s + 1)
    return code[s: nxt if nxt != -1 else len(code)]


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
    for m in re.finditer(r"[0-9a-f]{40,}", lowered):
        assert TOKEN_PLACEHOLDER.lower() not in index_html.lower()[max(0, m.start() - 40):m.end()], (
            "long hex run near token placeholder is fine"
        )


# --- contract endpoints in app.js -------------------------------------------

def test_app_calls_queue_endpoint(app_js: str):
    # The queue is still the issue picker.
    assert "/api/queue" in app_js


def test_app_calls_subdeck_endpoint(app_js: str):
    # GET /api/subdeck/{issue} opens the per-declaration subdeck.
    assert "/api/subdeck/" in app_js


def test_app_calls_decl_verdict_endpoint(app_js: str):
    # POST /api/decl-verdict records a LOCAL per-decl verdict.
    assert "/api/decl-verdict" in app_js


def test_app_calls_ship_endpoint(app_js: str):
    # POST /api/ship is the per-issue, irreversible ship.
    assert "/api/ship" in app_js


def test_app_calls_status_or_events(app_js: str):
    # live status pane via SSE or poll
    assert "/api/status" in app_js or "/api/events" in app_js


def test_app_sends_token_header_on_every_post(app_js: str):
    # The per-session token rides every POST (decl-verdict AND ship). The
    # header name MUST match the committed backend (server.py TOKEN_HEADER =
    # X-Marathon-Session-Token). Both POSTs flow through the shared postJSON
    # transport, which attaches the header.
    assert "X-Marathon-Session-Token" in app_js
    code = _strip_block_comments(app_js)
    body = _fn_body(code, "function postJSON")
    assert "TOKEN_HEADER" in body or "X-Marathon-Session-Token" in body, (
        "postJSON must attach the session-token header on every POST"
    )
    # and it is read from the injected hook
    assert "marathon-session-token" in app_js or "MARATHON_DECK" in app_js


def test_both_posts_flow_through_postjson(app_js: str):
    # decl-verdict and ship both go through the token-bearing postJSON, so the
    # token guard is uniform. Each named poster calls postJSON.
    code = _strip_block_comments(app_js)
    dv = _fn_body(code, "function postDeclVerdict")
    assert "postJSON(" in dv and "/api/decl-verdict" in dv
    ship = _fn_body(code, "function postShip")
    assert "postJSON(" in ship and "/api/ship" in ship


# --- binding safety: /api/ship fires ONLY from the ship-button handler ------

def test_ship_post_is_a_single_call_site(app_js: str):
    code = _strip_block_comments(app_js)
    # The ONLY fetch to /api/ship in executable code.
    posts = re.findall(r"fetch\(\s*['\"]/api/ship['\"]", code)
    direct = re.findall(r"['\"]/api/ship['\"]", code)
    # /api/ship appears exactly once as a literal (inside postShip's postJSON
    # call); it is never fetched directly elsewhere.
    assert len(direct) == 1, (
        f"expected exactly one '/api/ship' literal call site, found {len(direct)}"
    )
    # and it is not a raw fetch('/api/ship') — it goes through postJSON
    assert len(posts) == 0, "ship must route through postJSON, not a raw fetch('/api/ship')"


def test_ship_post_lives_in_postShip(app_js: str):
    code = _strip_block_comments(app_js)
    body = _fn_body(code, "function postShip")
    assert "/api/ship" in body, "the /api/ship POST must be inside postShip"


def test_postShip_only_caller_is_shipIssue(app_js: str):
    code = _strip_block_comments(app_js)
    # postShip is invoked exactly once, and only inside shipIssue.
    callers = re.findall(r"postShip\s*\(", code)
    def_count = len(re.findall(r"function\s+postShip\s*\(", code))
    invoke_count = len(callers) - def_count
    assert invoke_count >= 1, "postShip is never invoked"
    # The only invocation must be inside shipIssue.
    ship_body = _fn_body(code, "function shipIssue")
    assert "postShip(" in ship_body, "shipIssue must call postShip"
    # No OTHER function may call postShip. Scan every read/nav/verdict path.
    for fn in (
        "function init", "function refreshQueue", "function openIssue",
        "function openFirstReadyIssue", "function renderSubdeckStage",
        "function commitDeclVerdict", "function postDeclVerdict",
        "function startStatus", "function refreshStatus", "function backToQueue",
        "function renderSummaryCard", "function confirmAndShip",
    ):
        body = _fn_body(code, fn)
        assert "postShip(" not in body, (
            f"postShip must not be called from {fn} (only shipIssue may ship)"
        )


def test_ship_is_not_fired_on_decl_verdict(app_js: str):
    code = _strip_block_comments(app_js)
    # A per-decl verdict is LOCAL ONLY: committing it must not ship.
    body = _fn_body(code, "function commitDeclVerdict")
    assert "/api/ship" not in body
    assert "postShip(" not in body
    # it posts the LOCAL decl-verdict instead
    assert "postDeclVerdict(" in body


def test_ship_button_is_the_ship_entrypoint(app_js: str):
    # The summary card builds a ship button (#btn-ship) whose handler reaches
    # shipIssue via the confirm dialog. confirmAndShip is opened only from the
    # ship button; shipIssue is reached only from confirmAndShip's confirm.
    code = _strip_block_comments(app_js)
    assert "btn-ship" in code, "the completion summary must render a ship button"
    summary = _fn_body(code, "function renderSummaryCard")
    assert "confirmAndShip(" in summary, "ship button must open the confirm dialog"
    confirm = _fn_body(code, "function confirmAndShip")
    assert "shipIssue()" in confirm, "the confirm dialog fires shipIssue on confirm"


def test_decl_verdict_post_uses_post_method(app_js: str):
    code = _strip_block_comments(app_js)
    # The shared postJSON declares method POST.
    body = _fn_body(code, "function postJSON")
    assert '"POST"' in body or "'POST'" in body


def test_get_helpers_are_pure_reads(app_js: str):
    code = _strip_block_comments(app_js)
    # The GET helpers (queue/subdeck/status) must use method GET (or default
    # GET) and never POST.
    body = _fn_body(code, "function getJSON")
    assert '"GET"' in body or "'GET'" in body
    assert "POST" not in body
    # subdeck fetch is a pure read (no token, no POST)
    sd = _fn_body(code, "function fetchSubdeck")
    assert "getJSON(" in sd
    assert "POST" not in sd


def test_subdeck_load_does_not_ship(app_js: str):
    code = _strip_block_comments(app_js)
    # Opening an issue (a GET) must never ship or post a verdict.
    body = _fn_body(code, "function openIssue")
    assert "postShip(" not in body
    assert "postDeclVerdict(" not in body
    assert "/api/ship" not in body


# --- offline LaTeX fallback + source highlighting ---------------------------

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


def test_source_highlighting_is_wired(app_js: str):
    # The per-declaration source block is rendered through the offline Lean
    # highlighter into <code class="lean"> via leanHighlight.
    code = _strip_block_comments(app_js)
    assert "function leanHighlight" in code
    assert "leanHighlight(" in code
    # rendered into the decl card's source block
    card = _fn_body(code, "function renderDeclCard")
    assert "source-block" in card
    assert "leanHighlight(" in card


# --- reject requires a verbatim note ----------------------------------------

def test_reject_requires_a_note(app_js: str):
    code = _strip_block_comments(app_js)
    # The reject note modal validates a non-empty note before committing.
    body = _fn_body(code, "function openRejectNote")
    # a guard that blocks an empty note and only then commits a reject
    assert "trim()" in body
    assert 'commitDeclVerdict("reject"' in body
    # the empty-note path must NOT reach the commit (early return on empty).
    assert "return" in body


def test_reject_blocks_before_decl_verdict_when_note_empty(app_js: str):
    # Structural: in openRejectNote, the empty-note branch returns BEFORE the
    # commitDeclVerdict("reject", ...) call (so no POST fires without a note).
    code = _strip_block_comments(app_js)
    body = _fn_body(code, "function openRejectNote")
    commit_idx = body.find('commitDeclVerdict("reject"')
    assert commit_idx != -1
    before_commit = body[:commit_idx]
    # an early return guards the empty note ahead of the commit
    assert "return" in before_commit
    assert "if (!note)" in before_commit or "if(!note)" in before_commit


def test_reject_note_goes_in_the_post_body(app_js: str):
    code = _strip_block_comments(app_js)
    # postDeclVerdict attaches the note to the POST body (verbatim).
    body = _fn_body(code, "function postDeclVerdict")
    assert "note" in body
    assert "verdict" in body


def test_reject_note_is_not_rewritten(app_js: str):
    # Verbatim: the reject path passes the raw note straight through. The
    # value taken from the textarea is committed directly (only trimmed).
    code = _strip_block_comments(app_js)
    body = _fn_body(code, "function openRejectNote")
    assert re.search(r"var\s+note\s*=\s*ta\.value\.trim\(\)", body), (
        "reject note should be taken verbatim from the textarea (only trimmed)"
    )
    # no replace()/JSON-massaging of the note before commit
    pre_commit = body.split('commitDeclVerdict("reject"')[0].split("var note")[-1]
    assert ".replace(" not in pre_commit


# --- ship asks for confirm (irreversible) -----------------------------------

def test_ship_asks_for_confirm(app_js: str):
    code = _strip_block_comments(app_js)
    # ship is irreversible — there is an explicit confirm step.
    assert "function confirmAndShip" in code
    confirm = _fn_body(code, "function confirmAndShip")
    assert "irreversible" in confirm.lower()
    assert "shipIssue()" in confirm


def test_ship_label_reflects_rejected_set(app_js: str):
    # The brief: "Mark issue verified" when nothing rejected, else "Ship to
    # Aristotle" — chosen by reading summary.rejected.
    haystack = app_js
    assert "Ship to Aristotle" in haystack
    assert "Mark issue verified" in haystack
    code = _strip_block_comments(app_js)
    summary = _fn_body(code, "function renderSummaryCard")
    # the choice is gated on whether anything was rejected
    assert "rejected" in summary
    assert "anyRejected" in summary


# --- completion summary renders KEEP + FIX ----------------------------------

def test_summary_renders_kept_and_rejected(app_js: str):
    code = _strip_block_comments(app_js)
    body = _fn_body(code, "function renderSummaryCard")
    # the KEEP list (verified) and FIX list (rejected + notes) are both built
    assert "kept" in body
    assert "rejected" in body
    assert "KEEP" in body
    assert "FIX" in body
    # rejected entries surface their note
    assert ".note" in body or "r.note" in body


def test_summary_reads_subdeck_summary(app_js: str):
    code = _strip_block_comments(app_js)
    body = _fn_body(code, "function renderSummaryCard")
    # the summary is composed from the subdeck's summary block (kept/rejected/
    # deferred), with a local fallback derivation.
    assert "summary" in body
    assert "deriveSummary" in code


def test_revisit_returns_to_a_decl(app_js: str):
    # The summary offers a "revisit" path back to re-review any declaration.
    code = _strip_block_comments(app_js)
    assert "function revisitDecl" in code
    body = _fn_body(code, "function renderSummaryCard")
    assert "revisitDecl(" in body or "Revisit" in body


# --- progress + parent-issue header -----------------------------------------

def test_subdeck_header_shows_issue_and_progress(app_js: str):
    code = _strip_block_comments(app_js)
    body = _fn_body(code, "function subdeckHeader")
    # parent issue (#N + title) and a progress pill "reviewed M / N"
    assert "issue" in body
    assert "reviewed " in body
    assert "progress" in body


def test_local_only_reassurance_present(app_js: str):
    # The UI reassures that a swipe is LOCAL and does not ship yet.
    assert "LOCAL" in app_js
    # and that ship is the only irreversible action
    assert "irreversible" in app_js.lower()


# --- UI surface required by the brief ---------------------------------------

def test_keyboard_and_buttons_parity(app_js: str, index_html: str):
    # every swipe action has a keyboard equivalent AND a visible button.
    for bid in ("btn-verify", "btn-reject", "btn-defer", "btn-deps", "btn-deepdive"):
        assert bid in index_html, f"missing action button {bid}"
        assert bid in app_js, f"action button {bid} not wired in app.js"
    code = _strip_block_comments(app_js)
    assert "ArrowRight" in code and "ArrowLeft" in code
    # space / down = defer
    assert "actDefer" in code
    assert "ArrowDown" in code or '" "' in code


def test_swipe_release_back_is_noop(app_js: str):
    # Starting a drag and releasing back within the threshold does nothing.
    code = _strip_block_comments(app_js)
    body = _fn_body(code, "function attachSwipe")
    # only past-threshold commits a verdict
    assert "THRESH" in body
    assert "if (dx > THRESH)" in body
    assert "else if (dx < -THRESH)" in body


def test_llm_flag_is_present(index_html: str, app_js: str):
    # The informal rendering must be clearly flagged as LLM-rendered, gated by
    # the informal_is_llm contract field.
    haystack = index_html + app_js
    assert "LLM-rendered" in haystack
    assert "informal_is_llm" in app_js


# --- the kernel section was REMOVED from the deck card ----------------------

def test_kernel_section_removed_from_deck(app_js: str, style_css: str):
    # The "Definitions you must read" kernel section is gone from the deck card:
    # no render function, no call site, no section, no styles. (The audit CLI's
    # `marathon audit spec-card` keeps its own kernel section — a separate
    # surface untouched here.)
    code = _strip_block_comments(app_js)
    assert "function kernelList" not in code
    assert "Definitions you must read" not in app_js
    body = _fn_body(code, "function renderDeclCard")
    assert "kernelList(" not in body
    assert "sec-kernel" not in body
    for cls in (".kernel-title", ".kernel-member", ".kernel-head", ".kernel-body"):
        assert cls not in style_css, f"stale kernel style {cls}"


# --- the leading informal-approval card -------------------------------------

def test_informal_warning_gated_on_approval(app_js: str):
    # informalBlock must REPLACE the LLM-rendered warning with a verified chip
    # once the human approves the subdeck's informal text (informalApproved).
    code = _strip_block_comments(app_js)
    body = _fn_body(code, "function informalBlock")
    assert "informalApproved()" in body
    assert "informal rendering verified by you" in body
    # the approval-state helper reads the subdeck-level informal verdict
    helper = _fn_body(code, "function informalApproved")
    assert "informal" in helper and "verified" in helper


def test_approval_card_leads_the_subdeck(app_js: str):
    # renderSubdeckStage renders the informal-approval card BEFORE the decl
    # cards when there is unapproved LLM text, and it is a real render path.
    code = _strip_block_comments(app_js)
    assert "function renderApprovalCard" in code
    stage = _fn_body(code, "function renderSubdeckStage")
    assert "renderApprovalCard(" in stage
    # it leads: the approval branch returns before firstUnreviewed(cards)
    approval_at = stage.find("renderApprovalCard(")
    next_at = stage.find("firstUnreviewed(cards)")
    assert approval_at != -1 and next_at != -1 and approval_at < next_at


def test_approval_card_shows_one_top_level_statement(app_js: str):
    # The approval card shows the issue's ONE top-level informal statement
    # (inf.statement), NOT per-declaration items — Jack's review-once design.
    code = _strip_block_comments(app_js)
    body = _fn_body(code, "function renderApprovalCard")
    assert "inf.statement" in body
    assert "inf.items" not in body and ".forEach" not in body


def test_approval_card_routes_through_the_same_actions(app_js: str):
    # The SAME verify/reject/defer actions act on the approval card (so swipe +
    # keys work), branching on state.informalActive — verify=approve,
    # reject=dispute, defer=decide-later.
    code = _strip_block_comments(app_js)
    av = _fn_body(code, "function actVerify")
    ar = _fn_body(code, "function actReject")
    ad = _fn_body(code, "function actDefer")
    assert "state.informalActive" in av and "approveInformal()" in av
    assert "state.informalActive" in ar and "openInformalRejectNote()" in ar
    assert "state.informalActive" in ad and "dismissInformal()" in ad


def test_informal_warning_persists_until_approved(app_js: str):
    # The LLM-rendered warning STAYS on the per-card block until approval — the
    # else-branch (informal_is_llm but NOT approved, e.g. pending or rejected)
    # still renders it. Regression guard for accidentally suppressing it.
    code = _strip_block_comments(app_js)
    body = _fn_body(code, "function informalBlock")
    assert "LLM-rendered — verify against the source" in body
    assert "else if (card.informal_is_llm)" in body


def test_swipe_allows_the_approval_card(app_js: str):
    # The swipe onDown guard must allow the leading approval card (canActInformal),
    # not only decl cards — else approval-card swipes break silently.
    code = _strip_block_comments(app_js)
    body = _fn_body(code, "function attachSwipe")
    assert "canActInformal()" in body


def test_open_issue_resets_informal_session_flags(app_js: str):
    # A genuinely fresh issue re-offers its approval card: openIssue resets the
    # session-scoped flags so a prior "decide later" cannot leak across issues.
    code = _strip_block_comments(app_js)
    body = _fn_body(code, "function openIssue")
    assert "state.informalDismissed = false" in body
    assert "state.informalActive = false" in body


def test_informal_error_path_does_not_leave_card_active(app_js: str):
    # The commitInformalVerdict error handler must NOT leave state.informalActive
    # true (that would let a stray key/swipe re-fire the POST on the error card).
    code = _strip_block_comments(app_js)
    body = _fn_body(code, "function commitInformalVerdict")
    assert "state.informalActive = true" not in body


def test_informal_verdict_posts_under_reserved_key(app_js: str):
    # The approval verdict POSTs under the reserved "__informal__" decl name
    # (LOCAL /api/decl-verdict — never a ship), keeping it out of the per-decl
    # cards/progress/summary.
    code = _strip_block_comments(app_js)
    assert '"__informal__"' in code or "'__informal__'" in code
    commit = _fn_body(code, "function commitInformalVerdict")
    assert "postDeclVerdict(" in commit and "INFORMAL_KEY" in commit
    # it never calls postShip — the approval is not irreversible
    assert "postShip" not in commit


def test_style_has_approval_card(style_css: str):
    for cls in (".approval-card", ".approval-item", ".informal-flag-ok"):
        assert cls in style_css, f"missing style for {cls}"


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


def test_subdeck_card_tier_is_the_gate(app_js: str):
    # The card_tier (worst over decls) is shown in the subdeck header badge.
    assert "card_tier" in app_js


def test_server_binds_localhost_is_documented(index_html: str):
    # The page documents the 127.0.0.1-only + token binding-safety contract so
    # the rule is visible at the surface the human reviews.
    assert "127.0.0.1" in index_html


def test_no_eval_or_innerhtml_userdata(app_js: str):
    code = _strip_block_comments(app_js)
    # Defense-in-depth: card content is text-set via textContent, not eval'd.
    assert "eval(" not in code


def test_style_has_summary_and_subdeck_styling(style_css: str):
    # The new subdeck/summary surfaces are styled (header + progress + ship).
    for cls in (".subdeck-head", ".progress-pill", ".summary-card", ".ship-btn", ".summary-keep", ".summary-fix"):
        assert cls in style_css, f"missing style for {cls}"


# --- request 1: stale-citation display (in_code === false) ------------------

def test_decl_card_handles_in_code_false_stale_banner(app_js: str):
    # When a DeclCard reports in_code === false, the source-block section is
    # replaced by a clear "cited but NOT FOUND in the current code" warning.
    code = _strip_block_comments(app_js)
    body = _fn_body(code, "function renderDeclCard")
    assert "in_code === false" in body or "in_code===false" in body, (
        "renderDeclCard must branch on in_code === false (the stale-citation path)"
    )
    assert "NOT FOUND in the current code" in body
    # the banner explains it is likely renamed/deleted and the issue is stale
    assert "renamed or deleted" in body
    assert "stale" in body.lower()
    # it is a distinct styled element (not the generic no-source note)
    assert "stale-citation" in body


def test_source_block_still_renders_when_present(app_js: str):
    # When source_block IS present (incl. via the backend repo fallback), the
    # existing highlighted source block is rendered — the stale path is the
    # ELSE branch, not a replacement of the normal render.
    code = _strip_block_comments(app_js)
    body = _fn_body(code, "function renderDeclCard")
    assert "if (card.source_block)" in body
    assert "leanHighlight(card.source_block)" in body
    # the in_code===false branch is reached only when there is no source_block
    sb = body.find("if (card.source_block)")
    stale = body.find("in_code === false")
    assert sb != -1 and stale != -1 and sb < stale, (
        "the stale banner must be an else-branch after the source_block render"
    )


def test_style_has_stale_citation_styling(style_css: str):
    assert ".stale-citation" in style_css, "stale-citation banner must be styled"


# --- request 2: skip-API toggle (view-only) ---------------------------------

def test_subdeck_fetch_passes_skip_api(app_js: str):
    # fetchSubdeck threads the skip_api flag onto the GET query string.
    code = _strip_block_comments(app_js)
    body = _fn_body(code, "function fetchSubdeck")
    assert "skip_api" in body, "fetchSubdeck must pass skip_api on the query string"
    assert "getJSON(" in body, "fetchSubdeck remains a pure GET read"
    assert "/api/subdeck/" in body


def test_skip_api_request_sent_when_toggle_on(app_js: str):
    # When the toggle is ON, app.js requests /api/subdeck with skip_api=1; when
    # OFF, skip_api=0 — the flag is derived from state.skipApi (not hardcoded).
    code = _strip_block_comments(app_js)
    body = _fn_body(code, "function fetchSubdeck")
    assert "skip_api=1" in body
    assert "skip_api=0" in body
    # openIssue / refetchSubdeck pass the live toggle state into the fetch
    opn = _fn_body(code, "function openIssue")
    assert "state.skipApi" in opn and "fetchSubdeck(" in opn
    refetch = _fn_body(code, "function refetchSubdeck")
    assert "state.skipApi" in refetch and "fetchSubdeck(" in refetch


def test_toggle_skip_api_does_not_post(app_js: str):
    # BINDING SAFETY: flipping the toggle is a PURE VIEW change — it triggers a
    # GET refetch and NEVER a POST / verdict / ship.
    code = _strip_block_comments(app_js)
    setter = _fn_body(code, "function setSkipApi")
    assert "refetchSubdeck(" in setter, "setSkipApi must refetch (GET) the subdeck"
    for forbidden in ("postShip(", "postDeclVerdict(", "postJSON(", "/api/ship", "/api/decl-verdict"):
        assert forbidden not in setter, (
            f"toggling skip-API must not {forbidden!r} (it is a pure view change)"
        )
    # the refetch path itself only GETs (no POST primitives)
    refetch = _fn_body(code, "function refetchSubdeck")
    for forbidden in ("postShip(", "postDeclVerdict(", "postJSON(", "/api/ship"):
        assert forbidden not in refetch
    assert "fetchSubdeck(" in refetch


def test_skip_api_toggle_is_rendered_in_header(app_js: str):
    # The persistent toggle lives in the subdeck header.
    code = _strip_block_comments(app_js)
    assert "function skipApiToggle" in code
    head = _fn_body(code, "function subdeckHeader")
    assert "skipApiToggle(" in head, "the subdeck header must render the Skip-API toggle"
    # the checkbox change handler routes through setSkipApi (the GET-only flip)
    toggle = _fn_body(code, "function skipApiToggle")
    assert "setSkipApi(" in toggle
    assert "checkbox" in toggle


def test_skip_api_persists_per_session(app_js: str):
    # The toggle state is persisted per browser session (survives navigation)
    # and read back into state on load — a purely local preference.
    code = _strip_block_comments(app_js)
    assert "sessionStorage" in code
    assert "readSkipApiPref" in code and "writeSkipApiPref" in code
    # default OFF: state.skipApi is initialised from the stored pref
    assert "state.skipApi = readSkipApiPref()" in code


def test_ship_body_includes_skip_api(app_js: str):
    # POST /api/ship carries the skip_api flag so the BACKEND realizes the
    # auto-approval at ship — the single irreversible step.
    code = _strip_block_comments(app_js)
    body = _fn_body(code, "function postShip")
    assert "skip_api" in body, "postShip must include skip_api in the ship body"
    assert "/api/ship" in body
    # shipIssue passes the live toggle state into postShip
    ship = _fn_body(code, "function shipIssue")
    assert "postShip(" in ship
    assert "skipApi" in ship and "state.skipApi" in ship


def test_progress_pill_counts_main_only_under_skip_api(app_js: str):
    # Under skip-API the progress pill is over MAIN cards only (the backend
    # scopes progress); the UI marks the count as "(main)".
    code = _strip_block_comments(app_js)
    head = _fn_body(code, "function subdeckHeader")
    assert "state.skipApi" in head
    assert "(main)" in head


def test_decl_verdict_progress_recomputed_main_only_under_skip_api(app_js: str):
    # The POST /api/decl-verdict response is built WITHOUT skip_api, so its
    # progress/complete are over the WHOLE deck. Folding those full-deck
    # figures into the skip-API view would mis-count the "(main)" pill and
    # keep `complete` false forever (the unreviewed API decls live outside the
    # main `cards`), stranding the human short of the ship summary. Under
    # skip-API the frontend must recompute progress/complete LOCALLY over the
    # visible main cards rather than trust the full-deck response.
    code = _strip_block_comments(app_js)
    body = _fn_body(code, "function applyDeclResult")
    assert "state.skipApi" in body, (
        "applyDeclResult must branch on skip-API before trusting the full-deck "
        "decl-verdict progress/complete"
    )
    assert "recomputeMainProgress(" in body
    # the recompute scopes over the main cards and counts verified/rejected
    # (deferred leaves the subdeck incomplete, per the backend contract).
    recompute = _fn_body(code, "function recomputeMainProgress")
    assert "sd.cards" in recompute
    assert "verified" in recompute and "rejected" in recompute
    assert "sd.complete" in recompute and "sd.progress" in recompute
    # the recompute is purely local (no network)
    for forbidden in ("postShip(", "postDeclVerdict(", "postJSON(", "fetch("):
        assert forbidden not in recompute


# --- request 2: auto-approved panel + per-decl override ----------------------

def test_auto_approved_panel_exists(app_js: str):
    # A collapsible panel lists the API decls that will be auto-approved on
    # ship (name + api_reason), reading the backend's auto_approved list.
    code = _strip_block_comments(app_js)
    assert "function autoApprovedPanel" in code
    panel = _fn_body(code, "function autoApprovedPanel")
    assert "auto_approved" in panel, "the panel reads the auto_approved list"
    assert "api_reason" in panel, "each row shows its api_reason"
    assert "auto-approved on ship" in panel
    # it is collapsible (expand/collapse state)
    assert "apiPanelOpen" in panel


def test_auto_approved_panel_is_pure_read(app_js: str):
    # Building the panel (incl. the auto_approved LIST) must POST nothing —
    # auto-approval is realized only at ship.
    code = _strip_block_comments(app_js)
    panel = _fn_body(code, "function autoApprovedPanel")
    for forbidden in ("postShip(", "postDeclVerdict(", "postJSON(", "/api/ship", "/api/decl-verdict"):
        assert forbidden not in panel, f"the auto-approved panel must not {forbidden!r}"


def test_pull_into_review_override_exists(app_js: str):
    # The override: pull a single API decl out of auto-approval back into the
    # review deck so the human swipes it as a normal card.
    code = _strip_block_comments(app_js)
    assert "function pullIntoReview" in code
    pull = _fn_body(code, "function pullIntoReview")
    # it surfaces the decl as a normal card to swipe (revisitDecl)
    assert "revisitDecl(" in pull
    # and records the override locally (no POST)
    assert "overrideSet(" in pull or "overrides" in pull
    for forbidden in ("postShip(", "postDeclVerdict(", "postJSON(", "/api/ship", "/api/decl-verdict"):
        assert forbidden not in pull, f"pulling a decl into review must not {forbidden!r}"
    # the panel wires a pull-into-review control per auto_approved decl
    panel = _fn_body(code, "function autoApprovedPanel")
    assert "pullIntoReview(" in panel


def test_summary_marks_auto_approved_api_in_keep(app_js: str):
    # Under skip-API the confirm-and-ship summary shows the auto-approved API
    # decls in the KEEP section, clearly marked "auto-approved (API)", and
    # makes the no-individual-review consequence explicit.
    code = _strip_block_comments(app_js)
    body = _fn_body(code, "function renderSummaryCard")
    assert "autoApproved" in body
    assert "auto-approved (API)" in body
    assert "without individual review" in body
    # the auto-approved set is the backend list minus pulled-back overrides
    assert "autoApprovedForShip(" in body


def test_ship_confirm_warns_about_auto_approval(app_js: str):
    # The irreversible confirm dialog makes the auto-approval explicit when
    # skip-API would auto-approve any API decl.
    code = _strip_block_comments(app_js)
    confirm = _fn_body(code, "function confirmAndShip")
    assert "autoApprovedForShip(" in confirm
    assert "auto-approve" in confirm.lower()
    assert "without individual review" in confirm


def test_skip_api_does_not_break_ship_single_call_site(app_js: str):
    # Regression guard: the skip-API work must not introduce a second
    # /api/ship literal or a raw fetch — ship still has exactly one call site.
    code = _strip_block_comments(app_js)
    direct = re.findall(r"['\"]/api/ship['\"]", code)
    assert len(direct) == 1, (
        f"expected exactly one '/api/ship' literal, found {len(direct)}"
    )
    raw = re.findall(r"fetch\(\s*['\"]/api/ship['\"]", code)
    assert len(raw) == 0


def test_style_has_skip_api_and_panel_styling(style_css: str):
    for cls in (".skip-api-toggle", ".api-panel", ".api-row", ".api-tag", ".keep-row-api"):
        assert cls in style_css, f"missing style for {cls}"


# --- reconciliation: stale-citation FOOTNOTE (never cards) ------------------

def test_stale_citations_render_as_a_footnote_not_cards(app_js: str):
    # The reconciliation drops decls the issue body cites but that are NOT in
    # the current code into subdeck.stale_citations, shown as a small,
    # non-swipeable FOOTNOTE — never as review cards.
    code = _strip_block_comments(app_js)
    assert "function staleFootnote" in code, (
        "app.js must build a stale-citation footnote"
    )
    foot = _fn_body(code, "function staleFootnote")
    # it reads the backend's stale_citations list
    assert "stale_citations" in code
    assert "staleCitations(" in foot
    # the footnote text states the issue is stale and is NOT cards
    assert "not found in the current code" in foot.lower()
    assert "stale" in foot.lower()
    # rendered as a <footer> footnote element with the stale-footnote class
    assert "stale-footnote" in foot
    assert '"footer"' in foot or "'footer'" in foot


def test_stale_footnote_is_mounted_under_the_deck(app_js: str):
    # The footnote is appended beneath whatever card is on the stage, on every
    # render path (so the stale warning is never lost) — via mountStaleFootnote.
    code = _strip_block_comments(app_js)
    assert "function mountStaleFootnote" in code
    mount = _fn_body(code, "function mountStaleFootnote")
    assert "staleFootnote(" in mount
    assert "appendChild" in mount
    # the common under-deck mount also mounts the footnote, so every card path
    # (decl / summary / deferred / revisit) shows the reconciliation footnote.
    panel_mount = _fn_body(code, "function mountAutoApprovedPanel")
    assert "mountStaleFootnote(" in panel_mount


def test_stale_citations_are_not_turned_into_cards(app_js: str):
    # BINDING: a stale citation is advisory only — the footnote path must not
    # build a swipeable decl card or post a verdict for it.
    code = _strip_block_comments(app_js)
    foot = _fn_body(code, "function staleFootnote")
    for forbidden in ("renderDeclCard(", "postDeclVerdict(", "commitDeclVerdict(",
                      "postShip(", "postRefreshIssue(", "/api/decl-verdict"):
        assert forbidden not in foot, (
            f"the stale footnote must not {forbidden!r} (it is not a card)"
        )


def test_style_has_stale_footnote_styling(style_css: str):
    assert ".stale-footnote" in style_css, "the stale-citation footnote must be styled"


# --- reconciliation: SURFACED-from-code cards (reviewable) ------------------

def test_surfaced_decl_card_has_a_badge(app_js: str):
    # A DeclCard with surfaced===true renders a distinct badge/banner
    # ("surfaced from code — not yet in the issue") but is otherwise a normal
    # reviewable card. The wording reflects the auto-add: keeping it writes it in.
    code = _strip_block_comments(app_js)
    body = _fn_body(code, "function renderDeclCard")
    # the render branches on the surfaced flag
    assert "card.surfaced" in body, (
        "renderDeclCard must branch on the surfaced flag"
    )
    assert "surfacedBadge(" in body
    assert "surfacedBanner(" in body
    # the badge/banner say it is surfaced from code and not yet in the issue
    assert "function surfacedBadge" in code
    assert "function surfacedBanner" in code
    badge = _fn_body(code, "function surfacedBadge")
    assert "surfaced" in badge and "not yet in issue" in badge
    banner = _fn_body(code, "function surfacedBanner")
    assert "Surfaced from code" in banner and "not yet in the issue" in banner
    # the badge/banner explain the auto-add (kept → written into the issue body)
    assert "when you ship" in badge
    assert "issue body when you ship" in banner
    # the banner surfaces the backend's surfaced_reason when present
    assert "surfaced_reason" in banner


def test_surfaced_card_is_still_reviewable(app_js: str):
    # A surfaced card is a NORMAL card — it flows through renderDeclCard (the
    # same swipe/verdict path), not a special non-card branch. The surfaced
    # banner is additive chrome, not a replacement of the card body.
    code = _strip_block_comments(app_js)
    body = _fn_body(code, "function renderDeclCard")
    # the card still renders its informal/evidence sections (a full card)
    assert "informalBlock(" in body
    assert "evidenceRow(" in body
    # the surfaced banner does not short-circuit the render (no early return in
    # the surfaced branch)
    surf = body.find("card.surfaced")
    assert surf != -1
    assert "return" not in body[surf:surf + 120], (
        "the surfaced branch must not early-return — the card stays reviewable"
    )


def test_style_has_surfaced_styling(style_css: str):
    for cls in (".surfaced-badge", ".surfaced-banner"):
        assert cls in style_css, f"missing style for {cls}"


# --- push-refresh button: the ONLY /api/refresh-issue POST ------------------

def test_refresh_issue_post_is_a_single_call_site(app_js: str):
    # The push-refresh is a GitHub WRITE. It must have EXACTLY ONE
    # /api/refresh-issue literal (inside postRefreshIssue's postJSON call) and
    # never a raw fetch — mirroring the /api/ship single-call-site discipline.
    code = _strip_block_comments(app_js)
    direct = re.findall(r"['\"]/api/refresh-issue['\"]", code)
    assert len(direct) == 1, (
        f"expected exactly one '/api/refresh-issue' literal, found {len(direct)}"
    )
    raw = re.findall(r"fetch\(\s*['\"]/api/refresh-issue['\"]", code)
    assert len(raw) == 0, "refresh must route through postJSON, not a raw fetch"
    # the literal lives inside postRefreshIssue, which routes through postJSON
    body = _fn_body(code, "function postRefreshIssue")
    assert "/api/refresh-issue" in body
    assert "postJSON(" in body


def test_refresh_post_carries_the_session_token(app_js: str):
    # postRefreshIssue flows through the shared token-bearing postJSON (which
    # attaches X-Marathon-Session-Token on every POST) — so the refresh write
    # carries the per-session token like ship/decl-verdict.
    code = _strip_block_comments(app_js)
    body = _fn_body(code, "function postRefreshIssue")
    assert "postJSON(" in body, "refresh must use the token-bearing postJSON"


def test_refresh_post_only_caller_is_refreshIssue(app_js: str):
    # postRefreshIssue is invoked exactly once, and only inside the refreshIssue
    # handler (the button -> confirm -> refreshIssue path).
    code = _strip_block_comments(app_js)
    callers = re.findall(r"postRefreshIssue\s*\(", code)
    def_count = len(re.findall(r"function\s+postRefreshIssue\s*\(", code))
    invoke_count = len(callers) - def_count
    assert invoke_count == 1, (
        f"postRefreshIssue must be invoked exactly once, found {invoke_count}"
    )
    # the invocation is inside refreshIssue (header carries the open paren so it
    # is not confused with refreshIssueButton).
    handler = _fn_body(code, "function refreshIssue(")
    assert "postRefreshIssue(" in handler, "refreshIssue must call postRefreshIssue"
    # NO read/nav/open/reconcile/verdict/ship path may POST the refresh.
    for fn in (
        "function init", "function refreshQueue", "function openIssue",
        "function openFirstReadyIssue", "function renderSubdeckStage",
        "function refetchSubdeck", "function commitDeclVerdict",
        "function postDeclVerdict", "function applyDeclResult",
        "function startStatus", "function refreshStatus", "function backToQueue",
        "function shipIssue", "function setSkipApi",
        "function autoApprovedPanel", "function staleFootnote",
        "function subdeckHeader", "function refreshIssueButton",
        "function renderDeclCard",
    ):
        body = _fn_body(code, fn)
        assert "postRefreshIssue(" not in body, (
            f"postRefreshIssue must not be called from {fn} (only refreshIssue may)"
        )


def test_refresh_is_not_fired_on_subdeck_load_or_reconcile(app_js: str):
    # Opening an issue (a GET) and building the reconciled view must NEVER POST
    # the refresh. The reconciliation (footnote + surfaced) is a pure read.
    code = _strip_block_comments(app_js)
    opn = _fn_body(code, "function openIssue")
    assert "/api/refresh-issue" not in opn
    assert "postRefreshIssue(" not in opn
    refetch = _fn_body(code, "function refetchSubdeck")
    assert "postRefreshIssue(" not in refetch
    # the drift read-model helpers compute, never POST
    for fn in ("function hasReconcileDrift", "function staleCitations",
               "function reconcileSummary"):
        body = _fn_body(code, fn)
        for forbidden in ("postRefreshIssue(", "postShip(", "postDeclVerdict(",
                          "postJSON(", "fetch(", "/api/refresh-issue"):
            assert forbidden not in body, (
                f"the reconcile helper {fn} must not {forbidden!r}"
            )


def test_refresh_button_confirms_before_writing(app_js: str):
    # Clicking the refresh button asks an explicit confirm (it EDITS the GitHub
    # issue) BEFORE the write. The button handler opens confirmAndRefreshIssue;
    # the actual POST (refreshIssue) fires only from the dialog's confirm.
    code = _strip_block_comments(app_js)
    btn = _fn_body(code, "function refreshIssueButton")
    assert "confirmAndRefreshIssue(" in btn, (
        "the refresh button must open the confirm dialog, not POST directly"
    )
    # the button handler itself must NOT POST the refresh
    assert "postRefreshIssue(" not in btn
    assert "/api/refresh-issue" not in btn
    # the confirm dialog makes the GitHub write explicit and fires refreshIssue
    # only on confirm.
    confirm = _fn_body(code, "function confirmAndRefreshIssue")
    assert "EDITS the GitHub issue" in confirm or "EDITS the GitHub" in confirm
    assert "refreshIssue()" in confirm
    # opening the confirm dialog POSTs nothing.
    assert "postRefreshIssue(" not in confirm
    assert "/api/refresh-issue" not in confirm


def test_refresh_button_is_drift_gated(app_js: str):
    # The button is ENABLED only when the reconciliation found drift (stale
    # citations and/or surfaced decls). On a non-drifting issue it is disabled
    # (a refresh would be a no-op write).
    code = _strip_block_comments(app_js)
    btn = _fn_body(code, "function refreshIssueButton")
    assert "hasReconcileDrift(" in btn
    assert "disabled" in btn, "the refresh button must toggle its disabled state"
    # drift is derived from the stale citations and the reconcile summary.
    drift = _fn_body(code, "function hasReconcileDrift")
    assert "staleCitations(" in drift
    assert "reconcileSummary(" in drift or "surfaced" in drift


def test_refresh_button_is_in_the_subdeck_header(app_js: str):
    # The push-refresh button lives in the subdeck header, alongside the
    # skip-API toggle.
    code = _strip_block_comments(app_js)
    head = _fn_body(code, "function subdeckHeader")
    assert "refreshIssueButton(" in head
    assert "function refreshIssueButton" in code
    # it carries a stable id for the action surface.
    btn = _fn_body(code, "function refreshIssueButton")
    assert "btn-refresh-issue" in btn


def test_refresh_success_refetches_the_subdeck(app_js: str):
    # On success the handler re-fetches the subdeck (a PURE READ) so the
    # reconciled view + the button's drift state reflect the now-current body,
    # and reports what changed (dropped/added + the SHA).
    code = _strip_block_comments(app_js)
    handler = _fn_body(code, "function refreshIssue(")
    assert "refetchSubdeck(" in handler, (
        "a successful refresh must re-fetch the subdeck (a GET) to re-reconcile"
    )
    assert "dropped" in handler and "added" in handler
    assert "sha" in handler.lower()


def test_refresh_does_not_break_ship_single_call_site(app_js: str):
    # Regression guard: the refresh work must not introduce a second /api/ship
    # literal or a raw fetch — ship still has exactly one call site.
    code = _strip_block_comments(app_js)
    direct = re.findall(r"['\"]/api/ship['\"]", code)
    assert len(direct) == 1
    raw = re.findall(r"fetch\(\s*['\"]/api/ship['\"]", code)
    assert len(raw) == 0


def test_style_has_refresh_button_styling(style_css: str):
    assert ".refresh-issue-btn" in style_css, "the push-refresh button must be styled"


# --- dashboard: tabbed shell (Status / Issues / History + Review) -----------

def test_index_has_tab_shell(index_html: str):
    assert 'class="tab-nav"' in index_html
    for t in ("review", "status", "issues", "history"):
        assert 'data-tab="' + t + '"' in index_html
        assert 'id="tab-' + t + '"' in index_html
    for h in ("dash-status", "dash-issues", "dash-history"):
        assert 'id="' + h + '"' in index_html


def test_action_bar_lives_inside_the_review_pane(index_html: str):
    # The action bar + card stage must be INSIDE #tab-review so they hide with
    # the tab on the dashboard views (display:none on the pane hides them).
    i_open = index_html.find('id="tab-review"')
    i_action = index_html.find('class="action-bar"')
    i_close = index_html.find('/#tab-review')
    assert -1 < i_open < i_action < i_close


def test_app_has_tab_render_functions(app_js: str):
    code = _strip_block_comments(app_js)
    for fn in ("function switchTab", "function renderStatusTab",
               "function renderIssuesTab", "function renderHistoryTab"):
        assert fn in code


def test_dashboard_views_are_pure_reads(app_js: str):
    # The dashboard fetches/render and tab switching must NEVER POST.
    code = _strip_block_comments(app_js)
    for fn in ("function renderStatusTab", "function renderIssuesTab",
               "function renderHistoryTab", "function switchTab"):
        body = _fn_body(code, fn)
        assert "postJSON" not in body and "postShip" not in body
    for fn in ("function renderStatusTab", "function renderIssuesTab",
               "function renderHistoryTab"):
        assert "getJSON(" in _fn_body(code, fn)


def test_card_actions_gated_to_review_tab(app_js: str):
    code = _strip_block_comments(app_js)
    keys = _fn_body(code, "function attachKeys")
    assert 'state.activeTab !== "review"' in keys
    assert 'switchTab("status")' in keys           # 1-4 tab shortcuts
    swipe = _fn_body(code, "function attachSwipe")
    assert 'state.activeTab !== "review"' in swipe  # swipe is Review-only


def test_dashboard_endpoints_referenced(app_js: str):
    code = _strip_block_comments(app_js)
    assert "/api/conductor-status" in code
    assert "/api/issues-overview" in code
    assert "/api/history" in code


def test_style_has_tab_and_dashboard_classes(style_css: str):
    for cls in (".tab-nav", ".tab-btn", ".cond-banner", ".issue-row",
                ".hist-row", ".sb-verified"):
        assert cls in style_css


# --- "flag missing declaration" (ADD-to-Aristotle for an incomplete issue) ---

def test_summary_has_flag_missing_section(app_js: str):
    code = _strip_block_comments(app_js)
    body = _fn_body(code, "function renderSummaryCard")
    assert "summary-add" in body
    assert "openMissingNote" in body
    # a flagged-missing issue routes the ship to Aristotle even with no rejects
    assert "anyAristotle" in body
    assert "sd.missing" in body or "state.subdeck.missing" in body


def test_missing_note_is_a_local_post(app_js: str):
    code = _strip_block_comments(app_js)
    assert "/api/missing" in code
    # postMissing flows through postJSON (token-bearing) like every POST
    pm = _fn_body(code, "function postMissing")
    assert "postJSON(" in pm
    # saving the note never ships
    save = _fn_body(code, "function saveMissing")
    assert "postMissing(" in save and "postShip" not in save


def test_missing_endpoint_is_a_post_route(index_html: str, app_js: str):
    # the flag-missing modal exists and is reachable from the summary
    code = _strip_block_comments(app_js)
    assert "function openMissingNote" in code
    assert "Flag a missing declaration" in app_js


# --- flag-missing "already exists" suggestions (advisory, async) -------------

def test_missing_suggestions_fetch_defined(app_js: str):
    code = _strip_block_comments(app_js)
    assert "function fetchMissingSuggestions" in code
    assert "/api/missing-suggestions/" in code
    # flows through getJSON (a pure GET), never postJSON
    body = _fn_body(code, "function fetchMissingSuggestions")
    assert "getJSON(" in body and "postJSON" not in body


def test_openMissingNote_async_fill_is_advisory(app_js: str):
    code = _strip_block_comments(app_js)
    body = _fn_body(code, "function openMissingNote")
    # a container is inserted synchronously, then filled after the fetch
    assert "missing-suggest" in body
    assert "fetchMissingSuggestions(" in body
    # advisory-only: a fetch failure must NOT block the modal (.catch present)
    assert ".catch(" in body
    # Save still records the note locally (never disabled by suggestions)
    assert "saveMissing(" in body
    # ORDERING: the modal is built + shown SYNCHRONOUSLY before the GET fires,
    # so a slow/failed suggestions fetch can never block opening the modal.
    assert body.index("openModal(") < body.index("fetchMissingSuggestions(")
    # the .catch guard is chained off the suggestions fetch (after it).
    assert ".catch(" in body[body.index("fetchMissingSuggestions("):]


def test_dismiss_suggestion_does_not_save(app_js: str):
    """The 'not missing — one of these is it' path closes the modal WITHOUT
    saving a missing note (so no Aristotle ADD is later dispatched)."""
    code = _strip_block_comments(app_js)
    body = _fn_body(code, "function renderMissingSuggestions")
    assert "closeModal()" in body
    assert "saveMissing" not in body and "postMissing" not in body
