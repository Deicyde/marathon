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
  forbids one tracker-body rewrite per affected decl: this helper reads
  the parent body ONCE, applies every substring→emoji edit in memory,
  and writes ONCE — substring surgery happens a single time per audit
  run regardless of how many issues flip.
"""

from __future__ import annotations

from pathlib import Path
from typing import Optional

from marathon.review.config import ReviewConfig
from marathon.review.github import gh, issue_body


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
    body = issue_body(cfg.parent_issue, cfg.github_repo)
    if body is None:
        return False, f"could not load parent issue #{cfg.parent_issue}"

    new_body, msg = _apply_emoji_flip(
        cfg, body, issue_num, new_emoji, old_emoji
    )
    if new_body is None:
        return False, msg

    tmp_path = tmp_dir / f"review-tracker-body-{issue_num}.md"
    tmp_path.write_text(new_body)
    gh(
        "issue", "edit", str(cfg.parent_issue),
        "--repo", cfg.github_repo,
        "--body-file", str(tmp_path),
    )
    return True, msg


def update_tracker_emojis(
    cfg: ReviewConfig,
    flips: list[tuple[int, str]],
    *,
    old_emoji: str = "🟡",
    tmp_dir: Path = Path("/tmp"),
) -> tuple[bool, str]:
    """BATCHED multi-flip: apply every ``(issue_num, new_emoji)`` edit to
    the parent body in ONE read + ONE ``gh issue edit`` write.

    The write-storm ruling forbids one tracker-body rewrite per affected
    decl (crit-feas §5): the invalidation engine may flip dozens of
    downstream issues 🟡→🟠 in one audit run, and that must cost a single
    body rewrite. So the parent body is fetched once, each flip is
    applied in memory through the shared :func:`_apply_emoji_flip`
    matcher (same line logic as the single-flip writer), and the result
    is written once.

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
    body = issue_body(cfg.parent_issue, cfg.github_repo)
    if body is None:
        return False, f"could not load parent issue #{cfg.parent_issue}"

    applied: list[int] = []
    skipped: list[str] = []
    for issue_num, new_emoji in flips:
        new_body, msg = _apply_emoji_flip(
            cfg, body, issue_num, new_emoji, old_emoji
        )
        if new_body is None:
            skipped.append(f"#{issue_num}: {msg}")
            continue
        body = new_body  # accumulate edits so each writes onto the last
        applied.append(issue_num)

    if not applied:
        return False, (
            "no tracker lines flipped "
            + ("(" + "; ".join(skipped) + ")" if skipped else "")
        )

    # ONE write for all N flips — the substring surgery already happened
    # in memory above.
    tmp_path = tmp_dir / "review-tracker-body-batch.md"
    tmp_path.write_text(body)
    gh(
        "issue", "edit", str(cfg.parent_issue),
        "--repo", cfg.github_repo,
        "--body-file", str(tmp_path),
    )
    msg = (
        f"flipped {len(applied)} tracker line(s) → {old_emoji}-replaced "
        f"in one rewrite (issues {', '.join(f'#{n}' for n in applied)})"
    )
    if skipped:
        msg += "; skipped " + "; ".join(skipped)
    return True, msg
