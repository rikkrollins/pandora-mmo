"""
class_features.py
Real Dungeons & Dragons 5E (SRD) class features available at character
level 1. This build doesn't yet model resource tracking for all of
these (e.g. Rage uses/day, Ki points), so features are granted and
shown on the character sheet as real, accurate information — using
them mechanically in combat beyond what's already built (attacks,
spells) is a further step, not yet wired in everywhere.
"""

CLASS_FEATURES_LEVEL_1 = {
    "Barbarian": [
        "Rage: bonus action to enter a rage — advantage on STR checks/saves, "
        "bonus melee damage, resistance to bludgeoning/piercing/slashing damage",
        "Unarmored Defense: AC = 10 + DEX modifier + CON modifier when not wearing armor",
    ],
    "Bard": [
        "Bardic Inspiration: bonus action, give an ally a d6 to add to one "
        "attack roll, ability check, or saving throw",
        "Spellcasting: casts bard spells using Charisma",
    ],
    "Cleric": [
        "Spellcasting: casts cleric spells using Wisdom",
        "Divine Domain: grants an additional domain spell and domain feature",
    ],
    "Druid": [
        "Spellcasting: casts druid spells using Wisdom",
        "Druidic: knows the secret Druidic language",
    ],
    "Fighter": [
        "Fighting Style: a combat specialization (e.g. Defense, Dueling, Great Weapon Fighting)",
        "Second Wind: bonus action, regain 1d10 + fighter level HP once per short/long rest",
    ],
    "Monk": [
        "Unarmored Defense: AC = 10 + DEX modifier + WIS modifier when not wearing armor",
        "Martial Arts: can use DEX instead of STR for unarmed strikes/monk weapons, "
        "unarmed strike damage die of 1d4",
    ],
    "Paladin": [
        "Divine Sense: action, detect celestials/fiends/undead within 60 ft.",
        "Lay on Hands: heal a pool of HP (5 x paladin level) by touch",
    ],
    "Ranger": [
        "Favored Enemy: advantage on tracking and recalling information about a chosen enemy type",
        "Natural Explorer: expertise navigating and surviving in a chosen terrain type",
    ],
    "Rogue": [
        "Sneak Attack: extra 1d6 damage once per turn when you have advantage "
        "or an ally is adjacent to your target",
        "Thieves' Cant: knows a secret rogue language/code",
        "Expertise: double proficiency bonus on two chosen skills",
    ],
    "Sorcerer": [
        "Spellcasting: casts sorcerer spells using Charisma",
        "Sorcerous Origin: grants a source of innate magical power (e.g. Draconic Bloodline)",
    ],
    "Warlock": [
        "Otherworldly Patron: a pact with a powerful entity grants abilities",
        "Pact Magic: casts warlock spells using Charisma, spell slots recover on a short rest",
    ],
    "Wizard": [
        "Spellcasting: casts wizard spells using Intelligence, prepared from a spellbook",
        "Arcane Recovery: once per day, recover expended spell slots on a short rest",
    ],
}


def get_class_features(char_class: str) -> list[str]:
    return CLASS_FEATURES_LEVEL_1.get(char_class, [])
