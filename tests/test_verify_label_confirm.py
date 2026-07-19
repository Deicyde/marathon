"""cmd_verify's label write is CONFIRMED, not silent (the #23/#24 re-surfacing).

A verify that records the T2 ledger + flips the tracker but leaves the issue
UNLABELED makes the deck re-surface it forever. `_ensure_verified_label`
confirms the add landed, retries, and WARNs loudly instead of swallowing."""
from __future__ import annotations

import json
from types import SimpleNamespace
from urllib.parse import unquote

import marathon.review.github as github
import marathon.review.review as review
from marathon.review.config import ReviewConfig, ReviewLabels


def _cfg(tmp_path):
    return ReviewConfig(
        repo_dir=tmp_path, config_path=tmp_path / "c.toml",
        github_repo="o/r", parent_issue=1, target_path_template="D/Ch{chapter}",
        tracker_section_pattern="### Chapter {chapter}:", labels=ReviewLabels(),
    )


def _fake_rest_gh(state):
    calls = []

    def gh(*args, **kwargs):
        a = list(args)
        calls.append((a, dict(kwargs)))
        if a[:3] == ["api", "--method", "DELETE"]:
            label = unquote(a[3].rsplit("/", 1)[-1])
            if state.get("remove_ok", True):
                state["labels"] = [
                    name for name in state["labels"] if name != label
                ]
            return SimpleNamespace(stdout="", stderr="", returncode=0)
        if a[:3] == ["api", "--method", "POST"]:
            labels = json.loads(kwargs["input_text"])["labels"]
            if state.get("add_ok", True):
                for label in labels:
                    if label not in state["labels"]:
                        state["labels"].append(label)
            return SimpleNamespace(stdout="", stderr="", returncode=0)
        if a and a[0] == "api":
            return SimpleNamespace(
                stdout=json.dumps({"labels": [{"name": n} for n in state["labels"]]}),
                stderr="",
                returncode=0,
            )
        raise AssertionError(f"unexpected non-REST GitHub call: {a}")

    return gh, calls


def test_confirms_label_landed(tmp_path, monkeypatch):
    state = {
        "add_ok": True,
        "labels": ["review", "review:rejected"],
    }
    fake_gh, calls = _fake_rest_gh(state)
    monkeypatch.setattr(github, "gh", fake_gh)
    assert review._ensure_verified_label(_cfg(tmp_path), 23) is True
    assert "review:verified" in state["labels"]
    assert "review:rejected" not in state["labels"]
    assert all(call[0][0] == "api" for call in calls)
    assert all("graphql" not in call[0] for call in calls)
    post = next(call for call in calls if call[0][:3] == ["api", "--method", "POST"])
    assert json.loads(post[1]["input_text"]) == {"labels": ["review:verified"]}


def test_reject_convergence_removes_verified_label(tmp_path, monkeypatch):
    state = {"labels": ["review", "review:verified"]}
    fake_gh, _calls = _fake_rest_gh(state)
    monkeypatch.setattr(github, "gh", fake_gh)

    assert review._ensure_rejected_label(_cfg(tmp_path), 23) is True
    assert "review:rejected" in state["labels"]
    assert "review:verified" not in state["labels"]


def test_warns_loudly_when_label_never_lands(tmp_path, monkeypatch, capsys):
    state = {"add_ok": False, "labels": ["review"]}  # add silently no-ops
    fake_gh, _calls = _fake_rest_gh(state)
    monkeypatch.setattr(github, "gh", fake_gh)
    assert review._ensure_verified_label(_cfg(tmp_path), 23) is False
    out = capsys.readouterr().out
    assert "could NOT confirm" in out and "Retry Ship" in out


def test_rejected_must_be_absent_before_verify_is_confirmed(tmp_path, monkeypatch):
    state = {
        "add_ok": True,
        "remove_ok": False,
        "labels": ["review", "review:rejected"],
    }
    fake_gh, _calls = _fake_rest_gh(state)
    monkeypatch.setattr(github, "gh", fake_gh)

    assert review._ensure_verified_label(_cfg(tmp_path), 23) is False
    assert "review:verified" in state["labels"]
    assert "review:rejected" in state["labels"]


def test_malformed_rest_label_response_is_not_an_empty_label_set(monkeypatch):
    monkeypatch.setattr(
        github,
        "gh",
        lambda *a, **k: SimpleNamespace(returncode=0, stdout="{}", stderr=""),
    )
    assert github.issue_labels_rest(23, "o/r") is None


def test_cmd_verify_does_not_record_local_state_when_labels_split(
    tmp_path, monkeypatch
):
    import pytest

    cfg = _cfg(tmp_path)
    recorded = []
    comments = []
    monkeypatch.setattr(review, "load_config", lambda: cfg)
    monkeypatch.setattr(
        review,
        "post_issue_comment_rest",
        lambda *a, **k: comments.append((a, k)),
    )
    monkeypatch.setattr(review, "_ensure_verified_label", lambda _cfg, _num: False)
    monkeypatch.setattr(
        review, "record_verification",
        lambda _cfg, num: recorded.append(num),
    )

    with pytest.raises(RuntimeError, match="labels did not converge"):
        review.cmd_verify(SimpleNamespace(
            issue_num=23,
            close=False,
            comment=None,
            operation_id="saved-review-23",
        ))
    assert recorded == []
    assert comments[0][1] == {"operation_id": "saved-review-23"}
