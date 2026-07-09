"""
ai/intent_parser.py
Turns free-flowing player text ("I swing my sword at the goblin!") into
a structured intent dict the bot can act on, WITHOUT ever deciding game
outcomes itself. This module only classifies what the player is trying
to do — the actual dice/damage/HP math still happens entirely in
rules/combat.py and rules/dice.py, exactly as before.

If the model is unreachable or returns something unparseable, a plain
keyword-based fallback takes over so the game never hard-fails just
because a natural-language interpretation didn't come back cleanly.
"""
import json
import re

import requests

import config
from ai.text_cleanup import strip_think_tags

INTENT_SYSTEM_PROMPT = """You are an intent classifier for a text-based D&D 5E game. \
Given a player's free-text message and some context, output ONLY a JSON object \
(no other text, no markdown fences) with this shape:

{"action": "<one of: attack, pass_turn, start_combat, create_character, check_sheet, \
talk_npc, move, look, check_inventory, check_party, buy, sell, cast_spell, join_guild, \
recruit_npc, rest, skill_check, shove, chat>", \
"ability": "<one of: strength, dexterity, constitution, intelligence, wisdom, charisma, or null>", \
"target": "<name/place/item mentioned, or null>", "npc_name": "<npc name if talking to one, or null>", \
"item_name": "<item mentioned for buy/sell, or null>", "spell_name": "<spell mentioned for cast_spell, or null>", \
"quantity": "<number mentioned for buy/sell, default 1>", \
"raw_text": "<the original message, verbatim, for narration flavor>"}

Rules:
- "attack" is for any offensive action aimed at an enemy (attack, swing, shoot, cast at, strike).
- "talk_npc" is for addressing a specific named NPC conversationally.
- "start_combat" is when a player wants to begin a fight or encounter.
- "create_character" is when a player wants to make/join with a new character.
- "check_sheet" is for asking about their own stats/HP/level (not items).
- "check_inventory" is for asking what's in their backpack/bag/items they're carrying.
- "check_party" is for asking who's in the party, how many members, or who's adventuring with them.
- "recruit_npc" is for asking a specific named NPC to join their party / travel with them / come along.
- "rest" is for resting, recovering, healing up outside combat, or asking to be revived/healed after being downed.
- "skill_check" is for any risky non-combat action with uncertain outcome that isn't covered above: sneaking, \
persuading, climbing, searching, lifting, recalling lore, perceiving hidden things, resisting an effect, etc. \
Set "ability" to whichever of the 6 abilities best fits the action (dexterity for sneaking/climbing, strength \
for lifting/breaking, intelligence for recalling lore, wisdom for perceiving/insight, charisma for \
persuading/deceiving, constitution for enduring/resisting).
- "shove" is specifically for trying to knock an enemy down/prone (shoving, tackling, tripping).
- "move" is for traveling, walking, heading to, entering, or descending/ascending to a place.
- "look" is for looking around, examining the current area, or asking where they are.
- "buy" is for purchasing something from a shop or merchant.
- "sell" is for selling something they're carrying.
- "cast_spell" is for casting/using a named spell.
- "join_guild" is for joining/asking to join a specific guild or order.
- "pass_turn" is for skipping, waiting, or passing.
- "chat" is for anything else — general roleplay talk with no clear game action.
Output ONLY the JSON object, nothing else."""


def _extract_json(text: str) -> dict | None:
    """Best-effort extraction of a JSON object from model output."""
    text = text.strip()
    # Strip markdown code fences if the model added them anyway.
    text = re.sub(r"^```(?:json)?", "", text).strip()
    text = re.sub(r"```$", "", text).strip()
    try:
        return json.loads(text)
    except (ValueError, json.JSONDecodeError):
        pass
    # Try to find the first {...} block if there's extra prose around it.
    match = re.search(r"\{.*\}", text, re.DOTALL)
    if match:
        try:
            return json.loads(match.group(0))
        except (ValueError, json.JSONDecodeError):
            return None
    return None


def _keyword_fallback(text: str, known_npc_names: list[str]) -> dict:
    """
    Plain keyword-based classification used when the model is unreachable
    or returns something we can't parse. Deliberately simple and
    conservative — when in doubt, classify as 'chat' rather than guess
    at a game action.
    """
    lowered = text.lower()
    base = {"action": "chat", "target": None, "npc_name": None, "ability": None,
            "item_name": None, "spell_name": None, "quantity": 1, "raw_text": text}

    recruit_words = ["join us", "join our party", "join my party", "come with us",
                      "travel with us", "come along", "join the party"]
    for npc_name in known_npc_names:
        if npc_name.lower() in lowered:
            if any(w in lowered for w in recruit_words):
                return {**base, "action": "recruit_npc", "npc_name": npc_name}
            return {**base, "action": "talk_npc", "npc_name": npc_name}

    attack_words = ["attack", "swing", "shoot", "strike", "hit", "stab", "cast at", "fire at"]
    if any(w in lowered for w in attack_words):
        return {**base, "action": "attack"}

    combat_start_words = [
        "start combat", "begin fight", "let's fight", "lets fight", "encounter",
        "start a fight", "begin combat", "fight some", "fight the",
    ]
    if any(w in lowered for w in combat_start_words):
        return {**base, "action": "start_combat"}

    if any(w in lowered for w in ["create a character", "make a character", "new character", "join the game"]):
        return {**base, "action": "create_character"}

    if any(w in lowered for w in ["my backpack", "my bag", "my inventory", "what am i carrying", "what do i have"]):
        return {**base, "action": "check_inventory"}

    if any(w in lowered for w in ["who's in my party", "whos in my party", "my party", "who is with me", "who's with me"]):
        return {**base, "action": "check_party"}

    if any(w in lowered for w in ["my sheet", "my stats", "my hp", "my health", "my character"]):
        return {**base, "action": "check_sheet"}

    move_words = ["go to", "head to", "walk to", "travel to", "move to", "enter the", "descend", "ascend", "climb down", "climb up"]
    if any(w in lowered for w in move_words):
        return {**base, "action": "move"}

    if any(w in lowered for w in ["look around", "where am i", "look at my surroundings", "examine the area", "describe this place"]):
        return {**base, "action": "look"}

    if any(w in lowered for w in ["buy", "purchase"]):
        return {**base, "action": "buy"}

    if lowered.startswith("sell") or " sell " in lowered:
        return {**base, "action": "sell"}

    if any(w in lowered for w in ["cast ", "i cast"]):
        return {**base, "action": "cast_spell"}

    if any(w in lowered for w in ["join the", "i want to join", "become a member of"]):
        return {**base, "action": "join_guild"}

    pass_words = ["pass", "skip my turn", "i wait", "i'll wait", "ill wait", "wait and see", "hold my action"]
    if any(w in lowered for w in pass_words):
        return {**base, "action": "pass_turn"}

    rest_words = ["i rest", "let's rest", "lets rest", "take a rest", "revive me", "heal up", "recover"]
    if any(w in lowered for w in rest_words):
        return {**base, "action": "rest"}

    unconditional_shove_words = ["shove", "tackle", "trip", "push over"]
    knock_down_phrasing = "knock" in lowered and ("prone" in lowered or "down" in lowered)
    if any(w in lowered for w in unconditional_shove_words) or knock_down_phrasing:
        return {**base, "action": "shove"}

    # Ability-check verb -> ability mapping. Deliberately conservative:
    # only fires on fairly explicit risky-action phrasing, so ordinary
    # roleplay chat isn't constantly misread as a check attempt.
    skill_check_verb_abilities = [
        (["sneak", "hide", "climb", "balance", "pick the lock", "disarm the trap", "tiptoe"], "dexterity"),
        (["lift", "push", "break down", "force open", "shove the", "smash"], "strength"),
        (["recall", "remember lore", "investigate", "decipher", "figure out the puzzle"], "intelligence"),
        (["search for", "look for hidden", "listen for", "spot", "sense", "track", "survive"], "wisdom"),
        (["persuade", "convince", "deceive", "lie to", "intimidate", "impress"], "charisma"),
        (["hold my breath", "endure", "resist the poison", "push through the pain"], "constitution"),
    ]
    for verbs, ability in skill_check_verb_abilities:
        if any(v in lowered for v in verbs):
            return {**base, "action": "skill_check", "ability": ability}

    return base


def parse_intent(text: str, known_npc_names: list[str] | None = None) -> dict:
    """
    Classify free text into a structured intent dict. Uses the build
    model (better at structured/JSON output) rather than the narration
    model. Falls back to keyword matching on any failure.
    """
    known_npc_names = known_npc_names or []
    fallback = _keyword_fallback(text, known_npc_names)

    try:
        response = requests.post(
            f"{config.OLLAMA_BASE_URL}/api/generate",
            json={
                "model": config.BUILD_MODEL,
                "prompt": f"{INTENT_SYSTEM_PROMPT}\n\nKnown NPCs: {known_npc_names}\n\nPlayer message: {text}",
                "stream": False,
                "format": "json",
            },
            timeout=30,
        )
        response.raise_for_status()
        data = response.json()
        raw_response = data.get("response", "")
        print(f"DEBUG: raw model response for intent parsing: {raw_response!r}")
        parsed = _extract_json(strip_think_tags(raw_response))
        print(f"DEBUG: parsed JSON: {parsed}")
        if parsed and "action" in parsed:
            parsed.setdefault("target", None)
            parsed.setdefault("npc_name", None)
            parsed.setdefault("ability", None)
            parsed.setdefault("item_name", None)
            parsed.setdefault("spell_name", None)
            parsed.setdefault("quantity", 1)
            parsed.setdefault("raw_text", text)
            valid_actions = (
                "attack", "pass_turn", "start_combat", "create_character",
                "check_sheet", "check_inventory", "check_party", "talk_npc", "move", "look",
                "buy", "sell", "cast_spell", "join_guild", "recruit_npc", "rest",
                "skill_check", "shove", "chat",
            )
            if parsed["action"] not in valid_actions:
                return fallback
            return parsed
    except (requests.RequestException, ValueError) as e:
        print(f"[intent_parser] model call failed, using keyword fallback: {e}")

    return fallback
