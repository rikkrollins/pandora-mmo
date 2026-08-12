#!/usr/bin/env python3
"""
scripts/backfill_guild_curriculum_pins.py
ONE-TIME live data fix (2026-08-12, per Coffee's live report: "im not
seeing anything posted/pinned in the guild topics. are u sure this was
completed ?").

Real confirmed gap: the guild curriculum (v1.27.154) posts+pins its
first step ONLY from a fresh `join_guild` call (bot._do_join_guild) or
a step completion (bot._complete_guild_curriculum_step) -- neither path
ever runs retroactively for a character who joined their guild BEFORE
v1.27.154 shipped. Confirmed via a live, read-only DB query: every real
character currently in a guild has `guild_curriculum_step_unlocked_at
IS NULL`, meaning `join_guild`'s post-v1.27.154 stamp never touched
them and they were never told the curriculum exists.

This script finds every real (chat_id, guild) pair with at least one
member in that state, and for each ONE posts+pins step 0's real
announcement into that guild's own Telegram topic -- same text bot.py
itself would have posted at join time -- and stamps
guild_curriculum_step_unlocked_at=now() for every affected member of
that guild in that chat, starting their real 6h cooldown from the
moment they're actually told about it (matching what a fresh join does
today), not leaving it silently NULL forever.

Idempotent: skips any (chat_id, guild) pair that already has a
guild_curriculum_pin_{chat_id}_{guild} setting recorded (either from a
previous run of this script, or because SOME member of that guild in
that chat already triggered a real post via a later join/completion).
Does not touch a character whose guild_curriculum_step_unlocked_at is
already set -- only backfills the confirmed-silent NULL case.
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from datetime import datetime, timezone

import requests

import bot
import config
import db
import guild_curriculum as guild_curriculum_module


def _affected_guild_chat_pairs() -> list[tuple[int, str]]:
    with db.get_connection() as conn:
        rows = conn.execute(
            "SELECT DISTINCT chat_id, guild FROM characters "
            "WHERE guild IS NOT NULL AND guild_curriculum_step_unlocked_at IS NULL"
        ).fetchall()
    return [(row["chat_id"], row["guild"]) for row in rows]


def _affected_members(chat_id: int, guild_id: str) -> list[int]:
    # Targets exact character_id rows, not telegram_user_id -- a player
    # can have an inactive character sitting in this same state while a
    # DIFFERENT character of theirs is currently active; update_character
    # (telegram_user_id-keyed) would silently write to whichever one is
    # active instead of the one actually queried here (same bug class as
    # the 2026-07-24 shrine-revival incident documented on
    # db.update_character_by_id). Using update_character_by_id below with
    # this exact character_id avoids that entirely.
    with db.get_connection() as conn:
        rows = conn.execute(
            "SELECT character_id FROM characters "
            "WHERE chat_id = ? AND guild = ? AND guild_curriculum_step_unlocked_at IS NULL",
            (chat_id, guild_id),
        ).fetchall()
    return [row["character_id"] for row in rows]


def post_and_pin_step_zero(chat_id: int, guild_id: str) -> bool:
    topic_id = config.GUILD_TOPIC_IDS.get(guild_id)
    if not topic_id:
        print(f"  Skipping {guild_id} in chat {chat_id} -- no topic ID configured.", file=sys.stderr)
        return False

    step = guild_curriculum_module.get_step(guild_id, 0)
    if step is None:
        return False
    text = bot._format_guild_curriculum_step_announcement(step)

    resp = requests.post(
        f"https://api.telegram.org/bot{config.BOT_TOKEN}/sendMessage",
        json={"chat_id": chat_id, "message_thread_id": topic_id, "text": text, "parse_mode": "Markdown"},
        timeout=30,
    )
    resp.raise_for_status()
    message_id = resp.json()["result"]["message_id"]

    pin_resp = requests.post(
        f"https://api.telegram.org/bot{config.BOT_TOKEN}/pinChatMessage",
        json={"chat_id": chat_id, "message_id": message_id, "disable_notification": True},
        timeout=30,
    )
    pin_resp.raise_for_status()
    db.set_setting(f"guild_curriculum_pin_{chat_id}_{guild_id}", str(message_id))
    return True


if __name__ == "__main__":
    pairs = _affected_guild_chat_pairs()
    if not pairs:
        print("No affected (chat_id, guild) pairs found -- nothing to backfill.")
        sys.exit(0)

    for chat_id, guild_id in pairs:
        pin_key = f"guild_curriculum_pin_{chat_id}_{guild_id}"
        if db.get_setting(pin_key):
            print(f"Skipping {guild_id} in chat {chat_id} -- already has a recorded curriculum pin.")
            continue
        members = _affected_members(chat_id, guild_id)
        if not members:
            continue
        if post_and_pin_step_zero(chat_id, guild_id):
            now = datetime.now(timezone.utc).isoformat()
            for character_id in members:
                db.update_character_by_id(character_id, guild_curriculum_step_unlocked_at=now)
            print(f"Posted+pinned step 0 for {guild_id} in chat {chat_id}; stamped {len(members)} member(s).")
