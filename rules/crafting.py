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
    # Cooking (2026-07-15): raw_fish was gatherable but no recipe used
    # it at all -- uses wood too (cooking over a fire), producing an
    # actually usable food item now that _do_use_item exists.
    "cooked_fish": {
        "materials": {"raw_fish": 1, "wood": 1},
        "result_item": "cooked_fish", "result_qty": 1,
        "ability": "wisdom", "dc": 10, "profession": "cooking",
    },
    # Blacksmithing (2026-07-24): the first recipe that actually forges
    # a real weapon rather than a consumable, giving iron_ore (already
    # gatherable via mining) a second real use besides the scroll above.
    "longsword": {
        "materials": {"iron_ore": 3},
        "result_item": "longsword", "result_qty": 1,
        "ability": "strength", "dc": 13, "profession": "blacksmithing",
    },
}


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
