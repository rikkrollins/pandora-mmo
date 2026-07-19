"""
guilds.py
Guild definitions and membership benefits. Guild membership is stored
directly on the character record in db.py (a single 'guild' column,
since this game keeps one guild per character for simplicity).
"""

GUILDS = {
    "adventurers_guild": {
        "name": "Adventurers' Guild",
        "description": "A loose, practical association open to anyone willing to take on paid work.",
        "join_requirement_level": 1,
        "benefits": ["shop_discount_10"],
    },
    "arcane_circle": {
        "name": "The Arcane Circle",
        "description": "A secretive order of spellcasters who trade knowledge for loyalty.",
        "join_requirement_level": 3,
        "join_requirement_classes": ["wizard", "sorcerer", "warlock"],
        "benefits": ["bonus_spell_scroll", "shop_discount_10"],
    },
    "silver_wardens": {
        "name": "The Silver Wardens",
        "description": "Monster hunters who've noticed the world's darkening edges and organized against it.",
        "join_requirement_level": 5,
        "benefits": ["bonus_damage_vs_undead", "shop_discount_15"],
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
}


def get_guild_quest(guild_id: str) -> dict | None:
    return GUILD_QUESTS.get(guild_id)


def get_guild(guild_id: str) -> dict | None:
    return GUILDS.get(guild_id)


def eligible_for_guild(character: dict, guild_id: str) -> tuple[bool, str]:
    guild = GUILDS.get(guild_id)
    if guild is None:
        return False, "That guild doesn't exist."
    if character["level"] < guild["join_requirement_level"]:
        return False, f"Requires level {guild['join_requirement_level']}."
    required_classes = guild.get("join_requirement_classes")
    if required_classes and character["char_class"].lower() not in required_classes:
        return False, f"Only open to: {', '.join(required_classes)}."
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
