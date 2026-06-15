/*
 * app.js — the marathon deck "Code Tinder" swipe UI (vanilla JS, no build).
 *
 * Codes to the SHARED API CONTRACT:
 *   GET  /api/queue?chapter=N  -> {cards: [CardSummary], building, landed_today}
 *   GET  /api/card/{id}        -> CardDetail
 *   POST /api/verdict {id, verdict, note?}  (IRREVERSIBLE side-effects)
 *   GET  /api/status (poll) or /api/events (SSE)  -> live job/build events
 *
 * BINDING SAFETY (load-bearing — see the task brief):
 *   - POST /api/verdict is fired ONLY from postVerdict(), which is reached ONLY
 *     from the explicit verify/reject/defer user-action handlers below. It is
 *     NEVER called on page load, GET, navigation, prefetch, or status polling.
 *     (tests/test_deck_frontend.py statically enforces this.)
 *   - Every POST carries the per-session token in the X-Marathon-Session-Token header,
 *     read from the injected meta tag / bootstrap global.
 *   - Reject notes are sent VERBATIM (no client-side rewriting) and go straight
 *     to Aristotle by the backend's bypass path; the note is REQUIRED.
 *   - verify asks for an explicit confirm (it merges a PR — irreversible).
 *   - Read endpoints (queue/card/status) are pure reads; they never mutate.
 */
(function () {
  "use strict";

  // --- session token -------------------------------------------------------
  // Prefer the meta tag; fall back to the bootstrap global. We hold it in a
  // closure and attach it to every verdict POST. If the placeholder was never
  // substituted (token absent), verdict POSTs are blocked client-side so we
  // never fire an irreversible action without authorization.
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
  // These three GET helpers never mutate; they are safe to call on load,
  // navigation, and polling. They MUST NOT be used to issue a verdict.
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
  function fetchCard(id) {
    return getJSON("/api/card/" + encodeURIComponent(id));
  }
  function fetchStatus() {
    return getJSON("/api/status");
  }

  // --- network: THE ONLY MUTATING CALL -------------------------------------
  // postVerdict is the single irreversible side-effect path. It is invoked
  // ONLY from the verify/reject/defer user-action handlers (commitVerdict).
  // Never call this from a read/poll/load path.
  function postVerdict(id, verdict, note) {
    if (!SESSION_TOKEN) {
      return Promise.reject(
        new Error("no session token — refusing to POST a verdict")
      );
    }
    var body = { id: id, verdict: verdict, token: SESSION_TOKEN };
    if (typeof note === "string") body.note = note; // verbatim, no rewrite
    var headers = {
      "Content-Type": "application/json",
      Accept: "application/json",
    };
    // per-session CSRF-style token on EVERY verdict POST (header name MUST
    // match the committed backend; the body `token` is the backend's
    // documented convenience fallback).
    headers[TOKEN_HEADER] = SESSION_TOKEN;
    return fetch("/api/verdict", {
      method: "POST",
      headers: headers,
      credentials: "same-origin",
      body: JSON.stringify(body),
    }).then(function (r) {
      return r.json().then(function (data) {
        if (!r.ok || (data && data.ok === false)) {
          throw new Error((data && data.message) || "verdict failed (" + r.status + ")");
        }
        return data;
      });
    });
  }

  // --- app state -----------------------------------------------------------
  var state = {
    chapter: "",
    queue: [],          // [CardSummary]
    building: 0,
    landedToday: 0,
    current: null,      // CardSummary at top of ready stack
    detail: null,       // CardDetail for current
    busy: false,        // a verdict POST is in flight (locks input)
  };

  function readyCards() {
    return state.queue.filter(function (c) { return c.ready; });
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

  function kernelList(kernel) {
    var box = el("div", { class: "kernel" }, [
      el("h3", { class: "kernel-title", text: "Definitions you must read" }),
    ]);
    if (!kernel || !kernel.length) {
      box.appendChild(
        el("p", {
          class: "kernel-empty",
          text:
            "none — phrased entirely in trusted (Mathlib/core) vocabulary. " +
            "Zero new definitions to read.",
        })
      );
      return box;
    }
    kernel.forEach(function (m) {
      var head = el("button", {
        class: "kernel-head",
        type: "button",
        "aria-expanded": "false",
      }, [
        el("span", { class: "kernel-name mono", text: m.name }),
        el("span", { class: "kernel-kind", text: m.kind || "" }),
        m.value_pp ? el("span", { class: "kernel-toggle", text: "▸ value" }) : null,
      ]);
      var typePp = el("pre", { class: "mono kernel-type" }, [m.type_pp || "(no type)"]);
      var body = el("div", { class: "kernel-body" }, [typePp]);
      if (m.value_pp) {
        var valuePp = el("pre", { class: "mono kernel-value", hidden: "hidden" }, [
          m.value_pp,
        ]);
        body.appendChild(valuePp);
        head.addEventListener("click", function () {
          var open = valuePp.hasAttribute("hidden");
          if (open) valuePp.removeAttribute("hidden");
          else valuePp.setAttribute("hidden", "hidden");
          head.setAttribute("aria-expanded", open ? "true" : "false");
        });
      }
      box.appendChild(el("div", { class: "kernel-member" }, [head, body]));
    });
    return box;
  }

  function informalBlock(detail) {
    var wrap = el("div", { class: "informal" }, [
      el("div", { class: "informal-flag" }, [
        "⚠️ LLM-rendered — verify against the source",
      ]),
    ]);
    var txt = detail.informal_rendering;
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

  // Render the full top card from (summary, detail). Pure read render.
  function renderCard(summary, detail) {
    var card = el("article", { class: "card", id: "active-card" });
    var header = el("header", { class: "card-head" }, [
      el("div", { class: "card-decl mono", text: detail.decl || summary.decl }),
      tierBadge(detail.tier || summary.tier, detail.qualifiers || summary.tier_qualifiers),
    ]);
    card.appendChild(header);
    card.appendChild(
      el("div", { class: "card-meta" }, [
        el("span", { class: "chapter-pill", text: "ch " + (detail.chapter ?? summary.chapter ?? "—") }),
        el("span", { class: "issue-pill", text: "#" + summary.id }),
        detail.title ? el("span", { class: "card-title", text: detail.title }) : null,
      ])
    );

    var delta = semanticDeltaBanner(detail.semantic_delta);
    if (delta) card.appendChild(delta);

    // Lean statement (monospace).
    card.appendChild(
      el("section", { class: "statement" }, [
        el("h3", { class: "sec-title", text: "Lean statement" }),
        el("pre", { class: "mono statement-pp" }, [
          detail.statement_pp || "(no elaborated type — declaration absent or did not elaborate)",
        ]),
      ])
    );

    // Informal (LLM-flagged) rendering.
    card.appendChild(el("section", { class: "sec-informal" }, [informalBlock(detail)]));

    // Kernel.
    card.appendChild(el("section", { class: "sec-kernel" }, [kernelList(detail.kernel)]));

    // Evidence.
    card.appendChild(
      el("section", { class: "sec-evidence" }, [
        el("h3", { class: "sec-title", text: "Evidence" }),
        evidenceRow(detail.evidence),
      ])
    );

    return card;
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

  function loadTopCard() {
    var ready = readyCards();
    if (!ready.length) {
      state.current = null;
      state.detail = null;
      showMessageCard({
        title: "All caught up",
        body:
          state.building > 0
            ? state.building + " card(s) building — check back shortly."
            : "No ready cards in this view.",
        actions: [{ label: "Refresh", kind: "btn-primary", onClick: refreshQueue }],
      });
      return;
    }
    var summary = ready[0];
    state.current = summary;
    // Pure READ of the card detail. No verdict here.
    setActionsEnabled(false);
    stage().innerHTML = "";
    stage().appendChild(el("div", { class: "card card-loading", text: "loading card…" }));
    fetchCard(summary.id)
      .then(function (detail) {
        state.detail = detail;
        stage().innerHTML = "";
        stage().appendChild(renderCard(summary, detail));
        setActionsEnabled(true);
      })
      .catch(function (err) {
        showErrorCard("Could not load card #" + summary.id, err, function () {
          loadTopCard();
        });
      });
  }

  function showErrorCard(title, err, retry) {
    showMessageCard({
      title: title,
      body: (err && err.message) || "network error",
      actions: [
        { label: "Retry", kind: "btn-primary", onClick: retry },
        { label: "Reload queue", onClick: refreshQueue },
      ],
    });
  }

  // === verdict flow ========================================================

  // The ONLY entry points to a mutating verdict. Each is triggered by a
  // deliberate user action (button click or key). They open the required UI
  // (reject note / verify confirm) and then call commitVerdict, which is the
  // sole caller of postVerdict.
  function actVerify() {
    if (!canAct()) return;
    openVerifyConfirm();
  }
  function actReject() {
    if (!canAct()) return;
    openRejectNote();
  }
  function actDefer() {
    if (!canAct()) return;
    commitVerdict("defer", undefined);
  }

  function canAct() {
    return !!state.current && !state.busy && !isModalOpen();
  }

  function commitVerdict(verdict, note) {
    if (!state.current) return;
    var id = state.current.id;
    state.busy = true;
    setActionsEnabled(false);
    animateOut(verdict);
    postVerdict(id, verdict, note)
      .then(function (resp) {
        state.busy = false;
        // Remove the acted card from our local queue (it left the deck).
        state.queue = state.queue.filter(function (c) { return c.id !== id; });
        if (resp && resp.advanced_to) {
          // Backend told us the next ready card; ensure it's in the queue.
          var adv = resp.advanced_to;
          if (!state.queue.some(function (c) { return c.id === adv.id; })) {
            state.queue.unshift(adv);
          } else {
            // move it to front so loadTopCard picks it
            state.queue = [adv].concat(state.queue.filter(function (c) { return c.id !== adv.id; }));
          }
        }
        flashToast(verdictPastTense(verdict) + " #" + id);
        loadTopCard();
        // Refresh status so building/landed reflect the new action.
        refreshStatus();
      })
      .catch(function (err) {
        state.busy = false;
        // Put the card's detail back up and report the failure; nothing landed.
        showErrorCard("Verdict failed for #" + id, err, function () {
          loadTopCard();
        });
      });
  }

  function verdictPastTense(v) {
    return v === "verify" ? "verified" : v === "reject" ? "rejected" : "deferred";
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
    var ta = el("textarea", {
      class: "reject-note",
      rows: "5",
      placeholder: "Why is this wrong? This note goes VERBATIM to Aristotle.",
      "aria-label": "Rejection note (required, sent verbatim to Aristotle)",
    });
    var err = el("div", { class: "modal-err", hidden: "hidden" });
    var panel = el("div", { class: "modal-panel" }, [
      el("h2", { class: "modal-title", text: "Reject #" + state.current.id }),
      el("p", { class: "modal-sub" }, [
        "Your note is sent ",
        el("strong", { text: "verbatim to Aristotle" }),
        " to drive the next attempt. A note is required.",
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
              err.textContent = "A rejection note is required (it goes to Aristotle).";
              err.removeAttribute("hidden");
              ta.focus();
              return;
            }
            closeModal();
            commitVerdict("reject", note); // verbatim, no client rewrite
          },
        }, ["Reject & send to Aristotle"]),
      ]),
    ]);
    openModal(panel);
    setTimeout(function () { ta.focus(); }, 0);
  }

  function openVerifyConfirm() {
    var panel = el("div", { class: "modal-panel" }, [
      el("h2", { class: "modal-title", text: "Verify #" + state.current.id }),
      el("p", { class: "modal-sub" }, [
        "Verifying is ",
        el("strong", { text: "irreversible" }),
        " — it merges the marathon PR. Confirm you have read the statement and kernel.",
      ]),
      el("div", { class: "modal-actions" }, [
        el("button", { class: "btn", type: "button", onclick: function () { closeModal(); } }, ["Cancel"]),
        el("button", {
          class: "btn btn-primary",
          type: "button",
          onclick: function () { closeModal(); commitVerdict("verify", undefined); },
        }, ["Confirm verify"]),
      ]),
    ]);
    openModal(panel);
  }

  function openDeps() {
    if (!state.detail) return;
    var deps = state.detail.deps || [];
    var list = el("ul", { class: "deps-list" });
    if (!deps.length) {
      list.appendChild(el("li", { class: "deps-empty", text: "no dependency predecessors" }));
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
    if (!state.detail) return;
    var url = state.detail.permalink;
    if (url) {
      // Opening the GitHub permalink / issue is a pure navigation, NOT a verdict.
      window.open(url, "_blank", "noopener");
      flashToast("opened deep-dive (#" + state.current.id + ")");
    } else {
      flashToast("no deep-dive link for this card");
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
    return fetchQueue(state.chapter)
      .then(function (data) {
        state.queue = (data.cards || []).slice();
        state.building = data.building || 0;
        state.landedToday = data.landed_today || 0;
        applyStatus({ building: state.building, landed_today: state.landedToday });
        populateChapters();
        loadTopCard();
      })
      .catch(function (err) {
        showErrorCard("Could not load the queue", err, refreshQueue);
      });
  }

  function populateChapters() {
    var sel = document.getElementById("chapter-select");
    if (!sel) return;
    var seen = {};
    state.queue.forEach(function (c) {
      if (c.chapter != null) seen[c.chapter] = true;
    });
    var chapters = Object.keys(seen).sort(function (a, b) { return Number(a) - Number(b); });
    var current = sel.value;
    // Keep "all" + observed chapters; preserve selection.
    sel.innerHTML = "";
    sel.appendChild(el("option", { value: "", text: "all" }));
    chapters.forEach(function (ch) {
      sel.appendChild(el("option", { value: ch, text: "ch " + ch }));
    });
    sel.value = current;
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
  // Drag a card; release past the threshold = verify (right) / reject (left).
  // These are deliberate user gestures — they route through the SAME
  // verify/reject handlers as the buttons/keys (which open confirm/note).
  function attachSwipe() {
    var dragging = false, startX = 0, startY = 0, dx = 0, dy = 0, target = null;
    var THRESH = 110;
    function onDown(e) {
      var card = document.getElementById("active-card");
      if (!card || !canAct()) return;
      // ignore drags that start on interactive children (kernel toggles etc.)
      if (e.target.closest("button, a, textarea, select, .kernel-body")) return;
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
      switch (e.key) {
        case "ArrowRight": e.preventDefault(); actVerify(); break;
        case "ArrowLeft": e.preventDefault(); actReject(); break;
        case " ": case "Spacebar": e.preventDefault(); actDefer(); break;
        case "d": case "D": openDeps(); break;
        case "o": case "O": openDeepDive(); break;
        case "?": openHelp(); break;
        default: break;
      }
    });
  }

  function openHelp() {
    var panel = el("div", { class: "modal-panel" }, [
      el("h2", { class: "modal-title", text: "Keyboard & gestures" }),
      el("ul", { class: "help-list" }, [
        el("li", { text: "→ / drag right — verify (asks confirm; merges the PR)" }),
        el("li", { text: "← / drag left — reject (requires a note, sent verbatim to Aristotle)" }),
        el("li", { text: "space — defer" }),
        el("li", { text: "d — dependencies (review predecessors first)" }),
        el("li", { text: "o — open deep-dive (GitHub permalink)" }),
        el("li", { text: "? — this help" }),
      ]),
      el("div", { class: "modal-actions" }, [
        el("button", { class: "btn", type: "button", onclick: function () { closeModal(); } }, ["Close"]),
      ]),
    ]);
    openModal(panel);
  }

  // === wiring ==============================================================
  function attachButtons() {
    document.getElementById("btn-verify").addEventListener("click", actVerify);
    document.getElementById("btn-reject").addEventListener("click", actReject);
    document.getElementById("btn-defer").addEventListener("click", actDefer);
    document.getElementById("btn-deps").addEventListener("click", openDeps);
    document.getElementById("btn-deepdive").addEventListener("click", openDeepDive);
    var sel = document.getElementById("chapter-select");
    if (sel) {
      sel.addEventListener("change", function () {
        state.chapter = sel.value;
        refreshQueue();
      });
    }
  }

  function init() {
    attachButtons();
    attachKeys();
    attachSwipe();
    setActionsEnabled(false);
    refreshQueue();   // pure read
    startStatus();    // pure read (SSE/poll)
  }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", init);
  } else {
    init();
  }

  // Expose a tiny surface for tests / debugging (read-only helpers).
  window.MarathonDeck = {
    readSessionToken: readSessionToken,
    tierClass: tierClass,
    _state: state,
  };
})();
