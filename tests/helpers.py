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
    def __init__(self, text, thread_id=None, reply_to_message=None):
        self.text = text
        self.message_thread_id = thread_id if thread_id is not None else config.TOPIC_ADVENTURE_ID
        self.reply_to_message = reply_to_message


class FakeSentMessage:
    """Real enough to stand in for python-telegram-bot's Message where a test needs .delete()."""
    def __init__(self, sink):
        self.deleted = False
        self._sink = sink

    async def delete(self):
        self.deleted = True
        self._sink.append("<deleted>")


class FakeChat:
    def __init__(self, sink):
        self.id = -999
        self._sink = sink
        self.last_sent_message = None

    async def send_message(self, text, **kwargs):
        self._sink.append(text)
        self.last_sent_message = FakeSentMessage(self._sink)
        return self.last_sent_message


class FakeUser:
    def __init__(self, user_id, username=None, full_name=None):
        self.id = user_id
        self.username = username
        self.full_name = full_name if full_name is not None else f"User{user_id}"


class FakeUpdate:
    """
    A minimal stand-in for python-telegram-bot's Update, real enough to
    drive bot.py's actual handlers (adventure_master_handler,
    support_topic_handler, etc.) end-to-end. `sink` collects every
    message the handler sends, in order. `reply_to_message` should be
    another FakeUpdate (or anything with an effective_user) when a test
    needs to simulate replying to someone else's message (e.g.
    /add_admin, /ban).
    """
    def __init__(self, user_id, text, sink, thread_id=None, reply_to_message=None):
        self.effective_user = FakeUser(user_id)
        self.effective_chat = FakeChat(sink)
        self.message = FakeMessage(text, thread_id, reply_to_message=reply_to_message)
        self.effective_message = self.message


class DummyMessage:
    """Bare reply_to_message target -- just enough for reply.from_user resolution."""
    def __init__(self, user_id, username=None, full_name=None):
        self.from_user = FakeUser(user_id, username=username, full_name=full_name)


class FakeChatMember:
    def __init__(self, status):
        self.status = status


class FakeBot:
    """
    Minimal stand-in for python-telegram-bot's Bot, just enough to
    drive code paths that call context.bot.get_chat_member (e.g.
    _is_group_owner/_is_group_admin_or_owner in bot.py). `status`
    controls what every get_chat_member call reports back.
    """
    def __init__(self, status="creator"):
        self.status = status

    async def get_chat_member(self, chat_id, user_id):
        return FakeChatMember(self.status)


class DummyContext:
    def __init__(self, bot=None, args=None):
        self.user_data = {}
        self.bot = bot if bot is not None else FakeBot()
        self.args = args if args is not None else []


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
