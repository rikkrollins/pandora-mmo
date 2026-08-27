#!/usr/bin/env python3
"""
scripts/check_topic_activity.py
Lists real player activity in the Adventure topic ([intent] log lines,
logged for every message adventure_master_handler processes) and the
Support topic ([support] log lines) that arrived after the last-
processed cursor in .topic_monitor_state.json. Unlike
check_dev_bridge.py, these lines are NOT owner-gated -- they're normal
gameplay/support traffic from any real player, used to spot real bugs
(misclassified intents, confused-sounding questions, a support answer
that doesn't actually resolve anything) as they happen, not to accept
commands.

Usage:
    python3 scripts/check_topic_activity.py            # list new activity
    python3 scripts/check_topic_activity.py --mark-processed "2026-07-12 00:45:10,000"
        # advance the cursor once you've reviewed everything up to (and
        # including) that log timestamp, so it isn't reprocessed next time
"""
import json
import re
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
STATE_PATH = REPO_ROOT / ".topic_monitor_state.json"
LOG_PATH = REPO_ROOT / "bot_live_tmp.log"

INTENT_RE = re.compile(
    r"^(?P<ts>\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2},\d{3}) \[INFO\] pandora_mmo: "
    r"\[intent\] user=(?P<user>-?\d+) action=(?P<action>.*)$"
)
SUPPORT_RE = re.compile(
    r"^(?P<ts>\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2},\d{3}) \[INFO\] pandora_mmo: "
    r"\[support\] user=(?P<user>-?\d+) text=(?P<rest>.*)$"
)
# Real live request (2026-08-27, Coffee: "work with the language model
# to vote or down vote responses so it can get better and more
# accurate?") -- every Support answer now carries a real 👍/👎 button
# (bot.py's _support_feedback_keyboard/support_vote_callback); a tap
# logs this line the same way [support] already does. Surfaced here,
# not a separate script, so it rides the exact same cursor and gets
# reviewed by every regular self-improvement pass without any extra
# wiring -- a 👎 is the strongest possible signal of a real grounding
# gap, worth treating with at least as much weight as a repeated
# "chat"-swallowed phrase.
SUPPORT_FEEDBACK_RE = re.compile(
    r"^(?P<ts>\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2},\d{3}) \[INFO\] pandora_mmo: "
    r"\[support_feedback\] user=(?P<user>-?\d+) vote=(?P<vote>up|down) (?P<rest>.*)$"
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


def list_new_activity() -> list[dict]:
    cursor = load_cursor()
    if not LOG_PATH.exists():
        return []
    activity = []
    for line in LOG_PATH.read_text(errors="replace").splitlines():
        m = INTENT_RE.match(line)
        if m and m["ts"] > cursor:
            activity.append({"timestamp": m["ts"], "topic": "adventure", "user": m["user"], "detail": m["action"]})
            continue
        m = SUPPORT_RE.match(line)
        if m and m["ts"] > cursor:
            activity.append({"timestamp": m["ts"], "topic": "support", "user": m["user"], "detail": m["rest"]})
            continue
        m = SUPPORT_FEEDBACK_RE.match(line)
        if m and m["ts"] > cursor:
            activity.append({
                "timestamp": m["ts"], "topic": "support_feedback", "user": m["user"],
                "detail": f"vote={m['vote']} {m['rest']}",
            })
    return activity


if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "--mark-processed":
        mark_processed(sys.argv[2])
        print(f"Cursor advanced to {sys.argv[2]}")
    else:
        for item in list_new_activity():
            print(json.dumps(item))
