"""
hybrid_features.py
Real mechanical hooks for the hybrid-class system (2026-07-22, per
Coffee: "pick any second class flavor and each [rebirth] unlocking a
deeper hybrid tier"). Gated behind a character's own hybrid_class/
rebirth_count fields (see rules/leveling.py's hybrid_tier) -- every
function here is a pure, deterministic roll against the SAME real
scaling formulas the primary version of each feature already uses
(rules/leveling.py's rage_damage_bonus/wild_shape_damage_bonus/
wild_shape_temp_hp/sneak_attack_dice_count), just weaker and chance-
gated rather than automatic, since a hybrid flavor is meant to be a
taste of a second class, not the genuine full feature.

Called from rules/combat.py (damage/advantage/AC), spells.py (heal
bonus), and bot.py (rest recovery) -- the same three real hook points
every OTHER class feature in this game already plugs into.
"""
from rules.dice import roll_d20
from rules.leveling import (
    rage_damage_bonus, wild_shape_damage_bonus, wild_shape_temp_hp,
)

# Roll d20 <= threshold to succeed -- escalates with hybrid tier
# (1/2/3), same "don't guess when ambiguous" spirit as everything else
# in this game: a flat, real, named probability rather than a vague
# "sometimes."
_TIER_THRESHOLD = {1: 5, 2: 8, 3: 12}  # 25% / 40% / 60% of a d20


def _tier_roll(tier: int) -> bool:
    if tier <= 0:
        return False
    return roll_d20() <= _TIER_THRESHOLD.get(tier, 0)


def _tier(character: dict) -> int:
    from rules.leveling import hybrid_tier
    return hybrid_tier(character.get("rebirth_count", 0))


def hybrid_damage_bonus(attacker: dict) -> int:
    """
    Real bonus damage on a hit for the hybrid classes whose flavor is
    offense: Barbarian (Rage-scaled), Rogue (Sneak-Attack-scaled),
    Ranger (only at tier 3, on top of its deterministic advantage),
    Paladin (Divine-Smite-flavored), Monk (tier 3 only), Bard, Druid,
    Fighter (tier 1/2). Each check is independent -- a character can
    only ever HAVE one hybrid_class at a time, so only one of these
    branches can ever actually fire for a given attacker.
    """
    hybrid = attacker.get("hybrid_class")
    if not hybrid or hybrid == attacker.get("char_class"):
        return 0
    tier = _tier(attacker)
    level = attacker.get("level", 1)
    if hybrid == "Barbarian" and tier >= 3 and _tier_roll(tier):
        return max(1, rage_damage_bonus(level) // 2)
    if hybrid == "Fighter" and tier >= 1 and _tier_roll(tier):
        return max(1, level // 4 + 1)
    if hybrid == "Rogue" and tier >= 1 and _tier_roll(tier):
        return max(1, (level + 1) // 4 + 1)
    if hybrid == "Ranger" and tier >= 3 and _tier_roll(tier):
        return max(1, level // 5 + 1)
    if hybrid == "Paladin" and tier >= 3 and _tier_roll(tier):
        return max(1, level // 4 + 1)
    if hybrid == "Monk" and tier >= 3 and _tier_roll(tier):
        return max(1, level // 5 + 1)
    if hybrid == "Bard" and tier >= 1 and _tier_roll(tier):
        return max(1, level // 5 + 1)
    if hybrid == "Druid" and tier >= 1 and _tier_roll(tier):
        return max(1, wild_shape_damage_bonus(level) // 2)
    return 0


def hybrid_temp_hp_on_damage(attacker: dict) -> int:
    """A Druid hybrid's Wild-Shape-flavored temp HP, alongside its bonus damage above -- only rolls once damage already landed."""
    hybrid = attacker.get("hybrid_class")
    if hybrid != "Druid" or hybrid == attacker.get("char_class"):
        return 0
    tier = _tier(attacker)
    if tier >= 2 and _tier_roll(tier):
        return max(1, wild_shape_temp_hp(attacker.get("level", 1)) // 2)
    return 0


def hybrid_self_heal_on_hit(attacker: dict) -> int:
    """Fighter (tier 3 only, Second-Wind-flavored) / Paladin (tier 1+, Lay-on-Hands-flavored) self-heal when landing a hit."""
    hybrid = attacker.get("hybrid_class")
    if not hybrid or hybrid == attacker.get("char_class"):
        return 0
    tier = _tier(attacker)
    level = attacker.get("level", 1)
    if hybrid == "Fighter" and tier >= 3 and _tier_roll(tier):
        return max(1, level // 3 + 2)
    if hybrid == "Paladin" and tier >= 1 and _tier_roll(tier):
        return max(1, level // 3 + 2)
    return 0


def hybrid_temp_hp_on_kill(attacker: dict) -> int:
    """A Warlock hybrid's Dark-One's-Blessing-flavored temp HP when the attacker's hit reduces a hostile to 0 HP."""
    hybrid = attacker.get("hybrid_class")
    if hybrid != "Warlock" or hybrid == attacker.get("char_class"):
        return 0
    tier = _tier(attacker)
    if tier >= 1 and _tier_roll(tier):
        from rules.dice import ability_modifier
        return max(1, (ability_modifier(attacker.get("charisma", 10)) + attacker.get("level", 1)) // 2)
    return 0


def hybrid_ac_bonus(character: dict) -> int:
    """
    Monk/Sorcerer hybrid's static AC bump -- computed live at read
    time (never mutates the stored armor_class field), so freely
    switching hybrid classes can never leave a stale bonus behind or
    need any reversal bookkeeping.
    """
    hybrid = character.get("hybrid_class")
    if hybrid not in ("Monk", "Sorcerer") or hybrid == character.get("char_class"):
        return 0
    tier = _tier(character)
    if tier >= 2:
        return 2
    if tier >= 1:
        return 1
    return 0


def hybrid_favored_enemy_advantage(attacker: dict, defender_monster_key: str) -> bool:
    """Ranger hybrid's Favored-Enemy-flavored advantage -- goblins at tier 1+, goblins AND wolves at tier 2+."""
    if attacker.get("hybrid_class") != "Ranger" or attacker.get("char_class") == "Ranger":
        return False
    tier = _tier(attacker)
    key = defender_monster_key or ""
    if tier >= 1 and key.startswith("goblin"):
        return True
    if tier >= 2 and key == "wolf":
        return True
    return False


def hybrid_reckless_advantage(attacker: dict) -> bool:
    """Barbarian hybrid's Reckless-Attack-flavored advantage chance (tier 2+)."""
    if attacker.get("hybrid_class") != "Barbarian" or attacker.get("char_class") == "Barbarian":
        return False
    tier = _tier(attacker)
    return tier >= 2 and _tier_roll(tier)


def hybrid_damage_resistance(defender: dict) -> bool:
    """Barbarian hybrid's Rage-flavored damage resistance chance (halves damage taken) on a per-hit roll, tier 1+."""
    if defender.get("hybrid_class") != "Barbarian" or defender.get("char_class") == "Barbarian":
        return False
    tier = _tier(defender)
    return tier >= 1 and _tier_roll(tier)


def hybrid_heal_bonus(caster: dict, spell_level: int) -> int:
    """Cleric hybrid's Disciple-of-Life-flavored bonus heal, scaled down from the real Cleric formula, chance-gated."""
    if caster.get("hybrid_class") != "Cleric" or caster.get("char_class") == "Cleric":
        return 0
    tier = _tier(caster)
    if tier >= 1 and _tier_roll(tier) and spell_level > 0:
        return max(1, (1 + spell_level) // 2)
    return 0


def hybrid_rest_slot_recovery(character: dict) -> int:
    """Wizard/Sorcerer hybrid's Arcane-Recovery-flavored bonus spell-slot recovery on a completed full rest."""
    hybrid = character.get("hybrid_class")
    if hybrid not in ("Wizard", "Sorcerer") or hybrid == character.get("char_class"):
        return 0
    tier = _tier(character)
    if hybrid == "Sorcerer" and tier < 3:
        return 0
    if tier >= 1 and _tier_roll(tier):
        return 1
    return 0
