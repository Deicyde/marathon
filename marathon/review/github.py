"""Thin wrapper around the ``gh`` CLI used by the review commands."""

from __future__ import annotations

import json
import subprocess
from typing import Optional, Sequence


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


# --- bulk fetch (one GraphQL round-trip instead of N `gh issue view`s) --------


def fetch_issues_bulk(
    issue_nums: Sequence[int],
    repo: str,
) -> Optional[dict[int, dict]]:
    """Fetch title/state/body/labels for many issues in ONE ``gh api graphql``
    call.

    The per-issue helpers above (`issue_labels`, `issue_title`, ...) each
    cost a full ``gh issue view`` subprocess + HTTP round-trip. Callers
    that walk a whole chapter registry (``review list`` / ``review next``
    / the verified-decl audit) were issuing one per issue, serially — the
    N+1 latency the human feels on every interaction, and a rate-limit
    hazard. GraphQL lets us alias one ``issue(number:)`` field per issue
    and fetch them all in a single request.

    Returns ``{issue_num: {"title": str, "state": str, "body": str,
    "labels": set[str]}}``. Issues that don't resolve (deleted,
    transferred, no access) are simply absent from the dict — callers
    should fall back to the per-issue path for those.

    Returns ``None`` on any whole-call failure (gh missing, network/auth
    error, malformed response). Callers MUST treat ``None`` as "fall back
    to the per-issue helpers" and should print a warning so degraded
    performance is visible rather than silent.
    """
    # Dedupe while preserving order: duplicate numbers would generate
    # duplicate GraphQL aliases, which the API rejects outright.
    nums = list(dict.fromkeys(int(n) for n in issue_nums))
    if not nums:
        return {}
    if "/" not in repo:
        return None
    owner, name = repo.split("/", 1)

    # One aliased field per issue. Numbers are ints (validated above) so
    # inlining them into the query body is injection-safe; owner/name go
    # through GraphQL variables (`-f` = raw string, no type coercion).
    issue_fields = "\n".join(
        f"i{n}: issue(number: {n}) {{ number title state body "
        f"labels(first: 100) {{ nodes {{ name }} }} }}"
        for n in nums
    )
    query = (
        "query($owner: String!, $name: String!) {\n"
        "  repository(owner: $owner, name: $name) {\n"
        f"{issue_fields}\n"
        "  }\n"
        "}"
    )
    try:
        cp = gh(
            "api", "graphql",
            "-f", f"query={query}",
            "-f", f"owner={owner}",
            "-f", f"name={name}",
            check=False,
        )
    except OSError:  # gh binary missing/unrunnable
        return None
    if cp.returncode != 0:
        return None
    try:
        data = json.loads(cp.stdout)
    except (json.JSONDecodeError, TypeError):
        return None
    repository = (data.get("data") or {}).get("repository")
    if not isinstance(repository, dict):
        return None

    out: dict[int, dict] = {}
    for n in nums:
        node = repository.get(f"i{n}")
        if not node:
            # Partial failure (issue gone / inaccessible): omit so the
            # caller can per-issue-fallback just this one.
            continue
        labels_nodes = (node.get("labels") or {}).get("nodes") or []
        out[n] = {
            "title": node.get("title") or "(?)",
            "state": node.get("state") or "",
            "body": node.get("body") or "",
            "labels": {l["name"] for l in labels_nodes if l.get("name")},
        }
    return out
