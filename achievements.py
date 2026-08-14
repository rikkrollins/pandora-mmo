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
well_equipped (needs a weapon AND either armor or a shield equipped),
master_of_any_profession (Master rank in any of the 7 real professions),
completed_specific_quest (a single named quest_id, for one-off finale
achievements rather than a reusable threshold), min_bound_remnants (real
count of character["bound_remnants"]), has_secondary_guild (a real
"doubled up" Promotion guild beyond the primary -- character["secondary_
guilds"] non-empty).
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
    "ascended": {
        "name": "Ascended",
        "description": "Reach character level 99 (task #215's real level-cap extension, 2026-07-21).",
        "title": "the Ascended",
        "check": {"type": "min_level", "value": 99},
    },
    "reborn": {
        "name": "Reborn",
        "description": "Go through the rebirth loop once (2026-07-22's prestige system) -- level and XP reset, everything else earned stays.",
        "title": "the Reborn",
        "check": {"type": "min_rebirth_count", "value": 1},
    },
    "undying": {
        "name": "the Undying",
        "description": "Rebirth 3 times -- deep enough for your hybrid class to reach its final tier.",
        "title": "the Undying",
        "check": {"type": "min_rebirth_count", "value": 3},
    },
    "godlike": {
        "name": "Godlike",
        "description": "Rebirth 10 times -- an ability-score cap of 40, real godhood by this game's own numbers.",
        "title": "the Godlike",
        "check": {"type": "min_rebirth_count", "value": 10},
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
    "master_of_a_trade": {
        "name": "Master of a Trade",
        "description": "Reach Master rank (the top practiced bonus) in any real profession.",
        "title": "the Master Artisan",
        "check": {"type": "master_of_any_profession"},
    },
    # Task #134, per Coffee: "Hidden emergent-synergy system: secret
    # builds discovered, not documented." Deliberately vague description
    # text (this module's own achievements already never get listed to
    # a player before they're earned -- see bot.py's _do_check_achievements,
    # which only ever shows the unlocked list) -- these two are real,
    # multi-system combos (alignment + a real skill-tree investment +
    # an existing level milestone), not a single stat threshold, so
    # they can't be stumbled into by chasing one number.
    "the_elect": {
        "name": "The Elect",
        "description": "A path few walk on purpose.",
        "title": "the Elect",
        "check": {"type": "hidden_synergy", "variant": "elect"},
    },
    "the_damned": {
        "name": "The Damned",
        "description": "A path few walk on purpose.",
        "title": "the Damned",
        "check": {"type": "hidden_synergy", "variant": "damned"},
    },
    # The true ending (2026-07-25): the 3rd and final rebirth-gated
    # dungeon's capstone quest gets its own achievement, tied to that
    # one specific quest (completed_specific_quest) rather than a
    # reusable threshold -- deliberate, since reaching this one moment
    # is meant to feel unmistakably different from every other
    # achievement here. See bot.py's _complete_quest_and_announce for
    # this same quest's own distinct completion message.
    "cycle_breaker": {
        "name": "Cycle-Breaker",
        "description": "Defeat The Unbegun -- the thing that was waiting before the Waiting Shape ever stood watch, and the true reason the cycle never really ended.",
        "title": "the Cycle-Breaker",
        "check": {"type": "completed_specific_quest", "quest_id": "the_unbegun_reckoning"},
    },
    # The true hidden final boss (2026-07-25): gated on 10 rebirths AND
    # having already defeated every other secret final boss in the game
    # (the Waiting Shape, the Unrepeating, the Unbegun) -- the genuine,
    # game-completing capstone this achievement list has been building
    # toward.
    "the_answered": {
        "name": "The Answered",
        "description": "Defeat The Unasked -- the thing Pandora's box was always going to hold, sealed by a question nobody before you was willing to finish asking.",
        "title": "the Answered",
        "check": {"type": "completed_specific_quest", "quest_id": "the_unaskeds_reckoning"},
    },
    # Real coverage gap found in a 2026-08-14 full synergy audit: the
    # Remnants system (2026-08-13) and guild Promotions (secondary/
    # "doubled up" guilds, also 2026-08-13) both postdate this list's own
    # last addition (2026-07-25) and had zero achievement coverage despite
    # being full, real systems with their own persistent character state.
    "remnant_keeper": {
        "name": "Remnant Keeper",
        "description": "Bind your first Unbound Remnant of Pandora's Box.",
        "title": "the Remnant Keeper",
        "check": {"type": "min_bound_remnants", "value": 1},
    },
    "remnant_master": {
        "name": "Remnant Master",
        "description": "Bind all 12 Unbound Remnants of Pandora's Box.",
        "title": "the Remnant Master",
        "check": {"type": "min_bound_remnants", "value": 12},
    },
    "promoted": {
        "name": "Promoted",
        "description": "Hold a second, \"doubled up\" guild alongside your primary one.",
        "title": "the Promoted",
        "check": {"type": "has_secondary_guild"},
    },
}


def get_achievement(achievement_id: str) -> dict | None:
    return ACHIEVEMENTS.get(achievement_id)
