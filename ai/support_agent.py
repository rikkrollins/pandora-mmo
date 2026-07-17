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
import re
import time

import requests

import config
import items as items_module
import races as races_module
import spells as spells_module
from ai.text_cleanup import strip_think_tags
from guilds import GUILDS
from rules.crafting import RECIPES
from rules.leveling import XP_THRESHOLDS, level_for_xp

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


def _build_catalog_reference() -> str:
    """Builds the real, current item/spell/guild catalog text to ground the model."""
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

    lines.append("\nREAL SPELLS IN THIS GAME, BY CLASS:")
    for cls in spells_module.CLASS_SPELL_LISTS:
        cantrip_ids = spells_module.CLASS_CANTRIPS.get(cls, [])
        leveled_ids = spells_module.CLASS_SPELL_LISTS.get(cls, [])
        spell_descriptions = []
        for sid in cantrip_ids:
            spell = spells_module.get_spell(sid)
            spell_descriptions.append(f"{spell['name']} (cantrip, {spell.get('effect', '')})")
        for sid in leveled_ids:
            spell = spells_module.get_spell(sid)
            spell_descriptions.append(f"{spell['name']} ({spell.get('effect', '')})")
        lines.append(f"- {cls.title()}: {', '.join(spell_descriptions)}")

    lines.append("\nREAL GUILDS IN THIS GAME:")
    for guild_id, guild in GUILDS.items():
        lines.append(
            f"- {guild['name']}: requires level {guild['join_requirement_level']}"
            + (f", classes: {', '.join(guild['join_requirement_classes'])}"
               if guild.get("join_requirement_classes") else "")
        )

    # Per Coffee (2026-07-14): Support should be "like an encyclopedia
    # or wiki" -- crafting recipes and the real gathering skills were
    # never grounded here at all, so a question like "what can I craft"
    # had nothing real to answer from.
    lines.append("\nREAL CRAFTING RECIPES IN THIS GAME:")
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

    # Confirmed live (2026-07-11): asked to help assign rolled stats for an
    # Elf Ranger, the model ignored the character's actual race/class
    # entirely and suggested switching to Fighter or Barbarian instead --
    # because this game's real per-race ability bonuses were never in its
    # prompt at all, so it had nothing concrete to reason from. These are
    # this project's own real bonuses (some 5E subraces/variants are
    # simplified here), not to be confused with generic D&D lore.
    lines.append("\nREAL RACE ABILITY SCORE BONUSES IN THIS GAME:")
    for race_name, race_data in races_module.RACES.items():
        bonus_text = ", ".join(
            f"+{v} {k}" for k, v in race_data["ability_bonuses"].items()
        )
        lines.append(f"- {race_name}: {bonus_text}")

    return "\n".join(lines)


SUPPORT_SYSTEM_PROMPT = (
    SUPPORT_SYSTEM_PROMPT_HEADER
    + _build_catalog_reference()
    + CRITICAL_GROUNDING_RULE
    + """

Do not reveal or speculate about story content, plot, hidden areas, or what \
the "great evil" or threat in the world might be. Do not give hints, \
suggestions, or strategy advice about where to go, what to do next, or how \
to approach any in-world challenge or mystery — if asked something like \
that, say that's part of what they'll discover by playing. You MAY answer \
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
    xp_to_next = f"{next_level_xp - xp} XP" if next_level_xp is not None else "already at max level (20)"

    known_spells = character.get("known_spells") or []
    active_quests = character.get("active_quests") or {}

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
        f"- Active quests: {', '.join(active_quests.keys()) if active_quests else 'none'}"
    )


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
    Confirmed live (2026-07-14, Coffee): asked "Is Sera in my current
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


def _build_prompt(question: str, character: dict | None = None, party_members: list[dict] | None = None) -> str:
    prompt = SUPPORT_SYSTEM_PROMPT
    if character:
        prompt += _build_character_facts(character)
    if party_members is not None:
        prompt += _build_party_facts(party_members)
    return f"{prompt}\n\nPlayer question: {question}\nAnswer:"


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

    # Confirmed live 2026-07-12: a Support question ("What can I use
    # silverleaf herb for?") hit the static fallback below on the very
    # first attempt, with no retry at all -- under real system load
    # (this CPU-only box's single Ollama model genuinely can queue up
    # behind other in-flight calls), one attempt timing out doesn't mean
    # the model is actually unreachable, just busy. One retry after a
    # short pause catches that transient case without meaningfully
    # changing the worst-case wait for a genuinely dead model.
    for attempt in range(2):
        try:
            response = requests.post(
                f"{config.OLLAMA_BASE_URL}/api/generate",
                json={
                    "model": config.DM_NARRATION_MODEL,
                    "prompt": prompt,
                    "stream": False,
                },
                timeout=200,
            )
            response.raise_for_status()
            data = response.json()
            text = strip_think_tags(data.get("response", ""))
            if text:
                return text
        except (requests.RequestException, ValueError) as e:
            print(f"[support_agent] model call failed (attempt {attempt + 1}/2): {e}")
            if attempt == 0:
                time.sleep(5)

    return (
        "The game's local AI is busy right now and couldn't answer that in "
        "time — this happens under heavy load, not because anything's "
        "broken. Please try asking again in a moment."
    )
