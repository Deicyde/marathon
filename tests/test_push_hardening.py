"""B0 hardening of the conductor's job machinery (the #98 failure class):
- pushes use an explicit refspec plus an observed-OID force-with-lease, so they
  survive an upstream mismatch without overwriting an unowned remote ref;
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


def test_open_or_update_pr_uses_explicit_oid_lease_and_refspec(monkeypatch):
    calls = []
    monkeypatch.setattr(pp.subprocess, "run", lambda a, **k: calls.append(a) or _ok(a))
    monkeypatch.setattr(pp, "_existing_pr_number", lambda *a, **k: None)
    monkeypatch.setattr(pp, "_gh", lambda *a, **k: _ok(("gh",) + a))
    ok, _ = pp.open_or_update_pr(
        repo_dir=Path("/repo"), branch="marathon/refine-c12-i98",
        base="main", repo="o/r", title="t", body="b",
    )
    # Explicit lease says the branch was observed absent. A concurrent/stale
    # remote ref cannot be clobbered by a plain force push.
    assert [
        "git", "push",
        "--force-with-lease=refs/heads/marathon/refine-c12-i98:",
        "origin", "HEAD:refs/heads/marathon/refine-c12-i98",
    ] in calls


def test_repeat_issue_session_updates_owned_open_pr_without_duplicate(
    monkeypatch, tmp_path,
):
    """A later verdict keeps the persistent branch when its PR is still open,
    CAS-updates that remote head, and PATCHes the existing PR (never POSTs a
    duplicate)."""
    from tests.test_conductor import make_cfg

    cfg = make_cfg(tmp_path)
    legacy = "marathon/refine-c14-i22"
    monkeypatch.setattr(
        cond,
        "_git",
        lambda *a, **k: SimpleNamespace(
            returncode=0,
            stdout=f"50eff009\trefs/heads/{legacy}\n",
            stderr="",
        ),
    )
    monkeypatch.setattr(
        cond,
        "_branch_pull_requests",
        lambda *a, **k: [{"number": 273, "head": {"sha": "50eff009"}}],
    )
    assert cond._refine_branch_for_dispatch(
        cfg, 22, 14, "2026-07-10T11:21:06+01:00"
    ) == legacy

    pushes = []
    api_calls = []
    monkeypatch.setattr(
        pp,
        "observe_remote_branch",
        lambda *a, **k: (True, "50eff009deadbeef", "observed"),
    )
    monkeypatch.setattr(
        pp.subprocess,
        "run",
        lambda args, **kw: pushes.append(args) or _ok(args),
    )
    monkeypatch.setattr(pp, "_existing_pr_number", lambda *a, **k: 273)
    monkeypatch.setattr(
        pp,
        "_gh",
        lambda *args, **kw: api_calls.append(args) or _ok(("gh",) + args),
    )

    ok, url = pp.open_or_update_pr(
        repo_dir=Path("/repo"), branch=legacy, base="main", repo="o/r",
        title="new", body="new body",
        expected_remote_oid="50eff009deadbeef",
        local_commit_sha="6221ffa0",
    )

    assert ok and url.endswith("/273")
    assert [
        "git", "push",
        f"--force-with-lease=refs/heads/{legacy}:50eff009deadbeef",
        "origin", f"HEAD:refs/heads/{legacy}",
    ] in pushes
    assert any(call[:4] == ("api", "--method", "PATCH", "repos/o/r/pulls/273")
               for call in api_calls)
    assert not any("POST" in call for call in api_calls)


def test_stale_remote_without_open_pr_gets_verdict_branch(monkeypatch, tmp_path):
    """A leftover/closed remote ref is preserved, not force-overwritten."""
    from tests.test_conductor import make_cfg

    cfg = make_cfg(tmp_path)
    legacy = "marathon/refine-c14-i22"
    verdict = "2026-07-10T11:21:06+01:00"
    monkeypatch.setattr(
        cond,
        "_git",
        lambda *a, **k: SimpleNamespace(
            returncode=0,
            stdout=f"oldsha\trefs/heads/{legacy}\n",
            stderr="",
        ),
    )
    monkeypatch.setattr(cond, "_branch_pull_requests", lambda *a, **k: [])

    selected = cond._refine_branch_for_dispatch(cfg, 22, 14, verdict)

    assert selected == pp._branch_name_for_issue(
        "Chapter14", 22, generation=verdict
    )
    assert selected != legacy


def test_safe_push_refuses_remote_changed_after_preparation(monkeypatch):
    calls = []
    monkeypatch.setattr(
        pp,
        "observe_remote_branch",
        lambda *a, **k: (True, "someone-elses-sha", "observed"),
    )
    monkeypatch.setattr(
        pp.subprocess, "run", lambda args, **kw: calls.append(args) or _ok(args)
    )

    ok, message = pp.open_or_update_pr(
        repo_dir=Path("/repo"), branch="marathon/refine-c14-i22",
        base="main", repo="o/r", title="t", body="b",
        expected_remote_oid="50eff009",
        local_commit_sha="6221ffa0",
    )

    assert not ok and "refusing to overwrite" in message
    assert not any(call[:2] == ["git", "push"] for call in calls)


# --- B0c: requeue a refine that landed no PR ---------------------------------

def _job(issue=98, branch="marathon/refine-c12-i98"):
    return SimpleNamespace(
        issue_num=issue,
        branch=branch,
        iteration_commit_sha="new-head-sha",
    )


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
    assert requeued == [(
        "marathon/refine-c12-i98",
        {"landed": False, "expected_recovery_id": None},
    )]


def test_pr_exists_does_not_requeue(monkeypatch):
    cfg = SimpleNamespace(github_repo="o/r")
    _patch_gh(
        monkeypatch,
        '[{"number":116,"head":{"sha":"new-head-sha"}}]',
    )  # matching PR exists → leave to merge→requeue
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

def test_pipeline_writes_exact_publication_journal_before_network(
    monkeypatch, tmp_path,
):
    """The WAL must exist before push starts and survive a successful PR."""
    repo = tmp_path / "repo"
    target = repo / "Demo" / "Chapter14"
    target.mkdir(parents=True)
    events: list[tuple[str, object]] = []
    journal: list[dict] = []

    monkeypatch.setattr(
        pp,
        "run_git_commit",
        lambda *a, **k: pp.CommitResult(sha="c" * 40),
    )
    monkeypatch.setattr(
        pp.subprocess,
        "run",
        lambda args, **kw: subprocess.CompletedProcess(args, 0, "", ""),
    )

    def persist(recipe):
        journal.append(dict(recipe))
        events.append(("journal", recipe["commit_sha"]))

    def publish(**kwargs):
        events.append(("network", kwargs["local_commit_sha"]))
        assert journal and journal[-1]["commit_sha"] == kwargs["local_commit_sha"]
        return True, "https://github.com/o/r/pull/9"

    monkeypatch.setattr(pp, "open_or_update_pr", publish)
    config = pp.PipelineConfig(
        auto_commit=True,
        auto_pr=True,
        auto_pr_repo="o/r",
        auto_pr_branch="marathon/refine-c14-i22",
        auto_pr_base="main",
        auto_pr_expected_remote_oid="a" * 40,
        recovery_operation_id="operation-22",
        publication_journal=persist,
        gate="off",
        enforce_frozen_statements=False,
        update_formalization=False,
    )

    result = pp.run_post_pipeline(
        config,
        repo,
        target,
        "Chapter14",
        1,
        "project",
    )

    assert [name for name, _ in events] == ["journal", "network"]
    assert journal == [{
        "schema_version": pp.PUBLICATION_JOURNAL_SCHEMA_VERSION,
        "operation_id": "operation-22",
        "kind": "pr",
        "commit_sha": "c" * 40,
        "branch": "marathon/refine-c14-i22",
        "base": "main",
        "repo": "o/r",
        "title": result["pending_publication"]["title"],
        "body": result["pending_publication"]["body"],
        "expected_remote_oid": "a" * 40,
    }]
    # Success does not clear the WAL inside the pipeline; refine clears it in
    # the same atomic save that records the iteration complete.
    assert result["pr_ok"] is True
    assert result["pending_publication"] == journal[0]


def test_pr_only_retry_skips_push_already_at_committed_head(monkeypatch):
    """If the push landed but PR creation failed, retry only the REST PR step;
    the stale pre-push lease does not cause a second push or another session."""
    pushes = []
    api_calls = []
    monkeypatch.setattr(
        pp,
        "observe_remote_branch",
        lambda *a, **k: (True, "a" * 40, "observed"),
    )
    monkeypatch.setattr(
        pp.subprocess,
        "run",
        lambda args, **kw: pushes.append(args) or _ok(args),
    )
    monkeypatch.setattr(pp, "_existing_pr_number", lambda *a, **k: None)
    monkeypatch.setattr(
        pp,
        "_gh",
        lambda *args, **kw: api_calls.append(args)
        or subprocess.CompletedProcess(args, 0, "https://github.com/o/r/pull/9\n", ""),
    )

    ok, _ = pp.open_or_update_pr(
        repo_dir=Path("/repo"), branch="marathon/refine-c14-i22",
        base="main", repo="o/r", title="t", body="b",
        expected_remote_oid="old-pre-push-oid",
        local_commit_sha="a" * 40,
    )

    assert ok
    assert not any(call[:2] == ["git", "push"] for call in pushes)
    assert any(call[:3] == ("api", "--method", "POST") for call in api_calls)

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
        iteration_commit_sha="new-head-sha",
    )
    answers = iter([None, False])
    monkeypatch.setattr(cond, "_branch_pr_exists", lambda *args: next(answers))

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


def test_stale_prior_pr_does_not_count_as_new_iteration(monkeypatch):
    cfg = SimpleNamespace(github_repo="o/r")
    branch = "marathon/refine-c16-i238"
    monkeypatch.setattr(
        cond,
        "_branch_pull_requests",
        lambda *a, **k: [
            {"number": 273, "head": {"sha": "50eff0099056a3c7"}}
        ],
    )

    assert cond._branch_pr_exists(cfg, branch, "6221ffa0da414cce") is False
    assert cond._branch_pr_exists(cfg, branch, "50eff009") is True


def test_legacy_suppressed_no_pr_row_is_requeued_by_sha_audit(
    monkeypatch, tmp_path,
):
    """Exact live #238 shape: last_iteration is set, no pending marker was
    persisted, and the branch's only PR points at an older commit."""
    from tests.test_conductor import make_cfg

    cfg = make_cfg(tmp_path)
    verdict = record_rejection(cfg, 22, "fix")
    branch = "marathon/refine-c14-i22"
    record_iteration(
        cfg,
        22,
        expected_verdict_ts=verdict.verdict_ts,
        iteration_branch=branch,
        iteration_commit_sha="6221ffa0",
    )
    monkeypatch.setattr(
        cond,
        "_branch_pr_exists",
        lambda cfg_, branch_, expected: (
            False
            if branch_ == branch and expected == "6221ffa0"
            else (_ for _ in ()).throw(AssertionError("unexpected check"))
        ),
    )

    def requeue(cfg_, branch_, **kwargs):
        assert kwargs == {
            "landed": False,
            "expected_verdict_ts": verdict.verdict_ts,
            "expected_recovery_id": None,
        }
        assert requeue_for_rereview(
            cfg_, 22, expected_verdict_ts=verdict.verdict_ts
        )
        return 22

    monkeypatch.setattr(amod, "_requeue_refined_issue", requeue)

    assert cond._retry_pending_pr_checks(cfg) == 1
    assert 22 not in load_state(cfg).issues


def test_pr_presence_check_uses_rest_not_graphql(monkeypatch):
    cfg = SimpleNamespace(github_repo="o/r")
    calls = []

    def fake_gh(*args, **kwargs):
        calls.append(args)
        return SimpleNamespace(
            returncode=0,
            stdout='[{"number":273,"head":{"sha":"6221ffa0"}}]',
            stderr="",
        )

    monkeypatch.setattr(ghmod, "gh", fake_gh)

    assert cond._branch_pr_exists(
        cfg, "marathon/refine-c16-i238", "6221ffa0"
    ) is True
    assert calls and calls[0][0] == "api"
    assert "pulls?" in calls[0][1]
    assert "pr" not in calls[0][:1] and "repo" not in calls[0][:1]


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
        cond,
        "_refine_branch_for_dispatch",
        lambda *a, **k: "marathon/refine-c14-i22",
    )
    monkeypatch.setattr(
        cond.subprocess,
        "Popen",
        lambda cmd, **kw: spawned.append(cmd) or SimpleNamespace(pid=4001),
    )
    monkeypatch.setattr(
        cond,
        "_await_worker_handshake",
        lambda _wt, proc, **_kw: {
            "pid": proc.pid, "pgid": proc.pid, "status": "running",
        },
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
    assert cmd[cmd.index("--auto-pr-repo") + 1] == cfg.github_repo
    assert cmd[cmd.index("--auto-pr-branch") + 1] == \
        "marathon/refine-c14-i22"


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
