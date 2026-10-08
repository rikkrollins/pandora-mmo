#!/usr/bin/env python3
"""
scripts/pin_support_welcome.py
Posts (and pins) a one-time "how to use Support" guide for new players
in the Support topic, per Coffee's direct request (2026-10-08): "make
sure support topic gaps have been filled and please post a pinned
post for new players in the support topic explaining how to use it".

Sends via the Bot API directly (BOT_TOKEN from .env), independent of
the bot's own getUpdates polling loop and of announce_deploy.py's own
--pin mechanism -- deliberately tracked under its OWN game_settings
key (not announce_deploy's _PINNED_MESSAGE_ID_SETTING, which rotates
on every deploy, and not scripts/pin_main_welcome.py's own key either,
since this is a separate pin in a separate topic).

Usage:
    python3 scripts/pin_support_welcome.py
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import requests

import config
import db

_PINNED_MESSAGE_ID_SETTING = "support_topic_welcome_pin_message_id"

WELCOME_TEXT = """\
🛟 **Support — ask anything about how the game works.**

This topic is a real, working wiki — ask in plain English and you'll \
get a real, grounded answer (never made up), from a real person or \
the Support AI. A few things worth knowing:

**1. Ask how-to questions.** "How do I enchant my weapon?", "How do I \
join the Forge Guild?", "What does Eldritch Blast do?", "How do I \
replenish a spell slot?" — all answered from this game's own real \
rules and content, never general tabletop-RPG guesswork.

**2. Check a character sheet.** "Show me my character sheet" or \
"Show me Sarah's sheet" works right here too — no need to go to \
Adventure or Main for a quick look.

**3. Toggle physical dice.** Say "turn on physical dice" (or "turn \
it off") if you'd rather roll your own real dice and type the result, \
instead of the game rolling for you.

**4. Answers can take a minute or two** under load — you'll get a \
heads-up message first, then the real answer. Rate it 👍/👎 once it \
arrives; that feedback actually gets reviewed.

Support is for QUESTIONS, not actions — for playing (moving, \
fighting, casting, exploring), head to **Adventure**. For character \
maintenance (sheet, gear, guild, party, market, and more), **Main** \
and **Adventure** both work.

For more information, visit the GitHub: github.com/rikkrollins/pandora-mmo
"""


def pin_welcome() -> None:
    chat_id = getattr(config, "TELEGRAM_CHAT_ID", None)
    if not chat_id:
        raise RuntimeError(
            "TELEGRAM_CHAT_ID isn't set in .env — add it (the group's chat id, "
            "usually a negative number) before this script can post anything."
        )

    resp = requests.post(
        f"https://api.telegram.org/bot{config.BOT_TOKEN}/sendMessage",
        json={
            "chat_id": chat_id, "text": WELCOME_TEXT, "parse_mode": "Markdown",
            "message_thread_id": config.TOPIC_SUPPORT_ID,
        },
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
    print(f"Posted and pinned message {new_message_id} in Support.")


if __name__ == "__main__":
    pin_welcome()
