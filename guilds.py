"""
guilds.py
Guild definitions and membership benefits. Guild membership is stored
directly on the character record in db.py (a single 'guild' column,
since this game keeps one guild per character for simplicity).

Redesigned 2026-07-25 (per Coffee): 5 real guilds, each restricted to
specific classes (subclass required too, once a class has one to
choose), and joining is a real, PERMANENT commitment -- once you're in
a guild you can never leave or switch to another one. A hybrid
character (rules/leveling.py's rebirth-earned hybrid_class) qualifies
for a guild on EITHER of its two classes' terms, rewarding going hybrid
with genuinely wider guild access instead of narrowing it.
"""

GUILDS = {
    # 2026-08-11, per Coffee: every other guild has a real combat/utility
    # benefit and a real reason to seek it out; the Adventurers' Guild
    # had neither -- just the same shop_discount_10 every guild grants.
    # Now it's the one place a member can ask what real, already-posted
    # board-quest work exists across the locations they've actually
    # found (bot.guild_topic_handler's job-board query), and completing
    # any board quest as a member pays out real bonus gold on top of the
    # quest's own listed reward -- see ADVENTURERS_GUILD_BOARD_QUEST_
    # GOLD_BONUS_PCT below, applied in bot._check_board_quest_turnin.
    "adventurers_guild": {
        "name": "Adventurers' Guild",
        "description": "A loose, practical association open to anyone willing to take on paid work.",
        "join_requirement_level": 1,
        "benefits": ["shop_discount_10", "board_quest_gold_bonus_20"],
    },
    "arcane_circle": {
        "name": "The Arcane Circle",
        "description": "A secretive order of spellcasters who trade knowledge for loyalty.",
        "join_requirement_level": 3,
        "join_requirement_classes": ["wizard", "sorcerer", "warlock"],
        "benefits": ["bonus_spell_scroll", "shop_discount_10", "bonus_spell_damage_15"],
    },
    "silver_wardens": {
        "name": "The Silver Wardens",
        "description": "Monster hunters who've noticed the world's darkening edges and organized against it.",
        "join_requirement_level": 5,
        "join_requirement_classes": ["fighter", "paladin", "barbarian", "ranger", "monk"],
        "benefits": ["bonus_damage_vs_undead", "shop_discount_15"],
    },
    "thieves_guild": {
        "name": "The Thieves' Guild",
        "description": "A quiet network of pickpockets, fences, and people who ask too few questions.",
        "join_requirement_level": 3,
        "join_requirement_classes": ["rogue", "bard"],
        "benefits": ["bonus_steal", "shop_discount_10"],
    },
    "faith_circle": {
        "name": "The Faith Circle",
        "description": "Healers and keepers of living things, sworn to mend what the world keeps breaking.",
        "join_requirement_level": 3,
        "join_requirement_classes": ["cleric", "druid"],
        "benefits": ["bonus_healing", "shop_discount_10"],
    },
    # 2 more guilds (2026-07-25, per Coffee): "also consider a guild for
    # forging (with % enhancements stats)... and a guild for enchanting
    # wearable items and making items magic items." Both are a real
    # alternate PATH for the same classes Silver Wardens/Arcane Circle
    # already cover -- crafting focus instead of combat focus -- rather
    # than open to everyone, keeping the same class+subclass gating
    # every other guild here already uses.
    "forge_guild": {
        "name": "The Forge Guild",
        "description": "Master smiths who trust their own hammer and steel more than any spell.",
        "join_requirement_level": 5,
        "join_requirement_classes": ["fighter", "paladin", "barbarian", "ranger", "monk"],
        "benefits": ["bonus_weapon_damage_10", "shop_discount_10"],
    },
    "enchanters_guild": {
        "name": "The Enchanters' Guild",
        "description": "Artificers who bind real magic into ring and blade rather than casting it themselves.",
        "join_requirement_level": 5,
        "join_requirement_classes": ["wizard", "sorcerer", "warlock"],
        "benefits": ["guild_enchant_ladder", "shop_discount_10"],
    },
}

# Guild quests (task #77): one real, repeatable bounty per guild,
# completed once per real calendar day per member by winning any fight
# while a guild member -- reuses the same day-gating pattern as login
# streaks (db.update_login_streak) rather than a whole new generation/
# expiry system, since the objective itself never changes day to day.
GUILD_QUESTS = {
    "adventurers_guild": {
        "title": "Clear the Roads",
        "description": "The Guild always has paid work for anyone willing to clear out a real threat.",
        "reward_gold": 25,
        "reward_xp": 20,
    },
    "arcane_circle": {
        "title": "Field Research",
        "description": "The Circle wants firsthand accounts of real battles -- prove yourself in one.",
        "reward_gold": 20,
        "reward_xp": 30,
    },
    "silver_wardens": {
        "title": "Warden's Watch",
        "description": "Every real threat put down is one less the Wardens have to worry about later.",
        "reward_gold": 30,
        "reward_xp": 25,
    },
    "thieves_guild": {
        "title": "Quiet Work",
        "description": "The Ledger always needs proof you can still handle yourself when it matters.",
        "reward_gold": 30,
        "reward_xp": 25,
    },
    "faith_circle": {
        "title": "Mercy's Errand",
        "description": "Every real threat put down is one less the Circle has to tend the wounds of later.",
        "reward_gold": 25,
        "reward_xp": 25,
    },
    "forge_guild": {
        "title": "Proving the Steel",
        "description": "A smith's work means nothing untested -- put your own forged gear through a real fight.",
        "reward_gold": 30,
        "reward_xp": 25,
    },
    "enchanters_guild": {
        "title": "Gathering Residue",
        "description": "Real battle leaves behind the raw arcane residue enchantment needs -- go collect some.",
        "reward_gold": 25,
        "reward_xp": 30,
    },
}

# Real, modest membership benefits (2026-07-25) -- consumed by bot.py's
# _do_cast_spell (Arcane Circle) / _do_steal (Thieves' Guild) /
# spells.py's resolve_heal_spell (Faith Circle). Silver Wardens' and
# Adventurers' Guild's existing benefits (bonus_damage_vs_undead,
# shop_discount) were already real before this pass.
ARCANE_CIRCLE_SPELL_DAMAGE_BONUS_PCT = 15
THIEVES_GUILD_STEAL_BONUS = 3
FAITH_CIRCLE_HEAL_BONUS = 3
FORGE_GUILD_WEAPON_DAMAGE_BONUS_PCT = 10
ADVENTURERS_GUILD_BOARD_QUEST_GOLD_BONUS_PCT = 20
# 2026-08-11, per Coffee: "do locked treasures need keys... if not
# let[s] work it in for the thieves guild" -- lockpicking (bot._do_
# lockpick) already exists (a real DC-13 dexterity check, no keys
# needed) but had zero guild tie-in, unlike stealing just above. Same
# real +3 shape, same real trade-secrets flavor.
THIEVES_GUILD_LOCKPICK_BONUS = 3

# Arcane Circle exclusive spells (2026-07-25, per Coffee: "let them
# learn new spells not otherwise available unless in the guilds") --
# real spell_ids defined in spells.py's SPELLS, deliberately absent
# from any class's normal CLASS_SPELL_LISTS entry so they can never
# auto-unlock the ordinary way. bot.py's _do_learn_guild_spell is the
# only path to ever know one, gated on real Arcane Circle membership.
ARCANE_CIRCLE_EXCLUSIVE_SPELLS = ["starfall_lance", "voidcall"]

def get_guild_quest(guild_id: str) -> dict | None:
    return GUILD_QUESTS.get(guild_id)


def get_guild(guild_id: str) -> dict | None:
    return GUILDS.get(guild_id)


def eligible_for_guild(character: dict, guild_id: str) -> tuple[bool, str]:
    """
    Real eligibility check, now covering (2026-07-25, per Coffee):
    - Permanent membership: once in ANY guild, ineligible to join ANY
      other (including the one they're already in) -- guild membership
      is a one-time, irreversible commitment, never a re-spec.
    - Class + subclass gating: a guild with join_requirement_classes
      also requires the character to have actually chosen a subclass
      (rules/leveling.py's CLASS_SUBCLASSES/WIZARD_SCHOOLS) -- a
      fighting guild shouldn't take a magic user with no martial
      training, and vice versa, so "picked a real subclass" is the
      proof of that training, same spirit as proven_in_combat below.
    - Hybrid characters (a real second class via rebirth's hybrid_class)
      qualify for a guild on EITHER their base class's or their hybrid
      class's terms -- going hybrid genuinely widens guild access
      rather than leaving them stuck with only their original class's
      options.
    """
    guild = GUILDS.get(guild_id)
    if guild is None:
        return False, "That guild doesn't exist."
    if character.get("guild"):
        if character["guild"] == guild_id:
            return False, "You're already a member."
        current = GUILDS.get(character["guild"])
        current_name = current["name"] if current else character["guild"]
        return False, f"Guild membership is permanent — you've already sworn yourself to {current_name}."
    if character["level"] < guild["join_requirement_level"]:
        return False, f"Requires level {guild['join_requirement_level']}."
    required_classes = guild.get("join_requirement_classes")
    if required_classes:
        char_class = character["char_class"].lower()
        hybrid_class = (character.get("hybrid_class") or "").lower()
        if char_class not in required_classes and hybrid_class not in required_classes:
            return False, f"Only open to: {', '.join(c.capitalize() for c in required_classes)}."
        if not character.get("subclass"):
            return False, "Requires choosing a subclass first (say \"choose the path of...\")."
    # Task #170, per Coffee: guild membership should require vetting, not
    # be an instant join. Reuses the exact same real proof every guild
    # already demands of its ONGOING members (winning a real fight, see
    # GUILD_QUESTS above) -- a prospective member has to show that same
    # thing once, first, rather than a guild-specific new mechanic.
    # `proven_in_combat` is set the moment a real (non-AI) party member
    # wins any real fight (bot.py's _award_victory_xp).
    if not character.get("proven_in_combat"):
        return False, "requires proving yourself in real combat first — win a real fight, then ask again."
    return True, ""


def shop_discount_for_guild(guild_id: str | None) -> float:
    """Returns a fractional discount (e.g. 0.10 for 10% off) for the given guild."""
    if guild_id is None:
        return 0.0
    guild = GUILDS.get(guild_id)
    if guild is None:
        return 0.0
    for benefit in guild["benefits"]:
        if benefit.startswith("shop_discount_"):
            return int(benefit.split("_")[-1]) / 100.0
    return 0.0
