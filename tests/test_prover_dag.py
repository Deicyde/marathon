"""Prove-ordering DAG — T2 decl graph, T1 issue graph, leaves-first order."""
from __future__ import annotations

from dataclasses import dataclass, field

from marathon.prover import dag
from marathon.review.fill_discovery import FillTarget


# --- lightweight audit-snapshot stub ---------------------------------------

@dataclass
class FakeDecl:
    name: str
    kind: str = "theorem"
    cone: list = field(default_factory=list)
    axioms: list = field(default_factory=list)
    has_sorry: object = None  # bool | None


class FakeSnap:
    def __init__(self, decls):
        self._by = {d.name: d for d in decls}

    def by_name(self):
        return self._by


def _t(issue, *decls, chapter=14):
    return FillTarget(issue=issue, chapter=chapter, decls=list(decls))


# --- name resolution --------------------------------------------------------

def test_resolve_decl_exact_and_unique_suffix_and_ambiguous():
    by_name = {"Ns.foo": 1, "Other.bar": 1}
    assert dag.resolve_decl("Ns.foo", by_name) == "Ns.foo"   # exact
    assert dag.resolve_decl("foo", by_name) == "Ns.foo"      # unique suffix
    assert dag.resolve_decl("missing", by_name) is None
    # ambiguous suffix → None
    assert dag.resolve_decl("x", {"A.x": 1, "B.x": 1}) is None


# --- T2 decl graph ----------------------------------------------------------

def test_build_decl_graph_edges_filtered_to_in_snapshot_non_self():
    snap = FakeSnap([
        FakeDecl("Leaf", cone=["Mathlib.Foo", "Leaf"]),        # trusted + self dropped
        FakeDecl("Mid", cone=["Leaf", "Gone"]),                # Gone not in snapshot
    ])
    g = dag.build_decl_graph(snap)
    assert g["Leaf"].deps == ()                                # only self/trusted → empty
    assert g["Mid"].deps == ("Leaf",)                          # Gone filtered out


def test_clean_predicate_requires_no_sorry_and_no_sorryax():
    snap = FakeSnap([
        FakeDecl("proved", has_sorry=False, axioms=["propext"]),
        FakeDecl("still_sorry", has_sorry=True, axioms=[]),
        FakeDecl("cites_sorry", has_sorry=False, axioms=["sorryAx"]),
        FakeDecl("unknown", has_sorry=None, axioms=[]),
    ])
    g = dag.build_decl_graph(snap)
    assert g["proved"].clean is True
    assert g["still_sorry"].clean is False       # own body still sorry
    assert g["cites_sorry"].clean is False       # transitive sorryAx
    assert g["unknown"].clean is False           # unknown status → not clean


def test_transitive_deps_is_cycle_safe():
    snap = FakeSnap([
        FakeDecl("A", cone=["B"]),
        FakeDecl("B", cone=["C"]),
        FakeDecl("C", cone=["A"]),   # cycle A->B->C->A
    ])
    g = dag.build_decl_graph(snap)
    assert dag.transitive_deps("A", g) == {"B", "C"}   # self excluded, terminates


# --- T1 issue graph ---------------------------------------------------------

def test_issue_dependency_edges_lift_decl_cone_to_issues():
    # Root(type)->Mid, Mid(type)->Leaf. Each owned by a distinct issue.
    snap = FakeSnap([
        FakeDecl("Leaf", cone=[]),
        FakeDecl("Mid", cone=["Leaf"]),
        FakeDecl("Root", cone=["Mid"]),
    ])
    targets = [_t(1, "Leaf"), _t(2, "Mid"), _t(3, "Root")]
    deps = dag.issue_dependency_edges(targets, snap)
    assert deps[1] == set()          # leaf
    assert deps[2] == {1}            # Mid depends on Leaf's issue
    assert deps[3] == {1, 2}         # Root transitively depends on both


def test_issue_edges_ignore_deps_on_already_proven_non_targets():
    # Root cones onto Base, but Base is not a pending target → no issue edge.
    snap = FakeSnap([FakeDecl("Base", cone=[]), FakeDecl("Root", cone=["Base"])])
    targets = [_t(3, "Root")]        # Base already proved, not in the queue
    deps = dag.issue_dependency_edges(targets, snap)
    assert deps == {3: set()}        # Root is a leaf among pending work


# --- topo order -------------------------------------------------------------

def test_topo_order_leaves_first_with_issue_number_tiebreak():
    deps = {1: set(), 2: {1}, 3: {2}, 4: set()}
    # 1 and 4 are both leaves → tie broken by issue number (1 before 4)
    assert dag.topo_order_issues([3, 2, 1, 4], deps) == [1, 4, 2, 3]


def test_topo_order_is_total_under_cycles():
    deps = {1: {2}, 2: {1}, 3: set()}   # 1<->2 cycle, 3 a leaf
    order = dag.topo_order_issues([1, 2, 3], deps)
    assert order[0] == 3                 # the real leaf first
    assert set(order) == {1, 2, 3}       # cycle remnants still placed


# --- the conductor entry point ---------------------------------------------

def test_order_targets_reorders_leaves_first():
    snap = FakeSnap([
        FakeDecl("Leaf", cone=[]),
        FakeDecl("Mid", cone=["Leaf"]),
        FakeDecl("Root", cone=["Mid"]),
    ])
    # given in ROOT-first order; must come back LEAF-first
    targets = [_t(3, "Root"), _t(2, "Mid"), _t(1, "Leaf")]
    ordered = dag.order_targets(targets, snap)
    assert [t.issue for t in ordered] == [1, 2, 3]


def test_order_targets_falls_back_to_input_order_without_snapshot():
    targets = [_t(3, "Root"), _t(1, "Leaf")]
    assert dag.order_targets(targets, None) == targets          # unchanged
    assert dag.order_targets([_t(9, "X")], None) == [_t(9, "X")]  # single


def test_order_targets_preserves_all_targets():
    snap = FakeSnap([FakeDecl("A", cone=[]), FakeDecl("B", cone=["A"])])
    targets = [_t(2, "B"), _t(1, "A"), _t(5, "Unresolvable")]
    ordered = dag.order_targets(targets, snap)
    assert {t.issue for t in ordered} == {1, 2, 5}   # nothing dropped
    assert ordered.index(next(t for t in ordered if t.issue == 1)) \
        < ordered.index(next(t for t in ordered if t.issue == 2))  # A before B


def test_build_prover_dag_exposes_both_tiers_and_leaves():
    snap = FakeSnap([
        FakeDecl("Leaf", cone=[]),
        FakeDecl("Root", cone=["Leaf"]),
    ])
    d = dag.build_prover_dag([_t(1, "Leaf"), _t(2, "Root")], snap)
    assert set(d.decls) == {"Leaf", "Root"}     # T2
    assert d.issue_deps == {1: set(), 2: {1}}   # T1
    assert d.order == [1, 2]
    assert d.leaves() == [1]                     # proving starts at issue 1
