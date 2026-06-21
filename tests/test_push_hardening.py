"""B0 hardening of the conductor's job machinery (the #98 failure class):
- pushes use an explicit refspec/force so they survive an upstream mismatch or a
  stale remote-tracking ref;
- a refine that exits clean but opened no PR is requeued, not left stuck.
"""
from __future__ import annotations

import subprocess
from pathlib import Path
from types import SimpleNamespace

import marathon.conductor as cond
import marathon.post_pipeline as pp
import marathon.review.automerge as amod
import marathon.review.github as ghmod


def _ok(args, **kw):
    return subprocess.CompletedProcess(args, 0, "", "")


# --- B0a: push commands ------------------------------------------------------

def test_run_git_push_uses_explicit_origin_head(monkeypatch):
    calls = []
    monkeypatch.setattr(pp.subprocess, "run", lambda a, **k: calls.append(a) or _ok(a))
    ok, _ = pp.run_git_push(Path("/repo"))
    assert ok
    # explicit `origin HEAD` — never a bare `git push` (which hits push.default
    # "upstream does not match" when the worktree branch upstream is unset/wrong)
    assert calls[0] == ["git", "push", "origin", "HEAD"]


def test_open_or_update_pr_force_pushes_explicit_refspec(monkeypatch):
    calls = []
    monkeypatch.setattr(pp.subprocess, "run", lambda a, **k: calls.append(a) or _ok(a))
    monkeypatch.setattr(pp, "_existing_pr_number", lambda *a, **k: None)
    monkeypatch.setattr(pp, "_gh", lambda *a, **k: _ok(("gh",) + a))
    ok, _ = pp.open_or_update_pr(
        repo_dir=Path("/repo"), branch="marathon/refine-c12-i98",
        base="main", repo="o/r", title="t", body="b",
    )
    # explicit HEAD:refs/heads/<branch> + plain --force: robust to a stale
    # remote-tracking ref (which made --force-with-lease reject as "stale info")
    assert ["git", "push", "--force", "origin",
            "HEAD:refs/heads/marathon/refine-c12-i98"] in calls


# --- B0c: requeue a refine that landed no PR ---------------------------------

def _job(issue=98, branch="marathon/refine-c12-i98"):
    return SimpleNamespace(issue_num=issue, branch=branch)


def _patch_gh(monkeypatch, prs_json):
    monkeypatch.setattr(ghmod, "gh",
                        lambda *a, **k: SimpleNamespace(returncode=0, stdout=prs_json))


def _capture_requeue(monkeypatch):
    requeued = []
    monkeypatch.setattr(amod, "_requeue_refined_issue",
                        lambda cfg, branch: requeued.append(branch) or
                        int(branch.rsplit("-i", 1)[1]))
    return requeued


def test_no_pr_and_still_rejected_requeues(monkeypatch):
    cfg = SimpleNamespace(github_repo="o/r")
    _patch_gh(monkeypatch, "[]")  # branch has no PR
    monkeypatch.setattr(cond, "load_state",
                        lambda c: SimpleNamespace(issues={98: SimpleNamespace(status="rejected")}))
    requeued = _capture_requeue(monkeypatch)
    cond._requeue_if_no_pr_landed(cfg, _job())
    assert requeued == ["marathon/refine-c12-i98"]


def test_pr_exists_does_not_requeue(monkeypatch):
    cfg = SimpleNamespace(github_repo="o/r")
    _patch_gh(monkeypatch, '[{"number": 116}]')  # a PR exists → leave to merge→requeue
    requeued = _capture_requeue(monkeypatch)
    cond._requeue_if_no_pr_landed(cfg, _job())
    assert requeued == []


def test_non_refine_branch_is_noop(monkeypatch):
    cfg = SimpleNamespace(github_repo="o/r")
    requeued = _capture_requeue(monkeypatch)
    cond._requeue_if_no_pr_landed(cfg, _job(branch="feature/unrelated"))
    assert requeued == []


def test_no_pr_but_not_rejected_does_not_requeue(monkeypatch):
    cfg = SimpleNamespace(github_repo="o/r")
    _patch_gh(monkeypatch, "[]")
    monkeypatch.setattr(cond, "load_state",
                        lambda c: SimpleNamespace(issues={98: SimpleNamespace(status="verified")}))
    requeued = _capture_requeue(monkeypatch)
    cond._requeue_if_no_pr_landed(cfg, _job())
    assert requeued == []
