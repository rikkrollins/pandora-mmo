#!/usr/bin/env python3
"""
scripts/announce_deploy.py
Posts a deployment announcement to the Telegram group's Development
topic. Called after pushing and restarting the bot with new code, per
Coffee's standing request (2026-07-09) that every deploy gets posted
there automatically — see CLAUDE.md's Deployment section.

Sends via the Bot API directly (BOT_TOKEN from .env), independent of
the bot's own getUpdates polling loop — this only ever POSTs a message,
so it can't conflict with the live poller.

Usage:
    python3 scripts/announce_deploy.py "v1.1.0 deployed: lockpicking, crafting, ..."
"""
import sys

import requests

import config

REQUIRED_ENV = "TELEGRAM_CHAT_ID"


def announce(message: str) -> None:
    chat_id = getattr(config, "TELEGRAM_CHAT_ID", None)
    if not chat_id:
        raise RuntimeError(
            f"{REQUIRED_ENV} isn't set in .env — add it (the group's chat id, "
            "usually a negative number) before this script can post anything."
        )

    resp = requests.post(
        f"https://api.telegram.org/bot{config.BOT_TOKEN}/sendMessage",
        json={
            "chat_id": chat_id,
            "message_thread_id": config.TOPIC_DEVELOPMENT_ID,
            "text": f"🚀 **Deploy update**\n{message}",
            "parse_mode": "Markdown",
        },
        timeout=30,
    )
    resp.raise_for_status()


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("Usage: python3 scripts/announce_deploy.py \"message text\"", file=sys.stderr)
        sys.exit(1)
    announce(sys.argv[1])
    print("Posted to Development topic.")
