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
import logging

import requests

import config
from ai.ollama_health import record_timeout
from ai.text_cleanup import strip_think_tags

logger = logging.getLogger("pandora_mmo")

ACTION_STYLE_PREAMBLE = """You are role-playing an autonomous character in a \
5th-edition-style tabletop RPG, deciding your own next action for yourself. You \
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
#
# Live bug found 2026-08-14 (log monitoring): the accept_quest and
# choose-a-branch-decision examples used to be `(None, ...)` (always
# shown) with their real gating condition written as free English text
# INSIDE the example itself, e.g. "I accept the quest (if the facts
# below say something's posted or on offer)". The model isn't told to
# scrub a plain parenthetical the way it's told to scrub a [bracketed]
# placeholder, and copied that hedge verbatim into a live player
# message: "I accept the quest (if the facts say you have it)" --
# breaking the "looks like an ordinary player typed this" illusion
# CLAUDE.md's design philosophy depends on. Fixed by using this same
# (required, example) mechanism every other conditional example
# already uses, instead of hand-writing the condition into the
# sentence: the example is now omitted entirely unless it's actually
# true, so there's no hedge text left to copy.
_EXAMPLE_LINES = [
    ("needs you to travel to", "I head to [the exact place named after 'needs you to travel to', if it's also listed under Places reachable from here]"),
    ("is on offer back at", "I head to [the exact place named after 'is on offer back at', if it's also listed under Places reachable from here]"),
    ("Places reachable from here", "I head to [a place listed under Places reachable from here]"),
    ("Danger here", "I attack [something listed under Danger here]"),
    ("People here", "I talk to [someone listed under People here]"),
    ("You could recruit", "I ask [someone listed under You could recruit] to join our party"),
    (None, "Let's start a fight"),
    ("Things worth a closer look", "I examine [something listed under Things worth a closer look]"),
    (None, "I check the quest board"),
    ("is on offer here", "I accept the quest"),
    ("has something posted", "I accept the quest"),
    ("You have a decision to make", "I choose [the exact label of one of your options]"),
    ("reached the maximum level", "I rebirth"),
    ("trying to solve a riddle", "I say [your own real best guess at the answer to the riddle in the facts below]"),
    ("aren't carrying any healing items", "I want to buy [the exact item named in the healing-supplies fact below]"),
    ("Shop here sells", "I want to buy [something listed under Shop here sells]"),
    ("You're carrying", "sell my [something listed under You're carrying]"),
    # Real feature (2026-08-19, per Coffee: "can the AI players place
    # weapons, armour, rings, amulutes, shield, and other items for
    # sale on the Market place? can they purchase from the market
    # place?"). Both gated on real facts bot.py's _build_ai_player_
    # situation_facts only ever writes when genuinely true right now
    # (a real live listing this character can actually afford; real
    # carried inventory), same "never show an example with nothing
    # real behind it" convention every other line here already follows.
    ("The player marketplace has", "buy [something listed under The player marketplace has] from the market"),
    ("You're carrying", "sell my [something listed under You're carrying] on the market for [a real number, e.g. 30] gold"),
    # Real live request (2026-08-20, Coffee: "make sure the market has
    # what the players need, including AI players") -- the market's new
    # full item-detail view (real stats/buffs/requirements/seller/cost)
    # has a real natural-language path (_do_view_market_listing_intent)
    # same as buy/sell/cancel already do, but with no example hint here
    # an AI companion would rarely think to check an item's real stats
    # before buying it, unlike a human with a 🔍 View button in front
    # of them. Same fact-gating as the buy hint just above.
    ("The player marketplace has", "examine [something listed under The player marketplace has] on the market"),
    ("Guilds you could join", "I want to join the [a guild listed under Guilds you could join]"),
    ("Spells you know", "I cast [something listed under Spells you know]"),
    ("Resources here", "I gather [something listed under Resources here]"),
    ("still needs", "I gather [the material named in the party's quest need] to help finish it"),
    ("You have the materials to craft", "I craft [something listed under You have the materials to craft]"),
    # AI companion parity fix (2026-09-13, per Coffee: "do all of it")
    # -- forge/enchant were structurally invisible to this decision
    # loop even when a companion genuinely qualified (real Forge Guild/
    # level gate, or a real owned magic item + eligible recipe -- see
    # bot._build_ai_player_situation_facts, same fact-gating every
    # other line here already follows).
    ("You could forge a plain item into a real magic item", "I forge my [something listed under You could forge a plain item into a real magic item] into a magic item"),
    ("You have a real magic item you could enchant further", "I enchant my [something listed under You have a real magic item you could enchant further]"),
    # AI companion parity fix (2026-09-13) -- this only ever appears
    # for the separate, fully-autonomous AI roster (is_autonomous=1),
    # never a regular recruited companion (deliberately barred from
    # trading at all, see bot._do_trade_request's own real refusal).
    # Response-only: accept or decline a trade a human already opened,
    # never proactively starting a new one.
    ("You have an active trade proposal with", "accept the trade"),
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

How to prioritize, per Coffee (2026-07-14, revised 2026-07-22 after \
direct log monitoring caught the AI party stalled at level 1 for days, \
looping the same examine/gather actions in one place instead of ever \
progressing): if the facts say "A quest is on offer here," accepting \
it ("I accept the quest") is the single highest priority of all, full \
stop -- confirmed live that without this being said explicitly and \
first, this model can sit right next to an offered quest for DAYS \
without ever once accepting it, favoring safer-looking flavor actions \
instead. If instead the facts say a quest "is on offer back at" \
somewhere else -- meaning you already wandered away before ever \
accepting it -- heading back there is the same top priority; a quest \
you never accepted can never be worked toward. Right after that: if \
the facts say you've reached the maximum level, rebirth immediately \
("I rebirth") -- per Coffee (2026-07-25): make the autonomous party \
actually able to complete every dungeon in order, all the way through \
several rebirths, not stall forever at max level the way it used to. \
Rebirthing costs nothing, is never a mistake, and is the ONLY way \
several real places in this world ever become reachable at all. \
Likewise, if the facts say you're trying to solve a riddle, take a \
real guess at it right then -- it costs nothing to try, and an \
unanswered riddle is a permanent dead end for real progress until \
someone actually attempts it. A real quest of yours \
that "needs you to travel to" somewhere comes right after that -- \
heading there is how the story and \
your own level actually move forward, and it beats every other option \
below, including a party gather-quest need or a shop/skill-practice \
opportunity right where you're already standing. If the facts say \
you aren't carrying any healing items and name one this shop sells \
that you can afford, buying it comes right after that -- per Coffee \
(2026-07-25): "does the AI companion kno to buy items needed for \
battle?! they shud be able to take care of themselves," an empty-\
handed party member walking past exactly the potion they need, right \
when they can afford it, isn't taking care of themselves. This still \
loses to an actual quest to accept or travel to, but beats every \
other shopping/flavor option below. If the facts mention \
the party's quest still needing something you can gather right here, \
that comes second -- it directly helps the party. Otherwise, lean \
toward whatever skill you're already practiced in (shown under "Skills \
you've practiced"), the same way a real adventurer plays to their \
strengths. But if something useful here uses a skill you haven't tried \
yet or aren't practiced in, that's a real opportunity to improve at \
it, not a reason to avoid it -- don't only ever repeat what you're \
already good at. And if NONE of the above ever seem to apply for \
several turns in a row (no quest, no gather need, nothing new to try), \
that's a real sign you've been sitting still too long -- head to \
somewhere listed under "Places reachable from here" you haven't fully \
explored yet, rather than examining or gathering the exact same thing \
again.

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
            json={"model": config.BUILD_MODEL, "prompt": prompt, "stream": False,
                  "options": {"num_thread": config.OLLAMA_NUM_THREAD}},
            timeout=200,
        )
        response.raise_for_status()
        data = response.json()
        text = strip_think_tags(data.get("response", "")).strip()
        if text:
            # First line only, and strip stray wrapping quotes the model likes to add.
            return text.splitlines()[0].strip().strip('"').strip()
    except (requests.RequestException, ValueError) as e:
        record_timeout()
        logger.error(f"[autonomous_player] action generation failed, defaulting to a safe fallback: {e}")
    return "I look around"
