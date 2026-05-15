"""Review configuration loader.

The previously-bundled scripts under ``.marathon/review/`` hardcoded a
handful of project-specific values: the GitHub repo name, the parent
issue number, the target-path template, per-chapter registries.
Pulling them into a config file lets the same package serve any number
of projects.

Config file location: ``<repo>/.marathon/review/config.toml``.

Example::

    github_repo = "pitmonticone/GeometricAnalysis"
    parent_issue = 1
    referee_path = ".marathon/referee.md"
    target_path_template = "GeometricAnalysis/LeeSM/Chapter{chapter}"
    tracker_section_pattern = "### Chapter {chapter}:"

    [labels]
    verified = "review:verified"
    rejected = "review:rejected"
    inflight = "review:in-flight-fix"

    [[chapters]]
    chapter = 14
    entries = [
      [14, "Define elementary alternating tensors"],
      [13, "Lemma 14.7"],
      # ...
    ]

The chapter-registry format mirrors the old ``REVIEW_REGISTRY`` dict
from ``review.py``: each entry is ``[issue_num, tracker_substring]`` in
LeeSM logical order. The substring must match exactly one line in the
chapter's section of the parent issue's body.
"""

from __future__ import annotations

import sys
import tomllib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional


CONFIG_RELPATH = Path(".marathon/review/config.toml")


@dataclass(frozen=True)
class ReviewLabels:
    verified: str = "review:verified"
    rejected: str = "review:rejected"
    inflight: str = "review:in-flight-fix"


@dataclass(frozen=True)
class ChapterRegistry:
    """Per-chapter ordered list of (issue_num, tracker_substring) entries.

    Order is the LeeSM (or analogous) logical order — not GitHub issue
    number order — so the reviewer walks entries in the order they appear
    in the parent issue's body.
    """

    chapter: int
    entries: list[tuple[int, str]]

    def issue_for_index(self, entry_idx: int) -> int:
        """1-based ``entry_idx`` → GitHub issue number."""
        if not 1 <= entry_idx <= len(self.entries):
            raise IndexError(
                f"entry_idx {entry_idx} out of range for chapter {self.chapter} "
                f"(1..{len(self.entries)})"
            )
        return self.entries[entry_idx - 1][0]

    def index_for_issue(self, issue_num: int) -> Optional[int]:
        """GitHub issue number → 1-based entry index, or None if absent."""
        for idx, (num, _) in enumerate(self.entries, start=1):
            if num == issue_num:
                return idx
        return None

    def pattern_for_issue(self, issue_num: int) -> Optional[str]:
        for num, pattern in self.entries:
            if num == issue_num:
                return pattern
        return None


@dataclass(frozen=True)
class ReviewConfig:
    """Parsed ``review/config.toml`` plus paths derived from the repo
    root the config was found under."""

    repo_dir: Path
    config_path: Path

    github_repo: str
    parent_issue: int
    referee_path: Path
    target_path_template: str
    tracker_section_pattern: str
    labels: ReviewLabels

    # Loaded chapter registries, keyed by chapter number.
    chapters: dict[int, ChapterRegistry] = field(default_factory=dict)

    # Review support directory paths — derived from repo_dir.
    @property
    def review_dir(self) -> Path:
        return self.repo_dir / ".marathon" / "review"

    @property
    def drafts_dir(self) -> Path:
        return self.review_dir / "drafts"

    @property
    def runner_lock_dir(self) -> Path:
        return self.review_dir / "runner-locks"

    @property
    def runner_log_dir(self) -> Path:
        return self.review_dir / "runner-logs"

    def chapter_of_issue(self, issue_num: int) -> Optional[int]:
        for chap, registry in self.chapters.items():
            if registry.index_for_issue(issue_num) is not None:
                return chap
        return None

    def chapter_registry(self, chapter: int) -> ChapterRegistry:
        if chapter not in self.chapters:
            sys.exit(
                f"chapter {chapter} not declared in {self.config_path}; "
                "add a `[[chapters]]` entry"
            )
        return self.chapters[chapter]

    def target_path(self, chapter: int) -> Path:
        return self.repo_dir / self.target_path_template.format(chapter=chapter)

    def tracker_section(self, chapter: int) -> str:
        return self.tracker_section_pattern.format(chapter=chapter)


def find_repo_dir(start: Optional[Path] = None) -> Path:
    """Walk up from ``start`` (default: cwd) to find a directory with a
    ``.git`` entry. Used so ``marathon review ...`` can be run from
    any subdir of the consumer repo."""
    cur = (start or Path.cwd()).resolve()
    for candidate in [cur, *cur.parents]:
        if (candidate / ".git").exists():
            return candidate
    sys.exit(
        f"no git repo found above {cur}; run `marathon review ...` "
        "from inside the consumer repo"
    )


def load_config(repo_dir: Optional[Path] = None) -> ReviewConfig:
    """Load ``<repo>/.marathon/review/config.toml``.

    ``repo_dir`` defaults to the result of :func:`find_repo_dir`.
    """
    if repo_dir is None:
        repo_dir = find_repo_dir()
    config_path = repo_dir / CONFIG_RELPATH
    if not config_path.is_file():
        sys.exit(
            f"review config not found at {config_path}. Create it — see "
            "the docstring of `marathon.review.config` for the schema."
        )

    with config_path.open("rb") as f:
        data = tomllib.load(f)

    try:
        github_repo = data["github_repo"]
        parent_issue = int(data["parent_issue"])
        target_path_template = data["target_path_template"]
    except KeyError as e:
        sys.exit(f"missing required field in {config_path}: {e}")

    referee_rel = data.get("referee_path", ".marathon/referee.md")
    referee_path = repo_dir / referee_rel

    tracker_section_pattern = data.get(
        "tracker_section_pattern", "### Chapter {chapter}:"
    )

    labels_raw = data.get("labels", {})
    labels = ReviewLabels(
        verified=labels_raw.get("verified", "review:verified"),
        rejected=labels_raw.get("rejected", "review:rejected"),
        inflight=labels_raw.get("inflight", "review:in-flight-fix"),
    )

    chapters: dict[int, ChapterRegistry] = {}
    for entry in data.get("chapters", []):
        chap = int(entry["chapter"])
        raw_entries = entry.get("entries", [])
        parsed: list[tuple[int, str]] = []
        for row in raw_entries:
            if len(row) != 2:
                sys.exit(
                    f"chapter {chap} registry row {row!r} must be "
                    "[issue_number, tracker_substring]"
                )
            parsed.append((int(row[0]), str(row[1])))
        chapters[chap] = ChapterRegistry(chapter=chap, entries=parsed)

    return ReviewConfig(
        repo_dir=repo_dir,
        config_path=config_path,
        github_repo=github_repo,
        parent_issue=parent_issue,
        referee_path=referee_path,
        target_path_template=target_path_template,
        tracker_section_pattern=tracker_section_pattern,
        labels=labels,
        chapters=chapters,
    )
