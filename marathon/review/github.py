"""Thin wrapper around the ``gh`` CLI used by the review commands."""

from __future__ import annotations

import contextlib
import contextvars
from concurrent.futures import ThreadPoolExecutor, as_completed
import hashlib
import json
import os
import subprocess
from typing import Iterator, Optional, Sequence
from urllib.parse import quote


# Per-request GitHub identity. When the deck is shared behind an authenticating
# proxy, each collaborator's OWN GitHub OAuth token is bound here for the life of
# their request, so every gh() write (comment / label / PR merge / issue-body
# edit) authenticates AND is attributed to *them*, not the host's ambient gh
# auth. Unset — the default, and the entire single-user CLI path — means gh()
# uses the host's ambient auth, exactly as before.
_ACTOR_GH_TOKEN: "contextvars.ContextVar[Optional[str]]" = contextvars.ContextVar(
    "marathon_actor_gh_token", default=None
)


@contextlib.contextmanager
def actor_github_token(token: Optional[str]) -> "Iterator[None]":
    """Bind ``token`` as the acting GitHub identity for every :func:`gh` call
    made in this context (same thread). A falsy/blank token is a no-op — gh()
    falls back to the host's ambient auth — so the local single-user path is
    completely unchanged.

    SCOPE: this covers only SYNCHRONOUS gh() writes made within the context (the
    deck's verdict/ship/refresh handlers). It deliberately does NOT reach the
    detached conductor (a separate process spawned with start_new_session) — its
    later PR merges + Aristotle refines run under whatever credentials launched
    it, never this short-lived per-request token. On a shared deck, run the
    conductor per-collaborator (see ``--no-host-conductor``) for that half."""
    tok = (token or "").strip() or None
    reset = _ACTOR_GH_TOKEN.set(tok)
    try:
        yield
    finally:
        _ACTOR_GH_TOKEN.reset(reset)


def gh(
    *args: str,
    check: bool = True,
    capture: bool = True,
    timeout: Optional[float] = None,
    input_text: Optional[str] = None,
) -> subprocess.CompletedProcess:
    """Invoke ``gh`` with the given arguments.

    Captures stdout+stderr by default. Raises ``RuntimeError`` on
    non-zero exit when ``check`` is True. Matches the helper used by
    the previously-bundled scripts so behavior is preserved exactly.

    ``timeout`` (seconds) bounds the subprocess; on expiry ``subprocess.run``
    raises ``subprocess.TimeoutExpired`` — callers on a request-handler path
    should catch it. ``None`` (default) preserves the original unbounded call.

    ``input_text`` is passed on stdin.  This is the safe path for large issue
    comments: their mathematical/review content never appears in argv or in a
    command-shaped exception.

    If a per-request actor token is bound (see :func:`actor_github_token`),
    it is injected as ``GH_TOKEN`` so the call authenticates as that
    collaborator; otherwise ``gh`` uses the host's ambient auth.
    """
    tok = _ACTOR_GH_TOKEN.get()
    env = {**os.environ, "GH_TOKEN": tok} if tok else None
    cp = subprocess.run(
        ["gh", *args],
        capture_output=capture,
        text=True,
        check=False,
        env=env,
        timeout=timeout,
        input=input_text,
    )
    if check and cp.returncode != 0:
        raise RuntimeError(f"gh {' '.join(args)} failed: {cp.stderr.strip()}")
    return cp


def post_issue_comment_rest(
    issue_num: int,
    repo: str,
    body: str,
    *,
    operation_id: Optional[str] = None,
    timeout: float = 15.0,
) -> bool:
    """Post one issue comment through REST, keeping ``body`` off argv.

    ``gh issue comment`` uses GitHub's GraphQL quota even though issue comments
    have a first-class REST endpoint.  Review ships must not fail merely because
    GraphQL is exhausted while the REST core quota is healthy.

    When ``operation_id`` is supplied, a deterministic hidden marker makes the
    write retry-safe across a later label/local-state failure (and even an
    ambiguous POST timeout).  ``False`` means that marker already existed and
    the prior comment was reused; ``True`` means this call published it.
    """
    if operation_id:
        marker_digest = hashlib.sha256(operation_id.encode("utf-8")).hexdigest()
        marker = f"<!-- marathon-verdict-operation:{marker_digest} -->"
        exists = _issue_comment_marker_exists_rest(
            issue_num, repo, marker, timeout=min(timeout, 10.0)
        )
        if exists is None:
            raise RuntimeError(
                f"GitHub REST comment state for #{issue_num} could not be "
                "confirmed; no duplicate comment was attempted"
            )
        if exists:
            return False
        body = f"{body}\n\n{marker}"

    endpoint = f"repos/{repo}/issues/{issue_num}/comments"
    try:
        cp = gh(
            "api", "--method", "POST", endpoint, "--input", "-",
            check=False,
            timeout=timeout,
            input_text=json.dumps({"body": body}, ensure_ascii=False),
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise RuntimeError(
            f"GitHub REST comment for #{issue_num} could not be published: {exc}"
        ) from exc
    if cp.returncode != 0:
        detail = ((cp.stderr or cp.stdout) or "unknown GitHub error").strip()
        raise RuntimeError(
            f"GitHub REST comment for #{issue_num} could not be published: "
            f"{detail[:500]}"
        )
    return True


def _issue_comment_marker_exists_rest(
    issue_num: int,
    repo: str,
    marker: str,
    *,
    timeout: float = 10.0,
) -> Optional[bool]:
    """Whether any issue comment contains ``marker``; ``None`` if unknown."""
    endpoint = f"repos/{repo}/issues/{issue_num}/comments?per_page=100"
    try:
        cp = gh(
            "api", "--paginate", "--slurp", endpoint,
            check=False,
            timeout=timeout,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    if cp.returncode != 0:
        return None
    try:
        pages = json.loads(cp.stdout or "[]")
    except (TypeError, ValueError):
        return None
    if not isinstance(pages, list):
        return None
    for page in pages:
        if not isinstance(page, list):
            return None
        for comment in page:
            if not isinstance(comment, dict):
                continue
            comment_body = comment.get("body")
            if isinstance(comment_body, str) and marker in comment_body:
                return True
    return False


def issue_labels_rest(
    issue_num: int,
    repo: str,
    *,
    timeout: float = 20.0,
) -> Optional[set[str]]:
    """Read an issue's labels from the REST core quota; ``None`` on failure."""
    try:
        cp = gh(
            "api", f"repos/{repo}/issues/{issue_num}",
            check=False, timeout=timeout,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    if cp.returncode != 0:
        return None
    try:
        data = json.loads(cp.stdout or "{}")
        if not isinstance(data, dict) or not isinstance(data.get("labels"), list):
            return None
        return {
            str(label["name"])
            for label in data["labels"]
            if isinstance(label, dict) and label.get("name") is not None
        }
    except (TypeError, ValueError, KeyError):
        return None


def converge_issue_labels_rest(
    issue_num: int,
    repo: str,
    *,
    required: Sequence[str] = (),
    forbidden: Sequence[str] = (),
    attempts: int = 2,
    timeout: float = 8.0,
) -> bool:
    """Converge additive/removal label constraints and confirm by REST readback.

    The additive and single-label removal endpoints preserve unrelated labels,
    unlike replacing the issue's complete label array after a racy read.
    """
    required_set = {str(label) for label in required if str(label)}
    forbidden_set = {str(label) for label in forbidden if str(label)}
    for _ in range(max(1, attempts)):
        try:
            for label in sorted(forbidden_set):
                encoded = quote(label, safe="")
                gh(
                    "api", "--method", "DELETE",
                    f"repos/{repo}/issues/{issue_num}/labels/{encoded}",
                    check=False, timeout=timeout,
                )
            if required_set:
                gh(
                    "api", "--method", "POST",
                    f"repos/{repo}/issues/{issue_num}/labels", "--input", "-",
                    check=False,
                    timeout=timeout,
                    input_text=json.dumps({"labels": sorted(required_set)}),
                )
        except (OSError, subprocess.TimeoutExpired):
            continue
        labels = issue_labels_rest(issue_num, repo, timeout=timeout)
        if labels is not None and required_set <= labels and not (
            forbidden_set & labels
        ):
            return True
    return False


def issue_view_json(
    issue_num: int,
    repo: str,
    fields: str,
    *,
    check: bool = True,
    timeout: Optional[float] = None,
) -> Optional[dict]:
    """``gh issue view ... --json fields`` → parsed dict (or None on failure)."""
    args = (
        "issue", "view", str(issue_num),
        "--repo", repo,
        "--json", fields,
    )
    if timeout is None:
        cp = gh(*args, check=check)
    else:
        cp = gh(*args, check=check, timeout=timeout)
    if cp.returncode != 0:
        return None
    return json.loads(cp.stdout)


def issue_labels(issue_num: int, repo: str) -> Optional[set[str]]:
    labels = issue_labels_rest(issue_num, repo)
    if labels is not None:
        return labels
    data = issue_view_json(issue_num, repo, "labels", check=False)
    if data is None:
        return None
    return {l["name"] for l in data.get("labels", [])}


def issue_title(issue_num: int, repo: str) -> str:
    data = issue_view_json(issue_num, repo, "title", check=False)
    return (data or {}).get("title", "(?)")


def issue_body(
    issue_num: int, repo: str, *, timeout: Optional[float] = None
) -> Optional[str]:
    """Issue body, optionally bounding the underlying ``gh`` call."""
    if timeout is None:
        data = issue_view_json(issue_num, repo, "body", check=False)
    else:
        data = issue_view_json(
            issue_num, repo, "body", check=False, timeout=timeout
        )
    return (data or {}).get("body")


# --- bulk fetches -------------------------------------------------------------


_REST_ISSUE_TIMEOUT_SECONDS = 10.0
_REST_ISSUE_MAX_WORKERS = 8


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
            timeout=20,
        )
    except (OSError, subprocess.TimeoutExpired):
        # gh binary missing/unrunnable, or the one-shot request hung.
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


def _parse_rest_issue(node: object, expected_num: int) -> Optional[dict]:
    """Normalize one REST ``/issues/{number}`` response to the GraphQL shape.

    GitHub's REST issues endpoint also returns pull requests.  The
    ``pull_request`` marker is therefore an explicit exclusion, not merely an
    ignored field: a PR number must never become a review issue card.
    """
    if not isinstance(node, dict) or "pull_request" in node:
        return None

    number = node.get("number")
    if number is not None:
        try:
            if int(number) != expected_num:
                return None
        except (TypeError, ValueError):
            return None

    labels: set[str] = set()
    raw_labels = node.get("labels") or []
    if isinstance(raw_labels, list):
        for label in raw_labels:
            raw_name = label.get("name") if isinstance(label, dict) else label
            if isinstance(raw_name, str) and raw_name:
                labels.add(raw_name)

    title = node.get("title")
    state = node.get("state")
    body = node.get("body")
    return {
        "title": title if isinstance(title, str) and title else "(?)",
        # GraphQL emits OPEN/CLOSED; REST emits open/closed.
        "state": state.upper() if isinstance(state, str) else "",
        "body": body if isinstance(body, str) else "",
        "labels": labels,
    }


def fetch_issues_rest_bulk(
    issue_nums: Sequence[int],
    repo: str,
) -> dict[int, dict]:
    """Fetch issue metadata from REST, concurrently and fail-soft.

    This is the degraded-path companion to :func:`fetch_issues_bulk`: callers
    should first make the single GraphQL request, then pass only unresolved
    numbers here.  Each REST call has a hard timeout and the worker pool is
    capped, so a GraphQL outage cannot turn a deck request into an unbounded
    serial N+1 wait.  There is deliberately no cache: results are live reads
    and cannot outlive the request that consumed them.

    Returns only successfully parsed issues. Missing/inaccessible issues,
    pull requests, command failures, timeouts, and malformed JSON are omitted.
    """
    nums = list(dict.fromkeys(int(n) for n in issue_nums))
    if not nums or "/" not in repo:
        return {}
    owner, name = repo.split("/", 1)
    if not owner or not name:
        return {}

    def fetch_one(number: int) -> Optional[dict]:
        try:
            cp = gh(
                "api", "--method", "GET",
                f"repos/{owner}/{name}/issues/{number}",
                check=False,
                timeout=_REST_ISSUE_TIMEOUT_SECONDS,
            )
        except (OSError, subprocess.TimeoutExpired):
            return None
        if cp.returncode != 0:
            return None
        try:
            node = json.loads(cp.stdout)
        except (json.JSONDecodeError, TypeError):
            return None
        return _parse_rest_issue(node, number)

    out: dict[int, dict] = {}
    workers = min(_REST_ISSUE_MAX_WORKERS, len(nums))
    # ContextVars do not automatically cross thread boundaries.  Copy one
    # context per worker invocation so a shared deck's read uses the requesting
    # collaborator's GH_TOKEN rather than silently falling back to host auth.
    with ThreadPoolExecutor(max_workers=workers) as executor:
        futures = {
            executor.submit(contextvars.copy_context().run, fetch_one, number): number
            for number in nums
        }
        for future in as_completed(futures):
            number = futures[future]
            try:
                metadata = future.result()
            except Exception:  # noqa: BLE001 - degraded read must fail soft
                continue
            if metadata is not None:
                out[number] = metadata
    return out


def list_rejected_issues(cfg) -> "Optional[list[int]]":
    """Open issues carrying the rejected review label — the GitHub-side
    rejection-queue membership (the cheap Step-1 of a GitHub-discovered
    conductor queue). One ``gh`` call; ``None`` on failure (including a missing
    ``gh`` binary) so the caller can fall back to the local queue."""
    try:
        cp = gh(
            "issue", "list", "--repo", cfg.github_repo,
            "--label", cfg.labels.rejected, "--state", "open",
            "--json", "number", "--limit", "300", check=False,
            timeout=20,
        )
    except (OSError, subprocess.TimeoutExpired):  # gh missing/unrunnable or hung
        return None
    if cp.returncode != 0:
        return None
    try:
        data = json.loads(cp.stdout or "[]")
        return [int(it["number"]) for it in data if "number" in it]
    except (ValueError, TypeError, KeyError):
        return None


def fetch_reject_note(issue_num: int, repo: str) -> "tuple[Optional[str], Optional[str]]":
    """Recover ``(note, reject_ts)`` from an issue's comments: the verbatim note
    embedded by :func:`marathon.review.reject_note.embed_reject_note` in the
    LATEST marked comment, and the ``createdAt`` of the latest reject comment
    (the GitHub proxy for ``verdict_ts``). ``(None, ts)`` when a reject comment
    exists but carries no embedded note; ``(None, None)`` when there is none.

    ``reject_ts`` is the NEWEST reject signal (so a later un-marked re-reject
    still bumps verdict_ts). Comments are sorted by createdAt rather than trusting
    gh's order, and a reject comment is recognised by the embed marker or the
    exact ``❌ REJECTED`` lead cmd_reject emits — not a bare "REJECTED" anywhere."""
    from marathon.review.reject_note import (
        REJECT_NOTE_MARKER_OPEN, extract_reject_note,
    )

    try:
        data = issue_view_json(issue_num, repo, "comments", check=False)
    except OSError:  # gh binary missing/unrunnable
        return (None, None)
    if not data:
        return (None, None)
    note: Optional[str] = None
    note_ts: Optional[str] = None
    latest_reject_ts: Optional[str] = None
    # gh createdAt are uniform UTC 'Z' ISO 8601 → lexical sort = chronological.
    comments = sorted(
        data.get("comments") or [], key=lambda c: c.get("createdAt") or "")
    for c in comments:
        body = c.get("body") or ""
        created = c.get("createdAt")
        recovered = extract_reject_note(body)
        if recovered is not None:               # latest marked comment wins
            note, note_ts = recovered, created
        if REJECT_NOTE_MARKER_OPEN in body or "❌ REJECTED" in body:
            latest_reject_ts = created
    # Prefer the newest reject signal so a later note-less re-reject bumps the ts.
    ts = note_ts
    if latest_reject_ts and (ts is None or latest_reject_ts > ts):
        ts = latest_reject_ts
    return (note, ts)
