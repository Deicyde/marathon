"""Append rejection bullets to referee.md's user-managed header.

The auto-refine daemon (``marathon.review.daemon``) hashes the header
above the ``BEGIN: Marathon-managed referee tail`` sentinel; when the
hash changes, it fires a new ``marathon refine`` iteration. Appending a
rejection bullet here is the canonical way to queue a fix.
"""

from __future__ import annotations

from pathlib import Path


SENTINEL = "<!-- BEGIN: Marathon-managed referee tail"


def append_rejection_bullet(referee_path: Path, issue_num: int, notes: str) -> bool:
    """Append a ``- **Review #<num> REJECTED**`` block to the user header.

    Returns True on success, False if ``referee_path`` doesn't exist
    (caller should warn). If the sentinel isn't present in the file, the
    bullet is appended at the end so the queue still works on
    sentinel-less referee.md files (the daemon then hashes the entire
    file).
    """
    if not referee_path.is_file():
        return False

    text = referee_path.read_text()
    bullet = (
        f"\n- **Review #{issue_num} REJECTED**\n"
        + "\n".join(
            f"  {line}" if line.strip() else line for line in notes.splitlines()
        )
        + "\n"
    )
    idx = text.find(SENTINEL)
    if idx == -1:
        new_text = text.rstrip() + "\n" + bullet + "\n"
    else:
        before = text[:idx].rstrip()
        after = text[idx:]
        new_text = before + "\n" + bullet + "\n" + after
    referee_path.write_text(new_text)
    return True
