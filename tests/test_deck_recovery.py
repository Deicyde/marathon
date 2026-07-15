"""Explicit recovery API regressions.

The dashboard snapshot is a compare-and-swap capability: the server accepts
only an enabled action from the exact rendered token, requires an extra
confirmation before discarding an existing PR, and preserves a concurrent
human verdict.  The legacy ``/api/requeue`` alias may choose the safe server
recommendation but must never silently choose ``start_over`` when a PR exists.
"""
from __future__ import annotations

from http import HTTPStatus
from types import SimpleNamespace

import pytest

from marathon.deck import dashboard as dashboard_mod
from marathon.deck.server import DeckHandler
from marathon.review import review as review_mod
from marathon.review import state as state_mod
from marathon.review.state import record_iteration, record_rejection

from tests.test_conductor import make_cfg


def _fake(cfg, body):
    captured: dict = {}

    def send_json(payload, status=HTTPStatus.OK):
        captured["json"] = payload
        captured["status"] = status

    fake = SimpleNamespace(
        cfg=cfg,
        _demo=False,
        _read_guarded_body=lambda: body,
        _send_json=send_json,
        _send_error_json=lambda status, message: captured.__setitem__(
            "err", (status, message)
        ),
    )
    return fake, captured


def _parked(cfg, issue=171):
    rejected = record_rejection(cfg, issue, "keep the existing work")
    record_iteration(cfg, issue, expected_verdict_ts=rejected.verdict_ts)
    return rejected


def _row(
    *, verdict_ts, issue=171, recommended="retry_build", token="snapshot-token"
):
    branch = "marathon/refine-c15-i171"
    return {
        "num": issue,
        "status": "rejected",
        "verdict_ts": verdict_ts,
        "attempts": 0,
        "recovery_id": None,
        "recovery_mode": None,
        "has_note": True,
        "fix_pr": 277,
        "fix_pr_branch": branch,
        "fix_pr_head_sha": "a" * 40,
        "recovery_token": token,
        "recommended_action": recommended,
        "actions": {
            "retry_build": {"enabled": True, "reason": "build exact head"},
            "continue_pr": {"enabled": True, "reason": "continue PR"},
            "update_continue": {"enabled": True, "reason": "update PR"},
            "start_over": {"enabled": True, "reason": "archive then replace"},
        },
    }


@pytest.fixture(autouse=True)
def _safe_external_edges(monkeypatch):
    monkeypatch.setattr(review_mod, "_ensure_conductor", lambda cfg: None)
    monkeypatch.setattr(review_mod, "_ensure_rejected_label", lambda cfg, n: True)
    monkeypatch.setattr(review_mod, "_ensure_verified_label", lambda cfg, n: True)
    monkeypatch.setattr(review_mod, "_ensure_unreviewed_labels", lambda cfg, n: True)
    monkeypatch.setattr(
        dashboard_mod, "conductor_status", lambda cfg: {"alive": False}
    )


def test_recovery_route_registered():
    assert "/api/recovery" in DeckHandler._POST_ROUTES


def test_recovery_rejects_stale_snapshot_token_without_mutating_state(
    tmp_path, monkeypatch
):
    cfg = make_cfg(tmp_path)
    parked = _parked(cfg)
    monkeypatch.setattr(
        dashboard_mod,
        "stuck_issues",
        lambda cfg: [_row(verdict_ts=parked.verdict_ts)],
    )
    fake, captured = _fake(
        cfg,
        {
            "issue": 171,
            "action": "retry_build",
            "recovery_token": "old-token",
        },
    )

    DeckHandler._api_recovery(fake)

    assert captured["err"][0] == HTTPStatus.CONFLICT
    assert "changed since" in captured["err"][1]
    current = state_mod.load_state(cfg).issues[171]
    assert current.verdict_ts == parked.verdict_ts
    assert current.recovery_id is None


def test_state_change_after_token_check_cannot_retarget_old_dialog_action(
    tmp_path, monkeypatch
):
    cfg = make_cfg(tmp_path)
    rendered = _parked(cfg)
    row = _row(verdict_ts=rendered.verdict_ts)
    monkeypatch.setattr(dashboard_mod, "stuck_issues", lambda cfg: [row])
    real_load = state_mod.load_state
    raced = False

    def load_after_new_verdict(cfg_):
        nonlocal raced
        if not raced:
            raced = True
            state_mod.record_rejection(cfg_, 171, "newer human verdict")
        return real_load(cfg_)

    monkeypatch.setattr(state_mod, "load_state", load_after_new_verdict)
    fake, captured = _fake(
        cfg,
        {
            "issue": 171,
            "action": "retry_build",
            "recovery_token": "snapshot-token",
        },
    )

    DeckHandler._api_recovery(fake)

    assert captured["err"][0] == HTTPStatus.CONFLICT
    assert "changed after the snapshot" in captured["err"][1]
    current = real_load(cfg).issues[171]
    assert current.notes == "newer human verdict"
    assert current.verdict_ts != rendered.verdict_ts
    assert current.recovery_id is None


def test_recovery_rejects_disabled_or_unknown_action(tmp_path, monkeypatch):
    cfg = make_cfg(tmp_path)
    parked = _parked(cfg)
    row = _row(verdict_ts=parked.verdict_ts)
    row["actions"]["retry_build"]["enabled"] = False
    monkeypatch.setattr(dashboard_mod, "stuck_issues", lambda cfg: [row])
    fake, captured = _fake(
        cfg,
        {
            "issue": 171,
            "action": "retry_build",
            "recovery_token": "snapshot-token",
        },
    )

    DeckHandler._api_recovery(fake)

    assert captured["err"][0] == HTTPStatus.CONFLICT
    assert "unavailable" in captured["err"][1]


def test_note_less_live_row_can_queue_exact_build_only_recovery(
    tmp_path, monkeypatch
):
    cfg = make_cfg(tmp_path)
    rejected = record_rejection(cfg, 171, "")
    record_iteration(cfg, 171, expected_verdict_ts=rejected.verdict_ts)
    row = _row(verdict_ts=rejected.verdict_ts)
    row["has_note"] = False
    monkeypatch.setattr(dashboard_mod, "stuck_issues", lambda cfg: [row])
    fake, captured = _fake(
        cfg,
        {
            "issue": 171,
            "action": "retry_build",
            "recovery_token": "snapshot-token",
        },
    )

    DeckHandler._api_recovery(fake)

    assert captured["status"] == HTTPStatus.ACCEPTED
    current = state_mod.load_state(cfg).issues[171]
    assert current.recovery_mode == state_mod.RECOVERY_RETRY_BUILD


def test_start_over_existing_pr_requires_explicit_confirmation(
    tmp_path, monkeypatch
):
    cfg = make_cfg(tmp_path)
    parked = _parked(cfg)
    monkeypatch.setattr(
        dashboard_mod,
        "stuck_issues",
        lambda cfg: [_row(verdict_ts=parked.verdict_ts)],
    )
    fake, captured = _fake(
        cfg,
        {
            "issue": 171,
            "action": "start_over",
            "recovery_token": "snapshot-token",
        },
    )

    DeckHandler._api_recovery(fake)

    assert captured["err"] == (
        HTTPStatus.BAD_REQUEST,
        "start_over requires confirm_destructive=true",
    )
    assert state_mod.load_state(cfg).issues[171].recovery_id is None


def test_confirmed_start_over_records_exact_pr_snapshot(tmp_path, monkeypatch):
    cfg = make_cfg(tmp_path)
    parked = _parked(cfg)
    monkeypatch.setattr(
        dashboard_mod,
        "stuck_issues",
        lambda cfg: [_row(verdict_ts=parked.verdict_ts)],
    )
    fake, captured = _fake(
        cfg,
        {
            "issue": 171,
            "action": "start_over",
            "recovery_token": "snapshot-token",
            "confirm_destructive": True,
        },
    )

    DeckHandler._api_recovery(fake)

    assert captured["status"] == HTTPStatus.ACCEPTED
    assert captured["json"]["action"] == "start_over"
    current = state_mod.load_state(cfg).issues[171]
    assert current.verdict_ts == parked.verdict_ts
    assert current.recovery_id == captured["json"]["operation_id"]
    assert current.recovery_mode == state_mod.RECOVERY_START_OVER
    assert current.recovery_pr == 277
    assert current.recovery_branch == "marathon/refine-c15-i171"
    assert current.recovery_head_sha == "a" * 40


def test_recovery_cas_loser_preserves_concurrent_verify_and_repairs_labels(
    tmp_path, monkeypatch
):
    cfg = make_cfg(tmp_path)
    parked = _parked(cfg)
    monkeypatch.setattr(
        dashboard_mod,
        "stuck_issues",
        lambda cfg: [_row(verdict_ts=parked.verdict_ts)],
    )
    repairs = []

    def lose_to_verify(cfg_, issue, **kwargs):
        state_mod.record_verification(cfg_, issue)
        return None

    monkeypatch.setattr(state_mod, "request_recovery_if_current", lose_to_verify)
    monkeypatch.setattr(
        review_mod,
        "_ensure_verified_label",
        lambda cfg, issue: repairs.append(issue) or True,
    )
    fake, captured = _fake(
        cfg,
        {
            "issue": 171,
            "action": "continue_pr",
            "recovery_token": "snapshot-token",
        },
    )

    DeckHandler._api_recovery(fake)

    assert captured["err"][0] == HTTPStatus.CONFLICT
    assert state_mod.load_state(cfg).issues[171].status == "verified"
    assert repairs == [171]


def test_legacy_requeue_chooses_safe_server_recommendation(tmp_path, monkeypatch):
    cfg = make_cfg(tmp_path)
    parked = _parked(cfg)
    monkeypatch.setattr(
        dashboard_mod,
        "stuck_issues",
        lambda cfg: [_row(
            verdict_ts=parked.verdict_ts,
            recommended="retry_build",
        )],
    )
    fake, captured = _fake(cfg, {"issue": 171})

    DeckHandler._api_requeue(fake)

    assert captured["status"] == HTTPStatus.ACCEPTED
    assert captured["json"]["action"] == "retry_build"
    current = state_mod.load_state(cfg).issues[171]
    assert current.verdict_ts == parked.verdict_ts
    assert current.recovery_mode == state_mod.RECOVERY_RETRY_BUILD
    assert current.recovery_pr == 277


def test_legacy_requeue_ignores_caller_supplied_destructive_action(
    tmp_path, monkeypatch
):
    cfg = make_cfg(tmp_path)
    parked = _parked(cfg)
    monkeypatch.setattr(
        dashboard_mod,
        "stuck_issues",
        lambda cfg: [_row(
            verdict_ts=parked.verdict_ts,
            recommended="retry_build",
        )],
    )
    fake, captured = _fake(
        cfg,
        {
            "issue": 171,
            "action": "start_over",
            "confirm_destructive": True,
        },
    )

    DeckHandler._api_requeue(fake)

    assert captured["status"] == HTTPStatus.ACCEPTED
    assert captured["json"]["action"] == "retry_build"
    current = state_mod.load_state(cfg).issues[171]
    assert current.recovery_mode == state_mod.RECOVERY_RETRY_BUILD


def test_legacy_requeue_never_implicitly_starts_over_existing_pr(
    tmp_path, monkeypatch
):
    cfg = make_cfg(tmp_path)
    parked = _parked(cfg)
    monkeypatch.setattr(
        dashboard_mod,
        "stuck_issues",
        lambda cfg: [_row(
            verdict_ts=parked.verdict_ts,
            recommended="start_over",
        )],
    )
    fake, captured = _fake(cfg, {"issue": 171})

    DeckHandler._api_requeue(fake)

    assert captured["err"][0] == HTTPStatus.BAD_REQUEST
    assert "confirm_destructive" in captured["err"][1]
    assert state_mod.load_state(cfg).issues[171].recovery_id is None
