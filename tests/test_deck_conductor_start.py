"""Manual host-conductor control exposed by the deck.

The control is deliberately opt-in and uses the deck's shared guarded-POST
preamble.  These focused tests call the handler directly for response semantics
and drive one real loopback server to pin the session-token boundary.
"""
from __future__ import annotations

import json
import urllib.error
import urllib.request
from http import HTTPStatus
from pathlib import Path
from threading import Lock, Thread
from types import SimpleNamespace

import marathon.deck.dashboard as dashboard_mod
import marathon.deck.server as deck_server
import marathon.review.review as review_mod
from marathon.deck.server import DeckHandler
from marathon.__main__ import _build_parser
from tests.test_conductor import make_cfg


def _fake(cfg, body, *, allowed=True, demo=False):
    captured: dict = {}

    def send_json(payload, status=HTTPStatus.OK):
        captured["json"] = payload
        captured["status"] = status

    server = SimpleNamespace(
        allow_host_conductor_control=allowed,
        _conductor_start_lock=Lock(),
        demo=demo,
    )
    fake = SimpleNamespace(
        cfg=cfg,
        server=server,
        _demo=demo,
        _read_guarded_body=lambda: body,
        _send_json=send_json,
        _send_error_json=lambda status, message: captured.__setitem__(
            "err", (status, message)
        ),
    )
    return fake, captured


def test_conductor_start_route_registered():
    assert "/api/conductor-start" in DeckHandler._POST_ROUTES


def test_deck_cli_exposes_explicit_host_control_opt_in():
    args = _build_parser().parse_args(["deck", "--allow-host-conductor-control"])
    assert args.allow_host_conductor_control is True


def test_conductor_status_advertises_restart_capability(tmp_path, monkeypatch):
    cfg = make_cfg(tmp_path)
    monkeypatch.setattr(
        dashboard_mod,
        "conductor_status",
        lambda _cfg: {"alive": False, "pid": None},
    )
    fake, captured = _fake(cfg, {}, allowed=True)

    DeckHandler._api_conductor_status(fake)

    assert captured["json"]["alive"] is False
    assert captured["json"]["restart_allowed"] is True


def test_conductor_status_never_offers_restart_in_demo(tmp_path, monkeypatch):
    cfg = make_cfg(tmp_path)
    monkeypatch.setattr(
        dashboard_mod,
        "conductor_status",
        lambda _cfg: {"alive": False, "pid": None},
    )
    fake, captured = _fake(cfg, {}, allowed=True, demo=True)

    DeckHandler._api_conductor_status(fake)

    assert captured["json"]["restart_allowed"] is False


def test_conductor_start_guard_short_circuits(tmp_path, monkeypatch):
    cfg = make_cfg(tmp_path)
    monkeypatch.setattr(
        review_mod,
        "start_conductor",
        lambda _cfg: (_ for _ in ()).throw(
            AssertionError("guarded request must not launch the conductor")
        ),
    )
    fake, captured = _fake(cfg, None)

    DeckHandler._api_conductor_start(fake)

    assert captured == {}


def test_conductor_start_requires_server_opt_in(tmp_path, monkeypatch):
    cfg = make_cfg(tmp_path)
    monkeypatch.setattr(
        review_mod,
        "start_conductor",
        lambda _cfg: (_ for _ in ()).throw(
            AssertionError("disabled host control must not launch")
        ),
    )
    fake, captured = _fake(cfg, {}, allowed=False)

    DeckHandler._api_conductor_start(fake)

    assert captured["err"][0] == HTTPStatus.FORBIDDEN
    assert "host" in captured["err"][1].lower()


def test_conductor_start_is_unavailable_in_demo(tmp_path, monkeypatch):
    cfg = make_cfg(tmp_path)
    monkeypatch.setattr(
        review_mod,
        "start_conductor",
        lambda _cfg: (_ for _ in ()).throw(
            AssertionError("demo mode must not launch")
        ),
    )
    fake, captured = _fake(cfg, {}, allowed=True, demo=True)

    DeckHandler._api_conductor_start(fake)

    assert captured["err"][0] in (
        HTTPStatus.BAD_REQUEST,
        HTTPStatus.FORBIDDEN,
        HTTPStatus.CONFLICT,
    )
    assert "demo" in captured["err"][1].lower()


def test_conductor_start_returns_accepted_for_new_process(tmp_path, monkeypatch):
    cfg = make_cfg(tmp_path)
    result = SimpleNamespace(
        pid=4312,
        started=True,
        log_path=Path("/tmp/conductor-4312.log"),
    )
    monkeypatch.setattr(review_mod, "start_conductor", lambda _cfg: result)
    fake, captured = _fake(cfg, {})

    DeckHandler._api_conductor_start(fake)

    assert captured["status"] == HTTPStatus.ACCEPTED
    assert captured["json"]["ok"] is True
    assert captured["json"]["started"] is True
    assert captured["json"]["pid"] == 4312


def test_conductor_start_is_idempotent_when_already_running(
    tmp_path, monkeypatch,
):
    cfg = make_cfg(tmp_path)
    result = SimpleNamespace(pid=4312, started=False, log_path=None)
    monkeypatch.setattr(review_mod, "start_conductor", lambda _cfg: result)
    fake, captured = _fake(cfg, {})

    DeckHandler._api_conductor_start(fake)

    assert captured["status"] == HTTPStatus.OK
    assert captured["json"]["ok"] is True
    assert captured["json"]["started"] is False
    assert captured["json"]["pid"] == 4312


def test_conductor_start_surfaces_launch_failure(tmp_path, monkeypatch):
    cfg = make_cfg(tmp_path)
    monkeypatch.setattr(
        review_mod,
        "start_conductor",
        lambda _cfg: (_ for _ in ()).throw(RuntimeError("cannot spawn")),
    )
    fake, captured = _fake(cfg, {})

    DeckHandler._api_conductor_start(fake)

    assert captured["err"][0] in (
        HTTPStatus.INTERNAL_SERVER_ERROR,
        HTTPStatus.SERVICE_UNAVAILABLE,
    )
    assert "cannot spawn" in captured["err"][1]


def _post(url: str, body: dict, headers=None):
    request = urllib.request.Request(
        url,
        data=json.dumps(body).encode("utf-8"),
        method="POST",
        headers={"Content-Type": "application/json", **(headers or {})},
    )
    try:
        with urllib.request.urlopen(request, timeout=5) as response:
            return response.status, json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        return exc.code, json.loads(exc.read().decode("utf-8"))


def test_conductor_start_http_route_is_session_token_protected(
    tmp_path, monkeypatch,
):
    cfg = make_cfg(tmp_path)
    launches: list[object] = []

    def start_conductor(got_cfg):
        launches.append(got_cfg)
        return SimpleNamespace(pid=8123, started=True, log_path=None)

    monkeypatch.setattr(review_mod, "start_conductor", start_conductor)
    server = deck_server.make_server(
        cfg,
        port=0,
        token="unit-test-token",
        allow_host_conductor_control=True,
        verso_manager=None,
    )
    host, port = server.server_address[:2]
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    url = f"http://{host}:{port}/api/conductor-start"
    try:
        status, _payload = _post(url, {})
        assert status == HTTPStatus.FORBIDDEN
        assert launches == []

        status, payload = _post(
            url,
            {},
            headers={deck_server.TOKEN_HEADER: "unit-test-token"},
        )
        assert status == HTTPStatus.ACCEPTED
        assert payload["ok"] is True
        assert launches == [cfg]
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)
