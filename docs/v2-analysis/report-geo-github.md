# GeometricAnalysis (LeeSM → Stokes) — live workflow study
*Surveyed 2026-06-10 via `gh` (53 issues, 50 PRs, all by 2 humans; effectively one operator, `Deicyde`/Jack McCarthy, with `pitmonticone` contributing 3 docs/cleanup PRs).*

## 1. The data model as actually used

**Three issue tiers, one of them vestigial:**

- **#1 "LeeSM Tracker"** — the only true tracking issue (0 comments; all state lives in the body). It holds the Stokes-Theorem declaration roadmap: ~62 declarations across Chapters 1, 10, 11, 12, 14, 15, 16, each tagged with a status emoji ladder: 🔴 not implemented → 🟠 autoformalized skeleton awaiting review → 🟡 human-reviewed skeleton → 🔵 reviewed + sorry-free → 🟢 multi-human → 📘 in Mathlib. Currently: Ch.1 🔵, Ch.10/11/14 🟡, Ch.12 🟠 (under review now), Ch.15/16 🟠 (no sub-issues yet). #3 "Prompt Book" is auxiliary.
- **Per-declaration review sub-issues** (50 of them, labels `review` + `chapter-N` + optional `review:verified`/`review:rejected`). One issue per tracker line item (e.g., #50 "Ch11 def — Covector fields and the pairing ω(X) (Lee p. 276)"). The **body** is a structured review card, machine-written and continuously rewritten:
  - header: `**Parent**: #1 — **LeeSM ref**: Lee p. N — **Status**: 🟡`
  - `### Lean signatures` — verbatim code blocks, each followed by a permalink pinned to a commit SHA and line range
  - `### Informal Statement` — "*✅ Verified against Lee pp. X*" + a LaTeX rendering of the book statement
  - `### Mechanical accuracy` — ✅/⚠️ checklist comparing Lean to book (encoding choices, explicit-argument conventions, sorry status)
  - `### Verification questions` — open design questions for the human
  - `### Iteration log` — per-PR ledger ("PR #99 (build:FAIL) — delivers both items of the prior rejection…")
  - `**Verdict**:` — `✅ VERIFIED (PR #N — …)` or left as a placeholder.
  - The **comment thread** is the verdict log: terse imperative reject asks (`❌ Two-part: (1) delete ALL Contravariant*… (2) add 4 missing sorry-bodied bundle instances`), harness re-queues (`🔄 Re-queue: previous dispatch SIGTERMed mid-run`), debug pings (`🔍 Debug pass: capturing offending dirty-tree lines`), and verify records (`✅ VERIFIED — … Sub-issue stays open to track remaining sorry bodies; pass --close to verify if fully implemented`).
- Verification is recorded **redundantly in four places**: the body Verdict line, the label flip, a ✅ comment, and `.marathon/review/state.json` in-repo (sometimes synced by a dedicated PR, e.g. #74 "review: record #48 verify in state.json"). Sub-issues stay **open** after verification (only #50 is closed); "open" ≠ "unreviewed".

**PRs.** Two species:
- **Bot iteration PRs**: title `marathon: ChapterN iter for #X [build:OK|FAIL]`, branch `marathon/refine-cNN-iXX` (one branch per sub-issue, **force-pushed every iteration** so the diff is always latest-vs-main). Body: issue link, build verdict, a 7-axis rater line (`q=2 m=3 g=3 api=2 con=4 l4=3 struct=3`) with long rater prose, the full marathon design log, and an Aristotle dashboard link; footer "🤖 Opened automatically by `marathon refine --auto-pr`".
- **Human PRs**: descriptive titles (`Ch.11 #50: port SmoothCovectorField to Ch.14 Cₛ^↑k⟮…⟯ shape`), used for review landings, state syncs, docs, and rescues of stranded bot work (#72 "land stranded daemon iteration").

All merges are by Deicyde (self-merge, no reviewers assigned). 43/50 PRs merged; 7 closed unmerged (#10, #35, #59, #61, #76, #77, #81) — abandoned experiments or superseded bot iterations.

## 2. One declaration's full lifecycle: Exercise 11.10 (dual bundle), issue #48

1. Sub-issue #48 opened with the review-card body; first human review **rejects** (May 29 22:35): "`dualBundle_coordChange` is signature-dishonest (tautology…); proof uses forbidden `grind +suggestions`; hb₁/hb₂ vestigial."
2. **Five more reject cycles** follow (May 29 23:23, May 30 00:24/01:18/02:07, Jun 3 00:57), each a sharper structural ask: fill the `dualSmoothFunctor` data field, reroute through `VectorBundleCore`, bridge to a `ContMDiffVectorBundle` instance, restate `coordChange` so "(τ⁻¹)ᵀ is read directly off the statement", finally "IsContMDiff is already a class in Mathlib (Basic.lean:565)… collapse to `inferInstance`."
3. The fixing iteration got **stranded by a daemon failure**; the human manually landed it as PR #72 ("land stranded daemon iteration", Jun 3 19:21, merged 5 min later).
4. ✅ verify comment Jun 3 19:17; a *second* verify Jun 5 07:23 after re-running the review tool; the state.json record went in by hand via PR #74. Tracker line flips to 🟡. Issue remains open to track residual sorries.

Total: 6 rejects, ~5 days wall, 2 manual rescue PRs. Contrast #91 (Ch.12 tensor bundles, in flight now): reject Jun 8 11:23 → SIGTERM re-queue → config-bug re-queue ("Ch.12 entries were empty so prior reject runs fell through to Claude-in-loop and errored") → PR #99 `[build:FAIL]` merged anyway (deletion landed, but 10 call-site errors from the #50 signature change) → PR #100 "fix #50 carryover" (which by its own admission "did **not** address the original reject ask" but fixed the build) → four more ❌ REJECTED comments within hours (latest 02:31 today).

## 3. Quantitative feel

- **50 review sub-issues**: 42 `review:verified`, 1 `review:rejected` in active iteration (#91), 7 awaiting first review (Ch.12 #92–98, opened Jun 8 with empty threads). ~30 tracker items (Ch.15/16) have skeletons but no sub-issues yet.
- **Review cycles per declaration, by era**: Chapter 14 (May 15–27, `review.py` helper era) was cheap — most issues have 1–2 comments, i.e., verify-on-first-pass; PR #40's title says "21/23 verified". Chapter 11 (June, marathon-daemon era) is heavier: median ~2 comments but #48 = 9 comments/6 rejects, #52 = 8/4 rejects, #49 = 13 comments (1 substantive reject + **11 re-queue/debug comments** + 1 verify). Roughly: 0–1 rejects for definitions, 2–6 for anything touching bundle-instance plumbing.
- **PR cadence**: 50 PRs over ~6.5 weeks. Human-watched PRs merge in **seconds to minutes** (#88: 20s; #78: 78s; #84: 6min). Daemon PRs finishing overnight wait for the human: #87 13.0h, #90 12.4h, #99 5.4h.
- **Build health**: 6 merged PRs carry `[build:FAIL]` in the title. Main was red from Jun 6 (PR #79's `SmoothCovectorField` reshape) to Jun 10 (PR #100's 10-site sweep), with the failure repeatedly annotated as "#50 carryover, not a regression" in three separate PR bodies.

## 4. Friction inventory

**Humans doing machine work:**
- Manual state sync PRs: #74 (record a verify in state.json), #73 (hand-populate `formalization.yaml` wall_time), #82 (relocate PromptLog.md), #33 (track CRITIQUE.md).
- Manual rescue of bot output: #72 (stranded daemon iteration), #63/#68 (hand-importing autoform-bot output and wiring imports), #60/#40/#41 (batch "review cycle landings" commits).
- Manual re-queue comments as a substitute for retry logic: issue #49 alone has 11, documenting live-patching of the harness — a `NameError` on `chapter_label`, a dirty-tree block, a `strip()`-eats-leading-space bug, `uv tool` reinstall clobbering patches (fixed by switching to an editable install), and `--auto-pr`'s branch switch wiping `state.json` before notes were read.
- Manual debug polling: "🔍 Debug pass: capturing offending dirty-tree lines."
- Manual merges of every PR, including the bot's, with no CI gate — `[build:FAIL]` is a title string the human eyeballs, not a status check.

**Races and dropped dispatches:**
- #50: "previous reject at 16:23:19 collided with iteration finish at 16:23:25 — `last_iter > verdict_ts` blocked dispatch" — a verdict/iteration timestamp race requiring a human re-queue.
- #91: dispatch SIGTERMed mid-run; separately, empty Ch.12 config made reject runs "fall through to Claude-in-loop and error."
- Duplicate verdict comments (#50 has the same reject posted twice; #91 has 4 near-identical REJECTED comments in 6 hours) — the bot re-posts on every state pass.

**Cross-issue coupling:** the per-issue force-pushed branch model has no notion of cross-chapter consumers, so #50's signature reshape broke Ch.12 for four days, polluting the rater scores of every intervening iteration ("caps quality and math_correctness at 2… automatic struct=0 rule"), and the fix arrived as a side effect of an unrelated #91 iteration. The rater also notes a referee item "twelve-plus iters overdue" (Ch.11 `coordinateCoframe` maximalAtlas lift) with a "standing escalation rule" that fires but changes nothing.

**Waits:** every overnight daemon completion sits 5–13h for the sole human's morning; the 7 Ch.12 sub-issues have waited ~2 days for first review; PR #81 was opened, judged junk by the rater, closed unmerged, and redone by hand as #83 within the hour — a full bot round-trip that a pre-PR quality gate would have skipped.

**Net read:** the review-card format and verdict-queue loop are genuinely systematic, but the system's reliability layer is currently the human: dispatch retries, state persistence, build gating, and cross-file refactor propagation are all done by hand, and roughly a third of all issue comments are operational noise rather than mathematical review.