# Marathon v2: "Code Tinder" for Lean autoformalization

**Design principle:** the human's only job is reading mathematics and rendering verdicts. Every second the human spends on git, queues, retries, state files, or waiting for a build is a design failure. We design backwards from a deck of ready-to-judge cards and make the machine layer absorb everything else.

---

## 1. Target architecture

```
                ┌─────────────────────────────────────────────┐
                │  marathon deck  (TUI — the only human seat)  │
                │  card ⇄ verify / reject+note / defer / dive  │
                └──────────────┬──────────────────────────────┘
                               │ reads/writes
                ┌──────────────▼──────────────────────────────┐
                │  LEDGER  .marathon/marathon.db (SQLite)      │
                │  declarations · issues · jobs · verdicts ·   │
                │  dep edges · SHAs · wall-time · events       │
                └───┬──────────────┬───────────────┬──────────┘
        one-way sync│         jobs │               │ dep facts
   ┌────────────────▼───┐  ┌───────▼────────┐  ┌───▼──────────────┐
   │ GitHub projection   │  │ ORCHESTRATOR   │  │ DEP ENGINE        │
   │ (issues/labels/PRs  │  │ (one daemon,   │  │ Lean decl graph + │
   │  = durable mirror)  │  │  N async jobs) │  │ issue↔decl matcher│
   └─────────────────────┘  └───────┬────────┘  └───────────────────┘
                          worktrees │ per job
              ┌─────────────────────▼─────────────────────┐
              │ WORKERS: Aristotle (prove/edit, steered by │
              │ hermes_watcher) · Claude (draft/rate/jury/ │
              │ referee) — existing roles, unchanged       │
              └─────────────────────┬─────────────────────┘
                                    │ candidate diffs
              ┌─────────────────────▼─────────────────────┐
              │ LANDING QUEUE (bors-style)                 │
              │ cherry-pick → lake build → eval gate →     │
              │ land on marathon/next; main fast-forwards  │
              └────────────────────────────────────────────┘
```

**Three load-bearing decisions:**

**(a) One integration branch, not N per-issue branches.** Today every fix lives on `marathon/refine-c<N>-i<issue>` hard-reset to `origin/main` (`post_pipeline.py:497-741`), so queued reviews can't build on each other, `[build:FAIL]` lands on main, and #50's signature change broke Ch12 for four days. v2: all machine work lands serially on `marathon/next` through a merge queue that *requires* `lake build` green plus the verified-decl audit as a hard gate. `main` fast-forwards from `next` automatically when green (or on a human keystroke in the deck). Branch topology stops encoding review state entirely — **verification state lives in the ledger keyed to (declaration, SHA)**, which is the correct unit: the human verifies statements, not diffs. This is the single change that unlocks concurrency, kills the force-push/reset/state.json-wipe hack family (`refine.py:1195-1210`, `post_pipeline.py:533-571`), and makes red-main impossible by construction.

**(b) One ledger, not three drifting state surfaces.** `state.json` + labels + `config.toml` chapter registry drift today and `state.json` is git-tracked and branch-sensitive (review report §6.3). v2: a gitignored SQLite ledger is the sole source of truth; GitHub issues/labels/tracker-emoji become a one-way projection refreshed by an orchestrator job (batch GraphQL, killing the N+1 `gh` calls in `review.py:49-74`). GitHub remains the durable, linkable, human-readable record — we keep the excellent review-card body format verbatim.

**(c) One orchestrator daemon, many concurrent jobs.** Replace the per-chapter single-flight daemon (`review/daemon.py`) with one repo-level asyncio orchestrator. Each job = (kind: refine/fill/skeleton-chapter/referee, focus, prompt source) executed in its **own git worktree**, so N Aristotle projects run concurrently (the SDK is fully async; `list_projects` implies fleet concurrency; `backfill-wall-time` already proves concurrent API calls work). Aristotle wall time is 20–45 min/run — at concurrency 4–6, the 7 Ch.12 issues that waited 2 days become an overnight batch.

**State model.** Ledger tables: `declarations` (name, file, chapter, status ladder 🔴→📘, verified_sha), `issues` (gh number ↔ decl set), `jobs` (id, kind, focus, project_id/task_id, status, worktree, attempt), `verdicts` (issue, verdict, note, ts, sha), `deps` (decl→decl edges from Lean; issue→issue derived), `landings` (job → next-SHA), `wall_time` (project-keyed, absorbing `.marathon/wall-time.json`). Per-job artifacts (`marathon.md`, refine log, steering log) stay as files in the worktree, archived per job. `marathon-refine-state.json`'s single mutable record (only-latest-reattachable) is replaced by the jobs table — every in-flight project is reattachable and `list_projects` reconciliation finds orphans.

---

## 2. Highest-leverage changes

### C1. Orchestrator daemon — `marathon orchestrate` (L)
**What:** new `marathon/orchestrator/` (scheduler, job runner, worktree pool, GitHub sync, ledger). Job runner is `refine.py`'s attempt loop refactored into a callable (it's already 90% there: `refine.py:862-1056`), reusing `aristotle_runtime.py` unchanged. Semaphore-limited Aristotle concurrency (config, default 4). Dispatch rule: a rejection's job starts immediately on rejection; chapters' skeleton/fill jobs run opportunistically when the queue has slack. SIGTERM-safe: jobs record `project_id` before submit and reattach on restart (existing pattern, `refine.py:446-510`). Failed jobs **requeue with backoff** instead of being marked "iterated" (`daemon.py:263-272` — the bug behind issue #49's eleven manual re-queue comments).
**Touches:** new package; deletes `review/daemon.py` logic (keep verb as alias); `refine.py` (extract `run_refine_job()`); `state.py`.
**Why:** removes the human-as-retry-logic role and the 1-job-at-a-time ceiling — the two biggest throughput losses in the GeometricAnalysis logs.

### C2. Landing queue + integration branch (M)
**What:** new `marathon/landing.py`. A job's output is committed in its worktree, then queued: cherry-pick onto `marathon/next` in a dedicated landing worktree → `lake build` (cached toolchain) → verified-decl audit as a **hard reject** (not the soft warning in `post_pipeline.py:1176-1250`) → eval gate (C5) → push. Conflict or red build ⇒ job bounced back to the orchestrator with the build log as continuation context for `project.ask()` — the machine, not the human, eats the conflict. Optionally still opens a *display* PR per landing (body format from `post_pipeline.py:584-663` kept) but PRs are informational; nothing merges via `gh pr merge`.
**Touches:** replaces `post_pipeline.py` branch/PR sections (`:413-741`); deletes `_maybe_merge_marathon_pr` (`review/review.py:266-330` — currently dead-on-arrival anyway per the `AttributeError` bug); deletes the prefetch/auto-commit workarounds.
**Why:** solves branch serialization, `[build:FAIL]`-on-main, cross-issue breakage, and "partial output limbo" (`COMPLETE_WITH_ERRORS` extractions now land or bounce, never rot uncommitted).

### C3. SQLite ledger + GitHub projection (M)
**What:** `marathon/ledger.py` + `marathon/orchestrator/gh_sync.py`. Migration command imports `state.json`, `config.toml` chapters, label states, and `wall-time.json`. Verdict writes go ledger-first, then async to GitHub (comment + label + tracker emoji via the existing `tracker.py`, made idempotent). Fixes the verdict/iteration timestamp race (#50) by making dispatch decisions transactional.
**Touches:** `review/state.py`, `review/config.py`, `review/tracker.py`, `formalization.py` (wall-time source); deletes `review/referee_queue.py` (dead code).
**Why:** every state-drift incident in the recon (#74-style sync PRs, stale locks, desynced queues) traces to split state.

### C4. `marathon deck` — the Code Tinder TUI (M)
**What:** new `marathon/deck/` (Textual). The deck shows only **ready** cards: declaration is at a green `next` SHA, eval gate passed, and all dependency-predecessor cards are verified or explicitly deferred. Card = the existing sub-issue body sections (signatures with pinned permalinks, informal statement, mechanical-accuracy checklist, verification questions) rendered locally — zero new content format. Keys: `v` verify, `r` reject (inline note editor; note goes **verbatim** to Aristotle, preserving the deliberate Claude-bypass at `refine.py:128-165`), `space` defer, `d` dependency view, `o` deep-dive (launches the existing coreviewer chat via `open_session.py` — per-declaration review, goal 3, survives intact), `g` promote `next`→`main`. A status pane streams orchestrator events: per-job Aristotle `percent_complete` and event types (PROVING/EDITING/ERROR) — the SDK data we currently never surface. While the human judges card k, the orchestrator is already running cards k+1…k+n's fixes.
**Touches:** new package; `review/open_session.py` reused; `aristotle_runtime.py` event watcher feeds the ledger `events` table.
**Why:** this *is* goal 5. Today's loop (type command → wait → poll → pull → merge) becomes read → keystroke → next card. With verify-on-first-pass taking ~minutes (Ch.14 era data), a 20-card chapter becomes a single sitting.

### C5. Eval gate — vendor autoform's matcher + axiom check + jury (M)
**What:** `marathon/evalgate.py` vendoring three autoform pieces: the statement→declaration **matcher**, `#print axioms` batching (we already have the `lake env lean` batching in `formalization.py --check-axioms`), and the thresholded **jury** (faithfulness ≥4, proof_integrity ≥3, code_quality ≥3) run as `claude -p` like the existing rater. Runs at landing time and at card-build time. **Jury-fail never reaches the human** — failure feedback becomes an auto-reject note and requeues the job. The advisory 7-axis rater stays (referee calibration depends on it) but stops pretending to be a gate.
**Touches:** new module; `post_pipeline.py` rater section; `review/chapter_sessions.py` bootstrap (sub-issue bodies seeded from targets + jury scorecard + axiom set).
**Why:** goal 2 — minimizes human-verified surface area. The human reads only statements that a thresholded machine jury already believes; PR #81's wasted full bot round-trip ("opened, rated junk, closed, redone by hand") becomes impossible.

### C6. Dependency engine (M)
**What:** `marathon/deps.py`: vendor autoform's Lean metaprogram for the declaration-level dependency graph (with deception tags: `vacuous_body`, `ignores_params`, …); map issues→decls via the matcher (replacing the regex in `fill.py:52-77`). Three consumers: (1) **deck ordering** — present #50 before its Ch.12 consumers; (2) **impact analysis** — when a landed fix changes a signature, auto-create follow-up jobs for every broken consumer decl (the #50→Ch.12 carryover becomes a same-night machine sweep, not a 4-day red main); (3) **referee input** — deception tags feed the machine tail so Hermes prompts target structural rot.
**Touches:** new module; `orchestrator` scheduler; `referee.py` context assembly; `verified_decls.py` (regex audit replaced by graph lookup, fixing the rename/comment false-match class).
**Why:** "exactly how inter-issue dependencies are computed": from the Lean elaborator's own import/usage facts, not heuristics. This is also what makes optimistic concurrency *safe* — conflicting jobs are detected by decl-set overlap before dispatch and serialized only when they actually collide.

### C7. Bundle caching + prompt hygiene (S)
**What:** use `Project.create(tar_file_path=…)` with a content-hashed cached tarball instead of re-archiving the repo per submission; move rater and hermes-steer prompts from argv to stdin (E2BIG parity with the reviewer); add backoff + failure cap to `_poll_task_loop` (`aristotle_runtime.py:228-233`).
**Touches:** `aristotle_runtime.py`, `post_pipeline.py:838-845`, `hermes_watcher.py:445-451`.

### C8. Non-destructive extraction (S)
**What:** scan the tar for the expected output path **before** `rmtree` (`skeleton.py:258-262`); extract to temp and atomically swap. Also fix the `review.py:279/326` `AttributeError`/`NameError` pair immediately (even though C2 retires the function) so verifies stop silently skipping tracker updates today.
**Why:** landmines under every iteration; an afternoon of work.

---

## 3. Migration path

Each phase ships independently; the existing CLI verbs work throughout (they become thin wrappers that enqueue jobs once the orchestrator exists).

1. **Week 0 — hygiene (C8, C7):** bug fixes, stdin, safe extraction, delete `referee_queue.py`. Pure wins, no behavior change. GeometricAnalysis keeps running as-is.
2. **Phase 1 — ledger (C3):** introduce SQLite alongside `state.json` (dual-write, ledger-read), batch GH sync. Existing daemon keeps running; `review list/next` get fast. Cut over, drop `state.json` from git tracking.
3. **Phase 2 — orchestrator + worktrees (C1):** orchestrator replaces the per-chapter daemon at concurrency=1 first (parity with today), then raise concurrency. Still per-issue branches at this stage — proven landing semantics before changing them.
4. **Phase 3 — landing queue (C2):** introduce `marathon/next`, route orchestrator jobs through it; per-issue branches retired. `verify` stops touching PRs entirely. First point where reviews can build on one another.
5. **Phase 4 — deck (C4):** TUI over the now-stable ledger/queue. `review open` deep-dive kept as the escape hatch.
6. **Phase 5 — eval gate + deps (C5, C6):** tighten what reaches the deck; turn on impact-analysis auto-jobs. Bootstrap Ch.12/15/16 (the 36-declaration backlog) through the new path as the acceptance test.

GeometricAnalysis is the live testbed at every phase; nothing requires re-bootstrapping verified chapters — verified SHAs import into the ledger as-is.

---

## 4. Explicitly NOT building

- **A web app or hosted service.** One operator, local repo, local API keys, hour-long jobs: a TUI over SQLite is strictly cheaper and the deck's data (the GH projection) is already shareable. Revisit only if multi-reviewer becomes real.
- **GitHub-Actions/CI-native orchestration.** Aristotle runs are 20 min–24 h with server-side session state and need `ask()` mid-flight; a local long-lived daemon is the right shape. CI gets one job: a build-status check on `next`→`main` promotion.
- **A rewrite or adopting autoform-bot wholesale.** Marathon's Aristotle runtime (reattach, continuation, steering, budget semantics) is the battle-tested asset; autoform's SLURM/ZMQ/worker-racing tier solves a scale (multi-node, racing 5 agents) we don't have and Harmonic's concurrency terms may not permit. We vendor autoform's three best ideas (matcher, axiom/jury gate, dep graph) as libraries, not its coordinator.
- **Stacked-diff tooling (Graphite-style) or per-issue PR stacks.** The integration branch + statement-keyed verification makes diff stacking unnecessary — humans never review diffs as the primary artifact.
- **Auto-cancel / kill-switch automation for Aristotle.** Hermes' never-cancel policy is empirically sound; add only a manual cancel key in the deck.
- **Auto-verify.** The jury gates what humans *see*, never substitutes for the verify keystroke. Misformalization risk is the whole reason humans are in this loop; the design minimizes their surface area, not their authority.

---

**Net effect:** today, one rejection per chapter at a time, hand-merged, with ~⅓ of issue comments being operational noise. After this: the human opens `marathon deck` over coffee, swipes through overnight machine output that is already built, axiom-checked, jury-passed, and dependency-ordered — and the only thing they ever type is mathematics.