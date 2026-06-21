"""Conductor auto-reconcile: an issue stuck in 'rejected' after its refine PR was
merged OUT-OF-BAND (e.g. a hand-merge, which skips automerge's post-merge requeue)
is requeued for re-review — but ONLY when the merge landed strictly AFTER the
current rejection (so a fresh re-reject whose old PR merged is left alone)."""
from __future__ import annotations

import json
from types import SimpleNamespace

import marathon.conductor as conductor
import marathon.review.automerge as amod
import marathon.review.github as ghmod
import marathon.review.state as statemod


def _issue(status, verdict_ts):
    return SimpleNamespace(status=status, verdict_ts=verdict_ts)


def _patch_gh(monkeypatch, merged_prs):
    monkeypatch.setattr(
        ghmod, "gh",
        lambda *a, **k: SimpleNamespace(returncode=0, stdout=json.dumps(merged_prs)),
    )


def _capture_requeue(monkeypatch):
    requeued = []

    def fake(cfg, head):
        n = int(head.rsplit("-i", 1)[1])
        requeued.append(n)
        return n

    monkeypatch.setattr(amod, "_requeue_refined_issue", fake)
    return requeued


def test_requeues_only_when_merge_is_after_the_rejection(monkeypatch):
    state = SimpleNamespace(issues={
        96: _issue("rejected", "2026-06-21T00:00:00-04:00"),  # 04:00Z — merge AFTER  → requeue
        97: _issue("rejected", "2026-06-21T10:00:00-04:00"),  # 14:00Z — merge BEFORE → skip
        98: _issue("rejected", "2026-06-21T00:00:00-04:00"),  # no merged PR          → skip
        99: _issue("verified", "2026-06-21T00:00:00-04:00"),  # not rejected          → skip
    })
    monkeypatch.setattr(statemod, "load_state", lambda cfg: state)
    _patch_gh(monkeypatch, [
        {"headRefName": "marathon/refine-c12-i96", "mergedAt": "2026-06-21T05:00:00Z"},
        {"headRefName": "marathon/refine-c12-i97", "mergedAt": "2026-06-21T05:00:00Z"},
        {"headRefName": "feature/unrelated", "mergedAt": "2026-06-21T05:00:00Z"},
    ])
    requeued = _capture_requeue(monkeypatch)

    out = conductor._reconcile_merged_rejections(SimpleNamespace(github_repo="o/r"))
    assert out == [96]
    assert requeued == [96]


def test_no_gh_call_when_nothing_rejected(monkeypatch):
    state = SimpleNamespace(issues={99: _issue("verified", "2026-06-21T00:00:00Z")})
    monkeypatch.setattr(statemod, "load_state", lambda cfg: state)
    called = []
    monkeypatch.setattr(ghmod, "gh",
                        lambda *a, **k: called.append(1) or SimpleNamespace(returncode=0, stdout="[]"))
    assert conductor._reconcile_merged_rejections(SimpleNamespace(github_repo="o/r")) == []
    assert called == []  # short-circuits before any GitHub read


def test_failsoft_on_gh_error(monkeypatch):
    state = SimpleNamespace(issues={96: _issue("rejected", "2026-06-21T00:00:00-04:00")})
    monkeypatch.setattr(statemod, "load_state", lambda cfg: state)

    def boom(*a, **k):
        raise OSError("gh vanished")

    monkeypatch.setattr(ghmod, "gh", boom)
    # never raises — a reconcile failure must not crash a conductor tick
    assert conductor._reconcile_merged_rejections(SimpleNamespace(github_repo="o/r")) == []
