"""Find + flip tracker emojis on the parent (LeeSM Tracker) issue body.

The parent issue (e.g. #1 in pitmonticone/GeometricAnalysis) has one
line per declaration with a status emoji at the start. When a sub-issue
transitions from unreviewed (🟠) to verified (🟡) or rejected (🟠 still,
because rejection is queued and not yet applied), this module patches
the corresponding line in the parent issue body.

The mapping between a sub-issue and its line is via the substring
recorded in :class:`marathon.review.config.ChapterRegistry`.

Two writers share one line-matcher (:func:`_apply_emoji_flip`):

* :func:`update_tracker_emoji` — the single-issue flip used by
  ``marathon review verify`` (one ``gh issue edit`` per call); and
* :func:`update_tracker_emojis` — the BATCHED multi-flip used by the
  Phase-5b invalidation engine, which may need to flip N downstream
  issues 🟡→🟠 at once. The write-storm ruling
  (docs/v2-analysis/crit-feas-verification-surface-first.md §5)
  forbids one tracker-body rewrite per affected decl: this helper applies
  every substring→emoji edit to one fresh snapshot in memory and performs one
  write in the uncontended case (with pre-write conflict detection and
  post-write verification) — substring surgery happens as a single batch per
  audit run regardless of how many issues flip.
"""

from __future__ import annotations

import subprocess
import tempfile
import threading
from contextlib import contextmanager
from pathlib import Path
from typing import Optional

try:  # POSIX advisory locks (Linux/macOS).
    import fcntl as _fcntl
except ImportError:  # pragma: no cover - exercised on non-POSIX hosts
    _fcntl = None

try:  # Windows advisory byte-range locks.
    import msvcrt as _msvcrt
except ImportError:  # pragma: no cover - exercised on non-Windows hosts
    _msvcrt = None

from marathon.review.config import ReviewConfig
from marathon.review.github import gh, issue_body


# GitHub documents conditional requests for GET, but the issue-update endpoint
# does not opt its unsafe PATCH into conditional requests.  There is therefore
# no remote compare-and-swap primitive for an issue body.  We serialize every
# Marathon writer that shares this checkout, check immediately before writing,
# then verify immediately afterward and rebase/retry any conflict we can
# observe.  The tiny final check→PATCH window remains irreducible for a human or
# writer in another checkout; see :func:`_rewrite_tracker_emojis`.
_TRACKER_LOCKS_GUARD = threading.Lock()
_MAX_CONFLICT_RETRIES = 3
_GITHUB_CALL_TIMEOUT_SECONDS = 60.0


class _TrackerLockState:
    """One path's thread-reentrant lock plus its outermost OS-lock handle."""

    def __init__(self) -> None:
        self.rlock = threading.RLock()
        self.depth = 0
        self.handle = None


_TRACKER_LOCKS: dict[str, _TrackerLockState] = {}


def _tracker_lock_path(cfg: ReviewConfig) -> Path:
    # ``runner-locks/`` is the review subsystem's existing ignored runtime
    # directory.  Keeping the persistent flock inode here prevents it from
    # dirtying the consumer checkout or leaking into auto-generated commits.
    return cfg.runner_lock_dir / f"tracker-parent-{cfg.parent_issue}.lock"


def _tracker_lock_state(path: Path) -> _TrackerLockState:
    key = str(path.resolve())
    with _TRACKER_LOCKS_GUARD:
        return _TRACKER_LOCKS.setdefault(key, _TrackerLockState())


def _lock_file(handle) -> None:
    if _fcntl is not None:
        _fcntl.flock(handle.fileno(), _fcntl.LOCK_EX)
        return
    if _msvcrt is not None:  # pragma: no cover - Windows-only branch
        handle.seek(0, 2)
        if handle.tell() == 0:
            handle.write(b"\0")
            handle.flush()
        handle.seek(0)
        _msvcrt.locking(handle.fileno(), _msvcrt.LK_LOCK, 1)


def _unlock_file(handle) -> None:
    if _fcntl is not None:
        _fcntl.flock(handle.fileno(), _fcntl.LOCK_UN)
        return
    if _msvcrt is not None:  # pragma: no cover - Windows-only branch
        handle.seek(0)
        _msvcrt.locking(handle.fileno(), _msvcrt.LK_UNLCK, 1)


@contextmanager
def _tracker_writer_lock(cfg: ReviewConfig):
    """Hold the stable tracker-writer lock across fresh read + remote write.

    Nested acquisition by the same thread shares the outer file descriptor and
    flock.  Opening and flocking the path again would deadlock against the
    process's own independent open-file description on POSIX.
    """
    path = _tracker_lock_path(cfg)
    state = _tracker_lock_state(path)
    with state.rlock:
        if state.depth == 0:
            path.parent.mkdir(parents=True, exist_ok=True)
            gitignore = path.parent / ".gitignore"
            if not gitignore.is_file():
                gitignore.write_text("*\n")
            state.handle = path.open("a+b")
            try:
                _lock_file(state.handle)
            except BaseException:
                state.handle.close()
                state.handle = None
                raise
        state.depth += 1
        try:
            yield
        finally:
            state.depth -= 1
            if state.depth == 0:
                handle = state.handle
                state.handle = None
                assert handle is not None
                try:
                    _unlock_file(handle)
                finally:
                    handle.close()


def _apply_emoji_flip(
    cfg: ReviewConfig,
    body: str,
    issue_num: int,
    new_emoji: str,
    old_emoji: str,
) -> tuple[Optional[str], str]:
    """Flip ONE issue's tracker line in ``body`` (a copy of the parent
    issue body), in memory. Returns ``(new_body, message)``; ``new_body``
    is None on a no-op (issue not registered, no tracker substring,
    chapter section missing, or the line already in the new state) and
    the message explains why.

    The substring→line match (chapter section bounds, first
    ``pattern``-bearing line still carrying ``old_emoji``) is the single
    source of truth both the single-flip and batched writers call — the
    matching logic lives here exactly once."""
    chapter = cfg.chapter_of_issue(issue_num)
    if chapter is None:
        return None, f"#{issue_num} not in any registered chapter"
    registry = cfg.chapter_registry(chapter)
    pattern = registry.pattern_for_issue(issue_num)
    if pattern is None:
        return None, f"#{issue_num} has no tracker substring in chapter {chapter}"

    chap_marker = cfg.tracker_section(chapter)
    chap_start = body.find(chap_marker)
    if chap_start == -1:
        return None, f"'{chap_marker}' not found in #{cfg.parent_issue}"
    chap_end = body.find("\n### Chapter", chap_start + 1)
    if chap_end == -1:
        chap_end = len(body)

    chapter_section = body[chap_start:chap_end]
    new_lines = []
    found = False
    for line in chapter_section.splitlines():
        if not found and pattern in line and old_emoji in line:
            new_lines.append(line.replace(old_emoji, new_emoji, 1))
            found = True
        else:
            new_lines.append(line)
    if not found:
        return None, (
            f"line matching '{pattern}' with {old_emoji} not found in "
            f"chapter {chapter}"
        )
    new_body = body[:chap_start] + "\n".join(new_lines) + body[chap_end:]
    return new_body, f"'{pattern}' line updated {old_emoji} → {new_emoji}"


def _plan_emoji_flips(
    cfg: ReviewConfig,
    body: str,
    flips: list[tuple[int, str]],
    old_emoji: str,
) -> tuple[str, list[int], list[str], list[str]]:
    """Apply a batch to one immutable remote-body snapshot in memory."""
    applied: list[int] = []
    applied_messages: list[str] = []
    skipped: list[str] = []
    result = body
    for issue_num, new_emoji in flips:
        new_body, msg = _apply_emoji_flip(
            cfg, result, issue_num, new_emoji, old_emoji
        )
        if new_body is None:
            skipped.append(f"#{issue_num}: {msg}")
            continue
        result = new_body
        applied.append(issue_num)
        applied_messages.append(msg)
    return result, applied, applied_messages, skipped


def _requested_states_present(
    cfg: ReviewConfig,
    body: str,
    flips: list[tuple[int, str]],
    old_emoji: str,
) -> bool:
    """Whether every requested line already carries its destination emoji.

    Reuse the exact chapter/pattern matcher in reverse: if replacing
    ``new_emoji`` back to ``old_emoji`` would match, the requested destination
    is present.  This distinguishes a successful write followed by an unrelated
    human prose edit from a writer that actually reverted our emoji.
    """
    for issue_num, new_emoji in flips:
        matched, _message = _apply_emoji_flip(
            cfg, body, issue_num, old_emoji, new_emoji
        )
        if matched is None:
            return False
    return True


def _read_tracker_body(cfg: ReviewConfig) -> Optional[str]:
    """Read the remote body with a bound lock-held GitHub call."""
    return issue_body(
        cfg.parent_issue,
        cfg.github_repo,
        timeout=_GITHUB_CALL_TIMEOUT_SECONDS,
    )


def _rewrite_tracker_emojis(
    cfg: ReviewConfig,
    flips: list[tuple[int, str]],
    old_emoji: str,
    *,
    tmp_dir: Path,
    temp_prefix: str,
) -> tuple[bool, list[int], list[str], list[str], Optional[str]]:
    """Conflict-safe whole-body rewrite shared by single + batch writers.

    Returns ``(written, applied, applied_messages, skipped, error)``. A human
    edit observed between planning and the pre-write check rebases the full
    plan. The body is read again after PATCH: a later competing edit that kept
    our requested states is accepted; one that reverted them is rebased and
    retried. After three continuously changing snapshots the operation aborts.

    GitHub does not expose conditional PATCH for this endpoint, so no client can
    close the final pre-check→PATCH window. A human update that lands inside
    that window and is immediately overwritten by PATCH is not recoverable from
    the issue endpoint. The post-write read detects every conflict still visible
    after PATCH and makes that limitation explicit rather than claiming CAS.
    """
    with _tracker_writer_lock(cfg):
        prior_write: Optional[tuple[list[int], list[str], list[str]]] = None
        for attempt in range(_MAX_CONFLICT_RETRIES):
            try:
                base = _read_tracker_body(cfg)
            except subprocess.TimeoutExpired:
                return False, [], [], [], (
                    "timed out loading the tracker body while holding the "
                    "writer lock; aborted without writing"
                )
            if base is None:
                return False, [], [], [], (
                    f"could not load parent issue #{cfg.parent_issue}"
                )

            planned, applied, applied_messages, skipped = _plan_emoji_flips(
                cfg, base, flips, old_emoji
            )
            if not applied:
                if prior_write is not None:
                    prior_applied, prior_messages, prior_skipped = prior_write
                    if _requested_states_present(
                        cfg, base, flips, old_emoji
                    ):
                        return (
                            True, prior_applied, prior_messages,
                            prior_skipped, None,
                        )
                    return False, [], [], [], (
                        "tracker body changed after update and no longer "
                        "contains a line where every requested emoji state "
                        "can be recovered safely"
                    )
                return False, applied, applied_messages, skipped, None

            # Human edits and writers from another checkout do not hold our
            # local flock. Re-read immediately before replacement; if the body
            # changed, discard the stale plan and rebase all flips onto the new
            # remote prose on the next iteration.
            try:
                latest = _read_tracker_body(cfg)
            except subprocess.TimeoutExpired:
                return False, [], [], [], (
                    "timed out confirming the tracker body before update; "
                    "aborted without writing"
                )
            if latest is None:
                return False, [], [], [], (
                    f"could not confirm parent issue #{cfg.parent_issue} "
                    "before update; aborted without writing"
                )
            if latest != base:
                if attempt + 1 < _MAX_CONFLICT_RETRIES:
                    continue
                return False, [], [], [], (
                    "tracker body changed concurrently during all "
                    f"{_MAX_CONFLICT_RETRIES} attempts; aborted without "
                    "overwriting the newer prose"
                )

            # The directory name is unique per invocation/process. The staged
            # file exists only for the synchronous gh call and is cleaned on
            # success or exception, preventing cross-chapter/body exchange.
            with tempfile.TemporaryDirectory(
                prefix=temp_prefix,
                dir=str(tmp_dir),
            ) as temp_name:
                body_path = Path(temp_name) / "tracker-body.md"
                body_path.write_text(planned, encoding="utf-8")
                try:
                    gh(
                        "issue", "edit", str(cfg.parent_issue),
                        "--repo", cfg.github_repo,
                        "--body-file", str(body_path),
                        timeout=_GITHUB_CALL_TIMEOUT_SECONDS,
                    )
                except subprocess.TimeoutExpired:
                    return False, [], [], [], (
                        "tracker update timed out; the remote outcome is "
                        "uncertain, so no automatic retry was attempted "
                        "(retry is idempotent)"
                    )

            try:
                observed = _read_tracker_body(cfg)
            except subprocess.TimeoutExpired:
                return False, [], [], [], (
                    "tracker update was submitted but post-write verification "
                    "timed out; remote outcome uncertain (retry is idempotent)"
                )
            if observed is None:
                return False, [], [], [], (
                    "tracker update was submitted but the resulting body could "
                    "not be verified; remote outcome uncertain (retry is "
                    "idempotent)"
                )
            if observed == planned:
                return True, applied, applied_messages, skipped, None
            if _requested_states_present(cfg, observed, flips, old_emoji):
                annotated = [
                    message + " (confirmed after a concurrent remote edit)"
                    for message in applied_messages
                ]
                return True, applied, annotated, skipped, None
            if attempt + 1 < _MAX_CONFLICT_RETRIES:
                prior_write = (applied, applied_messages, skipped)
                continue
            return False, [], [], [], (
                "tracker body changed after every update attempt and the "
                "requested emoji state was not retained; stopped rather than "
                "overwriting the newest observed body"
            )

    raise AssertionError("unreachable tracker rewrite state")


def update_tracker_emoji(
    cfg: ReviewConfig,
    issue_num: int,
    new_emoji: str,
    *,
    old_emoji: str = "🟠",
    tmp_dir: Path = Path("/tmp"),
) -> tuple[bool, str]:
    """Patch the parent-issue line for ``issue_num``, replacing
    ``old_emoji`` with ``new_emoji`` exactly once.

    Returns ``(ok, message)``. On a no-op (already in the new state, or
    not matched), ``ok`` is False and ``message`` explains why.
    """
    written, _applied, messages, skipped, error = _rewrite_tracker_emojis(
        cfg,
        [(issue_num, new_emoji)],
        old_emoji,
        tmp_dir=tmp_dir,
        temp_prefix=f"marathon-tracker-{cfg.parent_issue}-{issue_num}-",
    )
    if error is not None:
        return False, error
    if not written:
        return False, skipped[0].split(": ", 1)[-1] if skipped else (
            f"no tracker line flipped for #{issue_num}"
        )
    return True, messages[0]


def sync_tracker_to_verified(
    cfg: ReviewConfig,
    *,
    chapter: Optional[int] = None,
    tmp_dir: Path = Path("/tmp"),
) -> tuple[bool, str]:
    """Safety-net: flip every issue that IS verified (carries the
    ``review:verified`` label) but whose tracker line is still 🟠 → 🟡, so the
    parent tracker can't drift out of sync with the verified state.

    The verify/ship path already flips the tracker per issue; this catches
    stragglers — an issue verified out-of-band (a hand-added label), or a flip
    that silently no-op'd. ONE bulk label read + ONE tracker write (plus the
    conflict-check/verification reads; only still-🟠 lines flip). Scoped to
    ``chapter`` when given, else all registered chapters. Returns
    ``(ok, message)``; ``ok`` is True iff at least one line was flipped."""
    from marathon.review.github import fetch_issues_bulk

    if chapter is not None:
        registry = cfg.chapter_registry(chapter)
        regs = [registry] if registry is not None else []
    else:
        regs = list(cfg.chapters.values())
    nums = sorted({n for r in regs for (n, _t) in r.entries})
    if not nums:
        return False, "no registered issues to sync"

    meta = fetch_issues_bulk(nums, cfg.github_repo) or {}
    verified = [
        n for n in nums
        if "review:verified" in ((meta.get(n) or {}).get("labels") or [])
    ]
    if not verified:
        return False, "no verified issues found"
    # Only the lines still carrying 🟠 actually flip (update_tracker_emojis
    # no-ops a line that isn't in old_emoji), so passing every verified issue is
    # safe and idempotent.
    return update_tracker_emojis(
        cfg, [(n, "🟡") for n in verified], old_emoji="🟠", tmp_dir=tmp_dir
    )


def update_tracker_emojis(
    cfg: ReviewConfig,
    flips: list[tuple[int, str]],
    *,
    old_emoji: str = "🟡",
    tmp_dir: Path = Path("/tmp"),
) -> tuple[bool, str]:
    """BATCHED multi-flip: apply every ``(issue_num, new_emoji)`` edit to
    the parent body in one conflict-checked transaction + one
    ``gh issue edit`` write in the uncontended case.

    The write-storm ruling forbids one tracker-body rewrite per affected
    decl (crit-feas §5): the invalidation engine may flip dozens of
    downstream issues 🟡→🟠 in one audit run, and that must cost a single
    body rewrite. Each flip is applied to one fresh snapshot in memory through
    the shared :func:`_apply_emoji_flip` matcher, that snapshot is confirmed
    immediately before the write (and rebased on conflict), and the result is
    written once unless post-write verification observes that a competing
    writer reverted part of the batch, in which case the newer body is rebased
    and the remaining transitions are retried together.

    ``old_emoji`` defaults to 🟡 (the invalidation direction: a verified
    decl whose tier dropped goes back to 🟠), overridable per call.
    Returns ``(ok, message)``. ``ok`` is True iff at least one flip
    landed; the message reports how many of the N flips matched (the
    rest are no-ops — already 🟠, unregistered, or unmatched — and are
    listed so they are never silently dropped). An empty ``flips`` list,
    or a body that won't load, returns ``(False, …)`` and writes
    nothing."""
    if not flips:
        return False, "no flips requested"
    written, applied, _messages, skipped, error = _rewrite_tracker_emojis(
        cfg,
        flips,
        old_emoji,
        tmp_dir=tmp_dir,
        temp_prefix=f"marathon-tracker-{cfg.parent_issue}-batch-",
    )
    if error is not None:
        return False, error
    if not written:
        return False, (
            "no tracker lines flipped "
            + ("(" + "; ".join(skipped) + ")" if skipped else "")
        )
    msg = (
        f"flipped {len(applied)} tracker line(s) → {old_emoji}-replaced "
        f"in one rewrite (issues {', '.join(f'#{n}' for n in applied)})"
    )
    if skipped:
        msg += "; skipped " + "; ".join(skipped)
    return True, msg
