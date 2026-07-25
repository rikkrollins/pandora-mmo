"""
rules/leveling.py
Standard D&D 5E XP-to-level thresholds and proficiency bonus table.
"""

# Level -> minimum XP required to reach that level (5E core rules,
# levels 1-20). Real 5E has no official rules past 20 at all.
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

# Task #215, per Coffee: "if you can make it so characters can level up
# past 20 maybe?! - those other games goto 99" and "die hards may jus
# continue to level up to max all stats." Real 5E has no official
# levels past 20, so this is a genuine, documented house extension
# (levels 21-99), not a real-rules lookup like the table above.
# Deliberately real but bounded in what it grants further stat-wise:
# - proficiency_bonus_for_level already naturally stays capped at +6
#   past level 17 (no change needed) -- letting it keep climbing to
#   99 would eventually make this game's fixed DC-13 skill checks an
#   automatic success, breaking a different, deliberate simplification
#   (CLAUDE.md: "Skill checks use one fixed DC (13) for every
#   situation... to avoid the AI inventing difficulty numbers").
# - Ability scores already cap at 20 (_apply_asi_choice's own
#   min(..., 20)), so continuing to grant ASI points past level 20
#   (see ASI_LEVELS below) can't run away either -- it just lets a
#   long-term player eventually max every one of their six scores,
#   exactly the "grind to max all stats" goal above.
# - HP and skill points (db.add_xp's own per-level-gained logic) are
#   already generic and keep growing for every level past 20 too, with
#   no code change needed here.
# The XP curve itself keeps growing per level (a real, escalating
# grind, not a flat repeat) rather than trying to hand-author 79 more
# real thresholds one at a time.
_post_20_increment = 50000
for _lvl in range(21, 100):
    XP_THRESHOLDS[_lvl] = XP_THRESHOLDS[_lvl - 1] + _post_20_increment
    _post_20_increment += 5000
del _lvl, _post_20_increment

MAX_LEVEL = 99

# Prestige/rebirth system, per Coffee (2026-07-22): "keeps all stats
# but we go to lv one to exponentially level up our character again...
# maybe making for another rebirth." A rebirth only resets level/XP
# (see bot.py's _do_rebirth) -- ability scores, gear, gold, and every
# other stat stay exactly as they are, so the reward for going through
# it again has to come from somewhere else: each rebirth permanently
# raises this character's personal ability-score cap above the normal
# 20 (real "godly" territory over multiple rebirths) and grants a
# stacking XP-gain bonus, so the climb back to MAX_LEVEL is genuinely
# faster each time -- the actual "exponential" part Coffee asked for.
REBIRTH_ABILITY_CAP_BONUS_PER_REBIRTH = 2
REBIRTH_XP_BONUS_PER_REBIRTH = 0.25


def ability_score_cap(rebirth_count: int) -> int:
    """A character's personal ability-score ceiling -- 20 normally, +2 per rebirth."""
    return 20 + REBIRTH_ABILITY_CAP_BONUS_PER_REBIRTH * max(rebirth_count, 0)


def xp_gain_multiplier(rebirth_count: int) -> float:
    """Permanent, stacking XP-gain bonus earned by rebirthing -- 1.0 normally, +25% per rebirth."""
    return 1.0 + REBIRTH_XP_BONUS_PER_REBIRTH * max(rebirth_count, 0)


# Hybrid classes (per Coffee, 2026-07-22): gated behind a character's
# first rebirth -- earned, not available day one -- then freely
# pick/re-pick any of the other 11 classes as a secondary flavor.
# Depth is a direct function of total rebirth count, capped at 3 tiers
# since that's the actual designed content depth (see bot.py's
# HYBRID_CLASS_FEATURES) -- further rebirths beyond 3 keep paying off
# through the ability-score-cap/XP-multiplier side above, just don't
# add more hybrid depth.
HYBRID_MAX_TIER = 3


def hybrid_tier(rebirth_count: int) -> int:
    """How deep a character's chosen hybrid class goes -- 0 (locked) until the first rebirth, then min(rebirth_count, HYBRID_MAX_TIER)."""
    return min(max(rebirth_count, 0), HYBRID_MAX_TIER)


# Magic penetration (per Coffee, 2026-07-24: "work in magic bonuses to
# counter act the magic resistences - characters can eventually use
# this massively to their advantage" -- confirmed tied to rebirths,
# "use the evolutions for that"): the damage-type/resistance system
# being built alongside this (rules/combat.py's
# _apply_damage_type_modifier) is a real, permanent wall against a
# build that leans on the wrong damage type -- this is the earned
# counter to it, same stacking-per-rebirth shape as the ability-score
# cap/XP multiplier above, so surviving into deep rebirths pays off in
# a third, independent way. Deliberately only ever closes the gap back
# to normal damage (never grants bonus damage beyond that -- see
# _apply_damage_type_modifier), so this counters resistance rather than
# creating a new exploit; vulnerability and immunity are both
# untouched by it (immunity is a real, absolute wall by design; a
# vulnerability already favors the attacker with nothing to "counter").
MAGIC_PENETRATION_PCT_PER_REBIRTH = 10.0


def magic_penetration_pct(rebirth_count: int) -> float:
    """How much of a resistant target's damage reduction this character's rebirths have earned back -- 0% normally, +10% per rebirth, capped at 100% (fully countering resistance, never past normal damage)."""
    return min(100.0, MAGIC_PENETRATION_PCT_PER_REBIRTH * max(rebirth_count, 0))


# Real subclass choice, extended to the other 11 classes (2026-07-25,
# following Wizard's Arcane Tradition pilot -- see bot.py's
# _do_choose_subclass/CLASS_SUBCLASSES). Two genuine 5E archetypes per
# class. The FIRST name in each pair is that class's "combat" pick and
# grants the one universal mechanical hook this pass ships --
# COMBAT_SUBCLASS_DAMAGE_BONUS_PCT more weapon damage (rules/combat.py's
# resolve_attack, same real hook point the damage-type system uses).
# The SECOND name is a genuine, valid, sheet-showing pick with no
# mechanical bonus wired up yet -- honest about the gap rather than
# inventing one, same convention as every other documented "not built
# yet" feature in this codebase.
CLASS_SUBCLASSES = {
    "Barbarian": ("Berserker", "Totem Warrior"),
    "Fighter": ("Champion", "Battle Master"),
    "Paladin": ("Vengeance", "Devotion"),
    "Ranger": ("Hunter", "Beast Master"),
    "Rogue": ("Assassin", "Thief"),
    "Cleric": ("War", "Life"),
    "Sorcerer": ("Draconic", "Wild Magic"),
    "Warlock": ("Fiend", "Great Old One"),
    "Bard": ("Valor", "Lore"),
    "Druid": ("Moon", "Land"),
    "Monk": ("Shadow", "Open Hand"),
}

COMBAT_SUBCLASS_DAMAGE_BONUS_PCT = 20

# Every class's first-listed (combat) subclass name, flattened into one
# set -- this is the only thing rules/combat.py actually needs to know
# (a character's own subclass is only ever set to a name valid for
# THEIR OWN char_class by _do_choose_subclass, so a flat name check
# here can't leak across classes).
COMBAT_SUBCLASS_NAMES = frozenset(names[0] for names in CLASS_SUBCLASSES.values())

# Real mechanical hooks for the SECOND ("utility") name in each
# CLASS_SUBCLASSES pair (2026-07-25, per Coffee: "go ahead with the
# subclass hooks" -- closing the honesty gap the pilot above explicitly
# flagged: "no mechanical bonus is built for it yet"). Three archetypes
# get a distinct, name-appropriate mechanic reusing an existing system
# (Thief -> steal bonus, Life -> extra healing, Totem Warrior ->
# broader Rage resistance) since a real existing hook fit naturally;
# the rest get a flat ability-check bonus via this one shared table,
# consumed generically by rules/dice.roll_ability_check so it applies
# everywhere a check of that ability already happens (skill checks,
# gathering, shoving) with no per-site wiring needed.
UTILITY_SUBCLASS_CHECK_BONUS_VALUE = 2
UTILITY_SUBCLASS_ABILITY_CHECK_BONUS = {
    "Battle Master": "strength",     # tactical brawn
    "Devotion": "charisma",          # force of presence and faith
    "Beast Master": "wisdom",        # attuned to animals
    "Wild Magic": "charisma",        # chaotic force of personality
    "Great Old One": "intelligence",  # alien, half-understood knowledge
    "Lore": "intelligence",          # bardic knowledge
    "Land": "wisdom",                # deep attunement to nature
    "Open Hand": "wisdom",           # inner stillness and focus
}

# The 3 specially-handled utility archetypes (Thief/Life/Totem Warrior)
# -- bot.py/spells.py/rules/combat.py check these constants directly at
# their own real hook points rather than through the generic ability-
# check table above.
THIEF_SUBCLASS_STEAL_BONUS = 3
LIFE_SUBCLASS_HEAL_BONUS = 2
TOTEM_WARRIOR_SUBCLASS_NAME = "Totem Warrior"


def utility_subclass_ability_check_bonus(subclass: str | None, ability: str) -> int:
    """Real +2 on an ability check when this is exactly the archetype's own flagged ability."""
    if subclass and UTILITY_SUBCLASS_ABILITY_CHECK_BONUS.get(subclass) == ability.lower():
        return UTILITY_SUBCLASS_CHECK_BONUS_VALUE
    return 0


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


def wild_shape_damage_bonus(level: int) -> int:
    """
    Druid's Wild Shape (task #91 audit, 2026-07-19 -- found completely
    absent: Druid had zero unique mechanical features beyond spellcasting,
    the only one of the 12 classes with nothing here at all). Real 5E
    Wild Shape replaces your attacks with a beast's natural weapons
    (claws/bite) rather than a flat bonus, but this engine has no
    separate-statblock system for any class (Barbarian's Rage doesn't
    reroll as a bear either) -- reusing the same "bonus die on your
    existing weapon roll" simplification Rage already established,
    scaled the same way (a claw/bite is comparable in punch to a Rage
    swing, not weaker).
    """
    if level >= 16:
        return 4
    if level >= 9:
        return 3
    return 2


def wild_shape_temp_hp(level: int) -> int:
    """
    Real 5E Wild Shape's real combat value is a separate HP pool (the
    beast's own HP), not just bonus damage -- an honest, simplified
    stand-in given this engine's temp_hp mechanic (already used for the
    Warlock's Dark One's Blessing) rather than modeling a whole second
    statblock. Scales with level since a higher-CR beast at higher
    levels has proportionally more HP in real 5E too.
    """
    return level * 3


# Real 5E: Ability Score Improvements are available at these levels.
# Crossing one of these no longer auto-applies +2 to a fixed stat
# (2026-07-16, per Coffee) -- it banks 2 points per level crossed on
# character["pending_asi_points"] instead, and the player spends them
# whenever they want by saying "level up" (or immediately, via CLASS_
# PRIMARY_ABILITY, if they say "auto"/"do it for me"). See db.add_xp
# and bot.py's _do_level_up.
# Task #215: continues the exact same every-4-levels cadence past 19
# (23, 27, 31, ... 99) rather than a different post-20 rule -- real 5E
# never defines this, so consistency with the existing pattern is the
# more honest choice than inventing a distinct "epic" schedule.
ASI_LEVELS = {4, 8, 12, 16, 19} | set(range(23, 100, 4))

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


# Skill checks in this engine are ability-based, not the 18 named 5E
# skills (see ai/intent_parser.py's skill_check description) -- there's
# no per-character skill selection to look a real proficiency up in.
# This is a deliberate, fixed simplification (2026-07-16, audit found
# skill checks NEVER applied a proficiency bonus at all, for any class):
# each class gets proficiency on the two abilities its real 5E skill
# list leans on hardest, picked by flavor rather than reused from
# CLASS_SAVE_PROFICIENCIES (which doesn't line up for every class --
# Ranger's real skill list is Wisdom-heavy for tracking/perception/
# survival, matching Natural Explorer's own advantage bonus, not the
# Strength/Dexterity its saves happen to use).
CLASS_SKILL_ABILITIES = {
    "barbarian": ("strength", "wisdom"),
    "fighter": ("strength", "wisdom"),
    "paladin": ("wisdom", "charisma"),
    "ranger": ("wisdom", "dexterity"),
    "rogue": ("dexterity", "intelligence"),
    "monk": ("dexterity", "strength"),
    "bard": ("charisma", "dexterity"),
    "cleric": ("wisdom", "charisma"),
    "druid": ("wisdom", "intelligence"),
    "sorcerer": ("charisma", "wisdom"),
    "warlock": ("charisma", "intelligence"),
    "wizard": ("intelligence", "wisdom"),
}


def is_proficient_in_skill(char_class: str, ability: str) -> bool:
    return ability in CLASS_SKILL_ABILITIES.get((char_class or "").lower(), ())


# Real 5E class-table levels these unlock at.
ROGUE_EXPERTISE_LEVEL = 1
BARD_JACK_OF_ALL_TRADES_LEVEL = 2
BARD_EXPERTISE_LEVEL = 3


def skill_check_proficiency_bonus(char_class: str, level: int, ability: str, proficiency_bonus: int) -> int:
    """
    Real proficiency-bonus contribution to a skill check on `ability`,
    including Rogue/Bard Expertise (double proficiency, once unlocked)
    and Bard's Jack of All Trades (half proficiency, rounded down, on
    abilities the Bard ISN'T otherwise proficient in) -- both previously
    pure flavor text in class_features.py (2026-07-16). Simplifies away
    real 5E's "choose 2 skills" mechanic the same way
    is_proficient_in_skill already does: Expertise applies to BOTH of
    the class's fixed proficient abilities at once, not 2 separately
    chosen ones.
    """
    lowered_class = (char_class or "").lower()
    if is_proficient_in_skill(char_class, ability):
        if lowered_class == "rogue" and level >= ROGUE_EXPERTISE_LEVEL:
            return proficiency_bonus * 2
        if lowered_class == "bard" and level >= BARD_EXPERTISE_LEVEL:
            return proficiency_bonus * 2
        return proficiency_bonus
    if lowered_class == "bard" and level >= BARD_JACK_OF_ALL_TRADES_LEVEL:
        return proficiency_bonus // 2
    return 0


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
