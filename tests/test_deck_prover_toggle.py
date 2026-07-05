"""The deck's /api/prover-toggle endpoint — writes the control file the
conductor polls. Exercised by calling the handler with a fake instance (the
guard/token/HTTP transport is shared boilerplate covered elsewhere)."""
from __future__ import annotations

from http import HTTPStatus
from types import SimpleNamespace

from marathon import conductor
from marathon.deck.server import DeckHandler


def _fake(tmp_path, body, demo=False):
    captured: dict = {}
    fake = SimpleNamespace(
        cfg=SimpleNamespace(repo_dir=tmp_path),
        _demo=demo,
        _read_guarded_body=lambda: body,
        _send_json=lambda payload, status=HTTPStatus.OK: captured.__setitem__("json", payload),
        _send_error_json=lambda status, msg: captured.__setitem__("err", (status, msg)),
    )
    return fake, captured


def test_prover_toggle_route_registered():
    assert "/api/prover-toggle" in DeckHandler._POST_ROUTES


def test_prover_toggle_enables_and_writes_control_file(tmp_path):
    fake, captured = _fake(tmp_path, {"enabled": True})
    DeckHandler._api_prover_toggle(fake)
    assert captured["json"]["ok"] is True
    assert captured["json"]["prover_enabled"] is True
    assert conductor.read_prover_control(tmp_path) is True


def test_prover_toggle_disables(tmp_path):
    conductor.set_prover_enabled(tmp_path, True)
    fake, captured = _fake(tmp_path, {"enabled": False})
    DeckHandler._api_prover_toggle(fake)
    assert captured["json"]["prover_enabled"] is False
    assert conductor.read_prover_control(tmp_path) is False


def test_prover_toggle_rejects_non_boolean(tmp_path):
    fake, captured = _fake(tmp_path, {"enabled": "yes"})
    DeckHandler._api_prover_toggle(fake)
    assert captured["err"][0] == HTTPStatus.BAD_REQUEST
    assert "json" not in captured
    assert conductor.read_prover_control(tmp_path) is None   # nothing written


def test_prover_toggle_guard_short_circuits(tmp_path):
    # _read_guarded_body returned None (it already sent the 4xx) → handler no-ops.
    fake, captured = _fake(tmp_path, None)
    DeckHandler._api_prover_toggle(fake)
    assert captured == {}
    assert conductor.read_prover_control(tmp_path) is None


def test_prover_toggle_demo_mode_refuses(tmp_path):
    fake, captured = _fake(tmp_path, {"enabled": True}, demo=True)
    DeckHandler._api_prover_toggle(fake)
    assert captured["json"]["ok"] is False
    assert conductor.read_prover_control(tmp_path) is None
