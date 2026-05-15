"""Single-flight auto-refine daemon for the coreview rejection queue.

Triggered indirectly by ``reject`` (via ``marathon coreview reject``) when
no other daemon is active for the chapter. While the daemon is alive,
subsequent rejections just append to ``referee.md``'s user-managed
header (the queue); the daemon's next loop iteration picks them up.

Loop semantics::

    while user-header hash changed since previous iteration started:
        run one `marathon refine --skeleton --max-iterations 1` iteration
    (safety cap at ``MAX_LOOPS_ONCE`` in one-shot mode.)

Per-chapter lock file lives at
``<repo>/.marathon/coreview/runner-locks/refine-c<N>.lock`` and contains
the daemon's PID. Cleaned up on exit.
"""

from __future__ import annotations

import argparse
import hashlib
import os
import signal
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path
from typing import Optional

from marathon.coreview.config import CoreviewConfig, load_config
from marathon.coreview.referee_queue import SENTINEL

# Polling interval (seconds) when the runner is in daemon mode and the
# queue is currently drained.
POLL_INTERVAL_SECONDS = 60

# Safety cap on consecutive iterations when running in ``--once`` mode.
# Daemon mode has no cap (loops forever, picking up new bullets as they
# arrive).
MAX_LOOPS_ONCE = 5

# Default ``marathon refine`` flags the daemon uses. Each value can be
# overridden via the ``[daemon.refine_args]`` table in coreview config
# (not currently wired — left as a hook for future tuning).
DEFAULT_REFINE_ARGS: list[str] = [
    "--skeleton", "--max-iterations", "1", "--max-retries", "3",
    "--auto-build", "--build-timeout", "1800",
    "--auto-commit", "--auto-push", "--auto-rate",
    "--auto-referee-every", "1",
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


def lock_path(cfg: CoreviewConfig, chapter: int) -> Path:
    return cfg.runner_lock_dir / f"refine-c{chapter}.lock"


def hash_user_header(cfg: CoreviewConfig) -> str:
    """SHA256 of ``referee.md``'s user-managed header (above the BEGIN
    sentinel). Returns '' if the file doesn't exist."""
    if not cfg.referee_path.is_file():
        return ""
    text = cfg.referee_path.read_text()
    idx = text.find(SENTINEL)
    user_header = text[:idx] if idx >= 0 else text
    return hashlib.sha256(user_header.encode()).hexdigest()


def process_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
        return True
    except (OSError, ProcessLookupError):
        return False


def acquire_lock(cfg: CoreviewConfig, chapter: int) -> bool:
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


def release_lock(cfg: CoreviewConfig, chapter: int) -> None:
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
    return Path.home() / "Desktop" / "marathon-runs" / "coreview-fixes"


def run_one_refine(cfg: CoreviewConfig, chapter: int) -> int:
    """Run a single ``marathon refine`` iteration for the chapter.

    Returns the subprocess exit code. Invokes ``python -m marathon refine``
    from the current environment so the daemon and Marathon share an
    aristotlelib version.
    """
    ts = datetime.now().strftime("%Y%m%d-%H%M%S")
    workdir = _workdir_parent() / f"c{chapter}-{ts}"
    workdir.mkdir(parents=True, exist_ok=True)
    target = cfg.target_path(chapter)

    cmd = [
        sys.executable, "-m", "marathon", "refine",
        str(target),
        "--repo-dir", str(cfg.repo_dir),
        "--workdir", str(workdir),
        *DEFAULT_REFINE_ARGS,
    ]
    print(f"\n--- marathon refine starting: workdir={workdir.name} ---")
    print(f"    target = {target}")
    print(f"    cmd    = {' '.join(cmd)}")
    sys.stdout.flush()
    # Inherit cwd — ``python -m marathon`` works from anywhere as long
    # as the marathon package is importable in the current Python env.
    result = subprocess.run(cmd)
    print(f"--- marathon refine exit {result.returncode} ---", flush=True)
    return result.returncode


def run_daemon(chapter: int, once: bool = False) -> int:
    """Run the daemon loop for ``chapter``. Returns final exit code."""
    cfg = load_config()

    if not acquire_lock(cfg, chapter):
        print(f"another refine daemon is already active for chapter {chapter}; exiting")
        return 0

    signal.signal(signal.SIGTERM, _handle_stop_signal)
    signal.signal(signal.SIGINT, _handle_stop_signal)

    mode = "one-shot" if once else "daemon"
    print(
        f"=== coreview refine daemon chapter={chapter} pid={os.getpid()} "
        f"mode={mode} starting {datetime.now().isoformat()} ===",
        flush=True,
    )

    last_processed_hash: Optional[str] = None
    iteration_count = 0

    try:
        while not _STOP_REQUESTED:
            current_hash = hash_user_header(cfg)

            if current_hash != last_processed_hash:
                iteration_count += 1
                print(
                    f"\n=== iteration {iteration_count}: referee.md user-header "
                    f"changed (hash={current_hash[:8]}); firing marathon refine ===",
                    flush=True,
                )
                run_one_refine(cfg, chapter)
                # Re-hash AFTER the run; new bullets that arrived during
                # the run cause the next loop iteration to fire again
                # immediately with no sleep.
                last_processed_hash = hash_user_header(cfg)
                if once and iteration_count >= MAX_LOOPS_ONCE:
                    print(
                        f"\n=== one-shot mode: safety cap of {MAX_LOOPS_ONCE} "
                        "iterations reached; exiting ===",
                        flush=True,
                    )
                    break
            elif once:
                print(
                    f"\n=== one-shot mode: queue stable (hash={current_hash[:8]}); exiting ===",
                    flush=True,
                )
                break
            else:
                print(
                    f"\n--- queue stable (hash={current_hash[:8]}); "
                    f"sleeping {POLL_INTERVAL_SECONDS}s ---",
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
            f"\n=== coreview refine daemon chapter={chapter} done "
            f"{datetime.now().isoformat()} (ran {iteration_count} iteration(s)) ===",
            flush=True,
        )

    return 0


def main(argv: Optional[list[str]] = None) -> int:
    """Standalone entry point. ``marathon coreview daemon`` wires this up.

    Also used by the backward-compat shim at
    ``<repo>/.marathon/coreview/refine_runner.py``.
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
