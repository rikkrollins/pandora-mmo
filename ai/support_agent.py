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
- "I head to the [place]" -> travels to a connected location
- "What am I carrying?" -> shows backpack contents
- "I want to buy [item]" -> purchases from a shop, only if standing in a \
location that has one
- "sell my [item]" -> sells an item from the backpack for half its value
- "Let's start a fight" / "I attack the goblin" -> begins or continues combat
- "I cast [spell name]" -> casts a spell the character actually knows
- "I want to join the [guild name]" -> requests guild membership, subject to \
level/class requirements
- Saying an NPC's name -> starts an in-character conversation with them
- "cancel" / "stop" / "reset" -> exits out of character creation or any \
other multi-step process, at any time
- "/sheet" or asking about stats/HP/level -> shows the character sheet"""

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


def _build_prompt(question: str, character: dict | None = None) -> str:
    prompt = SUPPORT_SYSTEM_PROMPT
    if character:
        prompt += _build_character_facts(character)
    return f"{prompt}\n\nPlayer question: {question}\nAnswer:"


def answer_support_question(question: str, character: dict | None = None) -> str:
    """
    Send a player's how-to-play (or, if `character` is given, their own
    character-specific) question to the narration model and return its
    reply. Falls back to a short static pointer if Ollama is unreachable.
    """
    lowered = question.lower()
    if character and any(w in lowered for w in _XP_QUESTION_WORDS):
        return _deterministic_xp_answer(character)
    if character and any(w in lowered for w in _ACTIVE_CHARACTER_QUESTION_WORDS):
        return _deterministic_active_character_answer(character)
    if not character and any(w in lowered for w in _ACTIVE_CHARACTER_QUESTION_WORDS):
        return "You don't have an active character yet — say \"I want to create a character\" in Adventure to get started."
    if character and "assign" in lowered:
        rolls = _extract_six_rolls(question)
        if rolls is not None:
            answer = _deterministic_stat_assignment_answer(character, rolls)
            if answer is not None:
                return answer

    prompt = _build_prompt(question, character)

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
        "I couldn't reach the local model just now. In the meantime: just "
        "talk naturally in Adventure — try 'I want to create a character' "
        "to get started, or check the pinned message for more examples."
    )
