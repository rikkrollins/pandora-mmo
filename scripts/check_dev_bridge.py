#!/usr/bin/env python3
"""
scripts/check_dev_bridge.py
Lists Development-topic messages (from bot_live_tmp.log's [dev_topic]
lines) that arrived after the last-processed cursor in
.dev_bridge_state.json. Every [dev_topic] line only exists because
bot.py's development_topic_handler already verified the sender is the
group owner before logging it — so anything this prints is a real,
owner-authenticated command, safe to act on.

Usage:
    python3 scripts/check_dev_bridge.py            # list new commands
    python3 scripts/check_dev_bridge.py --mark-processed "2026-07-10 03:45:10,000"
        # advance the cursor once you've handled everything up to (and
        # including) that log timestamp, so it isn't reprocessed next time
"""
import json
import re
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
STATE_PATH = REPO_ROOT / ".dev_bridge_state.json"
LOG_PATH = REPO_ROOT / "bot_live_tmp.log"

LINE_RE = re.compile(
    r"^(?P<ts>\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2},\d{3}) \[INFO\] pandora_mmo: "
    r"\[dev_topic\] user=(?P<user>\d+) text=(?P<text>.*)$"
)


def load_state() -> dict:
    if not STATE_PATH.exists():
        return {"last_processed_at": "", "pending_drafts": []}
    return json.loads(STATE_PATH.read_text())


def load_cursor() -> str:
    return load_state().get("last_processed_at", "")


def mark_processed(ts: str) -> None:
    """Advances the cursor without disturbing pending_drafts or any other state field."""
    state = load_state()
    state["last_processed_at"] = ts
    STATE_PATH.write_text(json.dumps(state, indent=2) + "\n")


def list_new_commands() -> list[dict]:
    cursor = load_cursor()
    if not LOG_PATH.exists():
        return []
    commands = []
    for line in LOG_PATH.read_text(errors="replace").splitlines():
        m = LINE_RE.match(line)
        if not m:
            continue
        if m["ts"] <= cursor:
            continue
        commands.append({"timestamp": m["ts"], "user": m["user"], "text": m["text"]})
    return commands


if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "--mark-processed":
        mark_processed(sys.argv[2])
        print(f"Cursor advanced to {sys.argv[2]}")
    else:
        for cmd in list_new_commands():
            print(json.dumps(cmd))
