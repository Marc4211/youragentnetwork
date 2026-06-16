# Rocket.Chat stack

This directory holds the Docker Compose stack that runs Rocket.Chat, its
MongoDB database, and a Caddy reverse proxy that handles HTTPS via Let's
Encrypt. It is the chat surface youragentnetwork users will see.

## What runs here

- **mongodb.** Rocket.Chat's database. One-node replica set (required by
  Rocket.Chat). Not exposed to the host.
- **mongodb-init.** One-shot helper that initializes the replica set on
  first boot. Exits 0 after success.
- **rocketchat.** Rocket.Chat itself. Bound to `127.0.0.1:3000` on the
  host (for SSH-tunnel debugging) and reachable internally as
  `rocketchat:3000` by Caddy.
- **caddy.** Reverse proxy for the corporate VPN. Listens on the EC2's
  port 80 and 443. Forwards traffic to Rocket.Chat and auto-manages a
  Let's Encrypt cert via the DNS-01 challenge against GoDaddy (so it
  works even though the EC2 is not publicly reachable). Custom built
  from `Dockerfile.caddy` to include the GoDaddy DNS plugin.

## Before first deploy: prerequisites

These must be true BEFORE you bring the stack up, or Caddy will fail to
issue an HTTPS cert and you'll see TLS errors in the logs.

1. **DNS A record exists.** `chat.youragent.network` resolves to the
   EC2's public IP `3.22.95.83`. Verify with `dig +short
   chat.youragent.network` from anywhere.
2. **EC2 Security Group allows inbound 80 and 443** from the corporate
   VPN's CIDR range (not from `0.0.0.0/0`). Port 80 is for HTTP-to-HTTPS
   redirection; 443 is the real HTTPS traffic. Cert issuance does NOT
   need public-internet access because we use the DNS-01 challenge.
3. **GoDaddy API key + secret generated** at
   https://developer.godaddy.com/keys (Production environment). Both
   values go into `.env` as `GODADDY_API_TOKEN=KEY:SECRET`.
4. **`.env` file is filled in** with the values from `.env.example`.

## First-time deploy on the EC2

```bash
# Clone the repo if you haven't already
cd ~ && git clone <repo-url> youragentnetwork
cd youragentnetwork/infra/rocketchat

# Create your env file (gitignored) and edit it
cp .env.example .env
# At minimum: set ADMIN_PASS to something strong, and replace the
# GODADDY_API_TOKEN placeholder with your real key:secret pair.

# Bring it up. The --build flag tells docker compose to build our custom
# Caddy image the first time (which compiles in the GoDaddy plugin).
docker compose up -d --build

# Watch the logs while it starts. First boot takes 60-120 seconds for
# Rocket.Chat to initialize, and another ~30 seconds for Caddy to fetch
# the Let's Encrypt cert.
docker compose logs -f

# Once you stop seeing log activity, check status:
docker compose ps
```

## Verifying

There are two paths to reach Rocket.Chat, and both should work once the
stack is up:

**Public path (the real one the test team will use):**
```
https://chat.youragent.network
```
This goes: your browser → EC2 port 443 → Caddy → rocketchat:3000.

**Debug path (only for you, when you suspect Caddy is the problem):**
```bash
# On your laptop:
ssh -L 3000:127.0.0.1:3000 ec2-user@3.22.95.83
# Then open http://localhost:3000 in your browser
```
This skips Caddy entirely and goes straight to Rocket.Chat. Useful for
isolating whether a bug is in Rocket.Chat or in the proxy layer.

## Common issues

- **Caddy fails to get a cert / TLS errors in logs.** Most likely
  causes, in order of frequency: (a) `GODADDY_API_TOKEN` in `.env` is
  missing, malformed (must be `KEY:SECRET` with a colon), or invalid;
  (b) the GoDaddy account does not have API write access for this
  domain; (c) DNS for `$CHAT_DOMAIN` doesn't resolve to this EC2 yet.
  Caddy's error messages name which check failed. Test the API token
  manually with:
  ```bash
  curl -s -H "Authorization: sso-key $GODADDY_API_TOKEN" \
    https://api.godaddy.com/v1/domains/youragent.network
  ```
  A 200 response with JSON about the domain means the token works.

- **Rocket.Chat is unhealthy or restart-looping.** Usually means Mongo
  replica set didn't initialize. Run `docker compose logs mongodb-init`.
  If you wiped/restarted Mongo, re-run init: `docker compose up
  mongodb-init`.

- **First boot is slow.** Rocket.Chat takes 60-120 seconds to start the
  first time as it migrates the database. Subsequent restarts are
  faster.

- **I want to wipe everything and start over.**
  ```bash
  docker compose down
  rm -rf ./data
  docker compose up -d
  ```
  Destructive. Only during initial setup. This also wipes Caddy's stored
  cert, so on next boot Caddy will re-issue from Let's Encrypt (which
  has rate limits, so do not do this dozens of times in a row).

## Data

Everything stateful lives under `./data/` (gitignored):
- `./data/mongo`: Mongo's database files
- `./data/uploads`: User-uploaded files in Rocket.Chat
- `./data/caddy`: Caddy's issued TLS certs and ACME state
- `./data/caddy-config`: Caddy's config cache

Back this up before destroying the stack if you care about its contents.
