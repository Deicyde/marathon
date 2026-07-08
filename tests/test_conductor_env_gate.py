"""The keyless-dispatch gate: a conductor with no ARISTOTLE_API_KEY must not
dispatch (every refine would instantly die), must say so loudly (degraded state
published in the jobs snapshot → deck banner), and the child's key check must
exit with the distinguished EXIT_CONFIG_ERROR so the exit ladder never burns
attempts on an operator problem (the 2026-07-07 fabricated-stall incident)."""
from __future__ import annotations

import pytest

import marathon.conductor as conductor
from marathon.skeleton import EXIT_CONFIG_ERROR, _ensure_api_key


def test_ensure_api_key_exits_with_config_error_code(monkeypatch, capsys):
    monkeypatch.delenv("ARISTOTLE_API_KEY", raising=False)
    with pytest.raises(SystemExit) as exc:
        _ensure_api_key()
    assert exc.value.code == EXIT_CONFIG_ERROR
    assert "ARISTOTLE_API_KEY" in capsys.readouterr().err


def test_dispatch_env_error_when_key_missing(monkeypatch):
    monkeypatch.delenv("ARISTOTLE_API_KEY", raising=False)
    reason = conductor._dispatch_env_error()
    assert reason is not None and "ARISTOTLE_API_KEY" in reason


def test_dispatch_env_ok_when_key_present(monkeypatch):
    monkeypatch.setenv("ARISTOTLE_API_KEY", "arstl_test")
    assert conductor._dispatch_env_error() is None


def test_degraded_round_trips_through_snapshot(tmp_path):
    conductor.write_jobs_snapshot(
        tmp_path, [], 1, degraded="ARISTOTLE_API_KEY is not set — ..."
    )
    snap = conductor.load_jobs_snapshot(tmp_path)
    assert snap["degraded"].startswith("ARISTOTLE_API_KEY")
    # and a healthy conductor publishes None
    conductor.write_jobs_snapshot(tmp_path, [], 1)
    assert conductor.load_jobs_snapshot(tmp_path)["degraded"] is None


def test_conductor_status_passes_degraded_through(tmp_path):
    from marathon.deck.dashboard import conductor_status

    from tests.test_conductor import make_cfg

    cfg = make_cfg(tmp_path)
    conductor.write_jobs_snapshot(
        cfg.repo_dir, [], 1, degraded="ARISTOTLE_API_KEY is not set — ..."
    )
    status = conductor_status(cfg)
    assert status["degraded"].startswith("ARISTOTLE_API_KEY")


def test_run_daemon_refuses_to_start_without_key(monkeypatch):
    # The standalone refine daemon would hot-loop on the attempt-free
    # config-error exit; it must refuse to start keyless instead.
    import marathon.review.daemon as daemon

    monkeypatch.delenv("ARISTOTLE_API_KEY", raising=False)
    called = {"acquire": False}
    monkeypatch.setattr(daemon, "acquire_lock",
                        lambda *a, **k: called.__setitem__("acquire", True) or True)
    monkeypatch.setattr(daemon, "load_config", lambda: object())
    rc = daemon.run_daemon(chapter=14, once=True)
    assert rc == 1
    assert called["acquire"] is False        # bailed BEFORE taking the lock


def test_job_error_field_round_trips(tmp_path):
    job = conductor.ConductorJob(
        issue_num=7, chapter=15, target="Demo/Chapter15",
        worktree="/wt", workdir="/wd", branch="marathon/refine-c15-i7",
        status="failed", exit_code=1, error="error: unknown identifier",
    )
    conductor.write_jobs_snapshot(tmp_path, [job], 1)
    raw = conductor.load_jobs_snapshot(tmp_path)["jobs"][0]
    assert raw["error"] == "error: unknown identifier"
    assert conductor.ConductorJob.from_json(raw).error == "error: unknown identifier"
