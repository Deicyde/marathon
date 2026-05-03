# marathon

A driver for the [Aristotle](https://aristotle.harmonic.fun) (Harmonic)
automated theorem-proving API. Submits a textbook chapter by chapter, bundling
a target Lean project as context, so each chapter's output lands directly in
the project and subsequent chapters automatically see prior chapters' Lean
code.

## Setup

Prerequisites: [`uv`](https://docs.astral.sh/uv/) and an Aristotle API key.

```bash
uv sync                          # install dependencies into .venv/
echo $ARISTOTLE_API_KEY          # confirm the env var is set
```

Mint a key at https://aristotle.harmonic.fun/dashboard/keys, then add this to
`~/.zshrc` (edit the file directly — pasting at the prompt logs it in shell
history):

```bash
export ARISTOTLE_API_KEY="arstl_..."
```

Marathon refuses to run if the variable isn't set, and never logs the value.

## Usage

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
