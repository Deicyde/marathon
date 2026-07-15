"""Fail-closed recovery edges: incomplete PR discovery and operation races."""
from __future__ import annotations

import json
from http import HTTPStatus
from types import SimpleNamespace

import pytest

from marathon.deck import dashboard as dashboard_mod
from marathon.deck.server import DeckHandler
from marathon.review import github as github_mod
from marathon.review import review as review_mod
from marathon.review import state as state_mod

from tests.test_conductor import make_cfg


def _parked(cfg, issue=171):
    rejected = state_mod.record_rejection(cfg, issue, "preserve this work")
    state_mod.record_iteration(
        cfg, issue, expected_verdict_ts=rejected.verdict_ts
    )
    return rejected


def _actions_disabled(row):
    return all(not action["enabled"] for action in row["actions"].values())


def _fake_handler(cfg, body):
    captured = {}

    def send_json(payload, status=HTTPStatus.OK):
        captured["json"] = payload
        captured["status"] = status

    return SimpleNamespace(
        cfg=cfg,
        _demo=False,
        _read_guarded_body=lambda: body,
        _send_json=send_json,
        _send_error_json=lambda status, message: captured.__setitem__(
            "err", (status, message)
        ),
    ), captured


def _server_row(*, lookup_known=True):
    row = {
        "num": 171,
        "status": "rejected",
        "verdict_ts": "rendered-verdict",
        "attempts": 0,
        "has_note": True,
        "pr_lookup_known": lookup_known,
    }
    row["actions"], row["recommended_action"] = (
        dashboard_mod._recovery_actions(row)
    )
    row["recovery_token"] = dashboard_mod._recovery_token(row)
    return row


@pytest.fixture(autouse=True)
def _offline_edges(monkeypatch):
    monkeypatch.setattr(github_mod, "list_rejected_issues", lambda cfg: None)
    monkeypatch.setattr(
        dashboard_mod, "conductor_status", lambda cfg: {"alive": False}
    )
    monkeypatch.setattr(review_mod, "_ensure_conductor", lambda cfg: None)
    monkeypatch.setattr(review_mod, "_ensure_rejected_label", lambda cfg, n: True)
    monkeypatch.setattr(review_mod, "_ensure_verified_label", lambda cfg, n: True)
    monkeypatch.setattr(review_mod, "_ensure_unreviewed_labels", lambda cfg, n: True)


def test_unknown_pr_lookup_disables_every_recovery_action(tmp_path, monkeypatch):
    cfg = make_cfg(tmp_path)
    _parked(cfg)
    monkeypatch.setattr(dashboard_mod, "_open_refine_prs", lambda cfg: None)

    row = dashboard_mod.stuck_issues(cfg)[0]

    assert row["pr_lookup_known"] is False
    assert _actions_disabled(row)


def test_truncated_pr_lookup_disables_every_recovery_action(tmp_path, monkeypatch):
    cfg = make_cfg(tmp_path)
    _parked(cfg)
    rows = [{} for _ in range(1000)]
    monkeypatch.setattr(
        github_mod,
        "gh",
        lambda *a, **k: SimpleNamespace(
            returncode=0, stdout=json.dumps(rows), stderr=""
        ),
    )

    lookup = dashboard_mod._open_refine_prs(cfg)
    assert lookup is None
    monkeypatch.setattr(dashboard_mod, "_open_refine_prs", lambda cfg: lookup)
    row = dashboard_mod.stuck_issues(cfg)[0]

    assert _actions_disabled(row)


def _pr(number, branch, head):
    return {
        "number": number,
        "headRefName": branch,
        "headRefOid": head,
        "baseRefName": "main",
        "title": f"recovery {number} [build:FAIL]",
        "mergeable": "MERGEABLE",
        "mergeStateStatus": "CLEAN",
        "statusCheckRollup": [],
    }


def test_duplicate_open_prs_disable_every_recovery_action(tmp_path, monkeypatch):
    cfg = make_cfg(tmp_path)
    _parked(cfg)
    rows = [
        _pr(277, "marathon/refine-c15-i171", "a" * 40),
        _pr(278, "marathon/refine-c15-i171-vdeadbeefcafe", "b" * 40),
    ]
    monkeypatch.setattr(
        github_mod,
        "gh",
        lambda *a, **k: SimpleNamespace(
            returncode=0, stdout=json.dumps(rows), stderr=""
        ),
    )

    lookup = dashboard_mod._open_refine_prs(cfg)
    assert lookup == {171: {"ambiguous": True}}
    monkeypatch.setattr(dashboard_mod, "_open_refine_prs", lambda cfg: lookup)
    row = dashboard_mod.stuck_issues(cfg)[0]

    assert row["fix_pr_ambiguous"] is True
    assert _actions_disabled(row)


def test_generation_suffixed_refine_head_remains_discoverable(
    tmp_path, monkeypatch
):
    cfg = make_cfg(tmp_path)
    branch = "marathon/refine-c15-i171-vdeadbeefcafe"
    rows = [_pr(278, branch, "b" * 40)]
    monkeypatch.setattr(
        github_mod,
        "gh",
        lambda *a, **k: SimpleNamespace(
            returncode=0, stdout=json.dumps(rows), stderr=""
        ),
    )

    lookup = dashboard_mod._open_refine_prs(cfg)

    assert lookup is not None
    assert lookup[171]["pr"] == 278
    assert lookup[171]["branch"] == branch
    assert lookup[171]["head_sha"] == "b" * 40


def test_legacy_endpoint_cannot_start_over_when_lookup_is_unknown(
    tmp_path, monkeypatch
):
    cfg = make_cfg(tmp_path)
    parked = _parked(cfg)
    row = _server_row(lookup_known=False)
    monkeypatch.setattr(dashboard_mod, "stuck_issues", lambda cfg: [row])
    handler, captured = _fake_handler(cfg, {"issue": 171})

    DeckHandler._api_requeue(handler)

    assert captured["err"][0] == HTTPStatus.CONFLICT
    current = state_mod.load_state(cfg).issues[171]
    assert current.verdict_ts == parked.verdict_ts
    assert current.recovery_id is None


def test_legacy_endpoint_cannot_implicitly_start_over_without_pr(
    tmp_path, monkeypatch
):
    cfg = make_cfg(tmp_path)
    parked = _parked(cfg)
    row = _server_row(lookup_known=True)
    assert row["recommended_action"] == state_mod.RECOVERY_START_OVER
    assert row["actions"][state_mod.RECOVERY_START_OVER]["enabled"] is True
    monkeypatch.setattr(dashboard_mod, "stuck_issues", lambda cfg: [row])
    handler, captured = _fake_handler(cfg, {"issue": 171})

    DeckHandler._api_requeue(handler)

    assert captured["err"][0] == HTTPStatus.BAD_REQUEST
    assert "confirm_destructive" in captured["err"][1]
    current = state_mod.load_state(cfg).issues[171]
    assert current.verdict_ts == parked.verdict_ts
    assert current.recovery_id is None


@pytest.mark.parametrize("confirmation", [None, False, 1, "true"])
def test_start_over_without_pr_always_requires_literal_true_confirmation(
    tmp_path, monkeypatch, confirmation
):
    cfg = make_cfg(tmp_path)
    _parked(cfg)
    row = _server_row(lookup_known=True)
    monkeypatch.setattr(dashboard_mod, "stuck_issues", lambda cfg: [row])
    body = {
        "issue": 171,
        "action": state_mod.RECOVERY_START_OVER,
        "recovery_token": row["recovery_token"],
    }
    if confirmation is not None:
        body["confirm_destructive"] = confirmation
    handler, captured = _fake_handler(cfg, body)

    DeckHandler._api_recovery(handler)

    assert captured["err"] == (
        HTTPStatus.BAD_REQUEST,
        "start_over requires confirm_destructive=true",
    )
    assert state_mod.load_state(cfg).issues[171].recovery_id is None


def test_stale_recovery_generation_cannot_overwrite_newer_request(tmp_path):
    cfg = make_cfg(tmp_path)
    parked = _parked(cfg)
    first = state_mod.request_recovery_if_current(
        cfg,
        171,
        expected_status="rejected",
        expected_verdict_ts=parked.verdict_ts,
        expected_recovery_id=None,
        recovery_id="operation-one",
        recovery_mode=state_mod.RECOVERY_START_OVER,
    )
    assert first is not None
    second = state_mod.request_recovery_if_current(
        cfg,
        171,
        expected_status="rejected",
        expected_verdict_ts=parked.verdict_ts,
        expected_recovery_id="operation-one",
        recovery_id="operation-two",
        recovery_mode=state_mod.RECOVERY_START_OVER,
    )
    assert second is not None

    stale = state_mod.request_recovery_if_current(
        cfg,
        171,
        expected_status="rejected",
        expected_verdict_ts=parked.verdict_ts,
        expected_recovery_id="operation-one",
        recovery_id="stale-operation-three",
        recovery_mode=state_mod.RECOVERY_START_OVER,
    )

    assert stale is None
    current = state_mod.load_state(cfg).issues[171]
    assert current.verdict_ts == parked.verdict_ts
    assert current.recovery_id == "operation-two"
    assert current.recovery_mode == state_mod.RECOVERY_START_OVER
