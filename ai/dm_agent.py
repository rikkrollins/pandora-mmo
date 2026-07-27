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

# Real perf fix (2026-07-17, per Coffee): no call anywhere in this
# module capped how many tokens the model could generate -- confirmed
# live tonight this let a handful of calls run unusually long (107s,
# 142s, 159s) even under otherwise-idle conditions, almost certainly
# from the model over-generating (this is a "thinking" model, so
# <think>...</think> reasoning tokens count against this cap too, not
# just the visible narration). Per Coffee's explicit direction right
# after this fix ("I want the storyline to feel long and entertaining
# but I want the code to execute like lightning"): the speed win here
# should come from bounding pathological runaway generation, NOT from
# shortening genuinely rich prose -- so this is deliberately generous,
# sized to comfortably fit even STORY_MODE 10's longest requested
# narration (up to ~19 sentences, see ai/story_mode.py's _FACTORS) plus
# real thinking overhead, not tuned down toward the typical case.
_NARRATION_OPTIONS = {"num_predict": 1200}

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
#
# Real live regression (2026-07-16, same night, caught within the hour):
# the first version of this instruction just said "use the acting
# character's real name" with no disambiguation -- and a compound
# message ("Hey Bram, are you here on a quest also?! And then I chop
# some lumber", where "Bram" isn't even a real NPC) got narrated as
# "Bram's hands clenched around the chisel..." instead of naming
# Ravenloft, the character who actually rolled the check. The prompt
# always puts the real actor's name on its own "Character: <name>"
# line, separate from the attempted-action text -- but the action text
# can legitimately contain OTHER names the player mentioned (someone
# they just spoke to), and the small model latched onto whichever name
# it saw first rather than the one on the Character: line. Now
# explicit about which name is authoritative.
_NAMING_INSTRUCTION = (
    "Always refer to the acting character by their actual given name "
    "(never a generic 'you' or unnamed pronoun as the subject) so "
    "readers can tell who is acting -- this narration is read by a "
    "shared group, not just this one player. The acting character's "
    "name is EXACTLY whatever is given on the 'Character:' line above "
    "-- if the attempted action mentions a different name (someone "
    "they spoke to, referenced, or addressed), that other name is "
    "NEVER the subject of your narration; only the Character: name is."
)


def _pronoun_line(character: dict) -> str:
    """
    Real pronoun fact for any SECONDARY reference to the character
    within the same narration (task #117, 2026-07-17, per Coffee: "no
    gender/pronoun field -- narration guesses pronouns with no real
    data, can guess wrong"). The subject itself is already covered by
    _NAMING_INSTRUCTION's real-name rule; this only matters for a
    follow-up pronoun later in the same sentence/paragraph. Falls back
    to they/them when the player never set one -- never guessed.
    """
    pronouns = character.get("pronouns") or "they/them"
    return f"If a pronoun is needed for {character.get('name')}, use: {pronouns}."


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
        f"{style_directive()} "
        # Real live bug (2026-07-18, task #165, reported by trusted dev
        # Sugar): a "Search for wolves" success came back as pure
        # atmospheric prose (forest holding its breath, senses
        # sharpening) with no stated result at all -- she genuinely
        # couldn't tell what her successful check had actually revealed
        # and had to ask in Development. Nothing above ever required the
        # narration to land on an actual answer, just mood. This closes
        # that gap without inventing new game facts: your VERY FIRST
        # sentence must be one plain-language line stating the concrete,
        # actionable outcome (what the character now knows, notices, or
        # can do next as a direct result of this roll) -- lead with the
        # answer, THEN let the sensory prose follow to build atmosphere
        # around it. Do not save the concrete outcome for the end: this
        # model has a hard output-length cap and a late payoff risks
        # being cut off entirely, silently reproducing the exact bug
        # this fixes. If a "Known real fact" is given below, that first "
        "sentence must reflect it exactly and invent nothing beyond it; "
        "if none is given, keep it honest and general (e.g. nothing of "
        "note turns up) rather than fabricating a specific discovery."
    )


def _build_skill_check_prompt(character: dict, action_text: str, ability: str,
                               mechanical_result: dict, grounded_fact: str | None = None) -> str:
    raw_roll = mechanical_result.get("raw_roll")
    drama = _drama_instruction(
        raw_roll,
        critical_hit=(raw_roll == 20),
        critical_fail=(raw_roll == 1),
    )
    fact_line = (
        f"Known real fact (reflect this exactly on success, invent nothing "
        f"beyond it): {grounded_fact}\n\n" if grounded_fact else ""
    )
    return (
        f"{_skill_check_preamble()}\n\n"
        f"Character: {character.get('name')} ({character.get('char_class')})\n"
        f"{_pronoun_line(character)}\n\n"
        f"Attempted action: {action_text}\n"
        f"Ability used: {ability.title()}\n\n"
        f"Mechanical result (already decided, narrate faithfully): {mechanical_result}\n\n"
        f"{fact_line}"
        f"Tone guidance for this specific roll: {drama}\n\n"
        f"Narrate this outcome now (remember: this is NOT combat):"
    )


def narrate_skill_check(character: dict, action_text: str, ability: str, mechanical_result: dict,
                         grounded_fact: str | None = None) -> str:
    """
    Generates narration for a non-combat skill check. Uses its own
    prompt and fallback (rather than reusing narrate_action) so a
    sneaking or persuading attempt is never narrated with combat
    language like "attacks" or "damage".

    grounded_fact (task #165): an optional real, already-true game fact
    (e.g. a monster confirmed present at this location) the caller has
    verified independently of the AI -- handed in so a search/perception
    check can state something concrete without the model ever having to
    invent what was found. Never populated with anything the rules
    layer hasn't already confirmed true.
    """
    prompt = _build_skill_check_prompt(character, action_text, ability, mechanical_result, grounded_fact)

    try:
        response = requests.post(
            f"{config.OLLAMA_BASE_URL}/api/generate",
            json={
                "model": config.DM_NARRATION_MODEL,
                "prompt": prompt,
                "stream": False,
                "options": _NARRATION_OPTIONS,
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

    return _fallback_skill_check_narration(character, action_text, mechanical_result, grounded_fact)


def _fallback_skill_check_narration(character: dict, action_text: str, mechanical_result: dict,
                                     grounded_fact: str | None = None) -> str:
    """Plain-text, non-combat fallback if the narration model is unreachable."""
    raw_roll = mechanical_result.get("raw_roll")
    success = mechanical_result.get("success", False)
    name = character.get("name", "You")

    if raw_roll == 20:
        base = f"{name} pulls it off spectacularly — a natural 20! Nothing could have gone better."
    elif raw_roll == 1:
        base = f"{name} fumbles badly — a natural 1. It couldn't have gone worse."
    elif success:
        base = f"{name} succeeds. The attempt goes just as hoped."
    else:
        base = f"{name} doesn't quite manage it this time."
    return f"{base} {grounded_fact}" if grounded_fact else base


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
        f"HP: {character.get('hp_current')}/{character.get('hp_max')}\n"
        f"{_pronoun_line(character)}\n\n"
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
                "options": _NARRATION_OPTIONS,
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


def _boss_decision_preamble() -> str:
    return (
        "You are the Dungeon Master narrating a boss monster's decision "
        "in a Dungeons & Dragons 5th Edition game, in the moment BEFORE "
        "any dice are rolled. No attack roll, hit/miss, or damage has "
        "been decided yet — do not mention or imply any of those. Your "
        "only job is a short, tense beat "
        f"({scaled_sentences(2, 3)}) of the boss sizing up the "
        "battlefield and settling on who to strike and why (weakest "
        "prey, biggest threat, a grudge, simple proximity — whatever "
        "fits the moment), ending with it committing to attack the "
        "target you're given. Never invent a different target, a "
        "different attacker, or any other characters, and never "
        "resolve or hint at the outcome of the attack that hasn't "
        f"happened yet. {_NAMING_INSTRUCTION} {style_directive()}"
    )


def _build_boss_decision_prompt(boss: dict, target: dict) -> str:
    return (
        f"{_boss_decision_preamble()}\n\n"
        f"Boss (the one deciding): {boss.get('name')}\n"
        f"Chosen target (already decided, do not change): {target.get('name')} "
        f"({target.get('hp_current')}/{target.get('hp_max', target.get('hp_current'))} HP)\n\n"
        f"Narrate the boss's decision now (no dice rolled yet):"
    )


def narrate_boss_decision(boss: dict, target: dict) -> str:
    """
    Task #167 (per Coffee, scoped to boss-tier enemies only after he
    flagged the latency tradeoff of doing this for every regular
    monster too): a short pre-roll narrative beat of a boss choosing
    its target, called from _resolve_ai_turns right after the target is
    already deterministically picked (min HP among the living
    opposition) but before resolve_attack rolls anything -- so this
    never invents a target, it just gives voice to a choice the rules
    layer already made. Separate Ollama call from the post-roll
    narrate_action, its own fallback so a network hiccup here never
    blocks the actual attack resolution that follows.
    """
    prompt = _build_boss_decision_prompt(boss, target)

    try:
        response = requests.post(
            f"{config.OLLAMA_BASE_URL}/api/generate",
            json={
                "model": config.DM_NARRATION_MODEL,
                "prompt": prompt,
                "stream": False,
                "options": _NARRATION_OPTIONS,
            },
            timeout=200,
        )
        response.raise_for_status()
        data = response.json()
        text = strip_think_tags(data.get("response", ""))
        if text:
            return text
    except (requests.RequestException, ValueError) as e:
        print(f"[dm_agent] boss decision narration call failed, falling back to template: {e}")

    return f"{boss.get('name')} sets its sights on {target.get('name')}."


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
                "options": _NARRATION_OPTIONS,
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
            json={"model": config.DM_NARRATION_MODEL, "prompt": prompt, "stream": False, "options": _NARRATION_OPTIONS},
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


def _story_so_far_preamble() -> str:
    # The "What's Next" hint used to be asked for as this recap's own
    # trailing paragraph; per Coffee's live dev-topic feedback right
    # after that shipped ("Make the what's next area it's own
    # paragraph so it's clear... style this up"), it's now generated as
    # its own separate, later narration call (see narrate_next_step_hint
    # below) and combined with guaranteed Markdown structure in bot.py,
    # so this preamble goes back to a plain reflective ending.
    return (
        "You are the Dungeon Master writing a \"story so far\" recap for a "
        "player of a Dungeons & Dragons 5th Edition game, in the voice of a "
        "novel's narrator looking back over what this character has "
        "actually lived through. You are given real facts — their name, "
        "which chapters of the story they've already completed, which one "
        "they're in right now, and which real quests they've finished along "
        "the way — that have ALREADY happened. Write a flowing narrative "
        f"recap ({scaled_sentences(4, 7)}) that reads like the opening of a "
        "novel's chapter, weaving these real facts into one coherent "
        "throughline in the order given, never inventing a person, place, "
        f"or event beyond what's provided. {_NAMING_INSTRUCTION} "
        f"{style_directive()} End on a natural, reflective forward-looking "
        "note about the chapter they're currently in, without revealing or "
        "hinting at anything from chapters still locked ahead."
    )


def _build_story_so_far_prompt(
    character_name: str, completed_arcs: list[tuple[str, str]],
    current_arc: tuple[str, str] | None, completed_quests: list[tuple[str, str]],
) -> str:
    completed_arc_text = "; ".join(f"{title}: {desc}" for title, desc in completed_arcs) or "none yet"
    completed_quest_text = "; ".join(f"{title} ({desc})" for title, desc in completed_quests) or "none yet"
    current_arc_text = f"{current_arc[0]}: {current_arc[1]}" if current_arc else "the story is complete"
    facts = (
        f"Character: {character_name}\n"
        f"Chapters already completed, in order: {completed_arc_text}\n"
        f"Current chapter: {current_arc_text}\n"
        f"Quests completed so far, in order: {completed_quest_text}"
    )
    return (
        f"{_story_so_far_preamble()}\n\n"
        f"Real facts (narrate ONLY these, faithfully):\n{facts}\n\n"
        f"Write the recap now:"
    )


def _next_step_hint_preamble(is_puzzle: bool) -> str:
    if is_puzzle:
        return (
            "You are the Dungeon Master giving a player a short, in-character "
            "hint about what comes next in their Dungeons & Dragons 5th "
            "Edition game. What comes next is a PUZZLE -- you are given its "
            "real riddle text. Write "
            f"{scaled_sentences(1, 2)} that evoke mood and mystery — vague and "
            "ominous, never simple — and you may hint at HOW one might go "
            "about puzzling it out (thinking carefully, listening, looking "
            "for a pattern), but you must NEVER state or imply the actual "
            f"answer. {style_directive()} Output ONLY the hint itself, no "
            "preamble, no chapter title, no quotation marks around it."
        )
    return (
        "You are the Dungeon Master giving a player a short, in-character "
        "hint about what comes next in their Dungeons & Dragons 5th Edition "
        "game. You are given a real location and a real clue about what "
        f"waits there. Write {scaled_sentences(1, 2)} that are clear and "
        "confident about WHERE to go and, in broad strokes, what to do when "
        "they get there — clear enough to act on immediately — without "
        f"spelling out plot specifics beyond that. {style_directive()} Output "
        "ONLY the hint itself, no preamble, no chapter title, no quotation "
        "marks around it."
    )


def _build_next_step_hint_prompt(next_step: dict) -> str:
    facts = ""
    if next_step.get("location_name"):
        facts += f"Location: {next_step['location_name']}\n"
    if next_step.get("clue"):
        facts += f"Clue: {next_step['clue']}"
    return (
        f"{_next_step_hint_preamble(next_step['is_puzzle'])}\n\n"
        f"Real facts (narrate ONLY these, faithfully):\n{facts}\n\n"
        f"Write the hint now:"
    )


def narrate_next_step_hint(next_step: dict) -> str:
    """
    The Story So Far screen's real "What's Next" section (2026-07-25,
    per Coffee: a clear hint for a normal quest, vague and ominous for a
    puzzle; 2026-07-26 follow-up, per live dev-topic feedback: "make
    the what's next area it's own paragraph... style this up" --
    generated as its OWN separate, focused narration call rather than
    asked for as a trailing paragraph inside the much longer recap
    call, so bot.py can combine the two with guaranteed, reliable
    Markdown structure instead of hoping the model's own paragraph
    break lands cleanly. next_step is bot.py's _next_step_hint_facts
    output -- real quest location/clue data, never invented here.
    """
    prompt = _build_next_step_hint_prompt(next_step)
    try:
        response = requests.post(
            f"{config.OLLAMA_BASE_URL}/api/generate",
            json={"model": config.DM_NARRATION_MODEL, "prompt": prompt, "stream": False, "options": _NARRATION_OPTIONS},
            timeout=200,
        )
        response.raise_for_status()
        data = response.json()
        text = strip_think_tags(data.get("response", ""))
        if text:
            return text
    except (requests.RequestException, ValueError) as e:
        print(f"[dm_agent] next-step-hint narration call failed, falling back to template: {e}")
    if next_step["is_puzzle"]:
        return f"Something unanswered still waits, patient and unwilling to make itself easy. {next_step.get('clue', '')}"
    where = f" toward {next_step['location_name']}" if next_step.get("location_name") else ""
    return f"The path ahead leads{where}. {next_step.get('clue', '')}"


def narrate_story_so_far(
    character_name: str, completed_arcs: list[tuple[str, str]],
    current_arc: tuple[str, str] | None, completed_quests: list[tuple[str, str]],
) -> str:
    """
    Task #176 menu revision, per Coffee: "I want that to be like a
    story/novel with actions and narrations making it a logical story" --
    the Story So Far menu screen's completed chapters/current chapter/
    completed quests are all real facts already computed by bot.py's
    _do_show_story_so_far (never invented here); this only turns them
    into flowing prose, same rules-decide/AI-narrates split as every
    other narration call in this game. See narrate_next_step_hint for
    the separate "What's Next" section bot.py combines with this.
    """
    prompt = _build_story_so_far_prompt(character_name, completed_arcs, current_arc, completed_quests)
    try:
        response = requests.post(
            f"{config.OLLAMA_BASE_URL}/api/generate",
            json={"model": config.DM_NARRATION_MODEL, "prompt": prompt, "stream": False, "options": _NARRATION_OPTIONS},
            timeout=200,
        )
        response.raise_for_status()
        data = response.json()
        text = strip_think_tags(data.get("response", ""))
        if text:
            return text
    except (requests.RequestException, ValueError) as e:
        print(f"[dm_agent] story-so-far narration call failed, falling back to template: {e}")
    return _fallback_story_so_far(character_name, completed_arcs, current_arc, completed_quests)


def _fallback_story_so_far(
    character_name: str, completed_arcs: list[tuple[str, str]],
    current_arc: tuple[str, str] | None, completed_quests: list[tuple[str, str]],
) -> str:
    """Plain-text fallback if the narration model is unreachable."""
    lines = [f"{character_name}'s journey so far:"]
    for title, desc in completed_arcs:
        lines.append(f"- {title}: {desc}")
    if current_arc:
        lines.append(f"Now: {current_arc[0]} — {current_arc[1]}")
    else:
        lines.append("The story is complete.")
    if completed_quests:
        lines.append("Quests completed: " + ", ".join(title for title, _ in completed_quests))
    return "\n".join(lines)


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
        f"{_pronoun_line(character)}\n"
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
            json={"model": config.DM_NARRATION_MODEL, "prompt": prompt, "stream": False, "options": _NARRATION_OPTIONS},
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
            json={"model": config.DM_NARRATION_MODEL, "prompt": prompt, "stream": False, "options": _NARRATION_OPTIONS},
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
            json={"model": config.DM_NARRATION_MODEL, "prompt": prompt, "stream": False, "options": _NARRATION_OPTIONS},
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


def _chapter_climax_preamble() -> str:
    return (
        "You are the Dungeon Master narrating the resolution of a "
        "chapter-defining, climactic quest a character just completed -- "
        "one of the biggest moments in this story so far. You are given "
        "the quest's real title and what it was actually about; narrate "
        f"ONLY these facts ({scaled_sentences(4, 6, boost=3)}), never "
        "inventing a new plot detail, character, or twist beyond what's "
        f"given. {_NAMING_INSTRUCTION} {style_directive(boost=3)}"
    )


def _build_chapter_climax_prompt(quest_title: str, quest_description: str, reward_text: str) -> str:
    return (
        f"{_chapter_climax_preamble()}\n\n"
        f"Real facts (narrate ONLY these, faithfully):\n"
        f"Quest just completed: {quest_title}\n"
        f"What it was about: {quest_description}\n"
        f"What was earned: {reward_text}\n\n"
        f"Write the climactic resolution now:"
    )


def narrate_chapter_climax(quest_title: str, quest_description: str, reward_text: str) -> str:
    """
    Phase 1 of the full-storyline plan: quests tagged "weight": "climactic"
    in campaign.json (currently clear_the_warrens, the_first_city_quest,
    the_unmoored_isle_quest -- The Hush joins this list in Phase 2 once
    it's split into a staged chain) get a real AI-narrated flourish here,
    at a deeper story_mode pass than ordinary quest completions -- same
    rules-decide/AI-narrates split as every other narration call in this
    game; the quest's title/description/reward are already-decided real
    facts, never invented here.
    """
    prompt = _build_chapter_climax_prompt(quest_title, quest_description, reward_text)
    try:
        response = requests.post(
            f"{config.OLLAMA_BASE_URL}/api/generate",
            json={"model": config.DM_NARRATION_MODEL, "prompt": prompt, "stream": False, "options": _NARRATION_OPTIONS},
            timeout=200,
        )
        response.raise_for_status()
        data = response.json()
        text = strip_think_tags(data.get("response", ""))
        if text:
            return text
    except (requests.RequestException, ValueError) as e:
        print(f"[dm_agent] chapter climax narration failed, falling back to template: {e}")
    return f"This was a turning point. {quest_description}"


def _boss_intro_preamble() -> str:
    return (
        "You are the Dungeon Master narrating the dramatic ENTRANCE of a "
        "real boss monster the party is about to fight -- a genuinely "
        "epic, tense moment, not a routine encounter. You are given the "
        "boss's real name, the real place this is happening, and that "
        "place's own real description; narrate ONLY these facts "
        f"({scaled_sentences(3, 5, boost=3)}), building real dread and "
        "stakes without resolving the fight or inventing a new plot "
        f"detail, character, or twist beyond what's given. {_NAMING_INSTRUCTION} "
        f"{style_directive(boost=3)}"
    )


def _build_boss_intro_prompt(monster_name: str, location_name: str, location_description: str) -> str:
    return (
        f"{_boss_intro_preamble()}\n\n"
        f"Real facts (narrate ONLY these, faithfully):\n"
        f"The boss: {monster_name}\n"
        f"Where this is happening: {location_name}\n"
        f"What this place is really like: {location_description}\n\n"
        f"Write the entrance now:"
    )


def narrate_boss_intro(monster_name: str, location_name: str, location_description: str) -> str:
    """
    A real, distinct cinematic beat the moment a fight against an
    is_boss monster actually begins (2026-07-27, per Coffee: "make
    sure all boss sequences are creative and scary... make it epic").
    Separate from the plain "Combat Begins!" header every fight
    already gets -- grounded only in the boss's own real name and the
    real location's own already-written description, never inventing
    new lore for the moment.
    """
    prompt = _build_boss_intro_prompt(monster_name, location_name, location_description)
    try:
        response = requests.post(
            f"{config.OLLAMA_BASE_URL}/api/generate",
            json={"model": config.DM_NARRATION_MODEL, "prompt": prompt, "stream": False, "options": _NARRATION_OPTIONS},
            timeout=200,
        )
        response.raise_for_status()
        data = response.json()
        text = strip_think_tags(data.get("response", ""))
        if text:
            return text
    except (requests.RequestException, ValueError) as e:
        print(f"[dm_agent] boss intro narration failed, falling back to template: {e}")
    return f"**{monster_name}** makes its presence known. This is going to be a real fight."


def _boss_defeat_preamble() -> str:
    return (
        "You are the Dungeon Master narrating the dramatic DEFEAT of a "
        "real boss monster the party just won a real fight against -- a "
        "genuinely epic, satisfying moment, not a routine kill. You are "
        "given the boss's real name and the real place this happened; "
        f"narrate ONLY these facts ({scaled_sentences(3, 5, boost=3)}), "
        "giving this victory real weight without inventing a new plot "
        f"detail, character, or twist beyond what's given. {_NAMING_INSTRUCTION} "
        f"{style_directive(boost=3)}"
    )


def _build_boss_defeat_prompt(monster_name: str, location_name: str) -> str:
    return (
        f"{_boss_defeat_preamble()}\n\n"
        f"Real facts (narrate ONLY these, faithfully):\n"
        f"The boss just defeated: {monster_name}\n"
        f"Where this happened: {location_name}\n\n"
        f"Write the defeat now:"
    )


def narrate_boss_defeat(monster_name: str, location_name: str) -> str:
    """
    The epic counterpart to narrate_boss_intro -- fires the moment an
    is_boss monster is actually defeated, distinct from the plain
    "X has been defeated!" line every other monster gets.
    """
    prompt = _build_boss_defeat_prompt(monster_name, location_name)
    try:
        response = requests.post(
            f"{config.OLLAMA_BASE_URL}/api/generate",
            json={"model": config.DM_NARRATION_MODEL, "prompt": prompt, "stream": False, "options": _NARRATION_OPTIONS},
            timeout=200,
        )
        response.raise_for_status()
        data = response.json()
        text = strip_think_tags(data.get("response", ""))
        if text:
            return text
    except (requests.RequestException, ValueError) as e:
        print(f"[dm_agent] boss defeat narration failed, falling back to template: {e}")
    return f"**{monster_name}** falls. A real, hard-won victory."


def _arc_opening_preamble() -> str:
    return (
        "You are the Dungeon Master narrating the OPENING of a brand new "
        "chapter of a much longer story -- the character has just taken "
        "on the quest that starts it. You are given the chapter's real "
        "title and description, and the specific quest that opens it; "
        f"narrate ONLY these facts ({scaled_sentences(3, 5, boost=2)}), "
        "setting the mood and stakes without resolving anything or "
        "inventing a new plot detail, character, or twist beyond what's "
        f"given. {_NAMING_INSTRUCTION} {style_directive(boost=2)}"
    )


def _build_arc_opening_prompt(arc_title: str, arc_description: str, quest_title: str) -> str:
    return (
        f"{_arc_opening_preamble()}\n\n"
        f"Real facts (narrate ONLY these, faithfully):\n"
        f"New chapter beginning: {arc_title}\n"
        f"What this chapter is about: {arc_description}\n"
        f"The quest that opens it: {quest_title}\n\n"
        f"Write the opening now:"
    )


def narrate_arc_opening(arc_title: str, arc_description: str, quest_title: str) -> str:
    """
    Cutscene-style bookend to narrate_chapter_climax's ending flourish:
    fires once, the moment a character accepts the FIRST quest of a new
    story arc (see bot.py's _arc_opening_note), giving that transition a
    real narrated beat instead of just a quest-accept line -- per
    Coffee's request for RPG-style cutscenes on story beats (2026-07-19/
    20). Same rules-decide/AI-narrates split as every other narration
    call: the arc's title/description and the quest's title are already-
    decided real facts, never invented here.
    """
    prompt = _build_arc_opening_prompt(arc_title, arc_description, quest_title)
    try:
        response = requests.post(
            f"{config.OLLAMA_BASE_URL}/api/generate",
            json={"model": config.DM_NARRATION_MODEL, "prompt": prompt, "stream": False, "options": _NARRATION_OPTIONS},
            timeout=200,
        )
        response.raise_for_status()
        data = response.json()
        text = strip_think_tags(data.get("response", ""))
        if text:
            return text
    except (requests.RequestException, ValueError) as e:
        print(f"[dm_agent] arc opening narration failed, falling back to template: {e}")
    return f"A new chapter begins. {arc_description}"
