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

import hashlib
import json
import os
import re
import threading
import time
from dataclasses import dataclass, field, replace
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
    # ``ready`` remains the backwards-compatible shipping predicate.
    # ``inspectable`` deliberately ignores unresolved predecessor issues while
    # preserving every machine-audit/current-code gate, allowing a human to
    # review downstream work provisionally. ``shippable`` is an explicit alias
    # for ``ready`` so clients never mistake visibility for permission.
    inspectable: bool = False
    shippable: bool = False
    provisional: bool = False
    inspection_blocked_reason: Optional[str] = None

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
            "inspectable": self.inspectable,
            "shippable": self.shippable,
            "provisional": self.provisional,
            "inspection_blocked_reason": self.inspection_blocked_reason,
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
    # Hash of the complete declaration source, even when ``source_block`` is
    # truncated for the browser payload.  Review decisions bind this value so
    # an edit below the rendering limit cannot preserve a stale approval.
    source_fingerprint: Optional[str] = None

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
            "source_fingerprint": self.source_fingerprint,
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
    tier: str  # the WORST tier over the reconciled current-code review set
    qualifiers: list[str]
    # Issue-level inspection gate.  Unlike shipping readiness this ignores
    # unresolved predecessor issues, but still requires current, build-clean,
    # machine-audited code and an unresolved review-state issue.
    inspectable: bool
    inspection_blocked_reason: Optional[str]
    shippable: bool
    ship_blocked_reason: Optional[str]
    provisional: bool
    # Every declaration this issue bundles, each with its own source /
    # kernel / evidence / tier / informal rendering, ordered worst-tier
    # first. The top-level scalar fields below mirror the reconciled headline
    # (which may be surfaced when every body citation went stale).
    decls: list[DeclView]
    # Current code declarations recovered by the rename/split reconciler.
    # Kept separate from ``decls`` so older detail consumers can still inspect
    # body-cited stale names, while subdeck/workspace gates use both sets.
    surfaced_decls: list[DeclView]
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
            "inspectable": self.inspectable,
            "inspection_blocked_reason": self.inspection_blocked_reason,
            "shippable": self.shippable,
            "ship_blocked_reason": self.ship_blocked_reason,
            "provisional": self.provisional,
            "decls": [d.to_json() for d in self.decls],
            "surfaced_decls": [d.to_json() for d in self.surfaced_decls],
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
class _ReconciledIssueSurface:
    """One canonical current-code surface shared by every review view."""

    issue: int
    cited_names: list[str]
    cited_views: list[DeclView]
    surfaced_views: list[DeclView]
    review_views: list[DeclView]
    stale_views: list[DeclView]
    tier: str
    qualifiers: list[str]
    headline: str
    error: Optional[str] = None


@dataclass(frozen=True)
class Queue:
    """The dep-ordered card queue plus live counters for the status pane."""

    cards: list[CardSummary]
    building: int
    landed_today: int
    view_building: int = 0
    unresolved: int = 0
    awaiting_refinement: int = 0
    needs_attention: int = 0
    needs_citation_mapping: int = 0
    awaiting_audit: int = 0
    dependency_blocked: int = 0
    other_unresolved: int = 0

    def to_json(self) -> dict:
        return {
            "cards": [c.to_json() for c in self.cards],
            "building": self.building,
            "landed_today": self.landed_today,
            "view_building": self.view_building,
            "unresolved": self.unresolved,
            "awaiting_refinement": self.awaiting_refinement,
            "needs_attention": self.needs_attention,
            "needs_citation_mapping": self.needs_citation_mapping,
            "awaiting_audit": self.awaiting_audit,
            "dependency_blocked": self.dependency_blocked,
            "other_unresolved": self.other_unresolved,
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
    issue_states: dict  # {issue_num: IssueState} local operational lifecycle
    repo_dir: object = None  # Path — for reading failed modules' source
    # Bare decl names defined in modules the last audit recorded as FAILING to
    # build. An issue citing one references code that does not compile → its own
    # decls are absent from the snapshot, so it must not read as a ready card.
    broken_module_decls: "set[str]" = field(default_factory=set)
    issue_chapter: dict = field(default_factory=dict)  # {issue_num: chapter}


# A whole-deck GraphQL failure must not turn one HTTP request into one slow REST
# request per registered issue.  The REST helper already runs eight workers with
# a ten-second per-issue timeout; limiting the deck fallback to one worker-pool
# wave keeps the degraded path bounded while still recovering every metadata gap
# in the normal (small/partial-failure) case.  Numbers beyond the cap remain
# absent from ``issue_meta`` and therefore render as explicit ``#<num>`` /
# no-evidence placeholders rather than pretending stale metadata is current.
_DECK_REST_METADATA_FALLBACK_LIMIT = 8


#: One completed ``_DeckContext`` per (issue set, REST limit), reused while
#: every LOCAL truth it was built from is byte-identical (state.json, the
#: audit snapshot, config) and its GitHub metadata is younger than the TTL.
#: This is the memo that keeps card/subdeck opens and per-swipe verdict
#: validation off the network: fingerprint-bearing data (source blocks,
#: elaborated statements, kernels) is read fresh by the surface builders on
#: every call, so the cache can only ever staleness GitHub issue bodies —
#: bounded by the TTL and by explicit invalidation on every mutating POST.
_CONTEXT_CACHE_TTL_SECONDS = float(
    os.environ.get("MARATHON_DECK_CONTEXT_TTL", "60")
)
_context_cache_lock = threading.Lock()
_context_cache: dict[tuple, "_ContextCacheEntry"] = {}


@dataclass
class _ContextCacheEntry:
    payload: "_DeckContext"
    freshness: tuple
    built_at: float
    refreshing: bool = False


def _context_freshness(cfg: "ReviewConfig") -> tuple:
    """Stat the local truths a cached context depends on.

    Any change in review state (a ship — deck OR CLI), the audit snapshot,
    or the registry invalidates the memo; mtime_ns + size is enough because
    every writer uses atomic replacement.
    """
    def _stat(path: Path) -> tuple:
        try:
            st = path.stat()
            return (st.st_mtime_ns, st.st_size)
        except OSError:
            return (None, None)

    marathon_dir = Path(cfg.repo_dir) / ".marathon"
    return (
        _stat(marathon_dir / "review" / "state.json"),
        _stat(marathon_dir / "audit" / "latest.json"),
        _stat(marathon_dir / "review" / "config.toml"),
    )


def invalidate_context_cache() -> None:
    """Drop every memoized deck context AND the per-issue detail memo
    (mutating POSTs, ship completion)."""
    with _context_cache_lock:
        _context_cache.clear()
    with _detail_cache_lock:
        _detail_cache.clear()


#: Per-issue assembled CardDetail memo. The context cache above removed
#: GitHub from the hot path; this removes the ~seconds of LOCAL assembly
#: (per-chapter reconciled surfaces, the global dependency model, source
#: rendering) that every subdeck/card open re-paid. Freshness covers every
#: fingerprint-bearing input — the context signature (state.json, audit
#: snapshot, registry) plus the Lean source-inventory token — so a served
#: detail is always current where it matters; the TTL only bounds issue-body
#: drift, exactly like the queue snapshot, and an expired-but-source-current
#: entry is served stale while one background rebuild refreshes it.
_DETAIL_CACHE_TTL_SECONDS = float(
    os.environ.get("MARATHON_DECK_DETAIL_TTL", "60")
)
_detail_cache_lock = threading.Lock()
_detail_cache: dict[tuple, "_DetailCacheEntry"] = {}


@dataclass
class _DetailCacheEntry:
    detail: "CardDetail"
    freshness: tuple
    built_at: float
    refreshing: bool = False


def _detail_freshness(cfg: "ReviewConfig") -> Optional[tuple]:
    """Every local truth an assembled detail depends on; None = don't cache."""
    from marathon.deck.definitions import definition_source_token

    try:
        token = definition_source_token(Path(cfg.repo_dir))
    except Exception:  # noqa: BLE001 — unknown source state: build fresh
        return None
    return (_context_freshness(cfg), token)


def build_card_detail_cached(
    cfg: "ReviewConfig", issue_num: int
) -> "CardDetail":
    """Serve :func:`build_card_detail` from the per-issue memo.

    A signature mismatch (source, audit, state, or registry changed) rebuilds
    synchronously — fingerprint-bearing data is never served stale.  A lapsed
    TTL with a matching signature serves the cached detail immediately and
    rebuilds once in the background (issue-body drift only).
    """
    key = (str(cfg.repo_dir), int(issue_num))
    freshness = _detail_freshness(cfg)
    if freshness is None:
        return build_card_detail(cfg, issue_num)
    now = time.monotonic()
    with _detail_cache_lock:
        entry = _detail_cache.get(key)
        if entry is not None and entry.freshness == freshness:
            if now - entry.built_at <= _DETAIL_CACHE_TTL_SECONDS:
                return entry.detail
            if entry.refreshing:
                return entry.detail
            entry.refreshing = True
            stale_detail = entry.detail

            def _refresh() -> None:
                try:
                    pre = _detail_freshness(cfg)
                    fresh = build_card_detail(cfg, issue_num)
                    if pre is not None:
                        with _detail_cache_lock:
                            _detail_cache[key] = _DetailCacheEntry(
                                detail=fresh,
                                freshness=pre,
                                built_at=time.monotonic(),
                            )
                except Exception:  # noqa: BLE001 — keep serving the stale memo
                    with _detail_cache_lock:
                        current = _detail_cache.get(key)
                        if current is not None:
                            current.refreshing = False

            threading.Thread(
                target=_refresh, name="deck-detail-refresh", daemon=True
            ).start()
            return stale_detail
    detail = build_card_detail(cfg, issue_num)
    with _detail_cache_lock:
        _detail_cache[key] = _DetailCacheEntry(
            detail=detail,
            freshness=freshness,
            built_at=time.monotonic(),
        )
    return detail


def _load_context(
    cfg: "ReviewConfig",
    issue_nums: list[int],
    *,
    rest_metadata_limit: Optional[int] = None,
) -> _DeckContext:
    """Freshness-guarded memo over :func:`_load_context_uncached`.

    Serves the cached context while the local truths are unchanged; when only
    the TTL has lapsed (possible GitHub body drift) it serves the cached value
    immediately and refreshes once in a background thread, so no golden-path
    request ever blocks on GitHub twice for the same registry.
    """
    key = (
        tuple(dict.fromkeys(int(n) for n in issue_nums)),
        rest_metadata_limit,
        str(cfg.repo_dir),
    )
    freshness = _context_freshness(cfg)
    now = time.monotonic()
    with _context_cache_lock:
        entry = _context_cache.get(key)
        if entry is not None and entry.freshness == freshness:
            if now - entry.built_at <= _CONTEXT_CACHE_TTL_SECONDS:
                return entry.payload
            if entry.refreshing:
                return entry.payload
            entry.refreshing = True
            stale_payload = entry.payload

            def _refresh() -> None:
                try:
                    # Stamp the freshness observed BEFORE the rebuild: if a
                    # local truth changes mid-build, the next read's signature
                    # mismatch forces a synchronous rebuild instead of trusting
                    # a context that may straddle the change.
                    pre_freshness = _context_freshness(cfg)
                    fresh = _load_context_uncached(
                        cfg, issue_nums,
                        rest_metadata_limit=rest_metadata_limit,
                    )
                    with _context_cache_lock:
                        _context_cache[key] = _ContextCacheEntry(
                            payload=fresh,
                            freshness=pre_freshness,
                            built_at=time.monotonic(),
                        )
                except Exception:  # noqa: BLE001 — keep serving the stale memo
                    with _context_cache_lock:
                        current = _context_cache.get(key)
                        if current is not None:
                            current.refreshing = False

            threading.Thread(
                target=_refresh, name="deck-context-refresh", daemon=True
            ).start()
            return stale_payload
    payload = _load_context_uncached(
        cfg, issue_nums, rest_metadata_limit=rest_metadata_limit
    )
    with _context_cache_lock:
        _context_cache[key] = _ContextCacheEntry(
            payload=payload,
            freshness=freshness,
            built_at=time.monotonic(),
        )
    return payload


def _load_context_uncached(
    cfg: "ReviewConfig",
    issue_nums: list[int],
    *,
    rest_metadata_limit: Optional[int] = None,
) -> _DeckContext:
    """Read the snapshot, tiers, issue metadata, and verdict statuses for
    ``issue_nums`` — all read-only, all degrading.

    ``rest_metadata_limit`` bounds only the REST recovery after a bulk GraphQL
    gap.  Correctness-sensitive reconciliation callers leave it unset so they
    still see every sibling issue body; the broad queue read supplies one
    worker-pool wave so its HTTP latency remains bounded.
    """
    # Local imports: keep this module importable on a checkout whose audit
    # / ledger deps are lazy (mirrors the review CLI's audit↔review
    # boundary discipline).
    from marathon.audit.engine import load_snapshot
    from marathon.review.github import fetch_issues_bulk, fetch_issues_rest_bulk

    snapshot = load_snapshot(cfg.repo_dir)
    tiers_by_decl = _tiers_by_decl(cfg, snapshot)
    requested = list(dict.fromkeys(int(n) for n in issue_nums))
    try:
        issue_meta = fetch_issues_bulk(requested, cfg.github_repo) or {}
    except Exception:  # noqa: BLE001 - read-side GitHub outage degrades honestly
        issue_meta = {}
    # Preserve the one-call GraphQL fast path. REST is live, uncached, and only
    # fills numbers GraphQL did not return (including whole-call failure). Broad
    # queue callers apply a limit; completeness-sensitive callers do not.
    missing = [n for n in requested if n not in issue_meta]
    if rest_metadata_limit is not None:
        missing = missing[:max(0, rest_metadata_limit)]
    if missing:
        try:
            issue_meta.update(fetch_issues_rest_bulk(missing, cfg.github_repo))
        except Exception:  # noqa: BLE001 - metadata is optional deck context
            pass
    try:
        from marathon.review.state import load_state

        issue_states = load_state(cfg).issues
    except Exception:  # noqa: BLE001 — missing state degrades to attention
        issue_states = {}
    verdict_status = _verdict_status(cfg, issue_meta, issue_states)
    return _DeckContext(
        snapshot=snapshot,
        tiers_by_decl=tiers_by_decl,
        issue_meta=issue_meta,
        verdict_status=verdict_status,
        issue_states=issue_states,
        repo_dir=cfg.repo_dir,
        broken_module_decls=_failed_module_decls(cfg.repo_dir, snapshot),
        issue_chapter={
            n: ch
            for ch, reg in (getattr(cfg, "chapters", None) or {}).items()
            for (n, *_rest) in (getattr(reg, "entries", None) or [])
        },
    )


#: Matches the audit snapshot's build-failure entries: ``module <M> failed to
#: build; …`` (see :func:`marathon.audit.engine.run_audit`).
_FAILED_MODULE_RE = re.compile(r"module\s+(\S+)\s+failed to build")


def _failed_module_decls(repo_dir, snapshot) -> "set[str]":
    """Bare decl names defined in modules the last audit recorded as FAILING to
    build. Their declarations are excluded from the snapshot, so an issue that
    cites one references a module that does not compile — its own statements have
    no audit evidence and it is not reviewable until the module is repaired.

    Read straight from source: the failed module's ``.lean`` file exists even
    though it will not build, and its declaration keywords are still parseable.
    Best-effort — an unreadable file just contributes nothing."""
    if snapshot is None or repo_dir is None:
        return set()
    from marathon.review.approved_statements import _DECL_RE

    out: set[str] = set()
    for f in getattr(snapshot, "failures", None) or ():
        m = _FAILED_MODULE_RE.search(f or "")
        if not m:
            continue
        rel = m.group(1).replace(".", "/") + ".lean"
        try:
            text = (Path(repo_dir) / rel).read_text()
        except OSError:
            continue
        for dm in _DECL_RE.finditer(text):
            out.add(dm.group("name").split(".")[-1])  # bare suffix
    return out


def _issue_build_broken(ctx: _DeckContext, num: int) -> Optional[str]:
    """The first decl issue ``num`` cites that belongs to a module the audit
    recorded as FAILING to build (defined there, absent from the snapshot), or
    ``None``. Such an issue references code that does not compile, so its own
    statement carries no audit evidence — it must not surface as a ready card
    (the misleading "Stokes shows T1/ready because its one resolvable citation is
    an upstream Ch15 decl" case). Requires a genuine snapshot absence too, so a
    coincidental bare-name collision with a BUILT decl never blocks."""
    broken = getattr(ctx, "broken_module_decls", None)
    if not broken:
        return None
    for c in _decls_for_issue(ctx, num):
        if c.split(".")[-1] in broken and _resolve_to_snapshot(ctx, c) is None:
            return c
    return None


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


def _verdict_status(
    cfg: "ReviewConfig",
    issue_meta: Optional[dict] = None,
    issue_states: Optional[dict] = None,
) -> dict:
    """``{issue_num: status}`` for the readiness gate.

    Layered, with GitHub authoritative for the human verdict and local state
    authoritative for the rejected-vs-stalled operational lifecycle:

    1. the deck's own DEFER markers (deck-local; touch neither Aristotle nor
       GitHub — see :mod:`marathon.deck.verdicts`);
    2. the committed local review state (``state.json`` — operational truth
       like ``'stalled'``, plus any locally-recorded verdicts);
    3. **the GitHub labels** (``review:verified`` / ``review:rejected``),
       which are AUTHORITATIVE for human verdicts (the Phase-1 sync policy).
       A card verified on GitHub drops out of the swipe queue even when the
       local ``state.json`` never recorded it
       (the documented state.json-lags-labels drift; recon §2). The labels
       come from ``issue_meta`` the queue already fetched, so this costs no
       extra GitHub call. A rejected label does not demote a local ``stalled``
       state back into the conductor's dispatchable queue.

    A missing/corrupt source on any layer is an empty contribution, never an
    error."""
    from marathon.deck.verdicts import deferred_issue_nums
    from marathon.review.review import _status_from_labels

    out: dict[int, str] = {}
    try:
        for num in deferred_issue_nums(cfg):
            out[num] = "deferred"
    except Exception:  # noqa: BLE001 — no defers → no overlay
        pass
    if issue_states is None:
        try:
            from marathon.review.state import load_state

            issue_states = load_state(cfg).issues
        except Exception:  # noqa: BLE001 — degrade to "no local verdicts"
            issue_states = {}
    for num, st in issue_states.items():
        out[num] = st.status
    # GitHub labels win for human verdicts (verified/rejected), but a rejected
    # label must not erase the local *operational* distinction between
    # ``rejected`` (dispatchable) and ``stalled`` (parked).  The label only
    # says what the human decided; state.json says whether the conductor can
    # still act on that decision.
    for num, meta in (issue_meta or {}).items():
        labels = meta.get("labels")
        if not labels:
            continue
        gh_status = _status_from_labels(cfg, set(labels))
        if gh_status == "verified":
            out[num] = gh_status
        elif gh_status == "rejected" and out.get(num) != "stalled":
            out[num] = gh_status
    return out


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


def _issue_needs_citation_mapping(ctx: _DeckContext, num: int) -> bool:
    """Whether a fetched issue body contains no Lean declaration citations.

    This is deliberately distinct from missing GitHub metadata: a failed or
    capped metadata fetch must not make a healthy card look as if a human
    deleted its mapping.  Once the body is present, however, an empty
    ``### Lean declarations`` / ``### Lean signatures`` section is actionable
    issue drift, not something another machine-audit run can repair.
    """
    meta = ctx.issue_meta.get(num)
    return (
        meta is not None
        and meta.get("body") is not None
        and not _decls_for_issue(ctx, num)
    )


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
    # A build-broken issue (its module failed to build → its own decls are
    # absent from the snapshot) must NOT inherit a tier from the one upstream
    # citation that happens to resolve. Floor it to '-' (no evidence) and headline
    # the broken decl, so the readiness gate holds it out of the queue.
    broken_decl = _issue_build_broken(ctx, num)
    if broken_decl is not None:
        return "-", [], broken_decl
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


def _decl_to_issues(
    ctx: _DeckContext,
    issue_nums: list[int],
    owned_decls: Optional[dict[int, list[str]]] = None,
) -> dict:
    """``{snapshot_decl_name: set[issue_num]}`` — which issue(s) cite each
    decl. Used to resolve a card's cone/planner deps (decl names) back to
    predecessor CARDS (issues)."""
    out: dict[str, set] = {}
    for num in issue_nums:
        names = (
            owned_decls.get(num, [])
            if owned_decls is not None
            else _decls_for_issue(ctx, num)
        )
        for raw in names:
            resolved = _resolve_to_snapshot(ctx, raw)
            key = resolved if resolved is not None else raw
            out.setdefault(key, set()).add(num)
    return out


def _dependency_declarations(
    ctx: _DeckContext,
    num: int,
    owned_decls: Optional[dict[int, list[str]]] = None,
) -> set[str]:
    """Project-local declarations issue ``num`` depends on, excluding itself."""
    dep_decls: set[str] = set()
    own_names = (
        owned_decls.get(num, [])
        if owned_decls is not None
        else _decls_for_issue(ctx, num)
    )
    own_decls = {
        _resolve_to_snapshot(ctx, raw) or raw for raw in own_names
    }
    if ctx.snapshot is not None:
        from marathon.audit.kernel import compute_kernel

        for decl in own_decls:
            if decl is None:
                continue
            kernel = compute_kernel(decl, ctx.snapshot)
            dep_decls.update(m.name for m in kernel.members)
            dep_decls.update(kernel.local_lemmas)
    dep_decls -= own_decls
    return dep_decls


def _predecessor_issues(
    ctx: _DeckContext,
    num: int,
    issue_nums: list[int],
    decl_to_issues: dict,
    owned_decls: Optional[dict[int, list[str]]] = None,
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
    # A decl the issue OWNS is not an external dependency. The shared helper
    # removes those self declarations before ownership is projected to issues.
    dep_decls = _dependency_declarations(ctx, num, owned_decls)

    in_queue = set(issue_nums)
    a_chapter = ctx.issue_chapter.get(num)
    preds: set[int] = set()
    for dep_decl in dep_decls:
        for owner in decl_to_issues.get(dep_decl, ()):
            if owner == num or owner not in in_queue:
                continue
            # A genuine dependency-predecessor never lives in a LATER chapter:
            # cross-chapter dependencies flow FORWARD (a later chapter uses an
            # earlier one), so a later-chapter issue that merely CITES an upstream
            # decl is a downstream dependent, not a predecessor — dropping it kills
            # the backwards #244→#170/#171 edges. Only compared when both chapters
            # are known, so a missing chapter never silently drops a real edge.
            o_chapter = ctx.issue_chapter.get(owner)
            if a_chapter is not None and o_chapter is not None and o_chapter > a_chapter:
                continue
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


def _base_review_eligibility(
    ctx: _DeckContext,
    num: int,
    tier: str,
) -> tuple[bool, Optional[str]]:
    """Current-code/machine-audit gates shared by inspect and ship."""
    from marathon.audit.trust import TIER_ORDER

    status = ctx.verdict_status.get(num)
    if status == "verified":
        return False, "already verified"
    if status == "rejected":
        local = ctx.issue_states.get(num)
        if local is not None and local.needs_iteration():
            return False, "rejected — queued for refinement"
        if local is None:
            return False, (
                "rejected — local refinement state is missing; recovery needed"
            )
        return False, (
            "rejected — refinement finished; waiting for PR merge or recovery"
        )
    if status == "stalled":
        return False, ("rejected — refinement parked (open Recover… on the "
                       "⚠ needs-attention card / Issues tab)")
    if status == "deferred":
        return False, "deferred"

    if _issue_needs_citation_mapping(ctx, num):
        return False, (
            "no Lean declarations cited — map this issue to its current "
            "declaration(s) before review"
        )
    if ctx.snapshot is None:
        return False, "no audit snapshot — run `marathon audit run` to gate it"
    broken_decl = _issue_build_broken(ctx, num)
    if broken_decl is not None:
        return False, (
            f"module for `{broken_decl}` fails to build — no audit evidence for "
            "this card's own declarations yet; repair the module first"
        )
    if tier == "-":
        return False, "no audit evidence for this card's declarations"
    if TIER_ORDER.index(tier) < TIER_ORDER.index(READY_TIER_FLOOR):
        return False, (
            f"tier {tier} below the machine-audit floor {READY_TIER_FLOOR} "
            "(not yet axiom-clean / deception-free)"
        )
    return True, None


def _inspectability(
    ctx: _DeckContext,
    num: int,
    tier: str,
) -> tuple[bool, Optional[str]]:
    """Whether the current card may be read and decided provisionally.

    Dependency predecessors are intentionally not a visibility gate.  Every
    other readiness condition still applies, so the provisional lane never
    asks a reviewer to bless stale, unelaborated, broken, or already-resolved
    code.
    """
    return _base_review_eligibility(ctx, num, tier)


def _readiness(
    ctx: _DeckContext,
    num: int,
    tier: str,
    preds: list[int],
) -> tuple[bool, Optional[str]]:
    """``(ready, blocked_reason)`` for the authoritative SHIP predicate.

    A card must pass the current-code/machine-audit gate and have every
    dependency predecessor resolved.  The browser may expose an ``inspectable``
    dependency-blocked card, but this function remains the server authority for
    irreversible shipping.
    """
    eligible, reason = _base_review_eligibility(ctx, num, tier)
    if not eligible:
        return False, reason

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


def _live_building_jobs(cfg: "ReviewConfig") -> list[dict]:
    """Serialized conductor jobs that definitely own a live worker.

    A raw ``status == 'running'`` row can outlive its worker across a reboot.
    Use conductor recovery's liveness predicate so Queue, Status, and Issues
    cannot disagree about a phantom build.
    """
    from marathon.conductor import load_jobs_snapshot, snapshot_job_is_live

    try:
        snap = load_jobs_snapshot(cfg.repo_dir)
    except Exception:  # noqa: BLE001 — droppable runtime state
        return []
    if not snap:
        return []
    return [
        job for job in snap.get("jobs", [])
        if isinstance(job, dict) and snapshot_job_is_live(job)
    ]


def _landed_today_count(cfg: "ReviewConfig") -> int:
    """The 'landed today' queue stat. The opt-in landing-queue JSONL this used
    to read is gone (default-off feature, deleted); computing this from merged
    refine PRs is a deck-signals follow-up. The Status tab's 'Recent landings'
    already shows the real merged-PR landings, so this returns 0 for now."""
    return 0


# ---------------------------------------------------------------------------
# Public builders
# ---------------------------------------------------------------------------


def _gating_deps(deps: "dict[int, list[int]]") -> "dict[int, list[int]]":
    """The predecessors that genuinely GATE each card — ``deps`` with CYCLIC
    edges removed. A predecessor ``p`` gates ``num`` only if ``num`` is NOT
    (transitively) a dependency of ``p``: a mutual/cyclic dependency (e.g.
    #237↔#243, two issues citing different parts of the same structure) can never
    be resolved by ordering, so it must not gate either card — each still gates
    on its predecessors OUTSIDE the cycle. Ordering keeps the full ``deps`` (and
    places cycle remnants deterministically); only readiness gating is broken."""
    def reachable(start: int) -> set:
        seen: set = set()
        stack = list(deps.get(start, ()))
        while stack:
            n = stack.pop()
            if n in seen:
                continue
            seen.add(n)
            stack.extend(deps.get(n, ()))
        return seen

    return {
        num: [p for p in preds if num not in reachable(p)]
        for num, preds in deps.items()
    }


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
    ctx = _load_context(
        cfg,
        issue_nums,
        rest_metadata_limit=_DECK_REST_METADATA_FALLBACK_LIMIT,
    )
    # Rename/split recovery needs current audit declarations to establish a
    # trustworthy tier.  When snapshot validation withheld every declaration
    # (for example, main moved since the last audit), scanning all stale cards
    # cannot promote one and only burns local git/source work.  Otherwise open
    # one shared ledger for the whole queue.  A broken ledger keeps the read
    # side fail-soft and simply disables surfacing.
    surface_ledger = None
    if getattr(ctx.snapshot, "decls", None):
        try:
            surface_ledger = _ledger(cfg)
        except Exception:  # noqa: BLE001 — no ledger means no surfaced tier
            pass

    surfaces: dict[int, _ReconciledIssueSurface] = {}
    if surface_ledger is not None:
        surfaces = {
            num: _build_reconciled_issue_surface(
                cfg, ctx, surface_ledger, num
            )
            for num in issue_nums
        }
    owned_decls = {
        num: [view.name for view in surface.review_views]
        for num, surface in surfaces.items()
    }
    decl_to_issues = _decl_to_issues(
        ctx, issue_nums, owned_decls if surfaces else None
    )
    deps: dict[int, list[int]] = {
        num: _predecessor_issues(
            ctx,
            num,
            issue_nums,
            decl_to_issues,
            owned_decls if surfaces else None,
        )
        for num in issue_nums
    }
    ordered = _topo_order(issue_nums, {n: set(d) for n, d in deps.items()})
    gating = _gating_deps(deps)  # cyclic edges dropped for readiness only

    cards: list[CardSummary] = []
    for num in ordered:
        surface = surfaces.get(num)
        if surface is not None:
            tier = surface.tier
            qualifiers = surface.qualifiers
            decl = surface.headline
        else:
            tier, qualifiers, decl = _issue_tier(ctx, num)
            # If a validated snapshot exists but its ledger/surface assembly
            # failed, raw citations are not enough to rule out an unaudited
            # surfaced rename/addition.  Hold the gate closed.
            if getattr(ctx.snapshot, "decls", None):
                tier, qualifiers = "-", []
        ready, blocked_reason = _readiness(ctx, num, tier, gating[num])
        inspectable, inspection_blocked_reason = _inspectability(
            ctx, num, tier
        )
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
            inspectable=inspectable,
            shippable=ready,
            provisional=inspectable and not ready,
            inspection_blocked_reason=inspection_blocked_reason,
        ))
    live_jobs = _live_building_jobs(cfg)
    live_issue_ids: set[int] = set()
    for job in live_jobs:
        try:
            live_issue_ids.add(int(job.get("issue_num")))
        except (TypeError, ValueError):
            pass

    unresolved_cards = [
        card for card in cards
        if ctx.verdict_status.get(card.id) not in RESOLVED_DEP_STATUSES
    ]
    unresolved_ids = {card.id for card in unresolved_cards}
    active_ids = unresolved_ids & live_issue_ids
    # Human verdict labels and conductor lifecycle are deliberately separate.
    # A ``review:rejected`` label can describe all of these:
    #   * a fresh rejection the conductor will dispatch;
    #   * a clean iteration whose PR now awaits merge/review;
    #   * a stalled or orphaned rejection that needs recovery.
    # Only the first is truthfully "awaiting refinement".  Derive that lane
    # from the same needs_iteration() predicate the conductor uses; fail closed
    # into attention when local operational state is absent/unreadable.
    rejected_ids = {
        card.id for card in unresolved_cards
        if ctx.verdict_status.get(card.id) in {"rejected", "stalled"}
    }
    refinement_ids = {
        issue_id for issue_id in rejected_ids
        if (
            ctx.issue_states.get(issue_id) is not None
            and ctx.issue_states[issue_id].needs_iteration()
        )
    } - active_ids
    attention_ids = rejected_ids - refinement_ids - active_ids
    mapping_ids = {
        card.id for card in unresolved_cards
        if _issue_needs_citation_mapping(ctx, card.id)
    } - active_ids - refinement_ids - attention_ids
    audit_ids = {
        card.id for card in unresolved_cards
        if not card.ready and card.blocked_reason
        and (
            "audit evidence" in card.blocked_reason
            or "audit snapshot" in card.blocked_reason
            or "fails to build" in card.blocked_reason
        )
    } - active_ids - refinement_ids - attention_ids - mapping_ids
    dependency_ids = {
        card.id for card in unresolved_cards
        if not card.ready and card.blocked_reason
        and card.blocked_reason.startswith("waiting on dependency")
    } - active_ids - refinement_ids - attention_ids - mapping_ids - audit_ids
    categorized_ids = (
        active_ids | refinement_ids | attention_ids | mapping_ids | audit_ids
        | dependency_ids
    )

    return Queue(
        cards=cards,
        building=len(live_jobs),
        landed_today=_landed_today_count(cfg),
        view_building=len(active_ids),
        unresolved=len(unresolved_ids),
        awaiting_refinement=len(refinement_ids),
        needs_attention=len(attention_ids),
        needs_citation_mapping=len(mapping_ids),
        awaiting_audit=len(audit_ids),
        dependency_blocked=len(dependency_ids),
        other_unresolved=len(unresolved_ids - categorized_ids),
    )


def issue_reviewability(
    cfg: "ReviewConfig", issue_num: int
) -> dict:
    """Fresh, global server-authoritative review/ship metadata for one issue.

    Shipping must use the global dependency graph even when the browser opened
    a chapter-filtered view.  This helper intentionally rebuilds the pure queue
    at the irreversible boundary and returns a small stable shape.
    """
    queue = build_queue(cfg, chapter=None)
    card = next((item for item in queue.cards if item.id == issue_num), None)
    if card is None:
        return {
            "issue": issue_num,
            "inspectable": False,
            "shippable": False,
            "provisional": False,
            "blocked_reason": (
                f"issue #{issue_num} is not in the current review registry"
            ),
            "inspection_blocked_reason": (
                f"issue #{issue_num} is not in the current review registry"
            ),
        }
    return {
        "issue": issue_num,
        "inspectable": card.inspectable,
        "shippable": card.shippable,
        "provisional": card.provisional,
        "blocked_reason": card.blocked_reason,
        "inspection_blocked_reason": card.inspection_blocked_reason,
    }


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


def _slice_decl_block(
    text: str, sites, target_i: int, *, truncate: bool = True
) -> str:
    """The verbatim source block for ``sites[target_i]``: the full
    ``def``/``theorem``/… body plus the docstring + attribute/modifier lines
    immediately above it, with trailing blanks, next-decl comments, and trailing
    ``section`` / ``variable`` / module-docstring (``/-! … -/``) blocks trimmed.

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

    def is_command(idx: int) -> bool:  # 1-based: a top-level command line that
        # opens the NEXT region (`section`/`namespace`/`variable`/…) — never
        # part of a decl's proof body, so a trailing one belongs to what follows.
        kw = lines[idx - 1].strip().split(maxsplit=1)
        return bool(kw) and kw[0] in {
            "section", "namespace", "end", "variable",
            "open", "attribute", "universe", "set_option",
        }

    def in_variable_block(idx: int) -> bool:  # 1-based: line idx is a binder
        # continuation of a trailing `variable` block (walk up over `[…]/{…}/(…)`
        # continuation lines to a `variable` keyword).
        i = idx
        while i > kw_line:
            s = lines[i - 1].strip()
            if s.startswith("variable"):
                return True
            if s[:1] in "[{(⦃⦅":
                i -= 1
            else:
                return False
        return False

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
        elif is_modifier(end):
            # An attribute/modifier line (@[simp], noncomputable, …) directly
            # above the NEXT decl's keyword belongs to that next decl — drop it,
            # which lets the loop go on to drop the next decl's docstring too.
            end -= 1
        elif is_command(end):
            # A top-level command after the proof (`section`, `namespace`,
            # `end`, `variable`, `open`, …) opens the NEXT region — never part
            # of this decl. Dropping it lets the loop reach a module docstring
            # (`/-! … -/`) above it and drop that too.
            end -= 1
        elif s[:1] in "[{(⦃⦅" and in_variable_block(end):
            # A continuation line of a trailing `variable` binder block.
            end -= 1
        else:
            break

    block_lines = lines[start - 1 : end]
    if truncate and len(block_lines) > _SOURCE_BLOCK_MAX_LINES:
        block_lines = block_lines[:_SOURCE_BLOCK_MAX_LINES] + [
            f"  -- … ({len(block_lines) - _SOURCE_BLOCK_MAX_LINES} more "
            "lines truncated — open the deep-dive to read the rest) …"
        ]
    return "\n".join(block_lines)


def _complete_source_fingerprint(
    repo_dir: Optional[Path],
    source_file: Optional[str],
    decl_name: str,
    rendered_block: Optional[str],
) -> Optional[str]:
    """Hash the complete declaration slice behind a rendered source block.

    Most declarations fit in ``rendered_block`` and need no second read.  For
    the rare block truncated at 400 lines, reopen only its known source file
    and slice without the payload cap.  If that best-effort lookup fails, hash
    the rendered block: conservative callers still retain statement/kernel
    binding, while normal live cards receive the full-source pin.
    """
    if rendered_block is None:
        return None
    complete = rendered_block
    if (
        "more lines truncated — open the deep-dive" in rendered_block
        and repo_dir is not None
        and source_file
    ):
        try:
            root = Path(repo_dir).resolve()
            path = (root / source_file).resolve(strict=True)
            path.relative_to(root)
            if (
                path.is_file()
                and path.suffix.lower() == ".lean"
                and path.stat().st_size <= _REPO_SCAN_MAX_FILE_BYTES
            ):
                text = path.read_text(encoding="utf-8", errors="replace")
                sites, target_i = _locate_decl_site(text, decl_name)
                if target_i is not None:
                    complete = _slice_decl_block(
                        text, sites, target_i, truncate=False
                    )
        except (OSError, RuntimeError, ValueError):
            pass
    return hashlib.sha256(complete.encode("utf-8")).hexdigest()


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

    source_fingerprint = _complete_source_fingerprint(
        scan_dir, source_file, name, source_block
    )
    view = DeclView(
        name=name, kind=kind, tier=tier, qualifiers=qualifiers,
        source_block=source_block, source_file=source_file,
        statement_pp=statement_pp, kernel=kernel, evidence=evidence,
        informal_rendering=informal_rendering, informal_is_llm=informal_is_llm,
        semantic_delta=semantic_delta, in_code=in_code,
        source_fingerprint=source_fingerprint,
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

    # Candidate names: not already cited here AND not cited by any OTHER chapter
    # issue (so a sibling result's decls never surface in). Two recovery shapes:
    #   (a) RENAME — a DIRECT member of one of the issue's cited namespaces
    #       (``<ns>.<leaf>``), e.g. a stale ``Foo.IsSmooth`` → renamed
    #       ``Bar.IsSmooth``.
    #   (b) SPLIT — a decl named ``<cited>_<suffix>``, e.g. a top-level
    #       ``Prop_16_1`` split into ``Prop_16_1_preserving`` /
    #       ``Prop_16_1_reversing``. The namespace scan misses these (they share a
    #       NAME prefix, not a namespace), so a split result would otherwise leave
    #       the issue with zero reviewable cards.
    # The result-key docstring filter below keeps BOTH shapes tight (only decls
    # whose docstring references THIS issue's textbook result survive).
    # Deterministic order.
    candidates: list[str] = []
    seen: set[str] = set()
    for name in sorted(ctx.tiers_by_decl):
        if name in cited_resolved or name in seen or name in exclude_names:
            continue
        in_namespace = any(
            name.startswith(ns + ".") and "." not in name[len(ns) + 1:]
            for ns in namespaces
        )
        is_split_child = any(name.startswith(c + "_") for c in cited_resolved)
        if in_namespace or is_split_child:
            candidates.append(name)
            seen.add(name)

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

    return _surfaced_decls_from_context(
        cfg, ctx, ledger, issue_num, cited_names
    )


def _surfaced_decls_from_context(
    cfg: "ReviewConfig",
    ctx: _DeckContext,
    ledger,
    issue_num: int,
    cited_names: list[str],
) -> list[DeclView]:
    """Context-reusing core for :func:`surfaced_decls_for_issue`.

    Callers must supply a context containing the issue's complete registered
    chapter.  The public subdeck path constructs that context above; the queue
    path already has it because queues are built for complete chapters.  Keeping
    this split prevents queue assembly from turning stale citations into an
    N+1 audit/GitHub reload while preserving the public helper's behavior.
    """
    chapter = cfg.chapter_of_issue(issue_num)
    chapter_nums = _chapter_issue_nums(cfg, chapter)
    if issue_num not in chapter_nums:
        chapter_nums = [issue_num, *chapter_nums]

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


def _fallback_decl_view(name: str) -> DeclView:
    """Fail-closed name-only view used when current source assembly errors."""
    return DeclView(
        name=name,
        kind=None,
        tier="-",
        qualifiers=[],
        source_block=None,
        source_file=None,
        statement_pp=None,
        kernel=[],
        evidence={
            "axioms_beyond_whitelist": [],
            "sorry": None,
            "deception_tags": [],
        },
        informal_rendering=None,
        informal_is_llm=False,
        semantic_delta=None,
        # An assembly failure is not evidence that the citation is obsolete.
        # Keep it in the review surface at tier '-' so the gate fails closed.
        in_code=True,
    )


def _build_reconciled_issue_surface(
    cfg: "ReviewConfig",
    ctx: _DeckContext,
    ledger,
    issue_num: int,
) -> _ReconciledIssueSurface:
    """Build the exact current-code declaration set reviewed for one issue.

    Queue, focused detail, file/chapter workspace, dependency ownership, and
    ship authorization all consume this object.  That prevents a stale cited
    name or a surfaced split/rename from having one tier in the list and a
    different tier at the irreversible boundary.
    """
    from marathon.audit.trust import TIER_ORDER

    cited_names = _decls_for_issue(ctx, issue_num)
    cited_views: list[DeclView] = []
    errors: list[str] = []
    seen: set[str] = set()
    for raw in cited_names:
        try:
            view = _build_decl_view(ctx, ledger, raw, Path(cfg.repo_dir))
        except Exception as exc:  # noqa: BLE001 - preserve a fail-closed card
            view = _fallback_decl_view(raw)
            errors.append(f"{raw}: {exc}")
        if view.name in seen:
            continue
        seen.add(view.name)
        cited_views.append(view)

    try:
        surfaced_views = _surfaced_decls_from_context(
            cfg, ctx, ledger, issue_num, cited_names
        )
    except Exception as exc:  # noqa: BLE001 - no guessed reviewability
        surfaced_views = []
        errors.append(f"reconciliation: {exc}")
    surfaced_views = [
        view for view in surfaced_views if view.name not in seen and view.in_code
    ]

    stale_views = [view for view in cited_views if not view.in_code]
    review_views = [
        view for view in cited_views if view.in_code
    ] + surfaced_views

    def tier_rank(view: DeclView) -> tuple[int, str]:
        try:
            rank = TIER_ORDER.index(view.tier)
        except ValueError:
            rank = -1
        return rank, view.name

    review_views.sort(key=tier_rank)
    headline = (
        review_views[0].name
        if review_views
        else (stale_views[0].name if stale_views else (cited_names[0] if cited_names else ""))
    )
    classified = {
        view.name: replace(
            view,
            **dict(zip(
                ("api", "api_reason"),
                classify_api(view, headline),
            )),
        )
        for view in review_views
    }
    review_views = [classified[view.name] for view in review_views]
    cited_views = [
        classified.get(view.name, view) for view in cited_views
    ]
    surfaced_views = [
        classified.get(view.name, view) for view in surfaced_views
    ]
    cited_views.sort(key=tier_rank)
    surfaced_views.sort(key=tier_rank)

    def covered_by_current_replacement(stale: DeclView) -> bool:
        """Whether surfacing gives this exact stale citation a safe identity.

        Split children retain the old qualified/bare name as a prefix.  A
        namespace-only rename retains the leaf and is accepted only when that
        leaf identifies one unique surfaced declaration.  Different-leaf
        renames remain blocked until reconciliation rewrites the issue body;
        merely finding some same-result declaration must never cover every
        stale citation in a multi-declaration issue.
        """
        old = stale.name
        old_leaf = old.rsplit(".", 1)[-1]
        old_is_qualified = "." in old
        split_matches = [
            view for view in surfaced_views
            if (
                view.name.startswith(old + "_")
                or (
                    not old_is_qualified
                    and view.name.rsplit(".", 1)[-1].startswith(
                        old_leaf + "_"
                    )
                )
            )
        ]
        if split_matches:
            if old_is_qualified:
                # A qualified stale citation can only be covered by children
                # of that exact qualified name.  A same-leaf child in another
                # namespace belongs to a different declaration.
                return True
            # Bare-name recovery is safe only when the stale leaf is unique.
            # Multiple qualified citations with the same leaf must not all be
            # "covered" by one surfaced split in one namespace.
            stale_with_leaf = [
                view for view in stale_views
                if view.name.rsplit(".", 1)[-1] == old_leaf
            ]
            return len(stale_with_leaf) == 1
        same_leaf = [
            view for view in surfaced_views
            if view.name.rsplit(".", 1)[-1] == old_leaf
        ]
        stale_with_leaf = [
            view for view in stale_views
            if view.name.rsplit(".", 1)[-1] == old_leaf
        ]
        return len(same_leaf) == 1 and len(stale_with_leaf) == 1

    broken_decl = _issue_build_broken(ctx, issue_num)
    unmapped_stale = any(
        not covered_by_current_replacement(stale)
        for stale in stale_views
    )
    if errors or broken_decl is not None or unmapped_stale or not review_views:
        tier = "-"
        qualifiers: list[str] = []
        if broken_decl is not None:
            headline = broken_decl
    else:
        worst = review_views[0]
        tier = worst.tier
        qualifiers = list(worst.qualifiers)

    return _ReconciledIssueSurface(
        issue=issue_num,
        cited_names=cited_names,
        cited_views=cited_views,
        surfaced_views=surfaced_views,
        review_views=review_views,
        stale_views=stale_views,
        tier=tier,
        qualifiers=qualifiers,
        headline=headline,
        error="; ".join(errors) if errors else None,
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


def _global_reconciled_dependency_model(
    cfg: "ReviewConfig",
    ctx: _DeckContext,
    ledger,
    target_issue_nums: list[int],
    target_surfaces: "dict[int, _ReconciledIssueSurface]",
) -> tuple[
    dict[int, _ReconciledIssueSurface],
    dict[int, list[str]],
    dict[int, list[int]],
    dict[int, list[int]],
]:
    """Project-wide ownership and readiness gates for a scoped review surface.

    Rich declaration views are supplied only for ``target_issue_nums``.  Other
    registered issues start with their inexpensive issue-body citations and
    are reconciled only when they could own a currently unowned dependency of
    a target.  This keeps chapter/file views cheap while ensuring that every
    irreversible review surface observes earlier-chapter gates, including a
    surfaced rename or split that no longer appears in its issue body.

    Returns ``(surfaces, owned_decls, raw_deps, gating_deps)``.  Dependency
    dictionaries include every registered issue so a target's predecessor list
    may truthfully contain an out-of-scope issue.
    """
    global_nums = _chapter_issue_nums(cfg, None)
    for issue_num in target_issue_nums:
        if issue_num not in global_nums:
            global_nums.append(issue_num)

    surfaces = dict(target_surfaces)
    owned_decls = {
        num: list(_decls_for_issue(ctx, num))
        for num in global_nums
    }
    owned_decls.update({
        num: [view.name for view in surface.review_views]
        for num, surface in surfaces.items()
    })

    raw_owners = _decl_to_issues(ctx, global_nums, owned_decls)
    target_dependency_decls: set[str] = set()
    for issue_num in target_issue_nums:
        target_dependency_decls.update(
            _dependency_declarations(ctx, issue_num, owned_decls)
        )
    unowned_dependencies = {
        name for name in target_dependency_decls
        if name not in raw_owners
    }

    if unowned_dependencies:
        for num in global_nums:
            if num in surfaces:
                continue
            meta_for_owner = ctx.issue_meta.get(num) or {}
            if not _textbook_result_keys(meta_for_owner.get("title") or ""):
                continue
            cited_resolved = {
                _resolve_to_snapshot(ctx, raw) or raw
                for raw in _decls_for_issue(ctx, num)
            }
            namespaces = _namespace_prefixes(sorted(cited_resolved))
            could_surface = any(
                (
                    any(
                        dep.startswith(namespace + ".")
                        and "." not in dep[len(namespace) + 1:]
                        for namespace in namespaces
                    )
                    or any(
                        dep.startswith(cited_name + "_")
                        for cited_name in cited_resolved
                    )
                )
                for dep in unowned_dependencies
            )
            if not could_surface:
                continue
            candidate_surface = _build_reconciled_issue_surface(
                cfg, ctx, ledger, num
            )
            candidate_names = {
                view.name for view in candidate_surface.review_views
            }
            if not (candidate_names & unowned_dependencies):
                continue
            surfaces[num] = candidate_surface
            owned_decls[num] = sorted(candidate_names)

    decl_to_issues = _decl_to_issues(ctx, global_nums, owned_decls)
    raw_deps = {
        num: _predecessor_issues(
            ctx, num, global_nums, decl_to_issues, owned_decls
        )
        for num in global_nums
    }
    return surfaces, owned_decls, raw_deps, _gating_deps(raw_deps)


def build_card_detail(cfg: "ReviewConfig", issue_num: int) -> CardDetail:
    """Assemble the full spec card for one issue.

    ``decls`` retains every body-cited declaration for compatibility and stale
    citation diagnostics; ``surfaced_decls`` contains current-code declarations
    recovered by rename/split reconciliation.  The card tier, headline, gates,
    and top-level scalar fields all come from the union of in-code cited and
    surfaced declarations—the exact set the subdeck reviews. Pure + read-only.
    """
    chapter = cfg.chapter_of_issue(issue_num)
    chapter_nums = _chapter_issue_nums(cfg, chapter)
    if issue_num not in chapter_nums:
        chapter_nums = [issue_num, *chapter_nums]
    # Rich declaration rendering stays scoped to the target chapter, but
    # shipping dependencies are project-wide: a Chapter 16 theorem can depend
    # on an unresolved Chapter 15 definition. Load metadata/audit context once
    # for the complete registry, then build full DeclViews only where needed.
    global_nums = _chapter_issue_nums(cfg, None)
    if issue_num not in global_nums:
        global_nums = [issue_num, *global_nums]
    ctx = _load_context(cfg, global_nums)
    ledger = _ledger(cfg)
    surfaces = {
        num: _build_reconciled_issue_surface(cfg, ctx, ledger, num)
        for num in chapter_nums
    }
    surface = surfaces[issue_num]
    tier = surface.tier
    qualifiers = surface.qualifiers
    decl = surface.headline
    # Preserve the body-cited list for detail/backward compatibility, while
    # the headline and all gates come from the exact reconciled review set.
    views = list(surface.cited_views)
    surfaced_views = list(surface.surfaced_views)
    review_views = list(surface.review_views)

    # Top-level scalar fields mirror the headline view (decls[0]) for
    # backward-compatible / degraded clients.
    head = review_views[0] if review_views else (views[0] if views else None)
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

    # Dependency ownership and ship gates are project-wide even though the
    # detail's rich source rendering remains scoped to its chapter.
    surfaces, _owned_decls, issue_deps, gating_deps = (
        _global_reconciled_dependency_model(
            cfg, ctx, ledger, chapter_nums, surfaces
        )
    )
    pred_ids = issue_deps.get(issue_num, [])
    deps: list[DepRef] = []
    for pid in pred_ids:
        predecessor = surfaces.get(pid)
        if predecessor is None:
            pred_tier, _pred_qualifiers, pred_headline = _issue_tier(
                ctx, pid
            )
        else:
            pred_tier = predecessor.tier
            pred_headline = predecessor.headline
        deps.append(DepRef(
            id=pid,
            decl=pred_headline,
            tier=pred_tier,
        ))
    inspectable, inspection_blocked_reason = _inspectability(
        ctx, issue_num, tier
    )
    shippable, ship_blocked_reason = _readiness(
        ctx,
        issue_num,
        tier,
        gating_deps.get(issue_num, []),
    )

    return CardDetail(
        id=issue_num,
        decl=decl,
        title=meta.get("title") or f"#{issue_num}",
        chapter=chapter,
        tier=tier,
        qualifiers=qualifiers,
        inspectable=inspectable,
        inspection_blocked_reason=inspection_blocked_reason,
        shippable=shippable,
        ship_blocked_reason=ship_blocked_reason,
        provisional=inspectable and not shippable,
        decls=views,
        surfaced_decls=surfaced_views,
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
