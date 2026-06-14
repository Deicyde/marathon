"""marathon.audit.dedup — fingerprint-based cross-chapter duplicate detection.

Plan §2 "Referee with teeth" (BINDING): the referee's cross-chapter
duplication detection is fed by the dep graph / audit FINGERPRINTS, not
by Claude guessing. The canonical evidence case is ``IsPositivelyOriented``
defined identically in two chapters (referee.md item #1): two project-local
defs with the SAME elaborated-type+value fingerprint living in DIFFERENT
modules are the same definition rendered twice — a navigability and reuse
regression the generic rubric never catches because each file is locally
clean.

This module is PURE Python over an
:class:`~marathon.audit.records.AuditSnapshot`. It computes structural
duplicates mechanically so that a duplicate ALWAYS becomes a referee
fix-task even if the Claude prose pass misses it (referee.py generates
dedup tasks directly from :func:`find_duplicates`; Claude only ranks /
annotates them).

The heuristic (documented, defensible)
--------------------------------------
We group **project-local def-like declarations** (kinds in
:data:`DEDUP_DEF_KINDS`) by their fingerprint key:

* **defs** (the value-carrying kinds — def/abbrev/instance) are keyed by
  ``(fingerprint_type, fingerprint_value)``: a definition's *meaning* is
  its value, so type-only equality is unsound (two different defs can
  share a type — e.g. ``two : Nat := 2`` and ``three : Nat := 3``). Both
  must match to call them duplicates.

* **theorems / lemmas** are keyed by ``fingerprint_type`` ALONE: by proof
  irrelevance a theorem carries no value fingerprint, and two theorems of
  the same statement ARE the same fact (their proofs may differ; that is
  exactly the redundancy worth flagging). Theorems group only with
  theorems; defs only with defs (the key namespace is partitioned by a
  ``kind``-class tag so a theorem and a def never collide on type alone).

* **structure/inductive/class/opaque/axiom** carry a type fingerprint but
  no value; we key them on ``fingerprint_type`` like theorems (a structure
  with the same elaborated type signature in two modules is a duplicate
  shape). They never group with theorems (different kind-class).

A group is a DUPLICATE only when its members live in **≥2 distinct
modules**. Two equal-fingerprint decls in the SAME module are not a
cross-chapter duplicate (a deliberate local alias, or the audit seeing a
decl twice) — those are left to the per-file reviewer, not the referee.

Robustness (BINDING — never crash on unknown/missing fingerprints)
------------------------------------------------------------------
* ``status='unknown'`` decls (no fingerprints) are skipped — absence of
  evidence is not evidence of duplication.
* A def missing its value fingerprint (value_pp absent) is skipped from
  the def keyspace: we will not call two defs duplicate on type alone.
* A decl missing a type fingerprint is skipped entirely. Nothing here
  raises on malformed input; skipped decls are simply not grouped.

The canonical keep-candidate
----------------------------
Each group names a deterministic **canonical** member to keep: the one in
the lexicographically-earliest module (the chapter that introduced the
shape first is, by convention, its home), breaking ties by SHORTEST then
lexicographically-smallest fully-qualified name (the least-decorated name
is the most reusable home). The other members are redundant restatements
the referee should redirect at the canonical.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from marathon.audit.records import AuditSnapshot, DeclAudit

#: Def-like kinds we consider for duplication. Mirrors the kernel's
#: notion of "a thing a human must read" (KERNEL_DEF_KINDS) unioned with
#: theorem/opaque/axiom — every kind that carries a type fingerprint and
#: whose duplication across chapters is a real reuse/navigability problem.
#: ('other' is excluded: it is the catch-all for declarations the audit
#: could not classify, so its fingerprints are not reliably comparable.)
DEDUP_DEF_KINDS: tuple[str, ...] = (
    "def",
    "abbrev",
    "instance",
    "structure",
    "inductive",
    "class",
    "opaque",
    "axiom",
    "theorem",
)

#: Kinds whose *value* fingerprint is load-bearing for equality (a def's
#: meaning is its value). Keyed by (type, value); everything else by type.
_VALUE_KINDS: tuple[str, ...] = ("def", "abbrev", "instance")


@dataclass(frozen=True)
class DuplicateGroup:
    """A set of project-local decls that share a fingerprint across ≥2
    modules — the same definition/fact rendered in different chapters.

    ``members`` are the fully-qualified decl names, sorted. ``canonical``
    is the deterministic keep-candidate (earliest module / shortest name).
    ``redundant`` is ``members`` minus the canonical — the restatements a
    referee fix-task should redirect at ``canonical``. ``modules`` is the
    sorted set of distinct modules the members live in (always ≥2).

    ``key_kind`` records HOW the group was keyed ('def' = type+value,
    'type' = type only) so callers can explain the match; ``fingerprint``
    is the shared TYPE fingerprint; ``fingerprint_value`` is the shared
    VALUE fingerprint for ``key_kind == 'def'`` groups (None for type-keyed
    groups). BOTH halves are load-bearing identity for def groups: two
    distinct def-duplicate groups can share an elaborated type yet differ
    in body (the IsPositivelyOriented wrapper-class case), so a downstream
    task key MUST fold in ``fingerprint_value`` or the two groups collide.
    Type-keyed groups carry ``fingerprint_value=None`` (distinct types
    cannot land in one group, so the type half alone is a unique key)."""

    members: list[str]
    modules: list[str]
    canonical: str
    redundant: list[str]
    key_kind: str  # 'def' (type+value) | 'type' (type only)
    fingerprint: str  # the shared type fingerprint
    fingerprint_value: str | None = None  # shared value fp (def groups only)
    kinds: list[str] = field(default_factory=list)  # member kinds, member order


def _kind_class(kind: str) -> str:
    """Partition the keyspace so a theorem and a def never collide on a
    bare type fingerprint. Returns 'def' for value-carrying kinds (keyed
    by type+value) and 'type' for the rest (keyed by type only)."""
    return "def" if kind in _VALUE_KINDS else "type"


def _dedup_key(decl: DeclAudit) -> tuple | None:
    """The grouping key for one decl, or None if it cannot be keyed
    (unknown status, missing fingerprints). Never raises.

    Value-carrying defs key on ``('def', type_fp, value_fp)``; everything
    else on ``('type', kind_is_theorem_or_shape, type_fp)``. The class tag
    keeps the two keyspaces disjoint."""
    if decl.is_unknown or decl.fingerprint_type is None:
        return None
    cls = _kind_class(decl.kind)
    if cls == "def":
        # A def with no value fingerprint cannot be matched on meaning;
        # refuse to group it on type alone (would be unsound).
        if decl.fingerprint_value is None:
            return None
        return ("def", decl.fingerprint_type, decl.fingerprint_value)
    return ("type", decl.fingerprint_type)


def _canonical(members: list[DeclAudit]) -> DeclAudit:
    """The deterministic keep-candidate: earliest module, then shortest
    name, then lexicographically-smallest name."""
    return min(members, key=lambda d: (d.module, len(d.name), d.name))


def find_duplicates(snapshot: AuditSnapshot) -> list[DuplicateGroup]:
    """Group project-local def-like decls of ``snapshot`` by fingerprint;
    return the groups whose members span ≥2 distinct modules.

    PURE: no I/O. Defs are matched on (type, value), theorems/shapes on
    type alone (see the module docstring's heuristic). Never crashes on
    unknown or missing fingerprints — unkeyable decls are skipped. Groups
    are returned in a stable order: by the canonical member's name.
    """
    by_key: dict[tuple, list[DeclAudit]] = {}
    for decl in snapshot.decls:
        if decl.kind not in DEDUP_DEF_KINDS:
            continue
        key = _dedup_key(decl)
        if key is None:
            continue
        by_key.setdefault(key, []).append(decl)

    groups: list[DuplicateGroup] = []
    for key, decls in by_key.items():
        modules = sorted({d.module for d in decls})
        if len(modules) < 2:
            # Same-module repeats are not cross-chapter duplicates.
            continue
        # Deduplicate by name within the group (defensive: a snapshot
        # should already be unique-by-name, but never assume it).
        by_name: dict[str, DeclAudit] = {}
        for d in decls:
            by_name.setdefault(d.name, d)
        unique = sorted(by_name.values(), key=lambda d: d.name)
        canonical = _canonical(unique)
        members = [d.name for d in unique]
        groups.append(
            DuplicateGroup(
                members=members,
                modules=modules,
                canonical=canonical.name,
                redundant=[n for n in members if n != canonical.name],
                key_kind=key[0],
                fingerprint=key[1],
                # Def keys are ('def', type_fp, value_fp); the value half is
                # load-bearing identity (see DuplicateGroup docstring). Type
                # keys are ('type', type_fp) — no value half.
                fingerprint_value=(key[2] if key[0] == "def" else None),
                kinds=[d.kind for d in unique],
            )
        )

    groups.sort(key=lambda g: g.canonical)
    return groups
