---
description: First-time bootstrap of a chapter's review sub-issues from its Lean files.
argument-hint: "<chapter-num> [--informal-statements FILE] [--repo-dir DIR] [--dry-run]"
allowed-tools: Read, Bash, Grep, Glob
---

# /marathon:review:bootstrap-chapter — open the per-declaration GitHub sub-issues

Drive `marathon review bootstrap-chapter`: the first-time entry point that walks a chapter's
Lean files, identifies its named results (theorems / definitions / propositions / exercises Lee
groups under section heads), and files one GitHub sub-issue per named result. Each sub-issue
quotes the relevant decl(s) and becomes the unit the human reviews via
`/marathon:review:verify <N>` or `marathon review reject <N> --notes …`. Arguments:
`$ARGUMENTS`.

The command writes a briefing file to `<repo-dir>/.marathon/review/sessions/c<N>-bootstrap-<timestamp>.md`
and opens an **interactive coreviewer chat in VS Code** (via the URI handler) where you and the
coreviewer collaboratively agree on the named results and the decls each one covers. The
coreviewer files the issues at the end of that chat. Issue numbers get recorded back into
`<repo-dir>/.marathon/review/config.toml`'s `[[chapters]] chapter = N` block as `entries`.

## When to use this vs `audit-chapter`

- **bootstrap-chapter**: first time you're opening review issues for a chapter
  (`config.toml`'s `entries = []` for this chapter, no sub-issues exist on GitHub).
- **`/marathon:review:audit-chapter`**: chapter has been bootstrapped before, you've added or
  reshuffled named results in the Lean files, and want the existing sub-issues refreshed to
  match the current code (rename, re-quote, file new ones for added results, flag stale ones).

## Preflight

1. **Echo resolved inputs**: the chapter number, `--repo-dir` (defaults to cwd), whether
   `--informal-statements` was given, and `--dry-run` state. Confirm the chapter's Lean folder
   exists at `<repo-dir>/GeometricAnalysis/LeeSM/Chapter<N>/` (or the project's analogous
   path).
2. Confirm `gh auth status` is healthy — the coreviewer will file real issues at the end of
   the session.
3. Inspect the chapter's `[[chapters]]` block in `.marathon/review/config.toml` — if `entries`
   is non-empty, you probably want `/marathon:review:audit-chapter` instead; surface that and
   pause for confirmation before running bootstrap (which would overlap with existing issues).
4. If you have a hand-written informal-statements markdown for this chapter (one section per
   named result), pass it via `--informal-statements`. Without it, the coreviewer LLM-renders
   statements with a `⚠️ verification pending` marker that you'll need to fix up later — fine
   for low-paraphrase-risk chapters, worth writing for analysis-heavy ones.

## Run

Print the command, then run it:

```bash
marathon review bootstrap-chapter \
    --chapter <N> \
    [--repo-dir <dir>] \
    [--informal-statements <path/to/c<N>-informal.md>] \
    [--dry-run]
```

`--dry-run` writes the briefing file and prints the would-be VS Code URI without opening it —
useful for inspecting the briefing template once before committing.

After the interactive chat finishes:

1. Run `marathon review list --chapter <N>` (or `/marathon:review list --chapter <N>`) to
   confirm the sub-issues were filed.
2. Diff `.marathon/review/config.toml` to confirm the `[[chapters]] chapter = <N>` block's
   `entries` field is now populated — that's what the rest of the review pipeline keys off of.
3. Commit the `config.toml` change.

## Relationship to the rest of the plugin

`bootstrap-chapter` is the first link in the review pipeline. Once it lands, the per-issue
workflow is the same as every other chapter:

- `/marathon:review next --chapter <N>` — surface the next unreviewed sub-issue.
- `/marathon:review:verify <N>` — verify + auto-merge the PR.
- `marathon review reject <N> --notes …` — queue a refine iteration via the daemon.
- `marathon review daemon --chapter <N>` — drain the rejection queue overnight.

## Next

If `config.toml`'s `entries` got populated, commit it, then `/marathon:review next --chapter
<N>` to start walking the queue. If the coreviewer flagged issues during the chat (e.g.
"section 12.4 has no clean named-result grouping, propose a split"), resolve those before
moving on — they'll be cheaper to fix now than after issues are filed.