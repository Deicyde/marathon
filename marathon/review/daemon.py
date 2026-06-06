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
        record_iteration(<issue_num>)   # mark as iterated regardless of
                                        # success — the human re-rejects
                                        # to re-queue on failure
    sleep, then re-poll.

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
    record_iteration,
)

# Polling interval (seconds) when the runner is in daemon mode and the
# queue is currently drained.
POLL_INTERVAL_SECONDS = 60

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
    "--auto-referee-every", "1",
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


def run_daemon(chapter: int, once: bool = False) -> int:
    """Run the daemon loop for ``chapter``. Returns final exit code.

    Per-issue dispatch loop (see module docstring for the rationale):
    on each tick, pick the oldest pending rejection that still needs
    an iteration (``last_iteration_ts is None`` or older than
    ``verdict_ts``), dispatch a single ``marathon refine`` with
    ``--review-rejection N``, and mark the issue iterated regardless of
    refine outcome. The human re-rejects to re-queue if an iteration
    didn't actually fix things.
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
                # Decide whether to record this as an attempted iteration:
                # * Clean exit (0): always record.
                # * Non-zero exit: record, to avoid infinite retry loops
                #   on persistent failures (the human re-rejects to
                #   re-queue).
                # * BUT: if the daemon itself received a stop signal
                #   while the refine was running, the iteration was
                #   killed prematurely — likely because the user wanted
                #   to swap code or pause work, not because the refine
                #   actually failed. Don't record in that case; the
                #   next daemon launch will pick the same issue up.
                if _STOP_REQUESTED and exit_code != 0:
                    print(
                        f"--- iteration for #{target_issue} interrupted "
                        f"by daemon stop signal (exit {exit_code}); NOT "
                        "marked iterated. Next daemon launch will "
                        "re-dispatch for this issue.",
                        flush=True,
                    )
                else:
                    record_iteration(cfg, target_issue)
                    if exit_code != 0:
                        print(
                            f"--- iteration for #{target_issue} exited "
                            f"non-zero ({exit_code}); marked iterated to "
                            "avoid retry loop. Re-reject the issue to "
                            "re-queue.",
                            flush=True,
                        )
                if once and iteration_count >= MAX_LOOPS_ONCE:
                    print(
                        f"\n=== one-shot mode: safety cap of {MAX_LOOPS_ONCE} "
                        "iterations reached; exiting ===",
                        flush=True,
                    )
                    break
                # No sleep — go check the queue again immediately for
                # any other pending rejections (including ones that
                # arrived during this iteration).
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
                # Sleep in small chunks so a stop signal is responsive.
                for _ in range(POLL_INTERVAL_SECONDS):
                    if _STOP_REQUESTED:
                        break
                    time.sleep(1)
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
    args = p.parse_args(argv)
    return run_daemon(chapter=args.chapter, once=args.once)


if __name__ == "__main__":
    raise SystemExit(main())
