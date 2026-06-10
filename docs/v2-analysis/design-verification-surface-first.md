# Marathon v2 — The Spec-Audit Layer ("Trust Kernel") Design

**Thesis.** The binding risk in this system is not unproved theorems — Aristotle is good and getting better — it is *proved theorems about the wrong statements*. Today, faithfulness assurance is the most expensive, least-instrumented part of marathon: a human reads review cards whose accuracy is enforced by regex (`review/verified_decls.py:69-77`), whose state lives redundantly in four places, and whose validity silently rots when upstream definitions change (the #50 `SmoothCovectorField` reshape broke Ch.12 for four days with no automatic downgrade of any 🟡). The design below makes "what must a human read, and is their past reading still valid?" a first-class, machine-computed object.

---

## 1. Target architecture

### Components

```
                ┌────────────────────────────────────────────┐
                │ refine loop (refine.py) — unchanged shape    │
                │ Claude drafts → Aristotle proves → extract   │
                └──────────────┬─────────────────────────────┘
                               ▼
   post_pipeline.py:  lake build → AUDIT ENGINE → TRUST LEDGER → gate → commit/PR
                               │            │
        ┌──────────────────────┘            └───────────────┐
        ▼                                                   ▼
  marathon/audit/ (NEW)                          .marathon/trust.json (NEW)
  • MarathonAudit Lean exe (vendored from         canonical per-decl state;
    autoform-bot's dep-graph metaprogram)         GitHub labels/tracker/issue
  • fingerprints, cones, axioms, sorries,         bodies become PROJECTIONS
    deception tags, probe results                 of it, never sources
        ▼
  spec-auditor (NEW Claude role, prompts/spec_audit.md)
  • renders spec cards, semantic deltas, kernel-shrinking proposals, probe drafts
        ▼
  review CLI / daemon (review/) — reads ledger; consumes human rejects
  AND machine-generated fix tasks through the same queue
```

Existing actors keep their roles: Aristotle proves (and alone sees the `.tex` — firewall preserved), refine-reviewer drafts prompts, rater/referee stay advisory, the daemon stays a single-flight dispatcher. One new Claude role (spec-auditor) joins the existing four.

### The audit engine

A `lake exe MarathonAudit` metaprogram (vendored and trimmed from `/Users/jack/Desktop/LEAN/autoform-bot/autoform/eval/`'s dependency-graph Lean program) emits, per target declaration:

- **Statement fingerprint**: hash of the *elaborated, pretty-printed type* (binders normalized, instance args canonicalized) — not source text, so comment/whitespace/proof edits don't invalidate, but any meaning-bearing change does.
- **Statement cone**: the constants appearing in the type, partitioned into Mathlib (trusted vocabulary) vs project-local (must be read), each with its own fingerprint.
- **Hard evidence**: `#print axioms` (whitelist `propext`/`Classical.choice`/`Quot.sound`), sorry status, autoform's deception tags (`vacuous_body`, `ignores_params`, `trivial_instance`, `proof_by_exfalso`).

This subsumes and replaces the batch `--check-axioms` path in `formalization.py` and the keyword-grep decl census.

### The trust ledger and graded trust

`.marathon/trust.json`, one entry per declaration:

```json
{"name": "...", "file": "...", "fingerprint": "...",
 "cone": [{"name": "...", "fingerprint": "..."}],
 "evidence": {"axioms": [...], "sorry": true, "probes": {...}, "jury": {...}},
 "human": {"tier": "T2", "sha": "...", "fingerprint_at_verdict": "...", "issue": 50}}
```

**Trust tiers** (mapped onto the existing emoji ladder, so the tracker semantics survive):

- **T0 — builds** (`lake build` green).
- **T1 — machine-audited** (🟠): axiom-clean, sorry-accounted, no deception tags, probes pass. *Nothing below T1 ever reaches a human.*
- **T2 — spec-audited** (🟡): a human has read the declaration's **trust kernel** and signed off.
- **T3 — line-by-line reviewed** (🔵 when also sorry-free): today's full `/marathon:review` card walk.

The load-bearing rule: **tier is computed, never stored as a bare label.** T2 holds iff a human verdict exists *and* the recorded fingerprint and every cone fingerprint still match the current repo. Upstream-definition invalidation is therefore exact and automatic — when a def's fingerprint changes, every downstream T2 whose cone contains it degrades to T1, the tracker emoji flips 🟡→🟠, and one comment lands on the issue. This deletes the regex audit and the verify/label/comment/state.json four-way redundancy described in the review-subsystem report (§2): labels and tracker emojis remain, but as projections written *from* the ledger by `review/tracker.py`.

### Trust kernel = the minimized human-read set

For target theorem `T`: **kernel(T) = normalized statement of T + every project-local definition in its transitive statement cone.** Mathlib constants are trusted vocabulary; proof bodies are never in the kernel. The per-chapter and per-project kernel size (count + LOC of local defs humans must read) becomes a tracked metric in `formalization.yaml`. The spec-auditor's standing job, *before* a spec card reaches a human, is kernel-shrinking: "this custom predicate is `ContMDiff … ⊤` in disguise — restate via Mathlib and the human reads zero new definitions." This mechanizes what `referee.md`'s header (hypothesis-bloat, parallel-namespace items) currently enforces by exhortation.

### Probes (machine evidence behind T1)

Generated by the spec-auditor into `MarathonAudit/Probes/` (built, never imported by the library):

1. **Unfolding tests**: `example : myDef x = expected := rfl` / `by simp [myDef]` — pins computational meaning.
2. **Sanity instances**: a nontrivial model satisfies each definition (ℝⁿ is a manifold, the dual bundle of a nontrivial bundle is nontrivial) — kills the `PUnit`-collapse trap, item #1 in GeometricAnalysis's `referee.md`.
3. **Vacuity probes**: ask Aristotle, cheaply, to prove `¬(hypotheses)` or `hypotheses → False` for each theorem statement. Success = broken spec, auto-filed as a rejection. This directly targets Aristotle's documented typo-exploit failure mode (Zulip, "Aristotle and axioms") and is a perfect use for the never-used `Project.create` prompt-only/tarball path — no full repo bundle needed.
4. **Round-trip informalization**: Claude (firewalled from the `.tex`) re-renders the Lean statement to LaTeX. Already in review cards as "⚠️ LLM-rendered"; the change is regenerating it *on every fingerprint change* so the human's comparison object is never stale.

### Spec cards: diff-of-meaning, not diff-of-code

The human-facing unit (replacing today's full review card for T2 work) shows: the statement, the kernel defs (only those), the fresh informal rendering, the probe/axiom evidence table — and on any change, a **semantic delta**: old vs new informal rendering plus a spec-auditor verdict in a closed vocabulary (`strengthened` / `weakened` / `equivalent-refactor` / `meaning-changed`), with the list of downstream T2s invalidated. The human reads mathematics, not git hunks. Verify/reject on a spec card is the "swipe" — this is the substrate goal #5's Code Tinder needs, regardless of eventual UI.

### Plugging into both modes

- **Review mode (goal 3)**: unchanged ceremony, better floor — `review next` serves only T1-passing items, sorted by (tracker order, kernel size); `verify` writes a fingerprint-pinned T2/T3 verdict to the ledger; rejects flow to the daemon exactly as today.
- **Hands-off mode (goal 2)**: adopt autoform's `targets.yaml` (run `/autoform:extract` once per book) as the target list keying the ledger; its matcher maps targets→decls; jury scores become optional T1 evidence. Probe failures, axiom violations, and deception tags are auto-filed as machine rejections into the *same* daemon queue (`review/state.py` pending-rejections shape), so the loop self-heals without a human. The human's entire job in hands-off mode is swiping spec cards — the minimal surface.

---

## 2. Highest-leverage changes

| # | Change | Touches | Why | Effort |
|---|--------|---------|-----|--------|
| 1 | **Fix the verify/merge bug** — `_maybe_merge_marathon_pr` iterates int keys (`review/review.py:279`, AttributeError swallowed at :284-285) and the latent `num` NameError at :326. Every verify silently skips PR merge and tracker update today. | `review/review.py` | A confirmed, simulated bug nullifying the verify→merge→tracker pipeline; everything else builds on verify working. | **S** |
| 2 | **Audit engine + fingerprints** — `marathon/audit/{engine.py, lean/MarathonAudit/}`, vendoring autoform's dep-graph metaprogram + axiom check; new `marathon audit` CLI verb; hook in `post_pipeline.run_post_pipeline` after `lake build`. | new `marathon/audit/`, `post_pipeline.py`, `formalization.py` (retire `--check-axioms` plumbing), `__main__.py` | Converts "build passed" into per-declaration ground truth; the precondition for every other change; replaces regex-grade tooling with elaborator-grade. | **L** |
| 3 | **Trust ledger + exact invalidation** — `.marathon/trust.json` as canonical state; tier computed from fingerprints; delete `verified_decls.py` regex matching; `review/state.py` queue and labels become projections; `referee_queue.py` deleted (dead code). | `review/{verified_decls.py, state.py, tracker.py, review.py}`, `post_pipeline.py:1176-1250` | Kills the four-way state redundancy, the state.json/branch-switch races (ledger keyed by decl+fingerprint is merge-friendly, like the wall-time v2 sidecar you just shipped in `formalization.py:326-382`), and makes "verified code silently clobbered" impossible. | **M** |
| 4 | **Spec cards + semantic-delta comments** — new `prompts/spec_audit.md`; restructure issue bodies in `review/subissues.py` and the coreviewer briefing in `review/open_session.py`; on fingerprint change, one auto-comment + emoji flip. | `review/{subissues.py, open_session.py, chapter_sessions.py}`, new prompt | This is the diff-of-meaning surface — the thing the human actually reads. Directly shrinks the 6-reject #48-style cycles by making each iteration's *semantic* effect legible. | **M** |
| 5 | **Hard gate before PR/merge** — `--auto-pr` refuses on build FAIL or T1 regression (no more `[build:FAIL]` merged-anyway PRs; 6 of the last 10 main commits!); PR body shows ledger delta (decls gained/lost tier); `verify`'s merge requires no outstanding invalidations. `COMPLETE_WITH_ERRORS` extractions commit to the branch (fixing partial-output limbo, `refine.py:783-787`) but can never pass the gate. | `post_pipeline.py:497-741`, `refine.py`, `review/review.py` | Replaces "human eyeballs a title string" with a real status check; ends red-main-by-default during refine bursts. | **S/M** |
| 6 | **Probe generation loop** — spec-auditor drafts probes; vacuity probes go out as cheap prompt-only Aristotle submissions (first use of `Project.create`); results land as T1 evidence; failures auto-queue rejections. | `marathon/audit/probes.py`, `aristotle_runtime.py` (one new submit path), daemon queue | The only component that *actively hunts misformalization* rather than waiting for a human to notice; mechanizes the referee header's top failure modes. | **M** |
| 7 | **Hands-off ingestion** — `targets.yaml` from `/autoform:extract` keys the ledger; vendor autoform's matcher; `review bootstrap-chapter` seeds sub-issues from ledger+targets instead of an LLM re-reading every file; machine failures auto-reject into the daemon. | `review/chapter_sessions.py`, `skeleton.py`/`order.py` (targets-aware), new `marathon/audit/match.py` | Turns "point marathon at a textbook" from chapter-granular hope into statement-granular accounting ("62 targets: 9 T2, 40 T1, 13 failing probes"), and clears the Ch.12/15/16 bootstrap debt mechanically. | **M/L** |
| 8 | **Batch GitHub reads** — one GraphQL query replaces the N+1 `gh issue view` loops. | `review/{review.py, github.py, verified_decls.py}` | Small, but it's the latency the human feels on every `list`/`next`. | **S** |

---

## 3. Migration path

Each step ships independently; the refine loop, daemon, and prompts keep working throughout.

1. **Week 0 — change #1 + #8.** Pure fixes; verify finally merges PRs and updates trackers.
2. **Shadow audit — change #2.** `marathon audit` runs read-only after each post-pipeline build, writing facts beside (not replacing) existing state. Backfill the ledger from `state.json` + labels: existing 🟡 decls (Ch.10/11/14's 29 verified) get T2 entries pinned to their verdict-time SHAs/fingerprints. Validate fingerprint stability across a few known-benign refactors before trusting it.
3. **Ledger canonical — change #3.** Flip `review list/next/verify/reject` to read tiers from the ledger; labels become write-only projections. Delete `referee_queue.py` and the regex audit. The daemon is untouched — it still consumes pending rejections.
4. **Gate + cards — changes #5, #4.** Gate starts warn-only for two weeks (PR body annotation), then enforcing. Spec cards roll out per-chapter starting with the un-bootstrapped Ch.12/15/16 — no rewriting of existing verified issues.
5. **Probes — change #6**, starting with unfolding tests and sanity instances (pure Lean, no Aristotle spend), vacuity probes after measuring cost.
6. **Hands-off — change #7**, validated on a fresh small text before touching GeometricAnalysis.

---

## 4. Explicitly NOT building

- **A rewrite onto autoform-bot's orchestrator** (ZMQ, SLURM, DAGRunner, merge queue). One operator, one repo, serialized Aristotle runs — the coordination problem doesn't exist at this scale. We vendor autoform's *artifacts and checkers* (metaprogram, matcher, targets.yaml, rubrics), not its runtime. If marathon later mounts as autoform's Aristotle worker tier, the ledger ports cleanly.
- **A Code Tinder GUI now.** The spec card is the swipe unit; until it exists, a UI would be a faster way to read stale regex-audited cards. `review next` + GitHub mobile is an adequate interim client for T2 triage. Build the UI only after cards + gate have soaked.
- **Formal spec-equivalence checking across refactors** (defeq/iso proofs that old and new statements "mean the same"). Undecidable in practice and falsely reassuring. Fingerprint-invalidate-and-reswipe is honest: meaning changed, a human re-reads one card.
- **Aristotle self-certification of faithfulness.** Aristotle sees the `.tex` and could "grade" its own statements, but it is the audited party; all T1 evidence must come from independent checkers (Lean elaborator, probes, firewalled Claude). The information firewall stays.
- **A database or web service.** The project-id-keyed wall-time sidecar pattern just proved that merge-friendly JSON keyed by stable IDs works; `trust.json` keyed by decl-name follows it. Add advisory file locking for the two known append races; nothing more.
- **Steering/cancel expansion, event TUIs, parallel multi-chapter refine.** Real gaps (per the SDK report), but they accelerate *production*, and production is not the bottleneck — assurance is. Sequence them after the trust layer, when faster output stops meaning faster accumulation of unaudited claims.