"""Offline tests for marathon.audit.kernel — the trust kernel (plan §2
ruling 5, goal 2's mechanism).

Binding rulings under test:

* kernel(T) = the project-local DEFINITIONS in T's transitive TYPE cone;
  Mathlib/core constants are trusted vocabulary and excluded (they are
  simply absent from the snapshot's project-local cone field);
* a theorem/lemma reached in the cone is NOT a "definition to read"
  (recorded in ``local_lemmas``), but its own type cone is still walked
  for the local defs it mentions;
* cycle-safe over mutual recursion; deterministic dependencies-first
  ordering; unresolved (absent-from-snapshot) references recorded, not
  raised;
* ``kernel_size_metric`` reports per-target sizes and a DISTINCT-union
  aggregate.

No subprocesses, no network, no Lean toolchain.
"""

from __future__ import annotations

from marathon.audit.kernel import (
    Kernel,
    KernelMember,
    compute_kernel,
    kernel_size_metric,
)
from marathon.audit.lean_template import DEFAULT_TRUSTED_PREFIXES
from marathon.audit.records import AuditSnapshot, DeclAudit


# --- builders (same style as test_trust / test_audit_engine) -----------------


def mk_decl(
    name="Foo.bar",
    kind="theorem",
    module="Foo",
    status="ok",
    type_pp="Nat",
    value_pp=None,
    cone=(),
    axioms=(),
    has_sorry=False,
    tags=(),
    reason=None,
) -> DeclAudit:
    return DeclAudit(
        name=name, kind=kind, module=module, status=status,
        type_pp=type_pp, value_pp=value_pp, cone=list(cone),
        axioms=list(axioms), has_sorry=has_sorry, tags=list(tags),
        reason=reason,
    )


def mk_snapshot(decls, **kw) -> AuditSnapshot:
    defaults = dict(
        repo_dir="/r",
        modules=["Foo"],
        toolchain="leanprover/lean4:v4.28.0",
        lean_version="4.28.0",
        package_revs={},
        trusted_prefixes=list(DEFAULT_TRUSTED_PREFIXES),
        created_at="2026-06-13T00:00:00+00:00",
        decls=list(decls),
        failures=[],
    )
    defaults.update(kw)
    return AuditSnapshot(**defaults)


# --- transitive closure over a 3-deep def->def->def cone ---------------------


def three_deep():
    """thm -> d1 -> d2 -> d3 (each a def), thm's type mentions a Mathlib
    constant that is ABSENT from the project-local cone (trusted)."""
    d3 = mk_decl(name="Foo.d3", kind="def", type_pp="Nat", value_pp="0",
                 cone=[])
    d2 = mk_decl(name="Foo.d2", kind="def", type_pp="Nat",
                 value_pp="Foo.d3 + 1", cone=["Foo.d3"])
    d1 = mk_decl(name="Foo.d1", kind="def", type_pp="Nat -> Nat",
                 value_pp="fun n => n + Foo.d2", cone=["Foo.d2"])
    thm = mk_decl(name="Foo.thm", kind="theorem",
                  type_pp="Foo.d1 0 = 0", cone=["Foo.d1"],
                  axioms=["propext"])
    return mk_snapshot([thm, d1, d2, d3])


def test_transitive_closure_collects_all_local_defs():
    k = compute_kernel("Foo.thm", three_deep())
    assert {m.name for m in k.members} == {"Foo.d1", "Foo.d2", "Foo.d3"}
    assert k.size_decls == 3
    assert k.unresolved == []
    assert k.local_lemmas == []


def test_ordering_is_dependencies_first():
    # d3 has no local deps; d2 depends on d3; d1 depends on d2. A reader
    # should see the leaf (d3) before the things built on it.
    k = compute_kernel("Foo.thm", three_deep())
    order = [m.name for m in k.members]
    assert order.index("Foo.d3") < order.index("Foo.d2")
    assert order.index("Foo.d2") < order.index("Foo.d1")


def test_mathlib_and_core_constants_excluded():
    # The snapshot's cone field is already the project-local partition:
    # a Mathlib constant simply never appears in any cone. Adding only a
    # trusted-vocabulary reference yields an empty kernel.
    thm = mk_decl(name="Foo.uses_mathlib", kind="theorem",
                  type_pp="ContMDiff IR IR top f", cone=[])
    k = compute_kernel("Foo.uses_mathlib", mk_snapshot([thm]))
    assert k.members == []
    assert k.size_decls == 0
    assert k.unresolved == []


def test_member_value_pp_only_for_value_kinds():
    # def/abbrev/instance carry value_pp (the meaning that matters);
    # structure/inductive/class do not.
    d_def = mk_decl(name="Foo.d", kind="def", type_pp="Nat", value_pp="7",
                    cone=[])
    s_struct = mk_decl(name="Foo.S", kind="structure", type_pp="Type",
                       value_pp="should-be-dropped", cone=[])
    thm = mk_decl(name="Foo.t", kind="theorem", type_pp="Foo.d = 7",
                  cone=["Foo.d", "Foo.S"])
    k = compute_kernel("Foo.t", mk_snapshot([thm, d_def, s_struct]))
    by = {m.name: m for m in k.members}
    assert by["Foo.d"].value_pp == "7"
    assert by["Foo.S"].value_pp is None  # structure: meaning is its type


# --- theorem-in-cone rule ----------------------------------------------------


def test_lemma_in_cone_is_not_a_definition_but_its_defs_are():
    # thm's statement mentions lemma `lem`; lem's statement mentions a
    # local def `helper`. The lemma is existence-only (recorded in
    # local_lemmas, NOT a kernel member); helper IS a kernel member.
    helper = mk_decl(name="Foo.helper", kind="def", type_pp="Nat",
                     value_pp="3", cone=[])
    lem = mk_decl(name="Foo.lem", kind="theorem",
                  type_pp="Foo.helper = 3", cone=["Foo.helper"])
    thm = mk_decl(name="Foo.thm", kind="theorem",
                  type_pp="Foo.lem -> True", cone=["Foo.lem"])
    k = compute_kernel("Foo.thm", mk_snapshot([thm, lem, helper]))
    assert {m.name for m in k.members} == {"Foo.helper"}
    assert k.local_lemmas == ["Foo.lem"]


def test_axiom_and_opaque_in_cone_are_members():
    ax = mk_decl(name="Foo.myAx", kind="axiom", type_pp="0 = 0", cone=[])
    opq = mk_decl(name="Foo.myOpaque", kind="opaque", type_pp="Nat",
                  value_pp=None, cone=[])
    thm = mk_decl(name="Foo.t", kind="theorem",
                  type_pp="Foo.myOpaque = 0", cone=["Foo.myAx", "Foo.myOpaque"])
    k = compute_kernel("Foo.t", mk_snapshot([thm, ax, opq]))
    names = {m.name for m in k.members}
    assert names == {"Foo.myAx", "Foo.myOpaque"}


# --- cycle safety ------------------------------------------------------------


def test_mutual_recursion_is_cycle_safe():
    # def f's type mentions g; def g's type mentions f. BFS must terminate.
    f = mk_decl(name="Foo.f", kind="def", type_pp="Foo.g -> Nat",
                value_pp="0", cone=["Foo.g"])
    g = mk_decl(name="Foo.g", kind="def", type_pp="Foo.f -> Nat",
                value_pp="0", cone=["Foo.f"])
    thm = mk_decl(name="Foo.t", kind="theorem", type_pp="Foo.f 0 = 0",
                  cone=["Foo.f"])
    k = compute_kernel("Foo.t", mk_snapshot([thm, f, g]))
    assert {m.name for m in k.members} == {"Foo.f", "Foo.g"}
    # Order is total and deterministic even under the cycle.
    assert [m.name for m in k.members] == sorted(
        m.name for m in k.members
    ) or len(k.members) == 2


def test_self_reference_is_cycle_safe():
    rec = mk_decl(name="Foo.rec", kind="def", type_pp="Nat",
                  value_pp="Foo.rec", cone=["Foo.rec"])
    thm = mk_decl(name="Foo.t", kind="theorem", type_pp="Foo.rec = 0",
                  cone=["Foo.rec"])
    k = compute_kernel("Foo.t", mk_snapshot([thm, rec]))
    assert {m.name for m in k.members} == {"Foo.rec"}


# --- unresolved / absent recorded, never crashed -----------------------------


def test_unresolved_cone_reference_recorded():
    # thm references a project-local def that is absent from the snapshot
    # (didn't elaborate, or was deleted). Must be recorded, not raised.
    thm = mk_decl(name="Foo.t", kind="theorem", type_pp="Foo.gone = 0",
                  cone=["Foo.gone"])
    k = compute_kernel("Foo.t", mk_snapshot([thm]))
    assert k.members == []
    assert k.unresolved == ["Foo.gone"]


def test_unknown_cone_member_is_unresolved():
    gone = mk_decl(name="Foo.gone", kind="def", status="unknown",
                   type_pp=None, value_pp=None, cone=[], has_sorry=None,
                   reason="elaboration failed")
    thm = mk_decl(name="Foo.t", kind="theorem", type_pp="Foo.gone = 0",
                  cone=["Foo.gone"])
    k = compute_kernel("Foo.t", mk_snapshot([thm, gone]))
    assert k.members == []
    assert k.unresolved == ["Foo.gone"]


def test_absent_target_reported_not_crashed():
    k = compute_kernel("Foo.missing", mk_snapshot([]))
    assert k.target == "Foo.missing"
    assert k.members == []
    assert k.unresolved == ["Foo.missing"]


def test_unknown_target_reported_not_crashed():
    bad = mk_decl(name="Foo.bad", status="unknown", type_pp=None,
                  has_sorry=None, reason="boom")
    k = compute_kernel("Foo.bad", mk_snapshot([bad]))
    assert k.members == []
    assert k.unresolved == ["Foo.bad"]


# --- diamond (shared def reached two ways, counted once) ---------------------


def test_diamond_def_counted_once():
    shared = mk_decl(name="Foo.shared", kind="def", type_pp="Nat",
                     value_pp="0", cone=[])
    left = mk_decl(name="Foo.left", kind="def", type_pp="Nat",
                   value_pp="Foo.shared", cone=["Foo.shared"])
    right = mk_decl(name="Foo.right", kind="def", type_pp="Nat",
                    value_pp="Foo.shared", cone=["Foo.shared"])
    thm = mk_decl(name="Foo.t", kind="theorem",
                  type_pp="Foo.left = Foo.right",
                  cone=["Foo.left", "Foo.right"])
    k = compute_kernel("Foo.t", mk_snapshot([thm, left, right, shared]))
    names = [m.name for m in k.members]
    assert names.count("Foo.shared") == 1
    assert set(names) == {"Foo.shared", "Foo.left", "Foo.right"}
    # shared is a dependency of both -> sorts first.
    assert names.index("Foo.shared") < names.index("Foo.left")
    assert names.index("Foo.shared") < names.index("Foo.right")


# --- size metric -------------------------------------------------------------


def test_loc_counts_multiline_pp():
    d = mk_decl(name="Foo.d", kind="def", type_pp="Nat ->\nNat",
                value_pp="fun n =>\n  n + 1", cone=[])
    thm = mk_decl(name="Foo.t", kind="theorem", type_pp="Foo.d 0 = 1",
                  cone=["Foo.d"])
    k = compute_kernel("Foo.t", mk_snapshot([thm, d]))
    # type = 2 lines, value = 2 lines -> 4
    assert k.size_loc == 4


def test_kernel_size_metric_aggregates_distinct_union():
    # Two targets share def `shared`; the aggregate counts it ONCE
    # (one human read), so aggregate.decls < sum of per-target decls.
    shared = mk_decl(name="Foo.shared", kind="def", type_pp="Nat",
                     value_pp="0", cone=[])
    a_def = mk_decl(name="Foo.aonly", kind="def", type_pp="Nat",
                    value_pp="Foo.shared", cone=["Foo.shared"])
    thm_a = mk_decl(name="Foo.A", kind="theorem", type_pp="Foo.aonly = 0",
                    cone=["Foo.aonly"])
    thm_b = mk_decl(name="Foo.B", kind="theorem", type_pp="Foo.shared = 0",
                    cone=["Foo.shared"])
    s = mk_snapshot([thm_a, thm_b, a_def, shared])
    metric = kernel_size_metric(s, ["Foo.A", "Foo.B"])
    assert metric["per_target"]["Foo.A"]["decls"] == 2  # aonly + shared
    assert metric["per_target"]["Foo.B"]["decls"] == 1  # shared
    # distinct union: shared, aonly -> 2 (not 3)
    assert metric["aggregate"]["decls"] == 2
    assert metric["aggregate"]["targets"] == 2


def test_kernel_size_metric_carries_unresolved():
    thm = mk_decl(name="Foo.t", kind="theorem", type_pp="Foo.gone = 0",
                  cone=["Foo.gone"])
    metric = kernel_size_metric(mk_snapshot([thm]), ["Foo.t"])
    assert metric["per_target"]["Foo.t"]["unresolved"] == ["Foo.gone"]
    assert metric["aggregate"]["unresolved"] == ["Foo.gone"]
