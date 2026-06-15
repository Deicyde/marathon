"""marathon.deck.cards — PURE card assembly + ready/dep-ordering logic.

This module turns the committed audit / ledger / review sources into the
deck's API-contract objects (:class:`Queue`, :class:`CardSummary`,
:class:`CardDetail`). It is the read side of the deck and is *pure* in the
sense that matters here:

* it never mutates the ledger, never writes git/gh, and performs only
  read-only queries (issue bodies via the bulk-GraphQL helper, the audit
  snapshot on disk, the ledger's append-only verdict log + planner
  target deps);
* every assembled object is derived, never persisted — recomputed on each
  request, exactly like trust tiers (plan §2 ruling 4).

The card content reuses the committed machinery, never a fork:

* tier + qualifiers   ← :func:`marathon.audit.trust.compute_tiers`
* statement / kernel / informal / evidence / semantic-delta slots
                      ← :meth:`marathon.audit.spec_card.SpecCard.from_snapshot`
* issue ↔ decl        ← :func:`marathon.review.verified_decls
                          .extract_declarations_from_issue_body`
* deps                ← the issue↔decl map + the kernel cone (which decls
                        a card's statement leans on) + the planner's
                        ``target_deps`` edges, all resolved back to the
                        issues that cite those decls.

**"ready"** (the swipeable predicate, shared API contract): a card is
ready iff it is at a green next/main SHA, gate-passed, and all of its
dependency-predecessor cards are verified-or-deferred. We ground the
green+gate half in the strongest read-only evidence the local substrate
provides — the card's computed trust tier: T1 is exactly "machine-audited
(elaborated, axiom-clean, no deception tags)", the plan's floor below
which *nothing reaches a human*. A card whose tier is below T1, or whose
predecessors are not all resolved, is returned NON-ready (greyed) with a
human ``blocked_reason`` rather than dropped.

**Dependency order** is topological, predecessors first: a card never
appears before a card it depends on.

**Degrade honestly.** With no audit snapshot on disk (the Ch.12
not-yet-audited case) tiers are ``-``, kernels empty, and cards are
non-ready with the reason "no audit snapshot" — but every card still
carries its issue title/body, so the deck works on an unaudited chapter.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING, Optional

if TYPE_CHECKING:  # pragma: no cover — type-only; avoids import-time cost
    from marathon.review.config import ReviewConfig

# The trust-tier floor below which a card is not swipeable (the plan's
# "nothing below T1 reaches a human" ruling). T1 = elaborated + axiom-clean
# + no deception tags — the machine-audit/"green+gate" half of readiness.
READY_TIER_FLOOR = "T1"

# Verdict statuses (from review.state.IssueState) that count a dependency
# predecessor as RESOLVED for the readiness gate: a verified or deferred
# card no longer blocks its dependents. "rejected"/"stalled"/unreviewed
# do NOT resolve a dependency.
RESOLVED_DEP_STATUSES = frozenset({"verified", "deferred"})


# ---------------------------------------------------------------------------
# API-contract dataclasses (the shapes the server serializes to JSON)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class CardSummary:
    """One queue card (the swipe surface's list item).

    ``ready`` is the swipeable predicate; non-ready cards are still
    returned (greyed) with a human ``blocked_reason``. ``tier`` is the
    worst computed tier over the issue's cited decls (a card is only as
    trustworthy as its least-trusted decl), or ``'-'`` when no snapshot
    evidence exists."""

    id: int  # issue_num — the card's stable identity
    decl: str  # the worst-tier cited decl (the card's headline decl)
    chapter: Optional[int]
    tier: str  # one of TIER_ORDER, or '-'
    tier_qualifiers: list[str]
    ready: bool
    blocked_reason: Optional[str]
    title: str

    def to_json(self) -> dict:
        return {
            "id": self.id,
            "decl": self.decl,
            "chapter": self.chapter,
            "tier": self.tier,
            "tier_qualifiers": list(self.tier_qualifiers),
            "ready": self.ready,
            "blocked_reason": self.blocked_reason,
            "title": self.title,
        }


@dataclass(frozen=True)
class DepRef:
    """A predecessor card a given card depends on (named in CardDetail)."""

    id: int  # the predecessor's issue_num
    decl: str
    tier: str

    def to_json(self) -> dict:
        return {"id": self.id, "decl": self.decl, "tier": self.tier}


@dataclass(frozen=True)
class KernelEntry:
    """One trust-kernel member (a project-local definition to read)."""

    name: str
    kind: str
    type_pp: Optional[str]
    value_pp: Optional[str]

    def to_json(self) -> dict:
        return {
            "name": self.name,
            "kind": self.kind,
            "type_pp": self.type_pp,
            "value_pp": self.value_pp,
        }


@dataclass(frozen=True)
class CardDetail:
    """The full spec card for one issue (the deep face of a queue card).

    Mirrors the shared API contract: statement + LLM-flagged informal
    rendering + the trust kernel + machine evidence + an optional semantic
    delta + the issue permalink + dependency refs + the iteration log.
    Built purely from the audit snapshot, ledger, and the issue body."""

    id: int  # issue_num
    decl: str
    title: str  # the issue title (the card's human headline)
    chapter: Optional[int]
    tier: str
    qualifiers: list[str]
    statement_pp: Optional[str]
    informal_rendering: Optional[str]  # LLM-flagged (see informal_is_llm)
    informal_is_llm: bool
    kernel: list[KernelEntry]
    evidence: dict  # {axioms_beyond_whitelist, sorry, deception_tags}
    semantic_delta: Optional[dict]  # {class, members[]} | None
    permalink: Optional[str]
    deps: list[DepRef]
    iteration_log: list[str]

    def to_json(self) -> dict:
        return {
            "id": self.id,
            "decl": self.decl,
            "title": self.title,
            "chapter": self.chapter,
            "tier": self.tier,
            "qualifiers": list(self.qualifiers),
            "statement_pp": self.statement_pp,
            "informal_rendering": self.informal_rendering,
            "informal_is_llm": self.informal_is_llm,
            "kernel": [k.to_json() for k in self.kernel],
            "evidence": dict(self.evidence),
            "semantic_delta": self.semantic_delta,
            "permalink": self.permalink,
            "deps": [d.to_json() for d in self.deps],
            "iteration_log": list(self.iteration_log),
        }


@dataclass(frozen=True)
class Queue:
    """The dep-ordered card queue plus live counters for the status pane."""

    cards: list[CardSummary]
    building: int
    landed_today: int

    def to_json(self) -> dict:
        return {
            "cards": [c.to_json() for c in self.cards],
            "building": self.building,
            "landed_today": self.landed_today,
        }


# ---------------------------------------------------------------------------
# Shared snapshot/ledger/tier context (one batched read per request)
# ---------------------------------------------------------------------------


@dataclass
class _DeckContext:
    """Everything a queue/detail build reads, gathered once.

    Gathering is fail-soft and read-only: a missing audit snapshot, an
    uninitializable ledger, or a failed bulk issue fetch each degrade to
    an honest empty/None rather than raising — the deck must work on a
    not-yet-audited chapter and offline."""

    snapshot: object  # AuditSnapshot | None
    tiers_by_decl: dict  # {decl_name: TierResult}
    issue_meta: dict  # {issue_num: {title, body, labels, ...}} (may be {})
    verdict_status: dict  # {issue_num: status str}  ('verified'/'rejected'/...)
    target_deps_by_decl: dict  # {decl_name: set[decl_name]} planner edges


def _load_context(
    cfg: "ReviewConfig", issue_nums: list[int]
) -> _DeckContext:
    """Read the snapshot, tiers, issue metadata, verdict statuses, and any
    planner dep edges for ``issue_nums`` — all read-only, all degrading."""
    # Local imports: keep this module importable on a checkout whose audit
    # / ledger deps are lazy (mirrors the review CLI's audit↔review
    # boundary discipline).
    from marathon.audit.engine import load_snapshot
    from marathon.review.github import fetch_issues_bulk

    snapshot = load_snapshot(cfg.repo_dir)
    tiers_by_decl = _tiers_by_decl(cfg, snapshot)
    issue_meta = fetch_issues_bulk(issue_nums, cfg.github_repo) or {}
    verdict_status = _verdict_status(cfg)
    target_deps_by_decl = _planner_deps_by_decl(cfg)
    return _DeckContext(
        snapshot=snapshot,
        tiers_by_decl=tiers_by_decl,
        issue_meta=issue_meta,
        verdict_status=verdict_status,
        target_deps_by_decl=target_deps_by_decl,
    )


def _tiers_by_decl(cfg: "ReviewConfig", snapshot) -> dict:
    """``{decl_name: TierResult}`` from the snapshot + ledger verdict log,
    or ``{}`` when no snapshot (the Ch.12 not-yet-audited case)."""
    if snapshot is None:
        return {}
    from marathon.audit.trust import compute_tiers
    from marathon.ledger import Ledger

    try:
        ledger = Ledger.for_review_config(cfg)
        return {r.decl_name: r for r in compute_tiers(snapshot, ledger)}
    except Exception:  # noqa: BLE001 — a broken ledger must not blank the deck
        return {}


def _verdict_status(cfg: "ReviewConfig") -> dict:
    """``{issue_num: status}`` for the readiness gate.

    The base is the committed review state (the read-side verdict truth in
    this phase: 'verified' / 'rejected' / 'stalled'); on top of it the
    deck's own DEFER markers are overlaid as status ``'deferred'`` (the
    deck-only verdict that touches neither Aristotle nor GitHub — see
    :mod:`marathon.deck.verdicts`). A live verify/reject always wins over a
    stale defer (the committed verdict is authoritative), so the base map
    is applied last for issues it knows. A missing/corrupt file on either
    side is an empty contribution, never an error."""
    from marathon.deck.verdicts import deferred_issue_nums
    from marathon.review.state import load_state

    out: dict[int, str] = {}
    try:
        for num in deferred_issue_nums(cfg):
            out[num] = "deferred"
    except Exception:  # noqa: BLE001 — no defers → no overlay
        pass
    try:
        state = load_state(cfg)
    except Exception:  # noqa: BLE001 — degrade to "no verdicts known"
        return out
    # The committed verdict is authoritative over a stale defer.
    for num, st in state.issues.items():
        out[num] = st.status
    return out


def _planner_deps_by_decl(cfg: "ReviewConfig") -> dict:
    """``{decl_name: {decl_name, ...}}`` from the planner's ``target_deps``
    DAG (a target depends on the targets it must land after), keyed by the
    target's ``lean_decl`` so it composes with the issue↔decl map. Empty
    when no planner targets exist (today's GeometricAnalysis state) — pure
    read, fully degrading."""
    from marathon.ledger import Ledger

    try:
        ledger = Ledger.for_review_config(cfg)
        targets = ledger.all_targets()
        edges = ledger.all_target_deps()
    except Exception:  # noqa: BLE001 — no planner ledger → no extra deps
        return {}
    by_id = {t.id: t for t in targets if t.id is not None}
    out: dict[str, set] = {}
    for target_id, dep_id in edges:
        target = by_id.get(target_id)
        dep = by_id.get(dep_id)
        if target is None or dep is None:
            continue
        td = target.lean_decl or target.name
        dd = dep.lean_decl or dep.name
        if td and dd:
            out.setdefault(td, set()).add(dd)
    return out


# ---------------------------------------------------------------------------
# Issue ↔ decl resolution (reusing the single committed parser)
# ---------------------------------------------------------------------------


def _decls_for_issue(ctx: _DeckContext, num: int) -> list[str]:
    """The Lean decls an issue's body cites, via the single committed
    parser (never forked). Empty when the body is unavailable."""
    from marathon.review.verified_decls import (
        extract_declarations_from_issue_body,
    )

    meta = ctx.issue_meta.get(num)
    body = (meta or {}).get("body")
    if not body:
        return []
    return sorted(extract_declarations_from_issue_body(body))


def _resolve_to_snapshot(ctx: _DeckContext, raw: str) -> Optional[str]:
    """Resolve a body-cited name (often unqualified) to a snapshot decl:
    exact, else a UNIQUE dotted-suffix match — the same deterministic rule
    as ``audit show`` / the tier backfill. None when absent/ambiguous."""
    if not ctx.tiers_by_decl:
        return None
    if raw in ctx.tiers_by_decl:
        return raw
    suffix = [name for name in ctx.tiers_by_decl if name.endswith("." + raw)]
    if len(suffix) == 1:
        return suffix[0]
    return None


def _issue_tier(
    ctx: _DeckContext, num: int
) -> tuple[str, list[str], Optional[str]]:
    """``(tier, qualifiers, headline_decl)`` for one issue.

    The tier is the WORST computed tier over the issue's cited decls (an
    issue is only as trustworthy as its least-trusted decl — the exact
    rule ``review.review._issue_tier`` uses). ``headline_decl`` is the
    decl that produced the worst tier (the card's title decl), or the
    first cited name when no snapshot evidence exists, or ``''`` when the
    body cites nothing. Returns tier ``'-'`` when there is no evidence."""
    from marathon.audit.trust import TIER_ORDER

    cited = _decls_for_issue(ctx, num)
    if not ctx.tiers_by_decl:
        # No snapshot: honest '-' tier, but keep a headline decl from the
        # body so the card still names what it is about.
        return "-", [], (cited[0] if cited else "")
    matched: list[tuple[str, str, list[str]]] = []  # (decl, tier, quals)
    for raw in cited:
        resolved = _resolve_to_snapshot(ctx, raw)
        if resolved is None:
            continue
        result = ctx.tiers_by_decl[resolved]
        matched.append((result.decl_name, result.tier, list(result.qualifiers)))
    if not matched:
        return "-", [], (cited[0] if cited else "")
    worst = min(matched, key=lambda m: TIER_ORDER.index(m[1]))
    return worst[1], worst[2], worst[0]


# ---------------------------------------------------------------------------
# Dependency edges (issue → predecessor issues)
# ---------------------------------------------------------------------------


def _decl_to_issues(ctx: _DeckContext, issue_nums: list[int]) -> dict:
    """``{snapshot_decl_name: set[issue_num]}`` — which issue(s) cite each
    decl. Used to resolve a card's cone/planner deps (decl names) back to
    predecessor CARDS (issues)."""
    out: dict[str, set] = {}
    for num in issue_nums:
        for raw in _decls_for_issue(ctx, num):
            resolved = _resolve_to_snapshot(ctx, raw)
            key = resolved if resolved is not None else raw
            out.setdefault(key, set()).add(num)
    return out


def _predecessor_issues(
    ctx: _DeckContext,
    num: int,
    issue_nums: list[int],
    decl_to_issues: dict,
) -> list[int]:
    """The predecessor issues card ``num`` depends on.

    A card depends on another card when its statement leans on a decl that
    other card owns. We derive the dependency decls two ways and union
    them: (a) the audit KERNEL cone of each of the card's decls (the
    project-local definitions its statement unfolds to) plus its local
    lemmas, and (b) the planner's ``target_deps`` edges. Each dependency
    decl is mapped back to the issue(s) that cite it; self-edges and
    dependencies the queue does not contain are dropped. Deterministic
    (sorted)."""
    dep_decls: set[str] = set()
    own_decls = {
        _resolve_to_snapshot(ctx, raw) or raw
        for raw in _decls_for_issue(ctx, num)
    }
    # (a) statement cone — only when a snapshot exists.
    if ctx.snapshot is not None:
        from marathon.audit.kernel import compute_kernel

        for decl in own_decls:
            if decl is None:
                continue
            kernel = compute_kernel(decl, ctx.snapshot)
            dep_decls.update(m.name for m in kernel.members)
            dep_decls.update(kernel.local_lemmas)
    # (b) planner edges.
    for decl in own_decls:
        dep_decls.update(ctx.target_deps_by_decl.get(decl, ()))

    in_queue = set(issue_nums)
    preds: set[int] = set()
    for dep_decl in dep_decls:
        for owner in decl_to_issues.get(dep_decl, ()):
            if owner != num and owner in in_queue:
                preds.add(owner)
    return sorted(preds)


def _topo_order(
    issue_nums: list[int], deps: dict
) -> list[int]:
    """Order issues predecessors-first (Kahn's algorithm, name-sorted ready
    set for determinism; any cycle remnants follow in issue-number order so
    the order is total). ``deps[num]`` is the set of issues ``num`` depends
    on (must come after)."""
    indegree = {num: len(deps.get(num, ())) for num in issue_nums}
    in_set = set(issue_nums)
    dependents: dict[int, list[int]] = {num: [] for num in issue_nums}
    for num in issue_nums:
        for dep in deps.get(num, ()):
            if dep in in_set:
                dependents[dep].append(num)
    ready = sorted(n for n in issue_nums if indegree[n] == 0)
    out: list[int] = []
    placed: set[int] = set()
    while ready:
        num = ready.pop(0)
        out.append(num)
        placed.add(num)
        newly: list[int] = []
        for dependent in dependents[num]:
            indegree[dependent] -= 1
            if indegree[dependent] == 0:
                newly.append(dependent)
        ready = sorted(set(ready) | set(newly))
    for num in sorted(in_set - placed):  # cycle remnants — keep total
        out.append(num)
    return out


# ---------------------------------------------------------------------------
# Readiness
# ---------------------------------------------------------------------------


def _readiness(
    ctx: _DeckContext,
    num: int,
    tier: str,
    preds: list[int],
) -> tuple[bool, Optional[str]]:
    """``(ready, blocked_reason)`` for one card (the shared-contract
    predicate). A card is ready iff it is at a green/gated state (grounded
    in tier ≥ T1 — the plan's machine-audit floor) AND every
    dependency-predecessor card is verified-or-deferred. An
    already-reviewed card (the human has a verdict) is non-ready: it is no
    longer in the swipe queue. Each non-ready reason is a single human
    sentence."""
    from marathon.audit.trust import TIER_ORDER

    status = ctx.verdict_status.get(num)
    if status == "verified":
        return False, "already verified"
    if status == "rejected":
        return False, "rejected — in the refine queue"
    if status == "stalled":
        return False, "rejected — refinement stalled (re-reject to re-queue)"
    if status == "deferred":
        return False, "deferred"

    if ctx.snapshot is None:
        return False, "no audit snapshot — run `marathon audit run` to gate it"
    if tier == "-":
        return False, "no audit evidence for this card's declarations"
    if TIER_ORDER.index(tier) < TIER_ORDER.index(READY_TIER_FLOOR):
        return False, (
            f"tier {tier} below the machine-audit floor {READY_TIER_FLOOR} "
            "(not yet axiom-clean / deception-free)"
        )

    unresolved = [
        p for p in preds
        if ctx.verdict_status.get(p) not in RESOLVED_DEP_STATUSES
    ]
    if unresolved:
        joined = ", ".join(f"#{p}" for p in unresolved)
        return False, (
            f"waiting on dependency-predecessor card(s) {joined} "
            "(verify or defer them first)"
        )
    return True, None


# ---------------------------------------------------------------------------
# Counters (the status-pane numbers folded into the queue payload)
# ---------------------------------------------------------------------------


def _building_count(cfg: "ReviewConfig") -> int:
    """How many conductor jobs are currently running (the "building"
    counter). Read purely from the conductor's jobs.json snapshot — no
    process inspection, no API. 0 when there is no conductor snapshot."""
    from marathon.conductor import load_jobs_snapshot

    try:
        snap = load_jobs_snapshot(cfg.repo_dir)
    except Exception:  # noqa: BLE001 — droppable runtime state
        return 0
    if not snap:
        return 0
    return sum(
        1 for j in snap.get("jobs", []) if j.get("status") == "running"
    )


def _landed_today_count(cfg: "ReviewConfig") -> int:
    """Successful landings recorded today (UTC), from the landing record
    JSONL. Read purely; 0 when there is no landing record."""
    from marathon.landing import LANDINGS_RELPATH

    path = Path(cfg.repo_dir) / LANDINGS_RELPATH
    if not path.is_file():
        return 0
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    count = 0
    try:
        for line in path.read_text().splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                row = json.loads(line)
            except (ValueError, TypeError):
                continue
            ts = str(row.get("ts", ""))
            if ts.startswith(today):
                count += 1
    except OSError:
        return 0
    return count


# ---------------------------------------------------------------------------
# Public builders
# ---------------------------------------------------------------------------


def build_queue(cfg: "ReviewConfig", chapter: Optional[int] = None) -> Queue:
    """Assemble the dep-ordered card queue for ``chapter`` (or every
    registered chapter when None), plus the status-pane counters.

    Pure + read-only: reads the audit snapshot, ledger, review state, and
    issue bodies; writes nothing and touches git/gh only via the
    read-only bulk issue fetch. Cards are returned in topological order
    (predecessors first); non-ready cards are included (greyed) with a
    ``blocked_reason`` so the deck can show — but not swipe — them.
    Degrades honestly with no snapshot (tier '-', every card non-ready
    with the reason, bodies/titles still present)."""
    issue_nums = _chapter_issue_nums(cfg, chapter)
    ctx = _load_context(cfg, issue_nums)

    decl_to_issues = _decl_to_issues(ctx, issue_nums)
    deps: dict[int, list[int]] = {
        num: _predecessor_issues(ctx, num, issue_nums, decl_to_issues)
        for num in issue_nums
    }
    ordered = _topo_order(issue_nums, {n: set(d) for n, d in deps.items()})

    cards: list[CardSummary] = []
    for num in ordered:
        tier, qualifiers, decl = _issue_tier(ctx, num)
        ready, blocked_reason = _readiness(ctx, num, tier, deps[num])
        meta = ctx.issue_meta.get(num) or {}
        cards.append(CardSummary(
            id=num,
            decl=decl,
            chapter=cfg.chapter_of_issue(num),
            tier=tier,
            tier_qualifiers=qualifiers,
            ready=ready,
            blocked_reason=blocked_reason,
            title=meta.get("title") or f"#{num}",
        ))
    return Queue(
        cards=cards,
        building=_building_count(cfg),
        landed_today=_landed_today_count(cfg),
    )


def build_card_detail(cfg: "ReviewConfig", issue_num: int) -> CardDetail:
    """Assemble the full spec card for one issue.

    Reuses :meth:`SpecCard.from_snapshot` for the statement / kernel /
    evidence (so the deck's deep card is byte-for-byte the same machine
    facts as ``marathon audit card``), attaches the spec-auditor's Claude
    half (informal rendering, semantic-delta prose) if it is present on
    disk, and resolves the card's deps back to predecessor cards. Pure +
    read-only; degrades honestly when the decl is absent/unknown (empty
    kernel, '-' tier) or no snapshot exists."""
    ctx = _load_context(cfg, [issue_num])
    tier, qualifiers, decl = _issue_tier(ctx, issue_num)
    chapter = cfg.chapter_of_issue(issue_num)

    statement_pp: Optional[str] = None
    informal_rendering: Optional[str] = None
    informal_is_llm = False
    kernel: list[KernelEntry] = []
    evidence: dict = {
        "axioms_beyond_whitelist": [],
        "sorry": None,
        "deception_tags": [],
    }
    semantic_delta: Optional[dict] = None

    resolved = _resolve_to_snapshot(ctx, decl) if decl else None
    if ctx.snapshot is not None and resolved is not None:
        from marathon.audit.spec_card import SpecCard

        card = SpecCard.from_snapshot(resolved, ctx.snapshot, _ledger(cfg))
        statement_pp = card.type_pp
        kernel = [
            KernelEntry(
                name=m.name, kind=m.kind,
                type_pp=m.type_pp, value_pp=m.value_pp,
            )
            for m in card.kernel.members
        ]
        evidence = {
            "axioms_beyond_whitelist": list(
                card.evidence.axioms_beyond_whitelist
            ),
            "sorry": card.evidence.sorry,
            "deception_tags": list(card.evidence.deception_tags),
        }
        # The Claude half (informal rendering, semantic-delta prose) is
        # attached by the spec-auditor later and may be absent. When the
        # slot is empty the deck flags any rendering it falls back to as
        # LLM-pending so the human knows it was not human-attested.
        if card.informal_rendering:
            informal_rendering = card.informal_rendering
            informal_is_llm = True
        if card.semantic_delta_prose:
            semantic_delta = {
                "class": "advisory",
                "members": [],
                "prose": card.semantic_delta_prose,
            }

    # Issue-derived fields (permalink, iteration log) — pure read.
    meta = ctx.issue_meta.get(issue_num) or {}
    permalink = _issue_permalink(cfg, issue_num)
    iteration_log = _iteration_log(cfg, issue_num, meta)

    # Deps back to predecessor cards (same chapter scope as the queue).
    chapter_nums = _chapter_issue_nums(cfg, chapter)
    if issue_num not in chapter_nums:
        chapter_nums = [issue_num, *chapter_nums]
    ctx_full = _load_context(cfg, chapter_nums)
    decl_to_issues = _decl_to_issues(ctx_full, chapter_nums)
    pred_ids = _predecessor_issues(
        ctx_full, issue_num, chapter_nums, decl_to_issues
    )
    deps: list[DepRef] = []
    for pid in pred_ids:
        ptier, _q, pdecl = _issue_tier(ctx_full, pid)
        deps.append(DepRef(id=pid, decl=pdecl, tier=ptier))

    return CardDetail(
        id=issue_num,
        decl=decl,
        title=meta.get("title") or f"#{issue_num}",
        chapter=chapter,
        tier=tier,
        qualifiers=qualifiers,
        statement_pp=statement_pp,
        informal_rendering=informal_rendering,
        informal_is_llm=informal_is_llm,
        kernel=kernel,
        evidence=evidence,
        semantic_delta=semantic_delta,
        permalink=permalink,
        deps=deps,
        iteration_log=iteration_log,
    )


# ---------------------------------------------------------------------------
# Small read-only helpers
# ---------------------------------------------------------------------------


def _ledger(cfg: "ReviewConfig"):
    from marathon.ledger import Ledger

    return Ledger.for_review_config(cfg)


def _chapter_issue_nums(
    cfg: "ReviewConfig", chapter: Optional[int]
) -> list[int]:
    """The issue numbers in scope: one chapter's registry, or every
    registered chapter's, in registry (textbook) order, de-duplicated."""
    nums: list[int] = []
    seen: set[int] = set()
    chapters = (
        [chapter] if chapter is not None else sorted(cfg.chapters)
    )
    for chap in chapters:
        registry = cfg.chapters.get(chap)
        if registry is None:
            continue
        for num, _ in registry.entries:
            if num not in seen:
                seen.add(num)
                nums.append(num)
    return nums


def _issue_permalink(cfg: "ReviewConfig", issue_num: int) -> Optional[str]:
    """The GitHub permalink for the issue, derived purely from the config's
    ``owner/repo`` slug (no network). None when the slug is malformed."""
    repo = cfg.github_repo
    if not repo or "/" not in repo:
        return None
    return f"https://github.com/{repo}/issues/{issue_num}"


def _iteration_log(
    cfg: "ReviewConfig", issue_num: int, meta: dict
) -> list[str]:
    """The card's iteration log: the per-issue verdict timeline from the
    review state (verdict status + timestamp + attempt count). Pure read;
    empty when the issue has no recorded state. The GitHub comment thread
    is the durable record but costs a per-issue round-trip — the deck
    surfaces the cheap local timeline and links to the issue for the
    full thread."""
    from marathon.review.state import load_state

    try:
        state = load_state(cfg)
    except Exception:  # noqa: BLE001 — no state → empty log
        return []
    entry = state.issues.get(issue_num)
    if entry is None:
        return []
    log = [f"{entry.verdict_ts}  {entry.status}"]
    if entry.last_iteration_ts:
        log.append(f"{entry.last_iteration_ts}  refine iteration dispatched")
    if entry.attempts:
        log.append(f"{entry.attempts} failed refine attempt(s) since verdict")
    return log
