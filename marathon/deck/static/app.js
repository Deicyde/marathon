/*
 * app.js — the marathon deck dashboard + swipe review UI (vanilla JS, no build).
 *
 * SUBDECK FLOW (the shape this file implements):
 *   The ready QUEUE is the ISSUE picker. Picking a ready issue opens its
 *   SUBDECK — one DECLARATION card at a time. You swipe each declaration
 *   (verify / reject / defer) which records a LOCAL per-decl verdict only.
 *   When every declaration has been reviewed — OR the first rejection pauses
 *   the dependency-ordered review — a confirm-and-SHIP summary card appears;
 *   shipping composes the issue outcome and is the irreversible review action.
 *
 * Codes to the SHARED API CONTRACT:
 *   GET  /api/queue?chapter=N           -> {cards:[CardSummary], building, landed_today}
 *   GET  /api/subdeck/{issue}           -> {issue,title,chapter,card_tier,cards:[DeclCard],
 *                                            progress:{reviewed,total}, complete, summary}
 *   POST /api/decl-verdict {issue,decl,verdict,note?}  (LOCAL ONLY — no GitHub/Aristotle/ledger)
 *                                       -> {ok, progress, complete, summary, next_decl}
 *   POST /api/ship {issue}              (THE IRREVERSIBLE REVIEW side-effect)
 *                                       -> 200 {ok,outcome,message,advanced_to_issue}
 *                                       or 202 {issue,operation_id,status,status_url}
 *   GET  /api/ship-status?...           -> durable async ship-job status
 *   POST /api/recovery {issue,action,recovery_token,confirm_destructive}
 *                                       -> {ok, accepted, queue_state, message}
 *   POST /api/verso-retry {}            -> retry one failed semantic-doc build
 *   GET  /api/status (poll) or /api/events (SSE)  -> live job/build events
 *
 * BINDING SAFETY (load-bearing — see the task brief):
 *   - POST /api/ship is fired ONLY from shipIssue() (the explicit ship-button
 *     handler). It is NEVER called on page load, GET, navigation, prefetch,
 *     status polling, OR a per-declaration verdict. Reviewing/swiping a
 *     declaration is LOCAL-ONLY (POST /api/decl-verdict records a sidecar
 *     verdict — it merges nothing, dispatches no Aristotle).
 *     (tests/test_deck_frontend.py statically enforces this single call site.)
 *   - Every POST carries the per-session token in the
 *     X-Marathon-Session-Token header, read from the injected meta tag /
 *     bootstrap global. No token => the POST is refused client-side.
 *   - Reject notes are sent VERBATIM (no client-side rewriting) and are
 *     composed by the BACKEND into the issue's fix instructions; the note is
 *     REQUIRED on a reject.
 *   - Ship asks for an explicit confirm (it merges a PR or dispatches
 *     Aristotle — irreversible).
 *   - Recovery asks the human to choose a named, server-authorized mode;
 *     archive-and-start-over requires its own destructive acknowledgement.
 *   - Read endpoints (queue/subdeck/status) are pure reads; they never mutate.
 */
(function () {
  "use strict";

  // --- session token -------------------------------------------------------
  // Prefer the meta tag; fall back to the bootstrap global. We hold it in a
  // closure and attach it to every POST. If the placeholder was never
  // substituted (token absent), POSTs are blocked client-side so we never
  // fire a mutating action without authorization.
  function readSessionToken() {
    var meta = document.querySelector('meta[name="marathon-session-token"]');
    var fromMeta = meta ? meta.getAttribute("content") : null;
    var fromGlobal =
      window.MARATHON_DECK && window.MARATHON_DECK.sessionToken
        ? window.MARATHON_DECK.sessionToken
        : null;
    var tok = fromMeta || fromGlobal || "";
    // The unsubstituted placeholder is not a real token.
    if (!tok || tok.indexOf("__MARATHON_SESSION_TOKEN__") !== -1) return "";
    return tok;
  }
  var SESSION_TOKEN = readSessionToken();
  // MUST match the committed backend (marathon/deck/server.py TOKEN_HEADER).
  var TOKEN_HEADER = "X-Marathon-Session-Token";

  // --- tier styling --------------------------------------------------------
  var TIER_CLASS = {
    T0: "tier-t0",
    T1: "tier-t1",
    T2: "tier-t2",
    T3: "tier-t3",
    UNKNOWN: "tier-unknown",
    "-": "tier-none",
  };
  function tierClass(tier) {
    return TIER_CLASS[tier] || "tier-unknown";
  }

  // --- DOM helpers ---------------------------------------------------------
  function el(tag, attrs, children) {
    var node = document.createElement(tag);
    if (attrs) {
      Object.keys(attrs).forEach(function (k) {
        if (k === "class") node.className = attrs[k];
        else if (k === "text") node.textContent = attrs[k];
        else if (k === "html") node.innerHTML = attrs[k];
        else if (k.slice(0, 2) === "on" && typeof attrs[k] === "function")
          node.addEventListener(k.slice(2).toLowerCase(), attrs[k]);
        else if (attrs[k] != null) node.setAttribute(k, attrs[k]);
      });
    }
    (children || []).forEach(function (c) {
      if (c == null) return;
      node.appendChild(typeof c === "string" ? document.createTextNode(c) : c);
    });
    return node;
  }
  var stage = function () { return document.getElementById("card-stage"); };
  var modalRoot = function () { return document.getElementById("modal-root"); };

  // --- network: PURE READS -------------------------------------------------
  // These GET helpers never mutate; they are safe to call on load,
  // navigation, and polling. They MUST NOT be used to issue a verdict/ship.
  function getJSON(url, options) {
    options = options || {};
    var request = {
      method: "GET",
      headers: { Accept: "application/json" },
      credentials: "same-origin",
    };
    if (options.signal) request.signal = options.signal;
    return fetch(url, request).then(function (r) {
      return r.json().then(function (data) {
        if (!r.ok) {
          var err = new Error(
            (data && (data.message || data.error)) ||
              "HTTP " + r.status + " for " + url
          );
          err.status = r.status;
          err.data = data || null;
          throw err;
        }
        return data;
      }, function () {
        throw new Error("HTTP " + r.status + " returned invalid JSON for " + url);
      });
    });
  }

  // Dashboard reads must eventually settle even when a browser/proxy leaves a
  // fetch pending indefinitely.  The caller may also abort the shared
  // controller immediately when the human explicitly retries; the generation
  // guard on that surface prevents the aborted response from repainting.
  function withReadDeadline(promise, timeoutMs, controller, label) {
    return new Promise(function (resolve, reject) {
      var settled = false;
      var timer = setTimeout(function () {
        if (settled) return;
        settled = true;
        if (controller) controller.abort();
        var err = new Error(
          (label || "Request") + " timed out after " +
          Math.round(timeoutMs / 1000) + " seconds"
        );
        err.name = "TimeoutError";
        reject(err);
      }, timeoutMs);
      promise.then(function (value) {
        if (settled) return;
        settled = true;
        clearTimeout(timer);
        resolve(value);
      }, function (err) {
        if (settled) return;
        settled = true;
        clearTimeout(timer);
        reject(err);
      });
    });
  }

  function newReadController() {
    return typeof AbortController === "function" ? new AbortController() : null;
  }

  function readOptions(controller) {
    return controller ? { signal: controller.signal } : {};
  }

  // A bounded variant for job-status reads. Ordinary read models can use the
  // browser's normal fetch lifetime, but a ship poll must always settle so one
  // wedged proxy connection cannot leave the irreversible-action UI locked
  // forever. Errors preserve the HTTP status + parsed JSON when available,
  // which lets the caller distinguish a confirmed "no job" 404 from an
  // unknown network/proxy failure.
  function getJSONWithTimeout(url, timeoutMs) {
    return new Promise(function (resolve, reject) {
      var settled = false;
      var controller = typeof AbortController === "function"
        ? new AbortController() : null;
      var timer = setTimeout(function () {
        if (settled) return;
        settled = true;
        if (controller) controller.abort();
        var timeoutError = new Error(
          "Timed out after " + timeoutMs + "ms while reading " + url
        );
        timeoutError.name = "TimeoutError";
        reject(timeoutError);
      }, timeoutMs);

      var options = {
        method: "GET",
        headers: { Accept: "application/json" },
        credentials: "same-origin",
      };
      if (controller) options.signal = controller.signal;

      fetch(url, options)
        .then(function (r) {
          return r.text().then(function (raw) {
            var data = null;
            if (raw) {
              try {
                data = JSON.parse(raw);
              } catch (parseError) {
                var invalid = new Error(
                  "HTTP " + r.status + " returned non-JSON data for " + url
                );
                invalid.status = r.status;
                invalid.responseText = raw.slice(0, 240);
                throw invalid;
              }
            }
            if (!r.ok) {
              var requestError = new Error(
                (data && (data.message || data.reason || data.error)) ||
                  "HTTP " + r.status + " for " + url
              );
              requestError.status = r.status;
              requestError.data = data;
              throw requestError;
            }
            if (!data || typeof data !== "object") {
              var empty = new Error(
                "HTTP " + r.status + " returned no JSON object for " + url
              );
              empty.status = r.status;
              throw empty;
            }
            return data;
          });
        })
        .then(function (data) {
          if (settled) return;
          settled = true;
          clearTimeout(timer);
          resolve(data);
        }, function (err) {
          if (settled) return;
          settled = true;
          clearTimeout(timer);
          reject(err);
        });
    });
  }
  function fetchQueue(chapter, options) {
    var q = chapter ? "?chapter=" + encodeURIComponent(chapter) : "";
    return getJSON("/api/queue" + q, options);
  }
  // GET /api/subdeck/{issue}?skip_api=0|1 — a PURE READ. The skip_api flag is
  // a VIEW-ONLY parameter: it changes WHICH cards the backend returns (main
  // results only) and surfaces the would-be auto-approved API decls in a
  // separate `auto_approved` list. It writes NOTHING and fires NO verdict —
  // auto-approval is realized only later at the explicit POST /api/ship.
  function fetchSubdeck(issue, skipApi) {
    var q = skipApi ? "?skip_api=1" : "?skip_api=0";
    return getJSON("/api/subdeck/" + encodeURIComponent(issue) + q);
  }
  function fetchStatus() {
    return getJSON("/api/status");
  }
  function fetchReviewWorkspace(chapter) {
    var query = chapter === "all"
      ? "?chapter=all"
      : chapter != null && chapter !== ""
        ? "?chapter=" + encodeURIComponent(chapter)
        : "";
    return getJSON("/api/review-workspace" + query);
  }
  function fetchReviewSource(path) {
    return getJSON("/api/review-source?path=" + encodeURIComponent(path))
      .then(function (payload) {
        // A whole-file reload is an explicit observation point for Aristotle's
        // worktree changes. Refresh the compact term index in the background.
        refreshDefinitionTerms(null, true);
        return payload;
      });
  }
  function fetchProjectGraph(force) {
    return getJSON("/api/project-graph" + (force ? "?refresh=1" : ""));
  }
  function fetchDefinitionTerms() {
    return getJSON("/api/definition-terms");
  }
  function fetchDefinition(name) {
    var url = "/api/definition?name=" + encodeURIComponent(name);
    if (state.issue != null) url += "&issue=" + encodeURIComponent(state.issue);
    return getJSON(url);
  }
  function fetchVersoStatus() {
    return getJSON("/api/verso-status");
  }
  function fetchVersoManifest() {
    return getJSON("/api/verso-manifest");
  }
  function retryVersoBuild() {
    return postJSON("/api/verso-retry", {});
  }
  function fetchReconciliation(issue) {
    return getJSON("/api/reconciliation/" + encodeURIComponent(issue));
  }

  // --- network: the token-bearing POST primitive ---------------------------
  // postJSON attaches the per-session token (header + body fallback) and
  // unwraps the {ok:false,...} convention. It is the shared transport for
  // local verdicts, operational recovery, and the irreversible ship — their
  // consequences still live in separate named callers below.
  // Re-read the session token from a FRESH page load (the meta tag / global the
  // server injects). The token is minted per deck process, so after a deck
  // restart the in-memory SESSION_TOKEN goes stale; this refetches the live one
  // without a full reload. Resolves to the new token, or "" if none is available
  // (e.g. the fetch hit an oauth login page).
  function refreshSessionToken() {
    return fetch("/", { credentials: "same-origin", headers: { Accept: "text/html" } })
      .then(function (r) { return r.ok ? r.text() : ""; })
      .then(function (html) {
        var m = html.match(/name="marathon-session-token"\s+content="([^"]+)"/);
        var tok = m ? m[1] : "";
        if (tok && tok.indexOf("__MARATHON_SESSION_TOKEN__") === -1) {
          SESSION_TOKEN = tok;
          return tok;
        }
        return "";
      })
      .catch(function () { return ""; });
  }

  function postJSON(url, body, _retried) {
    if (!SESSION_TOKEN && !_retried) {
      // No token in-page yet — try to fetch one, then proceed.
      return refreshSessionToken().then(function (tok) {
        if (!tok) return Promise.reject(new Error("no session token — refusing to POST"));
        return postJSON(url, body, true);
      });
    }
    if (!SESSION_TOKEN) {
      return Promise.reject(new Error("no session token — refusing to POST"));
    }
    var payload = {};
    Object.keys(body || {}).forEach(function (k) { payload[k] = body[k]; });
    payload.token = SESSION_TOKEN; // documented body fallback
    var headers = {
      "Content-Type": "application/json",
      Accept: "application/json",
    };
    // per-session CSRF-style token on EVERY POST (header name MUST match the
    // committed backend; the body `token` is the documented fallback).
    headers[TOKEN_HEADER] = SESSION_TOKEN;
    return fetch(url, {
      method: "POST",
      headers: headers,
      credentials: "same-origin",
      body: JSON.stringify(payload),
    }).then(function (r) {
      return r.json().then(function (data) {
        // A STALE per-process token (deck was restarted since this page loaded)
        // 403s here. Refresh the token from a fresh page load and retry ONCE, so
        // a deck restart is transparent instead of a dead-end "invalid session
        // token". The _retried guard prevents an infinite loop.
        if (r.status === 403 && !_retried && data &&
            /session token/i.test(data.message || data.error || "")) {
          return refreshSessionToken().then(function (tok) {
            if (tok) return postJSON(url, body, true);
            throw new Error((data && data.message) || "invalid session token");
          });
        }
        if (!r.ok || (data && data.ok === false)) {
          var requestError = new Error(
            (data && (data.message || data.reason || data.error)) ||
              "request failed (" + r.status + ")"
          );
          // Preserve structured recovery data (notably Add Chapter's durable
          // partial issue IDs) without changing the normal Promise API.
          requestError.data = data || null;
          requestError.status = r.status;
          throw requestError;
        }
        return data;
      });
    });
  }

  // postDeclVerdict — LOCAL-ONLY. Records a per-(issue,decl) verdict in the
  // backend sidecar. It merges NOTHING, dispatches NO Aristotle, writes NO
  // ledger verdict. Safe to call on every swipe. Reject notes ride verbatim.
  function postDeclVerdict(issue, decl, verdict, note, declFingerprint) {
    var body = { issue: issue, decl: decl, verdict: verdict };
    if (typeof note === "string") body.note = note; // verbatim, no rewrite
    // Real declaration decisions are bound to the exact source + elaborated
    // dependency context that was visible when the human clicked.  The
    // reserved informal slot supplies an exact normalized-prose fingerprint.
    if (declFingerprint) body.decl_fingerprint = declFingerprint;
    return postJSON("/api/decl-verdict", body);
  }

  // Atomic LOCAL-ONLY batch verdicts for the file/chapter reader. The backend
  // validates every target and rejected-predecessor guard before replacing the
  // sidecar once; this never ships an issue or dispatches Aristotle.
  function postBatchDeclVerdicts(items) {
    return postJSON("/api/batch-decl-verdict", { items: items || [] });
  }

  // postDeclLock — LOCAL-ONLY. Toggles the opt-in HARD-FREEZE lock on a
  // declaration. A locked decl's signature is frozen at reject-ship so no
  // sibling-decl refine can change it; unlocked (soft) decls stay rewritable.
  function postDeclLock(issue, decl, locked, declFingerprint) {
    var body = { issue: issue, decl: decl, locked: !!locked };
    if (declFingerprint) body.decl_fingerprint = declFingerprint;
    return postJSON("/api/decl-lock", body);
  }

  // postShip — THE IRREVERSIBLE REVIEW CALL. Composes the per-issue outcome
  // from the subdeck and either merges the PR (all verified) or dispatches
  // Aristotle with the composed note (any rejected). Reached ONLY from
  // shipIssue() (the explicit ship-button handler).
  //
  // The `skip_api` flag rides the ship body so the BACKEND realizes the
  // API auto-approval AT SHIP (folding un-verdicted API decls into KEEP). The
  // toggle itself never POSTs; auto-approval happens ONLY here, at the
  // explicit token-bearing ship — never on the GET that assembled the view.
  function postShip(issue, skipApi, reviewToken, requestId) {
    return postJSON("/api/ship", {
      issue: issue,
      skip_api: !!skipApi,
      review_token: reviewToken || null,
      request_id: requestId || null,
    });
  }

  // postDropCitation — A GitHub WRITE (token-gated, HUMAN-TRIGGERED). Drops a
  // confirmed-dead stale citation from the issue body so the issue reviews
  // cleanly. The backend re-validates that it is cited, gone from code, has no
  // replacement, and is not the issue's final anchor. Any failed condition is
  // REFUSED ({ok:false}). Reached ONLY from the footnote's per-citation button.
  function postDropCitation(issue, decl) {
    return postJSON("/api/drop-citation", { issue: issue, decl: decl });
  }

  function postReconciliationDecision(issue, proposalToken, action, kind, mapping) {
    var body = {
      issue: issue,
      proposal_token: proposalToken,
      action: action,
      kind: kind,
      old: mapping.old,
    };
    if (kind === "rename") body.new = mapping.new;
    else body.targets = mapping.targets || [];
    return postJSON("/api/reconcile-proposal", body);
  }

  // The manual "refresh issue body from code" write was RETIRED: the conductor
  // now auto-reconciles signatures to the live code (ADD uncited in-code
  // result-key decls + replace git-proven renames; never drop a stale citation
  // on a guess) whenever HEAD moves. There is no client-side push-refresh.

  // postMissing — LOCAL-ONLY. Records (or clears, with "") the issue's
  // "something is MISSING" note. Merges NOTHING, dispatches NO Aristotle; the
  // note only takes effect at the explicit ship (it routes the outcome to
  // Aristotle with an ADD section). Safe to call freely.
  function postMissing(issue, note) {
    return postJSON("/api/missing", { issue: issue, note: note });
  }

  // postRecovery — queues ONE explicit recovery mode against the exact
  // server-rendered stuck-row snapshot. Branches, PR numbers, and SHAs are
  // deliberately absent from this body: the backend owns those refs and uses
  // `recovery_token` as a compare-and-swap guard if they change while the
  // dialog is open.
  function postRecovery(issue, action, recoveryToken, confirmDestructive) {
    return postJSON("/api/recovery", {
      issue: issue,
      action: action,
      recovery_token: recoveryToken,
      confirm_destructive: !!confirmDestructive,
    });
  }

  // --- skip-API toggle (per-session, persistent) ---------------------------
  // "Skip API" is a VIEW preference: when ON, the deck shows ONLY the
  // main-result declarations and lists the easy API decls that WILL be
  // auto-approved on ship. It is persisted per browser session so it survives
  // navigation, and is read back into state on load. Reading/writing the
  // preference is purely local — it never touches the network.
  var SKIP_API_KEY = "marathon-deck-skip-api";
  function readSkipApiPref() {
    try {
      return window.sessionStorage.getItem(SKIP_API_KEY) === "1";
    } catch (e) {
      return false;
    }
  }
  function writeSkipApiPref(on) {
    try {
      window.sessionStorage.setItem(SKIP_API_KEY, on ? "1" : "0");
    } catch (e) { /* sessionStorage unavailable — keep the in-memory state */ }
  }

  // --- app state -----------------------------------------------------------
  var state = {
    queue: [],          // [CardSummary] — ready ISSUES (the picker)
    building: 0,
    viewBuilding: 0,
    landedToday: 0,
    unresolved: 0,
    awaitingRefinement: 0,
    needsAttention: 0,
    needsCitationMapping: 0,
    awaitingAudit: 0,
    dependencyBlocked: 0,
    otherUnresolved: 0,
    issue: null,        // the open subdeck's issue number (or null)
    subdeck: null,      // the GET /api/subdeck payload for `issue`
    currentDecl: null,  // the DeclCard currently on the stage (or null)
    digest: null,       // the GET /api/digest payload (landing view context)
    digestFetchedAt: 0, // debounce for digest refreshes
    landingRows: null,  // the queue rows the landing rendered (keyboard nav)
    landingFocus: 0,    // focused row index on the landing
    autoQueueRefreshAt: 0, // debounce for status-driven queue refetches
    showSummary: false, // the confirm-and-ship summary card is up
    busy: false,        // a POST is in flight (locks input)
    activeShip: null,   // durable non-secret {issue,operationId,statusUrl}
    shipPollSeq: 0,     // cancels stale poll continuations after terminal state
    conductorStartBusy: false, // explicit Status-tab recovery POST is in flight
    conductorStartError: "",   // durable across the five-second status rerender
    skipApi: false,     // "Skip API" view toggle (default OFF; per-session)
    informalActive: false,   // the leading informal-approval card is on the stage
    informalDismissed: false, // "decide later" on the informal card (this session)
    // Names the human PULLED BACK INTO REVIEW from the auto-approved panel
    // (an override). These are kept across a skip-API refetch so an override
    // survives re-rendering; they are re-merged into the visible deck. This
    // is purely local view state — pulling one back does NOT post anything.
    overrides: {},      // { issueNum: { declName: true } }
    apiPanelOpen: false, // the auto-approved panel's expand/collapse state
    rejectComposerOpen: false, // inline/sidebar editor; never obscures source
    // A rejected local predecessor makes verify/reject on its dependents a
    // two-step choice.  Overrides are per issue+decl and live only in this tab.
    dependencyOverrides: {},
    subdeckRequestSeq: 0, // discards stale async GETs after issue/navigation changes
    // Queue is the primary Review snapshot; parked-recovery data enriches it
    // independently. A retry aborts both reads, and generation guards make
    // older responses inert even when AbortController is unavailable.
    queueRequest: null,
    queueAbortController: null,
    queueStuckRequest: null,
    queueStuckAbortController: null,
    queueRequestSeq: 0,
    queueSnapshotKey: null,
    queueRenderedKey: null,
    queueFreshness: null,
    queueWarmRetryTimer: null,
    reviewStuck: { issues: [], repo: null },
    reviewStuckChecked: false,
    reviewStuckPending: false,
    reviewStuckUnavailable: false,
    // The lightweight conductor snapshot bundled into /api/stuck lets an
    // empty Review card explain (and repair) a stopped host once that
    // independent best-effort read settles.
    reviewConductor: null,
    activeTab: "review", // review | files | graph | status | issues | history
    filesChapter: "",   // "" = server default, "all" = project-wide reader
    filesData: null,
    filesQuery: "",
    filesFilter: "all", // all | provisional | unreviewed | rejected | changed | surfaced
    filesFocus: null,
    filesResumeScroll: null,
    filesResumeName: null,
    filesBusy: false,
    graphData: null,
    graphFilter: { chapter: "all", status: "all", query: "" },
    // The useful default is the project-level issue dependency graph.  The
    // mixed issue→declaration ownership picture remains available as an
    // explicitly-labelled map, but it is not dependency topology.
    graphView: { zoom: 1, mode: "issues" },
    definitionTerms: { names: [], short: {} },
    definitionNameSet: {},
    definitionSourceToken: null,
    definitionTermsRequest: null,
    definitionTermsRequestToken: null,
    definitionTermsQueued: null,
    definitionTermsRequestSeq: 0,
    definitionRequestSeq: 0,
    reviewInspectorGeneration: 0,
    // Verso is an optional, asynchronously-built semantic view of the same
    // source. Raw current source is always painted first; these fields only
    // control a progressive enhancement and can never block review.
    versoStatus: null,
    versoManifest: null,
    versoMetadataRequest: null,
    versoMetadataEpoch: 0,
    versoDocsRequest: null,
    versoDocsRequestToken: null,
    versoDocs: null,
    versoDocsToken: null,
    versoPages: Object.create(null),
    versoObservedSourceToken: null,
    versoPollTimer: null,
    versoPollAttempts: 0,
    versoRetryBusy: false,
    versoFragmentSeq: 0,
    versoHoverController: null,
    reconciliation: null,
    reconciliationBusy: false,
    // Dashboard reads use the same single-flight/stale-response discipline as
    // Review.  Snapshot keys let an unchanged poll leave the DOM (and focus)
    // exactly where it is.
    issuesRequest: null,
    issuesAbortController: null,
    issuesRequestSeq: 0,
    issuesSnapshotKey: null,
    issuesStuck: { issues: [], repo: null },
    statusDashboardRequest: null,
    statusDashboardAbortController: null,
    statusDashboardRequestSeq: 0,
    statusDashboardSnapshotKey: null,
  };
  state.skipApi = readSkipApiPref();

  // Keep an already-useful surface mounted while a manual retry is in flight.
  // The small, absolutely-positioned status avoids the loader → empty → stuck
  // sequence that used to make Review and Issues visibly flash.
  function surfaceHasStableContent(host) {
    if (!host) return false;
    var kids = host.children || [];
    for (var i = 0; i < kids.length; i++) {
      var child = kids[i];
      if (child.getAttribute &&
          child.getAttribute("data-marathon-refresh-indicator") === "1") continue;
      if (child.classList &&
          (child.classList.contains("card-loading") ||
           child.classList.contains("dash-loading"))) continue;
      return true;
    }
    return false;
  }

  function beginSurfaceRefresh(host, loadingClass, loadingText) {
    if (!host) return false;
    var hadContent = surfaceHasStableContent(host);
    host.setAttribute("aria-busy", "true");
    if (!hadContent) {
      var existingLoader = host.querySelector("." + loadingClass);
      if (!existingLoader) {
        host.innerHTML = "";
        host.appendChild(el("div", {
          class: loadingClass,
          text: loadingText,
        }));
      }
      return false;
    }
    var indicator = host.querySelector(
      '[data-marathon-refresh-indicator="1"]'
    );
    if (!indicator) {
      indicator = el("div", {
        class: "surface-refresh-indicator",
        "data-marathon-refresh-indicator": "1",
        role: "status",
        "aria-live": "polite",
      }, [
        el("span", { class: "surface-refresh-spinner", "aria-hidden": "true" }),
        loadingText,
      ]);
      host.appendChild(indicator);
    }
    return true;
  }

  function endSurfaceRefresh(host) {
    if (!host) return;
    host.removeAttribute("aria-busy");
    var indicator = host.querySelector(
      '[data-marathon-refresh-indicator="1"]'
    );
    if (indicator) indicator.remove();
  }

  function marathonNodeKey(node) {
    return node && node.nodeType === 1
      ? node.getAttribute("data-status-key") : null;
  }

  // Reconcile a detached status snapshot into the mounted dashboard. Stable
  // keyed rows retain their DOM identity, event handlers, focus, and scroll;
  // only text/attributes/children that actually changed are updated.
  function syncDomAttributes(current, fresh) {
    var wanted = Object.create(null);
    var freshAttrs = Array.prototype.slice.call(fresh.attributes || []);
    freshAttrs.forEach(function (attr) {
      wanted[attr.name] = true;
      if (current.getAttribute(attr.name) !== attr.value) {
        current.setAttribute(attr.name, attr.value);
      }
    });
    Array.prototype.slice.call(current.attributes || []).forEach(function (attr) {
      if (!wanted[attr.name]) current.removeAttribute(attr.name);
    });
    if (current.tagName === "INPUT") {
      current.checked = fresh.checked;
      current.disabled = fresh.disabled;
    } else if (current.tagName === "BUTTON" ||
               current.tagName === "SELECT" ||
               current.tagName === "TEXTAREA") {
      current.disabled = fresh.disabled;
    }
  }

  function syncDomNode(current, fresh) {
    if (!current || current.nodeType !== fresh.nodeType ||
        (current.nodeType === 1 && current.tagName !== fresh.tagName)) {
      if (current && current.parentNode) current.parentNode.replaceChild(fresh, current);
      return fresh;
    }
    if (current.nodeType === 3) {
      if (current.nodeValue !== fresh.nodeValue) current.nodeValue = fresh.nodeValue;
      return current;
    }
    if (current.nodeType !== 1) return current;
    syncDomAttributes(current, fresh);
    syncDomChildren(current, fresh);
    return current;
  }

  function syncDomChildren(current, fresh) {
    var desired = Array.prototype.slice.call(fresh.childNodes || []);
    desired.forEach(function (freshChild, index) {
      var key = marathonNodeKey(freshChild);
      var target = null;
      if (key) {
        for (var i = index; i < current.childNodes.length; i++) {
          if (marathonNodeKey(current.childNodes[i]) === key) {
            target = current.childNodes[i];
            break;
          }
        }
      } else {
        target = current.childNodes[index] || null;
      }
      if (!target) {
        current.insertBefore(freshChild, current.childNodes[index] || null);
        return;
      }
      if (current.childNodes[index] !== target) {
        current.insertBefore(target, current.childNodes[index] || null);
      }
      syncDomNode(target, freshChild);
    });
    while (current.childNodes.length > desired.length) {
      current.removeChild(current.lastChild);
    }
  }

  function repaintDefinitionSurfaces() {
    if (state.activeTab === "review" && state.subdeck) {
      // Definition-index updates change only which identifiers are clickable.
      // Patch those code nodes in place so the active card, inspector, focus,
      // semantic-source scroll, and page position are not torn down.
      if (state.currentDecl) {
        refreshReviewDefinitionLinks(state.currentDecl);
      }
    } else if (state.activeTab === "files" && state.filesData) {
      refreshReaderDefinitionLinks();
    }
  }

  function installDefinitionTerms(terms) {
    terms = terms || { names: [], short: {} };
    var token = terms.source_token || JSON.stringify(terms.names || []);
    if (token === state.definitionSourceToken) return false;
    state.definitionTerms = terms;
    state.definitionNameSet = Object.create(null);
    (terms.names || []).forEach(function (name) {
      state.definitionNameSet[name] = true;
    });
    state.definitionSourceToken = token;
    repaintDefinitionSurfaces();
    return true;
  }

  // Single-flight source-term refresh. Status/SSE, a subdeck reload, and a
  // whole-file source read can arrive together; coalesce identical observations
  // and queue at most one follow-up when source may have moved mid-request.
  function refreshDefinitionTerms(expectedToken, force) {
    expectedToken = expectedToken || null;
    if (!force && expectedToken && expectedToken === state.definitionSourceToken) {
      return Promise.resolve(state.definitionTerms);
    }
    if (state.definitionTermsRequest) {
      var queued = state.definitionTermsQueued;
      var shouldQueue = !!queued || !!force ||
        !!(expectedToken && expectedToken !== state.definitionTermsRequestToken);
      if (shouldQueue) {
        if (!queued ||
            queued.expectedToken !== expectedToken ||
            queued.force !== !!force) {
          state.definitionTermsRequestSeq += 1;
        }
        state.definitionTermsQueued = {
          expectedToken: expectedToken,
          force: !!force,
        };
      }
      return state.definitionTermsRequest;
    }

    var generation = ++state.definitionTermsRequestSeq;
    state.definitionTermsRequestToken = expectedToken;
    var flight = fetchDefinitionTerms()
      .then(function (terms) {
        // A newer source observation arrived while this GET was pending. Do
        // not briefly install/repaint the superseded index; the queued read
        // below will install the newest snapshot once.
        if (generation === state.definitionTermsRequestSeq) {
          installDefinitionTerms(terms);
        }
        return terms;
      })
      .catch(function () { return null; });
    state.definitionTermsRequest = flight.then(function (result) {
      var queued = state.definitionTermsQueued;
      state.definitionTermsRequest = null;
      state.definitionTermsRequestToken = null;
      state.definitionTermsQueued = null;
      if (queued) {
        refreshDefinitionTerms(queued.expectedToken, queued.force);
      }
      return result;
    });
    return state.definitionTermsRequest;
  }

  function overrideSet() {
    var k = String(state.issue);
    if (!state.overrides[k]) state.overrides[k] = {};
    return state.overrides[k];
  }
  function isOverridden(name) {
    var k = String(state.issue);
    return !!(state.overrides[k] && state.overrides[k][name]);
  }

  function readyIssues() {
    return state.queue.filter(function (c) { return c.ready; });
  }

  function provisionalIssues() {
    return state.queue.filter(function (c) {
      return !!c.inspectable && !!c.provisional && !c.ready;
    });
  }

  // The next UNREVIEWED card in the subdeck (verdict null) — these are the
  // cards that still need a decision. Deferred cards are revisitable but do
  // NOT block; the backend's `complete` flips only when none remain null.
  function firstUnreviewed(cards) {
    for (var i = 0; i < (cards || []).length; i++) {
      if (!cards[i].verdict) return cards[i];
    }
    return null;
  }
  function findDecl(name) {
    var cards = (state.subdeck && state.subdeck.cards) || [];
    for (var i = 0; i < cards.length; i++) {
      if (cards[i].name === name) return cards[i];
    }
    return null;
  }

  // Resolve the declarations in THIS issue that a card's elaborated kernel
  // mentions.  The backend already gives every DeclCard its project-local
  // kernel; using that data here keeps the review order definition-first even
  // when the wire order is severity-first.  Exact qualified names win; a
  // unique suffix match is a conservative fallback for older snapshots.
  function localDependencyNames(card, cards) {
    cards = cards || [];
    var names = cards.map(function (c) { return c.name; });
    var out = [];
    ((card && card.kernel) || []).forEach(function (member) {
      var raw = member && member.name;
      if (!raw || raw === card.name) return;
      var match = names.indexOf(raw) >= 0 ? raw : null;
      if (!match) {
        var suffixMatches = names.filter(function (name) {
          return name === raw || name.slice(-(raw.length + 1)) === "." + raw ||
            raw.slice(-(name.length + 1)) === "." + name;
        });
        if (suffixMatches.length === 1) match = suffixMatches[0];
      }
      if (match && out.indexOf(match) < 0) out.push(match);
    });
    return out;
  }

  // Stable topological sort of declarations within one issue.  Definitions
  // therefore appear before the theorems that rely on them.  Independent
  // declarations retain their backend order, and cycles retain their original
  // order rather than disappearing from the review deck.
  function dependencyOrder(cards) {
    cards = (cards || []).slice();
    var byName = {};
    var originalIndex = {};
    cards.forEach(function (card, i) {
      byName[card.name] = card;
      originalIndex[card.name] = i;
    });
    var indegree = {};
    var dependents = {};
    cards.forEach(function (card) {
      var deps = localDependencyNames(card, cards);
      indegree[card.name] = deps.length;
      deps.forEach(function (dep) {
        if (!dependents[dep]) dependents[dep] = [];
        dependents[dep].push(card.name);
      });
    });
    var ready = cards.filter(function (card) { return indegree[card.name] === 0; });
    var out = [];
    var placed = {};
    function stableReadySort() {
      ready.sort(function (a, b) {
        return originalIndex[a.name] - originalIndex[b.name];
      });
    }
    stableReadySort();
    while (ready.length) {
      var card = ready.shift();
      if (placed[card.name]) continue;
      placed[card.name] = true;
      out.push(card);
      (dependents[card.name] || []).forEach(function (name) {
        indegree[name] -= 1;
        if (indegree[name] === 0) ready.push(byName[name]);
      });
      stableReadySort();
    }
    cards.forEach(function (card) {
      if (!placed[card.name]) out.push(card); // cyclic remainder
    });
    return out;
  }

  function normalizeSubdeck(sd) {
    if (!sd) return sd;
    sd.cards = dependencyOrder(sd.cards || []);
    return sd;
  }

  function rejectedLocalDependencies(card) {
    var cards = (state.subdeck && state.subdeck.cards) || [];
    return localDependencyNames(card, cards).filter(function (name) {
      var dep = findDecl(name);
      return dep && dep.verdict === "rejected";
    });
  }

  function dependencyOverrideKey(card) {
    return String(state.issue) + "::" + ((card && card.name) || "");
  }

  // A partial reject has already produced a safe ship checkpoint, even if the
  // reviewer explicitly continued into a later declaration. Keep that
  // checkpoint reachable from every subsequent card so closing a dependency
  // guard can never strand the reviewer away from SHIP.
  function hasPausedReviewSummary() {
    var sd = state.subdeck;
    var rejected = (sd && sd.summary && sd.summary.rejected) || [];
    return rejected.length > 0 && partialWaitCards().length > 0;
  }

  function returnToPausedReviewSummary() {
    if (!hasPausedReviewSummary()) return;
    if (state.rejectComposerOpen) {
      flashToast("Finish or cancel the rejection note before leaving this declaration");
      return;
    }
    closeReviewInspector();
    state.showSummary = true;
    state.currentDecl = null;
    renderSubdeckStage();
  }

  function pausedSummaryButton() {
    if (state.showSummary || !hasPausedReviewSummary()) return null;
    return el("button", {
      class: "btn btn-tiny paused-summary-btn",
      type: "button",
      "data-no-swipe": "true",
      title: "Return to the partial KEEP/FIX/WAIT checkpoint and ship the upstream fix",
      onclick: function (event) {
        event.stopPropagation();
        returnToPausedReviewSummary();
      },
    }, ["Return to paused review summary"]);
  }

  // === reconciliation read-model (PURE READ — never POSTs) =================
  // The reconciliation is computed by the BACKEND on the GET /api/subdeck read
  // (it drops stale-cited decls into `stale_citations`, surfaces uncited
  // in-code decls as cards with surfaced:true, and summarises the drift in
  // `reconcile`). The frontend only READS these fields off the subdeck payload
  // to render the footnote / the surfaced badge. None of these helpers writes
  // anything — signature drift is reconciled to the code by the CONDUCTOR
  // (server-side, when HEAD moves), not by any client write.

  // The stale-cited decls (cited by the issue body but in_code=false) — shown
  // as a non-swipeable FOOTNOTE, never as cards. Tolerates an older backend
  // that omits the field (=> []).
  function staleCitations() {
    var sd = state.subdeck;
    return (sd && sd.stale_citations) || [];
  }
  // The reconcile summary {dropped:[name], surfaced:[name]} vs the raw issue
  // citations. Tolerates absence (=> {dropped:[], surfaced:[]}).
  function reconcileSummary() {
    var sd = state.subdeck;
    var r = (sd && sd.reconcile) || {};
    return { dropped: r.dropped || [], surfaced: r.surfaced || [] };
  }
  // Drift = the reconciled view differs from the raw issue citations: there are
  // stale citations to drop and/or surfaced decls to add. The push-refresh
  // button is ENABLED only when this is true (a refresh on a non-drifting issue
  // would be a no-op write).
  function hasReconcileDrift() {
    var rec = reconcileSummary();
    return staleCitations().length > 0 ||
      rec.dropped.length > 0 || rec.surfaced.length > 0;
  }

  // === rendering ===========================================================

  function tierBadge(tier, qualifiers) {
    var badge = el("span", { class: "tier-badge " + tierClass(tier) }, [
      el("span", { class: "tier-label", text: tier || "—" }),
    ]);
    (qualifiers || []).forEach(function (q) {
      badge.appendChild(el("span", { class: "tier-qual", text: q }));
    });
    return badge;
  }

  function evidenceRow(ev) {
    ev = ev || {};
    var items = [];
    var axioms = ev.axioms_beyond_whitelist || [];
    items.push(
      el("span", {
        class: "ev-chip " + (axioms.length ? "ev-bad" : "ev-ok"),
        title: "axioms beyond the whitelist",
      }, [
        "axioms: " + (axioms.length ? axioms.join(", ") : "clean"),
      ])
    );
    items.push(
      el("span", {
        class: "ev-chip " + (ev.sorry ? "ev-warn" : "ev-ok"),
      }, ["sorry: " + (ev.sorry == null ? "—" : ev.sorry ? "yes" : "no")])
    );
    var tags = ev.deception_tags || [];
    if (tags.length) {
      items.push(
        el("span", { class: "ev-chip ev-bad", title: "deception tags" }, [
          "deception: " + tags.join("; "),
        ])
      );
    } else {
      items.push(el("span", { class: "ev-chip ev-ok" }, ["no deception tags"]));
    }
    return el("div", { class: "evidence-row", "aria-label": "machine evidence" }, items);
  }

  function semanticDeltaBanner(delta) {
    if (!delta || !delta.class || delta.class === "unchanged") return null;
    var members = (delta.members || []).join(", ");
    return el("div", { class: "semantic-delta", role: "alert" }, [
      el("strong", { text: "semantic delta: " + delta.class }),
      members ? el("span", { class: "delta-members", text: " — " + members }) : null,
      el("div", {
        class: "delta-note",
        text: "advisory — meaning may have shifted; re-read before verifying.",
      }),
    ]);
  }

  // Has the issue's TOP-LEVEL informal statement been verified? `informalApproved`
  // => the human confirmed it on the leading approval card (verdict "verified"),
  // OR the top-level statement is not LLM-pending at all (is_llm false — already
  // verified / human-authored). Either way the per-card LLM-rendered warning is
  // REPLACED by a verified chip. `informalRejected` => flagged wrong; warning stays.
  function informalApproved() {
    var inf = state.subdeck && state.subdeck.informal;
    if (!inf) return false;
    return inf.verdict === "verified" || inf.is_llm === false;
  }
  function informalRejected() {
    var inf = state.subdeck && state.subdeck.informal;
    return !!(inf && inf.verdict === "rejected");
  }

  // The informal rendering. `informal_is_llm` (per the DeclCard contract) gates
  // the LLM-rendered warning flag — a human-authored rendering is not flagged.
  // Once the human approves the subdeck's informal text on the leading approval
  // card (`informalApproved`), the warning is replaced by a "verified by you"
  // chip across every card. The text is LaTeX-bearing; render AFTER it is in the
  // DOM, and degrade to raw LaTeX (never blank) when the math renderer is offline.
  // Pretty-print a pinned-pp Lean type. The audit stores the elaborated type as
  // ONE long line (`{E} → [inst] → (I) → … → T`); render each binder / hypothesis
  // on its own line by breaking at TOP-LEVEL function arrows only. Depth-tracking
  // keeps an arrow INSIDE a binder type (`(f : A → B)`) in place, and requiring a
  // trailing space keeps decorated arrows like `→ₗ[ℝ]` / `→+` intact.
  function prettyLeanType(s) {
    if (!s) return s;
    var OPEN = "([{⟨⦃";   // ( [ { ⟨ ⦃
    var CLOSE = ")]}⟩⦄";  // ) ] } ⟩ ⦄
    var out = "";
    var depth = 0;
    for (var i = 0; i < s.length; i++) {
      var ch = s[i];
      if (OPEN.indexOf(ch) >= 0) depth++;
      else if (CLOSE.indexOf(ch) >= 0) depth = Math.max(0, depth - 1);
      out += ch;
      if (depth === 0 && ch === "→" && s[i + 1] === " ") {
        out += "\n";
        i++; // consume the space after the arrow so the next line starts clean
      }
    }
    return out;
  }

  function informalBlock(card) {
    // No per-decl informal rendering is generated in the review workflow (the
    // spec_auditor pipeline that fills it is not run), so rather than show an
    // empty "(no informal rendering available)" box on every card, render
    // nothing when there is no text. If the rendering is ever populated it
    // appears automatically (with its LLM/verified flag).
    var txt = card.informal_rendering;
    if (!txt) return null;
    var wrap = el("div", { class: "informal" });
    if (card.informal_is_llm && informalApproved()) {
      wrap.appendChild(
        el("div", { class: "informal-flag informal-flag-ok" }, [
          "✅ informal rendering verified by you",
        ])
      );
    } else if (card.informal_is_llm) {
      wrap.appendChild(
        el("div", { class: "informal-flag" }, [
          "⚠️ LLM-rendered — verify against the source",
        ])
      );
    }
    var math = el("div", { class: "informal-text" });
    math.dataset.latexSource = txt;
    wrap.appendChild(math);
    // Render math AFTER it is in the DOM; degrades to raw LaTeX when offline.
    if (window.MarathonLatex) {
      try { window.MarathonLatex.render(math); }
      catch (e) { math.textContent = txt; }
    } else {
      math.textContent = txt;
    }
    return wrap;
  }

  // --- Lean syntax highlighter (offline, no CDN) ---------------------------
  // Returns an array of DOM nodes (text nodes + styled spans) — built with
  // textContent, never innerHTML, so source containing <, >, & is safe.
  // Block comments / docstrings are matched first so keywords inside them
  // are not separately coloured.
  var LEAN_KW = [
    "abbrev", "attribute", "by", "calc", "class", "def", "deriving", "do",
    "else", "end", "example", "extends", "forall", "from", "fun", "have",
    "if", "import", "in", "inductive", "instance", "lemma", "let", "macro",
    "match", "mutual", "namespace", "noncomputable", "notation", "open",
    "private", "protected", "scoped", "section", "set_option", "show",
    "structure", "suffices", "then", "theorem", "universe", "variable",
    "where", "with",
  ].join("|");
  var LEAN_RE = new RegExp(
    "(\\/-[\\s\\S]*?-\\/)|(--[^\\n]*)|(\"(?:[^\"\\\\]|\\\\.)*\")|" +
      "(\\bsorry\\b)|\\b(" + LEAN_KW + ")\\b",
    "g"
  );
  function leanSpan(cls, txt) {
    var s = document.createElement("span");
    s.className = cls;
    s.textContent = txt;
    return s;
  }

  // Map both qualified kernel names and unambiguous short names to their
  // definitions.  Ambiguous short names are deliberately not clickable.
  function kernelTermLookup(kernel) {
    var lookup = Object.create(null);
    var short = Object.create(null);
    (kernel || []).forEach(function (member) {
      if (!member || !member.name) return;
      lookup[member.name] = member;
      var bits = member.name.split(".");
      var tail = bits[bits.length - 1];
      if (!(tail in short)) short[tail] = member;
      else short[tail] = null;
    });
    Object.keys(short).forEach(function (name) {
      if (short[name] && !(name in lookup)) lookup[name] = short[name];
    });
    return lookup;
  }

  function leanReference(member, label) {
    var ref = leanSpan("lh-ref", label);
    ref.setAttribute("role", "button");
    ref.setAttribute("tabindex", "0");
    ref.setAttribute("data-no-swipe", "true");
    var lookupName = (member && member.name) || label;
    ref.setAttribute("title", "Inspect " + lookupName);
    ref.setAttribute("aria-label", "Inspect definition of " + lookupName);
    ref.addEventListener("click", function (e) {
      e.stopPropagation();
      showDefinitionLookup(label, member);
    });
    ref.addEventListener("keydown", function (e) {
      if (e.key === "Enter" || e.key === " ") {
        e.preventDefault();
        showDefinitionLookup(label, member);
      }
    });
    return ref;
  }

  function projectTermCandidates(label) {
    if (state.definitionNameSet[label]) return [label];
    var short = (state.definitionTerms && state.definitionTerms.short) || {};
    return Object.prototype.hasOwnProperty.call(short, label) ? short[label] : [];
  }

  // Highlight references only in ordinary code text; comments and strings are
  // emitted by leanHighlight as atomic spans and never receive fake links.
  function leanPlainNodes(txt, kernel) {
    var lookup = kernelTermLookup(kernel);
    // Tokenize ordinary identifiers, then link only names present in the
    // current-source project index (or the card's audit kernel). This avoids
    // making local variables/keywords look resolvable while still supporting
    // project identifiers outside the minimized statement kernel.
    var re = /(^|[^A-Za-z0-9_'.])([A-Za-z_][A-Za-z0-9_']*(?:\.[A-Za-z_][A-Za-z0-9_']*)*)(?=[^A-Za-z0-9_'.]|$)/g;
    var nodes = [];
    var last = 0;
    var m;
    while ((m = re.exec(txt))) {
      if (m.index > last) nodes.push(document.createTextNode(txt.slice(last, m.index)));
      if (m[1]) nodes.push(document.createTextNode(m[1]));
      var member = lookup[m[2]];
      var candidates = projectTermCandidates(m[2]);
      if (member || candidates.length) nodes.push(leanReference(member || null, m[2]));
      else nodes.push(document.createTextNode(m[2]));
      last = re.lastIndex;
      if (re.lastIndex === m.index) re.lastIndex++;
    }
    if (last < txt.length) nodes.push(document.createTextNode(txt.slice(last)));
    return nodes;
  }

  function leanHighlight(src, kernel) {
    var nodes = [];
    var last = 0;
    var m;
    LEAN_RE.lastIndex = 0;
    while ((m = LEAN_RE.exec(src))) {
      if (m.index > last) {
        nodes = nodes.concat(leanPlainNodes(src.slice(last, m.index), kernel));
      }
      if (m[1] || m[2]) nodes.push(leanSpan("lh-comment", m[1] || m[2]));
      else if (m[3]) nodes.push(leanSpan("lh-string", m[3]));
      else if (m[4]) nodes.push(leanSpan("lh-sorry", m[4]));
      else if (m[5]) nodes.push(leanSpan("lh-kw", m[5]));
      last = LEAN_RE.lastIndex;
      if (LEAN_RE.lastIndex === m.index) LEAN_RE.lastIndex++;  // zero-width guard
    }
    if (last < src.length) nodes = nodes.concat(leanPlainNodes(src.slice(last), kernel));
    return nodes;
  }

  function replaceLeanHighlight(host, src, kernel) {
    if (!host) return;
    var focused = document.activeElement;
    var focusedLabel = focused && host.contains(focused) &&
      focused.classList && focused.classList.contains("lh-ref")
      ? focused.textContent : null;
    while (host.firstChild) host.removeChild(host.firstChild);
    leanHighlight(src || "", kernel || []).forEach(function (node) {
      host.appendChild(node);
    });
    if (focusedLabel) {
      var refs = host.querySelectorAll(".lh-ref");
      for (var i = 0; i < refs.length; i++) {
        if (refs[i].textContent === focusedLabel) {
          refs[i].focus();
          break;
        }
      }
    }
  }

  function refreshReviewDefinitionLinks(card) {
    var active = stage().querySelector("#active-card");
    if (!active || !card) return;
    var source = active.querySelector(".source-block code.lean");
    if (source && card.source_block) {
      replaceLeanHighlight(source, card.source_block, card.kernel);
    }
    var statement = active.querySelector(".statement-pp");
    if (statement && card.statement_pp) {
      replaceLeanHighlight(
        statement, prettyLeanType(card.statement_pp), card.kernel
      );
    }
  }

  function refreshReaderDefinitionLinks() {
    var byName = Object.create(null);
    readerTargets(state.filesData).forEach(function (target) {
      byName[target.name] = target;
    });
    var articles = document.querySelectorAll(
      "#tab-files .reader-decl[data-decl-name]"
    );
    for (var i = 0; i < articles.length; i++) {
      var target = byName[articles[i].getAttribute("data-decl-name")];
      if (!target) continue;
      var source = articles[i].querySelector(".reader-source code.lean");
      if (source && target.source_block) {
        replaceLeanHighlight(source, target.source_block, []);
      }
      var statement = articles[i].querySelector(".statement-pp");
      if (statement && target.statement_pp) {
        replaceLeanHighlight(
          statement, prettyLeanType(target.statement_pp), []
        );
      }
    }
  }

  // --- Verso semantic source (progressive enhancement) --------------------
  // Verso's generated HTML is elaboration-aware: it knows bindings, resolved
  // constants, types, docstrings, and tactic states. The current raw source is
  // still rendered immediately and remains available behind a disclosure.
  //
  // SECURITY: generated pages and -verso-docs.json are build artifacts, not
  // trusted application markup. They are parsed in an inert document and
  // copied node-by-node through the allowlist below. No generated string is
  // ever assigned to innerHTML, and no script or stylesheet from the artifact
  // is installed into Marathon's privileged document.
  var VERSO_ALLOWED_TAGS = {
    A: true, BLOCKQUOTE: true, BR: true, CODE: true, DD: true, DIV: true,
    DEL: true, DL: true, DT: true, EM: true, H1: true, H2: true, H3: true, H4: true,
    H5: true, H6: true, HR: true, INPUT: true, KBD: true, LABEL: true,
    LI: true, OL: true, P: true, PRE: true, SPAN: true, STRONG: true,
    SUB: true, SUP: true, TABLE: true, TBODY: true, TD: true, TH: true,
    THEAD: true, TR: true, UL: true,
  };
  var VERSO_DROP_TAGS = {
    BASE: true, EMBED: true, FORM: true, FRAME: true, IFRAME: true,
    LINK: true, MATH: true, META: true, OBJECT: true, SCRIPT: true,
    STYLE: true, SVG: true, TEMPLATE: true,
  };
  var VERSO_DATA_ATTRS = {
    "data-binding": true,
    "data-lean-context": true,
    "data-verso-links": true,
    "data-verso-hover": true,
  };

  function versoRootPath() {
    var status = state.versoStatus || {};
    var manifest = state.versoManifest || {};
    // Once a manifest has been selected its immutable, token-scoped mount is
    // authoritative. This prevents a publication between the page and hover
    // requests from pairing code with another artifact's numeric hover IDs.
    var raw = typeof manifest.root_url === "string" ? manifest.root_url :
      (typeof status.root_url === "string" ? status.root_url : "/verso/");
    try {
      var parsed = new URL(raw, window.location.origin);
      if (parsed.origin !== window.location.origin) return "/verso/";
      var path = parsed.pathname || "/verso/";
      return path.slice(-1) === "/" ? path : path + "/";
    } catch (e) {
      return "/verso/";
    }
  }

  // Resolve a generated link underneath the same-origin Verso mount. Absolute
  // external URLs, protocol-relative URLs, javascript:, data:, and links that
  // escape the mount are discarded.
  function safeVersoURL(raw, pageRelative, artifactBase) {
    if (typeof raw !== "string" || !raw || raw.indexOf("//") === 0) return null;
    var root = versoRootPath();
    try {
      var rootURL = new URL(root, window.location.origin);
      var pageURL = rootURL;
      if (pageRelative) {
        if (/^[A-Za-z][A-Za-z0-9+.-]*:/.test(pageRelative)) {
          pageURL = new URL(pageRelative);
        } else if (String(pageRelative).indexOf(root) === 0) {
          pageURL = new URL(pageRelative, window.location.origin);
        } else if (String(pageRelative).charAt(0) === "/") {
          pageURL = new URL(root + String(pageRelative).replace(/^\/+/, ""), window.location.origin);
        } else {
          pageURL = new URL(pageRelative, rootURL);
        }
        if (pageURL.origin !== window.location.origin || pageURL.pathname.indexOf(root) !== 0) {
          return null;
        }
      }

      var parsed;
      if (/^[A-Za-z][A-Za-z0-9+.-]*:/.test(raw)) {
        parsed = new URL(raw);
      } else if (raw.indexOf(root) === 0) {
        parsed = new URL(raw, window.location.origin);
      } else if (raw.charAt(0) === "/") {
        parsed = new URL(root + raw.replace(/^\/+/, ""), window.location.origin);
      } else {
        // Verso's Link.relativeLink values are site-root-relative strings such
        // as `GeometricAnalysis/Other/#id`; the generated page's <base> tells
        // us where that site root lives. Explicit #, ./, ../, and ? references
        // retain ordinary page-relative URL semantics.
        var isExplicitRelative = raw.charAt(0) === "#" || raw.charAt(0) === "?" ||
          raw.indexOf("./") === 0 || raw.indexOf("../") === 0;
        var resolutionBase = pageURL;
        if (artifactBase && !isExplicitRelative) {
          var candidateBase = new URL(artifactBase, window.location.origin);
          if (candidateBase.origin === window.location.origin &&
              candidateBase.pathname.indexOf(root) === 0) {
            resolutionBase = candidateBase;
          }
        }
        parsed = new URL(raw, resolutionBase);
      }
      if (parsed.origin !== window.location.origin || parsed.pathname.indexOf(root) !== 0) {
        return null;
      }
      return parsed.pathname + parsed.search + parsed.hash;
    } catch (e) {
      return null;
    }
  }

  function versoDocumentBase(page, pageRelative) {
    var base = page && page.querySelector && page.querySelector("base[href]");
    if (!base) return null;
    var raw = base.getAttribute("href");
    if (!raw || raw.indexOf("//") === 0) return null;
    // Resolve in the artifact's *virtual* site root before remounting. Verso
    // emits bases such as ../../.. for pages that originally lived at `/`;
    // resolving that directly in the browser would climb above `/verso/`.
    // The dummy origin preserves the generated site's URL semantics while the
    // final safeVersoURL call confines the result to our same-origin mount.
    try {
      var root = versoRootPath();
      var safePage = safeVersoURL(pageRelative);
      if (!safePage) return null;
      var parsedPage = new URL(safePage, window.location.origin);
      var artifactPath = parsedPage.pathname.slice(root.length);
      var virtualOrigin = "https://marathon-verso.invalid/";
      var virtualPage = new URL(artifactPath + parsedPage.search + parsedPage.hash, virtualOrigin);
      var virtualBase = new URL(raw, virtualPage);
      if (virtualBase.origin !== new URL(virtualOrigin).origin) return null;
      var mounted = root + virtualBase.pathname.replace(/^\/+/, "") +
        virtualBase.search + virtualBase.hash;
      return safeVersoURL(mounted);
    } catch (e) {
      return null;
    }
  }

  function safeVersoClass(value) {
    value = String(value || "");
    return value.length <= 300 && /^[A-Za-z0-9 _:-]*$/.test(value) ? value : "";
  }

  function safeVersoScalar(value) {
    value = String(value || "");
    return value.length <= 1000 && !/[\u0000-\u001f]/.test(value) ? value : null;
  }

  function sanitizeVersoNode(source, pageRelative, artifactBase, idPrefix) {
    if (!source) return null;
    if (source.nodeType === 3) return document.createTextNode(source.nodeValue || "");
    if (source.nodeType !== 1) return null;
    var sourceTag = String(source.tagName || "").toUpperCase();
    if (VERSO_DROP_TAGS[sourceTag]) return null;
    // verso-html currently emits <emph> for Markdown emphasis. It is not a
    // standard HTML element, so normalize it to semantic <em> while copying
    // through the allowlist instead of flattening away the emphasis.
    var tag = sourceTag === "EMPH" ? "EM" : sourceTag;

    var target = VERSO_ALLOWED_TAGS[tag]
      ? document.createElement(tag.toLowerCase())
      : document.createDocumentFragment();
    if (VERSO_ALLOWED_TAGS[tag]) {
      var cls = safeVersoClass(source.getAttribute("class"));
      if (cls) target.setAttribute("class", cls);
      ["title", "role", "aria-label", "aria-hidden", "aria-expanded"].forEach(function (attr) {
        if (!source.hasAttribute(attr)) return;
        var value = safeVersoScalar(source.getAttribute(attr));
        if (value != null) target.setAttribute(attr, value);
      });
      if (source.hasAttribute("id")) {
        var sourceId = safeVersoScalar(source.getAttribute("id"));
        if (sourceId != null) target.setAttribute("id", (idPrefix || "") + sourceId);
      }
      Object.keys(VERSO_DATA_ATTRS).forEach(function (attr) {
        if (!source.hasAttribute(attr)) return;
        var value = safeVersoScalar(source.getAttribute(attr));
        if (value != null) target.setAttribute(attr, value);
      });
      if (tag === "A" && source.hasAttribute("href")) {
        var href = safeVersoURL(source.getAttribute("href"), pageRelative, artifactBase);
        if (href) {
          target.setAttribute("href", href);
          target.setAttribute("target", "_blank");
          target.setAttribute("rel", "noopener noreferrer");
        }
      }
      if (tag === "INPUT") {
        if (String(source.getAttribute("type") || "").toLowerCase() === "checkbox") {
          target.setAttribute("type", "checkbox");
          if (source.hasAttribute("checked")) target.checked = true;
        } else {
          // Verso uses only checkbox inputs for tactic-state toggles.
          return null;
        }
      }
      if (tag === "LABEL" && source.hasAttribute("for")) {
        var labelFor = safeVersoScalar(source.getAttribute("for"));
        if (labelFor != null) target.setAttribute("for", (idPrefix || "") + labelFor);
      }
    }
    for (var i = 0; i < source.childNodes.length; i++) {
      var child = sanitizeVersoNode(source.childNodes[i], pageRelative, artifactBase, idPrefix);
      if (child) target.appendChild(child);
    }
    return target;
  }

  function sanitizeVersoHTML(markup, pageRelative, artifactBase, idPrefix) {
    var fragment = document.createDocumentFragment();
    if (typeof markup !== "string") return fragment;
    var parsed = new DOMParser().parseFromString(markup, "text/html");
    for (var i = 0; i < parsed.body.childNodes.length; i++) {
      var child = sanitizeVersoNode(
        parsed.body.childNodes[i], pageRelative, artifactBase, idPrefix
      );
      if (child) fragment.appendChild(child);
    }
    return fragment;
  }

  function fetchVersoDocs() {
    var token = state.versoManifest && state.versoManifest.artifact_token;
    if (token && state.versoDocsToken === token && state.versoDocs) {
      return Promise.resolve(state.versoDocs);
    }
    if (state.versoDocsRequest && state.versoDocsRequestToken === token) {
      return state.versoDocsRequest;
    }
    var url = safeVersoURL("-verso-docs.json");
    if (!url) return Promise.resolve(null);
    state.versoDocsRequestToken = token || null;
    state.versoDocsRequest = getJSON(url)
      .then(function (docs) {
        var parsed = docs && typeof docs === "object" ? docs : null;
        if (token && state.versoManifest &&
            state.versoManifest.artifact_token === token) {
          state.versoDocs = parsed;
          state.versoDocsToken = token;
        }
        return parsed;
      })
      .catch(function () {
        if (state.versoDocsRequestToken === token) {
          state.versoDocsRequest = null;
          state.versoDocsRequestToken = null;
        }
        return null;
      });
    return state.versoDocsRequest;
  }

  function fetchVersoPage(relative) {
    var url = safeVersoURL(relative);
    if (!url) return Promise.reject(new Error("Unsafe Verso page URL"));
    var token = (state.versoManifest && state.versoManifest.artifact_token) || "unknown";
    var key = token + "\n" + url;
    if (state.versoPages[key]) return state.versoPages[key];
    state.versoPages[key] = fetch(url, {
      method: "GET",
      headers: { Accept: "text/html" },
      credentials: "same-origin",
    }).then(function (response) {
      if (!response.ok) {
        var error = new Error("HTTP " + response.status + " for " + url);
        error.httpStatus = response.status;
        throw error;
      }
      return response.text();
    }).then(function (markup) {
      return new DOMParser().parseFromString(markup, "text/html");
    }).catch(function (error) {
      delete state.versoPages[key];
      throw error;
    });
    return state.versoPages[key];
  }

  function ensureVersoMetadata(force) {
    var currentState = String((state.versoStatus && state.versoStatus.state) || "").toLowerCase();
    var isBuilding = currentState === "building" || currentState === "queued" || currentState === "idle";
    var selectedArtifactMatches = !!(
      state.versoManifest && state.versoStatus &&
      state.versoManifest.artifact_token &&
      state.versoManifest.artifact_token === state.versoStatus.artifact_token
    );
    if (!force && state.versoStatus && !isBuilding &&
        (!state.versoStatus.available || selectedArtifactMatches)) {
      return Promise.resolve({ status: state.versoStatus, manifest: state.versoManifest });
    }
    if (state.versoMetadataRequest) return state.versoMetadataRequest;
    var requestEpoch = state.versoMetadataEpoch;
    var previousStatus = state.versoStatus;
    var previousManifest = state.versoManifest;
    state.versoMetadataRequest = fetchVersoStatus()
      .then(function (status) {
        if (requestEpoch !== state.versoMetadataEpoch) {
          var superseded = new Error("Verso metadata request was superseded");
          superseded.versoMetadataSuperseded = true;
          throw superseded;
        }
        state.versoStatus = status || { state: "unavailable", available: false };
        if (!state.versoStatus.available) {
          if (!previousManifest) state.versoManifest = null;
          return null;
        }
        return fetchVersoManifest().then(function (manifest) {
          if (requestEpoch !== state.versoMetadataEpoch) {
            var superseded = new Error("Verso metadata request was superseded");
            superseded.versoMetadataSuperseded = true;
            throw superseded;
          }
          if (!manifest || typeof manifest !== "object") {
            throw new Error("Verso manifest is not an object");
          }
          if (!manifest.artifact_token || typeof manifest.artifact_token !== "string") {
            throw new Error("Verso manifest has no artifact token");
          }
          state.versoManifest = manifest;
          // The manifest is an atomic selection of one retained artifact. If
          // publication advanced after the preceding status read, keep the
          // badge honest and use this token for every page/docs/asset request.
          state.versoStatus.available = true;
          state.versoStatus.artifact_token = manifest.artifact_token;
          if (!previousManifest || previousManifest.artifact_token !== manifest.artifact_token) {
            state.versoPages = Object.create(null);
            state.versoDocs = null;
            state.versoDocsToken = null;
            state.versoDocsRequest = null;
            state.versoDocsRequestToken = null;
          }
          return manifest;
        });
      })
      .then(function () {
        return { status: state.versoStatus, manifest: state.versoManifest };
      })
      .catch(function (error) {
        if (error && error.versoMetadataSuperseded) {
          return { status: state.versoStatus, manifest: state.versoManifest };
        }
        var previousState = String((previousStatus && previousStatus.state) || "").toLowerCase();
        var wasBuilding = previousState === "building" || previousState === "queued" ||
          previousState === "idle";
        if (wasBuilding) {
          // A temporary status/manifest transport failure is not a backend
          // build failure. Preserve the transitional state so a long healthy
          // build continues polling instead of becoming permanently parked.
          state.versoStatus = Object.assign({}, previousStatus, {
            transport_error: (error && error.message) || "Could not poll Verso metadata",
          });
        } else {
          state.versoStatus = {
            state: "error",
            available: !!previousManifest,
            stale: !!previousManifest,
            root_url: "/verso/",
            error: (error && error.message) || "Could not load Verso metadata",
          };
        }
        state.versoManifest = previousManifest || null;
        return { status: state.versoStatus, manifest: state.versoManifest };
      });
    state.versoMetadataRequest = state.versoMetadataRequest.then(function (result) {
      if (requestEpoch === state.versoMetadataEpoch) {
        state.versoMetadataRequest = null;
        scheduleVersoStatusPoll(result.status);
      }
      return result;
    });
    return state.versoMetadataRequest;
  }

  // A first visit normally starts a background doc build and gets a truthful
  // `building` response. Retry without blocking the raw-source UI, then upgrade
  // every still-mounted surface when the last-good artifact becomes ready.
  // Poll quickly for the usual incremental build, then back off. The cap is
  // about 61 minutes, matching the build+render hard timeouts without letting a
  // forgotten tab poll forever.
  function scheduleVersoStatusPoll(status) {
    var statusState = String((status && status.state) || "").toLowerCase();
    var transitional = statusState === "building" || statusState === "queued" || statusState === "idle";
    var retryError = statusState === "error" && state.versoPollAttempts < 3;
    if (!transitional && !retryError) {
      if (state.versoPollTimer) clearTimeout(state.versoPollTimer);
      state.versoPollTimer = null;
      state.versoPollAttempts = 0;
      return;
    }
    if (state.versoPollTimer || state.versoPollAttempts >= 390) return;
    var delay = state.versoPollAttempts < 30 ? 2000 : 10000;
    state.versoPollTimer = setTimeout(function () {
      state.versoPollTimer = null;
      state.versoPollAttempts += 1;
      ensureVersoMetadata(true).then(function (metadata) {
        refreshMountedVersoSurfaces();
        scheduleVersoStatusPoll(metadata.status);
      });
    }, delay);
  }

  function versoConstantEntry(name) {
    var constants = state.versoManifest && state.versoManifest.constants;
    // The canonical manifest is keyed directly by qualified declaration name.
    // Also accept an explicitly nested `qualified` map for older prototypes.
    var qualified = constants && constants.qualified && !constants.qualified.url
      ? constants.qualified : constants;
    if (!qualified || typeof qualified !== "object") return null;
    if (qualified[name]) return qualified[name];
    var suffix = "." + name;
    var matches = Object.keys(qualified).filter(function (candidate) {
      return candidate === name || candidate.slice(-suffix.length) === suffix;
    });
    return matches.length === 1 ? qualified[matches[0]] : null;
  }

  function sourcePathToModule(path) {
    return String(path || "")
      .replace(/^\.\//, "")
      .replace(/\.lean$/, "")
      .replace(/^\/+|\/+$/g, "")
      .replace(/\//g, ".");
  }

  function versoModuleEntry(path) {
    var modules = state.versoManifest && state.versoManifest.modules;
    if (!modules || typeof modules !== "object") return null;
    var moduleName = sourcePathToModule(path);
    if (modules[moduleName]) return modules[moduleName];
    var normalizedPath = String(path || "").replace(/\\/g, "/").replace(/^\.\//, "");
    var matches = Object.keys(modules).filter(function (candidate) {
      var entry = modules[candidate] || {};
      return candidate.slice(-(moduleName.length + 1)) === "." + moduleName ||
        normalizedPath.slice(-(candidate.replace(/\./g, "/").length + 5)) ===
          candidate.replace(/\./g, "/") + ".lean" ||
        entry.path === path || entry.source_file === path;
    });
    return matches.length === 1 ? modules[matches[0]] : null;
  }

  function versoBadge(status, mode) {
    status = status || { state: "loading", available: false };
    var stateName = String(status.state || "loading").toLowerCase();
    var rebuilding = stateName === "building" || stateName === "queued";
    var label = "Verso loading";
    var cls = "verso-status-loading";
    if (status.stale && status.available && (stateName === "error" || status.error)) {
      label = "Verso rebuild failed · current source shown";
      cls = "verso-status-error";
    } else if (status.stale && status.available && rebuilding) {
      label = "Verso rebuilding · current source shown";
      cls = "verso-status-stale";
    } else if (status.stale && status.available) {
      label = "Verso update pending · current source shown";
      cls = "verso-status-stale";
    } else if (stateName === "building" || stateName === "queued") {
      label = "Verso building · raw source shown";
      cls = "verso-status-building";
    } else if (stateName === "error" || status.error) {
      label = "Verso error · raw source shown";
      cls = "verso-status-error";
    } else if (status.available) {
      // A healthy semantic view should look like the native source viewer,
      // not an embedded product. Keep the status node for atomic refreshes,
      // but remove it (and the whole metadata row) from the ready-state UI.
      label = "";
      cls = "verso-status-ready";
    } else if (stateName === "unavailable" || stateName === "missing") {
      label = "Verso unavailable · raw source shown";
      cls = "verso-status-error";
    }
    var badge = el("span", {
      class: "verso-status " + cls,
      text: label,
      hidden: label ? null : "hidden",
    });
    badge.setAttribute("role", "status");
    if (status.error) badge.setAttribute("title", String(status.error));
    return badge;
  }

  function versoNeedsStatusChrome(status) {
    status = status || { state: "loading", available: false };
    var stateName = String(status.state || "loading").toLowerCase();
    return !status.available || !!status.stale || !!status.error ||
      stateName === "building" || stateName === "queued" ||
      stateName === "error" || stateName === "failed";
  }

  function updateVersoStatusChrome(shell, status) {
    var meta = shell && shell.querySelector(".verso-source-meta");
    if (!meta) return;
    if (versoNeedsStatusChrome(status)) meta.removeAttribute("hidden");
    else meta.setAttribute("hidden", "hidden");
  }

  function retryVersoBuildFrom(button) {
    if (state.versoRetryBusy) return;
    state.versoRetryBusy = true;
    button.disabled = true;
    button.textContent = "Retrying…";
    retryVersoBuild()
      .then(function (status) {
        state.versoMetadataEpoch += 1;
        state.versoMetadataRequest = null;
        state.versoStatus = status;
        state.versoPollAttempts = 0;
        // A fast retry can publish between dispatch and response. Re-select
        // its manifest before repainting so ready status is never paired with
        // the previous artifact.
        return ensureVersoMetadata(true);
      })
      .then(function (metadata) {
        state.versoRetryBusy = false;
        refreshMountedVersoSurfaces();
        scheduleVersoStatusPoll(metadata.status);
      })
      .catch(function (error) {
        state.versoRetryBusy = false;
        button.disabled = false;
        button.textContent = "Retry Verso build";
        button.setAttribute(
          "title", (error && error.message) || "Could not retry the Verso build"
        );
      });
  }

  function versoRetryButton(status) {
    var stateName = String((status && status.state) || "").toLowerCase();
    if (stateName !== "error" && stateName !== "failed") return null;
    return el("button", {
      type: "button",
      class: "verso-retry",
      text: state.versoRetryBusy ? "Retrying…" : "Retry Verso build",
      disabled: state.versoRetryBusy ? "disabled" : null,
      onclick: function () { retryVersoBuildFrom(this); },
    });
  }

  function updateVersoBadge(shell, status, mode) {
    var old = shell.querySelector(".verso-status");
    var fresh = versoBadge(status, mode);
    if (!old || !old.parentNode) return;
    var meta = old.parentNode;
    meta.replaceChild(fresh, old);
    var oldRetry = meta.querySelector(".verso-retry");
    if (oldRetry) oldRetry.remove();
    var retry = versoRetryButton(status);
    if (retry) meta.insertBefore(retry, fresh.nextSibling);
    // A shell may already have hidden its raw source after a successful
    // semantic render. If a later rebuild becomes stale or fails, restore the
    // current source immediately along with the actionable status chrome.
    updateVersoFreshness(shell, status);
  }

  function copyVersoSelectionAsPlainText(event) {
    // Semantic references are real anchors so they remain mouse/keyboard
    // navigable. Browsers consequently put a text/html flavor on the
    // clipboard, which Markdown-aware destinations turn into `[name](URL)`.
    // Override only selections wholly inside this semantic source and publish
    // the rendered Lean text alone. Selections spanning other UI retain the
    // browser's normal copy behavior.
    var clipboard = event && event.clipboardData;
    var selection = window.getSelection && window.getSelection();
    if (!clipboard || !selection || selection.isCollapsed ||
        !selection.anchorNode || !selection.focusNode) return;
    var anchorElement = selection.anchorNode.nodeType === 1
      ? selection.anchorNode : selection.anchorNode.parentElement;
    var container = anchorElement && anchorElement.closest
      ? anchorElement.closest(".verso-semantic-source") : null;
    if (!container ||
        !container.contains(selection.focusNode)) return;
    var plain = selection.toString();
    if (!plain) return;
    try {
      clipboard.clearData();
      clipboard.setData("text/plain", plain);
      event.preventDefault();
    } catch (e) {
      // Leave the browser default intact if clipboard mutation is unavailable.
    }
  }
  // In non-editable content the Clipboard API targets the focused element
  // (often <body>), not necessarily the element containing the selection.
  // Delegate once from document and identify the semantic source above.
  document.addEventListener("copy", copyVersoSelectionAsPlainText);

  function makeVersoShell(rawSource, mode, identity) {
    // Raw source paints synchronously and remains the automatic fallback for
    // building, failed, unavailable, or stale semantic artifacts. Once a
    // fresh semantic view is mounted it disappears without adding a second
    // source-view toggle to every declaration.
    var raw = el("div", { class: "verso-raw-fallback" }, [rawSource]);
    var semantic = el("div", {
      class: "verso-semantic-source " + (mode === "module" ? "verso-semantic-module" : "verso-semantic-decl"),
      hidden: "hidden",
    });
    var meta = el("div", { class: "verso-source-meta" }, [
      versoBadge(state.versoStatus, mode),
      versoRetryButton(state.versoStatus),
      el("span", { class: "verso-source-help", text:
        "Hover for elaborated types; select a resolved name to open its definition." }),
    ]);
    var shell = el("div", {
      class: "verso-source-shell",
      "data-verso-kind": mode,
      "data-verso-identity": identity || "",
    }, [
      meta,
      raw,
      semantic,
    ]);
    updateVersoStatusChrome(shell, state.versoStatus);
    shell._marathonVersoGeneration = 0;
    shell._marathonVersoIdPrefix = "marathon-verso-" + (++state.versoFragmentSeq) + "-";
    return shell;
  }

  function declarationCodeFromPage(page, entry, pageRelative, artifactBase, idPrefix) {
    var anchorName = entry && entry.anchor;
    if (!anchorName && entry && typeof entry.url === "string" && entry.url.indexOf("#") >= 0) {
      try { anchorName = decodeURIComponent(entry.url.split("#").slice(1).join("#")); }
      catch (e) { anchorName = entry.url.split("#").slice(1).join("#"); }
    }
    if (!anchorName) return null;
    var anchor = page.getElementById(anchorName);
    if (!anchor) return null;
    var code = anchor;
    while (code && !(code.tagName === "CODE" && code.classList.contains("hl"))) {
      code = code.parentElement;
    }
    if (!code) return null;

    var fragment = document.createDocumentFragment();
    // A Verso declaration doc-comment is rendered as the element immediately
    // before its elaborated code block. Keep this association deliberately
    // strict: scanning farther can attach a module doc or the previous field's
    // prose to the declaration currently under review.
    var prose = code.previousElementSibling;
    var isDeclarationProse = !!(
      prose && prose.classList && !prose.classList.contains("mod-doc") &&
      (prose.classList.contains("md-text") || prose.classList.contains("verso-text"))
    );
    if (isDeclarationProse) {
      var cleanProse = sanitizeVersoNode(
        prose, pageRelative, artifactBase, (idPrefix || "") + "doc-"
      );
      if (cleanProse) {
        var proseContainer = cleanProse;
        if (cleanProse.nodeType === 1 && cleanProse.classList) {
          cleanProse.classList.add("verso-declaration-doc");
        } else {
          // A future Verso renderer may use a non-allowlisted wrapper element.
          // Retain its sanitized children inside Marathon-owned prose chrome.
          proseContainer = el("div", { class: "verso-declaration-doc" }, [cleanProse]);
        }
        fragment.appendChild(proseContainer);
      }
    }
    var cleanCode = sanitizeVersoNode(code, pageRelative, artifactBase, idPrefix);
    if (cleanCode) fragment.appendChild(cleanCode);

    // Verso renders a structure/class header and its fields as separate,
    // adjacent code blocks.  The manifest anchor above points only at the
    // header, while field anchors use `Parent___field`.  Mount the contiguous
    // member blocks (and only their immediately preceding doc-comments) so a
    // reviewer sees the declaration's actual public interface.  Stop at the
    // first empty/unrelated code block; in particular, never absorb the next
    // top-level declaration merely because it follows the structure in the
    // same module.
    var memberPrefix = anchorName + "___";
    var pendingMemberProse = null;
    var memberCount = 0;
    var sibling = code.nextElementSibling;
    while (sibling) {
      var siblingIsProse = !!(
        sibling.classList && !sibling.classList.contains("mod-doc") &&
        (sibling.classList.contains("md-text") ||
          sibling.classList.contains("verso-text"))
      );
      if (siblingIsProse) {
        pendingMemberProse = sibling;
        sibling = sibling.nextElementSibling;
        continue;
      }

      var isCodeBlock = !!(
        sibling.tagName === "CODE" && sibling.classList &&
        sibling.classList.contains("hl")
      );
      if (!isCodeBlock) break;
      var ownsMember = String(sibling.id || "").indexOf(memberPrefix) === 0;
      if (!ownsMember) {
        var memberAnchors = sibling.querySelectorAll("[id]");
        for (var memberI = 0; memberI < memberAnchors.length; memberI++) {
          if (String(memberAnchors[memberI].id || "").indexOf(memberPrefix) === 0) {
            ownsMember = true;
            break;
          }
        }
      }
      if (!ownsMember) break;

      if (pendingMemberProse) {
        var cleanMemberProse = sanitizeVersoNode(
          pendingMemberProse, pageRelative, artifactBase,
          (idPrefix || "") + "member-doc-" + memberCount + "-"
        );
        if (cleanMemberProse) {
          if (cleanMemberProse.nodeType === 1 && cleanMemberProse.classList) {
            cleanMemberProse.classList.add("verso-declaration-doc");
            cleanMemberProse.classList.add("verso-member-doc");
            fragment.appendChild(cleanMemberProse);
          } else {
            fragment.appendChild(el("div", {
              class: "verso-declaration-doc verso-member-doc",
            }, [cleanMemberProse]));
          }
        }
      }
      var cleanMember = sanitizeVersoNode(
        sibling, pageRelative, artifactBase,
        (idPrefix || "") + "member-" + memberCount + "-"
      );
      if (cleanMember) {
        if (cleanMember.classList) {
          cleanMember.classList.add("verso-declaration-member");
        }
        fragment.appendChild(cleanMember);
        memberCount += 1;
      }
      pendingMemberProse = null;
      sibling = sibling.nextElementSibling;
    }
    // DocumentFragment is an ordinary extensible DOM object.  Preserve this
    // one bit until paintVersoResult consumes the fragment, then mark the
    // shell so compound declarations are not clipped by the normal card cap.
    fragment._marathonVersoCompound = memberCount > 0;
    return fragment.childNodes.length ? fragment : null;
  }

  function moduleCodeFromPage(page, pageRelative, artifactBase, idPrefix) {
    var fragment = document.createDocumentFragment();
    var content = page.querySelector("section.code-content");
    var sourceNodes = content ? content.childNodes :
      page.querySelectorAll("code.hl.lean.block, pre code.hl.lean");
    for (var i = 0; i < sourceNodes.length; i++) {
      var source = sourceNodes[i];
      if (source.nodeType === 3 && !String(source.nodeValue || "").trim()) continue;
      var clean = sanitizeVersoNode(source, pageRelative, artifactBase, idPrefix);
      if (!clean) continue;
      if (source.nodeType === 1 && source.tagName === "CODE" &&
          source.classList.contains("hl")) {
        fragment.appendChild(el("pre", { class: "verso-module-block" }, [clean]));
      } else {
        fragment.appendChild(clean);
      }
    }
    return fragment.childNodes.length ? fragment : null;
  }

  function appendVersoExtraLinks(popup, target, pageRelative, artifactBase) {
    var owner = target.closest && target.closest("[data-verso-links]");
    if (!owner) return;
    var raw = owner.getAttribute("data-verso-links");
    if (!raw || raw.length > 20000) return;
    try {
      var extras = JSON.parse(raw);
      if (!Array.isArray(extras)) return;
      var list = el("ul", { class: "extra-doc-links" });
      extras.slice(0, 25).forEach(function (item) {
        if (!item || typeof item !== "object") return;
        var href = safeVersoURL(item.href, pageRelative, artifactBase);
        var label = safeVersoScalar(item.short || item.long || "Open definition");
        if (!href || label == null) return;
        var anchor = el("a", {
          href: href,
          target: "_blank",
          rel: "noopener noreferrer",
          text: label,
        });
        var title = safeVersoScalar(item.long || "");
        if (title) anchor.setAttribute("title", title);
        list.appendChild(el("li", {}, [anchor]));
      });
      if (list.childNodes.length) popup.appendChild(list);
    } catch (e) {
      // Malformed metadata is ignored; semantic source and raw fallback remain.
    }
  }

  function makeVersoHover(target, docs, pageRelative, artifactBase, idPrefix) {
    var popup = el("div", {
      class: "hl lean verso-safe-hover verso-hover-popover",
      role: "tooltip",
    });
    var hoverId = target.getAttribute("data-verso-hover");
    if (hoverId && docs && typeof docs[hoverId] === "string") {
      popup.appendChild(sanitizeVersoHTML(
        docs[hoverId], pageRelative, artifactBase,
        (idPrefix || "") + "hover-" + hoverId + "-"
      ));
    } else {
      var embedded = target.querySelector(".hover-info, .tactic-state");
      if (embedded) {
        var clean = sanitizeVersoNode(
          embedded, pageRelative, artifactBase, (idPrefix || "") + "embedded-"
        );
        if (clean) popup.appendChild(clean);
      }
    }
    appendVersoExtraLinks(popup, target, pageRelative, artifactBase);
    return popup.childNodes.length ? popup : null;
  }

  var VERSO_HOVER_SELECTOR =
    ".const.token, .keyword.token, .literal.token, .option.token, " +
    ".var.token, .typed.token, .has-info, .tactic, .level-var, " +
    ".level-const, .level-op, .sort";

  function versoHoverTarget(node) {
    if (!node || !node.closest) return null;
    // Wrapper diagnostics/tactic states take precedence over their nested
    // keyword token so one pointer position can never open two interpretations.
    var target = node.closest(".tactic, .has-info") || node.closest(VERSO_HOVER_SELECTOR);
    // Definition links are the keyboard focus target, while their semantic
    // hover metadata lives on the wrapped token span.
    if (!target && node.matches("a[href]")) {
      target = node.querySelector(VERSO_HOVER_SELECTOR);
    }
    if (!target) return null;
    var container = target.closest(".verso-semantic-source");
    return container && container._marathonVersoHoverContext
      ? { target: target, container: container } : null;
  }

  function versoTargetHasHover(target, docs) {
    var hoverId = target.getAttribute("data-verso-hover");
    return !!(
      (hoverId && docs && typeof docs[hoverId] === "string") ||
      target.querySelector(".hover-info, .tactic-state") ||
      (target.closest && target.closest("[data-verso-links]"))
    );
  }

  function ensureVersoHoverController() {
    if (state.versoHoverController) return state.versoHoverController;
    var popup = null;
    var activeTarget = null;
    var activeContainer = null;
    var closeTimer = null;

    function closeNow() {
      if (closeTimer) clearTimeout(closeTimer);
      closeTimer = null;
      if (popup && popup.parentNode) popup.parentNode.removeChild(popup);
      if (activeTarget) activeTarget.setAttribute("aria-expanded", "false");
      popup = null;
      activeTarget = null;
      activeContainer = null;
    }
    function scheduleClose() {
      if (closeTimer) clearTimeout(closeTimer);
      closeTimer = setTimeout(closeNow, 140);
    }
    function openTarget(found) {
      var target = found.target;
      var container = found.container;
      var context = container._marathonVersoHoverContext;
      if (!context || !versoTargetHasHover(target, context.docs)) return;
      if (activeTarget === target && popup) {
        if (closeTimer) clearTimeout(closeTimer);
        return;
      }
      closeNow();
      popup = makeVersoHover(
        target, context.docs, context.pageRelative, context.artifactBase, context.idPrefix
      );
      if (!popup) return;
      activeTarget = target;
      activeContainer = container;
      popup.addEventListener("mouseenter", function () {
        if (closeTimer) clearTimeout(closeTimer);
      });
      popup.addEventListener("mouseleave", scheduleClose);
      document.body.appendChild(popup);
      var rect = target.getBoundingClientRect();
      var left = Math.max(8, Math.min(rect.left, window.innerWidth - popup.offsetWidth - 8));
      var top = rect.bottom + 6;
      if (top + popup.offsetHeight > window.innerHeight - 8) {
        top = Math.max(8, rect.top - popup.offsetHeight - 6);
      }
      popup.style.left = Math.round(left) + "px";
      popup.style.top = Math.round(top) + "px";
      target.setAttribute("aria-expanded", "true");
    }
    function openFromEvent(event) {
      var found = versoHoverTarget(event.target);
      if (found) openTarget(found);
    }
    function leaveFromEvent(event) {
      var found = versoHoverTarget(event.target);
      if (!found || found.target !== activeTarget) return;
      if (event.relatedTarget && found.target.contains(event.relatedTarget)) return;
      scheduleClose();
    }
    function onKeydown(event) {
      if (event.key === "Escape") closeNow();
      else if ((event.key === "Enter" || event.key === " ") && versoHoverTarget(event.target)) {
        event.preventDefault();
        openFromEvent(event);
      }
    }
    function onClick(event) {
      if (event.target.closest && event.target.closest("a[href]")) return;
      var found = versoHoverTarget(event.target);
      if (found) openTarget(found);
    }
    document.addEventListener("mouseover", openFromEvent);
    document.addEventListener("mouseout", leaveFromEvent);
    document.addEventListener("focusin", openFromEvent);
    document.addEventListener("focusout", leaveFromEvent);
    document.addEventListener("keydown", onKeydown);
    document.addEventListener("click", onClick);
    document.addEventListener("scroll", closeNow, true);
    window.addEventListener("resize", closeNow);
    new MutationObserver(function () {
      if (activeTarget && !activeTarget.isConnected) closeNow();
    }).observe(document.body, { childList: true, subtree: true });

    state.versoHoverController = {
      closeFor: function (container) {
        if (container === activeContainer) closeNow();
      },
    };
    return state.versoHoverController;
  }

  function activateVersoHovers(
    container, docs, pageRelative, artifactBase, idPrefix, artifactToken
  ) {
    if (!container) return;
    if (container.getAttribute("data-verso-bindings") !== "1") {
      container.setAttribute("data-verso-bindings", "1");
      var tokens = container.querySelectorAll(".hl.lean .token[data-binding]");
      for (var i = 0; i < tokens.length; i++) {
        tokens[i].addEventListener("mouseover", function () {
          var binding = this.getAttribute("data-binding");
          var context = this.closest(".hl.lean");
          context = context && context.getAttribute("data-lean-context");
          if (!binding) return;
          var peers = container.querySelectorAll('.hl.lean .token[data-binding="' +
            (window.CSS && CSS.escape ? CSS.escape(binding) : binding.replace(/[^A-Za-z0-9_-]/g, "")) + '"]');
          for (var j = 0; j < peers.length; j++) {
            var peerContext = peers[j].closest(".hl.lean");
            if (!context || !peerContext || peerContext.getAttribute("data-lean-context") === context) {
              peers[j].classList.add("binding-hl");
            }
          }
        });
        tokens[i].addEventListener("mouseout", function () {
          var highlighted = container.querySelectorAll(".binding-hl");
          for (var j = 0; j < highlighted.length; j++) highlighted[j].classList.remove("binding-hl");
        });
      }
    }
    if (container.getAttribute("data-verso-hovers") === "1") return;
    if (!docs) {
      var retries = Number(container.getAttribute("data-verso-hover-retries") || "0");
      if (retries >= 3) return;
      container.setAttribute("data-verso-hover-retries", String(retries + 1));
      setTimeout(function () {
        var currentToken = state.versoManifest && state.versoManifest.artifact_token;
        if (artifactToken && currentToken !== artifactToken) return;
        fetchVersoDocs()
          .then(function (freshDocs) {
            activateVersoHovers(
              container, freshDocs, pageRelative, artifactBase, idPrefix, artifactToken
            );
          });
      }, 2000);
      return;
    }
    container._marathonVersoHoverContext = {
      docs: docs,
      pageRelative: pageRelative,
      artifactBase: artifactBase,
      idPrefix: idPrefix,
    };
    ensureVersoHoverController();
    var seenBindings = Object.create(null);
    var hoverTargets = container.querySelectorAll(VERSO_HOVER_SELECTOR);
    hoverTargets.forEach(function (target) {
      var owningWrapper = target.closest(".tactic, .has-info");
      if (owningWrapper && owningWrapper !== target) return;
      if (!versoTargetHasHover(target, docs) || target.closest("a[href]")) return;
      var binding = target.getAttribute("data-binding");
      var wrapper = target.matches(".tactic, .has-info");
      if (binding && !wrapper) {
        if (seenBindings[binding]) return;
        seenBindings[binding] = true;
      }
      target._marathonVersoOldTabIndex = target.getAttribute("tabindex");
      target.setAttribute("tabindex", "0");
      target.setAttribute("aria-expanded", "false");
      target.setAttribute("data-marathon-verso-focus", "1");
    });
    container.setAttribute("data-verso-hovers", "1");
  }

  // Keep the semantic blueprint navigable without forcing the reviewer out of
  // the declaration they are auditing.  Project declarations open in the
  // current review inspector; modified clicks retain the generated Verso link
  // and therefore still open the full, token-pinned blueprint in a new tab.
  // Mathlib/core links are left alone because the local definition endpoint
  // cannot resolve them honestly.
  function activateVersoDefinitionLinks(container) {
    if (!container || container.getAttribute("data-verso-definition-links") === "1") return;
    container.querySelectorAll("a[href]").forEach(function (anchor) {
      anchor.addEventListener("click", function (event) {
        if (event.defaultPrevented || event.button !== 0 || event.metaKey ||
            event.ctrlKey || event.shiftKey || event.altKey) return;
        var candidates = [];
        var label = String(anchor.textContent || "").trim();
        if (label) candidates.push(label);
        var href = String(anchor.getAttribute("href") || "");
        var hashAt = href.indexOf("#");
        if (hashAt >= 0 && hashAt + 1 < href.length) {
          try {
            var fragment = decodeURIComponent(href.slice(hashAt + 1));
            if (fragment && candidates.indexOf(fragment) < 0) candidates.push(fragment);
          } catch (e) { /* a malformed fragment keeps the normal link behavior */ }
        }
        var definitionName = null;
        for (var i = 0; i < candidates.length; i++) {
          if (projectTermCandidates(candidates[i]).length) {
            definitionName = candidates[i];
            break;
          }
        }
        if (!definitionName) return;
        event.preventDefault();
        event.stopPropagation();
        if (state.versoHoverController) {
          state.versoHoverController.closeFor(container);
        }
        showDefinitionLookup(definitionName, null);
      });
    });
    container.setAttribute("data-verso-definition-links", "1");
  }

  function resetVersoInteractions(container) {
    if (!container) return;
    container.removeAttribute("data-verso-bindings");
    container.removeAttribute("data-verso-hovers");
    container.removeAttribute("data-verso-hover-retries");
    container.removeAttribute("data-verso-definition-links");
    container.querySelectorAll(".binding-hl").forEach(function (node) {
      node.classList.remove("binding-hl");
    });
    if (state.versoHoverController) state.versoHoverController.closeFor(container);
    container.querySelectorAll("[data-marathon-verso-focus]").forEach(function (node) {
      if (node._marathonVersoOldTabIndex == null) node.removeAttribute("tabindex");
      else node.setAttribute("tabindex", node._marathonVersoOldTabIndex);
      node.removeAttribute("aria-expanded");
      node.removeAttribute("data-marathon-verso-focus");
      delete node._marathonVersoOldTabIndex;
    });
    delete container._marathonVersoHoverContext;
  }

  function updateVersoFreshness(shell, status) {
    var semantic = shell.querySelector(".verso-semantic-source");
    var raw = shell.querySelector(".verso-raw-fallback");
    var stale = !!(status && status.stale);
    if (semantic) semantic.setAttribute("data-verso-stale", stale ? "1" : "0");
    if (stale && semantic && semantic.childNodes.length) {
      // Stale generated markup is not merely covered by the current source:
      // remove it, its focus targets, and hover context from the review DOM.
      // A ready poll mounts a newly fetched, token-matched fragment later.
      resetVersoInteractions(semantic);
      while (semantic.firstChild) semantic.removeChild(semantic.firstChild);
      semantic.removeAttribute("data-verso-artifact");
      semantic.setAttribute("hidden", "hidden");
    }
    if (!raw) return;
    var selectedArtifact = state.versoManifest && state.versoManifest.artifact_token;
    var semanticArtifact = semantic && semantic.getAttribute("data-verso-artifact");
    var artifactMatches = !!(
      selectedArtifact && semanticArtifact === selectedArtifact
    );
    var hasSemantic = !!(semantic && semantic.childNodes.length);
    // A ready status can arrive before its page fragment. Never let a fragment
    // from the previous generation replace the current raw source during that
    // fetch (or remain visible if the fetch ultimately fails). In particular,
    // a last-good manifest is useful for direct, token-pinned links but is not
    // review evidence for the current source: mounting it beside the raw block
    // showed the declaration twice and invited reviewers to read the wrong one.
    var semanticIsCurrent = !!(
      hasSemantic && artifactMatches && status && status.available &&
      !status.stale
    );
    if (semanticIsCurrent) {
      semantic.removeAttribute("hidden");
    } else if (hasSemantic) {
      semantic.setAttribute("hidden", "hidden");
    }
    var semanticReady = !!(
      semanticIsCurrent && !semantic.hasAttribute("hidden")
    );
    if (versoNeedsStatusChrome(status) || !semanticReady) raw.removeAttribute("hidden");
    else raw.setAttribute("hidden", "hidden");
    updateVersoStatusChrome(shell, status);
  }

  function paintVersoResult(
    shell, clean, status, docs, pageRelative, artifactBase, artifactToken
  ) {
    var semantic = shell.querySelector(".verso-semantic-source");
    var compoundDeclaration = !!clean._marathonVersoCompound;
    if (compoundDeclaration) shell.classList.add("verso-source-compound");
    else shell.classList.remove("verso-source-compound");
    // Keep the semantic layer inert while replacing it. This makes the swap
    // from raw source to one coherent, token-matched fragment atomic even when
    // a previously mounted artifact was visible.
    semantic.setAttribute("hidden", "hidden");
    resetVersoInteractions(semantic);
    // renderVersoMath marks a mount after processing so repeat calls cannot
    // re-enter KaTeX's generated DOM. Fresh semantic content gets a fresh
    // budget by clearing that marker before the old children are removed.
    semantic.removeAttribute("data-marathon-verso-math-rendered");
    while (semantic.firstChild) semantic.removeChild(semantic.firstChild);
    semantic.appendChild(clean);
    // The sanitized fragment retains Verso's prose structure. Typeset only
    // its explicit or delimiter-based math nodes in place so paragraphs,
    // emphasis, links, and declaration/code boundaries remain intact.
    if (window.MarathonLatex && window.MarathonLatex.renderVersoMath) {
      try { window.MarathonLatex.renderVersoMath(semantic); }
      catch (e) { /* raw TeX remains visible as the fail-safe */ }
    }
    semantic.setAttribute("data-verso-artifact", artifactToken || "");
    updateVersoFreshness(shell, status);
    activateVersoDefinitionLinks(semantic);
    activateVersoHovers(
      semantic, docs, pageRelative, artifactBase,
      shell._marathonVersoIdPrefix, artifactToken
    );
  }

  function enhanceVersoDeclaration(shell, name) {
    var generation = ++shell._marathonVersoGeneration;
    ensureVersoMetadata(false).then(function (metadata) {
      if (generation !== shell._marathonVersoGeneration) return;
      var status = metadata.status || {};
      updateVersoBadge(shell, status, "declaration");
      if (!status.available || status.stale || !metadata.manifest) return;
      var entry = versoConstantEntry(name);
      if (!entry || !entry.url) {
        updateVersoBadge(shell, {
          state: "error", available: false,
          error: "No Verso declaration anchor for " + name,
        }, "declaration");
        return;
      }
      var pageURL = String(entry.url).split("#")[0];
      var artifactToken = metadata.manifest.artifact_token;
      return Promise.all([
        fetchVersoPage(pageURL), fetchVersoDocs(),
      ]).then(function (parts) {
        if (generation !== shell._marathonVersoGeneration) return;
        if (!state.versoManifest ||
            state.versoManifest.artifact_token !== artifactToken) return;
        var artifactBase = versoDocumentBase(parts[0], pageURL);
        var clean = declarationCodeFromPage(
          parts[0], entry, pageURL, artifactBase, shell._marathonVersoIdPrefix
        );
        if (!clean) throw new Error("Verso declaration anchor was not found");
        paintVersoResult(
          shell, clean, status, parts[1], pageURL, artifactBase, artifactToken
        );
      });
    }).catch(function (error) {
      if (generation !== shell._marathonVersoGeneration) return;
      updateVersoBadge(shell, {
        state: "error", available: false,
        error: (error && error.message) || "Could not render Verso source",
      }, "declaration");
    });
  }

  function enhanceVersoModule(shell, path) {
    var generation = ++shell._marathonVersoGeneration;
    ensureVersoMetadata(false).then(function (metadata) {
      if (generation !== shell._marathonVersoGeneration) return;
      var status = metadata.status || {};
      updateVersoBadge(shell, status, "module");
      if (!status.available || status.stale || !metadata.manifest) return;
      var entry = versoModuleEntry(path);
      if (!entry || !entry.url) {
        updateVersoBadge(shell, {
          state: "error", available: false,
          error: "No Verso module page for " + path,
        }, "module");
        return;
      }
      var artifactToken = metadata.manifest.artifact_token;
      return Promise.all([
        fetchVersoPage(entry.url), fetchVersoDocs(),
      ]).then(function (parts) {
        if (generation !== shell._marathonVersoGeneration) return;
        if (!state.versoManifest ||
            state.versoManifest.artifact_token !== artifactToken) return;
        var artifactBase = versoDocumentBase(parts[0], entry.url);
        var clean = moduleCodeFromPage(
          parts[0], entry.url, artifactBase, shell._marathonVersoIdPrefix
        );
        if (!clean) throw new Error("Verso module page has no Lean code blocks");
        paintVersoResult(
          shell, clean, status, parts[1], entry.url, artifactBase, artifactToken
        );
      });
    }).catch(function (error) {
      if (generation !== shell._marathonVersoGeneration) return;
      updateVersoBadge(shell, {
        state: "error", available: false,
        error: (error && error.message) || "Could not render Verso module",
      }, "module");
    });
  }

  function refreshMountedVersoSurfaces() {
    var shells = document.querySelectorAll(".verso-source-shell[data-verso-kind]");
    var artifactToken = state.versoManifest && state.versoManifest.artifact_token;
    for (var i = 0; i < shells.length; i++) {
      var kind = shells[i].getAttribute("data-verso-kind");
      var identity = shells[i].getAttribute("data-verso-identity");
      var semantic = shells[i].querySelector(".verso-semantic-source");
      if (artifactToken && semantic && semantic.childNodes.length &&
          semantic.getAttribute("data-verso-artifact") === artifactToken) {
        updateVersoBadge(shells[i], state.versoStatus || {}, kind);
        updateVersoFreshness(shells[i], state.versoStatus || {});
        continue;
      }
      if (kind === "module") enhanceVersoModule(shells[i], identity);
      else enhanceVersoDeclaration(shells[i], identity);
    }
  }

  function observeVersoSourceToken(token) {
    if (!token || token === state.versoObservedSourceToken) return;
    var hadToken = !!state.versoObservedSourceToken;
    state.versoObservedSourceToken = token;
    if (!hadToken) return;
    state.versoStatus = null;
    state.versoMetadataEpoch += 1;
    state.versoMetadataRequest = null;
    state.versoPollAttempts = 0;
    ensureVersoMetadata(true).then(refreshMountedVersoSurfaces);
  }

  // === subdeck header (parent issue + progress) ============================

  function subdeckHeader() {
    var sd = state.subdeck;
    var prog = (sd && sd.progress) || { reviewed: 0, total: 0 };
    // When skip-API is ON, progress is over MAIN cards only (the backend
    // already scopes it). Overrides pulled back into review are MAIN cards in
    // the served `cards` payload after a refetch, so the counts stay correct.
    var head = el("div", { class: "subdeck-head" }, [
      el("div", { class: "subdeck-issue" }, [
        el("span", { class: "issue-pill", text: "#" + sd.issue }),
        el("span", { class: "chapter-pill", text: "ch " + (sd.chapter == null ? "—" : sd.chapter) }),
        el("span", { class: "subdeck-title", text: sd.title || "" }),
        tierBadge(sd.card_tier),
      ]),
      el("div", { class: "subdeck-progress" }, [
        el("div", { class: "subdeck-controls" }, [
          skipApiToggle(),
          pausedSummaryButton(),
        ]),
        el("span", {
          class: "progress-pill",
          text:
            "reviewed " + (prog.reviewed || 0) + " / " + (prog.total || 0) +
            (state.skipApi ? " (main)" : ""),
        }),
        el("span", {
          class: "subdeck-local-note",
          text: "swipes are LOCAL — nothing ships until you confirm",
        }),
      ]),
    ]);
    return head;
  }

  // The persistent "Skip API" toggle, rendered into the subdeck header. It is
  // a CHECKBOX whose only effect is a PURE-READ refetch + re-render — it POSTs
  // nothing and ships nothing (auto-approval is realized only at the explicit
  // ship). The preference persists per browser session.
  function skipApiToggle() {
    var box = el("input", {
      type: "checkbox",
      id: "skip-api-toggle",
      class: "skip-api-box",
      "aria-label": "Skip API — auto-approve easy API declarations on ship",
    });
    box.checked = !!state.skipApi;
    box.addEventListener("change", function () {
      if (state.rejectComposerOpen) {
        box.checked = !!state.skipApi;
        setSkipApi(state.skipApi); // guarded: explains why navigation is locked
        return;
      }
      // PURE VIEW CHANGE: persist the preference, then GET-refetch the deck.
      // No POST, no verdict, no ship.
      setSkipApi(box.checked);
    });
    var wrap = el("label", { class: "skip-api-toggle", title:
      "Show only the main results; easy API defs/lemmas are AUTO-APPROVED on ship (their statements are kept without individual review)." }, [
      box,
      el("span", { class: "skip-api-label", text: "Skip API" }),
    ]);
    return wrap;
  }

  // Flip the skip-API view preference and refetch the current subdeck. This is
  // the SOLE writer of the preference and the SOLE trigger of the view
  // refetch; it performs only a GET. (Binding safety: never a POST.)
  function setSkipApi(on) {
    if (state.rejectComposerOpen) {
      flashToast("Finish or cancel the rejection note before changing Skip API");
      return;
    }
    state.skipApi = !!on;
    writeSkipApiPref(state.skipApi);
    if (state.issue != null) refetchSubdeck();
  }

  // The collapsible "N API declarations will be auto-approved on ship" panel,
  // shown beneath the deck when skip-API is ON. Lists each auto-approved decl
  // (name + api_reason) and offers a per-decl "pull into review" override that
  // surfaces it as a normal card to swipe. Building this panel — INCLUDING the
  // auto_approved LIST — is a PURE READ; nothing is written until ship.
  function autoApprovedPanel() {
    var sd = state.subdeck;
    if (!sd || !state.skipApi) return null;
    var list = (sd.auto_approved || []).filter(function (c) {
      // A decl the human already pulled back into review is shown as a normal
      // card (it moved into `cards` after the refetch) — drop it from here.
      return !isOverridden(c.name);
    });
    var n = list.length;
    var paused = (((sd.summary || {}).rejected) || []).length > 0;
    var panel = el("section", { class: "api-panel", "aria-label":
      paused ? "API declarations waiting after rejection" : "API declarations to auto-approve" });
    var head = el("button", {
      class: "api-panel-head",
      type: "button",
      "aria-expanded": state.apiPanelOpen ? "true" : "false",
    }, [
      el("span", { class: "api-panel-caret", text: state.apiPanelOpen ? "▾" : "▸" }),
      el("span", { class: "api-panel-title", text:
        n + " API declaration" + (n === 1 ? "" : "s") +
        (paused ? " paused in WAIT after rejection" : " will be auto-approved on ship") }),
    ]);
    head.addEventListener("click", function () {
      if (state.rejectComposerOpen) {
        flashToast("Finish or cancel the rejection note before changing this panel");
        return;
      }
      state.apiPanelOpen = !state.apiPanelOpen;
      // Pure re-render of the same payload (no fetch, no POST).
      renderSubdeckStage();
    });
    panel.appendChild(head);

    if (state.apiPanelOpen) {
      panel.appendChild(
        el("p", { class: "api-panel-note" }, [
          paused ? "Partial reject safety " : "Skip-API ",
          el("strong", { text: paused
            ? "suppresses auto-approval for these declarations"
            : "auto-approves these declarations' STATEMENTS" }),
          paused
            ? " — they remain untouched in WAIT until the upstream fix returns. Pull one back only to review it explicitly."
            : " — they are folded into KEEP on ship without individual review. Pull any one back to review it as a normal card.",
        ])
      );
      var ul = el("ul", { class: "api-list" });
      if (!n) {
        ul.appendChild(el("li", { class: "api-empty", text:
          "none — every API decl has been pulled back into review." }));
      }
      list.forEach(function (c) {
        ul.appendChild(
          el("li", { class: "api-row" }, [
            el("div", { class: "api-row-main" }, [
              el("span", { class: "api-name mono", text: c.name }),
              tierBadge(c.tier, c.qualifiers),
              c.api_reason
                ? el("span", { class: "api-reason", text: c.api_reason })
                : null,
            ]),
            el("button", {
              class: "btn btn-tiny api-pull",
              type: "button",
              onclick: (function (name) {
                return function () { pullIntoReview(name); };
              })(c.name),
            }, ["pull into review"]),
          ])
        );
      });
      panel.appendChild(ul);
    }
    return panel;
  }

  // Override: pull a single API decl out of the auto-approved set and back
  // into the review deck so the human swipes it as a normal card. This is a
  // PURE-READ refetch (the backend, given the override, returns it among the
  // MAIN `cards`); it POSTs nothing. We record the override locally so it
  // survives the refetch and is re-applied.
  function pullIntoReview(name) {
    if (state.rejectComposerOpen) {
      flashToast("Finish or cancel the rejection note before navigating away");
      return;
    }
    overrideSet()[name] = true;
    // Pull the served auto_approved card into the visible deck immediately so
    // the human can act without waiting on a round trip; a refetch would also
    // surface it among `cards` (the backend honours overrides), but doing it
    // locally keeps the view responsive and avoids any extra request.
    var sd = state.subdeck;
    if (sd && sd.auto_approved) {
      for (var i = 0; i < sd.auto_approved.length; i++) {
        if (sd.auto_approved[i].name === name) {
          sd.cards = dependencyOrder((sd.cards || []).concat([sd.auto_approved[i]]));
          if (sd.progress) sd.progress.total = (sd.progress.total || 0) + 1;
          break;
        }
      }
    }
    state.showSummary = false;
    flashToast("pulled " + name + " into review (not auto-approved)");
    revisitDecl(name);
  }

  // === stale-citation footnote (reconciliation — NOT cards) ================
  // The reconciliation drops decls the issue body cites but that are NOT in the
  // current code (in_code=false) out of the swipe deck and into this small,
  // non-swipeable FOOTNOTE. These are never cards — they cannot be verified or
  // rejected; they are advisory ("the issue is stale"). Building/appending the
  // footnote is a PURE READ (it reads subdeck.stale_citations and POSTs
  // nothing). The push-refresh button is the explicit way to fix the drift.
  // HUMAN-TRIGGERED drop of one confirmed-dead stale citation. POSTs to the
  // token-gated /api/drop-citation; the BACKEND re-validates the safety gate
  // (cited + gone-from-code + no replacement + another anchor remains) before
  // any write, so a refusal surfaces here as a toast and writes nothing. On
  // success the issue body lost the dead citation, so we
  // reload the subdeck to re-read it. NOT automatic — dropping can erase a real
  // missing-work signal, which is why it's a deliberate per-citation click.
  function dropCitation(name, btn) {
    var issue = state.issue;
    if (issue == null) return;
    if (btn) { btn.disabled = true; btn.textContent = "dropping…"; }
    postDropCitation(issue, name)
      .then(function (res) {
        if (res && res.dropped) {
          flashToast("Dropped stale citation " + name);
          refetchSubdeck(); // re-read the body now that the citation is gone
        } else {
          // Gate refused (rename available / live decl / uncited) — no write.
          flashToast((res && res.message) || "Not dropped");
          if (btn) { btn.disabled = false; btn.textContent = "Drop citation"; }
        }
      })
      .catch(function (err) {
        flashToast(err && err.message ? err.message : "Drop failed");
        if (btn) { btn.disabled = false; btn.textContent = "Drop citation"; }
      });
  }

  function loadReconciliation(issue, requestSeq) {
    state.reconciliation = { loading: true };
    fetchReconciliation(issue)
      .then(function (result) {
        if (requestSeq !== state.subdeckRequestSeq || state.issue !== issue) return;
        state.reconciliation = result || null;
        if (state.activeTab === "review" && !state.rejectComposerOpen) renderSubdeckStage();
      })
      .catch(function (err) {
        if (requestSeq !== state.subdeckRequestSeq || state.issue !== issue) return;
        state.reconciliation = { error: err.message || "reconciliation preview failed" };
        if (state.activeTab === "review" && !state.rejectComposerOpen) renderSubdeckStage();
      });
  }

  function decideReconciliation(kind, mapping, action, button) {
    var preview = state.reconciliation || {};
    if (state.issue == null || !preview.proposal_token || state.reconciliationBusy) return;
    state.reconciliationBusy = true;
    if (button) {
      button.disabled = true;
      button.textContent = action === "confirm" ? "applying…" : "dismissing…";
    }
    postReconciliationDecision(
      state.issue, preview.proposal_token, action, kind, mapping
    ).then(function (result) {
      flashToast((result && result.message) ||
        (action === "confirm" ? "Reconciliation applied" : "Proposal dismissed"));
      state.reconciliationBusy = false;
      refetchSubdeck();
    }).catch(function (err) {
      state.reconciliationBusy = false;
      flashToast(err && err.message ? err.message : "Reconciliation decision failed");
      // A conflict means the proposal/body changed. Reload both read models so
      // the reviewer never acts on a stale mapping.
      refetchSubdeck();
    });
  }

  function reconciliationProposalPanel() {
    var preview = state.reconciliation;
    if (!preview) return null;
    if (preview.loading) return el("div", { class: "reconcile-proposals muted", text:
      "Checking declaration identity…" });
    if (preview.error) return el("div", { class: "reconcile-proposals inspector-warning", text:
      preview.error });
    var renames = preview.proposed_renames || [];
    var splits = preview.proposed_splits || [];
    if (!renames.length && !splits.length) return null;
    var list = el("div", { class: "reconcile-proposal-list" });

    function proposalRow(kind, mapping, destination) {
      var confirm = el("button", {
        class: "btn btn-tiny reconcile-confirm",
        type: "button",
        disabled: state.reconciliationBusy ? "disabled" : null,
      }, ["Confirm"]);
      var dismiss = el("button", {
        class: "btn btn-tiny reconcile-dismiss",
        type: "button",
        disabled: state.reconciliationBusy ? "disabled" : null,
      }, ["Not the same"]);
      confirm.addEventListener("click", function () {
        decideReconciliation(kind, mapping, "confirm", confirm);
      });
      dismiss.addEventListener("click", function () {
        decideReconciliation(kind, mapping, "dismiss", dismiss);
      });
      return el("div", { class: "reconcile-proposal" }, [
        el("div", { class: "reconcile-map mono", text: mapping.old + " → " + destination }),
        el("div", { class: "reconcile-actions" }, [confirm, dismiss]),
      ]);
    }
    renames.forEach(function (mapping) {
      list.appendChild(proposalRow("rename", mapping, mapping.new));
    });
    splits.forEach(function (mapping) {
      list.appendChild(proposalRow("split", mapping, (mapping.targets || []).join(" + ")));
    });
    return el("section", { class: "reconcile-proposals", role: "alert" }, [
      el("strong", { text: "Aristotle may have changed declaration identity" }),
      el("p", { text:
        "Review each mapping once. Confirm rewrites the citation; “Not the same” keeps the stale citation without stalling future reconciliation." }),
      list,
    ]);
  }

  function staleFootnote() {
    var stale = staleCitations();
    var n = stale.length;
    if (!n) return null;
    var names = stale.map(function (s) {
      return (s && s.name) || String(s);
    });
    // One row per stale citation: the name + a "Drop citation" button (the
    // human-triggered escape for a citation whose decl was removed from the
    // code — the auto-reconciler keeps it conservatively).
    var rows = el("ul", { class: "stale-foot-list" }, names.map(function (name) {
      return el("li", { class: "stale-foot-row" }, [
        el("span", { class: "stale-foot-name mono", text: name }),
        el("button", {
          class: "btn btn-tiny stale-drop",
          type: "button",
          title: "Drop this citation from the issue body — only if the decl was "
               + "removed from the code, has no replacement, and is not the "
               + "issue's final declaration. The backend re-checks before writing.",
          onclick: (function (nm) {
            return function (ev) { dropCitation(nm, ev.currentTarget); };
          })(name),
        }, ["Drop citation"]),
      ]);
    }));
    var foot = el("footer", {
      class: "stale-footnote",
      role: "note",
      "aria-label": "stale citations in the issue body",
    }, [
      el("span", { class: "stale-foot-icon", text: "⚠" }),
      el("div", { class: "stale-foot-text" }, [
        el("span", { class: "stale-foot-lead", text:
          n + " citation" + (n === 1 ? "" : "s") +
          " in the issue body not found in the current code:" }),
        rows,
        reconciliationProposalPanel(),
        el("span", { class: "stale-foot-tail", text:
          "These are NOT review cards. The conductor reconciles renames " +
          "automatically; a citation with no proven replacement is kept (it may " +
          "signal genuinely-missing work). If the decl was deliberately " +
          "REMOVED from the code, drop its citation above after mapping any " +
          "replacement; the final declaration anchor cannot be dropped." }),
      ]),
    ]);
    return foot;
  }

  // Append the stale-citation footnote beneath whatever card is on the stage,
  // when the reconciliation found any. Pure read — appends a footnote, never a
  // card; nothing is written.
  function mountStaleFootnote() {
    var foot = staleFootnote();
    if (foot) stage().appendChild(foot);
  }

  // === one declaration card ================================================

  function verdictTag(card) {
    if (!card.verdict) return null;
    var label = card.verdict; // verified | rejected | deferred
    return el("span", { class: "decl-verdict-tag dv-" + label, text: label });
  }

  // The inline "surfaced from code" badge shown beside a surfaced decl's name.
  function surfacedBadge() {
    return el("span", {
      class: "surfaced-badge",
      title: "Found in the code as another part of this issue's result but NOT " +
        "yet cited by the issue body. Verify it and it is added to the issue " +
        "when you ship.",
    }, ["surfaced — not yet in issue"]);
  }

  // The surfaced banner: a distinct (non-stale) callout explaining that this
  // card was surfaced from the code and is not yet in the issue body — but
  // verifying it adds it to the issue at ship. Shows surfaced_reason when set.
  function surfacedBanner(card) {
    return el("div", { class: "surfaced-banner", role: "note" }, [
      el("span", { class: "surfaced-icon", text: "✚" }),
      el("div", { class: "surfaced-text" }, [
        el("strong", { text: "Surfaced from code — not yet in the issue" }),
        el("div", { class: "surfaced-sub", text:
          (card.surfaced_reason ||
           "exists in the code as part of this issue's result") +
          ". Verify it and it is written into the issue body when you ship." }),
      ]),
    ]);
  }

  // --- non-modal review inspector -----------------------------------------
  // Reject notes, dependency maps, links, and term definitions live beside
  // the source on wide screens (below it on narrow screens).  Unlike the old
  // full-screen modal, this never hides the code the reviewer is commenting on.
  function inspectorHost() {
    return document.getElementById("review-inspector");
  }

  function setComposerNavigationLocked(locked) {
    var selector = [
      ".skip-api-box", ".paused-summary-btn", ".decl-lock-ctl",
      ".api-panel-head", ".api-pull",
    ].join(", ");
    var controls = document.querySelectorAll(selector);
    for (var i = 0; i < controls.length; i++) controls[i].disabled = !!locked;
  }

  function isReviewInspectorOpen() {
    var host = inspectorHost();
    return !!(host && !host.hasAttribute("hidden"));
  }

  function closeReviewInspector() {
    var host = inspectorHost();
    if (!host) return;
    state.definitionRequestSeq += 1;
    state.reviewInspectorGeneration += 1;
    var onClose = host._marathonOnClose;
    host._marathonOnClose = null;
    host.setAttribute("hidden", "hidden");
    host.innerHTML = "";
    var card = document.getElementById("active-card");
    if (card) {
      card.classList.remove("has-inspector");
      card.classList.remove("has-definition-inspector");
    }
    if (typeof onClose === "function") onClose();
  }

  function openReviewInspector(title, children, onClose, kind) {
    var host = inspectorHost();
    if (!host) {
      flashToast("Open a declaration card to use the review inspector");
      return null;
    }
    // Replacing a reject composer would silently lose typed feedback.  Action
    // buttons are disabled while it is open, but term links remain clickable,
    // so guard that path as well.
    if (state.rejectComposerOpen && !onClose) {
      flashToast("Finish or cancel the rejection note first");
      return null;
    }
    state.reviewInspectorGeneration += 1;
    host.innerHTML = "";
    host._marathonOnClose = onClose || null;
    host.appendChild(el("div", { class: "inspector-head" }, [
      el("h2", { class: "inspector-title", text: title }),
      el("button", {
        class: "inspector-close",
        type: "button",
        title: "Close inspector (Esc)",
        "aria-label": "Close review inspector",
        onclick: closeReviewInspector,
      }, ["×"]),
    ]));
    (children || []).forEach(function (child) {
      if (child) host.appendChild(child);
    });
    host.removeAttribute("hidden");
    var card = document.getElementById("active-card");
    if (card) {
      card.classList.add("has-inspector");
      card.classList.toggle("has-definition-inspector", kind === "definition");
    }
    return host;
  }

  function inspectorCode(label, value, highlightLean) {
    var section = el("section", { class: "inspector-sec" }, [
      el("h3", { class: "inspector-label", text: label }),
    ]);
    if (value) {
      section.appendChild(el("pre", {
        class: "inspector-code mono" + (highlightLean ? " inspector-code-lean" : ""),
      }, highlightLean ? leanHighlight(value, []) : [value]));
    } else {
      section.appendChild(el("p", { class: "inspector-empty", text: "not available in this audit snapshot" }));
    }
    return section;
  }

  function showKernelDefinition(member) {
    if (!member) return;
    var meta = el("p", { class: "inspector-meta" }, [
      member.kind ? el("span", { class: "decl-panel-kind", text: member.kind }) : null,
      el("span", { class: "mono", text: member.name }),
    ]);
    openDefinitionSurface("Review details", [
      meta,
      inspectorCode("Elaborated type", member.type_pp, true),
      inspectorCode("Definition body", member.value_pp, true),
      el("p", { class: "inspector-hint", text:
        "This is the audit snapshot's elaborated kernel entry. Click another highlighted term to inspect it." }),
    ]);
  }

  // Append text at the reject composer's caret (the cross-reference
  // affordance: cite another declaration without abandoning the note).
  function insertIntoComposer(text) {
    var host = inspectorHost();
    var ta = host ? host.querySelector("textarea.reject-note") : null;
    if (!ta) return false;
    var start = ta.selectionStart == null ? ta.value.length : ta.selectionStart;
    var end = ta.selectionEnd == null ? start : ta.selectionEnd;
    ta.value = ta.value.slice(0, start) + text + ta.value.slice(end);
    var caret = start + text.length;
    ta.focus();
    try { ta.setSelectionRange(caret, caret); } catch (e) { /* best-effort */ }
    ta.dispatchEvent(new Event("input", { bubbles: true })); // draft persistence
    return true;
  }

  // Read-only review context (dependency map, source links) that may stack
  // OVER the reject composer as a modal — replacing the inspector would
  // destroy the typed note, but reading while writing is exactly what a
  // cross-referencing rejection needs.
  function openReadOnlyReviewSurface(title, children) {
    if (!state.rejectComposerOpen) {
      return openReviewInspector(title, children);
    }
    var panel = el("div", { class: "modal-panel definition-modal" }, [
      el("div", { class: "inspector-head" }, [
        el("h2", { class: "inspector-title", text: title }),
        el("button", { class: "inspector-close", type: "button",
          "aria-label": "Close " + title,
          onclick: function () { closeModal(); } }, ["×"]),
      ]),
    ]);
    (children || []).forEach(function (child) { if (child) panel.appendChild(child); });
    openModal(panel);
    return panel;
  }

  function openDefinitionSurface(title, children) {
    // While the reject composer holds the inspector, read-only surfaces open
    // as a MODAL OVERLAY instead of replacing (and losing) the note — the
    // reviewer can go look at another declaration mid-sentence.
    if (
      state.activeTab === "review" && inspectorHost()
      && !state.rejectComposerOpen
    ) {
      return openReviewInspector(title, children, null, "definition");
    }
    function closeDefinitionModal() {
      closeModal();
    }
    var panel = el("div", { class: "modal-panel definition-modal" }, [
      el("div", { class: "inspector-head" }, [
        el("h2", { class: "inspector-title", text: title }),
        el("button", { class: "inspector-close", type: "button",
          "aria-label": "Close definition", onclick: closeDefinitionModal }, ["×"]),
      ]),
    ]);
    (children || []).forEach(function (child) { if (child) panel.appendChild(child); });
    openModal(panel, function () { state.definitionRequestSeq += 1; });
    return panel;
  }

  function definitionEvidence(match) {
    var evidence = (match && match.evidence) || {};
    var rows = [
      "trust tier: " + (match.tier || "-"),
      "sorry: " + (evidence.sorry == null ? "unknown" : evidence.sorry ? "yes" : "no"),
      "axioms beyond whitelist: " + ((evidence.axioms_beyond_whitelist || []).join(", ") || "none"),
      "deception tags: " + ((evidence.deception_tags || []).join("; ") || "none"),
    ];
    if ((match.qualifiers || []).length) rows.push("qualifiers: " + match.qualifiers.join(", "));
    if (!match.in_audit) rows.push("audit: not yet present in the current snapshot");
    return rows.join("\n");
  }

  function definitionSourceSurface(match) {
    var location = match.source_file || "source unavailable";
    if (match.source_line != null) location += ":" + match.source_line;
    var children = [
      el("h3", { class: "inspector-label", text: "Current source" }),
      el("p", { class: "resource-file" }, [
        el("span", { class: "mono", text: location }),
      ]),
    ];
    var shell = null;
    if (match.source_block) {
      var rawCode = el("pre", { class: "source-block definition-source-block" }, [
        el("code", { class: "lean" }, leanHighlight(match.source_block, [])),
      ]);
      shell = makeVersoShell(rawCode, "declaration", match.name || "");
      children.push(shell);
    } else {
      children.push(el("p", { class: "inspector-empty", text:
        "not available in the current source snapshot" }));
    }
    return {
      section: el("section", { class: "inspector-sec definition-source-surface" }, children),
      shell: shell,
    };
  }

  function renderDefinitionMatch(match) {
    var sourceSurface = definitionSourceSurface(match);
    var owners = el("section", { class: "inspector-sec" }, [
      el("h3", { class: "inspector-label", text: "Owning review card" }),
    ]);
    if (!(match.owners || []).length) {
      owners.appendChild(el("p", { class: "inspector-empty", text:
        "No current issue owns this declaration; it may be newly added or shared vocabulary." }));
    } else {
      (match.owners || []).forEach(function (owner) {
        owners.appendChild(el("button", {
          class: "btn btn-tiny definition-owner",
          type: "button",
          onclick: function () {
            closeReviewInspector();
            closeModal();
            switchTab("review");
            openIssue(owner.issue);
          },
        }, ["Open #" + owner.issue + " · " + (owner.title || "review card")]));
      });
    }
    var surface = openDefinitionSurface("Review details", [
      el("p", { class: "inspector-meta" }, [
        match.kind ? el("span", { class: "decl-panel-kind", text: match.kind }) : null,
        tierBadge(match.tier || "-", match.qualifiers || []),
        el("span", { class: "mono", text: match.name }),
        state.rejectComposerOpen && match.name ? el("button", {
          class: "btn btn-tiny",
          type: "button",
          title: "Insert `" + match.name + "` at the note's caret",
          onclick: function () {
            if (insertIntoComposer("`" + match.name + "`")) {
              closeModal();
            }
          },
        }, ["Insert into note"]) : null,
      ]),
      sourceSurface.section,
      inspectorCode("Elaborated type", match.statement_pp, true),
      inspectorCode("Definition body", match.value_pp, true),
      inspectorCode("Trust evidence", definitionEvidence(match)),
      owners,
    ]);
    if (surface && sourceSurface.shell) {
      enhanceVersoDeclaration(sourceSurface.shell, match.name || "");
    }
  }

  function showDefinitionLookup(name, fallbackMember) {
    var requestSeq = ++state.definitionRequestSeq;
    var issue = state.issue;
    var activeTab = state.activeTab;
    var declaration = activeTab === "review" && state.currentDecl
      ? state.currentDecl.name : null;
    var originSurface = openDefinitionSurface("Definition lookup", [
      el("p", { class: "inspector-hint", text: "Resolving " + name + " against current project source…" }),
    ]);
    if (!originSurface) return;
    var originInspectorGeneration = state.reviewInspectorGeneration;
    function stillCurrent() {
      return requestSeq === state.definitionRequestSeq
        && issue === state.issue
        && activeTab === state.activeTab
        && originSurface.isConnected
        && (activeTab !== "review"
          || originInspectorGeneration === state.reviewInspectorGeneration)
        && (activeTab !== "review"
          || declaration === (state.currentDecl && state.currentDecl.name));
    }
    fetchDefinition(name)
      .then(function (result) {
        if (!stillCurrent()) return;
        var matches = (result && result.matches) || [];
        if (matches.length === 1) return renderDefinitionMatch(matches[0]);
        if (matches.length > 1) {
          var choices = el("div", { class: "definition-choices" });
          matches.forEach(function (match) {
            choices.appendChild(el("button", {
              class: "btn definition-choice",
              type: "button",
              onclick: function () { renderDefinitionMatch(match); },
            }, [
              el("span", { class: "mono", text: match.name }),
              el("span", { class: "definition-choice-source", text:
                (match.source_file || "source unavailable") +
                (match.source_line != null ? ":" + match.source_line : "") }),
            ]));
          });
          openDefinitionSurface("Choose a definition", [
            el("p", { class: "inspector-warning", text:
              (result.message || "This short name is ambiguous.") + " Choose the intended qualified declaration." }),
            choices,
          ]);
          return;
        }
        if (fallbackMember) {
          showKernelDefinition(fallbackMember);
          return;
        }
        openDefinitionSurface("Definition not resolved", [
          el("p", { class: "inspector-warning", text:
            (result && result.message) ||
            "This identifier is not a current project declaration; it may be Mathlib/core vocabulary or a local variable." }),
        ]);
      })
      .catch(function (err) {
        if (!stillCurrent()) return;
        if (fallbackMember) return showKernelDefinition(fallbackMember);
        openDefinitionSurface("Definition lookup failed", [
          el("p", { class: "inspector-warning", text: err.message || "Lookup failed" }),
        ]);
      });
  }

  function verdictGlyph(card) {
    if (card.verdict === "verified") return "✓";
    if (card.verdict === "rejected") return "✗";
    if (card.verdict === "deferred") return "↺";
    return "○";
  }

  function issuePermalink(issue) {
    var current = state.subdeck && state.subdeck.permalink;
    if (!current) return null;
    return current.replace(/\/issues\/\d+(?:[/?#].*)?$/, "/issues/" + issue);
  }

  function openDependencyMap() {
    var sd = state.subdeck;
    if (!sd) return;
    var cards = dependencyOrder(sd.cards || []);
    var files = [];
    cards.forEach(function (card) {
      if (card.source_file && files.indexOf(card.source_file) < 0) files.push(card.source_file);
    });
    var intro = el("p", { class: "inspector-hint", text:
      cards.length + " declarations" +
      (files.length ? " across " + files.length + " source file" + (files.length === 1 ? "" : "s") : "") +
      ". Arrows list local predecessors; click any node to inspect it." });
    var graph = el("ol", { class: "dep-graph", "aria-label": "Declaration dependency order" });
    cards.forEach(function (card, index) {
      var deps = localDependencyNames(card, cards);
      var row = el("li", {
        class: "dep-node" + (state.currentDecl && state.currentDecl.name === card.name ? " is-current" : ""),
      }, [
        el("span", { class: "dep-node-order", text: String(index + 1) }),
        el("button", {
          class: "dep-node-open mono",
          type: "button",
          onclick: (function (name) { return function () { revisitDecl(name); }; })(card.name),
        }, [card.name]),
        el("span", { class: "dep-node-status dv-" + (card.verdict || "open"), text: verdictGlyph(card) }),
        deps.length
          ? el("div", { class: "dep-node-edges", text: "depends on " + deps.join(", ") })
          : el("div", { class: "dep-node-edges dep-root", text: "root / no local predecessor" }),
        card.source_file
          ? el("div", { class: "dep-node-file mono", text: card.source_file })
          : null,
      ]);
      graph.appendChild(row);
    });

    var external = el("section", { class: "inspector-sec" }, [
      el("h3", { class: "inspector-label", text: "Predecessor issues" }),
    ]);
    var predecessors = sd.deps || [];
    if (!predecessors.length) {
      external.appendChild(el("p", { class: "inspector-empty", text: "none reported by the current payload" }));
    } else {
      var list = el("ul", { class: "deps-list" });
      predecessors.forEach(function (dep) {
        var url = issuePermalink(dep.id);
        list.appendChild(el("li", { class: "dep-row" }, [
          tierBadge(dep.tier),
          el("span", { class: "dep-decl mono", text: dep.decl }),
          url
            ? el("a", { class: "dep-id", href: url, target: "_blank", rel: "noopener", text: "#" + dep.id })
            : el("span", { class: "dep-id", text: "#" + dep.id }),
        ]));
      });
      external.appendChild(list);
    }
    openReadOnlyReviewSurface("Dependency map", [intro, graph, external]);
  }

  function openResourceLinks() {
    var sd = state.subdeck || {};
    var card = state.currentDecl || {};
    var links = [];
    if (sd.permalink) {
      links.push(el("a", {
        class: "btn btn-primary resource-link",
        href: sd.permalink,
        target: "_blank",
        rel: "noopener",
      }, ["Open GitHub issue #" + sd.issue]));
    }
    openReadOnlyReviewSurface("Open source & issue", [
      card.source_file
        ? el("p", { class: "resource-file" }, [
            el("strong", { text: "Source file" }),
            el("span", { class: "mono", text: card.source_file }),
          ])
        : el("p", { class: "inspector-empty", text: "No source file was reported for this declaration." }),
      links.length
        ? el("div", { class: "resource-links" }, links)
        : el("p", { class: "inspector-empty", text:
            "No GitHub permalink was reported by this payload. The source path above can still be opened in your editor." }),
    ]);
  }

  // Render the current declaration card. Pure read render (no side effects).
  // The per-decl LOCK control on a review card: shows the current lock state
  // and toggles it. A LOCKED decl's signature is hard-frozen — no refine can
  // change it (locking implies keep). Unlocked = soft (rewritable).
  function declLockControl(card) {
    var locked = !!card.locked;
    return el("button", {
      class: "decl-lock-ctl" + (locked ? " is-locked" : ""),
      type: "button",
      title: locked
        ? "Locked — this signature is hard-frozen; no refine can change it. Click to unlock."
        : "Click to LOCK — hard-freeze this signature so no refine can change it (implies keep).",
      onclick: (function (n, cur) {
        return function (ev) { ev.stopPropagation(); toggleDeclLock(n, !cur); };
      })(card.name, locked),
    }, [locked ? "🔒 locked" : "🔓 lock"]);
  }

  function renderDeclCard(card) {
    var article = el("article", { class: "card decl-card", id: "active-card" });
    article.appendChild(subdeckHeader());

    var panel = el("section", { class: "decl-panel" });
    panel.appendChild(
      el("div", { class: "decl-panel-head" }, [
        el("span", { class: "decl-panel-name mono", text: card.name || "(unnamed)" }),
        card.kind ? el("span", { class: "decl-panel-kind", text: card.kind }) : null,
        tierBadge(card.tier, card.qualifiers),
        // A SURFACED card was found in the code (in one of the issue's
        // namespaces) but is NOT cited by the issue body — auto-surfaced by the
        // reconciliation so the deck matches the CODE. Flag it inline so the
        // human knows it isn't in the issue; it is still a normal reviewable
        // (swipeable) card.
        card.surfaced ? surfacedBadge() : null,
        // Carried over from a prior review round (reject -> refine -> re-review):
        // approved before AND unchanged, so it is pre-verified — no re-review.
        card.carried_over ? el("span", { class: "carried-badge",
          title: "You approved this last round and its source is unchanged, so "
               + "it stays verified — no need to re-review." },
          ["✓ kept from last review"]) : null,
        verdictTag(card),
        declLockControl(card),
      ])
    );

    // The surfaced banner spells out the reconciliation, including the backend's
    // surfaced_reason (which namespace it was found in / why), so the human can
    // see why a card not in the issue body is here.
    if (card.surfaced) panel.appendChild(surfacedBanner(card));

    // A decl the human approved last round whose SOURCE has since changed — the
    // carried-over verdict was dropped, so this is back for re-review. When the
    // stored record kept its reviewed-at basis, show WHAT changed and offer a
    // one-key re-affirm instead of an unexplained full re-review.
    if (card.changed_since_review) {
      var prev = card.previous_review;
      var banner = el("div", { class: "changed-banner", role: "alert" }, [
        el("span", { class: "changed-icon", text: "⟳" }),
        el("div", {}, [
          el("strong", { text: "Changed since your last review" }),
          el("div", { class: "changed-sub", text:
            prev && prev.verdict
              ? "you marked this " + prev.verdict +
                (prev.ts ? " on " + fmtTs(prev.ts) : "") +
                ", and its source or elaborated context changed since — " +
                "check the diff, then re-affirm (a) or review fresh."
              : "you verified this before, but its source changed (Aristotle altered "
                + "it) — please re-review." }),
        ]),
      ]);
      panel.appendChild(banner);
      if (prev && (prev.source_block || prev.statement_pp)) {
        panel.appendChild(previousReviewPanel(card, prev));
      }
    }
    if (card.dependency_invalidated) {
      panel.appendChild(el("div", { class: "changed-banner dependency-invalidated-banner", role: "alert" }, [
        el("span", { class: "changed-icon", text: "↳" }),
        el("div", {}, [
          el("strong", { text: "Approval paused by an upstream rejection" }),
          el("div", { class: "changed-sub", text:
            "this declaration depends on a rejected definition and must be reviewed again after the upstream fix returns." }),
        ]),
      ]));
    }

    var delta = semanticDeltaBanner(card.semantic_delta);
    if (delta) panel.appendChild(delta);

    if (card.source_block) {
      // A source snippet is present — from the audit snapshot OR the backend's
      // repo-wide source fallback (request 1). Render it normally.
      var titleKids = [document.createTextNode("Source")];
      if (card.source_file)
        titleKids.push(el("span", { class: "src-file mono", text: card.source_file }));
      var rawCode = el("pre", { class: "source-block" }, [
        el("code", { class: "lean" }, leanHighlight(card.source_block, card.kernel)),
      ]);
      var semanticSource = makeVersoShell(rawCode, "declaration", card.name || "");
      var detailsButton = el("button", {
        class: "btn btn-tiny source-review-details",
        type: "button",
        "data-no-swipe": "true",
        onclick: function (event) {
          event.stopPropagation();
          showDefinitionLookup(card.name, kernelTermLookup(card.kernel)[card.name] || null);
        },
      }, ["Review details"]);
      panel.appendChild(
        el("section", { class: "sec-source" }, [
          el("div", { class: "source-title-row" }, [
            el("h3", { class: "sec-title" }, titleKids),
            detailsButton,
          ]),
          semanticSource,
        ])
      );
      enhanceVersoDeclaration(semanticSource, card.name || "");
    } else if (card.in_code === false) {
      // request 1: the decl is cited by the issue but was NOT found anywhere
      // in the current code (renamed/deleted) — a STALE CITATION. Replace the
      // generic "(no source)" line with a clear warning banner.
      panel.appendChild(
        el("div", { class: "stale-citation", role: "alert" }, [
          el("span", { class: "stale-icon", text: "⚠" }),
          el("div", { class: "stale-text" }, [
            el("strong", { text: "Cited by the issue but NOT FOUND in the current code" }),
            el("div", { class: "stale-sub", text:
              "likely renamed or deleted; the issue body is stale." }),
          ]),
        ])
      );
    } else {
      // No source snippet, but the decl is (or may be) in the code — the audit
      // snapshot simply did not carry it and the repo fallback found nothing
      // quotable. Keep the existing neutral note (never a blank).
      panel.appendChild(
        el("p", {
          class: "decl-nosource",
          text: "(no source — cited by the issue but absent/unresolved in the audit snapshot)",
        })
      );
    }

    if (card.statement_pp) {
      panel.appendChild(
        el("section", { class: "statement" }, [
          el("details", { class: "elab-type", "data-no-swipe": "true" }, [
            el("summary", { text: "Elaborated type (what is actually proven)", "data-no-swipe": "true" }),
            el("pre", { class: "mono statement-pp" },
              leanHighlight(prettyLeanType(card.statement_pp), card.kernel)),
          ]),
        ])
      );
    }

    var informal = informalBlock(card);
    if (informal) {
      panel.appendChild(el("section", { class: "sec-informal" }, [informal]));
    }
    panel.appendChild(
      el("section", { class: "sec-evidence" }, [
        el("h3", { class: "sec-title", text: "Evidence" }),
        evidenceRow(card.evidence),
      ])
    );

    article.appendChild(el("div", { class: "review-body" }, [
      panel,
      el("aside", {
        id: "review-inspector",
        class: "review-inspector",
        "data-no-swipe": "true",
        "aria-label": "Review inspector",
        hidden: "hidden",
      }),
    ]));
    return article;
  }

  // The leading INFORMAL-APPROVAL card: the human verifies the issue's
  // LLM-rendered informal statement(s) ONCE, up front, against the source.
  // It is NOT a declaration card — it carries no tier/verdict/kernel and never
  // counts toward progress or ships. The SAME swipe/keys/buttons act on it:
  // verify=approve (clears the warning on every card below; flips the issue
  // body's marker at ship), reject=dispute (note required; warning stays),
  // defer=decide later (skip for this session; warning stays).
  function renderApprovalCard(inf) {
    var article = el("article", { class: "card approval-card", id: "active-card" });
    article.appendChild(subdeckHeader());

    var panel = el("section", { class: "decl-panel approval-panel" });
    panel.appendChild(
      el("div", { class: "approval-head" }, [
        el("span", { class: "approval-badge", text: "informal text" }),
        el("h2", { class: "approval-title", text:
          "Verify the informal statement before you review the declarations" }),
      ])
    );
    var math = el("div", { class: "informal-text" });
    math.dataset.latexSource = inf.statement || "";
    panel.appendChild(el("section", { class: "approval-item" }, [math]));
    if (window.MarathonLatex) {
      try { window.MarathonLatex.render(math); }
      catch (e) { math.textContent = inf.statement || ""; }
    } else {
      math.textContent = inf.statement || "";
    }

    // In-card buttons mirror the global action bar / swipe / keys (all routed
    // through actVerify/actReject/actDefer, which detect the approval card).
    panel.appendChild(
      el("div", { class: "approval-actions" }, [
        el("button", { class: "btn btn-primary", type: "button",
          onclick: function () { actVerify(); } }, ["Approve — clears the warning"]),
        el("button", { class: "btn btn-danger", type: "button",
          onclick: function () { actReject(); } }, ["Reject — text is wrong"]),
        el("button", { class: "btn", type: "button",
          onclick: function () { actDefer(); } }, ["Decide later"]),
      ])
    );

    article.appendChild(panel);
    return article;
  }

  // === informal-approval verdict flow (LOCAL ONLY — never ships) ===========
  // Reached only when the approval card is active (state.informalActive). The
  // verdict is keyed by the reserved "__informal__" name — it lives OUTSIDE the
  // per-decl cards, so it never counts toward progress/completeness/summary.
  function canActInformal() {
    return !!state.informalActive && !state.busy && !isModalOpen();
  }
  var INFORMAL_KEY = "__informal__";

  function approveInformal() {
    if (!canActInformal()) return;
    commitInformalVerdict("verify", undefined);
  }
  function dismissInformal() {
    if (!canActInformal()) return;
    // "Decide later": skip the approval card for THIS session (no verdict
    // recorded — the warning stays). Re-opening the issue offers it again.
    state.informalDismissed = true;
    state.informalActive = false;
    flashToast("informal text — decide later");
    renderSubdeckStage();
  }
  function openInformalRejectNote() {
    if (!canActInformal()) return;
    var ta = el("textarea", {
      class: "reject-note",
      rows: "4",
      placeholder: "What is wrong with the informal rendering? (LOCAL note — it is NOT sent to Aristotle; it documents your dispute with the English.)",
      "aria-label": "Informal-rejection note (required)",
    });
    attachDraftPersistence(ta, state.issue, null);
    var err = el("div", { class: "modal-err", hidden: "hidden" });
    var panel = el("div", { class: "modal-panel" }, [
      el("h2", { class: "modal-title", text: "Reject the informal text" }),
      el("p", { class: "modal-sub" }, [
        "The LLM-rendered English does not faithfully describe this result. ",
        "Your note is ", el("strong", { text: "local" }),
        " — it is NOT composed into the Aristotle fix note (that is for the Lean declarations).",
      ]),
      ta, err,
      el("div", { class: "modal-actions" }, [
        el("button", { class: "btn", type: "button", onclick: function () { closeModal(); } }, ["Cancel"]),
        el("button", {
          class: "btn btn-danger",
          type: "button",
          onclick: function () {
            var note = ta.value.trim();
            if (!note) {
              err.textContent = "A note is required (say what is wrong with the rendering).";
              err.removeAttribute("hidden");
              ta.focus();
              return;
            }
            closeModal();
            clearDraft(state.issue, null);
            commitInformalVerdict("reject", note);
          },
        }, ["Reject informal text"]),
      ]),
    ]);
    openModal(panel);
    setTimeout(function () { ta.focus(); }, 0);
  }

  // commitInformalVerdict — records the informal-approval verdict under the
  // reserved __informal__ key (POST /api/decl-verdict, LOCAL only), then folds
  // it into our subdeck copy and advances to the declarations. NEVER ships.
  function commitInformalVerdict(verdict, note) {
    var issue = state.issue;
    var contextFingerprint = state.subdeck && state.subdeck.informal &&
      state.subdeck.informal.context_fingerprint;
    state.busy = true;
    setActionsEnabled(false);
    animateOut(verdict);
    postDeclVerdict(
      issue, INFORMAL_KEY, verdict, note, contextFingerprint
    )
      .then(function (resp) {
        state.busy = false;
        var sd = state.subdeck;
        if (sd) {
          if (resp && resp.review_token)
            sd.review_token = resp.review_token;
          // Trust the server's informal slot when present; else update locally.
          if (resp && resp.informal) sd.informal = resp.informal;
          else if (sd.informal) {
            sd.informal.verdict = verdict === "verify" ? "verified" : "rejected";
            sd.informal.note = typeof note === "string" ? note : null;
          }
        }
        state.informalActive = false;
        flashToast(
          (verdict === "verify" ? "approved" : "rejected") + " the informal text (local)"
        );
        renderSubdeckStage();
      })
      .catch(function (err) {
        state.busy = false;
        // The error card is NOT the approval card — clear the active flag so a
        // stray key/swipe cannot re-fire the POST while it is shown. The retry
        // re-renders the approval card (which re-sets the flag) if still pending.
        state.informalActive = false;
        if (err && err.status === 409) {
          flashToast("The informal text changed — reloading it before you decide again");
          refetchSubdeck();
          return;
        }
        showErrorCard("Could not record the informal verdict", err, function () {
          renderSubdeckStage();
        });
      });
  }

  // === card lifecycle ======================================================

  function showMessageCard(opts) {
    // opts: {title, body, actions: [{label, onClick, kind}]}
    stage().innerHTML = "";
    var card = el("article", { class: "card card-message" }, [
      el("h2", { class: "msg-title", text: opts.title }),
      opts.body ? el("p", { class: "msg-body", text: opts.body }) : null,
    ]);
    if (opts.actions) {
      var bar = el("div", { class: "msg-actions" });
      opts.actions.forEach(function (a) {
        bar.appendChild(
          el("button", { class: "btn " + (a.kind || ""), type: "button", onclick: a.onClick }, [a.label])
        );
      });
      card.appendChild(bar);
    }
    stage().appendChild(card);
    setActionsEnabled(false);
  }

  function showErrorCard(title, err, retry) {
    showMessageCard({
      title: title,
      body: (err && err.message) || "network error",
      actions: [
        { label: "Retry", kind: "btn-primary", onClick: retry },
        { label: "Back to queue", onClick: backToQueue },
      ],
    });
  }

  // === issue picker (the queue is ISSUE-level) =============================

  // The "⚠ N need attention" card shown on the swipe stage when the ready queue
  // is drained but rejected issues are parked. Without this they only appear in
  // the Issues tab, which the swipe reviewer may never open, so a rejection can
  // silently vanish. Each row opens the SAME recovery dialog as the Issues tab;
  // recovery is never a one-click, ambiguous "re-queue".
  function stuckRowText(it) {
    if (it.fix_pr != null) {
      var details = [];
      if (it.fix_pr_build) details.push("build:" + it.fix_pr_build);
      if (it.fix_pr_mergeable === "CONFLICTING" || it.fix_pr_merge_state === "DIRTY") {
        details.push("conflicts with main");
      } else if (it.fix_pr_merge_state === "BEHIND") {
        details.push("behind main");
      }
      if (it.local_recovery_available) details.push("local checkpoint preserved");
      return "fix PR #" + it.fix_pr + " already open" +
        (details.length ? " [" + details.join(" · ") + "]" : "");
    }
    return it.note_preview || "(rejected work is parked)";
  }

  function recoveryActions(it) {
    // `recovery_*` are the public contract names. Accept the short names from
    // the first recovery-enabled backend during a rolling deck restart.
    return (it && (it.recovery_actions || it.actions)) || {};
  }

  function recommendedRecovery(it) {
    var actions = recoveryActions(it);
    var named = it && (it.recommended_recovery || it.recommended_action);
    if (named && actions[named] && actions[named].enabled) return named;
    var order = ["retry_build", "continue_pr", "update_continue", "start_over"];
    for (var i = 0; i < order.length; i++) {
      if (actions[order[i]] && actions[order[i]].enabled) return order[i];
    }
    return null;
  }

  function hasRecoveryAction(it) {
    return !!recommendedRecovery(it);
  }

  function recoveryButton(row, repo, after) {
    var available = hasRecoveryAction(row);
    return el("button", {
      class: "btn btn-tiny issue-recover",
      type: "button",
      disabled: available ? null : "disabled",
      "aria-haspopup": "dialog",
      "aria-label": "Recover issue #" + row.num,
      title: available
        ? "Choose how to recover this parked rejection without losing existing work"
        : "No safe recovery action is currently available",
      onclick: function (ev) {
        openRecoveryDialog(row, repo, after, ev.currentTarget);
      },
    }, ["Recover…"]);
  }

  function showStuckAttentionCard(stuck, repo) {
    stage().innerHTML = "";
    var rows = stuck.map(function (it) {
      var kids = [
        ghLink(repo, "issues", it.num, "#" + it.num, "issue-id"),
        el("span", {
          class: "issue-title",
          title: it.note_preview || "",
          text: stuckRowText(it),
        }),
      ];
      if (it.fix_pr != null) {
        kids.push(ghLink(repo, "pull", it.fix_pr, "PR #" + it.fix_pr, "issue-tier"));
      }
      if (it.attempts) {
        kids.push(el("span", { class: "issue-tier", text: it.attempts + " tries" }));
      }
      kids.push(recoveryButton(it, repo, refreshQueue));
      return el("div", { class: "issue-row stuck-row" }, kids);
    });
    var n = stuck.length;
    var card = el("article", { class: "card card-message" }, [
      el("h2", { class: "msg-title",
                 text: "⚠ " + n + " issue" + (n > 1 ? "s" : "") + " need attention" }),
      el("p", { class: "msg-body",
                text: "These rejected issues are parked. Choose a recovery path; "
                    + "existing PR work is preserved unless you explicitly archive "
                    + "it and start over." }),
      el("div", { class: "stuck-attention-list" }, rows),
      el("div", { class: "msg-actions" }, [
        el("button", { class: "btn", type: "button",
                       onclick: function () { switchTab("issues"); } }, ["See all in Issues"]),
        el("button", { class: "btn btn-primary", type: "button",
                       onclick: refreshQueue }, ["Refresh"]),
      ]),
    ]);
    stage().appendChild(card);
    setActionsEnabled(false);
  }

  function queueBlockerSummary() {
    if (!state.unresolved) return "Every issue in this view has been reviewed.";
    var parts = [];
    if (state.viewBuilding > 0) {
      parts.push(
        state.viewBuilding + " issue" + (state.viewBuilding === 1 ? " is" : "s are") +
        " actively building"
      );
    }
    if (state.awaitingRefinement > 0) {
      parts.push(
        state.awaitingRefinement + " awaiting refinement"
      );
    }
    if (state.needsAttention > 0) {
      parts.push(state.needsAttention + " parked and need attention");
    }
    if (state.needsCitationMapping > 0) {
      parts.push(state.needsCitationMapping + " need declaration mapping");
    }
    if (state.awaitingAudit > 0) {
      parts.push(state.awaitingAudit + " need current machine-audit evidence");
    }
    if (state.dependencyBlocked > 0) {
      parts.push(state.dependencyBlocked + " blocked by dependencies");
    }
    if (state.otherUnresolved > 0) {
      parts.push(state.otherUnresolved + " other unresolved");
    }
    return (parts.length ? parts.join("; ") :
      state.unresolved + " unresolved issue(s) are currently blocked") + ".";
  }

  // Newer servers distinguish a live PID from a conductor that has completed
  // startup and is publishing fresh heartbeats. Preserve rolling-deploy
  // compatibility with older servers that expose only `alive`.
  function conductorIsReady(d) {
    if (!d) return false;
    return d.ready == null ? !!d.alive : !!d.ready;
  }

  function reviewConductorSnapshotKey(d) {
    if (!d) return null;
    // Review renders only these health/capability fields. Excluding heartbeat
    // timestamps, job progress, and landings lets an unchanged manual refresh
    // preserve the mounted empty card instead of visibly repainting it.
    return [
      !!d.alive,
      conductorIsReady(d),
      d.degraded || null,
      !!d.progress_stale,
      !!d.restart_allowed,
    ];
  }

  function semanticQueueSnapshot(data) {
    var semantic = {};
    Object.keys(data || {}).forEach(function (key) {
      if (key !== "queue_snapshot") semantic[key] = data[key];
    });
    var freshness = data && data.queue_snapshot;
    if (freshness) {
      // These two fields affect the visible stale/cold explanation. Volatile
      // transport telemetry does not, so polling cannot remount the card.
      semantic.queue_snapshot = {
        state: freshness.state || null,
        saved_at: freshness.saved_at || null,
      };
    }
    return semantic;
  }

  function reviewQueueSnapshotKey(data) {
    return JSON.stringify([
      semanticQueueSnapshot(data),
      state.reviewStuck,
      state.reviewStuckPending,
      state.reviewStuckUnavailable,
      reviewConductorSnapshotKey(state.reviewConductor),
      state.conductorStartBusy,
      state.conductorStartError,
    ]);
  }

  // Open the subdeck for the first ready issue, or explain why none are ready.
  // Queue renders without waiting for recovery status.  If that auxiliary read
  // is still pending, the empty card says so rather than briefly claiming the
  // project is caught up; a generation-safe completion then enriches the view.
  function openFirstReadyIssue() {
    var ready = readyIssues();
    if (!ready.length) {
      state.issue = null;
      state.subdeck = null;
      state.currentDecl = null;
      // An unchanged manual refresh should not destroy/recreate the same card:
      // doing so loses focus and creates a visible flash even with a fast API.
      if (state.queueRenderedKey &&
          state.queueRenderedKey === state.queueSnapshotKey &&
          surfaceHasStableContent(stage())) {
        return;
      }
      var unresolved = state.unresolved > 0;
      var provisional = provisionalIssues();
      var actions = [
        {
          label: "Refresh",
          kind: provisional.length ? "" : "btn-primary",
          onClick: refreshQueue,
        },
      ];
      if (unresolved) {
        actions.unshift({
          label: "Open status",
          onClick: function () { switchTab("status"); },
        });
      }
      if (state.needsCitationMapping > 0) {
        actions.unshift({
          label: "Open issues",
          onClick: function () { switchTab("issues"); },
        });
      }
      var stuckSnapshot = state.reviewStuck || { issues: [], repo: null };
      var stuck = (state.reviewStuckPending || state.reviewStuckUnavailable)
        ? [] : (stuckSnapshot.issues || []);
      if (stuck.length) {
        actions.unshift({
          label: "Open recovery (" + stuck.length + ")",
          onClick: function () { switchTab("issues"); },
        });
      }
      var conductor = (state.reviewStuckPending || state.reviewStuckUnavailable)
        ? null : state.reviewConductor;
      var conductorReady = conductorIsReady(conductor);
      var conductorProblem = conductor &&
        (!conductorReady || !!conductor.degraded || !!conductor.progress_stale);
      if (conductorProblem) {
        // A stale heartbeat with a still-live PID needs diagnosis; the start
        // endpoint deliberately will not replace a process that owns the lock.
        // Offer Restart only for a genuinely stopped/crashed conductor.
        if (!conductor.alive && conductor.restart_allowed) {
          actions.unshift({
            label: state.conductorStartBusy
              ? "Restarting orchestrator…" : "Restart orchestrator",
            onClick: startConductorFromStatus,
          });
        } else {
          actions.unshift({
            label: "Open orchestrator status",
            onClick: function () { switchTab("status"); },
          });
        }
      }
      if (provisional.length) {
        actions.unshift({
          label: "Review blocked code (" + provisional.length + ")",
          kind: "btn-primary",
          onClick: function () {
            var first = provisional[0];
            state.filesChapter = first.chapter == null
              ? "all" : String(first.chapter);
            state.filesFilter = "provisional";
            state.filesQuery = "";
            state.filesFocus = null;
            state.filesData = null;
            switchTab("files");
          },
        });
      }
      var freshnessNote = state.queueFreshness &&
        state.queueFreshness.state === "stale"
          ? " This is the last saved queue while the server refreshes it; " +
            "the page will update automatically."
          : "";
      var stuckPendingNote = state.reviewStuckPending
        ? " Checking parked issues and orchestrator health in the background."
        : "";
      var stuckUnavailableNote = state.reviewStuckUnavailable
        ? " Recovery status is temporarily unavailable; it will retry " +
          "automatically."
        : "";
      showMessageCard({
        title: unresolved
          ? (provisional.length
            ? "No issues ready to ship — blocked code is reviewable"
            : "No issues ready for review")
          : (state.reviewStuckPending
            ? "Queue loaded — checking issue status"
            : "All caught up"),
        body: queueBlockerSummary() + (conductorProblem
          ? (!conductorReady
            ? (!conductor.alive
              ? " The orchestrator is stopped, so queued repairs and merges " +
                "cannot advance." +
                (state.conductorStartError
                  ? " Restart failed: " + state.conductorStartError
                  : "")
              : " The orchestrator process exists but has no fresh ready " +
                "heartbeat; open Status to diagnose it.")
            : conductor.degraded
              ? " The orchestrator is running but cannot dispatch: " +
                String(conductor.degraded) + "."
              : " The orchestrator process is responsive, but its main loop " +
                "has not completed a tick recently; open Status to diagnose it.")
          : "") + (provisional.length
          ? " " + provisional.length + " dependency-blocked issue" +
            (provisional.length === 1 ? " is" : "s are") +
            " available for provisional, source-ordered review. Decisions are " +
            "bound to the exact code shown and reopen automatically if it changes; " +
            "shipping stays blocked until predecessors resolve."
          : "") + freshnessNote + stuckPendingNote + stuckUnavailableNote,
        actions: actions,
      });
      state.queueRenderedKey = state.queueSnapshotKey;
      mountDigestPanel();
      return;
    }
    state.queueRenderedKey = null;
    renderQueueLanding(ready);
  }

  // === Review landing: the one-glance digest + queue picker ================
  // Replaces the old auto-open of the first ready issue. The reviewer sees
  // WHAT happened (conductor/prover health, parked work, pending
  // confirmations) and CHOOSES an issue — in-progress reviews first, so
  // "continue where I left off" is one keystroke. Rendering is a PURE READ.

  function fetchDigest() {
    return getJSONWithTimeout("/api/digest", 8000);
  }

  function digestInProgress() {
    var digest = state.digest || {};
    var rows = digest.in_progress || [];
    var map = {};
    for (var i = 0; i < rows.length; i++) {
      if (rows[i] && rows[i].issue != null) map[rows[i].issue] = rows[i];
    }
    return map;
  }

  function refreshDigest(force) {
    var now = Date.now();
    if (!force && state.digestFetchedAt && now - state.digestFetchedAt < 10000) {
      return;
    }
    state.digestFetchedAt = now;
    fetchDigest().then(function (digest) {
      state.digest = digest || null;
      // Only repaint when the landing (not a subdeck) owns the stage.
      if (state.activeTab === "review" && state.issue == null) {
        var panel = document.getElementById("digest-panel");
        if (panel) {
          var fresh = digestPanel();
          if (fresh) panel.replaceWith(fresh);
        }
      }
    }).catch(function () { /* the landing renders without a digest */ });
  }

  function digestConductorChip() {
    var digest = state.digest || {};
    var conductor = digest.conductor || {};
    var ok = conductor.alive && conductor.ready;
    return el("span", {
      class: "digest-chip " + (ok ? "digest-ok" : "digest-warn"),
      text: "conductor " + (ok ? "ready" : conductor.alive ? "not ready" : "stopped"),
    });
  }

  function digestProverLine() {
    var digest = state.digest || {};
    var prover = digest.prover || {};
    if (prover.enabled !== false) return null;
    var since = prover.updated_ts ? " since " + fmtTs(prover.updated_ts) : "";
    var by = prover.updated_by ? " (by " + prover.updated_by + ")" : "";
    return el("div", { class: "digest-line digest-warn-line" }, [
      el("span", { text: "Prover off" + since + by + " — no new frontier work is being proven. " }),
      el("button", {
        class: "btn btn-small", type: "button",
        onclick: function (e) { confirmEnableProver(e.target); },
      }, ["Enable prover…"]),
    ]);
  }

  function confirmEnableProver(button) {
    var panel = el("div", { class: "modal-panel" }, [
      el("h2", { class: "modal-title", text: "Enable the prover?" }),
      el("p", { class: "modal-sub", text:
        "The conductor will resume dispatching fill/prove work to Aristotle " +
        "(billed). Refines of rejected issues run regardless of this toggle." }),
      el("div", { class: "modal-actions" }, [
        el("button", { class: "btn", type: "button",
          onclick: function () { closeModal(); } }, ["Cancel"]),
        el("button", {
          class: "btn btn-primary", type: "button", autofocus: "autofocus",
          onclick: function () {
            closeModal();
            if (button) button.disabled = true;
            postJSON("/api/prover-toggle", { enabled: true }).then(function () {
              flashToast("Prover enabled");
              refreshDigest(true);
            }).catch(function (err) {
              if (button) button.disabled = false;
              flashToast("Prover toggle failed: " + ((err && err.message) || err));
            });
          },
        }, ["Enable prover"]),
      ]),
    ]);
    openModal(panel);
  }

  function digestNeedsMe() {
    var digest = state.digest || {};
    var items = digest.needs_me || [];
    if (!items.length) return null;
    var rows = items.map(function (item) {
      if (item.kind === "reconcile") {
        return el("div", { class: "digest-need" }, [
          el("span", { class: "digest-need-title", text:
            "#" + item.issue + " — " + (item.proposals || 1) +
            " declaration identity proposal" + ((item.proposals || 1) === 1 ? "" : "s") +
            " await your confirmation" }),
          el("button", { class: "btn btn-small", type: "button",
            onclick: function () { openIssue(item.issue); } }, ["Open"]),
        ]);
      }
      var evidence = [];
      if (item.last_exit_code != null) {
        evidence.push("exit " + item.last_exit_code);
      }
      if (item.last_finished_ts) evidence.push(fmtTs(item.last_finished_ts));
      var need = el("div", { class: "digest-need" }, [
        el("span", { class: "digest-need-title", text:
          "#" + item.issue + " parked (" + (item.recovery_mode || "needs a recovery choice") +
          (item.recovery_pr ? ", PR #" + item.recovery_pr : "") + ")" +
          (evidence.length ? " — " + evidence.join(", ") : "") }),
        el("button", { class: "btn btn-small", type: "button",
          onclick: function () { switchTab("issues"); } }, ["Open recovery"]),
      ]);
      if (item.error_tail) {
        var details = el("details", { class: "digest-evidence" }, [
          el("summary", { text: "last failure" }),
          el("pre", { class: "digest-evidence-pre", text: item.error_tail }),
        ]);
        need.appendChild(details);
      }
      return need;
    });
    return el("div", { class: "digest-needs" }, [
      el("h3", { class: "digest-h", text: "Needs you" }),
    ].concat(rows));
  }

  function digestHealthLines() {
    var digest = state.digest || {};
    var out = [];
    var audit = digest.audit;
    if (audit && audit.complete === false && (audit.failures || []).length) {
      out.push(el("div", { class: "digest-line", text:
        "Machine audit incomplete: " + String(audit.failures[0]).slice(0, 140) }));
    }
    var verso = digest.verso;
    if (verso && verso.state === "error") {
      out.push(el("div", { class: "digest-line" }, [
        el("span", { text: "Verso semantic build failing — cards show raw source. " }),
        versoRetryButton(verso) || "",
      ]));
    }
    return out;
  }

  function digestPanel() {
    var digest = state.digest;
    var kids = [
      el("div", { class: "digest-head" }, [
        el("h3", { class: "digest-h", text: "While you were away" }),
        digestConductorChip(),
      ]),
    ];
    if (!digest) {
      kids.push(el("div", { class: "digest-line digest-muted",
        text: "loading digest…" }));
      return el("section", { id: "digest-panel", class: "digest-panel" }, kids);
    }
    var prover = digestProverLine();
    if (prover) kids.push(prover);
    var needs = digestNeedsMe();
    if (needs) kids.push(needs);
    kids = kids.concat(digestHealthLines());
    if (kids.length === 1 && !needs) {
      kids.push(el("div", { class: "digest-line digest-muted",
        text: "Nothing is waiting on you beyond the queue below." }));
    }
    return el("section", { id: "digest-panel", class: "digest-panel" }, kids);
  }

  function mountDigestPanel() {
    var existing = document.getElementById("digest-panel");
    var panel = digestPanel();
    if (existing) existing.replaceWith(panel);
    else stage().appendChild(panel);
    refreshDigest(false);
  }

  function landingRowVerdictActions(row) {
    // The fast path for "I already know my verdict on this issue": route the
    // EXISTING issue-level verdict endpoint without re-swiping every
    // declaration. verify merges the PR; reject dispatches Aristotle.
    var verifyBtn = el("button", { class: "btn btn-small", type: "button",
      onclick: function (e) {
        e.stopPropagation();
        confirmIssueVerdict(row.id, "verify", e.target);
      } }, ["Verify…"]);
    var rejectBtn = el("button", { class: "btn btn-small", type: "button",
      onclick: function (e) {
        e.stopPropagation();
        confirmIssueVerdict(row.id, "reject", e.target);
      } }, ["Reject…"]);
    return el("span", { class: "landing-row-actions" }, [verifyBtn, rejectBtn]);
  }

  function confirmIssueVerdict(issue, verdict, trigger) {
    var isReject = verdict === "reject";
    var ta = isReject ? el("textarea", {
      class: "reject-note",
      placeholder: "What must change? Sent to Aristotle verbatim.",
    }) : null;
    var kids = [
      el("h2", { class: "modal-title", text:
        (isReject ? "Reject issue" : "Verify issue") + " #" + issue }),
      el("p", { class: "modal-sub" }, [
        "This is ", el("strong", { text: "irreversible" }),
        isReject
          ? " — it records the rejection and dispatches Aristotle with your note verbatim."
          : " — it merges the marathon PR and flips the issue to VERIFIED, without per-declaration review.",
      ]),
    ];
    if (ta) kids.push(ta);
    kids.push(el("div", { class: "modal-actions" }, [
      el("button", { class: "btn", type: "button",
        onclick: function () { closeModal(); } }, ["Cancel"]),
      el("button", {
        class: "btn " + (isReject ? "btn-danger" : "btn-primary"),
        type: "button",
        autofocus: isReject ? null : "autofocus",
        onclick: function () {
          var note = ta ? ta.value.trim() : null;
          if (isReject && !note) {
            flashToast("A rejection needs a non-empty note");
            return;
          }
          closeModal();
          if (trigger) trigger.disabled = true;
          // Async fast path: durable 202 acceptance, then poll the job to a
          // terminal state — the button never blocks on GitHub writes.
          postJSON("/api/verdict", {
            id: issue, verdict: verdict, note: note, async: true,
          })
            .then(function () {
              flashToast("#" + issue + " " + (isReject ? "reject" : "verify") +
                " accepted — landing in the background");
              pollVerdictJob(issue, trigger, 0);
            })
            .catch(function (err) {
              if (trigger) trigger.disabled = false;
              flashToast("Verdict failed: " + ((err && err.message) || err));
            });
        },
      }, [isReject ? "Confirm — reject #" + issue : "Confirm — verify #" + issue]),
    ]));
    openModal(el("div", { class: "modal-panel" }, kids));
  }

  // Poll one accepted async verdict job to a terminal state (pure reads).
  function pollVerdictJob(issue, trigger, failures) {
    setTimeout(function () {
      getJSONWithTimeout("/api/verdict-status?issue=" + issue, 8000)
        .then(function (resp) {
          var job = (resp && resp.job) || {};
          if (job.status === "succeeded") {
            flashToast("#" + issue + " " + (job.verdict === "reject"
              ? "rejected" : "verified"));
            refreshDigest(true);
            refreshQueue();
            return;
          }
          if (job.status === "failed" || job.status === "awaiting_retry") {
            if (trigger) trigger.disabled = false;
            flashToast("Verdict on #" + issue + " " +
              (job.status === "failed" ? "failed: " + (job.error || "see log")
                : "was interrupted — try again"));
            return;
          }
          pollVerdictJob(issue, trigger, 0);
        })
        .catch(function () {
          if (failures >= 5) {
            if (trigger) trigger.disabled = false;
            flashToast("Lost track of #" + issue + "'s verdict — refresh to check");
            return;
          }
          pollVerdictJob(issue, trigger, failures + 1);
        });
    }, 1200);
  }

  function landingRow(row, index, progress) {
    var reviewed = progress ? progress.decided : 0;
    var meta = [];
    if (row.chapter != null) meta.push("ch. " + row.chapter);
    if (row.tier) meta.push(row.tier);
    if (reviewed) meta.push(reviewed + " decided");
    var node = el("div", {
      class: "landing-row" + (reviewed ? " landing-inprogress" : "") +
        (index === state.landingFocus ? " landing-focus" : ""),
      tabindex: "0",
      role: "button",
      "data-issue": String(row.id),
      onclick: function () { openIssue(row.id); },
      onkeydown: function (e) {
        if (e.key === "Enter") { e.preventDefault(); openIssue(row.id); }
      },
    }, [
      el("span", { class: "landing-row-id", text: "#" + row.id }),
      el("span", { class: "landing-row-title", text: row.title || row.decl || "" }),
      el("span", { class: "landing-row-meta", text: meta.join(" · ") }),
      reviewed
        ? el("span", { class: "landing-row-resume", text: "resume" })
        : landingRowVerdictActions(row),
    ]);
    return node;
  }

  function renderQueueLanding(ready) {
    state.issue = null;
    state.subdeck = null;
    state.currentDecl = null;
    var inProgress = digestInProgress();
    var rows = (ready || []).slice();
    // In-progress reviews lead: "continue where I left off" is one keystroke.
    rows.sort(function (a, b) {
      var pa = inProgress[a.id] ? 1 : 0;
      var pb = inProgress[b.id] ? 1 : 0;
      if (pa !== pb) return pb - pa;
      return 0; // otherwise preserve the server's dependency-aware order
    });
    if (state.landingFocus == null || state.landingFocus >= rows.length) {
      state.landingFocus = 0;
    }
    state.landingRows = rows;
    var host = stage();
    host.innerHTML = "";
    host.appendChild(digestPanel());
    var list = el("div", { class: "landing-list" },
      rows.map(function (row, i) { return landingRow(row, i, inProgress[row.id]); }));
    host.appendChild(el("section", { class: "landing" }, [
      el("h3", { class: "digest-h", text:
        rows.length + " issue" + (rows.length === 1 ? "" : "s") + " ready — pick one to review" }),
      list,
    ]));
    endSurfaceRefresh(host);
    setActionsEnabled(false);
    refreshDigest(false);
  }

  function landingMoveFocus(delta) {
    var rows = state.landingRows || [];
    if (!rows.length) return false;
    var next = Math.min(rows.length - 1, Math.max(0, (state.landingFocus || 0) + delta));
    state.landingFocus = next;
    var nodes = document.querySelectorAll(".landing-row");
    for (var i = 0; i < nodes.length; i++) {
      nodes[i].classList.toggle("landing-focus", i === next);
      if (i === next && typeof nodes[i].focus === "function") nodes[i].focus();
    }
    return true;
  }

  function landingOpenFocused() {
    var rows = state.landingRows || [];
    var row = rows[state.landingFocus || 0];
    if (row) { openIssue(row.id); return true; }
    return false;
  }

  // Pure READ: load the subdeck for `issue` and render its first card. Passes
  // the current skip-API view preference so the backend returns ONLY the
  // main-result cards (plus the auto_approved list) when it is ON. This is a
  // GET — it writes nothing and fires no verdict.
  function openIssue(issue) {
    if (state.rejectComposerOpen) {
      flashToast("Finish or cancel the rejection note before opening another issue");
      return;
    }
    var requestSeq = ++state.subdeckRequestSeq;
    state.definitionRequestSeq += 1;
    if (issue !== state.issue) state.apiPanelOpen = false; // fresh per issue
    state.issue = issue;
    state.queueRenderedKey = null;
    state.showSummary = false;
    // A genuinely fresh issue: re-offer its informal-approval card (a prior
    // session's "decide later" dismissal does not carry across issues).
    state.informalDismissed = false;
    state.informalActive = false;
    state.reconciliation = null;
    setActionsEnabled(false);
    stage().innerHTML = "";
    stage().appendChild(el("div", { class: "card card-loading", text: "loading declarations…" }));
    fetchSubdeck(issue, state.skipApi)
      .then(function (sd) {
        if (requestSeq !== state.subdeckRequestSeq || state.issue !== issue) return;
        state.subdeck = normalizeSubdeck(sd);
        renderSubdeckStage();
        refreshDefinitionTerms(null, true);
        if (staleCitations().length) loadReconciliation(issue, requestSeq);
      })
      .catch(function (err) {
        if (requestSeq !== state.subdeckRequestSeq || state.issue !== issue) return;
        showErrorCard("Could not load issue #" + issue, err, function () {
          openIssue(issue);
        });
      });
  }

  // Re-fetch the CURRENT issue's subdeck with the current skip-API preference
  // — a PURE READ used when the human flips the Skip-API toggle. It changes
  // only WHICH cards are shown; it POSTs nothing and ships nothing.
  function refetchSubdeck() {
    if (state.rejectComposerOpen) {
      flashToast("Finish or cancel the rejection note before refreshing the review");
      return;
    }
    var issue = state.issue;
    if (issue == null) return;
    var requestSeq = ++state.subdeckRequestSeq;
    setActionsEnabled(false);
    stage().innerHTML = "";
    stage().appendChild(el("div", { class: "card card-loading", text: "loading declarations…" }));
    fetchSubdeck(issue, state.skipApi)
      .then(function (sd) {
        if (requestSeq !== state.subdeckRequestSeq || state.issue !== issue) return;
        state.subdeck = normalizeSubdeck(sd);
        state.showSummary = false;
        state.reconciliation = null;
        renderSubdeckStage();
        refreshDefinitionTerms(null, true);
        if (staleCitations().length) loadReconciliation(issue, requestSeq);
      })
      .catch(function (err) {
        if (requestSeq !== state.subdeckRequestSeq || state.issue !== issue) return;
        showErrorCard("Could not load issue #" + issue, err, refetchSubdeck);
      });
  }

  function backToQueue() {
    state.subdeckRequestSeq += 1;
    state.definitionRequestSeq += 1;
    state.issue = null;
    state.subdeck = null;
    state.currentDecl = null;
    state.showSummary = false;
    state.apiPanelOpen = false;
    state.rejectComposerOpen = false;
    state.dependencyOverrides = {};
    state.reconciliation = null;
    // The skip-API preference persists per session; overrides are scoped per
    // issue and harmless to retain, but we drop them so a fresh open starts
    // from the backend's auto-approved classification.
    state.overrides = {};
    refreshQueue();
  }

  // Append the under-deck addenda beneath whatever card is on the stage: the
  // stale-citation FOOTNOTE (the reconciliation's dropped citations — never
  // cards) and the collapsible auto-approved API panel (skip-API only).
  // Building/appending both is a PURE READ — neither POSTs. Mounting the
  // footnote here means every render path that shows a card also shows the
  // reconciliation footnote, so the stale-issue warning is never lost.
  function mountAutoApprovedPanel() {
    mountStaleFootnote();
    var panel = autoApprovedPanel();
    if (panel) stage().appendChild(panel);
  }

  // Decide what to show for the open subdeck: a declaration card, or the
  // confirm-and-ship summary when every decl has been reviewed.
  function renderSubdeckStage() {
    var sd = state.subdeck;
    if (!sd) { openFirstReadyIssue(); return; }
    // The informal-approval card LEADS the subdeck: when the issue carries
    // LLM-rendered informal text the human has not yet acted on (and not
    // dismissed this session), they verify it BEFORE the per-decl cards. It is
    // NOT a declaration — it never counts toward progress/completeness and never
    // ships; acting on it only clears the per-card LLM-rendered warning.
    var inf = sd.informal;
    if (inf && inf.is_llm && !inf.verdict && !state.informalDismissed && !state.showSummary) {
      state.informalActive = true;
      state.currentDecl = null;
      stage().innerHTML = "";
      stage().appendChild(renderApprovalCard(inf));
      mountAutoApprovedPanel();
      setActionsEnabled(true);
      return;
    }
    state.informalActive = false;
    var cards = sd.cards || [];
    var next = firstUnreviewed(cards);
    if (state.showSummary || (sd.complete && !next)) {
      state.showSummary = true;
      state.currentDecl = null;
      renderSummaryCard();
      mountAutoApprovedPanel();
      return;
    }
    if (!next) {
      // No unreviewed cards but not flagged complete (e.g. only deferred
      // remain). Surface the deferred decls so the human can revisit them.
      renderDeferredOnly();
      mountAutoApprovedPanel();
      return;
    }
    state.currentDecl = next;
    stage().innerHTML = "";
    stage().appendChild(renderDeclCard(next));
    mountAutoApprovedPanel();
    setActionsEnabled(true);
  }

  // Only deferred decls remain — the subdeck is not complete. Let the human
  // revisit any of them (re-review) or jump back to the queue.
  function renderDeferredOnly() {
    var sd = state.subdeck;
    var deferred = (sd.cards || []).filter(function (c) { return c.verdict === "deferred"; });
    stage().innerHTML = "";
    // No deferred cards AND we got here (no unreviewed card, not complete) means
    // there are NO reviewable cards at all: every cited decl is missing from the
    // current code (a stale citation — often a decl that was renamed or SPLIT
    // under a different name, e.g. Prop_16_1 → Prop_16_1_preserving/_reversing, or
    // genuinely removed). Show that plainly instead of a bogus "0 deferred".
    if (deferred.length === 0) {
      var stale = (sd.stale_citations || []).map(function (c) { return c.name || c; });
      var msg = "This issue has no declarations to review — none of its cited "
        + "declarations are in the current code"
        + (stale.length ? " (" + stale.join(", ") + ")" : "")
        + ". The result was likely renamed or split under a different name, or "
        + "removed. Reconcile the citation(s) below — drop a genuinely-removed "
        + "one, or update the issue to cite the real decl(s) — before it can ship.";
      var noCard = el("article", { class: "card card-message" }, [
        el("h2", { class: "msg-title", text: "No reviewable declarations" }),
        el("p", { class: "msg-body", text: msg }),
        el("div", { class: "msg-actions" }, [
          el("button", { class: "btn", type: "button", onclick: backToQueue }, ["Back to queue"]),
        ]),
      ]);
      stage().appendChild(noCard);
      setActionsEnabled(false);
      return;
    }
    var card = el("article", { class: "card card-message" }, [
      el("h2", { class: "msg-title", text: "Deferred declarations remain" }),
      el("p", { class: "msg-body", text:
        "Every other declaration is reviewed, but " + deferred.length +
        " deferred decl(s) still need a decision before this issue can ship." }),
    ]);
    var list = el("ul", { class: "summary-list deferred-list" });
    deferred.forEach(function (c) {
      list.appendChild(
        el("li", { class: "summary-row" }, [
          el("button", {
            class: "btn revisit-decl",
            type: "button",
            onclick: function () { revisitDecl(c.name); },
          }, ["revisit " + c.name]),
        ])
      );
    });
    card.appendChild(list);
    card.appendChild(
      el("div", { class: "msg-actions" }, [
        el("button", { class: "btn", type: "button", onclick: backToQueue }, ["Back to queue"]),
      ])
    );
    stage().appendChild(card);
    setActionsEnabled(false);
  }

  // Re-review a specific decl: force it onto the stage regardless of its
  // current verdict (used from the summary "revisit" and the deferred list).
  function revisitDecl(name) {
    var card = findDecl(name);
    if (!card) return;
    state.showSummary = false;
    state.currentDecl = card;
    stage().innerHTML = "";
    stage().appendChild(renderDeclCard(card));
    mountAutoApprovedPanel();
    setActionsEnabled(true);
  }

  // Toggle the opt-in hard-freeze lock on a KEPT decl, from the ship summary.
  // LOCAL-ONLY; updates the local summary from the authoritative response and
  // re-renders the summary card so the lock badge reflects the new state.
  function toggleDeclLock(name, locked) {
    if (state.rejectComposerOpen) {
      flashToast("Finish or cancel the rejection note before changing the lock");
      return;
    }
    if (state.busy) return;
    var currentCard = findDecl(name);
    var contextFingerprint = currentCard && currentCard.context_fingerprint;
    var reloadAfterConflict = false;
    state.busy = true;
    postDeclLock(state.issue, name, locked, contextFingerprint)
      .then(function (resp) {
        if (resp && resp.summary && state.subdeck) state.subdeck.summary = resp.summary;
        if (resp && resp.review_token && state.subdeck)
          state.subdeck.review_token = resp.review_token;
        // Reflect the new lock state on the local card copy so re-renders and
        // a later ship summary agree with the backend.
        var card = findDecl(name);
        if (card) card.locked = locked;
        if (state.currentDecl && state.currentDecl.name === name)
          state.currentDecl.locked = locked;
      })
      .catch(function (err) {
        if (err && err.status === 409) {
          reloadAfterConflict = true;
          flashToast("The declaration changed — reloading the current review");
        } else {
          flashToast((err && err.message) || "Could not change the declaration lock");
        }
      })
      .then(function () {
        state.busy = false;
        if (reloadAfterConflict) {
          refetchSubdeck();
          return;
        }
        if (state.showSummary) {
          renderSummaryCard();
        } else if (state.currentDecl && state.currentDecl.name === name) {
          stage().innerHTML = "";
          stage().appendChild(renderDeclCard(state.currentDecl));
          mountAutoApprovedPanel();
          setActionsEnabled(true);
        }
      });
  }

  // === per-declaration verdict flow (LOCAL ONLY) ===========================

  // The entry points to a LOCAL per-decl verdict. Each is a deliberate user
  // action (button/key/swipe). verify/defer commit immediately; reject opens
  // the required side-by-side note inspector. NONE of these ships anything.
  function guardDependencyDecision() {
    var card = state.currentDecl;
    if (!card) return false;
    var blockers = rejectedLocalDependencies(card);
    var key = dependencyOverrideKey(card);
    if (!blockers.length || state.dependencyOverrides[key]) return false;
    var list = el("ul", { class: "blocked-deps" }, blockers.map(function (name) {
      return el("li", { class: "mono", text: name });
    }));
    openReviewInspector("Upstream declaration rejected", [
      el("p", { class: "inspector-warning", text:
        "This declaration relies on a declaration you rejected. Accepting or rejecting it before Aristotle repairs the predecessor may waste the review." }),
      list,
      el("button", {
        class: "btn dependency-override",
        type: "button",
        onclick: function () {
          state.dependencyOverrides[key] = true;
          closeReviewInspector();
          flashToast("Dependency guard lifted for " + card.name + "; choose a verdict explicitly");
        },
      }, ["Review this declaration anyway"]),
      el("button", {
        class: "btn btn-primary return-paused-summary",
        type: "button",
        onclick: returnToPausedReviewSummary,
      }, ["Return to paused review summary"]),
      el("p", { class: "inspector-hint", text:
        "No verdict was recorded. Close this panel to leave the guard in place." }),
    ]);
    return true;
  }

  // --- diff-and-reaffirm for changed cards --------------------------------
  // A plain line-level LCS diff — no libraries, bounded input (the basis is
  // one declaration's source block).
  function lineDiff(oldText, newText) {
    var a = String(oldText || "").split("\n");
    var b = String(newText || "").split("\n");
    if (a.length * b.length > 250000) {
      // Degenerate size: fall back to whole-block replacement.
      return a.map(function (line) { return { kind: "del", text: line }; })
        .concat(b.map(function (line) { return { kind: "add", text: line }; }));
    }
    var rows = a.length + 1, cols = b.length + 1;
    var lcs = new Array(rows);
    for (var i = 0; i < rows; i++) lcs[i] = new Array(cols).fill(0);
    for (var x = a.length - 1; x >= 0; x--) {
      for (var y = b.length - 1; y >= 0; y--) {
        lcs[x][y] = a[x] === b[y]
          ? lcs[x + 1][y + 1] + 1
          : Math.max(lcs[x + 1][y], lcs[x][y + 1]);
      }
    }
    var out = [], p = 0, q = 0;
    while (p < a.length && q < b.length) {
      if (a[p] === b[q]) { out.push({ kind: "same", text: a[p] }); p++; q++; }
      else if (lcs[p + 1][q] >= lcs[p][q + 1]) { out.push({ kind: "del", text: a[p] }); p++; }
      else { out.push({ kind: "add", text: b[q] }); q++; }
    }
    while (p < a.length) { out.push({ kind: "del", text: a[p++] }); }
    while (q < b.length) { out.push({ kind: "add", text: b[q++] }); }
    return out;
  }

  function previousReviewPanel(card, prev) {
    var diff = lineDiff(prev.source_block || "", card.source_block || "");
    var changedOnly = diff.some(function (d) { return d.kind !== "same"; });
    var diffNode = el("pre", { class: "reaffirm-diff mono" },
      diff.map(function (d) {
        var prefix = d.kind === "add" ? "+ " : d.kind === "del" ? "- " : "  ";
        return el("div", { class: "diff-" + d.kind, text: prefix + d.text });
      }));
    var kids = [
      el("div", { class: "reaffirm-head" }, [
        el("strong", { text: "What changed since your review" }),
        prev.note ? el("span", { class: "reaffirm-note",
          text: "your note: " + prev.note }) : null,
      ]),
      changedOnly ? diffNode : el("div", { class: "changed-sub", text:
        "The source text is identical — an elaborated dependency or the " +
        "statement changed instead." }),
    ];
    if (!changedOnly && prev.statement_pp && card.statement_pp &&
        prev.statement_pp !== card.statement_pp) {
      kids.push(el("div", { class: "reaffirm-stmt" }, [
        el("div", { class: "diff-del mono", text: "- " + prev.statement_pp }),
        el("div", { class: "diff-add mono", text: "+ " + card.statement_pp }),
      ]));
    }
    kids.push(el("div", { class: "reaffirm-actions" }, [
      el("button", { class: "btn btn-small", type: "button",
        onclick: function () { actReaffirm(); } },
        ["Re-affirm " + (prev.verdict || "verdict") + " (a)"]),
    ]));
    return el("div", { class: "reaffirm-panel" }, kids);
  }

  // Re-affirm a changed card's PREVIOUS verdict against the CURRENT
  // fingerprint — routed through the ordinary decl-verdict path so every
  // guard (token, fingerprint, lock) holds exactly as a fresh swipe would.
  function actReaffirm() {
    if (!canAct()) return;
    var card = state.currentDecl;
    var prev = card && card.previous_review;
    if (!card || !card.changed_since_review || !prev || !prev.verdict) return;
    var wire = { verified: "verify", rejected: "reject", deferred: "defer" };
    var verdict = wire[prev.verdict] || prev.verdict;
    if (verdict === "reject" && !(prev.note && String(prev.note).trim())) {
      openRejectNote();
      return;
    }
    commitDeclVerdict(verdict, prev.note || null);
  }

  function actVerify() {
    if (state.informalActive) return approveInformal();
    if (!canAct()) return;
    if (guardDependencyDecision()) return;
    commitDeclVerdict("verify", undefined);
  }
  function actReject() {
    if (state.informalActive) return openInformalRejectNote();
    if (!canAct()) return;
    if (guardDependencyDecision()) return;
    openRejectNote();
  }
  function actDefer() {
    if (state.informalActive) return dismissInformal();
    if (!canAct()) return;
    commitDeclVerdict("defer", undefined);
  }

  function canAct() {
    return !!state.currentDecl && !state.busy && !isModalOpen() &&
      !state.rejectComposerOpen && !state.showSummary;
  }

  // commitDeclVerdict — the sole caller of postDeclVerdict. Records the LOCAL
  // per-decl verdict, then advances to the next unreviewed card (or the
  // summary). This NEVER ships; the irreversible step is the explicit ship.
  function commitDeclVerdict(verdict, note) {
    if (!state.currentDecl) return;
    var issue = state.issue;
    var decl = state.currentDecl.name;
    var contextFingerprint = state.currentDecl.context_fingerprint;
    state.busy = true;
    setActionsEnabled(false);
    animateOut(verdict);
    postDeclVerdict(issue, decl, verdict, note, contextFingerprint)
      .then(function (resp) {
        state.busy = false;
        if (resp && resp.review_token && state.subdeck)
          state.subdeck.review_token = resp.review_token;
        // Reflect the recorded verdict + progress back into our subdeck copy.
        applyDeclResult(decl, verdict, note, resp);
        // Wide read models may already be cached from an earlier tab visit.
        // Invalidate both so returning to Files/Graph cannot show the old
        // declaration verdict or blocker projection.
        state.filesData = null;
        state.graphData = null;
        // Stop at the first bad declaration. A reject may be shipped partially:
        // reviewed predecessors become KEEP, this declaration becomes FIX, and
        // later declarations remain untouched for the post-Aristotle round.
        if (verdict === "reject") state.showSummary = true;
        flashToast(verdictPastTense(verdict) + " " + decl + " (local)");
        renderSubdeckStage();
      })
      .catch(function (err) {
        state.busy = false;
        if (err && err.status === 409) {
          flashToast("The code changed — reloading it before you decide again");
          refetchSubdeck();
          return;
        }
        showErrorCard("Could not record verdict for " + decl, err, function () {
          revisitDecl(decl);
        });
      });
  }

  // Fold a decl-verdict response into the local subdeck copy. We trust the
  // server's progress/complete/summary when present; otherwise we update
  // the touched card's verdict/note so the UI stays consistent offline-ish.
  //
  // SCOPING NOTE: the POST /api/decl-verdict response is built WITHOUT
  // skip_api (the endpoint takes no flag), so its progress/complete are over
  // the WHOLE deck. Under skip-API our `cards` is the MAIN-only set, so we
  // MUST NOT clobber the main-scoped progress/complete with the full-deck
  // figures — doing so would (a) mis-count the "(main)" pill and (b) keep
  // `complete` false forever (the unreviewed API decls live outside `cards`),
  // stranding the human short of the ship summary. So when skip-API is on we
  // recompute progress/complete LOCALLY over the visible main `cards`.
  function applyDeclResult(decl, verdict, note, resp) {
    var sd = state.subdeck;
    if (!sd) return;
    var pastTense = verdictPastTense(verdict);
    (sd.cards || []).forEach(function (c) {
      if (c.name === decl) {
        c.verdict = pastTense;
        c.note = typeof note === "string" ? note : null;
      }
    });
    if (resp && resp.summary) sd.summary = resp.summary;
    var invalidated = {};
    (((sd.summary || {}).invalidated) || []).forEach(function (name) {
      invalidated[name] = true;
    });
    (sd.cards || []).forEach(function (card) {
      if (invalidated[card.name]) {
        if (!card.dependency_invalidated)
          card.verdict_before_dependency_invalidation = card.verdict;
        card.dependency_invalidated = true;
        card.verdict = null;
      } else if (card.dependency_invalidated) {
        card.dependency_invalidated = false;
        // The touched card's POSTed verdict wins. Restore the saved verdict
        // only for a sibling whose upstream rejection was reversed.
        if (card.name !== decl && card.verdict_before_dependency_invalidation)
          card.verdict = card.verdict_before_dependency_invalidation;
        delete card.verdict_before_dependency_invalidation;
      }
    });
    if (state.skipApi) {
      // Main-scoped recompute: a card counts as reviewed iff verified/rejected
      // (deferred does NOT count — it leaves the subdeck incomplete, matching
      // the backend's REVIEWED_DECL_VERDICTS).
      recomputeMainProgress(sd);
    } else {
      if (resp && resp.progress) sd.progress = resp.progress;
      if (resp && typeof resp.complete === "boolean") sd.complete = resp.complete;
    }
  }

  // Recompute progress/complete over the visible MAIN `cards` (the skip-API
  // scope). A card is REVIEWED iff its wire verdict is verified/rejected;
  // deferred leaves the subdeck incomplete. Pure local — no network.
  function recomputeMainProgress(sd) {
    var cards = (sd && sd.cards) || [];
    var total = cards.length;
    var reviewed = 0;
    cards.forEach(function (c) {
      if (c.verdict === "verified" || c.verdict === "rejected") reviewed++;
    });
    sd.progress = { reviewed: reviewed, total: total };
    sd.complete = total > 0 && reviewed === total;
  }

  function verdictPastTense(v) {
    return v === "verify" ? "verified" : v === "reject" ? "rejected" : "deferred";
  }

  // === confirm-and-ship summary card =======================================

  // The API declarations hidden by Skip-API, minus local review overrides.
  function autoApprovedCandidates() {
    var sd = state.subdeck;
    if (!sd || !state.skipApi) return [];
    return (sd.auto_approved || []).filter(function (c) {
      return !isOverridden(c.name);
    });
  }

  // All declarations excluded from a partial reject decision. This includes
  // ordinary unreviewed/deferred cards, dependency-invalidated verified cards
  // (the POST response may leave the local card verdict stale), and every
  // hidden Skip-API candidate whose auto-approval is suppressed after reject.
  function partialWaitCards() {
    var sd = state.subdeck;
    if (!sd) return [];
    var invalidated = {};
    (((sd.summary || {}).invalidated) || []).forEach(function (name) {
      invalidated[name] = true;
    });
    var out = [];
    var seen = {};
    function add(card, reason) {
      if (!card || !card.name || seen[card.name]) return;
      seen[card.name] = true;
      out.push({
        name: card.name,
        verdict: card.verdict,
        source_file: card.source_file,
        wait_reason: reason,
      });
    }
    (sd.cards || []).forEach(function (card) {
      if (invalidated[card.name]) add(card, "dependency invalidated");
      else if (card.verdict !== "verified" && card.verdict !== "rejected")
        add(card, card.verdict === "deferred" ? "deferred" : "not reviewed");
    });
    var rejected = ((sd.summary || {}).rejected) || [];
    if (rejected.length) {
      autoApprovedCandidates().forEach(function (card) {
        add(card, "Skip-API paused after rejection");
      });
    }
    return out;
  }

  // API auto-approval is safe only for a non-partial review. Once anything is
  // rejected, hidden API declarations join WAIT rather than KEEP.
  function autoApprovedForShip() {
    if (hasPausedReviewSummary()) return [];
    return autoApprovedCandidates();
  }

  function renderSummaryCard() {
    var sd = state.subdeck;
    var summary = (sd && sd.summary) || deriveSummary();
    var kept = summary.kept || [];
    var rejected = summary.rejected || [];
    var deferred = summary.deferred || [];
    var anyRejected = rejected.length > 0;
    var pendingCards = partialWaitCards();
    var partialReject = anyRejected && pendingCards.length > 0;
    // A flagged MISSING declaration (the issue is incomplete) also routes the
    // ship to Aristotle (with an ADD instruction) even if nothing was rejected.
    var missing = (sd && sd.missing) || "";
    var anyAristotle = anyRejected || !!missing;
    // Under skip-API these API decls (with no human reject) join KEEP on ship.
    var autoApproved = autoApprovedForShip();
    var provisionalReview = !!(sd && sd.provisional && sd.shippable === false);

    stage().innerHTML = "";
    var card = el("article", { class: "card summary-card", id: "active-card" });
    card.appendChild(subdeckHeader());
    card.appendChild(
      el("h2", { class: "summary-title", text:
        provisionalReview
          ? "Provisional review saved — issue #" + sd.issue + " is still waiting"
          : (partialReject ? "Review paused at first rejection" : "Review complete") +
            " — confirm & ship issue #" + sd.issue })
    );

    // KEEP list (verified decls — kept exactly as written). Under skip-API the
    // auto-approved API decls are appended here, clearly marked, since they
    // will be folded into KEEP at ship without individual review.
    var keepCount = kept.length + autoApproved.length;
    var keepSec = el("section", { class: "summary-sec summary-keep" }, [
      el("h3", { class: "sec-title", text: "KEEP (" + keepCount + ") — verified, unchanged" }),
    ]);
    // Locked decls (opt-in hard freeze). Only meaningful on a reject ship,
    // where a refine will run and could otherwise re-type a kept decl.
    var lockedSet = {};
    ((summary.locked) || []).forEach(function (n) { lockedSet[n] = true; });
    if (keepCount) {
      // One-line explainer: soft by default; lock the few that must never change.
      if (anyAristotle) {
        keepSec.appendChild(el("p", { class: "keep-lock-note" }, [
          "These stay verified. Aristotle may re-type an unlocked one if a fix "
          + "needs it (it comes back for your review). ",
          el("strong", { text: "Lock" }),
          " the few whose statement must never change — the refine then "
          + "cannot touch their signature.",
        ]));
      }
      var keepList = el("ul", { class: "summary-list" });
      kept.forEach(function (name) {
        var isLocked = !!lockedSet[name];
        var row = el("li", { class: "summary-row keep-row" + (isLocked ? " keep-row-locked" : "") }, [
          el("span", { class: "mono", text: name }),
          anyAristotle ? el("button", {
            class: "btn btn-tiny lock-toggle" + (isLocked ? " is-locked" : ""),
            type: "button",
            title: isLocked
              ? "Locked — this signature is hard-frozen; a refine cannot change it. Click to unlock."
              : "Soft-kept — a refine may re-type it. Click to LOCK (hard-freeze the signature).",
            onclick: (function (n, cur) { return function () { toggleDeclLock(n, !cur); }; })(name, isLocked),
          }, [isLocked ? "🔒 locked" : "🔓 lock"]) : null,
          el("button", { class: "btn btn-tiny revisit-decl", type: "button",
            onclick: (function (n) { return function () { revisitDecl(n); }; })(name) }, ["revisit"]),
        ]);
        keepList.appendChild(row);
      });
      autoApproved.forEach(function (c) {
        keepList.appendChild(el("li", { class: "summary-row keep-row keep-row-api" }, [
          el("span", { class: "keep-api-main" }, [
            el("span", { class: "mono", text: c.name }),
            el("span", { class: "api-tag", title: c.api_reason || "easy API declaration",
              text: "auto-approved (API)" }),
            c.api_reason ? el("span", { class: "api-reason", text: c.api_reason }) : null,
          ]),
          el("button", { class: "btn btn-tiny api-pull", type: "button",
            onclick: (function (n) { return function () { pullIntoReview(n); }; })(c.name) },
            ["review instead"]),
        ]));
      });
      keepSec.appendChild(keepList);
      if (autoApproved.length) {
        keepSec.appendChild(
          el("p", { class: "keep-api-note" }, [
            "Skip-API auto-approves the " + autoApproved.length +
            " API declaration" + (autoApproved.length === 1 ? "" : "s") +
            " marked above — their ",
            el("strong", { text: "statements are kept without individual review" }),
            ". Use “review instead” to pull any one back.",
          ])
        );
      }
    } else {
      keepSec.appendChild(el("p", { class: "summary-empty", text: "none verified" }));
    }
    card.appendChild(keepSec);

    // FIX list (rejected decls + their verbatim notes -> Aristotle).
    var fixSec = el("section", { class: "summary-sec summary-fix" }, [
      el("h3", { class: "sec-title", text: "FIX (" + rejected.length + ") — rejected, sent to Aristotle" }),
    ]);
    if (rejected.length) {
      var fixList = el("ul", { class: "summary-list" });
      rejected.forEach(function (r) {
        fixList.appendChild(el("li", { class: "summary-row fix-row" }, [
          el("div", { class: "fix-head" }, [
            el("span", { class: "mono", text: r.name }),
            el("button", { class: "btn btn-tiny revisit-decl", type: "button",
              onclick: (function (n) { return function () { revisitDecl(n); }; })(r.name) }, ["revisit"]),
          ]),
          el("div", { class: "fix-note", text: r.note || "(no note)" }),
        ]));
      });
      fixSec.appendChild(fixList);
    } else {
      fixSec.appendChild(el("p", { class: "summary-empty", text: "none rejected" }));
    }
    card.appendChild(fixSec);

    // WAIT list: after the first rejection we deliberately stop. These cards
    // are neither KEEP nor FIX, and the partial ship note's footer leaves them
    // untouched until Aristotle repairs the earlier declaration.
    if (partialReject) {
      var waitSec = el("section", { class: "summary-sec summary-wait" }, [
        el("h3", { class: "sec-title", text:
          "WAIT (" + pendingCards.length + ") — review after the upstream fix" }),
        el("p", { class: "summary-wait-note", text:
          "These declarations are not included in this decision. Aristotle is told to leave them untouched; they return for review after the rejected declaration is repaired." }),
      ]);
      var waitList = el("ul", { class: "summary-list" });
      pendingCards.forEach(function (pending) {
        waitList.appendChild(el("li", { class: "summary-row wait-row" }, [
          el("span", { class: "mono", text: pending.name }),
          el("span", {
            class: "decl-verdict-tag " +
              (pending.verdict === "deferred" ? "dv-deferred" : "dv-open"),
            text: pending.wait_reason || "not reviewed",
          }),
        ]));
      });
      waitSec.appendChild(waitList);
      card.appendChild(waitSec);
    }

    // ADD section: flag that the issue is INCOMPLETE — a result its title
    // promises has no declaration. The note ships to Aristotle as an ADD
    // instruction (verbatim), routing to Aristotle even if all decls verified.
    var addSec = el("section", { class: "summary-sec summary-add" }, [
      el("h3", { class: "sec-title", text: "ADD — missing declaration(s)" }),
    ]);
    if (missing) {
      addSec.appendChild(el("div", { class: "missing-note", text: missing }));
      addSec.appendChild(el("div", { class: "missing-actions" }, [
        el("button", { class: "btn btn-tiny", type: "button", onclick: openMissingNote }, ["edit"]),
        el("button", { class: "btn btn-tiny", type: "button",
          onclick: function () { saveMissing(""); } }, ["clear"]),
      ]));
    } else {
      addSec.appendChild(el("p", { class: "summary-empty" }, [
        "Everything cited is reviewed — but is a result the title promises ",
        el("strong", { text: "missing" }),
        " a declaration? ",
        el("button", { class: "btn btn-tiny", type: "button", onclick: openMissingNote },
          ["flag a missing declaration"]),
      ]));
    }
    card.appendChild(addSec);

    if (deferred.length && !partialReject) {
      card.appendChild(
        el("p", { class: "summary-deferred", text:
          "note: " + deferred.length + " decl(s) were deferred — review them before shipping." })
      );
    }

    if (provisionalReview) {
      card.appendChild(el("section", {
        class: "reader-provisional-note summary-provisional-note",
        role: "status",
      }, [
        el("strong", { text: "SAVED, NOT SHIPPED — " }),
        sd.ship_blocked_reason ||
          "An upstream issue must resolve before this review can ship.",
        " Marathon will keep these exact-code decisions and reopen any declaration " +
          "whose source or elaborated dependencies change.",
      ]));
    }

    // Ship action. Label depends on whether anything was rejected:
    //   nothing rejected -> "Mark issue verified" (merges the PR, no Aristotle)
    //   any rejected     -> "Ship to Aristotle" (dispatches with the note)
    var shipLabel = anyAristotle ? "Ship to Aristotle" : "Mark issue verified";
    var shipBtn = provisionalReview ? null : el("button", {
        class: "btn ship-btn " + (anyAristotle ? "btn-danger" : "btn-primary"),
        type: "button",
        id: "btn-ship",
        onclick: function () { confirmAndShip(anyAristotle); },
      }, [shipLabel]);
    var summaryActions = [
      el("button", { class: "btn", type: "button", onclick: function () {
        // On a partial reject, continue at the first untouched card only by
        // explicit choice; otherwise revisit the first card as before.
        var first = partialReject ? pendingCards[0] : (sd.cards || [])[0];
        if (first && findDecl(first.name)) revisitDecl(first.name);
        else if (partialReject && state.skipApi) setSkipApi(false);
      } }, [partialReject ? "Continue reviewing anyway" : "Revisit declarations"]),
    ];
    if (shipBtn) summaryActions.push(shipBtn);
    if (provisionalReview) {
      summaryActions.push(el("button", {
        class: "btn btn-primary",
        type: "button",
        onclick: function () { switchTab("status"); },
      }, ["See what is blocking it"]));
    }
    card.appendChild(
      el("div", { class: "summary-actions" }, summaryActions)
    );
    card.appendChild(
      el("p", { class: "ship-warn", text:
        provisionalReview
          ? "No irreversible action is available while this issue is dependency-blocked."
          : "This is the irreversible review action — " +
            (anyAristotle
              ? (partialReject
                  ? "it dispatches the fix now; WAIT declarations stay untouched for the next round."
                  : "it dispatches Aristotle with your fix/add notes verbatim.")
              : "it merges the marathon PR and marks the issue verified.") })
    );

    stage().appendChild(card);
    setActionsEnabled(false);
  }

  // saveMissing — record (or clear with "") the issue's missing-declaration
  // note, fold the authoritative value back into our subdeck copy, and re-render
  // the summary. A PURE LOCAL write (never ships).
  function saveMissing(note) {
    var issue = state.issue;
    postMissing(issue, note)
      .then(function (resp) {
        if (state.subdeck) {
          state.subdeck.missing = (resp && resp.missing) || null;
          if (resp && resp.review_token) {
            state.subdeck.review_token = resp.review_token;
          }
        }
        renderSummaryCard();
      })
      .catch(function (err) {
        showErrorCard("Could not save the missing-declaration note", err,
          function () { renderSummaryCard(); });
      });
  }

  // openMissingNote — the modal to describe what the issue is MISSING. Recording
  // it is LOCAL (nothing dispatches yet); it becomes an ADD instruction to
  // Aristotle only at the explicit ship.
  // fetchMissingSuggestions — PURE GET. Decls the issue's title-promised result
  // may ALREADY be formalized as (the inverse of the surfacing exclusion). Used
  // only to WARN; never blocks the flag-missing flow.
  function fetchMissingSuggestions(issue) {
    return getJSON("/api/missing-suggestions/" + encodeURIComponent(issue));
  }

  // renderMissingSuggestions — fill the async container with "this may already
  // exist" rows. Each row names the existing decl, where it lives, and (when a
  // sibling owns it) which issue cites it; a "Not missing — this is it" button
  // dismisses WITHOUT saving (no Aristotle ADD). Empty -> nothing.
  function renderMissingSuggestions(container, candidates) {
    container.innerHTML = "";
    if (!candidates || !candidates.length) return;
    container.appendChild(el("p", { class: "missing-suggest-head" }, [
      el("strong", { text: "⚠ This may already be formalized" }),
      " — review before flagging it missing:",
    ]));
    var list = el("ul", { class: "missing-suggest-list" });
    candidates.forEach(function (c) {
      var where = c.cited_by && c.cited_by.length
        ? "cited by #" + c.cited_by.join(", #")
        : (c.in_code ? "in code, uncited" : "in the snapshot");
      list.appendChild(el("li", { class: "missing-suggest-row" }, [
        el("div", { class: "ms-head" }, [
          el("span", { class: "mono", text: c.decl }),
          el("span", { class: "ms-where", text: where }),
          c.matched_key ? el("span", { class: "ms-key", text: c.matched_key }) : null,
        ]),
        c.snippet ? el("div", { class: "ms-snippet", text: c.snippet }) : null,
        c.source_file ? el("div", { class: "ms-file mono", text: c.source_file }) : null,
      ]));
    });
    container.appendChild(list);
    container.appendChild(el("div", { class: "missing-suggest-actions" }, [
      el("button", {
        class: "btn btn-tiny", type: "button",
        onclick: function () { closeModal(); },
      }, ["Not missing — one of these is it"]),
    ]));
  }

  function openMissingNote() {
    var cur = (state.subdeck && state.subdeck.missing) || "";
    var issue = state.issue;
    // Async-fill container: inserted synchronously, populated after the GET.
    var suggestBox = el("div", { class: "missing-suggest" });
    var ta = el("textarea", {
      class: "reject-note", rows: "5",
      placeholder: "Which result does the issue's title promise that has NO "
        + "declaration? Describe it so Aristotle can add it — e.g. \"the "
        + "function-multiplication case: f • A is smooth when f and A are "
        + "smooth — a MixedTensorField.IsSmooth.smul_* lemma\".",
      "aria-label": "Missing-declaration note",
    });
    ta.value = cur;
    var panel = el("div", { class: "modal-panel" }, [
      el("h2", { class: "modal-title", text: "Flag a missing declaration" }),
      el("p", { class: "modal-sub" }, [
        "The existing declarations are fine, but the issue is ",
        el("strong", { text: "incomplete" }),
        " — a result its title promises is not formalized. Your note is ",
        el("strong", { text: "composed verbatim into an ADD instruction for Aristotle" }),
        " at ship (the issue routes to Aristotle even if every declaration is "
        + "verified). Recording it now is LOCAL — nothing dispatches yet.",
      ]),
      suggestBox,
      ta,
      el("div", { class: "modal-actions" }, [
        el("button", { class: "btn", type: "button", onclick: function () { closeModal(); } }, ["Cancel"]),
        el("button", {
          class: "btn btn-primary", type: "button",
          onclick: function () { var n = ta.value.trim(); closeModal(); saveMissing(n); },
        }, ["Save"]),
      ]),
    ]);
    openModal(panel);
    setTimeout(function () { ta.focus(); }, 0);
    // Advisory-only: a fetch failure must NEVER block Save. Swallow errors.
    fetchMissingSuggestions(issue)
      .then(function (resp) { renderMissingSuggestions(suggestBox, resp && resp.candidates); })
      .catch(function () { /* advisory — keep the modal fully usable */ });
  }

  // Fallback summary derivation from the local subdeck if the server did not
  // send one (keeps the summary card non-blank).
  function deriveSummary() {
    var cards = (state.subdeck && state.subdeck.cards) || [];
    var kept = [], rejected = [], deferred = [];
    cards.forEach(function (c) {
      if (c.verdict === "verified") kept.push(c.name);
      else if (c.verdict === "rejected") rejected.push({ name: c.name, note: c.note || "" });
      else if (c.verdict === "deferred") deferred.push(c.name);
    });
    return { kept: kept, rejected: rejected, deferred: deferred };
  }

  // The surfaced declarations the human KEPT (verified, not rejected) — these
  // are written into the issue body at ship (the auto-add behavior).
  function keptSurfacedForShip() {
    var sd = state.subdeck;
    if (!sd || !sd.summary) return [];
    var kept = sd.summary.kept || [];
    return (sd.cards || []).filter(function (c) {
      return c.surfaced && kept.indexOf(c.name) !== -1;
    });
  }

  // confirmAndShip — opens the explicit confirm modal. Shipping only fires
  // after the user confirms here (the irreversible step).
  function confirmAndShip(anyRejected) {
    var autoApproved = autoApprovedForShip();
    var keptSurfaced = keptSurfacedForShip();
    var rejected = (state.subdeck && state.subdeck.summary && state.subdeck.summary.rejected) || [];
    var pending = partialWaitCards();
    var partialReject = rejected.length > 0 && pending.length > 0;
    var kids = [
      el("h2", { class: "modal-title", text:
        (anyRejected ? "Ship to Aristotle" : "Mark issue verified") + " — #" + state.issue }),
      el("p", { class: "modal-sub" }, [
        "This is ",
        el("strong", { text: "irreversible" }),
        anyRejected
          ? " — it dispatches Aristotle with your fix notes verbatim and merges the verified declarations."
          : " — it merges the marathon PR and flips the issue to VERIFIED.",
      ]),
    ];
    if (partialReject) {
      kids.push(
        el("p", { class: "modal-sub modal-partial-warn" }, [
          el("strong", { text: "Stop-at-first-reject: " }),
          pending.length + " later declaration" + (pending.length === 1 ? " is" : "s are") +
          " excluded from KEEP/FIX and will be left untouched for the next review round.",
        ])
      );
    }
    // Skip-API: make the auto-approval explicit at the point of no return.
    if (autoApproved.length) {
      kids.push(
        el("p", { class: "modal-sub modal-api-warn" }, [
          "Skip-API will ",
          el("strong", { text: "auto-approve " + autoApproved.length + " API declaration" +
            (autoApproved.length === 1 ? "" : "s") + " without individual review" }),
          ": " + autoApproved.map(function (c) { return c.name; }).join(", ") + ".",
        ])
      );
    }
    // Auto-add: surfaced declarations the human kept get written into the issue.
    if (keptSurfaced.length) {
      kids.push(
        el("p", { class: "modal-sub" }, [
          "It also ",
          el("strong", { text: "writes " + keptSurfaced.length + " surfaced declaration" +
            (keptSurfaced.length === 1 ? "" : "s") + " into the issue body" }),
          " (in the code, not previously cited): " +
            keptSurfaced.map(function (c) { return c.name; }).join(", ") + ".",
        ])
      );
    }
    kids.push(
      el("div", { class: "modal-actions" }, [
        el("button", { class: "btn", type: "button", onclick: function () { closeModal(); } }, ["Cancel"]),
        el("button", {
          class: "btn " + (anyRejected ? "btn-danger" : "btn-primary"),
          type: "button",
          // The modal is already the explicit second step of a deliberate
          // ship: focus Confirm so Enter confirms (Esc still cancels).
          autofocus: "autofocus",
          onclick: function () { closeModal(); shipIssue(); },
        }, ["Confirm — ship #" + state.issue]),
      ])
    );
    openModal(el("div", { class: "modal-panel" }, kids));
  }

  var SHIP_POLL_DELAY_MS = 900;
  var SHIP_POLL_MAX_FAILURES = 6;
  var SHIP_STATUS_TIMEOUT_MS = 6000;
  var SHIP_CONTEXT_KEY = "marathon-deck-active-ship";

  function shipPollDelay(ms) {
    return new Promise(function (resolve) { setTimeout(resolve, ms); });
  }

  function normalizedShipStatus(job) {
    return String((job && job.status) || "").toLowerCase().replace(/-/g, "_");
  }

  function shipStatusUrl(url) {
    if (!url) return null;
    try {
      var parsed = new URL(url, window.location.href);
      // Ship-status polling is deliberately same-origin and token-free. Never
      // follow a status URL away from the deck origin.
      if (parsed.origin !== window.location.origin ||
          parsed.pathname !== "/api/ship-status") return null;
      return parsed.pathname + parsed.search;
    } catch (e) {
      return null;
    }
  }

  function issueShipStatusUrl(issue, requestId) {
    var url = "/api/ship-status?issue=" + encodeURIComponent(issue);
    if (requestId) {
      url += "&request_id=" + encodeURIComponent(requestId);
    }
    return url;
  }

  function shipContextsMatch(a, b) {
    if (!a || !b || a.issue !== b.issue) return false;
    if (a.requestId && b.requestId) return a.requestId === b.requestId;
    if (a.operationId && b.operationId) return a.operationId === b.operationId;
    return a.statusUrl === b.statusUrl;
  }

  function normalizeShipContext(raw) {
    if (!raw || typeof raw !== "object") return null;
    var issue = Number(raw.issue);
    var statusUrl = shipStatusUrl(raw.statusUrl || raw.status_url);
    if (!Number.isInteger(issue) || issue <= 0 || !statusUrl) return null;
    var operationId = raw.operationId || raw.operation_id || null;
    if (operationId != null && typeof operationId !== "string") return null;
    var requestId = raw.requestId || raw.request_id || null;
    if (requestId != null &&
        (typeof requestId !== "string" || !/^[0-9a-f]{32}$/.test(requestId))) {
      return null;
    }
    return {
      issue: issue,
      operationId: operationId,
      requestId: requestId,
      statusUrl: statusUrl,
    };
  }

  function persistShipContext(ctx) {
    ctx = normalizeShipContext(ctx);
    if (!ctx) return null;
    state.activeShip = ctx;
    try {
      window.sessionStorage.setItem(SHIP_CONTEXT_KEY, JSON.stringify(ctx));
    } catch (e) { /* storage unavailable — the in-memory poll remains safe */ }
    return ctx;
  }

  function readShipContext() {
    try {
      var raw = window.sessionStorage.getItem(SHIP_CONTEXT_KEY);
      return raw ? normalizeShipContext(JSON.parse(raw)) : null;
    } catch (e) {
      return null;
    }
  }

  function clearShipContext(ctx) {
    if (ctx && state.activeShip && !shipContextsMatch(ctx, state.activeShip)) {
      return false;
    }
    state.activeShip = null;
    state.shipPollSeq += 1;
    try {
      var saved = readShipContext();
      if (!ctx || !saved || shipContextsMatch(ctx, saved)) {
        window.sessionStorage.removeItem(SHIP_CONTEXT_KEY);
      }
    } catch (e) { /* storage unavailable */ }
    return true;
  }

  function shipContextFromJob(issue, job) {
    if (job && job.issue != null && Number(job.issue) !== Number(issue)) {
      throw new Error("ship status returned a different issue");
    }
    return normalizeShipContext({
      issue: issue,
      operationId: job && job.operation_id,
      requestId: job && job.request_id,
      statusUrl: (job && job.status_url) ||
        issueShipStatusUrl(issue, job && job.request_id),
    });
  }

  function newShipRequestId() {
    var bytes = new Uint8Array(16);
    if (window.crypto && typeof window.crypto.getRandomValues === "function") {
      window.crypto.getRandomValues(bytes);
    } else {
      // This identifier is an idempotency correlation key, not a credential.
      // The fallback keeps older embedded browsers functional while combining
      // time and per-byte randomness to make accidental reuse negligible.
      var seed = Date.now();
      for (var i = 0; i < bytes.length; i++) {
        bytes[i] = Math.floor(Math.random() * 256) ^ ((seed >>> ((i % 4) * 8)) & 255);
      }
    }
    return Array.prototype.map.call(bytes, function (value) {
      return value.toString(16).padStart(2, "0");
    }).join("");
  }

  function definiteShipPostRejection(err) {
    return !!(
      err && typeof err.status === "number" &&
      err.status >= 400 && err.status < 500
    );
  }

  function confirmedShipStatusNotFound(err) {
    return !!(
      err && err.status === 404 && err.data && err.data.ok === false &&
      /no matching ship job/i.test(err.data.error || err.data.message || "")
    );
  }

  function shipResult(job) {
    if (job && job.result && typeof job.result === "object") return job.result;
    if (job && job.response && typeof job.response === "object") return job.response;
    return job || {};
  }

  function isConfirmedShipResult(result) {
    return !!(
      result && result.ok !== false &&
      (result.outcome === "shipped" || result.outcome === "verified")
    );
  }

  function shipProgressText(status, job) {
    if (job && (job.message || job.detail)) return job.message || job.detail;
    if (status === "queued" || status === "pending") {
      return "Your review is saved and waiting to be published.";
    }
    return "Publishing the review to GitHub and updating the Aristotle queue. "
      + "This can take a minute when GitHub is busy.";
  }

  // Keep slow GitHub work visible instead of replacing it with a short-lived
  // toast. The review state remains in memory until the durable job succeeds.
  function showShipProgressCard(issue, status, job, reconnecting) {
    var detail = reconnecting
      ? "The ship job is still running. Reconnecting to its status…"
      : shipProgressText(status, job);
    var current = stage().querySelector(".ship-progress-card");
    if (current && Number(current.getAttribute("data-ship-issue")) === Number(issue)) {
      var currentDetail = current.querySelector(".msg-body");
      if (currentDetail && currentDetail.textContent !== detail) {
        currentDetail.textContent = detail;
      }
      return;
    }
    stage().innerHTML = "";
    var card = el("article", {
      class: "card card-message ship-progress-card",
      "data-ship-issue": String(issue),
      "aria-busy": "true",
    }, [
      el("div", { class: "ship-spinner", "aria-hidden": "true" }),
      el("h2", { class: "msg-title", text: "Shipping issue #" + issue + "…" }),
      el("p", { class: "msg-body", text: detail }),
    ]);
    stage().appendChild(card);
    setActionsEnabled(false);
  }

  function shipFailureMessage(status, job) {
    var detail = job && (job.message || job.error || job.detail);
    if (status === "awaiting_retry") {
      return (detail ? detail + " " : "")
        + "The deck restarted before it could confirm completion. Your review decisions "
        + "are saved; use the Ship button below to resume safely.";
    }
    return (detail ? detail + " " : "")
      + "Your review decisions are still saved. Check them below, then use the Ship "
      + "button again when you are ready to retry.";
  }

  // Restore the exact summary/subdeck that produced the job. A notice remains
  // visible above it, while the normal Ship button provides the only explicit
  // retry path (there is never an automatic second POST).
  function restoreShipSummary(issue, title, body, checkStatus, allowRetry) {
    allowRetry = allowRetry !== false;
    if (state.issue !== issue) {
      state.busy = false;
      flashToast(title + " for issue #" + issue);
      return;
    }
    state.busy = !allowRetry;
    if (!state.subdeck) {
      var messageActions = [];
      if (checkStatus) {
        messageActions.push({
          label: "Check shipping status",
          kind: "btn-primary",
          onClick: checkStatus,
        });
      }
      if (allowRetry) {
        messageActions.push({
          label: "Reload saved review",
          onClick: function () { openIssue(issue); },
        });
      }
      showMessageCard({
        title: title,
        body: body,
        actions: messageActions,
      });
      return;
    }
    state.showSummary = true;
    renderSummaryCard();
    var bannerActions = [];
    if (checkStatus) {
      bannerActions.push(el("button", {
        class: "btn btn-primary",
        type: "button",
        onclick: checkStatus,
      }, ["Check shipping status"]));
    }
    var banner = el("section", {
      class: "ship-status-banner",
      role: "alert",
    }, [
      el("div", { class: "ship-status-copy" }, [
        el("strong", { text: title }),
        el("p", { text: body }),
      ]),
      bannerActions.length ? el("div", { class: "ship-status-actions" }, bannerActions) : null,
    ]);
    stage().insertBefore(banner, stage().firstChild);
    if (!allowRetry) {
      var ship = document.getElementById("btn-ship");
      if (ship) {
        ship.disabled = true;
        ship.setAttribute("aria-disabled", "true");
      }
    }
  }

  function finishShipSuccess(issue, resp, ctx) {
    clearShipContext(ctx || state.activeShip);
    state.busy = false;
    resp = resp || {};
    var outcome = resp.outcome || "shipped";
    var msg = resp.message || ("issue #" + issue + " " + outcome);
    // The shipped issue left the ready queue.
    state.queue = state.queue.filter(function (c) { return c.id !== issue; });
    state.filesData = null;
    state.graphData = null;
    flashToast(msg);
    refreshStatus();
    // A ship may finish after the human moved to another issue. Update the
    // caches and status, but never erase or repaint that newer review.
    if (state.issue !== issue) return;

    state.subdeckRequestSeq += 1;
    state.subdeck = null;
    state.issue = null;
    state.showSummary = false;
    var adv = resp.advanced_to_issue;
    if (adv != null) {
      openIssue(adv);
    } else {
      // Refresh the queue; if empty -> "all caught up".
      refreshQueue();
    }
  }

  function pollShipJob(ctx, failedPolls, pollSeq) {
    if (pollSeq !== state.shipPollSeq ||
        !shipContextsMatch(ctx, state.activeShip)) {
      var cancelled = new Error("ship status poll superseded");
      cancelled.shipPollCancelled = true;
      return Promise.reject(cancelled);
    }
    return getJSONWithTimeout(ctx.statusUrl, SHIP_STATUS_TIMEOUT_MS).then(function (job) {
      var operationId = job && job.operation_id;
      if (ctx.operationId && operationId && operationId !== ctx.operationId) {
        throw new Error("ship status changed to a different operation");
      }
      var status = normalizedShipStatus(job);
      if (status === "succeeded" || status === "complete" || status === "completed") {
        if (!isConfirmedShipResult(shipResult(job))) {
          throw new Error("completed ship returned no recognized outcome");
        }
        return { terminal: "succeeded", job: job };
      }
      if (status === "failed" || status === "awaiting_retry") {
        return { terminal: status, job: job };
      }
      if (["running", "queued", "pending", "starting"].indexOf(status) === -1) {
        throw new Error("unrecognized ship status " + JSON.stringify(status));
      }
      if (state.issue === ctx.issue) {
        showShipProgressCard(ctx.issue, status, job, false);
      }
      return shipPollDelay(SHIP_POLL_DELAY_MS).then(function () {
        return pollShipJob(ctx, 0, pollSeq);
      });
    }, function (err) {
      if (confirmedShipStatusNotFound(err)) throw err;
      var failures = failedPolls + 1;
      if (failures > SHIP_POLL_MAX_FAILURES) throw err;
      if (state.issue === ctx.issue) {
        showShipProgressCard(ctx.issue, "running", null, true);
      }
      // Back off mildly during a connectivity wobble, but keep the normal
      // successful poll cadence responsive.
      var delay = Math.min(2500, SHIP_POLL_DELAY_MS * failures);
      return shipPollDelay(delay).then(function () {
        return pollShipJob(ctx, failures, pollSeq);
      });
    });
  }

  function resumeShipStatusPoll(ctx) {
    ctx = persistShipContext(ctx);
    if (!ctx) {
      return Promise.reject(new Error("invalid ship status context"));
    }
    state.busy = state.issue === ctx.issue;
    setActionsEnabled(false);
    if (state.issue === ctx.issue) {
      showShipProgressCard(ctx.issue, "running", null, false);
    }
    var pollSeq = ++state.shipPollSeq;
    return pollShipJob(ctx, 0, pollSeq)
      .then(function (terminal) {
        if (terminal.terminal === "succeeded") {
          finishShipSuccess(ctx.issue, shipResult(terminal.job), ctx);
          return;
        }
        clearShipContext(ctx);
        restoreShipSummary(
          ctx.issue,
          terminal.terminal === "awaiting_retry"
            ? "Shipping paused — safe retry needed"
            : "Shipping failed",
          shipFailureMessage(terminal.terminal, terminal.job),
          null,
          true
        );
      })
      .catch(function (err) {
        if (err && err.shipPollCancelled) return;
        if (confirmedShipStatusNotFound(err) && ctx.operationId) {
          // The exact operation disappeared or changed. A current client can
          // still reconcile through this click's request id; never fall back to
          // an unrelated "latest job for the issue" lookup.
          if (ctx.requestId) {
            return reconcileAmbiguousShip(ctx.issue, ctx.requestId, err);
          }
          restoreShipSummary(
            ctx.issue,
            "Could not confirm shipping status",
            "This older saved shipping session has no per-click identifier. "
              + "Ship remains disabled because another review round's result "
              + "must not be mistaken for this one.",
            null,
            false
          );
          return;
        }
        if (confirmedShipStatusNotFound(err)) {
          clearShipContext(ctx);
          restoreShipSummary(
            ctx.issue,
            "Ship was not accepted",
            "The server confirmed that no durable shipping job exists for issue #"
              + ctx.issue + ". Your saved review was not submitted, so it is safe to try again.",
            null,
            true
          );
          return;
        }
        restoreShipSummary(
          ctx.issue,
          "Could not read shipping status",
          ((err && err.message) || "network error")
            + ". The job may still be running. Ship remains disabled until its "
            + "durable status can be confirmed.",
          function () { resumeShipStatusPoll(ctx); },
          false
        );
      });
  }

  // A failed POST response is ambiguous: the server may have durably accepted
  // the job before a proxy/browser connection failed. Resolve that ambiguity
  // with the issue-only PURE GET. Only a specific backend JSON 404 proves that
  // no job exists and makes an explicit retry safe.
  function reconcileAmbiguousShip(issue, requestId, postError) {
    var provisional = persistShipContext({
      issue: issue,
      operationId: null,
      requestId: requestId,
      statusUrl: issueShipStatusUrl(issue, requestId),
    });
    state.busy = state.issue === issue;
    if (state.issue === issue) {
      showShipProgressCard(issue, "running", null, true);
    }
    return getJSONWithTimeout(
      provisional.statusUrl, SHIP_STATUS_TIMEOUT_MS
    ).then(function (job) {
      var status = normalizedShipStatus(job);
      var ctx = shipContextFromJob(issue, job) || provisional;
      persistShipContext(ctx);
      if (status === "succeeded" || status === "complete" || status === "completed") {
        var completedResult = shipResult(job);
        if (!isConfirmedShipResult(completedResult)) {
          throw new Error("completed ship returned no recognized outcome");
        }
        finishShipSuccess(issue, completedResult, ctx);
        return;
      }
      if (status === "failed" || status === "awaiting_retry") {
        clearShipContext(ctx);
        restoreShipSummary(
          issue,
          status === "awaiting_retry"
            ? "Shipping paused — safe retry needed"
            : "Shipping failed",
          shipFailureMessage(status, job),
          null,
          true
        );
        return;
      }
      if (["running", "queued", "pending", "starting"].indexOf(status) !== -1) {
        return resumeShipStatusPoll(ctx);
      }
      restoreShipSummary(
        issue,
        "Could not confirm shipping status",
        "The status service returned an unrecognized state. Ship remains disabled "
          + "until a durable result can be confirmed.",
        function () { reconcileAmbiguousShip(issue, requestId, postError); },
        false
      );
    }).catch(function (statusError) {
      if (confirmedShipStatusNotFound(statusError)) {
        clearShipContext(provisional);
        restoreShipSummary(
          issue,
          "Ship was not accepted",
          "The publish request could not be confirmed ("
            + ((postError && postError.message) || "network error")
            + "), and the server confirmed that no durable shipping job exists. "
            + "Your review was not submitted, so it is safe to press Ship again.",
          null,
          true
        );
        return;
      }
      // Timeout, 5xx, non-JSON proxy page, and connection failures are UNKNOWN,
      // not evidence that the irreversible request failed.
      persistShipContext(provisional);
      restoreShipSummary(
        issue,
        "Could not confirm whether shipping started",
        ((postError && postError.message) || "The publish request lost its response")
          + ". The status check also failed: "
          + ((statusError && statusError.message) || "network error")
          + ". Ship remains disabled; checking status is a pure read and will not submit again.",
        function () { reconcileAmbiguousShip(issue, requestId, postError); },
        false
      );
    });
  }

  function resumeSavedShipContext(ctx) {
    ctx = persistShipContext(ctx);
    if (!ctx) return false;
    state.issue = ctx.issue;
    state.showSummary = true;
    state.busy = true;
    showShipProgressCard(ctx.issue, "running", null, true);
    // Rehydrate the summary in parallel so a failed/paused terminal state can
    // restore the exact saved decisions. The status poll never waits on this
    // potentially slower read.
    fetchSubdeck(ctx.issue, state.skipApi)
      .then(function (sd) {
        if (!shipContextsMatch(ctx, state.activeShip)) return;
        state.subdeck = normalizeSubdeck(sd);
        state.showSummary = true;
      })
      .catch(function () { /* status remains authoritative; offer reload later */ });
    resumeShipStatusPoll(ctx);
    return true;
  }

  // shipIssue — THE SOLE CALLER OF postShip. This is the irreversible review
  // action. Reached ONLY from the confirm dialog opened by
  // the ship button on the completion summary — never on load, navigation,
  // a decl-verdict, or status polling.
  function shipIssue() {
    if (state.busy || !state.issue) return;
    if (state.subdeck && state.subdeck.shippable === false) {
      flashToast(
        state.subdeck.ship_blocked_reason ||
        "This provisional review cannot ship until its dependencies resolve"
      );
      return;
    }
    var issue = state.issue;
    // Capture the skip-API view state so the BACKEND realizes the API
    // auto-approval at ship (folding un-verdicted API decls into KEEP). This
    // is where auto-approval is REALIZED — never on the GET that built the
    // view. An overridden decl carries its own human verdict, so the backend
    // will not auto-approve it.
    var skipApi = !!state.skipApi;
    // Bind the irreversible action to the exact declarations + issue body the
    // human reviewed. The backend rejects a stale token instead of silently
    // shipping a newer/different subdeck.
    var reviewToken = state.subdeck && state.subdeck.review_token;
    var requestId = newShipRequestId();
    var provisionalContext = persistShipContext({
      issue: issue,
      operationId: null,
      requestId: requestId,
      statusUrl: issueShipStatusUrl(issue, requestId),
    });
    state.busy = true;
    setActionsEnabled(false);
    var shipButton = document.getElementById("btn-ship");
    if (shipButton) {
      shipButton.disabled = true;
      shipButton.setAttribute("aria-disabled", "true");
    }
    showShipProgressCard(issue, "starting", null, false);
    postShip(issue, skipApi, reviewToken, requestId)
      .then(function (resp) {
        var status = normalizedShipStatus(resp);
        if (status === "succeeded" || status === "complete" || status === "completed") {
          var completedResult = shipResult(resp);
          if (!isConfirmedShipResult(completedResult)) {
            throw new Error("completed ship returned no recognized outcome");
          }
          finishShipSuccess(issue, completedResult);
          return;
        }
        var statusUrl = shipStatusUrl(resp && resp.status_url);
        if (statusUrl || status === "queued" || status === "pending" || status === "running") {
          if (!statusUrl) {
            throw new Error("ship was accepted, but no status URL was returned");
          }
          resumeShipStatusPoll({
            issue: issue,
            operationId: resp && resp.operation_id,
            requestId: (resp && resp.request_id) || requestId,
            statusUrl: statusUrl,
          });
          return;
        }
        // Compatibility with an older/local deck that still completes the
        // entire ship request synchronously with a 200 response. Never infer
        // success from an arbitrary/malformed {ok:true} payload.
        if (isConfirmedShipResult(resp)) {
          finishShipSuccess(issue, resp);
          return;
        }
        throw new Error("ship returned an unrecognized response");
      })
      .catch(function (err) {
        if (definiteShipPostRejection(err)) {
          // A parsed 4xx is an authoritative refusal, not a lost response.
          // Looking up "the latest job for this issue" here could mistake an
          // older review round's success for this rejected click.
          clearShipContext(provisionalContext);
          restoreShipSummary(
            issue,
            "Ship was not accepted",
            ((err && err.message) || "The server refused this review") +
              ". Nothing was shipped.",
            null,
            true
          );
          return;
        }
        // Never equate a lost response with a failed irreversible operation.
        // The request-id-bound status GET is pure and tells us whether THIS
        // click was durably accepted before the connection failed.
        return reconcileAmbiguousShip(issue, requestId, err);
      });
  }

  // --- modals --------------------------------------------------------------
  var modalDialogSeq = 0;

  function modalFocusable(node) {
    var selector = [
      "a[href]", "button:not([disabled])", "input:not([disabled])",
      "select:not([disabled])", "textarea:not([disabled])",
      "[tabindex]:not([tabindex='-1'])",
    ].join(",");
    var all = node.querySelectorAll(selector);
    var out = [];
    for (var i = 0; i < all.length; i++) {
      var hiddenAncestor = typeof all[i].closest === "function"
        ? all[i].closest("[hidden]") : null;
      if (!hiddenAncestor && !all[i].hasAttribute("hidden") &&
          all[i].getAttribute("aria-hidden") !== "true") {
        out.push(all[i]);
      }
    }
    return out;
  }

  function isModalOpen() {
    return !modalRoot().hasAttribute("hidden");
  }
  function closeModal() {
    var root = modalRoot();
    var onClose = root._marathonOnClose;
    var returnFocus = root._marathonReturnFocus;
    if (root._marathonKeyHandler) {
      root.removeEventListener("keydown", root._marathonKeyHandler);
    }
    root._marathonOnClose = null;
    root._marathonReturnFocus = null;
    root._marathonKeyHandler = null;
    root.setAttribute("hidden", "hidden");
    root.innerHTML = "";
    if (typeof onClose === "function") onClose();
    // Restore focus after any onClose re-render. If the trigger disappeared in
    // that render, leave focus alone rather than jumping somewhere surprising.
    setTimeout(function () {
      if (returnFocus && document.contains(returnFocus) &&
          typeof returnFocus.focus === "function") returnFocus.focus();
    }, 0);
  }
  function openModal(node, onClose, returnFocus) {
    var root = modalRoot();
    root.innerHTML = "";
    root._marathonOnClose = onClose || null;
    root._marathonReturnFocus = returnFocus || document.activeElement || null;
    if (!node.hasAttribute("role")) node.setAttribute("role", "dialog");
    if (!node.hasAttribute("aria-modal")) node.setAttribute("aria-modal", "true");
    if (!node.hasAttribute("tabindex")) node.setAttribute("tabindex", "-1");
    if (!node.hasAttribute("aria-labelledby")) {
      var modalTitle = node.querySelector(".modal-title");
      if (modalTitle) {
        if (!modalTitle.id) modalTitle.id = "modal-title-" + (++modalDialogSeq);
        node.setAttribute("aria-labelledby", modalTitle.id);
      } else {
        node.setAttribute("aria-label", "Dialog");
      }
    }
    var backdrop = el("div", { class: "modal-backdrop" }, [node]);
    backdrop.addEventListener("click", function (e) {
      if (e.target === backdrop) closeModal();
    });
    root.appendChild(backdrop);
    root.removeAttribute("hidden");
    root._marathonKeyHandler = function (e) {
      if (e.key === "Escape") {
        e.preventDefault();
        e.stopPropagation();
        closeModal();
        return;
      }
      if (e.key !== "Tab") return;
      var focusable = modalFocusable(node);
      if (!focusable.length) {
        e.preventDefault();
        node.focus();
        return;
      }
      var first = focusable[0];
      var last = focusable[focusable.length - 1];
      if (e.shiftKey && (document.activeElement === first ||
          !node.contains(document.activeElement))) {
        e.preventDefault();
        last.focus();
      } else if (!e.shiftKey && document.activeElement === last) {
        e.preventDefault();
        first.focus();
      }
    };
    root.addEventListener("keydown", root._marathonKeyHandler);
    setTimeout(function () {
      if (root.hasAttribute("hidden") || !root.contains(node)) return;
      var autofocus = node.querySelector("[autofocus]:not([disabled])");
      var focusable = modalFocusable(node);
      (autofocus || focusable[0] || node).focus();
    }, 0);
  }

  // Draft persistence: a typed rejection note survives Cancel, navigation,
  // and reload. localStorage keyed per (issue, decl); cleared on submit.
  function draftKey(issue, decl) {
    return "marathon:draft:" + issue + ":" + (decl || "informal");
  }
  function readDraft(issue, decl) {
    try { return window.localStorage.getItem(draftKey(issue, decl)) || ""; }
    catch (e) { return ""; }
  }
  function writeDraft(issue, decl, text) {
    try {
      if (text && text.trim()) {
        window.localStorage.setItem(draftKey(issue, decl), text);
      } else {
        window.localStorage.removeItem(draftKey(issue, decl));
      }
    } catch (e) { /* private mode / quota — drafts are best-effort */ }
  }
  function clearDraft(issue, decl) {
    try { window.localStorage.removeItem(draftKey(issue, decl)); }
    catch (e) { /* best-effort */ }
  }
  function attachDraftPersistence(ta, issue, decl) {
    var restored = readDraft(issue, decl);
    if (restored) ta.value = restored;
    var timer = null;
    ta.addEventListener("input", function () {
      if (timer) clearTimeout(timer);
      timer = setTimeout(function () {
        writeDraft(issue, decl, ta.value);
      }, 300);
    });
    return !!restored;
  }

  function openRejectNote() {
    var decl = state.currentDecl ? state.currentDecl.name : "";
    var ta = el("textarea", {
      class: "reject-note",
      rows: "5",
      placeholder: "Why is this declaration wrong? This note is composed into the issue's fix instructions for Aristotle.",
      "aria-label": "Rejection note (required, composed into the issue's Aristotle fix instructions verbatim)",
    });
    var draftRestored = attachDraftPersistence(ta, state.issue, decl);
    var err = el("div", { class: "modal-err", hidden: "hidden" });
    var form = el("form", { class: "reject-composer", "data-no-swipe": "true" }, [
      el("p", { class: "inspector-hint" }, [
        "Your note is ",
        el("strong", { text: "composed verbatim into the issue's fix instructions for Aristotle" }),
        " when you ship. A note is required. (Recording it now is LOCAL — nothing dispatches yet.)",
      ]),
      draftRestored ? el("p", { class: "inspector-hint draft-hint",
        text: "Draft restored — cancelling keeps it." }) : null,
      ta,
      err,
      el("div", { class: "inspector-actions" }, [
        el("button", { class: "btn", type: "button", onclick: closeReviewInspector }, ["Cancel"]),
        el("button", {
          class: "btn btn-danger",
          type: "submit",
        }, ["Reject declaration"]),
      ]),
    ]);
    form.addEventListener("submit", function (e) {
      e.preventDefault();
      var note = ta.value.trim();
      if (!note) {
        err.textContent = "A rejection note is required (it is composed into the Aristotle fix instructions).";
        err.removeAttribute("hidden");
        ta.focus();
        return;
      }
      // Do not run the cancel callback while transitioning into the POST.
      var host = inspectorHost();
      if (host) host._marathonOnClose = null;
      state.rejectComposerOpen = false;
      setComposerNavigationLocked(false);
      closeReviewInspector();
      clearDraft(state.issue, decl);
      commitDeclVerdict("reject", note); // verbatim, no client rewrite
    });
    state.rejectComposerOpen = true;
    setActionsEnabled(false);
    var opened = openReviewInspector("Reject " + decl, [form], function () {
      state.rejectComposerOpen = false;
      setComposerNavigationLocked(false);
      setActionsEnabled(true);
    });
    if (!opened) {
      state.rejectComposerOpen = false;
      setActionsEnabled(true);
      return;
    }
    setComposerNavigationLocked(true);
    setTimeout(function () { ta.focus(); }, 0);
  }

  function openDeps() {
    openDependencyMap();
  }

  function openDeepDive() {
    // Render a durable, normal anchor instead of relying on window.open (which
    // popup blockers can discard with no visible result). Pure navigation.
    openResourceLinks();
  }

  // === status strip ========================================================
  function applyStatus(s) {
    var prevBuilding = state.building;
    var prevLanded = state.landedToday;
    state.building = s.building || 0;
    state.landedToday = s.landed_today || 0;
    var b = document.getElementById("status-building");
    var l = document.getElementById("status-landed");
    if (b) b.textContent = state.building + " building";
    if (l) l.textContent = state.landedToday + " landed today";
    if (s.jobs && s.jobs.length) renderJobProgress(s.jobs);
    if (s.source_token) {
      refreshDefinitionTerms(s.source_token, false);
      observeVersoSourceToken(s.source_token);
    }
    // Conductor activity that can change readiness (a job finished or work
    // landed) refetches the queue so newly-ready issues appear without a
    // manual Refresh — but never while a subdeck review is on the stage, and
    // never more often than the debounce window.
    var finishedSomething =
      (typeof prevBuilding === "number" && state.building < prevBuilding) ||
      (typeof prevLanded === "number" && state.landedToday > prevLanded);
    if (finishedSomething && state.activeTab === "review" && !state.subdeck) {
      var now = Date.now();
      if (!state.autoQueueRefreshAt || now - state.autoQueueRefreshAt > 5000) {
        state.autoQueueRefreshAt = now;
        refreshQueue();
      }
    }
  }
  function renderJobProgress(jobs) {
    var conn = document.getElementById("status-conn");
    if (!conn) return;
    var active = jobs.filter(function (j) { return j.percent != null; });
    if (!active.length) { conn.textContent = ""; return; }
    var best = active[0];
    conn.textContent = " · " + (best.label || "job") + " " + Math.round(best.percent) + "%";
  }

  var statusSource = null;
  function startStatus() {
    // Prefer SSE; fall back to polling. Both are PURE READS — they never POST.
    if (typeof EventSource !== "undefined") {
      try {
        statusSource = new EventSource("/api/events");
        var onFrame = function (e) {
          try { applyStatus(JSON.parse(e.data)); } catch (x) {}
        };
        // The backend emits a NAMED event ("event: status"); EventSource only
        // routes that to a "status" listener, not onmessage. Listen for both
        // so the SSE frame is never silently dropped.
        statusSource.onmessage = onFrame;
        statusSource.addEventListener("status", onFrame);
        statusSource.onerror = function () {
          if (statusSource) { statusSource.close(); statusSource = null; }
          markConn("poll");
          startPolling();
        };
        markConn("live");
        return;
      } catch (e) { /* fall through to polling */ }
    }
    markConn("poll");
    startPolling();
  }
  var pollTimer = null;
  function startPolling() {
    if (pollTimer) return;
    refreshStatus();
    pollTimer = setInterval(refreshStatus, 5000);
  }
  function refreshStatus() {
    return fetchStatus().then(applyStatus).catch(function () { markConn("offline"); });
  }
  function markConn(kind) {
    var conn = document.getElementById("status-conn");
    if (!conn) return;
    conn.className = "status-conn conn-" + kind;
  }

  // === queue / chapter =====================================================
  var QUEUE_READ_TIMEOUT_MS = 45000;
  var REVIEW_STUCK_READ_TIMEOUT_MS = 5000;
  var QUEUE_WARM_RETRY_MS = 2000;
  var QUEUE_STALE_RETRY_MS = 6000;

  function abortQueueReads() {
    if (state.queueAbortController) state.queueAbortController.abort();
    if (state.queueStuckAbortController) {
      state.queueStuckAbortController.abort();
    }
  }

  function scheduleQueueWarmRetry(generation, delayMs, minimumMs) {
    if (state.queueWarmRetryTimer) {
      clearTimeout(state.queueWarmRetryTimer);
      state.queueWarmRetryTimer = null;
    }
    var delay = Math.max(
      Number(minimumMs) || QUEUE_WARM_RETRY_MS,
      Number(delayMs) || 0
    );
    state.queueWarmRetryTimer = setTimeout(function () {
      state.queueWarmRetryTimer = null;
      if (generation !== state.queueRequestSeq ||
          state.activeTab !== "review") return;
      var nextGeneration = ++state.queueRequestSeq;
      abortQueueReads();
      // This is a quiet observation of the server's background refresh.  Keep
      // the mounted Review card intact; only a changed completed snapshot may
      // repaint it, avoiding the old retry/loading flicker.
      startQueueRefresh(
        nextGeneration, surfaceHasStableContent(stage())
      );
    }, delay);
  }

  function refreshQueue() {
    setActionsEnabled(false);
    if (state.queueWarmRetryTimer) {
      clearTimeout(state.queueWarmRetryTimer);
      state.queueWarmRetryTimer = null;
    }
    var generation = ++state.queueRequestSeq;
    var hadContent = beginSurfaceRefresh(
      stage(), "card-loading", "refreshing queue…"
    );
    // An explicit retry must never sit behind a fetch that the proxy/browser
    // left pending. Abort it when supported and immediately start a newer
    // generation; without AbortController the generation guard still makes
    // the older response observationally inert.
    abortQueueReads();
    return startQueueRefresh(generation, hadContent);
  }

  function startQueueRefresh(generation, hadContent) {
    // Queue data is the primary Review read. Parked/recovery status is useful
    // context, but it must never hold a usable queue behind a slow endpoint.
    // Run it independently with its own deadline/controller and merge it only
    // when this generation is still current.
    var controller = newReadController();
    var options = readOptions(controller);
    var stuckController = newReadController();
    var stuckOptions = readOptions(stuckController);
    var installedQueueData = null;
    var stuckSettled = false;
    var stuckCancelledByPrimary = false;
    var stuckNeedsRetry = false;
    var queueRenderPending = false;

    var stuckRequest = withReadDeadline(
      getJSON("/api/stuck", stuckOptions),
      REVIEW_STUCK_READ_TIMEOUT_MS,
      stuckController,
      "Recovery-status refresh"
    ).then(function (data) {
      return { ok: true, data: data || {} };
    }, function () {
      return { ok: false, data: null };
    });
    var stuckCompletion = stuckRequest.then(function (stuckRead) {
      stuckSettled = true;
      var currentStuck = state.queueStuckRequest === stuckCompletion;
      if (currentStuck) {
        state.queueStuckRequest = null;
        if (state.queueStuckAbortController === stuckController) {
          state.queueStuckAbortController = null;
        }
      }
      if (stuckCancelledByPrimary || generation !== state.queueRequestSeq) {
        return stuckRead;
      }
      state.reviewStuckPending = false;
      if (stuckRead.ok) {
        state.reviewStuckChecked = true;
        state.reviewStuckUnavailable = false;
        state.reviewStuck = {
          issues: ((stuckRead.data && stuckRead.data.issues) || []).slice(),
          repo: (stuckRead.data && stuckRead.data.repo) || null,
        };
        state.reviewConductor =
          (stuckRead.data && stuckRead.data.conductor) || null;
      } else {
        state.reviewStuckChecked = false;
        state.reviewStuckUnavailable = true;
        stuckNeedsRetry = true;
      }
      if (installedQueueData) {
        state.queueSnapshotKey = reviewQueueSnapshotKey(installedQueueData);
        if (!queueRenderPending && state.issue == null) openFirstReadyIssue();
        if (stuckNeedsRetry) {
          scheduleQueueWarmRetry(
            generation, QUEUE_STALE_RETRY_MS, QUEUE_STALE_RETRY_MS
          );
        }
      }
      return stuckRead;
    });
    state.queueStuckRequest = stuckCompletion;
    state.queueStuckAbortController = stuckController;

    function cancelAuxiliaryStuckRead() {
      stuckCancelledByPrimary = true;
      if (stuckController) stuckController.abort();
      if (generation === state.queueRequestSeq) {
        state.reviewStuckPending = false;
      }
    }

    var request = withReadDeadline(
      fetchQueue(null, options),
      QUEUE_READ_TIMEOUT_MS,
      controller,
      "Queue refresh"
    );
    var completion = request.then(function (data) {
      if (generation !== state.queueRequestSeq) return null;
      data = data || {};
      installedQueueData = data;
      queueRenderPending = true;
      state.reviewStuckPending = !state.reviewStuckChecked && !stuckSettled;
      if (state.reviewStuckPending) state.reviewStuckUnavailable = false;

      // Install and render the useful queue immediately.  The independent
      // recovery read above may enrich an empty Review card later.
      state.queue = (data.cards || []).slice();
      state.building = data.building || 0;
      state.viewBuilding = data.view_building || 0;
      state.landedToday = data.landed_today || 0;
      state.unresolved = data.unresolved || 0;
      state.awaitingRefinement = data.awaiting_refinement || 0;
      state.needsAttention = data.needs_attention || 0;
      state.needsCitationMapping = data.needs_citation_mapping || 0;
      state.awaitingAudit = data.awaiting_audit || 0;
      state.dependencyBlocked = data.dependency_blocked || 0;
      state.otherUnresolved = data.other_unresolved || 0;
      state.queueFreshness = data.queue_snapshot || null;
      state.queueSnapshotKey = reviewQueueSnapshotKey(data);
      applyStatus({ building: state.building, landed_today: state.landedToday });

      // Two microtask turns coalesce an already-completed auxiliary read
      // without waiting on one that is actually slow.  The queue state above
      // is installed immediately; this only avoids painting an intermediate
      // empty card when /api/stuck finished in the same event-loop turn.
      return Promise.resolve().then(function () {
        return Promise.resolve();
      }).then(function () {
        queueRenderPending = false;
        if (generation !== state.queueRequestSeq) return null;
        state.queueSnapshotKey = reviewQueueSnapshotKey(data);
        // The queue is the issue PICKER: drop straight into the first ready
        // issue's subdeck. Preserve a newer issue opened while reads settled.
        if (state.issue == null) openFirstReadyIssue();
        if (state.queueFreshness && state.queueFreshness.state === "stale") {
          scheduleQueueWarmRetry(
            generation,
            1000 * (state.queueFreshness.retry_after_seconds || 0),
            QUEUE_STALE_RETRY_MS
          );
        } else if (stuckNeedsRetry || state.reviewStuckUnavailable) {
          scheduleQueueWarmRetry(
            generation, QUEUE_STALE_RETRY_MS, QUEUE_STALE_RETRY_MS
          );
        } else if (state.queueWarmRetryTimer) {
          clearTimeout(state.queueWarmRetryTimer);
          state.queueWarmRetryTimer = null;
        }
        return data;
      });
    }, function (err) {
      if (generation !== state.queueRequestSeq) return null;
      // A primary timeout/warming response must not leave an auxiliary
      // /api/stuck fetch alive across automatic retries.
      cancelAuxiliaryStuckRead();
      var queueSnapshot = err && err.data && err.data.queue_snapshot;
      if (queueSnapshot && queueSnapshot.state === "warming") {
        state.queueFreshness = queueSnapshot;
        if (!hadContent && !surfaceHasStableContent(stage())) {
          showMessageCard({
            title: "Preparing review queue",
            body: "The server is building the first queue snapshot. This page " +
              "will update automatically; no manual retry is needed.",
          });
        }
        scheduleQueueWarmRetry(generation, QUEUE_WARM_RETRY_MS);
        return null;
      }
      if (hadContent || surfaceHasStableContent(stage())) {
        endSurfaceRefresh(stage());
        if (state.activeTab === "review") {
          flashToast("Could not refresh the queue: " +
            ((err && err.message) || "network error"));
        }
      } else {
        showErrorCard("Could not load the queue", err, refreshQueue);
      }
      return null;
    }).then(function (result) {
      var current = state.queueRequest === completion;
      if (current) {
        state.queueRequest = null;
        if (state.queueAbortController === controller) {
          state.queueAbortController = null;
        }
      }
      if (generation !== state.queueRequestSeq) return result;
      endSurfaceRefresh(stage());
      return result;
    }, function (err) {
      var current = state.queueRequest === completion;
      if (current) {
        state.queueRequest = null;
        if (state.queueAbortController === controller) {
          state.queueAbortController = null;
        }
      }
      if (generation !== state.queueRequestSeq) return null;
      cancelAuxiliaryStuckRead();
      endSurfaceRefresh(stage());
      if (hadContent || surfaceHasStableContent(stage())) {
        if (state.activeTab === "review") {
          flashToast("Could not refresh the queue: " +
            ((err && err.message) || "rendering error"));
        }
      } else {
        showErrorCard("Could not load the queue", err, refreshQueue);
      }
      return null;
    });
    state.queueRequest = completion;
    state.queueAbortController = controller;
    return completion;
  }

  // === action bar enable/disable ==========================================
  var ACTION_BTNS = ["btn-reject", "btn-defer", "btn-deps", "btn-deepdive", "btn-verify"];
  function setActionsEnabled(on) {
    ACTION_BTNS.forEach(function (id) {
      var b = document.getElementById(id);
      // The leading informal card has no source/inspector host yet. Keep its
      // verdict controls active, but do not present deps/open as clickable
      // no-ops until a declaration card is on stage.
      var needsDecl = id === "btn-deps" || id === "btn-deepdive";
      if (b) b.disabled = !on || (needsDecl && state.informalActive);
    });
    // Summary rendering disables the declaration action bar, but its Ship
    // button remains available unless a ship is actually in flight.
    var ship = document.getElementById("btn-ship");
    if (ship) {
      ship.disabled = !!state.busy;
      ship.setAttribute("aria-disabled", state.busy ? "true" : "false");
    }
  }

  // === toast / animation ===================================================
  function flashToast(msg) {
    var t = el("div", { class: "toast", text: msg });
    document.body.appendChild(t);
    setTimeout(function () { t.classList.add("toast-show"); }, 10);
    setTimeout(function () {
      t.classList.remove("toast-show");
      setTimeout(function () { t.remove(); }, 300);
    }, 1600);
  }
  function animateOut(verdict) {
    // A fixed desktop inspector is nested in the card so it can be destroyed
    // atomically with that card. Close it before applying a swipe/fly transform:
    // transformed ancestors redefine fixed positioning and would otherwise
    // make the side rail jump into, rotate with, and leave the viewport.
    if (isReviewInspectorOpen()) closeReviewInspector();
    var card = document.getElementById("active-card");
    if (!card) return;
    card.classList.add(
      verdict === "verify" ? "fly-right" : verdict === "reject" ? "fly-left" : "fly-down"
    );
  }

  // === pointer (swipe) handling ===========================================
  // Drag a DECLARATION card; release past the threshold = verify (right) /
  // reject (left). These are deliberate user gestures — they route through
  // the SAME per-decl verify/reject handlers as the buttons/keys (which open
  // the reject note). Starting a drag and releasing back within the
  // threshold does NOTHING (no verdict, no ship).
  function attachSwipe() {
    var dragging = false, startX = 0, startY = 0, dx = 0, dy = 0, target = null;
    var THRESH = 110;
    function onDown(e) {
      if (state.activeTab !== "review") return;   // swipe is Review-only
      // Side inspectors are reading/editing surfaces, not swipe handles. This
      // also guarantees the desktop fixed rail never acquires the transformed
      // card as its containing block during a drag.
      if (isReviewInspectorOpen()) return;
      var card = document.getElementById("active-card");
      // A decl card (canAct) OR the leading informal-approval card (canActInformal)
      // is draggable; both route through actVerify/actReject on release.
      if (!card || !(canAct() || canActInformal())) return;
      // ignore drags that start on interactive children (buttons etc.)
      if (e.target.closest("button, a, textarea, select, details, summary, [data-no-swipe]")) return;
      dragging = true; target = card;
      startX = e.clientX; startY = e.clientY; dx = 0; dy = 0;
      card.setPointerCapture && card.setPointerCapture(e.pointerId);
      card.classList.add("dragging");
    }
    function onMove(e) {
      if (!dragging || !target) return;
      dx = e.clientX - startX; dy = e.clientY - startY;
      target.style.transform = "translate(" + dx + "px," + dy * 0.3 + "px) rotate(" + dx / 30 + "deg)";
      target.classList.toggle("hint-verify", dx > 40);
      target.classList.toggle("hint-reject", dx < -40);
    }
    function onUp() {
      if (!dragging || !target) return;
      dragging = false;
      var t = target; target = null;
      t.classList.remove("dragging", "hint-verify", "hint-reject");
      t.style.transform = "";
      // Released back within threshold => no-op (no verdict committed).
      if (dx > THRESH) { actVerify(); }
      else if (dx < -THRESH) { actReject(); }
    }
    stage().addEventListener("pointerdown", onDown);
    window.addEventListener("pointermove", onMove);
    window.addEventListener("pointerup", onUp);
    window.addEventListener("pointercancel", onUp);
  }

  // === keyboard ============================================================
  function attachKeys() {
    document.addEventListener("keydown", function (e) {
      if (isModalOpen()) {
        if (e.key === "Escape") closeModal();
        return; // don't fire actions while a modal is up
      }
      if (e.key === "Escape" && isReviewInspectorOpen()) {
        e.preventDefault();
        closeReviewInspector();
        return;
      }
      // ignore typing into inputs
      if (e.target && /^(input|textarea|select)$/i.test(e.target.tagName)) return;
      // Tab switching (1-7, matching the visible tab order) works from any
      // tab; "?" help too.
      if (e.key === "1") return switchTab("review");
      if (e.key === "2") return switchTab("files");
      if (e.key === "3") return switchTab("graph");
      if (e.key === "4") return switchTab("status");
      if (e.key === "5") return switchTab("issues");
      if (e.key === "6") return switchTab("history");
      if (e.key === "7") return switchTab("add-chapter");
      if (e.key === "?") return openHelp();
      // The card actions (verify/reject/defer/deps/open) only fire on the
      // Review tab — they act on the card stage, which is hidden otherwise.
      if (state.activeTab !== "review") return;
      // On the landing (no subdeck open) the arrows/j/k walk the queue rows
      // and Enter opens the focused one.
      if (state.issue == null && state.landingRows && state.landingRows.length) {
        if (e.key === "ArrowDown" || e.key === "j" || e.key === "J") {
          e.preventDefault(); landingMoveFocus(1); return;
        }
        if (e.key === "ArrowUp" || e.key === "k" || e.key === "K") {
          e.preventDefault(); landingMoveFocus(-1); return;
        }
        if (e.key === "Enter") {
          e.preventDefault(); landingOpenFocused(); return;
        }
      }
      // "s" ships from the completion summary: it routes through the ship
      // BUTTON (the sole entrypoint), so the confirm modal still gates the
      // irreversible POST exactly as a click would.
      if (e.key === "s" || e.key === "S") {
        var shipButton = document.getElementById("btn-ship");
        if (shipButton && !shipButton.disabled) {
          e.preventDefault();
          shipButton.click();
        }
        return;
      }
      switch (e.key) {
        case "ArrowRight": e.preventDefault(); actVerify(); break;
        case "ArrowLeft": e.preventDefault(); actReject(); break;
        case " ": case "Spacebar": case "ArrowDown": e.preventDefault(); actDefer(); break;
        case "a": case "A": e.preventDefault(); actReaffirm(); break;
        case "d": case "D": openDeps(); break;
        case "o": case "O": openDeepDive(); break;
        default: break;
      }
    });
  }

  function openHelp() {
    var panel = el("div", { class: "modal-panel" }, [
      el("h2", { class: "modal-title", text: "Keyboard & gestures" }),
      el("ul", { class: "help-list" }, [
        el("li", { text: "→ / drag right — verify this declaration (LOCAL — does not ship)" }),
        el("li", { text: "← / drag left — reject (requires a note, composed into the issue's Aristotle fix)" }),
        el("li", { text: "space / ↓ — defer this declaration" }),
        el("li", { text: "a — re-affirm a changed declaration's previous verdict (when a diff is shown)" }),
        el("li", { text: "s — SHIP from the completion summary (opens the confirm dialog; Enter confirms)" }),
        el("li", { text: "d — declaration dependency map + predecessor issues" }),
        el("li", { text: "o — source path + GitHub issue link" }),
        el("li", { text: "1–7 — switch tabs (Review, Files, Graph, Status, Issues, History, Add Chapter)" }),
        el("li", { text: "↑ / ↓ or j / k, Enter — move through and open queue rows on the Review landing" }),
        el("li", { text: "? — this help" }),
      ]),
      el("p", { class: "modal-sub", text:
        "Skip API (header toggle): show only the main results; easy API " +
        "defs/lemmas are AUTO-APPROVED on ship — their statements are kept " +
        "without individual review. Pull any one back from the panel to review it." }),
      el("p", { class: "modal-sub", text:
        "Swiping records a LOCAL per-declaration verdict. Nothing merges or " +
        "dispatches until you confirm SHIP on the completion summary." }),
      el("p", { class: "modal-sub", text:
        "Reconciliation: loading the deck only builds a read-only view — stale " +
        "citations move to a footnote and uncited live declarations surface as " +
        "cards. An explicit SHIP may add kept surfaced declarations to the issue. " +
        "The conductor can also sync mechanically safe additions after code moves; " +
        "rename/split candidates require human confirmation and deletions remain " +
        "an explicit action." }),
      el("div", { class: "modal-actions" }, [
        el("button", { class: "btn", type: "button", onclick: function () { closeModal(); } }, ["Close"]),
      ]),
    ]);
    openModal(panel);
  }

  // === dashboard tabs (Status / Issues / History) =========================
  // Read-only views over the conductor + review state. Each fetches its
  // endpoint on activation (Status also auto-refreshes) and renders into its
  // pane. The Review tab is the existing card flow, untouched. Tab switching
  // and every dashboard fetch are PURE READS — they never POST.

  var DASH_POLL_MS = 5000;
  var dashTimer = null;

  function dashHost(id) { return document.getElementById(id); }

  // Compact, locale-free "MM-DD HH:MM" from an ISO-ish timestamp.
  function fmtTs(ts) {
    if (!ts) return "";
    var m = String(ts).match(/(\d{4})-(\d{2})-(\d{2})[T ](\d{2}):(\d{2})/);
    return m ? (m[2] + "-" + m[3] + " " + m[4] + ":" + m[5]) : String(ts).slice(0, 16);
  }

  // A status pill. The class keys off the FIRST word so "running (pid dead)"
  // still maps to .sb-running.
  function statusBadge(status) {
    var key = String(status || "pending").split(" ")[0];
    return el("span", { class: "sb sb-" + key, text: status || "pending" });
  }

  function statChip(num, label) {
    return el("div", { class: "stat-chip" }, [
      el("div", { class: "stat-num", text: String(num) }),
      el("div", { class: "stat-label", text: label }),
    ]);
  }

  function dashError(hostId, err) {
    var host = dashHost(hostId);
    if (!host) return;
    host.innerHTML = "";
    host.appendChild(el("div", { class: "dash-empty",
      text: "could not load: " + ((err && err.message) || err) }));
  }

  function switchTab(name) {
    if (!name) return;
    if (state.activeTab !== name) state.definitionRequestSeq += 1;
    state.activeTab = name;
    var btns = document.querySelectorAll(".tab-btn");
    for (var i = 0; i < btns.length; i++) {
      var on = btns[i].getAttribute("data-tab") === name;
      btns[i].classList.toggle("active", on);
      btns[i].setAttribute("aria-selected", on ? "true" : "false");
    }
    ["review", "files", "graph", "status", "issues", "history", "add-chapter"].forEach(function (t) {
      var pane = document.getElementById("tab-" + t);
      if (!pane) return;
      if (t === name) pane.removeAttribute("hidden");
      else pane.setAttribute("hidden", "hidden");
    });
    // The hotkey legend (←/→/space/d/o) only applies to the review/swipe flow;
    // it lives at the body level, so hide it on the dashboard tabs.
    var legend = document.querySelector(".legend");
    if (legend) legend.hidden = name !== "review";
    // Stop any prior tab's auto-refresh.
    if (dashTimer) { clearInterval(dashTimer); dashTimer = null; }
    if (name === "status") {
      renderStatusTab(true);
      dashTimer = setInterval(renderStatusTab, DASH_POLL_MS);
    } else if (name === "files") {
      renderFilesTab();
    } else if (name === "graph") {
      renderGraphTab();
    } else if (name === "issues") {
      renderIssuesTab();
    } else if (name === "history") {
      renderHistoryTab();
    } else if (name === "add-chapter") {
      renderAddChapterTab();
    }
    // Review tab renders on its own (event-driven); nothing to do here.
  }

  // --- Files tab: chapter/file reading + contextual/batch review ----------

  function readerTargets(data) {
    var out = [];
    ((data && data.files) || []).forEach(function (file) {
      (file.declarations || []).forEach(function (target) { out.push(target); });
    });
    return out;
  }

  function readerTargetMatches(target, query, filter) {
    filter = filter || "all";
    if (filter === "provisional" && !target.provisional) return false;
    if (filter === "unreviewed" && target.verdict) return false;
    if (filter === "rejected" && target.verdict !== "rejected") return false;
    if (filter === "changed" &&
        !target.changed_since_review && !target.dependency_invalidated) return false;
    if (filter === "surfaced" && !target.surfaced) return false;
    query = String(query || "").trim().toLowerCase();
    if (!query) return true;
    return [
      target.name, target.source_file, target.issue_title,
      target.issue, target.kind, target.statement_pp,
    ].some(function (value) {
      return value != null && String(value).toLowerCase().indexOf(query) >= 0;
    });
  }

  function readerVisibleTargets(data) {
    return readerTargets(data).filter(function (target) {
      return readerTargetMatches(target, state.filesQuery, state.filesFilter);
    });
  }

  function readerVerdict(target, verdict, note, button) {
    if (state.filesBusy) return;
    if (verdict !== "defer" && target.inspectable === false) {
      flashToast(
        target.inspection_blocked_reason ||
        "This declaration needs current machine-audit evidence before review"
      );
      return;
    }
    if (verdict !== "defer" && (target.blocked_by || []).length) {
      flashToast("Waiting on " + target.blocked_by.join(", ") + " — defer or repair it first");
      return;
    }
    state.filesBusy = true;
    if (button) button.disabled = true;
    postDeclVerdict(
      target.issue, target.name, verdict, note, target.context_fingerprint
    )
      .then(function () {
        state.filesBusy = false;
        state.graphData = null;
        flashToast(
          (verdict === "verify" ? "Kept " : verdict === "reject" ? "Marked for fix " : "Deferred ") +
          target.name
        );
        renderFilesTab();
      })
      .catch(function (err) {
        state.filesBusy = false;
        if (button) button.disabled = false;
        if (err && err.status === 409) {
          state.filesData = null;
          flashToast("The code changed — reloading the source-ordered review");
          renderFilesTab();
          return;
        }
        flashToast((err && err.message) || "Could not record declaration verdict");
      });
  }

  function readerRejectEditor(target) {
    var note = el("textarea", {
      class: "reject-note reader-reject-note",
      rows: "4",
      placeholder: "What is wrong, and what should Aristotle change?",
      "aria-label": "Rejection note for " + target.name,
    });
    note.value = target.note || "";
    var save = el("button", { class: "btn btn-danger", type: "button" }, ["Save FIX"]);
    var panel = el("div", {
      class: "reader-reject-editor",
      hidden: target.verdict === "rejected" ? null : "hidden",
    }, [
      el("div", { class: "reader-editor-help", text:
        "This note stays beside the source and is sent verbatim only when you later SHIP the issue." }),
      note,
      el("div", { class: "reader-editor-actions" }, [save]),
    ]);
    save.addEventListener("click", function () {
      if (!note.value.trim()) {
        note.focus();
        flashToast("A FIX needs a concrete note");
        return;
      }
      readerVerdict(target, "reject", note.value, save);
    });
    return { panel: panel, note: note };
  }

  function readerFullFile(path) {
    if (!path || path === "(source unavailable)") return null;
    var body = el("div", { class: "reader-full-body" });
    var details = el("details", { class: "reader-full-file" }, [
      el("summary", {}, [
        el("span", { text: "Read complete file" }),
        el("span", { class: "mono", text: path }),
      ]),
      body,
    ]);
    details.addEventListener("toggle", function () {
      if (!details.open || details.getAttribute("data-loaded") === "1") return;
      details.setAttribute("data-loaded", "1");
      body.appendChild(el("div", { class: "dash-loading", text: "loading complete source…" }));
      fetchReviewSource(path)
        .then(function (payload) {
          body.innerHTML = "";
          if (payload.truncated) {
            body.appendChild(el("div", { class: "reader-errors", text:
              "This unusually large file is shown up to " + payload.max_bytes + " bytes." }));
          }
          var code = el("code", { class: "lean" });
          leanHighlight(payload.source || "", []).forEach(function (node) { code.appendChild(node); });
          var rawSource = el("pre", { class: "source-block reader-full-source" }, [code]);
          var semanticFile = makeVersoShell(rawSource, "module", path);
          body.appendChild(semanticFile);
          enhanceVersoModule(semanticFile, path);
        })
        .catch(function (err) {
          body.innerHTML = "";
          body.appendChild(el("div", { class: "reader-errors", text:
            "Could not load the complete file: " + ((err && err.message) || err) }));
          details.removeAttribute("data-loaded");
        });
    });
    return details;
  }

  function renderReaderTarget(target) {
    var blocked = (target.blocked_by || []).length > 0;
    var unavailable = target.inspectable === false;
    var decisionBlocked = blocked || unavailable;
    var provisional = !!target.provisional;
    var article = el("article", {
      class: "reader-decl reader-status-" + (target.verdict || "unreviewed") +
        (blocked ? " reader-blocked" : "") +
        (unavailable ? " reader-unavailable" : "") +
        (provisional ? " reader-provisional" : ""),
      "data-decl-name": target.name,
    });
    var issueLink = target.permalink
      ? el("a", {
          class: "reader-issue-link",
          href: target.permalink,
          target: "_blank",
          rel: "noopener noreferrer",
        }, ["#" + target.issue])
      : el("span", { class: "reader-issue-link", text: "#" + target.issue });
    article.appendChild(el("div", { class: "reader-decl-head" }, [
      el("div", { class: "reader-decl-identity" }, [
        el("span", { class: "reader-decl-name mono", text: target.name }),
        target.kind ? el("span", { class: "decl-panel-kind", text: target.kind }) : null,
      ]),
      el("div", { class: "reader-badges" }, [
        statusBadge(target.verdict || "unreviewed"),
        provisional
          ? el("span", { class: "provisional-badge", text: "provisional" })
          : null,
        tierBadge(target.tier, target.qualifiers),
      ]),
    ]));
    if (unavailable) {
      article.appendChild(el("div", { class: "reader-wait" }, [
        el("strong", { text: "NOT READY FOR REVIEW — " }),
        target.inspection_blocked_reason ||
          "Current machine-audit evidence is not available for this declaration.",
      ]));
    }
    if (provisional) {
      article.appendChild(el("div", { class: "reader-provisional-note" }, [
        el("strong", { text: "PROVISIONAL REVIEW — " }),
        "Issue #" + target.issue + " is safe to inspect but cannot ship yet. " +
          (target.ship_blocked_reason
            ? target.ship_blocked_reason + ". "
            : "An upstream issue must resolve first. ") +
          "Your decision is saved against this exact source and elaborated " +
          "dependency context; Marathon reopens it automatically if either changes.",
      ]));
    }
    article.appendChild(el("div", { class: "reader-context" }, [
      issueLink,
      el("span", { text: target.issue_title || "" }),
      target.source_line != null
        ? el("span", { class: "reader-line mono", text: "line " + target.source_line })
        : null,
      target.surfaced ? el("span", { class: "surfaced-badge", text: "surfaced from code" }) : null,
    ]));
    if (blocked) {
      article.appendChild(el("div", { class: "reader-wait" }, [
        el("strong", { text: "WAIT — upstream FIX first. " }),
        "Decisions are paused because this declaration depends on " + target.blocked_by.join(", ") + ".",
      ]));
    }
    if (target.changed_since_review || target.dependency_invalidated) {
      article.appendChild(el("div", { class: "reader-wait" }, [
        el("strong", { text: "Re-review required. " }),
        target.dependency_invalidated
          ? "A rejected dependency invalidated the earlier KEEP."
          : "The declaration or its elaborated dependency context changed.",
      ]));
    }

    if (target.source_block) {
      var code = el("code", { class: "lean" });
      leanHighlight(target.source_block, []).forEach(function (node) { code.appendChild(node); });
      var rawSource = el("pre", { class: "source-block reader-source" }, [code]);
      var semanticSource = makeVersoShell(rawSource, "declaration", target.name || "");
      article.appendChild(semanticSource);
      enhanceVersoDeclaration(semanticSource, target.name || "");
    } else {
      article.appendChild(el("div", { class: "decl-nosource", text:
        "Source unavailable for this current review target." }));
    }
    if (target.statement_pp) {
      article.appendChild(el("details", { class: "reader-elab" }, [
        el("summary", { text: "Elaborated type" }),
        el("pre", { class: "statement-pp mono" },
          leanHighlight(prettyLeanType(target.statement_pp), [])),
      ]));
    }

    var editor = readerRejectEditor(target);
    var keep = el("button", {
      class: "btn reader-keep",
      type: "button",
      disabled: decisionBlocked ? "disabled" : null,
      title: blocked ? "Fix the rejected predecessor first" :
        (unavailable
          ? (target.inspection_blocked_reason ||
            "Current machine-audit evidence is required before review")
          :
        (provisional
          ? "Keep provisionally; shipping remains blocked until predecessors resolve"
          : "Keep this declaration (local only)")),
    }, [target.verdict === "verified"
      ? (provisional ? "✓ Kept provisionally" : "✓ Kept")
      : (provisional ? "Provisional keep" : "Keep")]);
    var reject = el("button", {
      class: "btn reader-fix",
      type: "button",
      disabled: decisionBlocked ? "disabled" : null,
      title: blocked ? "Fix the rejected predecessor first" :
        (unavailable
          ? (target.inspection_blocked_reason ||
            "Current machine-audit evidence is required before review")
          :
        (provisional
          ? "Record a provisional FIX note; shipping remains blocked"
          : "Write a FIX note beside the source")),
    }, [target.verdict === "rejected"
      ? (provisional ? "✗ Edit provisional FIX" : "✗ Edit FIX")
      : (provisional ? "Provisional fix…" : "Fix…")]);
    var defer = el("button", { class: "btn", type: "button" }, [
      target.verdict === "deferred" ? "↺ Deferred" : "Defer",
    ]);
    var focused = el("button", {
      class: "btn btn-tiny",
      type: "button",
      disabled: unavailable ? "disabled" : null,
      title: unavailable
        ? "Focused review opens after current machine-audit evidence is available"
        : "Open this issue in one-declaration-at-a-time review",
    }, ["Focused review"]);
    var reviewDetails = el("button", {
      class: "btn btn-tiny source-review-details",
      type: "button",
    }, ["Review details"]);
    keep.addEventListener("click", function () { readerVerdict(target, "verify", null, keep); });
    reject.addEventListener("click", function () {
      if (editor.panel.hasAttribute("hidden")) editor.panel.removeAttribute("hidden");
      else editor.panel.setAttribute("hidden", "hidden");
      if (!editor.panel.hasAttribute("hidden")) editor.note.focus();
    });
    defer.addEventListener("click", function () { readerVerdict(target, "defer", null, defer); });
    reviewDetails.addEventListener("click", function () {
      showDefinitionLookup(target.name, null);
    });
    focused.addEventListener("click", function () {
      state.filesResumeScroll = window.scrollY || window.pageYOffset || 0;
      state.filesResumeName = target.name;
      switchTab("review");
      openIssue(target.issue);
    });
    article.appendChild(el("div", { class: "reader-actions" }, [
      keep, reject, defer, reviewDetails, focused,
    ]));
    article.appendChild(editor.panel);
    return article;
  }

  function readerBatch(verdict, targets) {
    if (!targets.length || state.filesBusy) return;
    if (verdict !== "defer") return;
    var verb = "DEFER";
    if (!window.confirm(
      verb + " " + targets.length + " visible unreviewed declaration" +
      (targets.length === 1 ? "" : "s") + "?\n\n" +
      "This records local review state only. It does not SHIP any issue.\n" +
      "KEEP/FIX still require individual declaration decisions."
    )) return;
    state.filesBusy = true;
    var items = targets.map(function (target) {
      return {
        issue: target.issue,
        decl: target.name,
        verdict: verdict,
        decl_fingerprint: target.context_fingerprint,
      };
    });
    postBatchDeclVerdicts(items)
      .then(function (response) {
        state.filesBusy = false;
        state.graphData = null;
        flashToast("Recorded " + response.count + " local " + verb + " verdicts");
        renderFilesTab();
      })
      .catch(function (err) {
        state.filesBusy = false;
        if (err && err.status === 409) {
          state.filesData = null;
          flashToast("The code changed — reloading before applying the batch");
          renderFilesTab();
          return;
        }
        flashToast((err && err.message) || "Batch review failed");
      });
  }

  function paintFilesTab() {
    if (state.activeTab !== "files" || !state.filesData) return;
    var data = state.filesData;
    var host = dashHost("dash-files");
    if (!host) return;
    host.innerHTML = "";

    var chapter = data.chapter == null ? "all" : String(data.chapter);
    if (!state.filesChapter) state.filesChapter = chapter;
    var chapterSelect = el("select", { class: "reader-select", "aria-label": "Chapter" });
    chapterSelect.appendChild(el("option", { value: "all", text: "All chapters" }));
    (data.chapters || []).forEach(function (number) {
      chapterSelect.appendChild(el("option", { value: String(number), text: "Chapter " + number }));
    });
    chapterSelect.value = state.filesChapter === "" ? chapter : state.filesChapter;
    chapterSelect.addEventListener("change", function () {
      state.filesChapter = chapterSelect.value;
      state.filesData = null;
      renderFilesTab();
    });
    var search = el("input", {
      class: "reader-search",
      type: "search",
      placeholder: "Filter declaration, file, issue, or type…",
      value: state.filesQuery,
      "aria-label": "Filter chapter review",
    });
    search.value = state.filesQuery;
    // Typing repaints ONLY the results below the toolbar (debounced): the
    // input node is never remounted, so the caret stays where the reviewer
    // put it and mid-string edits work.
    var searchDebounce = null;
    search.addEventListener("input", function () {
      state.filesQuery = search.value;
      if (searchDebounce) clearTimeout(searchDebounce);
      searchDebounce = setTimeout(repaintResults, 150);
    });
    var targetFilter = el("select", {
      class: "reader-select", "aria-label": "Declaration status filter",
    }, [
      el("option", { value: "all", text: "All targets" }),
      el("option", { value: "provisional", text: "Provisional" }),
      el("option", { value: "unreviewed", text: "Unreviewed" }),
      el("option", { value: "rejected", text: "Rejected" }),
      el("option", { value: "changed", text: "Changed" }),
      el("option", { value: "surfaced", text: "Surfaced" }),
    ]);
    targetFilter.value = state.filesFilter;
    targetFilter.addEventListener("change", function () {
      state.filesFilter = targetFilter.value;
      repaintResults();
    });
    var refresh = el("button", { class: "btn", type: "button" }, ["Refresh"]);
    refresh.addEventListener("click", function () {
      state.filesData = null;
      renderFilesTab();
    });
    // The deferrable set tracks the CURRENT filter; repaintResults refreshes
    // the button in place so the toolbar (and the search input's caret)
    // survives every filter change.
    var deferrable = [];
    var deferAll = el("button", {
      class: "btn",
      type: "button",
      disabled: "disabled",
      title: "Records DEFER locally; never ships. KEEP/FIX remain per declaration.",
    }, ["Defer visible (0)"]);
    deferAll.addEventListener("click", function () {
      if (deferrable.length) readerBatch("defer", deferrable);
    });
    host.appendChild(el("div", { class: "reader-header" }, [
      el("div", {}, [
        el("h2", { text: data.chapter == null ? "Project file review" : "Chapter " + data.chapter + " file review" }),
        el("p", { class: "reader-help", text:
          "Read declarations in source order and keep notes beside the code. " +
          "Dependency-blocked declarations are clearly marked provisional: " +
          "their decisions are tied to the exact code and reopen after relevant changes; " +
          "they cannot ship until their issue is ready." }),
      ]),
      el("div", { class: "reader-toolbar" }, [
        chapterSelect, targetFilter, search, refresh, deferAll,
      ]),
    ]));

    var progress = data.progress || {};
    host.appendChild(el("div", { class: "stat-row reader-stats" }, [
      statChip(progress.reviewed || 0, "reviewed"),
      statChip(progress.total || 0, "targets"),
      statChip(progress.verified || 0, "kept"),
      statChip(progress.rejected || 0, "fix"),
      statChip((data.files || []).length, "files"),
    ]));

    var resultsHost = el("div", { class: "reader-results" });
    host.appendChild(resultsHost);

    function repaintResults() {
      if (state.activeTab !== "files" || !document.contains(resultsHost)) return;
      var visible = readerVisibleTargets(data);
      deferrable = visible.filter(function (target) { return !target.verdict; });
      deferAll.textContent = "Defer visible (" + deferrable.length + ")";
      if (deferrable.length) deferAll.removeAttribute("disabled");
      else deferAll.setAttribute("disabled", "disabled");
      deferAll.setAttribute("aria-label", "Defer " + deferrable.length +
        " visible unreviewed declarations — local only; does not ship");
      resultsHost.innerHTML = "";

      var files = (data.files || []).map(function (file) {
        return {
          path: file.path,
          progress: file.progress,
          issues: file.issues,
          declarations: (file.declarations || []).filter(function (target) {
            return readerTargetMatches(target, state.filesQuery, state.filesFilter);
          }),
        };
      }).filter(function (file) { return file.declarations.length; });

      if (!files.length) {
        resultsHost.appendChild(el("div", { class: "dash-empty", text:
          (state.filesQuery || state.filesFilter !== "all")
            ? "No declarations match this filter."
            : "No code-backed declarations are registered in this scope." }));
        return;
      }
      var nav = el("nav", { class: "reader-file-nav", "aria-label": "Source files" });
      files.forEach(function (file, index) {
        var button = el("button", { class: "reader-file-link", type: "button" }, [
          el("span", { class: "mono", text: file.path }),
          el("span", { text: file.declarations.length + " target" + (file.declarations.length === 1 ? "" : "s") }),
        ]);
        button.addEventListener("click", function () {
          var section = document.getElementById("reader-file-" + index);
          if (section) section.scrollIntoView({ behavior: "smooth", block: "start" });
        });
        nav.appendChild(button);
      });
      resultsHost.appendChild(nav);

      var body = el("div", { class: "reader-files" });
      files.forEach(function (file, index) {
        var section = el("section", { class: "reader-file", id: "reader-file-" + index });
        section.appendChild(el("div", { class: "reader-file-head" }, [
          el("h3", { class: "mono", text: file.path }),
          el("span", { text: file.declarations.length + " visible declaration" +
            (file.declarations.length === 1 ? "" : "s") }),
        ]));
        var fullFile = readerFullFile(file.path);
        if (fullFile) section.appendChild(fullFile);
        file.declarations.forEach(function (target) {
          section.appendChild(renderReaderTarget(target));
        });
        body.appendChild(section);
      });
      resultsHost.appendChild(body);

      if ((data.errors || []).length) {
        resultsHost.appendChild(el("div", { class: "reader-errors", text:
          data.errors.length + " issue payload" + (data.errors.length === 1 ? "" : "s") +
          " could not be assembled; the rest of the chapter is still shown." }));
      }
    }
    repaintResults();
    if (state.filesFocus) {
      var focusName = state.filesFocus;
      state.filesFocus = null;
      setTimeout(function () {
        var rows = document.querySelectorAll(".reader-decl[data-decl-name]");
        for (var i = 0; i < rows.length; i++) {
          if (rows[i].getAttribute("data-decl-name") === focusName) {
            rows[i].classList.add("reader-focused");
            rows[i].scrollIntoView({ behavior: "smooth", block: "center" });
            break;
          }
        }
      }, 0);
    }
    if (state.filesResumeScroll != null) {
      var resumeScroll = state.filesResumeScroll;
      var resumeName = state.filesResumeName;
      state.filesResumeScroll = null;
      state.filesResumeName = null;
      setTimeout(function () {
        if (resumeName) {
          var rows = document.querySelectorAll(".reader-decl[data-decl-name]");
          for (var i = 0; i < rows.length; i++) {
            if (rows[i].getAttribute("data-decl-name") === resumeName) {
              rows[i].classList.add("reader-focused");
              break;
            }
          }
        }
        window.scrollTo(0, resumeScroll);
      }, 0);
    }
  }

  function renderFilesTab() {
    if (state.activeTab !== "files") return;
    var host = dashHost("dash-files");
    if (!host) return;
    host.innerHTML = "";
    host.appendChild(el("div", { class: "dash-loading", text: "assembling source-ordered chapter review…" }));
    fetchReviewWorkspace(state.filesChapter)
      .then(function (data) {
        if (state.activeTab !== "files") return;
        state.filesData = data;
        if (!state.filesChapter) {
          state.filesChapter = data.chapter == null ? "all" : String(data.chapter);
        }
        if (state.filesFocus) state.filesQuery = state.filesFocus;
        paintFilesTab();
      })
      .catch(function (err) { dashError("dash-files", err); });
  }

  // --- Graph tab: project-wide issue/declaration dependency graph ---------

  var GRAPH_NODE_LIMIT = 500;

  function graphModeSpec(mode) {
    if (mode === "declarations") {
      return {
        mode: mode,
        title: "Declaration dependency graph",
        nodeType: "declaration",
        edgeKind: "declaration-dependency",
        help: "Direct declaration dependencies point from prerequisite to dependent.",
      };
    }
    if (mode === "ownership") {
      return {
        mode: mode,
        title: "Issues + declarations map",
        nodeType: null,
        edgeKind: "contains",
        help: "Dashed lines show which declarations each issue reviews. This is an ownership map, not a dependency graph.",
      };
    }
    return {
      mode: "issues",
      title: "Issue dependency graph",
      nodeType: "issue",
      edgeKind: "issue-dependency",
      help: "Issue prerequisites point toward the later issues that depend on them.",
    };
  }

  function graphNodeMatches(node, filter, ignoreQuery) {
    if (filter.chapter !== "all") {
      var chapters = node.chapters || (node.chapter != null ? [node.chapter] : []);
      if (chapters.map(String).indexOf(String(filter.chapter)) < 0) return false;
    }
    if (filter.status !== "all" && node.status !== filter.status) return false;
    if (ignoreQuery) return true;
    var query = String(filter.query || "").trim().toLowerCase();
    if (query) {
      var haystack = [node.label, node.title, node.source_file, node.issue,
        (node.issues || []).join(" ")].join(" ").toLowerCase();
      if (haystack.indexOf(query) < 0) return false;
    }
    return true;
  }

  function graphModeView(data, filter, mode) {
    var spec = graphModeSpec(mode);
    var candidates = (data.nodes || []).filter(function (node) {
      return (!spec.nodeType || node.type === spec.nodeType) &&
        graphNodeMatches(node, filter, true);
    });
    var candidateIds = Object.create(null);
    candidates.forEach(function (node) { candidateIds[node.id] = true; });
    var modeEdges = (data.edges || []).filter(function (edge) {
      return edge.kind === spec.edgeKind &&
        candidateIds[edge.source] && candidateIds[edge.target];
    });

    var query = String(filter.query || "").trim();
    if (!query) return { nodes: candidates, edges: modeEdges, spec: spec };

    // A search result without its immediate graph context is just a list.
    // Retain one dependency/ownership hop around every textual match while
    // still respecting the selected chapter and status filters.
    var matched = Object.create(null);
    var visible = Object.create(null);
    candidates.forEach(function (node) {
      if (graphNodeMatches(node, filter, false)) {
        matched[node.id] = true;
        visible[node.id] = true;
      }
    });
    modeEdges.forEach(function (edge) {
      if (matched[edge.source] || matched[edge.target]) {
        visible[edge.source] = true;
        visible[edge.target] = true;
      }
    });
    return {
      nodes: candidates.filter(function (node) { return visible[node.id]; }),
      edges: modeEdges.filter(function (edge) {
        return visible[edge.source] && visible[edge.target];
      }),
      spec: spec,
    };
  }

  function graphIsDependencyEdge(edge) {
    return edge.kind === "issue-dependency" ||
      edge.kind === "declaration-dependency";
  }

  function graphLayout(nodes, edges) {
    var byId = Object.create(null);
    var outgoing = Object.create(null);
    nodes.forEach(function (node) {
      byId[node.id] = node;
      outgoing[node.id] = [];
    });
    var seenEdges = Object.create(null);
    edges.forEach(function (edge) {
      // Ownership is useful context, but it must never manufacture a rank in
      // a dependency graph. The mixed ownership mode has its own two-column
      // layout below.
      if (!graphIsDependencyEdge(edge) ||
          !byId[edge.source] || !byId[edge.target]) return;
      var key = edge.source + "\u0000" + edge.target;
      if (seenEdges[key]) return;
      seenEdges[key] = true;
      outgoing[edge.source].push(edge.target);
    });
    Object.keys(outgoing).forEach(function (id) { outgoing[id].sort(); });

    // Tarjan SCCs turn a cyclic graph into an acyclic component graph. Rank
    // that condensation so a cycle occupies one layer while its downstream
    // dependents keep advancing through later layers. The old fallback put
    // every cyclic *and downstream* node into one final column.
    var nextIndex = 0;
    var indices = Object.create(null);
    var lowlink = Object.create(null);
    var onStack = Object.create(null);
    var stack = [];
    var components = [];
    function strongConnect(id) {
      indices[id] = nextIndex;
      lowlink[id] = nextIndex;
      nextIndex += 1;
      stack.push(id);
      onStack[id] = true;
      outgoing[id].forEach(function (target) {
        if (indices[target] == null) {
          strongConnect(target);
          lowlink[id] = Math.min(lowlink[id], lowlink[target]);
        } else if (onStack[target]) {
          lowlink[id] = Math.min(lowlink[id], indices[target]);
        }
      });
      if (lowlink[id] !== indices[id]) return;
      var component = [];
      var member;
      do {
        member = stack.pop();
        onStack[member] = false;
        component.push(member);
      } while (member !== id);
      component.sort();
      components.push(component);
    }
    Object.keys(byId).sort().forEach(function (id) {
      if (indices[id] == null) strongConnect(id);
    });

    var componentBy = Object.create(null);
    var componentKey = [];
    components.forEach(function (component, componentId) {
      componentKey[componentId] = component.map(function (id) {
        return String(byId[id].label || id);
      }).sort()[0];
      component.forEach(function (id) { componentBy[id] = componentId; });
    });
    var componentOutgoing = components.map(function () { return []; });
    var componentIndegree = components.map(function () { return 0; });
    var componentRank = components.map(function () { return 0; });
    var componentEdges = Object.create(null);
    Object.keys(outgoing).forEach(function (source) {
      outgoing[source].forEach(function (target) {
        var from = componentBy[source];
        var to = componentBy[target];
        var key = from + ":" + to;
        if (from === to || componentEdges[key]) return;
        componentEdges[key] = true;
        componentOutgoing[from].push(to);
        componentIndegree[to] += 1;
      });
    });
    componentOutgoing.forEach(function (targets) {
      targets.sort(function (a, b) {
        return componentKey[a].localeCompare(componentKey[b]);
      });
    });
    var ready = components.map(function (_component, id) { return id; })
      .filter(function (id) { return componentIndegree[id] === 0; });
    ready.sort(function (a, b) {
      return componentKey[a].localeCompare(componentKey[b]);
    });
    while (ready.length) {
      var componentId = ready.shift();
      componentOutgoing[componentId].forEach(function (target) {
        componentRank[target] = Math.max(
          componentRank[target], componentRank[componentId] + 1
        );
        componentIndegree[target] -= 1;
        if (componentIndegree[target] === 0) ready.push(target);
      });
      ready.sort(function (a, b) {
        return componentKey[a].localeCompare(componentKey[b]);
      });
    }

    var layers = {};
    nodes.forEach(function (node) {
      var r = componentRank[componentBy[node.id]] || 0;
      if (!layers[r]) layers[r] = [];
      layers[r].push(node);
    });
    Object.keys(layers).forEach(function (key) {
      layers[key].sort(function (a, b) {
        var acomp = componentKey[componentBy[a.id]];
        var bcomp = componentKey[componentBy[b.id]];
        var ach = (a.chapters || [a.chapter == null ? 9999 : a.chapter])[0];
        var bch = (b.chapters || [b.chapter == null ? 9999 : b.chapter])[0];
        return acomp.localeCompare(bcomp) || ach - bch ||
          a.type.localeCompare(b.type) || a.label.localeCompare(b.label);
      });
    });
    var positions = {};
    var widest = 1;
    var layerNums = Object.keys(layers).map(Number).sort(function (a, b) { return a - b; });
    layerNums.forEach(function (r) {
      widest = Math.max(widest, layers[r].length);
      layers[r].forEach(function (node, index) {
        positions[node.id] = { x: 30 + r * 235, y: 30 + index * 76, rank: r };
      });
    });
    var largestRank = layerNums.length ? layerNums[layerNums.length - 1] : 0;
    return {
      positions: positions,
      width: Math.max(520, 60 + (largestRank + 1) * 235),
      height: Math.max(220, 60 + widest * 76),
      components: componentBy,
    };
  }

  function graphOwnershipLayout(nodes) {
    var columns = { issue: [], declaration: [] };
    nodes.forEach(function (node) {
      (columns[node.type] || columns.declaration).push(node);
    });
    function order(a, b) {
      var ach = (a.chapters || [a.chapter == null ? 9999 : a.chapter])[0];
      var bch = (b.chapters || [b.chapter == null ? 9999 : b.chapter])[0];
      return ach - bch || a.label.localeCompare(b.label);
    }
    columns.issue.sort(order);
    columns.declaration.sort(order);
    var positions = {};
    columns.issue.forEach(function (node, index) {
      positions[node.id] = { x: 30, y: 30 + index * 76, rank: 0 };
    });
    columns.declaration.forEach(function (node, index) {
      positions[node.id] = { x: 265, y: 30 + index * 76, rank: 1 };
    });
    return {
      positions: positions,
      width: 520,
      height: Math.max(220, 60 + Math.max(
        columns.issue.length, columns.declaration.length, 1
      ) * 76),
    };
  }

  function graphConnectedSubset(nodes, edges, limit) {
    if (nodes.length <= limit) {
      return { nodes: nodes, edges: edges, capped: false, total: nodes.length };
    }
    var byId = Object.create(null);
    var adjacent = Object.create(null);
    nodes.forEach(function (node) {
      byId[node.id] = node;
      adjacent[node.id] = [];
    });
    edges.forEach(function (edge) {
      if (!byId[edge.source] || !byId[edge.target]) return;
      adjacent[edge.source].push(edge.target);
      adjacent[edge.target].push(edge.source);
    });
    Object.keys(adjacent).forEach(function (id) {
      adjacent[id] = Array.from(new Set(adjacent[id])).sort();
    });
    var seeds = nodes.slice().sort(function (a, b) {
      function priority(node) {
        if (node.changed || node.impact === "changed") return 0;
        if (node.impact === "downstream") return 1;
        if (node.status === "rejected") return 2;
        return 3;
      }
      return priority(a) - priority(b) ||
        adjacent[b.id].length - adjacent[a.id].length ||
        a.label.localeCompare(b.label);
    });
    var selected = Object.create(null);
    var count = 0;
    seeds.forEach(function (seed) {
      if (count >= limit || selected[seed.id]) return;
      var queued = Object.create(null);
      var queue = [seed.id];
      queued[seed.id] = true;
      while (queue.length && count < limit) {
        var id = queue.shift();
        if (selected[id]) continue;
        selected[id] = true;
        count += 1;
        adjacent[id].forEach(function (neighbor) {
          if (!selected[neighbor] && !queued[neighbor]) {
            queued[neighbor] = true;
            queue.push(neighbor);
          }
        });
      }
    });
    var keptNodes = nodes.filter(function (node) { return selected[node.id]; });
    var keptEdges = edges.filter(function (edge) {
      return selected[edge.source] && selected[edge.target];
    });
    return {
      nodes: keptNodes,
      edges: keptEdges,
      capped: true,
      total: nodes.length,
    };
  }

  function graphShortRevision(revision) {
    var text = String(revision || "").trim();
    return text ? text.slice(0, 12) : null;
  }

  function graphTopologyMeta(data, mode, dependencyEdgeCount) {
    if (mode === "ownership") {
      return { status: "ownership", revision: null, currentRevision: null };
    }
    var topology = data.topology || {};
    var rawStatus = topology.status || topology.freshness ||
      data.topology_status || "";
    var hasExplicitStatus = Boolean(rawStatus);
    var status = String(rawStatus).toLowerCase();
    var explicitlyAvailable = topology.available === true ||
      (hasExplicitStatus && topology.available !== false &&
       status !== "missing" && status !== "none" && status !== "unavailable");
    if (topology.available === false || status === "missing" ||
        status === "none" || status === "unavailable") {
      status = "unavailable";
    } else if (status === "current" || status === "ok") {
      status = "fresh";
    } else if (status && status !== "fresh") {
      status = "stale";
    }
    var revision = graphShortRevision(
      topology.revision || topology.source_revision ||
      topology.topology_revision || data.topology_revision ||
      (data.audit && data.audit.source_revision)
    );
    var currentRevision = graphShortRevision(
      topology.current_revision || data.current_revision
    );
    if (status === "fresh" && revision && currentRevision &&
        revision !== currentRevision) status = "stale";

    // Backward-compatible inference while older servers do not expose the
    // topology object. Never silently turn a stale, edge-less ownership
    // projection into something labelled a dependency graph.
    if (!status) {
      var auditStatus = String((data.audit && data.audit.status) || "missing");
      if (dependencyEdgeCount === 0) {
        status = "unavailable";
      } else {
        status = auditStatus === "fresh" ? "fresh" :
          (auditStatus === "missing" ? "unavailable" : "stale");
      }
    } else if (dependencyEdgeCount === 0 && status === "stale" &&
               !explicitlyAvailable) {
      status = "unavailable";
    }
    return {
      status: status,
      revision: revision,
      currentRevision: currentRevision,
    };
  }

  function graphTopologyNotice(meta, spec) {
    if (meta.status === "ownership") {
      return el("div", { class: "graph-topology-note graph-topology-ownership", text:
        "Ownership map: dashed lines connect issues to the declarations they review. " +
        "These lines are not dependency edges." });
    }
    if (meta.status === "fresh") {
      return el("div", { class: "graph-topology-note graph-topology-fresh", text:
        "Dependency topology is fresh" +
        (meta.revision ? " at " + meta.revision : "") + "." });
    }
    if (meta.status === "stale") {
      return el("div", { class: "graph-topology-note graph-topology-stale", text:
        "Showing last-known " + spec.title.toLowerCase() +
        (meta.revision ? " from " + meta.revision : "") +
        (meta.currentRevision ? "; current source is " + meta.currentRevision : "") +
        ". Dashed dependency edges may be outdated." });
    }
    return el("div", { class: "graph-topology-note graph-topology-unavailable", text:
      "Dependency topology is unavailable. The project audit has not captured " +
      "dependency edges for this view." });
  }

  function graphSvg(tag, attrs, textValue) {
    var node = document.createElementNS("http://www.w3.org/2000/svg", tag);
    Object.keys(attrs || {}).forEach(function (key) { node.setAttribute(key, attrs[key]); });
    if (textValue != null) node.textContent = textValue;
    return node;
  }

  function graphShortLabel(node) {
    if (node.type === "issue") return node.label;
    var bits = String(node.label || "").split(".");
    var label = bits.length > 1 ? bits.slice(-2).join(".") : bits[0];
    return label.length > 27 ? "…" + label.slice(-26) : label;
  }

  function graphApplyZoom(resetPan) {
    var host = dashHost("dash-graph");
    if (!host) return;
    var svg = host.querySelector(".project-graph");
    var viewport = host.querySelector(".graph-viewport");
    if (!svg || !viewport) return;
    var zoom = Math.max(0.5, Math.min(2.25, state.graphView.zoom || 1));
    state.graphView.zoom = zoom;
    svg.setAttribute("width", Math.round(Number(svg.getAttribute("data-base-width")) * zoom));
    svg.setAttribute("height", Math.round(Number(svg.getAttribute("data-base-height")) * zoom));
    var label = host.querySelector(".graph-zoom-value");
    if (label) label.textContent = Math.round(zoom * 100) + "%";
    if (resetPan) {
      viewport.scrollLeft = 0;
      viewport.scrollTop = 0;
    }
  }

  function graphChangeZoom(delta) {
    state.graphView.zoom = Math.max(
      0.5, Math.min(2.25, (state.graphView.zoom || 1) + delta)
    );
    graphApplyZoom(false);
  }

  function showGraphNode(node, data) {
    var byId = {};
    (data.nodes || []).forEach(function (candidate) { byId[candidate.id] = candidate; });
    var incoming = [];
    var outgoing = [];
    (data.edges || []).forEach(function (edge) {
      if (edge.target === node.id && byId[edge.source]) {
        incoming.push({ node: byId[edge.source], kind: edge.kind });
      }
      if (edge.source === node.id && byId[edge.target]) {
        outgoing.push({ node: byId[edge.target], kind: edge.kind });
      }
    });
    var unresolved = (data.unresolved_edges || []).filter(function (edge) {
      return edge.target === node.id;
    });
    var cyclic = (data.cyclic_edges || []).some(function (edge) {
      return edge.source === node.id || edge.target === node.id;
    });
    function relationList(title, rows) {
      var section = el("section", { class: "graph-inspector-section" }, [
        el("h3", { text: title }),
      ]);
      if (!rows.length) {
        section.appendChild(el("div", { class: "dash-empty", text: "none" }));
        return section;
      }
      var list = el("ul", { class: "graph-relations" });
      rows.slice(0, 40).forEach(function (row) {
        var button = el("button", { class: "graph-relation", type: "button" }, [
          el("span", { class: "mono", text: row.node.label }),
          el("span", { text: row.kind }),
        ]);
        button.addEventListener("click", function () { showGraphNode(row.node, data); });
        list.appendChild(el("li", {}, [button]));
      });
      section.appendChild(list);
      return section;
    }

    var panel = el("div", { class: "modal-panel graph-inspector" }, [
      el("h2", { class: "modal-title mono", text: node.label }),
      node.title ? el("p", { class: "modal-sub graph-node-title", text: node.title }) : null,
      el("div", { class: "graph-node-meta" }, [
        statusBadge(node.status),
        node.build_status ? el("span", {
          class: "graph-build-status graph-build-" + node.build_status,
          text: "build " + node.build_status,
        }) : null,
        node.tier ? tierBadge(node.tier) : null,
        node.kind ? el("span", { class: "decl-panel-kind", text: node.kind }) : null,
        (node.chapters || []).length
          ? el("span", { class: "chapter-pill", text: "ch " + node.chapters.join(", ") })
          : null,
      ]),
      node.source_file ? el("p", { class: "resource-file" }, [
        el("strong", { text: "Source" }),
        el("span", { class: "mono", text: node.source_file +
          (node.source_line != null ? ":" + node.source_line : "") }),
      ]) : null,
      (node.blocked_by || []).length ? el("p", { class: "reader-wait", text:
        "Waiting on rejected predecessor(s): " + node.blocked_by.join(", ") }) : null,
      node.impact ? el("p", {
        class: "graph-impact-note graph-impact-" + node.impact,
        text: node.impact === "changed"
          ? "This declaration changed and needs fresh review."
          : "Downstream impact: this node depends on a changed declaration.",
      }) : null,
      cyclic ? el("p", { class: "reader-wait", text:
        "A cyclic dependency edge touches this node; inspect both directions before relying on the ordering." }) : null,
      relationList("Predecessors / owners", incoming),
      relationList("Dependents / declarations", outgoing),
    ]);
    if (unresolved.length) {
      var unresolvedSection = el("section", { class: "graph-inspector-section" }, [
        el("h3", { text: "Unresolved dependencies" }),
      ]);
      var unresolvedList = el("ul", { class: "graph-relations" });
      unresolved.forEach(function (edge) {
        unresolvedList.appendChild(el("li", { class: "mono", text: edge.dependency }));
      });
      unresolvedSection.appendChild(unresolvedList);
      panel.appendChild(unresolvedSection);
    }
    var actions = el("div", { class: "modal-actions graph-inspector-actions" });
    if (node.type === "declaration") {
      var openFile = el("button", { class: "btn btn-primary", type: "button" }, ["Open in Files"]);
      openFile.addEventListener("click", function () {
        closeModal();
        state.filesChapter = (node.chapters || []).length === 1
          ? String(node.chapters[0]) : "all";
        state.filesFocus = node.label;
        state.filesQuery = node.label;
        state.filesFilter = "all";
        state.filesResumeScroll = null;
        state.filesResumeName = null;
        state.filesData = null;
        switchTab("files");
      });
      actions.appendChild(openFile);
      if ((node.issues || []).length) {
        var openDeclIssue = el("button", { class: "btn", type: "button" }, [
          "Focused review #" + node.issues[0],
        ]);
        openDeclIssue.addEventListener("click", function () {
          closeModal(); switchTab("review"); openIssue(node.issues[0]);
        });
        actions.appendChild(openDeclIssue);
      }
    } else if (node.issue != null) {
      var openReview = el("button", { class: "btn btn-primary", type: "button" }, ["Open focused review"]);
      openReview.addEventListener("click", function () {
        closeModal(); switchTab("review"); openIssue(node.issue);
      });
      actions.appendChild(openReview);
    }
    if (node.permalink) {
      actions.appendChild(el("a", {
        class: "btn graph-gh-link",
        href: node.permalink,
        target: "_blank",
        rel: "noopener noreferrer",
      }, ["GitHub issue"]));
    }
    var close = el("button", { class: "btn", type: "button" }, ["Close"]);
    close.addEventListener("click", closeModal);
    actions.appendChild(close);
    panel.appendChild(actions);
    openModal(panel);
  }

  function renderGraphCanvas(host, data, nodes, edges, mode, topology) {
    var subset = graphConnectedSubset(nodes, edges, GRAPH_NODE_LIMIT);
    nodes = subset.nodes;
    edges = subset.edges;
    if (!nodes.length) {
      host.appendChild(el("div", { class: "dash-empty", text: "No graph nodes match these filters." }));
      return;
    }
    if (subset.capped) {
      host.appendChild(el("div", { class: "graph-cap", text:
        "Showing a connected " + GRAPH_NODE_LIMIT + "-node neighborhood out of " +
        subset.total + " matching nodes. Narrow by chapter, status, or search to inspect the rest." }));
    }
    var layout = mode === "ownership"
      ? graphOwnershipLayout(nodes)
      : graphLayout(nodes, edges);
    var viewport = el("div", { class: "graph-viewport" });
    var zoom = Math.max(0.5, Math.min(2.25, state.graphView.zoom || 1));
    var svg = graphSvg("svg", {
      class: "project-graph",
      width: Math.round(layout.width * zoom),
      height: Math.round(layout.height * zoom),
      "data-base-width": layout.width,
      "data-base-height": layout.height,
      viewBox: "0 0 " + layout.width + " " + layout.height,
      role: "img",
      "aria-label": graphModeSpec(mode).title,
    });
    var defs = graphSvg("defs");
    var marker = graphSvg("marker", {
      id: "graph-arrow", markerWidth: "7", markerHeight: "7",
      refX: "6", refY: "3.5", orient: "auto", markerUnits: "strokeWidth",
    });
    marker.appendChild(graphSvg("path", { d: "M0,0 L7,3.5 L0,7 z", class: "graph-arrow" }));
    defs.appendChild(marker);
    svg.appendChild(defs);
    edges.forEach(function (edge) {
      var from = layout.positions[edge.source];
      var to = layout.positions[edge.target];
      if (!from || !to) return;
      var x1 = from.x + 190, y1 = from.y + 25;
      var x2 = to.x, y2 = to.y + 25;
      var bend = Math.max(30, Math.abs(x2 - x1) / 2);
      svg.appendChild(graphSvg("path", {
        class: "graph-edge graph-edge-" + edge.kind +
          (edge.cyclic ? " graph-edge-cyclic" : "") +
          (topology.status === "stale" && graphIsDependencyEdge(edge)
            ? " graph-edge-topology-stale" : ""),
        d: "M" + x1 + "," + y1 + " C" + (x1 + bend) + "," + y1 +
          " " + (x2 - bend) + "," + y2 + " " + x2 + "," + y2,
        "marker-end": "url(#graph-arrow)",
      }));
    });
    nodes.forEach(function (node) {
      var pos = layout.positions[node.id];
      if (!pos) return;
      var group = graphSvg("g", {
        class: "graph-node graph-node-" + node.type + " graph-status-" +
          (node.status || "pending") +
          (node.impact ? " graph-impact-" + node.impact : ""),
        transform: "translate(" + pos.x + " " + pos.y + ")",
        tabindex: "0", role: "button",
        "aria-label": node.type + " " + node.label + ", " + node.status,
      });
      group.appendChild(graphSvg("rect", { width: "190", height: "50", rx: "9", ry: "9" }));
      var title = graphSvg("title", {}, node.label + " — " + (node.title || ""));
      group.appendChild(title);
      group.appendChild(graphSvg("text", { x: "10", y: "20", class: "graph-node-label" }, graphShortLabel(node)));
      var sub = node.type === "issue"
        ? ("ch " + (node.chapter == null ? "—" : node.chapter) + " · " + node.status)
        : ((node.source_file ? node.source_file.split("/").slice(-1)[0] : "source ?") + " · " + node.status);
      group.appendChild(graphSvg("text", { x: "10", y: "38", class: "graph-node-sub" }, sub));
      group.addEventListener("click", function () { showGraphNode(node, data); });
      group.addEventListener("keydown", function (event) {
        if (event.key === "Enter" || event.key === " ") {
          event.preventDefault(); showGraphNode(node, data);
        }
      });
      svg.appendChild(group);
    });
    viewport.appendChild(svg);
    var drag = null;
    viewport.addEventListener("pointerdown", function (event) {
      if (event.button !== 0 || event.target.closest(".graph-node")) return;
      drag = {
        pointer: event.pointerId,
        x: event.clientX,
        y: event.clientY,
        left: viewport.scrollLeft,
        top: viewport.scrollTop,
      };
      viewport.classList.add("graph-dragging");
      viewport.setPointerCapture(event.pointerId);
      event.preventDefault();
    });
    viewport.addEventListener("pointermove", function (event) {
      if (!drag || event.pointerId !== drag.pointer) return;
      viewport.scrollLeft = drag.left - (event.clientX - drag.x);
      viewport.scrollTop = drag.top - (event.clientY - drag.y);
    });
    function stopGraphDrag(event) {
      if (!drag || event.pointerId !== drag.pointer) return;
      drag = null;
      viewport.classList.remove("graph-dragging");
    }
    viewport.addEventListener("pointerup", stopGraphDrag);
    viewport.addEventListener("pointercancel", stopGraphDrag);
    host.appendChild(viewport);
  }

  function paintGraphTab() {
    if (state.activeTab !== "graph" || !state.graphData) return;
    var data = state.graphData;
    var host = dashHost("dash-graph");
    if (!host) return;
    host.innerHTML = "";
    var filter = state.graphFilter;
    var mode = state.graphView.mode || "issues";
    var spec = graphModeSpec(mode);

    var chapter = el("select", { class: "reader-select", "aria-label": "Graph chapter" });
    chapter.appendChild(el("option", { value: "all", text: "All chapters" }));
    (data.chapters || []).forEach(function (number) {
      chapter.appendChild(el("option", { value: String(number), text: "Chapter " + number }));
    });
    chapter.value = filter.chapter;
    var status = el("select", { class: "reader-select", "aria-label": "Graph status" });
    status.appendChild(el("option", { value: "all", text: "All statuses" }));
    var statuses = {};
    (data.nodes || []).forEach(function (node) {
      if (!spec.nodeType || node.type === spec.nodeType) statuses[node.status] = true;
    });
    Object.keys(statuses).sort().forEach(function (name) {
      status.appendChild(el("option", { value: name, text: name }));
    });
    status.value = filter.status;
    var modeSelect = el("select", {
      class: "reader-select graph-mode", "aria-label": "Graph mode",
    }, [
      el("option", { value: "issues", text: "Issue graph" }),
      el("option", { value: "declarations", text: "Declaration graph" }),
      el("option", { value: "ownership", text: "Issues + declarations map" }),
    ]);
    modeSelect.value = mode;
    var search = el("input", {
      class: "reader-search", type: "search", value: filter.query,
      placeholder: "Find issue, declaration, or file…",
      "aria-label": "Filter project graph",
    });
    search.value = filter.query;
    var refresh = el("button", { class: "btn", type: "button" }, ["Refresh"]);
    var zoomOut = el("button", {
      class: "btn btn-tiny", type: "button", "aria-label": "Zoom graph out",
    }, ["−"]);
    var zoomValue = el("span", {
      class: "graph-zoom-value", text: Math.round((state.graphView.zoom || 1) * 100) + "%",
    });
    var resetView = el("button", {
      class: "btn btn-tiny", type: "button", "aria-label": "Reset graph view",
    }, ["Reset view"]);
    var zoomIn = el("button", {
      class: "btn btn-tiny", type: "button", "aria-label": "Zoom graph in",
    }, ["+"]);
    chapter.addEventListener("change", function () { filter.chapter = chapter.value; paintGraphTab(); });
    status.addEventListener("change", function () { filter.status = status.value; paintGraphTab(); });
    modeSelect.addEventListener("change", function () {
      state.graphView.mode = modeSelect.value;
      state.graphView.zoom = 1;
      filter.status = "all";
      paintGraphTab();
    });
    // The graph SVG genuinely depends on the query, so a repaint is needed —
    // but debounced, and the caret returns to its ACTUAL position instead of
    // being shoved to the end, so mid-string edits survive.
    var graphSearchDebounce = null;
    search.addEventListener("input", function () {
      filter.query = search.value;
      var caretStart = search.selectionStart;
      var caretEnd = search.selectionEnd;
      if (graphSearchDebounce) clearTimeout(graphSearchDebounce);
      graphSearchDebounce = setTimeout(function () {
        paintGraphTab();
        var replacement = dashHost("dash-graph").querySelector(".reader-search");
        if (replacement) {
          replacement.focus();
          try {
            replacement.setSelectionRange(caretStart, caretEnd);
          } catch (e) { /* selection restore is best-effort */ }
        }
      }, 200);
    });
    refresh.addEventListener("click", function () {
      state.graphData = null;
      renderGraphTab(true);
    });
    zoomOut.addEventListener("click", function () { graphChangeZoom(-0.2); });
    zoomIn.addEventListener("click", function () { graphChangeZoom(0.2); });
    resetView.addEventListener("click", function () {
      state.graphView.zoom = 1;
      graphApplyZoom(true);
    });
    host.appendChild(el("div", { class: "reader-header graph-header" }, [
      el("div", {}, [
        el("h2", { text: spec.title }),
        el("p", { class: "reader-help", text: spec.help +
          " Drag or scroll to pan; click a node to expand relationships." }),
      ]),
      el("div", { class: "reader-toolbar graph-toolbar" }, [
        modeSelect, chapter, status, search,
        zoomOut, zoomValue, resetView, zoomIn, refresh,
      ]),
    ]));

    var view = graphModeView(data, filter, mode);
    var nodes = view.nodes;
    var edges = view.edges;
    var topology = graphTopologyMeta(data, mode, edges.length);
    host.appendChild(graphTopologyNotice(topology, spec));
    var cyclicVisible = edges.filter(function (edge) { return edge.cyclic; }).length;
    var visibleNodeIds = Object.create(null);
    nodes.forEach(function (node) { visibleNodeIds[node.id] = true; });
    var unresolvedVisible = mode === "declarations"
      ? (data.unresolved_edges || []).filter(function (edge) {
          return visibleNodeIds[edge.target];
        }).length
      : 0;
    if (cyclicVisible && mode !== "ownership") {
      host.appendChild(el("div", {
        class: "graph-topology-note graph-cycle-note",
        text: cyclicVisible + " visible cyclic dependency edge" +
          (cyclicVisible === 1 ? "" : "s") +
          ". Strongly connected nodes share one layout layer; downstream " +
          "dependencies continue in later layers.",
      }));
    }

    if (data.audit && data.audit.status !== "fresh") {
      host.appendChild(el("div", { class: "reader-errors graph-audit-warning", text:
        data.audit.status === "missing"
          ? "No audit snapshot is available; build/tier evidence is shown as unknown."
          : "Audit evidence is " + data.audit.status +
            "; stale or unavailable build evidence is never presented as current." }));
    }

    host.appendChild(el("div", { class: "stat-row graph-stats" }, [
      statChip(nodes.length, "visible nodes"),
      statChip(edges.length, "visible edges"),
      statChip((data.counts && data.counts.by_type && data.counts.by_type.issue) || 0, "issues total"),
      statChip((data.counts && data.counts.by_type && data.counts.by_type.declaration) || 0, "declarations total"),
      statChip((data.counts && data.counts.impacted) || 0, "downstream impact"),
    ]));
    var legend = [];
    if (mode === "issues" || mode === "ownership") {
      legend.push(el("span", { class: "graph-key graph-key-issue", text: "issue" }));
    }
    if (mode === "declarations" || mode === "ownership") {
      legend.push(el("span", { class: "graph-key graph-key-decl", text: "declaration" }));
    }
    legend.push(mode === "ownership"
      ? el("span", { class: "graph-key graph-key-contains", text: "reviews / contains" })
      : el("span", { class: "graph-key graph-key-dependency", text:
          topology.status === "stale" ? "dependency (last known)" : "dependency" }));
    if (mode !== "issues") {
      legend.push(el("span", { class: "graph-key graph-key-changed", text: "changed" }));
      legend.push(el("span", { class: "graph-key graph-key-impact", text: "changed downstream" }));
    }
    legend.push(el("span", { class: "graph-key graph-key-warning", text:
      unresolvedVisible + " unresolved · " + cyclicVisible + " cyclic" }));
    host.appendChild(el("div", { class: "graph-legend" }, legend));
    if (topology.status === "unavailable" && mode !== "ownership") {
      host.appendChild(el("div", { class: "dash-empty graph-topology-empty", text:
        "No dependency edges are available for this graph." }));
    } else {
      renderGraphCanvas(host, data, nodes, edges, mode, topology);
    }
    if ((data.errors || []).length) {
      host.appendChild(el("div", { class: "reader-errors", text:
        data.errors.length + " issue payload" + (data.errors.length === 1 ? "" : "s") +
        " could not be included; the remaining project graph is still navigable." }));
    }
  }

  function renderGraphTab(force) {
    if (state.activeTab !== "graph") return;
    if (state.graphData) return paintGraphTab();
    var host = dashHost("dash-graph");
    if (!host) return;
    host.innerHTML = "";
    host.appendChild(el("div", { class: "dash-loading", text: "building project dependency graph…" }));
    fetchProjectGraph(force)
      .then(function (data) {
        if (state.activeTab !== "graph") return;
        state.graphData = data;
        paintGraphTab();
      })
      .catch(function (err) { dashError("dash-graph", err); });
  }

  // --- Add Chapter tab (F1): headless chapter-intake → review → approve -----
  // POST /api/add-chapter starts a headless intake (Claude drafts one review
  // sub-issue per chapter result); we poll /api/chapter-intake-status until it
  // is ready, render the drafts for human review, then POST /api/approve-chapter
  // to open the GitHub issues + register the chapter. Build the form ONCE so a
  // tab switch away+back keeps any in-flight state.
  var acPoll = null;
  function acEsc(s) {
    var d = document.createElement("div");
    d.textContent = s == null ? "" : String(s);
    return d.innerHTML;
  }
  function acEl(id) { return document.getElementById(id); }

  function renderAddChapterTab() {
    var root = acEl("dash-add-chapter");
    if (!root || root.getAttribute("data-init") === "1") return;
    root.setAttribute("data-init", "1");
    root.innerHTML =
      '<div class="addchap">' +
      '<h2>Add a chapter</h2>' +
      '<p class="addchap-help">Run a headless review of a <em>formalized</em> chapter: ' +
      'Claude reads the whole chapter and drafts one review sub-issue per first-class ' +
      'result. You review the drafts below, then approve to open the GitHub issues and ' +
      'register the chapter.</p>' +
      '<div class="addchap-form">' +
      '<label>Chapter <input id="ac-chapter" type="number" min="1" step="1" placeholder="15"></label>' +
      '<fieldset class="ac-source"><legend>Informal statements</legend>' +
      '<label><input type="radio" name="ac-source" value="tracker" checked> ' +
      'Tracker roadmap <span class="muted">— quote this chapter\'s curated page/name ' +
      'lines from the project tracker issue (recommended)</span></label>' +
      '<label><input type="radio" name="ac-source" value="file"> ' +
      'File <span class="muted">— a repo-relative markdown file of statements</span></label>' +
      '<label><input type="radio" name="ac-source" value="none"> ' +
      'None <span class="muted">— let Claude draft them; you verify each on review</span></label>' +
      '</fieldset>' +
      '<label id="ac-file-row" hidden>Informal-statements file <span class="muted">(repo-relative)</span>' +
      '<input id="ac-informal" type="text" placeholder=".marathon/review/informal/Chapter15.md"></label>' +
      '<button id="ac-start" class="ac-btn">Start intake</button>' +
      '</div>' +
      '<div id="ac-status" class="addchap-status" aria-live="polite"></div>' +
      '<div id="ac-drafts" class="addchap-drafts"></div>' +
      '</div>';
    root.querySelectorAll('input[name="ac-source"]').forEach(function (r) {
      r.addEventListener("change", function () {
        var fileRow = acEl("ac-file-row");
        if (!fileRow) return;
        if (acSource() === "file") fileRow.removeAttribute("hidden");
        else fileRow.setAttribute("hidden", "");
      });
    });
    acEl("ac-start").addEventListener("click", acStart);
  }

  // Which informal source the radio group has selected (defaults to tracker).
  function acSource() {
    var checked = document.querySelector('input[name="ac-source"]:checked');
    return checked ? checked.value : "tracker";
  }

  function acStart() {
    var chapter = parseInt((acEl("ac-chapter") || {}).value, 10);
    if (!chapter || chapter < 1) {
      acEl("ac-status").innerHTML = '<span class="err">Enter a chapter number first.</span>';
      return;
    }
    var source = acSource();
    var informal = ((acEl("ac-informal") || {}).value || "").trim();
    if (source === "file" && !informal) {
      acEl("ac-status").innerHTML =
        '<span class="err">Enter the informal-statements file path, or pick a different source.</span>';
      return;
    }
    acEl("ac-drafts").innerHTML = "";
    acEl("ac-status").innerHTML = "Starting intake for Chapter " + chapter + "…";
    postJSON("/api/add-chapter",
             { chapter: chapter, informal_source: source, informal_statements: informal })
      .then(function () { acPollOnce(chapter); })
      .catch(function (e) {
        acEl("ac-status").innerHTML = '<span class="err">' + acEsc(e.message) + "</span>";
      });
  }

  function acPollOnce(chapter) {
    if (acPoll) { clearTimeout(acPoll); acPoll = null; }
    postJSON("/api/chapter-intake-status", { chapter: chapter })
      .then(function (s) {
        var st = acEl("ac-status");
        if (!st) return; // tab torn down
        if (s.status === "running") {
          st.innerHTML = "⏳ Reviewing Chapter " + chapter +
            " — headless Claude is reading the whole chapter. This takes a few minutes.";
          acPoll = setTimeout(function () { acPollOnce(chapter); }, 4000);
        } else if (s.status === "ready") {
          st.innerHTML = "✅ Drafted " + (s.subissues || []).length +
            " sub-issues for Chapter " + chapter + ". Review them, then approve.";
          acRenderDrafts(chapter, s.subissues || []);
        } else if (s.status === "approval-incomplete") {
          var retained = s.created || [];
          st.innerHTML = '<span class="warn">⚠️ A previous approval was interrupted. ' +
            retained.length + " durable issue" + (retained.length === 1 ? " is" : "s are") +
            " retained; review the restored snapshot below and resume.</span>" +
            acCreatedList(retained);
          acRenderDrafts(chapter, s.subissues || []);
          var resume = acEl("ac-approve");
          if (resume) resume.textContent = "Resume approval";
        } else if (s.status === "approval-complete") {
          st.innerHTML = "✅ Chapter " + chapter +
            " was already approved and registered." + acCreatedList(s.created || []);
          acEl("ac-drafts").innerHTML = "";
        } else if (s.status === "error") {
          st.innerHTML = '<span class="err">Intake failed: ' + acEsc(s.error || "unknown") + "</span>";
        } else {
          st.innerHTML = '<span class="err">No intake job found — start one.</span>';
        }
      })
      .catch(function (e) {
        var st = acEl("ac-status");
        if (st) st.innerHTML = '<span class="err">' + acEsc(e.message) + "</span>";
      });
  }

  function acCreatedList(created) {
    if (!created || !created.length) return "";
    var html = '<ul class="ac-created">';
    created.forEach(function (c) {
      html += '<li>Draft ' + acEsc(c.index) + ': <a href="' + acEsc(c.url) +
        '" target="_blank" rel="noopener">#' + acEsc(c.issue) + "</a></li>";
    });
    return html + "</ul>";
  }

  function acRenderDrafts(chapter, subs) {
    var html = '<div class="drafts-list">';
    subs.forEach(function (s) {
      html += '<details class="draft"><summary><b>#' + s.index + "</b> · " +
        acEsc(s.title) + '</summary><pre class="draft-body">' + acEsc(s.body) + "</pre></details>";
    });
    html += "</div>";
    html += '<button id="ac-approve" class="ac-btn ac-approve">Approve &amp; open ' +
      subs.length + " issue" + (subs.length === 1 ? "" : "s") + "</button>";
    acEl("ac-drafts").innerHTML = html;
    acEl("ac-approve").addEventListener("click", function () { acApprove(chapter); });
  }

  function acApprove(chapter) {
    var btn = acEl("ac-approve");
    if (btn) { btn.disabled = true; btn.textContent = "Opening issues…"; }
    postJSON("/api/approve-chapter", { chapter: chapter })
      .then(function (r) {
        var created = r.created || [];
        var html = "<p>✅ Opened " + created.length + " issue" +
          (created.length === 1 ? "" : "s") + " for Chapter " + chapter + ":</p>" +
          '<ul class="ac-created">';
        created.forEach(function (c) {
          html += '<li><a href="' + acEsc(c.url) + '" target="_blank" rel="noopener">#' +
            c.issue + "</a></li>";
        });
        html += "</ul>";
        if (r.registry_error) {
          html += '<p class="warn">⚠️ Registry note: ' + acEsc(r.registry_error) +
            " — register the chapter in config.toml by hand.</p>";
        } else {
          html += '<p class="muted">Chapter ' + chapter + " registered in config.toml.</p>";
        }
        acEl("ac-drafts").innerHTML = html;
        acEl("ac-status").innerHTML = "";
      })
      .catch(function (e) {
        var detail = (e && e.data) || {};
        var retained = detail.created || [];
        if (btn) {
          btn.disabled = false;
          btn.textContent = detail.retryable ? "Resume approval" : "Approve & open issues";
        }
        var recovery = retained.length
          ? "<br><span class=\"warn\">These issue IDs were retained; retry will resume them, not create duplicates.</span>" +
            acCreatedList(retained)
          : "";
        acEl("ac-status").innerHTML = '<span class="err">Approve failed: ' +
          acEsc(e.message) + "</span>" + recovery;
      });
  }

  // --- Status tab: the conductor (global orchestrator) + jobs + landings ---
  // The prover on/off switch — writes the control file the conductor polls each
  // tick (POST /api/prover-toggle). Enabling starts leaves-first sorry-filling on
  // the prover branch; main stays skeleton.
  function proverToggle(d) {
    var enabled = d.prover_enabled === true;
    var unknown = d.prover_enabled == null;
    var input = el("input", { type: "checkbox" });
    input.checked = enabled;
    input.addEventListener("change", function () {
      var want = input.checked;
      input.disabled = true;
      postJSON("/api/prover-toggle", { enabled: want })
        .then(function () { renderStatusTab(true); })
        .catch(function (e) {
          input.checked = !want;
          input.disabled = false;
          alert("prover toggle failed: " + (e && e.message ? e.message : e));
        });
    });
    return el("label", {
      class: "prover-toggle",
      title: "Prove verified statements leaves-first (API lemmas → up to Stokes) "
           + "on the prover branch. main stays skeleton; Aristotle does the proving.",
    }, [input, el("span", {
      text: unknown ? "prover: unknown" : (enabled ? "prover: ON" : "prover: OFF"),
    })]);
  }

  // A successful start POST returns as soon as the detached process exists;
  // the conductor may need a moment to acquire and publish its lock. Poll a
  // bounded number of PURE status reads before declaring the recovery failed.
  function pollConductorUntilLive(attempts) {
    return getJSON("/api/conductor-status").then(function (d) {
      if (conductorIsReady(d) || attempts <= 1) return d;
      return new Promise(function (resolve) {
        setTimeout(resolve, 500);
      }).then(function () { return pollConductorUntilLive(attempts - 1); });
    });
  }

  function startConductorFromStatus() {
    if (state.conductorStartBusy) return;
    state.conductorStartBusy = true;
    state.conductorStartError = "";
    renderStatusTab(true);
    if (state.activeTab === "review") refreshQueue();
    postJSON("/api/conductor-start", {})
      .then(function () { return pollConductorUntilLive(16); })
      .then(function (d) {
        if (!conductorIsReady(d)) {
          throw new Error(
            "the launch was accepted, but no healthy conductor appeared"
          );
        }
        state.conductorStartBusy = false;
        state.conductorStartError = "";
        flashToast("Orchestrator restarted");
        renderStatusTab(true);
        if (state.activeTab === "review") refreshQueue();
      })
      .catch(function (e) {
        state.conductorStartBusy = false;
        state.conductorStartError = (e && e.message) ? e.message : String(e);
        renderStatusTab(true);
        if (state.activeTab === "review") refreshQueue();
      });
  }

  function conductorStartControl(d) {
    if (d.alive || !d.restart_allowed) return null;
    var button = el("button", {
      class: "btn btn-tiny cond-restart",
      type: "button",
      text: state.conductorStartBusy ? "Restarting…" : "Restart orchestrator",
      title: "Start the host conductor and resume its existing queued work",
    });
    button.disabled = state.conductorStartBusy;
    button.addEventListener("click", startConductorFromStatus);
    return el("div", { class: "cond-actions" }, [
      button,
      state.conductorStartError ? el("span", {
        class: "cond-start-error",
        role: "status",
        text: state.conductorStartError,
      }) : null,
    ]);
  }

  var STATUS_READ_TIMEOUT_MS = 15000;

  function clearStatusRefreshError(host) {
    if (!host) return;
    host.removeAttribute("data-status-stale");
    var warning = host.querySelector(".status-refresh-error");
    if (warning) warning.remove();
  }

  function showStatusRefreshError(host, err) {
    if (!host) return;
    var message = (err && err.message) || String(err || "network error");
    if (state.statusDashboardSnapshotKey && surfaceHasStableContent(host)) {
      host.setAttribute("data-status-stale", "true");
      var warning = host.querySelector(".status-refresh-error");
      if (!warning) {
        warning = el("div", {
          class: "status-refresh-error",
          "data-status-key": "status-refresh-error",
          role: "status",
          "aria-live": "polite",
        });
        host.appendChild(warning);
      }
      warning.textContent =
        "Status refresh failed — showing the last known state: " + message;
      return;
    }
    dashError("dash-status", err);
  }

  function statusRowKey(kind, values) {
    return kind + ":" + JSON.stringify(values || []);
  }

  function renderStatusTab(force) {
    if (state.activeTab !== "status") return; // a stale poll after a switch
    // The five-second timer must not overlap a slow conductor read. Calls made
    // while one is live share its completion. Explicit mutation/navigation
    // refreshes supersede the old read so a pre-mutation response cannot repaint.
    if (state.statusDashboardRequest && !force) {
      return state.statusDashboardRequest;
    }
    var host = dashHost("dash-status");
    if (!host) return;
    var generation = ++state.statusDashboardRequestSeq;
    beginSurfaceRefresh(host, "dash-loading", "refreshing status…");
    if (state.statusDashboardAbortController) {
      state.statusDashboardAbortController.abort();
    }
    var controller = newReadController();
    var request = withReadDeadline(
      getJSON("/api/conductor-status", readOptions(controller)),
      STATUS_READ_TIMEOUT_MS,
      controller,
      "Status refresh"
    );
    var completion = request.then(function (d) {
      if (generation !== state.statusDashboardRequestSeq ||
          state.activeTab !== "status") return null;
      var up = conductorIsReady(d);
      if (up) state.conductorStartError = "";
      // Heartbeat-only movement updates one text node and keeps the rest of
      // the dashboard (including focus and scroll position) mounted.
      var stableStatus = {};
      Object.keys(d || {}).forEach(function (key) {
        if (key !== "updated_ts" &&
            key !== "heartbeat_ts" &&
            key !== "heartbeat_age_seconds" &&
            key !== "progress_age_seconds") {
          stableStatus[key] = d[key];
        }
      });
      var snapshotKey = JSON.stringify([
        stableStatus,
        state.conductorStartBusy,
        state.conductorStartError,
      ]);
      if (snapshotKey === state.statusDashboardSnapshotKey &&
          surfaceHasStableContent(host)) {
        var heartbeat = host.querySelector(".cond-updated");
        if (heartbeat) {
          var heartbeatTs = d.heartbeat_ts || d.updated_ts;
          heartbeat.textContent = heartbeatTs
            ? ("updated " + fmtTs(heartbeatTs)) : "";
        }
        clearStatusRefreshError(host);
        return d;
      }
      var nextHost = document.createElement("div");
      // DEGRADED beats "running": a conductor with a broken dispatch env
      // (e.g. no Aristotle key) is alive but can't refine — a green chip
      // here hid exactly that during the 2026-07-07 incident.
      var degraded = !!d.alive && !!d.degraded;
      var progressStale = up && !!d.progress_stale;
      var warned = degraded || progressStale;
      var heartbeatTs = d.heartbeat_ts || d.updated_ts;
      nextHost.appendChild(el("div", {
        class: "cond-banner" + (warned ? " cond-degraded" : ""),
        "data-status-key": "conductor",
      }, [
        el("span", { class: "cond-dot " + (warned ? "degraded" : (up ? "up" : "down")) }),
        el("div", { class: "cond-summary" }, [
          el("div", { class: "cond-title",
            text: degraded ? "conductor DEGRADED — not dispatching"
                : progressStale ? "conductor running — progress is stale"
                : up ? "conductor running"
                : d.alive ? "conductor unresponsive or starting"
                : "conductor stopped" }),
          el("div", { class: "dash-empty",
            text: degraded ? String(d.degraded)
                : progressStale
                  ? "process heartbeat is live, but the main loop has not completed a tick recently"
                : up ? "dispatches refines + auto-merges PRs across all chapters"
                     : d.alive
                       ? "process is live but has no fresh ready heartbeat — inspect its log"
                     : d.restart_allowed
                       ? "no live orchestrator — restart it here to resume queued work"
                       : "no live orchestrator — host restart control is disabled" }),
          conductorStartControl(d),
        ]),
        el("div", { class: "cond-meta" }, [
          el("div", {}, ["pid ", el("span", { class: "mono",
            text: d.pid == null ? "—" : String(d.pid) })]),
          el("div", { text: "concurrency " + (d.concurrency == null ? "—" : d.concurrency) }),
          el("div", { class: "cond-updated",
            text: heartbeatTs ? ("updated " + fmtTs(heartbeatTs)) : "" }),
          proverToggle(d),
        ]),
      ]));
      nextHost.appendChild(el("div", {
        class: "stat-row dash-section",
        "data-status-key": "stats",
      }, [
        statChip(d.running || 0, "running"),
        statChip((d.queued || []).length, "queued"),
        statChip((d.jobs || []).length, "jobs tracked"),
        statChip((d.landings || []).length, "recent landings"),
      ]));
      var jobsSec = el("section", {
        class: "dash-section",
        "data-status-key": "jobs",
      },
        [el("div", { class: "dash-h", text: "Aristotle jobs" })]);
      if (!(d.jobs || []).length)
        jobsSec.appendChild(el("div", { class: "dash-empty", text: "no jobs — the queue is drained" }));
      (d.jobs || []).forEach(function (j) {
        // The project id only (the granular PHASE replaces the misleading
        // raw aristotle_status like "(COMPLETE)").
        var ar = j.project_id ? String(j.project_id).slice(0, 8) : "";
        var running = (j.status || "").indexOf("running") === 0;
        var kids = [
          el("span", { class: "job-issue", text: "#" + (j.issue_num == null ? "?" : j.issue_num) }),
          el("span", { class: "job-chip", text: "c" + (j.chapter == null ? "?" : j.chapter) }),
          statusBadge(j.status),
        ];
        // The granular phase (proving NN% / building / committing / pushing /
        // opening PR) for a running job — the key "it's still building, not done".
        if (running && j.phase) {
          var pslug = String(j.phase).split(" ")[0].toLowerCase().replace(/[^a-z]/g, "");
          kids.push(el("span", { class: "phase-chip phase-" + pslug, text: j.phase }));
        }
        // Live iteration + Aristotle progress for a running job.
        var it = j.iteration;
        if (it && (it.current != null || it.completed != null)) {
          var n = it.current != null ? it.current : it.completed;
          kids.push(el("span", { class: "job-iter", text: "iter " + n }));
        }
        if (j.progress_pct != null) {
          kids.push(el("span", { class: "job-prog" }, [
            el("span", { class: "job-prog-bar" }, [
              el("span", { class: "job-prog-fill",
                style: "width:" + j.progress_pct + "%" }),
            ]),
            el("span", { class: "job-prog-pct", text: j.progress_pct + "%" }),
          ]));
        }
        if (ar) kids.push(el("span", { class: "job-aristotle", text: ar }));
        kids.push(el("span", { class: "job-open", text: "details ›" }));
        var row = el("div", {
          class: "job-row job-row-click",
          "data-status-key": statusRowKey("job", [
            j.issue_num, j.started_ts, j.pid, j.branch,
          ]),
          title: "Open this run's ratings + prompts",
          onclick: (function (issue) {
            return function () { if (issue != null) openRunDetail(issue); };
          })(j.issue_num),
        }, kids);
        jobsSec.appendChild(row);
      });
      nextHost.appendChild(jobsSec);
      // Queued (rejected, not yet a running job) — the "I shipped a reject,
      // where is it?" gap. Single-flight + same-chapter means a reject waits
      // behind a running same-chapter job; show it here with the reason.
      var queued = d.queued || [];
      var qSec = el("section", {
        class: "dash-section",
        "data-status-key": "queued",
      },
        [el("div", { class: "dash-h", text: "Queued (waiting to dispatch)" })]);
      if (!queued.length) {
        qSec.appendChild(el("div", { class: "dash-empty", text: "nothing queued" }));
      }
      queued.forEach(function (it) {
        qSec.appendChild(el("div", {
          class: "job-row queued-row",
          "data-status-key": statusRowKey("queued", [it.issue_num]),
        }, [
          el("span", { class: "job-issue", text: "#" + (it.issue_num == null ? "?" : it.issue_num) }),
          el("span", { class: "job-chip", text: "c" + (it.chapter == null ? "?" : it.chapter) }),
          el("span", { class: "sb sb-queued", text: "queued" }),
          el("span", { class: "job-wait", text: it.reason || "queued" }),
        ]));
      });
      nextHost.appendChild(qSec);
      var landSec = el("section", {
        class: "dash-section",
        "data-status-key": "landings",
      },
        [el("div", { class: "dash-h", text: "Recent landings" })]);
      if (!(d.landings || []).length)
        landSec.appendChild(el("div", { class: "dash-empty", text: "none yet" }));
      // Backend is newest-first. A landing is a merged refine PR (issue + PR,
      // both linked to GitHub); the opt-in --land next records carry a sha.
      (d.landings || []).forEach(function (l) {
        var kids = [
          ghLink(d.repo, "issues", l.issue,
                 "#" + (l.issue == null ? "?" : l.issue), "job-issue"),
        ];
        if (l.pr != null)
          kids.push(ghLink(d.repo, "pull", l.pr, "merged PR #" + l.pr, "job-aristotle"));
        else if (l.sha_landed)
          kids.push(el("span", { class: "job-aristotle mono",
                                 text: String(l.sha_landed).slice(0, 7) }));
        kids.push(el("span", { class: "job-when", text: fmtTs(l.ts) }));
        landSec.appendChild(el("div", {
          class: "land-row",
          "data-status-key": statusRowKey("landing", [l.issue, l.pr, l.ts]),
        }, kids));
      });
      nextHost.appendChild(landSec);
      syncDomChildren(host, nextHost);
      state.statusDashboardSnapshotKey = snapshotKey;
      clearStatusRefreshError(host);
      return d;
    }, function (e) {
      if (generation === state.statusDashboardRequestSeq &&
          state.activeTab === "status") {
        showStatusRefreshError(host, e);
      }
      return null;
    }).then(function (result) {
      if (state.statusDashboardRequest === completion) {
        state.statusDashboardRequest = null;
        if (state.statusDashboardAbortController === controller) {
          state.statusDashboardAbortController = null;
        }
      }
      if (generation === state.statusDashboardRequestSeq) {
        endSurfaceRefresh(host);
      }
      return result;
    }, function (e) {
      if (state.statusDashboardRequest === completion) {
        state.statusDashboardRequest = null;
        if (state.statusDashboardAbortController === controller) {
          state.statusDashboardAbortController = null;
        }
      }
      if (generation === state.statusDashboardRequestSeq) {
        endSurfaceRefresh(host);
        if (state.activeTab === "status") showStatusRefreshError(host, e);
      }
      return null;
    });
    state.statusDashboardRequest = completion;
    state.statusDashboardAbortController = controller;
    return completion;
  }

  // openRunDetail — the click-into-a-run panel (Status tab → a job): its live
  // progress + iteration, the per-iteration auto-rater RATINGS, and the
  // refine-reviewer PROMPTS drafted for Aristotle. A PURE READ (/api/run/{n}).
  function openRunDetail(issue) {
    getJSON("/api/run/" + encodeURIComponent(issue)).then(function (d) {
      var panel = el("div", { class: "modal-panel run-detail" });
      panel.appendChild(el("h2", { class: "modal-title", text: "Run #" + issue }));
      if (!d || !d.found) {
        panel.appendChild(el("p", { class: "dash-empty",
          text: "no conductor run recorded for #" + issue }));
        panel.appendChild(el("div", { class: "modal-actions" }, [
          el("button", { class: "btn", type: "button",
            onclick: function () { closeModal(); } }, ["Close"]),
        ]));
        openModal(panel);
        return;
      }
      var meta = [];
      if (d.status) meta.push("status " + d.status);
      var it = d.iteration;
      if (it && (it.current != null || it.completed != null))
        meta.push("iteration " + (it.current != null ? it.current : it.completed));
      if (d.progress_pct != null) meta.push(d.progress_pct + "% complete");
      if (d.aristotle_status) meta.push("aristotle " + d.aristotle_status);
      panel.appendChild(el("p", { class: "modal-sub run-meta", text: meta.join(" · ") }));

      var rsec = el("section", { class: "run-sec" },
        [el("h3", { class: "run-h", text: "Review ratings" })]);
      if (!(d.ratings || []).length)
        rsec.appendChild(el("p", { class: "dash-empty", text: "no ratings yet" }));
      (d.ratings || []).forEach(function (r, i) {
        var chips = Object.keys(r || {}).map(function (k) {
          return el("span", { class: "rate-chip" }, [
            el("b", { text: k + ": " }), String(r[k]),
          ]);
        });
        rsec.appendChild(el("div", { class: "run-rating" }, [
          el("span", { class: "run-rating-n", text: "#" + (i + 1) }),
          el("span", { class: "run-rating-chips" }, chips),
        ]));
      });
      panel.appendChild(rsec);

      var psec = el("section", { class: "run-sec" },
        [el("h3", { class: "run-h", text: "Refine-reviewer prompts (→ Aristotle)" })]);
      var pbody = el("div", { class: "run-prompts md-body" });
      // The prompts are markdown (from marathon-refine-log.md) — render them
      // fully; degrade to plain text if the renderer is unavailable.
      if (window.MarathonLatex && window.MarathonLatex.renderMarkdown && d.prompts) {
        try { window.MarathonLatex.renderMarkdown(pbody, d.prompts); }
        catch (e) { pbody.textContent = d.prompts; }
      } else {
        pbody.textContent = d.prompts || "(none yet)";
      }
      psec.appendChild(pbody);
      panel.appendChild(psec);

      panel.appendChild(el("div", { class: "modal-actions" }, [
        el("button", { class: "btn", type: "button",
          onclick: function () { closeModal(); } }, ["Close"]),
      ]));
      openModal(panel);
    }).catch(function () {
      var panel = el("div", { class: "modal-panel" }, [
        el("h2", { class: "modal-title", text: "Run #" + issue }),
        el("p", { class: "dash-empty", text: "could not load run detail" }),
        el("div", { class: "modal-actions" }, [
          el("button", { class: "btn", type: "button",
            onclick: function () { closeModal(); } }, ["Close"]),
        ]),
      ]);
      openModal(panel);
    });
  }

  // ghLink — a clickable GitHub link for an issue/PR number. `kind` is "issues"
  // or "pull". Degrades to a plain span when the repo or number is unknown.
  // stopPropagation so clicking the link (in a clickable row) opens GitHub only.
  function ghLink(repo, kind, n, label, cls) {
    var text = label != null ? label : ("#" + n);
    var klass = (cls ? cls + " " : "") + "gh-num";
    if (!repo || n == null) return el("span", { class: klass, text: text });
    return el("a", {
      class: klass + " gh-link",
      href: "https://github.com/" + repo + "/" + kind + "/" + n,
      target: "_blank", rel: "noopener noreferrer",
      title: "open " + (kind === "pull" ? "PR" : "issue") + " #" + n + " on GitHub",
      onclick: function (ev) { ev.stopPropagation(); },
    }, [text]);
  }

  // --- Issues tab: every registered issue grouped by chapter ---

  var RECOVERY_ACTION_ORDER = [
    "retry_build", "continue_pr", "update_continue", "start_over",
  ];
  var RECOVERY_ACTION_COPY = {
    retry_build: {
      title: "Retry build",
      preservation: "Keeps the exact PR head. Does not call Aristotle.",
    },
    continue_pr: {
      title: "Continue existing PR",
      preservation: "Keeps the current diff and resumes its checkpoint when available.",
    },
    update_continue: {
      title: "Update from main & continue",
      preservation: "Keeps the PR diff while bringing it onto current main.",
    },
    start_over: {
      title: "Archive & start over",
      preservation: "Archives the old head, then starts a new Aristotle run from main.",
    },
  };
  var recoveryDialogSeq = 0;

  // One shared recovery dialog serves both the drained Review stage and the
  // Issues tab. It presents only server-derived capabilities and sends only an
  // action name + snapshot token; no client-controlled git refs cross the API.
  function openRecoveryDialog(row, repo, after, trigger) {
    var actions = recoveryActions(row);
    var selected = recommendedRecovery(row);
    if (!selected) {
      flashToast("No safe recovery action is currently available for #" + row.num);
      return;
    }

    var seq = ++recoveryDialogSeq;
    var titleId = "recovery-title-" + seq;
    var statusId = "recovery-status-" + seq;
    var completed = false;
    var busy = false;
    var refreshedAfterCompletion = false;
    var radios = [];

    function refreshOriginOnce() {
      if (refreshedAfterCompletion) return;
      refreshedAfterCompletion = true;
      if (typeof after === "function") after();
    }

    var title = el("h2", {
      class: "modal-title recovery-title",
      id: titleId,
      text: "Recover issue #" + row.num,
    });
    var contextKids = [];
    if (row.fix_pr != null) {
      contextKids.push("Continue from ");
      contextKids.push(ghLink(repo, "pull", row.fix_pr, "PR #" + row.fix_pr, "mono"));
      if (row.fix_pr_branch) contextKids.push(" on " + row.fix_pr_branch);
      if (row.fix_pr_head_sha) {
        contextKids.push(" at ");
        contextKids.push(el("span", {
          class: "mono recovery-sha",
          title: row.fix_pr_head_sha,
          text: row.fix_pr_head_sha.slice(0, 8),
        }));
      }
      if (row.fix_pr_build) contextKids.push(" · build " + row.fix_pr_build);
      if (row.local_recovery_available) {
        contextKids.push(" · unpublished local checkpoint available");
      }
      contextKids.push(".");
    } else {
      contextKids.push("No open Marathon PR was found. Only actions that can safely "
        + "recover from the stored rejection are available.");
    }
    var context = el("p", { class: "modal-sub recovery-context" }, contextKids);

    var conductorNote = null;
    if (row.conductor_online === false) {
      conductorNote = el("div", {
        class: "recovery-conductor-note",
        role: "note",
        text: row.host_conductor_disabled
          ? "This deck cannot launch a conductor on its host. The request will be saved and wait for an external conductor."
          : "The conductor appears offline. You can save the request now; it will wait until a conductor is running.",
      });
    }

    var optionList = el("div", {
      class: "recovery-options",
      role: "radiogroup",
      "aria-label": "Recovery method",
    });
    RECOVERY_ACTION_ORDER.forEach(function (name) {
      var spec = actions[name];
      if (!spec) return;
      var copy = RECOVERY_ACTION_COPY[name] || { title: name, preservation: "" };
      if (name === "start_over" && row.fix_pr == null) {
        copy = {
          title: "Start over",
          preservation: "Starts a new Aristotle run from current main.",
        };
      }
      var inputId = "recovery-" + seq + "-" + name;
      var recommended = name === recommendedRecovery(row);
      var radio = el("input", {
        class: "recovery-radio",
        id: inputId,
        type: "radio",
        name: "recovery-action-" + seq,
        value: name,
        checked: name === selected ? "checked" : null,
        disabled: spec.enabled ? null : "disabled",
        autofocus: name === selected ? "autofocus" : null,
      });
      radios.push(radio);
      var badges = [];
      if (recommended) badges.push(el("span", {
        class: "recovery-badge recovery-badge-recommended",
        text: "Recommended",
      }));
      if (spec.cost) badges.push(el("span", {
        class: "recovery-badge",
        text: spec.cost,
      }));
      if (spec.destructive || name === "start_over") badges.push(el("span", {
        class: "recovery-badge recovery-badge-danger",
        text: "Destructive",
      }));
      var body = el("span", { class: "recovery-choice-body" }, [
        el("span", { class: "recovery-choice-head" }, [
          el("strong", { text: copy.title }),
          el("span", { class: "recovery-badges" }, badges),
        ]),
        el("span", {
          class: "recovery-choice-reason",
          text: spec.reason || copy.preservation,
        }),
        el("span", {
          class: "recovery-choice-preservation",
          text: copy.preservation,
        }),
      ]);
      optionList.appendChild(el("label", {
        class: "recovery-option" + (spec.enabled ? "" : " is-disabled"),
        for: inputId,
      }, [radio, body]));
    });

    var destructiveCheck = el("input", {
      id: "recovery-destructive-" + seq,
      type: "checkbox",
    });
    var destructiveWrap = el("div", {
      class: "recovery-destructive-confirm",
    }, [
      el("label", { for: "recovery-destructive-" + seq }, [
        destructiveCheck,
        el("span", { text:
          (row.fix_pr != null
            ? "I understand that this archives the existing PR head and starts a new Aristotle run from current main."
            : "I understand that this abandons the parked recovery state and starts a new Aristotle run from current main.")
        }),
      ]),
    ]);
    var status = el("div", {
      class: "recovery-status",
      id: statusId,
      role: "status",
      tabindex: "-1",
      "aria-live": "polite",
      "aria-atomic": "true",
    });
    var cancel = el("button", {
      class: "btn",
      type: "button",
      onclick: function () { closeModal(); },
    }, ["Cancel"]);
    var submit = el("button", {
      class: "btn btn-primary",
      type: "submit",
    }, ["Queue recovery"]);
    var actionsBar = el("div", { class: "modal-actions" }, [cancel, submit]);
    var form = el("form", {
      class: "recovery-form",
      "aria-describedby": statusId,
    }, [
      title,
      context,
      conductorNote,
      optionList,
      destructiveWrap,
      status,
      actionsBar,
    ]);
    var panel = el("div", {
      class: "modal-panel recovery-modal",
      role: "dialog",
      "aria-modal": "true",
      "aria-labelledby": titleId,
    }, [form]);

    function chosenName() {
      for (var i = 0; i < radios.length; i++) {
        if (radios[i].checked) return radios[i].value;
      }
      return null;
    }

    function selectedIsDestructive() {
      var name = chosenName();
      return name === "start_over" ||
        !!(name && actions[name] && actions[name].destructive);
    }

    function updateConfirmation(resetCheck) {
      var destructive = selectedIsDestructive();
      if (resetCheck) destructiveCheck.checked = false;
      if (destructive) destructiveWrap.removeAttribute("hidden");
      else destructiveWrap.setAttribute("hidden", "hidden");
      submit.disabled = busy || !chosenName() ||
        (destructive && !destructiveCheck.checked);
      submit.className = "btn " + (destructive ? "btn-danger" : "btn-primary");
    }

    radios.forEach(function (radio) {
      radio.addEventListener("change", function () {
        status.textContent = "";
        status.className = "recovery-status";
        updateConfirmation(true);
      });
    });
    destructiveCheck.addEventListener("change", function () {
      updateConfirmation(false);
    });
    updateConfirmation(false);

    form.addEventListener("submit", function (ev) {
      ev.preventDefault();
      if (busy) return;
      var action = chosenName();
      var destructive = selectedIsDestructive();
      if (!action) return;
      if (destructive && !destructiveCheck.checked) {
        status.textContent = "Confirm the archive-and-start-over warning first.";
        status.className = "recovery-status is-error";
        destructiveCheck.focus();
        return;
      }
      busy = true;
      status.textContent = "Saving recovery request…";
      status.className = "recovery-status";
      submit.disabled = true;
      submit.textContent = "Queueing…";
      cancel.disabled = true;
      destructiveCheck.disabled = true;
      radios.forEach(function (radio) { radio.disabled = true; });

      postRecovery(row.num, action, row.recovery_token, destructive)
        .then(function (res) {
          completed = true;
          busy = false;
          var waiting = res && (res.conductor_online === false ||
            res.queue_state === "waiting_for_conductor");
          status.textContent = (res && res.message ? res.message :
            "Recovery queued for #" + row.num + ".") +
            (waiting
              ? " The request is saved and will start when a conductor is online."
              : " You can close this dialog; progress will appear in Status.");
          status.className = "recovery-status is-success";
          cancel.setAttribute("hidden", "hidden");
          submit.disabled = false;
          submit.textContent = "Close";
          submit.setAttribute("type", "button");
          submit.onclick = function () { closeModal(); };
          submit.focus();
          // Escape/backdrop remains available during a slow request. If the
          // user already closed the dialog, still refresh the originating
          // surface when the durable request is accepted.
          if (!document.contains(panel)) {
            flashToast(status.textContent);
            refreshOriginOnce();
          }
        })
        .catch(function (err) {
          busy = false;
          status.textContent = err && err.message
            ? err.message
            : "Could not queue recovery.";
          status.className = "recovery-status is-error";
          cancel.disabled = false;
          destructiveCheck.disabled = false;
          radios.forEach(function (radio) {
            var spec = actions[radio.value];
            radio.disabled = !(spec && spec.enabled);
          });
          submit.textContent = "Queue recovery";
          updateConfirmation(false);
          if (document.contains(panel)) status.focus();
          else flashToast(status.textContent);
        });
    });

    openModal(panel, function () {
      if (completed) refreshOriginOnce();
    }, trigger);
  }

  // Parked rejections are not in the swipe queue, so this best-effort section
  // keeps them reachable without the CLI. Every row opens the shared recovery
  // chooser; no recovery action fires from the row itself.
  // Best-effort parked-recovery data is fetched alongside the overview and
  // handed here synchronously. That makes the Issues tab one atomic paint and
  // prevents late stuck reads from inserting duplicate sections.
  function renderStuckSection(host, repo, stuckSnapshot) {
    var stuck = (stuckSnapshot && stuckSnapshot.issues) || [];
    if (!stuck.length) return;
    var rows = stuck.map(function (it) {
      var kids = [
        ghLink(repo, "issues", it.num, "#" + it.num, "issue-id"),
        statusBadge(it.status),
        el("span", { class: "issue-title", title: it.note_preview || "",
                     text: it.fix_pr != null ? stuckRowText(it)
                         : (it.note_preview || "(no stored note)") }),
      ];
      if (it.fix_pr != null)
        kids.push(ghLink(repo, "pull", it.fix_pr, "PR #" + it.fix_pr, "issue-tier"));
      if (it.attempts) kids.push(el("span", { class: "issue-tier",
                                              text: it.attempts + " tries" }));
      kids.push(recoveryButton(it, repo, renderIssuesTab));
      return el("div", { class: "issue-row stuck-row" }, kids);
    });
    var section = el("section", { class: "chap-block stuck-block" }, [
      el("div", { class: "chap-title" }, [
        "Parked — choose recovery",
        el("span", { class: "chap-count", text: stuck.length + " parked" }),
      ]),
    ].concat(rows));
    host.insertBefore(section, host.firstChild);
  }

  var ISSUES_READ_TIMEOUT_MS = 30000;

  function renderIssuesTab() {
    if (state.activeTab !== "issues") return;
    var host = dashHost("dash-issues");
    if (!host) return;
    var generation = ++state.issuesRequestSeq;
    var hadContent = beginSurfaceRefresh(
      host, "dash-loading", "refreshing issues…"
    );
    if (state.issuesAbortController) state.issuesAbortController.abort();

    function startIssuesRefresh(requestGeneration, preservedContent) {
      var controller = newReadController();
      var options = readOptions(controller);
      var request = withReadDeadline(Promise.all([
        getJSON("/api/issues-overview", options),
        getJSON("/api/stuck", options).then(function (data) {
          return { ok: true, data: data || {} };
        }, function () {
          return { ok: false, data: null };
        }),
      ]), ISSUES_READ_TIMEOUT_MS, controller, "Issues refresh");
      var completion = request.then(function (parts) {
        if (requestGeneration !== state.issuesRequestSeq ||
            state.activeTab !== "issues") return null;
        var d = parts[0] || {};
        var stuckRead = parts[1] || { ok: false };
        var stuckSnapshot = stuckRead.ok
          ? {
              issues: ((stuckRead.data && stuckRead.data.issues) || []).slice(),
              repo: (stuckRead.data && stuckRead.data.repo) || d.repo || null,
            }
          : state.issuesStuck;
        var snapshotKey = JSON.stringify([d, stuckSnapshot]);
        state.issuesStuck = stuckSnapshot || { issues: [], repo: null };
        if (snapshotKey === state.issuesSnapshotKey &&
            surfaceHasStableContent(host)) {
          return parts;
        }
        state.issuesSnapshotKey = snapshotKey;
        host.innerHTML = "";
        var counts = d.counts || {};
        var order = ["building", "ready", "verified", "rejected", "stalled", "blocked", "pending"];
        var chips = order.filter(function (s) { return counts[s]; })
          .map(function (s) { return statChip(counts[s], s); });
        if (chips.length) host.appendChild(el("div", { class: "stat-row dash-section" }, chips));
        var any = false;
        (d.chapters || []).forEach(function (ch) {
          if (!(ch.issues || []).length) return;
          any = true;
          var block = el("section", { class: "chap-block" }, [
            el("div", { class: "chap-title" }, [
              "Chapter " + ch.chapter,
              el("span", { class: "chap-count", text: ch.issues.length + " issues" }),
            ]),
          ]);
          ch.issues.forEach(function (it) {
            var kids = [
              ghLink(d.repo, "issues", it.id, "#" + it.id, "issue-id"),
              statusBadge(it.status),
              el("span", { class: "issue-title", title: it.title || "", text: it.title || "(untitled)" }),
            ];
            // The readiness reason earns its chip only when it says something
            // the status badge doesn't — "already verified" next to a
            // verified pill is redundant and its warning styling reads as a
            // problem where none exists.
            var redundantReason = it.blocked_reason === "already " + it.status;
            if (it.blocked_reason && !redundantReason) kids.push(el("span", {
              class: "issue-blocker",
              title: it.blocked_reason,
              text: it.blocked_reason,
            }));
            if (it.tier) kids.push(el("span", { class: "issue-tier", text: it.tier }));
            var row = el("div", { class: "issue-row" + (it.ready ? " is-ready" : "") }, kids);
            if (it.ready) row.addEventListener("click", function () {
              switchTab("review"); openIssue(it.id);
            });
            block.appendChild(row);
          });
          host.appendChild(block);
        });
        if (!any) host.appendChild(el("div", { class: "dash-empty", text: "no registered issues" }));
        renderStuckSection(host, d.repo || state.issuesStuck.repo, state.issuesStuck);
        return parts;
      }, function (err) {
        if (requestGeneration !== state.issuesRequestSeq ||
            state.activeTab !== "issues") return null;
        if (preservedContent || surfaceHasStableContent(host)) {
          endSurfaceRefresh(host);
          flashToast("Could not refresh issues: " +
            ((err && err.message) || "network error"));
        } else {
          dashError("dash-issues", err);
        }
        return null;
      }).then(function (result) {
        var current = state.issuesRequest === completion;
        if (current) {
          state.issuesRequest = null;
          if (state.issuesAbortController === controller) {
            state.issuesAbortController = null;
          }
        }
        if (requestGeneration !== state.issuesRequestSeq) return result;
        endSurfaceRefresh(host);
        return result;
      }, function (err) {
        var current = state.issuesRequest === completion;
        if (current) {
          state.issuesRequest = null;
          if (state.issuesAbortController === controller) {
            state.issuesAbortController = null;
          }
        }
        if (requestGeneration !== state.issuesRequestSeq) return null;
        endSurfaceRefresh(host);
        if (preservedContent || surfaceHasStableContent(host)) {
          if (state.activeTab === "issues") {
            flashToast("Could not refresh issues: " +
              ((err && err.message) || "rendering error"));
          }
        } else if (state.activeTab === "issues") {
          dashError("dash-issues", err);
        }
        return null;
      });
      state.issuesRequest = completion;
      state.issuesAbortController = controller;
      return completion;
    }

    return startIssuesRefresh(generation, hadContent);
  }

  // --- History tab: issue-status changes + merged PRs ---
  function renderHistoryTab() {
    var host = dashHost("dash-history");
    if (!host) return;
    host.innerHTML = "";
    host.appendChild(el("div", { class: "dash-loading", text: "loading history…" }));
    getJSON("/api/history").then(function (d) {
      if (state.activeTab !== "history") return;
      host.innerHTML = "";
      var vSec = el("section", { class: "dash-section" },
        [el("div", { class: "dash-h", text: "Issue status changes" })]);
      if (!(d.verdict_events || []).length)
        vSec.appendChild(el("div", { class: "dash-empty", text: "no recorded verdicts yet" }));
      (d.verdict_events || []).forEach(function (e) {
        vSec.appendChild(el("div", { class: "hist-row" }, [
          el("span", { class: "hist-ts", text: fmtTs(e.ts) }),
          el("div", { class: "hist-body" }, [
            ghLink(d.repo, "issues", e.issue_num, "#" + e.issue_num, "job-issue"), " ",
            statusBadge(e.verdict),
            e.notes ? el("div", { class: "dash-empty", text: e.notes }) : null,
          ]),
        ]));
      });
      host.appendChild(vSec);
      var pSec = el("section", { class: "dash-section" },
        [el("div", { class: "dash-h", text: "Merged PRs" })]);
      if (!(d.merged_prs || []).length)
        pSec.appendChild(el("div", { class: "dash-empty", text: "none" }));
      (d.merged_prs || []).forEach(function (p) {
        pSec.appendChild(el("div", { class: "hist-row" }, [
          el("span", { class: "hist-ts", text: fmtTs(p.merged_at) }),
          el("div", { class: "hist-body" }, [
            ghLink(d.repo, "pull", p.number, "#" + p.number, "hist-pr"), " ",
            el("span", { text: p.title || "" }),
          ]),
        ]));
      });
      host.appendChild(pSec);
    }).catch(function (e) { dashError("dash-history", e); });
  }

  // === wiring ==============================================================
  function attachButtons() {
    document.getElementById("btn-verify").addEventListener("click", actVerify);
    document.getElementById("btn-reject").addEventListener("click", actReject);
    document.getElementById("btn-defer").addEventListener("click", actDefer);
    document.getElementById("btn-deps").addEventListener("click", openDeps);
    document.getElementById("btn-deepdive").addEventListener("click", openDeepDive);
    // Dashboard tab bar — switching is a pure read (no POST).
    var tabs = document.querySelectorAll(".tab-btn");
    for (var i = 0; i < tabs.length; i++) {
      tabs[i].addEventListener("click", function (e) {
        switchTab(e.currentTarget.getAttribute("data-tab"));
      });
    }
  }

  function init() {
    attachButtons();
    attachKeys();
    attachSwipe();
    setActionsEnabled(false);
    var savedShip = readShipContext();
    // Prime the compact current-source identifier index before rendering code,
    // so project terms (including ambiguous short names) are clickable on the
    // first card. Lookup failure never blocks the review deck; kernel links
    // remain available as the honest fallback.
    if (!savedShip) {
      refreshQueue(); // pure read — opens the first ready issue's subdeck immediately
    } else {
      // A reload must reconnect to the durable job with PURE GET polling,
      // never fire a second POST or misleadingly offer Ship again.
      resumeSavedShipContext(savedShip);
    }
    refreshDefinitionTerms(null, true);
    ensureVersoMetadata(false); // optional semantic source; raw source never waits on it
    startStatus();    // pure read (SSE/poll)
  }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", init);
  } else {
    init();
  }

  // Expose a tiny surface for tests / debugging (read-only helpers). setSkipApi
  // is a PURE-READ view flip (it never POSTs) — exposed so the toggle can be
  // driven programmatically without simulating a click.
  window.MarathonDeck = {
    readSessionToken: readSessionToken,
    readSkipApiPref: readSkipApiPref,
    setSkipApi: setSkipApi,
    tierClass: tierClass,
    // Reconciliation read-model (PURE READS — never POST). Exposed so the
    // footnote / surfaced-badge can be exercised without simulating clicks.
    // Signature drift is reconciled to the code by the conductor (server-side).
    hasReconcileDrift: hasReconcileDrift,
    staleCitations: staleCitations,
    reconcileSummary: reconcileSummary,
    // Pure review-model helpers used by the dependency map/order and source
    // definition links. Exposed for deterministic smoke tests/debugging.
    localDependencyNames: localDependencyNames,
    dependencyOrder: dependencyOrder,
    kernelTermLookup: kernelTermLookup,
    projectTermCandidates: projectTermCandidates,
    showDefinitionLookup: showDefinitionLookup,
    // Security and progressive-enhancement hooks are exposed for deterministic
    // browser smoke tests. They perform reads/renders only and never POST.
    sanitizeVersoHTML: sanitizeVersoHTML,
    safeVersoURL: safeVersoURL,
    ensureVersoMetadata: ensureVersoMetadata,
    reconciliationProposalPanel: reconciliationProposalPanel,
    readerTargetMatches: readerTargetMatches,
    graphLayout: graphLayout,
    graphModeView: graphModeView,
    graphTopologyMeta: graphTopologyMeta,
    graphConnectedSubset: graphConnectedSubset,
    switchTab: switchTab,   // dashboard tab navigation (pure read)
    _state: state,
  };
})();
