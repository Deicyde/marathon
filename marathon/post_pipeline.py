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
) -> CommitResult:
    """Stage ``target_path`` (and ``PromptLog.md`` if it exists and is
    dirty) and commit. The final commit message includes a project URL
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
