# Marathon: Aristotle SDK + Claude-plugin surface map

## 1. Aristotle SDK capability inventory

**Caveat first:** `SDK-reference.md` (captured 2026-05-02) documents the **1.x** surface, but the installed `aristotlelib` is **2.0.0**, which split `Project` into `Project` + `AgentTask` + `Event`. `marathon/aristotle_runtime.py` is the adapter. Inventory below covers both, with usage flags.

### Documented in SDK-reference.md (1.x)
| Capability | What it does | Marathon usage |
|---|---|---|
| Auth: `ARISTOTLE_API_KEY` env / `set_api_key()` (`arstl_` prefix) | API auth | **Used** — `skeleton.py:_ensure_api_key()` reads env, calls `set_api_key` |
| `Project.create(prompt, tar_file_path=None, public_file_path=None)` | Prompt-only (informal, e.g. "Prove 1+1=2") or prompt+tarball submission; `public_file_path` names the input for the record | **Ignored** — marathon never submits prompt-only or pre-built tarballs |
| `Project.create_from_directory(prompt, project_dir)` | Auto-archives a dir (skips build artifacts/stdlib) and submits | **Used** — sole submission path (`aristotle_runtime.submit_from_directory`) |
| `Project.from_id(id)` | Reattach | **Used** — reattach on resume; wall-time backfill in `formalization.py` |
| `Project.list_projects(pagination_key, limit 1–100, status filter)` | Fleet listing, newest-first, filterable by status (implies many concurrent projects are allowed) | **Ignored** |
| `wait_for_completion(destination, polling_interval=30)` | Built-in poll+download | **Ignored** — marathon rolls its own `_poll_task_loop` so a watcher can run concurrently |
| `get_solution` / `get_input` | Download result / original input | get_solution superseded by 2.x `project.get_files` (**used**, `download_result`); `get_input` **ignored** |
| `refresh()` / `cancel()` | Status refresh; cancel queued/in-progress work | `refresh` **used** everywhere; `cancel` **ignored** (Hermes deliberately never cancels) |
| Properties: `project_id`, `status`, `created_at`, `last_updated_at`, `percent_complete`, `input_prompt`, `file_name`, `description` | Metadata | `project_id`, `status`, `description` **used**; `percent_complete` **never surfaced** to the user; `input_prompt`/`file_name` ignored |
| `ProjectStatus` enum: QUEUED, IN_PROGRESS, COMPLETE, COMPLETE_WITH_ERRORS, OUT_OF_BUDGET, FAILED, CANCELED (+UNKNOWN, deprecated NOT_STARTED) | Lifecycle vocabulary | **Used** (as `TaskStatus.value` strings in state files). Marathon partitions it: RETRYABLE={CWE, FAILED}, CONTINUABLE={CWE, OUT_OF_BUDGET} (session-preserving), IN_FLIGHT={QUEUED, IN_PROGRESS} |
| `AristotleAPIError` (+`status_code`) | Error handling | **Used** throughout |
| Pricing/quota | **Not captured** — only `OUT_OF_BUDGET` ("ran out of allocated compute budget; partial results may be available") implies per-project budget mechanics. The Pricing/Tips/Toolchain dashboard sections were never pasted in |
| CLI | `aristotle prove-from-file` only; doc says prefer the SDK | Followed |

### 2.x surface actually installed (beyond the captured doc)
- `project.get_tasks(pagination_key, limit, newest_first)` — **used** (`limit=1` to grab the latest `AgentTask`).
- `project.ask(prompt) -> AgentTask` — **used twice**: (a) session continuation after CONTINUABLE statuses (warm sandbox, no re-upload), (b) Hermes live-steering nudges. This *is* the steering API.
- `AgentTask`: `from_id`, `refresh`, `cancel` (**ignored**), `get_events(pagination_key, limit, newest_first)` (**used**), `wait_for_completion(num_events, poll_interval)` and `show()` (**ignored** — custom loop instead), fields `agent_task_id`, `status`, `description` (**used** as Hermes' "task goal"), `output_summary` (**used** in continuation prompts).
- `Event` / `EventType` — 15 event types: MESSAGE, BUILDING, THINKING, **EDITING_FILE**, SEARCHING_LOCAL, RUNNING_COMMAND, PROVING, READING_FILES, REVIEWING, FINISHED, ERROR, READING_LEAN, SEARCHING_EXTERNAL, RUNNING_LEAN. Marathon **acts only on EDITING_FILE** (one Hermes call per edit); the other types appear only as context bullets. Event pagination is never walked — `fetch_new_events_since` grabs the newest 50 per poll.
- `EventStatus` (COMPLETE/SENT) — ignored.

**Project/repo context support:** the bundle itself is the context mechanism — `create_from_directory` ships the whole Lean repo plus `macros.sty`, `marathon.md`, and (skeleton only) the chapter `.tex`. There is no incremental/persistent project-context upload in the SDK; marathon re-archives the repo every fresh submission and avoids that cost only via `ask()` continuations.

## 2. Plugin commands (`plugins/marathon/commands/`)

All commands wrap the `marathon` Python CLI; preflights consistently echo resolved inputs, check `ARISTOTLE_API_KEY` and `gh auth status`, and print the exact command before running.

- **`/marathon:skeleton`** — per line of `order.txt`, runs `marathon skeleton <input> --repo-dir --output-base`: bundles repo+tex, submits via `create_from_directory` with `prompts/skeleton.md` (+per-chapter targets block), polls, extracts the tarball into `<repo>/<output-base>/<Chapter>/`, checkpoints in `marathon-state.json` (resumable). No agents spawned.
- **`/marathon:refine`** — `marathon refine`: up to `--max-iterations` rounds of *refine-reviewer drafts → Aristotle executes → extract → post-pipeline (lake build, commit, push, rate, PR, verified-decl audit)*. Flags: `--skeleton` (scaffold mode, bodies stay `sorry`), `--live-steering` (spawns **hermes-steer** per EDITING_FILE), `--auto-referee-every N` (spawns **referee**), `--review-rejection N`, `--no-continue-on-review`, `--max-prompt-words`, `--dry-run`. State in `marathon-refine-state.json` / `marathon-refine-log.md`.
- **`/marathon:fill`** — `marathon fill --decl NAME | --issue N`: one refine iteration with a load-bearing `--focus-directive` scoping to a single decl (issue mode greps decl names from the issue body's ```lean blocks; lands on branch `marathon/refine-c<N>-i<issue>`). Draft step is **single-decl-filler**. Auto-build/commit/push/rate/pr/audit all default **on**.
- **`/marathon:fill-file`** — `marathon fill-file --file PATH`: same engine, directive enumerates *every* sorry-bodied decl in one file (`_find_sorries_in_file`); fast-fails if none.
- **`/marathon:referee`** — `marathon referee --repo-dir`: one-shot **referee** agent pass rewriting the machine tail of `.marathon/referee.md` (user header preserved verbatim; `--review` writes `.proposed`). Claude-only — no Aristotle key needed.
- **`/marathon:rate`** — no CLI submit; dispatches the **rater** subagent on the folder's `.lean` files, prints its one-line JSON + a human summary. Diagnosis, not a gate (gate = autoform eval-rubrics jury).
- **`/marathon:steer`** — no standalone CLI verb. `--enable` ⇒ run refine with `--live-steering`; `--test FILE` ⇒ dry-run one **hermes-steer** judgement and print its JSON.
- **`/marathon:review`** — passthrough to `marathon review list|next|show|open|verify|reject|bootstrap-chapter|audit-chapter|daemon` (GitHub-issue-backed, one sub-issue per declaration; `gh` + `.marathon/review/config.toml`). `reject --notes` queues the single-flight daemon, which dispatches `marathon refine --skeleton --max-iterations 1 --review-rejection N` per oldest pending rejection under a per-chapter PID lock.
- **`/marathon:review:bootstrap-chapter`** — first-time: writes a briefing file to `.marathon/review/sessions/`, opens an interactive coreviewer chat in VS Code (URI handler); coreviewer files one sub-issue per named result, issue numbers recorded into `config.toml` `entries`. Optional `--informal-statements`; `--dry-run` prints the URI.
- **`/marathon:review:audit-chapter`** — same shape, for already-bootstrapped chapters: re-quote stale bodies, file issues for new results, flag stale ones.
- **`/marathon:review:verify`** — `marathon review verify N`: label `review:verified` + close issue, update `state.json`, `gh pr merge --merge --delete-branch` on the per-issue branch (missing/merged PR = no-op).

The **daemon** stays a CLI process by design (slash commands don't survive sessions).

## 3. Agent definitions (`plugins/marathon/agents/`, all Opus, tools Read/Bash/Grep/Glob)

Each "re-homes" a prompt that the Python side independently runs via `claude -p` subprocess (`marathon/prompts/*.md`), Max-OAuth-billed (`ANTHROPIC_API_KEY` scrubbed; model `claude-opus-4-7`, overridable via `MARATHON_CLAUDE_MODEL`):

- **refine-reviewer** ← `claude_review.review_and_draft_prompt` + `prompts/review{,_skeleton}.md`. Inputs (assembled by Python, in priority order): rubric, pending-rejections queue ("EXCLUSIVE marquee move" contract), referee.md, repo Lean context (gitignore-filtered, `.tex` excluded, trimmable via `MARATHON_REVIEW_CONTEXT_PATHS`), marathon.md, target folder, refine log, cross-chapter context, previous rater note, continuation/retry context, focus directive (highest salience, last). **Never sees the `.tex` source.** Output: prompt text only → sent **verbatim** to Aristotle (`create_from_directory` or `ask`). Prompt passed via stdin (argv E2BIG).
- **single-decl-filler** — refine-reviewer variant for fill/fill-file; must lead with the focus directive verbatim, refuse-and-surface if the fill needs a signature change; output likewise verbatim to Aristotle.
- **hermes-steer** ← `hermes_watcher.py` + `prompts/hermes_steer.md`. Inputs: rubric (with `{target_folder}`/`{skeleton_mode}` substituted), task `description`, referee tail (8k), `hermes-memory.md` tail (4k), last 20 events, last 30 decisions, the focal edit. Output: strict one-line JSON `{steer, reason, prompt, memory_note}`; parsed leniently (`_parse_decision` strips fences, `raw_decode`); on `steer=true` Python re-checks the task is in-flight then `project.ask(prompt)`; every decision → `marathon-steering-log.jsonl`, notes → `hermes-memory.md` (400-char cap). Never asks on the post-terminal drain.
- **rater** ← `post_pipeline.call_claude_rater` + `prompts/rate.md`. Output: one-line JSON, seven 1–5 integers (quality, math_correctness, generality, api_coverage, concision, modern_lean4, structural_focus) + `note`; leniently extracted, appended to `marathon-ratings.jsonl`, and fed into the **next** iteration's reviewer context.
- **referee** ← `referee.update_referee` + `prompts/referee_agent.md`. Inputs: current referee.md, generic rubric (dedupe target), repo Lean (400k cap), per-chapter artifacts, git log (40). Output: replacement machine tail only (≤80 lines, evidence-backed, heavy-first); Python reassembles header+sentinels, bloat-checks, auto-commits (or `.proposed`).

## 4. Division of labor as implemented

**Claude** (Max-billed subprocess, tool-less single completions): reviews state and *drafts every Aristotle prompt* (refine-reviewer/filler), *steers mid-flight* (Hermes → `project.ask`), *rates* output, and *maintains* the project rubric (referee). **Aristotle**: all proving/editing, inside its own sandbox on the uploaded bundle (it alone sees the `.tex`). **Python**: bundling, submit/poll/reattach/continue, tar extraction with overlay semantics, `lake build`, git commit/push/PR, `formalization.yaml` upkeep (incl. project-keyed wall-time sidecar), review-issue state machine. **Human**: verify/reject gate; rejections become the next iteration's exclusive directive. The plugin layer mirrors the Python-side Claude roles as agents and wraps the CLI as commands.

## 5. Gaps — SDK features the harness doesn't exploit

1. **Parallelism.** Everything is sequential: skeleton walks chapters one at a time, refine runs one project, the daemon is single-flight per chapter. The SDK is fully async and `list_projects(status=[QUEUED, IN_PROGRESS])` implies many concurrent projects; an `asyncio.gather` over chapters (or a multi-chapter daemon) would multiply throughput at zero code-architecture cost — `aristotle_runtime` is already async-shaped.
2. **`Project.create` with `tar_file_path`** — unused. A cached, pre-built bundle would avoid re-archiving the whole repo every iteration; prompt-only submission could serve quick informal lemma probes.
3. **No fleet visibility / reconciliation**: `list_projects` is never called, so orphaned in-flight projects (lost state files) are invisible; no spend/budget dashboard. `percent_complete` is never surfaced — easy UX win during long polls.
4. **No cancel path**: `task.cancel()`/`project.cancel()` unused. Hermes' never-cancel policy is sound, but a hard-stop guard (e.g. budget runaway or editing outside the repo) has no lever.
5. **Event stream underused**: only EDITING_FILE triggers action, and only under `--live-steering`. PROVING/ERROR/BUILDING/FINISHED events could drive a progress TUI (the `EventWatcher` protocol explicitly anticipates "future TUIs" — none exists), and ERROR events could trigger early retry instead of waiting for terminal status. Event pagination is never walked (newest-50 window per poll can drop events in bursts).
6. **`ask()` only for continuation/steering** — could also cheaply interrogate a live session ("summarize what remains") to enrich reviewer context instead of relying on post-hoc `output_summary`.
7. **Doc rot**: SDK-reference.md is 1.x while 2.0.0 is installed (AgentTask/Event/ask/get_tasks/get_events are undocumented in-repo except via `aristotle_runtime` docstrings); pricing/quota/Tips sections were never captured — worth re-pasting.
8. **`get_input` unused** — could verify bundle integrity when debugging extraction drift.

Key files: `/Users/jack/Desktop/LEAN/marathon/SDK-reference.md`, `/Users/jack/Desktop/LEAN/marathon/marathon/aristotle_runtime.py`, `/Users/jack/Desktop/LEAN/marathon/marathon/claude_review.py`, `/Users/jack/Desktop/LEAN/marathon/marathon/hermes_watcher.py`, `/Users/jack/Desktop/LEAN/marathon/plugins/marathon/{commands,agents}/`.