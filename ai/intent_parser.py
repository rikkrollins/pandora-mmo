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
- "join_guild" is for joining/asking to join a specific guild or order.
- "pass_turn" is for skipping, waiting, or passing.
- "resolve_choice" is for declaring a decision on a moral choice/quest resolution (e.g. "I choose to...", "I'll go with...").
- "invite_to_party" is for inviting another player's or AI companion's character into their own formed party. Set "target" to the invitee's name.
- "accept_party_invite" is for accepting a pending party invite.
- "leave_party" is for leaving a party the character is currently in.
- "find_merchant" is for asking where to get supplies or find the nearest shop/merchant.
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
    if any(w in lowered for w in ["my quests", "quest journal", "quest log", "my quest log",
                                    "quest board", "the board", "what's on the board",
                                    "quest details", "current quest", "my quest", "what quest",
                                    "what's my quest", "whats my quest", "active quest"]):
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
    if any(w in lowered for w in ["buy", "purchase"]):
        return {**base, "action": "buy"}

    if lowered.startswith("sell") or " sell " in lowered:
        return {**base, "action": "sell"}

    # "recruit X" is checked BEFORE the known-NPC-name loop below and
    # regardless of whether the name matches one exactly -- confirmed
    # live 2026-07-12, twice: "Recruit Sera to my party" (correct
    # spelling, a known NPC) still came back as talk_npc, because the
    # only recruit_words phrases below all require the word "join",
    # never the word "recruit" itself, even though that's the action's
    # own name and the single most obvious way a player would phrase
    # it. "Recruit Seta to my party" (a likely typo/mishearing of
    # "Sera") is even worse off: it doesn't match any known NPC name at
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
    # 2026-07-12: "Invite Sera to my party" (Sera IS a real recruitable
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

    recruit_words = ["join us", "join our party", "join my party", "come with us",
                      "travel with us", "come along", "join the party"]
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
        name_words = [w for w in npc_name.lower().split() if len(w) >= 3]
        if npc_name.lower() in lowered or any(
            re.search(r"\b" + re.escape(w) + r"\b", lowered) for w in name_words
        ):
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

    attack_words = ["attack", "swing", "shoot", "strike", "hit", "stab", "cast at", "fire at"]
    if any(w in lowered for w in attack_words):
        return {**base, "action": "attack"}

    if any(w in lowered for w in COMBAT_START_WORDS):
        return {**base, "action": "start_combat"}

    if any(w in lowered for w in ["create a character", "make a character", "new character", "join the game"]):
        return {**base, "action": "create_character"}

    if any(w in lowered for w in ["my backpack", "my bag", "my inventory", "what am i carrying", "what do i have",
                                    "check inventory", "show inventory", "view inventory", "what items", "my items",
                                    "what weapons", "check my equipment", "my equipment"]):
        return {**base, "action": "check_inventory"}

    if any(w in lowered for w in ["who's in my party", "whos in my party", "my party", "who is with me",
                                    "who's with me", "am i in a party", "party status", "party members"]):
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
    if any(w in lowered for w in ["i choose", "i decide to", "i decided to", "i've decided", "ive decided",
                                    "i have decided", "i'll go with", "ill go with",
                                    "my choice is", "i'll take the", "ill take the",
                                    "keep it and collect the reward", "leave it be instead"]):
        return {**base, "action": "resolve_choice"}

    if any(w in lowered for w in ["ask for a clue", "ask for clues", "give me a clue", "any clues",
                                    "what's the clue", "need a hint", "give me a hint",
                                    "ask for a hint", "what clues"]):
        return {**base, "action": "ask_clue"}

    if any(w in lowered for w in ["i accept", "i'll do it", "ill do it", "count me in", "i'll help",
                                    "ill help", "i'll take the job", "i'll take it on"]):
        return {**base, "action": "accept_quest"}

    if any(w in lowered for w in ["the answer is", "my answer is", "i think it's", "i think the answer is",
                                    "could it be"]):
        return {**base, "action": "answer_puzzle"}

    if any(w in lowered for w in ["gamble", "wager", "place a bet", "i bet", "let's bet", "lets bet",
                                    "play dice for gold", "bet "]):
        return {**base, "action": "gamble"}

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

    # Checked BEFORE check_sheet below: "my characters" (plural, roster) is
    # a substring-superset of check_sheet's "my character" (singular) —
    # confirmed live to otherwise get shadowed and misread as check_sheet,
    # so the more specific roster/switch/delete phrasings must win first.
    if any(w in lowered for w in ["my characters", "show my characters", "list my characters",
                                    "character roster", "my roster"]):
        return {**base, "action": "list_characters"}

    for trigger in ["switch to ", "switch character to ", "play as "]:
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

    # Confirmed live 2026-07-14 (Coffee): "Show me SERA's character
    # sheet" -- asking for a SPECIFIC party member's sheet by name, not
    # "my sheet" or "my party's sheets" (already handled elsewhere) --
    # matched nothing here at all and fell through all the way to
    # "examine", which searched for an interactable object named "SERA"
    # and correctly (given what it was asked) found nothing. Checked
    # before the "my sheet" block below since a possessive name always
    # means someone else's sheet is wanted, never the asker's own.
    named_sheet_match = re.search(r"(\w+)'s (?:character )?sheet", lowered)
    if named_sheet_match:
        return {**base, "action": "check_sheet", "target": named_sheet_match.group(1)}

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

    move_words = ["go to", "goto", "head to", "walk to", "travel to", "move to", "enter the", "descend", "ascend",
                  "climb down", "climb up", "leave the ", "go back"]
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
    for trigger in ["examine the ", "examine ", "look at the ", "look closer at ", "inspect the ", "inspect ", "check out the ", "search the "]:
        if trigger in lowered:
            target = text[lowered.index(trigger) + len(trigger):].strip()
            return {**base, "action": "examine", "target": target or None}

    if any(w in lowered for w in ["steal", "pickpocket", "rob the", "rob this", "swipe the", "take without paying"]):
        return {**base, "action": "steal"}

    if any(w in lowered for w in ["cast ", "i cast"]):
        return {**base, "action": "cast_spell"}

    if any(w in lowered for w in ["join the", "i want to join", "become a member of",
                                    "join a guild", "guilds can i join", "what guilds",
                                    "which guilds"]):
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

    if any(w in lowered for w in ["make a campfire", "build a campfire", "start a campfire",
                                    "light a campfire", "make camp", "set up camp"]):
        return {**base, "action": "make_campfire"}

    gather_words = ["gather", "forage", "harvest", "mine ", "collect", "pick the herbs",
                    "pick some flowers", "pick flowers", "pick herbs"]
    if any(w in lowered for w in gather_words):
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
        (["search for", "look for hidden", "listen for", "spot", "sense", "track", "survive", "perceive",
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
                "make_campfire",
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
            if fallback["action"] != "chat" and parsed["action"] != fallback["action"]:
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
            return parsed
    except (requests.RequestException, ValueError) as e:
        print(f"[intent_parser] model call failed, using keyword fallback: {e}")

    return fallback


_COMPOUND_SPLIT_PATTERN = re.compile(r",\s*(?:and\s+)?|\s+and then\s+|\s+then\s+|\s+and\s+|;\s*")


def _split_compound_message(text: str) -> list[str]:
    """
    Splits on strong, explicit multi-clause separators: commas, "and
    then", "then", ";", and (2026-07-12, extended) a bare " and " too.
    Bare "and" was deliberately excluded at first, on the reasoning that
    it's exactly as likely to join two NOUNS in one action ("attack the
    goblin and the wolf") as it is to join two separate actions -- but
    confirmed live 2026-07-12 that excluding it silently drops entire
    requests instead: "Invite Sera to join my party and check my
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


def parse_intents(text: str, known_npc_names: list[str] | None = None) -> list[dict]:
    """
    Like parse_intent, but detects genuinely compound player messages
    ("recruit Sera, look at the quest board, and leave the tavern") and
    returns one intent per real action instead of just the first guess.

    Confirmed live 2026-07-12: a real compound message like this was
    classified as a SINGLE action (whatever keyword happened to match
    first), silently dropping every other requested step -- e.g.
    "recruit Sera... look at the quest board... leave the tavern" only
    ever ran check_quests, everything else was silently ignored.

    Deliberately conservative, and deliberately keyword-only (no extra
    Ollama calls -- classifying N segments would mean N sequential
    30-160s+ calls on this CPU-only setup, unacceptable latency for
    what's still usually a single, ordinary action):
    1. Split the text on explicit multi-clause separators only.
    2. Classify EACH segment with the free, instant keyword fallback.
    3. Only treat this as a genuine multi-action message if at least
       TWO segments independently produce DIFFERENT, non-chat actions
       -- a weak or ambiguous split (a stray comma in an otherwise
       single action, "attack the goblin and the wolf") falls through
       to the existing single-message classification unchanged, exactly
       as before this feature existed.
    """
    known_npc_names = known_npc_names or []
    segments = _split_compound_message(text)
    if len(segments) > 1:
        segment_intents = [_keyword_fallback(seg, known_npc_names) for seg in segments]
        distinct_real_actions = {i["action"] for i in segment_intents if i["action"] != "chat"}
        if len(distinct_real_actions) >= 2:
            return [i for i in segment_intents if i["action"] != "chat"]

    return [parse_intent(text, known_npc_names)]
