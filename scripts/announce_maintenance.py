#!/usr/bin/env python3
"""
scripts/announce_maintenance.py
Player-facing maintenance protocol (2026-07-17, per Coffee: "before you
[restart Ollama] announce serve shutdowns. create protocol for this so
you can roll this out systematically and smoothly to all players").

Unlike scripts/announce_deploy.py (which posts ONLY to the owner-only
Development topic), this posts to the topics real players actually
watch -- Main (the group's General topic) and Adventure (where a stuck
narration call is actually felt) -- so nobody is left staring at
silence wondering if the game is broken during a real service
interruption (an Ollama restart to clear a hung generation, a bot
redeploy that affects live gameplay, etc).

Only use this for maintenance that actually costs players real waiting
time (a stuck Ollama request needing a service restart, a redeploy
that will be down more than a few seconds) -- per Coffee (2026-07-17):
"only if it will be down for any amount of time tho dont be annoying".
A restart that completes in a second or two isn't worth a round-trip
announcement; skip it and just do the thing.

Protocol (use in this order):
    1. python3 scripts/announce_maintenance.py --starting "reason" ["ETA"]
       Post BEFORE taking any disruptive action (service restart, bot
       restart). Gives players a heads-up instead of silent downtime.
    2. Take the actual action (systemctl restart ..., etc).
    3. python3 scripts/announce_maintenance.py --complete ["summary"]
       Post immediately once service is confirmed healthy again.

Sends via the Bot API directly (BOT_TOKEN from .env), independent of
the bot's own getUpdates polling loop -- same reasoning as
announce_deploy.py: this only ever POSTs a message, so it can never
conflict with the live poller, and works even while the main bot
process itself is being restarted.
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import requests

import config

REQUIRED_ENV = "TELEGRAM_CHAT_ID"

# Player-facing topics only -- Development is deliberately excluded
# here (see project_dev_channel_exclusivity in memory: that bridge is
# owner+Claude-only). None means "General"/Main -- Telegram's own API
# quirk (see topics.py's is_main docstring), not omitted by mistake.
_PLAYER_TOPICS = (None, config.TOPIC_ADVENTURE_ID)


def _escape_markdown(text: str) -> str:
    """Same escaping as announce_deploy.py -- keeps a stray `_`/`*` from breaking Telegram's parser."""
    for ch in ("_", "*", "`", "["):
        text = text.replace(ch, f"\\{ch}")
    return text


def _send_to_all_player_topics(text: str) -> None:
    chat_id = getattr(config, "TELEGRAM_CHAT_ID", None)
    if not chat_id:
        raise RuntimeError(
            f"{REQUIRED_ENV} isn't set in .env — add it (the group's chat id, "
            "usually a negative number) before this script can post anything."
        )
    for thread_id in _PLAYER_TOPICS:
        payload = {"chat_id": chat_id, "text": text, "parse_mode": "Markdown"}
        if thread_id is not None:
            payload["message_thread_id"] = thread_id
        resp = requests.post(
            f"https://api.telegram.org/bot{config.BOT_TOKEN}/sendMessage",
            json=payload,
            timeout=30,
        )
        resp.raise_for_status()


def starting(reason: str, eta: str = "just a minute or two") -> None:
    # Written in the game's own voice, not a dry ops notice (2026-07-17,
    # per Coffee: "maybe a message from the actual bot to the user" --
    # this posts AS @PandoraMMO_Bot either way, but should read like the
    # world itself is speaking, matching the mystical tone the rest of
    # the game already leans into, not a sysadmin banner).
    _send_to_all_player_topics(
        f"🌫️ *The threads of the world pause for a moment...*\n\n"
        f"A quick bit of behind-the-scenes work is needed — {_escape_markdown(reason)}. "
        f"Replies may lag for a bit (should be back within {_escape_markdown(eta)}). "
        f"Nothing you've done is lost — stay right where you are, and your next message "
        f"will pick up exactly where things left off."
    )


def complete(summary: str = "everything's back to normal.") -> None:
    _send_to_all_player_topics(
        f"🌤️ *...and the world stirs again.*\n\n"
        f"All clear — {_escape_markdown(summary)} Thanks for your patience, adventurers."
    )


if __name__ == "__main__":
    if len(sys.argv) < 2 or sys.argv[1] not in ("--starting", "--complete"):
        print("Usage: python3 scripts/announce_maintenance.py --starting \"reason\" [\"ETA\"]", file=sys.stderr)
        print("       python3 scripts/announce_maintenance.py --complete [\"summary\"]", file=sys.stderr)
        sys.exit(1)
    if sys.argv[1] == "--starting":
        reason = sys.argv[2] if len(sys.argv) > 2 else "quick behind-the-scenes fix"
        eta = sys.argv[3] if len(sys.argv) > 3 else "just a minute or two"
        starting(reason, eta)
    else:
        complete(sys.argv[2] if len(sys.argv) > 2 else "All clear — everything's back to normal.")
    print("Posted to Main + Adventure.")
