#!/usr/bin/env bash
#
# scripts/install.sh - one-command installer for Agent Network (portable).
#
# Stands up OUR stack (Mongo + Rocket.Chat + glue) NEXT TO an OpenClaw the team
# already runs on this host, and wires everything together. We never install or
# manage OpenClaw itself.
#
# What it does, idempotently (safe to re-run):
#   1. Preflight: docker + compose present; the existing OpenClaw is reachable.
#   2. Ensure infra/rocketchat/.env exists; prompt for any missing required values.
#   3. Bring up Mongo + Rocket.Chat; wait for the API.
#   4. Log in as the RC admin; apply the settings provisioning needs
#      (disable email-2FA, disable self-registration) using RC's password-2FA.
#   5. Ensure the glue bot user + a fresh token.
#   6. Ensure the outgoing webhook -> glue.
#   7. Write ADMIN_*/BOT_* into .env and bring up the glue.
#   8. Print how to invite teammates.
#
# Usage (from the repo root or anywhere):
#   bash scripts/install.sh

set -uo pipefail

# --- paths ---
REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
RC_DIR="$REPO_ROOT/infra/rocketchat"
ENV_FILE="$RC_DIR/.env"
EXAMPLE_FILE="$RC_DIR/.env.portable.example"
COMPOSE_FILE="docker-compose.portable.yml"
RC_LOCAL="http://localhost:3000"   # RC's host-published port (loopback)
GLUE_LOCAL="http://localhost:8000"

say()  { printf '\n\033[1m==> %s\033[0m\n' "$*"; }
info() { printf '    %s\n' "$*"; }
die()  { printf '\n\033[31mERROR: %s\033[0m\n' "$*" >&2; exit 1; }

# --- 1. preflight ------------------------------------------------------------
say "Step 1/7: preflight"
command -v docker >/dev/null 2>&1 || die "docker is not installed."
docker compose version >/dev/null 2>&1 || die "docker compose v2 is not available."
info "docker + compose OK"

# --- 2. .env: ensure required values ----------------------------------------
say "Step 2/7: configuration (infra/rocketchat/.env)"
if [ ! -f "$ENV_FILE" ]; then
  cp "$EXAMPLE_FILE" "$ENV_FILE"
  info "created .env from the example template"
fi

# Read a value from .env (empty if missing).
get_env() { grep -E "^$1=" "$ENV_FILE" | head -1 | cut -d= -f2-; }
# Set/replace a key in .env.
set_env() {
  local key="$1" val="$2"
  sed -i.bak "/^$key=/d" "$ENV_FILE"; rm -f "$ENV_FILE.bak"
  printf '%s=%s\n' "$key" "$val" >> "$ENV_FILE"
}
# Prompt for a required value if missing (interactive only).
ensure_env() {
  local key="$1" prompt="$2" secret="${3:-}" cur
  cur="$(get_env "$key")"
  if [ -n "$cur" ]; then return 0; fi
  if [ ! -t 0 ]; then die "$key is not set in .env and no terminal to prompt. Set it and re-run."; fi
  local val
  if [ "$secret" = "secret" ]; then read -r -s -p "    $prompt: " val; echo; else read -r -p "    $prompt: " val; fi
  [ -n "$val" ] || die "$key cannot be empty."
  set_env "$key" "$val"
}

ensure_env INSTANCE_NAME            "Instance name (shown to your team, e.g. 'Acme Agents')"
ensure_env ADMIN_EMAIL              "Admin email"
ensure_env ADMIN_PASS               "Admin password (min 14 chars)" secret
ensure_env OPENCLAW_GATEWAY_URL     "Existing OpenClaw gateway URL (e.g. http://openclaw-gateway:18789)"
ensure_env OPENCLAW_GATEWAY_TOKEN   "OpenClaw gateway token" secret
ensure_env OPENCLAW_DATA_DIR        "OpenClaw data dir on this host (e.g. /root/.openclaw)"
ensure_env OPENCLAW_CONTAINER_NAME  "OpenClaw container name (e.g. openclaw-openclaw-gateway-1)"
# Sensible defaults if absent.
[ -n "$(get_env ADMIN_USERNAME)" ] || set_env ADMIN_USERNAME admin
[ -n "$(get_env BOT_USERNAME)" ]   || set_env BOT_USERNAME lois
info ".env ready"

# Read the values the API calls below need. We do NOT `source` .env because
# values may contain spaces (e.g. INSTANCE_NAME="Acme Agents"), which would
# break shell sourcing. docker compose reads .env directly and handles spaces.
ADMIN_USERNAME="$(get_env ADMIN_USERNAME)"
ADMIN_PASS="$(get_env ADMIN_PASS)"
BOT_USERNAME="$(get_env BOT_USERNAME)"

# --- 2b. preflight: the EXISTING, co-located OpenClaw ------------------------
# We do not run OpenClaw; verify the team's is actually here and wired so we
# fail early with a clear message instead of midway through provisioning.
say "Verifying the existing OpenClaw"
OC_URL="$(get_env OPENCLAW_GATEWAY_URL)"
OC_DATA="$(get_env OPENCLAW_DATA_DIR)"
OC_CONTAINER="$(get_env OPENCLAW_CONTAINER_NAME)"
RELOAD="$(get_env OPENCLAW_RELOAD_STRATEGY)"; RELOAD="${RELOAD:-hotreload}"

# (a) data dir must hold openclaw.json - provisioning writes workspaces + edits it.
[ -f "$OC_DATA/openclaw.json" ] || die "OPENCLAW_DATA_DIR ($OC_DATA) has no openclaw.json. Point it at the data dir of the OpenClaw you already run (e.g. ~/.openclaw)."
info "OpenClaw data dir OK ($OC_DATA)"

# (b) gateway must be reachable on its port. The glue reaches it via
# host.docker.internal (which maps to this host), so its port must be reachable
# here - true for a Docker OpenClaw that publishes its port AND a native install.
OC_PORT="$(printf '%s' "$OC_URL" | sed -nE 's#.*:([0-9]+).*#\1#p')"; OC_PORT="${OC_PORT:-18789}"
code="$(curl -s -o /dev/null -w '%{http_code}' "http://localhost:$OC_PORT/healthz" 2>/dev/null || echo 000)"
[ "$code" = 200 ] && info "OpenClaw gateway reachable (localhost:$OC_PORT)" \
  || info "warn: gateway healthz on localhost:$OC_PORT returned $code. Make sure OpenClaw is running and its gateway port is reachable on this host."

# (c) docker-restart reload only: the OpenClaw container must be running.
# Default (hotreload) needs no container - OpenClaw hot-reloads openclaw.json.
if [ "$RELOAD" = docker-restart ]; then
  docker ps --format '{{.Names}}' | grep -qx "$OC_CONTAINER" \
    && info "OpenClaw container running ($OC_CONTAINER)" \
    || info "warn: OPENCLAW_RELOAD_STRATEGY=docker-restart but container '$OC_CONTAINER' isn't running."
fi

# --- 2c. ingress profile: where the chat is reachable -----------------------
# loopback (default): 127.0.0.1, reach via SSH tunnel.
# lan:       0.0.0.0, reach at the host's LAN IP.
# tailscale: 0.0.0.0, reach at the host's Tailscale MagicDNS name (private mesh).
say "Ingress profile"
INGRESS="$(get_env INGRESS_PROFILE)"; INGRESS="${INGRESS:-loopback}"
case "$INGRESS" in
  loopback) BIND=127.0.0.1; HOST_ADDR=localhost ;;
  lan)      BIND=0.0.0.0;   HOST_ADDR="$(hostname -I 2>/dev/null | awk '{print $1}')" ;;
  tailscale)
    # Bind to the tailscale interface IP only, so the chat is reachable on the
    # tailnet but NOT on a public interface (important on cloud hosts).
    BIND="$(tailscale ip -4 2>/dev/null | head -1)"
    [ -n "$BIND" ] || die "INGRESS_PROFILE=tailscale but Tailscale is not up. Run 'tailscale up' first (see docs/portability/INGRESS.md)."
    HOST_ADDR="$(tailscale status --json 2>/dev/null | python3 -c 'import sys,json;print(json.load(sys.stdin).get("Self",{}).get("DNSName","").rstrip("."))' 2>/dev/null)"
    [ -n "$HOST_ADDR" ] || HOST_ADDR="$BIND"
    ;;
  *) die "unknown INGRESS_PROFILE '$INGRESS' (use: loopback | lan | tailscale)." ;;
esac
[ -n "$HOST_ADDR" ] || die "could not determine host address for INGRESS_PROFILE=$INGRESS."
set_env RC_BIND_ADDR "$BIND"
set_env GLUE_BIND_ADDR "$BIND"
set_env ROOT_URL "http://$HOST_ADDR:3000"
set_env ROCKETCHAT_PUBLIC_URL "http://$HOST_ADDR:3000"
info "profile=$INGRESS  bind=$BIND  url=http://$HOST_ADDR:3000"
# Talk to RC/glue at the address they actually bind to during setup. 0.0.0.0 and
# 127.0.0.1 are both reachable via localhost; a specific IP (tailscale) is not.
case "$BIND" in 127.0.0.1|0.0.0.0) SETUP_HOST=localhost ;; *) SETUP_HOST="$BIND" ;; esac
RC_LOCAL="http://$SETUP_HOST:3000"
GLUE_LOCAL="http://$SETUP_HOST:8000"

# --- 3. bring up Mongo + Rocket.Chat ----------------------------------------
say "Step 3/7: starting Mongo + Rocket.Chat"
( cd "$RC_DIR" && docker compose -f "$COMPOSE_FILE" up -d mongodb mongodb-init rocketchat ) || die "compose up failed"
printf '    waiting for Rocket.Chat'
for i in $(seq 1 90); do
  if curl -s -m 5 "$RC_LOCAL/api/info" 2>/dev/null | grep -q '"success":true'; then ok=1; break; fi
  printf '.'; sleep 5
done
echo
[ "${ok:-}" = 1 ] || die "Rocket.Chat did not become ready in time."
info "Rocket.Chat is up"

# --- 4. admin login + required settings -------------------------------------
say "Step 4/7: admin login + Rocket.Chat settings"
# Retry: on a fresh install the admin user is created during first-boot setup,
# which can lag slightly behind the API coming up.
ADMIN_ID=""; ADMIN_TOK=""
for i in $(seq 1 18); do
  AL="$(curl -s -X POST -H 'Content-Type: application/json' "$RC_LOCAL/api/v1/login" \
        -d "{\"user\":\"$ADMIN_USERNAME\",\"password\":\"$ADMIN_PASS\"}")"
  ADMIN_ID="$(printf '%s' "$AL" | python3 -c 'import sys,json;print(json.load(sys.stdin).get("data",{}).get("userId",""))' 2>/dev/null)"
  ADMIN_TOK="$(printf '%s' "$AL" | python3 -c 'import sys,json;print(json.load(sys.stdin).get("data",{}).get("authToken",""))' 2>/dev/null)"
  [ -n "$ADMIN_ID" ] && [ -n "$ADMIN_TOK" ] && break
  sleep 5
done
[ -n "$ADMIN_ID" ] && [ -n "$ADMIN_TOK" ] || die "admin login failed (check ADMIN_USERNAME/ADMIN_PASS in .env)."
TFA="$(printf '%s' "$ADMIN_PASS" | sha256sum | cut -d' ' -f1)"   # RC password-method 2FA code

# Authenticated admin curl, with the password-2FA header sensitive actions need.
rc_admin() { curl -s -X POST -H "X-Auth-Token:$ADMIN_TOK" -H "X-User-Id:$ADMIN_ID" \
  -H "x-2fa-code:$TFA" -H "x-2fa-method:password" -H 'Content-Type: application/json' "$@"; }
rc_set() { # rc_set <SettingId> <jsonValue>
  local r; r="$(rc_admin "$RC_LOCAL/api/v1/settings/$1" -d "{\"value\":$2}")"
  printf '%s' "$r" | grep -q '"success":true' && info "set $1 = $2" || info "warn: could not set $1 ($r)"
}
rc_set Accounts_TwoFactorAuthentication_By_Email_Enabled false   # else verified-user login = totp-required 401
rc_set Accounts_RegistrationForm '"Disabled"'                    # only the invite flow creates accounts

# --- 5. ensure bot user + fresh token ---------------------------------------
say "Step 5/7: glue bot user ($BOT_USERNAME)"
# Policy-compliant: users.update (unlike admin users.create) enforces RC's
# password policy, so include upper+lower+digit+special, plenty long.
BOT_PASS="Aa1!$(openssl rand -hex 20)"
EXISTS="$(curl -s -H "X-Auth-Token:$ADMIN_TOK" -H "X-User-Id:$ADMIN_ID" \
  "$RC_LOCAL/api/v1/users.info?username=$BOT_USERNAME" | grep -c '"success":true')"
if [ "$EXISTS" = 1 ]; then
  BID="$(curl -s -H "X-Auth-Token:$ADMIN_TOK" -H "X-User-Id:$ADMIN_ID" "$RC_LOCAL/api/v1/users.info?username=$BOT_USERNAME" | python3 -c 'import sys,json;print(json.load(sys.stdin)["user"]["_id"])')"
  UPD="$(rc_admin "$RC_LOCAL/api/v1/users.update" -d "{\"userId\":\"$BID\",\"data\":{\"password\":\"$BOT_PASS\"}}")"
  printf '%s' "$UPD" | grep -q '"success":true' || die "bot password reset failed: $UPD"
  info "bot exists; reset its password"
else
  rc_admin "$RC_LOCAL/api/v1/users.create" -d "{\"name\":\"${BOT_USERNAME^}\",\"username\":\"$BOT_USERNAME\",\"email\":\"$BOT_USERNAME@agentnetwork.local\",\"password\":\"$BOT_PASS\"}" >/dev/null
  info "bot created"
fi
BL="$(curl -s -X POST -H 'Content-Type: application/json' "$RC_LOCAL/api/v1/login" -d "{\"user\":\"$BOT_USERNAME\",\"password\":\"$BOT_PASS\"}")"
BOT_ID="$(printf '%s' "$BL" | python3 -c 'import sys,json;print(json.load(sys.stdin)["data"]["userId"])')"
BOT_TOK="$(printf '%s' "$BL" | python3 -c 'import sys,json;print(json.load(sys.stdin)["data"]["authToken"])')"
[ -n "$BOT_ID" ] && [ -n "$BOT_TOK" ] || die "bot login failed."
info "bot token acquired"

# --- 6. ensure outgoing webhook -> glue -------------------------------------
say "Step 6/7: outgoing webhook -> glue"
HAVE_HOOK="$(curl -s -H "X-Auth-Token:$ADMIN_TOK" -H "X-User-Id:$ADMIN_ID" "$RC_LOCAL/api/v1/integrations.list" \
  | python3 -c 'import sys,json;print(sum(1 for i in json.load(sys.stdin).get("integrations",[]) if i.get("name")=="glue"))' 2>/dev/null || echo 0)"
if [ "$HAVE_HOOK" = 0 ]; then
  rc_admin "$RC_LOCAL/api/v1/integrations.create" -d "{\"type\":\"webhook-outgoing\",\"name\":\"glue\",\"enabled\":true,\"username\":\"$ADMIN_USERNAME\",\"event\":\"sendMessage\",\"urls\":[\"http://glue:8000/webhook\"],\"channel\":\"all_public_channels,all_direct_messages\",\"scriptEnabled\":false}" \
    | grep -q '"success":true' && info "webhook created" || die "webhook creation failed"
else
  info "webhook already present"
fi

# --- 7. write creds to .env + bring up the glue -----------------------------
say "Step 7/7: wiring the glue"
set_env ADMIN_USER_ID "$ADMIN_ID"
set_env ADMIN_PAT     "$ADMIN_TOK"
set_env BOT_USER_ID   "$BOT_ID"
set_env BOT_PAT       "$BOT_TOK"
( cd "$RC_DIR" && docker compose -f "$COMPOSE_FILE" up -d --build glue ) || die "glue up failed"
sleep 3
curl -s -m 5 "$GLUE_LOCAL/health" | grep -q '"status":"ok"' && info "glue healthy" || info "warn: glue /health not OK yet (check: docker compose logs -f glue)"

cat <<DONE

============================================================
 Agent Network is installed.
============================================================
 Instance : $(get_env INSTANCE_NAME)
 Chat     : $(get_env ROOT_URL)
 Join page: $(get_env ROOT_URL | sed 's#:3000#:8000#')/join   (or http://localhost:8000/join via tunnel)

 Invite a teammate: send them the join URL above. They pick an
 agent name + personality and are provisioned instantly.

 Logs: cd infra/rocketchat && docker compose -f $COMPOSE_FILE logs -f glue
============================================================
DONE
