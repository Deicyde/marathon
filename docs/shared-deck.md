# Sharing the deck: a GitHub-gated, multi-user review deck

> Historical deployment guide. Aristotle's Lean-only workflow is unavailable
> as of November 6, 2026. Hosting the review UI does not restore automated
> proving or refinement. See the [release status](../README.md#historical-release--november-6-2026).

`marathon deck` is a localhost review UI: it binds `127.0.0.1` **only**, and its
verdicts perform irreversible GitHub actions (merge PRs, set labels, dispatch
Aristotle). This guide shows how to share it with trusted collaborators — anyone
with access to the repo — so that:

- they sign in with **their own GitHub account** (only repo-collaborators get in);
- their actions are **attributed to them** (their GitHub token, not yours);
- their rejects refine under **their own compute key** (their own conductor).

The deck never leaves `127.0.0.1`. An authenticating reverse proxy
(`oauth2-proxy`) and a tunnel (`cloudflared`) are what make it reachable and
gate who gets in — the deck itself has no user model, so do **not** rebind it
off loopback.

## Architecture

```
collaborator's browser
   │  GitHub login (must have access to the repo)
   ▼
cloudflared              public HTTPS, no inbound ports opened on your machine
   ▼
oauth2-proxy             --github-repo=<owner>/<repo>  --pass-access-token
   │                     forwards X-Forwarded-Access-Token (their OAuth token)
   ▼
marathon deck            127.0.0.1 (loopback)  --trusted-origin … --no-host-conductor
   │                     GitHub writes use the forwarded token → attributed to them
   ▼ reject → recorded on GitHub (label + comment), NOT refined here
collaborator's own  `marathon conductor run --queue-from-github`   ← their ARISTOTLE_API_KEY
```

## The flags this uses

| flag | command | what it does |
|---|---|---|
| `--trusted-origin <origin>` | `marathon deck` | Accept POSTs whose browser `Origin` is your public proxy host (without it the same-origin guard 403s every proxied verdict). Repeatable. |
| `--no-host-conductor` | `marathon deck` | A reject **records** the rejection but spawns **no** conductor on this host — compute is delegated to collaborators' own conductors. Env: `MARATHON_NO_HOST_CONDUCTOR`. |
| `--queue-from-github` | `marathon conductor run` | Discover the rejection queue from the GitHub `review:rejected` label (recovering each note from the reject comment) instead of the local `state.json`, so a conductor on **any clone** sees rejects fired on the shared deck. Env: `MARATHON_QUEUE_FROM_GITHUB`. |

## The credential model

The two halves of "act under your own keys" have different mechanics, because a
verdict is cheap and GitHub-facing while a refine is expensive and asynchronous.

**GitHub actions — per-user, synchronous.** Every GitHub write a verdict triggers
(issue comment, label add/remove, PR merge, issue-body edit) runs through one `gh`
wrapper. When a request carries `X-Forwarded-Access-Token` (set by `oauth2-proxy
--pass-access-token`), the deck binds that token as `GH_TOKEN` for the duration of
the request, so the write is authenticated **and attributed** to the signed-in
collaborator. No header (the local single-user path) → your ambient `gh` auth,
unchanged.

**Compute — per-collaborator, asynchronous.** Firing a reject does **not** run
Aristotle; it records the rejection (GitHub label + comment + local state) and a
separate long-running **conductor** later refines it. The shared deck runs with
`--no-host-conductor`, so it never spends your Aristotle key. Each collaborator
runs their own conductor with `--queue-from-github`, under their own
`ARISTOTLE_API_KEY`, which discovers rejects from GitHub and refines them on their
key.

## Setup

### 1. GitHub OAuth App

github.com → Settings → Developer settings → **OAuth Apps** → New:

- Homepage URL: `https://marathon.example.com`
- Authorization callback URL: `https://marathon.example.com/oauth2/callback`

Copy the **Client ID** and generate a **Client Secret**.

### 2. oauth2-proxy — the GitHub gate

```bash
oauth2-proxy \
  --provider=github \
  --github-repo=<owner>/<repo> \
  --scope="repo user:email" \
  --client-id="$GH_OAUTH_CLIENT_ID" \
  --client-secret="$GH_OAUTH_CLIENT_SECRET" \
  --cookie-secret="$(openssl rand -base64 32 | tr -- '+/' '-_')" \
  --email-domain='*' \
  --cookie-secure=true \
  --reverse-proxy=true \
  --pass-access-token=true \
  --redirect-url=https://marathon.example.com/oauth2/callback \
  --upstream=http://127.0.0.1:8770 \
  --http-address=127.0.0.1:4180
```

`--github-repo` admits **only** GitHub users with access to that repo;
`--pass-access-token` forwards each user's token to the deck.

### 3. Public HTTPS — a cloudflared named tunnel

```bash
cloudflared tunnel login
cloudflared tunnel create marathon          # prints a TUNNEL_ID + a creds .json
cloudflared tunnel route dns marathon marathon.example.com
```

`~/.cloudflared/config.yml` (or `--config`):

```yaml
tunnel: <TUNNEL_ID>
credentials-file: /Users/<you>/.cloudflared/<TUNNEL_ID>.json
ingress:
  - hostname: marathon.example.com
    service: http://127.0.0.1:4180     # → oauth2-proxy, NOT the deck directly
  - service: http_status:404
```

```bash
cloudflared tunnel run marathon
```

### 4. The deck — shared mode

```bash
marathon deck --port 8770 --no-open \
  --trusted-origin https://marathon.example.com \
  --no-host-conductor \
  --pull-every 60
```

`--pull-every 60` keeps a HOSTED deck current: a fast-forward `git pull` every 60
seconds, so the cards reflect the latest merged Lean source (issues come live
from GitHub regardless). It only fast-forwards — it never clobbers, because a
hosted deck never commits locally (merges happen on GitHub). Omit it for a local
single-user deck, which manages its own checkout.

> **Audit (trust tiers).** The trust-tier data (`.marathon/audit/latest.json`)
> is *gitignored* — a local compute artifact (Lean elaboration) the thin deck
> can't regenerate, so `--pull-every` does **not** pull it. The deck degrades
> gracefully (empty trust tiers) without one. To show fresh trust tiers on a
> hosted box, have a machine that runs `marathon audit` **publish** its
> `latest.json` (rsync to a path, or an http endpoint) and add
> **`--audit-from <path-or-url>`** — the sync loop then refreshes the snapshot
> each cycle (atomic + JSON-validated + fail-soft). The cards themselves (source
> + issue) stay current via the pull + live GitHub reads regardless.

### 5. Each collaborator's compute

On the collaborator's own machine:

```bash
git clone <repo> && cd <repo>
export ARISTOTLE_API_KEY=<their key>
marathon conductor run --queue-from-github
```

They install `marathon` the same way you did and provide their own `gh auth` +
`ARISTOTLE_API_KEY`.

## Verify the first run

- Open `https://marathon.example.com` in a browser where you are **not** signed
  in as yourself — you should hit the GitHub login, and only a repo-collaborator
  gets through.
- Have a collaborator review + **ship** one issue (verify or reject); confirm on
  GitHub that the resulting comment / label / merge is attributed to **their**
  account.

## Security notes

- **Keep the deck on `127.0.0.1`.** There is intentionally no `--host` flag.
  `oauth2-proxy` + the tunnel are the only exposed surface, and the tunnel opens
  no inbound ports.
- **Everyone admitted can act.** The deck vends a per-session token to the page
  it serves; `oauth2-proxy` restricts *who can load* that page, but it is not a
  per-action permission. Only invite people you would trust to fire verdicts.
- **Don't forget `--no-host-conductor` on a shared deck.** Without it, a
  collaborator's reject feeds *your* conductor, so the refine's PR merge and the
  Aristotle spend are attributed to *your* key. The deck prints a startup warning
  when fronted (a `--trusted-origin` is set) without it.
- **The repo's content is exposed to everyone admitted** — issue bodies, informal
  math, Lean source, and the refine-reviewer prompts all render in the page. Gate
  to repo-access people only (which `--github-repo` does).
- **One conductor per rejection.** Don't also run a host conductor against the
  same repo while collaborators run theirs, or both would drain the same queue.

## How it works under the hood

- **Note embedding (producer).** `marathon review reject` (and the deck's reject,
  which routes through it) mirrors the verbatim rejection note into the GitHub
  reject *comment* inside an HTML-comment marker — invisible in the rendered
  issue, recoverable via the API. That is how a collaborator's conductor obtains
  the note for a GitHub-discovered reject.
- **Discovery (consumer).** `--queue-from-github` lists `review:rejected` issues,
  recovers each note, and seeds the collaborator's local `state.json` — but only
  when GitHub shows a **strictly newer** reject than the one already recorded, so
  an issue the conductor already refined is never re-queued. From there the normal
  dispatch / iteration / collision machinery is reused unchanged. gh discovery is
  throttled (~once/minute) and fail-soft: any gh error falls back to the local
  queue rather than crashing a tick.
