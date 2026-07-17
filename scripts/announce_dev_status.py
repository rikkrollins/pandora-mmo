#!/usr/bin/env python3
"""
scripts/announce_dev_status.py
Reusable Development-topic status/task-update announcer (2026-07-17,
per Coffee: "post all pending tasks in Dev channel and eta time to
next update if there will be downtime or not?! i want a full
functional system for this, where every we need those announcements
we shud push them"). Distinct from scripts/announce_deploy.py (deploy
version bumps) and scripts/announce_maintenance.py (player-facing
Main/Adventure downtime notices) -- this one is specifically for
pushing a task-list/progress/ETA summary to the OWNER-ONLY Development
topic, any time it's useful, not just after a deploy.

Splits long messages across Telegram's ~4096-char limit automatically,
since a real task-list summary can run long.

Usage:
    python3 scripts/announce_dev_status.py "full status text"
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import requests

import config

_TELEGRAM_MESSAGE_LIMIT = 4000  # a little under Telegram's real 4096 cap, for safety margin


def _send_chunk(text: str) -> None:
    chat_id = getattr(config, "TELEGRAM_CHAT_ID", None)
    if not chat_id:
        raise RuntimeError("TELEGRAM_CHAT_ID isn't set in .env — add it before this script can post anything.")
    resp = requests.post(
        f"https://api.telegram.org/bot{config.BOT_TOKEN}/sendMessage",
        json={
            "chat_id": chat_id,
            "message_thread_id": config.TOPIC_DEVELOPMENT_ID,
            "text": text,
            "parse_mode": "Markdown",
        },
        timeout=30,
    )
    resp.raise_for_status()


def announce(text: str) -> None:
    """Splits on line boundaries so a long status update never gets cut mid-line."""
    lines = text.split("\n")
    chunk = ""
    for line in lines:
        candidate = f"{chunk}\n{line}" if chunk else line
        if len(candidate) > _TELEGRAM_MESSAGE_LIMIT:
            _send_chunk(chunk)
            chunk = line
        else:
            chunk = candidate
    if chunk:
        _send_chunk(chunk)


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print('Usage: python3 scripts/announce_dev_status.py "status text"', file=sys.stderr)
        sys.exit(1)
    announce(sys.argv[1])
    print("Posted to Development topic.")
