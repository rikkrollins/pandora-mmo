"""
items.py
The master item catalog. Every item players can carry, buy, sell, or
find is defined here as data — adding a new item never requires
touching bot.py or the rules engine.

Item categories: weapon, armor, shield, consumable, scroll, ring,
amulet, wondrous, quest_item, material.
"""

ITEMS = {
    # --- Weapons ---
    "rusty_dagger": {
        "name": "Rusty Dagger", "type": "weapon", "rarity": "common",
        "price": 2, "weight": 1, "damage_dice": "1d4", "ability": "dexterity",
    },
    "shortsword": {
        "name": "Shortsword", "type": "weapon", "rarity": "common",
        "price": 10, "weight": 2, "damage_dice": "1d6", "ability": "dexterity",
    },
    "longsword": {
        "name": "Longsword", "type": "weapon", "rarity": "common",
        "price": 15, "weight": 3, "damage_dice": "1d8", "ability": "strength",
    },
    "greataxe": {
        "name": "Greataxe", "type": "weapon", "rarity": "common",
        "price": 30, "weight": 7, "damage_dice": "1d12", "ability": "strength",
    },
    "longbow": {
        "name": "Longbow", "type": "weapon", "rarity": "common",
        "price": 50, "weight": 2, "damage_dice": "1d8", "ability": "dexterity",
    },
    "silvered_dagger": {
        "name": "Silvered Dagger", "type": "weapon", "rarity": "uncommon",
        "price": 75, "weight": 1, "damage_dice": "1d4+1", "ability": "dexterity",
        "note": "Effective against creatures vulnerable to silver.",
    },
    "flametongue_shortsword": {
        "name": "Flametongue Shortsword", "type": "weapon", "rarity": "rare",
        "price": 0, "weight": 2, "damage_dice": "1d6+2", "ability": "dexterity",
        "note": "Warm to the touch. Wreathes itself in fire when drawn in anger.",
    },

    # --- Armor & Shields ---
    "leather_armor": {"name": "Leather Armor", "type": "armor", "rarity": "common", "price": 10, "weight": 10, "ac_base": 11},
    "chain_shirt": {"name": "Chain Shirt", "type": "armor", "rarity": "common", "price": 50, "weight": 20, "ac_base": 13},
    "chain_mail": {"name": "Chain Mail", "type": "armor", "rarity": "uncommon", "price": 75, "weight": 55, "ac_base": 16},
    "wooden_shield": {"name": "Wooden Shield", "type": "shield", "rarity": "common", "price": 10, "weight": 6, "ac_bonus": 2},

    # --- Consumables ---
    "healing_potion": {
        "name": "Healing Potion", "type": "consumable", "rarity": "common",
        "price": 25, "weight": 0.5, "effect": "heal", "heal_dice": "2d4+2",
    },
    "greater_healing_potion": {
        "name": "Greater Healing Potion", "type": "consumable", "rarity": "uncommon",
        "price": 100, "weight": 0.5, "effect": "heal", "heal_dice": "4d4+4",
    },
    "antitoxin": {
        "name": "Antitoxin", "type": "consumable", "rarity": "common",
        "price": 15, "weight": 0.1, "effect": "cure_poison",
    },
    "rations": {
        "name": "Rations (1 day)", "type": "consumable", "rarity": "common",
        "price": 2, "weight": 2, "effect": "none",
    },
    "torch": {"name": "Torch", "type": "consumable", "rarity": "common", "price": 1, "weight": 1, "effect": "light"},
    "ale": {
        "name": "Mug of Ale", "type": "consumable", "rarity": "common",
        "price": 1, "weight": 1, "effect": "none",
        "note": "Grimsby's own brew. Doesn't heal anything, but it isn't meant to.",
    },

    # --- Scrolls (single-use spells for non-casters — or anyone, once bought) ---
    "scroll_magic_missile": {
        "name": "Scroll of Magic Missile", "type": "scroll", "rarity": "common",
        "price": 30, "weight": 0.1, "spell": "magic_missile",
    },
    "scroll_fireball": {
        "name": "Scroll of Fireball", "type": "scroll", "rarity": "rare",
        "price": 300, "weight": 0.1, "spell": "fireball",
    },
    "scroll_cure_wounds": {
        "name": "Scroll of Cure Wounds", "type": "scroll", "rarity": "common",
        "price": 35, "weight": 0.1, "spell": "cure_wounds",
    },
    "scroll_revivify": {
        "name": "Scroll of Revivify", "type": "scroll", "rarity": "rare",
        "price": 350, "weight": 0.1, "spell": "revivify",
        "note": "Brings a fallen ally back from death itself, at real cost — not to be used lightly.",
    },
    "scroll_shield": {
        "name": "Scroll of Shield", "type": "scroll", "rarity": "uncommon",
        "price": 60, "weight": 0.1, "spell": "shield",
    },
    "scroll_bless": {
        "name": "Scroll of Bless", "type": "scroll", "rarity": "uncommon",
        "price": 55, "weight": 0.1, "spell": "bless",
    },
    "scroll_invisibility": {
        "name": "Scroll of Invisibility", "type": "scroll", "rarity": "rare",
        "price": 175, "weight": 0.1, "spell": "invisibility",
    },
    "scroll_lightning_bolt": {
        "name": "Scroll of Lightning Bolt", "type": "scroll", "rarity": "rare",
        "price": 275, "weight": 0.1, "spell": "lightning_bolt",
    },
    "scroll_summon_spirit": {
        "name": "Scroll of Summoning", "type": "scroll", "rarity": "rare",
        "price": 200, "weight": 0.1, "spell": "summon_lesser_spirit",
    },

    # --- Rings, Amulets, Wondrous Items ---
    "ring_of_protection": {
        "name": "Ring of Protection", "type": "ring", "rarity": "rare",
        "price": 0, "weight": 0, "ac_bonus": 1,
        "note": "A faint shimmer surrounds it.",
    },
    "amulet_of_health": {
        "name": "Amulet of Health", "type": "amulet", "rarity": "rare",
        "price": 0, "weight": 0, "constitution_set": 19,
        "note": "Your vitality feels different the moment you put it on.",
    },
    "cloak_of_elvenkind": {
        "name": "Cloak of Elvenkind", "type": "wondrous", "rarity": "uncommon",
        "price": 0, "weight": 1, "stealth_advantage": True,
    },
    "boots_of_the_winterlands": {
        "name": "Boots of the Winterlands", "type": "wondrous", "rarity": "uncommon",
        "price": 0, "weight": 1, "note": "Cold never seems to trouble the wearer.",
    },
    "bracers_of_the_steady_hand": {
        "name": "Bracers of the Steady Hand", "type": "wondrous", "rarity": "uncommon",
        "price": 90, "weight": 1, "note": "Your hands never shake, even when the rest of you wants to.",
    },
    "lantern_of_true_sight": {
        "name": "Lantern of True Sight", "type": "wondrous", "rarity": "rare",
        "price": 220, "weight": 2, "note": "Its flame burns a color no ordinary fire does, and shows things exactly as they are, not as they'd rather look.",
    },
    "ring_of_the_undertow": {
        "name": "Ring of the Undertow", "type": "ring", "rarity": "uncommon",
        "price": 110, "weight": 0, "ac_bonus": 1,
        "note": "Cold as riverwater no matter how long it's worn.",
    },

    # --- Quest items (never sellable, never have a price) ---
    "waterlogged_journal": {
        "name": "Waterlogged Journal", "type": "quest_item", "rarity": "unique",
        "price": 0, "weight": 0.5,
    },
    "shard_of_dim_light": {
        "name": "Shard of Dim Light", "type": "quest_item", "rarity": "unique",
        "price": 0, "weight": 0.1,
    },
    "brass_key_no_lock": {
        "name": "Brass Key That Fits No Lock", "type": "quest_item", "rarity": "unique",
        "price": 0, "weight": 0.1,
    },

    # --- Crafting / trade materials ---
    "iron_ore": {"name": "Iron Ore", "type": "material", "rarity": "common", "price": 5, "weight": 2},
    "moonpetal": {"name": "Moonpetal Flower", "type": "material", "rarity": "uncommon", "price": 20, "weight": 0.05},
    "silverleaf_herb": {"name": "Silverleaf Herb", "type": "material", "rarity": "common", "price": 4, "weight": 0.1},
    "sulfur_dust": {"name": "Sulfur Dust", "type": "material", "rarity": "common", "price": 6, "weight": 0.1},
    "raw_fish": {"name": "Raw Fish", "type": "material", "rarity": "common", "price": 3, "weight": 0.3},
    "wood": {"name": "Wood", "type": "material", "rarity": "common", "price": 2, "weight": 1},

    # --- Cooking (2026-07-15: raw_fish was gatherable but had zero
    # recipe using it -- a real cooking recipe below turns it, plus
    # wood for the fire, into an actually usable food item) ---
    "cooked_fish": {
        "name": "Cooked Fish", "type": "consumable", "rarity": "common",
        "price": 8, "weight": 0.3, "effect": "heal", "heal_dice": "1d4+1",
    },

    # --- Gathering tools (2026-07-16, per Coffee): fishing/lumberjacking/
    # mining each require owning the real tool for that trade -- herbalism
    # deliberately doesn't (picking a plant needs no tool), but Shears let
    # an herbalist harvest more than one plant per success. "required_for"
    # is the gathering skill this tool gates (see bot.py's _do_gather);
    # Shears instead uses "boosts_quantity_for" since it's optional, not
    # gating access at all.
    "fishing_pole": {"name": "Fishing Pole", "type": "tool", "rarity": "common", "price": 8, "weight": 2, "required_for": "fishing"},
    "bait": {"name": "Bait", "type": "tool", "rarity": "common", "price": 2, "weight": 0.1, "required_for": "fishing"},
    "woodcutters_axe": {"name": "Woodcutter's Axe", "type": "tool", "rarity": "common", "price": 10, "weight": 4, "required_for": "lumberjacking"},
    "pickaxe": {"name": "Pickaxe", "type": "tool", "rarity": "common", "price": 10, "weight": 5, "required_for": "mining"},
    "shears": {"name": "Shears", "type": "tool", "rarity": "common", "price": 6, "weight": 0.5, "boosts_quantity_for": "herbalism"},
}


def get_item(item_id: str) -> dict | None:
    return ITEMS.get(item_id)


def is_sellable(item_id: str) -> bool:
    item = ITEMS.get(item_id)
    if item is None:
        return False
    return item["type"] != "quest_item" and item.get("price", 0) > 0


def find_item_id_by_name(name_fragment: str) -> str | None:
    """Fuzzy-ish lookup: match on item_id or display name, case-insensitively."""
    lowered = name_fragment.strip().lower()
    for item_id, data in ITEMS.items():
        if lowered == item_id.lower() or lowered == data["name"].lower():
            return item_id
    for item_id, data in ITEMS.items():
        if lowered in item_id.lower() or lowered in data["name"].lower():
            return item_id
    return None


def find_item_mentioned_in_text(text: str, candidate_ids: list[str] | None = None) -> str | None:
    """
    Search for any item's name or id mentioned WITHIN a longer sentence
    (e.g. "I want to buy a healing potion" -> "healing_potion"). Unlike
    find_item_id_by_name, this checks whether the item name appears
    inside the text, not the other way around. If candidate_ids is
    given, only those items are considered (e.g. a shop's stock list).
    """
    lowered = text.strip().lower()
    search_space = candidate_ids if candidate_ids is not None else list(ITEMS.keys())
    # Check longer names first so "greater healing potion" doesn't get
    # shadowed by a shorter partial match like "healing potion".
    ordered = sorted(search_space, key=lambda i: -len(ITEMS[i]["name"]))
    for item_id in ordered:
        data = ITEMS[item_id]
        if data["name"].lower() in lowered or item_id.replace("_", " ") in lowered:
            return item_id

    # Fall back to a generic category word (e.g. "potions" for "Healing
    # Potion") when it unambiguously picks out exactly one candidate --
    # confirmed live 2026-07-12 via a real screenshot: "buy two potions
    # from Grimsby" never says the full item name, just the general
    # kind, and this shop only sells one real potion. Word length >= 4
    # guards against trivial words ("of", "the") in longer item names
    # causing false matches.
    matches = set()
    for item_id in search_space:
        for word in ITEMS[item_id]["name"].lower().split():
            singular = word[:-1] if word.endswith("s") else word
            if len(singular) >= 4 and (singular in lowered or f"{singular}s" in lowered):
                matches.add(item_id)
                break
    if len(matches) == 1:
        return matches.pop()
    return None
