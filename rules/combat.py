"""
rules/combat.py
Turn-based combat resolution built on rules/dice.py. This module is
pure logic — no AI calls, no narration text. Everything here returns
structured data; the AI layer (ai/dm_agent.py) only ever narrates the
results computed here, never decides them.
"""
from rules.dice import roll_d20, roll_attack, roll_damage, ability_modifier


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
                    advantage: bool = False, disadvantage: bool = False) -> dict:
    """
    Resolve one attack. `weapon` is a dict like:
        {"ability": "strength", "damage_dice": "1d8", "damage_bonus": 0}
    Applies damage directly to defender['hp_current'] and returns a
    structured result — not narration text. `advantage`/`disadvantage`
    let callers apply real mechanical effects from conditions (e.g. a
    prone attacker has disadvantage; attacking a prone target has
    advantage) — if both are True they correctly cancel out (5E rule,
    already handled inside roll_attack/roll_d20).
    """
    attack_result = roll_attack(
        attacker,
        target_ac=defender["armor_class"],
        ability=weapon.get("ability", "strength"),
        proficient=True,
        advantage=advantage,
        disadvantage=disadvantage,
    )

    damage_dealt = 0
    if attack_result["hit"]:
        dmg = roll_damage(
            weapon["damage_dice"],
            modifier=weapon.get("damage_bonus", 0),
            critical=attack_result["critical_hit"],
        )
        damage_dealt = max(dmg["total"], 0)
        defender["hp_current"] = max(defender["hp_current"] - damage_dealt, 0)

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
