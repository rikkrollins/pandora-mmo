#!/usr/bin/env python3
"""
scripts/create_guild_topics.py
ONE-TIME infra script (task #77, per Coffee): creates a real Telegram
forum topic per guild (Adventurers' Guild, The Arcane Circle, Silver
Wardens) via the Bot API's createForumTopic, independent of the bot's
own getUpdates polling loop -- same reasoning as announce_deploy.py.

Do NOT re-run this after the topics already exist -- it will create
duplicates. Prints each new message_thread_id so it can be recorded in
.env as GUILD_TOPIC_<ID> for bot.py/config.py to use going forward.
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import requests

import config
from guilds import GUILDS

_ICON_COLORS = {
    "adventurers_guild": 0x6FB9F0,
    "arcane_circle": 0x9336FF,
    "silver_wardens": 0xB53101,
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

    print("Creating one Telegram forum topic per guild...")
    results = {}
    for guild_id, guild in GUILDS.items():
        thread_id = create_topic(guild["name"], _ICON_COLORS.get(guild_id, 0x6FB9F0))
        results[guild_id] = thread_id
        print(f"  {guild['name']} ({guild_id}) -> message_thread_id={thread_id}")

    print("\nAdd these to .env:")
    for guild_id, thread_id in results.items():
        env_name = f"TOPIC_{guild_id.upper()}_ID"
        print(f"{env_name}={thread_id}")
