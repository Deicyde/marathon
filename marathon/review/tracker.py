"""Find + flip tracker emojis on the parent (LeeSM Tracker) issue body.

The parent issue (e.g. #1 in pitmonticone/GeometricAnalysis) has one
line per declaration with a status emoji at the start. When a sub-issue
transitions from unreviewed (🟠) to verified (🟡) or rejected (🟠 still,
because rejection is queued and not yet applied), this module patches
the corresponding line in the parent issue body.

The mapping between a sub-issue and its line is via the substring
recorded in :class:`marathon.review.config.ChapterRegistry`.
"""

from __future__ import annotations

from pathlib import Path

from marathon.review.config import ReviewConfig
from marathon.review.github import gh, issue_body


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
    chapter = cfg.chapter_of_issue(issue_num)
    if chapter is None:
        return False, f"#{issue_num} not in any registered chapter"
    registry = cfg.chapter_registry(chapter)
    pattern = registry.pattern_for_issue(issue_num)
    if pattern is None:
        return False, f"#{issue_num} has no tracker substring in chapter {chapter}"

    body = issue_body(cfg.parent_issue, cfg.github_repo)
    if body is None:
        return False, f"could not load parent issue #{cfg.parent_issue}"

    chap_marker = cfg.tracker_section(chapter)
    chap_start = body.find(chap_marker)
    if chap_start == -1:
        return False, f"'{chap_marker}' not found in #{cfg.parent_issue}"
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
        return False, (
            f"line matching '{pattern}' with {old_emoji} not found in "
            f"chapter {chapter}"
        )

    new_body = body[:chap_start] + "\n".join(new_lines) + body[chap_end:]
    tmp_path = tmp_dir / f"review-tracker-body-{issue_num}.md"
    tmp_path.write_text(new_body)
    gh(
        "issue", "edit", str(cfg.parent_issue),
        "--repo", cfg.github_repo,
        "--body-file", str(tmp_path),
    )
    return True, f"'{pattern}' line updated {old_emoji} → {new_emoji}"
