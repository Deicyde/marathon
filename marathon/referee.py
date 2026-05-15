"""The ``marathon referee`` subcommand and ``update_referee`` helper.

Runs a Claude agent that scans the LeeSM repo + all per-chapter workdirs +
the existing referee.md, and rewrites the machine-managed tail of
``referee.md`` to reflect the most pressing project-specific issues.

Two entry points:

- ``referee_command(args)`` — invoked via ``marathon referee``. One-shot
  pass with optional ``--review`` to write to ``referee.md.proposed``
  instead of overwriting.
- ``update_referee(...)`` — library function callable from ``refine``'s
  inner loop via the ``--auto-referee-every N`` flag. Runs synchronously
  in the iteration loop; treats failures as warnings, not aborts.

The referee.md file is split into a **user-managed header** and a
**machine-managed tail** by sentinel comments. On first run (no
sentinel), the existing file becomes the user header and an empty
machine tail is appended.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

REFEREE_FILENAME = ".marathon/referee.md"
REFEREE_PROPOSED_SUFFIX = ".proposed"
REFEREE_MODEL = "claude-opus-4-7"

BEGIN_SENTINEL = "<!-- BEGIN: Marathon-managed referee tail (do not edit below this line; use `marathon referee` to refresh) -->"
END_SENTINEL = "<!-- END: Marathon-managed referee tail -->"

REFEREE_COAUTHOR_TRAILER = "Co-authored-by: Claude <noreply@anthropic.com>"


@dataclass
class RefereeResult:
    """Outcome of one referee pass."""
    ok: bool = False
    output_path: Optional[Path] = None
    commit_sha: Optional[str] = None
    diff_summary: Optional[str] = None  # short stats: lines +/-
    machine_tail_len: Optional[int] = None
    pushed: Optional[bool] = None  # None = not attempted; True/False = outcome
    push_message: Optional[str] = None
    skipped_reason: Optional[str] = None  # if we declined to run
    error: Optional[str] = None  # if Claude failed / output unparseable


def _split_referee(text: str) -> tuple[str, Optional[str]]:
    """Split an existing referee.md into (user_header, machine_tail).

    Returns ``(text, None)`` if the file has no sentinel — the whole
    file is treated as user-managed and the caller should attach a
    fresh machine tail at the end.
    """
    if BEGIN_SENTINEL not in text or END_SENTINEL not in text:
        return text.rstrip(), None
    begin_idx = text.index(BEGIN_SENTINEL)
    end_idx = text.index(END_SENTINEL)
    if end_idx < begin_idx:
        # Malformed — treat whole thing as user header.
        return text.rstrip(), None
    user_header = text[:begin_idx].rstrip()
    machine_tail = text[begin_idx + len(BEGIN_SENTINEL):end_idx].strip()
    return user_header, machine_tail


def _assemble_referee(user_header: str, new_machine_tail: str) -> str:
    """Recombine user_header + sentinels + new_machine_tail into a single
    referee.md body."""
    parts = [user_header.rstrip()]
    parts.append("")  # blank line before sentinel
    parts.append(BEGIN_SENTINEL)
    parts.append("")
    parts.append(new_machine_tail.strip())
    parts.append("")
    parts.append(END_SENTINEL)
    parts.append("")  # trailing newline
    return "\n".join(parts)


def _read_chapter_artifacts(workdir: Path, max_chars_each: int = 8_000) -> str:
    """Bundle one chapter's marathon.md + ratings.jsonl tail + refine-log
    tail into a markdown section keyed by the chapter name. Truncates
    each artifact to ``max_chars_each`` (tail-biased, so the most recent
    content survives)."""
    import json

    state_path = workdir / "marathon-refine-state.json"
    if not state_path.is_file():
        return ""
    try:
        state = json.loads(state_path.read_text())
    except (OSError, ValueError):
        return ""
    target = state.get("target_folder") or ""
    chap_label = Path(target).name if target else workdir.name

    parts = [f"### {chap_label}"]
    parts.append(
        f"- status: {state.get('status')!r}, "
        f"iterations: {state.get('iterations_completed')}/"
        f"{state.get('current_iteration_idx')}"
    )

    marathon_md = workdir / "marathon.md"
    if marathon_md.is_file():
        try:
            text = marathon_md.read_text()
            if len(text) > max_chars_each:
                text = "... (earlier marathon.md content trimmed)\n" + text[-max_chars_each:]
            parts.append("#### marathon.md")
            parts.append(text)
        except OSError:
            pass

    ratings = workdir / "marathon-ratings.jsonl"
    if ratings.is_file():
        try:
            lines = [l for l in ratings.read_text().splitlines() if l.strip()]
            # Keep all rating entries — each is one line of JSON, parsed below.
            entries = []
            for line in lines:
                try:
                    d = json.loads(line)
                    r = d.get("rating") or {}
                    iter_n = d.get("iteration")
                    scores = (
                        f"q={r.get('quality')} m={r.get('math_correctness')} "
                        f"g={r.get('generality')} api={r.get('api_coverage')} "
                        f"con={r.get('concision')} l4={r.get('modern_lean4')} "
                        f"struct={r.get('structural_focus')}"
                    )
                    notes = r.get("notes") or ""
                    entries.append(f"- iter {iter_n}: {scores}\n  notes: {notes}")
                except (ValueError, AttributeError):
                    continue
            if entries:
                parts.append("#### Rater diagnoses (per iteration)")
                joined = "\n".join(entries)
                if len(joined) > max_chars_each:
                    joined = joined[:max_chars_each] + "\n... (trimmed)"
                parts.append(joined)
        except OSError:
            pass

    refine_log = workdir / "marathon-refine-log.md"
    if refine_log.is_file():
        try:
            text = refine_log.read_text()
            if len(text) > max_chars_each:
                text = "... (earlier refine-log content trimmed)\n" + text[-max_chars_each:]
            parts.append("#### Hermes' drafted prompts (refine-log.md)")
            parts.append(text)
        except OSError:
            pass

    return "\n\n".join(parts)


def _gather_workdir_context(workdirs_parent: Optional[Path]) -> str:
    """Aggregate per-chapter artifacts from every subdir of
    ``workdirs_parent`` that looks like a marathon refine workdir.
    Returns a markdown block or empty string."""
    if workdirs_parent is None or not workdirs_parent.is_dir():
        return ""
    blocks = []
    for entry in sorted(workdirs_parent.iterdir()):
        if not entry.is_dir():
            continue
        if not (entry / "marathon-refine-state.json").is_file():
            continue
        block = _read_chapter_artifacts(entry)
        if block:
            blocks.append(block)
    return "\n\n---\n\n".join(blocks)


def _read_repo_lean(repo_dir: Path, max_chars: int = 400_000) -> str:
    """Read every Lean file under repo_dir (gitignore-filtered).
    Truncates aggregate output at ``max_chars``."""
    result = subprocess.run(
        ["git", "ls-files", "--cached", "--others", "--exclude-standard", "-z"],
        cwd=str(repo_dir),
        capture_output=True,
        check=False,
    )
    if result.returncode != 0:
        return ""
    parts: list[str] = []
    total = 0
    for path_bytes in result.stdout.split(b"\0"):
        if not path_bytes:
            continue
        rel = path_bytes.decode("utf-8")
        full = repo_dir / rel
        if not full.is_file() or full.suffix != ".lean":
            continue
        try:
            content = full.read_text()
        except OSError:
            continue
        section = f"=== FILE: {rel} ===\n{content}"
        if total + len(section) > max_chars:
            parts.append(f"... ({max_chars - total} more chars of Lean files trimmed)")
            break
        parts.append(section)
        total += len(section)
    return "\n\n".join(parts)


def _read_rubrics(marathon_pkg: Path) -> str:
    """Return the contents of the two reviewer rubrics for the referee
    agent to deduplicate against."""
    prompts = marathon_pkg / "prompts"
    parts = []
    for name in ("review_skeleton.md", "review.md"):
        p = prompts / name
        if p.is_file():
            parts.append(f"=== {name} ===\n{p.read_text()}")
    return "\n\n".join(parts)


def _read_git_log(repo_dir: Path, limit: int = 40) -> str:
    """Recent git log (one-line) for context on what landed when."""
    proc = subprocess.run(
        ["git", "log", "--oneline", f"-{limit}"],
        cwd=str(repo_dir),
        capture_output=True,
        text=True,
        check=False,
    )
    return proc.stdout.strip() if proc.returncode == 0 else ""


def _invoke_claude_referee(prompt: str) -> tuple[bool, str]:
    """Call the claude CLI synchronously. Returns (ok, response_or_error)."""
    claude_path = shutil.which("claude")
    if not claude_path:
        return False, "claude (Claude Code CLI) not on PATH"

    env = os.environ.copy()
    env.pop("ANTHROPIC_API_KEY", None)  # use Max OAuth

    try:
        proc = subprocess.run(
            [
                claude_path,
                "-p", prompt,
                "--model", REFEREE_MODEL,
                "--tools", "",
                "--output-format", "text",
            ],
            capture_output=True,
            text=True,
            check=False,
            env=env,
        )
    except OSError as e:
        return False, f"could not exec claude (errno {e.errno}: {e.strerror})"

    if proc.returncode != 0:
        err = ((proc.stderr or proc.stdout) or "").strip()[:500]
        return False, f"claude exited {proc.returncode}: {err}"

    response = (proc.stdout or "").strip()
    if not response:
        return False, "claude returned empty stdout"

    return True, response


def update_referee(
    repo_dir: Path,
    referee_path: Path,
    workdirs_parent: Optional[Path] = None,
    auto_commit: bool = True,
    auto_push: bool = False,
    write_to_proposed_only: bool = False,
) -> RefereeResult:
    """Run one referee agent pass to refresh the machine-managed tail of
    ``referee_path``.

    Returns a :class:`RefereeResult` describing what happened. Always
    returns; never raises for ordinary failures (so the auto-referee
    hook in refine doesn't abort a batch over a referee hiccup).
    """
    if not repo_dir.is_dir():
        return RefereeResult(error=f"repo_dir not a directory: {repo_dir}")

    marathon_pkg = Path(__file__).parent

    # 1. Read existing referee.md (or start with empty).
    if referee_path.is_file():
        existing = referee_path.read_text()
        user_header, existing_machine_tail = _split_referee(existing)
    else:
        existing = ""
        user_header = ""
        existing_machine_tail = None

    # 2. Gather context.
    repo_lean = _read_repo_lean(repo_dir)
    rubrics = _read_rubrics(marathon_pkg)
    workdir_ctx = _gather_workdir_context(workdirs_parent)
    git_log = _read_git_log(repo_dir)

    # 3. Read system prompt.
    system_prompt_path = marathon_pkg / "prompts" / "referee_agent.md"
    if not system_prompt_path.is_file():
        return RefereeResult(error=f"referee_agent.md missing at {system_prompt_path}")
    system_prompt = system_prompt_path.read_text()

    # 4. Assemble user message.
    sections = [f"# Referee agent system prompt\n\n{system_prompt}"]
    sections.append(
        "# Current referee.md\n\n"
        "## User-managed header (do not touch)\n\n"
        + (user_header or "(empty)")
        + "\n\n## Existing machine-managed tail\n\n"
        + (existing_machine_tail or "(empty — first referee pass, propose a fresh tail)")
    )
    sections.append(f"# Generic reviewer rubrics (do not duplicate these in referee output)\n\n{rubrics}")
    if workdir_ctx:
        sections.append(f"# Per-chapter workdir artifacts (marathon.md, ratings, refine-log)\n\n{workdir_ctx}")
    if git_log:
        sections.append(f"# Recent git log (top 40 commits)\n\n{git_log}")
    sections.append(f"# Repo Lean files (current state)\n\n{repo_lean}")
    sections.append(
        "Emit ONLY the new machine-managed tail of referee.md, following the rules above."
    )
    prompt = "\n\n---\n\n".join(sections)

    print(f"  referee: invoking Claude (prompt size: {len(prompt):,} chars)")

    # 5. Invoke Claude.
    ok, response = _invoke_claude_referee(prompt)
    if not ok:
        return RefereeResult(error=response)

    new_machine_tail = response.strip()
    if not new_machine_tail:
        return RefereeResult(error="agent returned empty machine tail")

    # 6. Validate output isn't accidentally embedding sentinels.
    if BEGIN_SENTINEL in new_machine_tail or END_SENTINEL in new_machine_tail:
        return RefereeResult(error="agent embedded sentinels in its output; refusing to write")

    # 7. Assemble new referee.md content.
    new_text = _assemble_referee(user_header, new_machine_tail)

    # 8. Decide where to write.
    output_path = referee_path
    if write_to_proposed_only:
        output_path = referee_path.with_suffix(referee_path.suffix + REFEREE_PROPOSED_SUFFIX)

    # 9. Compute a tiny diff summary if we have a prior version.
    diff_summary: Optional[str] = None
    if existing_machine_tail is not None and not write_to_proposed_only:
        old_lines = existing_machine_tail.splitlines()
        new_lines = new_machine_tail.splitlines()
        diff_summary = f"machine tail: {len(old_lines)} → {len(new_lines)} lines"

    # 10. Write.
    try:
        output_path.write_text(new_text)
    except OSError as e:
        return RefereeResult(error=f"could not write {output_path}: {e}")

    # 11. Optional commit.
    commit_sha: Optional[str] = None
    if auto_commit and not write_to_proposed_only:
        commit_sha = _commit_referee(repo_dir, output_path)

    # 12. Optional push (only when we actually landed a commit).
    pushed: Optional[bool] = None
    push_message: Optional[str] = None
    if auto_push and commit_sha is not None:
        from marathon.post_pipeline import run_git_push
        pushed, push_message = run_git_push(repo_dir)

    return RefereeResult(
        ok=True,
        output_path=output_path,
        commit_sha=commit_sha,
        pushed=pushed,
        push_message=push_message,
        diff_summary=diff_summary,
        machine_tail_len=len(new_machine_tail.splitlines()),
    )


def _commit_referee(repo_dir: Path, output_path: Path) -> Optional[str]:
    """Stage ``output_path`` and commit. Returns the new HEAD sha (short)
    on success, None on failure / nothing to commit."""
    try:
        rel = output_path.relative_to(repo_dir)
    except ValueError:
        print(f"  referee: skipping commit — {output_path} not under {repo_dir}")
        return None

    add_proc = subprocess.run(
        ["git", "add", "--", str(rel)],
        cwd=str(repo_dir),
        capture_output=True,
        text=True,
        check=False,
    )
    if add_proc.returncode != 0:
        print(f"  referee: git add failed — {(add_proc.stderr or '').strip()}")
        return None

    diff_check = subprocess.run(
        ["git", "diff", "--cached", "--quiet"],
        cwd=str(repo_dir),
        capture_output=True,
        check=False,
    )
    if diff_check.returncode == 0:
        # nothing staged — referee said the same thing
        print("  referee: no change vs HEAD; skipping commit")
        return None

    message = (
        "referee: refresh machine-managed tail\n\n"
        "Auto-update by `marathon referee` based on current repo state,\n"
        "per-chapter rater notes, and marathon.md design log.\n\n"
        + REFEREE_COAUTHOR_TRAILER
    )
    commit_proc = subprocess.run(
        ["git", "commit", "-m", message],
        cwd=str(repo_dir),
        capture_output=True,
        text=True,
        check=False,
    )
    if commit_proc.returncode != 0:
        print(f"  referee: git commit failed — {(commit_proc.stderr or '').strip()}")
        return None

    sha_proc = subprocess.run(
        ["git", "rev-parse", "--short", "HEAD"],
        cwd=str(repo_dir),
        capture_output=True,
        text=True,
        check=False,
    )
    return sha_proc.stdout.strip() or None


def referee_command(args) -> None:
    """Entry point for ``marathon referee``."""
    repo_dir: Path = args.repo_dir.resolve()
    if not repo_dir.is_dir():
        sys.exit(f"--repo-dir not found: {repo_dir}")
    if not (repo_dir / ".git").exists():
        sys.exit(f"--repo-dir is not a git repo: {repo_dir}")

    referee_path: Path = (args.referee or (repo_dir / REFEREE_FILENAME)).resolve()
    if referee_path.is_dir():
        sys.exit(f"--referee path is a directory: {referee_path}")

    workdirs_parent: Optional[Path] = None
    if args.workdirs_parent is not None:
        workdirs_parent = args.workdirs_parent.resolve()
        if not workdirs_parent.is_dir():
            sys.exit(f"--workdirs-parent not a directory: {workdirs_parent}")

    auto_commit = not (args.review or args.no_commit)
    auto_push = bool(args.push) and auto_commit

    mode_str = (
        "REVIEW (write to .proposed only)" if args.review
        else ("WRITE (no commit)" if args.no_commit
              else ("WRITE + auto-commit + auto-push" if auto_push
                    else "WRITE + auto-commit"))
    )

    print(f"repo dir:           {repo_dir}")
    print(f"referee path:       {referee_path}")
    if workdirs_parent is not None:
        print(f"workdirs parent:    {workdirs_parent}")
    print(f"mode:               {mode_str}")

    result = update_referee(
        repo_dir=repo_dir,
        referee_path=referee_path,
        workdirs_parent=workdirs_parent,
        auto_commit=auto_commit,
        auto_push=auto_push,
        write_to_proposed_only=args.review,
    )

    if result.error:
        print(f"\nreferee: ERROR — {result.error}")
        sys.exit(1)
    if not result.ok:
        print(f"\nreferee: did not run — {result.skipped_reason or 'unknown reason'}")
        sys.exit(2)

    print(f"\nreferee: wrote {result.output_path}")
    if result.diff_summary:
        print(f"  delta: {result.diff_summary}")
    if result.machine_tail_len is not None:
        print(f"  new machine tail: {result.machine_tail_len} lines")
    if result.commit_sha:
        print(f"  commit: {result.commit_sha}")
    if result.pushed is True:
        print(f"  push: ok ({result.push_message})")
    elif result.pushed is False:
        print(f"  push: failed — {result.push_message}")
