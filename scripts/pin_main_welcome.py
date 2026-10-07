#!/usr/bin/env python3
"""
scripts/pin_main_welcome.py
Posts (and pins) a one-time "how to start" guide for new players in
the Main topic, per Coffee's direct request (2026-10-07): "make a
pinned post in main topic for new players on how to 'start'".

Sends via the Bot API directly (BOT_TOKEN from .env), independent of
the bot's own getUpdates polling loop and of announce_deploy.py's own
--pin mechanism -- deliberately tracked under its OWN game_settings
key (not announce_deploy's _PINNED_MESSAGE_ID_SETTING), since that one
rotates on every deploy and would otherwise unpin this permanent
new-player guide the next time a version ships.

Usage:
    python3 scripts/pin_main_welcome.py
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import requests

import config
import db

_PINNED_MESSAGE_ID_SETTING = "main_topic_welcome_pin_message_id"

WELCOME_TEXT = """\
🌒 **New here? Start in 3 steps.**

**1. Create a character.** Just say "I want to create a character" — \
right here in Main, or in the **Adventure** topic. No forms, no menus \
required — the game will ask you what it needs to know as you go.

**2. Head to Adventure.** That's where the game actually happens — \
look around, walk, fight, talk to NPCs, cast spells, all in plain \
English. Say "look around" once you're in to get your bearings.

**3. Just talk.** There's no required syntax and no slash commands. \
Say what your character would actually do or say, and the game \
figures out the rest — same as talking to a real Game Master.

Stuck, or something broken? Ask in **Support** — a real person or the \
Support AI will help. Players and AI-driven companions play by the \
exact same rules here, so don't be surprised if an NPC remembers you \
from last time.

Welcome to Pandora. The road remembers everyone who's ever walked it.
"""


def pin_welcome() -> None:
    chat_id = getattr(config, "TELEGRAM_CHAT_ID", None)
    if not chat_id:
        raise RuntimeError(
            "TELEGRAM_CHAT_ID isn't set in .env — add it (the group's chat id, "
            "usually a negative number) before this script can post anything."
        )

    # Deliberately NO message_thread_id key at all -- Telegram's API
    # only accepts an OMITTED message_thread_id for Main/General
    # (confirmed live, see bot.py's own _notify_main_topic comment:
    # passing config.TOPIC_MAIN_ID explicitly fails with BadRequest
    # ('Message thread not found'), even though that same id is the
    # right value for RECOGNIZING an incoming Main-topic message).
    # announce_deploy.py's own pin_update follows the exact same
    # pattern for this exact reason.
    resp = requests.post(
        f"https://api.telegram.org/bot{config.BOT_TOKEN}/sendMessage",
        json={"chat_id": chat_id, "text": WELCOME_TEXT, "parse_mode": "Markdown"},
        timeout=30,
    )
    resp.raise_for_status()
    new_message_id = resp.json()["result"]["message_id"]

    old_message_id = db.get_setting(_PINNED_MESSAGE_ID_SETTING)
    if old_message_id:
        # Best-effort -- an already-deleted or already-unpinned old
        # message shouldn't stop the new one from getting pinned.
        try:
            requests.post(
                f"https://api.telegram.org/bot{config.BOT_TOKEN}/unpinChatMessage",
                json={"chat_id": chat_id, "message_id": int(old_message_id)},
                timeout=30,
            )
        except requests.RequestException:
            pass

    pin_resp = requests.post(
        f"https://api.telegram.org/bot{config.BOT_TOKEN}/pinChatMessage",
        json={"chat_id": chat_id, "message_id": new_message_id, "disable_notification": False},
        timeout=30,
    )
    pin_resp.raise_for_status()
    db.set_setting(_PINNED_MESSAGE_ID_SETTING, str(new_message_id))
    print(f"Posted and pinned message {new_message_id} in Main.")


if __name__ == "__main__":
    pin_welcome()
