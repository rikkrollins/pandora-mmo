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

ACTION_STYLE_PROMPT = """You are role-playing an autonomous character in a \
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
a place/person/item that isn't actually listed there):
- "I head to [a place listed under Places reachable from here]"
- "I attack [something listed under Danger here]"
- "I talk to [someone listed under People here]"
- "I ask [someone listed under You could recruit] to join our party"
- "Let's start a fight"
- "I examine [something listed under Things worth a closer look]"
- "I check the quest board"
- "I accept the quest" (if the facts below say something's posted or on offer)
- "I choose [the exact label of one of your options]" (if the facts below say you have a decision to make)
- "I want to buy [something listed under Shop here sells]"
- "sell my [something listed under You're carrying]"
- "I want to join the [a guild listed under Guilds you could join]"
- "I cast [something listed under Spells you know]"
- "I rest for now"

Confirmed live 2026-07-11: an earlier version of these examples named \
actual places/objects from this campaign, and this model kept parroting \
that exact example back verbatim regardless of whether it was even true \
right now -- e.g. repeatedly trying to travel to a place it was already \
standing in, spamming the same rejected action every cycle. The facts \
given below for THIS moment are the only valid source for any name in \
your action.

Respond with ONLY the action sentence, nothing else."""


def _build_prompt(character: dict, personality: str, situation_facts: str, last_action: str | None = None) -> str:
    last_action_line = (
        f"\nYour LAST action, already done, was: \"{last_action}\" -- do something "
        f"DIFFERENT this time, don't just repeat it.\n"
        if last_action else ""
    )
    return (
        f"{ACTION_STYLE_PROMPT}\n\n"
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
