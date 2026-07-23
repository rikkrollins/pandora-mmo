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
import difflib
import json
import re

import requests

import config
from ai.text_cleanup import strip_think_tags
from guilds import GUILDS

# Confirmed live, twice, on unrelated inputs ("my characters", "I'm
# back"): this small model has a real bias toward guessing
# "start_combat" when uncertain — an "attractor" wrong answer, not a
# one-off. A false positive here is uniquely disruptive (an unwanted
# fight), so it's held to a higher bar than other actions: trusted only
# when the raw text actually contains one of these explicit phrases.
COMBAT_START_WORDS = [
    "start combat", "begin fight", "let's fight", "lets fight", "encounter",
    "start a fight", "begin combat", "fight some", "fight the",
]

INTENT_SYSTEM_PROMPT = """You are an intent classifier for a text-based D&D 5E game. \
Given a player's free-text message and some context, output ONLY a JSON object \
(no other text, no markdown fences) with this shape:

{"action": "<one of: attack, pass_turn, start_combat, create_character, check_sheet, \
talk_npc, move, look, check_inventory, check_party, buy, sell, steal, cast_spell, join_guild, \
recruit_npc, rest, go_inactive, skill_check, shove, show_map, gather, craft, list_characters, \
switch_character, delete_character, fast_travel, accept_quest, check_quests, ask_clue, \
answer_puzzle, gamble, chat, second_wind, rage, bardic_inspiration, lay_on_hands, arcane_recovery>", \
"ability": "<one of: strength, dexterity, constitution, intelligence, wisdom, charisma, or null>", \
"target": "<name/place/item mentioned, or null>", "npc_name": "<npc name if talking to one, or null>", \
"item_name": "<item mentioned for buy/sell, or null>", "spell_name": "<spell mentioned for cast_spell, or null>", \
"quantity": "<number mentioned for buy/sell, default 1>", \
"raw_text": "<the original message, verbatim, for narration flavor>"}

Rules:
- "attack" is for any offensive action aimed at an enemy (attack, swing, shoot, cast at, strike).
- "flee" is for trying to run away, escape, or retreat from an active fight (a real risk, not guaranteed).
- "talk_npc" is for addressing a specific named NPC conversationally.
- "start_combat" is when a player wants to begin a fight or encounter.
- "create_character" is when a player wants to make/join with a new character.
- "check_sheet" is for asking about their own stats/HP/level (not items), including \
asking who their active/current character is (e.g. "who is my active character").
- "check_inventory" is for asking what's in their backpack/bag/items they're carrying.
- "check_party" is for asking who's in the party, how many members, or who's adventuring with them.
- "recruit_npc" is for asking a specific named NPC to join their party / travel with them / come along.
- "rest" is for resting, recovering, healing up outside combat, or asking to be revived/healed after being downed. \
Note this no longer heals instantly — it settles the character in to rest, and they recover in proportion to real \
time actually spent resting, same mechanic as "go_inactive" below.
- "go_inactive" is SPECIFICALLY for a player saying they're done playing for now / logging off / taking a rest \
until their next session — e.g. "rest for the night", "take a rest", "rest for now", "resting for a few hours", \
"I'm done for now". "go_inactive" is NEVER allowed during active combat — the player must escape or finish the \
fight first. Set "target" to the duration mentioned, if any.
- "accept_quest" is for a player agreeing to take on a task/favor/quest that's just been described or offered \
("I'll do it", "I accept", "count me in", "I'll help").
- "check_quests" is for asking to see their quest log/journal, what quests they have, or their progress.
- "ask_clue" is for asking for a clue, hint, or lead about their current quest(s) — NOT a general skill_check \
search, specifically asking what they know or what to look for next.
- "answer_puzzle" is for a player stating an answer/solution to a riddle or puzzle ("the answer is X", \
"I think it's X", "could it be X?").
- "gamble" is for wanting to bet, wager, or gamble gold on a game of chance/dice. Set "target" to the amount \
mentioned, if any.
- "skill_check" is for any risky non-combat action with uncertain outcome that isn't covered above: sneaking, \
persuading, climbing, searching, lifting, recalling lore, perceiving hidden things, resisting an effect, etc. \
Set "ability" to whichever of the 6 abilities best fits the action (dexterity for sneaking/climbing, strength \
for lifting/breaking, intelligence for recalling lore, wisdom for perceiving/insight, charisma for \
persuading/deceiving, constitution for enduring/resisting).
- "shove" is specifically for trying to knock an enemy down/prone (shoving, tackling, tripping).
- "show_map" is for asking to see the map or where they've explored.
- "gather" is for foraging, harvesting, mining, or collecting raw materials (herbs, ore, flowers) from the \
environment — NOT picking a lock (that's skill_check).
- "craft" is for brewing, crafting, or making an item from materials. Set "item_name" to the item being crafted.
- "make_campfire" is specifically for making/building/lighting a campfire or making/setting up camp, using wood.
- "list_characters" is for asking to see their own list/roster of characters.
- "switch_character" is for asking to switch to, play as, or make active a specific one of their own \
characters by name. Set "target" to the character name.
- "delete_character" is for asking to delete, remove, or permanently get rid of one of their own characters \
by name. Set "target" to the character name.
- "move" is for traveling, walking, heading to, entering, or descending/ascending to a place — normal, \
on-foot travel to a place directly reachable from here.
- "fast_travel" is for warping, fast-traveling, or teleporting directly to a place already explored \
before, skipping the walk. Set "target" to the destination name.
- "look" is for looking around, examining the current area as a whole, or asking where they are.
- "examine" is for looking at, inspecting, checking out, or searching one SPECIFIC object or detail \
in the area (not the whole area itself — that's "look"). Set "target" to the object's name.
- "buy" is for purchasing something from a shop or merchant.
- "sell" is for selling something they're carrying.
- "steal" is for stealing, pickpocketing, robbing, or taking something without paying — a real risk of \
getting caught, with real consequences, not the same as "buy".
- "cast_spell" is for casting/using a named spell.
- "use_item" is for drinking/using/consuming/quaffing a carried consumable item (e.g. a potion, antitoxin, \
rations) — NOT a spell and NOT a shop purchase. Set "item_name" to the item, and "target" to who it's for if named (defaults to self).
- "equip_item" is for equipping/wielding/wearing/putting on a weapon or piece of armor they're carrying \
(e.g. "equip my longsword", "wear the chain mail", "wield the dagger", "equip Sarah with the longbow"). \
Set "item_name" to the item, and "target" to who it's for if a specific OTHER party member is named (defaults to self).
- "auto_equip" is for asking the game to automatically equip the best weapon/armor/shield being carried, \
without naming a specific item (e.g. "auto equip my character", "put on my gear automatically", \
"help me equip my player"). Set "target" to a specific party member's name if named, else self.
- "join_guild" is for joining/asking to join a specific guild or order.
- "pass_turn" is for skipping, waiting, or passing.
- "resolve_choice" is for declaring a decision on a moral choice/quest resolution (e.g. "I choose to...", "I'll go with...").
- "invite_to_party" is for inviting another player's or AI companion's character into their own formed party. Set "target" to the invitee's name.
- "accept_party_invite" is for accepting a pending party invite.
- "leave_party" is for leaving a party the character is currently in.
- "find_merchant" is for asking where to get supplies or find the nearest shop/merchant.
- "give_item" is for handing/giving/trading a carried item to another real player or AI companion, \
not a shop transaction (e.g. "give my healing potion to Sarah", "hand Borin the torch"). Set "target" to the recipient's name.
- "second_wind" is specifically a Fighter's real class feature: a bonus action to catch their breath and \
recover some HP outside of resting (e.g. "I use second wind", "catch my breath", "second wind").
- "rage" is specifically a Barbarian's real class feature: entering a rage before or during a fight for \
bonus damage and damage resistance (e.g. "I rage", "I fly into a rage", "enter a rage").
- "bardic_inspiration" is specifically a Bard's real class feature: giving an ally an inspiration die to \
help their next roll (e.g. "I give X bardic inspiration", "inspire my ally"). Set "target" to who it's for.
- "lay_on_hands" is specifically a Paladin's real class feature: touching someone to heal them from their \
pool of divine healing (e.g. "I use lay on hands on X", "I lay hands on myself"). Set "target" to who it's for.
- "arcane_recovery" is specifically a Wizard's real class feature: recovering expended spell slots once per \
rest without fully resting (e.g. "I use arcane recovery", "recover a spell slot", "recover my spell slots").
- "breath_weapon" is specifically a Dragonborn's real racial trait: a damaging breath attack usable once per \
rest, replacing a normal attack in combat (e.g. "I use my breath weapon", "breathe fire", "unleash my breath", \
"use dragon breath").
- "channel_divinity" is specifically a Cleric's real class feature (level 2+): Turn Undead, forcing an undead \
creature to become frightened, usable once per rest (e.g. "I channel divinity", "I turn undead", "turn the undead").
- "action_surge" is specifically a Fighter's real class feature (level 2+): take an extra action, once per rest \
(e.g. "I use action surge", "action surge", "I surge").
- "reckless_attack" is specifically a Barbarian's real class feature: attacking recklessly for advantage on your \
attacks this turn, at the cost of attacks against you also having advantage until your next turn \
(e.g. "I attack recklessly", "reckless attack", "I go reckless").
- "divine_smite" is specifically a Paladin's real class feature (level 2+): spending a spell slot on your next \
hit for bonus radiant damage (e.g. "I smite", "divine smite", "I use divine smite").
- "flurry_of_blows" is specifically a Monk's real class feature (level 2+): spending a ki point for a bonus \
unarmed strike (e.g. "flurry of blows", "I use flurry of blows", "I flurry").
- "wild_shape" is specifically a Druid's real class feature (level 2+): shapeshifting into a beast in combat \
for bonus temporary HP and clawed/bitten attack damage, at the cost of being unable to cast spells while shifted \
(e.g. "I wild shape", "I shapeshift", "shift into a beast", "become a beast").
- "toggle_manual_dice" is for turning physical-dice mode on or off (e.g. "use my own dice", "roll my own dice", \
"let the game roll for me", "dice on", "dice off", "turn off manual dice").
- "level_up" is for spending a pending Ability Score Improvement -- saying "level up", naming which ability to \
raise, or asking the game to pick automatically ("auto", "do it for me").
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


# Words whose misspelling is common enough (and consequential enough --
# accept_quest silently failing on "accepet" was reported live) to be
# worth correcting before the exact-substring checks below run. Keyed
# by the correct spelling; only words of similar length get checked
# against it, so this can't accidentally rewrite unrelated words.
_TYPO_TOLERANT_WORDS = ["accept"]


def _normalize_common_typos(lowered: str) -> str:
    """Rewrites near-miss misspellings of a small set of key trigger
    words back to their correct spelling (e.g. 'accepet' -> 'accept'),
    so the exact-substring keyword checks below still fire. Confirmed
    live: this typo silently dropped accept_quest with no reply at all."""
    words = lowered.split()
    for i, word in enumerate(words):
        stripped = word.strip(".,!?;:'\"")
        if stripped in _TYPO_TOLERANT_WORDS:
            continue
        for target in _TYPO_TOLERANT_WORDS:
            if abs(len(stripped) - len(target)) <= 1 and difflib.SequenceMatcher(None, stripped, target).ratio() >= 0.8:
                words[i] = word.replace(stripped, target)
                break
    return " ".join(words)


def _keyword_fallback(text: str, known_npc_names: list[str]) -> dict:
    """
    Plain keyword-based classification used when the model is unreachable
    or returns something we can't parse. Deliberately simple and
    conservative — when in doubt, classify as 'chat' rather than guess
    at a game action.
    """
    lowered = text.lower()
    lowered = _normalize_common_typos(lowered)
    base = {"action": "chat", "target": None, "npc_name": None, "ability": None,
            "item_name": None, "spell_name": None, "quantity": 1, "raw_text": text}

    # Task #222, per Coffee: telling an AI party member something (e.g.
    # "tell Sarah to run") must never be misread as the SPEAKER's own
    # combat action -- checked first, before "flee"/"attack"/etc. below,
    # so a message clearly directed at someone else by name never falls
    # into one of those triggers just because it happens to share a word
    # like "run". _do_message_ai independently verifies a real AI party
    # member is actually named; if not, it says so rather than guessing.
    if lowered.startswith("tell ") or " tell " in lowered:
        return {**base, "action": "message_ai"}

    # Checked BEFORE check_quests below: "Accept the quest on the quest
    # board" and "Accept the quest 'a quiet request for Silverleaf Herb'"
    # both contain "quest board"/"quest" and would otherwise be swallowed
    # by check_quests's broader triggers just below -- confirmed live
    # 2026-07-12/13 via two real screenshots, the second falling through
    # everything all the way to pass_turn since the quoted quest name
    # didn't match anything else either ("There's no active turn to pass
    # right now"). The game's own board-quest prompt literally tells
    # players to say "I accept this quest" (already covered by "i accept"
    # below), but the equally natural imperative phrasing "accept the/
    # this quest" (no "I") was never a trigger at all. raw_text is passed
    # through unchanged so _do_accept_quest's existing find_board_quest_by_name
    # can still resolve a quoted/named quest exactly as before.
    if any(w in lowered for w in ["accept the quest", "accept this quest", "accept that quest"]):
        return {**base, "action": "accept_quest"}

    # Checked BEFORE the known-NPC-name loop below: naming an NPC while
    # asking to read the quest board (e.g. "read the quest for Grimsby
    # from the quest board") must not get swallowed as talk_npc just
    # because the NPC's name appears in the sentence -- confirmed live
    # 2026-07-11 via a real screenshot: this got misrouted to a talk_npc
    # reply about shop stock instead of showing the actual board listing,
    # because the NPC-name check below ran first and returned before this
    # block was ever reached. "quest board" is explicit enough to win.
    # Task #185 (real live incident): "any quests available ?" fell
    # through to plain chat -- every phrase below requires a qualifier
    # word ("my"/"board"/"current"/"active"/"check"/"show"/"list") right
    # next to "quest", but "any quests available" has none of them, it's
    # a bare "is there a quest" phrasing. Same recurring gap this file
    # has hit many times before for this exact action (#92, #99, #109,
    # #151, #154, #161) -- a tolerant regex for "any ... quest(s)" and
    # "quest(s) ... available" covers the natural variations without an
    # ever-growing literal list, same fix shape as #161's "who's ... with
    # me" just above.
    if (
        any(w in lowered for w in ["my quests", "quest journal", "quest log", "my quest log",
                                    "quest board", "the board", "what's on the board",
                                    "quest details", "current quest", "my quest", "what quest",
                                    "what's my quest", "whats my quest", "active quest",
                                    "check quest", "check my quest", "show quest", "list quest",
                                    "any quests", "any quest", "is there a quest", "are there any quests"])
        or re.search(r"\bany\b(?:\s+\w+){0,3}\s+quests?\b", lowered)
        or re.search(r"\bquests?\b(?:\s+\w+){0,3}\s+available\b", lowered)
        # Real live incident (2026-07-19, Sugar): "Check for quests in
        # cellar" fell through every branch above (the literal "check
        # quest"/"check my quest" phrases require no word in between,
        # but this has "for" between "check" and "quests") all the way
        # to the model, which misread it as check_sheet -- same
        # recurring "check ... quest(s)" gap this file has hit before,
        # just with a different filler word. Same tolerant-regex shape
        # as the "any ... quest" fix just above.
        or re.search(r"\bcheck\b(?:\s+\w+){0,3}\s+quests?\b", lowered)
    ):
        return {**base, "action": "check_quests"}

    # Checked BEFORE "buy" below: "where can I buy potions" is asking
    # about location/availability, not attempting an actual purchase --
    # confirmed live 2026-07-12 this got shadowed as "buy" once that
    # check was moved earlier (to fix "buy X from <NPC>" below), since
    # "where can i buy" also contains the bare word "buy".
    if any(w in lowered for w in ["where can i get supplies", "where can i find supplies", "nearest merchant",
                                    "closest merchant", "nearest shop", "closest shop", "where can i buy",
                                    "where can i shop", "need supplies", "where's the nearest",
                                    "wheres the nearest"]):
        return {**base, "action": "find_merchant"}

    # Checked BEFORE the known-NPC-name loop below, same reasoning as the
    # quest-board fix above: "buy two potions from Grimsby" names an NPC
    # and would otherwise be caught by that loop first and misread as
    # talk_npc -- confirmed live 2026-07-12 via a real screenshot, a
    # buy request that got silently swallowed as ordinary conversation
    # with no purchase ever happening. "buy"/"purchase"/"sell" are
    # explicit enough to win over a bare NPC-name mention.
    # Checked BEFORE "buy" below, same reasoning as "where can i buy"
    # above: real live bug (2026-07-19, Sugar) -- "Look at wares
    # available for purchase" contains the bare word "purchase" and got
    # caught by the "buy"/"purchase" check below with no item named,
    # giving the unhelpful "not sure what item you mean" the list_shop
    # action (further below) exists specifically to avoid. These are
    # clearly asking to browse, not naming anything to buy.
    if any(w in lowered for w in ["available for purchase", "available to buy", "for purchase"]):
        return {**base, "action": "list_shop"}

    if any(w in lowered for w in ["buy", "purchase"]):
        return {**base, "action": "buy"}

    if lowered.startswith("sell") or " sell " in lowered:
        return {**base, "action": "sell"}

    # "recruit X" is checked BEFORE the known-NPC-name loop below and
    # regardless of whether the name matches one exactly -- confirmed
    # live 2026-07-12, twice: "Recruit Sarah to my party" (correct
    # spelling, a known NPC) still came back as talk_npc, because the
    # only recruit_words phrases below all require the word "join",
    # never the word "recruit" itself, even though that's the action's
    # own name and the single most obvious way a player would phrase
    # it. "Recruit Seta to my party" (a likely typo/mishearing of
    # "Sarah") is even worse off: it doesn't match any known NPC name at
    # all, so it fell through everything else to "my party" and got
    # misread as check_party. Extracting the name after "recruit "
    # directly handles both a correct and a misspelled name the exact
    # same way -- _do_recruit_npc already has its own real,
    # DB-backed "no one by that name" handling either way.
    for trigger in ["recruit "]:
        if trigger in lowered:
            name = text[lowered.index(trigger) + len(trigger):].strip()
            for cut in (" to my party", " to the party", " to our party", " to join", " to my group"):
                if cut in name.lower():
                    name = name[:name.lower().index(cut)].strip()
                    break
            return {**base, "action": "recruit_npc", "npc_name": name or None}

    # "invite X to my party" is genuinely ambiguous between two
    # different actions: recruiting a campaign NPC (recruit_npc) or
    # inviting a fellow player's/AI companion's own character
    # (invite_to_party, checked later below) -- disambiguated here by
    # whether the named person is a real campaign NPC. Confirmed live
    # 2026-07-12: "Invite Sarah to my party" (Sarah IS a real recruitable
    # NPC) still came back as talk_npc, the same root cause as the
    # "recruit " gap just above -- this only ever matched invite_to_
    # party's OWN trigger below, which never got reached because the
    # known-NPC-name loop further down already returned talk_npc first.
    for trigger in ["invite ", "let "]:
        if trigger in lowered and ("to my party" in lowered or "to the party" in lowered
                                    or "join my party" in lowered or "join the party" in lowered):
            name = text[lowered.index(trigger) + len(trigger):].strip()
            for cut in (" to my party", " to the party", " join my party", " join the party"):
                if cut in name.lower():
                    name = name[:name.lower().index(cut)].strip()
                    break
            for npc_name in known_npc_names:
                if npc_name.lower() == name.lower():
                    return {**base, "action": "recruit_npc", "npc_name": npc_name}
            # Not a known campaign NPC -- fall through to invite_to_party's
            # own check further below, unchanged.

    # Checked BEFORE the known-NPC-name loop below, same reasoning as
    # every fix above it: "Show me SARAH's character sheet" (and the
    # apostrophe-less retype "Show me sarah character sheet") both
    # contain "Sarah" -- a real known NPC -- so the loop below caught
    # them FIRST and returned talk_npc before this ever ran, even
    # though it was already sitting further down in this same function.
    # Confirmed live 2026-07-14: moving the check earlier (not just
    # broadening its regex, which alone didn't fix it) was the actual
    # fix -- every single "show me X's sheet" attempt that day, with or
    # without the apostrophe, kept coming back as talk_npc/Sarah
    # replying instead, because it never got the chance to run. The
    # excluded-words guard keeps "my"/"the"/pronoun-only phrasing
    # ("check my character sheet") from being misread as a party
    # member literally named "my".
    named_sheet_match = re.search(r"(\w+)(?:'s)? (?:character )?sheet", lowered)
    if named_sheet_match and named_sheet_match.group(1) not in (
        "my", "the", "a", "an", "her", "his", "their", "your", "our"
    ):
        return {**base, "action": "check_sheet", "target": named_sheet_match.group(1)}

    if any(w in lowered for w in ["ask for a clue", "ask for clues", "give me a clue", "any clues",
                                    "what's the clue", "need a hint", "give me a hint",
                                    "ask for a hint", "what clues"]):
        return {**base, "action": "ask_clue"}

    # give_item (player-to-player trading, 2026-07-15): checked after
    # ask_clue above so "give me a clue/hint" is never shadowed -- this
    # only fires on "give"/"hand"/"trade"/"send" phrasing that also names a
    # recipient via "to" or "them"/a name, distinct from ask_clue's
    # "give me a ..." pattern (which never involves handing off to
    # someone else).
    #
    # Real live bug (2026-07-19, found via a real test written for the
    # "send" fix just below): giving an item to a party member whose
    # name happens to be a recruited campaign NPC's -- "Give the potion
    # to Sarah", "Hand Sarah the torch" -- came back as talk_npc instead,
    # same root cause as every other fix in this "checked BEFORE the
    # known-NPC-name loop" run above (buy/sell/recruit/invite/
    # check_sheet): this check used to sit AFTER that loop, so the loop
    # always caught Sarah's name first and returned talk_npc before
    # give_item ever got a chance to run. Moved up here, same fix
    # pattern as all the others.
    #
    # Real live bug (2026-07-18, confirmed live via topic-activity
    # monitoring: "Send woodcutters axe to @ShesAQueen_78" fell through
    # to silent chat, no item transfer): "send " was never in this
    # trigger-verb list, even though it's exactly the same "verb + item
    # + to + recipient" construction as give/hand/trade.
    if (any(w in lowered for w in ["give ", "hand ", "trade ", "send "])
            and " to " in lowered and "give me a" not in lowered):
        return {**base, "action": "give_item"}

    # Real live bug (2026-07-18, confirmed live: Coffee's "Give Laurienna
    # a woodcutting axe" fell through to silent chat): the "to" check
    # above misses the equally natural English dative construction "give
    # [recipient] a/an/the/some [item]" -- no "to" at all. Detected here
    # via a capitalized word right after the verb (a real recipient's
    # name is always capitalized), NOT via generic item-name matching --
    # confirmed live that a loose item-name check falsely fires on
    # idioms like "give it a hand" (matches "Bracers of the Steady
    # Hand") or "give it a try"/"give her a hand" in general, since
    # those don't require any real item at all. A lowercase pronoun
    # ("it"/"her"/"him"/"them") right after the verb is exactly what
    # this excludes.
    # Real live bug (2026-07-18, confirmed live: Coffee's "Give
    # @ShesAQueen_78 a Woodcutters Axe" fell through to silent chat):
    # the capitalized-word check above never matches a real Telegram
    # @username tag, since it starts with "@", not an uppercase letter
    # -- exactly the same dative construction as the fix right above it,
    # just with an @-tag recipient instead of a capitalized name. An
    # @-tag is unambiguous regardless of case (unlike a bare capitalized
    # word, which still needs the idiom-exclusion reasoning above), so
    # this is checked as its own alternative rather than loosening the
    # existing pattern.
    if re.search(r"\b(?:[Gg]ive|[Hh]and|[Tt]rade|[Ss]end)\s+(?:[A-Z]\w+|@\w+)\s+(?:a|an|the|some)\b", text):
        return {**base, "action": "give_item"}

    # Confirmed live 2026-07-14 (Coffee, reported as a broad "roadblock"
    # affecting both himself and the AI party): the per-word matching
    # below only filtered by word length (>=3), so filler words inside
    # a multi-word NPC name -- "the" in "Theron THE Wanderer"/"Kess THE
    # Bandit", "old" in "OLD Maren" -- were themselves treated as
    # distinctive identifying words. Since "the" appears in nearly
    # every English sentence, ANY message reaching this loop (not just
    # ones actually about Theron/Kess) matched and returned talk_npc
    # here, before it could ever reach later checks -- including
    # "Read the guest book", "Check out the door", every other
    # not-yet-covered examine phrasing below, and surely plenty more.
    # This is a strict denylist, not a rewrite of the length filter,
    # since genuine short distinctive name-parts (Sarah, Kess, Vane...)
    # still need to keep matching.
    _NPC_NAME_FILLER_WORDS = {"the", "a", "an", "of", "and", "old"}
    recruit_words = ["join us", "join our party", "join my party", "come with us",
                      "travel with us", "come along", "join the party"]
    # Live-caught (2026-07-19, Sugar): "Look at old maren's brass scale"
    # hijacked to talk_npc purely because "maren" appears in the text --
    # even though the message is clearly examining an OBJECT of hers
    # (the scale is a real, separately-described interactable), not
    # addressing her directly. The 2026-07-14 filler-word fix above
    # solved the "the"/"old" false-positive case but not this one, since
    # "maren" is a genuine, real name-word -- the real distinguishing
    # signal here is an explicit examine-style verb, which means route
    # to examine (checked further below) instead, letting the object's
    # own real description win over an ungrounded NPC-chat guess.
    _NPC_HIJACK_EXAMINE_GUARD = re.compile(
        r"\b(?:read|observe[d]?|examine[d]?|inspect(?:ed)?|search(?:ed)?|checked? out|"
        r"touch(?:ed)?|look(?:ed)?\s+(?:at|closer at)|(?:peer|perr)(?:ed)?\s+(?:at|into|in)|"
        r"glanced?\s+at)\b"
    )
    has_examine_verb = bool(_NPC_HIJACK_EXAMINE_GUARD.search(lowered))
    for npc_name in known_npc_names:
        # Matches the NPC's full registered name as a substring ("old
        # maren" in "go talk to old maren") OR any single word of it, at
        # least 3 letters, as a whole word ("maren" in "say hello to
        # maren") -- confirmed live 2026-07-12: "Say hello to Maren"
        # never matched "Old Maren" at all (the full name isn't a
        # substring of the message), silently fell through everything to
        # pass_turn ("There's no active turn to pass right now"), even
        # though a player naturally drops title words like "Old" once
        # introduced. Whole-word (not substring-within-a-word) matching
        # on individual name words avoids a short/common fragment
        # accidentally firing on unrelated text.
        name_words = [
            w for w in npc_name.lower().split()
            if len(w) >= 3 and w not in _NPC_NAME_FILLER_WORDS
        ]
        if (npc_name.lower() in lowered or any(
            re.search(r"\b" + re.escape(w) + r"\b", lowered) for w in name_words
        )) and not has_examine_verb:
            if any(w in lowered for w in recruit_words):
                return {**base, "action": "recruit_npc", "npc_name": npc_name}
            return {**base, "action": "talk_npc", "npc_name": npc_name}

    flee_words = ["flee", "run away", "try to run", "try to escape", "escape the fight",
                  "retreat", "get out of here", "get me out", "make a break for it"]
    if any(w in lowered for w in flee_words):
        return {**base, "action": "flee"}

    # Real party invite/accept/leave — checked here, before any known NPC
    # name would have already been caught above (recruit_npc's "join the
    # party" phrasing is for campaign NPCs specifically; this is for
    # inviting a fellow player's or AI companion's own character).
    for trigger in ["invite ", "let "]:
        if trigger in lowered and ("to my party" in lowered or "to the party" in lowered
                                    or "join my party" in lowered or "join the party" in lowered):
            name = text[lowered.index(trigger) + len(trigger):].strip()
            for cut in (" to my party", " to the party", " join my party", " join the party"):
                if cut in name.lower():
                    name = name[:name.lower().index(cut)].strip()
                    break
            return {**base, "action": "invite_to_party", "target": name or None}

    if any(w in lowered for w in ["accept the party invite", "accept the invite", "i'll join the party",
                                    "ill join the party", "i accept the party"]):
        return {**base, "action": "accept_party_invite"}

    if any(w in lowered for w in ["leave the party", "leave my party", "quit the party", "i quit my party"]):
        return {**base, "action": "leave_party"}

    # Checked BEFORE the attack-word match below: a hypothetical/defensive
    # statement like "Stand on guard in case the wolves attack" contains
    # the bare word "attack" as a plain substring, which the naive check
    # below would otherwise match unconditionally -- confirmed live
    # (task #154) as a real misclassification into an actual attack
    # action from a sentence that never asked to attack anything. These
    # conditional markers ("in case", "if", "should they", "in the event")
    # only ever appear when the player is describing a possible FUTURE
    # trigger, never an attack they're making right now, so skipping the
    # attack match entirely here is safe -- it just falls through to
    # "chat" (ordinary roleplay, no bot response), same as any other
    # non-actionable flavor text, rather than guessing wrong.
    conditional_words = ["in case", " if ", "if the", "if they", "should they", "in the event"]
    attack_words = ["attack", "swing", "shoot", "strike", "hit", "stab", "cast at", "fire at"]
    if any(w in lowered for w in attack_words) and not any(c in lowered for c in conditional_words):
        return {**base, "action": "attack"}

    if any(w in lowered for w in COMBAT_START_WORDS):
        return {**base, "action": "start_combat"}

    # Real live bug (2026-07-19, Sugar): "Create character" (no article)
    # and "Create a second character" (an extra word breaking the
    # "create a character" substring) both missed every phrase below and
    # fell through to the Ollama classifier, which misclassified them as
    # "chat" -- silently no character got created. Added the bare/no-
    # article form and explicit "second"/"another"/"additional character"
    # phrasing (a player's own second character, not a request about an
    # NPC), same narrow "fix the observed collision" pattern as every
    # other phrasing gap here.
    if any(w in lowered for w in [
        "create a character", "make a character", "new character", "join the game",
        "create character", "make character", "second character", "another character",
        "additional character",
    ]):
        return {**base, "action": "create_character"}

    # Checked BEFORE check_inventory below: "what items do you have for
    # sale" is asking about a SHOP's or NPC's stock, not the player's own
    # backpack -- real live bug (2026-07-16), this got shadowed by
    # check_inventory's own "what items"/"what do i have" triggers (meant
    # for the player's OWN items) since "have" phrasing overlaps, and
    # check_inventory's handler shows the ASKER's inventory, answering a
    # question about a shop's wares with completely unrelated data.
    #
    # Originally routed to "buy" (2026-07-16), but that only resolves a
    # SPECIFIC named item -- with none named, it just says "not sure
    # what item you mean," no better than silence. Real live bugs the
    # same night confirmed there was no actual "browse a shop" action to
    # reach for at all: "I want to shop" fell all the way to silent
    # chat, and "I want to see the items in the shop" got guessed by the
    # raw model as check_sheet with a hallucinated, garbled target name.
    # Now a real list_shop action -- see bot.py's _do_list_shop -- shows
    # the shop's ACTUAL stocked items (real names/prices), grounded, not
    # invented, same as every other real-data lookup in this game.
    if any(w in lowered for w in ["for sale", "to sell", "what do you have", "what does he have",
                                    "what does she have", "you got for sale", "you have for sale",
                                    "what's for sale", "whats for sale", "i want to shop",
                                    "id like to shop", "i'd like to shop", "let me shop",
                                    "let's shop", "lets shop", "time to shop", "go shopping",
                                    "items in the shop", "items in his shop", "items in her shop",
                                    "shop items", "browse the shop", "browse shop", "browse shops",
                                    "browse the shops", "see the shop",
                                    "what's in the shop", "whats in the shop", "look at the shop",
                                    "what's in his shop", "what's in her shop",
                                    "open the shop", "open shop", "open his shop", "open her shop",
                                    # Real live bug (2026-07-19, Sugar): "wares" was
                                    # simply never in this list (the "for purchase"/
                                    # "available to buy" phrasings from the same bug
                                    # are caught earlier, before the "buy" check, since
                                    # they contain the bare word "buy"/"purchase").
                                    "wares",
                                    # Real live bug (2026-07-22, Coffee: "why is it not
                                    # opening the shop for this user?"): Sugar's plain
                                    # "Shop" got zero reply (fell to silent chat), and
                                    # "Look at shop" -- missing "the" this list already
                                    # required -- fell to a plain "examine" instead,
                                    # which only ever shows the NPC, not the wares.
                                    "look at shop"]):
        return {**base, "action": "list_shop"}
    if lowered.strip(" .!?").strip() in ("shop", "shops", "the shop"):
        return {**base, "action": "list_shop"}

    # Real live bug (2026-07-15): "auto equip my equipment" contains "my
    # equipment", which otherwise matches here and never reaches
    # auto_equip's own check further down -- same shadowing shape as
    # "my characters name" above. "auto"/"automatically" alongside
    # "equip" is an unambiguous signal this is the auto_equip action,
    # not a request to see what's carried.
    if (any(w in lowered for w in ["my backpack", "my bag", "my inventory", "what am i carrying", "what do i have",
                                     "check inventory", "show inventory", "view inventory", "what items", "my items",
                                     "what weapons", "check my equipment", "my equipment"])
            and not ("auto" in lowered and "equip" in lowered)):
        return {**base, "action": "check_inventory"}

    # Confirmed live 2026-07-14 (Coffee): "Who is in my current party?"
    # fell through to plain chat entirely -- "my party" was a required
    # exact substring, but "current" inserted between "my" and "party"
    # broke it, and "who is in my party" (the fully spelled-out, non-
    # contraction phrasing) was never covered at all, only "who's"/
    # "whos". A regex tolerant of a word or two between "my" and
    # "party" (e.g. "my current party", "my active party") is more
    # robust than an ever-growing literal-phrase list.
    # Task #161 (real live incident): "Who's here at the market with me?"
    # fell through to plain chat -- the exact same "insertion between two
    # required words breaks a contiguous-phrase match" bug already fixed
    # above for "my ... party", just on the "who's ... with me" side of
    # this same check instead. Tolerant regex (up to ~6 words between)
    # instead of an ever-growing literal list, same fix shape as above.
    # Bare "who's here"/"anyone here" (no "with me" at all) is an equally
    # natural way to ask the same question, so it's added as its own
    # trigger rather than assuming "with me" is always spelled out.
    if (
        any(w in lowered for w in ["who's in my party", "whos in my party", "who is in my party",
                                    "who is with me", "who's with me", "am i in a party",
                                    "party status", "party members", "who's here", "who is here",
                                    "whos here", "anyone here", "anybody here"])
        or re.search(r"\bmy\b(?:\s+\w+){0,2}\s+party\b", lowered)
        or re.search(r"\bwho('s|s| is)\b(?:\s+\w+){0,6}\s+with me\b", lowered)
    ):
        return {**base, "action": "check_party"}

    # Confirmed live 2026-07-13 (Coffee): "I have decided to 'Keep it and
    # collect the reward'" -- the exact answer the game itself tells the
    # player to say when a branching quest is ready to resolve -- matched
    # none of the phrasings below ("i decide to" is present tense, his
    # message was "i have decided to"), fell all the way through to the
    # "gather" check further down (its "collect" keyword matched "collect
    # the reward"), and got silently misclassified as a gather action.
    # Broadened to also cover past-tense "decided" framing, and the two
    # actual choice labels defined in board_quests.py verbatim, so a
    # player who just echoes the game's own quoted suggested answer back
    # (with or without an "I choose"/"I've decided" wrapper) is always
    # caught here, before "collect" ever gets a chance to misfire as gather.
    # Checked BEFORE resolve_choice below: "I have decided the answer to
    # the riddle is a map" -- a genuine puzzle answer -- contains "i have
    # decided", which resolve_choice's own broadened trigger (added
    # 2026-07-13 for branching board-quest choices) would otherwise catch
    # first, since it's checked earlier in this function. "riddle"/
    # "puzzle"/"the answer to" are distinctive enough to unambiguously
    # mean a puzzle answer, not a quest-branch decision, regardless of
    # what decision-framing words surround them.
    if any(w in lowered for w in ["riddle", "puzzle", "the answer to"]):
        return {**base, "action": "answer_puzzle"}

    if any(w in lowered for w in ["i choose", "i decide to", "i decided to", "i've decided", "ive decided",
                                    "i have decided", "i'll go with", "ill go with",
                                    "my choice is", "i'll take the", "ill take the",
                                    "keep it and collect the reward", "leave it be instead"]):
        return {**base, "action": "resolve_choice"}

    if any(w in lowered for w in ["i accept", "i'll do it", "ill do it", "count me in", "i'll help",
                                    "ill help", "i'll take the job", "i'll take it on"]):
        return {**base, "action": "accept_quest"}

    if any(w in lowered for w in ["the answer is", "my answer is", "i think it's", "i think the answer is",
                                    "could it be"]):
        return {**base, "action": "answer_puzzle"}

    if any(w in lowered for w in ["gamble", "wager", "place a bet", "i bet", "let's bet", "lets bet",
                                    "play dice for gold", "bet "]):
        return {**base, "action": "gamble"}

    if any(w in lowered for w in ["fortune's wheel", "fortunes wheel", "spin the wheel", "spin fortune"]):
        return {**base, "action": "fortunes_wheel"}

    if any(w in lowered for w in ["play dice", "dice game", "roll my dice game", "roll for fun"]):
        return {**base, "action": "dice_game"}

    if "alignment" in lowered:
        return {**base, "action": "set_alignment"}

    if any(w in lowered for w in ["skill tree", "skill points", "skilltree"]):
        return {**base, "action": "skill_tree"}

    # Real gap (2026-07-21, per Coffee: "a player tried 'replay intro'
    # and it didnt work"): /replay_intro only ever existed as a slash
    # command -- plain text like "replay intro" or "replay the
    # cutscene" had no keyword trigger at all and silently fell through
    # to chat.
    if "replay" in lowered and any(w in lowered for w in ["intro", "cutscene", "chapter", "opening"]):
        return {**base, "action": "replay_intro"}

    if any(w in lowered for w in ["visual map", "picture of the map", "draw the map", "map image", "image of the map"]):
        return {**base, "action": "visual_map"}

    if "duel" in lowered:
        if "accept" in lowered:
            return {**base, "action": "accept_duel"}
        return {**base, "action": "challenge_duel"}

    if any(w in lowered for w in ["the market", "marketplace", "market listings"]):
        return {**base, "action": "check_market"}

    if any(w in lowered for w in ["join the battle", "join the fight", "help them fight",
                                    "jump into the fight", "join in the fight"]):
        return {**base, "action": "join_battle"}

    if any(w in lowered for w in ["second wind", "catch my breath", "catch our breath"]):
        return {**base, "action": "second_wind"}

    if any(w in lowered for w in ["i rage", "fly into a rage", "enter a rage", "go into a rage",
                                    "i enter rage", "rage now"]):
        return {**base, "action": "rage"}

    if any(w in lowered for w in ["bardic inspiration", "inspire "]):
        return {**base, "action": "bardic_inspiration"}

    if any(w in lowered for w in ["lay on hands", "lay hands"]):
        return {**base, "action": "lay_on_hands"}

    if any(w in lowered for w in ["arcane recovery", "recover a spell slot", "recover my spell slot",
                                    "recover spell slots"]):
        return {**base, "action": "arcane_recovery"}

    # Real live bug (2026-07-22, Charvenna/Sugar): "Use dragon breath on
    # the bark of the ancient, wide-boled tree" fell through every
    # trigger below (none of them cover "dragon breath", the single
    # most natural way to describe a Dragonborn's own racial ability),
    # landed on the model as an unrecognized "chat", and got hallucinated
    # as "show_map" -- a real action, just completely wrong -- twice in
    # a row. "dragon breath"/"dragon's breath" added directly.
    if any(w in lowered for w in ["breath weapon", "breathe fire", "unleash my breath", "use my breath",
                                    "dragon breath", "dragon's breath", "dragons breath"]):
        return {**base, "action": "breath_weapon"}

    if any(w in lowered for w in ["channel divinity", "turn undead", "turn the undead"]):
        return {**base, "action": "channel_divinity"}

    if any(w in lowered for w in ["action surge", "i surge"]):
        return {**base, "action": "action_surge"}

    if any(w in lowered for w in ["attack recklessly", "reckless attack", "i go reckless", "attack rashly"]):
        return {**base, "action": "reckless_attack"}

    if any(w in lowered for w in ["divine smite", "i smite", "use divine smite"]):
        return {**base, "action": "divine_smite"}

    if any(w in lowered for w in ["flurry of blows", "i flurry"]):
        return {**base, "action": "flurry_of_blows"}

    if any(w in lowered for w in ["wild shape", "i shapeshift", "shift into a beast", "turn into a beast",
                                    "become a beast"]):
        return {**base, "action": "wild_shape"}

    if any(w in lowered for w in ["use my own dice", "roll my own dice", "own physical dice",
                                    "let the game roll for me", "dice on", "dice off",
                                    "turn on manual dice", "turn off manual dice",
                                    "turn on physical dice", "turn off physical dice"]):
        return {**base, "action": "toggle_manual_dice"}

    if "level up" in lowered:
        return {**base, "action": "level_up"}

    # Rebirth/hybrid classes (2026-07-22, per Coffee): checked as
    # unambiguous standalone triggers, same shape as every other
    # explicit-keyword action here -- "rebirth" and "hybrid" aren't
    # words this game uses anywhere else, so a bare substring match is
    # safe with no risk of catching an unrelated sentence.
    if "rebirth" in lowered:
        return {**base, "action": "rebirth"}
    if "hybrid" in lowered:
        return {**base, "action": "choose_hybrid"}

    # Checked BEFORE check_sheet below: "my characters" (plural, roster) is
    # a substring-superset of check_sheet's "my character" (singular) —
    # confirmed live to otherwise get shadowed and misread as check_sheet,
    # so the more specific roster/switch/delete phrasings must win first.
    # Real bug (2026-07-15, live): "Write my characters name in the guest
    # book" -- Coffee dropped the apostrophe on the possessive "my
    # character's name", and the literal text "my characters" matched
    # this roster trigger anyway, showing his character list instead of
    # interacting with the guest book. "my characters name" (no
    # apostrophe, immediately followed by "name") is excluded as an
    # unambiguous signal of the mistyped-possessive case, not a real
    # roster request.
    if (any(w in lowered for w in ["my characters", "show my characters", "list my characters",
                                     "character roster", "my roster"])
            and "my characters name" not in lowered):
        return {**base, "action": "list_characters"}

    # Real bug, live (2026-07-19, Coffee): "Switch to my character
    # Elduinn" matched the plain "switch to " trigger below, leaving
    # "my character elduinn" as the extracted name -- longer than the
    # real name, so _find_own_character_by_name_fragment's match (which
    # only checks the fragment against/within the real name, not the
    # reverse) never found it, even with the fragment containing the
    # right name at the end. Longer, more specific triggers are checked
    # first so "my character "/"character " is stripped along with the
    # generic "switch to " prefix, same fix shape "delete my character "
    # already gets below.
    for trigger in ["switch to my character ", "switch character to ", "switch my character to ",
                     "switch to ", "play as "]:
        if trigger in lowered:
            name = text[lowered.index(trigger) + len(trigger):].strip()
            return {**base, "action": "switch_character", "target": name or None}

    # "delete " alone (no literal word "character" required) is trusted —
    # nothing else in this game is described as "deleting" something, so
    # "delete Nyssa" is just as unambiguous as "delete my character Nyssa".
    for trigger in ["delete my character ", "delete character ", "remove my character ", "delete "]:
        if trigger in lowered:
            name = text[lowered.index(trigger) + len(trigger):].strip()
            return {**base, "action": "delete_character", "target": name or None}

    # Checked BEFORE check_sheet below: "add a description to my
    # character" contains "my character" as a substring and would
    # otherwise be shadowed by check_sheet's broad trigger for that phrase.
    if any(w in lowered for w in ["character description", "add a description", "add description",
                                    "set my description", "describe my character", "character bio",
                                    "add a bio", "set a description", "update my description",
                                    "change my description"]):
        return {**base, "action": "set_description"}

    # Same reasoning/position as set_description above (task #117,
    # 2026-07-17): checked before check_sheet's broad "my character"
    # trigger, since "set my pronouns" also contains that phrase.
    if any(w in lowered for w in ["my pronouns", "set pronouns", "character pronouns",
                                    "update my pronouns", "change my pronouns"]):
        return {**base, "action": "set_pronouns"}

    # Checked BEFORE check_sheet below: "auto equip my character"/"equip
    # my player" would otherwise match check_sheet's broad "my
    # character" trigger first and never reach a more specific check.
    if any(w in lowered for w in ["auto equip", "auto-equip", "autoequip", "equip automatically",
                                    "equip me automatically", "help me equip", "gear up automatically",
                                    "put on my gear automatically", "equip my gear automatically"]):
        return {**base, "action": "auto_equip"}

    if any(w in lowered for w in [
        "my sheet", "my stats", "my hp", "my health", "my character", "status",
        "active character", "current character", "who am i playing", "which character am i",
        "who am i currently playing", "my class", "what class", "my race", "what race",
        "my gold", "how much gold", "how much money",
    ]):
        return {**base, "action": "check_sheet"}

    # Checked BEFORE move_words: "fast travel to X" / "warp to X" contain
    # "travel to" as a substring, which would otherwise shadow this as an
    # ordinary "move" — fast_travel needs to win first.
    for trigger in ["fast travel to ", "fast-travel to ", "warp to ", "teleport to ", "teleport back to "]:
        if trigger in lowered:
            name = text[lowered.index(trigger) + len(trigger):].strip()
            return {**base, "action": "fast_travel", "target": name or None}

    # "leave the " is checked here (not "leave" alone, which would be too
    # broad) -- confirmed live 2026-07-12: real messages like "I leave the
    # seller" and "Leave the tavern cellar and go back upstairs" got
    # misclassified as pass_turn (no keyword there matched either, so an
    # unchecked LLM guess went through), producing a confusing "no active
    # turn to pass" reply to someone just trying to leave and go
    # elsewhere. leave_party's own "leave the party"/"leave my party"
    # check above already wins first, so this can't shadow it.
    # "goto" (no space) confirmed live 2026-07-12: "Goto the Crossroads
    # Tavern" missed "go to" entirely (no space) and fell through to the
    # exact same pass_turn misclassification the comment above already
    # describes -- a common enough typo/one-word habit to handle directly
    # rather than relying on the player to always include the space.
    # Checked BEFORE move_words below, same reasoning as the quest-board
    # and "where can i buy" fixes above: "Where do I go to complete the
    # quest Silverleaf herbs?" is a genuine question about quest status,
    # not an actual travel command -- confirmed live 2026-07-12 this got
    # misread as move (via the bare "go to" inside the question) with no
    # destination named, producing a confusing "can't get there" reply
    # instead of showing the player their real quest state (gather-type
    # board quests already auto-complete the moment enough material is
    # gathered -- see _do_gather -- so check_quests will show them it's
    # actually done already, which is the real answer to what they asked).
    if any(w in lowered for w in ["where do i go to complete", "where do i turn in", "how do i turn in",
                                    "where do i deliver", "how do i complete the quest",
                                    "where do i complete", "how do i deliver"]):
        return {**base, "action": "check_quests"}

    # Confirmed live 2026-07-14 (Coffee, "stuck in the upper rooms"):
    # _do_move already handles generic leave/exit/downstairs/upstairs
    # phrasing for single-exit locations (its own generic_leave_words,
    # bot.py ~line 4166) -- but the intent parser never even dispatched
    # to move for "Go downstairs" or "Leave this area" in the first
    # place, since move_words only covered "leave the " (not "leave
    # this"/"leave here") and had no bare downstairs/upstairs/exit
    # phrasing at all. The handler could already resolve the
    # destination; the classifier just never sent it there, so both
    # commands fell through all the way to pass_turn, leaving the
    # player with no way to leave.
    # Checked BEFORE move_words: "I go to rest now please" / "going to
    # sleep" / "go to bed" contain "go to " as a substring and would
    # otherwise be shadowed as a literal travel command with no real
    # destination named -- confirmed live during 2026-07-18 varied-
    # phrasing playthrough testing, same "shadowing" pattern already
    # fixed for fast_travel above.
    if any(w in lowered for w in ["go to rest", "going to rest", "go to sleep", "going to sleep",
                                    "go to bed", "going to bed", "goto rest", "goto sleep", "goto bed"]):
        return {**base, "action": "rest"}

    move_words = ["go to", "goto", "head to", "walk to", "travel to", "move to", "enter the", "descend", "ascend",
                  "climb down", "climb up", "leave the ", "leave this", "leave here", "go back",
                  "go downstairs", "go upstairs", "head downstairs", "head upstairs",
                  "downstairs", "upstairs", "exit this", "exit the", "step out", "walk out",
                  "return to", "head back to", "back to the"]
    if any(w in lowered for w in move_words):
        return {**base, "action": "move"}

    if any(w in lowered for w in ["look around", "where am i", "look at my surroundings", "examine the area",
                                    "describe this place", "what's around me", "whats around me",
                                    "survey the area", "look like", "what is this place", "observe the"]):
        return {**base, "action": "look"}

    # A specific object/detail, not the whole area (that's "look" above,
    # already checked first so "examine the area" can't be shadowed).
    # Target text is matched against the current location's real
    # interactables list in bot.py — never invented, same as items.
    for trigger in ["examine the ", "examine ", "look at the ", "look closer at ", "inspect the ", "inspect ", "check out the ", "search the ", "look at "]:
        if trigger in lowered:
            target = text[lowered.index(trigger) + len(trigger):].strip()
            return {**base, "action": "examine", "target": target or None}

    # Confirmed live 2026-07-14 (Coffee, both himself and the AI party
    # independently): "Read the guest book on the landing table" and
    # "Observed the guest book" both fell all the way through to
    # pass_turn -- neither "read" nor the past tense "observed" (only
    # the imperative "observe the", and only as part of the whole-area
    # "look" trigger above, not a specific-object examine) was ever
    # covered. Coffee called this out as a real "roadblock" stopping
    # both humans and AI party members from freely interacting with
    # things -- broadened to a word-boundary regex (not another plain
    # substring in the list above) specifically because bare "read" is
    # a substring of ordinary words like "already"/"bread"/"spread",
    # which a naive substring check would misfire on.
    # "peered? at" alone missed "peer into"/"peer in" -- confirmed live
    # 2026-07-17: "Perr into the face of the wide-boled tree and say
    # hello" fell all the way through to the silent 'chat' default (no
    # reply at all) because neither this examine block nor any known-NPC
    # match ever fired for a non-NPC target like a tree. "perr" (typo for
    # "peer") is included directly rather than widened via
    # _normalize_common_typos's fuzzy threshold -- its similarity ratio
    # to "peer" (0.75) sits just under that mechanism's 0.8 cutoff, and
    # lowering the shared threshold risks new false positives on the
    # unrelated "accept" typo-tolerance it already covers.
    #
    # "touch(ed)?" added 2026-07-17 (Coffee, live): "I touch the tree"
    # wasn't covered by any keyword trigger, fell through to the
    # low-confidence 'chat' default, and the model call that followed
    # picked 'look' (whole-area) over 'examine' (the specific object
    # actually named) -- a real but non-silent misclassification, since
    # "look" still replies, just with the wrong, generic content.
    examine_verb_match = re.search(
        r"\b(?:read|observed|examined|inspected|searched|checked out|touch(?:ed)?|"
        r"looked (?:at|closer at)|(?:peer|perr)(?:ed)? (?:at|into|in)|glanced? at)\b\s+"
        r"(?:the |a |an )?(.+)",
        lowered,
    )
    if examine_verb_match:
        target = text[examine_verb_match.start(1):examine_verb_match.end(1)].strip()
        return {**base, "action": "examine", "target": target or None}

    # Live-caught (2026-07-19, Sugar): "Try opening the barrel with a
    # chalk symbol" fell all the way to 'chat' -- neither "open" nor any
    # of its verb forms appeared in either examine trigger list above,
    # the exact same "verb not covered" gap this file has hit many
    # times before (touch/peer/read/observe, all added the same way).
    # Deliberately excludes "force open"/"break down"/"smash" -- those
    # already route to a real strength skill check further below (the
    # object resists and needs to be forced), a genuinely different
    # intent from just looking inside/at something unobstructed.
    if not any(w in lowered for w in ["force open", "break down", "smash"]):
        open_match = re.search(r"\bopen(?:ing|ed)?\b\s+(?:the |a |an )?(.+)", lowered)
        if open_match:
            target = text[open_match.start(1):open_match.end(1)].strip()
            return {**base, "action": "examine", "target": target or None}

    if any(w in lowered for w in ["steal", "pickpocket", "rob the", "rob this", "swipe the", "take without paying"]):
        return {**base, "action": "steal"}

    if any(w in lowered for w in ["cast ", "i cast"]):
        return {**base, "action": "cast_spell"}

    if any(w in lowered for w in ["equip ", "wield ", "wear ", "put on the", "put on my",
                                    "i equip", "i wield"]):
        return {**base, "action": "equip_item"}

    # "eat" needs a real word-boundary check (not the bare substring style
    # used above) -- confirmed live 2026-07-18: a naive "eat " substring
    # check false-positives on any word ending in those letters followed
    # by a space ("repeat ", "retreat ", "great ", "seat ", "defeat ",
    # etc, all genuinely common in ordinary sentences), the same class of
    # bug already fixed elsewhere in this file for short substrings (see
    # items.py's "axe"/"ore" fixes). Also broadens the previous
    # exact-phrase-only "eat my rations"/"eat the rations" (which missed
    # the equally natural "Eat ration" / "I eat a ration") to any real
    # eat phrasing.
    if any(w in lowered for w in ["drink ", "quaff", "i use my", "i use the", "i use a"]) \
            or re.search(r"\beats?\b", lowered):
        return {**base, "action": "use_item"}

    # Real live bug (2026-07-18, confirmed by two independent players):
    # "join the"/"i want to join" alone are both too broad -- "I want to
    # join the hunt for wolves" and "I want to join Ravenloft on his
    # quest" both got misclassified as join_guild, since neither trigger
    # required a guild to actually be mentioned. Some real guild names
    # (Silver Wardens, Arcane Circle) don't contain the word "guild" at
    # all, so this checks for either the literal word or a real guild's
    # own name/id, not just "guild" -- genuine phrasing like "I want to
    # join the Silver Wardens" still works.
    mentions_a_real_guild = "guild" in lowered or any(
        gid.replace("_", " ") in lowered or g["name"].lower() in lowered for gid, g in GUILDS.items()
    )
    # Real live bug (2026-07-22, Coffee: "join Adventurers' Guild" /
    # "I wan to join Adventurers' Guild" got zero reply at all): the
    # phrase list below only covered "join THE X" or "I want to join
    # X", missing the equally natural bare "join X" (with or without
    # "the") and casual "wan[t] to join" typos. Safe to broaden to any
    # bare "join" since mentions_a_real_guild above already requires a
    # real guild's own name/id (or the literal word "guild") to be
    # present -- that's what keeps "I want to join the hunt for
    # wolves" from matching, not the phrase list's narrowness.
    if mentions_a_real_guild and (
        re.search(r"\bjoin\b", lowered)
        or any(w in lowered for w in [
            "become a member of", "what guilds", "which guilds",
        ])
    ):
        return {**base, "action": "join_guild"}

    pass_words = ["pass", "skip my turn", "i wait", "i'll wait", "ill wait", "wait and see", "hold my action"]
    if any(w in lowered for w in pass_words):
        return {**base, "action": "pass_turn"}

    # Checked BEFORE the plain "rest" keywords below: "rest for the
    # night" contains "i rest" as a substring and would otherwise be
    # shadowed as the in-universe full-heal action instead of this
    # real-world "done playing until next session" one.
    for trigger in ["rest for ", "resting for ", "rest for now", "take a rest", "take a break",
                     "logging off", "log off",
                     "i'm done for now", "im done for now", "done playing for now"]:
        if trigger in lowered:
            duration = text[lowered.index(trigger) + len(trigger):].strip() if trigger.endswith(" ") else ""
            return {**base, "action": "go_inactive", "target": duration or None}

    # "take a rest" is deliberately NOT here — it's claimed by go_inactive
    # above, since the user considers it equivalent to "resting until
    # next session," not the in-universe full-heal action.
    rest_words = ["i rest", "let's rest", "lets rest", "revive me", "heal up", "recover",
                  "heal me", "want to rest", "rest here"]
    if any(w in lowered for w in rest_words):
        return {**base, "action": "rest"}

    unconditional_shove_words = ["shove", "tackle", "trip", "push over"]
    knock_down_phrasing = "knock" in lowered and ("prone" in lowered or "down" in lowered)
    if any(w in lowered for w in unconditional_shove_words) or knock_down_phrasing:
        return {**base, "action": "shove"}

    if any(w in lowered for w in ["show me the map", "the map", "where have i explored", "where have i been",
                                    "my map", "show map"]):
        return {**base, "action": "show_map"}

    if any(w in lowered for w in ["bestiary", "monster compendium", "monster book",
                                    "monsters have i fought", "monsters i've fought",
                                    "monsters have i encountered", "monsters i've encountered",
                                    "what monsters"]):
        return {**base, "action": "bestiary"}

    if any(w in lowered for w in ["leaderboard", "hall of fame", "who's the best",
                                    "whos the best", "top players", "high scores",
                                    "who has the most xp", "rankings"]):
        return {**base, "action": "leaderboard"}

    if any(w in lowered for w in ["my achievements", "check achievements", "my titles",
                                    "what titles", "unlocked achievements"]):
        return {**base, "action": "check_achievements"}

    if "title" in lowered and any(w in lowered for w in ["set my title", "change my title",
                                                            "wear the title", "use the title"]):
        return {**base, "action": "set_title"}

    if any(w in lowered for w in ["what's the weather", "whats the weather", "check the weather",
                                    "how's the weather", "hows the weather", "what time of day",
                                    "is it day or night", "what's it like outside"]):
        return {**base, "action": "check_weather"}

    if any(w in lowered for w in ["guild quest", "today's guild quest", "todays guild quest"]):
        return {**base, "action": "check_guild_quest"}

    if any(w in lowered for w in ["make a campfire", "build a campfire", "start a campfire",
                                    "light a campfire", "make camp", "set up camp"]):
        return {**base, "action": "make_campfire"}

    # Confirmed live 2026-07-14 (Coffee): "I would like to chop for
    # lumber" fell all the way through to pass_turn -- lumberjacking
    # (a real gathering skill, campaign.json's old_timber_stand node)
    # had no matching verb at all here. "gather"/"forage"/"harvest"/
    # "mine"/"collect"/"pick" cover herbs/ore, but nobody naturally
    # says any of those for wood. Same audit request from Coffee
    # ("make sure all skills work... if I say go fishing, it means
    # using my fishing skill") turned up fishing had NO trigger verb
    # at all either, and mining only covered the bare word "mine".
    gather_words = ["gather", "forage", "harvest", "mine ", "dig for", "dig up", "collect", "pick the herbs",
                    "pick some flowers", "pick flowers", "pick herbs",
                    "chop", "cut down", "cut wood", "cut some wood",
                    "go fishing", "catch fish", "catch some fish", "cast a line",
                    # Confirmed live 2026-07-14 (Coffee): "Get wood from
                    # the whispering wood" also fell through -- "get "
                    # is too common a word to trigger bare (would
                    # misfire on "let me get my bearings" etc.), so only
                    # "get <real material>" phrasings are covered.
                    "get wood", "get some wood", "get ore", "get some ore",
                    "get herbs", "get some herbs", "get fish", "get some fish"]
    # Bare "fish" needs a word boundary, not a plain substring -- it's
    # embedded in ordinary words like "selfish"/"shellfish"/"finish".
    if any(w in lowered for w in gather_words) or re.search(r"\bfish\b", lowered):
        return {**base, "action": "gather"}
    # Confirmed live 2026-07-13 (Coffee, repeatedly since the night
    # before): "Pick a silverleaf herb" matched none of the fixed
    # phrasings above (they only cover "pick the/some herbs/flowers" as
    # exact substrings, not "pick a/an <specific material name>"), fell
    # through the keyword fallback entirely to its safe "chat" default,
    # which meant the small model's own answer was trusted instead --
    # and this model has a documented bias toward guessing "pass_turn"
    # for any "pick ..." phrasing it doesn't recognize (see the
    # lockpicking case further down). Result: gathering the exact
    # material a board quest needs silently did nothing, so the quest
    # could never progress no matter how many times it was retried.
    # "pick the lock"/"pick lock" is deliberately excluded -- that's a
    # real skill_check (dexterity), handled below.
    if "pick" in lowered and "lock" not in lowered:
        return {**base, "action": "gather"}

    craft_words = ["craft", "brew", "make a potion", "make an antitoxin", "make a scroll"]
    if any(w in lowered for w in craft_words):
        return {**base, "action": "craft"}

    # Ability-check verb -> ability mapping. Deliberately conservative:
    # only fires on fairly explicit risky-action phrasing, so ordinary
    # roleplay chat isn't constantly misread as a check attempt.
    #
    # 2026-07-14 (Coffee): audited against all 18 real 5E skills --
    # Arcana, Nature, Religion, Animal Handling, Insight, Medicine, and
    # Performance had NO trigger phrase at all (a player asking "do I
    # sense any magic here" or "can I calm this animal down" fell
    # through to chat, same silent-gap pattern as every other
    # intent_parser fix this session), and Sleight of Hand only had
    # "pick the lock" (lockpicking), not general dexterous trickery like
    # palming an object. This game already collapses all skills sharing
    # one ability into that ability's single practiced_bonus bucket
    # (Persuasion/Deception/Intimidation/Performance are all "charisma",
    # not four separate tracked skills) -- adding these fills the real
    # gap (a phrase with nothing to route to) without inventing new
    # per-skill tracking that doesn't exist anywhere else in this game.
    skill_check_verb_abilities = [
        (["sneak", "hide", "climb", "balance", "pick the lock", "disarm the trap", "tiptoe",
          "palm the", "pickpocket", "lift the coin purse", "plant this on", "swap the", "conceal the"], "dexterity"),
        (["lift", "push", "break down", "force open", "shove the", "smash"], "strength"),
        (["recall", "remember lore", "investigate", "decipher", "figure out the puzzle",
          "identify the magic", "sense the magic", "what spell is this", "arcane knowledge",
          "what kind of creature", "what plant is this", "identify the plant", "identify the animal",
          "what do i know about this holy site", "what religion", "identify the deity", "identify the god"], "intelligence"),
        (["search for", "look for hidden", "listen for", "listen,", "listen.", "what do i hear",
          "spot", "sense", "track", "survive", "perceive",
          "calm the animal", "calm down the", "soothe the animal", "tame the", "read them",
          "sense if they're lying", "sense if he's lying", "sense if she's lying", "gut feeling about",
          "treat the wound", "stabilize", "administer first aid", "diagnose", "identify the poison"], "wisdom"),
        (["persuade", "convince", "deceive", "lie to", "intimidate", "impress",
          "perform for", "sing for", "play music for", "put on a show", "tell a story to entertain"], "charisma"),
        (["hold my breath", "endure", "resist the poison", "push through the pain"], "constitution"),
    ]
    for verbs, ability in skill_check_verb_abilities:
        if any(v in lowered for v in verbs):
            return {**base, "action": "skill_check", "ability": ability}

    return base


def parse_intent(text: str, known_npc_names: list[str] | None = None, force_model: bool = False) -> dict:
    """
    Classify free text into a structured intent dict. Uses the build
    model (better at structured/JSON output) rather than the narration
    model. Falls back to keyword matching on any failure.

    force_model (2026-07-17, per Coffee, for bot.py's /redo command:
    "make it so when u hit /redo it actually tries to handle it
    differently"): the deterministic keyword fallback is, well,
    deterministic -- re-running it on the exact same text an admin is
    redoing would produce the IDENTICAL classification every time,
    which defeats the entire point of asking for a second attempt.
    force_model=True skips the normal fast-path shortcut below (which
    returns a confident fallback without ever calling Ollama) and
    always asks the model for its own independent read of the message,
    even when the keyword fallback is confident. The valid_actions
    allowlist and the start_combat/pass_turn anti-hallucination guards
    further down still apply unconditionally either way -- those exist
    to catch real, previously-confirmed model hallucination patterns,
    not to prefer the fallback on principle, so a manual redo is not a
    safe place to relax them. The one thing force_model DOES relax is
    the general "fallback disagrees with the model, trust the
    fallback" rule -- that rule is exactly what would otherwise hand
    back the same answer as the very first attempt.
    """
    known_npc_names = known_npc_names or []
    fallback = _keyword_fallback(text, known_npc_names)

    # Real perf fix (2026-07-17, per Coffee -- investigated after a
    # night of watching real Ollama latency firsthand): every dispatch
    # branch in bot.py's adventure_master_handler reads either
    # intent["raw_text"] (always identical to fallback's own raw_text,
    # since _keyword_fallback's base dict sets it to the same `text`)
    # or a field (target/npc_name/ability) that _keyword_fallback
    # already sets correctly whenever it confidently matches that exact
    # action -- audited every branch to confirm this, not assumed.
    # Combined with the fact that the rest of this function ALREADY
    # discards the model's own answer whenever it disagrees with a
    # confident (non-"chat") fallback (see the check below), calling
    # Ollama at all in that case was pure wasted latency -- 15-160s+ on
    # this CPU-only box, for an answer that was never going to be used
    # either way. Skipping the call here changes nothing about the
    # RESULT (still exactly what a confident fallback already produces
    # today), it just stops paying for work already known to be
    # discarded. Only genuinely ambiguous messages (fallback says
    # "chat", meaning it has no real opinion) still need the model's
    # actual language understanding below.
    if fallback["action"] != "chat" and not force_model:
        return fallback

    try:
        response = requests.post(
            f"{config.OLLAMA_BASE_URL}/api/generate",
            json={
                "model": config.BUILD_MODEL,
                "prompt": f"{INTENT_SYSTEM_PROMPT}\n\nKnown NPCs: {known_npc_names}\n\nPlayer message: {text}",
                "stream": False,
                "format": "json",
                # Real perf fix (2026-07-17): bounds worst-case
                # generation time, same reasoning as ai/dm_agent.py's
                # _NARRATION_OPTIONS -- smaller here since the actual
                # answer needed is just one short JSON object, not
                # multi-sentence prose.
                "options": {"num_predict": 400},
            },
            timeout=200,
        )
        response.raise_for_status()
        data = response.json()
        raw_response = data.get("response", "")
        parsed = _extract_json(strip_think_tags(raw_response))
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
                "buy", "sell", "steal", "cast_spell", "join_guild", "recruit_npc", "rest",
                "go_inactive", "skill_check", "shove", "show_map", "gather", "craft",
                "list_characters", "switch_character", "delete_character",
                "fast_travel", "accept_quest", "check_quests", "ask_clue",
                "answer_puzzle", "gamble", "chat", "examine", "flee", "resolve_choice",
                "invite_to_party", "accept_party_invite", "leave_party", "find_merchant",
                "second_wind", "rage", "bardic_inspiration", "lay_on_hands", "arcane_recovery",
                "make_campfire", "give_item", "use_item", "equip_item", "auto_equip", "breath_weapon",
                "channel_divinity", "action_surge", "reckless_attack", "divine_smite",
                "flurry_of_blows", "wild_shape", "toggle_manual_dice", "level_up", "set_description", "set_pronouns",
                "bestiary", "list_shop", "leaderboard", "check_achievements", "set_title", "check_weather",
                "check_guild_quest", "dice_game", "fortunes_wheel", "set_alignment", "message_ai",
                "skill_tree", "challenge_duel", "accept_duel", "check_market", "join_battle",
                "replay_intro", "visual_map", "rebirth", "choose_hybrid",
            )
            if parsed["action"] not in valid_actions:
                return fallback
            # The model reliably extracts npc_name even when it mislabels
            # the action itself — confirmed live: "Say hello to grimsby"
            # came back as action="chat", npc_name="Grimsby", which
            # produces zero reply by design (chat is intentionally
            # silent). Any known NPC name attached to the intent means
            # the player addressed that NPC, regardless of what the
            # model called the action.
            if parsed["action"] == "chat" and parsed.get("npc_name"):
                parsed["action"] = "talk_npc"
            # Confirmed live, repeatedly, on this small CPU-bound model
            # once the action-type list grew large: it misclassifies
            # against an explicit, unambiguous keyword trigger — "my
            # characters" came back as "start_combat", "show me the map"
            # came back as "move", and "I try to pick the lock on the
            # offering chest" came back as "pass_turn". _keyword_fallback
            # is deliberately conservative by design (its own docstring:
            # only fires on fairly explicit phrasing, defaults to "chat"
            # otherwise) — so whenever it has ANY non-default opinion,
            # that opinion is trusted over the model's, rather than
            # special-casing just the newest action types.
            if fallback["action"] != "chat" and parsed["action"] != fallback["action"] and not force_model:
                return fallback
            # Extra, narrower safeguard on top of the general rule above:
            # "start_combat" specifically is never trusted from the model
            # unless the raw text actually contains one of its explicit
            # trigger phrases — confirmed live twice now that the model
            # guesses this as a fallback answer for unrelated text ("I'm
            # back" started a fight). Falls through to the keyword
            # fallback's own (safe) classification instead.
            if parsed["action"] == "start_combat" and not any(w in text.lower() for w in COMBAT_START_WORDS):
                return fallback
            # Same defensive pattern as start_combat above: this model has
            # a documented bias toward guessing "pass_turn" for phrasing
            # it doesn't recognize (see the "pick a silverleaf herb" case
            # further up) -- confirmed live again 2026-07-16, "I'll take a
            # mug, ale!!! how are you doing old buddy?" (ordinary tavern
            # small talk, no active turn to pass) came back as pass_turn
            # and produced a confusing "There's no active turn to pass
            # right now" reply. Never trusted from the model alone unless
            # the keyword fallback independently agrees -- it already
            # would have been returned by the general rule above if so.
            if parsed["action"] == "pass_turn":
                return fallback
            return parsed
    except (requests.RequestException, ValueError) as e:
        print(f"[intent_parser] model call failed, using keyword fallback: {e}")

    return fallback


_COMPOUND_SPLIT_PATTERN = re.compile(r",\s*(?:and\s+)?|\s+and then\s+|\s+then\s+|\s+and\s+|;\s*")

# Actions where a player commonly lists several items in one message
# ("buy 10 torches, 1 pickaxe, 5 bait") -- see parse_intents below.
_SHOPPING_LIST_ACTIONS = {"buy", "sell", "give_item", "equip_item"}
# What a bare "<qty> <item>" segment (no verb of its own) tends to
# misclassify as, once split away from the verb that gave it context.
_BARE_ITEM_MISFIRES = {"chat", "gather", "craft"}


def _split_compound_message(text: str) -> list[str]:
    """
    Splits on strong, explicit multi-clause separators: commas, "and
    then", "then", ";", and (2026-07-12, extended) a bare " and " too.
    Bare "and" was deliberately excluded at first, on the reasoning that
    it's exactly as likely to join two NOUNS in one action ("attack the
    goblin and the wolf") as it is to join two separate actions -- but
    confirmed live 2026-07-12 that excluding it silently drops entire
    requests instead: "Invite Sarah to join my party and check my
    inventory" never split at all (no comma/"then"), fell through to
    single-action classification, and check_inventory was silently
    never run. The caller (parse_intents) already only trusts a split
    if multiple resulting segments independently classify to DIFFERENT
    real (non-chat) actions -- that's exactly what makes bare "and"
    safe to split on now too: "attack the goblin and the wolf" splits
    into "attack the goblin" (action) and "the wolf" (classifies as
    "chat", not a second real action), so it correctly falls back to
    single-action classification unchanged, while a genuine two-action
    "and" message now actually gets both actions run.
    """
    segments = _COMPOUND_SPLIT_PATTERN.split(text)
    cleaned = []
    for s in segments:
        s = s.strip()
        # A leading "then"/"and" can survive the split itself (e.g. after
        # a comma, "...board, then leave" splits into "then leave" with
        # "then" stuck to the front since the whitespace on either side
        # of it was already consumed by the comma match) -- purely
        # cosmetic for logging/narration, doesn't change classification.
        for lead in ("then ", "and then ", "and "):
            if s.lower().startswith(lead):
                s = s[len(lead):].strip()
                break
        if s:
            cleaned.append(s)
    return cleaned


def parse_intents(text: str, known_npc_names: list[str] | None = None, force_model: bool = False) -> list[dict]:
    """
    Like parse_intent, but detects genuinely compound player messages
    ("recruit Sarah, look at the quest board, and leave the tavern") and
    returns one intent per real action instead of just the first guess.

    Confirmed live 2026-07-12: a real compound message like this was
    classified as a SINGLE action (whatever keyword happened to match
    first), silently dropping every other requested step -- e.g.
    "recruit Sarah... look at the quest board... leave the tavern" only
    ever ran check_quests, everything else was silently ignored.

    Deliberately conservative, and deliberately keyword-only (no extra
    Ollama calls -- classifying N segments would mean N sequential
    30-160s+ calls on this CPU-only setup, unacceptable latency for
    what's still usually a single, ordinary action):
    1. Split the text on explicit multi-clause separators only.
    2. Classify EACH segment with the free, instant keyword fallback.
    3. Only treat this as a genuine multi-action message if at least
       TWO segments independently produce a real, non-chat action --
       a weak or ambiguous split (a stray comma in an otherwise single
       action, "attack the goblin and the wolf") falls through to the
       existing single-message classification unchanged, exactly as
       before this feature existed, since only ONE segment there
       ("attack the goblin") ever produces a real action -- "the wolf"
       alone classifies as chat and is filtered out either way.

       Real live bug (2026-07-16): this used to also require the two
       real actions be of DIFFERENT types (`len({...actions...}) >= 2`),
       which correctly handled "recruit X, look at Y, leave Z" (three
       different action types) but silently broke the equally common
       same-action-repeated compound -- "Go to the crossroads Tavern,
       and then go to the whispering wood" split into two segments that
       BOTH independently and explicitly matched "go to" (two real
       "move" actions), but since they were the same action type, the
       old distinctness check saw only 1 distinct action and fell
       through to single-message parsing, which only ever acted on the
       first destination and silently dropped the second. Segments are
       independently classified by the same deliberately-conservative
       keyword fallback either way, so requiring 2+ REAL segments
       (rather than 2+ DISTINCT action types) is no less safe -- it
       just also catches genuine multi-step requests that happen to
       repeat the same verb.
    """
    known_npc_names = known_npc_names or []
    segments = _split_compound_message(text)
    if len(segments) > 1:
        segment_intents = [_keyword_fallback(seg, known_npc_names) for seg in segments]

        # Real live bug (2026-07-17, Coffee): "Buy 10 torches, 1 shears,
        # 1 pickaxe, 1 fishing pole, 5 bait." only ever bought the
        # torches. Only the FIRST segment carries the "buy" verb --
        # every segment after it is a bare "<qty> <item>" phrase with no
        # verb of its own, so the per-segment keyword fallback either
        # shrugs (chat) or, worse, false-fires on unrelated over-eager
        # vocabulary ("1 pickaxe" contains the substring "pick", which
        # the gather fallback's bare-"pick" rule -- meant for "pick the
        # herbs" -- happily matches). Splitting into separate actions
        # here would drop every item after the first, or invent bogus
        # actions for them. Detected generally, not just for "buy":
        # if the FIRST segment is a shopping-list-style action (buy,
        # sell, give_item, equip_item) and every OTHER segment either
        # repeats that same action or is one of the known bare-noun-
        # phrase misfires, treat the WHOLE message as ONE action instead
        # of splitting -- the handler (_do_buy/_do_sell/_do_give_item/
        # _do_equip_item) is responsible for finding every item
        # mentioned across the full text, not just the first.
        first_action = segment_intents[0]["action"]
        if first_action in _SHOPPING_LIST_ACTIONS and all(
            i["action"] == first_action or i["action"] in _BARE_ITEM_MISFIRES
            for i in segment_intents[1:]
        ):
            return [parse_intent(text, known_npc_names, force_model=force_model)]

        real_segment_intents = [i for i in segment_intents if i["action"] != "chat"]
        if len(real_segment_intents) >= 2:
            return real_segment_intents

    return [parse_intent(text, known_npc_names, force_model=force_model)]
