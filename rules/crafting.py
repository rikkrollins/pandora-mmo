"""
rules/crafting.py
Deterministic crafting resolution — real material checks and a real
ability-check roll decide success, exactly like every other outcome in
rules/. The AI layer only narrates what happened here; it never invents
whether a craft attempt worked or what it produced.
"""
from rules.dice import roll_ability_check

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
