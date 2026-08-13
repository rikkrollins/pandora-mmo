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
import rules.leveling as leveling
import spells as spells_module
from ai.text_cleanup import strip_think_tags
from guilds import GUILDS
from remnants import REMNANTS

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
talk_npc, talk_party, use_environment, move, look, check_inventory, check_party, buy, sell, steal, cast_spell, join_guild, \
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
- "throw_weapon" is for throwing a carried weapon (not the one equipped) at an enemy, e.g. "throw my dagger at the goblin", "I hurl the axe at it".
- "flee" is for trying to run away, escape, or retreat from an active fight (a real risk, not guaranteed).
- "talk_npc" is for addressing a specific named NPC conversationally.
- "talk_party" is for speaking, calling out, or addressing the party/traveling companions in general \
(not a specific named NPC -- that's "talk_npc"), e.g. "I speak up", "let's talk about this place", \
"I yell for the others' thoughts", "I tell the party what I think", "I shout", "I scream in frustration".
- "use_environment" is for using the surroundings/environment against an enemy in combat (e.g. "use the \
environment", "use my surroundings against it", "interact with the environment") -- not a specific named object.
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
- "steal" is for stealing, pickpocketing, robbing, or taking something without paying — from a shop OR, \
mid-combat, pickpocketing a live enemy — a real risk of getting caught, with real consequences, not the \
same as "buy".
- "cast_spell" is for casting/using a named spell.
- "use_item" is for drinking/using/consuming/quaffing a carried consumable item (e.g. a potion, antitoxin, \
rations) — NOT a spell and NOT a shop purchase. Set "item_name" to the item, and "target" to who it's for if named (defaults to self).
- "give_offering" is for praying at a shrine and leaving a real offering (gold, not an item) to revive a dead \
party member — different from casting a revival spell. Set "target" to the dead party member being prayed for.
- "equip_item" is for equipping/wielding/wearing/putting on a weapon or piece of armor they're carrying \
(e.g. "equip my longsword", "wear the chain mail", "wield the dagger", "equip Sarah with the longbow"). \
Set "item_name" to the item, and "target" to who it's for if a specific OTHER party member is named (defaults to self).
- "unequip_item" is the inverse: taking off a currently-worn ring/amulet/wondrous item \
(e.g. "take off my ring", "unequip the amulet", "remove my cloak"). Set "item_name" to the item.
- "auto_equip" is for asking the game to automatically equip the best weapon/armor/shield being carried, \
without naming a specific item (e.g. "auto equip my character", "put on my gear automatically", \
"help me equip my player"). Set "target" to a specific party member's name if named, else self.
- "forge_item" is for upgrading a real, previously found/crafted magic item to a higher tier at the forge \
(e.g. "forge my longsword", "forge the chain shirt"). Set "item_name" to the item.
- "enchant_item" is for adding a new magic effect to a real, previously found/crafted item -- "enchant" and \
"imbue" mean the same thing here (e.g. "enchant my longsword with flame", "imbue the shield with warding"). \
Set "item_name" to the item.
- "discard_item" is for permanently scrapping/throwing away an item from inventory, no refund \
(e.g. "discard my rusty dagger", "scrap the longsword"). Set "item_name" to the item.
- "join_guild" is for joining/asking to join a specific guild or order.
- "leave_guild" is for leaving/quitting a guild the player is already a member of.
- "assign_summoner" is for naming a party member as the party's Summoner (e.g. "assign Sarah as summoner", "make me the summoner").
- "summon_remnant" is for calling forth a bound Remnant in combat (e.g. "summon The Wrathflame Unbound", "call forth my Remnant on the goblin").
- "pass_turn" is for skipping, waiting, or passing.
- "resolve_choice" is for declaring a decision on a moral choice/quest resolution (e.g. "I choose to...", "I'll go with...").
- "invite_to_party" is for inviting another player's or AI companion's character into their own formed party. Set "target" to the invitee's name.
- "accept_party_invite" is for accepting a pending party invite.
- "leave_party" is for leaving a party the character is currently in.
- "bench_party_member" is for sitting a real party member out of the next fight without them leaving the party \
(e.g. "bench Zara", "sit Grimsby out", "Zara stays out of this one"). Set "target" to their name.
- "unbench_party_member" is for putting a benched party member back into the active fighting roster \
(e.g. "unbench Zara", "bring Zara back", "Zara's back in"). Set "target" to their name.
- "set_front_row" is for moving a character to the front row battle formation (takes point, gets targeted \
first, but no evade bonus) (e.g. "move Zara to the front", "put me in front", "Zara takes point", "push up", \
"advance", "hold the line", "move up"). Set "target" to their name, or omit/self if unnamed.
- "set_back_row" is for moving a character to the back row battle formation (less likely to be targeted, \
harder to hit, free to heal/cast/shoot from safety) (e.g. "put Zara in the back row", "I'll hang back", \
"move me to the back", "Zara stays behind me", "pull back", "pull Zara back", "cover me", "fall back", \
"get behind me", "retreat to the back"). This is a real-time tactical repositioning during a fight, distinct \
from "flee" (which tries to escape the fight entirely) -- only classify as flee if they clearly mean leaving \
combat, not just moving within it. Set "target" to their name, or omit/self if unnamed.
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
- "auto_level_up_party" is for auto-applying pending ability points AND affordable skill-tree upgrades across the \
whole party at once (e.g. "auto level up the party", "auto assign skill points to the party", "level up everyone"), \
never just the one character talking.
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


def _fuzzy_match_spell_name(clause: str) -> str | None:
    """
    Whether `clause`, squashed (spaces removed), closely matches a real
    spell's squashed name -- shared by _keyword_fallback's "use/cast
    <spell>" check and its later bare-invocation fallback (2026-08-13),
    so a small typo survives either phrasing. Comparing the ratio over
    the WHOLE clause (never a substring search) is what keeps this safe
    from false-positiving against an unrelated sentence that merely
    shares a couple of letters with some real spell name -- see each
    call site for why it's checked exactly where it is.
    """
    squashed_clause = clause.replace(" ", "").strip()
    if not squashed_clause:
        return None
    best_spell, best_ratio = None, 0.0
    for spell in spells_module.SPELLS.values():
        squashed_name = spell["name"].lower().replace(" ", "")
        if abs(len(squashed_clause) - len(squashed_name)) > 2:
            continue
        ratio = difflib.SequenceMatcher(None, squashed_clause, squashed_name).ratio()
        if ratio >= 0.82 and ratio > best_ratio:
            best_spell, best_ratio = spell["name"], ratio
    return best_spell


def _split_target_clause(text: str) -> tuple[str, str | None]:
    """Splits off a trailing "at/on/against <target>" clause, if present."""
    match = re.search(r"\s+(?:at|on|against)\s+", text)
    if match:
        return text[:match.start()], text[match.end():].strip()
    return text, None


def _keyword_fallback(text: str, known_npc_names: list[str], environment_name: str | None = None) -> dict:
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
    # Real live bug (2026-07-23, Coffee: "Talk to Ossian Vane 'how are
    # you? What can you tell me about the whispering woods?'" got "Not
    # sure who you're talking to -- name a party member"): the bare "
    # tell " substring check above also fires on "what can you TELL ME
    # about X" -- a completely ordinary question aimed at whoever's
    # just been named/talked to, not an instruction being relayed to a
    # party member. "tell me" specifically is never that instruction
    # shape ("tell <name> to <verb>"), so it's excluded here.
    if (lowered.startswith("tell ") or " tell " in lowered) and "tell me" not in lowered:
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
    # Real live bug (2026-08-09, found via topic-activity monitoring):
    # "I head to The Goblin Warrens to accept the quest." came back as
    # accept_quest instead of move -- the check just below unconditionally
    # wins even when "accept the quest" is only the STATED PURPOSE of an
    # explicit "head to <destination>" travel clause, not the player's
    # actual immediate action. _do_accept_quest always checks
    # character['current_location'], never a stated destination, so this
    # silently accepted whatever was available wherever the player
    # ALREADY was (or replied "nothing to accept") instead of moving them
    # to Goblin Warrens at all -- worse than the analogous "head to X to
    # buy Y" phrasing (already reviewed live and confirmed safe: _do_buy
    # hard-gates on a shop actually being present), accept_quest has no
    # such gate and could silently accept a real but unrelated quest.
    # Mirrors the "go to the market row" fix above: an explicit movement
    # clause with a real destination should win over a same-sentence
    # purpose infinitive. Doesn't touch the comma/"then"-separated case
    # ("go to the tavern, then accept the quest") -- that already splits
    # into two real actions via parse_intents's compound-message handling
    # before this function ever sees a single merged segment.
    move_then_accept = re.search(r"\b(?:go to|goto|head to|walk to|travel to|move to)\b.+\bto\s+accept\b", lowered)
    if not move_then_accept and any(w in lowered for w in ["accept the quest", "accept this quest", "accept that quest"]):
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
                                    "any quests", "any quest", "is there a quest", "are there any quests",
                                    # Real live incident (2026-08-06, a real player): "Look at
                                    # quests" fell through every phrase above (none of them are a
                                    # bare "look at quest(s)" with no qualifier word) all the way
                                    # to the generic "look at <object>" examine handler, which then
                                    # tried to examine a literal in-world object named "quests" --
                                    # same recurring gap this block has hit many times before (see
                                    # the task numbers in the comment above), just with "look at"
                                    # as the new filler phrasing instead of "check"/"show"/"any".
                                    "look at quest", "view my quest", "view quest", "see my quest"])
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
            # Real live bug (2026-07-22, caught by the full-playthrough
            # simulation): "I recruit Sarah INTO my party" isn't covered
            # by any "to X" cut phrase below, so the whole trailing
            # "Sarah into my party" got passed as the npc_name verbatim
            # -- doesn't match any real NPC, so _do_recruit_npc's own
            # "no one by that name" fires even though Sarah IS real and
            # recruitable. Same gap shape as every other missing-
            # preposition-variant bug this session.
            for cut in (" to my party", " to the party", " to our party", " to join", " to my group",
                        " into my party", " into the party", " into our party", " into my group"):
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
    # "ti my/the party" added 2026-08-06 (real player, caught via
    # topic-activity monitoring): "Invite Pip Thistledown ti my party"
    # (typo for "to") matched none of the 4 exact phrases below, so this
    # whole block was skipped and the known-NPC-name loop further down
    # returned talk_npc instead -- the exact same root cause as the
    # 2026-07-12 "Sarah" fix documented above, just via a typo'd
    # preposition instead of the check being in the wrong order.
    for trigger in ["invite ", "let "]:
        if trigger in lowered and ("to my party" in lowered or "to the party" in lowered
                                    or "join my party" in lowered or "join the party" in lowered
                                    or "ti my party" in lowered or "ti the party" in lowered):
            name = text[lowered.index(trigger) + len(trigger):].strip()
            for cut in (" to my party", " to the party", " join my party", " join the party",
                        " ti my party", " ti the party"):
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

    # Real live bug (2026-08-06, confirmed live via topic-activity
    # monitoring): "Give Pan bracers of the steady hand" fell through
    # to a plain "chat" reply, and "Give Vesh ring of undertow" got
    # hijacked to talk_npc by the known-NPC-name loop below -- neither
    # has " to " (the first check above) nor an article word right after
    # the recipient's name (the second check above). Real players skip
    # the article on a multi-word item name just as often as they
    # include it. Detected here via the same capitalized-recipient/
    # @-tag signal as the check above, but requiring at least TWO more
    # words after the recipient -- real item names in this game are
    # essentially always multi-word ("ring of undertow", "bracers of
    # the steady hand", "woodcutters axe"), which keeps this narrower
    # than a bare one-word "give Pan space/trouble/credit" false
    # positive would need to be to slip through.
    if re.search(r"\b(?:[Gg]ive|[Hh]and|[Tt]rade|[Ss]end)\s+(?:[A-Z]\w+|@\w+)\s+\w+\s+\w+", text):
        return {**base, "action": "give_item"}

    # Real live bug (2026-08-08, confirmed live via topic-activity
    # monitoring): "Give pan health potion" fell through to a silent
    # "chat" reply -- both dative-construction checks above rely on
    # CAPITALIZATION (or an @-tag) as their only signal that a word is
    # a real recipient name, but real players type in lowercase
    # constantly, especially mid-combat. Same "at least two more words
    # after the recipient" multi-word-item-name heuristic as the
    # 2026-08-06 fix above, just case-insensitive -- with an explicit
    # pronoun denylist (checked here, not baked into the regex) so
    # "give me a hand", "hand it over", "send them away" don't newly
    # false-positive now that capitalization is no longer required to
    # gate this. Downstream recipient resolution (bot.py's
    # _do_give_item -> _match_member_by_name_or_username) already
    # matches case-insensitively -- this only had to fix classification.
    # Excludes pronouns (a lowercase "give me/it/him/her/them a hand"
    # etc. was never a real named recipient) AND articles/possessives
    # (caught live while testing this very fix: "give the sword away"
    # regressed to a false give_item without this -- "the" is never a
    # real recipient name either).
    _GIVE_ITEM_NON_RECIPIENTS = {
        "me", "myself", "it", "him", "her", "them", "himself", "herself", "themselves", "us",
        "the", "a", "an", "my", "his", "their", "your", "our", "this", "that", "these", "those",
    }
    dative_match = re.search(r"\b(?:give|hand|trade|send)\s+(\w+)\s+\w+\s+\w+", lowered)
    if dative_match and dative_match.group(1) not in _GIVE_ITEM_NON_RECIPIENTS:
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

    # Checked BEFORE the known-NPC-name loop below, same reasoning as
    # give_item/recruit_npc/quest-board above (2026-08-02, real live bug,
    # Coffee: "Use a scroll of revivify on Wren" -- mid-fight, trying to
    # revive a downed companion -- got completely swallowed by the loop
    # below into an unrelated ambient "Wren says a line about her garden"
    # reply, since Wren's own name is a real known NPC/companion name and
    # the loop ran first). This exact bug was "fixed" once already
    # (2026-07-24, Laurrienna) by adding the scroll->cast_spell check
    # further down in this function -- but that fix landed AFTER this
    # loop, so it only ever actually helped when the scroll message
    # DIDN'T also name a known NPC, which is precisely the case that
    # matters most (using an item ON someone). Moved here so naming the
    # target no longer defeats it.
    if any(w in lowered for w in ["cast ", "i cast"]) or ("scroll" in lowered and re.search(r"\buse\b", lowered)):
        return {**base, "action": "cast_spell"}

    # "Use <spell>"/"cast <spell>" (2026-08-13, real live bug found via
    # topic-activity monitoring): the SAME player who typo'd "Eldricth
    # blast" (fixed by the bare-invocation check further down this
    # function) also tried the correctly-spelled "Use eldritch blast on
    # the goblin" moments later and STILL lost -- a spell, unlike an
    # item, has no natural "the/my/a" article before it, but it landed
    # in the "on"-based use_item check just below anyway ("use ... on"
    # matches regardless of what's actually named), which runs BEFORE
    # the later bare-invocation check ever gets a chance. Checked here,
    # immediately after the scroll->cast_spell check above (same
    # priority tier, same reasoning: a real spell name after "use"/
    # "cast" must win before any generic item-use guess), so both "use
    # eldritch blast" (no target) and "use eldritch blast on the
    # goblin" (with one) resolve correctly. Reuses
    # _fuzzy_match_spell_name, the same typo-tolerant matcher the
    # bare-invocation check further down uses.
    _use_cast_match = re.search(r"\b(?:use|cast)\b\s+(.+)", lowered)
    if _use_cast_match:
        _use_cast_clause, _use_cast_target = _split_target_clause(_use_cast_match.group(1))
        _use_cast_spell = _fuzzy_match_spell_name(_use_cast_clause)
        if _use_cast_spell:
            return {**base, "action": "cast_spell", "spell_name": _use_cast_spell, "target": _use_cast_target}

    # Real live bug (2026-08-08, confirmed live twice -- a real dev-
    # topic screenshot AND independently via topic-activity
    # monitoring): "Use health potion on Vesh Nightglass" got hijacked
    # to talk_npc by the loop below, since Vesh is a real known NPC/
    # companion name -- same root cause and same "checked before the
    # loop" fix pattern as the scroll->cast_spell check just above,
    # but for the far more common case of using an ordinary consumable
    # (not a scroll) ON a named ally.
    #
    # Deliberately ONLY the narrow "on"-based signal here, NOT the
    # broader drink/quaff/"use (the|my|a|an) ..." checks that live at
    # this same function's later, general-purpose use_item block --
    # first attempt at this fix duplicated the whole combined
    # condition up here and it immediately shadowed "I use my breath
    # weapon" (breath_weapon) and "use my own dice"
    # (toggle_manual_dice), both of which are checked further down,
    # AFTER this point, and both of which contain "use my" but never
    # "on". The "on" requirement alone is exactly what the real
    # reported bug needs (a real recipient always follows "on") and is
    # narrow enough not to collide with those. Real regression caught
    # by this fix's own test suite while writing it -- explicitly also
    # excludes environment-use phrasing ("use the environment against
    # it") for the same reason, since that check also lives later in
    # this function.
    _ENVIRONMENT_USE_PHRASES = ("use the environment", "use my surroundings", "use the surroundings",
                                 "interact with the environment", "use the room against", "environment attack",
                                 "use the area against")
    if not any(w in lowered for w in _ENVIRONMENT_USE_PHRASES) and re.search(r"\buse\s+\S.*\bon\b", lowered):
        return {**base, "action": "use_item"}

    # Battle formations (2026-08-01, per Coffee: "character placement
    # has an effect in battle" + "allow us to customize the
    # formations"). Checked as an explicit move-to-row phrasing first
    # (name extracted between "move "/"put " and "to the front/back"),
    # then a same-clause "X to the front/back" fallback, then a bare
    # "front row"/"back row" mention defaulting to self -- same layered
    # pattern as the invite-to-party trigger above.
    #
    # Moved here, before the known-NPC-name loop below (2026-08-08,
    # live-caught via topic-activity monitoring: "Pull vesh back to the
    # back row" and "Move Vesh to the backrow" both came back as
    # talk_npc instead of a formation change). Root cause: this whole
    # block used to live AFTER the npc_name loop, so ANY formation
    # command that names a real companion -- which is nearly always,
    # since you have to say who to move -- got intercepted as talk_npc
    # first and never reached this logic at all. Same "checked before
    # the loop" fix shape already used for the scroll/use_item checks
    # just above.
    for trigger in ["move ", "put "]:
        if trigger in lowered:
            for row, cuts in (
                ("back", (" to the back row", " to the back", " in the back row", " in the back", " behind")),
                ("front", (" to the front row", " to the front", " in the front row", " in the front", " up front")),
            ):
                for cut in cuts:
                    if cut in lowered:
                        name = text[lowered.index(trigger) + len(trigger):lowered.index(cut)].strip()
                        action = "set_back_row" if row == "back" else "set_front_row"
                        return {**base, "action": action, "target": name or None}
    # Real live bug (2026-08-11, topic-monitor report): "Move laurienna
    # back" and "Move Zara and Sarah up" both came back as "move"/
    # "talk_npc" instead of a formation change -- right after Coffee's
    # own "Move Sarah to front row" correctly worked. Root cause: the
    # name-extraction loop above only recognizes "to the back"/"in the
    # front"/etc. phrasing, never a bare trailing "back"/"up" the way
    # "pull X back" already does (see _PULL_BACK_TRAILERS below, added
    # 2026-08-08 for exactly this same shape of bug) -- so a perfectly
    # natural "move NAME back"/"move NAME up" fell through past the
    # keyword fallback entirely and hit the model, which (per its
    # well-documented bias when a known companion name is present)
    # guessed talk_npc instead. Deliberately does NOT try to handle an
    # "and"-joined multi-name target ("Zara and Sarah") -- the target
    # field is a single name, and guessing a combined "Zara and Sarah"
    # string would just fail character lookup downstream instead of
    # fixing anything, so that case still falls through unchanged.
    #
    # "forward" (2026-08-11, same-day topic-monitor report): "Move zara
    # forward" came back as plain "move" (travel) right next to a
    # correctly-classified "Move elduinn to the backrow" -- the exact
    # same bare-trailer gap as "back"/"up" above, just a third natural
    # synonym ("forward" == "to the front") that was never added.
    _MOVE_BARE_TRAILERS = {"back": "set_back_row", "up": "set_front_row", "forward": "set_front_row"}
    for trigger in ["move ", "put "]:
        if trigger not in lowered:
            continue
        stripped = lowered.rstrip().rstrip(".!")
        for bare_word, action in _MOVE_BARE_TRAILERS.items():
            if stripped.endswith(" " + bare_word):
                start = lowered.index(trigger) + len(trigger)
                end = len(stripped) - len(bare_word)
                name = text[start:end].strip()
                if name and len(name.split()) <= 3 and not any(c in name for c in ",.") and " and " not in name.lower():
                    return {**base, "action": action, "target": name}
                break
    for trigger in ("takes point", "take point"):
        if trigger in lowered:
            name = text[:lowered.index(trigger)].strip()
            for cut in (" takes", " take"):
                if name.lower().endswith(cut):
                    name = name[:-len(cut)].strip()
            return {**base, "action": "set_front_row", "target": name or None}
    # Real-time tactical phrasing (2026-08-01, per Coffee: "research
    # phrases and words used to cover, defend, pull back, move forward
    # and other things used for formations" -- meant to work instantly
    # mid-fight in the heat of the moment, not just the more deliberate
    # "move X to the row" phrasing above). Deliberately conservative:
    # only phrases specific enough to a real battlefield context to be
    # safe as an unconditional keyword-fallback match (this fallback
    # WINS outright over the model whenever it returns non-"chat", so a
    # false positive here would misclassify real narrative text, e.g.
    # "we need to protect the village" -- that's why looser words like
    # bare "defend"/"protect"/"advance" are deliberately left out).
    # "pull " requires a trailing "back", OPTIONALLY followed by a row
    # phrase (a real name can sit between "pull " and "back", e.g.
    # "pull Zara back", "pull vesh back to the back row"); everything
    # else is a fixed phrase. The trailing-row-phrase case is the real
    # live bug fixed 2026-08-08 above: "pull vesh back to the back row"
    # ends in "row", not "back", so the old bare endswith("back") check
    # missed it even once this block ran before the npc_name loop.
    _PULL_BACK_TRAILERS = ("", " to the back row", " to the back", " to the front row", " to the front")
    if "pull " in lowered:
        stripped = lowered.rstrip().rstrip(".!")
        for trailer in _PULL_BACK_TRAILERS:
            suffix = "back" + trailer
            if stripped.endswith(suffix):
                start = lowered.index("pull ") + len("pull ")
                end = len(stripped) - len(suffix)
                name = text[start:end].strip()
                # Guards against an unrelated sentence that happens to end
                # in "back"/a row phrase after an earlier "pull " (e.g.
                # "pull the lever, then head back") -- a real name is
                # short and has no punctuation.
                if len(name.split()) <= 3 and "," not in name and "." not in name:
                    action = "set_front_row" if "front" in trailer else "set_back_row"
                    return {**base, "action": action, "target": name or None}
                break

    # Bare mention fallbacks (no name -- assumed to mean the speaker's
    # own character), checked AFTER the name-extracting blocks above so
    # a real "pull vesh back to the back row"/"move vesh to the
    # backrow"-style command with a real name is never shadowed by its
    # own substring ("back row" IS a substring of "...to the back
    # row") -- real regression caught while writing this fix's own test
    # suite: this used to sit before the pull-back block and won first.
    if any(w in lowered for w in ["front row", "up front", "take the front"]):
        return {**base, "action": "set_front_row", "target": None}
    if any(w in lowered for w in ["back row", "hang back", "stay behind", "stays behind"]):
        return {**base, "action": "set_back_row", "target": None}
    if any(w in lowered for w in ["fall back", "cover me"]):
        return {**base, "action": "set_back_row", "target": None}
    if any(w in lowered for w in ["push up", "move up", "hold the line", "hold the front"]):
        return {**base, "action": "set_front_row", "target": None}

    # Throw (2026-08-08, per Coffee: throw any carried weapon, not the
    # one equipped, at an enemy). Checked before the npc_name loop for
    # the same reason every other named-target combat action is --
    # "throw the dagger at Grask" would otherwise get swallowed as
    # talk_npc if "Grask" happens to also be a known companion name.
    if lowered.startswith("throw ") or " throw " in lowered or lowered.startswith("i throw"):
        return {**base, "action": "throw_weapon"}

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

    # Real live bug (2026-07-23, Coffee: "Run from battle" got classified
    # as bare chat instead of fleeing -- only "run away" was covered,
    # missing the equally natural "run from X").
    flee_words = ["flee", "run away", "run from", "try to run", "try to escape", "escape the fight",
                  "retreat", "get out of here", "get me out", "make a break for it"]
    if any(w in lowered for w in flee_words):
        return {**base, "action": "flee"}

    # Real party invite/accept/leave — checked here, before any known NPC
    # name would have already been caught above (recruit_npc's "join the
    # party" phrasing is for campaign NPCs specifically; this is for
    # inviting a fellow player's or AI companion's own character).
    for trigger in ["invite ", "let "]:
        if trigger in lowered and ("to my party" in lowered or "to the party" in lowered
                                    or "join my party" in lowered or "join the party" in lowered
                                    or "ti my party" in lowered or "ti the party" in lowered):
            name = text[lowered.index(trigger) + len(trigger):].strip()
            for cut in (" to my party", " to the party", " join my party", " join the party",
                        " ti my party", " ti the party"):
                if cut in name.lower():
                    name = name[:name.lower().index(cut)].strip()
                    break
            return {**base, "action": "invite_to_party", "target": name or None}

    if any(w in lowered for w in ["accept the party invite", "accept the invite", "i'll join the party",
                                    "ill join the party", "i accept the party"]):
        return {**base, "action": "accept_party_invite"}

    if any(w in lowered for w in ["leave the party", "leave my party", "quit the party", "i quit my party"]):
        return {**base, "action": "leave_party"}

    # leave_guild (2026-08-13, per Coffee: "add a leave guild feature").
    # Checked here, BEFORE move_words' own "leave the "/"leave this"/
    # "leave here" phrases further down would otherwise claim "leave the
    # Silver Wardens" as ordinary travel first. Grounded the same way
    # join_guild is (the literal word "guild" or a real guild's own
    # name/id must be present) so this can never fire on an unrelated
    # "leave the tavern"/"leave this room". "quit" alone is deliberately
    # not enough on its own without that same real-guild grounding --
    # too easy to collide with "quit fighting"/"I quit" otherwise.
    if re.search(r"\b(?:leave|quit|resign from|resign my)\b", lowered) and (
        "guild" in lowered or any(gid.replace("_", " ") in lowered or g["name"].lower() in lowered for gid, g in GUILDS.items())
    ):
        return {**base, "action": "leave_guild"}

    # The Remnants (2026-08-13, per Coffee -- see remnants.py's own
    # module docstring). "summon"/"call forth" naming a real bound
    # Remnant by name is checked BEFORE the generic "summon"/cast_spell
    # collision further down (this game's real "Summon Lesser Spirit"
    # spell also uses the word "summon") -- grounded in a real
    # Remnant's own name/id being present, the same "never fire on an
    # unrelated word alone" shape join_guild/leave_guild already use.
    mentions_a_real_remnant = any(
        r["name"].lower() in lowered or rid.replace("_", " ") in lowered for rid, r in REMNANTS.items()
    )
    if mentions_a_real_remnant and re.search(r"\b(?:summon|call forth|invoke)\b", lowered):
        return {**base, "action": "summon_remnant"}
    if re.search(r"\bassign\b.+\bsummoner\b", lowered) or re.search(r"\bmake\b.+\bsummoner\b", lowered):
        return {**base, "action": "assign_summoner"}

    # Bench/un-bench (2026-07-31, per Coffee: battle-planning roster
    # picker) -- checked before "unbench" would ever risk matching a
    # bare "bench" substring check, since "un" isn't a substring of
    # "bench" this ordering doesn't actually matter, but keeping
    # unbench first reads clearer next to its own trigger list.
    for trigger in ["unbench ", "un-bench ", "bring back ", "add back "]:
        if trigger in lowered:
            name = text[lowered.index(trigger) + len(trigger):].strip()
            return {**base, "action": "unbench_party_member", "target": name or None}
    if "bring" in lowered and "back" in lowered and lowered.index("bring") < lowered.index("back"):
        name = text[lowered.index("bring") + len("bring"):lowered.index("back")].strip()
        return {**base, "action": "unbench_party_member", "target": name or None}
    for trigger in ["bench "]:
        if trigger in lowered:
            name = text[lowered.index(trigger) + len(trigger):].strip()
            for cut in (" from the fight", " from this fight", " for this one", " this one"):
                if cut in name.lower():
                    name = name[:name.lower().index(cut)].strip()
                    break
            return {**base, "action": "bench_party_member", "target": name or None}

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

    # Real design fix (2026-07-27, per Coffee: "use the environment" is
    # too vague -- players should be able to act on the actual named
    # scenery/object in the room directly, the same natural way they'd
    # attack a monster ("attack the rock cliff", "I hit the tunnel
    # supports"), not a fixed meta-phrase. Checked BEFORE the generic
    # attack_words match just below (which would otherwise misclassify
    # this as an ordinary "attack" aimed at a nonexistent monster target)
    # and BEFORE use_item's "use (the|my|a|an)" regex further down.
    # Grounded in the CURRENT location's own real combat_environment
    # name (passed in by bot.py, never invented here) -- a stopword-
    # filtered word overlap with an interaction-style verb, so "I attack
    # the sagging tunnel supports", "bring down the tunnel supports", and
    # "I hit the cliff" all resolve the same real way. The generic
    # environment_words phrases below still work too, for a player who
    # doesn't remember/use the object's exact name.
    if environment_name:
        _env_stopwords = {"the", "a", "an", "of", "at", "in", "on", "overhead", "nearby", "loose", "hanging"}
        env_words = [w for w in re.findall(r"[a-z']+", environment_name.lower())
                     if w not in _env_stopwords and len(w) > 2]
        interact_verbs = ["attack", "hit", "strike", "smash", "throw", "pull", "kick", "bring down",
                           "topple", "collapse", "knock", "shoot", "cut", "break", "drop", "shove", "use",
                           "target", "trigger", "bring the"]
        if env_words and any(w in lowered for w in env_words) and any(v in lowered for v in interact_verbs):
            return {**base, "action": "use_environment"}

    # Real live bug, confirmed twice (2026-08-06, dev-topic screenshots):
    # "Attack spider 2 with fire ball" and "Attack spider 3 with burning
    # hands" both contain the bare word "attack", so the generic
    # attack_words match just below claimed them BEFORE anything ever
    # looked at what came after "with" -- the result was a mundane
    # weapon swing that silently discarded the named spell entirely,
    # never consuming a spell slot or dealing fire damage. Sheri's own
    # dev-topic report ("Was supposed to be skill fire ball") is exactly
    # this. Checked here, before the generic attack match, the same way
    # the environment-object check just above already overrides it for a
    # more specific case. Grounded only in spells.py's own real spell
    # names (never invented) -- compares with spaces stripped on both
    # sides so "fire ball" still matches the real spell "Fireball" (one
    # word), the longest real name is preferred so no shorter spell name
    # can accidentally match inside a longer one.
    # Real regression (2026-08-08, caught by the regression suite):
    # "imbue the shield with warding" got misclassified as casting the
    # real spell "Shield" instead of enchant_item, because "shield" is
    # a literal substring of "...theshieldwith..." once spaces are
    # stripped below -- this spell-match check runs BEFORE the
    # forge/enchant/imbue check further down this function, so it wins
    # even though the player is naming an ITEM to enchant, not casting
    # anything. Any real spell name that also happens to be a plausible
    # item name (Shield, Light, ...) combined with "with" in an
    # enchant/imbue/forge sentence would hit this same collision.
    # Guarded out the same forge/enchant/imbue phrasing this function
    # already checks for later, so that check still wins when it should.
    forge_enchant_words = ["forge my", "forge the", "i forge", "enchant my", "enchant the",
                            "imbue my", "imbue the", "i enchant", "i imbue"]
    if (("with" in lowered or "using" in lowered)
            and not any(w in lowered for w in forge_enchant_words)):
        squashed = lowered.replace(" ", "")
        spell_match = None
        for spell in sorted(spells_module.SPELLS.values(), key=lambda s: -len(s["name"])):
            if spell["name"].lower().replace(" ", "") in squashed:
                spell_match = spell["name"]
                break
        if spell_match:
            return {**base, "action": "cast_spell", "spell_name": spell_match}

    # Real live bug, confirmed AGAIN (2026-08-07, topic-activity log:
    # "Attack spider 1 with dragon breath"): the original "dragon
    # breath" fix below (2026-07-22, Charvenna/Sugar) has always been
    # unreachable whenever the message ALSO contains a bare attack word
    # ("attack"/"hit"/"swing"/etc.), since the generic attack_words match
    # just below runs first and returns immediately -- it only ever
    # helped a phrasing with no attack word at all ("use dragon breath on
    # X"), never the arguably more natural "attack X with dragon breath"
    # shape. Same root cause and same fix shape as the spell-name check
    # just above: moved ahead of the generic attack match instead of
    # after it. Kept as its own explicit phrase list (not folded into
    # the spell check above) since a Dragonborn's breath weapon is a
    # racial trait, not a spell in spells.py.
    if any(w in lowered for w in ["breath weapon", "breathe fire", "unleash my breath", "use my breath",
                                    "dragon breath", "dragon's breath", "dragons breath"]):
        return {**base, "action": "breath_weapon"}

    # Real live bug (2026-08-07, topic-activity log: Coffee typed a bare
    # "Fight" and got silent "chat" -- COMBAT_START_WORDS only matches
    # "fight the"/"fight some"/"let's fight" etc, never a standalone
    # "Fight", and attack_words (checked just below) never included
    # "fight" at all. Checked here as a whole WORD, not folded into
    # attack_words' plain substring check, because "fight" is a
    # substring of "Fighter" -- one of this game's own 12 real class
    # names -- a substring check would misclassify any ordinary mention
    # of the class ("I made a Fighter", "switch to my Fighter") as an
    # attack.
    # Real live bug (2026-08-10, topic-activity log): "we dont have to
    # fight" (part of a compound message also addressing an NPC by
    # name, "Wait kess, we dont have to fight") matched the bare
    # "fight" word rule below and returned 'attack' -- exactly
    # backwards, this is a plea to AVOID combat, not start it.
    # conditional_words above only guards hypothetical/conditional
    # phrasing ("if X"), never negation. A small, explicit list of
    # negated-fight phrasings (deliberately NOT a blanket "not" strip,
    # which would wrongly exclude a real attack like "fight the
    # goblin, not the spider") skips this match so it falls through
    # to whatever this actually is instead (talk_npc/chat), same
    # "skip rather than guess wrong" shape as conditional_words itself.
    negated_fight_phrases = [
        "don't have to fight", "dont have to fight", "don't need to fight", "dont need to fight",
        "no need to fight", "not going to fight", "won't fight", "wont fight", "refuse to fight",
        "don't want to fight", "dont want to fight", "we shouldn't fight", "we shouldnt fight",
    ]
    fight_words = set(re.findall(r"[a-z']+", lowered))
    if ("fight" in fight_words and not any(c in lowered for c in conditional_words)
            and not any(p in lowered for p in negated_fight_phrases)):
        return {**base, "action": "attack"}

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

    # Real live bug (2026-08-10, found via topic-activity monitoring):
    # "Look at the shard of dim light in my inventory" matched "my
    # inventory" below and got misclassified as check_inventory, whose
    # handler always dumps the WHOLE backpack list, ignoring any item
    # name in the message -- confirmed by the very next message from
    # the same player, "Examine the shard of dim light" (same item, no
    # "in my inventory" suffix), which correctly returned 'examine'.
    # Naming a specific item "in my inventory"/"in my backpack"/"in my
    # bag" is asking to inspect THAT item, not list everything carried.
    # Checked BEFORE check_inventory below so it can't be shadowed,
    # same "check the narrower phrasing first" shape as the "what items
    # do you have for sale" guard above.
    examine_named_item_in_inventory = re.search(
        r"\b(?:look at|examine|inspect|check(?: out)?)\s+(?:the |a |an )?(.+?)\s+in my (?:inventory|backpack|bag)\b",
        lowered,
    )
    if examine_named_item_in_inventory:
        target = text[examine_named_item_in_inventory.start(1):examine_named_item_in_inventory.end(1)].strip()
        if target:
            return {**base, "action": "examine", "target": target or None}

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

    # Real live bug (2026-08-09, found via topic-activity monitoring):
    # "Go to the market row" came back check_market instead of move --
    # "market row" is a REAL location (campaigns/default/campaign.json's
    # "market_row"), and its full display name "Market Row" contains
    # "market" as a substring of "the market", so the general
    # marketplace check below swallowed a perfectly explicit "go to <a
    # real place>" travel command before move_words ever got a chance
    # (confirmed live: "go to market row", no "the", already correctly
    # returned move -- only the "the market row" phrasing tripped this).
    # Excluded here rather than reordered, so every other genuine
    # marketplace phrase (check/cancel) below is completely unaffected.
    #
    # Real live bug, same root cause (2026-08-13, found via topic-activity
    # monitoring): "Go to the market" (no "row" at all) hit this exact
    # same collision -- Market Row is the only real market-flavored
    # location in this game, and _do_move's own word-level fallback
    # (bot.py) already resolves the bare word "market" to it uniquely, so
    # this phrasing was always a genuine travel command, never a request
    # to see marketplace listings. The "market row" substring exclusion
    # above doesn't help here since the player never said "row" at all.
    # Generalized: any explicit travel verb immediately preceding
    # "market" (with an optional "the"/"a" and, e.g., "back", in between)
    # is excluded from the marketplace check the same way "market row"
    # already is, so move_words gets its turn instead.
    _market_travel_phrase = re.search(
        r"\b(?:go|goes|going|head|heads|heading|walk|walks|walking|"
        r"travel|travels|traveling|move|moves|moving|return|returns|returning|"
        r"back)\s+(?:back\s+)?to\s+(?:the\s+|a\s+)?market\b", lowered,
    )
    if "market row" not in lowered and not _market_travel_phrase and any(
        w in lowered for w in ["the market", "marketplace", "market listings"]
    ):
        # Real live gap (2026-08-03, Coffee): "Cancel my listing in the
        # market" got swallowed by the plain "the market" check below
        # and showed him the market instead of cancelling anything --
        # this needs to be checked FIRST, same "specific case before the
        # general one" shape as the "duel"/"accept" check above.
        if any(w in lowered for w in ["cancel", "remove", "take back", "pull back", "unlist", "un-list", "delist"]):
            return {**base, "action": "cancel_market"}
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

    # Auto Level-Up Party (2026-07-25, per Coffee): checked BEFORE the
    # generic "level up" below, since a party-wide phrasing like "auto
    # level up the party" contains the substring "level up" too and
    # would otherwise be misread as the single-character action.
    if "party" in lowered and (
        "level up" in lowered or "level-up" in lowered
        or ("skill point" in lowered and "assign" in lowered)
        or ("skill point" in lowered and "auto" in lowered)
    ):
        return {**base, "action": "auto_level_up_party"}

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

    # Real live bug (2026-08-13, found via topic-activity monitoring
    # while investigating "go to the market"): none of these travel
    # verbs had a progressive-tense ("-ing") form -- "heading to the
    # market" matched neither this list ("head to" is not a substring
    # of "heading to", the "ing" breaks it) nor the market-travel-phrase
    # exclusion just above this function's marketplace check, and fell
    # all the way through to the silent "chat" default. Added the
    # natural "-ing" form of each existing verb here; every other entry
    # (enter/descend/leave/etc., already covering their own forms or not
    # naturally used in progressive tense for this purpose) is untouched.
    move_words = ["go to", "goto", "head to", "heading to", "walk to", "walking to",
                  "travel to", "traveling to", "travelling to", "move to", "moving to",
                  "enter the", "descend", "ascend",
                  "climb down", "climb up", "leave the ", "leave this", "leave here", "go back",
                  "go downstairs", "go upstairs", "head downstairs", "head upstairs",
                  "downstairs", "upstairs", "exit this", "exit the", "step out", "walk out",
                  "return to", "returning to", "head back to", "heading back to", "back to the"]
    if any(w in lowered for w in move_words):
        return {**base, "action": "move"}

    # Real live bug (2026-08-10, found via topic-activity monitoring):
    # "Travel west" got silently misclassified as chat -- move_words
    # above only recognizes movement verbs paired with a NAMED
    # destination ("travel to X"), never a bare compass direction with
    # no place name. _do_move (bot.py) already fully supports this --
    # it re-derives the destination straight from the raw text via the
    # current location's real "directions" map (compass word -> real
    # location id, campaign.json) -- the classifier just never sent
    # compass-direction phrasing there in the first place. Deliberately
    # a movement VERB + direction word, not the bare word alone
    # ("west" by itself is too likely to be part of something else,
    # e.g. a location/NPC name containing it).
    if re.search(r"\b(?:travel|go|goto|head|walk|move|ride|run|march|sail)\s+"
                 r"(?:north|south|east|west|northeast|northwest|southeast|southwest)\b", lowered):
        return {**base, "action": "move"}

    if any(w in lowered for w in ["look around", "where am i", "look at my surroundings", "examine the area",
                                    "describe this place", "what's around me", "whats around me",
                                    "survey the area", "look like", "what is this place", "observe the"]):
        return {**base, "action": "look"}

    # A specific object/detail, not the whole area (that's "look" above,
    # already checked first so "examine the area" can't be shadowed).
    # Target text is matched against the current location's real
    # interactables list in bot.py — never invented, same as items.
    # "look under" added 2026-08-06 (real player, caught via topic-
    # activity monitoring): "Look under the hollow stump shrine" fell
    # through this list (only "look at"/"look closer at" were covered,
    # not "look under") to the low-confidence 'chat' default, and the
    # model call that followed picked 'look' (whole-area) over
    # 'examine' (the specific object actually named) -- the exact same
    # non-silent-but-wrong misclassification shape as the "touch" gap
    # documented below.
    for trigger in ["examine the ", "examine ", "look at the ", "look closer at ", "look under the ", "look under ",
                     "inspect the ", "inspect ", "check out the ", "search the ", "look at "]:
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
    #
    # "gaze(d)? at" added 2026-08-07 (real player, caught via
    # topic-activity monitoring): "Gaze at the pool of water" fell
    # through to the low-confidence 'chat' default (no article-optional
    # trigger above covers "gaze"), and the model call that followed
    # picked 'look' (whole-area) over 'examine' (the specific object
    # actually named) -- same non-silent-but-wrong shape as the
    # touch/peer/glance gaps already fixed here, just one more common
    # synonym for "look at" a specific thing.
    #
    # "view(ed/ing)?" added 2026-08-12 (real player, caught via
    # topic-activity monitoring): "View the herbalism guide" -- a real
    # owned recipe-book item, see _do_examine's own inventory-item
    # branch -- fell all the way through to the fully silent 'chat'
    # default (no reply at all), the exact same "verb not covered" gap
    # this file has hit many times before (touch/peer/read/observe/
    # gaze), just one more common synonym for "look at" a specific
    # thing. No "at" required, same shape as read/observed/touch(ed)?.
    examine_verb_match = re.search(
        r"\b(?:read|observed|examined|inspected|searched|checked out|touch(?:ed)?|view(?:ed|ing)?|"
        r"looked (?:at|closer at)|(?:peer|perr)(?:ed)? (?:at|into|in)|glanced? at|gaze(?:d)? at)\b\s+"
        r"(?:the |a |an )?(.+)",
        lowered,
    )
    if examine_verb_match:
        target = text[examine_verb_match.start(1):examine_verb_match.end(1)].strip()
        return {**base, "action": "examine", "target": target or None}

    # "feel(s/ing)?/felt" added 2026-08-06 (real player, caught via
    # topic-activity monitoring): "Feel the pulse on the weathered
    # waystone" fell all the way through to the silent 'chat' default --
    # same shape as the "touch" gap above, just a different synonym for
    # the same physical-interaction verb, and this one IS fully silent
    # (no reply at all) rather than a wrong-but-present one. Kept as a
    # SEPARATE check with a REQUIRED article (unlike touch/read/etc.
    # above, whose article is optional) specifically because "feel" is
    # overwhelmingly used as an EMOTION verb in ordinary English ("I'm
    # feeling great today") with no article at all -- confirmed live via
    # direct testing that folding it into the shared optional-article
    # regex above misfired on exactly that phrase. Requiring "the/a/an"
    # right after the verb blocks the emotion-verb case while still
    # matching the real physical-interaction phrasing this was added for.
    feel_verb_match = re.search(r"\b(?:feel(?:s|ing)?|felt)\b\s+(?:the |a |an )(.+)", lowered)
    if feel_verb_match:
        target = text[feel_verb_match.start(1):feel_verb_match.end(1)].strip()
        return {**base, "action": "examine", "target": target or None}

    # Live-caught (2026-07-19, Sugar): "Try opening the barrel with a
    # chalk symbol" fell all the way to 'chat' -- neither "open" nor any
    # of its verb forms appeared in either examine trigger list above,
    # the exact same "verb not covered" gap this file has hit many
    # times before (touch/peer/read/observe, all added the same way).
    # Deliberately excludes "force open"/"break down"/"break open"/
    # "smash" -- those already route to a real strength skill check
    # further below (the object resists and needs to be forced), a
    # genuinely different intent from just looking inside/at something
    # unobstructed. "break open" added 2026-08-10 (found via topic-
    # activity monitoring): "Break open the barrel with a chalk symbol
    # on it" slipped through this exclusion (only "break down" was
    # listed, not "break open") and got a passive "examine" instead of
    # the real strength check it should trigger.
    if not any(w in lowered for w in ["force open", "break down", "break open", "smash"]):
        open_match = re.search(r"\bopen(?:ing|ed)?\b\s+(?:the |a |an )?(.+)", lowered)
        if open_match:
            target = text[open_match.start(1):open_match.end(1)].strip()
            return {**base, "action": "examine", "target": target or None}

    if any(w in lowered for w in ["steal", "pickpocket", "rob the", "rob this", "swipe the", "take without paying"]):
        return {**base, "action": "steal"}

    # The scroll->cast_spell check itself now lives further up in this
    # function, before the known-NPC-name loop (see 2026-08-02 comment
    # there) -- moved out of here since this position ran AFTER that
    # loop, which let a message naming a known NPC/companion ("...on
    # Wren") slip past it entirely.

    # Checked BEFORE equip_item just below -- "take off"/"unequip"/
    # "remove my" are the real inverse of "put on"/"wear", and share
    # enough surface words (both can mention a ring/amulet by name) that
    # the more specific removal phrasing needs to win first (magic item
    # system Phase 5, 2026-08-02: a real, previously entirely missing
    # unequip feature).
    if any(w in lowered for w in ["take off", "unequip", "remove my", "i remove"]):
        return {**base, "action": "unequip_item"}

    if any(w in lowered for w in ["equip ", "wield ", "wear ", "put on the", "put on my",
                                    "i equip", "i wield"]):
        return {**base, "action": "equip_item"}

    # Magic item system Phase 7 (2026-08-02): forge/enchant/imbue -- real
    # generated-item mutation, checked before nothing else conflicts
    # (neither word appears in any earlier trigger here). "enchant"/
    # "imbue" also appear in the SEPARATE enchanters_guild dialogue
    # check in bot.py's guild_topic_handler, but that only fires inside
    # that guild's own dedicated Telegram topic, never through this
    # general classifier -- no collision.
    if any(w in lowered for w in ["forge my", "forge the", "i forge"]):
        return {**base, "action": "forge_item"}
    if any(w in lowered for w in ["enchant my", "enchant the", "imbue my", "imbue the", "i enchant", "i imbue"]):
        return {**base, "action": "enchant_item"}

    # Mastery-grind discard (2026-08-11, per Coffee: RNG crafting quality
    # means a real risk of a weak roll -- "having to discard bad ones"
    # needs a real action to do that with, checked right alongside
    # forge/enchant since it shares the same "my inventory item" phrasing.
    if any(w in lowered for w in ["discard my", "discard the", "scrap my", "scrap the", "i discard", "i scrap"]):
        return {**base, "action": "discard_item"}

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
    # Real live bug (2026-07-24, Coffee: "Use the scroll of revivify on
    # Laurrienna" got misread as a plain "look around"): these all
    # required a leading "i " ("i use my"/"i use the"/"i use a"), so any
    # phrasing WITHOUT that pronoun ("Use the scroll of...", "Use my
    # potion on...") fell all the way through to the real model instead
    # of the deterministic fast path -- and the model got this one
    # wrong. Same word-boundary "use (the|my|a|an)" now matches
    # regardless of a leading pronoun; \b keeps it from false-positiving
    # on words that merely CONTAIN "use" ("because the", "used the").
    # Interactive combat environments (2026-07-27, per Coffee: "I want
    # interactive environments" in battles) -- checked BEFORE use_item's
    # own "use (the|my|a|an)" pattern just below, which would otherwise
    # always win first ("I use the environment" contains "use the").
    # Deliberately generic (not tied to any one room's own named
    # feature) -- _do_use_environment itself checks whether the current
    # location actually has a usable one.
    environment_words = ["use the environment", "use my surroundings", "use the surroundings",
                          "interact with the environment", "use the room against", "environment attack",
                          "use the area against"]
    if any(w in lowered for w in environment_words):
        return {**base, "action": "use_environment"}

    # Real live bug (2026-08-08): "use health potion on pan" (an
    # article-less "use X on Y" with no known NPC name in it) still
    # needs to reach here as a fallback -- the earlier, loop-order-
    # sensitive copy of this same "on"-based check (see the
    # known-NPC-name loop above, right after the scroll->cast_spell
    # check) only fires while iterating a known name, so a target
    # that ISN'T a registered NPC/companion name (a fellow real
    # player's own character, for instance) still needs this bare
    # fallback copy to ever match at all.
    if any(w in lowered for w in ["drink ", "quaff"]) \
            or re.search(r"\buse (the|my|a|an)\b", lowered) \
            or re.search(r"\buse\s+\S.*\bon\b", lowered) \
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

    # Real live bug (2026-08-09, found via topic-activity monitoring):
    # "Take a key from the locksmith window" fell all the way through
    # to the silent 'chat' default (no reply at all) -- this game has
    # no generic "take/pick up an item from the environment" mechanic,
    # only real, campaign-defined interactables you can examine (bot.py's
    # _find_interactable already correctly matches this exact phrase to
    # "rows of hanging keys in the locksmith's window", confirmed live --
    # the ONLY gap was classification, not the lookup itself). Same
    # "verb not covered, falls through to examine" shape as touch/gaze/
    # feel/read/open above, just for "take"/"grab"/"pick up" -- but
    # deliberately narrower (requires a "from" clause) than those, since
    # a bare "take/grab the X" with no "from" is far more often really
    # about a carried, already-owned item ("take the healing potion" =
    # drink it, i.e. use_item) than a location prop; this fallback
    # already returns 'chat' (no opinion) for that bare phrasing, which
    # lets the real model's own -- typically correct -- use_item read
    # through untouched (see the trust-priority rule below: only a
    # non-chat fallback opinion ever overrides the model).
    take_from_match = re.search(
        r"\b(?:take|took|grab|grabbed|pick up|picked up)\b\s+(?:the |a |an )?(.+\bfrom\b.+)", lowered,
    )
    if take_from_match:
        target = text[take_from_match.start(1):take_from_match.end(1)].strip()
        return {**base, "action": "examine", "target": target or None}

    # Per Coffee (2026-07-24): "a local church we can go to pray and
    # give an offering to the dead which revives the characters too" --
    # grounded in the Hollow Stump Shrine's existing "offering_line"
    # interactable, a real gold-cost alternative to Revivify that needs
    # no spell slot/scroll, just presence at the shrine. Checked after
    # rest_words so a plain "revive me" (self, no offering/shrine
    # language) keeps meaning the ordinary rest/heal action.
    #
    # Real live bug (2026-07-24, Coffee: "why won't it let me pray at
    # the shrine to bring back Laurienna"): the original trigger list
    # required an EXACT literal phrase ("pray at the shrine"), so real
    # natural variations -- "Pray to the shrine", "I pray at the hollow
    # stump shrine" (naming the shrine by its real name instead of the
    # bare word), "pray for X" -- all fell through to "chat" instead.
    # "pray" has no other meaning anywhere in this game, so matching the
    # bare word (any tense) is safe and far more forgiving.
    if re.search(r"\bpray(?:s|ed|ing)?\b", lowered) or any(
        w in lowered for w in ["give an offering", "give offering", "leave an offering",
                                "make an offering", "offering to the dead", "offer at the shrine"]
    ):
        return {**base, "action": "give_offering"}

    # drink_water (2026-07-24, per Coffee: "make spots in the game with
    # 'holy water' ... jus suggest there is water dont tell them if its
    # holy or not they have to find out"): a real, undisclosed effect
    # only some water sources in the world actually have -- bot.py's
    # _do_drink_water is the one place that decides whether THIS
    # location's water does anything, so this fast-path only ever needs
    # to recognize the plain verb, never which location is special.
    if re.search(r"\bdrink(?:s|ing)?\b", lowered) and "water" in lowered:
        return {**base, "action": "drink_water"}

    # choose_subclass (2026-07-24 pilot: Wizard's Arcane Tradition;
    # extended 2026-07-25 to every class's real subclass names, see
    # rules/leveling.CLASS_SUBCLASSES): any real school/subclass name,
    # alongside a clear pick verb, so this doesn't fire on a spell
    # description or class-features listing that merely mentions one
    # in passing.
    schools = ("evocation", "abjuration", "conjuration", "divination",
               "enchantment", "illusion", "necromancy", "transmutation")
    subclass_names = tuple(n.lower() for pair in leveling.CLASS_SUBCLASSES.values() for n in pair)
    if any(s in lowered for s in schools + subclass_names) and any(
        w in lowered for w in ["subclass", "specializ", "school of", "become a", "choose", "path of"]
    ):
        return {**base, "action": "choose_subclass"}

    # start_echo_trial (2026-07-25, Silver Wardens' Colosseum echo-
    # trials): "echo" is distinctive enough in this game's vocabulary
    # (never used for anything else) that a bare mention alongside a
    # clear challenge verb is unambiguous.
    if "echo" in lowered and any(w in lowered for w in ["challenge", "trial", "fight", "start", "begin"]):
        return {**base, "action": "start_echo_trial"}

    # check_professions (2026-07-25, per Coffee: "continue with
    # professions") -- "profession(s)" is distinctive enough in this
    # game's vocabulary to fire on its own, same convention as bare
    # "bestiary"/"leaderboard" elsewhere in this file.
    if re.search(r"\bprofessions?\b", lowered):
        return {**base, "action": "check_professions"}

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
        (["lift", "push", "break down", "break open", "force open", "shove the", "smash"], "strength"),
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

    # Bare (possibly typo'd) spell invocation (2026-08-13, real live bug
    # found via topic-activity monitoring, matches a previously-unresolved
    # note: a player in single-target combat typed just "Eldricth blast"
    # -- a typo of the real cantrip "Eldritch Blast" -- with none of the
    # trigger words the checks above require ("cast ", "with", "using"),
    # so _keyword_fallback had no opinion (fell through to "chat"), the
    # model was called, and it guessed "use_environment" instead, with
    # nothing here to catch and override that wrong guess (unlike
    # start_combat/pass_turn/flee, which already have this kind of
    # defensive override). This is exactly the "trigger message never
    # appears in logs" gap noted in an earlier investigation of this same
    # symptom -- now it does, and it's a plain typo, not a button tap.
    # Checked deliberately LAST (bare/fuzzy, so lowest-confidence of every
    # check in this function): compares the WHOLE remaining clause,
    # squashed, against every real spell's squashed name via
    # _fuzzy_match_spell_name (same difflib.SequenceMatcher pattern
    # _normalize_common_typos already uses above, and bot.py's guild-name
    # matching) -- a ratio this high over the ENTIRE clause (not a
    # substring search) is safe from false positives against an unrelated
    # full sentence, since one stray matching word can't drag a long,
    # mostly-different clause up near the same ratio a near-exact short
    # spell name gets. Splits off a trailing "at/on/against <target>"
    # first so "eldritch blast at the golem" still resolves the target.
    spell_clause, target_clause = _split_target_clause(lowered)
    matched_spell = _fuzzy_match_spell_name(spell_clause)
    if matched_spell:
        return {**base, "action": "cast_spell", "spell_name": matched_spell, "target": target_clause}

    # Party companion dialogue (2026-07-26, per Coffee: "when we say
    # 'talk' 'speak' 'say' 'tell' 'yell' 'shout' 'scream' in a location,
    # prompt dialog from the party members"). Deliberately checked LAST,
    # right before the silent "chat" default -- every other, more
    # specific action above (talk_npc for a named NPC, give_item's
    # "tell"-adjacent phrasing, skill_check's persuade/deceive verbs,
    # etc.) must win first if it already matched. Anything that reaches
    # this point containing one of these speaking verbs previously fell
    # through to "chat" (silent, no reply at all) -- now it prompts a
    # real line from whichever party companion is actually traveling
    # with the character, instead of being ignored.
    party_talk_words = ["talk", "speak", "say", "tell", "yell", "shout", "scream"]
    if any(re.search(r"\b" + w + r"\b", lowered) for w in party_talk_words):
        return {**base, "action": "talk_party"}

    return base


def parse_intent(text: str, known_npc_names: list[str] | None = None, force_model: bool = False,
                  environment_name: str | None = None) -> dict:
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
    fallback = _keyword_fallback(text, known_npc_names, environment_name=environment_name)

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

    env_hint = (
        f"\n\nInteractive scenery here: \"{environment_name}\" -- if the player's message attacks, "
        f"hits, throws at, pulls, or otherwise targets THIS object by name (not a monster), "
        f"classify as use_environment, not attack."
        if environment_name else ""
    )
    try:
        response = requests.post(
            f"{config.OLLAMA_BASE_URL}/api/generate",
            json={
                "model": config.BUILD_MODEL,
                "prompt": f"{INTENT_SYSTEM_PROMPT}\n\nKnown NPCs: {known_npc_names}{env_hint}\n\nPlayer message: {text}",
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
                "buy", "sell", "steal", "cast_spell", "join_guild", "leave_guild",
                "assign_summoner", "summon_remnant", "recruit_npc", "rest",
                "go_inactive", "skill_check", "shove", "show_map", "gather", "craft",
                "list_characters", "switch_character", "delete_character",
                "fast_travel", "accept_quest", "check_quests", "ask_clue",
                "answer_puzzle", "gamble", "chat", "examine", "flee", "resolve_choice",
                "invite_to_party", "accept_party_invite", "leave_party", "bench_party_member",
                "unbench_party_member", "set_front_row", "set_back_row", "find_merchant",
                "second_wind", "rage", "bardic_inspiration", "lay_on_hands", "arcane_recovery",
                "make_campfire", "give_item", "use_item", "equip_item", "unequip_item", "auto_equip", "breath_weapon",
                "forge_item", "enchant_item", "discard_item",
                "channel_divinity", "action_surge", "reckless_attack", "divine_smite",
                "flurry_of_blows", "wild_shape", "toggle_manual_dice", "level_up", "auto_level_up_party",
                "set_description", "set_pronouns",
                "bestiary", "list_shop", "leaderboard", "check_achievements", "set_title", "check_weather",
                "check_guild_quest", "dice_game", "fortunes_wheel", "set_alignment", "message_ai",
                "skill_tree", "challenge_duel", "accept_duel", "check_market", "cancel_market", "join_battle",
                "replay_intro", "visual_map", "rebirth", "choose_hybrid", "give_offering",
                "drink_water", "choose_subclass", "start_echo_trial", "check_professions",
                "talk_party", "use_environment", "throw_weapon",
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
            # Same defensive pattern again (2026-08-11, topic-monitor
            # report): "Take that you wretched" -- clearly aggressive
            # attack-flavor combat banter, not an attempt to retreat --
            # got classified as "flee" anyway. flee_words above is
            # already a deliberately broad, comprehensive list of every
            # real way a player asks to run away; if the keyword fallback
            # found none of them, the model's own "flee" guess is never
            # trusted alone, same reasoning as pass_turn. This matters
            # more than most other mis-guesses: flee is a real, risky
            # dice roll (per CLAUDE.md, can draw a real opportunity
            # attack) that derails whatever the player actually meant to
            # do, not just a silent non-reply.
            if parsed["action"] == "flee":
                return fallback
            # Same defensive pattern again (2026-08-11, live-confirmed by
            # Coffee): a bare emoji message ("😈", sent as banter/heckling
            # aimed at a boss, not a real combat command) got classified
            # as "attack" and actually spent a real combat turn attacking.
            # A message with no real letters at all has no verb for the
            # model to have genuinely understood -- "attack" specifically
            # is a stateful action with a real, hard-to-undo consequence
            # (a real turn, a real dice roll) when it fires wrongly, so
            # it's never trusted from the model alone for emoji/
            # punctuation-only text, same as start_combat/pass_turn above.
            # _keyword_fallback already returns "chat" for text like this
            # (no keyword can match a message with no words), so this
            # falls through to that safe, silent default instead.
            if parsed["action"] == "attack" and not re.search(r"[a-zA-Z]", text):
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


def parse_intents(text: str, known_npc_names: list[str] | None = None, force_model: bool = False,
                   environment_name: str | None = None) -> list[dict]:
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
        segment_intents = [_keyword_fallback(seg, known_npc_names, environment_name=environment_name) for seg in segments]

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
            return [parse_intent(text, known_npc_names, force_model=force_model, environment_name=environment_name)]

        real_segment_intents = [i for i in segment_intents if i["action"] != "chat"]
        if len(real_segment_intents) >= 2:
            return real_segment_intents

    return [parse_intent(text, known_npc_names, force_model=force_model, environment_name=environment_name)]
