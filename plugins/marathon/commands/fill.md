---
description: Fill a single declaration's `sorry` body with Aristotle.
argument-hint: "<target-folder> --repo-dir DIR (--decl NAME | --issue N) [--auto-pr/--no-auto-pr]"
allowed-tools: Read, Bash, Grep, Glob, Task
---

# /marathon:fill — single-declaration filler

Drive `marathon fill`: one focused refine iteration whose Hermes prompt is constrained to fill
**only** the named declaration's `sorry` body. Wraps `marathon refine` with a
`--focus-directive` and a single-iteration default, so all the existing refine machinery
(auto-build, auto-commit, auto-pr, rater, audit-verified) still applies. Arguments:
`$ARGUMENTS`.

You can target the decl one of two ways:

- `--decl NAME` — fully-qualified name (e.g. `DifferentialForm.coordinateCoframeWedge`).
- `--issue N` — fetch the GitHub sub-issue body and extract the decl(s) from its ` ```lean ``` `
  code blocks. Issue is **input**, not a requirement: pass it when you want the auto-PR to land
  on the per-issue branch (`marathon/refine-c<N>-i<issue>`) and to surface in the PR title.

## Preflight

1. **Echo resolved inputs**: target folder, `--repo-dir`, which of `--decl`/`--issue` was given,
   and the auto-PR settings. If `--issue` was used, print the decls that were extracted.
2. Confirm `ARISTOTLE_API_KEY` is set (Aristotle backend) and `gh auth status` is healthy if
   `--issue` or `--auto-pr` is in play.
3. If the target file is dirty in git, surface the diff before submitting — fill operates
   in-place and will be committed alongside any pre-existing edits if you allow auto-commit.

## Run

Print the command, then run it:

```bash
marathon fill <target-folder> \
    --repo-dir <repo-dir> \
    (--decl NAME | --issue N) \
    [--workdir DIR] [--max-retries N] [--polling-interval SECONDS] \
    [--build-timeout SECONDS] \
    [--auto-build | --no-auto-build] [--auto-commit | --no-auto-commit] \
    [--auto-push | --no-auto-push] [--auto-rate | --no-auto-rate] \
    [--auto-pr | --no-auto-pr] [--auto-pr-repo OWNER/NAME] [--auto-pr-base BRANCH] \
    [--audit-verified | --no-audit-verified]
```

Defaults match the slash-command use case: `--auto-build`, `--auto-commit`, `--auto-push`,
`--auto-rate`, `--auto-pr`, `--audit-verified` are all **on**. Pass the `--no-*` form to opt
out.

## How the Claude role maps to this plugin

Fill is one iteration of refine with a load-bearing focus directive. The draft-prompt step is
the **single-decl-filler** agent (a refine-reviewer variant tuned for single-decl edits) — it
echoes the focus directive at the top of the Aristotle prompt, then provides only the context
needed to fill that one body (the file's other decls, relevant imports, candidate Mathlib
lemmas, and any rejection notes from the issue if `--issue` was used).

The signature is **load-bearing**: the directive forbids edits to the decl's signature or to
any other decl in the file. The post-iteration verified-decl audit will flag any drift.

## Next

If the iteration didn't land, re-run with sharper context (point Aristotle at a specific lemma
in the rejection notes, or split off a helper). For multi-decl files, prefer `/marathon:fill-file`
which fills every sorry in one shot.
