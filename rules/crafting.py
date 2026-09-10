"""
rules/crafting.py
Deterministic crafting resolution — real material checks and a real
ability-check roll decide success, exactly like every other outcome in
rules/. The AI layer only narrates what happened here; it never invents
whether a craft attempt worked or what it produced.
"""
from rules.dice import roll_ability_check
from rules.item_generator import generate_item, TIERS

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
    # Tier-2 "Greater" elemental scrolls, real craft path (2026-09-08,
    # per Coffee, standalone follow-up to task #3: "dont have tier 2
    # scrolls in shops, they must be crafted by a lv 20 +"). Additive to
    # the existing find/steal/loot channels (rules/labyrinth.py's
    # checkpoint vault, bot.py's miniboss drop and steal-fallback), not
    # a replacement -- still genuinely never sold in any shop. min_level
    # (recipe_requirement_gate's own new field, added for task #6's
    # forge_magic_upgrade) is the real gate here; `_do_craft` already
    # calls recipe_requirement_gate for every plain RECIPES entry
    # (unconditionally, same call as ADVANCED_RECIPES), so this needed
    # no new plumbing at all, just these 9 data entries. Same DC/
    # material shape as supreme_spell_tonic just above -- the previous
    # real ceiling in this whole file -- since these are meant to be
    # genuinely the hardest thing an alchemist can scribe.
    "greater_scroll_fire": {
        "materials": {"glimmerdeep_moss": 3, "moonpetal": 3, "sulfur_dust": 2},
        "result_item": "greater_scroll_fire", "result_qty": 1,
        "ability": "intelligence", "dc": 24, "profession": "alchemy", "min_level": 20,
    },
    "greater_scroll_lightning": {
        "materials": {"glimmerdeep_moss": 3, "moonpetal": 3, "sulfur_dust": 2},
        "result_item": "greater_scroll_lightning", "result_qty": 1,
        "ability": "intelligence", "dc": 24, "profession": "alchemy", "min_level": 20,
    },
    "greater_scroll_cold": {
        "materials": {"glimmerdeep_moss": 3, "moonpetal": 3, "sulfur_dust": 2},
        "result_item": "greater_scroll_cold", "result_qty": 1,
        "ability": "intelligence", "dc": 24, "profession": "alchemy", "min_level": 20,
    },
    "greater_scroll_earth": {
        "materials": {"glimmerdeep_moss": 3, "moonpetal": 3, "sulfur_dust": 2},
        "result_item": "greater_scroll_earth", "result_qty": 1,
        "ability": "intelligence", "dc": 24, "profession": "alchemy", "min_level": 20,
    },
    "greater_scroll_force": {
        "materials": {"glimmerdeep_moss": 3, "moonpetal": 3, "sulfur_dust": 2},
        "result_item": "greater_scroll_force", "result_qty": 1,
        "ability": "intelligence", "dc": 24, "profession": "alchemy", "min_level": 20,
    },
    "greater_scroll_necrotic": {
        "materials": {"glimmerdeep_moss": 3, "moonpetal": 3, "sulfur_dust": 2},
        "result_item": "greater_scroll_necrotic", "result_qty": 1,
        "ability": "intelligence", "dc": 24, "profession": "alchemy", "min_level": 20,
    },
    "greater_scroll_poison": {
        "materials": {"glimmerdeep_moss": 3, "moonpetal": 3, "sulfur_dust": 2},
        "result_item": "greater_scroll_poison", "result_qty": 1,
        "ability": "intelligence", "dc": 24, "profession": "alchemy", "min_level": 20,
    },
    "greater_scroll_psychic": {
        "materials": {"glimmerdeep_moss": 3, "moonpetal": 3, "sulfur_dust": 2},
        "result_item": "greater_scroll_psychic", "result_qty": 1,
        "ability": "intelligence", "dc": 24, "profession": "alchemy", "min_level": 20,
    },
    "greater_scroll_radiant": {
        "materials": {"glimmerdeep_moss": 3, "moonpetal": 3, "sulfur_dust": 2},
        "result_item": "greater_scroll_radiant", "result_qty": 1,
        "ability": "intelligence", "dc": 24, "profession": "alchemy", "min_level": 20,
    },
    # Alchemy's own rebirth-gated capstone ladder (2026-09-10, per
    # Coffee: give Alchemy the same real endgame growth the 4 guilds got
    # in v1.27.578 -- Alchemy's real gap, per that audit, was capping at
    # min_level 20 with no rebirth tier at all, unlike Blacksmithing's
    # forge_guild+rebirth journeyman/master/grandmaster/godsforged ladder
    # just below (in ADVANCED_RECIPES). Same requires_guild/min_rebirth
    # gate shape, on the Arcane Circle (alchemy's own guild, see
    # guilds.GUILD_PERMANENT_PROFESSION) instead of the Forge Guild, DCs
    # pushed past the existing greater_scroll_* ceiling (24) the same way
    # each Forge tier pushes past chain_mail's. See items.py's own entries
    # for why these are new RECIPES (fixed-effect consumables), not
    # ADVANCED_RECIPES (generated gear).
    "tonic_of_ascension": {
        "materials": {"glimmerdeep_moss": 3, "moonpetal": 3, "silverleaf_herb": 2},
        "result_item": "tonic_of_ascension", "result_qty": 1,
        "ability": "intelligence", "dc": 26, "profession": "alchemy",
        "requires_guild": "arcane_circle",
    },
    "grand_tonic_of_ascension": {
        "materials": {"glimmerdeep_moss": 5, "moonpetal": 5, "silverleaf_herb": 3},
        "result_item": "grand_tonic_of_ascension", "result_qty": 1,
        "ability": "intelligence", "dc": 29, "profession": "alchemy",
        "requires_guild": "arcane_circle", "min_rebirth": 1,
    },
    "sublime_tonic_of_ascension": {
        "materials": {"glimmerdeep_moss": 7, "moonpetal": 7, "silverleaf_herb": 4},
        "result_item": "sublime_tonic_of_ascension", "result_qty": 1,
        "ability": "intelligence", "dc": 32, "profession": "alchemy",
        "requires_guild": "arcane_circle", "min_rebirth": 2,
    },
    "godsbrew_of_ascension": {
        "materials": {"glimmerdeep_moss": 10, "moonpetal": 10, "silverleaf_herb": 6, "godshard": 1},
        "result_item": "godsbrew_of_ascension", "result_qty": 1,
        "ability": "intelligence", "dc": 35, "profession": "alchemy",
        "requires_guild": "arcane_circle", "min_rebirth": 3,
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
    # Cooking's own scaling ladder (2026-09-10) -- see items.py's
    # hearty_stew/travelers_feast/banquet_of_the_reborn for the full
    # rationale. Same magnitude jump per tier as Alchemy's own capstone
    # ladder just above (roughly +3 DC per step, ending gated on
    # cooking's real home guild + a real rebirth).
    "hearty_stew": {
        "materials": {"raw_fish": 2, "wood": 1, "silverleaf_herb": 1},
        "result_item": "hearty_stew", "result_qty": 1,
        "ability": "wisdom", "dc": 12, "profession": "cooking",
    },
    "travelers_feast": {
        "materials": {"raw_fish": 4, "wood": 2, "silverleaf_herb": 2},
        "result_item": "travelers_feast", "result_qty": 1,
        "ability": "wisdom", "dc": 18, "profession": "cooking", "min_level": 10,
    },
    "banquet_of_the_reborn": {
        "materials": {"raw_fish": 6, "wood": 3, "silverleaf_herb": 3, "moonpetal": 1},
        "result_item": "banquet_of_the_reborn", "result_qty": 1,
        "ability": "wisdom", "dc": 24, "profession": "cooking",
        "requires_guild": "adventurers_guild", "min_rebirth": 1,
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


# Guild tier ladder (2026-08-11, per Coffee: "make sure all tiers are
# covered from beginning of the game to end game" + the rebirth system
# is the intended "growth and breaking the game... Human Ascends to
# God" spine). Two new OPTIONAL recipe fields, checked by this one
# shared helper and consumed identically by both _do_enchant_item and
# _do_craft (bot.py) -- a recipe with neither field set (every recipe
# above this point) is ungated, exactly as before.
def recipe_requirement_gate(character: dict, recipe: dict) -> str | None:
    """
    Returns a player-facing rejection message, or None if `character`
    meets `recipe`'s requirements.

    Real live bug (2026-08-13, per Coffee: "i want to be able to
    enchant with spells from warlock, sorcerer, bard and other magic
    type users"): the guild check here only ever looked at
    character["guild"] (the PRIMARY guild), never the secondary/
    Promotion guilds the v1.27.168 guild-doubling system introduced
    (guilds.held_guild_ids) -- a player whose CLASS home profession
    isn't alchemy (every non-Wizard/Rogue class, see
    CLASS_PROFESSIONS above) has no natural path to making Enchanters'
    Guild their PRIMARY guild without abandoning their class's own
    guild, so the realistic way any of them ever reach the guild-gated
    enchant tiers is by earning Enchanters' Guild as a SECOND
    (Promotion) guild after evolving -- which this check silently
    rejected anyway, even for a genuine member.
    """
    requires_guild = recipe.get("requires_guild")
    if requires_guild:
        from guilds import GUILDS, held_guild_ids
        if requires_guild not in held_guild_ids(character):
            guild_name = GUILDS.get(requires_guild, {}).get("name", requires_guild)
            return f"That recipe is reserved for members of the {guild_name}."
    min_rebirth = recipe.get("min_rebirth")
    if min_rebirth and character.get("rebirth_count", 0) < min_rebirth:
        return f"That recipe demands the mastery of rebirth #{min_rebirth} or higher — you're not there yet."
    # New third gate (2026-09-08, task #6, per Coffee's own explicit
    # standalone constraint: "player must be Min lv 20 to craft a magic
    # item"). No existing recipe needed a raw character-level gate
    # before this -- guild/rebirth already covered "how far into
    # end-game progression," but the magic-item-upgrade recipe is
    # deliberately gated on ordinary character level instead, since it's
    # meant to be a genuine mid-game (not end-game) milestone.
    min_level = recipe.get("min_level")
    if min_level and character.get("level", 1) < min_level:
        return f"That recipe demands character level {min_level} or higher — you're not there yet."
    return None


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

    # Forge Guild ladder (2026-08-11, per Coffee: cover every guild tier
    # from level 1 to end game, using the rebirth system -- "Human
    # Ascends to God" -- as the real gate, not an arbitrary new number.
    # Every tier here reuses generate_item's EXISTING tier strings
    # (very_rare/legendary/mythic already work today with zero new
    # plumbing) -- only the recipe entries and the requires_guild/
    # min_rebirth gate (see recipe_requirement_gate below) are new.
    # Journeyman: guild membership alone. Master/Grandmaster: guild +
    # successive rebirths. Godsforged: guild + rebirth 3 + one Godshard,
    # a real boss-drop-only material (bot.py's GODSHARD_DROP_CHANCE).
    "journeyman_blade": {
        "materials": {"iron_ore": 10, "moonpetal": 2},
        "item_type": "weapon", "base_id": "longsword", "tier": "very_rare",
        "ability": "strength", "dc": 19, "profession": "blacksmithing",
        "name": "Journeyman's Blade", "requires_guild": "forge_guild",
    },
    "masters_plate": {
        "materials": {"iron_ore": 14, "silverleaf_herb": 4, "moonpetal": 2},
        "item_type": "armor", "base_id": "chain_mail", "tier": "legendary",
        "ability": "strength", "dc": 23, "profession": "blacksmithing",
        "name": "Master's Plate", "requires_guild": "forge_guild", "min_rebirth": 1,
    },
    "grandmasters_greataxe": {
        "materials": {"iron_ore": 16, "moonpetal": 4},
        "item_type": "weapon", "base_id": "greataxe", "tier": "legendary",
        "ability": "strength", "dc": 26, "profession": "blacksmithing",
        "name": "Grandmaster's Greataxe", "requires_guild": "forge_guild", "min_rebirth": 2,
    },
    "godsforged_blade": {
        "materials": {"iron_ore": 20, "moonpetal": 6, "godshard": 1},
        "item_type": "weapon", "base_id": "longsword", "tier": "mythic",
        "ability": "strength", "dc": 29, "profession": "blacksmithing",
        "name": "Godsforged Blade", "requires_guild": "forge_guild", "min_rebirth": 3,
    },

    # Remaining weapon/armor base coverage (2026-09-08, per Coffee:
    # "investigate how to forge the next lv of weapons / armour - if
    # there isnt next lv weapons and armour available add it in"). Every
    # base above this point already had a real ADVANCED_RECIPES entry
    # (longsword/greataxe/chain_shirt/chain_mail/wooden_shield) -- dagger/
    # shortsword/longbow/leather were real bases in rules/item_generator.py
    # with no recipe at all yet, and rapier had no base entry whatsoever
    # (added just above, alongside this). Same masterwork-at-rare/no-gate
    # shape the original 4 masterwork_* recipes above use.
    "masterwork_dagger": {
        "materials": {"iron_ore": 3, "moonpetal": 1},
        "item_type": "weapon", "base_id": "dagger", "tier": "rare",
        "ability": "strength", "dc": 14, "profession": "blacksmithing",
        "name": "Masterwork Dagger",
    },
    "masterwork_shortsword": {
        "materials": {"iron_ore": 4, "moonpetal": 1},
        "item_type": "weapon", "base_id": "shortsword", "tier": "rare",
        "ability": "strength", "dc": 15, "profession": "blacksmithing",
        "name": "Masterwork Shortsword",
    },
    "masterwork_longbow": {
        "materials": {"wood": 5, "iron_ore": 1, "moonpetal": 1},
        "item_type": "weapon", "base_id": "longbow", "tier": "rare",
        "ability": "strength", "dc": 16, "profession": "blacksmithing",
        "name": "Masterwork Longbow",
    },
    "masterwork_leather_armor": {
        "materials": {"silverleaf_herb": 3, "iron_ore": 1},
        "item_type": "armor", "base_id": "leather", "tier": "rare",
        "ability": "strength", "dc": 14, "profession": "blacksmithing",
        "name": "Masterwork Leather Armor",
    },
    "masterwork_rapier": {
        "materials": {"iron_ore": 5, "moonpetal": 1},
        "item_type": "weapon", "base_id": "rapier", "tier": "rare",
        "ability": "strength", "dc": 16, "profession": "blacksmithing",
        "name": "Masterwork Rapier",
    },
    # Full-ladder parity for one of the new bases (matching longsword's
    # own masterwork -> journeyman -> ... spread) -- rapier, since a
    # duelist's-weapon flavor fits a guild-gated upper tier naturally.
    "duelists_rapier": {
        "materials": {"iron_ore": 9, "moonpetal": 3},
        "item_type": "weapon", "base_id": "rapier", "tier": "very_rare",
        "ability": "strength", "dc": 20, "profession": "blacksmithing",
        "name": "Duelist's Rapier", "requires_guild": "forge_guild",
    },
}


def get_advanced_recipe(recipe_id: str) -> dict | None:
    return ADVANCED_RECIPES.get(recipe_id)


def next_tier_up(tier: str) -> str:
    """One real step up TIERS, capped at the top (mythic) -- never overflows."""
    idx = TIERS.index(tier)
    return TIERS[min(idx + 1, len(TIERS) - 1)]


def resolve_advanced_craft(character: dict, recipe_id: str, practiced_bonus: int = 0, masterwork: bool = False) -> dict:
    """
    Same shape/convention as resolve_craft above (materials only consumed
    on success, practiced_bonus added to the roll here in the rules
    layer) -- the one difference is `generated_item`, a full rolled item
    dict from rules/item_generator.py at the recipe's fixed tier, instead
    of a static result_item/result_qty. The caller (bot.py) persists it
    via db.create_item_instance exactly like combat loot already does.

    `masterwork` (2026-08-11, per Coffee: "grinding for better weapons
    and RNG allowing us to make better ones") -- the caller has already
    rolled this off the crafter's own mastery %
    (bot._roll_profession_mastery); True bumps the generated item one
    real tier higher than the recipe's own fixed tier via next_tier_up,
    so a highly practiced crafter's SAME recipe can meaningfully
    outclass a fresh one's.
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
    result_tier = recipe["tier"]
    if success:
        if masterwork:
            result_tier = next_tier_up(recipe["tier"])
        generated_item = generate_item(
            item_type=recipe["item_type"], base_id=recipe.get("base_id"), tier=result_tier,
        )

    return {
        "outcome": "success" if success else "failure",
        "recipe_id": recipe_id,
        "materials_consumed": recipe["materials"] if success else {},
        "generated_item": generated_item,
        "check": check,
        "dc": recipe["dc"],
        "ability": recipe["ability"],
        "masterwork": masterwork if success else False,
    }


# Dismantling (2026-08-13, per Coffee: "if the players do not want the
# items/equipables/weapons/armor/Rings/Amuluts lets them Dismantle
# using thier forging skill to gather maters that would have been used
# to craft/forge/enchant that exact weapon ... if not successful give
# them the bare minimum, and if very successful give them the full
# materials for it"). Real materials come from the item's own real
# recipe when one exists -- ADVANCED_RECIPES for a procedurally-
# generated rare+ piece (matched by its real generated_base+rarity,
# the same two fields rules/item_generator.py always stamps onto a
# generated item), RECIPES for a plain catalog base (a longsword found
# rather than rolled, or a common/uncommon generated piece with no
# ADVANCED_RECIPES entry of its own). Items with no real recipe at all
# (rings/amulets -- catalog-only in this game, never player-forged --
# plus any boss-only/store-only drop) fall back to a price-scaled
# iron_ore salvage yield, the same "generic scrap metal" material
# blacksmithing already uses everywhere else in this file, rather than
# inventing a resource no recipe actually calls for.
DISMANTLE_FALLBACK_MATERIAL = "iron_ore"
DISMANTLE_FALLBACK_PRICE_PER_UNIT = 40


def dismantle_materials_for_item(item_id: str, item: dict) -> dict[str, int]:
    """Full (100%) material yield for dismantling `item` -- see the module comment above for the real-recipe-first, price-fallback-second resolution order."""
    generated_base = item.get("generated_base")
    if generated_base:
        tier = item.get("rarity")
        for recipe in ADVANCED_RECIPES.values():
            if recipe.get("base_id") == generated_base and recipe.get("tier") == tier:
                return dict(recipe["materials"])
        base_recipe = RECIPES.get(generated_base)
        if base_recipe:
            return dict(base_recipe["materials"])
    else:
        recipe = RECIPES.get(item_id)
        if recipe:
            return dict(recipe["materials"])
    price = item.get("price", 0)
    return {DISMANTLE_FALLBACK_MATERIAL: max(1, round(price / DISMANTLE_FALLBACK_PRICE_PER_UNIT))}


DISMANTLE_DC = 13


def resolve_dismantle(character: dict, item_id: str, item: dict, practiced_bonus: int = 0) -> dict:
    """
    Real "forging skill" (strength, blacksmithing) ability check decides
    how much of dismantle_materials_for_item's full yield the player
    actually recovers, per Coffee's own three-tier spec: a natural 20 or
    a check beating DC+10 ("very successful") returns the FULL yield; an
    ordinary success returns half (rounded up, at least 1 of each
    material); failure ("not successful") returns only the bare minimum
    -- 1 unit of a single material from the yield.
    """
    full_yield = dismantle_materials_for_item(item_id, item)
    check = roll_ability_check(character, "strength", proficient=False)
    check["total"] += practiced_bonus
    check["practiced_bonus"] = practiced_bonus

    if check["raw_roll"] == 20 or check["total"] >= DISMANTLE_DC + 10:
        outcome = "very_successful"
        materials = dict(full_yield)
    elif check["total"] >= DISMANTLE_DC:
        outcome = "successful"
        materials = {mat: max(1, -(-qty // 2)) for mat, qty in full_yield.items()}
    else:
        outcome = "not_successful"
        materials = {next(iter(full_yield)): 1}

    return {"outcome": outcome, "materials": materials, "check": check, "dc": DISMANTLE_DC}


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
    # Physical/any-type damage % (2026-08-11, per Coffee: "use the % to
    # increase damage of physical damage, damage types, and elemental
    # type") -- damage-type-agnostic on purpose, unlike enchant_flame/
    # enchant_frost below which retype the weapon. A plain physical
    # weapon keeps its own damage_type and still gets a real numeric
    # bonus; an elemental one stacks this on top of its retype. Base
    # tier, no guild gate -- every enchanter gets a real damage lever.
    "enchant_sharpen": {
        "materials": {"iron_ore": 2, "sulfur_dust": 1},
        "affix": {"kind": "elemental_damage_bonus", "value": 15},
        "applies_to": ("weapon",),
        "ability": "intelligence", "dc": 15, "profession": "alchemy",
    },
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
    # Real live gap (2026-08-13, per Coffee: "i want to be able to
    # enchant with spells from warlock, sorcerer, bard and other magic
    # type users") -- _do_enchant_item's own spell-gate (bot.py) already
    # accepts ANY class's known spell for a matching damage_type, no
    # class check at all, but enchant_flame/enchant_frost above were the
    # ONLY weapon-retype recipes that ever existed -- fire and cold.
    # Warlock's entire real spell list (spells.py CLASS_SPELL_LISTS) has
    # exactly one damage-dealing spell (Eldritch Blast, force) and Bard
    # has exactly one (Vicious Mockery, psychic) -- with no "enchant
    # force"/"enchant psychic" recipe to even name, those two classes
    # could never pass _find_enchant_recipe_in_text at all, regardless
    # of what they know. Same base tier as flame/frost (no guild gate)
    # -- this is about covering every real damage type this game
    # actually has (see rules/combat.py's apply_damage_type_modifier),
    # not a new mechanic.
    "enchant_force": {
        "materials": {"moonpetal": 1, "iron_ore": 2},
        "affix": {"kind": "elemental_damage", "damage_type": "force"},
        "applies_to": ("weapon",),
        "ability": "intelligence", "dc": 15, "profession": "alchemy",
    },
    "enchant_psychic": {
        "materials": {"moonpetal": 2, "silverleaf_herb": 1},
        "affix": {"kind": "elemental_damage", "damage_type": "psychic"},
        "applies_to": ("weapon",),
        "ability": "intelligence", "dc": 15, "profession": "alchemy",
    },
    "enchant_necrotic": {
        "materials": {"sulfur_dust": 2, "glimmerdeep_moss": 1},
        "affix": {"kind": "elemental_damage", "damage_type": "necrotic"},
        "applies_to": ("weapon",),
        "ability": "intelligence", "dc": 15, "profession": "alchemy",
    },
    "enchant_radiant": {
        "materials": {"silverleaf_herb": 2, "moonpetal": 1},
        "affix": {"kind": "elemental_damage", "damage_type": "radiant"},
        "applies_to": ("weapon",),
        "ability": "intelligence", "dc": 15, "profession": "alchemy",
    },
    "enchant_poison": {
        "materials": {"sulfur_dust": 1, "silverleaf_herb": 2},
        "affix": {"kind": "elemental_damage", "damage_type": "poison"},
        "applies_to": ("weapon",),
        "ability": "intelligence", "dc": 15, "profession": "alchemy",
    },
    # Elemental Foundations (2026-08-30): the note above enchant_force
    # already said the goal was "covering every real damage type this
    # game actually has" -- lightning was a real, live gap even before
    # earth existed at all (8 of 9 real types had an offensive enchant,
    # lightning didn't). Same base tier as every other elemental retype
    # above; both are now covered.
    "enchant_lightning": {
        "materials": {"sulfur_dust": 1, "iron_ore": 2},
        "affix": {"kind": "elemental_damage", "damage_type": "lightning"},
        "applies_to": ("weapon",),
        "ability": "intelligence", "dc": 15, "profession": "alchemy",
    },
    "enchant_earth": {
        "materials": {"iron_ore": 2, "glimmerdeep_moss": 1},
        "affix": {"kind": "elemental_damage", "damage_type": "earth"},
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
    # Elemental resistance wards (2026-08-10, per Coffee: "allow the
    # player to enchant armour and other equipables to raise resistences
    # in Frost/Flame/Spark... if the players gain enough resistences...
    # nullify the damage OR in extreme cases heal the player" -- a
    # NUMERIC, STACKING affix ("elemental_resistance", a real percentage
    # value) rather than enchant_warding's existing flat boolean
    # "resistance" affix above, which stays untouched. Each ward grants
    # 50 percentage points to ONE specific damage type, live-summed
    # across every equipped item by bot.py's
    # _apply_equipped_elemental_profile -- 2 matching wards on 2
    # different slots reaches 100% (rules.combat.apply_damage_type_
    # modifier fully nullifies the hit), 3 reaches 150% (rules.combat.
    # elemental_overflow_heal converts the 50% overflow into real
    # healing instead). Deliberately armor/shield/ring/amulet/wondrous
    # only, never weapon -- this is defense, matching enchant_warding's
    # own scoping, and matching Coffee's own wording ("armour and other
    # equipables").
    "enchant_flame_ward": {
        "materials": {"sulfur_dust": 2, "iron_ore": 1},
        "affix": {"kind": "elemental_resistance", "damage_type": "fire", "value": 50},
        "applies_to": ("armor", "shield", "ring", "amulet", "wondrous"),
        "ability": "intelligence", "dc": 16, "profession": "alchemy",
    },
    "enchant_frost_ward": {
        "materials": {"moonpetal": 2, "silverleaf_herb": 2},
        "affix": {"kind": "elemental_resistance", "damage_type": "cold", "value": 50},
        "applies_to": ("armor", "shield", "ring", "amulet", "wondrous"),
        "ability": "intelligence", "dc": 16, "profession": "alchemy",
    },
    "enchant_spark_ward": {
        "materials": {"iron_ore": 2, "moonpetal": 1},
        "affix": {"kind": "elemental_resistance", "damage_type": "lightning", "value": 50},
        "applies_to": ("armor", "shield", "ring", "amulet", "wondrous"),
        "ability": "intelligence", "dc": 16, "profession": "alchemy",
    },
    # Elemental Foundations (2026-08-30): earth's own base-tier ward,
    # same shape/tier as flame_ward/frost_ward/spark_ward above -- this
    # is the real mechanism behind Coffee's own example ("if u have an
    # ice armour and u get hit with ice it shud heal the player"), just
    # for earth instead of cold; stacking two of these on the same
    # damage type already crosses 100% via rules.combat.elemental_
    # overflow_heal with no extra work here.
    "enchant_stone_ward": {
        "materials": {"iron_ore": 2, "glimmerdeep_moss": 1},
        "affix": {"kind": "elemental_resistance", "damage_type": "earth", "value": 50},
        "applies_to": ("armor", "shield", "ring", "amulet", "wondrous"),
        "ability": "intelligence", "dc": 16, "profession": "alchemy",
    },

    # Enchanters' Guild ladder (2026-08-11, per Coffee: rework the guild
    # away from the commissioned-item idea -- players now enchant real
    # items themselves, climbing a real 5-tier ladder gated on guild
    # membership + rebirth, same "Human Ascends to God" spine as the
    # Forge Guild ladder above. Every kind here is already implemented
    # by db._apply_affix -- only the recipes and the new requires_guild/
    # min_rebirth gate (recipe_requirement_gate below) are new.
    "enchant_greater_ward": {
        "materials": {"sulfur_dust": 4, "iron_ore": 2, "glimmerdeep_moss": 1},
        "affix": {"kind": "elemental_resistance", "damage_type": "fire", "value": 75},
        "applies_to": ("armor", "shield", "ring", "amulet", "wondrous"),
        "ability": "intelligence", "dc": 19, "profession": "alchemy",
        "requires_guild": "enchanters_guild",
    },
    # A self-reinforcing mastery loop, not a combat stat -- Master
    # enchanters craft tools that make the WEARER better at enchanting,
    # via the already-implemented "profession_bonus" affix kind
    # (db._apply_affix, live-summed by bot.py's _equipped_profession_
    # bonus exactly like practiced_bonus).
    "enchant_masters_focus": {
        "materials": {"moonpetal": 4, "glimmerdeep_moss": 2},
        "affix": {"kind": "profession_bonus", "profession": "alchemy", "value": 3},
        "applies_to": ("ring", "amulet", "wondrous"),
        "ability": "intelligence", "dc": 22, "profession": "alchemy",
        "requires_guild": "enchanters_guild", "min_rebirth": 1,
    },
    "enchant_grand_ward": {
        "materials": {"moonpetal": 6, "silverleaf_herb": 4, "glimmerdeep_moss": 2},
        "affix": {"kind": "elemental_resistance", "damage_type": "cold", "value": 100},
        "applies_to": ("armor", "shield", "ring", "amulet", "wondrous"),
        "ability": "intelligence", "dc": 25, "profession": "alchemy",
        "requires_guild": "enchanters_guild", "min_rebirth": 2,
    },
    # The true capstone: db._apply_affix's "ignore_resistance" kind
    # already exists (previously mythic-tier-RNG-loot-exclusive) -- this
    # is the first DETERMINISTIC path to it, earned by walking the whole
    # guild ladder to rebirth 3 and finding a real Godshard.
    "enchant_godsforged_ward": {
        "materials": {"glimmerdeep_moss": 4, "moonpetal": 6, "godshard": 1},
        "affix": {"kind": "ignore_resistance"},
        "applies_to": ("weapon",),
        "ability": "intelligence", "dc": 28, "profession": "alchemy",
        "requires_guild": "enchanters_guild", "min_rebirth": 3,
    },

    # Magic item system Phase 8 (2026-09-08, per Coffee: "i want to be
    # able to craft something better than a longsword... offer a new
    # recipie to be able to generate a RNG magic item with a +1 to a RNG
    # stat (they can craft any weapon or armour into a magic item that
    # was looted or crafted previously)... Use the forging proficiency %
    # to add + to whatever stat"). A genuinely new axis -- nothing
    # before this let equipment touch a core ability score at all (see
    # db._apply_affix's new "ability_bonus" kind). Deliberately a Forge
    # Guild (blacksmithing) recipe, not Enchanters'/alchemy -- Coffee's
    # own wording is "forging"/"forge guild" throughout. `"ability":
    # "random"` is a special marker `bot._do_forge_magic_item` rolls at
    # cast time (both WHICH ability and whether it's +1 or +2, per the
    # forging proficiency % -- see that handler's own docstring) rather
    # than a fixed value like every other recipe's affix above; this is
    # the one recipe in this whole file whose outcome is genuinely
    # randomized beyond success/fail and masterwork quality. Applies to
    # weapon/armor/accessory per Coffee's own explicit answer when asked
    # ("Weapons, Armor, Accessories"). min_level (2026-09-08, per
    # Coffee's own standalone follow-up: "player must be Min lv 20 to
    # craft a magic item") is a real, new gate kind -- see
    # recipe_requirement_gate's own updated docstring.
    "forge_magic_upgrade": {
        "materials": {"iron_ore": 4, "moonpetal": 2},
        "affix": {"kind": "ability_bonus", "ability": "random", "value": 1},
        "applies_to": ("weapon", "armor", "shield", "ring", "amulet", "wondrous"),
        "ability": "strength", "dc": 18, "profession": "blacksmithing",
        "requires_guild": "forge_guild", "min_level": 20,
    },
}


def get_enchant_recipe(recipe_id: str) -> dict | None:
    return ENCHANT_RECIPES.get(recipe_id)
