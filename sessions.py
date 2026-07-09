"""
sessions.py
In-memory turn/session management for combat encounters, scoped to the
Adventure topic. A "session" tracks participants, turn order, whose
turn it is, and a rolling event log. Sessions are keyed by chat_id
(each Telegram group has exactly one active Adventure session at a time
in this simple design).

Every chat also gets its own asyncio.Lock (see get_lock()). This is
critical: without it, two players sending actions within moments of
each other can both read and mutate the same session concurrently,
producing exactly the kind of corrupted, out-of-order combat state
seen in real playtesting (wrong turn attribution, garbled HP, a
session that never resolves). Every handler that touches a session
MUST acquire this lock first.
"""
import asyncio
from dataclasses import dataclass, field

from rules.combat import start_combat


@dataclass
class Session:
    chat_id: int
    participants: list  # list of character dicts (mutated in place for HP etc.)
    turn_order: list  # list of telegram_user_id in initiative order
    sides: dict  # telegram_user_id -> "party" | "enemy"
    current_turn_index: int = 0
    round_number: int = 1
    event_log: list = field(default_factory=list)
    active: bool = True
    stabilized_ids: set = field(default_factory=set)

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


# chat_id -> Session
_ACTIVE_SESSIONS: dict[int, Session] = {}

# chat_id -> asyncio.Lock, so concurrent actions from different players in
# the same chat never mutate a session at the same time.
_CHAT_LOCKS: dict[int, asyncio.Lock] = {}


def get_lock(chat_id: int) -> asyncio.Lock:
    """Get (or create) the lock for a given chat. Always the same Lock
    object for a given chat_id, so 'async with sessions.get_lock(chat_id):'
    correctly serializes every action that touches that chat's state."""
    if chat_id not in _CHAT_LOCKS:
        _CHAT_LOCKS[chat_id] = asyncio.Lock()
    return _CHAT_LOCKS[chat_id]


def get_session(chat_id: int) -> Session | None:
    return _ACTIVE_SESSIONS.get(chat_id)


def start_session(chat_id: int, participants: list, sides: dict) -> Session:
    """
    Roll initiative for all participants and start a new combat session
    for this chat. Overwrites any existing session for this chat.
    `sides` maps telegram_user_id -> "party" or "enemy".
    """
    ordered = start_combat(list(participants))  # sorts by initiative, descending
    session = Session(
        chat_id=chat_id,
        participants=ordered,
        turn_order=[p["telegram_user_id"] for p in ordered],
        sides=dict(sides),
    )
    session.log_event(
        "Combat begins! Initiative order: "
        + ", ".join(f"{p['name']} ({p['initiative']})" for p in ordered)
    )
    _ACTIVE_SESSIONS[chat_id] = session
    return session


def end_session(chat_id: int) -> None:
    _ACTIVE_SESSIONS.pop(chat_id, None)
