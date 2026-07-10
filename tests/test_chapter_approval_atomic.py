"""Atomic/resumable Add Chapter approval.

The tests exercise the old data-loss windows directly: two approval requests
for one chapter, a child created before its parent attachment fails, and two
chapters staging draft #1 at the same time.
"""
from __future__ import annotations

import json
import multiprocessing
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path
from types import SimpleNamespace

import pytest

import marathon.deck.server as deck_server
import marathon.review.subissues as sub
from marathon.deck.intake_jobs import IntakeRegistry
from marathon.review.chapter_intake import IntakeResult, ProposedSubIssue
from marathon.review.config import load_config, register_chapter


BASE_CONFIG = """\
github_repo = "example/Demo"
parent_issue = 1
target_path_template = "Demo/Chapter{chapter}"
tracker_section_pattern = "### Chapter {chapter}:"

[labels]
verified = "review:verified"
rejected = "review:rejected"
inflight = "review:in-flight-fix"
"""


def _cfg(tmp_path: Path):
    repo = tmp_path / "repo"
    config = repo / ".marathon" / "review" / "config.toml"
    config.parent.mkdir(parents=True)
    config.write_text(BASE_CONFIG)
    return load_config(repo)


def _post(base: str, path: str, body: dict, token: str):
    request = urllib.request.Request(
        base + path,
        data=json.dumps(body).encode(),
        method="POST",
        headers={
            "Content-Type": "application/json",
            deck_server.TOKEN_HEADER: token,
        },
    )
    try:
        with urllib.request.urlopen(request, timeout=5) as response:
            return response.status, json.loads(response.read().decode())
    except urllib.error.HTTPError as exc:
        return exc.code, json.loads(exc.read().decode())


def _serve(server):
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    host, port = server.server_address[:2]
    return thread, f"http://{host}:{port}"


def _ready_registry(drafts):
    proposals = [
        ProposedSubIssue(index, title, body)
        for index, (title, body) in sorted(drafts.items())
    ]

    def runner(_cfg, chapter, *, informal_path=None, use_tracker=False):
        # The nonexistent drafts path is deliberate: approval must use the
        # immutable proposal snapshot the human reviewed, not reread a file.
        return IntakeResult(
            chapter, Path("/does/not/exist.md"), proposals, "raw", "test-model"
        )

    registry = IntakeRegistry(runner=runner)
    registry.start(None, 15)
    deadline = time.monotonic() + 2
    while time.monotonic() < deadline:
        if (registry.status(15) or {}).get("status") == "ready":
            return registry
        time.sleep(0.01)
    raise AssertionError("fake intake did not become ready")


class FakeGitHub:
    """Thread-safe gh() double with issue bodies and parent attachments."""

    def __init__(self, *, create_delay: float = 0.0) -> None:
        self.lock = threading.Lock()
        self.next_issue = 100
        self.issues: dict[int, dict] = {}
        self.attachments: set[int] = set()
        self.create_calls: list[str] = []
        self.create_delay = create_delay
        self.fail_attach_once_for: int | None = None
        self.lose_create_response_once_for: str | None = None
        self.hide_recovery_list_once = False

    def __call__(self, *args, **kwargs):
        if args[:2] == ("label", "list"):
            return SimpleNamespace(returncode=0, stdout="review\nchapter-15\n")
        if args[:2] == ("label", "create"):
            return SimpleNamespace(returncode=0, stdout="")

        if args[:2] == ("issue", "create"):
            title = args[args.index("--title") + 1]
            body_path = Path(args[args.index("--body-file") + 1])
            if self.create_delay:
                time.sleep(self.create_delay)
            body = body_path.read_text()
            with self.lock:
                self.next_issue += 1
                number = self.next_issue
                url = f"https://github.com/example/Demo/issues/{number}"
                self.issues[number] = {
                    "number": number,
                    "id": f"node-{number}",
                    "html_url": url,
                    "body": body,
                    "title": title,
                }
                self.create_calls.append(title)
                lose_response = self.lose_create_response_once_for == title
                if lose_response:
                    self.lose_create_response_once_for = None
            if lose_response:
                raise RuntimeError("injected lost create response")
            return SimpleNamespace(returncode=0, stdout=url + "\n")

        if args and args[0] == "api":
            # POST parent attachment.
            if len(args) > 3 and args[1:3] == ("-X", "POST"):
                node = args[args.index("-F") + 1].split("=", 1)[1]
                number = int(node.removeprefix("node-"))
                with self.lock:
                    if self.fail_attach_once_for == number:
                        self.fail_attach_once_for = None
                        raise RuntimeError("injected parent attach failure")
                    self.attachments.add(number)
                return SimpleNamespace(returncode=0, stdout="{}")

            endpoint = next(
                (str(arg) for arg in args[1:] if str(arg).startswith("/repos/")),
                "",
            )
            if "/sub_issues?" in endpoint:
                with self.lock:
                    page = [
                        {"number": number}
                        for number in sorted(self.attachments)
                    ]
                return SimpleNamespace(returncode=0, stdout=json.dumps([page]))
            if "?state=all&labels=" in endpoint:
                with self.lock:
                    if self.hide_recovery_list_once:
                        self.hide_recovery_list_once = False
                        page = []
                    else:
                        page = [dict(item) for item in self.issues.values()]
                return SimpleNamespace(returncode=0, stdout=json.dumps([page]))

            # GET issue database/node ID before attachment.
            number = int(endpoint.rstrip("/").rsplit("/", 1)[-1])
            with self.lock:
                node = self.issues[number]["id"]
            return SimpleNamespace(returncode=0, stdout=node + "\n")

        raise AssertionError(f"unexpected gh call: {args!r} {kwargs!r}")


DRAFTS = {
    1: ("First theorem", "First reviewed body"),
    2: ("Second theorem", "Second reviewed body"),
}


def _process_lock_worker(lock_path, start, inside, overlap):
    start.wait(2)
    with sub._stable_lock(Path(lock_path)):
        with inside.get_lock():
            if inside.value:
                overlap.value = 1
            inside.value += 1
        time.sleep(0.08)
        with inside.get_lock():
            inside.value -= 1


@pytest.mark.skipif(
    "fork" not in multiprocessing.get_all_start_methods(),
    reason="cross-process advisory-lock test requires fork",
)
def test_chapter_approval_lock_serializes_processes(tmp_path):
    ctx = multiprocessing.get_context("fork")
    start = ctx.Event()
    inside = ctx.Value("i", 0)
    overlap = ctx.Value("i", 0)
    lock_path = tmp_path / "chapter-15.lock"
    processes = [
        ctx.Process(
            target=_process_lock_worker,
            args=(str(lock_path), start, inside, overlap),
        )
        for _ in range(2)
    ]
    for process in processes:
        process.start()
    start.set()
    for process in processes:
        process.join(timeout=5)

    assert [process.exitcode for process in processes] == [0, 0]
    assert overlap.value == 0


def test_concurrent_double_approve_is_single_flight(tmp_path, monkeypatch):
    cfg = _cfg(tmp_path)
    remote = FakeGitHub(create_delay=0.03)
    monkeypatch.setattr(sub, "gh", remote)
    barrier = threading.Barrier(2)
    results = []
    errors = []

    def approve():
        try:
            barrier.wait(timeout=2)
            results.append(sub.approve_chapter_drafts(cfg, 15, DRAFTS))
        except BaseException as exc:  # surfaced after joins below
            errors.append(exc)

    threads = [threading.Thread(target=approve) for _ in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=5)

    assert not errors
    assert len(results) == 2
    assert remote.create_calls == ["First theorem", "Second theorem"]
    assert results[0].created == results[1].created
    assert {result.resumed for result in results} == {False, True}
    assert load_config(cfg.repo_dir).chapters[15].entries == [
        (101, "First theorem"),
        (102, "Second theorem"),
    ]


def test_partial_attach_failure_resumes_without_duplicate_or_early_register(
    tmp_path, monkeypatch
):
    cfg = _cfg(tmp_path)
    remote = FakeGitHub()
    # Entry #2 will receive issue #102. Its create succeeds, then attachment
    # fails — the exact window that previously lost the child ID.
    remote.fail_attach_once_for = 102
    monkeypatch.setattr(sub, "gh", remote)

    with pytest.raises(sub.ChapterApprovalIncomplete) as caught:
        sub.approve_chapter_drafts(cfg, 15, DRAFTS)

    assert caught.value.retryable is True
    assert [item[:2] for item in caught.value.created] == [(1, "101"), (2, "102")]
    assert 15 not in load_config(cfg.repo_dir).chapters
    journal = json.loads(sub._approval_journal_path(cfg, 15).read_text())
    assert journal["entries"][1]["issue"] == "102"
    assert journal["entries"][1]["phase"] == "attaching"

    result = sub.approve_chapter_drafts(cfg, 15, DRAFTS)

    assert result.resumed is True
    assert remote.create_calls == ["First theorem", "Second theorem"]
    assert remote.attachments == {101, 102}
    assert load_config(cfg.repo_dir).chapters[15].entries == [
        (101, "First theorem"),
        (102, "Second theorem"),
    ]


def test_lost_create_response_recovers_marker_instead_of_creating_again(
    tmp_path, monkeypatch
):
    cfg = _cfg(tmp_path)
    remote = FakeGitHub()
    remote.lose_create_response_once_for = "First theorem"
    monkeypatch.setattr(sub, "gh", remote)

    with pytest.raises(sub.ChapterApprovalIncomplete) as caught:
        sub.approve_chapter_drafts(cfg, 15, DRAFTS)
    assert caught.value.created == []
    assert 101 in remote.issues  # GitHub succeeded; only its response was lost.

    result = sub.approve_chapter_drafts(cfg, 15, DRAFTS)

    assert result.created == [
        (1, "101", "https://github.com/example/Demo/issues/101"),
        (2, "102", "https://github.com/example/Demo/issues/102"),
    ]
    assert remote.create_calls == ["First theorem", "Second theorem"]
    assert load_config(cfg.repo_dir).chapters[15].entries == [
        (101, "First theorem"),
        (102, "Second theorem"),
    ]


def test_stale_first_marker_read_never_authorizes_duplicate_create(
    tmp_path, monkeypatch
):
    cfg = _cfg(tmp_path)
    remote = FakeGitHub()
    remote.lose_create_response_once_for = "First theorem"
    remote.hide_recovery_list_once = True
    monkeypatch.setattr(sub, "gh", remote)

    with pytest.raises(sub.ChapterApprovalIncomplete):
        sub.approve_chapter_drafts(cfg, 15, DRAFTS)
    assert remote.create_calls == ["First theorem"]

    # The first empty recovery read is treated as possible API staleness. It
    # reports retryable/incomplete and performs no second create.
    with pytest.raises(sub.ChapterApprovalIncomplete) as caught:
        sub.approve_chapter_drafts(cfg, 15, DRAFTS)
    assert "retry to recheck" in str(caught.value)
    assert remote.create_calls == ["First theorem"]

    result = sub.approve_chapter_drafts(cfg, 15, DRAFTS)
    assert [item[1] for item in result.created] == ["101", "102"]
    assert remote.create_calls == ["First theorem", "Second theorem"]


def test_http_partial_approval_reports_ids_and_retry_resumes(tmp_path, monkeypatch):
    cfg = _cfg(tmp_path)
    remote = FakeGitHub()
    remote.fail_attach_once_for = 102
    monkeypatch.setattr(sub, "gh", remote)
    server1 = deck_server.make_server(cfg, port=0)
    server1.intake = _ready_registry(DRAFTS)
    thread1, base1 = _serve(server1)
    try:
        status, partial = _post(
            base1, "/api/approve-chapter", {"chapter": 15}, server1.token
        )
        assert status == 502
        assert partial["ok"] is False
        assert partial["incomplete"] is True
        assert partial["retryable"] is True
        assert [(item["index"], item["issue"]) for item in partial["created"]] == [
            (1, "101"),
            (2, "102"),
        ]
        assert 15 not in load_config(cfg.repo_dir).chapters
    finally:
        server1.shutdown()
        server1.server_close()
        thread1.join(timeout=5)

    # A fresh deck has no in-memory IntakeResult. Starting the chapter must
    # restore the exact persisted review snapshot instead of running Claude.
    server2 = deck_server.make_server(cfg, port=0)
    thread2, base2 = _serve(server2)
    try:
        status, restored = _post(
            base2,
            "/api/add-chapter",
            {"chapter": 15, "informal_source": "tracker"},
            server2.token,
        )
        assert status == 200
        assert restored["status"] == "approval-incomplete"
        assert [item["body"] for item in restored["subissues"]] == [
            "First reviewed body",
            "Second reviewed body",
        ]
        assert [(item["index"], item["issue"]) for item in restored["created"]] == [
            (1, "101"),
            (2, "102"),
        ]

        status, complete = _post(
            base2, "/api/approve-chapter", {"chapter": 15}, server2.token
        )
        assert status == 200
        assert complete["ok"] is True and complete["resumed"] is True
        assert remote.create_calls == ["First theorem", "Second theorem"]
        assert load_config(cfg.repo_dir).chapters[15].entries == [
            (101, "First theorem"),
            (102, "Second theorem"),
        ]
        status, already_done = _post(
            base2,
            "/api/add-chapter",
            {"chapter": 15, "informal_source": "tracker"},
            server2.token,
        )
        assert status == 200
        assert already_done["status"] == "approval-complete"
        assert remote.create_calls == ["First theorem", "Second theorem"]
    finally:
        server2.shutdown()
        server2.server_close()
        thread2.join(timeout=5)


def test_start_intake_fails_fast_for_legacy_registered_chapter(tmp_path):
    cfg = _cfg(tmp_path)
    register_chapter(
        cfg.repo_dir,
        15,
        "Demo/Chapter15",
        [(77, "Already reviewed")],
    )
    started = []

    def runner(*_args, **_kwargs):
        started.append(True)
        raise AssertionError("registered chapter must not launch intake")

    server = deck_server.make_server(cfg, port=0)
    server.intake = IntakeRegistry(runner=runner)
    thread, base = _serve(server)
    try:
        status, body = _post(
            base,
            "/api/add-chapter",
            {"chapter": 15, "informal_source": "tracker"},
            server.token,
        )
        assert status == 409
        assert "already registered" in body["error"]
        assert started == []
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def test_concurrent_chapters_use_isolated_body_files(tmp_path, monkeypatch):
    cfg = _cfg(tmp_path)
    first = tmp_path / "Chapter15.md"
    second = tmp_path / "Chapter16.md"
    first.write_text(
        "## 1/1 — first\n\n**Title**: `Chapter 15 draft`\n\nBODY-FIFTEEN\n"
    )
    second.write_text(
        "## 1/1 — second\n\n**Title**: `Chapter 16 draft`\n\nBODY-SIXTEEN\n"
    )

    body_barrier = threading.Barrier(2)
    seen = {}

    def fake_create(_cfg, title, body_path, chapter):
        # Both calls reach this point before either reads its path. With the old
        # shared /tmp/review-draft-1-body.md, both would read the last writer.
        body_barrier.wait(timeout=2)
        seen[chapter] = (Path(body_path), Path(body_path).read_text())
        return str(chapter), f"https://example.test/issues/{chapter}"

    monkeypatch.setattr(sub, "create_subissue", fake_create)
    monkeypatch.setattr(sub, "_ensure_chapter_label", lambda *_args: None)
    monkeypatch.setattr(
        sub,
        "gh",
        lambda *_args, **_kwargs: SimpleNamespace(returncode=0, stdout=""),
    )
    errors = []

    def create(path):
        try:
            sub.create_subissues_from_drafts(cfg, path, tmp_dir=tmp_path)
        except BaseException as exc:
            errors.append(exc)

    threads = [
        threading.Thread(target=create, args=(first,)),
        threading.Thread(target=create, args=(second,)),
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=5)

    assert not errors
    assert seen[15][0] != seen[16][0]
    assert "BODY-FIFTEEN" in seen[15][1]
    assert "BODY-SIXTEEN" in seen[16][1]
