"""
db.py
SQLite-backed persistence for characters (and later, sessions/NPCs).
Inventory is stored as a JSON-encoded string in the characters table.
"""
import json
import sqlite3
from contextlib import contextmanager

import config
from rules.leveling import (
    level_for_xp, proficiency_bonus_for_level,
    hp_gain_for_level, ASI_LEVELS, CLASS_PRIMARY_ABILITY,
)

CREATE_CHARACTERS_TABLE = """
CREATE TABLE IF NOT EXISTS characters (
    telegram_user_id INTEGER PRIMARY KEY,
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
    spell_slots_current INTEGER NOT NULL DEFAULT 0
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


def init_db():
    """Create tables if they don't already exist."""
    with get_connection() as conn:
        conn.execute(CREATE_CHARACTERS_TABLE)


def _row_to_dict(row: sqlite3.Row) -> dict:
    d = dict(row)
    d["inventory"] = json.loads(d["inventory"])
    d["known_spells"] = json.loads(d["known_spells"])
    d["completed_quests"] = json.loads(d["completed_quests"])
    return d


def create_character(telegram_user_id: int, name: str, race: str, char_class: str,
                      ability_scores: dict, hp_max: int, armor_class: int,
                      gold: int, inventory: dict, is_ai: bool = False,
                      known_spells: list | None = None,
                      current_location: str = "crossroads_tavern",
                      spell_slots_max: int = 0) -> dict:
    with get_connection() as conn:
        conn.execute(
            """
            INSERT INTO characters (
                telegram_user_id, name, race, char_class, level, xp,
                hp_current, hp_max, strength, dexterity, constitution,
                intelligence, wisdom, charisma, proficiency_bonus,
                armor_class, inventory, gold, is_ai, current_location, known_spells,
                spell_slots_max, spell_slots_current
            ) VALUES (?, ?, ?, ?, 1, 0, ?, ?, ?, ?, ?, ?, ?, ?, 2, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                telegram_user_id, name, race, char_class,
                hp_max, hp_max,
                ability_scores["strength"], ability_scores["dexterity"],
                ability_scores["constitution"], ability_scores["intelligence"],
                ability_scores["wisdom"], ability_scores["charisma"],
                armor_class, json.dumps(inventory), gold, int(is_ai),
                current_location, json.dumps(known_spells or []),
                spell_slots_max, spell_slots_max,
            ),
        )
    return get_character(telegram_user_id)


_NEXT_AI_ID = -1000  # AI companions use negative synthetic IDs (never collide with real Telegram user IDs)


def create_ai_companion(name: str, race: str, char_class: str, ability_scores: dict,
                         hp_max: int, armor_class: int, gold: int, inventory: dict) -> dict:
    """
    Create an AI-controlled companion character. Uses a synthetic negative
    telegram_user_id so it can never collide with a real player's ID, and
    is flagged is_ai=1 so the turn engine knows to auto-resolve its turns.
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

    json_fields = ("inventory", "known_spells", "completed_quests")
    for key in json_fields:
        if key in fields and not isinstance(fields[key], str):
            fields[key] = json.dumps(fields[key])

    columns = ", ".join(f"{k} = ?" for k in fields)
    values = list(fields.values()) + [telegram_user_id]

    with get_connection() as conn:
        conn.execute(
            f"UPDATE characters SET {columns} WHERE telegram_user_id = ?",
            values,
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
    with get_connection() as conn:
        row = conn.execute(
            "SELECT * FROM characters WHERE telegram_user_id = ?",
            (telegram_user_id,),
        ).fetchone()
    return _row_to_dict(row) if row else None


def add_xp(telegram_user_id: int, amount: int) -> dict | None:
    """
    Add XP and auto-update level + proficiency bonus if it crossed a
    threshold. If the character actually leveled up, this also applies
    real HP growth (average-per-level, per 5E's own optional rule) and
    Ability Score Improvements at levels 4/8/12/16/19 — leveling up now
    has a genuine mechanical effect, not just a cosmetic number change.
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

    return update_character(telegram_user_id, **updates)
