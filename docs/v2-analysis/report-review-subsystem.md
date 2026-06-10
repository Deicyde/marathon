# Marathon human-review subsystem — architecture report

## 1. The /marathon:review lifecycle

**Bootstrap.** `marathon review bootstrap-chapter --chapter N` (`review/review.py:138`) writes a long briefing to `.marathon/review/sessions/c{N}-bootstrap-<ts>.md` and fires a `vscode://anthropic.claude-code/open?prompt=…` URI containing only a short pointer (`review/chapter_sessions.py:527-573`; the 5,000-char URI ceiling forces the briefing onto disk, `chapter_sessions.py:48`). The "chapter-bootstrap coreviewer" agent reads every `.lean` file, pairs declarations with a human informal-statements file (or LLM-renders statements flagged `⚠️ verification pending`), drafts `.marathon/review/drafts/Chapter{N}.md`, proposes the sub-issue list, and **stops** for human go-ahead (`chapter_sessions.py:278-369`). On approval it runs `marathon review subissues create drafts/ChapterN.md` (`review/subissues.py:66-111`): one `gh issue create` per draft section, labels `review` + `chapter-N`, then a REST POST to `/issues/<parent>/sub_issues` attaches each as a GitHub sub-issue of the parent tracker (`subissues.py:55-63`). The agent then hand-edits `.marathon/review/config.toml`'s `[[chapters]]` registry (`[issue_num, tracker_substring]` pairs in textbook order) and patches the parent issue's `### Chapter N:` section. `audit-chapter` is the maintenance twin: read-only drift/coverage/label-mismatch audit, propose, wait, apply (`chapter_sessions.py:372-481`).

**Review session.** `marathon review next --chapter N` walks the registry in textbook order, querying labels per issue to find the first unreviewed one (`review.py:66-74`). `marathon review open N` launches a "coreviewer" Claude Code chat via the same URI handler (`review/open_session.py:319-353`), prompt budgeted into role/workflow/rubric/output/queue sections (`open_session.py:143-311`). The coreviewer: reads the issue + comments, checks body drift against current code, walks open verification questions one per turn (editing the body after each resolution), recommends a verdict, and stops; applying happens only when the human says verify/reject (`open_session.py:176-213`). After a reject it polls `marathon review refine-status` and re-reviews — "iterate-until-verify, not one-shot" (`open_session.py:205-212`).

**Verify.** `cmd_verify` (`review.py:216-263`) posts a one-line comment, adds `review:verified` / removes `review:rejected`, clears the state.json queue entry via `record_verification` (`state.py:188-201`), attempts to merge the iteration's marathon PR (`_maybe_merge_marathon_pr`, `review.py:266-330` — see §6, broken), and by default keeps the issue **open** (statements accepted, sorrys remain; `--close` only when fully implemented). Tracker emoji 🟠→🟡 on the parent issue body (`review/tracker.py:21-78`).

**Reject.** `cmd_reject` (`review.py:336-370`) posts a comment, swaps labels, records `{status: rejected, verdict_ts, notes}` in `.marathon/review/state.json` (`state.py:171-185`), then auto-launches the per-chapter refine daemon unless `--no-refine` (`review.py:388-424`).

**Daemon.** `marathon.review.daemon` is a single-flight per-chapter loop (PID lockfile at `.marathon/review/runner-locks/refine-cN.lock`, `daemon.py:120-148`). Each tick it picks the **oldest** rejection still needing iteration (`pending_rejections_needing_iteration`, `state.py:256-275`) and dispatches exactly one `marathon refine --skeleton --max-iterations 1 --review-rejection <issue> --auto-build --auto-commit --auto-push --auto-rate --audit-verified --auto-pr` (`daemon.py:64-81, 158-198`). After the subprocess returns it marks the issue iterated regardless of success (`daemon.py:243-272`; the human re-rejects to re-queue). Queue drained → sleep 60s and poll (`daemon.py:54, 289-298`).

**The refine iteration itself.** For focused rejections, Claude-in-loop is **bypassed entirely**: the human's reject notes are formatted verbatim as the Aristotle prompt (`refine.py:949-965`, `_format_reject_as_aristotle_prompt` at `refine.py:128-165`), because Claude repeatedly replaced the human's ask with "while we're at it" refactors. Post-iteration, `post_pipeline.run_post_pipeline` runs lake build → formalization.yaml refresh → commit → push → verified-decls audit → Claude rater → force-push branch + open/update PR (`post_pipeline.py:918-1173`).

## 2. GitHub usage

- **Issue structure:** one parent tracker issue per project (`parent_issue` in config.toml) whose body holds per-chapter sections with one emoji-statused line per declaration (`tracker.py:1-11`). One GitHub sub-issue per *first-class* declaration (textbook-named results; scaffolding lemmas fold under umbrellas — rubric at `chapter_sessions.py:115-133`), attached via the sub_issues REST endpoint (`subissues.py:58-63`).
- **Labels:** `review`, `chapter-N` at creation; verdict labels `review:verified` / `review:rejected` / `review:in-flight-fix` (`config.py:50-54`), managed exclusively by the CLI (coreviewer briefings forbid direct label edits, `chapter_sessions.py:410-415`).
- **PR-per-issue, not per-iteration:** `--auto-pr` uses a *persistent per-issue branch* `marathon/refine-c<N>-i<issue>` that is hard-reset to `origin/main` and force-pushed each iteration, so the open PR always shows only the **latest** iteration's diff (`post_pipeline.py:413-429, 497-581, 690-741`). PR body embeds build status, rater scores, marathon.md design log, Aristotle project link (`post_pipeline.py:584-663`).
- **State tracking is split:** GitHub labels are the *display* state read back by `list`/`next` (`review.py:32-43`); `.marathon/review/state.json` is the *queue* state (per-issue `status`/`verdict_ts`/`notes`/`last_iteration_ts`, schema at `state.py:27-58`). They can drift; `open_session.py:100-117` renders both side-by-side precisely because of that. The chapter registry in config.toml is a third state surface mapping issue numbers ↔ tracker lines.

## 3. Branching model and serialization

Each rejection's fix lands on its own branch created by `git checkout -B marathon/refine-c<N>-i<issue> origin/<base>` **before** the iteration (`post_pipeline.py:574-579`, called from `refine.py:1216-1232`). Because every branch is reset to `origin/main`, *no queued review can build on another's unmerged work*: if issues #22 and #23 are both rejected, #23's iteration branch will not contain #22's pending PR content. The chapter files only advance when a human verifies and the PR merges to main.

Serialization happens at three exact points:

1. **One daemon per chapter** — the PID lock (`daemon.py:120-137`); `cmd_reject` only queues when a daemon is alive (`review.py:396-410`).
2. **One rejection per iteration** — the daemon pops the head of the queue and dispatches a single focused refine (`daemon.py:231-243`); Hermes/Aristotle sees exactly one rejection (`state.py:278-311` `focus_issue` filter).
3. **Human verify = merge gate** — branches only reach main via `_maybe_merge_marathon_pr` during `verify` (`review.py:253, 266-325`). Until then, every subsequent branch reset excludes that work, and a second PR touching the same files (or `formalization.yaml` / `.marathon/wall-time.json`, both committed each iteration) conflicts; conflicted merges are surfaced but not resolved (`review.py:318-325`).

A subtle consequence: the branch reset also reverts the *tracked* `state.json` to origin/main's version, which is why refine prefetches the rejection notes before switching (`refine.py:1195-1210`).

## 4. Where the human waits

- **Coreviewer chat** (`open N`): the agent walks verification questions one-per-turn, waiting on the human each time (`open_session.py:188-198`); verdicts apply only on explicit go-ahead (`open_session.py:200-204`).
- **Bootstrap/audit sessions:** propose-then-stop, apply-step-by-step on approval ("Stopping is the load-bearing step", `chapter_sessions.py:168-171, 209-211`).
- **After reject:** the daemon iteration is 10–30 min of Aristotle compute; the human (or coreviewer) polls `marathon review refine-status --chapter N`, which tails the log (`review.py:427-451`), then `git pull` and re-review (`open_session.py:205-212`).
- **Daemon poll latency:** up to 60 s before a new rejection is picked up when the queue was drained (`daemon.py:54, 291-298`).
- **`refine-stop`:** SIGTERM lets the current iteration finish — "may take several minutes" (`review.py:473-479`).
- **PR merge:** automatic on verify *in theory*; on conflict (or due to the §6 bug, always) the human merges manually.
- **Per-issue `gh` round-trips:** `cmd_list`/`cmd_next` issue one `gh issue view` per registry entry, serially (`review.py:49-74`) — a 20-entry chapter is ~20 sequential network calls while the human watches.
- **Aristotle backfill / referee passes:** `marathon referee` is a synchronous `claude -p` call over a whole-repo prompt (`referee.py:350-384`).

## 5. The referee subsystem

Two distinct artifacts share history here:

- **`referee.md`** (`.marathon/referee.md`) is now **purely user-managed**: the project's long-lived rubric layer (failure modes, calibration rules). It feeds the Hermes review prompt as "project-specific reviewer notes" (`claude_review.py:206-220`) and the auto-rater as scoring context (`post_pipeline.py:804-815`) — but is **suppressed entirely** during focused rejection iterations so it can't compete with the human's ask (`refine.py:930-939`).
- **`standing-items.md`** (`.marathon/standing-items.md`) is **purely machine-managed**, written by `marathon referee` (`referee.py:36, 387-523`): a one-shot `claude-opus-4-7` pass over the repo's Lean files, every refine workdir's `marathon.md`/ratings/refine-log, git log, and the existing tail, producing a fresh ≤100-line standing-items snapshot with soft bloat caps per section (`referee.py:67-146`). Optional auto-commit/push, or `--review` to write `.proposed`. The auto-refresh hook from daemon iterations was deliberately removed (`daemon.py:70-73`) so the machine tail can't dilute explicit human reject notes.
- **`referee_queue.py`** is the *legacy* reject queue (append `- **Review #N REJECTED**` bullets to referee.md's header). It is now dead code — no callers remain; `state.py:1-26` documents the failure modes (auto-nesting, no dedup, cross-issue contamination) that motivated the move to state.json.

Feedback loop: human rejects → notes → Aristotle prompt; rater notes + marathon.md → next `marathon referee` pass → standing-items; referee.md (human) → every non-focused Hermes prompt and every rater call.

## 6. Fragility / pain points

1. **`_maybe_merge_marathon_pr` never merges — and never updates the tracker.** `review.py:279` iterates `cfg.chapters` (a `dict[int, ChapterRegistry]`, `config.py:108`), yielding int keys, then calls `ch.chapter` → `AttributeError`, swallowed by `except Exception: return` at `review.py:284-285`. Verified by simulation. So every `verify` silently skips the PR merge **and** the `update_tracker_emoji` call at `review.py:326-330`, despite printing "tracker → 🟡". Should be `for ch, reg in cfg.chapters.items()` (or reuse `cfg.chapter_of_issue`).
2. **Latent NameError in the same function:** `review.py:326` references `num`, but the parameter is `issue_num` — would crash the moment bug #1 is fixed. The tracker update also appears misplaced inside the PR helper rather than `cmd_verify`.
3. **`state.json` is git-tracked and branch-sensitive.** `--auto-pr`'s `checkout -B … origin/main` reverts it mid-flight; the read side is patched by prefetching (`refine.py:1195-1210`), but the daemon's post-iteration `record_iteration` (`daemon.py:264`, `state.py:204-227`) writes against whatever version the marathon branch carries — if the rejection commit never reached origin/main, the entry is missing (warning, no-op) and queue accounting silently desyncs. `state.py:55-57` additionally concedes concurrent writes are unprotected.
4. **Label-state and state.json drift by design** (no reconciliation job; `open_session.py:100-117` just displays both; the audit briefing asks the human to fix mismatches manually, `chapter_sessions.py:441-446`).
5. **Tracker patching is substring+emoji string surgery** on the parent issue body (`tracker.py:55-70`): a reworded line, duplicate substring, or already-flipped emoji yields a soft `WARN` no-op; concurrent edits to the parent body can race the read-modify-write.
6. **N+1 `gh` calls** in `cmd_list`/`cmd_next`/`verified_declarations` (`review.py:49-74`, `verified_decls.py:156-179`) — slow and rate-limit-prone.
7. **Verified-decl audit is regex-grade** (`verified_decls.py:69-77, 182-224`): misses renamed decls, matches keywords in comments, and is soft-warning only — verified code can be silently clobbered until a human reads the JSONL.
8. **Failed iterations are marked "iterated"** (`daemon.py:263-272`): a crash-looping refine consumes the rejection; the human must notice and re-reject — no notification path exists.
9. **Force-push + hard-reset branch model discards prior iterations** (`post_pipeline.py:497-514, 700-707`): if a human pushed manual fixups to the marathon branch between iterations, the next iteration's `checkout -B origin/main` erases them (only `--force-with-lease` on push, but the reset happens locally first).
10. **`referee_queue.py` is dead code** still shipping with a docstring describing the *old* hash-trigger daemon contract (`referee_queue.py:1-14`), a trap for future maintainers.