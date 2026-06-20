"""Keep a HOSTED deck's checkout current — the thin-deck sync loop.

When the deck is hosted off the author's laptop (a small always-on box that
serves the review UI + writes verdicts to GitHub under each user's token, while
the heavy refine compute runs on each collaborator's OWN ``marathon conductor
run --queue-from-github``), the hosting box still needs the repo's WORKING TREE
current so the cards show the latest merged Lean source. This module is the loop
that does that: a fast-forward ``git pull`` every N seconds, fail-soft.

What it does NOT sync: the audit snapshot (``.marathon/audit/latest.json``) is
gitignored by design — a local compute artifact (Lean elaboration) the thin deck
cannot regenerate. The deck degrades gracefully without a fresh one (empty trust
tiers). The loop reports the audit's age so staleness is visible; shipping a
fresh audit to a hosted box is a separate, optional compute-side step.
"""

from __future__ import annotations

import subprocess
import threading
import time
from pathlib import Path
from typing import Callable, Optional


def _git(repo_dir: Path, *args: str, timeout: int = 90) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git", "-C", str(repo_dir), *args],
        capture_output=True, text=True, timeout=timeout,
    )


def _head(repo_dir: Path) -> str:
    try:
        cp = _git(repo_dir, "rev-parse", "HEAD")
        return cp.stdout.strip() if cp.returncode == 0 else ""
    except Exception:  # noqa: BLE001
        return ""


def pull_once(repo_dir: Path) -> str:
    """Fast-forward the deck's checkout to its upstream; return a human status.

    Uses ``git pull --ff-only`` so it NEVER clobbers local work — a hosted deck
    never commits to the tree (merges happen on GitHub via gh, local review state
    is untracked), so it always fast-forwards cleanly. A non-ff state (someone
    left local commits) is reported, not forced. Fail-soft: never raises."""
    try:
        before = _head(repo_dir)
        cp = _git(repo_dir, "pull", "--ff-only", "--quiet")
        if cp.returncode != 0:
            msg = (cp.stderr or cp.stdout or "").strip().splitlines()
            tail = msg[-1] if msg else "unknown error"
            return f"pull failed (ff-only): {tail[:120]}"
        after = _head(repo_dir)
        if not after:
            return "pull ok (HEAD unreadable)"
        if before == after:
            return "up to date"
        return f"updated {before[:7]} → {after[:7]}"
    except subprocess.TimeoutExpired:
        return "pull timed out"
    except Exception as exc:  # noqa: BLE001 — a sync hiccup must never crash serving
        return f"sync error: {exc}"


def audit_age_note(repo_dir: Path) -> str:
    """A short note on the audit snapshot's freshness (it is gitignored, so the
    pull never refreshes it — this just surfaces staleness)."""
    p = Path(repo_dir) / ".marathon" / "audit" / "latest.json"
    try:
        if not p.is_file():
            return " · audit: none (trust tiers empty)"
        age_min = (time.time() - p.stat().st_mtime) / 60.0
        return f" · audit: {age_min:.0f}m old"
    except Exception:  # noqa: BLE001
        return ""


def start_repo_sync(
    repo_dir: Path,
    interval: int,
    *,
    log: Callable[[str], None] = print,
) -> Optional[threading.Thread]:
    """Start a daemon thread that fast-forwards ``repo_dir`` every ``interval``
    seconds (pulling once immediately), keeping a hosted deck's working tree
    current. A no-op returning ``None`` when ``interval <= 0`` (the default — a
    local single-user deck manages its own checkout)."""
    if interval is None or interval <= 0:
        return None

    def _loop() -> None:
        while True:
            try:
                log(f"  [sync] {pull_once(repo_dir)}{audit_age_note(repo_dir)}")
            except Exception:  # noqa: BLE001 — the sync thread must never die
                pass
            time.sleep(interval)

    t = threading.Thread(target=_loop, name="marathon-deck-sync", daemon=True)
    t.start()
    return t
