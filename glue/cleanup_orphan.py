"""
Targeted cleanup of one user's/agent's leftover artifacts after a join
partially failed.

When the join flow errors partway (or the form is double-submitted), it
can leave orphans behind: a Rocket.Chat human user, a Rocket.Chat agent
bot user, an OpenClaw workspace + openclaw.json entry, and sometimes a
glue database row. Those orphans block a clean retry (the username is
"already in use") and, if a stale DB row survives, leave the agent unable
to respond.

This removes everything matching a search TERM across all stores:
  - Rocket.Chat users whose username contains the term
  - OpenClaw workspaces whose directory name contains the term
  - openclaw.json agents.list entries whose id contains the term
  - glue SQLite rows where a name/agent column contains the term

It is a DRY RUN by default (lists what it would remove). Pass --apply to
actually delete. Reuses app.py's config + helpers.

Run via the host wrapper:
  bash ~/youragentnetwork/scripts/cleanup-orphan-agent.sh srishti          # preview
  bash ~/youragentnetwork/scripts/cleanup-orphan-agent.sh srishti --yes    # apply

Directly inside the glue container:
  python /app/cleanup_orphan.py srishti           # dry run
  python /app/cleanup_orphan.py srishti --apply   # apply

CAUTION: the term is matched as a substring. Pick something specific
(a username fragment like "srishti" or "clem"), review the dry-run list,
and make sure it does not match accounts you want to keep (e.g. "marc").
"""
import asyncio
import json
import shutil
import sqlite3
import sys

import httpx

import app


def _matches(haystack: str, terms: list) -> bool:
    """True if ANY term is a substring of haystack (case-insensitive)."""
    h = haystack.lower()
    return any(t in h for t in terms)


def matching_rows(terms: list) -> list[dict]:
    try:
        with sqlite3.connect(app.AGENTS_DB_FILE) as conn:
            conn.row_factory = sqlite3.Row
            rows = [dict(r) for r in conn.execute("SELECT * FROM agents")]
    except sqlite3.OperationalError:
        return []
    cols = ("human_username", "agent_username", "openclaw_agent",
            "human_name", "agent_name_input", "human_email")
    return [r for r in rows
            if _matches(" ".join(str(r.get(c, "")) for c in cols), terms)]


def matching_workspaces(terms: list) -> list:
    if not app.OPENCLAW_WORKSPACES_DIR.exists():
        return []
    return [p for p in app.OPENCLAW_WORKSPACES_DIR.iterdir()
            if p.is_dir() and _matches(p.name, terms)]


def matching_openclaw_entries(terms: list) -> list[dict]:
    if not app.OPENCLAW_CONFIG_FILE.exists():
        return []
    with app.OPENCLAW_CONFIG_FILE.open() as f:
        cfg = json.load(f)
    return [a for a in cfg.get("agents", {}).get("list", [])
            if _matches(str(a.get("id", "")), terms)]


def remove_openclaw_entries(ids_to_remove: set) -> None:
    with app.OPENCLAW_CONFIG_FILE.open() as f:
        cfg = json.load(f)
    lst = cfg.get("agents", {}).get("list", [])
    cfg.setdefault("agents", {})["list"] = [
        a for a in lst if a.get("id") not in ids_to_remove
    ]
    tmp = app.OPENCLAW_CONFIG_FILE.with_suffix(".json.tmp")
    with tmp.open("w") as f:
        json.dump(cfg, f, indent=2)
        f.write("\n")
    tmp.replace(app.OPENCLAW_CONFIG_FILE)


def delete_rows(rows: list[dict]) -> None:
    with sqlite3.connect(app.AGENTS_DB_FILE) as conn:
        for r in rows:
            conn.execute("DELETE FROM agents WHERE id = ?", (r["id"],))


async def find_rc_users(client: httpx.AsyncClient, terms: list) -> list[dict]:
    resp = await client.get("/api/v1/users.list", params={"count": 0})
    if resp.status_code != 200:
        print(f"    WARN users.list returned HTTP {resp.status_code}")
        return []
    users = resp.json().get("users", [])
    return [u for u in users if _matches(u.get("username", "") or "", terms)]


async def main(terms: list, apply: bool) -> None:
    print(f"Search terms: {terms}\n")

    rows = matching_rows(terms)
    wss = matching_workspaces(terms)
    entries = matching_openclaw_entries(terms)

    if not (app.ADMIN_USER_ID and app.ADMIN_PAT):
        print("ERROR: ADMIN_USER_ID / ADMIN_PAT not set; cannot reach Rocket.Chat.")
        sys.exit(1)

    async with httpx.AsyncClient(
        base_url=app.ROCKETCHAT_URL,
        headers={
            "X-Auth-Token": app.ADMIN_PAT,
            "X-User-Id": app.ADMIN_USER_ID,
            "Content-Type": "application/json",
        },
        timeout=30.0,
    ) as client:
        rc_users = await find_rc_users(client, terms)

        print(f"Rocket.Chat users:     {[(u.get('username'), u.get('_id')) for u in rc_users]}")
        print(f"OpenClaw workspaces:   {[p.name for p in wss]}")
        print(f"openclaw.json entries: {[e.get('id') for e in entries]}")
        print(f"SQLite rows:           {[(r.get('human_username'), r.get('agent_username')) for r in rows]}")

        if not apply:
            print("\nDRY RUN. Nothing deleted.")
            print("Review the Rocket.Chat users list above carefully, then re-run")
            print("with --apply to remove everything listed.")
            return

        print("\nApplying cleanup...")
        for u in rc_users:
            resp = await client.post(
                "/api/v1/users.delete",
                json={"userId": u["_id"], "confirmRelinquish": True},
            )
            ok = resp.status_code == 200 and resp.json().get("success")
            print(f"    {'deleted' if ok else 'FAILED'} RC user "
                  f"{u.get('username')} ({u['_id']})"
                  + ("" if ok else f": {resp.text[:160]}"))

    for p in wss:
        shutil.rmtree(p, ignore_errors=True)
        print(f"    removed workspace {p}")

    if entries:
        remove_openclaw_entries({e.get("id") for e in entries})
        print(f"    removed {len(entries)} openclaw.json entr"
              + ("y" if len(entries) == 1 else "ies"))

    if rows:
        delete_rows(rows)
        print(f"    removed {len(rows)} SQLite row" + ("" if len(rows) == 1 else "s"))

    try:
        await app.restart_openclaw_container()
        print("    triggered OpenClaw restart")
    except Exception as e:
        print(f"    WARN could not restart OpenClaw: {e}")

    print("\nCleanup complete.")


if __name__ == "__main__":
    args = sys.argv[1:]
    apply = "--apply" in args or "--yes" in args
    terms = [a.lower() for a in args if not a.startswith("--")]
    if not terms:
        print("Usage: python cleanup_orphan.py <term> [<term>...] [--apply]")
        sys.exit(1)
    asyncio.run(main(terms, apply))
