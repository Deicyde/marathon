"""Marathon CLI entry point. Run as ``python -m marathon ...``."""

import argparse
import asyncio
import sys
from pathlib import Path

from marathon.review.cli import add_subparser as _add_review_subparser
from marathon.review.cli import review_command
from marathon.refine import refine_command
from marathon.skeleton import skeleton_command
from marathon.fill import add_fill_subparsers
from marathon.repair import add_repair_subparsers
from marathon.cli import audit as _audit_cli


# Compatibility facade for callers and tests that historically imported the
# audit CLI's private handlers from ``marathon.__main__``.
_add_audit_subparser = _audit_cli._add_audit_subparser
_run_audit_run = _audit_cli._run_audit_run
_run_audit_diff = _audit_cli._run_audit_diff
_run_audit_show = _audit_cli._run_audit_show
_run_audit_tiers = _audit_cli._run_audit_tiers
_run_audit_backfill = _audit_cli._run_audit_backfill
_run_audit_invalidations = _audit_cli._run_audit_invalidations
_run_audit_repin = _audit_cli._run_audit_repin
_claimed_tier = _audit_cli._claimed_tier
_old_pinned_fingerprint = _audit_cli._old_pinned_fingerprint
_pinned_issue = _audit_cli._pinned_issue
_resolve_repin_target = _audit_cli._resolve_repin_target
_resolve_snapshot_decl = _audit_cli._resolve_snapshot_decl
_run_audit_kernel = _audit_cli._run_audit_kernel
_run_audit_card = _audit_cli._run_audit_card
_run_audit_spec_audit = _audit_cli._run_audit_spec_audit
_run_audit_probe = _audit_cli._run_audit_probe
_run_audit_vacuity_probe = _audit_cli._run_audit_vacuity_probe


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
        "--review-verdict-ts",
        default=None,
        metavar="ISO_TIMESTAMP",
        help=argparse.SUPPRESS,
    )
    p_refine.add_argument(
        "--auto-pr-expected-remote-oid",
        default=None,
        metavar="SHA",
        help=argparse.SUPPRESS,
    )
    p_refine.add_argument(
        "--auto-pr-continue-remote",
        action="store_true",
        help=argparse.SUPPRESS,
    )
    p_refine.add_argument(
        "--recovery-operation-id",
        default=None,
        metavar="UUID",
        help=argparse.SUPPRESS,
    )
    p_refine.add_argument(
        "--review-rejection-note-file",
        type=Path,
        default=None,
        help=argparse.SUPPRESS,
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

    # Prover tree: `marathon prover status` — read-only per-chapter proving
    # progress (verified / sorries-left / fill-targets + skeleton-done). The
    # PROVE loop itself is `marathon conductor run --fill-chapter N`.
    _add_prover_subparser(subparsers)


    # Audit tree: `marathon audit run/diff/show` — the Phase-5
    # elaborator-grade audit engine (see marathon.audit.engine).
    _add_audit_subparser(subparsers)

    # Deck tree: `marathon deck` — the Phase-8 human review surface (the
    # local dashboard web app; see marathon.deck). Ready cards in,
    # verify/reject/defer out, status pane streaming conductor/landing
    # events. verify/reject route through the committed review verdict
    # path (ledger + GitHub + tracker + daemon/conductor).
    _add_deck_subparser(subparsers)

    # Fill tree: `marathon fill` (single decl) and `marathon fill-file`
    # (every sorry in a file). Both wrap `refine_command` with a focus
    # directive so the slash commands can shell out without knowing the
    # focus-directive incantation.
    add_fill_subparsers(subparsers)

    # Repair tree: `marathon repair` — make a non-compiling module build again
    # via Aristotle (the third Aristotle pipeline; wraps refine with a
    # build-error directive, lands on main under `marathon/repair-c<N>`).
    add_repair_subparsers(subparsers)

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

    p_bfs = sub.add_parser(
        "backfill-shards",
        help=(
            "One-time: emit per-run metric shards from the existing "
            "wall-time.json + PromptLog.md (lossless, idempotent)."
        ),
        description=(
            "Reads the CURRENT .marathon/wall-time.json (its projects + "
            "build_only_seconds) and .marathon/PromptLog.md (its UUID "
            "lines) and writes one shard per project/run under "
            ".marathon/metrics/. After this, regenerating the rollups from "
            "(legacy + shards) reproduces today's content — nothing is "
            "lost. Idempotent: shard paths are keyed by project UUID, so "
            "re-running overwrites rather than duplicates. Run this once "
            "per consumer repo when upgrading to the shard-based metrics, "
            "then `marathon formalization aggregate-metrics` to verify the "
            "rollups round-trip. Offline: no Aristotle/network calls."
        ),
    )
    p_bfs.add_argument(
        "--repo-dir", type=Path, default=Path.cwd(),
        help="Repo root (must contain .marathon/). Default: current directory.",
    )
    p_bfs.set_defaults(func=_run_formalization_backfill_shards)

    p_agg = sub.add_parser(
        "aggregate-metrics",
        help=(
            "Aggregate every .marathon/metrics/ shard back into the "
            "wall-time.json + PromptLog.md rollups and refresh the ledger."
        ),
        description=(
            "The central rollup step the conductor runs after each "
            "successful job, exposed as a CLI for manual regeneration / "
            "verification. Folds every per-run shard (plus whatever is "
            "already in the two rollups) into .marathon/wall-time.json "
            "(sum/merge by project UUID) and .marathon/PromptLog.md "
            "(deduped, sorted lines), then imports them into the ledger. "
            "Offline: no Aristotle/network calls."
        ),
    )
    p_agg.add_argument(
        "--repo-dir", type=Path, default=Path.cwd(),
        help="Repo root (must contain .marathon/). Default: current directory.",
    )
    p_agg.set_defaults(func=_run_formalization_aggregate_metrics)


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
            "marathon-refine-state.json). Omit to import only "
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
        "--no-automerge",
        dest="automerge",
        action="store_false",
        default=True,
        help=(
            "Disable the per-cycle auto-merge of eligible open PRs onto the "
            "base branch (on by default; throttled to ~once/min — keeps "
            "infra/registration and green refine PRs on main)."
        ),
    )
    p_run.add_argument(
        "--merge-scope",
        choices=("any", "infra"),
        default="any",
        help=(
            "Scope of the per-cycle auto-merge — any (default) or infra "
            "(.marathon/ config PRs only; proof PRs stay deck-gated)."
        ),
    )
    p_run.add_argument(
        "--no-reconcile",
        dest="reconcile",
        action="store_false",
        default=True,
        help=(
            "Disable the auto-reconcile of issue signatures to the live code "
            "(on by default; fires only when HEAD moves). It ADDs uncited "
            "in-code result-key decls and replaces git-proven renames, and "
            "NEVER drops a stale citation without proof — the hands-off "
            "replacement for the manual 'refresh from code' button."
        ),
    )
    p_run.add_argument(
        "--no-reverify",
        dest="reverify",
        action="store_false",
        default=True,
        help=(
            "Disable the auto-un-verify of a verified issue when its reviewable "
            "content changes (on by default; wall-clock throttled, NOT "
            "HEAD-gated). When a verified issue's body adds a new card, or a "
            "frozen statement is changed / renamed / removed in code, the "
            "conductor drops the review:verified label, deletes the issue's "
            "state entry (so the deck re-surfaces it), and posts ONE comment — "
            "unchanged cards keep their verdicts. The approved-statements "
            "manifest is left frozen so later changes still diff."
        ),
    )
    p_run.add_argument(
        "--queue-from-github",
        action="store_true",
        help=(
            "Discover the rejection queue from the GitHub review:rejected "
            "label (recovering each note from its reject comment) instead of "
            "the local state.json. For a COLLABORATOR running their own "
            "conductor on their own clone: they see rejections fired on a "
            "shared deck and refine them under their own Aristotle key, with no "
            "state.json syncing. Off by default (local queue). Also settable "
            "via MARATHON_QUEUE_FROM_GITHUB=1."
        ),
    )
    p_run.add_argument(
        "--fill-chapter",
        type=int, action="append", default=None, metavar="N", dest="fill_chapters",
        help=(
            "Enable hands-off PROVING for chapter N (repeatable): the conductor "
            "dispatches `marathon fill` for every verified declaration that still "
            "has a `sorry` body, landing proof PRs. The statement-immutability "
            "guard ensures a fill can only fill bodies + add sub-lemmas, never "
            "change an approved statement. A chapter is only proved once its "
            "skeleton is done (every statement verified). Omit to disable proving."
        ),
    )
    p_run.add_argument(
        "--prove-frontier",
        action="store_true", default=False, dest="prove_frontier",
        help=(
            "Autonomous proving: prove EVERY registered chapter whose skeleton "
            "is done (all statements verified), auto-joining a chapter the moment "
            "it completes. The hands-off default — no per-chapter flag needed. "
            "Equivalent to --fill-chapter for every skeleton-done chapter, "
            "re-evaluated each tick. Composes with the skeleton-done gate."
        ),
    )
    p_run.add_argument(
        "--prover-branch", default="marathon/proving", metavar="BRANCH",
        help=(
            "The branch proofs land on (default: marathon/proving). Main stays "
            "in skeleton mode; every proof PR targets and only ever merges into "
            "this branch. Forked from origin/main if absent."
        ),
    )
    p_run.add_argument(
        "--repair-chapter",
        type=int, action="append", default=None, metavar="N", dest="repair_chapters",
        help=(
            "Enable autonomous REPAIR for chapter N (repeatable): when the human "
            "review queue is idle, the conductor dispatches `marathon repair` for "
            "every module that fails to build, fixing it against the current API "
            "while preserving statement meaning (build + faithfulness jury gated). "
            "Repairs land on `main` under `marathon/repair-c<N>`."
        ),
    )
    p_run.add_argument(
        "--repair-frontier",
        action="store_true", default=False, dest="repair_frontier",
        help=(
            "Autonomous repair across EVERY registered chapter: any module that "
            "stops building (e.g. a downstream chapter after an upstream API "
            "change) is auto-repaired. Self-healing; the systematic alternative "
            "to ad-hoc manual patches."
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


def _add_prover_subparser(subparsers) -> None:
    """`marathon prover status` — the read-only proving-progress surface.

    The prover itself is the conductor's ``--fill-chapter`` mode; this only
    reports per-chapter progress (verified statements, sorry-bodied decls
    remaining, live fill targets, and whether the skeleton is done so the
    chapter is provable)."""
    p_prov = subparsers.add_parser(
        "prover",
        help="Proving-progress surface (the loop is `conductor run --fill-chapter`).",
    )
    sub = p_prov.add_subparsers(dest="prover_command", required=True)
    p_stat = sub.add_parser("status", help="Per-chapter proving progress.")
    p_stat.add_argument(
        "--repo-dir", type=Path, default=None, metavar="PATH",
        help="Consumer repo root. Default: current directory.",
    )
    p_stat.add_argument(
        "--chapter", type=int, action="append", default=None,
        dest="chapters", metavar="N",
        help="Restrict to this chapter (repeatable). Default: every "
             "registered chapter.",
    )
    p_stat.set_defaults(func=_run_prover_status)


def _run_prover_status(args) -> None:
    from marathon.review.config import load_config
    from marathon.review.fill_discovery import prover_status

    cfg = load_config(getattr(args, "repo_dir", None))
    chapters = getattr(args, "chapters", None) or sorted(cfg.chapters or {})
    if not chapters:
        print("no chapters registered.")
        return
    rows = prover_status(cfg, chapters)
    print(f"{'ch':>4}  {'verified':>9}  {'skeleton':>8}  "
          f"{'sorries':>7}  {'targets':>7}  {'unproved':>8}")
    for r in rows:
        done = "done" if r.skeleton_done else "…churn"
        print(f"{r.chapter:>4}  {f'{r.verified}/{r.registered}':>9}  {done:>8}  "
              f"{r.sorries_left:>7}  {r.fill_targets:>7}  {r.unproved_decls:>8}")
    provable = [r.chapter for r in rows if r.skeleton_done]
    total_sorries = sum(r.sorries_left for r in rows)
    print(
        f"\n{total_sorries} sorry-bodied decl(s) across {len(rows)} chapter(s). "
        f"skeleton-done (provable): "
        f"{', '.join(map(str, provable)) if provable else 'none'}."
    )


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
        automerge=getattr(args, "automerge", True),
        merge_scope=getattr(args, "merge_scope", "any"),
        reconcile=getattr(args, "reconcile", True),
        reverify=getattr(args, "reverify", True),
        queue_from_github=getattr(args, "queue_from_github", False),
        fill_chapters=set(getattr(args, "fill_chapters", None) or ()),
        prove_frontier=getattr(args, "prove_frontier", False),
        prover_branch=getattr(args, "prover_branch", "marathon/proving"),
        repair_chapters=set(getattr(args, "repair_chapters", None) or ()),
        repair_frontier=getattr(args, "repair_frontier", False),
    )
    if rc:
        raise SystemExit(rc)


def _add_deck_subparser(subparsers) -> None:
    """Adds `marathon deck` — the Phase-8 deck (see ``marathon.deck``):
    the local-web-app review + operations dashboard. Serves ready cards
    (green SHA, gated, dep-ordered), routes verify/reject/defer through
    the committed review verdict path, and streams conductor/landing
    events to a live status pane.

    SAFETY: the server binds 127.0.0.1 ONLY and requires a per-session
    token on the irreversible POST /api/verdict; verify/reject fire only
    on a deliberate swipe, never on page load."""
    p_deck = subparsers.add_parser(
        "deck",
        help=(
            "Local review + ops dashboard (Phase 8): a 127.0.0.1 web app "
            "serving ready spec cards + conductor/issue/history views."
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
    p_deck.add_argument(
        "--demo", action="store_true",
        help=(
            "Demo mode: serve self-contained synthetic cards (no consumer "
            "repo / audit snapshot / GitHub needed). Shipping a subdeck is "
            "SIMULATED — fires nothing irreversible. For trying the UX."
        ),
    )
    p_deck.add_argument(
        "--trusted-origin", action="append", default=[], metavar="ORIGIN",
        help=(
            "Extra browser Origin the same-origin POST guard accepts "
            "(repeatable). Use when fronting the deck with an authenticating "
            "proxy on a public host, e.g. "
            "--trusted-origin https://marathon.example.com."
        ),
    )
    p_deck.add_argument(
        "--pull-every", type=int, default=0, metavar="SECONDS",
        help=(
            "For a HOSTED deck: fast-forward `git pull` the repo every SECONDS "
            "so the cards reflect the latest merged code (issues come live from "
            "GitHub regardless). Default: 0 = off (a local deck manages its own "
            "checkout). The gitignored audit snapshot is NOT pulled — the deck "
            "degrades to empty trust tiers without a local one (see --audit-from)."
        ),
    )
    p_deck.add_argument(
        "--audit-from", default=None, metavar="PATH_OR_URL",
        help=(
            "For a HOSTED deck: each --pull-every cycle, refresh the (gitignored) "
            "audit snapshot from this local path or http(s) URL — where a machine "
            "that runs `marathon audit` PUBLISHES its latest.json — so the deck "
            "shows fresh trust tiers without running Lean itself. Atomic + JSON-"
            "validated + fail-soft."
        ),
    )
    p_deck.add_argument(
        "--no-host-conductor", action="store_true",
        help=(
            "Do NOT spawn a conductor on this host when a reject is fired (sets "
            "MARATHON_NO_HOST_CONDUCTOR). For a SHARED deck (recommended with "
            "--trusted-origin): the rejection is recorded for a collaborator's "
            "OWN conductor to refine under their own Aristotle key. PRECONDITION: "
            "don't also run a host conductor here — an already-running one drains "
            "the shared queue under the host's credentials; this flag only stops "
            "THIS process from spawning one."
        ),
    )
    p_deck.set_defaults(func=_run_deck)


def _run_deck(args) -> None:
    import os

    from marathon.deck.server import serve

    demo = getattr(args, "demo", False)
    if demo:
        cfg = None  # demo mode serves synthetic cards; no consumer repo needed
    else:
        from marathon.review.config import load_config
        cfg = load_config()
    if getattr(args, "no_host_conductor", False):
        os.environ["MARATHON_NO_HOST_CONDUCTOR"] = "1"
    rc = serve(
        cfg,
        port=args.port,
        default_chapter=args.chapter,
        open_browser=not args.no_open,
        demo=demo,
        trusted_origins=getattr(args, "trusted_origin", []) or [],
        pull_every=getattr(args, "pull_every", 0),
        audit_from=getattr(args, "audit_from", None),
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


def _run_formalization_backfill_shards(args) -> None:
    """`marathon formalization backfill-shards` — turn the existing
    wall-time.json + PromptLog.md rollups into per-run metric shards."""
    from marathon.metrics import METRICS_DIR_RELPATH, backfill_shards

    repo_dir: Path = args.repo_dir.resolve()
    summary = backfill_shards(repo_dir)
    metrics_dir = repo_dir / METRICS_DIR_RELPATH
    print(
        f"backfilled metric shards into {metrics_dir}:\n"
        f"  PromptLog submissions: {summary['promptlog_shards']} shard(s)\n"
        f"  wall-time projects:    {summary['wall_shards']} shard(s)\n"
        f"  build-only seconds:    {summary['build_only_seconds']}"
    )
    if summary["promptlog_shards"] == 0 and summary["wall_shards"] == 0:
        print(
            "  (no PromptLog UUIDs or wall-time projects found under "
            f"{repo_dir} — is --repo-dir the consumer repo?)"
        )
        return
    print(
        "  next: `marathon formalization aggregate-metrics` regenerates "
        "the rollups from these shards (should reproduce today's content)."
    )


def _run_formalization_aggregate_metrics(args) -> None:
    """`marathon formalization aggregate-metrics` — fold every shard back
    into the rollups + refresh the ledger (the conductor's central step)."""
    from marathon.metrics import read_shards, regenerate_rollups

    repo_dir: Path = args.repo_dir.resolve()
    n_shards = len(read_shards(repo_dir))
    written = regenerate_rollups(repo_dir)
    print(f"aggregated {n_shards} shard(s) → rollups:")
    for p in written:
        try:
            rel = p.relative_to(repo_dir)
        except ValueError:
            rel = p
        print(f"  wrote {rel}")
    print("  ledger refreshed from the regenerated rollups.")


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
        "--auto-pr-branch",
        default=None,
        metavar="BRANCH",
        help=(
            "Explicit marathon-owned branch for --auto-pr. Used by the "
            "conductor when a stale remote per-issue ref must be preserved; "
            "normally derived automatically."
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
    elif args.command == "prover":
        # Dispatch via the subparser's set_defaults(func=…) handler — a
        # short-lived read-only proving-progress query.
        args.func(args)
    elif args.command == "audit":
        # Dispatch via the subparser's set_defaults(func=…) handler. All
        # subcommands are short-lived synchronous calls; `run`'s lake
        # subprocess dies with us on Ctrl-C.
        try:
            args.func(args)
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
    elif args.command == "repair":
        # `repair` set_defaults(func=_run_repair) — an async wrapper that builds
        # a build-error focus directive and delegates to `refine_command`.
        try:
            asyncio.run(args.func(args))
        except KeyboardInterrupt:
            print("\ninterrupted", file=sys.stderr)
            sys.exit(130)


if __name__ == "__main__":
    main()
