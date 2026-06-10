# Marathon in practice: the GeometricAnalysis / LeeSM project

## 1. Project shape

**Goal.** Formalize selected chapters of Lee's *Introduction to Smooth Manifolds* (2nd ed.) sufficient to state and eventually prove Stokes' theorem (`Thm_16_11` in `GeometricAnalysis/LeeSM/Chapter16/StokesTheorem.lean`). Active chapters: 1, 10, 11, 12, 14, 15, 16, with an explicit dependency graph in `README.md` (everything funnels into Ch16).

**Scale.** 26 `.lean` files under `GeometricAnalysis/LeeSM/` (46 in the repo counting `Mathlib_4_30/` vendor backports and legacy `VectorBundle/`). ~541 top-level declarations by keyword count; **78 first-class declarations** on the GitHub tracker (issue #1), per the docs/stokes_progress_report.tex distribution. Sorry counts: `formalization.yaml` records **386 sorries, 138 in definitions** (grep finds 388 occurrences). Per chapter: Ch1 1, Ch10 33, Ch11 23, Ch12 76, Ch14 135, Ch15 53, Ch16 63.

**Verification status.** Four-state ladder (tracker emojis): *autoformalized skeleton awaiting review* (orange, 46), *statement verified by human review* (yellow, 30), *verified + sorry-free* (blue, 2 — the Ch1 autoform-bot carve-out). Per `formalization.yaml`: **Ch14 23/23 verified, Ch10 5/5, Ch11 1/12 in review; Ch1/12/15/16 not yet bootstrapped** into the review queue (`entries = []` in `.marathon/review/config.toml`). Everything verified so far is *statement*-verified; proofs are still mostly `sorry`.

**Provenance.** `formalization.yaml` (mathlib-initiative schema v0.2) records two production paths: Marathon (Claude Opus 4.7 as the "Hermes" prompt-drafter + Aristotle/Harmonic as prover) for the bulk, and Meta's AutoformBot for the Chapter 1 carve-out. The source LaTeX is copyrighted and deliberately excluded from the repo.

## 2. Day-to-day workflow as evidenced by artifacts

Three phases, three actors (Aristotle worker, Claude reviewer, human referee):

1. **Skeleton** — `ARISTOTLE_SUMMARY.md` documents a typical run: Aristotle generated Chapter 11 as 5 files / 636 lines of compiling statements with all-`sorry` proofs, with design decisions (e.g. `IsManifold I ⊤ M` over deprecated names) written up per file.
2. **Refine** — `.marathon/PromptLog.md` is a bare ledger of ~240 timestamped Aristotle run UUIDs (Apr 25 → Jun 9), showing overnight bursts of serialized runs spaced 20–45 minutes apart (e.g. 17 runs on May 7 alone). `.marathon/wall-time.json` is a project-id-keyed sidecar summing **99 runs ≈ 66.7 hours of Aristotle wall time**. Each iteration lands as a PR titled `marathon: ChapterN iter for #X [build:OK|FAIL]`, whose body embeds the build verdict and a 7-dimension auto-rater scorecard (`q m g api con l4 struct`) plus prose rater notes (visible in commits `99ebd10`, `3fee15f`).
3. **Review** — the human-paced certification layer. Claude bootstraps a chapter via a "chapter-bootstrap reviewer" session (`.marathon/review/sessions/c10-bootstrap-*.md`): it reads every declaration, drafts `.marathon/review/drafts/ChapterN.md` (one section per first-class declaration), and **stops for human go-ahead** before creating GitHub sub-issues under tracker #1. The human then walks `marathon review list/next/show/verify/reject`. A *reject* appends a fix bullet to `referee.md`'s header and **auto-launches the refine daemon**, which dispatches one rejection per iteration (runner logs: `refine-c11-*.log` etc., looping "queue drained; sleeping 60s"). Verdicts mirror to `.marathon/review/state.json` and flip tracker emojis.

The commit log shows the texture: machine iterations (`marathon: Chapter11 iter for #55 [build:FAIL]`) interleaved with human hand-fix commits (`Ch.11 #54: restate Prop 11.20 (a)/(b)/(d)/(e) as global CovectorField equalities`, `Ch.11 pullback_id: drop unused [IsManifold IM] typeclass`) and bookkeeping (`review: record #48 verify in state.json`).

## 3. Referee notes and convention enforcement

`.marathon/referee.md` is the project's standing reviewer brief, layered on top of Marathon's generic rubric, with two zones:

- **Human-managed header**: a ranked 9-item failure-mode checklist — wrong-shaped placeholder types (the `PUnit`-collapse trap), prose-disclaimer "documented gaps" ("Prose disclaimers don't survive a Mathlib PR"), cross-chapter predicate duplication, hypothesis bloat that reinvents named predicates, dishonest compact-support/smoothness signatures, `SignType` over ℝ-valued signs, missing bridging/scaffolding lemmas, parallel-namespace bloat, and vendor-file rules including the **exact whitelist of valid `Mathlib_4_30` import paths** (hallucinated vendor imports are a known Aristotle failure mode). Plus iteration-by-iteration calibration: iter 1 must be structural, iter 2 future-proofing, iter 3 must close a previously flagged structural item.
- **Machine-managed tail** (`marathon referee` overwrites it): top-leverage open items per chapter, last-5 iteration closures, hard scoring rules wired to the auto-rater (`build:FAIL ⇒ struct=0`; new sorry-RHS ⇒ `struct=1` cap; statement weakening on an honest theorem ⇒ `struct≤1`; content-free build-OK refreshes cap at `struct≤1` with **escalation**), a taxonomy of the four `structural_focus=4` patterns, and a 10-step next-iter priority list.

Enforcement is thus *score-mediated*: conventions become rater caps and "hard rejection demand" triggers in the next Hermes prompt, with commit SHAs cited as precedent. `CRITIQUE.md` shows the Hermes-side output: line-precise demands ("`ofLinearMapSection` still reinvents `fiberMapOfSectionMap`… deletes ~25 lines") with replacement code.

## 4. Visible friction

- **`[build:FAIL]` merges.** Five of the last ten main-branch commits are build-failing iterations merged anyway (#80, #86, #87, #89, #90, #99). These are daemon iterations, one per rejected sub-issue, landed regardless of build state so the linear history, referee tail, and rater state advance; the rater rule (`build:FAIL ⇒ struct=0`; "next iter must diagnose and re-land green or revert") is the compensating control. PR #100's body shows main was *already* red before the PR (10 errors) — a red main is operationally tolerated during refine bursts, and human hand-fix commits routinely follow FAIL streaks.
- **Serialized, slow iterations.** One rejection per daemon iteration, 20–45 min per Aristotle run, ~66.7 h total prover wall time for a skeleton that is still 386 sorries deep.
- **The model ignoring the marquee ask.** The Ch.11 `coordinateCoframe` maximalAtlas lift was the standing top-priority item across **twelve consecutive iterations**; the escalation machinery (auto-`struct=0`, hard-rejection triggers) exists precisely because Aristotle kept doing polish instead. Commit `3fee15f` candidly notes the iteration "did **not** address the original reject ask… but accidentally fixed the #50 carryover."
- **Stale/manual state.** A whole PR just to record one verify in `state.json` (#74); a "land stranded daemon iteration" commit (#72); leftover `runner-locks/refine-c11.lock`/`refine-c12.lock` with no daemon running; `state.json` (21 entries) lagging the GitHub-label ground truth (29 verified). Most telling: `formalization.yaml` says `wall_time: 2h 36m` while the sidecar sums 66.7 h — exactly the wall-time merge race the marathon repo's current branch (`wall-time-project-keyed-sidecar`) is fixing.
- **Bootstrap debt.** Ch12/15/16 (36 of 78 declarations) have no review sub-issues yet; their referee items accumulate in the tail "if re-opened."

## 5. What "library-quality, human-verified" means here

Operationally it is a **two-phase, statement-first certification**:

1. **Statement verification** (the current frontier): a human compares each Lean signature against Lee's copyrighted text side-by-side. The sub-issue body provides pinned-SHA code permalinks, an informal rendering explicitly flagged `⚠️ LLM-rendered from common knowledge; verification pending`, a mechanical-accuracy checklist (including honest notes like "this hypothesis is strictly weaker, hence the theorem is stronger"), and targeted verification questions. `VERIFIED 🟡` means *faithful to Lee*, with sorries allowed; the issue stays open as a proof-tracking ticket.
2. **Implementation**: an issue closes only when sorry-free; only 2 of 78 declarations are there.

The quality bar beyond faithfulness is "Mathlib-PR-ready": honest signatures even for placeholders, no documented-gap docstrings, no parallel-namespace bloat, axiom accounting in `formalization.yaml` (`sorryAx` etc. predicted for `Thm_16_11`), Apache-2.0 with Aristotle credited in headers, and full automation provenance (models, framework, wall time, reviewers) in the schema-v0.2 metadata. Bodies of review sub-issues are kept as *current-state snapshots* with history in comment threads, and pinned SHAs make each verdict reproducible against the exact code reviewed.

Key files: `/Users/jack/Desktop/LEAN/GeometricAnalysis/.marathon/referee.md`, `.marathon/review/{README.md,config.toml,state.json,drafts/}`, `.marathon/{PromptLog.md,wall-time.json,CRITIQUE.md}`, `formalization.yaml`, `docs/stokes_progress_report.tex`.