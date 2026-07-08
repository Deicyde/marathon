"""Shared test fixtures.

The suite runs with a FAKE Aristotle key in the environment by default: the
conductor's keyless-dispatch gate (marathon.conductor._dispatch_env_error)
refuses to dispatch without ARISTOTLE_API_KEY, which would silently no-op
every end-to-end dispatch test. Tests that exercise the keyless/degraded
behavior itself (tests/test_conductor_env_gate.py) explicitly monkeypatch
the variable away, which overrides this autouse default within their scope.
No test ever talks to the real Aristotle API — dispatches are always
monkeypatched at the subprocess layer.
"""
from __future__ import annotations

import pytest


@pytest.fixture(autouse=True)
def _fake_aristotle_key(monkeypatch):
    monkeypatch.setenv("ARISTOTLE_API_KEY", "arstl_test_suite_fake")
