"""The async issue-verdict fast path (marathon.deck.verdict_jobs).

Same contract shape as the ship outbox: durable acceptance before any
worker starts, coalescing while one attempt is live, terminal records a
browser can poll, and an interrupted owner surfacing as awaiting_retry.
The runner is injected — no GitHub, no real verdict machinery.
"""
from __future__ import annotations

import threading

from marathon.deck.verdict_jobs import VerdictJobRegistry, _job_path

from tests.test_deck_subdeck import make_cfg


def _wait(predicate, timeout=5.0):
    deadline = threading.Event()
    for _ in range(int(timeout / 0.02)):
        if predicate():
            return True
        deadline.wait(0.02)
    return predicate()


def test_start_accepts_durably_and_completes(tmp_path):
    cfg = make_cfg(tmp_path)
    done = threading.Event()

    def runner(cfg_, issue, verdict, note):
        done.wait(5)
        return {"ok": True, "verdict": verdict, "issue_num": issue}

    reg = VerdictJobRegistry(runner=runner)
    record = reg.start(cfg, 41, "verify", None)
    assert record["status"] == "running"
    assert _job_path(cfg, 41).is_file()          # durable before completion
    assert reg.status(cfg, 41)["status"] == "running"
    done.set()
    assert _wait(lambda: reg.status(cfg, 41)["status"] == "succeeded")
    final = reg.status(cfg, 41)
    assert final["result"]["verdict"] == "verify"
    assert final["error"] is None


def test_live_attempt_coalesces_second_start(tmp_path):
    cfg = make_cfg(tmp_path)
    release = threading.Event()

    def runner(cfg_, issue, verdict, note):
        release.wait(5)
        return {"ok": True}

    reg = VerdictJobRegistry(runner=runner)
    first = reg.start(cfg, 42, "verify", None)
    second = reg.start(cfg, 42, "verify", None)
    assert second["attempt_id"] == first["attempt_id"]   # coalesced
    release.set()
    assert _wait(lambda: reg.status(cfg, 42)["status"] == "succeeded")


def test_worker_failure_is_a_terminal_failed_record(tmp_path):
    cfg = make_cfg(tmp_path)

    def runner(cfg_, issue, verdict, note):
        raise RuntimeError("gh exploded")

    reg = VerdictJobRegistry(runner=runner)
    reg.start(cfg, 43, "reject", "fix it")
    assert _wait(lambda: reg.status(cfg, 43)["status"] == "failed")
    assert "gh exploded" in reg.status(cfg, 43)["error"]


def test_dead_external_owner_reads_as_awaiting_retry(tmp_path):
    cfg = make_cfg(tmp_path)
    reg = VerdictJobRegistry(runner=lambda *a: {"ok": True})
    path = _job_path(cfg, 44)
    path.parent.mkdir(parents=True, exist_ok=True)
    import json

    path.write_text(json.dumps({
        "issue": 44, "verdict": "verify", "status": "running",
        "attempt_id": "x" * 32, "owner_pid": 2 ** 22 - 1,
        "accepted_at": "2026-08-27T00:00:00+00:00",
        "updated_at": "2026-08-27T00:00:00+00:00",
    }))
    assert reg.status(cfg, 44)["status"] == "awaiting_retry"
