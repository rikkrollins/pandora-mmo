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

Defeating an Unbound doesn't destroy it -- it lets the party bind a
real fragment of its essence. Once bound, any party member the party
DESIGNATES as its Summoner (bot._do_assign_summoner) can call that
fragment into a later fight as a Bound Remnant: a themed attack, not a
second combatant, following the same real damage-type pipeline
(rules.combat.apply_damage_type_modifier) every other attack in this
game already uses -- never an invented mechanic bolted on the side.

This ties directly into real, already-existing content, not invented
from nothing: campaigns/default/campaign.json's "choir_remnant"
monster, "The Remnant's Echo" quest, and the Echo-Bound Signet item
("Whatever it echoes back was never quite what you said") already
established this exact "echo/remnant/bound" vocabulary before this
system existed.
"""

# Every element used below is one of this game's own real, already-
# implemented damage types (rules/combat.py's apply_damage_type_
# modifier) -- never an invented one.
REAL_DAMAGE_TYPES = ("cold", "fire", "force", "lightning", "necrotic", "physical", "poison", "psychic", "radiant")

# Real secondary summon effects (2026-08-13) -- deliberately a small,
# fully-implemented set (see bot._do_summon_remnant) rather than one
# bespoke mechanic per Remnant, the same "data-driven, not code-per-
# entry" shape every other catalog in this game already uses
# (items.py, spells.py, guilds.py). "none" is a real, valid choice --
# some Remnants are just the hardest-hitting option, no secondary.
SUMMON_SECONDARY_EFFECTS = ("none", "dot", "self_heal", "party_heal")

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
        "location_id": "hollow_stump_shrine",
        "element": "fire",
        "summon_secondary": "none",
        "lore": "Not anger given shape -- anger that WAS a shape, once, before the Box, and hasn't forgiven the world for forgetting that.",
    },
    "the_drowned_choir": {
        "summon_damage_dice": "2d8", "summon_damage_bonus": 12,
        "name": "The Drowned Choir",
        "monster_key": "the_drowned_choir",
        "location_id": "the_weeping_well",
        "element": "poison",
        "summon_secondary": "dot",
        "lore": "It doesn't sing so much as it keeps singing, long after anyone still listening should have stopped being able to.",
    },
    "the_root_that_remembers": {
        "summon_damage_dice": "2d8", "summon_damage_bonus": 12,
        "name": "The Root That Remembers",
        "monster_key": "the_root_that_remembers",
        "location_id": "whispering_wood_deep_glade",
        "element": "poison",
        "summon_secondary": "dot",
        "lore": "Every forest has one root older than the forest. This one remembers what was planted here before there was a forest at all.",
    },
    "the_hollow_bell": {
        "summon_damage_dice": "3d8", "summon_damage_bonus": 16,
        "name": "The Hollow Bell",
        "monster_key": "the_hollow_bell",
        "location_id": "whispering_wood_root_hollow",
        "element": "psychic",
        "summon_secondary": "none",
        "lore": "It rang once, the day the Box opened, and the sound never actually finished arriving -- it's still landing, somewhere, on someone.",
    },
    "the_cairnbound": {
        "summon_damage_dice": "2d10", "summon_damage_bonus": 13,
        "name": "The Cairnbound",
        "monster_key": "the_cairnbound",
        "location_id": "greymoor_downs_lonely_cairn",
        "element": "necrotic",
        "summon_secondary": "self_heal",
        "lore": "Buried under stones piled by hands that all had the same reason, and none of them wrote it down.",
    },
    "the_waiting_dark": {
        "summon_damage_dice": "2d10", "summon_damage_bonus": 13,
        "name": "The Waiting Dark",
        "monster_key": "the_waiting_dark",
        "location_id": "greymoor_downs_below_the_cairn",
        "element": "necrotic",
        "summon_secondary": "self_heal",
        "lore": "Beneath the cairn is older than the cairn. It has been very patient, and it is not tired of waiting yet.",
    },
    "the_farthest_span": {
        "summon_damage_dice": "3d8", "summon_damage_bonus": 17,
        "name": "The Farthest Span",
        "monster_key": "the_farthest_span",
        "location_id": "stonearch_bridge_far_end",
        "element": "lightning",
        "summon_secondary": "none",
        "lore": "The bridge was built to cross something. Nobody building it ever asked what was already crossing back.",
    },
    "the_buried_current": {
        "summon_damage_dice": "2d8", "summon_damage_bonus": 12,
        "name": "The Buried Current",
        "monster_key": "the_buried_current",
        "location_id": "sunken_root_caverns_forgotten_cistern",
        "element": "cold",
        "summon_secondary": "dot",
        "lore": "Water that stopped flowing so long ago it forgot it was ever supposed to, and started doing something else instead.",
    },
    "the_spires_grace": {
        "summon_damage_dice": "2d8", "summon_damage_bonus": 11,
        "name": "The Spire's Grace",
        "monster_key": "the_spires_grace",
        "location_id": "the_first_city_spire_reaches",
        "element": "radiant",
        "summon_secondary": "party_heal",
        "lore": "The First City's tallest spire was built facing the sunrise. Something up there still believes that's what it's for.",
    },
    "the_archives_keeper": {
        "summon_damage_dice": "2d10", "summon_damage_bonus": 13,
        "name": "The Archive's Keeper",
        "monster_key": "the_archives_keeper",
        "location_id": "the_first_city_sunken_archive",
        "element": "psychic",
        "summon_secondary": "self_heal",
        "lore": "It has read everything ever written down here, in the correct order, and it still hasn't found the part that explains itself.",
    },
    "the_deepest_record": {
        "summon_damage_dice": "2d10", "summon_damage_bonus": 14,
        "name": "The Deepest Record",
        "monster_key": "the_deepest_record",
        "location_id": "the_first_city_deepest_record",
        "element": "force",
        "summon_secondary": "party_heal",
        "lore": "The last, lowest page of the First City's own memory. Whatever wrote it is still down here, still writing.",
    },
}


def get_remnant(remnant_id: str) -> dict | None:
    return REMNANTS.get(remnant_id)


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
