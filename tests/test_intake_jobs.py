"""Deck intake-job registry: background start → poll to ready/error, crash-safe,
no double-spawn while running."""
from __future__ import annotations

import threading
import time
from pathlib import Path

from marathon.deck.intake_jobs import IntakeRegistry
from marathon.review.chapter_intake import IntakeResult, ProposedSubIssue


def _wait(reg, chapter, want, timeout=3.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        s = reg.status(chapter)
        if s and s["status"] == want:
            return s
        time.sleep(0.01)
    raise AssertionError(f"#{chapter} never reached {want!r}: {reg.status(chapter)}")


def _good(cfg, chapter, *, informal_path=None, use_tracker=False):
    return IntakeResult(
        chapter, Path(f"/d/Chapter{chapter}.md"),
        [ProposedSubIssue(1, "T1", "body1")], "raw", "m",
    )


def _err(cfg, chapter, *, informal_path=None, use_tracker=False):
    return IntakeResult(chapter, Path("x"), [], "", "m", error="boom")


def _crash(cfg, chapter, *, informal_path=None, use_tracker=False):
    raise RuntimeError("kaboom")


def test_registry_runs_to_ready():
    reg = IntakeRegistry(runner=_good)
    snap = reg.start(None, 15)
    assert snap["status"] in ("running", "ready")
    s = _wait(reg, 15, "ready")
    assert s["subissues"][0]["title"] == "T1"
    assert reg.result(15).ok


def test_registry_surfaces_error():
    reg = IntakeRegistry(runner=_err)
    reg.start(None, 15)
    s = _wait(reg, 15, "error")
    assert s["error"] == "boom" and s["subissues"] == []


def test_registry_catches_runner_crash():
    reg = IntakeRegistry(runner=_crash)
    reg.start(None, 15)
    s = _wait(reg, 15, "error")
    assert "crashed" in (s["error"] or "")


def test_registry_status_none_when_unstarted():
    reg = IntakeRegistry(runner=_good)
    assert reg.status(99) is None
    assert reg.result(99) is None


def test_registry_result_none_while_running():
    ev = threading.Event()

    def slow(cfg, chapter, *, informal_path=None, use_tracker=False):
        ev.wait(2.0)
        return IntakeResult(chapter, Path("x"), [ProposedSubIssue(1, "t", "b")], "", "m")

    reg = IntakeRegistry(runner=slow)
    reg.start(None, 15)
    assert reg.result(15) is None  # still running → no result yet
    ev.set()
    _wait(reg, 15, "ready")
    assert reg.result(15) is not None


def test_registry_no_double_spawn_while_running():
    started: list[int] = []
    ev = threading.Event()

    def slow(cfg, chapter, *, informal_path=None, use_tracker=False):
        started.append(chapter)
        ev.wait(2.0)
        return IntakeResult(chapter, Path("x"), [ProposedSubIssue(1, "t", "b")], "", "m")

    reg = IntakeRegistry(runner=slow)
    reg.start(None, 15)
    snap2 = reg.start(None, 15)  # second call while the first runs
    assert snap2["status"] == "running"
    ev.set()
    _wait(reg, 15, "ready")
    assert started.count(15) == 1  # spawned exactly once
