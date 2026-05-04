"""Claude Code subprocess wrapper for the ``marathon refine`` command.

Invokes the ``claude`` CLI (Claude Code) as a subprocess for each
refinement iteration, instead of calling the Anthropic API directly. This
lets users with a Claude Max subscription pay for refine via their existing
subscription rather than prepaid API credits.

**Trade-offs vs the API path:**

- Authentication: uses the user's existing ``claude`` login (Max OAuth via
  keychain). No ``ANTHROPIC_API_KEY`` env var needed if the user has signed
  in interactively.
- Prompt caching: not exposed via the Claude Code CLI, so every call pays
  full token cost. Free for Max users (flat-rate); would matter if you
  flipped back to API billing.
- Multi-block prompt structure: the CLI takes one prompt argument. We
  combine the reviewer rubric and the per-iteration context into a single
  string with markdown headers. The semantic distinction between system
  prompt and user prompt is lost but doesn't materially affect output.
- Tool surface: disabled via ``--tools ""`` and ``--bare`` so the agent loop
  can't fire — we want a single completion.

**Claude is never given LaTeX files.** ``.tex`` content under the repo is
filtered out before assembling the prompt; the ``--tex`` file the user
provides on the command line goes straight into the Aristotle bundle and
never enters this module.
"""

import os
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Optional

CLAUDE_MODEL = "claude-opus-4-7"


def _ensure_claude_cli() -> str:
    path = shutil.which("claude")
    if not path:
        sys.exit(
            "claude (Claude Code CLI) not found on PATH. Install via "
            "https://claude.com/product/claude-code/, then run `claude` once "
            "interactively to authenticate with your Max subscription. "
            "(The `marathon refine` command shells out to `claude` so it can "
            "use your Max subscription instead of pay-per-token API credits.)"
        )
    return path


def _read_review_prompt(skeleton_mode: bool) -> str:
    name = "review_skeleton.md" if skeleton_mode else "review.md"
    path = Path(__file__).parent / "prompts" / name
    if not path.is_file():
        sys.exit(f"review prompt template missing: {path}")
    return path.read_text()


def _read_lean_files(folder: Path) -> str:
    parts: list[str] = []
    for lean_file in sorted(folder.rglob("*.lean")):
        rel = lean_file.relative_to(folder)
        parts.append(f"=== FILE: {rel} ===\n{lean_file.read_text()}")
    return "\n\n".join(parts)


def _read_repo_lean_context(repo_dir: Path, exclude_folder: Path) -> str:
    """Lean files in repo (gitignore-filtered), excluding everything under
    ``exclude_folder``. ``.tex`` and other non-Lean files are excluded."""
    result = subprocess.run(
        ["git", "ls-files", "--cached", "--others", "--exclude-standard", "-z"],
        cwd=str(repo_dir),
        capture_output=True,
        check=True,
    )
    parts: list[str] = []
    excl_resolved = exclude_folder.resolve()
    for path_bytes in result.stdout.split(b"\0"):
        if not path_bytes:
            continue
        rel = path_bytes.decode("utf-8")
        full = (repo_dir / rel).resolve()
        if not full.is_file() or full.suffix != ".lean":
            continue
        try:
            full.relative_to(excl_resolved)
            continue  # under target folder; skip
        except ValueError:
            pass  # not under target; include
        parts.append(f"=== FILE: {rel} ===\n{full.read_text()}")
    return "\n\n".join(parts)


def review_and_draft_prompt(
    target_folder: Path,
    repo_dir: Path,
    marathon_md: Optional[str],
    refine_log: str,
    iteration_idx: int,
    max_iterations: int,
    skeleton_mode: bool = False,
) -> str:
    """Call Claude Code. Return the response text (sent verbatim to Aristotle)."""
    claude_path = _ensure_claude_cli()
    system_prompt = _read_review_prompt(skeleton_mode)

    target_content = _read_lean_files(target_folder)
    if not target_content:
        sys.exit(
            f"target folder {target_folder} contains no .lean files; "
            "nothing to refine."
        )
    repo_context = _read_repo_lean_context(repo_dir, target_folder)

    sections: list[str] = ["# Reviewer rubric (your role and priorities)\n\n" + system_prompt]
    sections.append(
        "# Repo context (Lean files outside the target folder)\n\n"
        + (repo_context or "(none)")
    )
    if marathon_md:
        sections.append(f"# marathon.md (project notebook)\n\n{marathon_md}")
    sections.append(
        f"# Target folder (`{target_folder}`) — current state\n\n{target_content}"
    )
    sections.append(
        f"# Past refinement log\n\n{refine_log or '(no prior iterations)'}"
    )
    sections.append(
        f"This is iteration {iteration_idx} of up to {max_iterations}. "
        "Write the prompt for Aristotle now."
    )
    combined = "\n\n---\n\n".join(sections)

    # Note: we previously passed `--bare` here for "skip auto-discovery /
    # CI-friendly" behavior, but it also skips reading the keychain OAuth
    # token, which broke Max auth. `--tools ""` still disables the agent
    # tool surface; we accept the slight risk of cwd-local .claude/ files
    # affecting the call.
    cmd = [
        claude_path,
        "-p", combined,
        "--model", CLAUDE_MODEL,
        "--tools", "",
        "--output-format", "text",
    ]

    print(
        f"  invoking Claude Code via subprocess (prompt size: "
        f"{len(combined):,} chars)"
    )

    # Scrub ANTHROPIC_API_KEY from the subprocess env so claude falls back
    # to its keychain-stored Max OAuth instead of routing through the API
    # billing path. Users who actually want the API can edit this module to
    # remove the scrub or pass env=os.environ.
    env = os.environ.copy()
    env.pop("ANTHROPIC_API_KEY", None)

    try:
        proc = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            check=False,
            env=env,
        )
    except OSError as e:
        sys.exit(
            f"could not exec claude (errno {e.errno}: {e.strerror}). "
            f"If errno is 7 (E2BIG), the combined prompt exceeded the OS argv "
            f"limit ({len(combined):,} chars); split the refinement target or "
            "fall back to the API path."
        )

    if proc.returncode != 0:
        err = (proc.stderr or proc.stdout or "").strip() or "(no output)"
        sys.exit(f"claude exited with code {proc.returncode}:\n{err}")

    response = proc.stdout.strip()
    if not response:
        sys.exit("claude returned empty stdout.")

    return response
