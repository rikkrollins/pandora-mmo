"""
spells.py
Spell catalog and resolution. Like everything else in the rules layer,
spell damage/healing numbers are rolled through rules/dice.py — the AI
narrates what a spell looked like, but never decides its numeric effect.
"""
from rules.dice import roll_damage, roll_d20, ability_modifier

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
        "effect": "damage", "damage_dice": "1d4+1", "always_hits": True,
    },
    "shield": {
        "name": "Shield", "level": 1, "school": "abjuration",
        "effect": "ac_bonus", "amount": 5, "duration_rounds": 1,
    },
    "cure_wounds": {
        "name": "Cure Wounds", "level": 1, "school": "evocation",
        "effect": "heal", "heal_dice": "1d8+2",
    },
    "burning_hands": {
        "name": "Burning Hands", "level": 1, "school": "evocation",
        "effect": "damage", "damage_dice": "3d6", "save_ability": "dexterity",
    },
    "invisibility": {
        "name": "Invisibility", "level": 2, "school": "illusion",
        "effect": "buff", "duration_rounds": 10,
    },
    "fireball": {
        "name": "Fireball", "level": 3, "school": "evocation",
        "effect": "damage", "damage_dice": "8d6", "save_ability": "dexterity",
    },
    "lightning_bolt": {
        "name": "Lightning Bolt", "level": 3, "school": "evocation",
        "effect": "damage", "damage_dice": "8d6", "save_ability": "dexterity",
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
        "effect": "damage", "damage_dice": "1d10",
    },
    "sacred_flame": {
        "name": "Sacred Flame", "level": 0, "school": "evocation",
        "effect": "damage", "damage_dice": "1d8",
    },
    "vicious_mockery": {
        "name": "Vicious Mockery", "level": 0, "school": "enchantment",
        "effect": "damage", "damage_dice": "1d4",
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
    "summon_lesser_spirit": {
        "name": "Summon Lesser Spirit", "level": 2, "school": "conjuration",
        "effect": "summon",
        "summon_stats": {
            "name": "a lesser spirit",
            "dexterity": 14, "strength": 10, "armor_class": 12,
            "hp_max": 9, "proficiency_bonus": 2,
        },
    },
    # --- Cantrips added for full per-class coverage (2026-07-11) ---
    "eldritch_blast": {
        "name": "Eldritch Blast", "level": 0, "school": "evocation",
        "effect": "damage", "damage_dice": "1d10",
    },
    "ray_of_frost": {
        "name": "Ray of Frost", "level": 0, "school": "evocation",
        "effect": "damage", "damage_dice": "1d8",
    },
    "produce_flame": {
        "name": "Produce Flame", "level": 0, "school": "conjuration",
        "effect": "damage", "damage_dice": "1d8",
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
        "effect": "damage", "damage_dice": "4d6",
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
        "effect": "damage", "damage_dice": "6d6",
    },
    "spiritual_weapon": {
        "name": "Spiritual Weapon", "level": 2, "school": "evocation",
        "effect": "damage", "damage_dice": "1d8+3",
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
        "effect": "damage", "damage_dice": "2d10", "save_ability": "constitution",
    },
    "dispel_magic": {
        "name": "Dispel Magic", "level": 3, "school": "abjuration",
        "effect": "negate", "duration_rounds": 0,
    },
    "call_lightning": {
        "name": "Call Lightning", "level": 3, "school": "conjuration",
        "effect": "damage", "damage_dice": "3d10", "save_ability": "dexterity",
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
    "cleric": ["sacred_flame", "guidance", "thaumaturgy"],
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
               "scorching_ray", "misty_step", "hold_person",
               "fireball", "lightning_bolt", "counterspell", "dispel_magic"],
    "sorcerer": ["magic_missile", "shield", "burning_hands", "charm_person",
                 "scorching_ray", "misty_step", "hold_person",
                 "fireball", "lightning_bolt", "counterspell"],
    "cleric": ["cure_wounds", "healing_word", "guiding_bolt", "command",
               "spiritual_weapon", "hold_person", "dispel_magic"],
    "druid": ["cure_wounds", "animal_friendship", "faerie_fire",
              "moonbeam", "hold_person", "call_lightning"],
    "bard": ["healing_word", "charm_person", "faerie_fire",
             "invisibility", "hold_person", "dispel_magic"],
    "warlock": ["charm_person", "hex", "protection_from_evil_and_good",
                "misty_step", "hold_person", "counterspell", "dispel_magic"],
    "paladin": ["cure_wounds", "command", "protection_from_evil_and_good"],
    "ranger": ["cure_wounds", "animal_friendship", "hunters_mark", "longstrider"],
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
    total = dmg["total"]

    result = {
        "spell": spell["name"], "caster": caster.get("name", "Unknown"),
        "damage_dealt": total, "rolls": dmg["rolls"],
    }

    save_ability = spell.get("save_ability")
    if save_ability and target is not None:
        dc = spell_save_dc(caster)
        save_roll = roll_d20()
        save_total = save_roll + ability_modifier(target.get(save_ability, 10))
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
    hp_before = target["hp_current"]
    hp_max = target.get("hp_max", hp_before)
    target["hp_current"] = min(hp_before + healing["total"], hp_max)
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
