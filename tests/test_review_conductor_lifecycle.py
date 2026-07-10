"""The review refine-start/status/stop verbs must control one lifecycle.

refine-start was migrated to the repo-level conductor, but status/stop still
looked at retired per-chapter daemon locks.  These tests pin the global contract.
"""
from __future__ import annotations

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
