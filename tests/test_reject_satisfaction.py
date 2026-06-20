"""Reject-note parsing + the satisfaction judge (the foundation of the refine
loop's "did Aristotle deliver the asks?" check)."""

from __future__ import annotations

from marathon.deck.subdeck import compose_ship_note
from marathon.review.reject_note import parse_reject_note
from marathon.review.satisfaction import (
    judge_satisfaction,
    compose_corrective_prompt,
    is_no_progress,
    SatisfactionResult,
)

_SRC = """\
import Mathlib

namespace Demo.Chapter14

/-- kept -/
theorem Foo.tprod : True := trivial

end Demo.Chapter14
"""


def _write_target(tmp_path):
    d = tmp_path / "Demo" / "Chapter14"
    d.mkdir(parents=True)
    (d / "Tf.lean").write_text(_SRC)
    return "Demo/Chapter14"


def test_compose_then_parse_round_trips():
    """The note the deck ships parses back into the same KEEP/FIX/ADD items —
    the header-coupling guard (compose + parse share one header source)."""
    note = compose_ship_note(
        94,
        {"kept": ["Foo.tprod"],
         "rejected": [{"name": "Foo.bar", "note": "use ≥ not >"}],
         "deferred": []},
        missing="the CovariantTensorField smul-smoothness lemma",
    )
    p = parse_reject_note(note)
    assert p.parse_ok is True
    assert p.keep == ["Foo.tprod"]
    assert p.fix == [("Foo.bar", "use ≥ not >")]
    assert p.add == ["the CovariantTensorField smul-smoothness lemma"]


def test_deterministic_kept_decl_removed_is_unmet(tmp_path):
    target = _write_target(tmp_path)
    # KEEP names a decl that is NOT in the code → unmet (the refine deleted it).
    note = compose_ship_note(
        1, {"kept": ["Foo.ghost"], "rejected": [], "deferred": []}, missing=None)
    r = judge_satisfaction(note, tmp_path, target)
    assert r.satisfied is False
    assert any("ghost" in u for u in r.unmet_items)


def test_deterministic_named_add_present_is_satisfied(tmp_path):
    target = _write_target(tmp_path)
    # An ADD ask that NAMES a decl already present (Foo.tprod) → satisfied,
    # decided without an LLM.
    note = compose_ship_note(
        1, {"kept": [], "rejected": [], "deferred": []},
        missing="add `Demo.Chapter14.Foo.tprod`")
    r = judge_satisfaction(note, tmp_path, target)
    assert r.satisfied is True and r.decided_by == "deterministic"


def test_named_add_absent_is_unmet(tmp_path):
    target = _write_target(tmp_path)
    note = compose_ship_note(
        1, {"kept": [], "rejected": [], "deferred": []},
        missing="add `Demo.Chapter14.Foo.notThere`")
    r = judge_satisfaction(note, tmp_path, target)
    assert r.satisfied is False
    assert any("notThere" in u for u in r.unmet_items)


def test_free_text_add_without_llm_needs_human(tmp_path):
    """A descriptive ADD (#94's CovariantTensorField ask) can't be name-checked;
    with no LLM judge it is UNDECIDED → needs_human (open PR, don't loop)."""
    target = _write_target(tmp_path)
    note = compose_ship_note(
        94, {"kept": [], "rejected": [], "deferred": []},
        missing="the corresponding definitions for CovariantTensorField")
    r = judge_satisfaction(note, tmp_path, target)
    assert r.needs_human is True and r.decided_by == "fallback"


def test_free_text_add_with_llm_judge_decides(tmp_path):
    """With an injected LLM judge, the free-text ADD is decided. The #94 case:
    the judge reports the CovariantTensorField ADD still unmet → not satisfied."""
    target = _write_target(tmp_path)
    note = compose_ship_note(
        94, {"kept": [], "rejected": [], "deferred": []},
        missing="the corresponding definitions for CovariantTensorField")

    def fake_llm(_note, _diff, asks):
        return SatisfactionResult(False, ["ADD: CovariantTensorField smul-smoothness missing"])

    r = judge_satisfaction(note, tmp_path, target, diff_text="diff", llm_judge=fake_llm)
    assert r.satisfied is False and r.decided_by == "llm"
    assert any("CovariantTensorField" in u for u in r.unmet_items)


def test_unstructured_note_is_fallback_satisfied(tmp_path):
    target = _write_target(tmp_path)
    r = judge_satisfaction("just some freeform rejection text", tmp_path, target)
    assert r.satisfied is True and r.decided_by == "fallback"


def test_corrective_prompt_lists_only_unmet():
    p = compose_corrective_prompt(94, ["ADD: CovariantTensorField smul-smoothness"])
    assert "#94" in p
    assert "CovariantTensorField" in p
    assert "STILL outstanding" in p


def test_no_progress_detector():
    assert is_no_progress(["x"], ["x"]) is True
    assert is_no_progress(["x"], ["x", "y"]) is False
    assert is_no_progress([], []) is False  # nothing unmet ≠ stuck
