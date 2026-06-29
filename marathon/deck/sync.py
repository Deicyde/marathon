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
    ``marathon conductor`` writes review state and commits a ``formalization.yaml``
    regen onto it. Two failure shapes wedge the ff-only pull FOREVER — the silent
    staleness that made a reviewer re-reject an already-fixed decl (its card never
    dropped because the stale tree still ``in_code``-matched it):

      * DIVERGED — a local metadata commit didn't push (origin advanced via
        automerge first), so HEAD is ahead of upstream; reset --hard to upstream,
        but ONLY when every local-ahead commit is a marathon metadata commit
        (operator commits are never clobbered).
      * BEHIND-BLOCKED — HEAD is merely behind, but dirty/untracked review-state
        churn under ``.marathon`` blocks the fast-forward; stash it and ff-only.

    Both end with a CURRENT working tree. The dirty TRACKED review-state churn is
    stashed across the advance and restored after; untracked deck-local state
    (in-progress per-decl verdicts) is left in place — never upstream-tracked, it
    neither blocks the ff nor is lost. The conductor re-derives the tracked review
    state from upstream, so an unresolvable restore conflict keeps the advanced
    (upstream) tree rather than re-wedging."""
    _git(repo_dir, "fetch", "origin", "--quiet")
    up = _git(repo_dir, "rev-parse", "--abbrev-ref", "@{u}")
    if up.returncode != 0:
        return "pull failed (ff-only); no upstream to recover to"
    upstream = up.stdout.strip()
    log = _git(repo_dir, "log", "--format=%s", f"{upstream}..HEAD")
    subjects = [s for s in (log.stdout or "").splitlines() if s.strip()]
    # Never clobber operator commits: if AHEAD by any non-marathon commit, bail.
    if subjects and not all(_MARATHON_METADATA_RE.search(s) for s in subjects):
        return (f"diverged from {upstream} by {len(subjects)} non-marathon "
                "commit(s); NOT resetting — resolve manually")
    bc = _git(repo_dir, "rev-list", "--count", f"HEAD..{upstream}")
    n_behind = int(bc.stdout.strip()) if bc.returncode == 0 and bc.stdout.strip().isdigit() else 0
    if not subjects and n_behind == 0:
        # ff-only failed yet we are neither ahead nor behind — a genuine oddity
        # (detached HEAD, ref race). Leave it for a human rather than guess.
        return f"pull failed (ff-only) but in sync with {upstream}; manual check"

    # Two recoverable shapes, both ending with a CURRENT working tree (which is all
    # the deck's in_code/reconcile needs so stale cards drop themselves):
    #   AHEAD only by marathon-metadata commits  -> reset --hard to upstream
    #   purely BEHIND, ff blocked by dirty/untracked review-state churn -> ff-only
    # Stash the dirty TRACKED churn (review-state files the conductor co-writes) so
    # the advance is clean; untracked deck-local state (in-progress per-decl
    # verdicts) is left in place — it is never upstream-tracked, so it neither
    # blocks the ff nor risks being lost in the stash. The conductor authoritatively
    # re-derives the tracked review state from upstream, so on an unresolvable
    # restore conflict we keep the advanced (upstream) tree rather than re-wedge.
    stash = _git(repo_dir, "stash", "push", "-m", "deck-sync-autostash")
    stashed = stash.returncode == 0 and "No local changes" not in (stash.stdout or "")
    if subjects:
        adv = _git(repo_dir, "reset", "--hard", upstream)
        kind = f"dropped {len(subjects)} marathon metadata commit(s)"
    else:
        adv = _git(repo_dir, "merge", "--ff-only", upstream, "--quiet")
        kind = f"fast-forwarded {n_behind} commit(s)"
    if adv.returncode != 0:
        if stashed:
            _git(repo_dir, "stash", "pop")
        return f"recover to {upstream} failed: {(adv.stderr or '')[:80]}"
    after = _head(repo_dir)  # tree is now current — in_code is accurate from here
    if stashed:
        pop = _git(repo_dir, "stash", "pop")
        if pop.returncode != 0:
            # The advance already won (current code = the goal). Keep what applied
            # cleanly; resolve ONLY the genuinely-conflicting review-state files
            # toward upstream (the conductor re-derives those), so the deck
            # self-heals without re-wedging or discarding clean local edits.
            unmerged = [p for p in (_git(
                repo_dir, "diff", "--name-only", "--diff-filter=U").stdout or ""
            ).splitlines() if p.strip()]
            if unmerged:
                _git(repo_dir, "checkout", "HEAD", "--", *unmerged)
            else:
                _git(repo_dir, "reset", "--hard", "HEAD")
            _git(repo_dir, "stash", "drop")
            return (f"recovered wedged checkout {before[:7]} → {after[:7]} ({kind}); "
                    f"{len(unmerged)} review-state file(s) conflicted — kept upstream")
    return f"recovered wedged checkout {before[:7]} → {after[:7]} ({kind})"


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
