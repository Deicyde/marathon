"""Prove-ordering DAG — leaves-first sequencing for the ``sorry``-filling prover.

Two tiers, per Jack's directive:

* **T2 — the decl DAG.** One node per project-local declaration; an edge
  ``X → Y`` (``Y`` depends on ``X``) whenever ``X`` appears in ``Y``'s
  dependency ``cone``. Built straight from the audit snapshot (each
  :class:`~marathon.audit.records.DeclAudit` carries ``cone`` = the project-local
  constants referenced by the decl's TYPE — see
  :mod:`marathon.audit.lean_template`).

* **T1 — the issue DAG.** One node per pending fill target (a verified sub-issue
  with ≥1 still-``sorry`` decl); issue ``A`` depends on issue ``B`` when a decl
  ``A`` owns transitively cones onto a decl ``B`` owns. Derived by lifting the T2
  edges through the decl→issue ownership map.

Dispatch is per issue (``marathon fill --issue N``), so the conductor consumes
the T1 order (leaves first → the root, eventually Stokes, last). The T2 graph is
the substrate it is derived from — and the basis for a future per-decl dispatch
or a deck visualization.

WHY ``cone`` (type-level) is the right, sufficient signal:

* It captures STATEMENT dependencies — "what must exist before this can even be
  stated" — which is exactly the leaves→root axis and yields a stable Stokes-last
  order.
* It does NOT capture proof-body-only calls (a lemma a proof invokes that the
  statement never names). That is acceptable: the machine-clean gate
  (:mod:`marathon.gate`) TOLERATES a proof that transitively cites a still-``sorry``
  lemma (``sorryAx`` is accounted, not failed). So ordering is an efficiency /
  clean-foundations STRATEGY, never a correctness gate — an imperfect order costs
  a little rework, never a wrong result.
* Therefore a missing or stale snapshot degrades gracefully: :func:`order_targets`
  falls back to the input (issue-number) order and the dispatch queue can never
  wedge waiting on a dependency the snapshot cannot see.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Iterable, Optional


# --- name resolution --------------------------------------------------------

def resolve_decl(name: str, by_name: dict) -> Optional[str]:
    """Resolve a body-cited decl ``name`` (often unqualified) to a snapshot decl:
    exact match, else a UNIQUE dotted-suffix match. ``None`` when absent or the
    suffix is ambiguous. Same deterministic rule the deck cards / tier backfill
    use (:func:`marathon.deck.cards._resolve_to_snapshot`)."""
    if name in by_name:
        return name
    suffix = [n for n in by_name if n.endswith("." + name)]
    return suffix[0] if len(suffix) == 1 else None


# --- T2: the decl DAG -------------------------------------------------------

@dataclass(frozen=True)
class DeclNode:
    """One node of the T2 decl DAG."""
    name: str
    kind: str
    has_sorry: Optional[bool]
    clean: bool           # proven-clean: not sorry AND no transitive sorryAx
    deps: tuple[str, ...]  # direct, in-snapshot cone dependencies


def build_decl_graph(snapshot) -> dict[str, DeclNode]:
    """The T2 decl DAG: ``{decl_name: DeclNode}``. Edges are each decl's ``cone``
    filtered to in-snapshot, non-self targets. ``clean`` marks a genuinely
    discharged decl (``has_sorry`` is False AND no ``sorryAx`` in its transitive
    axioms) — the honest "already proven" signal a leaves-first order rests on."""
    by_name = snapshot.by_name()
    graph: dict[str, DeclNode] = {}
    for name, da in by_name.items():
        deps = tuple(sorted(
            c for c in (getattr(da, "cone", None) or [])
            if c != name and c in by_name
        ))
        clean = (getattr(da, "has_sorry", None) is False) and (
            "sorryAx" not in (getattr(da, "axioms", None) or [])
        )
        graph[name] = DeclNode(
            name=name, kind=getattr(da, "kind", ""),
            has_sorry=getattr(da, "has_sorry", None), clean=clean, deps=deps,
        )
    return graph


def transitive_deps(name: str, graph: dict[str, DeclNode]) -> set[str]:
    """Every in-snapshot decl reachable from ``name`` along ``cone`` edges
    (cycle-safe; ``name`` itself excluded)."""
    seen: set[str] = set()
    node = graph.get(name)
    stack: list[str] = list(node.deps) if node else []
    while stack:
        n = stack.pop()
        if n in seen:
            continue
        seen.add(n)
        child = graph.get(n)
        if child:
            stack.extend(child.deps)
    seen.discard(name)
    return seen


# --- T1: the issue DAG ------------------------------------------------------

def _owned_decls(targets, by_name) -> tuple[dict[int, set[str]], dict[str, int]]:
    """``(issue → resolved decl set, resolved decl → owning issue)`` for the
    pending targets. A decl a target cites but that does not resolve to a
    snapshot name is dropped (it can carry no dependency edge)."""
    resolved: dict[int, set[str]] = {}
    owner: dict[str, int] = {}
    for t in targets:
        rs: set[str] = set()
        for d in t.decls:
            r = resolve_decl(d, by_name)
            if r is not None:
                rs.add(r)
                owner.setdefault(r, t.issue)
        resolved[t.issue] = rs
    return resolved, owner


def issue_dependency_edges(targets, snapshot) -> dict[int, set[int]]:
    """The T1 edges: ``{issue: {issues it depends on}}`` over the pending
    ``targets``. Issue ``A`` depends on issue ``B`` when a decl ``A`` owns
    transitively cones onto a decl ``B`` owns (``B`` ≠ ``A``, both pending).
    Only edges between pending targets matter — a dependency on an
    already-proven decl imposes no ordering constraint."""
    graph = build_decl_graph(snapshot)
    by_name = snapshot.by_name()
    resolved, owner = _owned_decls(targets, by_name)
    deps: dict[int, set[int]] = {t.issue: set() for t in targets}
    for t in targets:
        dep_decls: set[str] = set()
        for r in resolved[t.issue]:
            dep_decls |= transitive_deps(r, graph)
        for dd in dep_decls:
            o = owner.get(dd)
            if o is not None and o != t.issue:
                deps[t.issue].add(o)
    return deps


def topo_order_issues(issue_nums: Iterable[int], deps: dict[int, set[int]]) -> list[int]:
    """Order issues DEPENDENCIES-FIRST via a LAYERED Kahn sort: every issue whose
    dependencies are all already placed (a "current leaf") comes before any issue
    in the next layer. ``deps[num]`` is the set of issues ``num`` depends on.

    Layering (rather than plain Kahn, which interleaves layers) matters because
    the conductor may dispatch several targets in one tick: taking a whole leaf
    layer first means a tick never dispatches a dependent alongside a dependency
    that is still ``sorry``. Within a layer, and for any issues trapped in a
    dependency cycle (appended last), order is by issue number — so the result is
    deterministic and total."""
    nums = list(issue_nums)
    in_set = set(nums)
    indeg: dict[int, int] = {
        n: len({d for d in deps.get(n, ()) if d in in_set}) for n in nums
    }
    dependents: dict[int, list[int]] = {n: [] for n in nums}
    for n in nums:
        for d in deps.get(n, ()):
            if d in in_set:
                dependents[d].append(n)
    out: list[int] = []
    placed: set[int] = set()
    ready = sorted(n for n in nums if indeg[n] == 0)
    while ready:
        layer, ready = ready, []          # take the ENTIRE current leaf layer
        for n in layer:
            out.append(n)
            placed.add(n)
        for n in layer:
            for dep in dependents[n]:
                indeg[dep] -= 1
                if indeg[dep] == 0:
                    ready.append(dep)
        ready = sorted(ready)
    for n in sorted(in_set - placed):     # cycle remnants, deterministic
        out.append(n)
    return out


# --- the conductor entry point ---------------------------------------------

def order_targets(targets, snapshot) -> list:
    """Reorder pending fill ``targets`` leaves-first over the dependency DAG —
    the single seam the conductor calls to sequence proving.

    Returns a new list, same elements. Falls back to the input order (which
    discovery already sorts by issue number) when there is no snapshot or any
    error, so a missing/stale snapshot only reorders, never wedges, the queue.
    """
    targets = list(targets)
    if snapshot is None or len(targets) <= 1:
        return targets
    try:
        deps = issue_dependency_edges(targets, snapshot)
        ordered = topo_order_issues((t.issue for t in targets), deps)
    except Exception:  # noqa: BLE001 — ordering must never crash the conductor
        return targets
    by_issue: dict[int, list] = {}
    for t in targets:
        by_issue.setdefault(t.issue, []).append(t)
    out: list = []
    for n in ordered:
        out.extend(by_issue.get(n, ()))
    # Any target whose issue somehow fell out of the ordering trails in input
    # order — the function is total over the inputs.
    seen_issues = set(ordered)
    for t in targets:
        if t.issue not in seen_issues:
            out.append(t)
    return out


# --- inspection bundle (T1 + T2 together; for status / a future deck view) --

@dataclass(frozen=True)
class ProverDag:
    """Both tiers, materialized for inspection: the T2 decl graph, the T1 issue
    edges over the pending targets, and the leaves-first issue order."""
    decls: dict[str, DeclNode]
    issue_deps: dict[int, set[int]]
    order: list[int] = field(default_factory=list)

    def leaves(self) -> list[int]:
        """The pending issues with no pending dependency — where proving starts."""
        return sorted(n for n, d in self.issue_deps.items() if not d)


def build_prover_dag(targets, snapshot) -> ProverDag:
    """Materialize both DAGs for the pending ``targets`` — construction that
    :func:`order_targets` does inline, exposed for status / inspection / tests.
    Returns an empty bundle when there is no snapshot."""
    if snapshot is None:
        return ProverDag(decls={}, issue_deps={}, order=[t.issue for t in targets])
    decls = build_decl_graph(snapshot)
    deps = issue_dependency_edges(targets, snapshot)
    order = topo_order_issues((t.issue for t in targets), deps)
    return ProverDag(decls=decls, issue_deps=deps, order=order)
