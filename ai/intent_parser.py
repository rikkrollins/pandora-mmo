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

import class_features
import config
import items as items_module
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
    # Real topic-activity finding (2026-08-23, self-improvement monitoring
    # pass): "Initiate battle with the spiders" fell all the way through
    # _keyword_fallback's own explicit checks to its silent "chat"
    # default -- the player got zero response. Same class of explicit-
    # phrase-only fix as the rest of this list.
    "initiate battle", "initiate combat", "begin battle", "start the battle", "start battle",
    # Real dev-bridge report (2026-08-25, Coffee's party): "Start a
    # battle" (indefinite article, not "the"/no article) fell all the
    # way through to silent "chat" -- the player was trying to start
    # the fight needed to clear a requires_cleared_location gate and
    # got no response, part of a real "why can't I move on?" report.
    "start a battle",
    # Real topic-activity finding (2026-08-26, self-improvement
    # monitoring pass): "Battle a wisp" got the silent "chat" default,
    # and Coffee immediately retried as "Start a battle with a wisp"
    # (which DOES match "start a battle" above) 25 seconds later --
    # same real "why didn't that do anything?" friction as the other
    # entries in this list. "fight the"/"fight some" already cover this
    # exact shape for "fight"; "battle" was never extended the same way.
    "battle a", "battle the", "battle some",
]

INTENT_SYSTEM_PROMPT = """You are an intent classifier for a text-based 5th-edition-style tabletop RPG. \
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
- "check_affinity" is for asking how a companion feels about them, their trust/affinity level, or how to build \
trust with a companion.
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
- "grapple" is specifically for trying to grab/seize/hold/restrain an enemy so they can't get away — distinct \
from "shove" (which knocks prone, not restrains). Set "target" to the enemy, if named.
- "escape_grapple" is for a currently-grappled character trying to break free, wriggle loose, or escape a grapple.
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
- "unequip_item" is the inverse: taking off a currently-worn weapon, armor, shield, ring, amulet, or wondrous item \
(e.g. "take off my ring", "unequip the amulet", "remove my cloak", "unequip the wooden shield", "take off my armor"). \
Set "item_name" to the item.
- "auto_equip" is for asking the game to automatically equip the best weapon/armor/shield being carried, \
without naming a specific item (e.g. "auto equip my character", "put on my gear automatically", \
"help me equip my player"). Set "target" to a specific party member's name if named, else self.
- "forge_item" is for upgrading a real, previously found/crafted magic item to a higher tier at the forge \
(e.g. "forge my longsword", "forge the chain shirt"). Set "item_name" to the item.
- "enchant_item" is for adding a new magic effect to a real, previously found/crafted item -- "enchant" and \
"imbue" mean the same thing here (e.g. "enchant my longsword with flame", "imbue the shield with warding"). \
Set "item_name" to the item.
- "forge_magic_item" is DIFFERENT from "forge_item" above -- it turns an ORDINARY (not-yet-magic) weapon, \
armor, or accessory into a real magic item with a random stat bonus, at the Forge Guild (e.g. "forge my \
longsword into a magic item", "upgrade my armor into a magic item", "turn this ring into a magic item"). \
Set "item_name" to the item.
- "discard_item" is for permanently scrapping/throwing away an item from inventory, no refund \
(e.g. "discard my rusty dagger", "scrap the longsword"). Set "item_name" to the item.
- "dismantle_item" is for breaking down a weapon/armor/shield/ring/amulet the player no longer wants to \
recover real crafting materials from it (e.g. "dismantle my old sword", "salvage the chain mail"). \
Set "item_name" to the item.
- "join_guild" is for joining/asking to join a specific guild or order.
- "leave_guild" is for leaving/quitting a guild the player is already a member of.
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
- "trade_request" is for proposing a formal, two-sided trade with another real player (e.g. "trade with Sarah", \
"I want to trade with Bob") -- distinct from "give_item", which is an immediate, one-sided hand-off with no \
consent needed. Set "target" to the other player's name.
- "trade_add" is for putting an item or a gold amount into an OPEN trade's own offer (e.g. "add 3 healing potions \
to the trade", "add 50 gold to trade", "put my torch in the trade"). Set "item_name"/"quantity" for items, or leave \
"raw_text" as the full message so the amount of gold can be parsed from it.
- "trade_remove" is for taking an item or gold back OUT of an open trade's own offer before it's finalized \
(e.g. "remove the healing potion from the trade", "take 20 gold out of the trade").
- "trade_accept" is for locking in / accepting the current state of an open trade (e.g. "accept the trade", "I accept").
- "trade_cancel" is for backing out of or declining an open trade (e.g. "cancel the trade", "decline the trade", "I decline").
- "trade_status" is for asking to see the current state of an open trade again (e.g. "check my trade", "what's in the trade").
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
- "divine_sense" is specifically a Paladin's real class feature (level 1+): detecting undead nearby (this world \
has no distinct fiend/celestial creatures), usable a limited number of times per rest, works in or out of combat \
(e.g. "I use divine sense", "divine sense", "sense evil", "sense undead").
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
# "attack" added (2026-08-18, found via topic-activity monitoring):
# "atack.." during a real combat turn silently fell all the way through
# to action='chat' -- consequential in the exact same "silently drops a
# real, common action with no reply" shape as "accepet", just during
# someone's actual turn instead of a quest accept.
_TYPO_TOLERANT_WORDS = ["accept", "attack"]


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

    # Real dev-bridge feature request (2026-08-16, per Coffee: "make one
    # for 'menu' so i can say 'open the menu' 'view the menu' ... It
    # allows us to text instead of using '/' commands"). Checked FIRST,
    # same priority as the "tell X to Y" relay check just above, since
    # every one of these words would otherwise be swallowed by a more
    # generic examine/open trigger further down this function ("view the
    # menu" -> the view/examine verb match; "open the menu" -> the open
    # -> examine match) and get misread as trying to look at a physical
    # object named "menu"/"formation"/"waypoints" instead of opening the
    # real screen. Bare "menu"/"formation"/"waypoints" are distinctive
    # enough in this game's vocabulary to fire unconditionally -- none of
    # them mean anything else here (no food/dining menu, no dance-
    # formation flavor text, no other use of "waypoint" anywhere in this
    # campaign).
    # Blacksmith/Alchemy crafting menus (2026-09-11, per Coffee: "i dont
    # want to have to type 'journeyman, or Masterwork' ... how can we
    # improve the Forging system so i can open a menu"). Checked BEFORE
    # the bare "menu" catch just below -- "blacksmith menu"/"alchemy
    # menu" both contain the word "menu" and would otherwise always be
    # swallowed by that unconditional generic-menu match first. Also
    # checked before the forge_item/forge_magic_item/enchant_item
    # triggers further down this function, none of which this phrasing
    # can collide with (those all require a possessive "forge my"/
    # "forge the X"/"enchant my", never bare "forge"/"blacksmith"/
    # "alchemy" + "menu").
    # Extended (2026-09-11, per Coffee: "open it if players say
    # 'blacksmith' or 'alchemy' example, 'Open Alchemy' 'Perform
    # Alchemy' 'Do Alchemy' 'Use Alchemy' 'Look at Alchemy'") -- rather
    # than enumerate every verb phrasing individually, a bare `\balchemy\b`/
    # `\bblacksmith\b` word-boundary match (same "one distinctive noun,
    # fires unconditionally" discipline this file already uses for
    # menu/formation/waypoints/skills just below/above) already covers
    # every example given, since all of them contain the word itself.
    # "smithy"/"forging"/etc. don't contain "blacksmith" as a substring,
    # so the explicit phrase list stays alongside the regex rather than
    # being replaced by it. Deliberately NOT extended to bare "forge" --
    # that word is already meaningfully claimed by forge_item/forge_
    # magic_item ("forge my X"/"forge the X" below), so making it
    # unconditional here would swallow those real actions.
    if any(p in lowered for p in ("smithy menu", "forge menu", "forging menu", "open the forge", "visit the forge")) \
            or re.search(r"\bblacksmith\b", lowered):
        return {**base, "action": "check_blacksmith_menu"}
    if any(p in lowered for p in ("enchanting menu", "enchant menu", "potion menu", "alchemist")) \
            or re.search(r"\balchemy\b", lowered):
        return {**base, "action": "check_alchemy_menu"}
    # Cooking menu (2026-09-15, per Coffee: "include a 'Cook' sub menu
    # in the professions menu"). Deliberately phrase-matched, NOT a
    # bare "cook(ing)" word like blacksmith/alchemy get -- "cook"/
    # "cooking" are ordinary English verbs that show up in unrelated
    # roleplay/narration text far more than "blacksmith"/"alchemy"
    # ever would, so a bare match here would risk misfiring on genuine
    # conversation instead of a real menu request.
    if any(p in lowered for p in ("cooking menu", "cook menu", "the cookfire", "cookfire menu")):
        return {**base, "action": "check_cooking_menu"}

    if re.search(r"\bmenu\b", lowered):
        return {**base, "action": "check_menu"}
    if re.search(r"\bformation\b", lowered):
        return {**base, "action": "check_formation"}
    if re.search(r"\bwaypoints?\b", lowered):
        return {**base, "action": "check_waypoints"}
    # Affinity Menu (2026-08-23, per Coffee: "affinity menu so players
    # arent guessing"). Bare "affinity"/"trust" are distinctive enough
    # in this game's vocabulary to fire unconditionally, same reasoning
    # as menu/formation/waypoints just above -- no other real use of
    # either word exists anywhere in this campaign's own text.
    if re.search(r"\baffinity\b|\btrust\b", lowered):
        return {**base, "action": "check_affinity"}

    # Real dev-bridge report (2026-08-18, Coffee): "When I use open, it
    # seems to work, but when I say view, it doesn't seem to work" --
    # "View the bestiary" got "Ravenloft doesn't spot anything like
    # that here" (the generic examine fallback) instead of the real
    # Bestiary screen. Same exact shadowing bug as menu/formation/
    # waypoints above -- examine_verb_match's "view(ed/ing)?" (and
    # "read"/"observed"/"checked out"/etc.) trigger sits much earlier
    # in this function than bestiary/leaderboard/achievements' own
    # keyword checks further down, so "view the bestiary" always
    # matched the generic examine verb first. Bare "bestiary" and
    # "leaderboard" are exactly as distinctive as menu/formation/
    # waypoints (no other meaning in this game's vocabulary); "my
    # achievements"/"my map" keep the same real, already-scoped phrases
    # their own later checks already use, just moved early enough to
    # actually be reachable via "view"/"read"/"check out" phrasing too.
    if re.search(r"\bbestiary\b", lowered):
        return {**base, "action": "bestiary"}
    if re.search(r"\bleaderboard\b", lowered):
        return {**base, "action": "leaderboard"}
    # check_professions -- moved up from its old spot much further down
    # this function (2026-09-15, dev-bridge screenshot, Coffee: "I
    # should be able to view or look at or open my professions menu").
    # Real live bug: "Open my professions" got "doesn't spot anything
    # like that here" instead of the real Professions screen. Root
    # cause: the generic "open X" -> examine catch-all (much further
    # down this function) and the "view/read/checked out" examine_verb_
    # match sit far earlier in this function than the old bare-
    # "profession(s)" check did, so both "open"/"view" phrasing always
    # matched one of those generic examine triggers first -- the exact
    # same shadowing bug already fixed once for bestiary/leaderboard/
    # menu/formation/waypoints/affinity, all right above this line, for
    # the identical reason. "profession(s)" is exactly as distinctive
    # as those (no other real meaning anywhere in this game's
    # vocabulary), so it gets the same early-position fix.
    if re.search(r"\bprofessions?\b", lowered):
        return {**base, "action": "check_professions"}
    if any(w in lowered for w in ["my achievements", "my titles", "unlocked achievements"]):
        return {**base, "action": "check_achievements"}
    # Real live gap found via topic-activity monitoring (2026-08-22,
    # Coffee: "What is the story so far") -- bot._do_show_story_so_far
    # is a real, fully-implemented recap feature (real AI narration
    # grounded in completed_arcs/current_arc_pair/completed_quests,
    # menu_callback's own "story" section), but it had NO real action
    # in valid_actions at all -- reachable only by tapping the menu
    # button, never by typing it, directly contradicting this game's
    # own "everything happens in plain English" design (CLAUDE.md).
    # "story so far"/"recap" are exactly as distinctive as bestiary/
    # leaderboard just above -- no other real meaning in this game's
    # vocabulary -- so placed at this same early position for the same
    # reason (avoids the generic "view/check/read" examine-verb match
    # shadowing it further down, the same bug already fixed once for
    # bestiary/leaderboard themselves, per the comment above).
    if any(w in lowered for w in ["story so far", "recap my story", "story recap", "whats the story", "what's the story"]):
        return {**base, "action": "check_story"}
    # Real live instruction (2026-08-20, Coffee, after the visual map's
    # grid/fog-of-war rewrite): "when we say show the map, view the
    # map, or open the map, thats the map we want to see" -- ordinary
    # map phrasing now opens the real visual grid image by default,
    # not the old plain-text listing. The text version stays reachable
    # via the /map slash command (map_command -> _do_show_map directly,
    # unaffected by this classifier).
    # Real live gap (2026-09-03, Coffee, dev-bridge: "Open map" -- no
    # article -- misclassified as examine instead, then correctly
    # worked once he retyped "Open the map"). The original fix above
    # only ever matched "the map"/"my map" verbatim; a bare "map" after
    # a real map-viewing verb is just as natural a phrasing and was
    # never covered.
    if any(w in lowered for w in ["the map", "my map"]) or re.search(r"\b(?:open|show|view|check|see|display)\s+map\b", lowered):
        return {**base, "action": "visual_map"}

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
    # marketplace phrase (check/cancel/sell/buy) below is completely
    # unaffected.
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
    #
    # Moved here, ahead of the generic buy/sell/purchase checks below
    # (2026-08-19, per Coffee: "can the AI players place weapons...
    # for sale on the Market place? can they purchase from the market
    # place?"): this whole block used to live much further down this
    # function, well AFTER the generic "buy"/"sell" checks -- so "sell
    # my sword for 50 gold on the market" and "buy listing 3 from the
    # market" were always shadowed as the plain shop actions (buy/sell)
    # before ever reaching the market-specific logic, exactly the same
    # class of bug this file has hit many times before whenever a
    # specific case sits after a broader one. /sell_market and
    # /buy_market previously only ever existed as slash commands too,
    # cutting against this whole game's "no slash commands required"
    # design AND leaving AI companions -- whose own autonomous actions
    # are always natural-language sentences, see ai/autonomous_
    # player.py, never slash commands -- with no way to ever reach
    # either one, regardless of ordering.
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
        # "list " (trailing space) rather than a bare "list" so this
        # never fires on "market listings" itself.
        if any(w in lowered for w in ["sell", "list "]):
            return {**base, "action": "sell_market"}
        # Checked before the generic "buy" below (2026-08-20, per
        # Coffee: "we also need to be able to full view the items on
        # market too -- all stats, buffs, elements... requirements to
        # equip") -- the same real natural-language equivalent of the
        # market's new 🔍 View button, so an AI companion (always
        # plain-text, never a button tap) can reach the full detail
        # view too, e.g. "examine listing 3 on the market" / "look at
        # the rusty dagger on the market".
        if any(w in lowered for w in ["examine", "inspect", "look at", "view", "details", "details of"]):
            return {**base, "action": "view_market_listing"}
        if any(w in lowered for w in ["buy", "purchase"]):
            return {**base, "action": "buy_market"}
        return {**base, "action": "check_market"}

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
    # Real live report (2026-09-07, dev-bridge, Coffee, screenshot: "Open
    # my player sheet" -> "Nobody named player is playing right now").
    # Root cause: this regex's own filler-word guard already special-
    # cased "(?:character )?" as an optional literal to skip before
    # "sheet", so "my character sheet" correctly captured "my" (excluded
    # below) -- but "player" was never given the same treatment, so "my
    # PLAYER sheet" instead captured "player" as the word immediately
    # before "sheet" and misread it as a real target name to search for.
    # "player"/"player's" are exactly as generic a filler as "character"
    # here -- nobody is ever named literally "player".
    named_sheet_match = re.search(r"(\w+)(?:'s)? (?:character |player )?sheet", lowered)
    if named_sheet_match and named_sheet_match.group(1) not in (
        "my", "the", "a", "an", "her", "his", "their", "your", "our", "player", "players"
    ):
        return {**base, "action": "check_sheet", "target": named_sheet_match.group(1)}

    # Real live gap (2026-09-25/26, topic-activity signal, two real
    # players stuck on the same riddle): "Can i get a hint?" and "Can
    # we have a hint?" -- both completely natural ways to ask for
    # exactly what this block already covers -- matched none of these
    # phrases and fell all the way through to silent chat. Generalized
    # from an enumerated "can i get a hint" style list (which only
    # caught the "i" phrasing, missing "we" -- caught live a second
    # time the very next day) to a regex covering can/could x i/we x
    # get/have, rather than enumerating every combination by hand.
    if any(w in lowered for w in ["ask for a clue", "ask for clues", "give me a clue", "any clues",
                                    "what's the clue", "need a hint", "give me a hint",
                                    "ask for a hint", "what clues", "got a hint", "any hint"]) or re.search(
        r"\b(?:can|could)\s+(?:i|we)\s+(?:get|have)\s+a\s+hint\b", lowered
    ):
        return {**base, "action": "ask_clue"}

    # Real live bug (2026-08-18, dev-bridge screenshot, Coffee: "I'm not
    # getting information on how to complete this quest... whenever I
    # ask for details about the quest the game isn't telling me
    # either"). "I want more information about the Wayfarer's circuit
    # quest" -- a completely natural way to ask the exact same thing the
    # narrower "ask for a clue"/"give me a hint" phrasing above already
    # covers -- never matched any of those, so it fell through to the
    # model with no strong signal, and quest-detail questions are
    # genuinely ambiguous for it to classify. "quest" plus a real
    # info-seeking verb (more/details/info/help/how) is unambiguous
    # enough to catch unconditionally, same "distinctive enough in this
    # game's vocabulary" bar the menu/formation/waypoints block above
    # uses.
    if "quest" in lowered and re.search(
        r"\b(?:more (?:info|information|details)|details|information|info|help with|how (?:do|to)|what do i)\b",
        lowered,
    ):
        return {**base, "action": "ask_clue"}

    # Formal two-sided trade (2026-08-26, per Coffee: "have it so we can
    # say 'trade with (player)'"). Checked BEFORE give_item's own "trade "
    # trigger-verb/dative checks below -- those already treat bare
    # "trade " as a give_item verb (e.g. "trade Sarah my sword"), and
    # give_item's dative regex (`trade (\w+) (\w+) (\w+)`) would otherwise
    # match "trade with bob please" too (capturing "with" as a bogus
    # recipient name), silently stealing this phrasing before it ever
    # reached here. "trade with X" is structurally distinct enough (the
    # literal word "with" right after "trade") to intercept unconditionally,
    # first.
    trade_with_match = re.search(r"\btrade\s+with\s+", lowered)
    if trade_with_match:
        name = text[trade_with_match.end():].strip()
        for cut in (" please", " now", "?", "."):
            if name.lower().endswith(cut):
                name = name[: len(name) - len(cut)].strip()
        return {**base, "action": "trade_request", "target": name or None}

    # Adding/removing items or gold to/from an already-open trade, and
    # accepting/declining it -- same feature. Checked here (before
    # give_item) so "add the torch to the trade" never falls into
    # give_item's own broader vocabulary (it doesn't share give_item's
    # trigger verbs at all, so this is just keeping the whole trade
    # feature's checks grouped together).
    if re.search(r"\b(?:add|put|offer)\b.+\b(?:to|in)\s+(?:the\s+)?trade\b", lowered):
        return {**base, "action": "trade_add"}
    if re.search(r"\b(?:remove|take)\b.+\b(?:from|out of)\s+(?:the\s+)?trade\b", lowered):
        return {**base, "action": "trade_remove"}
    if re.search(r"\b(?:accept|confirm|lock in)\s+(?:the\s+)?trade\b", lowered) or lowered.strip() in ("accept trade", "i accept the trade", "i accept"):
        return {**base, "action": "trade_accept"}
    if re.search(r"\b(?:cancel|decline|reject|back out of)\s+(?:the\s+)?trade\b", lowered) or lowered.strip() in ("decline trade", "i decline the trade", "i decline"):
        return {**base, "action": "trade_cancel"}
    if re.search(r"\b(?:check|show|what'?s in)\s+(?:my\s+)?(?:the\s+)?trade\b", lowered):
        return {**base, "action": "trade_status"}

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

    # Real live gap (2026-08-26, topic-activity finding): "Give Pan
    # Antitoxin" fell through to a silent "chat" reply -- every dative
    # check above requires at least TWO more words after the recipient
    # specifically to avoid false-positiving on a bare-word idiom
    # ("give Pan space/trouble/credit"), but a real single-word item
    # name (Antitoxin, Torch) can never satisfy that. Confirmed live:
    # Charvenna had to retype it as "Give Pan x5 antitoxin" (now three
    # words after the recipient) to get it working at all. Rather than
    # loosening the word-count heuristic generally (which would let
    # "space"/"trouble"/"credit" back in), this verifies the single
    # trailing word is an ACTUAL known item via items.find_item_
    # mentioned_in_text against the full catalog -- a real item passes,
    # an idiom's ordinary word doesn't.
    single_word_dative_match = re.search(r"\b(?:give|hand|trade|send)\s+(\w+)\s+(\w+)\b", lowered)
    if (single_word_dative_match and single_word_dative_match.group(1) not in _GIVE_ITEM_NON_RECIPIENTS
            and items_module.find_item_mentioned_in_text(single_word_dative_match.group(2)) is not None):
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
        # Real live bug (2026-09-27, topic-activity monitoring): "Cast a
        # spell tonic on elduinn" matched the bare "cast " check above
        # and short-circuited straight to cast_spell -- but "Spell
        # Tonic" (and Greater/Supreme Spell Tonic) are real, ordinary
        # consumable ITEMS, never actual spells; their own name just
        # happens to contain the word "spell". The player had to retry
        # with "Use a spell tonic on elduinn" 14 seconds later to get
        # the right classification. A real carried-item name mentioned
        # here (excluding scrolls, which SHOULD still cast) means this
        # is really a use_item request phrased loosely with "cast" --
        # classified directly as use_item (bot._do_use_item re-parses
        # the same raw_text itself to find the item/target, so nothing
        # else needs extracting here) rather than falling through to
        # the later spell-only checks, which would otherwise land this
        # on the silent "chat" default with no real spell to match
        # either. Confirmed no real spell name collides with any real
        # item name across the whole catalog before adding this.
        _cast_line_item_id = items_module.find_item_mentioned_in_text(lowered)
        _cast_line_item = items_module.get_item(_cast_line_item_id) if _cast_line_item_id else None
        _cast_line_is_non_scroll_item = _cast_line_item is not None and _cast_line_item.get("type") != "scroll"
        if _cast_line_is_non_scroll_item:
            return {**base, "action": "use_item"}
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

    # Real live bug (2026-09-04, dev-bridge screenshot): "Use the key on
    # the door" got "Use what, exactly? Name a consumable you're
    # actually carrying." instead of actually trying the lock. Root
    # cause: the generic "use ... on ..." -> use_item check right below
    # has no awareness of lockables at all -- exactly the same gap the
    # "open the door"/"pull the lever" fixes elsewhere in this function
    # already closed for their own phrasings. A key/item used ON a real
    # lockable object must reach _do_skill_check -> _find_lockable ->
    # _do_lockpick's own requires_key_item branch (which checks the
    # player's REAL inventory by item id, not by matching this text) --
    # never the generic consumable-use guess. Checked BEFORE that block,
    # same "carve out the real skill-check phrasing first" shape; the
    # lockable-word list mirrors bot._find_lockable's own kind_words.
    if re.search(
        r"\buse\s+\S.*\bon\b.*\b(?:door|gate|lock|lever|switch|chest|crystal|torch|brazier|lantern|"
        r"plate|urn|crate|pillar|column|wall|floor)\b",
        lowered,
    ):
        return {**base, "action": "skill_check", "ability": "dexterity"}

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
    # "backrow"/"frontrow" as one word, no "the" (2026-08-20, live-caught
    # via topic-activity monitoring: "Move Ravenloft to backrow" came
    # back as plain "move" (travel) instead of a formation change).
    # Root cause: every existing cut here required either "the" ("to
    # the back row") or a two-word "back row"/"front row" -- "to
    # backrow" (no "the", one word) matched none of them. Same real gap
    # class as the "bare trailer"/"forward" fixes below, just a spacing
    # variant neither of those covers either (_MOVE_BARE_TRAILERS only
    # matches a trailing bare "back"/"up"/"forward", not "backrow").
    for trigger in ["move ", "put "]:
        if trigger in lowered:
            for row, cuts in (
                ("back", (" to the back row", " to the back", " to backrow", " in the back row", " in the back",
                           " in backrow", " behind")),
                ("front", (" to the front row", " to the front", " to frontrow", " in the front row", " in the front",
                            " in frontrow", " up front")),
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
    # "backrow"/"frontrow" bare trailers (2026-08-20, same live report
    # as the "to backrow" cut above): "move X backrow" with no "to"
    # either -- same real gap, just without the preposition this time.
    _MOVE_BARE_TRAILERS = {
        "back": "set_back_row", "up": "set_front_row", "forward": "set_front_row",
        "backrow": "set_back_row", "frontrow": "set_front_row",
    }
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

    # Real live bug (2026-08-27, dev-bridge screenshot): "Auto equip
    # Borin Ironjaw" got swallowed as talk_npc (Borin's own quest
    # dialogue came back instead of gear being equipped) because this
    # check used to live after the npc_name loop below -- same exact
    # root cause/fix shape as throw_weapon just above, just never
    # applied here despite auto_equip's own request naturally naming a
    # party member to equip.
    if any(w in lowered for w in ["auto equip", "auto-equip", "autoequip", "equip automatically",
                                    "equip me automatically", "help me equip", "gear up automatically",
                                    "put on my gear automatically", "equip my gear automatically"]):
        return {**base, "action": "auto_equip"}

    # The Remnants (2026-08-13, per Coffee -- see remnants.py's own
    # module docstring). "summon"/"call forth" naming a real bound
    # Remnant by name is checked BEFORE the generic "summon"/cast_spell
    # collision further down (this game's real "Summon Lesser Spirit"
    # spell also uses the word "summon") -- grounded in a real
    # Remnant's own name/id being present, the same "never fire on an
    # unrelated word alone" shape join_guild/leave_guild already use.
    #
    # Real live bug + real spoiler (2026-08-31, dev-bridge, Elduinn:
    # "Summon the wrathflame unbound" mid-combat got "Kess, the Unbound
    # isn't interested in talking" -- revealing Kess's own future
    # transformed name/identity to a player nowhere near that content).
    # Root cause: this whole block used to live AFTER the npc_name loop
    # below, so the single shared word "Unbound" (present in BOTH "The
    # Wrathflame Unbound", a real Remnant, and "Kess, the Unbound", a
    # real NPC) let that loop's own whole-word name matching win first,
    # misrouting a real, unambiguous "summon [Remnant]" command into
    # talk_npc against a completely unrelated, spoiler-heavy NPC. Same
    # "checked before the npc_name loop" fix shape as throw_weapon/
    # auto_equip immediately above -- an explicit "summon"/"call forth"
    # naming a real Remnant now always wins before any NPC name, shared
    # words or not, ever gets a chance to compete for it.
    mentions_a_real_remnant = any(
        r["name"].lower() in lowered or rid.replace("_", " ") in lowered for rid, r in REMNANTS.items()
    )
    if mentions_a_real_remnant and re.search(r"\b(?:summon|call forth|invoke)\b", lowered):
        return {**base, "action": "summon_remnant"}
    # Real feature request (2026-08-18, per Coffee: "make a menu for
    # 'Remnants' so players can see what the summons do... and their
    # attack or ability"). Checked AFTER summon_remnant above so an
    # actual "summon [name]" command is never swallowed by this
    # broader bare-word catch -- same ordering convention as the
    # menu/formation/waypoints block above.
    if re.search(r"\bremnants?\b", lowered):
        return {**base, "action": "check_remnants"}

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
        # Real live bug (2026-08-31, found while testing the Kess-spoiler
        # fix above): plain .split() keeps trailing punctuation attached
        # ("Kess, the Unbound".split() -> ["kess,", "the", "unbound"]),
        # so the whole-word regex for "kess," (with a literal comma) never
        # matches bare "kess" in a player's message. Any epithet-style
        # name ("Name, the X") silently couldn't be addressed by first
        # name alone. Fixed by extracting word characters only.
        name_words = [
            w for w in re.findall(r"[a-z0-9']+", npc_name.lower())
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
    # "runaway"/"fallback" (2026-08-23, Coffee, dev-bridge: "when we
    # say fallback, or run or runaway, signifies AI players to run") --
    # the one-word forms of "run away"/"fall back" weren't covered
    # either; _do_flee now also broadcasts real retreat guidance to
    # every AI party member in the same session when this fires.
    flee_words = ["flee", "run away", "runaway", "run from", "try to run", "try to escape", "escape the fight",
                  "retreat", "fallback", "get out of here", "get me out", "make a break for it"]
    if any(w in lowered for w in flee_words):
        return {**base, "action": "flee"}
    # Bare "run" (2026-08-23, Coffee's own literal example: "fallback,
    # or run, or runaway") -- a real word-boundary match (not a plain
    # substring like the rest of flee_words above) so this doesn't fire
    # on "running"/"runner"/an unrelated word merely containing "run".
    if re.search(r"\brun\b", lowered):
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

    # Real live bug (2026-09-01/02, dev-bridge screenshots): "Look at
    # the shallow offshoot" (a real interactable at The Side Pool) got
    # misclassified as "attack" and routed into _do_attack, which then
    # found no matching monster and failed with "No combat is active
    # right now" -- utterly unrelated to what the player actually typed.
    # Root cause: attack_words' plain substring check matched "shoot"
    # inside "off-SHOOT", exactly the same class of bug "fight" inside
    # "Fighter" was already fixed for right above.
    #
    # First fix attempt used an exact word-SET match (fight_words), but
    # that introduced a real regression caught before shipping further:
    # "attacking"/"hitting"/"stabbing"/"swinging"/"shooting" (natural
    # conjugated forms the OLD plain-substring check correctly matched,
    # since e.g. "attack" is a literal prefix of "attacking") silently
    # stopped classifying as attack at all. A `\bword\w*\b` regex is the
    # real fix for both at once: it requires a real word BOUNDARY before
    # the base word (so "shoot" can never match starting mid-token, as
    # in "off-SHOOT"), while `\w*` after it still freely absorbs any
    # real suffix ("-ing"/"-ed"/"-s"), so "attack" also matches
    # "attacking" as its own real prefix -- exactly the two properties
    # a plain substring check conflated into one (and got half of).
    # Multi-word phrases ("cast at", "fire at") stay substring-checked,
    # unaffected -- they were never part of either bug.
    # Real live bug (2026-09-02, Coffee, dev-bridge: "Hit the crystal" ->
    # "Nothing here to fight"). These two checks used to live further
    # down this function, AFTER the generic attack_words check below --
    # but "hit"/"strike"/"attack" are themselves real attack_words, so
    # the generic check always won first and these two were completely
    # unreachable dead code for exactly the verbs their own docstrings
    # say they exist to handle. Moved ahead of the generic attack check
    # so a real switch/crystal/torch or breakable wall/floor mention
    # correctly routes to skill_check before "hit" alone can be
    # swallowed as a combat attack.
    #
    # Real elemental crystal-switch mechanic (2026-09-01, per Coffee's
    # own classic-action-adventure-inspired ask: hitting it also alternates it, same as
    # casting a matching spell at it). "hit"/"strike" is the single most
    # natural verb for a switch/crystal/torch, so it needs the exact
    # same real path into _do_skill_check -> _find_lockable -> _do_lockpick.
    if re.search(r"\b(?:hit|strike|attack|light)\b.*\b(?:switch|crystal|torch|brazier|lantern)\b", lowered):
        return {**base, "action": "skill_check", "ability": "dexterity"}

    # Real breakable wall/floor mechanic (2026-09-01, per Coffee: "cracks
    # in walls we can explode... or cast a fire spell onto it"). A plain
    # hit/bomb/explode attempt routes the same way; casting a real fire/
    # force spell at one is handled separately in _do_cast_spell's own
    # non-combat object-targeting branch, not here.
    if re.search(r"\b(?:hit|strike|attack|bomb|blow up|explode|smash)\b.*\b(?:wall|floor|crack)\b", lowered):
        return {**base, "action": "skill_check", "ability": "dexterity"}

    # Real carry-and-collapse dungeon puzzle (2026-09-03,
    # Phase L4, item 0.5). Same real bug class as the switch/breakable
    # fixes above -- "strike"/"hit" are themselves attack_words, and
    # "pick up"/"carry" would otherwise fall to "gather"/"chat" (a real
    # bug caught before ever shipping, not live yet). Both need this
    # same early routing into skill_check -> _find_lockable -> _do_
    # lockpick's own real "pillar"/"carry_object" branches.
    if re.search(r"\b(?:hit|strike|attack|smash|swing)\b.*\b(?:pillar|column|pedestal)\b", lowered):
        return {**base, "action": "skill_check", "ability": "dexterity"}
    if re.search(r"\b(?:pick up|carry|heave|lift|grab|haul)\b.*\b(?:weight|stone|ball|boulder)\b", lowered):
        return {**base, "action": "skill_check", "ability": "dexterity"}

    attack_phrases = [w for w in attack_words if " " in w]
    attack_single_words = [w for w in attack_words if " " not in w]
    _attack_word_pattern = r"\b(?:" + "|".join(attack_single_words) + r")\w*\b"
    if ((re.search(_attack_word_pattern, lowered) or any(p in lowered for p in attack_phrases))
            and not any(c in lowered for c in conditional_words)):
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

    # check_magic (2026-08-22, per Coffee: "add a Menu for Magic" --
    # same shape as check_equip_menu above, a dedicated read-only
    # listing rather than casting anything). Checked before cast_spell's
    # own "cast "/"use " triggers above never fire for these phrasings
    # anyway (no "cast"/"use" verb here), but kept explicit and narrow
    # ("my spells"/"my magic"/bare "magic") so it can't shadow a real
    # "cast fireball" or "use eldritch blast". "magic menu" is NOT
    # listed here on purpose -- the bare \bmenu\b check much earlier in
    # this function already claims anything containing "menu" as
    # check_menu (the root menu screen), by deliberate, pre-existing
    # design (2026-08-16), so that phrasing never reaches this block.
    if any(w in lowered for w in ["my spells", "my magic", "what spells do i know", "show my spells",
                                    "check my spells", "check my magic", "view my spells", "spell list"]) \
            or re.fullmatch(r"magic(\s+screen)?", lowered.strip(" .!?")):
        return {**base, "action": "check_magic"}

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
    # Found via topic-activity monitoring (2026-08-19): a bare "Party"
    # (the exact label on the merged Party/Party Sheets menu button,
    # v1.27.248) fell all the way through to silent 'chat' -- every
    # trigger below requires "my"/"who's" alongside "party", never a
    # single bare word. Deliberately scoped to the WHOLE message being
    # just that one word (unlike menu/bestiary/waypoints/formation's
    # unconditional \bword\b match anywhere in a sentence) -- "party" is
    # a far more common ordinary English/roleplay word than those ("throw
    # a party", "a party of dignitaries"), so only firing when nothing
    # else was said keeps genuine narrative use of the word safe.
    if lowered.strip(" .!?") == "party":
        return {**base, "action": "check_party"}

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

    # Checked BEFORE resolve_choice below, same reasoning/pattern as the
    # riddle/puzzle preemption just above: real live bug (2026-08-14,
    # dev-bridge screenshot, TWICE in a row): "I choose the path of the
    # battle master"/"I choose the path of the Battle Master" both got
    # swallowed by resolve_choice's own broad "i choose" trigger just
    # below, since this real subclass-pick check normally lives much
    # further down this function (see its full version there, kept
    # unchanged for phrasings that don't say "i choose" at all, e.g.
    # "specialize in evocation"). "path of " immediately followed by a
    # real subclass name is unambiguous -- no board-quest branch choice
    # is ever phrased that way -- so this narrow pre-check only needs to
    # catch that one specific collision, not duplicate the full
    # school/verb matching further down.
    _subclass_names_early = tuple(n.lower() for pair in leveling.CLASS_SUBCLASSES.values() for n in pair)
    if "path of" in lowered and any(name in lowered for name in _subclass_names_early):
        return {**base, "action": "choose_subclass"}

    # Real Fighting Style (2026-09-04): same real collision this file
    # already fixed for subclass names just above -- "I choose Great
    # Weapon Fighting" would otherwise be swallowed whole by the
    # generic "i choose" -> resolve_choice trigger right below.
    _fighting_style_names_early = tuple(s.lower() for s in class_features.FIGHTING_STYLES)
    if any(name in lowered for name in _fighting_style_names_early) and any(
        w in lowered for w in ["choose", "fighting style", "specializ", "adopt", "i'll take", "ill take", "i'll go with", "ill go with"]
    ):
        return {**base, "action": "choose_fighting_style"}

    if any(w in lowered for w in ["i choose", "i decide to", "i decided to", "i've decided", "ive decided",
                                    "i have decided", "i'll go with", "ill go with",
                                    "my choice is", "i'll take the", "ill take the",
                                    "keep it and collect the reward", "leave it be instead"]):
        return {**base, "action": "resolve_choice"}

    if any(w in lowered for w in ["i accept", "i'll do it", "ill do it", "count me in", "i'll help",
                                    "ill help", "i'll take the job", "i'll take it on"]):
        return {**base, "action": "accept_quest"}

    # Real live report (2026-08-30, dev-bridge): "Is the answer an
    # echo?" fell through to silent 'chat' -- a genuine natural-language
    # answer attempt, just phrased as a question rather than a
    # statement ("the answer is X" inverted to "is the answer X"). The
    # player got no response at all and had to fall back to a bare
    # one-word guess to get it recognized -- "is the answer" closes
    # that specific reported gap.
    #
    # Real live bug (2026-09-27, topic-activity monitoring): "Answer is
    # shape" (dropping the leading "the") fell through to the exact same
    # silent 'chat' default -- this list only ever recognized "the
    # answer is", never the bare "answer is" a player just as naturally
    # drops the article from. bot._RIDDLE_ANSWER_PREFIXES (the matching
    # side, once a message IS classified as answer_puzzle) already
    # strips both "the answer is " and "answer is " as separate
    # prefixes -- this classification check just never matched that.
    if any(w in lowered for w in ["the answer is", "answer is", "my answer is", "i think it's",
                                    "i think the answer is", "could it be", "is the answer"]):
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

    if any(w in lowered for w in ["skill tree", "skill points", "skilltree"]) or re.search(r"\bskills\b", lowered):
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

    if any(w in lowered for w in ["divine sense", "sense evil", "sense undead"]):
        return {**base, "action": "divine_sense"}

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
    # Real live bug (2026-08-14, dev-bridge: "Switch my player to
    # ravenloft" silently misclassified as action='chat' TWICE in a row,
    # same silent-failure shape CLAUDE.md's own "Resolved investigations"
    # already documents for the earlier talk_npc bug) -- every trigger
    # here said "character", never "player", even though players clearly
    # use both words interchangeably for their own character. Added the
    # same phrase shapes with "player" substituted in, ordered before
    # the bare "switch to "/"play as " catch-alls for the same reason
    # the "my character " variants already are (a more specific match
    # must win before a shorter one swallows extra words into the name).
    for trigger in ["switch to my character ", "switch character to ", "switch my character to ",
                     "switch to my player ", "switch player to ", "switch my player to ",
                     "switch to ", "play as "]:
        if trigger in lowered:
            name = text[lowered.index(trigger) + len(trigger):].strip()
            return {**base, "action": "switch_character", "target": name or None}

    # Real live gap (2026-08-21, topic-activity monitoring): Coffee typed
    # the bare "Switch my player" -- no target name at all -- which
    # doesn't match any trigger above (every one of those requires a
    # name to follow "to "/"switch to "/"play as "), so it fell all the
    # way through to the default "chat" classification and got silently
    # ignored, the exact same silent-failure shape CLAUDE.md's own
    # "Resolved investigations" already documents for the earlier
    # talk_npc bug (and the 2026-08-14 fix just above for the SAME
    # phrasing but WITH a name). Routed to switch_character with target
    # None -- bot._do_switch_character has its own real guard for a
    # blank target (added alongside this fix) that shows the roster
    # instead of trying to match a fragment, so this can't accidentally
    # silently switch to whichever character happens to be first in the
    # player's own roster.
    if lowered.strip() in ("switch my player", "switch my character", "switch player",
                             "switch character", "switch characters"):
        return {**base, "action": "switch_character", "target": None}

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

    if any(w in lowered for w in [
        "my sheet", "my stats", "my hp", "my health", "my character", "status",
        "active character", "current character", "who am i playing", "which character am i",
        "who am i currently playing", "my class", "what class", "my race", "what race",
        "my gold", "how much gold", "how much money", "player sheet", "player's sheet",
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
    # enter_labyrinth / leave_labyrinth / descend_labyrinth (2026-09-01,
    # Phase L1: "start the labyrinth architecture"). Checked BEFORE
    # move_words just below -- a real conflict found writing this:
    # move_words' own "enter the" entry (for ordinary movement phrasing
    # like "enter the tavern") would otherwise win first and misclassify
    # "I enter the labyrinth" as a plain move. "labyrinth" is distinctive
    # enough in this game's vocabulary (never used for anything else)
    # that a bare mention alongside a clear verb is unambiguous, same
    # convention as the echo-trial check above. "descend"/"go deeper"
    # alone stays ambiguous outside this context -- only fires here when
    # "labyrinth" is also named; the CORE way to descend is simply
    # walking to the floor's own stairs room via an ordinary move, which
    # bot.py's _do_labyrinth_move already detects on its own regardless
    # of this regex.
    if "labyrinth" in lowered:
        if any(w in lowered for w in ["enter", "start", "begin", "go into", "go in", "step into"]):
            return {**base, "action": "enter_labyrinth"}
        if any(w in lowered for w in ["leave", "exit", "abandon", "retreat", "flee"]):
            return {**base, "action": "leave_labyrinth"}
        if any(w in lowered for w in ["descend", "go down", "go deeper", "stairs down"]):
            return {**base, "action": "descend_labyrinth"}

    # Real live feature (2026-09-04, per Coffee: "we left the labyrinth
    # and when i went to return it reset?" -> "make a seed logging
    # system so if that happen u can reload the seed... also let us
    # load and play a seed with the generator" -- example phrasing he
    # gave, "Load labyrinth seed #123456" -- then explicitly asked for
    # bare "what is my seed"/"load seed"/"show the seed"/"current seed"
    # to ALSO work without the word "labyrinth"). "seed" as a real
    # number-loading concept never collides with this game's own
    # unrelated "a seed"/"The Last Seed" quest item -- that content is
    # only ever reached via gather/examine's own real trigger words
    # (checked elsewhere in this file), never "load"/"what is"/"show"/
    # "current" phrasing.
    load_seed_match = re.search(r"\bload\b.*?\bseed\b\s*#?\s*(\d+)", lowered)
    if load_seed_match:
        result = {**base, "action": "load_labyrinth_seed", "seed": int(load_seed_match.group(1))}
        segment_match = re.search(r"\bsegment\s*#?\s*(\d+)", lowered)
        if segment_match:
            result["segment"] = int(segment_match.group(1))
        return result
    if re.search(r"\b(?:what(?:'s| is)|show|check)\b.*\bseed\b", lowered) or "current seed" in lowered:
        return {**base, "action": "check_labyrinth_seed"}

    # "explore " added 2026-09-04 (found via the AI-driven Labyrinth
    # playtest tool): "I explore Labyrinth A Mirrored Chamber (II)" --
    # explicitly naming a real, just-displayed exit -- fell all the way
    # through to the fully silent 'chat' default. No "to" needed, same
    # bare-verb shape "enter the"/"descend"/"ascend" already use.
    move_words = ["go to", "goto", "head to", "heading to", "walk to", "walking to",
                  "travel to", "traveling to", "travelling to", "move to", "moving to",
                  "enter the", "descend", "ascend", "explore ",
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

    # Real live bug found via topic-activity monitoring (2026-09-04): an
    # AI companion's own real narration for a real Labyrinth descend --
    # "I proceed toward floor 5." -- was silently misclassified as chat.
    # Same shape as the compass-direction fix just above (a movement
    # VERB, here paired with "toward"/"towards" instead of a bare
    # direction): move_words only ever matched a fixed "<verb> to"
    # phrase, and "proceed"/"advance"/"make my way" were never in that
    # list at all -- "head toward" only ever worked by accident, since
    # "head to" happens to be a literal substring of "head toward".
    if re.search(r"\b(?:travel(?:l?ing)?|go(?:ing)?|goto|head(?:ing)?|walk(?:ing)?|mov(?:e|ing)|ride|run(?:ning)?|march(?:ing)?|sail(?:ing)?|proceed(?:ing)?|advanc(?:e|ing)|continu(?:e|ing)|make my way|making my way)\s+"
                 r"towards?\b", lowered):
        return {**base, "action": "move"}

    # Real live bug (2026-08-31, dev-bridge: a bare "look" from Elduinn
    # got sent to the model -- since no multi-word phrase below matched
    # it -- and the model hallucinated action="leave_guild", silently
    # removing him from the Forge Guild. Same failure shape as this
    # file's other documented misclassifications on this small model
    # ("my characters" -> start_combat, "show me the map" -> move) --
    # fixed the same way: a bare, otherwise-ambiguous single word gets
    # its own deterministic match instead of ever reaching the model.
    if re.fullmatch(r"look[.!]?", lowered.strip()):
        return {**base, "action": "look"}

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
    for trigger in ["examine the ", "examine ", "look at the ", "look closer at ", "look closer", "look under the ", "look under ",
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
    # "approach(ed)?" added 2026-09-04 (found via the AI-driven
    # Labyrinth playtest tool): "I approach the structural trigger
    # here"/"I approach the locked door" both fell all the way through
    # to the fully silent 'chat' default, the exact same "verb not
    # covered" gap this file has hit many times before -- this game has
    # no real spatial positioning within a room, so "approach X" (an
    # already-visible object right here) means the same thing as "look
    # closer at X".
    examine_verb_match = re.search(
        r"\b(?:read|observed|examined|inspected|searched|checked out|touch(?:ed)?|view(?:ed|ing)?|"
        r"looked (?:at|closer at)|(?:peer|perr)(?:ed)? (?:at|into|in)|glanced? at|gaze(?:d)? at|approach(?:ed|ing)?)\b\s+"
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
    # Real live bug (2026-08-31, dev-bridge, Elduinn: "Open the door" at
    # a real locked door -- the Wrathflame Vault's own warded iron gate
    # -- got "doesn't spot anything like that here" instead of actually
    # attempting the lock). Root cause: the generic "open X" -> examine
    # catch-all right below has no awareness of lockables at all, so the
    # single most natural phrase for a real locked door/gate was
    # swallowed as a failed examine before ever reaching the real
    # "pick the lock" dexterity check further down this function.
    # Checked BEFORE that catch-all, same "carve out the real skill-
    # check phrasing first" shape as force open/break open/smash below
    # -- _do_skill_check's own _find_lockable resolution (kind_words
    # already covers "door"/"gate"/"lock") correctly attempts the real
    # lock if one exists here, and falls through to an honest generic
    # ability check otherwise, never a false "nothing here" examine.
    if re.search(r"\bopen(?:ing|ed)?\b.*\b(?:door|gate|lock)\b", lowered):
        return {**base, "action": "skill_check", "ability": "dexterity"}

    # Real live bug (2026-09-01, dev-bridge screenshots: Sugar's "Pull
    # the lever" and Charvenna's "Pull the thick root lever" -- Deep
    # Root Vault's own room text literally reads "A thick root lever is
    # here, waiting to be pulled" -- both got total silence, reported
    # as "why isn't this working, why so slow"). Same exact gap as the
    # "open the door/gate" fix just above, just never generalized to
    # the lever kind: "pull" is the single most natural verb for a
    # lever (the room's own generated description uses the word
    # "pulled"), but nothing routed it to skill_check at all, so it
    # fell all the way through to the silent "chat" default -- not a
    # performance issue, a real routing gap. _find_lockable's own kind_
    # words for a lever already include "lever"/"switch", so this only
    # needs to get the player INTO _do_skill_check in the first place.
    if re.search(r"\bpull(?:ing|ed)?\b.*\b(?:lever|switch)\b", lowered):
        return {**base, "action": "skill_check", "ability": "dexterity"}

    # The crystal-switch and breakable wall/floor "hit" checks that used
    # to live here were moved earlier in this function (ahead of the
    # generic attack_words check) -- see that comment for why; "hit"/
    # "strike"/"attack" are themselves attack_words, so leaving these
    # this far down made them permanently unreachable dead code.

    # Real classic-action-adventure-style visible pit mechanic (2026-09-01, per Coffee):
    # jumping down a real, visible gap to the floor below is a genuine
    # one-way MOVE, not a skill check -- routed straight to the same
    # "move" action _do_move already handles everything else through;
    # _do_move's own new pit_down_to check does the actual work.
    if re.search(r"\bjump\b.*\b(?:down|through|into)\b.*\b(?:gap|hole|pit|opening|chasm)\b|\bjump down\b", lowered):
        return {**base, "action": "move"}

    # Real pressure-plate / movable-object mechanic (2026-09-01, per
    # Coffee: pushing a crate/statue onto a plate, or filling an urn
    # with liquid). Same routing shape as the switch/breakable fixes
    # above -- into skill_check -> _do_skill_check -> _find_lockable ->
    # _do_lockpick's own real "pressure_plate" branch.
    #
    # Real live bug + real misclassification (2026-09-04, dev-bridge,
    # Coffee: "Stand on the pressure plate" got "Summon which Remnant?
    # You've bound..." -- a completely unrelated action). Root cause:
    # this regex's own verb list never included "stand"/"step" -- the
    # single most natural real-world way to trigger a pressure plate,
    # arguably more natural than "push onto" for a plate specifically
    # (unlike a crate/urn, which really is pushed) -- so the text fell
    # all the way through the deterministic fallback chain and reached
    # the AI classifier, which guessed wrong. bot._do_activate_
    # pressure_plate's own real mechanic already doesn't care HOW the
    # plate gets triggered (the `verb` argument is flavor text only,
    # the state toggle is identical either way), so there's no real
    # reason to keep "stand"/"step" out -- same "carve out the real
    # skill-check phrasing first" fix shape as the door/lever gaps
    # above.
    if re.search(r"\b(?:push|place|put|move|shove|stand|step)\b.*\b(?:onto|on)\b.*\b(?:plate|switch|urn|crate|statue|block)\b", lowered):
        return {**base, "action": "skill_check", "ability": "dexterity"}
    if re.search(r"\bfill\b.*\b(?:urn|vessel|basin|jug|pot)\b.*\bwith\b", lowered):
        return {**base, "action": "skill_check", "ability": "dexterity"}

    # Real "as above, so below" mechanic (2026-09-01, per Coffee):
    # pushing a movable object down a real pit can activate a real
    # pressure plate in the room below -- a genuinely different action
    # from the player's own "jump down" just above (a distinct verb
    # set: push/throw/drop/shove/kick, never "jump"), so it gets its
    # own real action/handler (_do_push_object_down_pit) instead of
    # overloading "move".
    if re.search(r"\b(?:push|throw|drop|shove|kick)\b.*\b(?:down|into|through)\b.*\b(?:gap|hole|pit|opening|chasm)\b", lowered):
        return {**base, "action": "push_down_pit"}

    # Real live feature request (2026-08-31, dev-bridge, Elduinn: "I
    # don't know if this area has a lever but if it doesn't, you should
    # tell us"). A genuine QUESTION about whether a lockable exists here
    # ("look for a lever", "is there a lever", "do you see a lock") is
    # different from an ATTEMPT to open one (handled above) -- this
    # routes to a real, honest examine instead, which bot.py's
    # _do_examine now checks against the location's real lockables
    # (falling back to _find_lockable) before giving up, so the player
    # gets a real "yes, there's a rusted lever here" or a real "no,
    # nothing like that here", never silence or a misfire into an
    # unrelated action. Excludes "hidden"/"secret" so the existing
    # "look for hidden door" -> Perception-check phrasing (a real,
    # different mechanic, handled by skill_check_verb_abilities/
    # _mentions_hidden_passage further below) is never shadowed.
    if "hidden" not in lowered and "secret" not in lowered:
        lockable_query_match = re.search(
            r"\b(?:look for|search for|is there|are there|do you see|any|see a|see an)\b.*?"
            r"\b(lever|switch|lock|door|gate|chest|wheel|valve)\b",
            lowered,
        )
        if lockable_query_match:
            return {**base, "action": "examine", "target": lockable_query_match.group(1)}

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

    # check_equip_menu (2026-08-16, dev-bridge, same request as
    # check_menu/check_formation/check_waypoints above): a bare "equip"
    # with no item named means "show me what I can equip" (the existing
    # _do_show_equip_menu, previously only reachable via "/menu" ->
    # "Equip Gear") -- checked BEFORE equip_item's own "equip " trigger
    # just below so it only ever catches the truly bare form, never
    # shadowing a real "equip my sword"/"equip the longbow".
    if re.fullmatch(r"equip(\s+(gear|menu|screen))?", lowered.strip(" .!?")):
        return {**base, "action": "check_equip_menu"}

    # Real, mastery-gated dual wielding (2026-09-04) -- checked BEFORE
    # the generic equip_item trigger just below, same "more specific
    # phrasing wins first" discipline unequip_item's own check above
    # already follows, since "dual wield X"/"equip X in my off hand"
    # would otherwise share the bare "equip "/"wield " trigger word.
    if any(w in lowered for w in ["dual wield", "dual-wield", "off hand", "off-hand", "offhand", "second weapon"]):
        return {**base, "action": "equip_offhand"}

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
    # forge_magic_item (2026-09-08, task #6) checked BEFORE the plain
    # forge_item trigger just below -- "forge my longsword into a magic
    # item" would otherwise match "forge my" first and misfire as a
    # plain tier-reforge instead. A player who just says "forge my X"
    # with no "magic item" wording still falls through to forge_item,
    # unaffected.
    if any(w in lowered for w in ["into a magic item", "magic item out of", "make it magic", "magic upgrade", "forge a magic"]):
        return {**base, "action": "forge_magic_item"}
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

    # Dismantling (2026-08-13, per Coffee: "let the player say 'dismantle'
    # the (item)") -- checked right alongside discard above since it's
    # the same "my inventory item" phrasing family, but a distinct
    # outcome (real materials back, not just gone).
    #
    # Real gap found 2026-09-16 (dev-bridge report: "Dismantle 20 rusty
    # daggers" fell all the way through to the model, which misclassified
    # it as plain "chat") -- the old my/the/i-prefixed phrasing list
    # never matched a bulk-count phrasing with no "my"/"the" at all.
    # "dismantle"/"salvage" are distinctive enough words in this game's
    # own vocabulary (neither is a substring of any other real command
    # word here) that a bare match is safe, same reasoning already
    # applied to other single-word gameplay verbs in this file.
    if "dismantle" in lowered or "salvage" in lowered:
        return {**base, "action": "dismantle_item"}

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
    # Real live bug (2026-08-30, dev-bridge, Coffee/Elduinn: "Drink from
    # the warm clear spring" got "Use what, exactly? Name a consumable
    # you're actually carrying."): this "drink " catch-all fires before
    # the real drink_water fast-path further below ever gets a chance,
    # since a location's own water/spring/pool is never a carried item.
    # Deferring to drink_water here (instead of duplicating its own
    # water/spring/pool wording check) keeps exactly one place deciding
    # what counts as "drinkable location water" -- "drink my potion"/
    # "drink the vial" etc. still lands on use_item exactly as before,
    # since none of those mention water/spring/pool.
    if (any(w in lowered for w in ["drink ", "quaff"]) and not any(w in lowered for w in ("water", "spring", "pool"))) \
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

    # Real live bug (2026-08-27, found via topic-activity monitoring):
    # two different real players, in two separate sessions, typed a
    # bare "Rest"/"rest" and got the silent chat default -- every
    # existing rest_words entry below required "I rest"/"want to
    # rest"/etc, none of them a standalone imperative. Same "bare
    # single-word command" gap already fixed for "Fight" (2026-08-07).
    # Checked BEFORE rest_words below (not after) specifically because
    # rest_words' own "rest here" would otherwise match "we cant rest
    # here" first and skip this negation guard entirely -- this one
    # check now covers every phrasing containing the word "rest" at
    # all, negation-guarded the same shape as negated_fight_phrases,
    # since "I can't rest with these goblins around" is a warning, not
    # a request to actually rest.
    negated_rest_phrases = [
        "can't rest", "cant rest", "cannot rest", "won't rest", "wont rest",
        "don't want to rest", "dont want to rest", "shouldn't rest", "shouldnt rest",
    ]
    if any(p in lowered for p in negated_rest_phrases):
        pass  # falls through to whatever this actually is instead of guessing "rest"
    elif re.search(r"\brest\b", lowered) and not any(c in lowered for c in conditional_words):
        return {**base, "action": "rest"}

    # "take a rest" is deliberately NOT here — it's claimed by go_inactive
    # above, since the user considers it equivalent to "resting until
    # next session," not the in-universe full-heal action. "i rest"/
    # "let's rest"/"lets rest"/"want to rest"/"rest here" are no longer
    # listed here either -- all contain the bare word "rest", so the
    # negation-guarded whole-word check just above already covers them
    # (and, unlike this unguarded list ever did, without misreading
    # "we cant rest here" as a real request to rest).
    rest_words = ["revive me", "heal up", "recover", "heal me"]
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
    # Real live bug (2026-08-30, dev-bridge, Coffee/Elduinn: "Drink from
    # the warm clear spring" got "Use what, exactly? Name a consumable
    # you're actually carrying." -- the Chapter 3-8 expansion's own new
    # checkpoint rooms describe their water as a "spring"/"pool" in
    # their own flavor text, matching real player phrasing that never
    # says the bare word "water" at all. Widened to the same real
    # synonyms _do_drink_water's own docstring already treats as
    # interchangeable ("holy water", a spring, a pool) -- still gated
    # on the drink verb itself, so this never fires on an unrelated
    # sentence that merely mentions a spring/pool in passing.
    if re.search(r"\bdrink(?:s|ing)?\b", lowered) and any(w in lowered for w in ("water", "spring", "pool")):
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

    # check_professions moved up near bestiary/leaderboard (see that
    # comment, 2026-09-15) -- it's unreachable here now that "open"/
    # "view"/"read"/etc. examine triggers earlier in this function
    # would always win first anyway; kept out entirely rather than left
    # as dead code that looks reachable but never runs.

    unconditional_shove_words = ["shove", "tackle", "trip", "push over"]
    knock_down_phrasing = "knock" in lowered and ("prone" in lowered or "down" in lowered)
    if any(w in lowered for w in unconditional_shove_words) or knock_down_phrasing:
        return {**base, "action": "shove"}

    # Real 5E core action, previously entirely missing from this engine
    # (2026-09-27, feature-wishlist audit) -- a real grapple, distinct
    # from shove (restrains, doesn't knock prone). Checked for the
    # ESCAPING side first ("break free"/"escape" while already
    # grappled) since "escape the grapple" would otherwise also match
    # the grapple-initiation words below ("grab"/"hold"/"grapple").
    if any(w in lowered for w in ["break free", "escape the grapple", "escape grapple",
                                    "wriggle free", "wriggle loose", "struggle free", "shake off the grapple"]):
        return {**base, "action": "escape_grapple"}
    if any(w in lowered for w in ["grapple", "grab hold of", "seize", "restrain", "wrestle", "pin down", "hold them down"]):
        return {**base, "action": "grapple"}

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
                "options": {"num_predict": 400, "num_thread": config.OLLAMA_NUM_THREAD},
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
                "summon_remnant", "recruit_npc", "rest",
                "go_inactive", "skill_check", "shove", "grapple", "escape_grapple", "show_map", "gather", "craft",
                "list_characters", "switch_character", "delete_character",
                "fast_travel", "accept_quest", "check_quests", "ask_clue",
                "answer_puzzle", "gamble", "chat", "examine", "flee", "resolve_choice",
                "invite_to_party", "accept_party_invite", "leave_party", "bench_party_member",
                "unbench_party_member", "set_front_row", "set_back_row", "find_merchant",
                "second_wind", "rage", "bardic_inspiration", "lay_on_hands", "divine_sense", "arcane_recovery",
                "make_campfire", "give_item", "use_item", "equip_item", "unequip_item", "auto_equip", "breath_weapon",
                "forge_item", "forge_magic_item", "enchant_item", "discard_item", "dismantle_item",
                "channel_divinity", "action_surge", "reckless_attack", "divine_smite",
                "flurry_of_blows", "wild_shape", "toggle_manual_dice", "level_up", "auto_level_up_party",
                "set_description", "set_pronouns",
                "bestiary", "list_shop", "leaderboard", "check_achievements", "set_title", "check_weather",
                "check_guild_quest", "dice_game", "fortunes_wheel", "set_alignment", "message_ai",
                "skill_tree", "challenge_duel", "accept_duel", "check_market", "cancel_market",
                "sell_market", "buy_market", "view_market_listing", "join_battle",
                "replay_intro", "visual_map", "rebirth", "choose_hybrid", "give_offering",
                "drink_water", "choose_subclass", "choose_fighting_style", "equip_offhand", "start_echo_trial", "check_professions",
                "talk_party", "use_environment", "throw_weapon", "push_down_pit",
                "enter_labyrinth", "leave_labyrinth", "descend_labyrinth",
                "check_labyrinth_seed", "load_labyrinth_seed",
                "check_menu", "check_formation", "check_waypoints", "check_equip_menu",
                "check_blacksmith_menu", "check_alchemy_menu", "check_cooking_menu",
                "check_remnants", "check_story", "check_magic", "check_affinity",
                "trade_request", "trade_add", "trade_remove", "trade_accept", "trade_cancel", "trade_status",
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
            # Same defensive pattern again (2026-08-28, topic-activity
            # signal): a lost/confused player typing "Which way" and,
            # moments later, "I'm lost" both got classified as
            # dismantle_item -- a real, destructive, hard-to-undo action
            # (permanently breaks down a real inventory item into
            # materials) with zero relation to either message. Never
            # trusted from the model alone unless the raw text actually
            # contains one of its own real trigger words, same reasoning
            # as start_combat/pass_turn/flee/attack above.
            if parsed["action"] == "dismantle_item" and not any(w in text.lower() for w in ["dismantle", "salvage"]):
                return fallback
            # Same defensive pattern again (2026-08-31, dev-bridge, real
            # live incident): a bare "look" from Elduinn and "Surface
            # map" from Charvenna were both classified as "leave_guild"
            # by the model -- neither message has anything to do with
            # guilds at all, and this actually fired, silently removing
            # Elduinn from the Forge Guild. leave_guild/join_guild are
            # real, consequential, hard-to-notice-gone-wrong actions
            # (nothing about losing your guild is loud), so -- same
            # grounding the deterministic fallback already requires of
            # itself for these two actions -- never trusted from the
            # model alone unless the raw text actually contains "guild"
            # or a real guild's own name/id.
            if parsed["action"] in ("leave_guild", "join_guild") and not (
                "guild" in text.lower() or any(gid.replace("_", " ") in text.lower() or g["name"].lower() in text.lower() for gid, g in GUILDS.items())
            ):
                return fallback
            # Same real bug class again (2026-08-31, dev-bridge, real live
            # incident): "Look for a lever" -- a genuine, ordinary
            # question about a room, nothing to do with anyone's party --
            # got classified as leave_party by the model, and it actually
            # fired, silently dropping the player from their real party
            # mid-dungeon. leave_party is exactly as consequential and
            # exactly as easy to miss as leave_guild -- same fix, same
            # reasoning: never trusted from the model alone unless the
            # raw text actually contains "party".
            if parsed["action"] == "leave_party" and "party" not in text.lower():
                return fallback
            # Same real bug class again (2026-09-26, topic-activity
            # signal, a real player mid-puzzle): a wrong riddle guess
            # with a typo, "The answer os hollow" -- nothing to do with
            # anyone's party at all -- got classified as invite_to_party
            # with target "hollow" by the model. Same "never trust the
            # model alone unless the raw text actually contains 'party'"
            # fix as leave_party right above.
            if parsed["action"] == "invite_to_party" and "party" not in text.lower():
                return fallback
            # Same real bug class again (2026-09-25, topic-activity signal
            # while a real player was stuck on a puzzle): a bare one-word
            # guess, "Voices" (an unsuccessful riddle-answer attempt, not
            # a party command at all), got classified as bench_party_
            # member with target "Voices" by the model -- there's no real
            # companion by that name and no bench-related vocabulary
            # anywhere in the message. bench_party_member/unbench_party_
            # member are real roster changes with a real combat
            # consequence (a benched member sits out the next fight) --
            # same "never trust the model alone on a consequential action
            # with zero real grounding" reasoning as leave_guild/
            # leave_party/summon_remnant above. Grounded on the same
            # literal trigger words the deterministic fallback itself
            # requires ("bench"/"bring ... back"/"add back") -- a real
            # known-NPC-name mention doesn't need its own clause here
            # (unlike summon_remnant's Remnant-name check below): any
            # message naming a known companion already resolves to
            # talk_npc inside _keyword_fallback itself, well before
            # fallback["action"] != "chat" would even let this model
            # branch run.
            if parsed["action"] in ("bench_party_member", "unbench_party_member") and not (
                "bench" in text.lower()
                or ("bring" in text.lower() and "back" in text.lower())
                or "add back" in text.lower()
            ):
                return fallback
            # Same defensive pattern again (2026-09-04, topic-activity
            # signal): three different, unrelated real inputs in one
            # evening -- "Stand on the pressure plate", a bare
            # "Inventory", and "Go through the door" -- were all
            # classified as summon_remnant by the model, each firing a
            # real, confusing "Summon which Remnant? You've bound: ..."
            # prompt with zero relation to what the player actually
            # said. summon_remnant is exactly as exciting/sticky-
            # sounding a wrong guess for text this model doesn't
            # understand as start_combat/pass_turn/flee/dismantle_item/
            # leave_guild/leave_party above, and had no equivalent
            # guard. Never trusted from the model alone unless the raw
            # text actually contains a real trigger word or names a
            # real bound Remnant -- the same grounding the deterministic
            # fallback (mentions_a_real_remnant, above in this same
            # file) already requires of itself.
            if parsed["action"] == "summon_remnant" and not (
                re.search(r"\b(?:summon|call forth|invoke)\b", text.lower())
                or any(r["name"].lower() in text.lower() or rid.replace("_", " ") in text.lower() for rid, r in REMNANTS.items())
            ):
                return fallback
            # Real live bug (2026-08-15, dev-bridge screenshot): "Take
            # the band" -- a ring the player had just been shown in a
            # quest/reward preview but never actually earned yet -- came
            # back as action="chat" (silence) from both the fallback
            # (the take_from_match rule above deliberately only fires
            # with a "from" clause) and the model. Reported as "was this
            # supposed to happen?" -- a fully silent non-reply to an
            # unambiguous imperative sentence is never correct, same
            # "Say hello to grimsby" class of bug fixed above. Once the
            # model has ALSO given up (still "chat" here), reclassify a
            # bare take/grab/pick-up phrase as "examine": _do_examine
            # already gives an honest, specific answer either way --
            # the item's real description if it's actually owned, or "
            # doesn't spot anything like that here" if it's a
            # not-yet-earned reward/name mentioned only in flavor text --
            # strictly better than silence in both cases. Never
            # overrides a model action that ISN'T "chat" (e.g. a correct
            # "use_item" read on "take the healing potion" is untouched).
            if parsed["action"] == "chat":
                bare_take_match = re.search(
                    r"\b(?:take|took|grab|grabbed|pick up|picked up)\b\s+(?:the |a |an )?(.+)", text.lower(),
                )
                if bare_take_match:
                    target = text[bare_take_match.start(1):bare_take_match.end(1)].strip()
                    if target:
                        return {**fallback, "action": "examine", "target": target}
            return parsed
    except (requests.RequestException, ValueError) as e:
        print(f"[intent_parser] model call failed, using keyword fallback: {e}")

    return fallback


_COMPOUND_SPLIT_PATTERN = re.compile(r",\s*(?:and\s+)?|\s+and then\s+|\s+then\s+|\s+and\s+|;\s*")

# Actions where a player commonly lists several items in one message
# ("buy 10 torches, 1 pickaxe, 5 bait") -- see parse_intents below.
_SHOPPING_LIST_ACTIONS = {"buy", "sell", "give_item", "equip_item", "trade_add", "trade_remove"}
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

        # Real live bug (2026-08-24, self-improvement monitoring pass,
        # topic-activity log): "move grask and wren to the backrow"
        # split into "move grask" / "wren to the backrow" -- each
        # fragment names a real, known companion with no row keyword of
        # its own, so both independently (and wrongly) matched talk_npc
        # instead of falling through to "chat" -- exactly the ambiguous-
        # fragment trap the whole, UNSPLIT text's own multi-name
        # formation matcher (v1.27.332) already solves correctly
        # ("grask and wren" as one shared target). Only overrides the
        # split when EVERY segment independently landed on talk_npc --
        # a genuine "move X to the front row, then attack the goblin"
        # splits into two DIFFERENT real actions (set_front_row,
        # attack), never all talk_npc, so this never touches that
        # already-working linked-turn-action case (v1.27.333).
        whole_message_fallback = _keyword_fallback(text, known_npc_names, environment_name=environment_name)
        if (whole_message_fallback["action"] in ("set_front_row", "set_back_row")
                and all(i["action"] == "talk_npc" for i in segment_intents)):
            return [whole_message_fallback]

        real_segment_intents = [i for i in segment_intents if i["action"] != "chat"]
        if len(real_segment_intents) >= 2:
            return real_segment_intents

    return [parse_intent(text, known_npc_names, force_model=force_model, environment_name=environment_name)]
