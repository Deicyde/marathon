"""The deck's /api/requeue endpoint (DeckHandler._api_requeue) — re-issues a
parked rejection's note so the conductor retries it.

Focus: the ORPHAN path. When a hard reset reverts the git-tracked state.json,
an issue can be rejected on GitHub with NO local state entry. The endpoint then
recovers the verbatim note from the GitHub reject comment (fetch_reject_note)
rather than refusing — so a one-click re-queue still works. The handler is
exercised with a SimpleNamespace fake (guard/token/HTTP transport is shared
boilerplate covered elsewhere), mirroring tests/test_deck_prover_toggle.py."""
from __future__ import annotations

from http import HTTPStatus
from types import SimpleNamespace

import pytest

from marathon.deck.server import DeckHandler
from marathon.review import github as gh_mod
from marathon.review import review as review_mod
from marathon.review import state as state_mod
from marathon.review.state import record_iteration, record_rejection

from tests.test_conductor import make_cfg


def _fake(cfg, body, demo=False):
    captured: dict = {}
    fake = SimpleNamespace(
        cfg=cfg,
        _demo=demo,
        _read_guarded_body=lambda: body,
        _send_json=lambda payload, status=HTTPStatus.OK:
            captured.__setitem__("json", payload),
        _send_error_json=lambda status, msg:
            captured.__setitem__("err", (status, msg)),
    )
    return fake, captured


@pytest.fixture(autouse=True)
def _no_conductor(monkeypatch):
    # A unit test must never actually spawn a conductor process.
    monkeypatch.setattr(review_mod, "_ensure_conductor", lambda cfg: None)
    monkeypatch.setattr(review_mod, "_ensure_rejected_label", lambda cfg, num: True)
    monkeypatch.setattr(review_mod, "_ensure_verified_label", lambda cfg, num: True)
    monkeypatch.setattr(review_mod, "_ensure_unreviewed_labels", lambda cfg, num: True)


def test_requeue_route_registered():
    assert "/api/requeue" in DeckHandler._POST_ROUTES


def test_requeue_recovers_orphaned_rejection_from_github(tmp_path, monkeypatch):
    cfg = make_cfg(tmp_path)                 # empty state.json → #171 orphaned
    recorded: dict = {}
    monkeypatch.setattr(gh_mod, "list_rejected_issues", lambda c: [171])
    monkeypatch.setattr(
        gh_mod, "fetch_reject_note",
        lambda num, repo: ("FIX foo (rejected): bar", "2026-07-06T00:00:00Z"),
    )
    monkeypatch.setattr(
        state_mod, "record_rejection_if_absent",
        lambda c, num, note: recorded.update(num=num, note=note)
        or SimpleNamespace(verdict_ts="new"),
    )

    fake, captured = _fake(cfg, {"issue": 171})
    DeckHandler._api_requeue(fake)

    assert captured["json"]["requeued"] is True
    assert recorded == {"num": 171, "note": "FIX foo (rejected): bar"}


def test_requeue_orphan_not_labeled_rejected_errors(tmp_path, monkeypatch):
    cfg = make_cfg(tmp_path)
    monkeypatch.setattr(gh_mod, "list_rejected_issues", lambda c: [999])  # not 171
    fake, captured = _fake(cfg, {"issue": 171})
    DeckHandler._api_requeue(fake)
    assert captured["err"][0] == HTTPStatus.BAD_REQUEST
    assert "no stored rejection note" in captured["err"][1]
    assert "json" not in captured


def test_requeue_orphan_gh_unavailable_errors(tmp_path, monkeypatch):
    cfg = make_cfg(tmp_path)
    monkeypatch.setattr(gh_mod, "list_rejected_issues", lambda c: None)  # gh down
    fake, captured = _fake(cfg, {"issue": 171})
    DeckHandler._api_requeue(fake)
    assert captured["err"][0] == HTTPStatus.BAD_REQUEST


def test_requeue_orphan_unrecoverable_note_errors(tmp_path, monkeypatch):
    cfg = make_cfg(tmp_path)
    monkeypatch.setattr(gh_mod, "list_rejected_issues", lambda c: [171])
    monkeypatch.setattr(gh_mod, "fetch_reject_note", lambda num, repo: (None, None))
    fake, captured = _fake(cfg, {"issue": 171})
    DeckHandler._api_requeue(fake)
    assert captured["err"][0] == HTTPStatus.BAD_REQUEST
    assert "could not be recovered" in captured["err"][1]


def test_requeue_live_parked_entry_uses_stored_note(tmp_path, monkeypatch):
    cfg = make_cfg(tmp_path)
    record_rejection(cfg, 11, "the stored note")
    record_iteration(cfg, 11)                # parked: needs_iteration False
    recorded: dict = {}
    monkeypatch.setattr(
        state_mod, "record_rejection_if_current",
        lambda c, num, note, **expected: recorded.update(
            num=num, note=note, **expected
        ) or SimpleNamespace(verdict_ts="new"),
    )
    # A live entry must be handled WITHOUT consulting GitHub.
    monkeypatch.setattr(
        gh_mod, "list_rejected_issues",
        lambda c: (_ for _ in ()).throw(AssertionError("gh must not be called")),
    )

    fake, captured = _fake(cfg, {"issue": 11})
    DeckHandler._api_requeue(fake)

    assert captured["json"]["requeued"] is True
    assert recorded["num"] == 11 and recorded["note"] == "the stored note"
    assert recorded["expected_status"] == "rejected"


def test_requeue_live_verified_with_stale_label_refused(tmp_path, monkeypatch):
    # A locally VERIFIED issue (notes=None) that still carries a stale
    # review:rejected label must NOT be re-rejected via the orphan branch —
    # orphan is defined as `st is None`. Regression guard for the review finding.
    from marathon.review.state import record_verification

    cfg = make_cfg(tmp_path)
    record_verification(cfg, 42)             # status=verified, notes=None
    # A live entry must never consult GitHub, and never be re-recorded.
    monkeypatch.setattr(
        gh_mod, "list_rejected_issues",
        lambda c: (_ for _ in ()).throw(AssertionError("gh must not be called")),
    )
    recorded: dict = {}
    monkeypatch.setattr(
        state_mod, "record_rejection_if_current",
        lambda c, num, note, **expected: recorded.setdefault("hit", True),
    )

    fake, captured = _fake(cfg, {"issue": 42})
    DeckHandler._api_requeue(fake)

    assert captured["err"][0] == HTTPStatus.BAD_REQUEST
    assert "no stored rejection note" in captured["err"][1]
    assert "hit" not in recorded             # the verify verdict is untouched


def test_requeue_live_active_entry_conflicts(tmp_path):
    cfg = make_cfg(tmp_path)
    record_rejection(cfg, 12, "active")      # needs_iteration True → not parked
    fake, captured = _fake(cfg, {"issue": 12})
    DeckHandler._api_requeue(fake)
    assert captured["err"][0] == HTTPStatus.CONFLICT
    assert "not parked" in captured["err"][1]


def test_requeue_cas_loser_preserves_concurrent_verify_and_repairs_labels(
    tmp_path, monkeypatch
):
    from marathon.review.state import record_verification

    cfg = make_cfg(tmp_path)
    record_rejection(cfg, 13, "parked")
    record_iteration(cfg, 13)
    repairs = []

    def lose_to_verify(c, num, note, **expected):
        record_verification(c, num)
        return None

    monkeypatch.setattr(state_mod, "record_rejection_if_current", lose_to_verify)
    monkeypatch.setattr(
        review_mod,
        "_ensure_verified_label",
        lambda c, num: repairs.append((num, "verified")) or True,
    )

    fake, captured = _fake(cfg, {"issue": 13})
    DeckHandler._api_requeue(fake)

    assert captured["err"][0] == HTTPStatus.CONFLICT
    assert state_mod.load_state(cfg).issues[13].status == "verified"
    assert repairs == [(13, "verified")]


def test_requeue_label_failure_changes_no_local_state(tmp_path, monkeypatch):
    cfg = make_cfg(tmp_path)
    before = record_rejection(cfg, 14, "parked")
    record_iteration(cfg, 14)
    monkeypatch.setattr(
        review_mod, "_ensure_rejected_label", lambda c, num: False
    )

    fake, captured = _fake(cfg, {"issue": 14})
    DeckHandler._api_requeue(fake)

    assert captured["err"][0] == HTTPStatus.BAD_GATEWAY
    after = state_mod.load_state(cfg).issues[14]
    assert after.verdict_ts == before.verdict_ts
    assert after.needs_iteration() is False


def test_requeue_guard_short_circuits(tmp_path):
    cfg = make_cfg(tmp_path)
    fake, captured = _fake(cfg, None)        # guard already sent the 4xx
    DeckHandler._api_requeue(fake)
    assert captured == {}


def test_requeue_demo_mode_no_op(tmp_path):
    cfg = make_cfg(tmp_path)
    fake, captured = _fake(cfg, {"issue": 171}, demo=True)
    DeckHandler._api_requeue(fake)
    assert captured["json"]["requeued"] is False
