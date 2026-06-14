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
from marathon.refine import REFINE_STATE_FILENAME
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

# Where job worktrees branch from. refine's --auto-pr resets the branch
# to origin/<base> (default main) at iteration start anyway; this just
# has to be a valid start point for `git worktree add -B`.
WORKTREE_START_POINT = "origin/main"
# The branch the PRIMARY checkout must be on for conductor-side
# metadata regeneration — the same base the job worktrees branch from.
# Regenerating anywhere else would derive yaml fields from whatever the
# operator happens to have checked out.
METADATA_BASE_BRANCH = WORKTREE_START_POINT.rsplit("/", 1)[-1]

# Referee-cadence default: 0 = OFF (manual-only referee, today's
# behavior — BINDING parity). Raised explicitly via --referee-every N:
# after every N successful landings the conductor fires a referee
# --emit-tasks pass, whose structured fix-tasks then gate scheduling
# (see referee_blocked_chapters / _pick_dispatchable).
DEFAULT_REFEREE_EVERY = 0

# Loop cadence: short ticks while jobs are running (reap promptly, keep
# the snapshot fresh); the daemon's drained-queue interval otherwise.
TICK_SECONDS = 10
# A dispatch deferred by the worktree double-dispatch guard is retried
# after this long (the conflicting checkout is usually another live
# job or a leftover the operator must --prune; hammering won't help).
WORKTREE_DEFER_SECONDS = 60
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
    project_id: Optional[str] = None         # from the workdir refine checkpoint
    aristotle_status: Optional[str] = None   # TaskStatus.value from the checkpoint
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


# --- referee fix-task blocking (the "teeth" the scheduler respects) -----------
#
# A referee fix-task (marathon.ledger.RefereeTask, emitted by the
# concurrently-built `marathon referee --emit-tasks` — see referee.py) gains
# scheduling TEETH when it carries a BLOCKING relation: the ledger models
# this as the task's ``blocks_target`` column (the schema comment:
# "blocks_target names the planner target this task must land before; NULL =
# advisory, no scheduling teeth"). An OPEN task with a non-NULL blocks_target
# (or any decl in its ``target_decls``) is the depends_on/blocks edge the plan
# §2 "referee with teeth" ruling requires the conductor to honor: the
# scheduler must NOT dispatch a target/rejection around it (the Ch.11
# coordinateCoframe item survived twelve advisory iterations precisely because
# nothing blocked).
#
# The accessor below is a THIN, DOCUMENTED bridge between two units the
# referee-emit task and the conductor speak in:
#   * referee tasks block by DECL NAME (blocks_target / target_decls);
#   * the conductor schedules by (issue, CHAPTER) — its collision/dispatch
#     unit is the chapter target folder (Phase-3 grade; decl-level overlap
#     arrives with the audit dep graph).
# We resolve a blocking task's decls to chapters via the audit snapshot's
# decl->module map (module ``A.B.Chapter11.X`` lives under the chapter folder
# ``A/B/Chapter11`` = ``target_path_template.format(chapter=11)``). If the
# referee-emit task's exact field names shift, ONLY ``_blocking_decls`` and
# ``_task_chapters`` below need adjusting — they are the entire coupling
# surface, kept tiny on purpose.


def _blocking_decls(task) -> list[str]:
    """The decls a referee task blocks ON, or [] if the task has no teeth.

    A task has teeth iff it carries a non-NULL ``blocks_target`` (the
    ledger's blocks edge); an advisory task (blocks_target is None) is
    deliberately ignored here — it changes nothing about scheduling, by
    design. The blocked-decl set is ``blocks_target`` plus every name in
    ``target_decls`` (a dedup defect spans several decls; blocking any one
    of their chapters is correct — the fix touches them together)."""
    blocks_target = getattr(task, "blocks_target", None)
    if not blocks_target:
        return []
    decls = [blocks_target]
    for d in getattr(task, "target_decls", None) or []:
        if d and d not in decls:
            decls.append(d)
    return decls


def _module_to_relpath(module: str) -> str:
    """Lean module ``A.B.C`` -> repo-relative dir ``A/B/C`` (the module
    name with its final component still attached: ``A.B.Chapter11.Basic``
    -> ``A/B/Chapter11/Basic``). Chapter membership is then a prefix test
    against the chapter's target folder."""
    return "/".join(module.split("."))


def _chapter_for_decl(cfg: ReviewConfig, decl_name: str, by_module: dict) -> Optional[int]:
    """Resolve one decl name to its chapter number, or None.

    ``by_module`` maps decl name -> its audit module. A decl belongs to
    chapter N iff its module path lives under chapter N's target folder
    (``target_path_template.format(chapter=N)``): the decl's module-as-path
    equals that folder or is nested beneath it. Deterministic; no Lean, no
    network — pure over the snapshot already in memory."""
    module = by_module.get(decl_name)
    if not module:
        return None
    module_path = _module_to_relpath(module)
    for chapter in cfg.chapters:
        folder = _target_relpath(cfg, chapter)
        if module_path == folder or module_path.startswith(folder + "/"):
            return chapter
    return None


def referee_blocked_chapters(
    cfg: ReviewConfig, repo_dir: Optional[Path] = None
) -> dict[int, tuple]:
    """Map ``chapter -> (task_id, title)`` for every chapter an OPEN
    blocking referee fix-task gates. Empty dict = nothing blocked (today's
    state: no referee tasks → byte-identical scheduling).

    Read-only and fully degrading: a missing/newer ledger, a missing audit
    snapshot, or zero blocking tasks each yield ``{}`` with at most one
    printed note — a referee defect must never crash the scheduler (binding:
    the scheduler stays pure deterministic Python; this only READS the rows
    the referee already persisted). When several tasks block one chapter the
    lowest task id wins the displayed reason (deterministic, stable)."""
    repo = Path(repo_dir) if repo_dir is not None else cfg.repo_dir
    # 1. Open referee tasks (the ledger is the referee-emit task's output).
    try:
        from marathon.ledger import Ledger, LedgerError

        ledger = Ledger.for_repo(repo)
        ledger.init()
        tasks = ledger.all_referee_tasks(status="open")
    except (LedgerError, Exception) as e:  # noqa: BLE001 — degrade, never crash
        # A ledger that won't open (newer schema / corrupt) must not stop
        # the conductor; it simply schedules without referee teeth.
        print(f"  conductor: referee fix-task gate skipped (ledger unavailable: {e})")
        return {}
    blocking = [t for t in tasks if _blocking_decls(t)]
    if not blocking:
        return {}
    # 2. decl -> module, from the latest audit snapshot (the only place the
    #    referee's decl names can be located in a chapter folder).
    try:
        from marathon.audit.engine import load_snapshot

        snapshot = load_snapshot(repo)
    except Exception as e:  # noqa: BLE001 — snapshot is optional evidence
        print(f"  conductor: referee fix-task gate skipped (audit snapshot unavailable: {e})")
        return {}
    if snapshot is None:
        # Blocking tasks exist but we cannot locate their decls in any
        # chapter — report it (the operator should run `marathon audit run`),
        # and schedule unblocked rather than guessing a chapter.
        print(
            f"  conductor: {len(blocking)} blocking referee fix-task(s) present "
            "but no audit snapshot to resolve their decls to chapters — run "
            "`marathon audit run`; scheduling proceeds unblocked this pass"
        )
        return {}
    by_module = {d.name: d.module for d in snapshot.decls}
    blocked: dict[int, tuple] = {}
    for task in sorted(blocking, key=lambda t: (t.id if t.id is not None else 0)):
        for decl in _blocking_decls(task):
            chapter = _chapter_for_decl(cfg, decl, by_module)
            if chapter is not None and chapter not in blocked:
                blocked[chapter] = (task.id, task.title)
    return blocked


def _eligible_issues(
    cfg: ReviewConfig, warned_unknown: set[int]
) -> list[tuple[int, int]]:
    """(issue, chapter) pairs that still need an iteration, oldest
    verdict first ACROSS ALL chapters (the project-wide query in
    review.state already sorts by verdict_ts). Issues not registered in
    any chapter have no target folder to refine — skipped with one
    warning each per conductor run."""
    out: list[tuple[int, int]] = []
    for issue_num, _entry in pending_rejections_needing_iteration(cfg, None):
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
    blocked_chapters: Optional[dict[int, tuple]] = None,
    warned_blocked: Optional[set[int]] = None,
) -> list[tuple[int, int]]:
    """The deterministic scheduling decision: up to ``slots`` (issue,
    chapter) pairs to dispatch this tick.

    Rules, in order: oldest verdict first across all chapters; never the
    same issue twice (a running job's rejection still satisfies
    ``needs_iteration`` until its clean exit records the iteration);
    never before the issue's backoff ``not_before``; never a chapter
    gated by an unresolved BLOCKING referee fix-task (the ADDITIONAL
    Phase-8 defer condition — ``blocked_chapters`` maps chapter ->
    (task_id, title); the same-chapter-folder collision and the Phase-0
    retry/stall machine are untouched); never two jobs with equal target
    chapter folders — the younger is deferred (Phase-3 collision unit;
    decl-level overlap arrives with the Phase-5 audit engine).

    When ``blocked_chapters`` is empty/None (today's state — no referee
    tasks) every byte of this decision is identical to before the gate."""
    if slots <= 0:
        return []
    if warned_unknown is None:
        warned_unknown = set()
    if blocked_chapters is None:
        blocked_chapters = {}
    if warned_blocked is None:
        warned_blocked = set()
    running = [j for j in jobs if j.status == "running"]
    busy_issues = {j.issue_num for j in running}
    busy_targets = {j.target for j in running}
    picks: list[tuple[int, int]] = []
    for issue_num, chapter in _eligible_issues(cfg, warned_unknown):
        if len(picks) >= slots:
            break
        if issue_num in busy_issues:
            continue
        if not_before.get(issue_num, 0.0) > now:
            continue
        gate = blocked_chapters.get(chapter)
        if gate is not None:
            # Referee fix-task with teeth gates this chapter: DEFER (never
            # crash, never dispatch around it). One reason line per blocked
            # issue per pass so the log isn't spammed every tick.
            if issue_num not in warned_blocked:
                warned_blocked.add(issue_num)
                task_id, title = gate
                print(
                    f"--- deferred #{issue_num} (chapter {chapter}): blocked by "
                    f"referee task #{task_id} — {title} (resolve it, or close "
                    "the task, to unblock) ---",
                    flush=True,
                )
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


def _dispatch_job(
    cfg: ReviewConfig,
    issue_num: int,
    chapter: int,
    runs_parent: Path,
    kept_worktrees: dict[int, str],
) -> Optional[ConductorJob]:
    """Create the job worktree (or reuse the one a failed attempt kept —
    it still holds the per-issue branch, so a fresh add would trip the
    branch guard) and spawn ``python -m marathon refine`` in it.

    Returns None to DEFER (never crash) when the worktree cannot be
    created — most importantly when git refuses because the per-issue
    branch is checked out in another worktree, which is the
    double-dispatch guard working as intended."""
    ts = datetime.now().strftime("%Y%m%d-%H%M%S")
    branch = _branch_name_for_issue(f"Chapter{chapter}", issue_num)

    reuse = kept_worktrees.get(issue_num)
    if reuse and Path(reuse).is_dir():
        worktree = Path(reuse)
        print(f"--- reusing kept worktree for #{issue_num}: {worktree} ---")
    else:
        worktree = runs_parent / f"wt-i{issue_num}-{ts}"
        runs_parent.mkdir(parents=True, exist_ok=True)
        proc = _git(
            cfg.repo_dir,
            "worktree", "add", "-B", branch, str(worktree), WORKTREE_START_POINT,
        )
        if proc.returncode != 0:
            msg = ((proc.stderr or proc.stdout) or "").strip()
            lower = msg.lower()
            if "already checked out" in lower or "already used by worktree" in lower:
                print(
                    f"--- deferring #{issue_num}: branch {branch} is checked "
                    "out in another worktree (double-dispatch guard). If it "
                    "is a leftover, run `marathon conductor run --prune`. ---",
                    flush=True,
                )
            else:
                print(
                    f"--- deferring #{issue_num}: `git worktree add` failed: "
                    f"{msg} ---",
                    flush=True,
                )
            return None

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
    try:
        changed = regenerate_metadata(
            cfg.repo_dir, commit=True, push=_metadata_push_enabled(),
        )
    except Exception as e:  # noqa: BLE001 — bookkeeping must not kill the loop
        print(f"  metadata: regeneration skipped — {type(e).__name__}: {e}")
        return
    if changed:
        print("  metadata: formalization.yaml regenerated + committed", flush=True)


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


def _reap_finished(
    cfg: ReviewConfig,
    jobs: list[ConductorJob],
    not_before: dict[int, float],
    kept_worktrees: dict[int, str],
    max_attempts: int,
    *,
    land: Optional[str] = None,
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
        backoff = daemon._handle_refine_exit(
            cfg, job.issue_num, rc, max_attempts=max_attempts,
            interrupted=interrupted,
        )
        if backoff:
            not_before[job.issue_num] = _now() + backoff
        if interrupted:
            job.status = "interrupted"
            kept_worktrees[job.issue_num] = job.worktree
            print(f"    worktree kept (interrupted): {job.worktree}", flush=True)
        elif rc == 0:
            job.status = "succeeded"
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
            print(
                f"    worktree kept for debugging (#{job.issue_num} "
                f"{job.status}, exit {rc}): {job.worktree}",
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


def write_jobs_snapshot(
    repo_dir: Path, jobs: list[ConductorJob], concurrency: int
) -> None:
    """Atomic-ish write (tmp + rename, like review.state.save_state) so
    ``marathon conductor status`` never reads a torn snapshot."""
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


# --- Referee cadence (landings-count trigger) ---------------------------------


def _maybe_trigger_referee_cadence(
    cfg: ReviewConfig, referee_every: int, worktree_parent: Optional[Path]
) -> None:
    """Best-effort referee-cadence trigger (plan §2: cadence by landings-
    count). Delegates to :func:`marathon.landing.maybe_trigger_referee`,
    which counts landings.jsonl and fires ``marathon referee --emit-tasks``
    once per crossed multiple of ``referee_every`` (idempotent across ticks
    via its own state file). NEVER raises: a referee failure must not fail a
    conductor tick — the import and call are both guarded so even a broken
    landing module degrades to "no cadence this run".

    The worktree parent is forwarded so the count reads the landing
    worktree's marathon/next copy first (freshest), falling back to the
    repo checkout."""
    try:
        from marathon.landing import maybe_trigger_referee

        maybe_trigger_referee(
            cfg.repo_dir, referee_every, worktree_parent=worktree_parent
        )
    except Exception as e:  # noqa: BLE001 — cadence must never fail a tick
        print(f"  warning: referee cadence skipped ({type(e).__name__}: {e})")


# --- Main loop ----------------------------------------------------------------------


def run_conductor(
    repo_dir: Optional[Path] = None,
    concurrency: Optional[int] = None,
    once: bool = False,
    prune: bool = False,
    max_attempts: int = daemon.DEFAULT_MAX_ATTEMPTS,
    worktree_parent: Optional[Path] = None,
    land: Optional[str] = None,
    referee_every: int = DEFAULT_REFEREE_EVERY,
) -> int:
    """Run the conductor loop. Returns the final exit code.

    Each tick: reap finished jobs (Phase-0 state machine); compute the
    chapters gated by unresolved BLOCKING referee fix-tasks
    (:func:`referee_blocked_chapters` — read-only, fully degrading); pick
    up to ``concurrency - running`` dispatchable rejections (oldest
    verdict first across all chapters, with the collision/backoff/
    double-dispatch rules AND the referee-block gate in
    :func:`_pick_dispatchable`); spawn them in fresh worktrees; rewrite
    the jobs.json snapshot; and — when ``referee_every > 0`` — fire the
    referee on the landings-count cadence (best-effort, never blocking).
    ``--once``: exit when the queue is drained and all jobs finished.
    SIGTERM/SIGINT: stop dispatching, wait for running jobs (never
    cancel), record nothing for interrupted ones.

    ``referee_every`` defaults to 0 (OFF — manual-only referee, today's
    behavior); with no referee tasks on disk the scheduler is
    byte-identical to the pre-Phase-8 conductor.
    """
    cfg = load_config(repo_dir)
    n = resolve_concurrency(concurrency)
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

    mode = "one-shot" if once else "daemon"
    print(
        f"=== conductor pid={os.getpid()} mode={mode} concurrency={n} "
        f"runs_parent={runs_parent} starting {datetime.now().isoformat()} ===",
        flush=True,
    )

    jobs: list[ConductorJob] = []
    not_before: dict[int, float] = {}
    kept_worktrees: dict[int, str] = {}
    warned_unknown: set[int] = set()
    warned_blocked: set[int] = set()
    dispatch_count = 0
    ticks = 0

    try:
        if prune:
            prune_worktrees(cfg.repo_dir, runs_parent)
        _report_orphans(cfg, _known_project_ids(cfg.repo_dir))

        while True:
            ticks += 1
            _reap_finished(
                cfg, jobs, not_before, kept_worktrees, max_attempts, land=land
            )
            # Referee-cadence: fire the referee --emit-tasks pass once the
            # landings-count crosses a multiple of referee_every. Best-effort
            # and OFF by default (referee_every == 0): a referee failure
            # warns and never fails the loop (the binding best-effort rule).
            # Done BEFORE computing the gate so a pass that just emitted a
            # blocking task can take effect on the NEXT tick.
            if referee_every > 0 and not _STOP_REQUESTED:
                _maybe_trigger_referee_cadence(cfg, referee_every, worktree_parent)
            # Compute the chapters gated by unresolved BLOCKING referee
            # fix-tasks (read-only; {} when no referee tasks exist → the
            # scheduler is byte-identical to before Phase 8).
            blocked_chapters = referee_blocked_chapters(cfg)
            running = [j for j in jobs if j.status == "running"]

            picks: list[tuple[int, int]] = []
            if not _STOP_REQUESTED:
                picks = _pick_dispatchable(
                    cfg, jobs, not_before, _now(), n - len(running),
                    warned_unknown, blocked_chapters, warned_blocked,
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

            write_jobs_snapshot(cfg.repo_dir, jobs, n)

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
                # is running and every still-eligible issue is referee-
                # blocked (a blocked-only queue would otherwise spin to the
                # MAX_TICKS_ONCE safety cap — the block is resolved by a
                # human/referee action, not by waiting).
                eligible = _eligible_issues(cfg, warned_unknown)
                unblocked = [
                    pair for pair in eligible if pair[1] not in blocked_chapters
                ]
                if not running and not unblocked:
                    if eligible:
                        print(
                            f"\n=== one-shot mode: queue drained "
                            f"({len(eligible)} issue(s) deferred by referee "
                            "fix-tasks); exiting ===",
                            flush=True,
                        )
                    else:
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
        write_jobs_snapshot(cfg.repo_dir, jobs, n)
        release_conductor_lock(cfg)
        print(
            f"\n=== conductor done {datetime.now().isoformat()} "
            f"({dispatch_count} dispatch(es)) ===",
            flush=True,
        )

    return 0
