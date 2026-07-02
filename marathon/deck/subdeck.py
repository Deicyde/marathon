"""marathon.deck.subdeck — the SUBDECK backend (per-declaration review).

A ready issue is the entry point to a SUBDECK: a small stack of cards, one
per cited DECLARATION. The human reviews each declaration on its own card
(keep / fix / skip-for-now), and finishing the subdeck SHIPS the whole issue.
This composition aligns with the existing harness, where refine / PR /
conductor / labels are all per-ISSUE — so review is per-decl, but the
irreversible outcome stays per-issue.

Three responsibilities, all here:

1. **A per-(issue,decl) verdict store** — a self-contained JSON sidecar
   ``<repo>/.marathon/review/deck-subdecks.json``, shape
   ``{issue_num(str): {decl_name: {verdict, note, ts}}}``. This is purely
   LOCAL review state (the human's running notes on each declaration); it
   touches neither the ledger, GitHub, nor Aristotle. It is additive — like
   the deck's defer markers (:mod:`marathon.deck.verdicts`) — and lives
   under the self-gitignored ``.marathon`` dir.

2. **The subdeck assembly** (:func:`build_subdeck`) — reuses
   :func:`marathon.deck.cards.build_card_detail` to get the issue's
   per-declaration views (each already carrying source / kernel / evidence /
   tier / informal rendering), overlays the LOCAL verdict/note from the
   store, orders the cards (unreviewed first, then worst-tier, then name),
   and computes the subdeck's progress / completeness / summary.

3. **The ship composition** (:func:`compose_ship_note`, :func:`ship_issue`)
   — composes the per-issue outcome from the subdeck and routes it through
   the committed verdict path (:func:`marathon.deck.verdicts.apply_verdict`,
   which calls ``cmd_verify`` / ``cmd_reject``). All-verified → verify the
   issue (no Aristotle). Any rejected → reject the issue with a human-authored
   composite note carried VERBATIM (the committed Claude-bypass) listing the
   KEEP and FIX declarations. SHIP is the only irreversible step and is
   reached only from an explicit ``POST /api/ship`` (see
   :mod:`marathon.deck.server`).

BINDING SAFETY: per-decl verdicts (:func:`record_decl_verdict`) are LOCAL
ONLY — no GitHub, no Aristotle, no ledger. The single irreversible action
lives in :func:`ship_issue`, which fires only on an explicit, token-checked
``POST /api/ship``, and only when every declaration has been reviewed.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING, Optional

if TYPE_CHECKING:  # pragma: no cover — type-only
    from marathon.review.config import ReviewConfig

#: The per-(issue,decl) verdict sidecar (additive — never the committed
#: review/state.json). Self-contained JSON keyed by issue number (str), then
#: by decl name: ``{num: {decl: {"verdict", "note", "ts"}}}``.
SUBDECK_RELPATH = Path(".marathon") / "review" / "deck-subdecks.json"

#: The per-declaration verdicts the subdeck offers. ``verify`` = keep the
#: declaration exactly as written; ``reject`` = it needs a fix (note
#: required); ``defer`` = skip for now (the subdeck stays incomplete).
DECL_VERDICTS = ("verify", "reject", "defer")

#: The per-decl verdicts that count a declaration as REVIEWED (toward
#: progress / completeness). ``defer`` does NOT count — a deferred decl
#: leaves the subdeck incomplete (the contract).
REVIEWED_DECL_VERDICTS = frozenset({"verify", "reject"})

#: Reserved store key for the subdeck's ONE informal-text APPROVAL verdict (the
#: leading approval card: "approve/reject the LLM-rendered informal text").
#: It is NOT a Lean declaration name — it can never collide with a real decl —
#: so it never matches a card view and is invisible to the per-decl cards,
#: progress, completeness, summary, and ship composition (all of which key off
#: real decl names). It is advisory: approving it clears the deck's
#: LLM-rendered warning and, at ship, flips the issue body's informal marker.
INFORMAL_KEY = "__informal__"

#: Reserved store key for the issue's "something is MISSING" note — a per-issue
#: free-text instruction that a result the title promises has NO declaration yet
#: (e.g. Prop 12.22 cites the tensor-product case but not the function-
#: multiplication case). It is NOT a decl name, so (like INFORMAL_KEY) it never
#: becomes a card or enters progress/summary. At ship it makes the outcome route
#: to Aristotle with an ADD section even when every existing decl is verified.
MISSING_KEY = "__missing__"

#: The STORED per-decl verdict (``verify``/``reject``/``defer``) → the
#: PAST-TENSE verdict the deck FRONTEND consumes on a serialized card
#: (``app.js`` renders ``card.verdict`` as the tag text + the ``dv-<verdict>``
#: CSS class — ``dv-verified``/``dv-rejected``/``dv-deferred`` — and its
#: ``deriveSummary``/``applyDeclResult`` match the same past-tense set). The
#: store stays present-tense (the ship composition + record API key off it);
#: only the wire face is mapped, so the GET seam matches the client.
_VERDICT_WIRE = {"verify": "verified", "reject": "rejected", "defer": "deferred"}

#: The wire (past-tense) verdicts that count a serialized card as REVIEWED —
#: the post-serialization mirror of :data:`REVIEWED_DECL_VERDICTS`, used by
#: :func:`next_unreviewed_card` which inspects the already-serialized cards.
_REVIEWED_WIRE_VERDICTS = frozenset({"verified", "rejected"})


# ---------------------------------------------------------------------------
# Per-(issue,decl) verdict store (LOCAL — no Aristotle, no GitHub, no ledger)
# ---------------------------------------------------------------------------


def _store_path(cfg: "ReviewConfig") -> Path:
    return Path(cfg.repo_dir) / SUBDECK_RELPATH


def _load_store(cfg: "ReviewConfig") -> dict:
    """The whole subdeck store ``{num(str): {decl: {...}}}``, or ``{}``. A
    missing/corrupt file is "nothing recorded", never an error."""
    path = _store_path(cfg)
    try:
        data = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError):
        return {}
    if not isinstance(data, dict):
        return {}
    # Keep only well-shaped issue → decl → record entries.
    out: dict = {}
    for num, decls in data.items():
        if isinstance(decls, dict):
            out[str(num)] = {
                d: rec for d, rec in decls.items() if isinstance(rec, dict)
            }
    return out


def _save_store(cfg: "ReviewConfig", store: dict) -> None:
    """Persist the store atomically (tmp write + replace). Lives in the
    same ``.marathon/review`` dir as ``state.json`` / ``deck-defers.json``
    (the gitignored ``.marathon`` convention), created on first write."""
    path = _store_path(cfg)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(store, indent=2, sort_keys=True) + "\n")
    tmp.replace(path)


def issue_verdicts(cfg: "ReviewConfig", issue_num: int) -> dict:
    """``{decl_name: {"verdict", "note", "ts"}}`` for one issue, or ``{}``.
    Read-only; the per-decl LOCAL review state the subdeck overlays."""
    return _load_store(cfg).get(str(issue_num), {})


def record_decl_verdict(
    cfg: "ReviewConfig",
    issue_num: int,
    decl: str,
    verdict: str,
    note: Optional[str] = None,
) -> dict:
    """Record one per-(issue,decl) verdict to the local sidecar and return
    the stored record.

    ``verdict`` must be one of :data:`DECL_VERDICTS`. A ``reject`` REQUIRES a
    non-empty ``note`` (it composes into the verbatim ship note that reaches
    Aristotle) — :class:`ValueError` otherwise. A ``verify`` / ``defer`` note
    is optional and preserved if given. Idempotent on (issue, decl): a later
    verdict for the same declaration supersedes the earlier one (the human
    changed their mind).

    LOCAL ONLY: this writes the JSON sidecar and nothing else — no GitHub, no
    Aristotle, no ledger verdict. The irreversible step is :func:`ship_issue`.
    """
    if verdict not in DECL_VERDICTS:
        raise ValueError(
            f"unknown decl verdict {verdict!r}; expected one of {DECL_VERDICTS}"
        )
    if verdict == "reject" and (note is None or not str(note).strip()):
        raise ValueError(
            "a reject requires a non-empty note (it composes into the ship "
            "note carried verbatim to Aristotle)"
        )

    store = _load_store(cfg)
    decls = store.setdefault(str(issue_num), {})
    prev = decls.get(decl) or {}
    entry = {
        "verdict": verdict,
        "note": str(note) if note is not None else None,
        "ts": datetime.now(timezone.utc).isoformat(),
    }
    # The hard-freeze LOCK is a modifier on a verify: carry it over on a
    # re-verify, but DROP it when the verdict changes to reject/defer (a decl
    # the human now wants changed must not be frozen).
    if verdict == "verify" and prev.get("locked"):
        entry["locked"] = True
    decls[decl] = entry
    _save_store(cfg, store)
    return entry


def set_decl_lock(
    cfg: "ReviewConfig", issue_num: int, decl: str, locked: bool
) -> dict:
    """Toggle the HARD-FREEZE lock on a declaration (opt-in, per-decl).

    A LOCKED decl's signature is frozen into the approved-statements manifest at
    reject-ship, so no sibling-decl refine can change it (the hard guard reverts
    any attempt). An UNLOCKED (soft) verified decl stays rewritable — Aristotle
    may re-type it and the change re-surfaces for review. Locking implies KEEP,
    so an unreviewed decl is recorded ``verify`` when first locked. LOCAL ONLY —
    the sidecar; the freeze is realized at :func:`ship_issue`."""
    store = _load_store(cfg)
    decls = store.setdefault(str(issue_num), {})
    entry = decls.get(decl)
    if entry is None or entry.get("verdict") not in ("verify", "reject", "defer"):
        entry = {
            "verdict": "verify",
            "note": None,
            "ts": datetime.now(timezone.utc).isoformat(),
        }
        decls[decl] = entry
    if locked:
        entry["locked"] = True
    else:
        entry.pop("locked", None)
    _save_store(cfg, store)
    return entry


def record_missing(
    cfg: "ReviewConfig", issue_num: int, note: Optional[str]
) -> Optional[dict]:
    """Record (or CLEAR) the issue's "something is MISSING" note — a per-issue
    instruction that the issue is incomplete (a result the title promises has no
    declaration). A non-empty ``note`` stores it under :data:`MISSING_KEY`; an
    empty/None ``note`` clears the flag. Returns the stored record (or None when
    cleared). LOCAL ONLY — writes the sidecar; the irreversible step is
    :func:`ship_issue`, where a present note routes the outcome to Aristotle."""
    store = _load_store(cfg)
    decls = store.setdefault(str(issue_num), {})
    text = (note or "").strip()
    if not text:
        decls.pop(MISSING_KEY, None)
        if not decls:
            store.pop(str(issue_num), None)
        _save_store(cfg, store)
        return None
    decls[MISSING_KEY] = {
        "verdict": "missing",
        "note": text,
        "ts": datetime.now(timezone.utc).isoformat(),
    }
    _save_store(cfg, store)
    return decls[MISSING_KEY]


def clear_issue(cfg: "ReviewConfig", issue_num: int) -> None:
    """Drop an issue's whole subdeck store (called after a successful VERIFY
    ship so a finished issue's local review state never lingers). No-op when
    absent."""
    store = _load_store(cfg)
    if str(issue_num) in store:
        store.pop(str(issue_num))
        _save_store(cfg, store)


def _persist_kept_verdicts(
    cfg: "ReviewConfig", issue_num: int, subdeck: dict, kept_names: list
) -> list[str]:
    """After a REJECT ship, REWRITE the issue's store to keep ONLY the human
    KEPT (verified) decls — each pinned to its CURRENT source
    :func:`_decl_fingerprint` — plus the informal approval; everything else
    (the rejected decls Aristotle will fix) is dropped. So when the refined
    issue returns, the kept decls are already verified (no re-review) UNLESS
    their source changed (then the fingerprint mismatch re-opens them). Returns
    the decl names carried over. The store is wiped if nothing is kept."""
    store = _load_store(cfg)
    old = store.get(str(issue_num), {})
    card_by_name = {c.get("name"): c for c in subdeck.get("cards", [])}
    new_entry: dict = {}
    carried: list[str] = []
    for name in kept_names:
        rec = old.get(name)
        if not rec or rec.get("verdict") != "verify":
            continue
        card = card_by_name.get(name)
        new_entry[name] = {
            "verdict": "verify",
            "note": rec.get("note"),
            "ts": rec.get("ts"),
            "fingerprint": _decl_fingerprint(card) if card else None,
        }
        carried.append(name)
    # The informal approval is issue-level (the body marker was flipped at ship)
    # — keep it so the human is not re-asked to approve the same statement.
    if INFORMAL_KEY in old:
        new_entry[INFORMAL_KEY] = old[INFORMAL_KEY]
    if new_entry:
        store[str(issue_num)] = new_entry
    else:
        store.pop(str(issue_num), None)
    _save_store(cfg, store)
    return carried


# ---------------------------------------------------------------------------
# Subdeck assembly (reuses cards.build_card_detail — never forked)
# ---------------------------------------------------------------------------


def _decl_fingerprint(view_or_card) -> str:
    """A stable content hash of a declaration — its ``.lean`` source if we have
    it, else the elaborated type, else the name. Used to PIN a carried-over
    verdict: an approved decl stays approved across a reject→refine→re-review
    cycle ONLY while this hash is unchanged (i.e. Aristotle did not alter it).
    Accepts a DeclView (object) or a serialized card (dict) — same basis either
    way, so a fingerprint taken at ship matches the one taken at re-review."""
    import hashlib

    if isinstance(view_or_card, dict):
        basis = (view_or_card.get("source_block")
                 or view_or_card.get("statement_pp")
                 or view_or_card.get("name") or "")
    else:
        basis = (getattr(view_or_card, "source_block", None)
                 or getattr(view_or_card, "statement_pp", None)
                 or getattr(view_or_card, "name", None) or "")
    return hashlib.sha256(basis.encode("utf-8")).hexdigest()


def _decl_card(view, record: Optional[dict], carry: Optional[str] = None) -> dict:
    """One subdeck card: a :class:`marathon.deck.cards.DeclView`'s JSON face
    plus the overlaid LOCAL verdict/note from the store (``None`` when the
    declaration has not been reviewed yet).

    The verdict is mapped to the PAST-TENSE wire form (:data:`_VERDICT_WIRE`)
    the frontend renders/styles — the stored sidecar stays present-tense (the
    ship composition keys off it); only this serialized face is mapped so the
    GET /api/subdeck seam matches what ``app.js`` consumes.

    ``carry`` flags a verdict CARRIED OVER from a prior ship: ``"carried_over"``
    (the decl was approved before and is unchanged — pre-verified, no re-review)
    or ``"changed"`` (approved before but its source changed — the verdict was
    dropped by the caller, so this is shown unreviewed with a re-review flag)."""
    card = view.to_json()
    stored = (record or {}).get("verdict")
    card["verdict"] = _VERDICT_WIRE.get(stored, stored)
    card["note"] = (record or {}).get("note")
    card["locked"] = bool((record or {}).get("locked"))
    card["carried_over"] = carry == "carried_over"
    card["changed_since_review"] = carry == "changed"
    return card


def _is_reviewed(record: Optional[dict]) -> bool:
    """True iff this declaration carries a verify/reject verdict (a defer or
    no-verdict leaves it unreviewed — the contract's progress rule)."""
    return bool(record) and record.get("verdict") in REVIEWED_DECL_VERDICTS


def _informal_slot(detail, records: dict) -> Optional[dict]:
    """The subdeck's ONE TOP-LEVEL informal statement (the issue's
    ``### Informal Statement`` — the textbook result, e.g. "Proposition 12.19")
    plus the human's single APPROVAL verdict over it. This is the ONLY text the
    leading approval card shows — NOT the per-declaration renderings (the human
    reviews the result once at the issue level, never decl-by-decl).

    Prefers the issue body's informal statement (``detail.issue_informal``);
    falls back to the headline declaration's LLM rendering when the body has no
    such section. Returns ``None`` when there is no top-level informal text at
    all — there is then nothing to approve (no approval card). ``is_llm`` is
    True iff that text is LLM-rendered and still PENDING human verification.

    The verdict lives OUTSIDE ``cards`` (keyed by the reserved
    :data:`INFORMAL_KEY`, never a real Lean name), so it never matches a view
    and is invisible to the cards / progress / completeness / summary / ship
    composition. It is advisory: approving clears the deck's LLM-rendered
    warning and, at ship, flips the issue body's informal marker."""
    statement = detail.issue_informal
    is_llm = detail.issue_informal_is_llm
    if not statement:
        # Fall back to the headline declaration's rendering when the body has no
        # Informal Statement section (so there is still ONE text to approve).
        head = detail.decls[0] if detail.decls else None
        if head is not None and head.informal_is_llm and head.informal_rendering:
            statement, is_llm = head.informal_rendering, True
    if not statement:
        return None
    rec = records.get(INFORMAL_KEY) or {}
    stored = rec.get("verdict")
    return {
        "is_llm": bool(is_llm),
        "verdict": _VERDICT_WIRE.get(stored, stored),
        "note": rec.get("note"),
        "statement": statement,
    }


def build_subdeck(
    cfg: "ReviewConfig", issue_num: int, *, skip_api: bool = False
) -> dict:
    """Assemble the per-declaration subdeck for ``issue_num`` (the shared API
    contract).

    Reuses :func:`marathon.deck.cards.build_card_detail` for the issue's
    per-declaration views (source / kernel / evidence / tier / informal
    rendering, plus the ``api``/``api_reason``/``in_code`` flags), overlays
    each declaration's LOCAL verdict/note from the store, and orders the cards
    UNREVIEWED-first, then worst-tier, then name (so the next declaration
    needing review leads). Computes:

    * ``card_tier`` — the worst tier over the declarations (the gate);
    * ``cards`` — the reviewable cards (see ``skip_api``);
    * ``auto_approved`` — the API cards that WILL be auto-approved at ship
      (only when ``skip_api``; ``[]`` otherwise);
    * ``progress`` — ``{reviewed, total}`` (deferred does NOT count reviewed);
    * ``complete`` — every reviewable card carries a verify/reject verdict;
    * ``summary`` — ``{kept, rejected:[{name,note}], deferred}``.

    When ``skip_api`` is truthy this is the SKIP-API view: ``cards`` holds
    ONLY the MAIN-result decls (the human reviews these), ``auto_approved``
    lists the easy-API decls (full DeclCards) that ship will fold into KEEP,
    and progress/complete count over the MAIN cards only. **Pure + read-only**
    — it LISTS what ship would auto-approve but writes nothing and fires no
    verdict (auto-approval is realized only in :func:`ship_issue`).

    When ``skip_api`` is false (the default / current behavior) ``cards`` is
    every decl and ``auto_approved`` is ``[]``.

    RECONCILIATION (PURE — writes NOTHING, never touches GitHub/git-write).
    Before partitioning, the cited DeclViews are reconciled against the CODE so
    the deck matches it, not a stale issue body:

    * a cited decl with ``in_code=false`` (renamed/deleted — absent everywhere)
      is DROPPED from the cards and listed in ``stale_citations`` (a FOOTNOTE,
      never a swipe card);
    * decls found in the code under one of the cited decls' NAMESPACES but not
      cited by the body are SURFACED as cards (``surfaced=true``,
      ``surfaced_reason`` set) so the deck shows the whole result;
    * ``reconcile`` summarizes what changed vs the raw citations:
      ``{dropped:[name], surfaced:[name]}``.

    ``skip_api`` partitions the RECONCILED card set AFTER reconciliation."""
    from marathon.audit.trust import TIER_ORDER
    from marathon.deck.cards import build_card_detail, surfaced_decls_for_issue

    detail = build_card_detail(cfg, issue_num)
    records = issue_verdicts(cfg, issue_num)

    # --- RECONCILIATION (pure, read-only) ---------------------------------
    # Partition the cited views by whether they are still in the code. A stale
    # citation (in_code=false) is dropped to the footnote, NOT a card.
    cited_in_code = [v for v in detail.decls if v.in_code]
    stale_views = [v for v in detail.decls if not v.in_code]
    stale_citations = [{"name": v.name} for v in stale_views]
    dropped_names = [v.name for v in stale_views]

    # Surface decls present in the code under the cited decls' namespaces but
    # not cited by the body (so the deck shows the whole result). The surfacing
    # is scoped to the KEPT (in-code) cited decls' namespaces and never
    # re-surfaces a decl the body already cites. PURE — writes nothing.
    cited_names = [v.name for v in cited_in_code]
    surfaced_views = surfaced_decls_for_issue(cfg, issue_num, cited_names)
    # Defensive: never surface a name that is already a kept cited card.
    _kept_names = {v.name for v in cited_in_code}
    surfaced_views = [v for v in surfaced_views if v.name not in _kept_names]
    surfaced_names = [v.name for v in surfaced_views]

    reconcile = {"dropped": dropped_names, "surfaced": surfaced_names}

    # The reconciled decl set the subdeck reviews: kept cited decls + surfaced
    # decls (stale dropped). This is what the partition below operates on.
    reconciled_views = list(cited_in_code) + list(surfaced_views)

    # CARRIED-OVER verdicts: a verdict persisted from a prior ship carries a
    # ``fingerprint`` and only STANDS while the decl is unchanged. Compute the
    # effective record + a carry-status per decl ONCE, and read it everywhere
    # the store is consulted (partition / cards / progress / summary) — so a
    # 'kept' decl Aristotle actually altered is re-reviewed, while an untouched
    # one is not. An ordinary current-session verdict (no fingerprint) is
    # unaffected.
    view_by_name = {v.name: v for v in reconciled_views}

    def _effective(name):
        rec = records.get(name)
        if not rec:
            return None, None
        if rec.get("fingerprint") is None:
            return rec, None
        view = view_by_name.get(name)
        if view is not None and _decl_fingerprint(view) != rec["fingerprint"]:
            return None, "changed"
        return rec, "carried_over"

    eff = {v.name: _effective(v.name) for v in reconciled_views}

    # Partition the views into MAIN vs API. An explicit human reject of an API
    # decl pulls it back to MAIN (the human chose to review it), so it always
    # shows in ``cards`` and never silently auto-approves.
    main_views = []
    api_views = []
    for view in reconciled_views:
        rec = eff[view.name][0]
        rejected_by_human = (rec or {}).get("verdict") == "reject"
        if skip_api and view.api and not rejected_by_human:
            api_views.append(view)
        else:
            main_views.append(view)

    def _build_cards(views: list) -> list[dict]:
        return [_decl_card(v, *eff[v.name]) for v in views]

    cards = _build_cards(main_views)
    auto_approved = _build_cards(api_views) if skip_api else []

    # Order: UNREVIEWED first (so the next decl to review leads), then by
    # worst tier (the gating decl), then by name — a total, deterministic key.
    def _sort_key(card: dict) -> tuple:
        reviewed = _is_reviewed(eff.get(card["name"], (None, None))[0])
        tier = card.get("tier")
        tier_rank = TIER_ORDER.index(tier) if tier in TIER_ORDER else 0
        return (1 if reviewed else 0, tier_rank, card["name"])

    cards.sort(key=_sort_key)
    auto_approved.sort(key=_sort_key)

    # Progress / completeness count over the REVIEWABLE (cards) set only — when
    # skip_api, the API decls are not the human's to review (ship approves
    # them), so they are excluded from the denominator.
    total = len(cards)
    reviewed = sum(1 for c in cards if _is_reviewed(eff.get(c["name"], (None, None))[0]))
    complete = total > 0 and reviewed == total

    kept: list[str] = []
    locked: list[str] = []
    rejected: list[dict] = []
    deferred: list[str] = []
    # The summary follows the RECONCILED decl order (kept cited + surfaced),
    # not the raw citations — a stale-cited decl is dropped (it is never a
    # reviewable card) and a surfaced decl is included.
    for view in reconciled_views:
        rec = eff[view.name][0]
        verdict = (rec or {}).get("verdict")
        if verdict == "verify":
            kept.append(view.name)
            if (rec or {}).get("locked"):
                locked.append(view.name)  # opt-in HARD freeze; kept ⊇ locked
        elif verdict == "reject":
            rejected.append({"name": view.name, "note": (rec or {}).get("note")})
        elif verdict == "defer":
            deferred.append(view.name)

    return {
        "issue": issue_num,
        "title": detail.title,
        "chapter": detail.chapter,
        "card_tier": detail.tier,
        "cards": cards,
        "auto_approved": auto_approved,
        "progress": {"reviewed": reviewed, "total": total},
        "complete": complete,
        "summary": {"kept": kept, "locked": locked,
                    "rejected": rejected, "deferred": deferred},
        # The leading informal-approval card's data: the issue's ONE top-level
        # informal statement (### Informal Statement) + the human's single
        # approval verdict, or None when there is no informal text. One approval
        # at the issue level covers the whole result (NOT decl-by-decl). Lives
        # OUTSIDE cards — never counted in progress/completeness/summary.
        "informal": _informal_slot(detail, records),
        # The issue-level "something is MISSING" note (or None) — a per-issue
        # flag that the issue is incomplete; at ship it routes to Aristotle with
        # an ADD instruction even when every existing decl is verified. OUTSIDE
        # cards (keyed by MISSING_KEY) — never counted in progress/summary.
        "missing": (records.get(MISSING_KEY) or {}).get("note"),
        # Reconciliation outputs (PURE — computed, written nowhere):
        "stale_citations": stale_citations,
        "reconcile": reconcile,
    }


def next_unreviewed_card(subdeck: dict) -> Optional[dict]:
    """The first card in the (already-ordered) subdeck without a
    verified/rejected verdict, or ``None`` when every declaration is reviewed
    (subdeck complete). Used as the ``next_decl`` of a decl-verdict response.

    Inspects the SERIALIZED cards, whose ``verdict`` is the past-tense wire
    form (:data:`_VERDICT_WIRE`), so it tests against
    :data:`_REVIEWED_WIRE_VERDICTS` (a deferred/unreviewed card still leads)."""
    for card in subdeck.get("cards", []):
        if card.get("verdict") not in _REVIEWED_WIRE_VERDICTS:
            return card
    return None


# ---------------------------------------------------------------------------
# Ship composition (the per-issue outcome composed from the subdeck)
# ---------------------------------------------------------------------------


def compose_ship_note(
    issue_num: int, summary: dict, missing: Optional[str] = None
) -> str:
    """The composite reject note (the shared contract's exact format).

    Human-authored composite — NO Claude — listing the KEPT declarations
    (verified exactly as written, never to be changed), the FIXED declarations
    (rejected, each with the human's verbatim note), and — when the human flagged
    the issue incomplete — an ADD section carrying the verbatim "what is missing"
    note (a result the title promises but no declaration covers). This is the
    note :func:`ship_issue` carries VERBATIM to ``cmd_reject`` (the committed
    Claude-bypass), so Aristotle sees precisely what the human wrote.

    Pure: derives entirely from ``summary`` + ``missing``. A KEEP/FIX section
    with no members still renders its header so the note's structure is stable;
    the ADD section is omitted entirely when nothing is missing."""
    from marathon.review.reject_note import (
        ADD_HEADER, FIX_HEADER, FOOTER, KEEP_HEADER,
    )

    kept = summary.get("kept", [])
    rejected = summary.get("rejected", [])

    lines = [f"Per-declaration review of issue #{issue_num}.", ""]
    lines.append(KEEP_HEADER)
    for name in kept:
        lines.append(f"- {name}")
    lines.append("")
    lines.append(FIX_HEADER)
    for item in rejected:
        lines.append(f"- {item.get('name')}: {item.get('note')}")
    lines.append("")
    missing_text = (missing or "").strip()
    if missing_text:
        lines.append(ADD_HEADER)
        for ln in missing_text.splitlines():
            ln = ln.rstrip()
            lines.append(ln if ln.lstrip().startswith("-") else f"- {ln}")
        lines.append("")
    lines.append(FOOTER)
    return "\n".join(lines)


def _next_ready_issue(cfg: "ReviewConfig", just_shipped: int) -> Optional[int]:
    """The next ready issue to enter (its subdeck) after shipping
    ``just_shipped``, scoped to that issue's chapter. Rebuilds the ISSUE
    queue freshly (shipping just changed the readiness graph — a verify may
    have unblocked a dependent) and returns the first ready card that isn't
    the one just shipped. ``None`` when the chapter's ready queue is drained.

    Reuses the queue's readiness (:func:`marathon.deck.cards.build_queue`) so
    "ready" means exactly what it means everywhere else in the deck."""
    from marathon.deck.cards import build_queue

    chapter = cfg.chapter_of_issue(just_shipped)
    try:
        queue = build_queue(cfg, chapter)
    except Exception:  # noqa: BLE001 — a queue-rebuild failure must not abort a ship
        return None
    for card in queue.cards:
        if card.id != just_shipped and card.ready:
            return card.id
    return None


def ship_issue(
    cfg: "ReviewConfig", issue_num: int, *, skip_api: bool = False
) -> dict:
    """Compose and SHIP the per-issue outcome from the subdeck — the single
    irreversible step (reached only from an explicit ``POST /api/ship``).

    Reads the LOCAL per-decl store and:

    * refuses if the subdeck is NOT complete (some reviewable declaration is
      unreviewed or merely deferred) → ``{ok: False, reason}`` with NO side
      effect;
    * if EVERY declaration is verified → routes
      :func:`marathon.deck.verdicts.apply_verdict` ``"verify"`` (marks the
      issue VERIFIED, merges the marathon PR, flips the tracker — NO
      Aristotle). Outcome ``"verified"``;
    * if ANY declaration is rejected → composes the verbatim KEEP/FIX note
      (:func:`compose_ship_note`) and routes
      :func:`marathon.deck.verdicts.apply_verdict` ``"reject"`` with that
      note inline (the committed Claude-bypass — the note reaches Aristotle
      verbatim). Outcome ``"shipped"``.

    SKIP-API auto-approval (realized ONLY here, at this explicit token-bearing
    ship): when ``skip_api`` is truthy, every API decl WITHOUT an explicit
    human verdict is AUTO-APPROVED — folded into KEEP in the composed outcome.
    A human's explicit REJECT of an API decl overrides (it stays a MAIN decl
    and goes to FIX). The result ``message`` (and, when there's a reject, the
    composed note's KEEP list) states the auto-approved names. Auto-approval
    is NOT realized on a GET or in decl-verdict — only here.

    On success clears the issue's subdeck store and reports the next ready
    issue to enter. Routing through ``apply_verdict`` (never a second verdict
    path) preserves the daemon/conductor dispatch and the Claude-bypass."""
    from marathon.deck.verdicts import apply_verdict

    subdeck = build_subdeck(cfg, issue_num, skip_api=skip_api)
    if not subdeck["complete"]:
        progress = subdeck["progress"]
        unreviewed = progress["total"] - progress["reviewed"]
        return {
            "ok": False,
            "reason": (
                f"subdeck not complete — {unreviewed} of {progress['total']} "
                "declaration(s) still unreviewed or deferred; review every "
                "declaration before shipping the issue"
            ),
        }

    summary = subdeck["summary"]
    # Fold the auto-approved API decls (skip_api: API decls with no explicit
    # verdict) into KEEP — the realization of auto-approval, at this explicit
    # ship only. ``auto_approved`` is [] when skip_api is false, so this is a
    # no-op in the default path.
    auto_names = [c["name"] for c in subdeck.get("auto_approved", [])]
    kept = list(summary["kept"]) + auto_names
    composed_summary = {
        "kept": kept,
        "rejected": summary["rejected"],
        "deferred": summary["deferred"],
    }

    auto_clause = ""
    if auto_names:
        auto_clause = (
            f"; auto-approved {len(auto_names)} API declaration(s): "
            + ", ".join(auto_names)
        )

    # The issue's "something is MISSING" note (a result the title promises but no
    # decl covers). When present it routes to Aristotle with an ADD section EVEN
    # IF every existing decl was verified — the issue is incomplete, not done.
    missing = subdeck.get("missing")

    if summary["rejected"] or missing:
        note = compose_ship_note(issue_num, composed_summary, missing)
        # HARD-FREEZE only the LOCKED decls (opt-in) — a sibling-decl refine
        # cannot change their signature. Soft-kept (verified, unlocked) decls
        # stay REWRITABLE: Aristotle may re-type them when a fix legitimately
        # cascades, and any such change re-surfaces for review (fingerprint
        # mismatch) rather than being reverted. Rejected decls are unfrozen so
        # the refine may change them.
        try:
            from marathon.review.approved_statements import capture_kept_on_reject

            frozen, unfrozen = capture_kept_on_reject(
                cfg, issue_num,
                kept=list(summary.get("locked", [])),
                rejected=[r.get("name") for r in summary["rejected"] if r.get("name")],
            )
            if frozen or unfrozen:
                print(
                    f"  freeze: hard-froze {len(frozen)} LOCKED decl(s), "
                    f"unfroze {len(unfrozen)} rejected decl(s) for #{issue_num}",
                    flush=True,
                )
        except Exception as exc:  # noqa: BLE001 — freeze is a hardening, never a blocker
            print(f"  freeze: skipped ({exc})", flush=True)
        apply_verdict(cfg, issue_num, "reject", note=note)
        outcome = "shipped"
        n_fix = len(summary["rejected"])
        bits = []
        if n_fix:
            bits.append(f"{n_fix} sent back to Aristotle to fix")
        if missing:
            bits.append("a missing declaration to add")
        message = (
            f"#{issue_num} shipped: {len(kept)} kept, "
            + " + ".join(bits) + f" (note verbatim){auto_clause}"
        )
    else:
        apply_verdict(cfg, issue_num, "verify")
        outcome = "verified"
        message = (
            f"#{issue_num} verified: all {len(kept)} declaration(s) "
            f"kept exactly as written{auto_clause}"
        )

    # Auto-add the KEPT surfaced declarations into the issue body (part of THIS
    # explicit ship — never on a GET). A surfaced decl the human reviewed and
    # did NOT reject genuinely belongs to the issue, so it is written into the
    # `### Lean signatures` section (which also drops any stale citation); a
    # rejected surfaced decl is excluded. Best-effort: a write failure prints a
    # warning and never fails the ship (the verdict already landed).
    surfaced_names = {
        c["name"] for c in subdeck.get("cards", []) if c.get("surfaced")
    }
    kept_surfaced = sorted(n for n in kept if n in surfaced_names)
    added_to_issue: list[str] = []
    if kept_surfaced:
        try:
            from marathon.deck.refresh import refresh_issue_signatures

            res = refresh_issue_signatures(
                cfg, issue_num, only_surfaced=set(kept_surfaced)
            )
            if res.get("updated"):
                added_to_issue = list(res.get("added", []))
                message += (
                    f"; added {len(added_to_issue)} surfaced declaration(s) "
                    "to the issue body"
                )
        except Exception as exc:  # noqa: BLE001 — never fail a landed ship
            print(f"  ship: WARN — could not add surfaced decls to #{issue_num}: {exc}")

    # If the human APPROVED the issue's LLM-rendered informal statement (the
    # leading approval card), flip the body's "⚠️ LLM-rendered … verification
    # pending" marker to a "✅ Verified …" marker — the honest, durable record
    # of that verification. Part of THIS explicit ship only; best-effort (a
    # failure never fails the landed verdict); independent of the KEEP/FIX
    # outcome (the informal English is verified whatever the Lean verdicts).
    # Read the verdict BEFORE clear_issue wipes the store.
    informal_rec = issue_verdicts(cfg, issue_num).get(INFORMAL_KEY) or {}
    informal_verified = False
    if informal_rec.get("verdict") == "verify":
        try:
            from marathon.deck.refresh import mark_informal_verified

            res = mark_informal_verified(cfg, issue_num)
            informal_verified = bool(res.get("updated"))
            if informal_verified:
                message += "; marked the issue's informal statement verified"
        except Exception as exc:  # noqa: BLE001 — never fail a landed ship
            print(
                f"  ship: WARN — could not mark informal verified on "
                f"#{issue_num}: {exc}"
            )

    # Local store cleanup. A VERIFY ship finishes the issue → wipe everything.
    # A REJECT ship sends only the rejected decls to Aristotle (the KEPT ones are
    # told to stay unchanged), so PERSIST the human-verified verdicts — pinned to
    # their current source — and drop the rest. When the refined issue comes back
    # the human re-reviews only the FIXED decls; an untouched 'kept' decl stays
    # approved (and is re-reviewed automatically if Aristotle altered it anyway).
    carried_over: list[str] = []
    if outcome == "shipped":
        carried_over = _persist_kept_verdicts(cfg, issue_num, subdeck, summary["kept"])
    else:
        clear_issue(cfg, issue_num)
    return {
        "ok": True,
        "outcome": outcome,
        "message": message,
        "auto_approved": auto_names,
        "added_to_issue": added_to_issue,
        "informal_verified": informal_verified,
        "carried_over": carried_over,
        "advanced_to_issue": _next_ready_issue(cfg, issue_num),
    }
