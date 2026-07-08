"""MARATHON_NO_HOST_CONDUCTOR: a reject records state but spawns no conductor."""
from __future__ import annotations


import marathon.review.review as review


def test_no_host_conductor_skips_spawn(monkeypatch):
    monkeypatch.setenv("MARATHON_NO_HOST_CONDUCTOR", "1")

    def _boom(*a, **k):
        raise AssertionError("must not spawn a conductor when host-disabled")

    monkeypatch.setattr(review.subprocess, "Popen", _boom)
    # Must return BEFORE consulting the live-pid / spawning.
    review._ensure_conductor(cfg=object())


def test_host_conductor_not_disabled_uses_normal_path(monkeypatch):
    monkeypatch.delenv("MARATHON_NO_HOST_CONDUCTOR", raising=False)
    # Already-alive conductor → no-op note, no spawn (exercises the enabled path).
    monkeypatch.setattr(review, "_live_conductor_pid", lambda cfg: 4321)

    def _boom(*a, **k):
        raise AssertionError("should not spawn when one is already alive")

    monkeypatch.setattr(review.subprocess, "Popen", _boom)
    review._ensure_conductor(cfg=object())
