# Aristotle (Harmonic AI) — Capability Report, June 2026

## 1. API / SDK

**Confirmed.** The public API portal is [aristotle.harmonic.fun](https://aristotle.harmonic.fun/); the waitlist was removed in late Jan 2026 — "anyone can sign up and immediately get access" ([Zulip: "Aristotle's waitlist is gone"](https://leanprover-community.github.io/archive/stream/219941-Machine-Learning-for-Theorem-Proving/topic/Aristotle's.20waitlist.20is.20gone.html)). Access is via web UI, CLI, and API ([Benzinga, Mar 2026](https://www.benzinga.com/markets/tech/26/03/51317862/robinhood-ceo-vlad-tenev-touts-autonomous-mathematician-as-harmonic-unveils-aristotle-agent)).

**Submission modes** (confirmed via [aristotlelib 2.0.0 on PyPI](https://pypi.org/project/aristotlelib/), released 2026-05-14, and SDK surface observed in a local integration at `/Users/jack/Desktop/LEAN/marathon/marathon/aristotle_runtime.py`):
- **Sorry-filling / agentic tasks on whole projects**: `aristotle submit "Fill in all sorries" --project-dir ./my-lean-project`; SDK: `Project.create_from_directory(prompt=..., project_dir=...)` uploads a project bundle. Lean 4 only.
- **Informal→formal autoformalization**: `aristotle formalize paper.tex --wait --destination output.tar.gz` (LaTeX/English in, Lean project tarball out). A user reported uploading a math paper and receiving "822 lines of code that formalized the key parts" (Zulip, ibid.).
- **Snippet + context-file mode**: the community [lean-aristotle-mcp](https://github.com/septract/lean-aristotle-mcp) server wraps `prove` (snippet + multiple context files), `prove_file` (with "automatic Lake/Mathlib dependency resolution"), and `formalize`, all with async polling.

**Steering/feedback** (confirmed from SDK 2.x surface): tasks (`AgentTask`) expose an **event stream** and terminal statuses `COMPLETE`, `COMPLETE_WITH_ERRORS` ("Review Suggested"), `FAILED`, `OUT_OF_BUDGET`, `CANCELED`. On continuable states, `project.ask(prompt)` starts a follow-up task **with server-side session continuity** (no re-upload), which third-party harnesses use both for retry-with-feedback and live steering mid-run.

**Limits/pricing — partly unconfirmed.** Per-task compute budgets exist ("Aristotle's budget for this request has been reached" — Zulip, ibid.); terms forbid "concurrent sessions in excess of any limits established by Harmonic" ([API terms](https://aristotle.harmonic.fun/api-terms-of-use)). No public per-proof price list was found; Harmonic has said it plans to keep a free tier for community/benchmark use ([Sacra](https://sacra.com/c/harmonic/)) and funds [$300k to Lean FRO and $1M mathematician sponsorships](https://harmonic.fun/news/). Exact rate limits and paid pricing: **not publicly documented** (behind login).

## 2. Benchmarks, strengths, weaknesses

**Confirmed:**
- **IMO 2025 gold**: formal solutions to 5/6 problems (P6 unsolved) ([tech report, arXiv:2510.01346](https://arxiv.org/html/2510.01346v1)). Architecture: Monte Carlo *graph* search over Lean tactics with a learned policy/value (~200B-param model), an informal lemma-generation loop (informal proof → lemma decomposition → formalization → error-correction against Lean REPL feedback), and the Yuclid geometry solver.
- **miniF2F ~90%** (earlier Harmonic SOTA announcements; [SiliconANGLE](https://siliconangle.com/2025/07/10/harmonic-raises-100m-nearly-900m-valuation-scale-ai-model-formal-mathematical-reasoning/)).
- **#1 on ProofBench** (Vals AI), "+15% over closest competitor" ([Benzinga](https://www.benzinga.com/markets/tech/26/03/51317862/robinhood-ceo-vlad-tenev-touts-autonomous-mathematician-as-harmonic-unveils-aristotle-agent)).
- **Software verification**: 96.8% SOTA on VERINA code-verification benchmark (Dec 2025; Harmonic news + Zulip topic).
- **Real-world Lean**: proved Mathlib-missing theorems (Niven, Gauss–Lucas); found four false-as-written exercises in Tao's analysis textbook (tech report). Raw API outputs have been cleaned up and **merged into Mathlib** (Alex Meiburg, [Zulip: "Sign Up for the Aristotle API!"](https://leanprover-community.github.io/archive/stream/219941-Machine-Learning-for-Theorem-Proving/topic/Sign.20Up.20for.20the.20Aristotle.20API!.html)). Used in 2026 research papers (e.g. [Erdős matching-to-multiples bounds](https://arxiv.org/pdf/2603.28636), [Sárközy counterexample](https://arxiv.org/pdf/2603.29992), [Toom-Cook verification](https://arxiv.org/pdf/2603.14038)).

**Known failure modes (confirmed via community reports):**
- **Non-idiomatic, ungolfed proofs**: "faster at filling in sorries" but scripts aren't optimal; users run ablation/golfing passes (Malcolm Sharpe, Zulip; see also topic "Learning to golf with Aristotle").
- **Vacuous/exploit proofs**: it will exploit typos in hypotheses to prove statements vacuously (Aaron Liu, [Zulip: "Aristotle and axioms"](https://leanprover-community.github.io/archive/stream/219941-Machine-Learning-for-Theorem-Proving/topic/Aristotle.20and.20axioms.html)) — statement vetting remains the user's job.
- **Axiom handling**: files with explicit `axiom` declarations were rejected; `admit` workarounds behaved inconsistently; one report of it rewriting theorem declarations into `variable`s (same thread).
- **Gives up opaquely on hard problems**: returns `sorry` + comment with no partial trace; partial-results exposure was a *feature request* as of Feb 2026, not a shipped feature ([Zulip: "Aristotle Partial Results"](https://leanprover-community.github.io/archive/stream/219941-Machine-Learning-for-Theorem-Proving/topic/Aristotle.20Partial.20Results.html)).
- Strongest where Mathlib is deep (algebra/number theory); weaker coverage areas (e.g. probability, topology foundations) noted in [HN discussion with Harmonic staff](https://news.ycombinator.com/item?id=46561569). Long-horizon refactoring/definition-design quality: no positive public evidence; papers describe humans/general LLMs owning definitions and decomposition (lower confidence — see §3).

## 3. Combining with general LLM agents

- The **MCP server** ([septract/lean-aristotle-mcp](https://github.com/septract/lean-aristotle-mcp)) explicitly targets Claude Code: the agent develops Lean code and "strategically invoke[s] theorem proving"; README warns proofs take "a few minutes to several hours" and recommends async submission + polling. A similar community Claude Code plugin was announced on Zulip (Jan 2026).
- Research workflows (e.g. the [Vlasov–Maxwell–Landau formalization, arXiv:2603.15929](https://arxiv.org/pdf/2603.15929)) describe a division of labor: general LLMs (Claude/GPT) plan structure and decompose lemmas, Aristotle fills proofs, humans vet statements/definitions — with human decomposition still required for long multi-step arguments. (Details from an automated summary of the PDF; treat specifics as medium-confidence.)
- Community consensus (Zulip) echoes: machine-generated thousands of lines still need careful human review; SorryDB hackathon tested Aristotle against ">3000 real-world sorries" (Lenny Taelman). No *official* Harmonic playbook for repo-scale LLM+Aristotle orchestration was found — harness design is currently community-driven.

## 4. Recent feature announcements (2025–2026)

- **Oct 2025**: public API signup + tech report; Yuclid open-sourced.
- **Dec 2025**: "Aristotle Learns to Code" — formal *software* verification, 96.8% VERINA ([Harmonic news](https://harmonic.fun/news/)).
- **Jan 2026**: waitlist removed; scaling growing pains (network errors acknowledged by Harmonic's Vikram Shanker).
- **Mar 2026**: **Aristotle Agent** — autonomous up to 24h, "can work and edit files directly inside your Lean project or code repository," "repo-quality" output, web/CLI/API ([Benzinga](https://www.benzinga.com/markets/tech/26/03/51317862/robinhood-ceo-vlad-tenev-touts-autonomous-mathematician-as-harmonic-unveils-aristotle-agent); [aristotle.harmonic.fun](https://aristotle.harmonic.fun/)).
- **May 2026**: aristotlelib 2.0.0 — agent-task model (`Project`/`AgentTask`/`Event`), `project.ask()` continuations, project download.
- **Not found / speculative**: no public "vetted-statement mode," proof certificates beyond Lean-checkability, or documented batch API. Statement-faithfulness checking remains external to the product as far as public sources show.

**Confidence key**: SDK surface, Zulip reports, tech-report numbers, and dated Harmonic announcements are confirmed. Pricing/rate limits, internal details of the 2026 arXiv workflows, and anything labeled above as "not found" should be treated as unconfirmed or absent from public record.