# Marathon v2 — synthesis plan

*Produced 2026-06-10 from a 16-agent review: 7 recon readers (core CLI, review
subsystem, plugin+SDK, GeometricAnalysis local + GitHub, autoform-bot,
Aristotle public capabilities), 3 independent designers (UX-first,
orchestration-first, verification-surface-first), and 6 adversarial critics.
This document is the reconciled synthesis. Recon artifacts:
`/private/tmp/claude-501/-Users-jack-Desktop-LEAN-marathon/45c18b02-be8e-45d5-aeb4-509598d37650/tasks/pieces/`.*

---

## 1. Diagnosis — what's actually wrong today (all verified in code or live data)

**Bugs (fix immediately, regardless of any redesign):**

- `_maybe_merge_marathon_pr` iterates `cfg.chapters` (a `dict[int, ChapterRegistry]`),
  yielding int keys, then calls `.chapter` on them → `AttributeError`, swallowed by
  `except Exception: return` (`review/review.py:279-285`). **No `verify` has ever
  auto-merged a PR or flipped a tracker emoji.** The "manually accept the GitHub PR"
  pain is partly this silent bug. Fixing it trips a latent `NameError` at
  `review.py:326` (`num` vs `issue_num`).
- Failed daemon iterations are marked "iterated" with no notification
  (`review/daemon.py:263-272`) — issue #49 accumulated **11 manual re-queue
  comments** because the human is the retry logic.
- `skeleton._extract_solution` does `rmtree` before validating the tar.
- Rater and hermes-steer prompts go through argv (E2BIG class the reviewer
  prompt already hit and fixed via stdin).
- `review/referee_queue.py` is dead code with a stale contract docstring.
- `cmd_list`/`cmd_next`/`verified_decls` issue one `gh issue view` per issue,
  serially — the latency the human feels on every interaction.

**Architecture problems:**

- **Seven state surfaces** (`marathon-state.json`, `marathon-refine-state.json`,
  `review/state.json`, `wall-time.json`, `PromptLog.md`, runner-locks,
  `config.toml` chapter registry) plus GitHub labels, with documented drift
  (state.json: 21 entries vs 29 verified labels; PR #74 exists solely to record
  one verify; the wall-time merge race just patched on this branch is the same
  disease). `state.json` is git-tracked and branch-sensitive — `--auto-pr`'s
  `checkout -B … origin/main` reverts it mid-flight.
- **Serialization everywhere:** single-flight per-chapter daemon; one rejection
  per refine iteration; every fix branch hard-reset to `origin/main` so queued
  reviews can never build on each other. GeometricAnalysis numbers: 66.7 h of
  strictly serialized Aristotle wall time; overnight daemon PRs wait 5–13 h for
  the human's morning; ~⅓ of all issue comments are operational noise.
- **No hard gate:** `[build:FAIL]` is a title string the human eyeballs. Six
  merged PRs carry it; main was red Jun 6–10 (the #50 `SmoothCovectorField`
  reshape broke Ch.12 for four days; the fix arrived as a side effect of an
  unrelated iteration). The 7-axis rater is advisory; nothing has pass/fail
  semantics.
- **Unused Aristotle surface:** the SDK is fully async; `Project.list_projects`
  implies fleet concurrency (limits undocumented — must confirm); `percent_complete`
  never surfaced; `Project.create(tar_file_path=…)` (cached bundles) unused;
  `task.cancel()` unused; event stream acted on only for EDITING_FILE.
- **No statement-level data model:** marathon's unit is the chapter
  (`order.txt`); there is no machine answer to "is theorem X done?" beyond
  `lake build` + subjective ratings. autoform-bot has the vendorable pieces
  (targets.yaml, matcher, axiom check, dep-graph metaprogram with deception
  tags, jury rubrics, merge-queue pattern).

**What is load-bearing and must survive:**

- `aristotle_runtime.py` (reattach, `ask()` continuations, budget semantics,
  steering) — the battle-tested asset.
- The information firewall: Claude never sees the `.tex` (copyright
  confinement); Aristotle never sees `referee.md`.
- The reject-notes-verbatim-to-Aristotle bypass (`refine.py:128-165`) — Claude
  was removed from rejection prompts because it substituted its own agenda.
- The review-card body format and the GitHub-issues-as-durable-record model.
- referee.md's human-header / machine-tail split.

---

## 2. Target architecture

```
                 ┌───────────────────────────────────────────────┐
                 │ marathon deck (TUI, last phase) — the human    │
                 │ seat: spec cards in, verify/reject/defer out   │
                 └──────────────────┬────────────────────────────┘
                                    │
                 ┌──────────────────▼────────────────────────────┐
                 │ LEDGER  .marathon/marathon.db (SQLite, WAL,   │
                 │ gitignored) + tracked append-only verdict     │
                 │ JSONL (merge-friendly, like wall-time v2)     │
                 │ targets · tasks(DAG) · jobs · verdicts ·      │
                 │ fingerprints · events · wall-time             │
                 └───┬───────────────┬──────────────────┬────────┘
        two-way sync │          jobs │                  │ facts
   ┌─────────────────▼──┐   ┌────────▼─────────┐   ┌────▼───────────────┐
   │ GitHub projection   │   │ CONDUCTOR        │   │ AUDIT ENGINE       │
   │ issues/labels/PRs   │   │ one daemon, N    │   │ lake exe audit:    │
   │ (display + inbound  │   │ async jobs in    │   │ axioms, sorries,   │
   │ verdict channel)    │   │ git worktrees    │   │ deception tags,    │
   └─────────────────────┘   └────────┬─────────┘   │ fingerprints, cones│
                                      │             └────────────────────┘
              ┌───────────────────────▼───────────────────────┐
              │ WORKERS (existing roles, parallelized):        │
              │ Aristotle proves (steered by hermes_watcher);  │
              │ Claude drafts / steers / rates / juries /      │
              │ spec-audits / referees — always pure calls     │
              └───────────────────────┬───────────────────────┘
              ┌───────────────────────▼───────────────────────┐
              │ LANDING QUEUE: batch onto marathon/next →      │
              │ lake build + gate → main fast-forwards green   │
              └────────────────────────────────────────────────┘
```

**Six rulings reconciled from the designs + critiques:**

1. **One ledger.** SQLite (WAL) is the sole runtime truth; GitHub becomes a
   *two-way* projection (outbound idempotent writes; inbound reconciliation of
   comment-thread verdicts — the operator demonstrably drives verdicts from
   GitHub). A tracked append-only verdict-export JSONL keyed by
   `(decl, fingerprint, ts)` preserves git provenance (PR #74 proves it's wanted)
   and is merge-friendly by the same write-once-keyed pattern as the wall-time
   v2 sidecar. Machine evidence is derived cache — recomputed, never merged.

2. **One Conductor, deterministic.** A repo-level daemon (evolution of
   `review/daemon.py`) dispatching N concurrent jobs, each running the existing
   refine loop in its own git worktree. Claude is never the scheduler —
   scheduling/retries/merges must be auditable and crash-safe; Claude plans and
   reformulates as pure calls. Requeue-with-backoff + notification replaces
   mark-iterated-and-forget. Startup reconciles orphans via `list_projects`.
   **Concurrency N is an empirical number** — Harmonic's ToS caps concurrent
   sessions at an undocumented limit; soak-test 2 before sizing, handle 429s,
   and add a Claude-call semaphore (N jobs × whole-repo prompts × steering all
   bill one Max session).

3. **Machine faithfulness judging is out; everything else gates.** Both
   feasibility critics independently converged: autoform's faithfulness jury
   (w=0.4) requires reading the book, the firewall forbids it, and grading
   LLM-rendered statements against LLM renderings is circular. **Faithfulness
   stays human** — that's what the spec card is for. The machine gate =
   `lake build` + matcher (target ↔ decl) + `#print axioms` whitelist + sorry
   accounting + deception tags + probes + (advisory→enforcing)
   proof_integrity/code_quality jury. Gates are **mode-aware** (skeleton mode:
   sorries expected, statement quality gated; proof mode: proof integrity
   gated) and **overridable** (`--gate-override`, ledger-annotated — PR #99's
   red-but-wanted deletion shows cross-chapter refactors necessarily transit
   red).

4. **Trust is computed, never stored.** Tiers mapped onto the existing emoji
   ladder: T0 builds → T1 machine-audited (axiom-clean, sorry-accounted, no
   deception tags, probes pass; **nothing below T1 reaches a human**) → T2
   spec-audited (human read the trust kernel) → T3 line-by-line reviewed.
   A T2/T3 verdict is pinned to a statement fingerprint plus its cone's
   fingerprints; any upstream meaning change automatically degrades the tier
   and tells the human exactly which card to re-read. Critical corrections from
   review: fingerprint **project-local constants only** (a Mathlib bump must
   not nuke 29 verified decls — provide a re-pin amnesty command); theorems
   fingerprint by elaborated type, **defs by value** (type-only is unsound for
   definitions); `tier: unknown` when elaboration fails; T3 additionally pins
   proof bodies.

5. **Trust kernel = the minimized human-read surface (goal 2's actual
   mechanism).** kernel(T) = normalized statement of T + every project-local
   definition in its transitive *statement* cone (Mathlib = trusted vocabulary;
   proof bodies never included). Kernel size (count + LOC) is a tracked metric
   in formalization.yaml. The spec-auditor Claude role shrinks kernels before
   humans see them — but **kernel-shrinking claims must be mechanically
   certified** (`example : myDef = Mathlib.thing := rfl` probes), otherwise
   trust is moved, not shrunk. Spec cards show: statement, kernel defs, fresh
   informal rendering (regenerated on every fingerprint change), probe/axiom
   evidence, and on change a semantic delta
   (strengthened/weakened/equivalent/meaning-changed — advisory, never gating).
   Probes ship in order of cheapness: unfolding tests and sanity instances
   (pure Lean, kills the PUnit-collapse trap) first; Aristotle vacuity probes
   (prove `¬hypotheses` — targets the documented typo-exploit failure mode)
   later, with budget caps, dedup keys, and a circuit breaker before any
   auto-filed rejection.

6. **One machinery, two modes.** `gate_policy ∈ {auto, human}` per target.
   Hands-off mode = Planner fills the ledger with `auto` rows; jury-passed work
   lands automatically; humans sign spec digests over milestone cones.
   Review mode = today's per-declaration ceremony with a higher floor (only
   T1-passing cards served). Mixed mode is free: Stokes-critical declarations
   `human`, scaffolding `auto`. Stacking policy respects the gate: `auto` work
   stacks on `marathon/next`; `human`-gated work bases on main until rebase
   automation has a rollback story for mid-stack rejection.

**Escalation ladder** (replaces burn-the-rejection): Aristotle attempt →
`ask()` continuation → gate feedback → Claude reformulation (restate, split
into bridging lemmas as new DAG tasks) → human escalation with attempt history
pre-filled. Hard rule from review: **reformulation is forbidden on
human-verified statements** — any signature change auto-demotes the tier and
re-queues review instead.

**Referee with teeth** (the weakest goal in all three designs — committed
here): inputs gain the ledger (verdict history, gate failures,
N-iterations-overdue items); output gains **fix-tasks injected into the DAG
with `depends_on` blocking** that the Conductor will not schedule around (the
Ch.11 `coordinateCoframe` item survived twelve iterations of advisory
escalation rules); a self-accountability pass (re-read previous standing items
against the ledger; confirm or escalate); cross-chapter duplication detection
fed by the dep graph; cadence by landings-count, not manual runs. Conventions
continue flowing upstream into drafter prompts via referee.md as today.

**Firewall policy becomes per-project config.** For copyrighted sources
(GeometricAnalysis): extraction cannot use Claude-reads-the-book; targets come
from Aristotle-side runs or human-supplied informal statements, and the
"LLM-rendered, verification pending" honesty marker stays. For open sources:
vendor autoform's extraction (chunk → k-extractor consensus → merger) directly.

---

## 3. Roadmap

Every phase ships independently; GeometricAnalysis keeps working throughout
and is the acceptance test (Ch.12's 7 unreviewed sub-issues exercise the
queue; Ch.15/16's 36-declaration bootstrap debt exercises planner-seeded
intake).

| Phase | Contents | Effort |
|---|---|---|
| **0 — hygiene (days)** | Fix verify/merge AttributeError + NameError; requeue-with-backoff + notification in daemon; safe tar extraction; stdin for rater/steer prompts; delete `referee_queue.py`; batch GraphQL reads; surface `percent_complete` during polls. **Empirically determine Aristotle concurrency** (2–3 trivial concurrent projects) and re-capture the missing dashboard doc pages (pricing/limits/tips). | S |
| **1 — ledger** | `marathon/ledger.py` (SQLite WAL) behind a shim under `state.py`/`review/state.py`; one-shot import of all seven state surfaces; dual-write for a full chapter before cutover; tracked verdict-export JSONL; two-way GitHub sync; update bootstrap/audit briefings to stop hand-editing config.toml. | M |
| **2 — gate v1** | `marathon/gate.py`: build + axiom whitelist (existing `--check-axioms` machinery) + sorry accounting + forbidden-keyword scan; mode-aware; warn-only for two weeks, then enforcing on PR-open (never on landing of human-demanded overrides). Jury (proof_integrity/code_quality, **no faithfulness**) advisory. | M |
| **3 — conductor** | Repo-level daemon dispatching existing `marathon refine` subprocesses in worktrees, concurrency from phase-0 measurement (start 2); crude file-overlap collision check before dispatch (Lean dep graph replaces it later); Claude-call semaphore; orphan reconciliation; cancel lever. Per-chapter daemons retire. Metadata files (`formalization.yaml`, `PromptLog.md`) move to Conductor-side regeneration — never committed by workers (generalizes the wall-time fix). | L |
| **4 — landing queue** | `marathon/next` integration branch; batched bors-style landings; conflicts → Claude mechanical rebase, else fresh resubmit with conflict diff (never `ask()` — the server-side bundle is stale); auto fix-tasks for post-merge breakage (the #50→Ch.12 sweep becomes same-night machine work); display PRs kept with existing body format. | M |
| **5 — audit engine + tiers** | `lake exe MarathonAudit` building inside the target repo's workspace (toolchain-pinned; **XL, not a vendoring job** — autoform's 232-line script computes no fingerprints); fingerprint stability test suite is the ballgame; tiers computed on read; cone invalidation + amnesty command; backfill 29 verified decls pinned to *current main* with one human attestation pass (their verdict-time SHAs include red builds — unbuildable). | XL |
| **6 — spec cards + probes** | spec-auditor role + `prompts/spec_audit.md`; kernel computation + size metric; certified kernel-shrinking; semantic-delta comments (idempotent, batched tracker rewrites); pure-Lean probes, then budget-capped vacuity probes. | M/L |
| **7 — planner intake** | `marathon plan` (point at axiom / repo sorries / textbook); targets ledger rows + dependency edges; firewall-aware extraction path; `order.txt` demoted to legacy importer; bootstrap-chapter seeds sub-issues from ledger instead of LLM re-reading every file. | M/L |
| **8 — deck (Code Tinder)** | Textual TUI over the now-stable substrate: ready cards only (green SHA, gated, dep-ordered), v/r/space/d/o keys, reject notes verbatim-to-Aristotle, `o` opens today's coreviewer chat as the deep-dive escape hatch, status pane streams job events. Explicit prefetch-depth target to mitigate reject→refill latency (a reject costs one 20–45 min Aristotle round; keep the queue deep enough that the human always has ready cards). | L |

Phases 2–4 can interleave with 5 (the audit engine is long-lead; start it
early in the background).

---

## 4. Explicitly not building (consensus across all three designs)

- **No rewrite, no adoption of autoform-bot's coordinator** (ZMQ/SLURM/trace-resume
  orchestrator/visualizer). Vendor its four self-contained ideas: targets
  schema, matcher, axiom/deception checkers, merge-queue pattern.
- **No LLM scheduler.** Conductor is deterministic Python; Claude is a pure
  function everywhere.
- **No worker racing** (3–5 Aristotle jobs per task): multiplies real dollar
  cost for a prover whose failures are statement-quality, not luck. Parallelize
  *across* targets.
- **No web app / hosted service** — one operator, local keys; TUI over SQLite.
  GitHub remains the shareable surface. Revisit if multi-reviewer becomes real.
- **No auto-verify.** The jury gates what humans *see*; it never substitutes
  for the verify keystroke. Minimize the surface, never the authority.
- **No formal spec-equivalence checking across refactors** — undecidable in
  practice and falsely reassuring; fingerprint-invalidate-and-reswipe is honest.
- **No Aristotle self-certification of faithfulness** — it's the audited party
  and the only actor that sees the `.tex`.

---

## 5. Goal coverage

| Goal | Mechanism |
|---|---|
| 1. Claude+Aristotle | Role table (drafter/steerer/rater/jury/spec-auditor/referee vs prover); escalation ladder gives Claude the reformulation job; Conductor stays Python. |
| 2. Hands-off + minimal surface | gate_policy=auto + hard gate + trust kernel + spec digests + kernel-size metric; faithfulness explicitly human; probes hunt misformalization actively. |
| 3. Per-declaration review | Unchanged ceremony, higher floor (T1 prerequisite), verify actually merges (phase 0), reject-bypass preserved, T3 tier; coreviewer chat is the deck's deep-dive. |
| 4. Long-timescale referee | Ledger-fed, fix-task-emitting, self-accountable, dedup-detecting, cadence-scheduled; conventions still flow upstream via referee.md. |
| 5. Seamless UX | Conductor concurrency + landing queue + dep-aware ready cards + deck; requeue/notification kills human-as-retry-logic; two-way sync kills state-sync PRs. |

## 6. Open questions to resolve early

1. Aristotle concurrent-session limit and per-project budget pricing (ask
   Harmonic / measure) — sizes the Conductor.
2. Claude Max concurrent `claude -p` limits under N parallel jobs — sizes the
   semaphore; consider making `MARATHON_REVIEW_CONTEXT_PATHS` trimming the
   default at scale.
3. Fingerprint stability across pretty-printer/toolchain versions — the
   phase-5 test suite; project-local-only scoping is the mitigation.
4. Whether human-gated work can ever stack safely (mid-stack rejection
   rollback) — start conservative: auto stacks, human bases on main.
