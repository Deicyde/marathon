# Marathon architecture

This document describes the code that is running today. The reports under
`docs/v2-analysis/` and `docs/marathon-v2-plan.md` are design history: useful
for rationale, but not an authoritative map of the current web harness.

## Runtime flow

```text
browser
  -> marathon.deck.server (loopback HTTP + guarded writes)
       -> marathon.deck.cards/subdeck (read model and local review state)
       -> marathon.review (GitHub projection and verdict state)
       -> marathon.audit + marathon.ledger (trust evidence)

marathon.conductor
  -> isolated git worktrees
       -> marathon.refine/fill/repair
            -> Aristotle + Lean build/gates
  -> marathon.review.automerge
       -> GitHub PR/issue projection
       -> refreshed audit and review queue
```

The browser and conductor do not call each other directly. They coordinate
through `.marathon/` runtime state, the SQLite ledger, git, and GitHub.

## Package boundaries

| Path | Responsibility |
| --- | --- |
| `marathon/__main__.py`, `marathon/cli/` | CLI composition and command adapters |
| `marathon/aristotle_runtime.py`, `refine.py`, `fill.py`, `repair.py` | Prover sessions and focused code changes |
| `marathon/conductor.py` | Scheduling, retry, worktree ownership, and job recovery |
| `marathon/review/` | Chapter intake, issue/verdict state, GitHub sync, and automerge |
| `marathon/deck/` | Web API, card/subdeck assembly, dashboard reads, and reconciliation |
| `marathon/deck/static/` | Build-free HTML, CSS, and JavaScript client |
| `marathon/audit/` | Lean-derived trust evidence, kernels, probes, and invalidation |
| `marathon/prover/` | Declaration/issue DAG used by automated proving |
| `marathon/ledger.py` | SQLite event and evidence store |
| `tests/` | Unit, contract, regression, and fixture tests |

Keep orchestration deterministic. Claude and Aristotle may propose or perform
bounded work, but queue transitions, retries, merges, and state reconciliation
belong in Python and must be testable without either service.

## State ownership

| State | Location | Owner |
| --- | --- | --- |
| Project configuration and chapter registry | `.marathon/review/config.toml` | Review configuration commands |
| Issue verdict/retry state | `.marathon/review/state.json` plus ledger events | Review/conductor compatibility layer |
| In-progress declaration verdicts | `.marathon/review/deck-subdecks.json` | Deck only; local until SHIP |
| Trust snapshot | `.marathon/audit/latest.json` | Audit engine |
| Durable event/evidence cache | `.marathon/marathon.db` | Ledger |
| Running job snapshot and logs | `.marathon/conductor/` | Conductor |
| Shared review record | GitHub issues, labels, comments, and PRs | Idempotent review projection |

JSON compatibility files still exist while ledger migration is incomplete.
Every writer must use atomic replacement; cross-process state should move to a
transactional ledger operation rather than adding another mutable sidecar.

## Safety invariants

- GET routes and page navigation are read-only.
- Every browser write is same-origin and session-token guarded.
- Per-declaration swipes are local. Only explicit SHIP applies an issue verdict.
- Human rejection notes reach Aristotle verbatim.
- A verified declaration is fingerprint-pinned and reopens when its source
  changes; a hard lock additionally forbids signature changes during refine.
- A recoverable worker failure must not consume or strand its rejection.
- Publication and PR-observation failures resume from the existing commit and
  prover checkpoint; they must not reset the branch or buy another prover run.
- Dependency data may affect order and readiness, but missing/stale data must
  degrade visibly and must never wedge the queue.
- Reconciliation may append a mechanically grounded live declaration, but it
  never deletes a stale citation or infers declaration identity from a shared
  name/diff alone. Rename and split candidates require human confirmation.

## Development checks

Run the complete suite from the repository root:

```bash
uv run pytest -q
```

For web changes, start the side-effect-free harness and exercise the controls in
a browser as well as running the deck tests:

```bash
uv run marathon deck --demo --no-open --port 8765
uv run pytest -q tests/test_deck_*.py
```

For intake and conductor changes, use the focused suites before the full run:

```bash
uv run pytest -q tests/test_chapter_intake.py
uv run pytest -q tests/test_conductor.py tests/test_conductor_reap.py \
  tests/test_daemon_retry.py tests/test_review_automerge.py
```

## Current product backlog

The active web-harness backlog is tracked in Marathon-v2 issues
[#1](https://github.com/Deicyde/marathon-v2/issues/1) through
[#12](https://github.com/Deicyde/marathon-v2/issues/12). New code should reuse
the existing audit/source indexes and DAG machinery rather than creating a
parallel dependency or declaration-identity model.
