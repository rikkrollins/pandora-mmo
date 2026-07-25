#!/usr/bin/env python3
"""
scripts/create_new_guild_topics.py
ONE-TIME infra script (2026-07-25, per Coffee): creates a real Telegram
forum topic for each of the 2 NEW guilds added this pass (The Thieves'
Guild, The Faith Circle) via the Bot API's createForumTopic -- mirrors
scripts/create_guild_topics.py's original pattern, but scoped to just
these 2 so it never touches the 3 guild topics that already exist.

Do NOT re-run this after these topics already exist -- it will create
duplicates. Prints each new message_thread_id so it can be recorded in
.env as GUILD_TOPIC_<ID> for bot.py/config.py to use going forward.
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import requests

import config
from guilds import GUILDS

NEW_GUILD_IDS = ["thieves_guild", "faith_circle"]
_ICON_COLORS = {
    "thieves_guild": 0x1E1E1E,
    "faith_circle": 0x35C759,
}


def create_topic(name: str, icon_color: int) -> int:
    resp = requests.post(
        f"https://api.telegram.org/bot{config.BOT_TOKEN}/createForumTopic",
        json={"chat_id": config.TELEGRAM_CHAT_ID, "name": name, "icon_color": icon_color},
        timeout=30,
    )
    resp.raise_for_status()
    return resp.json()["result"]["message_thread_id"]


if __name__ == "__main__":
    if not config.TELEGRAM_CHAT_ID:
        print("TELEGRAM_CHAT_ID isn't set in .env -- required to create topics.", file=sys.stderr)
        sys.exit(1)

    print("Creating Telegram forum topics for the 2 new guilds...")
    results = {}
    for guild_id in NEW_GUILD_IDS:
        guild = GUILDS[guild_id]
        thread_id = create_topic(guild["name"], _ICON_COLORS[guild_id])
        results[guild_id] = thread_id
        print(f"  {guild['name']} ({guild_id}) -> message_thread_id={thread_id}")

    print("\nAdd these to .env:")
    for guild_id, thread_id in results.items():
        env_name = f"TOPIC_{guild_id.upper()}_ID"
        print(f"{env_name}={thread_id}")
