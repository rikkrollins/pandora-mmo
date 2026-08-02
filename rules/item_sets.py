"""
rules/item_sets.py
Real Diablo-style set bonuses (magic item system Phase 5, 2026-08-02).
Per Coffee's explicit decision: a small number of hand-authored, NAMED
sets with deliberately-chosen bonuses, not randomly-assembled tag
matches -- matches how rules/item_generator.py's own tier flavor names
are curated, not procedurally generated word salad. Each set's own
piece-count thresholds grant bonus AFFIXES drawn from the exact same
shared vocabulary db._apply_affix already understands (stat_bonus,
elemental_damage, resistance/vulnerability/immunity, grants_spell,
profession_bonus) -- a set bonus is never a new kind of effect, just
more of the same ones, active only while enough pieces are worn.
"""

ITEM_SETS = {
    "emberwoven_vanguard": {
        "name": "The Emberwoven Vanguard",
        "pieces": {
            2: [{"kind": "stat_bonus", "field": "ac_base", "value": 2}],
            4: [{"kind": "resistance", "damage_type": "fire"}],
        },
    },
    "hollow_choir": {
        "name": "The Hollow Choir",
        "pieces": {
            2: [{"kind": "stat_bonus", "field": "damage_bonus", "value": 2}],
            4: [{"kind": "resistance", "damage_type": "necrotic"}],
        },
    },
}

# Only a rare+ item generation roll gets a chance at a set tag -- same
# eligibility bar item_generator.py's own elemental affixes already use,
# keeping a set piece a real step up from an ordinary rare/very_rare/
# legendary drop, not noise on every roll.
SET_TAG_ELIGIBLE_TIERS = {"rare", "very_rare", "legendary", "mythic"}
SET_TAG_CHANCE = 0.15


def get_set(set_id: str) -> dict | None:
    return ITEM_SETS.get(set_id)


def active_set_bonus_affixes(equipped_set_ids: list[str]) -> list[dict]:
    """
    Given the set_id of every currently-equipped item (duplicates
    included -- one entry per piece worn), returns every bonus affix
    whose piece-count threshold is currently met, across every set worn
    at once. A player wearing 2 pieces of one set and 2 of another gets
    both sets' 2-piece bonuses, never cross-set totals.
    """
    counts: dict[str, int] = {}
    for set_id in equipped_set_ids:
        if not set_id:
            continue
        counts[set_id] = counts.get(set_id, 0) + 1

    affixes = []
    for set_id, worn_count in counts.items():
        item_set = ITEM_SETS.get(set_id)
        if not item_set:
            continue
        for threshold, bonus_affixes in item_set["pieces"].items():
            if worn_count >= threshold:
                affixes.extend(bonus_affixes)
    return affixes
