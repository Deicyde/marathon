"""HTTP boundary tests for the deck's generated Verso artifacts.

The artifact builder itself is covered in ``test_deck_verso.py``.  These tests
keep the web-server contract honest: builds stay off request threads, only the
manager-authorized last-good tree is served, and demo/unconfigured decks
degrade without pretending semantic documentation exists.
"""

from __future__ import annotations

import json
import time
import urllib.error
import urllib.request
from contextlib import contextmanager
from pathlib import Path
from threading import Event, Thread

import pytest

import marathon.deck.cards as cards
from marathon.deck import server as deck_server
from marathon.deck.verso import VersoArtifactManager, effective_artifact_token
from marathon.review.config import ChapterRegistry, ReviewConfig, ReviewLabels


def _cfg(tmp_path: Path) -> ReviewConfig:
    repo = tmp_path / "repo"
    repo.mkdir()
    return ReviewConfig(
        repo_dir=repo,
        config_path=repo / ".marathon/review/config.toml",
        github_repo="example/Demo",
        parent_issue=1,
        target_path_template="Demo/Chapter{chapter}",
        tracker_section_pattern="### Chapter {chapter}:",
        labels=ReviewLabels(),
        chapters={
            14: ChapterRegistry(chapter=14, entries=[(22, "Def Foo")]),
        },
    )


class _FakeVersoManager:
    def __init__(self, root: Path):
        self.root = root
        self.root.mkdir(parents=True)
        self.status_value = {
            "state": "ready",
            "available": True,
            "stale": False,
            "source_token": "source-2",
            "artifact_token": "artifact-2",
            "error": None,
        }
        self.manifest_value = {
            "constants": {
                "Demo.Foo": {"url": "/verso/Demo.html", "anchor": "Demo.Foo"}
            },
            "modules": {"Demo": {"url": "/verso/Demo.html"}},
            "artifact_token": "artifact-2",
        }
        self.status_calls: list[bool] = []
        self.trigger_calls: list[bool] = []
        self.manifest_calls: list[bool] = []
        self.resolve_calls: list[str] = []
        self.resolve_tokens: list[str | None] = []
        self.shutdown_calls: list[bool] = []
        self.worker_started = Event()
        self.release_worker = Event()
        self._worker_started = False

    def status(self, *, trigger: bool = True) -> dict:
        self.status_calls.append(trigger)
        if trigger and not self._worker_started:
            self._worker_started = True

            def work() -> None:
                self.worker_started.set()
                self.release_worker.wait(timeout=5)

            Thread(target=work, daemon=True).start()
        return dict(self.status_value)

    def trigger(self, *, force: bool = False) -> dict:
        self.trigger_calls.append(force)
        self.status_value.update({"state": "building", "error": None})
        return dict(self.status_value)

    def manifest(self, *, trigger: bool = True):
        self.manifest_calls.append(trigger)
        if self.manifest_value is None:
            return None
        return json.loads(json.dumps(self.manifest_value))

    def resolve_artifact(self, raw_path: str, *, artifact_token=None):
        self.resolve_calls.append(raw_path)
        self.resolve_tokens.append(artifact_token)
        candidate = (self.root / raw_path).resolve()
        if raw_path.endswith("/"):
            candidate /= "index.html"
        try:
            candidate.relative_to(self.root.resolve())
        except ValueError:
            return None
        return candidate if candidate.is_file() else None

    def shutdown(self, *, wait: bool = False) -> None:
        self.shutdown_calls.append(wait)
        self.release_worker.set()


@contextmanager
def _running(cfg: ReviewConfig, manager):
    server = deck_server.make_server(
        cfg, port=0, default_chapter=14, verso_manager=manager
    )
    host, port = server.server_address[:2]
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield server, f"http://{host}:{port}"
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def _get(base: str, path: str):
    request = urllib.request.Request(base + path)
    try:
        with urllib.request.urlopen(request, timeout=5) as response:
            return response.status, response.headers, response.read()
    except urllib.error.HTTPError as exc:
        return exc.code, exc.headers, exc.read()


def _post(base: str, path: str, token: str):
    request = urllib.request.Request(
        base + path,
        data=b"{}",
        method="POST",
        headers={
            "Content-Type": "application/json",
            deck_server.TOKEN_HEADER: token,
        },
    )
    try:
        with urllib.request.urlopen(request, timeout=5) as response:
            return response.status, response.headers, response.read()
    except urllib.error.HTTPError as exc:
        return exc.code, exc.headers, exc.read()


def test_status_triggers_background_build_without_delaying_queue(
    tmp_path, monkeypatch,
):
    manager = _FakeVersoManager(tmp_path / "artifact")
    manager.status_value.update({
        "state": "building", "available": False, "artifact_token": None
    })

    class _Queue:
        def to_json(self):
            return {
                "cards": [],
                "building": 0,
                "landed_today": 0,
                "view_building": 0,
                "unresolved": 0,
                "awaiting_refinement": 0,
                "needs_attention": 0,
                "needs_citation_mapping": 0,
                "awaiting_audit": 0,
                "dependency_blocked": 0,
                "other_unresolved": 0,
            }

    monkeypatch.setattr(cards, "build_queue", lambda _cfg, _chapter: _Queue())

    with _running(_cfg(tmp_path), manager) as (_server, base):
        started = time.monotonic()
        code, headers, raw = _get(base, "/api/verso-status")
        elapsed = time.monotonic() - started
        assert code == 200
        assert elapsed < 1
        assert manager.worker_started.wait(timeout=1)
        payload = json.loads(raw)
        assert payload == {
            "state": "building",
            "available": False,
            "stale": False,
            "source_token": "source-2",
            "artifact_token": None,
            "error": None,
            "root_url": "/verso/",
        }
        assert manager.status_calls == [True]
        assert headers["Cache-Control"] == "no-store"
        assert headers["X-Content-Type-Options"] == "nosniff"

        # The fake build remains blocked, but an unrelated deck endpoint is
        # immediately usable and never consults the artifact manager.
        started = time.monotonic()
        queue_code, _queue_headers, queue_raw = _get(base, "/api/queue")
        assert time.monotonic() - started < 1
        assert queue_code == 200
        assert json.loads(queue_raw)["cards"] == []
        assert manager.status_calls == [True]


def test_server_verso_prewarm_is_nonblocking_single_flight_boundary(tmp_path):
    manager = _FakeVersoManager(tmp_path / "artifact")
    server = deck_server.make_server(
        _cfg(tmp_path), port=0, default_chapter=14, verso_manager=manager
    )
    try:
        started = time.monotonic()
        status = server.prewarm_verso()
        assert time.monotonic() - started < 1
        assert status["state"] == "building"
        assert manager.trigger_calls == [False]
    finally:
        server.server_close()


def test_manifest_returns_last_good_artifact_without_starting_a_build(tmp_path):
    manager = _FakeVersoManager(tmp_path / "artifact")
    # A stale manifest remains useful while a replacement build is running.
    manager.status_value.update({"state": "building", "stale": True})
    with _running(_cfg(tmp_path), manager) as (_server, base):
        code, headers, raw = _get(base, "/api/verso-manifest")
        assert code == 200
        assert json.loads(raw) == {**manager.manifest_value, "root_url": "/verso/"}
        assert manager.manifest_calls == [False]
        assert manager.status_calls == []
        assert headers["Content-Type"] == "application/json; charset=utf-8"
        assert headers["Cache-Control"] == "no-store"


@pytest.mark.parametrize(
    ("state", "expected"), [("building", 503), ("idle", 404), ("error", 503)]
)
def test_missing_manifest_reports_honest_state(tmp_path, state, expected):
    manager = _FakeVersoManager(tmp_path / "artifact")
    manager.manifest_value = None
    manager.status_value.update({
        "state": state,
        "available": False,
        "error": "build failed" if state == "error" else None,
    })
    with _running(_cfg(tmp_path), manager) as (_server, base):
        code, headers, raw = _get(base, "/api/verso-manifest")
        assert code == expected
        payload = json.loads(raw)
        assert payload["ok"] is False
        assert payload["state"] == state
        assert payload["available"] is False
        assert manager.manifest_calls == [False]
        assert manager.status_calls == [False]
        assert headers["Cache-Control"] == "no-store"


def test_failed_verso_build_has_an_explicit_token_guarded_retry(tmp_path):
    manager = _FakeVersoManager(tmp_path / "artifact")
    manager.status_value.update({
        "state": "error", "available": False, "artifact_token": None,
        "error": "temporary dependency failure",
    })
    with _running(_cfg(tmp_path), manager) as (server, base):
        denied, _headers, _raw = _post(base, "/api/verso-retry", "wrong")
        assert denied == 403
        assert manager.trigger_calls == []

        code, headers, raw = _post(base, "/api/verso-retry", server.token)
        assert code == 200
        payload = json.loads(raw)
        assert payload["ok"] is True
        assert payload["state"] == "building"
        assert payload["error"] is None
        assert manager.trigger_calls == [True]
        assert manager.status_calls == [False]
        assert headers["Cache-Control"] == "no-store"


@pytest.mark.parametrize(
    ("name", "content_type"),
    [
        ("Demo.html", "text/html; charset=utf-8"),
        ("assets/site.css", "text/css; charset=utf-8"),
        ("assets/site.js", "application/javascript; charset=utf-8"),
        ("manifest.json", "application/json; charset=utf-8"),
    ],
)
def test_verso_static_files_have_safe_headers_and_mime(
    tmp_path, name, content_type,
):
    manager = _FakeVersoManager(tmp_path / "artifact")
    target = manager.root / name
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(b"artifact body")
    with _running(_cfg(tmp_path), manager) as (_server, base):
        code, headers, raw = _get(base, "/verso/" + name)
        assert code == 200
        assert raw == b"artifact body"
        assert headers["Content-Type"] == content_type
        assert headers["Cache-Control"] == "no-store"
        assert headers["X-Content-Type-Options"] == "nosniff"
        if name.endswith(".html"):
            csp = headers["Content-Security-Policy"]
            directives = [part.strip() for part in csp.split(";") if part.strip()]
            # Direct Verso pages sit behind oauth2-proxy.  They must retain
            # their origin so stylesheet requests carry the authenticated
            # identity, while generated artifact scripts remain disabled.
            assert [
                directive for directive in directives
                if directive.split()[0] == "sandbox"
            ] == ["sandbox allow-same-origin"]
            assert "allow-scripts" not in csp
            assert "script-src 'none'" in csp
            assert "connect-src 'none'" in csp
            assert "form-action 'none'" in csp
            assert "base-uri 'self'" in csp
            assert "style-src 'self' 'unsafe-inline'" in csp
            assert headers["Cross-Origin-Opener-Policy"] == "same-origin"
            assert headers["X-Frame-Options"] == "DENY"
        else:
            assert headers.get("Content-Security-Policy") is None


@pytest.mark.parametrize("name", ["diagram.svg", "payload.xml"])
def test_active_non_html_verso_documents_are_also_sandboxed(tmp_path, name):
    manager = _FakeVersoManager(tmp_path / "artifact")
    manager.root.joinpath(name).write_text("<script>alert(1)</script>")
    with _running(_cfg(tmp_path), manager) as (_server, base):
        code, headers, _raw = _get(base, "/verso/" + name)
        assert code == 200
        csp = headers["Content-Security-Policy"]
        directives = [part.strip() for part in csp.split(";") if part.strip()]
        assert [
            directive for directive in directives
            if directive.split()[0] == "sandbox"
        ] == ["sandbox allow-same-origin"]
        assert "allow-scripts" not in csp
        assert "script-src 'none'" in csp
        assert headers["X-Frame-Options"] == "DENY"


def test_verso_static_route_pins_the_manifest_artifact_token(tmp_path):
    token = "a" * 64
    manager = _FakeVersoManager(tmp_path / "artifact")
    target = manager.root / "Demo" / "index.html"
    target.parent.mkdir(parents=True)
    target.write_text("pinned artifact", encoding="utf-8")
    manager.status_value["artifact_token"] = token
    manager.manifest_value["artifact_token"] = token

    with _running(_cfg(tmp_path), manager) as (_server, base):
        code, _headers, raw = _get(base, f"/verso/{token}/Demo/")
        assert code == 200
        assert raw == b"pinned artifact"
        assert manager.resolve_calls == ["Demo/"]
        assert manager.resolve_tokens == [token]

        code, _headers, raw = _get(base, "/api/verso-manifest")
        assert code == 200
        assert json.loads(raw)["root_url"] == f"/verso/{token}/"


def test_token_pinned_definition_deep_link_serves_blueprint_page_and_css(tmp_path):
    """The URL fragment stays in the browser; its page and CSS must both be
    available beneath the same authenticated, token-pinned Verso mount."""
    token = "a" * 64
    manager = _FakeVersoManager(tmp_path / "artifact")
    module_path = "GeometricAnalysis/LeeSM/Chapter16/IntegrationRn/"
    page = manager.root / module_path / "index.html"
    page.parent.mkdir(parents=True)
    page.write_text(
        '<!doctype html><head><base href="../../../../">'
        '<link rel="stylesheet" href="code.css"></head>'
        '<body><span id="IsOrientedDiffeomorphOn">structure</span></body>',
        encoding="utf-8",
    )
    manager.root.joinpath("code.css").write_text(
        ".hl.lean { color: blue; }", encoding="utf-8"
    )

    with _running(_cfg(tmp_path), manager) as (_server, base):
        code, headers, raw = _get(base, f"/verso/{token}/{module_path}")
        assert code == 200
        assert headers["Content-Type"] == "text/html; charset=utf-8"
        assert b'<base href="../../../../">' in raw
        assert b'id="IsOrientedDiffeomorphOn"' in raw

        code, headers, raw = _get(base, f"/verso/{token}/code.css")
        assert code == 200
        assert headers["Content-Type"] == "text/css; charset=utf-8"
        assert raw == b".hl.lean { color: blue; }"
        assert manager.resolve_calls == [module_path, "code.css"]
        assert manager.resolve_tokens == [token, token]


def test_verso_directory_url_preserves_slash_for_index_resolution(tmp_path):
    manager = _FakeVersoManager(tmp_path / "artifact")
    target = manager.root / "Demo" / "index.html"
    target.parent.mkdir(parents=True)
    target.write_bytes(b"module page")
    with _running(_cfg(tmp_path), manager) as (_server, base):
        code, _headers, raw = _get(base, "/verso/Demo/")
        assert code == 200
        assert raw == b"module page"
        assert manager.resolve_calls == ["Demo/"]


@pytest.mark.parametrize(
    "path",
    [
        "/verso/%2e%2e/secret.txt",
        "/verso/a/%2E%2E/secret.txt",
        "/verso/%2Fetc%2Fpasswd",
        "/verso/a%5c..%5csecret.txt",
        "/verso/%ff.html",
    ],
)
def test_verso_route_rejects_encoded_traversal_before_resolution(tmp_path, path):
    manager = _FakeVersoManager(tmp_path / "artifact")
    with _running(_cfg(tmp_path), manager) as (_server, base):
        code, headers, raw = _get(base, path)
        assert code == 403
        assert json.loads(raw)["ok"] is False
        assert manager.resolve_calls == []
        assert headers["Cache-Control"] == "no-store"
        assert headers["X-Content-Type-Options"] == "nosniff"


def test_demo_mode_is_honestly_unavailable(tmp_path):
    cfg = _cfg(tmp_path)
    server = deck_server.make_server(cfg, port=0, demo=True)
    host, port = server.server_address[:2]
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    base = f"http://{host}:{port}"
    try:
        code, _headers, raw = _get(base, "/api/verso-status")
        assert code == 200
        payload = json.loads(raw)
        assert payload["state"] == "unavailable"
        assert payload["available"] is False
        assert "demo mode" in payload["error"]

        code, _headers, raw = _get(base, "/api/verso-manifest")
        assert code == 503
        assert json.loads(raw)["ok"] is False

        code, _headers, raw = _get(base, "/verso/Demo.html")
        assert code == 503
        assert json.loads(raw)["ok"] is False
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def test_server_owns_manager_and_closes_it_once(tmp_path):
    manager = _FakeVersoManager(tmp_path / "artifact")
    server = deck_server.make_server(
        _cfg(tmp_path), port=0, verso_manager=manager
    )
    server.server_close()
    server.server_close()
    assert manager.shutdown_calls == [False]


def test_http_contract_is_compatible_with_real_artifact_manager(tmp_path):
    """Exercise the actual manager API over HTTP using a seeded last-good site."""
    cfg = _cfg(tmp_path)
    cfg.repo_dir.joinpath("lakefile.toml").write_text(
        'name = "demo"\n\n[[lean_lib]]\nname = "Demo"\n', encoding="utf-8"
    )
    cfg.repo_dir.joinpath("lean-toolchain").write_text(
        "leanprover/lean4:v4.28.0\n", encoding="utf-8"
    )
    source_token = "a" * 64
    root = tmp_path / "real-manager"
    probe = VersoArtifactManager(
        cfg.repo_dir,
        runner=lambda *args, **kwargs: pytest.fail("probe must not build"),
        source_token_fn=lambda _repo: source_token,
        root=root,
    )
    dependency_signature = probe._dependency_signature()
    token = effective_artifact_token(source_token, dependency_signature)
    artifact = root / "artifacts" / token
    page = artifact / "site" / "Demo" / "index.html"
    page.parent.mkdir(parents=True)
    page.write_text("<title>Demo</title><main>semantic Demo</main>", encoding="utf-8")
    manifest = {
        "artifact_token": token,
        "root_url": f"/verso/{token}/",
        "constants": {},
        "modules": {"Demo": {"url": "Demo/"}},
    }
    artifact.joinpath("manifest.json").write_text(
        json.dumps(manifest), encoding="utf-8"
    )
    artifact.joinpath("metadata.json").write_text(
        json.dumps({
            "artifact_token": token,
            "source_token": source_token,
            "dependency_signature": dependency_signature,
        }),
        encoding="utf-8",
    )
    root.joinpath("state.json").write_text(
        json.dumps({"artifact_token": token}), encoding="utf-8"
    )
    manager = VersoArtifactManager(
        cfg.repo_dir,
        runner=lambda *args, **kwargs: pytest.fail("seeded artifact must not build"),
        source_token_fn=lambda _repo: source_token,
        root=root,
    )

    with _running(cfg, manager) as (_server, base):
        code, _headers, raw = _get(base, "/api/verso-status")
        assert code == 200
        assert json.loads(raw)["state"] == "ready"

        code, _headers, raw = _get(base, "/api/verso-manifest")
        assert code == 200
        assert json.loads(raw) == manifest

        code, _headers, raw = _get(base, f"/verso/{token}/Demo/")
        assert code == 200
        assert b"semantic Demo" in raw
