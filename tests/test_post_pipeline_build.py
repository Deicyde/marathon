"""Process-lifecycle regressions for the post-extraction Lake build."""

from __future__ import annotations

import signal
import subprocess

import pytest

import marathon.post_pipeline as post_pipeline


class _FinishedBuild:
    pid = 41001
    returncode = 0

    def communicate(self, timeout=None):
        return "Build completed successfully.\n", ""


def test_run_lake_build_starts_isolated_group_and_logs_status(
    monkeypatch, tmp_path, capsys,
):
    spawned: dict[str, object] = {}

    def fake_popen(command, **kwargs):
        spawned["command"] = command
        spawned["kwargs"] = kwargs
        return _FinishedBuild()

    monkeypatch.setattr(post_pipeline.shutil, "which", lambda _name: "/bin/lake")
    monkeypatch.setattr(post_pipeline.subprocess, "Popen", fake_popen)

    result = post_pipeline.run_lake_build(
        tmp_path,
        timeout=17,
        targets=["Demo.One", "Demo.Two"],
    )

    assert spawned["command"] == [
        "/bin/lake", "build", "Demo.One", "Demo.Two"
    ]
    kwargs = spawned["kwargs"]
    assert isinstance(kwargs, dict)
    assert kwargs["start_new_session"] is True
    assert kwargs["stdout"] is subprocess.PIPE
    assert kwargs["stderr"] is subprocess.PIPE
    assert result.ok is True
    assert result.log_tail == (
        "TAIL (warnings + other, sorry-noise stripped):\n"
        "Build completed successfully."
    )
    status = capsys.readouterr().out
    assert "build: starting lake build" in status
    assert "2 explicit target(s): Demo.One, Demo.Two" in status
    assert "timeout 17s" in status


def test_run_lake_build_finds_elan_shim_when_service_path_omits_it(
    monkeypatch, tmp_path,
):
    home = tmp_path / "home"
    lake = home / ".elan" / "bin" / "lake"
    lake.parent.mkdir(parents=True)
    lake.write_text("#!/bin/sh\n")
    lake.chmod(0o755)
    spawned: dict[str, object] = {}

    monkeypatch.setattr(post_pipeline.shutil, "which", lambda _name: None)
    monkeypatch.setattr(
        post_pipeline.Path, "home", classmethod(lambda cls: home)
    )
    monkeypatch.setattr(
        post_pipeline.subprocess,
        "Popen",
        lambda command, **kwargs: (
            spawned.update(command=command, kwargs=kwargs) or _FinishedBuild()
        ),
    )

    result = post_pipeline.run_lake_build(tmp_path, timeout=17)

    assert result.ok is True
    assert spawned["command"] == [str(lake), "build"]


class _StubbornBuild:
    pid = 41002
    returncode = None

    def __init__(self):
        self.communicate_timeouts: list[float | None] = []

    def communicate(self, timeout=None):
        self.communicate_timeouts.append(timeout)
        if len(self.communicate_timeouts) <= 2:
            raise subprocess.TimeoutExpired(["lake", "build"], timeout)
        self.returncode = -signal.SIGKILL
        return "partial stdout", "partial stderr"


def test_run_lake_build_timeout_terminates_then_kills_entire_group(
    monkeypatch, tmp_path,
):
    build = _StubbornBuild()
    signals: list[tuple[int, signal.Signals]] = []
    monkeypatch.setattr(post_pipeline.shutil, "which", lambda _name: "/bin/lake")
    monkeypatch.setattr(post_pipeline.subprocess, "Popen", lambda *a, **k: build)
    monkeypatch.setattr(
        post_pipeline.os,
        "killpg",
        lambda pgid, sig: signals.append((pgid, sig)),
    )

    result = post_pipeline.run_lake_build(tmp_path, timeout=23)

    assert result.ok is False
    assert result.timed_out is True
    assert result.log_tail == "timed out after 23s"
    assert signals == [
        (build.pid, signal.SIGTERM),
        (build.pid, signal.SIGKILL),
    ]
    assert build.communicate_timeouts == [
        23,
        post_pipeline._BUILD_TERMINATION_GRACE_SECONDS,
        None,
    ]


class _InterruptedBuild:
    pid = 41003
    returncode = None

    def __init__(self):
        self.calls = 0

    def communicate(self, timeout=None):
        self.calls += 1
        if self.calls == 1:
            raise KeyboardInterrupt
        self.returncode = -signal.SIGTERM
        return "", ""


def test_run_lake_build_interrupt_terminates_and_reaps_entire_group(
    monkeypatch, tmp_path,
):
    build = _InterruptedBuild()
    signals: list[tuple[int, signal.Signals]] = []
    monkeypatch.setattr(post_pipeline.shutil, "which", lambda _name: "/bin/lake")
    monkeypatch.setattr(post_pipeline.subprocess, "Popen", lambda *a, **k: build)
    monkeypatch.setattr(
        post_pipeline.os,
        "killpg",
        lambda pgid, sig: signals.append((pgid, sig)),
    )

    with pytest.raises(KeyboardInterrupt):
        post_pipeline.run_lake_build(tmp_path, timeout=23)

    assert signals == [(build.pid, signal.SIGTERM)]
    assert build.calls == 2
