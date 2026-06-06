---
description: Iterate a Lean folder with Aristotle — Claude drafts, Aristotle proves.
argument-hint: "<target-folder> --repo-dir DIR [--skeleton] [--max-iterations N] [--live-steering] [--dry-run]"
allowed-tools: Read, Bash, Grep, Glob, Task
---

# /marathon:refine — review/submit refine loop

Drive `marathon refine`: for up to `--max-iterations` rounds, the **refine-reviewer** drafts an
Aristotle prompt from the current Lean state, marathon submits it, extracts the result back into
the target folder in place, and loops. Arguments: `$ARGUMENTS`.

## Preflight

1. **Echo resolved inputs**: target folder, `--repo-dir`, mode (`--skeleton` = scaffold/no-proofs
   vs. default proof-filling), `--max-iterations`, whether `--live-steering` is on.
2. Confirm `ARISTOTLE_API_KEY` is set (refine submits to Aristotle); the `claude` CLI uses its own
   Max OAuth. If the key is unset, stop.
3. Referee notes: use `--referee FILE` or auto-detect `<repo-dir>/.marathon/referee.md`.

## Run

Print the command, then run it (always offer `--dry-run` first for new setups — it prints the
resolved config and exits without calling Claude or Aristotle):

```bash
marathon refine <target-folder> \
    --repo-dir <repo-dir> \
    [--skeleton] [--max-iterations N] [--max-retries N] [--polling-interval SECONDS] \
    [--referee FILE] [--tex FILE] [--workdir DIR] \
    [--auto-referee-every N] [--no-cross-chapter] [--max-prompt-words N] \
    [--live-steering] [--no-continue-on-review] [--review-rejection ISSUE_NUM] \
    [--dry-run]
```

## How the Claude role maps to this plugin

The draft-prompt step is the **refine-reviewer** agent; `--live-steering` adds the
**hermes-steer** agent (per `EDITING_FILE` event); `--auto-referee-every N` runs the **referee**
agent. State is checkpointed in `<workdir>/marathon-refine-state.json` and
`marathon-refine-log.md`; steering decisions in `marathon-steering-log.jsonl`. Use `--skeleton`
to perfect the scaffold before any proofs, then re-run without it to fill `sorry`s.

## Next

Grade the result with `/marathon:rate`; refresh project reviewer priorities with
`/marathon:referee`.
