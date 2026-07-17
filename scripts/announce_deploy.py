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
    python3 scripts/announce_deploy.py --pin "catchy player-facing update text"

--pin (2026-07-17, per Coffee: "write a pinned post... make it catchy
and enticing... make this part of the update/upgrade system") posts a
player-facing, enticing summary to Main and pins it there, replacing
whatever was pinned before -- meant to be run as a normal step of
every real update from now on, not a one-off. Requires the bot to have
"pin messages" admin rights in the group; if it doesn't, this fails
loudly rather than silently doing nothing, so the missing permission
gets noticed immediately.
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import requests

import config
import db

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


_PINNED_MESSAGE_ID_SETTING = "pinned_update_message_id"


def pin_update(catchy_text: str) -> None:
    """
    Posts a player-facing, enticing update summary to Main and pins it,
    unpinning whatever this same mechanism pinned last time (tracked in
    game_settings via db.py, same pattern as every other small piece of
    persistent state in this project). Meant to run as a normal part of
    every real update going forward, not a one-off -- see module
    docstring.
    """
    chat_id = getattr(config, "TELEGRAM_CHAT_ID", None)
    if not chat_id:
        raise RuntimeError(
            f"{REQUIRED_ENV} isn't set in .env — add it (the group's chat id, "
            "usually a negative number) before this script can post anything."
        )

    resp = requests.post(
        f"https://api.telegram.org/bot{config.BOT_TOKEN}/sendMessage",
        json={"chat_id": chat_id, "text": catchy_text, "parse_mode": "Markdown"},
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


if __name__ == "__main__":
    usage_lines = [
        "Usage: python3 scripts/announce_deploy.py \"message text\"",
        "       python3 scripts/announce_deploy.py --version X --summary \"...\"",
        "       python3 scripts/announce_deploy.py --warn \"ETA text\"",
        "       python3 scripts/announce_deploy.py --pin \"catchy player-facing text\"",
    ]
    if len(sys.argv) < 2:
        print("\n".join(usage_lines), file=sys.stderr)
        sys.exit(1)
    if sys.argv[1] == "--warn":
        warn(sys.argv[2] if len(sys.argv) > 2 else "a couple minutes")
        print("Posted to Development topic.")
    elif sys.argv[1] == "--pin":
        pin_update(sys.argv[2])
        print("Posted and pinned to Main.")
    elif sys.argv[1] == "--version":
        # 2026-07-17: added after this exact interface was called with
        # --version/--summary flags that silently didn't exist -- the
        # old code fell through to the plain-message branch and posted
        # the literal string "--version" to Development four deploys in
        # a row before anyone noticed, since the script still printed
        # "Posted to Development topic." either way. Supporting the
        # interface for real, rather than just documenting it, is the
        # actual fix; an unrecognized flag now errors loudly (below)
        # instead of silently posting garbage.
        try:
            version_idx = sys.argv.index("--version") + 1
            summary_idx = sys.argv.index("--summary") + 1
            version_text = sys.argv[version_idx]
            summary_text = sys.argv[summary_idx]
        except (ValueError, IndexError):
            print("--version requires both --version X and --summary \"...\"", file=sys.stderr)
            sys.exit(1)
        announce(f"v{version_text} deployed\n\n{summary_text}")
        print("Posted to Development topic.")
    elif sys.argv[1].startswith("--"):
        print(f"Unrecognized flag: {sys.argv[1]}", file=sys.stderr)
        print("\n".join(usage_lines), file=sys.stderr)
        sys.exit(1)
    else:
        announce(sys.argv[1])
        print("Posted to Development topic.")
