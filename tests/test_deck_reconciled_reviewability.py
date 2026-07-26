"""Reviewability must be derived from the declarations the subdeck reviews.

These regressions cover rename/split reconciliation specifically.  They are
fully offline: issue metadata is monkeypatched, audit snapshots are fabricated,
and the Lean files are small fixtures under ``tmp_path``.
"""

from __future__ import annotations

from dataclasses import replace

import marathon.deck.cards as cards
from marathon.deck.subdeck import build_subdeck
from marathon.deck.workspace import build_review_workspace
from test_deck_source_fallback import (
    _SPLIT_SRC,
    _bundle_body,
    _cfg_two_issues,
    _decl,
    install_snapshot,
    make_cfg,
    make_snapshot,
    patch_bulk,
)
from marathon.review.config import ChapterRegistry


def _install_split_fixture(
    tmp_path,
    monkeypatch,
    *,
    split_axioms=("propext",),
    include_current_part: bool = False,
):
    cfg = _cfg_two_issues(tmp_path)
    source_dir = cfg.repo_dir / "Demo" / "Chapter14"
    source_dir.mkdir(parents=True, exist_ok=True)
    current_source = ""
    decls = []
    cited = ["Prop_14_5"]
    if include_current_part:
        current_source = """\
/-- Proposition 14.5, a current cited part. -/
theorem CurrentPart : True := trivial

"""
        decls.append(_decl("CurrentPart", module="Demo.Chapter14"))
        cited.insert(0, "CurrentPart")
    (source_dir / "Split.lean").write_text(current_source + _SPLIT_SRC)
    decls.extend([
        _decl(
            "Prop_14_5_preserving",
            module="Demo.Chapter14",
            axioms=split_axioms,
        ),
        _decl(
            "Prop_14_5_reversing",
            module="Demo.Chapter14",
            axioms=split_axioms,
        ),
        _decl("Prop_14_5_unrelated_helper", module="Demo.Chapter14"),
        _decl("SiblingResult", module="Demo.Chapter14"),
    ])
    install_snapshot(cfg, make_snapshot(decls, cfg.repo_dir))
    patch_bulk(monkeypatch, {
        22: {
            "title": "Proposition 14.5 — diffeomorphism invariance",
            "state": "OPEN",
            "labels": set(),
            "body": _bundle_body(*cited),
        },
        23: {
            "title": "Chapter 14 sibling",
            "state": "OPEN",
            "labels": set(),
            "body": _bundle_body("SiblingResult"),
        },
    })
    return cfg


def _reviewability_surfaces(cfg, issue: int):
    queue_card = next(
        card
        for card in cards.build_queue(cfg, chapter=14).cards
        if card.id == issue
    )
    detail = cards.build_card_detail(cfg, issue)
    subdeck = build_subdeck(cfg, issue)
    workspace_issue = next(
        item
        for item in build_review_workspace(cfg, 14)["issues"]
        if item["id"] == issue
    )
    return queue_card, detail, subdeck, workspace_issue


def test_all_stale_split_uses_one_aligned_t1_reviewability(
    tmp_path, monkeypatch,
):
    """A recovered split is the T1 issue the human actually reviews everywhere."""
    cfg = _install_split_fixture(tmp_path, monkeypatch)

    queue_card, detail, subdeck, workspace_issue = _reviewability_surfaces(
        cfg, 22
    )

    expected = ("T1", True, True, False)
    assert (
        queue_card.tier,
        queue_card.inspectable,
        queue_card.shippable,
        queue_card.provisional,
    ) == expected
    assert (
        detail.tier,
        detail.inspectable,
        detail.shippable,
        detail.provisional,
    ) == expected
    assert (
        subdeck["card_tier"],
        subdeck["inspectable"],
        subdeck["shippable"],
        subdeck["provisional"],
    ) == expected
    assert (
        workspace_issue["tier"],
        workspace_issue["inspectable"],
        workspace_issue["shippable"],
        workspace_issue["provisional"],
    ) == expected
    assert {card["name"] for card in subdeck["cards"]} == {
        "Prop_14_5_preserving",
        "Prop_14_5_reversing",
    }


def test_partial_stale_t1_citation_cannot_hide_surfaced_t0(
    tmp_path, monkeypatch,
):
    """The gate is the worst tier over the complete reconciled review surface."""
    cfg = _install_split_fixture(
        tmp_path,
        monkeypatch,
        split_axioms=("propext", "Demo.cheat"),
        include_current_part=True,
    )

    queue_card, detail, subdeck, workspace_issue = _reviewability_surfaces(
        cfg, 22
    )

    expected = ("T0", False, False, False)
    assert (
        queue_card.tier,
        queue_card.inspectable,
        queue_card.shippable,
        queue_card.provisional,
    ) == expected
    assert (
        detail.tier,
        detail.inspectable,
        detail.shippable,
        detail.provisional,
    ) == expected
    assert (
        subdeck["card_tier"],
        subdeck["inspectable"],
        subdeck["shippable"],
        subdeck["provisional"],
    ) == expected
    assert (
        workspace_issue["tier"],
        workspace_issue["inspectable"],
        workspace_issue["shippable"],
        workspace_issue["provisional"],
    ) == expected
    assert {
        card["name"]: card["tier"] for card in subdeck["cards"]
    } == {
        "CurrentPart": "T1",
        "Prop_14_5_preserving": "T0",
        "Prop_14_5_reversing": "T0",
    }
    assert "tier T0" in subdeck["inspection_blocked_reason"]


def test_one_replacement_cannot_cover_two_stale_citations(
    tmp_path, monkeypatch,
):
    """Every stale citation needs its own trustworthy reconciliation."""
    cfg = _cfg_two_issues(tmp_path)
    source_dir = cfg.repo_dir / "Demo" / "Chapter14"
    source_dir.mkdir(parents=True, exist_ok=True)
    (source_dir / "PartialSplit.lean").write_text("""\
import Mathlib

/-- Proposition 14.5, the recovered first part. -/
theorem OldA_part : True := trivial

/-- A separately registered sibling result. -/
theorem SiblingResult : True := trivial
""")
    install_snapshot(cfg, make_snapshot([
        _decl("OldA_part", module="Demo.Chapter14"),
        _decl("SiblingResult", module="Demo.Chapter14"),
    ], cfg.repo_dir))
    patch_bulk(monkeypatch, {
        22: {
            "title": "Proposition 14.5 — two-part result",
            "state": "OPEN",
            "labels": set(),
            # OldA has a surfaced split child; OldB has no replacement.
            "body": _bundle_body("OldA", "OldB"),
        },
        23: {
            "title": "Chapter 14 sibling",
            "state": "OPEN",
            "labels": set(),
            "body": _bundle_body("SiblingResult"),
        },
    })

    queue_card, detail, subdeck, workspace_issue = _reviewability_surfaces(
        cfg, 22
    )

    expected = ("-", False, False, False)
    assert (
        queue_card.tier,
        queue_card.inspectable,
        queue_card.shippable,
        queue_card.provisional,
    ) == expected
    assert (
        detail.tier,
        detail.inspectable,
        detail.shippable,
        detail.provisional,
    ) == expected
    assert (
        subdeck["card_tier"],
        subdeck["inspectable"],
        subdeck["shippable"],
        subdeck["provisional"],
    ) == expected
    assert (
        workspace_issue["tier"],
        workspace_issue["inspectable"],
        workspace_issue["shippable"],
        workspace_issue["provisional"],
    ) == expected
    assert subdeck["reconcile"]["dropped"] == ["OldA", "OldB"]
    assert [card["name"] for card in subdeck["cards"]] == ["OldA_part"]


def test_qualified_split_cannot_cover_same_leaf_in_other_namespace(
    tmp_path, monkeypatch,
):
    """N1.OldA_part is not a replacement for the distinct N2.OldA."""
    cfg = _cfg_two_issues(tmp_path)
    source_dir = cfg.repo_dir / "Demo" / "Chapter14"
    source_dir.mkdir(parents=True, exist_ok=True)
    (source_dir / "QualifiedSplit.lean").write_text("""\
import Mathlib

namespace N1

/-- Proposition 14.5, the recovered N1 part. -/
theorem OldA_part : True := trivial

end N1

/-- A separately registered sibling result. -/
theorem SiblingResult : True := trivial
""")
    install_snapshot(cfg, make_snapshot([
        _decl("N1.OldA_part", module="Demo.Chapter14"),
        _decl("SiblingResult", module="Demo.Chapter14"),
    ], cfg.repo_dir))
    patch_bulk(monkeypatch, {
        22: {
            "title": "Proposition 14.5 — duplicate leaves",
            "state": "OPEN",
            "labels": set(),
            "body": _bundle_body("N1.OldA", "N2.OldA"),
        },
        23: {
            "title": "Chapter 14 sibling",
            "state": "OPEN",
            "labels": set(),
            "body": _bundle_body("SiblingResult"),
        },
    })

    queue_card, detail, subdeck, workspace_issue = _reviewability_surfaces(
        cfg, 22
    )

    expected = ("-", False, False, False)
    assert (
        queue_card.tier,
        queue_card.inspectable,
        queue_card.shippable,
        queue_card.provisional,
    ) == expected
    assert (
        detail.tier,
        detail.inspectable,
        detail.shippable,
        detail.provisional,
    ) == expected
    assert (
        subdeck["card_tier"],
        subdeck["inspectable"],
        subdeck["shippable"],
        subdeck["provisional"],
    ) == expected
    assert (
        workspace_issue["tier"],
        workspace_issue["inspectable"],
        workspace_issue["shippable"],
        workspace_issue["provisional"],
    ) == expected
    assert subdeck["reconcile"]["dropped"] == [
        "N1.OldA",
        "N2.OldA",
    ]
    assert [card["name"] for card in subdeck["cards"]] == [
        "N1.OldA_part"
    ]


def test_queue_fails_closed_when_reconciliation_ledger_open_fails(
    tmp_path, monkeypatch,
):
    """A second ledger failure cannot restore the unsafe raw cited tier."""
    cfg = _install_split_fixture(
        tmp_path,
        monkeypatch,
        split_axioms=("propext", "Demo.cheat"),
        include_current_part=True,
    )

    def broken_ledger(_cfg):
        raise RuntimeError("ledger unavailable")

    # _load_context has already-independent tier machinery.  This specifically
    # exercises the later ledger open used to assemble reconciled surfaces.
    monkeypatch.setattr(cards, "_ledger", broken_ledger)

    queue_card = next(
        card
        for card in cards.build_queue(cfg, chapter=14).cards
        if card.id == 22
    )

    assert (
        queue_card.tier,
        queue_card.inspectable,
        queue_card.shippable,
        queue_card.provisional,
    ) == ("-", False, False, False)


def test_surfaced_replacement_retains_issue_dependency_ownership(
    tmp_path, monkeypatch,
):
    """A downstream theorem waits on the issue owning its surfaced dependency."""
    cfg = _cfg_two_issues(tmp_path)
    source_dir = cfg.repo_dir / "Demo" / "Chapter14"
    source_dir.mkdir(parents=True, exist_ok=True)
    source = _SPLIT_SRC + """\

/-- Proposition 14.6, using the preserving part of Proposition 14.5. -/
theorem DependentResult : True := trivial
"""
    (source_dir / "Split.lean").write_text(source)
    dependent = replace(
        _decl("DependentResult", module="Demo.Chapter14"),
        cone=["Prop_14_5_preserving"],
    )
    install_snapshot(cfg, make_snapshot([
        _decl("Prop_14_5_preserving", module="Demo.Chapter14"),
        _decl("Prop_14_5_reversing", module="Demo.Chapter14"),
        _decl("Prop_14_5_unrelated_helper", module="Demo.Chapter14"),
        dependent,
    ], cfg.repo_dir))
    patch_bulk(monkeypatch, {
        22: {
            "title": "Proposition 14.5 — diffeomorphism invariance",
            "state": "OPEN",
            "labels": set(),
            "body": _bundle_body("Prop_14_5"),
        },
        23: {
            "title": "Proposition 14.6 — dependent result",
            "state": "OPEN",
            "labels": set(),
            "body": _bundle_body("DependentResult"),
        },
    })

    queue_card, detail, subdeck, workspace_issue = _reviewability_surfaces(
        cfg, 23
    )

    assert [dep.id for dep in detail.deps] == [22]
    assert [dep["id"] for dep in subdeck["deps"]] == [22]
    assert workspace_issue["deps"] == [22]
    assert (
        queue_card.inspectable,
        queue_card.shippable,
        queue_card.provisional,
    ) == (True, False, True)
    assert (
        detail.inspectable,
        detail.shippable,
        detail.provisional,
    ) == (True, False, True)
    assert (
        subdeck["inspectable"],
        subdeck["shippable"],
        subdeck["provisional"],
    ) == (True, False, True)


def test_detail_uses_global_gate_for_cross_chapter_dependency(
    tmp_path, monkeypatch,
):
    """A later-chapter detail remains provisional behind an earlier issue."""
    cfg = replace(
        make_cfg(tmp_path),
        chapters={
            14: ChapterRegistry(chapter=14, entries=[(22, "Upstream")]),
            15: ChapterRegistry(chapter=15, entries=[(23, "Downstream")]),
        },
    )
    source = cfg.repo_dir / "Demo" / "Chapter14.lean"
    source.parent.mkdir(parents=True, exist_ok=True)
    source.write_text("""\
import Mathlib

namespace Demo

def A : Nat := 0

theorem B : A = A := rfl

end Demo
""")
    downstream = replace(
        _decl("Demo.B", module="Demo.Chapter14"),
        cone=["Demo.A"],
    )
    install_snapshot(cfg, make_snapshot([
        _decl("Demo.A", kind="def", module="Demo.Chapter14"),
        downstream,
    ], cfg.repo_dir))
    patch_bulk(monkeypatch, {
        22: {
            "title": "Upstream definition",
            "state": "OPEN",
            "labels": set(),
            "body": _bundle_body("Demo.A"),
        },
        23: {
            "title": "Downstream theorem",
            "state": "OPEN",
            "labels": set(),
            "body": _bundle_body("Demo.B"),
        },
    })

    queue_card = next(
        card for card in cards.build_queue(cfg, chapter=None).cards
        if card.id == 23
    )
    detail = cards.build_card_detail(cfg, 23)
    subdeck = build_subdeck(cfg, 23)
    workspace_issue = next(
        issue
        for issue in build_review_workspace(cfg, 15)["issues"]
        if issue["id"] == 23
    )

    assert (
        queue_card.inspectable,
        queue_card.shippable,
        queue_card.provisional,
    ) == (True, False, True)
    assert (
        detail.inspectable,
        detail.shippable,
        detail.provisional,
    ) == (True, False, True)
    assert [dep.id for dep in detail.deps] == [22]
    assert (
        subdeck["inspectable"],
        subdeck["shippable"],
        subdeck["provisional"],
    ) == (True, False, True)
    assert [dep["id"] for dep in subdeck["deps"]] == [22]
    assert (
        workspace_issue["inspectable"],
        workspace_issue["shippable"],
        workspace_issue["provisional"],
    ) == (True, False, True)
    assert workspace_issue["deps"] == [22]
