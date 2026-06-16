"""
Reset all provisioned users/agents back to a clean slate.

Pre-launch utility. Deletes everything the join flow created so the
first real joiner sees a clean system:

  - the human Rocket.Chat user accounts
  - the agent Rocket.Chat bot users
  - the OpenClaw workspaces on disk
  - the agents.list entries in openclaw.json
  - the glue's agents table (dropped, so it is recreated empty AND with
    the current schema the next time glue starts)

It reuses app.py's config and helpers, so it talks to the same
Rocket.Chat, OpenClaw config, and workspace paths the join flow uses.
It reads each record with SELECT *, so it works whether the table is on
the old two-agent schema or the new one-agent schema.

Run it via the host wrapper (recommended):
    bash ~/youragentnetwork/scripts/reset-provisioned-agents.sh         # dry run
    bash ~/youragentnetwork/scripts/reset-provisioned-agents.sh --yes   # apply

Directly inside the glue container it is:
    python /app/reset.py            # dry run, lists what would go
    python /app/reset.py --apply    # actually delete
"""
import asyncio
import json
import shutil
import sqlite3
import sys

import httpx

import app


def load_rows() -> list[dict]:
    try:
        with sqlite3.connect(app.AGENTS_DB_FILE) as conn:
            conn.row_factory = sqlite3.Row
            return [dict(r) for r in conn.execute("SELECT * FROM agents")]
    except sqlite3.OperationalError:
        # Table does not exist yet (already clean).
        return []


def openclaw_ids(row: dict) -> list[str]:
    """All OpenClaw agent ids on a row, across old and new schema."""
    ids = []
    for key in ("openclaw_agent", "openclaw_agent_private", "openclaw_agent_team"):
        val = row.get(key)
        if val:
            ids.append(val)
    return ids


async def delete_rc_user(client: httpx.AsyncClient, user_id: str, label: str) -> None:
    if not user_id:
        return
    try:
        resp = await client.post(
            "/api/v1/users.delete",
            json={"userId": user_id, "confirmRelinquish": True},
        )
        if resp.status_code == 200 and resp.json().get("success"):
            print(f"    deleted RC user {label} ({user_id})")
        else:
            print(
                f"    WARN could not delete RC user {label} ({user_id}): "
                f"HTTP {resp.status_code} {resp.text[:200]}"
            )
    except Exception as e:
        print(f"    WARN error deleting RC user {label} ({user_id}): {e}")


def remove_openclaw_entries(ids_to_remove: set[str]) -> None:
    cfg_path = app.OPENCLAW_CONFIG_FILE
    if not cfg_path.exists():
        print("    openclaw.json not found; skipping config cleanup")
        return
    with cfg_path.open() as f:
        cfg = json.load(f)
    agents = cfg.get("agents", {})
    lst = agents.get("list", [])
    before = len(lst)
    agents["list"] = [a for a in lst if a.get("id") not in ids_to_remove]
    removed = before - len(agents["list"])
    tmp = cfg_path.with_suffix(".json.tmp")
    with tmp.open("w") as f:
        json.dump(cfg, f, indent=2)
        f.write("\n")
    tmp.replace(cfg_path)
    print(f"    removed {removed} openclaw.json agent entr" + ("y" if removed == 1 else "ies"))


def remove_workspaces(ids_to_remove: set[str]) -> None:
    for aid in sorted(ids_to_remove):
        path = app.OPENCLAW_WORKSPACES_DIR / aid
        if path.exists():
            shutil.rmtree(path, ignore_errors=True)
            print(f"    removed workspace {path}")


def drop_agents_table() -> None:
    with sqlite3.connect(app.AGENTS_DB_FILE) as conn:
        conn.execute("DROP TABLE IF EXISTS agents")
    print("    dropped agents table (recreated empty on next glue start)")


async def main(apply: bool) -> None:
    rows = load_rows()
    print(f"Found {len(rows)} provisioned record(s).")
    all_ids: set[str] = set()
    for r in rows:
        ids = openclaw_ids(r)
        all_ids.update(ids)
        print(
            f"  - human={r.get('human_username')!r} "
            f"agent=@{r.get('agent_username')} openclaw={ids}"
        )

    if not apply:
        print()
        print("DRY RUN. Nothing deleted. Re-run with --yes to actually reset.")
        return

    if not (app.ADMIN_USER_ID and app.ADMIN_PAT):
        print()
        print("ERROR: ADMIN_USER_ID / ADMIN_PAT not set; cannot delete RC users.")
        sys.exit(1)

    print()
    print("Applying reset...")
    async with httpx.AsyncClient(
        base_url=app.ROCKETCHAT_URL,
        headers={
            "X-Auth-Token": app.ADMIN_PAT,
            "X-User-Id": app.ADMIN_USER_ID,
            "Content-Type": "application/json",
        },
        timeout=30.0,
    ) as client:
        for r in rows:
            await delete_rc_user(
                client, r.get("agent_rc_user_id", ""),
                f"agent @{r.get('agent_username')}",
            )
            await delete_rc_user(
                client, r.get("human_rc_user_id", ""),
                f"human {r.get('human_username')}",
            )

    remove_openclaw_entries(all_ids)
    remove_workspaces(all_ids)
    drop_agents_table()

    try:
        await app.restart_openclaw_container()
        print("    triggered OpenClaw restart")
    except Exception as e:
        print(f"    WARN could not restart OpenClaw: {e}")

    print()
    print("Reset complete.")


if __name__ == "__main__":
    apply = "--apply" in sys.argv or "--yes" in sys.argv
    asyncio.run(main(apply))
