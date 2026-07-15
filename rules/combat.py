"""
rules/combat.py
Turn-based combat resolution built on rules/dice.py. This module is
pure logic — no AI calls, no narration text. Everything here returns
structured data; the AI layer (ai/dm_agent.py) only ever narrates the
results computed here, never decides them.
"""
from rules.dice import roll_d20, roll_attack, roll_damage, ability_modifier

# Monsters this campaign treats as undead for the Silver Wardens guild's
# bonus_damage_vs_undead benefit (guilds.py) -- no monster template field
# for creature type exists in this game, so, same convention as Ranger's
# Favored Enemy matching on monster_key prefix, this is a fixed set of
# monster_keys rather than a new schema field. Shadow Wisp is this
# campaign's one spectral/undead-flavored monster.
UNDEAD_MONSTER_KEYS = {"shadow_wisp"}


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
                    defender_relentless_endurance_available: bool = False) -> dict:
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
    """
    attack_ability = "dexterity" if attacker.get("char_class") == "Monk" else weapon.get("ability", "strength")
    attack_result = roll_attack(
        attacker,
        target_ac=defender["armor_class"],
        ability=attack_ability,
        proficient=True,
        advantage=advantage,
        disadvantage=disadvantage,
    )

    damage_dealt = 0
    relentless_endurance_triggered = False
    dark_ones_blessing_gained = 0
    if attack_result["hit"]:
        savage_attacks_die = 1 if (attacker.get("race") == "Half-Orc" and attack_result["critical_hit"]) else 0
        sneak_attack_die = 1 if (attacker.get("char_class") == "Rogue" and advantage) else 0
        rage_bonus = 2 if attacker.get("raging") else 0
        # Silver Wardens guild benefit (bonus_damage_vs_undead, guilds.py):
        # +2 damage against this campaign's undead-flavored monsters.
        warden_bonus = (
            2 if (attacker.get("guild") == "silver_wardens"
                  and defender.get("monster_key") in UNDEAD_MONSTER_KEYS)
            else 0
        )
        dmg = roll_damage(
            weapon["damage_dice"],
            modifier=weapon.get("damage_bonus", 0) + rage_bonus + warden_bonus,
            critical=attack_result["critical_hit"],
            extra_dice=savage_attacks_die,
        )
        damage_dealt = max(dmg["total"], 0)
        if sneak_attack_die:
            sneak_dmg = roll_damage("1d6", critical=attack_result["critical_hit"])
            damage_dealt += sneak_dmg["total"]
        if defender.get("raging"):
            damage_dealt = damage_dealt // 2

        # A Warlock's Otherworldly Patron (The Fiend, the default patron
        # for every Warlock here -- see resolve_attack's Martial Arts
        # comment above for why this game gives fixed defaults rather
        # than a half-built subclass-choice system) grants temporary HP
        # that absorbs damage before real HP, same combat-only,
        # in-memory-only convention as `raging`/`conditions` (reset when
        # combat ends, never persisted to the DB).
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
        defender["hp_current"] = hp_after

        # Dark One's Blessing: reducing a hostile creature to 0 HP grants
        # the Warlock temp HP = CHA modifier + level (minimum 1, real 5E
        # formula). Temp HP doesn't stack with itself -- take the higher
        # value, not add to it, matching the real rule.
        if hp_after == 0 and hp_before > 0 and attacker.get("char_class") == "Warlock":
            blessing_hp = max(1, ability_modifier(attacker.get("charisma", 10)) + attacker.get("level", 1))
            if blessing_hp > attacker.get("temp_hp", 0):
                attacker["temp_hp"] = blessing_hp
                dark_ones_blessing_gained = blessing_hp

    return {
        "attacker": attacker["name"],
        "defender": defender["name"],
        "hit": attack_result["hit"],
        "critical_hit": attack_result["critical_hit"],
        "critical_fail": attack_result["critical_fail"],
        "raw_roll": attack_result["raw_roll"],
        "attack_roll": attack_result["total"],
        "target_ac": defender["armor_class"],
        "damage_dealt": damage_dealt,
        "relentless_endurance_triggered": relentless_endurance_triggered,
        "dark_ones_blessing_gained": dark_ones_blessing_gained,
        "defender_hp_remaining": defender["hp_current"],
        "defender_hp_max": defender.get("hp_max", defender["hp_current"]),
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
