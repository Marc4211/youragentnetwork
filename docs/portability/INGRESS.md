# Ingress - where the chat is reachable

"Where the chat lives" is an ingress choice, set with `INGRESS_PROFILE` in
`infra/rocketchat/.env`. The installer reads it and sets the bind address +
`ROOT_URL` for you - you do not edit those by hand.

For the target audience (a team that already runs OpenClaw and often wants to
keep things local/private), the first two profiles need no domain and no cloud.

## Profiles

### `loopback` (default) - nothing exposed
Rocket.Chat + the glue bind to `127.0.0.1` on the host. Reach them by opening an
SSH tunnel from your own machine:

```
bash scripts/tunnel.sh user@host ~/.ssh/your_key
# then open http://localhost:3000 (chat) and http://localhost:8000/join
```

Best for: a single admin trying it out, or a server you only reach over SSH.

### `lan` - reachable on your network
Binds to `0.0.0.0`; teammates on the same network reach it at the host's LAN IP
(the installer detects it and sets `ROOT_URL=http://<lan-ip>:3000`).

Best for: an always-on box in the office, everyone on the same Wi-Fi/VLAN.
Note: only expose to networks you trust - there is no TLS in this profile.

### `tailscale` - private mesh, nothing public
Reachable only to devices on your Tailscale tailnet, by the host's MagicDNS name
(e.g. `http://teambox.tailnet-name.ts.net:3000`). No public exposure, no domain.

Setup on the host, once:
```
curl -fsSL https://tailscale.com/install.sh | sh
sudo tailscale up --ssh
```
Then set `INGRESS_PROFILE=tailscale` in `.env` and run `bash scripts/install.sh`.
Each teammate joins the same tailnet on their own device to reach it.

Best for: a remote team that wants agents together but nothing leaving their
private network - especially paired with a local model in OpenClaw.

## `public` (a domain + HTTPS) - deferred to v2
A real public URL (the original used Cloudflare Tunnel + Caddy). Not part of the
v1 profiles; the Cloudflare-specific scripts remain in `scripts/` for teams that
want to wire it themselves. See the refactor checklist (Phase 4).
