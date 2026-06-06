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
| `/marathon:fill` | `marathon fill` — fill one declaration's `sorry` body in a single iteration |
| `/marathon:fill-file` | `marathon fill-file` — fill every `sorry` in a single file in one iteration |
| `/marathon:referee` | `marathon referee` — refresh project reviewer notes (`referee.md`) |
| `/marathon:review` | `marathon review …` — human-in-the-loop per-declaration verify/reject |
| `/marathon:review:verify` | `marathon review verify` — verify a sub-issue and auto-merge its PR |
| `/marathon:rate` | seven-dimension quality rating of a chapter's output |
| `/marathon:steer` | Hermes live-steering of an in-flight Aristotle run |

## Agents

`refine-reviewer`, `single-decl-filler`, `referee`, `hermes-steer`, `rater` — sourced from
`marathon/prompts/*.md` and the plugin's `agents/` directory.

## Daemon (`marathon review daemon`)

The auto-refine daemon stays as a long-running CLI process (not a slash command). It watches
the per-chapter rejection queue in `.marathon/review/state.json` and dispatches one focused
refine iteration per slot. Because slash commands are per-invocation and don't survive across
sessions, the daemon is the right surface for "keep working overnight" — start it once with
`marathon review daemon` and let it run. The `/marathon:fill`, `/marathon:fill-file`, and
`/marathon:refine` commands are the right surface for human-driven one-shot landings during
an interactive Claude Code session.

## Prerequisites

The `marathon` CLI on `PATH`, an `ARISTOTLE_API_KEY` for submit steps, the `claude` CLI for
Marathon's Claude steps, and `gh` for `/marathon:review`.

## Optional pairing

If the **autoform** plugin (from the autoform-bot repo) is also installed, its `lean-conventions`,
`formalization-workflow`, and `eval-rubrics` skills are auto-discovered and used by these
commands. There is no hard dependency — the commands work without it.
