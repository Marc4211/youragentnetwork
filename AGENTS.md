# AGENTS.md: deploy Agent Network with an agent

This repo is built so an AI agent can deploy it. If you're an AI tool like Claude
working with a person, or an OpenClaw agent with shell access on the host where
OpenClaw runs, this file is your run book.

The install is designed for you, not just for a human clicking a wizard.
Everything the installer needs comes from **one config file**, and the installer
**never blocks on a prompt**. If a required value is missing it exits and names
the exact key to fix. So the loop is simple: fill the file, run one script, and
either it finishes or it tells you precisely what to correct.

## What you're standing up

Our stack (MongoDB + Rocket.Chat + a small "glue" service) **next to** the
OpenClaw that already runs on this host. You never install or manage OpenClaw
itself. You wire into it. The model and the API key stay in the operator's
OpenClaw, never in this stack.

## Preconditions (check these first)

- **Docker + Docker Compose v2**: `docker compose version` must succeed.
- **python3**: `command -v python3`.
- **git**: only if you still need to clone.
- **OpenClaw already running on this host**, and you can reach its gateway,
  read its data directory, and read its gateway token.

If any precondition fails, stop and report it. Don't try to install OpenClaw.

## The 7 required values

The installer reads `infra/rocketchat/.env`. Seven values are required. It will
stop and name any that's missing, so you never have to guess.

| Key | What to set | How to find it |
| --- | --- | --- |
| `INSTANCE_NAME` | Display name shown to the team | Any string, e.g. `Acme Agents` |
| `ADMIN_EMAIL` | Admin account email | The operator's email |
| `ADMIN_PASS` | Admin password, **at least 14 characters** | Generate a strong one |
| `OPENCLAW_GATEWAY_URL` | Gateway URL the glue reaches | Default `http://host.docker.internal:18789` works when OpenClaw publishes its gateway port on the host |
| `OPENCLAW_GATEWAY_TOKEN` | OpenClaw gateway token | From the operator's OpenClaw `.env` (often `~/.openclaw/.env`) |
| `OPENCLAW_DATA_DIR` | Host path of OpenClaw's data dir (the one holding `openclaw.json`) | e.g. `~/.openclaw` or `/root/.openclaw` |
| `OPENCLAW_CONTAINER_NAME` | OpenClaw's Docker container name | `docker ps` to find it. **If OpenClaw runs natively (not Docker)** and you're on the default reload strategy, this value isn't used but still must be non-empty, so set it to `none` |

> **Gotcha to know:** `OPENCLAW_CONTAINER_NAME` is required by the installer, but
> the example file leaves it commented out. Add it yourself, or the installer will
> stop and ask for it.

Optional, with sensible defaults:

- `INGRESS_PROFILE`: `loopback` (default, nothing exposed, reach via SSH tunnel) `|` `lan` `|` `tailscale`. See [docs/portability/INGRESS.md](docs/portability/INGRESS.md).
- `OPENCLAW_RELOAD_STRATEGY`: `hotreload` (default) `|` `docker-restart` `|` `none`.
- `ADMIN_USERNAME`: defaults to `admin`.

The installer generates everything else for you (admin and bot tokens, bind
address, public URLs, the webhook token). **Don't set those.**

## Steps

1. **Get the code** (skip if you're already inside a checkout):
   ```
   git clone --depth 1 https://github.com/Marc4211/youragentnetwork.git
   cd youragentnetwork
   ```
2. **Create the config from the template:**
   ```
   cp infra/rocketchat/.env.portable.example infra/rocketchat/.env
   ```
3. **Set the 7 required values** in `infra/rocketchat/.env`. This file is
   gitignored. **Never commit it:** it holds the OpenClaw token.
4. **Run the installer.** It's non-interactive and safe to re-run:
   ```
   bash scripts/install.sh
   ```
5. **If it exits with** `ERROR: <KEY> is not set in .env and no terminal to prompt`,
   set that one key and re-run step 4. That message is the contract: fix the named
   key, run again. Repeat until it completes.
6. **On success** it prints the chat URL, the join page, and the admin console.
   Confirm the glue is healthy:
   ```
   curl -s http://localhost:8000/health
   ```
   Expect `{"status":"ok"}`.

## Invite the team

Send teammates the join URL the installer printed (the join page, on the glue
port). Each person names their agent and describes its personality in plain
words, and is provisioned in one step: an account, their own agent, channel
membership, and a welcome message.

## Good to know

- **Idempotent:** re-running `bash scripts/install.sh` is safe. Use it to fix a
  half-finished install or to apply a changed value.
- **Privacy:** the model and key live in the operator's OpenClaw, never in this
  stack. Keep `OPENCLAW_GATEWAY_TOKEN` in `.env` only, and don't echo it back to
  the operator in plain text.
- **Ingress:** `loopback` exposes nothing (reach it over an SSH tunnel).
  `tailscale` puts it on a private mesh. `lan` binds to the local network. On a
  host with a public IP, `lan` will warn you loudly. See
  [docs/portability/INGRESS.md](docs/portability/INGRESS.md).
- **Prefer a human wizard instead?** `bash scripts/get.sh` (or the one-liner in
  the [README](README.md)) launches a browser setup wizard that walks a person
  through the same steps.
