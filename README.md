# marathon

Autoformalization with a human in the loop. Marathon turns a math textbook into
reviewed, merged Lean / Mathlib code: it drafts the formalization with Aristotle
(Harmonic's prover), you review each declaration in a local **web deck** — swipe
to verify or reject — and a **conductor** orchestrates Aristotle to refine the
rejects and merge the fixes, concurrently and hands-off.

> Marathon began as a batch extractor (`skeleton` / `refine`). v2 is built around
> the **review deck** and the **conductor**: a continuous review-and-refine loop
> with a web UI, per-declaration verdicts, and a shared multi-user mode.

## The loop

```
   formalize             review  (you, in the deck)      refine  (Aristotle)
   ─────────             ──────────────────────────      ───────────────────
   marathon skeleton  →  open an issue, swipe each    →  marathon conductor run
   / plan / fill         declaration ✓/✗, then SHIP:     picks up each rejected
                         all ✓ → merge the PR            issue, refines it, and
                         any ✗ → queue an Aristotle fix  auto-merges the green PR
        ▲                                                          │
        └──────────────────────────────────────────────────────────┘
                     repeat until every declaration is verified
```

1. **Formalize** — `marathon skeleton` extracts skeletal Lean outlines from the
   textbook in dependency order (or `plan` / `fill` for narrower targets).
2. **Review** — `marathon deck` opens a local web UI. The queue is an *issue*
   picker; opening an issue starts its **subdeck** — one declaration card at a
   time (statement, source, trust evidence). Swipe each declaration **verify** /
   **reject** (+ note) / **defer**; these are *local* verdicts and fire nothing.
   When every declaration is reviewed, an explicit **SHIP** composes the issue's
   outcome: merge the PR (all verified) or dispatch an Aristotle fix carrying your
   notes verbatim (any reject). Other tabs show live conductor status, every
   issue, and history.
3. **Refine** — `marathon conductor run` is the orchestrator: it picks up each
   rejection, dispatches an Aristotle refine in an isolated git worktree,
   re-reviews, and auto-merges the green PR — N concurrent jobs, oldest first.

## Install

From a clone of this repo (Python ≥ 3.11):

```bash
uv tool install .          # installs the `marathon` command globally
```

Marathon shells out to `gh` and Aristotle:

```bash
gh auth login                       # GitHub reads + verdict writes
export ARISTOTLE_API_KEY=…          # Harmonic prover (skeleton / refine / fill)
```

Per-repo config lives at `<repo>/.marathon/review/config.toml`.

## The deck — `marathon deck`

A `127.0.0.1` web app (vanilla JS, no build step). Run it from your repo:

```bash
marathon deck            # opens http://127.0.0.1:<port>/
marathon deck --demo     # synthetic cards, fires nothing — just try the UX
```

- **Review** — open an issue to start its subdeck, then review its declarations
  one card at a time: `←` reject, `→` verify, `space` / `↓` defer, `o` deep-dive,
  `?` for the full key list.
- **Status** — live conductor: running jobs, prover %, build phase, recent
  landings.
- **Issues** / **History** — every registered issue + its status; recent verdicts
  and merged PRs.

Swipes are *local* per-declaration verdicts and fire nothing on their own. The
single irreversible action is an explicit **SHIP**, available once every
declaration in the issue is reviewed: it merges the PR and flips the tracker (all
verified) or queues an Aristotle fix with your notes verbatim (any reject).
Loading or navigating the page never fires anything.

## The conductor — `marathon conductor run`

The single global orchestrator. It polls rejected issues across all chapters,
dispatches Aristotle refines in isolated worktrees (one at a time by default,
`--concurrency N`), re-reviews, and auto-merges eligible PRs each cycle. `marathon
conductor status` prints the live jobs table. Leave it running alongside the deck;
it survives the deck restarting.

## Sharing the deck (multi-user)

Collaborators can just run their own local deck — clone, install, `gh auth`, go;
see **[SETUP.md](SETUP.md)**. GitHub is the shared bus, so everyone's local deck
sees the same queue.

Or host one deck behind GitHub auth that collaborators reach by URL (for people
who won't install anything) — they sign in with their own GitHub account, act
under **their own** identity, and refine under **their own** compute key — see
**[docs/shared-deck.md](docs/shared-deck.md)**. The relevant
flags: `marathon deck --trusted-origin <origin> --no-host-conductor` (shared
deck) and `marathon conductor run --queue-from-github` (a collaborator's own
conductor, discovering the queue from GitHub).

## Reference — the other commands

| command | what it does |
|---|---|
| `skeleton` | Submit textbook chapters in dependency order; download skeletal Lean outlines. |
| `refine` | Iteratively improve a Lean folder (Claude reviews → Aristotle edits) — what the conductor runs per rejection. |
| `fill` / `fill-file` | Fill the `sorry` body of one declaration / every `sorry` in one file. |
| `plan` | Build the target ledger from an axiom, a repo's `sorry`s, or a textbook. |
| `review` | CLI verdicts — `verify` / `reject` / `sync` — the deck's non-UI equivalent. |
| `audit` | Elaborator-grade declaration audit → `.marathon/audit/latest.json`. |
| `landing` | Serial landing queue onto a `marathon/next` integration branch. |
| `ledger` | Inspect the runtime ledger (`.marathon/marathon.db`, SQLite). |

Run `marathon <command> --help` for flags.

## Where state lives

Everything is under `<repo>/.marathon/`:

- `review/config.toml` — project settings (repo, labels, chapter registry).
- `review/state.json` + `verdicts.jsonl` — verdicts and the rejection queue.
- `audit/latest.json` — the declaration-trust snapshot the deck reads.
- `conductor/` — the conductor lock + `jobs.json` snapshot.
- `marathon.db` — the SQLite runtime ledger.

Re-running a command resumes from this state — nothing already done is recomputed.
