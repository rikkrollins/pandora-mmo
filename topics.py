"""
topics.py
Maps Telegram forum topic names <-> message_thread_id, and provides
helpers to check which topic a message belongs to and whether game
logic is allowed to run there.
"""
import config

TOPIC_IDS = {
    "main": config.TOPIC_MAIN_ID,
    "support": config.TOPIC_SUPPORT_ID,
    "adventure": config.TOPIC_ADVENTURE_ID,
    "development": config.TOPIC_DEVELOPMENT_ID,
}

# Reverse lookup: thread_id -> topic name
_ID_TO_NAME = {v: k for k, v in TOPIC_IDS.items()}


def get_topic_id(topic_name: str) -> int:
    """Return the message_thread_id for a given topic name."""
    return TOPIC_IDS[topic_name.lower()]


def get_topic_name(message_thread_id: int) -> str:
    """Return the topic name for a given message_thread_id, or 'unknown'."""
    return _ID_TO_NAME.get(message_thread_id, "unknown")


def is_adventure(message_thread_id: int) -> bool:
    """True if this message came from the Adventure topic."""
    return message_thread_id == config.TOPIC_ADVENTURE_ID


def is_development(message_thread_id: int) -> bool:
    """True if this message came from the Development topic."""
    return message_thread_id == config.TOPIC_DEVELOPMENT_ID


def is_support(message_thread_id: int) -> bool:
    """True if this message came from the Support topic."""
    return message_thread_id == config.TOPIC_SUPPORT_ID


_GUILD_TOPIC_TO_ID = {v: k for k, v in config.GUILD_TOPIC_IDS.items() if v is not None}


def guild_id_for_topic(message_thread_id: int | None) -> str | None:
    """Returns the real guild_id this topic belongs to (task #77), or None if it isn't a guild topic."""
    if message_thread_id is None:
        return None
    return _GUILD_TOPIC_TO_ID.get(message_thread_id)


def is_main(message_thread_id: int | None) -> bool:
    """
    True if this message came from the Main topic. Telegram's General
    topic (which Main maps to) often sends message_thread_id as None
    rather than an explicit ID — that must be treated as Main, or every
    message in Main gets silently ignored.
    """
    return message_thread_id is None or message_thread_id == config.TOPIC_MAIN_ID
