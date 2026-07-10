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
    skill_uses TEXT NOT NULL DEFAULT '{}'
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


def _row_to_dict(row: sqlite3.Row) -> dict:
    d = dict(row)
    d["inventory"] = json.loads(d["inventory"])
    d["known_spells"] = json.loads(d["known_spells"])
    d["completed_quests"] = json.loads(d["completed_quests"])
    d["visited_locations"] = json.loads(d["visited_locations"])
    d["skill_uses"] = json.loads(d["skill_uses"])
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

    json_fields = ("inventory", "known_spells", "completed_quests", "visited_locations", "skill_uses")
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
    character = get_character(telegram_user_id)
    if character is None:
        return None
    if quest_id not in character["completed_quests"]:
        character["completed_quests"].append(quest_id)
    return update_character(telegram_user_id, completed_quests=character["completed_quests"])


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
