"""The conductor reaps an orphan worktree holding an issue's refine branch
(left by a failed refine of a previous run) instead of wedging on it —
UNLESS a live job from a previous conductor still uses it (the reap guard:
reaping under a running refine destroys a billed Aristotle run, the
2026-07-07 #171 incident)."""
from __future__ import annotations

import json
import os
from pathlib import Path
from types import SimpleNamespace

import pytest

import marathon.conductor as conductor
from marathon.refine import REFINE_STATE_FILENAME
from marathon.state import RefineState, save_refine_state


@pytest.fixture(autouse=True)
def _clean_inherited():
    """The inherited-running map is module state; keep tests isolated."""
    conductor._INHERITED_RUNNING.clear()
    yield
    conductor._INHERITED_RUNNING.clear()


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
        if args[:2] == ("status", "--porcelain"):
            return SimpleNamespace(returncode=0, stdout="", stderr="")
        if args[:2] == ("rev-parse", "HEAD"):
            return SimpleNamespace(returncode=0, stdout="abc123\n", stderr="")
        if args[:2] == ("ls-remote", "--heads"):
            return SimpleNamespace(
                returncode=0,
                stdout=(
                    "abc123\trefs/heads/marathon/refine-c12-i96\n"
                ),
                stderr="",
            )
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


# --- the reap guard: a live job's worktree is NEVER reaped -------------------

def _fake_git_with_holder(worktree_path):
    list_out = (
        f"worktree {worktree_path}\n"
        "HEAD abc123\n"
        "branch refs/heads/marathon/refine-c12-i96\n"
    )

    def fake_git(repo_dir, *args):
        if args[:2] == ("worktree", "list"):
            return SimpleNamespace(returncode=0, stdout=list_out, stderr="")
        if args[:2] == ("status", "--porcelain"):
            return SimpleNamespace(returncode=0, stdout="", stderr="")
        if args[:2] == ("rev-parse", "HEAD"):
            return SimpleNamespace(returncode=0, stdout="abc123\n", stderr="")
        if args[:2] == ("ls-remote", "--heads"):
            return SimpleNamespace(
                returncode=0,
                stdout=(
                    "abc123\trefs/heads/marathon/refine-c12-i96\n"
                ),
                stderr="",
            )
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    return fake_git


def test_reap_guard_refuses_live_pid_marker(monkeypatch, tmp_path):
    # The worktree carries a pid marker for a genuine LIVE job (identity
    # confirmed) → the guard must refuse and report nothing cleaned.
    wt = tmp_path / "wt-i96-live"
    conductor._write_worktree_pid_marker(wt, os.getpid())
    monkeypatch.setattr(conductor, "_git", _fake_git_with_holder(wt))
    monkeypatch.setattr(
        conductor, "_pid_is_job_for", lambda pid, w, **_kw: True
    )

    def _boom(*a):
        raise AssertionError("reaped a worktree with a live job")

    monkeypatch.setattr(conductor, "_remove_worktree", _boom)
    assert conductor._reap_orphan_worktree(tmp_path, "marathon/refine-c12-i96") is False


def test_reap_guard_reaps_when_marker_pid_dead(monkeypatch, tmp_path):
    # Marker present but its pid is dead → stale droppings, reap normally.
    import subprocess
    dead = subprocess.Popen(["true"])
    dead.wait()
    wt = tmp_path / "wt-i96-dead"
    conductor._write_worktree_pid_marker(wt, dead.pid)
    monkeypatch.setattr(conductor, "_git", _fake_git_with_holder(wt))
    removed = []
    monkeypatch.setattr(conductor, "_remove_worktree",
                        lambda repo, w: removed.append(str(w)))
    assert conductor._reap_orphan_worktree(tmp_path, "marathon/refine-c12-i96") is True
    assert removed == [str(wt)]


@pytest.mark.parametrize(
    ("status", "local_sha", "remote_sha", "reason"),
    [
        (" M Demo/Chapter12/Foo.lean\n", "abc123", "abc123", "uncommitted"),
        ("", "local-ahead", "published", "ahead of or diverged"),
    ],
)
def test_reap_preserves_unpublished_orphan_worktree(
    monkeypatch, tmp_path, status, local_sha, remote_sha, reason, capsys
):
    wt = tmp_path / "wt-i96-unpublished"
    branch = "marathon/refine-c12-i96"
    list_out = (
        f"worktree {wt}\n"
        f"HEAD {local_sha}\n"
        f"branch refs/heads/{branch}\n"
    )

    def fake_git(repo_dir, *args):
        if args[:2] == ("worktree", "list"):
            return SimpleNamespace(returncode=0, stdout=list_out, stderr="")
        if args[:2] == ("status", "--porcelain"):
            return SimpleNamespace(returncode=0, stdout=status, stderr="")
        if args[:2] == ("rev-parse", "HEAD"):
            return SimpleNamespace(
                returncode=0, stdout=f"{local_sha}\n", stderr=""
            )
        if args[:2] == ("ls-remote", "--heads"):
            return SimpleNamespace(
                returncode=0,
                stdout=f"{remote_sha}\trefs/heads/{branch}\n",
                stderr="",
            )
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    monkeypatch.setattr(conductor, "_git", fake_git)
    monkeypatch.setattr(
        conductor,
        "_remove_worktree",
        lambda *a: (_ for _ in ()).throw(
            AssertionError("unpublished recovery worktree must be preserved")
        ),
    )

    assert conductor._reap_orphan_worktree(tmp_path, branch) is False
    assert reason in capsys.readouterr().out


def test_reap_guard_refuses_inherited_running_row(monkeypatch, tmp_path):
    # No marker (job dispatched by a pre-marker conductor), but the startup
    # inherit captured its running row → still protected.
    wt = tmp_path / "wt-i96-inherited"
    wt.mkdir()
    conductor._INHERITED_RUNNING[str(wt)] = os.getpid()
    monkeypatch.setattr(conductor, "_git", _fake_git_with_holder(wt))
    monkeypatch.setattr(
        conductor, "_pid_is_job_for", lambda pid, w, **_kw: True
    )

    def _boom(*a):
        raise AssertionError("reaped an inherited live worktree")

    monkeypatch.setattr(conductor, "_remove_worktree", _boom)
    assert conductor._reap_orphan_worktree(tmp_path, "marathon/refine-c12-i96") is False


def _job(**kw):
    base = dict(issue_num=7, chapter=15, target="Demo/Chapter15",
               worktree="/wt/x", workdir="/wd/x",
               branch="marathon/refine-c15-i7", pid=1, status="running")
    base.update(kw)
    return conductor.ConductorJob(**base)


def _write_update_marker(
    job, *, phase: str, current_head: str, base_sha: str | None = None
):
    marker = {
        "schema_version": conductor.UPDATE_RECOVERY_SCHEMA_VERSION,
        "phase": phase,
        "operation_id": job.expected_recovery_id,
        "github_repo": "owner/repo",
        "pr": job.recovery_pr,
        "branch": job.branch,
        "original_expected_head": job.recovery_head_sha,
        "base_branch": job.recovery_base or "main",
        "base_sha": base_sha or "c" * 40,
        "current_local_head": current_head,
        "target_path": str((Path(job.worktree) / job.target).resolve()),
        "had_conflicts": phase
        == conductor.UPDATE_RECOVERY_PHASE_FALLBACK_PREPARED,
    }
    path = Path(job.workdir) / conductor.UPDATE_RECOVERY_MARKER_FILENAME
    path.write_text(json.dumps(marker) + "\n")
    return marker


def test_adopt_inherited_jobs_filters_by_status_and_liveness(monkeypatch, tmp_path):
    import subprocess
    dead = subprocess.Popen(["true"])
    dead.wait()
    conductor.write_jobs_snapshot(tmp_path, [
        _job(issue_num=1, pid=os.getpid(), worktree="/wt/alive"),
        _job(issue_num=2, pid=dead.pid, worktree="/wt/dead"),
        _job(issue_num=3, pid=os.getpid(), worktree="/wt/done", status="succeeded"),
    ], 1)
    monkeypatch.setattr(
        conductor,
        "_pid_is_job_for",
        lambda pid, worktree, **_kw: (
            pid == os.getpid() and worktree == "/wt/alive"
        ),
    )
    adopted = conductor._adopt_inherited_jobs(tmp_path)
    # only the alive RUNNING job is adopted, as a tracked job with no proc handle
    assert [j.issue_num for j in adopted] == [1]
    assert adopted[0].proc is None and adopted[0].pid == os.getpid()
    # and the reap/prune guard map is populated from it
    assert conductor._INHERITED_RUNNING == {"/wt/alive": os.getpid()}


def test_adopt_inherited_jobs_rejects_live_unrelated_pid(monkeypatch, tmp_path):
    """A recycled live PID is not proof that the recorded worker survived."""
    conductor.write_jobs_snapshot(tmp_path, [
        _job(issue_num=9, pid=os.getpid(), worktree="/wt/reused"),
    ], 1)
    monkeypatch.setattr(
        conductor, "_pid_is_job_for", lambda *_, **__: False
    )
    monkeypatch.setattr(conductor, "_registered_worktrees", lambda *_: [])
    assert conductor._adopt_inherited_jobs(tmp_path) == []
    assert conductor._INHERITED_RUNNING == {}


def test_adopt_inherited_jobs_retains_ambiguous_live_snapshot(monkeypatch, tmp_path):
    """Uninspectable legacy workers remain protected instead of redispatched."""
    conductor.write_jobs_snapshot(tmp_path, [
        _job(issue_num=10, pid=1010, worktree="/wt/ambiguous"),
    ], 1)
    monkeypatch.setattr(
        conductor,
        "_job_process_group_live",
        lambda *a, strict_identity=False, **k: not strict_identity,
    )
    monkeypatch.setattr(conductor, "_registered_worktrees", lambda *_: [])

    adopted = conductor._adopt_inherited_jobs(tmp_path)

    assert [(j.issue_num, j.pid) for j in adopted] == [(10, 1010)]
    assert conductor._INHERITED_RUNNING == {"/wt/ambiguous": 1010}


def test_adopt_recovers_pre_snapshot_worker_from_launch_marker(monkeypatch, tmp_path):
    """The trampoline marker survives a parent crash before jobs.json."""
    wt = tmp_path / "wt-i42-crash-window"
    job = _job(
        issue_num=42,
        pid=4242,
        pgid=4242,
        worktree=str(wt),
        workdir=str(tmp_path / "wd-i42"),
        status="running",
    )
    conductor._write_worktree_pid_marker(wt, 4242, job=job)
    monkeypatch.setattr(
        conductor, "_registered_worktrees", lambda _repo: [str(wt)]
    )
    monkeypatch.setattr(
        conductor, "_job_process_group_live", lambda *_, **__: True
    )

    adopted = conductor._adopt_inherited_jobs(tmp_path)

    assert [(j.issue_num, j.pid, j.status) for j in adopted] == [
        (42, 4242, "running")
    ]
    assert conductor._INHERITED_RUNNING == {str(wt): 4242}
    pid, marker = conductor._read_worktree_job_marker(wt)
    assert pid == 4242 and marker is not None and marker["status"] == "running"


def test_replacement_conductor_acknowledges_ready_trampoline(monkeypatch, tmp_path):
    wt = tmp_path / "wt-i43-ready"
    job = _job(
        issue_num=43,
        pid=4343,
        pgid=4343,
        worktree=str(wt),
        workdir=str(tmp_path / "wd-i43"),
        status="ready",
        launch_nonce="ready-43",
    )
    conductor._write_worktree_pid_marker(wt, 4343, job=job)
    monkeypatch.setattr(
        conductor, "_registered_worktrees", lambda _repo: [str(wt)]
    )
    monkeypatch.setattr(
        conductor, "_job_process_group_live", lambda *_, **__: True
    )

    adopted = conductor._adopt_inherited_jobs(tmp_path)

    assert [(j.issue_num, j.status) for j in adopted] == [(43, "running")]
    _, marker = conductor._read_worktree_job_marker(wt)
    assert marker is not None and marker["status"] == "running"
    assert conductor._worker_ack_path(wt, 4343, "ready-43").is_file()


def test_dead_marker_only_job_is_recovered_for_completion_reconcile(
    monkeypatch, tmp_path,
):
    """Exit before jobs.json still reaches adopted completion, never branch reap."""
    wt = tmp_path / "wt-i44-finished-before-restart"
    job = _job(
        issue_num=44,
        pid=4444,
        pgid=4444,
        worktree=str(wt),
        workdir=str(tmp_path / "wd-i44"),
        status="running",
    )
    conductor._write_worktree_pid_marker(wt, 4444, job=job)
    monkeypatch.setattr(
        conductor, "_registered_worktrees", lambda _repo: [str(wt)]
    )
    monkeypatch.setattr(
        conductor, "_job_process_group_live", lambda *_, **__: False
    )

    adopted = conductor._adopt_inherited_jobs(tmp_path)
    reconciled = []
    monkeypatch.setattr(
        conductor,
        "_reap_adopted_job",
        lambda cfg, recovered, kept, **kw: reconciled.append(recovered.issue_num),
    )

    reaped = conductor._reap_finished(
        SimpleNamespace(repo_dir=tmp_path),
        adopted,
        {},
        {},
        3,
    )

    assert reaped == 1
    assert reconciled == [44]


def test_adoption_dedupes_canonical_worktree_alias(monkeypatch, tmp_path):
    wt = tmp_path / "wt-i52-real"
    wt.mkdir()
    alias = tmp_path / "wt-i52-alias"
    alias.symlink_to(wt, target_is_directory=True)
    job = _job(issue_num=52, pid=5252, pgid=5252, worktree=str(wt))
    conductor.write_jobs_snapshot(tmp_path, [job], 1)
    conductor._write_worktree_pid_marker(wt, 5252, job=job)
    monkeypatch.setattr(
        conductor, "_job_process_group_live", lambda *_, **__: True
    )
    monkeypatch.setattr(
        conductor, "_registered_worktrees", lambda _repo: [str(alias)]
    )

    adopted = conductor._adopt_inherited_jobs(tmp_path)

    assert [j.issue_num for j in adopted] == [52]


def test_fresh_launch_intent_blocks_reap_before_trampoline_handshake(
    monkeypatch, tmp_path,
):
    wt = tmp_path / "wt-i62-launching"
    job = _job(issue_num=62, pid=None, worktree=str(wt), status="launching")
    conductor._write_worktree_pid_marker(wt, 0, job=job)
    monkeypatch.setattr(
        conductor, "_job_process_group_live", lambda *_, **__: False
    )

    assert conductor._worktree_live_pid(str(wt)) == conductor.LAUNCHING_PID_SENTINEL


def test_surviving_worker_group_keeps_worktree_live(monkeypatch, tmp_path):
    wt = tmp_path / "wt-i63-descendant"
    job = _job(issue_num=63, pid=6300, pgid=6300, worktree=str(wt))
    conductor._write_worktree_pid_marker(wt, 6300, job=job)
    monkeypatch.setattr(
        conductor, "_pid_is_job_for", lambda *_, **__: False
    )
    monkeypatch.setattr(
        conductor, "_process_group_matches_worktree", lambda *a, **k: None
    )
    monkeypatch.setattr(conductor, "_process_group_alive", lambda pgid: pgid == 6300)

    assert conductor._worktree_live_pid(str(wt)) == 6300


def test_reap_adopted_job_with_pr_marks_iterated(monkeypatch, tmp_path):
    # An adopted refine that finished WITH a PR → record_iteration (so it isn't
    # re-dispatched) + worktree removed; automerge lands the PR separately.
    import marathon.conductor as c
    calls = {"iterated": [], "removed": []}
    monkeypatch.setattr(c, "_job_head_sha", lambda job: "new-head-sha")
    monkeypatch.setattr(c, "_branch_pr_exists", lambda *a, **k: True)
    monkeypatch.setattr(c, "record_iteration",
                        lambda cfg, n, **kw: calls["iterated"].append(n))
    monkeypatch.setattr(c, "_remove_worktree",
                        lambda repo, w: calls["removed"].append(str(w)))
    monkeypatch.setattr(c, "_regenerate_metadata_after_success", lambda cfg: None)
    job = _job(issue_num=42, worktree=str(tmp_path / "wt-i42"))
    c._reap_adopted_job(SimpleNamespace(repo_dir=tmp_path), job, {}, reconcile=True)
    assert job.status == "succeeded"
    assert calls["iterated"] == [42]
    assert calls["removed"] == [str(tmp_path / "wt-i42")]


def test_adopted_recovery_build_status_uses_exact_sha_and_latest_own_context(
    monkeypatch
):
    import json

    from marathon.review import github as github_mod
    from marathon.review.automerge import TRUSTED_BUILD_CONTEXT

    sha = "a" * 40

    def fake_gh(*args, check=True, timeout=None):
        assert args == ("api", f"repos/example/repo/commits/{sha}/statuses")
        return SimpleNamespace(
            returncode=0,
            stdout=json.dumps(
                [
                    {
                        "id": 1,
                        "context": TRUSTED_BUILD_CONTEXT,
                        "state": "success",
                        "created_at": "2026-07-14T10:00:00Z",
                    },
                    {
                        "id": 99,
                        "context": "unrelated/green",
                        "state": "success",
                        "created_at": "2026-07-14T12:00:00Z",
                    },
                    {
                        "id": 2,
                        "context": TRUSTED_BUILD_CONTEXT,
                        "state": "failure",
                        "created_at": "2026-07-14T11:00:00Z",
                    },
                ]
            ),
            stderr="",
        )

    monkeypatch.setattr(github_mod, "gh", fake_gh)

    assert conductor._adopted_recovery_build_status(
        SimpleNamespace(github_repo="example/repo"), sha
    ) == "FAIL"


@pytest.mark.parametrize(
    "recovery_mode",
    [
        conductor.RECOVERY_RETRY_BUILD,
        conductor.RECOVERY_UPDATE_CONTINUE,
    ],
)
def test_adopted_recovery_does_not_treat_preexisting_pr_as_completion(
    monkeypatch, tmp_path, recovery_mode
):
    """After restart, the old PR proves neither worker actually succeeded."""
    worktree = tmp_path / "wt-i42-recovery"
    workdir = tmp_path / "wd-i42-recovery"
    worktree.mkdir()
    workdir.mkdir()
    job = _job(
        issue_num=42,
        branch="marathon/refine-c15-i42",
        worktree=str(worktree),
        workdir=str(workdir),
        recovery_mode=recovery_mode,
        expected_recovery_id="recovery-42-current",
        recovery_pr=277,
        recovery_head_sha="a" * 40,
        recovery_base="main",
    )
    monkeypatch.setattr(conductor, "_job_head_sha", lambda job: "a" * 40)
    monkeypatch.setattr(
        conductor,
        "_adopted_recovery_build_status",
        lambda cfg, sha: None,
        raising=False,
    )
    # This PR predates the recovery operation and therefore is deliberately
    # not recovery-completion evidence.
    monkeypatch.setattr(conductor, "_branch_pr_exists", lambda *a, **k: True)
    iterated = []
    monkeypatch.setattr(
        conductor,
        "record_iteration",
        lambda *a, **k: iterated.append((a, k)),
    )
    removed = []
    monkeypatch.setattr(
        conductor,
        "_remove_worktree",
        lambda repo, path: removed.append(path),
    )

    kept = {}
    conductor._reap_adopted_job(
        SimpleNamespace(repo_dir=tmp_path), job, kept, reconcile=True
    )

    assert job.status == "failed"
    assert iterated == []
    assert removed == []
    assert kept == {42: str(worktree)}


def test_adopted_retry_build_green_status_and_exact_pr_complete(
    monkeypatch, tmp_path
):
    worktree = tmp_path / "wt-i42-build-green"
    workdir = tmp_path / "wd-i42-build-green"
    worktree.mkdir()
    workdir.mkdir()
    head = "a" * 40
    job = _job(
        issue_num=42,
        branch="marathon/refine-c15-i42",
        worktree=str(worktree),
        workdir=str(workdir),
        recovery_mode=conductor.RECOVERY_RETRY_BUILD,
        expected_recovery_id="recovery-42-current",
        recovery_pr=277,
        recovery_head_sha=head,
        recovery_base="main",
    )
    monkeypatch.setattr(conductor, "_job_head_sha", lambda job: head)
    queried = []
    monkeypatch.setattr(
        conductor,
        "_adopted_recovery_build_status",
        lambda cfg, sha: queried.append(sha) or "OK",
        raising=False,
    )
    pr_queries = []
    monkeypatch.setattr(
        conductor,
        "_branch_pr_exists",
        lambda cfg, branch, expected: pr_queries.append(
            (branch, expected)
        ) or True,
    )
    iterated = []
    monkeypatch.setattr(
        conductor,
        "record_iteration",
        lambda cfg, issue, **kwargs: iterated.append((issue, kwargs)),
    )
    removed = []
    monkeypatch.setattr(
        conductor,
        "_remove_worktree",
        lambda repo, path: removed.append(path),
    )
    monkeypatch.setattr(
        conductor, "_regenerate_metadata_after_success", lambda cfg: None
    )

    kept = {}
    conductor._reap_adopted_job(
        SimpleNamespace(repo_dir=tmp_path), job, kept, reconcile=True
    )

    assert queried == [head]
    assert pr_queries == [("marathon/refine-c15-i42", head)]
    assert job.status == "succeeded"
    assert iterated[0][0] == 42
    assert iterated[0][1]["expected_recovery_id"] == "recovery-42-current"
    assert iterated[0][1]["iteration_branch"] == "marathon/refine-c15-i42"
    assert iterated[0][1]["iteration_commit_sha"] == head
    assert removed == [worktree]
    assert kept == {}


def test_adopted_retry_build_red_status_stalls_and_cleans_detached_tree(
    monkeypatch, tmp_path
):
    worktree = tmp_path / "wt-i42-build-red"
    workdir = tmp_path / "wd-i42-build-red"
    worktree.mkdir()
    workdir.mkdir()
    head = "a" * 40
    job = _job(
        issue_num=42,
        branch="marathon/refine-c15-i42",
        worktree=str(worktree),
        workdir=str(workdir),
        recovery_mode=conductor.RECOVERY_RETRY_BUILD,
        expected_recovery_id="recovery-42-current",
        recovery_pr=277,
        recovery_head_sha=head,
        recovery_base="main",
    )
    monkeypatch.setattr(conductor, "_job_head_sha", lambda job: head)
    monkeypatch.setattr(
        conductor,
        "_adopted_recovery_build_status",
        lambda cfg, sha: "FAIL",
        raising=False,
    )
    monkeypatch.setattr(
        conductor,
        "_branch_pr_exists",
        lambda *a, **k: (_ for _ in ()).throw(
            AssertionError("red build evidence needs no pre-existing PR probe")
        ),
    )
    stalls = []
    monkeypatch.setattr(
        conductor,
        "record_stall",
        lambda cfg, issue, **kwargs: stalls.append((issue, kwargs))
        or SimpleNamespace(status="stalled"),
    )
    monkeypatch.setattr(
        conductor,
        "record_iteration",
        lambda *a, **k: (_ for _ in ()).throw(
            AssertionError("red recovery must not be marked iterated")
        ),
    )
    removed = []
    monkeypatch.setattr(
        conductor,
        "_remove_worktree",
        lambda repo, path: removed.append(path),
    )

    kept = {}
    conductor._reap_adopted_job(
        SimpleNamespace(repo_dir=tmp_path), job, kept, reconcile=True
    )

    assert job.status == "stalled"
    assert stalls[0][0] == 42
    assert stalls[0][1]["expected_recovery_id"] == "recovery-42-current"
    assert stalls[0][1].get("expected_verdict_ts") is None
    assert removed == [worktree]
    assert kept == {}


@pytest.mark.parametrize("status", ["PENDING", None])
def test_adopted_retry_build_ambiguous_status_preserves_for_recovery(
    monkeypatch, tmp_path, status
):
    worktree = tmp_path / "wt-i42-build-ambiguous"
    workdir = tmp_path / "wd-i42-build-ambiguous"
    worktree.mkdir()
    workdir.mkdir()
    head = "a" * 40
    job = _job(
        issue_num=42,
        branch="marathon/refine-c15-i42",
        worktree=str(worktree),
        workdir=str(workdir),
        recovery_mode=conductor.RECOVERY_RETRY_BUILD,
        expected_recovery_id="recovery-42-current",
        recovery_pr=277,
        recovery_head_sha=head,
        recovery_base="main",
    )
    monkeypatch.setattr(conductor, "_job_head_sha", lambda job: head)
    monkeypatch.setattr(
        conductor,
        "_adopted_recovery_build_status",
        lambda cfg, sha: status,
        raising=False,
    )
    monkeypatch.setattr(
        conductor,
        "_branch_pr_exists",
        lambda *a, **k: (_ for _ in ()).throw(
            AssertionError("ambiguous status must fail closed before PR probe")
        ),
    )
    monkeypatch.setattr(
        conductor,
        "record_iteration",
        lambda *a, **k: (_ for _ in ()).throw(
            AssertionError("ambiguous recovery must not be marked iterated")
        ),
    )
    removed = []
    monkeypatch.setattr(
        conductor,
        "_remove_worktree",
        lambda repo, path: removed.append(path),
    )

    kept = {}
    conductor._reap_adopted_job(
        SimpleNamespace(repo_dir=tmp_path), job, kept, reconcile=True
    )

    assert job.status == "failed"
    assert removed == []
    assert kept == {42: str(worktree)}


def test_adopted_update_fast_path_needs_green_current_head_and_exact_pr(
    monkeypatch, tmp_path
):
    worktree = tmp_path / "wt-i42-update-green"
    workdir = tmp_path / "wd-i42-update-green"
    worktree.mkdir()
    workdir.mkdir()
    original = "a" * 40
    updated = "b" * 40
    job = _job(
        issue_num=42,
        branch="marathon/refine-c15-i42",
        worktree=str(worktree),
        workdir=str(workdir),
        recovery_mode=conductor.RECOVERY_UPDATE_CONTINUE,
        expected_recovery_id="recovery-42-current",
        recovery_pr=277,
        recovery_head_sha=original,
        recovery_base="main",
    )
    _write_update_marker(
        job,
        phase=conductor.UPDATE_RECOVERY_PHASE_PUBLISHED,
        current_head=updated,
    )
    monkeypatch.setattr(conductor, "_job_head_sha", lambda job: updated)
    queried = []
    monkeypatch.setattr(
        conductor,
        "_adopted_recovery_build_status",
        lambda cfg, sha: queried.append(sha) or "OK",
        raising=False,
    )
    pr_queries = []
    monkeypatch.setattr(
        conductor,
        "_branch_pr_exists",
        lambda cfg, branch, expected: pr_queries.append(
            (branch, expected)
        ) or True,
    )
    iterated = []
    monkeypatch.setattr(
        conductor,
        "record_iteration",
        lambda cfg, issue, **kwargs: iterated.append((issue, kwargs)),
    )
    removed = []
    monkeypatch.setattr(
        conductor,
        "_remove_worktree",
        lambda repo, path: removed.append(path),
    )
    monkeypatch.setattr(
        conductor, "_regenerate_metadata_after_success", lambda cfg: None
    )

    kept = {}
    conductor._reap_adopted_job(
        SimpleNamespace(repo_dir=tmp_path), job, kept, reconcile=True
    )

    assert queried == [updated]
    assert pr_queries == [("marathon/refine-c15-i42", updated)]
    assert job.status == "succeeded"
    assert iterated[0][1]["expected_recovery_id"] == "recovery-42-current"
    assert iterated[0][1]["iteration_commit_sha"] == updated
    assert removed == [worktree]
    assert kept == {}


def test_adopted_update_starting_ignores_old_green_original_head(
    monkeypatch, tmp_path
):
    """A crash before merging cannot inherit an earlier retry-build success."""
    worktree = tmp_path / "wt-i42-update-starting"
    workdir = tmp_path / "wd-i42-update-starting"
    worktree.mkdir()
    workdir.mkdir()
    original = "a" * 40
    job = _job(
        issue_num=42,
        branch="marathon/refine-c15-i42",
        worktree=str(worktree),
        workdir=str(workdir),
        recovery_mode=conductor.RECOVERY_UPDATE_CONTINUE,
        expected_recovery_id="recovery-42-current",
        recovery_pr=277,
        recovery_head_sha=original,
        recovery_base="main",
    )
    _write_update_marker(job, phase="starting", current_head=original)
    monkeypatch.setattr(conductor, "_job_head_sha", lambda job: original)
    monkeypatch.setattr(
        conductor,
        "_adopted_recovery_build_status",
        lambda *a, **k: (_ for _ in ()).throw(
            AssertionError("starting phase must ignore an old green status")
        ),
    )
    monkeypatch.setattr(
        conductor,
        "_branch_pr_exists",
        lambda *a, **k: (_ for _ in ()).throw(
            AssertionError("starting phase must resume before PR probing")
        ),
    )
    monkeypatch.setattr(
        conductor,
        "record_iteration",
        lambda *a, **k: (_ for _ in ()).throw(
            AssertionError("starting phase is not a completed recovery")
        ),
    )
    monkeypatch.setattr(
        conductor,
        "_remove_worktree",
        lambda *a, **k: (_ for _ in ()).throw(
            AssertionError("starting phase worktree must be preserved")
        ),
    )

    kept = {}
    conductor._reap_adopted_job(
        SimpleNamespace(repo_dir=tmp_path), job, kept, reconcile=True
    )

    assert job.status == "failed"
    assert kept == {42: str(worktree)}


@pytest.mark.parametrize(
    ("recovery_mode", "local_head"),
    [
        (conductor.RECOVERY_RETRY_BUILD, "a" * 40),
        (conductor.RECOVERY_UPDATE_CONTINUE, "b" * 40),
    ],
)
def test_adopted_green_recovery_without_exact_head_pr_still_preserves(
    monkeypatch, tmp_path, recovery_mode, local_head
):
    worktree = tmp_path / "wt-i42-green-no-pr"
    workdir = tmp_path / "wd-i42-green-no-pr"
    worktree.mkdir()
    workdir.mkdir()
    job = _job(
        issue_num=42,
        branch="marathon/refine-c15-i42",
        worktree=str(worktree),
        workdir=str(workdir),
        recovery_mode=recovery_mode,
        expected_recovery_id="recovery-42-current",
        recovery_pr=277,
        recovery_head_sha="a" * 40,
        recovery_base="main",
    )
    monkeypatch.setattr(conductor, "_job_head_sha", lambda job: local_head)
    monkeypatch.setattr(
        conductor,
        "_adopted_recovery_build_status",
        lambda cfg, sha: "OK",
    )
    monkeypatch.setattr(conductor, "_branch_pr_exists", lambda *a, **k: False)
    monkeypatch.setattr(
        conductor,
        "record_iteration",
        lambda *a, **k: (_ for _ in ()).throw(
            AssertionError("green status alone cannot prove PR publication")
        ),
    )
    removed = []
    monkeypatch.setattr(
        conductor,
        "_remove_worktree",
        lambda repo, path: removed.append(path),
    )

    kept = {}
    conductor._reap_adopted_job(
        SimpleNamespace(repo_dir=tmp_path), job, kept, reconcile=True
    )

    assert job.status == "failed"
    assert removed == []
    assert kept == {42: str(worktree)}


@pytest.mark.parametrize("status", ["FAIL", "PENDING", None])
def test_adopted_update_fast_path_without_green_evidence_preserves(
    monkeypatch, tmp_path, status
):
    worktree = tmp_path / "wt-i42-update-ambiguous"
    workdir = tmp_path / "wd-i42-update-ambiguous"
    worktree.mkdir()
    workdir.mkdir()
    original = "a" * 40
    updated = "b" * 40
    job = _job(
        issue_num=42,
        branch="marathon/refine-c15-i42",
        worktree=str(worktree),
        workdir=str(workdir),
        recovery_mode=conductor.RECOVERY_UPDATE_CONTINUE,
        expected_recovery_id="recovery-42-current",
        recovery_pr=277,
        recovery_head_sha=original,
        recovery_base="main",
    )
    _write_update_marker(
        job,
        phase=conductor.UPDATE_RECOVERY_PHASE_PUBLISHED,
        current_head=updated,
    )
    monkeypatch.setattr(conductor, "_job_head_sha", lambda job: updated)
    monkeypatch.setattr(
        conductor,
        "_adopted_recovery_build_status",
        lambda cfg, sha: status,
        raising=False,
    )
    monkeypatch.setattr(
        conductor,
        "_branch_pr_exists",
        lambda *a, **k: (_ for _ in ()).throw(
            AssertionError("non-green update must fail closed before PR probe")
        ),
    )
    monkeypatch.setattr(
        conductor,
        "record_iteration",
        lambda *a, **k: (_ for _ in ()).throw(
            AssertionError("non-green update must not be marked iterated")
        ),
    )
    removed = []
    monkeypatch.setattr(
        conductor,
        "_remove_worktree",
        lambda repo, path: removed.append(path),
    )

    kept = {}
    conductor._reap_adopted_job(
        SimpleNamespace(repo_dir=tmp_path), job, kept, reconcile=True
    )

    assert job.status == "failed"
    assert removed == []
    assert kept == {42: str(worktree)}


def test_adopted_update_marker_preserves_prepared_fallback_before_pr_probe(
    monkeypatch, tmp_path
):
    """A durable update fallback checkpoint is evidence to resume, not finish."""
    wt = tmp_path / "wt-i42-update"
    wd = tmp_path / "wd-i42-update"
    wt.mkdir()
    wd.mkdir()
    save_refine_state(
        wd / REFINE_STATE_FILENAME,
        RefineState(
            target_folder="Demo/Chapter15",
            preserve_local_branch=True,
            continuation_commit_sha="b" * 40,
        ),
    )
    job = _job(
        issue_num=42,
        worktree=str(wt),
        workdir=str(wd),
        recovery_mode=conductor.RECOVERY_UPDATE_CONTINUE,
        expected_recovery_id="recovery-42-current",
        recovery_pr=277,
        recovery_head_sha="a" * 40,
        recovery_base="main",
    )
    _write_update_marker(
        job,
        phase=conductor.UPDATE_RECOVERY_PHASE_FALLBACK_PREPARED,
        current_head="b" * 40,
    )
    monkeypatch.setattr(
        conductor,
        "_branch_pr_exists",
        lambda *a, **k: (_ for _ in ()).throw(
            AssertionError("prepared fallback must be resumed before PR probing")
        ),
    )
    monkeypatch.setattr(
        conductor,
        "record_iteration",
        lambda *a, **k: (_ for _ in ()).throw(
            AssertionError("prepared fallback is not a completed recovery")
        ),
    )

    kept = {}
    conductor._reap_adopted_job(
        SimpleNamespace(repo_dir=tmp_path), job, kept, reconcile=True
    )

    assert job.status == "failed"
    assert kept == {42: str(wt)}


def test_adopted_completed_update_fallback_uses_checkpoint_and_exact_pr(
    monkeypatch, tmp_path
):
    wt = tmp_path / "wt-i42-update-complete"
    wd = tmp_path / "wd-i42-update-complete"
    wt.mkdir()
    wd.mkdir()
    updated = "b" * 40
    save_refine_state(
        wd / REFINE_STATE_FILENAME,
        RefineState(
            target_folder="Demo/Chapter15",
            current_iteration_idx=1,
            iterations_completed=1,
            status="COMPLETE",
        ),
    )
    job = _job(
        issue_num=42,
        branch="marathon/refine-c15-i42",
        worktree=str(wt),
        workdir=str(wd),
        recovery_mode=conductor.RECOVERY_UPDATE_CONTINUE,
        expected_recovery_id="recovery-42-current",
        recovery_pr=277,
        recovery_head_sha="a" * 40,
        recovery_base="main",
    )
    _write_update_marker(
        job,
        phase=conductor.UPDATE_RECOVERY_PHASE_FALLBACK_PREPARED,
        current_head=updated,
    )
    monkeypatch.setattr(conductor, "_job_head_sha", lambda job: updated)
    monkeypatch.setattr(
        conductor,
        "_adopted_recovery_build_status",
        lambda *a, **k: (_ for _ in ()).throw(
            AssertionError("completed fallback uses its refine checkpoint")
        ),
        raising=False,
    )
    monkeypatch.setattr(
        conductor, "_branch_pr_exists", lambda cfg, branch, head: head == updated
    )
    iterated = []
    monkeypatch.setattr(
        conductor,
        "record_iteration",
        lambda cfg, issue, **kwargs: iterated.append((issue, kwargs)),
    )
    removed = []
    monkeypatch.setattr(
        conductor,
        "_remove_worktree",
        lambda repo, path: removed.append(path),
    )
    monkeypatch.setattr(
        conductor, "_regenerate_metadata_after_success", lambda cfg: None
    )

    kept = {}
    conductor._reap_adopted_job(
        SimpleNamespace(repo_dir=tmp_path), job, kept, reconcile=True
    )

    assert job.status == "succeeded"
    assert iterated[0][1]["expected_recovery_id"] == "recovery-42-current"
    assert iterated[0][1]["iteration_commit_sha"] == updated
    assert removed == [wt]
    assert kept == {}


def test_reap_adopted_job_without_pr_requeues(monkeypatch, tmp_path):
    # An adopted refine that finished with NO PR → keep worktree + run the
    # no-PR requeue path; NEVER record_iteration (would wrongly consume it).
    import marathon.conductor as c
    calls = {"iterated": 0, "requeued": []}
    monkeypatch.setattr(c, "_job_head_sha", lambda job: "new-head-sha")
    monkeypatch.setattr(c, "_branch_pr_exists", lambda *a, **k: False)
    monkeypatch.setattr(c, "record_iteration",
                        lambda cfg, n: calls.__setitem__("iterated", calls["iterated"] + 1))
    monkeypatch.setattr(c, "_requeue_if_no_pr_landed",
                        lambda cfg, job: calls["requeued"].append(job.issue_num))
    kept: dict = {}
    wd = tmp_path / "wd-i42"
    wd.mkdir()
    save_refine_state(
        wd / REFINE_STATE_FILENAME,
        RefineState(
            target_folder="Demo/Chapter15",
            current_iteration_idx=1,
            iterations_completed=1,
            status="COMPLETE",
        ),
    )
    job = _job(
        issue_num=42,
        worktree=str(tmp_path / "wt-i42"),
        workdir=str(wd),
    )
    c._reap_adopted_job(SimpleNamespace(repo_dir=tmp_path), job, kept, reconcile=True)
    assert job.status == "failed"
    assert calls["iterated"] == 0
    assert calls["requeued"] == [42]
    assert kept == {42: str(tmp_path / "wt-i42")}


def test_adopted_fill_with_resumable_checkpoint_is_never_cleaned(
    monkeypatch, tmp_path,
):
    wt = tmp_path / "wt-i42-x"
    wd = tmp_path / "wd-i42-x"
    wt.mkdir()
    wd.mkdir()
    save_refine_state(
        wd / REFINE_STATE_FILENAME,
        RefineState(
            target_folder="Demo/Chapter15",
            force_continuation=True,
            status="POST_PIPELINE_PR_BLOCKED",
        ),
    )
    job = _job(issue_num=42, worktree=str(wt), workdir=str(wd), fill=True)
    monkeypatch.setattr(
        conductor,
        "_remove_worktree",
        lambda *a, **k: (_ for _ in ()).throw(
            AssertionError("resumable fill worktree must not be removed")
        ),
    )
    kept = {}
    conductor._reap_adopted_job(
        SimpleNamespace(repo_dir=tmp_path), job, kept, reconcile=True
    )
    assert job.status == "failed"
    assert kept == {conductor.FILL_KEY_BASE + 42: str(wt)}


def test_adopted_fill_generic_failure_without_completion_is_retained(
    monkeypatch, tmp_path,
):
    wt = tmp_path / "wt-i42-x"
    wd = tmp_path / "wd-i42-x"
    wt.mkdir()
    wd.mkdir()
    save_refine_state(
        wd / REFINE_STATE_FILENAME,
        RefineState(
            target_folder="Demo/Chapter15",
            current_iteration_idx=1,
            iterations_completed=0,
            status="SUBMIT_FAILED",
            project_id="p-existing",
        ),
    )
    job = _job(issue_num=42, worktree=str(wt), workdir=str(wd), fill=True)
    monkeypatch.setattr(conductor, "_branch_pr_exists", lambda *a, **k: False)
    monkeypatch.setattr(
        conductor,
        "_remove_worktree",
        lambda *a, **k: (_ for _ in ()).throw(
            AssertionError("generic failed fill must not be inferred successful")
        ),
    )
    kept = {}
    conductor._reap_adopted_job(
        SimpleNamespace(repo_dir=tmp_path), job, kept, reconcile=True
    )
    assert job.status == "failed"
    assert kept == {conductor.FILL_KEY_BASE + 42: str(wt)}


def test_adopted_fill_with_completed_iteration_can_be_cleaned(monkeypatch, tmp_path):
    wt = tmp_path / "wt-i42-x"
    wd = tmp_path / "wd-i42-x"
    wt.mkdir()
    wd.mkdir()
    save_refine_state(
        wd / REFINE_STATE_FILENAME,
        RefineState(
            target_folder="Demo/Chapter15",
            current_iteration_idx=1,
            iterations_completed=1,
            status="COMPLETE",
        ),
    )
    job = _job(issue_num=42, worktree=str(wt), workdir=str(wd), fill=True)
    removed = []
    monkeypatch.setattr(
        conductor, "_remove_worktree", lambda repo, path: removed.append(path)
    )
    kept = {}
    conductor._reap_adopted_job(
        SimpleNamespace(repo_dir=tmp_path), job, kept, reconcile=True
    )
    assert job.status == "succeeded"
    assert removed == [wt]
    assert kept == {}


def test_adopted_refine_pending_publication_is_not_no_pr_requeued(
    monkeypatch, tmp_path,
):
    wt = tmp_path / "wt-i42-x"
    wd = tmp_path / "wd-i42-x"
    wt.mkdir()
    wd.mkdir()
    save_refine_state(
        wd / REFINE_STATE_FILENAME,
        RefineState(
            target_folder="Demo/Chapter15",
            pending_publication={"kind": "pr", "commit_sha": "abc123"},
        ),
    )
    job = _job(issue_num=42, worktree=str(wt), workdir=str(wd))
    monkeypatch.setattr(
        conductor,
        "_branch_pr_exists",
        lambda *a, **k: (_ for _ in ()).throw(
            AssertionError("checkpoint must be inspected before no-PR query")
        ),
    )
    kept = {}
    conductor._reap_adopted_job(
        SimpleNamespace(repo_dir=tmp_path), job, kept, reconcile=True
    )
    assert job.status == "publication_pending"
    assert kept == {42: str(wt)}


def test_pid_marker_write_is_self_ignoring(tmp_path):
    wt = tmp_path / "wt"
    conductor._write_worktree_pid_marker(wt, 4242)
    marker = wt / conductor.WORKTREE_PID_MARKER
    assert marker.read_text().strip() == "4242"
    assert (marker.parent / ".gitignore").read_text() == "*\n"


# --- pid-reuse hardening: a live-but-UNRELATED pid must not wedge the reap ----

def test_reap_guard_ignores_reused_pid_not_running_this_worktree(monkeypatch, tmp_path):
    # Marker holds a LIVE pid (this test's own), but that process's cmdline
    # does NOT reference the worktree basename → it's a recycled pid, not our
    # job. The guard must allow the reap (else #96 defer-loop wedge returns).
    import os
    wt = tmp_path / "wt-i96-reused"
    conductor._write_worktree_pid_marker(wt, os.getpid())
    # This pytest process's cmdline won't contain "wt-i96-reused".
    monkeypatch.setattr(conductor, "_git", _fake_git_with_holder(wt))
    removed = []
    monkeypatch.setattr(conductor, "_remove_worktree",
                        lambda repo, w: removed.append(str(w)))
    assert conductor._reap_orphan_worktree(tmp_path, "marathon/refine-c12-i96") is True
    assert removed == [str(wt)]


def test_pid_is_job_for_requires_full_worktree_path(monkeypatch, tmp_path):
    import os
    wt = tmp_path / "wt-i7-20260101-000000"
    # Full path match → genuine job.
    monkeypatch.setattr(conductor.subprocess, "run",
                        lambda *a, **k: __import__("types").SimpleNamespace(
                            returncode=0, stdout=f"python -m marathon refine {wt}/x"))
    assert conductor._pid_is_job_for(os.getpid(), str(wt)) is True
    # Same basename in another repository must not match.
    monkeypatch.setattr(conductor.subprocess, "run",
                        lambda *a, **k: __import__("types").SimpleNamespace(
                            returncode=0,
                            stdout=f"python -m marathon refine /other/{wt.name}/x"))
    assert conductor._pid_is_job_for(os.getpid(), str(wt)) is False


def test_uninspectable_pid_protects_reap_but_is_not_adoptable(monkeypatch):
    monkeypatch.setattr(conductor.daemon, "process_alive", lambda _pid: True)
    monkeypatch.setattr(
        conductor.subprocess,
        "run",
        lambda *a, **k: (_ for _ in ()).throw(FileNotFoundError("no ps")),
    )

    assert conductor._pid_is_job_for(77, "/wt/job") is True
    assert conductor._pid_is_job_for(77, "/wt/job", strict=True) is False


def test_ambiguous_reused_group_protects_but_is_not_adoptable(monkeypatch):
    monkeypatch.setattr(conductor, "_pid_is_job_for", lambda *a, **k: False)
    monkeypatch.setattr(
        conductor, "_process_group_matches_worktree", lambda *a, **k: None
    )
    monkeypatch.setattr(conductor, "_process_group_alive", lambda _pgid: True)

    assert conductor._job_process_group_live(71, 71, "/wt/job") is True
    assert conductor._job_process_group_live(
        71, 71, "/wt/job", strict_identity=True
    ) is False
    assert conductor.snapshot_job_is_live({
        "status": "running", "pid": 71, "pgid": 71, "worktree": "/wt/job",
    }) is False


def test_snapshot_liveness_requires_worktree_identity(monkeypatch):
    monkeypatch.setattr(conductor.daemon, "process_alive", lambda _pid: True)

    # An old pid-only row cannot prove that a reused PID is Marathon's worker.
    assert conductor.snapshot_job_is_live({
        "status": "running", "pid": 71,
    }) is False


@pytest.mark.parametrize("action", ["retry-build", "update-continue"])
def test_snapshot_liveness_recognizes_recovery_workers(
    monkeypatch, action,
):
    monkeypatch.setattr(conductor.daemon, "process_alive", lambda _pid: True)
    monkeypatch.setattr(
        conductor.subprocess,
        "run",
        lambda *a, **k: SimpleNamespace(
            returncode=0,
            stdout=(
                "python -m marathon.recovery " + action
                + " --repo-dir /wt/job --target Demo/Chapter14"
            ),
        ),
    )

    assert conductor.snapshot_job_is_live({
        "status": "running", "pid": 71, "pgid": 71,
        "worktree": "/wt/job",
    }) is True


def test_clear_marker_removes_file_and_inherited_entry(tmp_path):
    wt = tmp_path / "wt-i9"
    conductor._write_worktree_pid_marker(wt, 111)
    conductor._INHERITED_RUNNING[str(wt)] = 111
    conductor._clear_worktree_pid_marker(str(wt))
    assert not (wt / conductor.WORKTREE_PID_MARKER).exists()
    assert str(wt) not in conductor._INHERITED_RUNNING


def test_prune_skips_live_worktree(monkeypatch, tmp_path):
    # A --prune must NOT remove a worktree with a live job (2026-07-07
    # destruction reachable through the prune door).
    import os
    runs = tmp_path / "runs"
    live = runs / "wt-i171-live"
    dead = runs / "wt-i99-dead"
    for d in (live, dead):
        d.mkdir(parents=True)
    conductor._write_worktree_pid_marker(live, os.getpid())
    # make the live pid identify as this worktree's job
    monkeypatch.setattr(
        conductor, "_pid_is_job_for",
        lambda pid, wt, **_kw: "wt-i171-live" in wt,
    )
    removed = []

    def fake_git(repo, *args):
        if args[:2] == ("worktree", "remove"):
            removed.append(args[-1])
        if args[:2] == ("branch", "--show-current"):
            return SimpleNamespace(
                returncode=0,
                stdout="marathon/refine-c12-i99\n",
                stderr="",
            )
        if args[:2] == ("status", "--porcelain"):
            return SimpleNamespace(returncode=0, stdout="", stderr="")
        if args[:2] == ("rev-parse", "HEAD"):
            return SimpleNamespace(returncode=0, stdout="abc123\n", stderr="")
        if args[:2] == ("ls-remote", "--heads"):
            return SimpleNamespace(
                returncode=0,
                stdout="abc123\trefs/heads/marathon/refine-c12-i99\n",
                stderr="",
            )
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    monkeypatch.setattr(conductor, "_git", fake_git)
    conductor.prune_worktrees(tmp_path, runs)
    assert str(dead) in removed
    assert str(live) not in removed          # protected


@pytest.mark.parametrize(
    ("status", "local_sha", "remote_sha"),
    [
        ("?? scratch.lean\n", "abc123", "abc123"),
        ("", "local-ahead", "published"),
    ],
)
def test_prune_preserves_dirty_and_locally_ahead_worktrees(
    monkeypatch, tmp_path, status, local_sha, remote_sha
):
    runs = tmp_path / "runs"
    worktree = runs / "wt-i171-recovery"
    worktree.mkdir(parents=True)
    branch = "marathon/refine-c15-i171"
    removed = []

    def fake_git(repo, *args):
        if args[:2] == ("branch", "--show-current"):
            return SimpleNamespace(returncode=0, stdout=f"{branch}\n", stderr="")
        if args[:2] == ("status", "--porcelain"):
            return SimpleNamespace(returncode=0, stdout=status, stderr="")
        if args[:2] == ("rev-parse", "HEAD"):
            return SimpleNamespace(
                returncode=0, stdout=f"{local_sha}\n", stderr=""
            )
        if args[:2] == ("ls-remote", "--heads"):
            return SimpleNamespace(
                returncode=0,
                stdout=f"{remote_sha}\trefs/heads/{branch}\n",
                stderr="",
            )
        if args[:2] == ("worktree", "remove"):
            removed.append(args[-1])
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    monkeypatch.setattr(conductor, "_git", fake_git)

    conductor.prune_worktrees(tmp_path, runs)

    assert removed == []
