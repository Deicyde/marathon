"""Conductor dispatch integration tests for the recovery ladder.

These tests stop at the detached-worker boundary.  Git, GitHub, Lake, and the
worker command itself are faked; the assertions cover the worktree and command
contract handed to that worker.
"""
from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

import marathon.conductor as conductor
import marathon.review.daemon as daemon
from marathon.recovery import (
    UPDATE_RECOVERY_MARKER_FILENAME,
    UPDATE_RECOVERY_SCHEMA_VERSION,
)
from marathon.review import state as review_state
from marathon.state import RefineState, load_refine_state, save_refine_state

from tests.test_conductor import make_cfg


ISSUE = 31
CHAPTER = 15
BRANCH = "marathon/refine-c15-i31"
HEAD = "a" * 40
BASE = "main"
RECOVERY_ID = "recovery-operation-31"


def _cp(returncode=0, stdout="", stderr=""):
    return SimpleNamespace(
        returncode=returncode, stdout=stdout, stderr=stderr
    )


class DispatchHarness:
    def __init__(self, branch=BRANCH, head=HEAD):
        self.branch = branch
        self.head = head
        self.local_branch_head = head
        self.worktree_head = "b" * 40
        self.git_calls: list[tuple[Path, tuple[str, ...]]] = []
        self.events: list[tuple[str, object]] = []
        self.fetches: list[tuple[str, str]] = []
        self.spawned = []
        self.registered: Path | None = None

    def git(self, repo_dir, *args):
        repo = Path(repo_dir)
        self.git_calls.append((repo, tuple(args)))
        if args[:2] == ("worktree", "add"):
            worktree = Path(args[-2])
            worktree.mkdir(parents=True, exist_ok=True)
            self.events.append(("worktree_add", tuple(args)))
            return _cp()
        if args == ("rev-parse", "--verify", "--quiet", "MERGE_HEAD"):
            return _cp(returncode=1)
        if args[:3] == ("rev-parse", "--verify", "--quiet"):
            if self.local_branch_head is None:
                return _cp(returncode=1)
            return _cp(stdout=f"{self.local_branch_head}\n")
        if args[:2] == ("merge-base", "--is-ancestor"):
            return _cp()
        if args[:2] == ("rev-parse", "HEAD"):
            return _cp(stdout=f"{self.worktree_head}\n")
        if args[:2] == ("branch", "--show-current"):
            return _cp(stdout=f"{self.branch}\n")
        return _cp()

    def fetch_exact(self, repo_dir, branch, expected_head):
        self.fetches.append((branch, expected_head))
        return True

    def popen(self, cmd, **kwargs):
        split = list(cmd).index("--")
        proc = SimpleNamespace(
            pid=4310 + len(self.spawned),
            launch_cmd=list(cmd),
            cmd=list(cmd)[split + 1 :],
            kwargs=kwargs,
        )
        self.spawned.append(proc)
        return proc


@pytest.fixture
def dispatch_harness(monkeypatch):
    harness = DispatchHarness()
    monkeypatch.setattr(conductor, "_git", harness.git)
    monkeypatch.setattr(
        conductor, "_fetch_exact_recovery_head", harness.fetch_exact
    )
    monkeypatch.setattr(conductor, "_prime_mathlib_cache", lambda *_: True)
    monkeypatch.setattr(conductor, "_mathlib_already_built", lambda *_: True)
    monkeypatch.setattr(
        conductor, "_registered_worktree_for_branch", lambda *_: harness.registered
    )
    monkeypatch.setattr(conductor, "_worktree_live_pid", lambda *_: None)
    monkeypatch.setattr(
        conductor, "_write_worktree_pid_marker", lambda *a, **k: True
    )
    monkeypatch.setattr(conductor.subprocess, "Popen", harness.popen)
    monkeypatch.setattr(
        conductor,
        "_await_worker_handshake",
        lambda worktree, proc: {"pid": proc.pid, "pgid": proc.pid},
    )
    return harness


def _queue_recovery(cfg, mode):
    rejected = review_state.record_rejection(cfg, ISSUE, "preserve the old fix")
    review_state.record_iteration(
        cfg, ISSUE, expected_verdict_ts=rejected.verdict_ts
    )
    queued = review_state.request_recovery_if_current(
        cfg,
        ISSUE,
        expected_status="rejected",
        expected_verdict_ts=rejected.verdict_ts,
        recovery_id=RECOVERY_ID,
        recovery_mode=mode,
        recovery_pr=277,
        recovery_branch=BRANCH,
        recovery_head_sha=HEAD,
        recovery_base=BASE,
    )
    assert queued is not None
    return queued


def _queue_no_pr_start_over(cfg):
    rejected = review_state.record_rejection(
        cfg, ISSUE, "confirmed destructive restart with no PR"
    )
    review_state.record_iteration(
        cfg, ISSUE, expected_verdict_ts=rejected.verdict_ts
    )
    queued = review_state.request_recovery_if_current(
        cfg,
        ISSUE,
        expected_status="rejected",
        expected_verdict_ts=rejected.verdict_ts,
        recovery_id=RECOVERY_ID,
        recovery_mode=review_state.RECOVERY_START_OVER,
    )
    assert queued is not None
    return queued


def _dispatch(cfg, runs, kept):
    job = conductor._dispatch_job(cfg, ISSUE, CHAPTER, runs, kept)
    assert job is not None
    assert job.log_handle is not None
    job.log_handle.close()
    job.log_handle = None
    return job


def _arg(command, flag):
    return command[command.index(flag) + 1]


def test_retry_build_uses_detached_exact_head_without_aristotle(
    tmp_path, dispatch_harness
):
    cfg = make_cfg(tmp_path)
    _queue_recovery(cfg, review_state.RECOVERY_RETRY_BUILD)
    runs = tmp_path / "runs"
    old_worktree = runs / "wt-i31-dirty-old"
    old_worktree.mkdir(parents=True)
    (old_worktree / "unpublished.lean").write_text("-- dirty work\n")
    kept = {ISSUE: str(old_worktree)}

    job = _dispatch(cfg, runs, kept)

    assert job.worktree != str(old_worktree)
    assert (old_worktree / "unpublished.lean").is_file()
    assert dispatch_harness.fetches == [(BRANCH, HEAD)]
    add = next(
        args for _repo, args in dispatch_harness.git_calls
        if args[:2] == ("worktree", "add")
    )
    assert add[:3] == ("worktree", "add", "--detach")
    assert add[-1] == f"origin/{BRANCH}"
    child = dispatch_harness.spawned[0].cmd
    assert child[:4] == [
        sys.executable, "-m", "marathon.recovery", "retry-build"
    ]
    assert _arg(child, "--expected-head") == HEAD
    assert _arg(child, "--expected-branch") == BRANCH
    assert "refine" not in child


@pytest.mark.parametrize("source", ["kept", "registered"])
def test_continue_pr_reuses_preserved_tree_and_checkpoint_with_exact_lease(
    tmp_path, dispatch_harness, source
):
    cfg = make_cfg(tmp_path)
    queued = _queue_recovery(cfg, review_state.RECOVERY_CONTINUE_PR)
    runs = tmp_path / "runs"
    worktree = runs / "wt-i31-existing"
    workdir = runs / "wd-i31-existing"
    target = worktree / "Demo/Chapter15"
    target.mkdir(parents=True)
    workdir.mkdir(parents=True)
    save_refine_state(
        workdir / conductor.REFINE_STATE_FILENAME,
        RefineState(target_folder=str(target), project_id="project-existing"),
    )
    kept = {ISSUE: str(worktree)} if source == "kept" else {}
    if source == "registered":
        dispatch_harness.registered = worktree

    job = _dispatch(cfg, runs, kept)

    assert job.worktree == str(worktree)
    checkpoint = load_refine_state(workdir / conductor.REFINE_STATE_FILENAME)
    assert checkpoint.project_id == "project-existing"
    assert checkpoint.force_continuation is True
    assert checkpoint.preserve_local_branch is True
    assert checkpoint.continuation_commit_sha == dispatch_harness.worktree_head
    assert checkpoint.review_verdict_ts == queued.verdict_ts
    child = dispatch_harness.spawned[0].cmd
    assert child[:4] == [sys.executable, "-m", "marathon", "refine"]
    assert _arg(child, "--auto-pr-expected-remote-oid") == HEAD
    assert "--auto-pr-continue-remote" in child


def test_continue_pr_never_reuses_divergent_registered_worktree(
    tmp_path, dispatch_harness, monkeypatch
):
    cfg = make_cfg(tmp_path)
    _queue_recovery(cfg, review_state.RECOVERY_CONTINUE_PR)
    runs = tmp_path / "runs"
    worktree = runs / "wt-i31-divergent"
    workdir = runs / "wd-i31-divergent"
    (worktree / "Demo/Chapter15").mkdir(parents=True)
    workdir.mkdir(parents=True)
    dispatch_harness.registered = worktree
    dispatch_harness.local_branch_head = "b" * 40
    dispatch_harness.worktree_head = "b" * 40
    monkeypatch.setattr(conductor, "_local_branch_extends", lambda *a: False)
    monkeypatch.setattr(
        conductor, "_preserve_local_branch_ref", lambda *a: True
    )

    job = conductor._dispatch_job(cfg, ISSUE, CHAPTER, runs, {})
    if job is not None and job.log_handle is not None:
        job.log_handle.close()

    assert job is None
    assert dispatch_harness.spawned == []


def test_update_continue_wraps_fresh_refine_command(tmp_path, dispatch_harness):
    cfg = make_cfg(tmp_path)
    _queue_recovery(cfg, review_state.RECOVERY_UPDATE_CONTINUE)
    runs = tmp_path / "runs"

    job = _dispatch(cfg, runs, {})

    child = dispatch_harness.spawned[0].cmd
    assert child[:4] == [
        sys.executable, "-m", "marathon.recovery", "update-continue"
    ]
    assert _arg(child, "--expected-head") == HEAD
    separator = child.index("--")
    fallback = child[separator + 1 :]
    assert fallback[:4] == [sys.executable, "-m", "marathon", "refine"]
    assert _arg(fallback, "--auto-pr-expected-remote-oid") == HEAD
    assert job.recovery_mode == review_state.RECOVERY_UPDATE_CONTINUE


@pytest.mark.parametrize(
    "checkpoint_kind",
    ["dirty_at_pr_head", "diverged_with_project"],
)
def test_update_continue_adopts_exact_branch_checkpoint_without_work_loss(
    tmp_path, dispatch_harness, checkpoint_kind
):
    """BEHIND/DIRTY PR recovery reuses, rather than resets, local work."""
    cfg = make_cfg(tmp_path)
    _queue_recovery(cfg, review_state.RECOVERY_UPDATE_CONTINUE)
    runs = tmp_path / "runs"
    worktree = runs / "wt-i31-checkpoint"
    workdir = runs / "wd-i31-checkpoint"
    target = worktree / "Demo/Chapter15"
    target.mkdir(parents=True)
    workdir.mkdir(parents=True)
    irreplaceable = target / "Unpublished.lean"
    irreplaceable.write_text("-- preserved local formalization work\n")
    save_refine_state(
        workdir / conductor.REFINE_STATE_FILENAME,
        RefineState(
            target_folder=str(target),
            project_id="project-existing",
            agent_task_id="task-existing",
        ),
    )
    dispatch_harness.registered = worktree
    if checkpoint_kind == "diverged_with_project":
        dispatch_harness.local_branch_head = "b" * 40
        dispatch_harness.worktree_head = "b" * 40

    job = _dispatch(cfg, runs, {})

    assert job.worktree == str(worktree)
    assert job.workdir == str(workdir)
    assert irreplaceable.read_text() == "-- preserved local formalization work\n"
    checkpoint = load_refine_state(
        workdir / conductor.REFINE_STATE_FILENAME
    )
    assert checkpoint is not None
    assert checkpoint.project_id == "project-existing"
    assert checkpoint.agent_task_id == "task-existing"
    child = dispatch_harness.spawned[0].cmd
    assert child[:4] == [
        sys.executable, "-m", "marathon.recovery", "update-continue"
    ]
    assert _arg(child, "--expected-head") == HEAD
    assert _arg(child, "--operation-id") == RECOVERY_ID
    assert dispatch_harness.fetches == [(BRANCH, HEAD)]
    assert not any(
        args[:2] == ("worktree", "add")
        for _repo, args in dispatch_harness.git_calls
    )


def test_update_continue_adopts_dirty_tree_if_old_workdir_is_missing(
    tmp_path, dispatch_harness
):
    cfg = make_cfg(tmp_path)
    _queue_recovery(cfg, review_state.RECOVERY_UPDATE_CONTINUE)
    runs = tmp_path / "runs"
    worktree = runs / "wt-i31-missing-workdir"
    target = worktree / "Demo/Chapter15"
    target.mkdir(parents=True)
    local_file = target / "OnlyLocal.lean"
    local_file.write_text("-- checkpoint with no surviving session file\n")
    dispatch_harness.registered = worktree

    job = _dispatch(cfg, runs, {})

    expected_workdir = runs / "wd-i31-missing-workdir"
    assert job.worktree == str(worktree)
    assert job.workdir == str(expected_workdir)
    assert expected_workdir.is_dir()
    assert local_file.read_text() == "-- checkpoint with no surviving session file\n"
    assert dispatch_harness.spawned[0].cmd[:4] == [
        sys.executable, "-m", "marathon.recovery", "update-continue"
    ]
    assert not any(
        args[:2] == ("worktree", "add")
        for _repo, args in dispatch_harness.git_calls
    )


def test_update_continue_marker_retry_runs_prepared_plain_refine(
    tmp_path, dispatch_harness
):
    cfg = make_cfg(tmp_path)
    _queue_recovery(cfg, review_state.RECOVERY_UPDATE_CONTINUE)
    runs = tmp_path / "runs"
    worktree = runs / "wt-i31-prepared"
    workdir = runs / "wd-i31-prepared"
    target = worktree / "Demo/Chapter15"
    target.mkdir(parents=True)
    workdir.mkdir(parents=True)
    (workdir / UPDATE_RECOVERY_MARKER_FILENAME).write_text(
        json.dumps({
            "schema_version": UPDATE_RECOVERY_SCHEMA_VERSION,
            "phase": "fallback_prepared",
            "operation_id": RECOVERY_ID,
            "github_repo": cfg.github_repo,
            "pr": 277,
            "branch": BRANCH,
            "original_expected_head": HEAD,
            "base_branch": BASE,
            "base_sha": "c" * 40,
            "current_local_head": "b" * 40,
            "target_path": str(target.resolve()),
        })
        + "\n"
    )

    job = _dispatch(cfg, runs, {ISSUE: str(worktree)})

    assert job.worktree == str(worktree)
    child = dispatch_harness.spawned[0].cmd
    assert child[:4] == [sys.executable, "-m", "marathon", "refine"]
    assert "marathon.recovery" not in child
    assert _arg(child, "--auto-pr-expected-remote-oid") == HEAD


@pytest.mark.parametrize(
    "mode",
    [
        review_state.RECOVERY_CONTINUE_PR,
        review_state.RECOVERY_UPDATE_CONTINUE,
        review_state.RECOVERY_START_OVER,
    ],
)
def test_exact_publication_wal_bypasses_stale_original_head_guards(
    tmp_path, dispatch_harness, monkeypatch, mode
):
    """A kill after push relaunches only idempotent publication.

    The remote may now differ from HEAD, so neither fetch/archive nor the
    update wrapper may run.  The exact operation WAL + kept checkout is the
    authority for this one narrow restart path.
    """
    cfg = make_cfg(tmp_path)
    queued = _queue_recovery(cfg, mode)
    runs = tmp_path / "runs"
    worktree = runs / f"wt-i{ISSUE}-publication"
    workdir = runs / f"wd-i{ISSUE}-publication"
    target = worktree / f"Demo/Chapter{CHAPTER}"
    target.mkdir(parents=True)
    workdir.mkdir(parents=True)
    commit_sha = "b" * 40
    dispatch_harness.branch = BRANCH
    dispatch_harness.worktree_head = commit_sha
    save_refine_state(
        workdir / conductor.REFINE_STATE_FILENAME,
        RefineState(
            target_folder=str(target),
            review_verdict_ts=queued.verdict_ts,
            current_iteration_idx=1,
            pending_publication={
                "schema_version": conductor.PUBLICATION_JOURNAL_SCHEMA_VERSION,
                "operation_id": RECOVERY_ID,
                "kind": "pr",
                "commit_sha": commit_sha,
                "branch": BRANCH,
                "base": BASE,
                "repo": cfg.github_repo,
                "title": "recovered",
                "body": "recovered body",
                "expected_remote_oid": HEAD,
            },
        ),
    )
    monkeypatch.setattr(
        conductor,
        "_archive_start_over_head",
        lambda *a, **k: (_ for _ in ()).throw(
            AssertionError("must not archive after this operation published")
        ),
    )

    job = _dispatch(cfg, runs, {ISSUE: str(worktree)})

    assert job.worktree == str(worktree)
    assert dispatch_harness.fetches == []
    child = dispatch_harness.spawned[0].cmd
    assert child[:4] == [sys.executable, "-m", "marathon", "refine"]
    assert "marathon.recovery" not in child
    assert _arg(child, "--recovery-operation-id") == RECOVERY_ID


def test_no_pr_start_over_publication_wal_skips_new_pr_rejection(
    tmp_path, dispatch_harness, monkeypatch
):
    """A PR created just before death is own progress, not foreign work."""
    from marathon import post_pipeline

    cfg = make_cfg(tmp_path)
    queued = _queue_no_pr_start_over(cfg)
    branch = post_pipeline._branch_name_for_issue(
        f"Chapter{CHAPTER}",
        ISSUE,
        kind="refine",
        generation=f"start-over:{RECOVERY_ID}:0",
    )
    runs = tmp_path / "runs"
    worktree = runs / f"wt-i{ISSUE}-publication"
    workdir = runs / f"wd-i{ISSUE}-publication"
    target = worktree / f"Demo/Chapter{CHAPTER}"
    target.mkdir(parents=True)
    workdir.mkdir(parents=True)
    commit_sha = "b" * 40
    dispatch_harness.branch = branch
    dispatch_harness.worktree_head = commit_sha
    save_refine_state(
        workdir / conductor.REFINE_STATE_FILENAME,
        RefineState(
            target_folder=str(target),
            review_verdict_ts=queued.verdict_ts,
            current_iteration_idx=1,
            pending_publication={
                "schema_version": conductor.PUBLICATION_JOURNAL_SCHEMA_VERSION,
                "operation_id": RECOVERY_ID,
                "kind": "pr",
                "commit_sha": commit_sha,
                "branch": branch,
                "base": "main",
                "repo": cfg.github_repo,
                "title": "recovered",
                "body": "recovered body",
                "expected_remote_oid": "",
            },
        ),
    )
    monkeypatch.setattr(
        conductor,
        "_open_refine_prs_for_issue",
        lambda *a, **k: (_ for _ in ()).throw(
            AssertionError("exact WAL must bypass the no-PR precondition")
        ),
    )

    job = _dispatch(cfg, runs, {ISSUE: str(worktree)})

    assert job.branch == branch
    child = dispatch_harness.spawned[0].cmd
    assert child[:4] == [sys.executable, "-m", "marathon", "refine"]
    assert _arg(child, "--auto-pr-expected-remote-oid") == ""
    assert _arg(child, "--recovery-operation-id") == RECOVERY_ID


def test_remote_only_start_over_reconstructs_exact_wal_checkout(
    tmp_path, dispatch_harness, monkeypatch
):
    """Push-before-death survives even if only its paired workdir remains."""
    from marathon import post_pipeline

    cfg = make_cfg(tmp_path)
    queued = _queue_no_pr_start_over(cfg)
    branch = post_pipeline._branch_name_for_issue(
        f"Chapter{CHAPTER}", ISSUE, kind="refine",
        generation=f"start-over:{RECOVERY_ID}:0",
    )
    runs = tmp_path / "runs"
    worktree = runs / f"wt-i{ISSUE}-remote-only"
    workdir = runs / f"wd-i{ISSUE}-remote-only"
    target = worktree / f"Demo/Chapter{CHAPTER}"
    workdir.mkdir(parents=True)
    commit_sha = "c" * 40
    save_refine_state(
        workdir / conductor.REFINE_STATE_FILENAME,
        RefineState(
            target_folder=str(target),
            review_verdict_ts=queued.verdict_ts,
            current_iteration_idx=1,
            pending_publication={
                "schema_version": conductor.PUBLICATION_JOURNAL_SCHEMA_VERSION,
                "operation_id": RECOVERY_ID,
                "kind": "pr",
                "commit_sha": commit_sha,
                "branch": branch,
                "base": "main",
                "repo": cfg.github_repo,
                "title": "recover push-only death",
                "body": "body",
                "expected_remote_oid": "",
            },
        ),
    )
    assert conductor._write_start_over_recovery_marker(
        workdir,
        operation_id=RECOVERY_ID,
        github_repo=cfg.github_repo,
        issue_num=ISSUE,
        branch=branch,
        target_path=target,
        recovery_pr=None,
        original_expected_head=None,
        base_branch=None,
    )
    old_job = conductor.ConductorJob(
        issue_num=ISSUE,
        chapter=CHAPTER,
        target=f"Demo/Chapter{CHAPTER}",
        worktree=str(worktree),
        workdir=str(workdir),
        branch=branch,
        status="publication_pending",
        expected_verdict_ts=queued.verdict_ts,
        expected_recovery_id=RECOVERY_ID,
        recovery_mode=review_state.RECOVERY_START_OVER,
    )
    conductor.write_jobs_snapshot(cfg.repo_dir, [old_job], 1)
    dispatch_harness.branch = branch
    dispatch_harness.worktree_head = commit_sha
    original_git = dispatch_harness.git

    def exact_remote_git(repo_dir, *args):
        if args[:2] == ("rev-parse", f"origin/{branch}"):
            return _cp(stdout=f"{commit_sha}\n")
        if (
            args[:3] == ("rev-parse", "--verify", "--quiet")
            and str(args[3]).startswith("refs/heads/marathon/local-wip-")
        ):
            return _cp(returncode=1)
        return original_git(repo_dir, *args)

    monkeypatch.setattr(conductor, "_git", exact_remote_git)
    monkeypatch.setattr(
        post_pipeline,
        "observe_remote_branch",
        lambda repo, selected: (
            (True, commit_sha, "exact WAL commit")
            if selected == branch
            else (True, None, "absent")
        ),
    )
    monkeypatch.setattr(
        conductor,
        "_open_refine_prs_for_issue",
        lambda *a, **k: (_ for _ in ()).throw(
            AssertionError("exact WAL recovery precedes the no-PR check")
        ),
    )

    job = _dispatch(cfg, runs, {})

    assert job.worktree == str(worktree)
    assert worktree.is_dir()
    adds = [
        args for _repo, args in dispatch_harness.git_calls
        if args[:2] == ("worktree", "add")
    ]
    assert adds == [
        ("worktree", "add", "-B", branch, str(worktree), commit_sha)
    ]
    archives = [
        args for _repo, args in dispatch_harness.git_calls
        if args[:1] == ("branch",)
        and len(args) == 3
        and args[1].startswith(f"marathon/local-wip-i{ISSUE}-")
    ]
    assert archives == [("branch", archives[0][1], HEAD)]
    child = dispatch_harness.spawned[0].cmd
    assert child[:4] == [sys.executable, "-m", "marathon", "refine"]
    assert _arg(child, "--recovery-operation-id") == RECOVERY_ID


def test_occupied_start_over_operation_branch_without_wal_defers(
    tmp_path, dispatch_harness, monkeypatch
):
    from marathon import post_pipeline

    cfg = make_cfg(tmp_path)
    _queue_no_pr_start_over(cfg)
    branch = post_pipeline._branch_name_for_issue(
        f"Chapter{CHAPTER}", ISSUE, kind="refine",
        generation=f"start-over:{RECOVERY_ID}:0",
    )
    observed: list[str] = []
    dispatch_harness.local_branch_head = None
    monkeypatch.setattr(
        conductor, "_open_refine_prs_for_issue", lambda *a, **k: []
    )

    def observe(repo, selected):
        observed.append(selected)
        if selected == branch:
            return True, "d" * 40, "occupied"
        return True, None, "absent"

    monkeypatch.setattr(post_pipeline, "observe_remote_branch", observe)

    job = conductor._dispatch_job(cfg, ISSUE, CHAPTER, tmp_path / "runs", {})

    assert job is None
    assert dispatch_harness.spawned == []
    assert branch in observed
    assert not any("start-over:" in selected and selected != branch for selected in observed)
    assert review_state.load_state(cfg).issues[ISSUE].status == "stalled"


@pytest.mark.parametrize("phase", ["starting", "merged", "pushing", "published"])
def test_update_journal_nonfallback_phases_resume_wrapper_in_same_tree(
    tmp_path, dispatch_harness, phase
):
    cfg = make_cfg(tmp_path)
    _queue_recovery(cfg, review_state.RECOVERY_UPDATE_CONTINUE)
    runs = tmp_path / "runs"
    worktree = runs / f"wt-i31-{phase}"
    workdir = runs / f"wd-i31-{phase}"
    target = worktree / "Demo/Chapter15"
    target.mkdir(parents=True)
    workdir.mkdir(parents=True)
    (workdir / UPDATE_RECOVERY_MARKER_FILENAME).write_text(
        json.dumps({
            "schema_version": UPDATE_RECOVERY_SCHEMA_VERSION,
            "phase": phase,
            "operation_id": RECOVERY_ID,
            "github_repo": cfg.github_repo,
            "pr": 277,
            "branch": BRANCH,
            "original_expected_head": HEAD,
            "base_branch": BASE,
            "base_sha": "c" * 40,
            "current_local_head": "b" * 40,
            "target_path": str(target.resolve()),
        })
        + "\n"
    )

    job = _dispatch(cfg, runs, {ISSUE: str(worktree)})
    child = dispatch_harness.spawned[0].cmd

    assert job.worktree == str(worktree)
    assert child[:4] == [
        sys.executable, "-m", "marathon.recovery", "update-continue"
    ]
    assert _arg(child, "--operation-id") == RECOVERY_ID
    assert dispatch_harness.fetches == []


def test_start_over_archives_before_fresh_main_reset_and_ignores_kept_tree(
    tmp_path, dispatch_harness, monkeypatch
):
    cfg = make_cfg(tmp_path)
    _queue_recovery(cfg, review_state.RECOVERY_START_OVER)
    runs = tmp_path / "runs"
    old_worktree = runs / "wt-i31-old-pr"
    old_worktree.mkdir(parents=True)
    (old_worktree / "old-work.lean").write_text("-- recoverable\n")

    def archive(cfg_, issue, branch, head, recovery_id):
        dispatch_harness.events.append(
            ("archive", (issue, branch, head, recovery_id))
        )
        return True

    monkeypatch.setattr(conductor, "_archive_start_over_head", archive)

    job = _dispatch(cfg, runs, {ISSUE: str(old_worktree)})

    assert job.worktree != str(old_worktree)
    assert (old_worktree / "old-work.lean").is_file()
    assert dispatch_harness.events[0] == (
        "archive", (ISSUE, BRANCH, HEAD, RECOVERY_ID)
    )
    assert dispatch_harness.events[1][0] == "worktree_add"
    add = dispatch_harness.events[1][1]
    assert add[:4] == ("worktree", "add", "-B", BRANCH)
    assert add[-1] == "origin/main"


def test_start_over_identity_marker_precedes_worktree_branch_mutation(
    tmp_path, dispatch_harness, monkeypatch
):
    cfg = make_cfg(tmp_path)
    _queue_recovery(cfg, review_state.RECOVERY_START_OVER)
    runs = tmp_path / "runs"
    monkeypatch.setattr(conductor, "_archive_start_over_head", lambda *a: True)
    original_git = dispatch_harness.git

    def assert_marker_then_git(repo_dir, *args):
        if args[:2] == ("worktree", "add"):
            worktree = Path(args[-2])
            suffix = worktree.name.removeprefix(f"wt-i{ISSUE}-")
            marker = (
                runs
                / f"wd-i{ISSUE}-{suffix}"
                / conductor.START_OVER_RECOVERY_MARKER_FILENAME
            )
            assert marker.is_file(), (
                "start-over ownership must be durable before worktree add "
                "can claim or move the branch"
            )
        return original_git(repo_dir, *args)

    monkeypatch.setattr(conductor, "_git", assert_marker_then_git)

    job = _dispatch(cfg, runs, {})

    assert Path(
        job.workdir, conductor.START_OVER_RECOVERY_MARKER_FILENAME
    ).is_file()


@pytest.mark.parametrize("resume_source", ["kept", "snapshot"])
def test_known_pr_start_over_same_operation_resumes_without_rearchiving(
    tmp_path, dispatch_harness, monkeypatch, resume_source
):
    cfg = make_cfg(tmp_path)
    _queue_recovery(cfg, review_state.RECOVERY_START_OVER)
    archived = []

    def archive(cfg_, issue, branch, head, recovery_id):
        archived.append((issue, branch, head, recovery_id))
        return True

    monkeypatch.setattr(conductor, "_archive_start_over_head", archive)
    runs = tmp_path / "runs"
    first = _dispatch(cfg, runs, {})
    checkpoint = Path(first.workdir) / "known-pr-checkpoint.txt"
    checkpoint.write_text("same paid Aristotle project\n")
    kept = {ISSUE: first.worktree} if resume_source == "kept" else {}
    if resume_source == "snapshot":
        conductor.write_jobs_snapshot(cfg.repo_dir, [first], 1)

    resumed = _dispatch(cfg, runs, kept)

    adds = [
        args
        for _repo, args in dispatch_harness.git_calls
        if args[:2] == ("worktree", "add")
    ]
    assert archived == [(ISSUE, BRANCH, HEAD, RECOVERY_ID)]
    assert len(adds) == 1
    assert resumed.worktree == first.worktree
    assert resumed.workdir == first.workdir
    assert checkpoint.read_text() == "same paid Aristotle project\n"
    assert len(dispatch_harness.spawned) == 2


def test_no_pr_start_over_parks_if_pr_appears_before_dispatch(
    tmp_path, dispatch_harness, monkeypatch
):
    cfg = make_cfg(tmp_path)
    rejected = review_state.record_rejection(
        cfg, ISSUE, "start fresh only if no PR exists"
    )
    review_state.record_iteration(
        cfg, ISSUE, expected_verdict_ts=rejected.verdict_ts
    )
    queued = review_state.request_recovery_if_current(
        cfg,
        ISSUE,
        expected_status="rejected",
        expected_verdict_ts=rejected.verdict_ts,
        recovery_id=RECOVERY_ID,
        recovery_mode=review_state.RECOVERY_START_OVER,
    )
    assert queued is not None
    monkeypatch.setattr(
        conductor,
        "_open_refine_prs_for_issue",
        lambda cfg_, issue: [{"number": 999}],
    )

    job = conductor._dispatch_job(
        cfg, ISSUE, CHAPTER, tmp_path / "runs", {}
    )

    assert job is None
    assert dispatch_harness.spawned == []
    assert dispatch_harness.events == []


def test_no_pr_start_over_carries_expected_absent_branch_lease(
    tmp_path, dispatch_harness, monkeypatch
):
    cfg = make_cfg(tmp_path)
    rejected = review_state.record_rejection(cfg, ISSUE, "confirmed fresh start")
    review_state.record_iteration(
        cfg, ISSUE, expected_verdict_ts=rejected.verdict_ts
    )
    assert review_state.request_recovery_if_current(
        cfg,
        ISSUE,
        expected_status="rejected",
        expected_verdict_ts=rejected.verdict_ts,
        recovery_id=RECOVERY_ID,
        recovery_mode=review_state.RECOVERY_START_OVER,
    ) is not None
    monkeypatch.setattr(
        conductor, "_open_refine_prs_for_issue", lambda cfg_, issue: []
    )
    monkeypatch.setattr(
        conductor, "_refine_branch_for_dispatch", lambda *a: BRANCH
    )
    from marathon import post_pipeline

    monkeypatch.setattr(
        post_pipeline,
        "observe_remote_branch",
        lambda repo, branch: (True, None, f"observed {branch} absent"),
    )

    job = _dispatch(cfg, tmp_path / "runs", {})
    child = dispatch_harness.spawned[0].cmd

    assert job.recovery_head_sha is None
    flag = child.index("--auto-pr-expected-remote-oid")
    assert child[flag + 1] == ""


@pytest.mark.parametrize("stale_kind", ["legacy", "verdict"])
def test_no_pr_start_over_preserves_stale_remote_and_uses_operation_branch(
    tmp_path, dispatch_harness, monkeypatch, stale_kind
):
    from marathon import post_pipeline

    cfg = make_cfg(tmp_path)
    queued = _queue_no_pr_start_over(cfg)
    legacy = post_pipeline._branch_name_for_issue(
        f"Chapter{CHAPTER}", ISSUE, kind="refine"
    )
    verdict = post_pipeline._branch_name_for_issue(
        f"Chapter{CHAPTER}",
        ISSUE,
        kind="refine",
        generation=queued.verdict_ts,
    )
    stale = legacy if stale_kind == "legacy" else verdict
    observed: list[str] = []

    monkeypatch.setattr(
        conductor, "_open_refine_prs_for_issue", lambda cfg_, issue: []
    )

    def observe(repo, branch):
        observed.append(branch)
        if branch == stale:
            return True, HEAD, f"observed stale {branch}"
        return True, None, f"observed {branch} absent"

    monkeypatch.setattr(post_pipeline, "observe_remote_branch", observe)
    operation_branch = post_pipeline._branch_name_for_issue(
        f"Chapter{CHAPTER}",
        ISSUE,
        kind="refine",
        generation=f"start-over:{RECOVERY_ID}:0",
    )

    job = _dispatch(cfg, tmp_path / "runs", {})
    child = dispatch_harness.spawned[0].cmd

    assert stale in observed
    assert job.branch == operation_branch
    assert job.branch not in {legacy, verdict}
    assert _arg(child, "--auto-pr-branch") == operation_branch
    assert _arg(child, "--auto-pr-expected-remote-oid") == ""
    assert not any(args[:1] == ("push",) for _repo, args in dispatch_harness.git_calls)


def test_no_pr_start_over_same_operation_resumes_exact_checkpoint(
    tmp_path, dispatch_harness, monkeypatch
):
    from marathon import post_pipeline

    cfg = make_cfg(tmp_path)
    _queue_no_pr_start_over(cfg)
    monkeypatch.setattr(
        conductor, "_open_refine_prs_for_issue", lambda cfg_, issue: []
    )
    monkeypatch.setattr(
        post_pipeline,
        "observe_remote_branch",
        lambda repo, branch: (True, None, f"observed {branch} absent"),
    )
    runs = tmp_path / "runs"

    first = _dispatch(cfg, runs, {})
    checkpoint = Path(first.workdir) / "aristotle-checkpoint.txt"
    checkpoint.write_text("project from first launch\n")
    dispatch_harness.branch = first.branch

    resumed = _dispatch(cfg, runs, {ISSUE: first.worktree})

    adds = [
        args
        for _repo, args in dispatch_harness.git_calls
        if args[:2] == ("worktree", "add")
    ]
    assert len(adds) == 1
    assert resumed.worktree == first.worktree
    assert resumed.workdir == first.workdir
    assert checkpoint.read_text() == "project from first launch\n"
    assert len(dispatch_harness.spawned) == 2


@pytest.mark.parametrize("dirty", [False, True], ids=["clean", "dirty"])
def test_no_pr_start_over_renames_held_tree_without_losing_files_and_launches(
    tmp_path, monkeypatch, dirty
):
    """Exercise the preservation path with real git worktrees."""
    cfg = make_cfg(tmp_path)
    repo = cfg.repo_dir
    origin = tmp_path / "origin.git"
    target = repo / f"Demo/Chapter{CHAPTER}"
    target.mkdir(parents=True)
    (target / "Seed.lean").write_text("-- main\n")
    subprocess.run(["git", "-C", str(repo), "init", "-b", "main"], check=True)
    subprocess.run(
        ["git", "-C", str(repo), "config", "user.email", "test@example.com"],
        check=True,
    )
    subprocess.run(
        ["git", "-C", str(repo), "config", "user.name", "Marathon Test"],
        check=True,
    )
    subprocess.run(["git", "-C", str(repo), "add", "."], check=True)
    subprocess.run(
        ["git", "-C", str(repo), "commit", "-m", "seed"], check=True
    )
    subprocess.run(["git", "init", "--bare", str(origin)], check=True)
    subprocess.run(
        ["git", "-C", str(repo), "remote", "add", "origin", str(origin)],
        check=True,
    )
    subprocess.run(
        ["git", "-C", str(repo), "push", "-u", "origin", "main"],
        check=True,
    )

    runs = tmp_path / "runs"
    runs.mkdir()
    held = runs / "wt-i31-held"
    subprocess.run(
        [
            "git",
            "-C",
            str(repo),
            "worktree",
            "add",
            "-b",
            BRANCH,
            str(held),
            "origin/main",
        ],
        check=True,
    )
    old_file = held / "old-work.lean"
    old_file.write_text("-- irreplaceable local work\n")
    if not dirty:
        subprocess.run(["git", "-C", str(held), "add", old_file.name], check=True)
        subprocess.run(
            ["git", "-C", str(held), "commit", "-m", "local checkpoint"],
            check=True,
        )
    old_head = subprocess.run(
        ["git", "-C", str(held), "rev-parse", "HEAD"],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    _queue_no_pr_start_over(cfg)

    monkeypatch.setattr(
        conductor, "_open_refine_prs_for_issue", lambda cfg_, issue: []
    )
    monkeypatch.setattr(conductor, "_prime_mathlib_cache", lambda *_: True)
    monkeypatch.setattr(conductor, "_mathlib_already_built", lambda *_: True)
    monkeypatch.setattr(
        conductor, "_write_worktree_pid_marker", lambda *a, **k: True
    )
    real_popen = subprocess.Popen
    spawned = []

    def routed_popen(cmd, *args, **kwargs):
        if list(cmd)[0] == "git":
            return real_popen(cmd, *args, **kwargs)
        proc = SimpleNamespace(pid=7310 + len(spawned), launch_cmd=list(cmd))
        spawned.append(proc)
        return proc

    monkeypatch.setattr(conductor.subprocess, "Popen", routed_popen)
    monkeypatch.setattr(
        conductor,
        "_await_worker_handshake",
        lambda worktree, proc: {"pid": proc.pid, "pgid": proc.pid},
    )

    job = _dispatch(cfg, runs, {})

    archived_branch = subprocess.run(
        ["git", "-C", str(held), "branch", "--show-current"],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    archived_head = subprocess.run(
        ["git", "-C", str(held), "rev-parse", "HEAD"],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    fresh_branch = subprocess.run(
        ["git", "-C", job.worktree, "branch", "--show-current"],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()

    assert old_file.read_text() == "-- irreplaceable local work\n"
    assert archived_branch.startswith(f"marathon/local-archive-i{ISSUE}-")
    assert archived_head == old_head
    assert fresh_branch == job.branch
    assert job.branch != BRANCH
    assert not (Path(job.worktree) / old_file.name).exists()
    assert spawned


def test_conductor_job_snapshot_round_trips_recovery_identity():
    job = conductor.ConductorJob(
        issue_num=ISSUE,
        chapter=CHAPTER,
        target="Demo/Chapter15",
        worktree="/runs/wt-i31",
        workdir="/runs/wd-i31",
        branch=BRANCH,
        expected_recovery_id=RECOVERY_ID,
        recovery_mode=review_state.RECOVERY_CONTINUE_PR,
        recovery_pr=277,
        recovery_head_sha=HEAD,
        recovery_base=BASE,
    )

    encoded = job.to_json()
    restored = conductor.ConductorJob.from_json(encoded)

    assert encoded["expected_recovery_id"] == RECOVERY_ID
    assert encoded["recovery_mode"] == review_state.RECOVERY_CONTINUE_PR
    assert restored.expected_recovery_id == RECOVERY_ID
    assert restored.recovery_mode == review_state.RECOVERY_CONTINUE_PR


def test_reap_finished_passes_expected_recovery_id_to_daemon(
    tmp_path, monkeypatch
):
    cfg = make_cfg(tmp_path)
    worktree = tmp_path / "wt-i31"
    worktree.mkdir()
    captured = {}

    class Finished:
        def poll(self):
            return 0

    job = conductor.ConductorJob(
        issue_num=ISSUE,
        chapter=CHAPTER,
        target="Demo/Chapter15",
        worktree=str(worktree),
        workdir=str(tmp_path / "wd-i31"),
        branch=BRANCH,
        status="running",
        pgid=4310,
        expected_verdict_ts="2026-07-14T12:00:00+00:00",
        expected_recovery_id=RECOVERY_ID,
        recovery_mode=review_state.RECOVERY_CONTINUE_PR,
        proc=Finished(),
    )
    monkeypatch.setattr(conductor, "_process_group_alive", lambda pgid: False)
    monkeypatch.setattr(conductor, "_refresh_job_runtime", lambda job: None)
    monkeypatch.setattr(conductor, "_job_head_sha", lambda job: HEAD)
    monkeypatch.setattr(conductor, "_remove_worktree", lambda *a: None)
    monkeypatch.setattr(
        conductor, "_regenerate_metadata_after_success", lambda cfg: None
    )

    def handle(cfg_, issue, rc, **kwargs):
        captured.update(issue=issue, rc=rc, **kwargs)
        return 0

    monkeypatch.setattr(daemon, "_handle_refine_exit", handle)

    reaped = conductor._reap_finished(
        cfg, [job], {}, {}, max_attempts=3, reconcile=False
    )

    assert reaped == 1
    assert captured["issue"] == ISSUE
    assert captured["expected_recovery_id"] == RECOVERY_ID
    assert captured["expected_verdict_ts"] == job.expected_verdict_ts
    # ``--no-reconcile`` disables the PR-presence follow-up, not immutable
    # branch ownership.  A later normal conductor still needs this identity to
    # merge the successful exact-SHA recovery.
    assert captured["pending_pr_branch"] is None
    assert captured["iteration_branch"] == BRANCH
    assert captured["iteration_commit_sha"] == HEAD


def test_failed_retry_build_does_not_claim_stall_after_recovery_cas_loses(
    tmp_path, monkeypatch, capsys,
):
    cfg = make_cfg(tmp_path)
    queued = _queue_recovery(cfg, review_state.RECOVERY_RETRY_BUILD)
    replacement_id = "replacement-recovery-operation-31"
    replacement = review_state.request_recovery_if_current(
        cfg,
        ISSUE,
        expected_status="rejected",
        expected_verdict_ts=queued.verdict_ts,
        expected_recovery_id=RECOVERY_ID,
        recovery_id=replacement_id,
        recovery_mode=review_state.RECOVERY_RETRY_BUILD,
        recovery_pr=277,
        recovery_branch=BRANCH,
        recovery_head_sha=HEAD,
        recovery_base=BASE,
    )
    assert replacement is not None

    worktree = tmp_path / "wt-i31-stale-build"
    worktree.mkdir()

    class Failed:
        def poll(self):
            return 9

    job = conductor.ConductorJob(
        issue_num=ISSUE,
        chapter=CHAPTER,
        target="Demo/Chapter15",
        worktree=str(worktree),
        workdir=str(tmp_path / "wd-i31-stale-build"),
        branch=BRANCH,
        status="running",
        pgid=4310,
        expected_verdict_ts=queued.verdict_ts,
        expected_recovery_id=RECOVERY_ID,
        recovery_mode=review_state.RECOVERY_RETRY_BUILD,
        proc=Failed(),
    )
    removed: list[Path] = []
    monkeypatch.setattr(conductor, "_process_group_alive", lambda pgid: False)
    monkeypatch.setattr(conductor, "_refresh_job_runtime", lambda job: None)
    monkeypatch.setattr(
        conductor,
        "_remove_worktree",
        lambda repo, path: removed.append(path),
    )

    reaped = conductor._reap_finished(
        cfg, [job], {}, {}, max_attempts=3, reconcile=False
    )

    current = review_state.load_state(cfg).issues[ISSUE]
    assert reaped == 1
    assert job.status == "superseded"
    assert current.status == "rejected"
    assert current.recovery_id == replacement_id
    assert removed == [worktree]
    output = capsys.readouterr().out
    assert "superseded by newer review state" in output
    assert "parked for the next explicit recovery choice" not in output


def test_superseded_recovery_worktree_is_not_adopted_after_restart(
    tmp_path, monkeypatch,
):
    cfg = make_cfg(tmp_path)
    queued = _queue_recovery(cfg, review_state.RECOVERY_RETRY_BUILD)
    replacement_id = "replacement-recovery-operation-31"
    replacement = review_state.request_recovery_if_current(
        cfg,
        ISSUE,
        expected_status="rejected",
        expected_verdict_ts=queued.verdict_ts,
        expected_recovery_id=RECOVERY_ID,
        recovery_id=replacement_id,
        recovery_mode=review_state.RECOVERY_RETRY_BUILD,
        recovery_pr=277,
        recovery_branch=BRANCH,
        recovery_head_sha=HEAD,
        recovery_base=BASE,
    )
    assert replacement is not None

    runs = tmp_path / "runs"
    worktree = runs / "wt-i31-superseded"
    workdir = runs / "wd-i31-superseded"
    worktree.mkdir(parents=True)
    workdir.mkdir(parents=True)
    conductor.write_jobs_snapshot(
        cfg.repo_dir,
        [
            conductor.ConductorJob(
                issue_num=ISSUE,
                chapter=CHAPTER,
                target="Demo/Chapter15",
                worktree=str(worktree),
                workdir=str(workdir),
                branch=BRANCH,
                status="superseded",
                expected_verdict_ts=queued.verdict_ts,
                expected_recovery_id=RECOVERY_ID,
                recovery_mode=review_state.RECOVERY_RETRY_BUILD,
            )
        ],
        concurrency=1,
    )
    monkeypatch.setattr(conductor, "_worktree_live_pid", lambda _path: None)

    recovered = conductor._recover_retry_worktrees(
        cfg, runs, busy_issues=set()
    )

    assert recovered == {}
