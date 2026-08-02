"""
rules/item_generator.py
Procedurally generates tiered weapons and armor. Purely deterministic
dice-and-data, same as every other rules/ module — no AI involved in
deciding what an item IS or what stats it has; only the name flavor
comes from a curated word list, never invented at call time by a model.

Tiers (weighted, common -> legendary) roughly mirror standard 5E
rarity bands. Each tier adds a flat bonus to damage/AC and a price
multiplier, and picks from that tier's own name-flavor word lists.
"""
import random

from rules.dice import roll
from rules.item_sets import ITEM_SETS, SET_TAG_ELIGIBLE_TIERS, SET_TAG_CHANCE

# weapon_category/armor_category (task #223's proficiency system,
# 2026-07-21, caught before it caused a real bug): every generated item
# must carry the same real category fields items.py's own weapons/
# armor do, or a generated "legendary longsword" would silently fall
# back to _weapon_for_attacker's "simple" default -- wrong for a real
# martial weapon, and a real, if quiet, hole in the proficiency system
# for any item that ever came from this generator instead of the
# static catalog. Categories match items.py's own for the same base
# weapons exactly (dagger/shortsword simple, longsword/greataxe/longbow
# martial).
WEAPON_BASES = {
    "dagger": {"damage_dice": "1d4", "ability": "dexterity", "base_price": 2, "weapon_category": "simple"},
    "shortsword": {"damage_dice": "1d6", "ability": "dexterity", "base_price": 10, "weapon_category": "simple"},
    "longsword": {"damage_dice": "1d8", "ability": "strength", "base_price": 15, "weapon_category": "martial"},
    "greataxe": {"damage_dice": "1d12", "ability": "strength", "base_price": 30, "weapon_category": "martial"},
    "longbow": {"damage_dice": "1d8", "ability": "dexterity", "base_price": 50, "weapon_category": "martial"},
}

ARMOR_BASES = {
    "leather": {"ac_base": 11, "base_price": 10, "armor_category": "light"},
    "chain_shirt": {"ac_base": 13, "base_price": 50, "armor_category": "medium"},
    "chain_mail": {"ac_base": 16, "base_price": 75, "armor_category": "heavy"},
}

# Task, per Coffee (2026-07-21): "make sure there is weapons and
# armours for all proficiencies... make complete list of normal items
# then you can use the item generator to make magic ones." Shields
# were the one real gap -- items.py has exactly one (Wooden Shield),
# but this generator had no shield base or generate_shield() at all,
# so a magic/tiered shield could never actually be rolled.
SHIELD_BASES = {
    "wooden_shield": {"ac_bonus": 2, "base_price": 10, "armor_category": "shield"},
}

TIERS = ["common", "uncommon", "rare", "very_rare", "legendary", "mythic"]
# mythic (2026-08-02, magic item system Phase 6): ONE step above
# legendary, deliberately a MODEST numeric bump (+5, not +10 or +20) --
# per Coffee's own framing, the point of mythic is real new mechanical
# effects (ignore_resistance/free_extra_attack/damage_immunity below),
# never just a bigger flat number. Splits what used to be legendary's
# whole 2% slice in half with legendary (roll_tier below), so getting a
# legendary drop is now slightly MORE common than before, and mythic is
# the new rarest tier.
TIER_BONUS = {"common": 0, "uncommon": 1, "rare": 2, "very_rare": 3, "legendary": 4, "mythic": 5}
TIER_PRICE_MULT = {"common": 1, "uncommon": 4, "rare": 12, "very_rare": 30, "legendary": 80, "mythic": 250}

# Phase 2 of the magic item system (2026-08-02): real elemental damage
# types already in live use across this game (rules/combat.py's
# apply_damage_type_modifier, spells.py, items.py's static weapons like
# flametongue_shortsword) -- reused verbatim, never invented here.
# "physical" is deliberately excluded: it's the unmarked default every
# weapon already has, not something worth rolling as a bonus affix.
ELEMENTAL_DAMAGE_TYPES = [
    "fire", "cold", "lightning", "force", "radiant", "psychic", "poison", "necrotic", "silver",
]
# Only a rare+ item gets a shot at an elemental affix -- keeps it a real
# step up from a plain stat-bonus common/uncommon roll, not just noise on
# every drop.
ELEMENTAL_AFFIX_ELIGIBLE_TIERS = {"rare", "very_rare", "legendary", "mythic"}
ELEMENTAL_AFFIX_CHANCE = 0.3

PREFIXES = {
    "common": ["Sturdy", "Plain", "Worn", "Serviceable"],
    "uncommon": ["Gleaming", "Keen", "Tempered", "Well-Balanced"],
    "rare": ["Ashforged", "Stormwrought", "Runed", "Deepcut"],
    "very_rare": ["Emberbound", "Frostwoven", "Starforged", "Hollowlight"],
    "legendary": ["World-Ending", "Godsbane", "Undying", "Last-Dawn"],
    "mythic": ["Realitybreaking", "World-Splitting", "Godsforged", "Truthless"],
}
SUFFIXES = {
    "rare": ["of the Wolf", "of Embers", "of the Deep", "of Quiet Ruin"],
    "very_rare": ["of the Undying", "of the Tempest", "of the First Flame", "of the Hollow Choir"],
    "legendary": ["of the World's End", "of the Last Dawn", "of Forgotten Kings", "of the Unmoored Isle"],
    "mythic": ["of the Unwritten Law", "of the Broken Pantheon", "that Should Not Be", "of the Last Rebirth"],
}


def roll_tier() -> str:
    """1d100, weighted toward common — a normal loot table's feel, not a coin flip between tiers."""
    r = roll(1, 100)[0]
    if r <= 50:
        return "common"
    if r <= 75:
        return "uncommon"
    if r <= 90:
        return "rare"
    if r <= 98:
        return "very_rare"
    if r <= 99:
        return "legendary"
    return "mythic"


def _name_for(base_label: str, tier: str) -> str:
    prefix = random.choice(PREFIXES[tier])
    name = f"{prefix} {base_label}"
    if tier in SUFFIXES:
        name += f" {random.choice(SUFFIXES[tier])}"
    return name


def _maybe_elemental_affix(tier: str, kind: str) -> list[dict]:
    """
    A rare+ roll has a real (not guaranteed) chance at one elemental
    affix -- `kind="elemental_damage"` for a weapon (offense),
    `kind="resistance"` for armor/shield (defense). Empty list otherwise,
    same "omit rather than roll a zero-effect affix" convention the
    stat_bonus affix already uses at common tier.
    """
    if tier not in ELEMENTAL_AFFIX_ELIGIBLE_TIERS or random.random() > ELEMENTAL_AFFIX_CHANCE:
        return []
    return [{"kind": kind, "damage_type": random.choice(ELEMENTAL_DAMAGE_TYPES)}]


def _maybe_set_id(tier: str) -> str | None:
    """
    Magic item system Phase 5 (2026-08-02): a rare+ roll has a real
    (not guaranteed) chance to be tagged as a piece of one of the small,
    hand-authored named sets in rules/item_sets.py -- per Coffee's
    explicit decision, sets are curated content, never randomly
    assembled, so this only ever picks an EXISTING set_id, never
    invents one.
    """
    if tier not in SET_TAG_ELIGIBLE_TIERS or random.random() > SET_TAG_CHANCE or not ITEM_SETS:
        return None
    return random.choice(list(ITEM_SETS.keys()))


# Real progression gate for every generated mythic item (Phase 6,
# 2026-08-02): met by ANY ONE of a rebirth, a maxed echo trial tier, or
# beating the hidden superboss quest -- the reward loop that lets real
# progression unlock the gear that then unlocks harder content, per
# Coffee's own framing, rather than pure drop luck alone deciding who
# gets to use it. Values mirror bot.py's real ECHO_TRIAL_MAX_TIER (10)
# and the confirmed hidden-superboss quest id
# ("the_unaskeds_reckoning") -- duplicated here rather than imported,
# since rules/ modules stay free of any bot.py dependency by
# convention; if either of those ever changes, this needs updating too.
MYTHIC_EQUIP_REQUIREMENT = {
    "any_of": [
        {"kind": "rebirth_count", "value": 1},
        {"kind": "echo_trial_tier", "value": 10},
        {"kind": "completed_quest", "value": "the_unaskeds_reckoning"},
    ]
}


def _mythic_affix(item_type: str) -> dict:
    """
    Every mythic roll GUARANTEES exactly one mythic-exclusive effect --
    per Coffee's own framing, the point of the tier is real new
    mechanical effects, not a coin-flip chance at one on top of an
    already-rare drop. Weapons get ignore_resistance or
    free_extra_attack (50/50); armor/shield get damage_immunity (reuses
    Phase 2's existing "immunity" affix kind directly -- no new kind
    needed, just guaranteed here instead of a rare+ chance).
    """
    if item_type == "weapon":
        return random.choice([{"kind": "ignore_resistance"}, {"kind": "free_extra_attack"}])
    return {"kind": "immunity", "damage_type": random.choice(ELEMENTAL_DAMAGE_TYPES)}


def generate_weapon(base_id: str | None = None, tier: str | None = None) -> dict:
    """
    Returns a fully-formed item dict in the same shape as items.py's
    ITEMS entries — safe to hand straight to db.create_item_instance
    (base_stats=this dict minus "affixes", affixes=this dict's "affixes"),
    or to merge into a shop/loot table.

    2026-08-02 magic item system refactor: the tier bonus used to be
    BAKED directly into damage_dice ("1d8+4") -- that's exactly the
    "single flat monolithic stat baked in at generation time" shape the
    real per-instance item architecture rules out (see the plan's
    Context section: base item + a list of independently-attachable
    affixes, so enchanting/forging/imbuing can all reuse this same
    system later by appending one more affix, never bespoke merge logic).
    damage_dice now stays the pure base value; the bonus comes back as a
    separate "affixes" list instead -- which also matches how combat
    actually consumes it better than the old code did:
    _weapon_for_attacker/resolve_attack already read damage_bonus as a
    field SEPARATE from damage_dice (rules/combat.py's roll_damage
    modifier=weapon.get("damage_bonus", 0) + ...), never as dice-string
    concatenation.
    """
    base_id = base_id or random.choice(list(WEAPON_BASES.keys()))
    base = WEAPON_BASES[base_id]
    tier = tier or roll_tier()
    bonus = TIER_BONUS[tier]
    name = _name_for(base_id.replace("_", " ").title(), tier)

    return {
        "name": name,
        "type": "weapon",
        "rarity": tier,
        "price": base["base_price"] * TIER_PRICE_MULT[tier],
        "weight": 2,
        "damage_dice": base["damage_dice"],
        "ability": base["ability"],
        "weapon_category": base["weapon_category"],
        "generated": True,
        "generated_base": base_id,
        "set_id": _maybe_set_id(tier),
        "equip_requirement": MYTHIC_EQUIP_REQUIREMENT if tier == "mythic" else None,
        "note": f"A {tier.replace('_', ' ')} find, worked with more care than most of its kind.",
        "affixes": (
            ([{"kind": "stat_bonus", "field": "damage_bonus", "value": bonus}] if bonus else [])
            + _maybe_elemental_affix(tier, "elemental_damage")
            + ([_mythic_affix("weapon")] if tier == "mythic" else [])
        ),
    }


def generate_armor(base_id: str | None = None, tier: str | None = None) -> dict:
    """See generate_weapon's docstring for why the tier bonus is now an affix, not baked into ac_base."""
    base_id = base_id or random.choice(list(ARMOR_BASES.keys()))
    base = ARMOR_BASES[base_id]
    tier = tier or roll_tier()
    bonus = TIER_BONUS[tier]
    name = _name_for(base_id.replace("_", " ").title() + " Armor", tier)

    return {
        "name": name,
        "type": "armor",
        "rarity": tier,
        "price": base["base_price"] * TIER_PRICE_MULT[tier],
        "weight": 15,
        "ac_base": base["ac_base"],
        "armor_category": base["armor_category"],
        "generated": True,
        "generated_base": base_id,
        "set_id": _maybe_set_id(tier),
        "equip_requirement": MYTHIC_EQUIP_REQUIREMENT if tier == "mythic" else None,
        "note": f"A {tier.replace('_', ' ')} find, worked with more care than most of its kind.",
        "affixes": (
            ([{"kind": "stat_bonus", "field": "ac_base", "value": bonus}] if bonus else [])
            + _maybe_elemental_affix(tier, "resistance")
            + ([_mythic_affix("armor")] if tier == "mythic" else [])
        ),
    }


def generate_shield(base_id: str | None = None, tier: str | None = None) -> dict:
    """See generate_weapon's docstring for why the tier bonus is now an affix, not baked into ac_bonus."""
    base_id = base_id or random.choice(list(SHIELD_BASES.keys()))
    base = SHIELD_BASES[base_id]
    tier = tier or roll_tier()
    bonus = TIER_BONUS[tier]
    name = _name_for(base_id.replace("_", " ").title(), tier)

    return {
        "name": name,
        "type": "shield",
        "rarity": tier,
        "price": base["base_price"] * TIER_PRICE_MULT[tier],
        "weight": 6,
        "ac_bonus": base["ac_bonus"],
        "armor_category": base["armor_category"],
        "generated": True,
        "generated_base": base_id,
        "set_id": _maybe_set_id(tier),
        "equip_requirement": MYTHIC_EQUIP_REQUIREMENT if tier == "mythic" else None,
        "note": f"A {tier.replace('_', ' ')} find, worked with more care than most of its kind.",
        "affixes": (
            ([{"kind": "stat_bonus", "field": "ac_bonus", "value": bonus}] if bonus else [])
            + _maybe_elemental_affix(tier, "resistance")
            + ([_mythic_affix("shield")] if tier == "mythic" else [])
        ),
    }


def generate_item(item_type: str = "weapon", base_id: str | None = None, tier: str | None = None) -> dict:
    """item_type is 'weapon', 'armor', or 'shield'. base_id/tier are optional — omit either to roll it."""
    if item_type == "armor":
        return generate_armor(base_id=base_id, tier=tier)
    if item_type == "shield":
        return generate_shield(base_id=base_id, tier=tier)
    return generate_weapon(base_id=base_id, tier=tier)
