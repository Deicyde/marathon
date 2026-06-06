---
description: Verify or reject formalized declarations, one at a time.
argument-hint: "<list|next|show|verify|reject> [--chapter N | ISSUE] [--notes …]"
allowed-tools: Read, Bash, Grep, Glob
---

# /marathon:review — per-declaration review workflow

Drive `marathon review …`, the GitHub-issue-backed review subsystem (one sub-issue per
declaration: verify or reject-with-notes; rejections feed the auto-refine daemon). Arguments:
`$ARGUMENTS`.

## Preflight

- **Echo** the resolved action and target (chapter or issue number).
- This subsystem uses `gh` (GitHub CLI) and a `<repo-dir>/.marathon/review/config.toml`; confirm
  `gh auth status` is healthy for actions that touch issues.

## Actions (pass through to the CLI)

```bash
marathon review list --chapter N        # ordered sub-issues + statuses
marathon review next --chapter N        # next unreviewed sub-issue
marathon review show ISSUE_NUM          # display a sub-issue body
marathon review open ISSUE_NUM          # open an interactive Claude review chat (VS Code)
marathon review verify ISSUE_NUM        # mark verified
marathon review reject ISSUE_NUM --notes "..."   # queue a fix (triggers the daemon)
marathon review bootstrap-chapter --chapter N    # first-time: create the sub-issues
marathon review audit-chapter --chapter N        # refresh existing sub-issues
marathon review daemon                  # run the single-flight auto-refine daemon
```

Print the exact command before running it. For `reject`, confirm the notes are specific and
actionable (they become the next refine prompt). A rejection dispatches one
`marathon refine --skeleton --max-iterations 1 --review-rejection N` per iteration via the daemon
— surface that this will submit to Aristotle (needs `ARISTOTLE_API_KEY`).

## Relationship to the rest of the plugin

`verify`/`reject` are the human gate; `reject` ultimately routes through the **refine-reviewer**
agent (via `/marathon:refine`). Track overall progress with `marathon review list`.
