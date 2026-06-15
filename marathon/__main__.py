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
from marathon.audit.probes import ProbeKind
from marathon.probes_aristotle import DEFAULT_MAX_PROBES as _VAC_DEFAULT_MAX
from marathon.extraction import DEFAULT_K, SOURCE_MODES


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
        default=None,
        metavar="PATH",
        help=(
            "Lean repo containing referee.md (must be a git repo). Required "
            "for the default prose pass; the `tasks` subcommand takes its "
            "own --repo-dir."
        ),
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
    p_ref.add_argument(
        "--emit-tasks",
        action="store_true",
        help=(
            "After the prose pass, persist STRUCTURED referee fix-tasks to "
            "the ledger (the 'teeth'): mechanical cross-chapter dedup tasks "
            "from audit fingerprints, plus Claude-proposed "
            "deception/naming/doc/structural tasks, plus a self-"
            "accountability pass that closes resolved prior tasks and "
            "escalates overdue ones. Default OFF — without this flag the "
            "referee is the unchanged prose-only standing-items.md rewriter. "
            "List the persisted tasks with `marathon referee tasks`."
        ),
    )

    # `marathon referee tasks` — list referee-origin fix-tasks (read-only).
    p_ref_sub = p_ref.add_subparsers(dest="referee_command")
    p_ref_tasks = p_ref_sub.add_parser(
        "tasks",
        help="List referee-origin fix-tasks (status + overdue counts).",
        description=(
            "Read-only listing of the structured fix-tasks the referee has "
            "emitted into the ledger (via `marathon referee --emit-tasks`), "
            "with each task's status, severity, kind, the decls it spans, "
            "the planner target it blocks, and how many referee passes it is "
            "overdue. No Claude call, no git mutation."
        ),
    )
    p_ref_tasks.add_argument(
        "--repo-dir",
        type=Path,
        required=True,
        metavar="PATH",
        help="Lean repo whose ledger holds the referee tasks.",
    )
    p_ref_tasks.add_argument(
        "--open",
        dest="open_only",
        action="store_true",
        help="Show only open (unresolved) tasks.",
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

    # Landing tree: `marathon landing run/status/promote` — the Phase-4
    # serial landing queue onto marathon/next (see marathon.landing).
    _add_landing_subparser(subparsers)

    # Audit tree: `marathon audit run/diff/show` — the Phase-5
    # elaborator-grade audit engine (see marathon.audit.engine).
    _add_audit_subparser(subparsers)

    # Plan tree: `marathon plan sorries/axiom/repo` — the Phase-7 planner
    # intake (see marathon.plan): build target-ledger rows from a repo's
    # sorries or a named axiom. The `textbook` mode is declared by the
    # OTHER (extraction) agent on the SAME `plan` parser via the
    # extension point left in _add_plan_subparser — do not add a
    # conflicting subparser here.
    _add_plan_subparser(subparsers)

    # Deck tree: `marathon deck` — the Phase-8 human review surface (the
    # local "Code Tinder" web app; see marathon.deck). Ready cards in,
    # verify/reject/defer out, status pane streaming conductor/landing
    # events. verify/reject route through the committed review verdict
    # path (ledger + GitHub + tracker + daemon/conductor).
    _add_deck_subparser(subparsers)

    # Fill tree: `marathon fill` (single decl) and `marathon fill-file`
    # (every sorry in a file). Both wrap `refine_command` with a focus
    # directive so the slash commands can shell out without knowing the
    # focus-directive incantation.
    add_fill_subparsers(subparsers)

    # Plan tree: `marathon plan {sorries,axiom,repo,textbook}` — the
    # Phase-7 planner intake that fills the target ledger. `textbook`
    # (firewall-gated extraction) is wired here; the other intake modes
    # register on the same `plan` parent via `_plan_subparsers()`.
    _add_plan_textbook_subparser(subparsers)

    return parser


def _plan_subparsers(subparsers):
    """Return the `marathon plan` subparsers action — the extension point
    the OTHER (plan-layer) agent's ``_add_plan_subparser`` creates and
    leaves ``textbook`` unclaimed on. ``_add_plan_subparser`` runs first in
    ``_build_parser``, so by the time the textbook registrar calls this the
    `plan` parent and its ``add_subparsers`` action already exist; we
    recover the live action from the parser's ``_actions`` (no shared global
    state — robust to the CLI being rebuilt afresh in every test).

    Falls back to creating the `plan` parent if it is somehow absent (the
    textbook registrar called in isolation), so this module is self-
    contained."""
    existing = subparsers._name_parser_map.get("plan")
    if existing is not None:
        for action in existing._actions:
            if isinstance(action, argparse._SubParsersAction):
                return action
    p_plan = subparsers.add_parser(
        "plan",
        help=(
            "Planner intake (Phase 7): build the target ledger from an "
            "axiom, a repo's sorries, or a textbook."
        ),
        description=(
            "Point marathon at a source and it produces target rows for "
            "the ledger. `textbook` extracts targets from a source text "
            "under a firewall-gated mode (copyrighted: human-supplied "
            "informal statements only, Claude never reads the book; open: "
            "the autoform chunk -> K-extractor consensus -> merge "
            "pipeline). Other intake modes (sorries/axiom/repo) attach to "
            "this same `plan` parent."
        ),
    )
    return p_plan.add_subparsers(dest="plan_command", required=True)


def _add_plan_textbook_subparser(subparsers) -> None:
    """Wire `marathon plan textbook` — firewall-gated textbook extraction
    (see ``marathon.extraction``). Owns ONLY the `textbook` sub-subcommand;
    the `plan` parent is shared via `_plan_subparsers`."""
    sub = _plan_subparsers(subparsers)
    p_tb = sub.add_parser(
        "textbook",
        help="Extract targets from a source text (firewall-gated).",
        description=(
            "Build target rows from a textbook. The firewall is per-"
            "project and mode-gated. `--mode copyrighted` (DEFAULT) NEVER "
            "lets Claude read the source: targets come from a human-"
            "authored informal-statements markdown file (one section per "
            "named result) and/or `--named-result` labels; passing a .tex "
            "is refused. `--mode open` runs the autoform consensus "
            "pipeline over an open-licensed `--source` (chunk -> K "
            "extractor calls -> consensus -> reviewer arbitration -> "
            "merge dedup). Output targets are flagged with their "
            "source_mode so downstream knows whether the informal "
            "statement was human-authored or LLM-extracted."
        ),
    )
    p_tb.add_argument(
        "--source", type=Path, default=None, metavar="PATH",
        help=(
            "Open-mode: path to the open-licensed source text (a file or a "
            "directory of .md/.tex). Required for --mode open; ignored "
            "(and refused if a .tex) under --mode copyrighted."
        ),
    )
    p_tb.add_argument(
        "--mode", choices=SOURCE_MODES, default=None,
        help=(
            "Firewall mode override. Default: the per-project `source_mode` "
            "from .marathon/review/config.toml (absent → the SAFE "
            "'copyrighted'). copyrighted: human-supplied statements only, "
            "the book is never read by Claude. open: the autoform "
            "extraction pipeline reads the source."
        ),
    )
    p_tb.add_argument(
        "--informal-statements", type=Path, default=None, metavar="FILE",
        help=(
            "Copyrighted-mode: human-authored informal-statements markdown "
            "file (one section per named result). Required for --mode "
            "copyrighted unless --named-result is given."
        ),
    )
    p_tb.add_argument(
        "--named-result", action="append", default=None, metavar="LABEL",
        dest="named_results",
        help=(
            "Copyrighted-mode: a named result to seed as a target (repeat "
            "for several). Alternative/supplement to --informal-statements."
        ),
    )
    p_tb.add_argument(
        "--normalize", action="store_true",
        help=(
            "Copyrighted-mode: run Claude to normalize the human wording "
            "(the book is never shown — only the human's statement). "
            "Off by default; the human's verbatim text is used as-is."
        ),
    )
    p_tb.add_argument(
        "--k", type=int, default=DEFAULT_K, metavar="N",
        help=(
            "Open-mode: number of independent extractor calls per chunk "
            f"(consensus over survivors). Default {DEFAULT_K}."
        ),
    )
    p_tb.add_argument(
        "--model", default=None,
        help="Claude model override (else MARATHON_CLAUDE_MODEL / default).",
    )
    p_tb.add_argument(
        "--gate-policy", choices=("auto", "human", "mixed"),
        default="human", metavar="MODE",
        help=(
            "Gate-policy resolution mode for the produced targets (plan §2 "
            "ruling 6), matching the other `plan` modes. auto: every "
            "target hands-off. human (default): every target the per-"
            "declaration ceremony. mixed: milestone-named targets human, "
            "the rest auto."
        ),
    )
    p_tb.add_argument(
        "--repo-dir", type=Path, default=None, metavar="PATH",
        help=(
            "Consumer repo root (for the firewall config + persisting "
            "targets). Default: walk up from the current directory."
        ),
    )
    p_tb.add_argument(
        "--dry-run", action="store_true",
        help=(
            "Extract and print the targets without writing the ledger."
        ),
    )
    p_tb.set_defaults(func=_run_plan_textbook)


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
    p_run.add_argument(
        "--land", choices=("next",), default=None,
        help=(
            "Phase-4 opt-in: after each successful job, enqueue its "
            "commits onto the marathon/next landing queue (processed "
            "by `marathon landing run`: cherry-pick + lake build + "
            "gate, then a plain push). Default: off — today's "
            "per-issue --auto-pr flow is unchanged until the landing "
            "stack has soaked."
        ),
    )
    p_run.add_argument(
        "--referee-every", type=int, default=0, metavar="N",
        help=(
            "Phase-8 opt-in: after every N successful landings (counted "
            "from .marathon/landing/landings.jsonl) fire `marathon referee "
            "--emit-tasks` to refresh the standing items AND persist "
            "structured fix-tasks. Those referee fix-tasks then GATE "
            "SCHEDULING: a target/rejection whose chapter is named by an "
            "unresolved BLOCKING referee task (one carrying a blocks_target) "
            "is deferred — never dispatched around — until the task "
            "resolves (the Ch.11 coordinateCoframe item survived twelve "
            "advisory iterations; this gives the referee teeth). The "
            "trigger is best-effort and idempotent (a referee failure warns "
            "and never fails a landing/dispatch). Default: 0 = OFF "
            "(manual-only referee, today's behavior — the scheduler is "
            "byte-identical when no referee tasks exist)."
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
        land=args.land,
        referee_every=args.referee_every,
    )
    if rc:
        raise SystemExit(rc)


def _add_deck_subparser(subparsers) -> None:
    """Adds `marathon deck` — the Phase-8 deck (see ``marathon.deck``):
    the local-web-app "Code Tinder" review surface. Serves ready cards
    (green SHA, gated, dep-ordered), routes verify/reject/defer through
    the committed review verdict path, and streams conductor/landing
    events to a live status pane.

    SAFETY: the server binds 127.0.0.1 ONLY and requires a per-session
    token on the irreversible POST /api/verdict; verify/reject fire only
    on a deliberate swipe, never on page load."""
    p_deck = subparsers.add_parser(
        "deck",
        help=(
            "Local 'Code Tinder' review surface (Phase 8): a 127.0.0.1 "
            "web app serving ready spec cards; v/r/defer/deep-dive."
        ),
        description=(
            "Start the deck: a local web app (bound to 127.0.0.1 only) "
            "that serves dependency-ordered, ready-only spec cards and "
            "lets you verify / reject / defer them with a live "
            "conductor/landing status pane. verify and reject are REAL, "
            "IRREVERSIBLE actions routed through the same review verdict "
            "logic as `marathon review verify/reject` (verify merges the "
            "marathon PR + flips the tracker; reject dispatches Aristotle "
            "with your note verbatim). They fire ONLY on a deliberate, "
            "token-bearing swipe — never on page load or navigation. "
            "Reads the chapter registry + audit snapshot + ledger from "
            "the consumer repo (run from inside it)."
        ),
    )
    p_deck.add_argument(
        "--chapter", type=int, default=None, metavar="N",
        help=(
            "Default chapter to scope the queue to (the UI can still "
            "switch). Default: all registered chapters."
        ),
    )
    p_deck.add_argument(
        "--port", type=int, default=0, metavar="PORT",
        help=(
            "Port to bind on 127.0.0.1. Default: 0 = an OS-assigned "
            "ephemeral port (the chosen URL is printed)."
        ),
    )
    p_deck.add_argument(
        "--no-open", action="store_true",
        help="Do not open a browser automatically (just print the URL).",
    )
    p_deck.set_defaults(func=_run_deck)


def _run_deck(args) -> None:
    from marathon.deck.server import serve
    from marathon.review.config import load_config

    cfg = load_config()
    rc = serve(
        cfg,
        port=args.port,
        default_chapter=args.chapter,
        open_browser=not args.no_open,
    )
    if rc:
        raise SystemExit(rc)


def _add_landing_subparser(subparsers) -> None:
    """Adds `marathon landing run/status/promote` — the Phase-4 landing
    queue (see ``marathon.landing``): a serial FIFO that cherry-picks
    each successful job onto the ``marathon/next`` integration branch
    behind a hard `lake build` + machine-gate check (enforce semantics,
    no override), bounces failures with a circuit-broken GitHub
    notification instead of blocking, and only ever promotes into the
    base branch via an explicit fast-forward-only command."""
    p_land = subparsers.add_parser(
        "landing",
        help=(
            "Serial landing queue onto the marathon/next integration "
            "branch (Phase 4): cherry-pick + build + gate, then push."
        ),
        description=(
            "Run or inspect the landing queue. `run` pops queued "
            "requests oldest-first and lands each onto marathon/next "
            "in a dedicated worktree (cherry-pick, lake build, machine "
            "gate with enforce semantics, plain push — never force). "
            "Failures bounce: clean abort, a report under "
            ".marathon/landing/bounces/, and at most one deduplicated "
            "GitHub comment; only push rejections re-queue (once). "
            "`promote` fast-forwards the base branch to "
            "origin/marathon/next and refuses on divergence. `status` "
            "prints queue depth, recent landings/bounces, and the lock "
            "holder. Requests are enqueued by `marathon conductor run "
            "--land next` (or marathon.landing.enqueue_landing)."
        ),
    )
    sub = p_land.add_subparsers(dest="landing_command", required=True)

    p_run = sub.add_parser(
        "run",
        help="Process the landing queue (or one drain pass with --once).",
    )
    p_run.add_argument(
        "--repo-dir", type=Path, default=None, metavar="PATH",
        help=(
            "Consumer repo root (must contain .marathon/review/"
            "config.toml). Default: walk up from the current directory."
        ),
    )
    p_run.add_argument(
        "--once", action="store_true",
        help="Drain the queue then exit instead of polling forever.",
    )
    p_run.add_argument(
        "--worktree-parent", type=Path, default=None, metavar="DIR",
        help=(
            "Parent directory for the landing worktree. MUST be outside "
            "the repo (in-repo worktrees leak into Aristotle bundles). "
            "Default: ~/Desktop/marathon-runs/landing/<repo-name>/."
        ),
    )
    p_run.add_argument(
        "--base", default="main", metavar="BRANCH",
        help=(
            "Base branch marathon/next is created from when it does not "
            "exist yet, and the promotion target. Default: main."
        ),
    )
    p_run.add_argument(
        "--build-timeout", type=int, default=1800, metavar="SECONDS",
        help=(
            "Wall-clock timeout for the landing `lake build` (default: "
            "1800 = 30 minutes, the daemon's build budget). A timeout "
            "bounces the landing."
        ),
    )
    p_run.set_defaults(func=_run_landing_run)

    p_stat = sub.add_parser(
        "status",
        help="Print queue depth + ages, recent landings/bounces, lock holder.",
    )
    p_stat.add_argument(
        "--repo-dir", type=Path, default=None, metavar="PATH",
        help="Consumer repo root. Default: walk up from the current directory.",
    )
    p_stat.add_argument(
        "--worktree-parent", type=Path, default=None, metavar="DIR",
        help=(
            "Landing worktree parent (to locate the tracked "
            "landings.jsonl riding marathon/next). Default: "
            "~/Desktop/marathon-runs/landing/<repo-name>/."
        ),
    )
    p_stat.set_defaults(func=_run_landing_status)

    p_prom = sub.add_parser(
        "promote",
        help=(
            "Fast-forward-only merge of marathon/next into the base "
            "branch (refuses on divergence)."
        ),
    )
    p_prom.add_argument(
        "--repo-dir", type=Path, default=None, metavar="PATH",
        help="Consumer repo root. Default: walk up from the current directory.",
    )
    p_prom.add_argument(
        "--base", default="main", metavar="BRANCH",
        help="Branch to fast-forward to origin/marathon/next. Default: main.",
    )
    p_prom.set_defaults(func=_run_landing_promote)


def _add_audit_subparser(subparsers) -> None:
    """Adds `marathon audit run/diff/show/tiers/backfill` — the Phase-5
    elaborator-grade audit engine (see ``marathon.audit.engine``): runs
    the generated Lean audit script inside the target repo's own
    workspace via ``lake env lean``, records per-declaration evidence
    (elaborated-type/value fingerprints over project-local vocabulary,
    transitive axioms, sorry accounting, deception tags), and persists
    snapshots at ``<repo>/.marathon/audit/latest.json`` (self-gitignored
    derived cache; the prior run rotates to ``previous.json``). `tiers`
    and `backfill` are the Phase-5b trust layer (see
    ``marathon.audit.trust``): tiers computed on read from snapshot +
    ledger verdict events, never stored."""
    p_audit = subparsers.add_parser(
        "audit",
        help="Elaborator-grade declaration audit (.marathon/audit/latest.json).",
        description=(
            "Run, diff, or inspect declaration audits. `run` derives the "
            "target folder's modules, elaborates them with the repo's own "
            "pinned toolchain (`lake env lean`), and snapshots per-decl "
            "evidence: pinned-pp type/value fingerprints (project-local "
            "constants only — Mathlib/Std/... are trusted vocabulary), "
            "transitive axioms, sorry accounting, deception tags. "
            "Declarations that fail to elaborate are recorded with status "
            "`unknown` — absence of evidence is reported, never hidden. "
            "Snapshots are derived cache (recomputable, gitignored), "
            "never committed."
        ),
    )
    sub = p_audit.add_subparsers(dest="audit_command", required=True)

    p_run = sub.add_parser(
        "run",
        help="Audit a folder's declarations and save latest.json.",
    )
    p_run.add_argument(
        "--repo-dir", type=Path, default=Path.cwd(),
        help="Consumer repo root (lake workspace). Default: current directory.",
    )
    p_run.add_argument(
        "--target", required=True, metavar="FOLDER",
        help=(
            "Folder of .lean files to audit, relative to --repo-dir "
            "(e.g. GeometricAnalysis/LeeSM). A single .lean file works too."
        ),
    )
    p_run.add_argument(
        "--timeout", type=int, default=900, metavar="SECONDS",
        help="Timeout for the `lake env lean` run. Default: 900.",
    )
    p_run.set_defaults(func=_run_audit_run)

    p_diff = sub.add_parser(
        "diff",
        help="Per-decl changes: latest.json vs previous.json.",
    )
    p_diff.add_argument(
        "--repo-dir", type=Path, default=Path.cwd(),
        help="Consumer repo root. Default: current directory.",
    )
    p_diff.set_defaults(func=_run_audit_diff)

    p_show = sub.add_parser(
        "show",
        help="Print one declaration's audit record from latest.json.",
    )
    p_show.add_argument(
        "decl", metavar="DECL",
        help=(
            "Fully qualified declaration name (a unique dotted suffix "
            "also works, e.g. `double_eq`)."
        ),
    )
    p_show.add_argument(
        "--repo-dir", type=Path, default=Path.cwd(),
        help="Consumer repo root. Default: current directory.",
    )
    p_show.set_defaults(func=_run_audit_show)

    p_tiers = sub.add_parser(
        "tiers",
        help="Computed trust tier per declaration (snapshot + verdicts).",
        description=(
            "Compute the trust tier of every declaration in the latest "
            "audit snapshot. Tiers are COMPUTED on read from the snapshot "
            "plus the ledger's append-only decl-verdict events — never "
            "stored (plan §2 ruling 4). UNKNOWN = absent/failed to "
            "elaborate; T0 = elaborated; T1 = axiom-clean (sorryAx "
            "accounted), no deception tags; T2 = + human spec-verdict "
            "with matching type+cone pins; T3 = + line-review verdict. "
            "Stale or broken pins surface as qualifiers "
            "(stale-toolchain, fingerprint-changed, cone-changed:X)."
        ),
    )
    p_tiers.add_argument(
        "--repo-dir", type=Path, default=Path.cwd(),
        help="Consumer repo root. Default: current directory.",
    )
    p_tiers.add_argument(
        "--target", default=None, metavar="FOLDER",
        help=(
            "Restrict the table to declarations from this folder's "
            "modules (same semantics as `audit run --target`). "
            "Default: every declaration in the snapshot."
        ),
    )
    p_tiers.set_defaults(func=_run_audit_tiers)

    p_backfill = sub.add_parser(
        "backfill",
        help="Pin existing VERIFIED review issues as T2 verdicts (one-time).",
        description=(
            "Map every VERIFIED review sub-issue's cited declarations "
            "onto the LATEST audit snapshot and append T2 verdict events "
            "pinned to CURRENT main (source='backfill'). The historical "
            "verdict-time SHAs include red builds and are unbuildable, "
            "so these pins assert 'current main matches what I verified "
            "back then' — which is why writing REQUIRES --attest (one "
            "human attestation pass). Without --attest this is a dry "
            "run: the full would-be-pinned table prints and nothing is "
            "written. Declarations missing from the snapshot are listed "
            "as skipped with a reason, never guessed."
        ),
    )
    p_backfill.add_argument(
        "--repo-dir", type=Path, default=Path.cwd(),
        help="Consumer repo root. Default: current directory.",
    )
    p_backfill.add_argument(
        "--chapter", type=int, default=None, metavar="N",
        help="Backfill only this chapter's issues. Default: all chapters.",
    )
    p_backfill.add_argument(
        "--attest", action="store_true",
        help=(
            "Attest that current main matches what you verified, and "
            "write the T2 events. Default: dry run."
        ),
    )
    p_backfill.set_defaults(func=_run_audit_backfill)

    p_inval = sub.add_parser(
        "invalidations",
        help="Report (and optionally surface) T2/T3 verdicts the latest "
             "snapshot no longer covers.",
        description=(
            "Diff the LATEST snapshot against the PREVIOUS one and the "
            "ledger's live verdicts: which human T2/T3 verdicts no longer "
            "cover the current code? A verdict is INVALIDATED when the "
            "decl's own type fingerprint changed, when a pinned cone "
            "member changed meaning or vanished (the member is NAMED), or "
            "when the decl went absent/unknown. Cross-toolchain wholesale "
            "staleness is reported SEPARATELY (resolve with `audit repin`, "
            "never a per-decl alarm). Default is a DRY RUN that writes "
            "nothing. With --apply: ONE batched parent-issue body rewrite "
            "flips every affected emoji 🟡→🟠, plus one idempotent "
            "marker-comment per affected issue behind a circuit breaker "
            "(content-hash dedup + per-issue daily cap) — the write-storm "
            "ruling forbids one body rewrite or one tracker flip per decl."
        ),
    )
    p_inval.add_argument(
        "--repo-dir", type=Path, default=Path.cwd(),
        help="Consumer repo root. Default: current directory.",
    )
    p_inval.add_argument(
        "--apply", action="store_true",
        help=(
            "Flip the affected tracker emojis (one batched rewrite) and "
            "post circuit-broken marker comments. Default: dry run "
            "(print the table, write nothing)."
        ),
    )
    p_inval.set_defaults(func=_run_audit_invalidations)

    p_repin = sub.add_parser(
        "repin",
        help="Bulk trust override: re-pin stale/changed verdicts to the "
             "CURRENT snapshot (requires --yes).",
        description=(
            "RE-PIN AMNESTY — the bulk trust override. For every decl "
            "carrying a live T2/T3 verdict whose pins no longer match the "
            "current snapshot (own type changed, cone changed, or stale "
            "across a toolchain bump), print the re-pin table (decl, old "
            "fingerprint prefix → new, change kind: type-text / cone / "
            "toolchain-only). With --yes, append a NEW source='repin' "
            "verdict event for each, pinned to the CURRENT snapshot "
            "(append-only — the old events are preserved, never mutated). "
            "\n\nThis asserts that YOU re-checked these declarations "
            "against their cards, OR that you accept the changes "
            "sight-unseen: re-pinning silences the invalidation by "
            "moving the human attestation onto current main. The "
            "trade-off is yours. Decls absent or unknown in the current "
            "snapshot are REFUSED (you cannot re-pin what does not "
            "elaborate). Without --yes this is a dry run."
        ),
    )
    p_repin.add_argument(
        "--repo-dir", type=Path, default=Path.cwd(),
        help="Consumer repo root (lake workspace). Default: current directory.",
    )
    p_repin.add_argument(
        "--decl", action="append", default=None, metavar="NAME",
        help=(
            "Restrict the re-pin to these declarations (repeatable; a "
            "unique dotted suffix also resolves). Default: every decl "
            "with a stale/changed verdict."
        ),
    )
    p_repin.add_argument(
        "--yes", action="store_true",
        help=(
            "Write the source='repin' verdict events. This is the trust "
            "override — see the command description. Default: dry run."
        ),
    )
    p_repin.set_defaults(func=_run_audit_repin)

    p_kernel = sub.add_parser(
        "kernel",
        help="The trust kernel of a declaration: the minimized human-read "
             "surface (project-local defs in its statement cone).",
        description=(
            "Compute the TRUST KERNEL of DECL (plan §2 ruling 5, goal 2's "
            "mechanism): the transitive set of PROJECT-LOCAL definitions in "
            "DECL's elaborated-type statement cone — the exact set of "
            "definitions a human must read to trust the statement. "
            "Mathlib/core constants are trusted vocabulary and are NOT in "
            "the kernel; proof bodies are NEVER in the kernel. Prints the "
            "kernel members (dependencies-first), the size (decls + "
            "pinned-pp LOC), any project-local lemmas whose statements are "
            "referenced but not read, and any unresolved cone references "
            "(absent from the snapshot — reported, never hidden). Pure over "
            "the latest snapshot; no Lean, no network."
        ),
    )
    p_kernel.add_argument(
        "decl", metavar="DECL",
        help=(
            "Fully qualified declaration name (a unique dotted suffix also "
            "works, e.g. `main_thm`)."
        ),
    )
    p_kernel.add_argument(
        "--repo-dir", type=Path, default=Path.cwd(),
        help="Consumer repo root. Default: current directory.",
    )
    p_kernel.set_defaults(func=_run_audit_kernel)

    p_card = sub.add_parser(
        "card",
        help="The machine half of a declaration's spec card (markdown).",
        description=(
            "Render the MACHINE HALF of DECL's spec card (the "
            "diff-of-meaning unit, plan §2 ruling 5): the statement, the "
            "'Definitions you must read' = the trust kernel ONLY (never the "
            "whole file), and the evidence table (computed tier, axioms "
            "beyond whitelist, sorry status, deception tags). The Claude "
            "half — fresh informal rendering, kernel-shrink suggestions, "
            "advisory semantic-delta prose — is attached later by the "
            "spec-auditor and is absent here. Pure over the latest snapshot "
            "+ ledger; no Lean, no network."
        ),
    )
    p_card.add_argument(
        "decl", metavar="DECL",
        help=(
            "Fully qualified declaration name (a unique dotted suffix also "
            "works)."
        ),
    )
    p_card.add_argument(
        "--repo-dir", type=Path, default=Path.cwd(),
        help="Consumer repo root. Default: current directory.",
    )
    p_card.set_defaults(func=_run_audit_card)

    p_vac = sub.add_parser(
        "vacuity-probe",
        help="OPT-IN, BUDGET-SPENDING: ask Aristotle to DISPROVE a "
             "theorem's hypotheses (success = broken/vacuous spec).",
        description=(
            "!! SPENDS REAL ARISTOTLE BUDGET !! A vacuity probe asks "
            "Aristotle to prove a target theorem's hypotheses entail "
            "`False`. A SUCCESS means the hypotheses are jointly "
            "unsatisfiable, so the theorem is VACUOUSLY TRUE — a "
            "broken/misformalized spec (the documented typo-exploit "
            "failure mode). This is the only probe that ACTIVELY HUNTS "
            "misformalization with a prover, and it is the expensive one, "
            "so it is OPT-IN ONLY (never wired into the auto pipeline) and "
            "governed: a HARD per-invocation cap (--max-probes, default "
            f"{_VAC_DEFAULT_MAX}), and a persisted per-goal DEDUP "
            "(content-hash under .marathon/audit/vacuity/) so the same "
            "vacuity goal is never resubmitted across runs.\n\n"
            "EVIDENCE IS ASYMMETRIC: a SUCCESSFUL disproof is HIGH-SIGNAL "
            "and writes a structured finding (it does NOT auto-file a "
            "rejection this phase — review and reject manually). A "
            "failure-to-disprove is WEAK negative evidence ('no vacuity "
            "found, inconclusive') and NEVER raises any tier or trust "
            "level — Aristotle gives up opaquely, so absence of a finding "
            "proves nothing.\n\n"
            "The probe goal is staged into a THROWAWAY COPY of your repo "
            "(filtered by .gitignore); your working tree is never touched "
            "and the probe .lean is never committed. Pure-Lean probes "
            "(unfolding/sanity) are free and ship first; reach for this "
            "only when you want a prover to hunt vacuous statements."
        ),
    )
    p_vac.add_argument(
        "decl", nargs="+", metavar="DECL",
        help=(
            "One or more target theorem names to probe (fully qualified; a "
            "unique dotted suffix also resolves). Only theorem-like decls "
            "are probeable; defs/structures are skipped."
        ),
    )
    p_vac.add_argument(
        "--repo-dir", type=Path, default=Path.cwd(),
        help="Consumer repo root (lake workspace). Default: current directory.",
    )
    p_vac.add_argument(
        "--max-probes", type=int, default=_VAC_DEFAULT_MAX, metavar="N",
        help=(
            f"HARD cap on probes submitted this invocation (default "
            f"{_VAC_DEFAULT_MAX}). Each probe spends real Aristotle budget; "
            "eligible goals beyond the cap are skipped (reported)."
        ),
    )
    p_vac.add_argument(
        "--polling-interval", type=int, default=30, metavar="SECONDS",
        help="Seconds between task-status polls. Default: 30.",
    )
    p_vac.add_argument(
        "--dry-run", action="store_true",
        help=(
            "Plan only: print which goals WOULD be submitted (under the cap, "
            "minus dedups) and exit WITHOUT spending any budget. Generates "
            "and prints the goal files but submits nothing."
        ),
    )
    p_vac.set_defaults(func=_run_audit_vacuity_probe)

    p_probe = sub.add_parser(
        "probe",
        help="Generate (and optionally build) pure-Lean probes for a decl "
             "(unfolding / sanity — FREE, no Aristotle).",
        description=(
            "Generate the PURE-LEAN probes for DECL (plan §2 ruling 5, "
            "Phase 6b cheap-first tier — NO Aristotle, no budget): an "
            "UNFOLDING probe (a def's wellformedness / total-ness, degrading "
            "honestly to a typecheck when no expected value is supplied — it "
            "does NOT emit rfl-trivial probes that prove nothing) and a "
            "SANITY probe (the PUnit-collapse catcher, referee.md #1, for "
            "structures/classes — emitted as 'needs witness' when the "
            "snapshot can't supply a non-collapsing model, never a vacuous "
            "pass). Prints the generated probe source. With --run (and lake "
            "present) it BUILDS each probe OUTSIDE the repo tree (so probe "
            "files can never enter an Aristotle bundle) against the repo's "
            "built modules and reports pass/fail/error/needs_witness — a "
            "FAILING unfolding/sanity probe is a high-signal finding; a "
            "FAILING shrink certificate rejects the shrink. Pure over the "
            "latest snapshot; --run additionally needs the repo built. "
            "(For the BUDGET-SPENDING vacuity probe, see `audit "
            "vacuity-probe`.)"
        ),
    )
    p_probe.add_argument(
        "decl", metavar="DECL",
        help=(
            "Fully qualified declaration name (a unique dotted suffix also "
            "works)."
        ),
    )
    p_probe.add_argument(
        "--repo-dir", type=Path, default=Path.cwd(),
        help="Consumer repo root. Default: current directory.",
    )
    p_probe.add_argument(
        "--kind", action="append", choices=[k.value for k in ProbeKind],
        metavar="KIND",
        help=(
            "Restrict to a probe kind (unfolding / sanity); repeatable. "
            "Default: the kinds that apply to the decl. shrink_certificate "
            "needs a spec-auditor obligation and is never auto-generated "
            "from a decl."
        ),
    )
    p_probe.add_argument(
        "--run", action="store_true",
        help=(
            "Build the generated probes via `lake env lean` (outside the "
            "repo tree) and report outcomes. Default: print source only."
        ),
    )
    p_probe.add_argument(
        "--timeout", type=int, default=600, metavar="SECONDS",
        help="Timeout for each probe's `lake env lean` build. Default: 600.",
    )
    p_probe.set_defaults(func=_run_audit_probe)


def _add_plan_subparser(subparsers) -> None:
    """Adds `marathon plan sorries/axiom/repo` — the Phase-7 planner
    intake (see ``marathon.plan``): point marathon at a repo's sorries or
    a named axiom and it builds target-ledger rows (the per-statement work
    model that ``order.txt``'s chapter granularity could never give).

    EXTENSION POINT (do not collide): the `plan` PARENT parser is shared
    via :func:`_plan_subparsers` (the idempotent coordination point the
    extraction agent's `textbook` mode also uses) — this builder owns ONLY
    the firewall-safe non-textbook sub-subcommands (sorries / axiom /
    repo) and never recreates the parent. Registration order between this
    and `_add_plan_textbook_subparser` is therefore irrelevant: whichever
    runs first creates the parent, the other reuses it. The dispatcher in
    ``main`` routes every `plan ...` subcommand through the subparser's
    ``set_defaults(func=…)`` handler."""
    sub = _plan_subparsers(subparsers)

    def _add_common(p: argparse.ArgumentParser) -> None:
        p.add_argument(
            "--repo-dir", type=Path, default=None, metavar="PATH",
            help=(
                "Consumer repo root (must be a git repo for the "
                "gitignore filter). Default: walk up from the current "
                "directory."
            ),
        )
        p.add_argument(
            "--gate-policy", choices=("auto", "human", "mixed"),
            default="human", metavar="MODE",
            help=(
                "Gate-policy resolution mode (plan §2 ruling 6). auto: "
                "every target hands-off. human (default): every target "
                "the per-declaration ceremony. mixed: milestone-named "
                "targets (Stokes-critical) human, the rest auto."
            ),
        )
        p.add_argument(
            "--dry-run", action="store_true",
            help="Print the target table and exit WITHOUT writing the ledger.",
        )

    p_sorries = sub.add_parser(
        "sorries",
        help="One target per sorry-bodied decl under a folder (or the repo).",
    )
    p_sorries.add_argument(
        "--target", type=Path, default=None, metavar="FOLDER",
        help=(
            "Folder of .lean files to scan, relative to --repo-dir (a "
            "single .lean file works too). Default: the whole "
            "gitignore-filtered repo (same as `plan repo`)."
        ),
    )
    _add_common(p_sorries)
    p_sorries.set_defaults(func=_run_plan_sorries)

    p_repo = sub.add_parser(
        "repo",
        help="Every sorry across the gitignore-filtered repo.",
    )
    _add_common(p_repo)
    p_repo.set_defaults(func=_run_plan_repo)

    p_axiom = sub.add_parser(
        "axiom",
        help="A single target for a named axiom/decl to discharge.",
    )
    p_axiom.add_argument(
        "axiom_name", metavar="NAME",
        help="Fully-qualified axiom/declaration name to discharge.",
    )
    _add_common(p_axiom)
    p_axiom.set_defaults(func=_run_plan_axiom)


def _plan_repo_dir(args) -> Path:
    from marathon.review.config import find_repo_dir

    return args.repo_dir.resolve() if args.repo_dir else find_repo_dir()


def _emit_plan(plan, repo_dir: Path, *, dry_run: bool) -> None:
    """Print the target table + summary, and (unless dry-run) commit the
    plan to the ledger. Shared by every `plan` subcommand."""
    from marathon.ledger import Ledger
    from marathon.plan import source_mode

    targets = plan.targets
    if not targets:
        print("no targets found")
        return
    name_w = max(len(t.name) for t in targets)
    kind_w = max(len(t.kind) for t in targets)
    print(f"{len(targets)} target(s) [firewall source_mode="
          f"{source_mode(repo_dir)}]:")
    # Count how many targets have at least one outgoing dep edge.
    with_deps = {src for src, _ in plan.edges}
    for t in sorted(targets, key=lambda t: t.name):
        dep_mark = " *" if t.name in with_deps else ""
        ref = t.source_ref or "-"
        print(
            f"  {t.name:<{name_w}}  {t.kind:<{kind_w}}  "
            f"{t.gate_policy:<5}  {ref}{dep_mark}"
        )
    breakdown = plan.gate_breakdown()
    print(
        f"summary: {len(targets)} target(s), {len(plan.edges)} dep edge(s) "
        f"across {len(with_deps)} target(s); gate_policy "
        + ", ".join(f"{k}={v}" for k, v in breakdown.items() if v)
    )
    if dry_run:
        print("dry run — nothing written. Re-run without --dry-run to "
              "write these target rows to the ledger.")
        return
    written = plan.commit(Ledger.for_repo(repo_dir))
    print(
        f"wrote {written['targets']} target(s) and {written['edges']} dep "
        f"edge(s) to {Ledger.for_repo(repo_dir).db_path}"
    )


def _run_plan_sorries(args) -> None:
    from marathon.plan import plan_from_sorries

    repo_dir = _plan_repo_dir(args)
    plan = plan_from_sorries(
        repo_dir, args.target, gate_mode=args.gate_policy
    )
    _emit_plan(plan, repo_dir, dry_run=args.dry_run)


def _run_plan_repo(args) -> None:
    from marathon.plan import plan_from_repo

    repo_dir = _plan_repo_dir(args)
    plan = plan_from_repo(repo_dir, gate_mode=args.gate_policy)
    _emit_plan(plan, repo_dir, dry_run=args.dry_run)


def _run_plan_axiom(args) -> None:
    from marathon.plan import plan_from_axiom

    repo_dir = _plan_repo_dir(args)
    plan = plan_from_axiom(
        repo_dir, args.axiom_name, gate_mode=args.gate_policy
    )
    _emit_plan(plan, repo_dir, dry_run=args.dry_run)


def _run_audit_run(args) -> None:
    from marathon.audit.engine import run_audit, save_snapshot
    from marathon.gate import AXIOM_WHITELIST, SORRY_AXIOM

    repo_dir: Path = args.repo_dir.resolve()
    snapshot = run_audit(repo_dir, args.target, timeout=args.timeout)
    path = save_snapshot(snapshot, repo_dir)
    decls = snapshot.decls
    sorried = sum(1 for d in decls if d.has_sorry is True)
    unknown = sum(1 for d in decls if d.status == "unknown")
    beyond = sorted({
        ax for d in decls for ax in d.axioms
        if ax not in AXIOM_WHITELIST and ax != SORRY_AXIOM
    })
    tagged = [(d.name, ";".join(d.tags)) for d in decls if d.tags]
    print(
        f"audited {len(decls)} declaration(s) across "
        f"{len(snapshot.modules)} module(s)"
        + (f" [toolchain {snapshot.toolchain}]" if snapshot.toolchain else "")
    )
    print(f"  sorry'd (transitive sorryAx): {sorried}")
    print(f"  unknown (no evidence): {unknown}")
    print(
        f"  axioms beyond whitelist: {len(beyond)}"
        + (f" ({', '.join(beyond)})" if beyond else "")
    )
    print(f"  deception-tagged: {len(tagged)}")
    for name, tags in tagged:
        print(f"    {name}: {tags}")
    if snapshot.failures:
        print(f"  failures ({len(snapshot.failures)}):")
        for failure in snapshot.failures:
            print(f"    - {failure}")
    print(f"saved {path}")
    if not decls and snapshot.failures:
        # Nothing audited at all — honest absence, but a failing exit so
        # scripts don't mistake an empty snapshot for a clean one.
        raise SystemExit(1)


def _run_audit_diff(args) -> None:
    from marathon.audit.engine import (
        DIFF_KEYS, LATEST_NAME, PREVIOUS_NAME, diff_snapshots, load_snapshot,
    )

    repo_dir: Path = args.repo_dir.resolve()
    new = load_snapshot(repo_dir, LATEST_NAME)
    if new is None:
        print("no latest audit snapshot; run `marathon audit run` first")
        raise SystemExit(1)
    old = load_snapshot(repo_dir, PREVIOUS_NAME)
    if old is None:
        print(
            "no previous audit snapshot to diff against "
            "(only one run recorded so far)"
        )
        raise SystemExit(1)
    diff = diff_snapshots(old, new)
    print(f"audit diff: {old.created_at} -> {new.created_at}")
    for warning in diff.get("warnings", []):
        print(f"  WARNING: {warning}")
    total = 0
    for key in DIFF_KEYS:
        names = diff.get(key, [])
        total += len(names)
        print(f"  {key}: {len(names)}")
        for name in names:
            print(f"    {name}")
    if total == 0:
        print("  no per-declaration changes")


def _run_audit_show(args) -> None:
    from marathon.audit.engine import load_snapshot

    repo_dir: Path = args.repo_dir.resolve()
    snapshot = load_snapshot(repo_dir)
    if snapshot is None:
        print("no latest audit snapshot; run `marathon audit run` first")
        raise SystemExit(1)
    by_name = snapshot.by_name()
    decl = by_name.get(args.decl)
    if decl is None:
        # Convenience: a unique dotted-suffix match also resolves.
        matches = [
            d for name, d in by_name.items()
            if name.endswith("." + args.decl)
        ]
        if len(matches) == 1:
            decl = matches[0]
        elif matches:
            print(f"ambiguous suffix {args.decl!r}; candidates:")
            for d in matches:
                print(f"  {d.name}")
            raise SystemExit(1)
    if decl is None:
        print(f"declaration {args.decl!r} not in latest snapshot "
              f"({len(by_name)} decl(s) audited)")
        raise SystemExit(1)
    print(f"name: {decl.name}")
    print(f"kind: {decl.kind}")
    print(f"module: {decl.module}")
    print(f"status: {decl.status}")
    print(f"type: {decl.type_pp if decl.type_pp is not None else '-'}")
    print(f"value: {decl.value_pp if decl.value_pp is not None else '-'}")
    print(f"fingerprint_type: {decl.fingerprint_type or '-'}")
    print(f"fingerprint_value: {decl.fingerprint_value or '-'}")
    print(f"cone: {', '.join(decl.cone) or '-'}")
    print(f"axioms: {', '.join(decl.axioms) or '-'}")
    sorry = "-" if decl.has_sorry is None else str(decl.has_sorry).lower()
    print(f"has_sorry: {sorry}")
    print(f"tags: {';'.join(decl.tags) or '-'}")
    if decl.reason is not None:
        print(f"reason: {decl.reason}")


def _run_audit_tiers(args) -> None:
    from marathon.audit.engine import derive_modules, load_snapshot
    from marathon.audit.trust import TIER_ORDER, compute_tiers
    from marathon.ledger import Ledger

    repo_dir: Path = args.repo_dir.resolve()
    snapshot = load_snapshot(repo_dir)
    if snapshot is None:
        print("no latest audit snapshot; run `marathon audit run` first")
        raise SystemExit(1)
    ledger = Ledger.for_repo(repo_dir)
    results = compute_tiers(snapshot, ledger)
    if args.target is not None:
        modules, failures = derive_modules(repo_dir, args.target)
        for failure in failures:
            print(f"  warning: {failure}")
        if not modules:
            print(f"no auditable .lean modules under {args.target}")
            raise SystemExit(1)
        wanted = set(modules)
        by_name = snapshot.by_name()
        results = [
            r for r in results if by_name[r.decl_name].module in wanted
        ]
    if not results:
        print("no declarations to report")
        return
    print(
        f"trust tiers: {len(results)} declaration(s), computed on read "
        f"(snapshot {snapshot.created_at}"
        + (f", toolchain {snapshot.toolchain}" if snapshot.toolchain else "")
        + ")"
    )
    name_w = max(len(r.decl_name) for r in results)
    for r in sorted(results, key=lambda r: r.decl_name):
        line = f"  {r.decl_name:<{name_w}}  {r.tier:<7}"
        if r.qualifiers:
            line += "  " + ",".join(r.qualifiers)
        print(line)
    counts = {tier: 0 for tier in TIER_ORDER}
    for r in results:
        counts[r.tier] += 1
    print("summary: " + "  ".join(
        f"{tier}={counts[tier]}" for tier in TIER_ORDER
    ))


def _run_audit_backfill(args) -> None:
    from marathon.audit.engine import load_snapshot
    from marathon.audit.trust import apply_backfill, plan_backfill
    from marathon.ledger import Ledger
    from marathon.review.config import load_config

    repo_dir: Path = args.repo_dir.resolve()
    snapshot = load_snapshot(repo_dir)
    if snapshot is None:
        print("no latest audit snapshot; run `marathon audit run` first")
        raise SystemExit(1)
    cfg = load_config(repo_dir)
    ledger = Ledger.for_repo(repo_dir)
    chapters = [args.chapter] if args.chapter is not None else None
    plan = plan_backfill(cfg, snapshot, ledger, chapters=chapters)
    if plan.items:
        print(f"would pin {len(plan.items)} declaration(s) as T2 "
              f"(toolchain {snapshot.toolchain}):")
        name_w = max(len(i.decl_name) for i in plan.items)
        for item in plan.items:
            print(
                f"  {item.decl_name:<{name_w}}  #{item.issue_num}  "
                f"{item.fingerprint_type[:12]}…  cone={item.cone_size}"
            )
    else:
        print("nothing to pin")
    if plan.skipped:
        print(f"skipped {len(plan.skipped)} citation(s):")
        for skip in plan.skipped:
            print(f"  {skip.name} (#{skip.issue_num}): {skip.reason}")
    if not plan.items:
        return
    if not args.attest:
        print(
            "dry run — nothing written. These pins assert 'current main "
            "matches what I verified back then'; re-run with --attest to "
            "make that attestation and write the T2 events."
        )
        return
    written = apply_backfill(ledger, snapshot, plan)
    print(
        f"wrote {written} T2 verdict event(s) (source=backfill) to "
        f"{ledger.db_path}"
    )


def _run_audit_invalidations(args) -> None:
    from marathon.audit.engine import LATEST_NAME, PREVIOUS_NAME, load_snapshot
    from marathon.audit.invalidate import (
        compute_invalidations, notify_invalidations,
    )
    from marathon.ledger import Ledger
    from marathon.review.config import load_config

    repo_dir: Path = args.repo_dir.resolve()
    new = load_snapshot(repo_dir, LATEST_NAME)
    if new is None:
        print("no latest audit snapshot; run `marathon audit run` first")
        raise SystemExit(1)
    old = load_snapshot(repo_dir, PREVIOUS_NAME)  # may be None (first run)
    ledger = Ledger.for_repo(repo_dir)
    report = compute_invalidations(old, new, ledger)
    if not args.apply:
        print(notify_invalidations(load_config(repo_dir), report, apply=False))
        return
    cfg = load_config(repo_dir)
    print(notify_invalidations(cfg, report, apply=True))


def _run_audit_repin(args) -> None:
    from marathon.audit.engine import LATEST_NAME, PREVIOUS_NAME, load_snapshot
    from marathon.audit.invalidate import compute_invalidations
    from marathon.audit.trust import TrustError, _cone_pins, record_spec_verdict
    from marathon.ledger import Ledger

    repo_dir: Path = args.repo_dir.resolve()
    snapshot = load_snapshot(repo_dir, LATEST_NAME)
    if snapshot is None:
        print("no latest audit snapshot; run `marathon audit run` first")
        raise SystemExit(1)
    previous = load_snapshot(repo_dir, PREVIOUS_NAME)
    ledger = Ledger.for_repo(repo_dir)
    report = compute_invalidations(previous, snapshot, ledger)
    by_name = snapshot.by_name()
    all_events = ledger.all_decl_verdict_events()

    # Candidate (decl, change-kind, claimed-tier) re-pins. type-text /
    # cone come from per-decl invalidations; toolchain-only from the
    # separate stale list (a bump, not a meaning change). Decls absent/
    # unknown in the CURRENT snapshot are refused below, never re-pinned.
    candidates: list[tuple[str, str, str]] = []
    for inv in report.invalidations:
        kind = "cone" if inv.cause == "cone-changed" else (
            "absent" if inv.cause == "absent" else "type-text"
        )
        candidates.append((inv.decl_name, kind, inv.tier_claimed))
    for name in report.stale_toolchain:
        claimed = _claimed_tier(all_events.get(name, []))
        candidates.append((name, "toolchain-only", claimed))

    # --decl filter: exact, else unique dotted-suffix (same rule as
    # `audit show`). An unresolvable/ambiguous selector is a hard error
    # rather than a silent no-op.
    if args.decl is not None:
        wanted: set[str] = set()
        for raw in args.decl:
            resolved = _resolve_repin_target(raw, [c[0] for c in candidates])
            if resolved is None:
                print(
                    f"--decl {raw!r}: no stale/changed verdict matches "
                    "(nothing to re-pin for it)"
                )
                raise SystemExit(1)
            wanted.add(resolved)
        candidates = [c for c in candidates if c[0] in wanted]

    if not candidates:
        print("no stale or changed verdicts to re-pin")
        return

    refused: list[tuple[str, str]] = []
    pinnable: list[tuple[str, str, str]] = []
    for name, kind, claimed in candidates:
        decl = by_name.get(name)
        if kind == "absent" or decl is None or decl.is_unknown \
                or decl.fingerprint_type is None:
            refused.append((name, "absent or unknown in the current snapshot"))
            continue
        # A re-pin pins the decl's CURRENT cone, so a cone member that
        # vanished/never-elaborated makes the decl unpinnable too — the
        # 'refuse decls absent/unknown' ruling extends to an absent cone.
        # Pre-check here (mirroring record_spec_verdict's guard) so the
        # command refuses cleanly per-decl instead of crashing mid-batch
        # on TrustError after the table prints (which also left earlier
        # decls in the batch partially re-pinned). Naming the missing
        # member fixes the MINOR old-fp==new-fp 'cone' row: the operator
        # sees WHY it cannot be re-pinned, not a fake clean diff.
        _pins, unpinnable = _cone_pins(decl, by_name)
        if unpinnable:
            refused.append((
                name,
                "cone member(s) absent or unknown — cannot re-pin: "
                + ", ".join(unpinnable),
            ))
        else:
            pinnable.append((name, kind, claimed))

    print(f"re-pin table (snapshot {snapshot.created_at}, toolchain "
          f"{snapshot.toolchain}):")
    if pinnable:
        name_w = max(len(n) for n, _, _ in pinnable)
        for name, kind, claimed in pinnable:
            old_fp = _old_pinned_fingerprint(all_events.get(name, []))
            new_fp = by_name[name].fingerprint_type or ""
            print(
                f"  {name:<{name_w}}  {claimed}  "
                f"{(old_fp[:12] + '…') if old_fp else '-':<13} → "
                f"{(new_fp[:12] + '…') if new_fp else '-':<13}  {kind}"
            )
    else:
        print("  (nothing pinnable)")
    if refused:
        print(f"refused {len(refused)} decl(s):")
        for name, why in refused:
            print(f"  {name}: {why}")

    if not pinnable:
        return
    if not args.yes:
        print(
            "dry run — nothing written. `audit repin --yes` is the bulk "
            "trust override: it asserts you re-checked these decls (or "
            "accept the changes sight-unseen) and appends new "
            "source='repin' verdict events pinned to current main."
        )
        return
    written = 0
    late_refused: list[tuple[str, str]] = []
    for name, _kind, claimed in pinnable:
        # The cone pre-check above should already have refused any
        # unpinnable decl, so record_spec_verdict cannot raise here in
        # practice. Guard anyway: a TrustError mid-batch must refuse THIS
        # decl, not abort the loop and leave the already-written earlier
        # decls as a partially-applied amnesty (defense in depth for the
        # append-only / 'refuse, never crash' rulings).
        try:
            record_spec_verdict(
                ledger, name, snapshot,
                tier=claimed,
                issue_num=_pinned_issue(all_events.get(name, [])),
                source="repin",
                notes="re-pin amnesty: operator re-checked or accepts current "
                      "main sight-unseen",
            )
        except TrustError as e:
            late_refused.append((name, str(e)))
            continue
        written += 1
    print(
        f"wrote {written} source='repin' verdict event(s) to "
        f"{ledger.db_path} (old events preserved — append-only)"
    )
    if late_refused:
        print(f"refused {len(late_refused)} decl(s) at write time:")
        for name, why in late_refused:
            print(f"  {name}: {why}")


def _claimed_tier(events) -> str:
    """Highest live verdict tier across an event log (T3 before T2),
    defaulting to T2 — used to re-pin at the same rung the human
    attested."""
    from marathon.audit.invalidate import _live_verdict

    event = _live_verdict(events)
    return event.tier_claimed if event is not None else "T2"


def _old_pinned_fingerprint(events) -> str:
    from marathon.audit.invalidate import _live_verdict

    event = _live_verdict(events)
    return (event.fingerprint_type or "") if event is not None else ""


def _pinned_issue(events):
    from marathon.audit.invalidate import _live_verdict

    event = _live_verdict(events)
    return event.issue_num if event is not None else None


def _resolve_repin_target(raw: str, names: list[str]):
    """Resolve a ``--decl`` selector against the candidate decl names:
    exact match, else a UNIQUE dotted-suffix match. None when neither
    (caller errors out — a re-pin must never guess which decl)."""
    if raw in names:
        return raw
    suffix = [n for n in names if n.endswith("." + raw)]
    return suffix[0] if len(suffix) == 1 else None


def _resolve_snapshot_decl(decl: str, snapshot):
    """Resolve a CLI decl selector against a snapshot: exact match, else a
    UNIQUE dotted-suffix match (same convenience as `audit show`). Returns
    the resolved fully-qualified name, or None when ambiguous/absent — the
    caller prints the candidates and exits, never guessing."""
    by_name = snapshot.by_name()
    if decl in by_name:
        return decl
    matches = sorted(n for n in by_name if n.endswith("." + decl))
    if len(matches) == 1:
        return matches[0]
    if matches:
        print(f"ambiguous suffix {decl!r}; candidates:")
        for name in matches:
            print(f"  {name}")
        raise SystemExit(1)
    return None


def _run_audit_kernel(args) -> None:
    from marathon.audit.engine import load_snapshot
    from marathon.audit.kernel import compute_kernel

    repo_dir: Path = args.repo_dir.resolve()
    snapshot = load_snapshot(repo_dir)
    if snapshot is None:
        print("no latest audit snapshot; run `marathon audit run` first")
        raise SystemExit(1)
    resolved = _resolve_snapshot_decl(args.decl, snapshot)
    # An unresolved selector still produces an honest kernel (target
    # reported unresolved) — but if it isn't even a known suffix, say so.
    target = resolved if resolved is not None else args.decl
    kernel = compute_kernel(target, snapshot)
    print(f"trust kernel of {kernel.target}")
    print(
        f"  size: {kernel.size_decls} local def(s), "
        f"{kernel.size_loc} pinned-pp line(s)"
    )
    print("  definitions you must read:")
    if kernel.members:
        for member in kernel.members:
            print(f"    {member.name}  ({member.kind})")
    else:
        print("    none — phrased entirely in trusted vocabulary")
    if kernel.local_lemmas:
        print("  local lemmas referenced (statements not read):")
        for name in kernel.local_lemmas:
            print(f"    {name}")
    if kernel.unresolved:
        print("  unresolved cone references (no audit evidence):")
        for name in kernel.unresolved:
            print(f"    {name}")


def _run_audit_card(args) -> None:
    from marathon.audit.engine import load_snapshot
    from marathon.audit.spec_card import SpecCard
    from marathon.ledger import Ledger

    repo_dir: Path = args.repo_dir.resolve()
    snapshot = load_snapshot(repo_dir)
    if snapshot is None:
        print("no latest audit snapshot; run `marathon audit run` first")
        raise SystemExit(1)
    resolved = _resolve_snapshot_decl(args.decl, snapshot)
    target = resolved if resolved is not None else args.decl
    ledger = Ledger.for_repo(repo_dir)
    card = SpecCard.from_snapshot(target, snapshot, ledger)
    print(card.render_markdown())


def _run_audit_probe(args) -> None:
    from marathon.audit.engine import load_snapshot
    from marathon.audit.probes import probes_for_decl, run_probes

    repo_dir: Path = args.repo_dir.resolve()
    snapshot = load_snapshot(repo_dir)
    if snapshot is None:
        print("no latest audit snapshot; run `marathon audit run` first")
        raise SystemExit(1)
    resolved = _resolve_snapshot_decl(args.decl, snapshot)
    if resolved is None:
        print(
            f"declaration {args.decl!r} not in latest snapshot "
            f"({len(snapshot.by_name())} decl(s) audited)"
        )
        raise SystemExit(1)
    decl = snapshot.by_name()[resolved]
    kinds = (
        tuple(ProbeKind(k) for k in args.kind) if args.kind else None
    )
    probes = probes_for_decl(decl, kinds=kinds)
    if not probes:
        # The kinds that auto-generate (unfolding/sanity) don't apply to
        # this decl — say so honestly rather than print nothing.
        print(
            f"no auto-generatable probes for {resolved} (kind {decl.kind!r}): "
            "unfolding applies to def/abbrev/instance, sanity to "
            "structure/class. (shrink_certificate needs a spec-auditor "
            "obligation, not a decl.)"
        )
        return
    print(f"generated {len(probes)} probe(s) for {resolved} (kind {decl.kind}):")
    for probe in probes:
        marker = " [needs witness]" if probe.needs_witness else ""
        print(f"\n--- {probe.kind.value} probe{marker} ---")
        print(probe.source.rstrip())
    if not args.run:
        print(
            "\n(generated only — pass --run to build them with `lake env "
            "lean` and report outcomes.)"
        )
        return
    report = run_probes(repo_dir, probes, timeout=args.timeout)
    print()
    print(report.render())
    # A high-signal finding (a failing unfolding/sanity probe, or a rejected
    # shrink) is a nonzero exit so scripts notice — an `error` (toolchain
    # trouble) or `needs_witness` is honest absence of evidence, not a fail.
    if report.findings:
        raise SystemExit(1)


def _run_audit_vacuity_probe(args) -> None:
    from marathon.audit.engine import load_snapshot
    from marathon.probes_aristotle import run_probes, select_targets

    repo_dir: Path = args.repo_dir.resolve()
    snapshot = load_snapshot(repo_dir)
    if snapshot is None:
        print("no latest audit snapshot; run `marathon audit run` first")
        raise SystemExit(1)

    # Resolve every selector against the snapshot (exact / unique suffix);
    # an unresolved one is a hard error, never a silent no-op.
    resolved_names: list[str] = []
    for raw in args.decl:
        resolved = _resolve_snapshot_decl(raw, snapshot)
        if resolved is None:
            print(f"declaration {raw!r} not in latest snapshot")
            raise SystemExit(1)
        resolved_names.append(resolved)

    if args.dry_run:
        # Plan only — generate goals, report the governor's decision, spend
        # nothing. (Dedup index is still consulted so the dry run is honest
        # about what a real run would skip.)
        plan = select_targets(
            snapshot, resolved_names, repo_dir, max_probes=args.max_probes
        )
        print(
            f"DRY RUN — no Aristotle budget spent. cap={args.max_probes}; "
            f"would submit {len(plan.to_run)} probe(s):"
        )
        for goal in plan.to_run:
            print(f"  {goal.decl_name}  (goal-hash {goal.goal_hash[:8]})")
        if plan.skipped_dedup:
            print(
                "  skipped (already submitted): "
                + ", ".join(plan.skipped_dedup)
            )
        if plan.skipped_cap:
            print(f"  skipped (over cap): {', '.join(plan.skipped_cap)}")
        if plan.skipped_unprobeable:
            print(
                "  skipped (not a probeable theorem): "
                + ", ".join(plan.skipped_unprobeable)
            )
        # Print one generated goal so the operator can eyeball it.
        if plan.to_run:
            sample = plan.to_run[0]
            print(
                f"\n--- generated goal for {sample.decl_name} "
                f"({sample.relpath}) ---"
            )
            print(sample.lean_source)
        return

    print(
        f"!! vacuity probe — spending real Aristotle budget !! "
        f"cap={args.max_probes}"
    )
    plan, outcomes = asyncio.run(
        run_probes(
            repo_dir,
            snapshot,
            resolved_names,
            max_probes=args.max_probes,
            polling_interval=args.polling_interval,
        )
    )
    if plan.skipped_dedup:
        print(f"skipped (already submitted): {', '.join(plan.skipped_dedup)}")
    if plan.skipped_cap:
        print(f"skipped (over cap, not spent): {', '.join(plan.skipped_cap)}")
    if plan.skipped_unprobeable:
        print(
            "skipped (not a probeable theorem): "
            + ", ".join(plan.skipped_unprobeable)
        )
    broken = [o for o in outcomes if o.broken_spec]
    for outcome in outcomes:
        print(f"  {outcome.summary}")
    if broken:
        print(
            f"\n{len(broken)} BROKEN-SPEC finding(s) written to "
            ".marathon/audit/vacuity/findings/ — review and reject "
            "manually (no auto-rejection filed this phase)."
        )
        raise SystemExit(2)
    print(
        "no broken specs found (all probes inconclusive — WEAK negative "
        "evidence, no tier change)."
    )


def _run_landing_run(args) -> None:
    from marathon.landing import run_landing

    rc = run_landing(
        repo_dir=args.repo_dir.resolve() if args.repo_dir else None,
        once=args.once,
        worktree_parent=args.worktree_parent,
        base=args.base,
        build_timeout=args.build_timeout,
    )
    if rc:
        raise SystemExit(rc)


def _run_landing_status(args) -> None:
    from marathon.landing import print_landing_status
    from marathon.review.config import find_repo_dir

    repo_dir: Path = args.repo_dir.resolve() if args.repo_dir else find_repo_dir()
    rc = print_landing_status(repo_dir, worktree_parent=args.worktree_parent)
    if rc:
        raise SystemExit(rc)


def _run_landing_promote(args) -> None:
    from marathon.landing import promote

    rc = promote(
        args.repo_dir.resolve() if args.repo_dir else None, base=args.base
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


def _run_plan_textbook(args) -> None:
    """`marathon plan textbook` — firewall-gated textbook extraction.

    Produces Target-shaped dicts via ``marathon.extraction.extract_targets``,
    maps them onto the plan layer's ``Target`` rows, and reuses the plan
    layer's ``Plan.from_targets`` + ``_emit_plan`` (so the table print,
    summary, `--dry-run`, and ledger commit are identical to the other
    `plan` modes).

    The firewall mode defaults to the PER-PROJECT ``source_mode`` config
    (the binding "firewall policy becomes per-project config" ruling), and
    is overridable with ``--mode``.
    """
    from marathon.extraction import ExtractionError, extract_targets
    from marathon.plan import Plan, resolve_gate_policy, source_mode

    repo_dir = _plan_repo_dir(args)

    # Mode: explicit --mode wins; else the per-project firewall config
    # (absent → the SAFE 'copyrighted').
    mode = args.mode or source_mode(repo_dir)

    try:
        target_dicts = extract_targets(
            args.source,
            mode=mode,
            k=args.k,
            model=args.model,
            informal_statements=args.informal_statements,
            named_results=args.named_results,
            normalize=args.normalize,
        )
    except ExtractionError as e:
        raise SystemExit(str(e))

    targets = [
        _textbook_dict_to_target(d, gate_mode=args.gate_policy, resolver=resolve_gate_policy)
        for d in target_dicts
    ]
    print(f"firewall mode: {mode}")
    # Textbook intake derives no dependency edges (the statement cone is the
    # audit engine's domain, not the extractor's) — targets only.
    plan = Plan.from_targets(targets)
    _emit_plan(plan, repo_dir, dry_run=args.dry_run)


# Map an extractor kind (theorem/lemma/definition/...) onto a ledger
# TARGET_KIND. Anything statement-shaped that isn't already a def collapses
# to the generic 'statement' kind (the ledger kind reserved for textbook-
# extracted targets); definitions map to 'def'.
_EXTRACT_KIND_TO_TARGET_KIND = {
    "theorem": "theorem",
    "lemma": "theorem",
    "proposition": "theorem",
    "corollary": "theorem",
    "claim": "theorem",
    "conjecture": "theorem",
    "definition": "def",
    "construction": "def",
    "axiom": "axiom",
}


def _textbook_dict_to_target(d: dict, *, gate_mode: str, resolver):
    """Map one extraction dict onto a plan-layer ``Target``.

    The single named seam between the extraction module's documented dict
    interface (``{name, kind, source_ref, statement, source_mode}``) and the
    plan layer's ``Target`` row. The informal statement goes into ``notes``;
    the firewall ``source_mode`` is appended to ``source_ref`` so the honesty
    marker (human-authored vs LLM-extracted) survives into the ledger."""
    from marathon.ledger import Target

    name = d["name"]
    kind = _EXTRACT_KIND_TO_TARGET_KIND.get(d.get("kind", ""), "statement")
    src = d.get("source_ref") or None
    source_mode_flag = d.get("source_mode", "")
    if src and source_mode_flag:
        src = f"{src} [source_mode={source_mode_flag}]"
    elif source_mode_flag:
        src = f"[source_mode={source_mode_flag}]"
    return Target(
        name=name,
        kind=kind,
        source_ref=src,
        gate_policy=resolver(
            gate_mode=gate_mode, decl=name, source_ref=d.get("source_ref")
        ),
        notes=d.get("statement") or None,
    )


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
    elif args.command == "landing":
        # Dispatch via the subparser's set_defaults(func=…) handler. A
        # Ctrl-C mid-landing is safe: every attempt starts by realigning
        # the worktree to origin/marathon/next, and the popped request's
        # work is re-derivable from the per-issue branch/PR.
        try:
            args.func(args)
        except KeyboardInterrupt:
            print("\ninterrupted", file=sys.stderr)
            sys.exit(130)
    elif args.command == "audit":
        # Dispatch via the subparser's set_defaults(func=…) handler. All
        # subcommands are short-lived synchronous calls; `run`'s lake
        # subprocess dies with us on Ctrl-C.
        try:
            args.func(args)
        except KeyboardInterrupt:
            print("\ninterrupted", file=sys.stderr)
            sys.exit(130)
    elif args.command == "plan":
        # Dispatch via the subparser's set_defaults(func=…) handler. The
        # non-textbook modes (sorries/axiom/repo) are pure Python; the
        # textbook mode (owned by the extraction agent) manages its own
        # asyncio internally but may also register an async func — handle
        # both. A Ctrl-C is safe everywhere here (the sorry/axiom planner
        # holds no durable state until commit; `plan textbook`'s K Claude
        # calls hold none until persist).
        try:
            result = args.func(args)
            if asyncio.iscoroutine(result):
                asyncio.run(result)
        except KeyboardInterrupt:
            print("\ninterrupted", file=sys.stderr)
            sys.exit(130)
    elif args.command == "deck":
        # Dispatch via the subparser's set_defaults(func=…) handler. The
        # deck's `serve` blocks in serve_forever and handles its own
        # KeyboardInterrupt (clean shutdown + server_close); a bare Ctrl-C
        # here is the fallback.
        try:
            args.func(args)
        except KeyboardInterrupt:
            print("\ninterrupted", file=sys.stderr)
            sys.exit(130)
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
