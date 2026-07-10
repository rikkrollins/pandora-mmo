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
import random

from telegram import Update
from telegram.ext import (
    Application,
    ApplicationBuilder,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

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
import topics
from ai.dev_agent import answer_dev_question
from ai.dm_agent import narrate_action, narrate_welcome, narrate_skill_check
from ai.intent_parser import parse_intent
from ai.npc_agent import register_npc, talk_to_npc, generate_ambient_line, _NPCS
from ai.support_agent import answer_support_question
from guilds import GUILDS, eligible_for_guild
from models import (
    VALID_CLASSES,
    VALID_RACES,
    STARTING_EQUIPMENT,
    STARTING_GOLD,
    BASE_ARMOR_CLASS,
)
from rules.combat import resolve_attack, resolve_death_save
from rules.crafting import RECIPES, get_recipe, resolve_craft
from rules.dice import roll, ability_modifier, roll_ability_check, roll_d20
from rules.leveling import CLASS_HIT_DICE
from rules.proficiency import practiced_bonus

logging.basicConfig(
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger("pandora_mmo")

ACTIVE_CAMPAIGN_ID = "default"
CAMPAIGN = cl.load_campaign(ACTIVE_CAMPAIGN_ID)

DEFAULT_WEAPON = {"ability": "strength", "damage_dice": "1d8", "damage_bonus": 0}

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


async def hear_you_main(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Simple connection-test responder for the Main topic (unrelated to game logic)."""
    if update.message is None or not topics.is_main(update.message.message_thread_id):
        return
    username = update.effective_user.first_name if update.effective_user else "there"
    await update.message.reply_text(
        f"I hear you, {username}", message_thread_id=update.message.message_thread_id
    )


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


def _party_summary_text() -> str:
    """
    A clear, factual description of who's currently in the party and how
    many, e.g. 'Kara, Finn (AI companion) — 2 members'. Never guesses or
    invents members; only reports what's actually in the database.
    """
    party = _get_party_members()
    if not party:
        return "No one has joined yet."
    names = [
        f"{p['name']} (AI companion)" if p.get("is_ai") else p["name"]
        for p in party
    ]
    return f"{', '.join(names)} — {len(party)} member{'s' if len(party) != 1 else ''}"


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

    await update.effective_chat.send_message(
        welcome_text, message_thread_id=config.TOPIC_ADVENTURE_ID
    )


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
    await update.message.reply_text(
        f"{prefix}Let's create your character! What's their name?",
        message_thread_id=config.TOPIC_ADVENTURE_ID,
    )


async def _continue_character_creation(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    creation = context.user_data["creation"]
    step = creation["step"]
    text = update.message.text.strip()

    if step == "name":
        creation["name"] = text
        creation["step"] = "race"
        await update.message.reply_text(
            f"Nice! What race? Choose one: {', '.join(VALID_RACES)}",
            message_thread_id=config.TOPIC_ADVENTURE_ID,
        )
        return

    if step == "race":
        race = text.title()
        if race not in VALID_RACES:
            await update.message.reply_text(
                f"Please choose one of: {', '.join(VALID_RACES)}",
                message_thread_id=config.TOPIC_ADVENTURE_ID,
            )
            return
        creation["race"] = race
        creation["step"] = "class"
        await update.message.reply_text(
            f"Great, a {race}! What class? Choose one: {', '.join(VALID_CLASSES)}",
            message_thread_id=config.TOPIC_ADVENTURE_ID,
        )
        return

    if step == "class":
        char_class = text.title()
        if char_class not in VALID_CLASSES:
            await update.message.reply_text(
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
        await update.message.reply_text(
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
            await update.message.reply_text(
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
        if char_class in ("Wizard", "Sorcerer"):
            armor_class += dex_mod

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
        await update.message.reply_text(sheet, message_thread_id=config.TOPIC_ADVENTURE_ID)
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
            await update.effective_chat.send_message(
                f"💀 **{entry['name']} has fallen!** They'll need to rest once "
                f"combat ends to recover (just say \"I rest\" in Adventure).",
                message_thread_id=config.TOPIC_ADVENTURE_ID,
            )
        else:
            await update.effective_chat.send_message(
                f"💀 **{entry['name']} has been defeated!**",
                message_thread_id=config.TOPIC_ADVENTURE_ID,
            )


async def _post_narrated(update: Update, character: dict, action_text: str,
                          mechanical_result: dict, session: sessions.Session) -> None:
    flavor = await asyncio.to_thread(
        narrate_action, character, action_text, mechanical_result, session.recent_events()
    )
    message = _format_combat_result(
        flavor, mechanical_result,
        actor_label=mechanical_result.get("attacker", character.get("name", "?")),
        defender_label=mechanical_result.get("defender", "?"),
    )
    session.log_event(f"{mechanical_result.get('attacker')} vs {mechanical_result.get('defender')}: {flavor}")
    await update.effective_chat.send_message(message, message_thread_id=config.TOPIC_ADVENTURE_ID)


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
    for pid in real_party_ids:
        before = db.get_character(pid)
        after = db.add_xp(pid, xp_each)
        if after["level"] > before["level"]:
            level_up_notes.append(
                f"🎉 {after['name']} leveled up to {after['level']}! "
                f"(HP max: {before['hp_max']} → {after['hp_max']})"
            )

    summary = f"\n✨ Party gains {xp_each} XP each ({enemy_xp_total} total)."
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
            await update.effective_chat.send_message(
                "⚠️ Combat seems stuck in a loop — ending it automatically. "
                "Please report this if it happens again.",
                message_thread_id=config.TOPIC_ADVENTURE_ID,
            )
            sessions.end_session(session.chat_id)
            return

        stall_threshold = max(len(session.turn_order) * 2, 4)
        if consecutive_noop_turns >= stall_threshold:
            await update.effective_chat.send_message(
                "🏳️ **Stalemate — the enemy breaks off, unable to finish the fight. "
                "Your party survives.**",
                message_thread_id=config.TOPIC_ADVENTURE_ID,
            )
            sessions.end_session(session.chat_id)
            return

        current = session.current_participant()

        if session.is_downed(current["telegram_user_id"]):
            consecutive_noop_turns = 0
            death_result = resolve_death_save(current)
            _sync_player_to_db(current)

            if death_result["outcome"] == "dead":
                await update.effective_chat.send_message(
                    f"💀 **{current['name']} rolls a {death_result['roll']} — "
                    f"3rd failed death save. {current['name']} has died.**",
                    message_thread_id=config.TOPIC_ADVENTURE_ID,
                )
                session.remove_dead_player(current["telegram_user_id"])
            elif death_result["outcome"] == "stable":
                session.stabilized_ids.add(current["telegram_user_id"])
                await update.effective_chat.send_message(
                    f"💚 **{current['name']} rolls a {death_result['roll']} — "
                    f"3rd death save success! {current['name']} is stabilized "
                    f"(unconscious but no longer in danger).**",
                    message_thread_id=config.TOPIC_ADVENTURE_ID,
                )
            elif death_result["outcome"] == "natural_20_revived":
                await update.effective_chat.send_message(
                    f"✨ **{current['name']} rolls a NATURAL 20 on their death save "
                    f"and springs back up with 1 HP!**",
                    message_thread_id=config.TOPIC_ADVENTURE_ID,
                )
            else:
                await update.effective_chat.send_message(
                    f"🎲 **{current['name']}'s death save: rolled {death_result['roll']} "
                    f"({death_result['outcome']}) — "
                    f"{death_result['successes']} successes, {death_result['failures']} failures.**",
                    message_thread_id=config.TOPIC_ADVENTURE_ID,
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
            await update.effective_chat.send_message(
                f"😴 **{current['name']} is unconscious and stable** — "
                f"they can't act until healed above 0 HP.",
                message_thread_id=config.TOPIC_ADVENTURE_ID,
            )
            if session.is_combat_over():
                break
            session.advance_turn()
            continue

        if not current.get("is_ai"):
            await update.effective_chat.send_message(
                _turn_announcement(session), message_thread_id=config.TOPIC_ADVENTURE_ID
            )
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
        target = min(opposing, key=lambda p: p["hp_current"])
        adv, disadv = _attack_advantage_disadvantage(current, target)
        result = resolve_attack(current, target, DEFAULT_WEAPON, advantage=adv, disadvantage=disadv)
        _sync_player_to_db(target)

        applied_condition = None
        if result["hit"] and current.get("on_hit_condition"):
            condition = current["on_hit_condition"]
            target.setdefault("conditions", [])
            if condition not in target["conditions"]:
                target["conditions"].append(condition)
                applied_condition = condition

        await _post_narrated(update, current, f"{current['name']} attacks {target['name']}", result, session)
        if applied_condition:
            await update.effective_chat.send_message(
                f"☠️ **{target['name']} is now {applied_condition.upper()}!**",
                message_thread_id=config.TOPIC_ADVENTURE_ID,
            )

        if target["hp_current"] <= 0 and not target.get("is_ai"):
            await update.effective_chat.send_message(
                f"⚠️ **{target['name']} drops to 0 HP and falls unconscious!** "
                f"They'll roll death saving throws on their turns until stable, revived, or worse.",
                message_thread_id=config.TOPIC_ADVENTURE_ID,
            )

        removed = session.remove_defeated()
        await _announce_defeats(update, session, removed)
        if session.is_combat_over():
            break
        session.advance_turn()

    if session.is_combat_over():
        winner = _determine_winner(session)
        xp_summary = _award_victory_xp(session) if winner == "party" else ""
        await update.effective_chat.send_message(
            f"🏆 **Combat over!** The {winner} side is victorious!{xp_summary}",
            message_thread_id=config.TOPIC_ADVENTURE_ID,
        )
        sessions.end_session(session.chat_id)


_NUMBER_WORDS = {"one": 1, "two": 2, "three": 3, "four": 4, "a couple of": 2, "a few": 3}


def _parse_enemy_count(lowered_text: str) -> int:
    """
    Looks for an explicit count of enemies in natural language ("3
    goblins", "two goblins", "a few goblins"). Defaults to 2 if a
    plural monster name appears with no explicit number (a reasonable
    "small group" default), or 1 otherwise. Capped at 4 for sanity —
    this build's targeting is simple and a huge mob would be unwieldy
    to specify targets for via plain text.
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
    return 1


async def _do_start_combat(update: Update, monster_key: str | None = None, count: int = 1) -> None:
    chat_id = update.effective_chat.id
    async with sessions.get_lock(chat_id):
        if sessions.get_session(chat_id) is not None:
            await update.effective_chat.send_message(
                "A combat session is already active! Say \"cancel\" if you think "
                "it's stuck and need to force-end it.",
                message_thread_id=config.TOPIC_ADVENTURE_ID,
            )
            return

        all_characters = _get_party_members()
        # Characters at 0 HP can't fight until they rest — never silently
        # dragged into a new combat as if nothing happened.
        party = [p for p in all_characters if p["hp_current"] > 0]
        downed = [p for p in all_characters if p["hp_current"] <= 0 and not p.get("is_ai")]

        if not party:
            if downed:
                await update.effective_chat.send_message(
                    "Everyone in your party has fallen — say \"I rest\" to recover before continuing.",
                    message_thread_id=config.TOPIC_ADVENTURE_ID,
                )
            else:
                await update.effective_chat.send_message(
                    "No characters exist yet — say something like 'I want to create a character' first!",
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
            f"Your party ({_party_summary_text()}) faces {enemy_description}!\n\n"
            f"🎯 **Initiative order:** {initiative_line}\n\n"
            + _turn_announcement(session)
        )
        await update.effective_chat.send_message(header, message_thread_id=config.TOPIC_ADVENTURE_ID)
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

    npc_ids = location.get("npcs", [])
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
            all_characters = _get_party_members()
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
                f"Your party ({_party_summary_text()}) faces **{npc_data['name']}**!\n\n"
                f"🎯 **Initiative order:** {initiative_line}\n\n"
                + _turn_announcement(session)
            )
            await update.effective_chat.send_message(header, message_thread_id=config.TOPIC_ADVENTURE_ID)
            await _resolve_ai_turns(update, session)
    else:
        line = await asyncio.to_thread(
            generate_ambient_line, npc_id, character["name"], "arrives", memory_facts
        )
        if line:
            await update.effective_chat.send_message(
                f"💬 **{npc_data['name']}:** {line}", message_thread_id=config.TOPIC_ADVENTURE_ID
            )


async def _do_attack(update: Update, action_text: str) -> None:
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
        result = resolve_attack(attacker, target, DEFAULT_WEAPON, advantage=adv, disadvantage=disadv)
        _sync_player_to_db(target)
        await _post_narrated(update, attacker, action_text, result, session)

        removed = session.remove_defeated()
        await _announce_defeats(update, session, removed)

        if session.is_combat_over():
            winner = _determine_winner(session)
            xp_summary = _award_victory_xp(session) if winner == "party" else ""
            await update.effective_chat.send_message(
                f"🏆 **Combat over!** The {winner} side is victorious!{xp_summary}",
                message_thread_id=config.TOPIC_ADVENTURE_ID,
            )
            sessions.end_session(chat_id)
            return

        session.advance_turn()
        await _resolve_ai_turns(update, session)


async def _do_pass_turn(update: Update) -> None:
    chat_id = update.effective_chat.id
    async with sessions.get_lock(chat_id):
        session = sessions.get_session(chat_id)
        if session is None:
            await update.effective_chat.send_message(
                "There's no active turn to pass right now.",
                message_thread_id=config.TOPIC_ADVENTURE_ID,
            )
            return
        session.advance_turn()
        await update.effective_chat.send_message(
            _turn_announcement(session), message_thread_id=config.TOPIC_ADVENTURE_ID
        )
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

    await update.effective_chat.send_message(
        f"🤝 {npc['name']} joins your party! {_party_summary_text()}",
        message_thread_id=config.TOPIC_ADVENTURE_ID,
    )


# A fixed, moderate DC for all non-combat skill checks. Real 5E lets a
# DM set the DC per situation (Easy=10, Medium=15, Hard=20, etc.) — this
# build uses one flat value (13, roughly "moderately hard") for every
# check as a deliberate, documented simplification, rather than having
# the AI invent a DC per situation (which risked exactly the kind of
# made-up-numbers problem already fixed elsewhere in this build).
SKILL_CHECK_DC = 13
STEAL_DC = 15  # harder than an ordinary skill check — stealing carries real risk


def _practiced_bonus_for(telegram_user_id: int, ability: str) -> int:
    """Current earned bonus for this ability from repeated real use (rules/proficiency.py)."""
    uses = db.get_skill_uses(telegram_user_id).get(ability, 0)
    return practiced_bonus(uses)


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
    db.record_skill_use(update.effective_user.id, "dexterity")
    success = result["total"] >= SKILL_CHECK_DC

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
    await update.effective_chat.send_message(message, message_thread_id=config.TOPIC_ADVENTURE_ID)


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
    db.record_skill_use(update.effective_user.id, ability)
    success = result["total"] >= SKILL_CHECK_DC

    flavor = await asyncio.to_thread(
        narrate_skill_check, character, action_text, ability,
        {**result, "ability": ability, "dc": SKILL_CHECK_DC, "success": success},
    )
    message = _format_skill_check_result(flavor, result, ability, SKILL_CHECK_DC, success)
    await update.effective_chat.send_message(message, message_thread_id=config.TOPIC_ADVENTURE_ID)


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
    """
    attacker_conditions = attacker.get("conditions", [])
    defender_conditions = defender.get("conditions", [])
    disadvantage = "prone" in attacker_conditions or "poisoned" in attacker_conditions
    advantage = "prone" in defender_conditions
    return advantage, disadvantage


def _condition_tags(character: dict) -> str:
    """Short display tags for a character's active conditions, e.g. '🛌😷'."""
    icons = {"prone": "🛌", "poisoned": "😷"}
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
        await update.effective_chat.send_message(message, message_thread_id=config.TOPIC_ADVENTURE_ID)

        session.advance_turn()
        await _resolve_ai_turns(update, session)


async def _do_rest(update: Update) -> None:
    chat_id = update.effective_chat.id
    if sessions.get_session(chat_id) is not None:
        await update.effective_chat.send_message(
            "You can't rest in the middle of combat.", message_thread_id=config.TOPIC_ADVENTURE_ID
        )
        return

    character = db.get_character(update.effective_user.id)
    if character is None:
        await update.effective_chat.send_message(
            "You don't have a character yet!", message_thread_id=config.TOPIC_ADVENTURE_ID
        )
        return

    hp_full = character["hp_current"] == character["hp_max"]
    slots_full = character["spell_slots_current"] == character["spell_slots_max"]
    if hp_full and slots_full:
        await update.effective_chat.send_message(
            "You're already at full health and spell slots.", message_thread_id=config.TOPIC_ADVENTURE_ID
        )
        return

    db.update_character(update.effective_user.id, hp_current=character["hp_max"],
                         death_save_successes=0, death_save_failures=0)
    db.restore_spell_slots(update.effective_user.id)

    slot_note = ""
    if character["spell_slots_max"] > 0:
        slot_note = f" Spell slots restored to {character['spell_slots_max']}/{character['spell_slots_max']}."
    await update.effective_chat.send_message(
        f"🌙 You rest and recover. HP restored to {character['hp_max']}/{character['hp_max']}.{slot_note}",
        message_thread_id=config.TOPIC_ADVENTURE_ID,
    )


async def _do_check_party(update: Update) -> None:
    await update.effective_chat.send_message(
        f"👥 Current party: {_party_summary_text()}",
        message_thread_id=config.TOPIC_ADVENTURE_ID,
    )


async def _do_check_sheet(update: Update) -> None:
    character = db.get_character(update.effective_user.id)
    if character is None:
        await update.effective_chat.send_message(
            "You don't have a character yet — say something like "
            "'I want to create a character' to get started!",
            message_thread_id=config.TOPIC_ADVENTURE_ID,
        )
        return
    spell_names = [spells_module.get_spell(s)["name"] for s in character["known_spells"]]
    race_data = races_module.get_race(character["race"])
    features = class_features_module.get_class_features(character["char_class"])
    slot_line = ""
    if character["spell_slots_max"] > 0:
        slot_line = f"Spell slots: {character['spell_slots_current']}/{character['spell_slots_max']}\n"
    sheet = (
        f"**{character['name']}** — {character['race']} {character['char_class']}\n"
        f"Level {character['level']} | XP {character['xp']}\n"
        f"HP {character['hp_current']}/{character['hp_max']} | AC {character['armor_class']}\n"
        f"Gold: {character['gold']} | Guild: {character['guild'] or 'None'}\n"
        f"Spells known: {', '.join(spell_names) if spell_names else 'None'}\n"
        f"{slot_line}"
        f"Racial traits: {'; '.join(race_data['traits']) if race_data else 'None'}\n"
        f"Class features: {'; '.join(features) if features else 'None'}\n"
        f"Location: {cl.get_location(CAMPAIGN, character['current_location'])['name']}"
    )
    await update.effective_chat.send_message(sheet, message_thread_id=config.TOPIC_ADVENTURE_ID)


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
    await update.effective_chat.send_message(
        "🎒 Your backpack:\n" + "\n".join(lines), message_thread_id=config.TOPIC_ADVENTURE_ID
    )


def _find_resource_node(location: dict, action_text: str) -> dict | None:
    nodes = location.get("resource_nodes", [])
    if not nodes:
        return None
    lowered = action_text.lower()
    for node in nodes:
        if node["id"] in lowered or node["material"] in lowered or node["name"].lower() in lowered:
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
        await update.effective_chat.send_message(
            "There's nothing worth gathering here.", message_thread_id=config.TOPIC_ADVENTURE_ID
        )
        return

    result = roll_ability_check(character, node["ability"], proficient=False)
    bonus = _practiced_bonus_for(update.effective_user.id, node["ability"])
    result["total"] += bonus
    result["practiced_bonus"] = bonus
    db.record_skill_use(update.effective_user.id, node["ability"])
    success = result["total"] >= SKILL_CHECK_DC
    material = items_module.get_item(node["material"])

    if success:
        db.add_item(update.effective_user.id, node["material"], 1)

    flavor = await asyncio.to_thread(
        narrate_skill_check, character, action_text, node["ability"],
        {**result, "ability": node["ability"], "dc": SKILL_CHECK_DC, "success": success},
    )
    message = _format_skill_check_result(flavor, result, node["ability"], SKILL_CHECK_DC, success)
    if success:
        message += f"\n🌿 You gather **1x {material['name']}**."
    await update.effective_chat.send_message(message, message_thread_id=config.TOPIC_ADVENTURE_ID)


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

    recipe_ability = get_recipe(recipe_id)["ability"]
    bonus = _practiced_bonus_for(update.effective_user.id, recipe_ability)
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

    db.record_skill_use(update.effective_user.id, recipe_ability)

    for item_id, qty in result["materials_consumed"].items():
        db.remove_item(update.effective_user.id, item_id, qty)

    success = result["outcome"] == "success"
    if success:
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
    await update.effective_chat.send_message(message, message_thread_id=config.TOPIC_ADVENTURE_ID)


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
    npcs_here = location.get("npcs", [])
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

    await update.effective_chat.send_message("\n".join(lines), message_thread_id=config.TOPIC_ADVENTURE_ID)


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

    await update.effective_chat.send_message("\n".join(lines), message_thread_id=config.TOPIC_ADVENTURE_ID)


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
    await update.effective_chat.send_message(
        f"🚶 You travel to **{destination['name']}**.\n{destination['description']}",
        message_thread_id=config.TOPIC_ADVENTURE_ID,
    )

    updated_character = db.get_character(update.effective_user.id)
    await _maybe_trigger_npc_encounter(update, updated_character, destination)


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
    db.move_character(telegram_user_id, destination_id)
    await update.effective_chat.send_message(
        f"🌀 You fast-travel to **{destination['name']}**.\n{destination['description']}",
        message_thread_id=config.TOPIC_ADVENTURE_ID,
    )

    updated_character = db.get_character(telegram_user_id)
    await _maybe_trigger_npc_encounter(update, updated_character, destination)


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

    ok, msg = shop_module.buy_item(update.effective_user.id, shop_data, item_id, 1)
    await update.effective_chat.send_message(msg, message_thread_id=config.TOPIC_ADVENTURE_ID)


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

    ok, msg = shop_module.sell_item(update.effective_user.id, item_id, 1)
    await update.effective_chat.send_message(msg, message_thread_id=config.TOPIC_ADVENTURE_ID)


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
    db.record_skill_use(telegram_user_id, "dexterity")
    success = result["total"] >= STEAL_DC

    if success:
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
    await update.effective_chat.send_message(message, message_thread_id=config.TOPIC_ADVENTURE_ID)


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

    if spell_id is None:
        known = ", ".join(character["known_spells"]) or "none yet"
        await update.effective_chat.send_message(
            f"You don't know a spell by that name. Spells you know: {known}",
            message_thread_id=config.TOPIC_ADVENTURE_ID,
        )
        return

    spell = spells_module.get_spell(spell_id)
    chat_id = update.effective_chat.id

    if spell["effect"] == "damage":
        async with sessions.get_lock(chat_id):
            session = sessions.get_session(chat_id)
            if session is None:
                await update.effective_chat.send_message(
                    "There's nothing to cast that at right now.", message_thread_id=config.TOPIC_ADVENTURE_ID
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
            if spell["level"] > 0:
                spent, _ = db.spend_spell_slot(update.effective_user.id)
                if not spent:
                    await update.effective_chat.send_message(
                        f"You have no spell slots remaining to cast {spell['name']} "
                        f"({character['spell_slots_current']}/{character['spell_slots_max']} left). "
                        f"Rest to recover them.",
                        message_thread_id=config.TOPIC_ADVENTURE_ID,
                    )
                    return

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
                await update.effective_chat.send_message(
                    f"🏆 **Combat over!** The {winner} side is victorious!{xp_summary}",
                    message_thread_id=config.TOPIC_ADVENTURE_ID,
                )
                sessions.end_session(chat_id)
                return
            session.advance_turn()
            await _resolve_ai_turns(update, session)

    elif spell["effect"] == "heal":
        if spell["level"] > 0:
            spent, _ = db.spend_spell_slot(update.effective_user.id)
            if not spent:
                await update.effective_chat.send_message(
                    f"You have no spell slots remaining to cast {spell['name']} "
                    f"({character['spell_slots_current']}/{character['spell_slots_max']} left). "
                    f"Rest to recover them.",
                    message_thread_id=config.TOPIC_ADVENTURE_ID,
                )
                return
        result = spells_module.resolve_heal_spell(spell_id, character, character)
        db.update_character(update.effective_user.id, hp_current=character["hp_current"])
        await update.effective_chat.send_message(
            f"✨ You cast {spell['name']} and heal {result['healing_done']} HP "
            f"({result['hp_current']}/{result['hp_max']}).",
            message_thread_id=config.TOPIC_ADVENTURE_ID,
        )
    else:
        if spell["level"] > 0:
            spent, _ = db.spend_spell_slot(update.effective_user.id)
            if not spent:
                await update.effective_chat.send_message(
                    f"You have no spell slots remaining to cast {spell['name']} "
                    f"({character['spell_slots_current']}/{character['spell_slots_max']} left). "
                    f"Rest to recover them.",
                    message_thread_id=config.TOPIC_ADVENTURE_ID,
                )
                return
        await update.effective_chat.send_message(
            f"✨ You cast {spell['name']}. (Note: this spell's flavor is real, but it "
            f"doesn't yet apply a mechanical effect in this build — that's a known "
            f"limitation, not a bug.)",
            message_thread_id=config.TOPIC_ADVENTURE_ID,
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
    await update.effective_chat.send_message(
        f"🏛️ You've joined {GUILDS[guild_id]['name']}!",
        message_thread_id=config.TOPIC_ADVENTURE_ID,
    )


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
    await update.effective_chat.send_message("\n".join(lines), message_thread_id=config.TOPIC_ADVENTURE_ID)


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
    await update.effective_chat.send_message(
        f"🎭 Switched to **{switched['name']}** the {switched['race']} {switched['char_class']} "
        f"(Level {switched['level']}).",
        message_thread_id=config.TOPIC_ADVENTURE_ID,
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


def setup_default_npcs() -> None:
    for npc_id, npc_data in CAMPAIGN["npcs"].items():
        register_npc(
            npc_id, npc_data["name"], npc_data["personality"],
            goals=npc_data.get("goals", ""),
            alignment=npc_data.get("alignment", ""),
            disposition=npc_data.get("disposition", "friendly"),
        )


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

    # Universal escape hatch, checked FIRST, before any stateful flow gets
    # a chance to swallow the message. Exact-match only (never a substring
    # check) so ordinary gameplay text like "I stop to look around" or
    # "I reset the trap" is never misread as a cancel command.
    text_exact = update.message.text.strip().lower()
    if text_exact in UNIVERSAL_ESCAPE_PHRASES:
        had_active_state = _clear_all_stateful_flows(context)

        chat_id = update.effective_chat.id
        had_active_combat = False
        async with sessions.get_lock(chat_id):
            if sessions.get_session(chat_id) is not None:
                sessions.end_session(chat_id)
                had_active_combat = True

        if had_active_state and had_active_combat:
            msg = "Cancelled, and force-ended the stuck combat session. You're free to act again."
        elif had_active_combat:
            msg = "Force-ended the stuck combat session. You're free to act again."
        elif had_active_state:
            msg = "Cancelled. Say 'I want to create a character' whenever you're ready to try again."
        else:
            msg = "There's nothing in progress to cancel right now."

        await update.message.reply_text(msg, message_thread_id=config.TOPIC_ADVENTURE_ID)
        return

    # If this user is mid-character-creation, that flow owns their next message.
    if "creation" in context.user_data:
        await _continue_character_creation(update, context)
        return

    text = update.message.text.strip()
    known_npcs = [data["name"] for data in CAMPAIGN["npcs"].values()]
    intent = await asyncio.to_thread(parse_intent, text, known_npc_names=known_npcs)
    action = intent["action"]

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
    elif action == "check_inventory":
        await _do_check_inventory(update)
    elif action == "check_party":
        await _do_check_party(update)
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
        await _do_check_sheet(update)
    elif action == "talk_npc" and intent.get("npc_name"):
        npc_id = _find_npc_id_by_name(intent["npc_name"])
        if npc_id and npc_id in _NPCS:
            character = db.get_character(update.effective_user.id)
            character_name = character["name"] if character else "the player"
            relationship = db.get_relationship(update.effective_user.id, npc_id)
            reply = await asyncio.to_thread(
                talk_to_npc, npc_id, text, character_name, relationship["memory_events"]
            )
            await update.message.reply_text(reply, message_thread_id=config.TOPIC_ADVENTURE_ID)
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
    elif action == "skill_check":
        await _do_skill_check(update, intent.get("ability") or "dexterity", text)
    elif action == "shove":
        await _do_shove(update, text)
    elif action == "show_map":
        await _do_show_map(update)
    elif action == "gather":
        await _do_gather(update, intent.get("raw_text", text))
    elif action == "craft":
        await _do_craft(update, text)
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
    await update.message.reply_text(
        f"Pandora MMO v{version.get_version()}",
        message_thread_id=update.message.message_thread_id,
    )


async def changelog_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text(
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
        await update.message.reply_text(
            "Couldn't verify permissions just now (a Telegram API call failed) — try again in a moment.",
            message_thread_id=config.TOPIC_DEVELOPMENT_ID,
        )
        return
    if not is_owner:
        await update.message.reply_text(
            "The Development topic is restricted to the group owner.",
            message_thread_id=config.TOPIC_DEVELOPMENT_ID,
        )
        return

    question = update.message.text.strip()
    history = context.user_data.setdefault("dev_history", [])

    reply = await asyncio.to_thread(answer_dev_question, question, history)

    history.append(f"Developer: {question}")
    history.append(f"Assistant: {reply}")
    del history[:-MAX_ASSISTANT_HISTORY]

    await update.message.reply_text(reply, message_thread_id=config.TOPIC_DEVELOPMENT_ID)


async def support_topic_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    question = update.message.text.strip()
    reply = await asyncio.to_thread(answer_support_question, question)
    await update.message.reply_text(reply, message_thread_id=config.TOPIC_SUPPORT_ID)


async def text_message_router(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """
    Single entry point for ALL plain text messages, across every topic.
    This exists because registering hear_you_main and
    adventure_master_handler as two separate handlers with identical
    filters caused a real bug: python-telegram-bot only runs the FIRST
    matching handler per update by default, so the second one never
    fired at all, in ANY topic. Routing internally, in one handler,
    guarantees both code paths actually run.
    """
    if update.message is None or not update.message.text:
        return
    raw_thread_id = update.message.message_thread_id  # may genuinely be None for Main
    thread_id = raw_thread_id or 0

    if topics.is_main(raw_thread_id):
        await hear_you_main(update, context)
        return

    if topics.is_adventure(thread_id):
        await adventure_master_handler(update, context)
        return

    if topics.is_development(thread_id):
        await development_topic_handler(update, context)
        return

    if topics.is_support(thread_id):
        await support_topic_handler(update, context)
        return


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
            await update.message.reply_text(
                "Something went wrong processing that — try again in a moment.",
                message_thread_id=update.message.message_thread_id,
            )
        except Exception:
            pass


def build_application() -> Application:
    # concurrent_updates=True is important: without it, python-telegram-bot
    # processes updates ONE AT A TIME. If any single handler ever stalls
    # (e.g. a slow/hung network call), the ENTIRE bot goes silent across
    # every topic until it resolves — exactly the kind of total, unrelated
    # freeze that's hard to diagnose. With this enabled, a stuck handler
    # only affects the update that triggered it.
    application = ApplicationBuilder().token(config.BOT_TOKEN).concurrent_updates(True).build()

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
