"""
version.py
Reads the bot's version and changelog from plain files on disk (VERSION,
CHANGELOG.md) — the single source of truth, updated by hand or by a
release process, not auto-fetched from anywhere at runtime. No GitHub
polling, no background network calls: you control when this changes.
"""
import os

_BASE_DIR = os.path.dirname(__file__)
_VERSION_FILE = os.path.join(_BASE_DIR, "VERSION")
_CHANGELOG_FILE = os.path.join(_BASE_DIR, "CHANGELOG.md")


def get_version() -> str:
    """Returns the current version string, e.g. '1.0.0'. Falls back to 'unknown' if missing."""
    try:
        with open(_VERSION_FILE, "r", encoding="utf-8") as f:
            return f.read().strip()
    except FileNotFoundError:
        return "unknown"


def get_changelog(max_chars: int = 3500) -> str:
    """
    Returns the changelog text, truncated to a Telegram-friendly length
    if needed (Telegram messages have a hard 4096-character limit).
    """
    try:
        with open(_CHANGELOG_FILE, "r", encoding="utf-8") as f:
            text = f.read().strip()
    except FileNotFoundError:
        return "No changelog available."

    if len(text) > max_chars:
        return text[:max_chars].rstrip() + "\n\n... (truncated, see CHANGELOG.md for the full list)"
    return text
