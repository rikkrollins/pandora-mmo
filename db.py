"""
db.py
SQLite-backed persistence for characters (and later, sessions/NPCs).
Inventory is stored as a JSON-encoded string in the characters table.

Character slots: a telegram_user_id can own MULTIPLE character rows
(character_id is the real primary key). Exactly one of a user's
characters is "active" at a time, tracked in the active_characters
table — that's the character every other lookup by telegram_user_id
resolves to. This keeps every existing call site (which all pass a
telegram_user_id, never a character_id) working unchanged: they always
mean "my currently active character."
"""
import json
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone

import config
from rules.leveling import (
    level_for_xp, proficiency_bonus_for_level,
    hp_gain_for_level, ASI_LEVELS, CLASS_PRIMARY_ABILITY,
)
import spells as spells_module

CREATE_CHARACTERS_TABLE = """
CREATE TABLE IF NOT EXISTS characters (
    character_id INTEGER PRIMARY KEY AUTOINCREMENT,
    telegram_user_id INTEGER NOT NULL,
    name TEXT NOT NULL,
    race TEXT NOT NULL,
    char_class TEXT NOT NULL,
    level INTEGER NOT NULL DEFAULT 1,
    xp INTEGER NOT NULL DEFAULT 0,
    hp_current INTEGER NOT NULL,
    hp_max INTEGER NOT NULL,
    strength INTEGER NOT NULL,
    dexterity INTEGER NOT NULL,
    constitution INTEGER NOT NULL,
    intelligence INTEGER NOT NULL,
    wisdom INTEGER NOT NULL,
    charisma INTEGER NOT NULL,
    proficiency_bonus INTEGER NOT NULL DEFAULT 2,
    armor_class INTEGER NOT NULL,
    inventory TEXT NOT NULL DEFAULT '{}',
    gold INTEGER NOT NULL DEFAULT 0,
    death_save_successes INTEGER NOT NULL DEFAULT 0,
    death_save_failures INTEGER NOT NULL DEFAULT 0,
    is_ai INTEGER NOT NULL DEFAULT 0,
    current_location TEXT NOT NULL DEFAULT 'crossroads_tavern',
    known_spells TEXT NOT NULL DEFAULT '[]',
    guild TEXT,
    completed_quests TEXT NOT NULL DEFAULT '[]',
    spell_slots_max INTEGER NOT NULL DEFAULT 0,
    spell_slots_current INTEGER NOT NULL DEFAULT 0,
    visited_locations TEXT NOT NULL DEFAULT '[]',
    is_deleted INTEGER NOT NULL DEFAULT 0,
    skill_uses TEXT NOT NULL DEFAULT '{}',
    is_inactive INTEGER NOT NULL DEFAULT 0,
    last_active_at TEXT,
    active_quests TEXT NOT NULL DEFAULT '{}'
);
"""

CREATE_ACTIVE_CHARACTERS_TABLE = """
CREATE TABLE IF NOT EXISTS active_characters (
    telegram_user_id INTEGER PRIMARY KEY,
    character_id INTEGER NOT NULL
);
"""

# Per-(player, NPC) relationship — persists across restarts, unlike the
# in-memory conversation buffer in ai/npc_agent.py. affinity is a simple
# running rapport score; memory_events is a JSON list of short factual
# strings ("stole from my shop", "helped fight off bandits") an NPC can
# be reminded of in future conversations/ambient lines; banned is a real
# consequence (e.g. a shopkeeper refusing to sell to a caught thief).
CREATE_NPC_RELATIONSHIPS_TABLE = """
CREATE TABLE IF NOT EXISTS npc_relationships (
    telegram_user_id INTEGER NOT NULL,
    npc_id TEXT NOT NULL,
    affinity INTEGER NOT NULL DEFAULT 0,
    memory_events TEXT NOT NULL DEFAULT '[]',
    banned INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (telegram_user_id, npc_id)
);
"""

# Per-(player, faction) standing — a faction's disposition toward a
# player evolves from what they actually do to its members (see
# bot.py's _adjust_faction_standing_for_npc), not from anything invented.
CREATE_FACTION_STANDING_TABLE = """
CREATE TABLE IF NOT EXISTS faction_standing (
    telegram_user_id INTEGER NOT NULL,
    faction_id TEXT NOT NULL,
    standing INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (telegram_user_id, faction_id)
);
"""

# Area quest board — distinct from a character's personal quest journal
# (active_quests/completed_quests on the characters table, which only
# ever covers the hand-authored story quests). A board quest is a
# repeatable, generated bounty tied to a LOCATION rather than to any
# one character: anyone there can see it and accept it, it expires 24h
# after being accepted if not finished (returning to the board for
# someone else), and one gets generated per location per real day, so
# there's something new to do without touching the main story arcs.
CREATE_BOARD_QUESTS_TABLE = """
CREATE TABLE IF NOT EXISTS board_quests (
    board_quest_id INTEGER PRIMARY KEY AUTOINCREMENT,
    location_id TEXT NOT NULL,
    day_key TEXT NOT NULL,
    title TEXT NOT NULL,
    description TEXT NOT NULL,
    giver_npc TEXT,
    objective_type TEXT NOT NULL,
    objective_target TEXT NOT NULL,
    objective_count INTEGER NOT NULL DEFAULT 1,
    reward_xp INTEGER NOT NULL DEFAULT 0,
    reward_gold INTEGER NOT NULL DEFAULT 0,
    generated_at TEXT NOT NULL,
    accepted_by INTEGER,
    accepted_at TEXT,
    expires_at TEXT,
    progress_count INTEGER NOT NULL DEFAULT 0,
    completed_at TEXT,
    branch_data TEXT
);
"""

# Real, explicit party membership -- deliberately separate from combat
# grouping (every active character still fights together regardless of
# party, per Coffee's explicit call 2026-07-10: this is a social/roster
# construct, not a change to who's on whose side in a fight). Up to 6
# members, invite/accept-based, leave any time.
CREATE_PARTIES_TABLE = """
CREATE TABLE IF NOT EXISTS parties (
    party_id INTEGER PRIMARY KEY AUTOINCREMENT,
    created_by INTEGER NOT NULL,
    created_at TEXT NOT NULL
);
"""

PARTY_MAX_MEMBERS = 6


@contextmanager
def get_connection():
    conn = sqlite3.connect(config.DB_PATH)
    conn.row_factory = sqlite3.Row
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


def _existing_columns(conn, table: str) -> set[str]:
    return {row["name"] for row in conn.execute(f"PRAGMA table_info({table})")}


def init_db() -> None:
    """
    Create tables if they don't already exist. If an OLDER schema is
    found (characters table without character_id — the pre-slots,
    one-row-per-user schema), migrate in place: safe to drop and
    recreate ONLY because that old table is empty in practice; if it
    somehow has rows, refuse rather than silently discarding real
    player data.
    """
    with get_connection() as conn:
        old_table_exists = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='characters'"
        ).fetchone()
        if old_table_exists:
            columns = _existing_columns(conn, "characters")
            if "character_id" not in columns:
                row_count = conn.execute("SELECT COUNT(*) AS n FROM characters").fetchone()["n"]
                if row_count > 0:
                    raise RuntimeError(
                        "characters table uses the pre-character-slots schema and has "
                        f"{row_count} existing row(s) — refusing to auto-migrate live data. "
                        "A manual migration script is needed."
                    )
                conn.execute("DROP TABLE characters")

        conn.execute(CREATE_CHARACTERS_TABLE)
        conn.execute(CREATE_ACTIVE_CHARACTERS_TABLE)
        conn.execute(CREATE_NPC_RELATIONSHIPS_TABLE)
        conn.execute(CREATE_FACTION_STANDING_TABLE)
        conn.execute(CREATE_BOARD_QUESTS_TABLE)
        conn.execute(CREATE_PARTIES_TABLE)

        # Older DBs created before fog-of-war/character-slots/proficiency
        # may already have the new characters table but be missing later
        # columns — add them if absent.
        columns = _existing_columns(conn, "characters")
        if "visited_locations" not in columns:
            conn.execute("ALTER TABLE characters ADD COLUMN visited_locations TEXT NOT NULL DEFAULT '[]'")
        if "is_deleted" not in columns:
            conn.execute("ALTER TABLE characters ADD COLUMN is_deleted INTEGER NOT NULL DEFAULT 0")
        if "skill_uses" not in columns:
            conn.execute("ALTER TABLE characters ADD COLUMN skill_uses TEXT NOT NULL DEFAULT '{}'")
        if "is_inactive" not in columns:
            conn.execute("ALTER TABLE characters ADD COLUMN is_inactive INTEGER NOT NULL DEFAULT 0")
        if "last_active_at" not in columns:
            conn.execute("ALTER TABLE characters ADD COLUMN last_active_at TEXT")
        if "active_quests" not in columns:
            conn.execute("ALTER TABLE characters ADD COLUMN active_quests TEXT NOT NULL DEFAULT '{}'")

        board_quest_columns = _existing_columns(conn, "board_quests")
        if "branch_data" not in board_quest_columns:
            conn.execute("ALTER TABLE board_quests ADD COLUMN branch_data TEXT")

        if "party_id" not in columns:
            conn.execute("ALTER TABLE characters ADD COLUMN party_id INTEGER")
        if "pending_party_invite" not in columns:
            conn.execute("ALTER TABLE characters ADD COLUMN pending_party_invite INTEGER")
        if "is_autonomous" not in columns:
            conn.execute("ALTER TABLE characters ADD COLUMN is_autonomous INTEGER NOT NULL DEFAULT 0")


def _row_to_dict(row: sqlite3.Row) -> dict:
    d = dict(row)
    d["inventory"] = json.loads(d["inventory"])
    d["known_spells"] = json.loads(d["known_spells"])
    d["completed_quests"] = json.loads(d["completed_quests"])
    d["visited_locations"] = json.loads(d["visited_locations"])
    d["skill_uses"] = json.loads(d["skill_uses"])
    d["active_quests"] = json.loads(d["active_quests"])
    return d


def _active_character_id(telegram_user_id: int, conn=None) -> int | None:
    def _lookup(c):
        row = c.execute(
            "SELECT character_id FROM active_characters WHERE telegram_user_id = ?",
            (telegram_user_id,),
        ).fetchone()
        return row["character_id"] if row else None

    if conn is not None:
        return _lookup(conn)
    with get_connection() as c:
        return _lookup(c)


def _set_active_character(telegram_user_id: int, character_id: int, conn) -> None:
    conn.execute(
        """
        INSERT INTO active_characters (telegram_user_id, character_id) VALUES (?, ?)
        ON CONFLICT(telegram_user_id) DO UPDATE SET character_id = excluded.character_id
        """,
        (telegram_user_id, character_id),
    )


def create_character(telegram_user_id: int, name: str, race: str, char_class: str,
                      ability_scores: dict, hp_max: int, armor_class: int,
                      gold: int, inventory: dict, is_ai: bool = False,
                      known_spells: list | None = None,
                      current_location: str = "crossroads_tavern",
                      spell_slots_max: int = 0) -> dict:
    """
    Always creates a NEW character row (a new slot) and makes it this
    telegram_user_id's active character. A user can own many characters;
    only the active one is what every other db.* function by
    telegram_user_id operates on.
    """
    with get_connection() as conn:
        cur = conn.execute(
            """
            INSERT INTO characters (
                telegram_user_id, name, race, char_class, level, xp,
                hp_current, hp_max, strength, dexterity, constitution,
                intelligence, wisdom, charisma, proficiency_bonus,
                armor_class, inventory, gold, is_ai, current_location, known_spells,
                spell_slots_max, spell_slots_current, visited_locations
            ) VALUES (?, ?, ?, ?, 1, 0, ?, ?, ?, ?, ?, ?, ?, ?, 2, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                telegram_user_id, name, race, char_class,
                hp_max, hp_max,
                ability_scores["strength"], ability_scores["dexterity"],
                ability_scores["constitution"], ability_scores["intelligence"],
                ability_scores["wisdom"], ability_scores["charisma"],
                armor_class, json.dumps(inventory), gold, int(is_ai),
                current_location, json.dumps(known_spells or []),
                spell_slots_max, spell_slots_max, json.dumps([current_location]),
            ),
        )
        character_id = cur.lastrowid
        _set_active_character(telegram_user_id, character_id, conn)
    return get_character(telegram_user_id)


_NEXT_AI_ID = -1000  # AI companions use negative synthetic IDs (never collide with real Telegram user IDs)


def create_ai_companion(name: str, race: str, char_class: str, ability_scores: dict,
                         hp_max: int, armor_class: int, gold: int, inventory: dict) -> dict:
    """
    Create an AI-controlled companion character. Uses a synthetic negative
    telegram_user_id so it can never collide with a real player's ID, and
    is flagged is_ai=1 so the turn engine knows to auto-resolve its turns.
    It is its own "owner" for active_characters purposes (self-mapped).
    """
    global _NEXT_AI_ID
    ai_id = _NEXT_AI_ID
    _NEXT_AI_ID -= 1
    return create_character(
        telegram_user_id=ai_id, name=name, race=race, char_class=char_class,
        ability_scores=ability_scores, hp_max=hp_max, armor_class=armor_class,
        gold=gold, inventory=inventory, is_ai=True,
    )


def update_character(telegram_user_id: int, **fields) -> dict | None:
    if not fields:
        return get_character(telegram_user_id)

    json_fields = ("inventory", "known_spells", "completed_quests", "visited_locations", "skill_uses", "active_quests")
    for key in json_fields:
        if key in fields and not isinstance(fields[key], str):
            fields[key] = json.dumps(fields[key])

    columns = ", ".join(f"{k} = ?" for k in fields)
    values = list(fields.values())

    with get_connection() as conn:
        character_id = _active_character_id(telegram_user_id, conn)
        if character_id is None:
            return None
        conn.execute(
            f"UPDATE characters SET {columns} WHERE character_id = ?",
            values + [character_id],
        )
    return get_character(telegram_user_id)


def add_item(telegram_user_id: int, item_id: str, quantity: int = 1) -> dict | None:
    """Add `quantity` of an item to a character's backpack (dict of item_id -> count)."""
    character = get_character(telegram_user_id)
    if character is None:
        return None
    character["inventory"][item_id] = character["inventory"].get(item_id, 0) + quantity
    return update_character(telegram_user_id, inventory=character["inventory"])


def remove_item(telegram_user_id: int, item_id: str, quantity: int = 1) -> tuple[bool, dict | None]:
    """
    Remove `quantity` of an item from a character's backpack. Returns
    (success, updated_character). Fails cleanly (no partial removal) if
    the character doesn't have enough of the item.
    """
    character = get_character(telegram_user_id)
    if character is None:
        return False, None
    have = character["inventory"].get(item_id, 0)
    if have < quantity:
        return False, character
    remaining = have - quantity
    if remaining <= 0:
        character["inventory"].pop(item_id, None)
    else:
        character["inventory"][item_id] = remaining
    updated = update_character(telegram_user_id, inventory=character["inventory"])
    return True, updated


def move_character(telegram_user_id: int, new_location: str) -> dict | None:
    return update_character(telegram_user_id, current_location=new_location)


def mark_visited(telegram_user_id: int, location_id: str) -> dict | None:
    """Adds location_id to this character's visited_locations, if new."""
    character = get_character(telegram_user_id)
    if character is None:
        return None
    if location_id in character["visited_locations"]:
        return character
    character["visited_locations"].append(location_id)
    return update_character(telegram_user_id, visited_locations=character["visited_locations"])


def learn_spell(telegram_user_id: int, spell_id: str) -> dict | None:
    character = get_character(telegram_user_id)
    if character is None:
        return None
    if spell_id not in character["known_spells"]:
        character["known_spells"].append(spell_id)
    return update_character(telegram_user_id, known_spells=character["known_spells"])


def spend_spell_slot(telegram_user_id: int) -> tuple[bool, dict | None]:
    """
    Attempts to spend one spell slot. Returns (success, updated_character).
    Fails cleanly (no partial change) if none remain.
    """
    character = get_character(telegram_user_id)
    if character is None:
        return False, None
    if character["spell_slots_current"] <= 0:
        return False, character
    updated = update_character(
        telegram_user_id, spell_slots_current=character["spell_slots_current"] - 1
    )
    return True, updated


def restore_spell_slots(telegram_user_id: int) -> dict | None:
    """Restores spell slots to their max — called on rest."""
    character = get_character(telegram_user_id)
    if character is None:
        return None
    return update_character(telegram_user_id, spell_slots_current=character["spell_slots_max"])


def complete_quest(telegram_user_id: int, quest_id: str) -> dict | None:
    """Marks a quest completed and clears it from active_quests, if present."""
    character = get_character(telegram_user_id)
    if character is None:
        return None
    if quest_id not in character["completed_quests"]:
        character["completed_quests"].append(quest_id)
    active = character["active_quests"]
    active.pop(quest_id, None)
    return update_character(
        telegram_user_id, completed_quests=character["completed_quests"], active_quests=active
    )


def accept_quest(telegram_user_id: int, quest_id: str) -> dict | None:
    character = get_character(telegram_user_id)
    if character is None:
        return None
    active = character["active_quests"]
    if quest_id not in active:
        active[quest_id] = {"accepted_at": datetime.now(timezone.utc).isoformat()}
    return update_character(telegram_user_id, active_quests=active)


def join_guild(telegram_user_id: int, guild_id: str) -> dict | None:
    return update_character(telegram_user_id, guild=guild_id)


def get_character(telegram_user_id: int) -> dict | None:
    """Returns this telegram_user_id's currently ACTIVE character, if any."""
    with get_connection() as conn:
        character_id = _active_character_id(telegram_user_id, conn)
        if character_id is None:
            return None
        row = conn.execute(
            "SELECT * FROM characters WHERE character_id = ? AND is_deleted = 0",
            (character_id,),
        ).fetchone()
    return _row_to_dict(row) if row else None


def list_characters(telegram_user_id: int) -> list[dict]:
    """All non-deleted characters this telegram_user_id owns, oldest first."""
    with get_connection() as conn:
        rows = conn.execute(
            "SELECT * FROM characters WHERE telegram_user_id = ? AND is_deleted = 0 ORDER BY character_id",
            (telegram_user_id,),
        ).fetchall()
    return [_row_to_dict(r) for r in rows]


def switch_character(telegram_user_id: int, character_id: int) -> dict | None:
    """
    Makes character_id the active character for telegram_user_id, if it
    exists, is owned by them, and isn't deleted. Returns the newly-active
    character dict, or None if the switch was invalid.
    """
    with get_connection() as conn:
        row = conn.execute(
            "SELECT * FROM characters WHERE character_id = ? AND telegram_user_id = ? AND is_deleted = 0",
            (character_id, telegram_user_id),
        ).fetchone()
        if row is None:
            return None
        _set_active_character(telegram_user_id, character_id, conn)
    return _row_to_dict(row)


def delete_character(telegram_user_id: int, character_id: int) -> bool:
    """
    Soft-deletes a character this telegram_user_id owns. If it was the
    active character, another remaining (non-deleted) character owned by
    the same user becomes active, if one exists; otherwise the user has
    no active character until they create or switch to one.
    """
    with get_connection() as conn:
        row = conn.execute(
            "SELECT character_id FROM characters WHERE character_id = ? AND telegram_user_id = ? AND is_deleted = 0",
            (character_id, telegram_user_id),
        ).fetchone()
        if row is None:
            return False

        conn.execute("UPDATE characters SET is_deleted = 1 WHERE character_id = ?", (character_id,))

        current_active = _active_character_id(telegram_user_id, conn)
        if current_active == character_id:
            replacement = conn.execute(
                "SELECT character_id FROM characters WHERE telegram_user_id = ? AND is_deleted = 0 "
                "ORDER BY character_id LIMIT 1",
                (telegram_user_id,),
            ).fetchone()
            if replacement:
                _set_active_character(telegram_user_id, replacement["character_id"], conn)
            else:
                conn.execute("DELETE FROM active_characters WHERE telegram_user_id = ?", (telegram_user_id,))
    return True


def add_xp(telegram_user_id: int, amount: int) -> dict | None:
    """
    Add XP and auto-update level + proficiency bonus if it crossed a
    threshold. If the character actually leveled up, this also applies
    real HP growth (average-per-level, per 5E's own optional rule),
    Ability Score Improvements at levels 4/8/12/16/19, and grants any
    class spells newly unlocked at the new level (spells.py's
    SPELL_LEVEL_UNLOCK_CHAR_LEVEL table) — leveling up now has a genuine
    mechanical effect, not just a cosmetic number change. Applies
    equally to AI-controlled companions, since they level through this
    same function.
    """
    character = get_character(telegram_user_id)
    if character is None:
        return None

    old_level = character["level"]
    new_xp = character["xp"] + amount
    new_level = level_for_xp(new_xp)
    new_prof = proficiency_bonus_for_level(new_level)

    updates = {"xp": new_xp, "level": new_level, "proficiency_bonus": new_prof}

    if new_level > old_level:
        con_mod = (character["constitution"] - 10) // 2
        hp_per_level = hp_gain_for_level(character["char_class"], con_mod)
        levels_gained = new_level - old_level
        hp_gain_total = hp_per_level * levels_gained
        new_hp_max = character["hp_max"] + hp_gain_total
        updates["hp_max"] = new_hp_max
        # Leveling up also heals you back up by the same amount gained,
        # a common, player-friendly 5E table convention.
        updates["hp_current"] = min(character["hp_current"] + hp_gain_total, new_hp_max)

        asi_count = sum(1 for lvl in range(old_level + 1, new_level + 1) if lvl in ASI_LEVELS)
        if asi_count:
            primary_ability = CLASS_PRIMARY_ABILITY.get(character["char_class"].lower())
            if primary_ability:
                current_value = character[primary_ability]
                updates[primary_ability] = min(current_value + 2 * asi_count, 20)

        newly_unlocked = spells_module.spells_unlocked_at_level(character["char_class"], new_level)
        newly_learned = [s for s in newly_unlocked if s not in character["known_spells"]]
        if newly_learned:
            updates["known_spells"] = character["known_spells"] + newly_learned

    return update_character(telegram_user_id, **updates)


# ---------------------------------------------------------------------
# NPC relationships — persistent, per-(player, NPC) memory and rapport.
# ---------------------------------------------------------------------

MAX_MEMORY_EVENTS = 20  # oldest facts drop off rather than growing forever

_RELATIONSHIP_DEFAULTS = {"affinity": 0, "memory_events": [], "banned": 0}


def get_relationship(telegram_user_id: int, npc_id: str) -> dict:
    """Returns this player's relationship with an NPC, creating a neutral default row if none exists yet."""
    with get_connection() as conn:
        row = conn.execute(
            "SELECT * FROM npc_relationships WHERE telegram_user_id = ? AND npc_id = ?",
            (telegram_user_id, npc_id),
        ).fetchone()
        if row is None:
            conn.execute(
                "INSERT INTO npc_relationships (telegram_user_id, npc_id) VALUES (?, ?)",
                (telegram_user_id, npc_id),
            )
            return {"telegram_user_id": telegram_user_id, "npc_id": npc_id, **_RELATIONSHIP_DEFAULTS}
    d = dict(row)
    d["memory_events"] = json.loads(d["memory_events"])
    return d


def adjust_affinity(telegram_user_id: int, npc_id: str, delta: int, event: str | None = None) -> dict:
    """
    Adjusts rapport with an NPC (clamped to -100..100) and, if `event` is
    given, appends a short factual memory (e.g. "was caught stealing")
    that future conversations/ambient lines can be grounded in — real,
    persistent consequence, not just a transient combat-log line.
    """
    relationship = get_relationship(telegram_user_id, npc_id)
    new_affinity = max(-100, min(100, relationship["affinity"] + delta))
    memory_events = relationship["memory_events"]
    if event:
        memory_events = memory_events + [event]
        memory_events = memory_events[-MAX_MEMORY_EVENTS:]

    with get_connection() as conn:
        conn.execute(
            """
            UPDATE npc_relationships SET affinity = ?, memory_events = ?
            WHERE telegram_user_id = ? AND npc_id = ?
            """,
            (new_affinity, json.dumps(memory_events), telegram_user_id, npc_id),
        )
    return get_relationship(telegram_user_id, npc_id)


def set_banned_by_npc(telegram_user_id: int, npc_id: str, banned: bool = True) -> dict:
    """A real, persistent consequence — e.g. a shopkeeper refusing to trade with a caught thief."""
    get_relationship(telegram_user_id, npc_id)  # ensure the row exists
    with get_connection() as conn:
        conn.execute(
            "UPDATE npc_relationships SET banned = ? WHERE telegram_user_id = ? AND npc_id = ?",
            (int(banned), telegram_user_id, npc_id),
        )
    return get_relationship(telegram_user_id, npc_id)


def is_banned_by_npc(telegram_user_id: int, npc_id: str) -> bool:
    return bool(get_relationship(telegram_user_id, npc_id)["banned"])


# ---------------------------------------------------------------------
# Faction standing — how a faction as a whole regards a player, driven
# by what they actually do to its members (see bot.py's combat/theft
# hooks), not anything invented by narration.
# ---------------------------------------------------------------------

def get_faction_standing(telegram_user_id: int, faction_id: str, starting_standing: int = 0) -> int:
    with get_connection() as conn:
        row = conn.execute(
            "SELECT standing FROM faction_standing WHERE telegram_user_id = ? AND faction_id = ?",
            (telegram_user_id, faction_id),
        ).fetchone()
        if row is None:
            conn.execute(
                "INSERT INTO faction_standing (telegram_user_id, faction_id, standing) VALUES (?, ?, ?)",
                (telegram_user_id, faction_id, starting_standing),
            )
            return starting_standing
    return row["standing"]


def adjust_faction_standing(telegram_user_id: int, faction_id: str, delta: int,
                             starting_standing: int = 0) -> int:
    current = get_faction_standing(telegram_user_id, faction_id, starting_standing)
    new_standing = max(-100, min(100, current + delta))
    with get_connection() as conn:
        conn.execute(
            "UPDATE faction_standing SET standing = ? WHERE telegram_user_id = ? AND faction_id = ?",
            (new_standing, telegram_user_id, faction_id),
        )
    return new_standing


# ---------------------------------------------------------------------
# Proficiency growth — "the more you do something, the better you get."
# See rules/proficiency.py for the deterministic bonus formula; this
# just tracks the raw per-ability use counts the formula is applied to.
# ---------------------------------------------------------------------

def get_skill_uses(telegram_user_id: int) -> dict:
    character = get_character(telegram_user_id)
    return character["skill_uses"] if character else {}


def record_skill_use(telegram_user_id: int, ability: str) -> int:
    """Increments and returns the new use count for this ability."""
    character = get_character(telegram_user_id)
    if character is None:
        return 0
    uses = character["skill_uses"]
    uses[ability] = uses.get(ability, 0) + 1
    update_character(telegram_user_id, skill_uses=uses)
    return uses[ability]


# ---------------------------------------------------------------------
# Inactivity — "resting until next session," either explicit (a player
# says so) or automatic (5 real-world minutes of chat silence). An
# inactive character can still be targeted with support spells by
# active party members; it just can't act itself until it's reactivated
# (which happens automatically the next time its owner sends any real
# message — see bot.py's adventure_master_handler).
# ---------------------------------------------------------------------

def touch_last_active(telegram_user_id: int) -> None:
    """Records 'this player did something just now' — real players only in practice."""
    with get_connection() as conn:
        character_id = _active_character_id(telegram_user_id, conn)
        if character_id is None:
            return
        conn.execute(
            "UPDATE characters SET last_active_at = ? WHERE character_id = ?",
            (datetime.now(timezone.utc).isoformat(), character_id),
        )


def mark_inactive(telegram_user_id: int) -> dict | None:
    return update_character(telegram_user_id, is_inactive=1)


def mark_active(telegram_user_id: int) -> dict | None:
    return update_character(telegram_user_id, is_inactive=0)


def get_idle_real_characters() -> list[dict]:
    """
    All real (non-AI), non-deleted, currently-active (not already
    resting) characters that have a recorded last-activity time —
    candidates for the auto-inactivity idle check. Filtering by actual
    elapsed time is left to the caller (bot.py), which owns the
    threshold and needs real wall-clock comparison, not a SQL string
    comparison on ISO timestamps of unknown precision.
    """
    with get_connection() as conn:
        rows = conn.execute(
            """
            SELECT c.* FROM characters c
            JOIN active_characters a ON a.character_id = c.character_id
            WHERE c.is_deleted = 0 AND c.is_ai = 0 AND c.is_inactive = 0
              AND c.last_active_at IS NOT NULL
            """
        ).fetchall()
    return [_row_to_dict(r) for r in rows]


# --- Area quest board ---

def _board_quest_row_to_dict(row) -> dict:
    d = dict(row)
    d["branch_data"] = json.loads(d["branch_data"]) if d.get("branch_data") else None
    return d


def get_active_board_quests(location_id: str, day_key: str) -> list[dict]:
    """All of today's board quests for this location (accepted or not), oldest first."""
    with get_connection() as conn:
        rows = conn.execute(
            "SELECT * FROM board_quests WHERE location_id = ? AND day_key = ? "
            "ORDER BY board_quest_id ASC",
            (location_id, day_key),
        ).fetchall()
    return [_board_quest_row_to_dict(r) for r in rows]


def get_active_board_quest(location_id: str, day_key: str) -> dict | None:
    """The single most-recent board quest for this location today, if any (accepted or not)."""
    with get_connection() as conn:
        row = conn.execute(
            "SELECT * FROM board_quests WHERE location_id = ? AND day_key = ? "
            "ORDER BY board_quest_id DESC LIMIT 1",
            (location_id, day_key),
        ).fetchone()
    return _board_quest_row_to_dict(row) if row else None


def create_board_quest(location_id: str, day_key: str, title: str, description: str,
                        giver_npc: str | None, objective_type: str, objective_target: str,
                        objective_count: int, reward_xp: int, reward_gold: int) -> dict:
    with get_connection() as conn:
        cur = conn.execute(
            """
            INSERT INTO board_quests (
                location_id, day_key, title, description, giver_npc,
                objective_type, objective_target, objective_count,
                reward_xp, reward_gold, generated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (location_id, day_key, title, description, giver_npc,
             objective_type, objective_target, objective_count,
             reward_xp, reward_gold, datetime.now(timezone.utc).isoformat()),
        )
        board_quest_id = cur.lastrowid
        row = conn.execute(
            "SELECT * FROM board_quests WHERE board_quest_id = ?", (board_quest_id,)
        ).fetchone()
    return _board_quest_row_to_dict(row)


def accept_board_quest(board_quest_id: int, telegram_user_id: int) -> dict | None:
    now = datetime.now(timezone.utc)
    expires = now.timestamp() + (24 * 3600)
    with get_connection() as conn:
        conn.execute(
            "UPDATE board_quests SET accepted_by = ?, accepted_at = ?, expires_at = ? "
            "WHERE board_quest_id = ? AND accepted_by IS NULL",
            (telegram_user_id, now.isoformat(), datetime.fromtimestamp(expires, timezone.utc).isoformat(),
             board_quest_id),
        )
        row = conn.execute(
            "SELECT * FROM board_quests WHERE board_quest_id = ?", (board_quest_id,)
        ).fetchone()
    return _board_quest_row_to_dict(row) if row else None


def record_board_quest_progress(board_quest_id: int, amount: int = 1) -> dict | None:
    with get_connection() as conn:
        conn.execute(
            "UPDATE board_quests SET progress_count = progress_count + ? WHERE board_quest_id = ?",
            (amount, board_quest_id),
        )
        row = conn.execute(
            "SELECT * FROM board_quests WHERE board_quest_id = ?", (board_quest_id,)
        ).fetchone()
    return _board_quest_row_to_dict(row) if row else None


def complete_board_quest(board_quest_id: int) -> None:
    with get_connection() as conn:
        conn.execute(
            "UPDATE board_quests SET completed_at = ? WHERE board_quest_id = ?",
            (datetime.now(timezone.utc).isoformat(), board_quest_id),
        )


def set_board_quest_branch_data(board_quest_id: int, branch_data: dict) -> None:
    """
    Attaches branching-quest metadata (archetype id, AI-generated setup
    narration, and the two named choices with their own fixed
    reward/consequence data) to a board quest generated as a branching
    one. Stored as JSON since its shape is archetype-specific, unlike
    the other board_quests columns which are the same for every quest.
    """
    with get_connection() as conn:
        conn.execute(
            "UPDATE board_quests SET branch_data = ? WHERE board_quest_id = ?",
            (json.dumps(branch_data), board_quest_id),
        )


def resolve_board_quest_branch(board_quest_id: int, choice_key: str) -> dict | None:
    """Marks a branching quest completed with a specific choice recorded, for narration/history."""
    with get_connection() as conn:
        row = conn.execute(
            "SELECT branch_data FROM board_quests WHERE board_quest_id = ?", (board_quest_id,)
        ).fetchone()
        if row is None or not row["branch_data"]:
            return None
        branch_data = json.loads(row["branch_data"])
        branch_data["resolved_choice"] = choice_key
        conn.execute(
            "UPDATE board_quests SET branch_data = ?, completed_at = ? WHERE board_quest_id = ?",
            (json.dumps(branch_data), datetime.now(timezone.utc).isoformat(), board_quest_id),
        )
    return branch_data


def get_accepted_board_quests_for_user(telegram_user_id: int) -> list[dict]:
    """A player's currently-accepted, not-yet-completed board quests (any location)."""
    with get_connection() as conn:
        rows = conn.execute(
            "SELECT * FROM board_quests WHERE accepted_by = ? AND completed_at IS NULL",
            (telegram_user_id,),
        ).fetchall()
    return [_board_quest_row_to_dict(r) for r in rows]


def expire_stale_board_quests() -> list[dict]:
    """
    Releases any accepted-but-not-completed board quest whose 24h
    window has passed, back to the board for someone else. Returns the
    rows that were just expired (for narration/logging), not deleted --
    the location simply gets a fresh one generated next time it's checked.
    """
    now_iso = datetime.now(timezone.utc).isoformat()
    with get_connection() as conn:
        rows = conn.execute(
            "SELECT * FROM board_quests WHERE accepted_by IS NOT NULL AND completed_at IS NULL "
            "AND expires_at IS NOT NULL AND expires_at < ?",
            (now_iso,),
        ).fetchall()
        expired = [_board_quest_row_to_dict(r) for r in rows]
        if expired:
            conn.execute(
                "UPDATE board_quests SET accepted_by = NULL, accepted_at = NULL, expires_at = NULL, "
                "progress_count = 0 WHERE accepted_by IS NOT NULL AND completed_at IS NULL "
                "AND expires_at IS NOT NULL AND expires_at < ?",
                (now_iso,),
            )
    return expired


# --- Real party membership (roster only -- see CREATE_PARTIES_TABLE's
# comment: combat grouping is unaffected by this) ---

def get_party_size(party_id: int) -> int:
    with get_connection() as conn:
        row = conn.execute(
            "SELECT COUNT(*) AS n FROM characters WHERE party_id = ? AND is_deleted = 0", (party_id,)
        ).fetchone()
    return row["n"] if row else 0


def get_party_members_by_id(party_id: int) -> list[dict]:
    with get_connection() as conn:
        rows = conn.execute(
            "SELECT c.* FROM characters c JOIN active_characters a ON a.character_id = c.character_id "
            "WHERE c.party_id = ? AND c.is_deleted = 0",
            (party_id,),
        ).fetchall()
    return [_row_to_dict(r) for r in rows]


def create_party(telegram_user_id: int) -> int:
    """Creates a new party and immediately puts the creator's active character in it."""
    with get_connection() as conn:
        cur = conn.execute(
            "INSERT INTO parties (created_by, created_at) VALUES (?, ?)",
            (telegram_user_id, datetime.now(timezone.utc).isoformat()),
        )
        party_id = cur.lastrowid
        character_id = _active_character_id(telegram_user_id, conn)
        conn.execute("UPDATE characters SET party_id = ? WHERE character_id = ?", (party_id, character_id))
    return party_id


def set_pending_party_invite(telegram_user_id: int, party_id: int) -> None:
    with get_connection() as conn:
        character_id = _active_character_id(telegram_user_id, conn)
        conn.execute(
            "UPDATE characters SET pending_party_invite = ? WHERE character_id = ?", (party_id, character_id)
        )


def accept_party_invite(telegram_user_id: int) -> tuple[bool, str]:
    """Joins the party the character was invited to, if there's room. Returns (success, message)."""
    character = get_character(telegram_user_id)
    if character is None:
        return False, "You don't have a character yet!"
    party_id = character.get("pending_party_invite")
    if not party_id:
        return False, "You don't have a pending party invite."
    if get_party_size(party_id) >= PARTY_MAX_MEMBERS:
        return False, "That party is already full (6 members)."
    with get_connection() as conn:
        character_id = _active_character_id(telegram_user_id, conn)
        conn.execute(
            "UPDATE characters SET party_id = ?, pending_party_invite = NULL WHERE character_id = ?",
            (party_id, character_id),
        )
    return True, "Joined the party!"


def leave_party(telegram_user_id: int) -> bool:
    """Returns False if the character wasn't in a party to begin with."""
    character = get_character(telegram_user_id)
    if character is None or not character.get("party_id"):
        return False
    with get_connection() as conn:
        character_id = _active_character_id(telegram_user_id, conn)
        conn.execute("UPDATE characters SET party_id = NULL WHERE character_id = ?", (character_id,))
    return True


def add_ai_companion_to_party(telegram_user_id: int, party_id: int) -> None:
    """AI companions have no real turn to 'accept' with -- they join immediately when invited."""
    with get_connection() as conn:
        conn.execute("UPDATE characters SET party_id = ? WHERE telegram_user_id = ?", (party_id, telegram_user_id))


# --- Autonomous AI-played party (2026-07-10, per Coffee: a separate,
# self-directed party that plays through the exact same pipeline real
# players use -- distinct from is_ai=1 combat companions, which only
# ever auto-resolve combat turns and never act outside them) ---

def mark_autonomous(telegram_user_id: int) -> None:
    with get_connection() as conn:
        conn.execute("UPDATE characters SET is_autonomous = 1 WHERE telegram_user_id = ?", (telegram_user_id,))


def get_autonomous_players() -> list[dict]:
    with get_connection() as conn:
        rows = conn.execute(
            "SELECT * FROM characters WHERE is_autonomous = 1 AND is_deleted = 0 ORDER BY character_id"
        ).fetchall()
    return [_row_to_dict(r) for r in rows]
