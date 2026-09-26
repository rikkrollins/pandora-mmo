"""
items.py
The master item catalog. Every item players can carry, buy, sell, or
find is defined here as data — adding a new item never requires
touching bot.py or the rules engine.

Item categories: weapon, armor, shield, consumable, scroll, ring,
amulet, wondrous, quest_item, material.
"""
import copy
import re

ITEMS = {
    # --- Weapons ---
    # weapon_category (task #223, per Coffee): "simple" or "martial" --
    # real classification for THIS game's own small weapon catalog, not
    # a byte-for-byte copy of the full 5E weapon table (which has many
    # weapons this game doesn't). See class_features.py's
    # WEAPON_PROFICIENCIES for which classes are proficient with which.
    # damage_type (2026-07-24): defaults to "physical" everywhere it's
    # read (see rules/combat.py's _apply_damage_type_modifier), so every
    # weapon below behaves exactly as before unless it's one of the two
    # that already had a real elemental/material identity in its flavor
    # note -- flametongue_shortsword's fire and silvered_dagger's silver
    # were previously pure flavor text with zero mechanical effect; both
    # are now real.
    "rusty_dagger": {
        "name": "Rusty Dagger", "type": "weapon", "rarity": "common",
        "price": 2, "weight": 1, "damage_dice": "1d4", "ability": "dexterity",
        "weapon_category": "simple", "weapon_type": "dagger", "damage_type": "physical",
        "description": "Deals 1d4 physical damage. Pitted along the blade, the edge still holds well enough to do the job.",
    },
    "shortsword": {
        "name": "Shortsword", "type": "weapon", "rarity": "common",
        "price": 10, "weight": 2, "damage_dice": "1d6", "ability": "dexterity",
        "weapon_category": "simple", "weapon_type": "shortsword", "damage_type": "physical",
        "description": "Deals 1d6 physical damage. A plain, honest blade — nothing about it stands out, and nothing about it fails you.",
    },
    "longsword": {
        "name": "Longsword", "type": "weapon", "rarity": "common",
        "price": 15, "weight": 3, "damage_dice": "1d8", "ability": "strength",
        "weapon_category": "martial", "weapon_type": "longsword", "damage_type": "physical",
        "description": "Deals 1d8 physical damage. Well-balanced steel, the kind every armory keeps in stock because it never stops selling.",
    },
    "greataxe": {
        "name": "Greataxe", "type": "weapon", "rarity": "common",
        "price": 30, "weight": 7, "damage_dice": "1d12", "ability": "strength",
        "weapon_category": "martial", "weapon_type": "greataxe", "damage_type": "physical", "two_handed": True,
        "description": "Deals 1d12 physical damage. Heavy enough that swinging it wrong would hurt you as much as anything else.",
    },
    "longbow": {
        "name": "Longbow", "type": "weapon", "rarity": "common",
        "price": 50, "weight": 2, "damage_dice": "1d8", "ability": "dexterity",
        "weapon_category": "martial", "weapon_type": "longbow", "damage_type": "physical", "ranged": True,
        "description": "Deals 1d8 physical damage. Strung tight, the wood still flexes like it was cut yesterday.",
    },
    "silvered_dagger": {
        "name": "Silvered Dagger", "type": "weapon", "rarity": "uncommon",
        "price": 75, "weight": 1, "damage_dice": "1d4+1", "ability": "dexterity",
        "note": "Effective against creatures vulnerable to silver.",
        "description": "Deals 1d4+1 silver damage. Effective against creatures vulnerable to silver.",
        "weapon_category": "simple", "weapon_type": "dagger", "damage_type": "silver",
    },
    "flametongue_shortsword": {
        "name": "Flametongue Shortsword", "type": "weapon", "rarity": "rare",
        "price": 0, "weight": 2, "damage_dice": "1d6+2", "ability": "dexterity",
        "note": "Warm to the touch. Wreathes itself in fire when drawn in anger.",
        "description": "Deals 1d6+2 fire damage. Warm to the touch. Wreathes itself in fire when drawn in anger.",
        "weapon_category": "martial", "weapon_type": "shortsword", "damage_type": "fire",
    },
    "the_last_word": {
        "name": "The Last Word", "type": "weapon", "rarity": "legendary",
        "price": 0, "weight": 3, "damage_dice": "2d8+3", "ability": "strength",
        "note": "The carving stops repeating itself the instant your hand closes around the hilt.",
        "description": "Deals 2d8+3 physical damage. The carving stops repeating itself the instant your hand closes around the hilt.",
        "weapon_category": "martial", "weapon_type": "longsword", "damage_type": "physical", "two_handed": True,
    },
    # Elemental Foundations (2026-08-30): before this, flametongue_
    # shortsword (fire) was the ONLY elemental weapon in the entire
    # catalog. These expand real elemental gear across Earth (new) and
    # Air/lightning (existing type, previously no dedicated weapon at
    # all), same rare-tier drop-only shape as flametongue.
    "stoneheart_warhammer": {
        "name": "Stoneheart Warhammer", "type": "weapon", "rarity": "rare",
        "price": 0, "weight": 5, "damage_dice": "1d8+2", "ability": "strength",
        "note": "Heavier than its size should allow. The head never chips, no matter what it strikes.",
        "description": "Deals 1d8+2 earth damage. Heavier than its size should allow. The head never chips, no matter what it strikes.",
        "weapon_category": "martial", "weapon_type": "warhammer", "damage_type": "earth",
    },
    "stormcaller_rapier": {
        "name": "Stormcaller Rapier", "type": "weapon", "rarity": "rare",
        "price": 0, "weight": 2, "damage_dice": "1d6+2", "ability": "dexterity",
        "note": "A faint crackle follows every thrust, half a second behind the blade.",
        "description": "Deals 1d6+2 lightning damage. A faint crackle follows every thrust, half a second behind the blade.",
        "weapon_category": "martial", "weapon_type": "rapier", "damage_type": "lightning",
    },

    # --- Armor & Shields ---
    # armor_category (task #223): "light"/"medium"/"heavy", or "shield"
    # for the shield itself -- same real-consequence system as
    # weapon_category above (see class_features.py's ARMOR_PROFICIENCIES).
    "leather_armor": {"name": "Leather Armor", "type": "armor", "rarity": "common", "price": 10, "weight": 10, "ac_base": 11, "armor_category": "light", "description": "Boiled and cured stiff enough to turn a glancing blow, soft enough not to slow you down."},
    "chain_shirt": {"name": "Chain Shirt", "type": "armor", "rarity": "common", "price": 50, "weight": 20, "ac_base": 13, "armor_category": "medium", "description": "Rings of iron, close-linked and heavier than they look at a glance."},
    "chain_mail": {"name": "Chain Mail", "type": "armor", "rarity": "uncommon", "price": 75, "weight": 55, "ac_base": 16, "armor_category": "heavy", "description": "A real suit of it, head to knee — the kind of weight you stop noticing after the first hour."},
    "wooden_shield": {"name": "Wooden Shield", "type": "shield", "rarity": "common", "price": 10, "weight": 6, "ac_bonus": 2, "armor_category": "shield", "description": "Banded oak, scarred along the rim from blows that never got any further."},

    # Elemental Foundations (2026-08-30): static armor with a real,
    # authored elemental_resistances affix -- same field shape
    # db._apply_affix's "elemental_resistance" kind writes onto generated
    # gear (bot._compute_equipped_resist_profile reads "elemental_
    # resistances": [{"damage_type", "value"}] off ANY item, generated or
    # static, identically). This is the literal mechanism behind Coffee's
    # own example ("if u have an ice armour and u get hit with ice it
    # shud heal the player") -- stack two of these (or an enchant_stone_
    # ward on top) past 100% earth resistance and rules.combat.elemental_
    # overflow_heal converts the overflow into real healing.
    "stoneward_plate": {
        "name": "Stoneward Plate", "type": "armor", "rarity": "rare", "price": 0, "weight": 60,
        "ac_base": 16, "armor_category": "heavy",
        "elemental_resistances": [{"damage_type": "earth", "value": 50}],
        "description": "Plate forged from stone that never fully stopped being stone. Cracks under an earth-shaking blow instead of denting.",
    },
    "stormguard_cloak": {
        "name": "Stormguard Cloak", "type": "wondrous", "rarity": "rare", "price": 0, "weight": 2,
        "elemental_resistances": [{"damage_type": "lightning", "value": 50}],
        "description": "Fabric that stands on end a half-second before a real strike lands, like it already knows.",
    },

    # --- Consumables ---
    # Potion healing (2026-07-25, per Coffee's evolution/HP-scaling pass):
    # rescaled from real dice rolls to guaranteed flat amounts (a d1
    # always rolls exactly 1, so "1d1+99" always totals exactly 100 --
    # no new schema needed) to match character HP now reaching into the
    # thousands/hundreds of thousands with rebirths (see rules/leveling.py's
    # EVOLUTION_HP_MULTIPLIER/rebirth_hp_max).
    "healing_potion": {
        "name": "Healing Potion", "type": "consumable", "rarity": "common",
        "price": 25, "weight": 0.5, "effect": "heal", "heal_dice": "1d1+99",
        "description": "Heals 1d1+99 HP (100 flat). A dull red liquid in a corked glass vial. Tastes worse than it works.",
    },
    "greater_healing_potion": {
        "name": "Greater Healing Potion", "type": "consumable", "rarity": "uncommon",
        "price": 100, "weight": 0.5, "effect": "heal", "heal_dice": "1d1+999",
        "description": "Heals 1d1+999 HP (1,000 flat). Thicker and darker than the common brew, and it goes down just as badly.",
    },
    # Labyrinth waystation shrine (2026-09-02, Phase L3, per Coffee: "a
    # shrine there for us to pray or give an offering of spring water")
    # -- deliberately no generic "effect"/"heal_dice" here, unlike an
    # ordinary potion: its real effect (a full party HP/spell-slot
    # refill) only ever fires through _do_labyrinth_checkpoint_offering,
    # gated to standing at a real checkpoint room, not a plain "use item".
    "spring_water": {
        "name": "Vial of Spring Water", "type": "material", "rarity": "uncommon", "price": 40, "weight": 0.2,
        "description": "Drawn from someplace deep and still, this deep in the Labyrinth. Tastes like nothing at all -- but a waystation's shrine seems to want it.",
    },
    "supreme_healing_potion": {
        "name": "Supreme Healing Potion", "type": "consumable", "rarity": "rare",
        "price": 500, "weight": 0.5, "effect": "heal", "heal_dice": "1d1+9999",
        "note": "Brewed for someone who's rebirthed more times than most people have leveled up.",
        "description": "Heals 1d1+9999 HP (10,000 flat). Brewed for someone who's rebirthed more times than most people have leveled up.",
    },
    "antitoxin": {
        "name": "Antitoxin", "type": "consumable", "rarity": "common",
        "price": 15, "weight": 0.1, "effect": "cure_poison",
        "description": "Cures poison. A thin, bitter tonic. Smells like it's already working before you drink it.",
    },
    # Real live gap (2026-08-26, per Coffee: "what cures silence?" ->
    # "create an item and a spell to cure silence -- check other status
    # effects and make sure there is items and spells to cure them
    # also"). Confirmed live: this engine's own on_hit_condition system
    # has NO cure at all for blinded/silenced (poisoned already had
    # Antitoxin above; prone recovers on its own the moment you act
    # again, same real 5E "standing up" rule, so it doesn't need one).
    # Both use a new generic "cure_condition" effect (bot.py's
    # _do_use_item) rather than a bespoke one-off per condition, driven
    # by each item's own "cures" list -- Antitoxin's existing
    # "cure_poison" path is left exactly as it was, untouched.
    "vocal_tonic": {
        "name": "Vocal Tonic", "type": "consumable", "rarity": "common",
        "price": 15, "weight": 0.1, "effect": "cure_condition", "cures": ["silenced"],
        "description": "Cures silence. A warm, honeyed syrup that unsticks a blocked throat and clears whatever's choking off your voice.",
    },
    "clarifying_drops": {
        "name": "Clarifying Drops", "type": "consumable", "rarity": "common",
        "price": 15, "weight": 0.1, "effect": "cure_condition", "cures": ["blinded"],
        "description": "Cures blindness. A few cold drops restore your sight almost instantly, though the sting lingers a moment after.",
    },
    # Per Coffee (2026-08-10): spell slots have no in-battle recovery
    # option at all in this game (real rule, see Support's deterministic
    # answer in ai/support_agent.py) -- these give casters a genuine
    # item-based alternative, same tiered Minor/Greater/Supreme pattern
    # as the healing potions above, distilled by The Arcane Circle (the
    # real spellcaster guild, see guilds.py) rather than a generic
    # unbranded "mana potion". restore_spell_slots_amount is read by
    # bot.py's _do_use_item; the top tier sets restore_all instead of a
    # fixed number.
    "spell_tonic": {
        "name": "Spell Tonic", "type": "consumable", "rarity": "common",
        "price": 40, "weight": 0.5, "effect": "restore_spell_slots", "restore_spell_slots_amount": 2,
        "description": "Restores 2 spell slots. A faintly glowing draught the Arcane Circle sells to apprentices who keep running dry mid-lesson.",
    },
    "greater_spell_tonic": {
        "name": "Greater Spell Tonic", "type": "consumable", "rarity": "uncommon",
        "price": 175, "weight": 0.5, "effect": "restore_spell_slots", "restore_spell_slots_amount": 5,
        "description": "Restores 5 spell slots. Brewed stronger, and it shows — the glow doesn't fade until well after the bottle's empty.",
    },
    "supreme_spell_tonic": {
        "name": "Supreme Spell Tonic", "type": "consumable", "rarity": "rare",
        "price": 700, "weight": 0.5, "effect": "restore_spell_slots", "restore_spell_slots_amount": 10,
        "description": "Restores 10 spell slots. The Arcane Circle only distills this for members they actually trust with it.",
    },
    "elixir_of_the_arcane_circle": {
        "name": "Elixir of the Arcane Circle", "type": "consumable", "rarity": "legendary",
        "price": 2500, "weight": 0.5, "effect": "restore_spell_slots", "restore_all": True,
        "note": "Fully restores every spell slot in a single swallow.",
        "description": "Fully restores every spell slot in a single swallow. The Arcane Circle brews vanishingly little of it, and never explains why.",
    },
    # Alchemy's own rebirth-gated capstone ladder (2026-09-10, per Coffee:
    # give the profession the same real "flatlines early -> real endgame
    # growth" fix the 4 guilds already got in v1.27.578 -- Alchemy's own
    # real gap, per that audit, was capping at min_level 20 with no
    # rebirth tier at all, unlike Blacksmithing's forge_guild+rebirth
    # journeyman/master/grandmaster/godsforged ladder (rules/crafting.py's
    # ADVANCED_RECIPES). That ladder produces GENERATED gear via
    # rules/item_generator.py, which doesn't fit a fixed-effect
    # consumable -- so this ladder is real RECIPES entries instead, same
    # shape recipe_requirement_gate already supports for either (it takes
    # a plain recipe dict, not an ADVANCED_RECIPES-specific one).
    # Deliberately a NEW effect (permanent_stat_increase) rather than
    # reusing elixir_of_the_arcane_circle's restore_all=True -- that
    # elixir is intentionally kept uncraftable (see scroll_cure_wounds's
    # RECIPES comment for the "left as a real find only" convention this
    # already follows), so a craftable ladder needed its own real payoff
    # instead of a backdoor to that one. permanent_stat_guild (not a
    # hardcoded ability score) reuses guilds.permanent_stat_for at
    # use-time -- the exact per-class lookup the 2026-08-14 guild audit
    # added after catching Sorcerer/Warlock wrongly granted Wizard's
    # Intelligence -- so this correctly grants Charisma to a Sorcerer/
    # Warlock and Intelligence to a Wizard, never one flat stat for all
    # three Arcane Circle classes.
    "tonic_of_ascension": {
        "name": "Tonic of Ascension", "type": "consumable", "rarity": "epic",
        "price": 900, "weight": 0.5, "effect": "permanent_stat_increase",
        "permanent_stat_guild": "arcane_circle", "permanent_stat_amount": 1,
        "description": "A single, real, permanent point of arcane mastery, brewed and bound into the body. The Arcane Circle guards this recipe closely.",
    },
    "grand_tonic_of_ascension": {
        "name": "Grand Tonic of Ascension", "type": "consumable", "rarity": "legendary",
        "price": 2200, "weight": 0.5, "effect": "permanent_stat_increase",
        "permanent_stat_guild": "arcane_circle", "permanent_stat_amount": 1,
        "description": "A stronger brew of the same rite, reserved for a caster who has already died and been reborn at least once.",
    },
    "sublime_tonic_of_ascension": {
        "name": "Sublime Tonic of Ascension", "type": "consumable", "rarity": "legendary",
        "price": 4500, "weight": 0.5, "effect": "permanent_stat_increase",
        "permanent_stat_guild": "arcane_circle", "permanent_stat_amount": 1,
        "description": "Twice-reborn mastery, distilled. Vanishingly few Arcane Circle members ever brew this one for themselves.",
    },
    "godsbrew_of_ascension": {
        "name": "Godsbrew of Ascension", "type": "consumable", "rarity": "mythic",
        "price": 9000, "weight": 0.5, "effect": "permanent_stat_increase",
        "permanent_stat_guild": "arcane_circle", "permanent_stat_amount": 2,
        "description": "The last real rite the Arcane Circle has left to teach — a shard of something that shouldn't still exist, folded into the brew. Whatever drinks this stops being entirely human.",
    },
    # Advanced-mechanic combat potions (2026-09-16, per Coffee: "make some
    # other types of potions that use advance battle mechanics ? like
    # speed... allowing them to dodge, making the nemies slower,
    # increasing defense in layers, increasing attack power or magic
    # power in players, and decreasing in enemies", then "advanced
    # recipies and potions can be available at 60+", then "dont sell
    # those potions in shops, make it so u have to be lv 20+ to be able
    # to craft them"). Six real effects, each a Tier 1 (min_level 20,
    # rules/crafting.py) / Tier 2 "Greater" (min_level 60) pair --
    # deliberately never added to any shop's inventory, craft-only.
    # Every one of these reuses this game's own EXISTING temporary-
    # condition machinery (bot._apply_timed_condition, the same shared
    # helper Bless/Shield/Invisibility/Faerie Fire already use) rather
    # than inventing a new buff system -- see bot.py's
    # _attacks_per_turn/_attack_advantage_disadvantage/_do_use_item and
    # rules/combat.py's resolve_attack/effective_defender_ac for the
    # real mechanical hooks each one lands on.
    "potion_of_haste": {
        "name": "Potion of Haste", "type": "consumable", "rarity": "rare",
        "price": 0, "weight": 0.5, "effect": "combat_buff", "buff_condition": "hastened",
        "buff_duration_rounds": 10,
        "description": "Grants a real extra attack this fight — your limbs move like the world just slowed down half a step. Fades after 10 rounds.",
    },
    "greater_potion_of_haste": {
        "name": "Greater Potion of Haste", "type": "consumable", "rarity": "very_rare",
        "price": 0, "weight": 0.5, "effect": "combat_buff", "buff_condition": "greater_hastened",
        "buff_duration_rounds": 10,
        "description": "The extra attack, plus a real edge reading the fight itself — your own attacks land with advantage for 10 rounds, like everyone else is a half-beat behind.",
    },
    "potion_of_evasion": {
        "name": "Potion of Evasion", "type": "consumable", "rarity": "rare",
        "price": 0, "weight": 0.5, "effect": "combat_buff", "buff_condition": "evasive",
        "buff_duration_rounds": 10,
        "description": "Anyone swinging at you fights with disadvantage for 10 rounds — you're never quite where the attack expects you to be.",
    },
    "greater_potion_of_evasion": {
        "name": "Greater Potion of Evasion", "type": "consumable", "rarity": "very_rare",
        "price": 0, "weight": 0.5, "effect": "combat_buff", "buff_condition": "greater_evasive",
        "buff_duration_rounds": 10,
        "description": "The same disadvantage against attackers, plus a real +2 AC on top for 10 rounds — not just hard to hit, hard to even threaten.",
    },
    "vial_of_sluggishness": {
        "name": "Vial of Sluggishness", "type": "consumable", "rarity": "rare",
        "price": 0, "weight": 0.5, "effect": "combat_debuff", "debuff_condition": "slowed",
        "debuff_duration_rounds": 10,
        "description": "Thrown at a real enemy, not drunk — their own attacks roll with disadvantage for 10 rounds. A thick, syrupy liquid that seems to slow down whatever it splashes.",
    },
    "greater_vial_of_sluggishness": {
        "name": "Greater Vial of Sluggishness", "type": "consumable", "rarity": "very_rare",
        "price": 0, "weight": 0.5, "effect": "combat_debuff", "debuff_condition": "greater_slowed",
        "debuff_duration_rounds": 10,
        "description": "The same disadvantage, plus a real flat penalty on top of every attack roll they make for 10 rounds — genuinely, measurably worse in a fight, not just unlucky.",
    },
    "potion_of_fortification": {
        "name": "Potion of Fortification", "type": "consumable", "rarity": "rare",
        "price": 0, "weight": 0.5, "effect": "fortify", "fortify_ac_amount": 2, "fortify_max_stacks": 3,
        "buff_duration_rounds": 10,
        "description": "A real +2 AC for 10 rounds, layered on top of anything already worn — drink more for more layers, up to three (+6 AC total). A thick, mineral draught that seems to settle into the skin.",
    },
    "greater_potion_of_fortification": {
        "name": "Greater Potion of Fortification", "type": "consumable", "rarity": "very_rare",
        "price": 0, "weight": 0.5, "effect": "fortify", "fortify_ac_amount": 3, "fortify_max_stacks": 3, "fortify_greater": True,
        "buff_duration_rounds": 10,
        "description": "The same layered defense, brewed stronger — +3 AC per layer, up to three (+9 AC total) for 10 rounds.",
    },
    "potion_of_might": {
        "name": "Potion of Might", "type": "consumable", "rarity": "rare",
        "price": 0, "weight": 0.5, "effect": "combat_buff", "buff_condition": "empowered",
        "buff_pct_amount": 10, "buff_pct_cap": 60, "buff_duration_rounds": 10,
        "description": "A real +10% to every weapon and spell hit for 10 rounds — drink more for more, up to a real cap. A hot, metallic draught that makes your own strength feel unfamiliar for a moment.",
    },
    "greater_potion_of_might": {
        "name": "Greater Potion of Might", "type": "consumable", "rarity": "very_rare",
        "price": 0, "weight": 0.5, "effect": "combat_buff", "buff_condition": "empowered",
        "buff_pct_amount": 20, "buff_pct_cap": 60, "buff_duration_rounds": 10,
        "description": "The same real damage bonus, brewed to hit twice as hard per dose — +20% per drink toward the same cap.",
    },
    "vial_of_enfeeblement": {
        "name": "Vial of Enfeeblement", "type": "consumable", "rarity": "rare",
        "price": 0, "weight": 0.5, "effect": "combat_debuff", "debuff_condition": "weakened",
        "debuff_pct_amount": 10, "debuff_pct_cap": 60, "debuff_duration_rounds": 10,
        "description": "Thrown at a real enemy — a real -10% to their own weapon and spell damage for 10 rounds, stacking toward a cap the more you throw. A pale, oily liquid that visibly dulls whatever it touches.",
    },
    "greater_vial_of_enfeeblement": {
        "name": "Greater Vial of Enfeeblement", "type": "consumable", "rarity": "very_rare",
        "price": 0, "weight": 0.5, "effect": "combat_debuff", "debuff_condition": "weakened",
        "debuff_pct_amount": 20, "debuff_pct_cap": 60, "debuff_duration_rounds": 10,
        "description": "The same real damage penalty, brewed to bite twice as hard per dose — -20% per throw toward the same cap.",
    },
    # Per Coffee (2026-07-24): "add items like tents and cabins and
    # houses to reviving and healing characters to full" -- a stronger
    # alternative to Revivify (which only restores 1 HP): these fully
    # heal AND revive, tiered by both price and how many party members
    # they reach at once (see effect="heal_and_revive" in _do_use_item).
    # Prices per Coffee, 2026-07-24 ("make tents 100, cabins 1000, and
    # houses 10000"): a steep, deliberate order-of-magnitude jump per
    # tier, not a gradual one.
    "tent": {
        "name": "Tent", "type": "consumable", "rarity": "rare",
        "price": 100, "weight": 10, "effect": "heal_and_revive", "revive_targets": 1,
        "note": "A night under real canvas mends more than a potion ever could.",
        "description": "Fully heals and revives 1 fallen party member to full HP. A night under real canvas mends more than a potion ever could.",
    },
    "cabin": {
        "name": "Cabin", "type": "consumable", "rarity": "rare",
        "price": 1000, "weight": 20, "effect": "heal_and_revive", "revive_targets": 3,
        "note": "Four walls and a hearth -- room enough for the whole party to actually rest.",
        "description": "Fully heals and revives up to 3 fallen party members to full HP. Four walls and a hearth -- room enough for the whole party to actually rest.",
    },
    "house": {
        "name": "House", "type": "consumable", "rarity": "legendary",
        "price": 10000, "weight": 50, "effect": "heal_and_revive", "revive_targets": None,
        "note": "A real roof over everyone's head. Whatever happened out there, it stays outside.",
        "description": "Fully heals and revives the entire party to full HP. A real roof over everyone's head. Whatever happened out there, it stays outside.",
    },
    # Real late-game gold sink (2026-09-14, per Coffee: gold sinks
    # topped out at House's 10,000g while the final story quest alone
    # grants 100,000g, per project_economy_market_audit_2026_09_13 --
    # "just add higher house tiers... proportionally better effects").
    # Manor/Castle reuse House's exact heal_and_revive shape (never a
    # new effect type) with two new, real, additive flags bot.py's own
    # heal_and_revive branch checks for: restoring every present party
    # member's spell slots too (the one other real, DB-backed resource
    # nothing outside a full rest normally restores at once), and, at
    # the top tier, a real bonus potion restock.
    "manor": {
        "name": "Manor", "type": "consumable", "rarity": "legendary",
        "price": 25000, "weight": 60, "effect": "heal_and_revive", "revive_targets": None,
        "restore_spell_slots_too": True,
        "note": "A real staffed household -- wounds mended, minds rested, every spell ready again.",
        "description": "Fully heals and revives the entire party to full HP, and restores everyone's spell slots to full. A real staffed household -- wounds mended, minds rested, every spell ready again.",
    },
    "castle": {
        "name": "Castle", "type": "consumable", "rarity": "legendary",
        "price": 100000, "weight": 80, "effect": "heal_and_revive", "revive_targets": None,
        "restore_spell_slots_too": True, "bonus_potion_grant": {"greater_healing_potion": 5},
        "note": "The whole party, fully restored, and sent back out the gate with a real war chest of potions.",
        "description": "Fully heals and revives the entire party to full HP, restores everyone's spell slots to full, and stocks the party with 5 Greater Healing Potions. The whole party, fully restored, and sent back out the gate with a real war chest of potions.",
    },
    "rations": {
        "name": "Rations (1 day)", "type": "consumable", "rarity": "common",
        "price": 2, "weight": 2, "effect": "none",
        "description": "Hardtack, dried meat, a little salt. Filling in the way that word technically means.",
    },
    # Cooking's own real scaling ladder (2026-09-10, per Coffee: the same
    # "flatlines early" audit that found Alchemy's rebirth gap above also
    # found Cooking's -- only cooked_fish/rations existed (both DC <=10,
    # neither heals), nothing to ever grind toward. Reuses the fully-wired
    # generic "heal"/"heal_dice" effect (zero new bot.py code needed,
    # unlike Alchemy's new permanent_stat_increase) at the same flat-HP
    # magnitudes healing_potion/greater_healing_potion/
    # supreme_healing_potion already established for HP scaling into the
    # thousands with rebirths (100/1,000/10,000) -- own cooking-flavored
    # items, not the same ids, so the two professions stay distinct.
    # banquet_of_the_reborn's gate mirrors Blacksmithing/Alchemy's own
    # ladder shape, on cooking's real home guild (guilds.
    # GUILD_PERMANENT_PROFESSION's adventurers_guild: ["fishing", "cooking"]).
    "hearty_stew": {
        "name": "Hearty Stew", "type": "consumable", "rarity": "common",
        "price": 20, "weight": 1, "effect": "heal", "heal_dice": "1d1+99",
        "description": "Heals 1d1+99 HP (100 flat). Whatever's in the pot, it's enough to put real color back in your face.",
    },
    "travelers_feast": {
        "name": "Traveler's Feast", "type": "consumable", "rarity": "uncommon",
        "price": 90, "weight": 2, "effect": "heal", "heal_dice": "1d1+999",
        "description": "Heals 1d1+999 HP (1,000 flat). A real spread, cooked properly over a real fire — worth the extra time it takes.",
    },
    "banquet_of_the_reborn": {
        "name": "Banquet of the Reborn", "type": "consumable", "rarity": "rare",
        "price": 400, "weight": 3, "effect": "heal", "heal_dice": "1d1+9999",
        "description": "Heals 1d1+9999 HP (10,000 flat). Cooked the way the Adventurers' Guild teaches only its own who've already died once and come back.",
    },
    "torch": {"name": "Torch", "type": "consumable", "rarity": "common", "price": 1, "weight": 1, "effect": "light", "description": "Pitch-wrapped wood, ready to catch. Burns longer than it has any right to."},
    "ale": {
        "name": "Mug of Ale", "type": "consumable", "rarity": "common",
        "price": 1, "weight": 1, "effect": "none",
        "note": "Grimsby's own brew. Doesn't heal anything, but it isn't meant to.",
        "description": "Grimsby's own brew. Doesn't heal anything, but it isn't meant to.",
    },

    # --- Scrolls (single-use spells for non-casters — or anyone, once bought) ---
    "scroll_magic_missile": {
        "name": "Scroll of Magic Missile", "type": "scroll", "rarity": "common",
        "price": 30, "weight": 0.1, "spell": "magic_missile",
        "description": "Deals 1d4+1 force damage (always hits). The ink shifts faintly on the page, like it's still deciding where to strike.",
    },
    "scroll_fireball": {
        "name": "Scroll of Fireball", "type": "scroll", "rarity": "rare",
        "price": 300, "weight": 0.1, "spell": "fireball",
        "description": "Deals 8d6 fire damage (Dexterity save for half). Warm to the touch even rolled up. Whoever wrote this one meant it.",
    },
    "scroll_cure_wounds": {
        "name": "Scroll of Cure Wounds", "type": "scroll", "rarity": "common",
        "price": 35, "weight": 0.1, "spell": "cure_wounds",
        "description": "Heals 1d8+2 HP. The handwriting is steadier than most healing scrolls bother to be.",
    },
    "scroll_revivify": {
        "name": "Scroll of Revivify", "type": "scroll", "rarity": "rare",
        "price": 350, "weight": 0.1, "spell": "revivify",
        "note": "Brings a fallen ally back from death itself, at real cost — not to be used lightly.",
        "description": "Revives a fallen ally at 5% of their max HP. Brings a fallen ally back from death itself, at real cost — not to be used lightly.",
    },
    "scroll_shield": {
        "name": "Scroll of Shield", "type": "scroll", "rarity": "uncommon",
        "price": 60, "weight": 0.1, "spell": "shield",
        "description": "A short scroll, meant to be read fast — there's never much time to spare when you need it.",
    },
    "scroll_bless": {
        "name": "Scroll of Bless", "type": "scroll", "rarity": "uncommon",
        "price": 55, "weight": 0.1, "spell": "bless",
        "description": "The kind of scroll a priest presses into your hand and tells you to save.",
    },
    "scroll_invisibility": {
        "name": "Scroll of Invisibility", "type": "scroll", "rarity": "rare",
        "price": 175, "weight": 0.1, "spell": "invisibility",
        "description": "The parchment is faintly hard to look straight at, even before it's read.",
    },
    "scroll_lightning_bolt": {
        "name": "Scroll of Lightning Bolt", "type": "scroll", "rarity": "rare",
        "price": 275, "weight": 0.1, "spell": "lightning_bolt",
        "description": "Deals 8d6 lightning damage (Dexterity save for half). It crackles faintly against your fingers, like it hasn't fully settled since it was written.",
    },

    # Elemental scroll expansion, Tier 1 (2026-09-08, task #3, per
    # Coffee: "We want more elemental scrolls available"). Same real
    # level-3/~8d(6-8) shape as Scroll of Fireball/Lightning Bolt above,
    # just the other 7 real damage types (rules.combat.apply_damage_
    # type_modifier's own vocabulary) that had no scroll at all before
    # this. Buyable, same as the two originals -- ordinary market/find
    # content, not gated to harder dungeons (that's the Tier-2 "Greater"
    # scrolls below).
    "scroll_ice_storm": {
        "name": "Scroll of Ice Storm", "type": "scroll", "rarity": "rare",
        "price": 260, "weight": 0.1, "spell": "ice_storm",
        "description": "Deals 6d8 cold damage (Dexterity save for half). The parchment is cold enough to sting bare fingers.",
    },
    "scroll_mudslide": {
        "name": "Scroll of Mudslide", "type": "scroll", "rarity": "rare",
        "price": 250, "weight": 0.1, "spell": "mudslide",
        "description": "Deals 5d8 earth damage (Strength save for half). Smells faintly of wet clay, however it's been stored.",
    },
    "scroll_force_lance": {
        "name": "Scroll of the Force Lance", "type": "scroll", "rarity": "rare",
        "price": 275, "weight": 0.1, "spell": "force_lance",
        "description": "Deals 8d6 force damage (Dexterity save for half). The page hums faintly, like it doesn't like being held.",
    },
    "scroll_bone_spear": {
        "name": "Scroll of the Bone Spear", "type": "scroll", "rarity": "rare",
        "price": 275, "weight": 0.1, "spell": "bone_spear",
        "description": "Deals 8d6 necrotic damage (Constitution save for half). Written, unmistakably, in something other than ink.",
    },
    "scroll_toxic_cloud": {
        "name": "Scroll of the Toxic Cloud", "type": "scroll", "rarity": "rare",
        "price": 275, "weight": 0.1, "spell": "toxic_cloud",
        "description": "Deals 8d6 poison damage (Constitution save for half). Best held at arm's length before it's read.",
    },
    "scroll_mind_spike": {
        "name": "Scroll of the Mind Spike", "type": "scroll", "rarity": "rare",
        "price": 275, "weight": 0.1, "spell": "mind_spike",
        "description": "Deals 8d6 psychic damage (Wisdom save for half). Reading it silently is somehow louder than reading it aloud.",
    },
    "scroll_radiant_lance": {
        "name": "Scroll of the Radiant Lance", "type": "scroll", "rarity": "rare",
        "price": 275, "weight": 0.1, "spell": "radiant_lance",
        "description": "Deals 8d6 radiant damage (Dexterity save for half). Faint light leaks from between its folds even rolled shut.",
    },

    # Elemental scroll expansion, Tier 2: "Greater" scrolls (2026-09-08,
    # task #3, per Coffee: "the next lvl of scrools shud be avaialbe in
    # harder dungeons as a find, steal, or loot"). Deliberately NOT in
    # any shop inventory and NOT craftable -- same "left as a real find
    # only" convention scroll_cure_wounds's own RECIPES comment already
    # documents for Fireball/Revivify/etc, just a full tier stronger.
    # Distribution lives in rules/labyrinth.py (checkpoint vault "find")
    # and bot.py (miniboss/boss "loot", Labyrinth steal-fallback
    # "steal") -- all three gated to floor > 25, past the game's first
    # segment band. One per real damage type, all 9 -- fire/lightning/
    # cold/earth/necrotic/radiant/poison reuse existing high-tier spells
    # unchanged (Flame Strike/Cyclone/Cone of Cold/Meteor/Voidcall/
    # Starfall Lance/Insect Plague), force/psychic use the two new
    # spells added alongside these.
    "greater_scroll_fire": {
        "name": "Greater Scroll of Flame Strike", "type": "scroll", "rarity": "epic",
        "price": 0, "weight": 0.1, "spell": "flame_strike",
        "description": "Deals 8d6 fire damage (Dexterity save for half). The page itself looks half-charred, yet never quite burns through.",
    },
    "greater_scroll_lightning": {
        "name": "Greater Scroll of the Cyclone", "type": "scroll", "rarity": "epic",
        "price": 0, "weight": 0.1, "spell": "cyclone",
        "description": "Deals 6d8 lightning damage (Dexterity save for half). It practically vibrates in your hand.",
    },
    "greater_scroll_cold": {
        "name": "Greater Scroll of the Cone of Cold", "type": "scroll", "rarity": "epic",
        "price": 0, "weight": 0.1, "spell": "cone_of_cold",
        "description": "Deals 8d8 cold damage (Dexterity save for half). Frost creeps across whatever it touches.",
    },
    "greater_scroll_earth": {
        "name": "Greater Scroll of the Meteor", "type": "scroll", "rarity": "epic",
        "price": 0, "weight": 0.1, "spell": "meteor",
        "description": "Deals 9d8 earth damage (Dexterity save for half). Impossibly heavy for a single sheet of parchment.",
    },
    "greater_scroll_force": {
        "name": "Greater Scroll of Force Cataclysm", "type": "scroll", "rarity": "epic",
        "price": 0, "weight": 0.1, "spell": "force_cataclysm",
        "description": "Deals 9d8 force damage (Dexterity save for half). The air around it seems to bend slightly out of true.",
    },
    "greater_scroll_necrotic": {
        "name": "Greater Scroll of Voidcall", "type": "scroll", "rarity": "epic",
        "price": 0, "weight": 0.1, "spell": "voidcall",
        "description": "Deals 10d8 necrotic damage (Constitution save for half). Cold in a way that has nothing to do with temperature.",
    },
    "greater_scroll_poison": {
        "name": "Greater Scroll of the Insect Plague", "type": "scroll", "rarity": "epic",
        "price": 0, "weight": 0.1, "spell": "insect_plague",
        "description": "Deals 4d10 poison damage (Constitution save for half). Something inside it is very faintly buzzing.",
    },
    "greater_scroll_psychic": {
        "name": "Greater Scroll of Mind Shatter", "type": "scroll", "rarity": "epic",
        "price": 0, "weight": 0.1, "spell": "mind_shatter",
        "description": "Deals 9d8 psychic damage (Wisdom save for half). Reading even the title gives you a headache.",
    },
    "greater_scroll_radiant": {
        "name": "Greater Scroll of the Starfall Lance", "type": "scroll", "rarity": "epic",
        "price": 0, "weight": 0.1, "spell": "starfall_lance",
        "description": "Deals 8d8 radiant damage (Dexterity save for half). Too bright to look at directly, even folded shut.",
    },
    "scroll_summon_spirit": {
        # Real live question (2026-08-20, Coffee: "do summoning scrolls
        # have anything to do with the remnants? if not we should
        # remove them so it doesnt confuse the players") -- confirmed
        # NOT related: this is the item form of the real, separate
        # "Summon Lesser Spirit" spell (spells.py, effect="summon"),
        # which calls a temporary spirit ally into the current fight --
        # a genuine, working 5E-style conjuration spell, not dead
        # content. ai/intent_parser.py already disambiguates a real
        # bound Remnant's name from this spell correctly, so there was
        # never a code-level collision -- but the old generic name
        # "Scroll of Summoning" gave a player zero hint it's a
        # completely different mechanic from Remnant summoning, so
        # renamed/re-described to make that clear rather than removing
        # a real, functioning item.
        "name": "Scroll of the Lesser Spirit", "type": "scroll", "rarity": "rare",
        "price": 200, "weight": 0.1, "spell": "summon_lesser_spirit",
        "rebirth_scales_price": True,
        "description": "Calls a lesser spirit to fight at your side for the rest of the battle. The final line of the ritual text is written smaller, like the scribe wasn't sure they should include it. The spirit answering this particular ritual is never much stronger than a level-25 fighter, whoever reads it.",
    },
    # Tiers II-IV of the same family (2026-08-21, per Coffee: "create
    # caps so each scroll can only summon up to a certain level... make
    # it so players dont have access to the higher/better scroll unless
    # we find, steal, or buy them at higher level areas"). Each one's
    # own spell (spells.py) carries the real max_summon_level cap this
    # description promises. rebirth_scales_price (shop.buy_item) means
    # the gold cost itself climbs with the buyer's own rebirth_count --
    # the "let them evolve too, more expensive" half of the request.
    "scroll_summon_spirit_ii": {
        "name": "Scroll of the Spirit", "type": "scroll", "rarity": "rare",
        "price": 800, "weight": 0.1, "spell": "summon_spirit",
        "rebirth_scales_price": True,
        "description": "A more complete ritual than the lesser version -- the ink is darker, the folds more deliberate. Calls a real spirit ally, capable up to roughly a level-50 fighter's own strength.",
    },
    "scroll_summon_greater_spirit": {
        "name": "Scroll of the Greater Spirit", "type": "scroll", "rarity": "epic",
        "price": 2500, "weight": 0.1, "spell": "summon_greater_spirit",
        "rebirth_scales_price": True,
        "description": "Not something Ossian Vane's shelves ever carried -- this one turned up on something that used to be dangerous. Calls a genuinely powerful spirit ally, capable up to roughly a level-75 fighter's own strength.",
    },
    "scroll_summon_elder_spirit": {
        "name": "Scroll of the Elder Spirit", "type": "scroll", "rarity": "legendary",
        "price": 6000, "weight": 0.1, "spell": "summon_elder_spirit",
        "rebirth_scales_price": True,
        "description": "The ritual text doesn't read like it was written for a mortal hand. Calls an elder spirit ally at nearly the full strength this kind of summoning allows.",
    },

    # --- Rings, Amulets, Wondrous Items ---
    "ring_of_protection": {
        "name": "Ring of Protection", "type": "ring", "rarity": "rare",
        "price": 0, "weight": 0, "ac_bonus": 1,
        "note": "A faint shimmer surrounds it.",
        "description": "A faint shimmer surrounds it.",
    },
    "amulet_of_health": {
        "name": "Amulet of Health", "type": "amulet", "rarity": "rare",
        "price": 0, "weight": 0, "constitution_set": 19, "regen_bonus": 1,
        "note": "Your vitality feels different the moment you put it on.",
        "description": "Your vitality feels different the moment you put it on.",
    },
    "cloak_of_elvenkind": {
        "name": "Cloak of Elvenkind", "type": "wondrous", "rarity": "uncommon",
        "price": 0, "weight": 1, "stealth_advantage": True,
        "description": "The color of it shifts a little depending on where you're standing, never quite matching what it's actually near.",
    },
    "boots_of_the_winterlands": {
        "name": "Boots of the Winterlands", "type": "wondrous", "rarity": "uncommon",
        "price": 0, "weight": 1, "note": "Cold never seems to trouble the wearer.",
        "description": "Cold never seems to trouble the wearer.",
    },
    "bracers_of_the_steady_hand": {
        "name": "Bracers of the Steady Hand", "type": "wondrous", "rarity": "uncommon",
        "price": 90, "weight": 1, "note": "Your hands never shake, even when the rest of you wants to.",
        "description": "Your hands never shake, even when the rest of you wants to.",
    },
    "lantern_of_true_sight": {
        "name": "Lantern of True Sight", "type": "wondrous", "rarity": "rare",
        "price": 220, "weight": 2, "note": "Its flame burns a color no ordinary fire does, and shows things exactly as they are, not as they'd rather look.",
        "description": "Its flame burns a color no ordinary fire does, and shows things exactly as they are, not as they'd rather look.",
    },
    "ring_of_the_undertow": {
        "name": "Ring of the Undertow", "type": "ring", "rarity": "uncommon",
        "price": 110, "weight": 0, "ac_bonus": 1,
        "note": "Cold as riverwater no matter how long it's worn.",
        "description": "Cold as riverwater no matter how long it's worn.",
    },
    # Enchanters' Guild commissions (2026-07-25 - 2026-08-11): 3 real,
    # equippable rewards, never shop-buyable (price 0, same convention
    # as Amulet of Health/Ring of Protection). Originally granted via a
    # random-roll "commission" mechanic that Coffee later replaced with
    # the real guild tier ladder (rules/crafting.py's ENCHANT_RECIPES) --
    # left in the catalog since existing characters may already own
    # one, just no longer a live drop source.
    "band_of_ember": {
        "name": "Band of Ember", "type": "ring", "rarity": "uncommon",
        "price": 0, "weight": 0, "regen_bonus": 1,
        "note": "Warm to the touch, like a coal that never quite goes out.",
        "description": "Warm to the touch, like a coal that never quite goes out.",
    },
    "sigil_of_the_deep": {
        "name": "Sigil of the Deep", "type": "amulet", "rarity": "rare",
        "price": 0, "weight": 0, "regen_bonus": 2, "ac_bonus": 1,
        "note": "Carved from something that was never meant to see the surface.",
        "description": "Carved from something that was never meant to see the surface.",
    },
    "crown_of_the_unmoored": {
        "name": "Crown of the Unmoored", "type": "wondrous", "rarity": "very_rare",
        "price": 0, "weight": 1, "regen_bonus": 2, "ac_bonus": 2,
        "note": "It doesn't quite sit still on your head, like it's listening for something.",
        "description": "It doesn't quite sit still on your head, like it's listening for something.",
    },

    # Hollow Verge dungeon rewards (2026-07-25, rebirth-1 gated content --
    # see campaign.json's hollow_verge_* locations/quests). Quest-only,
    # never shop-buyable, same price-0 convention as the guild commissions
    # above.
    "verge_ashbound_band": {
        "name": "Verge-Ashbound Band", "type": "ring", "rarity": "rare",
        "price": 0, "weight": 0, "regen_bonus": 2, "ac_bonus": 1,
        "note": "Ash that never finished falling, cooled into a ring shape and left to be found.",
        "description": "Ash that never finished falling, cooled into a ring shape and left to be found.",
    },
    "wardens_reprieve": {
        "name": "Warden's Reprieve", "type": "wondrous", "rarity": "very_rare",
        "price": 0, "weight": 1, "regen_bonus": 3,
        "note": "Whatever waited that long to be defeated leaves behind something that knows how to wait, too.",
        "description": "Whatever waited that long to be defeated leaves behind something that knows how to wait, too.",
    },

    # The Wordless Choir dungeon rewards (2026-07-25, rebirth-2 gated
    # content -- see campaign.json's wordless_choir_* locations/quests).
    # Quest-only, never shop-buyable, same convention as the Hollow
    # Verge rewards above -- a clear step up in power for a deeper gate.
    "echo_bound_signet": {
        "name": "Echo-Bound Signet", "type": "ring", "rarity": "rare",
        "price": 0, "weight": 0, "regen_bonus": 3, "ac_bonus": 1,
        "note": "Whatever it echoes back was never quite what you said.",
        "description": "Whatever it echoes back was never quite what you said.",
    },
    "the_undertones_hum": {
        "name": "The Undertone's Hum", "type": "wondrous", "rarity": "legendary",
        "price": 0, "weight": 1, "regen_bonus": 4, "ac_bonus": 2,
        "note": "It doesn't make a sound. You just stop, for a moment, being able to not hear it.",
        "description": "It doesn't make a sound. You just stop, for a moment, being able to not hear it.",
    },

    # The Unbegun dungeon rewards (2026-07-25, rebirth-3 gated content,
    # the true final boss -- see campaign.json's unmoored_isle_* /
    # the_unbegun_reckoning). Quest-only, never shop-buyable. The
    # Unbegun's Crown is deliberately the strongest item in the game.
    "hollow_watchers_eye": {
        "name": "Hollow Watcher's Eye", "type": "amulet", "rarity": "rare",
        "price": 0, "weight": 0, "regen_bonus": 3, "ac_bonus": 2,
        "note": "It sees exactly as well with its eyes closed.",
        "description": "It sees exactly as well with its eyes closed.",
    },
    "the_unbegun_crown": {
        "name": "The Unbegun's Crown", "type": "wondrous", "rarity": "legendary",
        "price": 0, "weight": 1, "regen_bonus": 5, "ac_bonus": 3,
        "note": "Worn by whatever was here before there was a story to tell about it.",
        "description": "Worn by whatever was here before there was a story to tell about it.",
    },

    # Hollow Verge expansion rewards (2026-07-25, per Coffee: "bigger
    # expansion" -- new mid-path/side-branch nodes added to the
    # existing rebirth-1 dungeon). Quest-only, same convention as every
    # other dungeon reward.
    "bound_wraiths_chain": {
        "name": "Bound Wraith's Chain", "type": "ring", "rarity": "rare",
        "price": 0, "weight": 0, "regen_bonus": 2,
        "note": "It was never really what was holding the wraith in place.",
        "description": "It was never really what was holding the wraith in place.",
    },
    "forgotten_nooks_trinket": {
        "name": "Forgotten Nook's Trinket", "type": "amulet", "rarity": "uncommon",
        "price": 0, "weight": 0, "ac_bonus": 1,
        "note": "Small enough to have been overlooked. That's probably why it survived.",
        "description": "Small enough to have been overlooked. That's probably why it survived.",
    },

    # Wordless Choir expansion rewards (2026-07-25, same "bigger
    # expansion" pass as the Hollow Verge rewards above).
    "resonance_bound_ring": {
        "name": "Resonance-Bound Ring", "type": "ring", "rarity": "rare",
        "price": 0, "weight": 0, "regen_bonus": 3, "ac_bonus": 1,
        "note": "It answers back half a second after you put it on. It has been for a while now.",
        "description": "It answers back half a second after you put it on. It has been for a while now.",
    },
    "unscheduled_chord_charm": {
        "name": "Unscheduled Chord Charm", "type": "amulet", "rarity": "uncommon",
        "price": 0, "weight": 0, "ac_bonus": 1,
        "note": "Hums the start of a tune it never finishes.",
        "description": "Hums the start of a tune it never finishes.",
    },

    # The Unbegun expansion rewards (2026-07-25, same "bigger expansion"
    # pass as the other two dungeons above).
    "frayed_edge_band": {
        "name": "Frayed Edge Band", "type": "ring", "rarity": "very_rare",
        "price": 0, "weight": 0, "regen_bonus": 4, "ac_bonus": 2,
        "note": "Every thread it's woven from stops at the exact same point. None of them were ever going to finish.",
        "description": "Every thread it's woven from stops at the exact same point. None of them were ever going to finish.",
    },
    "disconnected_steps_charm": {
        "name": "Disconnected Step's Charm", "type": "amulet", "rarity": "uncommon",
        "price": 0, "weight": 0, "ac_bonus": 1,
        "note": "Doesn't lead anywhere the rest of your gear goes. Useful anyway.",
        "description": "Doesn't lead anywhere the rest of your gear goes. Useful anyway.",
    },

    # Goblin Warrens expansion rewards (2026-07-25, same "bigger
    # expansion" pass, extended to the wider world per Coffee: "do this
    # for every dungeon in the game" -- this is an early-game area, so
    # rewards stay proportionately modest next to the rebirth-dungeon
    # gear above).
    "deep_larders_charm": {
        "name": "Deep Larder's Charm", "type": "amulet", "rarity": "uncommon",
        "price": 0, "weight": 0, "regen_bonus": 1,
        "note": "Smells faintly of everything it was stacked next to.",
        "description": "Smells faintly of everything it was stacked next to.",
    },
    "collapsed_tunnels_keepsake": {
        "name": "Collapsed Tunnel's Keepsake", "type": "amulet", "rarity": "common",
        "price": 0, "weight": 0, "ac_bonus": 1,
        "note": "Whoever it belonged to before the cave-in never came back for it.",
        "description": "Whoever it belonged to before the cave-in never came back for it.",
    },

    # Sunken Root Caverns expansion rewards (2026-07-25, same pass).
    "hollow_wellsprings_charm": {
        "name": "Hollow Wellspring's Charm", "type": "amulet", "rarity": "uncommon",
        "price": 0, "weight": 0, "regen_bonus": 1,
        "note": "Always feels faintly damp, no matter how long it's been out of the water.",
        "description": "Always feels faintly damp, no matter how long it's been out of the water.",
    },
    "side_pools_trinket": {
        "name": "Side Pool's Trinket", "type": "amulet", "rarity": "common",
        "price": 0, "weight": 0, "ac_bonus": 1,
        "note": "Small enough that whatever was guarding it barely noticed it was gone.",
        "description": "Small enough that whatever was guarding it barely noticed it was gone.",
    },

    # Stonearch Bridge (gorge) expansion rewards (2026-07-25, same pass).
    "deep_currents_charm": {
        "name": "Deep Current's Charm", "type": "amulet", "rarity": "uncommon",
        "price": 0, "weight": 0, "regen_bonus": 1,
        "note": "Always feels like it's being pulled gently in one direction.",
        "description": "Always feels like it's being pulled gently in one direction.",
    },
    "undertows_band": {
        "name": "Undertow's Band", "type": "ring", "rarity": "rare",
        "price": 0, "weight": 0, "regen_bonus": 1, "ac_bonus": 1,
        "note": "Everything that's ever been dragged down here eventually stops fighting the pull. This didn't.",
        "description": "Everything that's ever been dragged down here eventually stops fighting the pull. This didn't.",
    },
    "silked_nooks_keepsake": {
        "name": "Silked Nook's Keepsake", "type": "amulet", "rarity": "common",
        "price": 0, "weight": 0, "ac_bonus": 1,
        "note": "Still faintly sticky. Best not to think about why.",
        "description": "Still faintly sticky. Best not to think about why.",
    },

    # Greymoor Downs expansion rewards (2026-07-25, same pass -- the
    # Lonely Cairn / Below the Cairn lore location was deliberately
    # left untouched, no reward items added there).
    "vantage_belows_charm": {
        "name": "Beneath-the-Vantage Charm", "type": "amulet", "rarity": "uncommon",
        "price": 0, "weight": 0, "regen_bonus": 1,
        "note": "Still smells faintly of wind, even indoors.",
        "description": "Still smells faintly of wind, even indoors.",
    },
    "tower_cellars_keepsake": {
        "name": "Tower Cellar's Keepsake", "type": "amulet", "rarity": "common",
        "price": 0, "weight": 0, "ac_bonus": 1,
        "note": "Whatever it was keeping safe down there, it isn't anymore.",
        "description": "Whatever it was keeping safe down there, it isn't anymore.",
    },
    "barrow_depths_band": {
        "name": "Barrow Depths Band", "type": "ring", "rarity": "uncommon",
        "price": 0, "weight": 0, "ac_bonus": 1,
        "note": "The cold campsite up top was always bait. This was never meant to be found.",
        "description": "The cold campsite up top was always bait. This was never meant to be found.",
    },

    # Whispering Wood expansion rewards (2026-07-25, same pass -- the
    # Mossy Creek, Elder Glen, and Root Hollow gathering/lore spots were
    # deliberately left untouched, no combat or rewards added there).
    "stray_dens_keepsake": {
        "name": "Stray Den's Keepsake", "type": "amulet", "rarity": "common",
        "price": 0, "weight": 0, "ac_bonus": 1,
        "note": "Pushed out of the main pack's territory, same as whatever carried it here.",
        "description": "Pushed out of the main pack's territory, same as whatever carried it here.",
    },
    "root_wroughts_charm": {
        "name": "Root-Wrought Charm", "type": "amulet", "rarity": "uncommon",
        "price": 0, "weight": 0, "regen_bonus": 1,
        "note": "Every join is the same angle. Nothing about it grew.",
        "description": "Every join is the same angle. Nothing about it grew.",
    },
    "deep_root_wardens_band": {
        "name": "Deep Root Warden's Band", "type": "ring", "rarity": "rare",
        "price": 0, "weight": 0, "ac_bonus": 1, "regen_bonus": 1,
        "note": "Built for exactly one chamber, and nowhere else -- and it still remembers which one.",
        "description": "Built for exactly one chamber, and nowhere else -- and it still remembers which one.",
    },

    # Glimmerdeep Grotto expansion rewards (2026-07-25, same pass --
    # the last of the 6 areas Coffee asked to expand this pass).
    "buried_glows_band": {
        "name": "Buried Glow's Band", "type": "ring", "rarity": "rare",
        "price": 0, "weight": 0, "ac_bonus": 1, "regen_bonus": 1,
        "note": "Glows exactly like the crystals up above. It isn't one.",
        "description": "Glows exactly like the crystals up above. It isn't one.",
    },
    "dim_hollows_keepsake": {
        "name": "Dim Hollow's Keepsake", "type": "amulet", "rarity": "common",
        "price": 0, "weight": 0, "ac_bonus": 1,
        "note": "Just enough light left in it to see by.",
        "description": "Just enough light left in it to see by.",
    },

    # The true hidden final boss (2026-07-25, per Coffee: a classic-
    # JRPG-style ultimate secret superboss, gated on 10 rebirths AND having
    # already defeated every other secret final boss in the game). The
    # single strongest item that exists -- upgraded to real mythic tier
    # (2026-08-02, magic item system Phase 6) with real mechanical
    # effects to match its own "single strongest item" billing, rather
    # than leaving it a plain legendary-tier stat stick the moment
    # actual mythic gear started existing. Hand-authored, never rolled
    # -- same shape a generated mythic item's own affixes produce
    # (ignores_resistance/immunities), set by hand instead, so combat
    # code treats both identically. equip_requirement is almost
    # decorative here (you can only ever receive this by having already
    # beaten the exact content it also gates on), but kept for
    # consistency with every other mythic item.
    "pandoras_answer": {
        "name": "Pandora's Answer", "type": "wondrous", "rarity": "mythic",
        "price": 0, "weight": 0, "regen_bonus": 10, "ac_bonus": 5,
        "ignores_resistance": True, "immunities": ["necrotic", "psychic"],
        "equip_requirement": {"any_of": [{"kind": "completed_quest", "value": "the_unaskeds_reckoning"}]},
        "note": "Not a weapon. Not really armor, either. Just an answer, finally given to whoever was willing to ask the question one more time than everyone before them.",
        "description": "Not a weapon. Not really armor, either. Just an answer, finally given to whoever was willing to ask the question one more time than everyone before them.",
    },

    # --- Maps (task #141: buyable/discoverable, partial-reveal only --
    # see bot.py's _do_use_item "map" branch and _do_show_map's
    # revealed-but-unvisited rendering. reveals_layer/reveals_count are
    # what makes this a genuine partial reveal: only that many random
    # not-yet-visited/revealed location NAMES from that one layer, never
    # the whole map, never connection details -- those stay a real
    # spoiler you still have to walk to and see for yourself.) ---
    "weathered_surface_map": {
        "name": "Weathered Surface Map", "type": "map", "rarity": "uncommon",
        "price": 35, "weight": 0.2, "reveals_layer": "surface", "reveals_count": 3,
        "description": "Water-stained at the corners, marked in a hand that clearly knew this ground well.",
    },
    "tattered_underground_chart": {
        "name": "Tattered Underground Chart", "type": "map", "rarity": "rare",
        "price": 60, "weight": 0.2, "reveals_layer": "underground", "reveals_count": 3,
        "description": "Torn along one edge, the surviving ink tracing passages that never see daylight.",
    },
    # Bonus vault dungeons (2026-08-30): each vault's own findable map,
    # same real reveals_layer/reveals_count mechanic as the two above --
    # never sold, found only via the Molten Sentinel/Elder Bramble Husk
    # defeat-quest reward inside each vault.
    "cinder_marked_chart": {
        "name": "Cinder-Marked Chart", "type": "map", "rarity": "rare",
        "price": 0, "weight": 0.2, "reveals_layer": "underground", "reveals_count": 3,
        "description": "Scorched at the edges but still legible -- whoever drew this knew the Wrathflame Vault well enough to map it and get out again.",
    },
    "root_bound_survey": {
        "name": "Root-Bound Survey", "type": "map", "rarity": "rare",
        "price": 0, "weight": 0.2, "reveals_layer": "underground", "reveals_count": 3,
        "description": "Drawn on bark instead of parchment, the ink following the grain like it grew there.",
    },

    # Chapter 3 expansion (2026-08-30): The Last Glyph's real unique
    # reward, ties directly into Phase 0's new Earth element.
    "the_last_glyphs_seal": {
        "name": "The Last Glyph's Seal", "type": "wondrous", "rarity": "very_rare",
        "price": 0, "weight": 1,
        "elemental_resistances": [{"damage_type": "earth", "value": 50}],
        "note": "Whatever kept repeating itself through that whole buried city finally stopped, and left this behind instead.",
        "description": "Whatever kept repeating itself through that whole buried city finally stopped, and left this behind instead.",
    },
    # Chapter 4 expansion (2026-08-30): The Drowned Reflection's real
    # unique reward.
    "the_drowned_reflections_lens": {
        "name": "The Drowned Reflection's Lens", "type": "wondrous", "rarity": "very_rare",
        "price": 0, "weight": 1,
        "elemental_resistances": [{"damage_type": "cold", "value": 50}],
        "note": "Looking through it shows things exactly as they are, which turns out to be the rarer trick.",
        "description": "Looking through it shows things exactly as they are, which turns out to be the rarer trick.",
    },
    # Chapter 5 expansion (2026-08-30): The Paymaster's Shadow's real
    # unique reward.
    "the_paymasters_shadows_ledger": {
        "name": "The Paymaster's Shadow's Ledger", "type": "wondrous", "rarity": "very_rare",
        "price": 0, "weight": 1,
        "elemental_resistances": [{"damage_type": "force", "value": 50}],
        "note": "The very last entry is finally crossed out. Whatever it was owed, it isn't anymore.",
        "description": "The very last entry is finally crossed out. Whatever it was owed, it isn't anymore.",
    },
    # Chapter 6 expansion (2026-08-30): The Keeping Current's real
    # unique reward.
    "the_keeping_currents_seal": {
        "name": "The Keeping Current's Seal", "type": "wondrous", "rarity": "very_rare",
        "price": 0, "weight": 1,
        "elemental_resistances": [{"damage_type": "poison", "value": 50}],
        "note": "Whatever it was keeping finally stopped needing to be kept.",
        "description": "Whatever it was keeping finally stopped needing to be kept.",
    },
    # Chapter 7 expansion (2026-08-30): The Keep's Warden's real unique
    # reward.
    "the_keeps_wardens_crown": {
        "name": "The Keep's Warden's Crown", "type": "wondrous", "rarity": "very_rare",
        "price": 0, "weight": 1,
        "elemental_resistances": [{"damage_type": "lightning", "value": 50}],
        "note": "Whoever it was warding this crossing for never actually arrived. It kept watch anyway.",
        "description": "Whoever it was warding this crossing for never actually arrived. It kept watch anyway.",
    },
    # Chapter 8 expansion (2026-08-30): The Downs' Last Watch's real
    # unique reward -- the final new item of the Chapter 3-8 expansion.
    "the_downs_last_watchs_seal": {
        "name": "The Downs' Last Watch's Seal", "type": "wondrous", "rarity": "very_rare",
        "price": 0, "weight": 1,
        "elemental_resistances": [{"damage_type": "necrotic", "value": 50}],
        "note": "The watch is finally, genuinely over. Whatever it was waiting to be relieved by, it wasn't this -- and it doesn't seem to mind.",
        "description": "The watch is finally, genuinely over. Whatever it was waiting to be relieved by, it wasn't this -- and it doesn't seem to mind.",
    },

    # --- Recipe books (2026-08-11, per Coffee: real, buyable reference
    # items teaching a profession's real level-1 recipes -- "make basic,
    # make tiered books that will be available in the game as the
    # players progress. also buy basic Crafting book in the market. Only
    # show Lv 1 items tho (players must join guild for advanced
    # recipies)"). "teaches_profession" is read by bot._do_read_recipe_
    # book, which builds the actual recipe list live from rules.crafting.
    # RECIPES -- never ADVANCED_RECIPES/ENCHANT_RECIPES, so a book can
    # never leak guild-ladder content. Not consumed on use (see
    # bot._do_use_item's "book" branch) -- a reference book is reused. ---
    "cook_book_basic": {
        "name": "Cook Book", "type": "book", "rarity": "common",
        "price": 15, "weight": 1, "teaches_profession": "cooking",
        "description": "Grease-stained, dog-eared, and clearly used by someone who actually cooked from it.",
    },
    "herbalism_guide_basic": {
        "name": "Herbalism Guide", "type": "book", "rarity": "common",
        "price": 20, "weight": 1, "teaches_profession": "alchemy",
        "description": "Pressed leaves still mark a few of its pages, decades after whoever put them there.",
    },
    "crafting_book_basic": {
        "name": "Crafting Book", "type": "book", "rarity": "common",
        "price": 15, "weight": 1, "teaches_profession": "blacksmithing",
        "description": "Soot-smudged and warped from heat, like it's spent as much time by the forge as in a bag.",
    },
    # Advanced/guild-tier tier of the same book series (2026-09-08, task
    # #7, per Coffee: "add any recipies needed and make them available
    # in the books at lv 20 +") -- Coffee's own ORIGINAL 2026-08-11 ask
    # above already said "make tiered books... as the players progress,"
    # so this is the first real tier past "basic," not a reversal of the
    # "Only show Lv 1 items" rule. "teaches_advanced_profession" is a
    # real, DIFFERENT field from "teaches_profession" -- read by
    # bot._do_read_advanced_recipe_book, which lists ADVANCED_RECIPES/
    # ENCHANT_RECIPES (guild-ladder content the basic books' own
    # docstring explicitly says they can never leak) gated live, every
    # read, on the reader's own real level/guild/rebirth via the same
    # recipe_requirement_gate a real craft attempt uses.
    "grandmasters_forge_tome": {
        "name": "Grandmaster's Forge Tome", "type": "book", "rarity": "rare",
        "price": 150, "weight": 2, "teaches_advanced_profession": "blacksmithing",
        "description": "Bound in scorched iron plate; only someone who's actually stood at a real forge for years could make sense of it.",
    },
    # Alchemy/enchanting's own equivalent (2026-09-11, per Coffee: "have
    # these recipies been added to the books or are they guild only" --
    # confirmed they were guild-only tribal knowledge, no book at all
    # ever covered them). bot._do_read_advanced_recipe_book already
    # takes `profession` as a plain parameter -- it was never
    # blacksmithing-specific, just never given a second real book to
    # dispatch to. Reveals every ENCHANT_RECIPES entry tagged "alchemy"
    # (the whole Enchanters' Guild ladder included, since enchanting
    # itself stays alchemy's own identity, not blacksmithing's -- see
    # [[feedback_enchanting_stays_alchemy_not_blacksmithing]]) plus any
    # gated (min_level/requires_guild/min_rebirth) alchemy RECIPES entry,
    # same live recipe_requirement_gate check the Forge Tome already uses.
    "enchanters_grimoire": {
        "name": "Enchanters' Grimoire", "type": "book", "rarity": "rare",
        "price": 150, "weight": 2, "teaches_advanced_profession": "alchemy",
        "description": "The binding is warm to the touch, and the ink seems to shift slightly whenever you're not looking directly at the page.",
    },

    # --- Quest items (never sellable, never have a price) ---
    "waterlogged_journal": {
        "name": "Waterlogged Journal", "type": "quest_item", "rarity": "unique",
        "price": 0, "weight": 0.5,
        "description": "Most of the pages are ruined, swollen and stuck together. A few words still survive here and there.",
    },
    "shard_of_dim_light": {
        "name": "Shard of Dim Light", "type": "quest_item", "rarity": "unique",
        "price": 0, "weight": 0.1,
        "description": "A sliver of something that glows just barely enough to notice, and no brighter no matter how you turn it.",
    },
    # Dungeon redesign Phase 1 (2026-08-30, per Coffee: real locked
    # doors and a lock-and-key formula, not a straight corridor) --
    # Wrathflame Vault's first real key item, opening the warded door
    # from the Ember Hall hub into the Smoldering Stair branch.
    "the_cinder_key": {
        "name": "The Cinder Key", "type": "quest_item", "rarity": "unique",
        "price": 0, "weight": 0.1,
        "description": "Still faintly warm no matter how long it's carried, as if something in the vault remembers exactly where it belongs.",
    },
    # Dungeon redesign Phase 2 (2026-08-30) -- Deep Root Vault's own key
    # item, opening the resin-sealed root door from Spore Hollow into
    # the Sentinel Hollow branch.
    "the_last_seed": {
        "name": "The Last Seed", "type": "quest_item", "rarity": "unique",
        "price": 0, "weight": 0.1,
        "description": "Never sprouted, never rotted either -- it's been waiting for something for a very long time.",
    },
    # Dungeon redesign Phase 3 (2026-08-30) -- Sunken Root Caverns' own
    # key item, opening the silt-sealed door from Deep Tunnels into the
    # Silt Vault (a genuinely optional bonus side-branch, since the
    # dungeon's two existing main branches were already open before
    # this pass and real players had already started exploring them).
    "the_silt_key": {
        "name": "The Silt Key", "type": "quest_item", "rarity": "unique",
        "price": 0, "weight": 0.1,
        "description": "Caked in fine grey silt no amount of handling seems to wear off.",
    },
    # Dungeon redesign Phase 4 (2026-08-30) -- Goblin Warrens' own key
    # item, opening the scavenged iron grate from The Old Seam into The
    # Deep Stash.
    # Real live feature (2026-09-04, per Coffee: "you can have locked
    # doors requireing 'keys'... keys can be dropped by enemies or
    # found in another room"), Phase B of the combat-gating work. A
    # single, plain, REUSABLE key (unlike every key above, which is a
    # unique, hand-authored story item tied to one specific real door)
    # -- the Labyrinth and evolved dungeons regenerate a brand new key
    # gate on demand, so this same item id is placed and consumed over
    # and over rather than needing a fresh unique id invented per
    # floor. rarity is deliberately "common", not "unique" like the
    # named story keys above -- a player should never be blocked from
    # holding a second one.
    "labyrinth_floor_key": {
        "name": "A Tarnished Floor Key", "type": "quest_item", "rarity": "common",
        "price": 0, "weight": 0.1,
        "description": "Worn smooth by hands that needed it just as badly, somewhere else, some other time.",
    },
    # Real live feature (2026-09-04, per Coffee: "have the mini boss,
    # and boss and scatter them around, have it collected by battle
    # and by chests" -- one variation among the Labyrinth's existing
    # branch-gate rotation, not the main mechanic, per his own
    # follow-up: "use it in variations with the other mechanics").
    # Same reusable/common shape as labyrinth_floor_key just above --
    # a "Locked Rune Doorway" needs N of these rather than one unique
    # key, so this same item id is found and spent over and over.
    "labyrinth_rune": {
        "name": "A Labyrinth Rune", "type": "quest_item", "rarity": "common",
        "price": 0, "weight": 0.1,
        "description": "It hums faintly against your palm, in tune with something you haven't found yet.",
    },
    "the_vein_key": {
        "name": "The Vein Key", "type": "quest_item", "rarity": "unique",
        "price": 0, "weight": 0.1,
        "description": "Cut cleaner than anything else pulled out of this vein -- whoever made it wasn't a goblin.",
    },
    # Dungeon redesign Phase 5 (2026-08-30) -- Greymoor Downs' own key
    # item, opening the warded cellar door from The Cellar Hollow into
    # The Warden's Vault. Light-touch phase -- the Kess climax at the
    # dungeon's own entrance is completely untouched.
    "the_warden_key": {
        "name": "The Warden Key", "type": "quest_item", "rarity": "unique",
        "price": 0, "weight": 0.1,
        "description": "Heavier than it looks, like it was made to be noticed by whoever finally found it.",
    },
    # Dungeon redesign Phase 6 (2026-08-30) -- Unmoored Isle's own key
    # item, opening the drifting door from the hub into The Drifting
    # Vault.
    "the_drifting_key": {
        "name": "The Drifting Key", "type": "quest_item", "rarity": "unique",
        "price": 0, "weight": 0.1,
        "description": "Never quite settles in your hand, like it's still deciding whether it wants to be held.",
    },
    # Dungeon redesign Phase 7 (2026-08-30) -- Stonearch Bridge's own
    # key item, opening the rusted old gate from the bridge's own
    # entrance into The Old Vault.
    "the_old_watch_key": {
        "name": "The Old Watch Key", "type": "quest_item", "rarity": "unique",
        "price": 0, "weight": 0.1,
        "description": "Cold to the touch no matter the weather, like it's still standing its own watch.",
    },
    # Dungeon redesign Phase 8, FINAL (2026-08-30) -- The First City's
    # own key item, opening the sealed old archway from the city's
    # entrance into The Old Vault.
    "the_old_archive_key": {
        "name": "The Old Archive Key", "type": "quest_item", "rarity": "unique",
        "price": 0, "weight": 0.1,
        "description": "Etched with the same unfamiliar glyphs as the city's own entrance -- whoever cut it wasn't working from any script anyone above ground has ever read.",
    },
    "brass_key_no_lock": {
        "name": "Brass Key That Fits No Lock", "type": "quest_item", "rarity": "unique",
        "price": 0, "weight": 0.1,
        "description": "Worn smooth from use, though nothing you've found yet has ever turned for it.",
    },
    # Real live request (2026-08-26, per Coffee: evolve Kess the Bandit
    # from a bare random ambush into a real recurring antagonist arc,
    # via a real reveal quest from Borin Ironjaw). A quiet confirmation
    # of the blackthorn_raiders faction's own already-written hook
    # ("funded by something bigger than banditry," campaign.json) --
    # never invented from nothing, just made concrete.
    "torn_ledger_page": {
        "name": "Torn Ledger Page", "type": "quest_item", "rarity": "unique",
        "price": 0, "weight": 0.1,
        "description": "A single page torn from a Blackthorn Raiders' account book — tallies of coin far too "
                       "large for a roadside gang, paid out to a name written in a hand that isn't Kess's.",
    },

    # --- Crafting / trade materials ---
    "iron_ore": {"name": "Iron Ore", "type": "material", "rarity": "common", "price": 5, "weight": 2, "description": "A rough, heavy chunk, veined with metal not yet worth calling ore-grade."},
    "moonpetal": {"name": "Moonpetal Flower", "type": "material", "rarity": "uncommon", "price": 20, "weight": 0.05, "description": "Pale petals that seem to hold on to whatever light they last caught."},
    "silverleaf_herb": {"name": "Silverleaf Herb", "type": "material", "rarity": "common", "price": 4, "weight": 0.1, "description": "Thin, pale leaves with a faint metallic sheen along the edges."},
    "sulfur_dust": {"name": "Sulfur Dust", "type": "material", "rarity": "common", "price": 6, "weight": 0.1, "description": "A fine yellow powder that smells exactly as bad as you'd expect."},
    "raw_fish": {"name": "Raw Fish", "type": "material", "rarity": "common", "price": 3, "weight": 0.3, "description": "Still cold from the water. Best cooked before eating."},
    "wood": {"name": "Wood", "type": "material", "rarity": "common", "price": 2, "weight": 1, "description": "A few solid, unremarkable logs — good for a fire or a repair."},
    # Per Coffee (2026-08-10): "make crafting Ethers possible but
    # difficult, shud be rare herbs to craft this" -- a genuinely rare
    # material, gathered from exactly one real node (The Glimmering
    # Pool, campaign.json), used only by the advanced spell-tonic
    # recipes in rules/crafting.py.
    "glimmerdeep_moss": {"name": "Glimmerdeep Moss", "type": "material", "rarity": "rare", "price": 60, "weight": 0.05, "description": "A faint, cold light clings to it even out of the water. It never fully dries."},
    # Guild tier-5 capstone material (2026-08-11, per Coffee: cover every
    # guild tier from level 1 to end game, "Human Ascends to God"). Boss
    # drop only -- same GODSHARD_DROP_CHANCE-gated pattern as spell
    # tonics (see bot.py's _award_victory_xp), never sold in any shop.
    # Feeds exactly one recipe each in enchant_godsforged_ward and
    # godsforged_blade (rules/crafting.py), the true end of both guild
    # ladders.
    "godshard": {"name": "Godshard", "type": "material", "rarity": "mythic", "price": 0, "weight": 0.1, "description": "A splinter of something that was never meant to be small enough to hold. It doesn't feel like it belongs to this world's weight."},

    # --- Cooking (2026-07-15: raw_fish was gatherable but had zero
    # recipe using it -- a real cooking recipe below turns it, plus
    # wood for the fire, into an actually usable food item) ---
    "cooked_fish": {
        "name": "Cooked Fish", "type": "consumable", "rarity": "common",
        "price": 8, "weight": 0.3, "effect": "heal", "heal_dice": "1d4+1",
        "description": "Heals 1d4+1 HP. Simple, filling, and still warm off the fire.",
    },

    # --- Gathering tools (2026-07-16, per Coffee): fishing/lumberjacking/
    # mining each require owning the real tool for that trade -- herbalism
    # deliberately doesn't (picking a plant needs no tool), but Shears let
    # an herbalist harvest more than one plant per success. "required_for"
    # is the gathering skill this tool gates (see bot.py's _do_gather);
    # Shears instead uses "boosts_quantity_for" since it's optional, not
    # gating access at all.
    "fishing_pole": {"name": "Fishing Pole", "type": "tool", "rarity": "common", "price": 8, "weight": 2, "required_for": "fishing", "description": "A simple rod and line. Required to fish anywhere."},
    "bait": {"name": "Bait", "type": "tool", "rarity": "common", "price": 2, "weight": 0.1, "required_for": "fishing", "note": "A small tin of squirming earthworms and grubs, dug fresh from damp soil.", "description": "A small tin of squirming earthworms and grubs, dug fresh from damp soil."},
    "woodcutters_axe": {"name": "Woodcutter's Axe", "type": "tool", "rarity": "common", "price": 10, "weight": 4, "required_for": "lumberjacking", "description": "A well-worn axe, single-bladed and heavy in the head. Required to chop wood."},
    "pickaxe": {"name": "Pickaxe", "type": "tool", "rarity": "common", "price": 10, "weight": 5, "required_for": "mining", "description": "Iron-headed and solid. Required to mine ore."},
    "shears": {"name": "Shears", "type": "tool", "rarity": "common", "price": 6, "weight": 0.5, "boosts_quantity_for": "herbalism", "description": "Sharp, spring-hinged blades. Not required to gather herbs, but they let you harvest more per attempt."},
    # Task, per Coffee (2026-07-21): "add shovels to increase the
    # amount of bait we can get?! Have it use a dice roll." Same
    # optional, dice-rolled quantity-boost shape as Shears above, just
    # for bait_gathering instead of herbalism.
    "shovel": {"name": "Shovel", "type": "tool", "rarity": "common", "price": 5, "weight": 3, "boosts_quantity_for": "bait_gathering", "description": "A plain digging shovel. Not required to dig for bait, but it lets you turn up more per attempt."},
}


def get_item(item_id: str) -> dict | None:
    """
    Real per-instance magic items (2026-08-02 magic item system, per
    Coffee: "make loot real"): a generated/crafted item's id (e.g.
    "gi42") is never a key in the static ITEMS dict below -- it's a
    pointer into db.py's item_instances table instead. Falling back here,
    rather than teaching every call site about the new id scheme
    separately, is what lets equip/combat/shop/market/scrolls all keep
    working against a generated item completely unchanged -- they only
    ever call get_item(). Deferred import: db.py imports this module
    (`items as items_module`) at its own load time, so a top-level
    `import db` here would be circular; by the time get_item() is ever
    actually CALLED, both modules are already fully loaded, which is the
    standard way to break this exact kind of cycle without restructuring
    either file.
    """
    item = ITEMS.get(item_id)
    if item is not None:
        # Real live bug (2026-08-14/15, dev-topic screenshot reports: a
        # plain, hand-authored "Silvered Dagger" -- base damage_type
        # "silver" -- landing hits typed "poison" then later "fire" in
        # the SAME fight, on the same weapon). ITEMS.get() used to hand
        # back a LIVE reference into this module's own shared dict --
        # any code anywhere that ever mutated a get_item() result in
        # place (a reasonable thing to assume is safe; a generated
        # item's own materialize_item_instance below already returns a
        # fresh dict every call, so this was the one inconsistent
        # exception) would permanently corrupt that catalog entry for
        # every player, for the rest of the process's life, until
        # restart -- explaining why the exact SAME static id could show
        # two different damage types on different hits. The specific
        # mutating call site was never conclusively found despite an
        # extensive live-simulated hunt (multiple casters/rounds/
        # elemental procs, all clean) -- fixed at the root instead: this
        # now always returns an independent copy, exactly matching
        # materialize_item_instance's existing per-call-fresh-dict
        # behavior for generated items, closing the entire bug CLASS
        # regardless of which caller was ever responsible.
        return copy.deepcopy(item)
    if isinstance(item_id, str) and item_id.startswith("gi") and item_id[2:].isdigit():
        import db
        return db.materialize_item_instance(item_id)
    return None


def equipped_ability_bonus(character: dict, ability: str) -> int:
    """
    Magic item system Phase 8 (2026-09-08, task #6: "generate a RNG
    magic item with a +1 to a RNG stat"). Sum of every equipped item's
    real "ability_bonus" affix matching this ability, live-summed at
    read time -- same convention every other equipped-gear bonus in
    this game already uses (never baked into the character's own
    stored ability score). Lives here, not in bot.py, so rules/combat.py
    can call it directly for attack rolls without a circular import --
    both bot.py and rules/combat.py already reach into this module
    freely for get_item(). A monster/enemy dict (no "equipped_weapon"
    field at all) safely resolves to 0 via the plain .get() calls below.
    """
    total = 0
    equipped_ids = [
        character.get("equipped_weapon"), character.get("equipped_armor"), character.get("equipped_shield"),
    ] + character.get("equipped_accessories", [])
    for item_id in equipped_ids:
        if not item_id:
            continue
        item = get_item(item_id)
        if not item:
            continue
        for entry in item.get("ability_bonuses", []):
            if entry.get("ability") == ability:
                total += entry.get("value", 0)
    return total


def is_quest_item(item_id: str) -> bool:
    """
    Real gap found continuing the equipped-item transfer audit
    (2026-09-19, Coffee: "keep looking for gaps"): a quest_item can be
    genuinely load-bearing for story progression -- e.g.
    shard_of_dim_light, a one-time, non-repeatable quest reward
    (the_wrong_color) that a location's own real requires_item gate
    (the_unmoored_isle) checks for LIVE, at every attempt, not just
    once at pickup. Giving one away, market-listing it, or trading it
    off would permanently and unrecoverably lock a player out of that
    content. is_sellable() already excludes quest_item from the shop
    path, but that check ALSO excludes every ordinary price-0 loot
    item (Ring of Protection, etc.), which genuinely SHOULD stay
    giveable/tradeable/marketable by design (see _item_actions_
    keyboard's own comment on List on Market) -- so give/market/trade
    need this narrower, quest_item-only check instead of reusing
    is_sellable.
    """
    item = get_item(item_id)
    return item is not None and item.get("type") == "quest_item"


def is_sellable(item_id: str) -> bool:
    """
    Real live bug (2026-08-02, caught investigating a Development-topic
    request: "make sure players can sell the items to applicable shops
    also if they don't want them"): this used a direct ITEMS.get(...)
    lookup, not get_item()'s fallback -- every generated magic item
    (rules/item_generator.py, a "gi<n>" id) always came back None here,
    so shop.sell_item's is_sellable check silently rejected selling ANY
    generated item at all, unconditionally, with no way to know why. An
    earlier investigation (magic item system Phase 1) concluded this
    function was dead code never called anywhere in bot.py -- wrong;
    shop.sell_item calls it directly via _do_sell.
    """
    item = get_item(item_id)
    if item is None:
        return False
    return item.get("type") != "quest_item" and item.get("price", 0) > 0


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
    # Real live bug (2026-07-18, confirmed live: "Equip my longbow and
    # armour" silently equipped only the longbow): every real armor item
    # in this game ("Leather Armor", "Chain Shirt", "Chain Mail") is
    # named with the American spelling, and neither the full-name nor
    # the word-level fallback match below ever normalized the player's
    # own British spelling ("armour") to match -- confirmed by direct
    # repro. A live player retried with the American spelling 52 seconds
    # later, strongly suggesting they noticed the first attempt silently
    # failed. Normalizing just this one word (the only real-item-name
    # collision this game has) rather than a general British/American
    # dictionary, which would be a much bigger surface for false
    # positives with no other real payoff here.
    #
    # Same treatment for "fishing rod" (2026-07-19, confirmed live: "Sell
    # 1x fishing rod" failed even though the character owned a real
    # Fishing Pole) -- "rod" is the everyday real-world word for this
    # exact tool and this game has no other item using "rod" at all, so
    # normalizing it to "pole" is the same kind of safe, single-word,
    # real-collision fix as armour/armor, not a general synonym engine.
    lowered = text.strip().lower().replace("armour", "armor").replace("fishing rod", "fishing pole")
    search_space = candidate_ids if candidate_ids is not None else list(ITEMS.keys())
    # Check longer names first so "greater healing potion" doesn't get
    # shadowed by a shorter partial match like "healing potion".
    # get_item(), not a direct ITEMS[...] index (2026-08-02 magic item
    # system): a generated item's id (e.g. "gi42") is never a key in the
    # static ITEMS dict -- candidate_ids can genuinely include one the
    # moment a player carries any kept generated loot, and this function
    # is reached from ordinary inventory-scanning free text (equip/sell/
    # give), so it must resolve those too, not just the static catalog.
    ordered = sorted(search_space, key=lambda i: -len(get_item(i)["name"]))
    for item_id in ordered:
        data = get_item(item_id)
        name_lower = data["name"].lower()
        # Real live bug (2026-08-27, found via topic-activity
        # monitoring right after shipping trade's ambiguity-naming
        # fix): "Add 10 scrolls of the spirit to the trade" -- the
        # exact real name of a real, unambiguously-owned item (Scroll
        # of the Spirit) -- failed to resolve at all, even though the
        # SINGULAR "Scroll of the Spirit" worked fine. English pluralizes
        # a multi-word item name at its FIRST word ("Scroll of the
        # Spirit" -> "Scrolls of the Spirit"), not its last, so a bare
        # substring check against the singular name never matches, and
        # the phrase fell through to the weaker head-word fallback below
        # -- ambiguous here since Scroll of the LESSER Spirit shares the
        # same head noun "spirit", so a genuinely unambiguous plural
        # phrase wrongly landed on "which one?" instead of resolving
        # cleanly like its singular form already did.
        first_word, sep, rest = name_lower.partition(" ")
        plural_name = f"{first_word}s{sep}{rest}" if sep else f"{name_lower}s"
        if name_lower in lowered or plural_name in lowered or item_id.replace("_", " ") in lowered:
            return item_id

    # Fall back to a generic category word (e.g. "potions" for "Healing
    # Potion") when it unambiguously picks out exactly one candidate --
    # confirmed live 2026-07-12 via a real screenshot: "buy two potions
    # from Grimsby" never says the full item name, just the general
    # kind, and this shop only sells one real potion.
    #
    # Real live bug (2026-07-16, Coffee): "Look for a shop to buy an
    # axe" failed ("not sure what item you mean") even though the shop
    # sold exactly one axe (Woodcutter's Axe) -- the word-length >= 4
    # guard here, meant to keep trivial connector words ("of", "the")
    # from causing false matches, ALSO excluded genuinely short,
    # unambiguous item nouns like "axe" (3 letters). Fixed by explicitly
    # excluding a real stopword list instead of using a blanket length
    # cutoff, and by matching whole WORDS of the input (word membership)
    # rather than a bare substring check -- the old substring check
    # could also have matched a short word inside an unrelated longer
    # word (e.g. "ore" inside "before"), a latent risk this removes too.
    stopwords = {"the", "of", "an", "a", "on", "in", "to", "for", "and", "no"}
    # A plain .split() would leave punctuation stuck to words ("axe!!!"
    # never equals "axe") -- \w+(?:'\w+)? keeps a real internal
    # apostrophe (e.g. "woodcutter's") but strips trailing punctuation.
    lowered_words = set(re.findall(r"\w+(?:'\w+)?", lowered))

    # Real live bug (2026-07-19, confirmed live TWICE within the hour):
    # "Buy fishing hooks" silently bought a Fishing Pole -- "hooks" isn't
    # a real item anywhere, but "fishing" alone is a name-word of
    # "Fishing Pole" and word-overlap treated that lone MODIFIER as a
    # confident match. A required_for-based ambiguity check (flagging
    # Bait too, since both tools share required_for="fishing") fixed
    # that, but applying it unconditionally then broke "Sell 1x fishing
    # rod" -- Fishing Pole should resolve confidently there since "rod"
    # is just the everyday synonym for the SAME item's actual head noun
    # ("pole"), already normalized to match above.
    #
    # The real distinction: a match on an item's HEAD noun (the last
    # significant word -- "pole" in "Fishing Pole", "potion" in "Healing
    # Potion", "axe" in "Woodcutter's Axe") is a strong, confident match
    # that should win outright. A match on any OTHER word (a modifier,
    # like the bare "fishing" in "fishing hooks") is weak and should
    # still be checked against required_for for real ambiguity, exactly
    # like the fallback below already does for a bare category word.
    strong_matches = set()
    weak_matches = set()
    for item_id in search_space:
        name_words = [w for w in get_item(item_id)["name"].lower().split()]
        significant_words = [
            w[:-1] if w.endswith("s") else w for w in name_words
            if (w[:-1] if w.endswith("s") else w) not in stopwords
            and len(w[:-1] if w.endswith("s") else w) >= 3
        ]
        if not significant_words:
            continue
        # Check the head word FIRST, independent of its position in the
        # name -- iterating in name order and breaking on the first
        # match (as an earlier version of this fix did) could match a
        # modifier before ever checking the head word even when the
        # head word ALSO appears in the input, misclassifying a strong
        # match as weak.
        head_word = significant_words[-1]
        if head_word in lowered_words or f"{head_word}s" in lowered_words:
            strong_matches.add(item_id)
            continue
        for word in significant_words[:-1]:
            if word in lowered_words or f"{word}s" in lowered_words:
                weak_matches.add(item_id)
                break

    if strong_matches:
        if len(strong_matches) == 1:
            return strong_matches.pop()
        return None

    matches = set(weak_matches)
    for item_id in search_space:
        required_for = get_item(item_id).get("required_for")
        if required_for and (required_for in lowered_words or f"{required_for}s" in lowered_words):
            matches.add(item_id)
    if len(matches) == 1:
        return matches.pop()
    return None


def ambiguous_item_candidates(text: str, candidate_ids: list[str]) -> list[str]:
    """
    Real live bug (2026-08-27, Coffee dev-bridge: "I am trying to add
    items to the trade and it's not letting me" -- "Add 5 scrolls of
    spirits to the trade"). find_item_mentioned_in_text correctly
    returned None here -- a real, deliberate ambiguity guard, not a
    bug: the phrase's own head word ("spirit(s)") matches MULTIPLE
    real owned items at once (Scroll of the Lesser Spirit, Scroll of
    the Spirit, Scroll of the Greater Spirit, and Scroll of the Elder
    Spirit all share "Spirit" as their head noun). The caller had no
    way to tell "genuinely ambiguous, needs a real choice" apart from
    "nothing matched at all," so both produced the exact same
    unhelpful generic message. This duplicates ONLY the head-word
    strong-match detection (not the full function above, which has a
    long, carefully-tuned exact-match/weak-match/required_for fallback
    history other real callers depend on staying unchanged) so a
    caller can build a real "which one?" follow-up naming the actual
    candidates, same convention this file's own board-quest/market-
    listing disambiguation messages already use.
    """
    lowered = text.strip().lower().replace("armour", "armor").replace("fishing rod", "fishing pole")
    lowered_words = set(re.findall(r"\w+(?:'\w+)?", lowered))
    stopwords = {"the", "of", "an", "a", "on", "in", "to", "for", "and", "no"}
    matches = []
    for item_id in candidate_ids:
        name_words = get_item(item_id)["name"].lower().split()
        significant_words = [
            w[:-1] if w.endswith("s") else w for w in name_words
            if (w[:-1] if w.endswith("s") else w) not in stopwords
            and len(w[:-1] if w.endswith("s") else w) >= 3
        ]
        if not significant_words:
            continue
        head_word = significant_words[-1]
        if head_word in lowered_words or f"{head_word}s" in lowered_words:
            matches.append(item_id)
    return matches
