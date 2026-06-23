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

import json
import re
import subprocess
import threading
import time
from pathlib import Path
from typing import Callable, Optional

# Subjects of commits the co-located conductor makes onto the deck's checkout
# (it regenerates formalization.yaml centrally after each job). These are the
# ONLY local-ahead commits the deck-sync is allowed to discard when recovering a
# diverged checkout — operator commits are never touched.
_MARATHON_METADATA_RE = re.compile(
    r"chore\(marathon\)|regenerate formalization\.yaml", re.IGNORECASE
)


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


def _recover_from_marathon_divergence(repo_dir: Path, before: str) -> str:
    """Recover a deck checkout that ``git pull --ff-only`` can no longer advance.

    The deck's tree is a read-only MIRROR of its upstream, but the co-located
    ``marathon conductor`` commits a ``formalization.yaml`` regen onto it; if that
    commit doesn't push (e.g. origin advanced via automerge first) the checkout
    diverges and the ff-only pull wedges FOREVER — the silent staleness that made
    a reviewer re-reject an already-fixed decl. Reset to the upstream, but ONLY
    when every local-ahead commit is a marathon metadata commit (never clobber
    operator commits), preserving tracked review-state edits across the reset
    (untracked files survive ``reset --hard``)."""
    _git(repo_dir, "fetch", "origin", "--quiet")
    up = _git(repo_dir, "rev-parse", "--abbrev-ref", "@{u}")
    if up.returncode != 0:
        return "pull failed (ff-only); no upstream to recover to"
    upstream = up.stdout.strip()
    log = _git(repo_dir, "log", "--format=%s", f"{upstream}..HEAD")
    subjects = [s for s in (log.stdout or "").splitlines() if s.strip()]
    if not subjects:
        return f"pull failed (ff-only) but not ahead of {upstream}; manual check"
    if not all(_MARATHON_METADATA_RE.search(s) for s in subjects):
        return (f"diverged from {upstream} by {len(subjects)} non-marathon "
                "commit(s); NOT resetting — resolve manually")
    stash = _git(repo_dir, "stash", "push", "-m", "deck-sync-autostash")
    stashed = stash.returncode == 0 and "No local changes" not in (stash.stdout or "")
    reset = _git(repo_dir, "reset", "--hard", upstream)
    if reset.returncode != 0:
        if stashed:
            _git(repo_dir, "stash", "pop")
        return f"diverged; reset to {upstream} failed: {(reset.stderr or '')[:80]}"
    if stashed:
        pop = _git(repo_dir, "stash", "pop")
        if pop.returncode != 0:
            return (f"recovered to {upstream} but review-state stash pop "
                    "conflicted — restore manually (`git stash list`)")
    after = _head(repo_dir)
    return (f"recovered diverged checkout {before[:7]} → {after[:7]} "
            f"(dropped {len(subjects)} marathon metadata commit(s))")


def pull_once(repo_dir: Path) -> str:
    """Fast-forward the deck's checkout to its upstream; return a human status.

    Uses ``git pull --ff-only`` so it NEVER clobbers operator work. The deck's
    tree is a read-only mirror, but the co-located conductor commits a
    ``formalization.yaml`` regen onto it that can diverge the checkout; on an
    ff-failure we hand off to :func:`_recover_from_marathon_divergence`, which
    resets to upstream ONLY when the divergence is marathon metadata commits (and
    preserves tracked review state). Fail-soft: never raises."""
    try:
        before = _head(repo_dir)
        cp = _git(repo_dir, "pull", "--ff-only", "--quiet")
        if cp.returncode != 0:
            return _recover_from_marathon_divergence(repo_dir, before)
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


def refresh_audit(repo_dir: Path, source: str) -> str:
    """Fetch the audit snapshot from ``source`` — a local path or an http(s) URL
    — to ``<repo_dir>/.marathon/audit/latest.json``, atomically.

    For a HOSTED deck whose compute machine PUBLISHES its audit (rsync to a path,
    or an http endpoint), so the deck shows fresh trust tiers without running Lean
    itself. Validates the payload parses as JSON before swapping the live file in,
    and swaps via an atomic rename so the deck never reads a partial snapshot.
    Fail-soft: any error keeps the existing snapshot."""
    dest = Path(repo_dir) / ".marathon" / "audit" / "latest.json"
    try:
        if source.startswith(("http://", "https://")):
            import urllib.request

            with urllib.request.urlopen(source, timeout=30) as r:  # noqa: S310
                data = r.read()
        else:
            data = Path(source).read_bytes()
        json.loads(data)  # reject garbage before touching the live file
        dest.parent.mkdir(parents=True, exist_ok=True)
        tmp = dest.with_name(dest.name + ".tmp")
        tmp.write_bytes(data)
        tmp.replace(dest)  # atomic swap
        return "audit refreshed"
    except Exception as exc:  # noqa: BLE001
        return f"audit refresh failed ({type(exc).__name__}); kept existing"


def start_repo_sync(
    repo_dir: Path,
    interval: int,
    *,
    audit_from: Optional[str] = None,
    log: Callable[[str], None] = print,
) -> Optional[threading.Thread]:
    """Start a daemon thread that fast-forwards ``repo_dir`` every ``interval``
    seconds (pulling once immediately), keeping a hosted deck's working tree
    current. When ``audit_from`` is set, ALSO refresh the (gitignored) audit
    snapshot from that path/URL each cycle. A no-op returning ``None`` when
    ``interval <= 0`` (the default — a local single-user deck manages its own
    checkout)."""
    if interval is None or interval <= 0:
        return None

    def _loop() -> None:
        while True:
            try:
                msg = pull_once(repo_dir)
                if audit_from:
                    msg += " · " + refresh_audit(repo_dir, audit_from)
                else:
                    msg += audit_age_note(repo_dir)
                log(f"  [sync] {msg}")
            except Exception:  # noqa: BLE001 — the sync thread must never die
                pass
            time.sleep(interval)

    t = threading.Thread(target=_loop, name="marathon-deck-sync", daemon=True)
    t.start()
    return t
