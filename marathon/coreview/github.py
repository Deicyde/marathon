"""Thin wrapper around the ``gh`` CLI used by the coreview commands."""

from __future__ import annotations

import json
import subprocess
from typing import Optional


def gh(
    *args: str,
    check: bool = True,
    capture: bool = True,
) -> subprocess.CompletedProcess:
    """Invoke ``gh`` with the given arguments.

    Captures stdout+stderr by default. Raises ``RuntimeError`` on
    non-zero exit when ``check`` is True. Matches the helper used by
    the previously-bundled scripts so behavior is preserved exactly.
    """
    cp = subprocess.run(
        ["gh", *args],
        capture_output=capture,
        text=True,
        check=False,
    )
    if check and cp.returncode != 0:
        raise RuntimeError(f"gh {' '.join(args)} failed: {cp.stderr.strip()}")
    return cp


def issue_view_json(
    issue_num: int,
    repo: str,
    fields: str,
    *,
    check: bool = True,
) -> Optional[dict]:
    """``gh issue view ... --json fields`` → parsed dict (or None on failure)."""
    cp = gh(
        "issue", "view", str(issue_num),
        "--repo", repo,
        "--json", fields,
        check=check,
    )
    if cp.returncode != 0:
        return None
    return json.loads(cp.stdout)


def issue_labels(issue_num: int, repo: str) -> Optional[set[str]]:
    data = issue_view_json(issue_num, repo, "labels", check=False)
    if data is None:
        return None
    return {l["name"] for l in data.get("labels", [])}


def issue_title(issue_num: int, repo: str) -> str:
    data = issue_view_json(issue_num, repo, "title", check=False)
    return (data or {}).get("title", "(?)")


def issue_body(issue_num: int, repo: str) -> Optional[str]:
    data = issue_view_json(issue_num, repo, "body", check=False)
    return (data or {}).get("body")
