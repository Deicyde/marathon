"""Marathon CLI entry point. Run as ``python -m marathon ...``."""

import argparse
import asyncio
import sys
from pathlib import Path

from marathon.refine import refine_command
from marathon.skeleton import skeleton_command


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="marathon",
        description=(
            "Driver for the Aristotle (Harmonic) automated theorem proving API. "
            "Submits chapters of a textbook in user-specified dependency order, "
            "bundling each submission with the Lean outlines produced for prior chapters."
        ),
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    p_skel = subparsers.add_parser(
        "skeleton",
        help="Submit each chapter in order and download skeletal Lean outlines.",
        description=(
            "For every line of <folder>/order.txt, submit the named .tex file to "
            "Aristotle bundled with the entire --repo-dir Lean project (filtered "
            "by .gitignore), macros.sty, and marathon.md. Outputs land at "
            "<repo-dir>/<output-path-from-order.txt>/. Progress is checkpointed "
            "in <folder>/marathon-state.json."
        ),
    )
    p_skel.add_argument(
        "folder",
        type=Path,
        help="Path to the input folder containing order.txt and the .tex files.",
    )
    p_skel.add_argument(
        "--repo-dir",
        type=Path,
        required=True,
        metavar="PATH",
        help=(
            "Path to the Lean project repo (must be a git repo). Its tracked + "
            "untracked-not-gitignored contents are bundled into every submission."
        ),
    )
    p_skel.add_argument(
        "--output-base",
        type=str,
        required=True,
        metavar="REL_PATH",
        help=(
            "Relative path within --repo-dir where chapter outputs land. "
            "Each chapter's output folder (right column of order.txt) is appended. "
            "Example: 'GeometricAnalysis/LeeSM' -> chapter outputs at "
            "<repo-dir>/GeometricAnalysis/LeeSM/<chapter-folder>/."
        ),
    )
    p_skel.add_argument(
        "--continue-on-error",
        action="store_true",
        help="Continue with the next chapter even if one fails (default: abort).",
    )
    p_skel.add_argument(
        "--polling-interval",
        type=int,
        default=60,
        metavar="SECONDS",
        help="Seconds between Aristotle status checks (default: 60).",
    )
    p_skel.add_argument(
        "--max-retries",
        type=int,
        default=2,
        metavar="N",
        help=(
            "On COMPLETE_WITH_ERRORS or FAILED, retry the chapter up to N additional "
            "times (so up to N+1 total attempts). Each retry submits a fresh project, "
            "tells Aristotle it's a continuation, and bundles any partial output from "
            "the previous attempt as context. OUT_OF_BUDGET and CANCELED are not "
            "retried. Default: 2."
        ),
    )
    _add_pipeline_flags(p_skel)

    p_refine = subparsers.add_parser(
        "refine",
        help="Iteratively improve an existing Lean folder with Claude+Aristotle.",
        description=(
            "For up to --max-iterations rounds: Claude reviews the target Lean "
            "folder (plus the rest of the repo and the past refinement log) "
            "and writes a prompt for Aristotle. Marathon submits, retries, "
            "extracts the response back into the target folder in place, "
            "and loops. Claude is never given .tex files; the optional --tex "
            "file is bundled only with the Aristotle submission."
        ),
    )
    p_refine.add_argument(
        "target",
        type=Path,
        help="Path to the Lean folder to refine. Must be inside --repo-dir.",
    )
    p_refine.add_argument(
        "--repo-dir",
        type=Path,
        required=True,
        metavar="PATH",
        help=(
            "Path to the Lean project repo (must be a git repo). Its tracked + "
            "untracked-not-gitignored contents are bundled into every submission."
        ),
    )
    p_refine.add_argument(
        "--tex",
        type=Path,
        default=None,
        metavar="FILE",
        help=(
            "Optional .tex reference file the user supplies for Aristotle. "
            "Bundled at the top level of every Aristotle submission. Claude "
            "is never given its contents."
        ),
    )
    p_refine.add_argument(
        "--referee",
        type=Path,
        default=None,
        metavar="FILE",
        help=(
            "Path to a markdown file with project-specific reviewer notes "
            "that Claude should layer on top of its rubric. If this flag "
            "is omitted, Marathon auto-detects <repo-dir>/referee.md. The "
            "file is given only to Claude (the reviewer); it is excluded "
            "from the Aristotle bundle."
        ),
    )
    p_refine.add_argument(
        "--workdir",
        type=Path,
        default=None,
        metavar="DIR",
        help=(
            "Where Marathon writes marathon-refine-state.json and "
            "marathon-refine-log.md, and reads marathon.md from if present. "
            "Defaults to the current working directory."
        ),
    )
    p_refine.add_argument(
        "--max-iterations",
        type=int,
        default=3,
        metavar="N",
        help=(
            "Maximum number of refinement iterations (Claude review + Aristotle "
            "submit). Default: 3. Each iteration costs one Claude call plus "
            "one or more Aristotle submissions."
        ),
    )
    p_refine.add_argument(
        "--max-retries",
        type=int,
        default=2,
        metavar="N",
        help=(
            "Per-iteration: on COMPLETE_WITH_ERRORS or FAILED, retry the "
            "Aristotle submission up to N additional times. Default: 2."
        ),
    )
    p_refine.add_argument(
        "--polling-interval",
        type=int,
        default=60,
        metavar="SECONDS",
        help="Seconds between Aristotle status checks (default: 60).",
    )
    p_refine.add_argument(
        "--skeleton",
        action="store_true",
        help=(
            "Skeleton mode: instruct Aristotle to keep every theorem/lemma/"
            "proposition/corollary body as `sorry` (no proofs), and switch "
            "Claude's reviewer rubric to focus on signature/definition "
            "correctness, future-proofness, and idiomatic Mathlib style. "
            "Use this to iterate on the scaffold quality before filling in "
            "any proofs."
        ),
    )
    p_refine.add_argument(
        "--max-prompt-words",
        type=int,
        default=None,
        metavar="N",
        help=(
            "Constrain Claude's drafted Aristotle prompt to roughly N words. "
            "Tells Claude to cut redundant prose, prefer short bullets, and "
            "trim long code blocks. Useful for A/B-testing how prompt length "
            "affects Aristotle's behavior. Default: no limit."
        ),
    )
    p_refine.add_argument(
        "--dry-run",
        action="store_true",
        help=(
            "Print the resolved configuration and exit without calling Claude "
            "or Aristotle. Useful for first-time use."
        ),
    )
    _add_pipeline_flags(p_refine)

    return parser


def _add_pipeline_flags(parser: argparse.ArgumentParser) -> None:
    """Adds --auto-build, --auto-commit, --auto-rate, --build-timeout to a
    subparser. Each flag is independent and defaults off."""
    parser.add_argument(
        "--auto-build",
        action="store_true",
        help=(
            "After each successful extraction, run `lake build` in --repo-dir. "
            "Captures exit code and a tail of the build log. Build failures do "
            "not abort the pipeline. Default: off."
        ),
    )
    parser.add_argument(
        "--auto-commit",
        action="store_true",
        help=(
            "After each successful extraction, stage the chapter's output "
            "folder (plus PromptLog.md if dirty) and `git commit` with an "
            "auto message. No push by default; pair with --auto-push to push. "
            "Skipped with a warning if the git index is busy. Default: off."
        ),
    )
    parser.add_argument(
        "--auto-push",
        action="store_true",
        help=(
            "After each successful auto-commit, run `git push` to send the "
            "current branch to its remote. Requires --auto-commit. Default: off."
        ),
    )
    parser.add_argument(
        "--auto-rate",
        action="store_true",
        help=(
            "After each successful extraction, spawn a Claude subprocess to "
            "rate the code 1–5 across quality / math_correctness / generality "
            "/ api_coverage / modern_lean4. Appends one JSON line per rating "
            "to <workdir>/marathon-ratings.jsonl. Uses your Max subscription. "
            "Default: off."
        ),
    )
    parser.add_argument(
        "--build-timeout",
        type=int,
        default=600,
        metavar="SECONDS",
        help=(
            "Wall-clock timeout for `lake build` in --auto-build (default: 600 "
            "= 10 minutes). On timeout the build is killed and recorded as "
            "TIMED OUT; the rest of the pipeline still runs."
        ),
    )


def main() -> None:
    args = _build_parser().parse_args()

    if args.command == "skeleton":
        try:
            asyncio.run(skeleton_command(args))
        except KeyboardInterrupt:
            print(
                "\ninterrupted; re-run the same command to resume from "
                "marathon-state.json.",
                file=sys.stderr,
            )
            sys.exit(130)
    elif args.command == "refine":
        try:
            asyncio.run(refine_command(args))
        except KeyboardInterrupt:
            print(
                "\ninterrupted; re-run the same command to resume from "
                "marathon-refine-state.json (will reattach to any in-flight "
                "Aristotle project).",
                file=sys.stderr,
            )
            sys.exit(130)


if __name__ == "__main__":
    main()
