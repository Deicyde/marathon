"""Per-request GitHub identity: actor_github_token binds GH_TOKEN for gh()."""
from __future__ import annotations

from types import SimpleNamespace

import marathon.review.github as github


def _capture_env(monkeypatch):
    seen = {}

    def fake_run(cmd, **kw):
        seen["env"] = kw.get("env")
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    monkeypatch.setattr(github.subprocess, "run", fake_run)
    return seen


def test_gh_uses_ambient_auth_by_default(monkeypatch):
    seen = _capture_env(monkeypatch)
    github.gh("issue", "view", "1", check=False)
    assert seen["env"] is None                 # ambient gh auth, no env override


def test_actor_token_injects_gh_token(monkeypatch):
    seen = _capture_env(monkeypatch)
    with github.actor_github_token("ghu_collab"):
        github.gh("issue", "comment", "1", check=False)
    assert seen["env"]["GH_TOKEN"] == "ghu_collab"


def test_blank_actor_token_is_noop(monkeypatch):
    seen = _capture_env(monkeypatch)
    with github.actor_github_token("   "):
        github.gh("issue", "view", "1", check=False)
    assert seen["env"] is None                 # blank → ambient, not GH_TOKEN=""


def test_actor_token_unbinds_after_context(monkeypatch):
    seen = _capture_env(monkeypatch)
    with github.actor_github_token("ghu_x"):
        pass
    github.gh("issue", "view", "1", check=False)
    assert seen["env"] is None                 # does not leak past the context
