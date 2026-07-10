"""
rules/proficiency.py
"The more you do something, the better you get at it" — a real,
deterministic bonus for repeated use of an ability in non-combat checks
(skill checks, lockpicking, gathering, crafting). Never invented by AI
narration: purely a function of a stored per-ability use count (see
db.record_skill_use / db.get_skill_uses). Deliberately NOT applied to
attack rolls — this is a skill-practice mechanic, not a combat buff, so
it can't be used to quietly power-creep combat balance.
"""

PRACTICE_USES_PER_BONUS = 5
MAX_PRACTICE_BONUS = 3


def practiced_bonus(use_count: int) -> int:
    """+1 per PRACTICE_USES_PER_BONUS uses of an ability, capped at MAX_PRACTICE_BONUS."""
    return min(use_count // PRACTICE_USES_PER_BONUS, MAX_PRACTICE_BONUS)


def uses_until_next_bonus(use_count: int) -> int:
    """How many more uses until the next +1 practiced bonus (0 if already at the cap)."""
    if practiced_bonus(use_count) >= MAX_PRACTICE_BONUS:
        return 0
    return PRACTICE_USES_PER_BONUS - (use_count % PRACTICE_USES_PER_BONUS)
