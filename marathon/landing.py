"""Phase-4 landing queue: serial FIFO landings onto ``marathon/next``.

WHY: today every fix lands via a per-issue branch hard-reset to
``origin/main`` and a force-pushed PR — machine work can never stack on
other machine work, and ``[build:FAIL]`` PRs reach main on title-string
eyeballing. The landing queue gives machine work an integration branch
with a HARD gate in front of the push: cherry-pick the job's commits
onto ``marathon/next``, ``lake build``, run the machine gate with
*enforce* semantics, and only then push. Main fast-forwards green via an
explicit, human-invoked promotion.

Binding rulings (docs/marathon-v2-plan.md §2 LANDING QUEUE box +
ruling 6, §3 Phase 4 row; critiques in
docs/v2-analysis/crit-feas-ux-first.md point 3):

* **The integration branch is sacred.** ``marathon/next`` is created
  from ``origin/<base>`` if absent and is NEVER force-pushed and never
  reset. The only ``reset --hard`` ever issued aligns the *local landing
  worktree* to ``origin/marathon/next`` (discarding unpushed debris from
  crashed/bounced attempts); remote history is append-only.
* **Bounce, not block.** Any failure (cherry-pick conflict, red build,
  gate fail, push rejection) aborts cleanly, writes a bounce report,
  posts at most ONE ``gh issue comment`` (circuit breaker: signature
  dedup + a daily per-issue cap), and lets the queue move on. Only
  transient classes (push rejection after the remote moved) re-queue,
  and at most once; conflicts/build/gate failures wait for the next
  human/conductor action.
* **NEVER resubmit to Aristotle from here and never call
  ``project.ask()``.** The server-side bundle is stale by definition
  once ``marathon/next`` has moved (critique point 3): Aristotle would
  "fix" a conflict in a world that no longer exists. Nothing in this
  module imports aristotlelib.
* **No gate override.** A fail verdict blocks the landing, full stop —
  overrides are an audited PR-flow affordance (``--gate-override``),
  not a landing-queue one.
* **Promotion is explicit.** ``marathon landing promote`` fast-forwards
  the base branch to ``origin/marathon/next`` and refuses (with the
  divergence summary) when not fast-forwardable. Nothing promotes
  automatically.

**Landing worktree.** A dedicated checkout of ``marathon/next`` under a
parent OUTSIDE the repo (default
``~/Desktop/marathon-runs/landing/<repo-name>/next``) — the same hazard
rationale as conductor worktrees: in-repo worktrees leak into Aristotle
bundles via ``git ls-files --others``, so in-repo parents are refused at
startup.

**Queue + records.** Requests are JSON files in the self-gitignoring
``.marathon/landing/queue/`` (the conductor ``jobs.json`` convention) —
droppable runtime state. Successful landings append one line to the
*tracked* ``.marathon/landing/landings.jsonl`` — committed on
``marathon/next`` itself (in the landing worktree, pushed with the
landing) so the record rides the branch it describes and merges by the
keyed write-once pattern of the wall-time v2 sidecar. Bounce reports
land in the self-gitignoring ``.marathon/landing/bounces/`` — local
evidence; the gh comment is the durable notification.
"""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
import time
from dataclasses import dataclass, fields
from datetime import datetime
from pathlib import Path
from typing import Optional

import marathon.review.daemon as daemon
from marathon.gate import MODE_SKELETON, MODES, run_gate
from marathon.post_pipeline import run_lake_build
from marathon.review.config import ReviewConfig, find_repo_dir, load_config
from marathon.state import now_iso

# The integration branch (plan §2 LANDING QUEUE box). Never force-pushed,
# never reset; created from origin/<base> on first landing.
LANDING_BRANCH = "marathon/next"
DEFAULT_BASE_BRANCH = "main"

# Repo-relative locations. queue/ and bounces/ self-gitignore (runtime
# state); landings.jsonl is tracked — committed on marathon/next itself.
QUEUE_RELPATH = Path(".marathon/landing/queue")
BOUNCES_RELPATH = Path(".marathon/landing/bounces")
LANDINGS_RELPATH = Path(".marathon/landing/landings.jsonl")
# Bounce-comment circuit-breaker state. Lives inside the self-ignored
# bounces/ dir (it IS bounce-comment bookkeeping) so it never dirties
# `git status` or leaks into Aristotle bundles.
BREAKER_FILENAME = "comment-breaker.json"

# Single landing PID lock, beside the daemon/conductor locks.
LANDING_LOCK_FILENAME = "landing.lock"
LANDING_WORKTREE_NAME = "next"

# Same value the daemon's refine dispatch passes as --build-timeout: a
# full Mathlib-pinned `lake build` is the dominant landing cost.
DEFAULT_BUILD_TIMEOUT_SECONDS = 1800
# Poll cadence when the queue is drained in daemon mode.
LANDING_POLL_SECONDS = 60

# Failure classes. Only TRANSIENT classes ever re-queue (at most once —
# MAX_REQUEUES): a push rejection means the remote moved under us and a
# fresh fetch+cherry-pick is exactly the right retry. Conflicts, red
# builds, and gate failures are content problems — retrying without new
# content would bounce identically forever.
CLASS_CONFLICT = "cherry-pick-conflict"
CLASS_BUILD = "build-failed"
CLASS_GATE = "gate-failed"
CLASS_PUSH = "push-rejected"
CLASS_SETUP = "setup-failed"
TRANSIENT_CLASSES = frozenset({CLASS_PUSH})
MAX_REQUEUES = 1

# Bounce-comment circuit breaker: a failure signature is posted at most
# once EVER, and each issue gets at most this many bounce comments per
# day. Past either limit the report file is still written; only the
# GitHub noise is suppressed (a console warning says so).
BOUNCE_COMMENT_DAILY_CAP = 3

_CLASS_HINTS = {
    CLASS_CONFLICT: (
        "Conflicts never auto-retry: the next conductor iteration (or a "
        "human rebase) must produce a fresh fix against the current "
        f"`{LANDING_BRANCH}`."
    ),
    CLASS_BUILD: (
        "Build failures never auto-retry: fix the breakage and let the "
        "next iteration re-enqueue."
    ),
    CLASS_GATE: (
        "Gate failures never auto-retry and have no override here "
        "(overrides belong to the PR flow): address the findings and let "
        "the next iteration re-enqueue."
    ),
    CLASS_PUSH: (
        "Push rejections are transient (the remote moved): the landing "
        "runner fetches and retries once automatically."
    ),
    CLASS_SETUP: (
        "Setup failures never auto-retry: check the landing runner's "
        "environment, then re-enqueue manually."
    ),
}


def _sleep(seconds: float) -> None:
    """Module-level so tests can stub the drained-queue poll pause."""
    time.sleep(seconds)


# --- Request model -----------------------------------------------------------------


@dataclass
class LandingRequest:
    """One queued landing. ``source_ref`` is the job's commit SHA (the
    conductor resolves the per-issue branch at enqueue time — the next
    --auto-pr iteration hard-resets that branch, so a branch name would
    dangle) or a ref the landing worktree can resolve. ``mode`` selects
    the gate semantics (skeleton iterations legitimately carry sorries;
    proof mode treats new ones as regressions)."""

    issue_num: int
    chapter: int
    source_ref: str
    workdir: str
    enqueued_ts: str
    attempts: int = 0
    mode: str = MODE_SKELETON

    def to_json(self) -> dict:
        return {f.name: getattr(self, f.name) for f in fields(self)}

    @classmethod
    def from_json(cls, data: dict) -> "LandingRequest":
        # Drop unknown keys so old queue files survive future field
        # additions (same tolerance as ConductorJob.from_json).
        known = {f.name for f in fields(cls)}
        kwargs = {k: v for k, v in data.items() if k in known}
        # Strict int coercion: queue files are plain JSON anyone local
        # can drop in, and issue_num flows into bounce-report FILENAMES
        # and gh argv — a path-shaped value must quarantine (int()
        # raises → pop renames *.corrupt), never traverse out of
        # .marathon/landing/.
        for key in ("issue_num", "chapter", "attempts"):
            if key in kwargs:
                kwargs[key] = int(kwargs[key])
        return cls(**kwargs)


def enqueue_landing(
    repo_dir: Path,
    *,
    issue_num: int,
    chapter: int,
    source_ref: str,
    workdir: str,
    mode: str = MODE_SKELETON,
    attempts: int = 0,
) -> Path:
    """Public API: append one landing request to the FIFO queue.

    The queue is plain JSON files whose names sort in enqueue order
    (microsecond timestamp prefix), so FIFO needs no index file and
    concurrent enqueuers (N conductor jobs) never contend on shared
    state. The directory self-gitignores on first write — an
    untracked-not-ignored queue would leak into Aristotle bundles and
    permanently dirty ``git status`` (the conductor jobs.json rationale).
    """
    if mode not in MODES:
        raise ValueError(f"unknown landing mode {mode!r}; expected one of {MODES}")
    qdir = Path(repo_dir) / QUEUE_RELPATH
    qdir.mkdir(parents=True, exist_ok=True)
    gitignore = qdir / ".gitignore"
    if not gitignore.is_file():
        gitignore.write_text("*\n")
    req = LandingRequest(
        issue_num=issue_num,
        chapter=chapter,
        source_ref=source_ref,
        workdir=str(workdir),
        enqueued_ts=now_iso(),
        attempts=attempts,
        mode=mode,
    )
    ts = datetime.now().strftime("%Y%m%d-%H%M%S-%f")
    path = qdir / f"{ts}-i{issue_num}.json"
    # Write-then-rename: the runner polls this directory and a torn
    # in-place write would be quarantined as *.corrupt — a silently
    # dropped landing. The .tmp name never matches the *.json glob.
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(req.to_json(), indent=2) + "\n")
    os.replace(tmp, path)
    return path


def pop_oldest_request(repo_dir: Path) -> Optional[LandingRequest]:
    """Remove and return the oldest queued request (filename order =
    enqueue order), or None when the queue is empty. Unparseable files
    are renamed ``*.corrupt`` (warned) so one bad file can never wedge
    the FIFO."""
    qdir = Path(repo_dir) / QUEUE_RELPATH
    if not qdir.is_dir():
        return None
    for path in sorted(qdir.glob("*.json")):
        try:
            req = LandingRequest.from_json(json.loads(path.read_text()))
        except (json.JSONDecodeError, TypeError, ValueError) as e:
            corrupt = path.with_suffix(".corrupt")
            print(
                f"  warning: unreadable landing request {path.name} ({e}); "
                f"renamed to {corrupt.name}"
            )
            path.rename(corrupt)
            continue
        path.unlink()
        return req
    return None


# --- Single landing lock --------------------------------------------------------------
#
# Same acquire/release semantics as the conductor's PID lock (stale
# locks from dead PIDs reclaimed; only the owning PID releases), on
# ``landing.lock``. Mirrored rather than imported: the conductor's
# helpers are filename-bound to conductor.lock and conductor.py is
# frozen to the enqueue-on-success integration this phase, so making
# them generic there is out of scope.


def landing_lock_path(repo_dir: Path) -> Path:
    return (
        Path(repo_dir) / ".marathon" / "review" / "runner-locks"
        / LANDING_LOCK_FILENAME
    )


def acquire_landing_lock(repo_dir: Path) -> bool:
    """Returns False if another *live* landing runner holds the lock.

    The claim is an atomic hard-link of a pre-written PID file (O_EXCL
    semantics with the content already in place): a check-then-write
    sequence has a TOCTOU window in which two runners starting together
    would both acquire and share (and ``reset --hard``) one landing
    worktree — one could even push the other's not-yet-gated
    cherry-pick. Two runners racing to reclaim a stale (dead-PID) lock
    resolve the same way: both unlink, exactly one link wins, the loser
    reads the winner's live PID on the second pass and yields."""
    lock = landing_lock_path(repo_dir)
    lock.parent.mkdir(parents=True, exist_ok=True)
    tmp = lock.with_name(f"{lock.name}.{os.getpid()}.tmp")
    tmp.write_text(str(os.getpid()))
    try:
        for _ in range(2):
            try:
                os.link(tmp, lock)
                return True
            except FileExistsError:
                try:
                    existing_pid = int(lock.read_text().strip())
                    if daemon.process_alive(existing_pid):
                        return False
                except (ValueError, OSError):
                    pass
                lock.unlink(missing_ok=True)  # stale/unreadable: reclaim
        return False
    finally:
        tmp.unlink(missing_ok=True)


def release_landing_lock(repo_dir: Path) -> None:
    lock = landing_lock_path(repo_dir)
    try:
        if lock.is_file():
            stored_pid = int(lock.read_text().strip())
            if stored_pid == os.getpid():
                lock.unlink()
    except (ValueError, OSError):
        pass


# --- Git plumbing ----------------------------------------------------------------------


def _git(repo_dir: Path, *args: str) -> subprocess.CompletedProcess:
    """All git goes through here (the test seam, like conductor._git)."""
    return subprocess.run(
        ["git", "-C", str(repo_dir), *args],
        capture_output=True,
        text=True,
    )


def _err(proc: subprocess.CompletedProcess) -> str:
    return ((proc.stderr or proc.stdout) or "").strip()


def default_landing_parent(repo_dir: Path) -> Path:
    """Default parent for the landing worktree. OUTSIDE the repo (see
    module docstring) and namespaced by repo name, beside the conductor's
    runs parent."""
    return Path.home() / "Desktop" / "marathon-runs" / "landing" / repo_dir.name


def _prepare_worktree(repo_dir: Path, worktree: Path, base: str) -> Optional[str]:
    """Fetch, ensure the marathon/next checkout exists, and align it to
    the remote tip. Returns an error message, or None when ready.

    Branch creation uses ``-b`` (never ``-B`` — the integration branch
    is never reset); the only ``reset --hard`` aligns the LOCAL worktree
    to ``origin/marathon/next``, discarding unpushed debris from a
    crashed or bounced previous attempt. A brand-new branch is published
    immediately so origin/marathon/next exists for every later align,
    bounce rollback, and promotion."""
    fetch = _git(repo_dir, "fetch", "origin")
    if fetch.returncode != 0:
        return f"git fetch failed: {_err(fetch)}"
    if (worktree / ".git").exists():
        head = _git(worktree, "rev-parse", "--abbrev-ref", "HEAD")
        branch = (head.stdout or "").strip()
        if branch != LANDING_BRANCH:
            return (
                f"landing worktree {worktree} is on {branch or '?'!r}, not "
                f"{LANDING_BRANCH!r}; remove the worktree and rerun"
            )
    else:
        local = _git(
            repo_dir, "rev-parse", "--verify", "--quiet",
            f"refs/heads/{LANDING_BRANCH}",
        )
        worktree.parent.mkdir(parents=True, exist_ok=True)
        if local.returncode == 0:
            add = _git(repo_dir, "worktree", "add", str(worktree), LANDING_BRANCH)
        else:
            remote = _git(
                repo_dir, "rev-parse", "--verify", "--quiet",
                f"refs/remotes/origin/{LANDING_BRANCH}",
            )
            start = (
                f"origin/{LANDING_BRANCH}" if remote.returncode == 0
                else f"origin/{base}"
            )
            add = _git(
                repo_dir, "worktree", "add", "-b", LANDING_BRANCH,
                str(worktree), start,
            )
        if add.returncode != 0:
            return f"git worktree add failed: {_err(add)}"
    remote = _git(
        worktree, "rev-parse", "--verify", "--quiet",
        f"refs/remotes/origin/{LANDING_BRANCH}",
    )
    if remote.returncode == 0:
        align = _git(worktree, "reset", "--hard", f"origin/{LANDING_BRANCH}")
        if align.returncode != 0:
            return f"could not align worktree to origin/{LANDING_BRANCH}: {_err(align)}"
        # Crash recovery: reset --hard never removes an untracked
        # landings.jsonl left by an attempt that died between the
        # record append and its commit (see _discard_untracked_record).
        _discard_untracked_record(worktree)
    else:
        publish = _git(worktree, "push", "--set-upstream", "origin", LANDING_BRANCH)
        if publish.returncode != 0:
            return f"could not publish new {LANDING_BRANCH}: {_err(publish)}"
    return None


def _resolve_commit(worktree: Path, ref: str) -> Optional[str]:
    """Resolve ``ref`` (SHA or branch; bare branch names fall back to
    their origin/ remote-tracking ref) to a full commit SHA."""
    for candidate in (ref, f"origin/{ref}"):
        proc = _git(
            worktree, "rev-parse", "--verify", "--quiet", f"{candidate}^{{commit}}"
        )
        sha = (proc.stdout or "").strip()
        if proc.returncode == 0 and sha:
            return sha
    return None


def _discard_untracked_record(worktree: Path) -> None:
    """``reset --hard`` leaves UNTRACKED files in place: on a branch
    where landings.jsonl is not yet tracked (the first-ever landing), a
    record appended but never committed — ``git add``/``commit`` failed,
    or a crash hit between append and commit — would survive every
    align and be silently folded into the NEXT landing's record commit
    as a phantom row (a landing that never pushed). Drop the file
    whenever it is not tracked."""
    tracked = _git(
        worktree, "ls-files", "--error-unmatch", LANDINGS_RELPATH.as_posix()
    )
    if tracked.returncode != 0:
        try:
            (Path(worktree) / LANDINGS_RELPATH).unlink(missing_ok=True)
        except OSError:
            pass


def _rollback(worktree: Path) -> None:
    """Bounce cleanup: realign the worktree to the remote tip. Best
    effort — the next attempt's _prepare_worktree aligns again anyway."""
    _git(worktree, "reset", "--hard", f"origin/{LANDING_BRANCH}")
    _discard_untracked_record(worktree)


# --- Bounce path -------------------------------------------------------------------------


def _write_bounce_report(
    repo_dir: Path, req: LandingRequest, klass: str, detail: str, requeued: bool
) -> Path:
    bdir = Path(repo_dir) / BOUNCES_RELPATH
    bdir.mkdir(parents=True, exist_ok=True)
    gitignore = bdir / ".gitignore"
    if not gitignore.is_file():
        gitignore.write_text("*\n")
    ts = datetime.now().strftime("%Y%m%d-%H%M%S-%f")
    path = bdir / f"{req.issue_num}-{ts}.md"
    path.write_text(
        f"# Landing bounce — issue #{req.issue_num}\n\n"
        f"- class: {klass}\n"
        f"- ts: {now_iso()}\n"
        f"- chapter: {req.chapter}\n"
        f"- source_ref: {req.source_ref}\n"
        f"- mode: {req.mode}\n"
        f"- workdir: {req.workdir}\n"
        f"- attempts: {req.attempts}\n"
        f"- requeued: {'yes' if requeued else 'no'}\n\n"
        f"## Detail\n\n{detail.strip()}\n"
    )
    return path


def _breaker_path(repo_dir: Path) -> Path:
    return Path(repo_dir) / BOUNCES_RELPATH / BREAKER_FILENAME


def _load_breaker(path: Path) -> dict:
    try:
        data = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError):
        data = {}
    if not isinstance(data.get("posted"), dict):
        data["posted"] = {}
    if not isinstance(data.get("daily"), dict):
        data["daily"] = {}
    return data


def _bounce_comment_body(
    req: LandingRequest, klass: str, detail: str, report_rel: str, requeued: bool
) -> str:
    excerpt = detail.strip()
    if len(excerpt) > 1500:
        excerpt = "… (truncated)\n" + excerpt[-1500:]
    requeue_line = (
        "The request was re-queued once (transient class)."
        if requeued
        else "The request was NOT re-queued."
    )
    return (
        f"**Landing bounced** (`{klass}`): the fix from this issue could not "
        f"land on `{LANDING_BRANCH}`. The landing worktree was rolled back to "
        f"`origin/{LANDING_BRANCH}`; nothing was pushed.\n\n"
        f"```\n{excerpt}\n```\n\n"
        f"{requeue_line} {_CLASS_HINTS[klass]}\n\n"
        f"Full report: `{report_rel}` (local to the landing machine)."
    )


def _maybe_post_bounce_comment(
    cfg: ReviewConfig,
    req: LandingRequest,
    klass: str,
    detail: str,
    report_path: Path,
    requeued: bool,
) -> bool:
    """Post ONE ``gh issue comment`` for this bounce — behind the
    circuit breaker: an identical failure signature is never posted
    twice (the daemon retry-comment disease was the human re-queuing
    #49 eleven times; the inverse — the machine re-nagging the human —
    is the same noise), and each issue gets at most
    ``BOUNCE_COMMENT_DAILY_CAP`` bounce comments per day. Suppressed or
    failed posts are warned on the console; the report file is the
    local record either way. Best-effort by design (the _notify_stall
    contract): a gh failure never crashes the runner and is not counted
    against the breaker, so the next bounce retries the notification."""
    signature = f"{req.issue_num}\n{klass}\n{detail.strip()}"
    sig_hash = hashlib.sha256(signature.encode("utf-8", "replace")).hexdigest()
    breaker = _breaker_path(cfg.repo_dir)
    state = _load_breaker(breaker)
    if sig_hash in state["posted"]:
        print(
            f"  bounce comment for #{req.issue_num} suppressed (identical "
            f"failure signature already posted: {sig_hash[:12]}); report at "
            f"{report_path}"
        )
        return False
    today = datetime.now().strftime("%Y-%m-%d")
    day_counts = state["daily"].get(today, {})
    posted_today = int(day_counts.get(str(req.issue_num), 0) or 0)
    if posted_today >= BOUNCE_COMMENT_DAILY_CAP:
        print(
            f"  bounce comment for #{req.issue_num} suppressed (daily cap of "
            f"{BOUNCE_COMMENT_DAILY_CAP} reached); report at {report_path}"
        )
        return False
    try:
        report_rel = str(report_path.relative_to(cfg.repo_dir))
    except ValueError:
        report_rel = str(report_path)
    body = _bounce_comment_body(req, klass, detail, report_rel, requeued)
    cmd = [
        "gh", "issue", "comment", str(req.issue_num),
        "--repo", cfg.github_repo,
        "--body", body,
    ]
    try:
        result = subprocess.run(cmd, capture_output=True, text=True)
        if result.returncode != 0:
            print(
                f"  warning: bounce comment for #{req.issue_num} failed "
                f"(gh exit {result.returncode}): {(result.stderr or '').strip()}"
            )
            return False
    except Exception as e:  # noqa: BLE001 — best-effort; see docstring
        print(f"  warning: bounce comment for #{req.issue_num} failed ({e})")
        return False
    state["posted"][sig_hash] = now_iso()
    # Keep only today's tallies: old days can never matter again and the
    # posted-signature set already provides the long-term dedup.
    state["daily"] = {today: {**day_counts, str(req.issue_num): posted_today + 1}}
    breaker.parent.mkdir(parents=True, exist_ok=True)
    breaker.write_text(json.dumps(state, indent=2) + "\n")
    return True


def _bounce(cfg: ReviewConfig, req: LandingRequest, klass: str, detail: str) -> str:
    """Record a failed landing: report file, circuit-broken gh comment,
    and — for transient classes only — one re-queue. Returns the
    outcome string ("requeued" or "bounced"). Cleanup (cherry-pick
    --abort / worktree rollback) already happened at the failure site."""
    requeue = klass in TRANSIENT_CLASSES and req.attempts < MAX_REQUEUES
    report_path = _write_bounce_report(cfg.repo_dir, req, klass, detail, requeue)
    if requeue:
        enqueue_landing(
            cfg.repo_dir,
            issue_num=req.issue_num,
            chapter=req.chapter,
            source_ref=req.source_ref,
            workdir=req.workdir,
            mode=req.mode,
            attempts=req.attempts + 1,
        )
    _maybe_post_bounce_comment(cfg, req, klass, detail, report_path, requeue)
    print(
        f"--- BOUNCED #{req.issue_num} ({klass}): report at {report_path}"
        + (" — re-queued once" if requeue else "")
        + " ---",
        flush=True,
    )
    return "requeued" if requeue else "bounced"


# --- One landing attempt --------------------------------------------------------------------


def _append_landing_record(worktree: Path, record: dict) -> Path:
    path = Path(worktree) / LANDINGS_RELPATH
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a") as f:
        f.write(json.dumps(record) + "\n")
    return path


def _land_one(
    cfg: ReviewConfig,
    req: LandingRequest,
    worktree: Path,
    base: str,
    build_timeout: int,
) -> str:
    """Attempt one landing. Returns "landed" | "bounced" | "requeued" |
    "dropped" (nothing to land). Every failure path rolls the worktree
    back to ``origin/marathon/next`` before bouncing, so a bounce can
    never leave half a cherry-pick for the next request to land on."""
    err = _prepare_worktree(cfg.repo_dir, worktree, base)
    if err is not None:
        return _bounce(cfg, req, CLASS_SETUP, err)

    src = _resolve_commit(worktree, req.source_ref)
    if src is None:
        return _bounce(
            cfg, req, CLASS_SETUP,
            f"source ref {req.source_ref!r} not found after fetch (the "
            "per-issue branch may have been reset by a newer iteration)",
        )
    rev_list = _git(worktree, "rev-list", "--reverse", f"HEAD..{src}")
    if rev_list.returncode != 0:
        return _bounce(cfg, req, CLASS_SETUP, f"rev-list failed: {_err(rev_list)}")
    shas = rev_list.stdout.split()
    if not shas:
        print(
            f"--- nothing to land for #{req.issue_num}: {src[:12]} is already "
            f"contained in {LANDING_BRANCH}; dropping the request ---",
            flush=True,
        )
        return "dropped"

    # Cherry-pick the job's commits (one per refine iteration; ranges
    # land oldest-first so multi-commit jobs apply in original order).
    pick = _git(worktree, "cherry-pick", *shas)
    if pick.returncode != 0:
        conflict_diff = _git(worktree, "diff").stdout or ""
        _git(worktree, "cherry-pick", "--abort")
        _rollback(worktree)
        detail = f"{_err(pick)}\n\n## Conflict diff\n\n{conflict_diff.strip()}"
        return _bounce(cfg, req, CLASS_CONFLICT, detail)

    # Hard gate, part 1: lake build (same semantics as the daemon's
    # build step — timeout kills the build and the landing bounces).
    build = run_lake_build(worktree, build_timeout)
    if build.skipped_reason:
        _rollback(worktree)
        return _bounce(
            cfg, req, CLASS_BUILD,
            f"lake build could not run: {build.skipped_reason}",
        )
    if not build.ok:
        _rollback(worktree)
        what = "timed out" if build.timed_out else "failed"
        return _bounce(
            cfg, req, CLASS_BUILD,
            f"lake build {what}\n\n{build.log_tail or '(no log tail captured)'}",
        )

    # Hard gate, part 2: the machine gate with ENFORCE semantics — a
    # fail verdict blocks, no override path (overrides belong to the
    # PR flow). Mode comes from the request (skeleton iterations carry
    # sorries legitimately).
    target_rel = cfg.target_path_template.format(chapter=req.chapter).strip("/")
    report = run_gate(
        worktree, Path(worktree) / target_rel, mode=req.mode, build_ok=True,
    )
    print(report.render_console(), flush=True)
    if report.verdict == "fail":
        _rollback(worktree)
        return _bounce(cfg, req, CLASS_GATE, report.render_markdown())

    head = _git(worktree, "rev-parse", "HEAD")
    next_sha = (head.stdout or "").strip()
    record = {
        "issue": req.issue_num,
        "sha_landed": src,
        "next_sha": next_sha,
        "ts": now_iso(),
        "build_secs": round(build.duration_seconds, 1),
    }
    _append_landing_record(worktree, record)
    add = _git(worktree, "add", "--", LANDINGS_RELPATH.as_posix())
    commit = _git(
        worktree, "commit", "-m",
        f"marathon landing: record #{req.issue_num} ({src[:12]})",
    )
    if add.returncode != 0 or commit.returncode != 0:
        _rollback(worktree)
        return _bounce(
            cfg, req, CLASS_SETUP,
            f"could not commit landing record: {_err(add) or _err(commit)}",
        )

    # Plain push — NEVER force. A rejection means the remote moved
    # (another writer); that's the one transient class: re-queue once
    # and let the next attempt fetch + re-cherry-pick onto the new tip.
    push = _git(worktree, "push", "origin", LANDING_BRANCH)
    if push.returncode != 0:
        _rollback(worktree)
        return _bounce(cfg, req, CLASS_PUSH, _err(push) or "push rejected")

    print(
        f"--- LANDED #{req.issue_num}: {src[:12]} → {LANDING_BRANCH} "
        f"({next_sha[:12]}, build {record['build_secs']}s) ---",
        flush=True,
    )
    return "landed"


# --- Runner loop ------------------------------------------------------------------------------


def run_landing(
    repo_dir: Optional[Path] = None,
    *,
    once: bool = False,
    worktree_parent: Optional[Path] = None,
    base: str = DEFAULT_BASE_BRANCH,
    build_timeout: int = DEFAULT_BUILD_TIMEOUT_SECONDS,
) -> int:
    """``marathon landing run``: pop requests oldest-first and land each
    serially (the build+gate is the serial section by design — landings
    onto one branch cannot parallelize). ``--once`` drains the queue and
    exits; otherwise polls every ``LANDING_POLL_SECONDS``. Crash-safe by
    construction: every attempt starts with fetch + align-to-remote, so
    an interrupted attempt leaves only local debris the next attempt
    discards."""
    cfg = load_config(repo_dir)
    parent = (
        Path(worktree_parent) if worktree_parent
        else default_landing_parent(cfg.repo_dir)
    ).expanduser().resolve()
    repo_resolved = cfg.repo_dir.resolve()
    if parent == repo_resolved or parent.is_relative_to(repo_resolved):
        sys.exit(
            f"landing worktree parent {parent} is inside the repo "
            f"{repo_resolved}; in-repo worktrees leak into Aristotle bundles "
            "via `git ls-files --others` — pick a parent outside the repo"
        )
    worktree = parent / LANDING_WORKTREE_NAME

    if not acquire_landing_lock(cfg.repo_dir):
        print(
            f"another landing runner is already active (lock at "
            f"{landing_lock_path(cfg.repo_dir)}); exiting"
        )
        return 0

    mode = "one-shot" if once else "daemon"
    print(
        f"=== landing runner pid={os.getpid()} mode={mode} "
        f"branch={LANDING_BRANCH} worktree={worktree} "
        f"starting {datetime.now().isoformat()} ===",
        flush=True,
    )
    outcomes: dict[str, int] = {}
    try:
        while True:
            req = pop_oldest_request(cfg.repo_dir)
            if req is None:
                if once:
                    print("\n=== one-shot mode: queue drained; exiting ===", flush=True)
                    break
                _sleep(LANDING_POLL_SECONDS)
                continue
            print(
                f"\n--- landing #{req.issue_num} (chapter {req.chapter}, "
                f"source {req.source_ref[:12]}, mode {req.mode}, "
                f"attempt {req.attempts + 1}) ---",
                flush=True,
            )
            outcome = _land_one(cfg, req, worktree, base, build_timeout)
            outcomes[outcome] = outcomes.get(outcome, 0) + 1
    finally:
        release_landing_lock(cfg.repo_dir)
        summary = ", ".join(f"{v} {k}" for k, v in sorted(outcomes.items())) or "idle"
        print(
            f"\n=== landing runner done {datetime.now().isoformat()} "
            f"({summary}) ===",
            flush=True,
        )
    return 0


# --- Promotion ---------------------------------------------------------------------------------


def promote(repo_dir: Optional[Path] = None, base: str = DEFAULT_BASE_BRANCH) -> int:
    """``marathon landing promote``: fast-forward-only promotion of
    ``origin/marathon/next`` into ``origin/<base>``, refused with a
    divergence summary otherwise. Implemented as a server-side ff push
    of the remote tip SHA — no local checkout is touched, so it works
    regardless of what the operator has checked out. No force flag
    exists on this path; the remote's own non-ff rejection is a second
    line of defense."""
    repo = Path(repo_dir) if repo_dir else find_repo_dir()
    fetch = _git(repo, "fetch", "origin")
    if fetch.returncode != 0:
        print(f"git fetch failed: {_err(fetch)}")
        return 1
    nxt = _git(
        repo, "rev-parse", "--verify", "--quiet",
        f"refs/remotes/origin/{LANDING_BRANCH}",
    )
    next_sha = (nxt.stdout or "").strip()
    if nxt.returncode != 0 or not next_sha:
        print(
            f"origin/{LANDING_BRANCH} does not exist — nothing to promote "
            "(run `marathon landing run` first)"
        )
        return 1
    counts = _git(
        repo, "rev-list", "--left-right", "--count",
        f"origin/{base}...origin/{LANDING_BRANCH}",
    )
    try:
        base_only, next_only = (int(n) for n in counts.stdout.split())
    except (ValueError, AttributeError):
        print(f"could not compute divergence: {_err(counts)}")
        return 1
    if next_only == 0:
        print(
            f"origin/{LANDING_BRANCH} has no commits beyond origin/{base}; "
            "nothing to promote"
        )
        return 0
    if base_only > 0:
        print(
            f"REFUSED: origin/{base} and origin/{LANDING_BRANCH} have "
            "diverged — not fast-forwardable."
        )
        print(f"  origin/{base}: {base_only} commit(s) not on {LANDING_BRANCH}")
        print(f"  origin/{LANDING_BRANCH}: {next_only} commit(s) not on {base}")
        print(
            "  Reconcile first (land the base-side commits onto "
            f"{LANDING_BRANCH}, or merge manually); promotion is never forced."
        )
        return 2
    push = _git(repo, "push", "origin", f"{next_sha}:refs/heads/{base}")
    if push.returncode != 0:
        print(f"promotion push failed: {_err(push)}")
        return 1
    print(
        f"promoted: origin/{base} fast-forwarded to {next_sha[:12]} "
        f"({next_only} commit(s))"
    )
    return 0


# --- Status -------------------------------------------------------------------------------------


def _age_str(iso_ts: str) -> str:
    try:
        then = datetime.fromisoformat(iso_ts)
    except (ValueError, TypeError):
        return "?"
    now = datetime.now(then.tzinfo) if then.tzinfo else datetime.now()
    secs = max(0, int((now - then).total_seconds()))
    if secs < 3600:
        return f"{secs // 60}m"
    if secs < 86400:
        return f"{secs // 3600}h{(secs % 3600) // 60:02d}m"
    return f"{secs // 86400}d{(secs % 86400) // 3600}h"


def _tail_jsonl(path: Path, n: int) -> list[dict]:
    if not path.is_file():
        return []
    rows: list[dict] = []
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            rows.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return rows[-n:]


def print_landing_status(
    repo_dir: Path, worktree_parent: Optional[Path] = None
) -> int:
    """``marathon landing status``: queue depth + ages, last landings,
    last bounces, lock holder — all from files, no git or network."""
    repo_dir = Path(repo_dir)
    parent = (
        Path(worktree_parent) if worktree_parent
        else default_landing_parent(repo_dir)
    ).expanduser()
    worktree = parent / LANDING_WORKTREE_NAME

    qdir = repo_dir / QUEUE_RELPATH
    requests: list[LandingRequest] = []
    if qdir.is_dir():
        for path in sorted(qdir.glob("*.json")):
            try:
                requests.append(LandingRequest.from_json(json.loads(path.read_text())))
            except (json.JSONDecodeError, TypeError, ValueError):
                print(f"  warning: unreadable queue file {path.name}")
    print(f"queue: {len(requests)} request(s)")
    for r in requests:
        print(
            f"  #{r.issue_num:<5} c{r.chapter:<3} mode={r.mode:<8} "
            f"age={_age_str(r.enqueued_ts):<8} attempts={r.attempts} "
            f"source={r.source_ref[:12]}"
        )

    # The tracked record rides marathon/next, so the landing worktree's
    # copy is authoritative; the repo checkout has it only after a
    # promotion reaches the operator's branch.
    landings_path = worktree / LANDINGS_RELPATH
    if not landings_path.is_file():
        landings_path = repo_dir / LANDINGS_RELPATH
    landings = _tail_jsonl(landings_path, 5)
    print(f"last {len(landings)} landing(s):" if landings else "no landings recorded")
    for row in landings:
        print(
            f"  #{row.get('issue', '?'):<5} {str(row.get('sha_landed', '?'))[:12]} "
            f"→ next {str(row.get('next_sha', '?'))[:12]}  "
            f"build {row.get('build_secs', '?')}s  {row.get('ts', '?')}"
        )

    bdir = repo_dir / BOUNCES_RELPATH
    reports = sorted(bdir.glob("*.md")) if bdir.is_dir() else []
    print(f"last {min(len(reports), 5)} bounce(s):" if reports else "no bounces recorded")
    for path in reports[-5:]:
        print(f"  {path.name}")

    lock = landing_lock_path(repo_dir)
    if lock.is_file():
        try:
            pid = int(lock.read_text().strip())
            alive = daemon.process_alive(pid)
            print(f"lock: held by pid {pid} ({'alive' if alive else 'DEAD — stale'})")
        except (ValueError, OSError):
            print(f"lock: unreadable ({lock})")
    else:
        print("lock: free")
    return 0
