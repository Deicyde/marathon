# Running marathon (collaborator setup)

> Historical setup guide. As of November 6, 2026, Aristotle does not support
> Lean-only projects. The review UI and local audits remain useful, but the
> Aristotle refinement steps below cannot complete the original workflow.
> See the [release status](README.md#historical-release--november-6-2026).

Review (and optionally refine) locally, as yourself — no shared server.

## Prerequisites

- Python ≥ 3.11, [`uv`](https://docs.astral.sh/uv/), `gh`
- Access to this marathon repo + the project repo (e.g. `GeometricAnalysis`)
- *To refine (compute) only:* an Aristotle API key + the Lean toolchain (`lake`)

## Steps

```bash
# 1. install the marathon tool
git clone <marathon-repo-url> marathon && cd marathon && uv tool install .

# 2. clone the project repo (review config + state come with it)
git clone <project-repo-url> && cd <project-repo>

# 3. authenticate as yourself (verdicts are attributed to you)
gh auth login

# 4. review  →  opens http://127.0.0.1:<port>/
marathon deck

# 5. (optional) refine the rejects under your own key
export ARISTOTLE_API_KEY=<your key>
marathon conductor run --queue-from-github
```

In the deck: open an issue, swipe `→` verify / `←` reject (+ note) per
declaration, then **SHIP**. `git pull` (or `marathon deck --pull-every 60`) keeps
you current.
