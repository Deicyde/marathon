/*
 * app.js — the marathon deck dashboard + swipe review UI (vanilla JS, no build).
 *
 * SUBDECK FLOW (the shape this file implements):
 *   The ready QUEUE is the ISSUE picker. Picking a ready issue opens its
 *   SUBDECK — one DECLARATION card at a time. You swipe each declaration
 *   (verify / reject / defer) which records a LOCAL per-decl verdict only.
 *   When every declaration has been reviewed, a confirm-and-SHIP summary
 *   card appears; shipping composes the whole issue's outcome and is the
 *   single irreversible action.
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

  // --- network: the token-bearing POST primitive ---------------------------
  // postJSON attaches the per-session token (header + body fallback) and
  // unwraps the {ok:false,...} convention. It is the shared transport for
  // BOTH the local decl-verdict and the irreversible ship — but note the
  // two have very different consequences (see the two named callers below).
  function postJSON(url, body) {
    if (!SESSION_TOKEN) {
      return Promise.reject(
        new Error("no session token — refusing to POST")
      );
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
        if (!r.ok || (data && data.ok === false)) {
          throw new Error(
            (data && (data.message || data.reason || data.error)) ||
              "request failed (" + r.status + ")"
          );
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
    activeTab: "review", // dashboard tab: review | status | issues | history
  };
  state.skipApi = readSkipApiPref();

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
  function informalBlock(card) {
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
    var txt = card.informal_rendering;
    if (!txt) {
      wrap.appendChild(
        el("p", { class: "informal-empty", text: "(no informal rendering available)" })
      );
      return wrap;
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
  function leanHighlight(src) {
    var nodes = [];
    var last = 0;
    var m;
    LEAN_RE.lastIndex = 0;
    while ((m = LEAN_RE.exec(src))) {
      if (m.index > last) nodes.push(document.createTextNode(src.slice(last, m.index)));
      if (m[1] || m[2]) nodes.push(leanSpan("lh-comment", m[1] || m[2]));
      else if (m[3]) nodes.push(leanSpan("lh-string", m[3]));
      else if (m[4]) nodes.push(leanSpan("lh-sorry", m[4]));
      else if (m[5]) nodes.push(leanSpan("lh-kw", m[5]));
      last = LEAN_RE.lastIndex;
      if (LEAN_RE.lastIndex === m.index) LEAN_RE.lastIndex++;  // zero-width guard
    }
    if (last < src.length) nodes.push(document.createTextNode(src.slice(last)));
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
    var panel = el("section", { class: "api-panel", "aria-label": "API declarations to auto-approve" });
    var head = el("button", {
      class: "api-panel-head",
      type: "button",
      "aria-expanded": state.apiPanelOpen ? "true" : "false",
    }, [
      el("span", { class: "api-panel-caret", text: state.apiPanelOpen ? "▾" : "▸" }),
      el("span", { class: "api-panel-title", text:
        n + " API declaration" + (n === 1 ? "" : "s") + " will be auto-approved on ship" }),
    ]);
    head.addEventListener("click", function () {
      state.apiPanelOpen = !state.apiPanelOpen;
      // Pure re-render of the same payload (no fetch, no POST).
      renderSubdeckStage();
    });
    panel.appendChild(head);

    if (state.apiPanelOpen) {
      panel.appendChild(
        el("p", { class: "api-panel-note" }, [
          "Skip-API ",
          el("strong", { text: "auto-approves these declarations' STATEMENTS" }),
          " — they are folded into KEEP on ship without individual review. " +
          "Pull any one back to review it as a normal card.",
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
    overrideSet()[name] = true;
    // Pull the served auto_approved card into the visible deck immediately so
    // the human can act without waiting on a round trip; a refetch would also
    // surface it among `cards` (the backend honours overrides), but doing it
    // locally keeps the view responsive and avoids any extra request.
    var sd = state.subdeck;
    if (sd && sd.auto_approved) {
      for (var i = 0; i < sd.auto_approved.length; i++) {
        if (sd.auto_approved[i].name === name) {
          sd.cards = (sd.cards || []).concat([sd.auto_approved[i]]);
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
  function staleFootnote() {
    var stale = staleCitations();
    var n = stale.length;
    if (!n) return null;
    var names = stale.map(function (s) {
      return (s && s.name) || String(s);
    });
    var foot = el("footer", {
      class: "stale-footnote",
      role: "note",
      "aria-label": "stale citations in the issue body",
    }, [
      el("span", { class: "stale-foot-icon", text: "⚠" }),
      el("div", { class: "stale-foot-text" }, [
        el("span", { class: "stale-foot-lead", text:
          n + " citation" + (n === 1 ? "" : "s") +
          " in the issue body not found in the current code: " }),
        el("span", { class: "stale-foot-names mono", text: names.join(", ") }),
        el("span", { class: "stale-foot-tail", text:
          " — the issue is stale. These are NOT review cards. The conductor " +
          "reconciles renames automatically; a citation with no proven rename " +
          "is kept (it may signal genuinely-missing work)." }),
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

  // Render the current declaration card. Pure read render (no side effects).
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
            el("code", { class: "lean" }, leanHighlight(card.source_block)),
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
          el("details", { class: "elab-type" }, [
            el("summary", { text: "Elaborated type (what is actually proven)" }),
            el("pre", { class: "mono statement-pp" }, [card.statement_pp]),
          ]),
        ])
      );
    }

    panel.appendChild(el("section", { class: "sec-informal" }, [informalBlock(card)]));
    panel.appendChild(
      el("section", { class: "sec-evidence" }, [
        el("h3", { class: "sec-title", text: "Evidence" }),
        evidenceRow(card.evidence),
      ])
    );

    article.appendChild(panel);
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
      return;
    }
    openIssue(ready[0].id);
  }

  // Pure READ: load the subdeck for `issue` and render its first card. Passes
  // the current skip-API view preference so the backend returns ONLY the
  // main-result cards (plus the auto_approved list) when it is ON. This is a
  // GET — it writes nothing and fires no verdict.
  function openIssue(issue) {
    if (issue !== state.issue) state.apiPanelOpen = false; // fresh per issue
    state.issue = issue;
    state.showSummary = false;
    // A genuinely fresh issue: re-offer its informal-approval card (a prior
    // session's "decide later" dismissal does not carry across issues).
    state.informalDismissed = false;
    state.informalActive = false;
    setActionsEnabled(false);
    stage().innerHTML = "";
    stage().appendChild(el("div", { class: "card card-loading", text: "loading declarations…" }));
    fetchSubdeck(issue, state.skipApi)
      .then(function (sd) {
        state.subdeck = sd;
        renderSubdeckStage();
      })
      .catch(function (err) {
        showErrorCard("Could not load issue #" + issue, err, function () {
          openIssue(issue);
        });
      });
  }

  // Re-fetch the CURRENT issue's subdeck with the current skip-API preference
  // — a PURE READ used when the human flips the Skip-API toggle. It changes
  // only WHICH cards are shown; it POSTs nothing and ships nothing.
  function refetchSubdeck() {
    var issue = state.issue;
    if (issue == null) return;
    setActionsEnabled(false);
    stage().innerHTML = "";
    stage().appendChild(el("div", { class: "card card-loading", text: "loading declarations…" }));
    fetchSubdeck(issue, state.skipApi)
      .then(function (sd) {
        state.subdeck = sd;
        state.showSummary = false;
        renderSubdeckStage();
      })
      .catch(function (err) {
        showErrorCard("Could not load issue #" + issue, err, refetchSubdeck);
      });
  }

  function backToQueue() {
    state.issue = null;
    state.subdeck = null;
    state.currentDecl = null;
    state.showSummary = false;
    state.apiPanelOpen = false;
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

  // === per-declaration verdict flow (LOCAL ONLY) ===========================

  // The entry points to a LOCAL per-decl verdict. Each is a deliberate user
  // action (button/key/swipe). verify/defer commit immediately; reject opens
  // the required note modal. NONE of these ships anything.
  function actVerify() {
    if (state.informalActive) return approveInformal();
    if (!canAct()) return;
    commitDeclVerdict("verify", undefined);
  }
  function actReject() {
    if (state.informalActive) return openInformalRejectNote();
    if (!canAct()) return;
    openRejectNote();
  }
  function actDefer() {
    if (state.informalActive) return dismissInformal();
    if (!canAct()) return;
    commitDeclVerdict("defer", undefined);
  }

  function canAct() {
    return !!state.currentDecl && !state.busy && !isModalOpen() && !state.showSummary;
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
    if (state.skipApi) {
      // Main-scoped recompute: a card counts as reviewed iff verified/rejected
      // (deferred does NOT count — it leaves the subdeck incomplete, matching
      // the backend's REVIEWED_DECL_VERDICTS).
      recomputeMainProgress(sd);
    } else {
      if (resp && resp.progress) sd.progress = resp.progress;
      if (resp && typeof resp.complete === "boolean") sd.complete = resp.complete;
    }
    if (resp && resp.summary) sd.summary = resp.summary;
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

  // The API decls that WILL be auto-approved at ship under skip-API: the
  // backend's auto_approved list MINUS any the human pulled back into review
  // (those moved into the visible deck and are reviewed normally). Pure read.
  function autoApprovedForShip() {
    var sd = state.subdeck;
    if (!sd || !state.skipApi) return [];
    return (sd.auto_approved || []).filter(function (c) {
      return !isOverridden(c.name);
    });
  }

  function renderSummaryCard() {
    var sd = state.subdeck;
    var summary = (sd && sd.summary) || deriveSummary();
    var kept = summary.kept || [];
    var rejected = summary.rejected || [];
    var deferred = summary.deferred || [];
    var anyRejected = rejected.length > 0;
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
      el("h2", { class: "summary-title", text: "Review complete — confirm & ship issue #" + sd.issue })
    );

    // KEEP list (verified decls — kept exactly as written). Under skip-API the
    // auto-approved API decls are appended here, clearly marked, since they
    // will be folded into KEEP at ship without individual review.
    var keepCount = kept.length + autoApproved.length;
    var keepSec = el("section", { class: "summary-sec summary-keep" }, [
      el("h3", { class: "sec-title", text: "KEEP (" + keepCount + ") — verified, unchanged" }),
    ]);
    if (keepCount) {
      var keepList = el("ul", { class: "summary-list" });
      kept.forEach(function (name) {
        keepList.appendChild(el("li", { class: "summary-row keep-row" }, [
          el("span", { class: "mono", text: name }),
          el("button", { class: "btn btn-tiny revisit-decl", type: "button",
            onclick: (function (n) { return function () { revisitDecl(n); }; })(name) }, ["revisit"]),
        ]));
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

    if (deferred.length) {
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
          // revisit: drop back into the deck at the first card to re-review.
          var first = (sd.cards || [])[0];
          if (first) revisitDecl(first.name);
        } }, ["Revisit declarations"]),
        shipBtn,
      ])
    );
    card.appendChild(
      el("p", { class: "ship-warn", text:
        "This is the ONLY irreversible action — " +
        (anyAristotle
          ? "it dispatches Aristotle with your fix/add notes verbatim."
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
        state.subdeck = null;
        state.issue = null;
        state.showSummary = false;
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
    root.setAttribute("hidden", "hidden");
    root.innerHTML = "";
  }
  function openModal(node, onEscape) {
    var root = modalRoot();
    root.innerHTML = "";
    var backdrop = el("div", { class: "modal-backdrop" }, [node]);
    backdrop.addEventListener("click", function (e) {
      if (e.target === backdrop) { closeModal(); if (onEscape) onEscape(); }
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
    var panel = el("div", { class: "modal-panel" }, [
      el("h2", { class: "modal-title", text: "Reject " + decl }),
      el("p", { class: "modal-sub" }, [
        "Your note is ",
        el("strong", { text: "composed verbatim into the issue's fix instructions for Aristotle" }),
        " when you ship. A note is required. (Recording it now is LOCAL — nothing dispatches yet.)",
      ]),
      ta,
      err,
      el("div", { class: "modal-actions" }, [
        el("button", { class: "btn", type: "button", onclick: function () { closeModal(); } }, ["Cancel"]),
        el("button", {
          class: "btn btn-danger",
          type: "button",
          onclick: function () {
            var note = ta.value.trim();
            if (!note) {
              err.textContent = "A rejection note is required (it is composed into the Aristotle fix instructions).";
              err.removeAttribute("hidden");
              ta.focus();
              return;
            }
            closeModal();
            commitDeclVerdict("reject", note); // verbatim, no client rewrite
          },
        }, ["Reject declaration"]),
      ]),
    ]);
    openModal(panel);
    setTimeout(function () { ta.focus(); }, 0);
  }

  function openDeps() {
    // Dependencies are shown at the issue level; the subdeck header already
    // carries the parent issue. Here we surface the per-issue dep refs if the
    // subdeck payload included them (best-effort — no extra fetch).
    var deps = (state.subdeck && state.subdeck.deps) || [];
    var list = el("ul", { class: "deps-list" });
    if (!deps.length) {
      list.appendChild(el("li", { class: "deps-empty", text: "no dependency predecessors (review predecessors first if any)" }));
    } else {
      deps.forEach(function (d) {
        list.appendChild(
          el("li", { class: "dep-row" }, [
            tierBadge(d.tier),
            el("span", { class: "dep-decl mono", text: d.decl }),
            el("span", { class: "dep-id", text: "#" + d.id }),
          ])
        );
      });
    }
    var panel = el("div", { class: "modal-panel" }, [
      el("h2", { class: "modal-title", text: "Dependencies — review predecessors first" }),
      list,
      el("div", { class: "modal-actions" }, [
        el("button", { class: "btn", type: "button", onclick: function () { closeModal(); } }, ["Close"]),
      ]),
    ]);
    openModal(panel);
  }

  function openDeepDive() {
    var url = state.subdeck && state.subdeck.permalink;
    if (url) {
      // Opening the GitHub permalink / issue is a pure navigation, NOT a ship.
      window.open(url, "_blank", "noopener");
      flashToast("opened deep-dive (#" + state.issue + ")");
    } else {
      flashToast("no deep-dive link for this issue");
    }
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
      if (b) b.disabled = !on;
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
      if (e.target.closest("button, a, textarea, select")) return;
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
        el("li", { text: "d — dependencies (review predecessors first)" }),
        el("li", { text: "o — open deep-dive (GitHub permalink)" }),
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
        "Reconciliation (automatic, read-only): citations the issue body makes " +
        "that aren't in the current code are dropped into a footnote; decls " +
        "found in the code but not cited are surfaced as cards. " +
        "“Refresh issue body from code” (header) is the only way to push that " +
        "drift back to GitHub — it EDITS the issue body and asks to confirm." }),
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
    state.activeTab = name;
    var btns = document.querySelectorAll(".tab-btn");
    for (var i = 0; i < btns.length; i++) {
      var on = btns[i].getAttribute("data-tab") === name;
      btns[i].classList.toggle("active", on);
      btns[i].setAttribute("aria-selected", on ? "true" : "false");
    }
    ["review", "status", "issues", "history", "add-chapter"].forEach(function (t) {
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
    } else if (name === "issues") {
      renderIssuesTab();
    } else if (name === "history") {
      renderHistoryTab();
    } else if (name === "add-chapter") {
      renderAddChapterTab();
    }
    // Review tab renders on its own (event-driven); nothing to do here.
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
      '<label>Informal-statements file <span class="muted">(optional, repo-relative)</span>' +
      '<input id="ac-informal" type="text" placeholder=".marathon/review/informal/Chapter15.md"></label>' +
      '<button id="ac-start" class="ac-btn">Start intake</button>' +
      '</div>' +
      '<div id="ac-status" class="addchap-status" aria-live="polite"></div>' +
      '<div id="ac-drafts" class="addchap-drafts"></div>' +
      '</div>';
    acEl("ac-start").addEventListener("click", acStart);
  }

  function acStart() {
    var chapter = parseInt((acEl("ac-chapter") || {}).value, 10);
    if (!chapter || chapter < 1) {
      acEl("ac-status").innerHTML = '<span class="err">Enter a chapter number first.</span>';
      return;
    }
    var informal = ((acEl("ac-informal") || {}).value || "").trim();
    acEl("ac-drafts").innerHTML = "";
    acEl("ac-status").innerHTML = "Starting intake for Chapter " + chapter + "…";
    postJSON("/api/add-chapter", { chapter: chapter, informal_statements: informal })
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
        if (btn) { btn.disabled = false; btn.textContent = "Approve & open issues"; }
        acEl("ac-status").innerHTML = '<span class="err">Approve failed: ' + acEsc(e.message) + "</span>";
      });
  }

  // --- Status tab: the conductor (global orchestrator) + jobs + landings ---
  function renderStatusTab() {
    if (state.activeTab !== "status") return; // a stale poll after a switch
    getJSON("/api/conductor-status").then(function (d) {
      if (state.activeTab !== "status") return;
      var host = dashHost("dash-status");
      if (!host) return;
      host.innerHTML = "";
      var up = !!d.alive;
      host.appendChild(el("div", { class: "cond-banner" }, [
        el("span", { class: "cond-dot " + (up ? "up" : "down") }),
        el("div", {}, [
          el("div", { class: "cond-title",
            text: up ? "conductor running" : "conductor stopped" }),
          el("div", { class: "dash-empty",
            text: up ? "dispatches refines + auto-merges PRs across all chapters"
                     : "no live orchestrator — rejecting an issue starts one" }),
        ]),
        el("div", { class: "cond-meta" }, [
          el("div", {}, ["pid ", el("span", { class: "mono",
            text: d.pid == null ? "—" : String(d.pid) })]),
          el("div", { text: "concurrency " + (d.concurrency == null ? "—" : d.concurrency) }),
          el("div", { text: d.updated_ts ? ("updated " + fmtTs(d.updated_ts)) : "" }),
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
    refreshQueue();   // pure read — opens the first ready issue's subdeck
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
    switchTab: switchTab,   // dashboard tab navigation (pure read)
    _state: state,
  };
})();
