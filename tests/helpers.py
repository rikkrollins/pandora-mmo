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
import sessions


def use_test_db(path: str) -> None:
    """
    Points every db.* call at a fresh, throwaway SQLite file instead of
    the live pandora_mmo.db. Safe to call after `import db`/`import
    bot` -- config.DB_PATH is read at call time (see db.get_connection),
    not import time, so mutating it here takes effect immediately.

    Also isolates sessions.py's combat-session state (2026-07-19, real
    incident found live): sessions.SNAPSHOT_PATH is a bare relative
    filename ("sessions_snapshot.json"), resolved against the process's
    cwd at save time -- NOT anything config.DB_PATH controls. A
    throwaway test script run from ~/pandora_mmo (the same cwd the real
    bot runs from) that imports sessions and calls start_session() will
    silently overwrite the LIVE bot's real sessions_snapshot.json with
    fake test combat data the moment save_snapshot() runs, with no
    error or warning at all -- this actually happened (a "Ravenloft"
    vs. two goblins test fixture ended up in the live snapshot file).
    Redirecting SNAPSHOT_PATH here and resetting the in-memory session
    dicts makes tests that touch sessions.py safe by default, the same
    way config.DB_PATH already protects db.py.
    """
    if os.path.exists(path):
        os.remove(path)
    journal = path + "-journal"
    if os.path.exists(journal):
        os.remove(journal)
    config.DB_PATH = path
    db.init_db()
    sessions.SNAPSHOT_PATH = path + ".sessions_snapshot.json"
    sessions._ACTIVE_SESSIONS.clear()
    sessions._CHAT_LOCKS.clear()


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
        self.sent_photos = []

    async def send_message(self, text, **kwargs):
        self._sink.append(text)
        self.last_sent_message = FakeSentMessage(self._sink)
        return self.last_sent_message

    async def send_photo(self, photo, caption=None, **kwargs):
        self.sent_photos.append({"photo": photo, "caption": caption})
        self._sink.append(f"<photo:{caption}>")
        return FakeSentMessage(self._sink)


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


class FakeCallbackQuery:
    """
    Minimal stand-in for python-telegram-bot's CallbackQuery, real enough
    to drive the button-tap handlers (battle_menu_callback and task #176's
    shop_menu_callback/spell_menu_callback/quest_menu_callback) end-to-end.
    `sink` collects .answer() calls the same way FakeChat collects sent
    messages, so a test can assert a tap was acknowledged.
    """
    def __init__(self, data, sink):
        self.data = data
        self._sink = sink
        self.answered = None

    async def answer(self, text=None, show_alert=False):
        self.answered = text or "<ack>"
        self._sink.append(f"<answer:{self.answered}>")

    async def edit_message_reply_markup(self, reply_markup=None):
        self._sink.append(f"<edit_markup:{reply_markup}>")


class FakeCallbackUpdate:
    """A FakeUpdate-equivalent for a button tap (callback_query), not a typed message."""
    def __init__(self, user_id, data, sink):
        self.effective_user = FakeUser(user_id)
        self.effective_chat = FakeChat(sink)
        self.callback_query = FakeCallbackQuery(data, sink)
        self.message = None
        self.effective_message = None


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
