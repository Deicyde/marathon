# Feasibility critique — Marathon v2 "Trust Kernel"

The thesis is right and the ledger direction is sound. But three of the eight changes are sized a tier too small, two rest on capabilities that don't exist, and the migration order would break the one production user.

## 1. The audit engine is not a vendoring job — the load-bearing piece doesn't exist

Change #2 says "vendored and trimmed from autoform's dep-graph metaprogram." I checked: autoform's "metaprogram" is a 232-line Lean script template embedded in Python (`autoform/eval/dependency_graph/lean_script.py`), emitting pipe-delimited constants and deception tags via `lake env lean`. **It computes no fingerprints.** "Hash of the elaborated type with binders normalized and instance args canonicalized" is new, genuinely hard Lean metaprogramming: pretty-printer output is options- and version-sensitive, instance terms embed in types, universe/auto-bound-implicit naming is unstable. Worse, toolchains already diverge (autoform pins lean4 v4.29.0; GeometricAnalysis pins v4.28.0), so the audit code must build *inside each target repo's workspace* — a lakefile modification of the user's repo the design never mentions. And if cone entries fingerprint Mathlib constants, **every Mathlib bump degrades all 29 verified decls to T1 and posts 29 issue comments**. Fix: fingerprint project-local constants only; record the toolchain/Mathlib rev in the ledger and treat bumps as a one-shot "re-pin amnesty" command; budget #2 as XL with a fingerprint-stability test suite (the migration plan's "validate across a few benign refactors" is the whole ballgame, not a checkbox).

## 2. The audit needs a green build; main is red exactly when you need it

`lake exe MarathonAudit` requires elaboration. Per geo-github, 6 recent merges carry `[build:FAIL]` and main was red Jun 6–10 — precisely the #50-reshape window the design uses as its motivating example. During such windows fingerprints are uncomputable and the ledger goes stale or lies. The migration compounds this: ledger becomes canonical at step 3, the build gate only at step 4. Reorder — gate (warn-then-enforce) **before** ledger-canonical — and define an explicit `tier: unknown` for decls that fail to elaborate, surfaced in `review list`.

## 3. `trust.json` is not like the wall-time sidecar

The wall-time fix works because entries are write-once, keyed by immutable project UUID. `trust.json` entries are *mutable per decl* — machine evidence changes every iteration, human verdicts land on main, and `--auto-pr` hard-resets branches to `origin/main` (`post_pipeline.py:497-514`). That recreates the state.json branch race (review-subsystem §6.3) on a bigger surface: an iteration branch's ledger commit can clobber a verify written to main meanwhile. Fix: split it. Machine evidence is *derived* — recompute post-merge on main (or keep it gitignored cache), never merge it. Human verdicts go in an append-only event log keyed by `(decl, fingerprint, ts)`; tier is computed on read. Then the merge story actually matches the sidecar precedent.

## 4. Vacuity probes can't be prompt-only, and machine rejections need a circuit breaker

A `¬hypotheses` goal mentioning `SmoothCovectorField` doesn't elaborate without the project — prompt-only `Project.create` only works for Mathlib-pure statements. You'd ship a tarball anyway (fine — use `tar_file_path` with a cached bundle), and per aristotle-web, pricing/rate limits are undocumented and the ToS caps concurrent sessions: per-chapter probe fan-out has no known budget. Also: failure to prove `¬H` is weak evidence (Aristotle gives up opaquely), so absence of a vacuity finding must not raise tier. Most dangerous: auto-filing probe failures into the daemon queue, whose accounting already silently desyncs and marks failed iterations "iterated" (`daemon.py:263-272`), with no notification path. Unattended machine rejections + that daemon = crash-looping Aristotle spend nobody sees. Fix: probes start as *evidence only* (changes #6's pure-Lean unfolding/sanity probes are the real 80% — ship those); auto-rejection requires a daily cap, a dedup key, and a notification hook first. Probe files must also be excluded from `create_from_directory` bundles or Aristotle will edit them.

## 5. GitHub projections: the write side of the N+1 problem

Change #8 batches reads; invalidation creates a write storm — emoji flips via substring surgery on the parent body (`tracker.py:55-70`, already race-prone) plus one comment per downstream T2, from a bot that already posts duplicate verdicts (#91: four near-identical rejects in 6h). Fix: idempotent marker-comments (edit-in-place), a single batched tracker-body rewrite per audit run, and rate-limit awareness.

## 6. Migration breaks GeometricAnalysis as written

Backfilling T2 "pinned to verdict-time SHAs/fingerprints" requires building 29 historical SHAs, several of which are `[build:FAIL]` — infeasible. Pin to current-main fingerprints with one human attestation pass instead. "`review next` serves only T1-passing items" would stall Ch.12's seven awaiting-first-review issues behind tooling that doesn't exist yet — make T1-filtering opt-in per chapter. And the hard gate would have blocked PR #99, which (red) delivered exactly the deletion the human demanded; cross-chapter refactors necessarily transit red. Provide `--allow-red` with ledger annotation, or the daemon dead-ends mid-refactor.

## Keep

Changes #1, #8, #5-as-warn-only, and pure-Lean probes are correctly sized and should ship in week 0–2. The spec-auditor's `weakened/strengthened` verdicts: advisory only — the recon shows Claude ignoring the marquee ask twelve iterations running; don't let role #5 gate anything.