"""Open an interactive Claude Code session in the user's VS Code IDE
for a specific review sub-issue.

The Claude Code VS Code extension registers the URI handler
``vscode://anthropic.claude-code/open`` with two query parameters:

* ``prompt`` — URL-encoded text pre-filled in the chat box (NOT
  auto-submitted; the user reviews and presses Enter). Hard cap of
  5,000 characters per the extension's parser.
* ``session`` — optional ID of a previously-recorded session under
  ``~/.claude/projects/<slug>/`` to resume.

This module builds the ``prompt`` URI for a given review sub-issue and
shells out to the platform's URL opener. The prompt is structured so
the user lands in a fresh chat with:

* The issue's title and current verdict status
* The chapter's pending-rejections queue (so they see what's outstanding
  *project-wide* before discussing the focal issue)
* A ``@``-mention list for the target Lean folder + key adjacent
  context files so the user only needs to accept the file-attach
  prompts after pressing Enter
* A role directive telling Claude what kind of conversation this is
  (review-pass, not refinement)

Programmatic file pre-attachment isn't supported by the extension's
URI handler at the time of writing; ``@``-mentions are the closest
ergonomic substitute and are accepted with a single click each.

References:
* `vs-code.md#launch-a-vs-code-tab-from-other-tools <https://code.claude.com/docs/en/vs-code.md>`_
* `deep-links.md <https://code.claude.com/docs/en/deep-links.md>`_
"""

from __future__ import annotations

import shutil
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Optional
from urllib.parse import quote

from marathon.review.config import ReviewConfig
from marathon.review.github import gh, issue_labels, issue_title
from marathon.review.state import load_state, pending_rejections


# Per the VS Code extension's parser. The URI launcher silently
# truncates beyond this; we cap with a budget split so the role
# directive + file mentions don't get clipped.
PROMPT_CHAR_BUDGET = 5_000

# Per-section soft budgets. Sum is ≤ PROMPT_CHAR_BUDGET with margin.
_BUDGET_HEADER = 800
_BUDGET_BODY_EXCERPT = 1_500
_BUDGET_QUEUE = 1_400
_BUDGET_FILES = 600
_BUDGET_TAIL = 400


@dataclass
class OpenSessionResult:
    uri: str
    prompt_chars: int
    files_listed: list[str]
    truncated: bool


def _platform_url_opener() -> Optional[list[str]]:
    """Return the platform's URL-opener argv prefix, or ``None`` if no
    suitable opener is on PATH. Caller appends the URI."""
    if sys.platform == "darwin":
        return ["open"]
    if sys.platform.startswith("linux"):
        # xdg-open is the standard freedesktop launcher; fall back to
        # gnome-open / kde-open* if absent.
        for cand in ("xdg-open", "gnome-open", "kde-open5", "kde-open"):
            if shutil.which(cand):
                return [cand]
        return None
    if sys.platform.startswith("win"):
        # `start` is a cmd builtin; needs an empty title argument so the
        # URI isn't parsed as the window title.
        return ["cmd", "/c", "start", ""]
    return None


def _truncate(text: str, budget: int, marker: str = "…[trimmed]…") -> tuple[str, bool]:
    """Truncate ``text`` to ``budget`` characters, keeping a marker so
    the consumer knows trimming happened. Returns (trimmed, did_trim)."""
    if len(text) <= budget:
        return text, False
    head_budget = max(budget - len(marker), 0)
    return text[:head_budget] + marker, True


def _fetch_issue_body(num: int, repo: str) -> str:
    """Best-effort fetch of the issue body. Returns '' on failure."""
    cp = gh(
        "issue", "view", str(num),
        "--repo", repo,
        "--json", "body",
        "--jq", ".body",
        check=False,
    )
    if cp.returncode != 0:
        return ""
    return cp.stdout.strip()


def _current_status(cfg: ReviewConfig, num: int) -> str:
    """Render a one-line status summary combining the GitHub label and
    the state.json record, since they can drift."""
    labels = issue_labels(num, cfg.github_repo) or []
    label_status = "(no review:* label)"
    if cfg.labels.verified in labels:
        label_status = f"label: `{cfg.labels.verified}`"
    elif cfg.labels.rejected in labels:
        label_status = f"label: `{cfg.labels.rejected}`"
    elif cfg.labels.inflight in labels:
        label_status = f"label: `{cfg.labels.inflight}`"

    state = load_state(cfg).get(num)
    state_status = "(no state.json record)"
    if state is not None:
        state_status = f"state.json: `{state.status}` @ {state.verdict_ts}"

    return f"{label_status}  •  {state_status}"


def _file_mentions_for_chapter(cfg: ReviewConfig, chapter: int) -> list[str]:
    """Return a list of ``@``-mention paths the user can paste/accept in
    chat to load the chapter's Lean files and the project-wide review
    context.

    Paths are repo-root-relative; the Claude Code VS Code extension
    resolves ``@`` paths against the open workspace.
    """
    mentions: list[str] = []
    target = cfg.target_path(chapter)
    try:
        rel_target = target.relative_to(cfg.repo_dir)
    except ValueError:
        rel_target = target
    mentions.append(f"@{rel_target}/")  # whole folder as a single chip
    # Also list the standard review context files so the user can attach
    # them in one go.
    for relpath in (".marathon/referee.md", ".marathon/review/state.json"):
        if (cfg.repo_dir / relpath).is_file():
            mentions.append(f"@{relpath}")
    return mentions


def build_open_prompt(
    cfg: ReviewConfig,
    issue_num: int,
    *,
    include_file_mentions: bool = True,
) -> tuple[str, list[str], bool]:
    """Build the prompt body that goes into the
    ``vscode://anthropic.claude-code/open?prompt=…`` query.

    Returns ``(prompt_text, files_listed, truncated)`` where
    ``truncated`` indicates whether any section had to be trimmed to
    fit the 5,000-character ceiling. ``files_listed`` are the
    ``@``-mention paths included (for logging by the caller).
    """
    title = issue_title(issue_num, cfg.github_repo) or "(title unavailable)"
    status_line = _current_status(cfg, issue_num)
    chapter = cfg.chapter_of_issue(issue_num)

    header = (
        f"# Marathon review session — issue #{issue_num}\n\n"
        f"**Title**: {title}\n"
        f"**Status**: {status_line}\n"
        + (f"**Chapter**: {chapter}\n" if chapter is not None else "")
        + "\n"
        "You are joining a per-issue review conversation in the marathon "
        "workflow. Your role this turn is the **reviewer**, not the "
        "refiner: read the issue, inspect the cited Lean code (see "
        "`@`-mentions below), and discuss findings with me before I run "
        "`marathon review verify N` or `marathon review reject N --notes …`. "
        "Stay close to (1) math correctness vs the textbook reference, "
        "(2) Lean-style / readability, (3) Mathlib PR-readiness — the "
        "project's standing critique rubric. Don't draft a refinement "
        "prompt; that's a different tool.\n"
    )

    body_excerpt_raw = _fetch_issue_body(issue_num, cfg.github_repo)
    body_section_truncated = False
    if body_excerpt_raw:
        body_excerpt, body_section_truncated = _truncate(body_excerpt_raw, _BUDGET_BODY_EXCERPT)
        body_section = (
            f"## Issue #{issue_num} body (current GitHub state)\n\n"
            f"{body_excerpt}\n"
        )
    else:
        body_section = (
            f"## Issue #{issue_num} body\n\n"
            f"(could not fetch from GitHub; run `gh issue view {issue_num}` "
            "after the session opens)\n"
        )

    queue_section_truncated = False
    pending = pending_rejections(cfg, chapter)
    if pending and chapter is not None:
        queue_lines = [
            f"## Chapter {chapter} pending-rejection queue ({len(pending)} open)",
            "",
            f"This issue (#{issue_num}) is one of several still-open rejections "
            "in this chapter; awareness of the others may inform the discussion.",
            "",
        ]
        for num, st in pending:
            marker = " ← current focus" if num == issue_num else ""
            first_line = (st.notes or "").splitlines()[0] if st.notes else "(no notes)"
            queue_lines.append(
                f"- **#{num}**{marker} — rejected {st.verdict_ts}\n"
                f"  {first_line}"
            )
        queue_text = "\n".join(queue_lines) + "\n"
        queue_section, queue_section_truncated = _truncate(queue_text, _BUDGET_QUEUE)
    else:
        queue_section = (
            f"## Chapter {chapter} pending-rejection queue\n\n"
            "(empty — no open rejections in this chapter)\n"
        )

    files_listed: list[str] = []
    if include_file_mentions and chapter is not None:
        files_listed = _file_mentions_for_chapter(cfg, chapter)
        files_block = (
            "## Files to attach (use `@`-mentions in chat — accept each chip)\n\n"
            + "\n".join(f"- {m}" for m in files_listed)
            + "\n"
        )
    else:
        files_block = ""

    tail = (
        "\n---\n\n"
        "Confirm you've read this brief, then ask whatever you need to "
        "form a verdict. End the turn with one of: \"verify\", \"reject "
        "(with these notes …)\", or \"need more info\". I'll run the "
        "marathon CLI on my side once you signal.\n"
    )

    sections = [
        _truncate(header, _BUDGET_HEADER)[0],
        body_section,
        queue_section,
        files_block,
        _truncate(tail, _BUDGET_TAIL)[0],
    ]
    prompt = "\n".join(s for s in sections if s)

    truncated = body_section_truncated or queue_section_truncated
    if len(prompt) > PROMPT_CHAR_BUDGET:
        prompt, hard_trimmed = _truncate(prompt, PROMPT_CHAR_BUDGET)
        truncated = truncated or hard_trimmed

    return prompt, files_listed, truncated


def build_open_uri(prompt: str) -> str:
    """URL-encode ``prompt`` and assemble the full ``vscode://...`` URI."""
    return f"vscode://anthropic.claude-code/open?prompt={quote(prompt, safe='')}"


def open_session_for_issue(
    cfg: ReviewConfig,
    issue_num: int,
    *,
    include_file_mentions: bool = True,
    dry_run: bool = False,
) -> OpenSessionResult:
    """End-to-end entry: build the URI and shell out to the platform's
    URL opener. If ``dry_run`` is True, return the URI without launching.

    Raises ``RuntimeError`` if no platform URL opener is available; the
    caller (``cmd_open``) catches this and prints a fallback instruction.
    """
    prompt, files_listed, truncated = build_open_prompt(
        cfg, issue_num, include_file_mentions=include_file_mentions,
    )
    uri = build_open_uri(prompt)
    result = OpenSessionResult(
        uri=uri,
        prompt_chars=len(prompt),
        files_listed=files_listed,
        truncated=truncated,
    )
    if dry_run:
        return result

    opener = _platform_url_opener()
    if opener is None:
        raise RuntimeError(
            f"no URL opener detected for platform {sys.platform!r}; "
            "copy the URI manually and paste into your browser or "
            "VS Code's URI handler"
        )
    subprocess.run([*opener, uri], check=False)
    return result
