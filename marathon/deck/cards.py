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
import re
from dataclasses import dataclass, replace
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

# The trust-tier floor at/above which a decl is eligible to be classed
# "easy API" (the same machine-audit floor as readiness — never auto-approve
# anything the audit hasn't gated). Below it, a decl is always MAIN.
_API_TIER_FLOOR = "T1"

# Name segments/suffixes that mark a declaration as boilerplate API —
# projection/simp lemmas and bundled-map plumbing that a human reviewing the
# MAIN textbook results does not need to read line-by-line. Conservative and
# transparent: each entry yields a one-phrase ``api_reason``. A SEGMENT
# matches a dotted component anywhere in the name (``Foo.IsSmooth.bar``); a
# SUFFIX matches the final component's ending (``foo_apply``). When in doubt,
# a decl is MAIN (not API).
_API_NAME_SUFFIXES = (
    "_apply", "_smul", "_id", "_comp", "_zero", "_add", "_neg",
    "_fun", "_of_",
)
_API_NAME_SEGMENTS = ("IsSmooth", "coordChange")
# CamelCase suffixes on the final name component (matched as endings, not
# whole components) marking bundled-map plumbing: `pullbackLM` etc.
_API_CAMEL_SUFFIXES = ("LM",)


def classify_api(decl_view: "DeclView", headline_name: Optional[str]) -> tuple:
    """``(is_api, reason)`` — is ``decl_view`` an "easy API" declaration the
    skip-API toggle may auto-approve at ship? Conservative + transparent.

    A decl is ``api`` ONLY IF (the CONTRACT heuristic):

    * its tier is at/above :data:`_API_TIER_FLOOR` (machine-audited), AND
    * it carries NO deception tags, AND
    * it is found IN THE CODE (``in_code`` — never a stale citation), AND
    * it is NOT the issue's headline decl (``headline_name``), AND
    * it is an ``instance`` OR its name matches a helper/projection pattern
      (an ``_apply``/``_smul``/… suffix, or an ``LM``/``IsSmooth``/
      ``coordChange`` segment).

    Everything else — textbook-named results, the headline, anything below
    the floor, deception-tagged, or not-in-code — is a MAIN result the human
    always reviews. Returns ``(False, None)`` for MAIN. Pure."""
    from marathon.audit.trust import TIER_ORDER

    name = decl_view.name
    # Never auto-approve: the headline, a below-floor / unknown-tier decl, a
    # deception-tagged decl, or a stale citation (not in the code).
    if headline_name is not None and name == headline_name:
        return False, None
    if not getattr(decl_view, "in_code", True):
        return False, None
    tier = decl_view.tier
    if tier not in TIER_ORDER:
        return False, None
    if TIER_ORDER.index(tier) < TIER_ORDER.index(_API_TIER_FLOOR):
        return False, None
    if (decl_view.evidence or {}).get("deception_tags"):
        return False, None

    if decl_view.kind == "instance":
        return True, "instance"

    short = name.rsplit(".", 1)[-1]
    for suffix in _API_NAME_SUFFIXES:
        if short.endswith(suffix):
            return True, f"projection/helper lemma ({suffix})"
    # CamelCase suffix on the final component marking a bundled-map variant
    # (e.g. `pullbackLM`, `coordChangeLM` — the linear-map plumbing beside a
    # main `pullback`). A whole-component check misses these (the component
    # is `pullbackLM`, not a standalone `LM`), so match the suffix directly.
    for cs in _API_CAMEL_SUFFIXES:
        if short.endswith(cs) and short != cs:
            return True, f"bundled-map variant (…{cs})"
    components = name.split(".")
    for seg in _API_NAME_SEGMENTS:
        if seg in components:
            return True, f"bundled-map plumbing (.{seg}.)"
    return False, None


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
class DeclView:
    """One declaration's full face within a card.

    A review issue bundles every declaration of one textbook result (e.g.
    the covariant/mixed/contravariant `pullback` defs + their helpers all
    live under one sub-issue), so a card carries a ``DeclView`` per cited
    declaration — each with its own source, trust kernel, evidence, tier,
    and (LLM-flagged) informal rendering. The card-level tier is the WORST
    of these; the views are ordered worst-tier-first so the declaration
    holding the card back leads."""

    name: str
    kind: Optional[str]
    tier: str
    qualifiers: list[str]
    source_block: Optional[str]
    source_file: Optional[str]
    statement_pp: Optional[str]
    kernel: list[KernelEntry]
    evidence: dict
    informal_rendering: Optional[str]
    informal_is_llm: bool
    semantic_delta: Optional[dict]
    # ``in_code`` is true iff the declaration was located in the repo
    # source — in the audit snapshot OR by a repo-wide source scan. False
    # means the issue cites a name no longer present in the code: a STALE
    # CITATION (renamed/deleted), for which ``source_block`` is None and the
    # tier stays ``'-'``.
    in_code: bool = True
    # ``api`` marks a conservatively-classified "easy API" declaration the
    # skip-API toggle auto-approves at ship (the human only reviews MAIN
    # results); ``api_reason`` is one short phrase explaining the call.
    api: bool = False
    api_reason: Optional[str] = None
    # ``surfaced`` marks a declaration the RECONCILIATION auto-added so the
    # deck matches the CODE: a decl found in the code in one of the issue's
    # namespaces but NOT cited by the issue body. ``surfaced_reason`` is one
    # short phrase explaining why it was surfaced. A normally-cited decl has
    # ``surfaced=False`` / ``surfaced_reason=None``.
    surfaced: bool = False
    surfaced_reason: Optional[str] = None

    def to_json(self) -> dict:
        return {
            "name": self.name,
            "kind": self.kind,
            "tier": self.tier,
            "qualifiers": list(self.qualifiers),
            "source_block": self.source_block,
            "source_file": self.source_file,
            "statement_pp": self.statement_pp,
            "kernel": [k.to_json() for k in self.kernel],
            "evidence": dict(self.evidence),
            "informal_rendering": self.informal_rendering,
            "informal_is_llm": self.informal_is_llm,
            "semantic_delta": self.semantic_delta,
            "in_code": self.in_code,
            "api": self.api,
            "api_reason": self.api_reason,
            "surfaced": self.surfaced,
            "surfaced_reason": self.surfaced_reason,
        }


@dataclass(frozen=True)
class CardDetail:
    """The full spec card for one issue (the deep face of a queue card).

    Mirrors the shared API contract: statement + LLM-flagged informal
    rendering + the trust kernel + machine evidence + an optional semantic
    delta + the issue permalink + dependency refs + the iteration log.
    Built purely from the audit snapshot, ledger, and the issue body."""

    id: int  # issue_num
    decl: str  # the headline (worst-tier) decl name — decls[0].name
    title: str  # the issue title (the card's human headline)
    chapter: Optional[int]
    tier: str  # the WORST tier over all cited decls (the card's gate)
    qualifiers: list[str]
    # Every declaration this issue bundles, each with its own source /
    # kernel / evidence / tier / informal rendering, ordered worst-tier
    # first. The top-level scalar fields below mirror ``decls[0]`` (the
    # headline) for backward compatibility / degraded rendering.
    decls: list[DeclView]
    statement_pp: Optional[str]
    source_block: Optional[str]
    source_file: Optional[str]  # repo-relative path of the .lean file
    informal_rendering: Optional[str]  # LLM-flagged (see informal_is_llm)
    informal_is_llm: bool
    # The issue's TOP-LEVEL informal statement — the ``### Informal Statement``
    # section of the body (the textbook result, e.g. "Proposition 12.19"), with
    # the LLM-rendered marker stripped. ``issue_informal_is_llm`` is True iff
    # that section still carries the "verification pending" marker (not yet
    # human-verified). This is the ONE text the leading approval card shows —
    # NOT the per-declaration renderings.
    issue_informal: Optional[str]
    issue_informal_is_llm: bool
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
            "decls": [d.to_json() for d in self.decls],
            "statement_pp": self.statement_pp,
            "source_block": self.source_block,
            "source_file": self.source_file,
            "informal_rendering": self.informal_rendering,
            "informal_is_llm": self.informal_is_llm,
            "issue_informal": self.issue_informal,
            "issue_informal_is_llm": self.issue_informal_is_llm,
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
    verdict_status = _verdict_status(cfg, issue_meta)
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


def _verdict_status(cfg: "ReviewConfig", issue_meta: Optional[dict] = None) -> dict:
    """``{issue_num: status}`` for the readiness gate.

    Layered, later wins:

    1. the deck's own DEFER markers (deck-local; touch neither Aristotle nor
       GitHub — see :mod:`marathon.deck.verdicts`);
    2. the committed local review state (``state.json`` — operational truth
       like ``'stalled'``, plus any locally-recorded verdicts);
    3. **the GitHub labels** (``review:verified`` / ``review:rejected``),
       which are AUTHORITATIVE for human verdicts (the Phase-1 sync policy)
       and so are applied last — a card verified on GitHub drops out of the
       swipe queue even when the local ``state.json`` never recorded it
       (the documented state.json-lags-labels drift; recon §2). The labels
       come from ``issue_meta`` the queue already fetched, so this costs no
       extra GitHub call.

    A missing/corrupt source on any layer is an empty contribution, never an
    error."""
    from marathon.deck.verdicts import deferred_issue_nums
    from marathon.review.review import _status_from_labels
    from marathon.review.state import load_state

    out: dict[int, str] = {}
    try:
        for num in deferred_issue_nums(cfg):
            out[num] = "deferred"
    except Exception:  # noqa: BLE001 — no defers → no overlay
        pass
    try:
        state = load_state(cfg)
        for num, st in state.issues.items():
            out[num] = st.status
    except Exception:  # noqa: BLE001 — degrade to "no local verdicts"
        pass
    # GitHub labels win for human verdicts (verified/rejected). 'inflight' and
    # unlabelled issues do not override a more specific local state.
    for num, meta in (issue_meta or {}).items():
        labels = meta.get("labels")
        if not labels:
            continue
        gh_status = _status_from_labels(cfg, set(labels))
        if gh_status in ("verified", "rejected"):
            out[num] = gh_status
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


#: A line carries the LLM-rendered "verification pending" marker when it
#: mentions both halves (matches both textual variants the project emits —
#: spec_auditor's "LLM-rendered, verification pending" and the coreviewer's
#: "LLM-rendered from common knowledge; verification pending").
def _is_llm_marker_line(line: str) -> bool:
    return "LLM-rendered" in line and "verification pending" in line


def _issue_informal_statement(body: Optional[str]) -> tuple[Optional[str], bool]:
    """The issue's TOP-LEVEL informal statement — the ``### Informal Statement``
    section of the body (the textbook result, e.g. "Proposition 12.19").

    Returns ``(text, is_pending)``: ``text`` is the section's prose with the
    LLM-rendered marker line(s) and blockquote ``>`` prefixes stripped (None
    when there is no such section or it is empty); ``is_pending`` is True iff
    the section still carries the ``⚠️ LLM-rendered … verification pending``
    marker (i.e. not yet human-verified). PURE — reads only the body string."""
    if not body:
        return None, False
    lines = body.splitlines()
    start = None
    for i, line in enumerate(lines):
        if line.strip() == "### Informal Statement":
            start = i
            break
    if start is None:
        return None, False
    end = len(lines)
    for j in range(start + 1, len(lines)):
        if lines[j].startswith("### "):
            end = j
            break
    section = lines[start + 1:end]
    is_pending = any(_is_llm_marker_line(ln) for ln in section)
    kept = []
    for ln in section:
        # Strip a leading markdown blockquote marker FIRST so a blockquoted
        # status-marker line (e.g. "> *⚠️ …*") is still recognized and dropped.
        if ln.startswith("> "):
            ln = ln[2:]
        elif ln.startswith(">"):
            ln = ln[1:]
        # Drop the status-marker line — pending (⚠️) or already-verified (✅) —
        # whether or not it is wrapped in markdown italic asterisks.
        marker = ln.strip().strip("*").strip()
        if marker.startswith("⚠️") or marker.startswith("✅"):
            continue
        kept.append(ln)
    text = "\n".join(kept).strip()
    return (text or None), is_pending


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


def _module_to_lean_path(repo_dir: Path, module: str) -> Optional[Path]:
    """Inverse of ``module_from_file_path``: a dotted module name maps to
    ``<repo>/<dotted/as/slashes>.lean``. Returns the path if it exists."""
    if not module:
        return None
    rel = Path(*module.split(".")).with_suffix(".lean")
    candidate = repo_dir / rel
    return candidate if candidate.is_file() else None


# Cap a source block so a pathological mega-declaration can't blow up the
# card payload / page; the deep-dive (`o`) is the escape hatch for the rest.
_SOURCE_BLOCK_MAX_LINES = 400


# Caps for the repo-wide source fallback so a pathological repo can't blow up
# a card build: bound the number of .lean files scanned and each file's size.
# Deterministic regardless of which cap is hit (files are scanned in sorted
# order; the first matching site wins).
_REPO_SCAN_MAX_FILES = 4000
_REPO_SCAN_MAX_FILE_BYTES = 2 * 1024 * 1024

# Cap on how many declarations the reconciliation may auto-SURFACE for one
# issue (decls present in the code under the issue's namespaces but not cited
# by the body). Conservative: a runaway namespace can't flood the subdeck with
# dozens of uncited cards. Surfaced decls are added in deterministic (sorted)
# order, so the cap is stable.
_MAX_SURFACED_DECLS = 12

# Cap on how many "this may already be formalized" suggestions the flag-missing
# modal surfaces. Advisory + read-only; keep the list short and high-signal.
_MAX_MISSING_SUGGESTIONS = 8


def _doc_block(source_block: "Optional[str]") -> str:
    """The text of a decl's LEADING ``/-- … -/`` docstring (the part of its
    source block ABOVE the keyword), or ``""`` when it has none.

    The result-key gate tests against THIS — the decl's OWN docstring — not its
    proof body and not a neighbouring decl's docstring that a loose source slice
    (:func:`_slice_decl_block`) may have bled in past the keyword. Pure."""
    if not source_block:
        return ""
    s = source_block.lstrip()
    if not s.startswith("/--"):
        return ""
    end = s.find("-/")
    return (s[3:end] if end != -1 else s[3:]).strip()


def _doc_summary(source_block: "Optional[str]", limit: int = 200) -> str:
    """A one-line human snippet for a decl's source block: the first line of its
    ``/-- … -/`` docstring when present, else the first source line (the
    signature). Trimmed to ``limit`` chars. Pure; ``""`` for empty input."""
    doc = _doc_block(source_block)
    base = doc if doc else (source_block or "").strip()
    first = next((ln.strip() for ln in base.splitlines() if ln.strip()), "")
    return first[:limit]


def _result_key_pattern(key: str) -> "re.Pattern":
    """A boundary-anchored matcher for a textbook result key, so a key cannot
    collide with a LONGER number: ``12.2`` must not match ``12.22``/``12.20`` and
    ``1.1`` must not match ``1.10``/``21.1``/``v1.1.3``. The key may not be
    flanked by a digit (either side) or a dot (left side)."""
    return re.compile(r"(?<![\d.])" + re.escape(key) + r"(?![\d])")


def _docstring_result_key(
    source_block: "Optional[str]", patterns: "dict[str, re.Pattern]"
) -> "Optional[str]":
    """The first (sorted) result key whose boundary-anchored pattern appears in
    the decl's OWN docstring, or ``None``. Deterministic — ``patterns`` is
    iterated in sorted-key order by the caller's dict construction."""
    doc = _doc_block(source_block)
    if not doc:
        return None
    for key in patterns:  # insertion order = sorted (built by the caller)
        if patterns[key].search(doc):
            return key
    return None


def _locate_decl_site(text: str, decl_name: str):
    """``(sites, target_i)`` — the :func:`scan_lean_source` sites and the
    index of the one matching ``decl_name``, or ``(sites, None)`` when absent.

    Matching is on the FULL cited name: an exact qualified/name match, or
    ``decl_name`` as a whole trailing dotted segment of a site's qualified
    name (so cited ``A.B`` matches ``X.A.B`` but NOT ``Y.B``). It deliberately
    NEVER matches on the bare last component alone — across a repo-wide scan
    that collides catastrophically (e.g. every ``*.IsSmooth`` would match one
    cited ``Foo.IsSmooth``, surfacing a wrong declaration's source in a review
    tool). When no site matches the full name, the decl is absent here."""
    from marathon.formalization import scan_lean_source

    sites, _ = scan_lean_source(text)
    if not sites:
        return sites, None
    for i, s in enumerate(sites):
        if s.qualified == decl_name or s.name == decl_name:
            return sites, i
    suffix = "." + decl_name
    for i, s in enumerate(sites):  # full cited name as a whole dotted suffix
        if (s.qualified or "").endswith(suffix):
            return sites, i
    return sites, None


def _slice_decl_block(text: str, sites, target_i: int) -> str:
    """The verbatim source block for ``sites[target_i]``: the full
    ``def``/``theorem``/… body plus the docstring + attribute/modifier lines
    immediately above it, with trailing blanks / next-decl comments trimmed.

    The single extraction slice shared by the snapshot path
    (:func:`_read_decl_source`) and the repo-wide fallback
    (:func:`_find_decl_source_in_repo`) so both render identically. Capped at
    :data:`_SOURCE_BLOCK_MAX_LINES`."""
    lines = text.splitlines()
    kw_line = sites[target_i].line  # 1-based, the decl keyword line

    def has_open(idx: int) -> bool:  # 1-based: line opens a block comment
        return "/-" in lines[idx - 1]

    def is_modifier(idx: int) -> bool:  # 1-based: a lone modifier line
        s = lines[idx - 1].strip()
        return s in {"noncomputable", "private", "protected", "scoped", "@[simp]"} \
            or s.startswith("@[")

    # START: include attributes/modifiers directly above, then the full
    # docstring block (walk up to its `/-…` opener) if one sits directly
    # above with no blank gap. Stops at the blank line separating this decl
    # from the previous one (Mathlib-style spacing).
    start = kw_line
    j = kw_line - 1
    while j >= 1 and is_modifier(j):
        start = j
        j -= 1
    if j >= 1 and lines[j - 1].strip() != "":
        s = lines[j - 1].rstrip()
        if s.endswith("-/"):  # tail of a block docstring — include all of it
            k = j
            while k >= 1 and not has_open(k):
                k -= 1
            if k >= 1:
                start = k
        elif s.lstrip().startswith("--"):  # contiguous line-comment doc
            k = j
            while k >= 1 and lines[k - 1].lstrip().startswith("--"):
                start = k
                k -= 1

    # END: next decl keyword (or EOF), then trim BACKWARD past trailing
    # blank lines and any comment / section blocks (`/-! … -/`, `/-- … -/`,
    # `--`) that belong to the NEXT declaration, not this one.
    end = sites[target_i + 1].line - 1 if target_i + 1 < len(sites) else len(lines)
    while end > kw_line:
        s = lines[end - 1].strip()
        if s == "":
            end -= 1
        elif s.endswith("-/"):  # a trailing block comment — drop the block
            k = end
            while k > kw_line and not has_open(k):
                k -= 1
            end = k - 1
        elif s.startswith("--"):
            end -= 1
        else:
            break

    block_lines = lines[start - 1 : end]
    if len(block_lines) > _SOURCE_BLOCK_MAX_LINES:
        block_lines = block_lines[:_SOURCE_BLOCK_MAX_LINES] + [
            f"  -- … ({len(block_lines) - _SOURCE_BLOCK_MAX_LINES} more "
            "lines truncated — open the deep-dive to read the rest) …"
        ]
    return "\n".join(block_lines)


def _read_decl_source(
    repo_dir: Path, module: str, decl_name: str
) -> tuple[Optional[str], Optional[str]]:
    """Return ``(source_block, repo_relative_file)`` — the declaration as
    written in its snapshot-recorded ``.lean`` file (resolved from ``module``).

    Pure, read-only, best-effort. Locates the decl with the committed
    namespace-aware :func:`scan_lean_source`, then slices the block with the
    shared :func:`_slice_decl_block`. Returns ``(None, None)`` when the file
    or decl can't be found — the caller then tries the repo-wide fallback."""
    path = _module_to_lean_path(repo_dir, module)
    if path is None:
        return None, None
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None, None

    sites, target_i = _locate_decl_site(text, decl_name)
    if target_i is None:
        return None, None
    try:
        rel = str(path.relative_to(repo_dir))
    except ValueError:
        rel = str(path)
    return _slice_decl_block(text, sites, target_i), rel


def _tracked_lean_files(repo_dir: Path) -> list[Path]:
    """The repo's git-tracked (and untracked-but-not-ignored) ``.lean``
    files, sorted, capped at :data:`_REPO_SCAN_MAX_FILES`. Uses the SAME
    ``git ls-files --cached --others --exclude-standard`` mechanism as the
    rest of the harness; falls back to a deterministic ``rglob`` walk when
    git is unavailable (so the fallback still works on a non-git checkout).
    Pure read-only."""
    import subprocess

    rels: list[str] = []
    try:
        proc = subprocess.run(
            ["git", "ls-files", "--cached", "--others",
             "--exclude-standard", "-z"],
            cwd=str(repo_dir),
            capture_output=True,
            check=False,
        )
        if proc.returncode == 0:
            rels = [
                r.decode("utf-8", errors="replace")
                for r in proc.stdout.split(b"\0")
                if r and r.endswith(b".lean")
            ]
    except (OSError, ValueError):  # noqa: BLE001 — git missing → walk instead
        rels = []
    if rels:
        paths = [repo_dir / r for r in sorted(set(rels))]
    else:  # non-git checkout / git failed: deterministic filesystem walk
        try:
            paths = sorted(repo_dir.rglob("*.lean"))
        except OSError:
            return []
    return paths[:_REPO_SCAN_MAX_FILES]


def _scan_dir_has_lean(scan_dir: Optional[Path]) -> bool:
    """True iff ``scan_dir`` contains at least one ``.lean`` file — i.e. there is
    a real Lean repo we could have found a decl in. Used to decide whether a
    snapshot-resolved decl with no current source is a STALE CITATION (a by-name
    miss IS evidence of absence when there are Lean files) or merely unscannable
    (trust the snapshot). Returns on the first hit, so it is cheap for a Lean
    repo; ``False`` on any error or an empty/sourceless dir."""
    if scan_dir is None:
        return False
    try:
        for _ in Path(scan_dir).rglob("*.lean"):
            return True
    except OSError:
        return False
    return False


def _find_decl_source_in_repo(
    repo_dir: Path, decl_name: str
) -> tuple[Optional[str], Optional[str]]:
    """Repo-wide source fallback: scan the git-tracked ``.lean`` files for
    ``decl_name`` (via :func:`scan_lean_source`) and return its
    ``(source_block, repo_relative_file)`` using the same extraction slice as
    :func:`_read_decl_source`. ``(None, None)`` when the decl is found in no
    file — the issue cites a name absent from the current code.

    Used when the snapshot path yields no source (a decl that EXISTS in the
    code but was not in the audited snapshot, or not quoted in the issue).
    Deterministic (files scanned in sorted order; the first matching site
    wins), capped on file count + per-file size. Pure read-only."""
    for path in _tracked_lean_files(repo_dir):
        try:
            if path.stat().st_size > _REPO_SCAN_MAX_FILE_BYTES:
                continue
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        sites, target_i = _locate_decl_site(text, decl_name)
        if target_i is None:
            continue
        try:
            rel = str(path.relative_to(repo_dir))
        except ValueError:
            rel = str(path)
        return _slice_decl_block(text, sites, target_i), rel
    return None, None


def _code_decls_in_namespaces(
    repo_dir: Path, namespaces: "list[str]"
) -> "dict[str, tuple[str, Optional[str]]]":
    """Index ``{qualified_name: (source_block, repo_relative_file)}`` of decls in
    the CURRENT code that are DIRECT members (``<ns>.<leaf>``, no further dot) of
    one of ``namespaces``.

    A SINGLE pass over the (substring-pre-filtered) tracked ``.lean`` files — the
    source block is sliced INLINE, so a caller never re-scans the repo per
    candidate. Recovers decls ADDED or RENAMED after the audit snapshot — present
    in the code but in NEITHER the snapshot NOR any issue body (the #94 case: the
    code has the renamed ``…IsSmooth.smul_of_contMDiff`` while the snapshot AND
    the issue bodies still say the old ``…IsSmooth.smul_smoothFunction``).
    Deterministic (sorted files; first definition wins); pure read-only; capped
    on file count + per-file size."""
    from marathon.formalization import scan_lean_source

    nss = sorted(set(namespaces))
    if not nss:
        return {}
    prefixes = [ns + "." for ns in nss]
    out: "dict[str, tuple[str, Optional[str]]]" = {}
    for path in _tracked_lean_files(repo_dir):
        try:
            if path.stat().st_size > _REPO_SCAN_MAX_FILE_BYTES:
                continue
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        if not any(ns in text for ns in nss):  # cheap pre-filter
            continue
        sites, _ = scan_lean_source(text)
        try:
            rel: Optional[str] = str(path.relative_to(repo_dir))
        except ValueError:
            rel = str(path)
        for i, s in enumerate(sites):
            q = s.qualified or ""
            for p in prefixes:
                if q.startswith(p) and "." not in q[len(p):]:
                    out.setdefault(q, (_slice_decl_block(text, sites, i), rel))
                    break
    return out


def _build_decl_view(
    ctx: _DeckContext, ledger, raw_name: str, repo_dir: Optional[Path] = None
) -> DeclView:
    """One declaration's full face: source, kernel, evidence, tier, and the
    cached (LLM-flagged) informal rendering. Pure + read-only; degrades to a
    name-only view ('-' tier, no source) when the decl is unresolved/absent
    or no snapshot exists, so a card still lists everything its issue cites.

    Source resolution (the request-1 fix): the source block is read from the
    snapshot record first; failing that, a repo-wide scan over ``repo_dir``'s
    git-tracked ``.lean`` files locates a decl that EXISTS in the code but was
    not in the audited snapshot. ``in_code`` is true when the decl was found
    either way, and false when it is cited but absent everywhere (a STALE
    CITATION — ``source_block`` stays None and the tier stays ``'-'``)."""
    resolved = _resolve_to_snapshot(ctx, raw_name) if ctx.snapshot is not None else None

    tier = "-"
    qualifiers: list[str] = []
    if resolved is not None and resolved in ctx.tiers_by_decl:
        tr = ctx.tiers_by_decl[resolved]
        tier, qualifiers = tr.tier, list(tr.qualifiers)

    name = resolved or raw_name
    kind: Optional[str] = None
    statement_pp: Optional[str] = None
    source_block: Optional[str] = None
    source_file: Optional[str] = None
    kernel: list[KernelEntry] = []
    evidence: dict = {
        "axioms_beyond_whitelist": [],
        "sorry": None,
        "deception_tags": [],
    }
    informal_rendering: Optional[str] = None
    informal_is_llm = False
    semantic_delta: Optional[dict] = None

    if ctx.snapshot is not None and resolved is not None:
        from marathon.audit.spec_card import SpecCard

        card = SpecCard.from_snapshot(resolved, ctx.snapshot, ledger)
        statement_pp = card.type_pp
        snap_decl = ctx.snapshot.by_name().get(resolved)
        if snap_decl is not None:
            kind = snap_decl.kind
            # The declaration as written (docstring + full body), verbatim.
            if ctx.snapshot.repo_dir:
                source_block, source_file = _read_decl_source(
                    Path(ctx.snapshot.repo_dir), snap_decl.module, resolved
                )
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
        # The Claude half (informal rendering, semantic-delta prose) — cached
        # by `marathon audit spec-audit`, pinned to the type fingerprint so a
        # stale rendering for a changed statement is dropped. Always
        # LLM-flagged: rendered from the Lean type, never attested.
        if card.informal_rendering:  # in-object slot (rare; future-proof)
            informal_rendering = card.informal_rendering
            informal_is_llm = True
        if card.semantic_delta_prose:
            semantic_delta = {
                "class": "advisory", "members": [],
                "prose": card.semantic_delta_prose,
            }
        if snap_decl is not None and ctx.snapshot.repo_dir:
            from marathon.audit.spec_store import lookup as _spec_lookup

            cached = _spec_lookup(
                ctx.snapshot.repo_dir, resolved, snap_decl.fingerprint_type
            )
            if cached:
                if cached.get("informal_rendering") and not informal_rendering:
                    informal_rendering = cached["informal_rendering"]
                    informal_is_llm = True
                if cached.get("delta_prose") and not semantic_delta:
                    semantic_delta = {
                        "class": "advisory", "members": [],
                        "prose": cached["delta_prose"],
                    }

    # SOURCE RESOLUTION (request-1): if the snapshot path yielded no source
    # (no snapshot, the decl wasn't audited, or its module's file moved), fall
    # back to a repo-wide scan for the declaration BY NAME. This shows the
    # snippet for decls that exist in the code but were absent from the
    # snapshot. ``in_code`` is true when the decl is found in EITHER place —
    # a snapshot-RESOLVED decl was elaborated FROM the repo source, so it
    # exists in the code even if the source-text slice couldn't be extracted;
    # a repo-scan hit likewise proves existence. Only a decl found in NEITHER
    # is a stale citation (in_code=false, source null, tier '-').
    scan_dir = repo_dir
    if scan_dir is None and ctx.snapshot is not None and ctx.snapshot.repo_dir:
        scan_dir = Path(ctx.snapshot.repo_dir)
    if source_block is None and scan_dir is not None:
        scanned_block, scanned_file = _find_decl_source_in_repo(scan_dir, name)
        if scanned_block is not None:
            source_block, source_file = scanned_block, scanned_file
    # A decl is IN THE CURRENT CODE iff we have its source — from the snapshot's
    # (name-checked, current-file) location OR a repo-wide by-name scan.
    in_code = source_block is not None
    if not in_code and resolved is not None:
        # It resolved in the snapshot but we found NO source in the current code.
        # Treat it as a STALE CITATION (renamed/removed since the audit) ONLY when
        # there is a scannable Lean repo we could have found it in — there, a
        # by-name miss is real evidence of absence. With no scannable repo (e.g.
        # tests, or a snapshot whose repo is gone), the snapshot is the only
        # source of truth, so trust its resolution.
        in_code = not _scan_dir_has_lean(scan_dir)

    view = DeclView(
        name=name, kind=kind, tier=tier, qualifiers=qualifiers,
        source_block=source_block, source_file=source_file,
        statement_pp=statement_pp, kernel=kernel, evidence=evidence,
        informal_rendering=informal_rendering, informal_is_llm=informal_is_llm,
        semantic_delta=semantic_delta, in_code=in_code,
    )
    # Classify api WITHOUT a headline here (single-card detail also carries
    # api/api_reason); build_card_detail re-classifies with the real headline
    # so the headline is never auto-approvable.
    is_api, reason = classify_api(view, headline_name=None)
    return replace(view, api=is_api, api_reason=reason)


def _namespace_prefixes(cited_resolved: list[str]) -> list[str]:
    """The dotted-prefix NAMESPACES of a set of (snapshot-resolved) cited decl
    names: for each name, the part up to (but not including) its last dotted
    component (e.g. ``CovariantTensorField.pullback`` → ``CovariantTensorField``,
    ``Foo.Bar.baz`` → ``Foo.Bar``). De-duplicated and sorted (deterministic);
    names with no dot (no namespace) contribute nothing. Pure."""
    prefixes: set[str] = set()
    for name in cited_resolved:
        if "." in name:
            prefixes.add(name.rsplit(".", 1)[0])
    return sorted(prefixes)


def _surface_namespace_decls(
    ctx: _DeckContext,
    cited_names: list[str],
    ledger,
    repo_dir: Optional[Path] = None,
    *,
    exclude_names: "Optional[set[str]]" = None,
    result_keys: "Optional[set[str]]" = None,
) -> list[DeclView]:
    """Auto-SURFACE the declarations the issue body does NOT cite but which are
    the likely RENAME of one of its now-missing citations — so the deck matches
    the CODE, not a stale issue body.

    Scope (deliberately tight, to recover renames without flooding the subdeck
    with declarations that belong to OTHER review issues):

    * candidates are DIRECT members of a namespace ``N`` shared by the issue's
      cited decls (qualified name ``<N>.<leaf>``, no further dot), AND
    * NOT already cited by this issue, AND
    * NOT cited by any OTHER chapter issue (``exclude_names`` — so e.g. the
      ``*.pullback`` decls that belong to the pullback issue never surface into
      the smoothness issue), AND
    * their leaf-name MATCHES the leaf of one of this issue's own citations
      (the result family — e.g. a stale ``Foo.IsSmooth`` surfaces a renamed
      ``Bar.IsSmooth``, never an unrelated ``Bar.pullback``).

    Each surfaced decl is a full :class:`DeclView` via the shared
    :func:`_build_decl_view` (same source / kernel / evidence / tier), flagged
    ``surfaced=True``. Deterministic (sorted), capped at
    :data:`_MAX_SURFACED_DECLS`. **Pure + read-only** (snapshot-based; writes
    nothing). ``[]`` when there is no snapshot."""
    if ctx.snapshot is None or not ctx.tiers_by_decl:
        return []

    exclude_names = exclude_names or set()
    result_keys = result_keys or set()

    # The set of decls the body already cites, resolved to snapshot names so we
    # never re-surface a cited decl (matching is on the resolved/qualified name).
    cited_resolved = {
        _resolve_to_snapshot(ctx, raw) or raw for raw in cited_names
    }
    # Without textbook-result keys (an un-numbered "def" issue) the body's
    # citations are authoritative — surface nothing.
    if not result_keys:
        return []

    namespaces = _namespace_prefixes(sorted(cited_resolved))
    if not namespaces:
        return []

    # Candidate names: a DIRECT member of one of the issue's namespaces, not
    # already cited here AND not cited by any OTHER chapter issue (so a sibling
    # result's decls never surface in). Deterministic order.
    candidates: list[str] = []
    seen: set[str] = set()
    for name in sorted(ctx.tiers_by_decl):
        if name in cited_resolved or name in seen or name in exclude_names:
            continue
        for ns in namespaces:
            prefix = ns + "."
            if name.startswith(prefix) and "." not in name[len(prefix):]:
                candidates.append(name)
                seen.add(name)
                break

    # Keep only candidates whose OWN DOCSTRING references the issue's textbook
    # result (e.g. "Proposition 12.19(b)") — the signal that a decl is ANOTHER
    # PART of the same result (the missing (b)/(d) characterizations), vs
    # unrelated plumbing (algebra instances, pullback helpers) that merely shares
    # the namespace. Matched with a boundary-anchored pattern against the decl's
    # leading docstring (so "12.19" never matches "12.190" and a number in the
    # proof body / a bled-in neighbour docstring can't trigger it).
    key_patterns = {k: _result_key_pattern(k) for k in sorted(result_keys)}
    out: list[DeclView] = []
    for name in candidates:
        if len(out) >= _MAX_SURFACED_DECLS:
            break
        view = _build_decl_view(ctx, ledger, name, repo_dir)
        match = _docstring_result_key(view.source_block, key_patterns)
        if match is None:
            continue
        out.append(replace(
            view,
            surfaced=True,
            surfaced_reason=f"another part of {match} — in the code, not cited by the issue",
        ))
    return out


def surfaced_decls_for_issue(
    cfg: "ReviewConfig", issue_num: int, cited_names: list[str]
) -> list[DeclView]:
    """Public, PURE entry point for the RECONCILIATION's surfacing step: the
    DeclViews for decls present in the code under ``issue_num``'s cited
    namespaces but NOT cited by ``cited_names`` (the body's decls).

    Builds the same read-only snapshot/ledger context a card build uses, then
    delegates to :func:`_surface_namespace_decls`. ``cited_names`` is the raw
    list the issue body cites (the subdeck already has it from the kept views'
    names) so this never re-parses the body. Writes nothing; returns ``[]``
    when there is no audit snapshot. Used by :func:`subdeck.build_subdeck`.

    Surfacing excludes every decl cited by ANOTHER issue in the same chapter,
    so a sibling result's declarations (e.g. the pullback issue's
    ``*.pullback`` decls) never surface into this one."""
    chapter = cfg.chapter_of_issue(issue_num)
    chapter_nums = _chapter_issue_nums(cfg, chapter)
    if issue_num not in chapter_nums:
        chapter_nums = [issue_num, *chapter_nums]
    ctx = _load_context(cfg, chapter_nums)
    ledger = _ledger(cfg)

    # Decls cited by OTHER chapter issues — never surface a sibling result's
    # declarations into this subdeck. Resolved to snapshot names for matching.
    exclude: set[str] = set()
    for other in chapter_nums:
        if other == issue_num:
            continue
        for raw in _decls_for_issue(ctx, other):
            exclude.add(_resolve_to_snapshot(ctx, raw) or raw)

    # Textbook-result key(s) from the issue title (e.g. {"12.19"}): a candidate
    # only surfaces when its docstring references the SAME result.
    meta = ctx.issue_meta.get(issue_num) or {}
    result_keys = _textbook_result_keys(meta.get("title") or "")

    return _surface_namespace_decls(
        ctx, cited_names, ledger, Path(cfg.repo_dir),
        exclude_names=exclude, result_keys=result_keys,
    )


def missing_suggestions_for_issue(
    cfg: "ReviewConfig", issue_num: int
) -> list[dict]:
    """PURE read: declarations that the issue's title-promised result may
    ALREADY be formalized as — surfaced when the reviewer opens "flag a missing
    declaration", so a false "missing" doesn't dispatch a REDUNDANT Aristotle
    ADD (the #94 incident: the "function multiplication" case the reviewer
    flagged missing already existed as ``…IsSmooth.smul_of_contMDiff``, owned by
    sibling issue #92).

    The candidate pool is the CURRENT CODE (built from a SINGLE repo scan): the
    decls that are direct members of one of the issue's cited namespaces and that
    the issue does NOT itself cite. A candidate is kept only when its OWN leading
    docstring references the issue's textbook result-key — matched with a
    boundary-anchored pattern (so ``12.2`` never matches ``12.22``) against the
    docstring alone (so a number in the proof body, or a neighbouring decl's
    docstring bled in by a loose source slice, can't trigger it).

    Scanning the code — not the snapshot or the issue bodies — is load-bearing:
    the #94 case is a POST-snapshot RENAME (``smul_smoothFunction`` →
    ``smul_of_contMDiff`` in PR #107), so the renamed decl exists ONLY in the
    code; neither the snapshot nor any body knows the new name. Only an IN-CODE
    decl can be "already formalized", so the code is also the authoritative pool.

    Writes nothing; fires no verdict. Returns advisory dicts (``decl``,
    ``matched_key``, ``cited_by`` — possibly empty for an uncited duplicate,
    ``in_code``, ``source_file``, ``snippet``), ranked sibling-owned-first then
    by name, capped at :data:`_MAX_MISSING_SUGGESTIONS`. ``[]`` when the title is
    un-numbered or there is no scannable code."""
    chapter = cfg.chapter_of_issue(issue_num)
    chapter_nums = _chapter_issue_nums(cfg, chapter)
    if issue_num not in chapter_nums:
        chapter_nums = [issue_num, *chapter_nums]
    ctx = _load_context(cfg, chapter_nums)
    repo_dir = Path(cfg.repo_dir)

    # Un-numbered ("def") issues have no result to be "missing a part of" — the
    # body's citations are authoritative (parity with surfacing).
    meta = ctx.issue_meta.get(issue_num) or {}
    result_keys = _textbook_result_keys(meta.get("title") or "")
    if not result_keys:
        return []
    # "Already formalized" means present in the CODE — need a scannable Lean repo.
    if not _scan_dir_has_lean(repo_dir):
        return []

    # The issue's OWN citations (resolved) + their namespaces — the search scope.
    cited_self = _decls_for_issue(ctx, issue_num)
    cited_resolved = {
        _resolve_to_snapshot(ctx, raw) or raw for raw in cited_self
    }
    namespaces = _namespace_prefixes(sorted(cited_resolved))
    if not namespaces:
        return []

    # Which chapter issue(s) cite each decl (resolved-or-raw name → issues) — for
    # the cited_by annotation (a sibling owning the decl is a stronger signal).
    decl_to_issues = _decl_to_issues(ctx, chapter_nums)
    # Boundary-anchored key patterns, built ONCE in sorted-key order (so the
    # reported matched_key is deterministic for a multi-key title).
    key_patterns = {k: _result_key_pattern(k) for k in sorted(result_keys)}

    # The candidate pool IS the current code: direct namespace members with their
    # source, from a single indexed scan (no per-candidate repo rescan).
    code_index = _code_decls_in_namespaces(repo_dir, namespaces)

    matches: list[dict] = []
    for name in sorted(code_index):
        if name in cited_resolved:
            continue
        source_block, source_file = code_index[name]
        match = _docstring_result_key(source_block, key_patterns)
        if match is None:
            continue
        cited_by = sorted(decl_to_issues.get(name, set()) - {issue_num})
        matches.append({
            "decl": name,
            "matched_key": match,
            "cited_by": cited_by,
            "in_code": True,
            "source_file": source_file,
            "snippet": _doc_summary(source_block),
        })
    # Rank sibling-OWNED first (a decl another issue explicitly cites is a
    # stronger "already exists" signal than a same-namespace uncited match),
    # then by name. Then cap.
    matches.sort(key=lambda m: (not m["cited_by"], m["decl"]))
    return matches[:_MAX_MISSING_SUGGESTIONS]


def _textbook_result_keys(title: str) -> "set[str]":
    """Textbook-result numbers in an issue title, e.g. ``"Proposition 12.19 —
    …"`` → ``{"12.19"}`` and ``"Lemma 12.24/12.26"`` → ``{"12.24", "12.26"}``.

    These key the namespace-surfacing: a same-namespace decl is surfaced only
    when its source docstring references one of these numbers (so the other
    parts of a multi-part result are pulled in, but unrelated plumbing is not).
    Empty for un-numbered issues (e.g. a "def" issue) — then nothing surfaces."""
    import re

    return set(re.findall(r"\b\d+\.\d+\b", title or ""))


def build_card_detail(cfg: "ReviewConfig", issue_num: int) -> CardDetail:
    """Assemble the full spec card for one issue.

    A card carries a :class:`DeclView` for EVERY declaration the issue cites
    (an issue bundles all the decls of one textbook result), each with its
    own source / kernel / evidence / tier / informal rendering, ordered
    worst-tier first. The card-level ``tier`` is the worst of these (the
    gate). The top-level scalar fields mirror the headline (``decls[0]``)
    for backward-compatible / degraded rendering. Pure + read-only."""
    from marathon.audit.trust import TIER_ORDER

    ctx = _load_context(cfg, [issue_num])
    tier, qualifiers, decl = _issue_tier(ctx, issue_num)
    chapter = cfg.chapter_of_issue(issue_num)

    # One view per cited declaration, de-duplicated by resolved name,
    # ordered worst-tier first (the gating decl leads), then by name.
    ledger = _ledger(cfg)
    repo_dir = Path(cfg.repo_dir)
    views: list[DeclView] = []
    seen: set[str] = set()
    for raw in _decls_for_issue(ctx, issue_num):
        view = _build_decl_view(ctx, ledger, raw, repo_dir)
        if view.name in seen:
            continue
        seen.add(view.name)
        views.append(view)
    views.sort(
        key=lambda v: (
            TIER_ORDER.index(v.tier) if v.tier in TIER_ORDER else 0,
            v.name,
        )
    )

    # Re-classify api with the issue's HEADLINE decl now known (the worst-tier
    # decl that leads the sorted views) so the headline is never auto-approved.
    headline_name = views[0].name if views else None
    views = [
        replace(v, **dict(zip(("api", "api_reason"),
                             classify_api(v, headline_name))))
        for v in views
    ]

    # Top-level scalar fields mirror the headline view (decls[0]) for
    # backward-compatible / degraded clients.
    head = views[0] if views else None
    statement_pp = head.statement_pp if head else None
    source_block = head.source_block if head else None
    source_file = head.source_file if head else None
    informal_rendering = head.informal_rendering if head else None
    informal_is_llm = head.informal_is_llm if head else False
    kernel = list(head.kernel) if head else []
    evidence = dict(head.evidence) if head else {
        "axioms_beyond_whitelist": [], "sorry": None, "deception_tags": [],
    }
    semantic_delta = head.semantic_delta if head else None

    # Issue-derived fields (permalink, iteration log) — pure read.
    meta = ctx.issue_meta.get(issue_num) or {}
    permalink = _issue_permalink(cfg, issue_num)
    iteration_log = _iteration_log(cfg, issue_num, meta)
    # The issue's TOP-LEVEL informal statement (the body's ### Informal
    # Statement) — the one text the leading approval card shows.
    issue_informal, issue_informal_is_llm = _issue_informal_statement(
        meta.get("body")
    )

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
        decls=views,
        statement_pp=statement_pp,
        source_block=source_block,
        source_file=source_file,
        informal_rendering=informal_rendering,
        informal_is_llm=informal_is_llm,
        issue_informal=issue_informal,
        issue_informal_is_llm=issue_informal_is_llm,
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
