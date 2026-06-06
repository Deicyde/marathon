---
description: Fill every `sorry` body in a single Lean file with Aristotle.
argument-hint: "<target-folder> --repo-dir DIR --file PATH [--auto-pr/--no-auto-pr]"
allowed-tools: Read, Bash, Grep, Glob, Task
---

# /marathon:fill-file — file-scoped filler

Drive `marathon fill-file`: one focused refine iteration whose Hermes prompt is constrained to
fill **every** `sorry`-bodied declaration in a single Lean file, leaving every other file in
the chapter exactly as it is. Wraps `marathon refine` with a `--focus-directive`. Arguments:
`$ARGUMENTS`.

Use this when you have a freshly-skeletonized file (or a file that came back from review with
several rejected decls) and want one Aristotle pass that tries to land them all together. The
focus directive enumerates the file's sorry-bodied decls by name so Aristotle can plan a single
coherent edit.

## Preflight

1. **Echo resolved inputs**: target folder, `--repo-dir`, `--file` (absolute path), and the
   auto-PR settings. Print the list of sorry-bodied decls the CLI extracts (`marathon fill-file`
   greps the file itself; you should see the names in its preamble output).
2. Confirm `ARISTOTLE_API_KEY` is set and `gh auth status` is healthy if `--auto-pr` is on.
3. If the file has no `sorry`-bodied decls, `marathon fill-file` exits with an error before
   submitting to Aristotle — that's the cheap fast-fail; nothing for you to do.

## Run

Print the command, then run it:

```bash
marathon fill-file <target-folder> \
    --repo-dir <repo-dir> \
    --file <path/to/file.lean> \
    [--workdir DIR] [--max-retries N] [--polling-interval SECONDS] \
    [--build-timeout SECONDS] \
    [--auto-build | --no-auto-build] [--auto-commit | --no-auto-commit] \
    [--auto-push | --no-auto-push] [--auto-rate | --no-auto-rate] \
    [--auto-pr | --no-auto-pr] [--auto-pr-repo OWNER/NAME] [--auto-pr-base BRANCH] \
    [--audit-verified | --no-audit-verified]
```

Defaults are the same as `/marathon:fill`: auto-build, auto-commit, auto-push, auto-rate,
auto-pr, audit-verified all **on**. Pass the `--no-*` form to opt out.

## How the Claude role maps to this plugin

File-fill is one iteration of refine with a file-scoped focus directive. The draft-prompt step
is the **single-decl-filler** agent — for file-fill mode the directive enumerates the file's
sorry-bodied decls and tells Aristotle to fill all of them in one edit (no signature changes,
no new files, no edits to siblings in the chapter). The post-iteration verified-decl audit
catches any drift outside the target file.

## When to prefer `/marathon:fill` over this

Single-decl fills are cheaper to land and easier to debug if Aristotle stumbles. Reach for
`/marathon:fill-file` when the decls share a proof strategy or share helper lemmas — bundling
them keeps Aristotle from inventing duplicate helpers across siblings.
