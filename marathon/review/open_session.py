"""Open an interactive *coreviewer* Claude Code session in the user's
VS Code IDE for a specific review sub-issue.

The Claude Code VS Code extension registers the URI handler
``vscode://anthropic.claude-code/open`` with two query parameters:

* ``prompt`` — URL-encoded text pre-filled in the chat box (NOT
  auto-submitted; the user reviews and presses Enter). Hard cap of
  5,000 characters per the extension's parser.
* ``session`` — optional ID of a previously-recorded session under
  ``~/.claude/projects/<slug>/`` to resume.

This module builds the ``prompt`` URI for a given review sub-issue and
shells out to the platform's URL opener. The prompt instantiates **the
coreviewer**: a role distinct from the *Hermes* live-steerer used
during ``marathon refine``. The coreviewer is a thinking partner — it
reads the issue, identifies the relevant code, forms an opinion on
correctness / style / completeness, and recommends a verdict. The
human stays in the loop: nothing is applied without the human signal.
When the human green-lights a verdict, the coreviewer applies it by
invoking ``marathon review verify N`` or ``marathon review reject N
--notes file``, following the project's GitHub-thread hygiene
conventions.

Output expectation: concise — verdict-forward, structured bullets,
citations to specific declarations and lines. Human reviewers move
fast and read slowly.

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
from marathon.review.github import issue_labels, issue_title
from marathon.review.state import load_state, pending_rejections


# Per the VS Code extension's parser. The URI launcher silently
# truncates beyond this; we cap with a budget split so the role spec
# + the load-bearing "wait for human" directive don't get clipped.
PROMPT_CHAR_BUDGET = 5_000

# Per-section soft budgets. Sum is ≤ PROMPT_CHAR_BUDGET with margin.
_BUDGET_HEADER = 700
_BUDGET_QUEUE = 500
_BUDGET_FILES = 300


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
    repo = cfg.github_repo

    issue_url = f"https://github.com/{repo}/issues/{issue_num}"

    role = (
        f"# You are the coreviewer — marathon review session, [#{issue_num}]({issue_url})\n\n"
        f"**Title**: {title}  •  **Repo**: {repo}\n"
        f"**Status**: {status_line}"
        + (f"  •  **Chapter**: {chapter}" if chapter is not None else "")
        + "\n\n"
        "You are *the coreviewer* — a thinking partner for the human "
        "running marathon's per-sub-issue review pass. You read, "
        "inspect, opine, and recommend. You **never** apply a verdict on "
        "your own. Apply happens only after the human says go.\n"
    )

    workflow = (
        "## Workflow\n\n"
        f"1. **Read the issue.** `gh issue view {issue_num} --repo {repo} "
        "--comments` — body, labels, recent comments. Include the issue "
        f"URL [#{issue_num}]({issue_url}) verbatim in your first "
        "message so the human can click straight through.\n"
        "2. **Inspect the cited code; flag body drift.** Match the "
        "issue's claimed signatures / line ranges / code snippets "
        "against the file *right now*. Focus: statement correctness, "
        "hypothesis tightness, naming, downstream consumers. Also "
        "note any stale snippets / shifted line refs, or an *Informal "
        "Statement* still marked `⚠️ LLM-rendered … verification "
        "pending`. Drift is common after a reject — `marathon review "
        "reject N` launches a refine daemon that may run multiple "
        "Aristotle iterations against the code.\n"
        "3. **Recommend a verdict.** Concise verdict-forward bullets "
        "with line citations. If the *Informal Statement* is "
        "unverified, ask the human to approve or correct it first — "
        "math-correctness needs an accurate baseline. Then "
        "*stop and wait*.\n"
        "4. **Apply only on explicit human go-ahead.** When (and only "
        "when) the human says \"verify\", \"reject with these notes\", "
        "or similar, run the corresponding `marathon review` CLI. If "
        "they push back, keep discussing — don't pre-empt.\n"
    )

    rubric = (
        "## Decision rubric (three-axis critique)\n\n"
        "1. **Math correctness vs the textbook reference.** Lean "
        "statement matches what the source asserts? Caveats / "
        "strengthenings / deviations? (Use the issue's *Informal "
        "Statement* — ideally human-approved per workflow step 3 — "
        "since the textbook source itself may not be on disk or may be "
        "copyrighted.)\n"
        "2. **Lean style / readability.** Idiomatic, variable-block "
        "discipline, `@[simp]`/`@[ext]`/`@[fun_prop]` hooks, no `▸` "
        "ghosts, no `(M := M)` spam, no iteration-changelog docstrings.\n"
        "3. **Mathlib PR-readiness.** Naming convention, level of "
        "generality, no hypothesis bloat reinventing existing predicates.\n\n"
        "**Reject for**: wrong statement, dishonest return type, missing "
        "scaffolding, cross-chapter duplication, naming inconsistency, "
        "hypothesis-too-weak. **Not** for `sorry`s alone — the 🟡 marker "
        "already advertises skeleton-with-sorries.\n"
    )

    output_format = (
        "## Output format (concise — humans move fast, read slowly)\n\n"
        f"* **TL;DR** (one line): proposed verdict + single load-bearing "
        f"reason. Begin with the issue link [#{issue_num}]({issue_url}) "
        f"so the human can jump straight to GitHub.\n"
        "* **Findings** (≤5 bullets): each cites a specific declaration "
        "with a clickable VS Code link in the form "
        "`[file.lean:NN-MM](path/to/file.lean#LNN-LMM)` — VS Code "
        "renders these as direct jumps to the line range. Always use "
        "markdown link syntax, never bare backticks for file refs. "
        "No stylistic nitpicks unless load-bearing.\n"
        "* **Recommended action** (one line): exact marathon CLI command "
        "you'd run, with proposed `--comment` text.\n"
        "* **Then stop.** Don't run the CLI yet. Don't iterate "
        "speculatively. **Stopping is the load-bearing step.**\n"
    )

    apply_section = (
        "## On human go-ahead\n\n"
        f"Verify: `marathon review verify {issue_num} --comment '…'`. "
        "**Never pass `--close`** unless the human explicitly tells you "
        "to close the issue. Sorry-free ≠ unimprovable (naming, "
        "generality, Mathlib-PR-readiness, downstream ergonomics); the "
        "issue stays OPEN as a tracking handle until the human says "
        "otherwise.\n"
        f"Reject: write `/tmp/issue{issue_num}-reject-notes.md` first, "
        f"then `marathon review reject {issue_num} --notes "
        f"/tmp/issue{issue_num}-reject-notes.md --comment '…'`.\n\n"
        "**Body sync**: if step 2 surfaced drift (stale snippets, "
        "wrong line refs, or an Informal Statement the human just "
        "approved/rewrote), update the issue body **as part of the "
        "same verdict step** — `gh issue view N --json body --jq .body "
        "> /tmp/body.md`, edit, `gh issue edit N --body-file "
        "/tmp/body.md`. Body must reflect the code at the commit "
        "you're verifying/rejecting. Expect another body sync after a "
        "reject-then-refine cycle's daemon iteration converges.\n\n"
        "**Thread hygiene**: one one-line verdict comment per state "
        "transition (✅/❌/🟢/🟠); one substantive iteration comment "
        "per re-verify after a fix. No multi-paragraph verdicts, no "
        "duplicating the body, no @-tags.\n"
    )

    # The full issue body is fetched fresh by the coreviewer via
    # `gh issue view` per workflow step 1; we don't pre-inline an
    # excerpt here because it duplicates the header (title + status)
    # and the Lean snippet portion almost always gets clipped.
    queue_section_truncated = False
    pending = pending_rejections(cfg, chapter)
    if pending and chapter is not None:
        queue_lines = [
            f"## Chapter {chapter} pending-rejection queue ({len(pending)} open)",
            "",
        ]
        for num, st in pending:
            marker = " ← current focus" if num == issue_num else ""
            first_line = (st.notes or "").splitlines()[0] if st.notes else "(no notes)"
            queue_lines.append(
                f"- **#{num}**{marker} — rejected {st.verdict_ts}: {first_line[:120]}"
            )
        queue_text = "\n".join(queue_lines) + "\n"
        queue_section, queue_section_truncated = _truncate(queue_text, _BUDGET_QUEUE)
    else:
        queue_section = ""  # silent when empty — saves prompt budget

    files_listed: list[str] = []
    if include_file_mentions and chapter is not None:
        files_listed = _file_mentions_for_chapter(cfg, chapter)
        files_block = (
            "## Context files (`@`-mention to load — accept each chip)\n\n"
            + "\n".join(f"- {m}" for m in files_listed)
            + "\n"
        )
    else:
        files_block = ""

    tail = "\n---\n\nGo. Read, inspect, opine, recommend — then **stop**.\n"

    sections = [
        _truncate(role, _BUDGET_HEADER)[0],
        workflow,
        rubric,
        output_format,
        apply_section,
        queue_section,
        files_block,
        tail,
    ]
    prompt = "\n".join(s for s in sections if s)

    truncated = queue_section_truncated
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
