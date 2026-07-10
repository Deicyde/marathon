"""Two conductor/refine bugs around the --auto-pr branch-prep abort.

BUG 1 — early abort is a FAILURE that auto-retries (not a phantom success).
  ``prepare_auto_pr_branch`` returns ``(ok=False, ...)`` when ``git fetch
  origin <base>`` fails (e.g. a git-AUTH failure). The refine used to do a
  bare ``return`` there, which exits the process 0 → the conductor marked
  the job *succeeded*, recorded a phantom iteration (last_iteration_ts), and
  NEVER retried, silently stranding the rejection (the #156 symptom). The
  abort now ``sys.exit(EXIT_AUTO_PR_BRANCH_ABORT)`` (non-zero), so the
  conductor's failure branch runs (record_failed_attempt + backoff retry)
  and last_iteration_ts is left untouched → needs_iteration stays True.

BUG 2 — detect + surface git-auth failures.
  ``_is_git_auth_error`` classifies stderr; ``prepare_auto_pr_branch``
  emits an actionable, AUTH-flagged message on an auth failure; and the
  conductor prints a DISTINCT top-level warning when a refine fails with the
  distinguished EXIT_AUTO_PR_BRANCH_ABORT code.
"""
from __future__ import annotations

import asyncio
import subprocess
from types import SimpleNamespace

import pytest

import marathon.conductor as conductor
import marathon.post_pipeline as post_pipeline
import marathon.refine as refine
import marathon.review.daemon as daemon
from marathon.state import RefineState, save_refine_state
from marathon.aristotle_runtime import ReattachTransientError
from marathon.post_pipeline import _is_git_auth_error, prepare_auto_pr_branch

from tests.test_conductor import make_cfg, seed_rejections  # reuse fixtures' helpers


# ---------------------------------------------------------------------------
# BUG 2a — _is_git_auth_error: true for each signature, false for normal err
# ---------------------------------------------------------------------------

AUTH_STDERRS = [
    "fatal: could not read Username for 'https://github.com': Device not configured",
    "remote: Support for password authentication was removed. Authentication failed for ...",
    "fatal: could not read Password for 'https://github.com'",  # 'fatal: could not read'
    "fatal: Authentication failed for 'https://github.com/owner/repo.git/'",
    "git@github.com: Permission denied (publickey).",
    "fatal: could not read Username for 'https://github.com': terminal prompts disabled",
    "remote: Invalid username or password.",
    "remote: Invalid credentials",
]


@pytest.mark.parametrize("stderr", AUTH_STDERRS)
def test_is_git_auth_error_true_for_each_signature(stderr):
    assert _is_git_auth_error(stderr) is True


@pytest.mark.parametrize(
    "stderr",
    [
        "fatal: couldn't find remote ref main",
        "error: Your local changes to the following files would be overwritten",
        "fatal: not a git repository (or any of the parent directories): .git",
        "merge conflict in Foo.lean",
        "",
    ],
)
def test_is_git_auth_error_false_for_normal_errors(stderr):
    assert _is_git_auth_error(stderr) is False


# ---------------------------------------------------------------------------
# BUG 2b — prepare_auto_pr_branch: auth failure → AUTH-flagged actionable
# message; non-auth failure → normal message.
# ---------------------------------------------------------------------------

def _fetch_failure(monkeypatch, stderr: str):
    """Make the first git op (``git fetch origin <base>``) fail with
    ``stderr``; every other subprocess.run is a no-op success."""
    def fake_run(cmd, **kwargs):
        if cmd[:3] == ["git", "fetch", "origin"]:
            return subprocess.CompletedProcess(cmd, 1, stdout="", stderr=stderr)
        return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")

    monkeypatch.setattr(post_pipeline.subprocess, "run", fake_run)


def test_prepare_auto_pr_branch_auth_failure_is_flagged(monkeypatch, tmp_path):
    _fetch_failure(
        monkeypatch,
        "fatal: could not read Username for 'https://github.com': Device not configured",
    )
    ok, branch, msg = prepare_auto_pr_branch(tmp_path, "Chapter14", 156, base="main")
    assert ok is False
    assert "GIT AUTH" in msg
    assert "gh auth setup-git" in msg
    # The underlying stderr snippet is still surfaced for the human.
    assert "Device not configured" in msg


def test_prepare_auto_pr_branch_non_auth_failure_is_normal(monkeypatch, tmp_path):
    _fetch_failure(monkeypatch, "fatal: couldn't find remote ref main")
    ok, branch, msg = prepare_auto_pr_branch(tmp_path, "Chapter14", 156, base="main")
    assert ok is False
    assert "GIT AUTH" not in msg
    assert "gh auth setup-git" not in msg
    assert "git fetch origin main failed" in msg
    assert "couldn't find remote ref main" in msg


# ---------------------------------------------------------------------------
# BUG 1 — the refine abort path exits NON-ZERO with the distinguished code.
# ---------------------------------------------------------------------------

def _make_refine_args(repo_dir, target, workdir):
    """Minimal args namespace to drive refine_command up to the auto_pr
    branch-prep gate. Only the fields read before that gate matter."""
    return SimpleNamespace(
        target=target,
        repo_dir=repo_dir,
        workdir=workdir,
        tex=None,
        max_iterations=1,
        max_retries=1,
        polling_interval=5,          # read when building the _run_iteration call
        skeleton=False,
        max_prompt_words=None,
        no_cross_chapter=True,
        review_rejection=156,
        review_verdict_ts="2026-07-10T10:00:00+00:00",
        review_rejection_note_file=None,
        auto_build=False,
        auto_commit=False,
        auto_push=False,
        auto_rate=False,
        build_timeout=600,
        auto_pr=True,                # <-- reach the branch-prep gate
        auto_pr_repo=None,
        auto_pr_base="main",
        gate="warn",
        gate_override=None,
        jury=False,
        dry_run=False,
        live_steering=False,
    )


def test_refine_auto_pr_abort_exits_nonzero(monkeypatch, tmp_path):
    """When prepare_auto_pr_branch returns (False, ...), refine_command
    must sys.exit with the distinguished non-zero EXIT_AUTO_PR_BRANCH_ABORT
    code — NOT a bare return (which would exit 0 = phantom success).

    Driven via ``asyncio.run`` (the suite has no pytest-asyncio; this is
    exactly how ``__main__.main`` invokes ``refine_command``)."""
    repo_dir = tmp_path / "repo"
    target = repo_dir / "Demo" / "Chapter14"
    target.mkdir(parents=True)
    (repo_dir / ".git").mkdir()  # refine_command only checks `.git` exists
    workdir = tmp_path / "wd"
    workdir.mkdir()

    monkeypatch.setattr(refine, "_ensure_api_key", lambda: "sk-test")
    monkeypatch.setattr(refine, "_load_pending_rejections_md",
                        lambda *a, **k: "- **Review #156 REJECTED**\n\nfix it")
    # prepare_auto_pr_branch is imported lazily INSIDE refine_command from
    # marathon.post_pipeline — patch it on that module.
    monkeypatch.setattr(
        post_pipeline, "prepare_auto_pr_branch",
        lambda **k: (False, "marathon/refine-c14-i156",
                     "git fetch origin main failed — GIT AUTH: ..."),
    )

    args = _make_refine_args(repo_dir, target, workdir)
    with pytest.raises(SystemExit) as exc:
        asyncio.run(refine.refine_command(args))
    # Distinguished, non-zero — and exactly the code the conductor keys on.
    assert exc.value.code == refine.EXIT_AUTO_PR_BRANCH_ABORT
    assert exc.value.code != 0


def test_force_continuation_preserves_local_branch_without_reset(
    monkeypatch, tmp_path,
):
    repo_dir = tmp_path / "repo"
    target = repo_dir / "Demo" / "Chapter14"
    target.mkdir(parents=True)
    (repo_dir / ".git").mkdir()
    workdir = tmp_path / "wd"
    workdir.mkdir()
    save_refine_state(
        workdir / refine.REFINE_STATE_FILENAME,
        RefineState(
            target_folder=str(target),
            force_continuation=True,
            preserve_local_branch=True,
            continuation_commit_sha="abc12345",
        ),
    )
    monkeypatch.setattr(refine, "_ensure_api_key", lambda: "sk-test")
    monkeypatch.setattr(
        refine,
        "_load_pending_rejections_md",
        lambda *a, **k: "- **Review #156 REJECTED**\n\nfix it",
    )
    monkeypatch.setattr(
        post_pipeline,
        "prepare_auto_pr_branch",
        lambda **k: (_ for _ in ()).throw(
            AssertionError("must not checkout/reset a gate-blocked local commit")
        ),
    )

    def fake_git(cmd, **kwargs):
        if cmd[:3] == ["git", "branch", "--show-current"]:
            return SimpleNamespace(
                returncode=0, stdout="marathon/refine-c14-i156\n", stderr=""
            )
        if cmd[:3] == ["git", "rev-parse", "HEAD"]:
            return SimpleNamespace(
                returncode=0, stdout="abc12345deadbeef\n", stderr=""
            )
        raise AssertionError(f"unexpected git mutation/query: {cmd}")

    monkeypatch.setattr(refine.subprocess, "run", fake_git)
    args = _make_refine_args(repo_dir, target, workdir)
    args.dry_run = True

    asyncio.run(refine.refine_command(args))

    after = refine.load_refine_state(workdir / refine.REFINE_STATE_FILENAME)
    assert after is not None and after.preserve_local_branch is True
    assert after.continuation_commit_sha == "abc12345"


def test_transient_reattach_exits_attempt_free_and_preserves_ids(
    monkeypatch, tmp_path,
):
    repo_dir = tmp_path / "repo"
    target = repo_dir / "Demo" / "Chapter14"
    target.mkdir(parents=True)
    (repo_dir / ".git").mkdir()
    workdir = tmp_path / "wd"
    workdir.mkdir()
    save_refine_state(
        workdir / refine.REFINE_STATE_FILENAME,
        RefineState(
            target_folder=str(target),
            current_iteration_idx=1,
            project_id="p-saved",
            agent_task_id="t-saved",
            status="IN_PROGRESS",
        ),
    )
    monkeypatch.setattr(refine, "_ensure_api_key", lambda: "sk-test")
    monkeypatch.setattr(
        refine,
        "_load_pending_rejections_md",
        lambda *a, **k: "- **Review #156 REJECTED**\n\nfix it",
    )
    monkeypatch.setattr(
        post_pipeline,
        "prepare_auto_pr_branch",
        lambda **k: (True, "marathon/refine-c14-i156", "on branch"),
    )

    async def transient(*args, **kwargs):
        raise ReattachTransientError("temporary 503")

    monkeypatch.setattr(refine, "_try_reattach_or_continue", transient)
    args = _make_refine_args(repo_dir, target, workdir)
    with pytest.raises(SystemExit) as exc:
        asyncio.run(refine.refine_command(args))
    assert exc.value.code == refine.EXIT_REATTACH_TRANSIENT
    after = refine.load_refine_state(workdir / refine.REFINE_STATE_FILENAME)
    assert after is not None
    assert after.project_id == "p-saved" and after.agent_task_id == "t-saved"
    assert after.status == "REATTACH_RETRY"


# ---------------------------------------------------------------------------
# BUG 2c — conductor surfaces a DISTINCT top-level git-auth warning when a
# refine job fails with the distinguished exit code.
# ---------------------------------------------------------------------------

def test_conductor_warns_on_auth_abort_exit_code(tmp_path, monkeypatch, capsys):
    """A refine job that exits with EXIT_AUTO_PR_BRANCH_ABORT is marked
    failed (not succeeded), records a failed attempt (no last_iteration_ts),
    stays needs_iteration=True, AND prints a distinct top-level GIT AUTH
    warning."""
    cfg = make_cfg(tmp_path)
    seed_rejections(cfg, {22: "2026-06-10T12:00:00-04:00"})

    job = conductor.ConductorJob(
        issue_num=22, chapter=14, target="Demo/Chapter14",
        worktree=str(cfg.repo_dir.parent / "runs" / "wt-i22-x"),
        workdir=str(cfg.repo_dir.parent / "runs" / "wd-i22-x"),
        branch="marathon/refine-c14-i22", pid=4001, status="running",
        proc=SimpleNamespace(poll=lambda: refine.EXIT_AUTO_PR_BRANCH_ABORT),
    )
    reaped = conductor._reap_finished(cfg, [job], {}, {}, max_attempts=3)

    assert reaped == 1
    # FAILED, not succeeded — the phantom-success bug is gone.
    assert job.status == "failed"
    from marathon.review.state import load_state
    entry = load_state(cfg).issues[22]
    assert entry.attempts == 1                 # record_failed_attempt ran
    assert entry.last_iteration_ts is None     # no phantom iteration recorded
    assert entry.needs_iteration()             # conductor will auto-retry

    out = capsys.readouterr().out
    assert "GIT AUTH" in out
    assert "gh auth setup-git" in out
    assert "#22" in out


# ---------------------------------------------------------------------------
# BUG 3 — a FAILED iteration (RETRIES_EXHAUSTED / terminal status) is the same
# phantom-success class on a different path: the refine main loop used to do a
# bare ``return`` on ``not ok`` (exit 0 → phantom iteration → silently stranded,
# the #170 symptom). It now ``sys.exit(EXIT_ITERATION_FAILED)`` so the conductor
# retries with backoff and STALLS (visible + notified) instead of vanishing.
# ---------------------------------------------------------------------------

def test_refine_iteration_failure_exits_nonzero(monkeypatch, tmp_path):
    """``_run_iteration`` → False (e.g. RETRIES_EXHAUSTED) makes refine_command
    sys.exit(EXIT_ITERATION_FAILED), NOT a bare return (exit 0 = phantom success)."""
    repo_dir = tmp_path / "repo"
    target = repo_dir / "Demo" / "Chapter14"
    target.mkdir(parents=True)
    (repo_dir / ".git").mkdir()
    workdir = tmp_path / "wd"
    workdir.mkdir()

    monkeypatch.setattr(refine, "_ensure_api_key", lambda: "sk-test")
    monkeypatch.setattr(
        refine,
        "_load_pending_rejections_md",
        lambda *a, **k: "- **Review #156 REJECTED**\n\nfix it",
    )
    # branch prep SUCCEEDS so we reach the iteration loop...
    monkeypatch.setattr(
        post_pipeline, "prepare_auto_pr_branch",
        lambda **k: (True, "marathon/refine-c14-i170", "auto-pr: ok"))

    async def _no_reattach(*a, **k):
        return (None, None, None)

    monkeypatch.setattr(refine, "_try_reattach_or_continue", _no_reattach)

    async def _fail(**k):                      # the iteration FAILS
        st = k.get("state")
        if st is not None:
            st.status = "RETRIES_EXHAUSTED"
            st.note = "reached max retries (2 attempts)"
        return False

    monkeypatch.setattr(refine, "_run_iteration", _fail)

    args = _make_refine_args(repo_dir, target, workdir)
    with pytest.raises(SystemExit) as exc:
        asyncio.run(refine.refine_command(args))
    assert exc.value.code == refine.EXIT_ITERATION_FAILED
    assert exc.value.code != 0


def test_conductor_iteration_failure_retries_then_stalls(tmp_path, monkeypatch):
    """A job exiting EXIT_ITERATION_FAILED is FAILED (not succeeded): no phantom
    last_iteration_ts, needs_iteration stays True so it auto-retries; once the
    attempt budget is spent it STALLS (visible) instead of silently parking."""
    monkeypatch.setattr(daemon, "_notify_stall", lambda *a, **k: None)  # no gh
    cfg = make_cfg(tmp_path)
    seed_rejections(cfg, {22: "2026-06-10T12:00:00-04:00"})
    from marathon.review.state import load_state

    def _job():
        return conductor.ConductorJob(
            issue_num=22, chapter=14, target="Demo/Chapter14",
            worktree=str(cfg.repo_dir.parent / "runs" / "wt-i22-x"),
            workdir=str(cfg.repo_dir.parent / "runs" / "wd-i22-x"),
            branch="marathon/refine-c14-i22", pid=4001, status="running",
            proc=SimpleNamespace(poll=lambda: refine.EXIT_ITERATION_FAILED),
        )

    # 1st failure (budget 2) → failed, retriable, NOT yet stalled, no phantom ts.
    conductor._reap_finished(cfg, [_job()], {}, {}, max_attempts=2)
    e = load_state(cfg).issues[22]
    assert e.attempts == 1 and e.last_iteration_ts is None
    assert e.status == "rejected" and e.needs_iteration()
    # 2nd failure hits the budget → STALLED (dropped from the queue, surfaced).
    conductor._reap_finished(cfg, [_job()], {}, {}, max_attempts=2)
    e = load_state(cfg).issues[22]
    assert e.status == "stalled" and not e.needs_iteration()
