"""
spells.py
Spell catalog and resolution. Like everything else in the rules layer,
spell damage/healing numbers are rolled through rules/dice.py — the AI
narrates what a spell looked like, but never decides its numeric effect.
"""
from rules.dice import roll_damage, roll_d20, ability_modifier
from rules.leveling import (
    is_proficient_in_save, LIFE_SUBCLASS_HEAL_BONUS, power_scale_ratio,
    COMBAT_SUBCLASS_NAMES, COMBAT_SUBCLASS_DAMAGE_BONUS_PCT, world_damage_multiplier,
)
from guilds import FAITH_CIRCLE_HEAL_BONUS, held_guild_ids
import hybrid_features

# Which ability a class casts spells with — needed to calculate a real
# 5E spell save DC (8 + proficiency bonus + spellcasting ability modifier).
SPELLCASTING_ABILITY = {
    "wizard": "intelligence",
    "sorcerer": "charisma", "warlock": "charisma", "bard": "charisma", "paladin": "charisma",
    "cleric": "wisdom", "druid": "wisdom", "ranger": "wisdom",
}


def spell_save_dc(caster: dict) -> int:
    """Real 5E spell save DC: 8 + proficiency bonus + spellcasting ability modifier."""
    ability = SPELLCASTING_ABILITY.get(caster.get("char_class", "").lower(), "intelligence")
    mod = ability_modifier(caster.get(ability, 10))
    return 8 + caster.get("proficiency_bonus", 2) + mod

SPELLS = {
    "magic_missile": {
        "name": "Magic Missile", "level": 1, "school": "evocation",
        "effect": "damage", "damage_dice": "1d4+1", "always_hits": True, "damage_type": "force",
    },
    "shield": {
        "name": "Shield", "level": 1, "school": "abjuration",
        "effect": "ac_bonus", "amount": 5, "duration_rounds": 1,
    },
    "cure_wounds": {
        "name": "Cure Wounds", "level": 1, "school": "evocation",
        "effect": "heal", "heal_dice": "1d8+2",
    },
    "revivify": {
        "name": "Revivify", "level": 3, "school": "necromancy",
        "effect": "resurrect",
    },
    "burning_hands": {
        "name": "Burning Hands", "level": 1, "school": "evocation",
        "effect": "damage", "damage_dice": "3d6", "save_ability": "dexterity", "damage_type": "fire",
    },
    "invisibility": {
        "name": "Invisibility", "level": 2, "school": "illusion",
        "effect": "buff", "duration_rounds": 10,
    },
    "fireball": {
        "name": "Fireball", "level": 3, "school": "evocation",
        "effect": "damage", "damage_dice": "8d6", "save_ability": "dexterity", "damage_type": "fire",
    },
    "lightning_bolt": {
        "name": "Lightning Bolt", "level": 3, "school": "evocation",
        "effect": "damage", "damage_dice": "8d6", "save_ability": "dexterity", "damage_type": "lightning",
    },
    "counterspell": {
        "name": "Counterspell", "level": 3, "school": "abjuration",
        "effect": "negate", "duration_rounds": 0,
    },
    "healing_word": {
        "name": "Healing Word", "level": 1, "school": "evocation",
        "effect": "heal", "heal_dice": "1d4+2",
    },
    "fire_bolt": {
        "name": "Fire Bolt", "level": 0, "school": "evocation",
        "effect": "damage", "damage_dice": "1d10", "damage_type": "fire",
    },
    "sacred_flame": {
        "name": "Sacred Flame", "level": 0, "school": "evocation",
        "effect": "damage", "damage_dice": "1d8", "damage_type": "radiant",
    },
    "vicious_mockery": {
        "name": "Vicious Mockery", "level": 0, "school": "enchantment",
        "effect": "damage", "damage_dice": "1d4", "damage_type": "psychic",
    },
    "guidance": {
        "name": "Guidance", "level": 0, "school": "divination",
        "effect": "buff", "duration_rounds": 1,
    },
    "detect_magic": {
        "name": "Detect Magic", "level": 1, "school": "divination",
        "effect": "buff", "duration_rounds": 10,
    },
    "bless": {
        "name": "Bless", "level": 1, "school": "enchantment",
        "effect": "buff", "duration_rounds": 10,
    },
    "faerie_fire": {
        "name": "Faerie Fire", "level": 1, "school": "evocation",
        "effect": "buff", "duration_rounds": 10,
    },
    "thaumaturgy": {
        "name": "Thaumaturgy", "level": 0, "school": "transmutation",
        "effect": "buff", "duration_rounds": 1,
    },
    # Tiered spirit-summon family (2026-08-21, per Coffee: "create caps
    # so each scroll can only summon up to a certain level... players
    # dont have access to the higher/better scroll unless we find,
    # steal, or buy them at higher level areas"). Each tier's own
    # max_summon_level is a real, enforced SOFT cap (bot.py's "summon"
    # effect handler): the summon is built at
    # min(caster's real level, max_summon_level), never higher, even
    # for a caster who's long since outgrown this scroll -- so a
    # Lesser Spirit scroll stays usable (just weaker) instead of
    # becoming dead weight, matching Coffee's own "same level as the
    # player OR the max level the scroll allows, whichever is lower."
    # None of these four are added to any class's known-spell list --
    # scroll-only by design, so real access genuinely is gated behind
    # finding/stealing/buying the scroll itself (see items.py).
    "summon_lesser_spirit": {
        "name": "Summon Lesser Spirit", "level": 2, "school": "conjuration",
        "effect": "summon", "max_summon_level": 25,
        "summon_stats": {
            "name": "a lesser spirit",
            "dexterity": 14, "strength": 10, "armor_class": 12,
            "hp_max": 9, "proficiency_bonus": 2,
        },
    },
    "summon_spirit": {
        "name": "Summon Spirit", "level": 4, "school": "conjuration",
        "effect": "summon", "max_summon_level": 50,
        "summon_stats": {
            "name": "a spirit",
            "dexterity": 15, "strength": 13, "armor_class": 14,
            "hp_max": 9, "proficiency_bonus": 2,
        },
    },
    "summon_greater_spirit": {
        "name": "Summon Greater Spirit", "level": 6, "school": "conjuration",
        "effect": "summon", "max_summon_level": 75,
        "summon_stats": {
            "name": "a greater spirit",
            "dexterity": 16, "strength": 15, "armor_class": 16,
            "hp_max": 9, "proficiency_bonus": 2,
        },
    },
    "summon_elder_spirit": {
        "name": "Summon Elder Spirit", "level": 8, "school": "conjuration",
        "effect": "summon", "max_summon_level": 99,
        "summon_stats": {
            "name": "an elder spirit",
            "dexterity": 18, "strength": 18, "armor_class": 18,
            "hp_max": 9, "proficiency_bonus": 2,
        },
    },
    # Real spirit-summon abilities (2026-08-21, per Coffee: "make sure
    # the spirits also have abilits they can use 2 for each tier, a
    # magic type, and a damage type"). AI-only -- never offered to a
    # real player (no class known_spells entry, no scroll) -- picked up
    # automatically by the existing, already-tested side-agnostic
    # monster-spellcasting mechanic (bot.py's _decide_monster_spell/
    # _maybe_monster_cast_spell, MONSTER_SPELLCAST_CHANCE per turn) the
    # instant a summon's own known_spells lists them; no new AI-decision
    # code needed. Each tier gets its own distinct elemental identity.
    # Deliberately small base damage_dice (cantrip-shaped, same scale as
    # eldritch_blast/fire_bolt below) -- resolve_damage_spell already
    # applies power_scale_ratio(effective_level, rebirth_count) for any
    # caster with a real "level" field (a summon always has one, its
    # own capped effective_level), the same real scaling mechanism a
    # player's own spell damage gets, so these grow to match the
    # summon's own tier automatically rather than needing hand-tuned
    # huge numbers per tier here.
    "spirit_lash": {
        "name": "Spirit Lash", "level": 0, "school": "conjuration",
        "effect": "damage", "damage_dice": "2d6", "damage_type": "force",
    },
    "spirit_bolt": {
        "name": "Spirit Bolt", "level": 0, "school": "conjuration",
        "effect": "damage", "damage_dice": "2d6", "damage_type": "force",
    },
    "spirit_flare": {
        "name": "Spirit Flare", "level": 0, "school": "evocation",
        "effect": "damage", "damage_dice": "3d6", "damage_type": "radiant",
    },
    "spirit_ray": {
        "name": "Spirit Ray", "level": 0, "school": "evocation",
        "effect": "damage", "damage_dice": "3d6", "damage_type": "radiant",
    },
    "spirit_rend": {
        "name": "Spirit Rend", "level": 0, "school": "necromancy",
        "effect": "damage", "damage_dice": "4d6", "damage_type": "necrotic",
    },
    "spirit_wail": {
        "name": "Spirit Wail", "level": 0, "school": "necromancy",
        "effect": "damage", "damage_dice": "4d6", "damage_type": "necrotic",
    },
    "spirit_dread": {
        "name": "Spirit Dread", "level": 0, "school": "evocation",
        "effect": "damage", "damage_dice": "6d6", "damage_type": "psychic",
    },
    "spirit_collapse": {
        "name": "Spirit Collapse", "level": 0, "school": "evocation",
        "effect": "damage", "damage_dice": "6d6", "damage_type": "psychic",
    },
    # --- Cantrips added for full per-class coverage (2026-07-11) ---
    "eldritch_blast": {
        "name": "Eldritch Blast", "level": 0, "school": "evocation",
        "effect": "damage", "damage_dice": "1d10", "damage_type": "force",
    },
    "ray_of_frost": {
        "name": "Ray of Frost", "level": 0, "school": "evocation",
        "effect": "damage", "damage_dice": "1d8", "damage_type": "cold",
    },
    "produce_flame": {
        "name": "Produce Flame", "level": 0, "school": "conjuration",
        "effect": "damage", "damage_dice": "1d8", "damage_type": "fire",
    },
    "mage_hand": {
        "name": "Mage Hand", "level": 0, "school": "conjuration",
        "effect": "buff", "duration_rounds": 1,
    },
    "prestidigitation": {
        "name": "Prestidigitation", "level": 0, "school": "transmutation",
        "effect": "buff", "duration_rounds": 1,
    },
    "dancing_lights": {
        "name": "Dancing Lights", "level": 0, "school": "evocation",
        "effect": "buff", "duration_rounds": 10,
    },
    "spare_the_dying": {
        "name": "Spare the Dying", "level": 0, "school": "necromancy",
        "effect": "buff", "duration_rounds": 1,
    },
    # --- Leveled spells added for real level-up progression (2026-07-11) ---
    "charm_person": {
        "name": "Charm Person", "level": 1, "school": "enchantment",
        "effect": "buff", "duration_rounds": 10,
    },
    "guiding_bolt": {
        "name": "Guiding Bolt", "level": 1, "school": "evocation",
        "effect": "damage", "damage_dice": "4d6", "damage_type": "radiant",
    },
    "command": {
        "name": "Command", "level": 1, "school": "enchantment",
        "effect": "buff", "duration_rounds": 1,
    },
    "protection_from_evil_and_good": {
        "name": "Protection from Evil and Good", "level": 1, "school": "abjuration",
        "effect": "buff", "duration_rounds": 10,
    },
    "animal_friendship": {
        "name": "Animal Friendship", "level": 1, "school": "enchantment",
        "effect": "buff", "duration_rounds": 10,
    },
    "hunters_mark": {
        "name": "Hunter's Mark", "level": 1, "school": "divination",
        "effect": "buff", "duration_rounds": 10,
    },
    "hex": {
        "name": "Hex", "level": 1, "school": "enchantment",
        "effect": "buff", "duration_rounds": 10,
    },
    "longstrider": {
        "name": "Longstrider", "level": 1, "school": "transmutation",
        "effect": "buff", "duration_rounds": 10,
    },
    "scorching_ray": {
        "name": "Scorching Ray", "level": 2, "school": "evocation",
        "effect": "damage", "damage_dice": "6d6", "damage_type": "fire",
    },
    "spiritual_weapon": {
        "name": "Spiritual Weapon", "level": 2, "school": "evocation",
        "effect": "damage", "damage_dice": "1d8+3", "damage_type": "force",
    },
    "misty_step": {
        "name": "Misty Step", "level": 2, "school": "conjuration",
        "effect": "buff", "duration_rounds": 1,
    },
    "hold_person": {
        "name": "Hold Person", "level": 2, "school": "enchantment",
        "effect": "buff", "duration_rounds": 10,
    },
    "moonbeam": {
        "name": "Moonbeam", "level": 2, "school": "evocation",
        "effect": "damage", "damage_dice": "2d10", "save_ability": "constitution", "damage_type": "radiant",
    },
    "dispel_magic": {
        "name": "Dispel Magic", "level": 3, "school": "abjuration",
        "effect": "negate", "duration_rounds": 0,
    },
    "call_lightning": {
        "name": "Call Lightning", "level": 3, "school": "conjuration",
        "effect": "damage", "damage_dice": "3d10", "save_ability": "dexterity", "damage_type": "lightning",
    },
    # --- 4th/5th-level spells (2026-07-15): SPELL_LEVEL_UNLOCK_CHAR_LEVEL
    # already promised these tiers at character levels 7/9, but no spell
    # of either level actually existed -- casters got nothing new from
    # level 7 onward. Filled in below, reusing the same shared-across-
    # classes convention already used for e.g. hold_person/dispel_magic.
    "ice_storm": {
        "name": "Ice Storm", "level": 4, "school": "evocation",
        "effect": "damage", "damage_dice": "6d8", "save_ability": "dexterity", "damage_type": "cold",
    },
    "polymorph": {
        "name": "Polymorph", "level": 4, "school": "transmutation",
        "effect": "buff", "duration_rounds": 10,
    },
    "death_ward": {
        "name": "Death Ward", "level": 4, "school": "abjuration",
        "effect": "buff", "duration_rounds": 10,
    },
    "banishment": {
        "name": "Banishment", "level": 4, "school": "abjuration",
        "effect": "buff", "duration_rounds": 10,
    },
    "dimension_door": {
        "name": "Dimension Door", "level": 4, "school": "conjuration",
        "effect": "buff", "duration_rounds": 1,
    },
    "guardian_of_faith": {
        "name": "Guardian of Faith", "level": 4, "school": "conjuration",
        "effect": "damage", "damage_dice": "2d8", "damage_type": "radiant",
    },
    "cone_of_cold": {
        "name": "Cone of Cold", "level": 5, "school": "evocation",
        "effect": "damage", "damage_dice": "8d8", "save_ability": "dexterity", "damage_type": "cold",
    },
    "mass_cure_wounds": {
        "name": "Mass Cure Wounds", "level": 5, "school": "evocation",
        "effect": "heal", "heal_dice": "3d8+5",
    },
    "flame_strike": {
        "name": "Flame Strike", "level": 5, "school": "evocation",
        "effect": "damage", "damage_dice": "8d6", "save_ability": "dexterity", "damage_type": "fire",
    },
    "insect_plague": {
        "name": "Insect Plague", "level": 5, "school": "conjuration",
        "effect": "damage", "damage_dice": "4d10", "save_ability": "constitution", "damage_type": "poison",
    },
    "hold_monster": {
        "name": "Hold Monster", "level": 5, "school": "enchantment",
        "effect": "buff", "duration_rounds": 10,
    },
    # Arcane Circle exclusive spells (2026-07-25, per Coffee: "let them
    # learn new spells not otherwise available unless in the guilds").
    # Deliberately NOT in any CLASS_SPELL_LISTS entry below, so they
    # never auto-unlock via spells_unlocked_at_level like every other
    # spell in this file does -- the only way to ever know one is
    # bot.py's _do_learn_guild_spell, gated on real Arcane Circle
    # membership (guilds.ARCANE_CIRCLE_EXCLUSIVE_SPELLS). Genuinely the
    # strongest damage spells in the game (stronger than the previous
    # top tier, Cone of Cold's 8d8) -- a real, mechanical reason to join
    # beyond the flat +15% damage bonus.
    "starfall_lance": {
        "name": "Starfall Lance", "level": 4, "school": "evocation",
        "effect": "damage", "damage_dice": "8d8", "save_ability": "dexterity", "damage_type": "radiant",
    },
    "voidcall": {
        "name": "Voidcall", "level": 5, "school": "necromancy",
        "effect": "damage", "damage_dice": "10d8", "save_ability": "constitution", "damage_type": "necrotic",
    },
}

# Cantrips (level 0) each class has at-will, alongside their leveled spells.
# Real 5E SRD per-class cantrip counts at level 1 (2026-07-11 pass: filled
# these out to match real cantrip counts -- previously most classes only
# had a single placeholder cantrip). Paladin and Ranger correctly have
# none: neither class ever gets cantrips in 5E, only leveled spells
# starting at character level 2.
CLASS_CANTRIPS = {
    "wizard": ["fire_bolt", "ray_of_frost", "prestidigitation"],
    "sorcerer": ["fire_bolt", "ray_of_frost", "mage_hand", "prestidigitation"],
    "warlock": ["eldritch_blast", "mage_hand"],
    "cleric": ["sacred_flame", "guidance", "thaumaturgy", "spare_the_dying"],
    "druid": ["guidance", "produce_flame"],
    "bard": ["vicious_mockery", "dancing_lights"],
}

# Which classes get access to which spells, and at what character level
# (see spells_unlocked_at_level). NOTE: bot.py's character-creation flow
# grants the FIRST TWO entries of a class's list outright at level 1 (for
# classes with starting slots), so the first two entries here MUST always
# be real, valid 1st-level spells for that class -- everything after that
# is unlocked later via spells_unlocked_at_level, which correctly filters
# by real spell level regardless of list order.
#
# 2026-07-11 accuracy pass fixed three real 5E inaccuracies found here:
# Cleric had "shield" (that's a Wizard/Sorcerer-only spell, never Cleric),
# Druid had "healing_word" (never on Druid's real spell list), and
# Warlock had "magic_missile"/"burning_hands" (neither is a real Warlock
# spell -- Warlocks have their own distinct spell list). Also fixed: Bard
# previously got a 2nd-level spell (Invisibility) granted at character
# creation with no level gate, since the old list's first two entries
# weren't both real 1st-level spells.
CLASS_SPELL_LISTS = {
    "wizard": ["magic_missile", "shield", "burning_hands", "charm_person",
               "detect_magic", "scorching_ray", "misty_step", "hold_person",
               "fireball", "lightning_bolt", "counterspell", "dispel_magic",
               "ice_storm", "polymorph", "cone_of_cold", "hold_monster"],
    "sorcerer": ["magic_missile", "shield", "burning_hands", "charm_person",
                 "scorching_ray", "misty_step", "hold_person",
                 "fireball", "lightning_bolt", "counterspell",
                 "ice_storm", "polymorph", "cone_of_cold", "hold_monster"],
    "cleric": ["cure_wounds", "healing_word", "guiding_bolt", "command", "bless",
               "spiritual_weapon", "hold_person", "dispel_magic", "revivify",
               "death_ward", "guardian_of_faith", "flame_strike", "mass_cure_wounds"],
    "druid": ["cure_wounds", "animal_friendship", "faerie_fire",
              "moonbeam", "hold_person", "call_lightning",
              "ice_storm", "polymorph", "insect_plague", "mass_cure_wounds"],
    "bard": ["healing_word", "charm_person", "faerie_fire",
             "invisibility", "hold_person", "dispel_magic",
             "polymorph", "dimension_door", "mass_cure_wounds", "hold_monster"],
    "warlock": ["charm_person", "hex", "protection_from_evil_and_good",
                "misty_step", "hold_person", "counterspell", "dispel_magic",
                "banishment", "dimension_door", "hold_monster"],
    "paladin": ["cure_wounds", "command", "protection_from_evil_and_good",
                "death_ward", "banishment"],
    "ranger": ["cure_wounds", "animal_friendship", "hunters_mark", "longstrider",
               "summon_lesser_spirit", "insect_plague"],
}


def spells_known_for_class(char_class: str) -> list[str]:
    """All spells (cantrips + leveled) a class can access, cantrips first
    since a starting character always knows their cantrips at will."""
    cls = char_class.lower()
    return CLASS_CANTRIPS.get(cls, []) + CLASS_SPELL_LISTS.get(cls, [])


# Minimum character level required before a spell of a given spell-list
# level can be learned (a simplified, monotonic stand-in for 5E's real
# spell-slot-by-level table — enough to gate CLASS_SPELL_LISTS entries
# behind real level-ups instead of granting the whole list at level 1).
SPELL_LEVEL_UNLOCK_CHAR_LEVEL = {0: 1, 1: 1, 2: 3, 3: 5, 4: 7, 5: 9}


def max_spell_level_for_character_level(character_level: int) -> int:
    """Highest spell-list level this character level has unlocked."""
    unlocked = 0
    for spell_level, required_char_level in SPELL_LEVEL_UNLOCK_CHAR_LEVEL.items():
        if character_level >= required_char_level and spell_level > unlocked:
            unlocked = spell_level
    return unlocked


def spells_unlocked_at_level(char_class: str, character_level: int) -> list[str]:
    """
    All spells (cantrips + leveled) a class can know at a given character
    level, per SPELL_LEVEL_UNLOCK_CHAR_LEVEL — used to grant new spells on
    level-up, not just at character creation.
    """
    cls = char_class.lower()
    max_level = max_spell_level_for_character_level(character_level)
    candidates = CLASS_CANTRIPS.get(cls, []) + CLASS_SPELL_LISTS.get(cls, [])
    return [s for s in candidates if SPELLS[s]["level"] <= max_level]


def get_spell(spell_id: str) -> dict | None:
    return SPELLS.get(spell_id)


def _save_bonus(target: dict, save_ability: str) -> int:
    """
    Saving throw proficiency (2026-07-16 audit): confirmed via grep this
    was never applied anywhere -- every save previously only ever added
    the raw ability modifier, even for a class real 5E says is
    proficient in that specific save (e.g. a Fighter resisting a
    Constitution-based spell should add their proficiency bonus too).
    """
    bonus = ability_modifier(target.get(save_ability, 10))
    if is_proficient_in_save(target.get("char_class"), save_ability):
        bonus += target.get("proficiency_bonus", 2)
    return bonus


def _gnome_cunning_advantage(target: dict, save_ability: str) -> bool:
    """
    Gnome Cunning (races.py racial trait, 2026-07-16 audit): advantage
    on Intelligence/Wisdom/Charisma saving throws against magic. The
    closest existing hook for "against magic" is a spell's own save,
    since this engine has no separate magic-vs-mundane save distinction.
    """
    return target.get("race") == "Gnome" and save_ability in ("intelligence", "wisdom", "charisma")


def ranger_danger_sense_advantage(target: dict, save_ability: str) -> bool:
    """
    Ranger's Danger Sense (real 5E, level 2+, previously pure flavor
    text in class_features.py): advantage on Dexterity saving throws
    against effects you can see coming. This engine has no "can you see
    it" distinction on a spell's own save -- same simplification as
    Gnome Cunning above -- so it applies to every Dexterity save a
    level 2+ Ranger makes.
    """
    return target.get("char_class") == "Ranger" and target.get("level", 1) >= 2 and save_ability == "dexterity"


def _warlock_agonizing_blast_bonus(spell_id: str, caster: dict) -> int:
    """
    Warlock's Agonizing Blast (real 5E Eldritch Invocation, task #91
    2026-07-18): every level 2+ Warlock defaults to knowing this
    invocation -- same fixed-default, no-in-game-choice convention as
    Sorcerous Origin/Otherworldly Patron/Divine Domain elsewhere in
    this file -- adding their Charisma modifier to Eldritch Blast's
    damage. Real 5E's invocation system offers many choices; this one
    is close to universal among real players since Eldritch Blast is a
    Warlock's core damage cantrip, making it the safe default pick.
    """
    if spell_id != "eldritch_blast":
        return 0
    if caster.get("char_class") != "Warlock" or caster.get("level", 1) < 2:
        return 0
    return max(ability_modifier(caster.get("charisma", 10)), 0)


def resolve_damage_spell(spell_id: str, caster: dict, target: dict | None = None) -> dict:
    """
    Roll a damage spell's effect. Returns a structured result, not prose.
    If the spell has a "save_ability" (e.g. Fireball forces a DEX save),
    the target's real saving throw is rolled here — half damage on a
    success, per real 5E rules — rather than the spell always dealing
    full, unavoidable damage.
    """
    spell = SPELLS[spell_id]
    if spell["effect"] != "damage":
        raise ValueError(f"{spell_id} is not a damage spell")
    dmg = roll_damage(spell["damage_dice"])
    total = dmg["total"] + _warlock_agonizing_blast_bonus(spell_id, caster)
    # Real player-power rebalance (2026-07-26, per Coffee: "rebalance
    # everything... all skills and abilities and magic and spells and
    # cantrips"): spell damage_dice never scales past its fixed value at
    # all -- confirmed the single most consequential gap in a full
    # creation-to-rebirth-10 audit, since casters got real power growth
    # only through the level a spell unlocks, then flatlined for the
    # rest of the game while monsters/HP/martial classes all now scale.
    # Same power_scale_ratio already applied to a real character's
    # weapon damage in rules/combat.py's resolve_attack.
    #
    # Real live bug (2026-08-14, Coffee, dev-bridge screenshot: "the
    # enemy doesn't seem to be doing much damage... for this to be a
    # boss don't you think it would be a bit more challenging?"): a
    # MONSTER caster (the_unspoken and every other shaman/boss with
    # known_spells, since v1.27.194's side-agnostic monster
    # spellcasting reuses this exact function) has no real "level"
    # field at all -- caster.get("level", 1) silently defaulted to 1,
    # so power_scale_ratio(1, 0) == 1.0 (a no-op), leaving a boss's
    # cast at its spell's raw, unscaled cantrip damage (e.g. Vicious
    # Mockery's flat 1d4 == 1-4 damage, confirmed live) while that same
    # boss's own melee attack correctly used its real, hand-tuned
    # damage_bonus (+17) via rules/combat.py's resolve_attack --
    # completely divorcing "the boss casts a spell" from "the boss
    # attacks normally" in outgoing power, the opposite of the "same
    # rules-layer pipeline" parity v1.27.194 was built to guarantee.
    # rules.combat.resolve_attack's own docstring already establishes
    # the real fix's shape for the melee side ("monsters already carry
    # their own pre-scaled damage_dice/damage_bonus directly in
    # campaign.json") -- mirrored here: a caster with no real "level"
    # (every genuine player/companion character always has one; no
    # monster participant dict ever sets it, confirmed by grep) is a
    # monster, so its own already-tuned damage_bonus is added directly
    # instead of applying power_scale_ratio (which was only ever meant
    # for a real character's flat, unscaled spell numbers).
    if "level" in caster:
        total = int(total * power_scale_ratio(caster.get("level", 1), caster.get("rebirth_count", 0)))
    else:
        total += caster.get("damage_bonus", 0)
        # "The World Evolves" (2026-08-14, per Coffee): same real
        # inverse of power_scale_ratio rules/combat.py's resolve_attack
        # now applies for monster weapon attacks -- a monster CASTER
        # hits harder in proportion to how many times the target it's
        # casting at has rebirthed. Only in the "else" (monster caster)
        # branch, same reasoning as damage_bonus just above: a real
        # character caster's own spell numbers are untouched by this.
        if target is not None:
            total = int(total * world_damage_multiplier(target.get("rebirth_count", 0)))

    # Real live gap (2026-08-14, Coffee: "Draconic shud be 20% more
    # damage on magic based attacks... shudnt the magic user have a
    # stat that influences there magic ability" -- correctly spotted
    # while asking about Sorcerer's Draconic/Wild Magic split): the
    # exact same "combat pick" subclass bonus rules/combat.py's
    # resolve_attack already grants weapon attacks (rules.leveling.
    # COMBAT_SUBCLASS_NAMES/COMBAT_SUBCLASS_DAMAGE_BONUS_PCT) never
    # applied here at all -- so Draconic (Sorcerer), Fiend (Warlock),
    # War (Cleric), Moon (Druid), and Valor (Bard) -- five real
    # "combat" subclass picks belonging to classes that deal most or
    # all of their real damage through SPELLS, not weapon swings --
    # granted a bonus their own class could rarely if ever actually
    # use. Applied here too, same flat multiplier, so the bonus
    # actually reaches the damage type these classes deal.
    if caster.get("subclass") in COMBAT_SUBCLASS_NAMES:
        total = int(total * (1 + COMBAT_SUBCLASS_DAMAGE_BONUS_PCT / 100))

    result = {
        "spell": spell["name"], "caster": caster.get("name", "Unknown"),
        "damage_dealt": total, "rolls": dmg["rolls"],
    }

    save_ability = spell.get("save_ability")
    if save_ability and target is not None:
        dc = spell_save_dc(caster)
        save_roll = roll_d20(
            advantage=_gnome_cunning_advantage(target, save_ability)
            or ranger_danger_sense_advantage(target, save_ability)
        )
        save_total = save_roll + _save_bonus(target, save_ability)
        save_success = save_total >= dc
        if save_success:
            total = total // 2
        result.update({
            "damage_dealt": total,
            "save_ability": save_ability,
            "save_dc": dc,
            "save_roll": save_roll,
            "save_total": save_total,
            "save_success": save_success,
        })

    return result


def resolve_heal_spell(spell_id: str, caster: dict, target: dict) -> dict:
    """Roll a healing spell's effect and apply it directly to target['hp_current']."""
    spell = SPELLS[spell_id]
    if spell["effect"] != "heal":
        raise ValueError(f"{spell_id} is not a healing spell")
    healing = roll_damage(spell["heal_dice"])
    total_healed = healing["total"]
    # Cleric's Divine Domain: every Cleric gets this Disciple of Life
    # bonus unconditionally (a fixed-default Life Domain flavor, same
    # convention as Sorcerer's Draconic Bloodline/Warlock's Fiend
    # patron) -- adds 2 + the spell's level whenever a leveled (not
    # cantrip) spell restores HP, real 5E formula.
    if caster.get("char_class", "").lower() == "cleric" and spell["level"] > 0:
        disciple_of_life_bonus = 2 + spell["level"]
        # Universal Manipulation "disciples_grace" (2026-08-06,
        # repeatable): was a flat one-time double; now each point
        # invested adds ANOTHER full copy of the base bonus (1 point =
        # still exactly doubled, matching every existing character's
        # first point unchanged; 2 points = tripled; unlimited beyond
        # that).
        grace_points = (caster.get("skill_tree_upgrades") or []).count("disciples_grace")
        disciple_of_life_bonus *= 1 + grace_points
        total_healed += disciple_of_life_bonus
    # Hybrid Cleric (2026-07-22): a real, chance-gated, scaled-down
    # taste of Disciple of Life -- can't ever double up with the real
    # Cleric bonus above (a hybrid pick can never equal your own real
    # char_class).
    total_healed += hybrid_features.hybrid_heal_bonus(caster, spell["level"])
    # Faith Circle membership benefit (2026-07-25, per Coffee): a real
    # flat bonus on every heal cast, same additive convention as
    # Disciple of Life above -- stacks with it rather than replacing it.
    # Real gap found in a 2026-08-14 full synergy audit: this checked only
    # the PRIMARY guild, missed by the 2026-08-13 sweep that fixed every
    # other guild-benefit site (Silver Wardens/Forge Guild/Thieves' Guild/
    # Arcane Circle/Adventurers' Guild/Enchanters' Guild all already use
    # held_guild_ids) -- a character holding Faith Circle as a SECONDARY
    # (Promotion) guild got zero bonus despite genuinely holding it.
    if "faith_circle" in held_guild_ids(caster):
        total_healed += FAITH_CIRCLE_HEAL_BONUS
    # Cleric's Life subclass hook (2026-07-25): actually CHOOSING Life
    # (rather than War) over the unconditional Disciple of Life above
    # now means something real -- a further, distinct flat bonus on
    # top, same stacking convention as Faith Circle.
    if caster.get("subclass") == "Life":
        total_healed += LIFE_SUBCLASS_HEAL_BONUS
    # Real player-power rebalance (2026-07-26): healing needs to keep
    # pace with the same rescaled HP pools everything else now scales
    # to -- without this, healing spells would become progressively
    # negligible at high level/rebirth even as monster damage and max
    # HP both keep growing. Same power_scale_ratio as weapon/damage-
    # spell scaling above.
    total_healed = int(total_healed * power_scale_ratio(caster.get("level", 1), caster.get("rebirth_count", 0)))
    hp_before = target["hp_current"]
    hp_max = target.get("hp_max", hp_before)
    target["hp_current"] = min(hp_before + total_healed, hp_max)
    return {
        "spell": spell["name"], "caster": caster.get("name", "Unknown"),
        "target": target.get("name", "Unknown"), "healing_done": target["hp_current"] - hp_before,
        "hp_current": target["hp_current"], "hp_max": hp_max,
    }


# Real 5E starting spell slots at character level 1. Full casters
# (Wizard, Sorcerer, Cleric, Druid, Bard) get 2 first-level slots.
# Warlock (Pact Magic) gets 1, refreshing on a SHORT rest in real 5E —
# simplified here to refresh on any rest alongside everyone else.
# Paladin and Ranger are correctly given ZERO spell slots and ZERO
# leveled spells at level 1 — in real 5E, half-casters don't gain
# spellcasting until level 2. (This build previously incorrectly
# granted them a spell at creation; fixed here.)
STARTING_SPELL_SLOTS = {
    "wizard": 2, "sorcerer": 2, "cleric": 2, "druid": 2, "bard": 2,
    "warlock": 1,
    "paladin": 0, "ranger": 0,
}


def starting_spell_slots_for_class(char_class: str) -> int:
    return STARTING_SPELL_SLOTS.get(char_class.lower(), 0)
