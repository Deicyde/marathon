"""GitHub-label-driven conductor queue — the consumer half of the
--queue-from-github feature: a collaborator's conductor discovers the rejection
queue (and notes) from GitHub, seeding local state.json, reusing all the local
iteration-tracking + dispatch machinery."""
from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import marathon.conductor as conductor
import marathon.review.github as github
import marathon.review.state as state
import marathon.review.sync as sync
from marathon.review.config import ChapterRegistry, ReviewConfig, ReviewLabels
from marathon.review.reject_note import embed_reject_note


def make_cfg(tmp_path: Path) -> ReviewConfig:
    return ReviewConfig(
        repo_dir=tmp_path,
        config_path=tmp_path / ".marathon" / "review" / "config.toml",
        github_repo="someone/SomeProject",
        parent_issue=1,
        target_path_template="SomeProject/Chapter{chapter}",
        tracker_section_pattern="### Chapter {chapter}:",
        labels=ReviewLabels(),
        chapters={14: ChapterRegistry(chapter=14, entries=[(7, "Lemma 14.7")])},
    )


# --- reconcile_github_rejection ---------------------------------------------

def test_reconcile_seeds_when_absent(tmp_path):
    cfg = make_cfg(tmp_path)
    sync.reconcile_github_rejection(cfg, 7, "FIX A: do X", "2026-06-20T03:00:00Z")
    st = state.load_state(cfg).issues[7]
    assert st.status == "rejected" and st.notes == "FIX A: do X"
    assert st.last_iteration_ts is None and st.needs_iteration()
    # verdict_ts is the GitHub reject instant, stored in local-offset form
    assert sync._parse_ts(st.verdict_ts) == sync._parse_ts("2026-06-20T03:00:00Z")


def test_reconcile_skips_when_already_iterated(tmp_path):
    cfg = make_cfg(tmp_path)
    state.record_rejection(cfg, 7, "orig note")
    state.record_iteration(cfg, 7)                          # iterated since verdict
    before = state.load_state(cfg).issues[7]
    # an OLDER GitHub reject must not disturb our iteration progress
    sync.reconcile_github_rejection(cfg, 7, "new note", "2000-01-01T00:00:00Z")
    after = state.load_state(cfg).issues[7]
    assert after.last_iteration_ts == before.last_iteration_ts
    assert not after.needs_iteration() and after.notes == "orig note"


def test_reconcile_requeues_on_strictly_newer_reject(tmp_path):
    cfg = make_cfg(tmp_path)
    state.record_rejection(cfg, 7, "orig")
    state.record_iteration(cfg, 7)
    # a strictly newer GitHub reject (host re-rejected) → re-seed + re-queue once
    sync.reconcile_github_rejection(cfg, 7, "fresh note", "2999-01-01T00:00:00Z")
    st = state.load_state(cfg).issues[7]
    assert st.needs_iteration() and st.last_iteration_ts is None
    assert st.notes == "fresh note"
    assert sync._parse_ts(st.verdict_ts) == sync._parse_ts("2999-01-01T00:00:00Z")


def test_reconcile_keeps_local_stall(tmp_path):
    cfg = make_cfg(tmp_path)
    s = state.load_state(cfg)
    s.issues[7] = state.IssueState(
        status="stalled", verdict_ts="2999-01-01T00:00:00Z",
        notes="x", last_iteration_ts=None, attempts=9)
    state.save_state(cfg, s)
    # an OLDER GitHub reject must NOT un-stall (mirrors sync's ACTION_KEEP)
    sync.reconcile_github_rejection(cfg, 7, "note", "2000-01-01T00:00:00Z")
    assert state.load_state(cfg).issues[7].status == "stalled"


def test_reconcile_parks_when_no_note(tmp_path):
    cfg = make_cfg(tmp_path)
    sync.reconcile_github_rejection(cfg, 7, None, "2026-06-20T03:00:00Z")
    st = state.load_state(cfg).issues[7]
    assert st.status == "rejected" and not st.needs_iteration()   # parked
    assert st.last_iteration_ts is not None


def test_ts_strictly_newer_handles_utc_vs_offset():
    # 07:00-04:00 == 11:00Z
    assert sync._ts_strictly_newer("2026-06-20T12:00:00Z", "2026-06-20T07:00:00-04:00")
    assert not sync._ts_strictly_newer("2026-06-20T10:00:00Z", "2026-06-20T07:00:00-04:00")
    assert not sync._ts_strictly_newer(None, "2026-06-20T00:00:00Z")
    assert not sync._ts_strictly_newer("not-a-ts", "2026-06-20T00:00:00Z")


# --- github discovery helpers ------------------------------------------------

def test_list_rejected_issues_parses(monkeypatch):
    cfg = SimpleNamespace(github_repo="o/r",
                          labels=SimpleNamespace(rejected="review:rejected"))
    monkeypatch.setattr(github, "gh", lambda *a, **k: SimpleNamespace(
        returncode=0, stdout=json.dumps([{"number": 7}, {"number": 9}]), stderr=""))
    assert github.list_rejected_issues(cfg) == [7, 9]


def test_list_rejected_issues_none_on_failure(monkeypatch):
    cfg = SimpleNamespace(github_repo="o/r",
                          labels=SimpleNamespace(rejected="review:rejected"))
    monkeypatch.setattr(github, "gh",
                        lambda *a, **k: SimpleNamespace(returncode=1, stdout="", stderr="x"))
    assert github.list_rejected_issues(cfg) is None


def test_fetch_reject_note_picks_latest_marked(monkeypatch):
    comments = [
        {"body": "❌ REJECTED — generic", "createdAt": "2026-06-20T01:00:00Z"},
        {"body": "x\n\n" + embed_reject_note("FIX A: do X"),
         "createdAt": "2026-06-20T02:00:00Z"},
    ]
    monkeypatch.setattr(github, "issue_view_json",
                        lambda n, repo, fields, **k: {"comments": comments})
    assert github.fetch_reject_note(7, "o/r") == ("FIX A: do X", "2026-06-20T02:00:00Z")


def test_fetch_reject_note_recovers_ts_without_marker(monkeypatch):
    comments = [{"body": "❌ REJECTED — generic", "createdAt": "2026-06-20T01:00:00Z"}]
    monkeypatch.setattr(github, "issue_view_json",
                        lambda n, repo, fields, **k: {"comments": comments})
    note, ts = github.fetch_reject_note(7, "o/r")
    assert note is None and ts == "2026-06-20T01:00:00Z"


# --- conductor queue source + fail-soft + throttle ---------------------------

def test_eligible_issues_uses_github_source_when_flagged(tmp_path, monkeypatch):
    cfg = make_cfg(tmp_path)
    monkeypatch.setattr(conductor, "pending_rejections_needing_iteration",
                        lambda c, ch: [])                   # local queue empty
    seeded = [(7, state.IssueState(status="rejected",
                                   verdict_ts="2026-06-20T03:00:00Z", notes="n"))]
    monkeypatch.setattr(conductor, "_github_discovered_rejections", lambda c: seeded)
    assert conductor._eligible_issues(cfg, set(), queue_from_github=False) == []
    assert conductor._eligible_issues(cfg, set(), queue_from_github=True) == [(7, 14)]


def test_github_discovery_fail_soft_falls_back_to_local(tmp_path, monkeypatch):
    cfg = make_cfg(tmp_path)
    conductor._last_gh_discovery = 0.0                      # open the throttle window
    monkeypatch.setattr(github, "list_rejected_issues", lambda c: None)  # gh failed
    monkeypatch.setattr(conductor, "pending_rejections_needing_iteration",
                        lambda c, ch: [(7, "X")])
    assert conductor._github_discovered_rejections(cfg) == [(7, "X")]


def test_github_discovery_is_throttled(tmp_path, monkeypatch):
    cfg = make_cfg(tmp_path)
    conductor._last_gh_discovery = 0.0
    calls = {"n": 0}

    def _list(c):
        calls["n"] += 1
        return []

    monkeypatch.setattr(github, "list_rejected_issues", _list)
    monkeypatch.setattr(conductor, "pending_rejections_needing_iteration",
                        lambda c, ch: [])
    conductor._github_discovered_rejections(cfg)            # discovers
    conductor._github_discovered_rejections(cfg)            # within window → skip gh
    assert calls["n"] == 1


def test_reconcile_future_ts_does_not_loop_after_iteration(tmp_path):
    """The named worst case: a GitHub reject timestamped AHEAD of the local clock
    must NOT re-dispatch every tick. After one iteration needs_iteration is False,
    and re-reconciling the same reject does not reset it (no re-seed loop)."""
    cfg = make_cfg(tmp_path)
    future = "2999-01-01T00:00:00Z"
    sync.reconcile_github_rejection(cfg, 7, "FIX A: do X", future)
    assert state.load_state(cfg).issues[7].needs_iteration()       # 1st: dispatch
    state.record_iteration(cfg, 7)                                 # collaborator refines
    assert not state.load_state(cfg).issues[7].needs_iteration()   # done — parked
    sync.reconcile_github_rejection(cfg, 7, "FIX A: do X", future)  # same reject, next tick
    assert not state.load_state(cfg).issues[7].needs_iteration()   # still parked, no loop


def test_list_rejected_issues_none_on_gh_missing(monkeypatch):
    cfg = SimpleNamespace(github_repo="o/r",
                          labels=SimpleNamespace(rejected="review:rejected"))

    def _boom(*a, **k):
        raise FileNotFoundError("gh not installed")

    monkeypatch.setattr(github, "gh", _boom)
    assert github.list_rejected_issues(cfg) is None                # OSError → None
