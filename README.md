# marathon

A driver for the [Aristotle](https://aristotle.harmonic.fun) (Harmonic)
automated theorem-proving API. Two subcommands:

- **`skeleton`** — submit a textbook chapter by chapter, bundling a target
  Lean project as context, so each chapter's output lands directly in the
  project and subsequent chapters automatically see prior chapters' Lean code.
- **`refine`** — iteratively improve an existing Lean folder. Claude reviews
  the current state and drafts a prompt for Aristotle; Marathon submits and
  extracts the result back in place; the loop repeats up to `--max-iterations`.

## Setup

Prerequisites:

- [`uv`](https://docs.astral.sh/uv/) for Python deps.
- An **Aristotle API key** for `skeleton` and `refine` (both submit to
  Aristotle).
- The **Claude Code CLI** (`claude`) for `refine` only — it's invoked as a
  subprocess for the Claude review step, so refine bills against your
  Claude Max subscription instead of pay-per-token API credits. Install per
  https://claude.com/product/claude-code/ and run `claude` once
  interactively to authenticate.

```bash
uv sync                          # install dependencies into .venv/
echo $ARISTOTLE_API_KEY          # confirm the env var is set
which claude                     # confirm the CLI is on PATH (refine only)
```

Mint an Aristotle key at https://aristotle.harmonic.fun/dashboard/keys and
add it to `~/.zshrc` — edit the file directly, pasting at the prompt logs
it in shell history:

```bash
export ARISTOTLE_API_KEY="arstl_..."
```

Marathon refuses to run if `ARISTOTLE_API_KEY` isn't set, and never logs
the value. The `claude` CLI uses its own keychain-based OAuth from your
Max login — no env var required.

## `marathon skeleton`

```bash
uv run python -m marathon skeleton <input-folder> \
    --repo-dir <lean-repo> \
    --output-base <relative-path-within-repo>
```

Marathon expects two locations:

- **`<input-folder>`** — directory holding the textbook's `.tex` sources, an
  `order.txt`, and (optionally) `macros.sty`. Marathon also writes its own
  bookkeeping here: `marathon-state.json` and a shared `marathon.md` log.
- **`<lean-repo>`** — a git repository where chapter outputs land. Its tracked
  + untracked-not-gitignored contents are bundled into every submission as
  context for Aristotle. Outputs go under
  `<lean-repo>/<output-base>/<chapter-folder>/`.

Example layout:

```
book-tex/                        (input folder)
├── order.txt
├── chap01.tex
├── chap02.tex
├── macros.sty                   (optional but recommended)
├── marathon.md                  (created/updated by Aristotle)
└── marathon-state.json          (created by Marathon)

my-lean-project/                 (--repo-dir; must be a git repo)
├── .git/, .gitignore, lakefile.lean, lean-toolchain
└── MyProject/
    └── Chapters/                (--output-base = "MyProject/Chapters")
        ├── Chapter01/           ← Marathon writes these
        └── Chapter02/
```

## `order.txt`

Header lines list chapters in submission order:

```text
chap01.tex -> Chapter01
chap02.tex -> Chapter02
chap03.tex -> Chapter03
```

Both columns must be unique. Comments (`#`) and blank lines are ignored.

### Optional per-chapter targets

Indented lines beneath a header become free-form instructions for that
chapter, spliced into Aristotle's prompt as a "Per-chapter targets" section.
When present, they tell Aristotle to narrow scope and ignore unrelated
content.

```text
chap03.tex -> Chapter03
    - Prove Theorem 3.5 (the main result).
    - Skip the historical examples in §3.7.

chap04.tex -> Chapter04
    Just formalize the lemma on page 80.
```

Common leading whitespace is stripped, blank lines inside an instruction
block become paragraph breaks, and chapters with no continuation lines get
the default prompt.

## Flags

| Flag | Required | Default | Purpose |
|---|---|---|---|
| `--repo-dir <path>` | yes | — | Lean project repo (must be a git repo). |
| `--output-base <rel-path>` | yes | — | Where chapter folders go inside the repo. |
| `--polling-interval N` | no | 60 | Seconds between status polls. |
| `--max-retries N` | no | 2 | Extra attempts on `COMPLETE_WITH_ERRORS` / `FAILED`. |
| `--continue-on-error` | no | off | Don't abort the batch on chapter failure. |

## Retries

A chapter is only "done" when Aristotle returns `COMPLETE`. On
`COMPLETE_WITH_ERRORS` or `FAILED`, Marathon resubmits a fresh project with a
"Continuation context" note in the prompt explaining the previous attempt's
status and any partial output it produced. After `--max-retries` further
attempts, the chapter's status becomes `RETRIES_EXHAUSTED` and Marathon falls
through to its failure handling (abort unless `--continue-on-error`).

`OUT_OF_BUDGET` and `CANCELED` are **not** retried — the first needs human
attention, the second is a deliberate user action.

## Recovery from interrupted runs

If Marathon dies (Ctrl+C, terminal close, reboot) while a chapter's project
is still in flight on Aristotle's side, the next run reattaches via
`Project.from_id(...)` rather than submitting a duplicate. Just rerun the same
command — Marathon picks up where it left off with no wasted compute.

For long runs that should survive terminal close in the first place, launch
under [`tmux`](https://github.com/tmux/tmux) and prevent system sleep with
`caffeinate -i`:

```bash
tmux new -s marathon
caffeinate -i uv run python -m marathon skeleton <input-folder> \
    --repo-dir <lean-repo> --output-base <relative-path>
# detach: Ctrl-B then D    |    reattach: tmux attach -t marathon
```

## `marathon refine`

Iteratively improves an existing Lean folder. Each iteration runs one Claude
call (review + draft an Aristotle prompt) followed by one Aristotle
submission (with retries). Loops up to `--max-iterations` times.

```bash
uv run python -m marathon refine <target-lean-folder> \
    --repo-dir <lean-repo> \
    [--tex <tex-file>] \
    [--workdir <dir>] \
    [--max-iterations 3] [--max-retries 2]
```

- **`<target-lean-folder>`** — a folder inside `--repo-dir`. Aristotle's
  output overwrites this folder in place each iteration.
- **`--repo-dir`** — the Lean project repo (must be a git repo). Bundled
  into every Aristotle submission, gitignore-filtered.
- **`--tex`** (optional) — a `.tex` reference file the user supplies for
  Aristotle. Bundled at the top level of every Aristotle submission. **Claude
  is never given its contents** — only Aristotle sees it.
- **`--workdir`** (optional, default: cwd) — where Marathon writes
  `marathon-refine-state.json` and `marathon-refine-log.md`, and reads
  `marathon.md` from if present.
- **`--max-iterations N`** (default: 3) — total number of refinement
  iterations. Each iteration costs one Claude call (Opus 4.7 with adaptive
  thinking + `xhigh` effort) plus one or more Aristotle submissions.
- **`--max-retries N`** (default: 2) — per-iteration: extra Aristotle
  attempts on `COMPLETE_WITH_ERRORS` / `FAILED`. Same semantics as
  `skeleton`.
- **`--skeleton`** — skeleton mode. Instructs Aristotle to keep every
  theorem / lemma / proposition / corollary body as `sorry` (no proofs)
  and switches Claude's reviewer rubric to focus on signature and
  definition quality: correctness, future-proofness, and idiomatic
  Mathlib style. Use this to iterate on the scaffold before filling in
  any proofs.
- **`--max-prompt-words N`** — constrain Claude's drafted Aristotle prompt
  to roughly N words. Tells Claude to cut redundant prose, prefer short
  bullets, and trim long code blocks. Useful for A/B-testing how prompt
  length affects Aristotle's behavior. Default: no limit.
- **`--dry-run`** — print the resolved configuration and exit without
  calling Claude or Aristotle.

### Per-iteration flow

1. Claude reads the current state of the target folder, every other Lean
   file in the repo (gitignore-filtered), `marathon.md` from the workdir
   (if present), and the past refinement log. It does **not** read any
   `.tex` file.
2. Claude writes a prompt for Aristotle directly — the response is sent
   verbatim, no parsing. Marathon appends a "where to put output" trailer.
3. Marathon submits to Aristotle with the repo + `--tex` file (if any) +
   `marathon.md` bundled.
4. Standard retry/reattach machinery applies (same as `skeleton`).
5. Aristotle's output replaces the contents of `<target-lean-folder>`.
6. The Claude prompt and Aristotle outcome are appended to
   `marathon-refine-log.md`. Subsequent iterations read this log so Claude
   knows what's been tried.

The loop ends when `--max-iterations` is reached, when an iteration's
Aristotle attempts hit `RETRIES_EXHAUSTED`, or on `OUT_OF_BUDGET` /
`CANCELED`.

### How Claude is configured

Refine invokes the **Claude Code CLI** (`claude`) as a subprocess, billed
against your Max subscription. Each call uses `claude-opus-4-7` with
`--bare --tools ""` (no agent loop, no tool use — pure single-shot
completion). The system rubric is in `marathon/prompts/review.md` (or
`review_skeleton.md` with `--skeleton`); both rubric and per-iteration
context are concatenated into one `-p` prompt.

Trade-off vs the API path: prompt caching isn't exposed via the CLI, so
each iteration pays full token cost. Free for Max users; would matter if
you flipped back to API billing. To switch to direct API calls instead,
edit `marathon/claude_review.py` to use the `anthropic` SDK and add it
back to `pyproject.toml`.

## Post-extraction pipeline (optional)

Both `skeleton` and `refine` accept three independent flags that run after
each successful extraction. All default off.

| Flag | What it does |
|---|---|
| `--auto-build` | Runs `lake build` in `--repo-dir`. Captures exit code + log tail. Build failure does **not** abort the pipeline. Configurable timeout via `--build-timeout SECONDS` (default 600 = 10 min); on timeout the build is killed and recorded as `TIMED OUT`. |
| `--auto-commit` | Stages just the chapter's output folder (`git add <output-path>`) and `git commit`s with an auto message including build status (if known) and the project ID. No push. Skipped with a warning if the git index is busy or there's nothing to commit. |
| `--auto-rate` | Spawns Claude (Max-billed subprocess) to rate the code 1–5 across `quality`, `math_correctness`, `generality`, `api_coverage`, `modern_lean4`, with a one-paragraph note. Appends one JSON line per rating to `<workdir>/marathon-ratings.jsonl`. |

Per-chapter / per-iteration output:
```
done: COMPLETE  duration=42.3m  output=...
build: OK (24s)
commit: a3f8c12  message="marathon: Chapter12 [build:OK] (project=db243d68)"
rating: q=4 m=5 g=3 api=3 lean4=4
notes: "Statements are accurate but several lemmas could be generalized..."
```

Mix-and-match: `--auto-commit` alone (no build, no rating) just snapshots
each chapter to git. `--auto-rate` alone (no commit) collects quality data
without touching git.

## State

Marathon writes `<input-folder>/marathon-state.json` with one entry per
chapter. The `status` field is either an Aristotle `ProjectStatus` value
(`COMPLETE`, `COMPLETE_WITH_ERRORS`, `FAILED`, `OUT_OF_BUDGET`, `CANCELED`,
`QUEUED`, `IN_PROGRESS`) or a Marathon-internal marker:

- `SUBMIT_FAILED` — Aristotle rejected the initial submission.
- `POLL_FAILED` — an HTTP error occurred while polling.
- `OUTPUT_FOLDER_MISSING` — the response tar lacked the expected output folder.
- `RETRIES_EXHAUSTED` — used up `--max-retries` additional attempts.

Re-running with the same input folder skips chapters whose recorded status
is `COMPLETE` and retries everything else.
