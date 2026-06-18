"""
Agent Network glue service.

Two surfaces:

  1. The webhook handler at POST /webhook. Receives Rocket.Chat outgoing
     webhooks, decides which agent (if any) should respond per the
     channel-aware routing rules, calls OpenClaw, and posts the reply
     back into the channel as the agent's bot user.

  2. The join flow at GET /join (form) + POST /join (submit handler).
     v0.12 (this version) implements stages 2 through 5 of the join flow:
       - Stage 2: create the human's Rocket.Chat user account
       - Stage 3a: create the agent's Rocket.Chat bot user
       - Stage 3b: write OpenClaw workspace files for ONE agent
                   (<user>-<agent>), with one shared memory
       - Stage 3c: register the OpenClaw agent in openclaw.json
       - Stage 3d: restart the OpenClaw container via Docker socket
       - Stage 3e: persist agent metadata to a SQLite database
       - Stage 4: add the human and agent to the shared team channel
                  (defaults to #general, RC's built-in default channel)
       - Stage 5: (background) create the private 1:1 DM and post a
                  welcome message from the agent into it
     Only stage 6 (explicit login delivery) is left, and it is largely
     satisfied already by the temp password shown on the success page.

     MEMORY MODEL (changed in v0.12): one agent per user with ONE shared
     memory across the private DM and the team channel. The conversation
     session is keyed to the agent (user=f"agent-{id}"), not the room, so
     the agent remembers private-channel context when @-mentioned in the
     team channel. The agent's identity file tells it to treat what it
     learns as shareable by default, withholding only what the operator
     explicitly marks private. This replaces the earlier two-agent hard
     memory wall.

Routing rules for the webhook handler (per PROJECT_NOTES.md):

  - direct message (channel type 'd'): always respond
  - public channel ('c') or private group ('p'): only respond when
    @<BOT_USERNAME> appears in the message text

Loop prevention:

  - skip messages flagged bot=true
  - skip messages from our own bot user id
"""
import asyncio
import base64
import hashlib
import hmac
import json
import logging
import os
import pathlib
import re
import secrets
import shutil
import sqlite3
import string
import time
from typing import Annotated

import httpx
from fastapi import BackgroundTasks, FastAPI, Form, Request
from fastapi.responses import FileResponse, HTMLResponse, RedirectResponse
from openai import AsyncOpenAI

from a2a_client import ask_a2a_agent


# --- logging setup ---
logging.basicConfig(
    level=os.environ.get("LOG_LEVEL", "INFO").upper(),
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
log = logging.getLogger("glue")


# --- configuration from env vars ---
OPENCLAW_URL = os.environ["OPENCLAW_GATEWAY_URL"]
OPENCLAW_TOKEN = os.environ["OPENCLAW_GATEWAY_TOKEN"]
AGENT_MODEL_ID = os.environ.get("AGENT_MODEL_ID", "openclaw/default")

# Shared secret for the OpenClaw cron webhook. OpenClaw's cron service POSTs a
# fired job here as `Authorization: Bearer <token>`; this MUST match the value
# in openclaw.json `cron.webhookToken`. Set on the box only (see
# scripts/setup-cron.sh). Empty => the /cron endpoint refuses every request.
CRON_WEBHOOK_TOKEN = os.environ.get("CRON_WEBHOOK_TOKEN", "")

ROCKETCHAT_URL = os.environ.get("ROCKETCHAT_URL", "http://rocketchat:3000")
ROCKETCHAT_PUBLIC_URL = os.environ.get(
    "ROCKETCHAT_PUBLIC_URL", "http://localhost:3000"
)

# Branding for the join page and agent identity. Configurable per deployment;
# neutral defaults, no vendor branding. INSTANCE_NAME is the product/team name
# shown to users; BRAND_LOGO_URL is an optional logo (empty => no logo shown).
INSTANCE_NAME = os.environ.get("INSTANCE_NAME", "Agent Network")
BRAND_LOGO_URL = os.environ.get("BRAND_LOGO_URL", "")

# Invites: when true, the join page requires a valid, unspent invite token.
# Defense in depth alongside disabling Rocket.Chat self-registration.
INVITES_REQUIRED = os.environ.get("INVITES_REQUIRED", "true").lower() != "false"
# Public base URL of the join page (the glue, port 8000). Defaults to the chat
# public URL with the port swapped, which matches our compose port mapping.
JOIN_PUBLIC_URL = os.environ.get(
    "JOIN_PUBLIC_URL", ROCKETCHAT_PUBLIC_URL.replace(":3000", ":8000")
)
# Which ingress profile is active, so the admin console can show the right
# "how teammates reach this" instructions on an invite.
INGRESS_PROFILE = os.environ.get("INGRESS_PROFILE", "loopback")

BOT_USER_ID = os.environ["BOT_USER_ID"]
BOT_PAT = os.environ["BOT_PAT"]
BOT_USERNAME = os.environ.get("BOT_USERNAME", "lois")

ADMIN_USER_ID = os.environ.get("ADMIN_USER_ID", "")
ADMIN_PAT = os.environ.get("ADMIN_PAT", "")
# The admin login name (defaults to "admin"). Shown on the admin console so the
# operator knows which account to use for Rocket.Chat administration too.
ADMIN_USERNAME = os.environ.get("ADMIN_USERNAME", "admin")

# The shared team channel that every human and every agent belongs to.
# Defaults to "general", Rocket.Chat's built-in default channel, so we
# reuse it instead of creating a second catch-all room. New users
# auto-join #general anyway; stage 4's invite just makes membership
# explicit (and is idempotent). Override to point at a different
# channel, which stage 4 will create on first join if it does not exist.
TEAM_CHANNEL_NAME = os.environ.get("TEAM_CHANNEL_NAME", "general")

# The public channel where humans CONTRIBUTE files to the team-wide shared
# knowledge folder. A file dropped here is saved into shared/uploads/ (see
# ingest_shared_attachments). This channel never triggers an agent reply; it
# is an inbox, not a conversation. The outgoing webhook already fires on all
# public channels, so no integration change is needed to add it.
SHARED_KNOWLEDGE_CHANNEL = os.environ.get(
    "SHARED_KNOWLEDGE_CHANNEL", "shared-knowledge"
)

# Topic shown on the #shared-knowledge channel so members know it is a file
# inbox, not a chat. Set when the glue first creates the channel.
SHARED_KNOWLEDGE_CHANNEL_TOPIC = os.environ.get(
    "SHARED_KNOWLEDGE_CHANNEL_TOPIC",
    "Drop a file here to add it to the team-wide shared knowledge folder. "
    "All agents can then read it.",
)

# OpenClaw provisioning paths and container info.
# OPENCLAW_DATA_HOST_PATH is where the host's ~/.openclaw is mounted
# inside the glue container; we read/write workspace files and
# openclaw.json there.
# OPENCLAW_DATA_OPENCLAW_PATH is the path the SAME directory has inside
# the OpenClaw container; this is what we write into openclaw.json so
# OpenClaw can find the workspaces.
# OPENCLAW_CONTAINER_NAME is what we pass to the Docker API when
# triggering a restart.
OPENCLAW_DATA_HOST_PATH = pathlib.Path(
    os.environ.get("OPENCLAW_DATA_HOST_PATH", "/openclaw_data")
)
OPENCLAW_DATA_OPENCLAW_PATH = os.environ.get(
    "OPENCLAW_DATA_OPENCLAW_PATH", "/home/node/.openclaw"
)
OPENCLAW_CONTAINER_NAME = os.environ.get(
    "OPENCLAW_CONTAINER_NAME", "openclaw-openclaw-gateway-1"
)
# How to make OpenClaw apply a newly-registered agent. OpenClaw watches
# openclaw.json and hot-reloads agents.list on its own (verified), so the
# default needs no process control and works for native/local installs too.
# "docker-restart" restarts the container (fallback for setups where the
# file-watch reload doesn't reach the gateway); "none" skips entirely.
OPENCLAW_RELOAD_STRATEGY = os.environ.get("OPENCLAW_RELOAD_STRATEGY", "hotreload")
OPENCLAW_CONFIG_FILE = OPENCLAW_DATA_HOST_PATH / "openclaw.json"
OPENCLAW_WORKSPACES_DIR = OPENCLAW_DATA_HOST_PATH / "workspaces"
# The team-wide shared knowledge folder, as the glue container sees it
# (host ~/.openclaw/shared). Agents see the SAME dir at
# OPENCLAW_DATA_OPENCLAW_PATH/shared (= /home/node/.openclaw/shared).
# shared/uploads/ is where #shared-knowledge contributions land.
SHARED_DIR = OPENCLAW_DATA_HOST_PATH / "shared"
DOCKER_SOCKET = pathlib.Path(os.environ.get("DOCKER_SOCKET", "/var/run/docker.sock"))

# SQLite metadata store: tracks the human-to-agent mapping.
DATA_DIR = pathlib.Path(os.environ.get("DATA_DIR", "/data"))
AGENTS_DB_FILE = DATA_DIR / "agents.sqlite"

# Scheduled reminders, pull model. OpenClaw's cron writes one run record per
# fired job to ~/.openclaw/cron/runs/<jobId>.jsonl (one JSON line per run).
# That directory is the SAME mount we already use for shared/uploads, so the
# glue can read it directly. We poll it and deliver each finished run's text
# into the owning agent's DM. This sidesteps OpenClaw's outbound webhook, which
# its SSRF guard blocks for internal hosts like glue:8000 (it resolves to a
# private Docker IP). See the "Scheduled reminders" block further down.
CRON_RUNS_DIR = OPENCLAW_DATA_HOST_PATH / "cron" / "runs"
# Persisted set of run keys we have already delivered, so a glue restart does
# not re-post old reminders. Lives in the same /data volume as the agents DB.
CRON_DELIVERED_STATE = DATA_DIR / "cron_delivered.json"
# How often to scan for newly fired jobs. Reminders are time-sensitive ("in 2
# minutes"), so keep this short; it bounds the worst-case delivery lag.
CRON_POLL_SECONDS = int(os.environ.get("CRON_POLL_SECONDS", "10"))


# --- clients ---
openclaw = AsyncOpenAI(
    base_url=f"{OPENCLAW_URL}/v1",
    api_key=OPENCLAW_TOKEN,
)

# Bot-authenticated Rocket.Chat client (used by the webhook handler).
rocketchat_http = httpx.AsyncClient(
    base_url=ROCKETCHAT_URL,
    headers={
        "X-Auth-Token": BOT_PAT,
        "X-User-Id": BOT_USER_ID,
        "Content-Type": "application/json",
    },
    timeout=30.0,
)

# Admin-authenticated Rocket.Chat client, lazily created.
_rocketchat_admin_http: httpx.AsyncClient | None = None


def get_admin_http() -> httpx.AsyncClient:
    global _rocketchat_admin_http
    if _rocketchat_admin_http is None:
        if not ADMIN_USER_ID or not ADMIN_PAT:
            raise RuntimeError(
                "ADMIN_USER_ID and ADMIN_PAT must be set for the join flow. "
                "Add them to infra/rocketchat/.env."
            )
        _rocketchat_admin_http = httpx.AsyncClient(
            base_url=ROCKETCHAT_URL,
            headers={
                "X-Auth-Token": ADMIN_PAT,
                "X-User-Id": ADMIN_USER_ID,
                "Content-Type": "application/json",
            },
            timeout=30.0,
        )
    return _rocketchat_admin_http


EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
# Match @-mentions in message text. The agent's username from SQLite is
# what we compare against (case-insensitive). Captures the bare handle.
MENTION_HANDLE_RE = re.compile(r"@([a-zA-Z0-9._-]+)")


app = FastAPI(title="youragentnetwork glue", version="0.23.0")


# --- SQLite schema bootstrap ---

def init_agents_db() -> None:
    """Create the agents table if it does not exist."""
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(AGENTS_DB_FILE) as conn:
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS agents (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                human_username TEXT NOT NULL,
                human_name TEXT NOT NULL,
                human_email TEXT NOT NULL,
                human_rc_user_id TEXT NOT NULL,
                agent_name_input TEXT NOT NULL,
                agent_username TEXT NOT NULL,
                agent_display_name TEXT NOT NULL,
                agent_rc_user_id TEXT NOT NULL,
                agent_rc_auth_token TEXT NOT NULL,
                openclaw_agent TEXT NOT NULL,
                persona TEXT NOT NULL,
                UNIQUE(human_username),
                UNIQUE(agent_username)
            );
            CREATE TABLE IF NOT EXISTS invites (
                token TEXT PRIMARY KEY,
                email TEXT,
                created_at INTEGER NOT NULL,
                expires_at INTEGER,
                used_at INTEGER,
                used_by TEXT
            );
            """
        )
        # Migration: columns for EXTERNAL A2A-protocol agents, added after the
        # original schema. An A2A agent reuses this table (so the loop guard and
        # @-mention routing cover it for free) but is reached over A2A instead of
        # OpenClaw. Existing OpenClaw rows default to type 'openclaw'; the two
        # a2a_* columns stay NULL for them. The human_* / openclaw_agent columns
        # are NOT NULL, so an A2A row stores sentinels there (human_username gets
        # a unique 'a2a-<username>' to satisfy UNIQUE(human_username)).
        cols = {row[1] for row in conn.execute("PRAGMA table_info(agents)")}
        if "type" not in cols:
            conn.execute(
                "ALTER TABLE agents ADD COLUMN type TEXT NOT NULL DEFAULT 'openclaw'"
            )
        if "a2a_card_url" not in cols:
            conn.execute("ALTER TABLE agents ADD COLUMN a2a_card_url TEXT")
        if "a2a_bearer_token" not in cols:
            conn.execute("ALTER TABLE agents ADD COLUMN a2a_bearer_token TEXT")

        # Invites migration: reusable "team link" support. A reusable invite is
        # not spent on use (anyone who can reach the URL may join); `uses` counts
        # how many joined through it.
        icols = {row[1] for row in conn.execute("PRAGMA table_info(invites)")}
        if "reusable" not in icols:
            conn.execute("ALTER TABLE invites ADD COLUMN reusable INTEGER NOT NULL DEFAULT 0")
        if "uses" not in icols:
            conn.execute("ALTER TABLE invites ADD COLUMN uses INTEGER NOT NULL DEFAULT 0")


@app.on_event("startup")
def on_startup() -> None:
    init_agents_db()


@app.on_event("startup")
async def _start_cron_watcher() -> None:
    """
    Launch the background reminder-delivery watcher. Kept as its own async
    startup handler so create_task runs inside the live event loop. The task
    reference is parked on app.state so it is not garbage-collected.
    """
    app.state.cron_watcher_task = asyncio.create_task(cron_watcher_loop())


@app.get("/health")
def health() -> dict:
    """Liveness probe. Exposes the version so a deploy can be verified."""
    return {"status": "ok", "version": app.version}


# Branding is configurable per deployment via BRAND_LOGO_URL (an external URL).
# We do not ship a vendor logo. If BRAND_LOGO_URL is empty, the join page shows
# no logo (just the instance name).
def _brand_logo_html() -> str:
    if not BRAND_LOGO_URL:
        return ""
    safe_url = _html_escape(BRAND_LOGO_URL)
    safe_alt = _html_escape(INSTANCE_NAME)
    return f'<img class="brand-logo" src="{safe_url}" alt="{safe_alt}">'


# ============================================================
#                    webhook handler
# ============================================================
#
# v0.8 routing model (per-user agents):
#
#   - DM (channel type 'd'): look up the OTHER user in the DM; if
#     they are a registered agent in our SQLite table, that agent
#     responds. Otherwise we ignore the message.
#
#   - Public channel ('c') or private group ('p'): find any
#     @-mentions in the message text; for each mention that matches
#     a registered agent's username, that agent responds. If a
#     message mentions two agents, both respond in tag order. If no
#     mentions match known agents, we ignore.
#
#   - Loop prevention: skip any message sent by a user we know to
#     be an agent (lookup in SQLite). Also skip the legacy lois
#     BOT_USER_ID for safety even though lois is no longer in the
#     active routing.
#
# Each responding agent uses its OWN OpenClaw model id (private for
# DM context, team for public context) and its OWN Rocket.Chat
# credentials for posting the reply.

async def get_room_info_as_admin(channel_id: str) -> dict | None:
    """
    Fetch a Rocket.Chat room's info using admin auth.

    Returns None if the admin cannot see the room (e.g., a DM the admin
    is not a member of, without the view-d-room permission). Callers
    should handle the None case.
    """
    admin_http = get_admin_http()
    try:
        response = await admin_http.get(
            "/api/v1/rooms.info",
            params={"roomId": channel_id},
        )
        if response.status_code == 200:
            return response.json().get("room", {})
    except Exception:
        log.exception("rooms.info (admin) failed for %s", channel_id)
    return None


async def is_dm_visible_to_agent(channel_id: str, agent: dict) -> bool:
    """
    Test whether a specific agent can see a given DM by trying
    rooms.info with the agent's own credentials. If the agent is a
    member of the DM, rooms.info returns 200 and t='d'; otherwise it
    returns 400 / 403 / 404.

    This is how we figure out which agent owns a DM without needing
    the admin view-d-room permission: the agent in the DM CAN see it,
    so their own credentials work.
    """
    try:
        async with httpx.AsyncClient(
            base_url=ROCKETCHAT_URL,
            headers={
                "X-Auth-Token": agent["agent_rc_auth_token"],
                "X-User-Id": agent["agent_rc_user_id"],
            },
            timeout=10.0,
        ) as client:
            response = await client.get(
                "/api/v1/rooms.info",
                params={"roomId": channel_id},
            )
            if response.status_code != 200:
                return False
            return response.json().get("room", {}).get("t") == "d"
    except Exception:
        return False


def find_agent_by_rc_user_id(rc_user_id: str) -> dict | None:
    """Look up an agent record by its Rocket.Chat user id."""
    with sqlite3.connect(AGENTS_DB_FILE) as conn:
        conn.row_factory = sqlite3.Row
        row = conn.execute(
            "SELECT * FROM agents WHERE agent_rc_user_id = ?",
            (rc_user_id,),
        ).fetchone()
        return dict(row) if row else None


def find_agent_by_username(username: str) -> dict | None:
    """Look up an agent record by its Rocket.Chat username (case-insensitive)."""
    with sqlite3.connect(AGENTS_DB_FILE) as conn:
        conn.row_factory = sqlite3.Row
        row = conn.execute(
            "SELECT * FROM agents WHERE LOWER(agent_username) = LOWER(?)",
            (username,),
        ).fetchone()
        return dict(row) if row else None


def find_agent_by_openclaw_id(openclaw_agent: str) -> dict | None:
    """
    Look up an agent record by its bare OpenClaw agent id (the value stored in
    the `openclaw_agent` column). Used by the cron webhook: a fired job's
    payload carries `job.agentId`, which is this id, and we map it back to the
    agent's Rocket.Chat credentials and human so we can deliver the reminder.
    """
    with sqlite3.connect(AGENTS_DB_FILE) as conn:
        conn.row_factory = sqlite3.Row
        row = conn.execute(
            "SELECT * FROM agents WHERE openclaw_agent = ?",
            (openclaw_agent,),
        ).fetchone()
        return dict(row) if row else None


def is_known_agent_user_id(rc_user_id: str) -> bool:
    """Loop-prevention check: is this Rocket.Chat user_id one of our agents?"""
    return find_agent_by_rc_user_id(rc_user_id) is not None


async def find_agent_for_dm(channel_id: str) -> dict | None:
    """
    Find which agent owns a DM channel by trying each registered
    agent's own credentials against rooms.info.

    The agent that successfully sees the room is in the DM. This
    avoids the chicken-and-egg of "we need admin view-d-room
    permission to look up who's in a DM," because each agent can
    see their own DMs.

    Iterates through all agents in the SQLite table. For a small
    test team this is fine; if the agent list grows large we can
    add a channel_id -> agent_id cache table populated on first
    successful match.
    """
    with sqlite3.connect(AGENTS_DB_FILE) as conn:
        conn.row_factory = sqlite3.Row
        agents = [dict(row) for row in conn.execute("SELECT * FROM agents")]
    for agent in agents:
        if await is_dm_visible_to_agent(channel_id, agent):
            return agent
    return None


def find_agents_mentioned(message_text: str) -> list[dict]:
    """
    Parse a message for @-mentions that match registered agents.

    Returns agent records in tag order, de-duplicated (if someone
    @-tags the same agent twice in one message, the agent only
    responds once).
    """
    mentions = MENTION_HANDLE_RE.findall(message_text)
    agents: list[dict] = []
    seen: set[str] = set()
    for handle in mentions:
        key = handle.lower()
        if key in seen:
            continue
        seen.add(key)
        agent = find_agent_by_username(handle)
        if agent:
            agents.append(agent)
    return agents


async def ask_openclaw_as(agent_id: str, message_text: str, session_user: str) -> str:
    """
    Call OpenClaw with a specific agent's id.

    OpenClaw's model parameter format is 'openclaw/<agentId>' (or
    plain 'openclaw' for the default agent). We store just the bare
    agent_id in SQLite for cleanliness; this function adds the prefix
    when making the API call.
    """
    response = await openclaw.chat.completions.create(
        model=f"openclaw/{agent_id}",
        messages=[{"role": "user", "content": message_text}],
        user=session_user,
    )
    return response.choices[0].message.content or ""


# OpenClaw returns this exact string (with HTTP 200) when an agent run
# produces no assistant message. It is a placeholder, not a real reply,
# so we must never post it into a channel as if the agent said it.
OPENCLAW_NO_REPLY_SENTINEL = "No response from OpenClaw."

# What an agent posts when, even after a retry, OpenClaw could not
# produce a real reply. Honest and recoverable, and far better than
# leaking the raw placeholder or going silent.
OPENCLAW_GRACEFUL_FALLBACK = "Sorry, I hit a snag and didn't catch that. Mind trying again?"


def _is_real_reply(text: str) -> bool:
    """True only if OpenClaw returned actual content, not its placeholder."""
    stripped = text.strip()
    return bool(stripped) and stripped != OPENCLAW_NO_REPLY_SENTINEL


async def ask_openclaw_reliably(
    agent_id: str, message_text: str, session_user: str, label: str = ""
) -> str:
    """
    Call OpenClaw and return a REAL reply, retrying once on the no-reply
    placeholder (or an exception), then falling back to a graceful
    message. Guarantees the caller never has to post the raw
    'No response from OpenClaw.' placeholder into a channel.

    The placeholder appears to be a transient hiccup (seen once on
    2026-06-03, did not recur on the next agent), so a single retry
    clears most cases; the fallback covers the rest.
    """
    for attempt in (1, 2):
        try:
            reply = await ask_openclaw_as(agent_id, message_text, session_user)
        except Exception:
            log.exception(
                "OpenClaw call raised (attempt %d%s)", attempt,
                f", {label}" if label else "",
            )
            reply = ""
        if _is_real_reply(reply):
            return reply
        log.warning(
            "OpenClaw gave no real reply (attempt %d%s): %r",
            attempt, f", {label}" if label else "", reply,
        )
    return OPENCLAW_GRACEFUL_FALLBACK


# What an external A2A agent's bot posts when its endpoint errors or returns
# nothing. Mirrors OPENCLAW_GRACEFUL_FALLBACK: honest and recoverable, never a
# raw error or silence.
A2A_GRACEFUL_FALLBACK = "Sorry, I couldn't reach that agent just now. Mind trying again?"


async def generate_a2a_reply(agent: dict, message_text: str, context_id: str) -> str:
    """
    Call an EXTERNAL A2A agent (over the A2A protocol, see a2a_client) and
    return a postable reply, falling back gracefully on failure so we never
    post an error or go silent.

    context_id is the per-conversation thread key for the remote agent's
    multi-turn memory. v1 passes the Rocket.Chat channel/room id, so all
    messages to this agent in one channel share a thread; no transcript is
    stored on our side. (If a remote agent requires server-assigned context
    ids, we would capture and reuse the id from its first response instead.)
    """
    try:
        reply = await ask_a2a_agent(
            card_url=agent["a2a_card_url"],
            bearer_token=agent.get("a2a_bearer_token"),
            message=message_text,
            context_id=context_id,
        )
    except Exception:
        log.exception("a2a: call failed for %s", agent["agent_username"])
        return A2A_GRACEFUL_FALLBACK
    return reply.strip() or A2A_GRACEFUL_FALLBACK


async def post_as_agent(agent: dict, channel_id: str, text: str) -> None:
    """
    Post a message to a Rocket.Chat channel using a specific agent's
    auth credentials, so the message appears in the channel AS that
    agent (their avatar, their name).
    """
    async with httpx.AsyncClient(
        base_url=ROCKETCHAT_URL,
        headers={
            "X-Auth-Token": agent["agent_rc_auth_token"],
            "X-User-Id": agent["agent_rc_user_id"],
            "Content-Type": "application/json",
        },
        timeout=30.0,
    ) as client:
        response = await client.post(
            "/api/v1/chat.postMessage",
            json={"roomId": channel_id, "text": text},
        )
        response.raise_for_status()


# ============================================================
#   Agent action tools: message_human + ask_agent
#
#   Agents are invoked with these as OpenAI-style tools (tool_choice=auto), so a
#   normal reply ignores them. They fire only when the operator asks the agent to
#   message a teammate or to find something out from another agent. Both are
#   operator-initiated and loop-safe by construction:
#     - message_human posts into the agent's own 1:1 DM; the webhook ignores
#       agent-authored posts, so it never re-fires.
#     - ask_agent queries other agents OFF the chat plane and invokes each target
#       WITHOUT tools, so it cannot fan out further (one hop) and nothing is
#       posted to chat. The asking agent is never queried; fan-out is bounded.
# ============================================================

# Bound tool round-trips per turn so a misbehaving model can't loop forever.
MAX_TOOL_ITERS = 5
# Cap how many agents one ask_agent call can fan out to (bounds cost).
MAX_ASK_AGENTS = 5


def find_human_by_username(username: str) -> dict | None:
    """Look up a teammate (human) by Rocket.Chat username (case-insensitive)."""
    with sqlite3.connect(AGENTS_DB_FILE) as conn:
        conn.row_factory = sqlite3.Row
        row = conn.execute(
            "SELECT human_username, human_name, human_rc_user_id FROM agents "
            "WHERE LOWER(human_username)=LOWER(?)",
            (username,),
        ).fetchone()
        return dict(row) if row else None


MESSAGE_HUMAN_TOOL = {
    "type": "function",
    "function": {
        "name": "message_human",
        "description": (
            "Send a direct message to a teammate (a human on the platform) on the "
            "operator's behalf. Use this ONLY when the operator explicitly asks you "
            "to message, notify, or ask someone something. The message is posted "
            "into your 1:1 direct message with that person, in your own voice."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "human": {
                    "type": "string",
                    "description": "The teammate's Rocket.Chat username, e.g. 'tushar'.",
                },
                "text": {
                    "type": "string",
                    "description": "The message to send, in your own voice.",
                },
            },
            "required": ["human", "text"],
        },
    },
}

ASK_AGENT_TOOL = {
    "type": "function",
    "function": {
        "name": "ask_agent",
        "description": (
            "Ask one or more OTHER agents on the platform a question and get their "
            "answers back, so you can synthesize and answer the operator. Use this "
            "when the operator asks you to find something out from another teammate's "
            "agent. You receive the answers directly; nothing is posted to any chat. "
            "Each agent answers from its own knowledge."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "agents": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Agent usernames to ask, e.g. ['niles', 'swamiji'].",
                },
                "question": {
                    "type": "string",
                    "description": "The question to ask them, in plain language.",
                },
            },
            "required": ["agents", "question"],
        },
    },
}

# Tools offered on every OpenClaw agent invocation. The model uses them only when
# relevant (tool_choice=auto); a normal reply ignores them.
AGENT_TOOLS = [MESSAGE_HUMAN_TOOL, ASK_AGENT_TOOL]


async def execute_message_human(asking_agent: dict, args: dict) -> str:
    """Deliver a DM from `asking_agent` to a target human. Returns a result
    string for the model (success, or a clear error it can relay)."""
    target = (args.get("human") or args.get("username") or "").strip().lstrip("@")
    text = (args.get("text") or args.get("message") or "").strip()
    if not target or not text:
        return "error: message_human needs both 'human' (the username) and 'text'."
    human = find_human_by_username(target)
    if not human:
        return (
            f"error: no teammate found with username '{target}'. "
            "Use their exact Rocket.Chat username."
        )
    try:
        room_id = await create_dm_as_agent(
            asking_agent["agent_rc_auth_token"],
            asking_agent["agent_rc_user_id"],
            human["human_username"],
        )
        await post_as_agent(asking_agent, room_id, text)
        log.info(
            "message_human: %s delivered a message to %s",
            asking_agent["agent_username"], human["human_username"],
        )
        return (
            "Delivered. Your message was posted into your direct message with "
            f"@{human['human_username']}."
        )
    except Exception:
        log.exception(
            "message_human failed: %s -> %s", asking_agent["agent_username"], target,
        )
        return f"error: could not deliver the message to @{target} (delivery error)."


async def execute_ask_agent(asking_agent: dict, args: dict) -> str:
    """Query one or more OTHER agents directly (OFF the chat plane) and return
    their answers to the asking agent.

    Loop-free by construction: each target is invoked WITHOUT action tools, so it
    simply answers and cannot fan out further (depth is capped at one hop), and
    nothing is posted to chat so the webhook never re-fires. The asking agent
    itself is never queried; duplicates and the fan-out count are bounded.
    """
    raw = args.get("agents") or args.get("agent") or []
    if isinstance(raw, str):
        raw = [raw]
    question = (args.get("question") or args.get("text") or "").strip()
    if not raw or not question:
        return "error: ask_agent needs 'agents' (a list of usernames) and 'question'."
    asker_un = asking_agent["agent_username"]
    asker_human = (
        asking_agent.get("human_name") or asking_agent.get("human_username") or "their operator"
    )
    results: list[str] = []
    seen: set[str] = set()
    for t in list(raw)[:MAX_ASK_AGENTS]:
        name = str(t).strip().lstrip("@")
        key = name.lower()
        if not name or key in seen:
            continue
        seen.add(key)
        if key == asker_un.lower():
            continue  # an agent cannot ask itself
        target = find_agent_by_username(name)
        if not target:
            results.append(f"@{name}: (no agent with that username)")
            continue
        framed = (
            f"{asker_human} (via their agent @{asker_un}) is asking you: {question}\n\n"
            "Answer from what you know, sharing only what is appropriate to share. "
            "Keep it brief; if you do not know, say so."
        )
        try:
            if target.get("type") == "a2a":
                ans = await generate_a2a_reply(target, framed, context_id=f"ask:{asker_un}")
            else:
                # NO tools passed: the asked agent just answers and cannot
                # re-ask, which is what keeps agent-to-agent loop-free.
                tmodel = target["openclaw_agent"]
                ans = await ask_openclaw_as(tmodel, framed, f"agent-{tmodel}")
                if not _is_real_reply(ans):
                    ans = "(no answer)"
            log.info("ask_agent: %s queried %s", asker_un, target["agent_username"])
        except Exception:
            log.exception("ask_agent: query to %s failed", name)
            ans = "(error reaching this agent)"
        results.append(f"@{target['agent_username']} says: {(ans or '').strip()}")
    return "\n\n".join(results) if results else "error: no valid agents to ask."


async def execute_agent_tool(asking_agent: dict, name: str, arguments_json: str) -> str:
    """Dispatch a tool call to its handler; returns a result string."""
    try:
        args = json.loads(arguments_json or "{}")
    except Exception:
        return "error: tool arguments were not valid JSON."
    if name == "message_human":
        return await execute_message_human(asking_agent, args)
    if name == "ask_agent":
        return await execute_ask_agent(asking_agent, args)
    return f"error: unknown tool '{name}'."


async def ask_openclaw_with_tools(
    asking_agent: dict, openclaw_model: str, message_text: str,
    session_user: str, label: str = "",
) -> str:
    """
    Invoke an OpenClaw agent with the agent-action tools available, running the
    function-calling loop: if the model calls a tool, execute it, feed the result
    back, and continue until it returns a normal reply (capped by MAX_TOOL_ITERS).
    Falls back gracefully so a channel never sees a raw placeholder. `asking_agent`
    is the agent record the tools act on behalf of.
    """
    model = f"openclaw/{openclaw_model}"
    messages: list[dict] = [{"role": "user", "content": message_text}]
    for _ in range(MAX_TOOL_ITERS):
        try:
            resp = await openclaw.chat.completions.create(
                model=model, messages=messages, tools=AGENT_TOOLS,
                tool_choice="auto", user=session_user,
            )
        except Exception:
            log.exception("OpenClaw tool-call failed (%s)", label)
            return OPENCLAW_GRACEFUL_FALLBACK
        msg = resp.choices[0].message
        tool_calls = getattr(msg, "tool_calls", None)
        if not tool_calls:
            content = msg.content or ""
            return content if _is_real_reply(content) else OPENCLAW_GRACEFUL_FALLBACK
        # Record the assistant's tool-call message, then run each tool.
        messages.append({
            "role": "assistant",
            "content": msg.content or "",
            "tool_calls": [
                {"id": tc.id, "type": "function",
                 "function": {"name": tc.function.name, "arguments": tc.function.arguments}}
                for tc in tool_calls
            ],
        })
        for tc in tool_calls:
            log.info("tool call (%s): %s %s", label, tc.function.name, tc.function.arguments)
            result = await execute_agent_tool(
                asking_agent, tc.function.name, tc.function.arguments
            )
            messages.append({"role": "tool", "tool_call_id": tc.id, "content": result})
    log.warning("OpenClaw tool loop hit max iterations (%s)", label)
    return OPENCLAW_GRACEFUL_FALLBACK


# ============================================================
#   DM file uploads
#
#   Rocket.Chat's outgoing webhook is TEXT-ONLY: it strips file
#   attachments and only delivers the caption. So when a user shares a
#   file in their 1:1 DM, we recover it by fetching the message by id
#   with the AGENT's own token (the agent is always a member of its own
#   DM), download the file, and save it into the agent's workspace under
#   uploads/. The webhook then hands the agent the saved path in the same
#   turn so "summarise this" resolves to the file.
#
#   Files are SAVED, not auto-read: the agent reads on demand (see
#   UPLOADS_SECTION), so a large upload never bloats the session.
#   DM only; shared-folder contributions use the separate upload form.
# ============================================================

# Collapse anything that is not a safe filename char so a Rocket.Chat
# filename cannot escape the uploads/ dir or create a dotfile.
UPLOAD_FILENAME_RE = re.compile(r"[^A-Za-z0-9._-]+")


def _safe_upload_name(name: str) -> str:
    """Sanitise a Rocket.Chat filename to a safe basename for the workspace."""
    base = os.path.basename(name or "").strip()
    base = UPLOAD_FILENAME_RE.sub("_", base).lstrip(".")
    return base or "file"


# --- PDF text extraction --------------------------------------------------
# An agent's file-read tool reads bytes as text, so a binary PDF would come
# back as garbage. At ingest we extract a PDF's text to a sibling ".txt" and
# point the agent at THAT, so "summarise this PDF" resolves to readable text
# while the original PDF stays on disk. Scope: text-layer PDFs only. A scanned
# or image-only PDF has no text layer, so extraction returns nothing and OCR
# is a later add; in that case we still write the sidecar with a short note so
# the agent answers sensibly instead of reading binary garbage.

# Cap a single extracted text so one pathological PDF cannot bloat a read.
PDF_TEXT_MAX_CHARS = 200_000


def _looks_like_pdf(safe_name: str, content: bytes) -> bool:
    """True if this upload is a PDF, by extension OR by the %PDF- magic bytes
    (the content sniff catches a PDF that arrived with a misleading name)."""
    return safe_name.lower().endswith(".pdf") or content[:5] == b"%PDF-"


def _extract_pdf_text(pdf_path: pathlib.Path) -> str:
    """Best-effort text extraction from a PDF. Returns '' on any failure
    (encrypted, corrupt, or no text layer), so callers can fall back."""
    try:
        from pypdf import PdfReader

        reader = PdfReader(str(pdf_path))
        parts = [(page.extract_text() or "") for page in reader.pages]
        return "\n\n".join(parts).strip()
    except Exception:
        log.exception("PDF text extraction failed for %s", pdf_path)
        return ""


def _write_pdf_sidecar(pdf_path: pathlib.Path) -> pathlib.Path:
    """Extract pdf_path's text to a sibling '<name>.pdf.txt' and return that
    path. ALWAYS yields a readable text file: a short note stands in when the
    PDF has no extractable text (scanned/image-only)."""
    text = _extract_pdf_text(pdf_path)
    if len(text) > PDF_TEXT_MAX_CHARS:
        text = text[:PDF_TEXT_MAX_CHARS] + "\n\n[...truncated...]"
    if not text:
        text = (
            "[This PDF has no extractable text layer (it looks scanned or "
            "image-only). Text extraction returned nothing and OCR is not yet "
            "supported. If you need the contents, ask the user to paste the "
            "relevant text.]"
        )
    sidecar = pdf_path.parent / (pdf_path.name + ".txt")  # report.pdf -> report.pdf.txt
    sidecar.write_text(text, encoding="utf-8")
    os.chmod(sidecar, 0o666)
    log.info("extracted PDF text -> %s (%d chars)", sidecar.name, len(text))
    return sidecar


def _readable_upload_name(path: pathlib.Path, safe: str, content: bytes) -> str:
    """Given a just-saved upload, return the basename the AGENT should read.
    For a PDF that means a freshly written '.pdf.txt' sidecar; for anything
    else it is the file itself, unchanged."""
    if _looks_like_pdf(safe, content):
        return _write_pdf_sidecar(path).name
    return safe


def _agent_rc_client(agent: dict) -> httpx.AsyncClient:
    """An httpx client authenticated as the agent (a member of its own DM)."""
    return httpx.AsyncClient(
        base_url=ROCKETCHAT_URL,
        headers={
            "X-Auth-Token": agent["agent_rc_auth_token"],
            "X-User-Id": agent["agent_rc_user_id"],
        },
        timeout=30.0,
    )


async def _fetch_dm_message(
    agent: dict, room_id: str, message_id: str
) -> dict | None:
    """
    Return the full message (attachments included) for a DM message id,
    using the agent's own credentials. The outgoing webhook is text-only,
    so this recovers the file metadata it stripped. We use im.history
    because chat.getMessage is permission-gated even for members on this
    Rocket.Chat; an agent can always read its own DM history.
    """
    async with _agent_rc_client(agent) as client:
        resp = await client.get(
            "/api/v1/im.history",
            params={"roomId": room_id, "count": 10},
        )
        resp.raise_for_status()
        data = resp.json()
    if not data.get("success"):
        log.warning("im.history failed for room %s: %s", room_id, data)
        return None
    for m in data.get("messages", []):
        if m.get("_id") == message_id:
            return m
    log.info("message %s not found in recent DM history", message_id)
    return None


def _attachment_downloads(message: dict) -> list[tuple[str, str]]:
    """
    Extract (download_link, filename) pairs from a message's file
    attachments. Returns [] when the message carries no files.
    """
    out: list[tuple[str, str]] = []
    for att in message.get("attachments") or []:
        link = att.get("title_link") or att.get("image_url")
        if link:
            out.append((link, att.get("title") or "file"))
    return out


async def ingest_dm_attachments(
    agent: dict, room_id: str, message_id: str
) -> list[str]:
    """
    If the DM message carried file attachments, download each with the
    agent's token and save it under the agent's workspace uploads/ folder.
    Returns the saved paths AS OPENCLAW SEES THEM (so they can be handed to
    the agent), or [] when there are no attachments.
    """
    message = await _fetch_dm_message(agent, room_id, message_id)
    if not message:
        return []
    downloads = _attachment_downloads(message)
    if not downloads:
        return []

    agent_id = agent["openclaw_agent"]
    uploads_host = OPENCLAW_WORKSPACES_DIR / agent_id / "uploads"
    uploads_host.mkdir(parents=True, exist_ok=True)
    # World-writable like the workspace dir: the glue runs as root, the
    # agent runs as 'node' and must be able to read what we save here.
    os.chmod(uploads_host, 0o777)

    saved: list[str] = []
    async with _agent_rc_client(agent) as client:
        for link, name in downloads:
            resp = await client.get(link, follow_redirects=True)
            if resp.status_code != 200:
                log.warning(
                    "file download failed (HTTP %s) for %s",
                    resp.status_code, link,
                )
                continue
            safe = _safe_upload_name(name)
            path = uploads_host / safe
            path.write_bytes(resp.content)
            os.chmod(path, 0o666)
            log.info(
                "saved upload %r (%d bytes) for agent %s",
                safe, len(resp.content), agent_id,
            )
            # For a PDF, hand the agent the readable .txt sidecar, not the
            # binary PDF (the original stays on disk alongside it).
            read_name = _readable_upload_name(path, safe, resp.content)
            saved.append(
                f"{OPENCLAW_DATA_OPENCLAW_PATH}/workspaces/{agent_id}/uploads/{read_name}"
            )
    return saved


def _build_upload_turn(message_text: str, saved_paths: list[str]) -> str:
    """
    Fuse the user's caption with the saved file path(s) into one turn, so
    the agent knows what "this file" refers to, while instructing it to
    read on demand rather than preloading the contents.
    """
    listing = "\n".join(f"- {p}" for p in saved_paths)
    if message_text:
        return (
            f"{message_text}\n\n"
            "[The user attached the following file(s), saved in your workspace "
            "and readable with your file read tool:\n"
            f"{listing}\n"
            'If the message above refers to a shared file ("this", "the file", '
            'etc.) it means the attached file. Read it only to carry out what '
            "the message asks; otherwise just acknowledge that you saved it.]"
        )
    return (
        "[The user shared the following file(s) with no message, saved in your "
        "workspace:\n"
        f"{listing}\n"
        "Acknowledge that you received and saved them. Do not read or summarise "
        "them unless the user later asks.]"
    )


# ============================================================
#   Shared-knowledge channel uploads
#
#   Same shape as the DM ingest above, but the file goes to the TEAM-WIDE
#   shared folder (shared/uploads/) instead of one agent's workspace, and
#   we use the ADMIN token to fetch + download (admin owns the
#   #shared-knowledge channel, so it can read its history and files).
#   This channel is an inbox: it never triggers an agent reply.
# ============================================================

def _admin_rc_client(download: bool = False) -> httpx.AsyncClient:
    """
    An httpx client authenticated as the admin account. Pass download=True
    to omit the JSON Content-Type header (file downloads must not advertise
    a JSON body). ADMIN_USER_ID / ADMIN_PAT come from the glue env.
    """
    headers = {"X-Auth-Token": ADMIN_PAT, "X-User-Id": ADMIN_USER_ID}
    if not download:
        headers["Content-Type"] = "application/json"
    return httpx.AsyncClient(
        base_url=ROCKETCHAT_URL, headers=headers, timeout=30.0
    )


async def _fetch_channel_message(
    room_id: str, message_id: str
) -> dict | None:
    """
    Return the full message (attachments included) for a public-channel
    message id, using the admin credentials. Mirrors _fetch_dm_message but
    via channels.history (the public-channel equivalent of im.history).
    """
    async with _admin_rc_client() as client:
        resp = await client.get(
            "/api/v1/channels.history",
            params={"roomId": room_id, "count": 10},
        )
        resp.raise_for_status()
        data = resp.json()
    if not data.get("success"):
        log.warning("channels.history failed for room %s: %s", room_id, data)
        return None
    for m in data.get("messages", []):
        if m.get("_id") == message_id:
            return m
    log.info("message %s not found in recent channel history", message_id)
    return None


async def ingest_shared_attachments(
    room_id: str, message_id: str
) -> list[tuple[str, str]]:
    """
    If the #shared-knowledge message carried file attachments, download each
    with the admin token and save it into the team-wide shared/uploads/
    folder. Returns a list of (filename, openclaw_path) for the saved files
    (so the confirmation can name them), or [] when there are no attachments.
    """
    message = await _fetch_channel_message(room_id, message_id)
    if not message:
        return []
    downloads = _attachment_downloads(message)
    if not downloads:
        return []

    uploads_host = SHARED_DIR / "uploads"
    uploads_host.mkdir(parents=True, exist_ok=True)
    # World-readable like the rest of the shared folder: glue runs as root,
    # the agents run as 'node' and must be able to read what we save.
    os.chmod(uploads_host, 0o777)

    saved: list[tuple[str, str]] = []
    async with _admin_rc_client(download=True) as client:
        for link, name in downloads:
            resp = await client.get(link, follow_redirects=True)
            if resp.status_code != 200:
                log.warning(
                    "shared file download failed (HTTP %s) for %s",
                    resp.status_code, link,
                )
                continue
            safe = _safe_upload_name(name)
            path = uploads_host / safe
            path.write_bytes(resp.content)
            os.chmod(path, 0o666)
            log.info(
                "saved shared upload %r (%d bytes) to shared/uploads/",
                safe, len(resp.content),
            )
            # For a PDF, also write the readable .txt sidecar so agents reading
            # shared/uploads/ get text, not binary. The confirmation still names
            # the original file the human dropped (safe), while the openclaw
            # path points at the readable version.
            read_name = _readable_upload_name(path, safe, resp.content)
            saved.append(
                (safe, f"{OPENCLAW_DATA_OPENCLAW_PATH}/shared/uploads/{read_name}")
            )
    return saved


async def post_as_admin(channel_id: str, text: str) -> None:
    """Post a plain message to a channel as the admin account."""
    async with _admin_rc_client() as client:
        resp = await client.post(
            "/api/v1/chat.postMessage",
            json={"roomId": channel_id, "text": text},
        )
        resp.raise_for_status()


@app.post("/webhook")
async def webhook(request: Request) -> dict:
    """Rocket.Chat outgoing webhook -> per-agent routing -> reply."""
    payload = await request.json()
    log.info("webhook received: %s", payload)

    # --- loop prevention ---
    if payload.get("bot"):
        log.info("skipping bot-flagged message")
        return {"received": True}

    sender_user_id = payload.get("user_id", "")
    if sender_user_id == BOT_USER_ID:
        # Legacy guard for the original lois BOT_USER_ID (now obsolete in
        # routing but kept here so a stray legacy DM doesn't loop).
        log.info("skipping message from legacy BOT_USER_ID")
        return {"received": True}
    if sender_user_id and is_known_agent_user_id(sender_user_id):
        log.info("skipping message from known agent user_id=%s", sender_user_id)
        return {"received": True}

    channel_id = payload.get("channel_id")
    if not channel_id:
        log.warning("payload had no channel_id; skipping")
        return {"received": True}

    message_text = (payload.get("text") or "").strip()
    message_id = payload.get("message_id") or ""

    # --- channel type from payload (no API call needed) ---
    # Rocket.Chat outgoing webhooks include channel_name for public
    # channels and private groups, and omit it for direct messages.
    # We use this signal instead of calling rooms.info, which would
    # require the caller to be a member (or to have the view-d-room
    # admin permission, which the default admin role does not get).
    channel_name = (payload.get("channel_name") or "").strip()
    is_dm = not channel_name

    # --- routing decision ---
    # responders: list of (agent_record, openclaw_model_id) tuples.
    # Each entry is one agent that should produce one reply.
    # agent_input is what we actually feed OpenClaw: usually the raw text,
    # but in a DM it may be augmented with saved upload path(s).
    responders: list[tuple[dict, str]] = []
    agent_input = message_text

    if is_dm:
        agent = await find_agent_for_dm(channel_id)
        if not agent:
            log.info(
                "DM in channel %s has no known agent; skipping",
                channel_id,
            )
            return {"received": True}
        # The webhook strips file attachments, so recover any shared file
        # and save it into the agent's uploads/, then hand the agent the
        # path in THIS turn so "summarise this" resolves to the file.
        saved_paths: list[str] = []
        if message_id:
            try:
                saved_paths = await ingest_dm_attachments(
                    agent, channel_id, message_id
                )
            except Exception:
                log.exception(
                    "DM attachment ingest failed for channel %s", channel_id
                )
        if saved_paths:
            agent_input = _build_upload_turn(message_text, saved_paths)
        if not agent_input.strip():
            log.info("DM had neither text nor a file; skipping")
            return {"received": True}
        responders.append((agent, agent["openclaw_agent"]))
        log.info(
            "DM routed to agent %s (model %s); %d file(s) saved",
            agent["agent_username"], agent["openclaw_agent"], len(saved_paths),
        )
    else:
        # The shared-knowledge channel is a file inbox, not a conversation:
        # save any attachment into the team-wide shared/uploads/ folder and
        # never invoke an agent. Plain chatter (no attachment) is ignored,
        # which also keeps our own confirmation post from looping the webhook.
        if channel_name == SHARED_KNOWLEDGE_CHANNEL:
            if message_id:
                try:
                    saved = await ingest_shared_attachments(
                        channel_id, message_id
                    )
                except Exception:
                    log.exception(
                        "shared-knowledge ingest failed for channel %s",
                        channel_id,
                    )
                    saved = []
                if saved:
                    names = ", ".join(n for n, _ in saved)
                    log.info("shared-knowledge: saved %s", names)
                    try:
                        await post_as_admin(
                            channel_id,
                            f"Saved {names} to the shared knowledge folder. "
                            "All agents can read it on request.",
                        )
                    except Exception:
                        log.exception(
                            "shared-knowledge confirmation post failed"
                        )
            return {"received": True}

        if not message_text:
            log.warning("channel message had no text; skipping")
            return {"received": True}
        agents = find_agents_mentioned(message_text)
        if not agents:
            log.info(
                "public-style channel %r without any known-agent mention; "
                "skipping", channel_name,
            )
            return {"received": True}
        for agent in agents:
            responders.append((agent, agent["openclaw_agent"]))
        log.info(
            "public channel %r routed to %d agent(s): %s",
            channel_name, len(responders),
            [a["agent_username"] for a, _ in responders],
        )

    # --- per-agent generate + post ---
    # The session is keyed to the AGENT, not the room. That gives each
    # agent ONE continuous memory across its owner's private DM and the
    # team channel: something said in the DM is remembered when the agent
    # is later @-mentioned in the team channel. (Previously the session
    # was keyed per-room, which deliberately split the two; we dropped
    # that split in favour of one shared memory per agent.)
    for agent, openclaw_model in responders:
        # An external A2A agent is reached over the A2A protocol instead of
        # OpenClaw; everything else (posting as its bot persona, the loop
        # guard) is identical, since it lives in the same agents table.
        if agent.get("type") == "a2a":
            log.info(
                "calling A2A agent=%s card=%s message=%r",
                agent["agent_username"], agent.get("a2a_card_url"), agent_input,
            )
            reply = await generate_a2a_reply(agent, agent_input, channel_id)
            log.info("A2A reply (as %s): %r", agent["agent_username"], reply)
            try:
                await post_as_agent(agent, channel_id, reply)
                log.info("reply posted by %s", agent["agent_username"])
            except Exception:
                log.exception("failed to post reply as %s", agent["agent_username"])
            continue

        session_user = f"agent-{openclaw_model}"
        log.info(
            "calling OpenClaw as=%s model=%s session=%s message=%r",
            agent["agent_username"], openclaw_model, session_user, agent_input,
        )
        # Invoke the agent WITH its action tools (message_human, ask_agent),
        # running the tool-calling loop. A normal turn ignores the tools and
        # just replies; the loop falls back gracefully so the agent never posts
        # the raw 'No response from OpenClaw.' placeholder into a channel. (We
        # don't retry the tool path: a retry could re-run a tool like
        # message_human and double-send.)
        reply = await ask_openclaw_with_tools(
            agent, openclaw_model, agent_input, session_user,
            label=f"as {agent['agent_username']}",
        )
        log.info("OpenClaw reply (as %s): %r", agent["agent_username"], reply)

        try:
            log.info(
                "posting reply as %s to channel %s",
                agent["agent_username"], channel_id,
            )
            await post_as_agent(agent, channel_id, reply)
            log.info("reply posted by %s", agent["agent_username"])
        except Exception:
            log.exception(
                "failed to post reply as %s",
                agent["agent_username"],
            )

    return {"received": True}


# ============================================================
#   Scheduled reminders (OpenClaw cron -> glue -> Rocket.Chat)
#
#   OpenClaw has a full built-in cron system and its agents have a `cron`
#   tool, so "remind me Monday at 9am" is captured and scheduled by the agent
#   itself (see REMINDERS_SECTION). The ONE gap is delivery: OpenClaw has no
#   Rocket.Chat channel adapter, so a fired job cannot post to chat on its own.
#
#   We bridge that gap by PULLING, not by being pushed to. OpenClaw can POST a
#   fired job to a webhook, but its SSRF guard blocks any internal target: a
#   POST to http://glue:8000/cron is killed inside the gateway because `glue`
#   resolves to a private Docker IP ("resolves to private/internal IP address").
#   There is no config knob to allow it on the cron path. So instead the glue
#   reads the run record OpenClaw writes to disk for every fired job (see the
#   watcher, cron_watcher_loop) and posts the reminder into the agent's DM.
#   That stays inside our no-inbound-ports model and removes the need for the
#   agent to set delivery fields correctly at all.
#
#   The /cron webhook endpoint below is kept as a harmless fallback (it shares
#   deliver_cron_summary with the watcher) in case a future deployment can
#   reach the glue from the gateway, but in THIS deployment it never fires.
#
#   Security: OpenClaw sends `Authorization: Bearer <cron.webhookToken>`.
#   We require that header to match CRON_WEBHOOK_TOKEN exactly (set on the
#   box only). No token configured => we refuse every request, so a
#   half-configured deploy fails closed rather than posting unauthenticated.
#
#   No loop: posting as the agent is ignored by our own webhook guard
#   (is_known_agent_user_id), same as every other agent-sent message.
# ============================================================


def _cron_bearer_ok(request: Request) -> bool:
    """Constant-time check of the cron webhook bearer token."""
    if not CRON_WEBHOOK_TOKEN:
        return False
    header = request.headers.get("authorization", "")
    prefix = "Bearer "
    if not header.startswith(prefix):
        return False
    return hmac.compare_digest(header[len(prefix):], CRON_WEBHOOK_TOKEN)


@app.post("/cron")
async def cron_webhook(request: Request) -> dict:
    """
    Receive a fired OpenClaw cron job and deliver its output into the owning
    agent's 1:1 DM. Body is a CronEvent: we act only on a finished run that
    produced text, keyed to an agent we know.
    """
    if not _cron_bearer_ok(request):
        # 200 with no work, not 401: we never want OpenClaw's best-effort
        # webhook retries to hammer us, and an attacker learns nothing.
        log.warning("cron webhook: rejected (bad or missing bearer token)")
        return {"received": True}

    try:
        evt = await request.json()
    except Exception:
        log.warning("cron webhook: body was not JSON")
        return {"received": True}

    if evt.get("action") != "finished":
        # Other lifecycle events (added/updated/removed/started) carry no
        # deliverable text; ignore them quietly.
        return {"received": True}

    summary = (evt.get("summary") or "").strip()
    job = evt.get("job") or {}
    agent_id = (job.get("agentId") or evt.get("agentId") or "").strip()
    job_id = evt.get("jobId") or job.get("id") or "?"

    if not summary:
        log.info("cron webhook: job %s finished with no text; nothing to post", job_id)
        return {"received": True}
    if not agent_id:
        log.warning("cron webhook: job %s has no agentId; cannot route", job_id)
        return {"received": True}

    await deliver_cron_summary(agent_id, summary, job_id, source="webhook")
    return {"received": True}


async def deliver_cron_summary(
    agent_id: str, summary: str, job_id: str, source: str
) -> bool:
    """
    Post a fired cron job's text into the owning agent's 1:1 DM, as that agent.

    Shared by the (legacy) /cron webhook and the run-record watcher below.
    Returns True if it posted, False if it could not route or failed. Never
    raises: callers (a watcher loop, a best-effort webhook) must not die on one
    bad job.
    """
    agent = find_agent_by_openclaw_id(agent_id)
    if not agent:
        log.warning(
            "cron %s: job %s names unknown agentId=%s; skipping",
            source, job_id, agent_id,
        )
        return False
    try:
        room_id = await create_dm_as_agent(
            agent["agent_rc_auth_token"],
            agent["agent_rc_user_id"],
            agent["human_username"],
        )
        await post_as_agent(agent, room_id, summary)
        log.info(
            "cron %s: delivered job %s as %s to %s's DM",
            source, job_id, agent["agent_username"], agent["human_username"],
        )
        return True
    except Exception:
        log.exception(
            "cron %s: failed to deliver job %s as %s",
            source, job_id, agent["agent_username"],
        )
        return False


# --- run-record watcher: the delivery half of scheduled reminders ----------
#
# The webhook above is effectively dead in our deployment: OpenClaw's SSRF
# guard blocks a POST to glue:8000 because the hostname resolves to a private
# Docker IP. So instead of waiting to be pushed to, the glue PULLS: it watches
# the run records OpenClaw writes to disk for every fired job and delivers them
# itself. The agent still does the scheduling (its cron tool); we own delivery.


def _agent_id_from_session_key(session_key: str) -> str:
    """
    Pull the OpenClaw agent id out of a cron run's sessionKey, which looks like
    'agent:<agentId>:cron:<jobId>:run:<runId>'. Returns '' if it does not match.
    """
    parts = session_key.split(":")
    if len(parts) >= 2 and parts[0] == "agent":
        return parts[1].strip()
    return ""


def _run_key(rec: dict) -> str:
    """
    Stable unique id for one fired run, used for dedup. Each run has its own
    sessionId (the ':run:<runId>' tail of sessionKey); that is the natural key.
    Fall back to jobId+ts if a record somehow lacks it.
    """
    sid = (rec.get("sessionId") or "").strip()
    if sid:
        return sid
    return f"{rec.get('jobId', '?')}:{rec.get('ts', '?')}"


def _scan_cron_runs() -> list[dict]:
    """
    Read every run record currently on disk and return the deliverable ones as
    small dicts: {key, agent_id, summary, job_id}. Records that are malformed,
    unfinished, errored, or textless are skipped. Synchronous file I/O, but
    small and bounded (OpenClaw prunes old runs on its own schedule).
    """
    out: list[dict] = []
    try:
        files = sorted(CRON_RUNS_DIR.glob("*.jsonl"))
    except OSError:
        return out
    for path in files:
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        for line in text.splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                rec = json.loads(line)
            except json.JSONDecodeError:
                continue
            if rec.get("action") != "finished" or rec.get("status") != "ok":
                continue
            summary = (rec.get("summary") or "").strip()
            if not summary:
                continue
            agent_id = _agent_id_from_session_key(rec.get("sessionKey") or "")
            if not agent_id:
                continue
            out.append({
                "key": _run_key(rec),
                "agent_id": agent_id,
                "summary": summary,
                "job_id": rec.get("jobId") or "?",
            })
    return out


def _load_delivered() -> set[str]:
    """Load the set of run keys we have already posted (survives restarts)."""
    try:
        data = json.loads(CRON_DELIVERED_STATE.read_text(encoding="utf-8"))
        return set(data.get("delivered", []))
    except (OSError, json.JSONDecodeError):
        return set()


def _save_delivered(keys: set[str]) -> None:
    """Persist the delivered set atomically (temp file then rename)."""
    try:
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        tmp = CRON_DELIVERED_STATE.with_suffix(".json.tmp")
        tmp.write_text(json.dumps({"delivered": sorted(keys)}), encoding="utf-8")
        tmp.replace(CRON_DELIVERED_STATE)
    except OSError:
        log.exception("cron watcher: could not persist delivered state")


async def cron_watcher_loop() -> None:
    """
    Poll OpenClaw's cron run records and deliver each newly fired reminder into
    the owning agent's DM. We never leave the gateway, so the SSRF guard that
    kills the built-in webhook is a non-issue.

    First-run baseline: with no state file yet, we mark every run already on
    disk as delivered WITHOUT posting it, so a fresh deploy does not spray out
    stale or already-failed reminders. Only runs that appear after the baseline
    are delivered.

    Delivery is at-most-once: we mark a run delivered whether the post
    succeeded or the agent was unroutable, so a permanent problem cannot loop
    forever. The narrow cost is that a reminder landing in the exact poll window
    where Rocket.Chat is down would be dropped; reminders are best-effort and
    that window is ~10s, so we accept it for simplicity.
    """
    log.info(
        "cron watcher: starting (dir=%s, every %ss)",
        CRON_RUNS_DIR, CRON_POLL_SECONDS,
    )
    first_run = not CRON_DELIVERED_STATE.exists()
    delivered = _load_delivered()
    if first_run:
        delivered = {r["key"] for r in _scan_cron_runs()}
        _save_delivered(delivered)
        log.info(
            "cron watcher: baseline established, %d existing run(s) marked "
            "delivered (not posted)", len(delivered),
        )

    while True:
        try:
            runs = _scan_cron_runs()
            seen_now = {r["key"] for r in runs}
            pending = [r for r in runs if r["key"] not in delivered]
            for r in pending:
                await deliver_cron_summary(
                    r["agent_id"], r["summary"], r["job_id"], source="watcher"
                )
                delivered.add(r["key"])
            if pending:
                # Prune keys whose run files OpenClaw has since removed, so the
                # state file tracks current runs and cannot grow unbounded.
                delivered = {k for k in delivered if k in seen_now}
                _save_delivered(delivered)
        except Exception:
            log.exception("cron watcher: poll cycle failed; continuing")
        await asyncio.sleep(CRON_POLL_SECONDS)


# ============================================================
#               join flow shared helpers
# ============================================================

def slugify_username(name: str) -> str:
    """Derive a Rocket.Chat-safe username from a person or agent name."""
    slug = name.lower().strip()
    slug = re.sub(r"\s+", "-", slug)
    slug = re.sub(r"[^a-z0-9._-]", "", slug)
    slug = re.sub(r"-+", "-", slug)
    slug = slug.strip("-._")
    return slug[:50]


def generate_temp_password() -> str:
    """16-char random password from a shell-safe alphabet."""
    alphabet = string.ascii_letters + string.digits + "!@#%^*-_=+."
    return "".join(secrets.choice(alphabet) for _ in range(16))


def _html_escape(s: str) -> str:
    return (
        s.replace("&", "&amp;")
         .replace("<", "&lt;")
         .replace(">", "&gt;")
         .replace('"', "&quot;")
    )


# --- rollback helpers (used when a join fails partway) ---
# A partial join used to strand orphans (a half-created RC user + agent)
# that blocked the person from retrying with the same name. These let the
# handler undo what an attempt created so a retry starts clean.

async def delete_rc_user_by_id(user_id: str) -> None:
    """Best-effort delete of a Rocket.Chat user by id (rollback)."""
    if not user_id:
        return
    try:
        admin_http = get_admin_http()
        await admin_http.post(
            "/api/v1/users.delete",
            json={"userId": user_id, "confirmRelinquish": True},
        )
    except Exception:
        log.exception("rollback: failed to delete RC user %s", user_id)


def remove_agent_workspace(agent_id: str) -> None:
    """Best-effort removal of an agent's workspace directory (rollback)."""
    try:
        path = OPENCLAW_WORKSPACES_DIR / agent_id
        if path.exists():
            shutil.rmtree(path, ignore_errors=True)
    except Exception:
        log.exception("rollback: failed to remove workspace %s", agent_id)


# ============================================================
#               stage 2: create the human user
# ============================================================

async def create_human_rc_user(
    name: str,
    username: str,
    email: str,
    password: str,
) -> dict:
    admin_http = get_admin_http()
    response = await admin_http.post(
        "/api/v1/users.create",
        json={
            "name": name,
            "username": username,
            "email": email,
            "password": password,
            "verified": True,
            "requirePasswordChange": True,
            "sendWelcomeEmail": False,
            "roles": ["user"],
        },
    )
    response.raise_for_status()
    return response.json()["user"]


# ============================================================
#               stage 3a: create the agent's bot user
# ============================================================

async def create_agent_rc_user(
    agent_name_input: str,
    operator_username: str,
) -> dict:
    """
    Create a Rocket.Chat bot user for an agent.

    Returns a dict with username, display_name, rc_user_id, and
    rc_auth_token. The auth token is fetched by logging in as the new
    user; we store it for future calls that post messages AS the agent.
    """
    agent_username = slugify_username(agent_name_input)
    if not agent_username:
        raise ValueError(
            f"Could not derive a Rocket.Chat username from agent name "
            f"{agent_name_input!r}. Try a different agent name."
        )

    agent_display_name = agent_name_input.strip()
    agent_password = generate_temp_password()
    # We need a unique email to dodge collisions on re-runs in test
    # environments. The agent themselves never receives mail at this
    # address; it is just a Rocket.Chat database constraint.
    agent_email = f"{agent_username}+{secrets.token_hex(3)}@agentnetwork.local"

    admin_http = get_admin_http()
    create_response = await admin_http.post(
        "/api/v1/users.create",
        json={
            "name": agent_display_name,
            "username": agent_username,
            "email": agent_email,
            "password": agent_password,
            "verified": True,
            "requirePasswordChange": False,
            "sendWelcomeEmail": False,
            "roles": ["user"],
        },
    )
    create_response.raise_for_status()
    user_data = create_response.json()["user"]

    # Log in as the new user to get an auth token. This is the same
    # pattern setup-bot.sh uses; the auth token is what we will use to
    # post messages as the agent later.
    async with httpx.AsyncClient(base_url=ROCKETCHAT_URL, timeout=30.0) as client:
        login_response = await client.post(
            "/api/v1/login",
            json={"user": agent_username, "password": agent_password},
        )
        login_response.raise_for_status()
        login_data = login_response.json()["data"]

    return {
        "username": agent_username,
        "display_name": agent_display_name,
        "rc_user_id": user_data["_id"],
        "rc_auth_token": login_data["authToken"],
    }


# ============================================================
#       stage 3b: write workspace files for both agents
# ============================================================

def _build_soul_md(agent_name: str, persona: str) -> str:
    return f"""# Soul

You are {agent_name}.

## How you show up

{persona.strip()}

## How you do NOT show up

- Do not ask "who am I?" or "who are you?" on first contact. You already
  know. See IDENTITY.md and USER.md.
- Do not enumerate your own qualities ("I am direct, I am honest..."). Just
  be those things.
- Do not use emojis unless the operator does first.
- Do not end every message with a question. Sometimes the right move is to
  just say what you think and stop.

## Voice

Short sentences. Active voice. Precise nouns. Avoid corporate cadence.
When you have a strong opinion, state it as an opinion. When something is
a fact, do not soften it.
"""


# Index block telling every agent the shared knowledge folder exists and WHEN
# to read it. Kept tiny and static so it never bloats session context; it is a
# signpost, not the data. The same text is appended to existing agents by
# scripts/backfill-shared-knowledge.sh, so keep the "## Shared knowledge
# folder" heading stable as the idempotency marker. Path is the CONTAINER path
# (agents run as 'node'); the host path ~/.openclaw/shared is bind-mounted there.
SHARED_KNOWLEDGE_SECTION = """
## Shared knowledge folder

A shared, team-wide knowledge folder lives at /home/node/.openclaw/shared/.
It holds context that is NOT in your own memory or workspace, maintained by
the team:

- updates/latest.md    the most recent team update (newest entry at the top)
- updates/roadmap.md   the current roadmap
- updates/history/     dated past updates; ignore unless asked about a date
- org-knowledge/       slower-moving background: overview, product, venture
- uploads/             files the team contributed via the #shared-knowledge channel

When someone asks about recent updates, "what's the latest", "what's going
on", the roadmap, the product, or other org or team questions, use your file
read tool to read the relevant file under /home/node/.openclaw/shared/ FIRST,
then answer from what you read. For "what's the latest", start with
updates/latest.md. If someone refers to a document the team shared (a doc,
spec, deck, etc.) that is not in your own uploads/, look in
/home/node/.openclaw/shared/uploads/. Do NOT read these files when the
question is unrelated to org or team context.
"""


# Signpost for files a user shares privately in their 1:1 DM. The webhook saves
# each shared file into <workspace>/uploads/ and hands the agent the path in the
# same turn, so this block is mainly for LATER recall ("what was in that file I
# sent?"). Signpost, not payload: the agent reads a file on demand, never
# preloads it, so big uploads never bloat the session. The same text is appended
# to existing agents by scripts/backfill-shared-knowledge.sh, so keep the
# "## Files shared with you" heading stable as the idempotency marker.
UPLOADS_SECTION = """
## Files shared with you

When your operator shares a file with you in your private 1:1 channel, the glue
saves it in your workspace under uploads/ and tells you its path in that same
message. Read a file there with your file read tool ONLY when you are asked to
act on it or asked what it contains - do not preload or summarise uploaded files
unprompted. If your operator refers to "the file I sent" without naming it, look
in uploads/ for the most recent file.
"""


# Signpost teaching the agent how to set a scheduled reminder. The agent owns
# the scheduling (OpenClaw's built-in `cron` tool); the glue only delivers a
# fired job back into chat. The one non-obvious requirement is delivery: the job
# MUST be created with mode=webhook to http://glue:8000/cron, or the reminder
# fires but never reaches the human (OpenClaw has no Rocket.Chat adapter). The
# bearer token is configured globally on the gateway, so the agent does not set
# it. Keep the "## Setting reminders" heading stable as the backfill idempotency
# marker.
REMINDERS_SECTION = """
## Setting reminders

You can schedule a reminder for your operator using your cron tool. When they
ask for one ("remind me Monday at 9am to send the report", "every Friday tell me
to file my hours"), add a cron job:

- Set the schedule from what they said (a one-off time, or a repeating rule).
- Put what you should say when it fires into the job's prompt, in your own
  voice (e.g. "Remind them to send the report").
- CRITICAL - delivery. The cron job's `delivery` object MUST contain BOTH of
  these fields, exactly:

      delivery: { "mode": "webhook", "to": "http://glue:8000/cron" }

  Both are required. `mode` MUST be the literal string "webhook" and `to` MUST
  be "http://glue:8000/cron". This is the ONLY way the reminder reaches your
  operator. If you set `to` but leave out `mode` (or use any other mode), the
  job still fires but is delivered to nowhere - your operator silently never
  sees it. Never omit `mode`. Never schedule a reminder without this exact
  delivery object.
- The server clock is UTC. A wall-clock time like "9am" means 9am UTC unless you
  set a timezone. If you know your operator's timezone, pass it as the job's tz.
  If you do not know it, ask them which timezone they mean BEFORE scheduling, so
  the reminder does not fire at the wrong hour.

When it fires later, you write the reminder fresh and it is posted into your 1:1
channel as a message from you. After you add the job, confirm in plain language
what you scheduled and the exact local time. If they ask to change or cancel a
reminder, use your cron tool to update or remove the job.
"""


def _build_identity_md(agent_name: str, operator_name: str) -> str:
    return f"""# Identity

Your name is {agent_name}.

You are an AI agent inside {INSTANCE_NAME}, a team chat where every team
member has their own AI agent as a first-class participant.
You are {operator_name}'s agent.

You are not a chatbot, an assistant, or a tool. You are a teammate. You
appear in channels alongside humans with your own username and avatar.
People @-mention you the same way they @-mention any colleague.

## Where you work

You show up in two places, with ONE shared memory across both:

- A private 1:1 channel with {operator_name}. This is where they brief
  you, think out loud, and tell you what they want.
- The public team channel, where you respond when @-mentioned by anyone
  on the team. You carry everything from your 1:1 with {operator_name}
  into here too. The memory is shared, not split.

## What you can share

Be a helpful, knowledgeable teammate. Treat what you learn from
{operator_name} as shareable with the team by default. The ONE exception:
if {operator_name} explicitly tells you something is private or not to be
shared, keep it to yourself, including in the team channel. Everything
else is fair game. Answer questions usefully whether they come from
{operator_name} or from anyone else on the team.

You do not need to introduce yourself or your structure to {operator_name}.
They set you up. They know.
{SHARED_KNOWLEDGE_SECTION}{UPLOADS_SECTION}{REMINDERS_SECTION}"""


def _build_user_md(operator_name: str) -> str:
    return f"""# User

Your operator is {operator_name}.

How to address them: just "{operator_name}".

{operator_name} is using {INSTANCE_NAME}, a team chat where each person
has their own AI agent working alongside them as a teammate.

Treat this as a working colleague relationship.

Things to NOT do:

- Do not ask "who are you?" - you already know them.
- Do not introduce yourself by asking what kind of creature you are,
  what your vibe is, what your emoji should be. Those are already set in
  your IDENTITY.md and SOUL.md.
- Do not make every reply about the meta-context (that this is a research
  deployment). The meta-context is true but it is background, not
  foreground.
"""


def write_agent_workspace(
    agent_id: str,
    agent_display_name: str,
    persona: str,
    operator_name: str,
) -> str:
    """
    Create the on-disk workspace for an OpenClaw agent.

    Writes SOUL.md, IDENTITY.md, USER.md.

    Returns the workspace path AS OPENCLAW WILL SEE IT (under
    OPENCLAW_DATA_OPENCLAW_PATH), suitable for writing into
    openclaw.json so the gateway can find it.

    Permissions note: the glue container runs as root; OpenClaw runs
    as a non-root user (typically UID 1000 / 'node') in its container.
    OpenClaw expects to auto-create additional workspace bootstrap
    files like AGENTS.md and BOOTSTRAP.md on first agent load. If the
    workspace directory we create is owned by root with 755 perms,
    OpenClaw gets EACCES when it tries to write those bootstrap files
    and the entire agent fails to start. We explicitly chmod the dir
    and the files we write so OpenClaw has the access it needs.
    Acceptable for v0; production hardening would chown to a known
    UID instead of going world-writable.
    """
    workspace_host = OPENCLAW_WORKSPACES_DIR / agent_id
    workspace_host.mkdir(parents=True, exist_ok=True)

    files_to_write = [
        ("SOUL.md", _build_soul_md(agent_display_name, persona)),
        ("IDENTITY.md", _build_identity_md(agent_display_name, operator_name)),
        ("USER.md", _build_user_md(operator_name)),
    ]
    for filename, content in files_to_write:
        path = workspace_host / filename
        path.write_text(content)
        os.chmod(path, 0o666)

    # Directory must be world-writable so OpenClaw can create the
    # bootstrap files we do not provide (AGENTS.md, BOOTSTRAP.md,
    # HEARTBEAT.md). Without this, OpenClaw fails with EACCES on
    # first call to the agent.
    os.chmod(workspace_host, 0o777)

    # Path inside the OpenClaw container.
    return f"{OPENCLAW_DATA_OPENCLAW_PATH}/workspaces/{agent_id}"


# ============================================================
#       stage 3c: register agents in openclaw.json
# ============================================================

def update_openclaw_agents_list(new_agents: list[dict]) -> None:
    """
    Add or update entries in OpenClaw's agents.list.

    new_agents: a list of dicts of the form
        {"id": "<agent-id>", "workspace": "<path-in-openclaw>"}

    Idempotent: if an agent with the same id already exists, its
    workspace is updated. New entries are appended. Writes atomically
    via a temp file.
    """
    if not OPENCLAW_CONFIG_FILE.exists():
        raise FileNotFoundError(
            f"OpenClaw config not found at {OPENCLAW_CONFIG_FILE}. "
            f"Is the host's ~/.openclaw mounted into this container?"
        )

    with OPENCLAW_CONFIG_FILE.open() as f:
        config = json.load(f)

    agents_section = config.setdefault("agents", {})
    agents_list = agents_section.setdefault("list", [])

    by_id = {a["id"]: a for a in agents_list if "id" in a}
    for entry in new_agents:
        agent_id = entry["id"]
        if agent_id in by_id:
            by_id[agent_id].update(entry)
        else:
            agents_list.append(entry)
            by_id[agent_id] = entry

    temp_file = OPENCLAW_CONFIG_FILE.with_suffix(".json.tmp")
    with temp_file.open("w") as f:
        json.dump(config, f, indent=2)
        f.write("\n")
    temp_file.replace(OPENCLAW_CONFIG_FILE)


# ============================================================
#       stage 3d: restart OpenClaw container
# ============================================================

async def restart_openclaw_container() -> None:
    """
    Restart the OpenClaw container via the host's Docker socket so the
    new agents.list entries take effect.

    Requires /var/run/docker.sock to be mounted into this container.
    Triggers a restart asynchronously: this returns when Docker accepts
    the restart command. OpenClaw itself takes another ~30 seconds to
    come back online after the restart actually completes.
    """
    if not DOCKER_SOCKET.exists():
        raise RuntimeError(
            f"Docker socket not found at {DOCKER_SOCKET}. Add a volume "
            f"mount of /var/run/docker.sock to the glue container so it "
            f"can trigger OpenClaw restarts."
        )

    transport = httpx.AsyncHTTPTransport(uds=str(DOCKER_SOCKET))
    async with httpx.AsyncClient(transport=transport, timeout=60.0) as client:
        # The host part of the URL is ignored when using a unix socket
        # transport, but httpx requires SOME host.
        response = await client.post(
            f"http://localhost/containers/{OPENCLAW_CONTAINER_NAME}/restart"
        )
        # Docker returns 204 on success, 404 if container not found.
        if response.status_code not in (204, 200):
            raise RuntimeError(
                f"Docker restart returned HTTP {response.status_code}: "
                f"{response.text}"
            )


async def reload_openclaw() -> None:
    """
    Make OpenClaw apply the new agents.list entry, per OPENCLAW_RELOAD_STRATEGY.

    - hotreload (default): no-op. OpenClaw watches openclaw.json and hot-reloads
      agents.list automatically, so writing the file (stage 3c) is enough. No
      process control needed, so this works for native/local installs (npm,
      launchd, systemd) as well as Docker.
    - docker-restart: restart the container via the Docker socket (fallback for
      setups where the file-watch reload doesn't reach the gateway).
    - none: do nothing.
    """
    if OPENCLAW_RELOAD_STRATEGY == "docker-restart":
        await restart_openclaw_container()
    elif OPENCLAW_RELOAD_STRATEGY not in ("hotreload", "none", ""):
        log.warning(
            "unknown OPENCLAW_RELOAD_STRATEGY=%r; relying on hot-reload",
            OPENCLAW_RELOAD_STRATEGY,
        )


# ============================================================
#       stage 3e: persist agent metadata
# ============================================================

def persist_agent_record(record: dict) -> None:
    """Write a row into the agents SQLite table."""
    with sqlite3.connect(AGENTS_DB_FILE) as conn:
        conn.execute(
            """
            INSERT INTO agents (
                human_username, human_name, human_email, human_rc_user_id,
                agent_name_input, agent_username, agent_display_name,
                agent_rc_user_id, agent_rc_auth_token,
                openclaw_agent,
                persona
            ) VALUES (
                :human_username, :human_name, :human_email, :human_rc_user_id,
                :agent_name_input, :agent_username, :agent_display_name,
                :agent_rc_user_id, :agent_rc_auth_token,
                :openclaw_agent,
                :persona
            )
            """,
            record,
        )


# ============================================================
#       stage 4: team channel membership
# ============================================================

async def ensure_team_channel_id() -> str:
    """
    Return the room id of the shared team channel, creating it as a
    public channel if it does not exist yet.

    Idempotent: looks the channel up by name first; only creates when
    Rocket.Chat does not already know it. Handles the rare race where
    another join created it between our lookup and our create.
    """
    admin_http = get_admin_http()

    info = await admin_http.get(
        "/api/v1/channels.info", params={"roomName": TEAM_CHANNEL_NAME}
    )
    if info.status_code == 200 and info.json().get("success"):
        return info.json()["channel"]["_id"]

    create = await admin_http.post(
        "/api/v1/channels.create", json={"name": TEAM_CHANNEL_NAME}
    )
    if create.status_code == 200 and create.json().get("success"):
        return create.json()["channel"]["_id"]

    # Create failed: most likely another join created it a moment ago.
    # Re-fetch before giving up.
    retry = await admin_http.get(
        "/api/v1/channels.info", params={"roomName": TEAM_CHANNEL_NAME}
    )
    if retry.status_code == 200 and retry.json().get("success"):
        return retry.json()["channel"]["_id"]

    raise RuntimeError(
        f"could not find or create team channel {TEAM_CHANNEL_NAME!r}: "
        f"HTTP {create.status_code} {create.text}"
    )


async def invite_user_to_team_channel(team_channel_id: str, rc_user_id: str) -> None:
    """
    Add a Rocket.Chat user to the team channel as admin.

    channels.invite is idempotent: inviting a user who is already a
    member returns success, so this is safe to call on re-runs.
    """
    admin_http = get_admin_http()
    response = await admin_http.post(
        "/api/v1/channels.invite",
        json={"roomId": team_channel_id, "userId": rc_user_id},
    )
    response.raise_for_status()


async def ensure_shared_knowledge_channel_id() -> str:
    """
    Return the room id of the #shared-knowledge channel, creating it as a
    public file-inbox channel (with its topic) if it does not exist yet.

    Mirrors ensure_team_channel_id(): idempotent, looks up by name first and
    only creates when missing, tolerating the rare create race. Agents read the
    shared folder on disk, not this channel, so only humans are added to it.
    """
    admin_http = get_admin_http()

    info = await admin_http.get(
        "/api/v1/channels.info", params={"roomName": SHARED_KNOWLEDGE_CHANNEL}
    )
    if info.status_code == 200 and info.json().get("success"):
        return info.json()["channel"]["_id"]

    create = await admin_http.post(
        "/api/v1/channels.create", json={"name": SHARED_KNOWLEDGE_CHANNEL}
    )
    if create.status_code == 200 and create.json().get("success"):
        channel_id = create.json()["channel"]["_id"]
        # Set the inbox topic on first creation; non-fatal if it fails.
        try:
            await admin_http.post(
                "/api/v1/channels.setTopic",
                json={"roomId": channel_id, "topic": SHARED_KNOWLEDGE_CHANNEL_TOPIC},
            )
        except Exception:
            log.warning("could not set #%s topic", SHARED_KNOWLEDGE_CHANNEL)
        return channel_id

    retry = await admin_http.get(
        "/api/v1/channels.info", params={"roomName": SHARED_KNOWLEDGE_CHANNEL}
    )
    if retry.status_code == 200 and retry.json().get("success"):
        return retry.json()["channel"]["_id"]

    raise RuntimeError(
        f"could not find or create shared-knowledge channel "
        f"{SHARED_KNOWLEDGE_CHANNEL!r}: HTTP {create.status_code} {create.text}"
    )


# ============================================================
#       stage 5: private DM + welcome message
# ============================================================
#
# Runs as a background task AFTER the join response is sent, because it
# has to wait for OpenClaw to finish the stage-3d restart (~30-40s)
# before a live agent can generate the welcome. Posting the welcome into
# the DM is also what makes the DM appear in the human's sidebar, so an
# empty DM is never left dangling.

# OPENCLAW_NO_REPLY_SENTINEL and _is_real_reply are defined up near
# ask_openclaw_as (both the webhook and this stage-5 path use them).

WELCOME_GENERATION_PROMPT = (
    "Privately greet the operator who just set you up. Write a brief, warm "
    "first message in your own voice: 2 to 4 sentences. Use their name. Make "
    "it clear you know who they are and that you are their agent. Do not ask "
    "'who are you?' and do not interview them. Do not list your own traits. No "
    "headings or bullet points. Just say hello the way a teammate would on day "
    "one."
)


def _fallback_welcome(operator_name: str, agent_display_name: str) -> str:
    """Deterministic welcome used if OpenClaw is not reachable in time."""
    return (
        f"Hi {operator_name}, it's {agent_display_name}. I'm set up and ready "
        f"to work with you. This channel is just the two of us, so it's the "
        f"place for anything you want to think through privately. Whenever "
        f"you're ready, let's get into it."
    )


async def create_dm_as_agent(
    agent_auth_token: str, agent_user_id: str, human_username: str
) -> str:
    """
    Open the 1:1 DM between the agent and the human, created with the
    agent's OWN credentials so Rocket.Chat records it as a direct message
    (type 'd'). The webhook router treats 'd' rooms as the private-memory
    path, which is exactly what the 1:1 needs. Returns the DM room id.
    """
    async with httpx.AsyncClient(
        base_url=ROCKETCHAT_URL,
        headers={
            "X-Auth-Token": agent_auth_token,
            "X-User-Id": agent_user_id,
            "Content-Type": "application/json",
        },
        timeout=30.0,
    ) as client:
        response = await client.post(
            "/api/v1/im.create", json={"username": human_username}
        )
        response.raise_for_status()
        room = response.json()["room"]
        return room.get("_id") or room["rid"]


async def post_message_as(
    auth_token: str, user_id: str, room_id: str, text: str
) -> None:
    """Post a message to a room using explicit credentials."""
    async with httpx.AsyncClient(
        base_url=ROCKETCHAT_URL,
        headers={
            "X-Auth-Token": auth_token,
            "X-User-Id": user_id,
            "Content-Type": "application/json",
        },
        timeout=30.0,
    ) as client:
        response = await client.post(
            "/api/v1/chat.postMessage", json={"roomId": room_id, "text": text}
        )
        response.raise_for_status()


async def wait_for_openclaw_agent(agent_id: str, timeout_s: float = 150.0) -> bool:
    """
    Poll OpenClaw's model list until the given agent id is available, or
    until timeout. Returns True if it appeared.

    Stage 3d restarts the OpenClaw container so it picks up the new
    agents.list entries; the gateway takes ~30-40s to come back. Polling
    here means the welcome is generated by a live agent instead of
    failing against a restarting gateway. Connection errors during the
    restart window are expected and swallowed.
    """
    target = f"openclaw/{agent_id}"
    waited = 0.0
    interval = 4.0
    while waited < timeout_s:
        try:
            models = await openclaw.models.list()
            if any(m.id == target for m in models.data):
                return True
        except Exception:
            pass  # gateway still restarting; keep waiting
        await asyncio.sleep(interval)
        waited += interval
    return False


async def provision_welcome_dm(
    agent_id: str,
    agent_auth_token: str,
    agent_user_id: str,
    agent_display_name: str,
    human_username: str,
    operator_name: str,
) -> None:
    """
    Stage 5, run as a background task after the join response is sent.

    Creates the private DM, waits for the agent to come back online after
    the stage-3d restart, generates a welcome in the agent's voice
    (falling back to a templated message if OpenClaw does not return in
    time), and posts it into the DM. Posting the message is what surfaces
    the DM in the human's sidebar.

    The welcome is generated under the agent-keyed session (f"agent-{id}",
    the same session the webhook uses) so it is the first turn of the
    agent's one shared memory, not a throwaway.
    """
    try:
        dm_room_id = await create_dm_as_agent(
            agent_auth_token, agent_user_id, human_username
        )
        log.info("stage 5: DM created room=%s for %s", dm_room_id, human_username)
    except Exception:
        log.exception("stage 5 failed: could not create DM for %s", human_username)
        return

    ready = await wait_for_openclaw_agent(agent_id)
    welcome_text = ""
    if ready:
        try:
            welcome_text = await ask_openclaw_as(
                agent_id,
                WELCOME_GENERATION_PROMPT,
                f"agent-{agent_id}",
            )
            log.info(
                "stage 5: welcome generated (as %s): %r",
                agent_id, welcome_text,
            )
        except Exception:
            log.exception("stage 5: welcome generation failed; using fallback")
    else:
        log.warning(
            "stage 5: OpenClaw agent %s not ready in time; using fallback welcome",
            agent_id,
        )

    if not _is_real_reply(welcome_text):
        log.warning(
            "stage 5: welcome was empty or the OpenClaw placeholder; "
            "using fallback for %s", agent_id,
        )
        welcome_text = _fallback_welcome(operator_name, agent_display_name)

    try:
        await post_message_as(
            agent_auth_token, agent_user_id, dm_room_id, welcome_text
        )
        log.info(
            "stage 5 done: welcome posted to DM %s by %s",
            dm_room_id, agent_display_name,
        )
    except Exception:
        log.exception("stage 5 failed: could not post welcome to DM %s", dm_room_id)


# ============================================================
#                    join flow HTML
# ============================================================

JOIN_FORM_HTML = """<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>{{INSTANCE_NAME}} - set up your agent</title>
  <style>
    body {
      font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif;
      max-width: 560px;
      margin: 4rem auto;
      padding: 0 1.5rem;
      color: #222;
      line-height: 1.5;
    }
    .brand-logo {
      display: block;
      margin: 0 auto 2rem;
      height: 52px;
      width: auto;
    }
    h1 { font-weight: 600; margin-bottom: 0.25em; font-size: 2em; text-align: center; }
    .subtitle { color: #666; margin-bottom: 2.5rem; font-size: 1.1em; text-align: center; }
    label {
      display: block;
      font-weight: 600;
      margin-top: 1.75em;
      margin-bottom: 0.5em;
      font-size: 0.95em;
    }
    .hint { font-weight: 400; color: #888; font-size: 0.9em; margin-left: 0.5em; }
    input[type="text"], input[type="email"], textarea {
      width: 100%;
      box-sizing: border-box;
      padding: 0.7em 0.85em;
      border: 1px solid #ccc;
      border-radius: 6px;
      font-family: inherit;
      font-size: 1em;
    }
    input[type="text"]:focus, input[type="email"]:focus, textarea:focus {
      outline: none;
      border-color: #1e7add;
      box-shadow: 0 0 0 3px rgba(30, 122, 221, 0.12);
    }
    textarea { min-height: 130px; resize: vertical; }
    button {
      margin-top: 2.25rem;
      padding: 0.85em 1.7em;
      background: #1e7add;
      color: white;
      border: none;
      border-radius: 6px;
      font-size: 1em;
      font-weight: 600;
      cursor: pointer;
    }
    button:hover { background: #1864b8; }
    .info {
      background: #f3f7fc;
      padding: 1em 1.1em;
      border-radius: 6px;
      margin-top: 1.5em;
      font-size: 0.9em;
      color: #555;
    }
    .info strong { color: #333; }
  </style>
</head>
<body>
  {{BRAND_LOGO}}
  <h1>Welcome to {{INSTANCE_NAME}}</h1>
  <p class="subtitle">Set up your AI teammate in 30 seconds.</p>

  <form method="post" action="/join">
    <input type="hidden" name="invite" value="{{INVITE_VALUE}}">
    <label>Your name <span class="hint">how the agent will address you</span></label>
    <input type="text" name="name" required maxlength="100" placeholder="Marc Scibelli" autocomplete="name">

    <label>Your email <span class="hint">used to log you in; pre-filled from your invite link</span></label>
    <input type="email" name="email" required maxlength="200" placeholder="alice@example.com" autocomplete="email" value="{{EMAIL_VALUE}}">

    <label>What do you want to call your agent? <span class="hint">letters, numbers, dashes</span></label>
    <input type="text" name="agent_name" required maxlength="50" pattern="[a-zA-Z0-9._-]+" placeholder="aria">

    <label>Describe your agent's personality <span class="hint">free text, however you would describe a colleague</span></label>
    <textarea name="agent_personality" required maxlength="2000" placeholder="Direct, no fluff. Asks good questions instead of giving easy answers. Pushes back when I am being lazy. Honest when she does not know something. Does not try to be liked."></textarea>

    <button type="submit">Create my agent</button>

    <div class="info">
      <strong>What happens next:</strong> we create your Rocket.Chat
      account and your agent's identity, then bring them online. You
      will get login info and a link to the chat once everything is
      ready (about a minute).
    </div>
  </form>
  <script>
    // Prevent double-submits. The flow takes ~a minute (it restarts the
    // agent runtime mid-way); a second click used to create a duplicate
    // account and collide. Disable the button on first submit.
    (function () {
      var form = document.querySelector('form');
      form.addEventListener('submit', function (e) {
        if (form.dataset.submitted === '1') { e.preventDefault(); return; }
        form.dataset.submitted = '1';
        var btn = form.querySelector('button[type="submit"]');
        btn.disabled = true;
        btn.textContent = 'Creating your agent\\u2026 this takes about a minute';
        btn.style.opacity = '0.7';
        btn.style.cursor = 'default';
      });
    })();
  </script>
</body>
</html>
"""


JOIN_SUCCESS_HTML = """<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>{instance_name} - account created</title>
  <style>
    body {{
      font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif;
      max-width: 560px;
      margin: 4rem auto;
      padding: 0 1.5rem;
      color: #222;
      line-height: 1.5;
    }}
    .brand-logo {{ display: block; margin: 0 auto 2rem; height: 52px; width: auto; }}
    h1 {{ font-weight: 600; font-size: 1.8em; margin-bottom: 0.25em; text-align: center; }}
    .subtitle {{ color: #666; margin-bottom: 2rem; text-align: center; }}
    .credentials {{
      background: #f7f8fa;
      border: 1px solid #e6e8eb;
      border-radius: 8px;
      padding: 1.25em;
      margin: 1.5em 0;
      font-family: ui-monospace, SFMono-Regular, Menlo, monospace;
      font-size: 0.95em;
      line-height: 1.8;
    }}
    .credentials .label {{
      color: #6b7280;
      font-size: 0.8em;
      letter-spacing: 0.05em;
      text-transform: uppercase;
      font-family: -apple-system, BlinkMacSystemFont, sans-serif;
      font-weight: 600;
    }}
    .credentials .value {{ color: #1a1c20; user-select: all; }}
    .save-warning {{
      background: #fff8e1;
      border-left: 4px solid #ffc107;
      padding: 0.85em 1em;
      border-radius: 4px;
      margin: 1em 0;
      font-size: 0.92em;
      color: #6b5a1a;
    }}
    .save-warning strong {{ color: #5a4912; }}
    .btn-primary {{
      display: inline-block;
      background: #1e7add;
      color: white;
      padding: 0.85em 1.7em;
      border-radius: 6px;
      text-decoration: none;
      font-weight: 600;
      font-size: 1em;
      margin: 1.5em 0 0.5em;
    }}
    .btn-primary:hover {{ background: #1864b8; }}
    .stage-note {{
      background: #fff8e1;
      border-left: 4px solid #ffc107;
      padding: 1em 1.1em;
      border-radius: 6px;
      margin-top: 2em;
      font-size: 0.9em;
      color: #6b5a1a;
    }}
    .agent-card {{
      background: #ecf3fc;
      border: 1px solid #c8dcf3;
      border-radius: 8px;
      padding: 1em 1.25em;
      margin: 1em 0 1.5em;
      font-size: 0.95em;
    }}
    .agent-card strong {{ color: #1e7add; }}
  </style>
</head>
<body>
  {brand_logo}
  <h1>Welcome, {name}.</h1>
  <p class="subtitle">Your account is ready. Your agent {agent_display_name} is coming online.</p>

  <div class="credentials">
    <div><span class="label">Username</span><br><span class="value">{username}</span></div>
    <div style="margin-top: 0.75em;"><span class="label">Temporary password</span><br><span class="value">{password}</span></div>
  </div>

  <div class="save-warning">
    <strong>Save these now.</strong> Drop them into your password manager
    before you do anything else. You will be asked to change the password
    the first time you log in.
  </div>

  <div class="agent-card">
    Your agent is registered as <strong>@{agent_username}</strong> in the
    chat. They know your name and personality, and they carry one shared
    memory across your private channel and the team channel, so anything
    you tell them in private they can use to be useful to the team. Tell
    them if something should stay private.
  </div>

  <a class="btn-primary" href="{chat_url}">Open the chat</a>

  <div class="stage-note">
    <strong>Two notes about timing:</strong>
    <ol style="margin: 0.5em 0 0 1.2em; padding: 0;">
      <li>{agent_display_name} needs about a minute to fully come online
        (OpenClaw is restarting to load their identity). Give it 60
        seconds before you try to talk to them.</li>
      <li>{channel_note}</li>
    </ol>
  </div>
</body>
</html>
"""


def render_join_form(email_default: str = "", invite: str = "") -> str:
    return (
        JOIN_FORM_HTML
        .replace("{{EMAIL_VALUE}}", _html_escape(email_default))
        .replace("{{INVITE_VALUE}}", _html_escape(invite))
        .replace("{{INSTANCE_NAME}}", _html_escape(INSTANCE_NAME))
        .replace("{{BRAND_LOGO}}", _brand_logo_html())
    )


@app.get("/join", response_class=HTMLResponse)
def join_form(email: str | None = None, invite: str | None = None) -> str:
    """Serve the join-flow form. Requires a valid invite when INVITES_REQUIRED."""
    if INVITES_REQUIRED:
        if not invite_is_valid(invite):
            return _error_page(
                "This invite link is invalid or has expired. Ask your admin for "
                "a new one."
            )
        inv = get_invite(invite or "")
        if inv and inv.get("email") and not email:
            email = inv["email"]
    return render_join_form(email_default=email or "", invite=invite or "")


@app.post("/join", response_class=HTMLResponse)
async def join_submit(
    background_tasks: BackgroundTasks,
    name: Annotated[str, Form()],
    email: Annotated[str, Form()],
    agent_name: Annotated[str, Form()],
    agent_personality: Annotated[str, Form()],
    invite: Annotated[str, Form()] = "",
) -> str:
    """
    Receive a join-form submission and provision the user + agent end to end.
    Requires a valid, unspent invite token when INVITES_REQUIRED.
    """
    # --- invite gate ---
    if INVITES_REQUIRED and not invite_is_valid(invite):
        return _error_page(
            "This invite link is invalid or has expired. Ask your admin for a "
            "new one."
        )

    # --- input validation ---
    name = name.strip()
    if not (1 <= len(name) <= 100):
        return _error_page("Your name must be 1 to 100 characters.")

    email = email.strip().lower()
    if not EMAIL_RE.match(email) or len(email) > 200:
        return _error_page("That email address does not look valid.")

    agent_name = agent_name.strip()
    if not re.fullmatch(r"[a-zA-Z0-9._-]{1,50}", agent_name):
        return _error_page(
            "Your agent's name must be 1 to 50 characters: letters, "
            "numbers, dots, dashes, underscores only."
        )

    agent_personality = agent_personality.strip()
    if not (1 <= len(agent_personality) <= 2000):
        return _error_page("The personality description must be 1 to 2000 characters.")

    human_username = slugify_username(name)
    if not human_username:
        return _error_page(
            "Could not derive a username from your name. Try a name with at "
            "least one letter or number."
        )

    temp_password = generate_temp_password()

    log.info(
        "join submission: name=%r email=%r username=%r agent_name=%r persona_len=%d",
        name, email, human_username, agent_name, len(agent_personality),
    )

    # Track what this attempt creates so a failure partway can be rolled
    # back, instead of stranding a half-account that blocks the retry.
    created = {"human_id": "", "agent_user_id": "", "workspace_id": ""}

    async def rollback_partial(reason: str) -> None:
        log.warning("rolling back partial join for %r: %s", human_username, reason)
        if created["workspace_id"]:
            remove_agent_workspace(created["workspace_id"])
        if created["agent_user_id"]:
            await delete_rc_user_by_id(created["agent_user_id"])
        if created["human_id"]:
            await delete_rc_user_by_id(created["human_id"])

    # --- Stage 2: create human RC user ---
    try:
        human_user = await create_human_rc_user(
            name=name,
            username=human_username,
            email=email,
            password=temp_password,
        )
        log.info("stage 2 done: human RC user _id=%s", human_user.get("_id"))
        created["human_id"] = human_user.get("_id", "")
    except httpx.HTTPStatusError as e:
        err_msg = _extract_rc_error(e)
        log.exception("stage 2 failed (human user creation)")
        return _error_page(
            f"We could not create your account: {err_msg}. If you think "
            f"this is fixable on your side (typo in email, etc.), try the "
            f"form again. Otherwise reply to your invitation email."
        )
    except Exception:
        log.exception("stage 2 failed unexpectedly")
        return _error_page(
            "Something went wrong creating your account. Reply to your "
            "invitation email and we will sort it out."
        )

    # --- Stage 3a: create agent's RC bot user ---
    try:
        agent_rc = await create_agent_rc_user(
            agent_name_input=agent_name,
            operator_username=human_username,
        )
        log.info(
            "stage 3a done: agent RC bot user username=%s _id=%s",
            agent_rc["username"], agent_rc["rc_user_id"],
        )
        created["agent_user_id"] = agent_rc["rc_user_id"]
    except httpx.HTTPStatusError as e:
        err_msg = _extract_rc_error(e)
        log.exception("stage 3a failed (agent bot user creation)")
        await rollback_partial("stage 3a failed")
        return _error_page(
            f"We could not set up your agent: {err_msg}. The most common "
            f"cause is that the agent name is already taken. Please try the "
            f"form again with a different agent name."
        )
    except Exception:
        log.exception("stage 3a failed unexpectedly")
        await rollback_partial("stage 3a failed unexpectedly")
        return _error_page(
            "Something went wrong setting up your agent on our side. Please "
            "try the form again. If it keeps failing, reply to your "
            "invitation email."
        )

    # --- Stage 3b: write the OpenClaw workspace ---
    # One agent per user, with one shared memory across the private DM
    # and the team channel (see the routing comment in the webhook
    # handler). No more -private / -team split.
    agent_id = f"{human_username}-{agent_rc['username']}"

    try:
        agent_workspace = write_agent_workspace(
            agent_id=agent_id,
            agent_display_name=agent_rc["display_name"],
            persona=agent_personality,
            operator_name=name,
        )
        log.info("stage 3b done: workspace written for %s", agent_id)
        created["workspace_id"] = agent_id
    except Exception:
        log.exception("stage 3b failed")
        await rollback_partial("stage 3b failed")
        return _error_page(
            "Something went wrong setting up your agent on our side. Please "
            "try the form again. If it keeps failing, reply to your "
            "invitation email."
        )

    # --- Stage 3c: register in openclaw.json ---
    try:
        update_openclaw_agents_list([
            {"id": agent_id, "workspace": agent_workspace},
        ])
        log.info("stage 3c done: openclaw.json updated")
    except Exception:
        log.exception("stage 3c failed")
        await rollback_partial("stage 3c failed")
        return _error_page(
            "Something went wrong setting up your agent on our side. Please "
            "try the form again. If it keeps failing, reply to your "
            "invitation email."
        )

    # --- Stage 3d: make OpenClaw apply the new agent (non-fatal) ---
    # Default relies on OpenClaw's file-watch hot reload (no restart needed,
    # works for native installs); OPENCLAW_RELOAD_STRATEGY=docker-restart
    # restarts the container instead.
    try:
        await reload_openclaw()
        log.info("stage 3d done: OpenClaw reload (%s)", OPENCLAW_RELOAD_STRATEGY)
        restart_ok = True
    except Exception:
        log.exception("stage 3d failed (OpenClaw reload non-fatal)")
        restart_ok = False

    # --- Stage 3e: persist metadata to SQLite ---
    try:
        persist_agent_record({
            "human_username": human_username,
            "human_name": name,
            "human_email": email,
            "human_rc_user_id": human_user.get("_id", ""),
            "agent_name_input": agent_name,
            "agent_username": agent_rc["username"],
            "agent_display_name": agent_rc["display_name"],
            "agent_rc_user_id": agent_rc["rc_user_id"],
            "agent_rc_auth_token": agent_rc["rc_auth_token"],
            "openclaw_agent": agent_id,
            "persona": agent_personality,
        })
        log.info("stage 3e done: metadata persisted to %s", AGENTS_DB_FILE)
    except Exception:
        log.exception("stage 3e failed (metadata persistence non-fatal)")

    # --- Stage 4: team-channel membership (non-fatal) ---
    # Add both the human and their agent to the shared team channel,
    # creating that channel on the first join. The private 1:1 DM is
    # created in stage 5 alongside the welcome message, because an empty
    # DM does not surface in the human's sidebar until a message lands
    # in it.
    stage4_ok = False
    try:
        team_channel_id = await ensure_team_channel_id()
        await invite_user_to_team_channel(team_channel_id, human_user.get("_id", ""))
        await invite_user_to_team_channel(team_channel_id, agent_rc["rc_user_id"])
        # #shared-knowledge: a file inbox humans drop into; the glue ingests each
        # upload into the shared folder that every agent reads. Created on the
        # first join; only the human is added (agents read the folder, not it).
        sk_channel_id = await ensure_shared_knowledge_channel_id()
        await invite_user_to_team_channel(sk_channel_id, human_user.get("_id", ""))
        log.info(
            "stage 4 done: %s and %s added to team channel %s (%s); "
            "%s added to #%s (%s)",
            human_username, agent_rc["username"], TEAM_CHANNEL_NAME, team_channel_id,
            human_username, SHARED_KNOWLEDGE_CHANNEL, sk_channel_id,
        )
        stage4_ok = True
    except Exception:
        log.exception("stage 4 failed (team-channel membership non-fatal)")

    # --- Stage 5: private DM + welcome message (background) ---
    # Scheduled to run after this response is sent. It waits for the
    # stage-3d OpenClaw restart to finish, then creates the 1:1 DM and
    # posts a welcome message into it as the agent. Done in the
    # background so the form submitter is not held for the ~30-40s the
    # OpenClaw restart takes.
    background_tasks.add_task(
        provision_welcome_dm,
        agent_id=agent_id,
        agent_auth_token=agent_rc["rc_auth_token"],
        agent_user_id=agent_rc["rc_user_id"],
        agent_display_name=agent_rc["display_name"],
        human_username=human_username,
        operator_name=name,
    )

    if not restart_ok:
        log.warning(
            "OpenClaw restart did not succeed; agent %s will not respond "
            "until the openclaw container is restarted manually",
            agent_rc["username"],
        )

    safe_agent_display = _html_escape(agent_rc["display_name"])
    safe_agent_username = _html_escape(agent_rc["username"])
    if stage4_ok:
        team_part = (
            f"You and {safe_agent_display} are both in the "
            f"<strong>#{_html_escape(TEAM_CHANNEL_NAME)}</strong> team channel. "
            f"@-mention <strong>@{safe_agent_username}</strong> to talk to them "
            f"in the open."
        )
    else:
        team_part = (
            f"To reach {safe_agent_display} in the open, find "
            f"<strong>@{safe_agent_username}</strong> in the team channel."
        )
    channel_note = (
        f"{team_part} They are also opening a private 1:1 with you and will "
        f"leave a welcome message there within a minute, so check your direct "
        f"messages shortly."
    )

    # Provisioning succeeded: record the use (single-use links are spent here;
    # reusable team links just increment their join count).
    if INVITES_REQUIRED and invite:
        record_invite_use(invite, email)

    return JOIN_SUCCESS_HTML.format(
        name=_html_escape(name),
        username=_html_escape(human_username),
        password=_html_escape(temp_password),
        agent_display_name=safe_agent_display,
        agent_username=safe_agent_username,
        chat_url=ROCKETCHAT_PUBLIC_URL,
        channel_note=channel_note,
        instance_name=_html_escape(INSTANCE_NAME),
        brand_logo=_brand_logo_html(),
    )


def _extract_rc_error(e: httpx.HTTPStatusError) -> str:
    """Pull a human-readable error message out of a Rocket.Chat error response."""
    try:
        data = e.response.json()
        return (
            data.get("error")
            or data.get("message")
            or f"HTTP {e.response.status_code}"
        )
    except Exception:
        return f"HTTP {e.response.status_code}"


def _error_page(message: str) -> str:
    safe = _html_escape(message)
    return f"""<!DOCTYPE html>
<html>
<head><meta charset="utf-8"><title>{_html_escape(INSTANCE_NAME)} - error</title>
<style>body {{ font-family: -apple-system, BlinkMacSystemFont, sans-serif;
max-width: 560px; margin: 4rem auto; padding: 0 1.5rem; color: #222;
line-height: 1.5; }}
h1 {{ font-weight: 600; font-size: 1.6em; }}
a {{ color: #1e7add; }}
.err {{ background: #fef2f2; border-left: 4px solid #dc2626; padding: 1em;
border-radius: 4px; margin: 1.25em 0; color: #7f1d1d; }}
</style>
</head>
<body>
  <h1>Something is off with that form</h1>
  <div class="err">{safe}</div>
  <p><a href="javascript:history.back()">Back to the form</a> (your other answers will still be there).</p>
</body>
</html>"""


# ============================================================
#            invites + admin console
# ============================================================

def create_invite(email: str | None, ttl_days: int = 7, reusable: bool = False) -> str:
    token = secrets.token_urlsafe(18)
    now = int(time.time())
    # ttl_days <= 0 means no expiry (useful for a standing team link).
    expires = now + ttl_days * 86400 if ttl_days and ttl_days > 0 else None
    with sqlite3.connect(AGENTS_DB_FILE) as conn:
        conn.execute(
            "INSERT INTO invites(token,email,created_at,expires_at,reusable) "
            "VALUES(?,?,?,?,?)",
            (token, email or None, now, expires, 1 if reusable else 0),
        )
    return token


def get_invite(token: str) -> dict | None:
    with sqlite3.connect(AGENTS_DB_FILE) as conn:
        conn.row_factory = sqlite3.Row
        row = conn.execute("SELECT * FROM invites WHERE token=?", (token,)).fetchone()
        return dict(row) if row else None


def invite_is_valid(token: str | None) -> bool:
    if not token:
        return False
    inv = get_invite(token)
    if not inv:
        return False
    if inv["expires_at"] and inv["expires_at"] < int(time.time()):
        return False
    # Reusable team links are never spent; single-use links are invalid once used.
    if not inv["reusable"] and inv["used_at"]:
        return False
    return True


def record_invite_use(token: str, used_by: str) -> None:
    inv = get_invite(token)
    if not inv:
        return
    with sqlite3.connect(AGENTS_DB_FILE) as conn:
        if inv["reusable"]:
            conn.execute(
                "UPDATE invites SET uses=uses+1, used_by=? WHERE token=?",
                (used_by, token),
            )
        else:
            conn.execute(
                "UPDATE invites SET used_at=?, used_by=?, uses=uses+1 "
                "WHERE token=? AND used_at IS NULL",
                (int(time.time()), used_by, token),
            )


def list_invites() -> list[dict]:
    with sqlite3.connect(AGENTS_DB_FILE) as conn:
        conn.row_factory = sqlite3.Row
        return [dict(r) for r in conn.execute(
            "SELECT * FROM invites ORDER BY created_at DESC LIMIT 50")]


def list_people() -> list[dict]:
    with sqlite3.connect(AGENTS_DB_FILE) as conn:
        conn.row_factory = sqlite3.Row
        return [dict(r) for r in conn.execute(
            "SELECT human_name, human_username, agent_display_name, agent_username, "
            "type FROM agents ORDER BY created_at DESC LIMIT 200")]


def active_team_link() -> dict | None:
    """The current reusable team link, if one is on (not expired). At most one."""
    now = int(time.time())
    for inv in list_invites():
        if inv["reusable"] and not (inv["expires_at"] and inv["expires_at"] < now):
            return inv
    return None


# --- admin auth: HTTP Basic verified against the Rocket.Chat admin account ---
_admin_cache: dict[str, tuple[bool, float]] = {}


def _basic_creds(request: Request) -> tuple[str, str] | None:
    h = request.headers.get("authorization", "")
    if not h.startswith("Basic "):
        return None
    try:
        user, _, pw = base64.b64decode(h[6:]).decode("utf-8").partition(":")
        return user, pw
    except Exception:
        return None


async def _verify_admin(user: str, pw: str) -> bool:
    try:
        async with httpx.AsyncClient(base_url=ROCKETCHAT_URL, timeout=10.0) as c:
            r = await c.post("/api/v1/login", json={"user": user, "password": pw})
            if r.status_code != 200:
                return False
            data = r.json().get("data", {})
            uid, tok = data.get("userId"), data.get("authToken")
            if not uid or not tok:
                return False
            me = await c.get(
                "/api/v1/me", headers={"X-Auth-Token": tok, "X-User-Id": uid})
            roles = me.json().get("roles", []) if me.status_code == 200 else []
            return "admin" in roles
    except Exception:
        return False


async def require_admin(request: Request) -> bool:
    creds = _basic_creds(request)
    if not creds:
        return False
    user, pw = creds
    key = hashlib.sha256(f"{user}:{pw}".encode()).hexdigest()
    now = time.time()
    cached = _admin_cache.get(key)
    if cached and cached[1] > now:
        return cached[0]
    ok = await _verify_admin(user, pw)
    _admin_cache[key] = (ok, now + 300)
    return ok


def _admin_challenge() -> HTMLResponse:
    return HTMLResponse(
        "<h1>Admin sign-in required</h1><p>Use your Rocket.Chat admin username "
        "and password.</p>",
        status_code=401,
        headers={"WWW-Authenticate": 'Basic realm="admin console"'},
    )


async def _stack_health() -> dict:
    health = {"glue": True, "rocketchat": False, "openclaw": False}
    try:
        async with httpx.AsyncClient(timeout=5.0) as c:
            r = await c.get(f"{ROCKETCHAT_URL}/api/info")
            health["rocketchat"] = r.status_code == 200 and r.json().get("success", False)
    except Exception:
        pass
    try:
        async with httpx.AsyncClient(timeout=5.0) as c:
            r = await c.get(f"{OPENCLAW_URL}/healthz")
            health["openclaw"] = r.status_code == 200
    except Exception:
        pass
    return health


def _reach_note() -> str:
    return {
        "tailscale": "Teammates must join your Tailscale tailnet, then open the link.",
        "lan": "Teammates must be on the same network to open the link.",
        "public": "Teammates can open the link from anywhere.",
    }.get(INGRESS_PROFILE, "Loopback only: reach it via an SSH tunnel (scripts/tunnel.sh).")


ADMIN_HTML = """<!DOCTYPE html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{{INSTANCE}} - admin</title>
<style>
body{font-family:-apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,sans-serif;max-width:760px;margin:2.5rem auto;padding:0 1.5rem;color:#222;line-height:1.5}
h1{font-size:1.6em;font-weight:600;margin:0}
h2{font-size:1.1em;font-weight:600;margin:1.75rem 0 .5rem}
.sub{color:#666;margin:.25rem 0 0}
.card{border:1px solid #e6e8eb;border-radius:10px;padding:1rem 1.25rem;margin-top:1rem}
table{width:100%;border-collapse:collapse;font-size:13px}
th{text-align:left;color:#888;font-weight:600;font-size:12px;padding:6px 8px;border-bottom:1px solid #eee}
td{border-bottom:1px solid #f1f1f1;vertical-align:top}
input,button,select{font-family:inherit;font-size:14px}
input[type=text],input[type=email]{padding:.55em .7em;border:1px solid #ccc;border-radius:6px}
button{padding:.6em 1.2em;background:#1e7add;color:#fff;border:0;border-radius:6px;font-weight:600;cursor:pointer}
.pill{display:inline-block;padding:4px 10px;border-radius:6px;font-size:13px;margin-right:6px}
.note{background:#f3f7fc;border-radius:6px;padding:.6em .8em;font-size:13px;color:#555;margin-top:.5rem}
</style></head><body>
<h1>{{INSTANCE}}</h1><p class="sub">admin console</p>
<div style="margin-top:1rem">{{HEALTH}}</div>
<div class="card">
  <h2 style="margin-top:0">Team link</h2>
  <p class="sub" style="margin:0 0 .75rem">One shared link anyone who can reach this server can join with. Turn it off to require single-use invites only.</p>
  {{TEAMLINK}}
  <div class="note">{{REACH}}</div>
</div>
<div class="card">
  <h2 style="margin-top:0">Invite specific people</h2>
  <p class="sub" style="margin:0 0 .75rem">One-time links, spent after a single join.</p>
  <form method="post" action="/admin/invites" style="display:flex;gap:8px;flex-wrap:wrap;align-items:center">
    <input type="email" name="email" placeholder="their email (optional)" style="flex:1;min-width:200px">
    <input type="text" name="ttl_days" value="7" style="width:64px" title="days valid" aria-label="days valid">
    <button type="submit">Generate invite link</button>
  </form>
  <table style="margin-top:1rem"><thead><tr><th>email</th><th>status</th><th>link</th><th></th></tr></thead><tbody>{{INVITES}}</tbody></table>
</div>
<div class="card">
  <h2 style="margin-top:0">People &amp; agents</h2>
  <table><thead><tr><th>person</th><th>agent</th><th>type</th></tr></thead><tbody>{{PEOPLE}}</tbody></table>
</div>
<div class="card">
  <h2 style="margin-top:0">Maintain the chat (Rocket.Chat)</h2>
  <p class="sub" style="margin:0 0 .6rem">This console manages your team (invites, people, agents). The chat itself runs on Rocket.Chat, which has its own admin area for accounts and for how the interface looks.</p>
  <p style="margin:.2rem 0 .35rem"><a href="{{RC_ADMIN_URL}}" target="_blank" rel="noopener">Open Rocket.Chat Administration &rarr;</a></p>
  <p class="sub" style="margin:0 0 .6rem">Sign in with the same admin account you used for this console: username <code>{{ADMIN_USERNAME}}</code> and the password you set during setup.</p>
  <div class="note">
    Once in <strong>Administration</strong>:<br>
    &bull; Add or remove people and change roles: <strong>Users</strong><br>
    &bull; Replace or remove logos and the favicon (including the Rocket.Chat logo): <strong>Settings &rarr; Assets</strong><br>
    &bull; Colors, layout, and custom CSS: <strong>Settings &rarr; Layout</strong>
  </div>
</div>
</body></html>"""


def render_admin(health: dict, invites: list[dict], people: list[dict]) -> str:
    def hp(name, ok):
        bg, fg, mark = ("#e1f5ee", "#0f6e56", "ok") if ok else ("#fcebeb", "#a32d2d", "down")
        return f'<span class="pill" style="background:{bg};color:{fg}">{name}: {mark}</span>'
    health_html = hp("Rocket.Chat", health["rocketchat"]) + hp("OpenClaw", health["openclaw"]) + hp("Glue", health["glue"])

    now = int(time.time())

    # --- team link block (one toggleable reusable link) ---
    team = next((i for i in invites if i["reusable"]
                 and not (i["expires_at"] and i["expires_at"] < now)), None)
    if team:
        url = _html_escape(f"{JOIN_PUBLIC_URL}/join?invite={team['token']}")
        team_html = (
            f'<p style="margin:.25rem 0 .6rem">Status: <strong style="color:#0f6e56">on</strong>'
            f' &middot; {team["uses"]} joined</p>'
            f'<input type="text" readonly value="{url}" onclick="this.select()" '
            'style="width:100%;font-family:ui-monospace,monospace;font-size:13px;'
            'border:1px solid #ddd;border-radius:6px;padding:8px;box-sizing:border-box">'
            '<form method="post" action="/admin/team-link/off" style="margin-top:.6rem">'
            '<button type="submit" style="background:#fff;color:#a32d2d;border:1px solid #e6c2c2">'
            'Turn off team link</button></form>'
        )
    else:
        team_html = (
            '<p style="margin:.25rem 0 .6rem">Status: <strong style="color:#888">off</strong>'
            ' &mdash; only single-use invites work right now.</p>'
            '<form method="post" action="/admin/team-link/on">'
            '<button type="submit">Create team link</button></form>'
        )

    # --- single-use invites only (the team link lives in its own section) ---
    inv_rows = ""
    for inv in invites:
        if inv["reusable"]:
            continue
        email = _html_escape(inv["email"] or "-")
        expired = bool(inv["expires_at"]) and inv["expires_at"] < now
        if inv["used_at"]:
            who = f' by {_html_escape(inv["used_by"])}' if inv["used_by"] else ""
            status, link = f'<span style="color:#0f6e56">used{who}</span>', "-"
        elif expired:
            status, link = '<span style="color:#a32d2d">expired</span>', "-"
        else:
            days = int((inv["expires_at"] - now) / 86400) if inv["expires_at"] else 0
            status = f'<span style="color:#185fa5">active &middot; {days}d left</span>'
            url = _html_escape(f"{JOIN_PUBLIC_URL}/join?invite={inv['token']}")
            link = (
                f'<input type="text" readonly value="{url}" onclick="this.select()" '
                'style="width:100%;font-family:ui-monospace,monospace;font-size:12px;'
                'border:1px solid #ddd;border-radius:4px;padding:4px">'
            )
        revoke = (
            '<form method="post" action="/admin/invites/revoke" style="margin:0">'
            f'<input type="hidden" name="token" value="{_html_escape(inv["token"])}">'
            '<button type="submit" style="background:#fff;color:#a32d2d;'
            'border:1px solid #eee;padding:3px 8px;font-size:12px;font-weight:400">'
            'revoke</button></form>'
        )
        inv_rows += (
            f'<tr><td style="padding:6px 8px">{email}</td>'
            f'<td style="padding:6px 8px">{status}</td>'
            f'<td style="padding:6px 8px">{link}</td>'
            f'<td style="padding:6px 8px">{revoke}</td></tr>'
        )
    if not inv_rows:
        inv_rows = '<tr><td colspan="4" style="padding:10px 8px;color:#888">No single-use invites yet.</td></tr>'

    ppl_rows = ""
    for p in people:
        ppl_rows += (
            f'<tr><td style="padding:6px 8px">{_html_escape(p["human_name"])} '
            f'<span style="color:#999">@{_html_escape(p["human_username"])}</span></td>'
            f'<td style="padding:6px 8px">{_html_escape(p["agent_display_name"])} '
            f'<span style="color:#999">@{_html_escape(p["agent_username"])}</span></td>'
            f'<td style="padding:6px 8px;color:#999">{_html_escape(p["type"])}</td></tr>'
        )
    if not ppl_rows:
        ppl_rows = '<tr><td colspan="3" style="padding:10px 8px;color:#888">No one has joined yet.</td></tr>'

    return (ADMIN_HTML
            .replace("{{INSTANCE}}", _html_escape(INSTANCE_NAME))
            .replace("{{HEALTH}}", health_html)
            .replace("{{TEAMLINK}}", team_html)
            .replace("{{REACH}}", _html_escape(_reach_note()))
            .replace("{{INVITES}}", inv_rows)
            .replace("{{PEOPLE}}", ppl_rows)
            .replace("{{RC_ADMIN_URL}}",
                     _html_escape(ROCKETCHAT_PUBLIC_URL.rstrip("/") + "/admin"))
            .replace("{{ADMIN_USERNAME}}", _html_escape(ADMIN_USERNAME)))


@app.get("/admin", response_class=HTMLResponse)
async def admin_console(request: Request):
    if not await require_admin(request):
        return _admin_challenge()
    health = await _stack_health()
    return HTMLResponse(render_admin(health, list_invites(), list_people()))


@app.post("/admin/invites")
async def admin_create_invite(
    request: Request,
    email: Annotated[str, Form()] = "",
    ttl_days: Annotated[int, Form()] = 7,
):
    """Create a single-use invite for one person."""
    if not await require_admin(request):
        return _admin_challenge()
    create_invite(email.strip().lower() or None, ttl_days, reusable=False)
    return RedirectResponse("/admin", status_code=303)


@app.post("/admin/team-link/on")
async def admin_team_link_on(request: Request):
    """Turn on the shared team link (reusable, no expiry). At most one active."""
    if not await require_admin(request):
        return _admin_challenge()
    if not active_team_link():
        create_invite(None, ttl_days=0, reusable=True)
    return RedirectResponse("/admin", status_code=303)


@app.post("/admin/team-link/off")
async def admin_team_link_off(request: Request):
    """Turn off (delete) the shared team link so only single-use invites work."""
    if not await require_admin(request):
        return _admin_challenge()
    with sqlite3.connect(AGENTS_DB_FILE) as conn:
        conn.execute("DELETE FROM invites WHERE reusable=1")
    return RedirectResponse("/admin", status_code=303)


@app.post("/admin/invites/revoke")
async def admin_revoke_invite(
    request: Request,
    token: Annotated[str, Form()],
):
    if not await require_admin(request):
        return _admin_challenge()
    with sqlite3.connect(AGENTS_DB_FILE) as conn:
        conn.execute("DELETE FROM invites WHERE token=?", (token,))
    return RedirectResponse("/admin", status_code=303)
