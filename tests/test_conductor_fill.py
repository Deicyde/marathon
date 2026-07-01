"""Conductor fill-dispatch: which verified-but-`sorry` issues get a fill this tick.
Excludes in-flight, backed-off, attempt-capped, and open-PR (pending-merge) issues."""
from __future__ import annotations

from types import SimpleNamespace

import marathon.conductor as cond
from marathon.review.fill_discovery import FillTarget


def _ft(issue, chapter=12):
    return FillTarget(issue=issue, chapter=chapter, decls=["d"])


def test_pick_fill_excludes_running_backoff_capped_and_open_pr():
    cfg = SimpleNamespace(github_repo="o/r")
    targets = {12: [_ft(101), _ft(102), _ft(103), _ft(104), _ft(105)]}
    picks = cond._pick_fill_dispatchable(
        cfg, {12},
        running_issues={101},               # 101 already running
        not_before={102: 2000.0},           # 102 backed off (future)
        fill_attempts={103: 3},             # 103 hit the attempt cap
        max_fill_attempts=3,
        _now_fn=lambda: 1000.0,
        _discover=lambda c, ch: targets.get(ch, []),
        _open_pr=lambda c, n, ch, **kw: n == 104,  # 104 has a fill PR pending merge
        _skeleton_done=lambda c, ch: True,
    )
    assert picks == [(105, 12)]


def test_pick_fill_skips_chapters_whose_skeleton_is_not_done():
    """A chapter is not proved until every registered statement is verified —
    even one still-churning issue blocks the whole chapter's fills."""
    cfg = SimpleNamespace(github_repo="o/r")
    targets = {11: [_ft(201, 11)], 12: [_ft(202, 12)]}
    picks = cond._pick_fill_dispatchable(
        cfg, {11, 12}, set(), {}, {}, 3,
        _discover=lambda c, ch: targets.get(ch, []),
        _open_pr=lambda c, n, ch, **kw: False,
        _skeleton_done=lambda c, ch: ch == 12,  # only 12 is skeleton-done
    )
    assert picks == [(202, 12)]  # chapter 11 skipped entirely


def test_pick_fill_empty_when_no_chapters_requested():
    cfg = SimpleNamespace(github_repo="o/r")
    assert cond._pick_fill_dispatchable(cfg, set(), set(), {}, {}, 3) == []


def test_pick_fill_spans_chapters_sorted_by_issue():
    cfg = SimpleNamespace(github_repo="o/r")
    targets = {11: [_ft(201, 11)], 12: [_ft(202, 12)]}
    picks = cond._pick_fill_dispatchable(
        cfg, {11, 12}, set(), {}, {}, 3,
        _discover=lambda c, ch: targets.get(ch, []),
        _open_pr=lambda c, n, ch, **kw: False,
        _skeleton_done=lambda c, ch: True,
    )
    assert picks == [(201, 11), (202, 12)]


def test_pick_fill_expired_backoff_is_dispatchable():
    cfg = SimpleNamespace(github_repo="o/r")
    picks = cond._pick_fill_dispatchable(
        cfg, {12}, set(),
        not_before={108: 500.0},  # backoff in the past → eligible again
        fill_attempts={}, max_fill_attempts=3,
        _now_fn=lambda: 1000.0,
        _discover=lambda c, ch: [_ft(108)],
        _open_pr=lambda c, n, ch, **kw: False,
        _skeleton_done=lambda c, ch: True,
    )
    assert picks == [(108, 12)]


# --- proofs land on the prover branch, never main ---------------------------------

from pathlib import Path  # noqa: E402

from marathon.post_pipeline import _branch_name_for_issue  # noqa: E402
from marathon.review.config import ReviewConfig, ReviewLabels  # noqa: E402


def test_proof_branch_name_distinct_from_refine():
    assert _branch_name_for_issue("Chapter14", 170, kind="proof") == \
        "marathon/proof-c14-i170"
    assert _branch_name_for_issue("Chapter14", 170) == "marathon/refine-c14-i170"


def _reviewcfg(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    return ReviewConfig(
        repo_dir=repo, config_path=repo / ".marathon/review/config.toml",
        github_repo="o/r", parent_issue=1,
        target_path_template="Demo/Chapter{chapter}",
        tracker_section_pattern="### Chapter {chapter}:",
        labels=ReviewLabels(), chapters={},
    )


def test_dispatch_fill_routes_to_prover_branch_never_main(tmp_path, monkeypatch):
    """A fill branches from origin/<prover>, uses a proof- branch, and targets
    its PR at the prover branch — proofs never touch main."""
    cfg = _reviewcfg(tmp_path)
    git_calls = []

    def fake_git(repo_dir, *args):
        git_calls.append(list(args))
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    monkeypatch.setattr(cond, "_git", fake_git)
    monkeypatch.setattr(cond, "_prime_mathlib_cache", lambda w: None)
    monkeypatch.setattr(cond, "_mathlib_already_built", lambda w: True)
    cmds = []

    class FakePopen:
        def __init__(self, cmd, **kw):
            self.cmd = list(cmd)
            self.pid = 123
            cmds.append(self.cmd)

    monkeypatch.setattr(cond.subprocess, "Popen", FakePopen)

    job = cond._dispatch_job(
        cfg, 170, 14, tmp_path / "runs", {},
        fill=True, prover_branch="marathon/proving",
    )
    assert job is not None and job.fill is True
    assert job.branch == "marathon/proof-c14-i170"

    add = [c for c in git_calls if c[:2] == ["worktree", "add"]][0]
    assert add[-1] == "origin/marathon/proving"   # branched from the prover branch
    assert "marathon/proof-c14-i170" in add
    assert "origin/main" not in add               # NEVER main

    cmd = cmds[0]
    i = cmd.index("--auto-pr-base")
    assert cmd[i + 1] == "marathon/proving"        # PR targets the prover branch
    assert "--gate" in cmd and cmd[cmd.index("--gate") + 1] == "enforce"
    assert "--issue" in cmd and "170" in cmd
