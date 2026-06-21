"""End-to-end smoke test of the deck's Add Chapter HTTP path: POST
/api/add-chapter starts a (mocked) intake, /api/chapter-intake-status polls it to
ready, and the token guard refuses an unauthenticated start."""
from __future__ import annotations

import json
import time
import urllib.error
import urllib.request
from pathlib import Path
from threading import Thread

import marathon.deck.server as deck_server
from marathon.deck.intake_jobs import IntakeRegistry
from marathon.review.chapter_intake import IntakeResult, ProposedSubIssue
from marathon.review.config import ReviewConfig, ReviewLabels


def _cfg(tmp_path) -> ReviewConfig:
    repo = tmp_path / "repo"
    repo.mkdir()
    return ReviewConfig(
        repo_dir=repo,
        config_path=repo / ".marathon/review/config.toml",
        github_repo="example/Demo",
        parent_issue=1,
        referee_path=repo / ".marathon/referee.md",
        target_path_template="Demo/Chapter{chapter}",
        tracker_section_pattern="### Chapter {chapter}:",
        labels=ReviewLabels(),
        chapters={},
    )


def _post(base, path, body, token):
    req = urllib.request.Request(
        base + path,
        data=json.dumps(body).encode(),
        method="POST",
        headers={"Content-Type": "application/json", deck_server.TOKEN_HEADER: token},
    )
    try:
        with urllib.request.urlopen(req, timeout=5) as r:
            return r.status, json.loads(r.read().decode())
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read().decode())


def _serve(server):
    t = Thread(target=server.serve_forever, daemon=True)
    t.start()
    host, port = server.server_address[:2]
    return t, f"http://{host}:{port}"


def test_add_chapter_http_flow(tmp_path):
    cfg = _cfg(tmp_path)

    def fake_runner(_cfg, chapter, *, informal_path=None):
        return IntakeResult(
            chapter, Path("/d.md"),
            [ProposedSubIssue(1, "T1", "b1"), ProposedSubIssue(2, "T2", "b2")],
            "raw", "m",
        )

    server = deck_server.make_server(cfg, port=0)
    server.intake = IntakeRegistry(runner=fake_runner)  # inject the mock
    thread, base = _serve(server)
    try:
        code, body = _post(base, "/api/add-chapter", {"chapter": 15}, server.token)
        assert code == 200 and body["chapter"] == 15
        assert body["status"] in ("running", "ready")

        deadline = time.monotonic() + 3
        while time.monotonic() < deadline:
            code, body = _post(
                base, "/api/chapter-intake-status", {"chapter": 15}, server.token
            )
            if body.get("status") == "ready":
                break
            time.sleep(0.02)
        assert body["status"] == "ready"
        assert [s["title"] for s in body["subissues"]] == ["T1", "T2"]
    finally:
        server.shutdown(); server.server_close(); thread.join(timeout=5)


def test_add_chapter_refused_without_valid_token(tmp_path):
    cfg = _cfg(tmp_path)
    started = []

    def fake_runner(_cfg, chapter, *, informal_path=None):
        started.append(chapter)
        return IntakeResult(chapter, Path("x"), [ProposedSubIssue(1, "t", "b")], "", "m")

    server = deck_server.make_server(cfg, port=0)
    server.intake = IntakeRegistry(runner=fake_runner)
    thread, base = _serve(server)
    try:
        code, _ = _post(base, "/api/add-chapter", {"chapter": 15}, "wrong-token")
        assert code == 403
        assert started == []  # the guard fired BEFORE any intake started
    finally:
        server.shutdown(); server.server_close(); thread.join(timeout=5)
