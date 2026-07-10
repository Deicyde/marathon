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
    load_state,
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
    # 2400s = 40 min: a heavy chapter (e.g. Ch12's tensor-field files) takes
    # ~20 min to `lake build` even WITH the Mathlib cache primed, so give it
    # generous headroom. NOTE: a build running far past this is almost always
    # compiling Mathlib FROM SCRATCH (the per-worktree cache prime didn't fire) —
    # the real fix for that is the cache prime, not a bigger timeout.
    "--auto-build", "--build-timeout", "2400",
    "--auto-commit", "--auto-push", "--auto-rate",
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
    ``.marathon/review/state.json`` (the per-issue rejection queue)."""
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
    ]
    dispatch_state = load_state(cfg).issues.get(focus_issue)
    if dispatch_state is not None:
        cmd += ["--review-verdict-ts", dispatch_state.verdict_ts]
    # Repo identity is already authoritative configuration. Passing it avoids
    # a GraphQL-backed `gh repo view` inference at the end of an otherwise
    # successful, billed refine session.
    cmd += ["--auto-pr-repo", cfg.github_repo]
    cmd += DEFAULT_REFINE_ARGS
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


#: Embedded marker so a later RECOVERY pass (automerge's requeue-for-rereview)
#: can find and follow up the stall comment instead of leaving it dangling as
#: the issue's newest signal forever (the 2026-07-07 perceived-outage bug).
STALL_COMMENT_MARKER = "<!-- marathon-stall -->"


def _notify_stall(
    cfg: ReviewConfig,
    issue_num: int,
    attempts: int,
    failure_detail: Optional[str] = None,
) -> None:
    """Post a one-time comment on the GitHub issue telling the human the
    rejection stalled and how to re-queue it.

    Posted exactly once per stall: ``record_stall`` drops the entry from
    the dispatch queue, so the daemon never reaches this path again for
    the same verdict. Best-effort by design — a notification failure
    (gh missing, network down, auth expired) is printed and swallowed,
    never allowed to crash the daemon; the "stalled" state in
    state.json is the durable record either way.

    ``failure_detail`` (optional) is the last attempt's log tail so the
    comment is self-diagnosing instead of "check the daemon log"."""
    detail = ""
    if failure_detail and failure_detail.strip():
        detail = (
            "\n\nLast attempt's log tail:\n```\n"
            + failure_detail.strip()[-1500:]
            + "\n```"
        )
    body = (
        f"{STALL_COMMENT_MARKER}\n"
        f"**Auto-refine stalled.** The review daemon failed to dispatch a "
        f"refine iteration for this rejection {attempts} times in a row and "
        f"has stopped retrying it.{detail}\n\n"
        f"To re-queue it: open the deck — a stalled issue surfaces on the "
        f"**⚠ needs attention** card in the review queue with a one-click "
        f"**Re-queue** (also in the Issues tab's Stalled section). CLI "
        f"fallback: re-rejecting (`marathon review reject {issue_num} ...`) "
        f"resets the attempt counter and re-queues it."
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
    failure_detail: Optional[str] = None,
    expected_verdict_ts: Optional[str] = None,
    pending_pr_branch: Optional[str] = None,
    iteration_commit_sha: Optional[str] = None,
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
        record_iteration(
            cfg,
            issue_num,
            expected_verdict_ts=expected_verdict_ts,
            pending_pr_branch=pending_pr_branch,
            iteration_commit_sha=iteration_commit_sha,
        )
        return 0

    # A CONFIGURATION failure (e.g. ARISTOTLE_API_KEY unset) aborted the
    # refine before any Aristotle work. That is an OPERATOR problem, not this
    # rejection's: burn no attempt, post nothing, leave the issue queued —
    # once the environment is fixed it re-dispatches untouched. The caller
    # (conductor) is responsible for not spinning on it (env preflight +
    # long per-issue backoff). This is what turned the 2026-07-07 keyless
    # relaunch into two bogus public 'stalled' verdicts.
    from marathon.skeleton import EXIT_CONFIG_ERROR

    if exit_code == EXIT_CONFIG_ERROR:
        print(
            f"--- iteration for #{issue_num} aborted on a CONFIGURATION "
            f"error (exit {exit_code}) — NOT counted as an attempt. Fix the "
            "environment (e.g. export ARISTOTLE_API_KEY); the issue stays "
            "queued and re-dispatches once it is fixed.",
            flush=True,
        )
        return 0

    from marathon.refine import (
        EXIT_REATTACH_TRANSIENT,
        EXIT_REJECTION_CONTEXT_MISSING,
    )

    if exit_code == EXIT_REJECTION_CONTEXT_MISSING:
        backoff = BACKOFF_BASE_SECONDS
        print(
            f"--- iteration for #{issue_num} had no authoritative rejection "
            f"note (exit {exit_code}) — NOT counted as an attempt; retrying "
            f"after {backoff}s without consuming the verdict.",
            flush=True,
        )
        return backoff

    if exit_code == EXIT_REATTACH_TRANSIENT:
        backoff = BACKOFF_BASE_SECONDS
        print(
            f"--- iteration for #{issue_num} could not reattach to its saved "
            f"Aristotle session (exit {exit_code}) — NOT counted as an "
            f"attempt; preserving IDs/checkpoint and retrying after {backoff}s.",
            flush=True,
        )
        return backoff

    entry = record_failed_attempt(
        cfg, issue_num, expected_verdict_ts=expected_verdict_ts
    )
    if entry is None:
        # The entry vanished mid-iteration (e.g. a concurrent `verify`
        # cleared it). Nothing left to retry against.
        return 0

    if entry.attempts >= max_attempts:
        stalled = record_stall(
            cfg, issue_num, expected_verdict_ts=expected_verdict_ts
        )
        if stalled is None:
            # A fresh verify/re-reject won the race between the failed-attempt
            # increment and stall transition.  The old dispatch owns nothing.
            return 0
        print(
            f"--- iteration for #{issue_num} exited non-zero ({exit_code}); "
            f"attempt {entry.attempts}/{max_attempts} — STALLED. Posting "
            "notification; re-reject the issue to re-queue.",
            flush=True,
        )
        _notify_stall(cfg, issue_num, entry.attempts,
                      failure_detail=failure_detail)
        return 0

    backoff = _backoff_seconds(entry.attempts)
    print(
        f"--- iteration for #{issue_num} exited non-zero ({exit_code}); "
        f"attempt {entry.attempts}/{max_attempts} — will retry after "
        f"{backoff}s backoff.",
        flush=True,
    )
    return backoff


def _maybe_automerge(
    cfg,
    enabled: bool,
    *,
    exclude_heads: "frozenset[str]" = frozenset(),
    scope: str = "any",
    base: "str | None" = None,
) -> None:
    """Best-effort: land every eligible open PR on the base branch (the merge
    orchestrator). ``base`` overrides the merge target (default: the config's
    auto-pr base, else ``main``) — the conductor runs a second cycle with the
    prover branch as base to land proof PRs there. ``exclude_heads`` protects
    the head branches of in-flight jobs (merging+deleting one would break a
    running push). Wrapped so a ``gh`` hiccup NEVER crashes the loop — a failed
    cycle just retries next tick. No-op when disabled."""
    if not enabled:
        return
    try:
        from marathon.review.automerge import automerge

        automerge(cfg, exclude_heads=exclude_heads, scope=scope, base=base)
    except Exception as exc:  # noqa: BLE001 — never let merge housekeeping kill the daemon
        print(f"  automerge: WARN — cycle skipped ({exc})", flush=True)


def run_daemon(
    chapter: int,
    once: bool = False,
    max_attempts: int = DEFAULT_MAX_ATTEMPTS,
    automerge: bool = True,
    merge_scope: str = "any",
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

    When ``automerge`` is on (the default), each poll first lands every
    eligible open PR on the base branch (:mod:`marathon.review.automerge`),
    so mechanical infra PRs (chapter registration) and green refine PRs reach
    ``main`` without the human babysitting a merge.
    """
    cfg = load_config()

    # Environment preflight: without the Aristotle key every dispatched refine
    # exits EXIT_CONFIG_ERROR, which the exit ladder (correctly) treats as
    # attempt-free — but that means this loop would HOT-SPIN forever, dispatch
    # → instant fail → re-pick, with no backoff or stall. Refuse to start
    # instead (mirrors the conductor's dispatch gate). ``os`` is imported at
    # module scope.
    if not os.environ.get("ARISTOTLE_API_KEY"):
        print(
            "ARISTOTLE_API_KEY is not set — refusing to start the refine "
            "daemon (every dispatch would fast-fail in a hot loop). Export "
            "the key and re-run."
        )
        return 1

    if not acquire_lock(cfg, chapter):
        print(f"another refine daemon is already active for chapter {chapter}; exiting")
        return 0

    signal.signal(signal.SIGTERM, _handle_stop_signal)
    signal.signal(signal.SIGINT, _handle_stop_signal)

    mode = "one-shot" if once else "daemon"
    print(
        f"=== review refine daemon chapter={chapter} pid={os.getpid()} "
        f"mode={mode} automerge={'on' if automerge else 'off'} "
        f"starting {datetime.now().isoformat()} ===",
        flush=True,
    )

    iteration_count = 0

    try:
        while not _STOP_REQUESTED:
            queue = pending_rejections_needing_iteration(cfg, chapter)
            # Land eligible PRs first so infra/registration stays on main and the
            # refine that follows builds against an up-to-date base (best-effort).
            # Exclude the branches of issues queued for iteration THIS loop —
            # merging+deleting a branch we are about to refine would break its
            # push.
            _maybe_automerge(
                cfg, automerge, scope=merge_scope,
                exclude_heads=frozenset(
                    f"marathon/refine-c{chapter}-i{issue}" for issue, _ in queue
                ),
            )

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
                    expected_verdict_ts=target_state.verdict_ts,
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
