"""Offline tests for marathon.audit.dedup — fingerprint-based cross-chapter
duplicate detection (plan §2 "referee with teeth": cross-chapter dedup is
computed from audit FINGERPRINTS, never Claude guessing).

Binding behaviors under test:

* two equal-fingerprint DEFS in DIFFERENT modules are a duplicate group;
* two equal-fingerprint defs in the SAME module are NOT (left to the
  per-file reviewer, not the cross-chapter referee);
* defs are matched on (type, value) — same type but different value is NOT
  a duplicate (a definition's meaning is its value);
* theorems are matched on TYPE alone (proof irrelevance — same statement is
  the same fact even with different proofs); a theorem and a def never
  collide on a bare type fingerprint;
* unknown / missing-fingerprint decls never crash and are never grouped;
* the canonical keep-candidate is deterministic (earliest module / shortest
  name).

Pure: no snapshot persistence, no Lean, no network.
"""

from __future__ import annotations

from marathon.audit.dedup import DuplicateGroup, find_duplicates
from marathon.audit.records import AuditSnapshot, DeclAudit


def mk_decl(
    name,
    *,
    kind="def",
    module="Ch1",
    status="ok",
    type_pp="MyType",
    value_pp="myvalue",
    cone=(),
    tags=(),
) -> DeclAudit:
    return DeclAudit(
        name=name, kind=kind, module=module, status=status,
        type_pp=type_pp, value_pp=value_pp, cone=list(cone),
        axioms=[], has_sorry=False, tags=list(tags), reason=None,
    )


def mk_snapshot(decls) -> AuditSnapshot:
    return AuditSnapshot(
        repo_dir="/r", modules=["Ch1", "Ch2"], toolchain="lean4:v1",
        lean_version="v1", package_revs={}, trusted_prefixes=["Mathlib"],
        created_at="2026-06-14T00:00:00+00:00", decls=list(decls), failures=[],
    )


# --- the canonical IsPositivelyOriented-in-two-chapters case ----------------


def test_equal_def_two_modules_is_a_duplicate():
    snap = mk_snapshot([
        mk_decl("Ch11.IsPositivelyOriented", module="Ch11"),
        mk_decl("Ch12.IsPositivelyOriented", module="Ch12"),
    ])
    groups = find_duplicates(snap)
    assert len(groups) == 1
    g = groups[0]
    assert set(g.members) == {
        "Ch11.IsPositivelyOriented", "Ch12.IsPositivelyOriented"
    }
    assert set(g.modules) == {"Ch11", "Ch12"}
    # Earliest module wins the canonical slot.
    assert g.canonical == "Ch11.IsPositivelyOriented"
    assert g.redundant == ["Ch12.IsPositivelyOriented"]
    assert g.key_kind == "def"


def test_same_module_repeats_are_not_cross_chapter_duplicates():
    # Two equal-fingerprint defs in the SAME module: not the referee's
    # problem (a deliberate local alias / the audit seeing one decl twice).
    snap = mk_snapshot([
        mk_decl("Ch1.foo", module="Ch1"),
        mk_decl("Ch1.fooAlias", module="Ch1"),
    ])
    assert find_duplicates(snap) == []


def test_def_same_type_different_value_not_duplicate():
    # `two : Nat := 2` vs `three : Nat := 3`: same type fingerprint, but a
    # def's MEANING is its value — different value, not a duplicate.
    snap = mk_snapshot([
        mk_decl("Ch1.two", module="Ch1", type_pp="Nat", value_pp="2"),
        mk_decl("Ch2.three", module="Ch2", type_pp="Nat", value_pp="3"),
    ])
    assert find_duplicates(snap) == []


def test_def_same_type_and_value_is_duplicate():
    snap = mk_snapshot([
        mk_decl("Ch1.two", module="Ch1", type_pp="Nat", value_pp="2"),
        mk_decl("Ch2.twoCopy", module="Ch2", type_pp="Nat", value_pp="2"),
    ])
    groups = find_duplicates(snap)
    assert len(groups) == 1
    assert groups[0].canonical == "Ch1.two"


def test_def_group_surfaces_value_fingerprint_type_group_does_not():
    # A def group's identity is BOTH fingerprints: it must carry the value
    # half so a downstream task key can keep two same-type/different-value
    # groups distinct (the mandatory-fix collision). A type-keyed group has
    # no value half (distinct types can't share a group).
    from marathon.audit.records import fingerprint
    snap = mk_snapshot([
        mk_decl("Ch1.two", module="Ch1", type_pp="Nat", value_pp="2"),
        mk_decl("Ch2.twoCopy", module="Ch2", type_pp="Nat", value_pp="2"),
        mk_decl("Ch1.comm", kind="theorem", module="Ch1",
                type_pp="a = a", value_pp=None),
        mk_decl("Ch2.commAgain", kind="theorem", module="Ch2",
                type_pp="a = a", value_pp=None),
    ])
    groups = {g.key_kind: g for g in find_duplicates(snap)}
    assert groups["def"].fingerprint_value == fingerprint("2")
    assert groups["def"].fingerprint == fingerprint("Nat")
    assert groups["type"].fingerprint_value is None


def test_two_def_groups_same_type_differ_in_value_fingerprint():
    # The collision case at the source: two def-duplicate groups with the
    # SAME type fingerprint but DIFFERENT value fingerprints. find_duplicates
    # must return two groups whose (type, value) identities differ in the
    # value half — so a key folding in both halves can't conflate them.
    snap = mk_snapshot([
        mk_decl("Ch1.a", module="Ch1", type_pp="Nat", value_pp="2"),
        mk_decl("Ch2.a", module="Ch2", type_pp="Nat", value_pp="2"),
        mk_decl("Ch3.b", module="Ch3", type_pp="Nat", value_pp="5"),
        mk_decl("Ch4.b", module="Ch4", type_pp="Nat", value_pp="5"),
    ])
    groups = find_duplicates(snap)
    assert len(groups) == 2
    # Same type fingerprint across both groups...
    assert len({g.fingerprint for g in groups}) == 1
    # ...but DISTINCT value fingerprints (the discriminator).
    assert len({g.fingerprint_value for g in groups}) == 2


# --- theorems group by TYPE only --------------------------------------------


def test_theorems_group_by_type_only():
    # Same statement, different (irrelevant) proofs / no value fingerprint:
    # the same FACT, flagged across chapters.
    snap = mk_snapshot([
        mk_decl("Ch1.comm", kind="theorem", module="Ch1",
                type_pp="a + b = b + a", value_pp=None),
        mk_decl("Ch2.commAgain", kind="theorem", module="Ch2",
                type_pp="a + b = b + a", value_pp=None),
    ])
    groups = find_duplicates(snap)
    assert len(groups) == 1
    g = groups[0]
    assert g.key_kind == "type"
    assert g.canonical == "Ch1.comm"


def test_theorem_and_def_with_same_type_do_not_collide():
    # A theorem keyed by type and a def keyed by (type,value) live in
    # disjoint keyspaces — a bare type match must not group them.
    snap = mk_snapshot([
        mk_decl("Ch1.thm", kind="theorem", module="Ch1",
                type_pp="SameType", value_pp=None),
        mk_decl("Ch2.def", kind="def", module="Ch2",
                type_pp="SameType", value_pp="body"),
    ])
    assert find_duplicates(snap) == []


def test_structure_groups_by_type_across_modules():
    snap = mk_snapshot([
        mk_decl("Ch1.Bundle", kind="structure", module="Ch1",
                type_pp="Type", value_pp=None),
        mk_decl("Ch2.Bundle", kind="structure", module="Ch2",
                type_pp="Type", value_pp=None),
    ])
    groups = find_duplicates(snap)
    assert len(groups) == 1
    assert groups[0].key_kind == "type"


# --- robustness: never crash on unknown / missing fingerprints --------------


def test_unknown_decls_are_skipped_not_grouped():
    snap = mk_snapshot([
        mk_decl("Ch1.broken", module="Ch1", status="unknown",
                type_pp=None, value_pp=None),
        mk_decl("Ch2.broken", module="Ch2", status="unknown",
                type_pp=None, value_pp=None),
    ])
    # No fingerprints -> nothing to compare -> no false duplicate.
    assert find_duplicates(snap) == []


def test_def_missing_value_fingerprint_not_grouped_on_type():
    # A def whose value_pp is absent has no value fingerprint; refusing to
    # group it on type alone is the documented soundness guarantee.
    snap = mk_snapshot([
        mk_decl("Ch1.d", kind="def", module="Ch1",
                type_pp="Nat", value_pp=None),
        mk_decl("Ch2.d", kind="def", module="Ch2",
                type_pp="Nat", value_pp=None),
    ])
    assert find_duplicates(snap) == []


def test_empty_snapshot_is_safe():
    assert find_duplicates(mk_snapshot([])) == []


def test_three_module_group_lists_all_modules_and_redundant():
    snap = mk_snapshot([
        mk_decl("ChA.x", module="ChA"),
        mk_decl("ChB.x", module="ChB"),
        mk_decl("ChC.x", module="ChC"),
    ])
    groups = find_duplicates(snap)
    assert len(groups) == 1
    g = groups[0]
    assert g.modules == ["ChA", "ChB", "ChC"]
    assert g.canonical == "ChA.x"
    assert set(g.redundant) == {"ChB.x", "ChC.x"}


def test_canonical_prefers_shortest_name_within_earliest_module():
    # Two members in the same earliest module: shortest name wins.
    snap = mk_snapshot([
        mk_decl("Ch1.longerName", module="Ch1"),
        mk_decl("Ch1.x", module="Ch1"),
        mk_decl("Ch2.y", module="Ch2"),
    ])
    groups = find_duplicates(snap)
    assert len(groups) == 1
    assert groups[0].canonical == "Ch1.x"


def test_groups_sorted_by_canonical_name():
    snap = mk_snapshot([
        mk_decl("ChZ.alpha", module="ChZ", type_pp="A", value_pp="a"),
        mk_decl("ChY.alpha", module="ChY", type_pp="A", value_pp="a"),
        mk_decl("ChB.beta", module="ChB", type_pp="B", value_pp="b"),
        mk_decl("ChA.beta", module="ChA", type_pp="B", value_pp="b"),
    ])
    groups = find_duplicates(snap)
    canonicals = [g.canonical for g in groups]
    assert canonicals == sorted(canonicals)
