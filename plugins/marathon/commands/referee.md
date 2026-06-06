---
description: Refresh a project's reviewer notes (referee.md).
argument-hint: "--repo-dir DIR [--referee FILE] [--review]"
allowed-tools: Read, Bash, Grep, Glob, Task
---

# /marathon:referee — refresh referee.md tail

Drive `marathon referee`: one-shot pass of the **referee** agent that scans the repo,
per-chapter workdirs, and git log and rewrites the machine-managed tail of `referee.md` (the
project-specific reviewer notes), preserving the user-managed header verbatim. Arguments:
`$ARGUMENTS`.

## Preflight

1. **Echo resolved inputs**: `--repo-dir`, referee path (default `<repo-dir>/.marathon/referee.md`).
2. Confirm `--repo-dir` is a git repo. (The referee agent uses the `claude` CLI / Max OAuth — no
   Aristotle key needed.)

## Run

Print the command, then run it (or print only with `--dry-run` where supported):

```bash
marathon referee \
    --repo-dir <repo-dir> \
    [--referee FILE] [--workdirs-parent DIR] \
    [--review]
```

Default overwrites `referee.md` and auto-commits; `--review` writes `referee.md.proposed` for you
to inspect before applying. The agent keeps the tail tight (≤80 lines), deduplicated against the
generic rubric, evidence-backed, and heavy-first.

## Next

`/marathon:refine` reads the refreshed `referee.md` automatically and layers it onto the reviewer
rubric.
