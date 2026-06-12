"""Marathon CLI entry point. Run as ``python -m marathon ...``."""

import argparse
import asyncio
import sys
from pathlib import Path

from marathon.review.cli import add_subparser as _add_review_subparser
from marathon.review.cli import review_command
from marathon.refine import refine_command
from marathon.referee import referee_command
from marathon.skeleton import skeleton_command
from marathon.fill import add_fill_subparsers


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
            "is omitted, Marathon auto-detects <repo-dir>/.marathon/referee.md. The "
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
        "--auto-referee-every",
        type=int,
        default=0,
        metavar="N",
        help=(
            "After every N successfully-completed iterations of this refine "
            "invocation, automatically run the referee agent to refresh the "
            "machine-managed tail of referee.md (and commit it). 0 (default) "
            "disables. Typical: 1 to run after every iteration, 3 to run "
            "once per chapter at the end. The referee scans the repo, "
            "per-chapter workdirs (siblings of --workdir), and the current "
            "referee.md; the agent's output replaces the section between "
            "the `BEGIN: Marathon-managed referee tail` sentinels. The "
            "user-managed header above the sentinel is preserved untouched."
        ),
    )
    p_refine.add_argument(
        "--no-cross-chapter",
        action="store_true",
        help=(
            "Disable cross-chapter context aggregation. Normally Marathon "
            "scans the parent of --workdir for sibling chapter workdirs and "
            "splices their latest marathon.md tails + auto-rater notes into "
            "Hermes' prompt, so chapters in the same batch can coordinate "
            "structural decisions. Pass this flag for solo / standalone "
            "refines where sibling workdirs are unrelated."
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
        "--live-steering",
        action="store_true",
        help=(
            "Run the Hermes live-steering watcher alongside each Aristotle "
            "submission. The watcher subscribes to the project's event stream "
            "and, on every EDITING_FILE event, asks Claude whether Aristotle "
            "is going off-course. If so, it sends a steering prompt via "
            "project.ask(...). Each decision is logged to "
            "<workdir>/marathon-steering-log.jsonl; Hermes' own running "
            "notes accumulate in <workdir>/hermes-memory.md, which each "
            "subsequent Hermes call reads so it doesn't re-flag resolved "
            "issues or forget what it asked Aristotle for. Default: off."
        ),
    )
    p_refine.add_argument(
        "--no-continue-on-review",
        action="store_true",
        help=(
            "Disable the auto-continuation protocol. By default, when an "
            "Aristotle task ends in COMPLETE_WITH_ERRORS (Aristotle's UI "
            "labels this \"Review Suggested\") or OUT_OF_BUDGET, Marathon "
            "dispatches the next iteration / retry via `project.ask(...)` "
            "on the same project to preserve Aristotle's server-side "
            "session, instead of uploading a fresh bundle. Hermes drafts a "
            "continuation prompt framed as \"refine what you've done\" "
            "rather than a from-scratch task, and the previous task's "
            "output_summary is folded into the review context. Pass this "
            "flag to fall back to the old fresh-project-each-iteration "
            "behavior."
        ),
    )
    p_refine.add_argument(
        "--review-rejection",
        type=int,
        default=None,
        metavar="ISSUE_NUM",
        help=(
            "Restrict the pending-rejections section of Hermes's prompt "
            "to a single rejected sub-issue (by GitHub issue number). "
            "Used by the auto-refine daemon to dispatch one rejection "
            "per iteration; eliminates the prior failure mode where "
            "Aristotle saw multiple queued rejections and silently "
            "picked one. Has no effect if the named issue isn't in "
            "the current rejection queue."
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

    p_ref = subparsers.add_parser(
        "referee",
        help=(
            "Run the referee agent: scan the repo + per-chapter workdirs and "
            "refresh the machine-managed tail of referee.md."
        ),
        description=(
            "One-shot pass of the referee agent. Reads the current "
            "referee.md (split into user-managed header above the BEGIN "
            "sentinel and machine-managed tail below), the reviewer rubrics "
            "(to deduplicate), the repo's Lean files, all per-chapter "
            "marathon.md / ratings.jsonl / refine-log.md under "
            "--workdirs-parent, and the recent git log. The agent emits a "
            "fresh machine-managed tail; Marathon reassembles the file with "
            "the user header preserved verbatim and either overwrites "
            "referee.md (default, with an auto-commit) or writes "
            "referee.md.proposed (with --review)."
        ),
    )
    p_ref.add_argument(
        "--repo-dir",
        type=Path,
        required=True,
        metavar="PATH",
        help="Lean repo containing referee.md (must be a git repo).",
    )
    p_ref.add_argument(
        "--referee",
        type=Path,
        default=None,
        metavar="FILE",
        help=(
            "Path to the referee.md file to update. Defaults to "
            "<repo-dir>/.marathon/referee.md."
        ),
    )
    p_ref.add_argument(
        "--workdirs-parent",
        type=Path,
        default=None,
        metavar="DIR",
        help=(
            "Parent directory containing per-chapter marathon refine workdirs "
            "(subdirs with marathon-refine-state.json). The referee aggregates "
            "their marathon.md, ratings, and refine-log files for context. "
            "Without this flag the agent only sees the repo state."
        ),
    )
    p_ref.add_argument(
        "--review",
        action="store_true",
        help=(
            "Don't overwrite referee.md. Write the new content to "
            "referee.md.proposed for manual review. Implies --no-commit."
        ),
    )
    p_ref.add_argument(
        "--no-commit",
        action="store_true",
        help=(
            "Write referee.md but don't auto-commit. Useful when you want "
            "to inspect the change before committing manually."
        ),
    )
    p_ref.add_argument(
        "--push",
        action="store_true",
        help=(
            "After auto-committing, also `git push` the current branch. "
            "Default: off. Ignored under --review or --no-commit."
        ),
    )

    # Review tree: `marathon review list/next/show/verify/reject/...`
    # Project-specific settings come from <repo>/.marathon/review/config.toml.
    _add_review_subparser(subparsers)

    # Formalization tree: `marathon formalization init/update`
    _add_formalization_subparser(subparsers)

    # Ledger tree: `marathon ledger init/import/status` — the Phase-1
    # SQLite runtime ledger (dual-write target; reads stay legacy).
    _add_ledger_subparser(subparsers)

    # Conductor tree: `marathon conductor run/status` — the Phase-3
    # repo-level multi-flight dispatcher (see marathon.conductor).
    _add_conductor_subparser(subparsers)

    # Fill tree: `marathon fill` (single decl) and `marathon fill-file`
    # (every sorry in a file). Both wrap `refine_command` with a focus
    # directive so the slash commands can shell out without knowing the
    # focus-directive incantation.
    add_fill_subparsers(subparsers)

    return parser


def _add_formalization_subparser(subparsers) -> None:
    """Adds `marathon formalization init/update` for managing the
    mathlib-initiative v0.2 ``formalization.yaml`` at the repo root."""
    p_form = subparsers.add_parser(
        "formalization",
        help="Manage formalization.yaml (mathlib-initiative v0.2 schema).",
        description=(
            "Initialize or refresh the project's `formalization.yaml`. "
            "Auto-fields (version, sorry_count, sorry_in_definitions, "
            "automation.models, automation.framework) are managed by "
            "marathon; every other field is human-curated and preserved "
            "verbatim across refreshes."
        ),
    )
    sub = p_form.add_subparsers(dest="form_command", required=True)

    p_init = sub.add_parser(
        "init",
        help="Create formalization.yaml at the repo root from the v0.2 template.",
    )
    p_init.add_argument(
        "--repo-dir", type=Path, default=Path.cwd(),
        help="Repo root. Default: current directory.",
    )
    p_init.add_argument(
        "--force", action="store_true",
        help="Overwrite an existing formalization.yaml. Default: refuse.",
    )
    p_init.set_defaults(func=_run_formalization_init)

    p_up = sub.add_parser(
        "update",
        help="Refresh formalization.yaml's auto-fields (no-op if file missing).",
    )
    p_up.add_argument(
        "--repo-dir", type=Path, default=Path.cwd(),
        help="Repo root. Default: current directory.",
    )
    p_up.add_argument(
        "--models", nargs="+", default=None,
        help="Model identifiers to stamp into automation.models.",
    )
    p_up.add_argument(
        "--framework", default="Marathon",
        help="Framework name for automation.framework. Default: 'Marathon'.",
    )
    p_up.add_argument(
        "--check-axioms", action="store_true",
        help=(
            "Run `#print axioms` on every declaration in "
            "`status.main_results` and replace their `axioms` lists "
            "with the verified set. Requires a built project "
            "(`.lake/build/lib/lean/...` populated); will silently "
            "leave existing axioms unchanged on decls whose modules "
            "haven't been built. One `lake env lean` invocation, "
            "batched across all main results. Default: off."
        ),
    )
    p_up.set_defaults(func=_run_formalization_update)

    p_bf = sub.add_parser(
        "backfill-wall-time",
        help=(
            "Reconstruct .marathon/wall-time.json from Aristotle's record "
            "of every project in PromptLog.md."
        ),
        description=(
            "Walk PromptLog.md for every Aristotle project UUID ever "
            "submitted, ask the Aristotle API for each project's actual "
            "task wall-clock, and rebuild the project-id-keyed wall-time "
            "sidecar from those authoritative spans. Use this once after "
            "upgrading to the v2 sidecar to recover the historical total "
            "(the live accumulator only counts compute from the upgrade "
            "forward). Requires ARISTOTLE_API_KEY. Overwrites the sidecar."
        ),
    )
    p_bf.add_argument(
        "--repo-dir", type=Path, default=Path.cwd(),
        help="Repo root (must contain PromptLog.md). Default: current directory.",
    )
    p_bf.add_argument(
        "--concurrency", type=int, default=8, metavar="N",
        help="Max concurrent Aristotle API fetches. Default: 8.",
    )
    p_bf.add_argument(
        "--update-yaml", action="store_true",
        help=(
            "After rebuilding the sidecar, refresh formalization.yaml's "
            "automation.cost.wall_time from the new total."
        ),
    )
    p_bf.set_defaults(func=_run_formalization_backfill_wall_time)


def _add_ledger_subparser(subparsers) -> None:
    """Adds `marathon ledger init/import/status` for the Phase-1 runtime
    ledger at ``<repo>/.marathon/marathon.db`` (see ``marathon.ledger``).

    Phase-1 contract: the ledger is a dual-write mirror of the legacy
    JSON state surfaces — reads stay on the legacy files until a later
    cutover, so these commands manage the mirror (create it, backfill it
    from the seven legacy surfaces, inspect it) without any behavior
    change elsewhere."""
    p_ledger = subparsers.add_parser(
        "ledger",
        help="Manage the runtime ledger (.marathon/marathon.db, SQLite).",
        description=(
            "Initialize, backfill, or inspect the Phase-1 SQLite ledger. "
            "The ledger mirrors the legacy state surfaces "
            "(review/state.json, config.toml chapter registry, "
            "wall-time.json, PromptLog.md, per-workdir marathon-state / "
            "marathon-refine-state checkpoints); marathon's review verdict "
            "commands dual-write into it automatically once it exists. "
            "The db is consumer-repo runtime state and must be gitignored "
            "(.marathon/marathon.db*); tracked git provenance for verdicts "
            "lives in .marathon/review/verdicts.jsonl instead."
        ),
    )
    sub = p_ledger.add_subparsers(dest="ledger_command", required=True)

    p_init = sub.add_parser(
        "init",
        help="Create the ledger db + schema (idempotent).",
    )
    p_init.add_argument(
        "--repo-dir", type=Path, default=Path.cwd(),
        help="Consumer repo root. Default: current directory.",
    )
    p_init.set_defaults(func=_run_ledger_init)

    p_imp = sub.add_parser(
        "import",
        help="One-shot idempotent import of the legacy state surfaces.",
        description=(
            "Ingest whatever legacy surfaces exist under --repo-dir "
            "(review config.toml chapters, review/state.json, "
            "wall-time.json, PromptLog.md) plus, with --workdirs-parent, "
            "the per-workdir marathon-state.json / "
            "marathon-refine-state.json checkpoints. Idempotent: "
            "re-running updates rows in place instead of duplicating."
        ),
    )
    p_imp.add_argument(
        "--repo-dir", type=Path, default=Path.cwd(),
        help="Consumer repo root. Default: current directory.",
    )
    p_imp.add_argument(
        "--workdirs-parent", type=Path, default=None, metavar="DIR",
        help=(
            "Parent directory containing per-chapter marathon workdirs "
            "(subdirs with marathon-state.json / "
            "marathon-refine-state.json) — same convention as "
            "`marathon referee --workdirs-parent`. Omit to import only "
            "the in-repo surfaces."
        ),
    )
    p_imp.set_defaults(func=_run_ledger_import)

    p_stat = sub.add_parser(
        "status",
        help="Print the ledger's schema version and per-table row counts.",
    )
    p_stat.add_argument(
        "--repo-dir", type=Path, default=Path.cwd(),
        help="Consumer repo root. Default: current directory.",
    )
    p_stat.set_defaults(func=_run_ledger_status)


def _add_conductor_subparser(subparsers) -> None:
    """Adds `marathon conductor run/status` — the Phase-3 repo-level
    multi-flight dispatcher (see ``marathon.conductor``): one daemon
    polling pending rejections across ALL registered chapters and
    running up to N concurrent `marathon refine` jobs, each in its own
    git worktree of the consumer repo. Deterministic Python only — no
    LLM in scheduling/retry decisions; Aristotle jobs are never
    canceled automatically."""
    p_cond = subparsers.add_parser(
        "conductor",
        help=(
            "Repo-level multi-flight refine dispatcher (Phase 3): N "
            "concurrent rejection-fix jobs in isolated git worktrees."
        ),
        description=(
            "Run or inspect the conductor. `run` polls the review "
            "rejection queue across every registered chapter (oldest "
            "verdict first) and dispatches up to --concurrency "
            "`marathon refine` subprocesses simultaneously, each in its "
            "own git worktree so jobs never contaminate each other's "
            "Aristotle bundles. Failure handling reuses the review "
            "daemon's retry/stall state machine (backoff requeue, then "
            "stall + GitHub notification). `status` prints the "
            ".marathon/conductor/jobs.json snapshot without touching a "
            "running conductor."
        ),
    )
    sub = p_cond.add_subparsers(dest="conductor_command", required=True)

    p_run = sub.add_parser(
        "run",
        help="Start the conductor loop (or one drain pass with --once).",
    )
    p_run.add_argument(
        "--repo-dir", type=Path, default=None, metavar="PATH",
        help=(
            "Consumer repo root (must contain .marathon/review/"
            "config.toml). Default: walk up from the current directory."
        ),
    )
    p_run.add_argument(
        "--concurrency", type=int, default=None, metavar="N",
        help=(
            "Max simultaneous refine jobs. Default: 1 (parity with the "
            "single-flight daemon), or the MARATHON_ARISTOTLE_MAX_"
            "CONCURRENT env var when set. Harmonic's concurrent-session "
            "limits are undocumented — probe with "
            "scripts/aristotle_concurrency_probe.py before raising this."
        ),
    )
    p_run.add_argument(
        "--once", action="store_true",
        help="Drain the queue (all jobs finished, nothing pending) then exit.",
    )
    p_run.add_argument(
        "--prune", action="store_true",
        help=(
            "Before dispatching, remove leftover job worktrees from "
            "prior runs (failed jobs keep theirs for debugging)."
        ),
    )
    p_run.add_argument(
        "--max-attempts", type=int, default=None, metavar="N",
        help=(
            "Consecutive failed dispatches tolerated per rejection "
            "before it is stalled + notified (default: the review "
            "daemon's budget, currently 3)."
        ),
    )
    p_run.add_argument(
        "--worktree-parent", type=Path, default=None, metavar="DIR",
        help=(
            "Parent directory for job worktrees + workdirs. MUST be "
            "outside the repo (in-repo worktrees leak into Aristotle "
            "bundles). Default: ~/Desktop/marathon-runs/conductor/"
            "<repo-name>/."
        ),
    )
    p_run.set_defaults(func=_run_conductor_run)

    p_stat = sub.add_parser(
        "status",
        help="Print the conductor's jobs.json snapshot as a table.",
    )
    p_stat.add_argument(
        "--repo-dir", type=Path, default=None, metavar="PATH",
        help=(
            "Consumer repo root. Default: walk up from the current "
            "directory."
        ),
    )
    p_stat.set_defaults(func=_run_conductor_status)


def _run_conductor_run(args) -> None:
    from marathon.conductor import run_conductor
    from marathon.review.daemon import DEFAULT_MAX_ATTEMPTS

    rc = run_conductor(
        repo_dir=args.repo_dir.resolve() if args.repo_dir else None,
        concurrency=args.concurrency,
        once=args.once,
        prune=args.prune,
        max_attempts=(
            args.max_attempts if args.max_attempts is not None
            else DEFAULT_MAX_ATTEMPTS
        ),
        worktree_parent=args.worktree_parent,
    )
    if rc:
        raise SystemExit(rc)


def _run_conductor_status(args) -> None:
    from marathon.conductor import print_status
    from marathon.review.config import find_repo_dir

    repo_dir: Path = args.repo_dir.resolve() if args.repo_dir else find_repo_dir()
    rc = print_status(repo_dir)
    if rc:
        raise SystemExit(rc)


def _run_ledger_init(args) -> None:
    from marathon.ledger import SCHEMA_VERSION, Ledger

    repo_dir: Path = args.repo_dir.resolve()
    path = Ledger.for_repo(repo_dir).init()
    print(f"ledger ready at {path} (schema v{SCHEMA_VERSION})")
    print(
        "reminder: add `.marathon/marathon.db*` to the consumer repo's "
        ".gitignore — the db (and its WAL -wal/-shm siblings) is runtime "
        "state, never committed. Tracked verdict provenance lives in "
        ".marathon/review/verdicts.jsonl."
    )


def _run_ledger_import(args) -> None:
    from marathon.ledger import Ledger, import_all, print_import_summary

    repo_dir: Path = args.repo_dir.resolve()
    workdirs_parent: Path | None = (
        args.workdirs_parent.resolve() if args.workdirs_parent else None
    )
    counts = import_all(repo_dir, workdirs_parent=workdirs_parent)
    print_import_summary(Ledger.for_repo(repo_dir).db_path, counts)


def _run_ledger_status(args) -> None:
    from marathon.ledger import Ledger

    repo_dir: Path = args.repo_dir.resolve()
    ledger = Ledger.for_repo(repo_dir)
    if not ledger.db_path.is_file():
        print(f"no ledger at {ledger.db_path}; run `marathon ledger init`")
        raise SystemExit(1)
    info = ledger.status()
    print(f"{ledger.db_path} (schema v{info['schema_version']})")
    for table, count in info["tables"].items():
        print(f"  {table}: {count} row(s)")


def _run_formalization_init(args) -> None:
    from marathon.formalization import (
        FORMALIZATION_FILENAME, update_formalization,
    )
    repo_dir: Path = args.repo_dir.resolve()
    target = repo_dir / FORMALIZATION_FILENAME
    if target.is_file() and not args.force:
        print(f"refusing to overwrite existing {target} (use --force to allow)")
        raise SystemExit(2)
    written = update_formalization(
        repo_dir, framework="Marathon", create_if_missing=True
    )
    print(f"wrote {written}")


def _run_formalization_update(args) -> None:
    from marathon.formalization import update_formalization
    repo_dir: Path = args.repo_dir.resolve()
    written = update_formalization(
        repo_dir,
        models=args.models,
        framework=args.framework,
        check_axioms_on_build=getattr(args, "check_axioms", False),
    )
    if written is None:
        print(f"no formalization.yaml at {repo_dir}; "
              "run `marathon formalization init` first")
        raise SystemExit(1)
    suffix = " (with axioms)" if getattr(args, "check_axioms", False) else ""
    print(f"refreshed {written}{suffix}")


def _run_formalization_backfill_wall_time(args) -> None:
    import aristotlelib
    from marathon.skeleton import _ensure_api_key
    from marathon.formalization import (
        backfill_wall_time, format_wall_time, update_formalization,
    )

    repo_dir: Path = args.repo_dir.resolve()
    _ensure_api_key()  # exits if ARISTOTLE_API_KEY is unset; calls set_api_key

    def _progress(done: int, total: int) -> None:
        print(f"\r  fetching project durations… {done}/{total}",
              end="", file=sys.stderr, flush=True)

    summary = asyncio.run(
        backfill_wall_time(repo_dir, concurrency=args.concurrency,
                           progress=_progress)
    )
    print("", file=sys.stderr)  # newline after the progress line

    if summary["projects_in_log"] == 0:
        print(f"no PromptLog.md project UUIDs found under {repo_dir}; nothing to backfill")
        raise SystemExit(1)

    total = summary["total_seconds"]
    print(f"backfilled {summary['fetched']}/{summary['projects_in_log']} projects "
          f"→ {format_wall_time(total)} ({total}s)")
    if summary["forbidden"]:
        n = len(summary["forbidden"])
        print(f"  could not fetch {n} project(s) (403/expired — likely submitted "
              f"under a different API key); total is a lower bound. Re-run with a "
              f"key that owns them to recover more.")

    if args.update_yaml:
        written = update_formalization(repo_dir, framework="Marathon")
        if written is None:
            print(f"  (no formalization.yaml at {repo_dir}; sidecar updated, yaml unchanged)")
        else:
            print(f"  refreshed {written} (wall_time = {format_wall_time(total)})")


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
        "--audit-verified",
        action="store_true",
        help=(
            "After each successful auto-commit, audit the just-landed "
            "diff against the set of verified declarations from the "
            "project's `marathon review` sub-issues. Any verified "
            "declaration modified by this iteration is flagged loudly "
            "and logged to `<workdir>/marathon-audit-violations.jsonl`. "
            "Soft warning only — does not auto-revert or re-launch. "
            "Requires the consumer repo to use the `marathon review` "
            "workflow (`.marathon/review/config.toml` + GitHub "
            "sub-issues); gracefully no-ops otherwise. Default: off "
            "for manual `marathon refine`; the auto-refine daemon "
            "enables this by default."
        ),
    )
    parser.add_argument(
        "--no-update-formalization",
        dest="update_formalization",
        action="store_false",
        default=True,
        help=(
            "Skip the per-iteration refresh of `formalization.yaml` "
            "(mathlib-initiative v0.2 schema) at the repo root. "
            "Default: on, but only writes when the file already "
            "exists at the repo root — opt in per project via "
            "`marathon formalization init`. The auto-refresh updates "
            "sorry_count, sorry_in_definitions, version, and the "
            "automation.models/framework fields; everything else is "
            "preserved verbatim."
        ),
    )
    parser.add_argument(
        "--no-metadata-commit",
        dest="commit_metadata",
        action="store_false",
        default=True,
        help=(
            "Exclude `formalization.yaml` from the per-iteration "
            "auto-commit staging and skip its per-iteration refresh. "
            "Used by the repo-level conductor's parallel workers: N "
            "jobs all rewriting the yaml is the generalized form of "
            "the wall_time merge race, so the conductor regenerates it "
            "centrally in the primary checkout after each job lands. "
            "The project-id-keyed wall-time sidecar and PromptLog.md "
            "are still committed — they are merge-friendly by design. "
            "Default: metadata committed (parity with single-flight "
            "runs)."
        ),
    )
    parser.add_argument(
        "--focus-directive",
        type=str,
        default=None,
        metavar="STRING",
        help=(
            "Inject a high-salience directive into Hermes's prompt — "
            "the last thing Claude reads before drafting the Aristotle "
            "instruction. Used by `marathon fill` / `marathon fill-file` "
            "to scope an iteration to a single declaration or file. "
            "Example: \"Fill ONLY the sorry body of "
            "`CovectorField.coordinateCoframe`. Do not modify any "
            "other declaration.\""
        ),
    )
    parser.add_argument(
        "--auto-pr",
        action="store_true",
        help=(
            "Run the iteration on a dedicated marathon-owned branch "
            "(`marathon/refine-c<N>-i<issue>` when --review-rejection "
            "is set, otherwise `marathon/refine-c<N>`) and open or "
            "update a PR against --auto-pr-base (default: `main`) "
            "after the auto-commit lands. The branch is reset to "
            "origin/<base> at iteration start so the PR always "
            "reflects the latest iteration's diff against the base, "
            "and force-pushed at iteration end. Refuses to run when "
            "the working tree is dirty (would clobber uncommitted "
            "work). Solves the failure mode where the daemon "
            "accidentally commits iteration changes onto an "
            "unrelated branch. Default: off."
        ),
    )
    parser.add_argument(
        "--auto-pr-repo",
        default=None,
        metavar="OWNER/NAME",
        help=(
            "GitHub repo for --auto-pr. Inferred from `gh repo view` "
            "if omitted."
        ),
    )
    parser.add_argument(
        "--auto-pr-base",
        default="main",
        metavar="BRANCH",
        help=(
            "Base branch for --auto-pr. Default: `main`."
        ),
    )
    parser.add_argument(
        "--gate",
        choices=("off", "warn", "enforce"),
        default="warn",
        help=(
            "Machine-gate posture for the post-extraction pipeline. The "
            "gate (build outcome + axiom whitelist + sorry accounting + "
            "forbidden-keyword scan; mode-aware — skeleton iterations "
            "expect sorry bodies) renders into the console and the "
            "--auto-pr body's Gate section. warn (default): report only, "
            "never block. enforce: a fail-level verdict blocks ONLY the "
            "PR open/update step — commit/push still happen, the work is "
            "preserved — unless --gate-override is given; "
            "--review-rejection iterations are always demoted to warn "
            "(human-demanded runs are never blocked). off: skip the gate "
            "entirely. No faithfulness judging anywhere — that review "
            "stays human."
        ),
    )
    parser.add_argument(
        "--gate-override",
        type=str,
        default=None,
        metavar="REASON",
        help=(
            "With --gate enforce: open/update the PR despite a fail-level "
            "gate verdict. REASON is recorded in the PR body's Gate "
            "section and printed to the console — an audited override, "
            "not a mute."
        ),
    )
    parser.add_argument(
        "--jury",
        action="store_true",
        help=(
            "After each successful extraction, spawn an advisory Claude "
            "jury scoring proof_integrity + code_quality (1–5; pass needs "
            "both ≥ 3; explicitly NO faithfulness — the firewall keeps "
            "the source text away from Claude). The verdict line joins "
            "the console output + PR body and one JSON line is appended "
            "to <workdir>/marathon-jury.jsonl. Advisory only — never "
            "blocks, even under --gate enforce. Uses your Max "
            "subscription. Default: off."
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
    elif args.command == "referee":
        try:
            referee_command(args)
        except KeyboardInterrupt:
            print("\ninterrupted", file=sys.stderr)
            sys.exit(130)
    elif args.command == "review":
        try:
            review_command(args)
        except KeyboardInterrupt:
            print("\ninterrupted", file=sys.stderr)
            sys.exit(130)
    elif args.command == "formalization":
        # Dispatch via the subparser's set_defaults(func=…) handler.
        args.func(args)
    elif args.command == "ledger":
        # Dispatch via the subparser's set_defaults(func=…) handler.
        args.func(args)
    elif args.command == "conductor":
        # Dispatch via the subparser's set_defaults(func=…) handler.
        # No KeyboardInterrupt wrapper: `conductor run` installs its own
        # SIGINT/SIGTERM handlers (stop dispatching, wait for jobs).
        args.func(args)
    elif args.command in ("fill", "fill-file"):
        # `fill`/`fill-file` set_defaults(func=_run_fill[_file]); both are
        # async wrappers that build a focus directive and delegate to
        # `refine_command`. The `_verb` default lets the handler distinguish
        # the two without re-parsing args.command.
        try:
            asyncio.run(args.func(args))
        except KeyboardInterrupt:
            print("\ninterrupted", file=sys.stderr)
            sys.exit(130)


if __name__ == "__main__":
    main()
