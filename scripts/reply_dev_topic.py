#!/usr/bin/env python3
"""
scripts/reply_dev_topic.py
Sends a plain message to the Telegram group's Development topic. Used by
the Dev-topic command bridge (Claude Code polling [dev_topic] log lines
via a recurring CronCreate job) to reply once a command's been acted on,
or to ask a clarifying question back. Independent of the bot's own
getUpdates polling loop, same as scripts/announce_deploy.py.

Usage:
    python3 scripts/reply_dev_topic.py "message text"
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import requests

import config


def send(text: str) -> None:
    chat_id = getattr(config, "TELEGRAM_CHAT_ID", None)
    if not chat_id:
        raise RuntimeError("TELEGRAM_CHAT_ID isn't set in .env — add it before this script can post anything.")

    resp = requests.post(
        f"https://api.telegram.org/bot{config.BOT_TOKEN}/sendMessage",
        json={
            "chat_id": chat_id,
            "message_thread_id": config.TOPIC_DEVELOPMENT_ID,
            "text": text,
        },
        timeout=30,
    )
    resp.raise_for_status()


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print('Usage: python3 scripts/reply_dev_topic.py "message text"', file=sys.stderr)
        sys.exit(1)
    send(sys.argv[1])
    print("Posted to Development topic.")
