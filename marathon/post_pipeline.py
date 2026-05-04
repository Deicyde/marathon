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
    auto_rate: bool = False
    build_timeout: int = 600
    ratings_path: Optional[Path] = None

    def has_any(self) -> bool:
        return self.auto_build or self.auto_commit or self.auto_rate


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
    modern_lean4: Optional[int] = None
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
    out_tail = (proc.stdout or "")[-1500:]
    err_tail = (proc.stderr or "")[-1500:]
    log_tail = (out_tail + ("\n---\n" + err_tail if err_tail.strip() else "")).strip()
    return BuildResult(
        ok=proc.returncode == 0,
        duration_seconds=elapsed,
        log_tail=log_tail or None,
    )


def run_git_commit(
    repo_dir: Path,
    target_path: Path,
    message: str,
) -> CommitResult:
    """Stage just ``target_path`` and commit. Skips silently if the index
    is busy or there's nothing to commit."""
    try:
        rel = target_path.relative_to(repo_dir)
    except ValueError:
        return CommitResult(skipped_reason=f"{target_path} not under repo {repo_dir}")

    add_proc = subprocess.run(
        ["git", "add", str(rel)],
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

    commit_proc = subprocess.run(
        ["git", "commit", "-m", message],
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


def call_claude_rater(
    target_path: Path,
    build_result: Optional[BuildResult],
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

    parts = [rubric]
    if build_result is not None and build_result.ok is not None:
        status = "PASS" if build_result.ok else ("TIMED OUT" if build_result.timed_out else "FAIL")
        parts.append(f"## Build status\n\n{status}")
    parts.append(f"## Code under review\n\n{code}")
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

    try:
        json_text = _extract_json_object(response)
        data = json.loads(json_text)
    except (json.JSONDecodeError, ValueError) as e:
        return RatingResult(parse_error=f"could not parse JSON: {e}; raw: {response[:300]}")

    return RatingResult(
        quality=_coerce_int(data.get("quality")),
        math_correctness=_coerce_int(data.get("math_correctness")),
        generality=_coerce_int(data.get("generality")),
        api_coverage=_coerce_int(data.get("api_coverage")),
        modern_lean4=_coerce_int(data.get("modern_lean4")),
        notes=data.get("notes") if isinstance(data.get("notes"), str) else None,
    )


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
        msg_parts = [f"marathon: {chapter_label}"]
        if iteration is not None:
            msg_parts.append(f"iteration {iteration}")
        if out["build"] is not None and out["build"].ok is not None:
            tag = "OK" if out["build"].ok else ("TIMEOUT" if out["build"].timed_out else "FAIL")
            msg_parts.append(f"[build:{tag}]")
        if project_id:
            msg_parts.append(f"(project={project_id[:8]})")
        message = " ".join(msg_parts)
        c = run_git_commit(repo_dir, target_path, message)
        out["commit"] = c
        if c.sha:
            print(f"  commit: {c.sha}  message=\"{message}\"")
        else:
            print(f"  commit: skipped — {c.skipped_reason}")

    if config.auto_rate:
        r = call_claude_rater(target_path, out["build"])
        out["rating"] = r
        if r.parse_error:
            print(f"  rating: parse error — {r.parse_error}")
        else:
            print(
                f"  rating: q={r.quality} m={r.math_correctness} g={r.generality} "
                f"api={r.api_coverage} lean4={r.modern_lean4}"
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
