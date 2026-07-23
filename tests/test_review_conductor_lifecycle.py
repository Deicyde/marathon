"""The review refine-start/status/stop verbs must control one lifecycle.

refine-start was migrated to the repo-level conductor, but status/stop still
looked at retired per-chapter daemon locks.  These tests pin the global contract.
"""
from __future__ import annotations

import subprocess
from types import SimpleNamespace

import marathon.conductor as conductor
import marathon.review.review as review
from tests.test_conductor import make_cfg


def test_refine_status_reads_global_conductor_and_filters_jobs(
    tmp_path, monkeypatch, capsys,
):
    cfg = make_cfg(tmp_path)
    monkeypatch.setattr(review, "load_config", lambda: cfg)
    monkeypatch.setattr(review, "_live_conductor_pid", lambda _cfg: 4242)
    monkeypatch.setattr(
        conductor,
        "load_jobs_snapshot",
        lambda _repo: {
            "jobs": [
                {"issue_num": 22, "chapter": 14, "status": "running",
                 "pid": 5001, "aristotle_status": "IN_PROGRESS"},
                {"issue_num": 31, "chapter": 15, "status": "running",
                 "pid": 5002, "aristotle_status": "QUEUED"},
            ]
        },
    )

    review.cmd_refine_status(SimpleNamespace(chapter=14))

    out = capsys.readouterr().out
    assert "global conductor active (pid 4242)" in out
    assert "#22 chapter 14" in out
    assert "#31" not in out
    assert "no refine daemon active" not in out


def test_refine_status_removes_stale_global_lock(tmp_path, monkeypatch, capsys):
    cfg = make_cfg(tmp_path)
    lock = conductor.conductor_lock_path(cfg)
    lock.parent.mkdir(parents=True, exist_ok=True)
    lock.write_text("999999\n")
    monkeypatch.setattr(review, "load_config", lambda: cfg)
    monkeypatch.setattr(review, "_live_conductor_pid", lambda _cfg: None)

    review.cmd_refine_status(SimpleNamespace(chapter=None))

    assert not lock.exists()
    assert "stale/unreadable" in capsys.readouterr().out


def test_refine_stop_signals_global_conductor_not_chapter_daemon(
    tmp_path, monkeypatch, capsys,
):
    cfg = make_cfg(tmp_path)
    monkeypatch.setattr(review, "load_config", lambda: cfg)
    monkeypatch.setattr(review, "_live_conductor_pid", lambda _cfg: 4242)
    signals = []
    monkeypatch.setattr(review.os, "kill", lambda pid, sig: signals.append((pid, sig)))

    review.cmd_refine_stop(SimpleNamespace(chapter=14))

    assert signals and signals[0][0] == 4242
    out = capsys.readouterr().out
    assert "global conductor: sent SIGTERM to pid 4242" in out
    assert "spans all chapters" in out
    assert "no Aristotle jobs are canceled" in out


def test_refine_start_and_stop_share_the_conductor_probe(tmp_path, monkeypatch):
    cfg = make_cfg(tmp_path)
    monkeypatch.setattr(review, "load_config", lambda: cfg)
    ensured = []
    monkeypatch.setattr(review, "_ensure_conductor", lambda c: ensured.append(c))
    review.cmd_refine_start(SimpleNamespace(chapter=14))
    assert ensured == [cfg]


def test_start_conductor_is_idempotent_when_global_conductor_is_live(
    tmp_path, monkeypatch,
):
    cfg = make_cfg(tmp_path)
    monkeypatch.setattr(review, "_live_conductor_pid", lambda _cfg: 4242)
    monkeypatch.setattr(review, "_pending_conductor_starts", {})
    monkeypatch.setattr(
        review.subprocess,
        "Popen",
        lambda *args, **kwargs: (_ for _ in ()).throw(
            AssertionError("an already-live conductor must not be spawned again")
        ),
    )

    result = review.start_conductor(cfg)

    assert result.pid == 4242
    assert result.started is False
    assert result.log_path is None


def test_start_conductor_spawns_detached_and_closes_parent_log_handle(
    tmp_path, monkeypatch,
):
    cfg = make_cfg(tmp_path)
    monkeypatch.setattr(review, "_live_conductor_pid", lambda _cfg: None)
    monkeypatch.setattr(review, "_pending_conductor_starts", {})
    captured = {}

    def popen(command, **kwargs):
        captured["command"] = command
        captured.update(kwargs)
        assert kwargs["stdout"].closed is False
        return SimpleNamespace(pid=5151)

    monkeypatch.setattr(review.subprocess, "Popen", popen)

    result = review.start_conductor(cfg)

    assert result.pid == 5151
    assert result.started is True
    assert result.log_path is not None and result.log_path.is_file()
    assert captured["command"] == [
        review.sys.executable,
        "-m",
        "marathon",
        "conductor",
        "run",
        "--repo-dir",
        str(cfg.repo_dir),
    ]
    assert captured["cwd"] == str(cfg.repo_dir)
    assert captured["stdin"] is subprocess.DEVNULL
    assert captured["stderr"] is subprocess.STDOUT
    assert captured["start_new_session"] is True
    assert captured["stdout"].closed is True


def test_explicit_start_ignores_automatic_no_host_policy(tmp_path, monkeypatch):
    """The deck opt-in is explicit authority even when reject auto-start is off."""
    cfg = make_cfg(tmp_path)
    monkeypatch.setenv("MARATHON_NO_HOST_CONDUCTOR", "1")
    monkeypatch.setattr(review, "_live_conductor_pid", lambda _cfg: None)
    monkeypatch.setattr(review, "_pending_conductor_starts", {})
    spawned = []

    def popen(command, **kwargs):
        spawned.append((command, kwargs))
        return SimpleNamespace(pid=6161)

    monkeypatch.setattr(review.subprocess, "Popen", popen)

    result = review.start_conductor(cfg)

    assert result.started is True
    assert result.pid == 6161
    assert len(spawned) == 1


def test_start_conductor_retries_after_child_exits_before_lock(
    tmp_path, monkeypatch,
):
    """An early-crashing child is reaped, not cached as a live zombie PID."""
    cfg = make_cfg(tmp_path)
    monkeypatch.setattr(review, "_pending_conductor_starts", {})

    class FakeProc:
        def __init__(self, pid, returncode):
            self.pid = pid
            self.returncode = returncode
            self.reaped = False

        def poll(self):
            if self.returncode is not None:
                self.reaped = True
            return self.returncode

    children = [FakeProc(7001, 1), FakeProc(7002, None)]
    launches = []

    def popen(command, **kwargs):
        launches.append(command)
        return children[len(launches) - 1]

    monkeypatch.setattr(review.subprocess, "Popen", popen)

    first = review.start_conductor(cfg)
    lock = conductor.conductor_lock_path(cfg)
    lock.parent.mkdir(parents=True, exist_ok=True)
    lock.write_text(str(first.pid))
    monkeypatch.setattr(
        review,
        "_process_alive",
        lambda pid: pid == first.pid and not children[0].reaped,
    )
    second = review.start_conductor(cfg)

    assert first.started is True and first.pid == 7001
    assert second.started is True and second.pid == 7002
    assert len(launches) == 2


def test_start_conductor_coalesces_clicks_while_child_is_starting(
    tmp_path, monkeypatch,
):
    cfg = make_cfg(tmp_path)
    monkeypatch.setattr(review, "_live_conductor_pid", lambda _cfg: None)
    monkeypatch.setattr(review, "_pending_conductor_starts", {})
    child = SimpleNamespace(pid=7101, poll=lambda: None)
    launches = []

    def popen(command, **kwargs):
        launches.append(command)
        return child

    monkeypatch.setattr(review.subprocess, "Popen", popen)

    first = review.start_conductor(cfg)
    second = review.start_conductor(cfg)

    assert first.started is True
    assert second.started is False and second.pid == first.pid == 7101
    assert len(launches) == 1
