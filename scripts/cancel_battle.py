"""
Ops tool, not a player-facing feature (2026-08-19, Coffee: "we dont need
an in-game cancel the battle command, jus make it so if i ask u here u
can cancel it"). The live bot process holds combat state in its own
process memory (sessions._ACTIVE_SESSIONS) -- a separate script's
process can't reach into that directly. The only safe way to end a live
session from outside the running process is to remove it from
sessions_snapshot.json (matching sessions.py's own save_snapshot()
tmp-file + os.replace pattern, so a concurrent live save can't produce
a half-written file) and then restart the bot service, since
_on_startup() unconditionally calls sessions.load_snapshot() on every
boot and will simply no longer find the removed session.

Usage: python3 scripts/cancel_battle.py [session_id]
  No args: cancels the session if there is exactly one active session
  (refuses if there's more than one, to avoid guessing which fight).
  With a session_id: cancels that specific one.
After running this, redeploy (systemctl --user restart
pandora-mmo-bot.service) for it to take effect.
"""
import json
import os
import sys

SNAPSHOT_PATH = "sessions_snapshot.json"


def main() -> None:
    if not os.path.exists(SNAPSHOT_PATH):
        print("No sessions_snapshot.json -- nothing to cancel.")
        return

    with open(SNAPSHOT_PATH) as f:
        data = json.load(f)

    sessions_list = data.get("sessions", [])
    if not sessions_list:
        print("No active sessions in the snapshot.")
        return

    if len(sys.argv) > 1:
        target_id = int(sys.argv[1])
    elif len(sessions_list) == 1:
        target_id = sessions_list[0]["session_id"]
    else:
        print("Multiple active sessions -- pass a session_id explicitly:")
        for s in sessions_list:
            names = [p.get("name") for p in s.get("participants", [])]
            print(f"  session_id={s['session_id']} chat_id={s['chat_id']} participants={names}")
        return

    remaining = [s for s in sessions_list if s["session_id"] != target_id]
    if len(remaining) == len(sessions_list):
        print(f"No session with session_id={target_id} found.")
        return

    cancelled = next(s for s in sessions_list if s["session_id"] == target_id)
    names = [p.get("name") for p in cancelled.get("participants", [])]
    data["sessions"] = remaining

    tmp_path = SNAPSHOT_PATH + ".tmp"
    with open(tmp_path, "w") as f:
        json.dump(data, f)
    os.replace(tmp_path, SNAPSHOT_PATH)

    print(f"Cancelled session_id={target_id} chat_id={cancelled['chat_id']} participants={names}")
    print("Now restart the bot service for this to take effect:")
    print("  systemctl --user restart pandora-mmo-bot.service")


if __name__ == "__main__":
    main()
