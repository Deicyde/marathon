"""marathon.audit.kernel — the trust kernel: the minimized human-read surface.

Plan §2 ruling 5 (BINDING): goal 2's actual mechanism.

    kernel(T) = the normalized statement of T
              + every PROJECT-LOCAL DEFINITION in T's transitive
                STATEMENT cone.

Mathlib/core constants are *trusted vocabulary* — they are not in the
kernel. Proof bodies are NEVER in the kernel (only the meaning of the
statement and the definitions its statement unfolds to is load-bearing).

This module is pure Python over an :class:`~marathon.audit.records.AuditSnapshot`.
The snapshot's per-decl ``cone`` field is ALREADY the project-local
partition of the constants appearing in that declaration's *elaborated
type* (the Lean script in :mod:`marathon.audit.lean_template` drops every
trusted-prefix constant). So computing the kernel is a transitive closure
over cone edges through the snapshot — no notion of "Mathlib" is needed
here beyond "absent from the snapshot's project-local universe".

The theorem-in-cone rule (documented, defensible interpretation)
----------------------------------------------------------------
A constant reached in the transitive cone is one of three things:

* a **def-like** constant (kind in :data:`KERNEL_DEF_KINDS` —
  def/abbrev/instance/structure/inductive/class): a thing whose *meaning*
  a human must read to trust ``T``. It IS a kernel member, AND we descend
  into its own type cone (its type may mention further local defs).

* a **theorem/lemma**: its *statement* is not load-bearing for ``T``'s
  meaning — only its *existence* is (and its proof is out of scope). A
  human auditing ``T`` does not have to re-read the lemma's statement to
  understand what ``T`` says; the lemma is itself separately audited. So
  the lemma is NOT a "definition to read". BUT a def used *inside* that
  lemma's statement is still part of the vocabulary ``T`` is phrased in
  (``T``'s type mentions the lemma by name, and to know what proposition
  that name denotes a reader of the lemma needs those defs). We therefore
  descend into the lemma's TYPE cone (collecting any local defs there)
  while recording the lemma itself only in :attr:`Kernel.local_lemmas`.
  Recommendation realized: kernel = local *defs* in the transitive
  TYPE-cone closure; local *lemmas* whose statements appear are recorded
  separately.

* an **axiom / opaque / other**: recorded as a kernel member too (an
  ``axiom`` a human must trust is squarely part of the surface; ``opaque``
  hides a value but its existence/type is meaning-bearing). These do not
  recurse beyond their own type cone.

Robustness rulings
------------------
* **Cycle-safe.** Mutual recursion (``def f`` mentions ``def g`` mentions
  ``f`` in their types) terminates: the BFS marks every visited name.
* **Deterministic ordering.** Members are returned in a stable order —
  topological where the cone DAG admits one (dependencies before
  dependents), else name order to break ties / cycles.
* **Unresolved recorded, never crashed.** A cone edge into a name absent
  from the snapshot (a project-local constant the audit didn't elaborate,
  or a stale reference) is collected in :attr:`Kernel.unresolved`, not
  raised. Absence of evidence is reported, never punished.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from marathon.audit.records import AuditSnapshot, DeclAudit

#: Kinds whose MEANING a human must read — the kernel "definitions".
#: ``opaque``/``axiom``/``other`` are added as members too (see
#: :func:`compute_kernel`), but only these recurse as *definitions*.
KERNEL_DEF_KINDS: tuple[str, ...] = (
    "def",
    "abbrev",
    "instance",
    "structure",
    "inductive",
    "class",
)

#: Kinds that are kernel members (a human must read/trust them) but are
#: not "definitions to read" in the unfold sense: their statement is the
#: thing, and they have no value to inspect.
KERNEL_OPAQUE_KINDS: tuple[str, ...] = ("opaque", "axiom", "other")


@dataclass(frozen=True)
class KernelMember:
    """One project-local definition a human must read to trust the target.

    ``value_pp`` is populated only for the value-carrying def kinds
    (def/abbrev/instance) — for those, the *value* is the meaning that
    matters; structures/inductives/classes/opaque/axiom carry no value
    and expose their meaning through their type/signature alone."""

    name: str
    kind: str
    type_pp: str | None
    value_pp: str | None

    def loc(self) -> int:
        """Cheap line-count proxy for this member's read cost: the lines
        of its pinned-pp type plus value. A human's reading burden is the
        text they must take in; this counts it without a source file."""
        text_lines = 0
        for text in (self.type_pp, self.value_pp):
            if text:
                text_lines += text.count("\n") + 1
        return text_lines


@dataclass(frozen=True)
class Kernel:
    """The trust kernel of one target declaration.

    ``members`` is the minimized human-read surface: the transitive set
    of project-local definitions in the target's statement cone, in
    dependency-respecting order. ``local_lemmas`` are project-local
    theorems/lemmas reached in the cone whose *existence* the statement
    leans on but whose *statements* are not part of what the target
    means (recorded for transparency, never counted in the kernel size).
    ``unresolved`` are cone references absent from the snapshot."""

    target: str
    members: list[KernelMember] = field(default_factory=list)
    local_lemmas: list[str] = field(default_factory=list)
    unresolved: list[str] = field(default_factory=list)

    @property
    def size_decls(self) -> int:
        """Number of local definitions a human must read (the headline
        kernel-size metric: count of meaning-bearing local defs)."""
        return len(self.members)

    @property
    def size_loc(self) -> int:
        """Total pinned-pp lines across kernel members — the LOC half of
        the kernel-size metric."""
        return sum(m.loc() for m in self.members)


def _is_kernel_def(decl: DeclAudit) -> bool:
    return decl.kind in KERNEL_DEF_KINDS


def _value_pp_for(decl: DeclAudit) -> str | None:
    """The value half of a member, present only for value-carrying defs.

    Mirrors the records contract: only def/abbrev/instance carry a
    ``value_pp`` (``VALUE_KINDS``); everything else exposes meaning via
    its type alone."""
    if decl.kind in ("def", "abbrev", "instance"):
        return decl.value_pp
    return None


def compute_kernel(decl_name: str, snapshot: AuditSnapshot) -> Kernel:
    """The trust kernel of ``decl_name`` over ``snapshot``.

    BFS over the cone graph starting from the target's TYPE cone; at each
    step follow only edges into project-local constants present in the
    snapshot (the snapshot's ``cone`` field is already the project-local
    partition). Collects the transitive set of def-like members; records
    local lemmas and unresolved references separately. Cycle-safe and
    deterministic (topological where possible, else name order).

    The target itself is never a member (the kernel is the *definitions
    behind* the statement, plus the statement — the statement is the
    target's own ``type_pp``, surfaced by the spec card, not duplicated
    here). If the target is absent/unknown, its missing-ness is reported
    via ``unresolved`` containing the target name and an empty member set.
    """
    by_name = snapshot.by_name()
    target = by_name.get(decl_name)

    members: dict[str, KernelMember] = {}
    local_lemmas: set[str] = set()
    unresolved: set[str] = set()
    # Adjacency over resolved cone edges, for a deterministic topo sort.
    edges: dict[str, list[str]] = {}

    if target is None or target.is_unknown:
        # No type cone to walk — report the target as unresolved evidence,
        # never crash. (A spec card built on this surfaces the gap.)
        unresolved.add(decl_name)
        return Kernel(
            target=decl_name,
            members=[],
            local_lemmas=[],
            unresolved=sorted(unresolved),
        )

    # BFS frontier of names to expand; `seen` guards cycles/diamonds.
    seen: set[str] = set()
    frontier: list[str] = sorted(target.cone)
    seen.update(frontier)

    while frontier:
        name = frontier.pop(0)
        edges.setdefault(name, [])
        decl = by_name.get(name)
        if decl is None or decl.is_unknown:
            # A project-local cone reference with no audit evidence.
            unresolved.add(name)
            continue
        if _is_kernel_def(decl):
            members[name] = KernelMember(
                name=name,
                kind=decl.kind,
                type_pp=decl.type_pp,
                value_pp=_value_pp_for(decl),
            )
            # Descend into this definition's own type cone.
            children = sorted(decl.cone)
        elif decl.kind in KERNEL_OPAQUE_KINDS:
            members[name] = KernelMember(
                name=name,
                kind=decl.kind,
                type_pp=decl.type_pp,
                value_pp=None,
            )
            # opaque/axiom/other still pull in the local defs their type
            # mentions, so a reader can decode that type.
            children = sorted(decl.cone)
        else:
            # theorem / lemma: existence-only. Not a "definition to read";
            # but local defs inside its statement remain vocabulary, so we
            # descend into its type cone.
            local_lemmas.add(name)
            children = sorted(decl.cone)
        for child in children:
            edges[name].append(child)
            if child not in seen:
                seen.add(child)
                frontier.append(child)

    ordered = _topo_order(members, edges)
    return Kernel(
        target=decl_name,
        members=ordered,
        local_lemmas=sorted(local_lemmas),
        unresolved=sorted(unresolved),
    )


def _topo_order(
    members: dict[str, KernelMember], edges: dict[str, list[str]]
) -> list[KernelMember]:
    """Order members dependencies-first (a def appears before the defs
    that mention it in their type), breaking ties and cycles by name.

    Only member names are ordered; non-member cone nodes (lemmas,
    unresolved) are transparent pass-throughs in the dependency relation
    — a def reachable only *through* a lemma still sorts after that
    lemma's other dependencies via its own edges. Kahn's algorithm with a
    name-sorted ready set gives determinism; any nodes left in a cycle are
    appended in name order so the function is total."""
    member_names = set(members)
    # Restrict the edge relation to member->member, transitively skipping
    # non-member intermediaries (lemmas/unresolved are not ordered).
    deps: dict[str, set[str]] = {name: set() for name in member_names}
    for name in member_names:
        for dep in _reachable_members(name, edges, member_names):
            if dep != name:
                deps[name].add(dep)
    # We want dependencies first: edge dep -> name. Compute indegree on
    # the reversed relation.
    dependents: dict[str, set[str]] = {name: set() for name in member_names}
    indegree: dict[str, int] = {name: 0 for name in member_names}
    for name, ds in deps.items():
        for dep in ds:
            dependents[dep].add(name)
            indegree[name] += 1
    ready = sorted(n for n in member_names if indegree[n] == 0)
    out: list[str] = []
    placed: set[str] = set()
    while ready:
        name = ready.pop(0)
        out.append(name)
        placed.add(name)
        newly: list[str] = []
        for dependent in sorted(dependents[name]):
            indegree[dependent] -= 1
            if indegree[dependent] == 0:
                newly.append(dependent)
        # Keep `ready` name-sorted for determinism.
        ready = sorted(set(ready) | set(newly))
    # Any members trapped in a cycle (indegree never hit 0) follow in
    # name order — the order is total and deterministic regardless.
    for name in sorted(member_names - placed):
        out.append(name)
    return [members[name] for name in out]


def _reachable_members(
    start: str, edges: dict[str, list[str]], member_names: set[str]
) -> set[str]:
    """Member names reachable from ``start`` along cone edges, treating
    non-member nodes as transparent. Cycle-safe."""
    found: set[str] = set()
    seen: set[str] = {start}
    stack = list(edges.get(start, []))
    while stack:
        node = stack.pop()
        if node in seen:
            continue
        seen.add(node)
        if node in member_names:
            found.add(node)
            # A member terminates this path: the dependent->member edge is
            # what we want; we do NOT continue *through* a member (its own
            # dependencies are computed from its own start).
            continue
        stack.extend(edges.get(node, []))
    return found


def kernel_size_metric(
    snapshot: AuditSnapshot, targets: list[str]
) -> dict[str, object]:
    """Per-target and aggregate kernel sizes (decls + LOC).

    The shape tracked in ``formalization.yaml`` later: a ``per_target``
    map of ``{decls, loc, unresolved}`` plus an ``aggregate`` whose
    ``decls``/``loc`` count the DISTINCT local defs across all targets
    (a def shared by two targets is one human read, not two — the
    aggregate measures the project's total human-read surface, not a sum
    of overlapping per-target surfaces). Pure."""
    per_target: dict[str, dict[str, object]] = {}
    union_members: dict[str, KernelMember] = {}
    union_unresolved: set[str] = set()
    for target in targets:
        kernel = compute_kernel(target, snapshot)
        per_target[target] = {
            "decls": kernel.size_decls,
            "loc": kernel.size_loc,
            "unresolved": list(kernel.unresolved),
        }
        for member in kernel.members:
            union_members.setdefault(member.name, member)
        union_unresolved.update(kernel.unresolved)
    aggregate = {
        "targets": len(targets),
        "decls": len(union_members),
        "loc": sum(m.loc() for m in union_members.values()),
        "unresolved": sorted(union_unresolved),
    }
    return {"per_target": per_target, "aggregate": aggregate}
