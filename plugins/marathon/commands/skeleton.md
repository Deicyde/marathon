---
description: Submit a textbook to Aristotle, chapter by chapter.
argument-hint: "<input-folder> --repo-dir DIR --output-base REL [--dry-run]"
allowed-tools: Read, Bash, Grep, Glob
---

# /marathon:skeleton — chapter-by-chapter submission

Drive `marathon skeleton`: for each line of `<input-folder>/order.txt`, submit the named `.tex`
(bundled with the whole `--repo-dir` Lean project, `macros.sty`, and `marathon.md`) to Aristotle,
landing outputs at `<repo-dir>/<output-base>/<chapter-folder>/`. Arguments: `$ARGUMENTS`.

## Preflight

1. **Echo resolved inputs**: input folder, `--repo-dir`, `--output-base`.
2. Confirm `ARISTOTLE_API_KEY` is set (skeleton submits to Aristotle); if unset, stop and tell the
   user to export it — never log the value.
3. Sanity-check `<input-folder>/order.txt` exists and parses (`chapNN.tex -> ChapterNN`, unique
   columns); confirm `--repo-dir` is a git repo.

## Run

Print the command, then run it (or print only with `--dry-run`):

```bash
marathon skeleton <input-folder> \
    --repo-dir <repo-dir> \
    --output-base <output-base> \
    [--continue-on-error] [--max-retries N] [--polling-interval SECONDS]
```

Progress is checkpointed in `<input-folder>/marathon-state.json` — on interrupt, re-running
resumes. Surface each chapter's status (COMPLETE / COMPLETE_WITH_ERRORS / FAILED / OUT_OF_BUDGET)
as it lands.

## Next

After the skeleton lands, iterate quality with `/marathon:refine <chapter-folder> --skeleton`
(scaffold pass) then without `--skeleton` (proof-filling), and grade with `/marathon:rate`.
