"""
sessions.py
In-memory turn/session management for combat encounters, scoped to the
Adventure topic. A "session" tracks participants, turn order, whose
turn it is, and a rolling event log.

Sessions are keyed by a real session_id (2026-08-01 rewrite, per
Coffee: wanting real support for multiple simultaneous fights instead
of "only one fight active at a time, anywhere"). The OLD design keyed
_ACTIVE_SESSIONS by chat_id alone, which meant a single Telegram group
could only ever host one fight at once -- confirmed live and directly
felt during this project's own testing (starting a second fight while
any other player's fight was still resolving refused outright). A
single PLAYER still can never be in two fights at once (see
_USER_SESSION below), but two different parties in the same chat can
now fight completely independent encounters at the same time, with
independent locks so neither blocks the other.

Every SESSION gets its own asyncio.Lock (see get_session_lock()) so
concurrent actions from players in the SAME fight never mutate it at
the same time -- this is the same safety property the old per-chat
lock provided, just scoped correctly now that a chat can host more
than one fight. Starting a brand-new fight is guarded by a separate
per-chat lock (get_start_lock()), since there's no session yet to lock
at that point. Every handler that touches an existing session MUST
acquire that session's own lock first; every handler that might START
a new one MUST acquire the chat's start-lock first.
"""
import asyncio
import json
import os
import time
from dataclasses import dataclass, field

from rules.combat import start_combat

# Real live incident (2026-07-18): a deploy restart mid-combat wiped
# Sugar's in-progress fight entirely (turn order, both goblins' HP) since
# this module's whole design is in-memory-only -- Coffee: "that was not
# fair to players." Filed as task #159. This is a snapshot-to-disk safety
# net, NOT a redesign: sessions are still the live source of truth in
# memory during normal operation; this only exists so an unplanned or
# necessary restart doesn't erase an active encounter outright. A
# snapshot older than this is treated as too stale to resurrect (an
# abandoned encounter silently reappearing hours later would be more
# confusing than just starting fresh) -- matches the existing
# NATURAL_HEALING_FULL_REST_HOURS-style "how old is too old" convention
# already used elsewhere in this project.
SNAPSHOT_PATH = "sessions_snapshot.json"
SNAPSHOT_MAX_AGE_SECONDS = 2 * 60 * 60


@dataclass
class Session:
    chat_id: int
    participants: list  # list of character dicts (mutated in place for HP etc.)
    turn_order: list  # list of telegram_user_id in initiative order
    sides: dict  # telegram_user_id -> "party" | "enemy"
    session_id: int = 0  # assigned by start_session; 0 is never a real id
    current_turn_index: int = 0
    round_number: int = 1
    event_log: list = field(default_factory=list)
    active: bool = True
    stabilized_ids: set = field(default_factory=set)
    # Interactive combat environments (2026-07-27, per Coffee: "I want
    # interactive environments" -- battles beyond just attack/spell/
    # ability). A real, location-authored environmental feature (see
    # campaign.json's "combat_environment" field) can be used once per
    # fight, tracked here rather than on any single participant, since
    # it's a real fact about the BATTLEFIELD, not any one combatant.
    environment_used: bool = False

    def current_participant_id(self) -> int:
        return self.turn_order[self.current_turn_index]

    def current_participant(self) -> dict:
        pid = self.current_participant_id()
        return next(p for p in self.participants if p["telegram_user_id"] == pid)

    def opposing_side(self, telegram_user_id: int) -> str:
        side = self.sides[telegram_user_id]
        return "enemy" if side == "party" else "party"

    def living_on_side(self, side: str) -> list:
        return [
            p for p in self.participants
            if self.sides.get(p["telegram_user_id"]) == side and p["hp_current"] > 0
        ]

    def advance_turn(self) -> None:
        self.current_turn_index = (self.current_turn_index + 1) % len(self.turn_order)
        if self.current_turn_index == 0:
            self.round_number += 1

    def log_event(self, text: str) -> None:
        self.event_log.append(text)

    def recent_events(self, n: int = 10) -> list:
        return self.event_log[-n:]

    def remove_defeated(self) -> list[dict]:
        """
        Removes AI/monster participants at 0 HP outright (monsters just
        die in 5E, no death saves). Real players at 0 HP are NOT removed
        here — they stay in turn_order, unconscious, and roll death
        saves on their turns instead (see remove_dead_player for actual
        permanent player death after 3 failed saves).
        """
        removed = []
        still_alive_ids = []
        for pid in self.turn_order:
            character = next(p for p in self.participants if p["telegram_user_id"] == pid)
            if character["hp_current"] <= 0 and character.get("is_ai"):
                removed.append({
                    "name": character["name"],
                    "is_ai": True,
                    "telegram_user_id": pid,
                })
            else:
                still_alive_ids.append(pid)

        if removed:
            current_id = self.turn_order[self.current_turn_index] if self.turn_order else None
            self.turn_order = still_alive_ids
            if current_id in self.turn_order:
                self.current_turn_index = self.turn_order.index(current_id)
            else:
                self.current_turn_index = 0 if self.turn_order else 0

        return removed

    def remove_dead_player(self, telegram_user_id: int) -> None:
        """
        Permanently removes a real player from turn_order after they've
        failed 3 death saves — genuine death, unlike a monster's normal
        removal in remove_defeated().
        """
        if telegram_user_id not in self.turn_order:
            return
        current_id = self.turn_order[self.current_turn_index]
        self.turn_order.remove(telegram_user_id)
        if not self.turn_order:
            self.current_turn_index = 0
        elif current_id in self.turn_order:
            self.current_turn_index = self.turn_order.index(current_id)
        else:
            self.current_turn_index = 0

    def is_downed(self, telegram_user_id: int) -> bool:
        """
        True if this participant is a real player at 0 HP still rolling
        death saves. A STABILIZED player (3 successes) stays at 0 HP but
        is no longer in danger and stops rolling — real 5E rule.
        """
        character = next(p for p in self.participants if p["telegram_user_id"] == telegram_user_id)
        return (
            character["hp_current"] <= 0
            and not character.get("is_ai")
            and telegram_user_id not in self.stabilized_ids
        )

    def is_combat_over(self) -> bool:
        """
        A side is defeated only when NONE of its members remain in
        turn_order — NOT simply when their HP hits 0. This matters
        because a downed real player (0 HP, still rolling death saves)
        must keep their side "alive" until they actually die or
        stabilize/get revived; otherwise combat would incorrectly end
        the instant the last player drops, skipping death saves entirely.
        """
        party_remains = any(self.sides.get(pid) == "party" for pid in self.turn_order)
        enemy_remains = any(self.sides.get(pid) == "enemy" for pid in self.turn_order)
        return not party_remains or not enemy_remains

    def to_json_dict(self) -> dict:
        """
        Plain-JSON-safe representation for the disk snapshot -- `sides`'
        int keys become strings (JSON has no int keys) and `stabilized_ids`
        (a set) becomes a list; both are converted back in from_json_dict.
        """
        return {
            "chat_id": self.chat_id,
            "session_id": self.session_id,
            "participants": self.participants,
            "turn_order": self.turn_order,
            "sides": {str(k): v for k, v in self.sides.items()},
            "current_turn_index": self.current_turn_index,
            "round_number": self.round_number,
            "event_log": self.event_log,
            "active": self.active,
            "stabilized_ids": list(self.stabilized_ids),
            "environment_used": self.environment_used,
        }

    @classmethod
    def from_json_dict(cls, data: dict) -> "Session":
        return cls(
            chat_id=data["chat_id"],
            # .get(...) with a default (2026-08-01): a snapshot written by
            # the old chat_id-only design has no session_id at all -- the
            # caller (load_snapshot) assigns a fresh real one in that case.
            session_id=data.get("session_id", 0),
            participants=data["participants"],
            turn_order=data["turn_order"],
            sides={int(k): v for k, v in data["sides"].items()},
            current_turn_index=data["current_turn_index"],
            round_number=data["round_number"],
            event_log=data["event_log"],
            active=data["active"],
            stabilized_ids=set(data["stabilized_ids"]),
            # .get(...) with a default (2026-07-27): a snapshot written
            # by an older running process, mid-restart, won't have this
            # key yet -- must not crash restoring a real in-progress fight.
            environment_used=data.get("environment_used", False),
        )


# session_id -> Session. The real source of truth; everything else below
# is just an index over this dict for fast lookups.
_ACTIVE_SESSIONS: dict[int, Session] = {}

# chat_id -> set of session_ids currently active in that chat. Lets
# "what's happening in this chat right now" stay cheap without scanning
# every session in the game.
_CHAT_SESSIONS: dict[int, set[int]] = {}

# telegram_user_id -> session_id. The real "is this specific player
# already in a fight" index -- replaces the old "is ANY fight active in
# this chat" check, which incorrectly blocked an uninvolved player's
# actions just because someone else in the same chat was fighting
# (confirmed live, 2026-07-22, Sugar: "I'm not in battle why can't I
# travel?" -- fixed once already for fast_travel specifically; this
# rewrite makes the underlying data model actually support the fix
# everywhere instead of needing a one-off patch per call site).
_USER_SESSION: dict[int, int] = {}

# session_id -> asyncio.Lock, one per ACTIVE FIGHT. Two unrelated fights
# in the same chat never contend on the same lock, which is the entire
# point of this rewrite -- under the old per-chat lock, one fight's
# combat resolution could make an unrelated fight in the same chat wait
# on it for no real reason.
_SESSION_LOCKS: dict[int, asyncio.Lock] = {}

# chat_id -> asyncio.Lock, guarding "is anyone here about to start a new
# fight" -- distinct from a session lock, since there's no session yet
# to lock at that point. Two players in the same chat both trying to
# start a fight at the same instant must not race each other into two
# sessions over the same participants.
_START_LOCKS: dict[int, asyncio.Lock] = {}

_NEXT_SESSION_ID = 1


def _new_session_id() -> int:
    global _NEXT_SESSION_ID
    sid = _NEXT_SESSION_ID
    _NEXT_SESSION_ID += 1
    return sid


def get_start_lock(chat_id: int) -> asyncio.Lock:
    """
    Lock guarding "start a brand-new fight in this chat" specifically.
    Acquire this before calling start_session() (or before the
    'is anyone already in a fight' check that decides whether to call
    it), same discipline the old get_lock(chat_id) provided for every
    combat action -- now split out because an ONGOING fight's actions
    use get_session_lock(session_id) instead, so two unrelated fights
    never block each other.
    """
    if chat_id not in _START_LOCKS:
        _START_LOCKS[chat_id] = asyncio.Lock()
    return _START_LOCKS[chat_id]


def get_session_lock(session_id: int) -> asyncio.Lock:
    """Get (or create) the lock for one specific active fight. Always the
    same Lock object for a given session_id, so 'async with
    sessions.get_session_lock(session_id):' correctly serializes every
    action that touches THAT fight's state, without blocking any other
    fight."""
    if session_id not in _SESSION_LOCKS:
        _SESSION_LOCKS[session_id] = asyncio.Lock()
    return _SESSION_LOCKS[session_id]


def get_session_by_id(session_id: int) -> Session | None:
    return _ACTIVE_SESSIONS.get(session_id)


def get_session_for_user(chat_id: int, telegram_user_id: int) -> Session | None:
    """
    The real "is this player currently in a fight" lookup -- the
    per-user replacement for the old get_session(chat_id). Returns None
    if this player isn't a participant in any currently active session,
    regardless of how many OTHER fights might be happening in this same
    chat right now.
    """
    session_id = _USER_SESSION.get(telegram_user_id)
    if session_id is None:
        return None
    session = _ACTIVE_SESSIONS.get(session_id)
    # Defensive: a stale index entry should never crash a caller -- just
    # report "not in a fight" and let the index get cleaned up on the
    # next end_session/start_session pass.
    if session is None or telegram_user_id not in session.turn_order:
        return None
    return session


def get_sessions_in_chat(chat_id: int) -> list[Session]:
    """Every currently active fight in this chat (zero, one, or many)."""
    return [_ACTIVE_SESSIONS[sid] for sid in _CHAT_SESSIONS.get(chat_id, set()) if sid in _ACTIVE_SESSIONS]


def get_session(chat_id: int) -> Session | None:
    """
    Back-compat shim for the old chat-wide lookup: returns a fight
    happening in this chat if there's EXACTLY one (the common case,
    including every real fight today), None if there are zero, and the
    FIRST one found if there happen to be several (callers that need to
    tell fights apart should use get_session_for_user or
    get_sessions_in_chat instead -- this exists so call sites that
    genuinely only care about the old chat-wide question don't all need
    to change on day one of the rewrite).
    """
    sessions_here = get_sessions_in_chat(chat_id)
    return sessions_here[0] if sessions_here else None


def get_lock(chat_id: int) -> asyncio.Lock:
    """
    Back-compat shim: the old per-chat lock. New code should use
    get_start_lock(chat_id) when deciding whether to start a fight, or
    get_session_lock(session_id) when acting within one already found
    via get_session_for_user -- kept as an alias of get_start_lock so
    any call site not yet migrated still serializes fight-START
    attempts correctly rather than breaking outright.
    """
    return get_start_lock(chat_id)


def start_session(chat_id: int, participants: list, sides: dict) -> Session | None:
    """
    Roll initiative for all participants and start a NEW combat session
    for this chat. Unlike the old design, this does NOT overwrite an
    existing fight -- a chat can now host more than one at once. Returns
    None (starting nothing) if any incoming participant is already a
    participant in some other active fight, anywhere, rather than
    silently drafting them into a second one or clobbering the fight
    they were already in.
    `sides` maps telegram_user_id -> "party" or "enemy".
    """
    for p in participants:
        if p["telegram_user_id"] in _USER_SESSION:
            return None

    ordered = start_combat(list(participants))  # sorts by initiative, descending
    session_id = _new_session_id()
    session = Session(
        chat_id=chat_id,
        session_id=session_id,
        participants=ordered,
        turn_order=[p["telegram_user_id"] for p in ordered],
        sides=dict(sides),
    )
    session.log_event(
        "Combat begins! Initiative order: "
        + ", ".join(f"{p['name']} ({p['initiative']})" for p in ordered)
    )
    _ACTIVE_SESSIONS[session_id] = session
    _CHAT_SESSIONS.setdefault(chat_id, set()).add(session_id)
    for pid in session.turn_order:
        _USER_SESSION[pid] = session_id
    save_snapshot()
    return session


def join_session(session: Session, character: dict, side: str) -> None:
    """
    Adds a new participant to an ALREADY-RUNNING fight (2026-08-01, e.g.
    _do_join_battle: a player traveling to where combat is already in
    progress after missing the initial roll-call). Caller must already
    hold this session's own lock (get_session_lock(session.session_id))
    before calling this. Keeps _USER_SESSION in sync the same way
    start_session does -- this is the only other place a participant
    gets added to turn_order after a fight has already begun.
    """
    telegram_user_id = character["telegram_user_id"]
    session.participants.append(character)
    session.sides[telegram_user_id] = side
    session.turn_order.append(telegram_user_id)
    _USER_SESSION[telegram_user_id] = session.session_id
    save_snapshot()


def end_session(chat_id: int, session: Session | None = None) -> None:
    """
    Ends one fight. `session` should be passed whenever the caller
    already has it (the common case now that a chat can host more than
    one) -- if omitted, falls back to ending whichever single fight
    get_session(chat_id) would find, for callers not yet migrated. A
    no-op if there's nothing to end.
    """
    if session is None:
        session = get_session(chat_id)
    if session is None:
        return
    _ACTIVE_SESSIONS.pop(session.session_id, None)
    _CHAT_SESSIONS.get(chat_id, set()).discard(session.session_id)
    for pid in list(session.turn_order):
        if _USER_SESSION.get(pid) == session.session_id:
            _USER_SESSION.pop(pid, None)
    save_snapshot()


def save_snapshot() -> None:
    """
    Writes every active session to disk (task #159 -- see the module
    docstring note above for why). Best-effort: a failure to write here
    should never break the actual game action that triggered it, so any
    exception is swallowed after logging. Written via a temp file +
    os.replace so a crash mid-write can never leave a half-written,
    unparseable snapshot behind for the next startup to choke on.
    """
    try:
        payload = {
            "saved_at": time.time(),
            "sessions": [s.to_json_dict() for s in _ACTIVE_SESSIONS.values()],
        }
        tmp_path = SNAPSHOT_PATH + ".tmp"
        with open(tmp_path, "w") as f:
            json.dump(payload, f)
        os.replace(tmp_path, SNAPSHOT_PATH)
    except Exception as e:  # noqa: BLE001 -- best-effort, never fatal to the caller
        print(f"[sessions] failed to save snapshot: {e}")


def load_snapshot() -> int:
    """
    Restores active sessions from disk at startup (task #159). Returns
    how many sessions were actually restored, so the caller can log/
    announce it. A snapshot older than SNAPSHOT_MAX_AGE_SECONDS is
    deliberately ignored (see module docstring) -- an old, abandoned
    encounter silently reappearing would be worse than just not
    restoring it. Missing or corrupt files are treated the same as "no
    snapshot" rather than crashing bot startup.
    """
    if not os.path.exists(SNAPSHOT_PATH):
        return 0
    try:
        with open(SNAPSHOT_PATH) as f:
            payload = json.load(f)
        if time.time() - payload.get("saved_at", 0) > SNAPSHOT_MAX_AGE_SECONDS:
            os.remove(SNAPSHOT_PATH)
            return 0
        restored = 0
        for data in payload.get("sessions", []):
            session = Session.from_json_dict(data)
            if not session.session_id:
                session.session_id = _new_session_id()
            _ACTIVE_SESSIONS[session.session_id] = session
            _CHAT_SESSIONS.setdefault(session.chat_id, set()).add(session.session_id)
            for pid in session.turn_order:
                _USER_SESSION[pid] = session.session_id
            restored += 1
        return restored
    except Exception as e:  # noqa: BLE001 -- corrupt/partial file must not block startup
        print(f"[sessions] failed to load snapshot, starting with no active sessions: {e}")
        return 0
