"""Claude API wrapper for the ``marathon refine`` command.

One Claude call per refinement iteration: hand Claude the target folder,
the rest of the repo's Lean files, ``marathon.md``, and the past refinement
log; receive back a prompt for Aristotle.

**Claude is never given LaTeX files.** ``.tex`` content under the repo is
filtered out before assembling the prompt; the ``--tex`` file the user
provides on the command line goes straight into the Aristotle bundle and
never enters this module.
"""

import os
import subprocess
import sys
from pathlib import Path
from typing import Optional

import anthropic
from anthropic import APIError

CLAUDE_MODEL = "claude-opus-4-7"
CLAUDE_MAX_TOKENS = 32000


def _ensure_claude_key() -> None:
    if not os.environ.get("ANTHROPIC_API_KEY"):
        sys.exit(
            "ANTHROPIC_API_KEY not set. Add `export ANTHROPIC_API_KEY=sk-ant-...` "
            "to ~/.zshrc and re-source. (Distinct from ARISTOTLE_API_KEY.)"
        )


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
    """Call Claude. Return the response text (sent verbatim to Aristotle)."""
    _ensure_claude_key()
    system_prompt = _read_review_prompt(skeleton_mode)

    target_content = _read_lean_files(target_folder)
    if not target_content:
        sys.exit(
            f"target folder {target_folder} contains no .lean files; "
            "nothing to refine."
        )
    repo_context = _read_repo_lean_context(repo_dir, target_folder)

    user_blocks: list[dict] = [
        {
            "type": "text",
            "text": (
                "# Repo context (Lean files outside the target folder)\n\n"
                + (repo_context or "(none)")
            ),
            "cache_control": {"type": "ephemeral"},
        },
    ]
    if marathon_md:
        user_blocks.append({
            "type": "text",
            "text": f"# marathon.md (project notebook)\n\n{marathon_md}",
        })
    user_blocks.append({
        "type": "text",
        "text": (
            f"# Target folder (`{target_folder}`) — current state\n\n"
            f"{target_content}\n\n"
            f"# Past refinement log\n\n{refine_log or '(no prior iterations)'}\n\n"
            f"This is iteration {iteration_idx} of up to {max_iterations}. "
            "Write the prompt for Aristotle now."
        ),
    })

    client = anthropic.Anthropic()
    try:
        with client.messages.stream(
            model=CLAUDE_MODEL,
            max_tokens=CLAUDE_MAX_TOKENS,
            thinking={"type": "adaptive"},
            output_config={"effort": "xhigh"},
            system=[{
                "type": "text",
                "text": system_prompt,
                "cache_control": {"type": "ephemeral"},
            }],
            messages=[{"role": "user", "content": user_blocks}],
        ) as stream:
            message = stream.get_final_message()
    except APIError as e:
        sys.exit(f"Claude API error (status {getattr(e, 'status_code', '?')}): {e}")

    text_parts: list[str] = []
    for block in message.content:
        if block.type == "text":
            text_parts.append(block.text)
    response = "\n".join(text_parts).strip()
    if not response:
        sys.exit("Claude returned no text content.")

    cache_read = getattr(message.usage, "cache_read_input_tokens", 0) or 0
    cache_create = getattr(message.usage, "cache_creation_input_tokens", 0) or 0
    print(
        f"  Claude usage: input={message.usage.input_tokens} "
        f"(cache read {cache_read}, write {cache_create}), "
        f"output={message.usage.output_tokens}"
    )
    return response
