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
import collections
import json
import sqlite3
import threading
from contextlib import contextmanager
from datetime import datetime, timezone

import campaign_loader
import config
import items as items_module
import class_features as class_features_module
from rules.dice import ability_modifier, average_damage
from rules.leveling import (
    level_for_xp, proficiency_bonus_for_level,
    hp_gain_for_level, ASI_LEVELS, xp_gain_multiplier, EVOLUTION_HP_MULTIPLIER,
)
from rules.item_sets import active_set_bonus_affixes
from rules.item_generator import TIERS, TIER_BONUS, TIER_PRICE_MULT
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

# Real per-instance magic items (2026-08-02, per Coffee: "make loot real"
# -- a Diablo-style system). Every OTHER item reference in this game is a
# bare item_id string into items.py's static, hand-authored ITEMS dict --
# fine for a fixed catalog, but there was never a way to say "this
# specific ring rolled ac_bonus 3, a different one of the same name
# rolled ac_bonus 1." Sibling table to market_listings above, same
# migration convention. `base_stats` is the item's un-bonused base shape
# (damage_dice/ability/weapon_category/armor_category/ac_base/ac_bonus/
# damage_type -- whatever rules/item_generator.py's generate_weapon/
# generate_armor/generate_shield already produce, minus the tier bonus,
# which is now an affix instead of baked into base_stats -- see that
# module's own 2026-08-02 refactor). `affixes` is a JSON list of
# independently-attachable effect dicts (stat_bonus now; elemental_damage/
# resistance/vulnerability/immunity, grants_spell, profession_bonus in
# later phases) -- this list is the whole point of the architecture: every
# later feature (elemental affixes, granted spells, profession bonuses,
# set bonuses, enchanting, forging) is just "add one more dict to this
# list" using the same shared vocabulary, never a bespoke merge per
# feature. `set_id` is unused until the set-bonus phase.
CREATE_ITEM_INSTANCES_TABLE = """
CREATE TABLE IF NOT EXISTS item_instances (
    instance_id INTEGER PRIMARY KEY AUTOINCREMENT,
    item_type TEXT NOT NULL,
    name TEXT NOT NULL,
    rarity TEXT NOT NULL,
    price INTEGER NOT NULL DEFAULT 0,
    base_stats TEXT NOT NULL DEFAULT '{}',
    affixes TEXT NOT NULL DEFAULT '[]',
    set_id TEXT,
    created_at TEXT NOT NULL,
    source TEXT NOT NULL DEFAULT 'loot'
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

# The Labyrinth (2026-09-01, per Coffee: "start the labyrinth
# architecture" -- a live, procedurally-generated, ever-deepening
# dungeon unlocked by defeating colosseum_champion). Runs are SHARED
# per party, not per character -- confirmed with Coffee directly, same
# as how every other real dungeon/combat already works -- so this is
# its own table, one row per active run, rather than a character
# column (two party members' own JSON blobs would drift out of sync).
# party_key unifies partied ("party:<party_id>") and solo
# ("solo:<telegram_user_id>") lookups into one indexed column, avoiding
# NULL-handling special cases a nullable party_id would need. Because
# this lives in the database (not sessions.py's in-memory-only model),
# a run survives a bot restart for free -- no snapshot-file safety net
# needed, unlike combat sessions.
CREATE_LABYRINTH_RUNS_TABLE = """
CREATE TABLE IF NOT EXISTS labyrinth_runs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    chat_id INTEGER NOT NULL,
    party_key TEXT NOT NULL,
    floor INTEGER NOT NULL DEFAULT 1,
    seed INTEGER NOT NULL,
    current_room_id TEXT NOT NULL,
    rooms_json TEXT NOT NULL DEFAULT '{}',
    modifier TEXT,
    entered_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    UNIQUE(chat_id, party_key)
);
"""

# Real live incident (2026-09-04, Coffee: "we left the labyrinth and
# when i went to return it reset?"): labyrinth_runs (above) is
# deliberately ephemeral -- deleted the instant a party leaves -- so
# every segment's real seed was permanently lost the moment that
# happened, with no record anywhere of what it had been. This table is
# the opposite: append-only, NEVER deleted, one row per real segment
# ever generated (both a brand-new entry and each later descend), so a
# seed can always be found again later even long after the run itself
# is gone. party_names is a real, point-in-time SNAPSHOT (not a live
# lookup) -- party composition can change after the fact, and "whose
# game was this" should still answer correctly for an old row.
CREATE_LABYRINTH_SEED_LOG_TABLE = """
CREATE TABLE IF NOT EXISTS labyrinth_seed_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    chat_id INTEGER NOT NULL,
    party_key TEXT NOT NULL,
    segment INTEGER NOT NULL,
    seed INTEGER NOT NULL,
    theme TEXT,
    party_names TEXT NOT NULL DEFAULT '[]',
    created_at TEXT NOT NULL
);
"""

# Multi-tenant scaling Phase 3 (2026-08-02): a real registry of every
# Telegram group that has ever run /set_topic, so another group can add
# this bot and get its own working topic routing without touching
# config.py's own hardcoded home-group constants at all. chat_topic_config
# is the actual per-tenant routing table topics.py reads from; chats is
# just onboarding metadata (who set it up, when) -- not yet a foreign key
# target for anything, since Phase 4 (adding chat_id to every other table)
# hasn't happened yet.
CREATE_CHATS_TABLE = """
CREATE TABLE IF NOT EXISTS chats (
    chat_id INTEGER PRIMARY KEY,
    title TEXT,
    added_by_user_id INTEGER,
    created_at TEXT NOT NULL
);
"""

CREATE_CHAT_TOPIC_CONFIG_TABLE = """
CREATE TABLE IF NOT EXISTS chat_topic_config (
    chat_id INTEGER NOT NULL,
    topic_name TEXT NOT NULL,
    message_thread_id INTEGER,
    PRIMARY KEY (chat_id, topic_name)
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


def _backfill_cleared_location_gates(conn) -> None:
    """
    General "downstream proof implies upstream credit" backfill (2026-
    09-05, see init_db's own call site for the full real-live-report
    context). Loads the live campaign fresh each call -- cheap (one
    JSON file, ~290 locations) and keeps this correct if the map itself
    changes, rather than baking today's specific gate list in as a
    hardcoded constant that would silently go stale.

    For every real requires_cleared_location gate (A -> B), first
    checks whether B is a genuine SOLE-ENTRANCE chokepoint: is B still
    reachable from the starting location with every OTHER real gate
    treated as freely passable, but only this one specific edge cut?
    If B is still reachable some other way, this gate provides no real
    proof of anything and is skipped entirely (matches this campaign's
    own real branching in a few places, e.g. Greymoor Downs' hub itself
    has more than one way in). If B is NOT otherwise reachable, then
    everything physically reachable FROM B (ignoring gates entirely --
    a real player who got there could walk anywhere within it) is real,
    unambiguous proof that A was cleared.

    Never fabricates a fight that didn't happen: a character is only
    ever credited with A if they already have some location from that
    exact proof set in their own cleared_locations.
    """
    campaign = campaign_loader.load_campaign(config.ACTIVE_CAMPAIGN)
    locs: dict[str, dict] = {}
    for layer_locations in campaign.get("locations", {}).values():
        locs.update(layer_locations)

    def neighbors_of(loc: dict) -> set[str]:
        out = set(loc.get("connections", []))
        if loc.get("ascends_to"):
            out.add(loc["ascends_to"])
        if loc.get("descends_to"):
            out.add(loc["descends_to"])
        return out

    def reachable_from_start_cutting_one_edge(cut_from: str, cut_to: str) -> set[str]:
        start = campaign.get("starting_location")
        visited = {start}
        frontier = collections.deque([start])
        while frontier:
            cur = frontier.popleft()
            for nb in neighbors_of(locs.get(cur, {})):
                if cur == cut_from and nb == cut_to:
                    continue
                if nb not in visited:
                    visited.add(nb)
                    frontier.append(nb)
        return visited

    def reachable_from(node: str) -> set[str]:
        visited = {node}
        frontier = collections.deque([node])
        while frontier:
            cur = frontier.popleft()
            for nb in neighbors_of(locs.get(cur, {})):
                if nb not in visited:
                    visited.add(nb)
                    frontier.append(nb)
        return visited

    proof_sets: dict[str, set[str]] = {}
    for src, loc in locs.items():
        for target, gate in loc.get("story_gates", {}).items():
            required = gate.get("requires_cleared_location")
            if not required:
                continue
            reachable_without_this_gate = reachable_from_start_cutting_one_edge(src, target)
            if target in reachable_without_this_gate:
                continue  # a real alternate path exists -- no proof from this gate
            # reachable_from(target) walks in every direction, including
            # back out through real bidirectional connections (e.g. a
            # hub several hops downstream that also has its own
            # completely unrelated, always-open route back to the
            # start) -- anything ALSO reachable without ever crossing
            # this gate is not real proof of it and must be excluded,
            # or a single deep gate could wrongly "prove" nearly the
            # whole map.
            exclusive_downstream = reachable_from(target) - reachable_without_this_gate
            proof_sets.setdefault(required, set()).update(exclusive_downstream)

    if not proof_sets:
        return
    for row in conn.execute("SELECT character_id, cleared_locations FROM characters").fetchall():
        cleared = json.loads(row["cleared_locations"])
        cleared_set = set(cleared)
        to_credit = [required for required, proof in proof_sets.items() if required not in cleared_set and not cleared_set.isdisjoint(proof)]
        if not to_credit:
            continue
        cleared.extend(to_credit)
        conn.execute(
            "UPDATE characters SET cleared_locations = ? WHERE character_id = ?",
            (json.dumps(cleared), row["character_id"]),
        )


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
        conn.execute(CREATE_ITEM_INSTANCES_TABLE)
        conn.execute(CREATE_BOARD_QUESTS_TABLE)
        conn.execute(CREATE_PARTIES_TABLE)
        conn.execute(CREATE_GAME_SETTINGS_TABLE)
        conn.execute(CREATE_CHATS_TABLE)
        conn.execute(CREATE_CHAT_TOPIC_CONFIG_TABLE)
        conn.execute(CREATE_LABYRINTH_RUNS_TABLE)
        conn.execute(CREATE_LABYRINTH_SEED_LOG_TABLE)

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

        # Universal Manipulation migration (2026-08-06): draconic_hide
        # used to be the ONE upgrade here that mutated armor_class
        # directly and permanently at purchase time (every other
        # upgrade was read live). Now that every mechanic (including
        # draconic_hide) is read/applied via the shared _um_pool_step
        # convention, that old one-time +1 needs to be undone once for
        # any character who already bought it under the old system --
        # otherwise their first Universal Manipulation point would
        # double-count (the old permanent +1, AND the new live one).
        # Confirmed against the live DB (2026-08-06): exactly 1 real
        # character (Charvenna, armor_class 17) has draconic_hide today.
        # Gated on its own marker column so this is a genuine one-time
        # fix, not something that re-fires (and re-subtracts!) on every
        # future init_db() call once the correction has already landed.
        if "draconic_hide_ac_migrated" not in columns:
            conn.execute("ALTER TABLE characters ADD COLUMN draconic_hide_ac_migrated INTEGER NOT NULL DEFAULT 0")
            conn.execute(
                "UPDATE characters SET armor_class = armor_class - 1, draconic_hide_ac_migrated = 1 "
                "WHERE draconic_hide_ac_migrated = 0 AND skill_tree_upgrades LIKE '%draconic_hide%'"
            )

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
        # Companion Favors (2026-08-23, per Coffee: "affinity menu...
        # task based quests that are easy and attainable"): a small,
        # repeatable board_quest scoped to a recruited companion
        # (synthetic location_id "companion_favor:{npc_id}") instead of
        # a real location, rewarding real npc_relationships.affinity
        # instead of gold/XP -- see get_or_generate_companion_favor in
        # board_quests.py. Every existing board quest simply gets 0.
        if "reward_affinity" not in board_quest_columns:
            conn.execute("ALTER TABLE board_quests ADD COLUMN reward_affinity INTEGER NOT NULL DEFAULT 0")

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

        # Assassin's Backstab, permanent cross-rebirth ratchet (2026-08-08,
        # per Coffee, explicitly: "this will allow a player to 'break the
        # game' as per say"). An Assassin's live Backstab multiplier is
        # base_multiplier * a level-tier factor (x2/x4/x8/x10 across the
        # 1-100 level range) -- see bot.py's _effective_backstab_multiplier.
        # Unlike every other rebirth stat (which either stays untouched or
        # is purely additive, see rebirth_count above), THIS one intentionally
        # folds the fully-earned multiplier back into itself right before
        # level resets to 1 (_do_rebirth), so climbing the tiers again next
        # life multiplies an already-inflated base instead of starting over
        # -- real, deliberate, unbounded compounding across rebirths.
        if "backstab_base_multiplier" not in columns:
            conn.execute("ALTER TABLE characters ADD COLUMN backstab_base_multiplier INTEGER NOT NULL DEFAULT 1")

        # Grindable "mastery" proficiencies (2026-08-08, per Coffee,
        # verbatim: "raises the % by .01 (make it an absolute grind to
        # level up to 100% hit probability)... include in the item
        # generator... items to help with this"). Four separate percent
        # chances, each starting near-zero and climbing +0.01 per real
        # use, capped at 100.0 -- see bot.py's _grind_proficiency and
        # the four call sites that roll against these. weapon_/armor_
        # are per-CATEGORY (items.py's existing weapon_category:
        # simple/martial, armor_category: light/medium/heavy/shield --
        # "if players choose to use another weapon or armour type they
        # CAN and they can level them up to get better with them", so a
        # fresh category genuinely starts its own fresh grind), stored
        # as a JSON dict the same way skill_uses already is per-
        # profession. backstab_/throw_ are flat (Backstab is Assassin-
        # only to begin with; Throw is universal but a single ability,
        # not a category).
        if "weapon_proficiency_pct" not in columns:
            conn.execute("ALTER TABLE characters ADD COLUMN weapon_proficiency_pct TEXT NOT NULL DEFAULT '{}'")
        if "armor_proficiency_pct" not in columns:
            conn.execute("ALTER TABLE characters ADD COLUMN armor_proficiency_pct TEXT NOT NULL DEFAULT '{}'")
        if "backstab_proficiency_pct" not in columns:
            conn.execute("ALTER TABLE characters ADD COLUMN backstab_proficiency_pct REAL NOT NULL DEFAULT 1.0")
        if "throw_proficiency_pct" not in columns:
            conn.execute("ALTER TABLE characters ADD COLUMN throw_proficiency_pct REAL NOT NULL DEFAULT 1.0")
        # Steal proficiency (2026-08-11, per Coffee: enemy-stealing
        # should be "hard to steal but allow a proficiency to level up
        # for them") -- same flat grindable shape as backstab/throw,
        # shared by both shop-steal and the new enemy-steal (bot._do_
        # steal_from_enemy), converted to a small flat ability-check
        # bonus (round(pct/10), 0-10 range) rather than a % chance-gate,
        # since steal is resolved by the SAME "1d20 + bonus vs DC" shape
        # every other skill check in this game already uses.
        if "steal_proficiency_pct" not in columns:
            conn.execute("ALTER TABLE characters ADD COLUMN steal_proficiency_pct REAL NOT NULL DEFAULT 1.0")

        # Lockpick proficiency (2026-08-13, per Coffee, same shape as
        # steal_proficiency_pct just above -- Thieves' Guild's permanent
        # growth needed a real, grindable lockpicking field to point at,
        # not just the existing flat THIEVES_GUILD_LOCKPICK_BONUS.
        if "lockpick_proficiency_pct" not in columns:
            conn.execute("ALTER TABLE characters ADD COLUMN lockpick_proficiency_pct REAL NOT NULL DEFAULT 1.0")

        # Crafting/enchanting mastery (2026-08-11, per Coffee: "grinding
        # for better weapons and RNG allowing us to make better ones" --
        # same real per-profession 0-100% grind as weapon_proficiency_pct
        # above, just keyed by profession id (blacksmithing/alchemy)
        # instead of weapon/armor category. Read by bot._do_craft/
        # _do_enchant_item's new quality roll -- see rules/crafting.py's
        # roll_craft_quality.
        if "profession_mastery_pct" not in columns:
            conn.execute("ALTER TABLE characters ADD COLUMN profession_mastery_pct TEXT NOT NULL DEFAULT '{}'")

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

        # Multi-tenant scaling Phase 4a (2026-08-03, see the full plan at
        # /home/pandora/.claude/plans/sequential-tinkering-quokka.md):
        # chat_id added to every currently-global game table, so a
        # second Telegram group (once one actually exists) doesn't share
        # one namespace with this bot's home group. Deliberately zero
        # behavior change in this sub-phase -- nothing reads or filters
        # by chat_id yet (that's Phases 4b-4e); this only adds the
        # column and backfills every EXISTING row to the one real home
        # chat_id (config.TELEGRAM_CHAT_ID), since that's genuinely
        # which chat they already belong to -- inventing anything else
        # would be fabricating data. Uses this same NOT-IN-columns-yet
        # guard style as every other migration above, so re-running
        # init_db() on an already-migrated DB is a no-op.
        home_chat_id = config.TELEGRAM_CHAT_ID
        if "chat_id" not in columns:
            conn.execute("ALTER TABLE characters ADD COLUMN chat_id INTEGER")
            if home_chat_id is not None:
                conn.execute("UPDATE characters SET chat_id = ? WHERE chat_id IS NULL", (home_chat_id,))

        market_listing_columns = _existing_columns(conn, "market_listings")
        if "chat_id" not in market_listing_columns:
            conn.execute("ALTER TABLE market_listings ADD COLUMN chat_id INTEGER")
            if home_chat_id is not None:
                conn.execute("UPDATE market_listings SET chat_id = ? WHERE chat_id IS NULL", (home_chat_id,))

        if "chat_id" not in board_quest_columns:
            conn.execute("ALTER TABLE board_quests ADD COLUMN chat_id INTEGER")
            if home_chat_id is not None:
                conn.execute("UPDATE board_quests SET chat_id = ? WHERE chat_id IS NULL", (home_chat_id,))

        # Real live bug found in Phase 4c/4d (2026-08-06): create_board_quest
        # never wrote the new chat_id column at all -- so every board quest
        # generated AFTER the one-time migration above (which only backfills
        # rows that existed at that exact moment) kept getting created with
        # chat_id NULL, including 2 currently-accepted quests belonging to a
        # real, active player. create_board_quest itself is now fixed to
        # always write chat_id; this is the matching one-time cleanup for
        # whatever NULL rows the bug already left behind, backfilled to the
        # same home_chat_id every other Phase 4a/4b backfill above uses.
        # Deliberately UNCONDITIONAL (not gated on a schema check like the
        # migration above) since it's cleaning up bad DATA, not adding a
        # column -- but still a no-op once every row is clean, so re-running
        # init_db() stays safe.
        if home_chat_id is not None:
            conn.execute("UPDATE board_quests SET chat_id = ? WHERE chat_id IS NULL", (home_chat_id,))

        parties_columns = _existing_columns(conn, "parties")
        if "chat_id" not in parties_columns:
            conn.execute("ALTER TABLE parties ADD COLUMN chat_id INTEGER")
            if home_chat_id is not None:
                conn.execute("UPDATE parties SET chat_id = ? WHERE chat_id IS NULL", (home_chat_id,))

        # Real live bug found in this same Phase 4c/4d/4e retrofit pass
        # (2026-08-06), same shape as the board_quests one above:
        # create_party already took chat_id as a parameter but never
        # wrote it into the INSERT, so every party created after the
        # one-time migration above kept getting chat_id NULL -- confirmed
        # on the live DB (one real party row). create_party is now fixed
        # to always write it; this is the matching one-time cleanup,
        # unconditional and idempotent for the same reason as the
        # board_quests backfill above.
        if home_chat_id is not None:
            conn.execute("UPDATE parties SET chat_id = ? WHERE chat_id IS NULL", (home_chat_id,))

        # active_characters/npc_relationships/faction_standing all need
        # their real PRIMARY KEY to grow a chat_id column -- SQLite
        # cannot ALTER TABLE to change a PK, so these three need a real
        # table rebuild (create the new shape, copy every row across
        # with the backfilled chat_id, drop the old table, rename the
        # new one into place) instead of a plain ADD COLUMN. Safe: this
        # schema uses zero SQL-level FOREIGN KEYs anywhere (confirmed),
        # so no other table can reference the old one mid-rebuild, and
        # this whole function already runs inside one connection/
        # transaction (get_connection commits once at the very end), so
        # a failure partway through rolls back cleanly instead of
        # leaving a half-migrated table behind.
        active_char_columns = _existing_columns(conn, "active_characters")
        if "chat_id" not in active_char_columns:
            conn.execute("""
                CREATE TABLE active_characters_new (
                    telegram_user_id INTEGER NOT NULL,
                    chat_id INTEGER NOT NULL,
                    character_id INTEGER NOT NULL,
                    PRIMARY KEY (telegram_user_id, chat_id)
                )
            """)
            conn.execute(
                "INSERT INTO active_characters_new (telegram_user_id, chat_id, character_id) "
                "SELECT telegram_user_id, ?, character_id FROM active_characters",
                (home_chat_id,),
            )
            conn.execute("DROP TABLE active_characters")
            conn.execute("ALTER TABLE active_characters_new RENAME TO active_characters")

        npc_relationship_rebuild_columns = _existing_columns(conn, "npc_relationships")
        if "chat_id" not in npc_relationship_rebuild_columns:
            conn.execute("""
                CREATE TABLE npc_relationships_new (
                    telegram_user_id INTEGER NOT NULL,
                    chat_id INTEGER NOT NULL,
                    npc_id TEXT NOT NULL,
                    affinity INTEGER NOT NULL DEFAULT 0,
                    memory_events TEXT NOT NULL DEFAULT '[]',
                    banned INTEGER NOT NULL DEFAULT 0,
                    resolution TEXT NOT NULL DEFAULT 'unresolved',
                    PRIMARY KEY (telegram_user_id, chat_id, npc_id)
                )
            """)
            conn.execute(
                "INSERT INTO npc_relationships_new "
                "(telegram_user_id, chat_id, npc_id, affinity, memory_events, banned, resolution) "
                "SELECT telegram_user_id, ?, npc_id, affinity, memory_events, banned, resolution "
                "FROM npc_relationships",
                (home_chat_id,),
            )
            conn.execute("DROP TABLE npc_relationships")
            conn.execute("ALTER TABLE npc_relationships_new RENAME TO npc_relationships")

        faction_standing_columns = _existing_columns(conn, "faction_standing")
        if "chat_id" not in faction_standing_columns:
            conn.execute("""
                CREATE TABLE faction_standing_new (
                    telegram_user_id INTEGER NOT NULL,
                    chat_id INTEGER NOT NULL,
                    faction_id TEXT NOT NULL,
                    standing INTEGER NOT NULL DEFAULT 0,
                    PRIMARY KEY (telegram_user_id, chat_id, faction_id)
                )
            """)
            conn.execute(
                "INSERT INTO faction_standing_new (telegram_user_id, chat_id, faction_id, standing) "
                "SELECT telegram_user_id, ?, faction_id, standing FROM faction_standing",
                (home_chat_id,),
            )
            conn.execute("DROP TABLE faction_standing")
            conn.execute("ALTER TABLE faction_standing_new RENAME TO faction_standing")

        # Reforge-source gate (2026-08-12, per Coffee via dev-topic
        # screenshot: "Don't have reforging on the list unless it is a
        # forged weapon we found this weapon battle, so we shouldn't have
        # the option to reforge it") -- item_instances previously had no
        # way to tell a player-crafted magic item apart from one dropped
        # as combat loot, so _forge_item_core (both the free-text "forge
        # my X" command and the item-view "Reforge" button) let ANY
        # generated weapon/armor/shield be forged to a higher tier
        # regardless of how it was obtained. Existing rows default to
        # 'loot' (the safer assumption: pre-migration rows can't be
        # proven to have been crafted, and treating them as loot only
        # blocks a forge that was never guaranteed to begin with, vs.
        # defaulting to 'crafted' and letting every old loot drop keep
        # forging).
        item_instance_columns = _existing_columns(conn, "item_instances")
        if "source" not in item_instance_columns:
            conn.execute("ALTER TABLE item_instances ADD COLUMN source TEXT NOT NULL DEFAULT 'loot'")

        # Guild curriculum (2026-08-12, per Coffee: "go ahead and start on
        # it" -- a real, hand-authored, gated training-quest chain per
        # guild, see guild_curriculum.py). `guild_curriculum_step` is a
        # single 0-indexed progress counter (guild membership is
        # permanent and 1-per-character, so no guild_id keying is
        # needed); `guild_curriculum_step_unlocked_at` stamps when the
        # CURRENT step became available, enforcing a real time-gate
        # before it can be credited (GUILD_CURRICULUM_STEP_COOLDOWN_
        # HOURS) so it can't be rushed through in one sitting;
        # `guild_curriculum_state` is a JSON scratch dict reserved for a
        # step type that needs to remember something mid-resolution (an
        # alignment_choice step's setup narration, so it's shown
        # identically if re-requested before being resolved).
        columns = _existing_columns(conn, "characters")
        if "guild_curriculum_step" not in columns:
            conn.execute("ALTER TABLE characters ADD COLUMN guild_curriculum_step INTEGER NOT NULL DEFAULT 0")
        if "guild_curriculum_step_unlocked_at" not in columns:
            conn.execute("ALTER TABLE characters ADD COLUMN guild_curriculum_step_unlocked_at TEXT")
        if "guild_curriculum_state" not in columns:
            conn.execute("ALTER TABLE characters ADD COLUMN guild_curriculum_state TEXT NOT NULL DEFAULT '{}'")

        # Leave-guild + evolution-gated multi-guild ("doubled up") system
        # (2026-08-13, per Coffee -- see guilds.py's GUILD_STAT_BONUS_
        # LEVELS/eligible_for_guild docstrings for the full design).
        # `guild_join_level` is the PRIMARY guild's level-at-join, needed
        # to compute how many GUILD_STAT_BONUS_LEVELS thresholds have
        # been crossed while a member (see add_xp) -- nullable, since a
        # character with no guild has none. The four `secondary_guild_*`
        # dicts are guild_id-keyed mirrors of the single-guild fields
        # above (join level / curriculum step / that step's unlock
        # timestamp / that step's scratch state) for every guild beyond
        # the first an evolved character has earned -- a dict rather
        # than a second scalar set, since more than one secondary guild
        # is the entire point of "doubled up."
        if "guild_join_level" not in columns:
            conn.execute("ALTER TABLE characters ADD COLUMN guild_join_level INTEGER")
        if "secondary_guilds" not in columns:
            conn.execute("ALTER TABLE characters ADD COLUMN secondary_guilds TEXT NOT NULL DEFAULT '[]'")
        if "secondary_guild_join_levels" not in columns:
            conn.execute("ALTER TABLE characters ADD COLUMN secondary_guild_join_levels TEXT NOT NULL DEFAULT '{}'")
        if "secondary_guild_curriculum_steps" not in columns:
            conn.execute("ALTER TABLE characters ADD COLUMN secondary_guild_curriculum_steps TEXT NOT NULL DEFAULT '{}'")
        if "secondary_guild_curriculum_unlocked_at" not in columns:
            conn.execute("ALTER TABLE characters ADD COLUMN secondary_guild_curriculum_unlocked_at TEXT NOT NULL DEFAULT '{}'")
        if "secondary_guild_curriculum_state" not in columns:
            conn.execute("ALTER TABLE characters ADD COLUMN secondary_guild_curriculum_state TEXT NOT NULL DEFAULT '{}'")

        # The Remnants (2026-08-13, per Coffee -- see remnants.py's own
        # docstring for the full design). `bound_remnants` is a JSON
        # list of remnant_ids this character has personally helped
        # defeat (every real party member present when an Unbound falls
        # gets it, same "shared credit" shape as known_monsters) --
        # whoever has one bound may cast it themselves, no other role
        # needed. `is_designated_summoner` is a dead column (the real,
        # single-holder-at-a-time Summoner role it backed was removed
        # 2026-08-20 per Coffee -- see remnants.py's docstring -- kept
        # here unread/unwritten rather than risk a live schema DROP
        # COLUMN on production data). `summoning_mastery_pct` is the
        # same real 0-100 grindable proficiency shape as steal/
        # lockpick/profession mastery elsewhere in this game, grown
        # only on a successful summon cast.
        if "bound_remnants" not in columns:
            conn.execute("ALTER TABLE characters ADD COLUMN bound_remnants TEXT NOT NULL DEFAULT '[]'")
        if "is_designated_summoner" not in columns:
            conn.execute("ALTER TABLE characters ADD COLUMN is_designated_summoner INTEGER NOT NULL DEFAULT 0")
        if "summoning_mastery_pct" not in columns:
            conn.execute("ALTER TABLE characters ADD COLUMN summoning_mastery_pct REAL NOT NULL DEFAULT 1.0")
        # Real live report (2026-08-15, per Coffee, following the
        # Encounter Ledger level-curve pass: "make the bosses the same
        # lvl as the ledger but dont show it to the player until they
        # beat the boss, then include it in bestiary"). known_monsters
        # (mark_known_monster) is set the moment a fight STARTS -- a
        # boss fled from or lost is already "known" under that fog-of-
        # war, which isn't good enough to gate a real spoiler (the
        # boss's own level) behind. This is a genuinely separate fact
        # ("have I actually WON against this monster type") from
        # "have I fought it at all".
        if "defeated_monsters" not in columns:
            conn.execute("ALTER TABLE characters ADD COLUMN defeated_monsters TEXT NOT NULL DEFAULT '[]'")
        # Quest Menu (2026-08-20, per Coffee's dev-bridge screenshots +
        # live conversation): "Not now" on a proactively-pushed quest
        # offer (see bot._maybe_push_quest_offer) is a soft pacing gate,
        # never a permanent skip -- the quest stays fully visible/
        # acceptable everywhere else, this just suppresses that ONE
        # quest_id's future proactive push until it's actually accepted
        # (cleared then, same "state resets once it advances" shape
        # guild_curriculum_state's own waiting_reminder_shown flag
        # already uses).
        if "dismissed_quest_ids" not in columns:
            conn.execute("ALTER TABLE characters ADD COLUMN dismissed_quest_ids TEXT NOT NULL DEFAULT '[]'")
        # Spell Mastery (2026-08-22, per Coffee: "let the players level
        # up thier magic... tiers of magic that can get stronger and
        # target multiple enemies"). Same grindable-proficiency-dict
        # shape as weapon_proficiency_pct/armor_proficiency_pct, at two
        # granularities: spell_mastery_pct (keyed by spell_id -- casting
        # Fireball repeatedly grows Fireball specifically) and
        # element_mastery_pct (keyed by damage_type -- any fire spell
        # cast also grows a smaller, shared bonus across every fire
        # spell known), per the confirmed dual-tier design.
        if "spell_mastery_pct" not in columns:
            conn.execute("ALTER TABLE characters ADD COLUMN spell_mastery_pct TEXT NOT NULL DEFAULT '{}'")
        if "element_mastery_pct" not in columns:
            conn.execute("ALTER TABLE characters ADD COLUMN element_mastery_pct TEXT NOT NULL DEFAULT '{}'")
        # Real live request (2026-08-27, Coffee: "create a function so i
        # can sort my inventory... Battle type, Support type, Magic
        # type, Normal") -- persistent per-character backpack view
        # preference, toggled from the backpack screen's own Sort
        # button. 'off' (plain insertion order) or 'type' (grouped into
        # the 4 real buckets -- see bot.py's _INVENTORY_SORT_CATEGORY_*).
        if "inventory_sort_mode" not in columns:
            conn.execute("ALTER TABLE characters ADD COLUMN inventory_sort_mode TEXT NOT NULL DEFAULT 'off'")

        # labyrinth_best_floor (2026-09-01, per Coffee: "start the
        # labyrinth architecture") -- permanent, per-character bragging
        # rights, same shape as echo_trial_tier above. Never decreases;
        # bumped by db.bump_labyrinth_best_floor whenever a live run's
        # floor exceeds whatever this character already has on record.
        if "labyrinth_best_floor" not in columns:
            conn.execute("ALTER TABLE characters ADD COLUMN labyrinth_best_floor INTEGER NOT NULL DEFAULT 0")

        # labyrinth_checkpoint_floor (2026-09-02, Phase L3, per Coffee:
        # "make this a safe spot with a waypoint to fast travel to
        # later on") -- the highest SEGMENT-END checkpoint floor this
        # character has actually reached and claimed the rewards of.
        # Permanent, never decreases. Unlike labyrinth_best_floor (pure
        # bragging rights), this one is load-bearing: _do_enter_
        # labyrinth uses it to decide which segment a fresh run starts
        # in, so leaving and re-entering resumes past what's already
        # been cleared instead of restarting at floor 1 every time.
        if "labyrinth_checkpoint_floor" not in columns:
            conn.execute("ALTER TABLE characters ADD COLUMN labyrinth_checkpoint_floor INTEGER NOT NULL DEFAULT 0")

        # labyrinth_intro_seen (2026-09-02, per Coffee: "if its the first
        # time, can u give them a brief rundown on what the labarynth is
        # and how to play it") -- permanent, per-character, so each real
        # player gets the tutorial exactly once on their own first entry,
        # regardless of what the rest of their party has already seen.
        if "labyrinth_intro_seen" not in columns:
            conn.execute("ALTER TABLE characters ADD COLUMN labyrinth_intro_seen INTEGER NOT NULL DEFAULT 0")

        # fighting_style / equipped_offhand_weapon (2026-09-04, real
        # Fighting Style + mastery-gated dual wielding feature) -- see
        # bot.FIGHTING_STYLES/_do_choose_fighting_style and
        # can_dual_wield/equip_offhand_weapon/bot._do_equip_offhand.
        if "fighting_style" not in columns:
            conn.execute("ALTER TABLE characters ADD COLUMN fighting_style TEXT")
        if "equipped_offhand_weapon" not in columns:
            conn.execute("ALTER TABLE characters ADD COLUMN equipped_offhand_weapon TEXT")

        # location_defeat_counts (2026-09-06, real "Advanced Dungeons"
        # repeated gate, ported to evolved overworld dungeons -- per
        # Coffee: "keep going"). Per-character, {location_id: count},
        # separate from `cleared_locations` -- an evolved-dungeon room's
        # own `monsters` list is shared, campaign-wide state that's
        # NEVER cleared on victory (unlike the Labyrinth's own per-run
        # room dict), so a real "fight the same encounter N times"
        # requires no monster-respawn logic at all here: the room's
        # monsters are already always still there to fight again. This
        # column just counts how many times THIS character has won,
        # gating exactly when `mark_location_cleared` (bot.py's
        # _mark_location_cleared_for_party) actually fires for a room
        # flagged `repeat_required` (rules/dungeon_evolve.py).
        if "location_defeat_counts" not in columns:
            conn.execute("ALTER TABLE characters ADD COLUMN location_defeat_counts TEXT NOT NULL DEFAULT '{}'")

        # labyrinth_runs.modifier (2026-09-02, Phase L2 floor modifiers)
        # -- a real DB already running Phase L1's CREATE TABLE IF NOT
        # EXISTS won't pick up a column added to that CREATE statement
        # alone, same reasoning as every ALTER TABLE above. Superseded
        # by Phase L3's per-ROOM "modifier" field (a live segment holds
        # 5 real floors, each rolling its own) -- column kept, just
        # unused going forward, not worth a destructive drop.
        labyrinth_runs_columns = _existing_columns(conn, "labyrinth_runs")
        if "modifier" not in labyrinth_runs_columns:
            conn.execute("ALTER TABLE labyrinth_runs ADD COLUMN modifier TEXT")

        # labyrinth_runs.visited_floors_json (2026-09-02, Phase L3) --
        # which of the CURRENT live segment's real floors this party has
        # actually physically stood on, not just generated. Drives the
        # real map floor-switcher (only ever show a floor button for one
        # a party has "attained," never a spoiler for what's deeper in
        # the same segment) and the achievements screen's own progress
        # line.
        if "visited_floors_json" not in labyrinth_runs_columns:
            conn.execute("ALTER TABLE labyrinth_runs ADD COLUMN visited_floors_json TEXT NOT NULL DEFAULT '[]'")

        # labyrinth_runs.steps_until_encounter (2026-09-02, Phase L3,
        # per Coffee: "i like the idea of enemies spawning and
        # attacking after so many RNG footsteps in the labarynth...
        # like an ambush... players shudnt know when they are being
        # encountered") -- a real, hidden, server-side-only countdown,
        # never surfaced in any player-facing text. 0 means "not yet
        # rolled" (a fresh run/segment picks a real threshold on first
        # move, see bot._do_labyrinth_move).
        if "steps_until_encounter" not in labyrinth_runs_columns:
            conn.execute("ALTER TABLE labyrinth_runs ADD COLUMN steps_until_encounter INTEGER NOT NULL DEFAULT 0")

        # labyrinth_runs.carrying (2026-09-03, Phase L4, item 0.5, per
        # Coffee's own live screenshot re-ask: "multiple levels floors
        # and basements to get to the other end of the dungeon" -- the
        # real carry-and-strike dungeon mechanic, confirmed via research: carry a
        # heavy object between rooms, strike real pillars scattered
        # across the floor with it, collapse a section once every
        # pillar's struck). NULL when nothing is being carried; the id
        # of a real `carry_object` lockable while it is -- party-shared,
        # same as every other Labyrinth run field, since only one party
        # can ever be hauling the one object at a time.
        if "carrying" not in labyrinth_runs_columns:
            conn.execute("ALTER TABLE labyrinth_runs ADD COLUMN carrying TEXT")

        # labyrinth_runs.shards (2026-09-06, real Labyrinth Shard
        # collectible-gate, per Coffee: "collect them to gain access to
        # the next labyrinth... have the mini boss, and boss and
        # scatter them around, have it collected by battle and by
        # chests"). Deliberately lives HERE, not as a real inventory
        # item on the character -- per his own explicit "per-run,
        # resets on leave" choice, the exact same lifetime every other
        # column on this table already has (deleted the instant a party
        # leaves, same as `rooms_json`/`current_room_id`). See
        # rules/labyrinth.required_shards_for_segment for the real,
        # depth-scaled cost _do_descend_labyrinth consumes this against.
        if "shards" not in labyrinth_runs_columns:
            conn.execute("ALTER TABLE labyrinth_runs ADD COLUMN shards INTEGER NOT NULL DEFAULT 0")

        # Real live gap (2026-09-05, Coffee, dev-bridge: "i had already
        # cleared that room to get to the first city right? ...next is
        # glimmer deep then the hush then the city" -- and he was right;
        # follow-up: "make sure all characters that have previous
        # cleared areas can travel"). Sunken Root Caverns' own
        # requires_cleared_location gate is genuinely OLD content (not
        # something a recent pass introduced), but its downstream proof
        # was never credited for characters who'd already gotten past
        # it some other way (admin/testing shortcuts, older builds,
        # etc.) -- and a direct scan of the whole campaign graph showed
        # this exact shape (a "clear this room to advance" gate whose
        # true DOWNSTREAM presence was never retroactively credited)
        # repeats at 39 other real chokepoints across the whole map, not
        # just this one. Generalized: for every real requires_cleared_
        # location gate in the campaign, `_backfill_cleared_location_
        # gates` below figures out (once, at import time, from the real
        # graph -- not hardcoded) which ones are genuine sole-entrance
        # chokepoints (no other real path around them) and what the
        # full reachable set beyond each one is, then credits any
        # character who already has real proof of being past a
        # chokepoint but is missing that specific gate's own location.
        # One-time, idempotent (naturally a no-op once applied, same as
        # every other migration in this function): never fabricates a
        # fight that didn't happen, only credits characters who already
        # have real, downstream proof.
        _backfill_cleared_location_gates(conn)


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
    d["defeated_monsters"] = json.loads(d["defeated_monsters"])
    d["cleared_locations"] = json.loads(d["cleared_locations"])
    d["achievements"] = json.loads(d["achievements"])
    d["map_revealed_locations"] = json.loads(d["map_revealed_locations"])
    d["skill_tree_upgrades"] = json.loads(d["skill_tree_upgrades"])
    d["weapon_proficiency_pct"] = json.loads(d["weapon_proficiency_pct"])
    d["armor_proficiency_pct"] = json.loads(d["armor_proficiency_pct"])
    d["profession_mastery_pct"] = json.loads(d["profession_mastery_pct"])
    d["guild_curriculum_state"] = json.loads(d["guild_curriculum_state"])
    d["secondary_guilds"] = json.loads(d["secondary_guilds"])
    d["secondary_guild_join_levels"] = json.loads(d["secondary_guild_join_levels"])
    d["secondary_guild_curriculum_steps"] = json.loads(d["secondary_guild_curriculum_steps"])
    d["secondary_guild_curriculum_unlocked_at"] = json.loads(d["secondary_guild_curriculum_unlocked_at"])
    d["secondary_guild_curriculum_state"] = json.loads(d["secondary_guild_curriculum_state"])
    d["bound_remnants"] = json.loads(d["bound_remnants"])
    d["dismissed_quest_ids"] = json.loads(d["dismissed_quest_ids"])
    d["spell_mastery_pct"] = json.loads(d["spell_mastery_pct"])
    d["element_mastery_pct"] = json.loads(d["element_mastery_pct"])
    d["location_defeat_counts"] = json.loads(d["location_defeat_counts"])
    return d


def _active_character_id(telegram_user_id: int, chat_id: int, conn=None) -> int | None:
    def _lookup(c):
        row = c.execute(
            "SELECT character_id FROM active_characters WHERE telegram_user_id = ? AND chat_id = ?",
            (telegram_user_id, chat_id),
        ).fetchone()
        return row["character_id"] if row else None

    if conn is not None:
        return _lookup(conn)
    with get_connection() as c:
        return _lookup(c)


def get_active_character_id(telegram_user_id: int, chat_id: int) -> int | None:
    """
    Public wrapper around _active_character_id -- which of this human's
    characters they're CURRENTLY piloting in this chat (2026-08-13, per
    Coffee: "human players can only use one character at a time,
    therefore only taking up one party slot"). get_party_members_by_id
    deliberately does NOT filter by this anymore (see that function's
    own 2026-08-06 docstring -- a dormant alt character, e.g. Laurienna
    while her owner is playing Charvenna, must still show up in party
    listings for revival/XP-sharing/death handling), which is correct
    there but means anything that counts REAL SIMULTANEOUS COMBATANTS
    (bot.py's _get_real_party_combatants, the active-roster cap check in
    _do_unbench_member) needs its own explicit way to tell "this is the
    one character of theirs actually in play right now" from "this is
    an owned-but-dormant alt sitting in the same party" -- a human can't
    dual-pilot two characters into the same fight.
    """
    return _active_character_id(telegram_user_id, chat_id)


def _set_active_character(telegram_user_id: int, chat_id: int, character_id: int, conn) -> None:
    # active_characters' real PK became (telegram_user_id, chat_id) in the
    # Phase 4a schema migration (2026-08-03); Phase 4b (2026-08-04) threads
    # the real chat_id through as a genuine parameter.
    conn.execute(
        """
        INSERT INTO active_characters (telegram_user_id, chat_id, character_id) VALUES (?, ?, ?)
        ON CONFLICT(telegram_user_id, chat_id) DO UPDATE SET character_id = excluded.character_id
        """,
        (telegram_user_id, chat_id, character_id),
    )


def create_character(telegram_user_id: int, chat_id: int, name: str, race: str, char_class: str,
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
                telegram_user_id, chat_id, name, race, char_class, level, xp,
                hp_current, hp_max, strength, dexterity, constitution,
                intelligence, wisdom, charisma, proficiency_bonus,
                armor_class, inventory, gold, is_ai, current_location, known_spells,
                spell_slots_max, spell_slots_current, visited_locations
            ) VALUES (?, ?, ?, ?, ?, 1, 0, ?, ?, ?, ?, ?, ?, ?, ?, 2, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                telegram_user_id, chat_id, name, race, char_class,
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
        _set_active_character(telegram_user_id, chat_id, character_id, conn)
    return get_character(telegram_user_id, chat_id)


_NEXT_AI_ID: int | None = None  # lazily seeded from the DB — see create_ai_companion


def create_ai_companion(chat_id: int, name: str, race: str, char_class: str, ability_scores: dict,
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
        telegram_user_id=ai_id, chat_id=chat_id, name=name, race=race, char_class=char_class,
        ability_scores=ability_scores, hp_max=hp_max, armor_class=armor_class,
        gold=gold, inventory=inventory, is_ai=True,
    )


def update_character(telegram_user_id: int, chat_id: int, **fields) -> dict | None:
    if not fields:
        return get_character(telegram_user_id, chat_id)

    json_fields = ("inventory", "known_spells", "completed_quests", "visited_locations", "skill_uses", "active_quests", "feature_uses", "equipped_accessories", "known_monsters", "defeated_monsters", "cleared_locations", "achievements", "map_revealed_locations", "skill_tree_upgrades", "weapon_proficiency_pct", "armor_proficiency_pct", "profession_mastery_pct", "guild_curriculum_state", "secondary_guilds", "secondary_guild_join_levels", "secondary_guild_curriculum_steps", "secondary_guild_curriculum_unlocked_at", "secondary_guild_curriculum_state", "bound_remnants", "dismissed_quest_ids", "spell_mastery_pct", "element_mastery_pct", "location_defeat_counts")
    for key in json_fields:
        if key in fields and not isinstance(fields[key], str):
            fields[key] = json.dumps(fields[key])

    columns = ", ".join(f"{k} = ?" for k in fields)
    values = list(fields.values())

    with get_connection() as conn:
        character_id = _active_character_id(telegram_user_id, chat_id, conn)
        if character_id is None:
            return None
        conn.execute(
            f"UPDATE characters SET {columns} WHERE character_id = ?",
            values + [character_id],
        )
    return get_character(telegram_user_id, chat_id)


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

    json_fields = ("inventory", "known_spells", "completed_quests", "visited_locations", "skill_uses", "active_quests", "feature_uses", "equipped_accessories", "known_monsters", "defeated_monsters", "cleared_locations", "achievements", "map_revealed_locations", "skill_tree_upgrades", "weapon_proficiency_pct", "armor_proficiency_pct", "profession_mastery_pct", "guild_curriculum_state", "secondary_guilds", "secondary_guild_join_levels", "secondary_guild_curriculum_steps", "secondary_guild_curriculum_unlocked_at", "secondary_guild_curriculum_state", "bound_remnants", "dismissed_quest_ids", "spell_mastery_pct", "element_mastery_pct", "location_defeat_counts")
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


def add_item(telegram_user_id: int, chat_id: int, item_id: str, quantity: int = 1) -> dict | None:
    """Add `quantity` of an item to a character's backpack (dict of item_id -> count)."""
    character = get_character(telegram_user_id, chat_id)
    if character is None:
        return None
    character["inventory"][item_id] = character["inventory"].get(item_id, 0) + quantity
    return update_character(telegram_user_id, chat_id, inventory=character["inventory"])


def remove_item(telegram_user_id: int, chat_id: int, item_id: str, quantity: int = 1) -> tuple[bool, dict | None]:
    """
    Remove `quantity` of an item from a character's backpack. Returns
    (success, updated_character). Fails cleanly (no partial removal) if
    the character doesn't have enough of the item.
    """
    character = get_character(telegram_user_id, chat_id)
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
    updated = update_character(telegram_user_id, chat_id, inventory=character["inventory"])
    return True, updated


# Synthetic item_id prefix for a real per-instance item (2026-08-02 magic
# item system) -- e.g. "gi42" for instance_id 42. Short and safely under
# Telegram's real callback_data length limit (see bot.py's "equip|item|
# {item_id}" pattern) -- deliberately never a UUID or anything embedding
# the item's own data.
GENERATED_ITEM_ID_PREFIX = "gi"


def create_item_instance(
    item_type: str, name: str, rarity: str, price: int,
    base_stats: dict, affixes: list | None = None, set_id: str | None = None,
    source: str = "loot",
) -> str:
    """
    Persists one real, unique rolled/crafted item and returns its
    synthetic item_id (e.g. "gi42") -- the string every other system
    (inventory, equip, market, scrolls-of-a-sort) treats exactly like any
    static items.py catalog key, via items.get_item()'s fallback into
    materialize_item_instance below.

    `source` ("loot" or "crafted", 2026-08-12) records how the item was
    obtained -- combat/treasure drops default to "loot"; bot.py's advanced
    crafting path passes "crafted" explicitly. Read by _forge_item_core's
    reforge gate (per Coffee: only a weapon the player actually forged
    should be reforgeable, not one found in battle).
    """
    with get_connection() as conn:
        cur = conn.execute(
            "INSERT INTO item_instances (item_type, name, rarity, price, base_stats, affixes, set_id, created_at, source) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                item_type, name, rarity, price, json.dumps(base_stats), json.dumps(affixes or []),
                set_id, datetime.now(timezone.utc).isoformat(), source,
            ),
        )
        instance_id = cur.lastrowid
    return f"{GENERATED_ITEM_ID_PREFIX}{instance_id}"


def _apply_affix(item: dict, affix: dict) -> None:
    """
    Folds one affix's effect onto an already-materialized item dict, in
    place. One branch per affix `kind` -- the shared vocabulary every
    later phase (elemental damage/resistance, granted spells, profession
    bonuses, set bonuses) adds its own branch to, and enchanting/forging/
    imbuing (2026-08-02 plan, Phase 7) all reuse unchanged, since they're
    just "append one more affix dict and re-materialize."
    """
    kind = affix.get("kind")
    if kind == "stat_bonus":
        field = affix["field"]
        item[field] = item.get(field, 0) + affix["value"]
    # Phase 2 (2026-08-02): elemental affixes. A weapon carries at most
    # one damage_type (matches every static weapon in items.py -- e.g.
    # flametongue_shortsword's single "fire"), so this just sets the
    # field rather than accumulating a list. Armor/shield/accessories
    # append to real list fields -- the SAME fields
    # rules.combat.apply_damage_type_modifier already reads off any
    # defender dict (resistances/vulnerabilities/immunities), reused here
    # completely unchanged; only equip-time now needs to actually
    # populate them (see bot._equipped_elemental_profile).
    elif kind == "elemental_damage":
        item["damage_type"] = affix["damage_type"]
    # Numeric, STACKING damage bonus (2026-08-11, per Coffee: "use the %
    # to increase damage of physical damage, damage types, and elemental
    # type" -- same real numeric-sibling-to-a-boolean-affix relationship
    # elemental_resistance already has to resistance above, just for
    # OFFENSE instead of defense. Deliberately damage-type-agnostic (a
    # plain physical weapon can carry this too, via enchant_sharpen) --
    # read by bot._weapon_for_attacker, which adds
    # round(average_damage(dice) * pct / 100) onto damage_bonus.
    elif kind == "elemental_damage_bonus":
        item["elemental_damage_bonus_pct"] = item.get("elemental_damage_bonus_pct", 0) + affix["value"]
    elif kind in ("resistance", "vulnerability", "immunity"):
        list_field = f"{kind}s"
        item.setdefault(list_field, []).append(affix["damage_type"])
    # Numeric, STACKING resistance (2026-08-10, per Coffee: "enchant
    # armour and other equipables to raise resistences in Frost/Flame/
    # Spark... if the players gain enough resistences... nullify the
    # damage OR ... heal"). Deliberately separate from the flat boolean
    # "resistance" kind above (which stays a plain 50%, no stacking) --
    # appended to a list, same live-summed-at-read-time convention as
    # profession_bonuses just below, read by bot.py's
    # _apply_equipped_elemental_profile and consumed by rules.combat's
    # apply_damage_type_modifier/elemental_overflow_heal.
    elif kind == "elemental_resistance":
        item.setdefault("elemental_resistances", []).append(
            {"damage_type": affix["damage_type"], "value": affix["value"]}
        )
    # Phase 3 (2026-08-02): an item that grants a spell the wearer
    # doesn't otherwise know -- bot._do_cast_spell's own third fallback
    # branch (after known_spells, after a carried scroll) reads these two
    # fields directly off the materialized item, gated by feature_uses
    # (f"item_spell_{instance_id}"), never a real spell slot -- a
    # permanently-worn item isn't a one-shot scroll, but still needs some
    # limiter.
    elif kind == "grants_spell":
        item["grants_spell"] = affix["spell_id"]
        item["grants_spell_uses"] = affix["uses"]
    # Phase 4 (2026-08-02): a numeric bonus to a real gathering/crafting
    # roll -- appended to a list since an item could theoretically carry
    # more than one (a future set bonus, say), read by
    # bot._equipped_profession_bonus (mirrors _equipped_regen_bonus's
    # live-summed-at-read-time pattern) at the exact same call sites
    # _practiced_bonus_for/class_profession_affinity_bonus already feed
    # into _do_gather/_do_craft's shared "bonus" accumulator.
    elif kind == "profession_bonus":
        item.setdefault("profession_bonuses", []).append(
            {"profession": affix["profession"], "value": affix["value"]}
        )
    # Phase 6 (2026-08-02): mythic-tier-exclusive combat affixes. Real
    # mechanical effects, not bigger numbers -- ignore_resistance is
    # checked by rules.combat.apply_damage_type_modifier, free_extra_attack
    # by bot._attacks_per_turn, both read off the equipped weapon via
    # bot._apply_equipped_elemental_profile the same live-summed way
    # every other combat-affecting affix already works. damage_immunity
    # is deliberately NOT its own kind -- it reuses the "immunity" kind
    # Phase 2 already built, just rolled preferentially at mythic tier.
    elif kind == "ignore_resistance":
        item["ignores_resistance"] = True
    elif kind == "free_extra_attack":
        item["free_extra_attack"] = True
    # Grindable mastery proficiency gear (2026-08-08, per Coffee: "u can
    # include in the item generator for weapons that increase the
    # backstab for weapons and armor and other wearable items"). "stat"
    # is "weapon"/"armor" (paired with "category", read by
    # bot._equipped_weapon_proficiency_bonus/_equipped_armor_proficiency_
    # bonus against the specific weapon_category/armor_category the item
    # is boosting) or "backstab"/"throw" (flat, no category -- Backstab
    # is Assassin-only to begin with; Throw is one universal ability, not
    # a category). Appended to a list, same live-summed-at-read-time
    # convention as profession_bonuses just above.
    elif kind == "proficiency_bonus":
        item.setdefault("proficiency_bonuses", []).append(
            {"stat": affix["stat"], "category": affix.get("category"), "value": affix["value"]}
        )
    # Magic item system Phase 8 (2026-09-08, task #6: "generate a RNG
    # magic item with a +1 to a RNG stat"). A real, brand-new axis --
    # nothing before this let equipment touch a core ability score
    # (strength/dexterity/etc.) at all, only derived combat numbers
    # (damage_bonus/ac_base/ac_bonus via stat_bonus above). Appended to
    # a list, same live-summed-at-read-time convention as profession_
    # bonuses/proficiency_bonuses just above -- read by items.
    # equipped_ability_bonus (bot.py's _effective_ability_check_bonus,
    # rules/combat.py's attack rolls).
    elif kind == "ability_bonus":
        item.setdefault("ability_bonuses", []).append({"ability": affix["ability"], "value": affix["value"]})


def materialize_item_instance(item_id: str) -> dict | None:
    """
    Resolves a synthetic "gi<n>" item_id into a full item dict, in the
    exact same shape items.py's static ITEMS entries already use --
    called from items.get_item()'s fallback, so every existing call site
    (equip_item above, combat, shop, scrolls, market) works against a
    generated item with zero changes of its own.
    """
    if not item_id.startswith(GENERATED_ITEM_ID_PREFIX):
        return None
    try:
        instance_id = int(item_id[len(GENERATED_ITEM_ID_PREFIX):])
    except ValueError:
        return None
    with get_connection() as conn:
        row = conn.execute(
            "SELECT * FROM item_instances WHERE instance_id = ?", (instance_id,)
        ).fetchone()
    if row is None:
        return None
    item = json.loads(row["base_stats"])
    item.update({
        "name": row["name"], "type": row["item_type"], "rarity": row["rarity"],
        "price": row["price"], "generated": True, "instance_id": instance_id,
        "set_id": row["set_id"], "source": row["source"],
    })
    for affix in json.loads(row["affixes"]):
        _apply_affix(item, affix)
    return item


# Magic item system Phase 7 (2026-08-02): which base stat field each
# item type's tier bonus rides on -- matches rules/item_generator.py's
# generate_weapon/generate_armor/generate_shield exactly (damage_bonus
# for a weapon, ac_base for armor, ac_bonus for a shield).
FORGE_STAT_BONUS_FIELD = {"weapon": "damage_bonus", "armor": "ac_base", "shield": "ac_bonus"}


def forge_item_instance(item_id: str) -> tuple[bool, str, dict | None]:
    """
    Forging: bumps an EXISTING generated item's rarity to the next real
    tier and its tier-bonus stat_bonus affix value to match -- one
    UPDATE, same instance_id/item_id, so every existing inventory/equip
    reference to it stays valid with no migration. Only ever called
    after bot.py has already confirmed materials/gold and rolled a real
    success -- this function itself just performs the mutation and
    reports what happened.
    """
    if not item_id.startswith(GENERATED_ITEM_ID_PREFIX):
        return False, "Only a real generated magic item can be forged.", None
    try:
        instance_id = int(item_id[len(GENERATED_ITEM_ID_PREFIX):])
    except ValueError:
        return False, "Only a real generated magic item can be forged.", None

    with get_connection() as conn:
        row = conn.execute(
            "SELECT * FROM item_instances WHERE instance_id = ?", (instance_id,)
        ).fetchone()
        if row is None:
            return False, "That item no longer exists.", None
        current_tier = row["rarity"]
        if current_tier not in TIERS or TIERS.index(current_tier) >= len(TIERS) - 1:
            return False, f"The {row['name']} is already at the highest tier — there's nowhere higher to forge it.", None

        next_tier = TIERS[TIERS.index(current_tier) + 1]
        new_bonus = TIER_BONUS[next_tier]
        field = FORGE_STAT_BONUS_FIELD.get(row["item_type"])
        affixes = json.loads(row["affixes"])
        if field:
            for affix in affixes:
                if affix.get("kind") == "stat_bonus" and affix.get("field") == field:
                    affix["value"] = new_bonus
                    break
            else:
                if new_bonus:
                    affixes.append({"kind": "stat_bonus", "field": field, "value": new_bonus})

        old_mult = TIER_PRICE_MULT.get(current_tier, 1) or 1
        new_price = int(row["price"] * TIER_PRICE_MULT.get(next_tier, old_mult) / old_mult)

        conn.execute(
            "UPDATE item_instances SET rarity = ?, price = ?, affixes = ? WHERE instance_id = ?",
            (next_tier, new_price, json.dumps(affixes), instance_id),
        )

    forged_item = materialize_item_instance(item_id)
    return True, f"The {forged_item['name']} is reforged into a real {next_tier.replace('_', ' ')}!", forged_item


def enchant_item_instance(item_id: str, affix: dict) -> tuple[bool, str, dict | None]:
    """
    Enchanting/imbuing (Phase 7): appends ONE new affix, from the exact
    same shared vocabulary _apply_affix already understands, to an
    existing generated item's affix list. The clearest possible proof
    the shared-affix architecture pays off -- an item enchanted with a
    grants_spell affix here works through bot._do_cast_spell's existing
    Phase 3 fallback with zero new cast-side code, since materialize_
    item_instance folds this new affix on exactly like every other one.
    """
    if not item_id.startswith(GENERATED_ITEM_ID_PREFIX):
        return False, "Only a real generated magic item can be enchanted.", None
    try:
        instance_id = int(item_id[len(GENERATED_ITEM_ID_PREFIX):])
    except ValueError:
        return False, "Only a real generated magic item can be enchanted.", None

    with get_connection() as conn:
        row = conn.execute(
            "SELECT * FROM item_instances WHERE instance_id = ?", (instance_id,)
        ).fetchone()
        if row is None:
            return False, "That item no longer exists.", None
        affixes = json.loads(row["affixes"])
        affixes.append(affix)
        conn.execute(
            "UPDATE item_instances SET affixes = ? WHERE instance_id = ?",
            (json.dumps(affixes), instance_id),
        )

    enchanted_item = materialize_item_instance(item_id)
    return True, f"The {enchanted_item['name']} hums with newly-worked power.", enchanted_item


def non_class_armor_bonus(character: dict) -> int:
    """
    Combined AC bonus from shield + rings + any active set bonus --
    everything EXCEPT the class/armor foundation itself (BASE_ARMOR_
    CLASS, an Unarmored Defense formula, or an equipped armor item's
    own ac_base). Public (2026-08-21, Draconic Resilience rework) so a
    caller that already knows the real foundation value for some OTHER
    reason (e.g. bot.py's own draconic_hide investment handler, which
    needs BASE_ARMOR_CLASS -- something db.py deliberately never
    duplicates, see unequip_accessory's own docstring) can combine it
    with this module's shield/ring/set lookups without re-deriving
    them.
    """
    shield_bonus = 0
    shield_id = character.get("equipped_shield")
    if shield_id:
        shield_item = items_module.get_item(shield_id)
        if shield_item:
            shield_bonus = shield_item.get("ac_bonus", 0)
    return shield_bonus + _equipped_ring_ac_bonus(character) + _equipped_set_ac_bonus(character)


def _equipped_ring_ac_bonus(character: dict) -> int:
    """Sum of ac_bonus from every currently-equipped ring (e.g. Ring of Protection)."""
    total = 0
    for acc_id in character.get("equipped_accessories", []):
        acc = items_module.get_item(acc_id)
        if acc and acc.get("type") == "ring":
            total += acc.get("ac_bonus", 0)
    return total


def _equipped_set_ids(character: dict) -> list[str]:
    """set_id of every currently-equipped item (weapon/armor/shield/accessories), duplicates included -- one entry per piece worn."""
    equipped_ids = [
        character.get("equipped_weapon"), character.get("equipped_armor"), character.get("equipped_shield"),
    ] + character.get("equipped_accessories", [])
    set_ids = []
    for item_id in equipped_ids:
        if not item_id:
            continue
        item = items_module.get_item(item_id)
        if item and item.get("set_id"):
            set_ids.append(item["set_id"])
    return set_ids


def _equipped_set_ac_bonus(character: dict) -> int:
    """Sum of any AC-affecting (ac_base/ac_bonus stat_bonus) affix from a currently-active set bonus (Phase 5, 2026-08-02)."""
    total = 0
    for affix in active_set_bonus_affixes(_equipped_set_ids(character)):
        if affix.get("kind") == "stat_bonus" and affix.get("field") in ("ac_base", "ac_bonus"):
            total += affix.get("value", 0)
    return total


def unequip_accessory(telegram_user_id: int, chat_id: int, item_id: str) -> tuple[bool, str, dict | None]:
    """
    Real, previously-missing feature (2026-08-02, needed for set bonuses
    to be meaningfully correct -- a bonus that could only ever be gained
    and never lost had no real "off" state to trust). Only accessories
    (rings/amulets/wondrous) for now -- weapon/armor/shield are single-
    slot columns already correctly replaced by equipping something else;
    "go bare-handed/bare-chested" isn't a real ask, so it's out of scope.

    Real bug caught before shipping (2026-08-02): an earlier version of
    this feature tried to recompute armor_class fully from scratch
    (10 + DEX, or equipped armor's ac_base + DEX) -- wrong, because this
    game's REAL starting AC is per-class (bot.py's BASE_ARMOR_CLASS
    table, plus special Wizard/Monk/Sorcerer Unarmored Defense formulas
    computed once at character creation), never a flat 10-based default.
    A from-scratch recompute silently discarded that the instant anyone
    equipped or unequipped anything. Fixed the only way that's safe
    without duplicating BASE_ARMOR_CLASS into db.py: treat the item's
    own direct ac_bonus, and any set-bonus CHANGE, as pure deltas
    applied on top of whatever armor_class already legitimately is --
    never a full recompute.
    """
    character = get_character(telegram_user_id, chat_id)
    if character is None:
        return False, "No character found.", None
    accessories = character.get("equipped_accessories", [])
    if item_id not in accessories:
        return False, "You're not wearing that.", character
    item = items_module.get_item(item_id)
    old_set_bonus = _equipped_set_ac_bonus(character)
    character["equipped_accessories"] = [a for a in accessories if a != item_id]
    new_set_bonus = _equipped_set_ac_bonus(character)
    new_ac = character["armor_class"] - (item.get("ac_bonus", 0) if item else 0) + (new_set_bonus - old_set_bonus)
    updated = update_character(
        telegram_user_id, chat_id, equipped_accessories=character["equipped_accessories"], armor_class=new_ac,
    )
    name = item["name"] if item else item_id
    return True, f"You take off the {name}. AC is now {new_ac}.", updated


def _meets_equip_requirement(character: dict, item: dict) -> bool:
    """
    Real progression gate for mythic-tier gear (magic item system
    Phase 6, 2026-08-02) -- mirrors bot.py's own location-gate
    convention (_meets_location_level/_meets_rebirth_requirement): an
    item with no "equip_requirement" field is completely unaffected
    (every non-mythic item today). Shape:
    {"any_of": [{"kind": "rebirth_count"|"echo_trial_tier", "value": int}
                | {"kind": "completed_quest", "value": quest_id}, ...]}
    -- met if the character satisfies AT LEAST ONE listed condition,
    since the real intent is "prove real progression ANY one of these
    ways" (a rebirth, a maxed echo trial tier, or beating the hidden
    superboss), not requiring every path at once.
    """
    requirement = item.get("equip_requirement")
    if not requirement:
        return True
    for condition in requirement.get("any_of", []):
        kind = condition.get("kind")
        value = condition.get("value")
        if kind == "rebirth_count" and character.get("rebirth_count", 0) >= value:
            return True
        if kind == "echo_trial_tier" and character.get("echo_trial_tier", 0) >= value:
            return True
        if kind == "completed_quest" and value in character.get("completed_quests", []):
            return True
    return False


def _meets_proficiency_requirement(character: dict, item: dict) -> bool:
    """
    Real feature (2026-08-13, per Coffee, dev-topic: "If weapons can't
    be used by characters, don't let them equip them until they are
    able") -- weapon/armor proficiency (class_features.py's
    is_weapon_proficient/is_armor_proficient) already existed and was
    already used at real combat-resolution time (rules/combat.py's
    resolve_attack drops the proficiency bonus on a non-proficient
    weapon; bot.py's attack-disadvantage check does the same for
    non-proficient armor -- both deliberately kept as-is, a real 5E-
    accurate "you CAN still use it, just badly" simplification, and
    still the right fallback for gear a character already had equipped
    before this gate existed, or for a monster/NPC with no char_class
    at all), but nothing ever stopped equipping the item in the first
    place -- a Wizard could freely equip a Greataxe. This is the equip-
    time half Coffee asked for: blocks the SWAP, not the swing. Reuses
    the exact same "purchased bonus proficiency widens the class-based
    check" pattern already established at both real call sites above
    (rules/combat.py's weapon_proficient, bot.py's is_proficient) --
    the skill_tree_upgrades key format ("prof_<category>_weapons" /
    "prof_<category>_armor") must match those exactly or a real
    purchased widening would silently stop working here.
    """
    if item.get("type") == "weapon":
        category = item.get("weapon_category", "simple")
        return (
            class_features_module.is_weapon_proficient(character.get("char_class"), category)
            or f"prof_{category}_weapons" in (character.get("skill_tree_upgrades") or [])
        )
    if item.get("type") in ("armor", "shield"):
        category = item.get("armor_category", "light")
        return (
            class_features_module.is_armor_proficient(character.get("char_class"), category)
            or f"prof_{category}_armor" in (character.get("skill_tree_upgrades") or [])
        )
    return True


def equip_item(telegram_user_id: int, chat_id: int, item_id: str) -> tuple[bool, str, dict | None]:
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
    character = get_character(telegram_user_id, chat_id)
    if character is None:
        return False, "No character found.", None
    if character["inventory"].get(item_id, 0) < 1:
        return False, "You don't have that to equip.", character

    item = items_module.get_item(item_id)
    if item is None or item.get("type") not in ("weapon", "armor", "shield", "ring", "amulet", "wondrous"):
        return False, f"{item['name'] if item else item_id} isn't something you can equip.", character

    if not _meets_equip_requirement(character, item):
        return (
            False,
            f"The {item['name']} resists your grasp — you haven't proven yourself enough yet to wield it.",
            character,
        )

    if not _meets_proficiency_requirement(character, item):
        category = item.get("weapon_category" if item["type"] == "weapon" else "armor_category", "")
        kind_word = "weapon" if item["type"] == "weapon" else item["type"]
        return (
            False,
            f"{character['name']} isn't trained to use a {category} {kind_word} like the {item['name']} yet.",
            character,
        )

    if item["type"] == "weapon":
        # A weapon carries no AC of its own, but COULD complete a set
        # whose bonus does (Phase 5, 2026-08-02) -- a pure delta, same
        # "capture before, mutate, capture after" pattern every other
        # branch below uses, so a weapon-based set piece isn't silently
        # ignored just because weapons never affected AC before.
        old_set_bonus = _equipped_set_ac_bonus(character)
        character["equipped_weapon"] = item_id
        set_delta = _equipped_set_ac_bonus(character) - old_set_bonus
        if set_delta:
            new_ac = character["armor_class"] + set_delta
            updated = update_character(telegram_user_id, chat_id, equipped_weapon=item_id, armor_class=new_ac)
            return True, f"You equip the {item['name']}. AC is now {new_ac}.", updated
        updated = update_character(telegram_user_id, chat_id, equipped_weapon=item_id)
        return True, f"You equip the {item['name']}.", updated

    current_shield_bonus = 0
    if character.get("equipped_shield"):
        current_shield = items_module.get_item(character["equipped_shield"])
        if current_shield:
            current_shield_bonus = current_shield.get("ac_bonus", 0)
    ring_bonus = _equipped_ring_ac_bonus(character)

    if item["type"] == "armor":
        # Real bug caught before shipping (2026-08-02): an earlier
        # version of this branch recomputed AC fully from scratch
        # (item's ac_base + DEX) -- wrong, because a character's real
        # starting AC is per-class (bot.py's BASE_ARMOR_CLASS table,
        # Wizard/Monk/Sorcerer Unarmored Defense formulas), never a flat
        # 10-based default; a from-scratch recompute silently discarded
        # that. Restored to the original delta math (ac_base + DEX +
        # whatever shield/ring bonus already applies), now with a set-
        # bonus delta layered on top the same way every other branch
        # here handles it.
        old_set_bonus = _equipped_set_ac_bonus(character)
        character["equipped_armor"] = item_id
        set_delta = _equipped_set_ac_bonus(character) - old_set_bonus
        dex_mod = ability_modifier(character["dexterity"])
        foundation = item["ac_base"] + dex_mod
        # Draconic Resilience rework (2026-08-21, per Coffee: "have it
        # add onto their armor" instead of only working unarmored) --
        # equipping armor used to silently discard 100% of whatever
        # draconic_hide compounding had accumulated (confirmed live:
        # Pan's 2 invested points were producing zero real AC once he
        # equipped real armor). This applies the same +10%-per-point
        # compounding to the NEW armor's own foundation instead, so the
        # bonus survives an armor equip/swap rather than vanishing. Safe
        # to compute entirely here (no BASE_ARMOR_CLASS needed) since
        # `item["ac_base"]` is the new item's own real, known field, not
        # a reconstruction of anything.
        draconic_points = (
            (character.get("skill_tree_upgrades") or []).count("draconic_hide")
            if character.get("char_class") == "Sorcerer" else 0
        )
        # Same iterative "+10%, floored at +1" step bot.py's _um_pool_step
        # already uses for every Universal Manipulation pool (Vitality,
        # Arcane Reserve, and this one) -- matched exactly (not a clean
        # x * 1.1**n closed form) so a Sorcerer's real AC here is
        # identical to what it always would have been, not a stealth
        # rebalance from switching formulas. A generic compounding-math
        # step, not class-specific knowledge, so it's safe to mirror
        # here without duplicating BASE_ARMOR_CLASS.
        for _ in range(draconic_points):
            foundation += max(1, round(foundation * 0.10))
        # Real Fighting Style: Defense (2026-09-04) -- "+1 AC while
        # wearing armor," folded into this same recompute so it
        # survives every future armor swap, same as the Draconic
        # Resilience compounding just above. _do_choose_fighting_style
        # applies this same +1 immediately if armor is already worn at
        # the moment Defense is chosen; there's no unequip_item in this
        # codebase (armor is only ever swapped, never removed), so
        # there's no "AC drops when you take armor off" case to handle.
        defense_bonus = 1 if character.get("fighting_style") == "Defense" else 0
        new_ac = foundation + current_shield_bonus + ring_bonus + set_delta + defense_bonus
        updated = update_character(telegram_user_id, chat_id, equipped_armor=item_id, armor_class=new_ac)
        return True, f"You put on the {item['name']} (AC {new_ac}).", updated

    if item["type"] == "shield":
        # Additive on top of current AC, swapping out any previously-
        # equipped shield's bonus first rather than stacking both.
        old_set_bonus = _equipped_set_ac_bonus(character)
        character["equipped_shield"] = item_id
        set_delta = _equipped_set_ac_bonus(character) - old_set_bonus
        new_ac = character["armor_class"] - current_shield_bonus + item["ac_bonus"] + set_delta
        updated = update_character(telegram_user_id, chat_id, equipped_shield=item_id, armor_class=new_ac)
        return True, f"You raise the {item['name']} (AC {new_ac}).", updated

    # Ring / amulet / wondrous: added to the accessories list (worn
    # alongside weapon/armor/shield, not instead of them).
    accessories = character["equipped_accessories"]
    if item_id in accessories:
        return False, f"You're already wearing the {item['name']}.", character
    old_set_bonus = _equipped_set_ac_bonus(character)
    accessories = accessories + [item_id]
    character["equipped_accessories"] = accessories
    updates = {"equipped_accessories": accessories}
    note_parts = [f"You put on the {item['name']}."]

    # This item's own direct ac_bonus, PLUS whatever set-bonus delta
    # equipping it just caused (2026-08-02, Phase 5) -- a ring with no
    # ac_bonus of its own can still be the piece that crosses a set
    # threshold, so both sources are checked, never just the item's own
    # field the way this branch originally only did.
    set_delta = _equipped_set_ac_bonus(character) - old_set_bonus
    ac_delta = item.get("ac_bonus", 0) + set_delta
    if ac_delta:
        new_ac = character["armor_class"] + ac_delta
        updates["armor_class"] = new_ac
        note_parts.append(f"AC is now {new_ac}.")
    if item.get("constitution_set") and item["constitution_set"] > character["constitution"]:
        updates["constitution"] = item["constitution_set"]
        note_parts.append(f"Constitution is now {item['constitution_set']}.")

    updated = update_character(telegram_user_id, chat_id, **updates)
    return True, " ".join(note_parts), updated


def can_dual_wield(character: dict) -> tuple[bool, str]:
    """
    Real Fighting Style: Two-Weapon Fighting's own precondition
    (2026-09-04, per Coffee: ship all 6 styles, then build mastery-
    gated dual wielding "for players that have attained mastery"). Real
    5E gates dual wielding on wielding two LIGHT weapons -- this
    catalog has no "light" property at all, so this house-rules the
    gate onto the character's own weapon_proficiency_pct instead,
    matching this game's existing "100% = Mastery" grind/language (see
    bot._format_proficiency_line's own header) -- a real reward for
    grinding, not a free starting option. Returns (eligible, reason) so
    a refusal can always be honest about exactly how far off the
    character still is, same "grounded fact" discipline as every other
    refusal shipped alongside this feature.
    """
    equipped_id = character.get("equipped_weapon")
    if not equipped_id:
        return False, "You need a real weapon equipped in your main hand first."
    main_weapon = items_module.get_item(equipped_id)
    if main_weapon is None or main_weapon.get("type") != "weapon":
        return False, "You need a real weapon equipped in your main hand first."
    category = main_weapon.get("weapon_category", "simple")
    pct = (character.get("weapon_proficiency_pct") or {}).get(category, 0.0)
    if pct < 100:
        return False, f"You haven't mastered {category} weapons yet — {pct:.1f}% of the 100% needed."
    return True, ""


def equip_offhand_weapon(telegram_user_id: int, chat_id: int, item_id: str) -> tuple[bool, str, dict | None]:
    """
    Real, mastery-gated dual wielding (2026-09-04) -- see
    can_dual_wield's own docstring for the real precondition. Mirrors
    equip_item's own weapon branch (validation, then a plain field
    write) but targets the separate equipped_offhand_weapon slot
    instead of overwriting the real main-hand equipped_weapon.
    """
    character = get_character(telegram_user_id, chat_id)
    if character is None:
        return False, "No character found.", None
    if character["inventory"].get(item_id, 0) < 1:
        return False, "You don't have that to equip.", character

    item = items_module.get_item(item_id)
    if item is None or item.get("type") != "weapon":
        return False, f"{item['name'] if item else item_id} isn't a real weapon.", character

    if not _meets_equip_requirement(character, item):
        return (
            False,
            f"The {item['name']} resists your grasp — you haven't proven yourself enough yet to wield it.",
            character,
        )
    if not _meets_proficiency_requirement(character, item):
        category = item.get("weapon_category", "")
        return (
            False,
            f"{character['name']} isn't trained to use a {category} weapon like the {item['name']} yet.",
            character,
        )

    eligible, reason = can_dual_wield(character)
    if not eligible:
        return False, reason, character

    updated = update_character(telegram_user_id, chat_id, equipped_offhand_weapon=item_id)
    return True, f"You take up the {item['name']} in your off hand.", updated


def _best_equippable_candidate(telegram_user_id: int, chat_id: int, candidate_ids: list[str], sort_key) -> tuple[str | None, dict | None]:
    """
    Tries `candidate_ids` (already-carried items of one slot type) in
    descending `sort_key` order via equip_item, stopping at the FIRST
    one that actually succeeds. Real bug (2026-08-13, Coffee dev-
    bridge, real pasted log): auto_equip_best_gear used to pick only
    the single highest-stat item and equip it ONCE -- once v1.27.179's
    real proficiency gate started correctly rejecting an unearned pick
    (e.g. heavy armor a Wizard isn't trained for), the slot was just
    left empty, with the character's own genuinely-wearable gear never
    even attempted. Now falls through to the next-best real candidate
    until one actually equips, so "the best thing they can hold for
    what they're able to hold" (Coffee's own framing) is genuine.
    Returns (message, character) for whichever equip succeeded, or
    (None, character) if every real candidate was rejected -- `character`
    is always the current, unmutated character row either way.
    """
    character = get_character(telegram_user_id, chat_id)
    for item_id in sorted(candidate_ids, key=sort_key, reverse=True):
        success, msg, character = equip_item(telegram_user_id, chat_id, item_id)
        if success:
            return msg, character
    return None, character


def auto_equip_best_gear(telegram_user_id: int, chat_id: int) -> tuple[str, dict | None]:
    """
    Picks the real best weapon (highest average damage -- see
    rules.dice.average_damage) and real best armor (highest ac_base)
    out of whatever this character is actually carrying, and equips
    both via equip_item above. Added 2026-07-15 alongside equip_item
    itself, per Coffee: a player shouldn't have to know every weapon's
    exact damage die to get sensible gear on -- this picks for them.
    Always returns a real, honest summary, even if there was nothing
    to equip in one or both slots (never silently no-ops). Falls back
    through progressively-lesser real candidates via _best_equippable_
    candidate (2026-08-13) when the single best pick isn't one this
    character is actually proficient with yet.
    """
    character = get_character(telegram_user_id, chat_id)
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
        msg, character = _best_equippable_candidate(
            telegram_user_id, chat_id, weapon_ids,
            lambda i: average_damage(items_module.get_item(i)["damage_dice"],
                                      items_module.get_item(i).get("damage_bonus", 0)),
        )
        messages.append(msg or "Nothing you're carrying can be wielded yet.")
    else:
        messages.append("No weapon carried to equip.")

    if armor_ids:
        msg, character = _best_equippable_candidate(
            telegram_user_id, chat_id, armor_ids, lambda i: items_module.get_item(i)["ac_base"],
        )
        messages.append(msg or "Nothing you're carrying can be worn yet.")
    else:
        messages.append("No armor carried to equip.")

    shield_ids = [
        item_id for item_id, qty in character["inventory"].items()
        if qty > 0 and (items_module.get_item(item_id) or {}).get("type") == "shield"
    ]
    if shield_ids:
        msg, character = _best_equippable_candidate(
            telegram_user_id, chat_id, shield_ids, lambda i: items_module.get_item(i)["ac_bonus"],
        )
        if msg:
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
        success, msg, character = equip_item(telegram_user_id, chat_id, accessory_id)
        if success:
            messages.append(msg)

    return " ".join(messages), character


def move_character(telegram_user_id: int, chat_id: int, new_location: str) -> dict | None:
    return update_character(telegram_user_id, chat_id, current_location=new_location)


def mark_visited(telegram_user_id: int, chat_id: int, location_id: str) -> dict | None:
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
    character = get_character(telegram_user_id, chat_id)
    if character is None:
        return None
    visited = character["visited_locations"]
    if location_id in visited:
        visited.remove(location_id)
    visited.append(location_id)
    return update_character(telegram_user_id, chat_id, visited_locations=visited)


def mark_known_monster(telegram_user_id: int, chat_id: int, monster_key: str) -> dict | None:
    """
    Adds monster_key to this character's known_monsters (bestiary
    discovery), if new -- same fog-of-war pattern as mark_visited above,
    called once a character has actually fought a given monster type.
    """
    character = get_character(telegram_user_id, chat_id)
    if character is None:
        return None
    if monster_key in character["known_monsters"]:
        return character
    character["known_monsters"].append(monster_key)
    return update_character(telegram_user_id, chat_id, known_monsters=character["known_monsters"])


def mark_defeated_monster(telegram_user_id: int, chat_id: int, monster_key: str) -> dict | None:
    """
    Adds monster_key to this character's defeated_monsters, if new --
    a genuinely separate fact from known_monsters above (2026-08-15,
    per Coffee: "make the bosses the same lvl as the ledger but dont
    show it to the player until they beat the boss, then include it in
    bestiary"). known_monsters is set the moment a fight STARTS
    (mark_known_monster, from _do_start_combat), so a boss fled from
    or lost is already "known" -- not good enough to gate a real
    spoiler (a boss's own level) behind. Only ever called from
    _award_victory_xp, the one real place a party win is already
    confirmed to have happened.
    """
    character = get_character(telegram_user_id, chat_id)
    if character is None:
        return None
    if monster_key in character["defeated_monsters"]:
        return character
    character["defeated_monsters"].append(monster_key)
    return update_character(telegram_user_id, chat_id, defeated_monsters=character["defeated_monsters"])


def mark_location_cleared(telegram_user_id: int, chat_id: int, location_id: str) -> dict | None:
    """
    Marks a location as "cleared" -- this character has won a real fight
    there at least once. Same fog-of-war-style pattern as mark_visited/
    mark_known_monster, called only from a real combat-victory checkpoint
    (see bot.py's _mark_location_cleared_for_party), never on a loss or
    fled fight. Consumed by _check_story_gate's requires_cleared_location
    check to block deeper connections until the room before them has
    actually been fought through.
    """
    character = get_character(telegram_user_id, chat_id)
    if character is None:
        return None
    if location_id in character["cleared_locations"]:
        return character
    character["cleared_locations"].append(location_id)
    return update_character(telegram_user_id, chat_id, cleared_locations=character["cleared_locations"])


def bump_location_defeat_count(telegram_user_id: int, chat_id: int, location_id: str) -> int:
    """
    Real "Advanced Dungeons" repeated gate, ported to evolved overworld
    dungeons (2026-09-06, per Coffee: "keep going"). Separate from
    cleared_locations above -- a location flagged `repeat_required`
    (rules/dungeon_evolve.py) needs this many real per-character wins
    before bot._mark_location_cleared_for_party actually calls
    mark_location_cleared for it, so the room keeps genuinely blocking
    movement across several real fights instead of clearing on the
    first. Returns the new count so the caller can compare it against
    the room's own requirement without a second read.
    """
    character = get_character(telegram_user_id, chat_id)
    if character is None:
        return 0
    counts = character["location_defeat_counts"]
    counts[location_id] = counts.get(location_id, 0) + 1
    update_character(telegram_user_id, chat_id, location_defeat_counts=counts)
    return counts[location_id]


def learn_spell(telegram_user_id: int, chat_id: int, spell_id: str) -> dict | None:
    character = get_character(telegram_user_id, chat_id)
    if character is None:
        return None
    if spell_id not in character["known_spells"]:
        character["known_spells"].append(spell_id)
    return update_character(telegram_user_id, chat_id, known_spells=character["known_spells"])


def spend_spell_slot(telegram_user_id: int, chat_id: int) -> tuple[bool, dict | None]:
    """
    Attempts to spend one spell slot. Returns (success, updated_character).
    Fails cleanly (no partial change) if none remain.
    """
    character = get_character(telegram_user_id, chat_id)
    if character is None:
        return False, None
    if character["spell_slots_current"] <= 0:
        return False, character
    updated = update_character(
        telegram_user_id, chat_id, spell_slots_current=character["spell_slots_current"] - 1
    )
    return True, updated


def complete_quest(telegram_user_id: int, chat_id: int, quest_id: str) -> dict | None:
    """Marks a quest completed and clears it from active_quests, if present."""
    character = get_character(telegram_user_id, chat_id)
    if character is None:
        return None
    if quest_id not in character["completed_quests"]:
        character["completed_quests"].append(quest_id)
    active = character["active_quests"]
    active.pop(quest_id, None)
    return update_character(
        telegram_user_id, chat_id, completed_quests=character["completed_quests"], active_quests=active
    )


def accept_quest(telegram_user_id: int, chat_id: int, quest_id: str) -> dict | None:
    character = get_character(telegram_user_id, chat_id)
    if character is None:
        return None
    active = character["active_quests"]
    if quest_id not in active:
        active[quest_id] = {"accepted_at": datetime.now(timezone.utc).isoformat()}
    return update_character(telegram_user_id, chat_id, active_quests=active)


def cancel_quest(telegram_user_id: int, chat_id: int, quest_id: str) -> dict | None:
    """
    Real request (2026-08-22, per Coffee: "in the quests menu when we
    click on a quest we can cancel the quest?... doesnt decline the
    quest it jus allows them to do it at another time"). Same pop-
    from-active_quests shape complete_quest already uses, just WITHOUT
    ever touching completed_quests -- so _offerable_quest_at_location
    (which only ever skips a quest_id already in completed_quests or
    active_quests) naturally re-offers it the next time this character
    reaches the quest's own location, exactly as asked.
    """
    character = get_character(telegram_user_id, chat_id)
    if character is None:
        return None
    active = character["active_quests"]
    active.pop(quest_id, None)
    return update_character(telegram_user_id, chat_id, active_quests=active)


def join_guild(telegram_user_id: int, chat_id: int, guild_id: str) -> dict | None:
    """
    First guild ever joined becomes the PRIMARY guild (character.guild,
    full benefits + shop discount + guild quest). Every guild after
    that (only reachable once guilds.eligible_for_guild's evolution +
    mastery gate has already passed -- a real "Promotion", per Coffee)
    is a SECONDARY guild instead -- real curriculum progress, real
    permanent stat/profession/proficiency growth (see add_xp), AND an
    immediate one-time GUILD_PROMOTION_PCT_BONUS % bump the moment the
    Promotion happens, same as the primary's growth but never the
    primary's other mechanical benefits (a Promotion is a real, earned
    prestige/growth system, not a second full membership stacking
    every bonus).
    """
    character = get_character(telegram_user_id, chat_id)
    if character is None:
        return None
    now = datetime.now(timezone.utc).isoformat()
    if not character.get("guild"):
        # Starts the guild curriculum's first step immediately
        # (2026-08-12) -- guild_curriculum_step defaults to 0 already,
        # but stamping unlocked_at here (rather than leaving it NULL
        # until some later write) starts that step's real time-gate the
        # moment membership begins, not whenever the member first
        # happens to ask about it.
        return update_character(
            telegram_user_id, chat_id, guild=guild_id,
            guild_curriculum_step_unlocked_at=now, guild_join_level=character["level"],
        )
    secondary_guilds = character["secondary_guilds"] + [guild_id]
    join_levels = character["secondary_guild_join_levels"]
    join_levels[guild_id] = character["level"]
    unlocked_at = character["secondary_guild_curriculum_unlocked_at"]
    unlocked_at[guild_id] = now
    updates = {
        "secondary_guilds": secondary_guilds, "secondary_guild_join_levels": join_levels,
        "secondary_guild_curriculum_unlocked_at": unlocked_at,
    }
    # Real Promotion bonus (2026-08-13, per Coffee: "promotions shud
    # increase % so players can eventually work on maxing out %") -- a
    # genuine evolution+mastery-earned Promotion into another guild
    # grants an immediate, one-time real bump to that guild's own
    # profession/scalar proficiency %s, on top of (not instead of) the
    # gradual per-level growth add_xp keeps applying afterward -- gives
    # a real, felt payoff the moment a Promotion actually happens, with
    # the same 100%-capped "eventually maxing out" ceiling as everything
    # else in this %-based system.
    import guilds as guilds_module
    promotion_professions = guilds_module.GUILD_PERMANENT_PROFESSION.get(guild_id) or []
    if promotion_professions:
        mastery = dict(character["profession_mastery_pct"])
        for profession in promotion_professions:
            mastery[profession] = min(mastery.get(profession, 1.0) + guilds_module.GUILD_PROMOTION_PCT_BONUS, 100.0)
        updates["profession_mastery_pct"] = mastery
    for scalar_field in guilds_module.GUILD_PERMANENT_SCALAR_PROFICIENCY.get(guild_id) or []:
        current = character.get(scalar_field, 1.0)
        updates[scalar_field] = min(current + guilds_module.GUILD_PROMOTION_PCT_BONUS, 100.0)
    return update_character(telegram_user_id, chat_id, **updates)


def leave_guild(telegram_user_id: int, chat_id: int, guild_id: str) -> dict | None:
    """
    Drops membership in exactly one currently-held guild (primary or
    secondary) -- clears that guild's own curriculum/benefit state and
    frees its slot back up, but never touches ability scores: any
    permanent stat points add_xp already granted while a member stay
    exactly where they are, real and permanent, per Coffee's explicit
    "when they leave they keep that bonus... but do not keep the other
    bonuses" design. Leaving the PRIMARY guild while a secondary is
    still held does NOT promote a secondary to primary -- the primary
    slot simply becomes empty again, re-fillable by any future join
    (first-guild rules) the same as a character who'd never joined one.
    """
    character = get_character(telegram_user_id, chat_id)
    if character is None:
        return None
    if character.get("guild") == guild_id:
        return update_character(
            telegram_user_id, chat_id, guild=None, guild_curriculum_step=0,
            guild_curriculum_step_unlocked_at=None, guild_curriculum_state={}, guild_join_level=None,
        )
    if guild_id in character["secondary_guilds"]:
        secondary_guilds = [g for g in character["secondary_guilds"] if g != guild_id]
        join_levels = character["secondary_guild_join_levels"]
        join_levels.pop(guild_id, None)
        steps = character["secondary_guild_curriculum_steps"]
        steps.pop(guild_id, None)
        unlocked_at = character["secondary_guild_curriculum_unlocked_at"]
        unlocked_at.pop(guild_id, None)
        state = character["secondary_guild_curriculum_state"]
        state.pop(guild_id, None)
        return update_character(
            telegram_user_id, chat_id, secondary_guilds=secondary_guilds,
            secondary_guild_join_levels=join_levels, secondary_guild_curriculum_steps=steps,
            secondary_guild_curriculum_unlocked_at=unlocked_at, secondary_guild_curriculum_state=state,
        )
    return None


def bind_remnant(telegram_user_id: int, chat_id: int, remnant_id: str) -> dict | None:
    """
    Real, permanent credit (2026-08-13, per Coffee's Remnants system --
    see remnants.py's own docstring) for having helped defeat a real
    Unbound boss -- every real party member present when it falls gets
    this called for them individually (bot.py's defeat_monster event
    hook), same "shared credit" shape as db.record_bestiary_monster's
    known_monsters. A no-op if already bound (never duplicates an
    entry, and never re-triggers whatever one-time effect binding it
    might someday carry).
    """
    character = get_character(telegram_user_id, chat_id)
    if character is None:
        return None
    if remnant_id in character["bound_remnants"]:
        return character
    return update_character(
        telegram_user_id, chat_id, bound_remnants=character["bound_remnants"] + [remnant_id],
    )


def advance_guild_curriculum_step(telegram_user_id: int, chat_id: int, guild_id: str | None = None) -> dict | None:
    """
    Credits the character's CURRENT guild curriculum step for `guild_id`
    (default: the primary guild, preserving every existing call site's
    exact prior behavior) and unlocks the next one -- advances that
    guild's own step counter by 1, re-stamps its unlocked-at to now
    (starting the next step's own real time-gate fresh), and clears its
    scratch state (any data an alignment_choice step's setup narration
    left behind belongs to the step just finished, not the next one).
    `guild_id` may also name a SECONDARY guild (2026-08-13, "doubled
    up" evolution guilds) -- reads/writes the guild_id-keyed secondary_
    guild_* dicts instead of the singular primary fields in that case.
    """
    character = get_character(telegram_user_id, chat_id)
    if character is None:
        return None
    updates = _compute_guild_curriculum_advance(character, guild_id)
    return update_character(telegram_user_id, chat_id, **updates)


def advance_guild_curriculum_step_by_id(character_id: int, guild_id: str | None = None) -> dict | None:
    """
    Same real advance-and-unlock logic as advance_guild_curriculum_step,
    but targets one exact character directly rather than "whichever slot
    this telegram_user_id has active right now" -- see add_xp_by_id's
    docstring for why this matters (bot._check_guild_curriculum_cooldowns
    crediting a dormant alt).
    """
    character = get_character_by_id(character_id)
    if character is None:
        return None
    updates = _compute_guild_curriculum_advance(character, guild_id)
    return update_character_by_id(character_id, **updates)


def _compute_guild_curriculum_advance(character: dict, guild_id: str | None) -> dict:
    now = datetime.now(timezone.utc).isoformat()
    if guild_id is None or guild_id == character.get("guild"):
        return {
            "guild_curriculum_step": character["guild_curriculum_step"] + 1,
            "guild_curriculum_step_unlocked_at": now,
            "guild_curriculum_state": {},
        }
    steps = character["secondary_guild_curriculum_steps"]
    steps[guild_id] = steps.get(guild_id, 0) + 1
    unlocked_at = character["secondary_guild_curriculum_unlocked_at"]
    unlocked_at[guild_id] = now
    state = character["secondary_guild_curriculum_state"]
    state.pop(guild_id, None)
    return {
        "secondary_guild_curriculum_steps": steps,
        "secondary_guild_curriculum_unlocked_at": unlocked_at,
        "secondary_guild_curriculum_state": state,
    }


def get_character(telegram_user_id: int, chat_id: int) -> dict | None:
    """Returns this telegram_user_id's currently ACTIVE character, if any."""
    with get_connection() as conn:
        character_id = _active_character_id(telegram_user_id, chat_id, conn)
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


def list_characters(telegram_user_id: int, chat_id: int) -> list[dict]:
    """All non-deleted characters this telegram_user_id owns IN THIS CHAT, oldest first."""
    with get_connection() as conn:
        rows = conn.execute(
            "SELECT * FROM characters WHERE telegram_user_id = ? AND chat_id = ? AND is_deleted = 0 ORDER BY character_id",
            (telegram_user_id, chat_id),
        ).fetchall()
    return [_row_to_dict(r) for r in rows]


def get_all_characters_in_chat(chat_id: int) -> list[dict]:
    """
    Every non-deleted character slot IN THIS CHAT -- real players' every
    slot (active or not) plus AI companions -- unlike
    _get_party_members/get_idle_real_characters, deliberately NOT joined
    against active_characters. Real live bug (2026-08-14, dev-bridge
    screenshots + proof of a satisfied-but-uncredited step):
    bot._check_guild_curriculum_cooldowns used _get_party_members, whose
    active_characters join means it only ever sees whichever ONE
    character slot a multi-character player currently has selected
    (active_characters' real PK is (telegram_user_id, chat_id) -- only
    one slot per player per chat can be active at a time). A player's
    OTHER character slots -- each with their own real, standing guild
    curriculum progress -- were completely invisible to this background
    sweep the entire time they weren't the selected slot, no matter how
    long real time passed, only ever catching up once the player
    happened to switch back and take some action that re-triggered the
    live checkpoint directly. Guild curriculum progress belongs to the
    character, not to "whichever slot is active right now" -- this
    sweep needs every real character, same as list_characters but
    across every player in the chat rather than just one.
    """
    with get_connection() as conn:
        rows = conn.execute(
            "SELECT * FROM characters WHERE chat_id = ? AND is_deleted = 0",
            (chat_id,),
        ).fetchall()
    return [_row_to_dict(r) for r in rows]


def get_leaderboard(chat_id: int, limit: int = 10) -> list[dict]:
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
    Scoped to one tenant chat (Phase 4e, 2026-08-06) -- a second tenant's
    players must never show up on this bot's home group's leaderboard.
    """
    with get_connection() as conn:
        rows = conn.execute(
            """
            SELECT c.* FROM characters c
            JOIN active_characters a ON a.character_id = c.character_id
            WHERE c.is_deleted = 0 AND c.is_ai = 0 AND c.chat_id = ?
            ORDER BY c.xp DESC, c.level DESC
            LIMIT ?
            """,
            (chat_id, limit),
        ).fetchall()
    return [_row_to_dict(r) for r in rows]


def find_character_by_name(name: str, chat_id: int) -> dict | None:
    """
    Finds any non-deleted character (human or AI, active or a player's
    other non-active character slot) by name, case-insensitive. Used
    for read-only sheet lookups -- Coffee wants to see anyone's sheet,
    not just whoever's currently marked active (2026-07-14). Scoped to
    one tenant chat (Phase 4e, 2026-08-06) -- two different tenant chats
    could easily have same-named characters, and a lookup must never
    cross that boundary.
    """
    with get_connection() as conn:
        row = conn.execute(
            "SELECT * FROM characters WHERE is_deleted = 0 AND chat_id = ? AND LOWER(name) = LOWER(?) "
            "ORDER BY character_id LIMIT 1",
            (chat_id, name),
        ).fetchone()
    return _row_to_dict(row) if row else None


def find_character_by_telegram_username(username: str, chat_id: int) -> dict | None:
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

    Real multi-tenant gap fixed (2026-08-08): this had no chat_id
    filter at all, so a tagged @username could resolve to that
    player's character in a DIFFERENT tenant chat. Not yet called
    anywhere in bot.py (confirmed via grep) so never actually
    exploitable, but fixed defensively before it's wired up rather
    than after.
    """
    username = username.lstrip("@")
    with get_connection() as conn:
        row = conn.execute(
            "SELECT * FROM characters WHERE is_deleted = 0 AND chat_id = ? "
            "AND LOWER(telegram_username) = LOWER(?) ORDER BY character_id LIMIT 1",
            (chat_id, username),
        ).fetchone()
    return _row_to_dict(row) if row else None


def update_telegram_username(telegram_user_id: int, chat_id: int, username: str | None) -> None:
    """
    Keeps a character's telegram_username fresh from the real incoming
    Update on every message (Telegram usernames can change, and are
    None for accounts that don't have one set) -- a no-op if this user
    has no active character yet, since there's nothing to attach it to.
    """
    if not username:
        return
    with get_connection() as conn:
        character_id = _active_character_id(telegram_user_id, chat_id, conn)
        if character_id is None:
            return
        conn.execute(
            "UPDATE characters SET telegram_username = ? WHERE character_id = ?",
            (username, character_id),
        )


def switch_character(telegram_user_id: int, chat_id: int, character_id: int) -> dict | None:
    """
    Makes character_id the active character for telegram_user_id, if it
    exists, is owned by them, and isn't deleted. Returns the newly-active
    character dict, or None if the switch was invalid.
    """
    with get_connection() as conn:
        row = conn.execute(
            "SELECT * FROM characters WHERE character_id = ? AND telegram_user_id = ? AND chat_id = ? AND is_deleted = 0",
            (character_id, telegram_user_id, chat_id),
        ).fetchone()
        if row is None:
            return None
        _set_active_character(telegram_user_id, chat_id, character_id, conn)
    return _row_to_dict(row)


def delete_character(telegram_user_id: int, chat_id: int, character_id: int) -> bool:
    """
    Soft-deletes a character this telegram_user_id owns. If it was the
    active character, another remaining (non-deleted) character owned by
    the same user becomes active, if one exists; otherwise the user has
    no active character until they create or switch to one.
    """
    with get_connection() as conn:
        row = conn.execute(
            "SELECT character_id FROM characters WHERE character_id = ? AND telegram_user_id = ? AND chat_id = ? AND is_deleted = 0",
            (character_id, telegram_user_id, chat_id),
        ).fetchone()
        if row is None:
            return False

        conn.execute("UPDATE characters SET is_deleted = 1 WHERE character_id = ?", (character_id,))

        current_active = _active_character_id(telegram_user_id, chat_id, conn)
        if current_active == character_id:
            replacement = conn.execute(
                "SELECT character_id FROM characters WHERE telegram_user_id = ? AND chat_id = ? AND is_deleted = 0 "
                "ORDER BY character_id LIMIT 1",
                (telegram_user_id, chat_id),
            ).fetchone()
            if replacement:
                _set_active_character(telegram_user_id, chat_id, replacement["character_id"], conn)
            else:
                conn.execute("DELETE FROM active_characters WHERE telegram_user_id = ? AND chat_id = ?", (telegram_user_id, chat_id))
    return True


def add_xp(telegram_user_id: int, chat_id: int, amount: int) -> dict | None:
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
    character = get_character(telegram_user_id, chat_id)
    if character is None:
        return None
    updates = _compute_xp_updates(character, amount)
    return update_character(telegram_user_id, chat_id, **updates)


def add_xp_by_id(character_id: int, amount: int) -> dict | None:
    """
    Same real leveling logic as add_xp, but targets one exact character
    directly rather than "whichever slot this telegram_user_id has
    active right now" -- needed anywhere a background process must
    credit a SPECIFIC character that may not be the active one (e.g.
    bot._check_guild_curriculum_cooldowns crediting a dormant alt's own
    real, standing curriculum progress; see db.get_all_characters_in_chat's
    docstring for the real bug this closes).
    """
    character = get_character_by_id(character_id)
    if character is None:
        return None
    updates = _compute_xp_updates(character, amount)
    return update_character_by_id(character_id, **updates)


def _compute_xp_updates(character: dict, amount: int) -> dict:

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
            # Redundant-spell pruning (2026-08-22, per Coffee) -- see
            # spells.prune_redundant_lower_power_spells's own docstring
            # for the full real-power-comparison rule this applies.
            updates["known_spells"] = spells_module.prune_redundant_lower_power_spells(
                character["known_spells"] + newly_learned
            )

        # Real, permanent guild stat + profession growth (2026-08-13, per
        # Coffee: "you add a + to stat that levels up the players stat
        # directly... you can decide how many points they get per
        # levels for that" -- then "add a stat to the proficiencies too
        # that level up when the player levels up that is also
        # permanant, each guild shud have one, make sure all
        # proficiencies are covered by this system and all stats are
        # also"). For every guilds.GUILD_STAT_BONUS_LEVELS levels gained
        # WHILE a member of a given guild (computed from bonus-tier
        # thresholds crossed between old_level and new_level, not just
        # "did we cross ANY threshold", so a big multi-level XP award
        # still grants every point it should in one pass): +1 to that
        # guild's own guilds.GUILDS[...]['permanent_stat'] (ability
        # score, uncapped -- 2026-08-20 per Coffee: "that's the whole
        # point of having guild and being able to increase the status
        # points for level ups", the old ability_score_cap clamp here was
        # removed entirely, same as every other cap-check site) AND +1%
        # to that guild's own guilds.GUILD_PERMANENT_PROFESSION[...]
        # profession's profession_mastery_pct (capped at
        # PROFICIENCY_MAX_PCT via bot.py's own constant -- inlined as
        # 100.0 here rather than importing bot.py, which itself imports
        # db.py). Once applied both are ordinary, permanent character
        # values like any other -- leaving a guild (db.leave_guild) never
        # reverts either, which is the entire point of this system.
        import guilds as guilds_module
        PROFICIENCY_MAX_PCT = 100.0

        def _bonus_tiers(join_level: int | None, at_level: int) -> int:
            if join_level is None or at_level <= join_level:
                return 0
            return (at_level - join_level) // guilds_module.GUILD_STAT_BONUS_LEVELS

        def _apply_guild_growth(guild_id: str, join_level: int | None) -> None:
            if join_level is None:
                return
            gained = _bonus_tiers(join_level, new_level) - _bonus_tiers(join_level, old_level)
            if not gained:
                return
            stat = guilds_module.permanent_stat_for(guild_id, character)
            if stat:
                current_value = updates.get(stat, character[stat])
                updates[stat] = current_value + gained
            professions = guilds_module.GUILD_PERMANENT_PROFESSION.get(guild_id) or []
            if professions:
                mastery = dict(updates.get("profession_mastery_pct", character["profession_mastery_pct"]))
                for profession in professions:
                    current_pct = mastery.get(profession, 1.0)
                    mastery[profession] = min(current_pct + gained, PROFICIENCY_MAX_PCT)
                updates["profession_mastery_pct"] = mastery
            scalar_fields = list(guilds_module.GUILD_PERMANENT_SCALAR_PROFICIENCY.get(guild_id) or [])
            # backstab_proficiency_pct only ever matters for a real
            # Assassin (bot.py's own rule -- see guilds.py's
            # GUILD_PERMANENT_SCALAR_PROFICIENCY docstring), so it's
            # added conditionally here rather than listed unconditionally
            # above; per Coffee: "if the player has it, otherwise dont
            # show that one".
            if guild_id == "thieves_guild" and character.get("subclass") == "Assassin":
                scalar_fields.append("backstab_proficiency_pct")
            for scalar_field in scalar_fields:
                current_scalar = updates.get(scalar_field, character.get(scalar_field, 1.0))
                updates[scalar_field] = min(current_scalar + gained, PROFICIENCY_MAX_PCT)

        if character.get("guild"):
            _apply_guild_growth(character["guild"], character.get("guild_join_level"))
        for gid in character.get("secondary_guilds") or []:
            _apply_guild_growth(gid, (character.get("secondary_guild_join_levels") or {}).get(gid))

    return updates


# ---------------------------------------------------------------------
# NPC relationships — persistent, per-(player, NPC) memory and rapport.
# ---------------------------------------------------------------------

MAX_MEMORY_EVENTS = 20  # oldest facts drop off rather than growing forever

_RELATIONSHIP_DEFAULTS = {"affinity": 0, "memory_events": [], "banned": 0, "resolution": "unresolved"}


def get_relationship(telegram_user_id: int, chat_id: int, npc_id: str) -> dict:
    """Returns this player's relationship with an NPC, creating a neutral default row if none exists yet."""
    with get_connection() as conn:
        row = conn.execute(
            "SELECT * FROM npc_relationships WHERE telegram_user_id = ? AND chat_id = ? AND npc_id = ?",
            (telegram_user_id, chat_id, npc_id),
        ).fetchone()
        if row is None:
            conn.execute(
                "INSERT INTO npc_relationships (telegram_user_id, chat_id, npc_id) VALUES (?, ?, ?)",
                (telegram_user_id, chat_id, npc_id),
            )
            return {"telegram_user_id": telegram_user_id, "chat_id": chat_id, "npc_id": npc_id, **_RELATIONSHIP_DEFAULTS}
    d = dict(row)
    d["memory_events"] = json.loads(d["memory_events"])
    return d


def adjust_affinity(telegram_user_id: int, chat_id: int, npc_id: str, delta: int, event: str | None = None) -> dict:
    """
    Adjusts rapport with an NPC (clamped to -100..100) and, if `event` is
    given, appends a short factual memory (e.g. "was caught stealing")
    that future conversations/ambient lines can be grounded in — real,
    persistent consequence, not just a transient combat-log line.
    """
    relationship = get_relationship(telegram_user_id, chat_id, npc_id)
    new_affinity = max(-100, min(100, relationship["affinity"] + delta))
    memory_events = relationship["memory_events"]
    if event:
        memory_events = memory_events + [event]
        memory_events = memory_events[-MAX_MEMORY_EVENTS:]

    with get_connection() as conn:
        conn.execute(
            """
            UPDATE npc_relationships SET affinity = ?, memory_events = ?
            WHERE telegram_user_id = ? AND chat_id = ? AND npc_id = ?
            """,
            (new_affinity, json.dumps(memory_events), telegram_user_id, chat_id, npc_id),
        )
    return get_relationship(telegram_user_id, chat_id, npc_id)


def set_banned_by_npc(telegram_user_id: int, chat_id: int, npc_id: str, banned: bool = True) -> dict:
    """A real, persistent consequence — e.g. a shopkeeper refusing to trade with a caught thief."""
    get_relationship(telegram_user_id, chat_id, npc_id)  # ensure the row exists
    with get_connection() as conn:
        conn.execute(
            "UPDATE npc_relationships SET banned = ? WHERE telegram_user_id = ? AND chat_id = ? AND npc_id = ?",
            (int(banned), telegram_user_id, chat_id, npc_id),
        )
    return get_relationship(telegram_user_id, chat_id, npc_id)


def is_banned_by_npc(telegram_user_id: int, chat_id: int, npc_id: str) -> bool:
    return bool(get_relationship(telegram_user_id, chat_id, npc_id)["banned"])


def resolve_companion(telegram_user_id: int, chat_id: int, npc_id: str, state: str) -> dict:
    """
    Writes a companion's final resolution state once, at the moment their
    quest chain's final stage completes -- e.g. "resolved_loyal",
    "resolved_distant", "resolved_estranged" depending on which trust
    band `affinity` was in at that checkpoint. Reuses the existing
    npc_relationships row (a companion is just an NPC a player has a real
    relationship with) rather than a new table -- see the full-storyline
    plan's "extend, don't invent" design.
    """
    get_relationship(telegram_user_id, chat_id, npc_id)  # ensure the row exists
    with get_connection() as conn:
        conn.execute(
            "UPDATE npc_relationships SET resolution = ? WHERE telegram_user_id = ? AND chat_id = ? AND npc_id = ?",
            (state, telegram_user_id, chat_id, npc_id),
        )
    return get_relationship(telegram_user_id, chat_id, npc_id)


def get_companion_resolution(telegram_user_id: int, chat_id: int, npc_id: str) -> str:
    return get_relationship(telegram_user_id, chat_id, npc_id)["resolution"]


# ---------------------------------------------------------------------
# Faction standing — how a faction as a whole regards a player, driven
# by what they actually do to its members (see bot.py's combat/theft
# hooks), not anything invented by narration.
# ---------------------------------------------------------------------

def get_faction_standing(telegram_user_id: int, chat_id: int, faction_id: str, starting_standing: int = 0) -> int:
    with get_connection() as conn:
        row = conn.execute(
            "SELECT standing FROM faction_standing WHERE telegram_user_id = ? AND chat_id = ? AND faction_id = ?",
            (telegram_user_id, chat_id, faction_id),
        ).fetchone()
        if row is None:
            conn.execute(
                "INSERT INTO faction_standing (telegram_user_id, chat_id, faction_id, standing) VALUES (?, ?, ?, ?)",
                (telegram_user_id, chat_id, faction_id, starting_standing),
            )
            return starting_standing
    return row["standing"]


def adjust_faction_standing(telegram_user_id: int, chat_id: int, faction_id: str, delta: int,
                             starting_standing: int = 0) -> int:
    current = get_faction_standing(telegram_user_id, chat_id, faction_id, starting_standing)
    new_standing = max(-100, min(100, current + delta))
    with get_connection() as conn:
        conn.execute(
            "UPDATE faction_standing SET standing = ? WHERE telegram_user_id = ? AND chat_id = ? AND faction_id = ?",
            (new_standing, telegram_user_id, chat_id, faction_id),
        )
    return new_standing


# ---------------------------------------------------------------------
# Player-run marketplace (task #79) — a real listing at a fixed price,
# never a bid/auction. Global, not location-scoped, for this first
# version.
# ---------------------------------------------------------------------

def create_market_listing(seller_id: int, chat_id: int, seller_name: str, item_id: str, quantity: int, price: int) -> int:
    with get_connection() as conn:
        cur = conn.execute(
            "INSERT INTO market_listings (seller_id, chat_id, seller_name, item_id, quantity, price, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (seller_id, chat_id, seller_name, item_id, quantity, price, datetime.now(timezone.utc).isoformat()),
        )
        return cur.lastrowid


def get_market_listings(chat_id: int) -> list[dict]:
    with get_connection() as conn:
        rows = conn.execute(
            "SELECT * FROM market_listings WHERE chat_id = ? ORDER BY listing_id", (chat_id,)
        ).fetchall()
    return [dict(r) for r in rows]


def get_market_listing(listing_id: int, chat_id: int) -> dict | None:
    with get_connection() as conn:
        row = conn.execute(
            "SELECT * FROM market_listings WHERE listing_id = ? AND chat_id = ?", (listing_id, chat_id)
        ).fetchone()
    return dict(row) if row else None


def remove_market_listing(listing_id: int, chat_id: int) -> None:
    with get_connection() as conn:
        conn.execute("DELETE FROM market_listings WHERE listing_id = ? AND chat_id = ?", (listing_id, chat_id))


# ---------------------------------------------------------------------
# Proficiency growth — "the more you do something, the better you get."
# See rules/proficiency.py for the deterministic bonus formula; this
# just tracks the raw per-ability use counts the formula is applied to.
# ---------------------------------------------------------------------

def get_skill_uses(telegram_user_id: int, chat_id: int) -> dict:
    character = get_character(telegram_user_id, chat_id)
    return character["skill_uses"] if character else {}


def record_skill_use(telegram_user_id: int, chat_id: int, ability: str) -> int:
    """Increments and returns the new use count for this ability."""
    character = get_character(telegram_user_id, chat_id)
    if character is None:
        return 0
    uses = character["skill_uses"]
    uses[ability] = uses.get(ability, 0) + 1
    update_character(telegram_user_id, chat_id, skill_uses=uses)
    return uses[ability]


# ---------------------------------------------------------------------
# feature_uses: same shape/rest-gating as skill_uses, but for limited-use
# class/racial features (Second Wind, Rage, Bardic Inspiration, Lay on
# Hands, Relentless Endurance) instead of ability practice. Reset to {}
# by _apply_natural_healing (bot.py) only on a FULL rest completion --
# these are real "per long rest" resources, not gradually recovered like
# HP/spell slots, so they either reset all at once or not at all.
# ---------------------------------------------------------------------

def get_feature_uses(telegram_user_id: int, chat_id: int, feature_id: str) -> int:
    character = get_character(telegram_user_id, chat_id)
    return character["feature_uses"].get(feature_id, 0) if character else 0


def use_feature(telegram_user_id: int, chat_id: int, feature_id: str) -> int:
    """Increments and returns the new use count for this feature this rest cycle."""
    character = get_character(telegram_user_id, chat_id)
    if character is None:
        return 0
    uses = character["feature_uses"]
    uses[feature_id] = uses.get(feature_id, 0) + 1
    update_character(telegram_user_id, chat_id, feature_uses=uses)
    return uses[feature_id]


def reset_feature_uses(telegram_user_id: int, chat_id: int) -> None:
    """Clears all limited-use feature counters -- called on a full rest."""
    update_character(telegram_user_id, chat_id, feature_uses={})


# ---------------------------------------------------------------------
# Inactivity — "resting until next session," either explicit (a player
# says so) or automatic (5 real-world minutes of chat silence). An
# inactive character can still be targeted with support spells by
# active party members; it just can't act itself until it's reactivated
# (which happens automatically the next time its owner sends any real
# message — see bot.py's adventure_master_handler).
# ---------------------------------------------------------------------

def touch_last_active(telegram_user_id: int, chat_id: int) -> None:
    """Records 'this player did something just now' — real players only in practice."""
    with get_connection() as conn:
        character_id = _active_character_id(telegram_user_id, chat_id, conn)
        if character_id is None:
            return
        conn.execute(
            "UPDATE characters SET last_active_at = ? WHERE character_id = ?",
            (datetime.now(timezone.utc).isoformat(), character_id),
        )


def update_login_streak(telegram_user_id: int, chat_id: int) -> tuple[int, bool] | None:
    """
    Login streak (task #78): a real consecutive-real-calendar-day
    counter, checked from the SAME real-activity checkpoint as
    touch_last_active. Returns (current_streak_days, is_new_day) --
    is_new_day is False if this player already had today counted (so
    the caller doesn't re-announce/re-reward on every message), or
    None entirely for AI characters (companions/autonomous party),
    which don't have a real login of their own.
    """
    character = get_character(telegram_user_id, chat_id)
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
    update_character(telegram_user_id, chat_id, login_streak_days=streak, last_login_date=today.isoformat())
    return streak, True


def claim_guild_quest_if_unclaimed_today(telegram_user_id: int, chat_id: int, reward_gold: int, reward_xp: int) -> bool:
    """
    Guild quests (task #77): same real-day-gating idea as
    update_login_streak, but a flat "claimed or not today" flag rather
    than a growing streak, since the guild quest's objective doesn't
    accumulate -- it's won once per real day. Returns True (and applies
    the reward) only the first time this is called for a given
    character on a given real day; every later call that same day is a
    silent no-op, so combat victories after the first won't double-pay.
    """
    character = get_character(telegram_user_id, chat_id)
    if character is None:
        return False
    today = datetime.now(timezone.utc).date().isoformat()
    if character.get("last_guild_quest_date") == today:
        return False
    update_character(
        telegram_user_id, chat_id,
        last_guild_quest_date=today,
        gold=character["gold"] + reward_gold,
    )
    add_xp(telegram_user_id, chat_id, reward_xp)
    return True


def mark_inactive(telegram_user_id: int, chat_id: int) -> dict | None:
    return update_character(telegram_user_id, chat_id, is_inactive=1)


def mark_active(telegram_user_id: int, chat_id: int) -> dict | None:
    return update_character(telegram_user_id, chat_id, is_inactive=0)


def set_do_not_disturb(telegram_user_id: int, chat_id: int, enabled: bool) -> dict | None:
    return update_character(telegram_user_id, chat_id, do_not_disturb=1 if enabled else 0)


def set_status_note(telegram_user_id: int, chat_id: int, note: str | None) -> dict | None:
    return update_character(telegram_user_id, chat_id, status_note=note)


def unlock_achievement(telegram_user_id: int, chat_id: int, achievement_id: str) -> dict | None:
    """Idempotent -- adding an already-unlocked achievement again is a no-op."""
    character = get_character(telegram_user_id, chat_id)
    if character is None:
        return None
    if achievement_id in character["achievements"]:
        return character
    character["achievements"].append(achievement_id)
    return update_character(telegram_user_id, chat_id, achievements=character["achievements"])


def set_active_title(telegram_user_id: int, chat_id: int, title: str | None) -> dict | None:
    return update_character(telegram_user_id, chat_id, active_title=title)


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


# --- Multi-tenant topic routing (Phase 3, 2026-08-02) ---

def register_chat(chat_id: int, title: str | None, added_by_user_id: int) -> None:
    """Records that a group has run /set_topic at least once. Safe to call repeatedly -- just refreshes the title."""
    with get_connection() as conn:
        conn.execute(
            "INSERT INTO chats (chat_id, title, added_by_user_id, created_at) VALUES (?, ?, ?, ?) "
            "ON CONFLICT(chat_id) DO UPDATE SET title = excluded.title",
            (chat_id, title, added_by_user_id, datetime.now(timezone.utc).isoformat()),
        )


def get_all_chat_ids() -> list[int]:
    """
    Every chat_id the background loop (bot.py's _idle_inactivity_loop
    and sub-functions) should run its per-cycle checks against --
    Phase 4b's fix for the "which chats exist" gap (see
    sequential-tinkering-quokka.md). Union of every chat that has ever
    run /set_topic (the `chats` table) plus this bot's own home chat
    (config.TELEGRAM_CHAT_ID), which must always be included even if
    it never ran /set_topic itself -- it's the one chat guaranteed to
    always exist.
    """
    with get_connection() as conn:
        rows = conn.execute("SELECT chat_id FROM chats").fetchall()
    chat_ids = {row["chat_id"] for row in rows}
    home_chat_id = getattr(config, "TELEGRAM_CHAT_ID", None)
    if home_chat_id is not None:
        chat_ids.add(home_chat_id)
    return sorted(chat_ids)


def set_chat_topic_id(chat_id: int, topic_name: str, message_thread_id: int | None) -> None:
    with get_connection() as conn:
        conn.execute(
            "INSERT INTO chat_topic_config (chat_id, topic_name, message_thread_id) VALUES (?, ?, ?) "
            "ON CONFLICT(chat_id, topic_name) DO UPDATE SET message_thread_id = excluded.message_thread_id",
            (chat_id, topic_name.lower(), message_thread_id),
        )


def get_chat_topic_id(chat_id: int, topic_name: str) -> int | None:
    """None means "no per-tenant row" -- callers (topics.py) fall back to the home-group constants in that case."""
    with get_connection() as conn:
        row = conn.execute(
            "SELECT message_thread_id FROM chat_topic_config WHERE chat_id = ? AND topic_name = ?",
            (chat_id, topic_name.lower()),
        ).fetchone()
    return row["message_thread_id"] if row else None


def get_chat_topic_name_for_thread(chat_id: int, message_thread_id: int) -> str | None:
    """Reverse lookup for this one chat's own configured topics -- used by topics.get_topic_name."""
    with get_connection() as conn:
        row = conn.execute(
            "SELECT topic_name FROM chat_topic_config WHERE chat_id = ? AND message_thread_id = ?",
            (chat_id, message_thread_id),
        ).fetchone()
    return row["topic_name"] if row else None


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


def get_active_board_quests(location_id: str, chat_id: int, day_key: str, tier: str = "daily") -> list[dict]:
    """All of this period's board quests for this location (accepted or not), oldest first."""
    with get_connection() as conn:
        rows = conn.execute(
            "SELECT * FROM board_quests WHERE location_id = ? AND chat_id = ? AND day_key = ? AND tier = ? "
            "ORDER BY board_quest_id ASC",
            (location_id, chat_id, day_key, tier),
        ).fetchall()
    return [_board_quest_row_to_dict(r) for r in rows]


def get_active_board_quest(location_id: str, chat_id: int, day_key: str, tier: str = "daily") -> dict | None:
    """The single most-recent board quest for this location this period, if any (accepted or not)."""
    with get_connection() as conn:
        row = conn.execute(
            "SELECT * FROM board_quests WHERE location_id = ? AND chat_id = ? AND day_key = ? AND tier = ? "
            "ORDER BY board_quest_id DESC LIMIT 1",
            (location_id, chat_id, day_key, tier),
        ).fetchone()
    return _board_quest_row_to_dict(row) if row else None


def create_board_quest(location_id: str, chat_id: int, day_key: str, title: str, description: str,
                        giver_npc: str | None, objective_type: str, objective_target: str,
                        objective_count: int, reward_xp: int, reward_gold: int,
                        tier: str = "daily", reward_affinity: int = 0) -> dict:
    with get_connection() as conn:
        cur = conn.execute(
            """
            INSERT INTO board_quests (
                location_id, chat_id, day_key, title, description, giver_npc,
                objective_type, objective_target, objective_count,
                reward_xp, reward_gold, generated_at, tier, reward_affinity
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (location_id, chat_id, day_key, title, description, giver_npc,
             objective_type, objective_target, objective_count,
             reward_xp, reward_gold, datetime.now(timezone.utc).isoformat(), tier, reward_affinity),
        )
        board_quest_id = cur.lastrowid
        row = conn.execute(
            "SELECT * FROM board_quests WHERE board_quest_id = ?", (board_quest_id,)
        ).fetchone()
    return _board_quest_row_to_dict(row)


BOARD_QUEST_TIER_EXPIRY_HOURS = {"daily": 24, "weekly": 7 * 24, "monthly": 30 * 24}


def accept_board_quest(board_quest_id: int, telegram_user_id: int, chat_id: int) -> dict | None:
    """
    Expiry window scales with the quest's own tier (2026-07-25,
    per Coffee's weekly/monthly missions ask) -- 24h for a daily bounty,
    same as always, but a real 7 days for a weekly one and 30 for a
    monthly one, so accepting a bigger mission doesn't hand back a
    24h-or-lose-it window that was only ever sized for the daily kind.

    Real multi-tenant completeness fix (2026-08-08): board_quest_id is
    already a globally-unique AUTOINCREMENT PK, so this was never a
    real cross-tenant leak -- but every sibling board-quest function
    (get_accepted_board_quests_for_user, get_accepted_board_quests_at_
    location, etc.) already takes chat_id, and this one didn't. Added
    for signature consistency and as a real defense-in-depth check: if
    a caller ever passes a board_quest_id that doesn't actually belong
    to chat_id (a bug elsewhere), this now fails to update instead of
    silently accepting a different tenant's quest.
    """
    now = datetime.now(timezone.utc)
    with get_connection() as conn:
        existing = conn.execute(
            "SELECT tier FROM board_quests WHERE board_quest_id = ? AND chat_id = ?", (board_quest_id, chat_id)
        ).fetchone()
        tier = existing["tier"] if existing else "daily"
        expiry_hours = BOARD_QUEST_TIER_EXPIRY_HOURS.get(tier, 24)
        expires = now.timestamp() + (expiry_hours * 3600)
        conn.execute(
            "UPDATE board_quests SET accepted_by = ?, accepted_at = ?, expires_at = ? "
            "WHERE board_quest_id = ? AND chat_id = ? AND accepted_by IS NULL",
            (telegram_user_id, now.isoformat(), datetime.fromtimestamp(expires, timezone.utc).isoformat(),
             board_quest_id, chat_id),
        )
        row = conn.execute(
            "SELECT * FROM board_quests WHERE board_quest_id = ? AND chat_id = ?", (board_quest_id, chat_id)
        ).fetchone()
    return _board_quest_row_to_dict(row) if row else None


def cancel_board_quest(board_quest_id: int, telegram_user_id: int, chat_id: int) -> dict | None:
    """
    Real request (2026-08-22, per Coffee: "in the quests menu when we
    click on a quest we can cancel the quest?... doesnt decline the
    quest it jus allows them to do it at another time"). Same reset
    shape expire_stale_board_quests already uses for a quest that aged
    out on its own (accepted_by/accepted_at/expires_at cleared,
    progress_count back to 0) -- this is just the player choosing that
    outcome deliberately, with a real confirmation first (see bot.py's
    _do_confirm_cancel_quest), instead of waiting for the clock.
    Scoped to accepted_by = telegram_user_id so a stale/guessed
    board_quest_id can never cancel someone else's accepted quest.
    """
    with get_connection() as conn:
        conn.execute(
            "UPDATE board_quests SET accepted_by = NULL, accepted_at = NULL, expires_at = NULL, "
            "progress_count = 0 WHERE board_quest_id = ? AND chat_id = ? AND accepted_by = ? AND completed_at IS NULL",
            (board_quest_id, chat_id, telegram_user_id),
        )
        row = conn.execute(
            "SELECT * FROM board_quests WHERE board_quest_id = ? AND chat_id = ?", (board_quest_id, chat_id)
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


def increment_board_quests_completed(telegram_user_id: int, chat_id: int) -> None:
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
            "UPDATE characters SET board_quests_completed = board_quests_completed + 1 "
            "WHERE telegram_user_id = ? AND chat_id = ?",
            (telegram_user_id, chat_id),
        )


def get_accepted_board_quests_for_user(telegram_user_id: int, chat_id: int) -> list[dict]:
    """A player's currently-accepted, not-yet-completed board quests (any location), scoped to one tenant chat."""
    with get_connection() as conn:
        rows = conn.execute(
            "SELECT * FROM board_quests WHERE accepted_by = ? AND chat_id = ? AND completed_at IS NULL",
            (telegram_user_id, chat_id),
        ).fetchall()
    return [_board_quest_row_to_dict(r) for r in rows]


def get_accepted_board_quests_at_location(location_id: str, chat_id: int) -> list[dict]:
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
            "SELECT * FROM board_quests WHERE location_id = ? AND chat_id = ? "
            "AND accepted_by IS NOT NULL AND completed_at IS NULL",
            (location_id, chat_id),
        ).fetchall()
    return [_board_quest_row_to_dict(r) for r in rows]


def expire_stale_board_quests() -> list[dict]:
    """
    Releases any accepted-but-not-completed board quest whose 24h
    window has passed, back to the board for someone else. Returns the
    rows that were just expired (for narration/logging), not deleted --
    the location simply gets a fresh one generated next time it's checked.

    Real live bug (2026-08-20, Coffee, screenshot: "This didnt work" --
    replying "Keep it and collect the reward" to a real "Ready to
    decide" prompt got "You don't have a decision to make right now").
    A plain quest auto-turns in and sets completed_at the moment its
    progress reaches objective_count, so this query already correctly
    left those alone. A BRANCHING quest deliberately does NOT set
    completed_at at that point -- it waits at full progress for the
    player's choice, which is the entire point of "Ready to decide."
    This query never accounted for that: a player who finished every
    objective but hadn't replied yet still had their real, earned
    quest silently expired and reset to progress_count=0 by this same
    24h-from-ACCEPTANCE window, discarding real work with no warning.
    progress_count < objective_count now excludes any quest that's
    already fully earned and just waiting on a choice.
    """
    now_iso = datetime.now(timezone.utc).isoformat()
    with get_connection() as conn:
        rows = conn.execute(
            "SELECT * FROM board_quests WHERE accepted_by IS NOT NULL AND completed_at IS NULL "
            "AND expires_at IS NOT NULL AND expires_at < ? AND progress_count < objective_count",
            (now_iso,),
        ).fetchall()
        expired = [_board_quest_row_to_dict(r) for r in rows]
        if expired:
            conn.execute(
                "UPDATE board_quests SET accepted_by = NULL, accepted_at = NULL, expires_at = NULL, "
                "progress_count = 0 WHERE accepted_by IS NOT NULL AND completed_at IS NULL "
                "AND expires_at IS NOT NULL AND expires_at < ? AND progress_count < objective_count",
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
    """
    Real live bug (2026-08-06, Coffee: "Why are all our characters
    getting dropped from party" -- confirmed his party was already
    established, no one had left it). Root cause: this used to JOIN
    against active_characters, so a real party member whose owner
    simply wasn't currently ON that exact character (switched to a
    different one of their own, or -- as here -- created a brand new
    one, which immediately becomes their new active slot) silently
    vanished from EVERY party lookup that called this -- the party
    status screen, the bench/unbench roster (whose own comment already
    said "real party_id membership only, regardless of ... this is
    planning ahead" -- directly contradicted by the JOIN it was
    actually using), XP-sharing to absent members, "is this party all
    AI" detection, human-leader lookup, and more. This is the exact
    same root cause already found and fixed for the shrine's revival
    flow on 2026-07-24 (get_party_members_by_id_including_inactive_
    slots below) -- that fix was only ever applied to the 2 shrine call
    sites, leaving the other ~17 real callers still silently dropping
    any party member who wasn't their owner's current active character.
    Audited every one of those callers (2026-08-06): none of them
    actually wants "only if this is literally the active slot right
    now" -- the ones that need real presence/availability semantics
    already filter explicitly via is_benched/is_inactive/is_dead/
    current_location, which is the real, intentional signal for that;
    the JOIN was just silently double-filtering on top, wrongly. Now
    identical to the "_including_inactive_slots" query below, which
    stays as a real function (not just an alias) since several callers
    already name it explicitly and its docstring documents the
    original 2026-07-24 incident.
    """
    with get_connection() as conn:
        rows = conn.execute(
            "SELECT * FROM characters WHERE party_id = ? AND is_deleted = 0",
            (party_id,),
        ).fetchall()
    return [_row_to_dict(r) for r in rows]


def get_party_members_by_id_including_inactive_slots(party_id: int) -> list[dict]:
    """
    Real bug caught live (2026-07-24, Coffee: "she's currently dead"
    but the shrine kept saying "there's no one to bring back"):
    get_party_members_by_id used to have an active_characters join that
    meant a dead real player who has since SWITCHED to a different
    character (per CLAUDE.md's own documented behavior -- "the player
    can switch to another character of theirs in the meantime" while
    dead) silently vanished from every party lookup entirely, since her
    dead character was no longer her account's active slot -- a
    different, alive character of hers was. Confirmed live: Laurienna's
    real owner had switched to a character named Charvenna, so
    Laurienna (is_dead=1, genuinely sitting in the party) never showed
    up in any active-characters-gated query at all. Scoped by party_id
    alone, matching every OTHER member sharing this party -- exactly
    what revival features (shrine offering, Tent/Cabin/House) need,
    since a permanently-dead character is precisely the kind of
    "inactive slot" real player death leaves behind. As of 2026-08-06
    this is identical to get_party_members_by_id itself (see that
    function's own docstring for why the join was removed there too,
    not just here) -- kept as its own real function rather than
    collapsed into an alias since several real callers already name it
    explicitly.
    """
    with get_connection() as conn:
        rows = conn.execute(
            "SELECT * FROM characters WHERE party_id = ? AND is_deleted = 0",
            (party_id,),
        ).fetchall()
    return [_row_to_dict(r) for r in rows]


def list_all_active_real_players(chat_id: int) -> list[dict]:
    """
    Every real (non-AI) player's currently active character, across the
    whole game -- not scoped to one party or session. Used by task
    #118's text_mention wiring (bot.py's _safe_send): a narration line
    can name a real player who isn't in the current combat/party at all
    (e.g. an achievement, a guild-quest announcement), so the candidate
    list for "does this message name a real player" has to be every
    real player, not just the ones already in scope. Scoped to one
    tenant chat (Phase 4e, 2026-08-06) -- a narration in one tenant chat
    must never text-mention a different tenant's player by name.
    """
    with get_connection() as conn:
        rows = conn.execute(
            "SELECT c.* FROM characters c JOIN active_characters a ON a.character_id = c.character_id "
            "WHERE c.is_ai = 0 AND c.is_deleted = 0 AND c.chat_id = ?",
            (chat_id,),
        ).fetchall()
    return [_row_to_dict(r) for r in rows]


def create_party(telegram_user_id: int, chat_id: int) -> int:
    """Creates a new party and immediately puts the creator's active character in it."""
    with get_connection() as conn:
        cur = conn.execute(
            "INSERT INTO parties (created_by, chat_id, created_at) VALUES (?, ?, ?)",
            (telegram_user_id, chat_id, datetime.now(timezone.utc).isoformat()),
        )
        party_id = cur.lastrowid
        character_id = _active_character_id(telegram_user_id, chat_id, conn)
        conn.execute("UPDATE characters SET party_id = ? WHERE character_id = ?", (party_id, character_id))
    return party_id


def set_pending_party_invite(telegram_user_id: int, chat_id: int, party_id: int) -> None:
    with get_connection() as conn:
        character_id = _active_character_id(telegram_user_id, chat_id, conn)
        conn.execute(
            "UPDATE characters SET pending_party_invite = ? WHERE character_id = ?", (party_id, character_id)
        )


def accept_party_invite(telegram_user_id: int, chat_id: int) -> tuple[bool, str]:
    """Joins the party the character was invited to, if there's room. Returns (success, message)."""
    character = get_character(telegram_user_id, chat_id)
    if character is None:
        return False, "You don't have a character yet!"
    party_id = character.get("pending_party_invite")
    if not party_id:
        return False, "You don't have a pending party invite."
    if get_party_size(party_id) >= PARTY_MAX_MEMBERS:
        return False, f"That party is already full ({PARTY_MAX_MEMBERS} members)."
    with get_connection() as conn:
        character_id = _active_character_id(telegram_user_id, chat_id, conn)
        conn.execute(
            "UPDATE characters SET party_id = ?, pending_party_invite = NULL WHERE character_id = ?",
            (party_id, character_id),
        )
    return True, "Joined the party!"


def leave_party(telegram_user_id: int, chat_id: int) -> bool:
    """Returns False if the character wasn't in a party to begin with."""
    character = get_character(telegram_user_id, chat_id)
    if character is None or not character.get("party_id"):
        return False
    with get_connection() as conn:
        character_id = _active_character_id(telegram_user_id, chat_id, conn)
        conn.execute("UPDATE characters SET party_id = NULL WHERE character_id = ?", (character_id,))
    return True


def add_ai_companion_to_party(telegram_user_id: int, chat_id: int, party_id: int) -> None:
    """AI companions have no real turn to 'accept' with -- they join immediately when invited."""
    with get_connection() as conn:
        conn.execute(
            "UPDATE characters SET party_id = ? WHERE telegram_user_id = ? AND chat_id = ?",
            (party_id, telegram_user_id, chat_id),
        )


# --- The Labyrinth (see CREATE_LABYRINTH_RUNS_TABLE's own comment for
# why this is a table, not a character column) ---

def _labyrinth_run_row_to_dict(row: sqlite3.Row) -> dict:
    d = dict(row)
    d["rooms"] = json.loads(d.pop("rooms_json"))
    d["visited_floors"] = json.loads(d.pop("visited_floors_json", "[]") or "[]")
    return d


def get_labyrinth_run(chat_id: int, party_key: str) -> dict | None:
    with get_connection() as conn:
        row = conn.execute(
            "SELECT * FROM labyrinth_runs WHERE chat_id = ? AND party_key = ?", (chat_id, party_key),
        ).fetchone()
    return _labyrinth_run_row_to_dict(row) if row else None


def create_labyrinth_run(
    chat_id: int, party_key: str, floor: int, seed: int, current_room_id: str, rooms: dict,
    modifier: str | None = None, visited_floors: list[int] | None = None,
) -> dict:
    now = datetime.now(timezone.utc).isoformat()
    with get_connection() as conn:
        conn.execute(
            "INSERT INTO labyrinth_runs (chat_id, party_key, floor, seed, current_room_id, rooms_json, modifier, visited_floors_json, entered_at, updated_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (chat_id, party_key, floor, seed, current_room_id, json.dumps(rooms), modifier, json.dumps(visited_floors or [floor]), now, now),
        )
    return get_labyrinth_run(chat_id, party_key)


def update_labyrinth_run(chat_id: int, party_key: str, **fields) -> dict | None:
    if not fields:
        return get_labyrinth_run(chat_id, party_key)
    if "rooms" in fields:
        fields["rooms_json"] = json.dumps(fields.pop("rooms"))
    if "visited_floors" in fields:
        fields["visited_floors_json"] = json.dumps(fields.pop("visited_floors"))
    fields["updated_at"] = datetime.now(timezone.utc).isoformat()
    columns = ", ".join(f"{k} = ?" for k in fields)
    values = list(fields.values())
    with get_connection() as conn:
        conn.execute(
            f"UPDATE labyrinth_runs SET {columns} WHERE chat_id = ? AND party_key = ?",
            values + [chat_id, party_key],
        )
    return get_labyrinth_run(chat_id, party_key)


def delete_labyrinth_run(chat_id: int, party_key: str) -> None:
    with get_connection() as conn:
        conn.execute("DELETE FROM labyrinth_runs WHERE chat_id = ? AND party_key = ?", (chat_id, party_key))


def log_labyrinth_seed(
    chat_id: int, party_key: str, segment: int, seed: int, theme: str | None, party_names: list[str],
) -> None:
    """
    Real live incident (2026-09-04, Coffee: "we left the labyrinth and
    when i went to return it reset?" / "make a seed logging system so
    if that happen u can reload the seed"). Append-only, never deleted
    (see CREATE_LABYRINTH_SEED_LOG_TABLE's own comment) -- called once
    per real segment generation, both a brand-new run and each later
    descend, so a seed always has a durable record even after
    labyrinth_runs itself is long gone.
    """
    with get_connection() as conn:
        conn.execute(
            "INSERT INTO labyrinth_seed_log (chat_id, party_key, segment, seed, theme, party_names, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (chat_id, party_key, segment, seed, theme, json.dumps(party_names), datetime.now(timezone.utc).isoformat()),
        )


def get_labyrinth_seed_log(chat_id: int, party_key: str, limit: int = 10) -> list[dict]:
    """Most recent first -- real history a player can browse to find a past seed again."""
    with get_connection() as conn:
        rows = conn.execute(
            "SELECT * FROM labyrinth_seed_log WHERE chat_id = ? AND party_key = ? ORDER BY id DESC LIMIT ?",
            (chat_id, party_key, limit),
        ).fetchall()
    entries = []
    for row in rows:
        d = dict(row)
        d["party_names"] = json.loads(d["party_names"])
        entries.append(d)
    return entries


def bump_labyrinth_best_floor(telegram_user_id: int, chat_id: int, floor: int) -> None:
    """Permanent, never-decreasing bragging-rights stat -- only writes if `floor` is a new personal best."""
    with get_connection() as conn:
        character_id = _active_character_id(telegram_user_id, chat_id, conn)
        if character_id is None:
            return
        conn.execute(
            "UPDATE characters SET labyrinth_best_floor = ? WHERE character_id = ? AND labyrinth_best_floor < ?",
            (floor, character_id, floor),
        )


def bump_labyrinth_best_floor_by_id(character_id: int, floor: int) -> None:
    """
    Same as bump_labyrinth_best_floor, but targets an exact character_id
    directly instead of resolving through telegram_user_id's currently-
    active character. Real bug found live (2026-09-04, Coffee: "we left
    the labyrinth and when i went to return it reset?"): a party can
    contain two characters owned by the same real person (exactly
    Coffee's and Sugar's own real setup), and the telegram_user_id-based
    variant always resolves to whichever of them is CURRENTLY active --
    so the non-active party member's own real depth progress silently
    lands on their owner's OTHER character instead, never its own row.
    Same real bug class as update_character_by_id's own documented
    incident (shrine offering revival) and the earlier "Ravenloft"
    stuck-sentinel fix.
    """
    with get_connection() as conn:
        conn.execute(
            "UPDATE characters SET labyrinth_best_floor = ? WHERE character_id = ? AND labyrinth_best_floor < ?",
            (floor, character_id, floor),
        )


# --- Autonomous AI-played party (2026-07-10, per Coffee: a separate,
# self-directed party that plays through the exact same pipeline real
# players use -- distinct from is_ai=1 combat companions, which only
# ever auto-resolve combat turns and never act outside them) ---

def mark_autonomous(telegram_user_id: int, chat_id: int) -> None:
    with get_connection() as conn:
        conn.execute(
            "UPDATE characters SET is_autonomous = 1 WHERE telegram_user_id = ? AND chat_id = ?",
            (telegram_user_id, chat_id),
        )


def get_autonomous_players(chat_id: int) -> list[dict]:
    with get_connection() as conn:
        rows = conn.execute(
            "SELECT * FROM characters WHERE is_autonomous = 1 AND is_deleted = 0 AND chat_id = ? ORDER BY character_id",
            (chat_id,),
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
