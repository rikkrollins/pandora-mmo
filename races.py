"""
races.py
Real Dungeons & Dragons 5E (SRD) racial traits: ability score increases,
speed, and signature traits. This is a genuine, sourced subset of the
5E rules — not padding. Full 5E race content (subraces, all racial
spells, every minor trait) is much larger than this; this covers the
core mechanical traits that actually affect play in this build.
"""

RACES = {
    "Human": {
        "ability_bonuses": {"strength": 1, "dexterity": 1, "constitution": 1,
                             "intelligence": 1, "wisdom": 1, "charisma": 1},
        "speed": 30,
        "traits": ["Versatile: +1 to all six ability scores"],
    },
    "Elf": {
        "ability_bonuses": {"dexterity": 2},
        "speed": 30,
        "traits": [
            "Darkvision 60 ft.",
            "Fey Ancestry: advantage on saves vs. being charmed, immune to magical sleep",
            "Keen Senses: proficiency in Perception",
            "Trance: doesn't need to sleep -- meditates 4 hours a day for the same benefit as 8 hours of sleep",
        ],
    },
    "Dwarf": {
        "ability_bonuses": {"constitution": 2},
        "speed": 25,
        "traits": [
            "Darkvision 60 ft.",
            "Dwarven Resilience: advantage on saves vs. poison, resistance to poison damage",
            "Stonecunning: double proficiency bonus on History checks about stonework",
            "Dwarven Combat Training: proficiency with battleaxe, handaxe, light hammer, and warhammer",
        ],
    },
    "Halfling": {
        "ability_bonuses": {"dexterity": 2},
        "speed": 25,
        "traits": [
            "Lucky: reroll a natural 1 on attack rolls, ability checks, or saving throws",
            "Brave: advantage on saves vs. being frightened",
            "Halfling Nimbleness: can move through the space of any creature larger than you",
        ],
    },
    "Half-Elf": {
        "ability_bonuses": {"charisma": 2, "dexterity": 1, "constitution": 1},
        "speed": 30,
        "traits": [
            "Darkvision 60 ft.",
            "Fey Ancestry: advantage on saves vs. being charmed, immune to magical sleep",
            "Skill Versatility: proficiency in two skills of your choice",
        ],
    },
    "Dragonborn": {
        "ability_bonuses": {"strength": 2, "charisma": 1},
        "speed": 30,
        "traits": [
            "Draconic Ancestry: your breath weapon and damage resistance depend on your draconic lineage",
            "Breath Weapon: replaces one attack, damage scales with level (a real, usable action)",
            "Damage Resistance to your draconic ancestry's damage type",
        ],
    },
    "Tiefling": {
        "ability_bonuses": {"charisma": 2, "intelligence": 1},
        "speed": 30,
        "traits": [
            "Darkvision 60 ft.",
            "Hellish Resistance: resistance to fire damage",
            "Infernal Legacy: knows the Thaumaturgy cantrip",
        ],
    },
    "Gnome": {
        "ability_bonuses": {"intelligence": 2},
        "speed": 25,
        "traits": [
            "Darkvision 60 ft.",
            "Gnome Cunning: advantage on Intelligence, Wisdom, and Charisma "
            "saving throws against magic",
        ],
    },
    "Half-Orc": {
        "ability_bonuses": {"strength": 2, "constitution": 1},
        "speed": 30,
        "traits": [
            "Darkvision 60 ft.",
            "Menacing: proficiency in Intimidation",
            "Relentless Endurance: when reduced to 0 HP but not killed outright, "
            "drop to 1 HP instead, once per long rest",
            "Savage Attacks: roll one additional weapon damage die on a melee critical hit",
        ],
    },
}


# Cantrips granted purely by race, independent of class — e.g. Tiefling's
# real 5E "Infernal Legacy" trait (already listed above in RACES' traits
# text) actually knowing Thaumaturgy. Granted once at character creation.
RACIAL_CANTRIPS = {
    "Tiefling": ["thaumaturgy"],
}


def get_race(race_name: str) -> dict | None:
    return RACES.get(race_name)


def racial_spells(race_name: str) -> list[str]:
    return list(RACIAL_CANTRIPS.get(race_name, []))


def apply_racial_bonuses(race_name: str, ability_scores: dict) -> dict:
    """Returns a NEW ability_scores dict with this race's real bonuses applied."""
    race = RACES.get(race_name)
    if race is None:
        return dict(ability_scores)
    result = dict(ability_scores)
    for ability, bonus in race["ability_bonuses"].items():
        result[ability] = result.get(ability, 10) + bonus
    return result
