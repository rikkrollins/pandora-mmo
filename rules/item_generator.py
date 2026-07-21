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

TIERS = ["common", "uncommon", "rare", "very_rare", "legendary"]
TIER_BONUS = {"common": 0, "uncommon": 1, "rare": 2, "very_rare": 3, "legendary": 4}
TIER_PRICE_MULT = {"common": 1, "uncommon": 4, "rare": 12, "very_rare": 30, "legendary": 80}

PREFIXES = {
    "common": ["Sturdy", "Plain", "Worn", "Serviceable"],
    "uncommon": ["Gleaming", "Keen", "Tempered", "Well-Balanced"],
    "rare": ["Ashforged", "Stormwrought", "Runed", "Deepcut"],
    "very_rare": ["Emberbound", "Frostwoven", "Starforged", "Hollowlight"],
    "legendary": ["World-Ending", "Godsbane", "Undying", "Last-Dawn"],
}
SUFFIXES = {
    "rare": ["of the Wolf", "of Embers", "of the Deep", "of Quiet Ruin"],
    "very_rare": ["of the Undying", "of the Tempest", "of the First Flame", "of the Hollow Choir"],
    "legendary": ["of the World's End", "of the Last Dawn", "of Forgotten Kings", "of the Unmoored Isle"],
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
    return "legendary"


def _name_for(base_label: str, tier: str) -> str:
    prefix = random.choice(PREFIXES[tier])
    name = f"{prefix} {base_label}"
    if tier in SUFFIXES:
        name += f" {random.choice(SUFFIXES[tier])}"
    return name


def generate_weapon(base_id: str | None = None, tier: str | None = None) -> dict:
    """
    Returns a fully-formed item dict in the same shape as items.py's
    ITEMS entries — safe to hand straight to db.add_item alongside a
    synthetic item_id, or to merge into a shop/loot table.
    """
    base_id = base_id or random.choice(list(WEAPON_BASES.keys()))
    base = WEAPON_BASES[base_id]
    tier = tier or roll_tier()
    bonus = TIER_BONUS[tier]

    damage_dice = f"{base['damage_dice']}+{bonus}" if bonus else base["damage_dice"]
    name = _name_for(base_id.replace("_", " ").title(), tier)

    return {
        "name": name,
        "type": "weapon",
        "rarity": tier,
        "price": base["base_price"] * TIER_PRICE_MULT[tier],
        "weight": 2,
        "damage_dice": damage_dice,
        "ability": base["ability"],
        "weapon_category": base["weapon_category"],
        "generated": True,
        "generated_base": base_id,
        "note": f"A {tier.replace('_', ' ')} find, worked with more care than most of its kind.",
    }


def generate_armor(base_id: str | None = None, tier: str | None = None) -> dict:
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
        "ac_base": base["ac_base"] + bonus,
        "armor_category": base["armor_category"],
        "generated": True,
        "generated_base": base_id,
        "note": f"A {tier.replace('_', ' ')} find, worked with more care than most of its kind.",
    }


def generate_shield(base_id: str | None = None, tier: str | None = None) -> dict:
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
        "ac_bonus": base["ac_bonus"] + bonus,
        "armor_category": base["armor_category"],
        "generated": True,
        "generated_base": base_id,
        "note": f"A {tier.replace('_', ' ')} find, worked with more care than most of its kind.",
    }


def generate_item(item_type: str = "weapon", base_id: str | None = None, tier: str | None = None) -> dict:
    """item_type is 'weapon', 'armor', or 'shield'. base_id/tier are optional — omit either to roll it."""
    if item_type == "armor":
        return generate_armor(base_id=base_id, tier=tier)
    if item_type == "shield":
        return generate_shield(base_id=base_id, tier=tier)
    return generate_weapon(base_id=base_id, tier=tier)
