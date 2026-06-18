"""Register / remove EXTERNAL A2A agents.

Shared by the glue admin console (glue/app.py) and the CLI dev tool
(scripts/setup_a2a_agent.py), so there is ONE code path. Deliberately has no
FastAPI / app imports and no module-level side effects: it is plain httpx +
sqlite, and every dependency (RC URL, admin creds, db path) is passed in.

An external A2A agent is stored as an `agents` row of type 'a2a' carrying the
Agent Card URL and an optional bearer token. A Rocket.Chat bot user is created
so the agent has a presence to post as, and it is invited into the team channel.
After registration, @mentioning the bot routes to generate_a2a_reply(), which
calls the external agent over the A2A protocol (see glue/a2a_client.py).
"""
from __future__ import annotations

import re
import secrets
import sqlite3

import httpx


def slug_username(name: str) -> str:
    """A safe Rocket.Chat username: letters, numbers, dot, dash, underscore."""
    s = re.sub(r"[^a-zA-Z0-9._-]+", "-", (name or "").strip().lower())
    return s.strip("-._")


def fetch_agent_card(card_url: str, bearer_token: str | None, timeout: float = 15.0) -> dict:
    """GET the agent's card and return the parsed JSON. Raises ValueError on any
    problem so callers can surface a clear message."""
    base = (card_url or "").strip().rstrip("/")
    url = base + "/.well-known/agent-card.json"
    headers = {"Authorization": f"Bearer {bearer_token}"} if bearer_token else {}
    try:
        r = httpx.get(url, headers=headers, timeout=timeout, follow_redirects=True)
    except Exception as exc:
        raise ValueError(f"could not reach the agent card at {url}: {exc}")
    if r.status_code != 200:
        raise ValueError(f"the agent card at {url} returned HTTP {r.status_code}")
    try:
        return r.json()
    except Exception:
        raise ValueError(f"the agent card at {url} was not valid JSON")


def register_external_a2a_agent(
    *, card_url: str, bearer_token: str | None, name: str | None,
    channel_name: str, rc_url: str, admin_user_id: str, admin_pat: str, db_path: str,
) -> dict:
    """Create (or update) an external A2A agent and add it to the team channel.

    If `name` is empty, it is read from the agent's card. Idempotent: an existing
    A2A agent with the same derived username has its card URL + bearer updated and
    is re-invited. Returns a small result dict; raises ValueError with a
    human-readable message on any failure.
    """
    card_url = (card_url or "").strip()
    if not card_url:
        raise ValueError("an agent card URL is required")
    bearer_token = (bearer_token or "").strip() or None
    if not admin_user_id or not admin_pat:
        raise ValueError("admin credentials are not configured")

    display_name = (name or "").strip()
    if not display_name:
        # No name given: pull it from the agent's own card (also validates reach).
        card = fetch_agent_card(card_url, bearer_token)
        display_name = (card.get("name") or "").strip()
    if not display_name:
        raise ValueError("no name was given and the agent card has no 'name' field")

    username = slug_username(display_name)
    if not username:
        raise ValueError(f"could not derive a username from {display_name!r}")

    headers = {
        "X-Auth-Token": admin_pat, "X-User-Id": admin_user_id,
        "Content-Type": "application/json",
    }
    db = sqlite3.connect(db_path)
    db.row_factory = sqlite3.Row
    try:
        existing = db.execute(
            "SELECT * FROM agents WHERE LOWER(agent_username)=LOWER(?)", (username,)
        ).fetchone()
        if existing and existing["type"] != "a2a":
            raise ValueError(
                f"@{username} already exists as a non-external agent; pick another name"
            )

        with httpx.Client(base_url=rc_url, headers=headers, timeout=30.0) as c:
            info = c.get("/api/v1/channels.info", params={"roomName": channel_name})
            if info.status_code != 200 or not info.json().get("success"):
                raise ValueError(f"channel #{channel_name} not found")
            room_id = info.json()["channel"]["_id"]

            if existing:
                rc_user_id = existing["agent_rc_user_id"]
                db.execute(
                    "UPDATE agents SET a2a_card_url=?, a2a_bearer_token=?, "
                    "agent_display_name=?, agent_name_input=? WHERE id=?",
                    (card_url, bearer_token, display_name, display_name, existing["id"]),
                )
                db.commit()
                action = "updated"
            else:
                password = secrets.token_urlsafe(24)
                email = f"{username}+{secrets.token_hex(3)}@agentnetwork.local"
                r = c.post(
                    "/api/v1/users.create",
                    json={
                        "name": display_name, "username": username, "email": email,
                        "password": password, "verified": True,
                        "requirePasswordChange": False, "sendWelcomeEmail": False,
                        "roles": ["user"],
                    },
                )
                if r.status_code != 200 or not r.json().get("success"):
                    raise ValueError(
                        f"could not create the agent's chat user: {r.text[:200]}"
                    )
                rc_user_id = r.json()["user"]["_id"]

                with httpx.Client(base_url=rc_url, timeout=30.0) as lc:
                    lr = lc.post(
                        "/api/v1/login", json={"user": username, "password": password}
                    )
                if lr.status_code != 200 or not lr.json().get("success"):
                    raise ValueError("could not log in as the new agent user")
                token = lr.json()["data"]["authToken"]

                db.execute(
                    """INSERT INTO agents
                       (human_username, human_name, human_email, human_rc_user_id,
                        agent_name_input, agent_username, agent_display_name,
                        agent_rc_user_id, agent_rc_auth_token, openclaw_agent, persona,
                        type, a2a_card_url, a2a_bearer_token)
                       VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                    (
                        f"a2a-{username}", "", "", "",
                        display_name, username, display_name,
                        rc_user_id, token, "", "",
                        "a2a", card_url, bearer_token,
                    ),
                )
                db.commit()
                action = "created"

            inv = c.post(
                "/api/v1/channels.invite", json={"roomId": room_id, "userId": rc_user_id}
            )
            invited = inv.status_code == 200 and inv.json().get("success", False)
    finally:
        db.close()

    return {
        "username": username, "display_name": display_name,
        "action": action, "channel": channel_name, "invited": bool(invited),
    }


def remove_external_a2a_agent(
    *, username: str, rc_url: str, admin_user_id: str, admin_pat: str, db_path: str,
) -> dict:
    """Remove an external A2A agent: delete its bot user (which also removes it
    from every channel) and drop its agents row. Only acts on type='a2a' rows."""
    username = (username or "").strip().lstrip("@")
    if not username:
        raise ValueError("a username is required")
    if not admin_user_id or not admin_pat:
        raise ValueError("admin credentials are not configured")

    headers = {
        "X-Auth-Token": admin_pat, "X-User-Id": admin_user_id,
        "Content-Type": "application/json",
    }
    db = sqlite3.connect(db_path)
    db.row_factory = sqlite3.Row
    try:
        row = db.execute(
            "SELECT * FROM agents WHERE LOWER(agent_username)=LOWER(?) AND type='a2a'",
            (username,),
        ).fetchone()
        if not row:
            raise ValueError(f"no external agent @{username} found")
        rc_user_id = row["agent_rc_user_id"]
        with httpx.Client(base_url=rc_url, headers=headers, timeout=30.0) as c:
            # Deleting the bot user also removes it from all channels.
            c.post("/api/v1/users.delete", json={"userId": rc_user_id})
        db.execute("DELETE FROM agents WHERE id=?", (row["id"],))
        db.commit()
    finally:
        db.close()
    return {"username": username}
