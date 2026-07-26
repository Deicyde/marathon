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
* when every declaration is reviewed — or the first rejection pauses a partial
  review — a confirm-and-ship SUMMARY card appears; SHIP (POST /api/ship)
  composes the per-issue outcome and is the ONLY irreversible action.

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

No network and no browser. Most checks are pure structural assertions; when a
Node runtime is available, one small offline subprocess executes the exported
pure graph-layout helper against dependency and cycle fixtures.
"""

from __future__ import annotations

import json
import re
import shutil
import subprocess
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


def _run_graph_layout(app_js: str, nodes: list[dict], edges: list[dict]) -> dict:
    """Execute the DOM-free graph layout in Node when available."""
    node = shutil.which("node")
    if node is None:
        pytest.skip("Node runtime unavailable for graph-layout semantic test")
    code = _strip_block_comments(app_js)
    helpers = "\n".join((
        _fn_body(code, "function graphIsDependencyEdge"),
        _fn_body(code, "function graphLayout"),
    ))
    script = (
        helpers + "\n" +
        "const result = graphLayout(" + json.dumps(nodes) + "," +
        json.dumps(edges) + ");\n" +
        "process.stdout.write(JSON.stringify(result));\n"
    )
    proc = subprocess.run(
        [node, "-e", script], capture_output=True, text=True, timeout=10,
        check=False,
    )
    assert proc.returncode == 0, proc.stderr
    return json.loads(proc.stdout)


def _run_ship_status_transport(app_js: str) -> dict:
    """Execute the DOM-free bounded GET + confirmed-404 classifier in Node."""
    node = shutil.which("node")
    if node is None:
        pytest.skip("Node runtime unavailable for ship-status transport test")
    code = _strip_block_comments(app_js)
    helpers = "\n".join((
        _fn_body(code, "function getJSONWithTimeout"),
        _fn_body(code, "function confirmedShipStatusNotFound"),
    ))
    script = helpers + r"""
(async function () {
  global.fetch = function () {
    return Promise.resolve({
      ok: false,
      status: 404,
      text: function () {
        return Promise.resolve(JSON.stringify({
          ok: false,
          error: "no matching ship job for issue #171"
        }));
      }
    });
  };
  var missing;
  try {
    await getJSONWithTimeout("/api/ship-status?issue=171", 100);
  } catch (err) {
    missing = {
      status: err.status,
      confirmed: confirmedShipStatusNotFound(err)
    };
  }

  global.fetch = function () { return new Promise(function () {}); };
  var timeoutName = null;
  try {
    await getJSONWithTimeout("/api/ship-status?issue=171", 10);
  } catch (err) {
    timeoutName = err.name;
  }
  process.stdout.write(JSON.stringify({
    missing: missing,
    timeoutName: timeoutName
  }));
})().catch(function (err) {
  process.stderr.write(String(err && err.stack || err));
  process.exit(1);
});
"""
    proc = subprocess.run(
        [node, "-e", script],
        capture_output=True,
        text=True,
        timeout=2,
        check=False,
    )
    assert proc.returncode == 0, proc.stderr
    return json.loads(proc.stdout)


def _run_queue_refresh_race(app_js: str) -> dict:
    """Execute the queue coordinator with two overlapping retries in Node."""
    node = shutil.which("node")
    if node is None:
        pytest.skip("Node runtime unavailable for queue race semantic test")
    code = _strip_block_comments(app_js)
    helpers = "\n".join((
        _fn_body(code, "function withReadDeadline"),
        _fn_body(code, "function newReadController"),
        _fn_body(code, "function readOptions"),
        _fn_body(code, "function refreshQueue"),
        _fn_body(code, "function startQueueRefresh"),
    ))
    script = "var QUEUE_READ_TIMEOUT_MS = 100;\n" + helpers + r"""
function deferred() {
  var resolve, reject;
  var promise = new Promise(function (yes, no) {
    resolve = yes; reject = no;
  });
  return { promise: promise, resolve: resolve, reject: reject };
}
var queueReads = [];
var stuckReads = [];
var renders = 0;
var ended = 0;
var abortedReads = 0;
var host = {};
var state = {
  queue: [], building: 0, viewBuilding: 0, landedToday: 0,
  unresolved: 0, awaitingRefinement: 0, needsAttention: 0,
  needsCitationMapping: 0, awaitingAudit: 0, dependencyBlocked: 0,
  otherUnresolved: 0, reviewStuck: { issues: [], repo: null },
  queueRequest: null, queueAbortController: null,
  queueRequestSeq: 0, queueSnapshotKey: null,
  issue: null, activeTab: "review"
};
function stage() { return host; }
function setActionsEnabled() {}
function beginSurfaceRefresh() { return true; }
function surfaceHasStableContent() { return true; }
function endSurfaceRefresh() { ended += 1; }
function applyStatus() {}
function flashToast(message) { throw new Error(message); }
function showErrorCard(title, err) { throw err || new Error(title); }
function openFirstReadyIssue() { renders += 1; }
function abortableRead(options, bucket) {
  var read = deferred();
  bucket.push(read);
  var signal = options && options.signal;
  if (signal) {
    var abort = function () {
      abortedReads += 1;
      var err = new Error("aborted");
      err.name = "AbortError";
      read.reject(err);
    };
    if (signal.aborted) abort();
    else signal.addEventListener("abort", abort, { once: true });
  }
  return read.promise;
}
function fetchQueue(chapter, options) {
  return abortableRead(options, queueReads);
}
function getJSON(url, options) {
  if (url !== "/api/stuck") throw new Error("unexpected URL " + url);
  return abortableRead(options, stuckReads);
}
(async function () {
  var first = refreshQueue();
  var second = refreshQueue();
  if (queueReads.length !== 2 || stuckReads.length !== 2) {
    throw new Error("retry did not supersede the hung read immediately");
  }
  queueReads[1].resolve({
    cards: [{ id: 2, ready: false, marker: "new" }],
    unresolved: 1
  });
  stuckReads[1].resolve({ issues: [], repo: "new/repo" });
  await Promise.all([first, second]);
  process.stdout.write(JSON.stringify({
    queueReads: queueReads.length,
    stuckReads: stuckReads.length,
    renders: renders,
    installedMarker: state.queue[0] && state.queue[0].marker,
    stuckCount: state.reviewStuck.issues.length,
    requestCleared: state.queueRequest === null,
    ended: ended,
    abortedReads: abortedReads
  }));
})().catch(function (err) {
  process.stderr.write(String(err && err.stack || err));
  process.exit(1);
});
"""
    proc = subprocess.run(
        [node, "-e", script],
        capture_output=True,
        text=True,
        timeout=2,
        check=False,
    )
    assert proc.returncode == 0, proc.stderr
    return json.loads(proc.stdout)


def _run_definition_terms_race(app_js: str) -> dict:
    """Execute a source-token change while the prior term read is pending."""
    node = shutil.which("node")
    if node is None:
        pytest.skip("Node runtime unavailable for definition race semantic test")
    code = _strip_block_comments(app_js)
    refresh = _fn_body(code, "function refreshDefinitionTerms")
    script = refresh + r"""
function deferred() {
  var resolve, reject;
  var promise = new Promise(function (yes, no) {
    resolve = yes; reject = no;
  });
  return { promise: promise, resolve: resolve, reject: reject };
}
var state = {
  definitionTerms: { names: [], short: {} },
  definitionSourceToken: null,
  definitionTermsRequest: null,
  definitionTermsRequestToken: null,
  definitionTermsQueued: null,
  definitionTermsRequestSeq: 0
};
var reads = [];
var installs = [];
function fetchDefinitionTerms() {
  var read = deferred();
  reads.push(read);
  return read.promise;
}
function installDefinitionTerms(terms) {
  installs.push(terms && terms.source_token);
}
(async function () {
  var first = refreshDefinitionTerms("token-a", false);
  var coalesced = refreshDefinitionTerms("token-b", false);
  var reverted = refreshDefinitionTerms("token-a", false);
  if (first !== coalesced || first !== reverted || reads.length !== 1) {
    throw new Error("overlap was not coalesced into one active read");
  }
  reads[0].resolve({ source_token: "token-a", names: ["First"] });
  await first;
  await new Promise(setImmediate);
  if (reads.length !== 2) {
    throw new Error("new source token did not schedule one follow-up");
  }
  var followup = state.definitionTermsRequest;
  reads[1].resolve({ source_token: "token-a", names: ["Latest"] });
  await followup;
  process.stdout.write(JSON.stringify({
    reads: reads.length,
    installs: installs,
    requestCleared: state.definitionTermsRequest === null,
    sequence: state.definitionTermsRequestSeq
  }));
})().catch(function (err) {
  process.stderr.write(String(err && err.stack || err));
  process.exit(1);
});
"""
    proc = subprocess.run(
        [node, "-e", script],
        capture_output=True,
        text=True,
        timeout=2,
        check=False,
    )
    assert proc.returncode == 0, proc.stderr
    return json.loads(proc.stdout)


def _run_issues_refresh_race(app_js: str) -> dict:
    """Execute two Issues retries and verify the aborted snapshot stays inert."""
    node = shutil.which("node")
    if node is None:
        pytest.skip("Node runtime unavailable for Issues race semantic test")
    code = _strip_block_comments(app_js)
    helpers = "\n".join((
        _fn_body(code, "function withReadDeadline"),
        _fn_body(code, "function newReadController"),
        _fn_body(code, "function readOptions"),
        _fn_body(code, "function renderIssuesTab"),
    ))
    script = "var ISSUES_READ_TIMEOUT_MS = 100;\n" + helpers + r"""
function deferred() {
  var resolve, reject;
  var promise = new Promise(function (yes, no) {
    resolve = yes; reject = no;
  });
  return { promise: promise, resolve: resolve, reject: reject };
}
var overviewReads = [];
var stuckReads = [];
var abortedReads = 0;
var stuckRenders = 0;
var ended = 0;
var renderedStuck = null;
var host = {
  innerHTML: "mounted",
  appendChild: function () {}
};
var state = {
  activeTab: "issues",
  issuesRequest: null,
  issuesAbortController: null,
  issuesRequestSeq: 0,
  issuesSnapshotKey: null,
  issuesStuck: { issues: [], repo: null }
};
function dashHost() { return host; }
function beginSurfaceRefresh() { return true; }
function endSurfaceRefresh() { ended += 1; }
function surfaceHasStableContent() { return true; }
function flashToast(message) { throw new Error(message); }
function dashError(id, err) { throw err || new Error(id); }
function el() { return {}; }
function renderStuckSection(target, repo, snapshot) {
  stuckRenders += 1;
  renderedStuck = snapshot;
}
function abortableRead(options, bucket) {
  var read = deferred();
  bucket.push(read);
  var signal = options && options.signal;
  if (signal) {
    var abort = function () {
      abortedReads += 1;
      var err = new Error("aborted");
      err.name = "AbortError";
      read.reject(err);
    };
    if (signal.aborted) abort();
    else signal.addEventListener("abort", abort, { once: true });
  }
  return read.promise;
}
function getJSON(url, options) {
  if (url === "/api/issues-overview") {
    return abortableRead(options, overviewReads);
  }
  if (url === "/api/stuck") {
    return abortableRead(options, stuckReads);
  }
  throw new Error("unexpected URL " + url);
}
(async function () {
  var first = renderIssuesTab();
  var second = renderIssuesTab();
  if (overviewReads.length !== 2 || stuckReads.length !== 2) {
    throw new Error("retry did not supersede the pending Issues read");
  }
  overviewReads[1].resolve({
    repo: "new/repo", counts: {}, chapters: []
  });
  stuckReads[1].resolve({
    repo: "new/repo", issues: [{ num: 240 }]
  });
  await Promise.all([first, second]);
  process.stdout.write(JSON.stringify({
    overviewReads: overviewReads.length,
    stuckReads: stuckReads.length,
    abortedReads: abortedReads,
    stuckRenders: stuckRenders,
    installedIssue: renderedStuck && renderedStuck.issues[0].num,
    requestCleared: state.issuesRequest === null,
    ended: ended
  }));
})().catch(function (err) {
  process.stderr.write(String(err && err.stack || err));
  process.exit(1);
});
"""
    proc = subprocess.run(
        [node, "-e", script],
        capture_output=True,
        text=True,
        timeout=2,
        check=False,
    )
    assert proc.returncode == 0, proc.stderr
    return json.loads(proc.stdout)


def _run_status_refresh_race(app_js: str) -> dict:
    """Execute poll coalescing plus a forced post-mutation Status refresh."""
    node = shutil.which("node")
    if node is None:
        pytest.skip("Node runtime unavailable for Status race semantic test")
    code = _strip_block_comments(app_js)
    helpers = "\n".join((
        _fn_body(code, "function withReadDeadline"),
        _fn_body(code, "function newReadController"),
        _fn_body(code, "function readOptions"),
        _fn_body(code, "function renderStatusTab"),
    ))
    script = "var STATUS_READ_TIMEOUT_MS = 100;\n" + helpers + r"""
function deferred() {
  var resolve, reject;
  var promise = new Promise(function (yes, no) {
    resolve = yes; reject = no;
  });
  return { promise: promise, resolve: resolve, reject: reject };
}
var reads = [];
var abortedReads = 0;
var ended = 0;
var clears = 0;
var heartbeat = { textContent: "" };
var host = {
  querySelector: function (selector) {
    return selector === ".cond-updated" ? heartbeat : null;
  }
};
var newest = {
  alive: true,
  running: 0,
  jobs: [],
  queued: [],
  landings: [],
  updated_ts: "new-heartbeat"
};
var stable = {};
Object.keys(newest).forEach(function (key) {
  if (key !== "updated_ts") stable[key] = newest[key];
});
var state = {
  activeTab: "status",
  conductorStartBusy: false,
  conductorStartError: "",
  statusDashboardRequest: null,
  statusDashboardAbortController: null,
  statusDashboardRequestSeq: 0,
  statusDashboardSnapshotKey: JSON.stringify([stable, false, ""])
};
function dashHost() { return host; }
function beginSurfaceRefresh() {}
function endSurfaceRefresh() { ended += 1; }
function surfaceHasStableContent() { return true; }
function clearStatusRefreshError() { clears += 1; }
function showStatusRefreshError(target, err) { throw err; }
function fmtTs(value) { return value; }
function getJSON(url, options) {
  if (url !== "/api/conductor-status") throw new Error("unexpected URL " + url);
  var read = deferred();
  reads.push(read);
  var signal = options && options.signal;
  if (signal) {
    var abort = function () {
      abortedReads += 1;
      var err = new Error("aborted");
      err.name = "AbortError";
      read.reject(err);
    };
    if (signal.aborted) abort();
    else signal.addEventListener("abort", abort, { once: true });
  }
  return read.promise;
}
(async function () {
  var first = renderStatusTab();
  var coalesced = renderStatusTab();
  if (first !== coalesced || reads.length !== 1) {
    throw new Error("ordinary poll did not coalesce");
  }
  var forced = renderStatusTab(true);
  if (reads.length !== 2) {
    throw new Error("forced refresh did not supersede the pending poll");
  }
  reads[1].resolve(newest);
  await Promise.all([first, forced]);
  process.stdout.write(JSON.stringify({
    reads: reads.length,
    abortedReads: abortedReads,
    heartbeat: heartbeat.textContent,
    clears: clears,
    requestCleared: state.statusDashboardRequest === null,
    ended: ended
  }));
})().catch(function (err) {
  process.stderr.write(String(err && err.stack || err));
  process.exit(1);
});
"""
    proc = subprocess.run(
        [node, "-e", script],
        capture_output=True,
        text=True,
        timeout=2,
        check=False,
    )
    assert proc.returncode == 0, proc.stderr
    return json.loads(proc.stdout)


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


def test_ambiguous_ship_post_reconciles_with_pure_issue_status_get(app_js: str):
    """A lost POST response is not evidence that the irreversible write failed."""
    code = _strip_block_comments(app_js)
    ship = _fn_body(code, "function shipIssue")
    reconcile = _fn_body(code, "function reconcileAmbiguousShip")
    context = _fn_body(code, "function normalizeShipContext")

    assert "newShipRequestId()" in ship
    assert "requestId: requestId" in ship
    assert "reconcileAmbiguousShip(issue, requestId, err)" in ship
    assert "Nothing will be submitted again" not in ship
    assert "issueShipStatusUrl(issue, requestId)" in reconcile
    assert "getJSONWithTimeout(" in reconcile
    assert "confirmedShipStatusNotFound" in reconcile
    assert "safe to press Ship again" in reconcile
    assert "Ship remains disabled" in reconcile
    assert "requestId" in context
    # Reconciliation is a pure read. It must never silently retry the POST.
    assert "postShip(" not in reconcile
    assert "postJSON(" not in reconcile


def test_known_ship_4xx_never_enters_lost_response_reconciliation(app_js: str):
    code = _strip_block_comments(app_js)
    ship = _fn_body(code, "function shipIssue")
    known = _fn_body(code, "function definiteShipPostRejection")

    assert "err.status >= 400" in known
    assert "err.status < 500" in known
    rejection = ship.index("if (definiteShipPostRejection(err))")
    reconcile = ship.index("reconcileAmbiguousShip(issue, requestId, err)")
    assert rejection < reconcile
    between = ship[rejection:reconcile]
    assert "clearShipContext(provisionalContext)" in between
    assert '"Ship was not accepted"' in between
    assert "return;" in between


def test_ship_status_poll_has_a_finite_fetch_timeout(app_js: str):
    code = _strip_block_comments(app_js)
    bounded_get = _fn_body(code, "function getJSONWithTimeout")
    poll = _fn_body(code, "function pollShipJob")

    assert "setTimeout" in bounded_get
    assert "AbortController" in bounded_get
    assert "timeoutMs" in bounded_get
    assert "getJSONWithTimeout(ctx.statusUrl, SHIP_STATUS_TIMEOUT_MS)" in poll


def test_ship_status_transport_distinguishes_confirmed_404_from_timeout(
    app_js: str,
):
    result = _run_ship_status_transport(app_js)
    assert result["missing"] == {"status": 404, "confirmed": True}
    assert result["timeoutName"] == "TimeoutError"


def test_ship_requires_an_explicit_known_outcome_before_success(app_js: str):
    code = _strip_block_comments(app_js)
    confirmed = _fn_body(code, "function isConfirmedShipResult")
    ship = _fn_body(code, "function shipIssue")

    assert '"shipped"' in confirmed and '"verified"' in confirmed
    assert "isConfirmedShipResult(resp)" in ship
    assert "ship returned an unrecognized response" in ship


def test_ship_terminal_handlers_do_not_clobber_a_newer_issue(app_js: str):
    code = _strip_block_comments(app_js)
    success = _fn_body(code, "function finishShipSuccess")
    failure = _fn_body(code, "function restoreShipSummary")

    guard = success.index("if (state.issue !== issue) return")
    destructive_clear = success.index("state.subdeck = null")
    assert guard < destructive_clear
    assert "if (state.issue !== issue)" in failure
    assert "showMessageCard" not in failure[:failure.index("if (!state.subdeck)")]


def test_active_ship_context_survives_reload_without_reposting(app_js: str):
    code = _strip_block_comments(app_js)
    persist = _fn_body(code, "function persistShipContext")
    clear = _fn_body(code, "function clearShipContext")
    resume = _fn_body(code, "function resumeSavedShipContext")
    init = _fn_body(code, "function init")

    assert "sessionStorage.setItem" in persist
    assert "sessionStorage.removeItem" in clear
    assert "readShipContext()" in init
    assert "resumeSavedShipContext(savedShip)" in init
    assert "resumeShipStatusPoll(ctx)" in resume
    assert "postShip(" not in resume and "postJSON(" not in resume


def test_ship_spinner_stops_for_reduced_motion(style_css: str):
    reduced_blocks = re.findall(
        r"@media\s*\(prefers-reduced-motion:\s*reduce\)\s*\{(.*?)\n\}",
        style_css,
        flags=re.DOTALL,
    )
    assert any(
        ".ship-spinner" in block and "animation: none" in block
        for block in reduced_blocks
    )


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


# --- Verso semantic source (safe progressive enhancement) ------------------

def test_verso_semantic_endpoints_are_pure_reads(app_js: str):
    code = _strip_block_comments(app_js)
    status = _fn_body(code, "function fetchVersoStatus")
    manifest = _fn_body(code, "function fetchVersoManifest")
    assert 'getJSON("/api/verso-status")' in status
    assert 'getJSON("/api/verso-manifest")' in manifest
    assert "postJSON(" not in status + manifest


def test_failed_verso_build_exposes_an_explicit_guarded_retry(app_js: str):
    code = _strip_block_comments(app_js)
    transport = _fn_body(code, "function retryVersoBuild")
    assert 'postJSON("/api/verso-retry", {})' in transport
    button = _fn_body(code, "function versoRetryButton")
    assert 'stateName !== "error"' in button
    assert "retryVersoBuildFrom(this)" in button
    retry = _fn_body(code, "function retryVersoBuildFrom")
    assert "versoRetryBusy" in retry
    assert "versoMetadataEpoch += 1" in retry
    assert "ensureVersoMetadata(true)" in retry
    assert "scheduleVersoStatusPoll(metadata.status)" in retry


def test_verso_is_a_progressive_layer_over_immediate_raw_source(app_js: str):
    code = _strip_block_comments(app_js)
    card = _fn_body(code, "function renderDeclCard")
    assert "leanHighlight(card.source_block, card.kernel)" in card
    assert "makeVersoShell(rawCode" in card
    assert "enhanceVersoDeclaration(semanticSource" in card

    reader = _fn_body(code, "function renderReaderTarget")
    assert "leanHighlight(target.source_block" in reader
    assert "makeVersoShell(rawSource" in reader
    assert "enhanceVersoDeclaration(semanticSource" in reader

    full_file = _fn_body(code, "function readerFullFile")
    assert "fetchReviewSource(path)" in full_file
    assert "makeVersoShell(rawSource" in full_file
    assert "enhanceVersoModule(semanticFile, path)" in full_file


def test_verso_generated_html_passes_through_strict_sanitizer(app_js: str):
    code = _strip_block_comments(app_js)
    sanitizer = _fn_body(code, "function sanitizeVersoNode")
    html = _fn_body(code, "function sanitizeVersoHTML")
    decl = _fn_body(code, "function declarationCodeFromPage")
    module = _fn_body(code, "function moduleCodeFromPage")
    hover = _fn_body(code, "function makeVersoHover")

    assert "VERSO_ALLOWED_TAGS" in sanitizer
    assert "VERSO_DROP_TAGS" in sanitizer
    assert "safeVersoURL" in sanitizer
    assert "createElement" in sanitizer and "createTextNode" in sanitizer
    assert "DOMParser" in html and "sanitizeVersoNode" in html
    assert "sanitizeVersoNode" in decl and "sanitizeVersoNode" in module
    assert 'querySelector("section.code-content")' in module
    assert "content.childNodes" in module
    for prose_tag in ("H1", "H2", "H3", "BLOCKQUOTE"):
        assert prose_tag in app_js
    assert "sanitizeVersoHTML(" in hover and "docs[hoverId]" in hover
    assert "idPrefix" in sanitizer and "idPrefix" in hover
    for body in (sanitizer, html, decl, module, hover):
        assert "innerHTML" not in body, "generated or hover HTML must never be injected raw"
    for dangerous in ("SCRIPT", "STYLE", "IFRAME", "OBJECT", "SVG", "FORM"):
        assert dangerous in app_js


def test_verso_declaration_mounts_only_its_immediately_preceding_prose(app_js: str):
    code = _strip_block_comments(app_js)
    decl = _fn_body(code, "function declarationCodeFromPage")
    sanitizer = _fn_body(code, "function sanitizeVersoNode")

    # Declaration doc-comments are an exact sibling association. In
    # particular, never scan backwards and accidentally borrow a module doc or
    # the previous declaration/field's prose.
    assert "code.previousElementSibling" in decl
    assert "previousSibling" not in decl
    assert 'classList.contains("md-text")' in decl
    assert 'classList.contains("verso-text")' in decl
    assert '!prose.classList.contains("mod-doc")' in decl
    assert decl.index("fragment.appendChild(proseContainer)") < decl.index(
        "fragment.appendChild(cleanCode)"
    )
    assert 'classList.add("verso-declaration-doc")' in decl

    # verso-html's nonstandard emphasis element is normalized while it is
    # still inside the sanitizer; it is never admitted as an arbitrary tag.
    assert 'sourceTag === "EMPH" ? "EM" : sourceTag' in sanitizer
    assert "VERSO_ALLOWED_TAGS[tag]" in sanitizer


def test_verso_prose_math_is_dom_preserving_bounded_and_offline_safe(
    app_js: str, katex_js: str,
):
    code = _strip_block_comments(app_js)
    paint = _fn_body(code, "function paintVersoResult")
    assert "semantic.appendChild(clean)" in paint
    assert "MarathonLatex.renderVersoMath(semantic)" in paint
    assert paint.index("semantic.appendChild(clean)") < paint.index(
        "MarathonLatex.renderVersoMath(semantic)"
    ) < paint.index('semantic.removeAttribute("hidden")')
    assert paint.index(
        'semantic.removeAttribute("data-marathon-verso-math-rendered")'
    ) < paint.index("semantic.removeChild")

    katex_code = _strip_block_comments(katex_js)
    render = _fn_body(katex_code, "function renderVersoMath")
    collect = _fn_body(katex_code, "function collectVersoMathText")
    replace = _fn_body(katex_code, "function replaceVersoDelimitedMath")
    math = _fn_body(katex_code, "function renderMath")
    assert 'querySelectorAll("code.math.inline, code.math.display")' in render
    assert 'querySelectorAll(".md-text, .verso-text")' in render
    for literal_parent in ('tag === "CODE"', 'tag === "PRE"', 'tag === "A"'):
        assert literal_parent in collect
    assert "MAX_VERSO_PROSE_CHARS - budget.chars" in collect
    assert "MAX_VERSO_MATH_NODES" in render
    assert "MAX_VERSO_TEX_CHARS" in katex_code
    per_expression = re.search(r"MAX_LATEX_SOURCE\s*=\s*(\d+)", katex_code)
    total_mount = re.search(r"MAX_VERSO_TEX_CHARS\s*=\s*(\d+)", katex_code)
    assert per_expression and int(per_expression.group(1)) <= 4096
    assert total_mount and int(total_mount.group(1)) <= 24000
    assert "var typesetBudget = { texChars: 0 }" in render
    assert "replaceVersoMathCode(explicit[i], typesetBudget)" in render
    assert "MAX_VERSO_MATH_NODES - renderedCount, typesetBudget" in render
    assert "reserveTypesetBudget(typesetBudget, latex.length)" in math
    assert "tokenize(node.nodeValue" in replace  # raw $...$ and $$...$$ prose
    assert "createDocumentFragment" in replace and "replaceChild" in replace
    assert "textContent" not in render  # never flatten the mounted prose tree
    for option in ("trust: false", "maxExpand: 1000", "maxSize: 20"):
        assert option in math
    assert "latex-fallback" in math and "textContent" in math
    assert "renderVersoMath: renderVersoMath" in katex_js


def test_verso_raw_math_parser_is_escape_aware_and_conservative(katex_js: str):
    code = _strip_block_comments(katex_js)
    tokenize = _fn_body(code, "function tokenize")
    escaped = _fn_body(code, "function dollarIsEscaped")

    # Odd backslash parity protects surviving Markdown/TeX escapes. Inline
    # delimiters follow TeX/Markdown whitespace conventions, and an invalid
    # opening marker is consumed alone so a later ordinary $x$ can still win.
    assert 'src[i] === "$" && !dollarIsEscaped(src, i)' in tokenize
    assert 'src[i + 1] === "$"' in tokenize
    assert "nextDisplayClose(src, i + 2)" in tokenize
    assert "nextUnescapedDollar(src, i + 1)" in tokenize
    assert 'inlineSource.indexOf("\\n") === -1' in tokenize
    assert 'inlineSource.indexOf("\\r") === -1' in tokenize
    assert "!/\\s/.test(first)" in tokenize
    assert "!/\\s/.test(src[endI - 1]" in tokenize
    assert 'buf += "$"' in tokenize and "i += 1" in tokenize
    assert 'src[i] === "\\\\"' in escaped and "slashes % 2 === 1" in escaped


def test_verso_math_renderer_is_css_aware_and_idempotent(katex_js: str):
    code = _strip_block_comments(katex_js)
    available = _fn_body(code, "function katexAvailable")
    render = _fn_body(code, "function renderVersoMath")
    collect = _fn_body(code, "function collectVersoMathText")
    generated = _fn_body(code, "function generatedVersoMathElement")

    assert "!window.__KATEX_JS_FAILED" in available
    assert "!window.__KATEX_CSS_FAILED" in available
    assert 'container.getAttribute(VERSO_MATH_RENDERED_ATTR) === "1"' in render
    assert 'container.setAttribute(VERSO_MATH_RENDERED_ATTR, "1")' in render
    assert "insideGeneratedVersoMath(explicit[i], container)" in render
    assert "generatedVersoMathElement(node)" in collect
    for generated_class in ('"katex"', '"verso-math"', '"latex-fallback"'):
        assert generated_class in generated


def test_verso_urls_are_same_origin_and_mount_confined(app_js: str):
    code = _strip_block_comments(app_js)
    safe_url = _fn_body(code, "function safeVersoURL")
    assert "window.location.origin" in safe_url
    assert "parsed.origin" in safe_url
    assert "parsed.pathname.indexOf(root)" in safe_url
    assert "pageRelative" in safe_url and "new URL(raw, resolutionBase)" in safe_url
    assert "artifactBase" in safe_url and "resolutionBase" in safe_url
    assert "return null" in safe_url
    assert "safeVersoURL(relative)" in _fn_body(code, "function fetchVersoPage")


def test_verso_page_relative_and_extra_definition_links_are_rewritten_safely(app_js: str):
    code = _strip_block_comments(app_js)
    sanitizer = _fn_body(code, "function sanitizeVersoNode")
    assert 'safeVersoURL(source.getAttribute("href"), pageRelative, artifactBase)' in sanitizer
    decl = _fn_body(code, "function declarationCodeFromPage")
    module = _fn_body(code, "function moduleCodeFromPage")
    assert "sanitizeVersoNode(code, pageRelative, artifactBase, idPrefix)" in decl
    assert "sanitizeVersoNode(source, pageRelative, artifactBase, idPrefix)" in module
    doc_base = _fn_body(code, "function versoDocumentBase")
    assert 'querySelector("base[href]")' in doc_base
    assert "virtualOrigin" in doc_base
    assert "virtualBase" in doc_base and "versoRootPath" in doc_base
    assert "return safeVersoURL(mounted)" in doc_base
    extras = _fn_body(code, "function appendVersoExtraLinks")
    assert 'closest("[data-verso-links]")' in extras
    assert "JSON.parse(raw)" in extras
    assert "safeVersoURL(item.href, pageRelative, artifactBase)" in extras
    assert "innerHTML" not in extras


def test_verso_copy_exports_plain_lean_without_markdown_links(app_js: str):
    code = _strip_block_comments(app_js)
    copy = _fn_body(code, "function copyVersoSelectionAsPlainText")
    sanitizer = _fn_body(code, "function sanitizeVersoNode")

    # Keep semantic names as accessible, clickable anchors in the rendered
    # source, but suppress the rich-HTML clipboard flavor that Markdown-aware
    # destinations rewrite as `[declaration](URL)`.
    assert 'target.setAttribute("href", href)' in sanitizer
    assert 'document.addEventListener("copy", copyVersoSelectionAsPlainText)' in copy
    assert "window.getSelection" in copy
    assert "selection.isCollapsed" in copy
    assert 'anchorElement.closest(".verso-semantic-source")' in copy
    assert "container.contains(selection.focusNode)" in copy
    assert "clipboard.clearData()" in copy
    assert 'clipboard.setData("text/plain", plain)' in copy
    assert "event.preventDefault()" in copy
    assert "text/html" not in copy


def test_verso_artifacts_are_data_only_and_hovers_use_committed_code(app_js: str):
    code = _strip_block_comments(app_js)
    enhance = _fn_body(code, "function enhanceVersoDeclaration")
    assert "ensureVersoMetadata" in enhance
    assert "fetchVersoPage" in enhance
    assert "fetchVersoDocs" in enhance
    hovers = _fn_body(code, "function activateVersoHovers")
    assert "data-binding" in hovers and "binding-hl" in hovers
    assert "ensureVersoHoverController" in hovers
    hover = _fn_body(code, "function ensureVersoHoverController")
    assert "mouseover" in hover and "focusin" in hover and "Escape" in hover
    assert "MutationObserver" in hover
    target = _fn_body(code, "function versoHoverTarget")
    assert 'node.matches("a[href]")' in target
    assert "node.querySelector(VERSO_HOVER_SELECTOR)" in target
    # Generated artifacts are parsed/fetched only. No generated CSS or JS is
    # installed into Marathon's privileged document.
    for forbidden in (
        "function loadVersoScript", "function appendVersoStylesheet",
        "function loadVersoAssets", 'el("script"', 'el("link"',
        "popper.js", "tippy.js", "tippy-border.css",
    ):
        assert forbidden not in app_js


def test_verso_artifact_requests_are_token_pinned_and_retryable(app_js: str):
    code = _strip_block_comments(app_js)
    root = _fn_body(code, "function versoRootPath")
    assert "manifest.root_url" in root
    assert root.index("manifest.root_url") < root.index("status.root_url")

    page = _fn_body(code, "function fetchVersoPage")
    assert "delete state.versoPages[key]" in page
    docs = _fn_body(code, "function fetchVersoDocs")
    assert "versoDocsRequestToken" in docs and "versoDocsToken" in docs


def test_verso_fragment_ids_are_unique_and_hover_init_waits_for_docs(app_js: str):
    code = _strip_block_comments(app_js)
    sanitizer = _fn_body(code, "function sanitizeVersoNode")
    assert '(idPrefix || "") + sourceId' in sanitizer
    assert '(idPrefix || "") + labelFor' in sanitizer
    shell = _fn_body(code, "function makeVersoShell")
    assert "versoFragmentSeq" in shell and "_marathonVersoIdPrefix" in shell
    decl = _fn_body(code, "function enhanceVersoDeclaration")
    module = _fn_body(code, "function enhanceVersoModule")
    assert "shell._marathonVersoIdPrefix" in decl
    assert "shell._marathonVersoIdPrefix" in module
    assert "state.versoManifest.artifact_token !== artifactToken" in decl
    assert "state.versoManifest.artifact_token !== artifactToken" in module

    hovers = _fn_body(code, "function activateVersoHovers")
    assert "fetchVersoDocs()" in hovers
    assert "ensureVersoHoverController" in hovers
    assert hovers.rindex('setAttribute("data-verso-hovers", "1")') > hovers.index(
        "ensureVersoHoverController"
    )

    reset = _fn_body(code, "function resetVersoInteractions")
    assert 'removeAttribute("data-verso-bindings")' in reset
    assert 'removeAttribute("data-verso-hovers")' in reset
    assert 'removeAttribute("data-verso-hover-retries")' in reset
    assert "state.versoHoverController.closeFor(container)" in reset
    paint = _fn_body(code, "function paintVersoResult")
    assert paint.index("resetVersoInteractions(semantic)") < paint.index(
        "semantic.removeChild"
    )
    assert paint.index('setAttribute("data-verso-artifact"') < paint.index(
        'removeAttribute("hidden")'
    ) < paint.index("updateVersoFreshness(shell, status)")


def test_verso_building_status_polls_then_upgrades_mounted_surfaces(app_js: str):
    code = _strip_block_comments(app_js)
    ensure = _fn_body(code, "function ensureVersoMetadata")
    poll = _fn_body(code, "function scheduleVersoStatusPoll")
    # A transient building response must not satisfy the terminal cache path.
    assert "isBuilding" in ensure
    assert "!isBuilding" in ensure
    assert "selectedArtifactMatches" in ensure
    assert "requestEpoch !== state.versoMetadataEpoch" in ensure
    assert "versoMetadataSuperseded" in ensure
    assert "scheduleVersoStatusPoll(result.status)" in ensure
    assert "wasBuilding" in ensure and "transport_error" in ensure
    for state_name in ('"building"', '"queued"', '"idle"'):
        assert state_name in poll
    assert "setTimeout" in poll and "2000" in poll and "10000" in poll
    assert "versoPollAttempts >= 390" in poll
    assert "ensureVersoMetadata(true)" in poll
    assert "refreshMountedVersoSurfaces()" in poll
    refresh = _fn_body(code, "function refreshMountedVersoSurfaces")
    assert 'getAttribute("data-verso-artifact") === artifactToken' in refresh
    assert "updateVersoFreshness" in refresh


def test_stale_verso_never_hides_current_raw_source(app_js: str):
    code = _strip_block_comments(app_js)
    shell = _fn_body(code, "function makeVersoShell")
    assert shell.index("raw,") < shell.index("semantic,")
    assert 'el("div", { class: "verso-raw-fallback" }' in shell
    assert 'el("details"' not in shell and 'el("summary"' not in shell
    assert "Current raw source" not in shell and "Show current raw source" not in shell
    freshness = _fn_body(code, "function updateVersoFreshness")
    assert "state.versoManifest.artifact_token" in freshness
    assert 'semantic.getAttribute("data-verso-artifact")' in freshness
    assert "semanticArtifact === selectedArtifact" in freshness
    assert "hasSemantic && artifactMatches && status && status.available" in freshness
    assert 'semantic.removeAttribute("hidden")' in freshness
    assert 'semantic.setAttribute("hidden", "hidden")' in freshness
    assert freshness.index('semantic.removeAttribute("hidden")') < freshness.index(
        'semantic.setAttribute("hidden", "hidden")'
    )
    assert "versoNeedsStatusChrome(status) || !semanticReady" in freshness
    assert 'raw.removeAttribute("hidden")' in freshness
    assert 'raw.setAttribute("hidden", "hidden")' in freshness
    paint = _fn_body(code, "function paintVersoResult")
    assert "updateVersoFreshness(shell, status)" in paint


def test_verso_surfaces_report_only_actionable_statuses_and_keep_raw_fallback(
    app_js: str, style_css: str,
):
    code = _strip_block_comments(app_js)
    badge = _fn_body(code, "function versoBadge")
    for label in ("Verso building", "Verso stale", "Verso error"):
        assert label in badge
    assert "Verso semantic" not in badge
    assert 'hidden: label ? null : "hidden"' in badge
    shell = _fn_body(code, "function makeVersoShell")
    assert "verso-raw-fallback" in shell
    assert "Current raw source" not in shell and "Show current raw source" not in shell
    for cls in (
        ".verso-source-shell", ".verso-semantic-source", ".verso-raw-fallback",
        ".verso-status-building", ".verso-status-stale", ".verso-status-error",
        ".binding-hl", ".verso-safe-hover",
        ".verso-semantic-module > .md-text",
    ):
        assert cls in style_css


def test_verso_semantic_styles_match_the_dark_marathon_theme(style_css: str):
    start = style_css.index(".verso-source-shell")
    end = style_css.index("/* Elaborated type demoted", start)
    verso = style_css[start:end]

    for themed_value in (
        "background: #0b0e14",
        "background: var(--panel)",
        "background: var(--panel-2)",
        "color: var(--ink)",
        "color: var(--muted)",
        "border-bottom: 1px solid var(--line)",
        "color: #ff7b72",
        "color: #79c0ff",
        "color: #ffa657",
        "color: #a5d6ff",
        "color: #d2a8ff",
    ):
        assert themed_value in verso

    for light_background in ("#fff", "#f8fafc", "#eef2f6", "#f6f8fa", "#eaeef2"):
        assert light_background not in verso


def test_verso_prose_and_math_have_scoped_readable_styles(style_css: str):
    start = style_css.index(".verso-source-shell")
    end = style_css.index("/* Elaborated type demoted", start)
    verso = style_css[start:end]
    for selector in (
        ".verso-declaration-doc",
        ".verso-semantic-module > .md-text",
        ".verso-semantic-module > .verso-text",
        ".verso-semantic-source code.math",
        ".verso-semantic-source .verso-math-inline",
        ".verso-semantic-source .verso-math-display",
        ".verso-semantic-source .verso-math.latex-fallback",
    ):
        assert selector in verso
    assert ":where(.md-text, .verso-text)" in verso
    assert "overflow-x: auto" in verso


def test_marathon_trust_and_owner_inspector_remains_review_details(app_js: str):
    code = _strip_block_comments(app_js)
    card = _fn_body(code, "function renderDeclCard")
    reader = _fn_body(code, "function renderReaderTarget")
    assert "Review details" in card and "showDefinitionLookup" in card
    assert "Review details" in reader and "showDefinitionLookup" in reader
    rendered = _fn_body(code, "function renderDefinitionMatch")
    assert "Trust evidence" in rendered
    assert "Owning review card" in rendered
    # Reconciliation/integrity reads are intentionally not replaced by Verso.
    assert "/api/reconciliation/" in app_js


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


# --- dependency-aware review workspace -------------------------------------

def test_subdeck_is_topologically_ordered_in_the_frontend(app_js: str):
    """The backend wire order is severity-first; the human-facing order must
    instead put local definitions before declarations whose kernel cites them."""
    code = _strip_block_comments(app_js)
    deps = _fn_body(code, "function localDependencyNames")
    assert ".kernel" in deps and "member.name" in deps
    order = _fn_body(code, "function dependencyOrder")
    assert "indegree" in order and "dependents" in order
    assert "cyclic remainder" in app_js
    normalize = _fn_body(code, "function normalizeSubdeck")
    assert "dependencyOrder" in normalize
    # Both initial load and refetch adapt the wire payload before rendering.
    for fn in ("function openIssue", "function refetchSubdeck"):
        assert "normalizeSubdeck(sd)" in _fn_body(code, fn)


def test_rejected_predecessor_guards_later_decisions(app_js: str):
    code = _strip_block_comments(app_js)
    guard = _fn_body(code, "function guardDependencyDecision")
    assert "rejectedLocalDependencies" in guard
    assert "No verdict was recorded" in guard
    assert "dependencyOverrides" in guard
    # Verify and reject are guarded; defer remains available as a non-decision.
    assert "guardDependencyDecision()" in _fn_body(code, "function actVerify")
    assert "guardDependencyDecision()" in _fn_body(code, "function actReject")
    assert "guardDependencyDecision()" not in _fn_body(code, "function actDefer")


def test_first_reject_pauses_at_partial_ship_summary(app_js: str, style_css: str):
    code = _strip_block_comments(app_js)
    commit = _fn_body(code, "function commitDeclVerdict")
    assert 'if (verdict === "reject") state.showSummary = true' in commit
    summary = _fn_body(code, "function renderSummaryCard")
    assert "partialReject" in summary
    assert "Review paused at first rejection" in summary
    assert "WAIT (" in summary
    assert "Continue reviewing anyway" in summary
    confirm = _fn_body(code, "function confirmAndShip")
    assert "partialReject" in confirm and "excluded from KEEP/FIX" in confirm
    assert ".summary-wait" in style_css


def test_continued_partial_review_can_return_to_paused_summary(
    app_js: str, style_css: str
):
    """Continuing past a rejection must never strand the partial SHIP card."""
    code = _strip_block_comments(app_js)
    predicate = _fn_body(code, "function hasPausedReviewSummary")
    assert "summary.rejected" in predicate
    assert "partialWaitCards()" in predicate
    wait = _fn_body(code, "function partialWaitCards")
    assert 'card.verdict !== "verified"' in wait
    assert 'card.verdict !== "rejected"' in wait
    assert "summary || {}).invalidated" in wait
    assert "autoApprovedCandidates()" in wait

    go_back = _fn_body(code, "function returnToPausedReviewSummary")
    assert "closeReviewInspector()" in go_back
    assert "state.showSummary = true" in go_back
    assert "renderSubdeckStage()" in go_back

    header = _fn_body(code, "function subdeckHeader")
    assert "pausedSummaryButton()" in header
    guard = _fn_body(code, "function guardDependencyDecision")
    assert "Return to paused review summary" in guard
    assert "returnToPausedReviewSummary" in guard
    assert ".paused-summary-btn" in style_css
    assert ".return-paused-summary" in style_css


def test_dependency_invalidation_reconciles_authoritative_post_response(app_js: str):
    code = _strip_block_comments(app_js)
    body = _fn_body(code, "function applyDeclResult")
    assert "resp.summary" in body and "summary = resp.summary" in body
    assert "dependency_invalidated" in body
    assert "verdict_before_dependency_invalidation" in body
    # An explicit new verdict on the touched card must beat any saved pre-mask
    # verdict; restoration is only for untouched siblings.
    assert "card.name !== decl" in body


def test_partial_reject_suppresses_skip_api_autoapproval_in_ui(app_js: str):
    code = _strip_block_comments(app_js)
    auto = _fn_body(code, "function autoApprovedForShip")
    assert "hasPausedReviewSummary()" in auto
    assert "return []" in auto
    wait = _fn_body(code, "function partialWaitCards")
    assert "autoApprovedCandidates()" in wait
    panel = _fn_body(code, "function autoApprovedPanel")
    assert "paused in WAIT after rejection" in panel
    assert "suppresses auto-approval" in panel


def test_reject_composer_guards_all_destructive_navigation(app_js: str):
    code = _strip_block_comments(app_js)
    for fn in (
        "function setSkipApi", "function refetchSubdeck", "function openIssue",
        "function returnToPausedReviewSummary", "function pullIntoReview",
        "function toggleDeclLock",
    ):
        assert "state.rejectComposerOpen" in _fn_body(code, fn), fn
    reject = _fn_body(code, "function openRejectNote")
    assert "setComposerNavigationLocked(true)" in reject
    assert "setComposerNavigationLocked(false)" in reject


def test_stale_subdeck_get_cannot_replace_newer_issue(app_js: str):
    code = _strip_block_comments(app_js)
    for fn in ("function openIssue", "function refetchSubdeck"):
        body = _fn_body(code, fn)
        assert "subdeckRequestSeq" in body
        assert "requestSeq !== state.subdeckRequestSeq" in body
        assert "state.issue !== issue" in body


def test_reject_editor_is_non_modal_and_keeps_source_visible(app_js: str, style_css: str):
    code = _strip_block_comments(app_js)
    body = _fn_body(code, "function openRejectNote")
    assert "openReviewInspector" in body
    assert "reject-composer" in body
    assert "openModal(" not in body
    assert "setActionsEnabled(false)" in body
    # Desktop layout is side-by-side; narrow screens naturally stack it below.
    assert ".decl-card.has-inspector .review-body" in style_css
    assert "grid-template-columns" in style_css
    assert ".review-inspector[hidden]" in style_css


def test_source_terms_open_current_definition_inspector(app_js: str, style_css: str):
    code = _strip_block_comments(app_js)
    lookup = _fn_body(code, "function kernelTermLookup")
    assert "member.name" in lookup
    ref = _fn_body(code, "function leanReference")
    assert "showDefinitionLookup" in ref
    assert 'setAttribute("role", "button")' in ref
    assert 'setAttribute("tabindex", "0")' in ref
    plain = _fn_body(code, "function leanPlainNodes")
    assert "projectTermCandidates" in plain and "leanReference" in plain
    inspector = _fn_body(code, "function showDefinitionLookup")
    assert "fetchDefinition" in inspector and "renderDefinitionMatch" in inspector
    rendered = _fn_body(code, "function renderDefinitionMatch")
    for field in ("source_file", "source_line", "statement_pp", "value_pp", "owners"):
        assert field in rendered
    assert "/api/definition" in app_js
    assert "/api/definition-terms" in app_js
    assert ".lh-ref" in style_css


def test_definition_terms_refresh_is_single_flight_and_source_aware(app_js: str):
    code = _strip_block_comments(app_js)
    refresh = _fn_body(code, "function refreshDefinitionTerms")
    assert "definitionTermsRequest" in refresh
    assert "definitionTermsQueued" in refresh
    assert "definitionTermsRequestSeq" in refresh
    assert "generation === state.definitionTermsRequestSeq" in refresh
    assert "definitionSourceToken" in refresh
    assert "fetchDefinitionTerms" in refresh
    install = _fn_body(code, "function installDefinitionTerms")
    assert "source_token" in install
    assert "Object.create(null)" in install
    assert "repaintDefinitionSurfaces" in install
    assert "refreshDefinitionTerms(s.source_token" in _fn_body(
        code, "function applyStatus"
    )
    for function_name in ("function openIssue", "function refetchSubdeck"):
        assert "refreshDefinitionTerms(null, true)" in _fn_body(code, function_name)
    assert "refreshDefinitionTerms(null, true)" in _fn_body(
        code, "function fetchReviewSource"
    )


def test_definition_term_race_never_installs_superseded_index(app_js: str):
    assert _run_definition_terms_race(app_js) == {
        "reads": 2,
        "installs": ["token-a"],
        "requestCleared": True,
        "sequence": 4,
    }


def test_definition_link_refresh_preserves_active_review_surface(app_js: str):
    code = _strip_block_comments(app_js)
    repaint = _fn_body(code, "function repaintDefinitionSurfaces")
    assert "refreshReviewDefinitionLinks" in repaint
    assert "refreshReaderDefinitionLinks" in repaint
    for teardown in ("revisitDecl", "renderSubdeckStage", "paintFilesTab"):
        assert teardown not in repaint
    assert "rejectComposerOpen" not in repaint
    assert "state.busy" not in repaint

    patch_review = _fn_body(code, "function refreshReviewDefinitionLinks")
    patch_reader = _fn_body(code, "function refreshReaderDefinitionLinks")
    replace = _fn_body(code, "function replaceLeanHighlight")
    assert "replaceLeanHighlight" in patch_review
    assert "replaceLeanHighlight" in patch_reader
    assert "document.activeElement" in replace
    assert 'querySelectorAll(".lh-ref")' in replace


def test_every_definition_modal_close_path_cancels_pending_lookup(app_js: str):
    code = _strip_block_comments(app_js)
    close = _fn_body(code, "function closeModal")
    assert "_marathonOnClose" in close
    assert 'typeof onClose === "function"' in close
    opened = _fn_body(code, "function openModal")
    assert "_marathonOnClose" in opened
    assert "if (e.target === backdrop) closeModal()" in opened
    # Escape closes only the top modal; it must not bubble into the document
    # handler and also close an underlying reject inspector/draft.
    assert "e.stopPropagation()" in opened
    definition = _fn_body(code, "function openDefinitionSurface")
    assert "definitionRequestSeq += 1" in definition
    keys = _fn_body(code, "function attachKeys")
    assert 'if (e.key === "Escape") closeModal()' in keys


def test_reconciliation_proposals_have_confirm_and_dismiss_ui(
    app_js: str, style_css: str
):
    code = _strip_block_comments(app_js)
    assert "/api/reconciliation/" in code
    assert "/api/reconcile-proposal" in code
    panel = _fn_body(code, "function reconciliationProposalPanel")
    assert "proposed_renames" in panel and "proposed_splits" in panel
    assert "Confirm" in panel and "Not the same" in panel
    decision = _fn_body(code, "function decideReconciliation")
    assert "postReconciliationDecision" in decision
    assert "refetchSubdeck" in decision
    assert ".reconcile-proposals" in style_css


def test_optional_definition_index_never_blocks_queue_startup(app_js: str):
    code = _strip_block_comments(app_js)
    init = _fn_body(code, "function init")
    assert "refreshQueue()" in init and "refreshDefinitionTerms(" in init
    assert init.index("refreshQueue()") < init.index("refreshDefinitionTerms(")
    assert ".then(refreshQueue)" not in init


def test_deps_and_open_controls_have_durable_non_popup_surfaces(app_js: str):
    code = _strip_block_comments(app_js)
    assert "openDependencyMap()" in _fn_body(code, "function openDeps")
    depmap = _fn_body(code, "function openDependencyMap")
    for token in ("dependencyOrder", "localDependencyNames", "source_file", "sd.deps"):
        assert token in depmap
    deep = _fn_body(code, "function openDeepDive")
    assert "openResourceLinks()" in deep
    assert "window.open" not in deep
    resources = _fn_body(code, "function openResourceLinks")
    assert "sd.permalink" in resources
    assert 'target: "_blank"' in resources and 'rel: "noopener"' in resources


def test_elaborated_type_and_inspector_are_excluded_from_swipe(app_js: str, style_css: str):
    code = _strip_block_comments(app_js)
    card = _fn_body(code, "function renderDeclCard")
    assert 'class: "elab-type", "data-no-swipe": "true"' in card
    swipe = _fn_body(code, "function attachSwipe")
    assert "details, summary, [data-no-swipe]" in swipe
    assert ".elab-type { touch-action: auto; }" in style_css


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
    # A truly resolved project says all caught up. An unresolved-but-blocked
    # project explains its refinement/audit/dependency blockers and links to
    # Status; a stale serialized job must never promise it is still building.
    assert "All caught up" in app_js
    assert "No issues ready for review" in app_js
    assert "awaiting_refinement" in app_js
    assert "needs_attention" in app_js
    assert "needs_citation_mapping" in app_js
    assert "need declaration mapping" in app_js
    assert "awaiting_audit" in app_js
    assert "need current machine-audit evidence" in app_js
    assert "dependency_blocked" in app_js
    assert "other_unresolved" in app_js
    assert "actively building" in app_js
    assert "check back shortly" not in app_js
    assert "Open status" in app_js
    assert "Open issues" in app_js
    assert "showErrorCard" in code
    assert "Retry" in app_js
    summary = _fn_body(code, "function queueBlockerSummary")
    assert "state.viewBuilding" in summary
    assert "state.building" not in summary


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
    assert "leanHighlight(card.source_block, card.kernel)" in body
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
    assert "review_token" in body, "postShip must bind to the reviewed subdeck token"
    assert "/api/ship" in body
    # shipIssue passes the live toggle state into postShip
    ship = _fn_body(code, "function shipIssue")
    assert "postShip(" in ship
    assert "skipApi" in ship and "state.skipApi" in ship
    assert "state.subdeck.review_token" in ship
    assert "state.subdeck.shippable === false" in ship
    assert "postShip(issue, skipApi, reviewToken, requestId)" in ship


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
                      "postShip(", "/api/decl-verdict"):
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


# --- the manual push-refresh button was RETIRED (auto-reconcile replaces it) -

def test_no_manual_push_refresh_button(app_js: str):
    # The conductor auto-reconciles signatures to the live code, so there is NO
    # client-side "refresh issue body from code" button and no /api/refresh-issue
    # POST anywhere in the deck.
    code = _strip_block_comments(app_js)
    assert "/api/refresh-issue" not in code
    assert "postRefreshIssue" not in code
    assert "function refreshIssueButton" not in code
    assert "function confirmAndRefreshIssue" not in code
    assert "btn-refresh-issue" not in code


def test_stale_footnote_explains_auto_reconcile(app_js: str):
    # The stale-citation footnote no longer points at a button; it explains that
    # renames reconcile automatically and an unproven stale citation is kept.
    code = _strip_block_comments(app_js)
    foot = _fn_body(code, "function staleFootnote")
    assert "Refresh issue body from code" not in foot
    assert "automatically" in foot


def test_refresh_does_not_break_ship_single_call_site(app_js: str):
    # Regression guard: ship still has exactly one call site / no raw fetch.
    code = _strip_block_comments(app_js)
    direct = re.findall(r"['\"]/api/ship['\"]", code)
    assert len(direct) == 1
    raw = re.findall(r"fetch\(\s*['\"]/api/ship['\"]", code)
    assert len(raw) == 0


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
    assert "/api/conductor-start" in code
    assert "/api/issues-overview" in code
    assert "/api/history" in code


def test_review_retry_supersedes_hung_read_and_paints_atomic_snapshot(
    app_js: str,
):
    """Retry must not tear down Review or let an older queue/stuck pair win."""
    code = _strip_block_comments(app_js)
    refresh = _fn_body(code, "function refreshQueue")
    start = _fn_body(code, "function startQueueRefresh")
    drained = _fn_body(code, "function openFirstReadyIssue")

    assert "beginSurfaceRefresh(" in refresh
    assert "stage().innerHTML" not in refresh
    assert "state.queueRequest" in refresh
    assert "++state.queueRequestSeq" in refresh
    assert "queueAbortController" in refresh
    assert ".abort()" in refresh

    assert "Promise.all(" in start
    assert "withReadDeadline(" in start
    assert "QUEUE_READ_TIMEOUT_MS" in start
    assert "fetchQueue(null, options)" in start
    assert 'getJSON("/api/stuck", options)' in start
    assert "generation !== state.queueRequestSeq" in start
    assert "state.queueRequest = completion" in start

    # Parked recovery is already part of the installed snapshot; the drained
    # renderer performs no late GET that could replace the card a second time.
    assert 'getJSON("/api/stuck")' not in drained
    assert "state.reviewStuck" in drained
    assert "state.queueRenderedKey === state.queueSnapshotKey" in drained


def test_review_refresh_race_installs_only_newest_snapshot(app_js: str):
    result = _run_queue_refresh_race(app_js)
    assert result == {
        "queueReads": 2,
        "stuckReads": 2,
        "renders": 1,
        "installedMarker": "new",
        "stuckCount": 0,
        "requestCleared": True,
        "ended": 1,
        "abortedReads": 2,
    }


def test_issues_retry_supersedes_hung_atomic_read_and_is_generation_guarded(
    app_js: str,
):
    code = _strip_block_comments(app_js)
    body = _fn_body(code, "function renderIssuesTab")
    assert "beginSurfaceRefresh(" in body
    assert "state.issuesRequest" in body
    assert "++state.issuesRequestSeq" in body
    assert "issuesAbortController" in body
    assert ".abort()" in body
    assert "Promise.all(" in body
    assert "withReadDeadline(" in body
    assert "ISSUES_READ_TIMEOUT_MS" in body
    assert 'getJSON("/api/issues-overview", options)' in body
    assert 'getJSON("/api/stuck", options)' in body
    assert "requestGeneration !== state.issuesRequestSeq" in body
    assert "snapshotKey === state.issuesSnapshotKey" in body

    stuck = _fn_body(code, "function renderStuckSection")
    assert "getJSON(" not in stuck
    assert "stuckSnapshot" in stuck


def test_issues_retry_race_installs_only_newest_atomic_snapshot(app_js: str):
    assert _run_issues_refresh_race(app_js) == {
        "overviewReads": 2,
        "stuckReads": 2,
        "abortedReads": 2,
        "stuckRenders": 1,
        "installedIssue": 240,
        "requestCleared": True,
        "ended": 1,
    }


def test_recovery_completion_refreshes_origin_exactly_once(app_js: str):
    code = _strip_block_comments(app_js)
    body = _fn_body(code, "function openRecoveryDialog")
    assert "refreshedAfterCompletion" in body
    assert "function refreshOriginOnce" in body
    assert body.count("refreshOriginOnce()") == 3  # definition + two race paths


def test_status_poll_keeps_unchanged_dom_mounted(app_js: str):
    code = _strip_block_comments(app_js)
    body = _fn_body(code, "function renderStatusTab")
    assert "state.statusDashboardRequest" in body
    assert "state.statusDashboardRequestSeq" in body
    assert "statusDashboardAbortController" in body
    assert "withReadDeadline(" in body
    assert "STATUS_READ_TIMEOUT_MS" in body
    assert "state.statusDashboardRequest && !force" in body
    assert body.count("generation === state.statusDashboardRequestSeq") >= 3
    assert 'key !== "updated_ts"' in body
    assert "snapshotKey === state.statusDashboardSnapshotKey" in body
    assert 'querySelector(".cond-updated")' in body
    assert "syncDomChildren(host, nextHost)" in body
    assert "data-status-key" in body
    assert "host.innerHTML" not in body


def test_forced_status_refresh_supersedes_stale_poll(app_js: str):
    assert _run_status_refresh_race(app_js) == {
        "reads": 2,
        "abortedReads": 1,
        "heartbeat": "updated new-heartbeat",
        "clears": 1,
        "requestCleared": True,
        "ended": 1,
    }


def test_status_transient_failure_keeps_last_good_snapshot(
    app_js: str, style_css: str,
):
    code = _strip_block_comments(app_js)
    warning = _fn_body(code, "function showStatusRefreshError")
    assert "state.statusDashboardSnapshotKey" in warning
    assert "surfaceHasStableContent(host)" in warning
    assert "status-refresh-error" in warning
    assert "dashError(" in warning
    assert "innerHTML" not in warning
    assert ".status-refresh-error" in style_css


def test_status_mutations_force_a_new_generation(app_js: str):
    code = _strip_block_comments(app_js)
    switch = _fn_body(code, "function switchTab")
    restart = _fn_body(code, "function startConductorFromStatus")
    toggle = _fn_body(code, "function proverToggle")
    assert "renderStatusTab(true)" in switch
    assert restart.count("renderStatusTab(true)") == 3
    assert "renderStatusTab(true)" in toggle


def test_background_refresh_indicator_is_compact_and_reduced_motion_safe(
    style_css: str,
):
    assert ".surface-refresh-indicator" in style_css
    assert ".surface-refresh-spinner" in style_css
    reduced = style_css[style_css.find("@media (prefers-reduced-motion: reduce)"):]
    assert ".surface-refresh-spinner" in reduced


def test_style_has_tab_and_dashboard_classes(style_css: str):
    for cls in (".tab-nav", ".tab-btn", ".cond-banner", ".issue-row",
                ".hist-row", ".sb-verified"):
        assert cls in style_css


def test_stopped_conductor_has_opt_in_restart_button(app_js: str, style_css: str):
    """The server capability, not a dead status alone, gates host compute."""
    code = _strip_block_comments(app_js)
    headers = re.findall(r"(?m)^  function [A-Za-z0-9_$]+", code)
    control_bodies = [
        _fn_body(code, header)
        for header in headers
        if "cond-restart" in _fn_body(code, header)
    ]
    assert control_bodies, "expected a dedicated conductor restart control"
    control = control_bodies[0]
    assert "restart_allowed" in control
    assert ".alive" in control
    assert "Restart orchestrator" in control
    assert "addEventListener" in control
    assert ".cond-restart" in style_css


def test_conductor_restart_post_is_isolated_from_status_renderer(app_js: str):
    """Ordinary five-second status refreshes must remain pure GETs."""
    code = _strip_block_comments(app_js)
    render = _fn_body(code, "function renderStatusTab")
    assert "postJSON" not in render
    assert code.count('"/api/conductor-start"') == 1

    headers = re.findall(r"(?m)^  function [A-Za-z0-9_$]+", code)
    start_bodies = [
        _fn_body(code, header)
        for header in headers
        if 'postJSON("/api/conductor-start"' in _fn_body(code, header)
    ]
    assert len(start_bodies) == 1
    start = start_bodies[0]
    assert "conductorStartBusy" in start
    assert "renderStatusTab" in start


def test_conductor_restart_waits_for_confirmed_liveness(app_js: str):
    """Popen success is not yet readiness; poll for a bounded period."""
    code = _strip_block_comments(app_js)
    assert "conductorStartBusy" in code
    headers = re.findall(r"(?m)^  function [A-Za-z0-9_$]+", code)
    pollers = [
        _fn_body(code, header)
        for header in headers
        if '/api/conductor-status' in _fn_body(code, header)
        and "setTimeout" in _fn_body(code, header)
    ]
    assert pollers, "restart should poll the status endpoint after launch"
    poller = pollers[0]
    assert "getJSON(" in poller
    assert ".alive" in poller
    assert re.search(r"attempt|tries|remaining", poller, flags=re.IGNORECASE)


# --- wide-angle file/chapter review + project dependency DAG ---------------

def test_index_has_files_and_graph_tabs(index_html: str):
    for tab, host in (("files", "dash-files"), ("graph", "dash-graph")):
        assert 'data-tab="' + tab + '"' in index_html
        assert 'id="tab-' + tab + '"' in index_html
        assert 'id="' + host + '"' in index_html


def test_files_reader_is_source_contextual_and_batch_is_local(app_js: str):
    code = _strip_block_comments(app_js)
    render = _fn_body(code, "function renderReaderTarget")
    assert "source_block" in render and "source_line" in render
    assert "readerRejectEditor" in render
    assert 'switchTab("review")' in render and "openIssue(" in render
    editor = _fn_body(code, "function readerRejectEditor")
    assert "reader-reject-editor" in editor and "textarea" in editor
    assert "openModal(" not in editor  # the note never covers the code
    batch = _fn_body(code, "function readerBatch")
    assert "postBatchDeclVerdicts" in batch
    assert "postShip" not in batch and "/api/ship" not in batch
    post = _fn_body(code, "function postBatchDeclVerdicts")
    assert "/api/batch-decl-verdict" in post and "postJSON(" in post
    whole = _fn_body(code, "function readerFullFile")
    assert "fetchReviewSource" in whole and "Read complete file" in whole
    assert "/api/review-source" in code
    assert "Keep visible unreviewed" not in code
    assert "readerBatch(\"verify\"" not in code
    assert "Defer visible unreviewed" in code


def test_files_reader_guards_rejected_predecessors(app_js: str):
    code = _strip_block_comments(app_js)
    single = _fn_body(code, "function readerVerdict")
    assert "blocked_by" in single and 'verdict !== "defer"' in single
    assert "target.inspectable === false" in single
    assert "inspection_blocked_reason" in single


def test_provisional_review_is_discoverable_and_cannot_masquerade_as_ship_ready(
    app_js: str,
):
    code = _strip_block_comments(app_js)
    provisional = _fn_body(code, "function provisionalIssues")
    assert "inspectable" in provisional
    assert "provisional" in provisional
    assert "!c.ready" in provisional

    drained = _fn_body(code, "function openFirstReadyIssue")
    assert "Review blocked code" in drained
    assert 'state.filesFilter = "provisional"' in drained
    assert 'switchTab("files")' in drained
    assert "shipping stays blocked" in drained
    # Parked recovery remains available, but no longer replaces Review's
    # drained-state surface or becomes the only apparent task.
    assert "Open recovery (" in drained
    assert "showStuckAttentionCard(" not in drained

    target = _fn_body(code, "function renderReaderTarget")
    for label in (
        "PROVISIONAL REVIEW",
        "Provisional keep",
        "Provisional fix",
        "reopens it automatically",
    ):
        assert label in target
    assert "ship_blocked_reason" in target
    assert "inspection_blocked_reason" in target
    assert "reader-unavailable" in target
    assert "disabled: decisionBlocked" in target

    summary = _fn_body(code, "function renderSummaryCard")
    assert "SAVED, NOT SHIPPED" in summary
    assert "var shipBtn = provisionalReview ? null" in summary
    assert "if (shipBtn) summaryActions.push(shipBtn)" in summary


def test_decl_mutations_send_the_visible_context_fingerprint(app_js: str):
    code = _strip_block_comments(app_js)
    post = _fn_body(code, "function postDeclVerdict")
    assert "declFingerprint" in post
    assert "body.decl_fingerprint" in post

    commit = _fn_body(code, "function commitDeclVerdict")
    assert "state.currentDecl.context_fingerprint" in commit
    assert "postDeclVerdict(issue, decl, verdict, note, contextFingerprint)" in commit
    assert "resp.review_token" in commit

    reader = _fn_body(code, "function readerVerdict")
    assert "target.context_fingerprint" in reader
    batch = _fn_body(code, "function readerBatch")
    assert "decl_fingerprint: target.context_fingerprint" in batch

    lock_post = _fn_body(code, "function postDeclLock")
    assert "body.decl_fingerprint" in lock_post
    lock = _fn_body(code, "function toggleDeclLock")
    assert "context_fingerprint" in lock
    assert "resp.review_token" in lock
    assert "err.status === 409" in lock
    assert "refetchSubdeck()" in lock

    # A stale fingerprint is a normal concurrent-code-change outcome. The UI
    # must fetch the new surface instead of offering an infinite retry against
    # the same stale card.
    assert "err.status === 409" in commit
    assert "refetchSubdeck()" in commit
    assert "err.status === 409" in reader
    assert "renderFilesTab()" in reader
    assert "err.status === 409" in batch
    assert "renderFilesTab()" in batch


def test_files_reader_filters_and_restores_overview_position(app_js: str):
    code = _strip_block_comments(app_js)
    match = _fn_body(code, "function readerTargetMatches")
    for token in (
        'filter === "provisional"', 'filter === "unreviewed"', 'filter === "rejected"',
        'filter === "changed"', 'filter === "surfaced"',
    ):
        assert token in match
    paint = _fn_body(code, "function paintFilesTab")
    for label in ("Provisional", "Unreviewed", "Rejected", "Changed", "Surfaced"):
        assert label in paint
    target = _fn_body(code, "function renderReaderTarget")
    assert "filesResumeScroll" in target and "filesResumeName" in target
    assert "window.scrollTo" in paint
    commit = _fn_body(code, "function commitDeclVerdict")
    assert "filesData = null" in commit and "graphData = null" in commit


def test_project_graph_is_filterable_and_navigable(app_js: str):
    code = _strip_block_comments(app_js)
    assert "/api/project-graph" in code
    assert "function graphLayout" in code
    assert "function graphModeView" in code
    assert "function renderGraphCanvas" in code
    paint = _fn_body(code, "function paintGraphTab")
    for token in ("filter.chapter", "filter.status", "filter.query"):
        assert token in paint
    canvas = _fn_body(code, "function renderGraphCanvas")
    assert "project-graph" in canvas and "marker-end" in canvas
    inspect = _fn_body(code, "function showGraphNode")
    assert "Open in Files" in inspect
    assert 'switchTab("review")' in inspect
    assert "openIssue(" in inspect
    assert "unresolved_edges" in inspect and "cyclic_edges" in inspect


def test_project_graph_defaults_to_issue_dependencies_and_separates_modes(
    app_js: str,
):
    code = _strip_block_comments(app_js)
    assert 'graphView: { zoom: 1, mode: "issues" }' in code
    modes = _fn_body(code, "function graphModeSpec")
    for label in (
        "Issue dependency graph", "Declaration dependency graph",
        "Issues + declarations map",
    ):
        assert label in modes
    for edge_kind in (
        'edgeKind: "issue-dependency"',
        'edgeKind: "declaration-dependency"',
        'edgeKind: "contains"',
    ):
        assert edge_kind in modes
    assert "not a dependency graph" in modes
    mode_view = _fn_body(code, "function graphModeView")
    assert "edge.kind === spec.edgeKind" in mode_view
    paint = _fn_body(code, "function paintGraphTab")
    for option in (
        "Issue graph", "Declaration graph", "Issues + declarations map",
    ):
        assert option in paint
    assert '"aria-label": "Graph mode"' in paint
    assert "cyclic dependency edge" in paint
    assert "Strongly connected nodes share one layout layer" in paint


def test_project_graph_manual_refresh_bypasses_server_cache(app_js: str):
    code = _strip_block_comments(app_js)
    fetch = _fn_body(code, "function fetchProjectGraph")
    assert 'force ? "?refresh=1" : ""' in fetch
    paint = _fn_body(code, "function paintGraphTab")
    assert "renderGraphTab(true)" in paint


def test_graph_layout_ignores_ownership_for_dependency_ranks(app_js: str):
    nodes = [
        {"id": "issue:1", "label": "#1", "type": "issue", "chapters": [1]},
        {"id": "decl:A", "label": "A", "type": "declaration", "chapters": [1]},
        {"id": "decl:B", "label": "B", "type": "declaration", "chapters": [1]},
    ]
    layout = _run_graph_layout(app_js, nodes, [
        {"source": "issue:1", "target": "decl:A", "kind": "contains"},
        {"source": "decl:A", "target": "decl:B", "kind": "declaration-dependency"},
    ])
    positions = layout["positions"]
    assert positions["issue:1"]["rank"] == 0
    assert positions["decl:A"]["rank"] == 0
    assert positions["decl:B"]["rank"] == 1


def test_graph_layout_ranks_scc_condensation_without_flattening_downstream(
    app_js: str,
):
    nodes = [
        {"id": name, "label": name, "type": "declaration", "chapters": [1]}
        for name in ("root", "cycle-a", "cycle-b", "child", "leaf")
    ]
    dep = "declaration-dependency"
    layout = _run_graph_layout(app_js, nodes, [
        {"source": "root", "target": "cycle-a", "kind": dep},
        {"source": "cycle-a", "target": "cycle-b", "kind": dep},
        {"source": "cycle-b", "target": "cycle-a", "kind": dep},
        {"source": "cycle-b", "target": "child", "kind": dep},
        {"source": "child", "target": "leaf", "kind": dep},
    ])
    ranks = {name: position["rank"] for name, position in layout["positions"].items()}
    assert ranks == {
        "root": 0,
        "cycle-a": 1,
        "cycle-b": 1,
        "child": 2,
        "leaf": 3,
    }


def test_project_graph_topology_states_and_connected_cap_are_explicit(app_js: str):
    code = _strip_block_comments(app_js)
    topology = _fn_body(code, "function graphTopologyMeta")
    for state in ('"fresh"', '"stale"', '"unavailable"', '"ownership"'):
        assert state in topology
    notice = _fn_body(code, "function graphTopologyNotice")
    for text in (
        "Dependency topology is fresh",
        "Showing last-known",
        "Dependency topology is unavailable",
        "Ownership map:",
    ):
        assert text in notice
    subset = _fn_body(code, "function graphConnectedSubset")
    assert "queue" in subset and "adjacent" in subset
    assert "GRAPH_NODE_LIMIT = 500" in code
    assert "nodes.slice(0, 220)" not in code
    canvas = _fn_body(code, "function renderGraphCanvas")
    assert "graphConnectedSubset" in canvas
    assert "graph-edge-topology-stale" in canvas


def test_project_graph_supports_pan_zoom_and_changed_impact(app_js: str):
    code = _strip_block_comments(app_js)
    paint = _fn_body(code, "function paintGraphTab")
    for token in (
        "Zoom graph out", "Reset graph view", "Zoom graph in",
        "changed downstream", "unresolved_edges", "cyclicVisible",
    ):
        assert token in paint
    canvas = _fn_body(code, "function renderGraphCanvas")
    for token in (
        "pointerdown", "pointermove", "scrollLeft", "scrollTop",
        "graph-impact-", "graph-edge-cyclic",
    ):
        assert token in canvas
    zoom = _fn_body(code, "function graphApplyZoom")
    assert 'setAttribute("width"' in zoom and 'setAttribute("height"' in zoom


def test_wide_review_styles_exist(style_css: str):
    for cls in (
        ".reader-file", ".reader-decl", ".reader-reject-editor",
        ".reader-batch", ".reader-full-file", ".graph-viewport",
        ".project-graph", ".graph-node",
        ".graph-impact-changed", ".graph-impact-downstream",
        ".graph-edge-cyclic", ".graph-edge-topology-stale",
        ".graph-topology-fresh", ".graph-topology-stale",
        ".graph-topology-unavailable", ".graph-topology-ownership",
        ".graph-cycle-note", ".graph-dragging",
    ):
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
    assert "resp.review_token" in save
    assert "state.subdeck.review_token" in save


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


# --- (b2) run observability: progress % + iteration + click-into-run ---------

def test_status_jobs_show_progress_and_iteration(app_js: str):
    code = _strip_block_comments(app_js)
    body = _fn_body(code, "function renderStatusTab")
    assert "progress_pct" in body
    assert "job-prog-fill" in body
    assert ".iteration" in body or "j.iteration" in body
    # rows are clickable into the run detail
    assert "openRunDetail(" in body
    assert "job-row-click" in body


def test_openRunDetail_is_a_pure_read(app_js: str):
    code = _strip_block_comments(app_js)
    body = _fn_body(code, "function openRunDetail")
    assert "/api/run/" in body
    assert "getJSON(" in body
    # it only READS — never posts/ships
    for forbidden in ("postJSON(", "postShip(", "/api/ship", "/api/verdict"):
        assert forbidden not in body
    # it surfaces ratings + prompts
    assert "ratings" in body and "prompts" in body


def test_informal_text_renders_inline_markdown(katex_js: str):
    """The reported bug: **bold** showed literally in the informal section. The
    LaTeX renderer's text segments now render inline markdown (bold/`code`/em) as
    DOM nodes — XSS-safe (createElement + textContent, never innerHTML)."""
    assert "renderRich" in katex_js
    # bold/italic spans recurse so a bold span MAY contain math
    assert "renderRich(b, m[3])" in katex_js or "renderRich(b," in katex_js
    # bold/code/italic tags are emitted
    for tag in ('"strong"', '"code"', '"em"'):
        assert tag in katex_js
    # the markdown renderer is XSS-safe (no innerHTML; uses textContent)
    fn = katex_js[katex_js.find("function renderRich"):
                  katex_js.find("function render(")]
    assert fn and "innerHTML" not in fn
    assert "createTextNode" in fn and "textContent" in fn


def test_status_tab_renders_pending_queue(app_js: str):
    code = _strip_block_comments(app_js)
    body = _fn_body(code, "function renderStatusTab")
    assert "d.queued" in body
    assert "Queued (waiting to dispatch)" in body
    assert "job-wait" in body          # the waiting-reason
    assert "sb-queued" in body


def test_dashboard_numbers_link_to_github(app_js: str):
    """Issue/PR numbers on the Issues + History tabs link to GitHub (the new
    ghLink helper), opening in a new tab and not triggering the row's click."""
    code = _strip_block_comments(app_js)
    assert "function ghLink" in code
    # the URL literal is checked on the RAW source — _strip_block_comments treats
    # the `//` in `https://` as a line comment and would truncate it.
    assert "https://github.com/" in app_js
    fn = _fn_body(code, "function ghLink")
    assert "target" in fn and "stopPropagation" in fn  # new tab; don't open review
    iss = _fn_body(code, "function renderIssuesTab")
    assert 'ghLink(d.repo, "issues"' in iss
    assert "it.blocked_reason" in iss and "issue-blocker" in iss
    hist = _fn_body(code, "function renderHistoryTab")
    assert 'ghLink(d.repo, "issues"' in hist   # verdict events -> issue
    assert 'ghLink(d.repo, "pull"' in hist     # merged PRs -> PR


def test_run_prompts_render_as_markdown(app_js: str, katex_js: str):
    """The refine-reviewer prompts (markdown from marathon-refine-log.md) render
    via the block-level markdown renderer, not a raw <pre>."""
    assert "function renderMarkdown" in katex_js
    assert "renderMarkdown: renderMarkdown" in katex_js     # exported
    for tok in ("md-code", "md-list", "md-h", "md-p"):       # block constructs
        assert tok in katex_js
    code = _strip_block_comments(app_js)
    body = _fn_body(code, "function openRunDetail")
    assert "renderMarkdown(" in body
    assert "md-body" in body


def test_hotkey_legend_hidden_off_review_tab(app_js: str):
    """The ←/→/space/d hotkey legend (a body-level footer) is shown only on the
    review tab; switchTab hides it on the dashboard tabs."""
    code = _strip_block_comments(app_js)
    body = _fn_body(code, "function switchTab")
    assert ".legend" in body
    assert '!== "review"' in body


def test_legend_hidden_attr_actually_hides(style_css: str):
    """The legend has display:flex (a fixed footer), so the `hidden` attribute
    set off the review tab needs an explicit rule to take effect."""
    assert ".legend[hidden]" in style_css


def test_status_job_shows_granular_phase(app_js: str):
    code = _strip_block_comments(app_js)
    body = _fn_body(code, "function renderStatusTab")
    assert "phase-chip" in body and "j.phase" in body


def test_status_landings_link_issue_and_pr(app_js: str):
    body = _fn_body(_strip_block_comments(app_js), "function renderStatusTab")
    # a landing renders the issue + its merged PR (both as GitHub links)
    assert "land-row" in body and "merged PR #" in body
