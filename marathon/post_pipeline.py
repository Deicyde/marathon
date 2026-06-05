"""Post-extraction pipeline: ``lake build`` → ``git commit`` → Claude rate.

These three steps run after a chapter (skeleton) or iteration (refine) has
been successfully extracted. Each is independently toggleable via CLI flags
on the parent subcommand. Each is a no-op when its flag is off; failures in
one step are recorded but don't fail subsequent steps.

- ``lake build`` runs in the user's repo with a configurable timeout
  (default 600 s = 10 min). Build failure does not abort the pipeline —
  Marathon trusts the caller's "the build isn't load-bearing" framing.
- ``git commit`` stages only the chapter's output folder and commits with
  an auto-generated message. If the index is busy (e.g. another agent is
  also committing), the step is skipped with a clear note.
- The Claude rating spawns ``claude -p`` (the same Max-billed subprocess
  pathway as ``refine``) and asks for a 1–5 score across five dimensions.
  Results are appended to a ``marathon-ratings.jsonl`` file in the workdir.
"""

import json
import os
import re
import shutil
import subprocess
import sys
from dataclasses import asdict, dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Optional


@dataclass
class PipelineConfig:
    auto_build: bool = False
    auto_commit: bool = False
    auto_push: bool = False
    auto_rate: bool = False
    build_timeout: int = 600
    ratings_path: Optional[Path] = None
    # When True, the auto-commit trailer also credits Claude (in addition to
    # Aristotle). Set by refine, not by skeleton — refine drafts each Aristotle
    # prompt via Claude, skeleton uses a static template.
    claude_in_loop: bool = False
    # Optional path to a referee notes file (e.g. referee.md). When set and
    # auto_rate is on, the rater also receives these notes as
    # project-specific priorities to weight scoring against.
    referee_path: Optional[Path] = None
    # When True (and auto_commit also True), after the auto-commit lands,
    # run a post-iteration audit that diffs HEAD~1..HEAD against the set
    # of verified declarations extracted from currently-verified
    # sub-issue bodies. Any overlap is flagged. Soft warning only — does
    # not auto-revert or auto-reject. Logs to
    # ``<workdir>/marathon-audit-violations.jsonl``. Requires the consumer
    # repo to use the `marathon review` workflow (review config + GitHub
    # sub-issues); no-ops gracefully otherwise.
    audit_verified: bool = False
    # Workdir path for the iteration; used to emit the audit JSONL log.
    # Set by refine; skeleton doesn't currently audit.
    audit_workdir: Optional[Path] = None
    # When True (default), refresh ``formalization.yaml`` (mathlib-
    # initiative v0.2 schema) at the repo root with auto-derived
    # fields (sorry_count, models, etc.) before each auto-commit.
    # No-op for repos that haven't created the file (the auto-updater
    # itself is opt-in; the file is created only by
    # ``marathon formalization init`` or manually).
    update_formalization: bool = True
    # Model identifiers stamped into ``automation.models``. Set by
    # refine to ``["claude-opus-4-7", "Aristotle"]`` (Claude + the
    # Aristotle worker); set by skeleton to ``["Aristotle"]``.
    formalization_models: Optional[list[str]] = None
    # Framework name stamped into ``automation.framework``. Defaults
    # to ``"Marathon"`` at call sites; override if the consumer pipes
    # marathon through a different orchestrator.
    formalization_framework: Optional[str] = "Marathon"
    # When True, the iteration runs on a dedicated marathon-owned
    # branch (``marathon/refine-c<N>-i<issue>``) instead of whatever
    # branch is currently checked out, and a PR is opened against
    # ``main`` after the auto-commit. Force-pushes the branch on
    # subsequent iterations so the PR always reflects the latest
    # iteration's diff. Solves the failure mode where the daemon
    # accidentally commits iteration changes onto an unrelated
    # branch (e.g., a docs-WIP branch) because that branch was
    # checked out at run time.
    auto_pr: bool = False
    # Owner/name of the GitHub repo. Inferred from ``gh repo view``
    # if None. Required when ``auto_pr`` is True.
    auto_pr_repo: Optional[str] = None
    # Sub-issue number this iteration is addressing. Set by refine
    # from ``--review-rejection N``. Used to (1) name the branch
    # deterministically per-issue and (2) link the PR back to the
    # tracking sub-issue. When None, the PR mechanism falls back to
    # a timestamped branch + a generic title.
    auto_pr_review_issue: Optional[int] = None
    # PR base branch. ``main`` is the default; override per project.
    auto_pr_base: str = "main"

    def has_any(self) -> bool:
        return self.auto_build or self.auto_commit or self.auto_push or self.auto_rate


ARISTOTLE_COAUTHOR_TRAILER = (
    "Co-authored-by: Aristotle (Harmonic) <aristotle-harmonic@harmonic.fun>"
)
CLAUDE_COAUTHOR_TRAILER = "Co-authored-by: Claude <noreply@anthropic.com>"


def _build_commit_message(
    short_message: str,
    project_id: Optional[str],
    claude_in_loop: bool,
) -> str:
    """Build a commit message body with project URL and co-author trailers."""
    parts = [short_message]
    if project_id:
        parts.append(
            f"Project: aristotle.harmonic.fun/dashboard/requests/{project_id}"
        )
    trailer_lines = [ARISTOTLE_COAUTHOR_TRAILER]
    if claude_in_loop:
        trailer_lines.append(CLAUDE_COAUTHOR_TRAILER)
    parts.append("\n".join(trailer_lines))
    return "\n\n".join(parts)


@dataclass
class BuildResult:
    ok: Optional[bool] = None
    duration_seconds: Optional[float] = None
    log_tail: Optional[str] = None
    timed_out: bool = False
    skipped_reason: Optional[str] = None


@dataclass
class CommitResult:
    sha: Optional[str] = None
    skipped_reason: Optional[str] = None


@dataclass
class RatingResult:
    quality: Optional[int] = None
    math_correctness: Optional[int] = None
    generality: Optional[int] = None
    api_coverage: Optional[int] = None
    concision: Optional[int] = None
    modern_lean4: Optional[int] = None
    structural_focus: Optional[int] = None
    notes: Optional[str] = None
    parse_error: Optional[str] = None


PROMPTLOG_FILENAME = "PromptLog.md"


def append_promptlog_url(repo_dir: Path, project_id: str) -> bool:
    """If ``PromptLog.md`` exists at the root of ``repo_dir``, append a
    blank line plus a ``<timestamp>  <project_id>`` entry. Skips silently
    if the file doesn't exist or ``project_id`` is empty.

    Returns True if the file was appended to, False if skipped.
    """
    if not project_id:
        return False
    log_path = repo_dir / PROMPTLOG_FILENAME
    if not log_path.is_file():
        return False
    # Local import to avoid a top-level circular dep with marathon.state.
    from marathon.state import now_iso
    line = f"{now_iso()}  {project_id}"
    existing = log_path.read_text()
    sep = "" if existing.endswith("\n\n") else ("\n" if existing.endswith("\n") else "\n\n")
    with log_path.open("a") as f:
        f.write(f"{sep}{line}\n")
    return True


_SORRY_WARNING_RE = re.compile(r"^warning: .*: declaration uses `sorry`\s*$")
# Lake error-summary lines that don't add information once the per-file
# `error:` lines are surfaced.
_GENERIC_BUILD_ERROR_LINES = {
    "error: build failed",
    "Some required targets logged failures:",
}


def _summarize_build_log(stdout: str, stderr: str) -> str:
    """Build a concise log_tail that surfaces real errors instead of being
    drowned in `declaration uses sorry` warnings.

    Strategy:
      1. Walk every line of stdout+stderr.
      2. Pull out lines starting with `error:` or `error.*:`, plus the
         immediately-following continuation lines (Lean follow-ups like
         "Note: ..." or stacktrace context — non-blank, non-warning lines).
      3. Drop the per-declaration `declaration uses 'sorry'` warnings (they
         repeat hundreds of times in skeleton mode and crowd out the cause).
      4. Append a short tail of remaining lines for context.
    """
    combined = []
    if stdout:
        combined.append(stdout)
    if stderr.strip():
        combined.append("--- stderr ---")
        combined.append(stderr)
    lines = "\n".join(combined).splitlines()

    errors: list[str] = []
    other: list[str] = []
    in_error_block = False
    for raw in lines:
        line = raw.rstrip()
        if not line:
            in_error_block = False
            continue
        # Lake build summary lines: keep but don't treat as the marquee error.
        is_error_line = (
            line.startswith("error:")
            or " error: " in line
            or "Lean exited with code" in line
        )
        if is_error_line and line not in _GENERIC_BUILD_ERROR_LINES:
            errors.append(line)
            in_error_block = True
            continue
        if in_error_block:
            # Capture follow-up context (Note:, stacktrace) until a blank or warning
            if _SORRY_WARNING_RE.match(line) or line.startswith("warning:"):
                in_error_block = False
            else:
                errors.append("  " + line)
                continue
        if _SORRY_WARNING_RE.match(line):
            continue
        other.append(line)

    parts: list[str] = []
    if errors:
        parts.append("ERRORS:\n" + "\n".join(errors))
    if other:
        tail = other[-30:]
        parts.append("TAIL (warnings + other, sorry-noise stripped):\n" + "\n".join(tail))
    summary = "\n\n".join(parts).strip()
    if not summary:
        return ""
    if len(summary) > 4_000:
        summary = summary[:4_000] + "\n... (truncated)"
    return summary


def run_lake_build(repo_dir: Path, timeout: int) -> BuildResult:
    if not shutil.which("lake"):
        return BuildResult(skipped_reason="lake CLI not on PATH")
    started = datetime.now()
    try:
        proc = subprocess.run(
            ["lake", "build"],
            cwd=str(repo_dir),
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )
    except subprocess.TimeoutExpired:
        elapsed = (datetime.now() - started).total_seconds()
        return BuildResult(
            ok=False,
            duration_seconds=elapsed,
            timed_out=True,
            log_tail=f"timed out after {timeout}s",
        )

    elapsed = (datetime.now() - started).total_seconds()
    log_tail = _summarize_build_log(proc.stdout or "", proc.stderr or "")
    return BuildResult(
        ok=proc.returncode == 0,
        duration_seconds=elapsed,
        log_tail=log_tail or None,
    )


def run_git_commit(
    repo_dir: Path,
    target_path: Path,
    message: str,
    project_id: Optional[str] = None,
    claude_in_loop: bool = False,
    extra_paths: Optional[list[str]] = None,
) -> CommitResult:
    """Stage ``target_path`` (and ``PromptLog.md`` if it exists and is
    dirty) and commit. ``extra_paths`` (repo-relative POSIX paths) are
    additionally staged — used for cross-chapter refactor iterations
    where Aristotle edits files in ``extra_writable_paths`` outside the
    primary target. The final commit message includes a project URL
    line (when ``project_id`` is set) and a Co-authored-by trailer block
    crediting Aristotle (and Claude, when ``claude_in_loop`` is True).
    Skips silently if the index is busy or there's nothing to commit."""
    try:
        rel = target_path.relative_to(repo_dir)
    except ValueError:
        return CommitResult(skipped_reason=f"{target_path} not under repo {repo_dir}")

    paths_to_stage = [str(rel)]
    promptlog = repo_dir / PROMPTLOG_FILENAME
    if promptlog.is_file():
        paths_to_stage.append(PROMPTLOG_FILENAME)
    # Cross-chapter refactor support: stage every file outside the
    # primary target that the extractor reported writing. Dedup since
    # any path under ``rel`` is already covered by the primary stage
    # and would otherwise produce a no-op git add (still safe, but
    # cleaner without it).
    if extra_paths:
        seen: set[str] = {str(rel)}
        for p in extra_paths:
            if p in seen:
                continue
            # Skip files already under the primary target — they're
            # staged via the directory entry above.
            if p == str(rel) or p.startswith(str(rel) + "/"):
                continue
            seen.add(p)
            paths_to_stage.append(p)
    # Stage formalization.yaml when present so its auto-update (run by
    # run_post_pipeline before this commit lands) is bundled into the
    # same commit as the iteration's .lean edits. No-op when the project
    # hasn't opted in.
    from marathon.formalization import FORMALIZATION_FILENAME
    formalization = repo_dir / FORMALIZATION_FILENAME
    if formalization.is_file():
        paths_to_stage.append(FORMALIZATION_FILENAME)

    add_proc = subprocess.run(
        ["git", "add", "--", *paths_to_stage],
        cwd=str(repo_dir),
        capture_output=True,
        text=True,
        check=False,
    )
    if add_proc.returncode != 0:
        return CommitResult(skipped_reason=f"git add failed: {add_proc.stderr.strip() or 'unknown'}")

    diff_proc = subprocess.run(
        ["git", "diff", "--cached", "--quiet"],
        cwd=str(repo_dir),
        check=False,
    )
    if diff_proc.returncode == 0:
        return CommitResult(skipped_reason="nothing to commit")

    full_message = _build_commit_message(message, project_id, claude_in_loop)
    commit_proc = subprocess.run(
        ["git", "commit", "-m", full_message],
        cwd=str(repo_dir),
        capture_output=True,
        text=True,
        check=False,
    )
    if commit_proc.returncode != 0:
        return CommitResult(
            skipped_reason=f"git commit failed: {(commit_proc.stderr or commit_proc.stdout).strip()}"
        )

    sha_proc = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=str(repo_dir),
        capture_output=True,
        text=True,
        check=False,
    )
    sha = sha_proc.stdout.strip()[:8] if sha_proc.returncode == 0 else None
    return CommitResult(sha=sha)


def run_git_push(repo_dir: Path) -> tuple[bool, str]:
    """Run ``git push`` from ``repo_dir``. Returns ``(ok, message)``."""
    proc = subprocess.run(
        ["git", "push"],
        cwd=str(repo_dir),
        capture_output=True,
        text=True,
        check=False,
    )
    if proc.returncode != 0:
        return False, ((proc.stderr or proc.stdout) or "unknown error").strip()
    out = ((proc.stderr or proc.stdout) or "").strip()
    short = out.splitlines()[-1] if out else "ok"
    return True, short


# ---------------------------------------------------------------------------
# Branch + PR management for --auto-pr
# ---------------------------------------------------------------------------
#
# When --auto-pr is enabled, each iteration:
# 1. Checks out a dedicated branch ``marathon/refine-c<N>-i<issue>``,
#    creating it off origin/<base> if it doesn't exist (or hard-resetting
#    to origin/<base> if it does, since each iteration replaces — the
#    persistent-branch-per-issue model means the branch always reflects
#    only the latest iteration's diff against the base).
# 2. Runs the iteration's normal pipeline (auto-build, auto-commit).
# 3. After the auto-commit lands, force-pushes the branch and opens or
#    updates the PR.
#
# The branch lifetime is tied to the issue's verdict: when
# `marathon review verify N` runs, it merges + deletes the branch. On
# re-rejection, the next iteration recreates the branch.


_CHAPTER_PATH_RE = re.compile(r"Chapter(\d+)")


def _extra_chapters_in_writes(
    primary_chapter_label: str, extra_paths: list[str]
) -> list[int]:
    """Return chapter numbers touched by ``extra_paths`` that are NOT
    the primary chapter, in ascending order. Used to compose
    ``+ChN+ChM`` suffixes for cross-chapter PR titles."""
    if not extra_paths:
        return []
    primary_match = _CHAPTER_PATH_RE.search(primary_chapter_label)
    primary_n = int(primary_match.group(1)) if primary_match else None
    found: set[int] = set()
    for p in extra_paths:
        m = _CHAPTER_PATH_RE.search(p)
        if m:
            n = int(m.group(1))
            if n != primary_n:
                found.add(n)
    return sorted(found)


def _branch_name_for_issue(chapter_label: str, issue_num: Optional[int]) -> str:
    """Return the dedicated marathon branch name for an iteration.

    Persistent per-issue when ``issue_num`` is set:
    ``marathon/refine-c<N>-i<issue>``. Falls back to a chapter-scoped
    name when ``issue_num`` is None (e.g., manual ``marathon refine``
    not driven by a sub-issue rejection)."""
    # ``chapter_label`` is e.g. "Chapter14" — extract the integer.
    chap_digits = "".join(ch for ch in chapter_label if ch.isdigit())
    chap = chap_digits or "?"
    if issue_num is not None:
        return f"marathon/refine-c{chap}-i{issue_num}"
    return f"marathon/refine-c{chap}"


def _gh(*args: str, cwd: Optional[Path] = None, check: bool = False) -> subprocess.CompletedProcess[str]:
    """Run ``gh ...`` with stdout/stderr captured. Centralised so the
    repo-inference + error formatting are consistent."""
    proc = subprocess.run(
        ["gh", *args],
        cwd=str(cwd) if cwd else None,
        capture_output=True,
        text=True,
        check=False,
    )
    if check and proc.returncode != 0:
        raise RuntimeError(
            f"gh {' '.join(args[:3])}... failed (exit {proc.returncode}): "
            f"{(proc.stderr or proc.stdout).strip()[:500]}"
        )
    return proc


def _infer_repo(repo_dir: Path) -> Optional[str]:
    """Run ``gh repo view --json nameWithOwner`` to infer ``owner/name``
    from the working directory. Returns None on failure."""
    proc = _gh("repo", "view", "--json", "nameWithOwner", "--jq", ".nameWithOwner", cwd=repo_dir)
    if proc.returncode != 0:
        return None
    return proc.stdout.strip() or None


def prepare_auto_pr_branch(
    repo_dir: Path,
    chapter_label: str,
    issue_num: Optional[int],
    base: str = "main",
) -> tuple[bool, str, str]:
    """Check out the marathon branch for this iteration, resetting to
    ``origin/<base>`` so each iteration's diff is clean.

    Returns ``(ok, branch_name, message)``. On failure (e.g., the
    working tree has uncommitted changes that would be lost), ``ok`` is
    False and ``message`` carries the error.

    Must be called BEFORE the iteration runs so the auto-commit lands
    on this branch. Existing branches are hard-reset to
    ``origin/<base>`` (the per-issue-persistent-branch model means we
    only keep the latest iteration on the branch).
    """
    branch = _branch_name_for_issue(chapter_label, issue_num)

    # Fetch latest base to ensure the reset target is current.
    fetch = subprocess.run(
        ["git", "fetch", "origin", base],
        cwd=str(repo_dir),
        capture_output=True,
        text=True,
        check=False,
    )
    if fetch.returncode != 0:
        return False, branch, f"git fetch origin {base} failed: {fetch.stderr.strip()[:200]}"

    # If the working tree is dirty, refuse — we'd risk losing uncommitted
    # work by switching branches.
    status = subprocess.run(
        ["git", "status", "--porcelain"],
        cwd=str(repo_dir), capture_output=True, text=True, check=False,
    )
    if status.returncode == 0 and status.stdout.strip():
        return False, branch, (
            "working tree has uncommitted changes; refusing to switch "
            "branches for --auto-pr (the iteration would have run on the "
            "current branch and risked losing work). Commit or stash, then "
            "re-run."
        )

    # Create-or-reset to origin/<base>.
    checkout = subprocess.run(
        ["git", "checkout", "-B", branch, f"origin/{base}"],
        cwd=str(repo_dir), capture_output=True, text=True, check=False,
    )
    if checkout.returncode != 0:
        return False, branch, f"git checkout -B {branch} failed: {checkout.stderr.strip()[:200]}"

    return True, branch, f"on {branch} (reset to origin/{base})"


def _build_pr_body(
    chapter_label: str,
    issue_num: Optional[int],
    iteration: Optional[int],
    build_result: "BuildResult",
    rating: Optional["RatingResult"] = None,
    project_id: Optional[str] = None,
    marathon_md: Optional[str] = None,
    repo: Optional[str] = None,
) -> str:
    """Assemble the PR body — build status, rater scores, issue link,
    marathon.md preview. Kept compact (≤ 8 KB) so the PR list page
    doesn't drown."""
    parts: list[str] = []

    # Header line: links the PR back to the tracking sub-issue.
    if issue_num is not None and repo:
        issue_url = f"https://github.com/{repo}/issues/{issue_num}"
        parts.append(
            f"Marathon refine iteration for [#{issue_num}]({issue_url}) "
            f"({chapter_label})."
        )
    else:
        parts.append(f"Marathon refine iteration ({chapter_label}).")

    # Build status.
    if build_result.skipped_reason:
        parts.append(f"**Build**: skipped — `{build_result.skipped_reason}`")
    elif build_result.timed_out:
        parts.append("**Build**: TIMED OUT")
    elif build_result.ok is True:
        dur = build_result.duration_seconds
        dur_str = f"{dur:.0f}s" if dur is not None else "?"
        parts.append(f"**Build**: ✅ OK ({dur_str})")
    elif build_result.ok is False:
        parts.append("**Build**: ❌ FAIL")
    else:
        parts.append("**Build**: (no result)")

    # Rater scores (single-line tabular summary).
    if rating is not None and rating.parse_error is None:
        scores = " ".join([
            f"q={rating.quality}",
            f"m={rating.math_correctness}",
            f"g={rating.generality}",
            f"api={rating.api_coverage}",
            f"con={rating.concision}",
            f"l4={rating.modern_lean4}",
            f"struct={rating.structural_focus}",
        ])
        parts.append(f"**Rater**: `{scores}`")
        if rating.notes:
            # Keep notes compact — first ~1500 chars so the PR body
            # doesn't balloon when the rater is verbose.
            note = rating.notes.strip()
            if len(note) > 1500:
                note = note[:1500] + "… *(truncated)*"
            parts.append(f"**Rater notes**:\n\n> {note.replace(chr(10), chr(10) + '> ')}")

    # marathon.md design log (truncated).
    if marathon_md:
        md = marathon_md.strip()
        if len(md) > 3000:
            md = md[:3000] + "\n\n*… (truncated; see workdir's marathon.md for the full design log)*"
        parts.append(f"**Design log**:\n\n{md}")

    # Project link.
    if project_id:
        parts.append(
            f"Aristotle project: "
            f"https://aristotle.harmonic.fun/dashboard/requests/{project_id}"
        )

    parts.append(
        "---\n\n"
        "🤖 Opened automatically by `marathon refine --auto-pr`. "
        "Branch is force-pushed each iteration; the diff above always "
        "reflects the latest iteration's content against `main`."
    )
    return "\n\n".join(parts)


def _existing_pr_number(
    repo: str, head: str, repo_dir: Path
) -> Optional[int]:
    """Return the number of an open PR with the given head branch, or
    None if no such PR exists."""
    # ``gh pr list --head <branch>`` returns matching PRs; --json + jq
    # gives us the number without parsing the human-readable output.
    proc = _gh(
        "pr", "list",
        "--repo", repo,
        "--head", head,
        "--state", "open",
        "--json", "number",
        "--jq", ".[0].number // empty",
        cwd=repo_dir,
    )
    if proc.returncode != 0 or not proc.stdout.strip():
        return None
    try:
        return int(proc.stdout.strip())
    except ValueError:
        return None


def open_or_update_pr(
    repo_dir: Path,
    branch: str,
    base: str,
    repo: str,
    title: str,
    body: str,
) -> tuple[bool, str]:
    """Push ``branch`` (force) and open-or-update its PR. Returns
    ``(ok, url_or_error)``."""
    # Force-push (with lease) so each iteration's commits land on the
    # branch cleanly. The per-issue persistent-branch model means the
    # branch's history only ever reflects the latest iteration, so a
    # force-push is safe — no shared collaborators are tracking this
    # branch besides the daemon itself.
    push = subprocess.run(
        ["git", "push", "--force-with-lease", "-u", "origin", branch],
        cwd=str(repo_dir), capture_output=True, text=True, check=False,
    )
    if push.returncode != 0:
        return False, f"git push failed: {(push.stderr or push.stdout).strip()[:300]}"

    existing = _existing_pr_number(repo, branch, repo_dir)
    if existing is not None:
        # Update the existing PR's title + body so the next reviewer
        # sees the freshest iteration's status.
        ed = _gh(
            "pr", "edit", str(existing),
            "--repo", repo,
            "--title", title,
            "--body", body,
            cwd=repo_dir,
        )
        if ed.returncode != 0:
            return False, f"gh pr edit #{existing} failed: {(ed.stderr or ed.stdout).strip()[:300]}"
        return True, f"https://github.com/{repo}/pull/{existing}"

    # No existing PR — open a fresh one.
    cr = _gh(
        "pr", "create",
        "--repo", repo,
        "--base", base,
        "--head", branch,
        "--title", title,
        "--body", body,
        cwd=repo_dir,
    )
    if cr.returncode != 0:
        return False, f"gh pr create failed: {(cr.stderr or cr.stdout).strip()[:300]}"
    # gh prints the new PR URL as the last line of stdout.
    url = cr.stdout.strip().splitlines()[-1] if cr.stdout else ""
    return True, url


def _compute_iteration_diff(
    repo_dir: Path,
    target_path: Path,
    head_sha: str,
) -> Optional[str]:
    """Diff the target path between ``head_sha`` and its parent commit.

    Returns the diff text if both commits exist and the diff is non-empty,
    otherwise None. Truncates very long diffs to the first ~80,000 chars
    to keep the rater prompt within reasonable size.
    """
    try:
        rel = target_path.relative_to(repo_dir)
    except ValueError:
        return None
    proc = subprocess.run(
        ["git", "diff", f"{head_sha}^..{head_sha}", "--", str(rel)],
        cwd=str(repo_dir),
        capture_output=True,
        text=True,
        check=False,
    )
    if proc.returncode != 0:
        return None
    diff = proc.stdout or ""
    if not diff.strip():
        return None
    if len(diff) > 80_000:
        diff = diff[:80_000] + "\n\n... (diff truncated at 80,000 chars)"
    return diff


def call_claude_rater(
    target_path: Path,
    build_result: Optional[BuildResult],
    repo_dir: Optional[Path] = None,
    iteration_commit_sha: Optional[str] = None,
    referee_path: Optional[Path] = None,
) -> RatingResult:
    claude_path = shutil.which("claude")
    if not claude_path:
        return RatingResult(parse_error="claude CLI not on PATH")

    rubric_path = Path(__file__).parent / "prompts" / "rate.md"
    if not rubric_path.is_file():
        return RatingResult(parse_error=f"rate.md missing at {rubric_path}")
    rubric = rubric_path.read_text()

    code = _read_lean_files(target_path)
    if not code:
        return RatingResult(parse_error=f"no .lean files under {target_path}")

    diff: Optional[str] = None
    if repo_dir is not None and iteration_commit_sha:
        diff = _compute_iteration_diff(repo_dir, target_path, iteration_commit_sha)

    referee_md: Optional[str] = None
    if referee_path is not None and referee_path.is_file():
        referee_md = referee_path.read_text()

    parts = [rubric]
    if referee_md:
        parts.append(
            "## Project-specific reviewer priorities (referee.md)\n\n"
            "These notes describe project-specific failure modes the reviewer "
            "agent (separate from you) is asked to push back on. Treat them as "
            "context for what counts as a structural fix on this project — a "
            "diff that closes a referee item is unambiguously structural, "
            "regardless of how small the textual change. Do not score against "
            "items absent from the diff; the rubric above is still the primary "
            "scoring guide.\n\n"
            + referee_md
        )
    if build_result is not None and build_result.ok is not None:
        status = "PASS" if build_result.ok else ("TIMED OUT" if build_result.timed_out else "FAIL")
        build_section = f"## Build status\n\n{status}"
        if not build_result.ok and build_result.log_tail:
            tail = build_result.log_tail
            if len(tail) > 6_000:
                tail = "... (earlier output truncated)\n" + tail[-6_000:]
            build_section += f"\n\n### Build log tail\n\n```\n{tail}\n```"
        parts.append(build_section)
    if diff is not None:
        parts.append(
            "## Diff under review (this iteration's changes)\n\n"
            "```diff\n" + diff + "\n```"
        )
    parts.append(f"## Code under review (current state)\n\n{code}")
    prompt = "\n\n---\n\n".join(parts)

    env = os.environ.copy()
    env.pop("ANTHROPIC_API_KEY", None)

    try:
        proc = subprocess.run(
            [
                claude_path,
                "-p", prompt,
                "--model", "claude-opus-4-7",
                "--tools", "",
                "--output-format", "text",
            ],
            capture_output=True,
            text=True,
            env=env,
            check=False,
        )
    except OSError as e:
        return RatingResult(parse_error=f"could not exec claude (errno {e.errno}: {e.strerror})")

    if proc.returncode != 0:
        err = ((proc.stderr or proc.stdout) or "").strip()[:300]
        return RatingResult(parse_error=f"claude exited {proc.returncode}: {err}")

    response = (proc.stdout or "").strip()
    if not response:
        return RatingResult(parse_error="claude returned empty stdout")

    data, partial_warning = _extract_rating_lenient(response)
    if data is None:
        return RatingResult(
            parse_error=f"could not extract any rating fields; raw: {response[:300]}"
        )

    result = RatingResult(
        quality=_coerce_int(data.get("quality")),
        math_correctness=_coerce_int(data.get("math_correctness")),
        generality=_coerce_int(data.get("generality")),
        api_coverage=_coerce_int(data.get("api_coverage")),
        concision=_coerce_int(data.get("concision")),
        modern_lean4=_coerce_int(data.get("modern_lean4")),
        structural_focus=_coerce_int(data.get("structural_focus")),
        notes=data.get("notes") if isinstance(data.get("notes"), str) else None,
    )
    # Surface partial extractions as a soft warning in parse_error so the
    # jsonl entry records what wasn't recovered, while still preserving
    # whatever scores we did get.
    if partial_warning:
        result.parse_error = partial_warning
    return result


def append_rating(
    ratings_path: Path,
    chapter: str,
    iteration: Optional[int],
    project_id: str,
    build_result: Optional[BuildResult],
    commit_result: Optional[CommitResult],
    rating: RatingResult,
) -> None:
    entry = {
        "ts": datetime.now().astimezone().isoformat(timespec="seconds"),
        "chapter": chapter,
        "iteration": iteration,
        "project_id": project_id,
        "build": (
            {
                "ok": build_result.ok,
                "duration_seconds": build_result.duration_seconds,
                "timed_out": build_result.timed_out,
                "skipped_reason": build_result.skipped_reason,
            }
            if build_result is not None
            else None
        ),
        "commit_sha": commit_result.sha if commit_result else None,
        "rating": asdict(rating),
    }
    ratings_path.parent.mkdir(parents=True, exist_ok=True)
    with ratings_path.open("a") as f:
        f.write(json.dumps(entry) + "\n")


def run_post_pipeline(
    config: PipelineConfig,
    repo_dir: Path,
    target_path: Path,
    chapter_label: str,
    iteration: Optional[int],
    project_id: Optional[str],
    extra_paths_to_stage: Optional[list[str]] = None,
) -> dict:
    """Run the build → commit → rate pipeline. Returns a dict with results."""
    out: dict = {"build": None, "commit": None, "rating": None}

    if config.auto_build:
        b = run_lake_build(repo_dir, config.build_timeout)
        out["build"] = b
        if b.skipped_reason:
            print(f"  build: skipped — {b.skipped_reason}")
        elif b.timed_out:
            print(f"  build: TIMED OUT after {config.build_timeout}s")
        else:
            from marathon.state import format_duration
            duration = format_duration(b.duration_seconds)
            status = "OK" if b.ok else "FAIL"
            print(f"  build: {status} ({duration})")
        # Accumulate iteration build time into the formalization
        # wall-time sidecar (when --update-formalization is on AND
        # the build actually ran). The yaml's
        # automation.cost.wall_time field is re-derived from the
        # sidecar on each refresh. Build-failed iterations still
        # count — the compute was spent.
        if (
            config.update_formalization
            and b.duration_seconds is not None
            and b.duration_seconds > 0
        ):
            try:
                from marathon.formalization import add_wall_seconds
                add_wall_seconds(repo_dir, b.duration_seconds)
            except Exception:  # noqa: BLE001 — soft-warning
                pass

    if config.auto_commit:
        # Refresh formalization.yaml's auto-fields (sorry_count,
        # models, framework) before the commit so the yaml change is
        # bundled into the same commit as the iteration's .lean edits.
        # No-op when the project hasn't opted in (file missing).
        # When the build succeeded this iteration, also refresh the
        # verified-axiom set on every `status.main_results` entry via
        # `#print axioms` (one `lake env lean` invocation, batched
        # across all main results).
        if config.update_formalization:
            try:
                from marathon.formalization import update_formalization
                build_ok = (
                    out["build"] is not None
                    and out["build"].ok is True
                )
                written = update_formalization(
                    repo_dir,
                    models=config.formalization_models,
                    framework=config.formalization_framework,
                    check_axioms_on_build=build_ok,
                )
                if written is not None:
                    suffix = " (with axioms)" if build_ok else ""
                    print(f"  formalization: refreshed {written.name}{suffix}")
            except Exception as e:  # noqa: BLE001 — soft-warning
                print(f"  formalization: skipped — {type(e).__name__}: {e}")

        msg_parts = [f"marathon: {chapter_label}"]
        if iteration is not None:
            msg_parts.append(f"iteration {iteration}")
        if out["build"] is not None and out["build"].ok is not None:
            tag = "OK" if out["build"].ok else ("TIMEOUT" if out["build"].timed_out else "FAIL")
            msg_parts.append(f"[build:{tag}]")
        if project_id:
            msg_parts.append(f"(project={project_id[:8]})")
        message = " ".join(msg_parts)
        c = run_git_commit(
            repo_dir,
            target_path,
            message,
            project_id=project_id,
            claude_in_loop=config.claude_in_loop,
            extra_paths=extra_paths_to_stage,
        )
        out["commit"] = c
        if c.sha:
            print(f"  commit: {c.sha}  message=\"{message}\"")
        else:
            print(f"  commit: skipped — {c.skipped_reason}")

        if config.auto_push and c.sha:
            ok, push_msg = run_git_push(repo_dir)
            if ok:
                print(f"  push: ok ({push_msg})")
            else:
                print(f"  push: failed — {push_msg}")


        # Post-commit audit: did this iteration touch any verified
        # declarations the human has already locked? Soft warning;
        # logs to <workdir>/marathon-audit-violations.jsonl. Graceful
        # no-op when the consumer repo isn't using `marathon review`.
        if config.audit_verified and c.sha:
            try:
                _run_verified_decls_audit(
                    repo_dir=repo_dir,
                    chapter_label=chapter_label,
                    commit_sha=c.sha,
                    workdir=config.audit_workdir,
                )
            except Exception as e:  # noqa: BLE001 — soft-warning audit
                print(f"  audit: skipped — {type(e).__name__}: {e}")

    if config.auto_rate:
        commit_sha = out["commit"].sha if out["commit"] else None
        r = call_claude_rater(
            target_path,
            out["build"],
            repo_dir=repo_dir,
            iteration_commit_sha=commit_sha,
            referee_path=config.referee_path,
        )
        out["rating"] = r
        if r.parse_error:
            print(f"  rating: parse error — {r.parse_error}")
        else:
            sf = r.structural_focus if r.structural_focus is not None else "—"
            con = r.concision if r.concision is not None else "—"
            print(
                f"  rating: q={r.quality} m={r.math_correctness} g={r.generality} "
                f"api={r.api_coverage} con={con} lean4={r.modern_lean4} struct={sf}"
            )
            if r.notes:
                print(f"  notes: {r.notes}")
        if config.ratings_path is not None:
            try:
                append_rating(
                    config.ratings_path,
                    chapter=chapter_label,
                    iteration=iteration,
                    project_id=project_id or "",
                    build_result=out["build"],
                    commit_result=out["commit"],
                    rating=r,
                )
            except OSError as e:
                print(f"  ratings log: could not append — {e}")

    # --- auto-pr: push the marathon branch + open/update its PR --------
    # Runs LAST so the rater scores (just computed above) land in the PR
    # body. The marathon branch was prepared by refine.py before this
    # iteration ran (via prepare_auto_pr_branch); we only handle the
    # push + PR here. No-op when the iteration didn't commit (c.sha is
    # None — usually because there was nothing to commit).
    if (
        config.auto_pr
        and out["commit"] is not None
        and out["commit"].sha is not None
    ):
        try:
            repo = config.auto_pr_repo or _infer_repo(repo_dir)
            if repo is None:
                print("  pr: skipped — could not infer GitHub repo "
                      "(set --auto-pr-repo or run `gh auth login`)")
            else:
                branch = _branch_name_for_issue(
                    chapter_label, config.auto_pr_review_issue
                )
                issue_part = (
                    f" iter for #{config.auto_pr_review_issue}"
                    if config.auto_pr_review_issue is not None
                    else ""
                )
                build_tag = ""
                if out["build"] is not None and out["build"].ok is not None:
                    build_tag = " [build:" + (
                        "OK" if out["build"].ok
                        else ("TIMEOUT" if out["build"].timed_out else "FAIL")
                    ) + "]"
                extra_chapters = _extra_chapters_in_writes(
                    chapter_label, extra_paths_to_stage or []
                )
                extra_suffix = (
                    " (" + " + ".join(f"+Ch{n}" for n in extra_chapters) + ")"
                    if extra_chapters else ""
                )
                pr_title = f"marathon: {chapter_label}{issue_part}{extra_suffix}{build_tag}"
                # Try to embed marathon.md (Aristotle's design log)
                # when the workdir is known.
                marathon_md_text: Optional[str] = None
                if config.audit_workdir is not None:
                    md_path = config.audit_workdir / "marathon.md"
                    if md_path.is_file():
                        try:
                            marathon_md_text = md_path.read_text()
                        except OSError:
                            pass
                pr_body = _build_pr_body(
                    chapter_label=chapter_label,
                    issue_num=config.auto_pr_review_issue,
                    iteration=iteration,
                    build_result=out["build"] or BuildResult(),
                    rating=out["rating"],
                    project_id=project_id,
                    marathon_md=marathon_md_text,
                    repo=repo,
                )
                pr_ok, pr_msg = open_or_update_pr(
                    repo_dir=repo_dir,
                    branch=branch,
                    base=config.auto_pr_base,
                    repo=repo,
                    title=pr_title,
                    body=pr_body,
                )
                if pr_ok:
                    print(f"  pr: {pr_msg}")
                else:
                    print(f"  pr: failed — {pr_msg}")
        except Exception as e:  # noqa: BLE001 — soft-warning
            print(f"  pr: skipped — {type(e).__name__}: {e}")

    return out


def _run_verified_decls_audit(
    repo_dir: Path,
    chapter_label: str,
    commit_sha: str,
    workdir: Optional[Path],
) -> None:
    """Helper invoked from ``run_post_pipeline`` when
    ``config.audit_verified`` is True and a commit landed.

    Parses the chapter number from ``chapter_label`` (e.g. ``Chapter14``
    → 14), loads the review config, runs
    ``audit_iteration(HEAD~1..commit_sha)``, prints results, and
    appends to the workdir's audit-violations JSONL.

    Soft no-op if the review config isn't present or the chapter
    label doesn't match the expected ``Chapter<N>`` shape.
    """
    import re as _re
    m = _re.match(r"Chapter(\d+)$", chapter_label)
    if not m:
        return  # not a chapter-style label; nothing to audit against
    chapter_num = int(m.group(1))

    try:
        from marathon.review.config import load_config
        from marathon.review.verified_decls import (
            audit_iteration,
            write_audit_log,
        )
    except ImportError:
        return

    config_path = repo_dir / ".marathon" / "review" / "config.toml"
    if not config_path.is_file():
        return

    try:
        cfg = load_config(repo_dir=repo_dir)
    except SystemExit:
        return
    if chapter_num not in cfg.chapters:
        return

    result = audit_iteration(
        cfg, chapter_num, repo_dir, ref_old=f"{commit_sha}~1", ref_new=commit_sha,
    )

    print(
        f"  audit: scanned {result.verified_decl_count} verified decls across "
        f"{result.verified_issue_count} verified issues; iteration modified "
        f"{result.modified_decl_count} decls."
    )
    if result.has_violations():
        print(
            f"  ⚠ audit: {len(result.violations)} verified declaration(s) "
            f"modified by this iteration:"
        )
        for v in result.violations:
            title = f" — {v.issue_title}" if v.issue_title else ""
            print(f"    #{v.issue_num}{title}: {v.decl_name}")
        print(
            "  ⚠ recommended: inspect the diff; if the changes are unwanted, "
            "`git revert` this commit or `git checkout HEAD~1 -- <file>` for "
            "the offending files, then re-launch refine."
        )
    if workdir is not None:
        try:
            log_path = write_audit_log(
                workdir, result,
                ref_old=f"{commit_sha}~1",
                ref_new=commit_sha,
            )
            print(f"  audit: log written to {log_path}")
        except OSError as e:
            print(f"  audit: could not write log — {e}")


# Helpers

def _read_lean_files(folder: Path) -> str:
    parts: list[str] = []
    for lean_file in sorted(folder.rglob("*.lean")):
        rel = lean_file.relative_to(folder)
        parts.append(f"=== FILE: {rel} ===\n{lean_file.read_text()}")
    return "\n\n".join(parts)


def _extract_json_object(text: str) -> str:
    """Try to extract a JSON object from a Claude response."""
    text = text.strip()
    if text.startswith("{"):
        return text
    fence = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", text, re.DOTALL)
    if fence:
        return fence.group(1)
    bare = re.search(r"\{.*\}", text, re.DOTALL)
    if bare:
        return bare.group(0)
    raise ValueError("no JSON object found in response")


_RATING_SCORE_FIELDS = (
    "quality", "math_correctness", "generality", "api_coverage",
    "concision", "modern_lean4", "structural_focus",
)


def _extract_rating_lenient(response: str) -> tuple[Optional[dict], Optional[str]]:
    """Tolerant extraction of a rating from a Claude response.

    Tries strict ``json.loads`` on the extracted JSON-shaped substring
    first. If that fails (typically because the ``notes`` string contains
    an unescaped quote, control character, or other JSON-hostile
    sequence), falls back to regex-extracting each known score field
    independently. This recovers numeric scores even when the notes
    string is malformed.

    Returns ``(data, partial_warning)``:
      - ``(dict, None)`` on clean strict parse;
      - ``(dict, "warning text")`` on partial extraction (e.g. scores
        extracted but notes string couldn't be recovered);
      - ``(None, None)`` if no fields could be extracted at all.
    """
    # First try strict parse via the existing extractor.
    try:
        json_text = _extract_json_object(response)
        return json.loads(json_text), None
    except (json.JSONDecodeError, ValueError):
        pass

    # Fallback: regex-extract each score field independently.
    data: dict = {}
    missing_score_fields: list[str] = []
    for field in _RATING_SCORE_FIELDS:
        m = re.search(rf'"{field}"\s*:\s*(\d+|null)\b', response)
        if m:
            v = m.group(1)
            data[field] = None if v == "null" else int(v)
        else:
            missing_score_fields.append(field)

    # Notes: best-effort. Lazy match up to the first `"` followed by `,` or `}`.
    notes_m = re.search(r'"notes"\s*:\s*"([\s\S]*?)"\s*[},]', response)
    if notes_m:
        raw_notes = notes_m.group(1)
        # Unescape basic JSON escapes; leave other content alone.
        data["notes"] = (
            raw_notes
            .replace(r"\n", "\n")
            .replace(r'\"', '"')
            .replace(r"\\", "\\")
        )
        notes_recovered = True
    else:
        notes_recovered = False

    if not data:
        return None, None

    parts = ["lenient-parse fallback used (strict json.loads failed)"]
    if missing_score_fields:
        parts.append(f"missing score fields: {','.join(missing_score_fields)}")
    if not notes_recovered:
        parts.append("notes string could not be recovered")
    return data, "; ".join(parts)


def _coerce_int(v) -> Optional[int]:
    if isinstance(v, int):
        return v
    if isinstance(v, float) and v.is_integer():
        return int(v)
    if isinstance(v, str):
        try:
            return int(v.strip())
        except ValueError:
            return None
    return None
