"""
topics.py
Maps Telegram forum topic names <-> message_thread_id, and provides
helpers to check which topic a message belongs to and whether game
logic is allowed to run there.

Multi-tenant scaling Phase 3 (2026-08-02): every routing/gating helper
below now takes a real chat_id and consults db.chat_topic_config first
-- a group that has run /set_topic gets its OWN topic layout; every
other chat (including this bot's own home group, which has never
needed to run /set_topic) falls back to the TOPIC_IDS constants below,
so nothing changes for the one real live group unless/until it's
deliberately reconfigured. Guild topics (GUILD_TOPIC_IDS) are
deliberately NOT covered by this yet -- home-group-only until a real
per-tenant guild system exists, a separate, not-yet-scoped follow-up.
"""
import config
import db

TOPIC_IDS = {
    "main": config.TOPIC_MAIN_ID,
    "support": config.TOPIC_SUPPORT_ID,
    "adventure": config.TOPIC_ADVENTURE_ID,
    "development": config.TOPIC_DEVELOPMENT_ID,
}

# Reverse lookup: thread_id -> topic name (home-group-only fallback)
_ID_TO_NAME = {v: k for k, v in TOPIC_IDS.items()}


def _resolve(chat_id: int | None, topic_name: str) -> int | None:
    """Per-tenant topic thread_id lookup, falling back to the home-group constants above."""
    if chat_id is not None:
        configured = db.get_chat_topic_id(chat_id, topic_name)
        if configured is not None:
            return configured
    return TOPIC_IDS.get(topic_name)


def thread_id_for(chat_id: int | None, topic_name: str) -> int | None:
    """Public per-tenant lookup for outbound message_thread_id=... in a reply."""
    return _resolve(chat_id, topic_name.lower())


def get_topic_id(topic_name: str) -> int:
    """
    Home-group-only lookup -- for the few callers with no real chat_id
    in scope (e.g. one-off scripts talking only to this bot's own
    group). Prefer thread_id_for wherever a real chat_id is available.
    """
    return TOPIC_IDS[topic_name.lower()]


def get_topic_name(chat_id: int | None, message_thread_id: int | None) -> str:
    """Return the topic name for a given (chat_id, message_thread_id), or 'unknown'."""
    if chat_id is not None and message_thread_id is not None:
        configured = db.get_chat_topic_name_for_thread(chat_id, message_thread_id)
        if configured is not None:
            return configured
    return _ID_TO_NAME.get(message_thread_id, "unknown")


def is_adventure(chat_id: int | None, message_thread_id: int | None) -> bool:
    """True if this message came from this chat's own Adventure topic."""
    return message_thread_id is not None and message_thread_id == _resolve(chat_id, "adventure")


def is_development(chat_id: int | None, message_thread_id: int | None) -> bool:
    """True if this message came from this chat's own Development topic."""
    return message_thread_id is not None and message_thread_id == _resolve(chat_id, "development")


def is_support(chat_id: int | None, message_thread_id: int | None) -> bool:
    """True if this message came from this chat's own Support topic."""
    return message_thread_id is not None and message_thread_id == _resolve(chat_id, "support")


_GUILD_TOPIC_TO_ID = {v: k for k, v in config.GUILD_TOPIC_IDS.items() if v is not None}


def guild_id_for_topic(message_thread_id: int | None) -> str | None:
    """Returns the real guild_id this topic belongs to (task #77), or None if it isn't a guild topic. Home-group-only for now."""
    if message_thread_id is None:
        return None
    return _GUILD_TOPIC_TO_ID.get(message_thread_id)


def is_main(chat_id: int | None, message_thread_id: int | None) -> bool:
    """
    True if this message came from this chat's own Main topic.
    Telegram's General topic (which Main maps to) often sends
    message_thread_id as None rather than an explicit ID — that must be
    treated as Main, or every message in Main gets silently ignored.
    """
    return message_thread_id is None or message_thread_id == _resolve(chat_id, "main")
