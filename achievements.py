"""
achievements.py
Titles & Achievements (task #73). Every achievement here is checked
against real, already-existing character data (level, xp, gold,
known_monsters, completed_quests, board_quests_completed, guild,
equipped gear) -- never a new counter invented just for this feature.
Unlocking one grants a real title the player can wear (see bot.py's
_do_set_title / /title), shown next to their name on their sheet.

check.type is one of: min_level, min_gold, min_known_monsters,
min_completed_quests, min_board_quests_completed, has_guild,
well_equipped (needs a weapon AND either armor or a shield equipped).
See bot.py's _achievement_condition_met for how each is evaluated.
"""

ACHIEVEMENTS = {
    "first_steps": {
        "name": "First Steps",
        "description": "Reach character level 2.",
        "title": "the Initiate",
        "check": {"type": "min_level", "value": 2},
    },
    "battle_tested": {
        "name": "Battle-Tested",
        "description": "Reach character level 5.",
        "title": "the Battle-Tested",
        "check": {"type": "min_level", "value": 5},
    },
    "legendary": {
        "name": "Legendary",
        "description": "Reach character level 10.",
        "title": "the Legendary",
        "check": {"type": "min_level", "value": 10},
    },
    "monster_hunter": {
        "name": "Monster Hunter",
        "description": "Learn the real bestiary entry for 3 different monsters.",
        "title": "the Monster Hunter",
        "check": {"type": "min_known_monsters", "value": 3},
    },
    "beast_slayer": {
        "name": "Beast-Slayer",
        "description": "Learn the real bestiary entry for 6 different monsters.",
        "title": "the Beast-Slayer",
        "check": {"type": "min_known_monsters", "value": 6},
    },
    "quest_master": {
        "name": "Quest Master",
        "description": "Complete 3 real story quests.",
        "title": "the Questmaster",
        "check": {"type": "min_completed_quests", "value": 3},
    },
    "reliable": {
        "name": "Reliable",
        "description": "Complete 5 quest board bounties.",
        "title": "the Reliable",
        "check": {"type": "min_board_quests_completed", "value": 5},
    },
    "guild_member": {
        "name": "Guild Member",
        "description": "Join a guild.",
        "title": "the Guildsman",
        "check": {"type": "has_guild"},
    },
    "battle_ready": {
        "name": "Battle-Ready",
        "description": "Have a weapon and either armor or a shield equipped at the same time.",
        "title": "the Battle-Ready",
        "check": {"type": "well_equipped"},
    },
    "wealthy": {
        "name": "Wealthy",
        "description": "Carry 500 gold at once.",
        "title": "the Wealthy",
        "check": {"type": "min_gold", "value": 500},
    },
}


def get_achievement(achievement_id: str) -> dict | None:
    return ACHIEVEMENTS.get(achievement_id)
