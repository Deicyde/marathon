"""marathon.deck — the deck: the human-facing "Code Tinder" review surface
(plan §2 "marathon deck" box + Phase 8 row).

The medium is a LOCAL WEB APP with the full swipe loop (an explicit user
decision overriding the plan's TUI default and its "no web app" line);
the *semantics* are still the plan's: ready cards only (green SHA,
gate-passed, dependency-predecessors resolved), dependency-ordered,
verify / reject / defer / deep-dive, reject notes verbatim to Aristotle,
a live status pane.

Three layers, all stdlib-only (no Flask/FastAPI, no npm build step):

* :mod:`marathon.deck.cards` — PURE card assembly. ``build_queue`` and
  ``build_card_detail`` read the committed audit/ledger/review sources
  (trust tiers, spec cards, kernels, the issue↔decl map, dep edges) and
  produce the API-contract objects. No mutation, no git/gh writes; reads
  are read-only queries. Degrades honestly when no audit snapshot exists
  (tier ``-``, kernel empty, card still shows the issue body) so the deck
  works on a not-yet-audited chapter.

* :mod:`marathon.deck.verdicts` — the verdict ROUTER. ``apply_verdict``
  routes verify/reject through the COMMITTED
  :func:`marathon.review.review.cmd_verify` / ``cmd_reject`` (the single
  verdict write path: ledger + GitHub + tracker + daemon/conductor
  trigger). It never reimplements the merge/dispatch; reject notes go
  VERBATIM to Aristotle by preserving the committed reject path's
  Claude-bypass. ``defer`` is a ledger/state marker only — no Aristotle,
  no GitHub verdict.

* :mod:`marathon.deck.server` — a stdlib
  :class:`http.server.ThreadingHTTPServer` bound to 127.0.0.1 ONLY, with
  a tiny router for the API contract plus static-file serving from
  ``marathon/deck/static/`` (the other track's dir). A per-session
  CSRF-style token minted on the served index is REQUIRED on every
  ``POST /api/verdict`` (the irreversible action), so a stray browser or
  other-origin tab cannot fire a verdict. GET endpoints are pure reads
  and never trigger a side effect.

Binding safety (the deck performs irreversible actions — verify merges
PRs, reject dispatches Aristotle): verdict side-effects fire ONLY on an
explicit, token-bearing ``POST /api/verdict`` from a deliberate user
action — never on page load, GET, navigation, or prefetch.
"""

from __future__ import annotations

__all__ = ["cards", "verdicts", "server"]
