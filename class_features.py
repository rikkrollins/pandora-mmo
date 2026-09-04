"""
class_features.py
Real Dungeons & Dragons 5E (SRD) class features available at character
level 1, shown on the character sheet as real, accurate information.

2026-07-11/12: nine of these are now genuinely mechanical, not just
flavor text, backed by a real limited-use resource (db.py's
feature_uses, resets on a full rest), an in-memory combat condition
(matching prone/poisoned), a real-time healing-curve tweak, or a
character-creation/combat-math tweak:
  - Fighter's Second Wind (real command, heals 1d10+level HP, 1/rest)
  - Barbarian's Rage (real command, bonus damage + damage resistance
    for the fight, 2/rest -- simplified to last until combat ends and
    to resist ALL damage rather than the three specific physical types,
    since this system has no damage-type modeling at all)
  - Rogue's Sneak Attack (automatic +1d6 when attacking with advantage
    -- no adjacent-ally trigger, since this system has no positioning)
  - Bard's Bardic Inspiration (real command, immediate +1d6 HP to an
    ally, uses = CHA modifier/rest -- simplified from a deferred bonus
    on the ally's next roll, which would need plumbing into every roll
    call site in the game)
  - Paladin's Lay on Hands (real command, heals 5 x level HP, once per
    rest -- simplified to spending the whole pool at once rather than
    a separately-spendable partial pool)
  - Wizard's Arcane Recovery (real command, recovers ceil(level/2)
    spell slots once per rest -- real 5E caps this by COMBINED spell
    level with a 6th-level-or-higher exclusion, but spell_slots_current/
    max here are a flat count with no per-level tracking at all, same
    simplification already used everywhere else in this build, so this
    recovers a NUMBER of slots instead of a combined level)
  - Warlock's Pact Magic (automatic, no command -- Warlocks recover
    spell slots on bot.py's WARLOCK_PACT_MAGIC_REST_HOURS curve, 1/8th
    of everyone else's NATURAL_HEALING_FULL_REST_HOURS, reflecting real
    5E's short-rest recovery vs. everyone else's long-rest recovery;
    their HP still heals on the normal shared curve) AND Otherworldly
    Patron (automatic, no command): every Warlock defaults to The Fiend
    (no in-game subclass-choice mechanism exists, same convention as
    Sorcerer's Draconic Bloodline below) -- Dark One's Blessing grants
    temp HP = CHA modifier + level (minimum 1, real 5E formula) whenever
    a Warlock reduces a hostile creature to 0 HP (rules/combat.py's
    resolve_attack). Temp HP is tracked as `temp_hp` on the in-memory
    participant dict, absorbing damage before real HP for ANY
    participant carrying it (not Warlock-specific) -- combat-only,
    never persisted to the DB, same convention as `raging`/`conditions`.
  - Sorcerer's Sorcerous Origin: Draconic Bloodline (automatic, no
    command, computed once at character creation): every Sorcerer
    defaults to this origin (no in-game subclass-choice mechanism
    exists, same convention as racial traits being fixed rather than
    picked) -- Draconic Resilience grants AC = 13 + DEX modifier when
    unarmored (better than the generic Wizard/Sorcerer 10+DEX formula)
    and +1 HP at level 1. Real 5E grants +1 HP per sorcerer level
    thereafter too, but hp_max is already a static value set once at
    creation for every class in this build, so this is a flat one-time
    +1 consistent with that.
  - Monk's Unarmored Defense + Martial Arts (both automatic, no
    command): AC is computed as 10 + DEX mod + WIS mod at character
    creation instead of the shared BASE_ARMOR_CLASS approximation
    (armor_class is a static stored field in this game, never
    recalculated from equipment, so this is done once, up front); every
    attack a Monk makes uses DEX instead of STR (rules/combat.py's
    resolve_attack). Deliberately did NOT drop a Monk's damage die to
    1d4 for "unarmed strikes" -- this game has no per-weapon-type
    modeling at all (every attacker shares one flat weapon dict), so
    there's no unarmed-vs-monk-weapon distinction to make, and shrinking
    just the Monk's die would make them strictly worse than every other
    martial class rather than matching the real rule's intent.
  - Ranger's Favored Enemy (automatic, no command, 2026-07-12): every
    Ranger defaults to goblinoids (fixed-default convention, same as
    Sorcerer/Warlock -- no in-game "choose a creature type" mechanism
    exists; goblin/goblin_shaman/goblin_boss are this campaign's single
    most common enemy type). Real 5E grants advantage on Survival
    checks to track/recall information about the favored enemy, which
    this game has no mechanic for at all -- adapted instead to grant
    attack-roll advantage vs. that enemy type
    (bot.py's _attack_advantage_disadvantage), the closest real
    in-combat expression available, matching how every other feature
    above was adapted to what this engine actually models.
  - Cleric's Divine Domain (automatic, no command, 2026-07-15): every
    Cleric defaults to the Life Domain (fixed-default convention, same
    as Sorcerer/Warlock/Ranger above -- no in-game domain-choice
    mechanism exists; Life fits a game where healing spells are a
    party's main sustain). Disciple of Life (spells.py's
    resolve_heal_spell) adds 2 + the spell's level whenever a Cleric
    casts a leveled (not cantrip) healing spell, the real 5E formula.

Since first written (2026-07-15/16), more real commands were added
directly in bot.py rather than back into this module: Fighter's Action
Surge (extra attack this turn, 1/rest), Barbarian's Reckless Attack
(advantage on your attack, but attacks against you also get advantage
until your next turn), Paladin's Divine Smite (spend a spell slot on a
successful hit for bonus radiant damage) and Channel Divinity (shared
Cleric/Paladin, real effect differs per class), and Monk's Flurry of
Blows (bonus-action extra unarmed strike, spends 1 ki). This docstring
undercounts real mechanical coverage as of 2026-07-19 -- see bot.py's
`_do_action_surge`/`_do_reckless_attack`/`_do_divine_smite`/
`_do_channel_divinity`/`_do_flurry_of_blows` for the current, accurate
list, not just what's enumerated above.

2026-07-21 correction: the note below claiming Druid had no real active
command was stale -- Wild Shape (bot.py's _do_wild_shape) was actually
built and shipped since this docstring was last touched: a real command,
2 uses/rest, granting real bonus damage (wild_shape_damage_bonus) and
temp HP (wild_shape_temp_hp) for the fight, live-tested end to end. It
just was never added to CLASS_FEATURES_LEVEL_1's Druid entry below, so
the character sheet never displayed it. Fixed here.

Ranger's Favored Enemy/Natural Explorer/Danger Sense remain passive-only
-- consistent with real 5E, which gives Rangers no active class feature
of their own before level 3's subcategory choice (no in-game subclass-
choice mechanism exists in this build, same fixed-default convention as
every other class above).

2026-09-04 correction: Fighter/Paladin/Ranger's "Fighting Style" was
pure flavor text below (an example list, "e.g. Defense, Dueling, Great
Weapon Fighting") with no in-game choice mechanism and zero mechanical
effect anywhere -- a real player reasonably read the example names as
things their character already had (a real dev-bridge report). Now a
real, chosen, mechanical feature for all three classes: see
FIGHTING_STYLES/FIGHTING_STYLE_CLASSES below and bot.py's
_do_choose_fighting_style. All 6 real 5E styles ship; Two-Weapon
Fighting's own real payoff needed a real dual-wielding mechanic this
game never had either (mastery-gated, see db.can_dual_wield/
equip_offhand_weapon and bot._offhand_weapon_for_attacker), built
alongside it the same day.
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
        "Jack of All Trades (level 2+): add half your proficiency bonus "
        "(rounded down) to skill checks you're not otherwise proficient in",
        "Song of Rest (level 2+): resting near you heals the whole party "
        "(including you) a bit extra",
        "Expertise (level 3+): double proficiency bonus on your two "
        "proficient skill-check abilities",
    ],
    "Cleric": [
        "Spellcasting: casts cleric spells using Wisdom",
        "Divine Domain: Life Domain — Disciple of Life adds 2 + the spell's "
        "level to any leveled healing spell you cast",
    ],
    "Druid": [
        "Spellcasting: casts druid spells using Wisdom",
        "Druidic: knows the secret Druidic language",
        "Wild Shape: bonus action, transform into a beast for the fight — "
        "bonus damage and temporary HP scaling with level, 2 uses per rest",
    ],
    "Fighter": [
        "Fighting Style: a real, chosen combat specialization — say \"I choose "
        "[style]\" (Archery, Defense, Dueling, Great Weapon Fighting, Protection, "
        "or Two-Weapon Fighting)",
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
        "Fighting Style: a real, chosen combat specialization — say \"I choose "
        "[style]\" (Archery, Defense, Dueling, Great Weapon Fighting, Protection, "
        "or Two-Weapon Fighting)",
    ],
    "Ranger": [
        "Favored Enemy: Goblinoids — advantage on attack rolls against goblins, "
        "goblin shamans, and goblin bosses",
        "Natural Explorer: expertise navigating and surviving in a chosen terrain type",
        "Fighting Style: a real, chosen combat specialization — say \"I choose "
        "[style]\" (Archery, Defense, Dueling, Great Weapon Fighting, Protection, "
        "or Two-Weapon Fighting)",
        "Danger Sense (level 2+): advantage on Dexterity saving throws",
    ],
    "Rogue": [
        "Sneak Attack: extra 1d6 damage once per turn when you have advantage "
        "or an ally is adjacent to your target",
        "Thieves' Cant: knows a secret rogue language/code",
        "Expertise: double proficiency bonus on two chosen skills",
    ],
    "Sorcerer": [
        "Spellcasting: casts sorcerer spells using Charisma",
        "Sorcerous Origin: Draconic Bloodline — Draconic Resilience grants "
        "AC = 13 + DEX modifier when unarmored and +1 HP at level 1",
        "Metamagic: Empowered Spell (level 3+) — once per rest, automatically "
        "rerolls any 1s and 2s on your next damage spell's dice",
    ],
    "Warlock": [
        "Otherworldly Patron: The Fiend — Dark One's Blessing grants "
        "temporary HP (CHA modifier + level, minimum 1) whenever you "
        "reduce a hostile creature to 0 HP",
        "Pact Magic: casts warlock spells using Charisma, spell slots recover on a short rest",
        "Eldritch Invocations (level 2+): Agonizing Blast — adds your "
        "Charisma modifier to Eldritch Blast's damage",
    ],
    "Wizard": [
        "Spellcasting: casts wizard spells using Intelligence, prepared from a spellbook",
        "Arcane Recovery: once per day, recover expended spell slots on a short rest",
    ],
}


def get_class_features(char_class: str) -> list[str]:
    return CLASS_FEATURES_LEVEL_1.get(char_class, [])


# Real Fighting Style (2026-09-04, per Coffee, dev-bridge: his Fighter
# read "Fighting Style: a combat specialization (e.g. Defense, Dueling,
# Great Weapon Fighting)" on his own sheet and reasonably expected a
# real, chosen mechanic behind it -- confirmed by grep this was pure
# flavor text with no DB field, no choice mechanic, and zero mechanical
# effect anywhere, unlike every other class feature in this game.
# Real 5E grants this to Fighter/Paladin/Ranger; all 6 real styles ship
# (including Two-Weapon Fighting, whose own real payoff needs mastery-
# gated dual wielding -- see bot._can_dual_wield). Lives here (not
# bot.py) so ai/intent_parser.py can share this exact same name list
# for its own "I choose X" collision pre-check (same reasoning
# leveling.CLASS_SUBCLASSES already gets imported there for) without a
# circular import back into bot.py.
FIGHTING_STYLE_CLASSES = {"Fighter", "Paladin", "Ranger"}
FIGHTING_STYLES = {
    "Archery": "+2 to attack rolls with ranged weapons",
    "Defense": "+1 AC while wearing armor",
    "Dueling": "+2 damage when wielding a one-handed melee weapon with no other weapon",
    "Great Weapon Fighting": "reroll any 1 or 2 on damage dice from a two-handed melee weapon (once per die)",
    "Protection": "reaction: impose disadvantage on an attack against a party member in the fight, once per round, if you have a shield equipped",
    "Two-Weapon Fighting": "add your ability modifier to your off-hand attack's damage, once you've mastered dual wielding",
}


# Task #223, per Coffee: weapon/armor proficiency, previously modeled
# nowhere at all in this game (confirmed via grep -- any class could
# equip and use any weapon/armor with zero penalty). Real 5E
# categories, simplified to fit this game's own small item catalog
# (see items.py's weapon_category/armor_category fields) rather than
# the full PHB weapon/armor lists, which include many things this game
# doesn't have. A monster/NPC with no char_class at all (every hostile
# combatant) is intentionally unaffected -- is_weapon_proficient/
# is_armor_proficient both fall back to fully permissive for an
# unrecognized class, so this only ever narrows a real player's own
# proficiency, never a monster's.
WEAPON_PROFICIENCIES = {
    "Barbarian": {"simple", "martial"},
    "Fighter": {"simple", "martial"},
    "Paladin": {"simple", "martial"},
    "Ranger": {"simple", "martial"},
    # Cleric and Rogue widened to include martial too (2026-07-21,
    # caught before shipping): this game's own STARTING_EQUIPMENT
    # (models.py) already starts a Cleric with a longsword and a Rogue
    # with a longbow -- both "martial" in this catalog's categorization
    # -- so a strict-RAW simple-only list would have immediately
    # disadvantaged every brand-new character of both classes with
    # their own starting weapon. A deliberate house departure from
    # strict 5E for this reason, not an oversight.
    "Cleric": {"simple", "martial"},
    "Rogue": {"simple", "martial"},
    "Bard": {"simple"},
    "Druid": {"simple"},
    "Monk": {"simple"},
    "Sorcerer": {"simple"},
    "Warlock": {"simple"},
    "Wizard": {"simple"},
}

ARMOR_PROFICIENCIES = {
    "Barbarian": {"light", "medium", "shield"},
    "Fighter": {"light", "medium", "heavy", "shield"},
    "Paladin": {"light", "medium", "heavy", "shield"},
    "Ranger": {"light", "medium", "shield"},
    "Cleric": {"light", "medium", "shield"},
    "Druid": {"light", "medium", "shield"},
    "Bard": {"light"},
    "Rogue": {"light"},
    "Warlock": {"light"},
    "Monk": set(),
    "Sorcerer": set(),
    "Wizard": set(),
}

_ALL_WEAPON_CATEGORIES = {"simple", "martial"}
_ALL_ARMOR_CATEGORIES = {"light", "medium", "heavy", "shield"}


def is_weapon_proficient(char_class: str | None, weapon_category: str) -> bool:
    # Real live bug (2026-08-21, Coffee, dev-bridge screenshot: "the
    # lesser spirit had a really good role and still wasn't able to
    # hit... rolled a 19 against The Wrathflame Unbound (AC 21) and
    # missed"). "natural" (rules.combat's own weapon_category for any
    # attacker whose damage comes from a raw damage_dice field --
    # _weapon_for_attacker's monster/echo/summon branch, bot.py) was
    # never a member of _ALL_WEAPON_CATEGORIES ({"simple", "martial"}
    # only), nor of any single class's own WEAPON_PROFICIENCIES set --
    # so EVERY attacker using a natural attack, not just this session's
    # new spirit summons, has been rolling to-hit with proficiency_
    # bonus silently zeroed out this whole time (confirmed live: roll
    # 19 + str_mod 0 + prof 0 = 19, short of AC 21 -- with the real
    # prof +5 this game's own data already has stored for that
    # participant, 19+0+5=24 clears it easily). Real 5E rule, not a
    # house deviation: every creature (and a wild-shaped Druid) is
    # always proficient with its own natural weapons -- this was
    # already the code's own STATED intent (see the "a monster/NPC
    # with no char_class always reads as proficient" comment at this
    # function's call site in rules/combat.py), just never actually
    # implemented for this one category.
    if weapon_category == "natural":
        return True
    return weapon_category in WEAPON_PROFICIENCIES.get(char_class, _ALL_WEAPON_CATEGORIES)


def is_armor_proficient(char_class: str | None, armor_category: str) -> bool:
    return armor_category in ARMOR_PROFICIENCIES.get(char_class, _ALL_ARMOR_CATEGORIES)
