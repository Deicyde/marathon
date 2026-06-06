---
description: Mark a review sub-issue verified and auto-merge its marathon PR.
argument-hint: "<issue-num> [--repo-dir DIR] [--notes …]"
allowed-tools: Read, Bash, Grep, Glob
---

# /marathon:review:verify — verify + auto-merge

Drive `marathon review verify`: mark a review sub-issue as verified, close it on GitHub, and
auto-merge the open marathon PR that landed it (the per-issue branch
`marathon/refine-c<N>-i<issue>` produced by `--auto-pr`). Arguments: `$ARGUMENTS`.

If the issue was filled without `--auto-pr` (no marathon PR for it), the verify step still
runs cleanly — the merge step is a no-op when no matching PR is found.

## Preflight

1. **Echo the resolved issue number and the repo-dir** that will be used (defaults to the
   current working directory). Confirm the sub-issue exists and is currently open.
2. Confirm `gh auth status` is healthy — verify writes a label/comment to GitHub and merges via
   `gh pr merge --merge --delete-branch`.
3. Show `marathon review show <issue>` so the human can sanity-check the decl(s) one last time
   before stamping verified. Pause for confirmation if anything in the body looks off (wrong
   namespace, missing axiom check, suspicious signature).

## Run

Print the command, then run it:

```bash
marathon review verify <issue-num> \
    [--repo-dir <dir>] \
    [--notes "freeform verification notes appended to the issue"]
```

The CLI will:

1. Apply the `review:verified` label and close the sub-issue.
2. Update `<repo-dir>/.marathon/review/state.json` (the per-chapter status mirror).
3. Look up the marathon PR whose head branch is `marathon/refine-c<N>-i<issue>` and run
   `gh pr merge --merge --delete-branch` on it. Already-merged PRs and missing PRs are silent
   no-ops. Conflicted PRs surface their `gh` error and leave the PR open for manual rebase.

## Relationship to the rest of the plugin

`verify` is the human gate that turns "Aristotle's PR built and Hermes drafted the review" into
"this declaration is landed on `main`". The opposite gate is `marathon review reject <N>
--notes …`, which queues a fresh refine iteration on the daemon. After a `verify`, the daemon's
per-chapter lock unblocks any rejection in the queue that was waiting on this slot.

## Next

Run `marathon review next --chapter N` (or `/marathon:review next --chapter N`) to surface the
next unreviewed sub-issue. Use `/marathon:fill <N>` for one-shot landings that bypass the
rejection-queue path entirely.
