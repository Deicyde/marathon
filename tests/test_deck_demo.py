"""Tests for the deck's --demo mode (marathon.deck.demo): synthetic cards,
in-memory verdicts, and a SIMULATED ship that fires nothing irreversible."""

from __future__ import annotations

import pytest

from marathon.deck import demo


@pytest.fixture(autouse=True)
def _reset_demo():
    demo.reset()
    yield
    demo.reset()


def test_queue_lists_demo_issues_all_ready():
    q = demo.demo_queue()
    ids = [c["id"] for c in q["cards"]]
    assert 9001 in ids and 9002 in ids
    assert all(c["ready"] for c in q["cards"])  # no dep gate in demo


def test_subdeck_has_per_decl_cards_and_worst_tier():
    sd = demo.demo_subdeck(9001)
    assert sd["progress"] == {"reviewed": 0, "total": 3}
    # card_tier is the worst over the decls (comp is T0 with a sorry).
    assert sd["card_tier"] == "T0"
    assert {c["name"] for c in sd["cards"]} == {
        "Demo.MonoidHom", "Demo.MonoidHom.map_pow", "Demo.MonoidHom.comp",
    }


def test_reject_requires_a_note():
    with pytest.raises(ValueError):
        demo.demo_record_verdict(9001, "Demo.MonoidHom", "reject", note="")


def test_defer_does_not_count_as_reviewed():
    demo.demo_record_verdict(9001, "Demo.MonoidHom", "defer")
    sd = demo.demo_subdeck(9001)
    assert sd["progress"]["reviewed"] == 0
    assert not sd["complete"]


def test_ship_refuses_incomplete_subdeck():
    demo.demo_record_verdict(9001, "Demo.MonoidHom", "verify")
    res = demo.demo_ship(9001)
    assert res["ok"] is False  # two decls still unreviewed


def test_ship_all_verified_is_verified_outcome():
    for name in ("Demo.IsEven", "Demo.isEven_add"):
        demo.demo_record_verdict(9002, name, "verify")
    res = demo.demo_ship(9002)
    assert res["ok"] and res["outcome"] == "verified"
    assert "Aristotle" not in res["message"] or "no Aristotle" in res["message"]


def test_ship_with_a_rejection_composes_the_keep_fix_note():
    demo.demo_record_verdict(9001, "Demo.MonoidHom", "verify")
    demo.demo_record_verdict(9001, "Demo.MonoidHom.map_pow", "verify")
    demo.demo_record_verdict(
        9001, "Demo.MonoidHom.comp", "reject", note="fill the map_one sorry"
    )
    res = demo.demo_ship(9001)
    assert res["ok"] and res["outcome"] == "shipped"
    msg = res["message"]
    # The composed instruction keeps the verified decls and carries the
    # rejected decl's note VERBATIM.
    assert "KEEP these declarations" in msg
    assert "Demo.MonoidHom" in msg and "Demo.MonoidHom.map_pow" in msg
    assert "FIX these declarations" in msg
    assert "Demo.MonoidHom.comp: fill the map_one sorry" in msg


def test_demo_partial_reject_leaves_later_declarations_untouched():
    demo.demo_record_verdict(9001, "Demo.MonoidHom", "verify")
    demo.demo_record_verdict(
        9001, "Demo.MonoidHom.comp", "reject", note="fill the map_one sorry"
    )
    # map_pow is intentionally not reviewed before the upstream fix ships.
    res = demo.demo_ship(9001)
    assert res["ok"] and res["outcome"] == "shipped"
    assert res["partial"] is True
    assert "Stopped at first rejection" in res["message"]
    assert "Demo.MonoidHom.map_pow" not in res["message"]


def test_ship_clears_the_issue_and_advances():
    for name in ("Demo.IsEven", "Demo.isEven_add"):
        demo.demo_record_verdict(9002, name, "verify")
    demo.demo_ship(9002)
    # Cleared: a fresh subdeck shows zero reviewed again.
    assert demo.demo_subdeck(9002)["progress"]["reviewed"] == 0


# --- the leading informal-approval card (demo mirror) -----------------------

from marathon.deck.subdeck import INFORMAL_KEY  # noqa: E402


def test_demo_subdeck_carries_the_informal_slot():
    sd = demo.demo_subdeck(9001)
    inf = sd["informal"]
    assert inf is not None and inf["is_llm"] is True
    assert inf["verdict"] is None  # not yet acted on
    # ONE top-level statement (the whole result), NOT per-decl items
    assert "items" not in inf
    assert "monoid homomorphism" in inf["statement"]


def test_demo_approve_informal_reflects_and_isolates():
    demo.demo_record_verdict(9001, INFORMAL_KEY, "verify")
    sd = demo.demo_subdeck(9001)
    assert sd["informal"]["verdict"] == "verified"
    # the approval is NOT a card and does not count toward progress
    assert INFORMAL_KEY not in {c["name"] for c in sd["cards"]}
    assert sd["progress"] == {"reviewed": 0, "total": 3}


def test_demo_ship_reports_informal_verified_only_when_approved():
    # approved: ship reports the flip
    demo.demo_record_verdict(9001, INFORMAL_KEY, "verify")
    for name in ("Demo.MonoidHom", "Demo.MonoidHom.map_pow", "Demo.MonoidHom.comp"):
        demo.demo_record_verdict(9001, name, "verify")
    res = demo.demo_ship(9001)
    assert res["informal_verified"] is True
    assert "informal statement verified" in res["message"]


def test_demo_ship_without_informal_approval_does_not_flip():
    for name in ("Demo.IsEven", "Demo.isEven_add"):
        demo.demo_record_verdict(9002, name, "verify")
    res = demo.demo_ship(9002)
    assert res["informal_verified"] is False
    assert "informal statement verified" not in res["message"]
