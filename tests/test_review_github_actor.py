"""Per-request GitHub identity: actor_github_token binds GH_TOKEN for gh()."""
from __future__ import annotations

import hashlib
import json
from types import SimpleNamespace

import marathon.review.github as github


def _capture_env(monkeypatch):
    seen = {}

    def fake_run(cmd, **kw):
        seen["cmd"] = list(cmd)
        seen["env"] = kw.get("env")
        seen["input"] = kw.get("input")
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
        github.post_issue_comment_rest(1, "o/r", "review body")
    assert seen["env"]["GH_TOKEN"] == "ghu_collab"


def test_rest_comment_body_is_json_stdin_and_absent_from_argv(monkeypatch):
    seen = _capture_env(monkeypatch)
    body = "FIX Foo: preserve α ≥ 0\n" + ("long private note " * 40)

    github.post_issue_comment_rest(171, "pitmonticone/GeometricAnalysis", body)

    assert seen["cmd"] == [
        "gh", "api", "--method", "POST",
        "repos/pitmonticone/GeometricAnalysis/issues/171/comments",
        "--input", "-",
    ]
    assert body not in " ".join(seen["cmd"])
    assert json.loads(seen["input"]) == {"body": body}


def test_rest_comment_operation_marker_suppresses_retry(monkeypatch):
    operation_id = "saved-review-generation-171"
    digest = hashlib.sha256(operation_id.encode("utf-8")).hexdigest()
    marker = f"<!-- marathon-verdict-operation:{digest} -->"
    calls = []

    def fake_gh(*args, **kwargs):
        calls.append((args, kwargs))
        assert "--method" not in args  # lookup only; retry must not POST
        return SimpleNamespace(
            returncode=0,
            stdout=json.dumps([[{"body": f"prior comment\n{marker}"}]]),
            stderr="",
        )

    monkeypatch.setattr(github, "gh", fake_gh)
    assert github.post_issue_comment_rest(
        171, "o/r", "review body", operation_id=operation_id
    ) is False
    assert len(calls) == 1


def test_rest_comment_operation_marker_is_added_on_first_post(monkeypatch):
    calls = []

    def fake_gh(*args, **kwargs):
        calls.append((args, kwargs))
        if "--method" not in args:
            return SimpleNamespace(returncode=0, stdout="[[]]", stderr="")
        return SimpleNamespace(returncode=0, stdout="{}", stderr="")

    monkeypatch.setattr(github, "gh", fake_gh)
    assert github.post_issue_comment_rest(
        171, "o/r", "review body", operation_id="generation-a"
    ) is True

    assert len(calls) == 2
    post_args, post_kwargs = calls[1]
    assert post_args[:3] == ("api", "--method", "POST")
    posted_body = json.loads(post_kwargs["input_text"])["body"]
    assert posted_body.startswith("review body\n\n")
    assert "<!-- marathon-verdict-operation:" in posted_body


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
