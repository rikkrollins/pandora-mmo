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
import requests

import config
import items as items_module
import spells as spells_module
from ai.text_cleanup import strip_think_tags
from guilds import GUILDS

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

    return "\n".join(lines)


SUPPORT_SYSTEM_PROMPT = (
    SUPPORT_SYSTEM_PROMPT_HEADER
    + _build_catalog_reference()
    + CRITICAL_GROUNDING_RULE
    + """

Do not reveal or speculate about story content, plot, hidden areas, or what \
the "great evil" or threat in the world might be. Do not give hints, \
suggestions, or strategy advice about how to solve anything, where to go, \
what to do next, or how to approach a challenge — if asked something like \
that, say that's part of what they'll discover by playing, and that you \
can only help with how the game's mechanics and interface work, not what \
to do with them. Keep answers short and friendly."""
)


def _build_prompt(question: str) -> str:
    return f"{SUPPORT_SYSTEM_PROMPT}\n\nPlayer question: {question}\nAnswer:"


def answer_support_question(question: str) -> str:
    """
    Send a player's how-to-play question to the narration model and
    return its reply. Falls back to a short static pointer if Ollama
    is unreachable.
    """
    prompt = _build_prompt(question)

    try:
        response = requests.post(
            f"{config.OLLAMA_BASE_URL}/api/generate",
            json={
                "model": config.DM_NARRATION_MODEL,
                "prompt": prompt,
                "stream": False,
            },
            timeout=120,
        )
        response.raise_for_status()
        data = response.json()
        text = strip_think_tags(data.get("response", ""))
        if text:
            return text
    except (requests.RequestException, ValueError) as e:
        print(f"[support_agent] model call failed: {e}")

    return (
        "I couldn't reach the local model just now. In the meantime: just "
        "talk naturally in Adventure — try 'I want to create a character' "
        "to get started, or check the pinned message for more examples."
    )
