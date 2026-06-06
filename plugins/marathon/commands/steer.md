---
description: Live-steer an in-flight Aristotle run (enable or test the watcher).
argument-hint: "[--enable | --test FILE]"
allowed-tools: Read, Bash, Grep, Glob, Task
---

# /marathon:steer — Hermes live steering

Hermes watches an in-flight Aristotle task and, on each `EDITING_FILE` event, decides whether to
nudge it back on course. There is no standalone CLI subcommand — steering runs *alongside*
`marathon refine`. Arguments: `$ARGUMENTS`.

## Enable (the normal path)

Live steering is a refine flag. To turn it on, run refine with `--live-steering`:

```bash
marathon refine <target-folder> --repo-dir <repo-dir> --live-steering [...]
```

(or invoke `/marathon:refine <target-folder> --live-steering`). The watcher subscribes to the
project event stream, calls the **hermes-steer** agent per `EDITING_FILE` event, sends approved
nudges via `project.ask(...)` (never cancels), logs each decision to
`<workdir>/marathon-steering-log.jsonl`, and persists running notes in
`<workdir>/hermes-memory.md`.

## Test a single decision (`--test`)

To dry-run the steering judgement on one edited file without a live Aristotle task, dispatch the
**hermes-steer** agent against the current target state and the named file; print its strict JSON
decision `{steer, reason, prompt, memory_note}`. Useful for tuning the intervention bar before a
real run.

The bar for steering is deliberately high — only clear, costly deviations (edits outside the
target folder, proofs written in `--skeleton` mode, forbidden patterns, unambiguous API/statement
mistakes), never mid-flight style nitpicks.
