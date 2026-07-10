"""The conductor reaps an orphan worktree holding an issue's refine branch
(left by a failed refine of a previous run) instead of wedging on it —
UNLESS a live job from a previous conductor still uses it (the reap guard:
reaping under a running refine destroys a billed Aristotle run, the
2026-07-07 #171 incident)."""
from __future__ import annotations

import os
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
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    return fake_git


def test_reap_guard_refuses_live_pid_marker(monkeypatch, tmp_path):
    # The worktree carries a pid marker for a genuine LIVE job (identity
    # confirmed) → the guard must refuse and report nothing cleaned.
    wt = tmp_path / "wt-i96-live"
    conductor._write_worktree_pid_marker(wt, os.getpid())
    monkeypatch.setattr(conductor, "_git", _fake_git_with_holder(wt))
    monkeypatch.setattr(conductor, "_pid_is_job_for", lambda pid, w: True)

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


def test_reap_guard_refuses_inherited_running_row(monkeypatch, tmp_path):
    # No marker (job dispatched by a pre-marker conductor), but the startup
    # inherit captured its running row → still protected.
    wt = tmp_path / "wt-i96-inherited"
    wt.mkdir()
    conductor._INHERITED_RUNNING[str(wt)] = os.getpid()
    monkeypatch.setattr(conductor, "_git", _fake_git_with_holder(wt))
    monkeypatch.setattr(conductor, "_pid_is_job_for", lambda pid, w: True)

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


def test_adopt_inherited_jobs_filters_by_status_and_liveness(tmp_path):
    import subprocess
    dead = subprocess.Popen(["true"])
    dead.wait()
    conductor.write_jobs_snapshot(tmp_path, [
        _job(issue_num=1, pid=os.getpid(), worktree="/wt/alive"),
        _job(issue_num=2, pid=dead.pid, worktree="/wt/dead"),
        _job(issue_num=3, pid=os.getpid(), worktree="/wt/done", status="succeeded"),
    ], 1)
    adopted = conductor._adopt_inherited_jobs(tmp_path)
    # only the alive RUNNING job is adopted, as a tracked job with no proc handle
    assert [j.issue_num for j in adopted] == [1]
    assert adopted[0].proc is None and adopted[0].pid == os.getpid()
    # and the reap/prune guard map is populated from it
    assert conductor._INHERITED_RUNNING == {"/wt/alive": os.getpid()}


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


def test_pid_is_job_for_matches_on_worktree_basename(monkeypatch, tmp_path):
    import os
    wt = tmp_path / "wt-i7-20260101-000000"
    # our own pid, cmdline faked to include the basename → genuine job
    monkeypatch.setattr(conductor.subprocess, "run",
                        lambda *a, **k: __import__("types").SimpleNamespace(
                            returncode=0, stdout=f"python -m marathon refine {wt}/x"))
    assert conductor._pid_is_job_for(os.getpid(), str(wt)) is True
    # cmdline without the basename → reused pid
    monkeypatch.setattr(conductor.subprocess, "run",
                        lambda *a, **k: __import__("types").SimpleNamespace(
                            returncode=0, stdout="python -m something.else"))
    assert conductor._pid_is_job_for(os.getpid(), str(wt)) is False


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
    monkeypatch.setattr(conductor, "_pid_is_job_for",
                        lambda pid, wt: "wt-i171-live" in wt)
    removed = []

    def fake_git(repo, *args):
        if args[:2] == ("worktree", "remove"):
            removed.append(args[-1])
        from types import SimpleNamespace
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    monkeypatch.setattr(conductor, "_git", fake_git)
    conductor.prune_worktrees(tmp_path, runs)
    assert str(dead) in removed
    assert str(live) not in removed          # protected
