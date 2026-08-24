"""Repo-level multi-flight Conductor (marathon v2 Phase 3).

Evolution of ``marathon.review.daemon`` (which stays untouched this
round): where the per-chapter daemon dispatches ONE ``marathon refine``
at a time inside the consumer repo itself, the Conductor polls the
rejection queue ACROSS ALL registered chapters (oldest verdict first,
project-wide) and dispatches up to ``--concurrency`` refine
subprocesses simultaneously, each in its own ``git worktree`` of the
consumer repo. GeometricAnalysis serialized 66.7 h of Aristotle wall
time through the single-flight daemon; this is the lever that ends it.

Binding rulings (docs/marathon-v2-plan.md §2 ruling 2, §3 Phase 3 row):

* **Deterministic Python only.** Scheduling, retry, collision, and
  cleanup decisions are plain code — Claude is never the scheduler.
* **Default concurrency is 1** (parity with today's daemon). Raised
  only explicitly via ``--concurrency`` or the
  ``MARATHON_ARISTOTLE_MAX_CONCURRENT`` env var: Harmonic's concurrent
  session limits are undocumented until the operator runs
  ``scripts/aristotle_concurrency_probe.py``.
* **Never cancel Aristotle jobs automatically.** Orphan reconciliation
  is report-only; SIGTERM/SIGINT stop dispatching but let running jobs
  finish (their pids/issues are printed while the conductor waits).
* **One retry/stall state machine.** Failure handling reuses the
  Phase-0 decision table verbatim via
  ``marathon.review.daemon._handle_refine_exit`` (record_failed_attempt
  → backoff requeue; record_stall + one GitHub notification after
  ``--max-attempts``; interrupted jobs record NOTHING). The only
  difference: the conductor turns the returned backoff into a
  per-issue ``not-before`` timestamp instead of sleeping the whole
  loop, so one chapter's backoff never starves the others.

**Worktree isolation.** Each job runs in its own ``git worktree add``
under a parent OUTSIDE the repo (default
``~/Desktop/marathon-runs/conductor/<repo-name>/wt-i<issue>-<ts>``;
per-job refine workdirs land beside them as ``wd-i<issue>-<ts>``).
Worktrees inside the repo would leak into Aristotle bundles via ``git
ls-files --others``, so an inside-the-repo parent is refused at
startup. Each worktree is created directly on the job's per-issue
branch (``marathon/refine-c<N>-i<issue>`` — the exact name
``post_pipeline`` uses, imported, not re-derived), which makes git's
same-branch-in-two-worktrees refusal a free double-dispatch guard: a
second conductor or a leftover per-chapter daemon racing on the same
issue gets a *deferred* dispatch, never a crash. Successful jobs
remove their worktree; failed/interrupted jobs keep theirs for
debugging (path printed) and the conductor reuses that kept worktree
when it retries the same issue (a fresh ``worktree add`` would trip
its own branch guard). ``--prune`` cleans leftovers from prior runs.

**Collision check (crude, Phase-3 grade).** Two jobs whose target
chapter folders are equal never run together; the younger rejection is
deferred. Declaration-level overlap detection arrives with the Phase-5
audit engine's Lean dep graph — until then the chapter folder is the
collision unit.

**Runtime snapshot.** ``.marathon/conductor/jobs.json`` is rewritten
every tick so ``marathon conductor status`` can print a table without
touching the running conductor. ``percent_complete`` is deliberately
absent: the refine checkpoint (``marathon-refine-state.json``) records
project id + task status but no percent, and surfacing percent would
cost an Aristotle API call per job per tick — the checkpoint's
``aristotle_status`` is recorded instead (cheap: one small file read).

**Single-conductor lock.** PID file at
``.marathon/review/runner-locks/conductor.lock`` with the same
reclaim-stale-locks semantics as the per-chapter daemon locks (the
helper lives here because the daemon's lock functions are
chapter-keyed and the daemon must stay untouched this phase).
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import math
import os
import re
import secrets
import shlex
import shutil
import signal
import stat
import subprocess
import sys
import threading
import time
from dataclasses import dataclass, field, fields
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional
from urllib.parse import urlencode

import marathon.review.daemon as daemon
from marathon.post_pipeline import (
    PUBLICATION_JOURNAL_SCHEMA_VERSION,
    _branch_name_for_issue,
    find_lake_executable,
)
from marathon.recovery import (
    RecoveryError,
    UPDATE_RECOVERY_MARKER_FILENAME,
    UPDATE_RECOVERY_PHASE_FALLBACK_PREPARED,
    UPDATE_RECOVERY_PHASE_PUBLISHED,
    UPDATE_RECOVERY_PHASES,
    UPDATE_RECOVERY_SCHEMA_VERSION,
    _update_recovery_marker_lock,
)
from marathon.refine import (
    EXIT_AUTO_PR_BRANCH_ABORT,
    EXIT_CONFIG_ERROR,
    EXIT_PUBLICATION_PENDING,
    EXIT_REATTACH_TRANSIENT,
    REFINE_STATE_FILENAME,
)
from marathon.review.config import ReviewConfig, load_config
from marathon.notify import notify
from marathon.review.state import (
    RECOVERY_CONTINUE_PR,
    RECOVERY_RETRY_BUILD,
    RECOVERY_START_OVER,
    RECOVERY_UPDATE_CONTINUE,
    advance_recovery_head_if_current,
    clear_pending_pr_check,
    load_state,
    pending_rejections_needing_iteration,
    record_iteration,
    record_stall,
)
from marathon.state import (
    RefineState,
    load_refine_state,
    now_iso,
    save_refine_state,
)

# Snapshot + lock locations (repo-relative).
SNAPSHOT_SCHEMA_VERSION = 1
JOBS_SNAPSHOT_RELPATH = Path(".marathon/conductor/jobs.json")
CONDUCTOR_HEARTBEAT_RELPATH = Path(".marathon/conductor/heartbeat.json")
CONDUCTOR_LOCK_FILENAME = "conductor.lock"
START_OVER_RECOVERY_MARKER_FILENAME = "marathon-start-over-recovery.json"
START_OVER_RECOVERY_SCHEMA_VERSION = 1

# Concurrency: default 1 = parity with the single-flight daemon
# (BINDING — see module docstring). The env var is a *fallback default*
# for operators who have probed their real session limit; an explicit
# --concurrency always wins.
DEFAULT_CONCURRENCY = 1
CONCURRENCY_ENV_VAR = "MARATHON_ARISTOTLE_MAX_CONCURRENT"
#: Discover the rejection queue from the GitHub review:rejected label instead of
#: local state.json (so a collaborator's conductor on a fresh clone sees rejects
#: fired on a shared deck). Set by --queue-from-github or this env var.
QUEUE_FROM_GITHUB_ENV_VAR = "MARATHON_QUEUE_FROM_GITHUB"

# Where job worktrees branch from. refine's --auto-pr resets the branch
# to origin/<base> (default main) at iteration start anyway; this just
# has to be a valid start point for `git worktree add -B`.
WORKTREE_START_POINT = "origin/main"
# The prover lands Aristotle PROOFS on a SEPARATE branch — main stays in
# skeleton mode. Proof PRs target this branch and only ever merge into it;
# the [build:OK] automerge's base-equality guard keeps a proof off main.
PROVER_BASE_BRANCH = "marathon/proving"
# Repair jobs are chapter-scoped (no review issue). Key them by
# ``REPAIR_KEY_BASE + chapter`` in the shared not_before/kept_worktrees dicts so
# a repair's bookkeeping can never collide with a real issue number.
REPAIR_KEY_BASE = 1_000_000
# Fill worktrees must not collide with a later rejection refine for the same
# GitHub issue in the shared kept-worktree map (proof branch/session ≠ refine).
FILL_KEY_BASE = 2_000_000


def _kept_worktree_key(issue_num: int, *, fill: bool) -> int:
    return FILL_KEY_BASE + issue_num if fill else issue_num


def _tex_args(cfg, chapter: int) -> list[str]:
    """``["--tex", <path>]`` for the chapter's informal source, or ``[]``.

    Every dispatched refine/fill/repair carries the chapter's ``.tex`` when
    ``tex_dir`` is configured, so Aristotle tracks the informal textbook —
    the original marathon design philosophy. The tex rides ONLY in the
    Aristotle bundle (Claude never sees it); a missing file just omits the
    flag. Best-effort: config without ``tex_path`` (older ReviewConfig) or
    any error yields ``[]``, never a blocked dispatch."""
    try:
        tex = cfg.tex_path(chapter)
    except Exception:  # noqa: BLE001
        return []
    return ["--tex", str(tex)] if tex else []
# The branch the PRIMARY checkout must be on for conductor-side
# metadata regeneration — the same base the job worktrees branch from.
# Regenerating anywhere else would derive yaml fields from whatever the
# operator happens to have checked out.
METADATA_BASE_BRANCH = WORKTREE_START_POINT.rsplit("/", 1)[-1]

# Loop cadence: short ticks while jobs are running (reap promptly, keep
# the snapshot fresh); the daemon's drained-queue interval otherwise.
TICK_SECONDS = 10
# The jobs snapshot is written only at the end of a tick. A separate tiny
# heartbeat stays fresh even while a slow GitHub or audit operation is running,
# so the deck never mistakes yesterday's snapshot for a ready conductor.
CONDUCTOR_HEARTBEAT_INTERVAL_SECONDS = 5
CONDUCTOR_HEARTBEAT_STALE_SECONDS = 30
# A separate main-loop progress age is diagnostic only: audits and builds can
# legitimately make one tick long, so it must not kill the process or cancel a
# worker. The UI can nevertheless stop claiming that queued work is advancing.
CONDUCTOR_PROGRESS_STALE_SECONDS = 15 * 60
# A dispatch deferred by the worktree double-dispatch guard is retried
# after this long (the conflicting checkout is usually another live
# job or a leftover the operator must --prune; hammering won't help).
WORKTREE_DEFER_SECONDS = 60
# A FILL (prove) job that exits non-zero backs off this long before the
# next dispatch. Retries are already hard-bounded by the dispatch-time
# `max_fill_attempts` cap; this only stops a tight re-dispatch loop.
FILL_RETRY_BACKOFF_SECONDS = 300
# Publication-only retries are cheap network operations and must not burn the
# Aristotle failure budget. Keep a modest delay to avoid hammering GitHub.
PUBLISH_RETRY_BACKOFF_SECONDS = 60
# Safety valve for --once mode: with per-issue backoffs the queue can
# stay non-empty for a long time without being stuck; cap the tick
# count rather than the dispatch count so retries aren't starved.
MAX_TICKS_ONCE = 1000

# ProjectStatus member names that mean "in flight", probed by name so
# the orphan report survives SDK vocabulary changes (current
# aristotlelib exposes RUNNING; older docs say QUEUED/IN_PROGRESS).
_IN_FLIGHT_PROJECT_STATUS_NAMES = ("QUEUED", "IN_PROGRESS", "RUNNING")

# Set by SIGTERM/SIGINT to stop dispatching. Module-level so the signal
# handlers can flip it without a closure (same pattern as the daemon's).
_STOP_REQUESTED = False


def _handle_stop_signal(signum, frame) -> None:
    global _STOP_REQUESTED
    _STOP_REQUESTED = True
    print(
        f"\n--- stop signal received (signum={signum}); no new jobs will be "
        "dispatched. Running Aristotle jobs are NEVER canceled — the "
        "conductor will wait for them to finish. ---",
        flush=True,
    )


def _now() -> float:
    """Wall-clock seconds. Routed through a module function so tests can
    drive the backoff/not-before bookkeeping with a fake clock."""
    return time.time()


def _sleep(seconds: float, *, interruptible: bool = True) -> None:
    """Sleep in 1s chunks. ``interruptible`` (default) returns early on a
    stop signal; the stop-and-wait path passes False so waiting on
    running jobs isn't a busy loop once the flag is already set."""
    deadline = int(seconds)
    for _ in range(max(deadline, 1)):
        if interruptible and _STOP_REQUESTED:
            return
        time.sleep(1)


# --- Job model -----------------------------------------------------------------

# Fields that exist only while the conductor process owns the job; they
# never serialize into jobs.json.
_RUNTIME_ONLY_FIELDS = ("proc", "log_handle")


@dataclass
class ConductorJob:
    """One dispatched ``marathon refine`` subprocess.

    Path fields are stored as ``str`` (not ``Path``) so ``to_json`` /
    ``from_json`` round-trip the jobs.json snapshot losslessly.
    ``target`` is the repo-relative chapter folder — it doubles as the
    Phase-3 collision key (see module docstring).
    """

    issue_num: int
    chapter: int
    target: str                              # repo-relative target folder
    worktree: str                            # absolute path of the job's worktree
    workdir: str                             # absolute path of the refine workdir
    branch: str                              # marathon/refine-c<N>-i<issue>
    pid: Optional[int] = None
    pgid: Optional[int] = None               # detached process group/session id
    started_ts: Optional[str] = None         # ISO 8601
    finished_ts: Optional[str] = None        # ISO 8601
    status: str = "running"                  # running | succeeded | failed |
                                             # stalled | interrupted |
                                             # publication_pending | superseded
    exit_code: Optional[int] = None
    error: Optional[str] = None              # log tail of a FAILED job (for the
                                             # snapshot/deck + the stall comment)
    project_id: Optional[str] = None         # from the workdir refine checkpoint
    aristotle_status: Optional[str] = None   # TaskStatus.value from the checkpoint
    fill: bool = False                       # a PROVE job (marathon fill) vs a refine
    repair: bool = False                     # a REPAIR job (marathon repair, → main)
    # Human-verdict generation this job was dispatched against. Completion
    # callbacks CAS this exact timestamp + rejected status; an old process can
    # never consume/attempt/stall a later re-reject or a verified issue.
    expected_verdict_ts: Optional[str] = None
    # Operational recovery generation. Like ``expected_verdict_ts``, this is
    # persisted into jobs.json/pid markers and must match at completion; an old
    # worker cannot consume a newer click in the recovery dialog.
    expected_recovery_id: Optional[str] = None
    recovery_mode: Optional[str] = None
    recovery_pr: Optional[int] = None
    recovery_head_sha: Optional[str] = None
    recovery_base: Optional[str] = None
    rejection_note: Optional[str] = None
    launch_nonce: Optional[str] = None         # binds the two-phase start ACK
    proc: Optional[object] = field(default=None, repr=False, compare=False)
    log_handle: Optional[object] = field(default=None, repr=False, compare=False)

    def to_json(self) -> dict:
        return {
            f.name: getattr(self, f.name)
            for f in fields(self)
            if f.name not in _RUNTIME_ONLY_FIELDS
        }

    @classmethod
    def from_json(cls, data: dict) -> "ConductorJob":
        # Drop unknown keys so old snapshots survive future field additions
        # (same tolerance as marathon.state.load_refine_state).
        known = {f.name for f in fields(cls)} - set(_RUNTIME_ONLY_FIELDS)
        return cls(**{k: v for k, v in data.items() if k in known})


# --- Single-conductor lock -------------------------------------------------------
#
# Same acquire/release semantics as daemon.acquire_lock/release_lock
# (stale locks from dead PIDs reclaimed; only the owning PID releases),
# but on a repo-wide ``conductor.lock``. Re-implemented as a small
# helper here rather than refactoring the daemon's chapter-keyed
# functions, because Phase 3 leaves the per-chapter daemon untouched.


def conductor_lock_path(cfg: ReviewConfig) -> Path:
    return cfg.runner_lock_dir / CONDUCTOR_LOCK_FILENAME


_CONDUCTOR_GUARDS: dict[Path, object] = {}
_CONDUCTOR_GUARDS_LOCK = threading.RLock()


def _conductor_guard_path(lock: Path) -> Path:
    """Stable inode used for the lifetime lock.

    The adjacent PID file remains the human/dashboard status surface and can be
    removed on clean shutdown.  This guard must never be unlinked: unlinking a
    locked inode allows a second process to create and lock a different inode at
    the same path.
    """
    return lock.with_name(lock.name + ".guard")


def _try_lock_guard(handle) -> bool:
    try:
        import fcntl

        fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        return True
    except BlockingIOError:
        return False
    except ImportError:  # pragma: no cover - Windows fallback
        import msvcrt

        try:
            handle.seek(0)
            msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            return True
        except OSError:
            return False


def _unlock_guard(handle) -> None:
    try:
        import fcntl

        fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
    except ImportError:  # pragma: no cover - Windows fallback
        import msvcrt

        try:
            handle.seek(0)
            msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
        except OSError:
            pass


def acquire_conductor_lock(cfg: ReviewConfig) -> bool:
    """Atomically acquire the repo's lifetime conductor lock.

    The previous read-PID/delete/write sequence allowed two simultaneous web
    rejects to both observe no lock and both launch billed conductors.  A
    non-blocking advisory lock on a stable guard inode makes the decision atomic
    across processes; the PID file is written only after that lock is held.
    """
    cfg.runner_lock_dir.mkdir(parents=True, exist_ok=True)
    lock = conductor_lock_path(cfg).resolve()
    guard_path = _conductor_guard_path(lock)
    with _CONDUCTOR_GUARDS_LOCK:
        if lock in _CONDUCTOR_GUARDS:
            return False
        guard = open(guard_path, "a+b")
        if not _try_lock_guard(guard):
            guard.close()
            return False

        # Compatibility with a conductor from an older Marathon process that
        # owns only the PID file and predates the guard inode.
        try:
            existing_pid = int(lock.read_text().strip()) if lock.is_file() else None
            if existing_pid is not None and daemon.process_alive(existing_pid):
                _unlock_guard(guard)
                guard.close()
                return False
        except (ValueError, OSError):
            pass
        lock.unlink(missing_ok=True)
        try:
            lock.write_text(str(os.getpid()))
        except OSError:
            _unlock_guard(guard)
            guard.close()
            raise
        _CONDUCTOR_GUARDS[lock] = guard
        return True


def release_conductor_lock(cfg: ReviewConfig) -> None:
    lock = conductor_lock_path(cfg).resolve()
    with _CONDUCTOR_GUARDS_LOCK:
        guard = _CONDUCTOR_GUARDS.pop(lock, None)
        if guard is None:
            return
        try:
            if lock.is_file():
                stored_pid = int(lock.read_text().strip())
                if stored_pid == os.getpid():
                    lock.unlink()
        except (ValueError, OSError):
            pass
        finally:
            _unlock_guard(guard)
            guard.close()


# --- Concurrency + run-parent resolution -----------------------------------------


def resolve_concurrency(cli_value: Optional[int]) -> int:
    """Explicit ``--concurrency`` > ``MARATHON_ARISTOTLE_MAX_CONCURRENT``
    env > 1. Clamped to >= 1; a malformed env value warns and falls back
    to the default rather than crashing a daemon launch."""
    if cli_value is not None:
        return max(1, cli_value)
    raw = os.environ.get(CONCURRENCY_ENV_VAR)
    if raw:
        try:
            return max(1, int(raw))
        except ValueError:
            print(
                f"  warning: {CONCURRENCY_ENV_VAR}={raw!r} is not an integer; "
                f"using default concurrency {DEFAULT_CONCURRENCY}"
            )
    return DEFAULT_CONCURRENCY


def default_runs_parent(repo_dir: Path) -> Path:
    """Default parent for job worktrees + workdirs. Deliberately OUTSIDE
    the repo (see module docstring) and namespaced by repo name so two
    consumer repos never share leftovers."""
    return Path.home() / "Desktop" / "marathon-runs" / "conductor" / repo_dir.name


def _paired_retry_workdir(
    runs_parent: Path, issue_num: int, worktree: Path,
) -> Optional[Path]:
    """The workdir created alongside ``worktree`` for this issue, if present.

    A conductor retry historically reused the failed workTREE but allocated a
    brand-new ``wd-i...`` directory.  That discarded ``marathon-refine-state``
    (the Aristotle project/task ids) and ``marathon-refine-log.md``, so the next
    machine retry opened a new Aristotle session instead of continuing the
    resumable one.  Worktree/workdir names share the same timestamp by design;
    recover the exact pair without another state file.
    """
    prefix = f"wt-i{issue_num}-"
    if not worktree.name.startswith(prefix):
        return None
    try:
        if worktree.parent.resolve() != runs_parent.resolve():
            return None
    except OSError:
        return None
    suffix = worktree.name[len(prefix):]
    candidate = runs_parent / f"wd-i{issue_num}-{suffix}"
    return candidate if candidate.is_dir() else None


def _publication_recovery_resume_checkout(
    repo_dir: Path,
    runs_parent: Path,
    issue_num: int,
    kept_worktrees: dict[int, str],
    *,
    expected_operation_id: str,
    expected_repo: str,
    expected_branch: Optional[str],
    expected_base: str,
    expected_remote_oid: str,
    target_relpath: str,
) -> Optional[tuple[Path, Path, str]]:
    """Find an exact-operation write-ahead publication checkpoint.

    The remote ref may already be at the new local commit, so recovery must
    recognise this journal *before* enforcing the original-head fetch/archive
    guards.  Every publication identity field plus the actual kept checkout is
    checked; an issue-number-only leftover is never sufficient for adoption.
    """
    raw_candidates = [kept_worktrees.get(issue_num)]
    snapshot_candidates: list[dict] = []
    if expected_branch:
        held = _registered_worktree_for_branch(repo_dir, expected_branch)
        if held is not None:
            raw_candidates.append(str(held))
    for raw in reversed(list((load_jobs_snapshot(repo_dir) or {}).get("jobs", []))):
        try:
            snapshot_matches = (
                int(raw.get("issue_num")) == issue_num
                and raw.get("expected_recovery_id") == expected_operation_id
            )
        except (AttributeError, TypeError, ValueError):
            snapshot_matches = False
        if snapshot_matches:
            snapshot_candidates.append(raw)
            if raw.get("worktree"):
                raw_candidates.append(str(raw["worktree"]))

    def checkpoint_identity(
        worktree: Path, workdir: Path,
    ) -> Optional[tuple[str, str]]:
        """Return the exact journal branch/commit, or fail closed."""
        try:
            checkpoint = load_refine_state(workdir / REFINE_STATE_FILENAME)
            pending = checkpoint.pending_publication if checkpoint else None
            branch = pending.get("branch") if isinstance(pending, dict) else None
            commit_sha = (
                pending.get("commit_sha") if isinstance(pending, dict) else None
            )
            identity_ok = (
                checkpoint is not None
                and isinstance(pending, dict)
                and pending.get("schema_version")
                == PUBLICATION_JOURNAL_SCHEMA_VERSION
                and pending.get("operation_id") == expected_operation_id
                and pending.get("kind") == "pr"
                and pending.get("repo") == expected_repo
                and pending.get("base") == expected_base
                and pending.get("expected_remote_oid") == expected_remote_oid
                and isinstance(branch, str)
                and bool(branch)
                and (expected_branch is None or branch == expected_branch)
                and isinstance(commit_sha, str)
                and re.fullmatch(r"[0-9a-fA-F]{40}", commit_sha) is not None
                and Path(checkpoint.target_folder).resolve()
                == (worktree / target_relpath).resolve()
            )
        except (OSError, TypeError, ValueError, json.JSONDecodeError):
            identity_ok = False
            branch = None
            commit_sha = None
        if identity_ok and isinstance(branch, str) and isinstance(commit_sha, str):
            return branch, commit_sha
        return None

    seen: set[Path] = set()
    for raw in raw_candidates:
        if not raw:
            continue
        worktree = Path(raw)
        try:
            resolved = worktree.resolve()
            if resolved in seen or not resolved.is_relative_to(
                runs_parent.resolve()
            ):
                continue
            seen.add(resolved)
        except OSError:
            continue
        if not worktree.is_dir() or _worktree_live_pid(str(worktree)) is not None:
            continue
        workdir = _paired_retry_workdir(runs_parent, issue_num, worktree)
        if workdir is None:
            continue
        identity = checkpoint_identity(worktree, workdir)
        if identity is None:
            continue
        branch, commit_sha = identity
        current_branch = _git(worktree, "branch", "--show-current")
        current_head = _git(worktree, "rev-parse", "HEAD")
        if (
            current_branch.returncode == 0
            and (current_branch.stdout or "").strip() == branch
            and current_head.returncode == 0
            and (current_head.stdout or "").strip() == commit_sha
        ):
            return worktree, workdir, branch

    # The worker may have pushed and then lost its checkout (process cleanup,
    # machine restart, or an operator removing only the worktree).  The old
    # jobs snapshot still identifies the paired workdir WAL.  Reconstruct only
    # when the remote is *exactly* the WAL's full commit; branch-name shape or
    # issue number alone is never ownership proof.
    from marathon.post_pipeline import observe_remote_branch

    for raw in snapshot_candidates:
        if not raw.get("worktree") or not raw.get("workdir"):
            continue
        worktree = Path(str(raw["worktree"]))
        workdir = Path(str(raw["workdir"]))
        try:
            if worktree.exists() or not worktree.resolve().is_relative_to(
                runs_parent.resolve()
            ) or not workdir.resolve().is_relative_to(runs_parent.resolve()):
                continue
            paired = _paired_retry_workdir(runs_parent, issue_num, worktree)
            if paired is None or paired.resolve() != workdir.resolve():
                continue
        except OSError:
            continue
        identity = checkpoint_identity(worktree, workdir)
        if identity is None:
            continue
        branch, commit_sha = identity
        if raw.get("branch") != branch:
            continue
        held = _registered_worktree_for_branch(repo_dir, branch)
        if held is not None:
            continue
        local_head = _local_branch_head(repo_dir, branch)
        if (
            local_head is not None
            and local_head != commit_sha
            and not _preserve_local_branch_ref(
                repo_dir,
                issue_num,
                branch,
                commit_sha,
                expected_operation_id,
            )
        ):
            continue
        known, remote_head, _message = observe_remote_branch(repo_dir, branch)
        if not known or remote_head != commit_sha:
            continue
        fetched = _git(repo_dir, "fetch", "origin", branch)
        remote = _git(repo_dir, "rev-parse", f"origin/{branch}")
        if (
            fetched.returncode != 0
            or remote.returncode != 0
            or (remote.stdout or "").strip() != commit_sha
        ):
            continue
        add = _git(
            repo_dir,
            "worktree", "add", "-B", branch, str(worktree), commit_sha,
        )
        if add.returncode != 0:
            continue
        current_branch = _git(worktree, "branch", "--show-current")
        current_head = _git(worktree, "rev-parse", "HEAD")
        if (
            current_branch.returncode == 0
            and (current_branch.stdout or "").strip() == branch
            and current_head.returncode == 0
            and (current_head.stdout or "").strip() == commit_sha
        ):
            print(
                f"--- reconstructed publication checkout for #{issue_num}: "
                f"{worktree}@{commit_sha[:12]} ---",
                flush=True,
            )
            return worktree, workdir, branch
    return None


def _update_recovery_resume_checkout(
    repo_dir: Path,
    runs_parent: Path,
    issue_num: int,
    branch: str,
    kept_worktrees: dict[int, str],
    *,
    expected_operation_id: str,
    expected_repo: str,
    expected_pr: int,
    expected_head: str,
    expected_base: str,
    target_relpath: str,
) -> Optional[tuple[Path, Path, str]]:
    """Find a journal-bound update worktree that can resume after a crash."""
    raw_candidates = [kept_worktrees.get(issue_num)]
    held = _registered_worktree_for_branch(repo_dir, branch)
    if held is not None:
        raw_candidates.append(str(held))
    seen: set[Path] = set()
    for raw in raw_candidates:
        if not raw:
            continue
        worktree = Path(raw)
        try:
            resolved = worktree.resolve()
            if resolved in seen or not resolved.is_relative_to(
                runs_parent.resolve()
            ):
                continue
            seen.add(resolved)
        except OSError:
            continue
        if not worktree.is_dir() or _worktree_live_pid(str(worktree)) is not None:
            continue
        workdir = _paired_retry_workdir(runs_parent, issue_num, worktree)
        if workdir is None:
            continue
        marker_path = workdir / UPDATE_RECOVERY_MARKER_FILENAME
        try:
            marker = json.loads(marker_path.read_text())
            phase = marker.get("phase")
            identity_ok = (
                isinstance(marker, dict)
                and marker.get("schema_version") == UPDATE_RECOVERY_SCHEMA_VERSION
                and phase in UPDATE_RECOVERY_PHASES
                and marker.get("operation_id") == expected_operation_id
                and marker.get("github_repo") == expected_repo
                and int(marker.get("pr")) == expected_pr
                and marker.get("branch") == branch
                and marker.get("original_expected_head") == expected_head
                and marker.get("base_branch") == expected_base
                and marker.get("target_path") == str(
                    (worktree / target_relpath).resolve()
                )
                and isinstance(marker.get("base_sha"), str)
                and isinstance(marker.get("current_local_head"), str)
            )
        except (OSError, TypeError, ValueError, json.JSONDecodeError):
            identity_ok = False
            phase = None
        if identity_ok and isinstance(phase, str):
            return worktree, workdir, phase
    return None


_TERMINAL_UPDATE_FALLBACK_STATUSES = frozenset({
    "ASK_FAILED",
    "CANCELED",
    "COMPLETE",
    "COMPLETE_WITH_ERRORS",
    "DOWNLOAD_FAILED",
    "FAILED",
    "OUT_OF_BUDGET",
    "OUTPUT_FOLDER_MISSING",
    "POLL_FAILED",
    "POST_PIPELINE_BUILD_FAILED",
    "POST_PIPELINE_PR_BLOCKED",
    "RETRIES_EXHAUSTED",
    "SUBMIT_FAILED",
})


def _adoptable_terminal_update_marker(
    marker_path: Path,
    checkpoint: Optional[RefineState],
    *,
    expected_operation_id: str,
    expected_repo: str,
    expected_pr: int,
    expected_branch: str,
    expected_head: str,
    expected_base: str,
    expected_target: Path,
    current_head: str,
) -> Optional[dict]:
    """Recognise one superseded, terminal fallback without relabelling it.

    ``fallback_prepared`` is the hand-off point from the update wrapper to a
    plain refine worker.  Once that worker has exited terminally, its operation
    journal no longer represents live work, but the named worktree may contain
    valuable uncommitted output.  A later explicit recovery may inherit that
    tree only when every durable identity still names the same PR revision and
    the refine checkpoint proves there is no publication left to finish.
    """
    try:
        marker = json.loads(marker_path.read_text())
        marker_target = Path(marker.get("target_path")).resolve()
    except (OSError, RuntimeError, TypeError, ValueError, json.JSONDecodeError):
        return None
    if not isinstance(marker, dict) or checkpoint is None:
        return None

    def _full_oid(value: object) -> bool:
        return (
            isinstance(value, str)
            and len(value) in {40, 64}
            and all(char in "0123456789abcdefABCDEF" for char in value)
        )

    old_operation = marker.get("operation_id")
    original_head = marker.get("original_expected_head")
    raw_selected_head = marker.get("expected_remote_head")
    # Early schema-v2 writers persisted JSON null for this additive field.
    # At fallback_prepared no push has happened, so null has the same narrow
    # meaning as the loader's legacy default: the originally selected head.
    selected_head = (
        original_head if raw_selected_head is None else raw_selected_head
    )
    marker_local_head = marker.get("current_local_head")
    identity_ok = (
        marker.get("schema_version") == UPDATE_RECOVERY_SCHEMA_VERSION
        and marker.get("phase") == UPDATE_RECOVERY_PHASE_FALLBACK_PREPARED
        and isinstance(old_operation, str)
        and bool(old_operation.strip())
        and old_operation != expected_operation_id
        and marker.get("github_repo") == expected_repo
        and not isinstance(marker.get("pr"), bool)
        and marker.get("pr") == expected_pr
        and marker.get("branch") == expected_branch
        and original_head == expected_head
        and selected_head == expected_head
        and marker.get("base_branch") == expected_base
        and marker_target == expected_target.resolve()
        and _full_oid(marker.get("base_sha"))
        and _full_oid(marker_local_head)
        and marker_local_head == current_head
        and checkpoint.status in _TERMINAL_UPDATE_FALLBACK_STATUSES
        and checkpoint.pending_publication is None
    )
    return marker if identity_ok else None


def _retire_terminal_update_marker(
    marker_path: Path,
    marker: dict,
    *,
    replacement_operation_id: str,
) -> bool:
    """Atomically archive an exact stale journal before a new wrapper starts.

    The hard link is a durable, byte-for-byte preservation of the old journal.
    We unlink the active name only while it still names that same inode.  A
    crash before unlink leaves the old operation resumable; a crash afterwards
    leaves the worktree marker-free and therefore adoptable on the next tick.
    """
    old_operation = str(marker.get("operation_id") or "")
    old_token = hashlib.sha256(old_operation.encode("utf-8")).hexdigest()[:12]
    new_token = hashlib.sha256(
        replacement_operation_id.encode("utf-8")
    ).hexdigest()[:12]
    try:
        with _update_recovery_marker_lock(marker_path):
            raw = marker_path.read_bytes()
            if json.loads(raw) != marker:
                return False
            marker_stat = marker_path.stat(follow_symlinks=False)
            if not stat.S_ISREG(marker_stat.st_mode):
                return False
            content_token = hashlib.sha256(raw).hexdigest()[:12]
            retired_path = marker_path.with_name(
                f"{marker_path.name}.retired-{old_token}-{content_token}-for-{new_token}"
            )
            try:
                os.link(marker_path, retired_path, follow_symlinks=False)
            except FileExistsError:
                pass
            retired_stat = retired_path.stat(follow_symlinks=False)
            current_stat = marker_path.stat(follow_symlinks=False)
            same_inode = (
                retired_stat.st_dev == current_stat.st_dev
                and retired_stat.st_ino == current_stat.st_ino
                and marker_stat.st_dev == current_stat.st_dev
                and marker_stat.st_ino == current_stat.st_ino
            )
            if (
                not same_inode
                or not stat.S_ISREG(retired_stat.st_mode)
                or marker_path.read_bytes() != raw
                or retired_path.read_bytes() != raw
            ):
                return False

            # Persist the archive directory entry before removing the active
            # name.  A power loss at any later point therefore leaves at least
            # one durable copy of the superseded operation journal.
            directory_fd = os.open(marker_path.parent, os.O_RDONLY)
            try:
                os.fsync(directory_fd)
            finally:
                os.close(directory_fd)
            marker_path.unlink()
            directory_fd = os.open(marker_path.parent, os.O_RDONLY)
            try:
                os.fsync(directory_fd)
            finally:
                os.close(directory_fd)
    except (
        OSError,
        RecoveryError,
        TypeError,
        ValueError,
        json.JSONDecodeError,
    ) as exc:
        print(
            f"--- recovery deferred: could not retire stale update journal "
            f"{marker_path} ({exc}) ---",
            flush=True,
        )
        return False
    print(
        f"--- recovery: archived terminal update journal from operation "
        f"{old_operation!r} before starting {replacement_operation_id!r} ---",
        flush=True,
    )
    return True


def _update_recovery_adoption_checkout(
    repo_dir: Path,
    runs_parent: Path,
    issue_num: int,
    branch: str,
    kept_worktrees: dict[int, str],
    *,
    expected_operation_id: str,
    expected_repo: str,
    expected_pr: int,
    expected_head: str,
    expected_base: str,
    target_relpath: str,
) -> Optional[tuple[Path, Path, Optional[dict]]]:
    """Find a dead exact-branch checkpoint for a fresh update operation.

    Unlike an update *resume*, this checkout has no operation journal yet. Its
    ownership evidence is the selected PR's exact named branch plus the
    conductor issue/worktree pairing. The recovery worker will checkpoint all
    files and create the operation-bound journal before merging or publishing.
    A journal or publication recipe from any other operation fails closed,
    except for a terminal ``fallback_prepared`` journal whose complete PR
    identity and unchanged local HEAD can be archived before the new wrapper
    starts.  The returned optional marker is not retired here: the caller first
    verifies the selected remote head and preserves the local branch ref.
    """
    candidates: list[tuple[str, Optional[str]]] = [
        (kept_worktrees.get(issue_num) or "", None)
    ]
    held = _registered_worktree_for_branch(repo_dir, branch)
    if held is not None:
        candidates.append((str(held), None))
    for raw in reversed(list((load_jobs_snapshot(repo_dir) or {}).get("jobs", []))):
        try:
            matches = (
                int(raw.get("issue_num")) == issue_num
                and raw.get("branch") == branch
            )
        except (AttributeError, TypeError, ValueError):
            matches = False
        if matches and raw.get("worktree"):
            candidates.append(
                (str(raw["worktree"]), str(raw.get("workdir") or "") or None)
            )

    seen: set[Path] = set()
    for raw_worktree, raw_workdir in candidates:
        if not raw_worktree:
            continue
        worktree = Path(raw_worktree)
        try:
            resolved = worktree.resolve()
            if resolved in seen or not resolved.is_relative_to(
                runs_parent.resolve()
            ):
                continue
            seen.add(resolved)
        except OSError:
            continue
        if not worktree.is_dir() or _worktree_live_pid(str(worktree)) is not None:
            continue
        workdir = Path(raw_workdir) if raw_workdir else _paired_retry_workdir(
            runs_parent, issue_num, worktree
        )
        if workdir is None:
            prefix = f"wt-i{issue_num}-"
            if not worktree.name.startswith(prefix):
                continue
            workdir = runs_parent / (
                f"wd-i{issue_num}-{worktree.name[len(prefix):]}"
            )
        if workdir.exists() and not workdir.is_dir():
            continue
        try:
            if not workdir.resolve().is_relative_to(runs_parent.resolve()):
                continue
        except OSError:
            continue
        try:
            checkpoint = load_refine_state(workdir / REFINE_STATE_FILENAME)
        except Exception:  # noqa: BLE001 - malformed state remains untouched
            continue
        if checkpoint is not None:
            try:
                if Path(checkpoint.target_folder).resolve() != (
                    worktree / target_relpath
                ).resolve():
                    continue
            except OSError:
                continue
            if checkpoint.pending_publication is not None:
                continue
        current_branch = _git(worktree, "branch", "--show-current")
        current_head = _git(worktree, "rev-parse", "HEAD")
        merge_head = _git(
            worktree, "rev-parse", "--verify", "--quiet", "MERGE_HEAD"
        )
        if (
            current_branch.returncode == 0
            and (current_branch.stdout or "").strip() == branch
            and current_head.returncode == 0
            and bool((current_head.stdout or "").strip())
            and merge_head.returncode != 0
            and (worktree / target_relpath).is_dir()
        ):
            marker_path = workdir / UPDATE_RECOVERY_MARKER_FILENAME
            stale_marker: Optional[dict] = None
            if marker_path.exists():
                stale_marker = _adoptable_terminal_update_marker(
                    marker_path,
                    checkpoint,
                    expected_operation_id=expected_operation_id,
                    expected_repo=expected_repo,
                    expected_pr=expected_pr,
                    expected_branch=branch,
                    expected_head=expected_head,
                    expected_base=expected_base,
                    expected_target=(worktree / target_relpath),
                    current_head=(current_head.stdout or "").strip(),
                )
                if stale_marker is None:
                    continue
            return worktree, workdir, stale_marker
    return None


def _start_over_recovery_resume_checkout(
    repo_dir: Path,
    runs_parent: Path,
    issue_num: int,
    branch: str,
    kept_worktrees: dict[int, str],
    *,
    expected_operation_id: str,
    expected_repo: str,
    target_relpath: str,
    expected_pr: Optional[int],
    expected_head: Optional[str],
    expected_base: Optional[str],
) -> Optional[tuple[Path, Path]]:
    """Find a dead checkpoint owned by this exact start-over operation.

    An issue-number-only kept-worktree entry is not enough: a later recovery
    click must never adopt an older Aristotle project.  The workdir marker is
    the durable identity, while an exact jobs-snapshot row supplies an
    additional restart path to the worktree location.
    """
    raw_candidates = [kept_worktrees.get(issue_num)]
    held = _registered_worktree_for_branch(repo_dir, branch)
    if held is not None:
        raw_candidates.append(str(held))
    for raw in reversed(list((load_jobs_snapshot(repo_dir) or {}).get("jobs", []))):
        try:
            snapshot_matches = (
                int(raw.get("issue_num")) == issue_num
                and raw.get("expected_recovery_id") == expected_operation_id
                and raw.get("recovery_mode") == RECOVERY_START_OVER
                and raw.get("branch") == branch
            )
        except (AttributeError, TypeError, ValueError):
            snapshot_matches = False
        if snapshot_matches and raw.get("worktree"):
            raw_candidates.append(str(raw["worktree"]))

    seen: set[Path] = set()
    for raw in raw_candidates:
        if not raw:
            continue
        worktree = Path(raw)
        try:
            resolved = worktree.resolve()
            if resolved in seen or not resolved.is_relative_to(
                runs_parent.resolve()
            ):
                continue
            seen.add(resolved)
        except OSError:
            continue
        if not worktree.is_dir() or _worktree_live_pid(str(worktree)) is not None:
            continue
        workdir = _paired_retry_workdir(runs_parent, issue_num, worktree)
        if workdir is None:
            continue
        try:
            marker = json.loads(
                (workdir / START_OVER_RECOVERY_MARKER_FILENAME).read_text()
            )
            identity_ok = (
                isinstance(marker, dict)
                and marker.get("schema_version")
                == START_OVER_RECOVERY_SCHEMA_VERSION
                and marker.get("operation_id") == expected_operation_id
                and marker.get("github_repo") == expected_repo
                and int(marker.get("issue")) == issue_num
                and marker.get("branch") == branch
                and marker.get("recovery_pr") == expected_pr
                and marker.get("original_expected_head") == expected_head
                and marker.get("base_branch") == expected_base
                and marker.get("target_path")
                == str((worktree / target_relpath).resolve())
            )
        except (OSError, TypeError, ValueError, json.JSONDecodeError):
            identity_ok = False
        current = _git(worktree, "branch", "--show-current")
        if (
            identity_ok
            and current.returncode == 0
            and (current.stdout or "").strip() == branch
        ):
            return worktree, workdir
    return None


def _write_start_over_recovery_marker(
    workdir: Path,
    *,
    operation_id: str,
    github_repo: str,
    issue_num: int,
    branch: str,
    target_path: Path,
    recovery_pr: Optional[int],
    original_expected_head: Optional[str],
    base_branch: Optional[str],
) -> bool:
    """Durably bind a start-over worktree/checkpoint before worker spawn."""
    marker = workdir / START_OVER_RECOVERY_MARKER_FILENAME
    payload = {
        "schema_version": START_OVER_RECOVERY_SCHEMA_VERSION,
        "operation_id": operation_id,
        "github_repo": github_repo,
        "issue": issue_num,
        "branch": branch,
        "recovery_pr": recovery_pr,
        "original_expected_head": original_expected_head,
        "base_branch": base_branch,
        "target_path": str(target_path.resolve()),
    }
    if marker.exists():
        try:
            if json.loads(marker.read_text()) == payload:
                return True
        except (OSError, json.JSONDecodeError):
            pass
        print(
            f"--- start-over deferred: {marker} belongs to a different or "
            "corrupt recovery operation ---",
            flush=True,
        )
        return False
    try:
        workdir.mkdir(parents=True, exist_ok=True)
        tmp = marker.with_name(f"{marker.name}.tmp-{os.getpid()}")
        with tmp.open("w") as handle:
            handle.write(json.dumps(payload, ensure_ascii=False, indent=2) + "\n")
            handle.flush()
            os.fsync(handle.fileno())
        _durable_replace_file(tmp, marker)
        return True
    except OSError as exc:
        print(
            f"--- start-over deferred: could not persist recovery identity "
            f"in {marker} ({exc}) ---",
            flush=True,
        )
        return False


def _recover_retry_worktrees(
    cfg: ReviewConfig,
    runs_parent: Path,
    *,
    busy_issues: "set[int]",
) -> dict[int, str]:
    """Recover dead/failed prior jobs from ``jobs.json`` across a restart.

    Only currently-pending rejected issues qualify, and a worktree with a live
    job is never adopted here (``_adopt_inherited_jobs`` owns it).  The paired
    workdir must still exist, which is the proof that the Aristotle checkpoint
    can be continued.  This closes the second session-loss door: a conductor
    restart between retry attempts no longer reaps the worktree/checkpoint and
    starts the paid proof search from scratch.
    """
    review_state = load_state(cfg)
    pending = {
        n for n, _st in pending_rejections_needing_iteration(cfg, None)
        if n not in busy_issues
        and review_state.issues.get(n) is not None
    }
    # A stalled verdict is intentionally not dispatchable, but its kept
    # worktree/checkpoint must survive restart so a later human requeue can
    # continue the same Aristotle session instead of orphan-reaping it.
    stalled = {
        n
        for n, st in review_state.issues.items()
        if st.status == "stalled" and n not in busy_issues
    }
    recovered: dict[int, str] = {}
    snap = load_jobs_snapshot(cfg.repo_dir)
    rows = list((snap or {}).get("jobs", []))
    # Newest row wins if an old snapshot somehow contains duplicate attempts.
    for raw in reversed(rows):
        # A compare-and-swap loser belongs to an older verdict/recovery
        # generation. Even if best-effort worktree cleanup left the directory
        # behind, it must never be adopted as the checkpoint for the winner.
        if raw.get("status") == "superseded":
            continue
        autonomous = bool(raw.get("fill") or raw.get("repair"))
        try:
            issue = int(raw.get("issue_num"))
        except (TypeError, ValueError):
            continue
        recovery_key = _kept_worktree_key(issue, fill=bool(raw.get("fill")))
        if (
            not autonomous
            and issue not in pending
            and issue not in stalled
        ) or recovery_key in recovered:
            continue
        entry = review_state.issues.get(issue)
        if (
            not autonomous
            and entry is not None
            and entry.recovery_mode == RECOVERY_START_OVER
            and (
                raw.get("recovery_mode") != RECOVERY_START_OVER
                or raw.get("expected_recovery_id") != entry.recovery_id
            )
        ):
            continue
        worktree_raw = raw.get("worktree")
        if not worktree_raw:
            continue
        worktree = Path(worktree_raw)
        if not worktree.is_dir():
            continue
        if _worktree_live_pid(str(worktree)) is not None:
            continue
        paired = _paired_retry_workdir(runs_parent, issue, worktree)
        if paired is None:
            continue
        if autonomous:
            try:
                checkpoint = load_refine_state(paired / REFINE_STATE_FILENAME)
            except Exception:  # noqa: BLE001
                checkpoint = None
            resumable = (
                raw.get("status")
                in {"running", "failed", "interrupted", "publication_pending"}
                or (
                    checkpoint is not None
                    and (
                        checkpoint.pending_publication is not None
                        or checkpoint.force_continuation
                        or checkpoint.preserve_local_branch
                    )
                )
            )
            if not resumable:
                continue
        _clear_worktree_pid_marker(str(worktree))
        recovered[recovery_key] = str(worktree)
        print(
            f"  recovered retry checkpoint for #{issue}: {worktree} "
            "(worktree + Aristotle session will be continued)",
            flush=True,
        )
    return recovered


# --- Scheduler --------------------------------------------------------------------


def _target_relpath(cfg: ReviewConfig, chapter: int) -> str:
    return cfg.target_path_template.format(chapter=chapter).strip("/")


#: Throttle GitHub queue discovery (its gh calls) to at most once per this many
#: seconds; between, the already-seeded local queue serves, so a frequent tick
#: loop never hammers the GitHub API.
GH_DISCOVERY_INTERVAL = 60.0
_last_gh_discovery = 0.0


def _github_discovered_rejections(cfg: ReviewConfig):
    """GitHub-driven replacement for the local
    ``pending_rejections_needing_iteration`` query (the conductor's
    ``--queue-from-github``): list ``review:rejected`` issues, recover each note
    from its embedded reject comment, SEED/refresh the local state.json (so local
    iteration-tracking still decides what THIS conductor already refined), then
    return the SAME (issue, IssueState) needing-iteration list. The point: a
    collaborator's conductor on a fresh clone sees rejections fired on a shared
    deck — with no state.json syncing.

    gh discovery is throttled to once per :data:`GH_DISCOVERY_INTERVAL`; between,
    the seeded local queue serves. Fail-soft: any gh failure logs once and the
    function still returns the local queue, so a tick never crashes."""
    global _last_gh_discovery
    from marathon.review.github import fetch_reject_note, list_rejected_issues
    from marathon.review.sync import reconcile_github_rejection

    now = _now()
    if now - _last_gh_discovery >= GH_DISCOVERY_INTERVAL:
        _last_gh_discovery = now
        nums = list_rejected_issues(cfg)
        if nums is None:
            print("  queue-from-github: gh discovery failed this cycle; "
                  "using the local queue")
        else:
            for issue_num in nums:
                try:
                    note, gh_ts = fetch_reject_note(issue_num, cfg.github_repo)
                    reconcile_github_rejection(cfg, issue_num, note, gh_ts)
                except Exception as exc:  # noqa: BLE001 — never crash a tick
                    print(f"  queue-from-github: reconcile #{issue_num} "
                          f"failed ({exc}); skipping")
    return pending_rejections_needing_iteration(cfg, None)


def _reconcile_merged_rejections(cfg: ReviewConfig) -> list[int]:
    """Heal issues left stuck in 'rejected' after their refine PR was MERGED
    OUTSIDE the conductor's automerge — e.g. a human merged it by hand. Automerge
    does the post-merge requeue itself (``_requeue_refined_issue``); a hand-merge
    bypasses it, so the issue reads 'rejected' forever and never re-enters the
    review feed (the recurring #98 trap). Each tick (throttled by the caller) we
    requeue any locally-'rejected' issue whose refine branch has a MERGED PR that
    landed STRICTLY AFTER the current rejection — so a fresh re-reject whose OLD PR
    merged is left alone (it is genuinely awaiting a new refine, not a stuck
    re-review). Fail-soft: a gh failure logs and the tick continues. Returns the
    issue numbers reconciled."""
    from marathon.review.automerge import _REFINE_BRANCH_RE, _requeue_refined_issue
    from marathon.review.github import gh
    from marathon.review.state import load_state
    from marathon.review.sync import _ts_strictly_newer

    try:
        state = load_state(cfg)
    except Exception:  # noqa: BLE001 — never crash a tick
        return []
    rejected = {
        n: st for n, st in state.issues.items()
        if getattr(st, "status", None) == "rejected"
    }
    if not rejected:
        return []
    # One gh read: recent MERGED refine PRs -> {issue_num: (branch, mergedAt)}.
    merged: dict[int, tuple[str, Optional[str]]] = {}
    try:
        cp = gh("pr", "list", "--repo", cfg.github_repo, "--state", "merged",
                "--json", "headRefName,mergedAt", "--limit", "100", check=False)
        if cp.returncode != 0:
            return []
        for pr in json.loads(cp.stdout or "[]"):
            m = _REFINE_BRANCH_RE.match(pr.get("headRefName", "") or "")
            if m:
                n = int(m.group(1))
                ma = pr.get("mergedAt")
                # Keep the MOST RECENT merged PR per issue. gh's list order is
                # not guaranteed newest-first, and an issue refined multiple
                # times has several merged refine PRs; overwriting blindly could
                # retain an OLD merge that predates the latest rejection, so the
                # `_ts_strictly_newer` gate below wrongly skips it and the issue
                # stays stuck 'rejected' forever (the bug that left #162/#164
                # out of the review queue). mergedAt is ISO-UTC, so string
                # comparison is chronological.
                prev = merged.get(n)
                if prev is None or (ma or "") > (prev[1] or ""):
                    merged[n] = (pr["headRefName"], ma)
    except Exception as exc:  # noqa: BLE001 — never crash a tick
        print(f"  merged-rejection reconcile: gh failed ({exc}); skipping",
              flush=True)
        return []
    reconciled: list[int] = []
    for issue_num, st in rejected.items():
        if issue_num not in merged:
            continue
        # An explicit recovery operation owns this issue/branch until its own
        # generation-aware completion path releases it.  A hand-merged old PR
        # must not silently cancel retry/update/start-over intent.
        if getattr(st, "recovery_id", None) is not None:
            continue
        branch, merged_at = merged[issue_num]
        if not _ts_strictly_newer(merged_at, getattr(st, "verdict_ts", None)):
            continue  # merge predates this rejection — a fresh reject, not a stuck one
        n = _requeue_refined_issue(
            cfg,
            branch,
            expected_verdict_ts=st.verdict_ts,
            # Bind the observed absence too: a recovery queued after this scan
            # wins the CAS and survives the old merge reconciliation.
            expected_recovery_id=None,
        )
        if n is not None:
            reconciled.append(n)
    if reconciled:
        print(f"--- reconciled {len(reconciled)} merged-but-rejected issue(s) into "
              f"re-review (refine PR merged out-of-band): {sorted(reconciled)} ---",
              flush=True)
    return reconciled


def _eligible_issues(
    cfg: ReviewConfig, warned_unknown: set[int],
    queue_from_github: bool = False,
) -> list[tuple[int, int]]:
    """(issue, chapter) pairs that still need an iteration, oldest
    verdict first ACROSS ALL chapters (the project-wide query in
    review.state already sorts by verdict_ts). Issues not registered in
    any chapter have no target folder to refine — skipped with one
    warning each per conductor run.

    With ``queue_from_github`` the candidate set is discovered from the GitHub
    ``review:rejected`` label (seeding local state.json) rather than the local
    queue — see :func:`_github_discovered_rejections`."""
    out: list[tuple[int, int]] = []
    source = (
        _github_discovered_rejections(cfg) if queue_from_github
        else pending_rejections_needing_iteration(cfg, None)
    )
    for issue_num, _entry in source:
        chapter = cfg.chapter_of_issue(issue_num)
        if chapter is None:
            if issue_num not in warned_unknown:
                warned_unknown.add(issue_num)
                print(
                    f"  warning: rejected issue #{issue_num} is not in any "
                    "[[chapters]] registry; the conductor cannot derive its "
                    "target folder — skipping (register the chapter to queue it)"
                )
            continue
        out.append((issue_num, chapter))
    return out


def _pick_dispatchable(
    cfg: ReviewConfig,
    jobs: list[ConductorJob],
    not_before: dict[int, float],
    now: float,
    slots: int,
    warned_unknown: Optional[set[int]] = None,
    queue_from_github: bool = False,
    recovery_modes: Optional["frozenset[str]"] = None,
) -> list[tuple[int, int]]:
    """The deterministic scheduling decision: up to ``slots`` (issue,
    chapter) pairs to dispatch this tick.

    Rules, in order: oldest verdict first across all chapters; never the
    same issue twice (a running job's rejection still satisfies
    ``needs_iteration`` until its clean exit records the iteration);
    never before the issue's backoff ``not_before``; never two jobs with
    equal target chapter folders — the younger is deferred (Phase-3
    collision unit; decl-level overlap arrives with the Phase-5 audit
    engine)."""
    if slots <= 0:
        return []
    if warned_unknown is None:
        warned_unknown = set()
    running = [j for j in jobs if j.status == "running"]
    busy_issues = {j.issue_num for j in running}
    busy_targets = {j.target for j in running}
    allowed_issues: Optional[set[int]] = None
    if recovery_modes is not None:
        allowed_issues = {
            issue_num
            for issue_num, entry in load_state(cfg).issues.items()
            if entry.recovery_mode in recovery_modes
        }
    picks: list[tuple[int, int]] = []
    for issue_num, chapter in _eligible_issues(cfg, warned_unknown, queue_from_github):
        if len(picks) >= slots:
            break
        if issue_num in busy_issues:
            continue
        if allowed_issues is not None and issue_num not in allowed_issues:
            continue
        if not_before.get(issue_num, 0.0) > now:
            continue
        target = _target_relpath(cfg, chapter)
        if target in busy_targets:
            continue  # same-chapter-folder collision: defer the younger
        busy_targets.add(target)
        picks.append((issue_num, chapter))
    return picks


def _park_deferred_recovery(cfg: ReviewConfig, issue_num: int) -> bool:
    """Return a failed pre-dispatch recovery to the visible chooser.

    Recovery intent makes a row dispatchable and therefore removes it from the
    deck's parked list. If an exact-head/worktree safety guard then refuses to
    launch, leaving the row rejected would hide it while the conductor retries
    forever. CAS-stalling the same operation keeps all work intact and makes
    the next decision visible again.
    """
    entry = load_state(cfg).issues.get(issue_num)
    if (
        entry is None
        or entry.status != "rejected"
        or not entry.recovery_id
    ):
        return False
    parked = record_stall(
        cfg,
        issue_num,
        expected_verdict_ts=entry.verdict_ts,
        expected_recovery_id=entry.recovery_id,
    )
    if parked is None:
        return False
    print(
        f"--- recovery #{issue_num} could not pass its safe pre-dispatch "
        "guards; parked visibly for a refreshed recovery choice ---",
        flush=True,
    )
    return True


def _observe_recovery_branch(
    repo_dir: Path, branch: str
) -> tuple[bool, Optional[str], str]:
    """Seam for the pre-dispatch remote observation (tests fake this)."""
    from marathon.post_pipeline import observe_remote_branch

    return observe_remote_branch(repo_dir, branch)


def _reconcile_recovery_head(
    cfg: ReviewConfig,
    issue_num: int,
    branch: str,
    pinned_head: str,
    *,
    expected_verdict_ts: Optional[str],
    expected_recovery_id: Optional[str],
    kept_worktrees: dict[int, str],
) -> Optional[str]:
    """Observe the recovery branch BEFORE dispatch and return the head to pin.

    Three outcomes:

    * remote still at the pinned head → dispatch with it, unchanged;
    * remote moved to a head we can PROVE is our own prior attempt's committed
      checkpoint (a kept worktree for this issue/branch is at exactly that
      commit) → CAS-advance the stored pin and dispatch with the new head —
      this is the #240 wedge: an attempt publishes its checkpoint, then every
      later attempt refuses the branch "changed" by our own push;
    * remote moved to anything else (or observation failed) → never dispatch a
      doomed job. Foreign movement parks the row visibly (the operator must
      re-pin in the deck's Recover dialog); an observation failure defers
      quietly to the next tick.
    """
    ok, observed_oid, msg = _observe_recovery_branch(cfg.repo_dir, branch)
    if not ok:
        print(
            f"--- deferring #{issue_num}: cannot observe recovery branch "
            f"{branch} ({msg}) ---",
            flush=True,
        )
        return None
    observed = observed_oid or ""
    if observed == pinned_head:
        return pinned_head
    own_heads: set[str] = set()
    candidates: list[str] = []
    raw = kept_worktrees.get(issue_num)
    if raw:
        candidates.append(raw)
    held = _registered_worktree_for_branch(cfg.repo_dir, branch)
    if held is not None:
        candidates.append(str(held))
    for cand in candidates:
        cp = _git(Path(cand), "rev-parse", "HEAD")
        if cp.returncode == 0:
            head = (cp.stdout or "").strip()
            if head:
                own_heads.add(head)
    if observed and observed in own_heads:
        advanced = advance_recovery_head_if_current(
            cfg,
            issue_num,
            expected_verdict_ts=expected_verdict_ts,
            expected_recovery_id=expected_recovery_id,
            expected_head=pinned_head,
            new_head=observed,
        )
        if advanced is None:
            print(
                f"--- deferring #{issue_num}: recovery row changed while "
                "adopting our own published head ---",
                flush=True,
            )
            return None
        print(
            f"--- #{issue_num}: adopting our own published checkpoint "
            f"{observed[:12]} as the recovery head (was "
            f"{pinned_head[:12]}) ---",
            flush=True,
        )
        return observed
    print(
        f"--- #{issue_num}: remote {branch} moved to "
        f"{observed[:12] or '<absent>'} after recovery was chosen at "
        f"{pinned_head[:12]}, and the new head is not our own checkpoint; "
        "parking for a refreshed recovery choice ---",
        flush=True,
    )
    if _park_deferred_recovery(cfg, issue_num):
        notify(
            "Marathon conductor",
            f"#{issue_num} recovery parked: {branch} moved to "
            f"{observed[:12] or '<absent>'} — reopen Recover to re-pin.",
        )
    return None


# --- Worktree management -----------------------------------------------------------


def _git(repo_dir: Path, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git", "-C", str(repo_dir), *args],
        capture_output=True,
        text=True,
    )


def _registered_worktree_for_branch(
    repo_dir: Path, branch: str
) -> Optional[Path]:
    """Return the registered worktree currently holding ``branch``."""
    cp = _git(repo_dir, "worktree", "list", "--porcelain")
    if cp.returncode != 0:
        return None
    current: Optional[Path] = None
    for line in (cp.stdout or "").splitlines():
        if line.startswith("worktree "):
            current = Path(line[len("worktree "):].strip())
        elif line.strip() == f"branch refs/heads/{branch}":
            return current
    return None


def _unpublished_worktree_reason(
    repo_dir: Path, worktree: Path, branch: Optional[str]
) -> Optional[str]:
    """Explain why force-removing a dead worktree could lose work.

    Fail closed: unreadable status/HEAD/remote state is itself a reason to keep
    the tree. Runtime marker files are ignored by git and therefore do not
    create false positives here.
    """
    status = _git(worktree, "status", "--porcelain")
    if status.returncode != 0:
        return "git status could not be read"
    if (status.stdout or "").strip():
        return "it has uncommitted changes"
    head = _git(worktree, "rev-parse", "HEAD")
    if head.returncode != 0 or not (head.stdout or "").strip():
        return "its HEAD could not be read"
    if not branch:
        return "it is detached and may be an unpublished recovery checkout"
    remote = _git(
        repo_dir, "ls-remote", "--heads", "origin", f"refs/heads/{branch}"
    )
    if remote.returncode != 0:
        return "the remote branch could not be checked"
    remote_sha = ""
    for line in (remote.stdout or "").splitlines():
        fields = line.split()
        if len(fields) >= 2 and fields[1] == f"refs/heads/{branch}":
            remote_sha = fields[0]
            break
    local_sha = (head.stdout or "").strip()
    if not remote_sha:
        return "its branch has no remote publication"
    if local_sha != remote_sha:
        return "its HEAD is ahead of or diverged from the remote branch"
    return None


def _remove_worktree(repo_dir: Path, worktree: Path) -> None:
    """Best-effort removal after a successful job. ``--force`` because
    refine runs leave untracked runtime droppings; the work itself was
    already committed + pushed by the job's --auto-commit/--auto-pr."""
    proc = _git(repo_dir, "worktree", "remove", "--force", str(worktree))
    if proc.returncode != 0:
        print(
            f"  warning: could not remove worktree {worktree} "
            f"({(proc.stderr or proc.stdout).strip()}); "
            "run `marathon conductor run --prune` later",
            flush=True,
        )


#: Marker file a dispatch drops inside the job's worktree so a LATER conductor
#: process can tell the worktree belongs to a still-running job. The jobs.json
#: snapshot alone can't carry this across a restart: the new conductor's first
#: ``write_jobs_snapshot`` full-rewrites it with only its own (empty) job list.
WORKTREE_PID_MARKER = Path(".marathon") / "conductor" / "refine.pid"
# A pre-spawn launch marker with no PID means the trampoline has not completed
# its marker handshake yet.  Protect the worktree briefly instead of confusing
# "not observable yet" with "no worker" and reaping underneath a fork/exec.
LAUNCH_INTENT_GRACE_SECONDS = 120.0
LAUNCHING_PID_SENTINEL = -1
WORKER_HANDSHAKE_TIMEOUT_SECONDS = 10.0

#: ``{worktree_path_str: pid}`` for jobs a PREVIOUS conductor process left
#: running (populated once at startup from the pre-restart jobs snapshot).
#: Consulted by the reap guard alongside the pid marker; liveness is
#: re-checked at use so a finished job's entry never blocks a reap for long.
_INHERITED_RUNNING: dict[str, int] = {}


def _durable_replace_file(tmp: Path, destination: Path) -> None:
    """Replace ``destination`` and persist the directory entry before return."""
    if os.name == "nt":  # pragma: no cover - exercised on Windows CI
        import ctypes

        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        move_file = kernel32.MoveFileExW
        move_file.argtypes = [ctypes.c_wchar_p, ctypes.c_wchar_p, ctypes.c_uint]
        move_file.restype = ctypes.c_int
        # REPLACE_EXISTING | WRITE_THROUGH
        if not move_file(str(tmp), str(destination), 0x1 | 0x8):
            raise OSError(ctypes.get_last_error(), "MoveFileExW failed")
        return
    os.replace(tmp, destination)
    directory_fd = os.open(destination.parent, os.O_RDONLY)
    try:
        os.fsync(directory_fd)
    finally:
        os.close(directory_fd)


def _write_worktree_pid_marker(
    worktree: Path,
    pid: int,
    *,
    job: Optional["ConductorJob"] = None,
) -> bool:
    """Atomically persist a worktree's live worker identity.

    Production dispatches include the complete job record.  That record is
    first written with ``status=launching`` and no pid *before* ``Popen``, then
    rewritten with the detached worker's pid.  A replacement conductor can
    therefore recover a worker even if its parent dies between spawn and the
    next repo-level ``jobs.json`` snapshot.  The pid-only form remains readable
    for worktrees created by older Marathon releases.
    """
    try:
        marker = worktree / WORKTREE_PID_MARKER
        marker.parent.mkdir(parents=True, exist_ok=True)
        gitignore = marker.parent / ".gitignore"
        if not gitignore.is_file():
            gitignore.write_text("*\n")
        if job is None:
            content = f"{pid}\n"
        else:
            payload = job.to_json()
            payload["marker_schema_version"] = 1
            payload["pid"] = pid or None
            content = json.dumps(payload, ensure_ascii=False, indent=2) + "\n"
        tmp = marker.with_name(f"{marker.name}.tmp-{os.getpid()}")
        with tmp.open("w") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        _durable_replace_file(tmp, marker)
        return True
    except OSError as e:
        print(f"  warning: could not write pid marker in {worktree} ({e})",
              flush=True)
        return False


def _read_worktree_job_marker(
    worktree: str | Path,
) -> tuple[Optional[int], Optional[dict]]:
    """Return ``(pid, job_record)`` for new JSON and legacy integer markers."""
    try:
        raw = (Path(worktree) / WORKTREE_PID_MARKER).read_text().strip()
    except OSError:
        return None, None
    try:
        parsed = json.loads(raw)
    except (json.JSONDecodeError, TypeError):
        return None, None
    if isinstance(parsed, int):
        return parsed, None
    if not isinstance(parsed, dict):
        return None, None
    try:
        pid = int(parsed["pid"]) if parsed.get("pid") else None
    except (TypeError, ValueError):
        pid = None
    return pid, parsed


def _worker_ack_path(
    worktree: str | Path, pid: int, launch_nonce: str,
) -> Path:
    marker = Path(worktree) / WORKTREE_PID_MARKER
    return marker.with_name(f"{marker.name}.ack-{pid}-{launch_nonce}")


def _signal_worker_ack(
    worktree: str | Path, pid: int, launch_nonce: str,
) -> bool:
    """Release the trampoline only after the running marker is durable."""
    try:
        # This file is intentionally created only *after* the marker's file and
        # directory fsync complete. Its own persistence is unnecessary: if a
        # host crash loses it, the already-durable running marker still lets the
        # replacement conductor reconcile any remote work that began.
        ack = _worker_ack_path(worktree, pid, launch_nonce)
        tmp = ack.with_name(f"{ack.name}.tmp-{os.getpid()}")
        tmp.write_text(f"{launch_nonce}\n")
        # Atomic rename is the first moment the trampoline can observe this
        # generation's ACK, and it is the final fallible operation here.
        os.replace(tmp, ack)
        return True
    except OSError as exc:
        print(f"  warning: could not signal worker launch ACK ({exc})", flush=True)
        return False


def _await_worker_handshake(
    worktree: Path,
    proc: object,
    *,
    timeout: float = WORKER_HANDSHAKE_TIMEOUT_SECONDS,
) -> Optional[dict]:
    """Wait until the detached trampoline durably publishes its PID/PGID."""
    deadline = time.monotonic() + timeout
    expected_pid = int(getattr(proc, "pid"))
    while time.monotonic() < deadline:
        pid, record = _read_worktree_job_marker(worktree)
        if (
            pid == expected_pid
            and record is not None
            and record.get("status") in {"ready", "running"}
        ):
            if record.get("status") == "ready":
                try:
                    acknowledged = ConductorJob.from_json(record)
                    acknowledged.pid = expected_pid
                    acknowledged.status = "running"
                except (TypeError, ValueError):
                    return None
                if not _write_worktree_pid_marker(
                    worktree, expected_pid, job=acknowledged
                ):
                    return None
                if not acknowledged.launch_nonce or not _signal_worker_ack(
                    worktree, expected_pid, acknowledged.launch_nonce
                ):
                    return None
                record = acknowledged.to_json()
            return record
        poll = getattr(proc, "poll")
        if poll() is not None:
            return None
        time.sleep(0.01)
    return None


def _canonical_worktree_key(worktree: str | Path) -> str:
    """Canonical identity used to dedupe git/macOS path aliases."""
    try:
        return str(Path(worktree).resolve(strict=False))
    except OSError:
        return os.path.realpath(os.fspath(worktree))


def _worktree_path_spellings(worktree: str | Path) -> set[str]:
    """Raw + canonical path spellings that may appear in ``ps`` output."""
    raw = os.path.abspath(os.fspath(worktree))
    spellings = {raw, os.path.realpath(raw), _canonical_worktree_key(raw)}
    # macOS commonly reports a /private-prefixed spelling for /tmp paths.
    for value in tuple(spellings):
        if value.startswith("/tmp/"):
            spellings.add("/private" + value)
        elif value.startswith("/private/tmp/"):
            spellings.add(value[len("/private"):])
    return {value for value in spellings if value}


def _command_mentions_worktree(command: str, worktree: str | Path) -> bool:
    return any(path in command for path in _worktree_path_spellings(worktree))


def _command_is_marathon_worker(command: str) -> bool:
    padded = f" {command} "
    return (
        " -m marathon.worker_launch " in padded
        or any(
            f" -m marathon {kind} " in padded
            for kind in ("refine", "fill", "repair")
        )
        or any(
            f" -m marathon.recovery {action} " in padded
            for action in ("retry-build", "update-continue")
        )
    )


def _process_group_alive(pgid: Optional[int]) -> bool:
    """Whether a detached POSIX worker group still has any members."""
    if not pgid or pgid <= 0 or os.name == "nt":
        return False
    try:
        os.killpg(pgid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return False


def _process_group_matches_worktree(
    pgid: Optional[int], worktree: str,
) -> Optional[bool]:
    """True/False when a POSIX group can be identified; None if ambiguous."""
    if not pgid or pgid <= 0 or os.name == "nt":
        return None
    try:
        cp = subprocess.run(
            ["ps", "-axo", "pgid=,command="],
            capture_output=True,
            text=True,
            timeout=5,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if cp.returncode != 0:
        return None
    found_group = False
    for line in (cp.stdout or "").splitlines():
        fields = line.strip().split(maxsplit=1)
        if len(fields) != 2:
            continue
        try:
            row_pgid = int(fields[0])
        except ValueError:
            continue
        if row_pgid != pgid:
            continue
        found_group = True
        if (
            _command_is_marathon_worker(fields[1])
            and _command_mentions_worktree(fields[1], worktree)
        ):
            return True
    return None if found_group else False


def _job_process_group_live(
    pid: Optional[int], pgid: Optional[int], worktree: str,
    *,
    strict_identity: bool = False,
) -> bool:
    """Fail-safe liveness for a worker leader or its surviving descendants."""
    if pid is not None and _pid_is_job_for(
        pid, worktree, strict=strict_identity
    ):
        return True
    # ``start_new_session`` makes PGID equal the original worker PID.  The ID
    # remains reserved while descendants live, so checking the group protects a
    # worktree even if the Python leader died during lake/git cleanup.
    group_match = _process_group_matches_worktree(pgid, worktree)
    if group_match is True:
        return True
    if group_match is None and not strict_identity:
        return _process_group_alive(pgid)
    return False


def snapshot_job_is_live(raw: dict) -> bool:
    """Whether a serialized ``running`` job definitely owns a live worker.

    ``jobs.json`` deliberately survives conductor and machine restarts.  Its
    status field is therefore launch history, not proof of current liveness.
    Read-only surfaces must use the same PID/PGID + worktree identity checks as
    conductor recovery; otherwise an interrupted job remains "building"
    forever even though the Status tab correctly reports a dead process.

    This is intentionally stricter than the cleanup guard: an ambiguous
    process-group lookup should protect a worktree from deletion, but it is not
    evidence that the deck may tell a reviewer an issue is actively building.
    Legacy rows without a worktree cannot be identity-checked and therefore do
    not count as live on read-only status surfaces.
    """
    if raw.get("status") != "running":
        return False
    try:
        pid = int(raw["pid"]) if raw.get("pid") else None
        pgid = int(raw["pgid"]) if raw.get("pgid") else None
    except (TypeError, ValueError):
        return False
    worktree = str(raw.get("worktree") or "")
    if worktree:
        return _job_process_group_live(
            pid, pgid, worktree, strict_identity=True
        )
    return False


def _registered_worktrees(repo_dir: Path) -> list[str]:
    """Absolute paths currently registered in the consumer's git worktree set."""
    cp = _git(repo_dir, "worktree", "list", "--porcelain")
    if cp.returncode != 0:
        return []
    return [
        line[len("worktree "):].strip()
        for line in cp.stdout.splitlines()
        if line.startswith("worktree ")
    ]


def _pid_is_job_for(pid: int, worktree: str, *, strict: bool = False) -> bool:
    """True iff ``pid`` is alive AND is genuinely a marathon job running in
    ``worktree`` — i.e. its command line references that worktree's full raw or
    canonical path (present via the target and ``--repo-dir``). Full-path
    identity makes this robust to PID reuse *and* to two repositories choosing
    the same timestamped worktree basename; path aliases such as macOS
    ``/tmp``/``/private/tmp`` are normalized explicitly."""
    if pid <= 0 or not daemon.process_alive(pid):
        return False
    try:
        if os.name == "nt":  # pragma: no cover - exercised on Windows CI
            query = (
                "(Get-CimInstance Win32_Process -Filter \"ProcessId = "
                f"{pid}\").CommandLine"
            )
            cp = subprocess.run(
                ["powershell.exe", "-NoProfile", "-Command", query],
                capture_output=True, text=True, timeout=5,
            )
        else:
            cp = subprocess.run(
                ["ps", "-p", str(pid), "-o", "command="],
                capture_output=True, text=True, timeout=5,
            )
    except (OSError, subprocess.SubprocessError):
        # Strict callers (adoption/completion ownership) never trust bare PID
        # liveness. Reap guards fail safe and merely protect the worktree.
        return not strict
    if cp.returncode != 0 or not (cp.stdout or "").strip():
        return not strict
    command = cp.stdout or ""
    return _command_is_marathon_worker(command) and _command_mentions_worktree(
        command, worktree
    )


def _clear_worktree_pid_marker(worktree: str) -> None:
    """Delete the live-job marker once the conductor OBSERVES the job exit
    (failed/interrupted branches keep the worktree for debugging, so the
    marker would otherwise linger holding a dead — reusable — pid). The
    success path removes the whole worktree, so it needs no explicit clear."""
    try:
        marker = Path(worktree) / WORKTREE_PID_MARKER
        marker.unlink(missing_ok=True)
        for ack in marker.parent.glob(f"{marker.name}.ack-*"):
            ack.unlink(missing_ok=True)
    except OSError:
        pass
    _INHERITED_RUNNING.pop(str(worktree), None)


def _worktree_live_pid(worktree: str) -> Optional[int]:
    """The pid of a LIVE job still using ``worktree``, or None.

    Two sources, each verified as a genuine job-for-this-worktree (see
    :func:`_pid_is_job_for` — pid-reuse-safe): the pid marker the dispatch
    dropped into the worktree (survives snapshot rewrites), and the
    inherited-running map captured from the pre-restart jobs snapshot at
    startup (covers worktrees dispatched by an older conductor that predates
    the marker). A dead/stale/reused pid is pruned from the inherited map so
    a one-time miss never lingers."""
    pid, record = _read_worktree_job_marker(worktree)
    pgid: Optional[int] = None
    if record is not None:
        try:
            pgid = int(record["pgid"]) if record.get("pgid") else None
        except (TypeError, ValueError):
            pgid = None
    if _job_process_group_live(pid, pgid, worktree):
        return pid or pgid
    if (
        record is not None
        and record.get("status") == "launching"
        and pid is None
    ):
        try:
            age = time.time() - (
                Path(worktree) / WORKTREE_PID_MARKER
            ).stat().st_mtime
        except OSError:
            age = LAUNCH_INTENT_GRACE_SECONDS + 1
        if age <= LAUNCH_INTENT_GRACE_SECONDS:
            return LAUNCHING_PID_SENTINEL
    pid = _INHERITED_RUNNING.get(worktree)
    if pid is not None:
        if _pid_is_job_for(pid, worktree):
            return pid
        _INHERITED_RUNNING.pop(worktree, None)  # its job exited — stop tracking
    return None


def _adopt_inherited_jobs(repo_dir: Path) -> list["ConductorJob"]:
    """ConductorJobs for still-alive refines a PREVIOUS conductor left running,
    reconstructed from its jobs snapshot at startup — BEFORE the first
    ``write_jobs_snapshot`` erases those rows (a pkill'd conductor's refine
    children survive it in their own process group).

    ADOPTING them as tracked jobs (``proc is None`` — we didn't spawn them, so
    they're polled via the pid) does two things a bare worktree-protection map
    can't: ``_pick_dispatchable`` sees the issue/target as busy and won't
    RE-DISPATCH a duplicate billed run while the inherited refine finishes; and
    ``_reap_finished`` handles the inherited job's completion (mark iterated if
    it opened a PR, else requeue-if-no-PR) instead of leaving a re-dispatch
    race. Without this, a mid-refine restart protected the worktree from
    destruction but still burned a wasteful duplicate run when it finished.

    Also (re)populates ``_INHERITED_RUNNING`` so the reap/prune guards protect
    these worktrees even on the (now unreachable via dispatch) collision path."""
    _INHERITED_RUNNING.clear()
    out: list["ConductorJob"] = []
    adopted_worktrees: set[str] = set()
    snap = load_jobs_snapshot(repo_dir)
    for raw in (snap or {}).get("jobs", []):
        pid, worktree = raw.get("pid"), raw.get("worktree")
        if raw.get("status") != "running" or not worktree:
            continue
        try:
            live_pid = int(pid) if pid else None
            pgid = int(raw["pgid"]) if raw.get("pgid") else None
        except (TypeError, ValueError):
            continue
        strictly_live = _job_process_group_live(
            live_pid, pgid, str(worktree), strict_identity=True
        )
        protected_live = _job_process_group_live(
            live_pid, pgid, str(worktree)
        )
        if not strictly_live and not protected_live:
            continue
        recovered = dict(raw)
        recovered["pid"] = live_pid
        recovered["pgid"] = pgid
        recovered["status"] = "running"
        job = ConductorJob.from_json(recovered)
        out.append(job)
        key = _canonical_worktree_key(str(worktree))
        adopted_worktrees.add(key)
        if live_pid is not None and protected_live:
            _INHERITED_RUNNING[str(worktree)] = live_pid

    # The per-worktree launch marker is written before spawn and carries the
    # complete job record.  Scan it after jobs.json so a crash between Popen and
    # the next snapshot cannot leave a detached, billed worker invisible.
    for worktree in _registered_worktrees(repo_dir):
        key = _canonical_worktree_key(worktree)
        if key in adopted_worktrees:
            continue
        pid, raw = _read_worktree_job_marker(worktree)
        if raw is None or raw.get("status") not in {"ready", "running"}:
            continue
        try:
            recorded_worktree = str(raw.get("worktree") or "")
            if Path(recorded_worktree).resolve() != Path(worktree).resolve():
                continue
            pgid = int(raw["pgid"]) if raw.get("pgid") else None
            strictly_live = _job_process_group_live(
                pid, pgid, worktree, strict_identity=True
            )
            marker_status = raw.get("status")
            # A READY trampoline has not started billed work. Only a strictly
            # identified replacement conductor may ACK it. A RUNNING marker,
            # by contrast, is itself the durable completion journal: retain it
            # even when the worker already exited so _reap_adopted_job can
            # reconcile its checkpoint/PR instead of reaping and rebilling.
            if marker_status == "ready" and not strictly_live:
                continue
            recovered = dict(raw)
            recovered["pid"] = pid
            recovered["pgid"] = pgid
            recovered["status"] = "running"
            job = ConductorJob.from_json(recovered)
        except (KeyError, OSError, TypeError, ValueError):
            continue
        out.append(job)
        adopted_worktrees.add(key)
        if pid is not None and _job_process_group_live(
            pid, pgid, worktree
        ):
            _INHERITED_RUNNING[worktree] = pid
        if marker_status == "ready":
            # The prior conductor died after the trampoline published identity
            # but before ACK. This durable write authorizes the waiting
            # trampoline to start the billed command under the new conductor.
            if pid is None or not _write_worktree_pid_marker(
                Path(worktree), pid, job=job
            ):
                out.pop()
                adopted_worktrees.discard(key)
                _INHERITED_RUNNING.pop(worktree, None)
            elif not job.launch_nonce or not _signal_worker_ack(
                worktree, pid, job.launch_nonce
            ):
                out.pop()
                adopted_worktrees.discard(key)
                _INHERITED_RUNNING.pop(worktree, None)
    return out


def _reap_orphan_worktree(repo_dir: Path, branch: str) -> bool:
    """Reap a worktree left holding ``branch`` by a FAILED refine of a PREVIOUS
    conductor run. ``kept_worktrees`` (the reuse map) is in-memory, so a conductor
    restart loses track of such a worktree; a fresh ``git worktree add -B`` then
    trips git's branch guard and the issue wedges in a defer loop forever.

    GUARDED: single-flight only holds within one conductor process — a refine
    dispatched by a killed conductor keeps running (it has its own process
    group), so before removing anything we check for a live job via the pid
    marker / inherited snapshot rows. A live worktree is NEVER reaped; the
    caller defers the dispatch instead (the job's own exit frees the branch).
    Returns True if a worktree (or stale metadata) was cleaned."""
    cp = _git(repo_dir, "worktree", "list", "--porcelain")
    if cp.returncode != 0:
        return False
    path: Optional[str] = None
    cur: Optional[str] = None
    for line in cp.stdout.splitlines():
        if line.startswith("worktree "):
            cur = line[len("worktree "):].strip()
        elif line.strip() == f"branch refs/heads/{branch}":
            path = cur
            break
    if path is None:
        _git(repo_dir, "worktree", "prune")  # only stale metadata held the branch
        return True
    if not Path(path).name.startswith("wt-i"):
        print(
            f"  NOT reaping {path}: it is not a conductor-managed issue "
            "worktree",
            flush=True,
        )
        return False
    live_pid = _worktree_live_pid(path)
    if live_pid is not None:
        print(
            f"  NOT reaping {path}: a live job (pid {live_pid}) from a previous "
            "conductor is still using it — deferring this dispatch until it "
            "exits", flush=True,
        )
        return False
    unpublished = _unpublished_worktree_reason(repo_dir, Path(path), branch)
    if unpublished:
        print(
            f"  NOT reaping {path}: {unpublished}; preserving it as a "
            "recovery candidate",
            flush=True,
        )
        return False
    print(f"  reaping orphan worktree for {branch}: {path}", flush=True)
    _remove_worktree(repo_dir, Path(path))
    _git(repo_dir, "worktree", "prune")
    return True


def prune_worktrees(repo_dir: Path, runs_parent: Path) -> None:
    """Remove leftover ``wt-*`` worktrees under ``runs_parent`` (failed /
    interrupted jobs from prior runs keep theirs by design), then ``git
    worktree prune`` the stale metadata. Conservative: only git removes
    — a dir git refuses to remove is reported, never rm -rf'd."""
    removed = 0
    if runs_parent.is_dir():
        for child in sorted(runs_parent.iterdir()):
            if not (child.is_dir() and child.name.startswith("wt-")):
                continue
            # NEVER prune a worktree with a LIVE job — a --prune after a
            # pkill -9 (which _remove_worktree's own warning suggests) would
            # otherwise force-remove the directory of a still-running, fully
            # billed refine, the exact 2026-07-07 destruction the reap guard
            # was built to prevent. Same liveness source as _reap_orphan_worktree.
            live_pid = _worktree_live_pid(str(child))
            if live_pid is not None:
                print(
                    f"  --prune SKIP {child}: a live job (pid {live_pid}) is "
                    "still using it",
                    flush=True,
                )
                continue
            branch_cp = _git(child, "branch", "--show-current")
            branch = (
                (branch_cp.stdout or "").strip()
                if branch_cp.returncode == 0 else None
            )
            unpublished = _unpublished_worktree_reason(
                repo_dir, child, branch or None
            )
            if unpublished:
                print(
                    f"  --prune SKIP {child}: {unpublished}",
                    flush=True,
                )
                continue
            proc = _git(repo_dir, "worktree", "remove", "--force", str(child))
            if proc.returncode == 0:
                removed += 1
            else:
                print(
                    f"  warning: --prune could not remove {child} "
                    f"({(proc.stderr or proc.stdout).strip()}); remove it manually"
                )
    _git(repo_dir, "worktree", "prune")
    print(f"--- pruned {removed} leftover worktree(s) under {runs_parent} ---")


# --- Dispatch / reap ----------------------------------------------------------------


# How long to let `lake exe cache get` run when priming a worktree's Mathlib
# oleans. A cold worktree's first run includes a dependency CLONE + cache-exe
# build before the olean decompress, so this is generous. Operators with slower
# cold checkouts can tune it without a redeploy via the env var below.
CACHE_GET_TIMEOUT_SECONDS = 1200
CACHE_GET_TIMEOUT_ENV_VAR = "MARATHON_CACHE_GET_TIMEOUT_SECONDS"

# Mathlib's top-level olean — a PRESENCE PROXY for "this checkout has Mathlib
# built", used to skip a redundant `cache get` decompress. It is a presence
# stat, NOT a completeness proof: a build/decompress interrupted after this one
# file landed would read as "built". That is acceptable — `lake build`
# incrementally recompiles any missing oleans, so the only cost of a false
# "built" is a slightly slower build, never a wrong result.
_MATHLIB_BUILT_MARKER = Path(
    ".lake/packages/mathlib/.lake/build/lib/lean/Mathlib.olean"
)
_LAKE_PACKAGES_RELPATH = Path(".lake/packages")
_INTERRUPTED_PACKAGES_RELPATH = Path(
    ".lake/marathon-interrupted-packages"
)
_CACHE_PRIME_UNSAFE = object()
_PROJECT_BUILD_RELPATH = Path(".lake/build")
_PROJECT_BUILD_SEED_PREFIX = ".marathon-project-build-seed-"
_PROJECT_BUILD_SEED_MIN_FREE_BYTES = 5 * 1024**3


def _project_build_tree_snapshot(
    build_dir: Path,
) -> Optional[tuple[tuple[str, int, int, int, int], ...]]:
    """Metadata snapshot used to reject a concurrently changing build tree."""
    entries: list[tuple[str, int, int, int, int]] = []
    for path in sorted(
        (build_dir, *build_dir.rglob("*")),
        key=lambda item: item.as_posix(),
    ):
        info = path.lstat()
        if not (stat.S_ISDIR(info.st_mode) or stat.S_ISREG(info.st_mode)):
            return None
        entries.append(
            (
                path.relative_to(build_dir).as_posix(),
                info.st_mode,
                info.st_size,
                info.st_mtime_ns,
                info.st_ino,
            )
        )
    return tuple(entries)


def _project_build_manifest(
    snapshot: tuple[tuple[str, int, int, int, int], ...],
) -> tuple[tuple[str, int, int, int], ...]:
    """Comparable path/type/size/mtime view; copied files have new inodes."""
    return tuple(
        (name, mode, size, mtime)
        for name, mode, size, mtime, _inode in snapshot
    )


def _project_seed_source_dirty(source_repo: Path) -> bool:
    """Fail closed when operator edits could make the live cache ambiguous."""
    if not (source_repo / ".git").exists():
        return False
    try:
        status_result = subprocess.run(
            [
                "git",
                "status",
                "--porcelain=v1",
                "--untracked-files=all",
                "--",
                "*.lean",
                "lakefile.lean",
                "lakefile.toml",
                "lake-manifest.json",
                "lean-toolchain",
            ],
            cwd=str(source_repo),
            capture_output=True,
            text=True,
            check=False,
            timeout=20,
        )
    except (OSError, subprocess.TimeoutExpired):
        return True
    return status_result.returncode != 0 or bool(
        (status_result.stdout or "").strip()
    )


def _remove_owned_seed_path(path: Path) -> None:
    """Best-effort cleanup restricted to one exclusively owned seed path."""
    try:
        if path.is_symlink():
            path.unlink()
        elif path.is_dir():
            shutil.rmtree(path)
        elif path.exists():
            path.unlink()
    except OSError:
        pass


def _copy_project_build_tree(source: Path, staged: Path) -> None:
    """Copy one build tree, preferring copy-on-write where the host supports it.

    macOS ``cp -c`` uses ``clonefile(2)``: the destination has independent file
    identity while unchanged blocks remain copy-on-write.  This is unlike a
    hardlink and therefore cannot let a job rewrite the operator's artifacts.
    A non-APFS filesystem (or another platform) falls back to ordinary private
    copies.
    """
    if sys.platform == "darwin":
        try:
            cloned = subprocess.run(
                ["/bin/cp", "-cR", "-p", str(source), str(staged)],
                capture_output=True,
                text=True,
                check=False,
                timeout=120,
            )
        except (OSError, subprocess.TimeoutExpired):
            cloned = None
        if cloned is not None and cloned.returncode == 0:
            return
        _remove_owned_seed_path(staged)
    shutil.copytree(
        source,
        staged,
        copy_function=shutil.copy2,
        symlinks=True,
    )


def _seed_project_build_outputs(
    source_repo: Path, worktree: Path
) -> bool:
    """Copy the live checkout's generated project outputs into a fresh job.

    ``git worktree add`` intentionally does not carry ignored ``.lake``
    outputs.  Mathlib's cache restores dependency oleans, but without this
    local seed Lake still has to rebuild every unchanged project module before
    checking the PR's target.  A private copy of ``<repo>/.lake/build`` gives
    Lake those candidate outputs; their trace/input hashes remain authoritative,
    so Lake rebuilds anything whose source or dependency changed on the PR.

    Correctness and isolation take priority over copy speed: artifacts are
    copied byte-for-byte rather than hard-linked, because Lake may replace or
    rewrite outputs in the job and must never mutate the operator's checkout.
    The copy is staged under the job's own ``.lake`` and renamed into place
    only after it completes.  Existing destination state and symlinked Lake
    roots are never touched.  Missing/unreadable caches and copy failures are
    ordinary fail-soft misses; the subsequent Lake build remains sufficient.
    """
    source_lake = source_repo / ".lake"
    source_build = source_repo / _PROJECT_BUILD_RELPATH
    destination_lake = worktree / ".lake"
    destination_build = worktree / _PROJECT_BUILD_RELPATH
    container: Optional[Path] = None
    staged: Optional[Path] = None

    try:
        # This helper is for a distinct, Marathon-owned job worktree.  Refuse
        # aliases and symlinked ownership boundaries rather than risking a
        # write into the operator's live checkout or a shared Lake directory.
        if source_repo.resolve() == worktree.resolve():
            return False
        if (
            source_lake.is_symlink()
            or source_build.is_symlink()
            or not source_build.is_dir()
            or _project_seed_source_dirty(source_repo)
            or worktree.is_symlink()
            or not worktree.is_dir()
            or destination_lake.is_symlink()
            or destination_build.is_symlink()
            or destination_build.exists()
        ):
            return False
        # A preserved link could point back into the live checkout, where a
        # later Lake write through it would violate the isolation guarantee.
        # The snapshot also lets us reject a source build that changes during
        # the copy, avoiding a mismatched olean/trace pair.
        source_snapshot = _project_build_tree_snapshot(source_build)
        if source_snapshot is None:
            return False
        source_bytes = sum(
            size
            for _name, mode, size, _mtime, _inode in source_snapshot
            if stat.S_ISREG(mode)
        )
        free_bytes = shutil.disk_usage(worktree).free
        if free_bytes - source_bytes < _PROJECT_BUILD_SEED_MIN_FREE_BYTES:
            print(
                "    project build seed skipped (insufficient disk reserve): "
                f"{worktree}",
                flush=True,
            )
            return False

        destination_lake.mkdir(parents=True, exist_ok=True)
        # Re-check after mkdir in case a pre-existing non-directory or a
        # concurrent owner made the destination unusable.
        if destination_lake.is_symlink() or not destination_lake.is_dir():
            return False

        for _ in range(16):
            candidate = (
                destination_lake
                / f"{_PROJECT_BUILD_SEED_PREFIX}{secrets.token_hex(8)}"
            )
            try:
                candidate.mkdir()
            except FileExistsError:
                continue
            else:
                container = candidate
                staged = candidate / "build"
                break
        if staged is None:
            return False

        _copy_project_build_tree(source_build, staged)
        staged_snapshot = _project_build_tree_snapshot(staged)
        if (
            staged_snapshot is None
            or _project_build_manifest(staged_snapshot)
            != _project_build_manifest(source_snapshot)
            or _project_build_tree_snapshot(source_build) != source_snapshot
        ):
            _remove_owned_seed_path(container)
            print(
                "    project build seed skipped (copy was not a stable, "
                f"link-free snapshot): {worktree}",
                flush=True,
            )
            return False
        if destination_build.exists() or destination_build.is_symlink():
            _remove_owned_seed_path(container)
            return False
        staged.rename(destination_build)
        container.rmdir()
    except Exception as e:  # noqa: BLE001 — a seed miss must never block a job
        if container is not None:
            _remove_owned_seed_path(container)
        print(
            "    project build seed skipped "
            f"({type(e).__name__}: {e}): {worktree}",
            flush=True,
        )
        return False

    print(
        f"    seeded project build outputs from {source_build}: {worktree}",
        flush=True,
    )
    return True


def _mathlib_already_built(worktree: Path) -> bool:
    """True iff ``worktree`` holds Mathlib's top-level olean (:data:`_MATHLIB_BUILT_MARKER`)
    — a cheap presence proxy that a prior build/prime populated Mathlib, so a
    fresh ``cache get`` would be a redundant decompress. Mathlib-specific (the
    only supported target); on a non-Mathlib project it is always False, which
    just means the best-effort, swallowed-on-failure prime runs anyway. Cheap
    stat; False on any error."""
    try:
        return (worktree / _MATHLIB_BUILT_MARKER).is_file()
    except OSError:
        return False


def _cache_get_timeout_seconds() -> float:
    """Return the cache-prime timeout, accepting a positive env override."""
    raw = os.environ.get(CACHE_GET_TIMEOUT_ENV_VAR)
    if raw is None:
        return float(CACHE_GET_TIMEOUT_SECONDS)
    try:
        value = float(raw)
    except ValueError:
        return float(CACHE_GET_TIMEOUT_SECONDS)
    return (
        value
        if 0 < value < float("inf")
        else float(CACHE_GET_TIMEOUT_SECONDS)
    )


def _quarantine_interrupted_lake_dependency(
    worktree: Path, dependency: Path
) -> Optional[Path]:
    """Atomically move one generated dependency out of Lake's active path.

    The move stays under the worktree's ``.lake`` directory, hence on the same
    filesystem, and uses an exclusively-created container so it never
    overwrites an earlier quarantine.  Return the preserved dependency path, or
    ``None`` without mutating the active path when quarantine cannot be made.
    """
    quarantine_root = worktree / _INTERRUPTED_PACKAGES_RELPATH
    try:
        if quarantine_root.is_symlink():
            return None
        quarantine_root.mkdir(parents=True, exist_ok=True)
        for _ in range(16):
            container = (
                quarantine_root
                / f"{dependency.name}-{secrets.token_hex(8)}"
            )
            try:
                container.mkdir()
            except FileExistsError:
                continue
            target = container / dependency.name
            try:
                dependency.rename(target)
            except OSError:
                try:
                    container.rmdir()
                except OSError:
                    pass
                return None
            return target
    except OSError:
        return None
    return None


def _quarantine_interrupted_lake_dependencies(worktree: Path) -> bool:
    """Quarantine generated dependency checkouts left mid-Git operation.

    Killing ``lake exe cache get`` while Lake is cloning a dependency can leave
    e.g. ``.lake/packages/mathlib/.git/index.lock`` plus a partially populated
    working tree.  Merely removing the lock is unsafe: the following fallback
    build can then consume an incomplete checkout.  Atomically move the whole
    generated dependency out of Lake's active package path so Lake can
    materialize it cleanly while every byte remains available for diagnosis.

    Safety is deliberately narrow: inspect only direct, non-symlink children of
    ``.lake/packages`` whose real ``.git`` directory contains a root-level Git
    lock.  Never move the project worktree, ``.lake/build`` output, symlinks, or
    an unlocked dependency (which may contain intentional local edits).

    Return ``False`` if a lock-marked dependency could not be quarantined.  The
    caller must then defer instead of allowing a fallback build to consume the
    known-poisoned checkout.  This function never raises.
    """
    lake_root = worktree / ".lake"
    packages = worktree / _LAKE_PACKAGES_RELPATH
    try:
        # A job-local .lake directory is part of the ownership boundary. If
        # it points elsewhere, even an ordinary-looking packages child may be
        # shared or user-owned, so moving anything below it is unsafe.
        if lake_root.is_symlink():
            print(
                "    unsafe: refusing to quarantine through symlinked Lake "
                f"root: {lake_root}",
                flush=True,
            )
            return False
        if packages.is_symlink() or not packages.is_dir():
            return True
        dependencies = list(packages.iterdir())
    except OSError as e:
        print(
            "    unsafe: could not inspect Lake dependency root "
            f"{packages} ({type(e).__name__}: {e})",
            flush=True,
        )
        return False

    for dependency in dependencies:
        try:
            if dependency.is_symlink() or not dependency.is_dir():
                continue
            git_dir = dependency / ".git"
            if git_dir.is_symlink() or not git_dir.is_dir():
                continue
            interrupted = any(
                child.name.endswith(".lock")
                and not child.is_symlink()
                and child.is_file()
                for child in git_dir.iterdir()
            )
            if not interrupted:
                continue
            quarantined = _quarantine_interrupted_lake_dependency(
                worktree, dependency
            )
            if quarantined is None:
                print(
                    "    unsafe: could not quarantine interrupted Lake "
                    f"dependency checkout: {dependency}",
                    flush=True,
                )
                return False
            print(
                "    quarantined interrupted Lake dependency checkout: "
                f"{dependency} -> {quarantined}",
                flush=True,
            )
        except OSError as e:
            print(
                "    unsafe: could not inspect interrupted Lake dependency "
                f"{dependency} ({type(e).__name__}: {e})",
                flush=True,
            )
            return False
    return True


def _kill_and_reap_cache_get(proc: subprocess.Popen) -> bool:
    """Stop the cache-get subtree and confirm its parent has exited."""
    try:
        os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
    except OSError:
        pass
    try:
        proc.communicate(timeout=15)
    except Exception:  # noqa: BLE001 — reaping must not raise
        try:
            return proc.poll() is not None
        except Exception:  # noqa: BLE001
            return False
    try:
        return proc.poll() is not None
    except Exception:  # noqa: BLE001
        return proc.returncode is not None


def _prime_mathlib_cache(
    worktree: Path,
    *,
    timeout: Optional[float] = None,
):
    """Best-effort: seed ``worktree``'s Mathlib oleans via ``lake exe cache get``
    so the refine's ``lake build`` SKIPS the multi-HOUR Mathlib source compile.

    A fresh ``git worktree add`` has NO ``.lake`` at all (it is gitignored), so
    Mathlib's source/oleans are absent and would otherwise be recompiled from
    scratch. ``lake exe cache get`` clones the pinned deps, builds the small
    ``cache`` exe, and DECOMPRESSES Mathlib's prebuilt oleans from the shared
    ``~/.cache/mathlib`` store — minutes, not hours.

    The store is resolved from the inherited HOME/XDG env (NOT cwd — cwd only
    controls where ``.lake`` is written), so we run with the environment
    inherited; with a warm store the decompress is an OFFLINE read into the
    worktree's OWN ``.lake``, so concurrent worktrees don't race.

    This primitive never moves existing checkout state, so it is safe for the
    operator's live checkout. Marathon-owned job worktrees use
    :func:`_prime_job_mathlib_cache`, which wraps this command with guarded
    pre/post quarantine.

    FAIL-SAFE: returns True only on a clean cache-get; False on fallback
    (skip flag set, no ``lake`` on PATH or in ``~/.elan/bin``, non-zero exit,
    timeout, any exception), and ``_CACHE_PRIME_UNSAFE`` if a failed child
    cannot be confirmed stopped. It NEVER raises. On a timeout the ENTIRE
    cache-get subtree (lake + its lean/leanc/curl workers) is killed via its
    process group, so no orphaned compiler/downloader is left racing the
    refine's build in the same worktree. No-op when
    ``MARATHON_SKIP_CACHE_GET`` is set.  The timeout defaults to
    :data:`CACHE_GET_TIMEOUT_SECONDS` and can be overridden with
    ``MARATHON_CACHE_GET_TIMEOUT_SECONDS``."""

    if os.environ.get("MARATHON_SKIP_CACHE_GET"):
        return False
    lake = find_lake_executable()
    if lake is None:
        return False
    effective_timeout = (
        _cache_get_timeout_seconds() if timeout is None else timeout
    )
    try:
        # Own process group (start_new_session) so a timeout can kill the WHOLE
        # subtree, not just the lake parent — subprocess.run's timeout would
        # SIGKILL only the direct child and orphan its lean/curl workers.
        proc = subprocess.Popen(
            [lake, "exe", "cache", "get"],
            cwd=str(worktree),
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            start_new_session=True,
        )
    except Exception as e:  # noqa: BLE001 — a prime must NEVER abort a dispatch
        print(
            f"    cache get skipped ({type(e).__name__}); will build Mathlib "
            f"normally: {worktree}",
            flush=True,
        )
        return False
    try:
        _out, err = proc.communicate(timeout=effective_timeout)
    except subprocess.TimeoutExpired:
        if not _kill_and_reap_cache_get(proc):
            print(
                "    cache get timed out and its process could not be "
                f"confirmed stopped; refusing worktree fallback: {worktree}",
                flush=True,
            )
            return _CACHE_PRIME_UNSAFE
        print(
            f"    cache get timed out after {effective_timeout:g}s "
            "(subtree killed); "
            f"will build Mathlib normally: {worktree}",
            flush=True,
        )
        return False
    except Exception as e:  # noqa: BLE001 — a prime must NEVER abort a dispatch
        # communicate can fail while the child remains live. Never move a
        # dependency checkout out from under that process: stop and reap the
        # whole subtree first, exactly as on a timeout.
        if not _kill_and_reap_cache_get(proc):
            print(
                "    cache get pipe failed and its process could not be "
                f"confirmed stopped; refusing worktree fallback: {worktree}",
                flush=True,
            )
            return _CACHE_PRIME_UNSAFE
        print(
            f"    cache get errored ({type(e).__name__}); will build Mathlib "
            f"normally: {worktree}",
            flush=True,
        )
        return False
    if proc.returncode != 0:
        tail = ((err or _out) or "").strip().splitlines()
        why = tail[-1][:140] if tail else f"exit {proc.returncode}"
        print(
            f"    cache get exit {proc.returncode} ({why}); will build Mathlib "
            f"normally: {worktree}",
            flush=True,
        )
        return False
    print(f"    primed Mathlib oleans (lake exe cache get): {worktree}", flush=True)
    return True


def _prime_job_mathlib_cache(
    worktree: Path, *, timeout: Optional[float] = None
):
    """Prime a Marathon-owned worktree without exposing a poisoned fallback.

    Quarantine before the command repairs state retained from an older killed
    prime. Quarantine afterward catches a lock left by this command. The
    command itself always kills/reaps its subtree before returning on timeout
    or pipe failure, so the post-pass never moves files under a live child.

    Return ``_CACHE_PRIME_UNSAFE`` when quarantine fails; dispatch callers must
    defer. Otherwise preserve the primitive's True/False result.
    """
    if not _quarantine_interrupted_lake_dependencies(worktree):
        return _CACHE_PRIME_UNSAFE
    result = (
        _prime_mathlib_cache(worktree)
        if timeout is None
        else _prime_mathlib_cache(worktree, timeout=timeout)
    )
    if result is _CACHE_PRIME_UNSAFE:
        return _CACHE_PRIME_UNSAFE
    if not _quarantine_interrupted_lake_dependencies(worktree):
        return _CACHE_PRIME_UNSAFE
    return result


def _ensure_prover_branch(repo_dir: Path, prover_branch: str) -> None:
    """Ensure the remote prover branch exists, forked from origin/main.

    Best-effort + idempotent: if ``origin/<prover_branch>`` is already there,
    do nothing (we do NOT reset it — that would discard landed proofs). If it
    is absent, create it on the remote pointing at current origin/main so the
    first fill has a base to branch from. A failure here is logged, never
    raised — a missing branch just means fills defer until it exists."""
    try:
        _git(repo_dir, "fetch", "origin", "main")
        ls = _git(repo_dir, "ls-remote", "--heads", "origin", prover_branch)
        if ls.returncode == 0 and (ls.stdout or "").strip():
            return  # already exists — leave its accumulated proofs intact
        push = _git(
            repo_dir, "push", "origin",
            f"origin/main:refs/heads/{prover_branch}",
        )
        if push.returncode == 0:
            print(f"  fill (proving): created prover branch '{prover_branch}' "
                  "from origin/main", flush=True)
        else:
            print(f"  fill (proving): WARN could not create '{prover_branch}': "
                  f"{((push.stderr or push.stdout) or '').strip()[:200]}",
                  flush=True)
    except Exception as e:  # noqa: BLE001 — never block startup on branch setup
        print(f"  fill (proving): WARN prover-branch ensure skipped — {e}",
              flush=True)


def _branch_pull_requests(
    cfg: ReviewConfig, branch: str, *, state: str,
) -> Optional[list[dict]]:
    """REST-backed PR rows for ``branch``, or ``None`` when GitHub is unknown.

    The conductor must not depend on GraphQL quota for dispatch safety or
    completion accounting.  ``gh pr list`` is GraphQL-backed; ``gh api`` here
    targets the pull-request REST collection directly.
    """
    from marathon.review.github import gh

    owner = cfg.github_repo.split("/", 1)[0]
    query = urlencode(
        {"head": f"{owner}:{branch}", "state": state, "per_page": 100}
    )
    try:
        cp = gh(
            "api", f"repos/{cfg.github_repo}/pulls?{query}", check=False
        )
        if cp.returncode != 0:
            return None
        rows = json.loads(cp.stdout or "[]")
        return rows if isinstance(rows, list) else None
    except Exception:  # noqa: BLE001 — unknown must fail closed at callers
        return None


def _open_refine_prs_for_issue(
    cfg: ReviewConfig, issue_num: int
) -> Optional[list[dict]]:
    """All open refine PRs for an issue, or ``None`` when not provable.

    A no-PR start-over confirmation is an expected-absence snapshot. Recheck
    that absence at dispatch through paginated REST before choosing any branch;
    otherwise a PR created after the dialog could be overwritten without ever
    being shown or archived.
    """
    from marathon.review.automerge import _REFINE_BRANCH_RE
    from marathon.review.github import gh

    matches: list[dict] = []
    for page in range(1, 11):
        query = urlencode(
            {"state": "open", "per_page": 100, "page": page}
        )
        try:
            cp = gh(
                "api",
                f"repos/{cfg.github_repo}/pulls?{query}",
                check=False,
            )
            if cp.returncode != 0:
                return None
            rows = json.loads(cp.stdout or "[]")
        except Exception:  # noqa: BLE001 — absence must be affirmative
            return None
        if not isinstance(rows, list):
            return None
        for row in rows:
            head = row.get("head") if isinstance(row, dict) else None
            branch = head.get("ref") if isinstance(head, dict) else None
            match = _REFINE_BRANCH_RE.match(branch or "")
            if match and int(match.group(1)) == issue_num:
                matches.append(row)
        if len(rows) < 100:
            return matches
    # Exactly 1000 rows may have a further page. Never infer absence from a
    # bounded/truncated listing.
    return None


def _has_open_pr_for_issue(
    cfg: ReviewConfig, issue_num: int, chapter: int, *, kind: str = "refine"
) -> Optional[bool]:
    """Whether the issue's persistent branch has an open PR.

    ``None`` means GitHub could not answer. Dispatch callers treat that as
    occupied (fail closed), avoiding a duplicate billed run during an API or
    quota outage.
    """
    branch = _branch_name_for_issue(f"Chapter{chapter}", issue_num, kind=kind)
    rows = _branch_pull_requests(cfg, branch, state="open")
    return None if rows is None else bool(rows)


#: Sentinel: `_pick_fill_dispatchable(..., _snapshot=_UNSET)` loads the real
#: audit snapshot; a test injects a fake (or ``None`` to force issue-order).
_UNSET = object()


def _load_audit_snapshot(cfg: ReviewConfig):
    """The latest audit snapshot, or ``None`` if absent/unreadable. Never raises
    — the fill ordering degrades to issue-number order without it."""
    try:
        from marathon.audit.engine import load_snapshot

        return load_snapshot(cfg.repo_dir)
    except Exception:  # noqa: BLE001
        return None


def _pick_fill_dispatchable(
    cfg: ReviewConfig,
    fill_chapters: set[int],
    running_issues: set[int],
    not_before: dict[int, float],
    fill_attempts: dict[int, int],
    max_fill_attempts: int,
    *,
    _now_fn=_now,
    _discover=None,
    _open_pr=None,
    _skeleton_done=None,
    _snapshot=_UNSET,
) -> list[tuple[int, int]]:
    """``[(issue, chapter), …]`` fills to dispatch this tick: verified-but-``sorry``
    issues in ``fill_chapters``, excluding those already running, backed off,
    attempt-capped, or with an open PR (a fill already pending merge). Discovery
    is naturally idempotent — once a fill's PR merges the decl is no longer
    ``sorry`` and drops out — so the only loop risk is a fill that lands nothing,
    which ``max_fill_attempts`` bounds.

    A chapter is skipped entirely until its skeleton is done (every registered
    issue verified) — proving while statements still churn wastes proofs on
    signatures that may still change under re-review.

    ORDER: the dispatchable targets across ALL chapters are ordered LEAVES-FIRST
    over the cross-chapter dependency DAG (:func:`marathon.prover.dag.order_targets`)
    — foundational API lemmas before the theorems that rest on them, up to the
    root (eventually Stokes). The order is recomputed every tick against the
    current snapshot, so as leaves prove clean the next layer surfaces. With no
    audit snapshot the order falls back to chapter-then-issue-number, so the
    queue can never wedge on a dependency the snapshot cannot see."""
    if not fill_chapters:
        return []
    from marathon.prover.dag import order_targets
    from marathon.review.fill_discovery import (
        chapter_skeleton_done,
        discover_fill_targets,
    )

    discover = _discover or discover_fill_targets
    skeleton_done = _skeleton_done or chapter_skeleton_done
    open_pr = _open_pr or _has_open_pr_for_issue
    now = _now_fn()
    dispatchable: list = []  # FillTargets, gathered across chapters
    for chapter in sorted(fill_chapters):
        if not skeleton_done(cfg, chapter):
            continue
        for t in discover(cfg, chapter):
            n = t.issue
            if n in running_issues:
                continue
            if n in not_before and not_before[n] > now:
                continue
            if fill_attempts.get(n, 0) >= max_fill_attempts:
                continue
            # Unknown is occupied: never bill a duplicate proof merely because
            # GitHub's dispatch-safety check is temporarily unavailable.
            if open_pr(cfg, n, chapter, kind="proof") is not False:
                continue
            dispatchable.append(t)
    snapshot = _load_audit_snapshot(cfg) if _snapshot is _UNSET else _snapshot
    ordered = order_targets(dispatchable, snapshot)
    return [(t.issue, t.chapter) for t in ordered]


def _refine_branch_for_dispatch(
    cfg: ReviewConfig,
    issue_num: int,
    chapter: int,
    verdict_ts: Optional[str],
) -> Optional[str]:
    """Select an owned publication branch without overwriting stale refs.

    The normal branch remains persistent per issue so a later session updates
    its existing open PR.  If that remote ref exists but has *no* open PR, it is
    stale/unowned for the active review loop; use a deterministic verdict-
    generation branch and leave the old ref untouched.  Any inability to
    establish remote/PR state fails closed before Aristotle is dispatched.
    """
    legacy = _branch_name_for_issue(f"Chapter{chapter}", issue_num, kind="refine")
    ref = f"refs/heads/{legacy}"
    remote = _git(cfg.repo_dir, "ls-remote", "--heads", "origin", ref)
    if remote.returncode != 0:
        print(
            f"--- deferring #{issue_num}: could not establish ownership of "
            f"remote branch {legacy} ---",
            flush=True,
        )
        return None
    if not (remote.stdout or "").strip():
        return legacy
    open_rows = _branch_pull_requests(cfg, legacy, state="open")
    if open_rows is None:
        print(
            f"--- deferring #{issue_num}: GitHub could not confirm whether "
            f"{legacy} has an open PR ---",
            flush=True,
        )
        return None
    if open_rows:
        return legacy
    if not verdict_ts:
        print(
            f"--- deferring #{issue_num}: stale remote branch {legacy} exists "
            "but this dispatch has no verdict generation for a safe suffix ---",
            flush=True,
        )
        return None
    branch = _branch_name_for_issue(
        f"Chapter{chapter}", issue_num, kind="refine", generation=verdict_ts
    )
    print(
        f"--- #{issue_num}: remote {legacy} has no open PR; preserving it and "
        f"publishing this verdict on {branch} ---",
        flush=True,
    )
    return branch


def _fetch_exact_recovery_head(
    repo_dir: Path, branch: str, expected_head: str
) -> bool:
    """Fetch and verify the exact PR head selected in the recovery dialog."""
    from marathon.post_pipeline import observe_remote_branch

    ok, remote_head, message = observe_remote_branch(repo_dir, branch)
    if not ok or not remote_head or remote_head != expected_head:
        found = remote_head[:12] if remote_head else "<absent>"
        print(
            f"--- recovery deferred: {branch} changed ({found} != expected "
            f"{expected_head[:12]}); {message} ---",
            flush=True,
        )
        return False
    fetch = _git(repo_dir, "fetch", "origin", branch)
    if fetch.returncode != 0:
        print(
            f"--- recovery deferred: fetch origin {branch} failed: "
            f"{(fetch.stderr or fetch.stdout).strip()[:240]} ---",
            flush=True,
        )
        return False
    fetched = _git(repo_dir, "rev-parse", f"origin/{branch}")
    if (
        fetched.returncode != 0
        or (fetched.stdout or "").strip() != expected_head
    ):
        print(
            f"--- recovery deferred: fetched origin/{branch} did not resolve "
            f"to {expected_head[:12]} ---",
            flush=True,
        )
        return False
    return True


def _local_branch_head(repo_dir: Path, branch: str) -> Optional[str]:
    cp = _git(repo_dir, "rev-parse", "--verify", "--quiet", f"refs/heads/{branch}")
    if cp.returncode != 0:
        return None
    value = (cp.stdout or "").strip()
    return value or None


def _local_branch_extends(
    repo_dir: Path, branch: str, expected_remote_head: str
) -> bool:
    local_head = _local_branch_head(repo_dir, branch)
    if local_head is None:
        return False
    return _git(
        repo_dir,
        "merge-base",
        "--is-ancestor",
        expected_remote_head,
        local_head,
    ).returncode == 0


def _preserve_local_branch_ref(
    repo_dir: Path,
    issue_num: int,
    branch: str,
    expected_remote_head: str,
    recovery_id: str,
) -> bool:
    """Archive an unpublished local branch tip before ``git -B`` can move it."""
    local_head = _local_branch_head(repo_dir, branch)
    if not local_head or local_head == expected_remote_head:
        return True
    archive = (
        f"marathon/local-wip-i{issue_num}-{local_head[:12]}-"
        f"{recovery_id[:8]}"
    )
    existing = _local_branch_head(repo_dir, archive)
    if existing == local_head:
        return True
    if existing is not None:
        print(
            f"--- recovery deferred: local archive {archive} points at "
            "different work ---",
            flush=True,
        )
        return False
    made = _git(repo_dir, "branch", archive, local_head)
    if made.returncode != 0:
        print(
            f"--- recovery deferred: could not preserve local {branch}@"
            f"{local_head[:12]}: "
            f"{(made.stderr or made.stdout).strip()[:240]} ---",
            flush=True,
        )
        return False
    print(
        f"--- recovery: preserved unpublished local ref as {archive}@"
        f"{local_head[:12]} ---",
        flush=True,
    )
    return True


def _start_over_local_archive_branch(
    issue_num: int, branch: str, recovery_id: str
) -> str:
    """Deterministic, ref-safe archive name for one recovery/branch pair."""
    operation = hashlib.sha256(recovery_id.encode("utf-8")).hexdigest()[:12]
    source = hashlib.sha256(branch.encode("utf-8")).hexdigest()[:12]
    return f"marathon/local-archive-i{issue_num}-o{operation}-b{source}"


def _preserve_no_pr_start_over_local_branch(
    repo_dir: Path,
    issue_num: int,
    branch: str,
    recovery_id: str,
) -> bool:
    """Preserve a local no-PR candidate before a fresh start-over branch.

    A dead registered worktree is renamed onto an operation-scoped archive
    branch.  ``git branch -m`` changes only the ref: tracked, staged, and
    untracked files remain exactly where they are.  A branch not checked out in
    a worktree is copied to the same archive instead, so a later ``-B`` can
    never discard its unpublished commit tip.  A live owner always wins and
    makes recovery defer.
    """
    archive = _start_over_local_archive_branch(
        issue_num, branch, recovery_id
    )
    held = _registered_worktree_for_branch(repo_dir, branch)
    if held is not None:
        live_pid = _worktree_live_pid(str(held))
        if live_pid is not None:
            print(
                f"--- start-over deferred: live pid {live_pid} still owns "
                f"{held}; local work was left untouched ---",
                flush=True,
            )
            return False
        rename = _git(held, "branch", "-m", archive)
        if rename.returncode != 0:
            # Accept only the exact idempotent post-rename state.  In normal
            # operation the old branch no longer has a registered holder after
            # a successful rename, but this also makes restart/stale-metadata
            # handling fail safe.
            current = _git(held, "branch", "--show-current")
            if (current.stdout or "").strip() != archive:
                print(
                    f"--- start-over deferred: could not preserve local "
                    f"worktree {held} on {archive}: "
                    f"{(rename.stderr or rename.stdout).strip()[:240]} ---",
                    flush=True,
                )
                return False
        print(
            f"--- start-over: preserved local worktree {held} on "
            f"{archive} ---",
            flush=True,
        )
        return True

    local_head = _local_branch_head(repo_dir, branch)
    if local_head is None:
        return True
    archived_head = _local_branch_head(repo_dir, archive)
    if archived_head == local_head:
        return True
    if archived_head is not None:
        print(
            f"--- start-over deferred: local archive {archive} points at "
            "different work ---",
            flush=True,
        )
        return False
    made = _git(repo_dir, "branch", archive, local_head)
    if made.returncode != 0:
        print(
            f"--- start-over deferred: could not preserve local {branch}@"
            f"{local_head[:12]}: "
            f"{(made.stderr or made.stdout).strip()[:240]} ---",
            flush=True,
        )
        return False
    print(
        f"--- start-over: preserved local {branch}@{local_head[:12]} as "
        f"{archive} ---",
        flush=True,
    )
    return True


def _select_no_pr_start_over_branch(
    cfg: ReviewConfig,
    issue_num: int,
    chapter: int,
    verdict_ts: Optional[str],
    recovery_id: str,
    runs_parent: Path,
    kept_worktrees: dict[int, str],
) -> Optional[tuple[str, Optional[tuple[Path, Path]]]]:
    """Choose an affirmatively absent branch without overwriting old work.

    The legacy branch and the verdict-generation branch may contain a push
    whose PR creation failed.  They are deliberately left on the remote.  Any
    local version (including a dirty dead worktree) is archived first.  The new
    publication branch is derived from the recovery operation.  If that exact
    operation branch is occupied, only its exact workdir publication WAL may
    resume it; otherwise recovery defers instead of paying for a new session
    on a slot suffix and abandoning the already-pushed commit.
    """
    from marathon.post_pipeline import observe_remote_branch

    legacy = _branch_name_for_issue(
        f"Chapter{chapter}", issue_num, kind="refine"
    )
    stale_candidates = [legacy]
    if verdict_ts:
        stale_candidates.append(
            _branch_name_for_issue(
                f"Chapter{chapter}",
                issue_num,
                kind="refine",
                generation=verdict_ts,
            )
        )
    for candidate in dict.fromkeys(stale_candidates):
        known, remote_head, message = observe_remote_branch(
            cfg.repo_dir, candidate
        )
        if not known:
            print(f"--- start-over deferred: {message} ---", flush=True)
            return None
        if not _preserve_no_pr_start_over_local_branch(
            cfg.repo_dir,
            issue_num,
            candidate,
            recovery_id,
        ):
            return None
        if remote_head is not None:
            print(
                f"--- start-over: preserving stale remote {candidate}@"
                f"{remote_head[:12]} unchanged ---",
                flush=True,
            )

    branch = _branch_name_for_issue(
        f"Chapter{chapter}",
        issue_num,
        kind="refine",
        generation=f"start-over:{recovery_id}:0",
    )
    resume = _start_over_recovery_resume_checkout(
        cfg.repo_dir,
        runs_parent,
        issue_num,
        branch,
        kept_worktrees,
        expected_operation_id=recovery_id,
        expected_repo=cfg.github_repo,
        target_relpath=_target_relpath(cfg, chapter),
        expected_pr=None,
        expected_head=None,
        expected_base=None,
    )
    if resume is not None:
        print(
            f"--- start-over: resuming operation {recovery_id[:8]} for "
            f"#{issue_num} in {resume[0]} ---",
            flush=True,
        )
        return branch, resume
    known, remote_head, message = observe_remote_branch(cfg.repo_dir, branch)
    if not known:
        print(f"--- start-over deferred: {message} ---", flush=True)
        return None
    if not _preserve_no_pr_start_over_local_branch(
        cfg.repo_dir,
        issue_num,
        branch,
        recovery_id,
    ):
        return None
    if remote_head is None:
        return branch, None
    print(
        f"--- start-over deferred: this operation's branch {branch}@"
        f"{remote_head[:12]} is already occupied but no exact publication "
        "checkpoint could be resumed; preserving it and refusing to start "
        "another Aristotle session ---",
        flush=True,
    )
    _park_deferred_recovery(cfg, issue_num)
    return None


def _archive_start_over_head(
    cfg: ReviewConfig,
    issue_num: int,
    branch: str,
    expected_head: str,
    recovery_id: str,
) -> bool:
    """Durably archive the remote PR head and free any dead local checkout.

    The archive is created before a fresh Aristotle session can start. A dirty
    local worktree is *renamed* onto that archive branch without touching its
    files, so unpublished progress remains recoverable on disk as well.
    """
    if not _fetch_exact_recovery_head(cfg.repo_dir, branch, expected_head):
        return False
    archive = (
        f"marathon/archive-i{issue_num}-{expected_head[:12]}-"
        f"{recovery_id[:8]}"
    )
    from marathon.post_pipeline import observe_remote_branch

    known, archived_head, message = observe_remote_branch(cfg.repo_dir, archive)
    if not known:
        print(f"--- start-over deferred: {message} ---", flush=True)
        return False
    if archived_head not in (None, expected_head):
        print(
            f"--- start-over deferred: archive {archive} already points at "
            "different work ---",
            flush=True,
        )
        return False
    if archived_head is None:
        ref = f"refs/heads/{archive}"
        push = _git(
            cfg.repo_dir,
            "push",
            f"--force-with-lease={ref}:",
            "origin",
            f"{expected_head}:{ref}",
        )
        if push.returncode != 0:
            print(
                f"--- start-over deferred: could not archive {branch}@"
                f"{expected_head[:12]}: "
                f"{(push.stderr or push.stdout).strip()[:240]} ---",
                flush=True,
            )
            return False
    held = _registered_worktree_for_branch(cfg.repo_dir, branch)
    if held is not None:
        live_pid = _worktree_live_pid(str(held))
        if live_pid is not None:
            print(
                f"--- start-over deferred: live pid {live_pid} still owns "
                f"{held}; archived remote head but left the job untouched ---",
                flush=True,
            )
            return False
        rename = _git(held, "branch", "-m", archive)
        if rename.returncode != 0:
            # A restart may have already completed the rename. Only accept that
            # exact safe state; otherwise keep both trees and defer.
            current = _git(held, "branch", "--show-current")
            if (current.stdout or "").strip() != archive:
                print(
                    f"--- start-over deferred: could not preserve local "
                    f"worktree on {archive}: "
                    f"{(rename.stderr or rename.stdout).strip()[:240]} ---",
                    flush=True,
                )
                return False
        print(
            f"--- start-over: preserved local worktree {held} on {archive} ---",
            flush=True,
        )
    elif not _preserve_local_branch_ref(
        cfg.repo_dir,
        issue_num,
        branch,
        expected_head,
        recovery_id,
    ):
        return False
    print(
        f"--- start-over: archived prior PR head as {archive}@"
        f"{expected_head[:12]} ---",
        flush=True,
    )
    return True


def _prepare_continue_checkpoint(
    worktree: Path,
    workdir: Path,
    target: Path,
    expected_branch: str,
    expected_verdict_ts: Optional[str],
) -> bool:
    """Make a kept tree run one more round without checkout/reset.

    Existing Aristotle IDs are retained and marked for ``project.ask``. If no
    checkpoint exists, the exact PR/local diff is still preserved and refine
    creates a fresh Aristotle project against that tree.
    """
    head = _git(worktree, "rev-parse", "HEAD")
    branch = _git(worktree, "branch", "--show-current")
    if head.returncode != 0 or branch.returncode != 0:
        return False
    head_sha = (head.stdout or "").strip()
    actual_branch = (branch.stdout or "").strip()
    if not head_sha or actual_branch != expected_branch:
        print(
            f"  continue: expected {expected_branch}, found "
            f"{actual_branch or '<detached>'}; refusing to reuse it",
            flush=True,
        )
        return False
    state_path = workdir / REFINE_STATE_FILENAME
    try:
        state = load_refine_state(state_path)
    except Exception as exc:  # noqa: BLE001
        print(f"  continue: unreadable checkpoint ({exc}); preserving files")
        state = None
    same_target = False
    if state is not None:
        try:
            same_target = Path(state.target_folder).resolve() == target.resolve()
        except OSError:
            same_target = False
    if state is None or not same_target:
        state = RefineState(target_folder=str(target))
    if state.pending_publication is None:
        state.iterations_completed = 0
        state.current_iteration_idx = 0
        state.force_continuation = bool(state.project_id)
        state.preserve_local_branch = True
        state.continuation_commit_sha = head_sha
        state.review_verdict_ts = expected_verdict_ts
    save_refine_state(state_path, state)
    return True


def _dispatch_job(
    cfg: ReviewConfig,
    issue_num: int,
    chapter: int,
    runs_parent: Path,
    kept_worktrees: dict[int, str],
    *,
    fill: bool = False,
    prover_branch: str = PROVER_BASE_BRANCH,
    repair: bool = False,
    repair_modules: Optional[list[str]] = None,
) -> Optional[ConductorJob]:
    """Create the job worktree (or reuse the one a failed attempt kept —
    it still holds the per-issue branch, so a fresh add would trip the
    branch guard) and spawn ``python -m marathon refine`` in it.

    Returns None to DEFER (never crash) when the worktree cannot be
    created — most importantly when git refuses because the per-issue
    branch is checked out in another worktree, which is the
    double-dispatch guard working as intended.

    A FILL job proves on the SEPARATE prover branch: it branches from
    ``origin/<prover_branch>`` under a ``proof-`` name and its PR targets
    the prover branch, so proofs never touch ``main``."""
    expected_verdict_ts: Optional[str] = None
    expected_recovery_id: Optional[str] = None
    recovery_mode: Optional[str] = None
    recovery_pr: Optional[int] = None
    recovery_branch: Optional[str] = None
    recovery_head_sha: Optional[str] = None
    recovery_base: Optional[str] = None
    rejection_note: Optional[str] = None
    auto_pr_expected_remote_oid: Optional[str] = None
    update_resume: Optional[tuple[Path, Path, str]] = None
    update_adoption: Optional[tuple[Path, Path, Optional[dict]]] = None
    start_over_resume: Optional[tuple[Path, Path]] = None
    publication_resume: Optional[tuple[Path, Path, str]] = None
    if not fill and not repair:
        current = load_state(cfg).issues.get(issue_num)
        # Missing is tolerated for low-level/manual callers and older tests;
        # the production scheduler only calls us for state-backed rejections.
        # A present non-rejected entry means a verify won after queue-pick — do
        # not spend an Aristotle session on a verdict that no longer exists.
        if current is not None and current.status != "rejected":
            print(
                f"--- deferring #{issue_num}: current verdict is "
                f"{current.status!r}, not rejected ---",
                flush=True,
            )
            return None
        if current is not None:
            expected_verdict_ts = current.verdict_ts
            expected_recovery_id = current.recovery_id
            recovery_mode = current.recovery_mode
            recovery_pr = current.recovery_pr
            recovery_branch = current.recovery_branch
            recovery_head_sha = current.recovery_head_sha
            recovery_base = current.recovery_base
            rejection_note = current.notes
            auto_pr_expected_remote_oid = recovery_head_sha
            if recovery_mode in {
                RECOVERY_RETRY_BUILD,
                RECOVERY_CONTINUE_PR,
                RECOVERY_UPDATE_CONTINUE,
            } and not (
                recovery_pr and recovery_branch and recovery_head_sha
            ):
                print(
                    f"--- deferring #{issue_num}: {recovery_mode} lacks an "
                    "exact PR/branch/head recovery snapshot ---",
                    flush=True,
                )
                return None
            if recovery_mode in {
                RECOVERY_RETRY_BUILD,
                RECOVERY_CONTINUE_PR,
                RECOVERY_UPDATE_CONTINUE,
            } and recovery_branch and recovery_head_sha:
                reconciled = _reconcile_recovery_head(
                    cfg,
                    issue_num,
                    recovery_branch,
                    recovery_head_sha,
                    expected_verdict_ts=expected_verdict_ts,
                    expected_recovery_id=expected_recovery_id,
                    kept_worktrees=kept_worktrees,
                )
                if reconciled is None:
                    return None
                if reconciled != recovery_head_sha:
                    recovery_head_sha = reconciled
                    auto_pr_expected_remote_oid = reconciled
            if (
                expected_recovery_id
                and recovery_mode in {
                    RECOVERY_CONTINUE_PR,
                    RECOVERY_UPDATE_CONTINUE,
                    RECOVERY_START_OVER,
                }
            ):
                publication_resume = _publication_recovery_resume_checkout(
                    cfg.repo_dir,
                    runs_parent,
                    issue_num,
                    kept_worktrees,
                    expected_operation_id=expected_recovery_id,
                    expected_repo=cfg.github_repo,
                    expected_branch=recovery_branch,
                    expected_base=recovery_base or "main",
                    expected_remote_oid=(
                        ""
                        if recovery_mode == RECOVERY_START_OVER
                        and recovery_pr is None
                        else recovery_head_sha or ""
                    ),
                    target_relpath=_target_relpath(cfg, chapter),
                )
                if (
                    publication_resume is not None
                    and recovery_mode == RECOVERY_START_OVER
                    and recovery_pr is None
                ):
                    auto_pr_expected_remote_oid = ""
    ts = datetime.now().strftime("%Y%m%d-%H%M%S")
    if repair:
        # Chapter-scoped repair branch (no review issue): marathon/repair-c<N>.
        # A retry CONTINUES the branch's remote head when it exists — a
        # [build:FAIL] repair round leaves its partial progress on the open
        # (automerge-refused) PR, and the next round must stack on it rather
        # than re-derive from origin/main (paying Aristotle twice for the
        # same fix). Fresh cycles (no remote branch — the last PR merged and
        # deleted it) fork from origin/main as before.
        branch = _branch_name_for_issue(f"Chapter{chapter}", None, kind="repair")
        start_point = WORKTREE_START_POINT
        _git(cfg.repo_dir, "fetch", "origin", branch)
        probe = _git(cfg.repo_dir, "rev-parse", "--verify", "--quiet",
                     f"origin/{branch}")
        if probe.returncode == 0:
            start_point = f"origin/{branch}"
            print(f"--- repair c{chapter}: continuing open branch "
                  f"origin/{branch} (stacking on prior partial fix) ---",
                  flush=True)
    elif fill:
        branch = _branch_name_for_issue(f"Chapter{chapter}", issue_num, kind="proof")
        start_point = f"origin/{prover_branch}"
    else:
        if (
            publication_resume is None
            and recovery_mode == RECOVERY_START_OVER
            and recovery_pr is None
        ):
            open_refines = _open_refine_prs_for_issue(cfg, issue_num)
            if open_refines is None:
                print(
                    f"--- deferring #{issue_num}: could not re-confirm that "
                    "no refine PR appeared after start-over confirmation ---",
                    flush=True,
                )
                return None
            if open_refines:
                print(
                    f"--- deferring #{issue_num}: a refine PR appeared after "
                    "the no-PR start-over confirmation; refusing to replace "
                    "work the user never saw ---",
                    flush=True,
                )
                _park_deferred_recovery(cfg, issue_num)
                return None
        if publication_resume is not None:
            branch = publication_resume[2]
            print(
                f"--- resuming publication-only checkpoint for #{issue_num}: "
                f"{publication_resume[1]} ---",
                flush=True,
            )
        elif recovery_mode == RECOVERY_START_OVER and recovery_pr is None:
            if not expected_recovery_id:
                print(
                    f"--- deferring #{issue_num}: no-PR start-over lacks an "
                    "operation id ---",
                    flush=True,
                )
                return None
            selection = _select_no_pr_start_over_branch(
                cfg,
                issue_num,
                chapter,
                expected_verdict_ts,
                expected_recovery_id,
                runs_parent,
                kept_worktrees,
            )
            if selection is None:
                return None
            branch, start_over_resume = selection
            # Preserve the expected-absence lease all the way to publication.
            # Refine must not re-observe a branch created after this guard and
            # adopt its new OID as permission to overwrite it.
            auto_pr_expected_remote_oid = ""
        elif recovery_branch and recovery_mode:
            branch = recovery_branch
        else:
            selected_branch = _refine_branch_for_dispatch(
                cfg, issue_num, chapter, expected_verdict_ts
            )
            if selected_branch is None:
                return None
            branch = selected_branch
        if (
            publication_resume is None
            and recovery_mode == RECOVERY_UPDATE_CONTINUE
            and recovery_pr is not None
            and recovery_head_sha is not None
            and expected_recovery_id is not None
        ):
            update_resume = _update_recovery_resume_checkout(
                cfg.repo_dir,
                runs_parent,
                issue_num,
                branch,
                kept_worktrees,
                expected_operation_id=expected_recovery_id,
                expected_repo=cfg.github_repo,
                expected_pr=recovery_pr,
                expected_head=recovery_head_sha,
                expected_base=recovery_base or "main",
                target_relpath=_target_relpath(cfg, chapter),
            )
            if update_resume is None:
                update_adoption = _update_recovery_adoption_checkout(
                    cfg.repo_dir,
                    runs_parent,
                    issue_num,
                    branch,
                    kept_worktrees,
                    expected_operation_id=expected_recovery_id,
                    expected_repo=cfg.github_repo,
                    expected_pr=recovery_pr,
                    expected_head=recovery_head_sha,
                    expected_base=recovery_base or "main",
                    target_relpath=_target_relpath(cfg, chapter),
                )
        start_point = WORKTREE_START_POINT
        if publication_resume is not None:
            # The write-ahead recipe proves that this exact operation may have
            # advanced the remote from the originally selected head.  Reuse
            # the kept checkout and let refine retry publication idempotently;
            # never fetch/reset/archive it against the stale original head.
            start_point = WORKTREE_START_POINT
        elif recovery_mode in {
            RECOVERY_RETRY_BUILD,
            RECOVERY_CONTINUE_PR,
            RECOVERY_UPDATE_CONTINUE,
        }:
            assert recovery_head_sha is not None
            if not (
                recovery_mode == RECOVERY_UPDATE_CONTINUE
                and update_resume is not None
            ):
                if not _fetch_exact_recovery_head(
                    cfg.repo_dir, branch, recovery_head_sha
                ):
                    return None
            start_point = f"origin/{branch}"
        elif (
            recovery_mode == RECOVERY_START_OVER
            and recovery_branch
            and recovery_head_sha
            and expected_recovery_id
        ):
            start_over_resume = _start_over_recovery_resume_checkout(
                cfg.repo_dir,
                runs_parent,
                issue_num,
                recovery_branch,
                kept_worktrees,
                expected_operation_id=expected_recovery_id,
                expected_repo=cfg.github_repo,
                target_relpath=_target_relpath(cfg, chapter),
                expected_pr=recovery_pr,
                expected_head=recovery_head_sha,
                expected_base=recovery_base,
            )
            if start_over_resume is None and not _archive_start_over_head(
                cfg,
                issue_num,
                recovery_branch,
                recovery_head_sha,
                expected_recovery_id,
            ):
                return None
        if (
            publication_resume is None
            and recovery_mode == RECOVERY_UPDATE_CONTINUE
            and recovery_head_sha
            and expected_recovery_id
            and update_resume is None
            and not _preserve_local_branch_ref(
                cfg.repo_dir,
                issue_num,
                branch,
                recovery_head_sha,
                expected_recovery_id,
            )
        ):
            return None
        if (
            publication_resume is None
            and recovery_mode == RECOVERY_CONTINUE_PR
            and recovery_head_sha
            and expected_recovery_id
            and _local_branch_head(cfg.repo_dir, branch) is not None
            and not _local_branch_extends(
                cfg.repo_dir, branch, recovery_head_sha
            )
        ):
            _preserve_local_branch_ref(
                cfg.repo_dir,
                issue_num,
                branch,
                recovery_head_sha,
                expected_recovery_id,
            )
            print(
                f"--- deferring #{issue_num}: local {branch} does not "
                "contain the selected PR head; archived it but will not "
                "publish divergent history ---",
                flush=True,
            )
            return None

    kept_key = _kept_worktree_key(issue_num, fill=fill)
    update_prepared = False
    reuse = (
        kept_worktrees.get(kept_key)
        if recovery_mode not in {
            RECOVERY_RETRY_BUILD,
            RECOVERY_UPDATE_CONTINUE,
            RECOVERY_START_OVER,
        }
        else None
    )
    selected_recovery_workdir: Optional[Path] = None
    if publication_resume is not None:
        resume_worktree, resume_workdir, _resume_branch = publication_resume
        reuse = str(resume_worktree)
        selected_recovery_workdir = resume_workdir
        # In update fallback mode this bypasses the merge/build wrapper and
        # invokes refine's publication-only fast path directly.
        update_prepared = True
    if recovery_mode == RECOVERY_UPDATE_CONTINUE and update_resume is not None:
        resume_worktree, resume_workdir, resume_phase = update_resume
        reuse = str(resume_worktree)
        selected_recovery_workdir = resume_workdir
        update_prepared = (
            resume_phase == UPDATE_RECOVERY_PHASE_FALLBACK_PREPARED
        )
        print(
            f"--- resuming base-update phase {resume_phase} for "
            f"#{issue_num}: {resume_workdir} ---",
            flush=True,
        )
    if recovery_mode == RECOVERY_UPDATE_CONTINUE and update_adoption is not None:
        adoption_worktree, adoption_workdir, stale_update_marker = update_adoption
        if stale_update_marker is not None and not _retire_terminal_update_marker(
            adoption_workdir / UPDATE_RECOVERY_MARKER_FILENAME,
            stale_update_marker,
            replacement_operation_id=expected_recovery_id or "",
        ):
            kept_worktrees[kept_key] = str(adoption_worktree)
            print(
                f"--- deferring #{issue_num}: terminal update journal changed "
                "before it could be archived ---",
                flush=True,
            )
            return None
        reuse = str(adoption_worktree)
        selected_recovery_workdir = adoption_workdir
        print(
            f"--- adopting preserved PR checkpoint for #{issue_num}: "
            f"{adoption_workdir} ---",
            flush=True,
        )
    if recovery_mode == RECOVERY_START_OVER and start_over_resume is not None:
        resume_worktree, resume_workdir = start_over_resume
        reuse = str(resume_worktree)
        selected_recovery_workdir = resume_workdir
        print(
            f"--- resuming start-over checkpoint for #{issue_num}: "
            f"{resume_workdir} ---",
            flush=True,
        )
    if (
        not reuse
        and publication_resume is None
        and recovery_mode == RECOVERY_CONTINUE_PR
    ):
        held = _registered_worktree_for_branch(cfg.repo_dir, branch)
        if (
            held is not None
            and held.is_dir()
            and _worktree_live_pid(str(held)) is None
            and held.resolve().is_relative_to(runs_parent.resolve())
            and recovery_head_sha is not None
            and _local_branch_extends(
                cfg.repo_dir, branch, recovery_head_sha
            )
        ):
            reuse = str(held)
            print(
                f"--- recovered preserved worktree for #{issue_num}: {held} ---",
                flush=True,
            )
    retry_workdir: Optional[Path] = None
    start_over_marker_prepared = False
    if reuse and Path(reuse).is_dir():
        worktree = Path(reuse)
        retry_workdir = selected_recovery_workdir or _paired_retry_workdir(
            runs_parent, issue_num, worktree
        )
        print(f"--- reusing kept worktree for #{issue_num}: {worktree} ---")
        if retry_workdir is not None:
            print(
                f"--- continuing kept Aristotle checkpoint for #{issue_num}: "
                f"{retry_workdir} ---",
                flush=True,
            )
        # A kept worktree from a FAILED attempt may already have Mathlib built
        # (the failure was mid-refine, not mid-build); only prime when it isn't,
        # so retries don't pay a redundant decompress.
        if (
            publication_resume is None
            and not _mathlib_already_built(worktree)
        ):
            prime_result = _prime_job_mathlib_cache(worktree)
            if prime_result is _CACHE_PRIME_UNSAFE:
                kept_worktrees[kept_key] = str(worktree)
                print(
                    f"--- deferring #{issue_num}: interrupted Lake dependency "
                    "could not be quarantined safely ---",
                    flush=True,
                )
                return None
    else:
        worktree = runs_parent / f"wt-i{issue_num}-{ts}"
        runs_parent.mkdir(parents=True, exist_ok=True)
        if (
            recovery_mode == RECOVERY_START_OVER
            and expected_recovery_id is not None
        ):
            # Write-ahead ownership precedes even `worktree add -B`: that Git
            # command creates the checkout and moves/claims the branch. A kill
            # immediately after it must not leave an unidentifiable held tree.
            # The paired paths are deterministic, so an orphan marker after a
            # failed add is harmless and can be safely reused/cleaned later.
            marker_workdir = runs_parent / f"wd-i{issue_num}-{ts}"
            marker_target = worktree / _target_relpath(cfg, chapter)
            if not _write_start_over_recovery_marker(
                marker_workdir,
                operation_id=expected_recovery_id,
                github_repo=cfg.github_repo,
                issue_num=issue_num,
                branch=branch,
                target_path=marker_target,
                recovery_pr=recovery_pr,
                original_expected_head=recovery_head_sha,
                base_branch=recovery_base,
            ):
                return None
            start_over_marker_prepared = True
        if recovery_mode == RECOVERY_RETRY_BUILD:
            # A build-only check never publishes code and need not own the PR's
            # local branch. Detached checkout lets it coexist with a dirty
            # continuation worktree that must remain untouched.
            proc = _git(
                cfg.repo_dir,
                "worktree", "add", "--detach", str(worktree), start_point,
            )
        elif (
            recovery_mode == RECOVERY_CONTINUE_PR
            and recovery_head_sha is not None
            and _local_branch_extends(
                cfg.repo_dir, branch, recovery_head_sha
            )
        ):
            # Prefer an unpublished local tip over the remote PR head. The
            # final push remains leased against the exact remote SHA selected
            # by the user, so this cannot overwrite a concurrent update.
            proc = _git(
                cfg.repo_dir,
                "worktree", "add", str(worktree), branch,
            )
        else:
            proc = _git(
                cfg.repo_dir,
                "worktree", "add", "-B", branch, str(worktree), start_point,
            )
        if proc.returncode != 0:
            msg = ((proc.stderr or proc.stdout) or "").strip()
            lower = msg.lower()
            if "already checked out" in lower or "already used by worktree" in lower:
                # A leftover worktree from a previous run still holds this issue's
                # branch (kept_worktrees is in-memory, so a conductor restart
                # orphans it). Reap it and retry the add ONCE — otherwise the
                # issue wedges in a defer loop forever (the #96 failure mode).
                # UNLESS a live job from a previous conductor is still using it
                # (the reap guard says so): then we defer this dispatch and let
                # the running job finish — reaping under it destroys a billed
                # Aristotle run (the 2026-07-07 #171 incident).
                print(
                    f"--- #{issue_num}: branch {branch} held by a leftover "
                    "worktree; reaping it and retrying the add ---", flush=True,
                )
                if (
                    recovery_mode != RECOVERY_RETRY_BUILD
                    and _reap_orphan_worktree(cfg.repo_dir, branch)
                ):
                    proc = _git(
                        cfg.repo_dir, "worktree", "add", "-B", branch,
                        str(worktree), start_point,
                    )
        if proc.returncode != 0:
            msg = ((proc.stderr or proc.stdout) or "").strip()
            print(
                f"--- deferring #{issue_num}: `git worktree add` failed: "
                f"{msg} ---", flush=True,
            )
            return None
        # A git worktree has source but none of the ignored project oleans.
        # Copy the live checkout's generated project build into this private
        # fresh worktree before priming Mathlib. Lake's trace hashes validate
        # the seed and rebuild the PR's changed modules; a failed/missing seed
        # merely retains the ordinary from-scratch project-build path.
        _seed_project_build_outputs(cfg.repo_dir, worktree)
        # Seed the fresh worktree's Mathlib oleans so the refine's `lake build`
        # skips the from-scratch Mathlib compile. An ordinary failure falls
        # back to a normal build; a known-poisoned checkout that cannot be
        # quarantined defers rather than launching a doomed worker.
        prime_result = _prime_job_mathlib_cache(worktree)
        if prime_result is _CACHE_PRIME_UNSAFE:
            kept_worktrees[kept_key] = str(worktree)
            print(
                f"--- deferring #{issue_num}: interrupted Lake dependency "
                "could not be quarantined safely ---",
                flush=True,
            )
            return None

    # Reuse BOTH halves of a failed attempt.  Reusing only the worktree keeps
    # extracted code but throws away the project/task ids needed for
    # ``project.ask()`` continuation, forcing an unnecessary new Aristotle
    # session on every conductor-level retry.
    workdir = retry_workdir or (runs_parent / f"wd-i{issue_num}-{ts}")
    workdir.mkdir(parents=True, exist_ok=True)
    target = worktree / _target_relpath(cfg, chapter)
    if (
        recovery_mode == RECOVERY_START_OVER
        and expected_recovery_id is not None
        and not start_over_marker_prepared
        and not _write_start_over_recovery_marker(
            workdir,
            operation_id=expected_recovery_id,
            github_repo=cfg.github_repo,
            issue_num=issue_num,
            branch=branch,
            target_path=target,
            recovery_pr=recovery_pr,
            original_expected_head=recovery_head_sha,
            base_branch=recovery_base,
        )
    ):
        kept_worktrees[kept_key] = str(worktree)
        return None
    if recovery_mode == RECOVERY_CONTINUE_PR and not _prepare_continue_checkpoint(
        worktree,
        workdir,
        target,
        branch,
        expected_verdict_ts,
    ):
        kept_worktrees[kept_key] = str(worktree)
        print(
            f"--- deferring #{issue_num}: could not prepare the preserved "
            "PR/checkpoint for continuation ---",
            flush=True,
        )
        return None
    if not fill and not repair:
        # A publication-only retry belongs to the OLD committed verdict even if
        # the human has since re-rejected.  Recover its persisted CAS token so
        # publishing that old commit can never consume the newer verdict.
        try:
            checkpoint = load_refine_state(workdir / REFINE_STATE_FILENAME)
        except Exception:  # noqa: BLE001 — malformed checkpoint fails in child
            checkpoint = None
        if (
            checkpoint is not None
            and checkpoint.pending_publication is not None
            and checkpoint.review_verdict_ts
        ):
            expected_verdict_ts = checkpoint.review_verdict_ts
            pending_branch = checkpoint.pending_publication.get("branch")
            if isinstance(pending_branch, str) and pending_branch:
                # Publication-only recovery must keep the branch recorded with
                # the committed fix, even if today's remote-state selector
                # would choose a different branch.
                branch = pending_branch
    # The exact per-issue dispatch the daemon uses, flag-for-flag:
    # DEFAULT_REFINE_ARGS is imported, never forked. The conductor adds
    # exactly one flag of its own at this dispatch site:
    # --no-metadata-commit — N parallel workers all rewriting
    # formalization.yaml is the generalized form of the wall_time merge
    # race, so jobs never commit the yaml; the conductor regenerates it
    # centrally after each success (_regenerate_metadata_after_success).
    if recovery_mode == RECOVERY_RETRY_BUILD:
        cmd = [
            sys.executable,
            "-m",
            "marathon.recovery",
            "retry-build",
            "--repo-dir",
            str(worktree),
            "--repo",
            cfg.github_repo,
            "--pr",
            str(recovery_pr),
            "--expected-head",
            str(recovery_head_sha),
            "--expected-branch",
            branch,
            "--expected-base",
            recovery_base or "main",
            "--target",
            _target_relpath(cfg, chapter),
            "--build-timeout",
            "2400",
        ]
        log_path = workdir / "conductor-recovery-build.log"
    elif repair:
        # Repair the chapter's non-building modules against the CURRENT code.
        # The conductor passes the broken set explicitly (a fresh worktree has
        # no audit snapshot to discover from). `marathon repair` defaults
        # auto-build/commit/push/pr ON and runs the faithfulness jury; it lands
        # on main under marathon/repair-c<N>.
        cmd = [
            sys.executable, "-m", "marathon", "repair",
            str(target),
            "--repo-dir", str(worktree),
            "--workdir", str(workdir),
            "--no-metadata-commit",
            "--build-timeout", "2400",
            "--auto-pr-repo", cfg.github_repo,
        ]
        for m in (repair_modules or []):
            cmd += ["--module", m]
        log_path = workdir / "conductor-repair.log"
        cmd += _tex_args(cfg, chapter)
    elif fill:
        # Prove the verified-but-`sorry` declarations cited in this issue. The
        # statement-immutability guard (enforce_frozen_statements, default on)
        # reverts any change to an approved signature, so a fill can only fill
        # bodies + add sub-lemmas. Reuses the per-issue branch so the existing
        # automerge path lands the proof PR. `marathon fill` defaults
        # auto-build/commit/push/pr/rate ON, so only the conductor-specific
        # flags are passed.
        cmd = [
            sys.executable, "-m", "marathon", "fill",
            str(target),
            "--repo-dir", str(worktree),
            "--workdir", str(workdir),
            "--issue", str(issue_num),
            "--no-metadata-commit",
            "--build-timeout", "2400",
            "--auto-pr-repo", cfg.github_repo,
            # Proofs land on the SEPARATE prover branch, never main. The
            # worktree already branched from origin/<prover_branch>; target the
            # PR at it too so the [build:OK] automerge (base-equality gated)
            # can only ever merge this proof into the prover branch.
            "--auto-pr-base", prover_branch,
            # Machine-clean acceptance: enforce the proof gate so a fill whose
            # proof leaves a nested `sorry` or smuggles an axiom (sorryAx / a
            # non-whitelisted axiom) is BLOCKED from opening its PR — the
            # cheat/incomplete proof never merges, the decl stays a live
            # target, and the automerge path only ever lands genuinely-clean
            # proofs. `fill_run` exempts this from the review-rejection
            # enforce→warn demotion.
            "--gate", "enforce",
            # The orchestrator's two jobs on a proof (Aristotle does the
            # proving): CHECK FAITHFULNESS — the proof-integrity jury flags a
            # compile-but-cheat proof (vacuous body, orphan-class-as-axiom, a
            # PUnit placeholder) that the statement-immutability guard + the
            # axiom gate don't catch — and STEER — the Hermes watcher nudges
            # Aristotle when a proof goes off-course. Both run on Claude (the
            # orchestrator), never Aristotle.
            "--jury",
            "--live-steering",
        ]
        log_path = workdir / "conductor-fill.log"
        cmd += _tex_args(cfg, chapter)
    else:
        rejection_note_file: Optional[Path] = None
        if rejection_note and rejection_note.strip():
            rejection_note_file = workdir / "review-rejection.json"
            rejection_note_file.write_text(
                json.dumps(
                    {
                        "issue": issue_num,
                        "verdict_ts": expected_verdict_ts,
                        "notes": rejection_note,
                    },
                    ensure_ascii=False,
                    indent=2,
                )
                + "\n"
            )
        cmd = [
            sys.executable, "-m", "marathon", "refine",
            str(target),
            "--repo-dir", str(worktree),
            "--workdir", str(workdir),
            "--review-rejection", str(issue_num),
            "--no-metadata-commit",
            # The consumer repo is configuration, not something a worker must
            # infer through GraphQL.  #238 completed and built successfully but
            # silently skipped its PR when `gh repo view` hit quota.
            "--auto-pr-repo", cfg.github_repo,
            "--auto-pr-branch", branch,
        ]
        if expected_verdict_ts is not None:
            cmd += ["--review-verdict-ts", expected_verdict_ts]
        if auto_pr_expected_remote_oid is not None:
            cmd += [
                "--auto-pr-expected-remote-oid",
                auto_pr_expected_remote_oid,
            ]
        if recovery_base is not None:
            cmd += ["--auto-pr-base", recovery_base]
        if expected_recovery_id is not None:
            cmd += ["--recovery-operation-id", expected_recovery_id]
        if recovery_mode == RECOVERY_CONTINUE_PR:
            cmd += ["--auto-pr-continue-remote"]
        if rejection_note_file is not None:
            cmd += ["--review-rejection-note-file", str(rejection_note_file)]
        cmd += daemon.DEFAULT_REFINE_ARGS
        log_path = workdir / "conductor-refine.log"
        cmd += _tex_args(cfg, chapter)
        if (
            recovery_mode == RECOVERY_UPDATE_CONTINUE
            and not update_prepared
        ):
            fallback_cmd = cmd
            cmd = [
                sys.executable,
                "-m",
                "marathon.recovery",
                "update-continue",
                "--repo-dir",
                str(worktree),
                "--repo",
                cfg.github_repo,
                "--pr",
                str(recovery_pr),
                "--expected-head",
                str(recovery_head_sha),
                "--expected-branch",
                branch,
                "--expected-base",
                recovery_base or "main",
                "--operation-id",
                str(expected_recovery_id),
                "--target",
                _target_relpath(cfg, chapter),
                "--workdir",
                str(workdir),
                "--build-timeout",
                "2400",
                "--",
                *fallback_cmd,
            ]
            log_path = workdir / "conductor-recovery-update.log"
    job = ConductorJob(
        issue_num=issue_num,
        chapter=chapter,
        target=_target_relpath(cfg, chapter),
        worktree=str(worktree),
        workdir=str(workdir),
        branch=branch,
        pid=None,
        started_ts=now_iso(),
        status="launching",
        fill=fill,
        repair=repair,
        expected_verdict_ts=expected_verdict_ts,
        expected_recovery_id=expected_recovery_id,
        recovery_mode=recovery_mode,
        recovery_pr=recovery_pr,
        recovery_head_sha=recovery_head_sha,
        recovery_base=recovery_base,
        rejection_note=rejection_note,
        launch_nonce=secrets.token_hex(16),
    )
    # Durable launch intent comes before Popen. If this conductor is SIGKILLed
    # immediately after spawning the detached worker, the replacement can scan
    # the marker, recover the PID from the command line, and adopt the job.
    if not _write_worktree_pid_marker(worktree, 0, job=job):
        kept_worktrees[kept_key] = str(worktree)
        print(
            f"--- deferring #{issue_num}: could not durably record worker "
            "launch intent; refusing to spawn ---",
            flush=True,
        )
        return None
    log_handle = log_path.open("ab")
    try:
        # Refine/fill/repair jobs must outlive a scheduler restart.  Giving each
        # worker its own session prevents Ctrl-C, terminal teardown, or a
        # conductor-only signal from reaching an already-billed Aristotle run.
        # DEVNULL stdin also severs the controlling terminal input inherited by
        # old workers. Windows needs its equivalent process-group flags.
        spawn_kwargs: dict[str, object] = {
            "stdout": log_handle,
            "stderr": subprocess.STDOUT,
            "stdin": subprocess.DEVNULL,
        }
        if os.name == "nt":  # pragma: no cover - POSIX CI/runtime
            spawn_kwargs["creationflags"] = (
                getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
                | getattr(subprocess, "DETACHED_PROCESS", 0)
            )
        else:
            spawn_kwargs["start_new_session"] = True
        marker_path = worktree / WORKTREE_PID_MARKER
        launch_cmd = [
            sys.executable,
            "-m",
            "marathon.worker_launch",
            str(marker_path),
            "--",
            *cmd,
        ]
        popen = subprocess.Popen(launch_cmd, **spawn_kwargs)
    except OSError as e:
        log_handle.close()
        _clear_worktree_pid_marker(str(worktree))
        # The worktree exists and holds the branch now; remember it so
        # the retry reuses it instead of tripping its own branch guard.
        kept_worktrees[kept_key] = str(worktree)
        print(f"--- deferring #{issue_num}: could not spawn refine ({e}) ---")
        return None
    handshake = _await_worker_handshake(worktree, popen)
    if handshake is None:
        # The trampoline never starts billed work until its marker update
        # succeeds. Terminating it here is therefore safe and prevents an
        # untracked process from lingering after a disk/exec failure.
        try:
            popen.terminate()
            popen.wait(timeout=2)
        except Exception:  # noqa: BLE001 — best-effort cleanup of failed launch
            try:
                popen.kill()
            except Exception:
                pass
        log_handle.close()
        _clear_worktree_pid_marker(str(worktree))
        kept_worktrees[kept_key] = str(worktree)
        print(
            f"--- deferring #{issue_num}: detached worker did not complete "
            "its durable launch handshake; billed command was not started ---",
            flush=True,
        )
        return None
    kept_worktrees.pop(kept_key, None)
    # Live-job marker: lets a FUTURE conductor process (after a restart) tell
    # this worktree is in use, so its reap guard defers instead of destroying
    # a running (billed) job.
    job.pid = popen.pid
    try:
        job.pgid = int(handshake["pgid"]) if handshake.get("pgid") else None
    except (TypeError, ValueError):
        job.pgid = None
    job.status = "running"
    job.proc = popen
    job.log_handle = log_handle
    print(
        f"\n--- dispatched #{issue_num} (chapter {chapter}): pid={popen.pid} "
        f"branch={branch} ---"
    )
    print(f"    worktree = {worktree}")
    print(f"    workdir  = {workdir}")
    print(f"    log      = {log_path}")
    print(f"    cmd      = {' '.join(cmd)}", flush=True)
    return job


def _refresh_job_runtime(job: ConductorJob) -> None:
    """Pull project id + task status from the job's refine checkpoint —
    the cheap per-tick status source (one small file read; no API
    calls, hence no percent_complete — see module docstring)."""
    try:
        st = load_refine_state(Path(job.workdir) / REFINE_STATE_FILENAME)
    except Exception:  # noqa: BLE001 — half-written checkpoint mid-save
        return
    if st is not None:
        if st.project_id:
            job.project_id = st.project_id
        if st.status:
            job.aristotle_status = st.status


def _metadata_push_enabled() -> bool:
    """The conductor's metadata commits follow the dispatched jobs'
    auto-push setting (read from DEFAULT_REFINE_ARGS, the single source
    for the job flag set) so the yaml lands wherever the iterations
    land."""
    return "--auto-push" in daemon.DEFAULT_REFINE_ARGS


def _regenerate_metadata_after_success(cfg: ReviewConfig) -> None:
    """Conductor-side metadata regeneration (plan §3 Phase 3: metadata
    files move to Conductor-side regeneration — never committed by
    workers). Jobs are dispatched with --no-metadata-commit, so after
    each successful job the conductor refreshes formalization.yaml ONCE
    in the PRIMARY repo checkout — one writer instead of N racing ones.

    Deterministic guards, never a crash: the primary checkout must be
    on the base branch with a clean tree (the operator may be mid-work
    there; the conductor must not commit onto their branch or tangle
    their dirt). Dirt confined to .marathon/ bookkeeping is carved out
    of "clean" — see the comment at the status check. Otherwise a
    deferral note is printed — the next successful job, or a manual
    `marathon formalization update`, picks it up."""
    head = _git(cfg.repo_dir, "rev-parse", "--abbrev-ref", "HEAD")
    branch = (head.stdout or "").strip()
    if head.returncode != 0 or branch != METADATA_BASE_BRANCH:
        print(
            f"  metadata: deferred — primary checkout is on "
            f"{branch or '?'!r}, not {METADATA_BASE_BRANCH!r}; run "
            "`marathon formalization update` there when convenient",
            flush=True,
        )
        return
    from marathon.formalization import (
        is_ignorable_bookkeeping_dirt,
        regenerate_metadata,
    )
    status = _git(cfg.repo_dir, "status", "--porcelain")
    # record_iteration has just dirtied .marathon/ bookkeeping
    # (review/state.json et al.) in this very checkout — and consumer
    # repos may git-track those files — so "any dirt at all" would
    # defer after EVERY success and the yaml would never regenerate.
    # This guard exists to protect operator work in flight, not
    # marathon's own bookkeeping: ignore dirt confined to .marathon/
    # (regenerate_metadata stages only formalization.yaml, so the
    # carved-out files never ride into its commit) and still defer on
    # any other dirty path.
    dirty = [
        line for line in (status.stdout or "").rstrip("\n").splitlines()
        if line.strip() and not is_ignorable_bookkeeping_dirt(line)
    ]
    if status.returncode != 0 or dirty:
        print(
            "  metadata: deferred — primary checkout is dirty; the "
            "conductor never commits over operator work in progress",
            flush=True,
        )
        return
    # Central metric aggregation (plan §2): fold every per-run shard under
    # .marathon/metrics/ — written by the refines that just merged, whose
    # PRs never collided because each shard path is project-UUID-keyed —
    # back into the two human-readable rollups (.marathon/wall-time.json,
    # .marathon/PromptLog.md) and refresh the ledger. The rollups + any
    # newly-merged shards then ride the SAME metadata-regen PR as the yaml
    # (one central writer, one merge path). Best-effort: a failure here
    # must not block the yaml regen below.
    extra_paths: list[str] = []
    try:
        from marathon.metrics import METRICS_DIR_RELPATH, regenerate_rollups

        written = regenerate_rollups(cfg.repo_dir)
        for p in written:
            try:
                extra_paths.append(p.relative_to(cfg.repo_dir).as_posix())
            except ValueError:
                pass
        # Stage the shards directory too, so shards that merged via refine
        # PRs (and any written here) are tracked in the same central commit.
        if (cfg.repo_dir / METRICS_DIR_RELPATH).is_dir():
            extra_paths.append(METRICS_DIR_RELPATH.as_posix())
    except Exception as e:  # noqa: BLE001 — aggregation must not kill the loop
        print(f"  metadata: metric aggregation skipped — {type(e).__name__}: {e}")

    try:
        changed = regenerate_metadata(
            cfg.repo_dir, commit=True, push=_metadata_push_enabled(),
            # Route the central regen through an auto-merged PR instead of
            # a direct push to main: main is ruleset-protected, so a direct
            # push to refs/heads/main is rejected with GH013 and the commit
            # silently never reaches origin (GH013 metadata-push bug).
            github_repo=cfg.github_repo,
            base_branch=METADATA_BASE_BRANCH,
            # Carry the centrally-aggregated rollups + shards on the same PR.
            extra_paths=extra_paths,
        )
    except Exception as e:  # noqa: BLE001 — bookkeeping must not kill the loop
        print(f"  metadata: regeneration skipped — {type(e).__name__}: {e}")
        return
    if changed:
        print("  metadata: formalization.yaml + metric rollups regenerated + committed", flush=True)


def _branch_pr_exists(
    cfg: ReviewConfig,
    branch: str,
    expected_head_sha: Optional[str] = None,
) -> Optional[bool]:
    """Whether a PR for ``branch`` publishes the expected commit.

    A persistent per-issue branch can have older PR history.  Merely finding
    *any* PR caused #238's old 50eff009 PR to count as publication of the new
    local 6221ffa commit.  When ``expected_head_sha`` is supplied, only a PR
    whose REST ``head.sha`` matches that iteration counts. ``None`` means the
    REST check itself was unavailable and must be retried fail-closed.
    """
    rows = _branch_pull_requests(cfg, branch, state="all")
    if rows is None:
        return None
    if not expected_head_sha:
        return bool(rows)
    expected = expected_head_sha.lower()
    for row in rows:
        head = row.get("head") if isinstance(row, dict) else None
        sha = head.get("sha") if isinstance(head, dict) else None
        if isinstance(sha, str) and (
            sha.lower().startswith(expected) or expected.startswith(sha.lower())
        ):
            return True
    return False


def _adopted_recovery_build_status(
    cfg: ReviewConfig, sha: Optional[str]
) -> Optional[str]:
    """Return the trusted Marathon build status for one immutable commit.

    An adopted recovery has no ``Popen`` handle, so its exit code is gone.
    Existing PR presence is not completion evidence because recovery starts
    from an already-open PR.  The exact-SHA commit status is the durable
    evidence written by ``marathon.recovery``; unknown/malformed/API-failure
    states fail closed as ``None``.
    """
    if not sha:
        return None
    from marathon.review.automerge import TRUSTED_BUILD_CONTEXT
    from marathon.review.github import gh

    try:
        cp = gh(
            "api",
            f"repos/{cfg.github_repo}/commits/{sha}/statuses",
            check=False,
            timeout=20,
        )
    except Exception:  # noqa: BLE001 — restart recovery must fail closed
        return None
    if cp.returncode != 0:
        return None
    try:
        rows = json.loads(cp.stdout or "[]")
        if not isinstance(rows, list):
            return None
        matches = [
            row for row in rows
            if isinstance(row, dict)
            and row.get("context") == TRUSTED_BUILD_CONTEXT
        ]
        if not matches:
            return None
        # GitHub returns newest-first today, but select explicitly so an API
        # ordering change cannot resurrect an older green/red attestation.
        latest = max(
            matches,
            key=lambda row: (
                str(row.get("created_at") or ""),
                int(row.get("id") or 0),
            ),
        )
        state = str(latest.get("state") or "").lower()
    except (TypeError, ValueError, json.JSONDecodeError):
        return None
    if state == "success":
        return "OK"
    if state in {"failure", "error"}:
        return "FAIL"
    if state == "pending":
        return "PENDING"
    return None


def _adopted_update_recovery_marker(
    cfg: ReviewConfig, job: ConductorJob
) -> Optional[dict]:
    """Load the crash journal only when it belongs to this exact recovery.

    A green status on the original PR head may predate an ``update_continue``
    request.  Consequently, process death plus a green local HEAD is never
    terminal evidence by itself: only this operation's schema-v2 journal can
    identify a published fast path (or a prepared fallback refine).
    """
    marker_path = Path(job.workdir) / UPDATE_RECOVERY_MARKER_FILENAME
    try:
        marker = json.loads(marker_path.read_text())
        if not isinstance(marker, dict):
            return None
        current_head = marker.get("current_local_head")
        base_sha = marker.get("base_sha")
        target_path = Path(marker.get("target_path"))
        expected_target = (Path(job.worktree) / job.target).resolve()
        marker_target = target_path.resolve()
    except (OSError, RuntimeError, TypeError, ValueError, json.JSONDecodeError):
        return None

    def _full_oid(value: object) -> bool:
        return (
            isinstance(value, str)
            and len(value) == 40
            and all(char in "0123456789abcdefABCDEF" for char in value)
        )

    expected_repo = getattr(cfg, "github_repo", None)
    identity_ok = (
        marker.get("schema_version") == UPDATE_RECOVERY_SCHEMA_VERSION
        and marker.get("phase") in UPDATE_RECOVERY_PHASES
        and marker.get("operation_id") == job.expected_recovery_id
        and (
            expected_repo is None
            or marker.get("github_repo") == expected_repo
        )
        and not isinstance(marker.get("pr"), bool)
        and marker.get("pr") == job.recovery_pr
        and marker.get("branch") == job.branch
        and marker.get("original_expected_head") == job.recovery_head_sha
        and marker.get("base_branch") == (job.recovery_base or "main")
        and _full_oid(current_head)
        and _full_oid(base_sha)
        and marker_target == expected_target
    )
    return marker if identity_ok else None


def _reap_adopted_job(
    cfg: ReviewConfig, job: ConductorJob, kept_worktrees: dict[int, str],
    *, reconcile: bool,
) -> None:
    """An ADOPTED refine (inherited from a previous conductor, no proc handle)
    has exited. We can't read its exit code, so decide by PR presence on its
    branch: a PR means the refine SUCCEEDED (it built + opened it) → mark the
    issue iterated so it isn't re-dispatched (automerge lands the PR → requeue);
    no PR + still rejected → the no-diff case, requeue for re-review; a genuine
    failure (no PR, gh unavailable) leaves it rejected to retry fresh next tick.
    A fill/repair is autonomous (no rejection state) → just clean up."""
    job.finished_ts = now_iso()
    _clear_worktree_pid_marker(job.worktree)
    try:
        checkpoint = load_refine_state(
            Path(job.workdir) / REFINE_STATE_FILENAME
        )
    except Exception:  # noqa: BLE001 — malformed checkpoint uses legacy path
        checkpoint = None
    if checkpoint is not None and (
        checkpoint.pending_publication is not None
        or checkpoint.force_continuation
        or checkpoint.preserve_local_branch
    ):
        # Exit code is unavailable after conductor restart, but the durable
        # checkpoint proves this is NOT a clean terminal job. Preserve the
        # committed worktree/session for publication-only or project.ask retry.
        job.status = (
            "publication_pending"
            if checkpoint.pending_publication is not None
            else "failed"
        )
        kept_worktrees[
            _kept_worktree_key(job.issue_num, fill=job.fill)
        ] = job.worktree
        print(
            f"--- adopted #{job.issue_num} exited with resumable checkpoint "
            f"({job.status}); kept worktree/session: {job.worktree} ---",
            flush=True,
        )
        return
    completed_cleanly = bool(
        checkpoint is not None
        and checkpoint.current_iteration_idx > 0
        and checkpoint.iterations_completed >= checkpoint.current_iteration_idx
    )
    if job.fill or job.repair:
        if completed_cleanly:
            job.status = "succeeded"
            _remove_worktree(cfg.repo_dir, Path(job.worktree))
            print(
                f"    adopted autonomous job #{job.issue_num} has affirmative "
                "completion evidence; cleaned up",
                flush=True,
            )
        else:
            # No exit code after restart and no affirmative completion/PR:
            # SUBMIT/POLL/download/branch failures all land here. Preserve the
            # checkpoint and retry; never infer success from process death.
            job.status = "failed"
            kept_worktrees[
                _kept_worktree_key(job.issue_num, fill=job.fill)
            ] = job.worktree
            print(
                f"--- adopted autonomous job #{job.issue_num} exited without "
                f"completion evidence; kept worktree/session for retry: "
                f"{job.worktree} ---",
                flush=True,
            )
        return
    if job.recovery_mode == RECOVERY_RETRY_BUILD:
        build_status = _adopted_recovery_build_status(
            cfg, job.recovery_head_sha
        )
        if build_status == "FAIL":
            stalled = record_stall(
                cfg,
                job.issue_num,
                expected_verdict_ts=job.expected_verdict_ts,
                expected_recovery_id=job.expected_recovery_id,
            )
            job.status = "stalled" if stalled is not None else "superseded"
            _remove_worktree(cfg.repo_dir, Path(job.worktree))
            if stalled is not None:
                print(
                    f"--- adopted build-only recovery #{job.issue_num} has a "
                    "trusted red status; parked for the next recovery choice ---",
                    flush=True,
                )
            else:
                print(
                    f"--- adopted build-only recovery #{job.issue_num} was "
                    "superseded by newer review state; its stale completion "
                    "changed nothing ---",
                    flush=True,
                )
            return
        if build_status != "OK":
            job.status = "failed"
            kept_worktrees[job.issue_num] = job.worktree
            print(
                f"--- adopted build-only recovery #{job.issue_num} exited "
                "without a trusted terminal status; preserving its worktree "
                "and refusing to infer success from the pre-existing PR ---",
                flush=True,
            )
            return
    elif job.recovery_mode == RECOVERY_UPDATE_CONTINUE:
        # The exact crash journal, not an old green status on the original PR
        # head, decides which update operation reached a terminal phase.
        marker = _adopted_update_recovery_marker(cfg, job)
        phase = marker.get("phase") if marker is not None else None
        local_head = _job_head_sha(job)
        published_head = (
            marker.get("current_local_head") if marker is not None else None
        )
        fallback_complete = (
            completed_cleanly
            and phase == UPDATE_RECOVERY_PHASE_FALLBACK_PREPARED
        )
        fast_path_complete = (
            not completed_cleanly
            and phase == UPDATE_RECOVERY_PHASE_PUBLISHED
            and local_head == published_head
            and _adopted_recovery_build_status(cfg, published_head) == "OK"
        )
        if not (fallback_complete or fast_path_complete):
            job.status = "failed"
            kept_worktrees[job.issue_num] = job.worktree
            print(
                f"--- adopted update recovery #{job.issue_num} exited "
                f"in journal phase {phase or '<unknown>'} without trusted "
                "completion evidence; preserving its worktree and refusing "
                "to infer success from the old PR ---",
                flush=True,
            )
            return
    elif job.recovery_mode in {RECOVERY_CONTINUE_PR, RECOVERY_START_OVER} and not (
        completed_cleanly
    ):
        job.status = "failed"
        kept_worktrees[job.issue_num] = job.worktree
        print(
            f"--- adopted {job.recovery_mode} recovery #{job.issue_num} "
            "exited without a completed refine checkpoint; preserving its "
            "worktree instead of trusting a pre-existing PR ---",
            flush=True,
        )
        return
    job_head_sha = _job_head_sha(job)
    has_pr = (
        _branch_pr_exists(cfg, job.branch, job_head_sha)
        if job.branch and job_head_sha else None
    )
    if has_pr:
        job.status = "succeeded"
        if job.expected_verdict_ts is None:
            record_iteration(
                cfg,
                job.issue_num,
                expected_recovery_id=job.expected_recovery_id,
                iteration_branch=job.branch,
                iteration_commit_sha=job_head_sha,
            )
        else:
            record_iteration(
                cfg,
                job.issue_num,
                expected_verdict_ts=job.expected_verdict_ts,
                expected_recovery_id=job.expected_recovery_id,
                iteration_branch=job.branch,
                iteration_commit_sha=job_head_sha,
            )
        _remove_worktree(cfg.repo_dir, Path(job.worktree))
        _regenerate_metadata_after_success(cfg)
        print(f"--- adopted #{job.issue_num} refine (prior conductor) finished "
              f"with a PR on {job.branch}; marked iterated, automerge will land "
              "it ---", flush=True)
    else:
        # No PR (or gh down): keep the worktree; requeue-if-no-PR handles the
        # no-diff stuck case ONLY when the checkpoint proves refine_command
        # completed its iteration. Without an exit code, absence of that proof
        # means failure/missing-note/etc. and must stay queued for retry.
        job.status = "failed"
        kept_worktrees[job.issue_num] = job.worktree
        if has_pr is None and completed_cleanly:
            kwargs = {
                "pending_pr_branch": job.branch,
                "iteration_commit_sha": job_head_sha,
            }
            if job.expected_verdict_ts is not None:
                kwargs["expected_verdict_ts"] = job.expected_verdict_ts
            if job.expected_recovery_id is not None:
                kwargs["expected_recovery_id"] = job.expected_recovery_id
            record_iteration(cfg, job.issue_num, **kwargs)
        if reconcile and has_pr is False and completed_cleanly:
            try:
                _requeue_if_no_pr_landed(cfg, job)
            except Exception as exc:  # noqa: BLE001 — never crash a reap
                print(f"  adopted no-PR requeue check failed: {exc}", flush=True)
        print(f"--- adopted #{job.issue_num} refine finished with NO PR "
              f"(kept worktree) ---", flush=True)


def _requeue_if_no_pr_landed(cfg: ReviewConfig, job: ConductorJob) -> None:
    """A per-issue refine that exits clean but opened NO PR (typically because
    Aristotle produced no diff) leaves its issue stuck 'rejected' — iterated, but
    with nothing to merge, so no requeue ever fires and the deck blocks it from
    review (the #98 symptom class). If the branch has no PR and the issue is still
    rejected, requeue it for re-review. No-op when a PR exists (open → automerge →
    requeue; merged → the merged-rejection reconcile handles it)."""
    branch = getattr(job, "branch", None)
    if not branch:
        return
    from marathon.review.automerge import _REFINE_BRANCH_RE, _requeue_refined_issue

    if not _REFINE_BRANCH_RE.match(branch):
        return
    expected_head = getattr(job, "iteration_commit_sha", None)
    if expected_head is None and getattr(job, "worktree", None):
        expected_head = _job_head_sha(job)
    if not expected_head:
        # Without commit identity an older PR on the persistent branch is
        # indistinguishable from this iteration. Leave the durable pending
        # marker untouched rather than guessing success or no-PR.
        return
    exists = _branch_pr_exists(cfg, branch, expected_head)
    expected_verdict_ts = getattr(job, "expected_verdict_ts", None)
    if exists is None:
        # record_iteration already persisted pending_pr_branch.  Leave it set:
        # _retry_pending_pr_checks re-runs this cheap query on later ticks and
        # across conductor restarts, without another Aristotle dispatch.
        return
    if exists:
        if expected_verdict_ts is not None:
            clear_pending_pr_check(
                cfg,
                job.issue_num,
                branch,
                expected_verdict_ts=expected_verdict_ts,
            )
        return  # normal merge → requeue path
    st = load_state(cfg).issues.get(job.issue_num)
    if (
        st is not None
        and getattr(st, "status", None) == "rejected"
        and (
            expected_verdict_ts is None
            or st.verdict_ts == expected_verdict_ts
        )
    ):
        # landed=False: NOTHING merged here (no diff → no PR). A 'fix landed'
        # recovery comment would be a public lie — this only re-surfaces the
        # unchanged issue for re-review.
        kwargs = {"landed": False}
        if expected_verdict_ts is not None:
            kwargs["expected_verdict_ts"] = expected_verdict_ts
        # ``record_iteration`` clears operation A's recovery id before this
        # network transition. Require it to remain clear: a recovery B queued
        # while GitHub labels are being updated must win the local delete CAS.
        kwargs["expected_recovery_id"] = None
        n = _requeue_refined_issue(cfg, branch, **kwargs)
        if n is not None:
            print(f"--- #{n} refine exited clean but landed no PR — requeued for "
                  f"re-review (was stuck 'rejected') ---", flush=True)


def _retry_pending_pr_checks(cfg: ReviewConfig) -> int:
    """Resolve durable or legacy rc=0 publication uncertainty.

    ``record_iteration(..., pending_pr_branch=...)`` parks the rejection while
    GitHub is uncertain.  This pass either clears the marker when a PR exists or
    requeues unchanged code when no matching PR exists; transient failures leave
    it untouched for the next tick.  It also audits older suppressed rows that
    have ``iteration_branch`` + ``iteration_commit_sha`` but no pending marker,
    recovering the exact #238 state written before SHA-aware publication checks.
    Returns the number resolved.
    """
    from marathon.review.automerge import _requeue_refined_issue

    resolved = 0
    state = load_state(cfg)
    checks = sorted(
        (
            (issue_num, entry)
            for issue_num, entry in state.issues.items()
            if entry.status == "rejected"
            and not entry.needs_iteration()
            and (
                entry.pending_pr_branch
                or (entry.iteration_branch and entry.iteration_commit_sha)
            )
        ),
        key=lambda item: item[1].verdict_ts,
    )
    for issue_num, entry in checks:
        branch = entry.pending_pr_branch or entry.iteration_branch
        if not branch or not entry.iteration_commit_sha:
            continue
        exists = _branch_pr_exists(
            cfg, branch, entry.iteration_commit_sha
        )
        if exists is None:
            continue
        if exists:
            if entry.pending_pr_branch:
                if clear_pending_pr_check(
                    cfg,
                    issue_num,
                    branch,
                    expected_verdict_ts=entry.verdict_ts,
                ):
                    resolved += 1
            continue
        n = _requeue_refined_issue(
            cfg,
            branch,
            landed=False,
            expected_verdict_ts=entry.verdict_ts,
            expected_recovery_id=entry.recovery_id,
        )
        if n is not None:
            resolved += 1
            print(
                f"--- #{n} durable no-PR check recovered — requeued for "
                "re-review without another Aristotle session ---",
                flush=True,
            )
    return resolved


def _reap_fill_job(
    cfg: ReviewConfig,
    job: ConductorJob,
    rc: int,
    interrupted: bool,
    kept_worktrees: dict[int, str],
    not_before: dict[int, float],
) -> None:
    """Reap a finished FILL (prove) job — the exit ladder a proof needs.

    A fill proves the ``sorry`` bodies of an already-VERIFIED issue. Its
    rejection state (attempts / stall / the review-queue verdict) belongs
    to the human review pass, NOT to the prover, so this path deliberately
    calls NONE of ``record_iteration`` / ``record_failed_attempt`` /
    ``record_stall`` / ``_requeue_if_no_pr_landed`` (any of which would
    corrupt a verified issue or bounce a proven statement back into
    review). Retries are already capped at dispatch by
    ``max_fill_attempts``; here a failure just backs off so the next tick
    doesn't tight-loop, and a clean exit lets the automerge path land the
    proof PR. Whether the ``sorry`` is genuinely discharged is confirmed
    against the audit snapshot by discovery, not by this exit code."""
    if interrupted:
        job.status = "interrupted"
        kept_worktrees[_kept_worktree_key(job.issue_num, fill=True)] = job.worktree
        print(f"    worktree kept (fill interrupted): {job.worktree}", flush=True)
        return
    if rc == 0:
        job.status = "succeeded"
        _remove_worktree(cfg.repo_dir, Path(job.worktree))
        # Ran with --no-metadata-commit; regenerate formalization.yaml centrally
        # (same completion hook as a clean refine).
        _regenerate_metadata_after_success(cfg)
        return
    if rc == EXIT_PUBLICATION_PENDING:
        job.status = "publication_pending"
        kept_worktrees[_kept_worktree_key(job.issue_num, fill=True)] = job.worktree
        _clear_worktree_pid_marker(job.worktree)
        not_before[job.issue_num] = _now() + PUBLISH_RETRY_BACKOFF_SECONDS
        print(
            f"    autonomous job #{job.issue_num} has a clean committed fix "
            f"awaiting publication; kept worktree, retrying push/PR only in "
            f"{PUBLISH_RETRY_BACKOFF_SECONDS}s: {job.worktree}",
            flush=True,
        )
        return
    # Non-zero: keep the worktree for debugging and back off (bounded by the
    # dispatch-time attempt cap). No rejection-state mutation.
    job.status = "failed"
    kept_worktrees[_kept_worktree_key(job.issue_num, fill=True)] = job.worktree
    _clear_worktree_pid_marker(job.worktree)  # process gone → drop the marker
    not_before[job.issue_num] = _now() + FILL_RETRY_BACKOFF_SECONDS
    print(
        f"    fill #{job.issue_num} exited {rc}; worktree kept, backing off "
        f"{FILL_RETRY_BACKOFF_SECONDS}s: {job.worktree}",
        flush=True,
    )
    if rc == EXIT_AUTO_PR_BRANCH_ABORT:
        print(
            f"⚠️  fill #{job.issue_num} aborted on a GIT AUTH failure — run "
            f"`gh auth setup-git`; retries will keep failing until fixed.",
            flush=True,
        )


#: Per-issue dispatch backoff after a CONFIG-error exit (missing API key etc.).
#: Long on purpose: the environment won't fix itself, and the attempt-free
#: config path would otherwise re-dispatch (and fast-fail) every tick.
CONFIG_ERROR_BACKOFF_SECONDS = 3600.0


def _job_log_tail(job: ConductorJob, max_lines: int = 12) -> Optional[str]:
    """Last non-empty lines of the job's conductor-*.log (best-effort) —
    captured into ``job.error`` so the snapshot/deck and the stall comment
    are self-diagnosing instead of pointing at a log file on the host."""
    try:
        logs = sorted(Path(job.workdir).glob("conductor-*.log"))
        if not logs:
            return None
        lines = [
            line for line in logs[-1].read_text(errors="replace").splitlines()
            if line.strip()
        ]
        return "\n".join(lines[-max_lines:]) or None
    except OSError:
        return None


def _job_head_sha(job: ConductorJob) -> Optional[str]:
    """Current committed HEAD of a finished job worktree, if readable."""
    cp = _git(Path(job.worktree), "rev-parse", "HEAD")
    if cp.returncode != 0:
        return None
    value = (cp.stdout or "").strip()
    return value or None


def _reap_finished(
    cfg: ReviewConfig,
    jobs: list[ConductorJob],
    not_before: dict[int, float],
    kept_worktrees: dict[int, str],
    max_attempts: int,
    *,
    reconcile: bool = True,
    fill_attempts: Optional[dict[int, int]] = None,
    repair_attempts: Optional[dict[int, int]] = None,
) -> int:
    """Poll running jobs; apply the Phase-0 state machine to each exit.

    The decision table is ``daemon._handle_refine_exit`` verbatim
    (clean exit → record_iteration; non-zero → record_failed_attempt
    with backoff, then record_stall + one notification at the budget;
    interrupted → record NOTHING). A returned backoff becomes a
    per-issue ``not_before`` so other chapters keep dispatching.
    Returns the number of jobs reaped this tick."""
    reaped = 0
    for job in jobs:
        if job.status != "running":
            continue
        if job.proc is None:
            # ADOPTED job (inherited from a previous conductor — we didn't
            # spawn it, so there's no Popen to poll). Track the detached group,
            # not only its leader: lake/git descendants can briefly outlive a
            # worker that died abruptly and still own the worktree.
            if _job_process_group_live(job.pid, job.pgid, job.worktree):
                _refresh_job_runtime(job)
                continue
            reaped += 1
            _reap_adopted_job(cfg, job, kept_worktrees, reconcile=reconcile)
            continue
        rc = job.proc.poll()
        if rc is None:
            _refresh_job_runtime(job)
            continue
        if _process_group_alive(job.pgid):
            # The session leader exited but a descendant still belongs to the
            # worker group. Never clean/reuse its worktree until the group is
            # empty; the same row remains running and is checked next tick.
            _refresh_job_runtime(job)
            continue
        reaped += 1
        job.exit_code = rc
        job.finished_ts = now_iso()
        if job.log_handle is not None:
            try:
                job.log_handle.close()
            except Exception:  # noqa: BLE001 — never let cleanup kill the loop
                pass
            job.log_handle = None
        _refresh_job_runtime(job)
        # Mirror the daemon exactly: a non-zero exit after a stop signal
        # reflects the kill (user pausing work), not a refine failure.
        interrupted = _STOP_REQUESTED and rc != 0
        if rc != 0 and not interrupted:
            job.error = _job_log_tail(job)
        # A FILL (prove) job acts on a VERIFIED issue whose rejection state it
        # does NOT own, so it must NEVER run the refine exit ladder
        # (record_iteration / record_failed_attempt / record_stall /
        # requeue-for-rereview would corrupt the verified state or bounce a
        # proven statement back into review). Its retries are bounded at
        # dispatch by `max_fill_attempts`; the automerge path lands a clean
        # proof PR. Discharge is confirmed against the audit snapshot elsewhere.
        if job.fill or job.repair:
            # Both are AUTONOMOUS jobs with no rejection state to own — a fill
            # (→ prover branch) and a repair (→ main) are reaped identically:
            # never touch the review verdict ladder; the automerge + the
            # in-pipeline gates (build/axiom/faithfulness) decide what lands.
            if rc in {EXIT_PUBLICATION_PENDING, EXIT_REATTACH_TRANSIENT}:
                # These retries perform no new Aristotle work. Do not let
                # transient network publication/reattach failures consume the
                # autonomous dispatch budget.
                if job.fill and fill_attempts is not None:
                    fill_attempts[job.issue_num] = max(
                        0, fill_attempts.get(job.issue_num, 0) - 1
                    )
                if job.repair and repair_attempts is not None:
                    repair_attempts[job.chapter] = max(
                        0, repair_attempts.get(job.chapter, 0) - 1
                    )
            _reap_fill_job(cfg, job, rc, interrupted, kept_worktrees, not_before)
            continue
        if rc == EXIT_PUBLICATION_PENDING and not interrupted:
            # Clean Aristotle/build/commit; only the final network publication
            # failed.  Do NOT run the failure ladder (no attempt/stall), and do
            # not remove/reset the only committed copy.  The paired workdir's
            # RefineState drives a publication-only fast path on redispatch.
            job.status = "publication_pending"
            kept_worktrees[job.issue_num] = job.worktree
            _clear_worktree_pid_marker(job.worktree)
            not_before[job.issue_num] = _now() + PUBLISH_RETRY_BACKOFF_SECONDS
            print(
                f"    refine #{job.issue_num} committed cleanly but publication "
                f"is pending; worktree kept and push/PR-only retry scheduled in "
                f"{PUBLISH_RETRY_BACKOFF_SECONDS}s: {job.worktree}",
                flush=True,
            )
            continue
        if (
            job.recovery_mode == RECOVERY_RETRY_BUILD
            and rc != 0
            and not interrupted
        ):
            # A deterministic rebuild has already published red (or safely
            # aborted on a changed SHA). Re-running it three times cannot
            # improve code; return the issue to the recovery chooser so the
            # next rung can preserve and continue the PR.
            stalled = record_stall(
                cfg,
                job.issue_num,
                expected_verdict_ts=job.expected_verdict_ts,
                expected_recovery_id=job.expected_recovery_id,
            )
            job.status = "stalled" if stalled is not None else "superseded"
            _clear_worktree_pid_marker(job.worktree)
            _remove_worktree(cfg.repo_dir, Path(job.worktree))
            if stalled is not None:
                print(
                    f"    build-only recovery for #{job.issue_num} did not pass; "
                    "parked for the next explicit recovery choice",
                    flush=True,
                )
            else:
                print(
                    f"    build-only recovery for #{job.issue_num} was "
                    "superseded by newer review state; stale completion ignored",
                    flush=True,
                )
            continue
        backoff = daemon._handle_refine_exit(
            cfg, job.issue_num, rc, max_attempts=max_attempts,
            interrupted=interrupted, failure_detail=job.error,
            expected_verdict_ts=job.expected_verdict_ts,
            expected_recovery_id=job.expected_recovery_id,
            pending_pr_branch=(job.branch if rc == 0 and reconcile else None),
            # Branch/SHA ownership is durable completion evidence, not an
            # optional reconciliation side effect.  In particular, an exact
            # retry-build reaped under ``--no-reconcile`` must still be
            # mergeable by a later normal conductor.
            iteration_branch=(job.branch if rc == 0 else None),
            iteration_commit_sha=(_job_head_sha(job) if rc == 0 else None),
        )
        if backoff:
            not_before[job.issue_num] = _now() + backoff
        if interrupted:
            job.status = "interrupted"
            kept_worktrees[job.issue_num] = job.worktree
            # The job process is gone; drop its live-job marker so a later
            # conductor's reap guard doesn't mistake a reused pid for it.
            _clear_worktree_pid_marker(job.worktree)
            print(f"    worktree kept (interrupted): {job.worktree}", flush=True)
        elif rc == 0:
            job.status = "succeeded"
            # B0c: a refine that exits clean but opened NO PR (typically no diff →
            # nothing to commit/PR) would leave the issue stuck 'rejected' forever
            # — iterated, but nothing to merge, so no requeue fires and the deck
            # blocks it from review (the #98 symptom). Requeue it so it returns to
            # the review feed instead of vanishing.
            if reconcile:
                try:
                    _requeue_if_no_pr_landed(cfg, job)
                except Exception as exc:  # noqa: BLE001 — never crash a reap
                    print(f"  no-PR requeue check failed: {exc}", flush=True)
            _remove_worktree(cfg.repo_dir, Path(job.worktree))
            # Completion hook: the job ran with --no-metadata-commit, so
            # formalization.yaml is regenerated here, centrally.
            _regenerate_metadata_after_success(cfg)
        else:
            entry = load_state(cfg).issues.get(job.issue_num)
            job.status = (
                "stalled" if entry is not None and entry.status == "stalled"
                else "failed"
            )
            kept_worktrees[job.issue_num] = job.worktree
            # The job process is gone; drop its live-job marker so a later
            # conductor's reap guard doesn't mistake a reused pid for it (the
            # marker's whole purpose is protecting a LIVE job).
            _clear_worktree_pid_marker(job.worktree)
            print(
                f"    worktree kept for debugging (#{job.issue_num} "
                f"{job.status}, exit {rc}): {job.worktree}",
                flush=True,
            )
            # A stall is the moment the pipeline starts waiting on a human
            # decision; push it to the operator instead of hoping they poll.
            if job.status == "stalled":
                notify(
                    "Marathon conductor",
                    f"#{job.issue_num} stalled (exit {rc}) — choose a "
                    "recovery path in the deck.",
                )
            # Distinct, top-level warning for the distinguished
            # EXIT_AUTO_PR_BRANCH_ABORT code. Two distinct causes share it:
            # a git-AUTH/branch-prep abort (credentials) and the recovery
            # moved-head refusal ("remote branch changed after recovery was
            # requested"). The latter must PARK the recovery — retrying the
            # same stale pin is doomed and each retry burns an attempt — and
            # must not tell the operator to fix credentials that are fine.
            if rc == EXIT_AUTO_PR_BRANCH_ABORT:
                tail = _job_log_tail(job) or ""
                if "remote branch changed after recovery was requested" in tail:
                    print(
                        f"⚠️  refine #{job.issue_num} refused a recovery "
                        f"branch whose remote head moved after the recovery "
                        f"was chosen; parking for a refreshed recovery "
                        f"choice instead of replaying the stale pin.",
                        flush=True,
                    )
                    if _park_deferred_recovery(cfg, job.issue_num):
                        notify(
                            "Marathon conductor",
                            f"#{job.issue_num} recovery parked: the branch "
                            "moved after recovery was chosen — reopen "
                            "Recover to re-pin.",
                        )
                else:
                    print(
                        f"⚠️  refine #{job.issue_num} aborted on a GIT AUTH "
                        f"failure — run `gh auth setup-git`; retries will "
                        f"keep failing until this is fixed.",
                        flush=True,
                    )
            # Config error (missing API key etc.): the exit ladder burned no
            # attempt (by design), so back this issue off HERE — otherwise the
            # loop would re-dispatch a doomed job every tick until the
            # operator fixes the environment.
            if rc == EXIT_CONFIG_ERROR:
                not_before[job.issue_num] = _now() + CONFIG_ERROR_BACKOFF_SECONDS
                print(
                    f"⚠️  refine #{job.issue_num} aborted on a CONFIGURATION "
                    f"error (e.g. ARISTOTLE_API_KEY unset) — no attempt "
                    f"burned; this issue backed off "
                    f"{int(CONFIG_ERROR_BACKOFF_SECONDS)}s. Fix the "
                    f"environment and restart the conductor.",
                    flush=True,
                )
    return reaped


# --- Orphan reconciliation -----------------------------------------------------------


def _known_project_ids(repo_dir: Path) -> set[str]:
    """Aristotle project ids referenced by the jobs snapshot (previous
    runs' jobs survive conductor restarts as live subprocesses) plus a
    fresh read of each snapshot job's refine checkpoint."""
    known: set[str] = set()
    snap = load_jobs_snapshot(repo_dir)
    for raw in (snap or {}).get("jobs", []):
        if raw.get("project_id"):
            known.add(raw["project_id"])
        workdir = raw.get("workdir")
        if workdir:
            try:
                st = load_refine_state(Path(workdir) / REFINE_STATE_FILENAME)
            except Exception:  # noqa: BLE001 — stale/corrupt checkpoint
                st = None
            if st is not None and st.project_id:
                known.add(st.project_id)
    return known


def _report_orphans(cfg: ReviewConfig, known_ids: set[str]) -> None:
    """Startup reconciliation: list Aristotle's in-flight projects and
    print any not referenced by current jobs/state. REPORT-ONLY — the
    conductor never cancels (binding; an orphan is usually a manual run
    or a job from before a restart, both of which must finish). Every
    failure path warns and continues: a flaky API must not block
    dispatching."""
    try:
        from aristotlelib import Project, ProjectStatus
    except Exception as e:  # noqa: BLE001 — import-guarded by contract
        print(f"  warning: orphan reconciliation skipped (aristotlelib unavailable: {e})")
        return
    statuses = [
        getattr(ProjectStatus, name)
        for name in _IN_FLIGHT_PROJECT_STATUS_NAMES
        if hasattr(ProjectStatus, name)
    ]
    if not statuses:
        print(
            "  warning: orphan reconciliation skipped (no in-flight "
            "ProjectStatus values in this aristotlelib version)"
        )
        return
    try:
        projects, _ = asyncio.run(Project.list_projects(limit=100, status=statuses))
    except Exception as e:  # noqa: BLE001 — network/auth must not block startup
        print(f"  warning: orphan reconciliation skipped (list_projects failed: {e})")
        return
    orphans = [
        p for p in projects
        if getattr(p, "project_id", None) and p.project_id not in known_ids
    ]
    if not orphans:
        print(
            f"orphan reconciliation: {len(projects)} in-flight project(s), "
            "all referenced by current jobs/state"
        )
        return
    print(
        f"orphan reconciliation: {len(orphans)} in-flight Aristotle "
        "project(s) NOT referenced by current jobs/state "
        "(report-only — never canceled):"
    )
    for p in orphans:
        print(f"  - {p.project_id}")


# --- jobs.json snapshot ----------------------------------------------------------------


def jobs_snapshot_path(repo_dir: Path) -> Path:
    return Path(repo_dir) / JOBS_SNAPSHOT_RELPATH


def conductor_heartbeat_path(repo_dir: Path) -> Path:
    return Path(repo_dir) / CONDUCTOR_HEARTBEAT_RELPATH


def load_conductor_heartbeat(repo_dir: Path) -> Optional[dict]:
    """Return the conductor readiness heartbeat, or ``None`` if unavailable."""
    try:
        data = json.loads(conductor_heartbeat_path(repo_dir).read_text())
    except (OSError, ValueError, AttributeError):
        return None
    return data if isinstance(data, dict) else None


class _ConductorHeartbeat:
    """Independent readiness heartbeat for the long-running conductor.

    Its first synchronous write is also a startup permission preflight. A host
    process that cannot write the consumer repo fails before the deck reports
    it ready, instead of pairing a live PID with an old jobs snapshot.
    """

    def __init__(self, repo_dir: Path):
        self.repo_dir = Path(repo_dir)
        self.pid = os.getpid()
        self.ready = False
        self._stop = threading.Event()
        self._write_lock = threading.Lock()
        self._thread: Optional[threading.Thread] = None

    def _write(self) -> None:
        # mark_ready() can coincide with the background tick. Serialize the
        # shared atomic temp path so one writer cannot replace it out from
        # underneath the other.
        with self._write_lock:
            path = conductor_heartbeat_path(self.repo_dir)
            path.parent.mkdir(parents=True, exist_ok=True)
            gitignore = path.parent / ".gitignore"
            if not gitignore.is_file():
                gitignore.write_text("*\n")
            payload = {
                "schema_version": 1,
                "pid": self.pid,
                "ready": self.ready,
                "updated_ts": now_iso(),
            }
            tmp = path.with_name(f".{path.name}.{self.pid}.tmp")
            tmp.write_text(json.dumps(payload, indent=2) + "\n")
            tmp.replace(path)

    def start(self) -> None:
        # Synchronous by design: inability to publish runtime state is a hard
        # startup failure, not a warning hidden behind a green PID.
        self._write()
        self._thread = threading.Thread(
            target=self._run,
            name="marathon-conductor-heartbeat",
            daemon=True,
        )
        self._thread.start()

    def mark_ready(self) -> None:
        self.ready = True
        self._write()

    def _run(self) -> None:
        while not self._stop.wait(CONDUCTOR_HEARTBEAT_INTERVAL_SECONDS):
            try:
                self._write()
            except OSError as exc:
                # The last timestamp ages out and the deck turns unhealthy.
                # The main loop retains ownership of running jobs and surfaces
                # its own write failure without cancelling an Aristotle run.
                print(
                    f"  warning: conductor heartbeat write failed ({exc})",
                    flush=True,
                )

    def stop(self) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=1)
        path = conductor_heartbeat_path(self.repo_dir)
        try:
            current = load_conductor_heartbeat(self.repo_dir)
            if current and current.get("pid") == self.pid:
                path.unlink(missing_ok=True)
        except OSError:
            pass


#: The deck's web toggle for proving — a control file the conductor polls each
#: tick. Lives in the self-ignoring .marathon/conductor/ runtime dir so it never
#: leaks into an Aristotle bundle or dirties `git status`.
PROVER_CONTROL_RELPATH = Path(".marathon/conductor/prover-control.json")
# Durable (gitignored) HEAD cursor for declaration reconciliation.  Keeping it
# only in a local variable lost the old..new diff window on every conductor
# restart, exactly when a rename/split/delete most needs provenance.
RECONCILE_CURSOR_RELPATH = Path(".marathon/conductor/reconcile-cursor.json")


def prover_control_path(repo_dir: Path) -> Path:
    return Path(repo_dir) / PROVER_CONTROL_RELPATH


def reconcile_cursor_path(repo_dir: Path) -> Path:
    return Path(repo_dir) / RECONCILE_CURSOR_RELPATH


def load_reconcile_cursor(repo_dir: Path) -> Optional[str]:
    """Last fully-reconciled HEAD, preserved across conductor restarts."""
    try:
        data = json.loads(reconcile_cursor_path(repo_dir).read_text())
        value = data.get("head")
        return value if isinstance(value, str) and value.strip() else None
    except (OSError, ValueError, AttributeError):
        return None


def save_reconcile_cursor(repo_dir: Path, head: str) -> None:
    """Atomically persist a successfully reconciled HEAD (best-effort)."""
    path = reconcile_cursor_path(repo_dir)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        gitignore = path.parent / ".gitignore"
        if not gitignore.is_file():
            gitignore.write_text("*\n")
        tmp = path.with_suffix(path.suffix + ".tmp")
        tmp.write_text(json.dumps({"head": head, "updated_ts": now_iso()}, indent=2) + "\n")
        tmp.replace(path)
    except OSError as exc:
        print(f"  warning: could not persist reconcile cursor ({exc})", flush=True)


def reconcile_cursor_is_ancestor(repo_dir: Path, old: str, current: str) -> bool:
    """Whether ``old..current`` is a valid forward reconciliation window.

    Persisted SHAs can outlive rebases and branch switches.  Diffing from an
    unrelated endpoint can manufacture bogus rename/split evidence, so require
    the old object to remain a commit and an ancestor of the current HEAD.
    """
    if _git(repo_dir, "cat-file", "-e", f"{old}^{{commit}}").returncode != 0:
        return False
    return _git(
        repo_dir, "merge-base", "--is-ancestor", old, current,
    ).returncode == 0


def discard_reconcile_cursor(repo_dir: Path) -> None:
    """Best-effort removal of a stale/unrelated persisted cursor."""
    try:
        reconcile_cursor_path(repo_dir).unlink(missing_ok=True)
    except OSError:
        pass


def read_prover_control(repo_dir: Path) -> Optional[bool]:
    """The deck's prover override: ``True``/``False`` when the web app has set
    one, else ``None`` (no override → honor the conductor's startup default).
    Never raises — an unreadable/malformed control file reads as no override."""
    try:
        data = json.loads(prover_control_path(repo_dir).read_text())
        val = data.get("enabled")
        return val if isinstance(val, bool) else None
    except (OSError, ValueError, AttributeError):
        return None


def set_prover_enabled(repo_dir: Path, enabled: bool) -> None:
    """Write the prover override (atomic tmp+rename; self-ignoring runtime dir) —
    the deck's channel to enable/disable proving on a SEPARATE conductor process,
    honored within one tick (``TICK_SECONDS``)."""
    path = prover_control_path(repo_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    gitignore = path.parent / ".gitignore"
    if not gitignore.is_file():
        gitignore.write_text("*\n")
    payload = {"enabled": bool(enabled), "updated_ts": now_iso(), "updated_by": "deck"}
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(payload, indent=2) + "\n")
    tmp.replace(path)


def _dispatch_env_error() -> Optional[str]:
    """A human-readable reason refine/fill/repair dispatch is impossible in
    this environment, or None. Checked once at conductor startup (a process
    env never changes underneath it): dispatching without the Aristotle key
    burns doomed subprocesses and, before EXIT_CONFIG_ERROR existed, whole
    attempt budgets."""
    if not os.environ.get("ARISTOTLE_API_KEY"):
        return (
            "ARISTOTLE_API_KEY is not set — refine/fill/repair dispatch is "
            "DISABLED (exact-SHA build recovery, automerge, and reconcile "
            "still run). Export the key and restart the conductor."
        )
    return None


def write_jobs_snapshot(
    repo_dir: Path, jobs: list[ConductorJob], concurrency: int,
    prover_enabled: Optional[bool] = None,
    degraded: Optional[str] = None,
    conductor_pid: Optional[int] = None,
) -> None:
    """Atomic-ish write (tmp + rename, like review.state.save_state) so
    ``marathon conductor status`` never reads a torn snapshot. ``prover_enabled``
    publishes the conductor's EFFECTIVE proving state (startup default overlaid
    with the deck toggle) so the deck can render the toggle in its true state.
    ``degraded`` publishes a dispatch-disabling environment problem (e.g. no
    Aristotle key) so the deck can show a red banner instead of a healthy
    'conductor running' chip."""
    path = jobs_snapshot_path(repo_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    # Self-ignoring runtime dir (same convention as .marathon/review's
    # runner-locks/): the snapshot is droppable runtime state living
    # inside the consumer repo, and an untracked-not-ignored jobs.json
    # would (a) leak into Aristotle bundles from the primary checkout
    # (skeleton/refine bundle untracked-not-gitignored files) and
    # (b) permanently dirty `git status --porcelain`, deferring the
    # metadata completion hook forever.
    gitignore = path.parent / ".gitignore"
    if not gitignore.is_file():
        gitignore.write_text("*\n")
    payload = {
        "schema_version": SNAPSHOT_SCHEMA_VERSION,
        "updated_ts": now_iso(),
        "conductor_pid": conductor_pid,
        "concurrency": concurrency,
        "prover_enabled": prover_enabled,
        "degraded": degraded,
        "jobs": [job.to_json() for job in jobs],
    }
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(payload, indent=2) + "\n")
    tmp.replace(path)


def load_jobs_snapshot(repo_dir: Path) -> Optional[dict]:
    """Returns the raw snapshot dict, or None if absent/unparseable
    (warned, non-fatal — the snapshot is droppable runtime state)."""
    path = jobs_snapshot_path(repo_dir)
    if not path.is_file():
        return None
    try:
        data = json.loads(path.read_text())
    except json.JSONDecodeError as e:
        print(f"  warning: {path} unparseable ({e}); ignoring snapshot")
        return None
    if not isinstance(data.get("jobs"), list):
        data["jobs"] = []
    return data


def print_status(repo_dir: Path) -> int:
    """``marathon conductor status``: render the snapshot table without
    touching (or requiring) a running conductor. Returns an exit code."""
    snap = load_jobs_snapshot(repo_dir)
    if snap is None:
        print(
            f"no conductor snapshot at {jobs_snapshot_path(repo_dir)}; "
            "run `marathon conductor run` first"
        )
        return 1
    print(
        f"{jobs_snapshot_path(repo_dir)} — updated {snap.get('updated_ts', '?')}, "
        f"concurrency {snap.get('concurrency', '?')}"
    )
    jobs = [ConductorJob.from_json(raw) for raw in snap["jobs"]]
    if not jobs:
        print("  no jobs recorded")
        return 0
    for job in jobs:
        status = job.status
        if (
            status == "running"
            and job.pid is not None
            and not daemon.process_alive(job.pid)
        ):
            # The state machine recorded nothing for it (daemon
            # semantics), so the next conductor run re-dispatches.
            status = "running (pid dead)"
        aristotle = job.project_id or "-"
        if job.aristotle_status:
            aristotle += f" ({job.aristotle_status})"
        print(
            f"  #{job.issue_num:<5} c{job.chapter:<3} {status:<20} "
            f"pid={job.pid if job.pid is not None else '-':<8} "
            f"started={job.started_ts or '-'}  aristotle={aristotle}"
        )
        print(f"        worktree={job.worktree}")
        print(f"        workdir ={job.workdir}")
    return 0


# --- Main loop ----------------------------------------------------------------------


#: Minimum spacing between auto re-audits — an audit is a full (incremental)
#: build, so this bounds the cost while keeping the snapshot near-fresh.
AUTO_AUDIT_MIN_INTERVAL_SECONDS = 900.0
#: A dead/incomplete full audit of one exact revision is expensive enough that
#: restarting the conductor must not immediately launch it again.  This is
#: deliberately longer than the in-process throttle: guard metadata survives a
#: restart, while a different HEAD bypasses the cooldown entirely.
AUTO_AUDIT_FAILED_ATTEMPT_COOLDOWN_SECONDS = 6 * 60 * 60.0
# Stable inode whose inherited POSIX flock is the auto-audit single-flight
# lease.  The file itself intentionally survives process/machine restarts; only
# the kernel lock is authoritative for liveness.  Once that lock is free, an
# exact recent same-HEAD attempt recorded on the inode may impose the bounded
# failed-attempt cooldown above; stale or unvalidated contents never do.
AUTO_AUDIT_GUARD_RELPATH = Path(".marathon/conductor/auto-audit.guard")
AUTO_AUDIT_GUARD_SCHEMA_VERSION = 2
AUTO_AUDIT_GUARD_MAX_BYTES = 32 * 1024
AUTO_AUDIT_GUARD_MAX_CLOCK_SKEW_SECONDS = 5 * 60.0
_AUTO_AUDIT_READABLE_GUARD_SCHEMAS = frozenset({1, 2})
# The public loop state retains the last successful audit revision separately
# from ``last_time``.  Remember which revision actually set that retry clock so
# an unreadable guard record can neither erase the same-HEAD throttle nor make
# a newly landed HEAD inherit the prior revision's delay.
_AUTO_AUDIT_RECENT_TIMED_HEADS: dict[tuple[str, str], str] = {}


def auto_audit_guard_path(repo_dir: str | Path) -> Path:
    return Path(repo_dir) / AUTO_AUDIT_GUARD_RELPATH


def _auto_audit_identity(
    repo_dir: str | Path, target: str,
) -> tuple[str, str]:
    """Canonical in-process identity for one repository audit target."""
    return (
        str(Path(repo_dir).expanduser().resolve(strict=False)),
        target,
    )


def _auto_audit_command(
    repo_dir: str | Path, target: str,
) -> list[str]:
    """The one exact argv emitted for an automatic audit."""
    canonical_repo, _ = _auto_audit_identity(repo_dir, target)
    return [
        sys.executable, "-m", "marathon", "audit", "run",
        "--target", target, "--repo-dir", canonical_repo,
    ]


def _try_acquire_auto_audit_guard(
    repo_dir: str | Path,
) -> tuple[Optional[object], str]:
    """Acquire the restart-safe auto-audit lease without waiting.

    ``flock`` belongs to the open file description inherited by the audit
    child.  The conductor closes only its duplicate after ``Popen``; the lease
    therefore remains held until that exact child exits, even if the conductor
    is killed and relaunched.  A stale file or PID is harmless because kernel
    lock state, not file contents, decides ownership.

    Returns ``(handle, "acquired")``, ``(None, "busy")``, or
    ``(None, "unsupported"/"error: ...")``.  Callers must close an acquired
    handle without explicitly unlocking it after a successful spawn.  Callers
    may inspect bounded, validated metadata only *after* this returns an
    acquired handle; metadata is never a substitute for the kernel lease.
    """
    try:
        import fcntl
    except ImportError:  # pragma: no cover - Windows fails closed
        return None, "unsupported"

    path = auto_audit_guard_path(repo_dir)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        gitignore = path.parent / ".gitignore"
        if not gitignore.exists():
            gitignore.write_text("*\n")
        handle = open(path, "a+b")
    except OSError as exc:
        return None, f"error: {exc}"
    try:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        handle.close()
        return None, "busy"
    except OSError as exc:
        handle.close()
        return None, f"error: {exc}"
    return handle, "acquired"


def _write_auto_audit_guard_metadata(handle: object, payload: dict) -> None:
    """Best-effort attempt metadata on the stable lock inode.

    This metadata is never treated as a lease: it legitimately outlives its
    process.  After the kernel proves that no child owns the inode, a strictly
    validated exact-identity record may impose only the bounded retry cooldown;
    it also keeps production incidents easy to inspect.
    """
    try:
        handle.seek(0)
        handle.truncate()
        handle.write(
            (json.dumps(payload, ensure_ascii=False, indent=2) + "\n").encode()
        )
        handle.flush()
        os.fsync(handle.fileno())
    except (AttributeError, OSError, TypeError, ValueError):
        pass


@dataclass(frozen=True)
class _AutoAuditGuardAttempt:
    """Validated, non-live diagnostics from one completed audit lease."""

    head: str
    attempted_at: float


def _is_exact_auto_audit_guard_command(
    command: object, *, repo_dir: str | Path, target: str,
) -> bool:
    """Validate the exact argv shape written by :func:`_maybe_auto_audit`.

    The process-table scanner intentionally accepts harmless extra argv so it
    can recognize legacy workers.  Durable guard metadata has a stricter trust
    boundary: duplicate or unknown options must fail open rather than letting
    an operator-edited record suppress work for six hours.
    """
    if (
        not isinstance(command, list)
        or len(command) != 9
        or any(
            type(token) is not str or not token or len(token) > 4096
            for token in command
        )
        or sum(len(token) for token in command) > 16 * 1024
    ):
        return False
    try:
        expected = _auto_audit_command(repo_dir, target)
    except (OSError, RuntimeError):
        return False
    return command == expected


def _read_auto_audit_guard_attempt(
    handle: object,
    *,
    repo_dir: str | Path,
    target: str,
    now: float,
) -> Optional[_AutoAuditGuardAttempt]:
    """Read one bounded, strictly validated attempt record from a free guard."""
    try:
        handle.seek(0)
        raw = handle.read(AUTO_AUDIT_GUARD_MAX_BYTES + 1)
    except (AttributeError, OSError, ValueError):
        return None
    if (
        not isinstance(raw, (bytes, bytearray))
        or not raw
        or len(raw) > AUTO_AUDIT_GUARD_MAX_BYTES
    ):
        return None
    try:
        payload = json.loads(bytes(raw).decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError, TypeError, ValueError):
        return None
    if not isinstance(payload, dict):
        return None

    schema = payload.get("schema_version")
    if type(schema) is not int or schema not in _AUTO_AUDIT_READABLE_GUARD_SCHEMAS:
        return None
    required = {
        "schema_version", "status", "launcher_pid", "pid", "head",
        "target", "repo_dir", "launched_at", "command",
    }
    allowed = set(required)
    if schema == 2:
        required.add("launched_at_epoch")
        allowed.add("launched_at_epoch")
    if not required.issubset(payload) or not set(payload).issubset(allowed):
        return None

    status = payload.get("status")
    launcher_pid = payload.get("launcher_pid")
    child_pid = payload.get("pid")
    if status not in {"launching", "running"}:
        return None
    if type(launcher_pid) is not int or launcher_pid <= 0:
        return None
    if status == "launching":
        if child_pid is not None:
            return None
    elif type(child_pid) is not int or child_pid <= 0:
        return None

    try:
        expected_repo = str(
            Path(repo_dir).expanduser().resolve(strict=False)
        )
    except (OSError, RuntimeError):
        return None
    head = payload.get("head")
    bounded_identity = (
        (head, 256),
        (payload.get("target"), 4096),
        (payload.get("repo_dir"), 4096),
    )
    for actual, limit in bounded_identity:
        if type(actual) is not str or not actual or len(actual) > limit:
            return None
    if payload.get("target") != target or payload.get("repo_dir") != expected_repo:
        return None

    launched_at = payload.get("launched_at")
    if type(launched_at) is not str or not launched_at or len(launched_at) > 128:
        return None
    try:
        launched_dt = datetime.fromisoformat(launched_at.replace("Z", "+00:00"))
        if launched_dt.tzinfo is None:
            return None
        launched_iso_epoch = launched_dt.timestamp()
    except (OSError, OverflowError, ValueError):
        return None

    if not _is_exact_auto_audit_guard_command(
        payload.get("command"), repo_dir=repo_dir, target=target
    ):
        return None

    attempted_at = launched_iso_epoch
    if schema == 2:
        epoch = payload.get("launched_at_epoch")
        if (
            isinstance(epoch, bool)
            or not isinstance(epoch, (int, float))
            or not math.isfinite(float(epoch))
            or float(epoch) < 0
        ):
            return None
        attempted_at = float(epoch)
        if abs(attempted_at - launched_iso_epoch) > (
            AUTO_AUDIT_GUARD_MAX_CLOCK_SKEW_SECONDS
        ):
            return None
    if (
        isinstance(now, bool)
        or not isinstance(now, (int, float))
        or not math.isfinite(float(now))
        or float(now) < 0
    ):
        return None
    now = float(now)
    if attempted_at > now + AUTO_AUDIT_GUARD_MAX_CLOCK_SKEW_SECONDS:
        return None
    return _AutoAuditGuardAttempt(head=head, attempted_at=attempted_at)


def _command_option(tokens: list[str], name: str) -> Optional[str]:
    """Return one command option in either ``--x value`` or ``--x=value`` form."""
    for index, token in enumerate(tokens):
        if token == name and index + 1 < len(tokens):
            return tokens[index + 1]
        prefix = name + "="
        if token.startswith(prefix):
            return token[len(prefix):]
    return None


def _is_exact_auto_audit_command(
    command: str, *, repo_dir: str | Path, target: str,
) -> bool:
    """Whether a process command is this repo's exact Marathon audit worker."""
    try:
        tokens = shlex.split(command)
    except ValueError:
        return False
    module_index = next(
        (
            index
            for index in range(1, len(tokens) - 3)
            if tokens[index:index + 4] == ["-m", "marathon", "audit", "run"]
        ),
        None,
    )
    if module_index is None or "python" not in Path(
        tokens[module_index - 1]
    ).name.lower():
        return False
    command_repo = _command_option(tokens, "--repo-dir")
    command_target = _command_option(tokens, "--target")
    if command_repo is None or command_target is None:
        return False
    try:
        expected_repo = Path(repo_dir).expanduser().resolve(strict=False)
        actual_repo = Path(command_repo).expanduser().resolve(strict=False)
        expected_target = (expected_repo / target).resolve(strict=False)
        actual_target = (actual_repo / command_target).resolve(strict=False)
    except (OSError, RuntimeError):
        return False
    return actual_repo == expected_repo and actual_target == expected_target


def _live_legacy_auto_audit_pids(
    repo_dir: str | Path, target: str,
) -> Optional[list[int]]:
    """Exact live auto-audits launched before the inherited-lock protocol.

    This migration guard prevents the first upgraded conductor from launching
    beside a still-running legacy child.  Zombies are deliberately ignored:
    they no longer execute or own descendants, and a detached child's inherited
    flock is the durable identity for every launch made by the new protocol.
    ``None`` means process identity could not be inspected and therefore makes
    launch fail closed.
    """
    if os.name == "nt":  # pragma: no cover - no inherited ``pass_fds`` lease
        return None
    try:
        cp = subprocess.run(
            ["ps", "-axo", "pid=,state=,command="],
            capture_output=True,
            text=True,
            timeout=5,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if cp.returncode != 0:
        return None
    matches: list[int] = []
    for line in (cp.stdout or "").splitlines():
        fields = line.strip().split(maxsplit=2)
        if len(fields) != 3:
            continue
        try:
            pid = int(fields[0])
        except ValueError:
            continue
        state, command = fields[1], fields[2]
        if state.upper().startswith("Z"):
            continue
        if _is_exact_auto_audit_command(
            command, repo_dir=repo_dir, target=target
        ):
            matches.append(pid)
    return matches


def _audit_target_folder(cfg: ReviewConfig) -> Optional[str]:
    """The folder ``marathon audit run`` should target — the parent of the
    per-chapter path template (e.g. ``GeometricAnalysis/LeeSM`` for a template
    ``GeometricAnalysis/LeeSM/Chapter{chapter}``). ``None`` if not derivable."""
    tmpl = getattr(cfg, "target_path_template", None)
    if not tmpl:
        return None
    try:
        return str(Path(tmpl.format(chapter=1)).parent)
    except Exception:  # noqa: BLE001
        return None


def _persisted_audit_revision(cfg: ReviewConfig) -> Optional[str]:
    """The revision validated for every module in the auto-audit target.

    This is the conductor's restart-safe success cursor.  It is derived from
    the normal fail-closed snapshot view: exact audits and older evidence whose
    complete import closure is Git-proven byte-identical both count. It is
    never advanced merely because an audit process launched. Partial, stale,
    dirty, or legacy snapshots return ``None``.
    """
    target = _audit_target_folder(cfg)
    if target is None:
        return None
    try:
        from marathon.audit.engine import derive_modules, load_snapshot

        modules, failures = derive_modules(Path(cfg.repo_dir), target)
        if not modules or failures:
            return None
        snapshot = load_snapshot(cfg.repo_dir)
        if snapshot is None:
            return None
        revisions = {
            snapshot.module_revisions.get(module) for module in modules
        }
        if None in revisions or len(revisions) != 1:
            return None
        return next(iter(revisions))
    except Exception:  # noqa: BLE001 - cursor recovery is best-effort
        return None


def _has_uncommitted_lean(repo_dir: Path) -> bool:
    """True if the checkout has uncommitted ``.lean`` changes (operator mid-edit)
    — or we cannot tell. Used to SKIP an auto-audit that would otherwise race the
    operator's build or audit their half-finished work."""
    try:
        cp = subprocess.run(
            ["git", "status", "--porcelain"], cwd=str(repo_dir),
            capture_output=True, text=True, check=False,
        )
    except OSError:
        return True
    if cp.returncode != 0:
        return True
    return any(
        ln[3:].endswith(".lean") for ln in cp.stdout.splitlines() if ln.strip()
    )


def _maybe_auto_audit(
    cfg: ReviewConfig, last_head: Optional[str], last_time: float, *, _now_fn=_now,
) -> "tuple[Optional[str], float]":
    """Auto-refresh the audit snapshot when main has MOVED since our last audit
    and the tree is clean. Returns the updated ``(audited_head, last_time)``.

    WHY: the snapshot is the source of truth for review-queue readiness and
    repair discovery, but it only refreshes on an explicit ``marathon audit
    run``. So a change that lands upstream — most acutely a FROZEN decl's
    signature changing (e.g. Chapter 15 bundling ``hypersurfaceInducedOrientation``
    into an ``IsSmoothHypersurface`` arg) — can silently break a DOWNSTREAM
    consumer chapter (StokesTheorem), while the stale snapshot still reports it
    green. The break then hides: the review queue shows phantom-ready cards and
    the repair's own stale-olean check no-ops. Re-auditing on every landed change
    keeps the snapshot honest so a downstream break surfaces immediately.

    Non-blocking: spawns a detached ``marathon audit run`` (the snapshot updates
    when it finishes; the tick continues).  The child inherits a flock on a
    stable repo-scoped inode, so a conductor restart cannot launch a duplicate
    while the first audit is still alive.  Legacy pre-lock audit processes are
    adopted by exact command/repo/target identity until they exit. Throttled by
    :data:`AUTO_AUDIT_MIN_INTERVAL_SECONDS`, and skipped while the operator has
    uncommitted Lean work. Best-effort — never raises."""
    now = _now_fn()
    # Poll the durable result before the throttle.  A child that completed
    # since the previous tick advances the cursor immediately. A failed child
    # changes nothing; in-process retries use the normal interval, while a
    # restart observes the longer durable same-HEAD cooldown below.
    persisted = _persisted_audit_revision(cfg)
    if persisted is not None and persisted != last_head:
        last_head = persisted
    from marathon.deck.refresh import head_sha

    cur = head_sha(cfg.repo_dir)
    if cur is None or cur == last_head:
        return last_head, last_time  # HEAD unmoved / unknown — nothing to refresh
    target = _audit_target_folder(cfg)
    if target is None:
        return last_head, now
    try:
        audit_identity = _auto_audit_identity(cfg.repo_dir, target)
    except (OSError, RuntimeError):
        return last_head, now

    def remember_timed_head() -> None:
        _AUTO_AUDIT_RECENT_TIMED_HEADS[audit_identity] = cur

    guard, guard_status = _try_acquire_auto_audit_guard(cfg.repo_dir)
    if guard_status == "busy":
        # A child from this or a previous conductor still owns the inherited
        # lease.  Do not advance the retry clock: once it exits, the very next
        # tick may observe its snapshot or retry a failed run.
        return last_head, last_time
    if guard is None:
        # Without an inheritable kernel lease, a detached launch cannot be
        # restart-safe. Fail closed and throttle the warning/cost.
        if (
            now - last_time < AUTO_AUDIT_MIN_INTERVAL_SECONDS
            and _AUTO_AUDIT_RECENT_TIMED_HEADS.get(audit_identity) == cur
        ):
            return last_head, last_time
        print(
            f"  audit: auto-refresh skipped (single-flight guard "
            f"{guard_status})",
            flush=True,
        )
        remember_timed_head()
        return last_head, now
    try:
        prior_attempt = _read_auto_audit_guard_attempt(
            guard,
            repo_dir=cfg.repo_dir,
            target=target,
            now=now,
        )
        # The in-memory clock limits repeated work on one revision. A durable
        # record for a *different* revision makes the distinction explicit and
        # lets a newly-landed HEAD bypass that old revision's throttle.
        if (
            now - last_time < AUTO_AUDIT_MIN_INTERVAL_SECONDS
            and (
                _AUTO_AUDIT_RECENT_TIMED_HEADS.get(audit_identity) == cur
                or (
                    prior_attempt is not None
                    and prior_attempt.head == cur
                )
            )
        ):
            return last_head, last_time
        if _has_uncommitted_lean(cfg.repo_dir):
            # Operator mid-edit — skip, but reset this revision's throttle.
            remember_timed_head()
            return last_head, now

        cooldown_remaining = None
        if prior_attempt is not None and prior_attempt.head == cur:
            age = max(0.0, float(now) - prior_attempt.attempted_at)
            if age < AUTO_AUDIT_FAILED_ATTEMPT_COOLDOWN_SECONDS:
                cooldown_remaining = (
                    AUTO_AUDIT_FAILED_ATTEMPT_COOLDOWN_SECONDS - age
                )
        if cooldown_remaining is not None:
            retry_minutes = max(1, math.ceil(cooldown_remaining / 60.0))
            print(
                "  audit: auto-refresh deferred (this exact HEAD already had "
                f"an incomplete attempt; retry cooldown {retry_minutes}m)",
                flush=True,
            )
            remember_timed_head()
            return last_head, now

        legacy_pids = _live_legacy_auto_audit_pids(cfg.repo_dir, target)
        if legacy_pids is None:
            print(
                "  audit: auto-refresh deferred (could not verify existing "
                "audit process identity)",
                flush=True,
            )
            return last_head, last_time
        if legacy_pids:
            print(
                "  audit: existing pre-single-flight audit still running "
                f"(pid{'s' if len(legacy_pids) != 1 else ''} "
                f"{', '.join(str(pid) for pid in legacy_pids)}); "
                "waiting for its persisted result",
                flush=True,
            )
            return last_head, last_time

        log_path = jobs_snapshot_path(cfg.repo_dir).parent / "auto-audit.log"
        log_path.parent.mkdir(parents=True, exist_ok=True)
        command = _auto_audit_command(cfg.repo_dir, target)
        try:
            launched_at = datetime.fromtimestamp(
                now, timezone.utc
            ).isoformat()
        except (OSError, OverflowError, ValueError):
            # A production wall clock is always representable. Preserve the
            # launch if an injected/test clock is not; the contradictory v2
            # record then fails open on a later restart.
            launched_at = now_iso()
        metadata = {
            "schema_version": AUTO_AUDIT_GUARD_SCHEMA_VERSION,
            "status": "launching",
            "launcher_pid": os.getpid(),
            "pid": None,
            "head": cur,
            "target": target,
            "repo_dir": str(Path(cfg.repo_dir).resolve(strict=False)),
            "launched_at": launched_at,
            "launched_at_epoch": now,
            "command": command,
        }
        _write_auto_audit_guard_metadata(guard, metadata)
        with open(log_path, "ab") as fh:
            proc = subprocess.Popen(
                command,
                stdout=fh, stderr=fh, cwd=str(cfg.repo_dir), start_new_session=True,
                # The audit process never reads this descriptor. Merely keeping
                # it open preserves the exact kernel lease across conductor
                # restarts; close-on-exec remains enabled for every other fd.
                pass_fds=(guard.fileno(),),
            )
        metadata["status"] = "running"
        metadata["pid"] = getattr(proc, "pid", None)
        _write_auto_audit_guard_metadata(guard, metadata)
        print(f"  audit: main moved to {cur[:8]} — auto-refreshing the snapshot "
              f"(target {target}) so a downstream break surfaces", flush=True)
        # Launch is not success.  The next tick advances ``last_head`` only
        # after save_snapshot has atomically persisted complete per-module
        # coverage for this revision.
        remember_timed_head()
        return last_head, now
    except Exception as e:  # noqa: BLE001 — a refresh failure must not crash a tick
        print(f"  audit: auto-refresh skipped ({e})", flush=True)
        remember_timed_head()
        return last_head, now
    finally:
        # Do NOT LOCK_UN here.  ``pass_fds`` gave the child a duplicate of the
        # same open file description; closing only the parent's copy leaves the
        # lease held until that exact audit process exits.  If Popen failed,
        # this close simply releases the launch reservation.
        try:
            guard.close()
        except (AttributeError, OSError):
            pass


def _maybe_reconcile_signatures(
    cfg: ReviewConfig, last_sha: Optional[str], *, exclude_issues: "frozenset[int]"
) -> Optional[str]:
    """Best-effort project-wide signature reconcile, fired ONLY when HEAD moved
    since ``last_sha``. For each registered issue (skipping ones with a running
    job — a live refine may be editing that body), run the SAFE auto-reconcile:
    ADD uncited in-code result-key decls + replace git-proven renames, never
    dropping a stale citation on a guess (:func:`marathon.deck.refresh.
    auto_reconcile_issue`). Returns the HEAD sha now reconciled-against (so the
    caller throttles to once-per-code-change). NEVER raises — a reconcile
    failure on one issue warns and the loop continues."""
    from marathon.deck.refresh import auto_reconcile_issue, head_sha

    try:
        cur = head_sha(cfg.repo_dir)
    except Exception:  # noqa: BLE001 — reconcile must never crash the tick
        return last_sha
    if not cur or cur == last_sha:
        return last_sha
    if last_sha and not reconcile_cursor_is_ancestor(cfg.repo_dir, last_sha, cur):
        print(
            f"  warning: reconcile cursor {last_sha[:8]} is missing or not an "
            f"ancestor of HEAD {cur[:8]}; discarding it and reconciling "
            "conservatively",
            flush=True,
        )
        discard_reconcile_cursor(cfg.repo_dir)
        last_sha = None
    try:
        nums = sorted({n for ch in cfg.chapters.values() for (n, _t) in ch.entries})
    except Exception:  # noqa: BLE001
        return cur
    retry_needed = False
    diff_window = (last_sha, cur) if last_sha else None
    pending_human: list[dict] = []
    for num in nums:
        if num in exclude_issues:
            # Do not advance past a HEAD while one affected issue is in-flight;
            # replay the same window once its job exits.  The old code skipped
            # it but still consumed the cursor, permanently losing its rename.
            retry_needed = True
            continue
        try:
            # The HEAD move since last_sha IS the refine's diff window — it tells
            # the reconcile which decls were removed/added (rename/split/delete),
            # not just which are missing. None on the first pass (no prior sha) →
            # the reconcile falls back to leaf-match.
            r = auto_reconcile_issue(
                cfg, num,
                diff_window=diff_window,
            )
            if not r.get("ok", True):
                retry_needed = True
                print(
                    f"  warning: reconcile #{num} did not complete "
                    f"({r.get('message', 'unknown error')}); retaining cursor",
                    flush=True,
                )
                continue
            # A verified/frozen issue cannot be silently rewritten.  But merely
            # returning "flagged/suppressed" and advancing the HEAD cursor loses
            # the event forever: the body never gains the new card, and its old
            # signature fence can mask a rename from verified_staleness.  Make
            # the state transition explicit: confirm-unverify, then mechanically
            # reconcile under the preserved manifest, so the CURRENT shape
            # returns to the human queue.
            flags = (
                list(r.get("flagged_renames") or [])
                + list(r.get("flagged_splits") or [])
            )
            flagged_deletes = list(r.get("flagged_deletes") or [])
            suppressed_adds = list(r.get("suppressed_adds") or [])
            if flags or flagged_deletes or suppressed_adds:
                reasons: list[str] = []
                for item in r.get("flagged_renames") or []:
                    reasons.append(
                        f"removed/renamed: {item.get('old')} → {item.get('new')}"
                    )
                for item in r.get("flagged_splits") or []:
                    targets = ", ".join(item.get("targets") or [])
                    reasons.append(
                        f"removed/renamed: {item.get('old')} → {targets}"
                    )
                reasons.extend(
                    f"removed/renamed: {name}" for name in flagged_deletes
                )
                reasons.extend(f"new card: {name}" for name in suppressed_adds)

                from marathon.review.reverify import unverify_for_rereview

                if not unverify_for_rereview(cfg, num, reasons):
                    retry_needed = True
                    print(
                        f"  warning: reconcile #{num} needs re-review but "
                        "unverify was not confirmed; retaining the old "
                        "reconcile cursor to retry",
                        flush=True,
                    )
                    continue
                r = auto_reconcile_issue(
                    cfg, num, diff_window=diff_window, allow_reopened=True,
                )
                if not r.get("ok", True):
                    retry_needed = True
                    print(
                        f"  warning: reconcile #{num} reopened but body "
                        "reconciliation failed; retaining cursor to retry",
                        flush=True,
                    )
                    continue
            # Heuristic rename/split candidates need one human decision.  Keep
            # the reconcile cursor on the SAME old..new window until that
            # decision is applied: advancing here loses the only evidence for a
            # changed-leaf split, and a later browser preview could no longer
            # reconstruct what Aristotle changed.
            proposals = (
                list(r.get("proposed_renames") or [])
                + list(r.get("proposed_splits") or [])
            )
            if proposals:
                retry_needed = True
                pending_human.append(
                    {"issue": num, "proposals": len(proposals)}
                )
                print(
                    f"  reconcile #{num}: {len(proposals)} declaration "
                    "identity proposal(s) awaiting human confirmation; "
                    "retaining the HEAD cursor",
                    flush=True,
                )
                continue
            if r.get("updated"):
                print(f"--- reconciled #{num}: {r.get('message', '')} ---", flush=True)
        except Exception as e:  # noqa: BLE001 — one issue's failure isn't fatal
            print(f"  warning: reconcile #{num} failed: {e}", flush=True)
            retry_needed = True
    # Persist the waiting-on-a-human observations so the deck's digest can
    # surface them from a local read (advisory display data: the deck prunes
    # an entry itself when it applies that issue's reconcile decision).
    _save_pending_human(cfg, pending_human)
    # Failures/partial frozen transitions retry against the SAME HEAD next tick;
    # never advance the only diff window past unresolved work.
    return last_sha if retry_needed else cur


#: Advisory local snapshot of reconcile decisions waiting on the operator.
PENDING_HUMAN_RELPATH = Path(".marathon") / "conductor" / "pending-human.json"


def _save_pending_human(cfg: ReviewConfig, items: list[dict]) -> None:
    """Atomically record which issues wait on a human reconcile confirmation."""
    try:
        path = Path(cfg.repo_dir) / PENDING_HUMAN_RELPATH
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps({
            "schema_version": 1,
            "updated_ts": now_iso(),
            "reconcile": items,
        }, indent=2, sort_keys=True) + "\n")
        tmp.replace(path)
    except OSError:
        pass


def _maybe_reopen_stale_verified(
    cfg: ReviewConfig, *, exclude_issues: "frozenset[int]"
) -> int:
    """Re-open verified issues whose reviewable content has DRIFTED since the
    verify froze it. Iterate every registered issue; for each whose
    ``state.json`` status is ``'verified'`` and which has no running job
    (a live refine may be editing that body), compute
    :func:`marathon.review.reverify.verified_staleness`. If stale, call
    :func:`unverify_for_rereview` (drops the verified label, deletes the
    state.json entry, posts ONE comment). Returns the number of issues
    re-opened.

    NOT HEAD-gated (unlike the signature reconcile): a new card can arrive via
    an issue-BODY edit that never moves HEAD, so this pass must run on its own
    wall-clock cadence. NEVER raises — one issue's failure warns and the loop
    continues."""
    from marathon.review.reverify import unverify_for_rereview, verified_staleness

    try:
        state = load_state(cfg)
    except Exception:  # noqa: BLE001 — never crash the tick
        return 0
    reopened = 0
    for num, st in sorted(state.issues.items()):
        if getattr(st, "status", None) != "verified":
            continue
        if num in exclude_issues:
            continue
        try:
            stale, reasons = verified_staleness(cfg, num)
        except Exception as e:  # noqa: BLE001 — one issue isn't fatal
            print(f"  warning: staleness check #{num} failed: {e}", flush=True)
            continue
        if not stale:
            continue
        try:
            if unverify_for_rereview(cfg, num, reasons):
                reopened += 1
                print(
                    f"--- re-opened verified #{num} for re-review "
                    f"({'; '.join(reasons)}) ---",
                    flush=True,
                )
        except Exception as e:  # noqa: BLE001 — one issue isn't fatal
            print(f"  warning: re-open #{num} failed: {e}", flush=True)
    return reopened


def run_conductor(
    repo_dir: Optional[Path] = None,
    concurrency: Optional[int] = None,
    once: bool = False,
    prune: bool = False,
    max_attempts: int = daemon.DEFAULT_MAX_ATTEMPTS,
    worktree_parent: Optional[Path] = None,
    automerge: bool = True,
    merge_scope: str = "any",
    reconcile: bool = True,
    reverify: bool = True,
    queue_from_github: bool = False,
    fill_chapters: Optional[set[int]] = None,
    max_fill_attempts: int = 3,
    prove_frontier: bool = False,
    prover_branch: str = PROVER_BASE_BRANCH,
    repair_chapters: Optional[set[int]] = None,
    repair_frontier: bool = False,
    max_repair_attempts: int = 3,
) -> int:
    """Run the conductor loop. Returns the final exit code.

    Each tick: reap finished jobs (Phase-0 state machine); pick
    up to ``concurrency - running`` dispatchable rejections (oldest
    verdict first across all chapters, with the collision/backoff/
    double-dispatch rules in :func:`_pick_dispatchable`); spawn them in
    fresh worktrees; and rewrite the jobs.json snapshot.
    ``--once``: exit when the queue is drained and all jobs finished.
    SIGTERM/SIGINT: stop dispatching, wait for running jobs (never
    cancel), record nothing for interrupted ones.
    """
    cfg = load_config(repo_dir)
    n = resolve_concurrency(concurrency)
    # --queue-from-github OR the env var: discover the queue from GitHub labels.
    queue_from_github = queue_from_github or (
        os.environ.get(QUEUE_FROM_GITHUB_ENV_VAR, "").strip().lower()
        in ("1", "true", "yes")
    )
    if queue_from_github:
        print("  queue source: GitHub review:rejected label "
              "(discovering rejections from GitHub, not local state.json)")
    runs_parent = (
        Path(worktree_parent) if worktree_parent else default_runs_parent(cfg.repo_dir)
    ).expanduser().resolve()
    repo_resolved = cfg.repo_dir.resolve()
    if runs_parent == repo_resolved or runs_parent.is_relative_to(repo_resolved):
        sys.exit(
            f"worktree parent {runs_parent} is inside the repo {repo_resolved}; "
            "worktrees inside the repo leak into Aristotle bundles via "
            "`git ls-files --others` — pick a parent outside the repo"
        )

    if not acquire_conductor_lock(cfg):
        print(
            f"another conductor is already active (lock at "
            f"{conductor_lock_path(cfg)}); exiting"
        )
        return 0

    heartbeat = _ConductorHeartbeat(cfg.repo_dir)
    try:
        heartbeat.start()
    except OSError as exc:
        release_conductor_lock(cfg)
        print(
            "conductor startup failed: cannot write its runtime heartbeat "
            f"under {conductor_heartbeat_path(cfg.repo_dir).parent}: {exc}",
            flush=True,
        )
        return 2

    signal.signal(signal.SIGTERM, _handle_stop_signal)
    signal.signal(signal.SIGINT, _handle_stop_signal)

    # Belt-and-suspenders: ensure the consumer's .marathon/.gitignore
    # ignores marathon.db* so the runtime SQLite db is never committed by
    # any consumer (mirrors Ledger.init; harmless if already present).
    try:
        from marathon.ledger import ensure_marathon_gitignore
        ensure_marathon_gitignore(cfg.repo_dir)
    except Exception:  # noqa: BLE001 — gitignore hygiene must not block startup
        pass

    mode = "one-shot" if once else "daemon"
    print(
        f"=== conductor pid={os.getpid()} mode={mode} concurrency={n} "
        f"runs_parent={runs_parent} starting {datetime.now().isoformat()} ===",
        flush=True,
    )

    # Environment preflight: a conductor with no Aristotle key must NOT
    # dispatch — every refine would instantly exit EXIT_CONFIG_ERROR. Before
    # this gate existed, a keyless relaunch burned both #171's and #238's full
    # attempt budgets in 12 minutes and posted bogus public 'stalled' comments
    # (2026-07-07). The conductor still runs its keyless-safe passes
    # (automerge, reconcile, reap, snapshot); the deck shows the reason.
    dispatch_env_error = _dispatch_env_error()
    if dispatch_env_error is not None:
        print(f"⚠️  DEGRADED: {dispatch_env_error}", flush=True)

    # ADOPT still-alive refines from a PREVIOUS conductor's snapshot BEFORE the
    # first write_jobs_snapshot erases those rows. Tracking them as running jobs
    # (proc=None) means we neither reap their worktree (destruction) nor
    # re-dispatch their issue (a wasteful duplicate billed run) — a mid-refine
    # restart is now fully clean.
    jobs: list[ConductorJob] = _adopt_inherited_jobs(cfg.repo_dir)
    for j in jobs:
        print(
            f"  adopted live job from a previous conductor: #{j.issue_num} "
            f"pid {j.pid} in {j.worktree} — tracked (not reaped, not "
            "re-dispatched) until it exits",
            flush=True,
        )

    not_before: dict[int, float] = {}
    kept_worktrees: dict[int, str] = {}
    warned_unknown: set[int] = set()
    dispatch_count = 0
    fill_attempts: dict[int, int] = {}         # per-issue fill dispatch counter (capped)
    # Proving scope + default. ``fill_chapters`` is the candidate set the picker
    # considers each tick (restricted to skeleton-done chapters there);
    # ``proving_default`` is what to do absent a deck override: ON when the
    # operator asked (--prove-frontier / --fill-chapter), else OFF but ENABLEABLE
    # from the web app. To let the toggle turn proving on even without a startup
    # flag, the scope defaults to every registered chapter (the skeleton-done
    # gate keeps churning chapters out); an explicit --fill-chapter narrows it.
    explicit_fill = set(fill_chapters or ())
    if explicit_fill and not prove_frontier:
        fill_chapters = explicit_fill
    else:
        fill_chapters = set(cfg.chapters or {}) | explicit_fill
    proving_default = bool(prove_frontier or explicit_fill)
    if fill_chapters and proving_default:
        print(f"  fill (proving): scope = chapter(s) {sorted(fill_chapters)}; "
              f"ON by default (max {max_fill_attempts} attempts/issue)", flush=True)
        # A chapter is only proved once its skeleton is done (every statement
        # verified), so note which candidates are still churning. This hits gh,
        # so it runs ONLY when proving is on at startup — when proving is off
        # (deck-enableable), the per-tick picker's own skeleton-done gate covers
        # it and startup stays gh-free.
        try:
            from marathon.review.fill_discovery import chapter_skeleton_done
            waiting = sorted(c for c in fill_chapters
                             if not chapter_skeleton_done(cfg, c))
            if waiting:
                print(f"  fill: chapter(s) {waiting} not skeleton-done yet — "
                      "their fills wait until every statement is verified",
                      flush=True)
        except Exception as e:  # noqa: BLE001 — informational only
            print(f"  fill: skeleton-done check skipped — {e}", flush=True)
    elif fill_chapters:
        print(f"  fill (proving): available for chapter(s) "
              f"{sorted(fill_chapters)} — OFF; enable from the deck", flush=True)
    repair_attempts: dict[int, int] = {}       # per-chapter repair dispatch counter
    repair_chapters = set(repair_chapters or ())
    if repair_frontier:
        # Autonomous repair: every registered chapter is a candidate; discovery
        # each tick restricts to the ones that actually have a broken module.
        repair_chapters |= set(cfg.chapters or {})
    if repair_chapters:
        print(f"  repair enabled for chapter(s): {sorted(repair_chapters)} "
              f"(max {max_repair_attempts} attempts/chapter) — broken modules "
              "auto-fixed to main, gated by build + faithfulness jury", flush=True)
    # Proofs land on a SEPARATE branch (main stays skeleton). It is ensured
    # LAZILY — the first tick proving is actually enabled (by default or via the
    # deck toggle) — so a conductor that never proves never creates the branch.
    prover_branch_ready = False
    ticks = 0
    last_automerge = float("-inf")  # run the merge orchestrator on tick 1, then ~once/min
    # Durable across restarts so an Aristotle rename/split/delete retains its
    # exact old..new provenance instead of degrading to a leaf-name guess.
    last_reconcile_sha: Optional[str] = load_reconcile_cursor(cfg.repo_dir)
    # Auto re-audit tracking: recover the revision whose complete module
    # coverage was atomically persisted.  Never assume current HEAD on startup:
    # a restart after merge but before audit completion must re-audit, not
    # silently bless the older snapshot.
    last_audit_head: Optional[str] = _persisted_audit_revision(cfg)
    last_audit_time = float("-inf")
    last_tracker_sync = float("-inf")  # tracker safety-net cadence (~once/2min)
    last_merge_reconcile = float("-inf")  # merged-but-rejected re-review heal (~once/min)
    last_reopen_check = float("-inf")  # stale-verified re-open cadence (~once/2min)

    # Bind the jobs snapshot to this process before publishing readiness. This
    # prevents a fresh heartbeat from making a previous conductor's jobs or
    # degraded environment look current during the first slow tick.
    try:
        write_jobs_snapshot(
            cfg.repo_dir,
            jobs,
            n,
            prover_enabled=proving_default,
            degraded=dispatch_env_error,
            conductor_pid=os.getpid(),
        )
        heartbeat.mark_ready()
    except OSError as exc:
        heartbeat.stop()
        release_conductor_lock(cfg)
        print(
            f"conductor startup failed while publishing readiness: {exc}",
            flush=True,
        )
        return 2

    try:
        if prune:
            prune_worktrees(cfg.repo_dir, runs_parent)
        else:
            kept_worktrees.update(_recover_retry_worktrees(
                cfg, runs_parent,
                busy_issues={j.issue_num for j in jobs if j.status == "running"},
            ))
        _report_orphans(cfg, _known_project_ids(cfg.repo_dir))
        # Warm the shared Mathlib olean store ONCE before dispatching any
        # worktree, so each worktree's `lake exe cache get` is a pure OFFLINE
        # read of ~/.cache/mathlib — concurrent workers then never curl+write the
        # same archive into the shared store. Gated on the main checkout NOT
        # being built already: when it is (the common operator-mid-work case) the
        # store is already warm, and we must NOT run `cache get` into the
        # operator's live checkout and race a build they may have running there.
        if not _mathlib_already_built(cfg.repo_dir):
            main_prime_result = _prime_mathlib_cache(cfg.repo_dir)
            if main_prime_result is _CACHE_PRIME_UNSAFE:
                cache_error = (
                    "Mathlib cache preparation could not confirm its child "
                    "process stopped — dispatch is disabled until the "
                    "orchestrator is restarted"
                )
                dispatch_env_error = (
                    f"{dispatch_env_error}; {cache_error}"
                    if dispatch_env_error
                    else cache_error
                )
                print(f"⚠️  DEGRADED: {cache_error}", flush=True)

        while True:
            ticks += 1
            # Poll the deck's prover toggle (a control file); absent an override,
            # honor the startup default. Ensure the proving branch the first time
            # proving is actually enabled (so a never-proving conductor never
            # creates it).
            _prover_override = read_prover_control(cfg.repo_dir)
            prover_enabled_now = (
                proving_default if _prover_override is None else _prover_override
            )
            if prover_enabled_now and fill_chapters and not prover_branch_ready:
                _ensure_prover_branch(cfg.repo_dir, prover_branch)
                prover_branch_ready = True
                print(f"  fill (proving): ENABLED — proofs land on "
                      f"'{prover_branch}' only, never main; runs only when the "
                      "refine queue is idle.", flush=True)
            _reap_finished(
                cfg, jobs, not_before, kept_worktrees, max_attempts,
                reconcile=reconcile,
                fill_attempts=fill_attempts,
                repair_attempts=repair_attempts,
            )
            # A clean rc=0 whose PR-presence query hit a transient gh failure is
            # parked durably in review state. Retry that cheap query every tick
            # (and after restart) before scheduling any new Aristotle work.
            if reconcile:
                try:
                    _retry_pending_pr_checks(cfg)
                except Exception as exc:  # noqa: BLE001 — never crash conductor loop
                    print(f"  pending PR reconciliation failed: {exc}", flush=True)
            # Land eligible PRs (the merge orchestrator) so infra/registration
            # stays on main across all chapters. Throttled to ~once/min so the
            # short tick cadence does not hammer `gh`; best-effort (a failure
            # warns, never crashes the tick) via the shared daemon helper.
            # Exclude the head branches of RUNNING jobs — merging+deleting a
            # branch a live refine worktree is pushing to would fail its push.
            if automerge and not _STOP_REQUESTED and _now() - last_automerge > 60:
                last_automerge = _now()
                running_heads = frozenset(
                    j.branch for j in jobs if j.status == "running"
                )
                daemon._maybe_automerge(
                    cfg, True, scope=merge_scope, exclude_heads=running_heads,
                )
                # Second pass: land clean proof PRs on the PROVER branch. Its
                # base-equality guard means this cycle only ever touches PRs
                # based on the prover branch (proofs) and the main cycle above
                # only touches base==main (refines) — so a proof can never
                # reach main. Runs once the prover branch is live, so in-flight
                # proof PRs still land even if the deck toggles proving off.
                if prover_branch_ready:
                    daemon._maybe_automerge(
                        cfg, True, scope=merge_scope,
                        exclude_heads=running_heads, base=prover_branch,
                    )
            # Auto-reconcile issue signatures to the live code (ADD uncited
            # in-code result-key decls; replace git-proven renames; never drop a
            # stale citation on a guess). Fires only when HEAD MOVED since the
            # last reconcile — the code is the only thing a signature edit
            # tracks, so there is nothing to do between merges. Skips issues with
            # a running job (a live refine may be editing that body). Best-effort.
            if reconcile and not _STOP_REQUESTED:
                prior_reconcile_sha = last_reconcile_sha
                last_reconcile_sha = _maybe_reconcile_signatures(
                    cfg, last_reconcile_sha,
                    exclude_issues=frozenset(
                        j.issue_num for j in jobs if j.status == "running"
                    ),
                )
                if (
                    last_reconcile_sha
                    and last_reconcile_sha != prior_reconcile_sha
                ):
                    save_reconcile_cursor(cfg.repo_dir, last_reconcile_sha)
            # Keep the audit snapshot honest: when main has moved (a change
            # landed) and the tree is clean, auto-refresh it so a DOWNSTREAM
            # break — e.g. a frozen upstream signature change breaking a consumer
            # chapter — surfaces in the queue + repair discovery instead of hiding
            # behind a stale snapshot. Throttled, non-blocking, best-effort.
            if not _STOP_REQUESTED:
                last_audit_head, last_audit_time = _maybe_auto_audit(
                    cfg, last_audit_head, last_audit_time,
                )
            # Tracker safety-net: keep the parent tracker's emojis in sync with
            # the review:verified labels (a verify normally flips its own line;
            # this heals a straggler — an out-of-band label or a no-op'd flip).
            # Throttled; best-effort (a failure warns, never crashes the tick).
            if reconcile and not _STOP_REQUESTED and _now() - last_tracker_sync > 120:
                last_tracker_sync = _now()
                try:
                    from marathon.review.tracker import sync_tracker_to_verified
                    ok, msg = sync_tracker_to_verified(cfg)
                    if ok:
                        print(f"--- tracker sync: {msg} ---", flush=True)
                except Exception as e:  # noqa: BLE001 — never crash the tick
                    print(f"  warning: tracker sync failed: {e}", flush=True)
            # Heal issues stuck in 'rejected' after their refine PR was merged
            # out-of-band (a hand-merge skips automerge's post-merge requeue), so
            # they re-enter the review feed instead of sitting stuck. Throttled;
            # best-effort (never crashes the tick).
            if reconcile and not _STOP_REQUESTED and _now() - last_merge_reconcile > 60:
                last_merge_reconcile = _now()
                try:
                    _reconcile_merged_rejections(cfg)
                except Exception as e:  # noqa: BLE001 — never crash the tick
                    print(f"  warning: merged-rejection reconcile failed: {e}",
                          flush=True)
            # Re-open verified issues whose reviewable content drifted since the
            # verify froze it (a body edit added a new card, or a frozen
            # statement changed / was renamed in code). Wall-clock throttled —
            # NOT HEAD-gated: a new card can arrive via an issue-body edit that
            # never moves HEAD. Skips issues with a running job (a live refine
            # may be editing that body). Best-effort (never crashes the tick).
            if reverify and not _STOP_REQUESTED and _now() - last_reopen_check > 120:
                last_reopen_check = _now()
                try:
                    _maybe_reopen_stale_verified(
                        cfg,
                        exclude_issues=frozenset(
                            j.issue_num for j in jobs if j.status == "running"
                        ),
                    )
                except Exception as e:  # noqa: BLE001 — never crash the tick
                    print(f"  warning: stale-verified re-open failed: {e}",
                          flush=True)
            running = [j for j in jobs if j.status == "running"]

            picks: list[tuple[int, int]] = []
            # DEGRADED mode still admits the exact-SHA retry-build recovery:
            # it is deliberately independent of Aristotle. Every other paid
            # dispatch remains blocked by the environment preflight.
            if not _STOP_REQUESTED:
                picks = _pick_dispatchable(
                    cfg, jobs, not_before, _now(), n - len(running),
                    warned_unknown,
                    queue_from_github,
                    recovery_modes=(
                        frozenset({RECOVERY_RETRY_BUILD})
                        if dispatch_env_error is not None else None
                    ),
                )
                for issue_num, chapter in picks:
                    job = _dispatch_job(
                        cfg, issue_num, chapter, runs_parent, kept_worktrees
                    )
                    if job is None:
                        # Deferred (double-dispatch guard / spawn failure):
                        # stays queued; retried after a fixed defer window.
                        _park_deferred_recovery(cfg, issue_num)
                        not_before[issue_num] = _now() + WORKTREE_DEFER_SECONDS
                        continue
                    jobs.append(job)
                    dispatch_count += 1
                running = [j for j in jobs if j.status == "running"]

                # Idle-priority gate shared by repair + fill: autonomous work
                # runs ONLY when the human review/refine loop is idle this tick.
                refine_running = any(
                    j.status == "running" and not j.fill and not j.repair
                    for j in jobs
                )
                refine_pending = bool(picks)  # rejections we just dispatched/deferred
                repair_running = any(j.status == "running" and j.repair for j in jobs)

                # Repair-dispatch (opt-in): fix a chapter's non-building modules
                # on main. ABOVE proving (a broken chapter blocks its own review
                # AND anything downstream), below the human refine loop. One
                # repair at a time (foundational; edits main); build + the
                # faithfulness jury gate what merges.
                if (
                    dispatch_env_error is None
                    and repair_chapters and not refine_running
                    and not refine_pending and not repair_running
                    and (n - len(running)) > 0
                ):
                    from marathon.repair import broken_modules as _broken

                    for rchap in sorted(repair_chapters):
                        key = REPAIR_KEY_BASE + rchap
                        if key in not_before and not_before[key] > _now():
                            continue
                        if repair_attempts.get(rchap, 0) >= max_repair_attempts:
                            continue
                        broken = _broken(cfg, rchap)
                        if not broken:
                            continue
                        job = _dispatch_job(
                            cfg, key, rchap, runs_parent, kept_worktrees,
                            repair=True, repair_modules=broken,
                        )
                        if job is None:
                            not_before[key] = _now() + WORKTREE_DEFER_SECONDS
                            continue
                        repair_attempts[rchap] = repair_attempts.get(rchap, 0) + 1
                        print(f"--- dispatched REPAIR chapter {rchap} "
                              f"({len(broken)} broken module(s): "
                              f"{', '.join(m.split('.')[-1] for m in broken)}, "
                              f"attempt {repair_attempts[rchap]}) → main ---",
                              flush=True)
                        jobs.append(job)
                        dispatch_count += 1
                        break  # one repair per tick
                    running = [j for j in jobs if j.status == "running"]
                    repair_running = any(
                        j.status == "running" and j.repair for j in jobs
                    )

                # Fill-dispatch (opt-in): prove verified-but-`sorry` decls on
                # the prover branch. STRICTLY lower priority than the human
                # review/refine loop AND repair — proving runs ONLY when both
                # are idle. So a proof never competes for a slot with, or delays
                # the start of, a refine or repair. In-flight one-shot fills
                # finish normally.
                if (
                    dispatch_env_error is None
                    and fill_chapters and prover_enabled_now and not refine_running
                    and not refine_pending and not repair_running
                ):
                    slots = n - len(running)
                    fill_picks = (
                        _pick_fill_dispatchable(
                            cfg, fill_chapters,
                            running_issues={j.issue_num for j in running},
                            not_before=not_before,
                            fill_attempts=fill_attempts,
                            max_fill_attempts=max_fill_attempts,
                        )[:slots] if slots > 0 else []
                    )
                    for issue_num, chapter in fill_picks:
                        job = _dispatch_job(
                            cfg, issue_num, chapter, runs_parent,
                            kept_worktrees, fill=True, prover_branch=prover_branch,
                        )
                        if job is None:
                            not_before[issue_num] = _now() + WORKTREE_DEFER_SECONDS
                            continue
                        fill_attempts[issue_num] = fill_attempts.get(issue_num, 0) + 1
                        print(f"--- dispatched FILL #{issue_num} (chapter {chapter}, "
                              f"attempt {fill_attempts[issue_num]}) → {prover_branch} "
                              "---", flush=True)
                        jobs.append(job)
                        dispatch_count += 1
                    running = [j for j in jobs if j.status == "running"]

            write_jobs_snapshot(cfg.repo_dir, jobs, n,
                                prover_enabled=prover_enabled_now,
                                degraded=dispatch_env_error,
                                conductor_pid=os.getpid())

            if _STOP_REQUESTED:
                if not running:
                    break
                waiting = ", ".join(
                    f"#{j.issue_num} (pid {j.pid})" for j in running
                )
                print(
                    f"--- stop requested; waiting on {len(running)} running "
                    f"job(s): {waiting} — letting them finish (never "
                    "canceled) ---",
                    flush=True,
                )
                _sleep(TICK_SECONDS, interruptible=False)
                continue

            if once:
                # The queue is "drained" for one-shot purposes when nothing
                # is running and no eligible issue remains to dispatch.
                eligible = _eligible_issues(cfg, warned_unknown, queue_from_github)
                if not running and not eligible:
                    print(
                        "\n=== one-shot mode: queue drained; exiting ===",
                        flush=True,
                    )
                    break
                if ticks >= MAX_TICKS_ONCE:
                    print(
                        f"\n=== one-shot mode: safety cap of {MAX_TICKS_ONCE} "
                        "ticks reached; exiting ===",
                        flush=True,
                    )
                    break

            _sleep(
                TICK_SECONDS if (running or picks) else daemon.POLL_INTERVAL_SECONDS
            )
    finally:
        try:
            write_jobs_snapshot(
                cfg.repo_dir,
                jobs,
                n,
                degraded=dispatch_env_error,
                conductor_pid=os.getpid(),
            )
        finally:
            heartbeat.stop()
            release_conductor_lock(cfg)
            print(
                f"\n=== conductor done {datetime.now().isoformat()} "
                f"({dispatch_count} dispatch(es)) ===",
                flush=True,
            )

    return 0
