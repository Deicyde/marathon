/*
 * app.js — the marathon deck dashboard + swipe review UI (vanilla JS, no build).
 *
 * SUBDECK FLOW (the shape this file implements):
 *   The ready QUEUE is the ISSUE picker. Picking a ready issue opens its
 *   SUBDECK — one DECLARATION card at a time. You swipe each declaration
 *   (verify / reject / defer) which records a LOCAL per-decl verdict only.
 *   When every declaration has been reviewed — OR the first rejection pauses
 *   the dependency-ordered review — a confirm-and-SHIP summary card appears;
 *   shipping composes the issue outcome and is the single irreversible action.
 *
 * Codes to the SHARED API CONTRACT:
 *   GET  /api/queue?chapter=N           -> {cards:[CardSummary], building, landed_today}
 *   GET  /api/subdeck/{issue}           -> {issue,title,chapter,card_tier,cards:[DeclCard],
 *                                            progress:{reviewed,total}, complete, summary}
 *   POST /api/decl-verdict {issue,decl,verdict,note?}  (LOCAL ONLY — no GitHub/Aristotle/ledger)
 *                                       -> {ok, progress, complete, summary, next_decl}
 *   POST /api/ship {issue}              (THE ONLY IRREVERSIBLE side-effect)
 *                                       -> {ok, outcome, message, advanced_to_issue}
 *   GET  /api/status (poll) or /api/events (SSE)  -> live job/build events
 *
 * BINDING SAFETY (load-bearing — see the task brief):
 *   - POST /api/ship is fired ONLY from shipIssue() (the explicit ship-button
 *     handler). It is NEVER called on page load, GET, navigation, prefetch,
 *     status polling, OR a per-declaration verdict. Reviewing/swiping a
 *     declaration is LOCAL-ONLY (POST /api/decl-verdict records a sidecar
 *     verdict — it merges nothing, dispatches no Aristotle).
 *     (tests/test_deck_frontend.py statically enforces this single call site.)
 *   - Every POST (decl-verdict AND ship) carries the per-session token in the
 *     X-Marathon-Session-Token header, read from the injected meta tag /
 *     bootstrap global. No token => the POST is refused client-side.
 *   - Reject notes are sent VERBATIM (no client-side rewriting) and are
 *     composed by the BACKEND into the issue's fix instructions; the note is
 *     REQUIRED on a reject.
 *   - Ship asks for an explicit confirm (it merges a PR or dispatches
 *     Aristotle — irreversible).
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
  function getJSON(url) {
    return fetch(url, {
      method: "GET",
      headers: { Accept: "application/json" },
      credentials: "same-origin",
    }).then(function (r) {
      if (!r.ok) throw new Error("HTTP " + r.status + " for " + url);
      return r.json();
    });
  }
  function fetchQueue(chapter) {
    var q = chapter ? "?chapter=" + encodeURIComponent(chapter) : "";
    return getJSON("/api/queue" + q);
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
  function fetchProjectGraph() {
    return getJSON("/api/project-graph");
  }
  function fetchDefinitionTerms() {
    return getJSON("/api/definition-terms");
  }
  function fetchDefinition(name) {
    var url = "/api/definition?name=" + encodeURIComponent(name);
    if (state.issue != null) url += "&issue=" + encodeURIComponent(state.issue);
    return getJSON(url);
  }
  function fetchReconciliation(issue) {
    return getJSON("/api/reconciliation/" + encodeURIComponent(issue));
  }

  // --- network: the token-bearing POST primitive ---------------------------
  // postJSON attaches the per-session token (header + body fallback) and
  // unwraps the {ok:false,...} convention. It is the shared transport for
  // BOTH the local decl-verdict and the irreversible ship — but note the
  // two have very different consequences (see the two named callers below).
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
  function postDeclVerdict(issue, decl, verdict, note) {
    var body = { issue: issue, decl: decl, verdict: verdict };
    if (typeof note === "string") body.note = note; // verbatim, no rewrite
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
  function postDeclLock(issue, decl, locked) {
    return postJSON("/api/decl-lock", { issue: issue, decl: decl, locked: !!locked });
  }

  // postShip — THE ONLY IRREVERSIBLE CALL. Composes the per-issue outcome
  // from the subdeck and either merges the PR (all verified) or dispatches
  // Aristotle with the composed note (any rejected). Reached ONLY from
  // shipIssue() (the explicit ship-button handler).
  //
  // The `skip_api` flag rides the ship body so the BACKEND realizes the
  // API auto-approval AT SHIP (folding un-verdicted API decls into KEEP). The
  // toggle itself never POSTs; auto-approval happens ONLY here, at the
  // explicit token-bearing ship — never on the GET that assembled the view.
  function postShip(issue, skipApi) {
    return postJSON("/api/ship", { issue: issue, skip_api: !!skipApi });
  }

  // postDropCitation — A GitHub WRITE (token-gated, HUMAN-TRIGGERED). Drops a
  // confirmed-dead stale citation (cited + gone-from-code + no rename) from the
  // issue body so the issue reviews cleanly. The backend re-validates the
  // safety gate before any write — a live decl / a rename target / an uncited
  // name is REFUSED ({ok:false}). Reached ONLY from the footnote's per-citation
  // "Drop citation" button.
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
    landedToday: 0,
    issue: null,        // the open subdeck's issue number (or null)
    subdeck: null,      // the GET /api/subdeck payload for `issue`
    currentDecl: null,  // the DeclCard currently on the stage (or null)
    showSummary: false, // the confirm-and-ship summary card is up
    busy: false,        // a POST is in flight (locks input)
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
    activeTab: "review", // review | files | graph | status | issues | history
    filesChapter: "",   // "" = server default, "all" = project-wide reader
    filesData: null,
    filesQuery: "",
    filesFilter: "all", // all | unreviewed | rejected | changed | surfaced
    filesFocus: null,
    filesResumeScroll: null,
    filesResumeName: null,
    filesBusy: false,
    graphData: null,
    graphFilter: { chapter: "all", status: "all", type: "all", query: "" },
    graphView: { zoom: 1 },
    definitionTerms: { names: [], short: {} },
    definitionNameSet: {},
    definitionSourceToken: null,
    definitionTermsRequest: null,
    definitionTermsRequestToken: null,
    definitionTermsQueued: null,
    definitionRequestSeq: 0,
    reconciliation: null,
    reconciliationBusy: false,
  };
  state.skipApi = readSkipApiPref();

  function repaintDefinitionSurfaces() {
    if (state.activeTab === "review" && state.subdeck &&
        !state.rejectComposerOpen && !state.busy) {
      // Preserve an explicitly revisited card instead of jumping back to the
      // first unreviewed declaration merely because highlighting data arrived.
      if (state.currentDecl) revisitDecl(state.currentDecl.name);
      else renderSubdeckStage();
    } else if (state.activeTab === "files" && state.filesData) {
      // Never rebuild over a draft that has not been POSTed yet.
      var openDraft = document.querySelector(
        '#tab-files .reader-reject-editor:not([hidden]) textarea'
      );
      if (!openDraft) paintFilesTab();
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
      if (force || (expectedToken && expectedToken !== state.definitionTermsRequestToken)) {
        state.definitionTermsQueued = {
          expectedToken: expectedToken,
          force: !!force,
        };
      }
      return state.definitionTermsRequest;
    }

    state.definitionTermsRequestToken = expectedToken;
    var flight = fetchDefinitionTerms()
      .then(function (terms) {
        installDefinitionTerms(terms);
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
  // (cited + gone-from-code + no rename) before any write, so a refusal
  // (rename available / live decl / uncited) surfaces here as a toast and
  // writes nothing. On success the issue body lost the dead citation, so we
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
               + "removed from the code (not renamed). The backend re-checks "
               + "before writing.",
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
          "automatically; a citation with no proven rename is kept (it may " +
          "signal genuinely-missing work). If the decl was deliberately " +
          "REMOVED from the code, drop its citation above." }),
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
    var onClose = host._marathonOnClose;
    host._marathonOnClose = null;
    host.setAttribute("hidden", "hidden");
    host.innerHTML = "";
    var card = document.getElementById("active-card");
    if (card) card.classList.remove("has-inspector");
    if (typeof onClose === "function") onClose();
  }

  function openReviewInspector(title, children, onClose) {
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
    if (card) card.classList.add("has-inspector");
    return host;
  }

  function inspectorCode(label, value) {
    var section = el("section", { class: "inspector-sec" }, [
      el("h3", { class: "inspector-label", text: label }),
    ]);
    if (value) {
      section.appendChild(el("pre", { class: "inspector-code mono" }, [value]));
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
    openDefinitionSurface("Definition", [
      meta,
      inspectorCode("Elaborated type", member.type_pp),
      inspectorCode("Definition body", member.value_pp),
      el("p", { class: "inspector-hint", text:
        "This is the audit snapshot's elaborated kernel entry. Click another highlighted term to inspect it." }),
    ]);
  }

  function openDefinitionSurface(title, children) {
    if (state.activeTab === "review" && inspectorHost()) {
      return openReviewInspector(title, children);
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

  function renderDefinitionMatch(match) {
    var location = match.source_file || "source unavailable";
    if (match.source_line != null) location += ":" + match.source_line;
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
    openDefinitionSurface("Definition", [
      el("p", { class: "inspector-meta" }, [
        match.kind ? el("span", { class: "decl-panel-kind", text: match.kind }) : null,
        tierBadge(match.tier || "-", match.qualifiers || []),
        el("span", { class: "mono", text: match.name }),
      ]),
      el("p", { class: "resource-file" }, [
        el("strong", { text: "Current source" }),
        el("span", { class: "mono", text: location }),
      ]),
      inspectorCode("Source block", match.source_block),
      inspectorCode("Elaborated type", match.statement_pp),
      inspectorCode("Definition body", match.value_pp),
      inspectorCode("Trust evidence", definitionEvidence(match)),
      owners,
    ]);
  }

  function showDefinitionLookup(name, fallbackMember) {
    var requestSeq = ++state.definitionRequestSeq;
    var issue = state.issue;
    var activeTab = state.activeTab;
    var declaration = activeTab === "review" && state.currentDecl
      ? state.currentDecl.name : null;
    function stillCurrent() {
      return requestSeq === state.definitionRequestSeq
        && issue === state.issue
        && activeTab === state.activeTab
        && (activeTab !== "review"
          || declaration === (state.currentDecl && state.currentDecl.name));
    }
    openDefinitionSurface("Definition lookup", [
      el("p", { class: "inspector-hint", text: "Resolving " + name + " against current project source…" }),
    ]);
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
    openReviewInspector("Dependency map", [intro, graph, external]);
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
    openReviewInspector("Open source & issue", [
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
    // carried-over verdict was dropped, so this is back for re-review.
    if (card.changed_since_review) {
      panel.appendChild(el("div", { class: "changed-banner", role: "alert" }, [
        el("span", { class: "changed-icon", text: "⟳" }),
        el("div", {}, [
          el("strong", { text: "Changed since your last approval" }),
          el("div", { class: "changed-sub", text:
            "you verified this before, but its source changed (Aristotle altered "
            + "it) — please re-review." }),
        ]),
      ]));
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
      panel.appendChild(
        el("section", { class: "sec-source" }, [
          el("h3", { class: "sec-title" }, titleKids),
          el("pre", { class: "source-block" }, [
            el("code", { class: "lean" }, leanHighlight(card.source_block, card.kernel)),
          ]),
        ])
      );
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
    state.busy = true;
    setActionsEnabled(false);
    animateOut(verdict);
    postDeclVerdict(issue, INFORMAL_KEY, verdict, note)
      .then(function (resp) {
        state.busy = false;
        var sd = state.subdeck;
        if (sd) {
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
  // is drained but rejected issues are STRANDED (their fix never landed, or their
  // tracking was reverted by a git reset — see dashboard.stuck_issues). Without
  // this they only appear in the Issues tab, which the swipe reviewer never
  // opens, so a rejection silently vanishes. Each row offers a one-click
  // re-queue; the note is re-issued (recovered from GitHub for an orphan).
  // One stuck row's status text + whether re-queue should confirm first. A
  // stuck issue with an OPEN fix PR is not "the fix never landed" — the
  // pipeline produced one and automerge is (correctly) holding it red; a
  // re-queue spends a fresh billed Aristotle run, so ask before firing.
  function stuckRowText(it) {
    if (it.fix_pr != null) {
      var held = it.fix_pr_build === "OK" ? " [build:OK — merging shortly]"
               : (it.fix_pr_build === "FAIL" || it.fix_pr_build === "TIMEOUT")
                   ? " [build:" + it.fix_pr_build + " — held by automerge]" : "";
      return "fix PR #" + it.fix_pr + " already open" + held;
    }
    return it.note_preview || "(rejected — no fix landed yet)";
  }

  function confirmRequeue(it) {
    if (it.fix_pr == null) return true;
    return window.confirm(
      "#" + it.num + " already has an open fix PR #" + it.fix_pr +
      (it.fix_pr_build ? " [build:" + it.fix_pr_build + "]" : "") +
      ".\nRe-queuing starts a NEW billed Aristotle run alongside it. Continue?"
    );
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
      kids.push(el("button", {
        class: "btn btn-tiny issue-requeue",
        type: "button",
        title: "Re-queue this rejection — the conductor retries it for Aristotle",
        onclick: (function (row) {
          return function (ev) {
            if (!confirmRequeue(row)) return;
            requeueIssue(row.num, ev.currentTarget, refreshQueue);
          };
        })(it),
      }, ["Re-queue"]));
      return el("div", { class: "issue-row stuck-row" }, kids);
    });
    var n = stuck.length;
    var card = el("article", { class: "card card-message" }, [
      el("h2", { class: "msg-title",
                 text: "⚠ " + n + " issue" + (n > 1 ? "s" : "") + " need attention" }),
      el("p", { class: "msg-body",
                text: "Rejected, but the fix never landed — the conductor can't "
                    + "reach them. Re-queue to retry, or open Issues for detail." }),
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

  // Open the subdeck for the first ready issue, or show "all caught up".
  function openFirstReadyIssue() {
    var ready = readyIssues();
    if (!ready.length) {
      state.issue = null;
      state.subdeck = null;
      state.currentDecl = null;
      showMessageCard({
        title: "All caught up",
        body:
          state.building > 0
            ? state.building + " issue(s) building — check back shortly."
            : "No ready issues in this view.",
        actions: [{ label: "Refresh", kind: "btn-primary", onClick: refreshQueue }],
      });
      // Upgrade to the attention card if rejected issues are stranded — else a
      // reviewer with an empty queue never learns they exist. Best-effort: a
      // fetch failure leaves the plain "all caught up" card. Guard on state.issue
      // so we never clobber a card the user has since navigated to.
      getJSON("/api/stuck")
        .then(function (d) {
          var stuck = (d && d.issues) || [];
          // Only paint if we're STILL on the drained Review stage — the fetch
          // is async, so the user may have opened an issue or switched tabs
          // (and re-queued from the Issues tab) meanwhile; don't render a stale
          // card into a backgrounded stage.
          if (stuck.length && state.issue == null && state.activeTab === "review") {
            showStuckAttentionCard(stuck, d.repo);
          }
        })
        .catch(function () {});
      return;
    }
    openIssue(ready[0].id);
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
    state.busy = true;
    postDeclLock(state.issue, name, locked)
      .then(function (resp) {
        if (resp && resp.summary && state.subdeck) state.subdeck.summary = resp.summary;
        // Reflect the new lock state on the local card copy so re-renders and
        // a later ship summary agree with the backend.
        var card = findDecl(name);
        if (card) card.locked = locked;
        if (state.currentDecl && state.currentDecl.name === name)
          state.currentDecl.locked = locked;
      })
      .catch(function () { /* toast already shown by postJSON */ })
      .then(function () {
        state.busy = false;
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
    state.busy = true;
    setActionsEnabled(false);
    animateOut(verdict);
    postDeclVerdict(issue, decl, verdict, note)
      .then(function (resp) {
        state.busy = false;
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

    stage().innerHTML = "";
    var card = el("article", { class: "card summary-card", id: "active-card" });
    card.appendChild(subdeckHeader());
    card.appendChild(
      el("h2", { class: "summary-title", text:
        (partialReject ? "Review paused at first rejection" : "Review complete") +
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

    // Ship action. Label depends on whether anything was rejected:
    //   nothing rejected -> "Mark issue verified" (merges the PR, no Aristotle)
    //   any rejected     -> "Ship to Aristotle" (dispatches with the note)
    var shipLabel = anyAristotle ? "Ship to Aristotle" : "Mark issue verified";
    var shipBtn = el("button", {
      class: "btn ship-btn " + (anyAristotle ? "btn-danger" : "btn-primary"),
      type: "button",
      id: "btn-ship",
      onclick: function () { confirmAndShip(anyAristotle); },
    }, [shipLabel]);
    card.appendChild(
      el("div", { class: "summary-actions" }, [
        el("button", { class: "btn", type: "button", onclick: function () {
          // On a partial reject, continue at the first untouched card only by
          // explicit choice; otherwise revisit the first card as before.
          var first = partialReject ? pendingCards[0] : (sd.cards || [])[0];
          if (first && findDecl(first.name)) revisitDecl(first.name);
          else if (partialReject && state.skipApi) setSkipApi(false);
        } }, [partialReject ? "Continue reviewing anyway" : "Revisit declarations"]),
        shipBtn,
      ])
    );
    card.appendChild(
      el("p", { class: "ship-warn", text:
        "This is the ONLY irreversible action — " +
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
        if (state.subdeck) state.subdeck.missing = (resp && resp.missing) || null;
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
          onclick: function () { closeModal(); shipIssue(); },
        }, ["Confirm — ship #" + state.issue]),
      ])
    );
    openModal(el("div", { class: "modal-panel" }, kids));
  }

  // shipIssue — THE SOLE CALLER OF postShip. This is the single irreversible
  // action in the whole app. Reached ONLY from the confirm dialog opened by
  // the ship button on the completion summary — never on load, navigation,
  // a decl-verdict, or status polling.
  function shipIssue() {
    if (state.busy || !state.issue) return;
    var issue = state.issue;
    // Capture the skip-API view state so the BACKEND realizes the API
    // auto-approval at ship (folding un-verdicted API decls into KEEP). This
    // is where auto-approval is REALIZED — never on the GET that built the
    // view. An overridden decl carries its own human verdict, so the backend
    // will not auto-approve it.
    var skipApi = !!state.skipApi;
    state.busy = true;
    setActionsEnabled(false);
    flashToast("shipping issue #" + issue + "…");
    postShip(issue, skipApi)
      .then(function (resp) {
        state.busy = false;
        var outcome = (resp && resp.outcome) || "shipped";
        var msg = (resp && resp.message) || ("issue #" + issue + " " + outcome);
        // The shipped issue left the ready queue.
        state.queue = state.queue.filter(function (c) { return c.id !== issue; });
        state.subdeckRequestSeq += 1;
        state.subdeck = null;
        state.issue = null;
        state.showSummary = false;
        state.filesData = null;
        state.graphData = null;
        flashToast(msg);
        var adv = resp && resp.advanced_to_issue;
        if (adv != null) {
          openIssue(adv);
        } else {
          // Refresh the queue; if empty -> "all caught up".
          refreshQueue();
        }
        refreshStatus();
      })
      .catch(function (err) {
        state.busy = false;
        // Nothing shipped on failure; return to the summary so the human can
        // retry or revisit.
        showErrorCard("Ship failed for issue #" + issue, err, function () {
          state.showSummary = true;
          renderSubdeckStage();
        });
      });
  }

  // --- modals --------------------------------------------------------------
  function isModalOpen() {
    return !modalRoot().hasAttribute("hidden");
  }
  function closeModal() {
    var root = modalRoot();
    var onClose = root._marathonOnClose;
    root._marathonOnClose = null;
    root.setAttribute("hidden", "hidden");
    root.innerHTML = "";
    if (typeof onClose === "function") onClose();
  }
  function openModal(node, onClose) {
    var root = modalRoot();
    root.innerHTML = "";
    root._marathonOnClose = onClose || null;
    var backdrop = el("div", { class: "modal-backdrop" }, [node]);
    backdrop.addEventListener("click", function (e) {
      if (e.target === backdrop) closeModal();
    });
    root.appendChild(backdrop);
    root.removeAttribute("hidden");
  }

  function openRejectNote() {
    var decl = state.currentDecl ? state.currentDecl.name : "";
    var ta = el("textarea", {
      class: "reject-note",
      rows: "5",
      placeholder: "Why is this declaration wrong? This note is composed into the issue's fix instructions for Aristotle.",
      "aria-label": "Rejection note (required, composed into the issue's Aristotle fix instructions verbatim)",
    });
    var err = el("div", { class: "modal-err", hidden: "hidden" });
    var form = el("form", { class: "reject-composer", "data-no-swipe": "true" }, [
      el("p", { class: "inspector-hint" }, [
        "Your note is ",
        el("strong", { text: "composed verbatim into the issue's fix instructions for Aristotle" }),
        " when you ship. A note is required. (Recording it now is LOCAL — nothing dispatches yet.)",
      ]),
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
    state.building = s.building || 0;
    state.landedToday = s.landed_today || 0;
    var b = document.getElementById("status-building");
    var l = document.getElementById("status-landed");
    if (b) b.textContent = state.building + " building";
    if (l) l.textContent = state.landedToday + " landed today";
    if (s.jobs && s.jobs.length) renderJobProgress(s.jobs);
    if (s.source_token) refreshDefinitionTerms(s.source_token, false);
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
  function refreshQueue() {
    setActionsEnabled(false);
    stage().innerHTML = "";
    stage().appendChild(el("div", { class: "card card-loading", text: "loading queue…" }));
    return fetchQueue()
      .then(function (data) {
        state.queue = (data.cards || []).slice();
        state.building = data.building || 0;
        state.landedToday = data.landed_today || 0;
        applyStatus({ building: state.building, landed_today: state.landedToday });
        // The queue is the issue PICKER: drop straight into the first ready
        // issue's subdeck (auto-pick). No verdict/ship here — pure reads only.
        openFirstReadyIssue();
      })
      .catch(function (err) {
        showErrorCard("Could not load the queue", err, refreshQueue);
      });
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
      // Tab switching (1-4) works from any tab; "?" help too.
      if (e.key === "1") return switchTab("review");
      if (e.key === "2") return switchTab("status");
      if (e.key === "3") return switchTab("issues");
      if (e.key === "4") return switchTab("history");
      if (e.key === "?") return openHelp();
      // The card actions (verify/reject/defer/deps/open) only fire on the
      // Review tab — they act on the card stage, which is hidden otherwise.
      if (state.activeTab !== "review") return;
      switch (e.key) {
        case "ArrowRight": e.preventDefault(); actVerify(); break;
        case "ArrowLeft": e.preventDefault(); actReject(); break;
        case " ": case "Spacebar": case "ArrowDown": e.preventDefault(); actDefer(); break;
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
        el("li", { text: "d — declaration dependency map + predecessor issues" }),
        el("li", { text: "o — source path + GitHub issue link" }),
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
      renderStatusTab();
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
    if (verdict !== "defer" && (target.blocked_by || []).length) {
      flashToast("Waiting on " + target.blocked_by.join(", ") + " — defer or repair it first");
      return;
    }
    state.filesBusy = true;
    if (button) button.disabled = true;
    postDeclVerdict(target.issue, target.name, verdict, note)
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
          body.appendChild(el("pre", { class: "source-block reader-full-source" }, [code]));
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
    var article = el("article", {
      class: "reader-decl reader-status-" + (target.verdict || "unreviewed") +
        (blocked ? " reader-blocked" : ""),
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
        tierBadge(target.tier, target.qualifiers),
      ]),
    ]));
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
      article.appendChild(el("pre", { class: "source-block reader-source" }, [code]));
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
      disabled: blocked ? "disabled" : null,
      title: blocked ? "Fix the rejected predecessor first" : "Keep this declaration (local only)",
    }, [target.verdict === "verified" ? "✓ Kept" : "Keep"]);
    var reject = el("button", {
      class: "btn reader-fix",
      type: "button",
      disabled: blocked ? "disabled" : null,
      title: blocked ? "Fix the rejected predecessor first" : "Write a FIX note beside the source",
    }, [target.verdict === "rejected" ? "✗ Edit FIX" : "Fix…"]);
    var defer = el("button", { class: "btn", type: "button" }, [
      target.verdict === "deferred" ? "↺ Deferred" : "Defer",
    ]);
    var focused = el("button", { class: "btn btn-tiny", type: "button" }, ["Focused review"]);
    keep.addEventListener("click", function () { readerVerdict(target, "verify", null, keep); });
    reject.addEventListener("click", function () {
      if (editor.panel.hasAttribute("hidden")) editor.panel.removeAttribute("hidden");
      else editor.panel.setAttribute("hidden", "hidden");
      if (!editor.panel.hasAttribute("hidden")) editor.note.focus();
    });
    defer.addEventListener("click", function () { readerVerdict(target, "defer", null, defer); });
    focused.addEventListener("click", function () {
      state.filesResumeScroll = window.scrollY || window.pageYOffset || 0;
      state.filesResumeName = target.name;
      switchTab("review");
      openIssue(target.issue);
    });
    article.appendChild(el("div", { class: "reader-actions" }, [keep, reject, defer, focused]));
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
      "This records local review state only. It does not SHIP any issue."
    )) return;
    state.filesBusy = true;
    var items = targets.map(function (target) {
      return { issue: target.issue, decl: target.name, verdict: verdict };
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
    search.addEventListener("input", function () {
      state.filesQuery = search.value;
      paintFilesTab();
      var replacement = dashHost("dash-files").querySelector(".reader-search");
      if (replacement) {
        replacement.focus();
        replacement.setSelectionRange(replacement.value.length, replacement.value.length);
      }
    });
    var targetFilter = el("select", {
      class: "reader-select", "aria-label": "Declaration status filter",
    }, [
      el("option", { value: "all", text: "All targets" }),
      el("option", { value: "unreviewed", text: "Unreviewed" }),
      el("option", { value: "rejected", text: "Rejected" }),
      el("option", { value: "changed", text: "Changed" }),
      el("option", { value: "surfaced", text: "Surfaced" }),
    ]);
    targetFilter.value = state.filesFilter;
    targetFilter.addEventListener("change", function () {
      state.filesFilter = targetFilter.value;
      paintFilesTab();
    });
    var refresh = el("button", { class: "btn", type: "button" }, ["Refresh"]);
    refresh.addEventListener("click", function () {
      state.filesData = null;
      renderFilesTab();
    });
    host.appendChild(el("div", { class: "reader-header" }, [
      el("div", {}, [
        el("h2", { text: data.chapter == null ? "Project file review" : "Chapter " + data.chapter + " file review" }),
        el("p", { class: "reader-help", text:
          "Read declarations in source order, keep notes beside the code, and batch only deliberate local verdicts." }),
      ]),
      el("div", { class: "reader-toolbar" }, [chapterSelect, targetFilter, search, refresh]),
    ]));

    var progress = data.progress || {};
    host.appendChild(el("div", { class: "stat-row reader-stats" }, [
      statChip(progress.reviewed || 0, "reviewed"),
      statChip(progress.total || 0, "targets"),
      statChip(progress.verified || 0, "kept"),
      statChip(progress.rejected || 0, "fix"),
      statChip((data.files || []).length, "files"),
    ]));

    var visible = readerVisibleTargets(data);
    var deferrable = visible.filter(function (target) { return !target.verdict; });
    var deferAll = el("button", {
      class: "btn", type: "button",
      disabled: deferrable.length ? null : "disabled",
    }, ["Defer visible unreviewed (" + deferrable.length + ")"]);
    deferAll.addEventListener("click", function () { readerBatch("defer", deferrable); });
    host.appendChild(el("div", { class: "reader-batch" }, [
      el("span", { class: "reader-batch-note", text:
        "Batch DEFER is a non-decision and never ships. KEEP/FIX remain explicit per declaration." }),
      deferAll,
    ]));

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
      host.appendChild(el("div", { class: "dash-empty", text:
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
    host.appendChild(nav);

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
    host.appendChild(body);

    if ((data.errors || []).length) {
      host.appendChild(el("div", { class: "reader-errors", text:
        data.errors.length + " issue payload" + (data.errors.length === 1 ? "" : "s") +
        " could not be assembled; the rest of the chapter is still shown." }));
    }
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

  // --- Graph tab: project-wide issue/declaration dependency DAG -----------

  function graphNodeMatches(node, filter) {
    if (filter.chapter !== "all") {
      var chapters = node.chapters || (node.chapter != null ? [node.chapter] : []);
      if (chapters.map(String).indexOf(String(filter.chapter)) < 0) return false;
    }
    if (filter.status !== "all" && node.status !== filter.status) return false;
    if (filter.type !== "all" && node.type !== filter.type) return false;
    var query = String(filter.query || "").trim().toLowerCase();
    if (query) {
      var haystack = [node.label, node.title, node.source_file, node.issue,
        (node.issues || []).join(" ")].join(" ").toLowerCase();
      if (haystack.indexOf(query) < 0) return false;
    }
    return true;
  }

  function graphLayout(nodes, edges) {
    var byId = {};
    var indegree = {};
    var outgoing = {};
    var rank = {};
    nodes.forEach(function (node) {
      byId[node.id] = node;
      indegree[node.id] = 0;
      outgoing[node.id] = [];
      rank[node.id] = 0;
    });
    edges.forEach(function (edge) {
      if (!byId[edge.source] || !byId[edge.target]) return;
      outgoing[edge.source].push(edge.target);
      indegree[edge.target] += 1;
    });
    var ready = nodes.filter(function (node) { return indegree[node.id] === 0; });
    ready.sort(function (a, b) { return a.label.localeCompare(b.label); });
    var placed = {};
    while (ready.length) {
      var node = ready.shift();
      if (placed[node.id]) continue;
      placed[node.id] = true;
      outgoing[node.id].forEach(function (target) {
        rank[target] = Math.max(rank[target], rank[node.id] + 1);
        indegree[target] -= 1;
        if (indegree[target] === 0) ready.push(byId[target]);
      });
      ready.sort(function (a, b) { return a.label.localeCompare(b.label); });
    }
    // A malformed/cyclic dependency snapshot must remain explorable. Put the
    // cyclic remainder in a final layer; the backend's issue gating already
    // removes cyclic issue edges, so this is normally declaration-only.
    var maxRank = 0;
    Object.keys(rank).forEach(function (id) { maxRank = Math.max(maxRank, rank[id]); });
    nodes.forEach(function (node) {
      if (!placed[node.id]) rank[node.id] = maxRank + 1;
    });
    var layers = {};
    nodes.forEach(function (node) {
      var r = rank[node.id];
      if (!layers[r]) layers[r] = [];
      layers[r].push(node);
    });
    Object.keys(layers).forEach(function (key) {
      layers[key].sort(function (a, b) {
        var ach = (a.chapters || [a.chapter == null ? 9999 : a.chapter])[0];
        var bch = (b.chapters || [b.chapter == null ? 9999 : b.chapter])[0];
        return ach - bch || a.type.localeCompare(b.type) || a.label.localeCompare(b.label);
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
    };
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

  function renderGraphCanvas(host, data, nodes, edges) {
    var capped = nodes.length > 220;
    if (capped) nodes = nodes.slice(0, 220);
    var included = {};
    nodes.forEach(function (node) { included[node.id] = true; });
    edges = edges.filter(function (edge) {
      return included[edge.source] && included[edge.target];
    });
    if (!nodes.length) {
      host.appendChild(el("div", { class: "dash-empty", text: "No graph nodes match these filters." }));
      return;
    }
    if (capped) {
      host.appendChild(el("div", { class: "graph-cap", text:
        "Showing the first 220 matching nodes. Narrow by chapter, status, type, or search to inspect the rest." }));
    }
    var layout = graphLayout(nodes, edges);
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
      "aria-label": "Project issue and declaration dependency graph",
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
          (edge.cyclic ? " graph-edge-cyclic" : ""),
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

    var chapter = el("select", { class: "reader-select", "aria-label": "Graph chapter" });
    chapter.appendChild(el("option", { value: "all", text: "All chapters" }));
    (data.chapters || []).forEach(function (number) {
      chapter.appendChild(el("option", { value: String(number), text: "Chapter " + number }));
    });
    chapter.value = filter.chapter;
    var status = el("select", { class: "reader-select", "aria-label": "Graph status" });
    status.appendChild(el("option", { value: "all", text: "All statuses" }));
    var statuses = {};
    (data.nodes || []).forEach(function (node) { statuses[node.status] = true; });
    Object.keys(statuses).sort().forEach(function (name) {
      status.appendChild(el("option", { value: name, text: name }));
    });
    status.value = filter.status;
    var type = el("select", { class: "reader-select", "aria-label": "Graph node type" }, [
      el("option", { value: "all", text: "Issues + declarations" }),
      el("option", { value: "issue", text: "Issues only" }),
      el("option", { value: "declaration", text: "Declarations only" }),
    ]);
    type.value = filter.type;
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
    type.addEventListener("change", function () { filter.type = type.value; paintGraphTab(); });
    search.addEventListener("input", function () {
      filter.query = search.value;
      paintGraphTab();
      var replacement = dashHost("dash-graph").querySelector(".reader-search");
      if (replacement) {
        replacement.focus();
        replacement.setSelectionRange(replacement.value.length, replacement.value.length);
      }
    });
    refresh.addEventListener("click", function () {
      state.graphData = null;
      renderGraphTab();
    });
    zoomOut.addEventListener("click", function () { graphChangeZoom(-0.2); });
    zoomIn.addEventListener("click", function () { graphChangeZoom(0.2); });
    resetView.addEventListener("click", function () {
      state.graphView.zoom = 1;
      graphApplyZoom(true);
    });
    host.appendChild(el("div", { class: "reader-header graph-header" }, [
      el("div", {}, [
        el("h2", { text: "Project dependency DAG" }),
        el("p", { class: "reader-help", text:
          "Edges point from predecessor to dependent. Drag or scroll to pan; click a node to expand relationships." }),
      ]),
      el("div", { class: "reader-toolbar graph-toolbar" }, [
        chapter, status, type, search, zoomOut, zoomValue, resetView, zoomIn, refresh,
      ]),
    ]));

    if (data.audit && data.audit.status !== "fresh") {
      host.appendChild(el("div", { class: "reader-errors graph-audit-warning", text:
        data.audit.status === "missing"
          ? "No audit snapshot is available; build/tier evidence is shown as unknown."
          : "Audit evidence is " + data.audit.status +
            "; stale or unavailable build evidence is never presented as current." }));
    }

    var nodes = (data.nodes || []).filter(function (node) { return graphNodeMatches(node, filter); });
    var visibleIds = {};
    nodes.forEach(function (node) { visibleIds[node.id] = true; });
    var edges = (data.edges || []).filter(function (edge) {
      return visibleIds[edge.source] && visibleIds[edge.target];
    });
    host.appendChild(el("div", { class: "stat-row graph-stats" }, [
      statChip(nodes.length, "visible nodes"),
      statChip(edges.length, "visible edges"),
      statChip((data.counts && data.counts.by_type && data.counts.by_type.issue) || 0, "issues total"),
      statChip((data.counts && data.counts.by_type && data.counts.by_type.declaration) || 0, "declarations total"),
      statChip((data.counts && data.counts.impacted) || 0, "downstream impact"),
    ]));
    host.appendChild(el("div", { class: "graph-legend" }, [
      el("span", { class: "graph-key graph-key-issue", text: "issue" }),
      el("span", { class: "graph-key graph-key-decl", text: "declaration" }),
      el("span", { class: "graph-key graph-key-dependency", text: "dependency" }),
      el("span", { class: "graph-key graph-key-contains", text: "reviews / contains" }),
      el("span", { class: "graph-key graph-key-changed", text: "changed" }),
      el("span", { class: "graph-key graph-key-impact", text: "changed downstream" }),
      el("span", { class: "graph-key graph-key-warning", text:
        ((data.counts && data.counts.unresolved_edges) || 0) + " unresolved · " +
        ((data.counts && data.counts.cyclic_edges) || 0) + " cyclic" }),
    ]));
    renderGraphCanvas(host, data, nodes, edges);
    if ((data.errors || []).length) {
      host.appendChild(el("div", { class: "reader-errors", text:
        data.errors.length + " issue payload" + (data.errors.length === 1 ? "" : "s") +
        " could not be included; the remaining project graph is still navigable." }));
    }
  }

  function renderGraphTab() {
    if (state.activeTab !== "graph") return;
    if (state.graphData) return paintGraphTab();
    var host = dashHost("dash-graph");
    if (!host) return;
    host.innerHTML = "";
    host.appendChild(el("div", { class: "dash-loading", text: "building project dependency DAG…" }));
    fetchProjectGraph()
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
        .then(function () { renderStatusTab(); })
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

  function renderStatusTab() {
    if (state.activeTab !== "status") return; // a stale poll after a switch
    getJSON("/api/conductor-status").then(function (d) {
      if (state.activeTab !== "status") return;
      var host = dashHost("dash-status");
      if (!host) return;
      host.innerHTML = "";
      var up = !!d.alive;
      // DEGRADED beats "running": a conductor with a broken dispatch env
      // (e.g. no Aristotle key) is alive but can't refine — a green chip
      // here hid exactly that during the 2026-07-07 incident.
      var degraded = up && d.degraded;
      host.appendChild(el("div", { class: "cond-banner" + (degraded ? " cond-degraded" : "") }, [
        el("span", { class: "cond-dot " + (degraded ? "degraded" : (up ? "up" : "down")) }),
        el("div", {}, [
          el("div", { class: "cond-title",
            text: degraded ? "conductor DEGRADED — not dispatching"
                : up ? "conductor running" : "conductor stopped" }),
          el("div", { class: "dash-empty",
            text: degraded ? String(d.degraded)
                : up ? "dispatches refines + auto-merges PRs across all chapters"
                     : "no live orchestrator — rejecting an issue starts one" }),
        ]),
        el("div", { class: "cond-meta" }, [
          el("div", {}, ["pid ", el("span", { class: "mono",
            text: d.pid == null ? "—" : String(d.pid) })]),
          el("div", { text: "concurrency " + (d.concurrency == null ? "—" : d.concurrency) }),
          el("div", { text: d.updated_ts ? ("updated " + fmtTs(d.updated_ts)) : "" }),
          proverToggle(d),
        ]),
      ]));
      host.appendChild(el("div", { class: "stat-row dash-section" }, [
        statChip(d.running || 0, "running"),
        statChip((d.queued || []).length, "queued"),
        statChip((d.jobs || []).length, "jobs tracked"),
        statChip((d.landings || []).length, "recent landings"),
      ]));
      var jobsSec = el("section", { class: "dash-section" },
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
          title: "Open this run's ratings + prompts",
          onclick: (function (issue) {
            return function () { if (issue != null) openRunDetail(issue); };
          })(j.issue_num),
        }, kids);
        jobsSec.appendChild(row);
      });
      host.appendChild(jobsSec);
      // Queued (rejected, not yet a running job) — the "I shipped a reject,
      // where is it?" gap. Single-flight + same-chapter means a reject waits
      // behind a running same-chapter job; show it here with the reason.
      var queued = d.queued || [];
      var qSec = el("section", { class: "dash-section" },
        [el("div", { class: "dash-h", text: "Queued (waiting to dispatch)" })]);
      if (!queued.length) {
        qSec.appendChild(el("div", { class: "dash-empty", text: "nothing queued" }));
      }
      queued.forEach(function (it) {
        qSec.appendChild(el("div", { class: "job-row queued-row" }, [
          el("span", { class: "job-issue", text: "#" + (it.issue_num == null ? "?" : it.issue_num) }),
          el("span", { class: "job-chip", text: "c" + (it.chapter == null ? "?" : it.chapter) }),
          el("span", { class: "sb sb-queued", text: "queued" }),
          el("span", { class: "job-wait", text: it.reason || "queued" }),
        ]));
      });
      host.appendChild(qSec);
      var landSec = el("section", { class: "dash-section" },
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
        landSec.appendChild(el("div", { class: "land-row" }, kids));
      });
      host.appendChild(landSec);
    }).catch(function (e) { dashError("dash-status", e); });
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
  function postRequeue(issue) {
    return postJSON("/api/requeue", { issue: issue });
  }

  // Re-issue a parked issue's stored rejection note (one-click) — mirrors the
  // dropCitation token-POST pattern: disable + spinner, POST, toast + re-render
  // on success, toast + re-enable on failure. `after` runs on success (default:
  // re-render the Issues tab); the swipe attention card passes refreshQueue.
  function requeueIssue(issue, btn, after) {
    if (btn) { btn.disabled = true; btn.textContent = "re-queuing…"; }
    postRequeue(issue)
      .then(function (res) {
        if (res && res.requeued) {
          flashToast("Re-queued #" + issue + " — the conductor will retry it");
          (after || renderIssuesTab)();
        } else {
          flashToast((res && res.message) || "Not re-queued");
          if (btn) { btn.disabled = false; btn.textContent = "Re-queue"; }
        }
      })
      .catch(function (err) {
        flashToast(err && err.message ? err.message : "Re-queue failed");
        if (btn) { btn.disabled = false; btn.textContent = "Re-queue"; }
      });
  }

  // The "Stalled — needs re-reject" surface: issues the conductor parked (status
  // 'stalled', or 'rejected' with no pending iteration) — NOT in the swipe queue,
  // so otherwise unreachable without the CLI. Each gets a one-click Re-queue.
  // Best-effort: a fetch failure never breaks the Issues tab.
  function renderStuckSection(host, repo) {
    getJSON("/api/stuck").then(function (d) {
      if (state.activeTab !== "issues") return;
      var stuck = (d && d.issues) || [];
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
        kids.push(el("button", {
          class: "btn btn-tiny issue-requeue",
          type: "button",
          title: it.has_note
            ? "Re-issue this rejection's stored note — re-queues it for Aristotle"
            : "No stored note to re-queue — use the CLI",
          onclick: (function (row) {
            return function (ev) {
              if (!confirmRequeue(row)) return;
              requeueIssue(row.num, ev.currentTarget);
            };
          })(it),
        }, ["Re-queue"]));
        return el("div", { class: "issue-row stuck-row" }, kids);
      });
      var section = el("section", { class: "chap-block stuck-block" }, [
        el("div", { class: "chap-title" }, [
          "Stalled — needs re-reject",
          el("span", { class: "chap-count", text: stuck.length + " parked" }),
        ]),
      ].concat(rows));
      host.insertBefore(section, host.firstChild);
    }).catch(function () { /* best-effort surface */ });
  }

  function renderIssuesTab() {
    var host = dashHost("dash-issues");
    if (!host) return;
    host.innerHTML = "";
    host.appendChild(el("div", { class: "dash-loading", text: "loading issues…" }));
    getJSON("/api/issues-overview").then(function (d) {
      if (state.activeTab !== "issues") return;
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
      renderStuckSection(host, d.repo);
    }).catch(function (e) { dashError("dash-issues", e); });
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
    // Prime the compact current-source identifier index before rendering code,
    // so project terms (including ambiguous short names) are clickable on the
    // first card. Lookup failure never blocks the review deck; kernel links
    // remain available as the honest fallback.
    refreshQueue();   // pure read — opens the first ready issue's subdeck immediately
    refreshDefinitionTerms(null, true);
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
    reconciliationProposalPanel: reconciliationProposalPanel,
    readerTargetMatches: readerTargetMatches,
    graphLayout: graphLayout,
    switchTab: switchTab,   // dashboard tab navigation (pure read)
    _state: state,
  };
})();
