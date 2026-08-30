"""
remnants.py
The Remnants (2026-08-13, per Coffee: "Espers"-inspired -- built on
this game's own real, already-existing lore instead: Pandora's Box.

Real story hook: long before anyone alive remembers, Pandora's Box was
opened and everything inside it scattered across the world. Most of
what escaped weakened into the ordinary monsters players fight every
day -- but a rare few endured whole, too old and too strong to ever
fully fade. Those are the Unbound: real, single, boss-tier fights,
each one hidden somewhere and found only by real exploration, never
handed to a player.

Defeating an Unbound doesn't destroy it -- it lets whoever helped
defeat it each bind their own real fragment of its essence (every
party member present when it falls, not just whoever landed the
killing blow). Whoever bound a fragment can call it into a later fight
themselves, no designated "Summoner" role needed (removed 2026-08-20,
per Coffee: "i want players that have beaten the Remnant to be
automatically bound to them. That is the incentive for them players
to find them and beat them" -- a single-holder gate undercut that
incentive for anyone but the one player holding the role) -- a themed
attack, not a second combatant, following the same real damage-type
pipeline (rules.combat.apply_damage_type_modifier) every other attack
in this game already uses -- never an invented mechanic bolted on the
side.

This ties directly into real, already-existing content, not invented
from nothing: campaigns/default/campaign.json's "choir_remnant"
monster, "The Remnant's Echo" quest, and the Echo-Bound Signet item
("Whatever it echoes back was never quite what you said") already
established this exact "echo/remnant/bound" vocabulary before this
system existed.

`story_tied` (2026-08-13, per Coffee: "make it so 60% of the summons
[Remnants] are tied to the storyline... the others (really good ones)
shud be findable... make some secrets" -- every Remnant originally
launched pure-secret, exploration-only). 7 of 12 (~60%) are now
story_tied=True: once a player has genuinely VISITED that Remnant's
real location (character["visited_locations"], the same real fact the
map/bestiary fog-of-war already uses) but hasn't bound it yet, its
real lore surfaces as a "Whispers" rumor on the Story So Far screen
(bot._do_show_story_so_far/_remnant_rumors_for_character) -- a real
nudge back toward content they've already walked past, not a spoiler
of one they haven't. The 5 strongest by real average summon damage
(story_tied=False) stay exactly as originally designed: pure secrets,
never hinted at, found only by genuine off-the-beaten-path exploration.
"""

# Every element used below is one of this game's own real, already-
# implemented damage types (rules/combat.py's apply_damage_type_
# modifier) -- never an invented one.
#
# "earth" added 2026-08-30 (Elemental Foundations, per Coffee: "im not
# sure what u have planned but i want all types of elementals included"
# -- classical Earth/Air/Fire/Water coverage). Air deliberately reuses
# the existing "lightning" type rather than adding a redundant one
# (Coffee's own catch: "what elemental type is lightnening considered
# ??? cant that be air type ?") -- Fire and Water already mapped onto
# "fire"/"cold" beforehand, so Earth is the only genuinely new type.
REAL_DAMAGE_TYPES = ("cold", "earth", "fire", "force", "lightning", "necrotic", "physical", "poison", "psychic", "radiant")

# Real secondary summon effects (2026-08-13) -- deliberately a small,
# fully-implemented set (see bot._do_summon_remnant) rather than one
# bespoke mechanic per Remnant, the same "data-driven, not code-per-
# entry" shape every other catalog in this game already uses
# (items.py, spells.py, guilds.py). "none" is a real, valid choice --
# some Remnants are just the hardest-hitting option, no secondary.
SUMMON_SECONDARY_EFFECTS = ("none", "dot", "self_heal", "party_heal")

# Real live report (2026-08-20, per Coffee, live-tested against a real
# Giant Spider: "i feel the remnants damage was abit low? i was
# thinking a remnant shud be doing about 150-300 damage against a
# lower lv enemy... Remnants are supposed to be a strong attack or
# ability. stronger than an attack or spell which is why its capped to
# 1 use until mastery. can u make sure all the remnants damage gets
# inscreased including the first one we already achieved"). Confirmed:
# The Wrathflame Unbound's real summon averaged only ~79.5 damage (3d8
# +16 own bonus +50 source damage_bonus) -- well under his own stated
# floor. A single flat bonus, applied on top of the existing summon_
# damage_bonus + source-boss damage_bonus stack (v1.27.268) at the one
# real calculation site (bot._do_summon_remnant), lands the weakest
# Remnant (The Root That Remembers, was ~71 avg) at ~161 and the
# strongest (The Unopened, was ~259.5 avg) at ~349.5 -- every one of
# the 12 now clears the requested floor, including Wrathflame Unbound
# itself (~79.5 -> ~169.5), and the already-strongest ones scale even
# further past 300 rather than being compressed down to it, matching
# "stronger source = stronger Remnant" (v1.27.268's own design intent)
# instead of flattening that gap. One tunable constant, not 12
# hand-edited dict entries, so a future adjustment never risks drifting
# the 12 out of relative sync with each other again.
REMNANT_SUMMON_POWER_BONUS = 90

# Every Remnant here is grounded in a REAL location already in
# campaigns/default/campaign.json (never a new zone invented for this
# system) -- each was previously an empty room with no monster, chosen
# deliberately so finding one really does mean real exploration off
# the beaten path, "just like the labyrinth, and just like the
# colosseum" per Coffee's own bar for difficulty and discovery.
REMNANTS = {
    "the_unopened": {
        "summon_damage_dice": "3d10", "summon_damage_bonus": 18,
        "name": "The Unopened",
        "monster_key": "the_unopened",
        "story_tied": False,
        "location_id": "greymoor_downs_the_unopened_seal",
        "element": "force",
        "summon_secondary": "none",
        "lore": (
            "The first thing that ever escaped the Box, and the one that never fully got out -- "
            "half of it is still somewhere else, straining against a seal that was never meant to hold forever."
        ),
    },
    "the_wrathflame_unbound": {
        "summon_damage_dice": "3d8", "summon_damage_bonus": 16,
        "name": "The Wrathflame Unbound",
        "monster_key": "the_wrathflame_unbound",
        "story_tied": False,
        "location_id": "hollow_stump_shrine",
        "element": "fire",
        "summon_secondary": "none",
        "lore": "Not anger given shape -- anger that WAS a shape, once, before the Box, and hasn't forgiven the world for forgetting that.",
    },
    "the_drowned_choir": {
        "summon_damage_dice": "2d8", "summon_damage_bonus": 12,
        "name": "The Drowned Choir",
        "monster_key": "the_drowned_choir",
        "story_tied": True,
        "location_id": "the_weeping_well",
        "element": "poison",
        "summon_secondary": "dot",
        "lore": "It doesn't sing so much as it keeps singing, long after anyone still listening should have stopped being able to.",
    },
    "the_root_that_remembers": {
        "summon_damage_dice": "2d8", "summon_damage_bonus": 12,
        "name": "The Root That Remembers",
        "monster_key": "the_root_that_remembers",
        "story_tied": True,
        "location_id": "whispering_wood_deep_glade",
        "element": "poison",
        "summon_secondary": "dot",
        "lore": "Every forest has one root older than the forest. This one remembers what was planted here before there was a forest at all.",
    },
    "the_hollow_bell": {
        "summon_damage_dice": "3d8", "summon_damage_bonus": 16,
        "name": "The Hollow Bell",
        "monster_key": "the_hollow_bell",
        "story_tied": False,
        "location_id": "whispering_wood_root_hollow",
        "element": "psychic",
        "summon_secondary": "none",
        "lore": "It rang once, the day the Box opened, and the sound never actually finished arriving -- it's still landing, somewhere, on someone.",
    },
    "the_cairnbound": {
        "summon_damage_dice": "2d10", "summon_damage_bonus": 13,
        "name": "The Cairnbound",
        "monster_key": "the_cairnbound",
        "story_tied": True,
        "location_id": "greymoor_downs_lonely_cairn",
        "element": "necrotic",
        "summon_secondary": "self_heal",
        "lore": "Buried under stones piled by hands that all had the same reason, and none of them wrote it down.",
    },
    "the_waiting_dark": {
        "summon_damage_dice": "2d10", "summon_damage_bonus": 13,
        "name": "The Waiting Dark",
        "monster_key": "the_waiting_dark",
        "story_tied": True,
        "location_id": "greymoor_downs_below_the_cairn",
        "element": "necrotic",
        "summon_secondary": "self_heal",
        "lore": "Beneath the cairn is older than the cairn. It has been very patient, and it is not tired of waiting yet.",
    },
    "the_farthest_span": {
        "summon_damage_dice": "3d8", "summon_damage_bonus": 17,
        "name": "The Farthest Span",
        "monster_key": "the_farthest_span",
        "story_tied": False,
        "location_id": "stonearch_bridge_far_end",
        "element": "lightning",
        "summon_secondary": "none",
        "lore": "The bridge was built to cross something. Nobody building it ever asked what was already crossing back.",
    },
    "the_buried_current": {
        "summon_damage_dice": "2d8", "summon_damage_bonus": 12,
        "name": "The Buried Current",
        "monster_key": "the_buried_current",
        "story_tied": True,
        "location_id": "sunken_root_caverns_forgotten_cistern",
        "element": "cold",
        "summon_secondary": "dot",
        "lore": "Water that stopped flowing so long ago it forgot it was ever supposed to, and started doing something else instead.",
    },
    "the_spires_grace": {
        "summon_damage_dice": "2d8", "summon_damage_bonus": 11,
        "name": "The Spire's Grace",
        "monster_key": "the_spires_grace",
        "story_tied": True,
        "location_id": "unmoored_isle_the_spires_grace_sanctum",
        "element": "radiant",
        "summon_secondary": "party_heal",
        # Relocated 2026-08-30 (Chapter 4 expansion, Phase 2, per the
        # approved plan): a radiant presence "facing the sunrise"
        # belongs naturally on a floating isle in the sky rather than
        # underground -- a better elemental fit than its old home, not
        # just a reshuffle. Stats completely unchanged; only the real
        # location and these two location-specific lines moved.
        "lore": "The First City's tallest spire was built facing the sunrise, reaching for something it could never actually touch from underground. Up here, above the clouds, it finally has.",
    },
    "the_archives_keeper": {
        "summon_damage_dice": "2d10", "summon_damage_bonus": 13,
        "name": "The Archive's Keeper",
        "monster_key": "the_archives_keeper",
        "story_tied": True,
        "location_id": "the_first_city_sunken_archive",
        "element": "psychic",
        "summon_secondary": "self_heal",
        "lore": "It has read everything ever written down here, in the correct order, and it still hasn't found the part that explains itself.",
    },
    "the_deepest_record": {
        "summon_damage_dice": "2d10", "summon_damage_bonus": 14,
        "name": "The Deepest Record",
        "monster_key": "the_deepest_record",
        "story_tied": False,
        "location_id": "the_first_city_deepest_record",
        "element": "force",
        "summon_secondary": "party_heal",
        "lore": "The last, lowest page of the First City's own memory. Whatever wrote it is still down here, still writing.",
    },
}


def get_remnant(remnant_id: str) -> dict | None:
    return REMNANTS.get(remnant_id)


def rumors_for_character(character: dict) -> list[tuple[str, dict]]:
    """
    Real, grounded "Whispers" for the Story So Far screen (2026-08-13,
    per Coffee): every story_tied Remnant whose real location this
    character has actually VISITED (visited_locations) but hasn't bound
    yet (bound_remnants) -- never a story_tied=False (pure secret)
    entry, and never a location the character hasn't genuinely been to.
    """
    visited = set(character.get("visited_locations") or [])
    bound = set(character.get("bound_remnants") or [])
    return [
        (remnant_id, data) for remnant_id, data in REMNANTS.items()
        if data.get("story_tied") and data["location_id"] in visited and remnant_id not in bound
    ]


def remnant_for_monster_key(monster_key: str) -> tuple[str, dict] | None:
    """Reverse lookup: given a defeated monster's key, find which Remnant (if any) it unlocks."""
    for remnant_id, data in REMNANTS.items():
        if data["monster_key"] == monster_key:
            return remnant_id, data
    return None


def find_remnant_mentioned_in_text(text: str, candidate_ids: list[str] | None = None) -> str | None:
    """Same 'name/id appears within a longer sentence' shape as items.find_item_mentioned_in_text."""
    lowered = text.lower()
    ids = candidate_ids if candidate_ids is not None else list(REMNANTS.keys())
    matches = []
    for remnant_id in ids:
        data = REMNANTS.get(remnant_id)
        if not data:
            continue
        name_lower = data["name"].lower()
        if name_lower in lowered or remnant_id.replace("_", " ") in lowered:
            matches.append((remnant_id, len(name_lower)))
    if not matches:
        return None
    return max(matches, key=lambda m: m[1])[0]
