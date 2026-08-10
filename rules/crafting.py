"""
rules/crafting.py
Deterministic crafting resolution — real material checks and a real
ability-check roll decide success, exactly like every other outcome in
rules/. The AI layer only narrates what happened here; it never invents
whether a craft attempt worked or what it produced.
"""
from rules.dice import roll_ability_check
from rules.item_generator import generate_item

# recipe_id -> {materials: {item_id: qty}, result_item: item_id,
# result_qty: int, ability: str, dc: int}. Materials are only consumed
# on a SUCCESSFUL craft, so a failed attempt can be retried without
# being punished twice (once by the failed roll, once by lost materials).
# "profession" (2026-07-24, per Coffee: expand named professions beyond
# gathering) splits crafting into its own separately-tracked, separately-
# leveled skills -- same real "the more you do it, the better you get"
# practiced_bonus mechanic gathering professions already use, just
# applied to crafting recipes instead of one shared "crafting" bucket.
# Defaults to "crafting" wherever unset, so nothing regresses.
RECIPES = {
    "healing_potion": {
        "materials": {"silverleaf_herb": 2, "moonpetal": 1},
        "result_item": "healing_potion", "result_qty": 1,
        "ability": "wisdom", "dc": 13, "profession": "alchemy",
    },
    "antitoxin": {
        "materials": {"sulfur_dust": 1, "silverleaf_herb": 1},
        "result_item": "antitoxin", "result_qty": 1,
        "ability": "intelligence", "dc": 13, "profession": "alchemy",
    },
    "scroll_magic_missile": {
        "materials": {"moonpetal": 2, "iron_ore": 1},
        "result_item": "scroll_magic_missile", "result_qty": 1,
        "ability": "intelligence", "dc": 15, "profession": "alchemy",
    },
    # Alchemy progression (2026-07-25): items.py's greater_healing_potion
    # already existed as a real, buyable item but had no recipe at all --
    # a genuinely harder brew (more of both herbs than the base potion)
    # gives alchemy its own top-tier recipe, same easy-to-hard curve
    # blacksmithing already has.
    "greater_healing_potion": {
        "materials": {"silverleaf_herb": 3, "moonpetal": 2},
        "result_item": "greater_healing_potion", "result_qty": 1,
        "ability": "wisdom", "dc": 17, "profession": "alchemy",
    },
    # Alchemy's scroll side (2026-07-25): items.py has 8 real scrolls but
    # only scroll_magic_missile was ever craftable. Scroll of Cure Wounds
    # is the other "common" rarity scroll (same tier/price bracket as
    # Magic Missile's), so it's the one that belongs here -- the rare
    # ones (Fireball, Revivify, Lightning Bolt, Invisibility, Summoning)
    # are deliberately left as real finds/purchases only, not craftable,
    # so scribing doesn't trivialize them.
    "scroll_cure_wounds": {
        "materials": {"silverleaf_herb": 2, "moonpetal": 2},
        "result_item": "scroll_cure_wounds", "result_qty": 1,
        "ability": "intelligence", "dc": 14, "profession": "alchemy",
    },
    # Advanced alchemy (2026-08-10, per Coffee: "make crafting Ethers
    # possible but difficult, shud be rare herbs to craft this. maybe
    # use advanced crafting recipes for this" -- the spell-slot tonics
    # (items.py, v1.27.130) are boss-drop/hidden-treasure-only otherwise
    # (see BOSS_SPELL_TONIC_DROP_CHANCE in bot.py), and this is the
    # THIRD path in, not a shortcut around the other two: every DC here
    # exceeds chain_mail's 18, the previous ceiling in this whole file,
    # and glimmerdeep_moss (items.py) only grows at one real gathering
    # node (The Glimmering Pool, campaign.json), a genuinely deep,
    # hard-to-reach location. Deliberately in RECIPES, not
    # ADVANCED_RECIPES below -- that system produces a procedurally
    # GENERATED item (rules/item_generator.py, random affixes at a fixed
    # tier), built for gear, which a fixed-effect catalog consumable
    # like a spell tonic doesn't fit; RECIPES' plain result_item/
    # result_qty shape is the correct one here, just pushed to a new,
    # genuinely harder DC ceiling ("advanced" as in difficulty, not as
    # in which dict it lives in). The top-tier Elixir of the Arcane
    # Circle is deliberately NOT craftable at all -- same "left as a
    # real find only" convention scroll_cure_wounds's own comment above
    # already documents for Fireball/Revivify/etc, so crafting can't
    # trivialize the single rarest item in the game.
    "spell_tonic": {
        "materials": {"moonpetal": 2, "glimmerdeep_moss": 1},
        "result_item": "spell_tonic", "result_qty": 1,
        "ability": "intelligence", "dc": 19, "profession": "alchemy",
    },
    "greater_spell_tonic": {
        "materials": {"moonpetal": 4, "glimmerdeep_moss": 2},
        "result_item": "greater_spell_tonic", "result_qty": 1,
        "ability": "intelligence", "dc": 22, "profession": "alchemy",
    },
    "supreme_spell_tonic": {
        "materials": {"moonpetal": 6, "glimmerdeep_moss": 4},
        "result_item": "supreme_spell_tonic", "result_qty": 1,
        "ability": "intelligence", "dc": 25, "profession": "alchemy",
    },
    # Cooking (2026-07-15): raw_fish was gatherable but no recipe used
    # it at all -- uses wood too (cooking over a fire), producing an
    # actually usable food item now that _do_use_item exists.
    "cooked_fish": {
        "materials": {"raw_fish": 1, "wood": 1},
        "result_item": "cooked_fish", "result_qty": 1,
        "ability": "wisdom", "dc": 10, "profession": "cooking",
    },
    # Cooking's easy end (2026-07-25): items.py's rations already
    # existed (a real buyable item, used for travel/downtime) but had no
    # recipe -- salting/drying fish into trail rations needs no fire,
    # just more of the raw catch, so it's the cheap/easy cooking recipe
    # cooked_fish's dc 10 didn't cover.
    "rations": {
        "materials": {"raw_fish": 2},
        "result_item": "rations", "result_qty": 1,
        "ability": "wisdom", "dc": 8, "profession": "cooking",
    },
    # Blacksmithing (2026-07-24): the first recipe that actually forges
    # a real weapon rather than a consumable, giving iron_ore (already
    # gatherable via mining) a second real use besides the scroll above.
    "longsword": {
        "materials": {"iron_ore": 3},
        "result_item": "longsword", "result_qty": 1,
        "ability": "strength", "dc": 13, "profession": "blacksmithing",
    },
    # Blacksmithing progression (2026-07-25): a real easy-to-hard curve
    # within one profession, same "the more you do it, the better you
    # get" practiced_bonus mechanic rewarding sticking with it -- an
    # early cheap piece, the existing mid-tier longsword above, and a
    # genuinely hard high-DC armor piece, all from the same iron_ore
    # mining already gathers.
    "rusty_dagger": {
        "materials": {"iron_ore": 1},
        "result_item": "rusty_dagger", "result_qty": 1,
        "ability": "strength", "dc": 8, "profession": "blacksmithing",
    },
    "chain_shirt": {
        "materials": {"iron_ore": 5},
        "result_item": "chain_shirt", "result_qty": 1,
        "ability": "strength", "dc": 16, "profession": "blacksmithing",
    },
    # Blacksmithing, filling in the rest of the real weapon/armor gaps
    # (2026-07-25): items.py's shortsword, wooden_shield, and chain_mail
    # all already existed as real, buyable items with no recipe. Rounds
    # the curve out to 6 real tiers: rusty_dagger(8) -> shortsword(10) /
    # wooden_shield(10) -> longsword(13) -> chain_shirt(16) ->
    # chain_mail(18, the real capstone).
    "shortsword": {
        "materials": {"iron_ore": 2},
        "result_item": "shortsword", "result_qty": 1,
        "ability": "strength", "dc": 10, "profession": "blacksmithing",
    },
    "wooden_shield": {
        "materials": {"wood": 3},
        "result_item": "wooden_shield", "result_qty": 1,
        "ability": "strength", "dc": 10, "profession": "blacksmithing",
    },
    "chain_mail": {
        "materials": {"iron_ore": 8},
        "result_item": "chain_mail", "result_qty": 1,
        "ability": "strength", "dc": 18, "profession": "blacksmithing",
    },
}


# Class profession affinity (2026-07-25, per Coffee: "make sure all
# classes and sub classes have a profession"). Every one of the 12
# classes gets a thematically-fitting "home" profession -- subclass
# choice (rules/leveling.py's CLASS_SUBCLASSES/WIZARD_SCHOOLS) never
# changes it, since every subclass is a refinement of one fixed base
# class, so mapping at the class level already covers every subclass
# for free. All 7 real professions (the 3 crafting ones above plus the
# 4 gathering ones -- herbalism/mining/fishing/lumberjacking) are used
# by at least one class, so nothing here is decorative filler.
CLASS_PROFESSIONS = {
    "Barbarian": "mining",         # breaks rock same as skulls
    "Sorcerer": "mining",          # raw, untamed power drawn like ore from the earth
    "Fighter": "blacksmithing",    # forges their own steel
    "Paladin": "blacksmithing",    # forges sacred arms
    "Rogue": "alchemy",            # poisons and a rogue's trade secrets
    "Wizard": "alchemy",           # arcane scholar, brews and scribes
    "Warlock": "fishing",          # patience, waiting on a bargain struck in still water
    "Monk": "fishing",             # patience and discipline
    "Ranger": "lumberjacking",     # woodsman, at home with an axe and the forest
    "Druid": "herbalism",          # living nature-magic
    "Cleric": "herbalism",         # tends healing herbs for the faithful
    "Bard": "cooking",             # innkeeper's trade, tavern life on the road
}
CLASS_PROFESSION_AFFINITY_BONUS = 2


def class_profession_affinity_bonus(char_class: str, profession: str) -> int:
    """Real +2 ability-check bonus when practicing your own class's home profession, on top of practiced_bonus."""
    return CLASS_PROFESSION_AFFINITY_BONUS if CLASS_PROFESSIONS.get(char_class) == profession else 0


def get_recipe(recipe_id: str) -> dict | None:
    return RECIPES.get(recipe_id)


def has_materials(inventory: dict, recipe: dict) -> bool:
    return all(inventory.get(item_id, 0) >= qty for item_id, qty in recipe["materials"].items())


def resolve_craft(character: dict, recipe_id: str, practiced_bonus: int = 0) -> dict:
    """
    Attempt to craft `recipe_id`. Returns a structured result dict —
    never raises for a missing-materials or failed-roll case, only for
    an unknown recipe_id (a programming error, not a player outcome).
    `practiced_bonus` (see rules/proficiency.py) is a real, earned bonus
    from repeated use of this ability — added to the roll here, in the
    rules layer, same as everything else that decides success.
    """
    recipe = RECIPES.get(recipe_id)
    if recipe is None:
        raise ValueError(f"Unknown recipe: {recipe_id}")

    if not has_materials(character["inventory"], recipe):
        missing = [
            item_id for item_id, qty in recipe["materials"].items()
            if character["inventory"].get(item_id, 0) < qty
        ]
        return {"outcome": "missing_materials", "missing": missing, "recipe_id": recipe_id}

    check = roll_ability_check(character, recipe["ability"], proficient=False)
    check["total"] += practiced_bonus
    check["practiced_bonus"] = practiced_bonus
    success = check["total"] >= recipe["dc"]

    return {
        "outcome": "success" if success else "failure",
        "recipe_id": recipe_id,
        "materials_consumed": recipe["materials"] if success else {},
        "result_item": recipe["result_item"] if success else None,
        "result_qty": recipe["result_qty"] if success else 0,
        "check": check,
        "dc": recipe["dc"],
        "ability": recipe["ability"],
    }


# Magic item system Phase 7 (2026-08-02): "will this work for crafting
# advanced items too? forging? enchanting? imbuing?" -- per Coffee's own
# question when scoping the whole system, the answer this phase proves
# is yes, by reusing every piece already built rather than inventing a
# parallel one. An advanced recipe produces a REAL generated item (via
# rules/item_generator.py, the exact same roll combat loot already uses)
# at a recipe-fixed tier, instead of a flat catalog item_id -- same
# material-check/ability-roll shape as RECIPES above, just a different
# result shape (generated_item, not result_item/result_qty).
ADVANCED_RECIPES = {
    "masterwork_longsword": {
        "materials": {"iron_ore": 6, "moonpetal": 1},
        "item_type": "weapon", "base_id": "longsword", "tier": "rare",
        "ability": "strength", "dc": 16, "profession": "blacksmithing",
        "name": "Masterwork Longsword",
    },
    "masterwork_greataxe": {
        "materials": {"iron_ore": 8, "moonpetal": 1},
        "item_type": "weapon", "base_id": "greataxe", "tier": "rare",
        "ability": "strength", "dc": 17, "profession": "blacksmithing",
        "name": "Masterwork Greataxe",
    },
    "runed_chain_shirt": {
        "materials": {"iron_ore": 7, "silverleaf_herb": 2},
        "item_type": "armor", "base_id": "chain_shirt", "tier": "rare",
        "ability": "strength", "dc": 17, "profession": "blacksmithing",
        "name": "Runed Chain Shirt",
    },
    "wardstone_shield": {
        "materials": {"iron_ore": 5, "silverleaf_herb": 1},
        "item_type": "shield", "base_id": "wooden_shield", "tier": "rare",
        "ability": "strength", "dc": 16, "profession": "blacksmithing",
        "name": "Wardstone Shield",
    },
}


def get_advanced_recipe(recipe_id: str) -> dict | None:
    return ADVANCED_RECIPES.get(recipe_id)


def resolve_advanced_craft(character: dict, recipe_id: str, practiced_bonus: int = 0) -> dict:
    """
    Same shape/convention as resolve_craft above (materials only consumed
    on success, practiced_bonus added to the roll here in the rules
    layer) -- the one difference is `generated_item`, a full rolled item
    dict from rules/item_generator.py at the recipe's fixed tier, instead
    of a static result_item/result_qty. The caller (bot.py) persists it
    via db.create_item_instance exactly like combat loot already does.
    """
    recipe = ADVANCED_RECIPES.get(recipe_id)
    if recipe is None:
        raise ValueError(f"Unknown advanced recipe: {recipe_id}")

    if not has_materials(character["inventory"], recipe):
        missing = [
            item_id for item_id, qty in recipe["materials"].items()
            if character["inventory"].get(item_id, 0) < qty
        ]
        return {"outcome": "missing_materials", "missing": missing, "recipe_id": recipe_id}

    check = roll_ability_check(character, recipe["ability"], proficient=False)
    check["total"] += practiced_bonus
    check["practiced_bonus"] = practiced_bonus
    success = check["total"] >= recipe["dc"]

    generated_item = None
    if success:
        generated_item = generate_item(
            item_type=recipe["item_type"], base_id=recipe.get("base_id"), tier=recipe["tier"],
        )

    return {
        "outcome": "success" if success else "failure",
        "recipe_id": recipe_id,
        "materials_consumed": recipe["materials"] if success else {},
        "generated_item": generated_item,
        "check": check,
        "dc": recipe["dc"],
        "ability": recipe["ability"],
    }


# Enchanting & imbuing (Phase 7): deliberately the SAME operation --
# append one new affix, from the exact same shared vocabulary
# db._apply_affix already understands, to an existing generated item's
# affix list. "Enchant" vs "imbue" is purely a player-facing/in-fiction
# distinction (which station/profession gates it), never a different
# data operation -- per the approved plan. Gated to the items each affix
# actually makes sense on (`applies_to`), same real-world logic as
# item_generator only ever offering weapons an elemental_damage affix
# and armor/shields a resistance one.
ENCHANT_RECIPES = {
    "enchant_flame": {
        "materials": {"sulfur_dust": 2, "moonpetal": 1},
        "affix": {"kind": "elemental_damage", "damage_type": "fire"},
        "applies_to": ("weapon",),
        "ability": "intelligence", "dc": 15, "profession": "alchemy",
    },
    "enchant_frost": {
        "materials": {"sulfur_dust": 1, "moonpetal": 2},
        "affix": {"kind": "elemental_damage", "damage_type": "cold"},
        "applies_to": ("weapon",),
        "ability": "intelligence", "dc": 15, "profession": "alchemy",
    },
    "enchant_warding": {
        "materials": {"iron_ore": 2, "silverleaf_herb": 2},
        "affix": {"kind": "resistance", "damage_type": "cold"},
        "applies_to": ("armor", "shield"),
        "ability": "intelligence", "dc": 15, "profession": "alchemy",
    },
    "enchant_arcana": {
        "materials": {"moonpetal": 3, "iron_ore": 1},
        "affix": {"kind": "grants_spell", "spell_id": "magic_missile", "uses": 2},
        "applies_to": ("weapon", "armor", "shield", "ring", "amulet", "wondrous"),
        "ability": "intelligence", "dc": 17, "profession": "alchemy",
    },
}


def get_enchant_recipe(recipe_id: str) -> dict | None:
    return ENCHANT_RECIPES.get(recipe_id)
