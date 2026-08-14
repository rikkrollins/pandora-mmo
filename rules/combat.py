"""
rules/combat.py
Turn-based combat resolution built on rules/dice.py. This module is
pure logic — no AI calls, no narration text. Everything here returns
structured data; the AI layer (ai/dm_agent.py) only ever narrates the
results computed here, never decides them.
"""
from rules.dice import roll_d20, roll_attack, roll_damage, ability_modifier
from rules.leveling import (
    sneak_attack_dice_count, rage_damage_bonus, wild_shape_damage_bonus, magic_penetration_pct,
    COMBAT_SUBCLASS_NAMES, COMBAT_SUBCLASS_DAMAGE_BONUS_PCT, power_scale_ratio,
)
from class_features import is_weapon_proficient
from guilds import FORGE_GUILD_WEAPON_DAMAGE_BONUS_PCT, held_guild_ids
import config
import hybrid_features
import races


def formation_ac_bonus(defender: dict) -> int:
    """
    Back-row evade bonus (2026-08-01, per Coffee: "players in the back
    row have a higher evade%"). This game's hit resolution is entirely
    AC-based, so "higher evade%" IS a flat AC bonus here -- same
    additive slot hybrid_ac_bonus already occupies, not a separate
    dodge-chance system. Front row (and anyone with no formation_row
    set at all, e.g. a solo fight or a monster template that never
    opted in) gets 0, identical to today's behavior.
    """
    return config.BACK_ROW_AC_BONUS if defender.get("formation_row") == "back" else 0

# Monsters this campaign treats as undead for the Silver Wardens guild's
# bonus_damage_vs_undead benefit (guilds.py) -- no monster template field
# for creature type exists in this game, so, same convention as Ranger's
# Favored Enemy matching on monster_key prefix, this is a fixed set of
# monster_keys rather than a new schema field.
#
# Expanded 2026-07-27 (per Coffee, follow-up to the 2026-07-26 damage-
# type pass): Shadow Wisp was the only monster ever in this set, making
# Silver Wardens' signature guild perk relevant against exactly one of
# 56 monsters game-wide. The Hollow Verge's own wraith/bone/cairn line
# (verge_wraith and its bound variant, The Verge Warden who leads them,
# bone_legionnaire and its elder variant, cairn_watcher and its young
# variant) are unambiguously undead by name/theme (wraith, bone,
# burial cairn) -- added here, grounded in that naming, not just "has
# necrotic resistance" (several non-undead monsters elsewhere also
# resist necrotic without being undead, e.g. The Unbegun's cosmic/
# paradox theme).
UNDEAD_MONSTER_KEYS = {
    "shadow_wisp",
    "verge_wraith", "bound_verge_wraith", "the_verge_warden",
    "bone_legionnaire", "elder_bone_legionnaire",
    "cairn_watcher", "young_cairn_watcher",
}

# Boss Enrage (2026-07-27, per Coffee: "I want the battles to be
# difficult with interesting mechanics"). A real playthrough simulation
# right after the 2026-07-26 monster rebalance confirmed even dungeon
# bosses were dying in 1 round for a sliver of the player's HP -- that
# pass deliberately preserved the ORIGINAL relative difficulty (which
# was already soft) onto the new absolute scale, rather than making
# anything genuinely dangerous. A boss crossing this HP threshold once
# permanently gains bonus damage for the rest of that fight -- a real,
# one-time, in-memory-only state flip (never reverts, never re-fires),
# giving every boss fight a real "the fight gets harder as it goes"
# second phase instead of a flat difficulty the whole way through.
ENRAGE_HP_THRESHOLD = 0.3
ENRAGE_DAMAGE_BONUS_PCT = 50

# Bloodied beat (2026-08-01, task #244, from this session's own COMPLETE
# GAME playthrough QoL audit: long fights had no mid-fight story beat at
# all, just a flat stream of attack rolls). Purely narrational -- unlike
# Enrage, crossing this threshold changes nothing mechanically, on
# either side of the fight, just gives it a real, one-time, grounded-in-
# actual-HP "the tide is turning" moment. Deliberately a flat 50%
# (distinct from ENRAGE_HP_THRESHOLD, which is lower and boss-only).
BLOODIED_HP_THRESHOLD = 0.5

# Real time pressure (2026-07-27, per Coffee: "use time mechanics in
# battles too with the environments and bosses") -- a boss fight that
# drags on past ENRAGE_ROUND_THRESHOLD rounds enrages automatically,
# even if the boss is still above the HP-based threshold above, same
# bonus, same one-time flip. ENRAGE_WARNING_ROUND gives a real, fair
# heads-up a few rounds ahead (checked in bot.py's _resolve_ai_turns),
# so this reads as "don't stall too long" pressure, never an
# unannounced gotcha.
ENRAGE_WARNING_ROUND = 8
ENRAGE_ROUND_THRESHOLD = 12


def _defender_resistance_profile(defender: dict) -> tuple[set, set, set]:
    """
    Combines a defender's racial damage resistances (real players/
    companions -- races.py's damage_resistances field, Dwarf/poison,
    Dragonborn+Tiefling/fire) with whatever a monster template already
    set directly on the participant dict (resistances/vulnerabilities/
    immunities lists, campaigns/default/campaign.json). Either side can
    be empty/absent; this never fails on a participant with neither.
    """
    resistances = set(defender.get("resistances", []))
    vulnerabilities = set(defender.get("vulnerabilities", []))
    immunities = set(defender.get("immunities", []))
    race = defender.get("race")
    if race:
        resistances |= set(races.racial_damage_resistances(race))
    return resistances, vulnerabilities, immunities


def apply_damage_type_modifier(damage: int, damage_type: str | None, defender: dict, attacker: dict | None = None) -> int:
    """
    Real damage-type resistance/vulnerability/immunity math (damage-type
    system, 2026-07-24): immune -> 0, resistant -> half, vulnerable ->
    double, and resistance+vulnerability on the same type cancel back to
    normal (real 5E stacking rule). damage_type defaults to "physical"
    everywhere it's read (items.py/spells.py/DEFAULT_WEAPON), so any
    weapon/spell/monster with no explicit type behaves exactly as it
    always did before this system existed.

    Magic penetration (rules.leveling.magic_penetration_pct, earned
    per rebirth -- per Coffee: "work in magic bonuses to counter act
    the magic resistences... use the evolutions for that") closes the
    gap a resistance leaves, proportionally: at 100% penetration a
    resistant hit deals full damage again; at 0% it's still halved.
    Never grants MORE than normal damage -- this counters resistance,
    it doesn't create a new vulnerability exploit. Immunity stays a
    real, absolute wall (untouched by penetration); vulnerability
    already favors the attacker and has nothing to counter.

    Stacking elemental resistance (2026-08-10, per Coffee: "allow the
    player to enchant armour and other equipables to raise resistences
    in Frost/Flame/Spark... if the players gain enough resistences...
    it shud nullify the damage" -- and, for enemies, "if enemies are
    strong in an element, it shud nullify the damage"): defender.
    elemental_resistance_pct is a NEW, separate, numeric, STACKING layer
    (0-100+ percent per damage type) -- for players it's live-summed
    from every equipped enchanted item's real elemental_resistance
    affix (see bot.py's _apply_equipped_elemental_profile); for
    monsters it's a real, hand-set field in campaign.json for creatures
    "strong in an element". Deliberately kept separate from the old
    boolean resistances list above rather than replacing it -- when
    present for a damage type, it wins outright (a numeric, explicitly-
    stacked resistance is always at least as strong as the flat 50%
    boolean case, so there's no real ambiguity to resolve). At exactly
    100% this nullifies the hit completely (still returned as a plain
    0, same as immunity/a fully-absorbed hit -- see this function's own
    "never returns negative" contract, kept deliberately intact so
    every call site that multiplies/gates/halves damage_dealt further
    afterward never has to guard against a sign it was never written to
    expect). Anything ABOVE 100% is NOT reflected in this return value
    at all -- see the separate elemental_overflow_heal() below for the
    "in extreme cases heal" half of the same feature, called
    independently by callers right after this function, exactly so
    that overflow can never leak into this one's always-non-negative
    contract.
    """
    if damage <= 0 or not damage_type:
        return damage
    resistances, vulnerabilities, immunities = _defender_resistance_profile(defender)
    if damage_type in immunities:
        return 0
    # Mythic-tier weapon affix (magic item system Phase 6, 2026-08-02):
    # ignore_resistance is a real, guaranteed mechanical effect that cuts
    # through BOTH the old boolean resistance and this new stacking
    # elemental layer alike -- checked once, up front, rather than
    # duplicated in each branch below.
    ignores = bool(attacker and attacker.get("ignores_resistance"))
    elemental_pct = 0.0 if ignores else float((defender.get("elemental_resistance_pct") or {}).get(damage_type, 0))
    if elemental_pct > 0:
        penetration = magic_penetration_pct(attacker.get("rebirth_count", 0) if attacker else 0) / 100.0
        effective_pct = min(elemental_pct * (1 - penetration), 100.0)
        return max(0, int(round(damage * (1 - effective_pct / 100))))
    resistant = damage_type in resistances
    vulnerable = damage_type in vulnerabilities
    if resistant and vulnerable:
        return damage
    if resistant and ignores:
        return damage
    if resistant:
        halved = damage // 2
        cut_off = damage - halved
        penetration = magic_penetration_pct(attacker.get("rebirth_count", 0) if attacker else 0) / 100.0
        recovered = int(round(cut_off * penetration))
        return min(damage, halved + recovered)
    if vulnerable:
        return damage * 2
    return damage


def elemental_overflow_heal(raw_damage: int, damage_type: str | None, defender: dict, attacker: dict | None = None) -> int:
    """
    The "in extreme cases heal" half of the elemental-resistance feature
    above (2026-08-10, per Coffee) -- called SEPARATELY by callers right
    after apply_damage_type_modifier, on the same raw pre-modifier
    damage value, never folded into that function's own return so its
    "always non-negative" contract stays intact for every existing call
    site. Zero whenever elemental_resistance_pct (after magic
    penetration) doesn't clear 100% -- i.e. this only ever fires on top
    of an already-fully-nullified hit, never as a separate/smaller
    effect. The amount healed scales with how far past 100% the
    (post-penetration) resistance goes, applied to the SAME raw_damage
    apply_damage_type_modifier was given, so a bigger hit against a
    heavily-elemental-resistant defender means a bigger heal, not a
    fixed number.
    """
    if raw_damage <= 0 or not damage_type:
        return 0
    if attacker and attacker.get("ignores_resistance"):
        return 0
    _, _, immunities = _defender_resistance_profile(defender)
    if damage_type in immunities:
        return 0
    elemental_pct = float((defender.get("elemental_resistance_pct") or {}).get(damage_type, 0))
    if elemental_pct <= 100:
        return 0
    penetration = magic_penetration_pct(attacker.get("rebirth_count", 0) if attacker else 0) / 100.0
    effective_pct = elemental_pct * (1 - penetration)
    if effective_pct <= 100:
        return 0
    return int(round(raw_damage * (effective_pct - 100) / 100))


# Synergy Phase 6 (2026-08-13, per Coffee: "signature mechanics for the
# bosses", extending the Phase 4/5b reactivity theme): a boss's
# "adapts_to_damage" flag (campaign.json, currently only set on
# the_unasked -- the true final boss -- but generic so any future boss
# could opt in the same way on_hit_condition/life_drain already do)
# means it genuinely learns from what's hitting it mid-fight, real and
# permanent for the rest of that combat, not a fixed pre-authored
# resistance. Reuses elemental_resistance_pct -- the EXACT same
# stacking layer already built for enchanted armor ("if enemies are
# strong in an element, it shud nullify the damage") -- rather than
# inventing a second resistance system, so a high-rebirth party's own
# earned magic_penetration_pct still counters it exactly like any
# other elemental resistance. Capped well below 100% (never fully
# immune to any one element) so hammering one damage type just makes
# that element progressively weaker, forcing real diversification
# instead of a hard wall.
ADAPTIVE_RESISTANCE_GROWTH_PCT_PER_HIT = 5
ADAPTIVE_RESISTANCE_MAX_PCT = 60


def _maybe_grow_adaptive_resistance(defender: dict, damage_type: str | None, damage_dealt: int) -> None:
    """Grows `defender`'s elemental_resistance_pct for `damage_type` on a real, damage-dealing hit -- see the constants' own docstring above."""
    if not defender.get("adapts_to_damage") or damage_dealt <= 0 or not damage_type:
        return
    profile = defender.setdefault("elemental_resistance_pct", {})
    profile[damage_type] = min(
        float(profile.get(damage_type, 0)) + ADAPTIVE_RESISTANCE_GROWTH_PCT_PER_HIT, ADAPTIVE_RESISTANCE_MAX_PCT,
    )


def start_combat(participants: list[dict]) -> list[dict]:
    """
    Roll initiative (d20 + DEX modifier) for each participant.
    Each participant dict must have a 'name' and 'dexterity' key.
    Returns the participants sorted by initiative, descending, each
    annotated with their rolled 'initiative' value.
    """
    for p in participants:
        dex_mod = ability_modifier(p["dexterity"])
        p["initiative"] = roll_d20() + dex_mod
    return sorted(participants, key=lambda p: p["initiative"], reverse=True)


def resolve_attack(attacker: dict, defender: dict, weapon: dict,
                    advantage: bool = False, disadvantage: bool = False,
                    defender_relentless_endurance_available: bool = False,
                    round_number: int = 0, forced_roll: int | None = None,
                    forced_damage_roll: int | None = None, damage_multiplier: int = 1) -> dict:
    """
    Resolve one attack. `weapon` is a dict like:
        {"ability": "strength", "damage_dice": "1d8", "damage_bonus": 0}
    Applies damage directly to defender['hp_current'] and returns a
    structured result — not narration text. `advantage`/`disadvantage`
    let callers apply real mechanical effects from conditions (e.g. a
    prone attacker has disadvantage; attacking a prone target has
    advantage) — if both are True they correctly cancel out (5E rule,
    already handled inside roll_attack/roll_d20).

    Two real Half-Orc racial traits are handled here (both check
    attacker/defender['race'], so every other race is completely
    unaffected): Savage Attacks (an extra weapon damage die on a melee
    crit, real 5E wording — distinct from the normal crit doubling) is
    pure logic, resolved unconditionally. Relentless Endurance (drop to
    1 HP instead of 0, once per long rest) needs real "already used
    this rest" state that only the DB-backed caller (bot.py) knows —
    this module has no I/O by design, so the caller passes whether it's
    currently AVAILABLE via `defender_relentless_endurance_available`,
    and this function reports back whether it actually triggered
    (`relentless_endurance_triggered`) so the caller can persist that
    the use was spent, same rules-decide/bot.py-persists split used
    everywhere else in this game.

    Two more real class features are also handled here, purely from
    flags already present on the participant dicts (bot.py sets these,
    see _do_rage): a Rogue's Sneak Attack (+1d6 damage, once per turn —
    automatically satisfied since this system already gives each
    participant only one attack per turn — when attacking with
    advantage; real 5E's other trigger, "an ally within 5 ft. of the
    target," has no equivalent here since this combat has no
    positioning/adjacency system at all) and a raging Barbarian's bonus
    damage plus resistance to all incoming damage while raging
    (simplified from the real three specific physical damage types,
    since this system doesn't model damage types at all).

    A Druid's Wild Shape (task #91 audit, 2026-07-19: Druid had ZERO
    unique mechanical features before this) is handled the same way as
    Rage -- a `wild_shaped` flag (see bot.py's _do_wild_shape) grants
    bonus claw/bite damage via wild_shape_damage_bonus, since this
    engine has no separate-statblock system to actually swap a Druid's
    attacks for a beast's (same simplification Rage already uses rather
    than modeling a Barbarian's three specific resisted damage types).

    A Monk's Martial Arts is handled here too: real 5E lets a Monk use
    Dexterity instead of Strength for attacks with a monk weapon or an
    unarmed strike -- applied unconditionally for any Monk attacker,
    since this game has no per-weapon-type modeling (every attacker
    already shares one flat `weapon` dict) so there's no "monk weapon
    vs. not" distinction to make. Deliberately NOT touching the damage
    die: real Martial Arts drops an unarmed strike to 1d4, but every
    class here already shares the same flat damage_dice regardless of
    weapon, so shrinking just the Monk's die would make them strictly
    worse than every other martial class instead of matching the real
    rule's intent (a Monk fighting with an actual weapon, which this
    game's starting equipment gives them, keeps the normal die).

    A Warlock's Otherworldly Patron (The Fiend, the default patron for
    every Warlock -- no in-game subclass-choice mechanism exists, same
    convention as Sorcerer's Draconic Bloodline) grants Dark One's
    Blessing here: reducing a hostile creature to 0 HP grants temp HP
    equal to CHA modifier + level (minimum 1, real 5E formula; doesn't
    stack with itself, takes the higher value). Temp HP is tracked as
    `temp_hp` on the participant dict -- combat-only, in-memory-only,
    same convention as `raging`/`conditions` (reset when combat ends,
    never persisted to the DB) -- and absorbs damage before real HP for
    ANY participant carrying it, not just Warlocks, matching real 5E's
    general temp-HP rule.

    Two real reactions (2026-07-15) are handled here too -- CLAUDE.md's
    Known Limitations flagged reactions as needing resolve_attack's
    roll-then-apply-damage step to actually have a checkpoint in
    between, rather than a bolt-on; that checkpoint already exists
    naturally (attack_result is fully known before any damage is
    rolled or applied), so both plug in there. This engine has no
    real-time "declare a reaction" prompt, so both are auto-triggered
    exactly when they'd actually change the outcome, spending a real
    resource, never wasted on a roll they couldn't have affected:
    - Shield (Wizard/Sorcerer, real spell + real spell slot): if the
      hit isn't a critical and the attack total is below
      defender's AC + 5, casting Shield retroactively turns it into a
      miss -- the same "ac_bonus" effect Shield's spells.py entry has
      always had, just never read anywhere before now (same silent-
      dead-effect shape as this session's potions/equipment fixes).
    - Uncanny Dodge (real 5E Rogue feature, level 5+): halves the
      damage from a confirmed hit.
    Both cost the defender their one reaction for the round (`round_
    number`, tracked via `reaction_used_round` on the participant dict,
    the same combat-only in-memory convention as `raging`/`conditions`
    -- resets naturally once round_number advances past it) so a
    defender can't Shield AND Uncanny-Dodge the same hit, matching real
    5E's one-reaction-per-round economy.

    `damage_multiplier` (Assassin's Backstab, 2026-08-08): a flat
    integer multiplier applied to the FULLY-resolved damage (every
    bonus above already folded in) right before it's applied to HP --
    deliberately the very last step, so a x2/x4/x8/x10 backstab still
    correctly triggers Relentless Endurance/Death Ward/temp-HP
    absorption on the true final number, rather than those checks
    running against a pre-multiplier amount and getting it wrong.
    """
    attack_ability = "dexterity" if attacker.get("char_class") == "Monk" else weapon.get("ability", "strength")
    # Task #223: real weapon proficiency -- a monster/NPC with no
    # char_class at all always reads as proficient (is_weapon_proficient's
    # own permissive fallback), so this only ever narrows a real
    # player's own attack, never a monster's.
    weapon_category = weapon.get("weapon_category", "simple")
    # Universal Manipulation "Weapon Mastery" (2026-08-06, per Coffee:
    # "make sure they can choose proficiencies in weapons and armours
    # too - make all items available to do so"): a real, purchased
    # bonus proficiency (bot.py's UNIVERSAL_MANIPULATION_PROFICIENCIES,
    # id "prof_<category>_weapons") widens is_weapon_proficient's
    # class-based check, never replaces it -- inlined as a plain string
    # check rather than an import to avoid a circular import (bot.py
    # already imports this module, not the other way around).
    weapon_proficient = (
        is_weapon_proficient(attacker.get("char_class"), weapon_category)
        or f"prof_{weapon_category}_weapons" in (attacker.get("skill_tree_upgrades") or [])
    )
    # Hybrid classes (2026-07-22): a Monk/Sorcerer hybrid's AC bump is
    # computed live here rather than stored on the character -- see
    # hybrid_features.hybrid_ac_bonus's own docstring for why (freely
    # switching hybrid flavors can never leave a stale bonus behind).
    # Shield (real 5E spell, 2026-08-04): +5 AC, computed live here (same
    # "checked at attack time, never mutated on the character" style as
    # hybrid_ac_bonus/formation_ac_bonus just above) rather than
    # permanently bumping armor_class -- the "shield_active" condition
    # expires on its own via Session._expire_timed_conditions, so there's
    # no separate AC-reversal code needed either.
    shield_bonus = 5 if "shield_active" in defender.get("conditions", []) else 0
    effective_defender_ac = (
        defender["armor_class"] + hybrid_features.hybrid_ac_bonus(defender) + formation_ac_bonus(defender) + shield_bonus
    )
    attack_result = roll_attack(
        attacker,
        target_ac=effective_defender_ac,
        ability=attack_ability,
        proficient=weapon_proficient,
        advantage=advantage,
        disadvantage=disadvantage,
        forced_roll=forced_roll,
    )

    # Bless (real 5E spell, 2026-08-04): a real, flat +2 to attack rolls
    # for the caster's whole blessed party -- roll_attack has no numeric
    # to-hit-bonus parameter of its own, so this is applied as a
    # post-roll adjustment, same "recheck hit off the adjusted total"
    # shape a forced physical-dice roll already uses elsewhere. Doesn't
    # touch a natural 1/20 (crit/fumble are about the raw die, not the
    # total), matching real 5E where Bless can't turn a fumble into a
    # hit or a crit into a miss.
    if "blessed" in attacker.get("conditions", []) and not attack_result["critical_fail"] and not attack_result["critical_hit"]:
        attack_result["total"] += 2
        attack_result["hit"] = attack_result["total"] >= effective_defender_ac

    shield_reaction_triggered = False
    reaction_available = defender.get("reaction_used_round") != round_number
    if (attack_result["hit"] and not attack_result["critical_hit"] and reaction_available
            and "shield" in (defender.get("known_spells") or [])
            and defender.get("spell_slots_current", 0) > 0
            and attack_result["total"] < effective_defender_ac + 5):
        defender["spell_slots_current"] -= 1
        defender["reaction_used_round"] = round_number
        attack_result["hit"] = False
        shield_reaction_triggered = True
        reaction_available = False

    damage_dealt = 0
    relentless_endurance_triggered = False
    death_ward_triggered = False
    dark_ones_blessing_gained = 0
    uncanny_dodge_triggered = False
    hybrid_bonus_gained = 0
    # Synergy Phase 9 (2026-08-14): The Undertone's echoes_damage_type
    # signature mechanic -- see the hit-count tracking and trigger check
    # further down this function for the real logic. Initialized here so
    # a miss (which skips the whole `if attack_result["hit"]:` block
    # below) still returns a real 0, never an undefined value.
    echo_backlash_damage = 0
    hybrid_temp_hp_gained = 0
    hybrid_self_heal_gained = 0
    enrage_triggered = False
    bloodied_triggered = False
    elemental_heal_gained = 0
    if attack_result["hit"]:
        savage_attacks_die = 1 if (attacker.get("race") == "Half-Orc" and attack_result["critical_hit"]) else 0
        # Synergy Phase 7 (2026-08-13, per Coffee: "subclass-specific boss
        # counters", naming Sneak Attack specifically): a boss's real
        # "counters_sneak_attack" flag (campaign.json, currently only set
        # on the_verge_warden -- a Warden's whole point is vigilance)
        # means it can't be caught off guard the same way twice in one
        # fight. The FIRST advantage-based Rogue hit still lands its full
        # Sneak Attack die (it hasn't learned the pattern yet); this also
        # flags the defender so every hit AFTER that one has it negated,
        # for the rest of THIS fight only (a plain runtime dict key, never
        # copied from the template, so it can't leak into the next fight).
        sneak_attack_die = 1 if (
            attacker.get("char_class") == "Rogue" and advantage and not defender.get("_sneak_attack_countered")
        ) else 0
        if attacker.get("char_class") == "Rogue" and advantage and defender.get("counters_sneak_attack"):
            defender["_sneak_attack_countered"] = True
        rage_bonus = rage_damage_bonus(attacker.get("level", 1)) if attacker.get("raging") else 0
        # Synergy Phase 9 counters_rage (2026-08-14, The Waking Ember --
        # a boss that "burns through" a Barbarian's fury): same real
        # "first hit still lands it, every hit after doesn't" shape as
        # counters_sneak_attack just above.
        if rage_bonus and defender.get("counters_rage"):
            if defender.get("_rage_countered"):
                rage_bonus = 0
            else:
                defender["_rage_countered"] = True
        wild_shape_bonus = (
            wild_shape_damage_bonus(attacker.get("level", 1)) if attacker.get("wild_shaped") else 0
        )
        # Synergy Phase 10 counters_wild_shape (2026-08-14, The Root That
        # Remembers -- a root older than the forest, unfooled by a
        # borrowed animal shape): same real "first hit still lands it,
        # every hit after doesn't" shape as counters_rage just above.
        if wild_shape_bonus and defender.get("counters_wild_shape"):
            if defender.get("_wild_shape_countered"):
                wild_shape_bonus = 0
            else:
                defender["_wild_shape_countered"] = True
        # Silver Wardens guild benefit (bonus_damage_vs_undead, guilds.py):
        # +2 damage against this campaign's undead-flavored monsters.
        # Real live bug (2026-08-13, synergy pass, per Coffee: "make sure
        # classes, sub-classes, and guilds all work in synergy"): this
        # checked attacker["guild"] (the PRIMARY guild) only, so a
        # Promotion-earned SECONDARY guild membership granted zero
        # combat benefit -- the exact same bug class already fixed in
        # recipe_requirement_gate (v1.27.174). held_guild_ids (primary
        # first, then every secondary) is the real fix used everywhere
        # else this session touched guild membership checks.
        warden_bonus = (
            2 if ("silver_wardens" in held_guild_ids(attacker)
                  and defender.get("monster_key") in UNDEAD_MONSTER_KEYS)
            else 0
        )
        # Hybrid classes (2026-07-22): a real, chance-gated, scaled-down
        # taste of a second class's damage-flavored feature -- see
        # hybrid_features.py's own docstring for the shared design.
        hybrid_bonus_gained = hybrid_features.hybrid_damage_bonus(attacker)
        # Task #143: a physical-dice-mode player's own reported damage
        # roll (see roll_damage's forced_roll docstring) only ever
        # substitutes into THIS main weapon die -- Sneak Attack's
        # separate 1d6 below is its own physical die a player would
        # roll separately, out of scope here same as Extra Attack's
        # later swings are for the attack-roll forced_roll above.
        dmg = roll_damage(
            weapon["damage_dice"],
            modifier=weapon.get("damage_bonus", 0) + rage_bonus + wild_shape_bonus + warden_bonus + hybrid_bonus_gained,
            critical=attack_result["critical_hit"],
            extra_dice=savage_attacks_die,
            forced_roll=forced_damage_roll,
        )
        damage_dealt = max(dmg["total"], 0)
        # Command's "Drop" word (real 5E spell, 2026-08-04): the target's
        # weapon clatters away for a round -- reduced here to a heavily
        # weakened, effectively-unarmed swing rather than tracking a
        # separate "no weapon equipped" combat path just for this.
        if "disarmed" in attacker.get("conditions", []):
            damage_dealt = damage_dealt // 4
        if sneak_attack_die:
            # Real 5E scales Sneak Attack's die count with Rogue level
            # (1d6 at 1-2, up to 10d6 at 19-20) -- found frozen at a flat
            # 1d6 regardless of level (2026-07-16 audit).
            extra_sneak_dice = sneak_attack_dice_count(attacker.get("level", 1)) - 1
            # Universal Manipulation "killers_instinct" (2026-08-06,
            # repeatable): +1 extra Sneak Attack die per point invested,
            # unlimited -- points invested = how many times this id
            # appears in skill_tree_upgrades (see bot.py's _skill_points).
            extra_sneak_dice += (attacker.get("skill_tree_upgrades") or []).count("killers_instinct")
            sneak_dmg = roll_damage("1d6", critical=attack_result["critical_hit"], extra_dice=extra_sneak_dice)
            damage_dealt += sneak_dmg["total"]
        # Hex / Hunter's Mark (real 5E spells, 2026-08-04): both grant
        # extra damage specifically "when you hit the marked creature
        # with a weapon attack" -- real 5E text for both, so this is
        # scoped to weapon attacks only (not spell damage), matching the
        # actual spells rather than a made-up broader bonus. Hunter's
        # Mark's die scales with slot level in real 5E; simplified here
        # to a flat 1d6, same "one die, no slot-level scaling" style
        # this engine already uses for Sneak Attack's base case.
        marked_target_id = attacker.get("marked_target_id")
        if marked_target_id is not None and marked_target_id == defender.get("telegram_user_id"):
            mark_dmg = roll_damage("1d6", critical=attack_result["critical_hit"])
            mark_bonus = mark_dmg["total"]
            # Synergy Phase 9 resists_dot_stacking (2026-08-14, The
            # Drowned Choir -- "it doesn't sing so much as it keeps
            # singing": a wound that's already marked doesn't cut deeper
            # for a second mark). Halves the Hex/Hunter's Mark bonus die
            # specifically, every hit, not just after the first -- this
            # boss is simply resistant to the mark itself, not "learning"
            # a pattern the way counters_rage/counters_sneak_attack do.
            if defender.get("resists_dot_stacking"):
                mark_bonus = mark_bonus // 2
            damage_dealt += mark_bonus
        pre_elemental_damage = damage_dealt
        weapon_damage_type = weapon.get("damage_type", "physical")
        damage_dealt = apply_damage_type_modifier(
            damage_dealt, weapon_damage_type, defender, attacker
        )
        elemental_heal_gained = elemental_overflow_heal(
            pre_elemental_damage, weapon_damage_type, defender, attacker
        )
        _maybe_grow_adaptive_resistance(defender, weapon_damage_type, damage_dealt)
        # Synergy Phase 9 echoes_damage_type (2026-08-14, The Undertone --
        # "something is still, impossibly, humming"): tracks real hits by
        # damage type across the fight; the 3rd hit of any one type
        # triggers a one-time echo, computed once the final damage_dealt
        # is known (see the trigger check further down, after every other
        # bonus/multiplier has been applied) -- themed as the location
        # itself echoing that exact wound back at whoever caused it.
        if defender.get("echoes_damage_type"):
            hit_counts = defender.setdefault("_damage_type_hit_counts", {})
            hit_counts[weapon_damage_type] = hit_counts.get(weapon_damage_type, 0) + 1
        # Real subclass choice, non-Wizard classes (2026-07-25): a
        # character whose chosen subclass is one of the "combat" picks
        # (rules/leveling.CLASS_SUBCLASSES) deals more weapon damage.
        if attacker.get("subclass") in COMBAT_SUBCLASS_NAMES:
            damage_dealt = int(damage_dealt * (1 + COMBAT_SUBCLASS_DAMAGE_BONUS_PCT / 100))
        # Forge Guild membership benefit (2026-07-25, per Coffee: "a
        # guild for forging (with % enhancements stats"): a real +10%
        # weapon damage bonus -- a smith trusts their own hammer and
        # steel more than any spell, same multiplicative stacking as
        # the subclass bonus just above. held_guild_ids, not primary-
        # only -- see warden_bonus's comment above for why.
        if "forge_guild" in held_guild_ids(attacker):
            pre_forge_bonus_damage = damage_dealt
            damage_dealt = int(damage_dealt * (1 + FORGE_GUILD_WEAPON_DAMAGE_BONUS_PCT / 100))
            # Synergy Phase 9 resists_forge_guild (2026-08-14, The
            # Unbegun -- old enough to predate any guild-taught
            # technique): negates half of the FORGE GUILD BONUS
            # SPECIFICALLY (not the base hit) on every hit after the
            # first -- deliberately checked here, right after the bonus
            # is added, not folded into apply_damage_type_modifier above
            # (which runs BEFORE this bonus even exists yet -- checking
            # there would resist damage that doesn't have the Forge Guild
            # bonus in it, a real ordering trap a full stacking-order
            # audit this same day flagged).
            if defender.get("resists_forge_guild"):
                forge_bonus_amount = damage_dealt - pre_forge_bonus_damage
                if defender.get("_forge_guild_resisted"):
                    damage_dealt -= forge_bonus_amount // 2
                else:
                    defender["_forge_guild_resisted"] = True
        # Synergy Phase 10 punishes_silenced_targets (2026-08-14, The
        # Unspoken -- a real combo with its own on_hit_condition:
        # "silenced": this boss silences whoever it hits, then hits
        # harder against anyone still carrying that silence, "the cavern
        # describes YOU back"). Every hit, not just the first -- this is
        # a standing vulnerability while silenced, not a one-time "learn
        # the pattern" counter like counters_rage/counters_wild_shape.
        if attacker.get("punishes_silenced_targets") and "silenced" in (defender.get("conditions") or []):
            damage_dealt = int(damage_dealt * 1.25)
        # Real player-power rebalance (2026-07-26, per Coffee: "rebalance
        # everything... all skills and abilities and magic and spells and
        # cantrips"): monster HP/damage were already rescaled to match
        # EVOLUTION_HP_MULTIPLIER, but a real character's own weapon
        # damage was never touched -- every weapon's damage_bonus tops
        # out at +3 (items.py's rarest legendary reward) game-wide, so
        # by late-game/rebirth tiers gear progression had gone
        # completely flat relative to monster HP. Scales a REAL
        # character's own weapon damage by the same ratio methodology
        # already used for monsters -- gated on char_class so a monster/
        # NPC attacker (no char_class field at all) is never touched,
        # since monsters already carry their own pre-scaled damage_dice/
        # damage_bonus directly in campaign.json.
        if attacker.get("char_class"):
            damage_dealt = int(damage_dealt * power_scale_ratio(attacker.get("level", 1), attacker.get("rebirth_count", 0)))
        # Boss Enrage bonus (2026-07-27) -- see ENRAGE_HP_THRESHOLD's own
        # docstring above. Applies once the ATTACKING boss has already
        # crossed its own enrage threshold on a previous hit taken.
        if attacker.get("enraged"):
            damage_dealt = int(damage_dealt * (1 + ENRAGE_DAMAGE_BONUS_PCT / 100))
        if defender.get("raging"):
            damage_dealt = damage_dealt // 2
        # Hybrid Barbarian (2026-07-22): a chance-gated, scaled-down
        # taste of Rage's damage resistance -- deliberately independent
        # of a REAL raging Barbarian's own unconditional halving above
        # (both can't ever apply to the same defender, since a hybrid
        # pick can never equal your own real char_class).
        if hybrid_features.hybrid_damage_resistance(defender):
            damage_dealt = damage_dealt // 2
        # Hybrid Druid (2026-07-22): Wild-Shape-flavored temp HP, rolled
        # alongside its own bonus damage above.
        hybrid_temp_hp_gained = hybrid_features.hybrid_temp_hp_on_damage(attacker)
        if hybrid_temp_hp_gained > attacker.get("temp_hp", 0):
            attacker["temp_hp"] = hybrid_temp_hp_gained
        # Hybrid Fighter/Paladin (2026-07-22): a chance-gated self-heal
        # on landing a hit.
        hybrid_self_heal_gained = hybrid_features.hybrid_self_heal_on_hit(attacker)
        if hybrid_self_heal_gained > 0:
            attacker_hp_max = attacker.get("hp_max", attacker.get("hp_current", 0))
            attacker["hp_current"] = min(attacker.get("hp_current", 0) + hybrid_self_heal_gained, attacker_hp_max)

        # Uncanny Dodge (real 5E Rogue feature, level 5+): halves damage
        # from a confirmed hit, once per round -- shares the same
        # reaction economy as Shield above (reaction_available already
        # reflects whether Shield used it first this round).
        if (reaction_available and defender.get("char_class") == "Rogue"
                and defender.get("level", 1) >= 5 and damage_dealt > 0):
            damage_dealt = damage_dealt // 2
            defender["reaction_used_round"] = round_number
            uncanny_dodge_triggered = True

        # A Warlock's Otherworldly Patron (The Fiend, the default patron
        # for every Warlock here -- see resolve_attack's Martial Arts
        # comment above for why this game gives fixed defaults rather
        # than a half-built subclass-choice system) grants temporary HP
        # that absorbs damage before real HP, same combat-only,
        # in-memory-only convention as `raging`/`conditions` (reset when
        # combat ends, never persisted to the DB).
        if damage_multiplier != 1:
            effective_multiplier = damage_multiplier
            # Synergy Phase 9 counters_backstab (2026-08-14, The
            # Cairnbound -- an ambush-flavored Remnant boss countering an
            # Assassin's own ambush trick): same "first hit still lands
            # it, every hit after is halved" shape as counters_rage --
            # halves the MULTIPLIER itself (not the base damage), so a
            # x10 late-game Backstab still lands x5, a real reduction
            # without trivializing the mechanic outright.
            if defender.get("counters_backstab"):
                if defender.get("_backstab_countered"):
                    effective_multiplier = damage_multiplier / 2
                else:
                    defender["_backstab_countered"] = True
            damage_dealt = int(damage_dealt * effective_multiplier)

        # Synergy Phase 10 reduces_first_hit_damage (2026-08-14, The
        # Waiting Shape -- "something has been waiting a very long time,"
        # slow to actually wake up): the very first confirmed hit landed
        # against it each fight is halved; every hit after that is
        # completely unaffected.
        if defender.get("reduces_first_hit_damage") and not defender.get("_first_hit_landed"):
            defender["_first_hit_landed"] = True
            damage_dealt = damage_dealt // 2

        # Synergy Phase 10 punishes_repeat_attacker (2026-08-14, The
        # Unrepeating -- "walls stopped bothering to explain itself,"
        # never doing the same thing twice): if the SAME attacker lands
        # two hits on it back to back (no other attacker in between),
        # every consecutive hit after the first from that one attacker is
        # halved -- rewards the party rotating who actually swings,
        # rather than one character just repeating the same attack.
        if defender.get("punishes_repeat_attacker"):
            attacker_id = attacker.get("telegram_user_id")
            if defender.get("_last_attacker_id") == attacker_id:
                damage_dealt = damage_dealt // 2
            defender["_last_attacker_id"] = attacker_id

        # Synergy Phase 9 echoes_damage_type trigger (2026-08-14): fires
        # exactly once per fight, the moment a 3rd real hit of the same
        # damage type lands -- damage_dealt here already reflects every
        # bonus/multiplier above (subclass %, guild %, power-scale,
        # enrage, backstab, ...), so the echo mirrors the REAL final
        # damage just dealt, not some earlier intermediate value.
        if (defender.get("echoes_damage_type") and not defender.get("_echo_triggered")
                and defender.get("_damage_type_hit_counts", {}).get(weapon_damage_type, 0) >= 3):
            defender["_echo_triggered"] = True
            echo_backlash_damage = damage_dealt

        temp_hp = defender.get("temp_hp", 0)
        if temp_hp > 0:
            absorbed = min(temp_hp, damage_dealt)
            defender["temp_hp"] = temp_hp - absorbed
            damage_dealt -= absorbed

        hp_before = defender["hp_current"]
        hp_after = max(hp_before - damage_dealt, 0)
        if (hp_after == 0 and hp_before > 0 and defender.get("race") == "Half-Orc"
                and defender_relentless_endurance_available):
            hp_after = 1
            relentless_endurance_triggered = True
        # Death Ward (real 5E spell, 2026-08-04): "the first time this
        # creature would drop to 0 hit points as a result of taking
        # damage, it instead drops to 1 hit point, and the spell ends" --
        # same shape as Relentless Endurance just above (which this
        # engine already modeled this exact way), gated on the
        # "death_warded" condition instead of race, and consumed
        # (removed) on trigger since real Death Ward only saves you once.
        elif hp_after == 0 and hp_before > 0 and "death_warded" in defender.get("conditions", []):
            hp_after = 1
            death_ward_triggered = True
            defender["conditions"].remove("death_warded")
        defender["hp_current"] = hp_after
        if elemental_heal_gained:
            defender_hp_max = defender.get("hp_max", defender["hp_current"])
            defender["hp_current"] = min(defender["hp_current"] + elemental_heal_gained, defender_hp_max)

        # Boss Enrage (2026-07-27, per Coffee: "I want the battles to be
        # difficult" -- a real playthrough simulation confirmed even
        # dungeon bosses were dying in 1 round for a sliver of the
        # player's HP after the 2026-07-26 rebalance, since that pass
        # deliberately preserved the ORIGINAL, already-soft relative
        # difficulty rather than making anything genuinely dangerous).
        # Once a boss drops to or below ENRAGE_HP_THRESHOLD of its own
        # max HP, it permanently gains ENRAGE_DAMAGE_BONUS_PCT bonus
        # damage for the rest of the fight -- a real, one-time,
        # persistent state flip (never re-triggers, never reverts),
        # same "combat-only, in-memory, resets when combat ends"
        # convention as raging/wild_shaped.
        if (defender.get("is_boss") and not defender.get("enraged") and hp_after > 0
                and hp_after <= defender.get("hp_max", hp_after) * ENRAGE_HP_THRESHOLD):
            defender["enraged"] = True
            enrage_triggered = True

        # Bloodied (2026-08-01, task #244) -- one-time, either side,
        # crossing half HP for the first time. Never re-fires (same
        # "flag on the participant dict, checked before setting"
        # pattern as enraged), never reverts even if healed back up.
        if (not defender.get("bloodied") and hp_after > 0
                and hp_after <= defender.get("hp_max", hp_after) * BLOODIED_HP_THRESHOLD):
            defender["bloodied"] = True
            bloodied_triggered = True

        # Dark One's Blessing: reducing a hostile creature to 0 HP grants
        # the Warlock temp HP = CHA modifier + level (minimum 1, real 5E
        # formula). Temp HP doesn't stack with itself -- take the higher
        # value, not add to it, matching the real rule.
        if hp_after == 0 and hp_before > 0 and attacker.get("char_class") == "Warlock":
            blessing_hp = max(1, ability_modifier(attacker.get("charisma", 10)) + attacker.get("level", 1))
            # Universal Manipulation "darker_bargain" (2026-08-06, repeatable): +2 temp HP per point invested, unlimited.
            blessing_hp += 2 * (attacker.get("skill_tree_upgrades") or []).count("darker_bargain")
            if blessing_hp > attacker.get("temp_hp", 0):
                attacker["temp_hp"] = blessing_hp
                dark_ones_blessing_gained = blessing_hp

        # Hybrid Warlock (2026-07-22): a chance-gated, scaled-down taste
        # of Dark One's Blessing, same 0-HP trigger as the real feature
        # above -- can't ever double up with it (a hybrid pick can
        # never equal your own real char_class).
        if hp_after == 0 and hp_before > 0:
            hybrid_kill_hp = hybrid_features.hybrid_temp_hp_on_kill(attacker)
            if hybrid_kill_hp > attacker.get("temp_hp", 0):
                attacker["temp_hp"] = hybrid_kill_hp
                hybrid_temp_hp_gained = max(hybrid_temp_hp_gained, hybrid_kill_hp)

    return {
        "attacker": attacker["name"],
        "defender": defender["name"],
        "hit": attack_result["hit"],
        "critical_hit": attack_result["critical_hit"],
        "critical_fail": attack_result["critical_fail"],
        "raw_roll": attack_result["raw_roll"],
        "attack_roll": attack_result["total"],
        "target_ac": effective_defender_ac,
        "damage_dealt": damage_dealt,
        "relentless_endurance_triggered": relentless_endurance_triggered,
        "death_ward_triggered": death_ward_triggered,
        "dark_ones_blessing_gained": dark_ones_blessing_gained,
        "shield_reaction_triggered": shield_reaction_triggered,
        "uncanny_dodge_triggered": uncanny_dodge_triggered,
        "hybrid_bonus_damage": hybrid_bonus_gained,
        "hybrid_temp_hp_gained": hybrid_temp_hp_gained,
        "hybrid_self_heal_gained": hybrid_self_heal_gained,
        "defender_hp_remaining": defender["hp_current"],
        "defender_hp_max": defender.get("hp_max", defender["hp_current"]),
        # Real damage type actually used (2026-07-27, per Coffee's
        # damage-type follow-up: surface it in narration, not just
        # apply it silently) -- only meaningful on an actual hit.
        "damage_type": weapon.get("damage_type", "physical") if attack_result["hit"] else None,
        "enrage_triggered": enrage_triggered,
        "bloodied_triggered": bloodied_triggered,
        "elemental_heal_gained": elemental_heal_gained,
        # Synergy Phase 9 echoes_damage_type (2026-08-14): non-zero exactly
        # once per fight, the hit that triggers The Undertone's echo. The
        # CALLER applies this to the ATTACKER's own hp_current (resolve_
        # attack never mutates the attacker's real HP itself elsewhere),
        # same "compute here, apply outside" convention mastery_strike_
        # dmg/armor_mastery_reduction already use in bot.py's _do_attack.
        "echo_backlash_damage": echo_backlash_damage,
    }


def resolve_thrown_attack(attacker: dict, defender: dict, weapon: dict, forced_hit: bool = False,
                           forced_roll: int | None = None, forced_damage_roll: int | None = None,
                           round_number: int = 0, defender_relentless_endurance_available: bool = False) -> dict:
    """
    Throw (2026-08-08, per Coffee: universal -- any class can throw any
    weapon sitting in their inventory, not the one equipped -- with an
    Assassin's own bonus of a guaranteed hit, no roll needed). Deliberately
    a SEPARATE, self-contained function rather than another resolve_attack
    parameter: per Coffee's explicit spec ("dont use the players attack
    damage only use the weapons attack damage -- so even weak players can
    have a good attack"), this must skip every character-derived damage
    bonus resolve_attack normally folds in (rage, wild shape, guild %,
    subclass %, sneak attack, hex/hunter's mark, power-scale) -- pure
    weapon["damage_dice"] + weapon["damage_bonus"], nothing else added.
    Threading a "skip all of that" flag through resolve_attack's ~15
    separate bonus blocks would be far riskier than a small, focused
    function that only replicates what actually needs to carry over:
    the to-hit roll (or forced_hit's guaranteed connect), the weapon's
    own elemental interaction with the defender's resistances
    (apply_damage_type_modifier -- the weapon's own stat, not the
    thrower's), and the same real defensive saves resolve_attack
    already has (temp HP absorption, Relentless Endurance, Death Ward)
    so a thrown weapon can't bypass those just by using a different
    code path.

    Real live follow-up (2026-08-10, Coffee, dev-bridge screenshot): a
    thrown Rusty Dagger (1d4, no ability modifier at all under the
    original "weapon-only" design above) hit for a real, confirmed 1
    damage against a 200 HP boss -- "that is pathetic... maybe use the
    players stats in some way?! Which stat do u think?" Reversing the
    "weapon-only damage" half of the original design specifically:
    damage now adds the attacker's own modifier for whichever ability
    the weapon already uses for its TO-HIT roll (weapon_ability below --
    dexterity for a finesse weapon like a dagger, strength otherwise),
    matching real 5E's actual thrown-weapon rule (ability modifier
    always applies to a thrown weapon's damage, same as any other
    weapon attack) and directly answering "which stat" with the same
    one the accuracy roll already reads from that weapon's own data,
    not a new invented rule. The rest of the original rationale still
    holds -- every OTHER character-derived bonus (rage, wild shape,
    guild %, subclass %, sneak attack, hex/hunter's mark, power-scale)
    still deliberately does NOT carry over, so a weak character still
    doesn't get a fully-loaded main-hand attack's damage just by
    throwing instead, only their own raw physical/finesse aptitude.
    """
    weapon_ability = weapon.get("ability", "strength")
    weapon_category = weapon.get("weapon_category", "simple")
    weapon_proficient = (
        is_weapon_proficient(attacker.get("char_class"), weapon_category)
        or f"prof_{weapon_category}_weapons" in (attacker.get("skill_tree_upgrades") or [])
    )
    shield_bonus = 5 if "shield_active" in defender.get("conditions", []) else 0
    effective_defender_ac = (
        defender["armor_class"] + hybrid_features.hybrid_ac_bonus(defender) + formation_ac_bonus(defender) + shield_bonus
    )

    if forced_hit:
        # Assassin's Throw bonus: no roll at all, always connects. Not
        # reported as a natural-20 critical (that's a specific dice
        # outcome, not "guaranteed") -- a real, clean guaranteed hit.
        attack_result = {"raw_roll": None, "critical_hit": False, "critical_fail": False,
                          "total": effective_defender_ac, "hit": True}
    else:
        attack_result = roll_attack(
            attacker, target_ac=effective_defender_ac, ability=weapon_ability,
            proficient=weapon_proficient, forced_roll=forced_roll,
        )

    damage_dealt = 0
    relentless_endurance_triggered = False
    death_ward_triggered = False
    elemental_heal_gained = 0
    if attack_result["hit"]:
        ability_bonus = ability_modifier(attacker.get(weapon_ability, 10))
        dmg = roll_damage(
            weapon["damage_dice"], modifier=weapon.get("damage_bonus", 0) + ability_bonus,
            critical=attack_result["critical_hit"], forced_roll=forced_damage_roll,
        )
        damage_dealt = max(dmg["total"], 0)
        pre_elemental_damage = damage_dealt
        damage_dealt = apply_damage_type_modifier(damage_dealt, weapon.get("damage_type", "physical"), defender, attacker)
        elemental_heal_gained = elemental_overflow_heal(
            pre_elemental_damage, weapon.get("damage_type", "physical"), defender, attacker
        )
        _maybe_grow_adaptive_resistance(defender, weapon.get("damage_type", "physical"), damage_dealt)

        temp_hp = defender.get("temp_hp", 0)
        if temp_hp > 0:
            absorbed = min(temp_hp, damage_dealt)
            defender["temp_hp"] = temp_hp - absorbed
            damage_dealt -= absorbed

        hp_before = defender["hp_current"]
        hp_after = max(hp_before - damage_dealt, 0)
        if (hp_after == 0 and hp_before > 0 and defender.get("race") == "Half-Orc"
                and defender_relentless_endurance_available):
            hp_after = 1
            relentless_endurance_triggered = True
        elif hp_after == 0 and hp_before > 0 and "death_warded" in defender.get("conditions", []):
            hp_after = 1
            death_ward_triggered = True
            defender["conditions"].remove("death_warded")
        defender["hp_current"] = hp_after
        if elemental_heal_gained:
            defender_hp_max = defender.get("hp_max", defender["hp_current"])
            defender["hp_current"] = min(defender["hp_current"] + elemental_heal_gained, defender_hp_max)

    return {
        "attacker": attacker["name"],
        "defender": defender["name"],
        "hit": attack_result["hit"],
        "critical_hit": attack_result["critical_hit"],
        "critical_fail": attack_result["critical_fail"],
        "raw_roll": attack_result["raw_roll"],
        "attack_roll": attack_result["total"],
        "target_ac": effective_defender_ac,
        "damage_dealt": damage_dealt,
        "relentless_endurance_triggered": relentless_endurance_triggered,
        "death_ward_triggered": death_ward_triggered,
        "dark_ones_blessing_gained": 0,
        "shield_reaction_triggered": False,
        "uncanny_dodge_triggered": False,
        "hybrid_bonus_damage": 0,
        "hybrid_temp_hp_gained": 0,
        "hybrid_self_heal_gained": 0,
        "defender_hp_remaining": defender["hp_current"],
        "defender_hp_max": defender.get("hp_max", defender["hp_current"]),
        "damage_type": weapon.get("damage_type", "physical") if attack_result["hit"] else None,
        "enrage_triggered": False,
        "bloodied_triggered": False,
        "forced_hit": forced_hit,
        "elemental_heal_gained": elemental_heal_gained,
    }


def check_defeated(character: dict) -> bool:
    """True if the character's hp_current has dropped to 0 or below."""
    return character["hp_current"] <= 0


def resolve_death_save(character: dict) -> dict:
    """
    Resolve one death saving throw per 5E rules:
    - 10+ is a success, below 10 is a failure
    - natural 1 counts as two failures
    - natural 20 means the character regains 1 HP and becomes conscious
    - 3 successes = stable, 3 failures = dead
    Expects character to have 'death_save_successes' and
    'death_save_failures' keys (defaulting to 0 if absent).
    """
    successes = character.setdefault("death_save_successes", 0)
    failures = character.setdefault("death_save_failures", 0)

    raw = roll_d20()

    if raw == 20:
        character["hp_current"] = 1
        character["death_save_successes"] = 0
        character["death_save_failures"] = 0
        return {"roll": raw, "outcome": "natural_20_revived", "hp_current": 1}

    if raw == 1:
        character["death_save_failures"] += 2
    elif raw >= 10:
        character["death_save_successes"] += 1
    else:
        character["death_save_failures"] += 1

    if character["death_save_successes"] >= 3:
        return {"roll": raw, "outcome": "stable",
                "successes": character["death_save_successes"],
                "failures": character["death_save_failures"]}
    if character["death_save_failures"] >= 3:
        return {"roll": raw, "outcome": "dead",
                "successes": character["death_save_successes"],
                "failures": character["death_save_failures"]}

    return {"roll": raw, "outcome": "pending",
            "successes": character["death_save_successes"],
            "failures": character["death_save_failures"]}
