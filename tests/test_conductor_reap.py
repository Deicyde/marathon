"""The conductor reaps an orphan worktree holding an issue's refine branch
(left by a failed refine of a previous run) instead of wedging on it."""
from __future__ import annotations

from types import SimpleNamespace

import marathon.conductor as conductor


def test_reap_orphan_worktree_removes_the_branch_holder(monkeypatch, tmp_path):
    list_out = (
        "worktree /runs/wt-i96-old\n"
        "HEAD abc123\n"
        "branch refs/heads/marathon/refine-c12-i96\n"
        "\n"
        "worktree /repo\n"
        "HEAD def456\n"
        "branch refs/heads/main\n"
    )

    def fake_git(repo_dir, *args):
        if args[:2] == ("worktree", "list"):
            return SimpleNamespace(returncode=0, stdout=list_out, stderr="")
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    removed = []
    monkeypatch.setattr(conductor, "_git", fake_git)
    monkeypatch.setattr(conductor, "_remove_worktree",
                        lambda repo, wt: removed.append(str(wt)))

    assert conductor._reap_orphan_worktree(tmp_path, "marathon/refine-c12-i96") is True
    assert removed == ["/runs/wt-i96-old"]      # found + removed the branch's worktree


def test_reap_orphan_worktree_prunes_when_no_live_holder(monkeypatch, tmp_path):
    # branch not checked out anywhere → only stale metadata; prune, never remove
    monkeypatch.setattr(conductor, "_git", lambda repo, *a: SimpleNamespace(
        returncode=0, stdout="worktree /repo\nbranch refs/heads/main\n", stderr=""))

    def _boom(*a):
        raise AssertionError("must not remove a live/other worktree")

    monkeypatch.setattr(conductor, "_remove_worktree", _boom)
    assert conductor._reap_orphan_worktree(tmp_path, "marathon/refine-c12-i96") is True
