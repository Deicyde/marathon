# Marathon v2: One Conductor, Two Provers' Worth of Roles

## 1. Target architecture

### The org chart

Marathon v2 is organized around a single long-running **Conductor** process per repo (evolved from `marathon/review/daemon.py`) that owns all state and dispatch. Every intelligent actor is a stateless contractor it calls:

| Role | Actor | Contract (in → out) | Today's code |
|---|---|---|---|
| **Conductor** | Python daemon | ledger + events → dispatch decisions, git/GitHub mutations | `review/daemon.py` (single-flight, per-chapter) → one multi-flight daemon |
| **Planner** | Claude (`claude -p`) | textbook/repo/axiom pointer → task DAG rows | new; absorbs autoform extraction |
| **Drafter** | Claude | task + context bundle → Aristotle prompt (verbatim) | `claude_review.review_and_draft_prompt` |
| **Prover** | Aristotle | (bundle, prompt) → tarball; `ask()` continuations | `aristotle_runtime.py` (keep, parallelize) |
| **Steerer** | Claude | event window → `{steer, prompt}` JSON | `hermes_watcher.py` (keep) |
| **Gatekeeper** | Python + Claude jury | extracted diff → pass/fail + structured feedback | new `marathon/gate.py`; replaces advisory `--auto-rate` as the gate (rater survives as diagnostics) |
| **Referee** | Claude, long-timescale | whole repo + ledger history → standing-items, style fix-tasks, digest review | `referee.py` (keep, add fix-task output) |
| **Human** | you | review card / spec digest → verify \| reject(notes) | `review/` CLI (keep verbs, fix plumbing) |

Two invariants make this an org chart rather than a pile of scripts. **(1) Claude is always a pure function**: context in, text/JSON out, no tool access, no memory beyond what Python feeds it — this is already true and is marathon's best property; keep it. **(2) Only the Conductor mutates state**: ledger writes, git operations, GitHub comments/labels all flow through one process, killing the races documented in the GeometricAnalysis logs (verdict/iteration timestamp collisions, duplicate reject comments, stranded iterations).

### State model

Collapse the seven state surfaces (`marathon-state.json`, `marathon-refine-state.json`, `review/state.json`, `wall-time.json`, `PromptLog.md`, runner-locks, the config.toml chapter registry) into **one git-ignored SQLite ledger** at `.marathon/marathon.db`:

- **`targets`** — per-declaration rows (autoform's `FormalizationTarget` schema: name, kind, source location, lean_file, decl hash), with status ladder mirroring the tracker emojis (🔴→🟠→🟡→🔵).
- **`tasks`** — units of Aristotle work: `kind` (skeleton/fill/rejection-fix/style-fix/reformulate), `depends_on` edges, `gate_policy` (auto|human), attempt count, `project_id`/`agent_task_id` (reattach survives crashes, as today), wall-time.
- **`verdicts`** — human and jury verdicts with timestamps; GitHub labels/comments become a *projection* of this table (one-way sync), ending the labels-vs-state.json-vs-config.toml drift that `open_session.py:100-117` renders side-by-side because neither is authoritative.

JSONL logs (ratings, steering) stay as-is. The ledger being untracked dissolves bug class #3 of the review report: `checkout -B … origin/main` can no longer revert the queue mid-flight, deleting the prefetch workaround at `refine.py:1195-1210`.

### Task lifecycle and the escalation ladder

Every task walks one state machine, with failure handled by *role escalation*, not retry-of-the-same-thing:

1. **Aristotle attempt** — drafter prompt → submit → poll. `COMPLETE_WITH_ERRORS`/`OUT_OF_BUDGET` → one `ask()` continuation (today's behavior).
2. **Gate** — extract into the task's worktree, `lake build`, matcher locates the target decl, `#print axioms`, jury rubrics (faithfulness/proof_integrity/code_quality with thresholds). Fail → structured feedback.
3. **Claude reformulates** — a new drafter mode fed the gate feedback, *allowed to change the task*: restate the lemma, split into bridging lemmas (new ledger tasks with `depends_on`), or flag the statement as suspect. This is the step marathon lacks: today a failed focused fix just burns the rejection (`daemon.py:263-272`) and waits for a human to notice.
4. **Human escalation** — after N reformulations, auto-file/refresh a review sub-issue with the jury feedback and attempt history pre-filled. The human's reject notes re-enter at step 1 verbatim (preserving the load-bearing Claude-bypass at `refine.py:949-965`).

### One machinery, two modes

Hands-off vs. per-declaration review is **one column, not two codebases**: `gate_policy`. In hands-off mode, jury-passed tasks merge automatically through the merge queue and humans only sign **spec digests** (below). In review mode, every task's merge gate is a human `verify`. `/marathon:review` keeps working unchanged on top; "point at a textbook" is just the Planner filling the ledger with `gate_policy=auto` rows. Mixed mode is free: mark Stokes-critical declarations `human`, scaffolding `auto`.

## 2. Highest-leverage changes

**C1. The ledger** (`marathon/ledger.py`; rewrites `state.py`, `review/state.py`; touches `refine.py`, `skeleton.py`, `daemon.py`, `formalization.py`). One SQLite file with WAL mode kills the unlocked read-modify-write races, the branch-sensitivity of tracked state, the wall-time merge race you just patched, and gives marathon a per-*statement* data model — the prerequisite for everything below. **Effort: M.**

**C2. Concurrent worker pool with worktree isolation** (`review/daemon.py` → `marathon/conductor.py`; `aristotle_runtime.py` mostly unchanged — it's already async-shaped). The Conductor runs N Aristotle tasks via `asyncio.gather`, each extracting into its own `git worktree` (borrowed from autoform's worker isolation), ending the shared-git-index contention and the "wipe before validation" landmine (`skeleton.py:258-262` — extraction now happens in a disposable worktree, validated, *then* applied). Reconcile orphans on startup via the never-used `Project.list_projects`; surface `percent_complete`; wire `task.cancel()` as a runaway-budget lever. The GeometricAnalysis numbers justify this alone: 66.7h of serialized prover wall time, 20–45 min per run, one rejection at a time. **Effort: L.**

**C3. The hard gate** (`marathon/gate.py`, vendoring autoform's matcher, axiom checker, and jury rubrics — the `autoform:eval-rubrics` skill already bridges the vocabularies; wires into `post_pipeline.py` replacing the rate step as a gate). "Build passed" becomes "target matched, axiom-clean, jury-passed." Three concrete wins: `[build:FAIL]` PRs never open (5 of the last 10 main-branch commits were red); junk iterations like PR #81 die pre-PR instead of costing a human round-trip; jury feedback strings become machine-generated reject notes feeding the escalation ladder. Inherited-failure analysis prevents re-rejecting every downstream decl for one upstream sorry. **Effort: M.**

**C4. Fix verify plumbing + a merge queue** (`review/review.py`, `post_pipeline.py`). First, the two known bugs: `_maybe_merge_marathon_pr` iterates dict keys (`review.py:279`, AttributeError swallowed at `:284`) so *no verify has ever auto-merged or flipped the tracker*; fixing it trips the `num`/`issue_num` NameError at `:326`. Then replace per-issue-branch-reset-to-`origin/main` with a Conductor-owned integration branch and a small bors-style queue (autoform's merge-queue pattern): verified work lands in order, queued tasks branch from the integration head so **reviews can finally stack** — the #50→Ch.12 four-day red-main carryover becomes a queue rebase + auto-spawned fix-task instead of collateral damage. Force-push-per-iteration PRs survive as the human-facing diff view. **Effort: S (bugs) + M (queue).**

**C5. Planner intake** (`marathon/planner.py`, new `marathon plan` verb; demotes `order.py`/`order.txt` to a legacy importer). Point at a textbook: run autoform's chunk/k-extractor/merger consensus pipeline (vendored, or shell to `/autoform:extract`) → `targets` rows; Claude planner adds dependency edges (chapter graph + decl-level imports) and emits skeleton tasks per chapter followed by per-target fill tasks. Point at a repo: scan sorries → one task each (generalizing `fill-file`'s `_find_sorries_in_file`). Point at an axiom: one target. The firewall survives: only Aristotle bundles see the `.tex`; the extractor output (`targets.yaml`-equivalent rows) is the Claude-visible artifact. **Effort: M.**

**C6. Spec digests — the minimal human surface** (`marathon/digest.py` + prompt; new `marathon digest` verb and review-card section). Claude generates, per chapter or per milestone, a signed-off document containing *only* the load-bearing definitions and theorem statements (computed from the dependency cone of the milestone target, e.g. `Thm_16_11`), annotated with deception-tag alerts (`vacuous_body`, `ignores_params`) from autoform's Lean metaprogram. Human signs the digest → all covered statements flip to verified in the ledger. This is goal 2's mechanism: in hands-off mode you read 2 pages, not 26 files; scaffolding below the digest line is jury-trusted. **Effort: M.**

**C7. Referee with teeth** (`referee.py` + one new output type). The referee keeps its human-header/machine-tail split and its prompt, but gains the ledger as input (verdict history, gate failures, twelve-iterations-overdue items) and may emit **style fix-tasks** directly into the DAG instead of only standing-items prose. The Ch.11 `coordinateCoframe` saga — twelve iterations of escalation rules that "fire but change nothing" — becomes a top-priority task with `depends_on` blocking, which the Conductor *will not schedule around*. Referee runs on a wall-clock cadence (Conductor cron), not `--auto-referee-every`. **Effort: S.**

**C8. Review UX: the swipe queue** (`review/cli.py`, `review/open_session.py`). Batch all GitHub reads into one GraphQL call (kills the N+1 `gh issue view` loops in `cmd_list`/`cmd_next`/`verified_decls.py`); make verdict posting idempotent (dedupe the 4-rejects-in-6-hours noise); `marathon review session` streams cards continuously — show card → `v`/`r notes…`/`s`kip → Conductor handles dispatch, stacking, merge. With C2 running fixes concurrently and C4 stacking them, the human's morning is: read cards, swipe, done. Move rater/steerer prompts to stdin while in there (the known `E2BIG` class). **Effort: M.**

## 3. Migration path

Build order is chosen so `marathon refine`, `fill`, `skeleton`, and `/marathon:review` work unchanged at every step:

1. **Week 0 — bug fixes on current architecture** (C4-bugs, stdin fixes, poll backoff cap in `aristotle_runtime._poll_task_loop`). Pure wins, zero risk; verify finally merges PRs on GeometricAnalysis.
2. **Ledger behind a shim** (C1). `ledger.py` lands with adapters so `state.py`/`review/state.py` callers read/write through it; legacy JSON files are imported once, then become export-only (PromptLog.md stays as a human-readable projection). All existing verbs untouched above the shim.
3. **Gate as advisor, then enforcer** (C3). Wire `gate.py` into `post_pipeline.py` alongside the rater, reporting only; after calibrating thresholds against the existing 42 verified issues (a labeled dataset you already own), flip it to blocking PRs.
4. **Conductor v1** (C2). It initially dispatches the *same* `python -m marathon refine …` subprocesses the daemon does today — just N at once, in worktrees, reading the ledger. `refine.py`'s loop is untouched; only its working directory and its caller change. Per-chapter daemons retire; `cmd_reject` queues to the ledger.
5. **Merge queue** (C4-queue), then **Planner + digests** (C5, C6) — hands-off mode goes live on a fresh textbook target while GeometricAnalysis continues in review mode on the same binary.
6. **Referee fix-tasks** (C7) and **swipe UX** (C8) last; both are additive.

GeometricAnalysis is the canary throughout: Ch.12's 7 unreviewed sub-issues exercise the new queue; Ch.15/16 bootstrap (36 declarations of debt) exercises Planner-seeded sub-issue creation (integration opportunity #4 from the autoform report).

## 4. Explicitly NOT building

- **No rewrite.** `aristotle_runtime.py`, the prompt library, `hermes_watcher.py`, and the review-card format are battle-tested; the failures are coordination-layer, and that layer is ~2k of the 11k lines.
- **No ZMQ/SLURM multi-node or worker racing.** Aristotle *is* the compute cluster; the local box only coordinates. asyncio in one process covers realistic concurrency (API terms cap concurrent sessions anyway), and racing 3–5 Aristotle jobs per task multiplies a real dollar cost for a prover that usually succeeds or fails on statement quality, not luck. Parallelize *across* targets instead.
- **No mounting marathon under autoform's coordinator** (its Mode-B inversion). Vendoring extraction/matcher/jury/merge-queue (~4 self-contained modules) is far cheaper than adopting trace-resume orchestrator agents, ZMQ dispatch, and the visualizer — and keeps the firewall and GitHub-native human gate that autoform lacks.
- **No LLM orchestrator agent.** autoform uses a persistent Opus planner with 1M context; marathon's Conductor stays deterministic Python over a ledger. Claude plans (C5) and reformulates (escalation step 3) as *pure calls*; scheduling, retries, and merges must be auditable and crash-safe, which agent loops are not.
- **No web dashboard/TUI.** GitHub issues, PR bodies, and the swipe CLI are the UI; the EventWatcher's anticipated TUI stays unbuilt until coordination is solid.
- **No event-push infrastructure.** Polling with backoff is fine at this scale; fix the unbounded-retry bug, don't build webhooks.
- **No autonomous merges to a protected main in review mode.** The human verdict stays the only path to main for human-gated targets — that asymmetry *is* the product. Hands-off auto-merge applies only to `gate_policy=auto` rows below a signed digest.