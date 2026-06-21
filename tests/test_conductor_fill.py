"""Conductor fill-dispatch: which verified-but-`sorry` issues get a fill this tick.
Excludes in-flight, backed-off, attempt-capped, and open-PR (pending-merge) issues."""
from __future__ import annotations

from types import SimpleNamespace

import marathon.conductor as cond
from marathon.review.fill_discovery import FillTarget


def _ft(issue, chapter=12):
    return FillTarget(issue=issue, chapter=chapter, decls=["d"])


def test_pick_fill_excludes_running_backoff_capped_and_open_pr():
    cfg = SimpleNamespace(github_repo="o/r")
    targets = {12: [_ft(101), _ft(102), _ft(103), _ft(104), _ft(105)]}
    picks = cond._pick_fill_dispatchable(
        cfg, {12},
        running_issues={101},               # 101 already running
        not_before={102: 2000.0},           # 102 backed off (future)
        fill_attempts={103: 3},             # 103 hit the attempt cap
        max_fill_attempts=3,
        _now_fn=lambda: 1000.0,
        _discover=lambda c, ch: targets.get(ch, []),
        _open_pr=lambda c, n, ch: n == 104,  # 104 has a fill PR pending merge
    )
    assert picks == [(105, 12)]


def test_pick_fill_empty_when_no_chapters_requested():
    cfg = SimpleNamespace(github_repo="o/r")
    assert cond._pick_fill_dispatchable(cfg, set(), set(), {}, {}, 3) == []


def test_pick_fill_spans_chapters_sorted_by_issue():
    cfg = SimpleNamespace(github_repo="o/r")
    targets = {11: [_ft(201, 11)], 12: [_ft(202, 12)]}
    picks = cond._pick_fill_dispatchable(
        cfg, {11, 12}, set(), {}, {}, 3,
        _discover=lambda c, ch: targets.get(ch, []),
        _open_pr=lambda c, n, ch: False,
    )
    assert picks == [(201, 11), (202, 12)]


def test_pick_fill_expired_backoff_is_dispatchable():
    cfg = SimpleNamespace(github_repo="o/r")
    picks = cond._pick_fill_dispatchable(
        cfg, {12}, set(),
        not_before={108: 500.0},  # backoff in the past → eligible again
        fill_attempts={}, max_fill_attempts=3,
        _now_fn=lambda: 1000.0,
        _discover=lambda c, ch: [_ft(108)],
        _open_pr=lambda c, n, ch: False,
    )
    assert picks == [(108, 12)]
