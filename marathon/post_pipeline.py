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
import re
import shutil
import subprocess
import sys
from dataclasses import asdict, dataclass, field
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING, Optional

from marathon.claude_proc import run_claude

if TYPE_CHECKING:  # gate/jury are imported lazily at the call sites
    from marathon.gate import GateReport
    from marathon.jury import JuryVerdict


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
    # When False (--no-metadata-commit), ``formalization.yaml`` is
    # EXCLUDED from the per-iteration git staging and its per-iteration
    # refresh is skipped: N parallel conductor workers all rewriting
    # the yaml is the generalized form of the wall_time merge race, so
    # the Conductor regenerates it centrally in the primary checkout
    # after each successful job (``formalization.regenerate_metadata``)
    # — one writer instead of N racing ones. The project-id-keyed
    # wall-time sidecar and the PromptLog stay committed either way:
    # they are merge-friendly by design (write-once keys / append-only).
    commit_metadata: bool = True
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
    # --- machine gate (phase-2) -----------------------------------------
    # Posture ∈ {"off", "warn", "enforce"}. ``warn`` (the default) runs
    # the deterministic gate (marathon.gate: build + axiom whitelist +
    # sorry accounting + forbidden keywords) and reports to console +
    # PR body without ever blocking. ``enforce`` additionally blocks the
    # PR open/update step on a fail-level verdict — NEVER the commit or
    # push (the work is always preserved). ``off`` skips the gate
    # entirely. Enforcement lives here in the wiring, not in the engine.
    gate: str = "warn"
    # Operator override for ``enforce``: when set and the gate verdict
    # is fail, the PR opens anyway and this reason is recorded in the
    # PR body's Gate section and the console — an audited override.
    gate_override: Optional[str] = None
    # Path of the gate's persisted snapshot (sorry counts baseline),
    # ``<workdir>/marathon-gate-state.json`` beside the ratings jsonl.
    # Set by refine; ``None`` ⇒ no baseline, no persistence (the gate
    # still runs, with the sorry delta explicitly unevaluated).
    gate_state_path: Optional[Path] = None
    # Gate mode selector: True ⇒ skeleton mode (sorry bodies expected;
    # only definition-body sorry deltas warn), False ⇒ proof mode (new
    # sorries are regressions). Set by refine from ``--skeleton``.
    skeleton_mode: bool = False
    # True when this iteration was dispatched by ``--review-rejection N``
    # — a human-demanded run. Enforcement never blocks those (the PR-#99
    # lesson: cross-chapter refactors necessarily transit red; the
    # human's explicit ask wins), so ``enforce`` demotes to ``warn``
    # with a printed note.
    review_rejection_run: bool = False
    # Advisory jury (marathon.jury): Claude-scored proof_integrity +
    # code_quality, no faithfulness. Runs only when True; the verdict
    # line joins the console output + PR body and one JSON line is
    # appended to ``jury_log_path`` (rater-jsonl pattern).
    jury: bool = False
    jury_log_path: Optional[Path] = None

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


# Workdir-side gate artifacts, siblings of marathon-ratings.jsonl. The
# state file carries the previous run's sorry counts so the gate's
# delta semantics survive across iterations; the jury jsonl is the
# advisory jury's append-only trail (rater-jsonl pattern).
GATE_STATE_FILENAME = "marathon-gate-state.json"
JURY_LOG_FILENAME = "marathon-jury.jsonl"

PROMPTLOG_FILENAME = ".marathon/PromptLog.md"
# Legacy location of PromptLog.md, kept for backward compatibility on
# repos that haven't yet moved the file under ``.marathon/``. We check
# both locations on read and prefer the new one when both exist; on write
# we always target the new location (and the auto-pr safety check only
# whitelists ``.marathon/**``, so the new location avoids the
# "marathon-managed file at repo root blocks branch switch" failure).
_LEGACY_PROMPTLOG_FILENAME = "PromptLog.md"


def _resolve_promptlog_path(repo_dir: Path) -> Optional[Path]:
    """Return the active ``PromptLog.md`` path, preferring
    ``.marathon/PromptLog.md`` over the legacy repo-root location. Returns
    None if neither exists (the per-repo opt-in convention)."""
    new_path = repo_dir / PROMPTLOG_FILENAME
    if new_path.is_file():
        return new_path
    legacy_path = repo_dir / _LEGACY_PROMPTLOG_FILENAME
    if legacy_path.is_file():
        return legacy_path
    return None


def append_promptlog_url(repo_dir: Path, project_id: str) -> bool:
    """If ``PromptLog.md`` exists (preferred under ``.marathon/``, legacy
    at repo root), append a blank line plus a ``<timestamp>  <project_id>``
    entry. Skips silently if the file doesn't exist or ``project_id`` is
    empty.

    Returns True if the file was appended to, False if skipped.
    """
    if not project_id:
        return False
    log_path = _resolve_promptlog_path(repo_dir)
    if log_path is None:
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
    commit_metadata: bool = True,
) -> CommitResult:
    """Stage ``target_path`` (and ``PromptLog.md`` if it exists and is
    dirty) and commit. ``extra_paths`` (repo-relative POSIX paths) are
    additionally staged — used for cross-chapter refactor iterations
    where Aristotle edits files in ``extra_writable_paths`` outside the
    primary target. ``commit_metadata=False`` excludes
    ``formalization.yaml`` from the staging (conductor workers; the
    yaml is regenerated centrally — see ``PipelineConfig``); the
    merge-friendly PromptLog + wall-time sidecar are staged regardless.
    The final commit message includes a project URL line (when
    ``project_id`` is set) and a Co-authored-by trailer block
    crediting Aristotle (and Claude, when ``claude_in_loop`` is True).
    Skips silently if the index is busy or there's nothing to commit."""
    try:
        rel = target_path.relative_to(repo_dir)
    except ValueError:
        return CommitResult(skipped_reason=f"{target_path} not under repo {repo_dir}")

    paths_to_stage = [str(rel)]
    promptlog = _resolve_promptlog_path(repo_dir)
    if promptlog is not None:
        try:
            paths_to_stage.append(str(promptlog.relative_to(repo_dir)))
        except ValueError:
            pass
    # The project-id-keyed wall-time sidecar is staged with every
    # iteration commit: write-once keys merge cleanly across parallel
    # workers (unlike the yaml's derived counter fields), so committing
    # it from N workers is safe by construction.
    from marathon.formalization import WALL_TIME_SIDECAR_RELPATH
    if (repo_dir / WALL_TIME_SIDECAR_RELPATH).is_file():
        paths_to_stage.append(WALL_TIME_SIDECAR_RELPATH.as_posix())
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
    # hasn't opted in. Skipped under --no-metadata-commit: the yaml's
    # derived fields (sorry counts, wall_time rollup) do NOT merge
    # cleanly across parallel workers, so conductor jobs leave it to
    # the conductor's central regeneration.
    if commit_metadata:
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
    # work by switching branches. EXCEPT: if the only dirty files are
    # marathon-managed (under ``.marathon/``), auto-commit them so the
    # branch switch can proceed and the audit trail stays tracked. Any
    # non-marathon dirt still refuses.
    status = subprocess.run(
        ["git", "status", "--porcelain"],
        cwd=str(repo_dir), capture_output=True, text=True, check=False,
    )
    if status.returncode == 0 and status.stdout.strip():
        # NOTE: don't ``.strip()`` before ``.splitlines()`` — porcelain v1
        # reserves col 0 for the unstaged-status code (often a space, e.g.
        # `` M path`` for unstaged-modified). Stripping the whole output
        # eats the first line's leading space and shifts column indices.
        dirty_lines = status.stdout.rstrip("\n").splitlines()
        # ``git status --porcelain`` prefixes each line with a 2-char
        # status code + space; the path starts at column 3.
        marathon_only = all(
            line[3:].startswith(".marathon/") for line in dirty_lines
        )
        if not marathon_only:
            offending = [l for l in dirty_lines if not l[3:].startswith(".marathon/")]
            return False, branch, (
                "working tree has uncommitted non-marathon changes; "
                "refusing to switch branches for --auto-pr (the iteration "
                "would have run on the current branch and risked losing "
                "work). Commit or stash, then re-run. "
                f"Offending lines: {offending!r}"
            )
        # All dirt is marathon bookkeeping — auto-commit it inline so the
        # branch switch is safe and the audit trail stays tracked.
        subprocess.run(
            ["git", "add", "--", ".marathon/"],
            cwd=str(repo_dir), capture_output=True, text=True, check=False,
        )
        commit = subprocess.run(
            ["git", "commit", "-m", f"chore(marathon): auto-bump for {branch}"],
            cwd=str(repo_dir), capture_output=True, text=True, check=False,
        )
        if commit.returncode != 0:
            return False, branch, (
                f"auto-commit of .marathon/ failed: "
                f"{(commit.stderr or commit.stdout).strip()[:200]}"
            )

    # Create-or-reset to origin/<base>.
    checkout = subprocess.run(
        ["git", "checkout", "-B", branch, f"origin/{base}"],
        cwd=str(repo_dir), capture_output=True, text=True, check=False,
    )
    if checkout.returncode != 0:
        return False, branch, f"git checkout -B {branch} failed: {checkout.stderr.strip()[:200]}"

    return True, branch, f"on {branch} (reset to origin/{base})"


# ---------------------------------------------------------------------------
# Machine gate (phase-2) wiring helpers
# ---------------------------------------------------------------------------
#
# The engine (marathon.gate) is pure and posture-free by design; the
# wiring below owns baselines, persistence, rendering destinations, and
# enforcement. Faithfulness is deliberately absent everywhere — the
# information firewall keeps the source text away from every Claude
# call, so faithfulness review stays human (marathon-v2 plan §2 r.3).


def _load_gate_baseline(
    state_path: Path, target_rel: str, mode: str
) -> Optional[dict]:
    """Return the previous gate run's ``{"total": …, "definitions": …}``
    sorry counts from the workdir snapshot, or ``None`` when the file is
    missing/corrupt or was written for a different target (a recycled
    workdir must not feed another chapter's counts into the delta) or
    under a different gate ``mode`` — skeleton iterations are expected
    to ADD theorem-body sorries, so a cross-mode delta would read an
    expected product as a regression (or vice versa). The mode mismatch
    is printed (unlike the silent missing/corrupt cases) because a mode
    flip is an operator decision worth surfacing."""
    try:
        data = json.loads(state_path.read_text())
    except (OSError, ValueError):
        return None
    if not isinstance(data, dict) or data.get("target") != target_rel:
        return None
    baseline_mode = data.get("mode")
    if baseline_mode != mode:
        print(
            f"  gate: ignoring sorry baseline written under mode "
            f"{baseline_mode!r} (this run is {mode!r}; cross-mode deltas "
            "mislead) — treating as no baseline"
        )
        return None
    counts = data.get("sorry_counts")
    if not isinstance(counts, dict):
        return None
    return counts


def _save_gate_state(
    state_path: Path,
    *,
    target_rel: str,
    mode: str,
    verdict: str,
    iteration: Optional[int],
    total: int,
    definitions: int,
) -> None:
    """Persist this iteration's gate snapshot — next run's baseline.
    Whole-file rewrite, not append: the baseline is always exactly the
    last run's counts (history lives in git / the ratings jsonl)."""
    payload = {
        "ts": datetime.now().astimezone().isoformat(timespec="seconds"),
        "target": target_rel,
        "mode": mode,
        "verdict": verdict,
        "iteration": iteration,
        "sorry_counts": {"total": total, "definitions": definitions},
    }
    state_path.parent.mkdir(parents=True, exist_ok=True)
    state_path.write_text(json.dumps(payload, indent=2) + "\n")


def _append_jury_entry(
    jury_log_path: Path,
    chapter: str,
    iteration: Optional[int],
    project_id: str,
    commit_result: Optional["CommitResult"],
    verdict: "JuryVerdict",
) -> None:
    """Append one jury verdict as a JSON line (same shape conventions as
    ``append_rating``: timestamp + iteration coordinates + payload)."""
    entry = {
        "ts": datetime.now().astimezone().isoformat(timespec="seconds"),
        "chapter": chapter,
        "iteration": iteration,
        "project_id": project_id,
        "commit_sha": commit_result.sha if commit_result else None,
        "jury": asdict(verdict),
    }
    jury_log_path.parent.mkdir(parents=True, exist_ok=True)
    with jury_log_path.open("a") as f:
        f.write(json.dumps(entry) + "\n")


def _build_gate_section(
    gate_report: Optional["GateReport"],
    jury_verdict: Optional["JuryVerdict"],
    override_reason: Optional[str],
) -> Optional[str]:
    """Assemble the PR body's ``## Gate`` section: the deterministic
    gate's markdown report, the advisory jury line, and — when the
    operator overrode a fail verdict under enforce — the audited
    override reason. Returns ``None`` when there is nothing to show so
    PR bodies stay unchanged for runs with the gate off and no jury."""
    parts: list[str] = []
    if gate_report is not None:
        parts.append(gate_report.render_markdown())
    if jury_verdict is not None:
        parts.append(f"**Jury**: `{jury_verdict.render_line()}`")
    if override_reason is not None:
        parts.append(
            "**Gate override**: PR opened despite a FAIL gate verdict "
            f"under `--gate enforce` — operator reason: {override_reason}"
        )
    if not parts:
        return None
    return "## Gate\n\n" + "\n\n".join(parts)


def _build_pr_body(
    chapter_label: str,
    issue_num: Optional[int],
    iteration: Optional[int],
    build_result: "BuildResult",
    rating: Optional["RatingResult"] = None,
    project_id: Optional[str] = None,
    marathon_md: Optional[str] = None,
    repo: Optional[str] = None,
    gate_section: Optional[str] = None,
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

    # Machine gate + advisory jury (phase-2). Pre-assembled by
    # _build_gate_section so this function stays a dumb renderer.
    if gate_section:
        parts.append(gate_section)

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

    # Subprocess conventions (stdin prompt against E2BIG, API-key scrub
    # for Max OAuth, cross-process slot limiter) live in
    # marathon.claude_proc.run_claude. The model is pinned explicitly —
    # historical rater behavior: no MARATHON_CLAUDE_MODEL override.
    try:
        proc = run_claude(prompt, model="claude-opus-4-7")
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
    iteration_duration_seconds: Optional[float] = None,
) -> dict:
    """Run the build → commit → rate pipeline. Returns a dict with results.

    ``iteration_duration_seconds`` is the Aristotle wall-clock for this
    iteration (typically the dominant compute cost — minutes to half-hours).
    It is accumulated alongside the lake build duration into the
    formalization wall-time sidecar so ``formalization.yaml`` reflects
    actual compute spent, not just local build time.
    """
    out: dict = {
        "build": None,
        "commit": None,
        "rating": None,
        "gate": None,
        "gate_posture": None,
        "jury": None,
    }

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
        # Accumulate iteration wall-clock into the formalization wall-time
        # sidecar (when --update-formalization is on). The yaml's
        # automation.cost.wall_time field is re-derived from the sidecar
        # on each refresh. Build-failed iterations still count — the
        # compute (Aristotle + lake) was spent.
        if config.update_formalization:
            seconds_to_add = 0.0
            if iteration_duration_seconds is not None and iteration_duration_seconds > 0:
                seconds_to_add += float(iteration_duration_seconds)
            if b.duration_seconds is not None and b.duration_seconds > 0:
                seconds_to_add += float(b.duration_seconds)
            if seconds_to_add > 0:
                try:
                    from marathon.formalization import add_wall_seconds
                    # Pass the Aristotle project_id so the sidecar entry
                    # is keyed by project — idempotent on re-runs, and
                    # immune to the merge race that caused main's
                    # wall_time to go *down* when two iteration branches
                    # racing off the same base both PR'd a counter update.
                    add_wall_seconds(repo_dir, seconds_to_add, project_id=project_id)
                except Exception:  # noqa: BLE001 — soft-warning
                    pass

    # --- machine gate (phase-2) ----------------------------------------
    # Runs right after the build step because it CONSUMES the build
    # outcome (the gate never re-runs lake) and before the commit so
    # the console verdict sits next to the build line. Posture:
    # off ⇒ skip entirely; warn (default) ⇒ report only; enforce ⇒ a
    # fail verdict blocks the PR open/update step further down — never
    # the commit/push, so the work is always preserved.
    gate_posture = (config.gate or "warn").lower()
    if gate_posture not in ("off", "warn", "enforce"):
        print(f"  gate: unknown posture {config.gate!r}; treating as warn")
        gate_posture = "warn"
    if gate_posture == "enforce" and config.review_rejection_run:
        # The PR-#99 lesson: cross-chapter refactors necessarily transit
        # red, and a --review-rejection iteration is the human's
        # explicit ask. Enforcement never blocks human-demanded runs.
        print(
            "  gate: posture demoted enforce → warn for this run "
            "(--review-rejection iterations are human-demanded; "
            "enforcement never blocks them)"
        )
        gate_posture = "warn"
    out["gate_posture"] = gate_posture
    if gate_posture != "off":
        try:
            from marathon import gate as gate_engine

            b = out["build"]
            try:
                target_rel = target_path.relative_to(repo_dir).as_posix()
            except ValueError:
                target_rel = str(target_path)
            gate_mode = (
                gate_engine.MODE_SKELETON
                if config.skeleton_mode
                else gate_engine.MODE_PROOF
            )
            prev_counts = (
                _load_gate_baseline(config.gate_state_path, target_rel, gate_mode)
                if config.gate_state_path is not None
                else None
            )
            report = gate_engine.run_gate(
                repo_dir,
                target_path,
                mode=gate_mode,
                build_ok=b.ok if b is not None else None,
                build_log_tail=b.log_tail if b is not None else None,
                prev_sorry_counts=prev_counts,
            )
            out["gate"] = report
            for line in report.render_console().splitlines():
                print(f"  {line}")
            # Persist this iteration's counts as the next run's
            # baseline. Skipped for a missing target folder — writing a
            # 0/0 snapshot there would fabricate a "regression" the
            # moment the folder reappears.
            if config.gate_state_path is not None and target_path.is_dir():
                counts = gate_engine.measure_sorries(target_path)
                try:
                    _save_gate_state(
                        config.gate_state_path,
                        target_rel=target_rel,
                        mode=report.mode,
                        verdict=report.verdict,
                        iteration=iteration,
                        total=counts.total,
                        definitions=counts.definitions,
                    )
                except OSError as e:
                    print(f"  gate: could not persist state — {e}")
        except Exception as e:  # noqa: BLE001 — gate must not break the pipeline
            # Fail OPEN, loudly: a crashed gate yields no report, so
            # enforcement below cannot block. Blocking on gate bugs
            # would make the gate the outage, not the safety net.
            print(f"  gate: skipped — {type(e).__name__}: {e}")

    if config.auto_commit:
        # Refresh formalization.yaml's auto-fields (sorry_count,
        # models, framework) before the commit so the yaml change is
        # bundled into the same commit as the iteration's .lean edits.
        # No-op when the project hasn't opted in (file missing).
        # When the build succeeded this iteration, also refresh the
        # verified-axiom set on every `status.main_results` entry via
        # `#print axioms` (one `lake env lean` invocation, batched
        # across all main results). Skipped entirely under
        # --no-metadata-commit: refreshing a yaml we won't commit
        # would leave the worker's checkout dirty at the repo root
        # (blocking the next iteration's branch switch) for a file the
        # conductor regenerates centrally anyway.
        if config.update_formalization and not config.commit_metadata:
            print(
                "  formalization: deferred — --no-metadata-commit "
                "(regenerated centrally by the conductor after the job lands)"
            )
        elif config.update_formalization:
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
            commit_metadata=config.commit_metadata,
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

    # --- advisory jury (phase-2): only when --jury is on ----------------
    # Runs after the commit so the iteration diff can be attached (same
    # source as the rater's diff). The jury never gates by itself —
    # run_jury returns None on any failure and prints its own skip note.
    if config.jury:
        try:
            from marathon.jury import run_jury

            commit_sha = out["commit"].sha if out["commit"] else None
            jury_diff = (
                _compute_iteration_diff(repo_dir, target_path, commit_sha)
                if commit_sha
                else None
            )
            verdict = run_jury(repo_dir, target_path, diff_text=jury_diff)
            out["jury"] = verdict
            if verdict is not None:
                print(f"  {verdict.render_line()}")
                if config.jury_log_path is not None:
                    try:
                        _append_jury_entry(
                            config.jury_log_path,
                            chapter=chapter_label,
                            iteration=iteration,
                            project_id=project_id or "",
                            commit_result=out["commit"],
                            verdict=verdict,
                        )
                    except OSError as e:
                        print(f"  jury log: could not append — {e}")
        except Exception as e:  # noqa: BLE001 — advisory, never breaks the pipeline
            print(f"  jury: skipped — {type(e).__name__}: {e}")

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
                # PR-title chapter suffix sources from `git diff
                # --name-only HEAD~1 HEAD` (actually-changed files),
                # not from `extra_paths_to_stage` (the writable scope,
                # which includes echo-writes Aristotle made that don't
                # differ from main). Otherwise multi-chapter writable
                # scopes produce inflated titles like
                # "(+Ch10 + +Ch12 + +Ch14 + +Ch15 + +Ch16)" when only
                # one extra chapter actually changed.
                diff_proc = subprocess.run(
                    ["git", "diff", "--name-only", "HEAD~1", "HEAD"],
                    cwd=str(repo_dir), capture_output=True, text=True,
                    check=False,
                )
                changed_paths = (
                    diff_proc.stdout.splitlines()
                    if diff_proc.returncode == 0 else (extra_paths_to_stage or [])
                )
                extra_chapters = _extra_chapters_in_writes(
                    chapter_label, changed_paths
                )
                extra_suffix = (
                    " (" + " ".join(f"+Ch{n}" for n in extra_chapters) + ")"
                    if extra_chapters else ""
                )
                pr_title = f"marathon: {chapter_label}{issue_part}{extra_suffix}{build_tag}"
                # --- gate enforcement (phase-2) -----------------------
                # The ONLY step enforcement may block is this PR
                # open/update. The commit (and any push) already landed
                # above — the work is preserved either way.
                gate_report = out["gate"]
                override_reason: Optional[str] = None
                gate_blocked = False
                if (
                    out["gate_posture"] == "enforce"
                    and gate_report is not None
                    and gate_report.verdict == "fail"
                ):
                    if config.gate_override:
                        override_reason = config.gate_override
                        print(
                            f'  gate: override accepted — "{override_reason}" '
                            "(FAIL verdict under enforce; opening the PR; "
                            "reason recorded in the PR body's Gate section)"
                        )
                    else:
                        gate_blocked = True
                if gate_blocked:
                    failing = "; ".join(
                        f"{c.name}: {c.summary}"
                        for c in gate_report.checks
                        if c.status == "fail"
                    ) or "(see gate report above)"
                    print(
                        "  pr: BLOCKED by gate — verdict FAIL under "
                        f"--gate enforce; skipped the PR open/update for "
                        f"branch {branch} → {config.auto_pr_base}."
                    )
                    print(f"      failing checks: {failing}")
                    print(
                        f"      commit {out['commit'].sha} was NOT blocked "
                        "— the work is preserved locally; re-run with "
                        '--gate-override "REASON" to open the PR anyway.'
                    )
                else:
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
                        gate_section=_build_gate_section(
                            gate_report, out["jury"], override_reason
                        ),
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
