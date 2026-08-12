"""
guild_curriculum.py
Daily/ongoing per-guild training quests (2026-08-12, per Coffee: "put
work into" a real curriculum that hand-holds a new guild member through
learning their profession/craft end to end, gated so it "can't be
rushed" -- a player must actually spend real time and effort, one real
step at a time, not just tap through it).

Each guild has its own ordered, hand-authored chain of REAL objectives
(`GUILD_CURRICULUM[guild_id]`, a list of step dicts) -- a character's
progress through it is a single index (`character["guild_curriculum_
step"]`), advanced one at a time, never skippable or reorderable. Every
step reuses an EXISTING real trigger/objective vocabulary already wired
into this game rather than inventing new mechanics:

  - "reach_location"   -- same trigger shape as campaign.json's own
                           story quests (checked at the same real
                           "arrival" event, bot._check_quest_completions
                           _reach_location's sibling).
  - "defeat_monster"    -- same shape, checked at the same real combat-
                           victory checkpoint.
  - "gather_material"   -- same shape board_quests already uses, checked
                           right where a gather action actually succeeds.
  - "solve_puzzle"      -- a real riddle with fixed accepted_answers,
                           same shape as campaign.json's own puzzles
                           catalog and bot._do_answer_puzzle.
  - "npc_dialogue"      -- talk to a specific real NPC with a specific
                           real keyword in the message, checked at the
                           same real talk_npc success checkpoint.
  - "dice_challenge"    -- a real 2d6 roll (rules.dice.roll, same dice
                           function _do_gamble already uses) against a
                           fixed target total, played on demand in the
                           guild's own topic ("try my luck"/"attempt the
                           trial") -- free to reattempt, no gold at risk
                           (distinct from _do_gamble, which wagers real
                           gold), since this is a training exercise, not
                           a game of chance for its own sake.
  - "alignment_choice"  -- a real two-option branching decision, same
                           narrative shape as board_quests.py's own
                           moral-choice quests (a real setup + two
                           labeled choices, each with fixed XP/gold and a
                           fixed alignment nudge) -- per Coffee's explicit
                           constraint, the choice's real alignment axis
                           and direction are NEVER shown to the player,
                           only the two labeled options themselves. Kept
                           on the character row (guild_curriculum_state),
                           not a new board_quests row, since this is
                           personal training progress, not a location-
                           posted bounty anyone can pick up.

Real, deliberate pacing gate (Coffee: "make this gated so players cant
do them too soon, and they must grind it out"): a freshly-unlocked step
can't be COMPLETED until GUILD_CURRICULUM_STEP_COOLDOWN_HOURS have
passed since it unlocked, even if the player satisfies its objective
immediately -- see bot._guild_curriculum_step_is_gated. The step is
still visible/attemptable at any time (so a player can start working
toward it right away); only the final credit is time-gated.
"""

# Real, deliberate pacing: even if a member satisfies a fresh step's
# objective instantly, it can't be credited until this many real hours
# have passed since it unlocked -- makes the curriculum a genuine
# multi-day commitment, not a one-sitting checklist.
GUILD_CURRICULUM_STEP_COOLDOWN_HOURS = 6

# 2d6 threshold for "dice_challenge" steps (rules.dice.roll(2, 6) ranges
# 2-12) -- same GAMBLE_WIN_THRESHOLD-shaped odds as bot._do_gamble's own
# 2d6 game, so the difficulty feels consistent with a mechanic players
# likely already know.
DICE_CHALLENGE_DEFAULT_THRESHOLD = 8

GUILD_CURRICULUM = {
    "adventurers_guild": [
        {
            "id": "adv_1_the_lay_of_the_land",
            "title": "The Lay of the Land",
            "flavor": (
                "Every real job starts the same way — knowing where the work actually is. "
                "The Guild wants you to walk the market district yourself before it hands you anything real."
            ),
            "min_level": 1,
            "trigger": {"type": "reach_location", "location": "market_row"},
            "reward_xp": 30,
            "reward_gold": 15,
        },
        {
            "id": "adv_2_clear_the_roads",
            "title": "Clear the Roads, For Real",
            "flavor": (
                "Talk is cheap. The Guild's ledger only counts a real threat actually put down — "
                "go deal with the goblins causing trouble at the Warrens."
            ),
            "min_level": 2,
            "trigger": {"type": "defeat_monster", "monster": "goblin", "count": 1},
            "reward_xp": 50,
            "reward_gold": 25,
        },
        {
            "id": "adv_3_reading_the_odds",
            "title": "Reading the Odds",
            "flavor": (
                "Half of paid work is knowing when a bad bet is still worth taking. The Guild's regulars "
                "want to see your nerve at the table first — say \"try my luck\" here when you're ready to roll."
            ),
            "min_level": 3,
            "trigger": {"type": "dice_challenge", "threshold": DICE_CHALLENGE_DEFAULT_THRESHOLD},
            "reward_xp": 40,
            "reward_gold": 30,
        },
        {
            "id": "adv_4_the_quiet_wrinkle",
            "title": "The Quiet Wrinkle",
            "flavor": (
                "A courier's satchel turned up loose on the road, unclaimed and apparently unmissed. "
                "The Guild doesn't ask what you do with it — that part's on you."
            ),
            "min_level": 4,
            "trigger": {
                "type": "alignment_choice",
                "setup": (
                    "The satchel's just sitting there, half-buried at the roadside, no owner in sight and no "
                    "one asking after it. Whatever's inside is yours the moment you decide it is."
                ),
                "choices": {
                    "keep_it": {
                        "label": "keep it and say nothing",
                        "reward_xp": 60,
                        "reward_gold": 80,
                        "alignment_law_chaos_delta": -5,
                        "alignment_good_evil_delta": -5,
                        "outcome": "You pocket it and walk on. Nobody comes looking. Nobody ever will.",
                    },
                    "turn_it_in": {
                        "label": "turn it in to the Guild's lost-and-found board",
                        "reward_xp": 60,
                        "reward_gold": 10,
                        "alignment_law_chaos_delta": 5,
                        "alignment_good_evil_delta": 5,
                        "outcome": (
                            "The Guild logs it without comment and posts it for its real owner — one more "
                            "small thing done properly, whether or not anyone ever notices."
                        ),
                    },
                },
            },
            "reward_xp": 0,
            "reward_gold": 0,
        },
    ],
    "arcane_circle": [
        {
            "id": "arc_1_the_nook",
            "title": "Find the Nook",
            "flavor": "The Circle's real business happens where outsiders don't wander. Find the Arcane Nook yourself.",
            "min_level": 3,
            "trigger": {"type": "reach_location", "location": "the_arcane_nook"},
            "reward_xp": 40,
            "reward_gold": 10,
        },
        {
            "id": "arc_2_the_first_riddle",
            "title": "The First Riddle",
            "flavor": (
                "Before the Circle teaches you a single real working, it wants proof you can think past the "
                "obvious answer. Solve this, in the Circle's own topic: \"I have cities, but no houses; "
                "forests, but no trees; rivers, but no water. What am I?\""
            ),
            "min_level": 3,
            "trigger": {
                "type": "solve_puzzle",
                "accepted_answers": ["a map", "map", "a map of the world", "a globe"],
            },
            "reward_xp": 45,
            "reward_gold": 0,
        },
        {
            "id": "arc_3_reagents",
            "title": "Reagents of Your Own",
            "flavor": "A spellcaster who can't source their own reagents is at someone else's mercy. Gather moonpetal.",
            "min_level": 4,
            "trigger": {"type": "gather_material", "material": "moonpetal", "count": 3},
            "reward_xp": 50,
            "reward_gold": 20,
        },
        {
            "id": "arc_4_a_wanderers_warning",
            "title": "A Wanderer's Warning",
            "flavor": (
                "Vesh Nightglass got turned around chasing a light into Glimmerdeep Grotto and never quite "
                "found her way back to caring about anything else since. Ask her, in person, about power."
            ),
            "min_level": 5,
            "trigger": {"type": "npc_dialogue", "npc": "vesh_nightglass", "keywords": ["power"]},
            "reward_xp": 60,
            "reward_gold": 15,
            "reward_mastery_profession": "enchanting",
            "reward_mastery_pct": 2.0,
        },
    ],
    "silver_wardens": [
        {
            "id": "wrd_1_first_undead",
            "title": "What the Dark Leaves Behind",
            "flavor": "The Wardens don't recruit on promises. Put down a real wraith and prove the training took.",
            "min_level": 5,
            "trigger": {"type": "defeat_monster", "monster": "verge_wraith", "count": 1},
            "reward_xp": 70,
            "reward_gold": 30,
        },
        {
            "id": "wrd_2_the_downs",
            "title": "Walk the Downs",
            "flavor": "Greymoor Downs is where the Wardens' own watch keeps closest count. Go see it yourself.",
            "min_level": 5,
            "trigger": {"type": "reach_location", "location": "greymoor_downs"},
            "reward_xp": 40,
            "reward_gold": 10,
        },
        {
            "id": "wrd_3_grasks_measure",
            "title": "Grask's Measure",
            "flavor": (
                "Grask Emberscale spent a long stretch as the goblins' captive and never quite lost the edge "
                "it cost him to survive it. Ask him, plainly, about strength."
            ),
            "min_level": 6,
            "trigger": {"type": "npc_dialogue", "npc": "grask_emberscale", "keywords": ["strength", "strong"]},
            "reward_xp": 55,
            "reward_gold": 20,
        },
        {
            "id": "wrd_4_after_the_kill",
            "title": "After the Kill",
            "flavor": (
                "The thing you just put down wasn't always what it is now — and it left behind someone who "
                "still remembers it as it was, asking what you mean to do about that."
            ),
            "min_level": 7,
            "trigger": {
                "type": "alignment_choice",
                "setup": (
                    "There's a marker here, half-sunk and long neglected, naming whatever this creature used "
                    "to be before it stopped being that. Someone left it. Someone might still visit it."
                ),
                "choices": {
                    "raze_it": {
                        "label": "clear the marker away with the rest of the site",
                        "reward_xp": 70,
                        "reward_gold": 25,
                        "alignment_law_chaos_delta": -5,
                        "alignment_good_evil_delta": -10,
                        "outcome": "You leave the ground bare behind you. Whatever visited here won't find it again.",
                    },
                    "leave_it": {
                        "label": "leave the marker standing and move on",
                        "reward_xp": 70,
                        "reward_gold": 5,
                        "alignment_law_chaos_delta": 5,
                        "alignment_good_evil_delta": 10,
                        "outcome": "You step around it and let it be. Some things aren't yours to clear away.",
                    },
                },
            },
            "reward_xp": 0,
            "reward_gold": 0,
        },
    ],
    "thieves_guild": [
        {
            "id": "thf_1_the_ledgers_game",
            "title": "The Ledger's Game",
            "flavor": (
                "The Guild trusts a steady hand before it trusts anything you say about yourself. "
                "Say \"try my luck\" here to show it."
            ),
            "min_level": 3,
            "trigger": {"type": "dice_challenge", "threshold": DICE_CHALLENGE_DEFAULT_THRESHOLD},
            "reward_xp": 35,
            "reward_gold": 25,
        },
        {
            "id": "thf_2_casing_the_bridge",
            "title": "Casing the Bridge",
            "flavor": "Know your ground before you ever need it. Walk Stonearch Bridge and its blind spots yourself.",
            "min_level": 3,
            "trigger": {"type": "reach_location", "location": "stonearch_bridge"},
            "reward_xp": 35,
            "reward_gold": 15,
        },
        {
            "id": "thf_3_kess_terms",
            "title": "Kess's Terms",
            "flavor": (
                "Kess sizes up every traveler as a mark before she decides otherwise about them. "
                "Ask her, directly, whether she trusts you yet."
            ),
            "min_level": 4,
            "trigger": {"type": "npc_dialogue", "npc": "kess_the_bandit", "keywords": ["trust"]},
            "reward_xp": 45,
            "reward_gold": 20,
        },
        {
            "id": "thf_4_the_locksmiths_puzzle",
            "title": "The Locksmith's Puzzle",
            "flavor": (
                "Every real lockpick in this Guild started with the same test question, in the Guild's own "
                "topic: \"The more you take, the more you leave behind. What am I?\""
            ),
            "min_level": 5,
            "trigger": {
                "type": "solve_puzzle",
                "accepted_answers": ["footsteps", "footprints", "steps"],
            },
            "reward_xp": 55,
            "reward_gold": 25,
        },
    ],
    "faith_circle": [
        {
            "id": "faith_1_the_herbs",
            "title": "What Heals",
            "flavor": "Before the Circle teaches you a single prayer, it wants your hands on real herbs. Gather silverleaf.",
            "min_level": 3,
            "trigger": {"type": "gather_material", "material": "silverleaf_herb", "count": 3},
            "reward_xp": 40,
            "reward_gold": 15,
        },
        {
            "id": "faith_2_the_shrine",
            "title": "The Shrine Itself",
            "flavor": "The Hollow Stump Shrine is where the Circle actually does its quiet work. Go see it.",
            "min_level": 3,
            "trigger": {"type": "reach_location", "location": "hollow_stump_shrine"},
            "reward_xp": 30,
            "reward_gold": 10,
        },
        {
            "id": "faith_3_wrens_tending",
            "title": "Wren's Tending",
            "flavor": (
                "Wren Hollowbrook has kept this shrine's wild garden alone for longer than anyone asks about. "
                "Ask her about mercy."
            ),
            "min_level": 4,
            "trigger": {"type": "npc_dialogue", "npc": "wren_hollowbrook", "keywords": ["mercy"]},
            "reward_xp": 50,
            "reward_gold": 15,
        },
        {
            "id": "faith_4_the_wounded_stranger",
            "title": "The Wounded Stranger",
            "flavor": (
                "Someone's collapsed at the roadside, hurt and unable to pay for the help they clearly need "
                "right now."
            ),
            "min_level": 5,
            "trigger": {
                "type": "alignment_choice",
                "setup": (
                    "They can't pay, and they're plainly not going to be able to for a while. Healing them "
                    "costs you real time and real supplies either way."
                ),
                "choices": {
                    "heal_freely": {
                        "label": "heal them, no charge, no questions",
                        "reward_xp": 60,
                        "reward_gold": 0,
                        "alignment_law_chaos_delta": 0,
                        "alignment_good_evil_delta": 10,
                        "outcome": "They walk away whole. You never learn their name, and it doesn't matter.",
                    },
                    "name_a_price": {
                        "label": "name a fair price and hold to it",
                        "reward_xp": 60,
                        "reward_gold": 30,
                        "alignment_law_chaos_delta": 5,
                        "alignment_good_evil_delta": 0,
                        "outcome": "They pay what they can, on the terms you set, and both of you keep your word.",
                    },
                },
            },
            "reward_xp": 0,
            "reward_gold": 0,
        },
    ],
    "forge_guild": [
        {
            "id": "forge_1_first_ore",
            "title": "First Ore",
            "flavor": "A smith who can't source their own iron is at someone else's mercy. Gather iron ore, yourself.",
            "min_level": 5,
            "trigger": {"type": "gather_material", "material": "iron_ore", "count": 5},
            "reward_xp": 50,
            "reward_gold": 20,
            "reward_mastery_profession": "blacksmithing",
            "reward_mastery_pct": 2.0,
        },
        {
            "id": "forge_2_borins_word",
            "title": "Borin's Word",
            "flavor": (
                "Borin Ironjaw is a stern, no-nonsense dwarf doing penance for an old failure he's never once "
                "explained. Ask him, directly, about the forge."
            ),
            "min_level": 5,
            "trigger": {"type": "npc_dialogue", "npc": "borin_ironjaw", "keywords": ["forge"]},
            "reward_xp": 45,
            "reward_gold": 15,
        },
        {
            "id": "forge_3_proving_the_steel",
            "title": "Proving the Steel",
            "flavor": (
                "A smith's work means nothing untested. Take whatever you've forged into a real fight and "
                "prove it against the Goblin Warrens' own boss."
            ),
            "min_level": 7,
            "trigger": {"type": "defeat_monster", "monster": "goblin_boss", "count": 1},
            "reward_xp": 80,
            "reward_gold": 35,
            "reward_mastery_profession": "blacksmithing",
            "reward_mastery_pct": 3.0,
        },
        {
            "id": "forge_4_temper_the_steel",
            "title": "Temper the Steel",
            "flavor": (
                "The last real lesson is nerve, not metal. Say \"try my luck\" here — a smith who flinches "
                "at the wrong moment ruins the blade."
            ),
            "min_level": 8,
            "trigger": {"type": "dice_challenge", "threshold": DICE_CHALLENGE_DEFAULT_THRESHOLD},
            "reward_xp": 60,
            "reward_gold": 30,
            "reward_mastery_profession": "blacksmithing",
            "reward_mastery_pct": 2.0,
        },
    ],
    "enchanters_guild": [
        {
            "id": "ench_1_sulfur",
            "title": "Raw Catalyst",
            "flavor": "Real enchanting starts with a real catalyst, not a spell. Gather sulfur dust yourself.",
            "min_level": 5,
            "trigger": {"type": "gather_material", "material": "sulfur_dust", "count": 5},
            "reward_xp": 50,
            "reward_gold": 20,
            "reward_mastery_profession": "enchanting",
            "reward_mastery_pct": 2.0,
        },
        {
            "id": "ench_2_the_binders_riddle",
            "title": "The Binder's Riddle",
            "flavor": (
                "Every apprentice hears this one first, in the Guild's own topic: \"I am not alive, but I "
                "grow; I don't have lungs, but I need air; I don't have a mouth, but water kills me. What am I?\""
            ),
            "min_level": 5,
            "trigger": {"type": "solve_puzzle", "accepted_answers": ["fire", "a fire", "flame"]},
            "reward_xp": 55,
            "reward_gold": 0,
        },
        {
            "id": "ench_3_the_glow",
            "title": "The Glow Itself",
            "flavor": "Glimmerdeep Grotto is where real arcane residue actually pools. Go see it, and gather it, yourself.",
            "min_level": 6,
            "trigger": {"type": "reach_location", "location": "glimmerdeep_grotto"},
            "reward_xp": 45,
            "reward_gold": 15,
        },
        {
            "id": "ench_4_bind_or_release",
            "title": "Bind or Release",
            "flavor": (
                "Something faint and half-aware is caught in what you're working on — not quite a spirit, not "
                "quite nothing. The Guild teaches binding. It never said you had to use it here."
            ),
            "min_level": 7,
            "trigger": {
                "type": "alignment_choice",
                "setup": (
                    "It's barely more than a flicker of real awareness, bound loosely into the residue you're "
                    "refining — easy enough to bind fully into the work, or just as easy to let go."
                ),
                "choices": {
                    "bind_it": {
                        "label": "bind it fully into the enchantment",
                        "reward_xp": 70,
                        "reward_gold": 20,
                        "alignment_law_chaos_delta": -5,
                        "alignment_good_evil_delta": -10,
                        "outcome": "The work takes on a real, sharper edge. Whatever that flicker was, it isn't anymore.",
                        "reward_mastery_profession": "enchanting",
                        "reward_mastery_pct": 3.0,
                    },
                    "release_it": {
                        "label": "let it go, and finish the work without it",
                        "reward_xp": 70,
                        "reward_gold": 5,
                        "alignment_law_chaos_delta": 0,
                        "alignment_good_evil_delta": 10,
                        "outcome": "It slips free and fades. The work is plainer for it, and that's fine.",
                        "reward_mastery_profession": "enchanting",
                        "reward_mastery_pct": 1.0,
                    },
                },
            },
            "reward_xp": 0,
            "reward_gold": 0,
        },
    ],
}


def get_curriculum(guild_id: str) -> list[dict]:
    return GUILD_CURRICULUM.get(guild_id, [])


def get_step(guild_id: str, step_index: int) -> dict | None:
    curriculum = get_curriculum(guild_id)
    if 0 <= step_index < len(curriculum):
        return curriculum[step_index]
    return None


def is_curriculum_complete(guild_id: str, step_index: int) -> bool:
    return step_index >= len(get_curriculum(guild_id))
