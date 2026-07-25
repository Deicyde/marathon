"""Durable background jobs for the deck's irreversible ship operation.

Shipping a reviewed issue can spend tens of seconds waiting on GitHub comment,
label, and issue-body APIs.  Running that work in the request handler makes the
browser look frozen and lets a reverse-proxy timeout obscure whether the write
landed.  This registry provides a small durable outbox:

* ``start`` atomically records the accepted review generation before starting
  a worker and returns immediately;
* repeated starts for the same live issue coalesce onto one worker;
* ``status`` is a pure read suitable for browser polling;
* a process restart exposes an interrupted ``running`` job as
  ``awaiting_retry``.  The next explicit, authenticated POST resumes the same
  generation, preserving the actor's GitHub identity without ever writing that
  OAuth token to disk.

The underlying :func:`marathon.deck.subdeck.ship_issue` remains the authority
for cross-process locking, GitHub operation markers, and completion receipts.
The outbox deliberately stores orchestration metadata only.
"""

from __future__ import annotations

import hashlib
import json
import os
import secrets
import threading
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING, Callable, Optional

if TYPE_CHECKING:  # pragma: no cover - type-only imports
    from marathon.review.config import ReviewConfig


_TERMINAL_STATUSES = frozenset({"succeeded", "failed"})
_EXTERNAL_OWNER_LEASE_SECONDS = 15 * 60

try:  # POSIX cross-process record fencing.
    import fcntl as _fcntl
except ImportError:  # pragma: no cover - non-POSIX fallback
    _fcntl = None


class ShipJobConflict(RuntimeError):
    """A different ship attempt for this issue is already in flight."""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _jobs_dir(cfg: "ReviewConfig") -> Path:
    # runner-logs/ is the established ignored runtime area in consumer repos.
    # Keeping the outbox there prevents accepted jobs from appearing as source
    # changes or contaminating conductor worktree hygiene.
    return Path(cfg.runner_log_dir) / "deck-ship-jobs"


def _job_path(cfg: "ReviewConfig", issue_num: int) -> Path:
    return _jobs_dir(cfg) / f"{issue_num}.json"


def _pid_alive(pid) -> bool:
    try:
        value = int(pid)
        if value <= 0:
            return False
        os.kill(value, 0)
        return True
    except (OSError, TypeError, ValueError):
        return False


def _external_owner_live(record: dict) -> bool:
    """Whether another deck process plausibly still owns a running attempt.

    PID reuse must not create a permanent phantom job, so an external owner is
    trusted only for a generous bounded lease. A genuine unusually long job is
    still protected by ship_issue's cross-process lock/idempotency if retried.
    """
    if record.get("owner_pid") == os.getpid():
        return False
    if not _pid_alive(record.get("owner_pid")):
        return False
    try:
        updated = datetime.fromisoformat(str(record["updated_at"]))
        age = (datetime.now(timezone.utc) - updated).total_seconds()
    except (KeyError, TypeError, ValueError):
        return False
    return age <= _EXTERNAL_OWNER_LEASE_SECONDS


@contextmanager
def _record_lock(path: Path):
    """Serialize short read/replace transactions for one durable job record."""
    lock_path = path.with_suffix(path.suffix + ".lock")
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    with lock_path.open("a+b") as handle:
        if _fcntl is not None:
            _fcntl.flock(handle.fileno(), _fcntl.LOCK_EX)
        try:
            yield
        finally:
            if _fcntl is not None:
                _fcntl.flock(handle.fileno(), _fcntl.LOCK_UN)


def _atomic_write(path: Path, payload: dict) -> None:
    """Atomically replace one job record.

    The temp name includes the thread id so a stale/retried worker can never
    share a temporary file with the request thread.  The final per-issue record
    is still a single atomic snapshot.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(
        f".{path.name}.{os.getpid()}.{threading.get_ident()}.tmp"
    )
    try:
        encoded = (
            json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True)
            + "\n"
        )
        with tmp.open("w", encoding="utf-8") as handle:
            handle.write(encoded)
            handle.flush()
            os.fsync(handle.fileno())
        tmp.replace(path)
        # Persist the directory entry as well as the file contents before the
        # request acknowledges the job. Directory fsync is unavailable on a
        # few platforms/filesystems, where atomic replace still provides the
        # process-restart guarantee.
        try:
            directory_fd = os.open(path.parent, os.O_RDONLY)
        except OSError:
            directory_fd = None
        if directory_fd is not None:
            try:
                os.fsync(directory_fd)
            except OSError:
                pass
            finally:
                os.close(directory_fd)
    finally:
        try:
            tmp.unlink()
        except FileNotFoundError:
            pass


def _load(path: Path) -> Optional[dict]:
    try:
        raw = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError):
        return None
    if not isinstance(raw, dict):
        return None
    if not isinstance(raw.get("issue"), int):
        return None
    if not isinstance(raw.get("operation_id"), str):
        return None
    if raw.get("status") not in {"running", *_TERMINAL_STATUSES}:
        return None
    return raw


def _generation_id(
    cfg: "ReviewConfig",
    issue_num: int,
    *,
    skip_api: bool,
    review_token: Optional[str] = None,
) -> str:
    """Identify one local human-review + source generation without GitHub."""
    from marathon.deck.definitions import definition_source_token
    from marathon.deck.subdeck import issue_verdicts

    payload = {
        "version": 1,
        "issue": issue_num,
        "skip_api": bool(skip_api),
        "review_token": review_token,
        "records": issue_verdicts(cfg, issue_num),
        # HEAD plus the current Lean-file inventory/mtime/size signature. This
        # catches an Aristotle checkout or dirty source edit between the click
        # and the worker's irreversible boundary without adding a network call
        # to the fast acknowledgement path.
        "source": definition_source_token(Path(cfg.repo_dir)),
    }
    encoded = json.dumps(
        payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _redact(value, secret: Optional[str]):
    """Remove a request credential from anything that may be persisted."""
    if not secret:
        return value
    if isinstance(value, str):
        return value.replace(secret, "[redacted]")
    if isinstance(value, dict):
        return {
            key: _redact(item, secret)
            for key, item in value.items()
            if "token" not in str(key).lower()
            and "credential" not in str(key).lower()
        }
    if isinstance(value, list):
        return [_redact(item, secret) for item in value]
    if isinstance(value, tuple):
        return [_redact(item, secret) for item in value]
    return value


def _public_snapshot(record: dict, *, interrupted: bool = False) -> dict:
    out = dict(record)
    # An OAuth credential is never part of a record.  Keep this defensive
    # filter so even a malformed old/manual record cannot echo one to a client.
    for key in tuple(out):
        if "token" in key.lower() or "credential" in key.lower():
            out.pop(key, None)
    if interrupted and out.get("status") == "running":
        out["status"] = "awaiting_retry"
        out["error"] = (
            "The deck restarted while this review was being published. "
            "Press “Ship to Aristotle” again to safely resume it."
        )
    out["ok"] = True
    out["status_url"] = (
        f"/api/ship-status?issue={out['issue']}"
        f"&operation_id={out['operation_id']}"
    )
    return out


class ShipJobRegistry:
    """Per-deck durable registry for asynchronous issue ships.

    ``runner`` and ``thread_factory`` are injectable so the acceptance boundary
    and restart behavior can be tested without GitHub or real background work.
    """

    def __init__(
        self,
        runner: Optional[Callable[..., dict]] = None,
        thread_factory: Callable[..., threading.Thread] = threading.Thread,
    ) -> None:
        if runner is None:
            from marathon.deck.subdeck import ship_issue

            runner = ship_issue
        self._runner = runner
        self._thread_factory = thread_factory
        self._lock = threading.Lock()
        # repo + issue is the single-flight key.  Persisted state is authoritative
        # across processes; this map only proves that a worker is live HERE.
        self._live: dict[tuple[str, int], tuple[str, threading.Thread]] = {}

    @staticmethod
    def _key(cfg: "ReviewConfig", issue_num: int) -> tuple[str, int]:
        return (str(Path(cfg.repo_dir).resolve()), issue_num)

    def start(
        self,
        cfg: "ReviewConfig",
        issue_num: int,
        *,
        skip_api: bool = False,
        review_token: Optional[str] = None,
        actor_token: Optional[str] = None,
        on_complete: Optional[Callable[[], None]] = None,
    ) -> dict:
        """Durably accept or coalesce one explicit ship request.

        The actor token is captured only by the worker closure.  It is never
        included in ``record`` and therefore never serialized.
        """
        key = self._key(cfg, issue_num)
        path = _job_path(cfg, issue_num)
        actor_token = (actor_token or "").strip() or None

        # Double-clicks must never wait behind the long issue-operation lock
        # already held by this worker. The worker revalidates the generation at
        # its irreversible boundary, so a mutation that slipped in before it
        # acquired that lock becomes an honest failed job, never a wrong ship.
        with self._lock:
            live = self._live.get(key)
            if live is not None and live[1].is_alive():
                with _record_lock(path):
                    record = _load(path)
                if record is not None:
                    if bool(record.get("skip_api")) != bool(skip_api):
                        raise ShipJobConflict(
                            "This issue is already shipping with a different "
                            "Skip easy API declarations setting. Wait for that "
                            "job to finish, then review the result."
                        )
                    return _public_snapshot(record)
            elif live is not None:
                self._live.pop(key, None)

        # During a rolling service restart, an older deck process may still own
        # the issue lock and background worker. Return its durable job promptly
        # instead of recreating the original long blocking request.
        with _record_lock(path):
            external = _load(path)
        if (
            external is not None
            and external.get("status") == "running"
            and _external_owner_live(external)
        ):
            if bool(external.get("skip_api")) != bool(skip_api):
                raise ShipJobConflict(
                    "This issue is already shipping with a different "
                    "Skip easy API declarations setting."
                )
            return _public_snapshot(external)

        # Capture the human-review generation under the SAME per-issue lock
        # used by declaration/missing/lock mutations and ship_issue itself.
        from marathon.deck.subdeck import _issue_operation_lock

        with _issue_operation_lock(cfg, issue_num):
            operation_id = _generation_id(
                cfg,
                issue_num,
                skip_api=skip_api,
                review_token=review_token,
            )
            with self._lock:
                # A worker may have started while this request waited for the
                # issue lock. Coalesce locally instead of creating a second.
                live = self._live.get(key)
                if live is not None and live[1].is_alive():
                    with _record_lock(path):
                        current = _load(path)
                    if current is not None:
                        if current.get("operation_id") != operation_id:
                            raise ShipJobConflict(
                                "This issue already has a different review "
                                "generation in flight. Wait for it to finish "
                                "before shipping the newer decisions."
                            )
                        return _public_snapshot(current)
                elif live is not None:
                    self._live.pop(key, None)

                with _record_lock(path):
                    previous = _load(path)
                    if (
                        previous is not None
                        and previous.get("operation_id") == operation_id
                        and previous.get("status") == "succeeded"
                    ):
                        return _public_snapshot(previous)

                    accepted_at = (
                        previous.get("accepted_at")
                        if previous is not None
                        and previous.get("operation_id") == operation_id
                        else None
                    ) or _now()
                    record = {
                        "version": 1,
                        "issue": issue_num,
                        "operation_id": operation_id,
                        "attempt_id": secrets.token_hex(16),
                        "owner_pid": os.getpid(),
                        "skip_api": bool(skip_api),
                        "review_token": review_token,
                        "status": "running",
                        "accepted_at": accepted_at,
                        "started_at": _now(),
                        "updated_at": _now(),
                    }
                    # This write is the acceptance boundary. If it fails, no
                    # worker is launched and no false 202 can escape.
                    _atomic_write(path, record)

                def _work() -> None:
                    from marathon.review.github import actor_github_token

                    # Keep this lock through terminal publication and removal
                    # from _live. Thus a new review mutation can never exist
                    # while a caller can still coalesce onto this generation.
                    with _issue_operation_lock(cfg, issue_num):
                        try:
                            # Revalidate at the irreversible boundary. A review
                            # edited after acceptance is never shipped under a
                            # stale operation id.
                            current_operation = _generation_id(
                                cfg,
                                issue_num,
                                skip_api=skip_api,
                                review_token=review_token,
                            )
                            if current_operation != operation_id:
                                result = {
                                    "ok": False,
                                    "reason": (
                                        "The review changed while shipping was "
                                        "starting. Nothing was shipped; review "
                                        "the latest decisions and try again."
                                    ),
                                }
                            else:
                                with actor_github_token(actor_token):
                                    runner_kwargs = {
                                        "skip_api": bool(skip_api),
                                    }
                                    if review_token is not None:
                                        runner_kwargs["expected_review_token"] = (
                                            review_token
                                        )
                                    result = self._runner(
                                        cfg, issue_num, **runner_kwargs
                                    )
                            safe_result = _redact(result, actor_token)
                            if result.get("ok") is False:
                                finished = {
                                    **record,
                                    "status": "failed",
                                    "error": str(
                                        safe_result.get("reason")
                                        or safe_result.get("error")
                                        or "The review could not be shipped."
                                    ),
                                    "result": safe_result,
                                    "finished_at": _now(),
                                    "updated_at": _now(),
                                }
                            else:
                                finished = {
                                    **record,
                                    "status": "succeeded",
                                    "result": safe_result,
                                    "finished_at": _now(),
                                    "updated_at": _now(),
                                }
                        except Exception as exc:  # noqa: BLE001 - worker boundary
                            safe_error = _redact(str(exc), actor_token)
                            finished = {
                                **record,
                                "status": "failed",
                                "error": f"ship failed: {safe_error}",
                                "finished_at": _now(),
                                "updated_at": _now(),
                            }

                        # Invalidate read models BEFORE a terminal result becomes
                        # visible. The first client refresh after "succeeded"
                        # must not reuse a cache built during the ship.
                        if on_complete is not None:
                            try:
                                on_complete()
                            except Exception:
                                # Cache invalidation/notification is best-effort;
                                # the durable job result remains authoritative.
                                pass

                        try:
                            # Attempt fencing prevents an old worker from
                            # overwriting a newer retry accepted by another deck
                            # process during a rolling restart.
                            with _record_lock(path):
                                current_record = _load(path)
                                if (
                                    current_record is not None
                                    and current_record.get("operation_id")
                                    == operation_id
                                    and current_record.get("attempt_id")
                                    == record["attempt_id"]
                                ):
                                    _atomic_write(path, finished)
                        finally:
                            with self._lock:
                                current = self._live.get(key)
                                if (
                                    current is not None
                                    and current[0] == operation_id
                                    and current[1]
                                    is threading.current_thread()
                                ):
                                    self._live.pop(key, None)

                try:
                    thread = self._thread_factory(
                        target=_work,
                        name=f"marathon-ship-{issue_num}",
                        daemon=True,
                    )
                    self._live[key] = (operation_id, thread)
                    thread.start()
                except Exception:
                    self._live.pop(key, None)
                    failed = {
                        **record,
                        "status": "failed",
                        "error": (
                            "The background ship worker could not be started."
                        ),
                        "finished_at": _now(),
                        "updated_at": _now(),
                    }
                    with _record_lock(path):
                        current_record = _load(path)
                        if (
                            current_record is not None
                            and current_record.get("attempt_id")
                            == record["attempt_id"]
                        ):
                            _atomic_write(path, failed)
                    raise
                return _public_snapshot(record)

    def status(
        self,
        cfg: "ReviewConfig",
        issue_num: int,
        operation_id: Optional[str] = None,
    ) -> Optional[dict]:
        """Return a pure-read job snapshot, or ``None`` for no matching job."""
        path = _job_path(cfg, issue_num)
        # Atomic replacement makes lock-free reads safe and keeps this GET
        # genuinely pure: polling an unknown issue creates no directories or
        # lock files.
        record = _load(path)
        if record is None:
            return None
        if operation_id is not None and record.get("operation_id") != operation_id:
            return None

        key = self._key(cfg, issue_num)
        with self._lock:
            live = self._live.get(key)
            local_live = (
                live is not None
                and live[0] == record.get("operation_id")
                and live[1].is_alive()
            )
        external_live = _external_owner_live(record)
        is_live = local_live or external_live
        interrupted = record.get("status") == "running" and not is_live
        if interrupted:
            # Close the narrow race where the worker published its terminal
            # record after our first read but removed itself before the live
            # check. Never report a completed job as awaiting retry.
            latest = _load(path)
            if (
                latest is not None
                and latest.get("operation_id") == record.get("operation_id")
                and latest.get("attempt_id") == record.get("attempt_id")
            ):
                record = latest
                interrupted = record.get("status") == "running"
        return _public_snapshot(record, interrupted=interrupted)
