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
import logging
import os
import random
import re
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

import requests

from telegram import Update
from telegram.error import TelegramError
from telegram.ext import (
    Application,
    ApplicationBuilder,
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
import items as items_module
import sessions
import shop as shop_module
import spells as spells_module
import races as races_module
import class_features as class_features_module
import moltbook
import topics
from ai.autonomous_player import choose_next_action
from ai.moltbook_agent import decide_social_action
from ai.dev_agent import answer_dev_question
from ai.dm_agent import (
    narrate_action, narrate_welcome, narrate_skill_check, narrate_hourly_update,
    narrate_examine, narrate_branching_choice_outcome,
)
from ai.intent_parser import parse_intents
from ai.npc_agent import register_npc, talk_to_npc, generate_ambient_line, _NPCS
from ai.support_agent import answer_support_question
from ai.text_cleanup import to_speakable_text
from guilds import GUILDS, eligible_for_guild
from models import (
    VALID_CLASSES,
    VALID_RACES,
    STARTING_EQUIPMENT,
    STARTING_GOLD,
    BASE_ARMOR_CLASS,
)
from rules.combat import resolve_attack, resolve_death_save
from rules.crafting import RECIPES, get_recipe, has_materials, resolve_craft
from rules.dice import roll, roll_damage, ability_modifier, roll_ability_check, roll_d20
from rules.item_generator import generate_item
from rules.leveling import CLASS_HIT_DICE, scaled_enemy_count
from rules.proficiency import practiced_bonus

logging.basicConfig(
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger("pandora_mmo")

ACTIVE_CAMPAIGN_ID = "default"
CAMPAIGN = cl.load_campaign(ACTIVE_CAMPAIGN_ID)

DEFAULT_WEAPON = {"ability": "strength", "damage_dice": "1d8", "damage_bonus": 0}

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
IDLE_WARNING_SECONDS = 900
IDLE_TIMEOUT_SECONDS = 1800
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

# ---------------------------------------------------------------------
# Living world — NPCs tagged "can_wander" in campaign.json (currently
# Sera and Theron) have a real, mutable current location, independent
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
_NPC_LOCATIONS: dict[str, str] = {}
NPC_WANDER_CHANCE_PER_CYCLE = 0.15
WORLD_HEARTBEAT_IDLE_THRESHOLD_SECONDS = 1200  # 20 real minutes with no player activity at all
WORLD_HEARTBEAT_MIN_GAP_SECONDS = 900  # never more than once per ~15 real minutes
_LAST_WORLD_HEARTBEAT_AT: datetime | None = None

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


def _wanderable_npc_ids() -> list[str]:
    return [npc_id for npc_id, data in CAMPAIGN["npcs"].items() if data.get("can_wander")]


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
        if not CAMPAIGN["npcs"].get(n, {}).get("can_wander")
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


def _find_party_target_by_name(text: str) -> dict | None:
    """
    Finds a party member (real player, AI companion, or a currently
    inactive/resting real player — all still count as "in the party")
    named in free text, for support-spell targeting. Returns None if no
    party member's name appears, letting the caller default to self.
    """
    lowered = text.lower()
    for member in _get_party_members():
        if member["name"].lower() in lowered:
            return member
    return None


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


async def _continue_character_creation(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    creation = context.user_data["creation"]
    step = creation["step"]
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
            f"(e.g. '15 14 13 12 10 8').",
            message_thread_id=config.TOPIC_ADVENTURE_ID,
        )
        return

    if step == "assign_scores":
        try:
            assigned = [int(x) for x in text.split()]
        except ValueError:
            assigned = []

        rolled = creation["rolled_scores"]
        if len(assigned) != 6 or sorted(assigned) != sorted(rolled):
            await update.effective_chat.send_message(
                f"That doesn't match your rolled scores ({', '.join(map(str, rolled))}). "
                f"Please reply with all 6 values, each used exactly once, in "
                f"STR DEX CON INT WIS CHA order.",
                message_thread_id=config.TOPIC_ADVENTURE_ID,
            )
            return

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

        sheet = (
            f"✅ Character created!\n\n"
            f"**{character['name']}** — {character['race']} {character['char_class']}\n"
            f"Level {character['level']} | XP {character['xp']}\n"
            f"HP {character['hp_current']}/{character['hp_max']} | AC {character['armor_class']}\n"
            f"STR {character['strength']} DEX {character['dexterity']} "
            f"CON {character['constitution']} INT {character['intelligence']} "
            f"WIS {character['wisdom']} CHA {character['charisma']}\n"
            f"Gold: {character['gold']}\n"
            f"{spell_line}"
            f"{traits_line}"
            f"{features_line}"
            f"Inventory: {', '.join(items_module.get_item(i)['name'] for i in character['inventory'])}"
        )
        await update.effective_chat.send_message(sheet, message_thread_id=config.TOPIC_ADVENTURE_ID)
        del context.user_data["creation"]

        await _send_welcome_narration(update, character)


# ---------------------------------------------------------------------
# Combat helpers (shared by both natural-language flow and /commands)
# ---------------------------------------------------------------------

def _format_combat_result(flavor_text: str, result: dict, actor_label: str, defender_label: str) -> str:
    """
    Builds the visually structured combat message: a banner (critical hit /
    success / miss / fumble), the AI's short flavor line as a quote, then a
    deterministic resolution block with the REAL numbers from the rules
    engine — including the actual raw d20 roll — never left to the AI to
    state or invent.
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
    if result.get("hit", True):
        lines.append(f"- 🗡️ **{actor_label}** attacks **{defender_label}** → **Hits for {dmg} damage!**")
    else:
        lines.append(f"- 🗡️ **{actor_label}** attacks **{defender_label}** → **Misses!**")

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
    """Always explicitly states whose turn it is and what round it is."""
    current = session.current_participant()
    label = f"{current['name']} (AI)" if current.get("is_ai") else current["name"]
    return f"🎲 **Round {session.round_number}** — It's now **{label}**'s turn! What do you do?"


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


async def _safe_send(update: Update, text: str, thread_id: int | None = None) -> None:
    """
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
    """
    for attempt in range(2):
        try:
            await update.effective_chat.send_message(
                text, message_thread_id=thread_id if thread_id is not None else config.TOPIC_ADVENTURE_ID
            )
            if not isinstance(update.effective_chat, _BufferingChatProxy):
                await _maybe_speak(update, text, thread_id)
            return
        except TelegramError as e:
            if attempt == 0:
                logger.warning(f"[message] send failed, retrying once: {e!r}")
                await asyncio.sleep(2)
            else:
                logger.warning(f"[message] send failed again, giving up: {e!r}")


async def _maybe_speak(update: Update, text: str, thread_id: int | None) -> None:
    """
    Optional TTS narration via @TextTSBot, already added to this group
    (2026-07-14) -- confirmed live it responds to another bot's own
    /tts command, not just a real player's, and Coffee picked a voice
    (Alan, Australian) he likes for the group. Off by default, toggled
    via db.get_setting("tts_enabled") (see the Development-topic
    "turn on/off tts" command). Deliberately one shared voice, not
    distinct per-NPC voices -- @TextTSBot's voice is one account-wide
    setting, not something safe to switch per-message -- so this is
    the "read narration aloud" slice Coffee asked to try as an on/off
    option, not the full per-character-voice item still on the
    backlog. Skipped during a compound-message's buffered sub-sends
    (see adventure_master_handler) so a multi-action message triggers
    exactly one /tts call on the final combined text, not one per
    sub-action.
    """
    if db.get_setting("tts_enabled") != "1":
        return
    speakable = to_speakable_text(text)
    if not speakable:
        return
    try:
        trigger_message = await update.effective_chat.send_message(
            f"/tts {speakable}",
            message_thread_id=thread_id if thread_id is not None else config.TOPIC_ADVENTURE_ID,
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
        await asyncio.sleep(2)
        try:
            await trigger_message.delete()
        except TelegramError as e:
            logger.warning(f"[tts] couldn't delete the trigger message afterward: {e!r}")

    asyncio.create_task(_delete_trigger_after_delay())


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


async def _post_narrated(update: Update, character: dict, action_text: str,
                          mechanical_result: dict, session: sessions.Session) -> None:
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
    )
    session.log_event(f"{mechanical_result.get('attacker')} vs {mechanical_result.get('defender')}: {flavor}")
    await _safe_send(update, message)


def _sync_player_to_db(character: dict) -> None:
    """
    Persists a real player's combat-mutated state (HP, death saves) back
    to the database. Combat mutates participant dicts in-memory only —
    without this, damage taken mid-fight would silently vanish the
    moment combat ends or another /sheet lookup re-reads the database.
    AI companions/monsters have no database row to sync (negative
    synthetic IDs), so this is a no-op for them.
    """
    if character.get("is_ai"):
        return
    db.update_character(
        character["telegram_user_id"],
        hp_current=character["hp_current"],
        death_save_successes=character.get("death_save_successes", 0),
        death_save_failures=character.get("death_save_failures", 0),
    )


def _award_victory_xp(session: sessions.Session) -> str:
    """
    Awards real XP (from the defeated monster's real 5E-sourced XP value)
    to every real (non-AI) party member still in the fight, split evenly
    per standard 5E group-XP conventions. Returns a summary string to
    append to the victory message, or an empty string if nothing to award.
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

    for p in session.participants:
        if session.sides.get(p["telegram_user_id"]) == "enemy" and p.get("source_npc_id"):
            npc_id = p["source_npc_id"]
            _DEFEATED_NPCS.add(npc_id)
            faction_id = _faction_for_npc(npc_id)
            if faction_id:
                for pid in real_party_ids_all:
                    db.adjust_faction_standing(pid, faction_id, -15, _faction_starting_standing(faction_id))

    enemy_xp_total = sum(
        p.get("xp_reward", 0) for p in session.participants
        if session.sides.get(p["telegram_user_id"]) == "enemy"
    )
    if enemy_xp_total <= 0:
        return ""

    real_party_ids = real_party_ids_all
    if not real_party_ids:
        return ""

    xp_each = max(enemy_xp_total // len(real_party_ids), 1)
    level_up_notes = []
    event_location = None
    for pid in real_party_ids:
        before = db.get_character(pid)
        after = db.add_xp(pid, xp_each)
        event_location = event_location or after.get("current_location")
        if after["level"] > before["level"]:
            level_up_notes.append(
                f"🎉 {after['name']} leveled up to {after['level']}! "
                f"(HP max: {before['hp_max']} → {after['hp_max']})"
            )
            _log_world_event(event_location, f"{after['name']} reached level {after['level']}.")

    enemy_names = [
        p["name"] for p in session.participants
        if session.sides.get(p["telegram_user_id"]) == "enemy"
    ]
    if enemy_names:
        _log_world_event(event_location, f"The party defeated {', '.join(enemy_names)}.")

    board_notes = []
    if event_location:
        for board_quest in board_quests_module.get_todays_board_quests(event_location):
            if not (board_quest.get("accepted_by") and not board_quest.get("completed_at")
                    and board_quest["objective_type"] == "defeat_monster"):
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
                        db.add_xp(pid, updated["reward_xp"])
                        character = db.get_character(pid)
                        db.update_character(pid, gold=character["gold"] + updated["reward_gold"])
                    board_notes.append(
                        f"\n📜 **Board quest complete: {updated['title']}!** "
                        f"Party earns {updated['reward_xp']} XP, {updated['reward_gold']} gold each."
                    )
            else:
                board_notes.append(
                    f"\n📋 Board quest progress: {updated['title']} "
                    f"({updated['progress_count']}/{updated['objective_count']})"
                )

    # Bonus gear loot: this game has no equip system yet (combat damage
    # doesn't read from carried weapons/armor), so a generated item is
    # framed as sold in town rather than added to inventory -- honest
    # about what actually exists, rather than half-building an equip
    # mechanic just to give rules/item_generator.py a caller. Its price
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

    summary = f"\n✨ Party gains {xp_each} XP each ({enemy_xp_total} total)."
    summary += loot_line
    summary += "".join(board_notes)
    if level_up_notes:
        summary += "\n" + "\n".join(level_up_notes)
    return summary


def _determine_winner(session: sessions.Session) -> str:
    """
    Returns 'party' or 'enemy' based on which side still has any member
    remaining in turn_order — NOT based on living_on_side's hp_current
    check, which would incorrectly call it for the enemy the instant a
    downed-but-not-dead player's HP hits 0.
    """
    party_remains = any(session.sides.get(pid) == "party" for pid in session.turn_order)
    return "party" if party_remains else "enemy"


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
            await _safe_send(update, _turn_announcement(session))
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

        # Boss Multiattack (2026-07-15, boss-specific abilities finding
        # from the reverse-playthrough sweep): every is_boss monster
        # previously fought exactly like a regular monster, just with a
        # bigger stat block -- real 5E boss-tier stat blocks almost
        # universally get Multiattack (2 attacks/turn). Loops the same
        # single-attack resolution below either once or twice; breaks
        # early if a mid-turn kill leaves no target for the 2nd attack,
        # or if combat itself ends mid-multiattack.
        attack_count = 2 if current.get("is_boss") else 1
        combat_ended_mid_turn = False
        for attack_num in range(attack_count):
            opposing = session.living_on_side(session.opposing_side(current["telegram_user_id"]))
            if not opposing:
                break
            target = min(opposing, key=lambda p: p["hp_current"])
            adv, disadv = _attack_advantage_disadvantage(current, target)
            result = resolve_attack(
                current, target, DEFAULT_WEAPON, advantage=adv, disadvantage=disadv,
                defender_relentless_endurance_available=_relentless_endurance_available(target),
            )
            if result["relentless_endurance_triggered"]:
                db.use_feature(target["telegram_user_id"], "relentless_endurance")
            _sync_player_to_db(target)

            applied_condition = None
            if result["hit"] and current.get("on_hit_condition"):
                condition = current["on_hit_condition"]
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
        xp_summary = _award_victory_xp(session) if winner == "party" else ""
        if winner == "party":
            await _check_quest_completions_defeat_monster(update, session)
        await _safe_send(update, f"🏆 **Combat over!** The {winner} side is victorious!{xp_summary}")
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
        plural_hint = key.replace("_", " ") + "s"
        if plural_hint in lowered_text:
            return 2
    return None


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
        await _safe_send(update, header)
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
            await _safe_send(update, header)
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


async def _do_attack(update: Update, action_text: str) -> None:
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
            (m for m in local_monsters if m.replace("_", " ") in lowered), None
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
            await update.effective_chat.send_message(
                "No valid targets remain.", message_thread_id=config.TOPIC_ADVENTURE_ID
            )
            return
        target = _pick_target(action_text, opposing)

        adv, disadv = _attack_advantage_disadvantage(attacker, target)
        result = resolve_attack(
            attacker, target, DEFAULT_WEAPON, advantage=adv, disadvantage=disadv,
            defender_relentless_endurance_available=_relentless_endurance_available(target),
        )
        if result["relentless_endurance_triggered"]:
            db.use_feature(target["telegram_user_id"], "relentless_endurance")
        _sync_player_to_db(target)
        await _post_narrated(update, attacker, action_text, result, session)

        removed = session.remove_defeated()
        await _announce_defeats(update, session, removed)

        if session.is_combat_over():
            winner = _determine_winner(session)
            xp_summary = _award_victory_xp(session) if winner == "party" else ""
            if winner == "party":
                await _check_quest_completions_defeat_monster(update, session)
            await _safe_send(update, f"🏆 **Combat over!** The {winner} side is victorious!{xp_summary}")
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
        await _safe_send(update, _turn_announcement(session))
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
    db.create_ai_companion(
        name=npc["name"], race=stats["race"], char_class=stats["char_class"],
        ability_scores=ability_scores, hp_max=stats["hp_max"],
        armor_class=stats["armor_class"], gold=stats["gold"],
        inventory=dict(stats["inventory"]),
    )

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


async def _do_lockpick(update: Update, character: dict, lockable: dict, action_text: str) -> None:
    """
    Real DC-13 DEX check to pick a chest/door lock — deterministic outcome
    computed here (rules layer), the AI only narrates it. Success on a
    chest grants its real loot/gold; success on a door permanently opens
    that shortcut connection (see _do_move's locked_connections check).
    """
    if lockable["id"] in _UNLOCKED:
        await update.effective_chat.send_message(
            f"{lockable['name'].capitalize()} is already unlocked.",
            message_thread_id=config.TOPIC_ADVENTURE_ID,
        )
        return

    result = roll_ability_check(character, "dexterity", proficient=False)
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


async def _do_skill_check(update: Update, ability: str, action_text: str) -> None:
    character = db.get_character(update.effective_user.id)
    if character is None:
        await update.effective_chat.send_message(
            "You don't have a character yet!", message_thread_id=config.TOPIC_ADVENTURE_ID
        )
        return

    location = cl.get_location(CAMPAIGN, character["current_location"])
    lockable = _find_lockable(location, action_text) if location else None
    if lockable is not None:
        await _do_lockpick(update, character, lockable, action_text)
        return

    result = roll_ability_check(character, ability, proficient=False)
    bonus = _practiced_bonus_for(update.effective_user.id, ability)
    result["total"] += bonus
    result["practiced_bonus"] = bonus
    success = result["total"] >= SKILL_CHECK_DC
    if success:
        db.record_skill_use(update.effective_user.id, ability)

    flavor = await asyncio.to_thread(
        narrate_skill_check, character, action_text, ability,
        {**result, "ability": ability, "dc": SKILL_CHECK_DC, "success": success},
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
    """
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
    disadvantage = (
        "prone" in attacker_conditions or "poisoned" in attacker_conditions
        or "blinded" in attacker_conditions or "frightened" in attacker_conditions
    )
    favored_enemy = (attacker.get("char_class") == "Ranger"
                      and defender.get("monster_key", "").startswith("goblin"))
    advantage = (
        "prone" in defender_conditions or "blinded" in defender_conditions
        or "paralyzed" in defender_conditions or favored_enemy
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


async def _do_shove(update: Update, action_text: str) -> None:
    """
    A contested STR (Athletics) check to knock an enemy prone — a real
    5E combat action, using your turn's action, per the rules (not a
    free action). Success applies the 'prone' condition to the target.
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
            await update.effective_chat.send_message(
                "No valid targets remain.", message_thread_id=config.TOPIC_ADVENTURE_ID
            )
            return
        target = _pick_target(action_text, opposing)

        attacker_result = roll_ability_check(attacker, "strength", proficient=True)
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


async def _do_flee(update: Update, action_text: str) -> None:
    """
    A real dice roll to escape an active fight — "cancel" no longer
    force-ends combat for ordinary players (see adventure_master_handler's
    UNIVERSAL_ESCAPE_PHRASES handling), so this is the actual way out.
    Impossible against any enemy flagged is_boss in campaign.json —
    those fights don't let you walk away. Uses the same fixed
    SKILL_CHECK_DC as every other check in this game, on purpose, so
    difficulty is never something the AI gets to invent.
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

        fleeing = session.current_participant()
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

        result = roll_ability_check(fleeing, "dexterity", proficient=False)
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
        opportunity_blocks = []
        for enemy in session.living_on_side(session.opposing_side(user_id)):
            if fleeing["hp_current"] <= 0:
                break
            atk_result = resolve_attack(enemy, fleeing, DEFAULT_WEAPON)
            opportunity_blocks.append(_format_combat_result(
                "", atk_result, enemy["name"], fleeing["name"],
            ))
        if opportunity_blocks:
            _sync_player_to_db(fleeing)
            message = message + "\n\n🗡️ **Opportunity attacks as you break away:**\n\n" + "\n\n".join(opportunity_blocks)

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
    on with players" -- e.g. Sera mentioning a task she'd like help
    with once recruited). Matched by 'giver_npc' against whoever's
    ACTUALLY in the party right now, not by location -- a companion's
    own request travels with the party rather than being tied to
    wherever they happened to be recruited, and this deliberately
    never collides with the ordinary location-based quests above
    (this game's only other quest with a 'location' field matching
    that spot might already be a different quest entirely).
    """
    party_npc_ids = {
        _find_npc_id_by_name(p["name"]) for p in _get_party_members() if p.get("is_ai")
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
    if reward_xp:
        db.add_xp(telegram_user_id, reward_xp)
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
    await _safe_send(
        update_like,
        f"📜 **Quest complete: {quest['title']}!**\nYou've earned: {reward_text}.{chapter_note}",
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


async def _check_board_quest_turnin(update_like, telegram_user_id: int, location_id: str) -> None:
    """
    Grants a completed-but-not-yet-collected board quest's reward when
    the player arrives back at the location where it's posted -- the
    counterpart to _do_gather deferring the reward instead of granting
    it the instant the objective is met (see that function's comment).
    Deliberately skips branch_data quests: those are resolved through
    the separate _do_resolve_quest_choice flow, not a location arrival.
    """
    for board_quest in board_quests_module.get_todays_board_quests(location_id):
        if not (board_quest.get("accepted_by") == telegram_user_id
                and not board_quest.get("completed_at")
                and not board_quest.get("branch_data")
                and board_quest["progress_count"] >= board_quest["objective_count"]):
            continue
        db.complete_board_quest(board_quest["board_quest_id"])
        db.add_xp(telegram_user_id, board_quest["reward_xp"])
        character = db.get_character(telegram_user_id)
        db.update_character(telegram_user_id, gold=character["gold"] + board_quest["reward_gold"])
        await _safe_send(
            update_like,
            f"📜 **Board quest complete: {board_quest['title']}!** "
            f"You earn {board_quest['reward_xp']} XP, {board_quest['reward_gold']} gold.",
        )


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
            db.accept_quest(telegram_user_id, quest_id)
            await _safe_send(update, f"📜 **{character['name']}** accepts Quest: {quest['title']}\n{quest['description']}")
            return

    offer = _offerable_quest_at_location(character, location_id)
    if offer is not None:
        quest_id, quest = offer
        db.accept_quest(telegram_user_id, quest_id)
        await _safe_send(update, f"📜 **{character['name']}** accepts Quest: {quest['title']}\n{quest['description']}")
        return

    # No story quest on offer here — try the area's board quest(s) instead.
    all_quests = board_quests_module.get_or_generate_board_quests(CAMPAIGN, location_id)
    available = [q for q in all_quests if not q.get("accepted_by") and not q.get("completed_at")]
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
        return
    await _safe_send(
        update,
        f"📜 **{character['name']}** accepts Quest: {board_quest['title']}\n{board_quest['description']}\n"
        f"Reward: {board_quest['reward_xp']} XP, {board_quest['reward_gold']} gold. "
        f"Expires in 24h if not finished.",
    )
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
    db.add_xp(telegram_user_id, chosen["reward_xp"])
    fresh = db.get_character(telegram_user_id)
    db.update_character(telegram_user_id, gold=fresh["gold"] + chosen["reward_gold"])
    if chosen.get("faction_id") and chosen.get("faction_delta"):
        db.adjust_faction_standing(telegram_user_id, chosen["faction_id"], chosen["faction_delta"])

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
                progress = f"{bq['progress_count']}/{bq['objective_count']}"
                lines.append(f"• {bq['title']} ({bq_location_name}) — {progress}")

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
        lines.append("(Say \"I accept this quest\" — name it if more than one's posted.)")

    await _safe_send(update, "\n".join(lines))


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
            )
            return
        members = db.get_party_members_by_id(party_id)
        sheets = "\n\n".join(_format_character_sheet(m) for m in members)
        await _safe_send(update, f"🎗️ **Your party's sheets ({len(members)}/{db.PARTY_MAX_MEMBERS}):**\n\n{sheets}")
        return

    lines = [f"👥 Everyone currently active: {_party_summary_text()}"]

    if character:
        party_id = character.get("party_id")
        if party_id:
            members = db.get_party_members_by_id(party_id)
            names = [f"{m['name']} (AI)" if m.get("is_ai") else m["name"] for m in members]
            lines.append(
                f"\n🎗️ Your formed party ({len(members)}/{db.PARTY_MAX_MEMBERS}): {', '.join(names)}"
            )
        elif character.get("pending_party_invite"):
            lines.append("\n🎗️ You have a pending party invite — say \"I accept the party invite\" to join.")
        else:
            lines.append("\n🎗️ You're not in a formed party. Say \"invite [name] to my party\" to start one.")

    await _safe_send(update, "\n".join(lines))


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
    return None


def _find_campaign_npc_by_name(name: str) -> dict | None:
    """
    Looks up a campaign.json NPC (Grimsby, Old Maren, etc.) by display
    name, case-insensitive. Only for NPCs who were never recruited into
    a real character row -- once recruited (like Sera), they show up
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
    name_line = f"**{character['name']}**" + (" *(AI companion)*" if character.get("is_ai") else "")
    return (
        f"{name_line} — {character['race']} {character['char_class']}\n"
        f"Level {character['level']} | XP {character['xp']}\n"
        f"HP {character['hp_current']}/{character['hp_max']} | AC {character['armor_class']}\n"
        f"Gold: {character['gold']} | Guild: {character.get('guild') or 'None'}\n"
        f"Spells known: {', '.join(spell_names) if spell_names else 'None'}\n"
        f"{slot_line}"
        f"Racial traits: {'; '.join(race_data['traits']) if race_data else 'None'}\n"
        f"Class features: {'; '.join(features) if features else 'None'}\n"
        f"{feature_use_line}"
        f"{skills_line}"
        f"Location: {cl.get_location(CAMPAIGN, character['current_location'])['name']}"
    )


async def _do_check_sheet(update: Update, target_name: str | None = None) -> None:
    # Per Coffee's live report (2026-07-14): "Show me SERA's character
    # sheet" had nowhere to go -- check_sheet only ever showed the
    # asker's OWN sheet, so it fell through to "examine" and searched
    # for an interactable object named "Sera" instead. A named target
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
            await _safe_send(update, _format_character_sheet(target))
            return
        npc = _find_campaign_npc_by_name(target_name)
        if npc is not None:
            await _safe_send(update, _format_npc_basic_info(npc))
            return
        await update.effective_chat.send_message(
            f"Nobody named {target_name.title()} is playing right now — can't show a sheet for them.",
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
    await _safe_send(update, _format_character_sheet(character))


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
    await _safe_send(update, "🎒 Your backpack:\n" + "\n".join(lines))


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


async def _do_gather(update: Update, action_text: str) -> None:
    """
    Gathering a raw material from a location's resource node — a real
    ability check (rules layer) decides success, matching every other
    outcome in this game; the AI only narrates it.
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
    result = roll_ability_check(character, node["ability"], proficient=False)
    bonus = _practiced_bonus_for(update.effective_user.id, skill_key)
    result["total"] += bonus
    result["practiced_bonus"] = bonus
    success = result["total"] >= SKILL_CHECK_DC
    material = items_module.get_item(node["material"])

    if success:
        db.record_skill_use(update.effective_user.id, skill_key)
        db.add_item(update.effective_user.id, node["material"], 1)

    flavor = await asyncio.to_thread(
        narrate_skill_check, character, action_text, node["ability"],
        {**result, "ability": node["ability"], "dc": SKILL_CHECK_DC, "success": success},
    )
    message = _format_skill_check_result(flavor, result, node["ability"], SKILL_CHECK_DC, success)
    if success:
        message += f"\n🌿 **{character['name']}** gathers **1x {material['name']}**."

        for board_quest in board_quests_module.get_todays_board_quests(character["current_location"]):
            if not (board_quest.get("accepted_by") and not board_quest.get("completed_at")
                    and board_quest["objective_type"] == "gather_material"
                    and board_quest["objective_target"] == node["material"]):
                continue
            updated = db.record_board_quest_progress(board_quest["board_quest_id"], 1)
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

    healed = roll_damage("1d10", modifier=character["level"])["total"]
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
    if db.get_feature_uses(update.effective_user.id, "rage") >= RAGE_MAX_USES:
        await update.effective_chat.send_message(
            f"You've already raged {RAGE_MAX_USES} times since your last rest.",
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
    boost = roll(1, 6)[0]
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

    pool = 5 * character["level"]
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
            "You seem to be nowhere in particular. That's... concerning.",
            message_thread_id=config.TOPIC_ADVENTURE_ID,
        )
        return

    db.mark_visited(update.effective_user.id, character["current_location"])

    lines = [f"📍 **{location['name']}** ({location['layer']})", location["description"]]
    npcs_here = _npcs_at_location(character["current_location"])
    if npcs_here:
        npc_names = [cl.get_npc(CAMPAIGN, n)["name"] for n in npcs_here if cl.get_npc(CAMPAIGN, n)]
        lines.append(f"People here: {', '.join(npc_names)}")
    monsters_here = location.get("monsters", [])
    if monsters_here:
        lines.append(f"You sense danger here: {', '.join(monsters_here)}")
    connections = location.get("connections", [])
    if connections:
        conn_names = [cl.get_location(CAMPAIGN, c)["name"] for c in connections]
        lines.append(f"You can travel to: {', '.join(conn_names)}")
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

    # Confirmed live 2026-07-14 (Coffee): TTS was silently skipped for
    # "look around" -- this reply went straight to
    # update.effective_chat.send_message, bypassing _safe_send
    # entirely, and _maybe_speak (TTS) is only ever hooked in there.
    # This is one of many direct-send call sites in this file (~195 of
    # them vs 64 through _safe_send) -- most are short refusal/error
    # messages that were never meant to be narrated aloud, but "look"
    # is real player-facing narration and should have gone through
    # _safe_send like every other primary action reply already does.
    await _safe_send(update, "\n".join(lines))


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
    lowered = text.strip().lower()
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
    stopwords = {"a", "an", "the", "of", "at", "on", "in", "to"}
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
    best_obj, best_score, ambiguous = None, 0, False
    for obj_id, data in ordered:
        words = [w for w in data["name"].lower().split() if w not in stopwords and len(w) >= 3]
        if not words:
            continue
        matches = sum(1 for w in words if re.search(r"\b" + re.escape(w) + r"\b", lowered))
        if matches > len(words) / 2:
            if matches > best_score:
                best_obj, best_score, ambiguous = (obj_id, data), matches, False
            elif matches == best_score:
                ambiguous = True
    if best_obj and not ambiguous:
        return best_obj

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
            "You seem to be nowhere in particular. That's... concerning.",
            message_thread_id=config.TOPIC_ADVENTURE_ID,
        )
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
    """
    character = db.get_character(update.effective_user.id)
    if character is None:
        await update.effective_chat.send_message(
            "You don't have a character yet!", message_thread_id=config.TOPIC_ADVENTURE_ID
        )
        return

    visited = set(character["visited_locations"])
    if not visited:
        await update.effective_chat.send_message(
            "You haven't explored anywhere yet.", message_thread_id=config.TOPIC_ADVENTURE_ID
        )
        return

    lines = ["🗺️ **Your map**"]
    for layer, layer_locations in CAMPAIGN["locations"].items():
        visited_here = [loc_id for loc_id in layer_locations if loc_id in visited]
        if not visited_here:
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

    await _safe_send(update, "\n".join(lines))


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
    for loc_id in reachable:
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
            f"You can't get there directly from {current['name']}. "
            f"From here you can reach: {reachable_names}",
            message_thread_id=config.TOPIC_ADVENTURE_ID,
        )
        return

    destination = cl.get_location(CAMPAIGN, destination_id)
    if destination.get("requires_item") and destination["requires_item"] not in character["inventory"]:
        await update.effective_chat.send_message(
            "Something stops you from going any further — you're missing something you'd need first.",
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

    db.move_character(update.effective_user.id, destination_id)
    db.mark_visited(update.effective_user.id, destination_id)
    # Per Coffee (2026-07-14): TTS coverage audit -- _do_move's primary
    # reply never went through _safe_send, so this (one of the most
    # common actions in the game) always silently skipped TTS.
    await _safe_send(update, f"🚶 **{character['name']}** travels to **{destination['name']}**.\n{destination['description']}")

    updated_character = db.get_character(update.effective_user.id)
    await _maybe_trigger_npc_encounter(update, updated_character, destination)
    await _check_quest_completions_reach_location(update, update.effective_user.id, destination_id)
    await _check_board_quest_turnin(update, update.effective_user.id, destination_id)


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

    if sessions.get_session(update.effective_chat.id) is not None:
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
    if not _meets_location_level(character, destination):
        await _send_level_gate_message(update, destination)
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
    lowered = text.lower()
    recipient = next((p for p in candidates if p["name"].lower() in lowered), None)
    if recipient is None:
        await update.effective_chat.send_message(
            "Give it to whom? Name someone real who's actually here with you.",
            message_thread_id=config.TOPIC_ADVENTURE_ID,
        )
        return

    item_id = items_module.find_item_mentioned_in_text(text, candidate_ids=list(character["inventory"].keys()))
    if item_id is None:
        await update.effective_chat.send_message(
            f"Give {recipient['name']} what, exactly? Name something you're actually carrying.",
            message_thread_id=config.TOPIC_ADVENTURE_ID,
        )
        return

    quantity = _extract_quantity(text)
    removed, _ = db.remove_item(update.effective_user.id, item_id, quantity)
    if not removed:
        have = character["inventory"].get(item_id, 0)
        await update.effective_chat.send_message(
            f"You don't have {quantity}x {items_module.get_item(item_id)['name']} to give"
            f" — you only have {have}.",
            message_thread_id=config.TOPIC_ADVENTURE_ID,
        )
        return

    db.add_item(recipient["telegram_user_id"], item_id, quantity)
    item_name = items_module.get_item(item_id)["name"]
    await _safe_send(
        update,
        f"🤝 **{character['name']}** gives {quantity}x {item_name} to **{recipient['name']}**.",
    )


_QUANTITY_WORDS = {
    "one": 1, "couple": 2, "two": 2, "three": 3, "four": 4,
    "five": 5, "six": 6, "seven": 7, "eight": 8, "nine": 9, "ten": 10,
}


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

    item_id = items_module.find_item_mentioned_in_text(text, candidate_ids=shop_data["inventory"])
    if item_id is None:
        await update.effective_chat.send_message(
            "Not sure what item you mean — try naming it more directly.",
            message_thread_id=config.TOPIC_ADVENTURE_ID,
        )
        return

    quantity = _extract_quantity(text)
    ok, msg = shop_module.buy_item(update.effective_user.id, shop_data, item_id, quantity)
    await _safe_send(update, msg)


async def _do_sell(update: Update, text: str) -> None:
    character = db.get_character(update.effective_user.id)
    if character is None:
        await update.effective_chat.send_message(
            "You don't have a character yet!", message_thread_id=config.TOPIC_ADVENTURE_ID
        )
        return

    item_id = items_module.find_item_mentioned_in_text(text, candidate_ids=list(character["inventory"].keys()))
    if item_id is None or item_id not in character["inventory"]:
        await update.effective_chat.send_message(
            "You're not carrying anything by that name.", message_thread_id=config.TOPIC_ADVENTURE_ID
        )
        return

    quantity = _extract_quantity(text)
    ok, msg = shop_module.sell_item(update.effective_user.id, item_id, quantity)
    await _safe_send(update, msg)


async def _do_steal(update: Update, text: str) -> None:
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

    result = roll_ability_check(character, "dexterity", proficient=False)
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
                db.adjust_faction_standing(
                    telegram_user_id, faction_id, -15, _faction_starting_standing(faction_id)
                )

    flavor = await asyncio.to_thread(
        narrate_skill_check, character, text, "dexterity",
        {**result, "ability": "dexterity", "dc": STEAL_DC, "success": success},
    )
    message = _format_skill_check_result(flavor, result, "dexterity", STEAL_DC, success) + consequence_line
    await _safe_send(update, message)


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
            target["hp_current"] = max(target["hp_current"] - result["damage_dealt"], 0)
            _sync_player_to_db(target)
            full_result = {
                **result, "attacker": character["name"], "defender": target["name"],
                "hit": True, "defender_hp_remaining": target["hp_current"],
                "defender_hp_max": target.get("hp_max", target["hp_current"]),
            }
            await _post_narrated(update, character, text, full_result, session)

            removed = session.remove_defeated()
            await _announce_defeats(update, session, removed)

            if session.is_combat_over():
                winner = _determine_winner(session)
                xp_summary = _award_victory_xp(session) if winner == "party" else ""
                if winner == "party":
                    await _check_quest_completions_defeat_monster(update, session)
                await update.effective_chat.send_message(
                    f"🏆 **Combat over!** The {winner} side is victorious!{xp_summary}",
                    message_thread_id=config.TOPIC_ADVENTURE_ID,
                )
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

    lowered = text.lower()
    guild_id = None
    for gid, guild in GUILDS.items():
        if gid.replace("_", " ") in lowered or guild["name"].lower() in lowered:
            guild_id = gid
            break

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
    await _safe_send(update, f"🏛️ You've joined {GUILDS[guild_id]['name']}!{join_note}")


# ---------------------------------------------------------------------
# Character slots — a telegram user can own multiple characters; exactly
# one is "active" (db.get_character resolves to it) at a time.
# ---------------------------------------------------------------------

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
    lines = ["🎭 **Your characters:**"]
    for c in roster:
        marker = "📍 " if c["character_id"] == active_id else "   "
        lines.append(
            f"{marker}{c['name']} — Level {c['level']} {c['race']} {c['char_class']}"
        )
    lines.append("\nSay 'switch to <name>' to change your active character.")
    await _safe_send(update, "\n".join(lines))


def _find_own_character_by_name_fragment(telegram_user_id: int, fragment: str) -> dict | None:
    lowered = fragment.strip().lower()
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
    and names don't always match (e.g. 'sera_wanderer' -> 'Sera'). Never
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


def _clear_all_stateful_flows(context: ContextTypes.DEFAULT_TYPE) -> bool:
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

    global _LAST_KNOWN_CHAT_ID
    _LAST_KNOWN_CHAT_ID = update.effective_chat.id
    db.touch_last_active(update.effective_user.id)
    _IDLE_WARNED.discard(update.effective_user.id)

    # Universal escape hatch, checked FIRST, before any stateful flow gets
    # a chance to swallow the message. Exact-match only (never a substring
    # check) so ordinary gameplay text like "I stop to look around" or
    # "I reset the trap" is never misread as a cancel command.
    text_exact = update.message.text.strip().lower()
    if text_exact in UNIVERSAL_ESCAPE_PHRASES:
        had_active_state = _clear_all_stateful_flows(context)

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

    text = update.message.text.strip()
    known_npcs = [data["name"] for data in CAMPAIGN["npcs"].values()]
    # A genuinely compound message ("recruit Sera, look at the quest
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
            await update.effective_chat.send_message(
                f"💀 **{character['name']}** is dead and can't act or be moved until revived — "
                f"staying right where they died. Say \"switch to <character>\" to play someone else, "
                f"or \"I want to create a character\" to start fresh.",
                message_thread_id=config.TOPIC_ADVENTURE_ID,
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
        # 2026-07-13 (Coffee): a genuinely compound message ("recruit Sera
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
        for i in intents:
            await _dispatch_intent(proxied_update, context, i, text)
        if chat_proxy.buffered:
            await _safe_send(update, "\n\n".join(chat_proxy.buffered))
        return

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
            if key.replace("_", " ") in lowered:
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
        if npc_id and npc_id in _NPCS:
            character = db.get_character(update.effective_user.id)
            character_name = character["name"] if character else "the player"
            relationship = db.get_relationship(update.effective_user.id, npc_id)
            quest_facts = _npc_quest_facts(character, npc_id) if character else None
            reply = await asyncio.to_thread(
                talk_to_npc, npc_id, text, character_name, relationship["memory_events"], quest_facts
            )
            npc_display_name = CAMPAIGN["npcs"].get(npc_id, {}).get("name", intent["npc_name"])
            await _safe_send(update, f"💬 **{npc_display_name}:** {reply}")
            # Ordinary conversation builds a small amount of rapport over
            # time — real, persistent, and separate from the short-term
            # conversation buffer talk_to_npc already keeps.
            db.adjust_affinity(update.effective_user.id, npc_id, 1)
            faction_id = _faction_for_npc(npc_id)
            if faction_id:
                db.adjust_faction_standing(
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
    elif action == "list_characters":
        await _do_list_characters(update)
    elif action == "switch_character":
        await _do_switch_character(update, intent.get("target") or text)
    elif action == "delete_character":
        await _do_delete_character(update, intent.get("target") or text)
    # action == "chat" (or unmatched talk_npc): no game action, let it be
    # ordinary roleplay chatter with no bot response required.


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


async def version_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.effective_chat.send_message(
        f"Pandora MMO v{version.get_version()}",
        message_thread_id=update.message.message_thread_id,
    )


async def changelog_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.effective_chat.send_message(
        version.get_changelog(),
        message_thread_id=update.message.message_thread_id,
    )


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


async def development_topic_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    is_owner = await _is_group_owner(update, context)
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

    is_owner = await _is_group_owner(update, context)
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

    is_owner = await _is_group_owner(update, context)
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


_NAMED_SHEET_EXCLUDED_WORDS = ("the", "a", "an", "her", "his", "their", "your", "our")
_SELF_SHEET_WORDS = ("my", "mine", "myself")


async def support_topic_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    question = update.message.text.strip()
    character = db.get_character(update.effective_user.id)
    # 2026-07-14, per Coffee: "Is Sera in my current party?" and "Show
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
    # character sheet and Sera's character sheet" -- TWO sheets in one
    # message -- used re.search, which only ever finds the FIRST match
    # ("my", which was then excluded), so this fell through to the LLM
    # entirely. The model then answered Coffee's own sheet reasonably
    # but OUTRIGHT INVENTED Sera's ("mirrors this structure... similar
    # stats based on available data") -- a real hallucination of fake
    # stats, exactly what this game's grounding rules exist to prevent.
    # Fixed with re.findall (every match, not just the first) and a
    # real per-name lookup for each one, never handing ANY of it to the
    # model once at least one real name resolves.
    sheet_names = re.findall(r"(\w+)(?:'s)? (?:character )?sheet", question.lower())
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


async def _ai_party_autonomous_tick(bot) -> None:
    """
    Advances ONE AI-controlled character's turn per cycle (never all at
    once, to keep Adventure from being flooded). Skips anyone currently
    in an active combat session — that already auto-resolves via the
    existing is_ai=1 mechanism with no action needed here.

    2026-07-14, per Coffee: "make recruitable able to make their own
    choices... this goes for AIs also." Previously only the separate
    hardcoded autonomous party (Zara/Bram) ever got a tick here --
    recruited companions like Sera sat completely idle between being
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

    # Recruited companions (e.g. Sera) aren't in the hardcoded
    # AI_PARTY_ROSTER at all -- their real personality lives in
    # campaign.json's NPC entry instead, same place their sheet-lookup
    # basic-info fallback already reads from.
    personality = next((m["personality"] for m in AI_PARTY_ROSTER if m["name"] == actor["name"]), None)
    if personality is None:
        npc = _find_campaign_npc_by_name(actor["name"])
        personality = npc.get("personality", "") if npc else ""
    situation_facts = _build_ai_player_situation_facts(actor, actor["current_location"])
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
            await _maybe_post_hourly_status_update(application.bot)
        except Exception as e:
            logger.error(f"[hourly_update] failed this cycle: {e!r}")
        try:
            db.expire_stale_board_quests()
        except Exception as e:
            logger.error(f"[board_quests] expiry check failed this cycle: {e!r}")
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


async def _on_startup(application: Application) -> None:
    # Application.create_task ties this loop's lifecycle to the
    # application, so it's cancelled cleanly on shutdown.
    application.create_task(_idle_inactivity_loop(application))


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
    # needs to survive them, unlike combat sessions/conditions, which are
    # deliberately NOT persisted (see CLAUDE.md).
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
    application.add_handler(CommandHandler("newcharacter", newcharacter_command))
    application.add_handler(CommandHandler("startcombat", startcombat_command))
    application.add_handler(CommandHandler("attack", attack_command))
    application.add_handler(CommandHandler("endturn", endturn_command))
    application.add_handler(CommandHandler("sheet", sheet_command))
    application.add_handler(CommandHandler("version", version_command))
    application.add_handler(CommandHandler("changelog", changelog_command))

    # Single unified router for all plain text messages, across topics.
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, text_message_router))
    application.add_handler(MessageHandler(filters.PHOTO, dev_topic_photo_handler))
    application.add_handler(MessageHandler(filters.Document.ALL, dev_topic_document_handler))

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
