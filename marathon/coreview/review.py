"""Coreview review-loop command handlers.

These are the per-sub-issue review commands: ``list`` / ``next`` /
``show`` / ``verify`` / ``reject`` / ``refine-status`` / ``refine-start``
/ ``refine-stop``. Each takes a parsed ``argparse`` namespace and
operates against a ``CoreviewConfig`` loaded from the current repo.

The command wiring lives in ``marathon.coreview.cli`` (which is
invoked by ``marathon coreview ...``); this module holds the logic so
it can also be tested / scripted directly.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from datetime import datetime
from pathlib import Path
from typing import Optional

from marathon.coreview.config import CoreviewConfig, load_config
from marathon.coreview.github import gh, issue_labels, issue_title
from marathon.coreview.referee_queue import append_rejection_bullet
from marathon.coreview.tracker import update_tracker_emoji


# --- Status reporting --------------------------------------------------------


def get_issue_status(cfg: CoreviewConfig, num: int) -> Optional[str]:
    """Returns 'verified', 'rejected', 'inflight', or None (unreviewed)."""
    labels = issue_labels(num, cfg.github_repo)
    if labels is None:
        return None
    if cfg.labels.verified in labels:
        return "verified"
    if cfg.labels.inflight in labels:
        return "inflight"
    if cfg.labels.rejected in labels:
        return "rejected"
    return None


# --- list / next / show ------------------------------------------------------


def cmd_list(args) -> None:
    cfg = load_config()
    registry = cfg.chapter_registry(args.chapter)
    print(
        f"Sub-issues of #{cfg.parent_issue} in coreview order "
        f"(chapter {args.chapter}):\n"
    )
    print(f"  {'idx':>3}  {'issue':>6}  {'status':<10}  title")
    print(f"  {'---':>3}  {'-----':>6}  {'------':<10}  -----")
    for idx, (num, _) in enumerate(registry.entries, 1):
        status = get_issue_status(cfg, num) or "unreviewed"
        title = issue_title(num, cfg.github_repo)
        marker = "→ " if status == "unreviewed" else "  "
        print(f"  {marker}{idx:>2}  #{num:>4}  {status:<10}  {title}")
    print()


def cmd_next(args) -> None:
    cfg = load_config()
    registry = cfg.chapter_registry(args.chapter)
    for num, _ in registry.entries:
        if get_issue_status(cfg, num) is None:
            print(f"Next unreviewed sub-issue: #{num}\n")
            _show_issue(cfg, num)
            return
    print(f"All chapter {args.chapter} sub-issues are reviewed. 🎉")


def _show_issue(cfg: CoreviewConfig, num: int) -> None:
    cp = gh("issue", "view", str(num), "--repo", cfg.github_repo)
    print(cp.stdout)


def cmd_show(args) -> None:
    cfg = load_config()
    _show_issue(cfg, args.issue_num)


# --- verify ------------------------------------------------------------------


def cmd_verify(args) -> None:
    cfg = load_config()
    num = args.issue_num
    if args.close:
        default_comment = (
            "✅ VERIFIED — declaration matches the textbook reference and is "
            "fully implemented. See body for findings."
        )
    else:
        default_comment = (
            "✅ VERIFIED — declaration matches the textbook reference. "
            "See body for findings. Sub-issue stays open to track remaining "
            "`sorry` bodies; pass `--close` to verify if the declaration is "
            "fully implemented."
        )
    comment = args.comment or default_comment

    print(f"Marking #{num} as VERIFIED...")
    gh("issue", "comment", str(num), "--repo", cfg.github_repo, "--body", comment)
    # If previously rejected, drop that label.
    gh(
        "issue", "edit", str(num),
        "--repo", cfg.github_repo,
        "--add-label", cfg.labels.verified,
        "--remove-label", cfg.labels.rejected,
        check=False,
    )
    if args.close:
        gh("issue", "close", str(num), "--repo", cfg.github_repo)
        print(f"✅ #{num} verified + closed (fully implemented), tracker → 🟡.")
    else:
        gh("issue", "reopen", str(num), "--repo", cfg.github_repo, check=False)
        print(
            f"✅ #{num} verified (statements accepted; sorrys remain — "
            "issue kept OPEN), tracker → 🟡."
        )
    ok, msg = update_tracker_emoji(cfg, num, "🟡")
    if ok:
        print(f"  tracker: {msg}")
    else:
        print(f"  tracker: WARN — {msg}")


# --- reject ------------------------------------------------------------------


def cmd_reject(args) -> None:
    cfg = load_config()
    num = args.issue_num
    notes_path = Path(args.notes)
    if notes_path.is_file():
        notes_text = notes_path.read_text()
    else:
        notes_text = args.notes
    notes_text = notes_text.strip()
    if not notes_text:
        sys.exit("rejection notes are empty; supply --notes <file-or-text>")

    print(f"Marking #{num} as REJECTED...")
    comment = args.comment or (
        "❌ REJECTED — see body for findings; fix bullet queued in "
        "`.marathon/referee.md` user-managed header."
    )
    gh("issue", "comment", str(num), "--repo", cfg.github_repo, "--body", comment)
    gh(
        "issue", "edit", str(num),
        "--repo", cfg.github_repo,
        "--add-label", cfg.labels.rejected,
    )
    if append_rejection_bullet(cfg.referee_path, num, notes_text):
        print(f"  appended rejection bullet to {cfg.referee_path}")
    else:
        print(
            f"  warning: {cfg.referee_path} not found; skipping referee.md append"
        )
    print(f"❌ #{num} rejected and queued for refinement.")

    if not args.no_refine:
        chapter = cfg.chapter_of_issue(num)
        if chapter is None:
            print(f"  cannot auto-launch refine: #{num} not in any registered chapter")
        else:
            _launch_or_queue_refine(cfg, chapter)


# --- refine daemon control ---------------------------------------------------


def _process_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
        return True
    except (OSError, ProcessLookupError):
        return False


def _runner_lock_path(cfg: CoreviewConfig, chapter: int) -> Path:
    return cfg.runner_lock_dir / f"refine-c{chapter}.lock"


def _launch_or_queue_refine(cfg: CoreviewConfig, chapter: int) -> None:
    """Single-flight launch of the auto-refine daemon per chapter.

    If a daemon is already alive for this chapter, just print a note
    that the rejection is queued (the daemon picks it up via referee.md
    on its next loop iteration). Otherwise spawn ``python -m
    marathon.coreview.daemon --chapter N`` in a detached subprocess.
    """
    lock = _runner_lock_path(cfg, chapter)
    if lock.is_file():
        try:
            pid = int(lock.read_text().strip())
            if _process_alive(pid):
                print(
                    f"  refine daemon already active for c{chapter} "
                    f"(pid {pid}); this rejection will be picked up on the "
                    "daemon's next loop iteration (referee.md is re-hashed "
                    "before each marathon refine call)"
                )
                return
        except (ValueError, OSError):
            pass
        lock.unlink(missing_ok=True)

    cfg.runner_log_dir.mkdir(parents=True, exist_ok=True)
    log_path = (
        cfg.runner_log_dir
        / f"refine-c{chapter}-{datetime.now().strftime('%Y%m%d-%H%M%S')}.log"
    )
    log_fh = log_path.open("w")
    proc = subprocess.Popen(
        [sys.executable, "-m", "marathon.coreview.daemon", "--chapter", str(chapter)],
        stdout=log_fh,
        stderr=subprocess.STDOUT,
        start_new_session=True,
    )
    print(f"  refine daemon launched (pid {proc.pid}); log: {log_path}")


def cmd_refine_status(args) -> None:
    cfg = load_config()
    chapter = args.chapter
    lock = _runner_lock_path(cfg, chapter)
    if not lock.is_file():
        print(f"chapter {chapter}: no refine daemon active")
        return
    try:
        pid = int(lock.read_text().strip())
    except (ValueError, OSError):
        print(f"chapter {chapter}: lock file unreadable; removing")
        lock.unlink(missing_ok=True)
        return
    if not _process_alive(pid):
        print(f"chapter {chapter}: stale lock (pid {pid} not alive); removing")
        lock.unlink(missing_ok=True)
        return
    print(f"chapter {chapter}: refine daemon active (pid {pid})")
    logs = sorted(cfg.runner_log_dir.glob(f"refine-c{chapter}-*.log"))
    if logs:
        latest = logs[-1]
        print(f"  log: {latest}")
        print(f"  --- tail -20 {latest.name} ---")
        tail = subprocess.run(["tail", "-20", str(latest)], capture_output=True, text=True)
        print(tail.stdout)


def cmd_refine_stop(args) -> None:
    import signal as _sig
    cfg = load_config()
    chapter = args.chapter
    lock = _runner_lock_path(cfg, chapter)
    if not lock.is_file():
        print(f"chapter {chapter}: no refine daemon active")
        return
    try:
        pid = int(lock.read_text().strip())
    except (ValueError, OSError):
        print(f"chapter {chapter}: lock unreadable; removing")
        lock.unlink(missing_ok=True)
        return
    if not _process_alive(pid):
        print(f"chapter {chapter}: stale lock (pid {pid}); removing")
        lock.unlink(missing_ok=True)
        return
    try:
        os.kill(pid, _sig.SIGTERM)
        print(f"chapter {chapter}: sent SIGTERM to refine daemon pid {pid}")
        print(
            f"  (the daemon will finish its current marathon iteration "
            "before exiting; may take several minutes)"
        )
    except (OSError, ProcessLookupError) as e:
        print(f"chapter {chapter}: could not signal pid {pid}: {e}")


def cmd_refine_start(args) -> None:
    cfg = load_config()
    _launch_or_queue_refine(cfg, args.chapter)


# --- subissues bulk commands -------------------------------------------------


def cmd_subissues_create(args) -> None:
    """``marathon coreview subissues create <drafts.md> [--skip N,M]``."""
    from marathon.coreview.subissues import create_subissues_from_drafts
    cfg = load_config()
    drafts_path = Path(args.drafts_file)
    if not drafts_path.is_file():
        sys.exit(f"drafts file not found: {drafts_path}")
    skip = {int(x) for x in (args.skip or "").split(",") if x.strip()}
    create_subissues_from_drafts(cfg, drafts_path, skip=skip)


def cmd_subissues_refresh(args) -> None:
    """``marathon coreview subissues refresh <drafts.md> [--only N,M]``."""
    from marathon.coreview.subissues import refresh_subissue_bodies
    cfg = load_config()
    drafts_path = Path(args.drafts_file)
    if not drafts_path.is_file():
        sys.exit(f"drafts file not found: {drafts_path}")
    only = {int(x) for x in (args.only or "").split(",") if x.strip()}
    refresh_subissue_bodies(cfg, drafts_path, only=only or None)
