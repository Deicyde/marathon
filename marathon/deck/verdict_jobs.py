"""Durable background jobs for the deck's issue-level verdict fast path.

``POST /api/verdict`` routes the committed ``cmd_verify``/``cmd_reject``
machinery, whose GitHub writes (PR merge, labels, comments, tracker flip)
can take many seconds.  Running them in the request handler freezes the
initiating control and lets a proxy timeout obscure whether the write
landed.  This registry gives the fast path the ship pipeline's shape:

* ``start`` durably records the accepted request and returns immediately;
* repeated starts for the same live issue coalesce onto one worker;
* ``status`` is a pure read suitable for browser polling;
* a failed verdict pushes an operator notification.

It deliberately reuses :mod:`marathon.deck.ship_jobs`' fencing primitives
(atomic record replacement, per-record flock, external-owner lease) rather
than reimplementing them.  The registry stores orchestration metadata only;
``apply_verdict`` remains the single side-effecting authority and its
GitHub writes stay idempotent projections, so a retry after any crash
re-converges.
"""

from __future__ import annotations

import os
import secrets
import threading
from pathlib import Path
from typing import TYPE_CHECKING, Callable, Optional

from marathon.deck.ship_jobs import (
    _atomic_write,
    _external_owner_live,
    _now,
    _record_lock,
)

if TYPE_CHECKING:  # pragma: no cover - type-only imports
    from marathon.review.config import ReviewConfig

_TERMINAL_STATUSES = frozenset({"succeeded", "failed"})


def _load(path: Path) -> Optional[dict]:
    """Read one verdict-job record; None for missing/foreign/corrupt files.

    (Ship's loader validates the SHIP record schema — operation ids, review
    tokens — so verdict jobs carry their own narrower validation.)
    """
    import json

    try:
        raw = json.loads(path.read_text())
    except (OSError, ValueError):
        return None
    if not isinstance(raw, dict):
        return None
    if not isinstance(raw.get("issue"), int):
        return None
    if raw.get("status") not in {"running", *_TERMINAL_STATUSES}:
        return None
    return raw


def _jobs_dir(cfg: "ReviewConfig") -> Path:
    return Path(cfg.runner_log_dir) / "deck-verdict-jobs"


def _job_path(cfg: "ReviewConfig", issue_num: int) -> Path:
    return _jobs_dir(cfg) / f"{issue_num}.json"


class VerdictJobRegistry:
    """Per-deck registry for asynchronous issue-level verdicts."""

    def __init__(
        self,
        runner: Optional[Callable[..., dict]] = None,
        thread_factory: Callable[..., threading.Thread] = threading.Thread,
    ) -> None:
        if runner is None:
            def runner(cfg, issue_num, verdict, note):  # type: ignore[misc]
                from marathon.deck.verdicts import apply_issue_verdict

                return apply_issue_verdict(cfg, issue_num, verdict, note).to_json()
        self._runner = runner
        self._thread_factory = thread_factory
        self._lock = threading.Lock()
        self._live: dict[tuple[str, int], str] = {}

    @staticmethod
    def _key(cfg: "ReviewConfig", issue_num: int) -> tuple[str, int]:
        return (str(Path(cfg.repo_dir).resolve()), issue_num)

    def status(self, cfg: "ReviewConfig", issue_num: int) -> Optional[dict]:
        """Pure read of the durable record (poll endpoint)."""
        record = _load(_job_path(cfg, issue_num))
        if record is None:
            return None
        if (
            record.get("status") == "running"
            and record.get("owner_pid") != os.getpid()
            and not _external_owner_live(record)
        ):
            # The owning deck died mid-write. The verdict machinery is an
            # idempotent projection, so an explicit retry re-converges.
            return {**record, "status": "awaiting_retry"}
        return record

    def start(
        self,
        cfg: "ReviewConfig",
        issue_num: int,
        verdict: str,
        note: Optional[str],
        *,
        actor_token: Optional[str] = None,
        on_complete: Optional[Callable[[], None]] = None,
    ) -> dict:
        """Accept one issue-level verdict and run it in the background.

        Returns the durable record.  A live record for the same issue
        coalesces (the caller polls the existing attempt); a terminal or
        orphaned record is replaced by this fresh attempt.
        """
        path = _job_path(cfg, issue_num)
        key = self._key(cfg, issue_num)
        with _record_lock(path):
            current = _load(path)
            if (
                current is not None
                and current.get("status") == "running"
                and (
                    current.get("owner_pid") == os.getpid()
                    and self._live.get(key)
                    or _external_owner_live(current)
                )
            ):
                return current
            record = {
                "issue": issue_num,
                "verdict": verdict,
                "status": "running",
                "attempt_id": secrets.token_hex(16),
                "owner_pid": os.getpid(),
                "accepted_at": _now(),
                "updated_at": _now(),
            }
            # The acceptance boundary: if this write fails, no worker starts
            # and no false 202 escapes.
            _atomic_write(path, record)
        with self._lock:
            self._live[key] = record["attempt_id"]

        def _work() -> None:
            from marathon.review.github import actor_github_token

            try:
                with actor_github_token(actor_token):
                    result = self._runner(cfg, issue_num, verdict, note)
                ok = bool(result.get("ok", True))
                finished = {
                    **record,
                    "status": "succeeded" if ok else "failed",
                    "result": result,
                    "error": None if ok else str(
                        result.get("message") or "verdict was refused"
                    ),
                    "finished_at": _now(),
                    "updated_at": _now(),
                }
            except Exception as exc:  # noqa: BLE001 - worker boundary
                finished = {
                    **record,
                    "status": "failed",
                    "error": f"verdict failed: {exc}",
                    "finished_at": _now(),
                    "updated_at": _now(),
                }
            if finished.get("status") == "failed":
                from marathon.notify import notify

                notify(
                    "Marathon deck",
                    f"Verdict on #{issue_num} failed: "
                    f"{str(finished.get('error') or '')[:200]}",
                )
            if on_complete is not None:
                try:
                    on_complete()
                except Exception:  # noqa: BLE001 - invalidation is best-effort
                    pass
            try:
                with _record_lock(path):
                    current = _load(path)
                    if (
                        current is not None
                        and current.get("attempt_id") == record["attempt_id"]
                    ):
                        _atomic_write(path, finished)
            finally:
                with self._lock:
                    if self._live.get(key) == record["attempt_id"]:
                        self._live.pop(key, None)

        worker = self._thread_factory(
            target=_work, name=f"deck-verdict-{issue_num}", daemon=True
        )
        worker.start()
        return record
