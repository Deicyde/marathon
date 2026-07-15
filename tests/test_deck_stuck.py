"""The "Stalled — needs re-reject" surface (deck/dashboard.stuck_issues): issues
parked OUTSIDE both the swipe queue and the refine queue — status 'stalled', or
'rejected' with no pending iteration (already iterated, no fix landed) — so a
human can re-queue them from the deck instead of dropping to the CLI."""
from __future__ import annotations

import json

import pytest

from marathon.deck.dashboard import _open_refine_prs as _real_open_refine_prs
from marathon.deck.dashboard import stuck_issues
from marathon.review import github as gh_mod
from marathon.review.state import (
    record_iteration, record_rejection, record_stall,
)

from tests.test_conductor import make_cfg


@pytest.fixture(autouse=True)
def _offline_gh(monkeypatch):
    """stuck_issues now consults GitHub (list_rejected_issues + the open
    fix-PR lookup) to recover orphaned rejections and annotate rows. Keep the
    state-based unit tests OFFLINE by default; individual tests re-patch."""
    from marathon.deck import dashboard as dash_mod

    monkeypatch.setattr(gh_mod, "list_rejected_issues", lambda cfg: None)
    monkeypatch.setattr(dash_mod, "_open_refine_prs", lambda cfg: {})
    monkeypatch.setattr(dash_mod, "conductor_status", lambda cfg: {"alive": False})


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


def test_stuck_rows_annotated_with_open_fix_pr(tmp_path, monkeypatch):
    # A stalled issue with an OPEN [build:FAIL] refine PR: the row must carry
    # the PR so the deck says "fix PR open, held" instead of the false "the
    # fix never landed" — and the frontend can confirm before a billed
    # re-queue. An issue with no open PR stays unannotated.
    from marathon.deck import dashboard as dash_mod

    cfg = make_cfg(tmp_path)
    record_rejection(cfg, 238, "fix it")
    record_stall(cfg, 238)
    record_rejection(cfg, 300, "other")
    record_stall(cfg, 300)
    monkeypatch.setattr(
        dash_mod, "_open_refine_prs",
        lambda cfg: {238: {"pr": 260, "build": "FAIL"}},
    )

    by = {r["num"]: r for r in stuck_issues(cfg)}
    assert by[238]["fix_pr"] == 260 and by[238]["fix_pr_build"] == "FAIL"
    assert "fix_pr" not in by[300]


def test_open_refine_prs_parses_heads_and_build_tags(monkeypatch, tmp_path):
    from types import SimpleNamespace

    from marathon.review import github as real_gh

    def fake_gh(*args, check=True, capture=True, timeout=None):
        assert args[:2] == ("pr", "list")
        return SimpleNamespace(returncode=0, stdout=json.dumps([
            {"number": 260, "headRefName": "marathon/refine-c16-i238",
             "title": "marathon: Chapter16 iter for #238 [build:FAIL]"},
            {"number": 264, "headRefName": "marathon/refine-c15-i171",
             "title": "marathon: Chapter15 iter for #171 [build:OK]"},
            {"number": 265,
             "headRefName": "marathon/refine-c15-i172-v0123456789ab",
             "title": "marathon: generation branch for #172 [build:FAIL]"},
            {"number": 263, "headRefName": "chore/register-ch16",
             "title": "registry"},                       # not a refine head
        ]))

    monkeypatch.setattr(real_gh, "gh", fake_gh)
    cfg = make_cfg(tmp_path)
    # the autouse fixture stubs the module attribute — call the REAL function
    got = _real_open_refine_prs(cfg)
    assert got == {
        238: {
            "pr": 260,
            "branch": "marathon/refine-c16-i238",
            "build": "FAIL",
        },
        171: {
            "pr": 264,
            "branch": "marathon/refine-c15-i171",
            "build": "OK",
        },
        172: {
            "pr": 265,
            "branch": "marathon/refine-c15-i172-v0123456789ab",
            "build": "FAIL",
        },
    }


def _parked(cfg, issue=238):
    rejected = record_rejection(cfg, issue, "fix it")
    record_stall(cfg, issue, expected_verdict_ts=rejected.verdict_ts)


def test_recovery_recommends_build_retry_for_exact_red_pr(tmp_path, monkeypatch):
    from marathon.deck import dashboard as dash_mod

    cfg = make_cfg(tmp_path)
    _parked(cfg)
    monkeypatch.setattr(
        dash_mod,
        "_open_refine_prs",
        lambda cfg: {
            238: {
                "pr": 260,
                "branch": "marathon/refine-c16-i238",
                "head_sha": "a" * 40,
                "base": "main",
                "build": "FAIL",
                "mergeable": "MERGEABLE",
                "merge_state": "CLEAN",
            }
        },
    )

    row = stuck_issues(cfg)[0]

    assert row["recommended_action"] == "retry_build"
    assert row["actions"]["retry_build"]["enabled"] is True
    assert row["actions"]["continue_pr"]["enabled"] is True
    assert row["actions"]["update_continue"]["enabled"] is True
    assert row["actions"]["start_over"]["destructive"] is True
    assert len(row["recovery_token"]) == 32


def test_recovery_recommends_update_for_conflicted_pr(tmp_path, monkeypatch):
    from marathon.deck import dashboard as dash_mod

    cfg = make_cfg(tmp_path)
    _parked(cfg)
    monkeypatch.setattr(
        dash_mod,
        "_open_refine_prs",
        lambda cfg: {
            238: {
                "pr": 260,
                "branch": "marathon/refine-c16-i238",
                "head_sha": "b" * 40,
                "base": "main",
                "build": "FAIL",
                "mergeable": "CONFLICTING",
                "merge_state": "DIRTY",
            }
        },
    )

    row = stuck_issues(cfg)[0]

    assert row["recommended_action"] == "update_continue"
    assert row["actions"]["retry_build"]["enabled"] is False
    assert row["actions"]["continue_pr"]["enabled"] is True
    assert row["actions"]["update_continue"]["enabled"] is True


def test_recovery_running_disables_every_overlapping_action():
    from marathon.deck import dashboard as dash_mod

    row = {
        "num": 238,
        "has_note": True,
        "fix_pr": 260,
        "fix_pr_branch": "marathon/refine-c16-i238",
        "fix_pr_head_sha": "a" * 40,
        "fix_pr_base": "main",
        "fix_pr_build": "FAIL",
        "fix_pr_mergeable": "MERGEABLE",
        "fix_pr_merge_state": "CLEAN",
        "recovery_running": True,
    }

    actions, recommended = dash_mod._recovery_actions(row)

    assert recommended is None
    assert all(not spec["enabled"] for spec in actions.values())
    assert all("already running" in spec["reason"] for spec in actions.values())


def test_behind_pr_with_local_checkpoint_still_recommends_update():
    from marathon.deck import dashboard as dash_mod

    row = {
        "num": 238,
        "has_note": True,
        "fix_pr": 260,
        "fix_pr_branch": "marathon/refine-c16-i238",
        "fix_pr_head_sha": "a" * 40,
        "fix_pr_base": "main",
        "fix_pr_build": "FAIL",
        "fix_pr_mergeable": "MERGEABLE",
        "fix_pr_merge_state": "BEHIND",
        "local_recovery_available": True,
    }

    actions, recommended = dash_mod._recovery_actions(row)

    assert actions["update_continue"]["enabled"] is True
    assert recommended == "update_continue"


def test_recovery_without_exact_pr_only_offers_start_over(tmp_path):
    cfg = make_cfg(tmp_path)
    _parked(cfg)

    row = stuck_issues(cfg)[0]

    assert row["recommended_action"] == "start_over"
    assert row["actions"]["retry_build"]["enabled"] is False
    assert row["actions"]["continue_pr"]["enabled"] is False
    assert row["actions"]["update_continue"]["enabled"] is False
    assert row["actions"]["start_over"]["enabled"] is True


def test_recovery_token_changes_with_exact_pr_head(tmp_path, monkeypatch):
    from marathon.deck import dashboard as dash_mod

    cfg = make_cfg(tmp_path)
    _parked(cfg)
    pr = {
        "pr": 260,
        "branch": "marathon/refine-c16-i238",
        "head_sha": "c" * 40,
        "base": "main",
        "build": "FAIL",
        "mergeable": "MERGEABLE",
        "merge_state": "CLEAN",
    }
    monkeypatch.setattr(dash_mod, "_open_refine_prs", lambda cfg: {238: pr})
    first = stuck_issues(cfg)[0]["recovery_token"]
    pr["head_sha"] = "d" * 40
    second = stuck_issues(cfg)[0]["recovery_token"]

    assert first != second


def test_stuck_issues_degrades_when_gh_unavailable(tmp_path):
    # autouse fixture sets list_rejected_issues → None (gh down): only the
    # state-based stuck set appears, no orphans, no crash.
    cfg = make_cfg(tmp_path)
    record_rejection(cfg, 10, "note")
    record_stall(cfg, 10)
    by = {r["num"]: r for r in stuck_issues(cfg)}
    assert set(by) == {10} and not by[10].get("orphaned")
