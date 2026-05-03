# marathon

A driver for the [Aristotle](https://aristotle.harmonic.fun) (Harmonic) automated
theorem proving API. Submits a textbook chapter by chapter to Aristotle along
with the entire target Lean project, so each chapter's output lands directly in
the Lean repo and subsequent chapters automatically see prior chapters' Lean
code as context.

See `SDK-reference.md` for a captured copy of the `aristotlelib` Python SDK
documentation.

## Setup

Prerequisites: [`uv`](https://docs.astral.sh/uv/) and an Aristotle API key.

```bash
# 1. From this directory:
uv sync

# 2. Mint an API key at https://aristotle.harmonic.fun/dashboard/keys.
#    Add this line to ~/.zshrc by editing the file directly
#    (do NOT paste at an interactive prompt — it would be logged in
#     ~/.zsh_history):
#
#       export ARISTOTLE_API_KEY="arstl_..."
#
# 3. source ~/.zshrc
# 4. echo $ARISTOTLE_API_KEY   # to verify
```

Marathon refuses to run if `ARISTOTLE_API_KEY` is unset. It never logs or
writes the key.

## Input layout

Marathon expects:

- An **input folder** containing the textbook's LaTeX sources, an `order.txt`,
  and (optionally) `macros.sty`. Marathon's bookkeeping (`marathon.md`,
  `marathon-state.json`) also lives here.
- A **Lean repo** (a git repo) where chapter outputs will land and whose
  contents are bundled into every submission as context.

```
LeeSM-LaTeX/                       (input folder)
├── order.txt
├── c12.tex
├── c13.tex
├── ...
├── macros.sty                     (optional but recommended)
├── marathon.md                    (created/updated by Aristotle)
└── marathon-state.json            (created by Marathon)

GeometricAnalysis/                 (Lean repo)
├── .git/, .gitignore
├── lakefile.lean
├── lean-toolchain
├── GeometricAnalysis/
│   ├── LeeSM/
│   │   ├── Chapter10/             (your existing Lean code)
│   │   ├── Chapter11/
│   │   ├── Chapter12/             ← Marathon writes this
│   │   └── ...
└── ...
```

`order.txt` controls submission order and the per-chapter output folder name
(leaf only — the parent path comes from `--output-base`):

```text
# <input-tex-file>  -> <chapter-output-folder-name>
c12.tex             -> Chapter12
c13.tex             -> Chapter13
c14.tex             -> Chapter14
```

Comments (`#`) and blank lines are ignored. Both columns must be unique.

### Optional per-chapter instructions

Indented lines beneath a chapter entry are free-form per-chapter targets /
instructions, spliced into Aristotle's prompt as a "Per-chapter targets"
section. Aristotle is told these narrow the scope: only formalize material
needed to reach them, and ignore unrelated content in the chapter.

```text
c12.tex -> Chapter12
    - Prove Theorem 12.3 (chain rule for smooth maps).
    - Prove the inverse function theorem.
    - Skip the appendix on examples.

c13.tex -> Chapter13
    Just formalize Theorem 13.2; ignore everything else in the chapter.

c14.tex -> Chapter14
```

- A non-indented line starts a new chapter entry.
- Indented lines below it are continuation content for that entry.
- Common leading whitespace is stripped, so any consistent indent works.
- Blank lines inside an instruction block are preserved (paragraph breaks).
- Chapters with no continuation content (like `c14` above) get the default
  prompt with no scoping section — Aristotle formalizes the whole chapter.

## Usage

```bash
uv run python -m marathon skeleton /path/to/LeeSM-LaTeX \
    --repo-dir /path/to/GeometricAnalysis \
    --output-base GeometricAnalysis/LeeSM
```

For each chapter in `order.txt`, in order, Marathon will:

1. **Build a submission tree** containing:
   - The chapter's `.tex` file (e.g. `c12.tex`).
   - `macros.sty` from the input folder, if present.
   - `marathon.md` from the input folder, if present.
   - The entire `--repo-dir` Lean project, filtered by `.gitignore` (uses
     `git ls-files --cached --others --exclude-standard`, so tracked files
     plus untracked-not-ignored files ship and `.git/`, `.lake/`, etc. don't).
2. **Submit it** to Aristotle with the prompt template at
   `marathon/prompts/skeleton.md` (edit that file to refine the instructions).
   The template's `{output_path}` placeholder is substituted with
   `<output-base>/<chapter-output-folder>` (e.g. `GeometricAnalysis/LeeSM/Chapter12`).
3. **Poll every 60 s** until completion (configurable via `--polling-interval`).
4. **Extract the response**:
   - Files at `<output-base>/<chapter-output-folder>/...` in the response tar
     land at `<repo-dir>/<output-base>/<chapter-output-folder>/...`. Any
     stale contents are wiped first.
   - A top-level `marathon.md` in the response lands at
     `<input>/marathon.md`, overwriting the previous version.
   - Anything else in the tar is ignored (mostly Aristotle echoing back input).
5. **Checkpoint state** in `<input>/marathon-state.json` so re-runs resume.

For chapter 13 onward, the bundle automatically grows: since chapter 12's Lean
output is now part of the repo working tree, `git ls-files` includes it, and
Aristotle sees it as context.

### Flags

- `--repo-dir <path>` (required) — the Lean project repo. Must be a git repo.
- `--output-base <relative-path>` (required) — relative path within `--repo-dir`
  where chapter outputs go. Each chapter's output folder is appended.
- `--polling-interval N` — seconds between status checks (default: 60).
- `--max-retries N` — on `COMPLETE_WITH_ERRORS` or `FAILED`, retry the chapter
  up to N additional times (so up to N+1 total attempts). Default: 2.
- `--continue-on-error` — keep going past failed chapters instead of aborting
  (default: abort, since later chapters depend on earlier ones).

### Retry behavior

When a chapter ends with `COMPLETE_WITH_ERRORS` or `FAILED`, Marathon does
**not** treat it as final. Instead it:

1. Extracts any partial output the SDK gave us (only `COMPLETE_WITH_ERRORS`
   tends to produce extractable output; `FAILED` typically does not).
2. Submits a fresh Aristotle project for the same chapter, with the prompt
   template's `{retry_context}` placeholder filled in to tell Aristotle:
   it's a retry, what status the previous attempt had, whether partial output
   is bundled, and that it should continue from where it stopped rather than
   starting over.
3. Repeats up to `--max-retries` additional times.

If all attempts are exhausted, the chapter's recorded status becomes
`RETRIES_EXHAUSTED` and Marathon falls through to its normal failure handling
(abort the batch unless `--continue-on-error` is set).

`OUT_OF_BUDGET` and `CANCELED` are **not** auto-retried — the first is an
account-level signal that needs human attention, the second is a deliberate
user action.

### Reattach to an in-flight project

If a previous Marathon run died (terminal closed, network dropped, machine
rebooted) while a chapter's project was still on Aristotle's side, the next
run notices: it sees a `project_id` in `marathon-state.json` together with
a recorded status of `QUEUED` or `IN_PROGRESS`. Rather than submitting a
duplicate project, Marathon calls `Project.from_id(...)` and resumes polling
the existing one — extracting its result when it terminates and falling into
the normal retry logic from there.

This avoids burning Aristotle compute on duplicate submissions and means you
can Ctrl+C a run, close the terminal, or even reboot, and just rerun the
same command later to pick up cleanly. (For very long runs, you may still
want to launch under `tmux` so the run survives the terminal close in the
first place.)

If `Project.from_id` fails (e.g. the project ID is invalid or has been
deleted), Marathon logs a warning and falls back to a fresh submission.

## Status values written to `marathon-state.json`

Values from the `aristotlelib.ProjectStatus` enum (`COMPLETE`,
`COMPLETE_WITH_ERRORS`, `FAILED`, `OUT_OF_BUDGET`, `CANCELED`,
`QUEUED`, `IN_PROGRESS`), plus a few Marathon-internal markers:

- `SUBMIT_FAILED` — Aristotle rejected the initial submission.
- `POLL_FAILED` — an HTTP error occurred while polling for completion.
- `OUTPUT_FOLDER_MISSING` — Aristotle returned a tar but the expected output
  path was not present.
- `RETRIES_EXHAUSTED` — `--max-retries` additional attempts all failed; last
  attempt's status is recorded in the `note` field.

Re-running `marathon skeleton ...` with the same input folder skips chapters
whose recorded status is `COMPLETE` and retries everything else — including
`COMPLETE_WITH_ERRORS`. Only a fully clean completion counts as "done."

## Files in this repo

- `marathon/` — the package
  - `__main__.py` — CLI entry (`python -m marathon`)
  - `skeleton.py` — the `skeleton` subcommand
  - `order.py` — `order.txt` parser
  - `state.py` — `marathon-state.json` reader/writer
  - `prompts/skeleton.md` — prompt template (edit freely; substitutions are
    `{input_file}` and `{output_path}`)
- `SDK-reference.md` — captured Aristotle SDK reference
- `pyproject.toml`, `uv.lock` — dependency manifest

## Known unknowns to verify on the first real run

- The SDK's `create_from_directory` claims it skips "build artifacts and
  standard library packages." We additionally pre-filter with `git ls-files`,
  so the bundle should be clean either way — but if Aristotle reports missing
  files, double-check what made it through.
- Aristotle is expected to echo most of the input bundle back in the response
  tar (sub-paths under `<output-base>` other than the output folder, plus
  unmodified Lean project files). Marathon ignores those silently and only
  records a count in the per-chapter note. If the count is unexpectedly large
  or small, Aristotle may be doing something we didn't expect.
- The output tar layout is assumed to be Aristotle preserving the input
  directory structure. If Aristotle nests output under a wrapping directory
  or uses a different layout, `_extract_solution` in `skeleton.py` needs a
  tweak.
