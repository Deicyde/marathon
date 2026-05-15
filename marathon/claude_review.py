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
    max_prompt_words: Optional[int] = None,
    attempt_idx: int = 0,
    max_retries: int = 0,
    previous_status: Optional[str] = None,
    referee_md: Optional[str] = None,
    previous_rating_note: Optional[str] = None,
    cross_chapter_md: Optional[str] = None,
    continuation_mode: bool = False,
    previous_output_summary: Optional[str] = None,
) -> str:
    """Call Claude Code. Return the response text (sent verbatim to Aristotle).

    On retry attempts (``attempt_idx > 0``), the user message includes a
    "Continuation context" section telling Claude the previous attempt's
    status so it can write a freshly-targeted prompt.

    When ``continuation_mode=True``, the prompt is instead framed as a
    **session continuation**: Marathon will dispatch the result via
    ``project.ask(...)`` to the same Aristotle session that ran the prior
    task, so Hermes should write a SHORT, surgical continuation prompt
    rather than an ab-initio instruction. Aristotle already knows what
    it did; the rubric's "no preamble, no rubric recap" guidance is
    extra important here. ``previous_output_summary`` (Aristotle's own
    description of what its prior task accomplished) is folded into the
    context when supplied.
    """
    claude_path = _ensure_claude_cli()
    system_prompt = _read_review_prompt(skeleton_mode)

    if max_prompt_words is not None:
        system_prompt = (
            system_prompt
            + "\n\n## Length constraint\n\n"
            + f"Keep your response to {max_prompt_words} words or fewer. Cut "
              "redundant phrasing, multi-paragraph asides, and prose Aristotle "
              "can infer from context. Prefer short bullets over paragraphs. "
              "Do not include code blocks longer than what's strictly necessary "
              "to communicate a fix; reference declarations by name and trust "
              "Aristotle to fill in mechanics it already knows."
        )

    target_content = _read_lean_files(target_folder)
    if not target_content:
        sys.exit(
            f"target folder {target_folder} contains no .lean files; "
            "nothing to refine."
        )
    repo_context = _read_repo_lean_context(repo_dir, target_folder)

    sections: list[str] = ["# Reviewer rubric (your role and priorities)\n\n" + system_prompt]
    if referee_md:
        sections.append(
            "# Project-specific reviewer notes (referee.md)\n\n"
            "These notes were written by an outside reviewer (human or AI) to "
            "course-correct your reviews on this project. Treat them as a layer "
            "of project-specific priorities **on top of** the rubric above. If "
            "the rubric and these notes conflict, the rubric wins on output "
            "style (second person, no preamble, specific replacements); these "
            "notes win on what to look at and how hard to push.\n\n"
            + referee_md
        )
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
    if cross_chapter_md:
        sections.append(
            "# Cross-chapter context (sibling chapters in this batch)\n\n"
            "These are other chapters being refined in the same batch as the "
            "target. Each block shows that chapter's latest design notes "
            "(from its `marathon.md`) and its most recent auto-rater "
            "diagnosis. Use this to coordinate cross-chapter structural "
            "decisions: if a sibling already exposes a canonical predicate "
            "or scaffolding lemma, demand the target reuse it; if a "
            "sibling's rater flagged an item that the target chapter is "
            "the canonical home for (e.g. a predicate spelled inline that "
            "lives in this chapter's namespace), demand the fix here. "
            "Treat this as authoritative on what other chapters claim to "
            "expose; verify against the bundled Lean code before "
            "demanding code changes.\n\n"
            + cross_chapter_md
        )
    if previous_rating_note:
        sections.append(
            "# Previous iteration's auto-rater diagnosis\n\n"
            "An independent reviewer scored the previous iteration's diff "
            "across quality / math correctness / generality / api coverage / "
            "modern Lean 4 / structural focus. Its note enumerates the "
            "structural and cosmetic moves it observed, plus the referee "
            "items it judged still on the table. Use this to: (a) avoid "
            "regressions the rater flagged, (b) re-flag referee items the "
            "rater says weren't addressed, (c) calibrate whether your prior "
            "drafted prompt actually translated into structural change "
            "(low `structural_focus` means your last prompt didn't land "
            "structurally — open this iteration with something heavier).\n\n"
            + previous_rating_note
        )
    if continuation_mode:
        ctx = (
            "# Continuation mode (session preserved)\n\n"
            f"The previous Aristotle task ended with status "
            f"`{previous_status or 'unknown'}` (Aristotle's UI labels this "
            "\"Review Suggested\" / \"Out of Budget\"). Marathon will send your "
            "drafted prompt to the **same Aristotle session** via "
            "`project.ask(...)` — Aristotle keeps its sandbox, file context, "
            "and reasoning state intact. **Do NOT re-explain the task from "
            "scratch.** Aristotle already knows what it was doing; you are "
            "giving it a short, surgical nudge to refine or extend its "
            "partial output.\n\n"
            "Concrete guidance for continuation prompts:\n"
            "* Lead with the gap you want closed (one sentence): "
            "\"Please now also handle X.\" / \"Please replace the placeholder "
            "in Foo with the proper definition.\"\n"
            "* If Aristotle's `output_summary` claims something is done that "
            "the target-folder code shows is NOT done, point at the specific "
            "file and declaration.\n"
            "* If the rater (above) flagged structural issues the partial "
            "output didn't address, demand them in this continuation rather "
            "than waiting for the next iteration.\n"
            "* Keep the prompt to roughly 100–300 words. Continuation prompts "
            "should be much shorter than fresh-task prompts."
        )
        if previous_output_summary:
            ctx += (
                "\n\n## Aristotle's own summary of what its prior task did\n\n"
                "(Verbatim from the previous task's `output_summary` field. "
                "Treat as Aristotle's claim about what it accomplished; "
                "verify against the target folder code before believing it.)\n\n"
                + previous_output_summary
            )
        sections.append(ctx)
    elif attempt_idx > 0:
        sections.append(
            "# Continuation context\n\n"
            f"This is **retry attempt {attempt_idx}** within iteration "
            f"{iteration_idx} (up to {max_retries} retries per iteration). The "
            f"previous attempt ended with status `{previous_status or 'unknown'}`. "
            "If that attempt produced partial output, the target folder above "
            "now reflects that — review the **current** state of the code (not "
            "what you remember asking for) and write a freshly-targeted prompt "
            "to push Aristotle further from where it stopped. Be more specific "
            "about what wasn't done; Aristotle has another shot."
        )
    sections.append(
        f"This is iteration {iteration_idx} of up to {max_iterations}, "
        f"attempt {attempt_idx + 1} of up to {max_retries + 1}"
        + (" (session continuation)." if continuation_mode else ".")
        + " Write the prompt for Aristotle now."
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
