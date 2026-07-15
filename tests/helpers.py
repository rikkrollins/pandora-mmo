"""
tests/helpers.py
Shared fixtures for Pandora MMO's regression suite -- extracted from
the throwaway bot_test_tmp.py scripts used throughout development
(see CLAUDE.md's testing convention) so permanent regression tests
don't each reinvent the same fake Telegram objects and DB setup.

Usage:
    from tests.helpers import use_test_db, FakeUpdate, DummyContext
    import bot

    class MyTest(unittest.TestCase):
        def setUp(self):
            use_test_db("tests/tmp/my_test.db")
"""
import os

import config
import db


def use_test_db(path: str) -> None:
    """
    Points every db.* call at a fresh, throwaway SQLite file instead of
    the live pandora_mmo.db. Safe to call after `import db`/`import
    bot` -- config.DB_PATH is read at call time (see db.get_connection),
    not import time, so mutating it here takes effect immediately.
    """
    if os.path.exists(path):
        os.remove(path)
    journal = path + "-journal"
    if os.path.exists(journal):
        os.remove(journal)
    config.DB_PATH = path
    db.init_db()


class FakeMessage:
    def __init__(self, text, thread_id=None):
        self.text = text
        self.message_thread_id = thread_id if thread_id is not None else config.TOPIC_ADVENTURE_ID


class FakeChat:
    def __init__(self, sink):
        self.id = -999
        self._sink = sink

    async def send_message(self, text, **kwargs):
        self._sink.append(text)


class FakeUser:
    def __init__(self, user_id):
        self.id = user_id


class FakeUpdate:
    """
    A minimal stand-in for python-telegram-bot's Update, real enough to
    drive bot.py's actual handlers (adventure_master_handler,
    support_topic_handler, etc.) end-to-end. `sink` collects every
    message the handler sends, in order.
    """
    def __init__(self, user_id, text, sink, thread_id=None):
        self.effective_user = FakeUser(user_id)
        self.effective_chat = FakeChat(sink)
        self.message = FakeMessage(text, thread_id)
        self.effective_message = self.message


class DummyContext:
    def __init__(self):
        self.user_data = {}


def make_basic_character(user_id: int, name: str = "Elduinn", **overrides) -> dict:
    """A real, minimal Fighter character row for tests that don't care about build specifics."""
    fields = dict(
        telegram_user_id=user_id, name=name, race="Human", char_class="Fighter",
        ability_scores={
            "strength": 15, "dexterity": 14, "constitution": 13,
            "intelligence": 10, "wisdom": 10, "charisma": 10,
        },
        hp_max=12, armor_class=15, gold=10, inventory={}, known_spells=[],
    )
    fields.update(overrides)
    return db.create_character(**fields)
