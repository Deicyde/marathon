"""B0 hardening of the conductor's job machinery (the #98 failure class):
- pushes use an explicit refspec/force so they survive an upstream mismatch or a
  stale remote-tracking ref;
- a refine that exits clean but opened no PR is requeued, not left stuck.
"""
from __future__ import annotations

import asyncio
import subprocess
from pathlib import Path
from types import SimpleNamespace

import marathon.conductor as cond
import marathon.post_pipeline as pp
import marathon.review.automerge as amod
import marathon.review.github as ghmod
import marathon.refine as refine
from marathon.review.state import (
    load_state,
    record_iteration,
    record_rejection,
    requeue_for_rereview,
)
from marathon.state import RefineState, load_refine_state, save_refine_state


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
                        lambda cfg, branch, *a, **k: requeued.append((branch, k))
                        or int(branch.rsplit("-i", 1)[1]))
    return requeued


def test_no_pr_and_still_rejected_requeues(monkeypatch):
    cfg = SimpleNamespace(github_repo="o/r")
    _patch_gh(monkeypatch, "[]")  # branch has no PR
    monkeypatch.setattr(cond, "load_state",
                        lambda c: SimpleNamespace(issues={98: SimpleNamespace(status="rejected")}))
    requeued = _capture_requeue(monkeypatch)
    cond._requeue_if_no_pr_landed(cfg, _job())
    # re-queued, and CRUCIALLY landed=False so no false "fix landed" comment
    assert requeued == [("marathon/refine-c12-i98", {"landed": False})]


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


# --- committed publication recovery ----------------------------------------

def test_refine_pending_publication_retries_only_network_step(
    monkeypatch, tmp_path,
):
    """A kept clean commit is published without branch prep or Aristotle."""
    repo = tmp_path / "repo"
    target = repo / "Demo" / "Chapter14"
    target.mkdir(parents=True)
    (repo / ".git").mkdir()
    workdir = tmp_path / "wd"
    workdir.mkdir()
    state_path = workdir / refine.REFINE_STATE_FILENAME
    saved = RefineState(
        target_folder=str(target),
        current_iteration_idx=1,
        project_id="aristotle-project-already-finished",
        review_verdict_ts="2026-07-10T10:00:00.000001+00:00",
        pending_publication={
            "kind": "pr",
            "commit_sha": "abc12345",
            "branch": "marathon/refine-c14-i22",
            "base": "main",
            "repo": "o/r",
            "title": "t",
            "body": "b",
        },
    )
    save_refine_state(state_path, saved)

    published = []
    monkeypatch.setattr(
        pp,
        "retry_pending_publication",
        lambda repo_dir, pending: published.append((repo_dir, pending))
        or (True, "https://github.com/o/r/pull/1"),
    )
    monkeypatch.setattr(
        refine,
        "_ensure_api_key",
        lambda: (_ for _ in ()).throw(AssertionError("must not touch Aristotle")),
    )
    monkeypatch.setattr(
        pp,
        "prepare_auto_pr_branch",
        lambda **kw: (_ for _ in ()).throw(AssertionError("must not reset branch")),
    )
    args = SimpleNamespace(
        target=target,
        repo_dir=repo,
        workdir=workdir,
        tex=None,
        review_verdict_ts="a-newer-verdict-must-not-be-adopted",
    )

    asyncio.run(refine.refine_command(args))

    assert len(published) == 1
    after = load_refine_state(state_path)
    assert after is not None and after.pending_publication is None
    assert after.iterations_completed == 1
    assert after.review_verdict_ts == saved.review_verdict_ts


def test_conductor_publication_exit_keeps_commit_without_burning_attempt(
    monkeypatch, tmp_path,
):
    from tests.test_conductor import make_cfg

    cfg = make_cfg(tmp_path)
    verdict = record_rejection(cfg, 22, "fix")
    worktree = tmp_path / "runs" / "wt-i22-x"
    workdir = tmp_path / "runs" / "wd-i22-x"
    worktree.mkdir(parents=True)
    workdir.mkdir(parents=True)
    save_refine_state(
        workdir / refine.REFINE_STATE_FILENAME,
        RefineState(
            target_folder=str(worktree / "Demo" / "Chapter14"),
            review_verdict_ts=verdict.verdict_ts,
            pending_publication={"kind": "pr", "commit_sha": "abc12345"},
        ),
    )
    job = cond.ConductorJob(
        issue_num=22,
        chapter=14,
        target="Demo/Chapter14",
        worktree=str(worktree),
        workdir=str(workdir),
        branch="marathon/refine-c14-i22",
        status="running",
        expected_verdict_ts=verdict.verdict_ts,
        proc=SimpleNamespace(poll=lambda: refine.EXIT_PUBLICATION_PENDING),
    )
    kept: dict[int, str] = {}
    not_before: dict[int, float] = {}
    monkeypatch.setattr(cond, "_job_log_tail", lambda job: "push failed")

    cond._reap_finished(cfg, [job], not_before, kept, max_attempts=3)

    current = load_state(cfg).issues[22]
    assert current.attempts == 0 and current.needs_iteration()
    assert job.status == "publication_pending"
    assert kept == {22: str(worktree)}
    assert 22 in not_before


# --- durable rc=0 / GitHub uncertainty -------------------------------------

def test_transient_pr_check_failure_recovers_on_later_tick(monkeypatch, tmp_path):
    from tests.test_conductor import make_cfg

    cfg = make_cfg(tmp_path)
    verdict = record_rejection(cfg, 22, "fix")
    branch = "marathon/refine-c14-i22"
    record_iteration(
        cfg,
        22,
        expected_verdict_ts=verdict.verdict_ts,
        pending_pr_branch=branch,
    )
    answers = iter([None, False])
    monkeypatch.setattr(cond, "_branch_pr_exists", lambda cfg, branch: next(answers))

    def requeue(cfg_, branch_, **kwargs):
        assert kwargs["expected_verdict_ts"] == verdict.verdict_ts
        assert requeue_for_rereview(
            cfg_, 22, expected_verdict_ts=kwargs["expected_verdict_ts"]
        )
        return 22

    monkeypatch.setattr(amod, "_requeue_refined_issue", requeue)

    assert cond._retry_pending_pr_checks(cfg) == 0
    parked = load_state(cfg).issues[22]
    assert parked.status == "rejected"
    assert not parked.needs_iteration() and parked.pending_pr_branch == branch

    # Simulates a later conductor tick (or a restarted conductor): no refine
    # dispatch is involved, only the durable GitHub check.
    assert cond._retry_pending_pr_checks(cfg) == 1
    assert 22 not in load_state(cfg).issues


def test_conductor_hands_verbatim_reject_note_to_worktree(monkeypatch, tmp_path):
    from tests.test_conductor import make_cfg

    cfg = make_cfg(tmp_path)
    note = "Fix `Demo.foo` first.\n\nDo not rename the downstream theorem."
    verdict = record_rejection(cfg, 22, note)
    worktree = tmp_path / "kept" / "wt-i22-x"
    worktree.mkdir(parents=True)
    spawned = []
    monkeypatch.setattr(cond, "_prime_mathlib_cache", lambda *a, **k: True)
    monkeypatch.setattr(cond, "_mathlib_already_built", lambda *a, **k: True)
    monkeypatch.setattr(
        cond.subprocess,
        "Popen",
        lambda cmd, **kw: spawned.append(cmd) or SimpleNamespace(pid=4001),
    )

    job = cond._dispatch_job(
        cfg, 22, 14, tmp_path / "runs", {22: str(worktree)}
    )

    assert job is not None
    assert job.rejection_note == note
    [cmd] = spawned
    note_path = Path(cmd[cmd.index("--review-rejection-note-file") + 1])
    payload = __import__("json").loads(note_path.read_text())
    assert payload == {
        "issue": 22,
        "verdict_ts": verdict.verdict_ts,
        "notes": note,
    }


def test_focused_refine_without_note_fails_before_aristotle(monkeypatch, tmp_path):
    repo = tmp_path / "repo"
    target = repo / "Demo" / "Chapter14"
    target.mkdir(parents=True)
    (repo / ".git").mkdir()
    workdir = tmp_path / "wd"
    workdir.mkdir()
    monkeypatch.setattr(refine, "_ensure_api_key", lambda: "sk-test")
    monkeypatch.setattr(refine, "_load_pending_rejections_md", lambda *a, **k: None)
    monkeypatch.setattr(
        refine,
        "_run_iteration",
        lambda **kw: (_ for _ in ()).throw(AssertionError("must not run prover")),
    )
    args = SimpleNamespace(
        target=target,
        repo_dir=repo,
        workdir=workdir,
        tex=None,
        max_iterations=1,
        max_retries=1,
        skeleton=True,
        max_prompt_words=None,
        no_cross_chapter=True,
        review_rejection=22,
        review_verdict_ts="2026-07-10T10:00:00+00:00",
        review_rejection_note_file=None,
        auto_build=False,
        auto_commit=False,
        auto_push=False,
        auto_rate=False,
        build_timeout=60,
        auto_pr=False,
        auto_pr_repo=None,
        auto_pr_base="main",
        gate="warn",
        gate_override=None,
        jury=False,
        dry_run=False,
        live_steering=False,
    )
    try:
        asyncio.run(refine.refine_command(args))
    except SystemExit as exc:
        assert exc.code == refine.EXIT_REJECTION_CONTEXT_MISSING
    else:
        raise AssertionError("focused missing-note run must fail closed")
