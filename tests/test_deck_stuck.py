"""The "Stalled — needs re-reject" surface (deck/dashboard.stuck_issues): issues
parked OUTSIDE both the swipe queue and the refine queue — status 'stalled', or
'rejected' with no pending iteration (already iterated, no fix landed) — so a
human can re-queue them from the deck instead of dropping to the CLI."""
from __future__ import annotations

from marathon.deck.dashboard import stuck_issues
from marathon.review.state import (
    record_iteration, record_rejection, record_stall,
)

from tests.test_conductor import make_cfg


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
