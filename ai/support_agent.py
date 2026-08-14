"""
ai/support_agent.py
Conversational assistant for the Support topic. Answers player
questions about HOW TO PLAY (not story content, not spoilers) — things
like "how do I attack" or "how do I check my inventory". Grounded in
the actual command list AND the actual item/spell/guild catalogs, so
it can't invent content that doesn't exist in this build (e.g. naming
a spell that was never defined in spells.py).

Uses config.DM_NARRATION_MODEL, since this is a natural, conversational
task rather than a structured/technical one.
"""
import logging
import re
import time

import requests

import campaign_loader as cl
import config
import items as items_module
import races as races_module
import spells as spells_module
from ai.text_cleanup import strip_think_tags
from guilds import GUILDS
from models import VALID_CLASSES
from rules.crafting import RECIPES, ENCHANT_RECIPES
from rules.leveling import (
    XP_THRESHOLDS, level_for_xp, MAX_LEVEL, CLASS_SUBCLASSES, COMBAT_SUBCLASS_NAMES,
    COMBAT_SUBCLASS_DAMAGE_BONUS_PCT, THIEF_SUBCLASS_STEAL_BONUS, LIFE_SUBCLASS_HEAL_BONUS,
    TOTEM_WARRIOR_SUBCLASS_NAME, UTILITY_SUBCLASS_ABILITY_CHECK_BONUS, UTILITY_SUBCLASS_CHECK_BONUS_VALUE,
)

# Same logger name/handlers as bot.py, so Support failures actually
# land in bot_live_tmp.log -- real gap found 2026-08-10: this file's
# retry-failure diagnostic was a bare print(), and stdout is
# block-buffered (not a tty) under the systemd service, so EVERY prior
# Support failure (2026-07-18, 2026-08-08 x2, 2026-08-10) had its real
# per-attempt error swallowed into an unflushed buffer -- confirmed by
# grepping bot_live_tmp.log for zero matches across the file's entire
# history despite multiple real, reproduced failures.
logger = logging.getLogger("pandora_mmo")

# Loaded once at import time, same singleton-per-campaign-id caching as
# bot.py's own CAMPAIGN (campaign_loader.load_campaign caches by id, so
# this is not a second real load). Used to ground the location/NPC/
# quest wiki content below.
CAMPAIGN = cl.load_campaign(config.ACTIVE_CAMPAIGN)

# Standard 5E priority order for which ability scores matter most to each
# class -- real, sourced SRD convention, not project-specific data (unlike
# races.py's ability bonuses, which ARE this project's own real data and
# get grounded via _build_catalog_reference below).
_CLASS_PRIORITY_STATS = {
    "Fighter": ["strength", "constitution", "dexterity"],
    "Wizard": ["intelligence", "constitution", "dexterity"],
    "Rogue": ["dexterity", "intelligence", "charisma"],
    "Cleric": ["wisdom", "strength", "constitution"],
    "Ranger": ["dexterity", "wisdom", "constitution"],
    "Barbarian": ["strength", "constitution", "dexterity"],
    "Bard": ["charisma", "dexterity", "constitution"],
    "Druid": ["wisdom", "constitution", "dexterity"],
    "Monk": ["dexterity", "wisdom", "constitution"],
    "Paladin": ["strength", "charisma", "constitution"],
    "Sorcerer": ["charisma", "constitution", "dexterity"],
    "Warlock": ["charisma", "constitution", "dexterity"],
}
_ALL_ABILITIES = ["strength", "dexterity", "constitution", "intelligence", "wisdom", "charisma"]
_ABILITY_ABBREV = {
    "strength": "STR", "dexterity": "DEX", "constitution": "CON",
    "intelligence": "INT", "wisdom": "WIS", "charisma": "CHA",
}

SUPPORT_SYSTEM_PROMPT_HEADER = """You are the support guide for Pandora MMO, a \
Dungeons & Dragons 5E game played entirely through natural language in a \
Telegram group's Adventure topic. Players never need slash commands — they \
just say what they mean. Here is the REAL, ACCURATE list of things a player \
can do (do not invent anything beyond this list):

- "I want to create a character" -> starts character creation (name, race, \
class, then assigning 6 rolled ability scores to STR/DEX/CON/INT/WIS/CHA)
- "Look around" / "where am I" -> describes the current location
- "I head to the [place]" / "go downstairs" / "leave this area" -> travels \
to a connected location
- "What am I carrying?" -> shows backpack contents
- "I want to buy [item]" -> purchases from a shop, only if standing in a \
location that has one
- "sell my [item]" -> sells an item from the backpack for half its value
- "Let's start a fight" / "I attack the goblin" -> begins or continues combat
- "I flee" / "I try to escape" -> breaks off from combat, but every living \
enemy gets one free attack as you go -- not risk-free
- "I cast [spell name]" -> casts a spell the character actually knows
- "examine the [thing]" / "read the [thing]" / "look at the [thing]" -> a \
closer look at one specific object in the current location
- "I gather [material]" -> collects a resource from a real gathering node \
at the current location (herbalism, mining, lumberjacking, fishing are the \
real gathering skills in this game) -- repeated success at a skill raises \
a real, persistent proficiency bonus for it, shown on the character sheet
- "I craft [item]" -> makes an item from a known recipe, if the character \
has the real materials for it in their backpack
- "I make a campfire" -> uses 1 wood to recover a small amount of HP \
outside combat
- "I rest" / going quiet for a while -> heals HP and spell slots over \
real-world elapsed time (not an instant full heal)
- "I want to join the [guild name]" -> requests guild membership, subject to \
level/class requirements
- Saying an NPC's name -> starts an in-character conversation with them
- "recruit [NPC]" / "invite [NPC] to my party" -> asks a recruitable NPC to \
join the party as an AI companion
- "invite [player/companion] to my party" -> invites a fellow real player's \
or AI companion's own character
- "show me my party" / "show me [name]'s character sheet" -> shows a full \
sheet for any real character, human or AI, not just your own -- an \
un-recruited NPC instead gets honest basic info (role/personality), since \
they don't have real combat stats until recruited
- "what's on the quest board" / "my quests" -> shows the location's board \
bounty AND the character's own story/board quest progress -- board quests \
are randomly generated daily bounties (gather or defeat something for XP/ \
gold); story quests are hand-authored and tied to specific locations
- "I accept the quest" / "accept this quest" -> accepts whichever quest is \
currently on offer at the current location
- "cancel" / "stop" / "reset" -> exits out of character creation or any \
other multi-step process, at any time
- "/sheet" or asking about stats/HP/level -> shows the character sheet

Status conditions that can affect a character in combat, each with a real \
mechanical effect (not just flavor text): prone, poisoned, blinded, \
silenced, paralyzed, frightened. A character reduced to 0 HP starts making \
death saves -- 3 failures means real, permanent death, reversible only by \
a genuine Revivify spell/scroll, not an instant free undo."""

CRITICAL_GROUNDING_RULE = """

CRITICAL RULE: The lists below (items, spells, guilds) are the COMPLETE and \
ONLY real content that exists in this game. If a player asks about an item, \
spell, or guild NOT on these lists, you MUST say it does not exist in this \
game — never invent a plausible-sounding D&D item/spell/guild name that \
isn't actually listed here, even if it's a real thing from D&D lore in \
general. Only describe mechanics for things that appear below, using \
exactly the facts given for them."""


def _items_catalog_text() -> str:
    lines = ["\nREAL ITEMS IN THIS GAME:"]
    for item_id, data in items_module.ITEMS.items():
        details = f"{data['name']} ({data['type']}"
        if "damage_dice" in data:
            details += f", damage {data['damage_dice']}"
        if "heal_dice" in data:
            details += f", heals {data['heal_dice']}"
        if "ac_base" in data:
            details += f", AC {data['ac_base']}"
        if "ac_bonus" in data:
            details += f", +{data['ac_bonus']} AC"
        if data.get("note"):
            details += f" — {data['note']}"
        details += ")"
        lines.append(f"- {details}")
    return "\n".join(lines)


# Real gap found 2026-08-11 (dev-topic report: "what does mage hand do?"
# got back "lets you cast spells without mana cost", a pure
# hallucination). "effect": "buff" alone gives the model zero real
# information about what a buff cantrip actually DOES, so it filled the
# gap from general D&D knowledge (Mage Hand's real-5E floating-hand
# effect, not even that correctly) instead of this game's actual
# mechanic -- exactly the grounding gap CLAUDE.md's rule exists to
# prevent. These four cantrips share one real, deterministic mechanic
# (bot.py's _do_cast_spell, `spell_id in ("guidance", "thaumaturgy",
# "mage_hand", "prestidigitation")`): a real +2 bonus to the caster's
# own next skill/ability check -- narrative-utility cantrips reflavored
# with one real mechanical hook per Coffee's direction, not their
# real-5E effects (Mage Hand's floating hand, Prestidigitation's minor
# tricks, etc.), so the override text says so explicitly rather than
# leaving room for the model to reach for the real-5E answer instead.
_CHECK_BONUS_CANTRIPS = ("guidance", "thaumaturgy", "mage_hand", "prestidigitation")
_SPELL_REAL_MECHANIC_OVERRIDES = {
    sid: "gives a real +2 bonus to your own next skill/ability check -- "
         "NOT its real-5E effect, this game reflavors it as a check-boost utility cantrip"
    for sid in _CHECK_BONUS_CANTRIPS
}


def _spells_catalog_text() -> str:
    lines = ["\nREAL SPELLS IN THIS GAME, BY CLASS:"]
    for cls in spells_module.CLASS_SPELL_LISTS:
        cantrip_ids = spells_module.CLASS_CANTRIPS.get(cls, [])
        leveled_ids = spells_module.CLASS_SPELL_LISTS.get(cls, [])
        spell_descriptions = []
        for sid in cantrip_ids:
            spell = spells_module.get_spell(sid)
            detail = _SPELL_REAL_MECHANIC_OVERRIDES.get(sid, spell.get('effect', ''))
            spell_descriptions.append(f"{spell['name']} (cantrip, {detail})")
        for sid in leveled_ids:
            spell = spells_module.get_spell(sid)
            detail = _SPELL_REAL_MECHANIC_OVERRIDES.get(sid, spell.get('effect', ''))
            spell_descriptions.append(f"{spell['name']} ({detail})")
        lines.append(f"- {cls.title()}: {', '.join(spell_descriptions)}")
    return "\n".join(lines)


def _format_guild_benefit(benefit_id: str) -> str:
    """Turns a raw benefit flag (e.g. "shop_discount_10") into readable text -- a trailing number is always a real percentage in this catalog's own data."""
    words = benefit_id.split("_")
    if words[-1].isdigit():
        return " ".join(words[:-1]) + f" {words[-1]}%"
    return " ".join(words)


def _guilds_catalog_text() -> str:
    # Real live bug (2026-08-13, Coffee, Support topic): asked "How do i
    # join the forge guild?" and got back "No additional steps required
    # beyond meeting these criteria" -- confirmed live this was flatly
    # wrong. This catalog only ever listed level+class, so Support had
    # no way to know guilds.py's eligible_for_guild() ALSO requires
    # choosing a real subclass first (for any class-gated guild) and
    # having won at least one real fight (proven_in_combat, for every
    # guild) -- the exact two gates that were actually blocking the
    # player, left entirely out of its own grounding. Naming them here
    # is what CLAUDE.md's grounding rule requires: answer from what's
    # ACTUALLY implemented, not a partial read of it.
    #
    # Real live bug (2026-08-14, Coffee, Support topic: "What does the
    # arcana guild and the wild magic sub class offer to my player?" ->
    # got back "The Arcane Circle amplifies your powers; Wild Magic
    # adds flexibility" -- pure vague filler, no real number anywhere).
    # This catalog never listed a guild's own real `benefits`/
    # `permanent_stat` fields at all, so Support had zero real facts to
    # answer "what does joining X actually give me" -- confirmed the
    # exact same "partial grounding" shape as the fix above, just for a
    # different pair of real fields.
    lines = ["\nREAL GUILDS IN THIS GAME:"]
    for guild_id, guild in GUILDS.items():
        extra = []
        if guild.get("join_requirement_classes"):
            extra.append(f"classes: {', '.join(guild['join_requirement_classes'])}")
            extra.append("must have chosen a real subclass first (say \"choose the path of...\")")
        extra.append("must have won at least one real fight (proving yourself in combat)")
        benefit_text = ", ".join(_format_guild_benefit(b) for b in guild.get("benefits", []))
        lines.append(
            f"- {guild['name']}: requires level {guild['join_requirement_level']}, {', '.join(extra)}. "
            f"Real benefits: {benefit_text}, plus a permanent {guild.get('permanent_stat', '')} boost on joining."
        )
    return "\n".join(lines)


def _subclass_catalog_text() -> str:
    """
    Real live bug, same report as the guild-benefits fix above (2026-08-14,
    Coffee, Support topic: "...and the wild magic sub class offer to my
    player?" answered with pure filler, "adds flexibility" -- no real
    number). Subclasses were never grounded anywhere in this file at
    all, for any class, so a subclass-specific question had nothing
    real to draw from. Mirrors bot.py's own _do_choose_subclass exactly
    -- the real mechanical branch (combat/Assassin/Thief/Life/Totem
    Warrior/generic ability-check bonus) for every one of the 22 real
    subclasses (11 classes x 2 each), never invented here.
    """
    lines = ["\nREAL SUBCLASSES IN THIS GAME (chosen via \"choose the path of...\"):"]
    for char_class, (combat_name, utility_name) in CLASS_SUBCLASSES.items():
        for name in (combat_name, utility_name):
            if name in COMBAT_SUBCLASS_NAMES:
                effect = f"weapon attacks deal {COMBAT_SUBCLASS_DAMAGE_BONUS_PCT}% more damage"
            elif name == "Assassin":
                effect = "every attack becomes a real Backstab attempt (a growing damage multiplier, up to x10, that gets more reliable the more you use it); thrown weapons always hit"
            elif name == "Thief":
                effect = f"+{THIEF_SUBCLASS_STEAL_BONUS} on every steal attempt"
            elif name == "Life":
                effect = f"healing spells restore +{LIFE_SUBCLASS_HEAL_BONUS} extra HP on top of Disciple of Life"
            elif name == TOTEM_WARRIOR_SUBCLASS_NAME:
                effect = "while raging, spell damage against you is halved too, not just weapon hits"
            elif name in UTILITY_SUBCLASS_ABILITY_CHECK_BONUS:
                ability = UTILITY_SUBCLASS_ABILITY_CHECK_BONUS[name]
                effect = f"+{UTILITY_SUBCLASS_CHECK_BONUS_VALUE} on every {ability.capitalize()} check"
            else:
                effect = "no real mechanical hook built yet"
            lines.append(f"- {char_class} / {name}: {effect}")
    return "\n".join(lines)


def _crafting_catalog_text() -> str:
    # Per Coffee (2026-07-14): Support should be "like an encyclopedia
    # or wiki" -- crafting recipes and the real gathering skills were
    # never grounded here at all, so a question like "what can I craft"
    # had nothing real to answer from.
    lines = ["\nREAL CRAFTING RECIPES IN THIS GAME:"]
    for recipe_id, recipe in RECIPES.items():
        result = items_module.get_item(recipe["result_item"])
        material_names = ", ".join(
            f"{qty}x {items_module.get_item(mat)['name'] if items_module.get_item(mat) else mat}"
            for mat, qty in recipe["materials"].items()
        )
        lines.append(
            f"- {result['name'] if result else recipe_id}: needs {material_names} "
            f"({recipe['ability']} check, DC {recipe['dc']})"
        )
    lines.append(
        "\nREAL GATHERING SKILLS IN THIS GAME: herbalism, mining, lumberjacking, "
        "fishing -- each is tied to real resource nodes at specific locations, "
        "uses a real ability check to succeed, and gets a persistent proficiency "
        "bonus the more it's successfully practiced (shown on the character sheet)."
    )
    # Real live bug (2026-08-10, dev-topic screenshot): "How do i enchant
    # my weapon?" got a fully ungrounded, hallucinated answer -- this
    # catalog builder never included ENCHANT_RECIPES at all before now,
    # so the model had zero real facts to work from. Defense in depth
    # alongside _deterministic_enchant_item_answer's exact-phrase bypass
    # -- a differently-worded enchant question that slips past that
    # keyword list still lands on real facts here instead of guessing.
    lines.append("\nREAL ENCHANTMENTS IN THIS GAME (only apply to a real found/crafted magic item, never a plain shop item):")
    for recipe_id, recipe in ENCHANT_RECIPES.items():
        label = recipe_id.replace("enchant_", "")
        material_names = ", ".join(
            f"{qty}x {items_module.get_item(mat)['name'] if items_module.get_item(mat) else mat}"
            for mat, qty in recipe["materials"].items()
        )
        lines.append(
            f"- {label}: needs {material_names} ({recipe['ability']} check, DC {recipe['dc']}), "
            f"applies to {'/'.join(recipe['applies_to'])} items"
        )
    return "\n".join(lines)


def _race_bonus_catalog_text() -> str:
    # Confirmed live (2026-07-11): asked to help assign rolled stats for an
    # Elf Ranger, the model ignored the character's actual race/class
    # entirely and suggested switching to Fighter or Barbarian instead --
    # because this game's real per-race ability bonuses were never in its
    # prompt at all, so it had nothing concrete to reason from. These are
    # this project's own real bonuses (some 5E subraces/variants are
    # simplified here), not to be confused with generic D&D lore.
    lines = ["\nREAL RACE ABILITY SCORE BONUSES IN THIS GAME:"]
    for race_name, race_data in races_module.RACES.items():
        bonus_text = ", ".join(
            f"+{v} {k}" for k, v in race_data["ability_bonuses"].items()
        )
        lines.append(f"- {race_name}: {bonus_text}")
    return "\n".join(lines)


# Real live bug (2026-08-10, found via topic-activity monitoring): both
# real Support questions ever asked in the live log failed with
# "Pandora AI is genuinely overloaded" -- confirmed root cause by
# measuring the actual prompt: SUPPORT_SYSTEM_PROMPT's OLD, unconditional
# full-catalog dump ran ~16,600 characters (~4,150 tokens) BEFORE even
# adding character/location/party facts, for every single question
# regardless of topic -- a "what guilds can I join" question paid the
# same huge prompt-processing cost as a question that actually needed
# the whole catalog. On this box's CPU-only, single-Ollama-slot,
# already-documented 30-160s+ latency, that's real, direct latency
# added to EVERY Support call, not a rare edge case -- explains a 2-for-2
# real failure rate better than "genuinely rare" congestion alone would.
# Filters the catalog to only the section(s) the question's own real
# keywords suggest are relevant, falling back to EVERY section
# (identical to the old always-everything behavior) whenever nothing
# matches -- a genuinely broad/ambiguous question never loses real
# grounding, it just no longer pays the full cost for a narrow one.
_CATALOG_SECTION_KEYWORDS = {
    "items": (["item", "weapon", "armor", "potion", "ring", "amulet", "shield",
               "sword", "dagger", "scroll", "gear", "equip", "wear", "wield"], _items_catalog_text),
    "spells": (["spell", "cast", "cantrip", "magic"], _spells_catalog_text),
    "guilds": (["guild", "join a", "join the", "guilds"], _guilds_catalog_text),
    "crafting": (["craft", "recipe", "brew", "gather", "herbalism", "mining",
                  "lumberjack", "fishing", "forage", "enchant", "imbue"], _crafting_catalog_text),
    "races": (["race", "ability score", "ability bonus", "stat bonus", "racial"], _race_bonus_catalog_text),
    # "subclass"/"sub class"/"archetype" catch a generic question; every
    # real subclass's own literal name (built from CLASS_SUBCLASSES, not
    # hand-typed, so this can't drift if that table ever changes) catches
    # a question that names one directly without ever saying "subclass"
    # at all -- exactly the live report that surfaced this gap ("What
    # does the arcana guild and the wild magic sub class offer").
    "subclasses": (
        ["subclass", "sub class", "archetype"]
        + [name.lower() for names in CLASS_SUBCLASSES.values() for name in names],
        _subclass_catalog_text,
    ),
}


def _build_catalog_reference(question: str | None = None) -> str:
    """
    Builds the real, current item/spell/guild/crafting/race catalog
    text to ground the model. `question=None` (or a question matching
    none of the real keyword categories) returns EVERY section, same
    as this function's original unconditional behavior -- only a
    question that clearly names one or more specific categories gets
    the smaller, filtered prompt.
    """
    if question is not None:
        lowered = question.lower()
        matched = [builder for keywords, builder in _CATALOG_SECTION_KEYWORDS.values()
                   if any(kw in lowered for kw in keywords)]
        if matched:
            return "\n".join(builder() for builder in matched)
    return "\n".join(builder() for _, builder in _CATALOG_SECTION_KEYWORDS.values())


def _build_support_system_prompt(question: str | None = None) -> str:
    """
    Real live bug fix (2026-08-10): this used to be a module-level
    constant built ONCE at import time with the full, unconditional
    catalog baked in -- now a real function so each call can pass the
    real question through to _build_catalog_reference's own relevance
    filtering (see that function's docstring for the actual bug this
    fixes). `question=None` reproduces the exact old always-everything
    behavior, so any other caller is unaffected.
    """
    return (
    SUPPORT_SYSTEM_PROMPT_HEADER
    + _build_catalog_reference(question)
    + CRITICAL_GROUNDING_RULE
    + """

Do not reveal or speculate about PLOT: why anything matters, what a \
mystery's answer is, what the "great evil" or any threat in the world \
really is, what lies beyond a place the player hasn't reached yet, or \
what to do next strategically. If asked something like that, say that's \
part of what they'll discover by playing — never guess or improvise an \
answer to fill the gap.

You MAY act as a plain encyclopedia/wiki for facts the game has ALREADY \
shown this exact player, given to you below as real, current ground \
truth: (1) locations they've actually visited — relay the given \
name/description/connections plainly, as pure physical/world facts, \
never attaching meaning or significance beyond what's stated; (2) NPCs \
they've actually met — their role and general character only, never \
personal history or motives; (3) their own active quest's real title \
and description, stated exactly as given, as a plain task — never \
explain WHY it matters or what they'll find. Never mention, describe, \
or hint at any location, NPC, or quest NOT explicitly given to you \
below — if asked about one that isn't listed, say that's for them to \
discover, exactly like any other story question. You MAY answer \
character-BUILD mechanical questions (e.g. which ability score to assign \
where, what a stat means, how much XP is needed to level up) using real \
5E rules and, if given, the player's own REAL character facts below — \
that's how the game's mechanics work, not a story spoiler. If real \
character facts are provided below, answer questions about THIS \
character using ONLY those facts — never invent or guess a stat, item, \
location, or quest that isn't actually listed. If no character facts are \
given, and the question is clearly about "my character", say they don't \
have one yet. Keep answers short and friendly."""
    )


def _build_character_facts(character: dict) -> str:
    """Real, current facts about the asking player's own character — never invented."""
    xp = character.get("xp", 0)
    level = character.get("level") or level_for_xp(xp)
    next_level_xp = XP_THRESHOLDS.get(level + 1)
    xp_to_next = f"{next_level_xp - xp} XP" if next_level_xp is not None else f"already at max level ({MAX_LEVEL})"

    known_spells = character.get("known_spells") or []
    active_quests = character.get("active_quests") or {}

    # Real gap fixed (2026-08-08, game-wiki expansion): this used to list
    # bare quest_id strings ("welcome_to_the_crossroads"), which tells a
    # player asking "what's my next quest task" nothing usable. The exact
    # same title/description text is already shown to the player, in the
    # open, via the ordinary quest journal ("check quests" -- see bot.py's
    # _do_check_quests) -- so surfacing it here through Support isn't new
    # spoiler exposure, just the same already-seen facts through a
    # different topic. Never adds anything beyond what that journal
    # already shows.
    if active_quests:
        quest_lines = []
        for quest_id in active_quests:
            quest = CAMPAIGN.get("quests", {}).get(quest_id)
            if quest:
                quest_lines.append(f"{quest['title']} — {quest['description']}")
        quests_text = "; ".join(quest_lines) if quest_lines else "none"
    else:
        quests_text = "none"

    return (
        "\n\nREAL FACTS ABOUT THE PLAYER'S OWN CHARACTER (answer personal "
        "questions using ONLY this, never invent or guess):\n"
        f"- Name: {character.get('name')}, a {character.get('race')} {character.get('char_class')}, level {level}\n"
        f"- XP: {xp} total, {xp_to_next} until next level\n"
        f"- Ability scores: STR {character.get('strength')}, DEX {character.get('dexterity')}, "
        f"CON {character.get('constitution')}, INT {character.get('intelligence')}, "
        f"WIS {character.get('wisdom')}, CHA {character.get('charisma')}\n"
        f"- HP: {character.get('hp_current')}/{character.get('hp_max')}, AC {character.get('armor_class')}\n"
        f"- Gold: {character.get('gold')}\n"
        f"- Current location: {character.get('current_location')}\n"
        f"- Known spells: {', '.join(known_spells) if known_spells else 'none'}\n"
        f"- Active quests (their real task, stated plainly -- never explain why it matters "
        f"or what they'll find, just what the quest journal itself already says): {quests_text}"
    )


# Real live bug (dev-topic screenshot, 2026-08-10, Coffee: "Support
# topic is still not working"): a Support question could still starve
# out its full 5-attempt retry budget even after the catalog-size fix,
# because bot.py's 60s world tick (NPC heartbeat narration, hourly
# status updates, AI-companion autonomous turns, Moltbook chatter)
# keeps firing its OWN Ollama calls on a fixed cadence regardless of
# whether any real player is around -- confirmed via Ollama's own
# request log showing back-to-back /api/generate calls saturating the
# single inference slot for the entire ~18 minutes a real Support
# question was retrying, with no gap ever long enough for one of
# Support's attempts to land. This flag lets bot.py's world tick check
# whether a Support answer is actively in flight and skip its own
# ambient AI calls for that cycle, so Support gets a real shot at the
# shared slot instead of losing every race to background chatter.
_support_call_active = False


def is_support_call_active() -> bool:
    return _support_call_active


_XP_QUESTION_WORDS = ["level up", "next level", "xp do i need", "xp to level", "experience do i need"]


def _deterministic_xp_answer(character: dict) -> str:
    """
    Confirmed live (2026-07-10): asked with the correct fact ("300 XP
    until next level") already in its prompt, the model still answered
    "10 XP" -- a flat hallucination of an exact number it was given
    verbatim. XP-to-level has exactly one correct value; there's no
    reason to let free-form generation touch it at all, same reasoning
    as every dice roll/reward elsewhere in this game never passing
    through the model. Answered directly, no Ollama call.
    """
    xp = character.get("xp", 0)
    level = character.get("level") or level_for_xp(xp)
    next_level_xp = XP_THRESHOLDS.get(level + 1)
    if next_level_xp is None:
        return f"You're level {level}, the maximum level in this game."
    return f"You're level {level} with {xp} XP. You need {next_level_xp - xp} more XP to reach level {level + 1}."


_SPELL_SLOT_RESTORE_QUESTION_WORDS = [
    "replenish spell slot", "restore spell slot", "recover spell slot",
    "regain spell slot", "spell slots back", "recharge spell slot",
    "refill spell slot", "get my spell slot", "get spell slots back",
    "run out of spell slot", "ran out of spell slot", "out of spell slot",
    "no spell slots left", "no more spell slot",
]


def _deterministic_spell_slot_restore_answer(character: dict | None) -> str:
    """
    Real live bug (2026-08-10, found via topic-activity monitoring):
    the SAME question ("How can i replenish spell slot in battle?")
    was asked three separate times by the same real player, and even
    with the correct fact already sitting in SUPPORT_SYSTEM_PROMPT_
    HEADER ("I rest ... heals HP and spell slots over real-world
    elapsed time"), the model still answered "casting spells again or
    activating abilities" — a real hallucination that flatly
    contradicts its own grounding, not a missing-fact problem. This is
    the exact same failure shape as the 2026-07-10 XP hallucination
    (`_deterministic_xp_answer`'s docstring): a fact handed to the
    model verbatim, still not trusted over free-form generation.
    Answered directly from the real constants, no Ollama call, so this
    specific question can never come out wrong again.

    Also folds in Coffee's own follow-up on the dev-topic screenshot
    of that wrong answer ("what can we do in battle so we can cast
    magic ... once we run out"): the real, honest answer per bot.py's
    _do_cast_spell is that cantrips (level 0) are free/unlimited and
    never cost a spell slot, so a caster who's out of slots can still
    use any cantrip their class knows, or fall back to a normal weapon
    attack -- there is no item or ability anywhere in items.py that
    restores a spell slot mid-battle, confirmed by grep.
    """
    full_rest_hours = config.NATURAL_HEALING_FULL_REST_HOURS
    base = (
        "Spell slots can't be replenished mid-battle in this game — nothing (no potion, item, or "
        "ability) restores them in combat. The only way to recover them is resting (say \"I rest\" or "
        "go quiet for a while), which heals HP and spell slots gradually over real-world elapsed time, "
        f"not instantly, capped at full after {full_rest_hours:g} hours. Once you're out of spell slots "
        "mid-battle, you can still cast any cantrip you know (cantrips are free and unlimited, no slot "
        "cost) or just attack with your weapon."
    )
    if character and character.get("char_class") == "Warlock":
        # Real Pact Magic rule (see bot.py's WARLOCK_PACT_MAGIC_REST_HOURS):
        # a Warlock's spell slots specifically recover on a short-rest-like
        # curve, ~1/8th the time everyone else's full rest takes.
        base += (
            f" Warlocks are the one exception (real Pact Magic rule): your spell slots specifically "
            f"recover much faster, on a roughly {full_rest_hours / 8:g}-hour curve instead."
        )
    return base


_ENCHANT_QUESTION_WORDS = [
    "enchant my", "enchant a", "enchant the", "how do i enchant", "how to enchant",
    "enchanting my", "enchanting a", "imbue my", "imbue a", "how do i imbue", "how to imbue",
]


def _deterministic_enchant_item_answer() -> str:
    """
    Real live bug (dev-topic screenshot, 2026-08-10, Coffee: "How do i
    enchant my weapon?" got back "Apply a suitable enchantment based on
    your stats." -- a real hallucination, not just a vague answer:
    ai/support_agent.py's catalog grounding (_crafting_catalog_text)
    only ever built from RECIPES, never ENCHANT_RECIPES, so the model
    had ZERO real grounding for this question at all and fell back to
    general D&D knowledge -- exactly what CLAUDE.md's "never answer
    ungrounded" rule exists to prevent. Coffee's own follow-up asked
    for "a step by step guide... tell the user what to type or
    examples," so this is answered directly and completely from the
    real mechanics (bot.py's _do_enchant_item), not left to a
    re-grounded but still free-form LLM call: the single easiest way to
    get this wrong is missing the ONE non-obvious real requirement (a
    plain shop-bought weapon can't be enchanted at all, only a real
    found/crafted magic item) -- a fact worth never leaving to chance.
    Guild tier ladder (2026-08-11): recipes past the base tier carry a
    real requires_guild/min_rebirth gate (rules/crafting.py's
    recipe_requirement_gate) -- included in each line below so the
    answer stays grounded instead of promising a recipe the asker can't
    actually use yet.
    """
    recipe_lines = []
    for recipe_id, recipe in ENCHANT_RECIPES.items():
        label = recipe_id.replace("enchant_", "")
        materials = ", ".join(f"{qty}x {items_module.get_item(mid)['name']}" for mid, qty in recipe["materials"].items())
        applies_to = "/".join(recipe["applies_to"])
        gate = ""
        if recipe.get("requires_guild"):
            from guilds import GUILDS
            gate += f", requires {GUILDS[recipe['requires_guild']]['name']} membership"
        if recipe.get("min_rebirth"):
            gate += f", requires rebirth #{recipe['min_rebirth']}+"
        recipe_lines.append(f"- \"{label}\" (DC {recipe['dc']} {recipe['ability']}): needs {materials}, applies to {applies_to} items{gate}")
    return (
        "Enchanting only works on a REAL found-or-crafted magic item — a plain shop-bought weapon or "
        "armor can never be enchanted, no matter what. If you don't have one yet, craft an advanced "
        "recipe (e.g. \"craft a masterwork longsword\") or find one as combat loot first.\n\n"
        "Once you have one, say something like \"enchant my [item name] with [enchantment]\" "
        "(or \"imbue\" instead of \"enchant\") — for example: \"enchant my masterwork longsword with flame\". "
        "The real enchantments are:\n" + "\n".join(recipe_lines) + "\n\n"
        "Materials are only consumed on a successful roll — a failed attempt doesn't waste them.\n\n"
        "The Enchanters' Guild runs a real 5-tier ladder from here: Journeyman wards are open to any "
        "member, Master/Grandmaster demand rebirth #1/#2, and the true Godsforged enchantment demands "
        "rebirth #3 and a Godshard (found only in a boss's remains)."
    )


_BLACKSMITH_QUESTION_WORDS = [
    "how do i blacksmith", "how to blacksmith", "what does blacksmith", "what does blacksmithing",
    "how does blacksmithing", "how does blacksmith", "what is blacksmithing", "what's blacksmithing",
]


def _deterministic_blacksmith_answer() -> str:
    """
    Real live bug (2026-08-11, topic-monitor report): "How do i
    blacksmith? And what does it do?" got back "Your STR power aids in
    shaping metal!" -- grounded in the sense that it's not a fabricated
    fact, but genuinely useless as an answer: no real command syntax,
    no materials, no examples, nothing a player could actually act on.
    _crafting_catalog_text() DOES already ground the model in every
    real blacksmithing recipe (materials, DC), same as every other
    profession -- this is a compliance failure (the model choosing
    flavor over the real facts it was given), not a missing-fact
    problem, the same shape _deterministic_spell_slot_restore_answer's
    docstring already documents for "how do I replenish spell slots".
    Answered directly from RECIPES so this specific question can never
    come out useless again, same pattern as
    _deterministic_enchant_item_answer right above.
    """
    recipe_lines = []
    for recipe_id, recipe in RECIPES.items():
        if recipe.get("profession") != "blacksmithing":
            continue
        result = items_module.get_item(recipe["result_item"])
        materials = ", ".join(
            f"{qty}x {items_module.get_item(mid)['name'] if items_module.get_item(mid) else mid}"
            for mid, qty in recipe["materials"].items()
        )
        recipe_lines.append(
            f"- {result['name'] if result else recipe_id}: needs {materials} "
            f"(DC {recipe['dc']} {recipe['ability']})"
        )
    return (
        "Blacksmithing is a real crafting profession — a Strength-based ability check that forges "
        "raw materials into a real weapon or piece of armor. Say something like \"craft [item name]\" "
        "(e.g. \"craft a longsword\") once you have the materials. The real blacksmithing recipes are:\n"
        + "\n".join(recipe_lines) + "\n\n"
        "Materials are only consumed on a successful roll — a failed attempt doesn't waste them. "
        "Fighters and Paladins get a real +2 bonus on blacksmithing checks specifically (class "
        "profession affinity). Repeated real use also earns a small, capped practiced bonus over time."
    )


_ACTIVE_CHARACTER_QUESTION_WORDS = [
    "active character", "current character", "who am i playing",
    "which character am i", "what character am i", "who is my character",
]


def _deterministic_active_character_answer(character: dict) -> str:
    """
    Per Coffee's request (2026-07-11): asking "who is my active
    character" should reliably say which character they're CURRENTLY
    playing (e.g. after switching characters and coming back an hour
    later) -- this is a single real fact with exactly one correct
    answer, same reasoning as XP-to-level above. The model answered it
    correctly in one live trial, but there's no reason to trust a small
    model on a plain lookup that's already sitting right there in the
    facts, any more than we trust it with XP.
    """
    race = character.get("race") or ""
    article = "an" if race[:1].lower() in "aeiou" else "a"
    return f"Your active character is {character.get('name')}, {article} {race} {character.get('char_class')}, level {character.get('level')}."


_INVENTORY_QUESTION_WORDS = [
    "what am i carrying", "whats in my inventory", "what's in my inventory",
    "what do i have", "check my inventory", "my backpack", "my inventory",
    "what items do i have", "what am i holding",
]


def _deterministic_inventory_answer(character: dict) -> str:
    """
    Confirmed live (2026-07-14, real player "Sugar"): "What am I
    carrying?" fell all the way through to the generic LLM path (no
    deterministic check existed for it at all), and when Ollama
    genuinely couldn't be reached in time, the player got the raw
    "couldn't reach the local model" fallback for a question that has
    exactly one correct, already-known answer -- same reasoning as
    XP-to-level and active-character above. A real inventory listing
    never needs a model call at all.
    """
    inventory = character.get("inventory") or {}
    if not inventory:
        return "Your backpack is empty right now."
    lines = []
    for item_id, qty in inventory.items():
        item = items_module.get_item(item_id)
        name = item["name"] if item else item_id
        lines.append(f"{name} x{qty}" if qty != 1 else name)
    return f"You're carrying: {', '.join(lines)}."


def _build_party_facts(party_members: list[dict]) -> str:
    """Real, current facts about who's actually in the party -- never invented."""
    if not party_members:
        return "\n\nREAL FACTS ABOUT THE PLAYER'S PARTY: not currently in a formed party with anyone."
    lines = ["\n\nREAL FACTS ABOUT THE PLAYER'S PARTY (answer party questions using ONLY this, "
             "never invent or guess a member who isn't listed):"]
    for member in party_members:
        kind = "AI companion" if member.get("is_ai") else "player character"
        lines.append(
            f"- {member.get('name')} ({kind}): a {member.get('race')} {member.get('char_class')}, "
            f"level {member.get('level')}, HP {member.get('hp_current')}/{member.get('hp_max')}"
        )
    return "\n".join(lines)


_PARTY_QUESTION_WORDS = [
    "in my party", "in our party", "in my current party", "in the party",
    "party character sheet", "party sheets", "who's in my party", "whos in my party",
    "who is in my party", "party members", "my party members",
]


def _deterministic_party_answer(party_members: list[dict], question: str) -> str | None:
    """
    Confirmed live (2026-07-14, Coffee): asked "Is Sarah in my current
    party?" and "Show me my party character sheets" with only the
    asking player's OWN character ever passed to this module -- the
    model had zero real party data to answer from, so it either
    answered about the player's own sheet only or admitted (correctly,
    given what it was given) that it didn't know. Same reasoning as
    every other deterministic answer here: a party roster is a real,
    fixed lookup, not something a model should generate.
    """
    lowered = question.lower()
    if not any(w in lowered for w in _PARTY_QUESTION_WORDS):
        return None

    if not party_members:
        return "You're not currently in a formed party with anyone."

    # "Is <name> in my party?" -- direct yes/no if a specific name is asked about.
    named = [m for m in party_members if m.get("name") and m["name"].lower() in lowered]
    if named and ("is " in lowered or "in my" in lowered) and len(lowered.split()) < 12:
        member = named[0]
        kind = "an AI companion" if member.get("is_ai") else "a party member"
        return (
            f"Yes, {member['name']} is {kind} in your current party -- "
            f"a {member.get('race')} {member.get('char_class')}, level {member.get('level')}, "
            f"HP {member.get('hp_current')}/{member.get('hp_max')}."
        )

    lines = [f"Your party ({len(party_members)}):"]
    for member in party_members:
        kind = "AI companion" if member.get("is_ai") else "player character"
        lines.append(
            f"- {member.get('name')} ({kind}): {member.get('race')} {member.get('char_class')}, "
            f"level {member.get('level')}, HP {member.get('hp_current')}/{member.get('hp_max')}"
        )
    return "\n".join(lines)


_LOCATION_CONNECTION_QUESTION_WORDS = ["connects to", "connect to", "what connects", "leads to", "lead to"]


def _deterministic_location_connections_answer(character: dict, question: str) -> str | None:
    """
    Real live bug (2026-08-08, game-wiki expansion): "What connects to
    Market Row?" got the nonsensical model answer "Market Row connects
    directly to Market Row." even with the correct connection data
    right there in its prompt -- same class of failure as the XP/active-
    character/inventory hallucinations above (a small model given a
    correct fact and still not relaying it right). This has exactly one
    well-defined correct answer per real, already-visited location (its
    real `connections`/descends_to/ascends_to fields), so it's answered
    directly here instead of trusting free-form generation, same
    reasoning as every other deterministic answer in this file. Returns
    None (falls through to the LLM) if no visited location's name
    appears in the question, so this never blocks a genuinely different
    kind of question that happens to contain "connects".
    """
    lowered = question.lower()
    if not any(w in lowered for w in _LOCATION_CONNECTION_QUESTION_WORDS):
        return None
    visited = character.get("visited_locations") or []
    candidates = []
    for loc_id in visited:
        loc = cl.get_location(CAMPAIGN, loc_id)
        if loc and loc["name"].lower() in lowered:
            candidates.append(loc)
    if not candidates:
        return None
    # Longest matching name wins, same "most specific match" convention
    # used by _match_member_by_name_or_username, so a shorter location
    # name never wrongly wins over one that contains it.
    loc = max(candidates, key=lambda l: len(l["name"]))
    connections = list(loc.get("connections", []))
    for extra in ("descends_to", "ascends_to"):
        if loc.get(extra):
            connections.append(loc[extra])
    connection_names = []
    for c in connections:
        connected_loc = cl.get_location(CAMPAIGN, c)
        if connected_loc:
            connection_names.append(connected_loc["name"])
    if not connection_names:
        return f"{loc['name']} doesn't connect anywhere else you've found yet."
    return f"{loc['name']} connects to: {', '.join(connection_names)}."


_ITEM_COMPARISON_RE = re.compile(
    r"(?:which(?:'s| is) better,?\s+(.+?)\s+or\s+(.+?)\??$)"
    r"|(?:(.+?)\s+or\s+(.+?),?\s+which(?:'s| is) better\??$)"
    r"|(?:is\s+(.+?)\s+or\s+(.+?)\s+better\??$)"
    r"|(?:(.+?)\s+(?:vs\.?|versus)\s+(.+?)\??$)",
    re.IGNORECASE,
)
_ITEM_PHRASE_STOPWORDS = {"a", "an", "the", "my", "your", "our"}


def _strip_item_phrase(phrase: str) -> str:
    words = [w for w in phrase.strip().split() if w.lower() not in _ITEM_PHRASE_STOPWORDS]
    return " ".join(words)


def _deterministic_item_comparison_answer(question: str, character: dict | None = None) -> str | None:
    """
    Real live bug (2026-08-13, topic-activity monitoring): "Which is
    better serviceable dagger or silvered dagger?" got a hallucinated
    answer inventing a "serviceable dagger" that doesn't exist anywhere
    in items.ITEMS -- CRITICAL_GROUNDING_RULE already tells the model
    never to do this, but a comparison question naming a nonexistent
    item alongside a real one is exactly the shape that got past it
    live, the same class of failure prompt-only grounding has already
    proven insufficient for elsewhere in this file (see the mage_hand
    cantrip override above). Detects a "which is better X or Y"/"X or
    Y, which is better"/"is X or Y better"/"X vs Y" comparison,
    resolves each side against the REAL item catalog, and
    short-circuits with a grounded correction the moment either side
    doesn't match a real item -- never reaching the model at all for
    exactly the question shape that hallucinated live. Returns None
    (falls through to the LLM, which can compare two names it already
    confirmed are both real using the grounded item catalog already in
    its prompt) when the question isn't this shape, or when both sides
    resolve to real items.

    Real live bug, found investigating the ABOVE fix's own accuracy
    (2026-08-13, dev-bridge screenshot: "This weapon was not equipped
    and I was supposed to use my serviceable dagger in the attack"):
    the asking player (character_id 22, Laurienna) genuinely owned a
    real generated "Serviceable Dagger" (instance id gi3, a common-tier
    roll of rules/item_generator.py's real "Serviceable" prefix) --
    this function's first version called find_item_mentioned_in_text
    with NO candidate_ids, so its search_space defaulted to
    items.ITEMS's keys only, which can never contain a per-instance
    generated item's id ("gi<n>"). The exact grounding fix meant to
    stop Support inventing FAKE items was therefore itself telling this
    player their own REAL item didn't exist. Passing `character` (every
    real call site already has one -- see bot.py's 3 call sites) lets a
    real owned generated item resolve too, by adding the character's
    own inventory ids as extra candidates alongside every static item
    (so an unowned real item can still be named and compared, exactly
    as before).
    """
    match = _ITEM_COMPARISON_RE.search(question.strip())
    if not match:
        return None
    groups = [g for g in match.groups() if g is not None]
    if len(groups) != 2:
        return None
    phrase_a, phrase_b = (_strip_item_phrase(g) for g in groups)
    if not phrase_a or not phrase_b:
        return None
    owned_ids = list(character.get("inventory", {}).keys()) if character else []
    candidate_ids = list(items_module.ITEMS.keys()) + [i for i in owned_ids if i not in items_module.ITEMS]
    item_a = items_module.find_item_mentioned_in_text(phrase_a, candidate_ids=candidate_ids)
    item_b = items_module.find_item_mentioned_in_text(phrase_b, candidate_ids=candidate_ids)
    if item_a and item_b:
        return None
    real_names = [items_module.get_item(i)["name"] for i in (item_a, item_b) if i]
    missing = [p for p, i in ((phrase_a, item_a), (phrase_b, item_b)) if not i]
    missing_text = " or ".join(f'"{m}"' for m in missing)
    if real_names:
        return (
            f"There's no {missing_text} in this game — only {' and '.join(real_names)} is real. "
            f"Nothing to compare it against."
        )
    return f"Neither {missing_text} exists in this game — not real items here."


_QUEST_TASK_QUESTION_WORDS = [
    "next quest", "quest task", "what's my quest", "whats my quest",
    "current quest", "my objective", "what am i supposed to do", "what do i need to do",
]


def _deterministic_quest_task_answer(character: dict) -> str | None:
    """
    Real gap found alongside the location-connections bug above
    (2026-08-08): the LLM path answered "What's my next quest task?"
    with just the bare title ("A Favor for Grimsby"), dropping the
    actual task description -- technically not wrong, but not useful
    either, since the title alone doesn't say what to DO. The real
    title+description is already known, fixed, already-shown-to-the-
    player data (see _build_character_facts) -- no reason to let free-
    form generation risk dropping half of it. Returns None (falls
    through to the LLM) if the character has no active story quests
    with real campaign.json entries, so an edge case still gets a
    real conversational answer rather than nothing.
    """
    active_quests = character.get("active_quests") or {}
    if not active_quests:
        return "You don't have any active story quests right now."
    lines = []
    for quest_id in active_quests:
        quest = CAMPAIGN.get("quests", {}).get(quest_id)
        if quest:
            lines.append(f"{quest['title']} — {quest['description']}")
    if not lines:
        return None
    label = "Your active quest task" + ("s" if len(lines) > 1 else "") + ":"
    return f"{label} {'; '.join(lines)}"


def _extract_six_rolls(question: str) -> list[int] | None:
    numbers = [int(n) for n in re.findall(r"\d+", question)]
    return numbers if len(numbers) == 6 else None


def _deterministic_stat_assignment_answer(character: dict, rolls: list[int]) -> str | None:
    """
    Confirmed live (2026-07-11): asked to help assign rolled stats for an
    existing Elf Ranger, the model ignored the character's real race and
    class entirely and suggested switching to Fighter or Barbarian instead
    -- and once the prompt was made big enough to ground this properly
    (adding races.py's real ability bonuses), the same question started
    consistently exceeding even a 280s timeout with zero output, three
    times running. Like XP-to-level, this has one well-defined correct
    answer (map the highest rolls to this class's standard 5E priority
    stats, then apply the character's real racial bonuses) -- there's no
    reason to pay for a slow, unreliable model call for something this
    deterministic. Returns None if the class isn't recognized (falls back
    to the LLM path in that case).
    """
    char_class = character.get("char_class")
    priority = _CLASS_PRIORITY_STATS.get(char_class)
    if priority is None:
        return None

    remaining_slots = [a for a in _ALL_ABILITIES if a not in priority]
    slot_order = priority + remaining_slots
    sorted_rolls = sorted(rolls, reverse=True)
    assignment = dict(zip(slot_order, sorted_rolls))

    race = character.get("race") or ""
    bonuses = races_module.get_race(race)
    bonuses = bonuses["ability_bonuses"] if bonuses else {}

    article = "an" if race[:1].lower() in "aeiou" else "a"
    lines = [f"For {article} {race} {char_class}, here's how I'd assign {sorted_rolls}:"]
    for ability in _ALL_ABILITIES:
        base = assignment[ability]
        bonus = bonuses.get(ability, 0)
        final = base + bonus
        bonus_note = f" (+{bonus} {race} bonus = {final})" if bonus else ""
        lines.append(f"- {_ABILITY_ABBREV[ability]}: {base}{bonus_note}")
    lines.append(
        f"Highest rolls go to {', '.join(_ABILITY_ABBREV[a] for a in priority)} "
        f"since those matter most for a {char_class}."
    )
    return "\n".join(lines)


def _build_location_reference(visited_location_ids: list[str]) -> str:
    """
    Game-wiki expansion (2026-08-08, per Coffee: Support should answer
    location/world questions like an encyclopedia). Gated to locations
    the player has ALREADY visited -- the exact same name/description/
    connections text "look around" already showed them in Adventure
    (see bot.py's location-arrival narration, which prints
    location['description'] verbatim), so nothing here is new
    information ahead of discovery. A location the player hasn't found
    yet is never included, deliberately -- Support must not preview
    unvisited content.
    """
    # Real bug caught before shipping (2026-08-08): campaign.json nests
    # locations by layer (surface/underground/sky) first, so a plain
    # CAMPAIGN["locations"].get(loc_id) always misses -- confirmed live,
    # this returned only the 3 layer keys instead of 82 real locations.
    # cl.get_location already handles the layer flattening correctly
    # (same helper bot.py uses everywhere else); reused here rather than
    # re-deriving the lookup.
    lines = []
    for loc_id in visited_location_ids:
        loc = cl.get_location(CAMPAIGN, loc_id)
        if not loc:
            continue
        connections = list(loc.get("connections", []))
        for extra in ("descends_to", "ascends_to"):
            if loc.get(extra):
                connections.append(loc[extra])
        connection_names = []
        for c in connections:
            connected_loc = cl.get_location(CAMPAIGN, c)
            if connected_loc:
                connection_names.append(connected_loc["name"])
        conn_text = f" Connects to: {', '.join(connection_names)}." if connection_names else ""
        lines.append(f"- {loc['name']}: {loc['description']}{conn_text}")
    if not lines:
        return ""
    return "\n\nREAL LOCATIONS THE PLAYER HAS ALREADY VISITED (pure world/wiki facts -- never " \
        "attach plot significance or hint at why anything matters beyond what's stated here; " \
        "never mention or describe ANY location not in this list, visited or not):\n" + "\n".join(lines)


def _build_npc_reference(visited_location_ids: list[str]) -> str:
    """
    Companion to _build_location_reference -- who's who at places the
    player has actually been. Only name/role/personality/disposition:
    real character-DESCRIPTION facts, the same "what are they like"
    info a player could glean from meeting them. Deliberately excludes
    each NPC's real 'goals'/'gives_quests' campaign.json fields, since
    those are quest-hook/plot-adjacent by nature (memory: "NPC role/
    class/personality, never backstory").
    """
    visited_npc_ids = []
    seen = set()
    for loc_id in visited_location_ids:
        loc = cl.get_location(CAMPAIGN, loc_id)
        if not loc:
            continue
        for npc_id in loc.get("npcs", []):
            if npc_id not in seen:
                seen.add(npc_id)
                visited_npc_ids.append(npc_id)

    lines = []
    for npc_id in visited_npc_ids:
        npc = cl.get_npc(CAMPAIGN, npc_id)
        if not npc:
            continue
        role = (npc.get("role") or "").replace("_", " ")
        char_class = npc.get("stats", {}).get("char_class")
        role_text = f"{role}, a {char_class}" if char_class else role
        lines.append(
            f"- {npc['name']} ({role_text}): {npc.get('personality', '')} "
            f"(disposition: {npc.get('disposition', 'unknown')})"
        )
    if not lines:
        return ""
    return "\n\nREAL NPCS THE PLAYER HAS ALREADY MET (their role and general character only -- " \
        "never their personal history, motives, or secrets, even if you happen to know them; " \
        "never mention an NPC not in this list):\n" + "\n".join(lines)


def _build_prompt(question: str, character: dict | None = None, party_members: list[dict] | None = None) -> str:
    prompt = _build_support_system_prompt(question)
    if character:
        prompt += _build_character_facts(character)
        visited = character.get("visited_locations") or []
        prompt += _build_location_reference(visited)
        prompt += _build_npc_reference(visited)
    if party_members is not None:
        prompt += _build_party_facts(party_members)
    return f"{prompt}\n\nPlayer question: {question}\nAnswer:"


def _correct_own_class_hallucination(text: str, character: dict) -> str:
    """
    Confirmed live (2026-07-17, task #153): a real Warlock ("Ravenloft")
    asked whether casting Fire Bolt fit their kit and got told "It aligns
    with your Wizard class ability" -- the yes/no judgment was right, but
    the class name was a flat hallucination, even though the correct
    class was already given verbatim in the prompt. Same failure mode as
    the XP hallucination above (a small model overriding a fact it was
    just handed), just for a class name instead of a number. There's
    exactly one correct class for "your ... class" phrasing about THIS
    character, so any other class name there is always wrong -- safe to
    deterministically correct rather than re-prompt. Only rewrites
    possessive "your <class>" phrasing, so a legitimate answer that
    discusses a different class in general (e.g. comparing builds) is
    left alone.
    """
    real_class = character.get("char_class")
    if not real_class:
        return text
    for other_class in VALID_CLASSES:
        if other_class.lower() == real_class.lower():
            continue
        text = re.sub(rf"\byour {other_class}\b", f"your {real_class}", text, flags=re.IGNORECASE)
    return text


def answer_support_question(
    question: str, character: dict | None = None, party_members: list[dict] | None = None
) -> str:
    """
    Send a player's how-to-play (or, if `character` is given, their own
    character-specific) question to the narration model and return its
    reply. Falls back to a short static pointer if Ollama is unreachable.
    `party_members`, if given, is this player's real, current party
    roster (see bot.py's _get_party_members) -- grounds both the
    deterministic party-roster answer below and the general LLM prompt.
    """
    lowered = question.lower()
    if any(w in lowered for w in _SPELL_SLOT_RESTORE_QUESTION_WORDS):
        return _deterministic_spell_slot_restore_answer(character)
    if any(w in lowered for w in _ENCHANT_QUESTION_WORDS):
        return _deterministic_enchant_item_answer()
    if any(w in lowered for w in _BLACKSMITH_QUESTION_WORDS):
        return _deterministic_blacksmith_answer()
    if character and any(w in lowered for w in _XP_QUESTION_WORDS):
        return _deterministic_xp_answer(character)
    if character and any(w in lowered for w in _ACTIVE_CHARACTER_QUESTION_WORDS):
        return _deterministic_active_character_answer(character)
    if not character and any(w in lowered for w in _ACTIVE_CHARACTER_QUESTION_WORDS):
        return "You don't have an active character yet — say \"I want to create a character\" in Adventure to get started."
    if character and any(w in lowered for w in _INVENTORY_QUESTION_WORDS):
        return _deterministic_inventory_answer(character)
    if not character and any(w in lowered for w in _INVENTORY_QUESTION_WORDS):
        return "You don't have an active character yet — say \"I want to create a character\" in Adventure to get started."
    if character:
        location_answer = _deterministic_location_connections_answer(character, question)
        if location_answer is not None:
            return location_answer
    comparison_answer = _deterministic_item_comparison_answer(question, character)
    if comparison_answer is not None:
        return comparison_answer
    if character and any(w in lowered for w in _QUEST_TASK_QUESTION_WORDS):
        quest_answer = _deterministic_quest_task_answer(character)
        if quest_answer is not None:
            return quest_answer
    if character and "assign" in lowered:
        rolls = _extract_six_rolls(question)
        if rolls is not None:
            answer = _deterministic_stat_assignment_answer(character, rolls)
            if answer is not None:
                return answer
    if party_members is not None:
        party_answer = _deterministic_party_answer(party_members, question)
        if party_answer is not None:
            return party_answer

    prompt = _build_prompt(question, character, party_members)

    # Real live bug (dev-topic screenshot, 2026-07-18): two genuine
    # player questions ("how do I start a battle?", "How do I join
    # Ravenloft on his quest") both hit the static apology below and
    # were NEVER actually answered -- the old 2-attempt loop (confirmed
    # live 2026-07-12 to help with transient contention) still gives up
    # far too easily under this box's real, documented 30-160s+ single-
    # Ollama-slot contention (see CLAUDE.md). Per Coffee (2026-07-18):
    # "The support topic is incredibly important and it needs to
    # function as a usable wiki for the players" + "if it takes time to
    # process a support message - tell them - then do it" -- i.e. a
    # slow answer is fine, but a DROPPED one is not. Bumped from 2 to 5
    # attempts with backoff (5s/10s/20s/30s) so a real transient busy
    # period (this box routinely queues the live bot's own narration
    # calls behind a Support question on the same shared model slot)
    # gets enough real chances to clear before giving up -- worst case
    # ~1065s (~18min) of retrying, vs. giving up for good after ~405s.
    delays = [5, 10, 20, 30]
    attempts = len(delays) + 1
    global _support_call_active
    _support_call_active = True
    try:
        for attempt in range(attempts):
            try:
                response = requests.post(
                    f"{config.OLLAMA_BASE_URL}/api/generate",
                    json={
                        "model": config.DM_NARRATION_MODEL,
                        "prompt": prompt,
                        "stream": False,
                        # Real live bug (2026-08-10, Coffee: "How do i
                        # enchant my weapon?" still failed after the
                        # world-tick priority fix): journalctl -u ollama
                        # showed all 5 attempts got a real HTTP 200 in a
                        # normal ~25-45s each -- Ollama was healthy and
                        # answering every time. The actual failure was
                        # silent: strip_think_tags() left an EMPTY
                        # string on every attempt, because 600 tokens
                        # (half of ai/dm_agent.py's proven 1200) wasn't
                        # enough for this thinking model to finish
                        # reasoning before ever emitting a real answer --
                        # see _UNCLOSED_THINK_RE's own docstring: a
                        # generation cut off mid-<think> strips to
                        # nothing. Matched to dm_agent's real working
                        # budget instead of a narrower guess.
                        "options": {"num_predict": 1200},
                    },
                    timeout=200,
                )
                response.raise_for_status()
                data = response.json()
                text = strip_think_tags(data.get("response", ""))
                if text:
                    if character:
                        text = _correct_own_class_hallucination(text, character)
                    return text
                # Real response, but nothing usable survived stripping
                # <think> tags -- the model spent its whole token budget
                # reasoning and never got to a real answer. This used to
                # be entirely silent (no log, no backoff, straight to
                # the next attempt) which is exactly what made this bug
                # invisible through every prior investigation.
                logger.warning(
                    f"[support_agent] attempt {attempt + 1}/{attempts} got a real response with no "
                    f"usable text after stripping <think> tags (raw len={len(data.get('response', ''))})"
                )
                if attempt < len(delays):
                    time.sleep(delays[attempt])
            except (requests.RequestException, ValueError) as e:
                logger.warning(f"[support_agent] model call failed (attempt {attempt + 1}/{attempts}): {e!r}")
                if attempt < len(delays):
                    time.sleep(delays[attempt])
    finally:
        _support_call_active = False

    return (
        "Pandora AI is genuinely overloaded right now and couldn't get "
        "an answer through after several real attempts — this is rare. "
        "Please ask again, or ping an admin to run /redo on this exact "
        "message so it doesn't need to be retyped."
    )
