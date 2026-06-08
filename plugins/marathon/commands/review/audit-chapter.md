---
description: Refresh a previously-bootstrapped chapter's review sub-issues against current Lean.
argument-hint: "<chapter-num> [--repo-dir DIR] [--dry-run]"
allowed-tools: Read, Bash, Grep, Glob
---

# /marathon:review:audit-chapter — refresh existing sub-issues

Drive `marathon review audit-chapter`: walk a chapter that has **already been bootstrapped**,
compare its current Lean state against the sub-issues recorded in
`<repo-dir>/.marathon/review/config.toml`, and reconcile drift. Same interactive-coreviewer-in-VS-Code
shape as `/marathon:review:bootstrap-chapter`, but the briefing tells the coreviewer what's
already on file so it can re-quote stale issue bodies, propose new issues for added results,
and flag issues whose named result no longer exists in the Lean. Arguments: `$ARGUMENTS`.

## When to use this vs `bootstrap-chapter`

- **bootstrap-chapter**: first time you're opening review issues for a chapter (`config.toml`'s
  `entries = []` for this chapter, no sub-issues exist on GitHub).
- **audit-chapter**: chapter has been bootstrapped before, you've added / renamed / deleted
  named results in the Lean files since, and want the existing sub-issues refreshed to match.

## Preflight

1. **Echo resolved inputs**: the chapter number, `--repo-dir` (defaults to cwd), `--dry-run`
   state. Confirm the chapter's `[[chapters]]` block in `.marathon/review/config.toml` has
   non-empty `entries` — if `entries = []`, you want `bootstrap-chapter` instead.
2. Confirm `gh auth status` is healthy — the coreviewer will edit / file real issues at the
   end of the chat.
3. Optionally `git log -- GeometricAnalysis/LeeSM/Chapter<N>/` since the last config.toml
   change for this chapter, to remind the coreviewer what's drifted. (Not required — the
   coreviewer will diff Lean ↔ issues directly during the chat.)

## Run

Print the command, then run it:

```bash
marathon review audit-chapter \
    --chapter <N> \
    [--repo-dir <dir>] \
    [--dry-run]
```

`--dry-run` writes the briefing file and prints the would-be VS Code URI without opening it.

After the interactive chat finishes:

1. `marathon review list --chapter <N>` to confirm the refreshed status of each sub-issue
   (renamed, body updated, newly filed, marked stale).
2. Diff `.marathon/review/config.toml` to confirm the `entries` list reflects any added /
   removed issues, then commit.
3. If the coreviewer flagged stale sub-issues (named result no longer in the Lean), decide
   per-issue whether to close them as obsolete or transfer the human-verified status to a
   replacement issue.

## Relationship to the rest of the plugin

Use this whenever the Lean has drifted from the on-GitHub issues — typically after a
multi-file refactor, a chapter merge, or a `/marathon:fill-file` run that introduced new named
results. Cheaper than re-bootstrapping (which would double-file every existing issue) and
safer than letting `/marathon:review next` walk an out-of-date queue.

## Next

`/marathon:review next --chapter <N>` to resume the per-issue workflow against the refreshed
queue.
