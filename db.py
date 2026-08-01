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
import threading
from contextlib import contextmanager
from datetime import datetime, timezone

import config
import items as items_module
from rules.dice import ability_modifier, average_damage
from rules.leveling import (
    level_for_xp, proficiency_bonus_for_level,
    hp_gain_for_level, ASI_LEVELS, xp_gain_multiplier, EVOLUTION_HP_MULTIPLIER,
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
    active_quests TEXT NOT NULL DEFAULT '{}',
    feature_uses TEXT NOT NULL DEFAULT '{}',
    description TEXT,
    known_monsters TEXT NOT NULL DEFAULT '[]',
    cleared_locations TEXT NOT NULL DEFAULT '[]'
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
    resolution TEXT NOT NULL DEFAULT 'unresolved',
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

# Task #79, per Coffee: a real player-run marketplace, NOT an auction
# house -- a direct listing at a fixed price, first buyer takes it,
# never a bid. Global (not location-scoped) for a first real version --
# a location-scoped marketplace is a natural, cheap follow-up on this
# same table if wanted later.
CREATE_MARKET_LISTINGS_TABLE = """
CREATE TABLE IF NOT EXISTS market_listings (
    listing_id INTEGER PRIMARY KEY AUTOINCREMENT,
    seller_id INTEGER NOT NULL,
    seller_name TEXT NOT NULL,
    item_id TEXT NOT NULL,
    quantity INTEGER NOT NULL,
    price INTEGER NOT NULL,
    created_at TEXT NOT NULL
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

CREATE_GAME_SETTINGS_TABLE = """
CREATE TABLE IF NOT EXISTS game_settings (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
"""

PARTY_MAX_MEMBERS = config.PARTY_MAX_MEMBERS  # moved to config.py 2026-07-14, now .env-configurable


# Real fix (2026-07-17, task #148): live log showed repeated
# `sqlite3.OperationalError: database is locked` -- several independent
# asyncio loops (adventure_master_handler, ai_party autonomous companion
# turns, the idle_check background loop, the world_tick heartbeat) each
# open their own synchronous connection to the same file, and the bare
# sqlite3.connect() default (a 5s busy wait, rollback-journal locking
# that blocks ALL readers behind any writer) was too easily exceeded
# under real concurrent load -- confirmed live: one occurrence crashed
# an AI companion's turn outright, another produced a ~22s delay on a
# real player's "Level up"/"my character" messages while idle_check and
# world_tick were independently failing on the same lock.
#
# `timeout=` + WAL mode (see get_connection below) cut this down
# enormously but didn't fully eliminate it under a deliberately
# aggressive stress test (8 threads tight-looping writes/reads) -- and
# every one of the real contenders above (the handler, ai_party,
# idle_check, world_tick) all run as asyncio tasks/threads *within this
# same single bot process*, not separate processes, so a plain
# in-process lock fully serializes them with zero risk of a locked
# error between them, at the cost of some serialization overhead that's
# negligible next to the actual write sizes here. Kept alongside
# timeout=/WAL (not instead of) as defense-in-depth for the remaining
# case this doesn't cover: a genuinely separate process (e.g. one of
# the scripts/*.py one-off tools) touching the same file concurrently.
_DB_LOCK = threading.Lock()


@contextmanager
def get_connection():
    with _DB_LOCK:
        conn = sqlite3.connect(config.DB_PATH, timeout=30.0)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
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
        conn.execute(CREATE_MARKET_LISTINGS_TABLE)
        conn.execute(CREATE_BOARD_QUESTS_TABLE)
        conn.execute(CREATE_PARTIES_TABLE)
        conn.execute(CREATE_GAME_SETTINGS_TABLE)

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

        # Task #133, per Coffee: real 9-box alignment (Law/Chaos x
        # Good/Evil), each axis a persistent -100..100 score defaulting
        # to 0 (True Neutral) -- see bot.py's _alignment_label for how
        # these two numbers become a real label.
        if "alignment_law_chaos" not in columns:
            conn.execute("ALTER TABLE characters ADD COLUMN alignment_law_chaos INTEGER NOT NULL DEFAULT 0")
        if "alignment_good_evil" not in columns:
            conn.execute("ALTER TABLE characters ADD COLUMN alignment_good_evil INTEGER NOT NULL DEFAULT 0")

        # Task #131, per Coffee: a real skill-tree point system on
        # level-up. skill_points is a spendable resource (like
        # pending_asi_points); skill_tree_upgrades is the real,
        # persistent list of which upgrades this character has bought.
        if "skill_points" not in columns:
            conn.execute("ALTER TABLE characters ADD COLUMN skill_points INTEGER NOT NULL DEFAULT 0")
        if "skill_tree_upgrades" not in columns:
            conn.execute("ALTER TABLE characters ADD COLUMN skill_tree_upgrades TEXT NOT NULL DEFAULT '[]'")

        npc_relationship_columns = _existing_columns(conn, "npc_relationships")
        if "resolution" not in npc_relationship_columns:
            conn.execute("ALTER TABLE npc_relationships ADD COLUMN resolution TEXT NOT NULL DEFAULT 'unresolved'")

        if "proven_in_combat" not in columns:
            conn.execute("ALTER TABLE characters ADD COLUMN proven_in_combat INTEGER NOT NULL DEFAULT 0")

        board_quest_columns = _existing_columns(conn, "board_quests")
        if "branch_data" not in board_quest_columns:
            conn.execute("ALTER TABLE board_quests ADD COLUMN branch_data TEXT")
        # tier (2026-07-25, per Coffee: "daily, weekly, monthly missions
        # separate from story-line... worked into the evolution
        # system"): board_quests were daily-only until now. day_key's
        # format already differs per tier (a plain date vs "2026-W30"
        # vs "2026-07"), so there's no real collision risk, but a real
        # column lets accept_board_quest look up the right expiry
        # window (24h/7d/30d) without parsing that string. Reward XP
        # already scales with rebirth for free via db.add_xp's existing
        # xp_gain_multiplier -- no extra plumbing needed for that part.
        if "tier" not in board_quest_columns:
            conn.execute("ALTER TABLE board_quests ADD COLUMN tier TEXT NOT NULL DEFAULT 'daily'")

        if "party_id" not in columns:
            conn.execute("ALTER TABLE characters ADD COLUMN party_id INTEGER")
        if "pending_party_invite" not in columns:
            conn.execute("ALTER TABLE characters ADD COLUMN pending_party_invite INTEGER")
        if "is_autonomous" not in columns:
            conn.execute("ALTER TABLE characters ADD COLUMN is_autonomous INTEGER NOT NULL DEFAULT 0")
        if "rest_started_at" not in columns:
            conn.execute("ALTER TABLE characters ADD COLUMN rest_started_at TEXT")
        if "feature_uses" not in columns:
            conn.execute("ALTER TABLE characters ADD COLUMN feature_uses TEXT NOT NULL DEFAULT '{}'")
        if "is_dead" not in columns:
            conn.execute("ALTER TABLE characters ADD COLUMN is_dead INTEGER NOT NULL DEFAULT 0")
        if "equipped_weapon" not in columns:
            conn.execute("ALTER TABLE characters ADD COLUMN equipped_weapon TEXT")
        if "equipped_armor" not in columns:
            conn.execute("ALTER TABLE characters ADD COLUMN equipped_armor TEXT")
        if "equipped_shield" not in columns:
            conn.execute("ALTER TABLE characters ADD COLUMN equipped_shield TEXT")
        if "equipped_accessories" not in columns:
            conn.execute("ALTER TABLE characters ADD COLUMN equipped_accessories TEXT NOT NULL DEFAULT '[]'")
        if "manual_dice_enabled" not in columns:
            conn.execute("ALTER TABLE characters ADD COLUMN manual_dice_enabled INTEGER NOT NULL DEFAULT 0")
        if "board_quests_completed" not in columns:
            conn.execute("ALTER TABLE characters ADD COLUMN board_quests_completed INTEGER NOT NULL DEFAULT 0")
        if "pending_asi_points" not in columns:
            conn.execute("ALTER TABLE characters ADD COLUMN pending_asi_points INTEGER NOT NULL DEFAULT 0")
        if "description" not in columns:
            conn.execute("ALTER TABLE characters ADD COLUMN description TEXT")
        if "known_monsters" not in columns:
            conn.execute("ALTER TABLE characters ADD COLUMN known_monsters TEXT NOT NULL DEFAULT '[]'")
        if "telegram_username" not in columns:
            conn.execute("ALTER TABLE characters ADD COLUMN telegram_username TEXT")
        if "pronouns" not in columns:
            conn.execute("ALTER TABLE characters ADD COLUMN pronouns TEXT")
        if "do_not_disturb" not in columns:
            conn.execute("ALTER TABLE characters ADD COLUMN do_not_disturb INTEGER NOT NULL DEFAULT 0")
        if "status_note" not in columns:
            conn.execute("ALTER TABLE characters ADD COLUMN status_note TEXT")
        if "achievements" not in columns:
            conn.execute("ALTER TABLE characters ADD COLUMN achievements TEXT NOT NULL DEFAULT '[]'")
        if "active_title" not in columns:
            conn.execute("ALTER TABLE characters ADD COLUMN active_title TEXT")
        if "login_streak_days" not in columns:
            conn.execute("ALTER TABLE characters ADD COLUMN login_streak_days INTEGER NOT NULL DEFAULT 0")
        if "last_login_date" not in columns:
            conn.execute("ALTER TABLE characters ADD COLUMN last_login_date TEXT")
        if "last_guild_quest_date" not in columns:
            conn.execute("ALTER TABLE characters ADD COLUMN last_guild_quest_date TEXT")
        if "map_revealed_locations" not in columns:
            # Task #141: buyable/discoverable maps reveal a location's NAME
            # without the player having actually visited it -- kept as its
            # own separate fog-of-war layer from visited_locations (never
            # merged into it), since a revealed-but-unvisited location must
            # still show up on _do_show_map with no connection details (the
            # actual spoiler), only the name itself.
            conn.execute("ALTER TABLE characters ADD COLUMN map_revealed_locations TEXT NOT NULL DEFAULT '[]'")

        # Prestige/rebirth system, per Coffee (2026-07-22): "keeps all
        # stats but we go to lv one to exponentially level up our
        # character again... maybe making for another rebirth." Only
        # level/xp reset on rebirth (see bot.py's _do_rebirth) --
        # ability scores, gear, gold, skill-tree upgrades, everything
        # else stays exactly as-is, so rebirth_count is purely an
        # additive counter, never a reset-and-recompute of anything
        # else. hybrid_class is the freely-chosen (and re-choosable)
        # secondary class flavor this unlocks after the first rebirth
        # -- see rules/leveling.py's rebirth constants and bot.py's
        # hybrid feature hooks for how rebirth_count and hybrid_class
        # combine into real mechanical bonuses.
        if "rebirth_count" not in columns:
            conn.execute("ALTER TABLE characters ADD COLUMN rebirth_count INTEGER NOT NULL DEFAULT 0")
        if "hybrid_class" not in columns:
            conn.execute("ALTER TABLE characters ADD COLUMN hybrid_class TEXT")

        # Sequential dungeon gating (2026-07-24, Coffee: "make it so we
        # cant progress to certain areas ... until we complete the
        # dungeons or missions in sequence"): a location is "cleared"
        # once a character has won a real fight there -- see
        # _mark_location_cleared_for_party (bot.py), called from every
        # combat-victory checkpoint. Consumed by the new
        # requires_cleared_location story_gate (_check_story_gate) to
        # block deeper connections until the room before them is
        # actually fought through, not just walked past.
        if "cleared_locations" not in columns:
            conn.execute("ALTER TABLE characters ADD COLUMN cleared_locations TEXT NOT NULL DEFAULT '[]'")

        # Real subclass choice (2026-07-24, per Coffee: available from
        # the FIRST playthrough, unlike hybrid_class -- no rebirth gate
        # here). Wizard's Arcane Tradition is the pilot: spells.py
        # already tags every spell's real school, so "pick a school"
        # needs no new content, just a bonus applied where that school
        # matches (see bot.py's _do_choose_subclass/_do_cast_spell).
        if "subclass" not in columns:
            conn.execute("ALTER TABLE characters ADD COLUMN subclass TEXT")

        # died_at (2026-07-25, per Coffee: an AI companion not in anyone's
        # real party -- e.g. Bram Ashfield -- died in battle and had no
        # path back at all, since shrine revival only ever covers a real
        # party and a dead character can't act OR move, so it can't even
        # walk itself to a shrine. Timestamped whenever is_dead is set,
        # cleared whenever a character is revived by any path, so a
        # standalone AI companion's death can be auto-resolved by real
        # elapsed time (see bot.py's _maybe_revive_standalone_ai_companions)
        # instead of staying stuck forever with no human able to intervene.
        if "died_at" not in columns:
            conn.execute("ALTER TABLE characters ADD COLUMN died_at TEXT")

        # echo_trial_tier (2026-07-25, per Coffee: Silver Wardens'
        # Colosseum echo-trials -- a repeatable grind path parallel to
        # rebirth). Increments by 1 on every real echo-trial victory
        # (bot.py's _check_echo_trial_progress), scaling future echoes
        # harder (more HP/AC, eventually a real damage-type resistance)
        # -- never resets, a real, persistent measure of how many times
        # this character has proven itself against its own echoes.
        if "echo_trial_tier" not in columns:
            conn.execute("ALTER TABLE characters ADD COLUMN echo_trial_tier INTEGER NOT NULL DEFAULT 0")

        # is_benched (2026-07-31, per Coffee: parties can now grow past a
        # comfortable battle size -- "cap it at 6 per fight, all members
        # in party get experience and gold tho"). A real party member
        # (party_id set) can be manually benched out of THIS fight
        # without leaving the party -- _get_real_party_combatants
        # (bot.py) excludes benched members from who actually fights,
        # same choke point already used for location/resting filtering,
        # so they fall through to the existing "absent party member"
        # INACTIVE_PARTY_XP_SHARE reward path with no new reward logic
        # needed. Independent of is_inactive (resting) -- a benched
        # member can be wide awake and standing right next to the fight.
        if "is_benched" not in columns:
            conn.execute("ALTER TABLE characters ADD COLUMN is_benched INTEGER NOT NULL DEFAULT 0")

        # formation_row (2026-08-01, per Coffee: "character placement has
        # an effect in battle... allow us to customize the formations").
        # 'front' (default, matches every existing character's
        # undifferentiated behavior today -- no data migration risk) or
        # 'back'. Read directly off the combat participant dict the same
        # way is_benched already is -- see bot.py's
        # _pick_formation_weighted_target and rules/combat.py's
        # resolve_attack back-row AC bonus.
        if "formation_row" not in columns:
            conn.execute("ALTER TABLE characters ADD COLUMN formation_row TEXT NOT NULL DEFAULT 'front'")


def _row_to_dict(row: sqlite3.Row) -> dict:
    d = dict(row)
    d["inventory"] = json.loads(d["inventory"])
    d["known_spells"] = json.loads(d["known_spells"])
    d["completed_quests"] = json.loads(d["completed_quests"])
    d["visited_locations"] = json.loads(d["visited_locations"])
    d["skill_uses"] = json.loads(d["skill_uses"])
    d["active_quests"] = json.loads(d["active_quests"])
    d["feature_uses"] = json.loads(d["feature_uses"])
    d["equipped_accessories"] = json.loads(d["equipped_accessories"])
    d["known_monsters"] = json.loads(d["known_monsters"])
    d["cleared_locations"] = json.loads(d["cleared_locations"])
    d["achievements"] = json.loads(d["achievements"])
    d["map_revealed_locations"] = json.loads(d["map_revealed_locations"])
    d["skill_tree_upgrades"] = json.loads(d["skill_tree_upgrades"])
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


_NEXT_AI_ID: int | None = None  # lazily seeded from the DB — see create_ai_companion


def create_ai_companion(name: str, race: str, char_class: str, ability_scores: dict,
                         hp_max: int, armor_class: int, gold: int, inventory: dict) -> dict:
    """
    Create an AI-controlled companion character. Uses a synthetic negative
    telegram_user_id so it can never collide with a real player's ID, and
    is flagged is_ai=1 so the turn engine knows to auto-resolve its turns.
    It is its own "owner" for active_characters purposes (self-mapped).

    The ID counter is lazily seeded from whatever's already in the
    database, not a hardcoded -1000 — confirmed live 2026-07-11: a
    hardcoded counter that resets to -1000 on every process restart
    collided a brand-new autonomous AI party member with an
    already-existing recruited companion NPC that had claimed -1000 in
    an earlier process. active_characters silently rebound to the new
    character, orphaning the old one's activity tracking and corrupting
    both characters' round-robin turn order and situational grounding
    (the older character's frozen roster row kept winning turn-order
    ties forever, while every action actually dispatched under the
    shared ID landed on the newer character's real, moving location).
    """
    global _NEXT_AI_ID
    if _NEXT_AI_ID is None:
        with get_connection() as conn:
            row = conn.execute("SELECT MIN(telegram_user_id) AS m FROM characters").fetchone()
        lowest_existing = row["m"] if row and row["m"] is not None else 0
        _NEXT_AI_ID = min(lowest_existing, -1000) - 1 if lowest_existing < 0 else -1000
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

    json_fields = ("inventory", "known_spells", "completed_quests", "visited_locations", "skill_uses", "active_quests", "feature_uses", "equipped_accessories", "known_monsters", "cleared_locations", "achievements", "map_revealed_locations", "skill_tree_upgrades")
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


def update_character_by_id(character_id: int, **fields) -> dict | None:
    """
    Real bug caught live (2026-07-24, same incident as
    get_party_members_by_id_including_inactive_slots): update_character
    always writes to whichever character is CURRENTLY ACTIVE for a
    telegram_user_id -- there's no way for it to target a specific,
    non-active character row. This silently broke the shrine offering's
    actual revival write: it correctly FOUND a dead character sitting in
    an inactive slot (their owner had switched to play someone else
    while dead), sent a real "they gasp back to life" message, then
    called update_character(target["telegram_user_id"], is_dead=0, ...)
    -- which updated the owner's CURRENTLY ACTIVE character instead
    (a different, already-alive one), leaving the actually-dead
    character silently untouched. The player-facing message claimed
    success while nothing had actually changed. This variant targets an
    exact character_id directly, bypassing the active-character
    indirection entirely -- required whenever the character being
    modified might not be its owner's active slot (reviving a fallen
    ally who isn't currently being played is exactly that case).
    """
    if not fields:
        with get_connection() as conn:
            row = conn.execute("SELECT * FROM characters WHERE character_id = ?", (character_id,)).fetchone()
        return _row_to_dict(row) if row else None

    json_fields = ("inventory", "known_spells", "completed_quests", "visited_locations", "skill_uses", "active_quests", "feature_uses", "equipped_accessories", "known_monsters", "cleared_locations", "achievements", "map_revealed_locations", "skill_tree_upgrades")
    for key in json_fields:
        if key in fields and not isinstance(fields[key], str):
            fields[key] = json.dumps(fields[key])

    columns = ", ".join(f"{k} = ?" for k in fields)
    values = list(fields.values())

    with get_connection() as conn:
        conn.execute(
            f"UPDATE characters SET {columns} WHERE character_id = ?",
            values + [character_id],
        )
        row = conn.execute("SELECT * FROM characters WHERE character_id = ?", (character_id,)).fetchone()
    return _row_to_dict(row) if row else None


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


def _equipped_ring_ac_bonus(character: dict) -> int:
    """Sum of ac_bonus from every currently-equipped ring (e.g. Ring of Protection)."""
    total = 0
    for acc_id in character.get("equipped_accessories", []):
        acc = items_module.get_item(acc_id)
        if acc and acc.get("type") == "ring":
            total += acc.get("ac_bonus", 0)
    return total


def equip_item(telegram_user_id: int, item_id: str) -> tuple[bool, str, dict | None]:
    """
    Equip a weapon, armor, shield, ring, amulet, or wondrous item the
    character is actually carrying. Returns (success, message,
    updated_character). Real bug fixed 2026-07-15: items.py's weapon
    (damage_dice/ability), armor (ac_base), and shield (ac_bonus)
    fields existed the whole time but nothing ever equipped anything or
    read them in combat -- every attack used one hardcoded default
    weapon regardless of what was bought/found, and armor_class never
    changed after character creation. Equipping armor recomputes
    armor_class the same way character creation does (ac_base + DEX
    modifier) -- a deliberate simplification (no heavy-armor-caps-DEX
    nuance) consistent with this build's existing style elsewhere. A
    shield is a real 5E SEPARATE slot from armor (worn in addition to
    it, not instead), so its ac_bonus is added on top of whatever
    armor_class already is, rather than replacing it -- and correctly
    re-added if armor is equipped/changed afterward while a shield is
    already worn.

    Rings/amulets/wondrous items (2026-07-15, second reverse-
    playthrough finding same night: Ring of Protection, Ring of the
    Undertow, Amulet of Health, and Cloak of Elvenkind all had real
    mechanical fields -- ac_bonus, constitution_set, stealth_advantage
    -- that nothing ever read either) go into equipped_accessories, a
    list rather than one named slot each, since real 5E allows wearing
    several of these at once (e.g. two rings). Armor/shield equips
    re-sum every currently-worn ring's ac_bonus so order never matters.
    """
    character = get_character(telegram_user_id)
    if character is None:
        return False, "No character found.", None
    if character["inventory"].get(item_id, 0) < 1:
        return False, "You don't have that to equip.", character

    item = items_module.get_item(item_id)
    if item is None or item.get("type") not in ("weapon", "armor", "shield", "ring", "amulet", "wondrous"):
        return False, f"{item['name'] if item else item_id} isn't something you can equip.", character

    if item["type"] == "weapon":
        updated = update_character(telegram_user_id, equipped_weapon=item_id)
        return True, f"You equip the {item['name']}.", updated

    current_shield_bonus = 0
    if character.get("equipped_shield"):
        current_shield = items_module.get_item(character["equipped_shield"])
        if current_shield:
            current_shield_bonus = current_shield.get("ac_bonus", 0)
    ring_bonus = _equipped_ring_ac_bonus(character)

    if item["type"] == "armor":
        dex_mod = ability_modifier(character["dexterity"])
        new_ac = item["ac_base"] + dex_mod + current_shield_bonus + ring_bonus
        updated = update_character(telegram_user_id, equipped_armor=item_id, armor_class=new_ac)
        return True, f"You put on the {item['name']} (AC {new_ac}).", updated

    if item["type"] == "shield":
        # Additive on top of current AC, swapping out any previously-
        # equipped shield's bonus first rather than stacking both.
        new_ac = character["armor_class"] - current_shield_bonus + item["ac_bonus"]
        updated = update_character(telegram_user_id, equipped_shield=item_id, armor_class=new_ac)
        return True, f"You raise the {item['name']} (AC {new_ac}).", updated

    # Ring / amulet / wondrous: added to the accessories list (worn
    # alongside weapon/armor/shield, not instead of them).
    accessories = character["equipped_accessories"]
    if item_id in accessories:
        return False, f"You're already wearing the {item['name']}.", character
    accessories = accessories + [item_id]
    updates = {"equipped_accessories": accessories}
    note_parts = [f"You put on the {item['name']}."]

    if item.get("ac_bonus"):
        new_ac = character["armor_class"] + item["ac_bonus"]
        updates["armor_class"] = new_ac
        note_parts.append(f"AC is now {new_ac}.")
    if item.get("constitution_set") and item["constitution_set"] > character["constitution"]:
        updates["constitution"] = item["constitution_set"]
        note_parts.append(f"Constitution is now {item['constitution_set']}.")

    updated = update_character(telegram_user_id, **updates)
    return True, " ".join(note_parts), updated


def auto_equip_best_gear(telegram_user_id: int) -> tuple[str, dict | None]:
    """
    Picks the real best weapon (highest average damage -- see
    rules.dice.average_damage) and real best armor (highest ac_base)
    out of whatever this character is actually carrying, and equips
    both via equip_item above. Added 2026-07-15 alongside equip_item
    itself, per Coffee: a player shouldn't have to know every weapon's
    exact damage die to get sensible gear on -- this picks for them.
    Always returns a real, honest summary, even if there was nothing
    to equip in one or both slots (never silently no-ops).
    """
    character = get_character(telegram_user_id)
    if character is None:
        return "No character found.", None

    weapon_ids = [
        item_id for item_id, qty in character["inventory"].items()
        if qty > 0 and (items_module.get_item(item_id) or {}).get("type") == "weapon"
    ]
    armor_ids = [
        item_id for item_id, qty in character["inventory"].items()
        if qty > 0 and (items_module.get_item(item_id) or {}).get("type") == "armor"
    ]

    messages = []
    if weapon_ids:
        best_weapon = max(
            weapon_ids,
            key=lambda i: average_damage(items_module.get_item(i)["damage_dice"],
                                          items_module.get_item(i).get("damage_bonus", 0)),
        )
        _, msg, character = equip_item(telegram_user_id, best_weapon)
        messages.append(msg)
    else:
        messages.append("No weapon carried to equip.")

    if armor_ids:
        best_armor = max(armor_ids, key=lambda i: items_module.get_item(i)["ac_base"])
        _, msg, character = equip_item(telegram_user_id, best_armor)
        messages.append(msg)
    else:
        messages.append("No armor carried to equip.")

    shield_ids = [
        item_id for item_id, qty in character["inventory"].items()
        if qty > 0 and (items_module.get_item(item_id) or {}).get("type") == "shield"
    ]
    if shield_ids:
        best_shield = max(shield_ids, key=lambda i: items_module.get_item(i)["ac_bonus"])
        _, msg, character = equip_item(telegram_user_id, best_shield)
        messages.append(msg)

    # Rings/amulets/wondrous items (2026-07-15): unlike weapon/armor/
    # shield, these aren't "pick the single best one" -- real 5E lets a
    # character wear several accessories at once (e.g. two rings), so
    # auto_equip puts on every one currently carried but not yet worn.
    accessory_ids = [
        item_id for item_id, qty in character["inventory"].items()
        if qty > 0 and (items_module.get_item(item_id) or {}).get("type") in ("ring", "amulet", "wondrous")
        and item_id not in character["equipped_accessories"]
    ]
    for accessory_id in accessory_ids:
        success, msg, character = equip_item(telegram_user_id, accessory_id)
        if success:
            messages.append(msg)

    return " ".join(messages), character


def move_character(telegram_user_id: int, new_location: str) -> dict | None:
    return update_character(telegram_user_id, current_location=new_location)


def mark_visited(telegram_user_id: int, location_id: str) -> dict | None:
    """
    Adds location_id to this character's visited_locations, moving it to
    the end if already present. Real bug, reported live 2026-07-18: a
    player who rested in The Whispering Wood expected to wake at The
    Crossroads Tavern but woke at Market Row instead. Root cause was
    here -- a revisit used to be a silent no-op, so the list's order
    only ever reflected each location's FIRST-ever visit, not its most
    recent one. _nearest_safe_waypoint (bot.py) scans this list in
    reverse specifically to find the most-recently-visited safe
    location, so for any character who visited a shop (Market Row,
    itself a safe location) before their very first trip to the tavern
    -- true for most characters, since the tavern is the starting
    location and gets visited constantly thereafter -- the tavern's one
    early list position never advanced past the shop's, even after
    dozens of later tavern visits. Moving the entry to the end on every
    revisit makes the list a genuine recency order, matching what
    _nearest_safe_waypoint's docstring already claimed it did.
    """
    character = get_character(telegram_user_id)
    if character is None:
        return None
    visited = character["visited_locations"]
    if location_id in visited:
        visited.remove(location_id)
    visited.append(location_id)
    return update_character(telegram_user_id, visited_locations=visited)


def mark_known_monster(telegram_user_id: int, monster_key: str) -> dict | None:
    """
    Adds monster_key to this character's known_monsters (bestiary
    discovery), if new -- same fog-of-war pattern as mark_visited above,
    called once a character has actually fought a given monster type.
    """
    character = get_character(telegram_user_id)
    if character is None:
        return None
    if monster_key in character["known_monsters"]:
        return character
    character["known_monsters"].append(monster_key)
    return update_character(telegram_user_id, known_monsters=character["known_monsters"])


def mark_location_cleared(telegram_user_id: int, location_id: str) -> dict | None:
    """
    Marks a location as "cleared" -- this character has won a real fight
    there at least once. Same fog-of-war-style pattern as mark_visited/
    mark_known_monster, called only from a real combat-victory checkpoint
    (see bot.py's _mark_location_cleared_for_party), never on a loss or
    fled fight. Consumed by _check_story_gate's requires_cleared_location
    check to block deeper connections until the room before them has
    actually been fought through.
    """
    character = get_character(telegram_user_id)
    if character is None:
        return None
    if location_id in character["cleared_locations"]:
        return character
    character["cleared_locations"].append(location_id)
    return update_character(telegram_user_id, cleared_locations=character["cleared_locations"])


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


def get_character_by_id(character_id: int) -> dict | None:
    """
    Returns one exact character row, regardless of whether it's that
    owner's currently active character -- needed anywhere a specific,
    possibly-inactive character must be targeted directly (e.g. the
    shrine's tap-to-revive buttons resolving a specific dead party
    member, not whichever character that player happens to have active
    right now). See update_character_by_id's docstring for the real
    live incident this pairs with.
    """
    with get_connection() as conn:
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


def get_leaderboard(limit: int = 10) -> list[dict]:
    """
    Real Hall of Fame ranking (task #74, 2026-07-17): every player's
    currently-ACTIVE character (same active_characters join as
    get_character, so an abandoned/inactive alt slot never clutters the
    board), ranked by real XP earned. Excludes is_ai=1 (combat-only
    companions with no independent existence) but deliberately does NOT
    exclude is_autonomous=1 -- the autonomous AI-played party plays
    through the exact same real pipeline as any human (see CLAUDE.md's
    design philosophy: "AI-driven players sit at the same table under
    the same rules"), so their real XP counts exactly the same.
    """
    with get_connection() as conn:
        rows = conn.execute(
            """
            SELECT c.* FROM characters c
            JOIN active_characters a ON a.character_id = c.character_id
            WHERE c.is_deleted = 0 AND c.is_ai = 0
            ORDER BY c.xp DESC, c.level DESC
            LIMIT ?
            """,
            (limit,),
        ).fetchall()
    return [_row_to_dict(r) for r in rows]


def find_character_by_name(name: str) -> dict | None:
    """
    Finds any non-deleted character (human or AI, active or a player's
    other non-active character slot) by name, case-insensitive. Used
    for read-only sheet lookups -- Coffee wants to see anyone's sheet,
    not just whoever's currently marked active (2026-07-14).
    """
    with get_connection() as conn:
        row = conn.execute(
            "SELECT * FROM characters WHERE is_deleted = 0 AND LOWER(name) = LOWER(?) "
            "ORDER BY character_id LIMIT 1",
            (name,),
        ).fetchone()
    return _row_to_dict(row) if row else None


def find_character_by_telegram_username(username: str) -> dict | None:
    """
    Resolves a real Telegram @username (2026-07-17, per Coffee: "give
    (item) to (telegram user/playername)" and /msg's target should
    accept a tagged Telegram user, not just a character name) to
    whichever non-deleted character that account currently has active.
    Case-insensitive since Telegram usernames aren't case-sensitive.
    Only matches telegram_username values already captured via
    update_telegram_username (set opportunistically on real incoming
    messages) -- a player who has never sent a message since this
    column existed simply won't resolve this way yet, same as any
    other freshly-added, backfill-free column in this file.
    """
    username = username.lstrip("@")
    with get_connection() as conn:
        row = conn.execute(
            "SELECT * FROM characters WHERE is_deleted = 0 AND LOWER(telegram_username) = LOWER(?) "
            "ORDER BY character_id LIMIT 1",
            (username,),
        ).fetchone()
    return _row_to_dict(row) if row else None


def update_telegram_username(telegram_user_id: int, username: str | None) -> None:
    """
    Keeps a character's telegram_username fresh from the real incoming
    Update on every message (Telegram usernames can change, and are
    None for accounts that don't have one set) -- a no-op if this user
    has no active character yet, since there's nothing to attach it to.
    """
    if not username:
        return
    with get_connection() as conn:
        character_id = _active_character_id(telegram_user_id, conn)
        if character_id is None:
            return
        conn.execute(
            "UPDATE characters SET telegram_username = ? WHERE character_id = ?",
            (username, character_id),
        )


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
    # Rebirth (2026-07-22, per Coffee): a permanent, stacking XP-gain
    # bonus is the actual "exponentially level up again" reward for
    # going through a rebirth -- see rules/leveling.py's
    # xp_gain_multiplier. A never-reborn character gets exactly the
    # same XP as before (multiplier 1.0).
    amount = round(amount * xp_gain_multiplier(character.get("rebirth_count", 0)))
    new_xp = character["xp"] + amount
    # Defensive floor (2026-07-27, found via real playthrough testing,
    # not yet seen live): level_for_xp recomputes level from xp alone
    # with no memory of the stored level, so if xp/level were EVER out
    # of sync for any reason (an out-of-band DB write, a future
    # migration script that touches one field but not the other), the
    # very next XP award of any size -- even 1 XP -- would silently
    # demote the character back down to whatever their (lagging) xp
    # implies. Gaining XP should never be able to LOWER a level.
    new_level = max(old_level, level_for_xp(new_xp))
    new_prof = proficiency_bonus_for_level(new_level)

    updates = {"xp": new_xp, "level": new_level, "proficiency_bonus": new_prof}

    if new_level > old_level:
        con_mod = (character["constitution"] - 10) // 2
        hp_per_level = hp_gain_for_level(character["char_class"], con_mod)
        levels_gained = new_level - old_level
        # EVOLUTION_HP_MULTIPLIER (2026-07-25, per Coffee: "by level 99
        # the first playthrough before evolution the HP would be apx
        # 3200 -- then scale that"): scales real 5E average HP gain up
        # to this game's own much larger power curve.
        hp_gain_total = hp_per_level * levels_gained * EVOLUTION_HP_MULTIPLIER
        new_hp_max = character["hp_max"] + hp_gain_total
        updates["hp_max"] = new_hp_max
        # Leveling up also heals you back up by the same amount gained,
        # a common, player-friendly 5E table convention.
        updates["hp_current"] = min(character["hp_current"] + hp_gain_total, new_hp_max)

        asi_count = sum(1 for lvl in range(old_level + 1, new_level + 1) if lvl in ASI_LEVELS)
        if asi_count:
            # Banked, not auto-applied (2026-07-16, per Coffee): the
            # player spends these whenever they want by saying "level
            # up" -- see bot.py's _do_level_up. Real 5E lets you defer
            # ASIs indefinitely too, so this has no expiry.
            updates["pending_asi_points"] = character.get("pending_asi_points", 0) + 2 * asi_count

        # Task #131, per Coffee: a real skill-tree point system on
        # level-up -- 1 point per level gained, spent on real,
        # class-flavored upgrades (see bot.py's SKILL_TREE_UPGRADES /
        # _do_show_skill_tree). Banked the same way pending_asi_points
        # is, no expiry.
        updates["skill_points"] = character.get("skill_points", 0) + levels_gained

        newly_unlocked = spells_module.spells_unlocked_at_level(character["char_class"], new_level)
        newly_learned = [s for s in newly_unlocked if s not in character["known_spells"]]
        if newly_learned:
            updates["known_spells"] = character["known_spells"] + newly_learned

    return update_character(telegram_user_id, **updates)


# ---------------------------------------------------------------------
# NPC relationships — persistent, per-(player, NPC) memory and rapport.
# ---------------------------------------------------------------------

MAX_MEMORY_EVENTS = 20  # oldest facts drop off rather than growing forever

_RELATIONSHIP_DEFAULTS = {"affinity": 0, "memory_events": [], "banned": 0, "resolution": "unresolved"}


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


def resolve_companion(telegram_user_id: int, npc_id: str, state: str) -> dict:
    """
    Writes a companion's final resolution state once, at the moment their
    quest chain's final stage completes -- e.g. "resolved_loyal",
    "resolved_distant", "resolved_estranged" depending on which trust
    band `affinity` was in at that checkpoint. Reuses the existing
    npc_relationships row (a companion is just an NPC a player has a real
    relationship with) rather than a new table -- see the full-storyline
    plan's "extend, don't invent" design.
    """
    get_relationship(telegram_user_id, npc_id)  # ensure the row exists
    with get_connection() as conn:
        conn.execute(
            "UPDATE npc_relationships SET resolution = ? WHERE telegram_user_id = ? AND npc_id = ?",
            (state, telegram_user_id, npc_id),
        )
    return get_relationship(telegram_user_id, npc_id)


def get_companion_resolution(telegram_user_id: int, npc_id: str) -> str:
    return get_relationship(telegram_user_id, npc_id)["resolution"]


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
# Player-run marketplace (task #79) — a real listing at a fixed price,
# never a bid/auction. Global, not location-scoped, for this first
# version.
# ---------------------------------------------------------------------

def create_market_listing(seller_id: int, seller_name: str, item_id: str, quantity: int, price: int) -> int:
    with get_connection() as conn:
        cur = conn.execute(
            "INSERT INTO market_listings (seller_id, seller_name, item_id, quantity, price, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (seller_id, seller_name, item_id, quantity, price, datetime.now(timezone.utc).isoformat()),
        )
        return cur.lastrowid


def get_market_listings() -> list[dict]:
    with get_connection() as conn:
        rows = conn.execute("SELECT * FROM market_listings ORDER BY listing_id").fetchall()
    return [dict(r) for r in rows]


def get_market_listing(listing_id: int) -> dict | None:
    with get_connection() as conn:
        row = conn.execute(
            "SELECT * FROM market_listings WHERE listing_id = ?", (listing_id,)
        ).fetchone()
    return dict(row) if row else None


def remove_market_listing(listing_id: int) -> None:
    with get_connection() as conn:
        conn.execute("DELETE FROM market_listings WHERE listing_id = ?", (listing_id,))


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
# feature_uses: same shape/rest-gating as skill_uses, but for limited-use
# class/racial features (Second Wind, Rage, Bardic Inspiration, Lay on
# Hands, Relentless Endurance) instead of ability practice. Reset to {}
# by _apply_natural_healing (bot.py) only on a FULL rest completion --
# these are real "per long rest" resources, not gradually recovered like
# HP/spell slots, so they either reset all at once or not at all.
# ---------------------------------------------------------------------

def get_feature_uses(telegram_user_id: int, feature_id: str) -> int:
    character = get_character(telegram_user_id)
    return character["feature_uses"].get(feature_id, 0) if character else 0


def use_feature(telegram_user_id: int, feature_id: str) -> int:
    """Increments and returns the new use count for this feature this rest cycle."""
    character = get_character(telegram_user_id)
    if character is None:
        return 0
    uses = character["feature_uses"]
    uses[feature_id] = uses.get(feature_id, 0) + 1
    update_character(telegram_user_id, feature_uses=uses)
    return uses[feature_id]


def reset_feature_uses(telegram_user_id: int) -> None:
    """Clears all limited-use feature counters -- called on a full rest."""
    update_character(telegram_user_id, feature_uses={})


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


def update_login_streak(telegram_user_id: int) -> tuple[int, bool] | None:
    """
    Login streak (task #78): a real consecutive-real-calendar-day
    counter, checked from the SAME real-activity checkpoint as
    touch_last_active. Returns (current_streak_days, is_new_day) --
    is_new_day is False if this player already had today counted (so
    the caller doesn't re-announce/re-reward on every message), or
    None entirely for AI characters (companions/autonomous party),
    which don't have a real login of their own.
    """
    character = get_character(telegram_user_id)
    if character is None or character.get("is_ai"):
        return None

    today = datetime.now(timezone.utc).date()
    streak = character.get("login_streak_days", 0)
    last_login_date = character.get("last_login_date")

    if last_login_date == today.isoformat():
        return streak, False

    last_date = None
    if last_login_date:
        try:
            last_date = datetime.fromisoformat(last_login_date).date()
        except ValueError:
            last_date = None

    streak = streak + 1 if last_date is not None and (today - last_date).days == 1 else 1
    update_character(telegram_user_id, login_streak_days=streak, last_login_date=today.isoformat())
    return streak, True


def claim_guild_quest_if_unclaimed_today(telegram_user_id: int, reward_gold: int, reward_xp: int) -> bool:
    """
    Guild quests (task #77): same real-day-gating idea as
    update_login_streak, but a flat "claimed or not today" flag rather
    than a growing streak, since the guild quest's objective doesn't
    accumulate -- it's won once per real day. Returns True (and applies
    the reward) only the first time this is called for a given
    character on a given real day; every later call that same day is a
    silent no-op, so combat victories after the first won't double-pay.
    """
    character = get_character(telegram_user_id)
    if character is None:
        return False
    today = datetime.now(timezone.utc).date().isoformat()
    if character.get("last_guild_quest_date") == today:
        return False
    update_character(
        telegram_user_id,
        last_guild_quest_date=today,
        gold=character["gold"] + reward_gold,
    )
    add_xp(telegram_user_id, reward_xp)
    return True


def mark_inactive(telegram_user_id: int) -> dict | None:
    return update_character(telegram_user_id, is_inactive=1)


def mark_active(telegram_user_id: int) -> dict | None:
    return update_character(telegram_user_id, is_inactive=0)


def set_do_not_disturb(telegram_user_id: int, enabled: bool) -> dict | None:
    return update_character(telegram_user_id, do_not_disturb=1 if enabled else 0)


def set_status_note(telegram_user_id: int, note: str | None) -> dict | None:
    return update_character(telegram_user_id, status_note=note)


def unlock_achievement(telegram_user_id: int, achievement_id: str) -> dict | None:
    """Idempotent -- adding an already-unlocked achievement again is a no-op."""
    character = get_character(telegram_user_id)
    if character is None:
        return None
    if achievement_id in character["achievements"]:
        return character
    character["achievements"].append(achievement_id)
    return update_character(telegram_user_id, achievements=character["achievements"])


def set_active_title(telegram_user_id: int, title: str | None) -> dict | None:
    return update_character(telegram_user_id, active_title=title)


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


# --- Game-wide settings (simple key/value toggles, e.g. TTS on/off) ---

def get_setting(key: str, default: str | None = None) -> str | None:
    with get_connection() as conn:
        row = conn.execute("SELECT value FROM game_settings WHERE key = ?", (key,)).fetchone()
    return row["value"] if row else default


def set_setting(key: str, value: str) -> None:
    with get_connection() as conn:
        conn.execute(
            "INSERT INTO game_settings (key, value) VALUES (?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (key, value),
        )


# --- Trusted-dev allowlist (2026-07-17, per Coffee: a genuine second
# dev like Sugar needs Development-topic access without being made a
# real Telegram group admin, which grants far more than intended --
# deleting messages, banning members, etc. Managed via bot.py's
# /add_admin and /remove_admin, gated to whoever already has Dev-topic
# access.) ---

def get_trusted_dev_ids() -> list[int]:
    raw = get_setting("trusted_dev_ids", "[]")
    try:
        return json.loads(raw)
    except (TypeError, ValueError):
        return []


def add_trusted_dev_id(telegram_user_id: int) -> None:
    ids = set(get_trusted_dev_ids())
    ids.add(telegram_user_id)
    set_setting("trusted_dev_ids", json.dumps(sorted(ids)))


def remove_trusted_dev_id(telegram_user_id: int) -> None:
    ids = set(get_trusted_dev_ids())
    ids.discard(telegram_user_id)
    set_setting("trusted_dev_ids", json.dumps(sorted(ids)))


# --- Player bans (2026-07-17, per Coffee: admins/devs can ban
# malicious players outright) ---

def is_banned(telegram_user_id: int) -> bool:
    return telegram_user_id in _get_banned_ids()


def _get_banned_ids() -> list[int]:
    raw = get_setting("banned_user_ids", "[]")
    try:
        return json.loads(raw)
    except (TypeError, ValueError):
        return []


def ban_user(telegram_user_id: int) -> None:
    ids = set(_get_banned_ids())
    ids.add(telegram_user_id)
    set_setting("banned_user_ids", json.dumps(sorted(ids)))


def unban_user(telegram_user_id: int) -> None:
    ids = set(_get_banned_ids())
    ids.discard(telegram_user_id)
    set_setting("banned_user_ids", json.dumps(sorted(ids)))


def list_banned_user_ids() -> list[int]:
    """Public wrapper for /ban_list -- _get_banned_ids stays private since it's an internal storage detail."""
    return _get_banned_ids()


# --- Warnings / infractions (2026-07-17, per Coffee: "/warning system
# admins can use to reply to users that are out-of-line... 3
# infractions results in a ban") ---

def _get_all_infractions() -> dict:
    raw = get_setting("infractions", "{}")
    try:
        return json.loads(raw)
    except (TypeError, ValueError):
        return {}


def get_infractions(telegram_user_id: int) -> list[dict]:
    return _get_all_infractions().get(str(telegram_user_id), [])


def get_all_infractions() -> dict:
    """{telegram_user_id_str: [{"reason", "issued_by", "timestamp"}, ...]} -- for /warning_list."""
    return _get_all_infractions()


def add_infraction(telegram_user_id: int, reason: str, issued_by: int) -> int:
    """Records a new infraction and returns this user's new total count."""
    all_infractions = _get_all_infractions()
    key = str(telegram_user_id)
    records = all_infractions.get(key, [])
    records.append({
        "reason": reason,
        "issued_by": issued_by,
        "timestamp": datetime.now(timezone.utc).isoformat(),
    })
    all_infractions[key] = records
    set_setting("infractions", json.dumps(all_infractions))
    return len(records)


def clear_infractions(telegram_user_id: int) -> None:
    """Used when reversing an auto-ban -- gives the player a clean slate rather than an immediate re-ban risk."""
    all_infractions = _get_all_infractions()
    all_infractions.pop(str(telegram_user_id), None)
    set_setting("infractions", json.dumps(all_infractions))


# --- Area quest board ---

def _board_quest_row_to_dict(row) -> dict:
    d = dict(row)
    d["branch_data"] = json.loads(d["branch_data"]) if d.get("branch_data") else None
    return d


def get_active_board_quests(location_id: str, day_key: str, tier: str = "daily") -> list[dict]:
    """All of this period's board quests for this location (accepted or not), oldest first."""
    with get_connection() as conn:
        rows = conn.execute(
            "SELECT * FROM board_quests WHERE location_id = ? AND day_key = ? AND tier = ? "
            "ORDER BY board_quest_id ASC",
            (location_id, day_key, tier),
        ).fetchall()
    return [_board_quest_row_to_dict(r) for r in rows]


def get_active_board_quest(location_id: str, day_key: str, tier: str = "daily") -> dict | None:
    """The single most-recent board quest for this location this period, if any (accepted or not)."""
    with get_connection() as conn:
        row = conn.execute(
            "SELECT * FROM board_quests WHERE location_id = ? AND day_key = ? AND tier = ? "
            "ORDER BY board_quest_id DESC LIMIT 1",
            (location_id, day_key, tier),
        ).fetchone()
    return _board_quest_row_to_dict(row) if row else None


def create_board_quest(location_id: str, day_key: str, title: str, description: str,
                        giver_npc: str | None, objective_type: str, objective_target: str,
                        objective_count: int, reward_xp: int, reward_gold: int,
                        tier: str = "daily") -> dict:
    with get_connection() as conn:
        cur = conn.execute(
            """
            INSERT INTO board_quests (
                location_id, day_key, title, description, giver_npc,
                objective_type, objective_target, objective_count,
                reward_xp, reward_gold, generated_at, tier
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (location_id, day_key, title, description, giver_npc,
             objective_type, objective_target, objective_count,
             reward_xp, reward_gold, datetime.now(timezone.utc).isoformat(), tier),
        )
        board_quest_id = cur.lastrowid
        row = conn.execute(
            "SELECT * FROM board_quests WHERE board_quest_id = ?", (board_quest_id,)
        ).fetchone()
    return _board_quest_row_to_dict(row)


BOARD_QUEST_TIER_EXPIRY_HOURS = {"daily": 24, "weekly": 7 * 24, "monthly": 30 * 24}


def accept_board_quest(board_quest_id: int, telegram_user_id: int) -> dict | None:
    """
    Expiry window scales with the quest's own tier (2026-07-25,
    per Coffee's weekly/monthly missions ask) -- 24h for a daily bounty,
    same as always, but a real 7 days for a weekly one and 30 for a
    monthly one, so accepting a bigger mission doesn't hand back a
    24h-or-lose-it window that was only ever sized for the daily kind.
    """
    now = datetime.now(timezone.utc)
    with get_connection() as conn:
        existing = conn.execute(
            "SELECT tier FROM board_quests WHERE board_quest_id = ?", (board_quest_id,)
        ).fetchone()
        tier = existing["tier"] if existing else "daily"
        expiry_hours = BOARD_QUEST_TIER_EXPIRY_HOURS.get(tier, 24)
        expires = now.timestamp() + (expiry_hours * 3600)
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


def increment_board_quests_completed(telegram_user_id: int) -> None:
    """
    Board quests track their own completion (board_quests.completed_at)
    entirely separately from a character's story-quest completed_quests
    log, so a player's board-quest history never showed up anywhere on
    their own character (2026-07-16, per Coffee). This is a simple
    running count, not a title list -- board quests are randomly
    generated and not worth naming individually the way story quests
    are, but "how many bounties has this character finished" is real,
    persistent progress worth showing.
    """
    with get_connection() as conn:
        conn.execute(
            "UPDATE characters SET board_quests_completed = board_quests_completed + 1 WHERE telegram_user_id = ?",
            (telegram_user_id,),
        )


def get_accepted_board_quests_for_user(telegram_user_id: int) -> list[dict]:
    """A player's currently-accepted, not-yet-completed board quests (any location)."""
    with get_connection() as conn:
        rows = conn.execute(
            "SELECT * FROM board_quests WHERE accepted_by = ? AND completed_at IS NULL",
            (telegram_user_id,),
        ).fetchall()
    return [_board_quest_row_to_dict(r) for r in rows]


def get_accepted_board_quests_at_location(location_id: str) -> list[dict]:
    """
    Every currently-accepted, not-yet-completed board quest at this
    location, regardless of which day it was posted/accepted (task #149,
    2026-07-17: get_active_board_quests's day_key filter meant a quest
    accepted yesterday but still within its real 24h expires_at window
    silently dropped out of crediting once the calendar day rolled over
    -- gathering/killing had nothing left to match against even though
    the quest was still legitimately accepted and shown to the player).
    Unlike get_accepted_board_quests_for_user, scoped by location instead
    of by player, since combat crediting isn't tied to one specific
    acceptor -- any accepted quest at the party's location can be fed by
    the kill.
    """
    with get_connection() as conn:
        rows = conn.execute(
            "SELECT * FROM board_quests WHERE location_id = ? "
            "AND accepted_by IS NOT NULL AND completed_at IS NULL",
            (location_id,),
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


def get_party_members_by_id_including_inactive_slots(party_id: int) -> list[dict]:
    """
    Real bug caught live (2026-07-24, Coffee: "she's currently dead"
    but the shrine kept saying "there's no one to bring back"):
    get_party_members_by_id's active_characters join means a dead real
    player who has since SWITCHED to a different character (per
    CLAUDE.md's own documented behavior -- "the player can switch to
    another character of theirs in the meantime" while dead) silently
    vanishes from every party lookup entirely, since her dead character
    is no longer her account's active slot -- a different, alive
    character of hers is. Confirmed live: Laurienna's real owner had
    switched to a character named Charvenna, so Laurienna (is_dead=1,
    genuinely sitting in the party) never showed up in any
    active-characters-gated query at all. This variant is scoped by
    party_id alone, matching every OTHER member sharing this party --
    exactly what revival features (shrine offering, Tent/Cabin/House)
    need, since a permanently-dead character is precisely the kind of
    "inactive slot" real player death leaves behind.
    """
    with get_connection() as conn:
        rows = conn.execute(
            "SELECT * FROM characters WHERE party_id = ? AND is_deleted = 0",
            (party_id,),
        ).fetchall()
    return [_row_to_dict(r) for r in rows]


def list_all_active_real_players() -> list[dict]:
    """
    Every real (non-AI) player's currently active character, across the
    whole game -- not scoped to one party or session. Used by task
    #118's text_mention wiring (bot.py's _safe_send): a narration line
    can name a real player who isn't in the current combat/party at all
    (e.g. an achievement, a guild-quest announcement), so the candidate
    list for "does this message name a real player" has to be every
    real player, not just the ones already in scope.
    """
    with get_connection() as conn:
        rows = conn.execute(
            "SELECT c.* FROM characters c JOIN active_characters a ON a.character_id = c.character_id "
            "WHERE c.is_ai = 0 AND c.is_deleted = 0",
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


def get_ai_controlled_characters() -> list[dict]:
    """
    Every AI-controlled character -- the separate autonomous party
    AND recruited companions alike (2026-07-14, per Coffee: "make
    recruitable able to make their own choices... this goes for AIs
    also"). Broader than get_autonomous_players, which only covers the
    hardcoded autonomous party and previously left recruits like Sarah
    doing nothing on their own between being talked to.
    """
    with get_connection() as conn:
        rows = conn.execute(
            "SELECT * FROM characters WHERE is_ai = 1 AND is_deleted = 0 ORDER BY character_id"
        ).fetchall()
    return [_row_to_dict(r) for r in rows]
