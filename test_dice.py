"""
test_dice.py
Runs statistical sanity checks on the dice engine: plain d20 average,
advantage/disadvantage skew, and basic attack/damage resolution.
Run with: python test_dice.py
"""
from rules.dice import roll_d20, roll_attack, roll_damage, ability_modifier

ITERATIONS = 1000


def test_plain_d20_average():
    total = sum(roll_d20() for _ in range(ITERATIONS))
    avg = total / ITERATIONS
    print(f"[plain d20] average over {ITERATIONS} rolls: {avg:.3f} (expected ~10.5)")
    assert 9.5 < avg < 11.5, f"Plain d20 average out of expected range: {avg}"


def test_advantage_skews_up():
    total = sum(roll_d20(advantage=True) for _ in range(ITERATIONS))
    avg = total / ITERATIONS
    print(f"[advantage]  average over {ITERATIONS} rolls: {avg:.3f} (expected ~13.8)")
    assert avg > 12.5, f"Advantage average did not skew up as expected: {avg}"


def test_disadvantage_skews_down():
    total = sum(roll_d20(disadvantage=True) for _ in range(ITERATIONS))
    avg = total / ITERATIONS
    print(f"[disadvantage] average over {ITERATIONS} rolls: {avg:.3f} (expected ~7.2)")
    assert avg < 8.5, f"Disadvantage average did not skew down as expected: {avg}"


def test_ability_modifier_table():
    cases = {8: -1, 10: 0, 11: 0, 12: 1, 15: 2, 20: 5, 3: -4}
    for score, expected_mod in cases.items():
        actual = ability_modifier(score)
        print(f"[ability_modifier] score={score} -> mod={actual} (expected {expected_mod})")
        assert actual == expected_mod, f"Wrong modifier for score {score}: got {actual}, expected {expected_mod}"


def test_attack_and_damage_sample():
    attacker = {"strength": 16, "proficiency_bonus": 2}
    result = roll_attack(attacker, target_ac=13, ability="strength")
    print(f"[sample attack] {result}")
    assert "hit" in result and "total" in result

    dmg = roll_damage("1d8", modifier=3)
    print(f"[sample damage 1d8+3] {dmg}")
    assert dmg["total"] == sum(dmg["rolls"]) + 3

    crit_dmg = roll_damage("1d8", modifier=3, critical=True)
    print(f"[sample CRIT damage 1d8+3] {crit_dmg}")
    assert len(crit_dmg["rolls"]) == 2, "Critical hit should double the dice, not the modifier"


if __name__ == "__main__":
    test_plain_d20_average()
    test_advantage_skews_up()
    test_disadvantage_skews_down()
    test_ability_modifier_table()
    test_attack_and_damage_sample()
    print("\nAll dice engine tests passed.")
