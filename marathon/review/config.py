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

The ``[[chapters]]`` registry is **machine-managed**: it is rewritten
wholesale by :func:`register_chapter` / :func:`update_chapter_entries`
(CLI: ``marathon review register-chapter``). Hand-editing it was a
documented desync source — the bootstrap/audit coreviewer agents used
to patch it by hand and the registry drifted from GitHub reality (plan
§1 "seven state surfaces"; recon report-review-subsystem §2 "third
state surface"). Everything *above* the registry block (top-level
keys, ``[labels]``, comments) is hand-edited territory and is
preserved byte-for-byte by the writer.
"""

from __future__ import annotations

import os
import re
import sys
import tomllib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional, Sequence


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

    # Additional writable-path whitelist for `marathon refine`'s
    # extractor (beyond the primary chapter folder + other registered
    # chapter folders). Use this for vendor directories the project
    # permits Aristotle to write into (e.g. backport vendor files under
    # ``Mathlib_4_30/``). Paths are repo-relative POSIX paths.
    extra_writable_paths: list[Path] = field(default_factory=list)

    # Review support directory paths — derived from repo_dir.
    @property
    def review_dir(self) -> Path:
        return self.repo_dir / ".marathon" / "review"

    @property
    def state_path(self) -> Path:
        """Per-issue rejection state JSON (see ``marathon.review.state``).
        Always ``<repo>/.marathon/review/state.json``."""
        return self.review_dir / "state.json"

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

    try:
        chapters = _parse_chapter_tables(data.get("chapters", []))
    except (KeyError, TypeError, ValueError) as e:
        sys.exit(f"{config_path}: {e}")

    extra_raw = data.get("extra_writable_paths", []) or []
    if not isinstance(extra_raw, list):
        sys.exit(
            f"{config_path}: `extra_writable_paths` must be a list of "
            "repo-relative path strings"
        )
    extra_writable_paths: list[Path] = []
    for p in extra_raw:
        if not isinstance(p, str):
            sys.exit(
                f"{config_path}: each entry in `extra_writable_paths` "
                f"must be a string; got {p!r}"
            )
        # Store as a Path with no leading slash; the extractor builds
        # POSIX path tuples for tar-member comparison.
        extra_writable_paths.append(Path(p.lstrip("/").rstrip("/")))

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
        extra_writable_paths=extra_writable_paths,
    )


# --- chapters-registry programmatic writer ------------------------------------
#
# WHY a writer at all: until marathon v2 phase 1, the bootstrap/audit
# coreviewer briefings told the Claude agent to hand-edit the
# ``[[chapters]]`` block. An LLM hand-editing a TOML registry that the
# review CLI then trusts blindly is exactly the state-drift disease the
# v2 plan diagnoses. The writer below makes the registry a CLI-owned
# surface: read the whole file with tomllib (refuse loudly if it does
# not parse), keep everything *before* the first ``[[chapters]]`` header
# byte-for-byte, and regenerate the registry block in one stable,
# commented format. Anything that would force a risky rewrite (TOML
# that doesn't parse, non-chapters tables *after* the registry block)
# is a hard refusal, never a best-effort mangle.


class RegistryEditError(RuntimeError):
    """The chapters registry cannot be edited safely.

    Raised (never ``sys.exit``) so library callers and tests can catch
    it; the CLI layer converts it to a clean exit. The file on disk is
    guaranteed untouched when this is raised.
    """


# First line of the regenerated registry block. Doubles as the split
# marker on subsequent rewrites so the banner never accumulates.
_REGISTRY_BANNER_PREFIX = "# --- chapter registries"

_REGISTRY_BANNER = (
    f"{_REGISTRY_BANNER_PREFIX} (machine-managed) "
    "----------------------------------\n"
    "# Do NOT hand-edit this block — it is regenerated wholesale by\n"
    "# `marathon review register-chapter` (see marathon.review.config).\n"
    '# Each entry is [issue_number, "tracker substring"] in textbook order;\n'
    "# the substring must match exactly one line in the parent issue's\n"
    "# chapter section.\n"
)

# A `[[chapters]]` array-of-tables header alone on a line (optionally
# followed by a comment). Content inside multi-line strings that merely
# *looks* like the header is caught later by the tail-parse and
# round-trip checks, which abort before writing.
_CHAPTERS_HEADER_RE = re.compile(r"^\s*\[\[\s*chapters\s*\]\]\s*(#.*)?$")


def parse_entry_arg(raw: str) -> tuple[int, str]:
    """Parse one ``--entry "ISSUE:SUBSTRING"`` CLI argument.

    Split on the *first* colon only — tracker substrings may themselves
    contain colons ("Theorem 14.9: uniqueness"). Raises ``ValueError``
    with a usage-shaped message on malformed input.
    """
    issue_s, sep, substring = raw.partition(":")
    substring = substring.strip()
    if not sep or not substring:
        raise ValueError(
            f'--entry must look like "ISSUE_NUM:TRACKER_SUBSTRING" '
            f'(e.g. "14:Lemma 14.7"); got {raw!r}'
        )
    try:
        issue_num = int(issue_s.strip())
    except ValueError:
        raise ValueError(
            f"--entry issue number must be an integer; got {issue_s!r} "
            f"in {raw!r}"
        ) from None
    return issue_num, substring


def _parse_chapter_tables(raw_chapters: object) -> dict[int, ChapterRegistry]:
    """``data["chapters"]`` (tomllib output) → ``dict[int, ChapterRegistry]``.

    Shared by :func:`load_config` (which wraps errors in ``sys.exit``)
    and the registry writer (which wraps them in
    :class:`RegistryEditError`). Raises ``ValueError`` / ``KeyError`` /
    ``TypeError`` on malformed input.
    """
    if raw_chapters is None:
        raw_chapters = []
    if not isinstance(raw_chapters, list):
        raise ValueError(
            "`chapters` must be a [[chapters]] array-of-tables, not a "
            f"plain key (got {type(raw_chapters).__name__})"
        )
    chapters: dict[int, ChapterRegistry] = {}
    for entry in raw_chapters:
        chap = int(entry["chapter"])
        if chap in chapters:
            raise ValueError(f"duplicate [[chapters]] table for chapter {chap}")
        parsed: list[tuple[int, str]] = []
        for row in entry.get("entries", []):
            if len(row) != 2:
                raise ValueError(
                    f"chapter {chap} registry row {row!r} must be "
                    "[issue_number, tracker_substring]"
                )
            parsed.append((int(row[0]), str(row[1])))
        chapters[chap] = ChapterRegistry(chapter=chap, entries=parsed)
    return chapters


def _validate_entries(
    chapter: int, entries: Sequence[Sequence[object]]
) -> list[tuple[int, str]]:
    """Normalize/validate an ordered entry list → ``[(issue, substring)]``.

    Hard requirements: non-empty; pairs; positive int issue numbers,
    unique within the chapter; non-empty single-line substrings (a
    substring must match exactly one *line* of the tracker body, so an
    embedded newline can never be right).
    """
    if not entries:
        raise RegistryEditError(
            f"chapter {chapter}: refusing to write an empty entry list; "
            "pass at least one [issue_num, tracker_substring] entry"
        )
    normalized: list[tuple[int, str]] = []
    seen: set[int] = set()
    for row in entries:
        if len(row) != 2:
            raise RegistryEditError(
                f"chapter {chapter} entry {row!r} must be "
                "[issue_number, tracker_substring]"
            )
        try:
            num = int(row[0])  # type: ignore[arg-type]
        except (TypeError, ValueError):
            raise RegistryEditError(
                f"chapter {chapter} entry {row!r}: issue number must be "
                "an integer"
            ) from None
        substring = str(row[1]).strip()
        if num <= 0:
            raise RegistryEditError(
                f"chapter {chapter} entry {row!r}: issue number must be "
                "positive"
            )
        if num in seen:
            raise RegistryEditError(
                f"chapter {chapter}: issue #{num} appears twice in the "
                "entry list"
            )
        if not substring:
            raise RegistryEditError(
                f"chapter {chapter} entry for issue #{num}: tracker "
                "substring is empty"
            )
        if "\n" in substring or "\r" in substring:
            raise RegistryEditError(
                f"chapter {chapter} entry for issue #{num}: tracker "
                "substring must be a single line"
            )
        seen.add(num)
        normalized.append((num, substring))
    return normalized


def _toml_basic_string(s: str) -> str:
    """Render ``s`` as a TOML basic (double-quoted) string."""
    out: list[str] = []
    for ch in s:
        if ch == "\\":
            out.append("\\\\")
        elif ch == '"':
            out.append('\\"')
        elif ord(ch) < 0x20 or ch == "\x7f":
            out.append(f"\\u{ord(ch):04X}")
        else:
            out.append(ch)
    return '"' + "".join(out) + '"'


def render_chapters_block(chapters: dict[int, ChapterRegistry]) -> str:
    """The full machine-managed registry block, banner included.

    Stable format: chapters sorted numerically, one entry per line, so
    rewrites produce minimal git diffs and the block is trivially
    reviewable in a consumer-repo commit.
    """
    lines: list[str] = [_REGISTRY_BANNER.rstrip("\n")]
    for chap in sorted(chapters):
        reg = chapters[chap]
        lines.append("")
        lines.append("[[chapters]]")
        lines.append(f"chapter = {chap}")
        if reg.entries:
            lines.append("entries = [")
            for num, substring in reg.entries:
                lines.append(f"  [{num}, {_toml_basic_string(substring)}],")
            lines.append("]")
        else:
            lines.append("entries = []")
    return "\n".join(lines) + "\n"


def _split_at_registry(text: str) -> tuple[str, str]:
    """Split config text at the start of the registry block.

    The split point is the first line that is either a previously
    written banner (so banners never accumulate) or a bare
    ``[[chapters]]`` header (hand-written registries from before this
    writer existed). Returns ``(head, tail)``; ``tail`` is ``""`` when
    no registry exists yet.
    """
    lines = text.splitlines(keepends=True)
    for i, line in enumerate(lines):
        stripped = line.strip()
        if stripped.startswith(_REGISTRY_BANNER_PREFIX) or _CHAPTERS_HEADER_RE.match(
            line.rstrip("\n")
        ):
            return "".join(lines[:i]), "".join(lines[i:])
    return text, ""


def _ensure_tail_is_only_chapters(config_path: Path, tail: str) -> None:
    """Refuse if anything other than ``[[chapters]]`` tables follows the
    first registry header — those keys would be silently swallowed by
    the wholesale block rewrite."""
    if not tail:
        return
    try:
        tail_data = tomllib.loads(tail)
    except tomllib.TOMLDecodeError as e:
        raise RegistryEditError(
            f"{config_path}: cannot isolate the [[chapters]] block "
            f"(content from the first [[chapters]] header onward does "
            f"not parse standalone: {e}); refusing to rewrite — fix the "
            "file by hand first"
        )
    extra = sorted(set(tail_data) - {"chapters"})
    if extra:
        raise RegistryEditError(
            f"{config_path}: non-chapters config {extra} appears *after* "
            "the first [[chapters]] table. The registry block is "
            "regenerated wholesale and would swallow those keys — move "
            "them above the [[chapters]] block, then retry"
        )


def _load_for_edit(repo_dir: Path) -> tuple[Path, str, dict, dict[int, ChapterRegistry]]:
    """Read + fully validate config.toml before any mutation.

    Returns ``(config_path, raw_text, parsed_data, chapters)``. Every
    refusal happens here or in the pure checks above — by the time we
    write, the only remaining failure mode is the filesystem.
    """
    config_path = Path(repo_dir) / CONFIG_RELPATH
    if not config_path.is_file():
        raise RegistryEditError(
            f"review config not found at {config_path}; create it first "
            "(see the marathon.review.config docstring for the schema)"
        )
    text = config_path.read_text()
    try:
        data = tomllib.loads(text)
    except tomllib.TOMLDecodeError as e:
        raise RegistryEditError(
            f"{config_path} does not parse as TOML ({e}); refusing to "
            "rewrite a file I cannot read back — fix it by hand first"
        )
    try:
        chapters = _parse_chapter_tables(data.get("chapters"))
    except (KeyError, TypeError, ValueError) as e:
        raise RegistryEditError(f"{config_path}: {e}")
    return config_path, text, data, chapters


def _validate_target_path(
    repo_dir: Path, data: dict, config_path: Path, chapter: int, target_path: Path | str
) -> None:
    """Guard against registering a chapter under the wrong number.

    The config has a single ``target_path_template`` — per-chapter
    target paths are not representable — so the caller-supplied path is
    purely a cross-check: it must equal the template instantiated at
    ``chapter``. Catches the classic off-by-one ("register Chapter15's
    issues as chapter 14") before it poisons the registry.
    """
    template = data.get("target_path_template")
    if not isinstance(template, str) or not template:
        raise RegistryEditError(
            f"{config_path}: missing `target_path_template`; the config "
            "is incomplete — fix it before registering chapters"
        )
    expected = template.format(chapter=chapter).strip("/")
    given = Path(target_path)
    if given.is_absolute():
        try:
            given = given.resolve().relative_to(Path(repo_dir).resolve())
        except ValueError:
            raise RegistryEditError(
                f"--target {target_path} is outside the repo {repo_dir}"
            ) from None
    norm = given.as_posix().strip("/")
    if norm != expected:
        raise RegistryEditError(
            f"--target {norm!r} does not match the config's "
            f"target_path_template for chapter {chapter} (expected "
            f"{expected!r}). The registry cannot express per-chapter "
            "target paths; if the folder really lives elsewhere, fix "
            f"`target_path_template` in {config_path} first"
        )


def _rewrite_registry(
    config_path: Path, text: str, chapters: dict[int, ChapterRegistry]
) -> str:
    """Regenerate the registry block and atomically rewrite the file.

    Head (everything before the block) is preserved byte-for-byte; the
    only normalization is ensuring exactly one blank separator line at
    the boundary (stable across repeated rewrites). The assembled text
    is parsed back and its chapters compared to the intended mapping
    before anything touches disk — a failed sanity check aborts with
    the original file intact.
    """
    head, tail = _split_at_registry(text)
    _ensure_tail_is_only_chapters(config_path, tail)
    block = render_chapters_block(chapters)
    if head and not head.endswith("\n"):
        head += "\n"
    if head and not head.endswith("\n\n"):
        head += "\n"
    new_text = head + block

    try:
        reparsed = _parse_chapter_tables(tomllib.loads(new_text).get("chapters"))
    except (tomllib.TOMLDecodeError, KeyError, TypeError, ValueError) as e:
        raise RegistryEditError(
            f"internal error: regenerated {config_path} failed its "
            f"read-back check ({e}); aborting without writing"
        )
    if reparsed != chapters:
        raise RegistryEditError(
            f"internal error: regenerated {config_path} round-tripped to "
            "a different registry; aborting without writing"
        )

    tmp = config_path.with_name(config_path.name + ".tmp")
    tmp.write_text(new_text)
    os.replace(tmp, config_path)
    return block


def register_chapter(
    repo_dir: Path,
    chapter: int,
    target_path: Path | str,
    entries: Sequence[Sequence[object]],
    *,
    replace: bool = False,
) -> str:
    """Register chapter ``chapter`` in the ``[[chapters]]`` registry.

    ``entries`` is the ordered ``[issue_num, tracker_substring]`` list
    (textbook order — the order the reviewer walks). ``target_path`` is
    a cross-check against ``target_path_template`` (see
    :func:`_validate_target_path`). An already-registered chapter is a
    refusal unless ``replace=True`` (CLI ``--replace``), in which case
    its entry list is overwritten wholesale.

    Returns the rendered registry block for display. Raises
    :class:`RegistryEditError` on any refusal; the file is untouched.
    """
    config_path, text, data, chapters = _load_for_edit(repo_dir)
    normalized = _validate_entries(chapter, entries)
    _validate_target_path(repo_dir, data, config_path, chapter, target_path)
    if chapter in chapters and not replace:
        raise RegistryEditError(
            f"chapter {chapter} is already registered in {config_path} "
            f"({len(chapters[chapter].entries)} entries). Pass --replace "
            "(or call update_chapter_entries) with the FULL entry list "
            "to overwrite it"
        )
    chapters[chapter] = ChapterRegistry(chapter=chapter, entries=normalized)
    return _rewrite_registry(config_path, text, chapters)


def update_chapter_entries(
    repo_dir: Path,
    chapter: int,
    entries: Sequence[Sequence[object]],
) -> str:
    """Overwrite an *existing* chapter's entry list (full list, in order).

    The complement of :func:`register_chapter`: refuses when the
    chapter is not yet registered (use ``register-chapter`` — it also
    cross-checks the target path). Returns the rendered registry block.
    """
    config_path, text, _data, chapters = _load_for_edit(repo_dir)
    normalized = _validate_entries(chapter, entries)
    if chapter not in chapters:
        raise RegistryEditError(
            f"chapter {chapter} is not registered in {config_path}; use "
            "`marathon review register-chapter` (it also validates the "
            "target path)"
        )
    chapters[chapter] = ChapterRegistry(chapter=chapter, entries=normalized)
    return _rewrite_registry(config_path, text, chapters)
