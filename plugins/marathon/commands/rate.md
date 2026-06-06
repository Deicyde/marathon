---
description: Score a Lean folder across seven quality dimensions.
argument-hint: "<target-folder>"
allowed-tools: Read, Bash, Grep, Glob, Task
---

# /marathon:rate — seven-dimension auto-rater

Score a freshly produced chapter folder for a fast diagnosis (the same rater marathon runs as the
post-pipeline `--auto-rate` step). Arguments: `$ARGUMENTS`.

## Steps

1. **Echo** the target folder being rated.
2. Dispatch the **rater** subagent on the folder's `.lean` files. It returns a single-line JSON
   object scoring 1–5 on: quality, math_correctness, generality, api_coverage, concision,
   modern_lean4, structural_focus.
3. Print the JSON, then a one-line human summary highlighting the lowest dimension(s) and a
   concrete next action.

This is a **diagnosis, not a gate**. For a gating verdict against the source, use the
**eval-rubrics** jury (the `autoform` plugin's `/autoform:eval`, or a manual rubric pass). Rating
notes accumulate in the chapter's `marathon-ratings.jsonl` and feed back into the next
`/marathon:refine` iteration's context.
