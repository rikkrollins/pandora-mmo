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
from ai.story_mode import scaled_sentences, style_directive
from ai.text_cleanup import strip_think_tags

# Real live feedback (2026-07-16, Coffee, via Development-topic
# screenshot): "please say who is doing the action -- for example if it
# is Zara asking to chop wood, it says '[player] attempts to [action]'
# ... so people know who was doing what actions." This is a shared
# group chat where several human/AI characters act in the same feed --
# a narration that defaults to second-person "you" framing (fine solo)
# is genuinely ambiguous about who just acted. Every preamble already
# hands the model the character's real name as context, but none of
# them previously told it to actually USE that name in the prose it
# writes, so the model was free to default to ambiguous phrasing.
_NAMING_INSTRUCTION = (
    "Always refer to the acting character by their actual given name "
    "(never a generic 'you' or unnamed pronoun as the subject) so "
    "readers can tell who is acting -- this narration is read by a "
    "shared group, not just this one player."
)


def _skill_check_preamble() -> str:
    return (
        "You are the Dungeon Master narrating the outcome of a NON-COMBAT "
        "skill/ability check in a Dungeons & Dragons 5th Edition game — things "
        "like sneaking, persuading, climbing, lifting, or searching. This is "
        "NOT combat — never mention attacks, weapons, damage, or hit points. "
        "You are given the mechanical result of a dice roll that has ALREADY "
        "been decided by the game's rules engine. Your only job is to narrate "
        f"that result in vivid, sensory prose ({scaled_sentences(4, 6)}) that reads "
        "like part of an ongoing journey, not an isolated dice log line — "
        "faithfully reflecting whether it succeeded or failed. Ground it in "
        "the physical scene: what the character sees, hears, and feels in "
        "this exact moment. Mention the actual raw d20 number rolled "
        "somewhere in your narration, and calibrate how dramatic your prose "
        f"is to how good or bad that roll actually was. {_NAMING_INSTRUCTION} "
        f"{style_directive()}"
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
        f"{_skill_check_preamble()}\n\n"
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


def _combat_preamble() -> str:
    return (
        "You are the Dungeon Master narrating a Dungeons & Dragons 5th Edition "
        "game in progress — an ongoing journey, not a series of disconnected "
        "dice logs. You are given the mechanical result of a dice roll or "
        "combat action that has ALREADY been decided by the game's rules "
        "engine, plus a short list of recent events for continuity. Your job "
        f"is to narrate that result in vivid, sensory prose ({scaled_sentences(4, 6)}): "
        "ground it in the physical scene, and where it's natural, let it "
        "carry a thread from what just happened rather than starting cold "
        "each time. You must NEVER invent or alter stat outcomes, dice "
        "rolls, damage numbers, or hit/miss results — treat the provided "
        "result as ground truth and narrate it faithfully. Make this feel "
        "epic and alive, not a dry log line: lean into real sensory, "
        "elemental, and environmental imagery appropriate to the action "
        "(a lightning spell crashing down like the wrath of a storm, flame "
        "roaring hungrily, steel ringing against steel) — grounded in "
        "whatever real physical surroundings you're given, never invented "
        "beyond them. "
        "Mention the actual raw d20 number rolled somewhere in your narration "
        "(e.g. 'rolling a 17...'), and calibrate how dramatic or restrained "
        f"your prose is to how good or bad that roll actually was. {_NAMING_INSTRUCTION} "
        f"{style_directive()}"
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
                   recent_events: list[str] | None = None, actor_personality: str | None = None,
                   location_description: str | None = None) -> str:
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
    # Real, already-decided physical surroundings -- the ONLY environment
    # detail the model is allowed to react to, never invented. Lets a
    # lightning/fire effect meaningfully play off a real wet/dry/enclosed
    # setting (a storm crashing down in an open field, a flame licking
    # dangerously close to spilled ale in a tavern) instead of narrating
    # every cast in a placeless void — ground truth (hit/miss, damage)
    # never changes because of this, it only colors HOW it's described.
    environment_line = (
        f"The physical surroundings this is happening in (let this genuinely "
        f"color the imagery — an apt or ironic environmental detail is far "
        f"more memorable than generic prose, but never let it change who won, "
        f"lost, or how much damage was dealt): {location_description}\n\n"
        if location_description else ""
    )

    return (
        f"{_combat_preamble()}\n\n"
        f"Character: {character.get('name')} ({character.get('char_class')}), "
        f"HP: {character.get('hp_current')}/{character.get('hp_max')}\n\n"
        f"{personality_line}"
        f"{environment_line}"
        f"Player action: {action_text}\n\n"
        f"Mechanical result (already decided, narrate faithfully): {mechanical_result}\n\n"
        f"Tone guidance for this specific roll: {drama}\n\n"
        f"Recent events:\n{history}\n\n"
        f"Narrate this outcome now:"
    )


def narrate_action(character: dict, action_text: str, mechanical_result: dict,
                    recent_events: list[str] | None = None, actor_personality: str | None = None,
                    location_description: str | None = None) -> str:
    """
    Sends the mechanical result to the narration model and returns prose.
    Falls back to a plain template if Ollama is unreachable or errors.
    `actor_personality` (a known NPC/AI companion's personality text) lets
    a recruited companion's or a hostile NPC's combat turn read in their
    own voice instead of generic monster-attack prose — same ground-truth
    mechanical result either way, just narrated in character.
    `location_description` (bot.py's _post_narrated resolves this from the
    real, current location) lets the environment genuinely inform the
    prose — see its note in _build_prompt for why and its limits.
    """
    prompt = _build_prompt(
        character, action_text, mechanical_result, recent_events, actor_personality, location_description
    )

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


def _welcome_preamble() -> str:
    return (
        "You are the Dungeon Master opening a brand-new character's journey "
        "into a Dungeons & Dragons 5th Edition game — this is the first page "
        "of their story, so make it feel like one. You are given real facts "
        "about where they are starting (a location name, description, "
        "who/what is physically present) that have ALREADY been decided by "
        f"the game data. Write a rich, immersive opening ({scaled_sentences(6, 9)}) "
        "that: (1) welcomes the character by name and weaves in their "
        "race/class as part of who they are, not a list of stats, (2) "
        "brings the scene to life with real sensory detail — sound, light, "
        "smell, texture — describing ONLY the real, provided location facts "
        "faithfully, never inventing new people, objects, dangers, or "
        "details beyond what's given, (3) NEVER gives hints, suggestions, "
        "or advice about what to do next, where to go, or what anything "
        "means — describe only what is visible right now, in this moment, "
        f"with no foreshadowing or speculation. {style_directive()} End on a "
        "note of open possibility, not a summary."
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
        f"{_welcome_preamble()}\n\n"
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


def _hourly_update_preamble() -> str:
    return (
        "You are the Dungeon Master delivering a brief, in-world \"town "
        "crier\" update on what's been happening at a location, for players "
        "checking the chat to catch up on the game's ongoing life. You are "
        "given real facts — recent events, and what NPCs/companions are "
        "currently doing here — that have ALREADY happened or are true "
        f"right now. Write a vivid update ({scaled_sentences(2, 4)}) that "
        "narrates ONLY these real, provided facts faithfully, in the same "
        "immersive voice as the rest of the game's narration — never "
        "inventing new events, people, or details beyond what's given. If "
        "there's nothing notable to report, just describe the quiet, "
        f"ordinary rhythm of the place. {style_directive()} Do not list player "
        "counts, quest names, or game statistics — those are added "
        "separately after your narration, so leave them out entirely."
    )


def _build_hourly_update_prompt(location_name: str, recent_events: list[str], activity_lines: list[str]) -> str:
    facts = (
        f"Location: {location_name}\n"
        f"Recent events here (last hour): {'; '.join(recent_events) if recent_events else 'none'}\n"
        f"Who's currently here and what they're doing: "
        f"{'; '.join(activity_lines) if activity_lines else 'no one of note'}"
    )
    return (
        f"{_hourly_update_preamble()}\n\n"
        f"Real facts (narrate ONLY these, faithfully):\n{facts}\n\n"
        f"Write the update now:"
    )


def narrate_hourly_update(location_name: str, recent_events: list[str], activity_lines: list[str]) -> str:
    """
    Generates the flavor portion of the hourly Adventure-topic status
    update, grounded strictly in real events/activity already computed
    by bot.py. Player counts and the quest board are appended
    separately as plain deterministic text (never passed through the
    model), so numbers and quest titles can never be misremembered or
    invented — same "rules decide, AI narrates" split as everywhere
    else in this game.
    """
    prompt = _build_hourly_update_prompt(location_name, recent_events, activity_lines)
    try:
        response = requests.post(
            f"{config.OLLAMA_BASE_URL}/api/generate",
            json={"model": config.DM_NARRATION_MODEL, "prompt": prompt, "stream": False},
            timeout=200,
        )
        response.raise_for_status()
        data = response.json()
        text = strip_think_tags(data.get("response", ""))
        if text:
            return text
    except (requests.RequestException, ValueError) as e:
        print(f"[dm_agent] hourly update narration call failed, falling back to template: {e}")
    return _fallback_hourly_update(location_name, recent_events, activity_lines)


def _fallback_hourly_update(location_name: str, recent_events: list[str], activity_lines: list[str]) -> str:
    """Plain-text fallback if the narration model is unreachable."""
    lines = [f"The hour turns over at {location_name}."]
    if recent_events:
        lines.append(" ".join(recent_events))
    if activity_lines:
        lines.append(" ".join(activity_lines))
    if len(lines) == 1:
        lines.append("Nothing much stirs here just now.")
    return " ".join(lines)


def _examine_preamble() -> str:
    return (
        "You are the Dungeon Master describing a character taking a closer "
        "look at ONE specific object or detail in their surroundings. You "
        "are given the real, already-decided facts about what this object "
        "is and what's noticeable about it — narrate ONLY these facts, "
        f"faithfully and vividly ({scaled_sentences(2, 4)}), in the same immersive "
        "voice as the rest of the game. Sensory detail and atmosphere are "
        "welcome; inventing a new object, a hidden mechanism, a secret "
        "passage, or ANY fact beyond what's given is not. Never resolve or "
        "explain what's strange about it — if it's presented as mysterious, "
        f"it must stay exactly as mysterious after your narration as before. {_NAMING_INSTRUCTION} "
        f"{style_directive()}"
    )


def _build_examine_prompt(character: dict, location_name: str, object_name: str, object_description: str) -> str:
    return (
        f"{_examine_preamble()}\n\n"
        f"Character: {character['name']}, a {character['race']} {character['char_class']}\n"
        f"Location: {location_name}\n"
        f"Object being examined: {object_name}\n"
        f"Real facts about it (narrate ONLY these, faithfully): {object_description}\n\n"
        f"Write the description now:"
    )


def narrate_examine(character: dict, location_name: str, object_name: str, object_description: str) -> str:
    """
    Narrates a character examining one specific, real, campaign-defined
    object — grounded strictly in that object's own description text.
    Falls back to the plain description if Ollama is unreachable.
    """
    prompt = _build_examine_prompt(character, location_name, object_name, object_description)
    try:
        response = requests.post(
            f"{config.OLLAMA_BASE_URL}/api/generate",
            json={"model": config.DM_NARRATION_MODEL, "prompt": prompt, "stream": False},
            timeout=200,
        )
        response.raise_for_status()
        data = response.json()
        text = strip_think_tags(data.get("response", ""))
        if text:
            return text
    except (requests.RequestException, ValueError) as e:
        print(f"[dm_agent] examine narration call failed, falling back to plain description: {e}")
    return object_description


def _branching_setup_preamble() -> str:
    return (
        "You are the Dungeon Master writing the setup for a morally "
        "ambiguous bounty posted on a location's quest board. You are "
        "given real facts: the location, an NPC connected to it (if "
        "any), and the real physical objective (a monster to deal with "
        "or material to retrieve) — these facts have ALREADY been "
        f"decided. Write a short, atmospheric setup ({scaled_sentences(3, 5)}) "
        "that frames the task with a hint of moral ambiguity or an "
        "unresolved question — something about this isn't quite as "
        "simple as it first sounds. Do NOT invent any new named person, "
        "place, faction, or object beyond what's given, and do NOT "
        "resolve the ambiguity or state which choice is 'right' — leave "
        "it genuinely open. Do NOT list or describe the specific "
        "choices themselves; those are presented separately, after your "
        f"narration. {style_directive()}"
    )


def _build_branching_setup_prompt(location_name: str, npc_name: str | None,
                                   npc_personality: str | None, objective_facts: str) -> str:
    npc_line = (
        f"An NPC tied to this place: {npc_name} ({npc_personality})"
        if npc_name else "No specific NPC is tied to this place — it's simply posted on the board."
    )
    return (
        f"{_branching_setup_preamble()}\n\n"
        f"Location: {location_name}\n"
        f"{npc_line}\n"
        f"Real objective (narrate ONLY this, faithfully): {objective_facts}\n\n"
        f"Write the setup now:"
    )


def narrate_branching_quest_setup(location_name: str, npc_name: str | None,
                                   npc_personality: str | None, objective_facts: str) -> str:
    """
    Narrates the atmospheric setup for a branching (moral-choice) board
    quest. Grounded strictly in the real location/NPC/objective facts
    given — never invents the choices themselves or their consequences,
    which are fixed, deterministic data decided by board_quests.py.
    """
    prompt = _build_branching_setup_prompt(location_name, npc_name, npc_personality, objective_facts)
    try:
        response = requests.post(
            f"{config.OLLAMA_BASE_URL}/api/generate",
            json={"model": config.DM_NARRATION_MODEL, "prompt": prompt, "stream": False},
            timeout=200,
        )
        response.raise_for_status()
        data = response.json()
        text = strip_think_tags(data.get("response", ""))
        if text:
            return text
    except (requests.RequestException, ValueError) as e:
        print(f"[dm_agent] branching quest setup narration failed, falling back to plain text: {e}")
    return f"Something about this task at {location_name} doesn't sit quite right."


def _build_branching_outcome_prompt(location_name: str, choice_label: str, outcome_facts: str) -> str:
    return (
        "You are the Dungeon Master narrating the resolution of a moral "
        "choice a character just made on a bounty. You are given the "
        "real choice they made and its real, already-decided "
        f"consequence — narrate ONLY this ({scaled_sentences(2, 4)}), faithfully, "
        "without inventing new facts, and without passing judgment on "
        f"whether it was the 'right' choice. {style_directive()}\n\n"
        f"Location: {location_name}\n"
        f"Choice made: {choice_label}\n"
        f"Real consequence (narrate ONLY this, faithfully): {outcome_facts}\n\n"
        f"Write the resolution now:"
    )


def narrate_branching_choice_outcome(location_name: str, choice_label: str, outcome_facts: str) -> str:
    """Narrates the resolution of a branching quest's final choice, grounded in its fixed, real consequence."""
    prompt = _build_branching_outcome_prompt(location_name, choice_label, outcome_facts)
    try:
        response = requests.post(
            f"{config.OLLAMA_BASE_URL}/api/generate",
            json={"model": config.DM_NARRATION_MODEL, "prompt": prompt, "stream": False},
            timeout=200,
        )
        response.raise_for_status()
        data = response.json()
        text = strip_think_tags(data.get("response", ""))
        if text:
            return text
    except (requests.RequestException, ValueError) as e:
        print(f"[dm_agent] branching quest outcome narration failed, falling back to plain text: {e}")
    return outcome_facts
