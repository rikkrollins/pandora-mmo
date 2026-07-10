"""
ai/dm_agent.py
Calls a local Ollama model to narrate combat/action outcomes in prose.
Uses config.DM_NARRATION_MODEL (default: lfm2.5-thinking:latest) rather
than the main build model, since this task needs good creative writing,
not tool-calling.

IMPORTANT: this module never decides outcomes. It only narrates facts
that were already computed by rules/combat.py or rules/dice.py.
"""
import requests

import config
from ai.text_cleanup import strip_think_tags

SKILL_CHECK_SYSTEM_PREAMBLE = (
    "You are the Dungeon Master narrating the outcome of a NON-COMBAT "
    "skill/ability check in a Dungeons & Dragons 5th Edition game — things "
    "like sneaking, persuading, climbing, lifting, or searching. This is "
    "NOT combat — never mention attacks, weapons, damage, or hit points. "
    "You are given the mechanical result of a dice roll that has ALREADY "
    "been decided by the game's rules engine. Your only job is to narrate "
    "that result in vivid, sensory prose (4-6 sentences) that reads like "
    "part of an ongoing journey, not an isolated dice log line — "
    "faithfully reflecting whether it succeeded or failed. Ground it in "
    "the physical scene: what the character sees, hears, and feels in "
    "this exact moment. Mention the actual raw d20 number rolled "
    "somewhere in your narration, and calibrate how dramatic your prose "
    "is to how good or bad that roll actually was."
)


def _build_skill_check_prompt(character: dict, action_text: str, ability: str,
                               mechanical_result: dict) -> str:
    raw_roll = mechanical_result.get("raw_roll")
    drama = _drama_instruction(
        raw_roll,
        critical_hit=(raw_roll == 20),
        critical_fail=(raw_roll == 1),
    )
    return (
        f"{SKILL_CHECK_SYSTEM_PREAMBLE}\n\n"
        f"Character: {character.get('name')} ({character.get('char_class')})\n\n"
        f"Attempted action: {action_text}\n"
        f"Ability used: {ability.title()}\n\n"
        f"Mechanical result (already decided, narrate faithfully): {mechanical_result}\n\n"
        f"Tone guidance for this specific roll: {drama}\n\n"
        f"Narrate this outcome now (remember: this is NOT combat):"
    )


def narrate_skill_check(character: dict, action_text: str, ability: str, mechanical_result: dict) -> str:
    """
    Generates narration for a non-combat skill check. Uses its own
    prompt and fallback (rather than reusing narrate_action) so a
    sneaking or persuading attempt is never narrated with combat
    language like "attacks" or "damage".
    """
    prompt = _build_skill_check_prompt(character, action_text, ability, mechanical_result)

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
        print(f"[dm_agent] skill check narration call failed, falling back to template: {e}")

    return _fallback_skill_check_narration(character, action_text, mechanical_result)


def _fallback_skill_check_narration(character: dict, action_text: str, mechanical_result: dict) -> str:
    """Plain-text, non-combat fallback if the narration model is unreachable."""
    raw_roll = mechanical_result.get("raw_roll")
    success = mechanical_result.get("success", False)
    name = character.get("name", "You")

    if raw_roll == 20:
        return f"{name} pulls it off spectacularly — a natural 20! Nothing could have gone better."
    if raw_roll == 1:
        return f"{name} fumbles badly — a natural 1. It couldn't have gone worse."
    if success:
        return f"{name} succeeds. The attempt goes just as hoped."
    return f"{name} doesn't quite manage it this time."


SYSTEM_PREAMBLE = (
    "You are the Dungeon Master narrating a Dungeons & Dragons 5th Edition "
    "game in progress — an ongoing journey, not a series of disconnected "
    "dice logs. You are given the mechanical result of a dice roll or "
    "combat action that has ALREADY been decided by the game's rules "
    "engine, plus a short list of recent events for continuity. Your job "
    "is to narrate that result in vivid, sensory prose (4-6 sentences): "
    "ground it in the physical scene, and where it's natural, let it "
    "carry a thread from what just happened rather than starting cold "
    "each time. You must NEVER invent or alter stat outcomes, dice "
    "rolls, damage numbers, or hit/miss results — treat the provided "
    "result as ground truth and narrate it faithfully. "
    "Mention the actual raw d20 number rolled somewhere in your narration "
    "(e.g. 'rolling a 17...'), and calibrate how dramatic or restrained "
    "your prose is to how good or bad that roll actually was."
)


def _drama_instruction(raw_roll: int | None, critical_hit: bool, critical_fail: bool) -> str:
    """
    Translates a raw d20 roll into an explicit tone instruction for the
    model, so a weaker local model has clear guidance rather than having
    to infer drama level purely from a bare number.
    """
    if raw_roll is None:
        return ""
    if critical_hit or raw_roll == 20:
        return (
            "This was a NATURAL 20 — a legendary, spectacular critical success. "
            "Go big: make this the single most triumphant, over-the-top moment "
            "of the fight so far."
        )
    if critical_fail or raw_roll == 1:
        return (
            "This was a NATURAL 1 — a humiliating, comedic total failure. "
            "Make it embarrassing and a little funny: a stumble, a fumble, "
            "something that would make onlookers wince or laugh."
        )
    if raw_roll >= 17:
        return "This was a very strong, skillful roll — narrate it as confident and impressive."
    if raw_roll <= 4:
        return "This was a very weak roll — narrate it as clumsy, unlucky, or barely competent."
    return "This was an ordinary, middling roll — narrate it with a normal, measured tone."


def _build_prompt(character: dict, action_text: str, mechanical_result: dict,
                   recent_events: list[str] | None = None, actor_personality: str | None = None) -> str:
    recent_events = recent_events or []
    history = "\n".join(f"- {e}" for e in recent_events[-10:]) or "(no prior events)"

    raw_roll = mechanical_result.get("raw_roll")
    drama = _drama_instruction(
        raw_roll,
        mechanical_result.get("critical_hit", False),
        mechanical_result.get("critical_fail", False),
    )
    personality_line = (
        f"{character.get('name')}'s personality — reflect this in HOW they act, not just that "
        f"they act: {actor_personality}\n\n"
        if actor_personality else ""
    )

    return (
        f"{SYSTEM_PREAMBLE}\n\n"
        f"Character: {character.get('name')} ({character.get('char_class')}), "
        f"HP: {character.get('hp_current')}/{character.get('hp_max')}\n\n"
        f"{personality_line}"
        f"Player action: {action_text}\n\n"
        f"Mechanical result (already decided, narrate faithfully): {mechanical_result}\n\n"
        f"Tone guidance for this specific roll: {drama}\n\n"
        f"Recent events:\n{history}\n\n"
        f"Narrate this outcome now:"
    )


def narrate_action(character: dict, action_text: str, mechanical_result: dict,
                    recent_events: list[str] | None = None, actor_personality: str | None = None) -> str:
    """
    Sends the mechanical result to the narration model and returns prose.
    Falls back to a plain template if Ollama is unreachable or errors.
    `actor_personality` (a known NPC/AI companion's personality text) lets
    a recruited companion's or a hostile NPC's combat turn read in their
    own voice instead of generic monster-attack prose — same ground-truth
    mechanical result either way, just narrated in character.
    """
    prompt = _build_prompt(character, action_text, mechanical_result, recent_events, actor_personality)

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
        print(f"[dm_agent] narration call failed, falling back to template: {e}")

    return _fallback_narration(mechanical_result)


def _fallback_narration(mechanical_result: dict) -> str:
    """Plain-text fallback if the narration model is unreachable."""
    raw_roll = mechanical_result.get("raw_roll")
    attacker = mechanical_result.get("attacker", "The attacker")
    defender = mechanical_result.get("defender", "the target")

    if mechanical_result.get("critical_fail") or raw_roll == 1:
        return f"{attacker} rolls a natural 1 — an embarrassing, total fumble against {defender}!"
    if not mechanical_result.get("hit", True):
        roll_note = f" (rolled a {raw_roll})" if raw_roll is not None else ""
        return f"{attacker} swings and misses{roll_note} against {defender}!"

    dmg = mechanical_result.get("damage_dealt", 0)
    if mechanical_result.get("critical_hit") or raw_roll == 20:
        return f"NATURAL 20! {attacker} lands a spectacular critical hit on {defender} for {dmg} damage!"
    roll_note = f" (rolled a {raw_roll})" if raw_roll is not None else ""
    return f"{attacker} hits{roll_note} {defender} for {dmg} damage!"


WELCOME_SYSTEM_PREAMBLE = (
    "You are the Dungeon Master opening a brand-new character's journey "
    "into a Dungeons & Dragons 5th Edition game — this is the first page "
    "of their story, so make it feel like one. You are given real facts "
    "about where they are starting (a location name, description, "
    "who/what is physically present) that have ALREADY been decided by "
    "the game data. Write a rich, immersive opening (6-9 sentences) "
    "that: (1) welcomes the character by name and weaves in their "
    "race/class as part of who they are, not a list of stats, (2) "
    "brings the scene to life with real sensory detail — sound, light, "
    "smell, texture — describing ONLY the real, provided location facts "
    "faithfully, never inventing new people, objects, dangers, or "
    "details beyond what's given, (3) NEVER gives hints, suggestions, "
    "or advice about what to do next, where to go, or what anything "
    "means — describe only what is visible right now, in this moment, "
    "with no foreshadowing or speculation. End on a note of open "
    "possibility, not a summary."
)


def _build_welcome_prompt(character: dict, location: dict, party_summary: str) -> str:
    npc_names = location.get("npc_names", [])
    monster_names = location.get("monster_names", [])
    connections = location.get("connection_names", [])

    facts = (
        f"Location name: {location['name']} (layer: {location.get('layer', 'surface')})\n"
        f"Location description: {location['description']}\n"
        f"People physically present: {', '.join(npc_names) or 'none'}\n"
        f"Danger physically present: {', '.join(monster_names) or 'none'}\n"
        f"Places reachable from here: {', '.join(connections) or 'none'}\n"
        f"Current party: {party_summary}"
    )

    return (
        f"{WELCOME_SYSTEM_PREAMBLE}\n\n"
        f"New character: {character['name']}, a {character['race']} {character['char_class']}\n\n"
        f"Real facts about their starting location (narrate ONLY these, faithfully):\n{facts}\n\n"
        f"Write the welcome now:"
    )


def narrate_welcome(character: dict, location: dict, party_summary: str) -> str:
    """
    Generates a one-time welcome narration for a newly created character,
    grounded strictly in real, provided location data. Falls back to a
    plain factual template if Ollama is unreachable.
    """
    prompt = _build_welcome_prompt(character, location, party_summary)

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
        print(f"[dm_agent] welcome narration call failed, falling back to template: {e}")

    return _fallback_welcome(character, location, party_summary)


def _fallback_welcome(character: dict, location: dict, party_summary: str) -> str:
    """Plain-text fallback welcome if the narration model is unreachable."""
    lines = [
        f"Welcome, {character['name']} the {character['race']} {character['char_class']}!",
        f"You find yourself at {location['name']}. {location['description']}",
    ]
    if location.get("npc_names"):
        lines.append(f"Present here: {', '.join(location['npc_names'])}.")
    if location.get("monster_names"):
        lines.append(f"Danger nearby: {', '.join(location['monster_names'])}.")
    lines.append(f"Your party: {party_summary}")
    return " ".join(lines)
