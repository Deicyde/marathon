"""Repair faithfulness jury — before/after weakening detection + block semantics."""
from __future__ import annotations

from types import SimpleNamespace

import marathon.repair_jury as rj


def test_parse_fail_on_weakened():
    passed, weak, _ = rj._parse(
        '{"verdict": "fail", "weakened": [{"decl": "Prop_16_1", "reason": "dropped hyp"}]}'
    )
    assert passed is False
    assert weak == [{"decl": "Prop_16_1", "reason": "dropped hyp"}]


def test_parse_pass():
    passed, weak, _ = rj._parse('{"verdict": "pass", "weakened": []}')
    assert passed is True and weak == []


def test_parse_weakened_list_forces_fail_even_if_verdict_says_pass():
    passed, weak, _ = rj._parse('{"verdict": "pass", "weakened": [{"decl": "X"}]}')
    assert passed is False and len(weak) == 1


def test_parse_undetermined_on_garbage():
    passed, weak, note = rj._parse("not json at all")
    assert passed is None and weak == []


def test_verdict_blocks_only_on_affirmative_fail():
    assert rj.RepairFaithfulnessVerdict(ran=True, passed=False).blocks is True
    assert rj.RepairFaithfulnessVerdict(ran=True, passed=True).blocks is False
    assert rj.RepairFaithfulnessVerdict(ran=True, passed=None).blocks is False  # undetermined
    assert rj.RepairFaithfulnessVerdict(ran=False).blocks is False


def test_jury_no_changed_files_does_not_run(monkeypatch, tmp_path):
    monkeypatch.setattr(rj, "_changed_lean_files", lambda repo, base: [])
    v = rj.run_repair_faithfulness_jury(tmp_path, "main")
    assert v.ran is False and v.blocks is False


def test_jury_flags_weakening_end_to_end(monkeypatch, tmp_path):
    (tmp_path / "M.lean").write_text("theorem t : True := trivial\n")  # after (weakened)
    monkeypatch.setattr(rj, "_changed_lean_files", lambda repo, base: ["M.lean"])
    monkeypatch.setattr(rj, "_before", lambda repo, base, rel: "theorem t : P ∧ Q := sorry\n")

    def fake_claude(prompt, **kw):
        return SimpleNamespace(
            returncode=0,
            stdout='{"verdict":"fail","weakened":[{"decl":"t","reason":"P∧Q → True"}]}',
        )

    v = rj.run_repair_faithfulness_jury(tmp_path, "main", _runner=fake_claude)
    assert v.ran and v.passed is False and v.blocks
    assert v.weakened[0]["decl"] == "t"


def test_jury_claude_failure_is_undetermined_not_block(monkeypatch, tmp_path):
    (tmp_path / "M.lean").write_text("after\n")
    monkeypatch.setattr(rj, "_changed_lean_files", lambda repo, base: ["M.lean"])
    monkeypatch.setattr(rj, "_before", lambda repo, base, rel: "before\n")

    def boom(prompt, **kw):
        raise RuntimeError("claude down")

    v = rj.run_repair_faithfulness_jury(tmp_path, "main", _runner=boom)
    assert v.ran and v.passed is None and v.blocks is False
