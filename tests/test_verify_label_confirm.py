"""cmd_verify's label write is CONFIRMED, not silent (the #23/#24 re-surfacing).

A verify that records the T2 ledger + flips the tracker but leaves the issue
UNLABELED makes the deck re-surface it forever. `_ensure_verified_label`
confirms the add landed, retries, and WARNs loudly instead of swallowing."""
from __future__ import annotations

import json
from types import SimpleNamespace

import marathon.review.review as review
from marathon.review.config import ReviewConfig, ReviewLabels


def _cfg(tmp_path):
    return ReviewConfig(
        repo_dir=tmp_path, config_path=tmp_path / "c.toml",
        github_repo="o/r", parent_issue=1, target_path_template="D/Ch{chapter}",
        tracker_section_pattern="### Chapter {chapter}:", labels=ReviewLabels(),
    )


def _fake_gh(state):
    def gh(*args, check=True, capture=True):
        a = list(args)
        if a[:2] == ["issue", "view"]:
            return SimpleNamespace(
                stdout=json.dumps({"labels": [{"name": n} for n in state["labels"]]}),
                returncode=0,
            )
        if a[:2] == ["issue", "edit"] and "--add-label" in a and state.get("add_ok", True):
            state["labels"].append(a[a.index("--add-label") + 1])
        return SimpleNamespace(stdout="", returncode=0)
    return gh


def test_confirms_label_landed(tmp_path, monkeypatch):
    state = {"add_ok": True, "labels": ["review"]}
    monkeypatch.setattr(review, "gh", _fake_gh(state))
    assert review._ensure_verified_label(_cfg(tmp_path), 23) is True
    assert "review:verified" in state["labels"]


def test_warns_loudly_when_label_never_lands(tmp_path, monkeypatch, capsys):
    state = {"add_ok": False, "labels": ["review"]}  # add silently no-ops
    monkeypatch.setattr(review, "gh", _fake_gh(state))
    assert review._ensure_verified_label(_cfg(tmp_path), 23) is False
    out = capsys.readouterr().out
    assert "could NOT confirm" in out and "gh issue edit 23" in out  # actionable, not silent
