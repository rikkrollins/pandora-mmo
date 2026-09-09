"""
models.py
Character/NPC/Session data structures and 5E-style starting-equipment
and starting-gold tables used at character creation.
"""
from dataclasses import dataclass, field

VALID_CLASSES = [
    "Fighter", "Wizard", "Rogue", "Cleric", "Ranger", "Barbarian",
    "Bard", "Druid", "Monk", "Paladin", "Sorcerer", "Warlock",
]

VALID_RACES = [
    "Human", "Elf", "Dwarf", "Halfling", "Half-Elf", "Dragonborn", "Tiefling",
]

# Starting equipment per class, as item_id -> quantity dicts referencing
# the catalog in items.py (only items that actually exist there — this
# is intentionally a condensed subset of full 5E starting gear).
STARTING_EQUIPMENT = {
    "Fighter": {"longsword": 1, "wooden_shield": 1, "chain_mail": 1, "rations": 3},
    "Wizard": {"rusty_dagger": 1, "healing_potion": 1, "rations": 3},
    "Rogue": {"shortsword": 1, "longbow": 1, "leather_armor": 1, "rations": 3},
    "Cleric": {"longsword": 1, "chain_shirt": 1, "wooden_shield": 1, "healing_potion": 1},
    "Ranger": {"longbow": 1, "shortsword": 1, "leather_armor": 1, "rations": 3},
    "Barbarian": {"greataxe": 1, "rations": 3},
    "Bard": {"shortsword": 1, "leather_armor": 1, "rations": 3},
    "Druid": {"leather_armor": 1, "rations": 3},
    "Monk": {"shortsword": 1, "rations": 3},
    "Paladin": {"longsword": 1, "wooden_shield": 1, "chain_mail": 1, "healing_potion": 1},
    "Sorcerer": {"rusty_dagger": 1, "healing_potion": 1, "rations": 3},
    "Warlock": {"rusty_dagger": 1, "leather_armor": 1, "rations": 3},
}

STARTING_GOLD = {
    "Fighter": 125, "Wizard": 100, "Rogue": 100, "Cleric": 125, "Ranger": 125,
    "Barbarian": 50, "Bard": 100, "Druid": 50, "Monk": 20, "Paladin": 125,
    "Sorcerer": 75, "Warlock": 100,
}

# Base armor class by class, before any DEX/shield bonuses are applied
# at character creation (kept simple/approximate for this game).
BASE_ARMOR_CLASS = {
    "Fighter": 16, "Paladin": 16, "Cleric": 16,
    "Ranger": 13, "Barbarian": 13, "Rogue": 12, "Monk": 12, "Bard": 12,
    "Wizard": 10, "Sorcerer": 10, "Warlock": 11, "Druid": 11,
}


@dataclass
class Character:
    telegram_user_id: int
    name: str
    race: str
    char_class: str
    level: int = 1
    xp: int = 0
    hp_current: int = 0
    hp_max: int = 0
    strength: int = 10
    dexterity: int = 10
    constitution: int = 10
    intelligence: int = 10
    wisdom: int = 10
    charisma: int = 10
    proficiency_bonus: int = 2
    armor_class: int = 10
    inventory: list = field(default_factory=list)
    gold: int = 0
    death_save_successes: int = 0
    death_save_failures: int = 0
