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
    python3 scripts/announce_deploy.py --warn "~2 minutes"
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import requests

import config

REQUIRED_ENV = "TELEGRAM_CHAT_ID"


def _escape_markdown(text: str) -> str:
    """
    Escapes Telegram legacy Markdown's special characters so a message
    describing code (which routinely mentions function_names, snake_case,
    or asterisks) can't accidentally break parsing. Confirmed live
    2026-07-14, twice: a deploy message mentioning "talk_npc" and later
    "accept_quest" each hit a 400 Bad Request from Telegram's Markdown
    parser choking on the bare underscore (an unmatched italic marker) --
    not something the caller should have to remember to avoid every time.
    """
    for ch in ("_", "*", "`", "["):
        text = text.replace(ch, f"\\{ch}")
    return text


def _send(text: str) -> None:
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
            "text": text,
            "parse_mode": "Markdown",
        },
        timeout=30,
    )
    resp.raise_for_status()


def announce(message: str) -> None:
    _send(f"🚀 **Deploy update**\n{_escape_markdown(message)}")


def warn(eta: str) -> None:
    """Posted before a redeploy — the bot briefly restarts, players shouldn't be caught off guard."""
    _send(f"🛠️ **Update coming soon** — the bot will restart shortly (ETA: {_escape_markdown(eta)}). Back momentarily.")


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("Usage: python3 scripts/announce_deploy.py \"message text\"", file=sys.stderr)
        print("       python3 scripts/announce_deploy.py --warn \"ETA text\"", file=sys.stderr)
        sys.exit(1)
    if sys.argv[1] == "--warn":
        warn(sys.argv[2] if len(sys.argv) > 2 else "a couple minutes")
    else:
        announce(sys.argv[1])
    print("Posted to Development topic.")
