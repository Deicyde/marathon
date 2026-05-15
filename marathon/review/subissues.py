"""Bulk-create and bulk-refresh review sub-issues from a drafts file.

Two modes:

* :func:`create_subissues_from_drafts` — for first-time setup of a
  chapter. Reads ``drafts/<Chapter>.md``, creates one GitHub issue per
  draft section via ``gh issue create``, then attaches each as a
  sub-issue of the parent via the sub_issues REST endpoint.

* :func:`refresh_subissue_bodies` — for keeping existing issues in sync
  with a hand-edited drafts file. Walks the chapter's registry, reads
  the corresponding section from drafts, and ``gh issue edit`` the
  body in place. Issue history (comments, labels, sub-issue parent
  link) is preserved.
"""

from __future__ import annotations

from pathlib import Path
from typing import Optional

from marathon.review.config import ReviewConfig
from marathon.review.drafts import detect_chapter, parse_drafts
from marathon.review.github import gh


REVIEW_LABEL = "review"


def _chapter_label(chapter: int) -> str:
    return f"chapter-{chapter}"


def create_subissue(
    cfg: ReviewConfig,
    title: str,
    body_path: Path,
    chapter: int,
) -> tuple[str, str]:
    """Create a single GitHub issue, label it, attach it as a sub-issue.

    Returns ``(issue_number_str, issue_url)``.
    """
    cp = gh(
        "issue", "create",
        "--repo", cfg.github_repo,
        "--title", title,
        "--body-file", str(body_path),
        "--label", REVIEW_LABEL,
        "--label", _chapter_label(chapter),
    )
    url = cp.stdout.strip().splitlines()[-1]
    issue_num = url.rsplit("/", 1)[-1]

    id_cp = gh("api", f"/repos/{cfg.github_repo}/issues/{issue_num}", "--jq", ".id")
    child_id = id_cp.stdout.strip()

    gh(
        "api", "-X", "POST",
        f"/repos/{cfg.github_repo}/issues/{cfg.parent_issue}/sub_issues",
        "-F", f"sub_issue_id={child_id}",
    )
    return issue_num, url


def create_subissues_from_drafts(
    cfg: ReviewConfig,
    drafts_path: Path,
    skip: Optional[set[int]] = None,
    tmp_dir: Path = Path("/tmp"),
) -> list[tuple[int, str, str]]:
    """Walk the drafts file in entry order and create a sub-issue per
    entry. Skips entry numbers in ``skip``.

    Returns ``[(entry_idx, issue_num, url), ...]`` for entries that
    were successfully created (skipped + failed entries omitted).
    """
    skip = skip or set()
    chapter = detect_chapter(drafts_path)
    text = drafts_path.read_text()
    drafts = parse_drafts(text)
    print(f"Found {len(drafts)} draft sections in {drafts_path}")
    print(f"Chapter label: {_chapter_label(chapter)}")
    print()

    created: list[tuple[int, str, str]] = []
    for n in sorted(drafts.keys()):
        title, body = drafts[n]
        if n in skip:
            print(f"[{n:2d}] SKIP (--skip)")
            continue
        body_path = tmp_dir / f"review-draft-{n}-body.md"
        body_path.write_text(body)
        print(f"[{n:2d}] creating: {title[:80]}")
        try:
            issue_num, url = create_subissue(cfg, title, body_path, chapter)
        except RuntimeError as e:
            print(f"  ❌ {e}")
            continue
        print(f"  ✅ #{issue_num} ({url})")
        created.append((n, issue_num, url))

    print()
    print(f"Done. Sub-issues of #{cfg.parent_issue}:")
    list_cp = gh(
        "api",
        f"/repos/{cfg.github_repo}/issues/{cfg.parent_issue}/sub_issues",
        "--jq", '.[] | "#" + (.number|tostring) + "  " + .title',
    )
    print(list_cp.stdout)
    return created


def refresh_subissue_bodies(
    cfg: ReviewConfig,
    drafts_path: Path,
    only: Optional[set[int]] = None,
    tmp_dir: Path = Path("/tmp"),
) -> tuple[int, int]:
    """Refresh existing sub-issue bodies from the drafts file.

    Walks the chapter's registry (``cfg.chapter_registry(chapter)``),
    finds each entry's section in the drafts, and edits the GitHub
    issue body in place.

    ``only`` (1-based entry indices), if provided, restricts the refresh
    to that subset.

    Returns ``(refreshed_count, skipped_count)``.
    """
    chapter = detect_chapter(drafts_path)
    registry = cfg.chapter_registry(chapter)
    drafts = parse_drafts(drafts_path.read_text())

    refreshed = 0
    skipped = 0
    for entry_idx, (issue_num, _pattern) in enumerate(registry.entries, start=1):
        if only and entry_idx not in only:
            skipped += 1
            continue
        if entry_idx not in drafts:
            print(
                f"  entry {entry_idx} (#{issue_num}): no draft section found, skipping"
            )
            skipped += 1
            continue
        title, body = drafts[entry_idx]
        body_path = tmp_dir / f"review-refresh-{issue_num}-body.md"
        body_path.write_text(body)
        print(f"  refresh #{issue_num} (entry {entry_idx}): {title[:60]}")
        cp = gh(
            "issue", "edit", str(issue_num),
            "--repo", cfg.github_repo,
            "--body-file", str(body_path),
            check=False,
        )
        if cp.returncode != 0:
            print(f"    ❌ gh edit failed: {cp.stderr.strip()}")
            continue
        refreshed += 1
        print(f"    ✅ updated")

    print(f"\nDone. Refreshed: {refreshed}. Skipped: {skipped}.")
    return refreshed, skipped
