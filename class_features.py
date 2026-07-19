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

Remaining real gap (task #91, still open): Druid and Ranger have no
real ACTIVE command yet (Druid has none at all beyond spellcasting;
Ranger's Favored Enemy/Natural Explorer/Danger Sense are all passive) --
Wild Shape specifically would need real alternate-stat-block combat
support this engine doesn't have yet, which is why it hasn't been
attempted alongside the others above. Everything else not named in
either list above is still real, accurate flavor text only.
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
        "Favored Enemy: Goblinoids — advantage on attack rolls against goblins, "
        "goblin shamans, and goblin bosses",
        "Natural Explorer: expertise navigating and surviving in a chosen terrain type",
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
