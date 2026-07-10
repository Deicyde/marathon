"""Concurrency and conflict-safety regressions for tracker body writers."""
from __future__ import annotations

import multiprocessing
import subprocess
import threading
import time
from pathlib import Path

import pytest

import marathon.review.tracker as tracker
from marathon.review.config import ChapterRegistry, ReviewConfig, ReviewLabels


BASE_BODY = (
    "# Project tracker\n\n"
    "Human-maintained introduction.\n\n"
    "### Chapter 14: Stuff\n"
    "- 🟠 #101 main thm\n"
    "- 🟠 #102 helper def\n"
)


def _cfg(repo: Path, github_repo: str = "example/Demo") -> ReviewConfig:
    repo.mkdir(parents=True, exist_ok=True)
    return ReviewConfig(
        repo_dir=repo,
        config_path=repo / ".marathon/review/config.toml",
        github_repo=github_repo,
        parent_issue=1,
        target_path_template="Demo/Chapter{chapter}",
        tracker_section_pattern="### Chapter {chapter}:",
        labels=ReviewLabels(),
        chapters={
            14: ChapterRegistry(
                chapter=14,
                entries=[(101, "main thm"), (102, "helper def")],
            )
        },
    )


class RemoteBody:
    def __init__(self, body: str = BASE_BODY, *, edit_delay: float = 0.0) -> None:
        self.body = body
        self.lock = threading.Lock()
        self.edit_delay = edit_delay
        self.edits: list[str] = []
        self.paths: list[Path] = []
        self.gh_timeouts: list[float | None] = []

    def read(self, _issue: int, _repo: str, **_kwargs) -> str:
        with self.lock:
            return self.body

    def gh(self, *args, **kwargs):
        assert args[:2] == ("issue", "edit")
        path = Path(args[args.index("--body-file") + 1])
        assert path.is_file()  # staged for the whole synchronous gh call
        payload = path.read_text()
        if self.edit_delay:
            time.sleep(self.edit_delay)
        with self.lock:
            self.body = payload
            self.edits.append(payload)
            self.paths.append(path)
            self.gh_timeouts.append(kwargs.get("timeout"))
        return subprocess.CompletedProcess(args, 0, "", "")


def test_concurrent_marathon_writers_preserve_both_flips(tmp_path, monkeypatch):
    cfg = _cfg(tmp_path / "repo")
    remote = RemoteBody(edit_delay=0.04)
    monkeypatch.setattr(tracker, "issue_body", remote.read)
    monkeypatch.setattr(tracker, "gh", remote.gh)
    start = threading.Barrier(2)
    results = []

    def flip(issue: int):
        start.wait(timeout=2)
        results.append(
            tracker.update_tracker_emoji(
                cfg, issue, "🟡", old_emoji="🟠", tmp_dir=tmp_path
            )
        )

    threads = [
        threading.Thread(target=flip, args=(101,)),
        threading.Thread(target=flip, args=(102,)),
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=5)

    assert sorted(ok for ok, _message in results) == [True, True]
    assert "🟡 #101 main thm" in remote.body
    assert "🟡 #102 helper def" in remote.body
    assert "Human-maintained introduction." in remote.body
    assert len(remote.edits) == 2


def test_human_edit_between_reads_is_rebased_not_overwritten(tmp_path, monkeypatch):
    cfg = _cfg(tmp_path / "repo")
    remote = RemoteBody()
    reads = 0

    def human_races(_issue: int, _repo: str, **_kwargs) -> str:
        nonlocal reads
        reads += 1
        if reads == 2:
            remote.body = remote.body.replace(
                "Human-maintained introduction.",
                "Human-maintained introduction — edited concurrently.",
            )
        return remote.body

    monkeypatch.setattr(tracker, "issue_body", human_races)
    monkeypatch.setattr(tracker, "gh", remote.gh)

    ok, _message = tracker.update_tracker_emoji(
        cfg, 101, "🟡", old_emoji="🟠", tmp_dir=tmp_path
    )

    assert ok is True
    assert reads == 5  # stale plan rejected, then plan + pre/post confirmation
    assert "edited concurrently" in remote.body
    assert "🟡 #101 main thm" in remote.body
    assert len(remote.edits) == 1


def test_continuously_changing_human_body_aborts_without_write(
    tmp_path, monkeypatch
):
    cfg = _cfg(tmp_path / "repo")
    reads = 0
    edits = []

    def always_changes(_issue: int, _repo: str, **_kwargs) -> str:
        nonlocal reads
        reads += 1
        return BASE_BODY + f"\nHuman revision {reads}.\n"

    monkeypatch.setattr(tracker, "issue_body", always_changes)
    monkeypatch.setattr(tracker, "gh", lambda *args, **kwargs: edits.append(args))

    ok, message = tracker.update_tracker_emoji(
        cfg, 101, "🟡", old_emoji="🟠", tmp_dir=tmp_path
    )

    assert ok is False
    assert "changed concurrently" in message
    assert "aborted without overwriting" in message
    assert reads == 2 * tracker._MAX_CONFLICT_RETRIES
    assert edits == []


def test_concurrent_repos_use_unique_cleaned_temp_paths(tmp_path, monkeypatch):
    cfg_a = _cfg(tmp_path / "repo-a", "example/A")
    cfg_b = _cfg(tmp_path / "repo-b", "example/B")
    bodies = {
        "example/A": BASE_BODY.replace("introduction", "introduction A"),
        "example/B": BASE_BODY.replace("introduction", "introduction B"),
    }
    barrier = threading.Barrier(2)
    seen: dict[str, tuple[Path, str]] = {}

    monkeypatch.setattr(
        tracker, "issue_body", lambda _issue, repo, **_kwargs: bodies[repo]
    )

    def gh(*args, **_kwargs):
        repo = args[args.index("--repo") + 1]
        path = Path(args[args.index("--body-file") + 1])
        barrier.wait(timeout=2)
        seen[repo] = (path, path.read_text())
        bodies[repo] = seen[repo][1]
        return subprocess.CompletedProcess(args, 0, "", "")

    monkeypatch.setattr(tracker, "gh", gh)
    errors = []

    def run(cfg):
        try:
            tracker.update_tracker_emoji(
                cfg, 101, "🟡", old_emoji="🟠", tmp_dir=tmp_path
            )
        except BaseException as exc:
            errors.append(exc)

    threads = [threading.Thread(target=run, args=(cfg,)) for cfg in (cfg_a, cfg_b)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=5)

    assert errors == []
    assert seen["example/A"][0] != seen["example/B"][0]
    assert "introduction A" in seen["example/A"][1]
    assert "introduction B" in seen["example/B"][1]
    assert all(not path.exists() for path, _body in seen.values())
    assert list(tmp_path.glob("marathon-tracker-*")) == []


def test_temp_stage_is_cleaned_when_github_edit_fails(tmp_path, monkeypatch):
    cfg = _cfg(tmp_path / "repo")
    monkeypatch.setattr(
        tracker, "issue_body", lambda _issue, _repo, **_kwargs: BASE_BODY
    )
    staged = []

    def failing_gh(*args, **_kwargs):
        path = Path(args[args.index("--body-file") + 1])
        assert path.is_file()
        staged.append(path)
        raise RuntimeError("injected GitHub failure")

    monkeypatch.setattr(tracker, "gh", failing_gh)
    with pytest.raises(RuntimeError, match="injected GitHub failure"):
        tracker.update_tracker_emoji(
            cfg, 101, "🟡", old_emoji="🟠", tmp_dir=tmp_path
        )

    assert staged and all(not path.exists() for path in staged)
    assert list(tmp_path.glob("marathon-tracker-*")) == []


def test_tracker_lock_is_reentrant_and_lives_in_ignored_runtime_dir(tmp_path):
    cfg = _cfg(tmp_path / "repo")
    lock_path = tracker._tracker_lock_path(cfg)
    assert lock_path.parent == cfg.runner_lock_dir

    # Regression: the old RLock + new-open-per-entry implementation blocked
    # forever when the inner flock contended with its own outer descriptor.
    with tracker._tracker_writer_lock(cfg):
        with tracker._tracker_writer_lock(cfg):
            assert lock_path.is_file()
            assert (lock_path.parent / ".gitignore").read_text() == "*\n"


def test_post_write_human_edit_with_requested_state_is_preserved(
    tmp_path, monkeypatch
):
    cfg = _cfg(tmp_path / "repo")
    remote = RemoteBody()
    monkeypatch.setattr(tracker, "issue_body", remote.read)

    def edit_then_human(*args, **_kwargs):
        path = Path(args[args.index("--body-file") + 1])
        payload = path.read_text()
        remote.body = payload.replace(
            "Human-maintained introduction.",
            "Human-maintained introduction — edited after PATCH.",
        )
        remote.edits.append(remote.body)
        return subprocess.CompletedProcess(args, 0, "", "")

    monkeypatch.setattr(tracker, "gh", edit_then_human)
    ok, message = tracker.update_tracker_emoji(
        cfg, 101, "🟡", old_emoji="🟠", tmp_dir=tmp_path
    )

    assert ok is True
    assert "concurrent remote edit" in message
    assert "edited after PATCH" in remote.body
    assert "🟡 #101 main thm" in remote.body
    assert len(remote.edits) == 1


def test_post_write_revert_is_rebased_and_retried(tmp_path, monkeypatch):
    cfg = _cfg(tmp_path / "repo")
    remote = RemoteBody()
    monkeypatch.setattr(tracker, "issue_body", remote.read)
    writes = 0

    def first_writer_reverts_our_flip(*args, **_kwargs):
        nonlocal writes
        writes += 1
        path = Path(args[args.index("--body-file") + 1])
        payload = path.read_text()
        if writes == 1:
            # Another checkout writes after us: it adds human prose but was
            # based on the old emoji, so our first flip disappears.
            remote.body = payload.replace(
                "Human-maintained introduction.",
                "Human-maintained introduction — competing writer.",
            ).replace("🟡 #101 main thm", "🟠 #101 main thm")
        else:
            remote.body = payload
        remote.edits.append(remote.body)
        return subprocess.CompletedProcess(args, 0, "", "")

    monkeypatch.setattr(tracker, "gh", first_writer_reverts_our_flip)
    ok, _message = tracker.update_tracker_emoji(
        cfg, 101, "🟡", old_emoji="🟠", tmp_dir=tmp_path
    )

    assert ok is True
    assert writes == 2
    assert "competing writer" in remote.body
    assert "🟡 #101 main thm" in remote.body


def test_post_write_removed_target_reports_conflict_without_overwrite(
    tmp_path, monkeypatch
):
    cfg = _cfg(tmp_path / "repo")
    remote = RemoteBody()
    monkeypatch.setattr(tracker, "issue_body", remote.read)
    writes = 0

    def competing_writer_removes_line(*args, **_kwargs):
        nonlocal writes
        writes += 1
        path = Path(args[args.index("--body-file") + 1])
        remote.body = path.read_text().replace(
            "- 🟡 #101 main thm\n", "Human replaced the target line.\n"
        )
        return subprocess.CompletedProcess(args, 0, "", "")

    monkeypatch.setattr(tracker, "gh", competing_writer_removes_line)
    ok, message = tracker.update_tracker_emoji(
        cfg, 101, "🟡", old_emoji="🟠", tmp_dir=tmp_path
    )

    assert ok is False
    assert "changed after update" in message
    assert "recovered safely" in message
    assert "Human replaced the target line." in remote.body
    assert writes == 1  # never overwrite the newer body with a guessed line


def test_lock_held_reads_are_bounded_and_timeout_releases_lock(
    tmp_path, monkeypatch
):
    cfg = _cfg(tmp_path / "repo")
    observed_timeouts = []

    def times_out(_issue, _repo, *, timeout=None):
        observed_timeouts.append(timeout)
        raise subprocess.TimeoutExpired("gh issue view", timeout)

    monkeypatch.setattr(tracker, "issue_body", times_out)
    ok, message = tracker.update_tracker_emoji(
        cfg, 101, "🟡", old_emoji="🟠", tmp_dir=tmp_path
    )
    assert ok is False and "timed out loading" in message
    assert observed_timeouts == [tracker._GITHUB_CALL_TIMEOUT_SECONDS]

    # A later writer can acquire both the thread lock and flock; the timed-out
    # path did not strand either layer.
    remote = RemoteBody()
    monkeypatch.setattr(tracker, "issue_body", remote.read)
    monkeypatch.setattr(tracker, "gh", remote.gh)
    ok, _message = tracker.update_tracker_emoji(
        cfg, 101, "🟡", old_emoji="🟠", tmp_dir=tmp_path
    )
    assert ok is True
    assert remote.gh_timeouts == [tracker._GITHUB_CALL_TIMEOUT_SECONDS]


def test_issue_body_forwards_optional_timeout_without_breaking_default(
    monkeypatch,
):
    import json
    import marathon.review.github as github

    seen = []

    def fake_gh(*args, **kwargs):
        seen.append(kwargs)
        return subprocess.CompletedProcess(
            args, 0, json.dumps({"body": "tracker"}), ""
        )

    monkeypatch.setattr(github, "gh", fake_gh)
    assert github.issue_body(1, "example/Demo") == "tracker"
    assert github.issue_body(1, "example/Demo", timeout=12.5) == "tracker"
    assert seen == [{"check": False}, {"check": False, "timeout": 12.5}]


def _process_lock_worker(lock_repo, start, inside, overlap):
    cfg = _cfg(Path(lock_repo))
    start.wait(2)
    with tracker._tracker_writer_lock(cfg):
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
def test_tracker_lock_serializes_processes(tmp_path):
    ctx = multiprocessing.get_context("fork")
    start = ctx.Event()
    inside = ctx.Value("i", 0)
    overlap = ctx.Value("i", 0)
    repo = tmp_path / "repo"
    processes = [
        ctx.Process(
            target=_process_lock_worker,
            args=(str(repo), start, inside, overlap),
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
