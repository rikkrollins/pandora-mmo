"""
rules/leveling.py
Standard D&D 5E XP-to-level thresholds and proficiency bonus table.
"""

# Level -> minimum XP required to reach that level (5E core rules)
XP_THRESHOLDS = {
    1: 0,
    2: 300,
    3: 900,
    4: 2700,
    5: 6500,
    6: 14000,
    7: 23000,
    8: 34000,
    9: 48000,
    10: 64000,
    11: 85000,
    12: 100000,
    13: 120000,
    14: 140000,
    15: 165000,
    16: 195000,
    17: 225000,
    18: 265000,
    19: 305000,
    20: 355000,
}


def level_for_xp(xp: int) -> int:
    """Return the correct level for a given total XP amount."""
    level = 1
    for lvl, threshold in sorted(XP_THRESHOLDS.items()):
        if xp >= threshold:
            level = lvl
        else:
            break
    return level


def proficiency_bonus_for_level(level: int) -> int:
    """5E proficiency bonus table."""
    if level >= 17:
        return 6
    if level >= 13:
        return 5
    if level >= 9:
        return 4
    if level >= 5:
        return 3
    return 2


# 5E hit die per class, used for HP calculation at level 1 and level-ups
CLASS_HIT_DICE = {
    "barbarian": 12,
    "fighter": 10,
    "paladin": 10,
    "ranger": 10,
    "bard": 8,
    "cleric": 8,
    "druid": 8,
    "monk": 8,
    "rogue": 8,
    "warlock": 8,
    "sorcerer": 6,
    "wizard": 6,
}

def breath_weapon_dice_count(level: int) -> int:
    """
    Real 5E Dragonborn Breath Weapon damage scaling: 2d6 at levels
    1-5, 3d6 at 6-10, 4d6 at 11-15, 5d6 at 16-20.
    """
    if level >= 16:
        return 5
    if level >= 11:
        return 4
    if level >= 6:
        return 3
    return 2


def sneak_attack_dice_count(level: int) -> int:
    """
    Real 5E Rogue Sneak Attack scaling: 1d6 at levels 1-2, up to 10d6
    at 19-20, gaining a die every 2 levels. Found frozen at a flat 1d6
    regardless of level (2026-07-16 audit) -- rules/combat.py's Sneak
    Attack always rolled exactly one extra d6.
    """
    return (level + 1) // 2


def rage_damage_bonus(level: int) -> int:
    """
    Real 5E Barbarian Rage bonus damage: +2 at levels 1-8, +3 at 9-15,
    +4 at 16-20. Found frozen at a flat +2 regardless of level
    (2026-07-16 audit) -- rules/combat.py always added exactly +2.
    """
    if level >= 16:
        return 4
    if level >= 9:
        return 3
    return 2


# Real 5E: Ability Score Improvements are available at these levels.
# In the real game, a player CHOOSES which score(s) to raise (or takes a
# feat instead). This build has no interactive choice mechanism yet, so
# it auto-applies +2 to the class's primary ability (capped at 20) — a
# reasonable, documented simplification, not a substitute for the real
# player choice a table would normally make.
ASI_LEVELS = {4, 8, 12, 16, 19}

CLASS_PRIMARY_ABILITY = {
    "barbarian": "strength", "fighter": "strength", "paladin": "strength",
    "rogue": "dexterity", "ranger": "dexterity", "monk": "dexterity",
    "wizard": "intelligence",
    "cleric": "wisdom", "druid": "wisdom",
    "bard": "charisma", "sorcerer": "charisma", "warlock": "charisma",
}

# Real 5E: every class is proficient in exactly 2 saving throws. Found
# unused (2026-07-16 audit) -- spells.py's save-roll code only ever
# added the raw ability modifier, never a proficiency bonus, because
# this table didn't exist yet to look one up in.
CLASS_SAVE_PROFICIENCIES = {
    "barbarian": ("strength", "constitution"),
    "fighter": ("strength", "constitution"),
    "paladin": ("wisdom", "charisma"),
    "ranger": ("strength", "dexterity"),
    "rogue": ("dexterity", "intelligence"),
    "monk": ("strength", "dexterity"),
    "bard": ("dexterity", "charisma"),
    "cleric": ("wisdom", "charisma"),
    "druid": ("intelligence", "wisdom"),
    "sorcerer": ("constitution", "charisma"),
    "warlock": ("wisdom", "charisma"),
    "wizard": ("intelligence", "wisdom"),
}


def is_proficient_in_save(char_class: str, ability: str) -> bool:
    return ability in CLASS_SAVE_PROFICIENCIES.get((char_class or "").lower(), ())


def hp_gain_for_level(char_class: str, constitution_modifier: int) -> int:
    """
    Real 5E average-HP-per-level rule (the PHB explicitly allows using
    the average instead of rolling): hit_die/2 + 1, plus CON modifier,
    minimum 1 HP gained per level.
    """
    hit_die = CLASS_HIT_DICE.get(char_class.lower(), 8)
    return max(hit_die // 2 + 1 + constitution_modifier, 1)


# Real 5E DMG "Medium difficulty" encounter XP budget, per individual
# character, by level (2014 DMG encounter-building table).
MEDIUM_ENCOUNTER_XP_PER_CHARACTER = {
    1: 50, 2: 100, 3: 150, 4: 250, 5: 500, 6: 600, 7: 750, 8: 900,
    9: 1100, 10: 1200, 11: 1600, 12: 2000, 13: 2200, 14: 2500,
    15: 2800, 16: 3200, 17: 3900, 18: 4200, 19: 4900, 20: 5700,
}

# Real 5E DMG encounter multiplier, by number of monsters in the fight
# (the multiplier normally also shifts a column for a very small/large
# party -- deliberately not modeled here, since this game's parties are
# always small (1-6), matching other intentional simplifications
# already in this build). Only goes to 4 since bot.py's own enemy-count
# parsing already caps encounters at 4 for plain-text targeting.
_ENCOUNTER_MULTIPLIER_BY_COUNT = {1: 1, 2: 1.5, 3: 2, 4: 2}


def scaled_enemy_count(party_levels: list[int], monster_xp_reward: int, max_count: int = 4) -> int:
    """
    Picks how many of a given monster makes a real 5E "Medium difficulty"
    encounter for this party, instead of always defaulting to a single
    monster regardless of party size or level. Sums each party member's
    own Medium XP budget (real 5E table above), then finds the largest
    monster count (1..max_count) whose adjusted encounter XP -- count *
    monster_xp_reward * the real monster-count multiplier -- doesn't
    exceed that combined budget. Always returns at least 1.
    """
    if not party_levels:
        return 1
    budget = sum(MEDIUM_ENCOUNTER_XP_PER_CHARACTER.get(min(max(lvl, 1), 20), 5700) for lvl in party_levels)
    best = 1
    for count in range(1, max_count + 1):
        multiplier = _ENCOUNTER_MULTIPLIER_BY_COUNT[count]
        if count * monster_xp_reward * multiplier <= budget:
            best = count
        else:
            break
    return best
