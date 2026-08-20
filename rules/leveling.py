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
# other stat stay exactly as they are. The reward is a stacking XP-gain
# bonus, so the climb back to MAX_LEVEL is genuinely faster each time --
# the actual "exponential" part Coffee asked for. Rebirth used to also
# raise this character's personal ability-score cap above the normal 20,
# but ability scores have no cap at all anymore (2026-08-20, per Coffee:
# "I want the cap removed so we can increase it via points - we earned
# the points we shud be able to use it") -- see bot.py's
# _apply_asi_choice.
REBIRTH_XP_BONUS_PER_REBIRTH = 0.25


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


# "The World Evolves" (2026-08-14, per Coffee: "when a character
# evolves, the worlds forces get stronger... using harder resistences
# and elemental damages... more evolutions more evolved the world
# around you becomes... you want to make ur player godly to break out
# of it" -- confirmed as a deliberate, systemic mechanic: "the game is
# meant to be played multiple times so that shud be a working mechanic
# to make the evolutions make sense"). This is the OTHER half of the
# tension magic_penetration_pct above only ever provided one side of:
# a rebirth already earns real, permanent counter-play against a
# RESISTANT monster, but nothing ever made the world itself lean on
# that resistance harder as the player kept evolving -- so a deeply
# reborn character's own penetration was countering a threat that
# never actually grew to match them.
#
# Deliberately set HIGHER than MAGIC_PENETRATION_PCT_PER_REBIRTH (15 vs
# 10) so the tension is real and felt well before rebirth 10 -- a
# rebirth-5 party faces 75% world resistance while their own
# penetration only cuts 50% of it back, a genuine mid-game difficulty
# spike. Deliberately UNCAPPED (unlike magic_penetration_pct's explicit
# 100% ceiling): the existing apply_damage_type_modifier math already
# provides the real "godly breakthrough" for free -- at exactly 100%
# penetration (rebirth 10), `elemental_pct * (1 - penetration)` reduces
# ANY amount of resistance, no matter how high the world has climbed,
# straight to zero effect. Nothing else needs to change for that payoff
# to already work; this only ever needed its missing other half.
#
# Only ever scales with the PARTY'S/PLAYER'S OWN rebirth_count, never a
# monster's (monsters have no rebirth_count at all) -- a rebirth-0
# character sees zero change from either constant below, so this never
# touches or contradicts the separate, deliberately one-directional
# level-based scaling in overtuned_monster_stat_multiplier above.
# Real live instruction (2026-08-20, Coffee, Noita NG+ reference --
# enemy HP/attack-rate/player-damage-multipliers all COMPOUND per NG+
# level, never a flat linear add): "keep in mind when the player
# evolves the same thing happens for the players, eventually causing
# exponential growth." Both sides of this arms race now share ONE real
# growth rate -- the world's own scaling below, and the player's own
# rebirth_power_multiplier further down -- rather than escalating at
# arbitrarily different, undocumented rates. Deliberately uncapped,
# same "godly, breaking the game on purpose" precedent this session
# already set (ability-score cap removal, uncapped Mastery Overflow):
# Coffee's own words, "this is why 'breaking the game' mechanics in
# needed so the players can beat the impossible bosses."
REBIRTH_POWER_GROWTH_RATE = 1.5  # +50% per rebirth, compounding


def rebirth_power_multiplier(rebirth_count: int) -> float:
    """
    The player's own compounding power growth per rebirth -- the
    missing "other half" of world_damage_multiplier's own growth
    below, applied once at the real universal choke-point every damage
    source in this game already shares (rules.combat.apply_damage_
    type_modifier), so attacks/spells/summons/mastery-bonus damage all
    scale together automatically. Exactly 1.0 at rebirth 0 -- a never-
    reborn character sees byte-for-byte the same damage as before this
    system existed.
    """
    return REBIRTH_POWER_GROWTH_RATE ** max(rebirth_count, 0)


def world_resistance_pct(party_rebirth_count: float) -> float:
    """
    How much extra elemental_resistance_pct the world's monsters gain
    per the party's own average rebirth_count -- now the same
    compounding curve as rebirth_power_multiplier (was flat +15%/
    rebirth). Safe uncapped: rules.combat.apply_damage_type_modifier's
    own magic_penetration_pct math already fully negates ANY resistance
    magnitude once penetration reaches 100% (`elemental_pct * (1 -
    1.0) == 0`, regardless of how large elemental_pct itself is), and
    elemental_overflow_heal already handles the >100% case -- nothing
    else needed to change for this to compound safely.
    """
    return (REBIRTH_POWER_GROWTH_RATE ** max(party_rebirth_count, 0) - 1.0) * 100


def world_damage_multiplier(party_rebirth_count: float) -> float:
    """How much harder the world's monsters hit per the defending player's own rebirth_count -- same compounding curve as rebirth_power_multiplier (was flat +10%/rebirth), so both sides of the fight escalate at the same real rate."""
    return REBIRTH_POWER_GROWTH_RATE ** max(party_rebirth_count, 0)


# Turn-based equivalent of Noita's exponential "attacks faster" NG+
# axis (real-time attack-rate scaling has no direct analog in turn-
# based combat) -- generalizes the existing extra_attack_when_enraged
# boss-only flag (bot.py) into a real, universal, rebirth-scaled bonus
# any monster's turn can read. Deliberately CAPPED, unlike the damage
# multipliers above: an uncapped ACTION COUNT risks a genuinely
# unplayable/endless combat round, a different kind of risk than a
# large damage number (always resolved in a bounded number of hits).
# Same "tiered, capped" shape bot._summons_per_battle already
# established for a comparable "more of a good thing" scaling curve.
EXTRA_MONSTER_ACTIONS_PER_REBIRTH_TIER = 3.0  # +1 action per this many average party rebirths
EXTRA_MONSTER_ACTIONS_CAP = 3


def extra_monster_actions(party_rebirth_count: float) -> int:
    """Bonus actions per round any monster's turn gets on top of its own baseline, from the party's own average rebirth_count -- 0 at rebirth 0 (unchanged from today), capped at EXTRA_MONSTER_ACTIONS_CAP."""
    return min(int(max(party_rebirth_count, 0) // EXTRA_MONSTER_ACTIONS_PER_REBIRTH_TIER), EXTRA_MONSTER_ACTIONS_CAP)


# "Smarter, Formation-Aware Enemy AI" Phase D (2026-08-14, per Coffee:
# "use all those mechanics so as the game evolves the AI gets more
# creative and strategic. Getting harder and harder."). Two previously
# FIXED constants in bot._pick_formation_weighted_target (config.
# FRONT_ROW_TARGET_CHANCE and its hardcoded +0.1 weight floor) become
# rebirth-scaled here, using the exact same party_rebirth_count input
# world_resistance_pct/world_damage_multiplier already read -- a
# never-reborn party sees the exact base values unchanged (both
# formulas below resolve to base_value at rebirth 0), so nothing about
# a first playthrough changes; only a party that's actually evolved
# faces a genuinely more disciplined, sharper-targeting world. Capped
# at the same rebirth-10 tier magic_penetration_pct's own 100% ceiling
# uses, for the same "a fixed, learnable ceiling, not infinite scaling"
# reason.
FORMATION_DISCIPLINE_REBIRTH_CAP = 10.0
FRONT_ROW_TARGET_CHANCE_CEILING = 0.95
FORMATION_TARGET_WEIGHT_FLOOR_SHARPENED = 0.02


def formation_target_discipline(party_rebirth_count: float, base_chance: float) -> float:
    """
    How far FRONT_ROW_TARGET_CHANCE has closed its own gap toward
    FRONT_ROW_TARGET_CHANCE_CEILING, given the party's own average
    rebirth_count -- a genuinely more disciplined "focus the aggressor
    row, protect the back row" instinct the more the world has evolved.
    """
    progress = min(max(party_rebirth_count, 0), FORMATION_DISCIPLINE_REBIRTH_CAP) / FORMATION_DISCIPLINE_REBIRTH_CAP
    return base_chance + (FRONT_ROW_TARGET_CHANCE_CEILING - base_chance) * progress


def formation_target_weight_floor(party_rebirth_count: float, base_floor: float) -> float:
    """
    The inverse shape of formation_target_discipline above: the HP-
    weighted targeting floor SHRINKS toward FORMATION_TARGET_WEIGHT_
    FLOOR_SHARPENED as rebirth climbs -- less randomness, a sharper,
    more decisive read on who's actually the highest-value target.
    """
    progress = min(max(party_rebirth_count, 0), FORMATION_DISCIPLINE_REBIRTH_CAP) / FORMATION_DISCIPLINE_REBIRTH_CAP
    return base_floor - (base_floor - FORMATION_TARGET_WEIGHT_FLOOR_SHARPENED) * progress


# Real subclass choice, extended to the other 11 classes (2026-07-25,
# following Wizard's Arcane Tradition pilot -- see bot.py's
# _do_choose_subclass/CLASS_SUBCLASSES). Two genuine 5E archetypes per
# class. The FIRST name in each pair is that class's "combat" pick and
# grants COMBAT_SUBCLASS_DAMAGE_BONUS_PCT more damage -- originally
# weapon-only (rules/combat.py's resolve_attack), extended in a
# 2026-08-14 full synergy audit to spell damage too (spells.
# resolve_damage_spell), since Draconic/Fiend/War/Moon/Valor belong to
# classes that deal most of their real damage through spells, not
# weapon swings. The SECOND name is the "utility" pick -- every one of
# them resolves to a real mechanic via describe_subclass_effect below
# (UTILITY_SUBCLASS_ABILITY_CHECK_BONUS's flat ability-check bonus for
# most, or Thief/Life/Totem Warrior's own bespoke hooks) -- this used to
# say "no mechanical bonus wired up yet," which was already stale the
# same day it was written; corrected in that same 2026-08-14 audit.
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
# here can't leak across classes). "Assassin" (2026-08-08) is
# deliberately EXCLUDED here even though it's Rogue's first-listed
# subclass -- it now gets its own real, distinct Backstab multiplier
# (see BACKSTAB_LEVEL_TIERS/bot.py's _effective_backstab_multiplier)
# instead of this generic flat bonus, so it doesn't also double-dip
# into the ordinary combat-subclass damage boost every other class's
# first pick still gets.
COMBAT_SUBCLASS_NAMES = frozenset(names[0] for names in CLASS_SUBCLASSES.values()) - {"Assassin"}

# Assassin's Backstab damage multiplier tiers (2026-08-08, per Coffee:
# spread across the full 1-100 level range, not clustered early --
# quarters of the range). This is the LIVE, level-based factor for the
# character's CURRENT rebirth cycle; the character's own persistent
# backstab_base_multiplier (db.py) then multiplies on top of this, so
# a reborn Assassin re-climbing these same tiers compounds an already-
# earned base rather than starting flat again -- see bot.py's
# _effective_backstab_multiplier and _do_rebirth for the actual hook.
BACKSTAB_LEVEL_TIERS = ((75, 10), (50, 8), (25, 4), (1, 2))


def backstab_tier_multiplier(level: int) -> int:
    """The live x2/x4/x8/x10 tier for a given character level, before backstab_base_multiplier is applied."""
    for threshold, multiplier in BACKSTAB_LEVEL_TIERS:
        if level >= threshold:
            return multiplier
    return 1

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


def describe_subclass_effect(subclass_name: str) -> str:
    """
    One short, honest, mechanically-accurate line for a subclass name --
    real live gap (2026-08-14, Coffee: "it wants me to choose without
    explaining what they even do for my character"): a player facing the
    "choose a subclass" prompt (guilds.eligible_for_guild's own rejection
    message, and bot._do_choose_subclass's "which subclass?" re-ask) only
    ever saw the two bare NAMES, never what either one actually does --
    the real mechanical payoff only ever got revealed by
    bot._do_choose_subclass AFTER the player already committed to one.
    Built here (not bot.py) so guilds.py can show it too without a
    circular import -- every number here is the same real constant
    bot._do_choose_subclass's own post-choice message already uses, just
    surfaced a step earlier.
    """
    if subclass_name == "Assassin":
        return "every attack becomes a Backstab attempt for bonus damage, landing more often the more you use it"
    if subclass_name == "Thief":
        return f"+{THIEF_SUBCLASS_STEAL_BONUS} on every steal attempt"
    if subclass_name == "Life":
        return f"healing spells restore +{LIFE_SUBCLASS_HEAL_BONUS} extra HP"
    if subclass_name == TOTEM_WARRIOR_SUBCLASS_NAME:
        return "while raging, spell damage is halved too, not just weapon hits"
    if subclass_name in COMBAT_SUBCLASS_NAMES:
        return f"weapon attacks deal +{COMBAT_SUBCLASS_DAMAGE_BONUS_PCT}% more damage"
    ability = UTILITY_SUBCLASS_ABILITY_CHECK_BONUS.get(subclass_name)
    if ability:
        return f"+{UTILITY_SUBCLASS_CHECK_BONUS_VALUE} on every {ability.capitalize()} check"
    return "a real, chosen subclass, though no mechanical bonus is built for it yet"


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


# HP scaling for evolutions/rebirths (2026-07-25, per Coffee: "by level
# 99 the first playthrough before evolution the HP would be apx 3200 -
# then scale that"). EVOLUTION_HP_MULTIPLIER scales every level-up's HP
# gain (db.py's add_xp) well past the vanilla-5E hp_gain_for_level
# above, landing an average level-1-to-99 climb around ~3200 max HP
# with no rebirths yet. REBIRTH_HP_MULTIPLIER then doubles current
# hp_max at each rebirth (bot.py's _do_rebirth, on top of that life's
# own level-up growth) -- reaches six-figure HP by roughly rebirth 5,
# matching the scale potions were re-tuned for (2026-07-25: Healing
# Potion 100, Greater 1,000, Supreme 10,000).
EVOLUTION_HP_MULTIPLIER = 5
REBIRTH_HP_MULTIPLIER = 2


def rebirth_hp_max(current_hp_max: int) -> int:
    """New hp_max on rebirth -- a straight doubling, never a power loss."""
    return current_hp_max * REBIRTH_HP_MULTIPLIER


def _reference_hp_at(level: int, rebirth_count: int, evo_mult: int) -> int:
    """Shared internal reference curve for power_scale_ratio -- same shape as full_hp_max_for, parameterized by evo_mult so old (mult=1) vs. new (mult=5) can be compared."""
    con_mod = (12 - 10) // 2  # a neutral con=12 reference, same baseline used throughout the 2026-07-26 rebalance
    hp_per_level = max(10 // 2 + 1 + con_mod, 1)  # Fighter hit die (10) as a neutral reference class
    hp = max(10 + con_mod, 1)
    for _ in range(rebirth_count):
        hp += hp_per_level * (MAX_LEVEL - 1) * evo_mult
        hp *= REBIRTH_HP_MULTIPLIER
    hp += hp_per_level * (level - 1) * evo_mult
    return hp


def power_scale_ratio(level: int, rebirth_count: int) -> float:
    """
    How much a real character's own OUTGOING damage (weapon attacks,
    spells) should be scaled up at this level/rebirth_count, to keep
    pace with the same 2026-07-26 rebalance that already rescaled
    every monster's HP and damage.

    Real gap found 2026-07-26 (Coffee: "rebalance everything... all
    skills and abilities and magic and spells and cantrips"): weapon
    damage_bonus in items.py tops out at +3 (the rarest legendary
    reward in the whole game) and spell damage_dice in spells.py never
    scales past its fixed value at all -- both completely flat,
    vanilla-5E-scale numbers untouched by EVOLUTION_HP_MULTIPLIER,
    while monster HP/damage (and player HP) now both scale 4-5x+ by
    late game. Rather than hand-retuning dozens of individual weapon/
    spell numbers (fragile, and drifts again the next time a scaling
    constant changes), this reuses the EXACT SAME ratio methodology
    already proven for the monster rebalance: how much bigger a
    same-tier reference character's own hp_max is today vs. under the
    original (evo_mult=1) formula. Applied multiplicatively to a real
    attacker's own weapon/spell damage in rules/combat.py and
    spells.py -- monsters are UNAFFECTED (they carry their own already-
    scaled damage_dice/damage_bonus directly in campaign.json, never
    read this function).
    """
    new_hp = _reference_hp_at(level, rebirth_count, EVOLUTION_HP_MULTIPLIER)
    old_hp = _reference_hp_at(level, rebirth_count, 1)
    return new_hp / old_hp


def full_hp_max_for(char_class: str, constitution: int, level: int, rebirth_count: int) -> int:
    """
    Reconstructs the hp_max a character WOULD have today, from scratch,
    under the current EVOLUTION_HP_MULTIPLIER/REBIRTH_HP_MULTIPLIER rules
    -- i.e. as if they'd leveled from 1 through every one of their
    real, already-completed rebirths under today's formula the whole
    way, not whatever formula happened to be live at the moment they
    actually leveled.

    Real gap (2026-07-26, per Coffee: "fix the characters so their HP
    matches the new HP values so previous made players can hit the
    3200+ HP mark too"): hp_max in the database is CUMULATIVE, built up
    incrementally one add_xp() call at a time (db.py) -- so any level-up
    that happened before EVOLUTION_HP_MULTIPLIER existed (2026-07-25)
    permanently baked in the OLD, unscaled HP gain for that level-up,
    even after the multiplier shipped. This recomputes the number
    deterministically from only real, current facts (class,
    constitution, level, rebirth_count) -- no stored history needed,
    same "recompute from real current data, never trust a stale stored
    derivation" discipline as xp_gain_multiplier.

    _do_rebirth only allows rebirthing at MAX_LEVEL, so every COMPLETED
    rebirth cycle is known to have leveled 1 -> MAX_LEVEL in that life
    before the doubling; only the CURRENT (still-in-progress) life may
    stop short of MAX_LEVEL, at `level`.
    """
    con_mod = (constitution - 10) // 2
    hit_die = CLASS_HIT_DICE.get(char_class.lower(), 8)
    hp_per_level = hp_gain_for_level(char_class, con_mod)
    hp = max(hit_die + con_mod, 1)
    for _ in range(rebirth_count):
        hp += hp_per_level * (MAX_LEVEL - 1) * EVOLUTION_HP_MULTIPLIER
        hp *= REBIRTH_HP_MULTIPLIER
    hp += hp_per_level * (level - 1) * EVOLUTION_HP_MULTIPLIER
    return hp


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


def overtuned_monster_stat_multiplier(party_levels: list[int], monster_xp_reward: int, floor: float = 0.2) -> float:
    """
    Real live bug (2026-08-10, per Coffee: "Make it so when these ai
    characters attack, they are the average party level. This one was
    clearly way out of its league."): scaled_enemy_count above already
    scales the encounter's NUMBER of monsters to the party's real 5E
    Medium-difficulty XP budget, but never touches a single monster's
    own raw stats -- so a party far below a monster's native challenge
    level still faced its full, untouched (and potentially crushing)
    HP/damage even at the minimum count of 1.

    One-directional by design, same precedent as
    _balance_companion_level_to_party (2026-08-01, bot.py: "only ever
    RAISES a companion... never lowers one back down"): only ever
    shrinks an OVERTUNED monster down toward the party's real level,
    never buffs an already-easy one up for a high-level party --
    the reported problem was a monster too strong for the party, never
    the reverse, and inflating harmless low-tier monsters would be new,
    unrequested difficulty, not a fix for what was actually reported.

    Floored (never below `floor`) so an extreme mismatch (a very
    high-level monster template vs a level-1 party) still leaves a
    REAL fight, not a one-shot joke -- same "clamp to avoid a
    degenerate result" convention as ECHO_TRIAL_TIER_STAT_BONUS_PCT.
    """
    if not party_levels or monster_xp_reward <= 0:
        return 1.0
    avg_level = round(sum(party_levels) / len(party_levels))
    reference_budget = MEDIUM_ENCOUNTER_XP_PER_CHARACTER.get(min(max(avg_level, 1), 20), 5700)
    if monster_xp_reward <= reference_budget:
        return 1.0
    return max(floor, reference_budget / monster_xp_reward)


# Real live report (2026-08-15, per Coffee, dev-topic screenshot at
# Greymoor Downs: "All of these areas shud be lv20+ ... I want the
# enemies to have about 500 HP ... enemies do apx 30-150 damage (at the
# most) per hit ... make [a boss like the_unspoken] one boss at 3000hp
# ... We have to raise the bar, the game is too easy"). The missing
# OTHER direction of overtuned_monster_stat_multiplier just above: that
# function only ever shrinks a monster too STRONG for the party,
# deliberately never buffs one too WEAK -- exactly the gap here, a
# level-20 party outleveling this content entirely (barrow_bound_wolf
# was 99 HP/140 xp_reward, the_unspoken was 200 HP/350 xp_reward,
# neither remotely matched to a real level-20 Medium-encounter budget
# of 5700).
#
# Two DIFFERENT ceilings, not one shared multiplier: calibrated against
# Coffee's own two real reference points, which don't fit a single
# ratio (barrow_bound_wolf needs ~5x to reach ~500 HP; the_unspoken
# needs ~15x to reach 3000 HP). This isn't arbitrary -- it matches how
# this game already treats trash mobs vs bosses everywhere else
# (scaled_enemy_count/overtuned_monster_stat_multiplier both already
# exempt is_boss from count/shrink scaling, "whose difficulty spike is
# intentional, not a bug"): trash is meant to die fast regardless of
# level (a quick, satisfying kill, not a threat on its own), while a
# named boss is the real "raise the bar" content -- so trash gets the
# smaller ceiling, a boss the larger one.
UNDERTUNED_TRASH_STAT_CEILING = 5.0
UNDERTUNED_BOSS_STAT_CEILING = 15.0

# Real calibration finding: applying the SAME multiplier to damage_bonus
# that HP gets badly overshoots Coffee's own explicit "30-150 damage at
# the most" ceiling (the_unspoken's 17 damage_bonus * 15x = 255, a
# single hit exceeding his stated MAXIMUM outright). The actual
# complaint was about fights ending too fast, not about being
# under-hit -- these monsters already deal real damage (existing
# damage_bonus values run high across this whole catalog); more HP is
# what makes the fight LAST and threaten across multiple rounds. Damage
# scales by the SQUARE ROOT of the same ratio instead -- still
# genuinely harder, verified against real player HP at level 20
# (rules.leveling.full_hp_max_for: ~580-870 HP depending on class) to
# land as a real, felt threat without ever approaching a one-shot.
UNDERTUNED_DAMAGE_SCALE_EXPONENT = 0.5

# Real 5E already treats a monster below a party's Medium budget as
# completely normal and intended -- that's just an "Easy" encounter,
# not a bug (Easy/Medium/Deadly bands routinely span a 2-4x XP spread
# per the DMG's own encounter-building rules). Found while sanity-
# checking this against ordinary early-game progression: WITHOUT a real
# floor here, a level-2 party fighting the exact starter goblin they're
# meant to be grinding (goblin xp_reward=50, lvl2 budget=100, ratio 2.0)
# would already get a 2x buff -- unrequested difficulty during totally
# normal leveling, exactly what overtuned_monster_stat_multiplier's own
# docstring already warns against creating. Growth now only engages
# once the gap is genuinely large (the monster's xp_reward is at least
# this many times below budget) -- both of Coffee's real reference
# points (barrow_bound_wolf ~40.7x, the_unspoken ~16.3x raw ratio) clear
# this by a wide margin; a level-2 party's starter goblin (2x) does not.
UNDERTUNED_GROWTH_THRESHOLD_RATIO = 4.0


def undertuned_monster_stat_multiplier(
    party_levels: list[int], monster_xp_reward: int, is_boss: bool = False,
) -> float:
    """
    The grow-direction sibling of overtuned_monster_stat_multiplier
    above -- see that function's docstring for the shared XP-budget
    mechanism (rules.leveling.MEDIUM_ENCOUNTER_XP_PER_CHARACTER) this
    mirrors. Returns 1.0 (no change) whenever the monster's own
    xp_reward is within UNDERTUNED_GROWTH_THRESHOLD_RATIO of the
    party's real Medium-encounter budget -- an ordinary "Easy" encounter
    a party is merely somewhat ahead of is left completely alone, same
    as real 5E treats it; this function ONLY ever grows a monster the
    party has badly outleveled, never shrinks one, so it's always safe
    to call alongside the existing shrink function without the two ever
    fighting each other (they can never both return something other
    than 1.0 for the same monster).

    `is_boss`: unlike the shrink function above (which exempts bosses
    entirely, since weakening a hand-placed boss was never the goal),
    growth intentionally DOES apply to bosses -- a boss the party has
    badly outleveled is exactly Coffee's own reported case, and a
    bigger ceiling here reflects that a named boss is meant to be the
    real centerpiece fight once genuinely scaled to the party's level.
    """
    if not party_levels or monster_xp_reward <= 0:
        return 1.0
    avg_level = round(sum(party_levels) / len(party_levels))
    reference_budget = MEDIUM_ENCOUNTER_XP_PER_CHARACTER.get(min(max(avg_level, 1), 20), 5700)
    if monster_xp_reward * UNDERTUNED_GROWTH_THRESHOLD_RATIO >= reference_budget:
        return 1.0
    ceiling = UNDERTUNED_BOSS_STAT_CEILING if is_boss else UNDERTUNED_TRASH_STAT_CEILING
    return min(ceiling, reference_budget / monster_xp_reward)
