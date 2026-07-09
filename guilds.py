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
