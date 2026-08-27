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
from ai import narration_cache


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

    Also isolates ai.narration_cache's own SQLite file (found live,
    2026-08-22): CACHE_DB_PATH is a fixed path relative to ai/narration_
    cache.py's own __file__, entirely independent of config.DB_PATH --
    NOT redirected by the two fixes above. Any test exercising real
    combat narration (mocked or not) writes its own mocked flavor text
    ("A blow lands.", "The goblins attack.", etc.) into this SAME file
    the live bot reads from for real players' narration, since
    cache_key() buckets only on {actor class}:{outcome}:{damage tier} --
    coarse enough that a mocked test string and a real player's next
    matching hit collide. Confirmed live: 138 of 373 real cache rows
    were exactly this kind of test-mock contamination before this fix
    (cleaned up separately, this only prevents new contamination).
    """
    if os.path.exists(path):
        os.remove(path)
    journal = path + "-journal"
    if os.path.exists(journal):
        os.remove(journal)
    config.DB_PATH = path
    db.init_db()
    sessions.SNAPSHOT_PATH = path + ".sessions_snapshot.json"
    narration_cache.CACHE_DB_PATH = path + ".narration_cache.db"
    # 2026-08-01 multi-fight rewrite: sessions.py replaced the old single
    # _ACTIVE_SESSIONS (chat_id-keyed)/_CHAT_LOCKS pair with a real
    # session_id-based index (_ACTIVE_SESSIONS is now session_id-keyed,
    # plus _CHAT_SESSIONS/_USER_SESSION/_SESSION_LOCKS/_START_LOCKS) --
    # every one of these needs resetting here, not just the old two, or a
    # previous test's fake session_ids/locks leak into the next test (and,
    # per this exact function's own docstring above, into the real
    # snapshot) the same way the old _ACTIVE_SESSIONS leak once did.
    sessions._ACTIVE_SESSIONS.clear()
    sessions._CHAT_SESSIONS.clear()
    sessions._USER_SESSION.clear()
    sessions._SESSION_LOCKS.clear()
    sessions._START_LOCKS.clear()
    sessions._NEXT_SESSION_ID = 1

    # bot._ai_turn_pacing_delay (2026-08-23, real live flood-control
    # fix -- see its own docstring in bot.py) deliberately sleeps
    # between each resolved AI turn in a real fight. That's purely a
    # live-Telegram-traffic concern, not game logic, so it's neutralized
    # to a no-op here for the whole test suite -- otherwise every
    # multi-turn combat test would pick up real wall-clock delay for no
    # test-relevant reason. Lazy import to avoid a circular import at
    # module load time (bot.py itself doesn't import tests/helpers.py,
    # but importing it here at module scope would still force bot.py to
    # fully load before some test modules are ready for that).
    import bot
    async def _no_pacing_delay() -> None:
        return None
    bot._ai_turn_pacing_delay = _no_pacing_delay

    # Real live incident (2026-08-27, same class as sessions.SNAPSHOT_PATH
    # above): bot._SUPPORT_FEEDBACK_STATE_PATH is a bare relative filename
    # too, resolved against the process's cwd at save time -- any test
    # exercising a Support answer or the vote-button callback (without
    # this) would silently write real-looking pending-feedback data into
    # the actual repo's support_feedback_pending.json, confirmed live
    # while adding this feature's own tests.
    bot._SUPPORT_FEEDBACK_STATE_PATH = path + ".support_feedback_pending.json"
    bot._SUPPORT_FEEDBACK_LOG.clear()
    bot._SUPPORT_FEEDBACK_NEXT_ID = 1


class FakeMessage:
    def __init__(self, text, thread_id=None, reply_to_message=None):
        self.text = text
        self.message_thread_id = thread_id if thread_id is not None else config.TOPIC_ADVENTURE_ID
        self.reply_to_message = reply_to_message


class FakeSentMessage:
    """Real enough to stand in for python-telegram-bot's Message where a test needs .delete() or .message_id."""
    def __init__(self, sink, message_id=None):
        self.deleted = False
        self.pinned = False
        self._sink = sink
        self.message_id = message_id

    async def delete(self):
        self.deleted = True
        self._sink.append("<deleted>")

    async def pin(self, disable_notification=False):
        self.pinned = True
        self._sink.append(f"<pinned:{self.message_id}>")


class FakeChat:
    def __init__(self, sink, chat_id=-999, title=None, type="group"):
        self.id = chat_id
        self.title = title
        self.type = type
        self._sink = sink
        self.last_sent_message = None
        self.sent_photos = []
        self.sent_thread_ids = []
        self._next_message_id = 1

    async def send_message(self, text, **kwargs):
        self._sink.append(text)
        self.sent_thread_ids.append(kwargs.get("message_thread_id"))
        self.last_sent_message = FakeSentMessage(self._sink, message_id=self._next_message_id)
        self._next_message_id += 1
        return self.last_sent_message

    async def send_photo(self, photo, caption=None, **kwargs):
        self.sent_photos.append({"photo": photo, "caption": caption, "reply_markup": kwargs.get("reply_markup")})
        self._sink.append(f"<photo:{caption}>")
        sent = FakeSentMessage(self._sink, message_id=self._next_message_id)
        self._next_message_id += 1
        self.last_sent_message = sent
        return sent

    async def unpin_message(self, message_id=None):
        self._sink.append(f"<unpinned:{message_id}>")


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
    def __init__(self, user_id, text, sink, thread_id=None, reply_to_message=None, chat_id=None, chat_title=None):
        self.effective_user = FakeUser(user_id)
        self.effective_chat = FakeChat(sink, chat_id=chat_id if chat_id is not None else -999, title=chat_title)
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
        self.last_edited_text = None

    async def answer(self, text=None, show_alert=False):
        self.answered = text or "<ack>"
        self._sink.append(f"<answer:{self.answered}>")

    async def edit_message_reply_markup(self, reply_markup=None):
        self._sink.append(f"<edit_markup:{reply_markup}>")

    async def edit_message_text(self, text, reply_markup=None):
        self.last_edited_text = text
        self._sink.append(text)


class FakeCallbackUpdate:
    """A FakeUpdate-equivalent for a button tap (callback_query), not a typed message."""
    def __init__(self, user_id, data, sink, chat_id=None):
        self.effective_user = FakeUser(user_id)
        self.effective_chat = FakeChat(sink, chat_id=chat_id if chat_id is not None else -999)
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
        telegram_user_id=user_id, chat_id=-999, name=name, race="Human", char_class="Fighter",
        ability_scores={
            "strength": 15, "dexterity": 14, "constitution": 13,
            "intelligence": 10, "wisdom": 10, "charisma": 10,
        },
        hp_max=12, armor_class=15, gold=10, inventory={}, known_spells=[],
    )
    fields.update(overrides)
    return db.create_character(**fields)
