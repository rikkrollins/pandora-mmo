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


def roll_d20(advantage: bool = False, disadvantage: bool = False, forced_roll: int | None = None) -> int:
    """
    Roll a d20, handling 5E advantage/disadvantage:
    - advantage: roll twice, take the higher
    - disadvantage: roll twice, take the lower
    - both True: they cancel out, roll normally (5E rule)

    `forced_roll` (2026-07-16, physical-dice mode): when a player has
    opted to roll their own physical dice instead of letting the game
    roll for them (see bot.py's manual_dice_enabled flow), the real
    number they reported is substituted here instead of a random roll
    -- this is still the one place a d20 "roll" happens, so every
    downstream caller (roll_attack, roll_ability_check) gets a forced
    value for free without needing its own separate override.

    2026-07-18, per Coffee ("I only wanna do the dice ... you should be
    adding [modifiers] yourself"): this used to bypass advantage/
    disadvantage entirely, asking the player to pre-resolve it
    themselves (roll twice physically, report only the one number they
    would have used) before the manual-dice prompts were updated to
    stop asking for that. A physical roller only ever has ONE real die
    in hand for the number they report, so advantage/disadvantage is
    now resolved the same way a second dice-app roll would be: this
    function rolls ONE internal random d20 and combines it with their
    real reported number (max for advantage, min for disadvantage) --
    the player's own roll is always genuinely used, never discarded,
    exactly like a real second physical die would be if they'd had one
    on hand.
    """
    if forced_roll is not None:
        if advantage and not disadvantage:
            return max(forced_roll, random.randint(1, 20))
        if disadvantage and not advantage:
            return min(forced_roll, random.randint(1, 20))
        return forced_roll
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
                        advantage: bool = False, disadvantage: bool = False,
                        forced_roll: int | None = None) -> dict:
    """
    Roll an ability check for a character dict (expects keys like
    'strength', 'dexterity', etc. and 'proficiency_bonus').
    Returns a dict with the raw roll, modifier, and total.
    `forced_roll`: see roll_d20's own docstring (physical-dice mode).
    """
    score = character[ability.lower()]
    mod = ability_modifier(score)
    prof = character.get("proficiency_bonus", 0) if proficient else 0
    raw = roll_d20(advantage=advantage, disadvantage=disadvantage, forced_roll=forced_roll)
    total = raw + mod + prof
    return {
        "raw_roll": raw,
        "modifier": mod,
        "proficiency": prof,
        "total": total,
    }


def roll_attack(character: dict, target_ac: int, ability: str = "strength",
                 proficient: bool = True, advantage: bool = False,
                 disadvantage: bool = False, forced_roll: int | None = None) -> dict:
    """
    Roll an attack for a character against a target AC.
    Returns hit/miss, whether it was a critical hit/fail, and the roll details.
    `forced_roll`: see roll_d20's own docstring (physical-dice mode).
    """
    score = character[ability.lower()]
    mod = ability_modifier(score)
    prof = character.get("proficiency_bonus", 0) if proficient else 0
    raw = roll_d20(advantage=advantage, disadvantage=disadvantage, forced_roll=forced_roll)

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


def roll_damage(dice_notation: str, modifier: int = 0, critical: bool = False, extra_dice: int = 0) -> dict:
    """
    Parse dice notation like '1d8', '2d6', or '1d8+2' and return damage
    rolled. An explicit `modifier` argument is ADDED to any modifier
    already present in the notation string itself. On a critical hit,
    the DICE are doubled (not the modifier), per 5E rules. `extra_dice`
    (e.g. a Half-Orc's real Savage Attacks trait: "roll one additional
    weapon damage die when determining the extra damage for a critical
    hit") adds that many MORE dice of the same size on top of whatever
    `critical` already doubled — distinct from doubling, per the real
    5E wording of that trait.
    """
    match = _DICE_NOTATION_RE.match(dice_notation.strip())
    if not match:
        raise ValueError(f"Invalid dice notation: {dice_notation!r}")

    num_dice, sides = int(match.group(1)), int(match.group(2))
    inline_modifier = int(match.group(3).replace(" ", "")) if match.group(3) else 0
    total_modifier = inline_modifier + modifier

    if critical:
        num_dice *= 2
    num_dice += extra_dice

    rolls = roll(num_dice, sides)
    total = sum(rolls) + total_modifier

    return {
        "dice_notation": dice_notation,
        "rolls": rolls,
        "modifier": total_modifier,
        "critical": critical,
        "total": total,
    }


def average_damage(dice_notation: str, modifier: int = 0) -> float:
    """
    Expected (average, not rolled) damage for a dice string plus a flat
    modifier -- e.g. "1d8+2" with modifier=0 -> 6.5. Used to pick the
    real best weapon out of several carried ones (auto-equip, 2026-07-15)
    without needing to actually roll anything.
    """
    match = _DICE_NOTATION_RE.match(dice_notation.strip())
    if not match:
        raise ValueError(f"Invalid dice notation: {dice_notation!r}")
    num_dice, sides = int(match.group(1)), int(match.group(2))
    inline_modifier = int(match.group(3).replace(" ", "")) if match.group(3) else 0
    return num_dice * (sides + 1) / 2 + inline_modifier + modifier
