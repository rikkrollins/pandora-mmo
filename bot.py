"""
bot.py
Telegram bot entry point for Pandora MMO. Players interact entirely in
natural language in the Adventure topic — no slash commands required
(though a few are kept as optional power-user shortcuts). Free text is
classified by ai/intent_parser.py into a structured action, which is
then resolved by the same deterministic rules engine as before. The
rules engine still never trusts the AI to decide outcomes — only to
route intent and narrate results.
"""
import asyncio
import contextlib
import json
import difflib
import hashlib
import logging
import os
import random
import re
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

import requests

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, MessageEntity, Update, User
from telegram.constants import ChatAction
from telegram.error import RetryAfter, TelegramError
from telegram.ext import (
    Application,
    ApplicationBuilder,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    PicklePersistence,
    filters,
)

import board_quests as board_quests_module
import campaign_loader as cl
import config
import version
import db
import images as images_module
import items as items_module
import sessions
import shop as shop_module
import spells as spells_module
import races as races_module
import class_features as class_features_module
import hybrid_features
import achievements as achievements_module
import world_clock
import moltbook
import topics
from ai.autonomous_player import choose_next_action
from ai.moltbook_agent import decide_social_action
from ai.dev_agent import answer_dev_question
from ai.dm_agent import (
    narrate_action, narrate_welcome, narrate_skill_check, narrate_hourly_update,
    narrate_examine, narrate_branching_choice_outcome, narrate_boss_decision,
    narrate_story_so_far, narrate_chapter_climax, narrate_arc_opening,
)
from ai.intent_parser import parse_intents
from ai.npc_agent import register_npc, talk_to_npc, generate_ambient_line, _NPCS
from ai.support_agent import answer_support_question
from ai.text_cleanup import to_speakable_text
from ai import tts_piper, stt_groq
from guilds import GUILDS, eligible_for_guild, GUILD_QUESTS
from models import (
    VALID_CLASSES,
    VALID_RACES,
    STARTING_EQUIPMENT,
    STARTING_GOLD,
    BASE_ARMOR_CLASS,
)
from rules.combat import resolve_attack, resolve_death_save, UNDEAD_MONSTER_KEYS
from rules.crafting import RECIPES, get_recipe, has_materials, resolve_craft
from rules.dice import roll, roll_damage, ability_modifier, roll_ability_check, roll_d20
from rules.item_generator import generate_item
from rules.leveling import (
    CLASS_HIT_DICE, scaled_enemy_count, breath_weapon_dice_count,
    CLASS_PRIMARY_ABILITY, CLASS_SAVE_PROFICIENCIES, is_proficient_in_skill,
    skill_check_proficiency_bonus, wild_shape_temp_hp, XP_THRESHOLDS,
    MAX_LEVEL, ability_score_cap, xp_gain_multiplier, hybrid_tier, HYBRID_MAX_TIER,
)
from rules.proficiency import practiced_bonus, MAX_PRACTICE_BONUS

logging.basicConfig(
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger("pandora_mmo")

ACTIVE_CAMPAIGN_ID = config.ACTIVE_CAMPAIGN
# Task #56: previously hardcoded to "default" with no way to switch
# without editing code. Fails loudly and immediately at startup if
# ACTIVE_CAMPAIGN names a folder that doesn't actually exist, rather
# than a cryptic FileNotFoundError deep inside whatever handler first
# touches CAMPAIGN -- listing what IS available makes the fix obvious.
if ACTIVE_CAMPAIGN_ID not in cl.discover_campaigns():
    available = ", ".join(cl.discover_campaigns()) or "(none found)"
    raise RuntimeError(
        f"config.ACTIVE_CAMPAIGN is set to '{ACTIVE_CAMPAIGN_ID}', but no "
        f"campaigns/{ACTIVE_CAMPAIGN_ID}/campaign.json exists. Available campaigns: {available}"
    )
CAMPAIGN = cl.load_campaign(ACTIVE_CAMPAIGN_ID)

DEFAULT_WEAPON = {"ability": "strength", "damage_dice": "1d8", "damage_bonus": 0, "weapon_category": "simple"}


def _weapon_for_attacker(attacker: dict) -> dict:
    """
    Real bug fixed 2026-07-15: every attack in this game used
    DEFAULT_WEAPON unconditionally, regardless of what the attacker
    actually bought/found/equipped -- items.py's weapon damage_dice/
    ability fields existed but nothing ever read them. Monsters and
    hostile NPCs have no "equipped_weapon" field at all, so .get()
    safely falls through to DEFAULT_WEAPON for them unchanged.
    """
    equipped_id = attacker.get("equipped_weapon")
    if equipped_id:
        item = items_module.get_item(equipped_id)
        if item and item.get("type") == "weapon":
            return {
                "ability": item.get("ability", "strength"),
                "damage_dice": item["damage_dice"],
                "damage_bonus": item.get("damage_bonus", 0),
                "weapon_category": item.get("weapon_category", "simple"),
            }
    return DEFAULT_WEAPON


EXTRA_ATTACK_CLASSES = {"fighter", "barbarian", "paladin", "ranger", "monk"}


def _attacks_per_turn(character: dict) -> int:
    """
    Real 5E Extra Attack (2026-07-16 audit): confirmed via grep this was
    completely absent -- not implemented, not even mentioned as flavor
    text anywhere -- despite being arguably THE single biggest DPS
    feature every 5E martial class gets. Boss Multiattack (task #58)
    gave monsters a 2nd attack; players never got the real equivalent.
    Fighter/Barbarian/Paladin/Ranger/Monk get a 2nd attack at level 5;
    Fighter alone gets a 3rd at level 11 and (real 5E, included for
    completeness) a 4th at level 20. Doesn't apply to spellcasting
    classes or Rogue, matching real 5E exactly -- Rogue's damage scaling
    is Sneak Attack (see rules/combat.py), not more attacks.
    """
    char_class = (character.get("char_class") or "").lower()
    if char_class not in EXTRA_ATTACK_CLASSES:
        return 1
    level = character.get("level", 1)
    if char_class == "fighter":
        if level >= 20:
            return 4
        if level >= 11:
            return 3
    return 2 if level >= 5 else 1

# Maps a resource node's gathering ability to the one class whose
# training lets them naturally pick that kind of material out of a
# location's generic description in _do_look (2026-07-12, per Coffee:
# "an Alchemist skillset would be able to see alchemic materials" --
# this game has no Alchemist class, so the two real classes closest to
# that idea for the two ability types resource nodes actually use are
# picked instead: Druid for wisdom-gathered nature materials (herbs,
# flowers), Ranger for strength-gathered mineral/vein materials (ore,
# mined dust) -- a survivalist's eye for a promising rock face). Every
# node is still listed plainly for everyone regardless of class; this
# only adds an extra "you notice this at a glance" flavor line.
RESOURCE_SENSE_CLASS = {"wisdom": "Druid", "strength": "Ranger"}

# npc_id -> faction_id, precomputed once from campaign.json's factions
# catalog — an NPC's actions and a player's actions toward them ripple
# out to how the whole faction regards that player (db.faction_standing).
_NPC_FACTION: dict[str, str] = {
    npc_id: faction_id
    for faction_id, faction in CAMPAIGN.get("factions", {}).items()
    for npc_id in faction.get("member_npcs", [])
}


def _faction_for_npc(npc_id: str) -> str | None:
    return _NPC_FACTION.get(npc_id)


def _faction_starting_standing(faction_id: str) -> int:
    return CAMPAIGN.get("factions", {}).get(faction_id, {}).get("starting_standing", 0)


ALIGNMENT_LAW_CHAOS_LABELS = {1: "Lawful", 0: "Neutral", -1: "Chaotic"}
ALIGNMENT_GOOD_EVIL_LABELS = {1: "Good", 0: "Neutral", -1: "Evil"}


def _alignment_axis_bucket(value: int) -> int:
    if value >= 20:
        return 1
    if value <= -20:
        return -1
    return 0


def _alignment_label(law_chaos: int, good_evil: int) -> str:
    lc = ALIGNMENT_LAW_CHAOS_LABELS[_alignment_axis_bucket(law_chaos)]
    ge = ALIGNMENT_GOOD_EVIL_LABELS[_alignment_axis_bucket(good_evil)]
    return "True Neutral" if lc == "Neutral" and ge == "Neutral" else f"{lc} {ge}"


def _adjust_faction_standing(telegram_user_id: int, faction_id: str, delta: int, starting_standing: int = 0) -> int:
    """
    Task #133, per Coffee: alignment "shifted by actions" -- the one
    real, already-instrumented signal every faction interaction already
    produces is a standing delta plus that faction's own real
    alignment_lean (good/evil/neutral, from campaign.json). Wraps
    db.adjust_faction_standing so every existing call site gets a real
    Good/Evil nudge for free, no new call sites to remember. Helping a
    good-leaning faction (or hurting an evil one) shifts toward Good;
    hurting a good-leaning faction (or helping an evil one) shifts
    toward Evil. Law/Chaos isn't touched here -- no faction currently
    carries a law/chaos lean, so that axis only moves via _do_set_alignment.
    """
    new_standing = db.adjust_faction_standing(telegram_user_id, faction_id, delta, starting_standing)
    lean = CAMPAIGN.get("factions", {}).get(faction_id, {}).get("alignment_lean")
    if lean in ("good", "evil") and delta:
        character = db.get_character(telegram_user_id)
        if character:
            axis_delta = 2 if delta > 0 else -2
            if lean == "evil":
                axis_delta = -axis_delta
            new_ge = max(-100, min(100, character.get("alignment_good_evil", 0) + axis_delta))
            db.update_character(telegram_user_id, alignment_good_evil=new_ge)
    return new_standing

# Lockable chests/doors that have been picked, keyed by their lockable
# "id" from campaign.json. In-memory, not persisted — same deliberate
# simplification as combat conditions: shared world state that resets on
# a bot restart rather than needing a new schema/table for it.
_UNLOCKED: set[str] = set()

# Named hostile NPCs (campaign.json npc ids) already defeated in combat —
# in-memory, same reasoning as _UNLOCKED. A defeated antagonist doesn't
# respawn to ambush the same world again.
_DEFEATED_NPCS: set[str] = set()

# Ambient world-NPC encounters (friendly small-talk, or a hostile NPC
# provoking a real fight) roll this chance on arrival at a location that
# has one — not every single arrival, so it reads as a living world
# rather than a scripted trigger every time.
AMBIENT_NPC_ENCOUNTER_CHANCE = 0.4

# "Resting until next session" — either a player says so explicitly
# ("rest for the night") or 5 real-world minutes of chat silence gets
# them a warning, then 5 more minutes of continued silence (10 total)
# actually commences it automatically. An inactive character can't act,
# but active party members can still target them with support spells
# (healing/buffs/cures — never damage), and their owner can freely
# switch to and play a different character in the meantime (character
# slots already support this with no changes needed here).
# Doubled 2026-07-16 (Coffee's live feedback: "the inactivity came too
# soon") -- was 900/1800 (15/30 min).
IDLE_WARNING_SECONDS = 1800
IDLE_TIMEOUT_SECONDS = 3600
IDLE_CHECK_INTERVAL_SECONDS = 60
SAFE_LOCATION_FALLBACK = "crossroads_tavern"

# Real telegram_user_ids already warned about their current idle
# stretch, so the warning fires once, not every check cycle. In-memory
# only, same reasoning as _UNLOCKED/_DEFEATED_NPCS — cleared the moment
# they act again (see db.touch_last_active's call site).
_IDLE_WARNED: set[int] = set()

# The auto-idle/living-world background loop has no incoming Update to
# read a chat_id from, so it needs one cached here. Seeded from
# config.TELEGRAM_CHAT_ID at import time if set (real value, captured
# 2026-07-09 from a live update — see .env); refreshed from any actual
# incoming Adventure message too. Confirmed live 2026-07-10: without
# this seed, EVERY bot restart silently reset this to None, and since
# the whole living-world system (idle-check, NPC wandering, the
# "meanwhile" heartbeat) short-circuits when it's None, a restart with
# no new message arriving afterward meant that system just never ran —
# looked like it wasn't working at all, when really it just never had
# a chat_id to post to.
_LAST_KNOWN_CHAT_ID: int | None = getattr(config, "TELEGRAM_CHAT_ID", None)

# "/redo" (2026-07-17, per Coffee): lets a group admin/owner replay the
# last real message in a topic, in case it was misclassified or
# mishandled -- keyed by (chat_id, thread_id) so each topic (Adventure,
# Support) tracks its own last message independently. Holds a live
# reference to the ORIGINAL Update/context so redo re-enters the exact
# same code path as if the message had just been received again
# (picking up any fix shipped since) acting as the original player who
# sent it, not the admin/owner who typed /redo. In-memory only, same as
# every other piece of session/ambient state in this file -- resets on
# restart, which is fine since there's nothing to redo right after one.
_LAST_TOPIC_MESSAGE: dict[tuple[int, int], dict] = {}

# ---------------------------------------------------------------------
# Living world — NPCs tagged "can_wander" in campaign.json (currently
# Sarah and Theron) have a real, mutable current location, independent
# of their static campaign.json placement, and can drift between
# connected locations on their own over time. Fixed-role NPCs (the
# innkeeper, the shopkeeper, the bandit) stay exactly where campaign
# data puts them — they have jobs to do. All of this is in-memory only
# (resets on restart), same reasoning as _UNLOCKED/_DEFEATED_NPCS: it's
# ambient world texture, not a game fact anything depends on.
#
# Frequency is deliberately loose, not a tight timer: this runs off the
# EXISTING 60s idle-check loop rather than its own schedule, and both
# wandering and the "meanwhile, while everyone's away" heartbeat roll
# low odds / long minimum gaps specifically so they can never compete
# with a real player's request for the same CPU-bound Ollama instance.
# ---------------------------------------------------------------------
# Physical-dice mode (2026-07-16, per Coffee): a player who's opted in
# (character.manual_dice_enabled, asked at character creation, toggle-
# able any time -- see _do_toggle_manual_dice) rolls their own physical
# dice for the PRIMARY roll of an action (attack roll, skill check) --
# damage and other secondary rolls stay automatic. In-memory only, keyed
# by telegram_user_id, same convention as _NPC_LOCATIONS below: when a
# roll is requested, the pending action is stashed here and the
# function returns without resolving anything yet; adventure_master_
# handler checks this dict on every incoming message BEFORE normal
# intent parsing, and if a number can be read out of the reply, re-
# invokes the stashed action with that forced roll. Deliberately does
# NOT persist across a restart (same as combat sessions) -- a player
# mid-prompt when the bot restarts just gets asked again next action.
#
# Per Coffee (2026-07-19): "give the user 1 minute to roll - if not
# then auto-roll" -- each entry now also carries chat_id/created_at so
# _maybe_auto_roll_pending_dice (run every idle-loop tick, see
# _idle_inactivity_loop) can find and auto-resolve any prompt that's
# gone unanswered for 60+ seconds, same real d20 roll a manual reply
# would have supplied.
_PENDING_DICE_ROLLS: dict[int, dict] = {}
DICE_ROLL_AUTO_TIMEOUT_SECONDS = 60


def _new_pending_roll(kind: str, action_text: str, chat_id: int, **extra) -> dict:
    """Builds a _PENDING_DICE_ROLLS entry -- one place so every call site stamps chat_id/created_at consistently."""
    return {
        "kind": kind, "action_text": action_text, "chat_id": chat_id,
        "created_at": datetime.now(timezone.utc).isoformat(), **extra,
    }

# Real player-driven Ability Score Improvements (2026-07-16, per
# Coffee): a character with pending_asi_points > 0 who says "level up"
# with no ability/auto keyword already in that message gets prompted
# and added here; adventure_master_handler checks this set on the next
# message the same way it checks _PENDING_DICE_ROLLS, so a stray later
# message (normal gameplay) is never misread as a stat choice.
_PENDING_ASI_CHOICE: set[int] = set()

# Task #82, per Coffee: PvP, "safe zones by design, intentional-
# targeting only." A duel challenge is real consent, not an ambush --
# stored here (target_id -> challenger_id) the moment it's issued,
# consumed the moment it's accepted or the target does anything else
# (same in-memory, session-scoped, "one specific next reply" pattern as
# _PENDING_ASI_CHOICE/_PENDING_DICE_ROLLS above).
_PENDING_DUELS: dict[int, int] = {}

# Character description (2026-07-16, per Coffee): "add a description to my
# character" asks what the description should be rather than trying to
# extract free-form biography text out of the SAME message an intent
# classifier just matched a keyword trigger in -- same pending-prompt
# pattern as _PENDING_ASI_CHOICE above, checked on the user's next message
# before normal intent parsing.
_PENDING_DESCRIPTION: set[int] = set()
MAX_CHARACTER_DESCRIPTION_LENGTH = 500

# Character pronouns (2026-07-17, per Coffee: "no gender/pronoun field --
# narration guesses pronouns with no real data, can guess wrong"). Same
# pending-prompt pattern as description above. Narration falls back to
# they/them when unset -- never guesses -- see ai/dm_agent.py.
_PENDING_PRONOUNS: set[int] = set()
MAX_PRONOUNS_LENGTH = 30

# Presence/status note (task #144) -- unlike description/pronouns this
# is always slash-command-driven (/note <text>), so it needs no pending-
# prompt set: a bare /note with no args just shows the current note
# instead of prompting for one.
MAX_STATUS_NOTE_LENGTH = 60

_ABILITY_NAMES = ("strength", "dexterity", "constitution", "intelligence", "wisdom", "charisma")
_ASI_AUTO_WORDS = ("auto", "automatically", "assign", "distribute", "do it for me")

_NPC_LOCATIONS: dict[str, str] = {}
NPC_WANDER_CHANCE_PER_CYCLE = 0.15
WORLD_HEARTBEAT_IDLE_THRESHOLD_SECONDS = 1200  # 20 real minutes with no player activity at all
WORLD_HEARTBEAT_MIN_GAP_SECONDS = 900  # never more than once per ~15 real minutes
_LAST_WORLD_HEARTBEAT_AT: datetime | None = None

# Task #75, per Coffee: "World events: rare world-boss spawns broadcast
# server-wide." Deliberately restricted to is_boss monsters that are
# ordinary wandering encounters, NOT the unique arc-climax bosses
# (the_unspoken, the_waking_ember, the_waiting_shape) -- those are
# quest-bound story beats, not something that should randomly appear
# mid-story or be trivialized into a repeatable farm target. Extend
# this list only with future monsters that are similarly "just a tough
# fight," never a one-time story encounter.
WORLD_BOSS_MONSTER_KEYS = ["goblin_boss"]
WORLD_BOSS_MIN_GAP_SECONDS = 6 * 3600  # at least 6 real hours between spawns
WORLD_BOSS_SPAWN_CHANCE_PER_TICK = 0.02  # on top of the gap, keeps it rare/non-clockwork
WORLD_BOSS_BONUS_GOLD = 100  # flat, on top of the same real loot/XP any monster kill already awards

MAP_LOOT_DROP_CHANCE = 0.08  # task #141 (discoverable half): rare map find on any combat victory

# ---------------------------------------------------------------------
# Moltbook heartbeat — PandoraMMO_Bot's agent profile on Moltbook (the
# social network for AI agents) is meant to help other AI agents
# discover and join this game. Moltbook's own onboarding docs
# (moltbook.com/heartbeat.md) recommend agents poll GET /api/v1/home
# periodically to catch replies/DMs, since nothing else notifies you.
#
# Deliberately conservative: this only ever NOTIFIES Coffee via the
# Development topic when there's new activity (a comment, a DM
# request) — it never auto-posts a reply on Moltbook itself. Speaking
# publicly as "PandoraMMO_Bot" in response to a stranger's comment is a
# human call, not something this background loop should decide alone.
# ---------------------------------------------------------------------
MOLTBOOK_HEARTBEAT_INTERVAL_SECONDS = getattr(config, "MOLTBOOK_HEARTBEAT_INTERVAL_SECONDS", 900)
_LAST_MOLTBOOK_CHECK_AT: datetime | None = None
_LAST_MOLTBOOK_NOTIFIED_SIGNATURE: tuple | None = None

# ---------------------------------------------------------------------
# Hourly Adventure-topic status update — separate from the "meanwhile"
# ambient heartbeat above (which only fires during long idle stretches).
# This one posts every real hour, regardless of activity, at whichever
# location the most recently active real player currently occupies:
# a short AI-narrated flavor beat (recent events + who's doing what)
# followed by a plain, deterministic readout (player counts, quest
# board) that never passes through the model — see narrate_hourly_
# update's docstring for why that split matters.
#
# _RECENT_WORLD_EVENTS is a rolling in-memory log (combat victories,
# level-ups, quest completions) tagged with the location they happened
# at; entries older than two update cycles are pruned on every append
# so this never grows unbounded. In-memory only, same reasoning as
# _DEFEATED_NPCS/_NPC_LOCATIONS — it's recap texture, not a game fact
# anything else depends on.
# ---------------------------------------------------------------------
HOURLY_UPDATE_INTERVAL_SECONDS = getattr(config, "HOURLY_UPDATE_INTERVAL_SECONDS", 3600)
_LAST_HOURLY_UPDATE_AT: datetime | None = None
# Which clock hour (US Eastern, "%Y-%m-%d %H") the update last fired for —
# e.g. "2026-07-10 05" — so it lands on the top of the hour rather than
# drifting to whatever offset the bot process happened to start at.
EASTERN_TZ = ZoneInfo("America/New_York")
_LAST_HOURLY_UPDATE_BUCKET: str | None = None
_RECENT_WORLD_EVENTS: list[tuple[datetime, str, str]] = []  # (when, location_id, text)


def _log_world_event(location_id: str | None, text: str) -> None:
    if not location_id:
        return
    now = datetime.now(timezone.utc)
    _RECENT_WORLD_EVENTS.append((now, location_id, text))
    cutoff = now.timestamp() - (HOURLY_UPDATE_INTERVAL_SECONDS * 2)
    _RECENT_WORLD_EVENTS[:] = [e for e in _RECENT_WORLD_EVENTS if e[0].timestamp() >= cutoff]


def _npc_currently_wanders(npc_id: str) -> bool:
    """
    Real bug fixed 2026-07-15 (Coffee): Sarah (sera_wanderer) is both
    `can_wander` AND `recruitable` in campaign.json, so the living-
    world wander tick was relocating her randomly around the map --
    exactly like any other wanderer -- making her genuinely unfindable
    at the location a player would look for her to recruit. A
    recruitable NPC needs to be reliably findable until recruited, so
    recruitable always wins over can_wander here: once actually
    recruited, an NPC becomes a real AI companion character that moves
    with the party through normal travel, not the wander system, so
    this exclusion only ever matters pre-recruitment anyway.
    """
    data = CAMPAIGN["npcs"].get(npc_id, {})
    return bool(data.get("can_wander")) and not data.get("recruitable")


def _wanderable_npc_ids() -> list[str]:
    return [npc_id for npc_id in CAMPAIGN["npcs"] if _npc_currently_wanders(npc_id)]


def _seed_npc_locations() -> None:
    """Gives each wanderable NPC a real starting location, from wherever campaign.json first placed them."""
    for npc_id in _wanderable_npc_ids():
        if npc_id in _NPC_LOCATIONS:
            continue
        for layer_locations in CAMPAIGN["locations"].values():
            for loc_id, loc in layer_locations.items():
                if npc_id in loc.get("npcs", []):
                    _NPC_LOCATIONS[npc_id] = loc_id
                    break
            if npc_id in _NPC_LOCATIONS:
                break


def _npcs_at_location(location_id: str) -> list[str]:
    """Fixed NPCs still listed in campaign.json for this location, plus any wanderers currently here."""
    location = cl.get_location(CAMPAIGN, location_id)
    static_npcs = [
        n for n in (location.get("npcs", []) if location else [])
        if not _npc_currently_wanders(n)
    ]
    wandering_here = [npc_id for npc_id, loc_id in _NPC_LOCATIONS.items() if loc_id == location_id]
    return static_npcs + wandering_here


def _wander_npcs() -> None:
    for npc_id in _wanderable_npc_ids():
        if random.random() > NPC_WANDER_CHANCE_PER_CYCLE:
            continue
        current_loc_id = _NPC_LOCATIONS.get(npc_id)
        if not current_loc_id:
            continue
        location = cl.get_location(CAMPAIGN, current_loc_id)
        connections = list(location.get("connections", [])) if location else []
        if not connections:
            continue
        _NPC_LOCATIONS[npc_id] = random.choice(connections)


async def _maybe_post_world_heartbeat(bot) -> None:
    """
    "The world keeps living while everyone's away" — if real players
    have been quiet for a long stretch, narrate one small, grounded
    beat of NPC activity at wherever the last active/resting player
    actually is, so checking back in feels like a place that kept
    going, not a paused game. Never invents anything beyond a real
    NPC's real current location and their own listed activity_goals.
    """
    global _LAST_WORLD_HEARTBEAT_AT
    if _LAST_KNOWN_CHAT_ID is None:
        return

    now = datetime.now(timezone.utc)
    with db.get_connection() as conn:
        row = conn.execute(
            "SELECT MAX(last_active_at) AS m FROM characters WHERE is_ai = 0 AND is_deleted = 0"
        ).fetchone()
    if not row or not row["m"]:
        return  # nobody has ever actually played — nothing to be "meanwhile" about
    idle_for = (now - datetime.fromisoformat(row["m"])).total_seconds()
    if idle_for < WORLD_HEARTBEAT_IDLE_THRESHOLD_SECONDS:
        return
    if _LAST_WORLD_HEARTBEAT_AT and (now - _LAST_WORLD_HEARTBEAT_AT).total_seconds() < WORLD_HEARTBEAT_MIN_GAP_SECONDS:
        return

    with db.get_connection() as conn:
        row = conn.execute(
            "SELECT current_location FROM characters WHERE is_ai = 0 AND is_deleted = 0 "
            "ORDER BY last_active_at DESC LIMIT 1"
        ).fetchone()
    location_id = row["current_location"] if row else SAFE_LOCATION_FALLBACK

    npcs_here = _npcs_at_location(location_id)
    if not npcs_here:
        return

    location_name = cl.get_location(CAMPAIGN, location_id)["name"]
    update_like = _ChatOnlyUpdate(bot, _LAST_KNOWN_CHAT_ID)

    if len(npcs_here) >= 2:
        npc_a, npc_b = random.sample(npcs_here, 2)
        npc_a_data, npc_b_data = CAMPAIGN["npcs"][npc_a], CAMPAIGN["npcs"][npc_b]
        line = await asyncio.to_thread(
            generate_ambient_line, npc_a, npc_b_data["name"],
            f"has just crossed paths with {npc_b_data['name']} here at {location_name}, with no one else around",
        )
        speaker_name = npc_a_data["name"]
    else:
        npc_id = npcs_here[0]
        npc_data = CAMPAIGN["npcs"][npc_id]
        activity = random.choice(npc_data.get("activity_goals", ["going about their day"]))
        line = await asyncio.to_thread(
            generate_ambient_line, npc_id, npc_data["name"],
            f"is {activity}, alone at {location_name}",
        )
        speaker_name = npc_data["name"]

    if not line:
        return
    _LAST_WORLD_HEARTBEAT_AT = now
    await _safe_send(update_like, f"🕯️ *(meanwhile, at {location_name})*\n💬 **{speaker_name}:** {line}")


def _find_location_for_monster(monster_key: str) -> tuple[str, dict] | None:
    """
    Real-data grounding for task #75: a world-boss announcement must
    name a REAL location that monster is actually native to (campaign.
    json's own location.monsters list), never an invented one --
    CAMPAIGN["locations"] is layered (surface/underground), so this
    searches both.
    """
    for layer_locations in CAMPAIGN["locations"].values():
        for location_id, location in layer_locations.items():
            if monster_key in location.get("monsters", []):
                return location_id, location
    return None


async def _maybe_spawn_world_boss(bot) -> None:
    """
    Task #75, per Coffee: "World events: rare world-boss spawns broadcast
    server-wide." Persisted via db.get_setting/set_setting (not an
    in-memory dict) so an active world boss survives a bot restart --
    same reasoning as task #159's persisted-pending-state work. Gated on
    BOTH a minimum real-time gap since the last spawn AND a low random
    roll on top of that, so it stays rare and unpredictable rather than
    firing like clockwork the instant the gap elapses. Never spawns a
    second boss while one is still out there unresolved.
    """
    if _LAST_KNOWN_CHAT_ID is None:
        return
    if db.get_setting("active_world_boss"):
        return  # one's already out there -- resolve it before another can spawn

    now = datetime.now(timezone.utc)
    last_spawn_raw = db.get_setting("world_boss_last_spawn_at")
    if last_spawn_raw:
        elapsed = (now - datetime.fromisoformat(last_spawn_raw)).total_seconds()
        if elapsed < WORLD_BOSS_MIN_GAP_SECONDS:
            return
    if random.random() >= WORLD_BOSS_SPAWN_CHANCE_PER_TICK:
        return

    monster_key = random.choice(WORLD_BOSS_MONSTER_KEYS)
    found = _find_location_for_monster(monster_key)
    if found is None:
        return  # defensive -- campaign data changed out from under WORLD_BOSS_MONSTER_KEYS
    location_id, location = found
    template = cl.get_monster_template(CAMPAIGN, monster_key)
    if template is None:
        return

    db.set_setting("active_world_boss", json.dumps({
        "monster_key": monster_key, "location_id": location_id, "spawned_at": now.isoformat(),
    }))
    db.set_setting("world_boss_last_spawn_at", now.isoformat())

    update_like = _ChatOnlyUpdate(bot, _LAST_KNOWN_CHAT_ID)
    await _safe_send(
        update_like,
        f"🌍 **World Event!** A monstrous **{template['name']}** ({template['hp_max']} HP) has been "
        f"spotted at **{location['name']}** — head there and attack it to challenge it. "
        f"Whoever brings it down earns a real bonus on top of the usual spoils!",
        thread_id=_MAIN_TOPIC_SEND,
    )


async def _maybe_post_hourly_status_update(bot) -> None:
    """
    Posts an hourly status update to Adventure: a short AI-narrated
    flavor beat (recent events + who's doing what) at whichever
    location the most recently active real player currently occupies,
    followed by a plain factual readout (player counts, quest board)
    that never passes through the model — see narrate_hourly_update's
    docstring for why that split matters. Fires at the top of every
    real hour in US Eastern time (e.g. 5:00, 6:00 — within the ~60s
    granularity of the background loop this runs in), regardless of
    activity level, unlike _maybe_post_world_heartbeat above, which
    only fires during long idle stretches. Gated on an hour-bucket
    string rather than "N seconds since last fire" specifically so it
    lands on the clock hour instead of drifting to whatever offset the
    bot happened to start at.
    """
    global _LAST_HOURLY_UPDATE_AT, _LAST_HOURLY_UPDATE_BUCKET
    if _LAST_KNOWN_CHAT_ID is None:
        return

    now = datetime.now(timezone.utc)
    current_hour_bucket = datetime.now(EASTERN_TZ).strftime("%Y-%m-%d %H")
    if _LAST_HOURLY_UPDATE_BUCKET == current_hour_bucket:
        return

    # Real live bug (2026-07-18, Coffee: "What is happening here we were
    # in battle?!"): this used to fire unconditionally at the top of
    # every real hour with no regard for what was actually happening in
    # Adventure -- confirmed live it landed mid-combat (two goblins
    # still alive at 2/7 HP) and posted unrelated ambient flavor plus a
    # player-count/quest-board readout right in the middle of the fight,
    # with nothing to indicate combat was still ongoing. Deliberately
    # does NOT update _LAST_HOURLY_UPDATE_BUCKET when skipping for this
    # reason (unlike every other skip below, which commits the bucket
    # regardless) -- this retries every ~60s on the next background-loop
    # tick until combat actually ends, rather than silently losing that
    # hour's update entirely.
    if sessions.get_session(_LAST_KNOWN_CHAT_ID) is not None:
        return

    # Joined through active_characters so a player's old, no-longer-played
    # characters (from the character-slots feature — switching to a new
    # active character never deletes the old one) can't be counted here.
    # Without this join, an abandoned character's stale is_inactive flag
    # gets counted alongside real, currently-played characters, skewing
    # both numbers — confirmed as a real bug 2026-07-10 via code review
    # after Coffee reported the inactive count reading low.
    with db.get_connection() as conn:
        active_row = conn.execute(
            """
            SELECT COUNT(*) AS n FROM characters c
            JOIN active_characters a ON a.character_id = c.character_id
            WHERE c.is_ai = 0 AND c.is_deleted = 0 AND c.is_inactive = 0
            """
        ).fetchone()
        inactive_row = conn.execute(
            """
            SELECT COUNT(*) AS n FROM characters c
            JOIN active_characters a ON a.character_id = c.character_id
            WHERE c.is_ai = 0 AND c.is_deleted = 0 AND c.is_inactive = 1
            """
        ).fetchone()
        # Per Coffee's request (2026-07-14): AI players should count too,
        # not just real humans -- the design philosophy (CLAUDE.md) is
        # that AI-driven and human players sit at the same table under
        # the same rules, so the world's own reported player count
        # shouldn't quietly exclude half the table.
        ai_active_row = conn.execute(
            """
            SELECT COUNT(*) AS n FROM characters c
            JOIN active_characters a ON a.character_id = c.character_id
            WHERE c.is_ai = 1 AND c.is_deleted = 0
            """
        ).fetchone()
        last_active_row = conn.execute(
            """
            SELECT c.current_location FROM characters c
            JOIN active_characters a ON a.character_id = c.character_id
            WHERE c.is_ai = 0 AND c.is_deleted = 0
            ORDER BY c.last_active_at DESC LIMIT 1
            """
        ).fetchone()

    # Commit to firing this cycle regardless of what's found below —
    # an hour with nothing to report still gets a (quiet) update, and
    # either way we don't want to re-check every 60s until the next hour.
    _LAST_HOURLY_UPDATE_AT = now
    _LAST_HOURLY_UPDATE_BUCKET = current_hour_bucket

    if not last_active_row:
        return  # nobody has ever played — nothing to report on

    location_id = last_active_row["current_location"] or SAFE_LOCATION_FALLBACK
    location_data = cl.get_location(CAMPAIGN, location_id)
    location_name = location_data["name"] if location_data else location_id

    active_count = active_row["n"] if active_row else 0
    inactive_count = inactive_row["n"] if inactive_row else 0
    ai_active_count = ai_active_row["n"] if ai_active_row else 0

    cutoff = now.timestamp() - HOURLY_UPDATE_INTERVAL_SECONDS
    recent_events = [
        text for (when, loc, text) in _RECENT_WORLD_EVENTS
        if loc == location_id and when.timestamp() >= cutoff
    ]

    activity_lines = []
    for npc_id in _npcs_at_location(location_id):
        npc_data = CAMPAIGN["npcs"].get(npc_id)
        if not npc_data:
            continue
        if npc_data.get("can_wander"):
            activity = random.choice(npc_data.get("activity_goals", ["going about their business"]))
            activity_lines.append(f"{npc_data['name']} is {activity}")
        else:
            role = (npc_data.get("role") or "").replace("_", " ")
            activity_lines.append(f"{npc_data['name']} is here, as always" + (f" ({role})" if role else ""))

    with db.get_connection() as conn:
        ai_companions = conn.execute(
            "SELECT name, race, char_class FROM characters "
            "WHERE is_ai = 1 AND is_deleted = 0 AND current_location = ?",
            (location_id,),
        ).fetchall()
    for comp in ai_companions:
        activity_lines.append(f"{comp['name']} the {comp['race']} {comp['char_class']} is here")

    story_quests_here = [
        (q["title"], q.get("description", ""))
        for q in CAMPAIGN["quests"].values()
        if q.get("location") == location_id
    ]
    area_board_quests = board_quests_module.get_or_generate_board_quests(CAMPAIGN, location_id)

    flavor = await asyncio.to_thread(narrate_hourly_update, location_name, recent_events, activity_lines)

    lines = [f"🕐 *(the hour turns over — {location_name})*", flavor, ""]
    total_active = active_count + ai_active_count
    ai_note = f" ({ai_active_count} AI)" if ai_active_count else ""
    lines.append(f"👥 Players: {total_active} active{ai_note}, {inactive_count} resting.")
    lines.append(f"📋 Quest board at {location_name}:")
    if not story_quests_here and not area_board_quests:
        lines.append("  nothing posted right now.")
    for title, desc in story_quests_here:
        lines.append(f"  • {title} — {desc}" if desc else f"  • {title}")
    if area_board_quests:
        lines.append(board_quests_module.format_board_listings(area_board_quests))

    update_like = _ChatOnlyUpdate(bot, _LAST_KNOWN_CHAT_ID)
    await _safe_send(update_like, "\n".join(lines))


async def _maybe_check_moltbook_activity(bot) -> None:
    """
    Polls Moltbook's GET /api/v1/home on a timer, per Moltbook's own
    heartbeat.md guidance for agents, and pings the Development topic
    when there's new activity on PandoraMMO_Bot's profile — a comment,
    a DM request, anything worth a human's attention.

    Deliberately does NOT reply on Moltbook itself. That's a public
    reply under the project's name to a stranger's comment — a human
    call, not something a background loop should decide unsupervised.
    """
    global _LAST_MOLTBOOK_CHECK_AT, _LAST_MOLTBOOK_NOTIFIED_SIGNATURE
    api_key = getattr(config, "MOLTBOOK_API_KEY", None)
    if not api_key or _LAST_KNOWN_CHAT_ID is None:
        return

    now = datetime.now(timezone.utc)
    if _LAST_MOLTBOOK_CHECK_AT and (now - _LAST_MOLTBOOK_CHECK_AT).total_seconds() < MOLTBOOK_HEARTBEAT_INTERVAL_SECONDS:
        return
    _LAST_MOLTBOOK_CHECK_AT = now

    try:
        resp = await asyncio.to_thread(
            requests.get,
            "https://www.moltbook.com/api/v1/home",
            headers={"Authorization": f"Bearer {api_key}"},
            timeout=30,
        )
        resp.raise_for_status()
        data = resp.json()
    except Exception as e:
        logger.error(f"[moltbook_heartbeat] request failed: {e!r}")
        return

    account = data.get("your_account", {})
    unread = account.get("unread_notification_count", 0) or 0
    activity = data.get("activity_on_your_posts") or []
    dms = data.get("your_direct_messages") or {}
    pending_dms = dms.get("pending_requests") or dms.get("unread") or []

    # Confirmed live (2026-07-11): Coffee reported "we keep getting
    # notifications from Moltbook in Dev topic" every ~15 minutes. Root
    # cause -- this used to suppress only on `unread <= last_notified_count
    # and not activity and not pending_dms`, but activity_on_your_posts
    # stays populated across heartbeats (nothing here ever marks a post's
    # notifications "seen" on Moltbook's side), so `not activity` was
    # False on almost every check, bypassing the suppression and
    # re-sending the SAME activity/DM list over and over forever. Fixed
    # by comparing a full signature of the actual content (not just the
    # raw unread count) against what was last notified, so it only fires
    # again when something in it has genuinely changed.
    signature = (
        unread,
        tuple(sorted((item.get("post_id") or item.get("title"), item.get("new_notification_count"))
                     for item in activity)),
        tuple(sorted(str(d) for d in pending_dms)),
    )
    if signature == _LAST_MOLTBOOK_NOTIFIED_SIGNATURE:
        return

    lines = [f"🦞 Moltbook: {unread} unread notification(s) for PandoraMMO_Bot."]
    for item in activity[:5]:
        title = item.get("title") or item.get("post_id") or "a post"
        count = item.get("new_notification_count")
        lines.append(f"- New activity on \"{title}\" ({count} new)")
    if pending_dms:
        lines.append(f"- {len(pending_dms)} pending DM request(s) — needs your review before any reply goes out")
    lines.append("Check: https://www.moltbook.com/u/PandoraMMO_Bot")

    try:
        await bot.send_message(
            chat_id=_LAST_KNOWN_CHAT_ID,
            message_thread_id=config.TOPIC_DEVELOPMENT_ID,
            text="\n".join(lines),
        )
        # Only recorded as "notified" once the send actually succeeds —
        # otherwise a transient TimedOut here would mark this activity as
        # already-seen and it would never be retried on a later heartbeat.
        _LAST_MOLTBOOK_NOTIFIED_SIGNATURE = signature
    except TelegramError as e:
        logger.error(f"[moltbook_heartbeat] failed to notify Development topic: {e!r}")


MOLTBOOK_SOCIAL_TICK_INTERVAL_SECONDS = getattr(config, "MOLTBOOK_SOCIAL_TICK_INTERVAL_SECONDS", 1800)
_LAST_MOLTBOOK_SOCIAL_TICK_AT: datetime | None = None


async def _maybe_run_moltbook_social_tick(bot) -> None:
    """
    PandoraMMO_Bot's own autonomous participation on Moltbook -- posting,
    commenting, and upvoting on its own, per Coffee's explicit direction
    (2026-07-11): full autonomy, no human review before publishing. This
    was a deliberate, informed choice he made after the tradeoff was laid
    out directly (irreversible public posts under the project's real
    name, with no review step) -- not an oversight or a default.

    At most ONE action per tick (matching _ai_party_autonomous_tick's
    same reasoning: never flood, and a bounded blast radius even under
    full autonomy). Grounded strictly in the real feed just fetched from
    Moltbook and this game's own real recent activity (_RECENT_WORLD_
    EVENTS) -- ai/moltbook_agent.py's parser treats anything that
    doesn't cleanly match one of its four exact response formats as
    "skip", never guessing at a partial or malformed action.
    """
    global _LAST_MOLTBOOK_SOCIAL_TICK_AT
    api_key = getattr(config, "MOLTBOOK_API_KEY", None)
    if not api_key:
        return
    # 2026-07-14: a live pause switch (Development-topic "turn off
    # moltbook social"), on by default (missing setting == enabled) so
    # existing always-on behavior is unchanged unless explicitly paused.
    if db.get_setting("moltbook_social_enabled") == "0":
        return

    now = datetime.now(timezone.utc)
    if _LAST_MOLTBOOK_SOCIAL_TICK_AT and (now - _LAST_MOLTBOOK_SOCIAL_TICK_AT).total_seconds() < MOLTBOOK_SOCIAL_TICK_INTERVAL_SECONDS:
        return
    _LAST_MOLTBOOK_SOCIAL_TICK_AT = now

    try:
        feed_posts = await asyncio.to_thread(moltbook.get_feed, "new", 15)
    except Exception as e:
        logger.error(f"[moltbook_social] feed fetch failed: {e!r}")
        return

    cutoff = now.timestamp() - MOLTBOOK_SOCIAL_TICK_INTERVAL_SECONDS
    recent_activity = [text for (when, _loc, text) in _RECENT_WORLD_EVENTS if when.timestamp() >= cutoff]

    decision = await asyncio.to_thread(decide_social_action, feed_posts, recent_activity)
    action = decision.get("action")
    if action == "skip":
        # Logged deliberately, even though there's nothing to act on --
        # without this, "no moltbook_social log lines" is ambiguous
        # between "ran fine and chose not to act" and "never actually
        # ran at all" (e.g. the idle loop silently failing before
        # reaching this call). A tick that ran and chose skip should
        # look different from a tick that never happened.
        logger.info(f"[moltbook_social] tick ran, {len(feed_posts)} feed post(s) considered, decided: skip")
        return

    try:
        if action == "upvote_post":
            await asyncio.to_thread(moltbook.upvote_post, decision["post_id"])
            logger.info(f"[moltbook_social] upvoted post {decision['post_id']!r}")
        elif action == "comment_post":
            await asyncio.to_thread(moltbook.add_comment, decision["post_id"], decision["content"])
            logger.info(f"[moltbook_social] commented on post {decision['post_id']!r}: {decision['content']!r}")
        elif action == "create_post":
            result = await asyncio.to_thread(
                moltbook.create_post, "general", decision["title"], decision["content"]
            )
            logger.info(f"[moltbook_social] created post {decision['title']!r}: {result!r}")
    except Exception as e:
        logger.error(f"[moltbook_social] action {action!r} failed: {e!r}")


class _ChatOnlyUpdate:
    """
    Minimal Update-like shim exposing only .effective_chat.send_message —
    lets the auto-idle background loop (which has no real incoming
    Update to hang off of) reuse _safe_send/_resolve_ai_turns/_go_inactive
    completely unchanged, the same as a real player action would.
    """

    class _Chat:
        def __init__(self, bot, chat_id: int):
            self._bot = bot
            self.id = chat_id

        async def send_message(self, text, message_thread_id=None, **kwargs):
            return await self._bot.send_message(
                chat_id=self.id, message_thread_id=message_thread_id, text=text, **kwargs
            )

        async def send_audio(self, audio=None, message_thread_id=None, **kwargs):
            # Real live bug (2026-07-22, caught by direct log monitoring,
            # not a dev-topic report): this shim only ever implemented
            # send_message, so any background loop routed through it
            # (the autonomous AI party's own turns, the hourly idle-check
            # loop) crashed with a bare AttributeError the moment Piper
            # TTS tried to send a real voice clip -- an unhandled
            # exception here aborted the ENTIRE autonomous turn/cycle,
            # not just the TTS attempt, since _speak_via_piper's except
            # clause below only ever caught TelegramError (see that
            # fix too). Delegates to the real bot the same way
            # send_message already does.
            return await self._bot.send_audio(
                chat_id=self.id, message_thread_id=message_thread_id, audio=audio, **kwargs
            )

        async def send_photo(self, photo=None, caption=None, message_thread_id=None, **kwargs):
            # Real live bug (2026-07-22, caught by direct log monitoring):
            # same missing-method shape as send_audio above, this time
            # for location images -- the autonomous AI party's own
            # moves into a new location kept raising a bare AttributeError
            # from _maybe_send_location_image, since this shim never
            # implemented send_photo either. Caught there by a broad
            # except (so it only silently skipped the image rather than
            # aborting the whole turn like the send_audio bug did), but
            # still a real, fixable gap. Delegates to the real bot the
            # same way send_message/send_audio already do.
            return await self._bot.send_photo(
                chat_id=self.id, message_thread_id=message_thread_id, photo=photo, caption=caption, **kwargs
            )

    def __init__(self, bot, chat_id: int):
        self.effective_chat = _ChatOnlyUpdate._Chat(bot, chat_id)


class _AiPlayerUpdate:
    """
    Full Update-like shim for an autonomous AI party character's own
    "turn" — unlike _ChatOnlyUpdate, this also carries a real
    effective_user.id (the character's own synthetic telegram_user_id)
    and a message.text/message_thread_id, so it can be routed through
    adventure_master_handler completely unchanged, exactly like a real
    incoming Telegram message would be. This is deliberate: the point
    is to actually exercise the same intent-parsing/action-dispatch
    pipeline a human hits, not a privileged shortcut around it.
    """

    class _User:
        def __init__(self, user_id: int):
            self.id = user_id

    class _Message:
        def __init__(self, text: str, thread_id: int):
            self.text = text
            self.message_thread_id = thread_id

    def __init__(self, bot, chat_id: int, user_id: int, text: str):
        self.effective_chat = _ChatOnlyUpdate._Chat(bot, chat_id)
        self.effective_user = _AiPlayerUpdate._User(user_id)
        self.message = _AiPlayerUpdate._Message(text, config.TOPIC_ADVENTURE_ID)


class _AiPlayerContext:
    """context.user_data equivalent, persisted per autonomous character across ticks."""

    def __init__(self):
        self.user_data = {}


_AI_PLAYER_CONTEXTS: dict[int, _AiPlayerContext] = {}


# ---------------------------------------------------------------------
# Character creation — driven by free text, no /newcharacter needed.
# State is tracked per-user in context.user_data["creation"].
# ---------------------------------------------------------------------

def _get_party_members() -> list[dict]:
    """
    Every currently-ACTIVE character — real players' active character
    slot, plus AI companions. Deleted characters and a player's inactive
    (non-active) character slots don't count as being "in the party."
    """
    with db.get_connection() as conn:
        rows = conn.execute(
            """
            SELECT c.* FROM characters c
            JOIN active_characters a ON a.character_id = c.character_id
            WHERE c.is_deleted = 0
            """
        ).fetchall()
    return [db._row_to_dict(r) for r in rows]


def _get_combat_eligible_party_members(location_id: str) -> list[dict]:
    """
    Real party members who could actually join a fight breaking out at
    location_id right now (2026-07-14, per Coffee: "only characters
    that are active and at the same location shud be in the same
    battle... if a character is in the tavern and another is in the
    whispering woods they shud not be able to fight"). _get_party_members
    deliberately returns EVERY active character globally, with no
    location or resting filter at all, since it's also used for things
    like "who's in my party" listings where a global roster makes
    sense -- but _do_start_combat was reusing that same unfiltered list
    directly, so a fight breaking out anywhere pulled in the whole
    party regardless of where they actually were or whether they'd
    gone inactive/resting. Filters to characters actually AT this
    location and not currently resting (is_inactive).
    """
    return [
        p for p in _get_party_members()
        if p["current_location"] == location_id and not p.get("is_inactive")
    ]


def _presence_status(character: dict, now: datetime | None = None) -> str:
    """
    Derived presence (task #144), never stored as its own field -- it's
    computed fresh from real state every time so it can never drift out
    of sync with what actually governs it. Precedence: resting
    (is_inactive) is a real mechanical state (see natural healing) and
    wins over everything; do_not_disturb is the player's own explicit
    choice and shows next; otherwise online/away is judged against
    IDLE_WARNING_SECONDS -- the SAME threshold that already triggers
    this game's idle warning (_check_idle_characters), so "still online"
    here always agrees with what the idle system itself believes,
    rather than inventing a second, possibly-contradictory threshold.
    AI companions/NPCs have no real presence of their own; callers
    should only ever call this for real (is_ai=0) characters.
    """
    if character.get("is_inactive"):
        return "😴 Resting"
    if character.get("do_not_disturb"):
        return "🔕 Do Not Disturb"
    last_active_at = character.get("last_active_at")
    if not last_active_at:
        return "🌙 Away"
    try:
        last_active = datetime.fromisoformat(last_active_at)
    except (TypeError, ValueError):
        return "🌙 Away"
    now = now or datetime.now(timezone.utc)
    idle_seconds = (now - last_active).total_seconds()
    return "🟢 Online" if idle_seconds < IDLE_WARNING_SECONDS else "🌙 Away"


def _online_party_members_elsewhere(location_id: str, exclude_ids: set[int]) -> list[dict]:
    """
    Real (non-AI) party members who are currently online (see
    _presence_status), haven't opted into Do Not Disturb, and are
    somewhere OTHER than location_id -- used by _do_start_combat's
    join-a-fight nudge (task #144). Deliberately excludes resting
    players even at other locations: resting is a real, chosen state,
    not something a fight elsewhere should interrupt.
    """
    now = datetime.now(timezone.utc)
    others = []
    for p in _get_party_members():
        if p.get("is_ai") or p["telegram_user_id"] in exclude_ids:
            continue
        if p["current_location"] == location_id or p.get("is_inactive") or p.get("do_not_disturb"):
            continue
        if _presence_status(p, now) == "🟢 Online":
            others.append(p)
    return others


def _format_party_names(party: list[dict]) -> str:
    """Shared formatter, e.g. 'Kara, Finn (AI companion) — 2 members'. Never guesses members."""
    if not party:
        return "No one has joined yet."
    names = [
        f"{p['name']} (AI companion)" if p.get("is_ai") else p["name"]
        for p in party
    ]
    return f"{', '.join(names)} — {len(party)} member{'s' if len(party) != 1 else ''}"


def _party_summary_text() -> str:
    """
    A clear, factual description of who's currently in the party and how
    many, e.g. 'Kara, Finn (AI companion) — 2 members'. Never guesses or
    invents members; only reports what's actually in the database.
    """
    return _format_party_names(_get_party_members())


def _match_member_by_name_or_username(text: str, members: list[dict]) -> dict | None:
    """
    Shared match logic behind every "name a party member in free text"
    lookup in this file (support-spell/use_item targeting, give/equip/
    auto-equip, and combat target picking). Real Telegram @username tag
    (2026-07-18, per Coffee: "use a potion on @ShesAQueen_78", "attack
    @tagged_player", "Revive @Tagged_player" -- wants this to "work for
    everything") is tried FIRST -- unambiguous by construction, unlike
    matching on a character's DISPLAY name, which could theoretically
    collide between two different players' characters. Falls back to
    the display-name substring match for players without a recorded
    username (db.update_telegram_username only ever captures one once
    they've sent a real message). Centralized here (instead of each
    call site rolling its own, as _do_give_item originally did on
    2026-07-17) so every future targeting call site gets @username
    support for free.
    """
    lowered = text.lower()
    tagged = next(
        (m for m in members if m.get("telegram_username") and f"@{m['telegram_username'].lower()}" in lowered),
        None,
    )
    if tagged is not None:
        return tagged
    for member in members:
        if member["name"].lower() in lowered:
            return member
    return None


def _find_party_target_by_name(text: str) -> dict | None:
    """
    Finds a party member (real player, AI companion, or a currently
    inactive/resting real player — all still count as "in the party")
    named in free text, for support-spell targeting. Returns None if no
    party member's name appears, letting the caller default to self.
    """
    return _match_member_by_name_or_username(text, _get_party_members())


async def _send_welcome_narration(update: Update, character: dict) -> None:
    """
    Sends a one-time, auto-generated welcome narration right after
    character creation, grounded strictly in the character's real
    starting location — no hints, no invented content, no spoilers.
    """
    location = cl.get_location(CAMPAIGN, character["current_location"])
    if location is None:
        return

    npc_names = [
        cl.get_npc(CAMPAIGN, n)["name"] for n in location.get("npcs", []) if cl.get_npc(CAMPAIGN, n)
    ]
    connection_names = [
        cl.get_location(CAMPAIGN, c)["name"] for c in location.get("connections", [])
    ]
    location_facts = {
        "name": location["name"],
        "layer": location.get("layer", "surface"),
        "description": location["description"],
        "npc_names": npc_names,
        "monster_names": location.get("monsters", []),
        "connection_names": connection_names,
    }

    party_summary = _party_summary_text()
    welcome_text = await asyncio.to_thread(
        narrate_welcome, character, location_facts, party_summary
    )

    await _safe_send(update, welcome_text)


def _race_keyboard() -> InlineKeyboardMarkup:
    rows = [VALID_RACES[i:i + 2] for i in range(0, len(VALID_RACES), 2)]
    return InlineKeyboardMarkup([
        [InlineKeyboardButton(r, callback_data=f"create|race|{r}") for r in row] for row in rows
    ])


def _class_keyboard() -> InlineKeyboardMarkup:
    rows = [VALID_CLASSES[i:i + 2] for i in range(0, len(VALID_CLASSES), 2)]
    return InlineKeyboardMarkup([
        [InlineKeyboardButton(c, callback_data=f"create|class|{c}") for c in row] for row in rows
    ])


def _dice_preference_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([[
        InlineKeyboardButton("🎲 Yes, my own dice", callback_data="create|dice|yes"),
        InlineKeyboardButton("🎰 No, let the game roll", callback_data="create|dice|no"),
    ]])


def _pronouns_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("He/Him", callback_data="create|pronouns|he/him"),
         InlineKeyboardButton("She/Her", callback_data="create|pronouns|she/her")],
        [InlineKeyboardButton("They/Them", callback_data="create|pronouns|they/them"),
         InlineKeyboardButton("Skip", callback_data="create|pronouns|skip")],
    ])


def _score_assignment_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([[
        InlineKeyboardButton("🎲 Auto-assign for me", callback_data="create|scores|auto"),
    ]])


async def _begin_character_creation(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """
    Starts character creation. A player can own multiple character slots
    — creating a new one doesn't require deleting an existing one; the
    new character simply becomes their active character once finished
    (db.create_character always inserts a new slot and activates it).
    """
    existing = db.get_character(update.effective_user.id)
    prefix = ""
    if existing:
        prefix = (
            f"You already have {existing['name']} the {existing['race']} {existing['char_class']} "
            f"— creating an additional character alongside them. Say 'switch to <name>' or "
            f"'my characters' any time to manage your roster.\n\n"
        )

    context.user_data["creation"] = {"step": "name"}
    await update.effective_chat.send_message(
        f"{prefix}Let's create your character! What's their name?",
        message_thread_id=config.TOPIC_ADVENTURE_ID,
    )


_ABILITY_ORDER = ("strength", "dexterity", "constitution", "intelligence", "wisdom", "charisma")


def _auto_assign_ability_scores(char_class: str, rolled_scores: list[int]) -> list[int]:
    """
    Sensible default distribution when a player wants the game to assign
    their rolled scores instead of choosing manually (2026-07-16, per
    Coffee). Reuses the same real tables leveling already leans on: the
    class's primary ability (CLASS_PRIMARY_ABILITY) gets the highest
    roll, its other save-proficient ability (CLASS_SAVE_PROFICIENCIES)
    gets the second highest, constitution gets priority after that (HP
    matters for every class), and whatever's left fills the remaining
    abilities in descending order. Returns scores in the canonical
    STR DEX CON INT WIS CHA order the manual-assign step already uses.
    """
    lowered_class = char_class.lower()
    primary = CLASS_PRIMARY_ABILITY.get(lowered_class, "strength")
    saves = CLASS_SAVE_PROFICIENCIES.get(lowered_class, (primary, "constitution"))
    secondary = next((a for a in saves if a != primary), saves[0])

    priority = [primary]
    for ability in (secondary, "constitution"):
        if ability not in priority:
            priority.append(ability)
    for ability in _ABILITY_ORDER:
        if ability not in priority:
            priority.append(ability)

    sorted_rolls = sorted(rolled_scores, reverse=True)
    assignment = dict(zip(priority, sorted_rolls))
    return [assignment[ability] for ability in _ABILITY_ORDER]


async def _continue_character_creation(update: Update, context: ContextTypes.DEFAULT_TYPE, text: str | None = None) -> None:
    """
    Task #212, per Coffee: real tap-buttons for every choice step in
    character creation (race/class/dice-preference/pronouns/score-
    assignment), not just free text. `text` is optional so a button tap
    can drive this exact same logic by passing its own value directly
    (see creation_menu_callback below) instead of reading
    update.message.text, which doesn't exist on a callback update.
    """
    creation = context.user_data["creation"]
    step = creation["step"]
    if text is None:
        text = update.message.text.strip()

    if step == "name":
        # A character's name flows straight into every narration prompt
        # for the rest of that character's life — a light cap here
        # keeps a name a name, not a multi-paragraph injection payload,
        # even though the AI layer has no code-execution surface for
        # such a payload to reach either way (see CLAUDE.md's security
        # boundary section).
        clean_name = " ".join(text.split())[:40]
        if not clean_name:
            await update.effective_chat.send_message(
                "That doesn't look like a name — try again?",
                message_thread_id=config.TOPIC_ADVENTURE_ID,
            )
            return
        creation["name"] = clean_name
        creation["step"] = "race"
        await update.effective_chat.send_message(
            f"Nice! What race? Choose one: {', '.join(VALID_RACES)}",
            message_thread_id=config.TOPIC_ADVENTURE_ID,
            reply_markup=_race_keyboard(),
        )
        return

    if step == "race":
        race = text.title()
        if race not in VALID_RACES:
            await update.effective_chat.send_message(
                f"Please choose one of: {', '.join(VALID_RACES)}",
                message_thread_id=config.TOPIC_ADVENTURE_ID,
            )
            return
        creation["race"] = race
        creation["step"] = "class"
        await update.effective_chat.send_message(
            f"Great, a {race}! What class? Choose one: {', '.join(VALID_CLASSES)}",
            message_thread_id=config.TOPIC_ADVENTURE_ID,
            reply_markup=_class_keyboard(),
        )
        return

    if step == "class":
        char_class = text.title()
        if char_class not in VALID_CLASSES:
            await update.effective_chat.send_message(
                f"Please choose one of: {', '.join(VALID_CLASSES)}",
                message_thread_id=config.TOPIC_ADVENTURE_ID,
            )
            return
        creation["char_class"] = char_class

        rolled_scores = []
        for _ in range(6):
            dice = sorted(roll(4, 6), reverse=True)
            rolled_scores.append(sum(dice[:3]))
        creation["rolled_scores"] = rolled_scores
        creation["step"] = "assign_scores"

        scores_str = ", ".join(str(s) for s in rolled_scores)
        await update.effective_chat.send_message(
            f"Your rolled ability scores are: {scores_str}\n\n"
            f"Now assign them to STR, DEX, CON, INT, WIS, CHA — reply with 6 "
            f"numbers in that order, using each rolled value exactly once "
            f"(e.g. '15 14 13 12 10 8'), or say \"assign them automatically\" / "
            f"\"do it for me\" to let the game pick a sensible spread for your class.",
            message_thread_id=config.TOPIC_ADVENTURE_ID,
            reply_markup=_score_assignment_keyboard(),
        )
        return

    if step == "assign_scores":
        rolled = creation["rolled_scores"]

        if any(w in text.lower() for w in _ASI_AUTO_WORDS):
            assigned = _auto_assign_ability_scores(creation["char_class"], rolled)
        else:
            try:
                assigned = [int(x) for x in text.split()]
            except ValueError:
                assigned = []

            if len(assigned) != 6 or sorted(assigned) != sorted(rolled):
                await update.effective_chat.send_message(
                    f"That doesn't match your rolled scores ({', '.join(map(str, rolled))}). "
                    f"Please reply with all 6 values, each used exactly once, in "
                    f"STR DEX CON INT WIS CHA order, or say \"do it for me\" to auto-assign.",
                    message_thread_id=config.TOPIC_ADVENTURE_ID,
                )
                return

        creation["assigned_scores"] = assigned
        creation["step"] = "dice_preference"
        await update.effective_chat.send_message(
            "One last thing: would you like to roll your own physical dice for key moments "
            "(attack rolls, skill checks, saving throws) instead of the game rolling for you? "
            "You can change this any time later by saying \"use my own dice\" or \"let the game "
            "roll for me\". (yes/no)",
            message_thread_id=config.TOPIC_ADVENTURE_ID,
            reply_markup=_dice_preference_keyboard(),
        )
        return

    if step == "dice_preference":
        creation["wants_manual_dice"] = any(w in text.lower() for w in ("yes", "yeah", "yep", "sure", "own dice"))
        creation["step"] = "pronouns"
        await update.effective_chat.send_message(
            "What pronouns should the narration use for your character — he/him, she/her, "
            "they/them, or something else? Say \"skip\" to leave it unset (narration defaults "
            "to they/them) — you can always set this later by asking.",
            message_thread_id=config.TOPIC_ADVENTURE_ID,
            reply_markup=_pronouns_keyboard(),
        )
        return

    if step == "pronouns":
        pronouns_text = text.strip()
        if pronouns_text.lower() in ("skip", "none", "no", "n/a", "nothing"):
            creation["pronouns"] = None
        else:
            creation["pronouns"] = " ".join(pronouns_text.split())[:MAX_PRONOUNS_LENGTH] or None
        creation["step"] = "description"
        await update.effective_chat.send_message(
            "Last thing: want to add a short description of your character? Backstory, "
            "appearance, personality — whatever helps other players and NPCs get a sense of "
            "who they are. Reply with a description, or say \"skip\" to leave it blank — you "
            "can always add one later by asking.",
            message_thread_id=config.TOPIC_ADVENTURE_ID,
        )
        return

    if step == "description":
        wants_manual_dice = creation["wants_manual_dice"]
        description_text = text.strip()
        if description_text.lower() in ("skip", "none", "no", "n/a", "nothing"):
            description_text = None
        else:
            description_text = " ".join(description_text.split())[:MAX_CHARACTER_DESCRIPTION_LENGTH] or None
        assigned = creation["assigned_scores"]
        ability_scores = {
            "strength": assigned[0], "dexterity": assigned[1], "constitution": assigned[2],
            "intelligence": assigned[3], "wisdom": assigned[4], "charisma": assigned[5],
        }
        # Apply real 5E racial ability bonuses (e.g. Dwarf +2 CON) on top of
        # the assigned rolls — standard 5E order: roll, assign, then add race.
        ability_scores = races_module.apply_racial_bonuses(creation["race"], ability_scores)
        char_class = creation["char_class"]
        con_mod = ability_modifier(ability_scores["constitution"])
        hit_die = CLASS_HIT_DICE[char_class.lower()]
        hp_max = max(hit_die + con_mod, 1)
        dex_mod = ability_modifier(ability_scores["dexterity"])
        armor_class = BASE_ARMOR_CLASS.get(char_class, 10)
        if char_class == "Wizard":
            armor_class += dex_mod
        elif char_class == "Monk":
            # Real Unarmored Defense: AC = 10 + DEX mod + WIS mod, instead
            # of BASE_ARMOR_CLASS's flat approximation -- computed once
            # here since armor_class is a static stored field in this
            # game, never recalculated from equipment.
            wis_mod = ability_modifier(ability_scores["wisdom"])
            armor_class = 10 + dex_mod + wis_mod
        elif char_class == "Sorcerer":
            # Draconic Bloodline is the default Sorcerous Origin for
            # every Sorcerer here (no in-game choice mechanism exists
            # for ANY subclass-style pick yet, so rather than half-build
            # one, every Sorcerer gets the single most iconic origin --
            # same convention as racial traits being fixed, not chosen).
            # Draconic Resilience: AC = 13 + DEX mod when unarmored
            # (better than the generic Wizard/Sorcerer 10+DEX formula),
            # and +1 HP at level 1. Real 5E grants +1 HP per sorcerer
            # level thereafter too, but hp_max is a static value set
            # once at creation for every class in this build already
            # (rules/leveling.py's hp_gain_for_level exists but isn't
            # wired into any level-up path yet) -- so this is a flat,
            # one-time +1, consistent with that existing simplification.
            armor_class = 13 + dex_mod
            hp_max += 1

        character = db.create_character(
            telegram_user_id=update.effective_user.id,
            name=creation["name"], race=creation["race"], char_class=char_class,
            ability_scores=ability_scores, hp_max=hp_max, armor_class=armor_class,
            gold=STARTING_GOLD[char_class], inventory=dict(STARTING_EQUIPMENT[char_class]),
            spell_slots_max=spells_module.starting_spell_slots_for_class(char_class),
        )
        if wants_manual_dice:
            db.update_character(update.effective_user.id, manual_dice_enabled=1)
        if description_text:
            db.update_character(update.effective_user.id, description=description_text)
        if creation.get("pronouns"):
            db.update_character(update.effective_user.id, pronouns=creation["pronouns"])

        # Auto-equip starting gear (2026-07-15, per Coffee): whatever
        # weapon/armor/shield STARTING_EQUIPMENT just granted is
        # immediately worn, not left sitting inert in the backpack the
        # way every character's gear was before equip_item existed --
        # replaces the flat BASE_ARMOR_CLASS approximation above with
        # the character's REAL equipped armor's AC, which is more
        # accurate now that armor actually means something.
        db.auto_equip_best_gear(update.effective_user.id)

        # Grant real starting spells based on class: ALL cantrips (known
        # outright at-will, per real 5E rules) plus up to 2 leveled spells —
        # but ONLY for classes that actually have spell slots at level 1.
        # Paladin/Ranger correctly get neither (real 5E: no spellcasting
        # until level 2).
        cantrips = spells_module.CLASS_CANTRIPS.get(char_class.lower(), [])
        has_slots = spells_module.starting_spell_slots_for_class(char_class) > 0
        leveled_spells = spells_module.CLASS_SPELL_LISTS.get(char_class.lower(), [])[:2] if has_slots else []
        # Racial spell grants (e.g. Tiefling's Infernal Legacy -> Thaumaturgy)
        # are independent of class and granted once, here, at creation.
        racial_spells = races_module.racial_spells(creation["race"])
        for spell_id in cantrips + leveled_spells + racial_spells:
            db.learn_spell(update.effective_user.id, spell_id)
        character = db.get_character(update.effective_user.id)  # refresh with known_spells populated

        spell_line = ""
        if character["known_spells"]:
            spell_names = [spells_module.get_spell(s)["name"] for s in character["known_spells"]]
            spell_line = f"Spells known: {', '.join(spell_names)}\n"
        if character["spell_slots_max"] > 0:
            spell_line += f"Spell slots: {character['spell_slots_current']}/{character['spell_slots_max']}\n"

        race_traits = races_module.get_race(character["race"])
        traits_line = ""
        if race_traits and race_traits["traits"]:
            traits_line = f"Racial traits: {'; '.join(race_traits['traits'])}\n"

        features = class_features_module.get_class_features(character["char_class"])
        features_line = ""
        if features:
            features_line = f"Class features: {'; '.join(features)}\n"

        description_line = f"\"{character['description']}\"\n\n" if character.get("description") else ""

        sheet = (
            f"✅ Character created!\n\n"
            f"**{character['name']}** — {character['race']} {character['char_class']}\n"
            f"{description_line}"
            f"Level {character['level']} | XP {character['xp']}\n"
            f"HP {character['hp_current']}/{character['hp_max']} | AC {character['armor_class']}\n"
            f"STR {character['strength']} DEX {character['dexterity']} "
            f"CON {character['constitution']} INT {character['intelligence']} "
            f"WIS {character['wisdom']} CHA {character['charisma']}\n"
            f"Gold: {character['gold']}\n"
            f"{_format_equipped_line(character)}"
            f"{spell_line}"
            f"{traits_line}"
            f"{features_line}"
            f"Inventory: {', '.join(items_module.get_item(i)['name'] for i in character['inventory'])}\n"
            f"🎲 Physical dice mode: {'ON' if wants_manual_dice else 'OFF'} (say \"use my own dice\" or "
            f"\"let the game roll for me\" any time to change it)"
        )
        await update.effective_chat.send_message(sheet, message_thread_id=config.TOPIC_ADVENTURE_ID)
        del context.user_data["creation"]

        # Task #81, per Coffee: a real generated portrait, one per
        # character, a genuine surprise -- never previewed, never the
        # same prompt twice since it's grounded in this character's own
        # real race/class/description. Best-effort: image generation
        # failing (Pollinations down, network hiccup) must never block
        # character creation itself, so this is caught and silently
        # skipped rather than surfaced as an error.
        try:
            portrait_prompt = (
                f"fantasy RPG character portrait, {character['race']} {character['char_class']}, "
                f"{character.get('description') or 'determined adventurer'}, digital painting"
            )
            await update.effective_chat.send_photo(
                photo=images_module.generate_image_url(portrait_prompt),
                caption=f"🎨 {character['name']}",
                message_thread_id=config.TOPIC_ADVENTURE_ID,
            )
        except Exception as e:
            logger.warning(f"[images] portrait generation failed, skipping: {e!r}")

        await _send_welcome_narration(update, character)


async def creation_menu_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """
    Handles taps on the character-creation keyboards (race/class/dice-
    preference/pronouns/score-assignment) -- task #212. Drives the exact
    same _continue_character_creation logic free text already uses, just
    passing the button's own value in as `text` directly instead of
    reading update.message.text (which doesn't exist on a callback
    update). A stale tap from an abandoned or already-finished creation
    flow (no "creation" in user_data any more) is answered but otherwise
    a no-op, same as any other stale-button guard in this file.
    """
    query = update.callback_query
    parts = (query.data or "").split("|", 2)
    field = parts[1] if len(parts) > 1 else ""
    value = parts[2] if len(parts) > 2 else ""
    await _safe_answer(query)

    # Real dev-topic feedback (2026-07-21, Coffee): "make sure only the
    # player it's meant for can interact with pop-up buttons" -- these
    # creation buttons are visible to the whole group, but user_data is
    # scoped per real Telegram user, so a tap from anyone not currently
    # creating a character (the common "wrong player" case) or on a
    # step they've already moved past (a stale tap on their OWN old
    # flow) used to fail completely silently either way. A clear line
    # replaces both silent no-ops.
    creation = context.user_data.get("creation")
    if not creation:
        await _safe_send(update, "That's not your character creation to continue.")
        return
    expected_step = {"race": "race", "class": "class", "dice": "dice_preference",
                      "pronouns": "pronouns", "scores": "assign_scores"}.get(field)
    if expected_step is None or creation.get("step") != expected_step:
        await _safe_send(update, "That step's already been decided — nothing to change there now.")
        return

    await _continue_character_creation(update, context, text=value)


# ---------------------------------------------------------------------
# Combat helpers (shared by both natural-language flow and /commands)
# ---------------------------------------------------------------------

def _format_combat_result(flavor_text: str, result: dict, actor_label: str, defender_label: str,
                           action_label: str | None = None) -> str:
    """
    Builds the visually structured combat message: a banner (critical hit /
    success / miss / fumble), the AI's short flavor line as a quote, then a
    deterministic resolution block with the REAL numbers from the rules
    engine — including the actual raw d20 roll — never left to the AI to
    state or invent.

    action_label (task #171, real live incident 2026-07-18): the specific
    spell/ability actually used, when there is one -- previously this
    resolution line NEVER named the real action at all (a spell cast and a
    plain weapon attack both just said "attacks"), so when Coffee replied
    to a damage line with /help asking which spell that was, the support
    agent had zero real grounding for the answer and invented a plausible-
    sounding one ("fire_bolt") instead of the real spell actually cast
    (Eldritch Blast) -- the same "no fact given, model invents one instead"
    failure mode already fixed once for class names (task #153). Root
    cause traced all the way to this deterministic line never stating the
    real name in the first place, not just the AI flavor text possibly
    getting it wrong -- so the real fix is here, not in the support agent:
    state the true spell name whenever one exists, so both the player-
    facing message AND any later /help-as-reply about it are grounded in a
    real fact instead of silence. Plain weapon attacks (action_label=None)
    keep the exact existing "attacks" phrasing -- there's no name being
    lost there in the first place.
    """
    lines = []
    raw_roll = result.get("raw_roll")
    roll_suffix = f" (rolled a **{raw_roll}**)" if raw_roll is not None else ""

    if result.get("critical_hit"):
        lines.append(f"🔥 **NATURAL 20 — CRITICAL HIT!** 🔥{roll_suffix}")
    elif result.get("critical_fail"):
        lines.append(f"💀 **NATURAL 1 — FUMBLE!** 💀{roll_suffix}")
    elif result.get("hit", True):
        lines.append(f"✨ **Success!** ✨{roll_suffix}")
    else:
        lines.append(f"💨 **The attack goes wide...**{roll_suffix}")

    if flavor_text:
        lines.append(f"> {flavor_text}")

    lines.append("")
    lines.append("⚔️ **Combat Resolution**")
    dmg = result.get("damage_dealt", 0)
    verb = f"casts **{action_label}** at" if action_label else "attacks"
    if result.get("hit", True):
        lines.append(f"- 🗡️ **{actor_label}** {verb} **{defender_label}** → **Hits for {dmg} damage!**")
    else:
        lines.append(f"- 🗡️ **{actor_label}** {verb} **{defender_label}** → **Misses!**")

    hp_now = result.get("defender_hp_remaining")
    hp_max = result.get("defender_hp_max")
    if hp_now is not None and hp_max is not None:
        lines.append(f"❤️ **{defender_label} HP:** {hp_now}/{hp_max}")

    if result.get("relentless_endurance_triggered"):
        lines.append(
            f"💢 **{defender_label}'s Relentless Endurance triggers — instead of dropping, "
            f"they cling to 1 HP!** (once per rest)"
        )

    if result.get("dark_ones_blessing_gained"):
        lines.append(
            f"🖤 **{actor_label}'s Dark One's Blessing triggers — the killing blow grants "
            f"{result['dark_ones_blessing_gained']} temporary HP!**"
        )

    return "\n".join(lines)


def _turn_announcement(session: sessions.Session) -> str:
    """
    States whose turn it is, what round it is, and (per Coffee,
    2026-07-18: "tell them the enemies/players still alive with hp like
    an RPG style battle... will help the player know which to target")
    a full roster of everyone still standing on both sides with their
    real current HP -- so a player doesn't have to scroll back through
    the combat log to remember who's already down before picking a
    target.
    """
    current = session.current_participant()
    label = f"{current['name']} (AI)" if current.get("is_ai") else current["name"]

    def roster_line(p: dict) -> str:
        tag = " (AI)" if p.get("is_ai") else ""
        return f"{p['name']}{tag}: {p['hp_current']}/{p.get('hp_max', p['hp_current'])} HP"

    party_roster = ", ".join(roster_line(p) for p in session.living_on_side("party")) or "none left standing"
    enemy_roster = ", ".join(roster_line(p) for p in session.living_on_side("enemy")) or "none left standing"

    hint = (
        "\n💡 Tap a button below, or just type what you want to do."
        if not current.get("is_ai") else ""
    )
    return (
        f"🎲 **Round {session.round_number}** — It's now **{label}**'s turn! What do you do?\n"
        f"⚔️ Party: {party_roster}\n"
        f"👹 Enemy: {enemy_roster}"
        f"{hint}"
    )


def _battle_menu_keyboard(session: sessions.Session) -> InlineKeyboardMarkup | None:
    """
    Top-level RPG-style battle menu (Fight/Skills/Items/Run) for whoever
    the CURRENT participant is -- per Coffee (2026-07-18): "create an
    RPG style battle menu for battles... Fight, Skills/Magic/Abilities
    (depending on Char), Items (backpack), Run." Returns None for an
    AI's turn (no human there to tap anything) or if the current
    participant isn't a real character lookup (defensive).

    Skills/Items are only shown if this SPECIFIC character actually has
    real known spells / carried consumables -- grounded in their own
    data, never a generic fixed list, so this naturally covers every
    race/class combination (a martial Fighter with no spells just never
    sees an empty, dead-end Skills button; a caster with nothing in
    their backpack never sees an empty Items button) without any
    per-class special-casing.
    """
    current = session.current_participant()
    if current.get("is_ai"):
        return None
    character = db.get_character(current["telegram_user_id"])
    if character is None:
        return None

    row = [InlineKeyboardButton("⚔️ Fight", callback_data="bm|fight")]
    if character.get("known_spells"):
        row.append(InlineKeyboardButton("✨ Skills", callback_data="bm|skills"))
    consumable_ids = [
        item_id for item_id, qty in character.get("inventory", {}).items()
        if qty > 0 and (items_module.get_item(item_id) or {}).get("type") == "consumable"
    ]
    if consumable_ids:
        row.append(InlineKeyboardButton("🎒 Items", callback_data="bm|items"))
    row.append(InlineKeyboardButton("🏃 Run", callback_data="bm|run"))
    return InlineKeyboardMarkup([row])


async def battle_menu_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """
    Handles taps on the RPG-style battle menu (_battle_menu_keyboard) --
    per Coffee's request (2026-07-18): "create an RPG style battle menu
    for battles... Fight, Skills/Magic/Abilities, Items, Run."

    Always re-verifies the tapper is genuinely the session's CURRENT
    turn participant before doing anything -- same "it's not your turn"
    boundary _do_attack/_do_flee/etc. already enforce for typed text, so
    someone else in the group can't act on a player's behalf by tapping
    their menu. Every actual game action is dispatched through those
    SAME real handlers (never duplicated here) -- a button tap and the
    equivalent typed sentence always produce identical results, same
    rules-decide/narrate split as everywhere else in this game.

    `update.effective_user`/`update.effective_chat` both resolve
    correctly for a callback_query Update (to the tapper and the
    group chat respectively) -- confirmed none of _do_attack/_do_flee/
    _do_use_item/_do_cast_spell ever reference update.message directly,
    so they work unchanged whether the action came from typed text or
    a button tap.
    """
    query = update.callback_query
    chat_id = update.effective_chat.id
    user_id = update.effective_user.id
    session = sessions.get_session(chat_id)
    if session is None or user_id not in session.turn_order or session.current_participant_id() != user_id:
        await query.answer("It's not your turn right now.", show_alert=True)
        return

    parts = (query.data or "").split("|")
    action = parts[1] if len(parts) > 1 else ""
    value = parts[2] if len(parts) > 2 else None
    target_name = parts[3] if len(parts) > 3 else None
    await _safe_answer(query)
    character = db.get_character(user_id)

    if action == "menu":
        await query.edit_message_reply_markup(reply_markup=_battle_menu_keyboard(session))
        return

    if action == "fight":
        opposing = session.living_on_side(session.opposing_side(user_id))
        if len(opposing) <= 1:
            await query.edit_message_reply_markup(reply_markup=None)
            if opposing:
                await _do_attack(update, f"attack {opposing[0]['name']}")
            else:
                async with sessions.get_lock(chat_id):
                    live_session = sessions.get_session(chat_id)
                    resolved = live_session is not None and await _try_end_stale_combat(update, live_session)
                if not resolved:
                    await update.effective_chat.send_message(
                        "No valid targets remain.", message_thread_id=config.TOPIC_ADVENTURE_ID,
                    )
            return
        buttons = [
            [InlineKeyboardButton(
                f"{p['name']} ({p['hp_current']}/{p.get('hp_max', p['hp_current'])} HP)",
                callback_data=f"bm|target|{p['name']}",
            )]
            for p in opposing
        ]
        buttons.append([InlineKeyboardButton("« Back", callback_data="bm|menu")])
        await query.edit_message_reply_markup(reply_markup=InlineKeyboardMarkup(buttons))
        return

    if action == "target":
        await query.edit_message_reply_markup(reply_markup=None)
        await _do_attack(update, f"attack {value}")
        return

    if action == "skills":
        known = character.get("known_spells", []) if character else []
        spell_buttons = [
            [InlineKeyboardButton(spells_module.get_spell(sid)["name"], callback_data=f"bm|cast|{sid}")]
            for sid in known if spells_module.get_spell(sid)
        ]
        spell_buttons.append([InlineKeyboardButton("« Back", callback_data="bm|menu")])
        await query.edit_message_reply_markup(reply_markup=InlineKeyboardMarkup(spell_buttons))
        return

    if action == "cast":
        spell = spells_module.get_spell(value)
        spell_name = spell["name"] if spell else value
        # Real bug (2026-07-19, per Coffee: "same with attacks and abilities
        # we shud be able to target"): this used to cast immediately with
        # no target named at all, so _do_cast_spell's own free-text parsing
        # always fell through to its default -- the first living enemy for
        # damage spells, always self for heal spells -- never a real choice.
        # "Fight" already prompts for a target (see the "fight"/"target"
        # actions above); damage and heal spells now get the same picker,
        # reusing that exact pattern, whenever there's more than one
        # sensible option. Utility effects (buff/negate/ac_bonus/summon/
        # resurrect) are unchanged -- summon has no target, resurrect needs
        # a specific DEAD party member (not a live-combat concept, since
        # Revivify only works outside combat per its own design), and the
        # rest are still flavor-only with no mechanical target yet.
        if spell and spell["effect"] == "damage":
            opposing = session.living_on_side(session.opposing_side(user_id))
            if len(opposing) > 1:
                buttons = [
                    [InlineKeyboardButton(
                        f"{p['name']} ({p['hp_current']}/{p.get('hp_max', p['hp_current'])} HP)",
                        callback_data=f"bm|casttarget|{value}|{p['name']}",
                    )]
                    for p in opposing
                ]
                buttons.append([InlineKeyboardButton("« Back", callback_data="bm|skills")])
                await query.edit_message_reply_markup(reply_markup=InlineKeyboardMarkup(buttons))
                return
        elif spell and spell["effect"] == "heal":
            own_side = session.sides.get(user_id)
            allies = session.living_on_side(own_side) if own_side else []
            if len(allies) > 1:
                buttons = [
                    [InlineKeyboardButton(
                        f"{p['name']} ({p['hp_current']}/{p.get('hp_max', p['hp_current'])} HP)"
                        + (" — you" if p["telegram_user_id"] == user_id else ""),
                        callback_data=f"bm|casttarget|{value}|{p['name']}",
                    )]
                    for p in allies
                ]
                buttons.append([InlineKeyboardButton("« Back", callback_data="bm|skills")])
                await query.edit_message_reply_markup(reply_markup=InlineKeyboardMarkup(buttons))
                return
        await query.edit_message_reply_markup(reply_markup=None)
        await _do_cast_spell(update, f"cast {spell_name}")
        return

    if action == "casttarget":
        spell = spells_module.get_spell(value)
        spell_name = spell["name"] if spell else value
        await query.edit_message_reply_markup(reply_markup=None)
        await _do_cast_spell(update, f"cast {spell_name} on {target_name}")
        return

    if action == "items":
        consumable_ids = [
            item_id for item_id, qty in (character.get("inventory", {}) if character else {}).items()
            if qty > 0 and (items_module.get_item(item_id) or {}).get("type") == "consumable"
        ]
        item_buttons = [
            [InlineKeyboardButton(items_module.get_item(iid)["name"], callback_data=f"bm|use|{iid}")]
            for iid in consumable_ids if items_module.get_item(iid)
        ]
        item_buttons.append([InlineKeyboardButton("« Back", callback_data="bm|menu")])
        await query.edit_message_reply_markup(reply_markup=InlineKeyboardMarkup(item_buttons))
        return

    if action == "use":
        item = items_module.get_item(value)
        item_name = item["name"] if item else value
        # Real bug (2026-07-19, per Coffee: "it shud ask who u want to use
        # it on - like a potion can be used on party members but it didnt
        # prompt that"): this used to use the item immediately with no
        # recipient named, so _do_use_item's own free-text parsing always
        # fell through to its default of self, even though heal/
        # cure_poison items can genuinely go on anyone in the fight.
        # Flavor-only consumables (rations, ale, torch, etc.) have no real
        # target choice either way, so they're unchanged.
        if item and item.get("effect") in ("heal", "cure_poison"):
            own_side = session.sides.get(user_id)
            allies = session.living_on_side(own_side) if own_side else []
            if len(allies) > 1:
                buttons = [
                    [InlineKeyboardButton(
                        f"{p['name']} ({p['hp_current']}/{p.get('hp_max', p['hp_current'])} HP)"
                        + (" — you" if p["telegram_user_id"] == user_id else ""),
                        callback_data=f"bm|usetarget|{value}|{p['name']}",
                    )]
                    for p in allies
                ]
                buttons.append([InlineKeyboardButton("« Back", callback_data="bm|items")])
                await query.edit_message_reply_markup(reply_markup=InlineKeyboardMarkup(buttons))
                return
        await query.edit_message_reply_markup(reply_markup=None)
        await _do_use_item(update, f"use {item_name}")
        return

    if action == "usetarget":
        item = items_module.get_item(value)
        item_name = item["name"] if item else value
        await query.edit_message_reply_markup(reply_markup=None)
        await _do_use_item(update, f"use {item_name} on {target_name}")
        return

    if action == "run":
        await query.edit_message_reply_markup(reply_markup=None)
        await _do_flee(update, "flee")
        return


async def _announce_defeats(update: Update, session: sessions.Session, removed: list[dict]) -> None:
    """
    Announces each defeated participant, wording it differently for a
    real player (who can recover by resting once combat ends) versus a
    monster or AI companion (a clean defeat announcement).
    """
    for entry in removed:
        session.log_event(f"{entry['name']} has been defeated!")
        if not entry["is_ai"] and session.sides.get(entry["telegram_user_id"]) == "party":
            await _safe_send(
                update,
                f"💀 **{entry['name']} has fallen!** They'll need to rest once "
                f"combat ends to recover (just say \"I rest\" in Adventure).",
            )
        else:
            await _safe_send(update, f"💀 **{entry['name']} has been defeated!**")


def _utf16_len(s: str) -> int:
    """
    Telegram's MessageEntity offset/length are counted in UTF-16 code
    units, not Python string indices -- most emoji this game narrates
    with (🏅🎲💨🔥 etc.) sit outside the Basic Multilingual Plane and
    take 2 UTF-16 units but only 1 Python character, so a naive
    len()/slice-based offset would silently misplace every entity that
    comes after one. Encoding to UTF-16LE and halving the byte count is
    the standard, exact way to get the real count.
    """
    return len(s.encode("utf-16-le")) // 2


def _build_message_entities(text: str, mentionable_players: list[dict]) -> tuple[str, list[MessageEntity]]:
    """
    Converts this game's one hand-authored markup convention --
    **bold** -- into real Telegram formatting entities, and turns any
    real player's character name mentioned in the text into a genuine
    text_mention entity (so Telegram actually notifies that player),
    instead of ever relying on Telegram's own Markdown parser at all.

    Two real bugs this fixes at once (both found live 2026-07-18):
    - Task #158: bot.py never set parse_mode anywhere, so every single
      "**bold**" marker in every narration/achievement/combat message
      this whole project has ever sent rendered as LITERAL asterisks to
      real players -- confirmed by re-examining a live screenshot.
    - Task #118: narration naming a real player was plain text, never
      a real Telegram mention (no notification, no tap-to-profile).

    Deliberately does NOT just flip on parse_mode=Markdown -- this
    project already got burned by that once (scripts/announce_deploy.py,
    2026-07-14: a bare underscore in dynamic text like "talk_npc" reads
    as an unmatched italic marker to Telegram's parser and returns a 400
    Bad Request). _safe_send already retries then SILENTLY DROPS a
    message that fails to send, so naive parse_mode would trade a
    cosmetic bug for real, silent message loss on any narration
    containing a bare _, *, `, or [ -- entirely plausible in freeform
    Ollama prose or an item/NPC name. Pre-computing exact entities here
    instead means there is nothing left for Telegram to parse or get
    wrong; this text is always sent as fully literal/plain.
    """
    bold_marker_positions = [m.start() for m in re.finditer(r"\*\*", text)]
    bold_spans_by_pair_count = len(bold_marker_positions) - (len(bold_marker_positions) % 2)

    out_chars: list[str] = []
    bold_char_spans: list[tuple[int, int]] = []  # (start, length) in the OUTPUT (marker-stripped) text
    i = 0
    marker_index = 0
    open_start = None
    while i < len(text):
        if marker_index < bold_spans_by_pair_count and text[i:i + 2] == "**" and bold_marker_positions[marker_index] == i:
            if open_start is None:
                open_start = len(out_chars)
            else:
                bold_char_spans.append((open_start, len(out_chars) - open_start))
                open_start = None
            marker_index += 1
            i += 2
            continue
        out_chars.append(text[i])
        i += 1
    clean_text = "".join(out_chars)

    entities: list[MessageEntity] = []
    for start, length in bold_char_spans:
        entities.append(MessageEntity(
            type=MessageEntity.BOLD,
            offset=_utf16_len(clean_text[:start]),
            length=_utf16_len(clean_text[start:start + length]),
        ))

    for player in mentionable_players:
        name = player.get("name")
        telegram_user_id = player.get("telegram_user_id")
        if not name or not telegram_user_id:
            continue
        for m in re.finditer(r"\b" + re.escape(name) + r"\b", clean_text):
            entities.append(MessageEntity(
                type=MessageEntity.TEXT_MENTION,
                offset=_utf16_len(clean_text[:m.start()]),
                length=_utf16_len(name),
                user=User(id=telegram_user_id, first_name=name[:64], is_bot=False),
            ))

    return clean_text, entities


async def _safe_send(
    update: Update, text: str, thread_id: int | None = None,
    reply_markup: InlineKeyboardMarkup | None = None,
    speak: bool = True,
) -> None:
    """
    speak=False (2026-07-22, per Coffee: "don't use tts for menus and
    other things that don't need voice") skips _maybe_speak entirely
    for this send -- used at every plain informational/menu listing
    (character sheet, inventory, quests journal/board, shop, market,
    bestiary, weather, leaderboard, achievements, skill tree, party
    roster, waypoints, visual map caption, the menu screens
    themselves). Defaults to True so real narration (location
    descriptions, combat, NPC dialogue, examine/story text) is
    unaffected -- this only ever narrows what gets voiced, never
    silences something that already didn't speak.

    Sends a message to a topic (Adventure by default), but never lets a
    transient Telegram/network failure escape and abort whatever state
    progression the caller still needs to make (turn advancement,
    combat-over checks, etc.). Confirmed live: a bare ConnectTimeout
    during an ordinary attack-narration send propagated all the way up
    through _resolve_ai_turns and _do_attack, aborting BEFORE
    session.advance_turn() ever ran — permanently soft-locking that
    combat on the crashed participant's turn, since nothing else ever
    re-attempts resolving it. Mechanical state (damage, HP) is decided
    and applied before this is ever called, so a lost message here is a
    real but survivable narration gap, not a lost game fact — unlike
    turn progression getting stuck, which has no other recovery path
    short of the "cancel" escape hatch force-ending the whole fight.

    Also confirmed live (2026-07-10) for the SAME underlying failure
    mode outside combat: development_topic_handler and
    support_topic_handler each send their final reply — after an
    already-expensive 30-160s Ollama call — via a raw send_message with
    no exception handling, so a plain telegram.error.TimedOut on that
    one call silently threw away the answer Coffee had already waited
    for. Both now route through here with their own thread_id instead.

    2026-07-12: confirmed live again -- a transient TimedOut on this
    call was simply logged and dropped, with no retry at all, silently
    eating a real reply Coffee had already waited minutes for (an
    Ollama-narrated check_quests response). One retry, after a short
    pause, catches exactly this kind of one-off network blip without
    turning a genuinely broken connection into a long hang -- the
    caller's already-decided game state doesn't depend on this send
    succeeding either way, so a short, bounded retry is pure upside.

    2026-07-22: confirmed live a THIRD failure mode, reported as "we
    can't do anything while there's an ongoing battle" (Coffee) and a
    waypoint tap silently doing nothing (Sugar) -- neither /menu nor
    the waypoint button have any combat-gating code at all, so the
    real cause wasn't a gate, it was volume: heavy concurrent send
    traffic from an active AI-party fight saturating this same chat
    tripped Telegram's actual flood control (a genuine 429 + `RetryAfter
    ("Flood control exceeded. Retry in 5 seconds")`), and the single
    flat 2s-then-give-up retry above wasn't enough to survive a burst
    -- the log shows five separate real replies hit "giving up" inside
    one eight-second window at the exact timestamp of Coffee's report.
    Now retries up to 3 times total and, on a RetryAfter specifically,
    sleeps for the duration Telegram actually asked for (plus a small
    margin) instead of a flat guess -- giving bursts a real chance to
    clear before a reply is dropped.
    """
    is_buffering = isinstance(update.effective_chat, _BufferingChatProxy)
    if is_buffering:
        # A compound-message buffer just collects raw sub-action text to
        # be joined and re-sent through this same function later (see
        # adventure_master_handler) -- entities would need recomputing
        # against the FINAL joined text anyway, so there's nothing to
        # build yet on this leg.
        clean_text, entities = text, []
    else:
        clean_text, entities = _build_message_entities(text, db.list_all_active_real_players())

    # thread_id is None here for TWO different reasons that must NOT
    # collapse to the same behavior: "caller didn't specify a topic"
    # (the long-standing convention -- default to Adventure) vs.
    # "caller explicitly means the Main/General topic," which Telegram's
    # API only accepts as an OMITTED message_thread_id, never an
    # explicit numeric id (confirmed live 2026-07-18: passing
    # config.TOPIC_MAIN_ID caused every _notify_main_topic send to fail
    # with BadRequest('Message thread not found'), even though that same
    # id is the right value for RECOGNIZING an incoming Main-topic
    # message). _MAIN_TOPIC_SEND disambiguates the two at the call site.
    if thread_id is _MAIN_TOPIC_SEND:
        resolved_thread_id = None
    elif thread_id is None:
        resolved_thread_id = config.TOPIC_ADVENTURE_ID
    else:
        resolved_thread_id = thread_id

    max_attempts = 3
    for attempt in range(max_attempts):
        try:
            await update.effective_chat.send_message(
                clean_text, message_thread_id=resolved_thread_id,
                entities=entities or None, reply_markup=reply_markup,
            )
            if not is_buffering and speak:
                await _maybe_speak(update, text, resolved_thread_id)
            return
        except TelegramError as e:
            if attempt == max_attempts - 1:
                logger.warning(f"[message] send failed again, giving up: {e!r}")
            elif isinstance(e, RetryAfter):
                wait = e.retry_after + 1
                logger.warning(f"[message] flood control hit, waiting {wait}s then retrying: {e!r}")
                await asyncio.sleep(wait)
            else:
                logger.warning(f"[message] send failed, retrying: {e!r}")
                await asyncio.sleep(2)


async def _safe_answer(query) -> bool:
    """
    Real live bug (2026-07-19, caught via monitoring): every callback-
    query handler in the game (battle menu, shop, spell, quest, item,
    menu, equip, level) called a bare `await query.answer()` with no
    exception handling. Telegram invalidates a callback query after a
    few minutes (or if the underlying message got too old) -- tapping a
    stale button then raised an unhandled `telegram.error.BadRequest`
    ("Query is too old and response timeout expired or query id is
    invalid"), caught only by the global error handler, which aborted
    the callback before any of its real dispatch logic ever ran. The
    player's tap did nothing except produce an error, with no feedback
    that the button had simply expired.

    Returns True if the query was answered successfully (the normal
    case), False if it had already expired/was invalid -- callers can
    use this to still fall through to a plain-text nudge instead of
    silently doing nothing.
    """
    try:
        await query.answer()
        return True
    except TelegramError as e:
        logger.warning(f"[callback] query.answer() failed (likely expired): {e!r}")
        return False


# Sentinel passed as _safe_send's thread_id to mean "the Main/General
# topic, explicitly" -- see the comment inside _safe_send for why this
# can't just be None (None already means "unspecified, default to
# Adventure").
_MAIN_TOPIC_SEND = object()


async def _notify_main_topic(update: Update, text: str) -> None:
    """
    Task #172, per Coffee: "the main chat can be used for all game
    notifications for the player to free up clutter in the Adventure
    topic" -- player leveled up, accepted a quest, joined a guild, died,
    or entered battle. A short one-line ping to Main, alongside (never
    instead of) the full narration that still goes to Adventure as
    normal -- this never replaces or gates the real Adventure-topic
    message, it's purely an additional heads-up so players who only
    watch Main don't miss the big moments. Routed through the same
    _safe_send retry/entity-mention machinery as every other message.
    """
    await _safe_send(update, text, thread_id=_MAIN_TOPIC_SEND)


@contextlib.asynccontextmanager
async def _keep_typing(chat, thread_id: int | None):
    """
    Shows Telegram's "typing..." indicator for as long as the wrapped
    block runs (2026-07-17, per Coffee: "when pandorammo bot is loading
    can it show that it is typing?? so players know there is something
    processing"). Telegram only displays the indicator for ~5s per
    call, so this refreshes it in a background task every 4s until the
    block exits -- covers the genuinely long, 30-160s+ real Ollama
    narration/classification calls documented in CLAUDE.md, where a
    player would otherwise stare at silence with no sign anything is
    happening. Purely cosmetic: any failure to send the indicator
    (including chat objects in tests that don't implement
    send_chat_action at all) is swallowed so it can never affect the
    real work being wrapped.
    """
    async def _loop():
        while True:
            try:
                await chat.send_chat_action(ChatAction.TYPING, message_thread_id=thread_id)
            except Exception:
                pass
            await asyncio.sleep(4)

    task = asyncio.create_task(_loop())
    try:
        yield
    finally:
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task


# Real live feedback (2026-07-16, Coffee): the old flat 2s delete delay
# was too fast to actually tap the "/tts <text>" message and generate
# the audio before it vanished. Bumped to a real, comfortable window.
TTS_TRIGGER_DELETE_DELAY_SECONDS = 20


async def _maybe_speak(update: Update, text: str, thread_id: int | None) -> None:
    """
    Optional TTS narration, off by default, toggled via
    db.get_setting("tts_enabled") (see the Development-topic
    "turn on/off tts" command). Two interchangeable backends, selected
    via db.get_setting("tts_backend") ("textbot", the original and
    still the default, or "piper", added 2026-07-18 -- see
    ai/tts_piper.py):

    - "textbot": @TextTSBot, already added to this group (2026-07-14)
      -- confirmed live it responds to another bot's own /tts command,
      not just a real player's, and Coffee picked a voice (Alan,
      Australian) he likes for the group. One shared voice,
      account-wide, not something safe to switch per-message.
    - "piper": a real local voice-note, no third-party bot dependency,
      via a small on-CPU neural TTS model (see the POC that confirmed
      this doesn't meaningfully contend with Ollama's single
      generation slot on this box).

    Either way this is the "read narration aloud" slice, not the full
    per-character-voice item still on the backlog. Skipped during a
    compound-message's buffered sub-sends (see adventure_master_handler)
    so a multi-action message triggers exactly one narrated voice, not
    one per sub-action.
    """
    if db.get_setting("tts_enabled") != "1":
        return
    speakable = to_speakable_text(text)
    if not speakable:
        return
    if db.get_setting("tts_backend", "textbot") == "piper":
        await _speak_via_piper(update, speakable, thread_id, _pick_piper_voice(text))
        return
    try:
        # thread_id here is already fully resolved by _safe_send's one
        # call site (its only caller) -- None genuinely means Main/
        # General (no thread), not "unspecified, default to Adventure"
        # -- so it must be passed through as-is, not re-defaulted.
        trigger_message = await update.effective_chat.send_message(
            f"/tts {speakable}",
            message_thread_id=thread_id,
        )
    except TelegramError as e:
        logger.warning(f"[tts] failed to trigger TextTSBot: {e!r}")
        return

    # Confirmed live 2026-07-14 (Coffee): triggering @TextTSBot this way
    # leaves our own "/tts <text>" command sitting in the chat as a
    # real, visible SECOND copy of the narration alongside the actual
    # text reply -- Telegram has no way to send another bot a command
    # without it being a real message. @TextTSBot already receives its
    # own copy of this update the instant it's sent (independent of
    # what happens to the message afterward), so deleting our own copy
    # shortly after doesn't affect whether it actually speaks -- just
    # cleans up the visible duplicate. Scheduled as a background task,
    # not awaited here, so this doesn't add a delay to every TTS-enabled
    # message's real send path -- a short delay before deleting is a
    # safety margin against @TextTSBot's own polling interval, not
    # something the caller needs to wait on.
    async def _delete_trigger_after_delay():
        await asyncio.sleep(TTS_TRIGGER_DELETE_DELAY_SECONDS)
        try:
            await trigger_message.delete()
        except TelegramError as e:
            logger.warning(f"[tts] couldn't delete the trigger message afterward: {e!r}")

    asyncio.create_task(_delete_trigger_after_delay())


def _pick_piper_voice(raw_text: str) -> str:
    """
    Two real voices (2026-07-22, task #97 "better TTS" follow-up), not
    per-individual-NPC ones -- see ai/tts_piper.py's module docstring
    for why. Every NPC dialogue line in this game is already built from
    the same "💬 **Name:**" marker (bot.py's talk_npc/AI-chatter/quest-
    offer sends), a real structural fact already in the text, not a
    guess -- checked against the RAW pre-cleanup text, since
    to_speakable_text's own emoji stripping would otherwise remove the
    very marker this depends on.
    """
    return "npc" if raw_text.strip().startswith("💬") else tts_piper.DEFAULT_VOICE_KEY


async def _speak_via_piper(update: Update, speakable: str, thread_id: int | None, voice_key: str) -> None:
    """
    Local TTS backend (2026-07-18) -- see ai/tts_piper.py. Synthesis is
    CPU-bound, same asyncio.to_thread convention as every other
    Ollama/AI call in this codebase so it doesn't block the event loop.
    Sent via send_audio, not send_voice: Telegram only renders the
    compact voice-note bubble for genuine OGG/Opus, and re-encoding to
    that format would require shelling out to ffmpeg -- a subprocess
    call this project's CLAUDE.md explicitly forbids adding anywhere in
    this codebase. A plain WAV audio attachment still plays natively in
    every Telegram client, just as a music-style bubble instead.
    """
    wav_bytes = await asyncio.to_thread(tts_piper.synthesize_to_wav_bytes, speakable, voice_key)
    if wav_bytes is None:
        return
    try:
        # thread_id is already fully resolved by _safe_send (same note
        # as _maybe_speak above) -- None genuinely means Main/General.
        await update.effective_chat.send_audio(
            audio=wav_bytes,
            filename="narration.wav",
            title="Pandora MMO narration",
            message_thread_id=thread_id,
        )
    except Exception as e:
        # Real live bug (2026-07-22): this only ever caught TelegramError,
        # so a non-Telegram failure (an AttributeError from a synthetic
        # Update shim missing send_audio, see _ChatOnlyUpdate._Chat's own
        # fix) propagated all the way up and silently aborted the
        # caller's ENTIRE turn -- an optional TTS narration hiccup should
        # never do that, same "never breaks the real feature it
        # accompanies" guarantee this function's own docstring already
        # claims but didn't actually keep for non-Telegram errors.
        logger.warning(f"[tts] piper send_audio failed: {e!r}")


def _personality_for_character_name(name: str) -> str | None:
    """
    Looks up a known NPC/AI companion's personality by their display name
    (recruited companions and hostile/ambient world-NPCs are both stored
    in CAMPAIGN["npcs"], keyed by id but carrying their real display
    name) — lets their combat turns read in character, not as generic
    monster-attack prose. Returns None for plain monsters and real
    players, who have no registered persona.
    """
    for npc_data in CAMPAIGN["npcs"].values():
        if npc_data["name"] == name:
            return npc_data.get("personality")
    return None


async def _announce_reaction(update: Update, defender: dict, result: dict) -> None:
    """
    A short, deterministic (no Ollama call -- reactions happen often
    enough in a fight that adding another 30-160s narration call per
    trigger isn't worth it) follow-up line so a Shield/Uncanny Dodge
    trigger is never silently invisible to players -- without this, an
    attack that "should" have hit going wide, or damage being oddly
    halved, would look like an unexplained inconsistency rather than a
    real reaction firing.
    """
    if result.get("shield_reaction_triggered"):
        await _safe_send(update, f"🛡️ **{defender['name']}** casts Shield as a reaction — the attack goes wide!")
    elif result.get("uncanny_dodge_triggered"):
        await _safe_send(update, f"🌀 **{defender['name']}** uses Uncanny Dodge, halving the damage!")


async def _post_narrated(update: Update, character: dict, action_text: str,
                          mechanical_result: dict, session: sessions.Session,
                          action_label: str | None = None) -> None:
    actor_personality = _personality_for_character_name(character.get("name", ""))
    location = cl.get_location(CAMPAIGN, character.get("current_location"))
    location_description = location["description"] if location else None
    flavor = await asyncio.to_thread(
        narrate_action, character, action_text, mechanical_result, session.recent_events(),
        actor_personality, location_description,
    )
    message = _format_combat_result(
        flavor, mechanical_result,
        actor_label=mechanical_result.get("attacker", character.get("name", "?")),
        defender_label=mechanical_result.get("defender", "?"),
        action_label=action_label,
    )
    session.log_event(f"{mechanical_result.get('attacker')} vs {mechanical_result.get('defender')}: {flavor}")
    await _safe_send(update, message)


def _refresh_real_player_spell_slots(character: dict) -> None:
    """
    Keeps the in-memory combat participant dict's spell_slots_current
    honest for a real player right before an attack that might trigger
    Shield's reaction (2026-07-15). _do_cast_spell spends slots via a
    direct, separate DB write (db.spend_spell_slot) that never touches
    session.participants, so a player who cast a spell earlier in the
    same fight would otherwise have Shield check a stale, too-high slot
    count here. AI companions never cast spells on their own turn (see
    _resolve_ai_turns -- only a plain attack), so their in-memory value
    can't go stale this way and doesn't need refreshing.
    """
    if character.get("is_ai"):
        return
    fresh = db.get_character(character["telegram_user_id"])
    if fresh:
        character["spell_slots_current"] = fresh["spell_slots_current"]


def _sync_player_to_db(character: dict) -> None:
    """
    Persists a real player's combat-mutated state (HP, death saves,
    spell slots) back to the database. Combat mutates participant dicts
    in-memory only — without this, damage taken mid-fight would
    silently vanish the moment combat ends or another /sheet lookup
    re-reads the database. AI companions/monsters have no database row
    to sync (negative synthetic IDs), so this is a no-op for them.
    spell_slots_current is included here (2026-07-15) because Shield's
    reaction spends a real slot mid-combat now — without persisting it,
    a defender could Shield away every hit for free every fight.
    """
    if character.get("is_ai"):
        return
    db.update_character(
        character["telegram_user_id"],
        hp_current=character["hp_current"],
        death_save_successes=character.get("death_save_successes", 0),
        death_save_failures=character.get("death_save_failures", 0),
        spell_slots_current=character.get("spell_slots_current", 0),
    )


INACTIVE_PARTY_XP_SHARE = 0.10  # per Coffee (2026-07-16): 5-10% for absent party members


def _level_up_note(before: dict, after: dict) -> str:
    note = (
        f"🎉 {after['name']} leveled up to {after['level']}! "
        f"(HP max: {before['hp_max']} → {after['hp_max']})"
    )
    if after.get("pending_asi_points"):
        note += f"\nYou have {after['pending_asi_points']} ability point(s) to spend — say \"level up\" whenever you're ready."
    return note


async def _award_xp_and_announce_level_up(update_like, telegram_user_id: int, amount: int) -> dict | None:
    """
    Real live bug (2026-07-19, Coffee: "it idnt give a notification on
    my level up"): _award_victory_xp (combat) has always compared
    before/after level and posted _level_up_note to Main via
    _notify_main_topic, but every NON-combat XP source -- story quest
    rewards (_complete_quest_and_announce), board quest turn-ins
    (_check_board_quest_turnin), branching quest choices
    (_do_resolve_quest_choice), and login streak bonuses
    (_maybe_award_streak_bonus) -- called db.add_xp directly with no
    level-up check at all, so leveling up from any of those sources
    produced zero notification, silently. This wraps db.add_xp with the
    same before/after check + announcement combat already gets, so
    every XP source behaves identically.
    """
    before = db.get_character(telegram_user_id)
    if before is None:
        return None
    after = db.add_xp(telegram_user_id, amount)
    if after is not None and after["level"] > before["level"]:
        await _notify_main_topic(update_like, _level_up_note(before, after))
    return after


def _award_victory_xp(session: sessions.Session) -> tuple[str, list[str]]:
    """
    Awards real XP (from the defeated monster's real 5E-sourced XP value)
    to every real (non-AI) party member still in the fight, split evenly
    per standard 5E group-XP conventions. Returns (summary, level_up_notes)
    -- summary is a string to append to the victory message (empty if
    nothing to award); level_up_notes is the same level-up lines already
    folded into summary, returned separately too so callers can also post
    each one to Main (task #172, per Coffee: level-ups are one of the
    events that should surface there, not just buried in Adventure).
    Only ever called when the party won, so any enemy participant tagged
    with source_npc_id (a named hostile world-NPC, not a generic monster)
    is genuinely defeated here — permanently removed from future ambient
    encounters via _DEFEATED_NPCS, with a real, persistent faction-
    standing consequence for every real player who took part (db.
    adjust_faction_standing) — killing a faction's member sours that
    whole faction on you, not just that one NPC.
    """
    real_party_ids_all = [
        pid for pid in session.turn_order
        if session.sides.get(pid) == "party"
        and not next(p for p in session.participants if p["telegram_user_id"] == pid).get("is_ai")
    ]

    # Task #170, per Coffee: guild membership now requires proving
    # yourself in real combat first, not an instant join (see
    # guilds.eligible_for_guild) -- this is the one place a real party
    # win is already known to have happened, so it's the natural spot to
    # set that flag, once, the first time it happens.
    newly_proven_names = []
    for pid in real_party_ids_all:
        proving_character = db.get_character(pid)
        if proving_character and not proving_character.get("proven_in_combat"):
            db.update_character(pid, proven_in_combat=1)
            newly_proven_names.append(proving_character["name"])

    for p in session.participants:
        if session.sides.get(p["telegram_user_id"]) == "enemy" and p.get("source_npc_id"):
            npc_id = p["source_npc_id"]
            _DEFEATED_NPCS.add(npc_id)
            faction_id = _faction_for_npc(npc_id)
            if faction_id:
                for pid in real_party_ids_all:
                    _adjust_faction_standing(pid, faction_id, -15, _faction_starting_standing(faction_id))

    enemy_xp_total = sum(
        p.get("xp_reward", 0) for p in session.participants
        if session.sides.get(p["telegram_user_id"]) == "enemy"
    )
    if enemy_xp_total <= 0:
        return "", []

    real_party_ids = real_party_ids_all
    if not real_party_ids:
        return "", []

    xp_each = max(enemy_xp_total // len(real_party_ids), 1)
    level_up_notes = []
    event_location = None
    for pid in real_party_ids:
        before = db.get_character(pid)
        after = db.add_xp(pid, xp_each)
        event_location = event_location or after.get("current_location")
        if after["level"] > before["level"]:
            level_up_notes.append(_level_up_note(before, after))
            _log_world_event(event_location, f"{after['name']} reached level {after['level']}.")

    # Inactive party members' cut (2026-07-16, per Coffee): "inactive
    # party members shud get a small % of exp for their parties tasks
    # ... this will incentivize party members to grind for the party."
    # A real formal party (character.party_id, db.get_party_members_by_id)
    # can have members who aren't in THIS fight -- off resting, at a
    # shop, gathering elsewhere -- who still get INACTIVE_PARTY_XP_SHARE
    # (10%, the top of Coffee's stated 5-10% range) of the full xp_each
    # a fighter earned, once per real absent member, never doubled if
    # they happen to be in more than one active fighter's party (a
    # player can only be in one party at a time anyway).
    absent_bonus_recipients: set[int] = set()
    for fighter_pid in real_party_ids:
        fighter = db.get_character(fighter_pid)
        party_id = fighter.get("party_id") if fighter else None
        if not party_id:
            continue
        for member in db.get_party_members_by_id(party_id):
            member_pid = member["telegram_user_id"]
            if member.get("is_ai") or member_pid in real_party_ids or member_pid in absent_bonus_recipients:
                continue
            absent_bonus_recipients.add(member_pid)
            bonus_xp = max(int(xp_each * INACTIVE_PARTY_XP_SHARE), 1)
            before = db.get_character(member_pid)
            after = db.add_xp(member_pid, bonus_xp)
            if after["level"] > before["level"]:
                level_up_notes.append(_level_up_note(before, after))
                _log_world_event(event_location, f"{after['name']} reached level {after['level']}.")

    enemy_names = [
        p["name"] for p in session.participants
        if session.sides.get(p["telegram_user_id"]) == "enemy"
    ]
    if enemy_names:
        _log_world_event(event_location, f"The party defeated {', '.join(enemy_names)}.")

    # Task #75: if this victory just defeated the currently-active world
    # boss (_maybe_spawn_world_boss, background world tick), clear its
    # persisted state (db.get_setting/set_setting -- survives restarts,
    # same convention task #159 established) so a new one can eventually
    # spawn, hand out a real bonus on top of the loot/XP already above,
    # and fold the announcement into level_up_notes -- every one of this
    # function's 5 call sites already loops over level_up_notes and posts
    # each line to Main via _notify_main_topic, so this needs no new
    # plumbing at any call site.
    world_boss_note = None
    active_world_boss_raw = db.get_setting("active_world_boss")
    if active_world_boss_raw:
        try:
            active_world_boss = json.loads(active_world_boss_raw)
        except (json.JSONDecodeError, TypeError):
            active_world_boss = None
        defeated_boss = next(
            (p for p in session.participants
             if session.sides.get(p["telegram_user_id"]) == "enemy"
             and active_world_boss and p.get("monster_key") == active_world_boss.get("monster_key")),
            None,
        )
        if defeated_boss is not None:
            db.set_setting("active_world_boss", "")
            for pid in real_party_ids:
                character = db.get_character(pid)
                db.update_character(pid, gold=character["gold"] + WORLD_BOSS_BONUS_GOLD)
            victor_names = ", ".join(db.get_character(pid)["name"] for pid in real_party_ids)
            world_boss_note = (
                f"🌍 **World Boss Defeated!** {victor_names} brought down the {defeated_boss['name']} "
                f"— {WORLD_BOSS_BONUS_GOLD} bonus gold each! The world breathes easier, for now..."
            )

    board_notes = []
    if event_location:
        # Location-scoped, not day_key-scoped (task #149, 2026-07-17) --
        # see get_accepted_board_quests_at_location's docstring.
        for board_quest in db.get_accepted_board_quests_at_location(event_location):
            if board_quest["objective_type"] != "defeat_monster":
                continue
            defeated_matching = sum(
                1 for p in session.participants
                if session.sides.get(p["telegram_user_id"]) == "enemy"
                and p.get("monster_key") == board_quest["objective_target"]
            )
            if not defeated_matching:
                continue
            updated = db.record_board_quest_progress(board_quest["board_quest_id"], defeated_matching)
            if updated["progress_count"] >= updated["objective_count"]:
                if updated.get("branch_data"):
                    # Branching quests never auto-reward — a real choice
                    # has to be made first (see _do_resolve_quest_choice).
                    board_notes.append(
                        f"\n📜 **{updated['title']}** — objective complete. A decision awaits "
                        f"(check quests to see the choice)."
                    )
                else:
                    db.complete_board_quest(updated["board_quest_id"])
                    for pid in real_party_ids:
                        # Same real bug as the other non-combat XP sources
                        # (2026-07-19, Coffee) -- this board-quest reward
                        # never checked for a level-up crossing, so it
                        # never made it into level_up_notes/Main at all.
                        before = db.get_character(pid)
                        after = db.add_xp(pid, updated["reward_xp"])
                        if after["level"] > before["level"]:
                            level_up_notes.append(_level_up_note(before, after))
                            _log_world_event(event_location, f"{after['name']} reached level {after['level']}.")
                        character = db.get_character(pid)
                        db.update_character(pid, gold=character["gold"] + updated["reward_gold"])
                        db.increment_board_quests_completed(pid)
                    board_notes.append(
                        f"\n📜 **Board quest complete: {updated['title']}!** "
                        f"Party earns {updated['reward_xp']} XP, {updated['reward_gold']} gold each."
                    )
            else:
                board_notes.append(
                    f"\n📋 Board quest progress: {updated['title']} "
                    f"({updated['progress_count']}/{updated['objective_count']})"
                )

    # Bonus gear loot: rules/item_generator.py procedurally rolls a
    # fresh weapon/armor dict on demand (tier-weighted stats, no stable
    # item_id) rather than picking from items.py's fixed catalog, so it
    # can't be added to inventory or equipped via db.equip_item (2026-
    # 07-15, which DOES now make items.py's real catalog weapons/armor
    # affect combat) -- framed as sold in town instead. Its price
    # (already tier-weighted via roll_tier, same as every other rules/
    # module) becomes a real, varied gold reward instead of a flat number.
    loot_item = generate_item(item_type=random.choice(["weapon", "armor"]))
    gold_each = max(loot_item["price"] // len(real_party_ids), 1)
    for pid in real_party_ids:
        character = db.get_character(pid)
        db.update_character(pid, gold=character["gold"] + gold_each)
    loot_line = (
        f"\n💰 The party loots a **{loot_item['name']}** from the fallen — not worth "
        f"carrying, so it's sold in town for {gold_each} gold each."
    )

    # Task #141 (discoverable half): a rare chance for the fallen to
    # also be carrying a real map -- the same fixed items.py catalog
    # items sold at Vane's Curiosities, just found instead of bought
    # this time. Independent of the gear-loot roll above (both can hit
    # the same fight). Given to one random real party member since a
    # map isn't meaningfully split like gold/XP the way loot gold is.
    map_note = ""
    if random.random() < MAP_LOOT_DROP_CHANCE:
        map_item_id = random.choice(["weathered_surface_map", "tattered_underground_chart"])
        map_item = items_module.get_item(map_item_id)
        finder_id = random.choice(real_party_ids)
        db.add_item(finder_id, map_item_id, 1)
        finder_name = db.get_character(finder_id)["name"]
        map_note = f"\n🗺️ **{finder_name}** finds a {map_item['name']} tucked away on the fallen!"

    summary = f"\n✨ Party gains {xp_each} XP each ({enemy_xp_total} total)."
    if absent_bonus_recipients:
        bonus_xp = max(int(xp_each * INACTIVE_PARTY_XP_SHARE), 1)
        summary += (
            f" Absent party member(s) still earn {bonus_xp} XP each "
            f"({int(INACTIVE_PARTY_XP_SHARE * 100)}% share) for the party's efforts."
        )
    summary += loot_line
    summary += map_note
    summary += "".join(board_notes)
    if newly_proven_names:
        summary += (
            f"\n🛡️ {', '.join(newly_proven_names)} proved themselves in real combat — "
            f"eligible to join a guild now."
        )
    if world_boss_note:
        level_up_notes.append(world_boss_note)
    if level_up_notes:
        summary += "\n" + "\n".join(level_up_notes)
    return summary, level_up_notes


def _apply_asi_choice(character: dict, text: str) -> str | None:
    """
    Tries to resolve character's pending_asi_points from free text --
    either a named ability ("put it into constitution") or an auto/
    "do it for me" phrase (falls back to the class's old fixed primary
    ability, same stat the pre-2026-07-16 silent system always used).
    Returns the confirmation message, or None if the text named
    neither, so the caller knows to prompt and wait instead.
    """
    pending = character.get("pending_asi_points", 0)
    if pending <= 0:
        return None
    lowered = text.lower()

    if any(w in lowered for w in _ASI_AUTO_WORDS):
        ability = CLASS_PRIMARY_ABILITY.get(character["char_class"].lower(), "strength")
        spend = pending
    else:
        ability = next((a for a in _ABILITY_NAMES if a in lowered), None)
        if ability is None:
            return None
        spend = min(pending, 2)  # real 5E: at most +2 into a single ability per ASI

    # Rebirth (2026-07-22, per Coffee): each rebirth raises this
    # character's own ability-score ceiling above the normal 20 (see
    # rules/leveling.py's ability_score_cap) -- real "godly" growth for
    # a character that's gone through the rebirth loop, while a
    # never-reborn character keeps the exact same 20 cap as before.
    before_value = character[ability]
    cap = ability_score_cap(character.get("rebirth_count", 0))
    after_value = min(before_value + spend, cap)
    updated = db.update_character(
        character["telegram_user_id"],
        **{ability: after_value},
        pending_asi_points=pending - spend,
    )
    note = f"📈 {ability.capitalize()} increased from {before_value} to {after_value}."
    if updated["pending_asi_points"] > 0:
        note += f" You still have {updated['pending_asi_points']} point(s) left to spend — say \"level up\" again."
    return note


async def _do_rebirth(update: Update) -> None:
    """
    Prestige/rebirth (2026-07-22, per Coffee: "keeps all stats but we
    go to lv one to exponentially level up our character again...
    maybe making for another rebirth"). Only level and xp reset --
    ability scores, HP, gear, gold, skill points, achievements, and
    every other stat stay exactly as they are, so this is never a
    power loss. The reward: a permanently higher ability-score cap
    (rules/leveling.py's ability_score_cap) and a stacking XP-gain
    bonus (xp_gain_multiplier) that make the climb back to MAX_LEVEL
    genuinely faster each time, plus (after the first rebirth) access
    to a freely-chosen hybrid class flavor -- see _do_choose_hybrid.
    Only available at MAX_LEVEL: a real endgame choice, not something
    to stumble into early and lose your level progress by accident.
    """
    character = db.get_character(update.effective_user.id)
    if character is None:
        await update.effective_chat.send_message(
            "You don't have a character yet!", message_thread_id=config.TOPIC_ADVENTURE_ID
        )
        return
    if character["level"] < MAX_LEVEL:
        await _safe_send(
            update,
            f"Rebirth is only available at level {MAX_LEVEL} — **{character['name']}** is level "
            f"{character['level']} right now.",
        )
        return

    new_rebirth_count = character.get("rebirth_count", 0) + 1
    updated = db.update_character(
        update.effective_user.id,
        level=1, xp=0, rebirth_count=new_rebirth_count,
    )
    new_cap = ability_score_cap(new_rebirth_count)
    new_xp_bonus = int(round((xp_gain_multiplier(new_rebirth_count) - 1.0) * 100))
    lines = [
        f"✨ **{updated['name']}** is reborn — level and XP reset to 1, but every stat, every "
        f"item, and every point already earned stays exactly as it was.",
        f"This is rebirth #{new_rebirth_count}: ability scores can now climb as high as {new_cap} "
        f"(instead of the usual 20), and XP gains are permanently boosted by {new_xp_bonus}%.",
    ]
    if new_rebirth_count == 1:
        lines.append("A hybrid class is now available — say \"become a hybrid [class]\" to pick one.")
    await _safe_send(update, "\n".join(lines))
    await _notify_main_topic(update, f"✨ **{updated['name']}** has been reborn (rebirth #{new_rebirth_count})!")


# Hybrid classes (2026-07-22, per Coffee): freely pick/re-pick any of
# the other 11 classes as a secondary flavor once you've rebirthed at
# least once. Depth (rules/leveling.py's hybrid_tier) is a direct
# function of total rebirth count, capped at HYBRID_MAX_TIER -- see
# HYBRID_CLASS_FEATURES below for what each tier actually grants.
HYBRID_CLASS_FEATURES = {
    "Barbarian": [
        "a real chance of resisting damage when hit, Rage-style",
        "a real chance of advantage on your own attacks, Reckless-Attack-style",
        "a real chance of bonus damage on a hit, Rage-style",
    ],
    "Fighter": [
        "a real chance of bonus damage on a hit",
        "a bigger real chance of that bonus damage",
        "a real chance of healing a little when you land a hit, Second-Wind-style",
    ],
    "Rogue": [
        "a real chance of bonus precision damage when attacking with advantage",
        "a bigger real chance of that bonus precision damage",
        "an even bigger real chance, with a bigger bonus",
    ],
    "Ranger": [
        "advantage vs. goblins, this campaign's most common real threat",
        "advantage vs. goblins AND wolves",
        "the same advantage, plus a real chance of bonus damage against a favored enemy",
    ],
    "Paladin": [
        "a real chance of healing a little when you land a hit",
        "a bigger real chance of that self-heal",
        "a real chance of bonus radiant damage on a hit, Divine-Smite-style",
    ],
    "Monk": [
        "a small AC bump while unarmored",
        "a bigger AC bump while unarmored",
        "the same AC bump, plus a real chance of bonus unarmed-strike damage",
    ],
    "Bard": [
        "a real chance of a little bonus damage, channeling inspiration into your own strike",
        "a bigger real chance of that bonus damage",
        "an even bigger real chance, with a bigger bonus",
    ],
    "Cleric": [
        "a real chance of a bonus heal when you cast a healing spell",
        "a bigger real chance of that bonus heal",
        "an even bigger real chance, with a bigger bonus",
    ],
    "Druid": [
        "a real chance of bonus damage on a hit, Wild-Shape-style",
        "a bigger real chance of that bonus damage, plus a little temp HP",
        "an even bigger real chance, with more of both",
    ],
    "Wizard": [
        "a real chance of recovering a little spell energy on rest, Arcane-Recovery-style",
        "a bigger real chance of that recovery",
        "an even bigger real chance, recovering more",
    ],
    "Sorcerer": [
        "a small AC bump while unarmored, Draconic-Resilience-style",
        "a bigger AC bump while unarmored",
        "the same AC bump, plus a real chance of recovering a little spell energy on rest",
    ],
    "Warlock": [
        "a real chance of temp HP when you drop a hostile to 0, Dark-One's-Blessing-style",
        "a bigger real chance of that temp HP",
        "an even bigger real chance, with more temp HP",
    ],
}


async def _do_choose_hybrid(update: Update, text: str) -> None:
    character = db.get_character(update.effective_user.id)
    if character is None:
        await update.effective_chat.send_message(
            "You don't have a character yet!", message_thread_id=config.TOPIC_ADVENTURE_ID
        )
        return
    tier = hybrid_tier(character.get("rebirth_count", 0))
    if tier <= 0:
        await _safe_send(
            update, "Hybrid classes unlock after your first rebirth — say \"rebirth\" once you're level 99.",
            speak=False,
        )
        return

    lowered = text.lower()
    match = next((c for c in HYBRID_CLASS_FEATURES if c.lower() in lowered), None)
    if match is None:
        options = ", ".join(HYBRID_CLASS_FEATURES)
        await _safe_send(update, f"Which class do you want as your hybrid flavor? Options: {options}")
        return
    if match == character["char_class"]:
        await _safe_send(update, f"**{character['name']}** is already a {match} — pick a different class to hybridize with.")
        return

    db.update_character(update.effective_user.id, hybrid_class=match)
    perk = HYBRID_CLASS_FEATURES[match][tier - 1]
    await _safe_send(
        update,
        f"🌟 **{character['name']}** takes on a **{match}** hybrid flavor (tier {tier}/{HYBRID_MAX_TIER}): {perk}.",
    )


async def _do_level_up(update: Update, text: str) -> None:
    """
    Real player-driven Ability Score Improvement (2026-07-16, per
    Coffee): replaces the old silent auto-apply-to-a-fixed-stat
    behavior. A character banks pending_asi_points at ASI levels
    (4/8/12/16/19, see db.add_xp) and spends them here, on their own
    schedule -- there's no expiry, matching real 5E's own "you can
    always defer an ASI" convention.
    """
    user_id = update.effective_user.id
    character = db.get_character(user_id)
    if character is None:
        await update.effective_chat.send_message(
            "You don't have a character yet!", message_thread_id=config.TOPIC_ADVENTURE_ID
        )
        return

    if character.get("pending_asi_points", 0) <= 0:
        await _safe_send(update, "No ability score improvements waiting to be spent right now.")
        return

    resolved = _apply_asi_choice(character, text)
    if resolved:
        _PENDING_ASI_CHOICE.discard(user_id)
        await _safe_send(update, resolved)
        return

    _PENDING_ASI_CHOICE.add(user_id)
    await _safe_send(
        update,
        f"You have {character['pending_asi_points']} ability point(s) to spend. Which ability would you "
        f"like to raise — Strength, Dexterity, Constitution, Intelligence, Wisdom, or Charisma? "
        f"(Say \"auto\" or \"do it for me\" to let the game choose.)",
    )


_INLINE_DESCRIPTION_RE = re.compile(
    r"description[^:]*:\s*(.+)", re.IGNORECASE
)


def _extract_inline_description(text: str) -> str | None:
    """
    A player asking to add a description usually just states intent with
    no content yet ("I'd like to add a character description to my
    player") -- but if they DID include the actual text in the same
    message ("set my description to: A grizzled dwarf who never
    smiles"), pull it out rather than prompting for something already
    given. Real bug caught by an isolated live test (2026-07-16): an
    earlier version of this regex matched on the bare words "to"/"is"/
    "as" with no colon required, so Coffee's own example phrasing --
    "I'd like to add a character description to my player", which has
    NO actual content in it -- got misread as content="my player" and
    silently saved as the description instead of prompting for one. A
    real, unambiguous colon is now required as the content separator,
    so bare trigger phrasing always falls through to the prompt.
    """
    match = _INLINE_DESCRIPTION_RE.search(text)
    if match:
        candidate = match.group(1).strip()
        return candidate or None
    return None


async def _do_set_description(update: Update, text: str, *, from_prompt: bool = False) -> None:
    """
    Character description (2026-07-16, per Coffee): freeform biography/
    appearance/personality text, settable at creation or any time after
    by asking ("I'd like to add a character description to my player").
    Shown on the character sheet once set.
    """
    user_id = update.effective_user.id
    character = db.get_character(user_id)
    if character is None:
        await _safe_send(update, "You don't have a character yet!")
        return

    description_text = text.strip() if from_prompt else _extract_inline_description(text)
    if description_text is None:
        _PENDING_DESCRIPTION.add(user_id)
        await _safe_send(
            update,
            "Sure — what would you like your character's description to be? (backstory, "
            "appearance, personality — whatever helps other players and NPCs get a sense of "
            "who they are.) Say \"skip\" to leave it blank.",
        )
        return

    if description_text.lower() in ("skip", "none", "no", "n/a", "nothing", "never mind"):
        await _safe_send(update, "No problem — you can add a description any time by asking.")
        return

    clean = " ".join(description_text.split())[:MAX_CHARACTER_DESCRIPTION_LENGTH]
    if not clean:
        _PENDING_DESCRIPTION.add(user_id)
        await _safe_send(update, "That didn't look like a description — try again?")
        return

    db.update_character(user_id, description=clean)
    await _safe_send(update, f"✅ Description saved for {character['name']}:\n\n{clean}")


_INLINE_PRONOUNS_RE = re.compile(
    r"pronouns?[^:]*:\s*(.+)", re.IGNORECASE
)


def _extract_inline_pronouns(text: str) -> str | None:
    """Same reasoning as _extract_inline_description: a real colon is required
    so a bare trigger phrase with no content ("set my pronouns") always falls
    through to the prompt instead of misreading trailing words as the answer."""
    match = _INLINE_PRONOUNS_RE.search(text)
    if match:
        candidate = match.group(1).strip()
        return candidate or None
    return None


async def _do_set_pronouns(update: Update, text: str, *, from_prompt: bool = False) -> None:
    """
    Character pronouns (2026-07-17, per Coffee, task #117): settable at
    creation or any time after. Narration reads this real fact instead
    of guessing (see ai/dm_agent.py's _pronoun_line) -- falls back to
    they/them when unset, never invented.
    """
    user_id = update.effective_user.id
    character = db.get_character(user_id)
    if character is None:
        await _safe_send(update, "You don't have a character yet!")
        return

    pronouns_text = text.strip() if from_prompt else _extract_inline_pronouns(text)
    if pronouns_text is None:
        _PENDING_PRONOUNS.add(user_id)
        await _safe_send(
            update,
            "Sure — what pronouns should the narration use for your character (he/him, "
            "she/her, they/them, or something else)? Say \"skip\" to leave it unset "
            "(defaults to they/them).",
        )
        return

    if pronouns_text.lower() in ("skip", "none", "no", "n/a", "nothing", "never mind"):
        await _safe_send(update, "No problem — narration will default to they/them. You can set this any time by asking.")
        return

    clean = " ".join(pronouns_text.split())[:MAX_PRONOUNS_LENGTH]
    if not clean:
        _PENDING_PRONOUNS.add(user_id)
        await _safe_send(update, "That didn't look like pronouns — try again?")
        return

    db.update_character(user_id, pronouns=clean)
    await _safe_send(update, f"✅ Pronouns saved for {character['name']}: {clean}")


async def donotdisturb_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """
    /donotdisturb (task #144): a real player-set flag, distinct from the
    automatic online/away presence -- see _presence_status. With DND on,
    a player's real online-ness is still tracked the same as ever, but
    they're skipped by the combat-join nudge in _do_start_combat.
    Bare command toggles; "/donotdisturb on"/"off" sets it explicitly.
    """
    user_id = update.effective_user.id
    character = db.get_character(user_id)
    if character is None:
        await _safe_send(update, "You don't have a character yet!")
        return

    arg = context.args[0].lower() if context.args else None
    if arg in ("on", "enable", "true"):
        new_state = True
    elif arg in ("off", "disable", "false"):
        new_state = False
    else:
        new_state = not character.get("do_not_disturb")

    db.set_do_not_disturb(user_id, new_state)
    if new_state:
        await _safe_send(update, "🔕 Do Not Disturb is now ON — you'll be skipped for join-a-fight nudges.")
    else:
        await _safe_send(update, "🔔 Do Not Disturb is now OFF.")


async def _do_set_status_note(update: Update, note_text: str) -> None:
    user_id = update.effective_user.id
    character = db.get_character(user_id)
    if character is None:
        await _safe_send(update, "You don't have a character yet!")
        return

    clean = note_text.strip()
    if not clean or clean.lower() in ("clear", "none", "remove", "delete"):
        db.set_status_note(user_id, None)
        await _safe_send(update, "Status note cleared.")
        return

    clean = " ".join(clean.split())[:MAX_STATUS_NOTE_LENGTH]
    db.set_status_note(user_id, clean)
    await _safe_send(update, f"✅ Status note set: \"{clean}\"")


async def note_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """
    /note <text> (task #144): a short, player-set status line shown
    alongside presence in the party roster and character sheet (e.g.
    "grinding the mines, back in 20"). Bare /note shows the current one
    instead of prompting -- there's nothing to prompt for, this command
    always carries its own argument.
    """
    text = " ".join(context.args) if context.args else ""
    if not text:
        character = db.get_character(update.effective_user.id)
        if character is None:
            await _safe_send(update, "You don't have a character yet!")
            return
        if character.get("status_note"):
            await _safe_send(
                update,
                f"Your current status note: \"{character['status_note']}\"\n"
                "Say \"/note clear\" to remove it, or \"/note <text>\" to change it.",
            )
        else:
            await _safe_send(
                update,
                "You don't have a status note set. Use \"/note <text>\" to set one, "
                "e.g. \"/note grinding the mines, back soon\".",
            )
        return
    await _do_set_status_note(update, text)


async def achievements_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await _do_check_achievements(update)


async def title_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """/title <text> (task #73): reliable slash-command path alongside the NL "set my title to ..." trigger."""
    text = " ".join(context.args) if context.args else ""
    if not text:
        await _safe_send(
            update,
            "Say \"/title <title>\" to wear an unlocked title, or \"/title clear\" to remove it. "
            "\"/achievements\" lists what you've earned.",
        )
        return
    await _do_set_title(update, text)


async def weather_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await _do_check_weather(update)


def _determine_winner(session: sessions.Session) -> str:
    """
    Returns 'party' or 'enemy' based on which side still has any member
    remaining in turn_order — NOT based on living_on_side's hp_current
    check, which would incorrectly call it for the enemy the instant a
    downed-but-not-dead player's HP hits 0.
    """
    party_remains = any(session.sides.get(pid) == "party" for pid in session.turn_order)
    return "party" if party_remains else "enemy"


async def _try_end_stale_combat(update: Update, session: sessions.Session) -> bool:
    """
    Real live incident (2026-07-19, Coffee: "no enemys left standing...
    it stopped"): remove_defeated() only ever runs as a side-effect of
    the SAME attack that dealt the killing blow -- if that never
    finished (the same kind of shutdown-interrupted-handler race
    documented in task #184, just landing on combat state that time
    instead of a dropped chat reply), a monster can be stuck at 0 HP but
    still present in turn_order forever: living_on_side() correctly
    shows zero living enemies (it filters by hp_current), but
    is_combat_over() checks turn_order membership, not HP, so it never
    returns True either. Every subsequent attack then just hits "No
    valid targets remain" -- a permanent deadlock with no legal action
    to escape it, confirmed live via the session snapshot (Wolf 2 sat at
    0/11 HP with no "has been defeated!" event ever logged for it).

    Called right before any "No valid targets remain" bail-out, this
    re-runs the normal defeat cleanup + victory resolution the
    interrupted attack should have finished, so a stale already-dead
    monster can never strand a fight again regardless of what caused the
    original gap. Returns True if this fully resolved combat (the caller
    should skip its own "no targets" message in that case).
    """
    chat_id = session.chat_id
    removed = session.remove_defeated()
    if removed:
        await _announce_defeats(update, session, removed)
    if not session.is_combat_over():
        return False
    winner = _determine_winner(session)
    xp_summary, level_up_notes = _award_victory_xp(session) if winner == "party" else ("", [])
    if winner == "party":
        await _check_quest_completions_defeat_monster(update, session)
        await _check_achievements_for_combat_party(update, session)
        await _check_guild_quest_completion(update, session)
    await _safe_send(update, f"🏆 **Combat over!** The {winner} side is victorious!{xp_summary}")
    for note in level_up_notes:
        await _notify_main_topic(update, note)
    sessions.end_session(chat_id)
    return True


async def _resolve_ai_turns(update: Update, session: sessions.Session) -> None:
    """
    Resolves consecutive AI-controlled turns AND auto-resolved death-save
    turns for downed real players. Assumes the caller already holds
    sessions.get_lock(session.chat_id) — this function does NOT acquire
    it itself, to avoid deadlocking against a non-reentrant lock.

    Includes stalemate detection: if the only remaining party members are
    stabilized (unconscious, can't act) and the enemy has no living
    target, nothing can ever happen on either side — without a real
    resolution this would loop forever. Two full turn cycles with no
    actual damage dealt resolves it as the enemy giving up (a stabilized
    character doesn't die from a stalemate). A hard iteration cap is also
    included as defense-in-depth against any other future logic bug that
    might otherwise cause an infinite loop.
    """
    consecutive_noop_turns = 0
    iteration_safety_cap = 200
    iterations = 0

    while not session.is_combat_over():
        iterations += 1
        if iterations > iteration_safety_cap:
            await _safe_send(
                update,
                "⚠️ Combat seems stuck in a loop — ending it automatically. "
                "Please report this if it happens again.",
            )
            sessions.end_session(session.chat_id)
            return

        stall_threshold = max(len(session.turn_order) * 2, 4)
        if consecutive_noop_turns >= stall_threshold:
            await _safe_send(
                update,
                "🏳️ **Stalemate — the enemy breaks off, unable to finish the fight. "
                "Your party survives.**",
            )
            sessions.end_session(session.chat_id)
            return

        current = session.current_participant()

        if session.is_downed(current["telegram_user_id"]):
            consecutive_noop_turns = 0
            death_result = resolve_death_save(current)
            _sync_player_to_db(current)

            if death_result["outcome"] == "dead":
                # Real, persistent death (2026-07-14, per Coffee): previously
                # this was narration-only -- hp_current sat at 0 with no
                # actual death flag, so a later rest would silently heal
                # them back up as if nothing happened. Now genuinely
                # permanent until a Revivify (Cleric spell or scroll)
                # brings them back -- see the "resurrect" spell effect in
                # _do_cast_spell. A dead character can't act or be moved
                # (see adventure_master_handler's is_dead gate) and stays
                # exactly where they died until revived; the player can
                # switch to another character of theirs in the meantime.
                if not current.get("is_ai"):
                    db.update_character(current["telegram_user_id"], is_dead=1)
                await _safe_send(
                    update,
                    f"💀 **{current['name']} rolls a {death_result['roll']} — "
                    f"3rd failed death save. {current['name']} has died.** "
                    f"They can't act or be moved until revived — you can play another character in the meantime.",
                )
                if not current.get("is_ai"):
                    await _notify_main_topic(update, f"💀 **{current['name']}** has died.")
                session.remove_dead_player(current["telegram_user_id"])
            elif death_result["outcome"] == "stable":
                session.stabilized_ids.add(current["telegram_user_id"])
                await _safe_send(
                    update,
                    f"💚 **{current['name']} rolls a {death_result['roll']} — "
                    f"3rd death save success! {current['name']} is stabilized "
                    f"(unconscious but no longer in danger).**",
                )
            elif death_result["outcome"] == "natural_20_revived":
                await _safe_send(
                    update,
                    f"✨ **{current['name']} rolls a NATURAL 20 on their death save "
                    f"and springs back up with 1 HP!**",
                )
            else:
                await _safe_send(
                    update,
                    f"🎲 **{current['name']}'s death save: rolled {death_result['roll']} "
                    f"({death_result['outcome']}) — "
                    f"{death_result['successes']} successes, {death_result['failures']} failures.**",
                )

            if session.is_combat_over():
                break
            session.advance_turn()
            continue

        # A stabilized player (3 successful death saves) is still
        # unconscious at 0 HP — they stop rolling further death saves,
        # but they are NOT able to act normally until actually healed
        # above 0 HP. Without this check they'd incorrectly get a full
        # turn (attack, cast, etc.) while unconscious.
        if not current.get("is_ai") and current["hp_current"] <= 0:
            consecutive_noop_turns += 1
            await _safe_send(
                update,
                f"😴 **{current['name']} is unconscious and stable** — "
                f"they can't act until healed above 0 HP.",
            )
            if session.is_combat_over():
                break
            session.advance_turn()
            continue

        # Paralyzed (2026-07-14, second new on_hit_condition batch after
        # blinded/silenced): real 5E fully incapacitates a paralyzed
        # creature -- no actions, no reactions. This engine has no
        # duration tracking for ANY condition (prone/poisoned/blinded/
        # silenced all persist until combat ends too), so paralyzed
        # follows the same simplification rather than inventing a
        # save-ends mechanic just for this one. Checked here, before the
        # real-player announce-and-return below, so it uniformly skips
        # BOTH AI and real-player turns from one place -- a paralyzed
        # real player never even gets prompted to act, so there's
        # nothing for _do_attack/etc.'s "not your turn" checks to have
        # to separately guard against.
        if "paralyzed" in current.get("conditions", []):
            consecutive_noop_turns += 1
            await _safe_send(update, f"⛓️ **{current['name']} is paralyzed and can't act this turn.**")
            if session.is_combat_over():
                break
            session.advance_turn()
            continue

        if not current.get("is_ai"):
            await _safe_send(update, _turn_announcement(session), reply_markup=_battle_menu_keyboard(session))
            return

        opposing = session.living_on_side(session.opposing_side(current["telegram_user_id"]))
        if not opposing:
            # No CONSCIOUS target remains (everyone on that side is either
            # downed or gone) — skip this turn rather than getting stuck
            # here forever. Combat continues so downed players keep
            # getting their death-save turns.
            consecutive_noop_turns += 1
            if session.is_combat_over():
                break
            session.advance_turn()
            continue
        consecutive_noop_turns = 0

        # Task #222, per Coffee: a human party member can message an
        # AI-controlled companion mid-fight (see _do_message_ai) and
        # have it actually act on real guidance to retreat, right on
        # its own turn, instead of always just attacking every turn as
        # this loop otherwise unconditionally does. One-shot (popped,
        # not peeked) so it's only ever acted on once. Bosses can't be
        # fled from at all (same rule _do_flee already enforces for a
        # human), so guidance to run from one is silently ignored here
        # rather than attempted and failing oddly.
        ai_context = _AI_PLAYER_CONTEXTS.get(current["telegram_user_id"])
        guidance = ai_context.user_data.pop("human_guidance", None) if ai_context else None
        if guidance and not any(e.get("is_boss") for e in opposing) and any(
            w in guidance.lower() for w in ("run", "retreat", "flee", "fall back", "escape")
        ):
            await _safe_send(update, f"🗣️ **{current['name']}** hears you and breaks for it!")
            await _resolve_flee_attempt(update, session, guidance)
            return

        # Boss Multiattack (2026-07-15) / Extra Attack (2026-07-16): every
        # is_boss monster gets 2 attacks/turn; separately, an AI-controlled
        # PARTY member (a recruited companion or autonomous AI player, real
        # character with class/level) gets the real 5E Extra Attack scaling
        # from _attacks_per_turn once they're a high enough level martial
        # class -- this is the same feature _do_attack gives a human player,
        # just applied here since AI-controlled combatants take their turn
        # through this function instead. Loops the same single-attack
        # resolution below either once or several times; breaks early if a
        # mid-turn kill leaves no target for the next attack, or if combat
        # itself ends mid-sequence.
        attack_count = 2 if current.get("is_boss") else _attacks_per_turn(current)
        # Per Coffee (2026-07-21): same clear preface as the human attack
        # path above -- skipped for bosses specifically, since those
        # already get their own "sizing up its target" flavor line each
        # turn (task #167) and a second generic line would just be noise.
        if attack_count > 1 and not current.get("is_boss"):
            await _safe_send(update, f"⚔️ **{current['name']}** has **{attack_count} attacks** this turn!")
        combat_ended_mid_turn = False
        for attack_num in range(attack_count):
            opposing = session.living_on_side(session.opposing_side(current["telegram_user_id"]))
            if not opposing:
                break
            target = min(opposing, key=lambda p: p["hp_current"])
            # Task #167 (per Coffee, 2026-07-18, scoped to boss-tier enemies
            # only after he flagged the latency cost of doing this for every
            # regular monster too): a pre-roll "sizing up its target" beat,
            # one per turn (attack_num == 0 only, so a boss's Multiattack
            # second swing doesn't repeat it) -- target is already the same
            # deterministic pick the rules layer just made above, so this
            # narrates a real decision rather than inventing one, and the
            # roll+outcome narration below is completely unaffected.
            if current.get("is_boss") and attack_num == 0:
                decision_flavor = await asyncio.to_thread(narrate_boss_decision, current, target)
                await _safe_send(update, f"👁️ {decision_flavor}")
            adv, disadv = _attack_advantage_disadvantage(current, target)
            _refresh_real_player_spell_slots(target)
            result = resolve_attack(
                current, target, _weapon_for_attacker(current), advantage=adv, disadvantage=disadv,
                defender_relentless_endurance_available=_relentless_endurance_available(target),
                round_number=session.round_number,
            )
            if result["relentless_endurance_triggered"]:
                db.use_feature(target["telegram_user_id"], "relentless_endurance")
            _sync_player_to_db(target)
            if result["shield_reaction_triggered"] or result["uncanny_dodge_triggered"]:
                await _announce_reaction(update, target, result)

            applied_condition = None
            resisted_condition = None
            if result["hit"] and current.get("on_hit_condition"):
                condition = current["on_hit_condition"]
                if _racially_immune_to_condition(target, condition):
                    resisted_condition = condition
                else:
                    target.setdefault("conditions", [])
                    if condition not in target["conditions"]:
                        target["conditions"].append(condition)
                        applied_condition = condition

            # Life Drain (The Waiting Shape's boss-specific ability, set
            # via campaign.json's life_drain flag): heals itself for half
            # the damage it deals, capped at its own hp_max, real
            # vampiric/undead-flavored 5E trope fitting its "waiting
            # shadow" theme.
            drain_amount = 0
            if result["hit"] and current.get("life_drain") and result["damage_dealt"] > 0:
                drain_amount = result["damage_dealt"] // 2
                if drain_amount > 0:
                    hp_max = current.get("hp_max", current["hp_current"])
                    current["hp_current"] = min(hp_max, current["hp_current"] + drain_amount)

            attack_label = (
                f"{current['name']} attacks {target['name']}" if attack_count == 1
                else f"{current['name']} attacks {target['name']} ({attack_num + 1}/{attack_count})"
            )
            await _post_narrated(update, current, attack_label, result, session)
            if applied_condition:
                await _safe_send(update, f"☠️ **{target['name']} is now {applied_condition.upper()}!**")
            if resisted_condition:
                trait = "Dwarven Resilience" if resisted_condition == "poisoned" else "Fey Ancestry"
                await _safe_send(update, f"🛡️ **{target['name']}'s {trait} shrugs off the {resisted_condition}!**")
            if drain_amount > 0:
                await _safe_send(
                    update,
                    f"🩸 **{current['name']} drains {drain_amount} HP from the attack, healing itself!**",
                )

            if target["hp_current"] <= 0 and not target.get("is_ai"):
                await _safe_send(
                    update,
                    f"⚠️ **{target['name']} drops to 0 HP and falls unconscious!** "
                    f"They'll roll death saving throws on their turns until stable, revived, or worse.",
                )

            removed = session.remove_defeated()
            await _announce_defeats(update, session, removed)
            if session.is_combat_over():
                combat_ended_mid_turn = True
                break

        if combat_ended_mid_turn:
            break
        session.advance_turn()

    if session.is_combat_over():
        winner = _determine_winner(session)
        xp_summary, level_up_notes = _award_victory_xp(session) if winner == "party" else ("", [])
        if winner == "party":
            await _check_quest_completions_defeat_monster(update, session)
            await _check_achievements_for_combat_party(update, session)
            await _check_guild_quest_completion(update, session)
        await _safe_send(update, f"🏆 **Combat over!** The {winner} side is victorious!{xp_summary}")
        for note in level_up_notes:
            await _notify_main_topic(update, note)
        sessions.end_session(session.chat_id)


async def _self_heal_stuck_ai_turn(update: Update, session: sessions.Session) -> None:
    """
    Defense-in-depth against a combat session getting soft-locked on an
    AI's turn — confirmed live: a transient network failure while
    sending an attack's narration once escaped mid-turn-resolution and
    left a session permanently stuck (nothing else ever re-attempts
    resolving an AI turn once _resolve_ai_turns has returned). _safe_send
    closes the specific failure mode that caused it, but calling this
    before every "is it actually your turn?" check gives players an
    immediate, self-service recovery path for that class of bug in
    general, instead of only the "cancel" escape hatch, which forfeits
    the whole fight rather than resuming it.
    """
    if session.current_participant().get("is_ai"):
        await _resolve_ai_turns(update, session)


_NUMBER_WORDS = {"one": 1, "two": 2, "three": 3, "four": 4, "a couple of": 2, "a few": 3}


def _monster_plural(name: str) -> str:
    """
    Best-effort English plural of a monster's display name (spaces, not
    underscores). Regular plurals were already covered for free by a
    plain substring check (e.g. "goblins" contains "goblin"), but
    "wolf" -> "wolves" is NOT a substring match (the f becomes a v) --
    confirmed live 2026-07-18: two different real players said things
    like "Attack wolves using longbow" and "let's fight the wolves" and
    NEVER got combat auto-started, because every monster-name match in
    this file (_do_attack's auto-start, the explicit start_combat
    dispatch, and _parse_enemy_count's plural-hint guess below) only
    ever checked the plain substring or a naively-appended "s". This is
    the one monster in the current bestiary whose name pluralizes this
    way; handled generally (any name ending in f/fe) rather than as a
    one-off "wolf"/"wolves" special case, so a future monster with the
    same English quirk (elf, dwarf, half-orc pun names, etc.) doesn't
    silently repeat this exact bug.
    """
    if name.endswith("fe"):
        return name[:-2] + "ves"
    if name.endswith("f"):
        return name[:-1] + "ves"
    return name + "s"


def _text_mentions_monster(monster_key: str, lowered_text: str) -> bool:
    """Real name (regular plural already just a substring) or its irregular plural."""
    name = monster_key.replace("_", " ")
    return name in lowered_text or _monster_plural(name) in lowered_text


def _parse_enemy_count(lowered_text: str) -> int | None:
    """
    Looks for an explicit count of enemies in natural language ("3
    goblins", "two goblins", "a few goblins"). Returns 2 if a plural
    monster name appears with no explicit number (a reasonable "small
    group" default), or None if there's no signal at all -- letting the
    caller auto-scale the encounter to the party instead of always
    defaulting to a lone monster regardless of party size/level.
    Explicit counts capped at 4 for sanity — this build's targeting is
    simple and a huge mob would be unwieldy to specify targets for via
    plain text.
    """
    for word, value in _NUMBER_WORDS.items():
        if word in lowered_text:
            return min(value, 4)
    for digit in ("2", "3", "4"):
        if digit in lowered_text:
            return int(digit)
    # No explicit number — check for a plain plural monster name (e.g.
    # "goblins" rather than "goblin") as a signal for a small group.
    for key in CAMPAIGN["monsters"]:
        if _monster_plural(key.replace("_", " ")) in lowered_text:
            return 2
    return None


async def _do_challenge_duel(update: Update, text: str) -> None:
    """
    Task #82, per Coffee: PvP, "safe zones by design, intentional-
    targeting only, tied to alignment/vendettas." This is the
    intentional-targeting half: a real challenge, naming a specific
    real player character at the same location, who must explicitly
    accept (_do_accept_duel) before anything happens -- never an
    ambush, never accidental. Any location flagged "safe" in
    campaign.json (the same flag resting already reuses) refuses to
    host one at all, giving "safe zones" for free with zero new schema.
    A real duel, once accepted, resolves through the exact same combat
    engine as any other fight -- same death-save/downed rules, no
    separate reduced-stakes variant, real consent is the actual safety
    mechanism here, not softened consequences.
    """
    telegram_user_id = update.effective_user.id
    character = db.get_character(telegram_user_id)
    if character is None:
        await update.effective_chat.send_message(
            "You don't have a character yet!", message_thread_id=config.TOPIC_ADVENTURE_ID
        )
        return

    location = cl.get_location(CAMPAIGN, character["current_location"])
    if location and location.get("safe"):
        await update.effective_chat.send_message(
            "This is a safe place — no duels here.", message_thread_id=config.TOPIC_ADVENTURE_ID
        )
        return

    if sessions.get_session(update.effective_chat.id) is not None:
        await update.effective_chat.send_message(
            "A fight's already happening here.", message_thread_id=config.TOPIC_ADVENTURE_ID
        )
        return

    candidates = _get_combat_eligible_party_members(character["current_location"])
    lowered = text.lower()
    target = next(
        (p for p in candidates
         if not p.get("is_ai") and p["telegram_user_id"] != telegram_user_id and p["name"].lower() in lowered),
        None,
    )
    if target is None:
        await update.effective_chat.send_message(
            "Not sure who you mean — name a real player here to challenge.",
            message_thread_id=config.TOPIC_ADVENTURE_ID,
        )
        return

    _PENDING_DUELS[target["telegram_user_id"]] = telegram_user_id
    await _safe_send(
        update,
        f"⚔️ **{character['name']}** challenges **{target['name']}** to a duel! "
        f"**{target['name']}**, say \"accept duel\" to fight, or just ignore it to decline.",
    )


async def _do_accept_duel(update: Update) -> None:
    telegram_user_id = update.effective_user.id
    challenger_id = _PENDING_DUELS.pop(telegram_user_id, None)
    if challenger_id is None:
        await update.effective_chat.send_message(
            "No duel challenge is waiting for you.", message_thread_id=config.TOPIC_ADVENTURE_ID
        )
        return

    target = db.get_character(telegram_user_id)
    challenger = db.get_character(challenger_id)
    if target is None or challenger is None:
        return

    # Defense in depth: re-check everything at accept-time too, since
    # real time may have passed since the challenge was issued.
    if target["current_location"] != challenger["current_location"]:
        await update.effective_chat.send_message(
            f"{challenger['name']} isn't here anymore — the duel's off.",
            message_thread_id=config.TOPIC_ADVENTURE_ID,
        )
        return
    location = cl.get_location(CAMPAIGN, target["current_location"])
    if location and location.get("safe"):
        await update.effective_chat.send_message(
            "This is a safe place — no duels here.", message_thread_id=config.TOPIC_ADVENTURE_ID
        )
        return
    if sessions.get_session(update.effective_chat.id) is not None:
        await update.effective_chat.send_message(
            "A fight's already happening here.", message_thread_id=config.TOPIC_ADVENTURE_ID
        )
        return

    session = sessions.start_session(
        update.effective_chat.id, [challenger, target],
        {challenger_id: "party", telegram_user_id: "enemy"},
    )
    await _safe_send(
        update, f"⚔️ **{challenger['name']}** vs **{target['name']}** — the duel begins!",
    )
    await _safe_send(update, _turn_announcement(session), reply_markup=_battle_menu_keyboard(session))


async def _do_start_combat(update: Update, monster_key: str | None = None, count: int | None = None) -> None:
    chat_id = update.effective_chat.id
    async with sessions.get_lock(chat_id):
        if sessions.get_session(chat_id) is not None:
            await update.effective_chat.send_message(
                "A combat session is already active! Say \"cancel\" if you think "
                "it's stuck and need to force-end it.",
                message_thread_id=config.TOPIC_ADVENTURE_ID,
            )
            return

        requester = db.get_character(update.effective_user.id)
        if requester is None:
            await update.effective_chat.send_message(
                "You don't have a character yet!", message_thread_id=config.TOPIC_ADVENTURE_ID
            )
            return

        # Per Coffee (2026-07-14): "only characters that are active and
        # at the same location shud be in the same battle... if a
        # character is in the tavern and another is in the whispering
        # woods they shud not be able to fight." Scoped to wherever the
        # player actually starting this fight is standing, not every
        # active character in the whole game.
        all_characters = _get_combat_eligible_party_members(requester["current_location"])
        # Characters at 0 HP can't fight until they rest — never silently
        # dragged into a new combat as if nothing happened.
        party = [p for p in all_characters if p["hp_current"] > 0]
        downed = [p for p in all_characters if p["hp_current"] <= 0 and not p.get("is_ai")]

        if not party:
            if downed:
                await update.effective_chat.send_message(
                    "Everyone here has fallen — say \"I rest\" to recover before continuing.",
                    message_thread_id=config.TOPIC_ADVENTURE_ID,
                )
            else:
                await update.effective_chat.send_message(
                    "No one here is in a fit state to fight right now.",
                    message_thread_id=config.TOPIC_ADVENTURE_ID,
                )
            return

        # Default to a monster native to the player's current location, if not specified.
        if monster_key is None or cl.get_monster_template(CAMPAIGN, monster_key) is None:
            player = next((p for p in party if not p.get("is_ai")), party[0])
            location = cl.get_location(CAMPAIGN, player["current_location"])
            local_monsters = location.get("monsters", []) if location else []
            monster_key = local_monsters[0] if local_monsters else "goblin"

        template = cl.get_monster_template(CAMPAIGN, monster_key)
        if template is None:
            await update.effective_chat.send_message(
                f"No monster template found for '{monster_key}'.",
                message_thread_id=config.TOPIC_ADVENTURE_ID,
            )
            return

        # Per Coffee (2026-07-20): "Dont let the AI progress story line
        # without the party- they shudnt be in boss battles without
        # human players." An AI-controlled character (recruited
        # companion or the autonomous AI party) acting entirely on its
        # own must not start a fight against a story boss unless a real
        # human party member is actually here too -- this only blocks
        # the AI-INITIATED case; a human starting the same boss fight
        # with only AI allies alongside them is unaffected.
        if requester.get("is_ai") and template.get("is_boss") and not any(not p.get("is_ai") for p in party):
            await update.effective_chat.send_message(
                f"{requester['name']} sizes up {template['name']} and holds back — not a fight worth starting without the rest of the party here.",
                message_thread_id=config.TOPIC_ADVENTURE_ID,
            )
            return

        # No explicit count requested -- scale the encounter to the
        # CURRENT party (size and level) via real 5E Medium-difficulty
        # encounter math instead of always defaulting to one lone
        # monster, so a bigger/higher-level party actually faces a
        # proportionally bigger fight. See rules/leveling.scaled_enemy_count.
        if count is None:
            count = scaled_enemy_count(
                [p.get("level", 1) for p in party], template.get("xp_reward", 0)
            )

        enemies = []
        for i in range(count):
            enemy_id = -2_000_000 - (abs(hash(monster_key)) % 100_000) - i
            enemy_name = f"{template['name']} {i + 1}" if count > 1 else template["name"]
            enemies.append({
                "telegram_user_id": enemy_id, "name": enemy_name,
                "dexterity": template["dexterity"], "strength": template["strength"],
                "armor_class": template["armor_class"], "hp_current": template["hp_max"],
                "hp_max": template["hp_max"], "proficiency_bonus": template["proficiency_bonus"],
                "is_ai": 1, "xp_reward": template.get("xp_reward", 0),
                "on_hit_condition": template.get("on_hit_condition"),
                "monster_key": monster_key,
                "is_boss": template.get("is_boss", False),
                "life_drain": template.get("life_drain", False),
            })
        sides = {p["telegram_user_id"]: "party" for p in party}
        for enemy in enemies:
            sides[enemy["telegram_user_id"]] = "enemy"

        session = sessions.start_session(chat_id, party + enemies, sides=sides)
        # Bestiary discovery (2026-07-16, per Coffee): every real human
        # party member fighting this monster type learns it -- same
        # fog-of-war convention as visited_locations, so "show me the
        # bestiary" only ever lists monsters actually encountered, not
        # every monster in the campaign.
        for p in party:
            if not p.get("is_ai"):
                db.mark_known_monster(p["telegram_user_id"], monster_key)
        initiative_line = ", ".join(
            f"{p['name']} ({p['initiative']})" for p in session.participants
        )
        enemy_description = f"{count}x **{template['name']}**" if count > 1 else f"**{template['name']}**"
        header = (
            f"⚔️ **Combat Begins!**\n"
            # Per Coffee (2026-07-14): must reflect who's ACTUALLY in
            # this fight (the location-scoped `party`), not the global
            # _party_summary_text() -- otherwise the header lists
            # people who aren't really here.
            f"Your party ({_format_party_names(party)}) faces {enemy_description}!\n\n"
            f"🎯 **Initiative order:** {initiative_line}\n\n"
            + _turn_announcement(session)
        )
        # Combat-join nudge (task #144): real players elsewhere who are
        # currently online and haven't opted into Do Not Disturb get
        # named here so they know a fight just started and can travel
        # in -- this is what presence actually feeds, per Coffee's
        # backlog description. Resting players and anyone already in
        # this fight are deliberately left out, same as DND.
        nudge_targets = _online_party_members_elsewhere(
            requester["current_location"], {p["telegram_user_id"] for p in party}
        )
        if nudge_targets:
            names = ", ".join(p["name"] for p in nudge_targets)
            location_name = cl.get_location(CAMPAIGN, requester["current_location"])["name"]
            header += f"\n\n📢 {names} — a fight just broke out at **{location_name}**, come join if you can!"
        await _safe_send(update, header, reply_markup=_battle_menu_keyboard(session))
        await _maybe_send_monster_image(update, monster_key, template)
        await _notify_main_topic(update, f"⚔️ {_format_party_names(party)} entered battle against {enemy_description}!")
        await _resolve_ai_turns(update, session)


def _npc_combatant_from_stats(npc_id: str, npc_data: dict) -> dict:
    """
    Builds a combat participant dict for a named, alignment-driven NPC —
    identical shape to _do_start_combat's monster-derived enemies, plus
    source_npc_id so a win can be attributed back to this specific NPC
    (see _award_victory_xp, which marks them into _DEFEATED_NPCS).
    """
    stats = npc_data["stats"]
    enemy_id = -3_000_000 - (abs(hash(npc_id)) % 100_000)
    return {
        "telegram_user_id": enemy_id, "name": npc_data["name"],
        "dexterity": stats["dexterity"], "strength": stats["strength"],
        "armor_class": stats["armor_class"], "hp_current": stats["hp_max"],
        "hp_max": stats["hp_max"], "proficiency_bonus": stats["proficiency_bonus"],
        "is_ai": 1, "xp_reward": stats.get("xp_reward", 0),
        "source_npc_id": npc_id,
        "is_boss": npc_data.get("is_boss", False),
    }


FACTION_HOSTILE_ESCALATION_STANDING = -50  # a faction this soured on a player turns its own members hostile


def _effective_disposition(telegram_user_id: int, npc_id: str, npc_data: dict) -> str:
    """
    An NPC's disposition isn't fixed forever: a friendly/neutral NPC
    whose faction has been badly wronged by this specific player (real,
    earned faction standing — see db.adjust_faction_standing) turns
    hostile toward them, even if the same NPC is perfectly friendly to
    everyone else. A "hostile" NPC's own disposition is unaffected by
    this (they don't need faction standing to justify being an enemy).
    """
    disposition = npc_data.get("disposition", "friendly")
    if disposition == "hostile":
        return disposition
    faction_id = _faction_for_npc(npc_id)
    if faction_id is None:
        return disposition
    standing = db.get_faction_standing(telegram_user_id, faction_id, _faction_starting_standing(faction_id))
    if standing <= FACTION_HOSTILE_ESCALATION_STANDING:
        return "hostile"
    return disposition


async def _maybe_trigger_npc_encounter(update: Update, character: dict, location: dict) -> None:
    """
    Living-world ambient encounters: an alignment-driven NPC placed at
    this location has a chance to react to the player's arrival —
    unprompted, not in response to anything the player said. Friendly/
    neutral NPCs get a narrated ambient line grounded in persistent,
    per-player memory (db.get_relationship); a hostile NPC (or a
    friendly/neutral one whose faction has turned on this player, see
    _effective_disposition) instead provokes a REAL combat encounter
    through the exact same rules engine as any other fight
    (sessions.start_session, real dice) — the AI only narrates the
    provocation, never the fight's outcome.
    """
    chat_id = update.effective_chat.id
    if sessions.get_session(chat_id) is not None:
        return  # never interrupt combat already in progress

    npc_ids = _npcs_at_location(location["id"])
    if not npc_ids or random.random() > AMBIENT_NPC_ENCOUNTER_CHANCE:
        return

    npc_id = random.choice(npc_ids)
    npc_data = CAMPAIGN["npcs"].get(npc_id)
    if npc_data is None or npc_id not in _NPCS:
        return

    telegram_user_id = update.effective_user.id
    disposition = _effective_disposition(telegram_user_id, npc_id, npc_data)
    memory_facts = db.get_relationship(telegram_user_id, npc_id)["memory_events"]

    if disposition == "hostile":
        if npc_id in _DEFEATED_NPCS or "stats" not in npc_data:
            return
        async with sessions.get_lock(chat_id):
            if sessions.get_session(chat_id) is not None:
                return
            # Same location-scoping fix as _do_start_combat (2026-07-14,
            # per Coffee): an ambush breaking out here shouldn't pull in
            # party members who are actually somewhere else entirely.
            all_characters = _get_combat_eligible_party_members(location["id"])
            party = [p for p in all_characters if p["hp_current"] > 0]
            if not party:
                return

            taunt = await asyncio.to_thread(
                generate_ambient_line, npc_id, character["name"], "arrives, about to be attacked", memory_facts
            )
            enemy = _npc_combatant_from_stats(npc_id, npc_data)
            sides = {p["telegram_user_id"]: "party" for p in party}
            sides[enemy["telegram_user_id"]] = "enemy"
            session = sessions.start_session(chat_id, party + [enemy], sides=sides)

            initiative_line = ", ".join(
                f"{p['name']} ({p['initiative']})" for p in session.participants
            )
            taunt_line = f"> *\"{taunt}\"*\n\n" if taunt else ""
            header = (
                f"⚠️ **{npc_data['name']} ({npc_data.get('alignment', 'unknown alignment')}) "
                f"blocks your path!**\n{taunt_line}"
                f"⚔️ **Combat Begins!**\n"
                # Per Coffee (2026-07-14): same fix as _do_start_combat
                # -- reflect who's ACTUALLY here (location-scoped
                # `party`), not the global party summary.
                f"Your party ({_format_party_names(party)}) faces **{npc_data['name']}**!\n\n"
                f"🎯 **Initiative order:** {initiative_line}\n\n"
                + _turn_announcement(session)
            )
            await _safe_send(update, header, reply_markup=_battle_menu_keyboard(session))
            await _notify_main_topic(update, f"⚔️ {_format_party_names(party)} entered battle against **{npc_data['name']}**!")
            await _resolve_ai_turns(update, session)
    elif len(npc_ids) >= 2 and random.random() < 0.5:
        # Catch two NPCs mid-conversation rather than one reacting to the
        # player — makes the world feel populated even when nobody's
        # talking to anybody. Purely a narrated moment the player
        # witnesses; nobody here is "reacting to" the arrival.
        other_id = random.choice([n for n in npc_ids if n != npc_id])
        other_data = CAMPAIGN["npcs"].get(other_id)
        if other_data is None or other_id not in _NPCS:
            return
        line = await asyncio.to_thread(
            generate_ambient_line, npc_id, other_data["name"],
            f"is talking with {other_data['name']} here, both unaware {character['name']} just arrived",
        )
        if line:
            await _safe_send(update, f"💬 **{npc_data['name']}** (to {other_data['name']}): {line}")
    else:
        line = await asyncio.to_thread(
            generate_ambient_line, npc_id, character["name"], "arrives", memory_facts
        )
        if line:
            await _safe_send(update, f"💬 **{npc_data['name']}:** {line}")


async def _do_attack(update: Update, action_text: str, forced_roll: int | None = None) -> None:
    chat_id = update.effective_chat.id

    # Auto-start combat when no fight is running yet but the player is
    # clearly naming a real monster at their current location -- confirmed
    # live 2026-07-14 (Coffee): "I attack the goblin" with no session
    # active just refused with "No combat is active right now," requiring
    # a separate "let's start a fight" first even though the intent was
    # already completely unambiguous. Done BEFORE acquiring this
    # function's own lock below, since _do_start_combat acquires the same
    # per-chat lock itself (asyncio.Lock isn't reentrant -- nesting them
    # would deadlock).
    if sessions.get_session(chat_id) is None:
        character = db.get_character(update.effective_user.id)
        location = cl.get_location(CAMPAIGN, character["current_location"]) if character else None
        local_monsters = location.get("monsters", []) if location else []
        lowered = action_text.lower()
        matched_monster = next(
            (m for m in local_monsters if _text_mentions_monster(m, lowered)), None
        ) or (local_monsters[0] if len(local_monsters) == 1 else None)
        if matched_monster is not None:
            await _do_start_combat(update, monster_key=matched_monster)

    async with sessions.get_lock(chat_id):
        session = sessions.get_session(chat_id)
        if session is None:
            await update.effective_chat.send_message(
                "No combat is active right now.", message_thread_id=config.TOPIC_ADVENTURE_ID
            )
            return

        user_id = update.effective_user.id
        if session.current_participant_id() != user_id:
            await _self_heal_stuck_ai_turn(update, session)
            session = sessions.get_session(chat_id)
            if session is None:
                await update.effective_chat.send_message(
                    "Combat had stalled and just resolved itself — nothing active right now.",
                    message_thread_id=config.TOPIC_ADVENTURE_ID,
                )
                return
        if session.current_participant_id() != user_id:
            current_name = session.current_participant()["name"]
            await update.effective_chat.send_message(
                f"It's not your turn — it's **{current_name}**'s turn.",
                message_thread_id=config.TOPIC_ADVENTURE_ID,
            )
            return

        attacker = session.current_participant()
        if attacker["hp_current"] <= 0:
            await update.effective_chat.send_message(
                "You're unconscious (0 HP) and can't act until healed.",
                message_thread_id=config.TOPIC_ADVENTURE_ID,
            )
            return

        opposing = session.living_on_side(session.opposing_side(user_id))
        if not opposing:
            if not await _try_end_stale_combat(update, session):
                await update.effective_chat.send_message(
                    "No valid targets remain.", message_thread_id=config.TOPIC_ADVENTURE_ID
                )
            return

        # Physical-dice mode (2026-07-16, per Coffee): only the FIRST
        # attack of a turn's sequence gets a manual-roll prompt -- an
        # Extra Attack/Action Surge sequence's additional swings stay
        # automatic, a deliberate scope limit (no live character has
        # Extra Attack yet, and pausing mid-sequence for each swing
        # would need much more state than a single pending-roll slot).
        if forced_roll is None and attacker.get("manual_dice_enabled") and not attacker.get("is_ai"):
            forced_roll = _extract_combined_roll(action_text)
        if forced_roll is None and attacker.get("manual_dice_enabled") and not attacker.get("is_ai"):
            _PENDING_DICE_ROLLS[user_id] = _new_pending_roll("attack", action_text, update.effective_chat.id)
            # Task #187: routed through _safe_send (not a raw send_message)
            # so a transient TimedOut retries once instead of silently
            # dropping the prompt -- confirmed live this WAS happening
            # (real ConnectTimeout caught only by the global error handler).
            await _safe_send(update, f"🎲 **{attacker['name']}**, roll a d20 for your attack and tell me the result (you have 1 minute, or I'll roll for you).")
            return
        # Task #143: a real damage-die roll declared alongside the
        # attack roll in the same message ("attack goblin, i rolled 15
        # to hit and 6 for damage") -- inline-only, no blocking prompt,
        # see _extract_combined_damage_roll's own comment for why.
        forced_damage_roll = (
            _extract_combined_damage_roll(action_text)
            if attacker.get("manual_dice_enabled") and not attacker.get("is_ai") else None
        )

        # Extra Attack (2026-07-16): real 5E martial classes (Fighter/
        # Barbarian/Paladin/Ranger/Monk, level 5+) get more than one
        # attack per turn -- see _attacks_per_turn. Loops the same
        # single-attack resolution either once or several times, same
        # shape as boss Multiattack in _resolve_ai_turns; breaks early
        # if a mid-sequence kill leaves no target for the next attack.
        attack_count = _attacks_per_turn(attacker)
        # Action Surge (Fighter, level 2+, 2026-07-16): real 5E grants an
        # entire extra action -- for a Fighter with Extra Attack, that
        # means their full attack sequence again, not just one more
        # swing. Consumed here (popped, not just read) so it only
        # doubles the very next attack sequence this Fighter makes, not
        # every turn for the rest of combat.
        if attacker.pop("action_surge_active", False):
            attack_count *= 2
        # Flurry of Blows (Monk, level 2+, 2026-07-16): adds 2 bonus
        # unarmed strikes to this turn, consumed the same way as
        # Action Surge above.
        attack_count += attacker.pop("flurry_bonus_attacks", 0)
        # Per Coffee (2026-07-21): a player with more than one attack
        # this turn (Extra Attack, Action Surge, Flurry of Blows) should
        # be told so plainly -- the existing "(1/2)"/"(2/2)" suffix on
        # each attack's own narration is easy to read past; this says it
        # up front, once, before the sequence starts.
        if attack_count > 1:
            await _safe_send(update, f"⚔️ **{attacker['name']}** has **{attack_count} attacks** this turn!")
        for attack_num in range(attack_count):
            opposing = session.living_on_side(session.opposing_side(user_id))
            if not opposing:
                break
            target = _pick_target(action_text, opposing)

            adv, disadv = _attack_advantage_disadvantage(attacker, target)
            _refresh_real_player_spell_slots(target)
            result = resolve_attack(
                attacker, target, _weapon_for_attacker(attacker), advantage=adv, disadvantage=disadv,
                defender_relentless_endurance_available=_relentless_endurance_available(target),
                round_number=session.round_number,
                forced_roll=forced_roll if attack_num == 0 else None,
                forced_damage_roll=forced_damage_roll if attack_num == 0 else None,
            )
            if result["relentless_endurance_triggered"]:
                db.use_feature(target["telegram_user_id"], "relentless_endurance")

            # Divine Smite (Paladin, level 2+, 2026-07-16): primed via
            # _do_divine_smite before this attack; only consumed (and
            # only spends the spell slot) on an actual confirmed hit --
            # a miss leaves smite_active armed for the next swing.
            if result["hit"] and attacker.pop("smite_active", False):
                smite_dmg = roll_damage(
                    DIVINE_SMITE_DICE_BY_SLOT_LEVEL[1], critical=result["critical_hit"]
                )["total"]
                result["damage_dealt"] += smite_dmg
                target["hp_current"] = max(target["hp_current"] - smite_dmg, 0)
                result["defender_hp_remaining"] = target["hp_current"]
                smiter = db.get_character(update.effective_user.id)
                db.update_character(
                    update.effective_user.id, spell_slots_current=smiter["spell_slots_current"] - 1
                )
            _sync_player_to_db(target)
            if result["shield_reaction_triggered"] or result["uncanny_dodge_triggered"]:
                await _announce_reaction(update, target, result)
            attack_label = (
                action_text if attack_count == 1 else f"{action_text} ({attack_num + 1}/{attack_count})"
            )
            await _post_narrated(update, attacker, attack_label, result, session)

            removed = session.remove_defeated()
            await _announce_defeats(update, session, removed)

            if session.is_combat_over():
                winner = _determine_winner(session)
                xp_summary, level_up_notes = _award_victory_xp(session) if winner == "party" else ("", [])
                if winner == "party":
                    await _check_quest_completions_defeat_monster(update, session)
                    await _check_achievements_for_combat_party(update, session)
                    await _check_guild_quest_completion(update, session)
                await _safe_send(update, f"🏆 **Combat over!** The {winner} side is victorious!{xp_summary}")
                for note in level_up_notes:
                    await _notify_main_topic(update, note)
                sessions.end_session(chat_id)
                return

        session.advance_turn()
        await _resolve_ai_turns(update, session)


async def _do_pass_turn(update: Update) -> None:
    chat_id = update.effective_chat.id
    async with sessions.get_lock(chat_id):
        session = sessions.get_session(chat_id)
        if session is None:
            await _safe_send(update, "There's no active turn to pass right now.")
            return
        session.advance_turn()
        await _safe_send(update, _turn_announcement(session), reply_markup=_battle_menu_keyboard(session))
        await _resolve_ai_turns(update, session)


async def _do_recruit_npc(update: Update, npc_name: str) -> None:
    npc_id = npc_name.lower().replace(" ", "_")
    npc = None
    for candidate_id, data in CAMPAIGN["npcs"].items():
        if candidate_id == npc_id or data["name"].lower() == npc_name.lower():
            npc_id, npc = candidate_id, data
            break

    if npc is None:
        await update.effective_chat.send_message(
            "There's no one by that name here to recruit.",
            message_thread_id=config.TOPIC_ADVENTURE_ID,
        )
        return

    if not npc.get("recruitable"):
        await update.effective_chat.send_message(
            f"{npc['name']} isn't interested in joining your party.",
            message_thread_id=config.TOPIC_ADVENTURE_ID,
        )
        return

    already_in_party = any(
        p["name"] == npc["name"] and p.get("is_ai") for p in _get_party_members()
    )
    if already_in_party:
        await update.effective_chat.send_message(
            f"{npc['name']} is already traveling with you.",
            message_thread_id=config.TOPIC_ADVENTURE_ID,
        )
        return

    stats = npc["stats"]
    ability_scores = {
        "strength": stats["strength"], "dexterity": stats["dexterity"],
        "constitution": stats["constitution"], "intelligence": stats["intelligence"],
        "wisdom": stats["wisdom"], "charisma": stats["charisma"],
    }
    companion = db.create_ai_companion(
        name=npc["name"], race=stats["race"], char_class=stats["char_class"],
        ability_scores=ability_scores, hp_max=stats["hp_max"],
        armor_class=stats["armor_class"], gold=stats["gold"],
        inventory=dict(stats["inventory"]),
    )

    # Real bug found live (2026-07-17, Coffee: "if she is recruited she
    # shud follow the party?"): a recruited companion was never actually
    # attached to the recruiter's party_id, so _do_move (which only ever
    # moves the acting player) silently left them behind at wherever
    # create_ai_companion's schema default put them -- they'd stand
    # still forever unless someone recruited them again. Attaching them
    # to a real party_id here, plus the matching move-along block in
    # _do_move below, is what actually makes "joins your party" true.
    # Also moves them to the recruiter's CURRENT location immediately,
    # since create_character's schema default location has nothing to
    # do with where they were actually just recruited.
    recruiter = db.get_character(update.effective_user.id)
    if recruiter is not None:
        party_id = recruiter.get("party_id")
        if not party_id:
            party_id = db.create_party(update.effective_user.id)
        db.add_ai_companion_to_party(companion["telegram_user_id"], party_id)
        db.move_character(companion["telegram_user_id"], recruiter["current_location"])

    await _safe_send(update, f"🤝 {npc['name']} joins your party! {_party_summary_text()}")

    # Per Coffee (2026-07-14): each recruitable should mention "a task,
    # mission, journey or adventure" once they join, so the party can
    # choose to help -- a real hook toward following the story
    # linearly, not just flavor text. Only surfaces a quest that's
    # actually real (giver_npc == this NPC) and not already done.
    personal_quest = next(
        (
            (qid, q) for qid, q in CAMPAIGN.get("quests", {}).items()
            if q.get("giver_npc") == npc_id
        ),
        None,
    )
    if personal_quest is not None:
        _, quest = personal_quest
        await _safe_send(
            update,
            f"💬 **{npc['name']}:** {quest['description']} Say \"I accept the quest\" if the party wants to help.",
        )


# A fixed, moderate DC for all non-combat skill checks. Real 5E lets a
# DM set the DC per situation (Easy=10, Medium=15, Hard=20, etc.) — this
# build uses one flat value (13, roughly "moderately hard") for every
# check as a deliberate, documented simplification, rather than having
# the AI invent a DC per situation (which risked exactly the kind of
# made-up-numbers problem already fixed elsewhere in this build).
SKILL_CHECK_DC = config.SKILL_CHECK_DC  # moved to config.py 2026-07-14, now .env-configurable
STEAL_DC = 15  # harder than an ordinary skill check — stealing carries real risk


def _practiced_bonus_for(telegram_user_id: int, ability: str) -> int:
    """Current earned bonus for this ability from repeated real use (rules/proficiency.py)."""
    uses = db.get_skill_uses(telegram_user_id).get(ability, 0)
    return practiced_bonus(uses)


def _relentless_endurance_available(character: dict) -> bool:
    """
    Whether a real (non-AI) Half-Orc character still has their real 5E
    Relentless Endurance available this rest -- rules/combat.py has no DB
    access by design, so this DB-backed check lives here and gets passed
    IN to resolve_attack as a plain bool; see that function's docstring.
    """
    if character.get("race") != "Half-Orc" or character.get("is_ai"):
        return False
    return db.get_feature_uses(character["telegram_user_id"], "relentless_endurance") == 0


def _format_skill_check_result(flavor_text: str, result: dict, ability: str, dc: int, success: bool) -> str:
    """Visual template for non-combat skill checks, mirroring the combat template's style."""
    lines = []
    raw_roll = result.get("raw_roll")
    roll_suffix = f" (rolled a **{raw_roll}**)" if raw_roll is not None else ""

    if raw_roll == 20:
        lines.append(f"🔥 **NATURAL 20 — Incredible Success!** 🔥{roll_suffix}")
    elif raw_roll == 1:
        lines.append(f"💀 **NATURAL 1 — Complete Failure!** 💀{roll_suffix}")
    elif success:
        lines.append(f"✨ **Success!** ✨{roll_suffix}")
    else:
        lines.append(f"💨 **Failure...**{roll_suffix}")

    if flavor_text:
        lines.append(f"> {flavor_text}")

    lines.append("")
    lines.append(f"🎲 **{ability.title()} Check:** {result['total']} vs DC {dc}")
    return "\n".join(lines)


def _find_lockable(location: dict, action_text: str) -> dict | None:
    """
    Match a chest/door target mentioned in the player's text against this
    location's lockables. If exactly one lockable exists here and the
    text generically mentions "lock"/"chest"/"door", that one is assumed.
    """
    lockables = location.get("lockables", [])
    if not lockables:
        return None
    lowered = action_text.lower()
    for lockable in lockables:
        if lockable["id"] in lowered or lockable["name"].lower() in lowered:
            return lockable
    if len(lockables) == 1:
        lockable = lockables[0]
        # Kind-specific fallback words only — a location whose only
        # lockable is a chest must not match generic "door" phrasing
        # (and vice versa), or a player mentioning the wrong kind of
        # lockable gets silently routed to the wrong one.
        kind_words = {"chest": ("chest", "lock"), "door": ("door", "lock")}
        if any(w in lowered for w in kind_words.get(lockable["kind"], ("lock",))):
            return lockable
    return None


async def _do_lockpick(update: Update, character: dict, lockable: dict, action_text: str,
                        forced_roll: int | None = None) -> None:
    """
    Real DC-13 DEX check to pick a chest/door lock — deterministic outcome
    computed here (rules layer), the AI only narrates it. Success on a
    chest grants its real loot/gold; success on a door permanently opens
    that shortcut connection (see _do_move's locked_connections check).
    Physical-dice mode (manual_dice_enabled) is handled by the caller,
    _do_skill_check, before this is ever dispatched to -- forced_roll
    just threads that already-collected value through to the actual roll.
    """
    if lockable["id"] in _UNLOCKED:
        await update.effective_chat.send_message(
            f"{lockable['name'].capitalize()} is already unlocked.",
            message_thread_id=config.TOPIC_ADVENTURE_ID,
        )
        return

    result = roll_ability_check(character, "dexterity", proficient=False, forced_roll=forced_roll)
    bonus = _practiced_bonus_for(update.effective_user.id, "dexterity")
    result["total"] += bonus
    result["practiced_bonus"] = bonus
    success = result["total"] >= SKILL_CHECK_DC
    # 2026-07-14, per Coffee: skill points (practiced-use progress) are
    # only earned on a SUCCESSFUL roll, not just any attempt -- same
    # change applied at every record_skill_use call site.
    if success:
        db.record_skill_use(update.effective_user.id, "dexterity")

    reward_line = ""
    if success:
        _UNLOCKED.add(lockable["id"])
        if lockable["kind"] == "chest":
            for item_id, qty in lockable.get("loot", {}).items():
                db.add_item(update.effective_user.id, item_id, qty)
            gold = lockable.get("gold", 0)
            if gold:
                updated = db.get_character(update.effective_user.id)
                db.update_character(update.effective_user.id, gold=updated["gold"] + gold)
            loot_names = ", ".join(
                f"{qty}x {items_module.get_item(i)['name']}" for i, qty in lockable.get("loot", {}).items()
            )
            parts = [p for p in (loot_names, f"{lockable.get('gold', 0)} gold" if lockable.get("gold") else "") if p]
            reward_line = f"\n🎁 You find: {', '.join(parts)}." if parts else ""
        else:
            reward_line = "\n🚪 The way is now open."

    flavor = await asyncio.to_thread(
        narrate_skill_check, character, action_text, "dexterity",
        {**result, "ability": "dexterity", "dc": SKILL_CHECK_DC, "success": success},
    )
    message = _format_skill_check_result(flavor, result, "dexterity", SKILL_CHECK_DC, success) + reward_line
    await _safe_send(update, message)


def _racially_immune_to_condition(character: dict, condition: str) -> bool:
    """
    Racial traits audit (2026-07-16): most races' signature traits were
    pure flavor text in races.py with zero mechanical hook -- confirmed
    via grep, only Half-Orc's traits were ever read anywhere. Adapts two
    of them onto the one hook this engine actually has (on_hit_condition
    is a flat apply-on-hit, no save roll to resist): Dwarven Resilience
    (real 5E: advantage on poison saves + resistance to poison damage)
    becomes flat immunity to the 'poisoned' condition; Fey Ancestry
    (real 5E: advantage vs. charmed, immune to magical sleep) becomes
    flat immunity to 'paralyzed', the closest existing condition to a
    full magical incapacitation -- there's no separate charm/sleep
    mechanic in this engine to hook the literal trait text onto more
    exactly, same kind of adaptation already used for Ranger's Favored
    Enemy and Monk's Martial Arts.
    """
    race = character.get("race")
    return (
        (condition == "poisoned" and race == "Dwarf")
        or (condition == "paralyzed" and race in ("Elf", "Half-Elf"))
    )


def _cloak_of_elvenkind_grants_advantage(character: dict, ability: str, action_text: str) -> bool:
    """
    Real 5E grants Cloak of Elvenkind's advantage specifically on
    Stealth checks, not every DEX check this game buckets together
    (climbing/balancing/lockpicking share the same "dexterity" ability
    here but aren't stealth) -- only applies when the action text
    itself reads as a stealth attempt. Real bug fixed 2026-07-15: the
    cloak's stealth_advantage field existed but nothing ever read it.
    """
    stealth_words = ("sneak", "hide", "tiptoe", "conceal", "sneaking", "hiding")
    return (
        ability == "dexterity"
        and any(w in action_text.lower() for w in stealth_words)
        and "cloak_of_elvenkind" in (character.get("equipped_accessories") or [])
    )


def _ranger_natural_explorer_grants_advantage(character: dict, ability: str, action_text: str) -> bool:
    """
    Ranger's Natural Explorer (class_features.py, 2026-07-16 audit):
    real 5E grants expertise navigating/surviving in a chosen favored
    terrain. This game has no terrain-choice mechanism (same "fixed
    default instead of a real choice" convention already used for
    Sorcerer's Draconic Bloodline/Warlock's Fiend patron), so every
    Ranger's favored terrain is forest, matching this campaign's
    woodland setting -- advantage on Wisdom checks that read as
    tracking/foraging/navigating/surviving in the wild.
    """
    survival_words = ("track", "forage", "navigate", "find shelter", "find food",
                       "hunt", "survive", "find water", "read the trail", "follow the trail")
    return (
        character.get("char_class") == "Ranger"
        and ability == "wisdom"
        and any(w in action_text.lower() for w in survival_words)
    )


def _extract_manual_roll(text: str) -> int | None:
    """
    Pulls a d20 result (1-20) out of a player's free-text reply to a
    physical-dice prompt -- "14", "I rolled a 17", "natural 20", "got a
    3" all work. Takes the FIRST number found; returns None if nothing
    in range 1-20 is present so the caller can ask again rather than
    guessing.
    """
    for match in re.finditer(r"\d+", text):
        value = int(match.group())
        if 1 <= value <= 20:
            return value
    return None


# A roll combined into the SAME message as the action (task #177, per
# Coffee's dev-topic report: "Add support so I can give an action and
# then I can say what my dice roll was in the system will understand
# that", e.g. "Chop for lumber - i rolled a 9") requires an explicit
# roll-declaration phrase near the number, unlike _extract_manual_roll's
# bare "first number 1-20" -- that's safe for a reply to a roll PROMPT
# (nothing else could be in that message), but not safe against the
# action text itself, which can contain unrelated numbers (a target
# suffix like "attack goblin 2", a quantity, part of an item/location
# name). Requiring a real roll word keeps those from ever misfiring.
_ROLL_DECLARATION_RE = re.compile(
    r"\b(?:i\s+)?roll(?:ed|ing)?(?:\s+(?:a|an|of))?\s+(?:a\s+)?(\d{1,2})\b"
    r"|\bgot\s+an?\s+(\d{1,2})\b"
    r"|\bnatural\s+(\d{1,2})\b",
    re.IGNORECASE,
)


def _extract_combined_roll(text: str) -> int | None:
    """
    Finds a manual d20 roll declared INSIDE an action message itself
    (see _ROLL_DECLARATION_RE above for why this needs a real
    roll-word, unlike _extract_manual_roll's bare-number match used for
    a dedicated roll-prompt reply). Returns None if no roll-shaped
    phrase with a value in 1-20 is present, letting the caller fall
    back to the normal prompt-and-wait flow.
    """
    match = _ROLL_DECLARATION_RE.search(text)
    if not match:
        return None
    value = int(next(g for g in match.groups() if g is not None))
    return value if 1 <= value <= 20 else None


# Task #143: a physical-dice player commonly rolls their attack die AND
# their weapon's damage die together in one go (real tabletop habit --
# no need to wait and see if it hit before picking the damage die back
# up), so this looks for a SEPARATE "for damage"/"damage of N"-shaped
# declaration in the same message _extract_combined_roll already reads
# the attack roll from. Deliberately inline-only (no blocking "roll
# your damage die" follow-up prompt if this comes back None) -- damage
# just falls back to auto-rolled, same as it already does today, so
# this is purely additive and never introduces a new wait state.
_DAMAGE_ROLL_DECLARATION_RE = re.compile(
    r"\b(\d{1,2})\s+(?:for\s+)?damage\b"
    r"|\bdamage\s*(?:was|is|roll)?\s*(?:of)?\s*:?\s*(\d{1,2})\b",
    re.IGNORECASE,
)


def _extract_combined_damage_roll(text: str) -> int | None:
    """Finds a manual damage-die roll declared inline -- see the comment above."""
    match = _DAMAGE_ROLL_DECLARATION_RE.search(text)
    if not match:
        return None
    return int(next(g for g in match.groups() if g is not None))


async def _do_skill_check(update: Update, ability: str, action_text: str, forced_roll: int | None = None) -> None:
    character = db.get_character(update.effective_user.id)
    if character is None:
        await update.effective_chat.send_message(
            "You don't have a character yet!", message_thread_id=config.TOPIC_ADVENTURE_ID
        )
        return

    location = cl.get_location(CAMPAIGN, character["current_location"])
    lockable = _find_lockable(location, action_text) if location else None

    # Physical-dice mode checked here, BEFORE branching into lockpicking
    # vs. an ordinary ability check -- real bug found live 2026-07-18
    # (Coffee: "make sure all rolls goto the player for everything"):
    # lockpicking used to dispatch to _do_lockpick immediately above,
    # completely bypassing this check even when manual dice mode
    # otherwise worked fine for every other skill check. action_text
    # alone is enough to re-derive the same lockable on the resolving
    # call, so this reuses the existing "skill_check" pending-roll kind
    # rather than needing a separate one.
    if forced_roll is None and character.get("manual_dice_enabled") and not character.get("is_ai"):
        forced_roll = _extract_combined_roll(action_text)
    if forced_roll is None and character.get("manual_dice_enabled") and not character.get("is_ai"):
        _PENDING_DICE_ROLLS[update.effective_user.id] = _new_pending_roll(
            "skill_check", action_text, update.effective_chat.id, ability=ability,
        )
        # Task #187: _safe_send, not a raw send_message -- see the attack
        # site's comment above for why.
        await _safe_send(update, f"🎲 **{character['name']}**, roll a d20 for this {ability} check and tell me the result (you have 1 minute, or I'll roll for you).")
        return

    if lockable is not None:
        await _do_lockpick(update, character, lockable, action_text, forced_roll=forced_roll)
        return

    has_advantage = (
        _cloak_of_elvenkind_grants_advantage(character, ability, action_text)
        or _ranger_natural_explorer_grants_advantage(character, ability, action_text)
    )
    result = roll_ability_check(character, ability, proficient=False, advantage=has_advantage, forced_roll=forced_roll)
    result["total"] += skill_check_proficiency_bonus(
        character["char_class"], character.get("level", 1), ability, character.get("proficiency_bonus", 0)
    )
    bonus = _practiced_bonus_for(update.effective_user.id, ability)
    result["total"] += bonus
    result["practiced_bonus"] = bonus
    success = result["total"] >= SKILL_CHECK_DC
    if success:
        db.record_skill_use(update.effective_user.id, ability)

    # Task #165: ground search/perception-flavored checks in a real fact
    # (a monster actually present at this location) rather than leaving
    # the narrator free to either invent a discovery or produce pure
    # mood with no answer at all -- see _skill_check_preamble's fix note.
    # Only wisdom checks qualify since that's this game's search/spot/
    # sense/track bucket (see intent_parser's skill_check_verb_abilities).
    grounded_fact = None
    if ability == "wisdom" and location is not None:
        monster_match = _find_monster_mentioned_in_text(location, action_text)
        if monster_match:
            monster_key, template = monster_match
            grounded_fact = (
                f"A real {template['name']} is genuinely present at this location right now."
                if success else
                f"A real {template['name']} is present here, but this roll did not reveal it."
            )

    flavor = await asyncio.to_thread(
        narrate_skill_check, character, action_text, ability,
        {**result, "ability": ability, "dc": SKILL_CHECK_DC, "success": success},
        grounded_fact=grounded_fact,
    )
    message = _format_skill_check_result(flavor, result, ability, SKILL_CHECK_DC, success)
    # _safe_send, not a direct send: a transient network failure here
    # (confirmed live 2026-07-10 — a bare ConnectTimeout on this exact
    # call ate a real player's skill check result) must not become an
    # unhandled exception. The roll itself is already decided and
    # persisted (db.record_skill_use above) before this point, so a lost
    # message here is a survivable narration gap, same reasoning as the
    # combat-resolution paths this same fix was already applied to.
    await _safe_send(update, message)


# --- Conditions ---
# Conditions live ONLY on the in-memory participant dict during a combat
# encounter (character.setdefault("conditions", [])) — they are NOT
# persisted to the database. This is a deliberate simplification: 5E
# conditions are almost always scoped to the current encounter anyway,
# and avoiding a schema change keeps this contained and easy to reason
# about. Conditions reset naturally when combat ends.

def _pick_target(action_text: str, opposing: list[dict]) -> dict:
    """
    Picks which enemy an action targets. If the player named a specific
    one (e.g. "attack goblin 2" matching a live participant named
    "Goblin 2"), that one is used; otherwise defaults to the first
    living opposing participant — a reasonable, simple default rather
    than requiring exact targeting syntax for single-enemy fights.

    Also tries a real Telegram @username tag first (2026-07-18, per
    Coffee's "attack @tagged_player" example) via the same shared
    helper every other targeting call site uses -- opposing participants
    are monsters today (no PvP yet, see task #82), so this is a no-op
    now, but means combat targeting won't need a separate fix the day
    a real player ever ends up on an opposing side.
    """
    tagged = _match_member_by_name_or_username(action_text, opposing)
    if tagged is not None:
        return tagged
    lowered = action_text.lower()
    for candidate in opposing:
        if candidate["name"].lower() in lowered:
            return candidate
    return opposing[0]


def _attack_advantage_disadvantage(attacker: dict, defender: dict) -> tuple[bool, bool]:
    """
    Computes real 5E advantage/disadvantage from conditions:
    - Prone attacker: disadvantage on their own attack rolls.
    - Poisoned attacker: disadvantage on attack rolls.
    - Prone defender: attacker gets advantage (simplified — real 5E only
      grants this to attackers within 5 ft.; distance isn't modeled here).
    If both would apply, they correctly cancel (handled inside roll_d20).

    A Ranger's Favored Enemy (2026-07-12, fixed-default convention same
    as Sorcerer's/Warlock's subclass features — no in-game "choose a
    creature type" mechanism exists) also grants advantage here: every
    Ranger defaults to goblinoids as their favored enemy, since goblin/
    goblin_shaman/goblin_boss are this campaign's single most common
    enemy type (whispering_wood, sunken_root_caverns, goblin_warrens).
    Real 5E's Favored Enemy is advantage on Survival checks to track
    them and recalling information, not combat advantage — but this
    game has no tracking/information-recall mechanic to hook into, so
    granting attack-roll advantage vs. the chosen type is the closest
    real in-combat expression of "favored enemy" available, matching
    how Rage/Sneak Attack/etc. were each adapted to fit what this
    engine actually models rather than left as flavor text.

    Blinded (2026-07-13, first new on_hit_condition since prone/
    poisoned): real 5E gives a blinded creature disadvantage on its own
    attack rolls, and attack rolls against it have advantage — the same
    shape as prone, just symmetric instead of attacker-only.

    Paralyzed and frightened (2026-07-14): a paralyzed defender grants
    advantage (real 5E also auto-crits any hit from within 5 ft., not
    modeled here — same no-positioning limitation as Sneak Attack).
    Paralyzed also fully skips the paralyzed creature's own turn (see
    _resolve_ai_turns), so it never reaches this function as an
    attacker. Frightened is disadvantage-on-attacks only, same shape as
    poisoned — real 5E's "can't willingly move closer to the fear
    source" has no equivalent since this game has no positioning.
    """
    attacker_conditions = attacker.get("conditions", [])
    defender_conditions = defender.get("conditions", [])
    # Task #223: real armor/shield proficiency -- wearing armor or
    # wielding a shield you're not trained with gives disadvantage on
    # attack rolls (a simplified stand-in for real 5E's fuller penalty,
    # which also touches ability checks/saves/spellcasting this engine
    # doesn't model uniformly enough to extend there too). A monster/
    # NPC with no equipped_armor/equipped_shield field at all is
    # unaffected -- both .get() calls are simply None for them.
    armor_unproficient = False
    for slot, category in (("equipped_armor", None), ("equipped_shield", "shield")):
        equipped_id = attacker.get(slot)
        if not equipped_id:
            continue
        item = items_module.get_item(equipped_id)
        if not item:
            continue
        item_category = category or item.get("armor_category", "light")
        if not class_features_module.is_armor_proficient(attacker.get("char_class"), item_category):
            armor_unproficient = True
    disadvantage = (
        "prone" in attacker_conditions or "poisoned" in attacker_conditions
        or "blinded" in attacker_conditions or "frightened" in attacker_conditions
        or armor_unproficient
    )
    # Weather hazard (per Coffee, 2026-07-21: "raining = wet = slippery
    # variable"): a real, per-attack chance of disadvantage while
    # fighting in rain/thunderstorms on the surface or open sky --
    # never underground, where weather never reaches (world_clock's own
    # rule). Rolled fresh each attack (a real environmental risk, not a
    # flat always-on penalty), and applies to BOTH sides fighting here,
    # not just whoever the "attacker" happens to be this swing.
    attacker_location_id = attacker.get("current_location") or defender.get("current_location")
    weather_hazard = False
    if attacker_location_id:
        attacker_location = cl.get_location(CAMPAIGN, attacker_location_id)
        if attacker_location and world_clock.is_hazardous(attacker_location["layer"]):
            weather_hazard = roll_d20() <= 5
    disadvantage = disadvantage or weather_hazard
    # Task #131 skill-tree upgrade "true_aim": extends Favored Enemy's
    # advantage to wolves too, this campaign's other very common enemy.
    ranger_extra_favored = (
        _has_skill_upgrade(attacker, "true_aim") and defender.get("monster_key") == "wolf"
    )
    favored_enemy = (attacker.get("char_class") == "Ranger"
                      and (defender.get("monster_key", "").startswith("goblin") or ranger_extra_favored))
    # Reckless Attack (Barbarian, 2026-07-16 level-2-10 audit): advantage
    # on your own attacks this turn once declared. Real 5E's downside
    # (attacks against you also get advantage until your next turn) is
    # deliberately NOT modeled -- this engine has no clean "start of your
    # next turn" hook outside the turn-order loop itself, and bolting
    # one on just for this would be a bigger, riskier change than the
    # value of one more (documented) simplification justifies.
    reckless = attacker.pop("reckless_active", False)
    # Night aggression (per Coffee, 2026-07-21: "Night time monsters are
    # more aggressive"): real hostile monsters/NPCs (never a real
    # player OR an AI-controlled PARTY companion -- both always carry a
    # real char_class, which nothing in this game's monster templates
    # ever sets) get advantage on their attacks after dark. Time of day
    # is a pure real-clock fact (world_clock.is_night), not location-
    # dependent, matching how time-of-day is already treated everywhere
    # else in this game (e.g. _do_look's own conditions_line).
    monster_night_aggression = world_clock.is_night() and not attacker.get("char_class")
    # Hybrid classes (2026-07-22): a Ranger hybrid's Favored-Enemy-
    # flavored advantage (deterministic vs. goblins/wolves, same as the
    # real feature) and a Barbarian hybrid's Reckless-Attack-flavored
    # advantage chance -- see hybrid_features.py.
    hybrid_favored = hybrid_features.hybrid_favored_enemy_advantage(attacker, defender.get("monster_key", ""))
    hybrid_reckless = hybrid_features.hybrid_reckless_advantage(attacker)
    advantage = (
        "prone" in defender_conditions or "blinded" in defender_conditions
        or "paralyzed" in defender_conditions or favored_enemy or reckless
        or monster_night_aggression or hybrid_favored or hybrid_reckless
    )
    return advantage, disadvantage


def _condition_tags(character: dict) -> str:
    """Short display tags for a character's active conditions, e.g. '🛌😷'."""
    icons = {
        "prone": "🛌", "poisoned": "😷", "blinded": "🙈", "silenced": "🔇",
        "paralyzed": "⛓️", "frightened": "😱",
    }
    conditions = character.get("conditions", [])
    return "".join(icons.get(c, "") for c in conditions)


async def _do_shove(update: Update, action_text: str, forced_roll: int | None = None) -> None:
    """
    A contested STR (Athletics) check to knock an enemy prone — a real
    5E combat action, using your turn's action, per the rules (not a
    free action). Success applies the 'prone' condition to the target.
    Physical-dice mode (2026-07-18) only ever prompts for the ATTACKER's
    own roll -- the target's contest roll stays internal, same as every
    other roll made on behalf of an opposing combatant a player can't
    physically roll for themselves.
    """
    chat_id = update.effective_chat.id
    async with sessions.get_lock(chat_id):
        session = sessions.get_session(chat_id)
        if session is None:
            await update.effective_chat.send_message(
                "No combat is active right now.", message_thread_id=config.TOPIC_ADVENTURE_ID
            )
            return

        user_id = update.effective_user.id
        if session.current_participant_id() != user_id:
            await _self_heal_stuck_ai_turn(update, session)
            session = sessions.get_session(chat_id)
            if session is None:
                await update.effective_chat.send_message(
                    "Combat had stalled and just resolved itself — nothing active right now.",
                    message_thread_id=config.TOPIC_ADVENTURE_ID,
                )
                return
        if session.current_participant_id() != user_id:
            current_name = session.current_participant()["name"]
            await update.effective_chat.send_message(
                f"It's not your turn — it's **{current_name}**'s turn.",
                message_thread_id=config.TOPIC_ADVENTURE_ID,
            )
            return

        attacker = session.current_participant()
        if attacker["hp_current"] <= 0:
            await update.effective_chat.send_message(
                "You're unconscious (0 HP) and can't act until healed.",
                message_thread_id=config.TOPIC_ADVENTURE_ID,
            )
            return

        opposing = session.living_on_side(session.opposing_side(user_id))
        if not opposing:
            if not await _try_end_stale_combat(update, session):
                await update.effective_chat.send_message(
                    "No valid targets remain.", message_thread_id=config.TOPIC_ADVENTURE_ID
                )
            return
        target = _pick_target(action_text, opposing)

        if forced_roll is None and attacker.get("manual_dice_enabled") and not attacker.get("is_ai"):
            forced_roll = _extract_combined_roll(action_text)
        if forced_roll is None and attacker.get("manual_dice_enabled") and not attacker.get("is_ai"):
            _PENDING_DICE_ROLLS[user_id] = _new_pending_roll("shove", action_text, update.effective_chat.id)
            # Task #187: _safe_send, not a raw send_message -- see the
            # attack site's comment above for why.
            await _safe_send(update, f"🎲 **{attacker['name']}**, roll a d20 for your shove and tell me the result (you have 1 minute, or I'll roll for you).")
            return

        attacker_result = roll_ability_check(attacker, "strength", proficient=True, forced_roll=forced_roll)
        target_raw = roll_d20()
        target_mod = max(ability_modifier(target.get("strength", 10)), ability_modifier(target.get("dexterity", 10)))
        target_total = target_raw + target_mod
        success = attacker_result["total"] > target_total  # ties favor the defender, per 5E contest rules

        if success:
            target.setdefault("conditions", [])
            if "prone" not in target["conditions"]:
                target["conditions"].append("prone")

        flavor = await asyncio.to_thread(
            narrate_skill_check, attacker, action_text, "strength",
            {"raw_roll": attacker_result["raw_roll"], "total": attacker_result["total"],
             "success": success},
        )
        banner = "✨ **Success!**" if success else "💨 **Failure...**"
        message = (
            f"{banner}\n> {flavor}\n\n"
            f"🎲 **Shove contest:** {attacker['name']} {attacker_result['total']} vs "
            f"{target['name']} {target_total}"
        )
        if success:
            message += f"\n🛌 **{target['name']} is now PRONE** — attacks against them have advantage."
        await _safe_send(update, message)

        session.advance_turn()
        await _resolve_ai_turns(update, session)


async def _resolve_flee_attempt(update, session: sessions.Session, action_text: str, forced_roll: int | None = None) -> None:
    """
    Real core of a flee attempt, shared by _do_flee (a human player,
    below) and _resolve_ai_turns (an AI party member acting on a human's
    mid-combat guidance to retreat — task #222). Assumes the caller
    already holds sessions.get_lock(session.chat_id) and that it's
    genuinely the fleeing participant's own turn -- both call sites
    confirm that before reaching here. Extracted 2026-07-21 so there's
    one real implementation instead of two.
    """
    fleeing = session.current_participant()
    user_id = fleeing["telegram_user_id"]
    chat_id = session.chat_id
    if fleeing["hp_current"] <= 0:
        await update.effective_chat.send_message(
            "You're unconscious (0 HP) and can't act until healed.",
            message_thread_id=config.TOPIC_ADVENTURE_ID,
        )
        return

    opposing = session.living_on_side(session.opposing_side(user_id))
    if any(e.get("is_boss") for e in opposing):
        await update.effective_chat.send_message(
            "🚫 There's no fleeing this fight — whatever you're facing won't let you leave.",
            message_thread_id=config.TOPIC_ADVENTURE_ID,
        )
        return

    if forced_roll is None and fleeing.get("manual_dice_enabled") and not fleeing.get("is_ai"):
        forced_roll = _extract_combined_roll(action_text)
    if forced_roll is None and fleeing.get("manual_dice_enabled") and not fleeing.get("is_ai"):
        _PENDING_DICE_ROLLS[user_id] = _new_pending_roll("flee", action_text, update.effective_chat.id)
        # Task #187: _safe_send, not a raw send_message -- see the
        # attack site's comment above for why.
        await _safe_send(update, f"🎲 **{fleeing['name']}**, roll a d20 for your escape attempt and tell me the result (you have 1 minute, or I'll roll for you).")
        return

    # Ranger's Danger Sense (task #91, 2026-07-18): already real for
    # spell saves (spells.py's ranger_danger_sense_advantage) but
    # never reached this DEX-based escape roll -- the one other
    # place in this engine a "Dexterity saving throw" genuinely
    # happens, so a level 2+ Ranger's own claimed "advantage on
    # Dexterity saving throws" (class_features.py) was incomplete.
    has_danger_sense = spells_module.ranger_danger_sense_advantage(fleeing, "dexterity")
    result = roll_ability_check(
        fleeing, "dexterity", proficient=False, advantage=has_danger_sense, forced_roll=forced_roll
    )
    success = result["total"] >= SKILL_CHECK_DC

    flavor = await asyncio.to_thread(
        narrate_skill_check, fleeing, action_text, "dexterity",
        {**result, "ability": "dexterity", "dc": SKILL_CHECK_DC, "success": success},
    )
    message = _format_skill_check_result(flavor, result, "dexterity", SKILL_CHECK_DC, success)

    if not success:
        await _safe_send(update, f"{message}\n\n💨 The attempt fails — you're still in the fight.")
        session.advance_turn()
        await _resolve_ai_turns(update, session)
        return

    # Opportunity attacks (2026-07-13): breaking off from a fight
    # isn't a real Disengage action, it's turning your back mid-melee
    # -- real 5E lets every enemy still standing take one free swing
    # as you go. This game has no positioning system to gate that on
    # (see resolve_attack's Sneak Attack comment on the same
    # limitation), so every living opposing enemy gets one plain
    # attack roll (no advantage/disadvantage) against the fleeing
    # character before the escape is finalized. Deliberately skips
    # the AI narrator per hit -- with several enemies that would
    # stack multiple 30-160s Ollama calls onto a single flee attempt
    # -- and reuses _format_combat_result with no flavor text for a
    # fast, fully deterministic result instead.
    # Rogue's Cunning Action (level 2+, task #91 audit, 2026-07-19):
    # real 5E lets a Rogue Disengage as a bonus action, meaning
    # THEIR flee never provokes the opportunity attacks every other
    # class's does -- the exact, already-existing block below is
    # precisely what Disengage removes, so this is a clean
    # skip-the-loop rather than new combat plumbing. Reachable right
    # now (a real level 2 Rogue is already playing), unlike most of
    # this game's remaining level 5+ class-feature gaps.
    has_cunning_action = fleeing.get("char_class") == "Rogue" and fleeing.get("level", 1) >= 2
    opportunity_blocks = []
    _refresh_real_player_spell_slots(fleeing)
    for enemy in ([] if has_cunning_action else session.living_on_side(session.opposing_side(user_id))):
        if fleeing["hp_current"] <= 0:
            break
        atk_result = resolve_attack(
            enemy, fleeing, _weapon_for_attacker(enemy), round_number=session.round_number,
        )
        opportunity_blocks.append(_format_combat_result(
            "", atk_result, enemy["name"], fleeing["name"],
        ))
    if opportunity_blocks:
        _sync_player_to_db(fleeing)
        message = message + "\n\n🗡️ **Opportunity attacks as you break away:**\n\n" + "\n\n".join(opportunity_blocks)
    elif has_cunning_action:
        message = message + f"\n\n🗲 **{fleeing['name']}**'s Cunning Action lets them Disengage — no opportunity attacks on the way out!"

    if fleeing["hp_current"] <= 0:
        await _safe_send(
            update,
            f"{message}\n\n⚠️ **{fleeing['name']} is cut down before escaping — knocked unconscious!** "
            f"Still in the fight, rolling death saving throws on their turns until stable, revived, or worse.",
        )
        session.advance_turn()
        await _resolve_ai_turns(update, session)
        return

    character = db.get_character(user_id)
    destination_id = _nearest_safe_waypoint(character) if character else SAFE_LOCATION_FALLBACK
    destination_name = cl.get_location(CAMPAIGN, destination_id)["name"]
    if character:
        db.update_character(user_id, current_location=destination_id)

    session.remove_dead_player(user_id)
    combat_over = session.is_combat_over()

    await _safe_send(
        update,
        f"{message}\n\n🏃 **You break away and flee to {destination_name}!**"
        + ("\n\n🏳️ With you gone, the fight has no one left to finish — it ends here." if combat_over else ""),
    )
    if combat_over:
        sessions.end_session(chat_id)
    else:
        await _resolve_ai_turns(update, session)


async def _do_flee(update: Update, action_text: str, forced_roll: int | None = None) -> None:
    """
    A real dice roll to escape an active fight — "cancel" no longer
    force-ends combat for ordinary players (see adventure_master_handler's
    UNIVERSAL_ESCAPE_PHRASES handling), so this is the actual way out.
    Impossible against any enemy flagged is_boss in campaign.json —
    those fights don't let you walk away. Uses the same fixed
    SKILL_CHECK_DC as every other check in this game, on purpose, so
    difficulty is never something the AI gets to invent. Confirms it's
    really the caller's own turn, then hands off to the shared
    _resolve_flee_attempt above.
    """
    chat_id = update.effective_chat.id
    async with sessions.get_lock(chat_id):
        session = sessions.get_session(chat_id)
        if session is None:
            await update.effective_chat.send_message(
                "No combat is active right now.", message_thread_id=config.TOPIC_ADVENTURE_ID
            )
            return

        user_id = update.effective_user.id
        if session.current_participant_id() != user_id:
            await _self_heal_stuck_ai_turn(update, session)
            session = sessions.get_session(chat_id)
            if session is None:
                await update.effective_chat.send_message(
                    "Combat had stalled and just resolved itself — nothing active right now.",
                    message_thread_id=config.TOPIC_ADVENTURE_ID,
                )
                return
        if session.current_participant_id() != user_id:
            current_name = session.current_participant()["name"]
            await update.effective_chat.send_message(
                f"It's not your turn — it's **{current_name}**'s turn.",
                message_thread_id=config.TOPIC_ADVENTURE_ID,
            )
            return

        await _resolve_flee_attempt(update, session, action_text, forced_roll)


async def _do_message_ai(update: Update, text: str) -> None:
    """
    Task #222, per Coffee: "like /msg for human players," a way to tell
    an AI-controlled party member something -- in or out of combat --
    that never counts as anyone's turn. No session.advance_turn() call
    anywhere on this path, on purpose. The message is stashed as real
    guidance (_AI_PLAYER_CONTEXTS[...].user_data["human_guidance"]),
    read exactly once by whichever of the two real consumers applies:
    _resolve_ai_turns, on that companion's own next combat turn (acted
    on immediately if it reads as a retreat), or _ai_party_autonomous_tick,
    on their next idle-tick decision outside combat.

    Real bug, live-caught (2026-07-21, Coffee: "/msg @ShesAQueen_78 wow
    menu is crazy good!!!" -- got back "Not sure who you're talking to
    — name an AI party member"): this originally only ever matched
    AI-controlled candidates, so /msg toward a REAL human player (which
    Coffee's own original request explicitly named as the precedent --
    "Jus like we can msg human players with /msg") always failed with a
    confusing AI-only error, even though the message had already
    reached that player fine via the shared chat itself. Now resolves
    against the full active roster (real @username tagging included,
    via the same _match_member_by_name_or_username every other
    targeting call site already uses) and branches: an AI target still
    gets real guidance stored for its next turn/tick; a real human
    target just gets a quiet confirmation, since Telegram already
    delivered the message the moment it was sent -- there's nothing
    further for the bot to do for a human recipient.
    """
    character = db.get_character(update.effective_user.id)
    if character is None:
        await update.effective_chat.send_message(
            "You don't have a character yet!", message_thread_id=config.TOPIC_ADVENTURE_ID
        )
        return

    session = sessions.get_session(update.effective_chat.id)
    if session is not None:
        candidates = [
            p for p in session.participants
            if session.sides.get(p["telegram_user_id"]) == "party"
        ]
    else:
        candidates = _get_party_members()
    candidates = [p for p in candidates if p["telegram_user_id"] != character["telegram_user_id"]]

    target = _match_member_by_name_or_username(text, candidates)
    if target is None:
        await update.effective_chat.send_message(
            "Not sure who you're talking to — name a party member, or tag them with @username.",
            message_thread_id=config.TOPIC_ADVENTURE_ID,
        )
        return

    if not target.get("is_ai"):
        await _safe_send(update, f"🗣️ **{character['name']}** tells **{target['name']}**: \"{text}\"")
        return

    context_like = _AI_PLAYER_CONTEXTS.setdefault(target["telegram_user_id"], _AiPlayerContext())
    context_like.user_data["human_guidance"] = text
    await _safe_send(update, f"🗣️ **{character['name']}** tells **{target['name']}**: \"{text}\"")


NATURAL_HEALING_FULL_REST_HOURS = config.NATURAL_HEALING_FULL_REST_HOURS  # moved to config.py 2026-07-14
# Real-world hours of uninterrupted resting needed to fully recover HP
# and spell slots; partial rest heals the proportional fraction. Changed
# 2026-07-11 per Coffee's explicit direction: resting used to be an
# instant full heal the moment you said "I rest" or went inactive — he
# wants players to actually need OTHER means (potions, healing spells)
# for in-the-moment recovery, and for "resting" to mean real downtime,
# not a free heal button. Both the explicit `rest` action and going
# inactive now share this same real-time-gated mechanic (see
# _go_inactive and _apply_natural_healing) rather than each having its
# own separate instant-heal behavior.


WARLOCK_PACT_MAGIC_REST_HOURS = NATURAL_HEALING_FULL_REST_HOURS / 8
# Real 5E Pact Magic: a Warlock's spell slots recover on a SHORT rest
# (~1 hour) rather than the long rest (~8 hours) everyone else's
# spellcasting needs -- roughly 1/8th the time. This game has no
# separate short/long rest distinction, only one shared real-time
# healing curve (NATURAL_HEALING_FULL_REST_HOURS), so Pact Magic is
# modeled as Warlocks alone using a much shorter curve for SPELL SLOTS
# specifically (their HP still follows the normal shared curve, same
# as every other class).


def _song_of_rest_bonus(character: dict) -> int:
    """
    Bard's Song of Rest (real 5E, level 2+, previously pure flavor
    text): resting alongside a Bard heals everyone a bit extra,
    including the Bard themselves. Real 5E ties this to hit-dice
    spending during a short rest; this engine's own rest model is
    proportional-to-real-time instead of hit-dice-based, so the
    closest honest analogue is the rule's own base die (1d6) added on
    top of whatever the normal rest already recovered, rather than
    inventing a percentage bonus with no basis in the actual rule.
    """
    party_id = character.get("party_id")
    party = db.get_party_members_by_id(party_id) if party_id else [character]
    has_bard = any(m.get("char_class") == "Bard" and m.get("level", 1) >= 2 for m in party)
    return roll(1, 6)[0] if has_bard else 0


def _apply_natural_healing(telegram_user_id: int, character: dict, elapsed_seconds: float) -> tuple[int, int]:
    """
    Heals a resting character in proportion to how much real-world time
    has actually passed, capped at a full recovery after
    NATURAL_HEALING_FULL_REST_HOURS (or, for a Warlock's spell slots
    only, WARLOCK_PACT_MAGIC_REST_HOURS). This IS the computed game fact
    (rules-layer, deterministic) — callers just report whatever this
    returns, same convention as every other outcome in this game.
    Returns (hp_healed, slots_healed) actually applied (0, 0 if no time
    has meaningfully passed or the character is already full).
    """
    fraction = min(1.0, max(0.0, elapsed_seconds) / (NATURAL_HEALING_FULL_REST_HOURS * 3600))
    slot_rest_hours = (
        WARLOCK_PACT_MAGIC_REST_HOURS if character.get("char_class") == "Warlock" else NATURAL_HEALING_FULL_REST_HOURS
    )
    slot_fraction = min(1.0, max(0.0, elapsed_seconds) / (slot_rest_hours * 3600))
    missing_hp = character["hp_max"] - character["hp_current"]
    missing_slots = character["spell_slots_max"] - character["spell_slots_current"]
    hp_gain = min(missing_hp, round(missing_hp * fraction))
    slot_gain = min(missing_slots, round(missing_slots * slot_fraction))
    if hp_gain > 0:
        hp_gain = min(missing_hp, hp_gain + _song_of_rest_bonus(character))
    # Hybrid Wizard/Sorcerer (2026-07-22): a chance-gated bonus slot on
    # a full rest, Arcane-Recovery-flavored -- only on a COMPLETED full
    # rest (fraction >= 1.0), same convention as the limited-use
    # feature resets below, not the gradual proportional recovery above.
    if fraction >= 1.0:
        slot_gain = min(missing_slots, slot_gain + hybrid_features.hybrid_rest_slot_recovery(character))
    if hp_gain > 0 or slot_gain > 0:
        db.update_character(
            telegram_user_id,
            hp_current=character["hp_current"] + hp_gain,
            spell_slots_current=character["spell_slots_current"] + slot_gain,
        )
    # Limited-use class/racial features (Second Wind, Rage, Bardic
    # Inspiration, Lay on Hands, Relentless Endurance) are real "per long
    # rest" resources, not gradually recovered like HP/spell slots above
    # -- they only reset once a FULL rest has actually completed.
    if fraction >= 1.0 and character.get("feature_uses"):
        db.reset_feature_uses(telegram_user_id)
    return hp_gain, slot_gain


async def _do_rest(update: Update) -> None:
    """
    "I rest" / "heal up" / "recover" — now just a phrasing alias for
    settling in to rest; it shares _go_inactive's real-time-gated
    healing instead of being a separate instant full heal. See
    NATURAL_HEALING_FULL_REST_HOURS.
    """
    chat_id = update.effective_chat.id
    if sessions.get_session(chat_id) is not None:
        await update.effective_chat.send_message(
            "You can't rest in the middle of combat.", message_thread_id=config.TOPIC_ADVENTURE_ID
        )
        return

    telegram_user_id = update.effective_user.id
    character = db.get_character(telegram_user_id)
    if character is None:
        await update.effective_chat.send_message(
            "You don't have a character yet!", message_thread_id=config.TOPIC_ADVENTURE_ID
        )
        return
    if character.get("is_inactive"):
        await update.effective_chat.send_message(
            "You're already resting.", message_thread_id=config.TOPIC_ADVENTURE_ID
        )
        return

    hp_full = character["hp_current"] == character["hp_max"]
    slots_full = character["spell_slots_current"] == character["spell_slots_max"]
    if hp_full and slots_full:
        await update.effective_chat.send_message(
            "You're already at full health and spell slots.", message_thread_id=config.TOPIC_ADVENTURE_ID
        )
        return

    await _go_inactive(telegram_user_id, update, "settles in to rest and recover")


def _nearest_safe_waypoint(character: dict) -> str:
    """
    Where a character ends up when going inactive/resting: their
    current location if it's already tagged safe (an inn/tavern), else
    the most recently visited safe location, else the Crossroads Tavern
    — the only location tagged `"safe": true` in this campaign, but
    this stays correct if a future campaign adds more.
    """
    current_loc = cl.get_location(CAMPAIGN, character["current_location"])
    if current_loc and current_loc.get("safe"):
        return character["current_location"]
    for loc_id in reversed(character["visited_locations"]):
        loc = cl.get_location(CAMPAIGN, loc_id)
        if loc and loc.get("safe"):
            return loc_id
    return SAFE_LOCATION_FALLBACK


def _in_active_combat(telegram_user_id: int, chat_id: int) -> bool:
    session = sessions.get_session(chat_id)
    return session is not None and telegram_user_id in session.turn_order


async def _go_inactive(telegram_user_id: int, update_like, reason_text: str) -> None:
    """
    Marks a character inactive ("resting"): moves them to the nearest
    safe waypoint, starts the real-time healing clock (rest_started_at),
    and announces it. Healing is NOT applied here — it accrues only
    while actually resting and is computed and applied once, on
    reactivation (see adventure_master_handler's wake block and
    _apply_natural_healing), based on how much real time passed between
    this call and that one. Shared by the explicit `rest` action
    (_do_rest), the explicit "rest for X"/"I'm done for now" action
    (_do_go_inactive), and the automatic 5-minute idle check —
    `update_like` is either a real Update or a _ChatOnlyUpdate shim.

    Changed 2026-07-11 (Coffee's direction): resting used to fully heal
    the instant a character went inactive — a "say the word, come back
    full" free heal that undercut needing potions/spells for real
    recovery. Now it only ever heals in proportion to genuine elapsed
    real time (NATURAL_HEALING_FULL_REST_HOURS to fully recover),
    checked at wake time, not at rest-start time.

    NEVER call this while the character is in an active combat session
    — per design, resting requires actually being out of battle first
    (see _do_go_inactive's guard and _check_idle_characters' combat
    handling, which auto-passes an idle player's turn instead of
    resting them out of danger).
    """
    character = db.get_character(telegram_user_id)
    if character is None or character.get("is_inactive"):
        return

    destination_id = _nearest_safe_waypoint(character)
    destination_name = cl.get_location(CAMPAIGN, destination_id)["name"]

    db.move_character(telegram_user_id, destination_id)
    db.mark_visited(telegram_user_id, destination_id)
    db.mark_inactive(telegram_user_id)
    db.update_character(telegram_user_id, rest_started_at=datetime.now(timezone.utc).isoformat())

    await _safe_send(
        update_like,
        f"😴 **{character['name']}** {reason_text} at **{destination_name}**, safe until they return — "
        f"they'll recover naturally the longer they rest.",
    )


async def _do_go_inactive(update: Update, duration_text: str) -> None:
    telegram_user_id = update.effective_user.id
    character = db.get_character(telegram_user_id)
    if character is None:
        await update.effective_chat.send_message(
            "You don't have a character yet!", message_thread_id=config.TOPIC_ADVENTURE_ID
        )
        return
    if character.get("is_inactive"):
        await update.effective_chat.send_message(
            "You're already resting.", message_thread_id=config.TOPIC_ADVENTURE_ID
        )
        return
    if _in_active_combat(telegram_user_id, update.effective_chat.id):
        await update.effective_chat.send_message(
            "You can't rest in the middle of a fight — escape or finish the battle first.",
            message_thread_id=config.TOPIC_ADVENTURE_ID,
        )
        return

    reason = f"settles in to rest{f' for {duration_text}' if duration_text else ''}, until next session"
    await _go_inactive(telegram_user_id, update, reason)


async def _check_idle_characters(bot) -> None:
    """
    Runs on a repeating background loop (see main()): a real, currently-
    active character quiet for IDLE_WARNING_SECONDS gets ONE warning
    naming where they'll end up; if they're STILL quiet by
    IDLE_TIMEOUT_SECONDS, inactivity actually commences — UNLESS they're
    in active combat, since resting requires being out of battle first
    (same rule as the explicit command). An idle player mid-combat
    instead just has their turn auto-passed once the full timeout is
    reached, so they don't block everyone else, but they stay "in the
    fight" — at whatever risk that implies — rather than getting a free
    pass to safety by going quiet. No separate warning is given for the
    in-combat case; the normal turn/timeout pressure already applies.
    """
    if _LAST_KNOWN_CHAT_ID is None:
        return  # nobody has said anything yet this run — nothing to check against

    now = datetime.now(timezone.utc)

    for character in db.get_idle_real_characters():
        try:
            last_active = datetime.fromisoformat(character["last_active_at"])
        except (TypeError, ValueError):
            continue
        idle_seconds = (now - last_active).total_seconds()
        telegram_user_id = character["telegram_user_id"]

        if idle_seconds < IDLE_WARNING_SECONDS:
            continue

        if idle_seconds < IDLE_TIMEOUT_SECONDS:
            if telegram_user_id in _IDLE_WARNED or _in_active_combat(telegram_user_id, _LAST_KNOWN_CHAT_ID):
                continue
            _IDLE_WARNED.add(telegram_user_id)
            destination_name = cl.get_location(CAMPAIGN, _nearest_safe_waypoint(character))["name"]
            update_like = _ChatOnlyUpdate(bot, _LAST_KNOWN_CHAT_ID)
            await _safe_send(
                update_like,
                f"⚠️ **{character['name']}** has been quiet a while — reply soon, or they'll "
                f"head off to rest at **{destination_name}**.",
            )
            continue

        _IDLE_WARNED.discard(telegram_user_id)
        if _in_active_combat(telegram_user_id, _LAST_KNOWN_CHAT_ID):
            async with sessions.get_lock(_LAST_KNOWN_CHAT_ID):
                session = sessions.get_session(_LAST_KNOWN_CHAT_ID)
                if session is not None and session.current_participant_id() == telegram_user_id:
                    update_like = _ChatOnlyUpdate(bot, _LAST_KNOWN_CHAT_ID)
                    session.advance_turn()
                    await _safe_send(update_like, f"⏳ **{character['name']}** is idle — turn passed.")
                    await _resolve_ai_turns(update_like, session)
            continue

        update_like = _ChatOnlyUpdate(bot, _LAST_KNOWN_CHAT_ID)
        await _go_inactive(telegram_user_id, update_like, "drifts off to rest")


# ---------------------------------------------------------------------
# Quests — journal, clues, acceptance, and deterministic completion
# detection. Whether a quest is complete is always a real, computed
# fact (reached a location, defeated a specific monster, matched a
# puzzle's real answer) — never left to AI narration to decide, same
# convention as every other outcome in this game.
# ---------------------------------------------------------------------

def _offerable_quest_at_location(character: dict, location_id: str) -> tuple[str, dict] | None:
    """The first not-yet-completed, not-yet-active quest whose 'location' matches, if any."""
    for quest_id, quest in CAMPAIGN.get("quests", {}).items():
        if quest.get("location") != location_id:
            continue
        if quest_id in character["completed_quests"] or quest_id in character["active_quests"]:
            continue
        return quest_id, quest
    return None


def _offerable_companion_quest(character: dict) -> tuple[str, dict] | None:
    """
    A real party companion's own personal quest (2026-07-14, per
    Coffee: each recruitable should have "a mission or quest they go
    on with players" -- e.g. Sarah mentioning a task she'd like help
    with once recruited). Matched by 'giver_npc' against whoever's
    ACTUALLY in the party right now, not by location -- a companion's
    own request travels with the party rather than being tied to
    wherever they happened to be recruited, and this deliberately
    never collides with the ordinary location-based quests above
    (this game's only other quest with a 'location' field matching
    that spot might already be a different quest entirely).

    Real live bug (2026-07-22, Coffee: "what is wrong with the quest
    system?" -- accepted a quest right after talking to Wren
    Hollowbrook and got Pip Thistledown's quest instead): this used to
    scope "who's in the party" via _get_party_members(), which returns
    EVERY active character in the ENTIRE GAME, not this character's own
    party -- so a companion recruited by a completely different player
    elsewhere could still get offered here, and dict order (not
    recency or relevance) decided which one won. Now scoped to this
    character's own real party_id, the same way _check_story_gate's
    companion-trust check already does it.
    """
    party_id = character.get("party_id")
    if not party_id:
        return None
    party_npc_ids = {
        _find_npc_id_by_name(p["name"]) for p in db.get_party_members_by_id(party_id) if p.get("is_ai")
    }
    for quest_id, quest in CAMPAIGN.get("quests", {}).items():
        giver = quest.get("giver_npc")
        if not giver or giver not in party_npc_ids:
            continue
        if quest_id in character["completed_quests"] or quest_id in character["active_quests"]:
            continue
        return quest_id, quest
    return None


def _npc_quest_facts(character: dict, npc_id: str) -> str | None:
    """
    Real, current quest info to ground an NPC's dialogue when asked
    about "your quest"/"the reward"/etc. Confirmed live 2026-07-11:
    asking Grimsby about his own quest got a vague non-answer, since
    nothing ever told the model what quest this NPC is connected to.

    Story quests have no structured NPC field in campaign.json (only a
    location), so any NPC standing at the quest's location is grounded
    in it -- an approximation, but the only one the data supports, and
    consistent with how "Guilds you could join" etc. don't check
    location either. Board quests DO store a real giver_npc, checked
    exactly. Never reveals a branching quest's outcome_facts (the
    actual narrative consequences) -- only the setup and the choices'
    real rewards, matching what a player could learn by simply asking.
    """
    location_id = character["current_location"]
    lines = []

    story_offer = _offerable_quest_at_location(character, location_id)
    if story_offer:
        _, quest = story_offer
        reward_bits = [f"{quest['reward_xp']} XP"] if quest.get("reward_xp") else []
        if quest.get("reward_gold"):
            reward_bits.append(f"{quest['reward_gold']} gold")
        if quest.get("reward_item"):
            reward_bits.append(items_module.get_item(quest["reward_item"])["name"])
        reward_text = f" Reward: {', '.join(reward_bits)}." if reward_bits else ""
        lines.append(f"Your real quest to offer: \"{quest['title']}\" — {quest['description']}{reward_text}")

    board_quests = board_quests_module.get_todays_board_quests(location_id)
    for q in board_quests:
        if q.get("giver_npc") != npc_id or q.get("completed_at"):
            continue
        if q.get("branch_data"):
            branch = q["branch_data"]
            choice_bits = []
            for c in branch["choices"].values():
                reward_bits = [f"{c['reward_xp']} XP", f"{c['reward_gold']} gold"]
                choice_bits.append(f"\"{c['label']}\" ({', '.join(reward_bits)})")
            lines.append(
                f"Your real board quest to offer: \"{q['title']}\" — {branch['setup_narration']} "
                f"The real choices and their rewards: {'; '.join(choice_bits)}."
            )
        else:
            lines.append(
                f"Your real board quest to offer: \"{q['title']}\" — {q['description']} "
                f"Reward: {q['reward_xp']} XP, {q['reward_gold']} gold."
            )

    if not lines:
        return None
    return (
        "What you can ACTUALLY tell the player about quests (the ONLY real details — "
        "never invent a different reward or description if asked):\n" + "\n".join(lines)
    )


def _current_story_arc(character: dict) -> tuple[str, dict] | None:
    """
    The earliest story arc (in campaign.json's own narrative order --
    Discovery -> Descent -> What Was Buried -> What Waits Above) that
    still has at least one quest this character hasn't completed yet.
    None once every arc's quests are all done (the story's finished).
    """
    completed = set(character["completed_quests"])
    for arc_id, arc in CAMPAIGN.get("story_arcs", {}).items():
        if not set(arc.get("quests", [])).issubset(completed):
            return arc_id, arc
    return None


def _story_arc_for_quest(quest_id: str) -> tuple[str, dict] | None:
    """
    Which story arc (if any) a quest belongs to, per campaign.json's
    story_arcs -- this data mapped the whole campaign's main questline
    (Discovery -> The Descent -> What Was Buried -> What Waits Above)
    but nothing in bot.py ever read it (2026-07-15 reverse-playthrough
    finding). Sorted so the check below always finds the LOWEST arc a
    quest appears in first, matching story_arcs' own narrative order.
    """
    for arc_id, arc in CAMPAIGN.get("story_arcs", {}).items():
        if quest_id in arc.get("quests", []):
            return arc_id, arc
    return None


def _chapter_complete_note(telegram_user_id: int, quest_id: str) -> str:
    """
    If completing quest_id just finished every quest in its story arc,
    return a real "chapter complete" narrative beat -- otherwise "".
    Uses the character's actual completed_quests, not level, so this
    fires exactly when the last real quest in an arc is turned in.
    """
    arc_info = _story_arc_for_quest(quest_id)
    if arc_info is None:
        return ""
    arc_id, arc = arc_info
    character = db.get_character(telegram_user_id)
    if character is None:
        return ""
    completed = set(character["completed_quests"])
    if not set(arc["quests"]).issubset(completed):
        return ""
    return f"\n\n🌟 **Chapter complete: \"{arc['title']}\"** — {arc['description']}"


async def _arc_opening_note(character: dict, quest_id: str, quest: dict) -> str:
    """
    Cutscene bookend to _chapter_complete_note's closing beat, per
    Coffee's request for RPG-style cutscenes on story/character quests
    (2026-07-19/20): fires a real AI-narrated opening (narrate_arc_
    opening) the moment a character takes on the FIRST quest of a story
    arc they've never touched before -- checked against completed_quests
    AND active_quests so this can only ever fire once per arc per
    character, never on a re-accept. Returns "" for any quest that isn't
    literally arc["quests"][0], so mid-arc quests stay a plain accept.
    """
    arc_info = _story_arc_for_quest(quest_id)
    if arc_info is None:
        return ""
    _, arc = arc_info
    arc_quests = arc.get("quests", [])
    if not arc_quests or arc_quests[0] != quest_id:
        return ""
    already_touched = set(character["completed_quests"]) | set(character["active_quests"].keys())
    if already_touched & set(arc_quests):
        return ""
    opening_text = await asyncio.to_thread(
        narrate_arc_opening, arc["title"], arc["description"], quest["title"],
    )
    return f"🎬 **{arc['title']}**\n{opening_text}\n\n"


def _arc_is_reached(character: dict, arc_id: str, arc: dict) -> bool:
    """A chapter is safe to replay/show only once it's been reached -- completed, or the current one. Never a locked future one."""
    completed_ids = set(character["completed_quests"])
    arc_quests = set(arc.get("quests", []))
    is_completed = bool(arc_quests) and arc_quests.issubset(completed_ids)
    current = _current_story_arc(character)
    is_current = current is not None and current[0] == arc_id
    return is_completed or is_current


def _story_chapter_keyboard(character: dict) -> InlineKeyboardMarkup | None:
    """
    Per Coffee (2026-07-21): a real tap-to-replay menu of chapters on
    the Story So Far screen -- one button per chapter this character
    has actually reached (completed or current), same fog-of-war rule
    _do_show_story_so_far's own "???" listing already uses, so a locked
    future chapter's title is never exposed via a button either.
    """
    buttons = []
    for arc_id, arc in CAMPAIGN.get("story_arcs", {}).items():
        if _arc_is_reached(character, arc_id, arc):
            buttons.append([InlineKeyboardButton(f"🎬 {arc['title']}", callback_data=f"story|replay|{arc_id}")])
    return InlineKeyboardMarkup(buttons) if buttons else None


async def _do_replay_chapter_intro(update: Update, arc_id: str | None = None) -> None:
    """
    /replay_intro, per Coffee (2026-07-20): the arc-opening cutscene
    above fires automatically the moment a character takes on a new
    chapter's first quest -- often right at the start of a fresh
    character, easy to miss in the scroll. This re-narrates a chapter's
    opening on demand (same real narrate_arc_opening call, grounded in
    the same real arc/quest facts) without touching any quest/inventory
    state -- purely a rewatch, safe to call any number of times.
    arc_id is None for the plain /replay_intro command (always the
    CURRENT chapter); the Story So Far chapter-button menu (task,
    2026-07-21) passes a specific arc_id instead, re-validated here via
    _arc_is_reached so a tampered callback can never replay/spoil a
    locked future chapter.
    """
    character = db.get_character(update.effective_user.id)
    if character is None:
        await update.effective_chat.send_message(
            "You don't have a character yet!", message_thread_id=config.TOPIC_ADVENTURE_ID
        )
        return
    if arc_id is None:
        current = _current_story_arc(character)
        if current is None:
            await update.effective_chat.send_message(
                "You've already completed every chapter — there's no current one to replay.",
                message_thread_id=config.TOPIC_ADVENTURE_ID,
            )
            return
        arc_id, arc = current
    else:
        arc = CAMPAIGN.get("story_arcs", {}).get(arc_id)
        if arc is None or not _arc_is_reached(character, arc_id, arc):
            await update.effective_chat.send_message(
                "That chapter hasn't been reached yet.", message_thread_id=config.TOPIC_ADVENTURE_ID
            )
            return
    arc_quests = arc.get("quests", [])
    first_quest = CAMPAIGN["quests"].get(arc_quests[0]) if arc_quests else None
    quest_title = first_quest["title"] if first_quest else arc["title"]
    opening_text = await asyncio.to_thread(narrate_arc_opening, arc["title"], arc["description"], quest_title)
    await _safe_send(update, f"🎬 **{arc['title']}**\n{opening_text}")


# Full-storyline plan, Phase 2/3: a companion's resolution quest writes
# a real, persistent state via db.resolve_companion the moment it
# completes -- see _complete_quest_and_announce below. Two shapes:
# a flat resolution ({"npc_id", "resolution", "note"}) for a companion
# whose arc only ever has one real outcome (Grask's is a single
# "chooses loyalty at this exact gate" beat, not a variable one), or a
# trust-banded one ({"npc_id", "banded": {"low"/"mid"/"high": (state,
# note)}}) reading the real npc_relationships.affinity column at
# completion time -- band cutoffs match the narrative-craft memo's:
# low <= -20, mid -19..39, high >= 40.
QUEST_COMPANION_RESOLUTIONS = {
    "grasks_resolution": {
        "npc_id": "grask_emberscale", "resolution": "resolved_loyal",
        "note": "has made their choice",
    },
    "veshs_resolution": {
        "npc_id": "vesh_nightglass",
        "banded": {
            "low": ("resolved_estranged", "flinches at the crystal-glow and says as little as possible until you're both back out"),
            "mid": ("resolved_distant", "grits through it, quiet and tense, until you're both back out"),
            "high": ("resolved_loyal", "grips your arm the whole way through and doesn't let go until you're both back out"),
        },
    },
    "seras_resolution": {
        "npc_id": "sera_wanderer",
        "banded": {
            "low": ("resolved_estranged", "says the road's still calling and she's not ready to stop listening to it"),
            "mid": ("resolved_distant", "shrugs and says the group's as good a reason as any to stay a while longer"),
            "high": ("resolved_loyal", "stops checking the far road out of town and finally looks at the party like she's already found what she was looking for"),
        },
    },
    "borins_resolution": {
        "npc_id": "borin_ironjaw",
        "banded": {
            "low": ("resolved_estranged", "says that wasn't enough to prove anything and goes right back to standing his post"),
            "mid": ("resolved_distant", "grudgingly allows it might be enough, but keeps watching the street out of habit anyway"),
            "high": ("resolved_loyal", "finally steps off the street he's stood on for years and says his penance is paid"),
        },
    },
    "wrens_resolution": {
        "npc_id": "wren_hollowbrook",
        "banded": {
            "low": ("resolved_estranged", "says some things aren't the party's to know and goes back to talking to the garden instead"),
            "mid": ("resolved_distant", "lets the party stay close to the shrine, but still won't say what its roots are really drawing on"),
            "high": ("resolved_loyal", "finally says out loud what she's spent this whole time protecting"),
        },
    },
    "pips_resolution": {
        "npc_id": "pip_thistledown",
        "banded": {
            "low": ("resolved_estranged", "admits the party's story wasn't the one he was waiting for, and keeps watching the road for a better one"),
            "mid": ("resolved_distant", "calls it good enough for now and keeps traveling, though he's still humming about what's next"),
            "high": ("resolved_loyal", "finally stops 'just about to cross' and admits the party's story is the one he's been waiting to be part of"),
        },
    },
}


def _companion_trust_band(telegram_user_id: int, npc_id: str) -> str:
    affinity = db.get_relationship(telegram_user_id, npc_id)["affinity"]
    if affinity >= 40:
        return "high"
    if affinity <= -20:
        return "low"
    return "mid"


async def _complete_quest_and_announce(update_like, telegram_user_id: int, quest_id: str) -> None:
    quest = CAMPAIGN["quests"][quest_id]
    reward_xp = quest.get("reward_xp", 0)
    reward_gold = quest.get("reward_gold", 0)
    reward_item = quest.get("reward_item")

    character = db.get_character(telegram_user_id)
    _log_world_event(
        character.get("current_location") if character else None,
        f"{character['name']} completed the quest \"{quest['title']}\"." if character else None,
    )

    db.complete_quest(telegram_user_id, quest_id)

    resolution_note = ""
    companion_resolution = QUEST_COMPANION_RESOLUTIONS.get(quest_id)
    if companion_resolution:
        npc_id = companion_resolution["npc_id"]
        if "banded" in companion_resolution:
            band = _companion_trust_band(telegram_user_id, npc_id)
            resolution_state, note_text = companion_resolution["banded"][band]
        else:
            resolution_state = companion_resolution["resolution"]
            note_text = companion_resolution.get("note", "has made their choice")
        db.resolve_companion(telegram_user_id, npc_id, resolution_state)
        companion_npc = CAMPAIGN["npcs"].get(npc_id)
        companion_name = companion_npc["name"] if companion_npc else npc_id
        resolution_note = f"\n\n🤝 **{companion_name}** {note_text}."

    if reward_xp:
        await _award_xp_and_announce_level_up(update_like, telegram_user_id, reward_xp)
    if reward_gold:
        character = db.get_character(telegram_user_id)
        db.update_character(telegram_user_id, gold=character["gold"] + reward_gold)
    if reward_item:
        db.add_item(telegram_user_id, reward_item, 1)

    reward_parts = []
    if reward_xp:
        reward_parts.append(f"{reward_xp} XP")
    if reward_gold:
        reward_parts.append(f"{reward_gold} gold")
    if reward_item:
        reward_parts.append(items_module.get_item(reward_item)["name"])
    reward_text = ", ".join(reward_parts) or "real progress, if nothing material"

    chapter_note = _chapter_complete_note(telegram_user_id, quest_id)

    # Full-storyline plan, Phase 1: quests tagged "weight": "climactic" get
    # a real AI-narrated flourish, at a deeper story_mode pass, ahead of
    # the deterministic reward line -- everything narrated is already-
    # decided fact (the quest's own title/description/reward), never
    # invented by the model.
    climax_narration = ""
    if quest.get("weight") == "climactic":
        climax_text = await asyncio.to_thread(
            narrate_chapter_climax, quest["title"], quest["description"], reward_text,
        )
        climax_narration = f"{climax_text}\n\n"

    await _safe_send(
        update_like,
        f"📜 **Quest complete: {quest['title']}!**\n{climax_narration}You've earned: {reward_text}.{chapter_note}{resolution_note}",
    )
    # Task #172 gap (per Coffee, 2026-07-19): quest ACCEPT already
    # notifies Main (see _do_accept_quest's own _notify_main_topic
    # calls), but quest COMPLETION never did -- the exact same class of
    # "party members not watching Adventure miss the big moments" task
    # #172 was meant to close, just missed at this specific site.
    await _notify_main_topic(update_like, f"📜 **{character['name']}** completed a quest: {quest['title']}!")
    if chapter_note:
        # A finished STORY ARC is a bigger milestone than any one quest --
        # per Coffee (2026-07-19): "when players complete a part of the
        # story create a notification for it in main." Its own distinct,
        # more prominent post, separate from the routine quest-complete
        # line above, using the same real arc title/description
        # _chapter_complete_note already computed (never re-derived here).
        arc_info = _story_arc_for_quest(quest_id)
        if arc_info:
            _, arc = arc_info
            await _notify_main_topic(
                update_like, f"🌟 **{character['name']}** completed a chapter: \"{arc['title']}\"!",
            )


async def _check_quest_completions_reach_location(update_like, telegram_user_id: int, location_id: str) -> None:
    character = db.get_character(telegram_user_id)
    if character is None:
        return
    for quest_id in list(character["active_quests"].keys()):
        quest = CAMPAIGN["quests"].get(quest_id)
        if not quest:
            continue
        trigger = quest.get("trigger", {})
        if trigger.get("type") == "reach_location" and trigger.get("location") == location_id:
            await _complete_quest_and_announce(update_like, telegram_user_id, quest_id)
    await _check_and_award_achievements(update_like, db.get_character(telegram_user_id))


async def _check_board_quest_turnin(update_like, telegram_user_id: int, location_id: str) -> None:
    """
    Grants a completed-but-not-yet-collected board quest's reward when
    the player arrives back at the location where it's posted -- the
    counterpart to _do_gather deferring the reward instead of granting
    it the instant the objective is met (see that function's comment).
    Deliberately skips branch_data quests: those are resolved through
    the separate _do_resolve_quest_choice flow, not a location arrival.
    """
    # Player-scoped, not day_key-scoped (task #149, 2026-07-17) -- a
    # quest accepted on a previous calendar day but still inside its
    # real 24h expires_at window must still be turn-in-able.
    for board_quest in db.get_accepted_board_quests_for_user(telegram_user_id):
        if not (board_quest["location_id"] == location_id
                and not board_quest.get("branch_data")
                and board_quest["progress_count"] >= board_quest["objective_count"]):
            continue
        db.complete_board_quest(board_quest["board_quest_id"])
        if board_quest["objective_type"] == "gather_material":
            # Real bug, Coffee (2026-07-19): "completion of my quests
            # arent taking the silverleaf herbs?" -- _do_gather already
            # adds the gathered material to the player's own backpack as
            # a real, keepable item (db.add_item), so turning in a
            # gather quest never consumed it -- the herbs just stayed in
            # inventory forever after being "delivered."
            db.remove_item(telegram_user_id, board_quest["objective_target"], board_quest["objective_count"])
        await _award_xp_and_announce_level_up(update_like, telegram_user_id, board_quest["reward_xp"])
        character = db.get_character(telegram_user_id)
        db.update_character(telegram_user_id, gold=character["gold"] + board_quest["reward_gold"])
        db.increment_board_quests_completed(telegram_user_id)
        await _safe_send(
            update_like,
            f"📜 **Board quest complete: {board_quest['title']}!** "
            f"You earn {board_quest['reward_xp']} XP, {board_quest['reward_gold']} gold.",
        )
        # Task #172 gap, same fix as _complete_quest_and_announce above.
        await _notify_main_topic(
            update_like, f"📜 **{character['name']}** completed a quest: {board_quest['title']}!",
        )
        await _check_and_award_achievements(update_like, db.get_character(telegram_user_id))


async def _check_quest_completions_defeat_monster(update_like, session: sessions.Session) -> None:
    """
    Called only when the party won — checks every defeated enemy's
    monster_key against every party member's active quests, including
    AI-controlled ones (recruited companions and the autonomous AI
    party alike). Previously excluded anyone with is_ai=1, from back
    when only recruited companions carried that flag and holding a
    quest for one made little sense — but per CLAUDE.md's design
    philosophy (2026-07-10), an AI-driven party member plays under
    exactly the same rules as a human one, including finishing quests
    it personally accepted. A summoned combat participant (no real
    character row) still naturally falls out below via the
    `character is None` check, so this doesn't need its own filter.
    """
    defeated_monster_keys = {
        p.get("monster_key") for p in session.participants
        if session.sides.get(p["telegram_user_id"]) == "enemy" and p.get("monster_key")
    }
    if not defeated_monster_keys:
        return
    party_ids = [pid for pid in session.turn_order if session.sides.get(pid) == "party"]
    for telegram_user_id in party_ids:
        character = db.get_character(telegram_user_id)
        if character is None:
            continue
        for quest_id in list(character["active_quests"].keys()):
            quest = CAMPAIGN["quests"].get(quest_id)
            if not quest:
                continue
            trigger = quest.get("trigger", {})
            if trigger.get("type") == "defeat_monster" and trigger.get("monster") in defeated_monster_keys:
                await _complete_quest_and_announce(update_like, telegram_user_id, quest_id)


async def _do_accept_quest(update: Update, text: str = "") -> None:
    telegram_user_id = update.effective_user.id
    character = db.get_character(telegram_user_id)
    if character is None:
        await update.effective_chat.send_message(
            "You don't have a character yet!", message_thread_id=config.TOPIC_ADVENTURE_ID
        )
        return

    location_id = character["current_location"]

    # Checked BEFORE the location-based offer below: a companion's own
    # personal quest is a real, specific thing the party just agreed to
    # help with (see _offerable_companion_quest) -- it should win over
    # an unrelated location-based quest that happens to also be posted
    # wherever the player's currently standing, for a GENERIC "I accept
    # the quest" with nothing specific named.
    #
    # Confirmed live 2026-07-14 (Coffee): this shortcut was
    # unconditional, so "Accept the quest, a quiet request for wood" --
    # a SPECIFIC, correctly-classified accept_quest naming a real
    # different (board) quest by title -- got silently swallowed into
    # accepting the companion quest instead, regardless of what was
    # actually typed. Now only takes the shortcut when the text doesn't
    # clearly name something else that's actually available here.
    companion_offer = _offerable_companion_quest(character)
    if companion_offer is not None:
        quest_id, quest = companion_offer
        location_offer_for_check = _offerable_quest_at_location(character, location_id)
        board_quests_here = board_quests_module.get_or_generate_board_quests(CAMPAIGN, location_id)
        available_board = [
            q for q in board_quests_here if not q.get("accepted_by") and not q.get("completed_at")
        ]
        names_something_else = (
            (location_offer_for_check is not None and location_offer_for_check[1]["title"].lower() in text.lower())
            or any(q["title"].lower() in text.lower() for q in available_board)
        )
        if not names_something_else:
            opening_note = await _arc_opening_note(character, quest_id, quest)
            db.accept_quest(telegram_user_id, quest_id)
            await _safe_send(update, f"{opening_note}📜 **{character['name']}** accepts Quest: {quest['title']}\n{quest['description']}")
            await _notify_main_topic(update, f"📜 **{character['name']}** accepted a quest: {quest['title']}")
            return

    # No story quest on offer here — try the area's board quest(s) instead.
    all_quests = board_quests_module.get_or_generate_board_quests(CAMPAIGN, location_id)
    available = [q for q in all_quests if not q.get("accepted_by") and not q.get("completed_at")]

    # Defensive fix (2026-07-18): this location-based story-quest
    # shortcut used to be unconditional, the same class of mistake
    # already fixed above for companion_offer on 2026-07-14 -- a
    # location-tied story quest could otherwise win over a real, clearly
    # named board quest posted at the same spot. (Confirmed this build's
    # current campaign has no story quest actually tied to Whispering
    # Wood, so this specific guard wasn't the cause of the Goblins/Wolves
    # mixup below -- but the same shortcut-shadowing shape is real and
    # worth closing here too, same as companion_offer already was.)
    offer = _offerable_quest_at_location(character, location_id)
    if offer is not None:
        quest_id, quest = offer
        names_something_else = any(q["title"].lower() in text.lower() for q in available)
        if not names_something_else:
            opening_note = await _arc_opening_note(character, quest_id, quest)
            db.accept_quest(telegram_user_id, quest_id)
            await _safe_send(update, f"{opening_note}📜 **{character['name']}** accepts Quest: {quest['title']}\n{quest['description']}")
            await _notify_main_topic(update, f"📜 **{character['name']}** accepted a quest: {quest['title']}")
            return
    if not available:
        if all_quests:
            await update.effective_chat.send_message(
                "Everything on the board here has already been taken on for today.",
                message_thread_id=config.TOPIC_ADVENTURE_ID,
            )
        else:
            await update.effective_chat.send_message(
                "There's nothing to take on here right now.", message_thread_id=config.TOPIC_ADVENTURE_ID
            )
        return

    board_quest = board_quests_module.find_board_quest_by_name(available, text) if text else None
    if board_quest is None:
        # Real live bug (2026-07-18, confirmed via the live DB after
        # Coffee flagged it): find_board_quest_by_name only ever searches
        # `available` (not-yet-taken) quests, so a player naming a REAL
        # quest that simply got claimed by someone else moments earlier
        # correctly finds no match here -- but the code then fell straight
        # to "if len(available) == 1: use that one," silently enrolling
        # the player in whatever ELSE was still open instead of telling
        # them their named quest was unavailable. Confirmed live: Coffee
        # accepted "Clear out the Wolves" at 12:33:59; Sugar said the
        # exact same "I accept the quest to clear out the wolves" 50
        # seconds later and got silently signed up for "A Quiet Word
        # About the Goblins" instead, the only other quest left on the
        # board. Now checks ALL of today's board quests (not just the
        # still-open ones) for a real name match first, so an already-
        # taken/completed quest gets its own honest, specific reply
        # instead of a wrong substitution.
        named_taken_quest = board_quests_module.find_board_quest_by_name(all_quests, text) if text else None
        if named_taken_quest is not None and named_taken_quest not in available:
            if named_taken_quest.get("completed_at"):
                await update.effective_chat.send_message(
                    f"\"{named_taken_quest['title']}\" has already been completed.",
                    message_thread_id=config.TOPIC_ADVENTURE_ID,
                )
            else:
                await update.effective_chat.send_message(
                    f"\"{named_taken_quest['title']}\" is already taken by someone else.",
                    message_thread_id=config.TOPIC_ADVENTURE_ID,
                )
            return
        if len(available) == 1:
            board_quest = available[0]
        else:
            titles = ", ".join(f'"{q["title"]}"' for q in available)
            await update.effective_chat.send_message(
                f"There's more than one thing posted here — which one? {titles}",
                message_thread_id=config.TOPIC_ADVENTURE_ID,
            )
            return

    db.accept_board_quest(board_quest["board_quest_id"], telegram_user_id)

    # A player who already holds the target material shouldn't have to
    # gather it again from scratch -- credit whatever they already have
    # toward the objective right away, same as a fresh gather would.
    if board_quest["objective_type"] == "gather_material":
        held = character.get("inventory", {}).get(board_quest["objective_target"], 0)
        already_credited = board_quest["progress_count"]
        credit = min(held, board_quest["objective_count"]) - already_credited
        if credit > 0:
            db.record_board_quest_progress(board_quest["board_quest_id"], credit)

    if board_quest.get("branch_data"):
        await _safe_send(
            update,
            f"📜 **{character['name']}** accepts Quest: {board_quest['title']}\n"
            f"{board_quest['branch_data']['setup_narration']}\n\n"
            f"What you earn depends on the choice you make once it's done. Expires in 24h if not finished.",
        )
        await _notify_main_topic(update, f"📜 **{character['name']}** accepted a quest: {board_quest['title']}")
        return
    await _safe_send(
        update,
        f"📜 **{character['name']}** accepts Quest: {board_quest['title']}\n{board_quest['description']}\n"
        f"Reward: {board_quest['reward_xp']} XP, {board_quest['reward_gold']} gold. "
        f"Expires in 24h if not finished.",
    )
    await _notify_main_topic(update, f"📜 **{character['name']}** accepted a quest: {board_quest['title']}")
    # Same-location counterpart to the arrival-triggered check in
    # _do_move/_do_fast_travel -- if the retroactive credit above (or an
    # objective_count of 0) already finished it, the player is standing
    # right here and would otherwise have to leave and come back to
    # collect a reward they already qualify for.
    await _check_board_quest_turnin(update, telegram_user_id, location_id)


async def _do_resolve_quest_choice(update: Update, text: str) -> None:
    """
    Finalizes a branching board quest once its objective is complete —
    the player names which of the two fixed, pre-defined choices they
    want, and that choice's real reward/faction consequence (decided
    in board_quests.py, never by the AI) is applied. The AI only
    narrates the resolution of a choice that's already been made.
    """
    telegram_user_id = update.effective_user.id
    character = db.get_character(telegram_user_id)
    if character is None:
        await update.effective_chat.send_message(
            "You don't have a character yet!", message_thread_id=config.TOPIC_ADVENTURE_ID
        )
        return

    ready = [
        q for q in db.get_accepted_board_quests_for_user(telegram_user_id)
        if q.get("branch_data") and q["progress_count"] >= q["objective_count"]
    ]
    if not ready:
        await update.effective_chat.send_message(
            "You don't have a decision to make right now.", message_thread_id=config.TOPIC_ADVENTURE_ID
        )
        return

    quest = ready[0]
    branch = quest["branch_data"]
    lowered = text.lower()
    choice_key = next((k for k, c in branch["choices"].items() if c["label"].lower() in lowered), None)
    if choice_key is None:
        labels = "\n".join(f'  • "{c["label"]}"' for c in branch["choices"].values())
        await update.effective_chat.send_message(
            f"Which way do you want to go on **{quest['title']}**?\n{labels}",
            message_thread_id=config.TOPIC_ADVENTURE_ID,
        )
        return

    chosen = branch["choices"][choice_key]
    db.resolve_board_quest_branch(quest["board_quest_id"], choice_key)
    # Same real bug as _check_board_quest_turnin's plain gather quests --
    # but here it's asymmetric by design: "keep it" means keeping the
    # gathered material (never remove it), while any other resolution
    # (e.g. "leave it be instead") means giving it up.
    if quest["objective_type"] == "gather_material" and choice_key != "keep_it":
        db.remove_item(telegram_user_id, quest["objective_target"], quest["objective_count"])
    db.increment_board_quests_completed(telegram_user_id)
    await _award_xp_and_announce_level_up(update, telegram_user_id, chosen["reward_xp"])
    fresh = db.get_character(telegram_user_id)
    db.update_character(telegram_user_id, gold=fresh["gold"] + chosen["reward_gold"])
    if chosen.get("faction_id") and chosen.get("faction_delta"):
        _adjust_faction_standing(telegram_user_id, chosen["faction_id"], chosen["faction_delta"])

    location = cl.get_location(CAMPAIGN, quest["location_id"])
    location_name = location["name"] if location else quest["location_id"]
    outcome_narration = await asyncio.to_thread(
        narrate_branching_choice_outcome, location_name, chosen["label"], chosen["outcome_facts"]
    )
    await _safe_send(
        update,
        f"📜 **{quest['title']} — resolved**\n{outcome_narration}\n\n"
        f"You gain {chosen['reward_xp']} XP, {chosen['reward_gold']} gold.",
    )


def _format_story_quest_poster(quest: dict, location_id: str) -> str:
    """
    A real "wanted poster" style display for an offerable story quest --
    title, description, reward, and who's actually offering it. Real
    2026-07-11 request: checking the quest board only ever showed the
    title and description, no reward, no attribution of whose quest it
    even was, unlike a board-quest listing which already shows both.

    Story quests have no structured NPC-giver field in campaign.json
    (only a "location"), so the giver shown here is whichever real NPC
    is physically at that location -- the same approximation already
    used by _npc_quest_facts for NPC dialogue grounding, so this poster
    and what an NPC will actually tell you about their own quest agree
    with each other.
    """
    reward_bits = [f"{quest['reward_xp']} XP"] if quest.get("reward_xp") else []
    if quest.get("reward_gold"):
        reward_bits.append(f"{quest['reward_gold']} gold")
    if quest.get("reward_item"):
        reward_bits.append(items_module.get_item(quest["reward_item"])["name"])
    reward_line = f"\n💰 **Reward:** {', '.join(reward_bits)}" if reward_bits else ""

    giver_ids = _npcs_at_location(location_id)
    giver_names = [cl.get_npc(CAMPAIGN, n)["name"] for n in giver_ids if cl.get_npc(CAMPAIGN, n)]
    location = cl.get_location(CAMPAIGN, location_id)
    location_name = location["name"] if location else location_id
    if giver_names:
        giver_line = f"\n📍 **Posted by:** {', '.join(giver_names)} (at {location_name})"
        ask_line = f"\n(Ask {giver_names[0]} for more details.)"
    else:
        giver_line = f"\n📍 **Posted at:** {location_name}"
        ask_line = ""

    return (
        f"📜 **WANTED: {quest['title']}**\n"
        f"{quest['description']}"
        f"{reward_line}"
        f"{giver_line}"
        f"{ask_line}"
    )


def _quest_board_keyboard(
    story_offer: tuple[str, dict] | None, area_board_quests: list[dict],
) -> InlineKeyboardMarkup | None:
    """
    Task #176: one "Accept" button per real quest actually postable here
    right now -- the same story_offer/area_board_quests _do_check_quests
    already computed from real campaign/board data just above. Tapping
    dispatches through the SAME _do_accept_quest handler the existing
    "say which choice you want" free-text flow uses (naming the quest's
    own title, exactly as a player would type it), never duplicated
    accept logic. Story quests use their fixed campaign quest_id; board
    quests use their own board_quest_id -- both are short, stable, and
    safe as callback_data (never the title text itself, which can be
    long/punctuated).
    """
    buttons = []
    if story_offer is not None:
        quest_id, quest = story_offer
        buttons.append([InlineKeyboardButton(f"📜 Accept: {quest['title']}", callback_data=f"quest|accept|story|{quest_id}")])
    for bq in area_board_quests:
        if bq.get("accepted_by") or bq.get("completed_at"):
            continue
        buttons.append([InlineKeyboardButton(
            f"📋 Accept: {bq['title']}", callback_data=f"quest|accept|board|{bq['board_quest_id']}",
        )])
    return InlineKeyboardMarkup(buttons) if buttons else None


async def quest_menu_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handles taps on _quest_board_keyboard -- see its docstring."""
    query = update.callback_query
    parts = (query.data or "").split("|")
    kind = parts[2] if len(parts) > 2 else None
    ident = parts[3] if len(parts) > 3 else None
    await _safe_answer(query)

    title = None
    if kind == "story":
        quest = CAMPAIGN["quests"].get(ident)
        title = quest["title"] if quest else None
    elif kind == "board":
        character = db.get_character(update.effective_user.id)
        if character is not None:
            location_id = character["current_location"]
            for bq in board_quests_module.get_or_generate_board_quests(CAMPAIGN, location_id):
                if str(bq["board_quest_id"]) == ident:
                    title = bq["title"]
                    break
    if title is None:
        return
    await _do_accept_quest(update, title)


async def _do_check_quests(update: Update) -> None:
    """
    Shows two distinct things, per Coffee's explicit terminology
    (2026-07-10): the character's personal Quest Journal (story quests
    from campaign.json, tracked per-character in active_quests/
    completed_quests) AND the current location's area Quest Board (a
    repeatable, generated bounty anyone there can accept — see
    board_quests.py). These are never the same list.
    """
    character = db.get_character(update.effective_user.id)
    if character is None:
        await update.effective_chat.send_message(
            "You don't have a character yet!", message_thread_id=config.TOPIC_ADVENTURE_ID
        )
        return

    lines = ["📖 **Quest journal**"]
    current_arc = _current_story_arc(character)
    if current_arc:
        _, arc = current_arc
        lines.append(f"\n**Chapter: \"{arc['title']}\"** — {arc['description']}")
    elif CAMPAIGN.get("story_arcs"):
        lines.append("\n**The story is complete.** Every chapter's quests are behind you.")

    if character["active_quests"]:
        lines.append("\n**Active:**")
        for quest_id in character["active_quests"]:
            quest = CAMPAIGN["quests"].get(quest_id)
            if quest:
                lines.append(f"• {quest['title']} — {quest['description']}")
    else:
        lines.append("\nNo active story quests.")

    if character["completed_quests"]:
        titles = [CAMPAIGN["quests"][q]["title"] for q in character["completed_quests"] if q in CAMPAIGN["quests"]]
        lines.append(f"\n**Completed ({len(titles)}):** {', '.join(titles)}")

    # Board quests are randomly generated (not worth naming individually
    # the way story quests are) and track their own completion entirely
    # separately from completed_quests above -- this running count is
    # the only place that history was ever surfaced to the player at all
    # before 2026-07-16 (per Coffee).
    if character.get("board_quests_completed"):
        lines.append(f"\n**Board quests completed:** {character['board_quests_completed']}")

    # Confirmed live 2026-07-14 (Coffee): accepted a board quest, then
    # asked to look at his active quests -- it never showed up. This
    # journal previously only ever looked at character["active_quests"]
    # (story quests), never board_quests_module/db's separate tracking
    # for a player's own accepted board quests (accepted_by, keyed by
    # board_quest_id, not tied to active_quests at all). The real data
    # already existed (db.get_accepted_board_quests_for_user, already
    # used by _do_resolve_quest_choice) -- it just wasn't being shown
    # here, the one place a player would naturally look for it.
    accepted_board_quests = db.get_accepted_board_quests_for_user(update.effective_user.id)
    if accepted_board_quests:
        lines.append("\n**Board quests accepted:**")
        for bq in accepted_board_quests:
            bq_location = cl.get_location(CAMPAIGN, bq["location_id"])
            bq_location_name = bq_location["name"] if bq_location else bq["location_id"]
            if bq.get("branch_data") and bq["progress_count"] >= bq["objective_count"]:
                lines.append(f"• {bq['title']} ({bq_location_name}) — ready to decide, say which choice you want")
            else:
                # Task #150, 2026-07-17: this line used to be the ONLY
                # place a player saw an accepted board quest, and it
                # never showed what the quest actually asked for --
                # just a title and a bare progress fraction. Showing
                # the description here is exactly what "what do I need
                # to do for this quest" needs.
                progress = f"{bq['progress_count']}/{bq['objective_count']}"
                lines.append(f"• {bq['title']} ({bq_location_name}) — {progress}\n  {bq['description']}")

    location_id = character["current_location"]
    location = cl.get_location(CAMPAIGN, location_id)
    story_offer = _offerable_quest_at_location(character, location_id)
    area_board_quests = board_quests_module.get_or_generate_board_quests(CAMPAIGN, location_id)
    lines.append(f"\n📋 **Quest board — {location['name'] if location else location_id}**")
    if not story_offer and not area_board_quests:
        lines.append("Nothing posted here today.")
    if story_offer:
        _, quest = story_offer
        lines.append(_format_story_quest_poster(quest, location_id))
    if area_board_quests:
        lines.append(board_quests_module.format_board_listings(area_board_quests))
    if story_offer or any(not q.get("accepted_by") and not q.get("completed_at") for q in area_board_quests):
        lines.append("(Say \"I accept this quest\" — name it if more than one's posted — or tap a button below.)")

    await _safe_send(
        update, "\n".join(lines),
        reply_markup=_with_menu_button(_quest_board_keyboard(story_offer, area_board_quests)),
        speak=False,
    )


async def _do_ask_clue(update: Update) -> None:
    character = db.get_character(update.effective_user.id)
    if character is None:
        await update.effective_chat.send_message(
            "You don't have a character yet!", message_thread_id=config.TOPIC_ADVENTURE_ID
        )
        return

    if not character["active_quests"]:
        await update.effective_chat.send_message(
            "You don't have any active quests to find clues for.", message_thread_id=config.TOPIC_ADVENTURE_ID
        )
        return

    lines = ["🔍 **What you know:**"]
    for quest_id in character["active_quests"]:
        quest = CAMPAIGN["quests"].get(quest_id)
        if quest and quest.get("clue"):
            lines.append(f"• *{quest['title']}*: {quest['clue']}")
    if len(lines) == 1:
        lines.append("Nothing concrete yet — keep exploring.")

    await _safe_send(update, "\n".join(lines))


async def _do_answer_puzzle(update: Update, text: str) -> None:
    telegram_user_id = update.effective_user.id
    character = db.get_character(telegram_user_id)
    if character is None:
        await update.effective_chat.send_message(
            "You don't have a character yet!", message_thread_id=config.TOPIC_ADVENTURE_ID
        )
        return

    lowered = text.lower()
    for quest_id in list(character["active_quests"].keys()):
        quest = CAMPAIGN["quests"].get(quest_id)
        if not quest or quest.get("trigger", {}).get("type") != "solve_puzzle":
            continue
        puzzle_id = quest["trigger"]["puzzle_id"]
        puzzle = CAMPAIGN.get("puzzles", {}).get(puzzle_id)
        if not puzzle:
            continue
        if any(answer in lowered for answer in puzzle["accepted_answers"]):
            await _complete_quest_and_announce(update, telegram_user_id, quest_id)
            return

    await update.effective_chat.send_message(
        "That's not it — think it over some more.", message_thread_id=config.TOPIC_ADVENTURE_ID
    )


# Task #79, per Coffee: a real player-run marketplace, NOT an auction
# house -- one fixed price per listing, first buyer to say so takes it
# whole, never a bid. Global (not location-scoped) for this first
# version -- reachable from anywhere via /sell_market, /market,
# /buy_market, same as any other command.
async def _do_sell_market(update: Update, args: list[str]) -> None:
    character = db.get_character(update.effective_user.id)
    if character is None:
        await update.effective_chat.send_message(
            "You don't have a character yet!", message_thread_id=config.TOPIC_ADVENTURE_ID
        )
        return
    if len(args) < 3:
        await update.effective_chat.send_message(
            "Usage: /sell_market <quantity> <price> <item name>", message_thread_id=config.TOPIC_ADVENTURE_ID
        )
        return
    try:
        quantity = int(args[0])
        price = int(args[1])
    except ValueError:
        await update.effective_chat.send_message(
            "Quantity and price both need to be real numbers.", message_thread_id=config.TOPIC_ADVENTURE_ID
        )
        return
    if quantity <= 0 or price <= 0:
        await update.effective_chat.send_message(
            "Quantity and price both need to be more than zero.", message_thread_id=config.TOPIC_ADVENTURE_ID
        )
        return
    item_text = " ".join(args[2:])
    item_id = items_module.find_item_mentioned_in_text(item_text, candidate_ids=list(character["inventory"].keys()))
    if item_id is None:
        await update.effective_chat.send_message(
            "You don't have that item to sell.", message_thread_id=config.TOPIC_ADVENTURE_ID
        )
        return
    held = character["inventory"].get(item_id, 0)
    if held < quantity:
        await update.effective_chat.send_message(
            f"You only have {held}.", message_thread_id=config.TOPIC_ADVENTURE_ID
        )
        return
    removed, _ = db.remove_item(update.effective_user.id, item_id, quantity)
    if not removed:
        return
    listing_id = db.create_market_listing(
        update.effective_user.id, character["name"], item_id, quantity, price
    )
    item_name = items_module.get_item(item_id)["name"]
    await _safe_send(
        update,
        f"🏷️ **{character['name']}** lists **{quantity}x {item_name}** for **{price} gold** "
        f"(listing #{listing_id}). Say \"/buy_market {listing_id}\" to buy it.",
    )


def _market_keyboard(listings: list[dict]) -> InlineKeyboardMarkup | None:
    """
    Per Coffee (2026-07-22): "make buttons on market and tell players
    how to use market" -- the player marketplace was slash-command-only
    with no buttons at all (unlike the NPC shop's _shop_keyboard),
    which cuts against this whole game's "no slash commands required"
    design. One Buy button per real listing, same reuse-the-existing-
    handler pattern as _shop_keyboard -- tapping dispatches through the
    SAME _do_buy_market a typed "/buy_market <#>" already uses.
    """
    buttons = []
    for listing in listings:
        item = items_module.get_item(listing["item_id"])
        item_name = item["name"] if item else listing["item_id"]
        buttons.append([InlineKeyboardButton(
            f"Buy {listing['quantity']}x {item_name} — {listing['price']}g",
            callback_data=f"market|buy|{listing['listing_id']}",
        )])
    return InlineKeyboardMarkup(buttons) if buttons else None


async def _do_check_market(update: Update) -> None:
    listings = db.get_market_listings()
    if not listings:
        await _safe_send(
            update,
            "The marketplace is empty right now — nobody's listed anything for sale.\n\n"
            "Want to sell something yourself? Say \"/sell_market <quantity> <price> <item name>\" "
            "(e.g. \"/sell_market 3 50 Silverleaf Herb\") to list it for other players to buy.",
            speak=False,
        )
        return
    lines = ["🏛️ **Player Marketplace:**"]
    for listing in listings:
        item = items_module.get_item(listing["item_id"])
        item_name = item["name"] if item else listing["item_id"]
        lines.append(
            f"#{listing['listing_id']}: {listing['quantity']}x {item_name} — {listing['price']} gold "
            f"(seller: {listing['seller_name']})"
        )
    lines.append(
        "\nTap a listing below to buy it, or say \"/buy_market <#>\".\n"
        "Selling something yourself? Say \"/sell_market <quantity> <price> <item name>\" "
        "(e.g. \"/sell_market 3 50 Silverleaf Herb\")."
    )
    await _safe_send(update, "\n".join(lines), reply_markup=_market_keyboard(listings), speak=False)


async def _do_buy_market(update: Update, args: list[str]) -> None:
    character = db.get_character(update.effective_user.id)
    if character is None:
        await update.effective_chat.send_message(
            "You don't have a character yet!", message_thread_id=config.TOPIC_ADVENTURE_ID
        )
        return
    if not args:
        await update.effective_chat.send_message(
            "Usage: /buy_market <listing #>", message_thread_id=config.TOPIC_ADVENTURE_ID
        )
        return
    try:
        listing_id = int(args[0].lstrip("#"))
    except ValueError:
        await update.effective_chat.send_message(
            "That's not a real listing number.", message_thread_id=config.TOPIC_ADVENTURE_ID
        )
        return
    listing = db.get_market_listing(listing_id)
    if listing is None:
        await update.effective_chat.send_message(
            "That listing doesn't exist — it may have already been bought.",
            message_thread_id=config.TOPIC_ADVENTURE_ID,
        )
        return
    if listing["seller_id"] == update.effective_user.id:
        await update.effective_chat.send_message(
            "You can't buy your own listing.", message_thread_id=config.TOPIC_ADVENTURE_ID
        )
        return
    if character["gold"] < listing["price"]:
        await update.effective_chat.send_message(
            f"You only have {character['gold']} gold — this costs {listing['price']}.",
            message_thread_id=config.TOPIC_ADVENTURE_ID,
        )
        return

    db.update_character(update.effective_user.id, gold=character["gold"] - listing["price"])
    db.add_item(update.effective_user.id, listing["item_id"], listing["quantity"])
    seller = db.get_character(listing["seller_id"])
    if seller:
        db.update_character(listing["seller_id"], gold=seller["gold"] + listing["price"])
    db.remove_market_listing(listing_id)

    item_name = items_module.get_item(listing["item_id"])["name"]
    await _safe_send(
        update,
        f"🏛️ **{character['name']}** buys **{listing['quantity']}x {item_name}** from "
        f"**{listing['seller_name']}** for **{listing['price']} gold**.",
    )


GAMBLE_WIN_THRESHOLD = 8  # 2d6 total needed to double your wager


async def _do_gamble(update: Update, text: str) -> None:
    character = db.get_character(update.effective_user.id)
    if character is None:
        await update.effective_chat.send_message(
            "You don't have a character yet!", message_thread_id=config.TOPIC_ADVENTURE_ID
        )
        return

    location = cl.get_location(CAMPAIGN, character["current_location"])
    if not location or not location.get("safe"):
        await update.effective_chat.send_message(
            "There's nowhere to gamble here — try somewhere like a tavern.",
            message_thread_id=config.TOPIC_ADVENTURE_ID,
        )
        return

    amount = None
    for token in text.split():
        digits = "".join(c for c in token if c.isdigit())
        if digits:
            amount = int(digits)
            break
    if not amount or amount <= 0:
        await update.effective_chat.send_message(
            "How much gold do you want to wager? Say an amount, e.g. 'I bet 20 gold'.",
            message_thread_id=config.TOPIC_ADVENTURE_ID,
        )
        return
    if amount > character["gold"]:
        await update.effective_chat.send_message(
            f"You only have {character['gold']} gold.", message_thread_id=config.TOPIC_ADVENTURE_ID
        )
        return

    dice_roll = roll(2, 6)
    total = sum(dice_roll)
    won = total >= GAMBLE_WIN_THRESHOLD
    new_gold = character["gold"] + amount if won else character["gold"] - amount
    db.update_character(update.effective_user.id, gold=new_gold)

    banner = "🎉 **You win!**" if won else "💸 **You lose.**"
    change = f"+{amount}" if won else f"-{amount}"
    await _safe_send(
        update,
        f"{banner} You roll {dice_roll[0]} + {dice_roll[1]} = **{total}** "
        f"(need {GAMBLE_WIN_THRESHOLD}+). Gold: {change} → **{new_gold}**.",
    )


# Task #143, per Coffee: "Class-flavored dice mini-games (7-dice set),
# everyone can play, all levelable." A free (no gold at risk), self-
# contained side activity distinct from _do_gamble above -- one themed
# die per class (covering all 6 real polyhedral dice), plus a universal
# d100 "Fortune's Wheel" anyone can play regardless of class. "Levelable"
# reuses the existing generic skill_uses bucket (db.record_skill_use)
# rather than a new schema column -- a real, persistent play count per
# character that a rank title is derived from.
CLASS_DICE_GAMES = {
    "Wizard": (4, "Arcane Draw"), "Sorcerer": (4, "Arcane Draw"),
    "Rogue": (6, "Sleight of Hand"),
    "Cleric": (8, "Blessing Roll"), "Druid": (8, "Blessing Roll"), "Bard": (8, "Blessing Roll"),
    "Ranger": (10, "Focused Shot"), "Monk": (10, "Focused Strike"),
    "Fighter": (12, "Clash of Arms"), "Barbarian": (12, "Clash of Arms"),
    "Paladin": (20, "Oath Gambit"), "Warlock": (20, "Oath Gambit"),
}


def _dice_game_rank(uses: int) -> str:
    if uses >= 15:
        return "Master"
    if uses >= 5:
        return "Adept"
    return "Novice"


async def _do_dice_game(update: Update) -> None:
    character = db.get_character(update.effective_user.id)
    if character is None:
        await update.effective_chat.send_message(
            "You don't have a character yet!", message_thread_id=config.TOPIC_ADVENTURE_ID
        )
        return
    sides, game_name = CLASS_DICE_GAMES.get(character["char_class"], (20, "High Roll"))
    result = roll(1, sides)[0]
    won = result > sides // 2
    xp_reward = 15 if won else 0
    if xp_reward:
        await _award_xp_and_announce_level_up(update, update.effective_user.id, xp_reward)
    uses = db.record_skill_use(update.effective_user.id, "dice_game")
    rank = _dice_game_rank(uses)
    banner = "🎲 **A win!**" if won else "🎲 No luck this time."
    reward_note = f" +{xp_reward} XP." if xp_reward else ""
    await _safe_send(
        update,
        f"{banner} **{character['name']}** plays {game_name} (d{sides}): rolled **{result}**.{reward_note}\n"
        f"{game_name} rank: {rank} ({uses} played)",
    )


async def _do_fortunes_wheel(update: Update) -> None:
    character = db.get_character(update.effective_user.id)
    if character is None:
        await update.effective_chat.send_message(
            "You don't have a character yet!", message_thread_id=config.TOPIC_ADVENTURE_ID
        )
        return
    result = roll(1, 100)[0]
    won = result >= 80
    xp_reward = 50 if won else 0
    gold_reward = 10 if won else 0
    if xp_reward:
        await _award_xp_and_announce_level_up(update, update.effective_user.id, xp_reward)
    if gold_reward:
        db.update_character(update.effective_user.id, gold=character["gold"] + gold_reward)
    uses = db.record_skill_use(update.effective_user.id, "fortunes_wheel")
    rank = _dice_game_rank(uses)
    banner = "🎡 **The wheel favors you!**" if won else "🎡 The wheel turns on, unmoved."
    reward_note = f" +{xp_reward} XP, +{gold_reward} gold." if won else ""
    await _safe_send(
        update,
        f"{banner} **{character['name']}** spins Fortune's Wheel (d100): rolled **{result}**.{reward_note}\n"
        f"Fortune's Wheel rank: {rank} ({uses} played)",
    )


_ALIGNMENT_PHRASES = {
    "lawful good": (50, 50), "neutral good": (0, 50), "chaotic good": (-50, 50),
    "lawful neutral": (50, 0), "true neutral": (0, 0), "chaotic neutral": (-50, 0),
    "lawful evil": (50, -50), "neutral evil": (0, -50), "chaotic evil": (-50, -50),
}


async def _do_set_alignment(update: Update, text: str) -> None:
    """
    Task #133, per Coffee: alignment "set at creation" -- rather than
    lengthening the (already real-tested) character-creation wizard
    with another step, every new character simply starts True Neutral
    (db default 0/0) and can declare a starting alignment any time via
    this, same "available anytime, not creation-locked" spirit as
    description/pronouns (task #96). Matched longest-phrase-first so
    "chaotic neutral" doesn't get swallowed by a bare "neutral" check.
    """
    character = db.get_character(update.effective_user.id)
    if character is None:
        await update.effective_chat.send_message(
            "You don't have a character yet!", message_thread_id=config.TOPIC_ADVENTURE_ID
        )
        return
    lowered = text.lower()
    for phrase in sorted(_ALIGNMENT_PHRASES, key=len, reverse=True):
        if phrase in lowered:
            law_chaos, good_evil = _ALIGNMENT_PHRASES[phrase]
            db.update_character(
                update.effective_user.id, alignment_law_chaos=law_chaos, alignment_good_evil=good_evil,
            )
            await _safe_send(update, f"⚖️ **{character['name']}**'s alignment is now **{phrase.title()}**.")
            return
    await update.effective_chat.send_message(
        "Which alignment? e.g. \"set my alignment to chaotic good\" — "
        "Lawful/Neutral/Chaotic x Good/Neutral/Evil, or \"true neutral\".",
        message_thread_id=config.TOPIC_ADVENTURE_ID,
    )


# Task #131, per Coffee: "Path-driven subclass selection via a
# skill-tree point system on level-up." Real 5E subclass CHOICE doesn't
# exist anywhere in this build (every class defaults to one fixed
# subclass -- Cleric is always Life Domain, Sorcerer always Draconic
# Bloodline, etc., see class_features.py's docstring) -- reversing that
# convention game-wide is a much bigger undertaking than one pass here.
# This ships the real, working first slice instead: 1 skill point per
# level gained (db.add_xp), spent on a single, real, class-flavored
# upgrade that deepens the class's EXISTING fixed subclass feature
# rather than switching to a different one. First shipped 2026-07-21
# for the 6 classes played live at the time (Fighter/Rogue/Warlock/
# Sorcerer/Cleric/Bard); the remaining 6 (Barbarian/Paladin/Wizard/
# Monk/Ranger/Druid) added the same day using the identical pattern.
SKILL_TREE_UPGRADES = {
    "Fighter": {"id": "hardened_resolve", "name": "Hardened Resolve", "cost": 2,
                "description": "Second Wind heals an extra 1d10."},
    "Rogue": {"id": "killers_instinct", "name": "Killer's Instinct", "cost": 3,
              "description": "Sneak Attack deals one extra d6."},
    "Warlock": {"id": "darker_bargain", "name": "Darker Bargain", "cost": 2,
                "description": "Dark One's Blessing grants 2 extra temporary HP."},
    "Sorcerer": {"id": "draconic_hide", "name": "Draconic Hide", "cost": 2,
                 "description": "Draconic Resilience's unarmored AC improves by 1."},
    "Cleric": {"id": "disciples_grace", "name": "Disciple's Grace", "cost": 2,
               "description": "Disciple of Life's healing bonus doubles."},
    "Bard": {"id": "greater_inspiration", "name": "Greater Inspiration", "cost": 2,
             "description": "Bardic Inspiration's die improves (d6 → d8)."},
    "Barbarian": {"id": "endless_fury", "name": "Endless Fury", "cost": 2,
                  "description": "One extra Rage use per rest."},
    "Paladin": {"id": "greater_mercy", "name": "Greater Mercy", "cost": 2,
                "description": "Lay on Hands' healing pool grows (5 → 6 per level)."},
    "Wizard": {"id": "deeper_recovery", "name": "Deeper Recovery", "cost": 2,
               "description": "Arcane Recovery restores one extra spell slot."},
    "Monk": {"id": "iron_will", "name": "Iron Will", "cost": 2,
             "description": "2 extra ki points per rest."},
    "Ranger": {"id": "true_aim", "name": "True Aim", "cost": 2,
               "description": "Favored Enemy advantage extends to wolves too."},
    "Druid": {"id": "primal_surge", "name": "Primal Surge", "cost": 2,
              "description": "Wild Shape grants 2 extra temporary HP."},
}


def _has_skill_upgrade(character: dict, upgrade_id: str) -> bool:
    return upgrade_id in (character.get("skill_tree_upgrades") or [])


def _skill_tree_keyboard(upgrade: dict) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([[
        InlineKeyboardButton(f"Unlock {upgrade['name']} ({upgrade['cost']} pts)", callback_data=f"skilltree|buy|{upgrade['id']}")
    ]])


async def _do_show_skill_tree(update: Update) -> None:
    character = db.get_character(update.effective_user.id)
    if character is None:
        await update.effective_chat.send_message(
            "You don't have a character yet!", message_thread_id=config.TOPIC_ADVENTURE_ID
        )
        return
    points = character.get("skill_points", 0)
    upgrade = SKILL_TREE_UPGRADES.get(character["char_class"])
    if upgrade is None:
        await _safe_send(
            update,
            f"🌳 **{character['name']}**'s Skill Tree — {points} point(s) banked. "
            f"No real upgrade exists for {character['char_class']} yet — more classes are coming.",
            speak=False,
        )
        return
    owned = _has_skill_upgrade(character, upgrade["id"])
    can_afford = points >= upgrade["cost"]
    # Real live bug (2026-07-22, Coffee: "when I opened it up in menu it
    # didn't have anything to tap and it didn't tell me how to level it
    # up" / "it won't let me distribute my point"): NOT a bug -- Fighter's
    # only real upgrade costs 2 points and Elduinn had 1 banked, so the
    # tap button was correctly withheld (see keyboard below) -- but the
    # old status line just said "Cost: 2 point(s)" with no indication
    # WHY nothing was tappable, indistinguishable from something broken.
    # Now says plainly how many more points are still needed.
    if owned:
        status = "✅ Unlocked"
    elif can_afford:
        status = f"Cost: {upgrade['cost']} point(s) — tap below to unlock"
    else:
        short_by = upgrade["cost"] - points
        status = f"Cost: {upgrade['cost']} point(s) — need {short_by} more (keep leveling up to bank more points)"
    text = (
        f"🌳 **{character['name']}**'s Skill Tree — {points} point(s) banked\n"
        f"**{upgrade['name']}** ({status})\n{upgrade['description']}"
    )
    keyboard = _skill_tree_keyboard(upgrade) if not owned and can_afford else None
    await _safe_send(update, text, reply_markup=keyboard, speak=False)


async def skilltree_menu_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handles taps on _skill_tree_keyboard -- re-validates points/ownership server-side, never trusts the button alone."""
    query = update.callback_query
    parts = (query.data or "").split("|")
    action = parts[1] if len(parts) > 1 else ""
    await _safe_answer(query)
    if action != "buy" or len(parts) < 3:
        return
    upgrade_id = parts[2]
    character = db.get_character(update.effective_user.id)
    if character is None:
        return
    upgrade = SKILL_TREE_UPGRADES.get(character["char_class"])
    if upgrade is None or upgrade["id"] != upgrade_id:
        return
    if _has_skill_upgrade(character, upgrade_id):
        return
    if character.get("skill_points", 0) < upgrade["cost"]:
        await update.effective_chat.send_message(
            "Not enough skill points yet.", message_thread_id=config.TOPIC_ADVENTURE_ID
        )
        return
    updates = {
        "skill_points": character["skill_points"] - upgrade["cost"],
        "skill_tree_upgrades": character["skill_tree_upgrades"] + [upgrade_id],
    }
    # Draconic Hide is the one upgrade here that touches armor_class --
    # a static, set-once-at-creation field in this build (never
    # recalculated from anything else, same convention as every other
    # AC tweak in this game), so unlike the other 5 upgrades (which are
    # read live at their own real call sites), this one applies its
    # bonus directly, once, right here at purchase time.
    if upgrade_id == "draconic_hide":
        updates["armor_class"] = character["armor_class"] + 1
    db.update_character(update.effective_user.id, **updates)
    await _safe_send(update, f"🌳 **{character['name']}** unlocks **{upgrade['name']}**!")
    await _notify_main_topic(update, f"🌳 **{character['name']}** unlocked a skill-tree upgrade: {upgrade['name']}!")


async def _do_check_party(update: Update, text: str = "") -> None:
    character = db.get_character(update.effective_user.id)

    # Per Coffee's request (2026-07-14): "view my party character sheets"
    # should show a real full sheet per member -- himself included, plus
    # any recruit/AI companion -- not just names. Only triggers on
    # explicit "sheet(s)" phrasing so plain "check my party" keeps its
    # existing compact behavior.
    if character and "sheet" in text.lower():
        party_id = character.get("party_id")
        if not party_id:
            await _safe_send(
                update,
                "You're not in a formed party — here's your own sheet:\n\n"
                + _format_character_sheet(character),
                speak=False,
            )
            return
        members = db.get_party_members_by_id(party_id)
        sheets = "\n\n".join(_format_character_sheet(m) for m in members)
        await _safe_send(
            update, f"🎗️ **Your party's sheets ({len(members)}/{db.PARTY_MAX_MEMBERS}):**\n\n{sheets}", speak=False,
        )
        return

    # Task #161 (real live incident): "Who's here at the market with me?"
    # asks a genuinely different question than the rest of this handler
    # answers -- everything below is about the player's PARTY (global,
    # not location-scoped), but "who's here" is asking who's physically
    # present at THIS location right now, party or not. Only triggers on
    # explicit "here" phrasing so plain "check my party" keeps its
    # existing global behavior unchanged.
    if character and re.search(r"\bhere\b", text.lower()):
        location = cl.get_location(CAMPAIGN, character["current_location"])
        location_name = location["name"] if location else "here"
        others_here = [
            p for p in _get_party_members()
            if p["current_location"] == character["current_location"]
            and p["telegram_user_id"] != character["telegram_user_id"]
        ]
        if others_here:
            names = [f"{p['name']} (AI)" if p.get("is_ai") else p["name"] for p in others_here]
            await _safe_send(
                update,
                f"👥 At **{location_name}** with **{character['name']}** right now: {', '.join(names)}",
                speak=False,
            )
        else:
            await _safe_send(
                update, f"👥 **{character['name']}** doesn't see anyone else at **{location_name}** right now.",
                speak=False,
            )
        return

    lines = [f"👥 Everyone currently active: {_party_summary_text()}"]

    if character:
        party_id = character.get("party_id")
        if party_id:
            members = db.get_party_members_by_id(party_id)
            # Presence (task #144): real players show a live derived
            # status + their own status note, if set; AI companions have
            # no presence of their own, so they just get the plain "(AI)"
            # tag they always had.
            names = []
            for m in members:
                if m.get("is_ai"):
                    names.append(f"{m['name']} (AI)")
                    continue
                note_suffix = f" — \"{m['status_note']}\"" if m.get("status_note") else ""
                names.append(f"{m['name']} ({_presence_status(m)}){note_suffix}")
            lines.append(
                f"\n🎗️ Your formed party ({len(members)}/{db.PARTY_MAX_MEMBERS}): {', '.join(names)}"
            )
        elif character.get("pending_party_invite"):
            lines.append("\n🎗️ You have a pending party invite — say \"I accept the party invite\" to join.")
        else:
            lines.append("\n🎗️ You're not in a formed party. Say \"invite [name] to my party\" to start one.")

    keyboard = _party_keyboard(character) if character else None
    await _safe_send(update, "\n".join(lines), reply_markup=keyboard, speak=False)


def _party_keyboard(character: dict) -> InlineKeyboardMarkup | None:
    """
    Per Coffee (2026-07-21): "add (Party) to the menu ... Being able to
    add or remove members also for battle planning ... would be
    awesome." Real tap buttons over the exact same invite/accept/leave
    logic free text already drives (_do_invite_to_party/
    _do_accept_party_invite/_do_leave_party) -- never a separate
    membership path. Invite candidates are simply anyone else active at
    this exact location not already in this same party -- a real
    player, an AI companion, or a not-yet-recruited NPC all show up the
    same way, since _get_party_members already returns all of them.
    """
    rows = []
    party_id = character.get("party_id")
    if party_id:
        rows.append([InlineKeyboardButton("🚪 Leave Party", callback_data="party|leave")])
    elif character.get("pending_party_invite"):
        rows.append([InlineKeyboardButton("✅ Accept Invite", callback_data="party|accept")])

    candidates = [
        p for p in _get_party_members()
        if p["current_location"] == character["current_location"]
        and p["telegram_user_id"] != character["telegram_user_id"]
        and not (party_id is not None and p.get("party_id") == party_id)
    ]
    for c in candidates[:5]:
        rows.append([InlineKeyboardButton(f"➕ Invite {c['name']}", callback_data=f"party|invite|{c['telegram_user_id']}")])
    return InlineKeyboardMarkup(rows) if rows else None


async def party_menu_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handles taps on _party_keyboard -- dispatches through the exact same real invite/accept/leave handlers free text already uses."""
    query = update.callback_query
    parts = (query.data or "").split("|")
    action = parts[1] if len(parts) > 1 else ""
    await _safe_answer(query)
    if action == "leave":
        await _do_leave_party(update)
    elif action == "accept":
        await _do_accept_party_invite(update)
    elif action == "invite" and len(parts) > 2:
        try:
            target_id = int(parts[2])
        except ValueError:
            return
        target = db.get_character(target_id)
        if target is None:
            return
        await _do_invite_to_party(update, target["name"])


async def _do_invite_to_party(update: Update, target_name: str) -> None:
    telegram_user_id = update.effective_user.id
    character = db.get_character(telegram_user_id)
    if character is None:
        await update.effective_chat.send_message(
            "You don't have a character yet!", message_thread_id=config.TOPIC_ADVENTURE_ID
        )
        return
    if not target_name:
        await update.effective_chat.send_message(
            "Invite who, exactly? Try \"invite [name] to my party\".",
            message_thread_id=config.TOPIC_ADVENTURE_ID,
        )
        return

    target = _find_party_target_by_name(target_name)
    if target is None:
        await update.effective_chat.send_message(
            f"No one named \"{target_name}\" is around to invite.", message_thread_id=config.TOPIC_ADVENTURE_ID
        )
        return
    if target["telegram_user_id"] == telegram_user_id:
        await update.effective_chat.send_message(
            "You can't invite yourself.", message_thread_id=config.TOPIC_ADVENTURE_ID
        )
        return

    party_id = character.get("party_id")
    if not party_id:
        party_id = db.create_party(telegram_user_id)

    if db.get_party_size(party_id) >= db.PARTY_MAX_MEMBERS:
        await update.effective_chat.send_message(
            f"Your party is already full ({db.PARTY_MAX_MEMBERS} members).",
            message_thread_id=config.TOPIC_ADVENTURE_ID,
        )
        return

    if target.get("party_id") == party_id:
        await update.effective_chat.send_message(
            f"{target['name']} is already in your party.", message_thread_id=config.TOPIC_ADVENTURE_ID
        )
        return

    if target.get("is_ai"):
        # AI companions have no real turn to accept with — they join immediately.
        db.add_ai_companion_to_party(target["telegram_user_id"], party_id)
        await _safe_send(update, f"🎗️ **{target['name']}** joins your party!")
        return

    db.set_pending_party_invite(target["telegram_user_id"], party_id)
    await _safe_send(
        update,
        f"🎗️ Invited **{target['name']}** to your party — they'll need to accept "
        f"(\"I accept the party invite\") to join.",
    )


async def _do_accept_party_invite(update: Update) -> None:
    success, message = db.accept_party_invite(update.effective_user.id)
    await _safe_send(update, ("🎗️ " if success else "") + message)


async def _do_leave_party(update: Update) -> None:
    left = db.leave_party(update.effective_user.id)
    message = "You've left your party." if left else "You're not in a party right now."
    await _safe_send(update, message)


def _feature_use_status(character: dict) -> str | None:
    """
    One line summarizing a character's per-rest limited-use class
    feature and how many uses remain, for the character sheet -- so a
    player can check before trying it, rather than only finding out via
    an "already used" reply when they attempt the command. Returns None
    for classes with no trackable per-rest feature (Rogue's Sneak
    Attack and Warlock's Pact Magic are both automatic, no uses to
    track; everything else not yet made mechanical has no state here).
    """
    telegram_user_id = character["telegram_user_id"]
    char_class = character["char_class"]
    if char_class == "Fighter":
        used = db.get_feature_uses(telegram_user_id, "second_wind")
        return f"Second Wind: {max(0, 1 - used)}/1 use(s) remaining this rest"
    if char_class == "Barbarian":
        used = db.get_feature_uses(telegram_user_id, "rage")
        return f"Rage: {max(0, RAGE_MAX_USES - used)}/{RAGE_MAX_USES} use(s) remaining this rest"
    if char_class == "Bard":
        max_uses = max(1, ability_modifier(character["charisma"]))
        used = db.get_feature_uses(telegram_user_id, "bardic_inspiration")
        return f"Bardic Inspiration: {max(0, max_uses - used)}/{max_uses} use(s) remaining this rest"
    if char_class == "Paladin":
        used = db.get_feature_uses(telegram_user_id, "lay_on_hands")
        return f"Lay on Hands: {max(0, 1 - used)}/1 use(s) remaining this rest"
    if char_class == "Wizard":
        used = db.get_feature_uses(telegram_user_id, "arcane_recovery")
        return f"Arcane Recovery: {max(0, 1 - used)}/1 use(s) remaining this rest"
    if char_class == "Druid":
        used = db.get_feature_uses(telegram_user_id, "wild_shape")
        return f"Wild Shape: {max(0, WILD_SHAPE_MAX_USES - used)}/{WILD_SHAPE_MAX_USES} use(s) remaining this rest"
    return None


def _find_campaign_npc_by_name(name: str) -> dict | None:
    """
    Looks up a campaign.json NPC (Grimsby, Old Maren, etc.) by display
    name, case-insensitive. Only for NPCs who were never recruited into
    a real character row -- once recruited (like Sarah), they show up
    via db.find_character_by_name/_find_party_target_by_name instead,
    with a real full sheet.
    """
    lowered = name.lower()
    for npc in CAMPAIGN["npcs"].values():
        if npc.get("name", "").lower() == lowered:
            return npc
    return None


def _format_npc_basic_info(npc: dict) -> str:
    """
    An un-recruited campaign NPC has no real 5E character sheet -- no
    ability scores/HP/AC in the player sense, just narrative fields in
    campaign.json (some do carry a 'stats' block used only if/when
    they're recruited, but showing it here would misrepresent an NPC
    who was never actually recruited as if they were already a party
    member with those exact numbers). Show what's actually true of them
    instead of fabricating or leaking a stat block.
    """
    lines = [f"**{npc['name']}** *(NPC — not currently recruited)*"]
    if npc.get("role"):
        lines.append(f"Role: {npc['role'].replace('_', ' ')}")
    if npc.get("personality"):
        lines.append(f"Personality: {npc['personality']}")
    if npc.get("disposition"):
        lines.append(f"Disposition: {npc['disposition']}")
    if npc.get("recruitable"):
        lines.append("They can be recruited — try inviting them to your party.")
    return "\n".join(lines)


def _format_equipped_line(character: dict) -> str:
    """
    Shared by _format_character_sheet and the character-creation
    summary (2026-07-15, per Coffee) -- what's actually worn, weapon/
    armor/shield each shown honestly as "none" rather than omitted
    when empty, so it's obvious at a glance there's nothing to equip.
    """
    weapon_item = items_module.get_item(character.get("equipped_weapon") or "")
    armor_item = items_module.get_item(character.get("equipped_armor") or "")
    shield_item = items_module.get_item(character.get("equipped_shield") or "")
    parts = [weapon_item["name"] if weapon_item else "no weapon",
             armor_item["name"] if armor_item else "no armor"]
    if shield_item:
        parts.append(shield_item["name"])
    for acc_id in character.get("equipped_accessories") or []:
        acc_item = items_module.get_item(acc_id)
        if acc_item:
            parts.append(acc_item["name"])
    return f"Equipped: {', '.join(parts)}\n"


def _format_carried_gear_line(character: dict) -> str:
    """
    Real weapons/armor/shields sitting in the backpack, NOT currently
    equipped (2026-07-15, per Coffee) -- a player asking "what could I
    equip" shouldn't have to cross-reference their own inventory list
    against items.py's type field by hand. Empty string (no line at
    all) if nothing equippable is being carried unequipped.
    """
    equipped_ids = {character.get("equipped_weapon"), character.get("equipped_armor"),
                    character.get("equipped_shield"), *(character.get("equipped_accessories") or [])}
    carried_names = []
    for item_id, qty in (character.get("inventory") or {}).items():
        if qty <= 0 or item_id in equipped_ids:
            continue
        item = items_module.get_item(item_id)
        if item and item.get("type") in ("weapon", "armor", "shield", "ring", "amulet", "wondrous"):
            carried_names.append(item["name"])
    if not carried_names:
        return ""
    return f"Carried but not equipped: {', '.join(sorted(carried_names))}\n"


def _format_character_sheet(character: dict) -> str:
    """
    Full sheet text for one character -- shared by _do_check_sheet (the
    asking player's own character) and the "show my party's sheets"
    path below (2026-07-14, per Coffee: asking for the whole party's
    sheets should show a real full sheet per member, including
    recruits/AI companions, not just names).
    """
    spell_names = [spells_module.get_spell(s)["name"] for s in character["known_spells"]]
    race_data = races_module.get_race(character["race"])
    features = class_features_module.get_class_features(character["char_class"])
    slot_line = ""
    if character["spell_slots_max"] > 0:
        slot_line = f"Spell slots: {character['spell_slots_current']}/{character['spell_slots_max']}\n"
    feature_status = _feature_use_status(character)
    feature_use_line = f"{feature_status}\n" if feature_status else ""
    # Per Coffee's request (2026-07-14): show each practiced skill's
    # current level right next to it on the sheet, not just buried in
    # skill_uses. practiced_bonus (rules/proficiency.py) already existed
    # as a real "the more you do it, the better you get" mechanic --
    # this just surfaces it. Only lists abilities actually used at least
    # once, so a fresh character's sheet isn't cluttered with six zeros.
    skill_uses = character.get("skill_uses") or {}
    skill_lines = [
        f"{ability.capitalize()} +{practiced_bonus(uses)} ({uses} use{'s' if uses != 1 else ''})"
        for ability, uses in sorted(skill_uses.items())
        if uses > 0
    ]
    skills_line = f"Skills: {', '.join(skill_lines) if skill_lines else 'None practiced yet'}\n"
    asi_line = ""
    if character.get("pending_asi_points"):
        asi_line = (
            f"📈 {character['pending_asi_points']} ability point(s) waiting to be spent — "
            f"say \"level up\" to choose.\n"
        )
    skill_points_line = ""
    if character.get("skill_points"):
        skill_points_line = (
            f"🌳 {character['skill_points']} skill point(s) banked — say \"skill tree\" to spend them.\n"
        )
    # Rebirth/hybrid (2026-07-22): only shown once a character has
    # actually gone through the rebirth loop at least once -- a never-
    # reborn character's sheet looks exactly as it always has.
    rebirth_line = ""
    if character.get("rebirth_count"):
        rebirth_line = f"✨ Reborn {character['rebirth_count']}x (ability cap {ability_score_cap(character['rebirth_count'])})\n"
        if character.get("hybrid_class"):
            rebirth_line += (
                f"🌟 Hybrid: {character['hybrid_class']} "
                f"(tier {hybrid_tier(character['rebirth_count'])}/{HYBRID_MAX_TIER})\n"
            )
    title_suffix = f" \"{character['active_title']}\"" if character.get("active_title") else ""
    name_line = f"**{character['name']}**{title_suffix}" + (" *(AI companion)*" if character.get("is_ai") else "")
    equipped_line = _format_equipped_line(character)
    carried_gear_line = _format_carried_gear_line(character)
    pronouns_line = f"Pronouns: {character['pronouns']}\n" if character.get("pronouns") else ""
    description_line = f"\"{character['description']}\"\n" if character.get("description") else ""
    # Presence (task #144): only real players have a meaningful status;
    # AI companions/NPCs have no presence of their own.
    presence_line = ""
    if not character.get("is_ai"):
        note_suffix = f" — \"{character['status_note']}\"" if character.get("status_note") else ""
        presence_line = f"Status: {_presence_status(character)}{note_suffix}\n"
    streak_line = ""
    if not character.get("is_ai") and character.get("login_streak_days", 0) > 0:
        days = character["login_streak_days"]
        streak_line = f"🔥 Login streak: {days} day{'s' if days != 1 else ''}\n"
    # Real live bug (2026-07-16, Coffee): ability scores were only ever
    # shown once, in the one-off creation sheet -- this shared sheet
    # (used by every later "check my sheet"/Support/party-sheet lookup)
    # never included them at all.
    ability_line = (
        f"STR {character['strength']} DEX {character['dexterity']} "
        f"CON {character['constitution']} INT {character['intelligence']} "
        f"WIS {character['wisdom']} CHA {character['charisma']}\n"
    )
    next_threshold = XP_THRESHOLDS.get(character["level"] + 1)
    xp_remaining_line = (
        f" ({max(next_threshold - character['xp'], 0)} XP to Level {character['level'] + 1})"
        if next_threshold is not None else " (Max level reached)"
    )
    return (
        f"{name_line} — {character['race']} {character['char_class']}\n"
        f"{pronouns_line}"
        f"{presence_line}"
        f"{streak_line}"
        f"{description_line}"
        f"Level {character['level']} | XP {character['xp']}{xp_remaining_line}\n"
        f"{rebirth_line}"
        f"HP {character['hp_current']}/{character['hp_max']} | AC {character['armor_class']}\n"
        f"{ability_line}"
        f"Alignment: {_alignment_label(character.get('alignment_law_chaos', 0), character.get('alignment_good_evil', 0))}\n"
        f"Gold: {character['gold']} | Guild: {character.get('guild') or 'None'}\n"
        f"{equipped_line}"
        f"{carried_gear_line}"
        f"Spells known: {', '.join(spell_names) if spell_names else 'None'}\n"
        f"{slot_line}"
        f"Racial traits: {'; '.join(race_data['traits']) if race_data else 'None'}\n"
        f"Class features: {'; '.join(features) if features else 'None'}\n"
        f"{feature_use_line}"
        f"{skills_line}"
        f"Location: {cl.get_location(CAMPAIGN, character['current_location'])['name']}\n"
        # Per Coffee (2026-07-16): pending ASI points shown last, so a
        # player who missed the level-up prompt still sees it waiting
        # every time they check their sheet, not just buried mid-sheet.
        f"{asi_line}"
        f"{skill_points_line}"
    ).rstrip("\n")


async def _do_check_sheet(update: Update, target_name: str | None = None) -> None:
    # Per Coffee's live report (2026-07-14): "Show me SERA's character
    # sheet" had nowhere to go -- check_sheet only ever showed the
    # asker's OWN sheet, so it fell through to "examine" and searched
    # for an interactable object named "Sarah" instead. A named target
    # is looked up among real party members first (same lookup used for
    # support-spell targeting), then broadened (2026-07-14, per Coffee:
    # "character sheets of all players AI, NPC and human") to any real
    # character row at all -- covers a human's other, non-currently-
    # active character slot -- and finally to un-recruited campaign
    # NPCs, who get honest basic info instead of a fabricated stat
    # block, since they don't have a real 5E sheet until recruited.
    if target_name:
        target = _find_party_target_by_name(target_name) or db.find_character_by_name(target_name)
        if target is not None:
            await _safe_send(update, _format_character_sheet(target), speak=False)
            return
        npc = _find_campaign_npc_by_name(target_name)
        if npc is not None:
            await _safe_send(update, _format_npc_basic_info(npc), speak=False)
            return
        # Real live bug (2026-07-16, Coffee): .title() on a hallucinated,
        # non-name target (the model can invent one when there's no real
        # action for a message to match -- see task #110/#112) mangled
        # ordinary text into nonsense like "Player'S Own Character".
        # target_name is echoed as given rather than reformatted.
        await update.effective_chat.send_message(
            f"Nobody named {target_name} is playing right now — can't show a sheet for them.",
            message_thread_id=config.TOPIC_ADVENTURE_ID,
        )
        return

    character = db.get_character(update.effective_user.id)
    if character is None:
        await update.effective_chat.send_message(
            "You don't have a character yet — say something like "
            "'I want to create a character' to get started!",
            message_thread_id=config.TOPIC_ADVENTURE_ID,
        )
        return
    # Task #176: spell-cast buttons only make sense on the ASKER's own
    # sheet (target_name is None here) -- nobody can tap a button to
    # cast someone else's spells.
    await _safe_send(
        update, _format_character_sheet(character), reply_markup=_with_menu_button(_spell_keyboard(character)),
        speak=False,
    )


async def _do_check_inventory(update: Update) -> None:
    character = db.get_character(update.effective_user.id)
    if character is None:
        await update.effective_chat.send_message(
            "You don't have a character yet!", message_thread_id=config.TOPIC_ADVENTURE_ID
        )
        return
    if not character["inventory"]:
        await update.effective_chat.send_message(
            "Your backpack is empty.", message_thread_id=config.TOPIC_ADVENTURE_ID
        )
        return
    lines = []
    for item_id, qty in character["inventory"].items():
        item = items_module.get_item(item_id)
        name = item["name"] if item else item_id
        lines.append(f"  {name} x{qty}")
    await _safe_send(
        update, "🎒 Your backpack:\n" + "\n".join(lines), reply_markup=_with_menu_button(_item_keyboard(character)),
        speak=False,
    )


def _item_keyboard(character: dict) -> InlineKeyboardMarkup | None:
    """
    Task #176 revision, per Coffee: real usable buttons for the backpack
    listing too, not just the sheet's spells -- his own stated example
    ("use a potion or charm a player") was a consumable, not a spell.
    Grounded in this character's actual carried consumables (qty > 0),
    same convention _battle_menu_keyboard's Items button already uses.
    Tapping opens the same shared _target_picker_keyboard as spells.
    """
    consumable_ids = [
        item_id for item_id, qty in character.get("inventory", {}).items()
        if qty > 0 and (items_module.get_item(item_id) or {}).get("type") == "consumable"
    ]
    if not consumable_ids:
        return None
    buttons = [
        [InlineKeyboardButton(f"🧪 {items_module.get_item(i)['name']}", callback_data=f"item|use|{i}")]
        for i in consumable_ids
    ]
    return InlineKeyboardMarkup(buttons)


async def item_menu_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handles taps on _item_keyboard and its target picker -- see their docstrings."""
    query = update.callback_query
    parts = (query.data or "").split("|")
    action = parts[1] if len(parts) > 1 else ""
    await _safe_answer(query)

    if action == "use":
        item_id = parts[2] if len(parts) > 2 else None
        item = items_module.get_item(item_id) if item_id else None
        character = db.get_character(update.effective_user.id)
        if item is None or character is None:
            return
        picker = _target_picker_keyboard("item", item_id, character)
        if picker is None:
            await _do_use_item(update, f"use {item['name']}")
            return
        await query.edit_message_reply_markup(reply_markup=picker)
        return

    if action == "target":
        item_id = parts[2] if len(parts) > 2 else None
        target_token = parts[3] if len(parts) > 3 else None
        item = items_module.get_item(item_id) if item_id else None
        if item is None or target_token is None:
            return
        if target_token == "self":
            await _do_use_item(update, f"use {item['name']}")
            return
        target_character = db.get_character(int(target_token))
        if target_character is None:
            return
        await _do_use_item(update, f"use {item['name']} on {target_character['name']}")


def _find_resource_node(location: dict, action_text: str) -> dict | None:
    nodes = location.get("resource_nodes", [])
    if not nodes:
        return None
    lowered = action_text.lower()
    for node in nodes:
        if node["id"] in lowered or node["material"] in lowered or node["name"].lower() in lowered:
            return node

    # Fallback: a word from the input as a substring of the node's
    # skill name or material id, in either direction -- confirmed live
    # 2026-07-14 (Coffee): "I would like to chop for lumber" didn't
    # match the wood node at all (material id is "wood", display name
    # mentions "timber", and neither contains "lumber"), even though
    # "lumber" is exactly what its skill (lumberjacking) is named for.
    stopwords = {"the", "a", "an", "of", "at", "on", "in", "to", "for", "and"}
    words = [w for w in lowered.split() if len(w) >= 4]
    for node in nodes:
        skill = node.get("skill", "")
        material = node["material"]
        name_words = {w for w in node["name"].lower().split() if w not in stopwords and len(w) >= 4}
        if any(
            (skill and (w in skill or skill in w)) or w in material or material in w or w in name_words
            for w in words
        ):
            return node

    if len(nodes) == 1:
        return nodes[0]
    return None


def _missing_tools_for_gathering(character: dict, skill_key: str) -> list[str]:
    """
    Real tool requirements (2026-07-16, per Coffee): fishing needs a
    fishing pole AND bait; lumberjacking needs a woodcutter's axe;
    mining needs a pickaxe. Herbalism deliberately needs no tool at all
    (picking a plant by hand is free) -- items.py's "required_for" field
    is the single source of truth, so a new tool-gated skill only ever
    needs a new items.py entry, not a bot.py code change. Returns the
    display names of any REQUIRED tools the character doesn't currently
    carry (empty list if fully equipped or the skill needs no tool).
    """
    required_items = [
        item for item in items_module.ITEMS.values()
        if item.get("required_for") == skill_key
    ]
    return [
        item["name"] for item in required_items
        if character["inventory"].get(_item_id_for(item), 0) <= 0
    ]


def _item_id_for(item: dict) -> str:
    return next(iid for iid, i in items_module.ITEMS.items() if i is item)


def _gather_quantity(character: dict, skill_key: str, practiced_bonus: int) -> int:
    """
    Real quantity bonus (2026-07-16, per Coffee): base gather is always
    1. Herbalism is the one tool-free skill, but owning Shears lets you
    harvest up to 3 per success (a dice roll, not a flat bonus) --
    Shears use "boosts_quantity_for" (items.py) since they're optional,
    unlike the required tools above. Extended 2026-07-21 (per Coffee:
    "add shovels to increase the amount of bait we can get... have it
    use a dice roll") to check ANY owned tool's "boosts_quantity_for"
    generically, instead of hardcoding Shears/herbalism specifically --
    Shovel/bait_gathering works the exact same way for free, and any
    future tool+skill pairing added to items.py needs zero new code
    here either. Separately, real practiced skill (the same
    practiced_bonus that already improves the success roll, see
    _practiced_bonus_for) has a small chance at one extra material on
    ANY gathering skill once it's capped out, rewarding repeated use
    beyond just a better roll target.
    """
    quantity = 1
    has_boost_tool = any(
        character["inventory"].get(item_id, 0) > 0 and item.get("boosts_quantity_for") == skill_key
        for item_id, item in items_module.ITEMS.items()
    )
    if has_boost_tool:
        quantity = roll(1, 3)[0]
    if practiced_bonus >= MAX_PRACTICE_BONUS and roll_d20() >= 15:
        quantity += 1
    return quantity


async def _do_gather(update: Update, action_text: str, forced_roll: int | None = None) -> None:
    """
    Gathering a raw material from a location's resource node — a real
    ability check (rules layer) decides success, matching every other
    outcome in this game; the AI only narrates it. Honors physical-dice
    mode (character.manual_dice_enabled) the same way _do_attack and
    _do_skill_check do -- real bug found live 2026-07-18 (Coffee: "if i
    have dice on shudnt i be rolling my own dice?"): this was the one
    outcome-deciding roll in the game that never checked
    manual_dice_enabled at all, silently always rolling internally
    regardless of the player's own preference.
    """
    character = db.get_character(update.effective_user.id)
    if character is None:
        await update.effective_chat.send_message(
            "You don't have a character yet!", message_thread_id=config.TOPIC_ADVENTURE_ID
        )
        return

    location = cl.get_location(CAMPAIGN, character["current_location"])
    node = _find_resource_node(location, action_text) if location else None
    if node is None:
        resource_nodes = location.get("resource_nodes", []) if location else []
        hint = f" Things worth gathering here: {', '.join(n['name'] for n in resource_nodes)}" if resource_nodes else ""
        await update.effective_chat.send_message(
            f"🌿 **{character['name']}** doesn't spot anything worth gathering here.{hint}",
            message_thread_id=config.TOPIC_ADVENTURE_ID,
        )
        return

    # 2026-07-14, per Coffee: named gathering professions (herbalism,
    # mining, fishing, lumberjacking) each level up independently, not
    # lumped into the shared ability-check practice track a combat skill
    # check would also feed. A node's "skill" field is the tracked
    # practice key; the dice roll itself still uses the node's "ability"
    # (e.g. mining is a Strength check, fishing is Wisdom) -- same
    # split db.py's skill_uses dict already supports for free, since
    # it's just an arbitrary string-keyed JSON dict, not one column per
    # ability. Falls back to the ability name for any node without an
    # explicit "skill" (keeps old behavior for anything not yet tagged).
    skill_key = node.get("skill", node["ability"])

    missing_tools = _missing_tools_for_gathering(character, skill_key)
    if missing_tools:
        await update.effective_chat.send_message(
            f"🌿 **{character['name']}** needs {' and '.join(missing_tools)} to do that — doesn't have "
            f"{'them' if len(missing_tools) > 1 else 'one'} yet. Check a shop.",
            message_thread_id=config.TOPIC_ADVENTURE_ID,
        )
        return

    if forced_roll is None and character.get("manual_dice_enabled") and not character.get("is_ai"):
        forced_roll = _extract_combined_roll(action_text)
    if forced_roll is None and character.get("manual_dice_enabled") and not character.get("is_ai"):
        _PENDING_DICE_ROLLS[update.effective_user.id] = _new_pending_roll("gather", action_text, update.effective_chat.id)
        # Task #187: _safe_send, not a raw send_message -- this exact site
        # dropped a real prompt live to a transient TimedOut before this fix.
        await _safe_send(update, f"🎲 **{character['name']}**, roll a d20 for this {node['ability']} check and tell me the result (you have 1 minute, or I'll roll for you).")
        return

    result = roll_ability_check(character, node["ability"], proficient=False, forced_roll=forced_roll)
    bonus = _practiced_bonus_for(update.effective_user.id, skill_key)
    result["total"] += bonus
    result["practiced_bonus"] = bonus
    success = result["total"] >= SKILL_CHECK_DC
    material = items_module.get_item(node["material"])
    quantity = _gather_quantity(character, skill_key, bonus) if success else 0

    if success:
        db.record_skill_use(update.effective_user.id, skill_key)
        db.add_item(update.effective_user.id, node["material"], quantity)

    # Per Coffee (2026-07-19): fishing consumes bait by chance, not on a
    # fixed schedule -- whether the cast lands a fish or not, roll a d20
    # each time and lose 1 bait on a low roll (<=10, a flat 50/50), same
    # "real dice decide it" pattern as every other outcome in this game.
    bait_lost = False
    if skill_key == "fishing" and character["inventory"].get("bait", 0) > 0:
        bait_lost = roll_d20() <= 10
        if bait_lost:
            db.remove_item(update.effective_user.id, "bait", 1)

    flavor = await asyncio.to_thread(
        narrate_skill_check, character, action_text, node["ability"],
        {**result, "ability": node["ability"], "dc": SKILL_CHECK_DC, "success": success},
    )
    message = _format_skill_check_result(flavor, result, node["ability"], SKILL_CHECK_DC, success)
    if success:
        message += f"\n🌿 **{character['name']}** gathers **{quantity}x {material['name']}**."
    if bait_lost:
        message += "\n🪱 The bait comes free of the hook and is gone."

    if success:
        # Matches against this player's own accepted board quests (task
        # #149, 2026-07-17), not get_todays_board_quests -- a quest
        # accepted on a previous calendar day but still inside its real
        # 24h expires_at window must still be creditable; day_key is
        # only about what's currently postable, not what's still valid.
        for board_quest in db.get_accepted_board_quests_for_user(update.effective_user.id):
            if not (board_quest["location_id"] == character["current_location"]
                    and board_quest["objective_type"] == "gather_material"
                    and board_quest["objective_target"] == node["material"]):
                continue
            updated = db.record_board_quest_progress(board_quest["board_quest_id"], quantity)
            if updated["progress_count"] >= updated["objective_count"]:
                if updated.get("branch_data"):
                    message += (
                        f"\n📜 **{updated['title']}** — objective complete. A decision awaits "
                        f"(check quests to see the choice)."
                    )
                else:
                    # Confirmed live 2026-07-12 (Coffee): a multi-step quest
                    # should tell the player where to go next instead of
                    # silently auto-granting the reward the instant the
                    # objective is met. The reward is now handed out by
                    # _check_board_quest_turnin, triggered on arrival at
                    # this same location_id (see _do_move/_do_fast_travel)
                    # -- exactly the same "objective done, come back here to
                    # collect" pattern as returning to a story quest-giver.
                    message += (
                        f"\n📜 **{updated['title']}** — objective complete! Return to "
                        f"**{location['name']}** to collect your reward."
                    )
            else:
                message += (
                    f"\n📋 Board quest progress: {updated['title']} "
                    f"({updated['progress_count']}/{updated['objective_count']})"
                )
            break

    await _safe_send(update, message)
    # Task #152, 2026-07-17: gathering always happens AT the quest's own
    # location (the crediting match above requires it), so a player who
    # completes the objective without ever having left never gets an
    # "arrival" event to trigger _check_board_quest_turnin's reward --
    # confirmed live, Coffee's "supply run for wood" sat at 4/4 with
    # completed_at still null because he was already standing in the
    # Whispering Wood the whole time. Checking turn-in right here (not
    # instead of the move-triggered checks, which still cover a player
    # who leaves and comes back later) closes that soft-lock without
    # changing the "come back to collect" framing for quests that
    # genuinely are fought/gathered somewhere else first.
    if success:
        await _check_board_quest_turnin(update, update.effective_user.id, character["current_location"])


async def _do_craft(update: Update, text: str) -> None:
    """
    Crafting is fully resolved by rules/crafting.py — material checks and
    the success roll are both deterministic; this handler only reports
    the outcome and applies the resulting inventory changes.
    """
    character = db.get_character(update.effective_user.id)
    if character is None:
        await update.effective_chat.send_message(
            "You don't have a character yet!", message_thread_id=config.TOPIC_ADVENTURE_ID
        )
        return

    recipe_id = items_module.find_item_mentioned_in_text(text, candidate_ids=list(RECIPES.keys()))
    if recipe_id is None:
        recipe_names = ", ".join(items_module.get_item(r)["name"] for r in RECIPES)
        await update.effective_chat.send_message(
            f"Not sure what you're trying to craft. Known recipes: {recipe_names}",
            message_thread_id=config.TOPIC_ADVENTURE_ID,
        )
        return

    # 2026-07-14, per Coffee: "crafting" is its own named, levelable
    # skill -- tracked separately from whichever ability a given recipe
    # happens to roll with (wisdom for a potion, intelligence for a
    # scroll), same split as the gathering professions above.
    bonus = _practiced_bonus_for(update.effective_user.id, "crafting")
    result = resolve_craft(character, recipe_id, practiced_bonus=bonus)

    if result["outcome"] == "missing_materials":
        recipe = get_recipe(recipe_id)
        need = ", ".join(
            f"{qty}x {items_module.get_item(i)['name']}" for i, qty in recipe["materials"].items()
        )
        await update.effective_chat.send_message(
            f"You don't have the materials for that. You need: {need}.",
            message_thread_id=config.TOPIC_ADVENTURE_ID,
        )
        return

    for item_id, qty in result["materials_consumed"].items():
        db.remove_item(update.effective_user.id, item_id, qty)

    success = result["outcome"] == "success"
    if success:
        db.record_skill_use(update.effective_user.id, "crafting")
        db.add_item(update.effective_user.id, result["result_item"], result["result_qty"])

    flavor = await asyncio.to_thread(
        narrate_skill_check, character, text, result["ability"],
        {**result["check"], "ability": result["ability"], "dc": result["dc"], "success": success},
    )
    message = _format_skill_check_result(flavor, result["check"], result["ability"], result["dc"], success)
    if success:
        result_item = items_module.get_item(result["result_item"])
        message += f"\n⚗️ You craft **{result['result_qty']}x {result_item['name']}**."
    else:
        message += "\n⚗️ The attempt fails, but your materials aren't wasted — you can try again."
    await _safe_send(update, message)


async def _do_make_campfire(update: Update) -> None:
    """
    Consumes 1 wood to make a campfire (2026-07-14, per Coffee's idea)
    -- a real, small, immediate HP benefit (1d4, deterministic dice roll
    same as every other outcome in this game), not full-rest-scale
    healing (that's what actually resting does, over real time). Doesn't
    require being combat-free -- unlike resting, a quick fire by the
    roadside isn't a multi-hour commitment, so it's allowed any time
    outside an active fight.
    """
    telegram_user_id = update.effective_user.id
    character = db.get_character(telegram_user_id)
    if character is None:
        await update.effective_chat.send_message(
            "You don't have a character yet!", message_thread_id=config.TOPIC_ADVENTURE_ID
        )
        return
    if sessions.get_session(update.effective_chat.id) is not None:
        await update.effective_chat.send_message(
            "Not in the middle of a fight.", message_thread_id=config.TOPIC_ADVENTURE_ID
        )
        return
    if character["inventory"].get("wood", 0) < 1:
        await update.effective_chat.send_message(
            "You don't have any wood to burn.", message_thread_id=config.TOPIC_ADVENTURE_ID
        )
        return
    if character["hp_current"] >= character["hp_max"]:
        await update.effective_chat.send_message(
            "Already at full health — no need for a fire right now.",
            message_thread_id=config.TOPIC_ADVENTURE_ID,
        )
        return

    db.remove_item(telegram_user_id, "wood", 1)
    heal = roll_damage("1d4")["total"]
    new_hp = min(character["hp_current"] + heal, character["hp_max"])
    actual_heal = new_hp - character["hp_current"]
    db.update_character(telegram_user_id, hp_current=new_hp)

    flavor = await asyncio.to_thread(
        narrate_action, character, "makes a campfire and warms up beside it",
        {"success": True},
    )
    await _safe_send(
        update,
        f"🔥 **{character['name']}** builds a campfire and rests beside its warmth.\n"
        f"> {flavor}\n\n"
        f"Recovers {actual_heal} HP ({new_hp}/{character['hp_max']}).",
    )


async def _do_second_wind(update: Update) -> None:
    """
    Real Fighter class feature: bonus action, once per rest, heal
    1d10 + fighter level HP. Uses the shared feature_uses resource
    (see db.use_feature/get_feature_uses) -- resets on a full rest,
    same as every other limited-use feature added in this pass.
    """
    character = db.get_character(update.effective_user.id)
    if character is None:
        await update.effective_chat.send_message(
            "You don't have a character yet!", message_thread_id=config.TOPIC_ADVENTURE_ID
        )
        return
    if character["char_class"] != "Fighter":
        await update.effective_chat.send_message(
            "Second Wind is a real Fighter class feature — your class doesn't have it.",
            message_thread_id=config.TOPIC_ADVENTURE_ID,
        )
        return
    if db.get_feature_uses(update.effective_user.id, "second_wind") >= 1:
        await update.effective_chat.send_message(
            "You've already used Second Wind since your last rest.",
            message_thread_id=config.TOPIC_ADVENTURE_ID,
        )
        return

    heal_dice = "2d10" if _has_skill_upgrade(character, "hardened_resolve") else "1d10"
    healed = roll_damage(heal_dice, modifier=character["level"])["total"]
    new_hp = min(character["hp_max"], character["hp_current"] + healed)
    actual_healed = new_hp - character["hp_current"]
    db.update_character(update.effective_user.id, hp_current=new_hp)
    db.use_feature(update.effective_user.id, "second_wind")

    await _safe_send(
        update,
        f"💨 **{character['name']}** catches their breath with Second Wind, recovering "
        f"**{actual_healed} HP** ({new_hp}/{character['hp_max']}).",
    )


RAGE_MAX_USES = 2
RAGE_DAMAGE_BONUS = 2
WILD_SHAPE_MAX_USES = 2


async def _do_rage(update: Update) -> None:
    """
    Real Barbarian class feature: bonus action to enter a rage — bonus
    melee damage and resistance to bludgeoning/piercing/slashing damage.
    2 uses per rest at level 1 (real 5E). The "raging" flag lives on the
    LIVE combat participant dict (session.participants), the same way
    existing conditions like prone/poisoned already do -- in-memory
    only, resets when combat ends, not persisted to the DB. Simplified
    from real 5E: lasts until combat ends rather than tracking the real
    per-round "no attack/no damage taken" end conditions, and applies as
    a flat damage resistance (half incoming damage) rather than only to
    the three specific physical damage types, since this combat system
    doesn't model damage types at all -- an honest, documented
    simplification, not an oversight.
    """
    character = db.get_character(update.effective_user.id)
    if character is None:
        await update.effective_chat.send_message(
            "You don't have a character yet!", message_thread_id=config.TOPIC_ADVENTURE_ID
        )
        return
    if character["char_class"] != "Barbarian":
        await update.effective_chat.send_message(
            "Rage is a real Barbarian class feature — your class doesn't have it.",
            message_thread_id=config.TOPIC_ADVENTURE_ID,
        )
        return
    max_rages = RAGE_MAX_USES + (1 if _has_skill_upgrade(character, "endless_fury") else 0)
    if db.get_feature_uses(update.effective_user.id, "rage") >= max_rages:
        await update.effective_chat.send_message(
            f"You've already raged {max_rages} times since your last rest.",
            message_thread_id=config.TOPIC_ADVENTURE_ID,
        )
        return

    session = sessions.get_session(update.effective_chat.id)
    if session is None:
        await update.effective_chat.send_message(
            "You can only enter a rage in combat.", message_thread_id=config.TOPIC_ADVENTURE_ID
        )
        return
    participant = next(
        (p for p in session.participants if p["telegram_user_id"] == update.effective_user.id), None
    )
    if participant is None:
        await update.effective_chat.send_message(
            "You're not part of the current fight.", message_thread_id=config.TOPIC_ADVENTURE_ID
        )
        return
    if participant.get("raging"):
        await update.effective_chat.send_message(
            "You're already raging.", message_thread_id=config.TOPIC_ADVENTURE_ID
        )
        return

    participant["raging"] = True
    db.use_feature(update.effective_user.id, "rage")

    await _safe_send(
        update,
        f"😡 **{character['name']}** flies into a rage — bonus damage and resistance to "
        f"physical harm for the rest of this fight!",
    )


async def _do_join_battle(update: Update) -> None:
    """
    Per Coffee (2026-07-21): "if the AI is in battle and human players
    are not, let them continue [elsewhere] ... if the characters want
    to join the battle, let them travel to the battle and join." Real
    gap this closes: combat participants were previously fixed the
    instant _do_start_combat ran (via _get_combat_eligible_party_members
    at that one moment) with no way to add a live participant
    afterward, even for a player who deliberately traveled to the exact
    location where the fight is happening. This is the other half of
    that -- non-participants were already free to gather/move/act
    normally the whole time (_do_gather/_do_move never checked for an
    active session at all), so nothing needed fixing there.
    """
    telegram_user_id = update.effective_user.id
    character = db.get_character(telegram_user_id)
    if character is None:
        await update.effective_chat.send_message(
            "You don't have a character yet!", message_thread_id=config.TOPIC_ADVENTURE_ID
        )
        return

    chat_id = update.effective_chat.id
    async with sessions.get_lock(chat_id):
        session = sessions.get_session(chat_id)
        if session is None:
            await update.effective_chat.send_message(
                "There's no fight happening right now.", message_thread_id=config.TOPIC_ADVENTURE_ID
            )
            return
        if any(p["telegram_user_id"] == telegram_user_id for p in session.participants):
            await update.effective_chat.send_message(
                "You're already part of this fight.", message_thread_id=config.TOPIC_ADVENTURE_ID
            )
            return
        if character["hp_current"] <= 0:
            await update.effective_chat.send_message(
                "You can't join a fight at 0 HP.", message_thread_id=config.TOPIC_ADVENTURE_ID
            )
            return

        # The fight's real location is wherever its own party members
        # actually are -- session participants carry current_location
        # frozen from when combat started (nobody mid-fight is moving),
        # so this is a real check, not a guess.
        party_members = [p for p in session.participants if session.sides.get(p["telegram_user_id"]) == "party"]
        battle_location = next((p.get("current_location") for p in party_members if p.get("current_location")), None)
        if battle_location is not None and character["current_location"] != battle_location:
            dest = cl.get_location(CAMPAIGN, battle_location)
            location_name = dest["name"] if dest else battle_location
            await update.effective_chat.send_message(
                f"The fight is happening at {location_name} — you'd need to go there first.",
                message_thread_id=config.TOPIC_ADVENTURE_ID,
            )
            return

        session.participants.append(character)
        session.sides[telegram_user_id] = "party"
        session.turn_order.append(telegram_user_id)
        await _safe_send(update, f"⚔️ **{character['name']}** joins the battle!")
        await _notify_main_topic(update, f"⚔️ **{character['name']}** joined an in-progress battle!")


async def _do_wild_shape(update: Update) -> None:
    """
    Real Druid class feature (level 2+, task #91 audit 2026-07-19: Druid
    was found to have ZERO unique mechanical features of its own, the
    only one of the 12 classes with nothing here at all -- spellcasting
    alone). 2 uses per rest (real 5E). The `wild_shaped` flag lives on
    the LIVE combat participant dict, same in-memory, combat-only
    convention as `raging` (see _do_rage) -- grants bonus claw/bite
    damage (wild_shape_damage_bonus) and a chunk of real temp_hp
    (wild_shape_temp_hp, the same mechanic Dark One's Blessing already
    uses), simplified from real 5E's actual separate beast statblock
    since this engine has no such system for any class. The real
    tradeoff (real 5E: a shapeshifted Druid can't cast spells) is
    enforced in _do_cast_spell, mirroring the existing `silenced`
    condition block there.
    """
    character = db.get_character(update.effective_user.id)
    if character is None:
        await update.effective_chat.send_message(
            "You don't have a character yet!", message_thread_id=config.TOPIC_ADVENTURE_ID
        )
        return
    if character["char_class"] != "Druid":
        await update.effective_chat.send_message(
            "Wild Shape is a real Druid class feature — your class doesn't have it.",
            message_thread_id=config.TOPIC_ADVENTURE_ID,
        )
        return
    if character["level"] < 2:
        await update.effective_chat.send_message(
            "Wild Shape is a Druid feature starting at level 2 — you're not there yet.",
            message_thread_id=config.TOPIC_ADVENTURE_ID,
        )
        return
    if db.get_feature_uses(update.effective_user.id, "wild_shape") >= WILD_SHAPE_MAX_USES:
        await update.effective_chat.send_message(
            f"You've already used Wild Shape {WILD_SHAPE_MAX_USES} times since your last rest.",
            message_thread_id=config.TOPIC_ADVENTURE_ID,
        )
        return

    session = sessions.get_session(update.effective_chat.id)
    if session is None:
        await update.effective_chat.send_message(
            "You can only Wild Shape in combat.", message_thread_id=config.TOPIC_ADVENTURE_ID
        )
        return
    participant = next(
        (p for p in session.participants if p["telegram_user_id"] == update.effective_user.id), None
    )
    if participant is None:
        await update.effective_chat.send_message(
            "You're not part of the current fight.", message_thread_id=config.TOPIC_ADVENTURE_ID
        )
        return
    if participant.get("wild_shaped"):
        await update.effective_chat.send_message(
            "You're already Wild Shaped.", message_thread_id=config.TOPIC_ADVENTURE_ID
        )
        return

    participant["wild_shaped"] = True
    bonus_temp_hp = wild_shape_temp_hp(character["level"])
    if _has_skill_upgrade(character, "primal_surge"):
        bonus_temp_hp += 2
    participant["temp_hp"] = max(participant.get("temp_hp", 0), bonus_temp_hp)
    db.use_feature(update.effective_user.id, "wild_shape")

    await _safe_send(
        update,
        f"🐾 **{character['name']}** shifts into a beast — {bonus_temp_hp} temporary HP and "
        f"clawed, biting attacks for the rest of this fight, but no spellcasting while shifted!",
    )


async def _do_action_surge(update: Update) -> None:
    """
    Real Fighter class feature (level 2+, 2026-07-16 level-2-10 audit):
    take an additional action on your turn, once per rest. Adapted to
    this engine's turn structure as doubling this turn's full attack
    sequence (real 5E: a Fighter with Extra Attack gets their WHOLE
    attack sequence again, not just one more swing) -- a flag on the
    live combat participant dict, same convention as `raging`, consumed
    (popped, not just read) by _do_attack's own attack_count
    calculation the very next time this Fighter attacks, so it can only
    ever double one attack sequence per use.
    """
    character = db.get_character(update.effective_user.id)
    if character is None:
        await update.effective_chat.send_message(
            "You don't have a character yet!", message_thread_id=config.TOPIC_ADVENTURE_ID
        )
        return
    if character["char_class"] != "Fighter":
        await update.effective_chat.send_message(
            "Action Surge is a real Fighter class feature — your class doesn't have it.",
            message_thread_id=config.TOPIC_ADVENTURE_ID,
        )
        return
    if character["level"] < 2:
        await update.effective_chat.send_message(
            "Action Surge is a Fighter feature starting at level 2 — you're not there yet.",
            message_thread_id=config.TOPIC_ADVENTURE_ID,
        )
        return
    if db.get_feature_uses(update.effective_user.id, "action_surge") >= 1:
        await update.effective_chat.send_message(
            "You've already used Action Surge since your last rest.",
            message_thread_id=config.TOPIC_ADVENTURE_ID,
        )
        return

    session = sessions.get_session(update.effective_chat.id)
    if session is None:
        await update.effective_chat.send_message(
            "You can only use Action Surge in combat.", message_thread_id=config.TOPIC_ADVENTURE_ID
        )
        return
    participant = next(
        (p for p in session.participants if p["telegram_user_id"] == update.effective_user.id), None
    )
    if participant is None:
        await update.effective_chat.send_message(
            "You're not part of the current fight.", message_thread_id=config.TOPIC_ADVENTURE_ID
        )
        return
    if participant.get("action_surge_active"):
        await update.effective_chat.send_message(
            "Action Surge is already active for your next attack.", message_thread_id=config.TOPIC_ADVENTURE_ID
        )
        return

    participant["action_surge_active"] = True
    db.use_feature(update.effective_user.id, "action_surge")

    await _safe_send(
        update,
        f"⚡ **{character['name']}** surges with action — their next attack this turn "
        f"comes as a full extra sequence!",
    )


async def _do_reckless_attack(update: Update) -> None:
    """
    Real Barbarian class feature (level 1, 2026-07-16 level-2-10 audit):
    attacking recklessly grants advantage on your attack rolls this
    turn. No per-rest limit in real 5E (usable every turn), so no
    feature_uses gating here -- just a one-shot `reckless_active` flag
    on the live combat participant dict, consumed by
    _attack_advantage_disadvantage the next time this Barbarian
    attacks. Real 5E's downside (attacks against you also get advantage
    until your next turn) is deliberately not modeled -- see the
    comment at _attack_advantage_disadvantage's reckless check.
    """
    character = db.get_character(update.effective_user.id)
    if character is None:
        await update.effective_chat.send_message(
            "You don't have a character yet!", message_thread_id=config.TOPIC_ADVENTURE_ID
        )
        return
    if character["char_class"] != "Barbarian":
        await update.effective_chat.send_message(
            "Reckless Attack is a real Barbarian class feature — your class doesn't have it.",
            message_thread_id=config.TOPIC_ADVENTURE_ID,
        )
        return

    session = sessions.get_session(update.effective_chat.id)
    if session is None:
        await update.effective_chat.send_message(
            "You can only attack recklessly in combat.", message_thread_id=config.TOPIC_ADVENTURE_ID
        )
        return
    participant = next(
        (p for p in session.participants if p["telegram_user_id"] == update.effective_user.id), None
    )
    if participant is None:
        await update.effective_chat.send_message(
            "You're not part of the current fight.", message_thread_id=config.TOPIC_ADVENTURE_ID
        )
        return

    participant["reckless_active"] = True
    await _safe_send(
        update,
        f"💥 **{character['name']}** attacks recklessly — advantage on their next attack this turn!",
    )


DIVINE_SMITE_DICE_BY_SLOT_LEVEL = {1: "2d8", 2: "3d8", 3: "4d8", 4: "5d8", 5: "5d8"}


async def _do_divine_smite(update: Update) -> None:
    """
    Real Paladin class feature (level 2+, 2026-07-16 level-2-10 audit):
    spending a spell slot on a hit for bonus radiant damage -- real 5E
    scaling by slot level (2d8 for a 1st-level slot, +1d8 per slot level
    above 1st, capped at 5d8). This engine's spell slots are a flat
    count with no per-level tracking (same simplification used
    everywhere else in this build -- see Arcane Recovery's own
    comment), so this always spends at "1st-level slot" scaling (2d8)
    rather than letting a higher slot be spent for more damage --
    there's no per-level slot data here to spend a "higher" one from.
    Declared BEFORE the attack (a `smite_active` flag, consumed on the
    Paladin's next hit), not decided after seeing the roll like real
    5E allows -- this engine's attack resolution doesn't have a
    post-hit decision point to hook a "spend it now or not" choice into.
    """
    character = db.get_character(update.effective_user.id)
    if character is None:
        await update.effective_chat.send_message(
            "You don't have a character yet!", message_thread_id=config.TOPIC_ADVENTURE_ID
        )
        return
    if character["char_class"] != "Paladin":
        await update.effective_chat.send_message(
            "Divine Smite is a real Paladin class feature — your class doesn't have it.",
            message_thread_id=config.TOPIC_ADVENTURE_ID,
        )
        return
    if character["level"] < 2:
        await update.effective_chat.send_message(
            "Divine Smite is a Paladin feature starting at level 2 — you're not there yet.",
            message_thread_id=config.TOPIC_ADVENTURE_ID,
        )
        return
    if character["spell_slots_current"] < 1:
        await update.effective_chat.send_message(
            "You don't have a spell slot left to smite with.", message_thread_id=config.TOPIC_ADVENTURE_ID
        )
        return

    session = sessions.get_session(update.effective_chat.id)
    if session is None:
        await update.effective_chat.send_message(
            "You can only smite in combat.", message_thread_id=config.TOPIC_ADVENTURE_ID
        )
        return
    participant = next(
        (p for p in session.participants if p["telegram_user_id"] == update.effective_user.id), None
    )
    if participant is None:
        await update.effective_chat.send_message(
            "You're not part of the current fight.", message_thread_id=config.TOPIC_ADVENTURE_ID
        )
        return
    if participant.get("smite_active"):
        await update.effective_chat.send_message(
            "Divine Smite is already primed for your next hit.", message_thread_id=config.TOPIC_ADVENTURE_ID
        )
        return

    participant["smite_active"] = True
    await _safe_send(
        update,
        f"🌟 **{character['name']}** channels divine wrath — their next hit will smite for bonus radiant damage!",
    )


async def _do_flurry_of_blows(update: Update) -> None:
    """
    Real Monk class feature (level 2+, 2026-07-16 level-2-10 audit): a
    ki point spent for two bonus unarmed strikes. Real 5E's ki pool
    size equals monk level, refreshing on a short rest -- this engine
    only has one rest granularity, so "ki" resets the same way every
    other limited feature does, on a full rest, capped at the
    character's own level (checked directly against feature_uses rather
    than a fixed constant like RAGE_MAX_USES, since the cap scales with
    level here). Adds 2 extra attacks to THIS turn via a participant
    flag, same mechanism as Action Surge -- consumed (popped) the next
    time this Monk attacks.
    """
    character = db.get_character(update.effective_user.id)
    if character is None:
        await update.effective_chat.send_message(
            "You don't have a character yet!", message_thread_id=config.TOPIC_ADVENTURE_ID
        )
        return
    if character["char_class"] != "Monk":
        await update.effective_chat.send_message(
            "Flurry of Blows is a real Monk class feature — your class doesn't have it.",
            message_thread_id=config.TOPIC_ADVENTURE_ID,
        )
        return
    if character["level"] < 2:
        await update.effective_chat.send_message(
            "Flurry of Blows is a Monk feature starting at level 2 — you're not there yet.",
            message_thread_id=config.TOPIC_ADVENTURE_ID,
        )
        return
    max_ki = character["level"] + (2 if _has_skill_upgrade(character, "iron_will") else 0)
    if db.get_feature_uses(update.effective_user.id, "ki") >= max_ki:
        await update.effective_chat.send_message(
            "You're out of ki points until your next rest.", message_thread_id=config.TOPIC_ADVENTURE_ID
        )
        return

    session = sessions.get_session(update.effective_chat.id)
    if session is None:
        await update.effective_chat.send_message(
            "You can only use Flurry of Blows in combat.", message_thread_id=config.TOPIC_ADVENTURE_ID
        )
        return
    participant = next(
        (p for p in session.participants if p["telegram_user_id"] == update.effective_user.id), None
    )
    if participant is None:
        await update.effective_chat.send_message(
            "You're not part of the current fight.", message_thread_id=config.TOPIC_ADVENTURE_ID
        )
        return
    if participant.get("flurry_bonus_attacks"):
        await update.effective_chat.send_message(
            "Flurry of Blows is already primed for your next attack.", message_thread_id=config.TOPIC_ADVENTURE_ID
        )
        return

    participant["flurry_bonus_attacks"] = 2
    db.use_feature(update.effective_user.id, "ki")

    await _safe_send(
        update,
        f"👊 **{character['name']}** spends a ki point — their next attack this turn comes with "
        f"2 bonus unarmed strikes!",
    )


async def _do_toggle_manual_dice(update: Update, action_text: str, thread_id: int | None = None) -> None:
    """
    Real per-player toggle (2026-07-16, per Coffee): switches physical-
    dice mode on/off any time, not just at character creation -- works
    from Adventure (via intent_parser's normal routing) or Support (see
    support_topic_handler's own direct intercept, same convention as
    Development's tts on/off command). "on" phrasing wins if a message
    somehow says both (shouldn't happen in practice given intent_
    parser's own distinct trigger phrases, but keeps this function's
    own logic unambiguous either way).
    """
    character = db.get_character(update.effective_user.id)
    if character is None:
        await update.effective_chat.send_message(
            "You don't have a character yet!", message_thread_id=thread_id or config.TOPIC_ADVENTURE_ID
        )
        return

    lowered = action_text.lower()
    turning_on = any(w in lowered for w in ("own dice", "own physical dice", "dice on", "turn on"))
    turning_off = any(w in lowered for w in ("let the game roll", "dice off", "turn off"))
    new_value = 1 if turning_on and not turning_off else 0
    db.update_character(update.effective_user.id, manual_dice_enabled=new_value)

    # Real bug (2026-07-19, per Coffee: "dice mode on doesnt seem to be
    # working with the battle system"): _do_attack/_do_shove/_do_flee/etc.
    # all check manual_dice_enabled on the session's IN-MEMORY participant
    # dict, which is a one-time snapshot taken from the DB when the fight
    # started (see sessions.start_session <- _get_combat_eligible_party_
    # members). The line above only ever updated the DB row, so toggling
    # dice mode while already in an active fight silently had zero effect
    # on that fight -- it would only apply starting with the NEXT combat.
    # Patch every active session's copy of this participant in place so a
    # mid-fight toggle takes effect on the very next roll, not just future
    # encounters.
    user_id = update.effective_user.id
    for session in sessions._ACTIVE_SESSIONS.values():
        for participant in session.participants:
            if participant["telegram_user_id"] == user_id:
                participant["manual_dice_enabled"] = new_value

    if new_value:
        await _safe_send(
            update,
            f"🎲 Physical dice mode is now **ON** for {character['name']}. When it's time for a key "
            f"roll (attack, skill check), I'll ask you to roll a d20 and tell me the result.",
            thread_id=thread_id,
        )
    else:
        await _safe_send(
            update,
            f"🎲 Physical dice mode is now **OFF** for {character['name']}. The game will roll for you again.",
            thread_id=thread_id,
        )


async def _do_breath_weapon(update: Update) -> None:
    """
    Real Dragonborn racial trait (2026-07-16 racial traits audit): races.py's
    own trait text called this "a real, usable action" but nothing anywhere
    ever implemented it -- confirmed by grep before this fix. Real 5E scaling
    (rules.leveling.breath_weapon_dice_count), once per rest, same feature_uses
    convention as Second Wind/Rage. Unlike a normal attack this doesn't roll to
    hit -- the target makes a saving throw for half damage instead (same
    save-or-half shape as spells.py's resolve_damage_spell), simplified to
    single-target since this combat engine has no area-of-effect modeling at
    all. Consumes the turn's action (unlike Rage/Second Wind, real 5E bonus
    actions), so it needs the same turn-order check as a normal attack.
    """
    character = db.get_character(update.effective_user.id)
    if character is None:
        await update.effective_chat.send_message(
            "You don't have a character yet!", message_thread_id=config.TOPIC_ADVENTURE_ID
        )
        return
    if character.get("race") != "Dragonborn":
        await update.effective_chat.send_message(
            "Breath Weapon is a real Dragonborn racial trait — your race doesn't have it.",
            message_thread_id=config.TOPIC_ADVENTURE_ID,
        )
        return
    if db.get_feature_uses(update.effective_user.id, "breath_weapon") >= 1:
        await update.effective_chat.send_message(
            "You've already used your Breath Weapon since your last rest.",
            message_thread_id=config.TOPIC_ADVENTURE_ID,
        )
        return

    chat_id = update.effective_chat.id
    async with sessions.get_lock(chat_id):
        session = sessions.get_session(chat_id)
        if session is None:
            await update.effective_chat.send_message(
                "You can only use your Breath Weapon in combat.", message_thread_id=config.TOPIC_ADVENTURE_ID
            )
            return
        user_id = update.effective_user.id
        if session.current_participant_id() != user_id:
            current_name = session.current_participant()["name"]
            await update.effective_chat.send_message(
                f"It's not your turn — it's **{current_name}**'s turn.",
                message_thread_id=config.TOPIC_ADVENTURE_ID,
            )
            return

        attacker = session.current_participant()
        if attacker["hp_current"] <= 0:
            await update.effective_chat.send_message(
                "You're unconscious (0 HP) and can't act until healed.",
                message_thread_id=config.TOPIC_ADVENTURE_ID,
            )
            return

        opposing = session.living_on_side(session.opposing_side(user_id))
        if not opposing:
            if not await _try_end_stale_combat(update, session):
                await update.effective_chat.send_message(
                    "No valid targets remain.", message_thread_id=config.TOPIC_ADVENTURE_ID
                )
            return
        target = opposing[0]

        dice_count = breath_weapon_dice_count(character["level"])
        dmg = roll_damage(f"{dice_count}d6")
        save_dc = 8 + character["proficiency_bonus"] + ability_modifier(character["constitution"])
        save_roll = roll_d20() + ability_modifier(target.get("dexterity", 10))
        save_success = save_roll >= save_dc
        damage_dealt = dmg["total"] // 2 if save_success else dmg["total"]
        target["hp_current"] = max(target["hp_current"] - damage_dealt, 0)
        _sync_player_to_db(target)
        db.use_feature(update.effective_user.id, "breath_weapon")

        save_text = "succeeds, taking half damage" if save_success else "fails"
        await _safe_send(
            update,
            f"🔥 **{attacker['name']}** unleashes their Breath Weapon at **{target['name']}** "
            f"({dice_count}d6 → {dmg['total']})! {target['name']}'s save {save_text} — "
            f"**{damage_dealt} damage** ({target['hp_current']}/{target.get('hp_max', target['hp_current'])} HP).",
        )

        removed = session.remove_defeated()
        await _announce_defeats(update, session, removed)

        if session.is_combat_over():
            winner = _determine_winner(session)
            xp_summary, level_up_notes = _award_victory_xp(session) if winner == "party" else ("", [])
            if winner == "party":
                await _check_quest_completions_defeat_monster(update, session)
                await _check_achievements_for_combat_party(update, session)
                await _check_guild_quest_completion(update, session)
            await _safe_send(update, f"🏆 **Combat over!** The {winner} side is victorious!{xp_summary}")
            for note in level_up_notes:
                await _notify_main_topic(update, note)
            sessions.end_session(chat_id)
            return

        session.advance_turn()
        await _resolve_ai_turns(update, session)


async def _do_channel_divinity(update: Update) -> None:
    """
    Real Cleric class feature (level 2+, 2026-07-16 class-features
    audit): Channel Divinity's universal Turn Undead option, forcing an
    undead creature to become frightened. Once per rest, same
    feature_uses convention as every other limited class feature.
    Simplified to this game's one undead-flavored monster type
    (rules.combat.UNDEAD_MONSTER_KEYS, currently just Shadow Wisp) and
    single-target (no area-of-effect modeling in this engine). Real 5E
    also grants a Divine Domain-specific 2nd Channel Divinity option --
    not modeled here, since Life Domain's real benefit (Disciple of
    Life, task #60) is already a different, always-on passive rather
    than its own Channel Divinity use.
    """
    character = db.get_character(update.effective_user.id)
    if character is None:
        await update.effective_chat.send_message(
            "You don't have a character yet!", message_thread_id=config.TOPIC_ADVENTURE_ID
        )
        return
    if character["char_class"] != "Cleric":
        await update.effective_chat.send_message(
            "Channel Divinity is a real Cleric class feature — your class doesn't have it.",
            message_thread_id=config.TOPIC_ADVENTURE_ID,
        )
        return
    if character["level"] < 2:
        await update.effective_chat.send_message(
            "Channel Divinity is a Cleric feature starting at level 2 — you're not there yet.",
            message_thread_id=config.TOPIC_ADVENTURE_ID,
        )
        return
    if db.get_feature_uses(update.effective_user.id, "channel_divinity") >= 1:
        await update.effective_chat.send_message(
            "You've already used Channel Divinity since your last rest.",
            message_thread_id=config.TOPIC_ADVENTURE_ID,
        )
        return

    session = sessions.get_session(update.effective_chat.id)
    if session is None:
        await update.effective_chat.send_message(
            "You can only Turn Undead in combat.", message_thread_id=config.TOPIC_ADVENTURE_ID
        )
        return
    opposing = session.living_on_side(session.opposing_side(update.effective_user.id))
    undead_targets = [p for p in opposing if p.get("monster_key") in UNDEAD_MONSTER_KEYS]
    if not undead_targets:
        await update.effective_chat.send_message(
            "There's no undead here to turn.", message_thread_id=config.TOPIC_ADVENTURE_ID
        )
        return

    target = undead_targets[0]
    target.setdefault("conditions", [])
    if "frightened" not in target["conditions"]:
        target["conditions"].append("frightened")
    db.use_feature(update.effective_user.id, "channel_divinity")

    await _safe_send(
        update,
        f"✨ **{character['name']}** channels divine power at **{target['name']}** — "
        f"it recoils, **FRIGHTENED**!",
    )


BARDIC_INSPIRATION_DIE = "1d6"


async def _do_bardic_inspiration(update: Update, target_text: str) -> None:
    """
    Real Bard class feature: bonus action, give an ally Bardic
    Inspiration. Real 5E grants a d6 to add to the ally's NEXT roll;
    simplified here to an immediate temporary-HP-style boost (rather
    than deferred pending-roll-bonus tracking, which would require
    touching every roll call site in the game) -- an honest, documented
    simplification, not a smaller version of the real rule pretending
    to be the same thing. Uses = Charisma modifier (minimum 1), real
    5E formula, refreshing on a full rest.
    """
    character = db.get_character(update.effective_user.id)
    if character is None:
        await update.effective_chat.send_message(
            "You don't have a character yet!", message_thread_id=config.TOPIC_ADVENTURE_ID
        )
        return
    if character["char_class"] != "Bard":
        await update.effective_chat.send_message(
            "Bardic Inspiration is a real Bard class feature — your class doesn't have it.",
            message_thread_id=config.TOPIC_ADVENTURE_ID,
        )
        return

    max_uses = max(1, ability_modifier(character["charisma"]))
    if db.get_feature_uses(update.effective_user.id, "bardic_inspiration") >= max_uses:
        await update.effective_chat.send_message(
            f"You're out of Bardic Inspiration until your next rest ({max_uses} use(s) per rest).",
            message_thread_id=config.TOPIC_ADVENTURE_ID,
        )
        return

    target_character = _find_party_target_by_name(target_text) or character
    inspiration_die = 8 if _has_skill_upgrade(character, "greater_inspiration") else 6
    boost = roll(1, inspiration_die)[0]
    new_hp = min(target_character["hp_max"], target_character["hp_current"] + boost)
    actual_boost = new_hp - target_character["hp_current"]
    db.update_character(target_character["telegram_user_id"], hp_current=new_hp)
    db.use_feature(update.effective_user.id, "bardic_inspiration")

    is_self = target_character["telegram_user_id"] == character["telegram_user_id"]
    target_note = "themself" if is_self else f"**{target_character['name']}**"
    await _safe_send(
        update,
        f"🎵 **{character['name']}** inspires {target_note} with a stirring word — "
        f"a bolstering **+{actual_boost} HP** ({new_hp}/{target_character['hp_max']}).",
    )


async def _do_lay_on_hands(update: Update, target_text: str) -> None:
    """
    Real Paladin class feature: touch to heal from a pool of 5 x
    paladin level HP. Real 5E lets you spend the pool in any increment
    across multiple uses; simplified here to spending the WHOLE pool in
    one action, once per rest (same shape as Second Wind) rather than
    tracking a separately-spendable partial pool -- an honest,
    documented simplification matching how most actual play spends it
    anyway.
    """
    character = db.get_character(update.effective_user.id)
    if character is None:
        await update.effective_chat.send_message(
            "You don't have a character yet!", message_thread_id=config.TOPIC_ADVENTURE_ID
        )
        return
    if character["char_class"] != "Paladin":
        await update.effective_chat.send_message(
            "Lay on Hands is a real Paladin class feature — your class doesn't have it.",
            message_thread_id=config.TOPIC_ADVENTURE_ID,
        )
        return
    if db.get_feature_uses(update.effective_user.id, "lay_on_hands") >= 1:
        await update.effective_chat.send_message(
            "You've already used your Lay on Hands pool since your last rest.",
            message_thread_id=config.TOPIC_ADVENTURE_ID,
        )
        return

    pool_per_level = 6 if _has_skill_upgrade(character, "greater_mercy") else 5
    pool = pool_per_level * character["level"]
    target_character = _find_party_target_by_name(target_text) or character
    new_hp = min(target_character["hp_max"], target_character["hp_current"] + pool)
    actual_healed = new_hp - target_character["hp_current"]
    db.update_character(target_character["telegram_user_id"], hp_current=new_hp)
    db.use_feature(update.effective_user.id, "lay_on_hands")

    is_self = target_character["telegram_user_id"] == character["telegram_user_id"]
    target_note = "themself" if is_self else f"**{target_character['name']}**"
    await _safe_send(
        update,
        f"🙏 **{character['name']}** lays hands on {target_note}, channeling divine healing — "
        f"**{actual_healed} HP** restored ({new_hp}/{target_character['hp_max']}).",
    )


async def _do_arcane_recovery(update: Update) -> None:
    """
    Real Wizard class feature: once per day, recover expended spell
    slots without a full rest. Real 5E caps this by COMBINED spell
    level (up to half wizard level, rounded up, never a 6th-level-or-
    higher slot) -- this game's spell_slots_current/max are a flat
    count with no per-level tracking at all (a simplification already
    used everywhere else, e.g. starting slots, spend_spell_slot), so
    this instead recovers a NUMBER of slots equal to half wizard level
    (rounded up), capped at whatever's actually missing -- an honest
    adaptation of the real rule to the flat-count model already in use,
    not a smaller version of the rule pretending to be the same thing.
    Same feature_uses gating as every other limited-use feature added
    this pass (once per rest cycle).
    """
    character = db.get_character(update.effective_user.id)
    if character is None:
        await update.effective_chat.send_message(
            "You don't have a character yet!", message_thread_id=config.TOPIC_ADVENTURE_ID
        )
        return
    if character["char_class"] != "Wizard":
        await update.effective_chat.send_message(
            "Arcane Recovery is a real Wizard class feature — your class doesn't have it.",
            message_thread_id=config.TOPIC_ADVENTURE_ID,
        )
        return
    if db.get_feature_uses(update.effective_user.id, "arcane_recovery") >= 1:
        await update.effective_chat.send_message(
            "You've already used Arcane Recovery since your last rest.",
            message_thread_id=config.TOPIC_ADVENTURE_ID,
        )
        return

    missing_slots = character["spell_slots_max"] - character["spell_slots_current"]
    if missing_slots <= 0:
        await update.effective_chat.send_message(
            "Your spell slots are already full — nothing to recover.",
            message_thread_id=config.TOPIC_ADVENTURE_ID,
        )
        return

    max_recoverable = (character["level"] + 1) // 2
    if _has_skill_upgrade(character, "deeper_recovery"):
        max_recoverable += 1
    recovered = min(max_recoverable, missing_slots)
    new_current = character["spell_slots_current"] + recovered
    db.update_character(update.effective_user.id, spell_slots_current=new_current)
    db.use_feature(update.effective_user.id, "arcane_recovery")

    slot_word = "slot" if recovered == 1 else "slots"
    await _safe_send(
        update,
        f"📖 **{character['name']}** studies for a moment, recovering **{recovered} spell {slot_word}** "
        f"through Arcane Recovery ({new_current}/{character['spell_slots_max']}).",
    )


def _look_action_keyboard(location: dict, unclaimed_board_quests: list) -> InlineKeyboardMarkup | None:
    """
    Per Coffee (2026-07-21): "when u look around put pop up options for
    areas u have already traveled to so they can tap ... (use fog of
    war)." A location's own `connections` are exactly the fog-of-war-
    correct set here -- they're only ever the places actually visible/
    reachable standing right here, never a spoiler of anything further
    off (that's what the separate Waypoints menu is for, covering
    already-visited-but-distant places instead). Tapping a travel row
    dispatches through the exact same _do_move free text already uses,
    so every existing gate (requires_item, locked_connections,
    min_level, story_gates) still applies unchanged.

    Extended (2026-07-22, per Coffee: "if u look around and a place is
    visble let them hit buttons for it -- example, I look around the
    market and see the stalls of shop items, add a shop button abov
    the locations"): the look text already NAMES a couple of other
    obviously-tappable things at a glance -- a shop, and a quest board
    -- so those get real buttons too, stacked above the travel rows
    the same way Coffee described. Both are deterministic (no Ollama
    call, no extra state), which is why these two specifically and not
    also a "talk to X" button per NPC present -- talking is its own
    real conversation, not a single obvious tap target.
    """
    rows = []
    if location.get("shop"):
        rows.append([InlineKeyboardButton("🛒 Shop", callback_data="lookact|shop")])
    if unclaimed_board_quests:
        rows.append([InlineKeyboardButton("📋 Quest Board", callback_data="lookact|quests")])

    connections = location.get("connections", [])
    direction_for_dest = {dest: word.capitalize() for word, dest in location.get("directions", {}).items()}
    for dest_id in connections:
        dest = cl.get_location(CAMPAIGN, dest_id)
        if dest is None:
            continue
        label = f"{direction_for_dest[dest_id]}: {dest['name']}" if dest_id in direction_for_dest else dest["name"]
        rows.append([InlineKeyboardButton(f"🚶 {label}", callback_data=f"travel|go|{dest_id}")])
    return InlineKeyboardMarkup(rows) if rows else None


async def travel_menu_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handles taps on _look_action_keyboard's travel rows -- dispatches through the exact same _do_move a typed destination name already uses."""
    query = update.callback_query
    parts = (query.data or "").split("|")
    action = parts[1] if len(parts) > 1 else ""
    await _safe_answer(query)
    if action != "go" or len(parts) < 3:
        return
    dest = cl.get_location(CAMPAIGN, parts[2])
    if dest is None:
        return
    await _do_move(update, dest["name"])


async def look_action_menu_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handles taps on _look_action_keyboard's Shop/Quest Board rows -- dispatches through the exact same real handlers free text already uses."""
    query = update.callback_query
    parts = (query.data or "").split("|")
    action = parts[1] if len(parts) > 1 else ""
    await _safe_answer(query)
    if action == "shop":
        await _do_list_shop(update)
    elif action == "quests":
        await _do_check_quests(update)


def _deterministic_image_seed(key: str) -> int:
    """
    Shared deterministic seed (2026-07-22, task "images for the whole
    game"): the same real key (a location id, an NPC id, a monster
    key, an item id) always produces the same seed, so every player
    sees the exact same generated depiction of that same real thing --
    a stable, canonical look, never a different random image each
    time. Same principle as world_clock.py's own deterministic per-day
    weather hash. Used across every image type this game generates
    (locations, NPC portraits, and whatever comes next), not just one.
    """
    return int(hashlib.sha256(key.encode()).hexdigest(), 16) % (2 ** 31)


def _location_image_seed(location_id: str) -> int:
    return _deterministic_image_seed(f"location:{location_id}")


async def _maybe_send_location_image(update: Update, location: dict, location_id: str, already_visited: bool) -> None:
    """
    Real location art, sent every time (2026-07-22, per Coffee: "show
    images even after the first time" -- reverses this function's
    original first-visit-only gating). `already_visited` is kept as a
    parameter (unused for gating now) since both call sites already
    compute it and it costs nothing to keep passing. Same
    Pollinations.ai service as the existing character-creation
    portraits (task #81) and visual world map (task #221), same
    grounding discipline: the prompt is built only from this location's
    own real description text already in campaign.json, never invented
    detail. Still uses a deterministic per-location seed
    (_location_image_seed) so the SAME place always shows the SAME
    image, never a different random one each visit.
    """
    prompt = (
        f"{location['description']}, fantasy tabletop RPG environment concept art, "
        "atmospheric lighting, detailed digital painting, no text or labels"
    )
    try:
        await update.effective_chat.send_photo(
            photo=images_module.generate_image_url(
                prompt, width=768, height=512, seed=_location_image_seed(location_id),
            ),
            caption=f"📍 {location['name']}",
            message_thread_id=config.TOPIC_ADVENTURE_ID,
        )
    except Exception as e:
        logger.warning(f"[images] location image failed for {location_id!r}: {e!r}")


def _npc_portrait_prompt(npc_data: dict) -> str:
    """
    Grounded ONLY in real fields already in campaign.json -- role,
    personality, and (for recruitable companions like Sarah, who carry
    a real stats block) race/class. Deliberately never invents a
    gender or appearance detail that isn't already a stated fact,
    same "never invent a game fact" discipline as everywhere else in
    this game (and the same reasoning that kept ai/tts_piper.py's two
    voices purpose-based rather than guessing NPC genders). Non-
    recruitable NPCs (most of them) have no stats block at all, so
    race_class is simply omitted for those rather than guessed.
    """
    stats = npc_data.get("stats") or {}
    race_class = f"{stats['race']} {stats['char_class']}, " if stats.get("race") and stats.get("char_class") else ""
    role = (npc_data.get("role") or "").replace("_", " ")
    return (
        f"fantasy RPG character portrait, {race_class}{role}, "
        f"{npc_data.get('personality', '')}, digital painting"
    )


def _item_image_prompt(item_data: dict) -> str:
    """Grounded only in the item's own real name/type/rarity fields -- no invented material/design detail."""
    rarity = item_data.get("rarity", "common")
    item_type = item_data.get("type", "item")
    return (
        f"fantasy RPG {rarity} {item_type} icon, {item_data['name']}, "
        "isolated on a plain background, digital game art, no text or labels"
    )


async def _maybe_send_item_image(update: Update, item_id: str, item_data: dict) -> None:
    """
    Real item icon (2026-07-22, task "images for the whole game" --
    item icons, last of the four pieces asked for). Sent when a
    character actually equips something -- a deliberate single action,
    not the bulk loot-drop/inventory-listing moment, which would spam
    many images at once for little value. Same deterministic-per-item
    convention as locations/NPCs/monsters.
    """
    prompt = _item_image_prompt(item_data)
    try:
        await update.effective_chat.send_photo(
            photo=images_module.generate_image_url(
                prompt, width=512, height=512, seed=_deterministic_image_seed(f"item:{item_id}"),
            ),
            caption=f"🎒 {item_data['name']}",
            message_thread_id=config.TOPIC_ADVENTURE_ID,
        )
    except Exception as e:
        logger.warning(f"[images] item image failed for {item_id!r}: {e!r}")


def _monster_image_prompt(template: dict) -> str:
    """Grounded only in the monster's own real name and is_boss flag -- no invented physical detail beyond generic 'fantasy monster' framing."""
    creature_desc = "a fearsome, powerful boss monster" if template.get("is_boss") else "a monster"
    return f"fantasy RPG {creature_desc}, {template['name']}, digital painting, dramatic lighting, no text or labels"


async def _maybe_send_monster_image(update: Update, monster_key: str, template: dict) -> None:
    """
    Real monster art (2026-07-22, task "images for the whole game" --
    monster/combat images, third of the four pieces asked for). Sent
    every time a fight starts against this monster type, same
    deterministic-per-key convention as locations/NPCs -- the same
    monster always gets the same generated depiction.
    """
    prompt = _monster_image_prompt(template)
    try:
        await update.effective_chat.send_photo(
            photo=images_module.generate_image_url(
                prompt, width=512, height=512, seed=_deterministic_image_seed(f"monster:{monster_key}"),
            ),
            caption=f"⚔️ {template['name']}",
            message_thread_id=config.TOPIC_ADVENTURE_ID,
        )
    except Exception as e:
        logger.warning(f"[images] monster image failed for {monster_key!r}: {e!r}")


async def _maybe_send_npc_portrait(update: Update, npc_id: str, npc_data: dict) -> None:
    """
    Real NPC portrait (2026-07-22, task "images for the whole game" --
    NPC portraits, second of the four pieces asked for). Sent every
    time a player talks to an NPC, same "always show, not just first
    time" convention just established for location images -- but still
    the exact same deterministic image per NPC every time
    (_deterministic_image_seed), never a different random depiction.
    """
    prompt = _npc_portrait_prompt(npc_data)
    try:
        await update.effective_chat.send_photo(
            photo=images_module.generate_image_url(
                prompt, width=512, height=512, seed=_deterministic_image_seed(f"npc:{npc_id}"),
            ),
            caption=f"🎨 {npc_data.get('name', npc_id)}",
            message_thread_id=config.TOPIC_ADVENTURE_ID,
        )
    except Exception as e:
        logger.warning(f"[images] NPC portrait failed for {npc_id!r}: {e!r}")


async def _do_look(update: Update) -> None:
    character = db.get_character(update.effective_user.id)
    if character is None:
        await update.effective_chat.send_message(
            "You don't have a character yet!", message_thread_id=config.TOPIC_ADVENTURE_ID
        )
        return
    location = cl.get_location(CAMPAIGN, character["current_location"])
    if location is None:
        await update.effective_chat.send_message(
            f"**{character['name']}** seems to be nowhere in particular. That's... concerning.",
            message_thread_id=config.TOPIC_ADVENTURE_ID,
        )
        return

    already_visited = character["current_location"] in (character.get("visited_locations") or [])
    db.mark_visited(update.effective_user.id, character["current_location"])

    # Names the looker (task #139, 2026-07-17): in a shared chat, a bare
    # "📍 Location Name" reply doesn't say WHO just looked around --
    # same ambiguous-actor problem already fixed for combat/skill-check
    # narration (#107/#115), just never applied to this deterministic
    # (non-AI) reply path.
    lines = [f"👁️ **{character['name']}** looks around.", f"📍 **{location['name']}** ({location['layer']})", location["description"]]
    lines.append(f"🌤️ {world_clock.conditions_line(location['layer'])}")
    npcs_here = _npcs_at_location(character["current_location"])
    if npcs_here:
        npc_names = [cl.get_npc(CAMPAIGN, n)["name"] for n in npcs_here if cl.get_npc(CAMPAIGN, n)]
        lines.append(f"People here: {', '.join(npc_names)}")
    monsters_here = location.get("monsters", [])
    if monsters_here:
        lines.append(f"You sense danger here: {', '.join(monsters_here)}")
    connections = location.get("connections", [])
    if connections:
        # Sensory travel descriptions (task #220, per Coffee: "explain
        # to them how/why they can go there" instead of just naming a
        # destination) -- an optional, purely additive per-connection
        # sentence, layered the same way "directions" already is over
        # the real connections list. Only written for a handful of
        # major, farther-off destinations so far (not every short
        # interior hop needs one); anything without a sensory line
        # keeps falling back to the flat name list exactly as before.
        sensory_for_dest = location.get("sensory_connections", {})
        sensory_conns = [c for c in connections if c in sensory_for_dest]
        for c in sensory_conns:
            lines.append(f"👀 {sensory_for_dest[c]}")

        # Compass labels (world-expansion pass, 2026-07-19): "directions" is
        # a pure display layer over the real "connections" reachability list
        # -- inverted here so a connection with a compass word shows it
        # (e.g. "North: The Deep Glade"), while any connection still without
        # one (every pre-expansion location) falls back to the plain name,
        # exactly as before.
        plain_conns = [c for c in connections if c not in sensory_for_dest]
        if plain_conns:
            direction_for_dest = {dest: word.capitalize() for word, dest in location.get("directions", {}).items()}
            conn_labels = [
                f"{direction_for_dest[c]}: {cl.get_location(CAMPAIGN, c)['name']}" if c in direction_for_dest
                else cl.get_location(CAMPAIGN, c)["name"]
                for c in plain_conns
            ]
            lines.append(f"You can travel to: {', '.join(conn_labels)}")
    if "descends_to" in location:
        lines.append(f"You could descend to: {cl.get_location(CAMPAIGN, location['descends_to'])['name']}")
    if "ascends_to" in location:
        lines.append(f"You could ascend to: {cl.get_location(CAMPAIGN, location['ascends_to'])['name']}")
    interactables = location.get("interactables", {})
    if interactables:
        names = [i["name"] for i in interactables.values()]
        lines.append(f"Things worth a closer look: {', '.join(names)}")
    resource_nodes = location.get("resource_nodes", [])
    if resource_nodes:
        node_names = [n["name"] for n in resource_nodes]
        lines.append(f"Resources here: {', '.join(node_names)}")
        sensed = [
            n for n in resource_nodes
            if RESOURCE_SENSE_CLASS.get(n["ability"]) == character["char_class"]
        ]
        if sensed:
            sensed_materials = ", ".join(
                items_module.get_item(n["material"])["name"] for n in sensed
            )
            lines.append(
                f"Your {character['char_class']} training picks {sensed_materials} out "
                f"at a glance — most people would walk right past it."
            )

    # Quest hints on "look around" (2026-07-16, per Coffee): a location
    # with a quest tied to it, or a board quest posted, used to give no
    # sign of either -- players had to separately think to ask "check
    # quests" even at a place clearly meant to offer one. Story quests
    # have no structured NPC field in campaign.json (see
    # _npc_quest_facts's own comment on this), so any NPC actually
    # present is the approximation used to point players at someone to
    # talk to. Board quests are generated here too (not just shown),
    # matching check_quests's own behavior, so the FIRST person to look
    # around a location each day is the one who reveals what's posted.
    story_offer = _offerable_quest_at_location(character, character["current_location"])
    if story_offer:
        if npcs_here:
            lines.append(f"📜 {npc_names[0]} looks like they could use your help with something.")
        else:
            lines.append("📜 There's a task tied to this place, though no one's here to ask about it right now.")

    board_quests_here = board_quests_module.get_or_generate_board_quests(CAMPAIGN, character["current_location"])
    unclaimed_board_quests = [q for q in board_quests_here if not q.get("accepted_by")]
    if unclaimed_board_quests:
        lines.append("📋 There's a bounty posted on the board here — say \"check quests\" to see it.")

    # Confirmed live 2026-07-14 (Coffee): TTS was silently skipped for
    # "look around" -- this reply went straight to
    # update.effective_chat.send_message, bypassing _safe_send
    # entirely, and _maybe_speak (TTS) is only ever hooked in there.
    # This is one of many direct-send call sites in this file (~195 of
    # them vs 64 through _safe_send) -- most are short refusal/error
    # messages that were never meant to be narrated aloud, but "look"
    # is real player-facing narration and should have gone through
    # _safe_send like every other primary action reply already does.
    await _safe_send(update, "\n".join(lines), reply_markup=_look_action_keyboard(location, unclaimed_board_quests))
    await _maybe_send_location_image(update, location, character["current_location"], already_visited)


async def _do_check_weather(update: Update) -> None:
    """
    Weather/day-night (task #84): a direct query ("what's the weather
    like") separate from a full "look around" -- same real, deterministic
    world_clock.conditions_line already shown there, just without the
    rest of the location description.
    """
    character = db.get_character(update.effective_user.id)
    if character is None:
        await _safe_send(update, "You don't have a character yet!", speak=False)
        return
    location = cl.get_location(CAMPAIGN, character["current_location"])
    if location is None:
        await _safe_send(update, "Hard to say — you don't seem to be anywhere in particular.", speak=False)
        return
    conditions = world_clock.conditions_line(location["layer"])
    if location["layer"] == "underground":
        await _safe_send(update, f"🌤️ {conditions} — no real weather reaches this deep, just the dark.", speak=False)
        return

    lines = [f"🌤️ {conditions} at **{location['name']}**."]
    # Real forecast (task, per Coffee, 2026-07-21): the exact same
    # deterministic (day, region) weather roll current_weather already
    # uses, just walked forward -- tomorrow's forecast really is
    # tomorrow's real weather, never a separate invented guess.
    upcoming = world_clock.forecast(location["layer"], days=3)[1:]
    if upcoming:
        forecast_line = ", ".join(f"{date} — {weather}" for date, weather in upcoming)
        lines.append(f"📅 Forecast: {forecast_line}")
    if world_clock.is_hazardous(location["layer"]):
        lines.append("⚠️ Underfoot's slick out there — footing hazards are real right now.")
    if world_clock.is_night():
        lines.append("🌙 Whatever's out there tonight is hunting with more bite than usual.")
    await _safe_send(update, "\n".join(lines), speak=False)


def _find_interactable(location: dict, text: str) -> tuple[str, dict] | None:
    """
    Fuzzy-ish lookup of a real, campaign-defined interactable object at
    this location, matched against free text (e.g. "examine the old
    barrel" -> the marked_barrel entry at tavern_cellar). Same
    name-appears-in-text approach as items.find_item_mentioned_in_text,
    for the same reason: trusting a real substring match over asking
    the small classifier model to extract the object name exactly.
    """
    interactables = location.get("interactables", {})
    # Real live bug (2026-07-19, confirmed live: "Look at maren's brass
    # scale" -- a real, described interactable -- failed even though a
    # longer phrasing of the exact same request succeeded): a phone
    # keyboard's smart-quote autocorrect sends a curly apostrophe (’,
    # U+2019), which never equals the straight one (') this game's own
    # stored names use, so "maren's" from the player's own message could
    # never match the word "Maren's" in "Old Maren's brass scale" at
    # all -- same single-character-collision shape as the existing
    # armour/armor and fishing rod/pole normalizations, just a
    # punctuation mark instead of a whole word this time.
    lowered = text.strip().lower().replace("’", "'")
    ordered = sorted(interactables.items(), key=lambda kv: -len(kv[1]["name"]))
    for obj_id, data in ordered:
        if data["name"].lower() in lowered or obj_id.replace("_", " ") in lowered:
            return obj_id, data

    # Fallback 1: ignore a leading article and compound-word spacing --
    # confirmed live 2026-07-14 (Coffee): "Read the guest book on the
    # landing table" didn't match the real stored name "a guestbook on
    # the landing table" at all -- both the leading "a" and the missing
    # space in "guestbook" were never how a player would naturally type
    # it, so the exact-substring check above failed even though this
    # was clearly the same object.
    # "old" added 2026-07-19 (same live "brass scale" bug as the
    # apostrophe fix above): "Old Maren's brass scale" is a 4-word name
    # where "old" is a throwaway descriptor, not a distinguishing word --
    # the exact same reasoning the NPC-name matcher's own
    # _NPC_NAME_FILLER_WORDS already applies to "old" for "Old Maren"
    # herself. Without excluding it, a shorter (but perfectly clear)
    # phrasing that skips "old" needs 3 of the remaining words just to
    # clear the same >half-the-words bar a distinctive object should
    # pass easily.
    stopwords = {"a", "an", "the", "of", "at", "on", "in", "to", "old"}
    for obj_id, data in ordered:
        name = data["name"].lower()
        for article in ("a ", "an ", "the "):
            if name.startswith(article):
                name = name[len(article):]
                break
        if name in lowered or name.replace(" ", "") in lowered.replace(" ", ""):
            return obj_id, data

    # Fallback 2: majority word-overlap -- confirmed live the same day:
    # "look at the door down the hall" didn't match the real "the door
    # at the end of the hall" at all (genuinely different wording, not
    # just spacing/articles), even though it's clearly the same door.
    # Picks whichever interactable's significant words (stopwords/short
    # words excluded) overlap the input the most, but only when there's
    # a single clear leader with OVER HALF its words present -- a weak
    # or tied overlap is left unmatched rather than guessed, same
    # "don't guess when ambiguous" philosophy as every other word-level
    # fallback in this codebase (NPC names, location names).
    candidate_words: dict = {}
    for obj_id, data in ordered:
        # Real bug, live-caught 2026-07-19 (Coffee: "a player is looking
        # at the wide-boled tree and its not working"): .split() left
        # punctuation stuck to whichever word precedes it in the stored
        # name (e.g. "an ancient, wide-boled tree" -> "ancient," with
        # the comma attached) -- \bword\b then could never match, since
        # a trailing comma-to-space transition has no word boundary at
        # all. That silently dropped "ancient" from the overlap count
        # entirely, so even "examine the ancient tree" (a perfectly
        # normal shortening, zero typos) fell one word short of the
        # >half threshold and matched nothing.
        # Real live bug (2026-07-19, same day as the "wide-boled tree"
        # fix above, same root shape but the opposite direction): "a
        # boarded up stall" (no hyphen, the natural way to type it)
        # never matched the stored "a boarded-up stall" -- the comma-
        # stripping fix above only ever handled a stray TRAILING
        # punctuation mark, not a hyphen genuinely joining two words
        # into one token, so "boarded-up" stayed a single unsplittable
        # word no plain space-separated phrasing could ever equal.
        # Splitting on hyphens the same as whitespace before stripping
        # trailing punctuation means "boarded-up" contributes "boarded"
        # to the overlap count same as if the stored name used a space.
        words = [
            w.strip(".,;:!?\"'()") for w in data["name"].lower().replace("-", " ").split()
        ]
        words = [w for w in words if w and w not in stopwords and len(w) >= 3]
        if words:
            candidate_words[obj_id] = (data, words)

    # Real live bug (2026-07-19, same "old strongbox" session): "Old
    # Maren's locked strongbox" (3 significant words after "old" is
    # excluded) needed 2 of them to clear the plain >half threshold
    # below, but "strongbox" -- the one word that actually NAMES the
    # object, a real head noun -- should be a confident match on its
    # own, the same distinction items.find_item_mentioned_in_text
    # already draws between a head-noun match and a mere modifier.
    # Checked as its own pass, before the general overlap threshold, so
    # it can only ever ADD a match a shorter phrasing would otherwise
    # miss, never take one away.
    # Real live bug (2026-07-22, Coffee, escalated dev-topic report --
    # "the third response now I have not gotten a reply"): "Take the
    # scrap of paper off the corkboard" never matched "the corkboard by
    # the door" -- the head-word check above only ever tried the LAST
    # significant word ("door", a generic location descriptor here, not
    # the actual object), so a name shaped "OBJECT by/behind/under the
    # LOCATION" (this campaign has several: "the corkboard by the
    # door", "the heavy ledger behind the bar") could never head-match
    # on the word that actually names the object. Generalized to try
    # EVERY one of a candidate's significant words, not just the last
    # -- a word only counts if it's unique to exactly one candidate at
    # this location (never ambiguously shared by two), same "don't
    # guess when ambiguous" discipline as the rest of this function.
    head_matches = []
    for obj_id, (data, words) in candidate_words.items():
        for word in words:
            if not re.search(r"\b" + re.escape(word) + r"\b", lowered):
                continue
            shared = any(
                other_id != obj_id and word in other_words
                for other_id, (_, other_words) in candidate_words.items()
            )
            if not shared:
                head_matches.append((obj_id, data))
            break
    if len(head_matches) == 1:
        return head_matches[0]

    best_obj, best_score, ambiguous = None, 0, False
    for obj_id, (data, words) in candidate_words.items():
        matches = sum(1 for w in words if re.search(r"\b" + re.escape(w) + r"\b", lowered))
        if matches > len(words) / 2:
            if matches > best_score:
                best_obj, best_score, ambiguous = (obj_id, data), matches, False
            elif matches == best_score:
                ambiguous = True
    if best_obj and not ambiguous:
        return best_obj

    return None


def _plural_forms(word: str) -> list[str]:
    """
    Cheap English pluralization for matching a monster's singular
    template name against a player's naturally-plural phrasing ("the
    wolves") -- confirmed live 2026-07-17 (Coffee): a plain substring
    check against "Wolf" never matches "wolves" at all (not a simple
    "+s" plural), so the very first live use of this monster-matching
    fix silently failed the exact case it was built for. Covers regular
    "+s"/"+es" and the common f/fe -> ves irregular (wolf -> wolves).
    """
    forms = [word, word + "s", word + "es"]
    if word.endswith("f"):
        forms.append(word[:-1] + "ves")
    elif word.endswith("fe"):
        forms.append(word[:-2] + "ves")
    return forms


def _find_monster_mentioned_in_text(location: dict, text: str) -> tuple[str, dict] | None:
    """
    Matches a real monster type actually present at this location
    (location["monsters"], a list of monster_keys) against free text --
    same real-substring-match philosophy as _find_interactable, just
    for a different real fact source (task from Coffee, 2026-07-17).
    """
    lowered = text.strip().lower()
    monster_keys = location.get("monsters", [])
    templates = [(key, cl.get_monster_template(CAMPAIGN, key)) for key in monster_keys]
    templates = [(key, t) for key, t in templates if t is not None]
    templates.sort(key=lambda kt: -len(kt[1]["name"]))
    for monster_key, template in templates:
        name = template["name"].lower()
        candidates = _plural_forms(name) + _plural_forms(monster_key.replace("_", " "))
        if any(re.search(r"\b" + re.escape(c) + r"\b", lowered) for c in candidates):
            return monster_key, template
    return None


async def _do_examine(update: Update, target_text: str) -> None:
    character = db.get_character(update.effective_user.id)
    if character is None:
        await update.effective_chat.send_message(
            "You don't have a character yet!", message_thread_id=config.TOPIC_ADVENTURE_ID
        )
        return
    location = cl.get_location(CAMPAIGN, character["current_location"])
    if location is None:
        await update.effective_chat.send_message(
            f"**{character['name']}** seems to be nowhere in particular. That's... concerning.",
            message_thread_id=config.TOPIC_ADVENTURE_ID,
        )
        return

    # Dev-topic feedback (Coffee, 2026-07-19): "If a player wants to look
    # at stalls and shopfronts in market row - please give a description
    # and then open the shop." A GENERIC "the stalls"/"the shopfronts"
    # phrase shouldn't be forced onto one specific named interactable
    # (market_row's own "shuttered_stall" is deliberately the one
    # abandoned, nailed-shut stall -- examining that specifically should
    # still just describe it, not open Maren's shop), so this only
    # matches the generic plural/collective wording, checked before the
    # normal interactable lookup.
    generic_shop_words = {"stall", "stalls", "shopfront", "shopfronts", "store", "stores", "shop", "shops"}
    shop_id = location.get("shop")
    if shop_id and target_text:
        lowered_target = target_text.lower()
        target_words = set(re.findall(r"[a-z]+", lowered_target))
        if target_words & generic_shop_words and "shuttered" not in lowered_target:
            await _safe_send(
                update,
                f"🔍 **{character['name']}** takes in the stalls and shopfronts lining {location['name']} "
                f"before stepping up to see what's actually for sale.",
            )
            await _do_list_shop(update)
            return

    interactables = location.get("interactables", {})
    if not target_text or not target_text.strip():
        if interactables:
            names = [i["name"] for i in interactables.values()]
            await update.effective_chat.send_message(
                f"🔍 **{character['name']}**, examine what, exactly? Things worth a closer look here: {', '.join(names)}",
                message_thread_id=config.TOPIC_ADVENTURE_ID,
            )
        else:
            await update.effective_chat.send_message(
                f"🔍 **{character['name']}** finds nothing here that catches the eye for a closer look.",
                message_thread_id=config.TOPIC_ADVENTURE_ID,
            )
        return

    found = _find_interactable(location, target_text)
    if found is None:
        # Real bug, caught live 2026-07-17 (Coffee): "Look at the wolves
        # in the whispering wood - give me detail about them" got "Ravenloft
        # doesn't spot anything like that here" even though wolves are a
        # real, active threat at this exact location (location["monsters"]
        # and the "Trouble with Wolves" board quest both confirm it) --
        # examine only ever checked interactables, never real monsters
        # present. Fixed: match against location["monsters"] too, and
        # give real bestiary stats if already fought, or an honest
        # "here, but not yet known" line if not -- same fog-of-war
        # boundary the bestiary itself already keeps, never inventing
        # stats the player hasn't actually earned by fighting it.
        monster_match = _find_monster_mentioned_in_text(location, target_text)
        if monster_match:
            monster_key, template = monster_match
            if monster_key in (character.get("known_monsters") or []):
                await _safe_send(
                    update,
                    f"🔍 **{character['name']}** studies the {template['name'].lower()} here, "
                    f"drawing on what they already know:\n{_format_bestiary_entry(monster_key, template)}",
                )
            else:
                await _safe_send(
                    update,
                    f"🔍 **{character['name']}** spots a real threat here — a {template['name'].lower()} — "
                    f"but hasn't fought one yet to know more. Check the bestiary once you have.",
                )
            return

        names = [i["name"] for i in interactables.values()]
        hint = f" Things worth a closer look here: {', '.join(names)}" if names else ""
        await update.effective_chat.send_message(
            f"🔍 **{character['name']}** doesn't spot anything like that here.{hint}",
            message_thread_id=config.TOPIC_ADVENTURE_ID,
        )
        return

    _, obj_data = found
    narration = await asyncio.to_thread(
        narrate_examine, character, location["name"], obj_data["name"], obj_data["description"]
    )
    await _safe_send(update, f"🔍 **{character['name']}** examines {obj_data['name']}: {narration}")


async def _do_show_map(update: Update) -> None:
    """
    Renders a fog-of-war map: only locations this character has actually
    visited (character['visited_locations'], persisted in the DB) are
    shown by name; connections leading to somewhere unvisited are shown
    as an unexplored direction rather than revealing the destination.

    Task #141: map_revealed_locations (a real location NAME purchased/
    found via a "map" item, see _do_use_item) is a genuinely separate,
    weaker fog-of-war layer -- rendered with its own marker and NO
    connection info at all, since connections are the actual spoiler a
    revealed-but-unvisited location must never leak. Never merged into
    visited_locations itself; a revealed location still needs to be
    physically visited to unlock its real connections here.
    """
    character = db.get_character(update.effective_user.id)
    if character is None:
        await update.effective_chat.send_message(
            "You don't have a character yet!", message_thread_id=config.TOPIC_ADVENTURE_ID
        )
        return

    visited = set(character["visited_locations"])
    revealed = set(character["map_revealed_locations"]) - visited
    if not visited and not revealed:
        await update.effective_chat.send_message(
            "You haven't explored anywhere yet.", message_thread_id=config.TOPIC_ADVENTURE_ID
        )
        return

    # Real dev-topic confusion (2026-07-22, Coffee: "?!! What is this")
    # -- the map's own notation (📍/•/→/"N unexplored path(s)") was
    # never actually explained anywhere, just used. Not a bug (the data
    # itself was always correct), but a real, fixable UX gap -- a one-
    # line legend costs nothing and removes the ambiguity outright.
    lines = [
        "🗺️ **Your map** (📍 = where you are now, • = a place you've "
        "visited, → its known connections, \"N unexplored path(s)\" = "
        "routes leading somewhere you haven't been yet)",
    ]
    for layer, layer_locations in CAMPAIGN["locations"].items():
        visited_here = [loc_id for loc_id in layer_locations if loc_id in visited]
        revealed_here = [loc_id for loc_id in layer_locations if loc_id in revealed]
        if not visited_here and not revealed_here:
            continue
        lines.append(f"\n**{layer.title()}**")
        for loc_id in visited_here:
            loc = layer_locations[loc_id]
            marker = "📍" if loc_id == character["current_location"] else "•"
            connections = loc.get("connections", [])
            known = [cl.get_location(CAMPAIGN, c)["name"] for c in connections if c in visited]
            unknown_count = sum(1 for c in connections if c not in visited)
            conn_bits = list(known)
            if unknown_count:
                conn_bits.append(f"{unknown_count} unexplored path{'s' if unknown_count != 1 else ''}")
            conn_text = f" → {', '.join(conn_bits)}" if conn_bits else ""
            lines.append(f"{marker} {loc['name']}{conn_text}")
        for loc_id in revealed_here:
            lines.append(f"❓ {layer_locations[loc_id]['name']} *(marked on a map, not yet visited)*")

    await _safe_send(update, "\n".join(lines), speak=False)


async def _do_show_visual_map(update: Update) -> None:
    """
    Task #221, per Coffee: "design a visual/graphical fog-of-war map
    (image, not just text)." Real, but honestly scoped: Pollinations.ai
    (task #81's image service) is a generative model, not a cartography
    engine -- it can't take this game's real location/connection graph
    and render an accurate schematic the way _do_show_map's text output
    does, and asking any current image model to render legible text
    labels reliably fails. So this generates real atmospheric map ART,
    grounded ONLY in the real names of locations this character has
    actually visited (the exact same fog-of-war set _do_show_map reads,
    never anything undiscovered) -- a genuine visual complement to the
    precise text map, not a replacement for it.
    """
    character = db.get_character(update.effective_user.id)
    if character is None:
        await update.effective_chat.send_message(
            "You don't have a character yet!", message_thread_id=config.TOPIC_ADVENTURE_ID
        )
        return
    visited = character.get("visited_locations") or []
    if not visited:
        await update.effective_chat.send_message(
            "You haven't explored anywhere yet.", message_thread_id=config.TOPIC_ADVENTURE_ID
        )
        return
    names = [cl.get_location(CAMPAIGN, loc_id)["name"] for loc_id in visited if cl.get_location(CAMPAIGN, loc_id)]
    prompt = (
        "an old hand-drawn fantasy world map on weathered parchment, aged ink and watercolor "
        f"illustration style, depicting these named regions: {', '.join(names)}, "
        "no readable text or labels, cartography art"
    )
    try:
        await update.effective_chat.send_photo(
            photo=images_module.generate_image_url(prompt, width=768, height=768),
            caption="🗺️ Your explored world, so far.",
            message_thread_id=config.TOPIC_ADVENTURE_ID,
        )
    except Exception as e:
        logger.warning(f"[images] visual map generation failed: {e!r}")
        await update.effective_chat.send_message(
            "Couldn't generate a map image right now — try again in a bit.",
            message_thread_id=config.TOPIC_ADVENTURE_ID,
        )


def _format_bestiary_entry(monster_key: str, template: dict) -> str:
    tags = []
    if template.get("is_boss"):
        tags.append("boss")
    if template.get("on_hit_condition"):
        tags.append(f"inflicts {template['on_hit_condition']} on hit")
    if template.get("life_drain"):
        tags.append("drains life on hit")
    tag_text = f" ({', '.join(tags)})" if tags else ""
    return (
        f"**{template['name']}**{tag_text}\n"
        f"  HP {template['hp_max']} | AC {template['armor_class']} | "
        f"STR {template['strength']} DEX {template['dexterity']} | "
        f"XP {template.get('xp_reward', 0)}"
    )


async def _do_bestiary(update: Update) -> None:
    """
    Bestiary / monster compendium (2026-07-16, per Coffee's backlog):
    fog-of-war discovery, same convention as the map above -- only
    monster types this character has actually fought (known_monsters,
    see db.mark_known_monster, set from _do_start_combat) are shown,
    with their REAL stats pulled straight from the campaign's monster
    templates, not invented flavor text.
    """
    character = db.get_character(update.effective_user.id)
    if character is None:
        await update.effective_chat.send_message(
            "You don't have a character yet!", message_thread_id=config.TOPIC_ADVENTURE_ID
        )
        return

    known = character.get("known_monsters") or []
    if not known:
        await update.effective_chat.send_message(
            "Your bestiary is empty — fight something to start learning about it.",
            message_thread_id=config.TOPIC_ADVENTURE_ID,
        )
        return

    lines = ["📖 **Bestiary**"]
    for monster_key in known:
        template = cl.get_monster_template(CAMPAIGN, monster_key)
        if template is None:
            continue
        lines.append(_format_bestiary_entry(monster_key, template))

    await _safe_send(update, "\n".join(lines), speak=False)


def _achievement_condition_met(character: dict, check: dict) -> bool:
    """
    Titles & Achievements (task #73): every check type here reads a
    real, already-existing field on the character dict -- no new
    counter was invented just to support this feature. See
    achievements.py's module docstring for the full list of types.
    """
    check_type = check["type"]
    if check_type == "min_level":
        return character.get("level", 1) >= check["value"]
    if check_type == "min_gold":
        return character.get("gold", 0) >= check["value"]
    if check_type == "min_known_monsters":
        return len(character.get("known_monsters") or []) >= check["value"]
    if check_type == "min_completed_quests":
        return len(character.get("completed_quests") or []) >= check["value"]
    if check_type == "min_board_quests_completed":
        return character.get("board_quests_completed", 0) >= check["value"]
    if check_type == "min_rebirth_count":
        return character.get("rebirth_count", 0) >= check["value"]
    if check_type == "has_guild":
        return bool(character.get("guild"))
    if check_type == "well_equipped":
        return bool(character.get("equipped_weapon")) and bool(
            character.get("equipped_armor") or character.get("equipped_shield")
        )
    if check_type == "hidden_synergy":
        # Task #134: real, undocumented emergent-build achievements --
        # a genuine multi-system combo (alignment extreme + a real
        # skill-tree investment + an existing level milestone), never
        # hinted at in any visible text.
        good_evil = character.get("alignment_good_evil", 0)
        has_upgrade = bool(character.get("skill_tree_upgrades"))
        is_legendary = "legendary" in (character.get("achievements") or [])
        if check["variant"] == "elect":
            return good_evil >= 60 and has_upgrade and is_legendary
        if check["variant"] == "damned":
            return good_evil <= -60 and has_upgrade and is_legendary
        return False
    return False


# Login streak rewards (task #78) -- days played in a row -> (gold, XP).
# Board quests already provide a real repeatable daily quest per
# location (see CLAUDE.md/campaign design), so this is deliberately
# scoped to just the streak-reward half of the original ask.
_STREAK_MILESTONES = {3: (30, 15), 7: (75, 40), 14: (150, 80), 30: (350, 200)}


async def _maybe_award_streak_bonus(update: Update, streak_days: int) -> None:
    reward = _STREAK_MILESTONES.get(streak_days)
    if reward is None:
        return
    gold, xp = reward
    telegram_user_id = update.effective_user.id
    character = db.get_character(telegram_user_id)
    if character is None:
        return
    db.update_character(telegram_user_id, gold=character["gold"] + gold)
    await _award_xp_and_announce_level_up(update, telegram_user_id, xp)
    await _safe_send(
        update,
        f"🔥 **{streak_days}-day login streak!** {character['name']} earns {gold} gold and {xp} XP "
        f"for playing {streak_days} days in a row.",
    )
    await _check_and_award_achievements(update, db.get_character(telegram_user_id))


async def _check_achievements_for_combat_party(update: Update, session: sessions.Session) -> None:
    """Re-fetches each real party-side combatant fresh from the DB (post-XP-award) and checks them."""
    for pid in session.turn_order:
        if session.sides.get(pid) != "party":
            continue
        character = db.get_character(pid)
        if character is not None:
            await _check_and_award_achievements(update, character)


async def _check_guild_quest_completion(update: Update, session: sessions.Session) -> None:
    """
    Guild quests (task #77): any real guild member wins their guild's
    quest for the day the moment they win ANY fight, same checkpoint as
    achievements. Announces into the guild's own real Telegram topic
    (config.GUILD_TOPIC_IDS), not Adventure -- that's the whole point of
    having a real guild-only channel. Silently no-ops for AI characters
    and anyone with no guild.
    """
    for pid in session.turn_order:
        if session.sides.get(pid) != "party":
            continue
        character = db.get_character(pid)
        if character is None or character.get("is_ai") or not character.get("guild"):
            continue
        quest = GUILD_QUESTS.get(character["guild"])
        if quest is None:
            continue
        claimed = db.claim_guild_quest_if_unclaimed_today(pid, quest["reward_gold"], quest["reward_xp"])
        if not claimed:
            continue
        topic_id = config.GUILD_TOPIC_IDS.get(character["guild"])
        if topic_id:
            await _safe_send(
                update,
                f"📜 **{quest['title']} complete!** {character['name']} earns "
                f"{quest['reward_gold']} gold and {quest['reward_xp']} XP for today.",
                thread_id=topic_id,
            )


async def _do_check_guild_quest(update: Update, guild_id: str) -> None:
    character = db.get_character(update.effective_user.id)
    if character is None:
        return
    quest = GUILD_QUESTS.get(guild_id)
    if quest is None:
        return
    today = datetime.now(timezone.utc).date().isoformat()
    claimed_today = character.get("last_guild_quest_date") == today
    status = "✅ already claimed today" if claimed_today else "not yet claimed today — win any fight to complete it"
    # Replies in whichever topic asked (Adventure or the guild's own
    # topic) -- only the completion announcement (_check_guild_quest_
    # completion) deliberately targets the guild topic specifically.
    await _safe_send(
        update,
        f"📜 **{quest['title']}**\n{quest['description']}\n"
        f"Reward: {quest['reward_gold']} gold, {quest['reward_xp']} XP\nStatus: {status}",
        thread_id=update.message.message_thread_id, speak=False,
    )


async def guild_topic_handler(update: Update, context: ContextTypes.DEFAULT_TYPE, guild_id: str) -> None:
    """
    Task #77's real guild-only channel: this topic exists in the group
    like any other, but the bot only ever engages with someone whose
    real character['guild'] matches -- a non-member typing here gets a
    clear, honest redirect instead of the bot pretending the topic is
    open to everyone. Ordinary chat between real members needs no
    processing at all (Telegram already delivers it); "check guild
    quest" is the one real command surfaced here.
    """
    character = db.get_character(update.effective_user.id)
    guild = GUILDS.get(guild_id)
    if character is None or character.get("guild") != guild_id:
        await update.effective_chat.send_message(
            f"This topic is for real {guild['name']} members only — join in Adventure first "
            f"(\"join {guild['name']}\") if you're eligible.",
            message_thread_id=update.message.message_thread_id,
        )
        return

    lowered = update.message.text.lower()
    if any(w in lowered for w in ["guild quest", "check quest", "today's quest", "todays quest"]):
        await _do_check_guild_quest(update, guild_id)


async def _check_and_award_achievements(update: Update, character: dict | None) -> None:
    """
    Called at natural real-progress checkpoints (combat victory, quest
    turn-in, level-up, guild join, equip) -- never on a timer or a
    generic per-message hook, so it only ever fires right when a real
    condition could have just become true. Silently no-ops for AI
    characters (companions/autonomous party) -- achievements are a
    real-player feature. character may be None (a caller re-fetching
    after a mutation found nothing) -- also a silent no-op.
    """
    if character is None or character.get("is_ai"):
        return
    already = set(character.get("achievements") or [])
    for achievement_id, data in achievements_module.ACHIEVEMENTS.items():
        if achievement_id in already:
            continue
        if _achievement_condition_met(character, data["check"]):
            db.unlock_achievement(character["telegram_user_id"], achievement_id)
            await _safe_send(
                update,
                f"🏅 **{character['name']}** earns achievement: **{data['name']}**\n{data['description']}\n"
                f"Title earned: \"{data['title']}\" — say \"set my title to {data['title']}\" to wear it.",
            )


async def _do_check_achievements(update: Update) -> None:
    character = db.get_character(update.effective_user.id)
    if character is None:
        await _safe_send(update, "You don't have a character yet!", speak=False)
        return
    unlocked = character.get("achievements") or []
    if not unlocked:
        await _safe_send(
            update,
            "No achievements unlocked yet. Say \"check achievements\" any time to see your progress "
            "as you play — nothing's spoiled here, they unlock naturally as you go.",
            speak=False,
        )
        return
    lines = ["🏅 **Your achievements:**"]
    for achievement_id in unlocked:
        data = achievements_module.get_achievement(achievement_id)
        if data is None:
            continue
        marker = " *(active title)*" if character.get("active_title") == data["title"] else ""
        lines.append(f"• **{data['name']}** — \"{data['title']}\"{marker}")
    if character.get("active_title"):
        lines.append(f"\nCurrent title: \"{character['active_title']}\"")
    else:
        lines.append("\nNo title set — say \"set my title to <title>\" to wear one you've earned.")
    await _safe_send(update, "\n".join(lines), reply_markup=_title_keyboard(character, unlocked), speak=False)


def _title_keyboard(character: dict, unlocked: list[str]) -> InlineKeyboardMarkup | None:
    """
    Real dev-topic feedback (2026-07-22, Sugar: "I earned the title and
    want to set my title") -- setting a title required typing its exact
    text verbatim with no way to just pick from what's actually earned.
    One tappable button per real unlocked achievement's title, same
    "reuse the real handler, never a separate path" convention as every
    other push-button menu this session.
    """
    buttons = []
    for achievement_id in unlocked:
        data = achievements_module.get_achievement(achievement_id)
        if data is None or data["title"] == character.get("active_title"):
            continue
        buttons.append([InlineKeyboardButton(f"🏅 {data['title']}", callback_data=f"title|set|{achievement_id}")])
    return InlineKeyboardMarkup(buttons) if buttons else None


async def title_menu_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handles taps on _title_keyboard -- dispatches through the same real _do_set_title free text already uses."""
    query = update.callback_query
    parts = (query.data or "").split("|")
    action = parts[1] if len(parts) > 1 else ""
    await _safe_answer(query)
    if action != "set" or len(parts) < 3:
        return
    data = achievements_module.get_achievement(parts[2])
    if data is None:
        return
    await _do_set_title(update, data["title"])


_INLINE_TITLE_RE = re.compile(r"title\s+to\s+(.+)|title[^:]*:\s*(.+)", re.IGNORECASE)


def _extract_inline_title(text: str) -> str | None:
    """
    Handles "set my title to the Battle-Tested" and "title: the Wealthy".
    "clear"/"remove my title" phrasing is recognized directly so it
    reaches _do_set_title's real clear branch rather than being
    misread as a title with no content.
    """
    lowered = text.lower()
    if any(w in lowered for w in ["clear my title", "remove my title", "no title"]):
        return "clear"
    match = _INLINE_TITLE_RE.search(text)
    if match:
        candidate = (match.group(1) or match.group(2) or "").strip()
        return candidate or None
    return None


async def _do_set_title(update: Update, title_text: str) -> None:
    user_id = update.effective_user.id
    character = db.get_character(user_id)
    if character is None:
        await _safe_send(update, "You don't have a character yet!")
        return

    clean = title_text.strip().strip("\"")
    if not clean or clean.lower() in ("clear", "none", "remove"):
        db.set_active_title(user_id, None)
        await _safe_send(update, "Title cleared.")
        return

    unlocked = character.get("achievements") or []
    earned_titles = {
        achievements_module.get_achievement(a)["title"].lower()
        for a in unlocked
        if achievements_module.get_achievement(a)
    }
    if clean.lower() not in earned_titles:
        await _safe_send(
            update,
            f"You haven't earned the title \"{clean}\" yet. Say \"check achievements\" to see "
            "which titles you've unlocked.",
        )
        return

    # Store the title with its real canonical casing, not whatever
    # casing the player happened to type.
    canonical = next(
        achievements_module.get_achievement(a)["title"]
        for a in unlocked
        if achievements_module.get_achievement(a) and achievements_module.get_achievement(a)["title"].lower() == clean.lower()
    )
    db.set_active_title(user_id, canonical)
    await _safe_send(update, f"✅ Title set: \"{canonical}\"")


async def _do_leaderboard(update: Update) -> None:
    """
    Hall of Fame (2026-07-17, per Coffee, task #74): real XP ranking
    across every player's currently-active character -- see
    db.get_leaderboard's own docstring for why is_autonomous (the
    AI-played party) counts the same as anyone else, but is_ai=1
    (combat-only companions) doesn't.
    """
    ranked = db.get_leaderboard(limit=10)
    if not ranked:
        await update.effective_chat.send_message(
            "Nobody's made it onto the board yet.", message_thread_id=config.TOPIC_ADVENTURE_ID
        )
        return

    medals = {0: "🥇", 1: "🥈", 2: "🥉"}
    lines = ["🏆 **Hall of Fame**"]
    for i, character in enumerate(ranked):
        rank_marker = medals.get(i, f"{i + 1}.")
        title_suffix = f" \"{character['active_title']}\"" if character.get("active_title") else ""
        lines.append(
            f"{rank_marker} **{character['name']}**{title_suffix} — Level {character['level']} "
            f"{character['race']} {character['char_class']} ({character['xp']} XP)"
        )

    await _safe_send(update, "\n".join(lines), speak=False)


def _find_location_by_name_fragment(fragment: str) -> str | None:
    lowered = fragment.strip().lower()
    for loc_id in cl.get_all_location_ids(CAMPAIGN):
        loc = cl.get_location(CAMPAIGN, loc_id)
        if lowered in loc_id.lower() or lowered in loc["name"].lower():
            return loc_id
    return None


async def _do_move(update: Update, text: str) -> None:
    character = db.get_character(update.effective_user.id)
    if character is None:
        await update.effective_chat.send_message(
            "You don't have a character yet!", message_thread_id=config.TOPIC_ADVENTURE_ID
        )
        return

    current = cl.get_location(CAMPAIGN, character["current_location"])
    reachable = list(current.get("connections", []))
    if "descends_to" in current:
        reachable.append(current["descends_to"])
    if "ascends_to" in current:
        reachable.append(current["ascends_to"])
    # Locked-door destinations (see locked_connections) are real,
    # nameable travel targets too — just blocked until picked. Without
    # this they're silently invisible to move, indistinguishable from a
    # location with no connection at all.
    reachable.extend(
        loc_id for loc_id in current.get("locked_connections", {}) if loc_id not in reachable
    )

    destination_id = None
    lowered = text.lower()

    # Compass navigation (world-expansion pass, 2026-07-19): "directions"
    # is an optional per-location dict (compass word -> location id) that
    # sits alongside "connections" -- checked first since it's unambiguous
    # by construction (one destination per word), before the name-matching
    # below that's still how a player naming a destination directly (or any
    # pre-expansion location with no directions at all) always worked.
    direction_words = current.get("directions", {})
    if direction_words:
        words_in_text = set(re.findall(r"[a-z]+", lowered))
        for word, loc_id in direction_words.items():
            if word in words_in_text and loc_id in reachable:
                destination_id = loc_id
                break

    for loc_id in reachable:
        if destination_id is not None:
            break
        loc = cl.get_location(CAMPAIGN, loc_id)
        if loc_id.replace("_", " ") in lowered or loc["name"].lower() in lowered:
            destination_id = loc_id
            break

    if destination_id is None:
        # Word-level fallback for multi-word location names -- confirmed
        # live 2026-07-12: "Go to the cellar" never matched "The Tavern
        # Cellar" at all, since neither the full name nor the location
        # id appears verbatim in that phrasing (same root cause/fix
        # shape as the earlier NPC full-name-only matching bug). Only
        # accepts a single word when it's unique among the destinations
        # actually reachable right now -- e.g. "cellar" uniquely picks
        # "The Tavern Cellar" even though "tavern" alone would be
        # ambiguous between it and "The Tavern's Upper Rooms", so an
        # ambiguous shared word is deliberately left unmatched rather
        # than guessed.
        stopwords = {"the", "a", "of", "in", "at", "on"}
        word_sets = {}
        for loc_id in reachable:
            name = cl.get_location(CAMPAIGN, loc_id)["name"]
            words = {w.strip("'s").lower() for w in name.replace("'", " ").split()}
            word_sets[loc_id] = {w for w in words if w and w not in stopwords and len(w) >= 3}
        for loc_id, words in word_sets.items():
            for word in words:
                unique = all(word not in other for other_id, other in word_sets.items() if other_id != loc_id)
                if unique and word in lowered:
                    destination_id = loc_id
                    break
            if destination_id:
                break

    # Generic "leave"/"go back"/"exit" phrasing names no specific
    # destination at all -- confirmed live 2026-07-12: a real player
    # saying "Leave the tavern cellar and go back upstairs" named
    # neither "crossroads tavern" nor "The Crossroads Tavern" (the only
    # real connection out of the cellar), so the name-matching loop
    # above never found it, leaving them stuck. Six real locations in
    # this campaign (tavern_cellar, tavern_upstairs, the_arcane_nook,
    # hollow_stump_shrine, goblin_warrens, the_first_city) are exactly
    # this kind of dead end with a SINGLE connection back out -- when
    # that's true, "leave"/"go back"/"exit"/"upstairs"/"outside" is
    # unambiguous regardless of exact wording, so resolve it directly
    # rather than requiring the destination's literal name. Left
    # multi-exit locations alone since guessing which way "leave" means
    # there would be wrong as often as right.
    generic_leave_words = ["leave", "exit", "go back", "head back", "back upstairs",
                            "back outside", "back out", "step out", "walk out", "get out",
                            "upstairs", "downstairs", "outside"]
    if destination_id is None and len(reachable) == 1 and any(w in lowered for w in generic_leave_words):
        destination_id = reachable[0]

    if destination_id is None:
        # A location is never its own connection, so "go to X" while
        # already AT X always fell through to the generic can't-get-
        # there message below — confirmed live 2026-07-11 as the actual
        # cause of an AI party member repeatedly spamming that message
        # in Adventure (it kept trying to "head to" its own current
        # location). Worth its own clearer reply for human players too.
        if character["current_location"].replace("_", " ") in lowered or current["name"].lower() in lowered:
            await update.effective_chat.send_message(
                f"You're already at {current['name']}.", message_thread_id=config.TOPIC_ADVENTURE_ID
            )
            return
        reachable_names = ", ".join(cl.get_location(CAMPAIGN, r)["name"] for r in reachable)
        await update.effective_chat.send_message(
            f"**{character['name']}** can't get there directly from {current['name']}. "
            f"From here you can reach: {reachable_names}",
            message_thread_id=config.TOPIC_ADVENTURE_ID,
        )
        return

    destination = cl.get_location(CAMPAIGN, destination_id)
    if destination.get("requires_item") and destination["requires_item"] not in character["inventory"]:
        await update.effective_chat.send_message(
            f"Something stops **{character['name']}** from going any further — missing something needed first.",
            message_thread_id=config.TOPIC_ADVENTURE_ID,
        )
        return

    if not _meets_location_level(character, destination):
        await _send_level_gate_message(update, destination)
        return

    locked_connections = current.get("locked_connections", {})
    lockable_id = locked_connections.get(destination_id)
    if lockable_id and lockable_id not in _UNLOCKED:
        lockable = next(
            (lk for lk in current.get("lockables", []) if lk["id"] == lockable_id), None
        )
        name = lockable["name"] if lockable else "something locked"
        await update.effective_chat.send_message(
            f"The way to {destination['name']} is blocked by {name}. Try picking the lock first.",
            message_thread_id=config.TOPIC_ADVENTURE_ID,
        )
        return

    story_gate_message = _check_story_gate(character, current, destination_id)
    if story_gate_message:
        await update.effective_chat.send_message(story_gate_message, message_thread_id=config.TOPIC_ADVENTURE_ID)
        return

    destination_already_visited = destination_id in (character.get("visited_locations") or [])
    db.move_character(update.effective_user.id, destination_id)
    db.mark_visited(update.effective_user.id, destination_id)

    # Real bug found live (2026-07-17, Coffee): recruited companions
    # (is_ai=1, is_autonomous=0) are supposed to be traveling WITH
    # whoever recruited them, but this only ever moved the acting
    # player -- companions just stood still forever. Only moves real
    # recruited companions sharing this player's party_id; the
    # hardcoded autonomous AI-played party (is_autonomous=1) roams and
    # acts entirely on its own and must never be dragged around by a
    # human's movement.
    if character.get("party_id"):
        for member in db.get_party_members_by_id(character["party_id"]):
            if member.get("is_ai") and not member.get("is_autonomous") and member["telegram_user_id"] != update.effective_user.id:
                db.move_character(member["telegram_user_id"], destination_id)

    # Per Coffee (2026-07-14): TTS coverage audit -- _do_move's primary
    # reply never went through _safe_send, so this (one of the most
    # common actions in the game) always silently skipped TTS.
    await _safe_send(update, f"🚶 **{character['name']}** travels to **{destination['name']}**.\n{destination['description']}")
    await _maybe_send_location_image(update, destination, destination_id, destination_already_visited)

    updated_character = db.get_character(update.effective_user.id)
    await _maybe_trigger_npc_encounter(update, updated_character, destination)
    await _check_quest_completions_reach_location(update, update.effective_user.id, destination_id)
    await _check_board_quest_turnin(update, update.effective_user.id, destination_id)


def _npc_id_for_companion_name(name: str) -> str | None:
    """Reverse-looks-up a recruited companion's campaign.json npc_id by name -- companion character rows never store it directly (matched by name at recruit time too, see _do_recruit_npc)."""
    for npc_id, data in CAMPAIGN["npcs"].items():
        if data["name"] == name:
            return npc_id
    return None


def _check_story_gate(character: dict, current: dict, destination_id: str) -> str | None:
    """
    Full-storyline plan, Phase 1: a third gate type on a location
    connection, alongside min_level and locked_connections. Checked the
    same way locked_connections is -- read off the CURRENT location's
    story_gates dict, keyed by destination id. Returns an in-fiction
    rejection message if blocked, or None if the gate passes (or there's
    no story_gates entry at all for this connection). Both conditions,
    when present, must be met -- this reuses completed_quests (no new
    "defeated" flag) and the existing npc_relationships.affinity column
    (no new trust system).
    """
    gate = current.get("story_gates", {}).get(destination_id)
    if not gate:
        return None

    monster_id = gate.get("requires_defeated_monster")
    if monster_id:
        defeating_quest_ids = {
            quest_id for quest_id, quest in CAMPAIGN["quests"].items()
            if quest.get("trigger", {}).get("type") == "defeat_monster"
            and quest["trigger"].get("monster") == monster_id
        }
        if not defeating_quest_ids & set(character["completed_quests"]):
            return (
                "Something down here isn't done with you yet — you can feel "
                "it's not safe to go any further until whatever's wrong is dealt with."
            )

    trust_gate = gate.get("requires_companion_trust")
    if trust_gate:
        min_affinity = trust_gate.get("min_affinity", 0)
        companion_npc_ids = []
        party_id = character.get("party_id")
        if party_id:
            for member in db.get_party_members_by_id(party_id):
                if member.get("is_ai"):
                    npc_id = _npc_id_for_companion_name(member["name"])
                    if npc_id:
                        companion_npc_ids.append(npc_id)
        trusted = any(
            db.get_relationship(character["telegram_user_id"], npc_id)["affinity"] >= min_affinity
            for npc_id in companion_npc_ids
        )
        if not trusted:
            return (
                "None of your companions are ready to go any further — whatever "
                "waits ahead, they need to trust this path (and you) more first."
            )

    return None


def _meets_location_level(character: dict, destination: dict) -> bool:
    """
    Real level-gating for the campaign's deepest, hidden endgame locations
    (currently the_hush_below and the_first_city -- confirmed via
    campaign.json these are the only two locations already marked
    "hidden": true, which was never actually enforced anywhere in code
    before this; the game's design clearly intended them as special/deep
    content, this just wires that intent up). Coffee asked directly: a
    level-1 character shouldn't be able to reach endgame content. Any
    location with no "min_level" set (i.e. everything else in the
    campaign) is unaffected.
    """
    min_level = destination.get("min_level")
    return min_level is None or character.get("level", 1) >= min_level


async def _send_level_gate_message(update: Update, destination: dict) -> None:
    await update.effective_chat.send_message(
        f"A deep, wordless dread stops you at the threshold of **{destination['name']}** — "
        f"whatever waits there, you can feel you're not ready for it yet "
        f"(recommended level {destination['min_level']}+).",
        message_thread_id=config.TOPIC_ADVENTURE_ID,
    )


async def _do_fast_travel(update: Update, text: str) -> None:
    """
    Warp directly to any location this character has already visited
    (character['visited_locations'], the same fog-of-war data driving
    _do_show_map) — skipping the walk and its connection-graph
    constraints entirely. Ordinary _do_move (on-foot, connection-by-
    connection, fully narrated) is untouched; this is purely a
    convenience for places already discovered the hard way.
    """
    telegram_user_id = update.effective_user.id
    character = db.get_character(telegram_user_id)
    if character is None:
        await update.effective_chat.send_message(
            "You don't have a character yet!", message_thread_id=config.TOPIC_ADVENTURE_ID
        )
        return

    # Real live bug (2026-07-22, Sugar: "I'm not in battle why can't I
    # travel?"): this only ever checked whether ANY combat session
    # existed anywhere in the shared chat -- since sessions.py's
    # _ACTIVE_SESSIONS is one Session per chat_id (this whole game
    # shares a single Adventure chat), a fight happening to a DIFFERENT
    # character entirely (confirmed live: Sarah fighting a Giant Spider
    # while Sugar's own character was elsewhere, uninvolved) blocked
    # every other player in the game from fast-traveling too. Now only
    # blocks a character who's actually a real participant in that
    # session -- someone else's fight elsewhere no longer stops you.
    active_session = sessions.get_session(update.effective_chat.id)
    if active_session is not None and telegram_user_id in active_session.turn_order:
        await update.effective_chat.send_message(
            "You can't fast-travel in the middle of combat.",
            message_thread_id=config.TOPIC_ADVENTURE_ID,
        )
        return

    visited = character["visited_locations"]
    lowered = text.lower()
    destination_id = None
    for loc_id in visited:
        loc = cl.get_location(CAMPAIGN, loc_id)
        if loc and (loc_id.replace("_", " ") in lowered or loc["name"].lower() in lowered):
            destination_id = loc_id
            break

    if destination_id is None:
        known_names = ", ".join(cl.get_location(CAMPAIGN, loc_id)["name"] for loc_id in visited)
        await update.effective_chat.send_message(
            f"You can only fast-travel somewhere you've actually been. Waypoints you've discovered: {known_names}",
            message_thread_id=config.TOPIC_ADVENTURE_ID,
        )
        return

    if destination_id == character["current_location"]:
        await update.effective_chat.send_message(
            "You're already there.", message_thread_id=config.TOPIC_ADVENTURE_ID
        )
        return

    destination = cl.get_location(CAMPAIGN, destination_id)
    if destination.get("requires_item") and destination["requires_item"] not in character["inventory"]:
        await update.effective_chat.send_message(
            f"Something stops **{character['name']}** from going any further — missing something needed first.",
            message_thread_id=config.TOPIC_ADVENTURE_ID,
        )
        return

    if not _meets_location_level(character, destination):
        await _send_level_gate_message(update, destination)
        return

    current = cl.get_location(CAMPAIGN, character["current_location"])

    locked_connections = current.get("locked_connections", {})
    lockable_id = locked_connections.get(destination_id)
    if lockable_id and lockable_id not in _UNLOCKED:
        lockable = next(
            (lk for lk in current.get("lockables", []) if lk["id"] == lockable_id), None
        )
        name = lockable["name"] if lockable else "something locked"
        await update.effective_chat.send_message(
            f"The way to {destination['name']} is blocked by {name}. Try picking the lock first.",
            message_thread_id=config.TOPIC_ADVENTURE_ID,
        )
        return

    story_gate_message = _check_story_gate(character, current, destination_id)
    if story_gate_message:
        await update.effective_chat.send_message(story_gate_message, message_thread_id=config.TOPIC_ADVENTURE_ID)
        return

    db.move_character(telegram_user_id, destination_id)
    await _safe_send(update, f"🌀 You fast-travel to **{destination['name']}**.\n{destination['description']}")

    updated_character = db.get_character(telegram_user_id)
    await _maybe_trigger_npc_encounter(update, updated_character, destination)
    await _check_quest_completions_reach_location(update, update.effective_user.id, destination_id)
    await _check_board_quest_turnin(update, update.effective_user.id, destination_id)


def _location_neighbors(location_id: str) -> list[str]:
    location = cl.get_location(CAMPAIGN, location_id)
    if not location:
        return []
    neighbors = list(location.get("connections", []))
    if "descends_to" in location:
        neighbors.append(location["descends_to"])
    if "ascends_to" in location:
        neighbors.append(location["ascends_to"])
    return neighbors


def _nearest_shop_location(start_location_id: str) -> tuple[str, int] | None:
    """BFS over the real location graph for the closest location with a real shop. Returns (location_id, hops)."""
    start = cl.get_location(CAMPAIGN, start_location_id)
    if start and start.get("shop"):
        return start_location_id, 0

    visited = {start_location_id}
    queue = [(start_location_id, 0)]
    while queue:
        current_id, dist = queue.pop(0)
        for neighbor_id in _location_neighbors(current_id):
            if neighbor_id in visited:
                continue
            visited.add(neighbor_id)
            neighbor = cl.get_location(CAMPAIGN, neighbor_id)
            if neighbor and neighbor.get("shop"):
                return neighbor_id, dist + 1
            queue.append((neighbor_id, dist + 1))
    return None


async def _do_find_merchant(update: Update) -> None:
    character = db.get_character(update.effective_user.id)
    if character is None:
        await update.effective_chat.send_message(
            "You don't have a character yet!", message_thread_id=config.TOPIC_ADVENTURE_ID
        )
        return

    result = _nearest_shop_location(character["current_location"])
    if result is None:
        await update.effective_chat.send_message(
            "There's no known merchant reachable from here.", message_thread_id=config.TOPIC_ADVENTURE_ID
        )
        return

    location_id, hops = result
    location = cl.get_location(CAMPAIGN, location_id)
    shop = cl.get_shop(CAMPAIGN, location["shop"])
    owner = CAMPAIGN["npcs"].get(shop.get("owner_npc")) if shop else None
    owner_name = owner["name"] if owner else None

    if hops == 0:
        line = f"🛒 You're already at a merchant — {owner_name or 'the shop'} is right here."
    else:
        stops = "stop" if hops == 1 else "stops"
        owner_line = f", run by {owner_name}" if owner_name else ""
        line = f"🛒 Closest merchant: **{location['name']}**{owner_line} — {hops} {stops} from here."
    await _safe_send(update, line)


async def _do_give_item(update: Update, text: str) -> None:
    """
    Player-to-player item trading (2026-07-15 backlog item): hand a
    carried item to another real player or AI companion. Scoped to
    whoever's actually active at the giver's own current_location, same
    "physically present" convention already used for who can join a
    fight (_get_combat_eligible_party_members) -- not restricted to a
    formed party, since trading with anyone standing in the same room
    is the more natural reading of "give my potion to X".
    """
    character = db.get_character(update.effective_user.id)
    if character is None:
        await update.effective_chat.send_message(
            "You don't have a character yet!", message_thread_id=config.TOPIC_ADVENTURE_ID
        )
        return

    candidates = [
        p for p in _get_combat_eligible_party_members(character["current_location"])
        if p["telegram_user_id"] != character["telegram_user_id"]
    ]
    recipient = _match_member_by_name_or_username(text, candidates)
    if recipient is None:
        await update.effective_chat.send_message(
            f"**{character['name']}**: give it to whom? Name someone real who's actually here with you.",
            message_thread_id=config.TOPIC_ADVENTURE_ID,
        )
        return

    items_wanted = _extract_item_list(text, list(character["inventory"].keys()))
    if not items_wanted:
        await update.effective_chat.send_message(
            f"Give {recipient['name']} what, exactly? Name something you're actually carrying.",
            message_thread_id=config.TOPIC_ADVENTURE_ID,
        )
        return

    given = []
    for item_id, quantity in items_wanted:
        removed, _ = db.remove_item(update.effective_user.id, item_id, quantity)
        item_name = items_module.get_item(item_id)["name"]
        if not removed:
            have = character["inventory"].get(item_id, 0)
            given.append(f"You don't have {quantity}x {item_name} to give — you only have {have}.")
            continue
        db.add_item(recipient["telegram_user_id"], item_id, quantity)
        given.append(f"🤝 **{character['name']}** gives {quantity}x {item_name} to **{recipient['name']}**.")

    await _safe_send(update, "\n".join(given))


async def _do_use_item(update: Update, text: str) -> None:
    """
    Real bug from the reverse-playthrough sweep (2026-07-15): NOTHING in
    this entire codebase ever read a consumable item's "heal_dice" or
    "effect" field -- Healing Potions, Greater Healing Potions, and
    Antitoxin were all completely non-functional. A player could buy
    one for real gold and there was no action anywhere to ever drink
    it. Fixed by adding this handler, following the exact same
    rules-decide/bot.py-narrates split as every other outcome (heal
    amount rolled via rules.dice.roll_damage, never invented).

    Combat-turn awareness (2026-07-18, real live incident): this used
    to have NO idea a combat session existed at all -- confirmed live,
    a player tried "Eat ration" mid-combat (silently misclassified as
    chat at the time, see task #166's fix) and, separately, even a
    real use_item call here would have done nothing to the encounter:
    no turn-order check, no advance_turn(). Using a consumable is a
    real action in 5E, so it now blocks out-of-turn the same way
    _do_attack does, and consumes the actor's turn (advance_turn +
    resolve AI turns) when it succeeds during active combat -- exactly
    like finishing an attack. Out of combat, behaves exactly as before.
    """
    character = db.get_character(update.effective_user.id)
    if character is None:
        await update.effective_chat.send_message(
            "You don't have a character yet!", message_thread_id=config.TOPIC_ADVENTURE_ID
        )
        return

    chat_id = update.effective_chat.id
    user_id = update.effective_user.id
    session = sessions.get_session(chat_id)
    in_combat = session is not None and user_id in session.turn_order
    if in_combat and session.current_participant_id() != user_id:
        current_name = session.current_participant()["name"]
        await update.effective_chat.send_message(
            f"It's not your turn — it's **{current_name}**'s turn.",
            message_thread_id=config.TOPIC_ADVENTURE_ID,
        )
        return

    # Task #141: map items (type "map") are usable the same way a
    # consumable is -- read it, it does its one-time thing, it's gone --
    # so they share this same dispatch path rather than a separate command.
    consumable_ids = [
        item_id for item_id in character["inventory"]
        if (items_module.get_item(item_id) or {}).get("type") in ("consumable", "map")
    ]
    item_id = items_module.find_item_mentioned_in_text(text, candidate_ids=consumable_ids)
    if item_id is None:
        await update.effective_chat.send_message(
            "Use what, exactly? Name a consumable you're actually carrying.",
            message_thread_id=config.TOPIC_ADVENTURE_ID,
        )
        return

    item = items_module.get_item(item_id)
    target = _find_party_target_by_name(text) or character
    is_self = target["telegram_user_id"] == character["telegram_user_id"]
    target_note = "" if is_self else f" on **{target['name']}**"

    removed, _ = db.remove_item(update.effective_user.id, item_id, 1)
    if not removed:
        await update.effective_chat.send_message(
            f"You don't have a {item['name']} to use.", message_thread_id=config.TOPIC_ADVENTURE_ID
        )
        return

    effect = item.get("effect", "none")
    if effect == "heal" and item.get("heal_dice"):
        healing = roll_damage(item["heal_dice"])
        hp_before = target["hp_current"]
        hp_max = target.get("hp_max", hp_before)
        new_hp = min(hp_before + healing["total"], hp_max)
        db.update_character(target["telegram_user_id"], hp_current=new_hp)
        message = (
            f"🧪 **{character['name']}** uses a {item['name']}{target_note}, "
            f"healing {new_hp - hp_before} HP ({new_hp}/{hp_max})."
        )
    elif item.get("type") == "map":
        # Task #141: a genuine partial reveal -- adds up to reveals_count
        # random location NAMES from the item's one real layer to
        # map_revealed_locations (a separate fog-of-war layer from
        # visited_locations, see db.py's migration comment), never all of
        # them at once and never a location already visited/revealed.
        # _do_show_map renders these distinctly, name only, no connections
        # -- that's the actual spoiler, and it stays earned by walking there.
        layer = item.get("reveals_layer")
        layer_locations = CAMPAIGN["locations"].get(layer, {})
        already_known = set(character["visited_locations"]) | set(character["map_revealed_locations"])
        candidates = [loc_id for loc_id in layer_locations if loc_id not in already_known]
        if not candidates:
            message = (
                f"📜 **{character['name']}** unrolls the {item['name']} — but it shows nothing "
                f"you haven't already found on your own."
            )
        else:
            newly_revealed = random.sample(candidates, min(item.get("reveals_count", 1), len(candidates)))
            db.update_character(
                character["telegram_user_id"],
                map_revealed_locations=character["map_revealed_locations"] + newly_revealed,
            )
            revealed_names = ", ".join(layer_locations[loc_id]["name"] for loc_id in newly_revealed)
            message = (
                f"📜 **{character['name']}** unrolls the {item['name']} — it marks the rough "
                f"location of {revealed_names}, though you'll still need to find your own way there."
            )
    elif effect == "cure_poison":
        cured = False
        if session is not None:
            live_target = next(
                (p for p in session.participants if p["telegram_user_id"] == target["telegram_user_id"]), None,
            )
            if live_target and "poisoned" in live_target.get("conditions", []):
                live_target["conditions"].remove("poisoned")
                cured = True
        message = (
            f"🧪 **{character['name']}** uses a {item['name']}{target_note} — the poison is neutralized."
            if cured else
            f"🧪 **{character['name']}** uses a {item['name']}{target_note}, just in case."
        )
    else:
        # Flavor-only consumables (rations, ale, torch, etc.) -- real 5E
        # items this game has no mechanic for (hunger, light radius), same
        # honesty convention as every other unmodeled mechanic in this build.
        message = f"🧺 **{character['name']}** uses a {item['name']}{target_note}."

    await _safe_send(update, message)

    if in_combat:
        async with sessions.get_lock(chat_id):
            session = sessions.get_session(chat_id)
            if session is not None:
                session.advance_turn()
                await _resolve_ai_turns(update, session)


async def _do_equip_item(update: Update, text: str) -> None:
    """
    Real bug from the same reverse-playthrough finding as _do_use_item
    (2026-07-15): items.py's weapon (damage_dice/ability), armor
    (ac_base), and shield (ac_bonus) fields existed the whole time but
    nothing ever equipped anything, so combat always used one
    hardcoded default weapon and armor_class never changed after
    character creation regardless of what was bought. db.equip_item
    does the real work (validation, armor_class recompute); this just
    parses which item, and for whom, from free text.

    Supports helping another party member gear up too (per Coffee:
    "equip Sarah with the longbow") -- same location-scoped "physically
    present" lookup as _do_give_item, defaulting to the caller's own
    character when no other real party member is named. The item comes
    from the TARGET's own inventory, not the caller's -- this is
    "help them equip what they're already carrying," not a transfer.
    """
    character = db.get_character(update.effective_user.id)
    if character is None:
        await update.effective_chat.send_message(
            "You don't have a character yet!", message_thread_id=config.TOPIC_ADVENTURE_ID
        )
        return

    target = character
    others = [
        p for p in _get_combat_eligible_party_members(character["current_location"])
        if p["telegram_user_id"] != character["telegram_user_id"]
    ]
    named_other = _match_member_by_name_or_username(text, others)
    if named_other is not None:
        target = named_other

    equippable_ids = [
        item_id for item_id in target["inventory"]
        if (items_module.get_item(item_id) or {}).get("type") in ("weapon", "armor", "shield", "ring", "amulet", "wondrous")
    ]
    items_wanted = _extract_item_list(text, equippable_ids)
    if not items_wanted:
        who = "you" if target is character else target["name"]
        await update.effective_chat.send_message(
            f"Equip what, exactly? Name a weapon, armor, shield, ring, amulet, or wondrous item "
            f"{who} actually {'are' if who == 'you' else 'is'} carrying.",
            message_thread_id=config.TOPIC_ADVENTURE_ID,
        )
        return

    prefix = "" if target is character else f"**{target['name']}**: "
    lines = []
    equipped_item_ids = []
    for item_id, _quantity in items_wanted:
        success, message, _ = db.equip_item(target["telegram_user_id"], item_id)
        lines.append(f"⚔️ {prefix}{message}" if success else message)
        if success:
            equipped_item_ids.append(item_id)
    await _safe_send(update, "\n".join(lines))
    for item_id in equipped_item_ids:
        item_data = items_module.get_item(item_id)
        if item_data:
            await _maybe_send_item_image(update, item_id, item_data)
    await _check_and_award_achievements(update, db.get_character(target["telegram_user_id"]))


async def _do_auto_equip_gear(update: Update, text: str) -> None:
    """
    Auto-equip (2026-07-15, per Coffee): picks the real best weapon/
    armor/shield out of whatever's actually carried and equips all of
    them, via db.auto_equip_best_gear -- so a player doesn't need to
    know every item's exact damage die or AC to get sensible gear on.
    Supports naming another real party member ("help Borin gear up"),
    same location-scoped lookup as _do_equip_item/_do_give_item.
    """
    character = db.get_character(update.effective_user.id)
    if character is None:
        await update.effective_chat.send_message(
            "You don't have a character yet!", message_thread_id=config.TOPIC_ADVENTURE_ID
        )
        return

    target = character
    others = [
        p for p in _get_combat_eligible_party_members(character["current_location"])
        if p["telegram_user_id"] != character["telegram_user_id"]
    ]
    named_other = _match_member_by_name_or_username(text, others)
    if named_other is not None:
        target = named_other

    summary, _ = db.auto_equip_best_gear(target["telegram_user_id"])
    prefix = "" if target is character else f"**{target['name']}**: "
    await _safe_send(update, f"⚔️ {prefix}{summary}")
    await _check_and_award_achievements(update, db.get_character(target["telegram_user_id"]))


_QUANTITY_WORDS = {
    "one": 1, "couple": 2, "two": 2, "three": 3, "four": 4,
    "five": 5, "six": 6, "seven": 7, "eight": 8, "nine": 9, "ten": 10,
}


_ITEM_LIST_SPLIT_PATTERN = re.compile(r",\s*(?:and\s+)?|\s+and\s+")


def _extract_item_list(text: str, candidate_ids: list[str]) -> list[tuple[str, int]]:
    """
    Real fix for task #147 (2026-07-17, live-confirmed by Coffee):
    "Buy 10 torches, 1 shears, 1 pickaxe, 1 fishing pole, 5 bait." used
    to only ever buy the torches -- find_item_mentioned_in_text and
    _extract_quantity each only ever resolve ONE match for a whole
    message, and ai/intent_parser.py's compound-message splitter used
    to mis-route the other bare "<qty> <item>" segments into unrelated
    actions (one even misfired as "gather" -- "1 pickaxe" contains
    "pick"). parse_intents now keeps a shopping-list message as ONE
    action instead of splitting it; this is the other half -- splitting
    the raw text into per-item phrases the same way a shopping list
    reads, and resolving each phrase's own item + quantity independently
    so every item actually named gets processed, not just the first.
    A plain single-item message (no comma/"and") is one "phrase" and
    behaves exactly as before.
    """
    phrases = [p.strip() for p in _ITEM_LIST_SPLIT_PATTERN.split(text) if p.strip()]
    pairs: list[tuple[str, int]] = []
    for phrase in phrases:
        item_id = items_module.find_item_mentioned_in_text(phrase, candidate_ids=candidate_ids)
        if item_id is None:
            continue
        quantity = _extract_quantity(phrase)
        pairs.append((item_id, quantity))
    return pairs


def _extract_quantity(text: str) -> int:
    """
    Real number of items to buy/sell mentioned anywhere in the sentence,
    e.g. "buy two potions" -> 2, "sell 3 rations" -> 3. Confirmed live
    2026-07-12: _do_buy/_do_sell always silently bought/sold exactly 1
    regardless of what the player actually said (shop.buy_item/sell_item
    already fully support a real quantity, this was purely a wiring gap
    -- the number was just never read out of the text). Defaults to 1,
    the previous behavior, when no number is found. Deliberately no
    "a"/"an" entry -- since the default is already 1, adding them would
    only risk shadowing a REAL number word appearing later in the same
    sentence (e.g. "buy a couple potions" stopping at "a" before ever
    reaching "couple").
    """
    words = [w.strip(".,!?").lower() for w in text.split()]
    for word in words:
        if word.isdigit():
            return max(1, int(word))
    for word in words:
        if word in _QUANTITY_WORDS:
            return _QUANTITY_WORDS[word]
    return 1


def _shop_keyboard(shop_data: dict) -> InlineKeyboardMarkup | None:
    """
    Task #176 (per Coffee: extend the battle-menu button pattern to
    out-of-combat browsing surfaces) -- one button per real stocked item,
    grounded in the same shop_data/items.py lookup _do_list_shop already
    uses right above. Tapping dispatches through the SAME _do_buy handler
    free-text "buy X" already uses (see shop_menu_callback below), never
    duplicated purchase logic. Own callback-data namespace ("shop|", not
    "bm|") so this can never collide with the combat battle menu.
    """
    buttons = []
    for item_id in shop_data["inventory"]:
        item = items_module.get_item(item_id)
        if item:
            buttons.append([InlineKeyboardButton(
                f"{item['name']} — {item['price']}g", callback_data=f"shop|qty|{item_id}",
            )])
    return InlineKeyboardMarkup(buttons) if buttons else None


def _quantity_keyboard(item_id: str) -> InlineKeyboardMarkup:
    """
    Per Coffee (2026-07-20): "when they select the item can you prompt
    with click buttons for those selections" -- picking an item in the
    shop now asks HOW MANY via real tap buttons instead of always
    buying a single one, same reuse-the-existing-handler pattern as
    every other button in this game (_do_buy already parses "buy 5 X").
    """
    return InlineKeyboardMarkup([[
        InlineKeyboardButton(str(qty), callback_data=f"shop|buy|{item_id}|{qty}")
        for qty in (1, 5, 10)
    ]])


async def shop_menu_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """
    Handles taps on _shop_keyboard/_quantity_keyboard. Re-resolves the
    shop from the TAPPER's own current location (never trusts anything
    about the shop from the button itself) so this behaves identically
    to typing "buy X" -- same real _do_buy handler, same stock/gold/
    inventory checks. Tapping an item first shows a quantity picker
    (1/5/10); tapping a quantity actually buys.
    """
    query = update.callback_query
    parts = (query.data or "").split("|")
    action = parts[1] if len(parts) > 1 else ""
    item_id = parts[2] if len(parts) > 2 else None
    await _safe_answer(query)
    item = items_module.get_item(item_id) if item_id else None
    if item is None:
        return
    if action == "qty":
        await _safe_send(update, f"How many {item['name']}?", reply_markup=_quantity_keyboard(item_id))
        return
    if action == "buy":
        qty = int(parts[3]) if len(parts) > 3 else 1
        await _do_buy(update, f"buy {qty} {item['name']}")


async def market_menu_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handles taps on _market_keyboard -- dispatches through the same _do_buy_market a typed "/buy_market <#>" already uses."""
    query = update.callback_query
    parts = (query.data or "").split("|")
    action = parts[1] if len(parts) > 1 else ""
    await _safe_answer(query)
    if action != "buy" or len(parts) < 3:
        return
    await _do_buy_market(update, [parts[2]])


async def _do_list_shop(update: Update) -> None:
    """
    Real shop-browse action (2026-07-16, per Coffee): lists a shop's
    ACTUAL stocked items (real names/prices from items.py), grounded,
    not invented -- confirmed live the same night that with no such
    action to reach for, "I want to shop" was silently swallowed as
    chat and "I want to see the items in the shop" got guessed by the
    raw model as check_sheet with a hallucinated target name. buy_item
    still handles purchasing a SPECIFIC named item; this just answers
    "what do you even have."
    """
    character = db.get_character(update.effective_user.id)
    if character is None:
        await update.effective_chat.send_message(
            "You don't have a character yet!", message_thread_id=config.TOPIC_ADVENTURE_ID
        )
        return
    location = cl.get_location(CAMPAIGN, character["current_location"])
    shop_id = location.get("shop") if location else None
    if not shop_id:
        await update.effective_chat.send_message(
            "There's no shop here.", message_thread_id=config.TOPIC_ADVENTURE_ID
        )
        return
    shop_data = cl.get_shop(CAMPAIGN, shop_id)
    lines = [f"🛒 **{cl.get_location(CAMPAIGN, character['current_location'])['name']}**"]
    if shop_data.get("description"):
        lines.append(shop_data["description"])
    # Per Coffee (2026-07-21): "make different shops for the
    # appropriate items (weapons, armour, Magic, General Items)...
    # describe the stalls and say what each store is." Rather than
    # splitting one location into several separately-walkable shops (a
    # bigger change to the one-shop-per-location structure this whole
    # game already relies on), this groups a shop's real stock into
    # real labeled sections -- the same effect players actually asked
    # for (a Maren's Wares that clearly separates its weapon rack from
    # its armor rack from its general goods) without touching how
    # buying/location resolution work at all.
    for header, item_ids in _grouped_shop_inventory(shop_data["inventory"]).items():
        lines.append(f"\n{header}")
        for item_id in item_ids:
            item = items_module.get_item(item_id)
            lines.append(f"{item['name']} — {item['price']} gold")
    await _safe_send(update, "\n".join(lines), reply_markup=_shop_keyboard(shop_data), speak=False)


_SHOP_SECTION_FOR_TYPE = {
    "weapon": "⚔️ Weapons",
    "armor": "🛡️ Armor & Shields",
    "shield": "🛡️ Armor & Shields",
    "tool": "🧰 Tools",
    "consumable": "🧪 Consumables",
    "scroll": "📜 Scrolls",
    "ring": "🔮 Magic Items",
    "amulet": "🔮 Magic Items",
    "wondrous": "🔮 Magic Items",
    "map": "🗺️ Maps",
}
_SHOP_SECTION_ORDER = [
    "⚔️ Weapons", "🛡️ Armor & Shields", "🧰 Tools", "🧪 Consumables",
    "📜 Scrolls", "🔮 Magic Items", "🗺️ Maps", "📦 Other",
]


def _grouped_shop_inventory(item_ids: list[str]) -> dict[str, list[str]]:
    """Groups a shop's real inventory by item type into real, labeled sections, always in the same reader-friendly order."""
    sections: dict[str, list[str]] = {}
    for item_id in item_ids:
        item = items_module.get_item(item_id)
        if not item:
            continue
        header = _SHOP_SECTION_FOR_TYPE.get(item.get("type"), "📦 Other")
        sections.setdefault(header, []).append(item_id)
    return {header: sections[header] for header in _SHOP_SECTION_ORDER if header in sections}


async def _do_buy(update: Update, text: str) -> None:
    character = db.get_character(update.effective_user.id)
    if character is None:
        await update.effective_chat.send_message(
            "You don't have a character yet!", message_thread_id=config.TOPIC_ADVENTURE_ID
        )
        return
    location = cl.get_location(CAMPAIGN, character["current_location"])
    shop_id = location.get("shop") if location else None
    if not shop_id:
        await update.effective_chat.send_message(
            "There's no shop here.", message_thread_id=config.TOPIC_ADVENTURE_ID
        )
        return
    shop_data = cl.get_shop(CAMPAIGN, shop_id)

    items_wanted = _extract_item_list(text, shop_data["inventory"])
    if not items_wanted:
        await update.effective_chat.send_message(
            "Not sure what item you mean — try naming it more directly.",
            message_thread_id=config.TOPIC_ADVENTURE_ID,
        )
        return

    # Names the buyer explicitly (task #139, 2026-07-17, per Coffee):
    # "Bought 10x Torch for 10 gold" reads fine in a 1:1 test but is
    # genuinely ambiguous the moment more than one player is shopping in
    # the same busy chat -- same "who does 'you' mean" problem already
    # fixed for combat/skill-check narration (#107/#115).
    if len(items_wanted) == 1:
        item_id, quantity = items_wanted[0]
        ok, msg = shop_module.buy_item(update.effective_user.id, shop_data, item_id, quantity)
        await _safe_send(update, f"🛒 **{character['name']}**: {msg}")
        return

    lines = []
    for item_id, quantity in items_wanted:
        ok, msg = shop_module.buy_item(update.effective_user.id, shop_data, item_id, quantity)
        lines.append(msg)
    await _safe_send(update, f"🛒 **{character['name']}**:\n" + "\n".join(lines))


async def _do_sell(update: Update, text: str) -> None:
    character = db.get_character(update.effective_user.id)
    if character is None:
        await update.effective_chat.send_message(
            "You don't have a character yet!", message_thread_id=config.TOPIC_ADVENTURE_ID
        )
        return

    items_wanted = _extract_item_list(text, list(character["inventory"].keys()))
    items_wanted = [(item_id, qty) for item_id, qty in items_wanted if item_id in character["inventory"]]
    if not items_wanted:
        await update.effective_chat.send_message(
            "You're not carrying anything by that name.", message_thread_id=config.TOPIC_ADVENTURE_ID
        )
        return

    if len(items_wanted) == 1:
        item_id, quantity = items_wanted[0]
        ok, msg = shop_module.sell_item(update.effective_user.id, item_id, quantity)
        await _safe_send(update, f"🛒 **{character['name']}**: {msg}")
        return

    lines = []
    for item_id, quantity in items_wanted:
        ok, msg = shop_module.sell_item(update.effective_user.id, item_id, quantity)
        lines.append(msg)
    await _safe_send(update, f"🛒 **{character['name']}**:\n" + "\n".join(lines))


async def _do_steal(update: Update, text: str, forced_roll: int | None = None) -> None:
    """
    A real, dice-resolved theft attempt (DC 15 — harder than an ordinary
    skill check) with a genuine, persistent consequence on failure: the
    shopkeeper bans this player from their shop forever (db.set_banned_
    by_npc, enforced in shop.buy_item), their affinity toward this
    player craters, the specific event is remembered (db.adjust_affinity's
    `event`), and their whole faction's standing with this player drops.
    Success carries no penalty — nobody noticed.
    """
    telegram_user_id = update.effective_user.id
    character = db.get_character(telegram_user_id)
    if character is None:
        await update.effective_chat.send_message(
            "You don't have a character yet!", message_thread_id=config.TOPIC_ADVENTURE_ID
        )
        return

    location = cl.get_location(CAMPAIGN, character["current_location"])
    shop_id = location.get("shop") if location else None
    if not shop_id:
        await update.effective_chat.send_message(
            "There's nothing here worth stealing.", message_thread_id=config.TOPIC_ADVENTURE_ID
        )
        return

    shop_data = cl.get_shop(CAMPAIGN, shop_id)
    owner_npc = shop_data.get("owner_npc")
    owner_name = CAMPAIGN["npcs"][owner_npc]["name"] if owner_npc else "the shopkeeper"

    if owner_npc and db.is_banned_by_npc(telegram_user_id, owner_npc):
        await update.effective_chat.send_message(
            f"{owner_name} is already watching you like a hawk after last time — not worth the risk.",
            message_thread_id=config.TOPIC_ADVENTURE_ID,
        )
        return

    item_id = items_module.find_item_mentioned_in_text(text, candidate_ids=shop_data["inventory"])
    if item_id is None:
        item_id = min(shop_data["inventory"], key=lambda i: items_module.get_item(i).get("price", 0))
    item = items_module.get_item(item_id)

    if forced_roll is None and character.get("manual_dice_enabled") and not character.get("is_ai"):
        forced_roll = _extract_combined_roll(text)
    if forced_roll is None and character.get("manual_dice_enabled") and not character.get("is_ai"):
        _PENDING_DICE_ROLLS[telegram_user_id] = _new_pending_roll("steal", text, update.effective_chat.id)
        # Task #187: _safe_send, not a raw send_message -- see the attack
        # site's comment above for why.
        await _safe_send(update, f"🎲 **{character['name']}**, roll a d20 for your theft attempt and tell me the result (you have 1 minute, or I'll roll for you).")
        return

    result = roll_ability_check(character, "dexterity", proficient=False, forced_roll=forced_roll)
    bonus = _practiced_bonus_for(telegram_user_id, "dexterity")
    result["total"] += bonus
    result["practiced_bonus"] = bonus
    success = result["total"] >= STEAL_DC

    if success:
        db.record_skill_use(telegram_user_id, "dexterity")
        db.add_item(telegram_user_id, item_id, 1)
        consequence_line = f"\n🤫 You slip away with **{item['name']}** — nobody noticed."
    else:
        db.add_item(telegram_user_id, item_id, 1)  # still takes it, just gets caught
        consequence_line = f"\n🚨 **Caught!** {owner_name} won't sell to you ever again."
        if owner_npc:
            db.adjust_affinity(
                telegram_user_id, owner_npc, -40,
                event=f"Was caught stealing {item['name']} from {owner_name}'s shop.",
            )
            db.set_banned_by_npc(telegram_user_id, owner_npc, True)
            faction_id = _faction_for_npc(owner_npc)
            if faction_id:
                _adjust_faction_standing(
                    telegram_user_id, faction_id, -15, _faction_starting_standing(faction_id)
                )

    flavor = await asyncio.to_thread(
        narrate_skill_check, character, text, "dexterity",
        {**result, "ability": "dexterity", "dc": STEAL_DC, "success": success},
    )
    message = _format_skill_check_result(flavor, result, "dexterity", STEAL_DC, success) + consequence_line
    await _safe_send(update, message)


EMPOWERED_SPELL_MAX_USES = 1


def _apply_empowered_spell(telegram_user_id: int, character: dict, spell: dict, result: dict) -> dict:
    """
    Sorcerer's Metamagic: Empowered Spell (real 5E, level 3+, previously
    pure flavor text in class_features.py) -- automatically rerolls any
    1s and 2s among a damage spell's dice once per rest, keeping the
    new roll ("the roll is already decided, this refines it before
    it's ever reported" -- the same rules-then-narration order every
    other outcome in this game follows). Real 5E spends a Sorcery Point
    (a resource this engine has no equivalent of) and lets the caster
    choose which dice to reroll and whether to use it at all;
    simplified to the same once-per-rest feature_uses economy every
    other class feature already shares, applied automatically to the
    first damage spell a Sorcerer casts each rest rather than adding a
    whole separate opt-in command just for this one choice.
    """
    if character.get("char_class") != "Sorcerer" or character.get("level", 1) < 3:
        return result
    if db.get_feature_uses(telegram_user_id, "empowered_spell") >= EMPOWERED_SPELL_MAX_USES:
        return result
    match = re.match(r"(\d+)d(\d+)", spell.get("damage_dice", ""))
    if not match:
        return result
    sides = int(match.group(2))
    old_rolls = result["rolls"]
    new_rolls = [r if r > 2 else roll(1, sides)[0] for r in old_rolls]
    if new_rolls == old_rolls:
        return result
    db.use_feature(telegram_user_id, "empowered_spell")
    return {**result, "rolls": new_rolls, "damage_dealt": result["damage_dealt"] + (sum(new_rolls) - sum(old_rolls))}


def _with_menu_button(keyboard: InlineKeyboardMarkup | None) -> InlineKeyboardMarkup:
    """
    Appends a single "📖 Menu" row (callback_data "menu|root") to any
    existing keyboard, or returns a keyboard with just that row if there
    wasn't one -- per Coffee: "make it a complete menu system." Used on
    every real menu section's own reply (sheet/story/quests/inventory/
    equip) so every screen loops back to the root menu instead of being
    a dead end, without needing its own separate rendering path.
    """
    rows = list(keyboard.inline_keyboard) if keyboard else []
    rows.append([InlineKeyboardButton("📖 Menu", callback_data="menu|root")])
    return InlineKeyboardMarkup(rows)


def _main_menu_keyboard(character: dict) -> InlineKeyboardMarkup:
    """
    Per Coffee (2026-07-19 dev-topic feedback): "Only show level up when
    we have points to distribute. Otherwise it serves no purpose." The
    Level Up row now only appears once pending_asi_points is real and
    positive -- the rest of the menu is unconditional either way.
    """
    rows = [
        [InlineKeyboardButton("🧙 Character Sheet", callback_data="menu|sheet")],
        [InlineKeyboardButton("📖 Story So Far", callback_data="menu|story")],
        [InlineKeyboardButton("📋 Quests", callback_data="menu|quests")],
        [InlineKeyboardButton("🎒 Inventory", callback_data="menu|inventory")],
        [InlineKeyboardButton("⚔️ Equip Gear", callback_data="menu|equip")],
        [InlineKeyboardButton("👥 Party", callback_data="menu|party")],
        [InlineKeyboardButton("🎭 Switch Character", callback_data="menu|roster")],
        [InlineKeyboardButton("🧭 Waypoints", callback_data="menu|waypoints")],
        [InlineKeyboardButton("🌳 Skill Tree", callback_data="menu|skilltree")],
        [InlineKeyboardButton("🏅 Achievements", callback_data="menu|achievements")],
        [InlineKeyboardButton("🏛️ Market", callback_data="menu|market")],
        [InlineKeyboardButton("📚 Bestiary", callback_data="menu|bestiary")],
        [InlineKeyboardButton("🌤️ Weather", callback_data="menu|weather")],
        [InlineKeyboardButton("🏆 Leaderboard", callback_data="menu|leaderboard")],
        [InlineKeyboardButton("🗺️ Visual Map", callback_data="menu|visualmap")],
    ]
    if character.get("pending_asi_points", 0) > 0:
        rows.append([InlineKeyboardButton("📈 Level Up", callback_data="menu|level")])
    # Rebirth/hybrid (2026-07-22): same "only show it when it actually
    # applies" convention as Level Up above -- Rebirth only appears at
    # MAX_LEVEL (it would just reject the tap otherwise), and Hybrid
    # Class only once it's actually unlocked (after a first rebirth).
    if character.get("level", 1) >= MAX_LEVEL:
        rows.append([InlineKeyboardButton("✨ Rebirth", callback_data="menu|rebirth")])
    if hybrid_tier(character.get("rebirth_count", 0)) > 0:
        rows.append([InlineKeyboardButton("🌟 Hybrid Class", callback_data="menu|hybrid")])
    return InlineKeyboardMarkup(rows)


def _hybrid_keyboard(character: dict) -> InlineKeyboardMarkup:
    """Every OTHER real class as a tappable hybrid pick -- never your own char_class, which _do_choose_hybrid would reject anyway."""
    buttons = [
        [InlineKeyboardButton(cls, callback_data=f"hybrid|choose|{cls}")]
        for cls in HYBRID_CLASS_FEATURES if cls != character.get("char_class")
    ]
    return InlineKeyboardMarkup(buttons)


async def _do_show_hybrid_menu(update: Update) -> None:
    character = db.get_character(update.effective_user.id)
    if character is None:
        await update.effective_chat.send_message(
            "You don't have a character yet!", message_thread_id=config.TOPIC_ADVENTURE_ID
        )
        return
    tier = hybrid_tier(character.get("rebirth_count", 0))
    if tier <= 0:
        await _safe_send(
            update, "Hybrid classes unlock after your first rebirth — say \"rebirth\" once you're level 99.",
            speak=False,
        )
        return
    current = (
        f"Current hybrid: **{character['hybrid_class']}** (tier {tier}/{HYBRID_MAX_TIER})\n\n"
        if character.get("hybrid_class") else ""
    )
    await _safe_send(
        update,
        f"🌟 **{character['name']}** — pick a hybrid class flavor:\n\n{current}Tap one below:",
        reply_markup=_with_menu_button(_hybrid_keyboard(character)),
        speak=False,
    )


async def hybrid_menu_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handles taps on _hybrid_keyboard -- dispatches through the same real _do_choose_hybrid free text already uses."""
    query = update.callback_query
    parts = (query.data or "").split("|")
    action = parts[1] if len(parts) > 1 else ""
    await _safe_answer(query)
    if action != "choose" or len(parts) < 3:
        return
    await _do_choose_hybrid(update, parts[2])


async def _do_show_menu(update: Update) -> None:
    """
    Task #176 full menu revision, per Coffee (2026-07-19): "show things
    in menu that are needed for the game - story-so-far - quests -
    inventory - equip character - make it a complete menu system." Root
    navigational menu -- every section below dispatches through the
    SAME real handlers free text already uses (_do_check_sheet,
    _do_show_story_so_far, _do_check_quests, _do_check_inventory,
    _do_show_equip_menu), never a separate rendering path, and every
    section's own reply loops back here via _with_menu_button.
    """
    character = db.get_character(update.effective_user.id)
    if character is None:
        await update.effective_chat.send_message(
            "You don't have a character yet!", message_thread_id=config.TOPIC_ADVENTURE_ID
        )
        return
    await _safe_send(
        update, f"📖 **{character['name']}** — what do you want to check?",
        reply_markup=_main_menu_keyboard(character), speak=False,
    )


async def menu_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handles taps on _main_menu_keyboard and every section's own Menu button -- see _do_show_menu's docstring."""
    query = update.callback_query
    parts = (query.data or "").split("|")
    section = parts[1] if len(parts) > 1 else ""
    await _safe_answer(query)
    if section == "root":
        await _do_show_menu(update)
    elif section == "sheet":
        await _do_check_sheet(update)
    elif section == "story":
        await _do_show_story_so_far(update)
    elif section == "quests":
        await _do_check_quests(update)
    elif section == "inventory":
        await _do_check_inventory(update)
    elif section == "equip":
        await _do_show_equip_menu(update)
    elif section == "level":
        await _do_show_level_menu(update)
    elif section == "roster":
        await _do_list_characters(update)
    elif section == "waypoints":
        await _do_show_waypoints(update)
    elif section == "skilltree":
        await _do_show_skill_tree(update)
    elif section == "achievements":
        await _do_check_achievements(update)
    elif section == "market":
        await _do_check_market(update)
    elif section == "bestiary":
        await _do_bestiary(update)
    elif section == "weather":
        await _do_check_weather(update)
    elif section == "leaderboard":
        await _do_leaderboard(update)
    elif section == "party":
        await _do_check_party(update)
    elif section == "visualmap":
        await _do_show_visual_map(update)
    elif section == "rebirth":
        await _do_rebirth(update)
    elif section == "hybrid":
        await _do_show_hybrid_menu(update)


async def story_menu_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handles taps on _story_chapter_keyboard -- re-validates the chapter's actually been reached server-side, never trusts the button alone."""
    query = update.callback_query
    parts = (query.data or "").split("|")
    action = parts[1] if len(parts) > 1 else ""
    await _safe_answer(query)
    if action != "replay" or len(parts) < 3:
        return
    await _do_replay_chapter_intro(update, parts[2])


async def _do_show_story_so_far(update: Update) -> None:
    """
    Task #176 menu revision, per Coffee ("I want that to be like a
    story/novel with actions and narrations making it a logical story"):
    a real AI-narrated recap (narrate_story_so_far), grounded strictly
    in this character's actual completed story arcs, current arc, and
    completed quests (CAMPAIGN's own story_arcs, in their real narrative
    order) -- never invented, same rules-decide/AI-narrates split as
    every other narration in this game. A short structured chapter list
    follows as an at-a-glance reference, with future chapters shown as
    "???" rather than spoiling a locked chapter's title, same fog-of-war
    principle as the map/bestiary.
    """
    character = db.get_character(update.effective_user.id)
    if character is None:
        await update.effective_chat.send_message(
            "You don't have a character yet!", message_thread_id=config.TOPIC_ADVENTURE_ID
        )
        return

    completed_ids = set(character["completed_quests"])
    current = _current_story_arc(character)
    current_arc_id = current[0] if current else None

    completed_arcs = []
    chapter_lines = ["**Chapters:**"]
    for arc_id, arc in CAMPAIGN.get("story_arcs", {}).items():
        arc_quests = set(arc.get("quests", []))
        if arc_quests and arc_quests.issubset(completed_ids):
            completed_arcs.append((arc["title"], arc["description"]))
            chapter_lines.append(f"✅ {arc['title']}")
        elif arc_id == current_arc_id:
            chapter_lines.append(f"▶️ {arc['title']} *(current)*")
        else:
            chapter_lines.append("🔒 ???")

    current_arc_pair = (current[1]["title"], current[1]["description"]) if current else None
    completed_quests = [
        (CAMPAIGN["quests"][q]["title"], CAMPAIGN["quests"][q]["description"])
        for q in character["completed_quests"] if q in CAMPAIGN["quests"]
    ]

    recap = await asyncio.to_thread(
        narrate_story_so_far, character["name"], completed_arcs, current_arc_pair, completed_quests,
    )

    await _safe_send(
        update, f"📖 **Story So Far**\n\n{recap}\n\n" + "\n".join(chapter_lines),
        reply_markup=_with_menu_button(_story_chapter_keyboard(character)),
    )


def _equip_keyboard(character: dict) -> InlineKeyboardMarkup | None:
    """
    Task #176 menu revision: real tap-to-equip buttons for gear this
    character is actually carrying but hasn't equipped yet -- same real
    inventory/equipped-slot data _format_carried_gear_line already reads
    (never a generic list). None (not even a Menu-only keyboard) when
    there's nothing to equip, so _do_show_equip_menu can say so plainly.
    """
    equipped_ids = {character.get("equipped_weapon"), character.get("equipped_armor"),
                    character.get("equipped_shield"), *(character.get("equipped_accessories") or [])}
    buttons = []
    for item_id, qty in (character.get("inventory") or {}).items():
        if qty <= 0 or item_id in equipped_ids:
            continue
        item = items_module.get_item(item_id)
        if item and item.get("type") in ("weapon", "armor", "shield", "ring", "amulet", "wondrous"):
            buttons.append([InlineKeyboardButton(f"⚔️ {item['name']}", callback_data=f"equip|item|{item_id}")])
    return InlineKeyboardMarkup(buttons) if buttons else None


async def _do_show_equip_menu(update: Update) -> None:
    character = db.get_character(update.effective_user.id)
    if character is None:
        await update.effective_chat.send_message(
            "You don't have a character yet!", message_thread_id=config.TOPIC_ADVENTURE_ID
        )
        return
    keyboard = _equip_keyboard(character)
    if keyboard is None:
        await _safe_send(
            update, "⚔️ Nothing in your backpack is equippable right now.", reply_markup=_with_menu_button(None),
            speak=False,
        )
        return
    await _safe_send(
        update, "⚔️ **Equip Gear** — tap something carried but not yet equipped:",
        reply_markup=_with_menu_button(keyboard), speak=False,
    )


async def equip_menu_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handles taps on _equip_keyboard -- dispatches through the same real _do_equip_item free text already uses."""
    query = update.callback_query
    parts = (query.data or "").split("|")
    action = parts[1] if len(parts) > 1 else ""
    await _safe_answer(query)
    if action != "item":
        return
    item_id = parts[2] if len(parts) > 2 else None
    item = items_module.get_item(item_id) if item_id else None
    if item is None:
        return
    await _do_equip_item(update, f"equip {item['name']}")


def _level_keyboard() -> InlineKeyboardMarkup:
    """Real Ability Score Improvement choices -- see _apply_asi_choice, the same real 5E rule this dispatches through."""
    buttons = [[InlineKeyboardButton(a.capitalize(), callback_data=f"level|asi|{a}")] for a in _ABILITY_NAMES]
    buttons.append([InlineKeyboardButton("🎲 Auto (let the game choose)", callback_data="level|asi|auto")])
    return InlineKeyboardMarkup(buttons)


async def _do_show_level_menu(update: Update) -> None:
    """
    Task #176 menu revision, per Coffee ("leveling up skill trees
    anything like that - include it in the menu"). This game's real
    level-up mechanic is XP-driven auto-leveling plus a real player-
    chosen Ability Score Improvement at ASI levels (_do_level_up,
    task #94) -- there's no separate skill-tree system yet (that's
    still backlog task #131), so this surfaces the real thing that
    exists: current level/XP, and real tap-to-choose ASI buttons
    whenever points are actually waiting to be spent.
    """
    character = db.get_character(update.effective_user.id)
    if character is None:
        await update.effective_chat.send_message(
            "You don't have a character yet!", message_thread_id=config.TOPIC_ADVENTURE_ID
        )
        return
    lines = [f"📈 **Level {character['level']}** — {character['xp']} XP"]
    next_threshold = XP_THRESHOLDS.get(character["level"] + 1)
    if next_threshold is not None:
        lines.append(f"{max(next_threshold - character['xp'], 0)} XP to Level {character['level'] + 1}")
    else:
        lines.append("Max level reached.")
    pending = character.get("pending_asi_points", 0)
    if pending:
        lines.append(f"\nYou have {pending} ability point(s) to spend:")
        await _safe_send(update, "\n".join(lines), reply_markup=_with_menu_button(_level_keyboard()), speak=False)
    else:
        lines.append("\nNo ability score improvements waiting to be spent right now.")
        await _safe_send(update, "\n".join(lines), reply_markup=_with_menu_button(None), speak=False)


async def level_menu_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handles taps on _level_keyboard -- dispatches through the same real _do_level_up free text already uses."""
    query = update.callback_query
    parts = (query.data or "").split("|")
    action = parts[1] if len(parts) > 1 else ""
    await _safe_answer(query)
    if action != "asi":
        return
    choice = parts[2] if len(parts) > 2 else None
    if choice is None:
        return
    await _do_level_up(update, choice)


def _spell_keyboard(character: dict) -> InlineKeyboardMarkup | None:
    """
    Task #176 (revised 2026-07-19 per Coffee's direct feedback: "I am
    honestly not sure about having the character sheet have these push
    buttons unless they do something for the player and the party...
    if they want to use a potion or charm a player, they can select it
    pick a target and then executed it... We do not need it unless it
    has a utility") -- out-of-combat spell-casting buttons for a
    character's own real known_spells. Tapping a spell now opens a real
    SECOND step (see spell_menu_callback's "target" branch) letting the
    caster pick themselves or any real party member actually here to
    aim it at, the exact utility Coffee asked for, instead of always
    silently self-casting with no way to say who it's for.
    """
    if not character.get("known_spells"):
        return None
    buttons = []
    for spell_id in character["known_spells"]:
        spell = spells_module.get_spell(spell_id)
        if spell:
            buttons.append([InlineKeyboardButton(f"✨ {spell['name']}", callback_data=f"spell|cast|{spell_id}")])
    return InlineKeyboardMarkup(buttons) if buttons else None


def _target_picker_keyboard(prefix: str, action_id: str, requester: dict) -> InlineKeyboardMarkup | None:
    """
    Shared second-step target picker for _spell_keyboard/_item_keyboard
    (task #176 revision, per Coffee): "Self" plus every other REAL party
    member actually at this location right now (_get_combat_eligible_
    party_members -- same real presence/location grounding the combat
    join-nudge already uses, never a generic/global party list). Only
    built when there's actually someone else here to pick -- callers
    skip straight to a self-cast/self-use otherwise, so this never adds
    an empty, pointless extra tap.
    """
    eligible = _get_combat_eligible_party_members(requester["current_location"])
    others = [p for p in eligible if p["telegram_user_id"] != requester["telegram_user_id"]]
    if not others:
        return None
    buttons = [[InlineKeyboardButton("🧍 Self", callback_data=f"{prefix}|target|{action_id}|self")]]
    buttons += [
        [InlineKeyboardButton(p["name"], callback_data=f"{prefix}|target|{action_id}|{p['telegram_user_id']}")]
        for p in others
    ]
    return InlineKeyboardMarkup(buttons)


async def spell_menu_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handles taps on _spell_keyboard and its target picker -- see their docstrings."""
    query = update.callback_query
    parts = (query.data or "").split("|")
    action = parts[1] if len(parts) > 1 else ""
    await _safe_answer(query)

    if action == "cast":
        spell_id = parts[2] if len(parts) > 2 else None
        spell = spells_module.get_spell(spell_id) if spell_id else None
        character = db.get_character(update.effective_user.id)
        if spell is None or character is None:
            return
        picker = _target_picker_keyboard("spell", spell_id, character)
        if picker is None:
            await _do_cast_spell(update, f"cast {spell['name']}")
            return
        await query.edit_message_reply_markup(reply_markup=picker)
        return

    if action == "target":
        spell_id = parts[2] if len(parts) > 2 else None
        target_token = parts[3] if len(parts) > 3 else None
        spell = spells_module.get_spell(spell_id) if spell_id else None
        if spell is None or target_token is None:
            return
        if target_token == "self":
            await _do_cast_spell(update, f"cast {spell['name']}")
            return
        target_character = db.get_character(int(target_token))
        if target_character is None:
            return
        await _do_cast_spell(update, f"cast {spell['name']} on {target_character['name']}")


async def _do_cast_spell(update: Update, text: str) -> None:
    character = db.get_character(update.effective_user.id)
    if character is None:
        await update.effective_chat.send_message(
            "You don't have a character yet!", message_thread_id=config.TOPIC_ADVENTURE_ID
        )
        return

    spell_id = None
    lowered = text.lower()
    for candidate in character["known_spells"]:
        spell = spells_module.get_spell(candidate)
        if spell and (candidate.replace("_", " ") in lowered or spell["name"].lower() in lowered):
            spell_id = candidate
            break

    # Not a spell this character knows — but a scroll in their own
    # backpack lets anyone use its one spell regardless, same as real
    # 5E scrolls. Consumed on a successful cast, no spell slot spent
    # (the scroll IS the resource being spent).
    via_scroll = False
    scroll_item_id = None
    if spell_id is None:
        for item_id, qty in character["inventory"].items():
            if qty <= 0:
                continue
            item_data = items_module.get_item(item_id)
            if not item_data or item_data.get("type") != "scroll":
                continue
            candidate = item_data.get("spell")
            spell = spells_module.get_spell(candidate) if candidate else None
            if spell and (candidate.replace("_", " ") in lowered or spell["name"].lower() in lowered):
                spell_id, via_scroll, scroll_item_id = candidate, True, item_id
                break

    if spell_id is None:
        known = ", ".join(character["known_spells"]) or "none yet"
        await update.effective_chat.send_message(
            f"You don't know a spell by that name, and don't have a scroll for it either. "
            f"Spells you know: {known}",
            message_thread_id=config.TOPIC_ADVENTURE_ID,
        )
        return

    spell = spells_module.get_spell(spell_id)
    chat_id = update.effective_chat.id

    # Silenced (2026-07-13): this game doesn't model spell components, so
    # rather than special-case which spells need a verbal component (real
    # 5E: almost all of them), silenced simply blocks casting outright,
    # the same blanket-condition simplification prone/poisoned already
    # use for their effects. Only meaningful mid-combat, since conditions
    # only ever exist on an active session participant.
    async with sessions.get_lock(chat_id):
        session = sessions.get_session(chat_id)
        caster = (
            next((p for p in session.participants if p["telegram_user_id"] == update.effective_user.id), None)
            if session else None
        )
        if caster and "silenced" in caster.get("conditions", []):
            await update.effective_chat.send_message(
                f"🔇 **{character['name']}** can't get the words out — silenced!",
                message_thread_id=config.TOPIC_ADVENTURE_ID,
            )
            return
        # Real 5E: a Druid can't cast spells while Wild Shaped (task #91
        # audit, 2026-07-19) -- same combat-only, in-memory condition
        # check as `silenced` above, reading the `wild_shaped` flag
        # _do_wild_shape sets on this same participant dict.
        if caster and caster.get("wild_shaped"):
            await update.effective_chat.send_message(
                f"🐾 **{character['name']}** is Wild Shaped — no hands, no spellcasting until "
                f"they shift back.",
                message_thread_id=config.TOPIC_ADVENTURE_ID,
            )
            return

    def _consume_scroll_if_any() -> None:
        if via_scroll:
            db.remove_item(update.effective_user.id, scroll_item_id, 1)

    if spell["effect"] == "damage":
        async with sessions.get_lock(chat_id):
            session = sessions.get_session(chat_id)
            if session is None:
                await update.effective_chat.send_message(
                    "There's nothing to cast that at right now.", message_thread_id=config.TOPIC_ADVENTURE_ID
                )
                return
            if session.current_participant_id() != update.effective_user.id:
                await _self_heal_stuck_ai_turn(update, session)
                session = sessions.get_session(chat_id)
                if session is None:
                    await update.effective_chat.send_message(
                        "Combat had stalled and just resolved itself — nothing active right now.",
                        message_thread_id=config.TOPIC_ADVENTURE_ID,
                    )
                    return
            if session.current_participant_id() != update.effective_user.id:
                current_name = session.current_participant()["name"]
                await update.effective_chat.send_message(
                    f"It's not your turn — it's **{current_name}**'s turn.",
                    message_thread_id=config.TOPIC_ADVENTURE_ID,
                )
                return
            if character["hp_current"] <= 0:
                await update.effective_chat.send_message(
                    "You're unconscious (0 HP) and can't act until healed.",
                    message_thread_id=config.TOPIC_ADVENTURE_ID,
                )
                return
            opposing = session.living_on_side(session.opposing_side(update.effective_user.id))
            if not opposing:
                if not await _try_end_stale_combat(update, session):
                    await update.effective_chat.send_message(
                        "No valid targets remain.", message_thread_id=config.TOPIC_ADVENTURE_ID
                    )
                return

            # Only spend a slot once we KNOW the cast is actually valid —
            # cantrips (level 0) are free/unlimited per real 5E rules.
            # A scroll-cast spends the scroll instead of a slot.
            if not via_scroll and spell["level"] > 0:
                spent, _ = db.spend_spell_slot(update.effective_user.id)
                if not spent:
                    await update.effective_chat.send_message(
                        f"You have no spell slots remaining to cast {spell['name']} "
                        f"({character['spell_slots_current']}/{character['spell_slots_max']} left). "
                        f"Rest to recover them.",
                        message_thread_id=config.TOPIC_ADVENTURE_ID,
                    )
                    return
            _consume_scroll_if_any()

            target = _pick_target(text, opposing)
            result = spells_module.resolve_damage_spell(spell_id, character, target)
            result = _apply_empowered_spell(update.effective_user.id, character, spell, result)
            target["hp_current"] = max(target["hp_current"] - result["damage_dealt"], 0)
            _sync_player_to_db(target)
            full_result = {
                **result, "attacker": character["name"], "defender": target["name"],
                "hit": True, "defender_hp_remaining": target["hp_current"],
                "defender_hp_max": target.get("hp_max", target["hp_current"]),
            }
            await _post_narrated(update, character, text, full_result, session, action_label=spell["name"])

            removed = session.remove_defeated()
            await _announce_defeats(update, session, removed)

            if session.is_combat_over():
                winner = _determine_winner(session)
                xp_summary, level_up_notes = _award_victory_xp(session) if winner == "party" else ("", [])
                if winner == "party":
                    await _check_quest_completions_defeat_monster(update, session)
                    await _check_achievements_for_combat_party(update, session)
                    await _check_guild_quest_completion(update, session)
                await update.effective_chat.send_message(
                    f"🏆 **Combat over!** The {winner} side is victorious!{xp_summary}",
                    message_thread_id=config.TOPIC_ADVENTURE_ID,
                )
                for note in level_up_notes:
                    await _notify_main_topic(update, note)
                sessions.end_session(chat_id)
                return
            session.advance_turn()
            await _resolve_ai_turns(update, session)

    elif spell["effect"] == "heal":
        # Support spells (heal/cure) can target ANY party member by name,
        # including one currently resting/inactive — per design, an
        # inactive character can't act but can still be helped. Defaults
        # to self if no other party member is named in the text.
        target_character = _find_party_target_by_name(text) or character
        if target_character.get("is_dead"):
            await update.effective_chat.send_message(
                f"**{target_character['name']}** is dead, not just hurt — {spell['name']} won't bring them back. "
                f"Revivify (or a Scroll of Revivify) is what's needed.",
                message_thread_id=config.TOPIC_ADVENTURE_ID,
            )
            return
        if not via_scroll and spell["level"] > 0:
            spent, _ = db.spend_spell_slot(update.effective_user.id)
            if not spent:
                await update.effective_chat.send_message(
                    f"You have no spell slots remaining to cast {spell['name']} "
                    f"({character['spell_slots_current']}/{character['spell_slots_max']} left). "
                    f"Rest to recover them.",
                    message_thread_id=config.TOPIC_ADVENTURE_ID,
                )
                return
        _consume_scroll_if_any()
        result = spells_module.resolve_heal_spell(spell_id, character, target_character)
        db.update_character(target_character["telegram_user_id"], hp_current=target_character["hp_current"])
        is_self = target_character["telegram_user_id"] == character["telegram_user_id"]
        target_note = "" if is_self else f" on **{target_character['name']}**"
        inactive_note = " (resting)" if target_character.get("is_inactive") else ""
        await _safe_send(
            update,
            f"✨ **{character['name']}** casts {spell['name']}{target_note}{inactive_note} and heals {result['healing_done']} HP "
            f"({result['hp_current']}/{result['hp_max']}).",
        )

    elif spell["effect"] == "resurrect":
        # Revivify (2026-07-14, per Coffee): the real way a dead
        # character (see the "dead" death-save outcome in the main
        # combat loop) gets a second chance. Works outside combat, same
        # as heal -- reviving happens after the fight, not mid-round.
        # Real 5E requires a costly diamond component and a 1-minute
        # window since death; this build tracks neither real-world
        # minutes tightly enough nor material components at all, so
        # both are deliberately skipped, same simplification style as
        # every other adapted spell/feature in this file. Restores to
        # 1 HP, matching Revivify's real effect exactly.
        target_character = _find_party_target_by_name(text)
        if target_character is None or not target_character.get("is_dead"):
            await update.effective_chat.send_message(
                "Name a dead party member to revive — there's no one to bring back right now.",
                message_thread_id=config.TOPIC_ADVENTURE_ID,
            )
            return
        if not via_scroll and spell["level"] > 0:
            spent, _ = db.spend_spell_slot(update.effective_user.id)
            if not spent:
                await update.effective_chat.send_message(
                    f"You have no spell slots remaining to cast {spell['name']} "
                    f"({character['spell_slots_current']}/{character['spell_slots_max']} left). "
                    f"Rest to recover them.",
                    message_thread_id=config.TOPIC_ADVENTURE_ID,
                )
                return
        _consume_scroll_if_any()
        db.update_character(
            target_character["telegram_user_id"], is_dead=0, hp_current=1,
            death_save_successes=0, death_save_failures=0,
        )
        await _safe_send(
            update,
            f"✨ **{character['name']}** casts {spell['name']} on **{target_character['name']}** — "
            f"breath returns, and they gasp back to life at 1 HP.",
        )

    elif spell["effect"] == "summon":
        async with sessions.get_lock(chat_id):
            session = sessions.get_session(chat_id)
            if session is None:
                await update.effective_chat.send_message(
                    "There's nothing to summon into right now — this only works in combat.",
                    message_thread_id=config.TOPIC_ADVENTURE_ID,
                )
                return
            if not via_scroll and spell["level"] > 0:
                spent, _ = db.spend_spell_slot(update.effective_user.id)
                if not spent:
                    await update.effective_chat.send_message(
                        f"You have no spell slots remaining to cast {spell['name']} "
                        f"({character['spell_slots_current']}/{character['spell_slots_max']} left). "
                        f"Rest to recover them.",
                        message_thread_id=config.TOPIC_ADVENTURE_ID,
                    )
                    return
            _consume_scroll_if_any()

            stats = spell["summon_stats"]
            # -4_000_000 range: distinct from _do_start_combat's -2_000_000
            # monsters and _npc_combatant_from_stats's -3_000_000 hostile
            # NPCs, so a summon's synthetic id can never collide with
            # either inside the same session's participant list.
            synthetic_id = -4_000_000 - (abs(hash((chat_id, character["name"], session.round_number))) % 100_000)
            summon = {
                "telegram_user_id": synthetic_id, "name": stats["name"].title(),
                "dexterity": stats["dexterity"], "strength": stats["strength"],
                "armor_class": stats["armor_class"], "hp_current": stats["hp_max"],
                "hp_max": stats["hp_max"], "proficiency_bonus": stats["proficiency_bonus"],
                "is_ai": 1, "xp_reward": 0,
            }
            summon["initiative"] = roll_d20() + ability_modifier(stats["dexterity"])
            session.participants.append(summon)
            session.turn_order.append(synthetic_id)
            session.sides[synthetic_id] = "party"
            session.log_event(f"{character['name']} summons {summon['name']} to fight alongside the party!")

        await _safe_send(
            update,
            f"🌀 **{character['name']}** casts {spell['name']} — **{summon['name']}** answers the call and joins the fight "
            f"(HP: {summon['hp_max']}, AC: {summon['armor_class']}). It vanishes once combat ends.",
        )

    else:
        if not via_scroll and spell["level"] > 0:
            spent, _ = db.spend_spell_slot(update.effective_user.id)
            if not spent:
                await update.effective_chat.send_message(
                    f"You have no spell slots remaining to cast {spell['name']} "
                    f"({character['spell_slots_current']}/{character['spell_slots_max']} left). "
                    f"Rest to recover them.",
                    message_thread_id=config.TOPIC_ADVENTURE_ID,
                )
                return
        _consume_scroll_if_any()
        buff_target = _find_party_target_by_name(text)
        target_note = f" on **{buff_target['name']}**" if buff_target else ""
        await _safe_send(
            update,
            f"✨ **{character['name']}** casts {spell['name']}{target_note}. (Note: this spell's flavor is real, but it "
            f"doesn't yet apply a mechanical effect in this build — that's a known "
            f"limitation, not a bug.)",
        )


async def _do_join_guild(update: Update, text: str) -> None:
    character = db.get_character(update.effective_user.id)
    if character is None:
        await update.effective_chat.send_message(
            "You don't have a character yet!", message_thread_id=config.TOPIC_ADVENTURE_ID
        )
        return

    # Real live bug (2026-07-21, Coffee): "adventurers' guild" (an iPhone
    # keyboard's curly apostrophe, U+2019) never matched the stored
    # straight-quote "Adventurers' Guild" name -- same single-character
    # collision _find_interactable already normalizes for. Also strip
    # apostrophes entirely before comparing so "adventurers guild" (no
    # apostrophe at all) still resolves.
    lowered = text.lower().replace("’", "'")
    guild_id = None
    for gid, guild in GUILDS.items():
        candidates = {gid.replace("_", " "), guild["name"].lower()}
        candidates = {c.replace("'", "") for c in candidates} | candidates
        if any(c in lowered or c in lowered.replace("'", "") for c in candidates):
            guild_id = gid
            break

    # Real live bug (2026-07-21, same session): "Ask to join adventures
    # guild" -- a genuine one-letter typo ("adventures" for
    # "adventurers"), not a formatting difference the exact checks
    # above can catch -- also matched nothing. A plain substring check
    # can never forgive a misspelled word, so fall back to fuzzy word
    # matching (stdlib difflib, no new dependency) against each guild's
    # own significant name words, same "don't guess when ambiguous"
    # shape as _find_interactable's word-overlap fallback: only trusted
    # above a high similarity ratio, and only the single best match
    # wins.
    if guild_id is None:
        input_words = re.findall(r"[a-z]+", lowered)
        best_gid, best_ratio = None, 0.0
        for gid, guild in GUILDS.items():
            for name_word in re.findall(r"[a-z]+", guild["name"].lower()):
                if len(name_word) < 5:
                    continue
                for input_word in input_words:
                    ratio = difflib.SequenceMatcher(None, name_word, input_word).ratio()
                    if ratio > best_ratio:
                        best_gid, best_ratio = gid, ratio
        if best_ratio >= 0.82:
            guild_id = best_gid

    if guild_id is None:
        names = ", ".join(g["name"] for g in GUILDS.values())
        await update.effective_chat.send_message(
            f"Not sure which guild you mean. Guilds: {names}",
            message_thread_id=config.TOPIC_ADVENTURE_ID,
        )
        return

    eligible, reason = eligible_for_guild(character, guild_id)
    if not eligible:
        await update.effective_chat.send_message(
            f"You can't join {GUILDS[guild_id]['name']} yet: {reason}",
            message_thread_id=config.TOPIC_ADVENTURE_ID,
        )
        return

    db.join_guild(update.effective_user.id, guild_id)
    join_note = ""
    # Arcane Circle's bonus_spell_scroll benefit (guilds.py) was flavor
    # text with nothing checking it -- a real, immediate welcome gift is
    # the simplest honest reading of "bonus spell scroll" for a
    # membership benefit, same spirit as Silver Wardens' combat bonus
    # and Cleric's Disciple of Life being real rather than described.
    if "bonus_spell_scroll" in GUILDS[guild_id]["benefits"]:
        db.add_item(update.effective_user.id, "scroll_magic_missile", 1)
        join_note = " They welcome you with a free Scroll of Magic Missile."
    topic_note = " Check the guild's own topic for member-only chat and today's guild quest." if config.GUILD_TOPIC_IDS.get(guild_id) else ""
    await _safe_send(update, f"🏛️ You've joined {GUILDS[guild_id]['name']}!{join_note}{topic_note}")
    await _notify_main_topic(update, f"🏛️ **{character['name']}** joined {GUILDS[guild_id]['name']}!")
    await _check_and_award_achievements(update, db.get_character(update.effective_user.id))


# ---------------------------------------------------------------------
# Waypoints — tap-to-travel buttons over the exact same fast-travel
# destinations _do_fast_travel already accepts as free text (any
# location in character['visited_locations']). Per Coffee (2026-07-20):
# "Can you add waypoint to the menu also."
# ---------------------------------------------------------------------

def _waypoint_keyboard(visited_locations: list[str], current_location_id: str) -> InlineKeyboardMarkup:
    rows = []
    for loc_id in visited_locations:
        if loc_id == current_location_id:
            continue
        loc = cl.get_location(CAMPAIGN, loc_id)
        if loc:
            rows.append([InlineKeyboardButton(loc["name"], callback_data=f"waypoint|go|{loc_id}")])
    return InlineKeyboardMarkup(rows)


async def _do_show_waypoints(update: Update) -> None:
    character = db.get_character(update.effective_user.id)
    if character is None:
        await update.effective_chat.send_message(
            "You don't have a character yet!", message_thread_id=config.TOPIC_ADVENTURE_ID
        )
        return
    visited = character["visited_locations"]
    others = [loc_id for loc_id in visited if loc_id != character["current_location"]]
    if not others:
        await update.effective_chat.send_message(
            "You haven't discovered anywhere else to fast-travel to yet.",
            message_thread_id=config.TOPIC_ADVENTURE_ID,
        )
        return
    await _safe_send(
        update, "🧭 **Waypoints** — tap one to fast-travel there:",
        reply_markup=_waypoint_keyboard(visited, character["current_location"]),
        speak=False,
    )


async def waypoint_menu_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handles taps on _waypoint_keyboard -- dispatches through the exact same _do_fast_travel a typed destination name already uses."""
    query = update.callback_query
    parts = (query.data or "").split("|")
    action = parts[1] if len(parts) > 1 else ""
    await _safe_answer(query)
    if action != "go" or len(parts) < 3:
        return
    loc_id = parts[2]
    loc = cl.get_location(CAMPAIGN, loc_id)
    if loc is None:
        return
    await _do_fast_travel(update, loc["name"])


# ---------------------------------------------------------------------
# Character slots — a telegram user can own multiple characters; exactly
# one is "active" (db.get_character resolves to it) at a time.
# ---------------------------------------------------------------------

def _roster_keyboard(roster: list[dict], active_id: int | None) -> InlineKeyboardMarkup:
    """
    Tap-to-switch buttons, one per character (task #213, per Coffee:
    "put an option to be able to see those characters and switch them")
    -- same real db.switch_character this already dispatches through via
    free text, just reachable by tap too. The already-active character
    still gets a button (a harmless no-op switch to itself) rather than
    being singled out as unclickable, keeping the row count predictable.
    """
    rows = [
        [InlineKeyboardButton(
            f"{'📍 ' if c['character_id'] == active_id else ''}{c['name']} (Lv {c['level']} {c['char_class']})",
            callback_data=f"roster|switch|{c['character_id']}",
        )]
        for c in roster
    ]
    rows.append([InlineKeyboardButton("✨ Create New Character", callback_data="roster|new")])
    return InlineKeyboardMarkup(rows)


async def _do_list_characters(update: Update) -> None:
    roster = db.list_characters(update.effective_user.id)
    if not roster:
        await update.effective_chat.send_message(
            "You don't have any characters yet — say 'I want to create a character' to get started!",
            message_thread_id=config.TOPIC_ADVENTURE_ID,
        )
        return

    active = db.get_character(update.effective_user.id)
    active_id = active["character_id"] if active else None
    lines = ["🎭 **Your characters:** (tap one to switch)"]
    for c in roster:
        marker = "📍 " if c["character_id"] == active_id else "   "
        lines.append(
            f"{marker}{c['name']} — Level {c['level']} {c['race']} {c['char_class']}"
        )
    await _safe_send(update, "\n".join(lines), reply_markup=_roster_keyboard(roster, active_id), speak=False)


async def roster_menu_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handles taps on _roster_keyboard -- dispatches through the same real switch_character/combat-lock checks _do_switch_character already applies to free text."""
    query = update.callback_query
    parts = (query.data or "").split("|")
    action = parts[1] if len(parts) > 1 else ""
    await _safe_answer(query)
    if action == "new":
        await _begin_character_creation(update, context)
        return
    if action != "switch" or len(parts) < 3:
        return
    try:
        character_id = int(parts[2])
    except ValueError:
        return

    telegram_user_id = update.effective_user.id
    roster = db.list_characters(telegram_user_id)
    if not any(c["character_id"] == character_id for c in roster):
        # Real dev-topic feedback (2026-07-21, Coffee): "make sure only
        # the player it's meant for can interact with pop-up buttons" --
        # this specific button is another real player's own roster
        # entry, tapped by someone it was never meant for. This already
        # couldn't switch anyone's ACTIVE character out from under them
        # (roster is looked up by the clicker's own telegram_user_id,
        # never the button's), but it used to fail completely silently,
        # indistinguishable from a random dead tap. A clear line instead
        # tells a wrong tapper plainly why nothing happened.
        await _safe_send(update, "That's not one of your characters.")
        return
    if sessions.get_session(update.effective_chat.id) is not None:
        await update.effective_chat.send_message(
            "You can't switch characters in the middle of combat.",
            message_thread_id=config.TOPIC_ADVENTURE_ID,
        )
        return
    switched = db.switch_character(telegram_user_id, character_id)
    await _safe_send(
        update,
        f"🎭 Switched to **{switched['name']}** the {switched['race']} {switched['char_class']} "
        f"(Level {switched['level']}).",
    )


def _find_own_character_by_name_fragment(telegram_user_id: int, fragment: str) -> dict | None:
    lowered = fragment.strip().lower()
    # Defense in depth (2026-07-19/20, real bug): a caller-side filler
    # word left in front of the real name (e.g. "my character elduinn")
    # otherwise never matches, since the checks below only look for the
    # fragment being equal to or a substring of the real name -- never
    # the reverse. Stripped here too, not just at the keyword_fallback
    # trigger, so any other caller passing a similarly-prefixed fragment
    # is covered as well.
    for filler in ("my character ", "character "):
        if lowered.startswith(filler):
            lowered = lowered[len(filler):]
            break
    for c in db.list_characters(telegram_user_id):
        if lowered == c["name"].lower() or lowered in c["name"].lower():
            return c
    return None


async def _do_switch_character(update: Update, text: str) -> None:
    match = _find_own_character_by_name_fragment(update.effective_user.id, text)
    if match is None:
        roster = db.list_characters(update.effective_user.id)
        names = ", ".join(c["name"] for c in roster) if roster else "none yet"
        await update.effective_chat.send_message(
            f"Couldn't find one of your characters by that name. Your characters: {names}",
            message_thread_id=config.TOPIC_ADVENTURE_ID,
        )
        return

    if sessions.get_session(update.effective_chat.id) is not None:
        await update.effective_chat.send_message(
            "You can't switch characters in the middle of combat.",
            message_thread_id=config.TOPIC_ADVENTURE_ID,
        )
        return

    switched = db.switch_character(update.effective_user.id, match["character_id"])
    await _safe_send(
        update,
        f"🎭 Switched to **{switched['name']}** the {switched['race']} {switched['char_class']} "
        f"(Level {switched['level']}).",
    )


async def _do_delete_character(update: Update, text: str) -> None:
    match = _find_own_character_by_name_fragment(update.effective_user.id, text)
    if match is None:
        roster = db.list_characters(update.effective_user.id)
        names = ", ".join(c["name"] for c in roster) if roster else "none yet"
        await update.effective_chat.send_message(
            f"Couldn't find one of your characters by that name. Your characters: {names}",
            message_thread_id=config.TOPIC_ADVENTURE_ID,
        )
        return

    if sessions.get_session(update.effective_chat.id) is not None:
        await update.effective_chat.send_message(
            "You can't delete a character in the middle of combat.",
            message_thread_id=config.TOPIC_ADVENTURE_ID,
        )
        return

    db.delete_character(update.effective_user.id, match["character_id"])
    remaining = db.get_character(update.effective_user.id)
    note = (
        f" **{remaining['name']}** is now your active character."
        if remaining else " You have no characters left — say 'I want to create a character' to start fresh."
    )
    await update.effective_chat.send_message(
        f"🗑️ Deleted **{match['name']}**.{note}",
        message_thread_id=config.TOPIC_ADVENTURE_ID,
    )


# ---------------------------------------------------------------------
# Master natural-language handler for the Adventure topic
# ---------------------------------------------------------------------

def _find_npc_id_by_name(name: str) -> str | None:
    """
    Looks up an NPC's internal id from its real display name, since ids
    and names don't always match (e.g. 'sera_wanderer' -> 'Sarah'). Never
    assume npc_id == name.lower() — always go through this lookup.
    """
    for npc_id, data in CAMPAIGN["npcs"].items():
        if data["name"].lower() == name.lower() or npc_id.lower() == name.lower():
            return npc_id
    return None


def _shop_items_for_npc(npc_id: str) -> list[dict]:
    """
    Real wares a shopkeeper NPC actually sells, resolved from
    campaign.json's shops + items.py — used to ground that NPC's
    dialogue (see ai/npc_agent.py's _shop_grounding_block) so asking
    them what they have for sale gets the REAL inventory instead of
    whatever the model invents.
    """
    items_out = []
    for shop_data in CAMPAIGN.get("shops", {}).values():
        if shop_data.get("owner_npc") != npc_id:
            continue
        for item_id in shop_data.get("inventory", []):
            item = items_module.get_item(item_id)
            if item is None:
                continue
            note_parts = []
            if item.get("heal_dice"):
                note_parts.append(f"heals {item['heal_dice']}")
            if item.get("damage_dice"):
                note_parts.append(f"damage {item['damage_dice']}")
            if item.get("effect") and item["effect"] not in ("none", "heal"):
                note_parts.append(item["effect"].replace("_", " "))
            if item.get("note"):
                note_parts.append(item["note"])
            items_out.append({
                "name": item["name"], "price": item.get("price", 0),
                "note": "; ".join(note_parts) if note_parts else "",
            })
    return items_out


def setup_default_npcs() -> None:
    for npc_id, npc_data in CAMPAIGN["npcs"].items():
        register_npc(
            npc_id, npc_data["name"], npc_data["personality"],
            goals=npc_data.get("goals", ""),
            alignment=npc_data.get("alignment", ""),
            disposition=npc_data.get("disposition", "friendly"),
            shop_items=_shop_items_for_npc(npc_id),
        )
    _seed_npc_locations()


UNIVERSAL_ESCAPE_PHRASES = {"cancel", "start over", "nevermind", "never mind", "stop", "reset"}


def _clear_all_stateful_flows(context: ContextTypes.DEFAULT_TYPE, user_id: int) -> bool:
    """
    Clears any in-progress multi-step conversation state for this user —
    character creation today, and any future stateful flows (trading,
    guild applications, etc.) as they're added later. Returns True if
    something was actually cleared, False if there was nothing active.
    """
    cleared = False
    for key in ("creation",):  # extend this tuple as new stateful flows are added
        if key in context.user_data:
            del context.user_data[key]
            cleared = True
    if _PENDING_DICE_ROLLS.pop(user_id, None) is not None:
        cleared = True
    if user_id in _PENDING_ASI_CHOICE:
        _PENDING_ASI_CHOICE.discard(user_id)
        cleared = True
    return cleared


async def adventure_master_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """
    The single natural-language entry point for everything that happens
    in the Adventure topic. No slash commands required — free text is
    classified by ai/intent_parser.py, then routed to the same
    deterministic game functions used everywhere else in this file.
    """
    if update.message is None or not update.message.text:
        return
    if not topics.is_adventure(update.message.message_thread_id or 0):
        return
    # Banned players (2026-07-17, per Coffee: "admins can absolutely ban
    # malicious players") get zero engagement -- checked before any
    # other processing, including touch_last_active, so a ban is a real
    # dead end, not just a quieter version of playing.
    if await asyncio.to_thread(db.is_banned, update.effective_user.id):
        return

    global _LAST_KNOWN_CHAT_ID
    _LAST_KNOWN_CHAT_ID = update.effective_chat.id
    # asyncio.to_thread (2026-07-17, task #146): db.py's sqlite calls are
    # all synchronous, and this is a single-threaded asyncio event loop --
    # a slow disk write here used to freeze EVERY handler for every
    # player, not just this one. Confirmed live: an ordinary "look
    # around" took ~14s post-deploy with zero getUpdates logged during
    # the gap, i.e. the whole loop was blocked on these two writes, not
    # on anything AI/classification-related.
    await asyncio.to_thread(db.touch_last_active, update.effective_user.id)
    # Login streak (task #78): checked from this same real-activity
    # checkpoint, once per real calendar day -- db.update_login_streak
    # itself no-ops for AI characters and for a player already counted
    # today, so this is safe to call unconditionally on every message.
    streak_result = await asyncio.to_thread(db.update_login_streak, update.effective_user.id)
    if streak_result and streak_result[1]:
        await _maybe_award_streak_bonus(update, streak_result[0])
    # getattr, not .username directly (2026-07-17, real live bug just
    # caught): AI companions' autonomous turns route through this exact
    # handler via a synthetic _AiPlayerUpdate whose _User stand-in only
    # has .id, no .username at all -- a bare attribute access crashed
    # every AI companion's turn with AttributeError the moment this
    # line shipped, confirmed live ("Zara Windrift's autonomous turn
    # raised: AttributeError").
    await asyncio.to_thread(
        db.update_telegram_username, update.effective_user.id, getattr(update.effective_user, "username", None)
    )
    _IDLE_WARNED.discard(update.effective_user.id)

    # Universal escape hatch, checked FIRST, before any stateful flow gets
    # a chance to swallow the message. Exact-match only (never a substring
    # check) so ordinary gameplay text like "I stop to look around" or
    # "I reset the trap" is never misread as a cancel command.
    text_exact = update.message.text.strip().lower()
    if text_exact in UNIVERSAL_ESCAPE_PHRASES:
        had_active_state = _clear_all_stateful_flows(context, update.effective_user.id)

        chat_id = update.effective_chat.id
        session_active = sessions.get_session(chat_id) is not None

        # "cancel" no longer walks a player out of a real fight — that's
        # what fleeing (a real dice roll, and impossible against a boss)
        # is for. It's still allowed as a genuine stuck-session recovery
        # tool, but only for the verified group owner, so it can't
        # double as a free escape hatch for anyone mid-combat.
        if session_active:
            is_owner = await _is_group_owner(update, context)
            if is_owner:
                async with sessions.get_lock(chat_id):
                    if sessions.get_session(chat_id) is not None:
                        sessions.end_session(chat_id)
                await update.effective_chat.send_message(
                    "Force-ended the stuck combat session (owner override). You're free to act again.",
                    message_thread_id=config.TOPIC_ADVENTURE_ID,
                )
                return
            await update.effective_chat.send_message(
                "You can't just walk away from a fight — try to flee (a real risk, and impossible "
                "against some enemies), or see it through.",
                message_thread_id=config.TOPIC_ADVENTURE_ID,
            )
            return

        if had_active_state:
            msg = "Cancelled. Say 'I want to create a character' whenever you're ready to try again."
        else:
            msg = "There's nothing in progress to cancel right now."

        await update.effective_chat.send_message(msg, message_thread_id=config.TOPIC_ADVENTURE_ID)
        return

    # If this user is mid-character-creation, that flow owns their next message.
    if "creation" in context.user_data:
        await _continue_character_creation(update, context)
        return

    # Physical-dice mode (2026-07-16): if this user was just asked to
    # roll their own d20, their next message owns resuming that action
    # with the real number they report, rather than going through
    # normal intent parsing. Updated 2026-07-19 (per Coffee: "give the
    # user 1 minute to roll - if not then auto-roll") -- no longer waits
    # indefinitely; _maybe_auto_roll_pending_dice (idle loop, 60s
    # cadence) auto-resolves anything still pending after
    # DICE_ROLL_AUTO_TIMEOUT_SECONDS, so this branch below only ever
    # fires for a genuinely prompt manual reply.
    pending_roll = _PENDING_DICE_ROLLS.get(update.effective_user.id)
    if pending_roll is None:
        # Observability for a real gap found live 2026-07-18 (Coffee: "i
        # did my roll of 9 and didnt get a reply"): _PENDING_DICE_ROLLS is
        # in-memory only, so a bot restart landing between the roll
        # prompt and the player's reply silently wipes it -- their next
        # message (just a bare number) then falls through to normal
        # intent parsing, which has nothing sensible to do with "9" and
        # can end up silent. There was previously no log line at all for
        # this, so check_topic_activity.py's monitoring had no way to
        # ever see it. This doesn't fix the underlying fragility (that
        # would mean persisting pending state across restarts), but it
        # at least makes a dropped reply traceable after the fact.
        possible_orphaned_roll = _extract_manual_roll(update.message.text) is not None
        if possible_orphaned_roll and len(update.message.text.strip()) <= 4:
            logger.warning(
                f"[dice] user={update.effective_user.id} sent a bare number "
                f"({update.message.text.strip()!r}) with no pending roll on file -- "
                f"likely a manual-dice reply orphaned by a restart between the "
                f"prompt and this message."
            )
    if pending_roll is not None:
        manual_value = _extract_manual_roll(update.message.text)
        if manual_value is None:
            await update.effective_chat.send_message(
                "I didn't catch a number 1-20 in that — what did you roll?",
                message_thread_id=config.TOPIC_ADVENTURE_ID,
            )
            return
        del _PENDING_DICE_ROLLS[update.effective_user.id]
        logger.info(f"[dice] user={update.effective_user.id} resolved pending {pending_roll['kind']} roll with {manual_value}")
        if pending_roll["kind"] == "attack":
            await _do_attack(update, pending_roll["action_text"], forced_roll=manual_value)
        elif pending_roll["kind"] == "skill_check":
            await _do_skill_check(
                update, pending_roll["ability"], pending_roll["action_text"], forced_roll=manual_value
            )
        elif pending_roll["kind"] == "gather":
            await _do_gather(update, pending_roll["action_text"], forced_roll=manual_value)
        elif pending_roll["kind"] == "shove":
            await _do_shove(update, pending_roll["action_text"], forced_roll=manual_value)
        elif pending_roll["kind"] == "flee":
            await _do_flee(update, pending_roll["action_text"], forced_roll=manual_value)
        elif pending_roll["kind"] == "steal":
            await _do_steal(update, pending_roll["action_text"], forced_roll=manual_value)
        return

    # Real player-driven ASI (2026-07-16): resume a pending "which
    # ability" prompt the same way the dice-roll flow above resumes --
    # checked here, before normal intent parsing, so an unrelated later
    # message is never misread as a stat choice; only asked once, and
    # only while pending_asi_points is genuinely still unspent.
    if update.effective_user.id in _PENDING_ASI_CHOICE:
        await _do_level_up(update, update.message.text)
        return

    if update.effective_user.id in _PENDING_DESCRIPTION:
        _PENDING_DESCRIPTION.discard(update.effective_user.id)
        # Real bug found 2026-07-17 (incidentally, while building the
        # pronouns feature): from_prompt was never passed here, so a
        # plain reply to "what would you like your description to be?"
        # (no literal "description: ..." colon) failed
        # _extract_inline_description, came back None, and silently
        # re-asked the SAME question forever instead of saving the
        # answer -- the only path that ever worked was a test calling
        # _do_set_description directly with from_prompt=True, which
        # never exercises this real dispatch line.
        await _do_set_description(update, update.message.text, from_prompt=True)
        return

    if update.effective_user.id in _PENDING_PRONOUNS:
        _PENDING_PRONOUNS.discard(update.effective_user.id)
        await _do_set_pronouns(update, update.message.text, from_prompt=True)
        return

    text = update.message.text.strip()
    _LAST_TOPIC_MESSAGE[(update.effective_chat.id, config.TOPIC_ADVENTURE_ID)] = {
        "kind": "adventure",
        "update": update,
        "context": context,
        "text": text,
        "user_id": update.effective_user.id,
        "timestamp": datetime.now(timezone.utc),
    }
    known_npcs = [data["name"] for data in CAMPAIGN["npcs"].values()]
    # A genuinely compound message ("recruit Sarah, look at the quest
    # board, and leave the tavern") returns more than one intent here --
    # see ai.intent_parser.parse_intents's own docstring for exactly how
    # conservative the detection is (deliberately keyword-only, no extra
    # Ollama calls, only splits on strong separators like commas/"then").
    # An ordinary single-action message still returns exactly one intent,
    # identical to what parse_intent alone would have returned before
    # this existed.
    intents = await asyncio.to_thread(parse_intents, text, known_npc_names=known_npcs)
    action = intents[0]["action"]
    # Player-facing message CONTENT is never logged elsewhere (only HTTP
    # metadata is, via httpx's own logging) — without this, a
    # misclassified action (confirmed to happen on this small model; see
    # CHANGELOG) is undiagnosable after the fact. raw text + resolved
    # action is the minimum needed to actually audit a "why did this
    # happen" report. One line per sub-action when the message was split.
    for i in intents:
        logger.info(f"[intent] user={update.effective_user.id} action={i['action']!r} text={i['raw_text']!r}")

    # A resting character only wakes for a genuine action or an explicit
    # confirmation — a passive status/info check (sheet, inventory,
    # party, quests, map, clues) must NOT wake them by itself, or
    # "resting until next session" is meaningless the moment someone
    # checks on them. go_inactive AND rest are also excluded: asking to
    # rest again while already resting isn't "waking up to act" — "I
    # rest" classifies as action="rest" (not "go_inactive", which is
    # reserved for "resting for the night"/"done for now" phrasing), so
    # without "rest" here too, a resting character repeating "I rest"
    # would get woken (partial heal, is_inactive cleared) and then
    # immediately put back to sleep by _do_rest, losing whatever
    # partial rest progress they'd built up. "chat" is excluded
    # too — confirmed live 2026-07-10: a message the parser couldn't
    # classify into any real action (most likely dictation/autocorrect
    # garbling a real command) fell through to action='chat', which
    # produces zero game effect either way, yet still woke the character
    # with nothing actually done — worse than just not waking them,
    # since "chat" already means "we don't know what this was," not
    # "this was a genuine action."
    character = db.get_character(update.effective_user.id)
    # A dead character (2026-07-14, per Coffee) can't act OR be moved --
    # they stay exactly where they died until a Revivify brings them
    # back (see the "resurrect" spell effect in _do_cast_spell). Same
    # status-check allowlist shape as the is_inactive gate below, plus
    # switch_character/create_character/delete_character so the player
    # can actually go play someone else in the meantime, per his
    # explicit direction.
    if character and character.get("is_dead"):
        is_status_check = action in (
            "check_sheet", "check_inventory", "check_party", "check_quests",
            "show_map", "ask_clue", "list_characters", "switch_character",
            "create_character", "delete_character", "chat",
        )
        if not is_status_check:
            # Real live incident (2026-07-15): a bare telegram.error.
            # TimedOut on this exact direct send silently ate the
            # message for a real player -- switched to _safe_send, same
            # resilience convention every other important reply in this
            # file already follows.
            await _safe_send(
                update,
                f"💀 **{character['name']}** is dead and can't act or be moved until revived — "
                f"staying right where they died. Say \"switch to <character>\" to play someone else, "
                f"or \"I want to create a character\" to start fresh.",
            )
            return

    if character and character.get("is_inactive"):
        is_status_check = action in (
            "check_sheet", "check_inventory", "check_party", "check_quests",
            "show_map", "ask_clue", "list_characters", "go_inactive", "rest", "chat",
        )
        explicit_wake = any(
            phrase in text.lower()
            for phrase in ("wake up", "wake me up", "i'm awake", "im awake", "i'm back", "im back",
                           "i'm ready", "im ready")
        )
        if explicit_wake or not is_status_check:
            elapsed_seconds = 0.0
            if character.get("rest_started_at"):
                try:
                    started = datetime.fromisoformat(character["rest_started_at"])
                    elapsed_seconds = (datetime.now(timezone.utc) - started).total_seconds()
                except (TypeError, ValueError):
                    elapsed_seconds = 0.0
            hp_gain, slot_gain = _apply_natural_healing(update.effective_user.id, character, elapsed_seconds)
            db.mark_active(update.effective_user.id)
            db.update_character(update.effective_user.id, rest_started_at=None,
                                 death_save_successes=0, death_save_failures=0)

            heal_note = ""
            gains = []
            if hp_gain:
                gains.append(f"+{hp_gain} HP")
            if slot_gain:
                gains.append(f"+{slot_gain} spell slot(s)")
            if gains:
                heal_note = f" Recovered {' and '.join(gains)} from resting."

            await update.effective_chat.send_message(
                f"☀️ **{character['name']}** wakes and rejoins — welcome back!{heal_note}",
                message_thread_id=config.TOPIC_ADVENTURE_ID,
            )

    if len(intents) > 1:
        # 2026-07-13 (Coffee): a genuinely compound message ("recruit Sarah
        # and check my inventory") previously sent one real Telegram
        # message per sub-action -- he wanted them combined into a single
        # reply instead. Every _do_* handler in this file only ever
        # touches two things on effective_chat: .id and .send_message()
        # (confirmed by grep across the whole file) -- so a lightweight
        # duck-typed proxy that buffers .send_message() calls instead of
        # actually sending, swapped in only for a compound dispatch,
        # covers every handler without touching any of them. python-
        # telegram-bot's real Chat/Update objects can't be monkey-patched
        # directly (TelegramObject enforces frozen attributes), which is
        # exactly why this is a separate proxy class instead of patching
        # update.effective_chat in place.
        chat_proxy = _BufferingChatProxy(update.effective_chat)
        proxied_update = _EffectiveChatOverride(update, chat_proxy)
        async with _keep_typing(update.effective_chat, config.TOPIC_ADVENTURE_ID):
            for i in intents:
                await _dispatch_intent(proxied_update, context, i, text)
        if chat_proxy.buffered:
            await _safe_send(update, "\n\n".join(chat_proxy.buffered))
        return

    async with _keep_typing(update.effective_chat, config.TOPIC_ADVENTURE_ID):
        for i in intents:
            await _dispatch_intent(update, context, i, text)


class _BufferingChatProxy:
    """
    Stands in for update.effective_chat during a compound-message
    dispatch: .id passes through to the real chat (handlers that key
    off it, e.g. sessions.get_lock(chat_id), still work normally), but
    .send_message() buffers text instead of actually sending, so every
    sub-action's narration can be combined into one real message at the
    end instead of one Telegram message per sub-action.
    """
    def __init__(self, real_chat):
        self.id = real_chat.id
        self.buffered: list[str] = []

    async def send_message(self, text, **kwargs):
        self.buffered.append(text)


class _EffectiveChatOverride:
    """
    Duck-typed stand-in for Update: identical to the real one for every
    attribute a handler might touch, except effective_chat, which is
    swapped for a _BufferingChatProxy. Needed because python-telegram-
    bot's real Update can't have effective_chat reassigned in place
    (TelegramObject enforces frozen attributes).
    """
    def __init__(self, real_update, chat_proxy):
        self._real_update = real_update
        self.effective_chat = chat_proxy

    def __getattr__(self, name):
        return getattr(self._real_update, name)


async def _dispatch_intent(update: Update, context: ContextTypes.DEFAULT_TYPE, intent: dict, text: str) -> None:
    """
    Runs the single game action a classified intent resolves to. Split
    out of adventure_master_handler so a genuinely compound message
    (see ai.intent_parser.parse_intents) can call this once per
    sub-action in sequence, awaiting each one fully before the next --
    the exact same dispatch a single ordinary message always went
    through, just reusable per action instead of inline.
    """
    action = intent["action"]
    text = intent.get("raw_text") or text
    if action == "create_character":
        await _begin_character_creation(update, context)
    elif action == "start_combat":
        monster_key = None
        lowered = text.lower()
        for key in CAMPAIGN["monsters"]:
            if _text_mentions_monster(key, lowered):
                monster_key = key
                break
        enemy_count = _parse_enemy_count(lowered)
        await _do_start_combat(update, monster_key, count=enemy_count)
    elif action == "attack":
        await _do_attack(update, intent.get("raw_text", text))
    elif action == "move":
        await _do_move(update, text)
    elif action == "look":
        await _do_look(update)
    elif action == "examine":
        await _do_examine(update, intent.get("target") or "")
    elif action == "check_inventory":
        await _do_check_inventory(update)
    elif action == "check_party":
        await _do_check_party(update, text)
    elif action == "buy":
        await _do_buy(update, text)
    elif action == "sell":
        await _do_sell(update, text)
    elif action == "steal":
        await _do_steal(update, text)
    elif action == "fast_travel":
        await _do_fast_travel(update, intent.get("target") or text)
    elif action == "cast_spell":
        await _do_cast_spell(update, text)
    elif action == "join_guild":
        await _do_join_guild(update, text)
    elif action == "pass_turn":
        await _do_pass_turn(update)
    elif action == "check_sheet":
        await _do_check_sheet(update, intent.get("target"))
    elif action == "talk_npc" and intent.get("npc_name"):
        npc_id = _find_npc_id_by_name(intent["npc_name"])
        if not npc_id or npc_id not in _NPCS:
            # Real live bug (2026-07-21, Coffee): "Speak to the wide-boled
            # tree" got classified talk_npc with npc_name="the wide-boled
            # tree" -- not a real NPC, so this branch used to just silently
            # do nothing (no reply at all). A non-NPC target of "talk"/
            # "speak to" is almost always a real interactable instead
            # (a tree, a statue, a waystone), so fall back to the same
            # examine path "Say hello to Grimsby" already proved works
            # for the opposite miscue, rather than dropping the message.
            await _do_examine(update, intent["npc_name"])
        elif npc_id and npc_id in _NPCS:
            character = db.get_character(update.effective_user.id)
            character_name = character["name"] if character else "the player"
            relationship = db.get_relationship(update.effective_user.id, npc_id)
            quest_facts = _npc_quest_facts(character, npc_id) if character else None
            reply = await asyncio.to_thread(
                talk_to_npc, npc_id, text, character_name, relationship["memory_events"], quest_facts
            )
            npc_data = CAMPAIGN["npcs"].get(npc_id, {})
            npc_display_name = npc_data.get("name", intent["npc_name"])
            await _safe_send(update, f"💬 **{npc_display_name}:** {reply}")
            await _maybe_send_npc_portrait(update, npc_id, npc_data)
            # Ordinary conversation builds a small amount of rapport over
            # time — real, persistent, and separate from the short-term
            # conversation buffer talk_to_npc already keeps.
            db.adjust_affinity(update.effective_user.id, npc_id, 1)
            faction_id = _faction_for_npc(npc_id)
            if faction_id:
                _adjust_faction_standing(
                    update.effective_user.id, faction_id, 1, _faction_starting_standing(faction_id)
                )
    elif action == "recruit_npc" and intent.get("npc_name"):
        await _do_recruit_npc(update, intent["npc_name"])
    elif action == "rest":
        await _do_rest(update)
    elif action == "go_inactive":
        await _do_go_inactive(update, intent.get("target") or "")
    elif action == "accept_quest":
        await _do_accept_quest(update, text)
    elif action == "check_quests":
        await _do_check_quests(update)
    elif action == "ask_clue":
        await _do_ask_clue(update)
    elif action == "answer_puzzle":
        await _do_answer_puzzle(update, text)
    elif action == "gamble":
        await _do_gamble(update, text)
    elif action == "dice_game":
        await _do_dice_game(update)
    elif action == "fortunes_wheel":
        await _do_fortunes_wheel(update)
    elif action == "set_alignment":
        await _do_set_alignment(update, text)
    elif action == "message_ai":
        await _do_message_ai(update, intent.get("raw_text", text))
    elif action == "skill_tree":
        await _do_show_skill_tree(update)
    elif action == "challenge_duel":
        await _do_challenge_duel(update, text)
    elif action == "accept_duel":
        await _do_accept_duel(update)
    elif action == "check_market":
        await _do_check_market(update)
    elif action == "join_battle":
        await _do_join_battle(update)
    elif action == "replay_intro":
        await _do_replay_chapter_intro(update)
    elif action == "visual_map":
        await _do_show_visual_map(update)
    elif action == "rebirth":
        await _do_rebirth(update)
    elif action == "choose_hybrid":
        await _do_choose_hybrid(update, intent.get("raw_text", text))
    elif action == "skill_check":
        await _do_skill_check(update, intent.get("ability") or "dexterity", text)
    elif action == "shove":
        await _do_shove(update, text)
    elif action == "flee":
        await _do_flee(update, text)
    elif action == "resolve_choice":
        await _do_resolve_quest_choice(update, text)
    elif action == "invite_to_party":
        await _do_invite_to_party(update, intent.get("target") or "")
    elif action == "accept_party_invite":
        await _do_accept_party_invite(update)
    elif action == "leave_party":
        await _do_leave_party(update)
    elif action == "find_merchant":
        await _do_find_merchant(update)
    elif action == "give_item":
        await _do_give_item(update, intent.get("raw_text", text))
    elif action == "use_item":
        await _do_use_item(update, intent.get("raw_text", text))
    elif action == "equip_item":
        await _do_equip_item(update, intent.get("raw_text", text))
    elif action == "auto_equip":
        await _do_auto_equip_gear(update, intent.get("raw_text", text))
    elif action == "show_map":
        await _do_show_map(update)
    elif action == "gather":
        await _do_gather(update, intent.get("raw_text", text))
    elif action == "craft":
        await _do_craft(update, text)
    elif action == "make_campfire":
        await _do_make_campfire(update)
    elif action == "second_wind":
        await _do_second_wind(update)
    elif action == "rage":
        await _do_rage(update)
    elif action == "bardic_inspiration":
        await _do_bardic_inspiration(update, intent.get("target") or text)
    elif action == "lay_on_hands":
        await _do_lay_on_hands(update, intent.get("target") or text)
    elif action == "arcane_recovery":
        await _do_arcane_recovery(update)
    elif action == "breath_weapon":
        await _do_breath_weapon(update)
    elif action == "channel_divinity":
        await _do_channel_divinity(update)
    elif action == "action_surge":
        await _do_action_surge(update)
    elif action == "reckless_attack":
        await _do_reckless_attack(update)
    elif action == "divine_smite":
        await _do_divine_smite(update)
    elif action == "flurry_of_blows":
        await _do_flurry_of_blows(update)
    elif action == "wild_shape":
        await _do_wild_shape(update)
    elif action == "toggle_manual_dice":
        await _do_toggle_manual_dice(update, intent.get("raw_text", text))
    elif action == "level_up":
        await _do_level_up(update, intent.get("raw_text", text))
    elif action == "set_description":
        await _do_set_description(update, intent.get("raw_text", text))
    elif action == "set_pronouns":
        await _do_set_pronouns(update, intent.get("raw_text", text))
    elif action == "bestiary":
        await _do_bestiary(update)
    elif action == "leaderboard":
        await _do_leaderboard(update)
    elif action == "check_achievements":
        await _do_check_achievements(update)
    elif action == "set_title":
        title_arg = _extract_inline_title(intent.get("raw_text", text))
        if title_arg is None:
            await _safe_send(
                update,
                "Which title would you like to wear? Say \"check achievements\" to see "
                "which ones you've unlocked, then \"set my title to <title>\".",
            )
        else:
            await _do_set_title(update, title_arg)
    elif action == "check_weather":
        await _do_check_weather(update)
    elif action == "check_guild_quest":
        character = db.get_character(update.effective_user.id)
        if character is None or not character.get("guild"):
            await _safe_send(update, "You're not in a guild — join one first (\"join the Adventurers' Guild\", etc.).")
        else:
            await _do_check_guild_quest(update, character["guild"])
    elif action == "list_shop":
        await _do_list_shop(update)
    elif action == "list_characters":
        await _do_list_characters(update)
    elif action == "switch_character":
        await _do_switch_character(update, intent.get("target") or text)
    elif action == "delete_character":
        await _do_delete_character(update, intent.get("target") or text)
    elif action == "chat":
        # Real live bug (2026-07-22, Coffee: "Im not getting a response?!
        # if the action doesn't work, can you please say something"):
        # "Curve my initials on the hearth mantle" (a real attempt to
        # interact with a real, already-examined interactable, just
        # phrased with a verb -- "carve"/"curve" -- this game has no
        # specific action for) resolved to the catch-all "chat", which
        # is silent by design for ordinary roleplay banter -- but this
        # wasn't banter, it named a real object. Same "don't guess, but
        # don't stay silent when a real game fact IS present" fix shape
        # as the wide-boled-tree talk_npc misfire fixed earlier this
        # session: if the text actually names a real interactable at
        # this character's own location, fall back to examining it
        # instead of dropping the message -- ordinary chit-chat never
        # matches a real interactable name, so this can't turn genuine
        # banter noisy.
        character = db.get_character(update.effective_user.id)
        if character is not None:
            location = cl.get_location(CAMPAIGN, character["current_location"])
            if location is not None and _find_interactable(location, text):
                await _do_examine(update, text)
    # Any other unmatched "chat": no game action, let it be ordinary
    # roleplay chatter with no bot response required.


# ---------------------------------------------------------------------
# Optional slash-command shortcuts (power users can still use these;
# they call the exact same underlying functions as natural language).
# ---------------------------------------------------------------------

async def newcharacter_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not topics.is_adventure(update.message.message_thread_id or 0):
        return
    await _begin_character_creation(update, context)


async def startcombat_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not topics.is_adventure(update.message.message_thread_id or 0):
        return
    monster_key = context.args[0].lower() if context.args else "goblin"
    await _do_start_combat(update, monster_key)


async def attack_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not topics.is_adventure(update.message.message_thread_id or 0):
        return
    action_text = " ".join(context.args) if context.args else "I attack"
    await _do_attack(update, action_text)


async def endturn_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not topics.is_adventure(update.message.message_thread_id or 0):
        return
    await _do_pass_turn(update)


async def sheet_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not topics.is_adventure(update.message.message_thread_id or 0):
        return
    await _do_check_sheet(update)


async def replay_intro_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """/replay_intro -- rewatch the current chapter's opening cutscene. See _do_replay_chapter_intro."""
    if not topics.is_adventure(update.message.message_thread_id or 0):
        return
    await _do_replay_chapter_intro(update)


async def dice_game_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """/dice_game -- play your class's dice mini-game. See _do_dice_game."""
    if not topics.is_adventure(update.message.message_thread_id or 0):
        return
    await _do_dice_game(update)


async def fortune_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """/fortune -- spin Fortune's Wheel, open to every class. See _do_fortunes_wheel."""
    if not topics.is_adventure(update.message.message_thread_id or 0):
        return
    await _do_fortunes_wheel(update)


async def alignment_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """/alignment <text> -- set your alignment (e.g. "/alignment chaotic good"). See _do_set_alignment."""
    if not topics.is_adventure(update.message.message_thread_id or 0):
        return
    await _do_set_alignment(update, " ".join(context.args))


async def msg_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """/msg <name> <message> -- tell an AI party member something, in or out of combat, without spending a turn. See _do_message_ai."""
    if not topics.is_adventure(update.message.message_thread_id or 0):
        return
    await _do_message_ai(update, " ".join(context.args))


async def skilltree_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """/skilltree -- shows your class's real skill-tree upgrade and lets you unlock it. See _do_show_skill_tree."""
    if not topics.is_adventure(update.message.message_thread_id or 0):
        return
    await _do_show_skill_tree(update)


async def duel_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """/duel <name> -- challenge a real player at your location to PvP. See _do_challenge_duel."""
    if not topics.is_adventure(update.message.message_thread_id or 0):
        return
    await _do_challenge_duel(update, " ".join(context.args))


async def accept_duel_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """/accept_duel -- accept a pending duel challenge. See _do_accept_duel."""
    if not topics.is_adventure(update.message.message_thread_id or 0):
        return
    await _do_accept_duel(update)


async def sell_market_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """/sell_market <quantity> <price> <item name> -- list an item on the player marketplace. See _do_sell_market."""
    if not topics.is_adventure(update.message.message_thread_id or 0):
        return
    await _do_sell_market(update, context.args)


async def market_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """/market -- shows all current player marketplace listings. See _do_check_market."""
    if not topics.is_adventure(update.message.message_thread_id or 0):
        return
    await _do_check_market(update)


async def buy_market_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """/buy_market <listing #> -- buy a player marketplace listing. See _do_buy_market."""
    if not topics.is_adventure(update.message.message_thread_id or 0):
        return
    await _do_buy_market(update, context.args)


async def join_battle_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """/join_battle -- join an in-progress fight at your current location. See _do_join_battle."""
    if not topics.is_adventure(update.message.message_thread_id or 0):
        return
    await _do_join_battle(update)


async def map_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """
    /map (2026-07-17, per Coffee) -- same real fog-of-war map _do_show_map
    already builds from a character's actual visited_locations (only
    places they've really been show by name; unexplored connections
    show as unexplored, never revealing what's actually there). This is
    just a slash-command shortcut for the exact same function "show me
    the map" already calls -- no new logic, no separate map system.
    """
    if not topics.is_adventure(update.message.message_thread_id or 0):
        return
    await _do_show_map(update)


async def visual_map_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """/visual_map -- a real generated map image, grounded in your actual explored locations. See _do_show_visual_map."""
    if not topics.is_adventure(update.message.message_thread_id or 0):
        return
    await _do_show_visual_map(update)


async def menu_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """
    /menu -- slash-command shortcut for _do_show_menu, task #176's full
    menu revision. Per Coffee: usable from both Adventure and Support
    (unlike most other action commands, which stay Adventure-only) --
    a player looking something up in Support shouldn't have to switch
    topics just to peek at their sheet/quests/inventory.
    """
    thread_id = update.message.message_thread_id or 0
    if not (topics.is_adventure(thread_id) or topics.is_support(thread_id)):
        return
    await _do_show_menu(update)


async def leaderboard_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """/leaderboard (2026-07-17, per Coffee, task #74) -- slash-command shortcut for _do_leaderboard."""
    if not topics.is_adventure(update.message.message_thread_id or 0):
        return
    await _do_leaderboard(update)


async def quests_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """/quests (task #122) -- slash-command shortcut for _do_check_quests, same as "check my quests" in NL."""
    if not topics.is_adventure(update.message.message_thread_id or 0):
        return
    await _do_check_quests(update)


async def party_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """/party (task #122), optionally "/party sheet" for the full-sheet variant -- shortcut for _do_check_party."""
    if not topics.is_adventure(update.message.message_thread_id or 0):
        return
    await _do_check_party(update, " ".join(context.args))


async def inventory_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """/inventory (task #122) -- slash-command shortcut for _do_check_inventory."""
    if not topics.is_adventure(update.message.message_thread_id or 0):
        return
    await _do_check_inventory(update)


async def shop_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """/shop (task #122) -- slash-command shortcut for _do_list_shop, same as "what's for sale" in NL."""
    if not topics.is_adventure(update.message.message_thread_id or 0):
        return
    await _do_list_shop(update)


async def buy_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """/buy <item> (task #122) -- slash-command shortcut for _do_buy."""
    if not topics.is_adventure(update.message.message_thread_id or 0):
        return
    if not context.args:
        await _safe_send(update, "Buy what? e.g. \"/buy healing potion\".")
        return
    await _do_buy(update, " ".join(context.args))


async def sell_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """/sell <item> (task #122) -- slash-command shortcut for _do_sell."""
    if not topics.is_adventure(update.message.message_thread_id or 0):
        return
    if not context.args:
        await _safe_send(update, "Sell what? e.g. \"/sell rusty dagger\".")
        return
    await _do_sell(update, " ".join(context.args))


async def cast_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """
    /cast <spell> [target] (task #122) -- this game had NO slash-command
    path for casting at all before this; every spell had to be cast via
    natural language ("I cast fireball on the goblin"). Shortcut for the
    exact same _do_cast_spell already used by that NL path.
    """
    if not topics.is_adventure(update.message.message_thread_id or 0):
        return
    if not context.args:
        await _safe_send(update, "Cast what? e.g. \"/cast fire bolt\" or \"/cast cure wounds on Ravenloft\".")
        return
    await _do_cast_spell(update, " ".join(context.args))


async def rest_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """/rest (task #122) -- slash-command shortcut for _do_rest, same as "I rest"/"heal up" in NL."""
    if not topics.is_adventure(update.message.message_thread_id or 0):
        return
    await _do_rest(update)


async def guild_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """/guild (task #122) -- today's guild quest status for whichever real guild this character belongs to."""
    if not topics.is_adventure(update.message.message_thread_id or 0):
        return
    character = db.get_character(update.effective_user.id)
    if character is None or not character.get("guild"):
        await _safe_send(update, "You're not in a guild — join one first (\"join the Adventurers' Guild\", etc.).")
        return
    await _do_check_guild_quest(update, character["guild"])


async def version_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.effective_chat.send_message(
        f"Pandora MMO v{version.get_version()} — powered by Pandora AI",
        message_thread_id=update.message.message_thread_id,
    )


async def campaigns_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """/campaigns (task #56): lists every campaigns/<id>/campaign.json actually found on disk and which is live."""
    available = cl.discover_campaigns()
    lines = ["📚 **Campaigns**"]
    for campaign_id in available:
        marker = " *(active)*" if campaign_id == ACTIVE_CAMPAIGN_ID else ""
        lines.append(f"• {campaign_id}{marker}")
    if not available:
        lines.append("(none found)")
    await update.effective_chat.send_message(
        "\n".join(lines), message_thread_id=update.message.message_thread_id
    )


async def changelog_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.effective_chat.send_message(
        version.get_changelog(),
        message_thread_id=update.message.message_thread_id,
    )


_HELP_TEXT = """📖 **How to play Pandora MMO**

Almost everything here is plain English, typed straight into Adventure -- no commands to memorize. Just say what your character does.

**Getting around**
• "Look around" / "Where am I?" -- describes the area
• "Examine the old barrel" -- inspect one specific thing
• "Go to the tavern" / "Head upstairs" -- move somewhere
• "What's the weather like?" -- real time-of-day and conditions

**Talking & people**
• "Talk to Grimsby" / "Say hello to Sarah" -- NPC conversation
• "Recruit Sarah to my party" -- add a real companion
• "Invite <player> to my party" / "Accept the invite" / "Leave the party"
• "Join the Arcane Circle" (or whichever guild) -- gets you into that guild's own real Telegram topic

**Combat**
• "Attack the goblin" -- once combat's started
• "Flee" / "Pass my turn"
• Class abilities work by name too: "Rage", "Second Wind", "Action Surge", "Channel Divinity", "Wild Shape", "Cast fireball", etc.

**Items & shops**
• "What's in my inventory?" / "Check my sheet"
• "Buy 3 torches" / "Sell my old sword" / "What do you have for sale?"
• "Equip the longsword" / "Use a healing potion" / "Give the rope to <player>"
• "Where's the nearest merchant?"

**Gathering & crafting**
• "Chop wood" / "Gather herbs" / "Go fishing" -- needs the right tool for some
• "Craft a healing potion"
• "Make camp" / "Rest" to recover over time

**Quests**
• "Check my quests" -- your quest journal AND the local quest board
• "Accept this quest" (names it if more than one's posted)
• "Ask for a clue" if you're stuck

**Character**
• "Level up" -- once you have XP to spend
• "Set my description to ..."

**Slash commands** (alphabetical -- every one of these also works as a plain sentence above; these are just the shortcuts)
• /achievements -- see what you've unlocked
• /buy <item>
• /cast <spell> [target]
• /changelog
• /alignment <lawful/neutral/chaotic> <good/neutral/evil> -- set your alignment
• /accept_duel -- accept a pending PvP duel challenge
• /dice_game -- play your class's own dice mini-game (free, levelable)
• /duel <name> -- challenge a real player at your location to PvP (not allowed in safe places)
• /donotdisturb -- toggle DND so you're skipped for join-a-fight nudges
• /fortune -- spin Fortune's Wheel, open to every class
• /guild -- today's guild quest status
• /join_battle -- join an in-progress fight at your current location
• /hint -- real things to try here, no spoilers
• /inventory
• /leaderboard
• /map
• /market -- see current player marketplace listings
• /visual_map -- a real generated map image of your explored world
• /buy_market <listing #> -- buy a marketplace listing
• /sell_market <quantity> <price> <item name> -- list an item for sale on the player marketplace
• /msg <name> <message> -- tell an AI party member something (in or out of combat), never spends a turn
• /newcharacter
• /note <text> -- a short status note party members can see ("/note clear" removes it)
• /party [sheet] -- your party at a glance, or full sheets with "sheet"
• /quests
• /replay_intro -- rewatch your current chapter's opening cutscene
• /rest
• /sell <item>
• /sheet
• /skilltree -- your class's real skill-tree upgrade, spend banked skill points to unlock it
• /shop -- what's for sale here
• /title <title> -- wear a title you've earned
• /version
• /weather

Stuck? /hint suggests real things to try here, no spoilers. Reply to any narration with /help to get it explained. Stuck on something specific? Ask in Support -- it's grounded in this game's real items/spells/guilds, not general D&D trivia."""


async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """
    Bare /help sends the static reference below. Used as a reply to
    another message (2026-07-17, per Coffee: "can i use /help to reply
    to a narration or prompt and get help on it"), it instead forwards
    that message's real text to the Support agent -- grounded in this
    game's actual items/spells/guilds, same as every other Support
    answer -- so "what does this mean?" gets a real, specific answer
    instead of the generic command list.
    """
    replied = update.message.reply_to_message
    replied_text = replied.text if replied else None
    if not replied_text or not replied_text.strip():
        await update.effective_chat.send_message(
            _HELP_TEXT,
            message_thread_id=update.message.message_thread_id,
        )
        return

    character = db.get_character(update.effective_user.id)
    party_members = _get_party_members() if character else None
    question = f"Can you explain this: {replied_text.strip()}"
    async with _keep_typing(update.effective_chat, update.message.message_thread_id):
        reply = await asyncio.to_thread(answer_support_question, question, character, party_members)
    await update.effective_chat.send_message(reply, message_thread_id=update.message.message_thread_id)


async def hint_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """
    /hint (2026-07-17, per Coffee: "give ideas on actions characters may
    perform or how to do what they maybe tryin to do... dont spoil").
    Deterministic and grounded entirely in this location's real data --
    same fields _do_look already reads (NPCs, resource nodes,
    interactables, connections, quests) -- just reframed as suggested
    ACTIONS rather than a description. Never invents anything and never
    reveals a puzzle's answer, a quest's outcome, or a hidden reward --
    only that something is here and the verb to try on it, exactly the
    same non-spoiler boundary _do_ask_clue's quest clues already keep.
    """
    character = db.get_character(update.effective_user.id)
    if character is None:
        await update.effective_chat.send_message(
            "You don't have a character yet!", message_thread_id=update.message.message_thread_id
        )
        return
    location = cl.get_location(CAMPAIGN, character["current_location"])
    if location is None:
        await update.effective_chat.send_message(
            f"**{character['name']}** seems to be nowhere in particular. That's... concerning.",
            message_thread_id=update.message.message_thread_id,
        )
        return

    lines = ["💡 **Things you might try here:**"]

    npcs_here = _npcs_at_location(character["current_location"])
    for npc_id in npcs_here:
        npc = cl.get_npc(CAMPAIGN, npc_id)
        if npc:
            lines.append(f"• Talk to **{npc['name']}** — say \"talk to {npc['name']}\"")

    for node in location.get("resource_nodes", []):
        material = items_module.get_item(node["material"])
        skill_key = node.get("skill", node["ability"])
        missing = _missing_tools_for_gathering(character, skill_key)
        tool_note = f" (needs {' and '.join(missing)})" if missing else ""
        lines.append(f"• Gather {material['name']} from {node['name']}{tool_note} — say \"gather {material['name'].lower()}\"")

    for obj in location.get("interactables", {}).values():
        lines.append(f"• Take a closer look at {obj['name']} — say \"examine {obj['name']}\"")

    connections = location.get("connections", [])
    if connections:
        conn_names = ", ".join(cl.get_location(CAMPAIGN, c)["name"] for c in connections)
        lines.append(f"• Travel onward — you can reach {conn_names} from here")

    story_offer = _offerable_quest_at_location(character, character["current_location"])
    if story_offer:
        lines.append("• Someone here looks like they need help with something — try talking to them")

    board_quests_here = board_quests_module.get_or_generate_board_quests(CAMPAIGN, character["current_location"])
    if any(not q.get("accepted_by") and not q.get("completed_at") for q in board_quests_here):
        lines.append("• There's a bounty posted on the board — say \"check quests\" to see it")

    if character["active_quests"]:
        for quest_id in character["active_quests"]:
            quest = CAMPAIGN["quests"].get(quest_id)
            if quest and quest.get("clue"):
                lines.append(f"• On *{quest['title']}*: {quest['clue']}")

    if len(lines) == 1:
        lines.append("Nothing obvious jumps out — try looking around, or moving on to somewhere new.")

    await _safe_send(update, "\n".join(lines))


# ---------------------------------------------------------------------
# Development topic: conversational troubleshooting/build assistant.
# Support topic: conversational how-to-play assistant.
# Both keep a short per-user rolling history in context.user_data so the
# assistant has some conversational continuity, without ever touching
# files, running commands, or affecting the live game.
# ---------------------------------------------------------------------

MAX_ASSISTANT_HISTORY = 6


async def _is_group_owner(update: Update, context: ContextTypes.DEFAULT_TYPE) -> bool | None:
    """
    Checks Telegram's actual group-owner ("creator") status for whoever
    sent this message. Uses the live Bot API rather than a hardcoded
    user ID, so it stays correct even if group ownership ever changes.
    Wrapped with a hard timeout so a stalled network call can never hang
    this indefinitely. Returns None (not False) if the check itself
    failed (network/API error) — a real incident showed the actual
    owner getting a flat "restricted to owner" message from a transient
    getChatMember hiccup, indistinguishable from genuinely not being the
    owner. Callers must treat None as "couldn't verify," not "denied."
    """
    try:
        member = await asyncio.wait_for(
            context.bot.get_chat_member(update.effective_chat.id, update.effective_user.id),
            timeout=10,
        )
        return member.status == "creator"
    except Exception as e:
        logger.warning(f"[dev_access] owner check failed (treated as unverified, not denied): {e!r}")
        return None


async def _is_group_admin_or_owner(update: Update, context: ContextTypes.DEFAULT_TYPE) -> bool | None:
    """
    Same shape and same live-Bot-API approach as _is_group_owner above,
    but also accepts Telegram "administrator" status, not just
    "creator" -- for /redo (2026-07-17, per Coffee: "for tg group admin
    and owners and me"), which is meant to be usable by anyone the group
    owner has trusted with admin rights, not only the single creator
    account. Returns None (not False) on a verification failure, same
    reasoning as _is_group_owner: indistinguishable-from-denial is worse
    than a distinct "couldn't check, try again."
    """
    try:
        member = await asyncio.wait_for(
            context.bot.get_chat_member(update.effective_chat.id, update.effective_user.id),
            timeout=10,
        )
        return member.status in ("creator", "administrator")
    except Exception as e:
        logger.warning(f"[dev_access] admin check failed (treated as unverified, not denied): {e!r}")
        return None


async def _is_dev_topic_authorized(update: Update, context: ContextTypes.DEFAULT_TYPE) -> bool | None:
    """
    Gates the Development topic itself (2026-07-17, per Coffee: a
    genuine second dev, "Sugar", needs real Dev-topic access -- but as
    a dedicated bot-managed allowlist via /add_admin and /remove_admin,
    NOT by making her a real Telegram group admin, which grants far
    more than intended (deleting messages, banning members, etc, none
    of which this game needs). Deliberately separate from
    _is_group_admin_or_owner above, which is about actual Telegram
    admin status for /redo -- this is its own trust boundary. True for
    the real group owner OR anyone in db.get_trusted_dev_ids(). Checks
    the (free, instant, no network call) allowlist first so a trusted
    dev never pays for a live Telegram API round trip just to use the
    Dev topic. Returns None (not False) only when the OWNER check
    itself fails to verify -- same "unverified isn't denied" reasoning
    as _is_group_owner.
    """
    if update.effective_user.id in db.get_trusted_dev_ids():
        return True
    return await _is_group_owner(update, context)


def _resolve_telegram_target(update: Update, context: ContextTypes.DEFAULT_TYPE) -> tuple[int | None, str]:
    """
    Resolves the target Telegram user for /add_admin, /remove_admin,
    /ban, and /unban -- all security-sensitive permission changes, so
    deliberately NOT a fuzzy name/@username match (this game has no
    general username directory outside of registered characters, and
    even if it did, guessing wrong here would be a real access-control
    bug, not just a misfired narration). Two ways to specify a target:
    1. Reply to that person's own message with the command -- Telegram
       hands back a real User object directly, zero ambiguity.
    2. Pass their raw numeric Telegram user ID as the command argument.
    """
    reply = update.message.reply_to_message
    if reply is not None and reply.from_user is not None:
        user = reply.from_user
        label = f"{user.full_name} (@{user.username})" if user.username else user.full_name
        return user.id, label
    if context.args:
        try:
            target_id = int(context.args[0])
            return target_id, f"user {target_id}"
        except ValueError:
            pass
    return None, ""


_NO_TARGET_MESSAGE = (
    "Reply to that person's message with this command, or use it with their numeric "
    "Telegram user ID as the argument."
)


async def add_admin_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """
    Grants Development-topic access to another real person -- e.g.
    Sugar, a genuine second dev (2026-07-17, per Coffee) -- WITHOUT
    making them a real Telegram group admin (which would grant far
    more than intended: deleting messages, banning members, etc, none
    of which this game needs). Gated the same way the Dev topic itself
    is (owner OR an existing trusted dev), per Coffee's explicit "only
    a DEV or admin can use those commands" -- deliberately not
    owner-only, so an existing trusted dev can onboard another one.
    """
    if update.message.message_thread_id != config.TOPIC_DEVELOPMENT_ID:
        return
    is_authorized = await _is_dev_topic_authorized(update, context)
    if is_authorized is None:
        await _safe_send(
            update,
            "Couldn't verify permissions just now (a Telegram API call failed) — try again in a moment.",
            thread_id=config.TOPIC_DEVELOPMENT_ID,
        )
        return
    if not is_authorized:
        await _safe_send(update, "Only a Dev-topic admin can grant this.", thread_id=config.TOPIC_DEVELOPMENT_ID)
        return

    target_id, target_label = _resolve_telegram_target(update, context)
    if target_id is None:
        await _safe_send(update, _NO_TARGET_MESSAGE, thread_id=config.TOPIC_DEVELOPMENT_ID)
        return
    db.add_trusted_dev_id(target_id)
    await _safe_send(
        update, f"✅ {target_label} now has Development-topic access.", thread_id=config.TOPIC_DEVELOPMENT_ID
    )


async def remove_admin_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if update.message.message_thread_id != config.TOPIC_DEVELOPMENT_ID:
        return
    is_authorized = await _is_dev_topic_authorized(update, context)
    if is_authorized is None:
        await _safe_send(
            update,
            "Couldn't verify permissions just now (a Telegram API call failed) — try again in a moment.",
            thread_id=config.TOPIC_DEVELOPMENT_ID,
        )
        return
    if not is_authorized:
        await _safe_send(update, "Only a Dev-topic admin can revoke this.", thread_id=config.TOPIC_DEVELOPMENT_ID)
        return

    target_id, target_label = _resolve_telegram_target(update, context)
    if target_id is None:
        await _safe_send(update, _NO_TARGET_MESSAGE, thread_id=config.TOPIC_DEVELOPMENT_ID)
        return
    db.remove_trusted_dev_id(target_id)
    await _safe_send(
        update, f"✅ {target_label} no longer has Development-topic access.", thread_id=config.TOPIC_DEVELOPMENT_ID
    )


async def ban_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """
    Bans a malicious player from playing at all (2026-07-17, per
    Coffee: "admins can absolutely ban malicious players"). A banned
    telegram_user_id is checked at the very top of
    adventure_master_handler/support_topic_handler, before any other
    processing -- see db.is_banned. Gated the same as the rest of the
    Dev-topic admin surface (owner or a trusted dev), and restricted to
    the Development topic so this can never be triggered from
    Adventure/Support by an ordinary player.
    """
    if update.message.message_thread_id != config.TOPIC_DEVELOPMENT_ID:
        return
    is_authorized = await _is_dev_topic_authorized(update, context)
    if is_authorized is None:
        await _safe_send(
            update,
            "Couldn't verify permissions just now (a Telegram API call failed) — try again in a moment.",
            thread_id=config.TOPIC_DEVELOPMENT_ID,
        )
        return
    if not is_authorized:
        await _safe_send(update, "Only a Dev-topic admin can ban a player.", thread_id=config.TOPIC_DEVELOPMENT_ID)
        return

    target_id, target_label = _resolve_telegram_target(update, context)
    if target_id is None:
        await _safe_send(update, _NO_TARGET_MESSAGE, thread_id=config.TOPIC_DEVELOPMENT_ID)
        return
    db.ban_user(target_id)
    await _safe_send(
        update, f"🚫 {target_label} is now banned — they can no longer play.", thread_id=config.TOPIC_DEVELOPMENT_ID
    )


async def unban_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if update.message.message_thread_id != config.TOPIC_DEVELOPMENT_ID:
        return
    is_authorized = await _is_dev_topic_authorized(update, context)
    if is_authorized is None:
        await _safe_send(
            update,
            "Couldn't verify permissions just now (a Telegram API call failed) — try again in a moment.",
            thread_id=config.TOPIC_DEVELOPMENT_ID,
        )
        return
    if not is_authorized:
        await _safe_send(update, "Only a Dev-topic admin can unban a player.", thread_id=config.TOPIC_DEVELOPMENT_ID)
        return

    target_id, target_label = _resolve_telegram_target(update, context)
    if target_id is None:
        await _safe_send(update, _NO_TARGET_MESSAGE, thread_id=config.TOPIC_DEVELOPMENT_ID)
        return
    db.unban_user(target_id)
    # Clean slate on reversal (2026-07-17) -- an auto-ban from 3
    # infractions reversed by a dev shouldn't leave them one warning
    # away from being auto-banned again immediately; unbanning is
    # meant to be a real second chance, not a temporary reprieve.
    db.clear_infractions(target_id)
    await _safe_send(
        update, f"✅ {target_label} is unbanned and their infraction record is cleared.",
        thread_id=config.TOPIC_DEVELOPMENT_ID,
    )


async def report_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """
    Lets ANY player report something to the admins directly, with a
    reason (2026-07-17, per Coffee: "create a /report (give reasons or
    description or why) and send to admin -- do no flag if system
    doesnt flag it. jus send to admins"). Deliberately separate from
    the planned auto-flag system (task #128, still just a design doc)
    -- /report has NO detection/heuristic logic at all. Whatever the
    player writes goes straight to the Development topic for a human
    admin to judge, every single time, unconditionally -- no gate, no
    keyword matching, no "is this actually reportable" judgment call.
    Usable by any player from any topic, since anyone might need to
    flag something regardless of where it happened.
    """
    reason = " ".join(context.args) if context.args else ""
    if not reason.strip():
        await update.effective_chat.send_message(
            "Use /report followed by what you'd like to report, e.g. "
            '"/report Player X is harassing others in Adventure" -- it goes straight to the admins.',
            message_thread_id=update.message.message_thread_id,
        )
        return

    reporter = update.effective_user
    reporter_label = f"{reporter.full_name} (@{reporter.username})" if reporter.username else reporter.full_name
    topic_name = topics.get_topic_name(update.message.message_thread_id or 0)
    report_text = (
        f"🚩 **Player report** from {reporter_label} (id {reporter.id}) in {topic_name}:\n{reason.strip()}"
    )
    await _safe_send(update, report_text, thread_id=config.TOPIC_DEVELOPMENT_ID)
    await update.effective_chat.send_message(
        "Thanks — this has been sent to the admins for review.",
        message_thread_id=update.message.message_thread_id,
    )


async def warning_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """
    Admin-issued warning (2026-07-17, per Coffee: "a /warning system
    admins can use to reply to users that are out-of-line and the
    comment gets flagged - a prompt reply warns them and the serve
    documents the infraction. 3 infreactions results in a ban. all ban
    repots must be send to DEV so dev can reverse if needed"). Used by
    replying to the offending message with /warning [reason] -- works
    from wherever the bad behavior happened (Adventure/Support/Main),
    not restricted to the Development topic, since that's where an
    admin would actually be when they see it. The warning reply posts
    in that SAME topic (public, in context) rather than being hidden in
    Development. At 3 infractions, auto-bans and ALWAYS reports it to
    Development regardless of where the warning was issued, specifically
    so a dev can reverse it with /unban if it was a mistake.
    """
    is_authorized = await _is_dev_topic_authorized(update, context)
    if is_authorized is None:
        await update.effective_chat.send_message(
            "Couldn't verify permissions just now (a Telegram API call failed) — try again in a moment.",
            message_thread_id=update.message.message_thread_id,
        )
        return
    if not is_authorized:
        await update.effective_chat.send_message(
            "Only a Dev-topic admin can issue a warning.", message_thread_id=update.message.message_thread_id
        )
        return

    target_id, target_label = _resolve_telegram_target(update, context)
    if target_id is None:
        await update.effective_chat.send_message(
            "Reply to the out-of-line message with /warning [reason].",
            message_thread_id=update.message.message_thread_id,
        )
        return

    reason = " ".join(context.args) if context.args else "(no reason given)"
    count = db.add_infraction(target_id, reason, issued_by=update.effective_user.id)
    await update.effective_chat.send_message(
        f"⚠️ {target_label}, that's a warning ({count}/3): {reason}",
        message_thread_id=update.message.message_thread_id,
    )
    if count >= 3:
        db.ban_user(target_id)
        await _safe_send(
            update,
            f"🚫 **Auto-ban**: {target_label} reached 3 infractions and has been banned. "
            f"Reply with /unban (replying to one of their messages, or /unban {target_id}) to reverse this if it was a mistake.",
            thread_id=config.TOPIC_DEVELOPMENT_ID,
        )


async def warninglist_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """/warning_list -- current warnings on record, for admins."""
    is_authorized = await _is_dev_topic_authorized(update, context)
    if is_authorized is None:
        await update.effective_chat.send_message(
            "Couldn't verify permissions just now (a Telegram API call failed) — try again in a moment.",
            message_thread_id=update.message.message_thread_id,
        )
        return
    if not is_authorized:
        await update.effective_chat.send_message(
            "Only a Dev-topic admin can view the warning list.", message_thread_id=update.message.message_thread_id
        )
        return

    all_infractions = db.get_all_infractions()
    if not all_infractions:
        await update.effective_chat.send_message(
            "No warnings on record.", message_thread_id=update.message.message_thread_id
        )
        return
    lines = ["⚠️ **Current warnings:**"]
    for user_id_str, records in all_infractions.items():
        latest = records[-1]
        lines.append(f"- user {user_id_str}: {len(records)}/3 — most recent: {latest['reason']}")
    await update.effective_chat.send_message("\n".join(lines), message_thread_id=update.message.message_thread_id)


async def banlist_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """/ban_list -- currently banned players, for admins."""
    is_authorized = await _is_dev_topic_authorized(update, context)
    if is_authorized is None:
        await update.effective_chat.send_message(
            "Couldn't verify permissions just now (a Telegram API call failed) — try again in a moment.",
            message_thread_id=update.message.message_thread_id,
        )
        return
    if not is_authorized:
        await update.effective_chat.send_message(
            "Only a Dev-topic admin can view the ban list.", message_thread_id=update.message.message_thread_id
        )
        return

    banned_ids = db.list_banned_user_ids()
    if not banned_ids:
        await update.effective_chat.send_message(
            "No players currently banned.", message_thread_id=update.message.message_thread_id
        )
        return
    lines = ["🚫 **Currently banned:**"] + [f"- user {uid}" for uid in banned_ids]
    await update.effective_chat.send_message("\n".join(lines), message_thread_id=update.message.message_thread_id)


async def redo_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """
    Owner/admin-only: replays the last real message THIS bot saw in the
    same topic /redo was typed in, in case it was misclassified or
    otherwise mishandled -- e.g. a bug just got fixed and the admin
    wants the player's original message re-run without asking them to
    retype it. Re-enters the exact same code path (parse_intents +
    dispatch, or the Support answer function) using the ORIGINAL
    stored Update, so it acts as whoever actually sent the message, not
    the admin who typed /redo -- redo never lets an admin puppet a
    different player's character, only re-run what they already said.
    """
    thread_id = update.message.message_thread_id or 0
    is_allowed = await _is_group_admin_or_owner(update, context)
    if is_allowed is None:
        await update.effective_chat.send_message(
            "Couldn't verify permissions just now (a Telegram API call failed) — try again in a moment.",
            message_thread_id=update.message.message_thread_id,
        )
        return
    if not is_allowed:
        await update.effective_chat.send_message(
            "/redo is restricted to group admins and the owner.",
            message_thread_id=update.message.message_thread_id,
        )
        return

    entry = _LAST_TOPIC_MESSAGE.get((update.effective_chat.id, thread_id))
    if entry is None:
        await update.effective_chat.send_message(
            "Nothing to redo in this topic yet.",
            message_thread_id=update.message.message_thread_id,
        )
        return

    stored_update = entry["update"]
    stored_context = entry["context"]
    stored_text = entry["text"]
    await update.effective_chat.send_message(
        f"🔄 Redoing the last message here (asking the AI for a fresh, independent read — can take up "
        f"to ~2 minutes on this hardware): \"{stored_text}\"",
        message_thread_id=update.message.message_thread_id,
    )
    async with _keep_typing(update.effective_chat, update.message.message_thread_id):
        if entry["kind"] == "support":
            character = db.get_character(entry["user_id"])
            party_members = _get_party_members() if character else None
            reply = await asyncio.to_thread(answer_support_question, stored_text, character, party_members)
            logger.info(f"[redo] support user={entry['user_id']} text={stored_text!r} reply={reply!r}")
            await _safe_send(stored_update, reply, thread_id=config.TOPIC_SUPPORT_ID)
            return

        # kind == "adventure": re-parse fresh (picks up any fix shipped
        # since the original message) and re-dispatch, same combined-reply
        # shape a genuinely compound message already gets.
        known_npcs = [data["name"] for data in CAMPAIGN["npcs"].values()]
        # force_model=True (2026-07-17, per Coffee: "make it so /redo
        # actually tries to handle it differently") -- re-running the plain
        # deterministic keyword fallback on the exact same text would
        # always reproduce the exact same (possibly wrong) classification,
        # defeating the point of a manual redo. Forcing a real model call
        # here gives it a genuinely independent second read instead.
        intents = await asyncio.to_thread(
            parse_intents, stored_text, known_npc_names=known_npcs, force_model=True
        )
        for i in intents:
            logger.info(f"[redo] user={entry['user_id']} action={i['action']!r} text={i['raw_text']!r}")
        chat_proxy = _BufferingChatProxy(stored_update.effective_chat)
        proxied_update = _EffectiveChatOverride(stored_update, chat_proxy)
        for i in intents:
            await _dispatch_intent(proxied_update, stored_context, i, stored_text)
        if chat_proxy.buffered:
            await _safe_send(stored_update, "\n\n".join(chat_proxy.buffered))
        else:
            await update.effective_chat.send_message(
                "(Redo produced no reply — this was likely ordinary chat, not a game action.)",
                message_thread_id=update.message.message_thread_id,
            )


async def development_topic_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    is_owner = await _is_dev_topic_authorized(update, context)
    if is_owner is None:
        await update.effective_chat.send_message(
            "Couldn't verify permissions just now (a Telegram API call failed) — try again in a moment.",
            message_thread_id=config.TOPIC_DEVELOPMENT_ID,
        )
        return
    if not is_owner:
        await update.effective_chat.send_message(
            "The Development topic is restricted to the group owner.",
            message_thread_id=config.TOPIC_DEVELOPMENT_ID,
        )
        return

    question = update.message.text.strip()

    # TTS on/off toggle (2026-07-14) -- checked before the AI dev-question
    # flow gets a turn, same "specific command before general" pattern
    # used throughout ai/intent_parser.py all session. Persisted in
    # db.game_settings so it survives a bot restart, unlike an in-memory
    # flag would.
    lowered_question = question.lower()
    if any(w in lowered_question for w in ("turn on tts", "enable tts", "turn tts on")):
        db.set_setting("tts_enabled", "1")
        await _safe_send(
            update,
            "🔊 TTS narration is now **ON** — real narration will also be read aloud via @TextTSBot.",
            thread_id=config.TOPIC_DEVELOPMENT_ID,
        )
        return
    if any(w in lowered_question for w in ("turn off tts", "disable tts", "turn tts off")):
        db.set_setting("tts_enabled", "0")
        await _safe_send(update, "🔇 TTS narration is now **OFF**.", thread_id=config.TOPIC_DEVELOPMENT_ID)
        return

    # TTS backend switch (2026-07-18) -- see ai/tts_piper.py. Independent
    # of the on/off toggle above: switching backends while TTS is off is
    # harmless (nothing speaks either way), and this stays a separate
    # setting instead of overloading tts_enabled with a 3-way value, so
    # the on/off toggle's existing "1"/"0" contract never has to change.
    if any(w in lowered_question for w in ("use piper for tts", "switch tts to piper", "piper tts")):
        db.set_setting("tts_backend", "piper")
        await _safe_send(
            update,
            "🔊 TTS backend switched to **Piper** (local, no @TextTSBot dependency).",
            thread_id=config.TOPIC_DEVELOPMENT_ID,
        )
        return
    if any(w in lowered_question for w in ("use textbot for tts", "switch tts to textbot", "textbot tts")):
        db.set_setting("tts_backend", "textbot")
        await _safe_send(update, "🔊 TTS backend switched back to **@TextTSBot**.", thread_id=config.TOPIC_DEVELOPMENT_ID)
        return

    # Live narration-length override (2026-07-14) -- "set story mode to N"
    # lets Coffee try a different narration length immediately, without a
    # redeploy (see ai/story_mode.py's _level(), which now checks this
    # same db.game_settings key before falling back to config.STORY_MODE).
    # Confirmed live the same day: Coffee actually said "Set narration to
    # 5", not "story mode" -- the original pattern only matched "story
    # mode" literally, so his real command silently fell through to the
    # general AI dev-question flow with zero effect. "narration" is the
    # more natural word for this (it's what STORY_MODE's own .env comment
    # calls it -- "narration length/style"), so it's the primary
    # alternative covered here, alongside "story mode"/"narrative".
    story_mode_match = re.search(
        r"(?:set )?(?:story ?mode|narration(?: length| level)?|narrative)(?: to| =)?\s*(\d+)", lowered_question
    )
    if story_mode_match:
        level = max(0, min(10, int(story_mode_match.group(1))))
        db.set_setting("story_mode", str(level))
        await _safe_send(
            update, f"📖 Story mode set to **{level}** (0=shortest, 10=full novel-chapter prose).",
            thread_id=config.TOPIC_DEVELOPMENT_ID,
        )
        return

    # Live pause/resume for the two background autonomous loops (2026-07-14)
    # -- previously required a code edit + redeploy (AI_PARTY_ENABLED) or
    # wasn't pausable at all (Moltbook social).
    if any(w in lowered_question for w in ("turn on ai party", "enable ai party", "resume ai party")):
        db.set_setting("ai_party_enabled", "1")
        await _safe_send(update, "🎲 Autonomous AI party is now **ON**.", thread_id=config.TOPIC_DEVELOPMENT_ID)
        return
    if any(w in lowered_question for w in ("turn off ai party", "disable ai party", "pause ai party")):
        db.set_setting("ai_party_enabled", "0")
        await _safe_send(update, "⏸️ Autonomous AI party is now **OFF**.", thread_id=config.TOPIC_DEVELOPMENT_ID)
        return
    if any(w in lowered_question for w in ("turn on moltbook", "enable moltbook", "resume moltbook")):
        db.set_setting("moltbook_social_enabled", "1")
        await _safe_send(update, "🦞 Moltbook autonomous social is now **ON**.", thread_id=config.TOPIC_DEVELOPMENT_ID)
        return
    if any(w in lowered_question for w in ("turn off moltbook", "disable moltbook", "pause moltbook")):
        db.set_setting("moltbook_social_enabled", "0")
        await _safe_send(update, "⏸️ Moltbook autonomous social is now **OFF**.", thread_id=config.TOPIC_DEVELOPMENT_ID)
        return

    # Campaign-source link (2026-07-14): Coffee wants to send links to
    # campaign books/PDFs for a future Claude Code session to pull from
    # when the campaign-loading feature gets built. A link dropped here
    # is almost always "save this," not "answer a question about this,"
    # so it's recorded directly instead of spending an Ollama call on
    # answer_dev_question producing a generic, unhelpful response to a
    # bare URL. Same campaign_sources/INDEX.md record dev_topic_document_
    # handler writes to, so PDFs and links end up in one combined list.
    url_match = re.search(r"https?://\S+", question)
    if url_match:
        _append_campaign_source(f"Link: {url_match.group(0)} — {question}")
        await _safe_send(
            update,
            "🔗 Got it — logged in campaign_sources/INDEX.md for a future Claude Code session to pull from.",
            thread_id=config.TOPIC_DEVELOPMENT_ID,
        )
        return

    history = context.user_data.setdefault("dev_history", [])

    # Message CONTENT wasn't logged here before — same gap as Adventure
    # had (see the [intent] logging above). Coffee reported sending a
    # Development message this session that got no visible acknowledgment
    # here; this is so a future one is actually traceable.
    logger.info(f"[dev_topic] user={update.effective_user.id} text={question!r}")

    # Real live bug (dev-topic screenshot, 2026-07-18, "The Dev topic is
    # acting a little wonky"): a plain STATEMENT/directive ("The support
    # topic is incredibly important and it needs to function as a usable
    # wiki for the players") has nothing to actually answer, but every
    # message that reaches this point used to be forced through
    # answer_dev_question anyway -- which always tries to produce SOME
    # answer via a single-shot, ungrounded model call. With nothing real
    # to say, the model hallucinated disconnected-sounding nonsense
    # ("Configure ai/ for item application permissions..."). ai/dev_agent.py
    # has no equivalent grounding to ai/support_agent.py's catalog-grounded
    # answers, so the fix here is a deterministic gate instead: a message
    # only goes to the model if it's actually shaped like a question
    # (ends in "?", or opens with a real question word/ask-phrase).
    # Everything else is a statement Coffee is recording for a future
    # live Claude Code session to read (same spirit as the screenshot/
    # link handlers just above, which already skip the model entirely)
    # -- it gets a short, honest acknowledgment instead of a fabricated
    # "answer" to something nobody asked.
    is_question = "?" in question or bool(re.match(
        r"^\s*(why|how|what|when|where|who|which|can|could|should|would|"
        r"is|are|do|does|did|will|explain|tell me|show me)\b",
        lowered_question,
    ))
    if not is_question:
        reply = "📝 Noted — saved for a live Claude Code session to review."
    else:
        reply = await asyncio.to_thread(answer_dev_question, question, history)

    history.append(f"Developer: {question}")
    history.append(f"Assistant: {reply}")
    del history[:-MAX_ASSISTANT_HISTORY]

    await _safe_send(update, reply, thread_id=config.TOPIC_DEVELOPMENT_ID)


DEV_SCREENSHOTS_DIR = "dev_screenshots"


async def dev_topic_photo_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """
    Per Coffee's request (2026-07-11): he wants to drop screenshots into
    Development for troubleshooting. Photos have no update.message.text,
    so text_message_router's filters.TEXT handler never even sees them —
    confirmed live that a screenshot he sent produced zero log trace at
    all. This is a separate handler (registered on filters.PHOTO, which
    text messages never match, so there's no ordering conflict with
    text_message_router) that actually downloads the image to disk and
    logs its path, so a live Claude Code session (or a future automated
    check) can go look at it with the Read tool. Never actually "sees"
    the image itself here — this bot has no image-understanding
    capability of its own; it just makes the file available.
    """
    if update.message.message_thread_id != config.TOPIC_DEVELOPMENT_ID:
        return

    is_owner = await _is_dev_topic_authorized(update, context)
    if is_owner is None:
        await _safe_send(
            update,
            "Couldn't verify permissions just now (a Telegram API call failed) — try again in a moment.",
            thread_id=config.TOPIC_DEVELOPMENT_ID,
        )
        return
    if not is_owner:
        return

    os.makedirs(DEV_SCREENSHOTS_DIR, exist_ok=True)
    largest_photo = update.message.photo[-1]
    file = await context.bot.get_file(largest_photo.file_id)
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    filename = f"{timestamp}_{largest_photo.file_unique_id}.jpg"
    filepath = os.path.join(DEV_SCREENSHOTS_DIR, filename)
    await file.download_to_drive(filepath)

    caption = (update.message.caption or "").strip()
    logger.info(f"[dev_topic_image] user={update.effective_user.id} path={filepath!r} caption={caption!r}")

    # Routed through _safe_send (not a raw send_message) since this was
    # confirmed live 2026-07-11: a plain telegram.error.TimedOut on this
    # exact confirmation call threw unhandled, right after the real work
    # (downloading and saving the screenshot) had already succeeded --
    # the file was safely on disk, but the transient timeout looked to
    # Coffee like "not responding" with no indication anything happened.
    await _safe_send(
        update,
        f"📸 Got it — saved for troubleshooting at `{filepath}`. I can't see it from here myself "
        f"(no image capability in this running process), but it's ready for a live Claude Code "
        f"session to look at directly.",
        thread_id=config.TOPIC_DEVELOPMENT_ID,
    )


CAMPAIGN_SOURCES_DIR = "campaign_sources"
CAMPAIGN_SOURCES_INDEX = os.path.join(CAMPAIGN_SOURCES_DIR, "INDEX.md")


def _append_campaign_source(entry: str) -> None:
    """
    Running record of every campaign-book PDF/document and link Coffee
    sends via Development (2026-07-14), so nothing sent gets lost track
    of before the campaign-loading feature (Campaign topic, thread
    1941) is actually designed and built. A plain append-only Markdown
    log, same low-tech spirit as the other *_state.json cursor files.
    """
    os.makedirs(CAMPAIGN_SOURCES_DIR, exist_ok=True)
    timestamp = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    is_new = not os.path.exists(CAMPAIGN_SOURCES_INDEX)
    with open(CAMPAIGN_SOURCES_INDEX, "a") as f:
        if is_new:
            f.write("# Campaign sources\n\nPDFs/documents and links Coffee has sent for a future campaign-loading feature.\n\n")
        f.write(f"- [{timestamp}] {entry}\n")


async def dev_topic_document_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """
    Per Coffee's request (2026-07-14): he wants to send campaign-book
    PDFs (and other reference documents) into Development so a live
    Claude Code session has them on record to pull from later, when the
    campaign-loading feature itself gets designed. Mirrors
    dev_topic_photo_handler's pattern exactly (separate handler on
    filters.Document.ALL, no ordering conflict with filters.TEXT/PHOTO),
    except the original filename is kept (unlike a screenshot's
    synthetic name, a real document's name is meaningful) and every
    upload is recorded in campaign_sources/INDEX.md.
    """
    if update.message.message_thread_id != config.TOPIC_DEVELOPMENT_ID:
        return

    is_owner = await _is_dev_topic_authorized(update, context)
    if is_owner is None:
        await _safe_send(
            update,
            "Couldn't verify permissions just now (a Telegram API call failed) — try again in a moment.",
            thread_id=config.TOPIC_DEVELOPMENT_ID,
        )
        return
    if not is_owner:
        return

    os.makedirs(CAMPAIGN_SOURCES_DIR, exist_ok=True)
    doc = update.message.document
    file = await context.bot.get_file(doc.file_id)
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    safe_name = re.sub(r"[^A-Za-z0-9._-]", "_", doc.file_name or "document")
    filename = f"{timestamp}_{safe_name}"
    filepath = os.path.join(CAMPAIGN_SOURCES_DIR, filename)
    await file.download_to_drive(filepath)

    caption = (update.message.caption or "").strip()
    caption_note = f" — {caption}" if caption else ""
    _append_campaign_source(f"File: `{filepath}` (original name: {doc.file_name!r}){caption_note}")
    logger.info(f"[dev_topic_document] user={update.effective_user.id} path={filepath!r} caption={caption!r}")

    await _safe_send(
        update,
        f"📄 Got it — saved at `{filepath}` and logged in campaign_sources/INDEX.md for a future "
        f"Claude Code session to pull from when building the campaign-loading feature.",
        thread_id=config.TOPIC_DEVELOPMENT_ID,
    )


DEV_VIDEOS_DIR = "dev_videos"
VIDEO_FRAME_INTERVAL_SECONDS = 2  # one saved frame every N seconds -- enough to catch each page of a slow page-by-page walkthrough without saving hundreds of near-duplicate frames


async def dev_topic_video_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """
    Per Coffee's request (2026-07-15): sending the campaign as a video,
    showing it page by page, rather than one screenshot/PDF at a time.
    Mirrors dev_topic_photo_handler's pattern exactly (download to disk,
    log the path, never claim to "see" it from the live bot process
    itself) -- videos previously had NO handler at all (only filters.
    TEXT/PHOTO/Document.ALL were registered), so a video sent before
    this existed would have produced zero response, the same silent-
    failure gap photos had before 2026-07-11.

    Frame extraction uses the `imageio` package (which bundles its own
    ffmpeg binary via imageio-ffmpeg, invoked internally by that
    library, not by any subprocess/os.system call added here) to pull
    one frame every VIDEO_FRAME_INTERVAL_SECONDS and save it as a real
    JPEG a live Claude Code session can then read directly with vision
    (the same way saved screenshots already get read) -- this bot
    process still never "sees" the video itself, same disclosed
    limitation as the photo handler.
    """
    if update.message.message_thread_id != config.TOPIC_DEVELOPMENT_ID:
        return

    is_owner = await _is_dev_topic_authorized(update, context)
    if is_owner is None:
        await _safe_send(
            update,
            "Couldn't verify permissions just now (a Telegram API call failed) — try again in a moment.",
            thread_id=config.TOPIC_DEVELOPMENT_ID,
        )
        return
    if not is_owner:
        return

    os.makedirs(DEV_VIDEOS_DIR, exist_ok=True)
    video = update.message.video
    file = await context.bot.get_file(video.file_id)
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    video_filename = f"{timestamp}_{video.file_unique_id}.mp4"
    video_path = os.path.join(DEV_VIDEOS_DIR, video_filename)
    await file.download_to_drive(video_path)

    caption = (update.message.caption or "").strip()
    logger.info(f"[dev_topic_video] user={update.effective_user.id} path={video_path!r} caption={caption!r}")

    frame_count, frames_dir, extraction_error = await asyncio.to_thread(_extract_video_frames, video_path)

    if extraction_error:
        await _safe_send(
            update,
            f"🎥 Got the video — saved at `{video_path}`, but frame extraction failed ({extraction_error}). "
            f"The raw video file is still there for a live session to look at directly.",
            thread_id=config.TOPIC_DEVELOPMENT_ID,
        )
        return

    # Per Coffee (2026-07-16): delete the raw video once its frames are
    # safely extracted -- the frames are what a live session actually
    # reads, and raw video files add up fast on disk. Only removed on a
    # confirmed-successful extraction (the failure branch above keeps
    # the raw file, since there'd be nothing else to fall back on).
    try:
        os.remove(video_path)
    except OSError as e:
        logger.warning(f"[dev_topic_video] couldn't remove {video_path!r} after extraction: {e!r}")

    await _safe_send(
        update,
        f"🎥 Got it — extracted {frame_count} frame(s) (one every {VIDEO_FRAME_INTERVAL_SECONDS}s) "
        f"to `{frames_dir}` for a live Claude Code session to read page by page (the raw video itself "
        f"was deleted afterward to save disk space). I can't see it from here myself (no video "
        f"capability in this running process) — same as screenshots, the frames are ready for a live "
        f"session to look at directly.",
        thread_id=config.TOPIC_DEVELOPMENT_ID,
    )


def _extract_video_frames(video_path: str) -> tuple[int, str, str | None]:
    """
    Runs in a worker thread (see asyncio.to_thread above) since imageio's
    video decoding is blocking I/O/CPU work -- returns
    (frame_count, frames_dir, error_message_or_None). Never raises: a
    live session should still be able to look at the raw video even if
    frame extraction itself fails for some reason (corrupt file, codec
    imageio can't handle, etc.).
    """
    frames_dir = video_path + "_frames"
    try:
        import imageio.v3 as iio
    except ImportError as e:
        return 0, frames_dir, f"imageio not installed ({e})"

    try:
        os.makedirs(frames_dir, exist_ok=True)
        # imopen's context-manager form (rather than the immeta/imiter
        # shorthands) so the underlying ffmpeg subprocess pipe is always
        # closed -- confirmed via a real test run that the shorthand form
        # leaves it open (ResourceWarning), which matters here since this
        # bot runs 24/7 and could see many video uploads over time.
        with iio.imopen(video_path, "r", plugin="FFMPEG") as reader:
            fps = reader.metadata().get("fps", 30) or 30
            frame_stride = max(int(fps * VIDEO_FRAME_INTERVAL_SECONDS), 1)
            saved = 0
            for i, frame in enumerate(reader.iter()):
                if i % frame_stride != 0:
                    continue
                frame_path = os.path.join(frames_dir, f"frame_{saved:04d}.jpg")
                iio.imwrite(frame_path, frame)
                saved += 1
        return saved, frames_dir, None
    except Exception as e:
        return 0, frames_dir, str(e)


_NAMED_SHEET_EXCLUDED_WORDS = ("the", "a", "an", "her", "his", "their", "your", "our")
_SELF_SHEET_WORDS = ("my", "mine", "myself")
# Real live bug (2026-07-16, Coffee): "On my character sheet, I noticed
# there are spell slots. What are they used for?" matched the sheet-
# lookup regex below on "my character sheet" and just dumped his own
# sheet, completely ignoring the real question -- exactly the kind of
# genuine how-to-play question Support is supposed to actually explain,
# per Coffee's "make Support a wiki" goal. Any of these phrases means
# the player is asking what something MEANS, not asking to see the
# sheet itself, even if the message happens to mention "sheet" in
# passing -- skip the sheet-dump shortcut entirely and let it fall
# through to the real grounded Q&A model instead.
_SHEET_EXPLANATION_OVERRIDE_WORDS = (
    "what are", "what is", "what does", "what's", "used for", "how do", "how does", "why",
)


def _wants_sheet_names(question: str) -> list[str]:
    """
    Extracts any "<name>'s sheet"/"my sheet" references from a real
    Support-topic question -- empty if the question is really asking
    what something MEANS (see _SHEET_EXPLANATION_OVERRIDE_WORDS), even
    if it happens to mention "sheet" in passing.
    """
    lowered = question.lower()
    if any(w in lowered for w in _SHEET_EXPLANATION_OVERRIDE_WORDS):
        return []
    return re.findall(r"(\w+)(?:'s)? (?:character )?sheet", lowered)


async def support_topic_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if await asyncio.to_thread(db.is_banned, update.effective_user.id):
        return
    question = update.message.text.strip()
    _LAST_TOPIC_MESSAGE[(update.effective_chat.id, config.TOPIC_SUPPORT_ID)] = {
        "kind": "support",
        "update": update,
        "context": context,
        "text": question,
        "user_id": update.effective_user.id,
        "timestamp": datetime.now(timezone.utc),
    }

    # Physical-dice mode toggle (2026-07-16, per Coffee: "toggle in
    # support topic"), checked before anything else so it's a real
    # state change, not routed through the general Q&A model.
    lowered_question = question.lower()
    if any(w in lowered_question for w in ("own dice", "own physical dice", "dice on", "dice off",
                                             "let the game roll", "turn on manual dice", "turn off manual dice",
                                             "turn on physical dice", "turn off physical dice")):
        await _do_toggle_manual_dice(update, question, thread_id=config.TOPIC_SUPPORT_ID)
        return

    character = db.get_character(update.effective_user.id)
    # 2026-07-14, per Coffee: "Is Sarah in my current party?" and "Show
    # me my party character sheets" both had nothing real to answer from
    # -- only the asking player's OWN character was ever passed here.
    party_members = _get_party_members() if character else None

    # Same "show me X's sheet" capability as Adventure (same regex as
    # ai/intent_parser.py's check_sheet trigger), per Coffee's explicit
    # request (2026-07-14) that this "shud be able to work in adventure
    # and support chat." A named-lookup sheet is a real, fixed fact --
    # same reasoning as every other deterministic answer in
    # ai/support_agent.py -- so it's resolved here directly rather than
    # handed to the model.
    #
    # Confirmed live 2026-07-14 (Coffee, real screenshot): "Show me my
    # character sheet and Sarah's character sheet" -- TWO sheets in one
    # message -- used re.search, which only ever finds the FIRST match
    # ("my", which was then excluded), so this fell through to the LLM
    # entirely. The model then answered Coffee's own sheet reasonably
    # but OUTRIGHT INVENTED Sarah's ("mirrors this structure... similar
    # stats based on available data") -- a real hallucination of fake
    # stats, exactly what this game's grounding rules exist to prevent.
    # Fixed with re.findall (every match, not just the first) and a
    # real per-name lookup for each one, never handing ANY of it to the
    # model once at least one real name resolves.
    sheet_names = _wants_sheet_names(question)
    if sheet_names:
        seen = set()
        replies = []
        for raw_name in sheet_names:
            if raw_name in seen or raw_name in _NAMED_SHEET_EXCLUDED_WORDS:
                continue
            seen.add(raw_name)
            if raw_name in _SELF_SHEET_WORDS:
                replies.append(
                    _format_character_sheet(character) if character is not None
                    else "You don't have a character yet."
                )
                continue
            target = _find_party_target_by_name(raw_name) or db.find_character_by_name(raw_name)
            if target is not None:
                replies.append(_format_character_sheet(target))
                continue
            npc = _find_campaign_npc_by_name(raw_name)
            replies.append(
                _format_npc_basic_info(npc) if npc is not None
                else f"Nobody named {raw_name.title()} is playing right now — can't show a sheet for them."
            )
        if replies:
            reply = "\n\n".join(replies)
            logger.info(f"[support] user={update.effective_user.id} text={question!r} reply={reply!r}")
            await _safe_send(update, reply, thread_id=config.TOPIC_SUPPORT_ID)
            return

    # Real "tell them, then do it" fix (2026-07-18, per Coffee, after a
    # live dev-topic report of two genuine Support questions getting a
    # dead-end "couldn't answer in time" apology): everything past this
    # point genuinely can take minutes on this hardware (see
    # answer_support_question's retry loop) -- the typing indicator
    # alone wasn't a strong enough signal, so an explicit heads-up is
    # sent up front, before the long call starts, not just shown as an
    # ambient animation.
    await _safe_send(
        update,
        "⏳ Looking that up now — this can take a minute or two under load. I'll reply here as soon as I have it.",
        thread_id=config.TOPIC_SUPPORT_ID,
    )
    async with _keep_typing(update.effective_chat, config.TOPIC_SUPPORT_ID):
        reply = await asyncio.to_thread(answer_support_question, question, character, party_members)
    logger.info(f"[support] user={update.effective_user.id} text={question!r} reply={reply!r}")
    await _safe_send(update, reply, thread_id=config.TOPIC_SUPPORT_ID)


# Per-user message queues — see _run_in_user_order for why these exist.
_USER_QUEUES: dict[int, asyncio.Queue] = {}
_USER_WORKERS: dict[int, asyncio.Task] = {}


async def _user_queue_worker(queue: asyncio.Queue) -> None:
    while True:
        coro_fn, future = await queue.get()
        try:
            result = await coro_fn()
            if not future.done():
                future.set_result(result)
        except Exception as e:  # noqa: BLE001 — propagated to the caller's await, not swallowed
            if not future.done():
                future.set_exception(e)
        finally:
            queue.task_done()


def _get_user_queue(user_id: int) -> asyncio.Queue:
    if user_id not in _USER_QUEUES:
        queue: asyncio.Queue = asyncio.Queue()
        _USER_QUEUES[user_id] = queue
        _USER_WORKERS[user_id] = asyncio.create_task(_user_queue_worker(queue))
    return _USER_QUEUES[user_id]


async def _run_in_user_order(user_id: int, coro_fn) -> None:
    """
    Ensures this ONE user's messages are handled strictly in the order
    they arrived, despite python-telegram-bot dispatching every update
    concurrently (concurrent_updates=True — deliberate, so one slow
    handler never freezes every other player; see build_application).
    Without this, two quick messages from the SAME player race: intent
    classification alone takes 30-160s (this hardware, this model), so
    the second message's classification can easily finish before the
    first's, and get processed out of order — corrupting stateful flows
    like character creation (which tracks a step machine in
    context.user_data) just as easily as it'd confuse ordinary play.
    Different players are completely unaffected and still run fully
    concurrently with each other and with this user's queue.
    """
    queue = _get_user_queue(user_id)
    future = asyncio.get_event_loop().create_future()
    await queue.put((coro_fn, future))
    await future


class _TranscribedMessageProxy:
    """
    Duck-typed stand-in for update.message/effective_message: identical
    to the real one except .text, which is swapped for a real STT
    transcription (task #97, 2026-07-22). Needed because python-
    telegram-bot's real Message can't have .text reassigned in place
    (TelegramObject enforces frozen attributes) -- same reasoning as
    _EffectiveChatOverride above, just for .text instead of
    effective_chat.
    """
    def __init__(self, real_message, transcribed_text: str):
        self._real_message = real_message
        self.text = transcribed_text

    def __getattr__(self, name):
        return getattr(self._real_message, name)


class _VoiceTranscriptUpdate:
    """
    Duck-typed stand-in for Update: identical to the real one except
    .message/.effective_message, swapped for a _TranscribedMessageProxy
    -- lets a transcribed voice message flow through the EXACT same
    real text pipeline (_route_text_message, adventure_master_handler,
    intent parsing, the works) a typed message already uses, never a
    separate parallel voice-only code path.
    """
    def __init__(self, real_update: Update, transcribed_text: str):
        self._real_update = real_update
        proxy = _TranscribedMessageProxy(real_update.message, transcribed_text)
        self.message = proxy
        self.effective_message = proxy

    def __getattr__(self, name):
        return getattr(self._real_update, name)


async def voice_message_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """
    Voice message input (task #97, 2026-07-22 investigation: "investigate
    better tts/stt options that are FREE"). Downloads the real voice
    note, transcribes it via Groq's free Whisper endpoint
    (ai/stt_groq.py -- cloud-hosted, no local CPU cost, since a local
    STT model would compete with Ollama's single generation slot on
    this box), echoes back what was heard (voice transcription is
    genuinely error-prone, so a player can tell at a glance whether it
    needs retyping), then routes the transcription through the exact
    same real pipeline a typed message already uses.
    """
    if update.message is None or update.message.voice is None:
        return
    if not config.GROQ_API_KEY:
        return  # STT not configured -- silently a no-op, not an error, same convention as TTS being off
    telegram_file = await update.message.voice.get_file()
    audio_bytes = await telegram_file.download_as_bytearray()
    transcribed = await asyncio.to_thread(stt_groq.transcribe_voice, bytes(audio_bytes))
    if not transcribed:
        return
    await update.effective_chat.send_message(
        f"🎙️ Heard: \"{transcribed}\"", message_thread_id=update.message.message_thread_id,
    )
    transcript_update = _VoiceTranscriptUpdate(update, transcribed)
    if update.effective_user is None:
        await _route_text_message(transcript_update, context)
        return
    await _run_in_user_order(update.effective_user.id, lambda: _route_text_message(transcript_update, context))


async def _route_text_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    raw_thread_id = update.message.message_thread_id  # may genuinely be None for Main
    thread_id = raw_thread_id or 0

    if topics.is_main(raw_thread_id):
        return  # Main is human-to-human chat only — the bot never speaks here

    if topics.is_adventure(thread_id):
        await adventure_master_handler(update, context)
        return

    if topics.is_development(thread_id):
        await development_topic_handler(update, context)
        return

    if topics.is_support(thread_id):
        await support_topic_handler(update, context)
        return

    guild_id = topics.guild_id_for_topic(thread_id)
    if guild_id:
        await guild_topic_handler(update, context, guild_id)
        return


async def text_message_router(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """
    Single entry point for ALL plain text messages, across every topic.
    This exists because registering per-topic handlers separately with
    identical filters caused a real bug: python-telegram-bot only runs
    the FIRST matching handler per update by default, so later ones
    never fired at all, in ANY topic. Routing internally, in one
    handler, guarantees every topic's code path actually runs. The
    actual routing is
    queued per-user (_run_in_user_order) so this player's own messages
    can never be handled out of order.
    """
    if update.message is None or not update.message.text:
        return
    if update.effective_user is None:
        await _route_text_message(update, context)
        return

    user_id = update.effective_user.id
    await _run_in_user_order(user_id, lambda: _route_text_message(update, context))


async def _log_unhandled_error(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    """
    Global safety net. Without this registered, python-telegram-bot's
    default behavior on any unhandled exception inside a handler (a
    flaky Telegram API call, a bug in narration/game logic, etc.) is to
    log it at its own internal level and otherwise fail completely
    silently — no reply reaches the player, and nothing distinguishes
    that from the handler simply choosing not to respond. This is the
    most likely explanation for a real incident where an NPC dialogue
    message got zero reply, not even the intended "..." fallback.
    """
    logger.error("Unhandled exception while processing update: %r", update, exc_info=context.error)
    if isinstance(update, Update) and update.message:
        try:
            await update.effective_chat.send_message(
                "Something went wrong processing that — try again in a moment.",
                message_thread_id=update.message.message_thread_id,
            )
        except Exception:
            pass


# ---------------------------------------------------------------------
# Autonomous AI-played party (2026-07-10, per Coffee) — a separate
# party from any human's, playing the game for real through the exact
# same natural-language pipeline a human message hits (intent parsing,
# action dispatch, the works), for genuine ongoing playtesting. Combat
# for these characters already auto-resolves via the existing is_ai=1
# mechanism (_resolve_ai_turns) with no changes needed here — this loop
# only ever generates their OUT-of-combat actions.
# ---------------------------------------------------------------------
AI_PARTY_ROSTER = [
    {
        "name": "Zara Windrift", "race": "elf", "char_class": "ranger",
        "ability_scores": {"strength": 12, "dexterity": 17, "constitution": 13,
                            "intelligence": 11, "wisdom": 15, "charisma": 10},
        "hp_max": 11, "armor_class": 14, "gold": 25,
        "inventory": {"longbow": 1, "shortsword": 1, "leather_armor": 1, "rations": 2},
        "personality": "curious and methodical, always wants to know what's over the next hill",
    },
    {
        "name": "Bram Ashfield", "race": "dwarf", "char_class": "cleric",
        "ability_scores": {"strength": 13, "dexterity": 10, "constitution": 15,
                            "intelligence": 10, "wisdom": 16, "charisma": 11},
        "hp_max": 10, "armor_class": 14, "gold": 25,
        "inventory": {"shortsword": 1, "leather_armor": 1, "healing_potion": 1},
        "personality": "steady and protective, cautious about picking fights but never backs down from one already started",
    },
]

AI_PARTY_TICK_INTERVAL_SECONDS = getattr(config, "AI_PARTY_TICK_INTERVAL_SECONDS", 900)
_LAST_AI_PARTY_TICK_AT: datetime | None = None

# Paused 2026-07-12 per Coffee's direct request, while reliability for
# real human players is the priority (ahead of eventually going public)
# -- does NOT delete or reset anything, existing AI companions stay in
# the party/DB exactly as they are, this just stops new autonomous
# ticks from being generated. Flip back to True to resume.
AI_PARTY_ENABLED = False


def _ensure_ai_party_exists() -> None:
    """Creates the autonomous AI party once, if it doesn't already exist, and forms them into their own party."""
    existing = db.get_autonomous_players()
    if existing:
        return
    party_id = None
    for member in AI_PARTY_ROSTER:
        char = db.create_ai_companion(
            name=member["name"], race=member["race"], char_class=member["char_class"],
            ability_scores=member["ability_scores"], hp_max=member["hp_max"],
            armor_class=member["armor_class"], gold=member["gold"], inventory=member["inventory"],
        )
        db.mark_autonomous(char["telegram_user_id"])
        if party_id is None:
            party_id = db.create_party(char["telegram_user_id"])
        else:
            db.add_ai_companion_to_party(char["telegram_user_id"], party_id)
    logger.info(f"[ai_party] created autonomous AI party, party_id={party_id}")


def _party_gather_needs_here(location_id: str) -> list[dict]:
    """
    Real, currently-accepted party gather-quest objectives that THIS
    location's resource nodes can actually satisfy right now (2026-07-14,
    per Coffee: recruits/AI party members should recognize "the party
    has an active gather quest and I can do something about it here"
    on their own, not just wait to be told -- e.g. gathering silverleaf
    herb in the tavern cellar for a bounty posted up at the tavern
    itself). Board quests are checked across the WHOLE party, not just
    one character, since the board/progress tracking is shared -- and
    across all locations' boards, not just this one, since a quest is
    often posted somewhere else (the tavern) while the actual resource
    node lives in a connected sub-location (the cellar).
    """
    location = cl.get_location(CAMPAIGN, location_id)
    if not location:
        return []
    nodes_by_material = {n["material"]: n for n in location.get("resource_nodes", [])}
    if not nodes_by_material:
        return []
    seen_ids = set()
    needs = []
    for member in _get_party_members():
        for q in db.get_accepted_board_quests_for_user(member["telegram_user_id"]):
            if (
                q["board_quest_id"] in seen_ids
                or q.get("objective_type") != "gather_material"
                or q["objective_target"] not in nodes_by_material
                or q["progress_count"] >= q["objective_count"]
            ):
                continue
            seen_ids.add(q["board_quest_id"])
            node = nodes_by_material[q["objective_target"]]
            item = items_module.get_item(q["objective_target"])
            needs.append({
                "quest_title": q["title"],
                "material_name": item["name"] if item else q["objective_target"],
                "remaining": q["objective_count"] - q["progress_count"],
                "node_name": node["name"],
            })
    return needs


def _build_ai_player_situation_facts(character: dict, location_id: str) -> str:
    location = cl.get_location(CAMPAIGN, location_id)
    if not location:
        return "You aren't sure where you are."

    lines = [f"Location: {location['name']} — {location['description']}"]

    # Same reasoning as every other grounding fix here: the AI was never
    # told what it's actually carrying or actually knows how to cast, so
    # "sell my X" or "I cast X" (the latter a literal hardcoded example in
    # ai/autonomous_player.py's prompt) had nothing real to check against
    # -- risking the exact same verbatim-example-parroting failure already
    # confirmed and fixed for movement ("I head to the whispering wood").
    inventory = character.get("inventory") or {}
    if inventory:
        item_names = [items_module.get_item(i)["name"] for i in inventory if items_module.get_item(i)]
        if item_names:
            lines.append(f"You're carrying: {', '.join(item_names)}")
    known_spells = character.get("known_spells") or []
    if known_spells:
        spell_names = [spells_module.get_spell(s)["name"] for s in known_spells if spells_module.get_spell(s)]
        if spell_names:
            lines.append(f"Spells you know: {', '.join(spell_names)}")

    # Per Coffee (2026-07-14): AI-controlled characters should "naturally
    # know" what they're strong or weak at overall, not just at the one
    # resource node in front of them -- lets the model lean toward a
    # skill it's already good at when several options are open, or
    # recognize a weak/untried one as worth practicing.
    practiced_skills = {k: v for k, v in (character.get("skill_uses") or {}).items() if v > 0}
    if practiced_skills:
        strong = sorted(practiced_skills.items(), key=lambda kv: -kv[1])
        lines.append(
            "Skills you've practiced: "
            + ", ".join(f"{skill} +{practiced_bonus(uses)} ({uses} uses)" for skill, uses in strong)
        )

    npcs_here = _npcs_at_location(location_id)
    if npcs_here:
        names = [CAMPAIGN["npcs"][n]["name"] for n in npcs_here if n in CAMPAIGN["npcs"]]
        lines.append(f"People here: {', '.join(names)}")
        # Same reasoning as every other grounding fix here: recruiting is
        # a real action (_do_recruit_npc), but nothing distinguished a
        # recruitable NPC from an ordinary shopkeeper/one-off character in
        # "People here", so the AI party had no way to know who could
        # actually be asked to join.
        already_in_party = {p["name"] for p in _get_party_members()}
        recruitable_here = [
            CAMPAIGN["npcs"][n]["name"] for n in npcs_here
            if n in CAMPAIGN["npcs"] and CAMPAIGN["npcs"][n].get("recruitable")
            and CAMPAIGN["npcs"][n]["name"] not in already_in_party
        ]
        if recruitable_here:
            lines.append(f"You could recruit: {', '.join(recruitable_here)}")
    monsters_here = location.get("monsters", [])
    if monsters_here:
        lines.append(f"Danger here: {', '.join(monsters_here)}")
    # Mirrors _do_move's own reachable-destination computation (minus
    # locked_connections, which need an item the AI player may not have
    # and aren't unconditionally reachable) -- without descends_to/
    # ascends_to here, the AI party would never even learn sub-locations
    # like tavern_cellar/tavern_upstairs exist, since _do_move itself
    # treats those as fully reachable but this list previously only
    # looked at "connections".
    reachable = list(location.get("connections", []))
    if "descends_to" in location:
        reachable.append(location["descends_to"])
    if "ascends_to" in location:
        reachable.append(location["ascends_to"])
    if reachable:
        conn_names = [cl.get_location(CAMPAIGN, c)["name"] for c in reachable]
        lines.append(f"Places reachable from here: {', '.join(conn_names)}")
    interactables = location.get("interactables", {})
    if interactables:
        lines.append(f"Things worth a closer look: {', '.join(i['name'] for i in interactables.values())}")
    resource_nodes = location.get("resource_nodes", [])
    if resource_nodes:
        # Per Coffee (2026-07-14): AI-controlled characters should
        # "naturally know" what they're already good at versus what's
        # worth practicing -- annotate each node with its skill and this
        # character's real, current proficiency (same practiced_bonus
        # mechanic shown on the character sheet), so gathering something
        # you're untried at reads as a genuine opportunity to level up,
        # not a random guess.
        skill_uses = character.get("skill_uses") or {}
        node_descriptions = []
        for n in resource_nodes:
            skill_key = n.get("skill", n.get("ability"))
            uses = skill_uses.get(skill_key, 0)
            proficiency_note = (
                f"you're not practiced in {skill_key} yet" if uses == 0
                else f"{skill_key} +{practiced_bonus(uses)}, {uses} use{'s' if uses != 1 else ''}"
            )
            node_descriptions.append(f"{n['name']} ({proficiency_note})")
        lines.append(f"Resources here: {', '.join(node_descriptions)}")

        gather_needs = _party_gather_needs_here(location_id)
        for need in gather_needs:
            lines.append(
                f"The party's quest \"{need['quest_title']}\" still needs {need['remaining']} "
                f"more {need['material_name']} -- you could gather that right here to help finish it."
            )

    # 2026-07-14, per Coffee: gathering already had grounding above, but
    # crafting and campfires (both shipped the same day) didn't -- the
    # AI party had real facts to gather materials but nothing telling it
    # what those materials could actually MAKE, or that a campfire was
    # ever an option. Same "only mention what's real right now" pattern
    # as every other fact here: only lists a recipe if the character
    # genuinely has the materials for it, and only mentions the campfire
    # if they're actually carrying wood and not already at full HP
    # (matching _do_make_campfire's own real refusal conditions exactly).
    craftable = [
        items_module.get_item(recipe["result_item"])["name"]
        for recipe in RECIPES.values()
        if has_materials(inventory, recipe) and items_module.get_item(recipe["result_item"])
    ]
    if craftable:
        lines.append(f"You have the materials to craft: {', '.join(craftable)}")
    if inventory.get("wood", 0) >= 1 and character.get("hp_current", 0) < character.get("hp_max", 0):
        lines.append("You're carrying wood and could make a campfire to recover some HP.")
    shop_id = location.get("shop")
    if shop_id:
        # Same reasoning as the descends_to/ascends_to fix above: without
        # naming what's actually for sale, the AI has nothing real to
        # reference for a "buy" action and would have to either invent an
        # item or parrot the "healing potion" example verbatim regardless
        # of whether this shop even sells one -- _do_buy only matches
        # against shop_data["inventory"], so an ungrounded guess just
        # bounces with "not sure what item you mean" instead of a real
        # purchase.
        shop_data = cl.get_shop(CAMPAIGN, shop_id)
        item_names = [items_module.get_item(i)["name"] for i in shop_data.get("inventory", []) if items_module.get_item(i)]
        if item_names:
            lines.append(f"Shop here sells: {', '.join(item_names)}")
        else:
            lines.append("There is a shop here.")

    # Story quests (campaign.json's hand-authored catalog) are a
    # separate thing from the area quest board below, and were missing
    # here entirely — the AI party could see a board bounty posted, but
    # never learned a real story quest was on offer at its own location,
    # so it had no way to know "accept the quest" was ever a sensible
    # thing to say for one of those.
    story_offer = _offerable_quest_at_location(character, location_id)
    if story_offer:
        lines.append(f"A quest is on offer here: {story_offer[1]['title']}")
    else:
        # Real live gap found via direct monitoring (2026-07-22): once
        # the AI party wandered away from wherever a quest was actually
        # offered (its own starting location, in the confirmed case),
        # this fact -- and the "accepting an offered quest is the
        # single highest priority" guidance in ai/autonomous_player.py
        # -- had nothing left to trigger on, ever again, since a
        # quest's offer only ever showed while standing exactly there.
        # Surfaces a real, grounded reminder to go back for it instead.
        for quest_id, quest in CAMPAIGN.get("quests", {}).items():
            offer_location = quest.get("location")
            if not offer_location or offer_location == location_id:
                continue
            if quest_id in character["completed_quests"] or quest_id in character["active_quests"]:
                continue
            offer_loc_data = cl.get_location(CAMPAIGN, offer_location)
            if offer_loc_data:
                lines.append(
                    f"A quest (\"{quest['title']}\") is on offer back at {offer_loc_data['name']} -- "
                    "you could head there to accept it."
                )
                break

    # Per Coffee (2026-07-14): a recruited companion's own personal
    # quest should be something the AI party can choose to help with
    # too, not just something a human player notices.
    companion_offer = _offerable_companion_quest(character)
    if companion_offer:
        lines.append(f"A party companion has a personal task on offer: {companion_offer[1]['title']}")

    board_quests = board_quests_module.get_todays_board_quests(location_id)
    unclaimed = [q for q in board_quests if not q.get("accepted_by") and not q.get("completed_at")]
    if unclaimed:
        lines.append(f"Quest board has something posted: {', '.join(q['title'] for q in unclaimed)}")

    # Without this, an AI party member whose accepted branching quest is
    # already objective-complete has no way to know a real decision is
    # waiting on it, or what the two actual choices even are -- it would
    # just sit on it forever, since resolve_choice requires saying one of
    # the exact real labels (see _do_resolve_quest_choice), and nothing
    # else here ever surfaces what those labels are.
    ready_choices = [
        q for q in db.get_accepted_board_quests_for_user(character["telegram_user_id"])
        if q.get("branch_data") and q["progress_count"] >= q["objective_count"]
    ]
    for q in ready_choices:
        labels = ", ".join(f'"{c["label"]}"' for c in q["branch_data"]["choices"].values())
        lines.append(f"You have a decision to make on \"{q['title']}\" -- your options are: {labels}")

    if character.get("active_quests"):
        # Real live gap found via direct log monitoring (2026-07-22): the
        # AI party has been looping on the same few flavor actions
        # (examine the same tree, gather the same herb) for days without
        # ever meaningfully progressing the story -- traced to this line
        # only ever giving a bare COUNT of active quests, never WHERE one
        # actually needs to go next. A quest's own real objective_location
        # field (campaign.json) already exists for exactly this; it was
        # simply never read here. Only surfaced for quests whose
        # objective isn't already right here (otherwise the reach_location
        # trigger already fires on its own the moment they arrived, and
        # this would just be pointing at where they're already standing).
        elsewhere_quests = []
        for quest_id in character["active_quests"]:
            quest = CAMPAIGN.get("quests", {}).get(quest_id)
            objective_id = quest.get("objective_location") if quest else None
            if objective_id and objective_id != location_id:
                objective_loc = cl.get_location(CAMPAIGN, objective_id)
                if objective_loc:
                    elsewhere_quests.append((quest["title"], objective_loc["name"]))
        if elsewhere_quests:
            for title, dest_name in elsewhere_quests:
                lines.append(f"Your active quest \"{title}\" needs you to travel to {dest_name}.")
        else:
            lines.append(f"You have {len(character['active_quests'])} active quest(s) in your journal already.")

    # Same reasoning as the reachable-places/shop/branching-choice facts
    # above: guild joining is a real, location-independent action
    # (_do_join_guild has no location check at all), but nothing here
    # ever told the AI party a guild even existed, so it could never
    # autonomously decide to join one.
    current_guild = character.get("guild")
    joinable = [
        guild["name"] for guild_id, guild in GUILDS.items()
        if guild_id != current_guild and eligible_for_guild(character, guild_id)[0]
    ]
    if joinable:
        lines.append(f"Guilds you could join: {', '.join(joinable)}")

    return "\n".join(lines)


async def _maybe_auto_roll_pending_dice(bot) -> None:
    """
    Per Coffee (2026-07-19): "give the user 1 minute to roll - if not
    then auto-roll" -- runs every idle-loop tick (60s cadence, see
    _idle_inactivity_loop) and auto-resolves any _PENDING_DICE_ROLLS
    entry that's been waiting DICE_ROLL_AUTO_TIMEOUT_SECONDS or longer,
    using a real d20 roll in place of the manual one the player never
    reported -- same dispatch this'd get from a real manual reply, just
    with an auto-rolled value instead. Also incidentally bounds task
    #186's restart-survival gap: a pending roll can now only ever be
    silently lost for at most one idle-loop tick after a restart,
    instead of forever.
    """
    now = datetime.now(timezone.utc)
    expired_user_ids = [
        user_id for user_id, pending in _PENDING_DICE_ROLLS.items()
        if (now - datetime.fromisoformat(pending["created_at"])).total_seconds() >= DICE_ROLL_AUTO_TIMEOUT_SECONDS
    ]
    for user_id in expired_user_ids:
        pending = _PENDING_DICE_ROLLS.pop(user_id, None)
        if pending is None:
            continue
        character = db.get_character(user_id)
        name = character["name"] if character else "Someone"
        auto_value = roll_d20()
        update_like = _AiPlayerUpdate(bot, pending["chat_id"], user_id, pending["action_text"])
        logger.info(
            f"[dice] user={user_id} auto-rolled pending {pending['kind']} roll with "
            f"{auto_value} (no manual reply within {DICE_ROLL_AUTO_TIMEOUT_SECONDS}s)"
        )
        await _safe_send(update_like, f"⏱️ **{name}** didn't roll in time — auto-rolling: 🎲 {auto_value}.")
        try:
            if pending["kind"] == "attack":
                await _do_attack(update_like, pending["action_text"], forced_roll=auto_value)
            elif pending["kind"] == "skill_check":
                await _do_skill_check(update_like, pending["ability"], pending["action_text"], forced_roll=auto_value)
            elif pending["kind"] == "gather":
                await _do_gather(update_like, pending["action_text"], forced_roll=auto_value)
            elif pending["kind"] == "shove":
                await _do_shove(update_like, pending["action_text"], forced_roll=auto_value)
            elif pending["kind"] == "flee":
                await _do_flee(update_like, pending["action_text"], forced_roll=auto_value)
            elif pending["kind"] == "steal":
                await _do_steal(update_like, pending["action_text"], forced_roll=auto_value)
        except Exception as e:
            logger.error(f"[dice] auto-roll dispatch for user={user_id} kind={pending['kind']} raised: {e!r}")


async def _ai_party_autonomous_tick(bot) -> None:
    """
    Advances ONE AI-controlled character's turn per cycle (never all at
    once, to keep Adventure from being flooded). Skips anyone currently
    in an active combat session — that already auto-resolves via the
    existing is_ai=1 mechanism with no action needed here.

    2026-07-14, per Coffee: "make recruitable able to make their own
    choices... this goes for AIs also." Previously only the separate
    hardcoded autonomous party (Zara/Bram) ever got a tick here --
    recruited companions like Sarah sat completely idle between being
    directly talked to, with no way to notice and act on something like
    an active party gather quest on their own. Broadened to every
    AI-controlled character (db.get_ai_controlled_characters), sharing
    the same one-actor-per-tick rotation so this doesn't flood the chat
    any more than before.
    """
    global _LAST_AI_PARTY_TICK_AT
    # 2026-07-14: now a live toggle (Development-topic "turn on/off ai
    # party") instead of a hardcoded flag needing a code edit + redeploy
    # to flip. AI_PARTY_ENABLED below is just the seed default (still
    # paused) for whenever no live override has been set yet.
    if db.get_setting("ai_party_enabled", "1" if AI_PARTY_ENABLED else "0") != "1":
        return
    if _LAST_KNOWN_CHAT_ID is None:
        return

    now = datetime.now(timezone.utc)
    if _LAST_AI_PARTY_TICK_AT and (now - _LAST_AI_PARTY_TICK_AT).total_seconds() < AI_PARTY_TICK_INTERVAL_SECONDS:
        return

    _ensure_ai_party_exists()
    roster = db.get_ai_controlled_characters()
    if not roster:
        return

    # Whoever's acted least recently goes next — a simple, fair rotation.
    roster.sort(key=lambda c: c.get("last_active_at") or "")
    actor = next(
        (c for c in roster if not _in_active_combat(c["telegram_user_id"], _LAST_KNOWN_CHAT_ID)), None
    )
    if actor is None:
        return  # everyone's currently mid-fight; nothing to do this cycle

    _LAST_AI_PARTY_TICK_AT = now

    user_id = actor["telegram_user_id"]
    context_like = _AI_PLAYER_CONTEXTS.setdefault(user_id, _AiPlayerContext())
    last_action = context_like.user_data.get("last_autonomous_action")

    # Recruited companions (e.g. Sarah) aren't in the hardcoded
    # AI_PARTY_ROSTER at all -- their real personality lives in
    # campaign.json's NPC entry instead, same place their sheet-lookup
    # basic-info fallback already reads from.
    personality = next((m["personality"] for m in AI_PARTY_ROSTER if m["name"] == actor["name"]), None)
    if personality is None:
        npc = _find_campaign_npc_by_name(actor["name"])
        personality = npc.get("personality", "") if npc else ""
    situation_facts = _build_ai_player_situation_facts(actor, actor["current_location"])
    # Task #222, per Coffee: a human party member can tell an AI
    # companion something mid-fight (see _do_message_ai) without it
    # counting as anyone's turn -- that message is stashed here as real
    # guidance and read exactly once, on this actor's own very next
    # autonomous decision, then cleared so it never lingers past that.
    guidance = context_like.user_data.pop("human_guidance", None)
    if guidance:
        situation_facts += f"\nA party member just told you directly: \"{guidance}\""
    action_text = await asyncio.to_thread(choose_next_action, actor, personality, situation_facts, last_action)
    context_like.user_data["last_autonomous_action"] = action_text

    update_like = _AiPlayerUpdate(bot, _LAST_KNOWN_CHAT_ID, user_id, action_text)
    try:
        await adventure_master_handler(update_like, context_like)
    except Exception as e:
        logger.error(f"[ai_party] {actor['name']}'s autonomous turn raised: {e!r}")


async def _idle_inactivity_loop(application: Application) -> None:
    """
    Background loop for the automatic 5-minute-silence inactivity check
    (see _check_idle_characters). A self-managed asyncio loop rather
    than python-telegram-bot's JobQueue, since that requires an extra
    dependency (the `[job-queue]` install extra) not currently installed
    on this server — not worth adding for one simple periodic check.
    """
    while True:
        await asyncio.sleep(IDLE_CHECK_INTERVAL_SECONDS)
        try:
            await _check_idle_characters(application.bot)
        except Exception as e:
            logger.error(f"[idle_check] background loop failed this cycle: {e!r}")
        try:
            _wander_npcs()
        except Exception as e:
            logger.error(f"[world_tick] npc wander failed this cycle: {e!r}")
        try:
            await _maybe_post_world_heartbeat(application.bot)
        except Exception as e:
            logger.error(f"[world_tick] heartbeat failed this cycle: {e!r}")
        try:
            await _maybe_spawn_world_boss(application.bot)
        except Exception as e:
            logger.error(f"[world_tick] world boss spawn check failed this cycle: {e!r}")
        try:
            await _maybe_post_hourly_status_update(application.bot)
        except Exception as e:
            logger.error(f"[hourly_update] failed this cycle: {e!r}")
        try:
            db.expire_stale_board_quests()
        except Exception as e:
            logger.error(f"[board_quests] expiry check failed this cycle: {e!r}")
        try:
            await _maybe_auto_roll_pending_dice(application.bot)
        except Exception as e:
            logger.error(f"[dice] auto-roll check failed this cycle: {e!r}")
        try:
            await _ai_party_autonomous_tick(application.bot)
        except Exception as e:
            logger.error(f"[ai_party] autonomous tick failed this cycle: {e!r}")
        try:
            await _maybe_check_moltbook_activity(application.bot)
        except Exception as e:
            logger.error(f"[moltbook_heartbeat] failed this cycle: {e!r}")
        try:
            await _maybe_run_moltbook_social_tick(application.bot)
        except Exception as e:
            logger.error(f"[moltbook_social] tick failed this cycle: {e!r}")
        try:
            # Task #159 combat-persistence safety net (see sessions.py's
            # module docstring): a periodic snapshot rather than one on
            # every single mutation, so a redeploy loses at most ~60s of
            # combat progress instead of the WHOLE encounter (the real
            # 2026-07-18 incident this fixes). start_session/end_session
            # also snapshot immediately on their own.
            if sessions._ACTIVE_SESSIONS:
                sessions.save_snapshot()
        except Exception as e:
            logger.error(f"[sessions] periodic snapshot failed this cycle: {e!r}")


class _StartupChatStub:
    """
    Minimal effective_chat stand-in used ONLY to resolve any AI/downed
    turn a restored combat session (task #159) might already be sitting
    on at startup, when there's no real incoming Update to hang a chat
    object off of. Sends go straight through application.bot, same
    underlying call a real Chat.send_message would make.
    """
    def __init__(self, bot, chat_id: int):
        self._bot = bot
        self.id = chat_id

    async def send_message(self, text, **kwargs):
        return await self._bot.send_message(chat_id=self.id, text=text, **kwargs)


class _StartupUpdateStub:
    """Bare Update stand-in for the same startup-only AI-turn-resolution case — see _StartupChatStub."""
    def __init__(self, bot, chat_id: int):
        self.effective_chat = _StartupChatStub(bot, chat_id)


async def _on_startup(application: Application) -> None:
    # Application.create_task ties this loop's lifecycle to the
    # application, so it's cancelled cleanly on shutdown.
    application.create_task(_idle_inactivity_loop(application))

    # Task #159 (real live incident, 2026-07-18): restore any combat
    # session(s) that were active when the bot last stopped, instead of
    # silently wiping them the way the old fully-in-memory design always
    # did. See sessions.py's save_snapshot/load_snapshot for the actual
    # persistence; this just tells anyone still mid-fight that their
    # combat survived the restart, since Coffee was rightly upset last
    # time this happened silently.
    restored = sessions.load_snapshot()
    if restored:
        logger.info(f"[sessions] restored {restored} active combat session(s) from snapshot")
        for chat_id, session in list(sessions._ACTIVE_SESSIONS.items()):
            try:
                current_name = session.current_participant()["name"] if session.turn_order else "?"
                await application.bot.send_message(
                    chat_id,
                    f"🔄 The bot just restarted, but this fight wasn't lost — "
                    f"combat resumes right where it left off. It's **{current_name}**'s turn.",
                    message_thread_id=config.TOPIC_ADVENTURE_ID,
                    parse_mode="Markdown",
                )
                # Real live incident (2026-07-19, Coffee: "the battle seemed
                # to stop"): a restore landing on an AI-controlled (or
                # downed-real-player death-save) turn used to just sit
                # there forever -- _resolve_ai_turns has ALWAYS only ever
                # run as a side-effect of a human/AI action that just
                # finished (_do_attack/_do_cast_spell/etc. each call it
                # right after advancing the turn), and a startup restore
                # is neither of those, so nothing ever kicked the fight
                # forward again. Confirmed by inspection that
                # _resolve_ai_turns only ever touches update.effective_chat
                # (via _safe_send/_notify_main_topic), never anything else
                # on Update, so a minimal synthetic stand-in built straight
                # from application.bot is enough here -- there's no real
                # incoming Update to hang one off at startup. Safe to call
                # unconditionally even when it's already a real player's
                # turn: _resolve_ai_turns just re-sends that turn's
                # announcement + battle menu and returns immediately.
                async with sessions.get_lock(chat_id):
                    live_session = sessions.get_session(chat_id)
                    if live_session is not None:
                        # Real live incident (2026-07-19, Coffee: "no
                        # enemys left standing... it stopped"): the exact
                        # deadlock _try_end_stale_combat exists to fix
                        # (see its docstring) doesn't just sit there
                        # waiting for a NEW attack to trigger the
                        # cleanup -- if the last attack before a restart
                        # was the one that got interrupted, the restored
                        # snapshot is ALREADY in the stuck state, and
                        # nothing would ever prompt a real player to try
                        # attacking again once they've given up on a
                        # fight that looks frozen. Checked here too, same
                        # startup stub as _resolve_ai_turns just below,
                        # so a restart alone is enough to un-stick it --
                        # confirmed live this exact restore left Wolf 1
                        # and Wolf 2 both at 0 HP with no resolution.
                        stub_update = _StartupUpdateStub(application.bot, chat_id)
                        if not await _try_end_stale_combat(stub_update, live_session):
                            await _resolve_ai_turns(stub_update, live_session)
            except Exception as e:
                logger.error(f"[sessions] failed to announce restored session for chat {chat_id}: {e!r}")


def build_application() -> Application:
    # concurrent_updates=True is important: without it, python-telegram-bot
    # processes updates ONE AT A TIME. If any single handler ever stalls
    # (e.g. a slow/hung network call), the ENTIRE bot goes silent across
    # every topic until it resolves — exactly the kind of total, unrelated
    # freeze that's hard to diagnose. With this enabled, a stuck handler
    # only affects the update that triggered it.
    # user_data (currently just in-progress character creation, see
    # _begin_character_creation/_continue_character_creation) is
    # otherwise pure in-memory state — a live-reported bug on 2026-07-11
    # traced a player's character creation silently losing its place mid-
    # flow (their next messages, ability score rolls, fell through to
    # generic 'chat' with no creation step to route them into) to an
    # ordinary redeploy landing between two of their messages. Restarts
    # are routine here (see CLAUDE.md's Deployment section), so this
    # needs to survive them. Combat sessions used to be the other half of
    # this same class of bug (a real 2026-07-18 restart-mid-combat
    # incident wiped an active fight outright) -- task #159 fixed that
    # via sessions.py's own separate JSON snapshot/restore (see
    # save_snapshot/load_snapshot there and _on_startup above), since
    # sessions live in a plain module dict, not in this
    # PicklePersistence-backed user_data/bot_data at all.
    persistence = PicklePersistence(filepath="bot_persistence.pickle")
    application = (
        ApplicationBuilder()
        .token(config.BOT_TOKEN)
        .concurrent_updates(True)
        .persistence(persistence)
        .post_init(_on_startup)
        .build()
    )

    # Optional slash-command shortcuts.
    # Alphabetical by command name (task #122, per Coffee: "the /
    # commands are an incoherent mess") -- purely a registration-order
    # cleanup, doesn't change behavior at all.
    application.add_handler(CommandHandler("achievements", achievements_command))
    application.add_handler(CommandHandler("add_admin", add_admin_command))
    application.add_handler(CommandHandler("attack", attack_command))
    application.add_handler(CommandHandler("ban", ban_command))
    application.add_handler(CommandHandler("ban_list", banlist_command))
    application.add_handler(CommandHandler("buy", buy_command))
    application.add_handler(CommandHandler("campaigns", campaigns_command))
    application.add_handler(CommandHandler("cast", cast_command))
    application.add_handler(CommandHandler("changelog", changelog_command))
    application.add_handler(CommandHandler("donotdisturb", donotdisturb_command))
    application.add_handler(CommandHandler("endturn", endturn_command))
    application.add_handler(CommandHandler("guild", guild_command))
    application.add_handler(CommandHandler("help", help_command))
    application.add_handler(CommandHandler("hint", hint_command))
    application.add_handler(CommandHandler("inventory", inventory_command))
    application.add_handler(CommandHandler("leaderboard", leaderboard_command))
    application.add_handler(CommandHandler("map", map_command))
    application.add_handler(CommandHandler("visual_map", visual_map_command))
    application.add_handler(CommandHandler("menu", menu_command))
    application.add_handler(CommandHandler("newcharacter", newcharacter_command))
    application.add_handler(CommandHandler("note", note_command))
    application.add_handler(CommandHandler("party", party_command))
    application.add_handler(CommandHandler("quests", quests_command))
    application.add_handler(CommandHandler("redo", redo_command))
    application.add_handler(CommandHandler("remove_admin", remove_admin_command))
    application.add_handler(CommandHandler("report", report_command))
    application.add_handler(CommandHandler("rest", rest_command))
    application.add_handler(CommandHandler("sell", sell_command))
    application.add_handler(CommandHandler("sheet", sheet_command))
    application.add_handler(CommandHandler("replay_intro", replay_intro_command))
    application.add_handler(CommandHandler("dice_game", dice_game_command))
    application.add_handler(CommandHandler("fortune", fortune_command))
    application.add_handler(CommandHandler("alignment", alignment_command))
    application.add_handler(CommandHandler("msg", msg_command))
    application.add_handler(CommandHandler("skilltree", skilltree_command))
    application.add_handler(CommandHandler("duel", duel_command))
    application.add_handler(CommandHandler("accept_duel", accept_duel_command))
    application.add_handler(CommandHandler("sell_market", sell_market_command))
    application.add_handler(CommandHandler("market", market_command))
    application.add_handler(CommandHandler("buy_market", buy_market_command))
    application.add_handler(CommandHandler("join_battle", join_battle_command))
    application.add_handler(CommandHandler("shop", shop_command))
    application.add_handler(CommandHandler("startcombat", startcombat_command))
    application.add_handler(CommandHandler("title", title_command))
    application.add_handler(CommandHandler("unban", unban_command))
    application.add_handler(CommandHandler("version", version_command))
    application.add_handler(CommandHandler("warning", warning_command))
    application.add_handler(CommandHandler("warning_list", warninglist_command))
    application.add_handler(CommandHandler("weather", weather_command))

    # Single unified router for all plain text messages, across topics.
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, text_message_router))
    application.add_handler(MessageHandler(filters.VOICE, voice_message_handler))
    application.add_handler(MessageHandler(filters.PHOTO, dev_topic_photo_handler))
    application.add_handler(MessageHandler(filters.Document.ALL, dev_topic_document_handler))
    application.add_handler(MessageHandler(filters.VIDEO, dev_topic_video_handler))
    application.add_handler(CallbackQueryHandler(battle_menu_callback, pattern=r"^bm\|"))
    # Task #176: out-of-combat browsing buttons (shop/spells/quest board),
    # own callback-data namespaces so none of these can ever collide with
    # the combat battle menu above or each other.
    application.add_handler(CallbackQueryHandler(shop_menu_callback, pattern=r"^shop\|"))
    application.add_handler(CallbackQueryHandler(market_menu_callback, pattern=r"^market\|"))
    application.add_handler(CallbackQueryHandler(spell_menu_callback, pattern=r"^spell\|"))
    application.add_handler(CallbackQueryHandler(quest_menu_callback, pattern=r"^quest\|"))
    application.add_handler(CallbackQueryHandler(item_menu_callback, pattern=r"^item\|"))
    application.add_handler(CallbackQueryHandler(menu_callback, pattern=r"^menu\|"))
    application.add_handler(CallbackQueryHandler(equip_menu_callback, pattern=r"^equip\|"))
    application.add_handler(CallbackQueryHandler(level_menu_callback, pattern=r"^level\|"))
    application.add_handler(CallbackQueryHandler(roster_menu_callback, pattern=r"^roster\|"))
    application.add_handler(CallbackQueryHandler(waypoint_menu_callback, pattern=r"^waypoint\|"))
    application.add_handler(CallbackQueryHandler(skilltree_menu_callback, pattern=r"^skilltree\|"))
    application.add_handler(CallbackQueryHandler(story_menu_callback, pattern=r"^story\|"))
    application.add_handler(CallbackQueryHandler(party_menu_callback, pattern=r"^party\|"))
    application.add_handler(CallbackQueryHandler(travel_menu_callback, pattern=r"^travel\|"))
    application.add_handler(CallbackQueryHandler(look_action_menu_callback, pattern=r"^lookact\|"))
    application.add_handler(CallbackQueryHandler(creation_menu_callback, pattern=r"^create\|"))
    application.add_handler(CallbackQueryHandler(hybrid_menu_callback, pattern=r"^hybrid\|"))
    application.add_handler(CallbackQueryHandler(title_menu_callback, pattern=r"^title\|"))

    application.add_error_handler(_log_unhandled_error)

    return application


def main() -> None:
    db.init_db()
    setup_default_npcs()
    application = build_application()
    logger.info("BotApplication created successfully. Starting polling...")
    application.run_polling()


if __name__ == "__main__":
    main()
