"""Shared ``claude`` CLI subprocess helper + cross-process concurrency limiter.

WHY this module exists: five call sites (the refine reviewer in
``claude_review``, the auto-rater in ``post_pipeline``, the Hermes steerer
in ``hermes_watcher``, the advisory jury in ``jury``, and the referee in
``referee``) each grew their own copy of the same subprocess conventions —
prompt via stdin, ``ANTHROPIC_API_KEY`` scrubbed, ``--tools ""``,
``--output-format text``. Phase 3's Conductor fans N refine jobs out in
parallel, and every one of them drafts/steers/rates/juries through this
single Claude Max session — N copies of "just call ``subprocess.run``"
would stampede it. Centralizing here gives one place for the conventions
AND one cross-process throttle (plan §2 ruling 2: "add a Claude-call
semaphore — N jobs × whole-repo prompts × steering all bill one Max
session").

The conventions, and why each exists:

- **Prompt via stdin, never argv.** The review/rater prompts bundle whole
  repos and routinely exceed the OS argv limit (the E2BIG lesson).
  ``claude -p`` with no inline query reads the prompt from stdin.
- **``ANTHROPIC_API_KEY`` scrubbed from the child env.** With the key
  present, the CLI routes through pay-per-token API billing; scrubbed, it
  falls back to the keychain-stored Max OAuth login (flat-rate).
- **Model resolution: ``model`` kwarg > ``MARATHON_CLAUDE_MODEL`` env >
  ``claude-opus-4-7``.** Computed per call (the jury convention, not
  ``claude_review``'s historical import-time read) so long-lived processes
  and tests see env changes. Call sites that must stay pinned (the rater,
  Hermes, the referee) pass their model explicitly.

The slot limiter: K lock files under ``~/.marathon/claude-slots/``
(K = ``MARATHON_CLAUDE_MAX_CONCURRENT``, default 2). A caller acquires any
slot with ``fcntl.flock(LOCK_EX | LOCK_NB)``, retrying across slots with a
short sleep until a generous overall timeout (default 600 s — Claude calls
legitimately run minutes). On timeout we print a warning and RUN ANYWAY:
the limiter is a courtesy throttle protecting the Max session from a
stampede, never a deadlock source — a wedged lock file must not be able to
halt the pipeline.

WHY ``flock`` beats PID files here: the kernel releases a flock
automatically when the holding process dies — including SIGKILL and crash
— so there is no stale-lock cleanup, no liveness probing, and no PID-reuse
race (a PID file scheme must guess whether the recorded PID still belongs
to the original holder; flock simply *is* held or *isn't*). Slot files and
their directory are created lazily on first use; the files themselves stay
empty and are never deleted (an unlocked slot file is free capacity, and
deleting one under a concurrent opener would split the lock identity).
"""

from __future__ import annotations

import fcntl
import os
import shutil
import subprocess
import time
from pathlib import Path
from typing import Optional, Sequence

# Default model for every Claude call site. Env-overridable so large repos
# can route prompts through a 1M-context model, e.g.
# MARATHON_CLAUDE_MODEL="claude-opus-4-8[1m]".
DEFAULT_MODEL = "claude-opus-4-7"

# Cross-process slot count when MARATHON_CLAUDE_MAX_CONCURRENT is unset.
# 2 matches the plan's "soak-test 2 before sizing" posture for shared-
# session concurrency.
DEFAULT_MAX_CONCURRENT = 2

# Overall slot-acquire budget. Generous on purpose: a slot is held for an
# entire Claude call (minutes for whole-repo review prompts), so a queued
# caller legitimately waits a while. Module-level so tests can shrink it.
SLOT_ACQUIRE_TIMEOUT_SECONDS = 600.0

# Sleep between acquisition passes over the slot files. Module-level so
# tests can shrink it.
SLOT_POLL_INTERVAL_SECONDS = 0.25


def resolve_model(model: Optional[str] = None) -> str:
    """Per-call model resolution: explicit ``model`` arg >
    ``MARATHON_CLAUDE_MODEL`` env var > :data:`DEFAULT_MODEL`."""
    if model:
        return model
    return os.environ.get("MARATHON_CLAUDE_MODEL") or DEFAULT_MODEL


def _slot_dir() -> Path:
    """Slot-file directory: ``MARATHON_CLAUDE_SLOT_DIR`` env override
    (tests point this at a tmp dir) or ``~/.marathon/claude-slots``."""
    override = os.environ.get("MARATHON_CLAUDE_SLOT_DIR")
    if override:
        return Path(override)
    return Path.home() / ".marathon" / "claude-slots"


def _max_concurrent() -> int:
    """K from ``MARATHON_CLAUDE_MAX_CONCURRENT``, clamped to ≥ 1.
    Garbage values fall back to the default rather than erroring — a typo
    in an env var must not break every Claude call."""
    raw = os.environ.get("MARATHON_CLAUDE_MAX_CONCURRENT", "")
    try:
        k = int(raw)
    except ValueError:
        return DEFAULT_MAX_CONCURRENT
    return max(1, k)


def _acquire_slot(
    timeout_seconds: float, poll_seconds: float
) -> Optional[int]:
    """Try to take one of the K slots; return the open fd holding the
    exclusive flock, or ``None`` on timeout (or unusable slot dir).

    The caller MUST ``os.close`` the returned fd when the Claude call
    finishes — closing the fd releases the flock. If the caller dies
    first, the kernel releases it anyway (the whole point of flock).
    """
    slot_dir = _slot_dir()
    try:
        slot_dir.mkdir(parents=True, exist_ok=True)
    except OSError:
        # Courtesy throttle: an uncreatable slot dir (permissions, RO fs)
        # degrades to "no limiter", never to "no Claude".
        return None
    k = _max_concurrent()
    deadline = time.monotonic() + timeout_seconds
    while True:
        for i in range(k):
            path = slot_dir / f"slot-{i}.lock"
            try:
                fd = os.open(str(path), os.O_RDWR | os.O_CREAT, 0o644)
            except OSError:
                continue
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except OSError:
                os.close(fd)  # slot busy in another process; try the next
                continue
            return fd
        if time.monotonic() >= deadline:
            return None
        time.sleep(poll_seconds)


def run_claude(
    prompt: str,
    *,
    model: Optional[str] = None,
    timeout: Optional[float] = None,
    extra_args: Sequence[str] = (),
) -> subprocess.CompletedProcess:
    """Run one ``claude -p`` completion under the cross-process limiter.

    Returns the raw :class:`subprocess.CompletedProcess` (``check=False``)
    so each call site keeps its own error/empty-output policy — the
    reviewer ``sys.exit``s, the jury returns ``None``, the rater records a
    ``parse_error``, Hermes raises. ``OSError`` from the exec and
    ``subprocess.TimeoutExpired`` propagate for the same reason (the slot
    is still released).

    ``timeout`` is forwarded to ``subprocess.run``; ``extra_args`` are
    appended after the shared flag set.
    """
    claude_path = shutil.which("claude")
    if not claude_path:
        # Call sites pre-check with their own user-facing messaging; this
        # is the backstop for direct callers.
        raise FileNotFoundError("claude (Claude Code CLI) not found on PATH")

    cmd = [
        claude_path,
        "-p",
        "--model", resolve_model(model),
        "--tools", "",
        "--output-format", "text",
        *extra_args,
    ]

    env = os.environ.copy()
    env.pop("ANTHROPIC_API_KEY", None)

    slot_fd = _acquire_slot(
        SLOT_ACQUIRE_TIMEOUT_SECONDS, SLOT_POLL_INTERVAL_SECONDS
    )
    if slot_fd is None:
        print(
            f"  claude: WARNING — no concurrency slot free after "
            f"{SLOT_ACQUIRE_TIMEOUT_SECONDS:.0f}s "
            f"(K={_max_concurrent()}); proceeding anyway — the limiter is "
            "a courtesy throttle, never a deadlock source"
        )
    try:
        return subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            check=False,
            env=env,
            input=prompt,
            timeout=timeout,
        )
    finally:
        if slot_fd is not None:
            os.close(slot_fd)  # closing the fd releases the flock
