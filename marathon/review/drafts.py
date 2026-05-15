"""Parse ``drafts/<Chapter>.md`` files used by review.

The drafts file is the human-authored markdown one section per
declaration. ``create_subissues`` reads it to bulk-create GitHub issues;
``refresh_subissue_bodies`` reads it to update existing issue bodies in
place.

Format::

    ## N/<total> — <description>

    **Title**: `<issue title>`

    [issue body — everything until the next `## N+1/...` or
    `## Coverage summary`]
"""

from __future__ import annotations

import re
from pathlib import Path


SECTION_PATTERN = re.compile(r"^## (\d+)/\d+ — (.+)$", re.MULTILINE)
TITLE_PATTERN = re.compile(r"\*\*Title\*\*: `(.+?)`")
SUMMARY_HEADER = "\n## Coverage summary"


def parse_drafts(text: str) -> dict[int, tuple[str, str]]:
    """Return ``{entry_idx: (title, body)}``.

    Entries without a ``**Title**: \\`...\\`` line are skipped (with a
    None title). Sections are delimited by the next ``## N/...`` heading
    or by ``## Coverage summary`` at the end of the file.
    """
    out: dict[int, tuple[str, str]] = {}
    matches = list(SECTION_PATTERN.finditer(text))
    for i, m in enumerate(matches):
        n = int(m.group(1))
        start = m.start()
        end = (
            matches[i + 1].start()
            if i + 1 < len(matches)
            else text.find(SUMMARY_HEADER)
        )
        if end == -1:
            end = len(text)
        section = text[start:end]

        title_match = TITLE_PATTERN.search(section)
        if not title_match:
            continue
        title = title_match.group(1)

        body_lines: list[str] = []
        for line in section.splitlines():
            if line.startswith("## ") and "/" in line and "—" in line:
                continue
            if line.startswith("**Title**:"):
                continue
            body_lines.append(line)
        body = "\n".join(body_lines).strip()
        out[n] = (title, body)
    return out


def detect_chapter(drafts_path: Path) -> int:
    """``Chapter14.md`` → 14. Exits if the filename doesn't match."""
    import sys
    m = re.match(r"Chapter(\d+)", drafts_path.stem)
    if not m:
        sys.exit(f"could not derive chapter from filename {drafts_path.name}")
    return int(m.group(1))
