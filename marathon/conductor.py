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
import json
import os
import signal
import subprocess
import sys
import time
from dataclasses import dataclass, field, fields
from datetime import datetime
from pathlib import Path
from typing import Optional

import marathon.review.daemon as daemon
from marathon.post_pipeline import _branch_name_for_issue
from marathon.refine import (
    EXIT_AUTO_PR_BRANCH_ABORT,
    EXIT_CONFIG_ERROR,
    REFINE_STATE_FILENAME,
)
from marathon.review.config import ReviewConfig, load_config
from marathon.review.state import (
    load_state,
    pending_rejections_needing_iteration,
)
from marathon.state import load_refine_state, now_iso

# Snapshot + lock locations (repo-relative).
SNAPSHOT_SCHEMA_VERSION = 1
JOBS_SNAPSHOT_RELPATH = Path(".marathon/conductor/jobs.json")
CONDUCTOR_LOCK_FILENAME = "conductor.lock"

# Phase-4 landing integration: the only accepted value for --land.
# Opt-in (default None = off): today's per-issue --auto-pr flow is
# unchanged until the marathon/next stack has soaked.
LAND_NEXT = "next"

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
# A dispatch deferred by the worktree double-dispatch guard is retried
# after this long (the conflicting checkout is usually another live
# job or a leftover the operator must --prune; hammering won't help).
WORKTREE_DEFER_SECONDS = 60
# A FILL (prove) job that exits non-zero backs off this long before the
# next dispatch. Retries are already hard-bounded by the dispatch-time
# `max_fill_attempts` cap; this only stops a tight re-dispatch loop.
FILL_RETRY_BACKOFF_SECONDS = 300
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
    started_ts: Optional[str] = None         # ISO 8601
    finished_ts: Optional[str] = None        # ISO 8601
    status: str = "running"                  # running | succeeded | failed |
                                             # stalled | interrupted
    exit_code: Optional[int] = None
    error: Optional[str] = None              # log tail of a FAILED job (for the
                                             # snapshot/deck + the stall comment)
    project_id: Optional[str] = None         # from the workdir refine checkpoint
    aristotle_status: Optional[str] = None   # TaskStatus.value from the checkpoint
    fill: bool = False                       # a PROVE job (marathon fill) vs a refine
    repair: bool = False                     # a REPAIR job (marathon repair, → main)
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


def acquire_conductor_lock(cfg: ReviewConfig) -> bool:
    """Returns False if another *live* conductor already holds the lock."""
    cfg.runner_lock_dir.mkdir(parents=True, exist_ok=True)
    lock = conductor_lock_path(cfg)
    if lock.is_file():
        try:
            existing_pid = int(lock.read_text().strip())
            if daemon.process_alive(existing_pid):
                return False
        except (ValueError, OSError):
            pass
        lock.unlink(missing_ok=True)
    lock.write_text(str(os.getpid()))
    return True


def release_conductor_lock(cfg: ReviewConfig) -> None:
    lock = conductor_lock_path(cfg)
    try:
        if lock.is_file():
            stored_pid = int(lock.read_text().strip())
            if stored_pid == os.getpid():
                lock.unlink()
    except (ValueError, OSError):
        pass


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
        branch, merged_at = merged[issue_num]
        if not _ts_strictly_newer(merged_at, getattr(st, "verdict_ts", None)):
            continue  # merge predates this rejection — a fresh reject, not a stuck one
        n = _requeue_refined_issue(cfg, branch)
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
    picks: list[tuple[int, int]] = []
    for issue_num, chapter in _eligible_issues(cfg, warned_unknown, queue_from_github):
        if len(picks) >= slots:
            break
        if issue_num in busy_issues:
            continue
        if not_before.get(issue_num, 0.0) > now:
            continue
        target = _target_relpath(cfg, chapter)
        if target in busy_targets:
            continue  # same-chapter-folder collision: defer the younger
        busy_targets.add(target)
        picks.append((issue_num, chapter))
    return picks


# --- Worktree management -----------------------------------------------------------


def _git(repo_dir: Path, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git", "-C", str(repo_dir), *args],
        capture_output=True,
        text=True,
    )


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

#: ``{worktree_path_str: pid}`` for jobs a PREVIOUS conductor process left
#: running (populated once at startup from the pre-restart jobs snapshot).
#: Consulted by the reap guard alongside the pid marker; liveness is
#: re-checked at use so a finished job's entry never blocks a reap for long.
_INHERITED_RUNNING: dict[str, int] = {}


def _write_worktree_pid_marker(worktree: Path, pid: int) -> None:
    """Drop the live-job marker into ``worktree`` (best-effort). Uses the
    same self-ignoring convention as ``write_jobs_snapshot`` so the marker
    neither dirties ``git status`` nor leaks into Aristotle bundles."""
    try:
        marker = worktree / WORKTREE_PID_MARKER
        marker.parent.mkdir(parents=True, exist_ok=True)
        gitignore = marker.parent / ".gitignore"
        if not gitignore.is_file():
            gitignore.write_text("*\n")
        marker.write_text(f"{pid}\n")
    except OSError as e:
        print(f"  warning: could not write pid marker in {worktree} ({e})",
              flush=True)


def _pid_is_job_for(pid: int, worktree: str) -> bool:
    """True iff ``pid`` is alive AND is genuinely a marathon job running in
    ``worktree`` — i.e. its command line references the worktree's unique
    ``wt-i<issue>-<ts>`` basename (present via the refine's target path and
    ``--repo-dir``). The basename identity check is what makes the liveness
    test robust to PID REUSE: a bare ``os.kill(pid, 0)`` treats a recycled pid
    (a different same-user process that inherited the number) as "the job is
    alive", which would refuse the reap forever and re-open the #96 defer-loop
    wedge. Basename (not full path) avoids macOS /private symlink normalization
    mismatches between git's worktree list and the dispatch cmd."""
    if not daemon.process_alive(pid):
        return False
    name = Path(worktree).name
    try:
        cp = subprocess.run(
            ["ps", "-p", str(pid), "-o", "command="],
            capture_output=True, text=True, timeout=5,
        )
    except (OSError, subprocess.SubprocessError):
        # Can't introspect the cmdline — fall back to bare liveness (better to
        # defer a reap than destroy a possibly-live job).
        return True
    return name in (cp.stdout or "")


def _clear_worktree_pid_marker(worktree: str) -> None:
    """Delete the live-job marker once the conductor OBSERVES the job exit
    (failed/interrupted branches keep the worktree for debugging, so the
    marker would otherwise linger holding a dead — reusable — pid). The
    success path removes the whole worktree, so it needs no explicit clear."""
    try:
        (Path(worktree) / WORKTREE_PID_MARKER).unlink(missing_ok=True)
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
    try:
        pid = int((Path(worktree) / WORKTREE_PID_MARKER).read_text().strip())
        if _pid_is_job_for(pid, worktree):
            return pid
    except (OSError, ValueError):
        pass
    pid = _INHERITED_RUNNING.get(worktree)
    if pid is not None:
        if _pid_is_job_for(pid, worktree):
            return pid
        _INHERITED_RUNNING.pop(worktree, None)  # its job exited — stop tracking
    return None


def _inherit_running_worktrees(repo_dir: Path) -> dict[str, int]:
    """``{worktree: pid}`` of still-alive jobs recorded by a PREVIOUS
    conductor's snapshot. Called once at startup, BEFORE the first
    ``write_jobs_snapshot`` erases those rows: a pkill'd conductor's
    refine children survive it (their own process group), and reaping
    their worktrees mid-run destroys a fully billed Aristotle iteration
    (the 2026-07-07 #171 incident — 29.2m of Aristotle into a gutted
    directory, then a 3h39m re-run)."""
    out: dict[str, int] = {}
    snap = load_jobs_snapshot(repo_dir)
    for raw in (snap or {}).get("jobs", []):
        pid, worktree = raw.get("pid"), raw.get("worktree")
        if raw.get("status") != "running" or not pid or not worktree:
            continue
        try:
            alive = daemon.process_alive(int(pid))
        except (TypeError, ValueError):
            continue
        if alive:
            out[str(worktree)] = int(pid)
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
    live_pid = _worktree_live_pid(path)
    if live_pid is not None:
        print(
            f"  NOT reaping {path}: a live job (pid {live_pid}) from a previous "
            "conductor is still using it — deferring this dispatch until it "
            "exits", flush=True,
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
# build before the olean decompress, so this is generous.
CACHE_GET_TIMEOUT_SECONDS = 1200

# Mathlib's top-level olean — a PRESENCE PROXY for "this checkout has Mathlib
# built", used to skip a redundant `cache get` decompress. It is a presence
# stat, NOT a completeness proof: a build/decompress interrupted after this one
# file landed would read as "built". That is acceptable — `lake build`
# incrementally recompiles any missing oleans, so the only cost of a false
# "built" is a slightly slower build, never a wrong result.
_MATHLIB_BUILT_MARKER = Path(
    ".lake/packages/mathlib/.lake/build/lib/lean/Mathlib.olean"
)


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


def _prime_mathlib_cache(
    worktree: Path, *, timeout: float = CACHE_GET_TIMEOUT_SECONDS
) -> bool:
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

    FAIL-SAFE: returns True only on a clean cache-get; False on EVERYTHING else
    (skip flag set, no ``lake`` on PATH, non-zero exit, timeout, any exception)
    and NEVER raises — a prime failure must never abort a dispatch (the refine
    then just does its normal, slower build). On a timeout the ENTIRE cache-get
    subtree (lake + its lean/leanc/curl workers) is killed via its process group,
    so no orphaned compiler/downloader is left racing the refine's build in the
    same worktree. No-op when ``MARATHON_SKIP_CACHE_GET`` is set."""
    import shutil

    if os.environ.get("MARATHON_SKIP_CACHE_GET"):
        return False
    if shutil.which("lake") is None:
        return False
    try:
        # Own process group (start_new_session) so a timeout can kill the WHOLE
        # subtree, not just the lake parent — subprocess.run's timeout would
        # SIGKILL only the direct child and orphan its lean/curl workers.
        proc = subprocess.Popen(
            ["lake", "exe", "cache", "get"],
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
        _out, err = proc.communicate(timeout=timeout)
    except subprocess.TimeoutExpired:
        try:
            os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
        except OSError:
            pass
        try:
            proc.communicate(timeout=15)
        except Exception:  # noqa: BLE001 — reaping must not raise
            pass
        print(
            f"    cache get timed out after {int(timeout)}s (subtree killed); "
            f"will build Mathlib normally: {worktree}",
            flush=True,
        )
        return False
    except Exception as e:  # noqa: BLE001 — a prime must NEVER abort a dispatch
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


def _has_open_pr_for_issue(
    cfg: ReviewConfig, issue_num: int, chapter: int, *, kind: str = "refine"
) -> bool:
    """True when the issue's per-issue branch already has an OPEN PR — a fill is
    pending merge, so don't dispatch another. ``kind`` picks the branch prefix
    (``proof`` for a fill's prover-branch PR). Best-effort: a check failure
    returns False (better a rare duplicate than a wedged queue)."""
    from marathon.review.github import gh

    branch = _branch_name_for_issue(f"Chapter{chapter}", issue_num, kind=kind)
    try:
        cp = gh("pr", "list", "--repo", cfg.github_repo, "--head", branch,
                "--state", "open", "--json", "number", "--limit", "1", check=False)
        if cp.returncode != 0:
            return False
        return bool(json.loads(cp.stdout or "[]"))
    except Exception:  # noqa: BLE001
        return False


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
            if open_pr(cfg, n, chapter, kind="proof"):
                continue
            dispatchable.append(t)
    snapshot = _load_audit_snapshot(cfg) if _snapshot is _UNSET else _snapshot
    ordered = order_targets(dispatchable, snapshot)
    return [(t.issue, t.chapter) for t in ordered]


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
        branch = _branch_name_for_issue(f"Chapter{chapter}", issue_num, kind="refine")
        start_point = WORKTREE_START_POINT

    reuse = kept_worktrees.get(issue_num)
    if reuse and Path(reuse).is_dir():
        worktree = Path(reuse)
        print(f"--- reusing kept worktree for #{issue_num}: {worktree} ---")
        # A kept worktree from a FAILED attempt may already have Mathlib built
        # (the failure was mid-refine, not mid-build); only prime when it isn't,
        # so retries don't pay a redundant decompress.
        if not _mathlib_already_built(worktree):
            _prime_mathlib_cache(worktree)
    else:
        worktree = runs_parent / f"wt-i{issue_num}-{ts}"
        runs_parent.mkdir(parents=True, exist_ok=True)
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
                if _reap_orphan_worktree(cfg.repo_dir, branch):
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
        # Seed the fresh worktree's Mathlib oleans so the refine's `lake build`
        # skips the from-scratch Mathlib compile. Best-effort — a failure just
        # means a slower (normal) build; it never blocks the dispatch.
        _prime_mathlib_cache(worktree)

    workdir = runs_parent / f"wd-i{issue_num}-{ts}"
    workdir.mkdir(parents=True, exist_ok=True)
    target = worktree / _target_relpath(cfg, chapter)
    # The exact per-issue dispatch the daemon uses, flag-for-flag:
    # DEFAULT_REFINE_ARGS is imported, never forked. The conductor adds
    # exactly one flag of its own at this dispatch site:
    # --no-metadata-commit — N parallel workers all rewriting
    # formalization.yaml is the generalized form of the wall_time merge
    # race, so jobs never commit the yaml; the conductor regenerates it
    # centrally after each success (_regenerate_metadata_after_success).
    if repair:
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
        cmd = [
            sys.executable, "-m", "marathon", "refine",
            str(target),
            "--repo-dir", str(worktree),
            "--workdir", str(workdir),
            "--review-rejection", str(issue_num),
            "--no-metadata-commit",
            *daemon.DEFAULT_REFINE_ARGS,
        ]
        log_path = workdir / "conductor-refine.log"
        cmd += _tex_args(cfg, chapter)
    log_handle = log_path.open("ab")
    try:
        popen = subprocess.Popen(cmd, stdout=log_handle, stderr=subprocess.STDOUT)
    except OSError as e:
        log_handle.close()
        # The worktree exists and holds the branch now; remember it so
        # the retry reuses it instead of tripping its own branch guard.
        kept_worktrees[issue_num] = str(worktree)
        print(f"--- deferring #{issue_num}: could not spawn refine ({e}) ---")
        return None
    kept_worktrees.pop(issue_num, None)
    # Live-job marker: lets a FUTURE conductor process (after a restart) tell
    # this worktree is in use, so its reap guard defers instead of destroying
    # a running (billed) job.
    _write_worktree_pid_marker(worktree, popen.pid)
    print(
        f"\n--- dispatched #{issue_num} (chapter {chapter}): pid={popen.pid} "
        f"branch={branch} ---"
    )
    print(f"    worktree = {worktree}")
    print(f"    workdir  = {workdir}")
    print(f"    log      = {log_path}")
    print(f"    cmd      = {' '.join(cmd)}", flush=True)
    return ConductorJob(
        issue_num=issue_num,
        chapter=chapter,
        target=_target_relpath(cfg, chapter),
        worktree=str(worktree),
        workdir=str(workdir),
        branch=branch,
        pid=popen.pid,
        started_ts=now_iso(),
        status="running",
        fill=fill,
        repair=repair,
        proc=popen,
        log_handle=log_handle,
    )


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


def _enqueue_landing_after_success(cfg: ReviewConfig, job: ConductorJob) -> None:
    """Phase-4 opt-in hook (``--land next``): hand a just-succeeded job
    to the landing queue (``marathon landing run`` cherry-picks it onto
    ``marathon/next`` behind the build+gate).

    The per-issue branch is resolved to its commit SHA NOW: the next
    --auto-pr iteration hard-resets that branch to origin/<base>, so a
    branch name sitting in the queue could dangle — or silently point at
    a different iteration — by the time the landing runner pops it.
    Best-effort: a failed enqueue warns and never kills the loop (the
    per-issue PR still carries the work)."""
    from marathon.landing import enqueue_landing

    try:
        rev = _git(cfg.repo_dir, "rev-parse", "--verify", f"{job.branch}^{{commit}}")
        source_ref = (rev.stdout or "").strip()
        if rev.returncode != 0 or not source_ref:
            # NEVER enqueue the branch name as a fallback: by the time
            # the landing runner pops the request, origin/<branch> may
            # have been hard-reset to a DIFFERENT iteration — the queue
            # would silently land commits this job never produced.
            print(
                f"  warning: landing enqueue for #{job.issue_num} skipped — "
                f"could not resolve {job.branch!r} to a commit "
                f"({(rev.stderr or '').strip() or 'no output'}); the "
                "per-issue PR flow still has the work",
                flush=True,
            )
            return
        # Gate mode mirrors the dispatched flag set (single source: the
        # daemon's DEFAULT_REFINE_ARGS) — skeleton iterations must not
        # be gated on proof-mode sorry semantics.
        mode = "skeleton" if "--skeleton" in daemon.DEFAULT_REFINE_ARGS else "proof"
        path = enqueue_landing(
            cfg.repo_dir,
            issue_num=job.issue_num,
            chapter=job.chapter,
            source_ref=source_ref,
            workdir=job.workdir,
            mode=mode,
        )
        print(
            f"  landing: enqueued #{job.issue_num} ({source_ref[:12]}) "
            f"→ {path.name}",
            flush=True,
        )
    except Exception as e:  # noqa: BLE001 — bookkeeping must not kill the loop
        print(
            f"  warning: landing enqueue for #{job.issue_num} failed "
            f"({type(e).__name__}: {e}); the per-issue PR flow still has "
            "the work",
            flush=True,
        )


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
    from marathon.review.github import gh

    if not _REFINE_BRANCH_RE.match(branch):
        return
    try:
        cp = gh("pr", "list", "--repo", cfg.github_repo, "--head", branch,
                "--state", "all", "--json", "number", "--limit", "1", check=False)
        if cp.returncode != 0:
            return
        if json.loads(cp.stdout or "[]"):
            return  # a PR exists — let the normal merge → requeue path handle it
    except Exception as exc:  # noqa: BLE001 — a check failure must not crash a reap
        print(f"  no-PR check for {branch} failed ({exc}); skipping", flush=True)
        return
    st = load_state(cfg).issues.get(job.issue_num)
    if st is not None and getattr(st, "status", None) == "rejected":
        # landed=False: NOTHING merged here (no diff → no PR). A 'fix landed'
        # recovery comment would be a public lie — this only re-surfaces the
        # unchanged issue for re-review.
        n = _requeue_refined_issue(cfg, branch, landed=False)
        if n is not None:
            print(f"--- #{n} refine exited clean but landed no PR — requeued for "
                  f"re-review (was stuck 'rejected') ---", flush=True)


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
        kept_worktrees[job.issue_num] = job.worktree
        print(f"    worktree kept (fill interrupted): {job.worktree}", flush=True)
        return
    if rc == 0:
        job.status = "succeeded"
        _remove_worktree(cfg.repo_dir, Path(job.worktree))
        # Ran with --no-metadata-commit; regenerate formalization.yaml centrally
        # (same completion hook as a clean refine).
        _regenerate_metadata_after_success(cfg)
        return
    # Non-zero: keep the worktree for debugging and back off (bounded by the
    # dispatch-time attempt cap). No rejection-state mutation.
    job.status = "failed"
    kept_worktrees[job.issue_num] = job.worktree
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


def _reap_finished(
    cfg: ReviewConfig,
    jobs: list[ConductorJob],
    not_before: dict[int, float],
    kept_worktrees: dict[int, str],
    max_attempts: int,
    *,
    land: Optional[str] = None,
    reconcile: bool = True,
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
        if job.status != "running" or job.proc is None:
            continue
        rc = job.proc.poll()
        if rc is None:
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
            _reap_fill_job(cfg, job, rc, interrupted, kept_worktrees, not_before)
            continue
        backoff = daemon._handle_refine_exit(
            cfg, job.issue_num, rc, max_attempts=max_attempts,
            interrupted=interrupted, failure_detail=job.error,
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
            # Phase-4 opt-in: enqueue onto the landing queue ONLY under
            # --land next (default off — the per-issue PR flow is
            # unchanged until the marathon/next stack soaks).
            if land == LAND_NEXT:
                _enqueue_landing_after_success(cfg, job)
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
            # Distinct, top-level git-AUTH warning. The refine exits with the
            # distinguished EXIT_AUTO_PR_BRANCH_ABORT code when --auto-pr
            # branch prep aborted (e.g. ``git fetch origin <base>`` failed,
            # including a credential failure). We surface it here — NOT buried
            # in the per-job log — because retries will keep failing until a
            # human fixes the credentials. Keying off the exit code (rather
            # than tailing the job log) avoids a log-file/closed-handle race
            # and is robust regardless of log content.
            if rc == EXIT_AUTO_PR_BRANCH_ABORT:
                print(
                    f"⚠️  refine #{job.issue_num} aborted on a GIT AUTH "
                    f"failure — run `gh auth setup-git`; retries will keep "
                    f"failing until this is fixed.",
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


#: The deck's web toggle for proving — a control file the conductor polls each
#: tick. Lives in the self-ignoring .marathon/conductor/ runtime dir so it never
#: leaks into an Aristotle bundle or dirties `git status`.
PROVER_CONTROL_RELPATH = Path(".marathon/conductor/prover-control.json")


def prover_control_path(repo_dir: Path) -> Path:
    return Path(repo_dir) / PROVER_CONTROL_RELPATH


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
            "DISABLED (automerge/reconcile still run). Export the key and "
            "restart the conductor."
        )
    return None


def write_jobs_snapshot(
    repo_dir: Path, jobs: list[ConductorJob], concurrency: int,
    prover_enabled: Optional[bool] = None,
    degraded: Optional[str] = None,
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
    when it finishes; the tick continues). Throttled by
    :data:`AUTO_AUDIT_MIN_INTERVAL_SECONDS`, and skipped while the operator has
    uncommitted Lean work. Best-effort — never raises."""
    now = _now_fn()
    if now - last_time < AUTO_AUDIT_MIN_INTERVAL_SECONDS:
        return last_head, last_time
    from marathon.deck.refresh import head_sha

    cur = head_sha(cfg.repo_dir)
    if cur is None or cur == last_head:
        return last_head, last_time  # HEAD unmoved / unknown — nothing to refresh
    if _has_uncommitted_lean(cfg.repo_dir):
        return last_head, now  # operator mid-edit — skip, but reset the throttle
    target = _audit_target_folder(cfg)
    if target is None:
        return last_head, now
    try:
        log_path = jobs_snapshot_path(cfg.repo_dir).parent / "auto-audit.log"
        log_path.parent.mkdir(parents=True, exist_ok=True)
        with open(log_path, "ab") as fh:
            subprocess.Popen(
                [sys.executable, "-m", "marathon", "audit", "run",
                 "--target", target, "--repo-dir", str(cfg.repo_dir)],
                stdout=fh, stderr=fh, cwd=str(cfg.repo_dir), start_new_session=True,
            )
        print(f"  audit: main moved to {cur[:8]} — auto-refreshing the snapshot "
              f"(target {target}) so a downstream break surfaces", flush=True)
        return cur, now
    except Exception as e:  # noqa: BLE001 — a refresh failure must not crash a tick
        print(f"  audit: auto-refresh skipped ({e})", flush=True)
        return last_head, now


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
    try:
        nums = sorted({n for ch in cfg.chapters.values() for (n, _t) in ch.entries})
    except Exception:  # noqa: BLE001
        return cur
    for num in nums:
        if num in exclude_issues:
            continue
        try:
            # The HEAD move since last_sha IS the refine's diff window — it tells
            # the reconcile which decls were removed/added (rename/split/delete),
            # not just which are missing. None on the first pass (no prior sha) →
            # the reconcile falls back to leaf-match.
            r = auto_reconcile_issue(
                cfg, num,
                diff_window=((last_sha, cur) if last_sha else None),
            )
            if r.get("updated"):
                print(f"--- reconciled #{num}: {r.get('message', '')} ---", flush=True)
        except Exception as e:  # noqa: BLE001 — one issue's failure isn't fatal
            print(f"  warning: reconcile #{num} failed: {e}", flush=True)
    return cur


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
    land: Optional[str] = None,
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

    # Capture still-alive jobs from a PREVIOUS conductor's snapshot BEFORE the
    # first write_jobs_snapshot erases those rows — the reap guard consults
    # this map so a restart never destroys a running (billed) refine.
    _INHERITED_RUNNING.clear()
    _INHERITED_RUNNING.update(_inherit_running_worktrees(cfg.repo_dir))
    for wt, pid in _INHERITED_RUNNING.items():
        print(
            f"  inherited live job from a previous conductor: pid {pid} in "
            f"{wt} — worktree protected from reaping until it exits",
            flush=True,
        )

    jobs: list[ConductorJob] = []
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
    last_reconcile_sha: Optional[str] = None  # reconcile only when HEAD changes
    # Auto re-audit tracking: the HEAD our last audit covered (init to the
    # current HEAD so we don't audit at startup — only after a change lands) and
    # the throttle clock.
    from marathon.deck.refresh import head_sha as _head_sha
    last_audit_head: Optional[str] = _head_sha(cfg.repo_dir)
    last_audit_time = float("-inf")
    last_tracker_sync = float("-inf")  # tracker safety-net cadence (~once/2min)
    last_merge_reconcile = float("-inf")  # merged-but-rejected re-review heal (~once/min)
    last_reopen_check = float("-inf")  # stale-verified re-open cadence (~once/2min)

    try:
        if prune:
            prune_worktrees(cfg.repo_dir, runs_parent)
        _report_orphans(cfg, _known_project_ids(cfg.repo_dir))
        # Warm the shared Mathlib olean store ONCE before dispatching any
        # worktree, so each worktree's `lake exe cache get` is a pure OFFLINE
        # read of ~/.cache/mathlib — concurrent workers then never curl+write the
        # same archive into the shared store. Gated on the main checkout NOT
        # being built already: when it is (the common operator-mid-work case) the
        # store is already warm, and we must NOT run `cache get` into the
        # operator's live checkout and race a build they may have running there.
        if not _mathlib_already_built(cfg.repo_dir):
            _prime_mathlib_cache(cfg.repo_dir)

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
                land=land, reconcile=reconcile,
            )
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
                last_reconcile_sha = _maybe_reconcile_signatures(
                    cfg, last_reconcile_sha,
                    exclude_issues=frozenset(
                        j.issue_num for j in jobs if j.status == "running"
                    ),
                )
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
            # DEGRADED mode: with a broken dispatch environment (no Aristotle
            # key), dispatching would only burn doomed subprocesses — skip the
            # whole dispatch phase but keep the keyless-safe passes (reap,
            # automerge, reconcile, snapshot) running. The reason is published
            # in the jobs snapshot so the deck can show it.
            if not _STOP_REQUESTED and dispatch_env_error is None:
                picks = _pick_dispatchable(
                    cfg, jobs, not_before, _now(), n - len(running),
                    warned_unknown,
                    queue_from_github,
                )
                for issue_num, chapter in picks:
                    job = _dispatch_job(
                        cfg, issue_num, chapter, runs_parent, kept_worktrees
                    )
                    if job is None:
                        # Deferred (double-dispatch guard / spawn failure):
                        # stays queued; retried after a fixed defer window.
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
                    repair_chapters and not refine_running
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
                    fill_chapters and prover_enabled_now and not refine_running
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
                                degraded=dispatch_env_error)

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
        write_jobs_snapshot(cfg.repo_dir, jobs, n, degraded=dispatch_env_error)
        release_conductor_lock(cfg)
        print(
            f"\n=== conductor done {datetime.now().isoformat()} "
            f"({dispatch_count} dispatch(es)) ===",
            flush=True,
        )

    return 0
