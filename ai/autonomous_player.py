"""
ai/autonomous_player.py
Decides the next natural-language action for an autonomously-played AI
party character (2026-07-10, per Coffee: a separate party that plays
the game the same way a human would, for real, ongoing playtesting).

Grounded strictly in that character's real, current game state (never
inventing an NPC/monster/location/quest beyond what's actually given).
Deliberately biased toward the same plain, unambiguous phrasing already
documented (README/SKILL.md) as reliably classifying correctly, e.g.
"I head to X", "I attack X" -- confirmed repeatedly this session that
free-form creative phrasing risks misclassification by the small,
CPU-bound intent model. The whole point of this loop is to actually
exercise the real pipeline a human would hit, not dodge it with wording
picked to avoid it.
"""
import requests

import config
from ai.text_cleanup import strip_think_tags

ACTION_STYLE_PREAMBLE = """You are role-playing an autonomous character in a \
Dungeons & Dragons 5E game, deciding your own next action for yourself. You \
are given real facts about your character and your surroundings -- use \
ONLY these, never invent a person, monster, place, item, or quest beyond \
what's listed below.

Decide ONE thing to do right now and say it in plain first-person natural \
language, EXACTLY the way a player would type it into a chat -- simple, \
direct, unambiguous phrasing, one short sentence, no narration or \
explanation. Good examples of the phrasing STYLE to match (the bracketed \
parts are placeholders -- always replace them with something real from the \
facts given below, NEVER copy a bracketed example verbatim, and never name \
a place/person/item that isn't actually listed there):"""

# (required situation_facts substring, example line) -- the substring is
# the exact "You could X" / "Y here" heading _build_ai_player_situation_facts
# (bot.py) only ever writes when that thing is REALLY true right now.
# None means always show it (no grounding needed, e.g. "Let's start a fight"
# is always a syntactically valid thing to try saying).
#
# Confirmed live 2026-07-14 (Coffee): with recruiting always shown as an
# example regardless of whether a recruitable NPC was actually nearby, an
# AI party member with nobody real to recruit still generated "I ask
# villagers to join our party" -- pattern-matching the example's shape
# and hallucinating a filler ("villagers") for the bracketed placeholder,
# instead of recognizing the example didn't apply and picking something
# it actually had real grounding for. Same root cause already fixed once
# for place/object names specifically (2026-07-11, see the note below) --
# this generalizes that fix to EVERY example, not just names within an
# always-shown line: an example whose entire premise isn't true right now
# is now omitted from the prompt entirely, not just its placeholder name.
_EXAMPLE_LINES = [
    ("Places reachable from here", "I head to [a place listed under Places reachable from here]"),
    ("Danger here", "I attack [something listed under Danger here]"),
    ("People here", "I talk to [someone listed under People here]"),
    ("You could recruit", "I ask [someone listed under You could recruit] to join our party"),
    (None, "Let's start a fight"),
    ("Things worth a closer look", "I examine [something listed under Things worth a closer look]"),
    (None, "I check the quest board"),
    (None, "I accept the quest (if the facts below say something's posted or on offer)"),
    (None, "I choose [the exact label of one of your options] (if the facts below say you have a decision to make)"),
    ("Shop here sells", "I want to buy [something listed under Shop here sells]"),
    ("You're carrying", "sell my [something listed under You're carrying]"),
    ("Guilds you could join", "I want to join the [a guild listed under Guilds you could join]"),
    ("Spells you know", "I cast [something listed under Spells you know]"),
    ("Resources here", "I gather [something listed under Resources here]"),
    ("You have the materials to craft", "I craft [something listed under You have the materials to craft]"),
    ("could make a campfire", "I make a campfire"),
    (None, "I rest for now"),
]

ACTION_STYLE_CLOSING = """
Confirmed live 2026-07-11: an earlier version of these examples named \
actual places/objects from this campaign, and this model kept parroting \
that exact example back verbatim regardless of whether it was even true \
right now -- e.g. repeatedly trying to travel to a place it was already \
standing in, spamming the same rejected action every cycle. The facts \
given below for THIS moment are the only valid source for any name in \
your action.

Respond with ONLY the action sentence, nothing else."""


def _action_style_prompt(situation_facts: str) -> str:
    """Only shows example action shapes actually grounded in something real right now."""
    lines = [
        f'- "{example}"' for required, example in _EXAMPLE_LINES
        if required is None or required in situation_facts
    ]
    return f"{ACTION_STYLE_PREAMBLE}\n{chr(10).join(lines)}\n{ACTION_STYLE_CLOSING}"


def _build_prompt(character: dict, personality: str, situation_facts: str, last_action: str | None = None) -> str:
    last_action_line = (
        f"\nYour LAST action, already done, was: \"{last_action}\" -- do something "
        f"DIFFERENT this time, don't just repeat it.\n"
        if last_action else ""
    )
    return (
        f"{_action_style_prompt(situation_facts)}\n\n"
        f"Your character: {character['name']}, a {character['race']} {character['char_class']}, "
        f"level {character['level']}, HP {character['hp_current']}/{character['hp_max']}\n"
        f"Your personality: {personality}\n"
        f"{last_action_line}\n"
        f"Real facts about right now (use ONLY these):\n{situation_facts}\n\n"
        f"Your action:"
    )


def choose_next_action(character: dict, personality: str, situation_facts: str, last_action: str | None = None) -> str:
    """
    Returns a single natural-language action sentence for this
    autonomous character to "type" into Adventure. Falls back to a
    harmless, always-valid action if Ollama is unreachable.

    2026-07-12: `last_action`, when given, is folded into the prompt as
    an explicit "don't repeat this" instruction -- confirmed live that
    without it, this model can get stuck parroting the exact same
    action (e.g. "examine an ancient, wide-boled tree") every single
    tick for hours, since nothing here previously told it what it had
    already just done.
    """
    prompt = _build_prompt(character, personality, situation_facts, last_action)
    try:
        response = requests.post(
            f"{config.OLLAMA_BASE_URL}/api/generate",
            json={"model": config.BUILD_MODEL, "prompt": prompt, "stream": False},
            timeout=200,
        )
        response.raise_for_status()
        data = response.json()
        text = strip_think_tags(data.get("response", "")).strip()
        if text:
            # First line only, and strip stray wrapping quotes the model likes to add.
            return text.splitlines()[0].strip().strip('"').strip()
    except (requests.RequestException, ValueError) as e:
        print(f"[autonomous_player] action generation failed, defaulting to a safe fallback: {e}")
    return "I look around"
