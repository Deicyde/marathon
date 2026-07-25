"""Crash-safe background shipping for the review deck.

Shipping can spend tens of seconds talking to GitHub.  The request handler
must therefore durably record the intent and return immediately while a
single background worker performs the existing, idempotent ``ship_issue``
operation.  These tests stay below the HTTP layer so the important guarantees
remain explicit and deterministic:

* acknowledgement is fast and the running job already exists on disk;
* two starts for the same review generation coalesce into one worker;
* success and failure are durable across registry reconstruction;
* an orphaned ``running`` record is honestly reported as ``awaiting_retry``;
* retrying a failed job reuses its operation id and preserves review state;
* the request-scoped GitHub actor reaches the worker but is never persisted.
"""
from __future__ import annotations

import json
import threading
import time

import marathon.review.github as github
from marathon.deck import subdeck
from marathon.deck import ship_jobs
from marathon.deck.ship_jobs import ShipJobConflict, ShipJobRegistry
from marathon.review.config import ReviewConfig, ReviewLabels


def _cfg(tmp_path) -> ReviewConfig:
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
    )


def _wait_for(registry, cfg, issue: int, wanted: str, timeout: float = 3.0):
    deadline = time.monotonic() + timeout
    latest = None
    while time.monotonic() < deadline:
        latest = registry.status(cfg, issue)
        if latest is not None and latest["status"] == wanted:
            return latest
        time.sleep(0.01)
    raise AssertionError(
        f"ship #{issue} never reached {wanted!r}; latest snapshot: {latest!r}"
    )


def _job_record(cfg, issue: int) -> dict:
    path = cfg.runner_log_dir / "deck-ship-jobs" / f"{issue}.json"
    assert path.is_file(), "start must persist the job before acknowledging it"
    return json.loads(path.read_text(encoding="utf-8"))


def test_start_acknowledges_quickly_after_durable_write(tmp_path):
    cfg = _cfg(tmp_path)
    entered = threading.Event()
    release = threading.Event()

    def slow_runner(cfg_arg, issue, *, skip_api=False):
        assert cfg_arg is cfg
        assert issue == 171
        assert skip_api is True
        entered.set()
        assert release.wait(3)
        return {"ok": True, "outcome": "shipped"}

    registry = ShipJobRegistry(runner=slow_runner)
    try:
        before = time.perf_counter()
        snapshot = registry.start(cfg, 171, skip_api=True)
        elapsed = time.perf_counter() - before

        assert elapsed < 0.5
        assert snapshot["issue"] == 171
        assert snapshot["status"] == "running"
        assert snapshot["operation_id"]
        record = _job_record(cfg, 171)
        assert record["operation_id"] == snapshot["operation_id"]
        assert record["status"] == "running"
        assert entered.wait(1)
    finally:
        release.set()

    completed = _wait_for(registry, cfg, 171, "succeeded")
    assert completed["result"] == {"ok": True, "outcome": "shipped"}


def test_unknown_status_is_a_pure_read(tmp_path):
    cfg = _cfg(tmp_path)
    registry = ShipJobRegistry(runner=lambda *_a, **_kw: {"ok": True})

    assert registry.status(cfg, 999) is None
    assert not (
        cfg.runner_log_dir / "deck-ship-jobs"
    ).exists()


def test_same_generation_double_start_coalesces_to_one_worker(tmp_path):
    cfg = _cfg(tmp_path)
    entered = threading.Event()
    release = threading.Event()
    calls: list[int] = []

    def slow_runner(_cfg, issue, *, skip_api=False):
        calls.append(issue)
        entered.set()
        assert release.wait(3)
        return {"ok": True}

    registry = ShipJobRegistry(runner=slow_runner)
    try:
        first = registry.start(cfg, 240)
        assert entered.wait(1)
        second = registry.start(cfg, 240)

        assert first["operation_id"] == second["operation_id"]
        assert second["status"] == "running"
        assert calls == [240]
    finally:
        release.set()

    _wait_for(registry, cfg, 240, "succeeded")
    assert calls == [240]


def test_live_job_refuses_a_different_skip_api_generation(tmp_path):
    cfg = _cfg(tmp_path)
    entered = threading.Event()
    release = threading.Event()

    def slow_runner(_cfg, _issue, *, skip_api=False):
        entered.set()
        assert release.wait(3)
        return {"ok": True}

    registry = ShipJobRegistry(runner=slow_runner)
    try:
        registry.start(cfg, 171, skip_api=False)
        assert entered.wait(1)
        try:
            registry.start(cfg, 171, skip_api=True)
        except ShipJobConflict:
            pass
        else:
            raise AssertionError("a different live generation must not coalesce")
    finally:
        release.set()


def test_mutation_after_acceptance_prevents_stale_ship(tmp_path):
    cfg = _cfg(tmp_path)
    runner_called = threading.Event()
    from marathon.deck.subdeck import _issue_operation_lock

    registry = ShipJobRegistry(
        runner=lambda *_args, **_kwargs: (
            runner_called.set() or {"ok": True}
        )
    )
    with _issue_operation_lock(cfg, 171):
        started = registry.start(cfg, 171)
        subdeck.record_decl_verdict(
            cfg, 171, "Demo.New", "reject", note="newer decision"
        )

    failed = _wait_for(registry, cfg, 171, "failed")
    assert failed["operation_id"] == started["operation_id"]
    assert "review changed" in failed["error"].lower()
    assert not runner_called.is_set()


def test_source_change_after_acceptance_prevents_stale_ship(tmp_path):
    cfg = _cfg(tmp_path)
    source = cfg.repo_dir / "Demo.lean"
    source.write_text("def reviewed : Nat := 0\n")
    runner_called = threading.Event()
    from marathon.deck.subdeck import _issue_operation_lock

    registry = ShipJobRegistry(
        runner=lambda *_args, **_kwargs: (
            runner_called.set() or {"ok": True}
        )
    )
    with _issue_operation_lock(cfg, 171):
        registry.start(cfg, 171)
        source.write_text("def reviewed : Nat := 100\n")

    failed = _wait_for(registry, cfg, 171, "failed")
    assert "review changed" in failed["error"].lower()
    assert not runner_called.is_set()


def test_failed_outbox_write_never_launches_worker(tmp_path, monkeypatch):
    cfg = _cfg(tmp_path)
    launched = threading.Event()

    def runner(_cfg, _issue, *, skip_api=False):
        launched.set()
        return {"ok": True}

    def fail_write(_path, _payload):
        raise OSError("disk full")

    monkeypatch.setattr(ship_jobs, "_atomic_write", fail_write)
    registry = ShipJobRegistry(runner=runner)

    try:
        registry.start(cfg, 171)
    except OSError as exc:
        assert "disk full" in str(exc)
    else:
        raise AssertionError("an unpersisted job must not be acknowledged")
    assert not launched.wait(0.05)


def test_terminal_status_waits_for_completion_callback(tmp_path):
    cfg = _cfg(tmp_path)
    callback_entered = threading.Event()
    release_callback = threading.Event()

    def on_complete():
        callback_entered.set()
        assert release_callback.wait(3)

    registry = ShipJobRegistry(
        runner=lambda *_args, **_kwargs: {"ok": True}
    )
    registry.start(cfg, 171, on_complete=on_complete)
    assert callback_entered.wait(1)
    assert registry.status(cfg, 171)["status"] == "running"
    release_callback.set()
    assert _wait_for(registry, cfg, 171, "succeeded")["status"] == "succeeded"


def test_thread_factory_failure_is_durable(tmp_path):
    cfg = _cfg(tmp_path)

    def broken_factory(**_kwargs):
        raise RuntimeError("threads unavailable")

    registry = ShipJobRegistry(
        runner=lambda *_args, **_kwargs: {"ok": True},
        thread_factory=broken_factory,
    )
    try:
        registry.start(cfg, 171)
    except RuntimeError as exc:
        assert "threads unavailable" in str(exc)
    else:
        raise AssertionError("thread construction should fail")
    failed = registry.status(cfg, 171)
    assert failed["status"] == "failed"
    assert "could not be started" in failed["error"]


def test_success_is_durable_and_actor_token_is_never_written(tmp_path):
    cfg = _cfg(tmp_path)
    seen_actor_tokens: list[str | None] = []
    secret = "ghu_request_scoped_secret"

    def runner(_cfg, _issue, *, skip_api=False):
        # ContextVars do not propagate to a new thread automatically.  The
        # registry must deliberately bind the token inside its worker.
        seen_actor_tokens.append(github._ACTOR_GH_TOKEN.get())
        return {"ok": True, "outcome": "verified", "skip_api": skip_api}

    registry = ShipJobRegistry(runner=runner)
    started = registry.start(cfg, 171, actor_token=secret)
    finished = _wait_for(registry, cfg, 171, "succeeded")
    assert seen_actor_tokens == [secret]
    assert finished["operation_id"] == started["operation_id"]

    raw_record = (
        cfg.runner_log_dir / "deck-ship-jobs" / "171.json"
    ).read_text(encoding="utf-8")
    assert secret not in raw_record

    # A new registry has no in-memory job, yet can serve the completed result
    # entirely from the durable outbox record.
    reconstructed = ShipJobRegistry(
        runner=lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("reading a completed job must not rerun it")
        )
    )
    recovered = reconstructed.status(
        cfg, 171, operation_id=started["operation_id"]
    )
    assert recovered["status"] == "succeeded"
    assert recovered["result"]["outcome"] == "verified"


def test_review_surface_token_reaches_authoritative_runner(tmp_path):
    cfg = _cfg(tmp_path)
    expected = "a" * 64
    seen: list[str | None] = []

    def runner(
        _cfg,
        _issue,
        *,
        skip_api=False,
        expected_review_token=None,
    ):
        seen.append(expected_review_token)
        return {"ok": True}

    registry = ShipJobRegistry(runner=runner)
    registry.start(cfg, 171, review_token=expected)
    _wait_for(registry, cfg, 171, "succeeded")
    assert seen == [expected]


def test_actor_token_is_redacted_from_worker_failure(tmp_path):
    cfg = _cfg(tmp_path)
    secret = "ghu_do_not_persist"

    def runner(_cfg, _issue, *, skip_api=False):
        raise RuntimeError(f"upstream accidentally echoed {secret}")

    registry = ShipJobRegistry(runner=runner)
    registry.start(cfg, 171, actor_token=f"  {secret}  ")
    failed = _wait_for(registry, cfg, 171, "failed")
    assert secret not in failed["error"]
    raw = (
        cfg.runner_log_dir / "deck-ship-jobs" / "171.json"
    ).read_text(encoding="utf-8")
    assert secret not in raw


def test_orphaned_running_record_becomes_awaiting_retry(tmp_path):
    cfg = _cfg(tmp_path)
    entered = threading.Event()
    release = threading.Event()

    def blocked_runner(_cfg, _issue, *, skip_api=False):
        entered.set()
        assert release.wait(3)
        return {"ok": True}

    original = ShipJobRegistry(runner=blocked_runner)
    try:
        started = original.start(cfg, 171)
        assert entered.wait(1)

        # This models reconstructing DeckServer after a process restart: its
        # registry has no corresponding live thread, so it must not pretend the
        # persisted "running" state is still making progress.
        reconstructed = ShipJobRegistry(runner=blocked_runner)
        recovered = reconstructed.status(
            cfg, 171, operation_id=started["operation_id"]
        )
        assert recovered["status"] == "awaiting_retry"
        assert recovered["operation_id"] == started["operation_id"]
    finally:
        release.set()


def test_failed_job_is_durable_and_retry_reuses_operation(tmp_path):
    cfg = _cfg(tmp_path)
    decl = "Demo.Chapter15.Theorem"
    subdeck.record_decl_verdict(
        cfg, 171, decl, "reject", note="the conclusion is too weak"
    )

    def fail_runner(_cfg, _issue, *, skip_api=False):
        raise RuntimeError("GitHub timed out")

    first_registry = ShipJobRegistry(runner=fail_runner)
    first = first_registry.start(cfg, 171, actor_token="ghu_old")
    failed = _wait_for(first_registry, cfg, 171, "failed")
    assert failed["operation_id"] == first["operation_id"]
    assert "GitHub timed out" in failed["error"]
    assert subdeck.issue_verdicts(cfg, 171)[decl]["note"] \
        == "the conclusion is too weak"

    seen_tokens: list[str | None] = []

    def succeed_runner(_cfg, _issue, *, skip_api=False):
        seen_tokens.append(github._ACTOR_GH_TOKEN.get())
        return {"ok": True, "outcome": "shipped"}

    retry_registry = ShipJobRegistry(runner=succeed_runner)
    on_disk = retry_registry.status(cfg, 171)
    assert on_disk["status"] == "failed"

    retried = retry_registry.start(cfg, 171, actor_token="ghu_fresh")
    assert retried["operation_id"] == first["operation_id"]
    completed = _wait_for(retry_registry, cfg, 171, "succeeded")
    assert completed["result"]["outcome"] == "shipped"
    assert seen_tokens == ["ghu_fresh"]


def test_status_rejects_a_stale_operation_id(tmp_path):
    cfg = _cfg(tmp_path)
    registry = ShipJobRegistry(runner=lambda *_a, **_kw: {"ok": True})
    started = registry.start(cfg, 171)
    _wait_for(registry, cfg, 171, "succeeded")

    assert registry.status(cfg, 171, operation_id="different-generation") is None
    assert registry.status(
        cfg, 171, operation_id=started["operation_id"]
    )["status"] == "succeeded"
