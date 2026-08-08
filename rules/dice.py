"""
rules/dice.py
Pure dice-rolling and check-resolution functions. No AI, no I/O side
effects (aside from Python's random module) — this is the deterministic
core the rest of the game trusts for numeric outcomes.
"""
import random
import re

from rules.leveling import utility_subclass_ability_check_bonus


def roll(num_dice: int, sides: int) -> list[int]:
    """Roll num_dice dice of `sides` sides each, return list of individual rolls."""
    return [random.randint(1, sides) for _ in range(num_dice)]


def roll_percentage_check(chance_pct: float, forced_roll: float | None = None) -> bool:
    """
    Grindable "mastery" proficiency checks (2026-08-08, per Coffee):
    Backstab, Throw, and general weapon/armor proficiency all share this
    same shape -- a real percent chance, ground up +0.01 per use toward
    100%, rolled fresh every single time. A uniform roll in [0, 100);
    success iff the roll lands strictly below chance_pct, so chance_pct
    <= 0 can never succeed and chance_pct >= 100 always does.
    `forced_roll` lets tests (and any future physical-dice-mode
    extension) substitute a real value instead of trusting randomness.
    """
    value = forced_roll if forced_roll is not None else random.uniform(0, 100)
    return value < chance_pct


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
    # Utility subclass hook (2026-07-25): a real, flat +2 when this
    # check's ability matches the character's chosen "utility"
    # archetype's own flagged ability (rules/leveling.
    # UTILITY_SUBCLASS_ABILITY_CHECK_BONUS) -- one shared hook point so
    # every ability check that already routes through here (skill
    # checks, gathering, shoving) picks it up automatically, no
    # per-call-site wiring needed.
    subclass_bonus = utility_subclass_ability_check_bonus(character.get("subclass"), ability)
    raw = roll_d20(advantage=advantage, disadvantage=disadvantage, forced_roll=forced_roll)
    total = raw + mod + prof + subclass_bonus
    return {
        "raw_roll": raw,
        "modifier": mod,
        "proficiency": prof,
        "subclass_bonus": subclass_bonus,
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


def roll_damage(dice_notation: str, modifier: int = 0, critical: bool = False, extra_dice: int = 0,
                 forced_roll: int | None = None) -> dict:
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

    `forced_roll` (task #143, physical-dice mode): a player with a real
    dice set only ever has ONE physical die for their weapon's damage
    (e.g. one d8 for a longsword) even when the notation calls for
    several dice (a crit, Savage Attacks) -- substituted into just the
    FIRST rolled die, same "the player's own roll is always genuinely
    used, never discarded, the rest fills in around it" convention
    roll_d20 already established for advantage/disadvantage. Clamped
    into this die's real range (a physical d8 can't report a 9) rather
    than trusting free-text extraction blindly.
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
    if forced_roll is not None and rolls:
        rolls[0] = min(max(forced_roll, 1), sides)
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
