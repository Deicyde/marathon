# marathon — Aristotle driver plugin for Claude Code

A Claude Code plugin that drives **Marathon** — this repo's driver for the Aristotle (Harmonic)
automated theorem-proving API — as slash commands. Each command wraps the `marathon` CLI; the
Claude roles Marathon already shells out to (refine review, referee, Hermes steering, auto-rater)
are re-homed here as agents.

## Install

```
/plugin marketplace add Deicyde/marathon
/plugin install marathon@marathon-suite
```

(Local checkout instead of GitHub: `/plugin marketplace add /path/to/marathon`.)

## Commands

| Command | Wraps |
|---|---|
| `/marathon:skeleton` | `marathon skeleton` — submit a textbook to Aristotle chapter by chapter |
| `/marathon:refine` | `marathon refine` — iterate a Lean folder (Claude drafts, Aristotle proves) |
| `/marathon:referee` | `marathon referee` — refresh project reviewer notes (`referee.md`) |
| `/marathon:review` | `marathon review …` — human-in-the-loop per-declaration verify/reject |
| `/marathon:rate` | seven-dimension quality rating of a chapter's output |
| `/marathon:steer` | Hermes live-steering of an in-flight Aristotle run |

## Agents

`refine-reviewer`, `referee`, `hermes-steer`, `rater` — sourced from `marathon/prompts/*.md`.

## Prerequisites

The `marathon` CLI on `PATH`, an `ARISTOTLE_API_KEY` for submit steps, the `claude` CLI for
Marathon's Claude steps, and `gh` for `/marathon:review`.

## Optional pairing

If the **autoform** plugin (from the autoform-bot repo) is also installed, its `lean-conventions`,
`formalization-workflow`, and `eval-rubrics` skills are auto-discovered and used by these
commands. There is no hard dependency — the commands work without it.
