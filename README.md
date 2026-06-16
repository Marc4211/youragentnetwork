# Your Agent Network

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

> Want to read the code first? `git clone` this repo and run `scripts/get.sh`
> from inside it. New here? [get.youragent.network](https://get.youragent.network)
> explains the product.

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

- [docs/portability/INGRESS.md](docs/portability/INGRESS.md) — how teammates reach
  the chat: the `loopback` / `lan` / `tailscale` ingress profiles.
