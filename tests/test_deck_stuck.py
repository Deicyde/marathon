"""The "Stalled — needs re-reject" surface (deck/dashboard.stuck_issues): issues
parked OUTSIDE both the swipe queue and the refine queue — status 'stalled', or
'rejected' with no pending iteration (already iterated, no fix landed) — so a
human can re-queue them from the deck instead of dropping to the CLI."""
from __future__ import annotations

import json

import pytest

from marathon.deck.dashboard import stuck_issues
from marathon.review import github as gh_mod
from marathon.review.state import (
    record_iteration, record_rejection, record_stall,
)

from tests.test_conductor import make_cfg


@pytest.fixture(autouse=True)
def _offline_gh(monkeypatch):
    """stuck_issues now consults GitHub (list_rejected_issues) to recover
    orphaned rejections. Keep the state-based unit tests OFFLINE by default;
    the orphan tests re-patch with an explicit rejected set."""
    monkeypatch.setattr(gh_mod, "list_rejected_issues", lambda cfg: None)


def test_stuck_issues_lists_stalled_and_parked_only(tmp_path):
    cfg = make_cfg(tmp_path)
    # 10: stalled (conductor exhausted its retry budget) → STUCK
    record_rejection(cfg, 10, "fix Prop_15_21 with a bundled structure")
    record_stall(cfg, 10)
    # 11: rejected then iterated, no fix landed (phantom-parked) → STUCK
    record_rejection(cfg, 11, "fix the other thing")
    record_iteration(cfg, 11)            # last_iteration_ts ≥ verdict_ts
    # 12: rejected, not yet iterated (live in the refine queue) → NOT stuck
    record_rejection(cfg, 12, "fix more")

    by = {r["num"]: r for r in stuck_issues(cfg)}
    assert set(by) == {10, 11}                       # 12 (active) excluded
    assert by[10]["status"] == "stalled" and by[10]["has_note"]
    assert by[11]["status"] == "rejected" and by[11]["has_note"]
    assert "Prop_15_21" in by[10]["note_preview"]
    assert by[10]["attempts"] is not None


def test_stuck_issues_empty_when_nothing_parked(tmp_path):
    cfg = make_cfg(tmp_path)
    record_rejection(cfg, 12, "active rejection")    # needs_iteration True
    assert stuck_issues(cfg) == []


def test_stuck_issues_note_preview_truncated(tmp_path):
    cfg = make_cfg(tmp_path)
    record_rejection(cfg, 10, "x" * 500)
    record_stall(cfg, 10)
    prev = stuck_issues(cfg)[0]["note_preview"]
    assert len(prev) <= 241 and prev.endswith("…")   # 240 + ellipsis


# --- orphaned rejections: rejected on GitHub but state.json was reverted -----

def test_stuck_issues_surfaces_orphaned_rejections(tmp_path, monkeypatch):
    cfg = make_cfg(tmp_path)
    # 12 has a live (active) state entry — must NOT be re-listed as an orphan
    # even though GitHub still labels it rejected.
    record_rejection(cfg, 12, "active")
    monkeypatch.setattr(gh_mod, "list_rejected_issues", lambda cfg: [171, 238, 12])

    by = {r["num"]: r for r in stuck_issues(cfg)}
    assert set(by) == {171, 238}                      # 12 (has state) excluded
    assert by[171]["orphaned"] and by[171]["status"] == "rejected"
    assert by[171]["has_note"]                        # recoverable from GitHub
    assert by[238]["orphaned"]


def test_stuck_issues_orphan_preview_from_local_subdecks(tmp_path, monkeypatch):
    cfg = make_cfg(tmp_path)
    # deck-subdecks.json is untracked, so it survives the reset that wiped
    # state.json; a reject verdict there gives a local (no-gh) preview.
    store = cfg.repo_dir / ".marathon" / "review" / "deck-subdecks.json"
    store.parent.mkdir(parents=True, exist_ok=True)
    store.write_text(json.dumps(
        {"171": {"foo": {"verdict": "reject"}, "bar": {"verdict": "verify"}}}
    ))
    monkeypatch.setattr(gh_mod, "list_rejected_issues", lambda cfg: [171])

    row = {r["num"]: r for r in stuck_issues(cfg)}[171]
    assert "foo" in row["note_preview"] and "1 declaration" in row["note_preview"]


def test_stuck_issues_orphan_preview_skips_reserved_keys(tmp_path, monkeypatch):
    # The reserved __informal__ store key can carry verdict 'reject' (the human
    # rejected the LLM-rendered informal text) — it is not a Lean declaration and
    # must not appear in the orphan preview.
    cfg = make_cfg(tmp_path)
    store = cfg.repo_dir / ".marathon" / "review" / "deck-subdecks.json"
    store.parent.mkdir(parents=True, exist_ok=True)
    store.write_text(json.dumps(
        {"171": {"__informal__": {"verdict": "reject"}}}
    ))
    monkeypatch.setattr(gh_mod, "list_rejected_issues", lambda cfg: [171])

    row = {r["num"]: r for r in stuck_issues(cfg)}[171]
    assert "__informal__" not in row["note_preview"]
    assert row["note_preview"] == ""                  # no real decl → no preview


def test_stuck_issues_orphan_and_stalled_coexist_sorted(tmp_path, monkeypatch):
    cfg = make_cfg(tmp_path)
    record_rejection(cfg, 200, "stalled note")
    record_stall(cfg, 200)
    monkeypatch.setattr(gh_mod, "list_rejected_issues", lambda cfg: [171])

    nums = [r["num"] for r in stuck_issues(cfg)]
    assert nums == [171, 200]                         # merged + sorted by num


def test_stuck_issues_degrades_when_gh_unavailable(tmp_path):
    # autouse fixture sets list_rejected_issues → None (gh down): only the
    # state-based stuck set appears, no orphans, no crash.
    cfg = make_cfg(tmp_path)
    record_rejection(cfg, 10, "note")
    record_stall(cfg, 10)
    by = {r["num"]: r for r in stuck_issues(cfg)}
    assert set(by) == {10} and not by[10].get("orphaned")
