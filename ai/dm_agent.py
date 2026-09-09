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
from ai.text_cleanup import strip_think_tags, strip_internal_jargon, is_placeholder_text

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
#
# Real live bug (2026-08-13, dev-bridge screenshot, Coffee: "The
# narration cut off can you investigate this? This might be why the
# banter isn't working also."): 1200 was measurably NOT enough headroom
# -- confirmed live, narration truncated mid-word ("...the weight of
# unsp") at STORY_MODE=7 (factor 1.7 of _FACTORS' max 3.2), well below
# the STORY_MODE 10 case this cap was originally sized for. Since
# num_predict caps thinking + answer tokens TOGETHER and the model's own
# reasoning length varies run-to-run independent of how long the
# eventual narration turns out to be, a heavier thinking pass on any
# given call can still eat past 1200 before the visible answer is even
# finished -- exactly the mechanism ai/text_cleanup.py's strip_think_
# tags docstring already documents for a truncated <think> block, just
# manifesting here as truncated narration instead. Doubled for real
# headroom against this, still bounded (not unlimited) per Coffee's
# original "execute like lightning" intent -- this is a floor increase,
# not a removal of the runaway-generation guard.
_NARRATION_OPTIONS = {"num_predict": 2400}

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

# Real live bug (2026-08-14, dev-topic screenshot report): the
# "Mechanical result" dict handed to you below is internal data for
# your reference only, in Python syntax -- a real reported occurrence
# had the model quote a literal field name straight into prose ("The
# raw_roll of 9 thudded through him"). Prompt-level instruction paired
# with a deterministic post-generation strip (ai.text_cleanup.
# strip_internal_jargon) as a real safety net either way -- same
# defense-in-depth philosophy this codebase already uses everywhere
# else (never trust the model alone to follow a formatting rule).
_INTERNAL_FIELD_WARNING = (
    "The 'Mechanical result' below is internal data in raw Python syntax, "
    "for your reference only -- NEVER quote its field names (things like "
    "raw_roll, damage_dealt, hp_current) or any snake_case/underscored word "
    "literally in your narration; translate every value into plain, natural "
    "in-world language instead."
)

# Real live bug (2026-08-14, same dev-topic screenshot report as above):
# a separate deterministic "Combat Resolution" block ALWAYS follows your
# prose and states the real numbers (target, exact damage dealt, HP
# remaining) -- that is its one and only job. A real reported occurrence
# had the model instead write its OWN fake summary sentence mimicking
# that exact format inside its prose ("Wren Hollowbrook hits Goblin
# Shaman 2 for 116 damage!"), naming a target that wasn't even in this
# fight and inventing a damage number that didn't match the real one
# printed right below it -- almost certainly picked up from past combat
# log lines appearing in "Recent events" and mimicked as if they were a
# style to imitate rather than raw data.
_NO_FAKE_RESOLUTION_WARNING = (
    "NEVER write your own summary sentence that states a specific damage "
    "number, a target's remaining HP, or names who was hit in a "
    "'X hits Y for Z damage' style line -- that is exclusively the job of "
    "the deterministic block that automatically follows your narration, "
    "and doing so risks stating a number or name that contradicts it. "
    "Write atmosphere, action, and consequence in prose; never a log line, "
    "and never mimic the format of anything in 'Recent events' below."
)


def _pronoun_line(character: dict) -> str:
    """
    Real pronoun fact for any SECONDARY reference to the character
    within the same narration (task #117, 2026-07-17, per Coffee: "no
    gender/pronoun field -- narration guesses pronouns with no real
    data, can guess wrong"). The subject itself is already covered by
    _NAMING_INSTRUCTION's real-name rule; this only matters for a
    follow-up pronoun later in the same sentence/paragraph. Falls back
    to they/them when the player never set one (true for every real
    monster/spirit participant too, which never has a pronouns field
    at all) -- never guessed.

    Strengthened wording (2026-08-21, real live report, Coffee: "The
    narration is mistaking gender again" -- caught live against a
    genderless Shadow Wisp, which the model called "she" anyway despite
    the old, softer "if a pronoun is needed, use X" phrasing). The
    they/them fallback case now gets an explicit "never guess a gender"
    call-out rather than relying on the model to infer that they/them
    means no gender is known -- same "make the constraint impossible to
    miss" fix shape _NAMING_INSTRUCTION/target_line already use for
    their own real live reports.
    """
    pronouns = character.get("pronouns")
    if pronouns:
        return f"If a pronoun is needed for {character.get('name')}, use: {pronouns}."
    return (
        f"{character.get('name')}'s gender is unknown -- if a pronoun is needed, use "
        f"they/them ONLY. Never guess or assume he/him or she/her for {character.get('name')}, "
        f"even if the name, role, or a taunt/line of dialogue might suggest one."
    )


def _skill_check_preamble() -> str:
    return (
        "You are the Dungeon Master narrating the outcome of a NON-COMBAT "
        "skill/ability check in a 5th-edition-style tabletop role-playing game — things "
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
        f"is to how good or bad that roll actually was. {_INTERNAL_FIELD_WARNING} {_NO_FAKE_RESOLUTION_WARNING} "
        f"{_NAMING_INSTRUCTION} "
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
        f"Ability used: {ability.title()}\n"
        # Real live bug (2026-09-01, dev-bridge screenshot): a failed
        # gather check's narration read "The d12 whispers 4," despite
        # the preamble already saying "the actual raw d20 number" --
        # the SAME model, on the very next roll (a success), correctly
        # said "d20." The general prose instruction alone wasn't a
        # reliable enough guardrail for this model to never invent a
        # wrong die type; every ability check in this game is a d20,
        # full stop, so it's restated here as its own isolated, hard
        # fact line -- same "Known real fact" pattern grounded_fact
        # already uses for a search/perception check's own discovery.
        f"Known real fact: this is a d20 roll. It is never a d12, d10, d8, "
        f"d6, or any other die -- do not call it anything but a d20.\n\n"
        f"Mechanical result (already decided, narrate faithfully): {mechanical_result}\n\n"
        f"{fact_line}"
        f"Tone guidance for this specific roll: {drama}\n\n"
        f"Narrate this outcome now (remember: this is NOT combat):"
    )


# Real live finding (2026-08-11, dev-topic investigation into "enemy
# battle banter never appears"): _BANTER_INSTRUCTION below is correctly
# wired and DOES work -- confirmed live, a direct narrate_action call
# with include_banter=True returned real quoted dialogue -- but a
# SEPARATE direct call made minutes earlier, under ordinary daytime
# load, never completed within its own 200s timeout at all, silently
# falling back to the plain banter-less template. bot.py's world tick
# already has this exact "back off while a real player is waiting on
# an answer" protection for Support (is_support_call_active(), see
# ai/support_agent.py's own docstring on the identical starvation
# bug), but combat/skill-check narration -- the two calls that
# actually block a real player's turn -- had no equivalent, exactly
# the gap project_world_tick_ollama_starvation's own notes flagged as
# untested. Same mechanism, mirrored here: a plain module-level flag,
# set for the duration of the real network call, checked by the world
# tick before it fires its own ambient Ollama calls (NPC heartbeat,
# hourly update, AI-party autonomous turns).
_narration_call_active = False


def is_narration_call_active() -> bool:
    return _narration_call_active


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

    global _narration_call_active
    _narration_call_active = True
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
        text = strip_internal_jargon(strip_think_tags(data.get("response", "")))
        if text and not is_placeholder_text(text):
            return text
    except (requests.RequestException, ValueError) as e:
        print(f"[dm_agent] skill check narration call failed, falling back to template: {e}")
    finally:
        _narration_call_active = False

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
        "You are the Dungeon Master narrating a 5th-edition-style tabletop role-playing "
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
        f"your prose is to how good or bad that roll actually was. {_INTERNAL_FIELD_WARNING} {_NO_FAKE_RESOLUTION_WARNING} "
        f"{_NAMING_INSTRUCTION} "
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


# Real live feature request (2026-08-09, per Coffee): "in battle give
# the enemies small talk banter, teasing, coaxing type narrations to
# keep the fights entertaining, enjoyable, funny." Folded into this
# EXISTING per-attack narration call rather than a separate Ollama
# call -- this session's own dev-topic monitoring has repeatedly
# observed severe single-slot Ollama contention (a single generation
# slot shared with the live bot means a second call per enemy attack
# would double an already-scarce resource), and bot.py only sets
# include_banter=True for a fraction of enemy turns (see
# _post_narrated's include_banter roll) so this never fires every
# single attack even for the calls that do use it. Most monsters have
# no personality field (plain stat blocks in campaign.json), so this
# instruction leans on the model inferring in-character flavor from
# the attacker's own name/type (a goblin sounds cocky, a spider
# hisses, a boss monologues) rather than requiring one.
_BANTER_INSTRUCTION = (
    "As part of this narration, have the attacker speak ONE short line "
    "of in-character dialogue (in quotation marks) — taunting, teasing, "
    "or coaxing the target, fitting whatever kind of creature or "
    "villain it is (a goblin might sound cocky and gleeful, a spider "
    "might just hiss or click, a boss-tier enemy might monologue a "
    "little). Keep it fun, alive, and a little funny where it fits — "
    "this is meant to make the fight more entertaining, not grim. "
    "Never let the line reveal hidden mechanics (exact HP, dice "
    "numbers, or what the attacker will do next) or contradict the "
    "real hit/miss and damage outcome you're narrating."
)


def _build_prompt(character: dict, action_text: str, mechanical_result: dict,
                   recent_events: list[str] | None = None, actor_personality: str | None = None,
                   location_description: str | None = None, include_banter: bool = False) -> str:
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
    banter_line = f"{_BANTER_INSTRUCTION}\n\n" if include_banter else ""
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

    # Real live bug (2026-08-15, dev-topic screenshot, TWICE now): "swings
    # and misses against Goblin Shaman 2!" while the real target (per the
    # deterministic block right below it) was Kess -- the model pulled a
    # name from Recent events (an earlier fight this same session) instead
    # of the real current target. mechanical_result's own "defender" key
    # already has the real name, but it was only ever available buried
    # inside the raw dict repr -- same fix shape as _NAMING_INSTRUCTION
    # already uses for the ACTOR's name: an explicit, labeled fact line
    # plus an instruction that this is the one authoritative source.
    defender_name = mechanical_result.get("defender")
    target_line = f"Target: {defender_name} -- this is the ONLY correct name for whoever is being acted upon in this narration; never substitute a different name, especially not one seen in Recent events below.\n\n" if defender_name else ""

    return (
        f"{_combat_preamble()}\n\n"
        f"Character: {character.get('name')} ({character.get('char_class')}), "
        f"HP: {character.get('hp_current')}/{character.get('hp_max')}\n"
        f"{_pronoun_line(character)}\n\n"
        f"{personality_line}"
        f"{banter_line}"
        f"{environment_line}"
        f"Player action: {action_text}\n\n"
        f"{target_line}"
        f"Mechanical result (already decided, narrate faithfully): {mechanical_result}\n\n"
        f"Tone guidance for this specific roll: {drama}\n\n"
        f"Recent events:\n{history}\n\n"
        f"Narrate this outcome now:"
    )


def narrate_action(character: dict, action_text: str, mechanical_result: dict,
                    recent_events: list[str] | None = None, actor_personality: str | None = None,
                    location_description: str | None = None, include_banter: bool = False) -> str:
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
    `include_banter` (see _BANTER_INSTRUCTION) asks the model to weave in
    one short in-character taunt/tease/coax line from the attacker —
    bot.py only ever sets this for enemy-side attackers, and only some
    of the time, never for a real player's own action.
    """
    prompt = _build_prompt(
        character, action_text, mechanical_result, recent_events, actor_personality,
        location_description, include_banter,
    )

    global _narration_call_active
    _narration_call_active = True
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
        text = strip_internal_jargon(strip_think_tags(data.get("response", "")))
        if text and not is_placeholder_text(text):
            return text
    except (requests.RequestException, ValueError) as e:
        print(f"[dm_agent] narration call failed, falling back to template: {e}")
    finally:
        _narration_call_active = False

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
        "in a 5th-edition-style tabletop role-playing game, in the moment BEFORE "
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


def _build_boss_decision_prompt(boss: dict, target: dict, spell_name: str | None = None) -> str:
    casting_line = (
        f"Real fact, already decided: this turn the boss is channeling a real spell -- {spell_name} -- "
        f"instead of a mundane weapon strike. Let the narration reflect real magic being cast, not a sword swing.\n"
        if spell_name else ""
    )
    return (
        f"{_boss_decision_preamble()}\n\n"
        f"Boss (the one deciding): {boss.get('name')}\n"
        f"Chosen target (already decided, do not change): {target.get('name')} "
        f"({target.get('hp_current')}/{target.get('hp_max', target.get('hp_current'))} HP)\n"
        f"{casting_line}\n"
        f"Narrate the boss's decision now (no dice rolled yet):"
    )


def narrate_boss_decision(boss: dict, target: dict, spell_name: str | None = None) -> str:
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
    blocks the actual attack resolution that follows. `spell_name`
    (2026-08-13, per Coffee: "bosses shud def have spells and
    abilities, narrations shud work this in") -- bot.py now decides
    BEFORE this call whether the boss casts a real known spell this
    turn (_decide_monster_spell), so this pre-roll beat can genuinely
    foreshadow real magic rather than staying silent about it and only
    revealing "casts X" after the fact.
    """
    prompt = _build_boss_decision_prompt(boss, target, spell_name)

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
        text = strip_internal_jargon(strip_think_tags(data.get("response", "")))
        if text and not is_placeholder_text(text):
            return text
    except (requests.RequestException, ValueError) as e:
        print(f"[dm_agent] boss decision narration call failed, falling back to template: {e}")

    return f"{boss.get('name')} sets its sights on {target.get('name')}."


def _welcome_preamble() -> str:
    return (
        "You are the Dungeon Master opening a brand-new character's journey "
        "into a 5th-edition-style tabletop role-playing game — this is the first page "
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
        text = strip_internal_jargon(strip_think_tags(data.get("response", "")))
        if text and not is_placeholder_text(text):
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
        text = strip_internal_jargon(strip_think_tags(data.get("response", "")))
        if text and not is_placeholder_text(text):
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
        "player of a 5th-edition-style tabletop role-playing game, in the voice of a "
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


def _next_step_hint_preamble(is_puzzle: bool, quest_name: str | None) -> str:
    quest_clause = (
        f" You are also given the real Quest name, \"{quest_name}\" -- naming it plainly is NOT a spoiler either."
        if quest_name else ""
    )
    if is_puzzle:
        return (
            "You are the Dungeon Master giving a player a short, in-character "
            "hint about what comes next in their 5th-edition-style tabletop "
            "role-playing game. What comes next is a PUZZLE -- you are given its "
            "real Location and its real riddle text as a Clue." + quest_clause + " A player "
            "reading this because they feel lost needs to be able to actually "
            "act on it, so you MUST plainly name the real Location given "
            "below somewhere in your answer, so they know where to go -- "
            "naming the location is NOT a spoiler; only the puzzle's own "
            "answer is a spoiler. Write "
            f"{scaled_sentences(1, 2)} that name that real place and evoke "
            "mood and mystery about what waits there — vague and ominous "
            "about the puzzle itself, never simple — and you may hint at HOW "
            "one might go about puzzling it out (thinking carefully, "
            "listening, looking for a pattern), but you must NEVER state or "
            f"imply the actual answer. {style_directive()} Output ONLY the "
            "hint itself, no preamble, no chapter title, no quotation marks "
            "around it."
        )
    return (
        "You are the Dungeon Master giving a player a short, in-character "
        "hint about what comes next in their 5th-edition-style tabletop role-playing "
        "game. You are given a real location and a real clue about what "
        "waits there." + quest_clause + f" Write {scaled_sentences(1, 2)} that are clear and "
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
    if next_step.get("quest_name"):
        facts += f"Quest: {next_step['quest_name']}\n"
    if next_step.get("clue"):
        facts += f"Clue: {next_step['clue']}"
    return (
        f"{_next_step_hint_preamble(next_step['is_puzzle'], next_step.get('quest_name'))}\n\n"
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
        text = strip_internal_jargon(strip_think_tags(data.get("response", "")))
        if text and not is_placeholder_text(text):
            return _ensure_next_step_facts_present(text, next_step)
    except (requests.RequestException, ValueError) as e:
        print(f"[dm_agent] next-step-hint narration call failed, falling back to template: {e}")
    # Real live report (2026-09-02, Coffee): this fallback used to
    # include next_step["clue"] verbatim even for a puzzle -- for a
    # normal quest that's the intended, deterministic hint text, but
    # for a puzzle it's the literal riddle question, spilled in full
    # the moment Ollama times out, contradicting the real narration
    # call's own explicit "be vague and ominous for a puzzle"
    # instruction (_build_next_step_hint_prompt) purely by accident of
    # WHICH path happened to run. The fallback now stays exactly as
    # vague for a puzzle as the real narration is meant to be -- no
    # clue text at all, just the honest "something unsolved" flavor --
    # and only includes the real clue for the non-puzzle case, where
    # showing it plainly was always the actual design.
    if next_step["is_puzzle"]:
        base = "Something unanswered still waits, patient and unwilling to make itself easy."
    else:
        where = f" toward {next_step['location_name']}" if next_step.get("location_name") else ""
        base = f"The path ahead leads{where}. {next_step.get('clue', '')}"
    return _ensure_next_step_facts_present(base, next_step)


def _ensure_next_step_facts_present(text: str, next_step: dict) -> str:
    """
    Real live report (2026-09-02, Coffee, dev-bridge): a live puzzle
    hint came back as "The enigmatic site whispers its truth through
    shadows" -- no location named anywhere, despite the prompt's own
    explicit "you MUST plainly name the real Location" instruction.
    `lfm2.5-thinking` doesn't reliably follow that instruction every
    time, and there's no retry budget for a call already this slow
    (see CLAUDE.md) -- so this is a deterministic backstop, not a
    replacement for the prompt: if the model's own text doesn't
    actually contain the real location/quest name, append them
    plainly rather than trusting compliance alone. A no-op whenever
    the model DID name them (the normal, hoped-for case).
    """
    lowered = text.lower()
    missing = []
    if next_step.get("location_name") and next_step["location_name"].lower() not in lowered:
        missing.append(f"📍 {next_step['location_name']}")
    if next_step.get("quest_name") and next_step["quest_name"].lower() not in lowered:
        missing.append(f"📜 {next_step['quest_name']}")
    if not missing:
        return text
    return text + "\n(" + " — ".join(missing) + ")"


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
        text = strip_internal_jargon(strip_think_tags(data.get("response", "")))
        if text and not is_placeholder_text(text):
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
        text = strip_internal_jargon(strip_think_tags(data.get("response", "")))
        if text and not is_placeholder_text(text):
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
        text = strip_internal_jargon(strip_think_tags(data.get("response", "")))
        if text and not is_placeholder_text(text):
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
        text = strip_internal_jargon(strip_think_tags(data.get("response", "")))
        if text and not is_placeholder_text(text):
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
        text = strip_internal_jargon(strip_think_tags(data.get("response", "")))
        if text and not is_placeholder_text(text):
            return text
    except (requests.RequestException, ValueError) as e:
        print(f"[dm_agent] chapter climax narration failed, falling back to template: {e}")
    return f"This was a turning point. {quest_description}"


def _reach_location_quest_preamble() -> str:
    return (
        "You are the Dungeon Master narrating what a character actually "
        "finds the moment they arrive somewhere and a quest tied to that "
        "arrival completes -- a real discovery beat, not a routine travel "
        "line. You are given the arriving character's real name, the "
        "quest's real title, what it was actually about, and (when "
        "given) a real clue tied to it; the ARRIVING character is always "
        "the one doing the finding/seeing -- any other named person in "
        "the quest's own description is backstory (who assigned this, or "
        "who it's about), never who is physically arriving here now. "
        f"Narrate ONLY the given facts ({scaled_sentences(2, 4)}), "
        "describing what the arriving character actually sees or learns, "
        f"never inventing a new plot detail, character, or twist beyond "
        f"what's given. {_NAMING_INSTRUCTION} {style_directive()}"
    )


def _build_reach_location_quest_prompt(
    character_name: str, quest_title: str, quest_description: str, location_name: str, clue: str | None = None,
) -> str:
    clue_line = f"A real clue tied to this: {clue}\n" if clue else ""
    return (
        f"{_reach_location_quest_preamble()}\n\n"
        f"Real facts (narrate ONLY these, faithfully):\n"
        f"Who just arrived (the one doing the finding): {character_name}\n"
        f"Quest just completed: {quest_title}\n"
        f"What it was about: {quest_description}\n"
        f"Where they just arrived: {location_name}\n"
        f"{clue_line}\n"
        f"Write what {character_name} finds now:"
    )


def narrate_reach_location_quest_completion(
    character_name: str, quest_title: str, quest_description: str, location_name: str, clue: str | None = None,
) -> str:
    """
    Real live request (2026-08-28, Coffee: a reach_location quest like
    "More Than Banditry" completed the instant a character walked into
    Greymoor Downs with zero real payoff, even though its own
    description promises an actual investigation -- "why did we
    complete the quest?? what happened?"). Every non-climactic
    reach_location quest (climactic ones already get real narration via
    narrate_chapter_climax) now gets a real, grounded arrival beat here
    instead of a silent stat-only completion -- grounded ONLY in the
    quest's own real title/description/clue, never inventing new plot.

    character_name (2026-08-28, same-day live follow-up: the first
    version of this omitted who actually arrived, and the model latched
    onto the quest-giver NPC named in the description instead -- "Borin
    arrives at Greymoor Downs..." when Borin never left the tavern, the
    PLAYER did) -- an explicit, unambiguous "who is arriving" fact fixes
    the model's real, observed subject confusion, same fix shape as
    narrate_boss_confrontation's own party_names fact.
    """
    prompt = _build_reach_location_quest_prompt(character_name, quest_title, quest_description, location_name, clue)
    try:
        response = requests.post(
            f"{config.OLLAMA_BASE_URL}/api/generate",
            json={"model": config.DM_NARRATION_MODEL, "prompt": prompt, "stream": False, "options": _NARRATION_OPTIONS},
            timeout=200,
        )
        response.raise_for_status()
        data = response.json()
        text = strip_internal_jargon(strip_think_tags(data.get("response", "")))
        if text and not is_placeholder_text(text):
            return text
    except (requests.RequestException, ValueError) as e:
        print(f"[dm_agent] reach-location quest narration failed, falling back to template: {e}")
    return quest_description


def _boss_intro_preamble() -> str:
    return (
        "You are the Dungeon Master narrating the dramatic ENTRANCE of a "
        "real boss monster the party is about to fight -- a genuinely "
        "epic, tense moment, not a routine encounter. You are given the "
        "boss's real name, the real place this is happening, that "
        "place's own real description, and (when given) a real fact "
        "about this boss's own established powers; narrate ONLY these "
        f"facts ({scaled_sentences(3, 5, boost=3)}), building real dread "
        "and stakes without resolving the fight or inventing a new plot "
        f"detail, character, or twist beyond what's given. {_NAMING_INSTRUCTION} "
        f"{style_directive(boost=3)}"
    )


def _build_boss_intro_prompt(
    monster_name: str, location_name: str, location_description: str, ability_facts: str | None = None,
) -> str:
    ability_line = f"What this boss is known to do: {ability_facts}\n" if ability_facts else ""
    return (
        f"{_boss_intro_preamble()}\n\n"
        f"Real facts (narrate ONLY these, faithfully):\n"
        f"The boss: {monster_name}\n"
        f"Where this is happening: {location_name}\n"
        f"What this place is really like: {location_description}\n"
        f"{ability_line}\n"
        f"Write the entrance now:"
    )


def narrate_boss_intro(
    monster_name: str, location_name: str, location_description: str, ability_facts: str | None = None,
) -> str:
    """
    A real, distinct cinematic beat the moment a fight against an
    is_boss monster actually begins (2026-07-27, per Coffee: "make
    sure all boss sequences are creative and scary... make it epic").
    Separate from the plain "Combat Begins!" header every fight
    already gets -- grounded only in the boss's own real name and the
    real location's own already-written description, never inventing
    new lore for the moment. `ability_facts` (2026-08-13, per Coffee:
    "bosses shud def have spells and abilities, narrations shud work
    this in") -- a real, pre-built fact string (bot.py's
    _boss_ability_facts) naming this boss's own actual known spells/
    signature mechanics, when it has any; the model weaves it in as
    real foreshadowing rather than generic "epic boss" filler.
    """
    prompt = _build_boss_intro_prompt(monster_name, location_name, location_description, ability_facts)
    try:
        response = requests.post(
            f"{config.OLLAMA_BASE_URL}/api/generate",
            json={"model": config.DM_NARRATION_MODEL, "prompt": prompt, "stream": False, "options": _NARRATION_OPTIONS},
            timeout=200,
        )
        response.raise_for_status()
        data = response.json()
        text = strip_internal_jargon(strip_think_tags(data.get("response", "")))
        if text and not is_placeholder_text(text):
            return text
    except (requests.RequestException, ValueError) as e:
        print(f"[dm_agent] boss intro narration failed, falling back to template: {e}")
    return f"**{monster_name}** makes its presence known. This is going to be a real fight."


def _boss_defeat_preamble() -> str:
    return (
        "You are the Dungeon Master narrating the dramatic DEFEAT of a "
        "real boss monster the party just won a real fight against -- a "
        "genuinely epic, satisfying moment, not a routine kill. You are "
        "given the boss's real name, the real place this happened, and "
        "(when given) a real fact about the powers this boss actually "
        f"wielded in the fight; narrate ONLY these facts ({scaled_sentences(3, 5, boost=3)}), "
        "giving this victory real weight without inventing a new plot "
        f"detail, character, or twist beyond what's given. {_NAMING_INSTRUCTION} "
        f"{style_directive(boost=3)}"
    )


def _build_boss_defeat_prompt(monster_name: str, location_name: str, ability_facts: str | None = None) -> str:
    ability_line = f"What this boss wielded in the fight: {ability_facts}\n" if ability_facts else ""
    return (
        f"{_boss_defeat_preamble()}\n\n"
        f"Real facts (narrate ONLY these, faithfully):\n"
        f"The boss just defeated: {monster_name}\n"
        f"Where this happened: {location_name}\n"
        f"{ability_line}\n"
        f"Write the defeat now:"
    )


def narrate_boss_defeat(monster_name: str, location_name: str, ability_facts: str | None = None) -> str:
    """
    The epic counterpart to narrate_boss_intro -- fires the moment an
    is_boss monster is actually defeated, distinct from the plain
    "X has been defeated!" line every other monster gets. `ability_facts`
    (2026-08-13, per Coffee): same real, pre-built fact string as
    narrate_boss_intro, letting the defeat narration reference what the
    party actually overcame instead of a generic win.
    """
    prompt = _build_boss_defeat_prompt(monster_name, location_name, ability_facts)
    try:
        response = requests.post(
            f"{config.OLLAMA_BASE_URL}/api/generate",
            json={"model": config.DM_NARRATION_MODEL, "prompt": prompt, "stream": False, "options": _NARRATION_OPTIONS},
            timeout=200,
        )
        response.raise_for_status()
        data = response.json()
        text = strip_internal_jargon(strip_think_tags(data.get("response", "")))
        if text and not is_placeholder_text(text):
            return text
    except (requests.RequestException, ValueError) as e:
        print(f"[dm_agent] boss defeat narration failed, falling back to template: {e}")
    return f"**{monster_name}** falls. A real, hard-won victory."


def _boss_summon_preamble() -> str:
    return (
        "You are the Dungeon Master narrating a badly wounded boss "
        "monster calling for reinforcements mid-fight -- a real, tense "
        "escalation, not routine. You are given the boss's real name and "
        "the real names of the reinforcements that actually arrive; "
        f"narrate ONLY these facts ({scaled_sentences(2, 3)}), making the "
        "call for backup feel desperate and real without resolving any "
        "future attack or inventing a new plot detail, character, or "
        f"twist beyond what's given. {_NAMING_INSTRUCTION} {style_directive()}"
    )


def _build_boss_summon_prompt(boss_name: str, minion_names: str) -> str:
    return (
        f"{_boss_summon_preamble()}\n\n"
        f"Real facts (narrate ONLY these, faithfully):\n"
        f"The boss calling for backup: {boss_name}\n"
        f"Reinforcements that answer the call: {minion_names}\n\n"
        f"Write the moment now:"
    )


def narrate_boss_summon(boss_name: str, minion_names: str) -> str:
    """
    Real boss summons woven into the story (2026-08-13, per Coffee:
    "are the summons worked into the story line?" -- confirmed live they
    weren't: _maybe_summon_minions only ever sent a plain deterministic
    line, unlike every other boss-tier moment (intro/defeat/decision),
    which all get a real AI-narrated beat. Same epic-moment pattern as
    narrate_boss_defeat -- the plain "calls for reinforcements" line
    bot.py already sends stays (reliable, always-present information),
    this ADDS a real narrated flourish alongside it, grounded only in
    the boss's real name and the real reinforcements that actually
    joined (never inventing a new monster or plot beat).
    """
    prompt = _build_boss_summon_prompt(boss_name, minion_names)
    try:
        response = requests.post(
            f"{config.OLLAMA_BASE_URL}/api/generate",
            json={"model": config.DM_NARRATION_MODEL, "prompt": prompt, "stream": False, "options": _NARRATION_OPTIONS},
            timeout=200,
        )
        response.raise_for_status()
        data = response.json()
        text = strip_internal_jargon(strip_think_tags(data.get("response", "")))
        if text and not is_placeholder_text(text):
            return text
    except (requests.RequestException, ValueError) as e:
        print(f"[dm_agent] boss summon narration failed, falling back to template: {e}")
    return f"**{boss_name}**, badly wounded, calls for reinforcements!"


def _remnant_summon_preamble() -> str:
    return (
        "You are the Dungeon Master narrating the exact moment a bound "
        "Remnant -- a fragment of an ancient, boss-tier creature the "
        "player's party once genuinely defeated -- is called into a "
        "current battle and turns on the enemy it now faces. You are "
        "given the Remnant's real name, its real lore, and the real "
        "name of the enemy it's about to strike; narrate ONLY these "
        f"facts ({scaled_sentences(1, 2)}), as a short, menacing line "
        "the Remnant itself directs AT that enemy -- ancient, "
        "otherworldly, never friendly banter -- without resolving the "
        "attack's outcome (damage/hit/miss) or inventing a new plot "
        f"detail, character, or twist beyond what's given. {_NAMING_INSTRUCTION} {style_directive()}"
    )


def _build_remnant_summon_prompt(remnant_name: str, remnant_lore: str, target_name: str) -> str:
    return (
        f"{_remnant_summon_preamble()}\n\n"
        f"Real facts (narrate ONLY these, faithfully):\n"
        f"The Remnant being summoned: {remnant_name}\n"
        f"Its real lore: {remnant_lore}\n"
        f"The enemy it now turns on: {target_name}\n\n"
        f"Write the Remnant's line now:"
    )


def narrate_remnant_summon(remnant_name: str, remnant_lore: str, target_name: str) -> str:
    """
    Real feature request (2026-08-20, per Coffee: "when we use a
    Remnant inclde a narration from the Remnant to the current battle
    enemy it is facing"). Same real epic-moment pattern as
    narrate_boss_summon right above -- grounded only in the Remnant's
    own real name/lore (remnants.py) and the real target it's actually
    facing this cast, never inventing a new detail. The plain damage
    line bot.py already sends stays (reliable, always-present
    information); this adds a real narrated line alongside it.
    """
    prompt = _build_remnant_summon_prompt(remnant_name, remnant_lore, target_name)
    try:
        response = requests.post(
            f"{config.OLLAMA_BASE_URL}/api/generate",
            json={"model": config.DM_NARRATION_MODEL, "prompt": prompt, "stream": False, "options": _NARRATION_OPTIONS},
            timeout=200,
        )
        response.raise_for_status()
        data = response.json()
        text = strip_internal_jargon(strip_think_tags(data.get("response", "")))
        if text and not is_placeholder_text(text):
            return text
    except (requests.RequestException, ValueError) as e:
        print(f"[dm_agent] remnant summon narration failed, falling back to template: {e}")
    return f"**{remnant_name}** turns its full attention on **{target_name}**."


def narrate_labyrinth_segment_flavor(theme_name: str, theme_intro: str, antagonist_name: str, antagonist_lore: str, segment: int) -> str:
    """
    Phase L3 (2026-09-02, per Coffee: "the labyrinth generator can use
    a[n] AI to make themes and storylines for the levels... but don't
    use characters that are in the storyline, make sure they are
    separate from the actual game"). A real, OPTIONAL flavor line for
    a brand new segment -- grounded ONLY in the segment's own real
    theme name/intro (rules.labyrinth.LABYRINTH_THEMES) and the one
    real, Labyrinth-exclusive antagonist entity (never a main-story
    NPC/boss, confirmed by rules.labyrinth's own module docstring).
    Fired fire-and-forget, same pattern as narrate_remnant_summon
    above -- the deterministic entry message already covers the real
    mechanical facts (floor number, room description); this only ever
    adds atmosphere on top, arriving whenever Ollama finishes.
    """
    prompt = (
        "You are the Dungeon Master narrating a player's arrival in a brand new stretch of a "
        "vast, ever-shifting Labyrinth -- a pocket dimension, not the main world. You are given "
        f"this stretch's real Theme name and its own real Intro flavor, plus one real, recurring "
        f"in-fiction entity blamed for the Labyrinth's endless, malfunctioning reshaping: "
        f"{antagonist_name} ({antagonist_lore}).\n\n"
        f"Theme: {theme_name}\nIntro: {theme_intro}\n\n"
        f"Write {scaled_sentences(1, 2)} of atmospheric flavor for arriving here, in the voice of "
        f"the Dungeon Master. Lean into real dungeon-crawl imagery -- ancient mechanisms, sealed "
        f"archways, the sense of a real structure built with real dangers waiting deeper in -- so "
        f"this reads as an explorable dungeon, not generic horror or mystery prose. You may "
        f"reference {antagonist_name} obliquely (its handiwork, not a direct confrontation) but "
        f"never invent a new named character, faction, or plot detail beyond what's given. "
        f"{style_directive()} Output ONLY the line itself, no preamble, no quotation marks."
    )
    try:
        response = requests.post(
            f"{config.OLLAMA_BASE_URL}/api/generate",
            json={"model": config.DM_NARRATION_MODEL, "prompt": prompt, "stream": False, "options": _NARRATION_OPTIONS},
            timeout=200,
        )
        response.raise_for_status()
        data = response.json()
        text = strip_internal_jargon(strip_think_tags(data.get("response", "")))
        if text and not is_placeholder_text(text):
            return text
    except (requests.RequestException, ValueError) as e:
        print(f"[dm_agent] labyrinth segment flavor narration failed, falling back to template: {e}")
    return theme_intro


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


def _build_arc_opening_prompt(arc_title: str, arc_description: str, quest_title: str, character_name: str) -> str:
    return (
        f"{_arc_opening_preamble()}\n\n"
        f"Real facts (narrate ONLY these, faithfully):\n"
        f"Character: {character_name}\n"
        f"New chapter beginning: {arc_title}\n"
        f"What this chapter is about: {arc_description}\n"
        f"The quest that opens it: {quest_title}\n\n"
        f"Write the opening now:"
    )


def narrate_arc_opening(arc_title: str, arc_description: str, quest_title: str, character_name: str) -> str:
    """
    Cutscene-style bookend to narrate_chapter_climax's ending flourish:
    fires once, the moment a character accepts the FIRST quest of a new
    story arc (see bot.py's _arc_opening_note), giving that transition a
    real narrated beat instead of just a quest-accept line -- per
    Coffee's request for RPG-style cutscenes on story beats (2026-07-19/
    20). Same rules-decide/AI-narrates split as every other narration
    call: the arc's title/description and the quest's title are already-
    decided real facts, never invented here.

    Real live bug (2026-08-20, Coffee, screenshot: "I think the
    narration made a mistake"): this prompt told the model, via
    _NAMING_INSTRUCTION, to always use "whatever is given on the
    Character: line" -- but never actually included a Character: line
    at all, unlike every other narration call in this file. With no
    real name to anchor to, the model invented one ("Aria") instead of
    the real player's character. character_name is now a required real
    fact here too, same as everywhere else.
    """
    prompt = _build_arc_opening_prompt(arc_title, arc_description, quest_title, character_name)
    try:
        response = requests.post(
            f"{config.OLLAMA_BASE_URL}/api/generate",
            json={"model": config.DM_NARRATION_MODEL, "prompt": prompt, "stream": False, "options": _NARRATION_OPTIONS},
            timeout=200,
        )
        response.raise_for_status()
        data = response.json()
        text = strip_internal_jargon(strip_think_tags(data.get("response", "")))
        if text and not is_placeholder_text(text):
            return text
    except (requests.RequestException, ValueError) as e:
        print(f"[dm_agent] arc opening narration failed, falling back to template: {e}")
    return f"A new chapter begins. {arc_description}"


def _boss_confrontation_preamble(escalated: bool) -> str:
    stage = (
        "This is the SECOND, ESCALATED confrontation -- the boss has "
        "changed since the party last faced them, per the real facts "
        "given below. Make the change felt: this is no longer the same "
        "threat it was, without inventing any detail beyond what's given."
        if escalated else
        "This is the FIRST real confrontation with this boss -- a "
        "dramatic, cinematic entrance, not a routine ambush."
    )
    return (
        "You are the Dungeon Master narrating a real, high-stakes "
        "confrontation cutscene, right before combat begins. You are "
        f"given the boss's real personality and goals, the real party "
        f"members present, and the real location. {stage} Narrate ONLY "
        f"these facts ({scaled_sentences(4, 6, boost=2)}) -- epic and "
        "cinematic in tone, making the stakes and the boss's own "
        "menace felt, but never inventing a new plot detail, twist, or "
        "backstory beyond what's given, and never deciding or "
        "describing the fight's outcome (that hasn't happened yet). "
        f"{style_directive(boost=2)}"
    )


def _build_boss_confrontation_prompt(
    boss_name: str, boss_personality: str, boss_goals: str,
    party_names: str, location_name: str, escalated: bool,
) -> str:
    return (
        f"{_boss_confrontation_preamble(escalated)}\n\n"
        f"Real facts (narrate ONLY these, faithfully):\n"
        f"The boss: {boss_name}\n"
        f"Their real personality: {boss_personality}\n"
        f"Their real goals: {boss_goals}\n"
        f"The party present: {party_names}\n"
        f"Location: {location_name}\n\n"
        f"Write the confrontation now:"
    )


def narrate_boss_confrontation(
    boss_name: str, boss_personality: str, boss_goals: str,
    party_names: str, location_name: str, escalated: bool = False,
) -> str:
    """
    Real live request (2026-08-27, Coffee: "I want this to feel like
    the players have to walk in and the other AI players can narrate
    and act out the dialog sequences and cutscenes. make it feel epic
    for the player" -- "use the Kess sequences to really tell a story
    ... make them WANT to save the world"). A real, dedicated pre-fight
    cutscene beat, same rules-decide/AI-narrates split as
    narrate_arc_opening right above -- the boss's own real personality/
    goals fields (already hand-authored in campaign.json, e.g. Kess's
    "collecting tolls to fund something far bigger than banditry", or
    Kess the Unbound's "the calculation is gone, something underneath
    is doing the deciding now") are the only real facts given; this
    just gives them a genuine dramatic spotlight instead of staying
    buried in data nobody ever sees narrated. `escalated=True` for a
    boss's SECOND, changed appearance (e.g. Kess the Unbound) --
    without it, this call would have no way to know the boss it's
    narrating isn't being met for the first time.
    """
    prompt = _build_boss_confrontation_prompt(boss_name, boss_personality, boss_goals, party_names, location_name, escalated)
    try:
        response = requests.post(
            f"{config.OLLAMA_BASE_URL}/api/generate",
            json={"model": config.DM_NARRATION_MODEL, "prompt": prompt, "stream": False, "options": _NARRATION_OPTIONS},
            timeout=200,
        )
        response.raise_for_status()
        data = response.json()
        text = strip_internal_jargon(strip_think_tags(data.get("response", "")))
        if text and not is_placeholder_text(text):
            return text
    except (requests.RequestException, ValueError) as e:
        print(f"[dm_agent] boss confrontation narration failed, falling back to template: {e}")
    return f"**{boss_name}** turns to face the party, {boss_goals.split('--')[0].strip().rstrip('.')}."



# ---------------------------------------------------------------------
# The Kess Arc, Phase 2 (2026-08-28, per Coffee, after live-testing the
# AI-generated versions of these exact beats and finding them genuinely
# unreliable -- wrong pronouns for Kess, empty responses, invented
# details like "a clock struck three" that violated the "never invent"
# instruction anyway): "you dont need to use ollama calls for an
# outcome or response that we have already determined via story line."
#
# Every beat below is a REAL, one-time, fully-determined story moment
# (there is exactly one "party confronts Kess for the first time,"
# exactly one "Kess flees and something else answers," exactly one
# real ending to this chapter) -- so it's hand-written here, the same
# way `the_unasked`/`the_unbegun`'s own bespoke endings in bot.py are
# already fully hand-written prose with zero Ollama calls. This
# guarantees the FF6-style script format, the Kafka-inspired calm/
# philosophical voice for Kess (2026-08-28 dev-bridge reference: "Use
# this as an example on how to characteristically build Kess"), and
# the exact tone Coffee approved live, 100% of the time -- no risk of
# a wrong pronoun, an empty response, or an invented detail. Only
# `{character_name}`/party facts are ever substituted in, via plain
# Python formatting, never an LLM call.
# ---------------------------------------------------------------------

_BORIN_KESS_ARC_LINES = {
    "borins_blackthorn_warning": {
        "low": "\"You want to know why I'm sending you out there? Go look. You'll see it yourselves, or you won't.\"",
        "mid": "\"The Blackthorn Raiders aren't just bandits -- too well-fed, too well-armed, and the coin never stays with them. Go see for yourselves.\"",
        "high": "\"I've watched that road for years, {name}. Real banditry doesn't look like that -- well-fed, well-armed, throwing away coin they never keep. Someone's paying for it. I need eyes I trust out there.\"",
    },
    "kess_first_reckoning": {
        "low": "\"There's a name on this page. Not one I know. That's all you need.\"",
        "mid": "\"This ledger names someone I don't recognize -- real proof Kess answers to somebody else. Stop her, question her, before whatever she's funding gets any further.\"",
        "high": "\"{name}, look at this. A name I don't recognize, but real proof -- Kess answers to someone else entirely. Whoever's holding her leash won't show themselves while she's still standing. I need her stopped. I need her questioned. And I trust you to do both.\"",
    },
    "kess_the_unbound_reckoning": {
        "low": "\"Finish it. That's all I've got.\"",
        "mid": "\"Whatever we questioned back there -- it's gone. What's left has to be finished before it remembers how to run.\"",
        "high": "\"{name}... that was never just a bandit captain. I should have seen it sooner. Go. Finish it -- and come back.\"",
    },
}


def narrate_borin_dialogue(
    character_name: str, trust_band: str, quest_title: str, quest_description: str, quest_clue: str | None = None,
) -> str:
    """
    Real live report (2026-08-28, Coffee, dev-bridge screenshot): Borin
    hallucinated a blended, nonsensical line ("the Name on the Page
    task needs vouchering for Kess... seek Charvenna... stop your
    oversight") via the generic talk_to_npc path's own cross-topic
    conversation memory.

    Hand-written, not AI-generated (Kess Arc Phase 2, per Coffee: "you
    dont need to use ollama calls for an outcome or response that we
    have already determined via story line") -- Borin's real line for
    each of the three real Kess-arc quest stages, banded by his own
    real companion trust (_companion_trust_band, the SAME stat borins_
    resolution already uses), with only the speaking character's name
    substituted in. Falls back to the quest's own real description if
    somehow called for a quest_id outside this real three-quest chain
    (should never happen in practice -- _active_kess_arc_quest only
    ever returns one of these three).
    """
    lines_by_band = _BORIN_KESS_ARC_LINES.get(
        _kess_arc_quest_id_for_title(quest_title),
    )
    if lines_by_band is None:
        return quest_description
    return lines_by_band[trust_band].format(name=character_name)


def _kess_arc_quest_id_for_title(quest_title: str) -> str | None:
    """Reverse lookup from a quest's real title back to its id -- avoids a second parameter just for this internal dict key."""
    titles = {
        "More Than Banditry": "borins_blackthorn_warning",
        "The Name on the Page": "kess_first_reckoning",
        "What Answered Instead": "kess_the_unbound_reckoning",
    }
    return titles.get(quest_title)


def kess_first_confrontation_script(character_name: str) -> str:
    """
    Hand-written FF6-style confrontation script for the party's FIRST
    real meeting with Kess (Kess Arc Phase 2) -- the Kafka (Honkai:
    Star Rail) voice reference Coffee gave (2026-08-28 dev-bridge:
    "Use this as an example on how to characteristically build Kess"):
    calm manipulation over snarling threats, references to a script
    she's following that isn't her own. Grounded in her real,
    already-written campaign.json goals ("collecting tolls to fund
    something far bigger than banditry") -- the ledger reference is
    the party's own real earned knowledge (torn_ledger_page), not
    invented here.

    Per Coffee (2026-08-28): "I want to see a progression showing her
    as a human... make it seem like the players can save her or offer
    hope." This is the FIRST beat of that arc -- still visibly a
    person, still capable of a real choice, the strain of that only
    barely showing. Fires once, right as her fight actually begins.
    """
    return (
        "*The road narrows here. A woman is already waiting at the crossing, "
        "unhurried, like she's been expecting exactly this arrival and no other.*\n\n"
        "**Kess:** \"You found the ledger. I wondered how long that would take.\"\n\n"
        "*She doesn't reach for a weapon yet -- for a moment she just looks tired, "
        "like someone who stopped being able to remember why she started.*\n\n"
        "**Kess:** \"Everyone who crosses here pays a toll. You're about to learn "
        "mine was never really the point.\""
    )


def kess_unbound_confrontation_script(character_name: str) -> str:
    """
    The second, escalated confrontation script -- Kess the Unbound's
    own real, changed voice (calculation gone, something else
    deciding), same hand-written discipline as kess_first_
    confrontation_script. Per Coffee (2026-08-28): the party's own
    Remnants ("Whispers of the Universe" -- the in-fiction name this
    arc uses for them) are what she's been reaching for the whole
    time; the more the party carries, the further gone she already is.
    """
    return (
        "*What's left of Kess is still standing where she fell -- taller now, "
        "waiting, in no hurry at all. The air around her hums, the same low "
        "note a Whisper of the Universe makes right before it answers.*\n\n"
        "**???:** \"She said this was where it ended. She was almost right.\"\n\n"
        "*Its voice still has the shape of hers. Nothing else does.*\n\n"
        "**???:** \"You carry the same voices I do. I can hear them from here. "
        "Finish it, then -- she would have wanted someone to.\""
    )


_CONFRONTATION_CHOICE_SCRIPTS = {
    "confront": (
        "**{name}:** \"Whoever you're funding -- it ends here.\"\n\n"
        "**Kess:** \"Ends. That's an interesting word for someone who doesn't know what they're ending.\"\n\n"
        "*The party presses the advantage while she's still talking -- {mechanical_outcome}*"
    ),
    "reason": (
        "**{name}:** \"You don't have to do this. Whoever's pulling the strings -- you could still walk away.\"\n\n"
        "*For just a moment, something flickers behind her eyes -- gone as fast as it came.*\n\n"
        "**Kess:** \"...No. I don't think I could.\"\n\n"
        "*{mechanical_outcome}*"
    ),
}


def narrate_confrontation_choice_outcome(
    choice: str, character_name: str, mechanical_outcome: str,
) -> str:
    """
    Hand-written, not AI-generated (Kess Arc Phase 2) -- real live
    testing found the AI version genuinely unreliable for a moment this
    important (wrong pronouns, empty responses). Both real branches
    ("confront"/"reason") are fully determined by which button the
    player actually tapped, so there's nothing here an LLM call could
    legitimately vary; `mechanical_outcome` is the one real, already-
    decided fact (bot.py computes it BEFORE this is ever called) woven
    into the fixed script as a plain sentence.
    """
    script = _CONFRONTATION_CHOICE_SCRIPTS.get(choice, _CONFRONTATION_CHOICE_SCRIPTS["confront"])
    return script.format(name=character_name, mechanical_outcome=mechanical_outcome)


def kess_shrine_vigil_script(character_name: str) -> str:
    """
    Chapter 6 expansion, Phase 4 (2026-08-30, per the approved plan's
    "Story & character craft" section) -- the single clearest "why is
    Kess evil" beat in the whole Chapter 3-8 arc, delivered by staging
    rather than exposition: the party finds her already at the same
    kind of shrine they themselves use, alone, unguarded, mid-vigil --
    not fighting, not taunting, genuinely at rest for the length of a
    few real lines. This is a pure narrative beat, not a fight (no
    combat triggers here); she notices the party and the moment breaks
    on its own, same real "flees rather than dies" discipline her
    actual boss fights already use, just without a fight ever starting
    at all. Hand-written, zero Ollama calls, same voice established by
    kess_first_confrontation_script -- calm, controlled, a script she's
    following that isn't her own, "Whispers of the Universe" the same
    in-fiction Remnant term her later scenes already use.
    """
    return (
        "*The shrine ahead is lit, faintly, by something that isn't a torch. A woman kneels at it, "
        "alone, head bowed -- not praying, exactly. Listening.*\n\n"
        f"*{character_name} recognizes her before she ever looks up. Kess.*\n\n"
        "**Kess:** \"...They're louder down here. The Whispers. I didn't think that was possible "
        "this far from the surface.\"\n\n"
        "*She doesn't reach for a weapon. For a moment she just sounds tired, the calculation gone "
        "out of her voice entirely.*\n\n"
        "**Kess:** \"I used to think I was the one asking them for something. Lately I can't tell "
        "anymore which direction that actually runs.\"\n\n"
        "*She hears them move, and the moment closes over as fast as it opened -- the tiredness gone, "
        "replaced by the same unhurried calm as always.*\n\n"
        "**Kess:** \"You shouldn't have seen that. Pretend you didn't, and I'll pretend the same about "
        "you being here at all.\"\n\n"
        "*She's already gone before anyone can answer -- not fled, exactly. Excused herself.*"
    )


def kess_flees_line() -> str:
    """
    Hand-written (Kess Arc Phase 2, per Coffee: "if KESS is not
    supposed to be killed, make sure they leave the battle -- you can
    continue any cut-scene also after battle ends"). Kess's first form
    flees at 0 HP rather than dying outright -- sets up the real
    transformation beat (narrate_kess_transformation) as something
    that happens to her after she runs, not a corpse reanimating.
    """
    return (
        "*Kess staggers back, blade dropping from her grip.*\n\n"
        "**Kess:** \"This isn't -- this was never supposed --\"\n\n"
        "*She turns and bolts into the fog before anyone can stop her. "
        "Whatever she was about to say, she didn't get to finish it.*"
    )


def kess_scouting_confrontation_script(character_name: str) -> str:
    """
    Chapter 7 expansion, Phase 5 (2026-08-30, per the approved plan's
    "Beat 3 -- A Warning Shot") -- the party's first PHYSICAL
    confrontation with Kess, deliberately short and one-sided in her
    favor tonally even though the fight itself is short and one-sided
    in the party's favor mechanically (her real hp_max is set low on
    purpose). First real taunting, capable Kess, not the calm toll-
    collector or the tired woman at the shrine -- she's enjoying this.
    Hand-written, zero Ollama calls, same voice discipline as every
    other Kess beat.
    """
    return (
        "*She's waiting in the open this time, no crossing to hide behind, no crowd to blend into -- "
        "just her, clearly amused that it took this long.*\n\n"
        f"**Kess:** \"You've been very thorough, {character_name}. The warrens, the caverns, all that "
        "counting. I almost feel bad about how little of it was actually about you.\"\n\n"
        "*She draws a real blade this time, unhurried, still talking.*\n\n"
        "**Kess:** \"Let's see what you're actually carrying, then. Consider this a professional courtesy -- "
        "I like to know what I'm dealing with before it matters.\""
    )


def kess_scouting_flees_line() -> str:
    """
    Companion piece to kess_flees_line() above, for her Chapter 7
    scouting form specifically -- per the approved plan, ends on "a
    genuine crack in her composure... visibly costs her control for a
    half-second before she flees," the clearest "insanity is already
    setting in" beat before Chapter 8's transformation makes it
    literal. Deliberately references the Whispers of the Universe (the
    same in-fiction Remnant term her other scenes already use) without
    checking what the party actually carries -- the crack is in HER,
    not a reaction to a specific real fact.
    """
    return (
        "*Kess breaks off before it's even close, faster than the fight itself really demanded.*\n\n"
        "**Kess:** \"That's -- \" *For half a second something genuinely slips, the calm cracking straight "
        "through.* \"-- that's enough for today. The Whispers are loud enough without you adding to it.\"\n\n"
        "*She catches herself, and the crack seals back over almost as fast as it opened -- almost.*\n\n"
        "**Kess:** \"Professional courtesy only extends so far. Next time won't be a warning.\"\n\n"
        "*She's already gone, faster than anyone can follow.*"
    )


def grask_supply_tunnels_reaction() -> str:
    """
    Hand-written (Kess Arc plan, party-composition requirement: a short
    curated list of companions get a real reactive line when present
    for a beat that's actually about them -- Grask Emberscale was
    explicitly named for Chapter 5's own "someone bigger" thread, since
    he was a real captive of these exact Goblin Warrens, per his own
    campaign.json personality/goals). Fires when the party finds the
    veteran goblin's too-fine coin purse -- grounded in his own real
    "never quite managed to break him" backstory, not invented.
    """
    return (
        "*Grask goes quiet at the sight of the purse, turning it over once in his claws.*\n\n"
        "**Grask:** \"I watched them hand off coin like that more than once, back when I still had "
        "a cell to watch it from. Never once thought to ask who they were handing it to.\""
    )


def grask_deep_larders_reaction() -> str:
    """
    Companion piece to grask_supply_tunnels_reaction() above -- fires
    at the deep larder elder's beat, grounded in his own real "getting
    out of these warrens and putting them a long way behind him" goal.
    """
    return (
        "*Grask stares at the oversized stockpile longer than anyone else bothers to.*\n\n"
        "**Grask:** \"That's not goblin thinking. Goblins don't plan past the next meal. "
        "Someone's been feeding this place on purpose -- and I spent longer in here than I'd like, "
        "never once wondering why it never ran dry.\""
    )


def narrate_kess_transformation(
    boss_name: str, before_personality: str, before_goals: str,
    after_name: str, after_personality: str, after_goals: str, party_names: str,
) -> str:
    """
    Hand-written, not AI-generated (Kess Arc Phase 2) -- the real
    Kefka-style "she breaks, something else answers" beat, picked back
    up right after Kess flees the first fight (kess_flees_line above),
    not a corpse reanimating. Fires once, as kess_first_reckoning's own
    real completion flourish, grounded in kess_the_unbound's own
    already-written campaign.json personality/goals (never invented
    here) -- before_personality/before_goals/after_personality/
    after_goals are kept as real parameters for API consistency with
    the rest of this arc's functions, even though this hand-written
    version only ever uses the fixed script below.
    """
    return (
        "*Word comes back from where Kess fled: she never stopped running -- "
        "and something caught up with her.*\n\n"
        "*What returns to the Downs still wears her face. It's taller now, "
        "wrong in the joints, and it isn't bothering to sound like her anymore.*\n\n"
        "**???:** \"She served her purpose. I'll serve mine better.\""
    )


def narrate_chapter_8_epilogue(boss_name: str, party_names: str, plan_succeeded_fact: str) -> str:
    """
    Hand-written, not AI-generated (Kess Arc Phase 2) -- the bespoke,
    `the_unasked`-tier unique ending treatment for kess_the_unbound_
    reckoning (Kess is "the boss of the game" per Coffee), same real
    "hand-write the true ending" discipline bot.py already uses for
    the_unasked/the_unbegun. The FF6 "the Fall" beat: winning the fight
    doesn't mean winning the war. `plan_succeeded_fact` is the one
    real, already-decided fact bot.py computes and folds in verbatim.

    Per Coffee (2026-08-28): Kess stays the ENTIRE focus of this
    chapter -- the true identity of whatever finally answers through
    her body is never named here (deliberately deferred to the actual
    final battle/cutscenes of the eventual Chapter 14). This is her
    real death and something else's real arrival, in the same body, in
    the same breath -- not a separate third boss fight, a single
    unbroken image. Also hints at Evolution/rebirth (a real, already-
    built mechanic, EVOLUTION_HP_MULTIPLIER/rules/leveling.py --
    "players may not have heard of this up to this point," per Coffee,
    and should want to now) as the real, grounded answer to "how do we
    get strong enough for whatever comes next."
    """
    return (
        f"*{boss_name} finally falls -- and this time she doesn't get back up.*\n\n"
        f"*But something else does. It rises through her, wearing what's left of "
        f"her the way water wears a shape it was never built to hold, and for one "
        f"unbearable second the whole battlefield feels far too small to be standing in.*\n\n"
        f"*Then it's gone -- folded away somewhere the party can't follow, taking "
        f"whatever it needed from her and leaving the rest behind.*\n\n"
        f"{plan_succeeded_fact}\n\n"
        f"*Kess's own \"Divine Purpose\" was never really hers to answer -- she was "
        f"only ever the door. Something used her, and the door is closed now, but "
        f"a door having been used once means it can be found again.*\n\n"
        f"*Ordinary strength won't be enough for whatever that was. Something in "
        f"{party_names.split(',')[0].strip()} already senses it -- the way forward "
        f"isn't just levels anymore. It's evolution.*"
    )
