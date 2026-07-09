"""
rules/dice.py
Pure dice-rolling and check-resolution functions. No AI, no I/O side
effects (aside from Python's random module) — this is the deterministic
core the rest of the game trusts for numeric outcomes.
"""
import random
import re


def roll(num_dice: int, sides: int) -> list[int]:
    """Roll num_dice dice of `sides` sides each, return list of individual rolls."""
    return [random.randint(1, sides) for _ in range(num_dice)]


def roll_d20(advantage: bool = False, disadvantage: bool = False) -> int:
    """
    Roll a d20, handling 5E advantage/disadvantage:
    - advantage: roll twice, take the higher
    - disadvantage: roll twice, take the lower
    - both True: they cancel out, roll normally (5E rule)
    """
    if advantage and disadvantage:
        return random.randint(1, 20)
    if advantage:
        return max(random.randint(1, 20), random.randint(1, 20))
    if disadvantage:
        return min(random.randint(1, 20), random.randint(1, 20))
    return random.randint(1, 20)


def ability_modifier(score: int) -> int:
    """5E ability modifier formula."""
    return (score - 10) // 2


def roll_ability_check(character: dict, ability: str, proficient: bool = False,
                        advantage: bool = False, disadvantage: bool = False) -> dict:
    """
    Roll an ability check for a character dict (expects keys like
    'strength', 'dexterity', etc. and 'proficiency_bonus').
    Returns a dict with the raw roll, modifier, and total.
    """
    score = character[ability.lower()]
    mod = ability_modifier(score)
    prof = character.get("proficiency_bonus", 0) if proficient else 0
    raw = roll_d20(advantage=advantage, disadvantage=disadvantage)
    total = raw + mod + prof
    return {
        "raw_roll": raw,
        "modifier": mod,
        "proficiency": prof,
        "total": total,
    }


def roll_attack(character: dict, target_ac: int, ability: str = "strength",
                 proficient: bool = True, advantage: bool = False,
                 disadvantage: bool = False) -> dict:
    """
    Roll an attack for a character against a target AC.
    Returns hit/miss, whether it was a critical hit/fail, and the roll details.
    """
    score = character[ability.lower()]
    mod = ability_modifier(score)
    prof = character.get("proficiency_bonus", 0) if proficient else 0
    raw = roll_d20(advantage=advantage, disadvantage=disadvantage)

    critical_hit = raw == 20
    critical_fail = raw == 1
    total = raw + mod + prof

    if critical_fail:
        hit = False
    elif critical_hit:
        hit = True
    else:
        hit = total >= target_ac

    return {
        "raw_roll": raw,
        "modifier": mod,
        "proficiency": prof,
        "total": total,
        "target_ac": target_ac,
        "hit": hit,
        "critical_hit": critical_hit,
        "critical_fail": critical_fail,
    }


_DICE_NOTATION_RE = re.compile(r"^(\d+)d(\d+)\s*([+-]\s*\d+)?$")


def roll_damage(dice_notation: str, modifier: int = 0, critical: bool = False) -> dict:
    """
    Parse dice notation like '1d8', '2d6', or '1d8+2' and return damage
    rolled. An explicit `modifier` argument is ADDED to any modifier
    already present in the notation string itself. On a critical hit,
    the DICE are doubled (not the modifier), per 5E rules.
    """
    match = _DICE_NOTATION_RE.match(dice_notation.strip())
    if not match:
        raise ValueError(f"Invalid dice notation: {dice_notation!r}")

    num_dice, sides = int(match.group(1)), int(match.group(2))
    inline_modifier = int(match.group(3).replace(" ", "")) if match.group(3) else 0
    total_modifier = inline_modifier + modifier

    if critical:
        num_dice *= 2

    rolls = roll(num_dice, sides)
    total = sum(rolls) + total_modifier

    return {
        "dice_notation": dice_notation,
        "rolls": rolls,
        "modifier": total_modifier,
        "critical": critical,
        "total": total,
    }
