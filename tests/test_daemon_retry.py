"""Regression tests for the review daemon's failure handling.

Historical contract under test (the bug): a refine subprocess exiting
non-zero was marked "iterated" anyway, silently consuming the rejection
— the human had to notice the silence and re-reject (GeometricAnalysis
issue #49 accumulated 11 manual re-queue comments this way). The fixed
contract:

* clean exit (0)  → record_iteration, exactly as before (human verdict
  is the gate);
* non-zero exit   → attempt counter increments, NOTHING is consumed,
  same issue retried after exponential backoff;
* max attempts    → entry stalled (out of the dispatch queue) + ONE
  ``gh issue comment`` notification, which must never crash the daemon;
* re-reject       → counter resets, entry back in the normal flow;
* SIGTERM mid-run → nothing recorded at all (interrupted ≠ failed).

Tests drive both layers: the extracted decision table
(``_handle_refine_exit``) directly, and ``run_daemon`` end-to-end with
``run_one_refine`` monkeypatched out.
"""

from __future__ import annotations

import json
import signal
import subprocess

import pytest

import marathon.review.daemon as daemon
from marathon.review.config import ChapterRegistry, ReviewConfig, ReviewLabels
from marathon.review.state import (
    load_state,
    pending_rejections,
    pending_rejections_needing_iteration,
    record_failed_attempt,
    record_rejection,
    record_stall,
    state_path,
)

CHAPTER = 14
ISSUE = 22
REPO = "example/Demo"


def make_cfg(tmp_path) -> ReviewConfig:
    """Minimal in-memory ReviewConfig rooted at ``tmp_path``. Built
    directly (not via load_config) so no config.toml fixture file is
    needed; the chapter registry maps ISSUE so chapter-filtered queue
    queries work."""
    return ReviewConfig(
        repo_dir=tmp_path,
        config_path=tmp_path / ".marathon/review/config.toml",
        github_repo=REPO,
        parent_issue=1,
        referee_path=tmp_path / ".marathon/referee.md",
        target_path_template="Demo/Chapter{chapter}",
        tracker_section_pattern="### Chapter {chapter}:",
        labels=ReviewLabels(),
        chapters={
            CHAPTER: ChapterRegistry(
                chapter=CHAPTER, entries=[(ISSUE, "Lemma A"), (23, "Lemma B")]
            )
        },
    )


@pytest.fixture
def cfg(tmp_path):
    return make_cfg(tmp_path)


@pytest.fixture
def gh_calls(monkeypatch):
    """Capture ``subprocess.run`` invocations from the daemon module
    (only _notify_stall reaches subprocess in these tests — the refine
    dispatch itself is always monkeypatched out)."""
    calls: list[list[str]] = []

    def fake_run(cmd, **kwargs):
        calls.append(list(cmd))
        return subprocess.CompletedProcess(args=cmd, returncode=0, stdout="", stderr="")

    monkeypatch.setattr(daemon.subprocess, "run", fake_run)
    return calls


@pytest.fixture(autouse=True)
def _restore_signals_and_stop_flag(monkeypatch):
    """run_daemon installs SIGTERM/SIGINT handlers and the stop flag is
    module-global state; keep both from leaking across tests."""
    monkeypatch.setattr(daemon, "_STOP_REQUESTED", False)
    old_term = signal.getsignal(signal.SIGTERM)
    old_int = signal.getsignal(signal.SIGINT)
    yield
    signal.signal(signal.SIGTERM, old_term)
    signal.signal(signal.SIGINT, old_int)


# --- schema tolerance ---------------------------------------------------------


def test_legacy_state_file_without_attempts_loads_as_zero(cfg):
    """State files written before the retry feature lack `attempts`;
    they must load with attempts == 0, not error."""
    path = state_path(cfg)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "issues": {
                    str(ISSUE): {
                        "status": "rejected",
                        "verdict_ts": "2026-05-15T20:52:04-04:00",
                        "notes": "wrong lemma statement",
                    }
                },
            }
        )
    )
    state = load_state(cfg)
    assert state.issues[ISSUE].attempts == 0
    assert state.issues[ISSUE].needs_iteration()


def test_attempts_omitted_from_json_when_zero(cfg):
    """Round-trip of a never-failed entry must not grow an `attempts`
    key (absent-means-default, like notes/last_iteration_ts)."""
    record_rejection(cfg, ISSUE, "notes")
    raw = json.loads(state_path(cfg).read_text())
    assert "attempts" not in raw["issues"][str(ISSUE)]
    record_failed_attempt(cfg, ISSUE)
    raw = json.loads(state_path(cfg).read_text())
    assert raw["issues"][str(ISSUE)]["attempts"] == 1


# --- decision table: _handle_refine_exit --------------------------------------


def test_clean_exit_records_iteration_no_backoff(cfg):
    record_rejection(cfg, ISSUE, "notes")
    backoff = daemon._handle_refine_exit(
        cfg, ISSUE, 0, max_attempts=3, interrupted=False
    )
    assert backoff == 0
    entry = load_state(cfg).issues[ISSUE]
    assert entry.status == "rejected"
    assert entry.last_iteration_ts is not None  # consumed: human verdict next
    assert entry.attempts == 0
    assert not entry.needs_iteration()


def test_failed_exit_increments_attempts_and_stays_queued(cfg):
    """First and second failures: rejection NOT consumed, backoff grows
    exponentially, issue stays at the head of the dispatch queue."""
    record_rejection(cfg, ISSUE, "notes")

    backoff1 = daemon._handle_refine_exit(
        cfg, ISSUE, 1, max_attempts=3, interrupted=False
    )
    assert backoff1 == 2 * daemon.BACKOFF_BASE_SECONDS  # 2^1 * base
    entry = load_state(cfg).issues[ISSUE]
    assert entry.attempts == 1
    assert entry.status == "rejected"
    assert entry.last_iteration_ts is None  # NOT marked iterated
    assert [n for n, _ in pending_rejections_needing_iteration(cfg, CHAPTER)] == [ISSUE]

    backoff2 = daemon._handle_refine_exit(
        cfg, ISSUE, 1, max_attempts=3, interrupted=False
    )
    assert backoff2 == 4 * daemon.BACKOFF_BASE_SECONDS  # 2^2 * base
    assert load_state(cfg).issues[ISSUE].attempts == 2


def test_backoff_is_capped():
    assert daemon._backoff_seconds(1) == 120
    assert daemon._backoff_seconds(100) == daemon.BACKOFF_CAP_SECONDS


def test_max_attempts_stalls_and_notifies_once(cfg, gh_calls):
    record_rejection(cfg, ISSUE, "notes")
    for _ in range(2):
        daemon._handle_refine_exit(cfg, ISSUE, 1, max_attempts=3, interrupted=False)
    assert gh_calls == []  # not stalled yet — no noise

    backoff = daemon._handle_refine_exit(
        cfg, ISSUE, 1, max_attempts=3, interrupted=False
    )
    assert backoff == 0  # move on to other queued rejections immediately

    entry = load_state(cfg).issues[ISSUE]
    assert entry.status == "stalled"
    assert entry.attempts == 3
    assert entry.notes == "notes"  # kept for the post-mortem
    # Out of the dispatch queue AND out of the prompt-context query.
    assert pending_rejections_needing_iteration(cfg, CHAPTER) == []
    assert pending_rejections(cfg, CHAPTER) == []

    # Exactly one notification, via `gh issue comment N --repo <slug>`.
    assert len(gh_calls) == 1
    cmd = gh_calls[0]
    assert cmd[:4] == ["gh", "issue", "comment", str(ISSUE)]
    assert REPO == cmd[cmd.index("--repo") + 1]
    body = cmd[cmd.index("--body") + 1]
    assert "3" in body and "stalled" in body.lower()
    assert "re-reject" in body.lower()  # tells the human how to re-queue


def test_notification_failure_does_not_crash(cfg, monkeypatch):
    """gh missing / network down: the stall must still be recorded and
    the daemon must not die — the comment is best-effort."""
    record_rejection(cfg, ISSUE, "notes")

    def boom(cmd, **kwargs):
        raise FileNotFoundError("gh not on PATH")

    monkeypatch.setattr(daemon.subprocess, "run", boom)
    backoff = daemon._handle_refine_exit(
        cfg, ISSUE, 1, max_attempts=1, interrupted=False
    )
    assert backoff == 0
    assert load_state(cfg).issues[ISSUE].status == "stalled"


def test_interrupted_records_nothing(cfg):
    """SIGTERM mid-refine: interrupted ≠ failed. No iteration, no
    attempt — the next daemon launch re-dispatches the same issue."""
    record_rejection(cfg, ISSUE, "notes")
    backoff = daemon._handle_refine_exit(
        cfg, ISSUE, 143, max_attempts=3, interrupted=True
    )
    assert backoff == 0
    entry = load_state(cfg).issues[ISSUE]
    assert entry.attempts == 0
    assert entry.last_iteration_ts is None
    assert entry.needs_iteration()


# --- re-reject resets ----------------------------------------------------------


def test_rereject_resets_counter_and_unstalls(cfg, gh_calls):
    record_rejection(cfg, ISSUE, "first verdict")
    record_failed_attempt(cfg, ISSUE)
    record_stall(cfg, ISSUE)
    assert pending_rejections_needing_iteration(cfg, CHAPTER) == []

    # The human re-rejects (what `marathon review reject` calls):
    # counter resets, status back to the normal pending flow.
    record_rejection(cfg, ISSUE, "second verdict")
    entry = load_state(cfg).issues[ISSUE]
    assert entry.status == "rejected"
    assert entry.attempts == 0
    assert entry.notes == "second verdict"
    assert [n for n, _ in pending_rejections_needing_iteration(cfg, CHAPTER)] == [ISSUE]


# --- daemon loop end-to-end -----------------------------------------------------


def test_run_daemon_retries_then_stalls(cfg, gh_calls, monkeypatch):
    """Three consecutive refine failures in --once mode: the daemon
    re-dispatches the SAME issue with growing backoffs, then stalls it,
    notifies once, and exits with a drained queue."""
    record_rejection(cfg, ISSUE, "notes")
    monkeypatch.setattr(daemon, "load_config", lambda: cfg)

    dispatched: list[int] = []

    def failing_refine(cfg_, chapter, focus_issue):
        dispatched.append(focus_issue)
        return 1

    monkeypatch.setattr(daemon, "run_one_refine", failing_refine)

    sleeps: list[int] = []
    monkeypatch.setattr(daemon, "_interruptible_sleep", sleeps.append)

    assert daemon.run_daemon(chapter=CHAPTER, once=True, max_attempts=3) == 0

    assert dispatched == [ISSUE, ISSUE, ISSUE]  # retried, not consumed
    assert sleeps == [120, 240]  # 2^1*60, 2^2*60; none after the stall
    assert load_state(cfg).issues[ISSUE].status == "stalled"
    assert len(gh_calls) == 1  # exactly one notification
    assert pending_rejections_needing_iteration(cfg, CHAPTER) == []


def test_run_daemon_clean_exit_keeps_legacy_behavior(cfg, gh_calls, monkeypatch):
    """Exit-0 path unchanged: one dispatch, record_iteration, queue
    drained, no attempts, no notification."""
    record_rejection(cfg, ISSUE, "notes")
    monkeypatch.setattr(daemon, "load_config", lambda: cfg)
    monkeypatch.setattr(daemon, "run_one_refine", lambda cfg_, ch, focus_issue: 0)

    assert daemon.run_daemon(chapter=CHAPTER, once=True) == 0

    entry = load_state(cfg).issues[ISSUE]
    assert entry.status == "rejected"
    assert entry.last_iteration_ts is not None
    assert entry.attempts == 0
    assert gh_calls == []


def test_run_daemon_sigterm_mid_refine_records_nothing(cfg, gh_calls, monkeypatch):
    """A stop signal arriving while refine runs (simulated by the fake
    refine flipping the module flag, as the real handler would) must
    leave the entry untouched so the next launch re-dispatches it."""
    record_rejection(cfg, ISSUE, "notes")
    monkeypatch.setattr(daemon, "load_config", lambda: cfg)

    def interrupted_refine(cfg_, chapter, focus_issue):
        daemon._STOP_REQUESTED = True  # what _handle_stop_signal does
        return 143  # SIGTERM'd child

    monkeypatch.setattr(daemon, "run_one_refine", interrupted_refine)

    assert daemon.run_daemon(chapter=CHAPTER, once=False) == 0  # loop exits on flag

    entry = load_state(cfg).issues[ISSUE]
    assert entry.attempts == 0
    assert entry.last_iteration_ts is None
    assert entry.needs_iteration()  # next daemon launch picks it up
    assert [n for n, _ in pending_rejections_needing_iteration(cfg, CHAPTER)] == [ISSUE]
    assert gh_calls == []
