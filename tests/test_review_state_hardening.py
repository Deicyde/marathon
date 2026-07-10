"""Concurrency and verdict-generation regressions for review/state.py."""
from __future__ import annotations

import json
import multiprocessing
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import marathon.review.state as state
import marathon.review.sync as sync
import pytest
from marathon.review.config import ReviewConfig, ReviewLabels


def _cfg(root: Path) -> ReviewConfig:
    return ReviewConfig(
        repo_dir=root,
        config_path=root / ".marathon/review/config.toml",
        github_repo="o/r",
        parent_issue=1,
        target_path_template="Demo/Chapter{chapter}",
        tracker_section_pattern="### Chapter {chapter}:",
        labels=ReviewLabels(),
        chapters={},
    )


def _process_reject(root: str, issue: int) -> None:
    state.record_rejection(_cfg(Path(root)), issue, f"note {issue}")


def test_threaded_writers_do_not_lose_distinct_issues(tmp_path):
    cfg = _cfg(tmp_path)
    with ThreadPoolExecutor(max_workers=12) as pool:
        list(pool.map(lambda n: state.record_rejection(cfg, n, f"note {n}"), range(24)))

    loaded = state.load_state(cfg)
    assert set(loaded.issues) == set(range(24))
    rows = [
        json.loads(line)
        for line in (tmp_path / state.VERDICTS_RELPATH).read_text().splitlines()
    ]
    assert {row["issue"] for row in rows} == set(range(24))


def test_process_writers_share_stable_lock_inode(tmp_path):
    cfg = _cfg(tmp_path)
    # ``spawn`` avoids forking pytest's already-multithreaded process and still
    # exercises genuinely independent interpreters/lock handles.
    ctx = multiprocessing.get_context("spawn")
    procs = [
        ctx.Process(target=_process_reject, args=(str(tmp_path), issue))
        for issue in range(8)
    ]
    for proc in procs:
        proc.start()
    for proc in procs:
        proc.join(15)
        assert proc.exitcode == 0

    assert set(state.load_state(cfg).issues) == set(range(8))
    assert state.state_lock_path(cfg).is_file()


def test_state_jsonl_and_ledger_mirrors_keep_one_total_order(tmp_path, monkeypatch):
    cfg = _cfg(tmp_path)
    first_inside_mirror = threading.Event()
    release_first = threading.Event()
    ledger_order: list[str] = []

    def fake_ledger(_cfg, _issue, entry, verdict_event=None):
        if verdict_event == "rejected":
            first_inside_mirror.set()
            assert release_first.wait(5)
        ledger_order.append(verdict_event or entry.status)

    monkeypatch.setattr(state, "_ledger_upsert", fake_ledger)
    reject = threading.Thread(
        target=state.record_rejection, args=(cfg, 7, "first")
    )
    verify = threading.Thread(target=state.record_verification, args=(cfg, 7))
    reject.start()
    assert first_inside_mirror.wait(5)
    verify.start()
    time.sleep(0.05)
    # Verification cannot replace state.json while the older rejection mirror
    # is blocked; otherwise the SQLite latest row could end up stale.
    assert verify.is_alive()
    release_first.set()
    reject.join(5)
    verify.join(5)

    assert ledger_order == ["rejected", "verified"]
    assert state.load_state(cfg).issues[7].status == "verified"
    rows = [
        json.loads(line)
        for line in (tmp_path / state.VERDICTS_RELPATH).read_text().splitlines()
    ]
    assert [row["verdict"] for row in rows] == ["rejected", "verified"]


def test_verdict_generation_is_strict_even_with_frozen_clock(tmp_path, monkeypatch):
    cfg = _cfg(tmp_path)
    monkeypatch.setattr(
        state, "_now_iso", lambda: "2026-07-10T12:00:00.000000+00:00"
    )
    first = state.record_rejection(cfg, 7, "first")
    second = state.record_rejection(cfg, 7, "second")
    assert second.verdict_ts != first.verdict_ts
    assert state.datetime.fromisoformat(second.verdict_ts) > state.datetime.fromisoformat(
        first.verdict_ts
    )


def test_stale_completion_after_verify_is_a_noop(tmp_path):
    cfg = _cfg(tmp_path)
    rejected = state.record_rejection(cfg, 7, "fix")
    verified = state.record_verification(cfg, 7)

    assert state.record_iteration(
        cfg, 7, expected_verdict_ts=rejected.verdict_ts
    ) is None
    assert state.record_failed_attempt(
        cfg, 7, expected_verdict_ts=rejected.verdict_ts
    ) is None
    assert state.record_stall(
        cfg, 7, expected_verdict_ts=rejected.verdict_ts
    ) is None
    assert state.load_state(cfg).issues[7] == verified


def test_stale_completion_cannot_consume_fresh_rereject(tmp_path):
    cfg = _cfg(tmp_path)
    old = state.record_rejection(cfg, 7, "old")
    fresh = state.record_rejection(cfg, 7, "fresh")

    assert state.record_iteration(
        cfg, 7, expected_verdict_ts=old.verdict_ts
    ) is None
    assert state.record_failed_attempt(
        cfg, 7, expected_verdict_ts=old.verdict_ts
    ) is None
    current = state.load_state(cfg).issues[7]
    assert current.verdict_ts == fresh.verdict_ts
    assert current.notes == "fresh" and current.attempts == 0
    assert current.needs_iteration()


def test_stale_unverify_cannot_delete_newer_verdict(tmp_path):
    cfg = _cfg(tmp_path)
    verified = state.record_verification(cfg, 7)
    fresh = state.record_rejection(cfg, 7, "new human reject")
    assert not state.requeue_for_rereview(
        cfg,
        7,
        expected_verdict_ts=verified.verdict_ts,
        expected_status="verified",
    )
    assert state.load_state(cfg).issues[7].verdict_ts == fresh.verdict_ts


def test_cas_requeue_cannot_overwrite_concurrent_verify(tmp_path):
    cfg = _cfg(tmp_path)
    parked = state.record_rejection(cfg, 7, "old note")
    state.record_iteration(
        cfg, 7, expected_verdict_ts=parked.verdict_ts
    )
    verified = state.record_verification(cfg, 7)

    assert state.record_rejection_if_current(
        cfg,
        7,
        "retry old note",
        expected_status="rejected",
        expected_verdict_ts=parked.verdict_ts,
    ) is None
    assert state.load_state(cfg).issues[7].verdict_ts == verified.verdict_ts


def test_absent_cas_reject_cannot_overwrite_concurrent_verify(tmp_path):
    cfg = _cfg(tmp_path)
    verified = state.record_verification(cfg, 7)
    assert state.record_rejection_if_absent(cfg, 7, "recovered note") is None
    assert state.load_state(cfg).issues[7].verdict_ts == verified.verdict_ts


def test_github_reconcile_participates_in_same_transaction(tmp_path):
    cfg = _cfg(tmp_path)
    with ThreadPoolExecutor(max_workers=8) as pool:
        list(
            pool.map(
                lambda n: sync.reconcile_github_rejection(
                    cfg,
                    n,
                    f"note {n}",
                    f"2026-07-10T12:00:{n:02d}Z",
                ),
                range(8),
            )
        )
    assert set(state.load_state(cfg).issues) == set(range(8))


def test_mutation_refuses_to_overwrite_corrupt_state(tmp_path):
    cfg = _cfg(tmp_path)
    path = state.state_path(cfg)
    path.parent.mkdir(parents=True)
    path.write_text("{ definitely not json")

    with pytest.raises(state.CorruptReviewStateError):
        state.record_rejection(cfg, 7, "must not erase other unknown entries")
    assert path.read_text() == "{ definitely not json"


def test_mutation_refuses_to_drop_one_malformed_issue_row(tmp_path):
    cfg = _cfg(tmp_path)
    path = state.state_path(cfg)
    path.parent.mkdir(parents=True)
    original = json.dumps(
        {
            "schema_version": 1,
            "issues": {
                "7": {
                    "status": "rejected",
                    "verdict_ts": "2026-07-10T12:00:00+00:00",
                    "notes": "valid",
                },
                "8": {"status": "rejected"},  # missing verdict_ts
            },
        }
    )
    path.write_text(original)

    with pytest.raises(state.CorruptReviewStateError):
        state.record_verification(cfg, 7)
    assert path.read_text() == original
