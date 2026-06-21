"""Reject-note delivery: a focused refine runs in a worktree checked out from
origin/main, whose state.json lacks the just-written reject note. The loader must
fall back to GitHub (the durable source) so the "send the note to Aristotle
verbatim" bypass fires — otherwise the iteration silently falls to Claude-in-loop
and refactors the target (the #96 failure: deleted IsSmooth.*, no SmoothPullback)."""
from __future__ import annotations

import marathon.refine as refine
import marathon.review.config as cfgmod
import marathon.review.github as ghmod
import marathon.review.state as statemod


class _FakeCfg:
    chapters: dict = {}
    github_repo = "owner/repo"

    def target_path(self, chapter):  # never matched (chapters empty) → chapter=None
        return None


def _repo(tmp_path):
    repo = tmp_path / "repo"
    (repo / ".marathon" / "review").mkdir(parents=True)
    (repo / ".marathon" / "review" / "config.toml").write_text("")  # is_file() gate
    return repo


def _patch(monkeypatch, *, local, github):
    monkeypatch.setattr(cfgmod, "load_config", lambda repo_dir=None: _FakeCfg())
    monkeypatch.setattr(statemod, "render_pending_rejections_md",
                        lambda *a, **k: local)
    calls = []

    def fake_fetch(issue_num, repo):  # REAL sig: (issue_num, repo) -> (note, ts)
        assert isinstance(issue_num, int) and isinstance(repo, str)  # guard the sig
        calls.append(issue_num)
        return (github, "2026-06-21T00:00:00Z")

    monkeypatch.setattr(ghmod, "fetch_reject_note", fake_fetch)
    return calls


def test_github_fallback_when_local_state_lacks_the_note(tmp_path, monkeypatch):
    calls = _patch(monkeypatch, local=None,
                   github="Add a SmoothPullback; keep the IsSmooth.* lemmas.")
    md = refine._load_pending_rejections_md(
        _repo(tmp_path), tmp_path / "Chapter12", focus_issue=96
    )
    assert md is not None
    assert "SmoothPullback" in md
    assert "Review #96 REJECTED" in md  # wrapped into the rendered-block shape
    assert calls == [96]                # GitHub was consulted for #96


def test_local_note_preferred_no_github_call(tmp_path, monkeypatch):
    calls = _patch(monkeypatch, local="- **Review #96 REJECTED**\n\nlocal note",
                   github="SHOULD NOT BE USED")
    md = refine._load_pending_rejections_md(
        _repo(tmp_path), tmp_path / "Chapter12", focus_issue=96
    )
    assert md == "- **Review #96 REJECTED**\n\nlocal note"
    assert calls == []  # GitHub never consulted when the local state has the note


def test_no_focus_issue_never_falls_back(tmp_path, monkeypatch):
    calls = _patch(monkeypatch, local=None, github="x")
    md = refine._load_pending_rejections_md(
        _repo(tmp_path), tmp_path / "Chapter12", focus_issue=None
    )
    assert md is None
    assert calls == []  # project-wide load: no single-issue GitHub fallback


def test_fallback_returns_none_when_github_also_empty(tmp_path, monkeypatch):
    _patch(monkeypatch, local=None, github=None)
    md = refine._load_pending_rejections_md(
        _repo(tmp_path), tmp_path / "Chapter12", focus_issue=96
    )
    assert md is None  # no note anywhere → None (bypass correctly won't fire)
