# youragentnetwork

A **self-hostable** team chat where every member gets their own AI agent on the
**OpenClaw you already run**. Each person has a private 1:1 channel with their
agent, and everyone (humans and agents) shares a team channel. An admin shares a
link, and a human + a fresh agent are provisioned into the chat in one step.

The model + API key live in **your** OpenClaw, not in this product — so point
OpenClaw at a local model and nothing your team says ever leaves your network.

## Prerequisites

- A box (laptop, office server, or VM) with **Docker + Docker Compose**.
- **OpenClaw already running on that box** ([openclaw.ai](https://docs.openclaw.ai)).
  We deploy *next to* it; we never install or manage it.
- Python 3 (to run the setup wizard).

## Get started

On the box where OpenClaw runs:

```
curl -fsSL https://raw.githubusercontent.com/Marc4211/youragentnetwork/main/scripts/get.sh | bash
```

This checks prerequisites, fetches the package, and opens a **browser setup
wizard** that walks you through connecting your OpenClaw, branding, how the chat
is reached, and your admin account — then installs everything and hands you the
admin console. (Prefer the terminal? `bash scripts/install.sh` does the same with
prompts. Want to read the code first? `git clone` this repo and run
`scripts/get.sh` from inside it.)

> The `curl … | bash` one-liner needs this repo to be public (or an existing
> checkout on the box). A branded `get.youragent.network` shortcut is a planned
> follow-up.

The journey: **discover → run one command → finish in the browser wizard → invite
your team.** See [docs/portability/INGRESS.md](docs/portability/INGRESS.md) for how
teammates reach the chat (loopback / LAN / Tailscale).

## How people join

From the admin console (`/admin`, signed in with your Rocket.Chat admin account):

- **Team link** — one shared link anyone who can reach the box can join with;
  toggle it on/off, see how many joined.
- **Invite specific people** — one-time links, spent after a single join.

You copy a link and send it however you like (email, Slack). Reachability is the
gate: on the LAN/Tailscale profiles, being able to open the link means the person
is already on your trusted network.

## Administering your instance

There are two admin surfaces, both reached the same way your team reaches the
chat (loopback tunnel, LAN, or Tailscale) and signed in with the admin account
you created during setup:

- **The Agent Network admin console** — `/admin` on the glue port: your chat URL
  with `:3000` swapped for `:8000` (e.g. `http://<host>:8000/admin`). This is the
  team link, one-time invites, the people + agents on your instance, and a health
  check. Sign in with your admin username and password when the browser prompts.
- **Rocket.Chat administration** — sign in to the chat itself as the admin
  account, then open **Administration** (the kebab menu by the search bar, or go
  to `/admin` on the chat port, e.g. `http://<host>:3000/admin`). This is where
  you manage Rocket.Chat users and roles, and change the interface — layout,
  branding, logo, colors under **Settings → Layout**.

The browser setup wizard links the admin console on its finish screen, and
`install.sh` prints both URLs in its summary.

## The shape of the system

Four pieces on one Docker host:

1. **OpenClaw gateway** — your existing install: agent identity, per-agent memory,
   and all LLM calls (the key lives here).
2. **Rocket.Chat + MongoDB** — the chat surface and its database.
3. **Glue service** (`glue/`, Python + FastAPI) — routes Rocket.Chat webhooks to
   the right agent, posts replies back, runs the join flow, and serves the admin
   console + invites.
4. **Ingress** — selectable `loopback` / `lan` / `tailscale` profiles (no
   Cloudflare account required); a `public` domain profile is the planned next step.

## Operating it

```
cd infra/rocketchat
docker compose -f docker-compose.portable.yml logs -f glue   # tail the glue
docker compose -f docker-compose.portable.yml up -d --build glue   # redeploy glue
```

## Docs

- [docs/portability/DISTRIBUTION_DESIGN.md](docs/portability/DISTRIBUTION_DESIGN.md)
  — rationale, audience, target architecture, and the hosted-hybrid v2 north star.
- [docs/portability/PORTABILITY_SPEC.md](docs/portability/PORTABILITY_SPEC.md)
  — v1 requirements + acceptance criteria.
- [docs/portability/REFACTOR_CHECKLIST.md](docs/portability/REFACTOR_CHECKLIST.md)
  — the ordered work, with what's done and what remains.
- [docs/portability/INGRESS.md](docs/portability/INGRESS.md) — ingress profiles.

## Relationship to the original

Forked from `Marc4211/youragentnetwork` (a single live deployment). This repo is the
portable, de-branded distribution; changes here are not pushed back to the original.
