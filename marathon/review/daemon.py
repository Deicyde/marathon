"""Single-flight auto-refine daemon for the review rejection queue.

Triggered indirectly by ``reject`` (via ``marathon review reject``) when
no other daemon is active for the chapter. While the daemon is alive,
subsequent rejections write to ``.marathon/review/state.json``; the
daemon's next loop iteration picks them up by polling
``pending_rejections_needing_iteration`` for this chapter.

Loop semantics — *one rejection per iteration, dispatched explicitly*::

    while a pending rejection still needs iteration:
        pick the oldest one (by verdict_ts)
        run one `marathon refine --skeleton --max-iterations 1 \\
            --review-rejection <issue_num>` iteration
        if it exited 0:
            record_iteration(<issue_num>)   # human verdict (verify /
                                            # re-reject) is the gate
        elif the daemon got a stop signal mid-run:
            record nothing                  # interrupted ≠ failed; next
                                            # daemon launch re-dispatches
        else:
            increment the issue's attempt counter; retry the SAME
            issue after an exponential backoff (2^attempts * 60s,
            capped). After --max-attempts consecutive failures, mark
            the entry "stalled" (drops out of the dispatch queue) and
            post ONE `gh issue comment` notification; re-rejecting
            resets the counter and re-queues.
    sleep, then re-poll.

Failed dispatches used to be marked "iterated" anyway — consuming the
rejection and requiring the human to notice the silence and re-reject
(GeometricAnalysis issue #49 accumulated 11 manual re-queue comments
this way). The retry/stall path above replaces that contract: the
rejection is only ever consumed by a clean refine exit or a fresh
human verdict, never by a crash.

The earlier single-hash design treated the whole pending-rejection
set as one batch and marked every rejection "processed" after one
iteration that often addressed only one of them — see
``/tmp/marathon-daemon-queue-bug.md`` (now resolved). The current
per-issue dispatch eliminates that failure mode at both layers: the
daemon picks exactly one, and Hermes' prompt contains exactly that
one in its "Actionable review queue" section.

Per-chapter lock file lives at
``<repo>/.marathon/review/runner-locks/refine-c<N>.lock`` and contains
the daemon's PID. Cleaned up on exit.
"""

from __future__ import annotations

import argparse
import os
import signal
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path
from typing import Optional

from marathon.review.config import ReviewConfig, load_config
from marathon.review.state import (
    hash_pending,
    pending_rejections_needing_iteration,
    record_failed_attempt,
    record_iteration,
    record_stall,
)

# Polling interval (seconds) when the runner is in daemon mode and the
# queue is currently drained.
POLL_INTERVAL_SECONDS = 60

# Retry budget for failed refine dispatches. After this many consecutive
# non-zero refine exits for the same rejection, the daemon marks the
# queue entry "stalled", posts one notification comment on the GitHub
# issue, and moves on. Overridable via ``--max-attempts``.
DEFAULT_MAX_ATTEMPTS = 3

# Exponential backoff before re-dispatching a failed rejection:
# 2^attempts * BASE, capped at CAP. With the defaults that's 2 min
# after the first failure and 4 min after the second — enough for
# transient causes (API hiccup, dirty checkout from a concurrent
# command) to clear, without holding the queue hostage for hours.
BACKOFF_BASE_SECONDS = 60
BACKOFF_CAP_SECONDS = 3600

# Safety cap on consecutive iterations when running in ``--once`` mode.
# Daemon mode has no cap (loops forever, picking up new bullets as they
# arrive).
MAX_LOOPS_ONCE = 5

# Default ``marathon refine`` flags the daemon uses. Each value can be
# overridden via the ``[daemon.refine_args]`` table in review config
# (not currently wired — left as a hook for future tuning).
DEFAULT_REFINE_ARGS: list[str] = [
    "--skeleton", "--max-iterations", "1", "--max-retries", "3",
    "--auto-build", "--build-timeout", "1800",
    "--auto-commit", "--auto-push", "--auto-rate",
    # ``--auto-referee-every`` deliberately omitted: Claude no longer
    # auto-refreshes referee.md from daemon iterations. Standing items
    # are tracked in standing-items.md instead (run ``marathon referee``
    # manually if you want a refresh). Removing the auto-refresh prevents
    # the machine-managed priority tail from competing with explicit
    # human reject notes for Aristotle's marquee move.
    "--audit-verified",
    # Daemon iterations always go through a marathon-owned branch +
    # PR. Solves the stranded-commit failure mode where the daemon
    # would auto-commit to whatever branch happened to be checked
    # out — with --auto-pr the daemon uses
    # `marathon/refine-c<N>-i<issue>` regardless of HEAD.
    "--auto-pr",
]

# Set by SIGTERM/SIGINT to break the daemon's poll loop. Module-level so
# the signal handlers can flip it without a closure.
_STOP_REQUESTED = False


def _handle_stop_signal(signum, frame) -> None:
    global _STOP_REQUESTED
    _STOP_REQUESTED = True
    print(
        f"\n--- stop signal received (signum={signum}); will exit after current iteration ---",
        flush=True,
    )


def lock_path(cfg: ReviewConfig, chapter: int) -> Path:
    return cfg.runner_lock_dir / f"refine-c{chapter}.lock"


def hash_user_header(cfg: ReviewConfig, chapter: Optional[int] = None) -> str:
    """SHA256 of the pending-rejections content for ``chapter`` (or all
    chapters when None). Returns '' when there are no pending rejections.

    Name retained for callsite compatibility; the underlying source is
    now ``.marathon/review/state.json`` (per-issue queue) instead of
    ``referee.md``'s user-managed header. ``referee.md`` is back to
    being purely the project rubric layer."""
    return hash_pending(cfg, chapter)


def process_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
        return True
    except (OSError, ProcessLookupError):
        return False


def acquire_lock(cfg: ReviewConfig, chapter: int) -> bool:
    """Try to acquire the per-chapter runner lock.

    Returns False if another *live* daemon is already active for this
    chapter (stale locks pointing to dead PIDs are reclaimed).
    """
    cfg.runner_lock_dir.mkdir(parents=True, exist_ok=True)
    lock = lock_path(cfg, chapter)
    if lock.is_file():
        try:
            existing_pid = int(lock.read_text().strip())
            if process_alive(existing_pid):
                return False
        except (ValueError, OSError):
            pass
        lock.unlink(missing_ok=True)
    lock.write_text(str(os.getpid()))
    return True


def release_lock(cfg: ReviewConfig, chapter: int) -> None:
    lock = lock_path(cfg, chapter)
    try:
        if lock.is_file():
            stored_pid = int(lock.read_text().strip())
            if stored_pid == os.getpid():
                lock.unlink()
    except (ValueError, OSError):
        pass


# Workdir parent for per-iteration refine runs. Lives under the user's
# Desktop (matches the old refine_runner.py default). Could be made
# configurable later.
def _workdir_parent() -> Path:
    return Path.home() / "Desktop" / "marathon-runs" / "review-fixes"


def run_one_refine(
    cfg: ReviewConfig, chapter: int, focus_issue: int
) -> int:
    """Run a single ``marathon refine`` iteration for the chapter,
    focused on a single pending rejection.

    ``focus_issue`` is forwarded as ``--review-rejection N`` so the
    refine command filters its pending-rejections context down to that
    one issue; Hermes then sees a queue of exactly one rejection in
    its prompt, removing the prior "Aristotle picks one and silently
    ignores the rest" failure mode.

    Returns the subprocess exit code. Invokes ``python -m marathon
    refine`` from the current environment so the daemon and Marathon
    share an aristotlelib version.
    """
    ts = datetime.now().strftime("%Y%m%d-%H%M%S")
    workdir = _workdir_parent() / f"c{chapter}-i{focus_issue}-{ts}"
    workdir.mkdir(parents=True, exist_ok=True)
    target = cfg.target_path(chapter)

    cmd = [
        sys.executable, "-m", "marathon", "refine",
        str(target),
        "--repo-dir", str(cfg.repo_dir),
        "--workdir", str(workdir),
        "--review-rejection", str(focus_issue),
        *DEFAULT_REFINE_ARGS,
    ]
    print(
        f"\n--- marathon refine starting (focus #{focus_issue}): "
        f"workdir={workdir.name} ---"
    )
    print(f"    target = {target}")
    print(f"    cmd    = {' '.join(cmd)}")
    sys.stdout.flush()
    # Inherit cwd — ``python -m marathon`` works from anywhere as long
    # as the marathon package is importable in the current Python env.
    result = subprocess.run(cmd)
    print(f"--- marathon refine exit {result.returncode} ---", flush=True)
    return result.returncode


def _interruptible_sleep(seconds: int) -> None:
    """Sleep in 1s chunks so a SIGTERM/SIGINT is responsive mid-sleep.

    Used for both the drained-queue poll interval and the failed-
    dispatch backoff."""
    for _ in range(seconds):
        if _STOP_REQUESTED:
            return
        time.sleep(1)


def _backoff_seconds(attempts: int) -> int:
    """Exponential backoff after ``attempts`` consecutive failures:
    ``2^attempts * BACKOFF_BASE_SECONDS``, capped at
    ``BACKOFF_CAP_SECONDS``."""
    return min(2 ** attempts * BACKOFF_BASE_SECONDS, BACKOFF_CAP_SECONDS)


def _notify_stall(cfg: ReviewConfig, issue_num: int, attempts: int) -> None:
    """Post a one-time comment on the GitHub issue telling the human the
    rejection stalled and how to re-queue it.

    Posted exactly once per stall: ``record_stall`` drops the entry from
    the dispatch queue, so the daemon never reaches this path again for
    the same verdict. Best-effort by design — a notification failure
    (gh missing, network down, auth expired) is printed and swallowed,
    never allowed to crash the daemon; the "stalled" state in
    state.json is the durable record either way."""
    body = (
        f"**Auto-refine stalled.** The review daemon failed to dispatch a "
        f"refine iteration for this rejection {attempts} times in a row and "
        f"has stopped retrying it.\n\n"
        f"Check the daemon log for the underlying error. Re-rejecting this "
        f"issue (`marathon review reject {issue_num} ...`) resets the "
        f"attempt counter and re-queues it."
    )
    cmd = [
        "gh", "issue", "comment", str(issue_num),
        "--repo", cfg.github_repo,
        "--body", body,
    ]
    try:
        result = subprocess.run(cmd, capture_output=True, text=True)
        if result.returncode != 0:
            print(
                f"  warning: stall notification for #{issue_num} failed "
                f"(gh exit {result.returncode}): {result.stderr.strip()}",
                flush=True,
            )
    except Exception as e:  # noqa: BLE001 — best-effort; see docstring.
        print(
            f"  warning: stall notification for #{issue_num} failed ({e})",
            flush=True,
        )


def _handle_refine_exit(
    cfg: ReviewConfig,
    issue_num: int,
    exit_code: int,
    *,
    max_attempts: int,
    interrupted: bool,
) -> int:
    """Apply the post-refine state transition for ``issue_num``; return
    the backoff (seconds) to sleep before the next dispatch (0 ⇒ none).

    Extracted from ``run_daemon`` so the retry/stall decision table is
    unit-testable without a real subprocess or poll loop:

    * ``interrupted`` — the daemon received a stop signal while the
      refine was running and the refine died non-zero. The kill was
      almost certainly the user pausing work, not a refine failure:
      record NOTHING (no iteration, no attempt), so the next daemon
      launch re-dispatches the same issue.
    * clean exit (0) — ``record_iteration``, exactly the historical
      contract: the human verdict (verify / re-reject) is the gate on
      whether the fix actually landed.
    * non-zero exit — do NOT consume the rejection. Increment the
      attempt counter; the issue still ``needs_iteration`` so the loop
      re-picks it after the returned backoff. Once ``max_attempts``
      consecutive failures accumulate, mark the entry "stalled"
      (drops it from the queue), post one notification comment, and
      return 0 so any other queued rejections get attention
      immediately.
    """
    if interrupted:
        print(
            f"--- iteration for #{issue_num} interrupted by daemon stop "
            f"signal (exit {exit_code}); NOT recorded. Next daemon launch "
            "will re-dispatch for this issue.",
            flush=True,
        )
        return 0

    if exit_code == 0:
        record_iteration(cfg, issue_num)
        return 0

    entry = record_failed_attempt(cfg, issue_num)
    if entry is None:
        # The entry vanished mid-iteration (e.g. a concurrent `verify`
        # cleared it). Nothing left to retry against.
        return 0

    if entry.attempts >= max_attempts:
        record_stall(cfg, issue_num)
        print(
            f"--- iteration for #{issue_num} exited non-zero ({exit_code}); "
            f"attempt {entry.attempts}/{max_attempts} — STALLED. Posting "
            "notification; re-reject the issue to re-queue.",
            flush=True,
        )
        _notify_stall(cfg, issue_num, entry.attempts)
        return 0

    backoff = _backoff_seconds(entry.attempts)
    print(
        f"--- iteration for #{issue_num} exited non-zero ({exit_code}); "
        f"attempt {entry.attempts}/{max_attempts} — will retry after "
        f"{backoff}s backoff.",
        flush=True,
    )
    return backoff


def run_daemon(
    chapter: int, once: bool = False, max_attempts: int = DEFAULT_MAX_ATTEMPTS
) -> int:
    """Run the daemon loop for ``chapter``. Returns final exit code.

    Per-issue dispatch loop (see module docstring for the rationale):
    on each tick, pick the oldest pending rejection that still needs
    an iteration (``last_iteration_ts is None`` or older than
    ``verdict_ts``) and dispatch a single ``marathon refine`` with
    ``--review-rejection N``. The outcome handling lives in
    :func:`_handle_refine_exit`: clean exits are marked iterated (the
    human re-rejects if the fix didn't land); failed exits are retried
    with exponential backoff up to ``max_attempts`` times, then
    stalled + notified.
    """
    cfg = load_config()

    if not acquire_lock(cfg, chapter):
        print(f"another refine daemon is already active for chapter {chapter}; exiting")
        return 0

    signal.signal(signal.SIGTERM, _handle_stop_signal)
    signal.signal(signal.SIGINT, _handle_stop_signal)

    mode = "one-shot" if once else "daemon"
    print(
        f"=== review refine daemon chapter={chapter} pid={os.getpid()} "
        f"mode={mode} starting {datetime.now().isoformat()} ===",
        flush=True,
    )

    iteration_count = 0

    try:
        while not _STOP_REQUESTED:
            queue = pending_rejections_needing_iteration(cfg, chapter)

            if queue:
                target_issue, target_state = queue[0]
                iteration_count += 1
                print(
                    f"\n=== iteration {iteration_count}: dispatching for "
                    f"#{target_issue} (verdict {target_state.verdict_ts}; "
                    f"{len(queue) - 1} other rejection(s) queued behind) ===",
                    flush=True,
                )
                exit_code = run_one_refine(cfg, chapter, focus_issue=target_issue)
                # A stop signal received while the refine was running
                # means the non-zero exit (if any) reflects the kill,
                # not a real refine failure — likely the user pausing
                # work. _handle_refine_exit records nothing in that
                # case so the next daemon launch re-dispatches.
                interrupted = _STOP_REQUESTED and exit_code != 0
                backoff = _handle_refine_exit(
                    cfg,
                    target_issue,
                    exit_code,
                    max_attempts=max_attempts,
                    interrupted=interrupted,
                )
                if once and iteration_count >= MAX_LOOPS_ONCE:
                    print(
                        f"\n=== one-shot mode: safety cap of {MAX_LOOPS_ONCE} "
                        "iterations reached; exiting ===",
                        flush=True,
                    )
                    break
                if backoff:
                    # Failed dispatch, retry budget not yet exhausted:
                    # the issue still needs_iteration, so the next tick
                    # re-picks it. Back off first so transient causes
                    # can clear (sleep is stop-signal responsive).
                    print(
                        f"\n--- backing off {backoff}s before "
                        f"re-dispatching #{target_issue} ---",
                        flush=True,
                    )
                    _interruptible_sleep(backoff)
                # Otherwise no sleep — go check the queue again
                # immediately for any other pending rejections
                # (including ones that arrived during this iteration).
            elif once:
                print(
                    "\n=== one-shot mode: queue drained; exiting ===",
                    flush=True,
                )
                break
            else:
                print(
                    f"\n--- queue drained; sleeping {POLL_INTERVAL_SECONDS}s ---",
                    flush=True,
                )
                _interruptible_sleep(POLL_INTERVAL_SECONDS)
    finally:
        release_lock(cfg, chapter)
        print(
            f"\n=== review refine daemon chapter={chapter} done "
            f"{datetime.now().isoformat()} (ran {iteration_count} iteration(s)) ===",
            flush=True,
        )

    return 0


def main(argv: Optional[list[str]] = None) -> int:
    """Standalone entry point. ``marathon review daemon`` wires this up.

    Also used by the backward-compat shim at
    ``<repo>/.marathon/review/refine_runner.py``.
    """
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--chapter", type=int, required=True)
    p.add_argument(
        "--once",
        action="store_true",
        help=(
            "Process queue once then exit (legacy single-flight semantics, "
            f"max {MAX_LOOPS_ONCE} iterations). Default: run as a daemon, "
            f"polling every {POLL_INTERVAL_SECONDS}s."
        ),
    )
    p.add_argument(
        "--max-attempts",
        type=int,
        default=DEFAULT_MAX_ATTEMPTS,
        help=(
            "Consecutive failed refine dispatches tolerated per rejection "
            "before its queue entry is marked stalled and a notification "
            f"comment is posted on the issue (default {DEFAULT_MAX_ATTEMPTS}). "
            "Re-rejecting a stalled issue resets the counter and re-queues it."
        ),
    )
    args = p.parse_args(argv)
    return run_daemon(
        chapter=args.chapter, once=args.once, max_attempts=args.max_attempts
    )


if __name__ == "__main__":
    raise SystemExit(main())
