#!/usr/bin/env python3
"""
scripts/pin_guild_topic_info.py
ONE-TIME infra script (task #77, per Coffee: "pin descriptions on how
to use guilds"): posts a short explainer into each real guild topic
and pins it there, same Bot-API-direct pattern as announce_deploy.py.

Safe to re-run -- each run posts (and pins) a fresh copy; it does NOT
unpin or delete any earlier one, so avoid running this repeatedly
without reason (it'll leave old explainer messages un-pinned but still
present in the topic).
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import requests

import config
from guilds import GUILDS, GUILD_QUESTS

EXPLAINER_TEMPLATE = """🏛️ **{name} — how this topic works**

This is {name}'s own real, members-only space.

• Only real members get engaged by the bot here — anyone else gets a redirect. Join with "join {name}" in Adventure (if you're eligible).
• Chat freely with your guildmates — this is just an ordinary topic for that.
• Say "check guild quest" here (or in Adventure) to see today's real guild bounty:
  📜 *{quest_title}* — {quest_desc}
  Reward: {reward_gold} gold, {reward_xp} XP
• Win ANY fight in Adventure while you're a member to claim it — once per real day, announced right here.
"""


def post_and_pin(guild_id: str, topic_id: int) -> None:
    guild = GUILDS[guild_id]
    quest = GUILD_QUESTS[guild_id]
    text = EXPLAINER_TEMPLATE.format(
        name=guild["name"], quest_title=quest["title"], quest_desc=quest["description"],
        reward_gold=quest["reward_gold"], reward_xp=quest["reward_xp"],
    )
    resp = requests.post(
        f"https://api.telegram.org/bot{config.BOT_TOKEN}/sendMessage",
        json={"chat_id": config.TELEGRAM_CHAT_ID, "message_thread_id": topic_id, "text": text, "parse_mode": "Markdown"},
        timeout=30,
    )
    resp.raise_for_status()
    message_id = resp.json()["result"]["message_id"]

    pin_resp = requests.post(
        f"https://api.telegram.org/bot{config.BOT_TOKEN}/pinChatMessage",
        json={"chat_id": config.TELEGRAM_CHAT_ID, "message_id": message_id, "disable_notification": True},
        timeout=30,
    )
    pin_resp.raise_for_status()
    print(f"  Posted and pinned explainer in {guild['name']}'s topic.")


if __name__ == "__main__":
    if not config.TELEGRAM_CHAT_ID:
        print("TELEGRAM_CHAT_ID isn't set in .env.", file=sys.stderr)
        sys.exit(1)

    for guild_id, topic_id in config.GUILD_TOPIC_IDS.items():
        if not topic_id:
            print(f"Skipping {guild_id} -- no topic ID configured.", file=sys.stderr)
            continue
        post_and_pin(guild_id, topic_id)
