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

# Leave-guild permanent stat growth (2026-08-13, per Coffee: "add a
# leave guild feature - also for the guilds and you add a + to stat
# that levels up the players stat directly. u can decide what stat to
# use per guild. when they leave they keep that bonus because it is
# now permanently on the player but they do not keep the other
# bonuses" -- then, on how fast it should grow: "you can decide how
# many points they get per levels for that"). Each guild's
# "permanent_stat" ability score gains +1, real and permanent, for
# every GUILD_STAT_BONUS_LEVELS levels gained WHILE a member (applied
# incrementally as the character actually levels up -- see db.add_xp
# -- not all at once on join or on leave). Roughly one real ASI's
# worth (+4) across a full 1-20 membership, deliberately modest next
# to ASI's own +1/+2 choices at levels 4/8/12/16/19, not a replacement
# for them. Once earned it's just an ordinary ability-score point,
# indistinguishable from any other source -- leaving the guild clears
# membership and every OTHER benefit (shop discount, curriculum,
# guild-specific combat bonuses) but never reverts stat points already
# applied, which is the entire point: something real and permanent to
# show for having belonged, even after leaving.
GUILD_STAT_BONUS_LEVELS = 5

# Real, immediate Promotion payoff (2026-08-13, per Coffee: "promotions
# shud increase % so players can eventually work on maxing out %") --
# on top of the gradual GUILD_STAT_BONUS_LEVELS growth, the moment a
# Promotion into another guild actually happens (see db.join_guild's
# secondary-join branch) grants this flat, one-time % bump to that
# guild's own profession/scalar-proficiency fields, same 100%-capped
# ceiling as everything else in this system.
GUILD_PROMOTION_PCT_BONUS = 10.0

# Real, permanent profession-mastery growth (2026-08-13, per Coffee:
# "add a stat to the proficiencies too that level up when the player
# levels up that is also permanant, each guild shud have one, make
# sure all proficiencies are covered by this system and all stats are
# also" -- then, moving Cooking off Thieves' Guild per his correction
# below: "find somewhere else for cooking so its included"). Same
# GUILD_STAT_BONUS_LEVELS cadence as permanent_stat, +1% profession_
# mastery_pct (bot.py's PROFICIENCY_MAX_PCT-capped 0-100 scale) per
# tier instead of +1 to an ability score. Each entry is a LIST (not a
# single string) since Adventurers' Guild now covers two -- bot.py's
# ALL_PROFESSIONS has exactly 7 (alchemy, cooking, blacksmithing,
# herbalism, mining, fishing, lumberjacking), and with Thieves' Guild
# now covering none (its own real identity lives entirely in
# GUILD_PERMANENT_SCALAR_PROFICIENCY below instead), Cooking needed a
# new home -- Adventurers' Guild ("a loose, practical association open
# to anyone") is the most honest fit for a second ordinary trade.
GUILD_PERMANENT_PROFESSION = {
    "adventurers_guild": ["fishing", "cooking"],
    "arcane_circle": ["alchemy"],
    "silver_wardens": ["lumberjacking"],
    "thieves_guild": [],
    "faith_circle": ["herbalism"],
    "forge_guild": ["blacksmithing"],
    "enchanters_guild": ["mining"],
}

# Real, direct scalar proficiency growth (2026-08-13, per Coffee: "i
# dont think thieves guild shud be cooking - it shud lock picking, and
# or stealing"). Thieves' Guild's own real identity, grown the same
# +1%-per-GUILD_STAT_BONUS_LEVELS way as a profession above, but
# pointed at real DIRECT scalar fields instead of the profession_
# mastery_pct dict: bot.py's steal_proficiency_pct (already real, the
# same field THIEVES_GUILD_STEAL_BONUS stacks with) and lockpick_
# proficiency_pct (new, 2026-08-13, wired into bot._do_lockpick the
# same way). backstab_proficiency_pct is deliberately NOT listed here
# even though it's a third real Thieves'-flavored field -- it only
# ever applies to a real Assassin subclass (bot.py's own "Only ever
# called for a real Assassin" rule), so it's granted conditionally in
# db.add_xp instead of unconditionally here, per Coffee: "also for
# backstacking (if the player has it, otherwise dont show that one)".
# Empty for every other guild -- their own identity is already the
# ability score + profession pair above.
GUILD_PERMANENT_SCALAR_PROFICIENCY = {
    "thieves_guild": ["steal_proficiency_pct", "lockpick_proficiency_pct"],
}

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
        "permanent_stat": "constitution",
    },
    "arcane_circle": {
        "name": "The Arcane Circle",
        "description": "A secretive order of spellcasters who trade knowledge for loyalty.",
        "join_requirement_level": 3,
        "join_requirement_classes": ["wizard", "sorcerer", "warlock"],
        "benefits": ["bonus_spell_scroll", "shop_discount_10", "bonus_spell_damage_15"],
        "permanent_stat": "intelligence",
    },
    "silver_wardens": {
        "name": "The Silver Wardens",
        "description": "Monster hunters who've noticed the world's darkening edges and organized against it.",
        "join_requirement_level": 5,
        "join_requirement_classes": ["fighter", "paladin", "barbarian", "ranger", "monk"],
        "benefits": ["bonus_damage_vs_undead", "shop_discount_15"],
        "permanent_stat": "strength",
    },
    "thieves_guild": {
        "name": "The Thieves' Guild",
        "description": "A quiet network of pickpockets, fences, and people who ask too few questions.",
        "join_requirement_level": 3,
        "join_requirement_classes": ["rogue", "bard"],
        "benefits": ["bonus_steal", "shop_discount_10"],
        "permanent_stat": "dexterity",
    },
    "faith_circle": {
        "name": "The Faith Circle",
        "description": "Healers and keepers of living things, sworn to mend what the world keeps breaking.",
        "join_requirement_level": 3,
        "join_requirement_classes": ["cleric", "druid"],
        "benefits": ["bonus_healing", "shop_discount_10"],
        "permanent_stat": "wisdom",
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
        "permanent_stat": "strength",
    },
    "enchanters_guild": {
        "name": "The Enchanters' Guild",
        "description": "Artificers who bind real magic into ring and blade rather than casting it themselves.",
        "join_requirement_level": 5,
        "join_requirement_classes": ["wizard", "sorcerer", "warlock"],
        "benefits": ["guild_enchant_ladder", "shop_discount_10"],
        "permanent_stat": "charisma",
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


def held_guild_ids(character: dict) -> list[str]:
    """Every guild this character actually belongs to, primary first."""
    ids = []
    if character.get("guild"):
        ids.append(character["guild"])
    ids.extend(character.get("secondary_guilds") or [])
    return ids


def has_mastered_all_held_guilds(character: dict) -> bool:
    """True only once every currently-held guild's curriculum is fully complete."""
    import guild_curriculum
    if character.get("guild"):
        if not guild_curriculum.is_curriculum_complete(character["guild"], character["guild_curriculum_step"]):
            return False
    secondary_steps = character.get("secondary_guild_curriculum_steps") or {}
    for gid in character.get("secondary_guilds") or []:
        if not guild_curriculum.is_curriculum_complete(gid, secondary_steps.get(gid, 0)):
            return False
    return True


def guild_slots_unlocked(character: dict) -> int:
    """
    Evolution-gated guild slots (2026-08-13, per Coffee: "each evolution
    grants them another guild"). The first guild is free at rebirth 0;
    every rebirth after that ("evolving") unlocks exactly one more --
    real guilds can genuinely be held at once ("doubled up"), not
    swapped one-for-one. Capped at len(GUILDS) (currently 7, per
    Coffee: "what shud be the max?" -- there are only 7 real guilds
    that exist, so a slot beyond that could never be filled by
    anything real; further rebirths past the cap don't unlock a hollow
    extra slot).
    """
    return min(1 + max(character.get("rebirth_count", 0), 0), len(GUILDS))


def eligible_for_guild(character: dict, guild_id: str) -> tuple[bool, str]:
    """
    Real eligibility check, now covering (2026-07-25, per Coffee):
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

    Membership used to be a strict one-guild-ever commitment (2026-07-25).
    Revised 2026-08-13, per Coffee: a character can still only ever hold
    ONE guild by default, but each real evolution (rebirth) unlocks one
    more guild slot ("each evolution grants them another guild... guilds
    CAN be doubled up") -- gated on having actually mastered (fully
    completed the real curriculum of) every guild currently held first
    ("they must have mastered the previous guilds mastery tree"), so a
    second/third guild is earned, not free. Leaving a guild (see
    db.leave_guild) always frees its slot back up regardless of any of
    this.
    """
    guild = GUILDS.get(guild_id)
    if guild is None:
        return False, "That guild doesn't exist."
    held = held_guild_ids(character)
    if guild_id in held:
        return False, "You're already a member."
    if held:
        held_names = ", ".join(GUILDS[gid]["name"] for gid in held if gid in GUILDS)
        if len(held) >= guild_slots_unlocked(character):
            # Real live bug (2026-08-13, Coffee, dev-bridge screenshot:
            # "This is not true. I'm currently eligible and I'm not in
            # any other guild" -- confirmed live against the real
            # database: Coffee's CURRENTLY ACTIVE character at that
            # moment (Ravenloft, not the character they were actually
            # thinking of) really does already hold The Adventurers'
            # Guild with 0 rebirths -- 1 slot unlocked, 1 slot used,
            # mathematically correct -- but the rejection never named
            # WHICH guild, so there was no way to tell "you're mistaken
            # about your own state" from "this is broken." Same class of
            # fix as the Forge Guild subclass hint just above: name the
            # concrete fact instead of a bare generic rejection.
            return False, (
                f"You're already in {held_names} — you've earned every Promotion your "
                "evolutions allow so far. Rebirth again to become eligible for another."
            )
        if not has_mastered_all_held_guilds(character):
            return False, (
                f"You're in {held_names}, but not eligible for a Promotion yet -- true mastery "
                "of your current guild's full training curriculum comes first."
            )
    if character["level"] < guild["join_requirement_level"]:
        return False, f"Requires level {guild['join_requirement_level']}."
    required_classes = guild.get("join_requirement_classes")
    if required_classes:
        char_class = character["char_class"].lower()
        hybrid_class = (character.get("hybrid_class") or "").lower()
        if char_class not in required_classes and hybrid_class not in required_classes:
            return False, f"Only open to: {', '.join(c.capitalize() for c in required_classes)}."
        if not character.get("subclass"):
            # Real live bug (2026-08-13, Coffee: "It is not letting me
            # join the Forge guild and it is not being clear on how I
            # can join" -- confirmed live, a Fighter stuck retrying
            # "join the forge guild"/"join the path of the forge guild"
            # forever). The old generic hint here told the player to
            # say "choose the path of..." without ever naming what goes
            # after "of" -- the real options only ever appeared in a
            # DIFFERENT command's own no-match message
            # (_do_choose_subclass in bot.py), which the player had no
            # reason to invoke separately. Naming the real options for
            # their own class right here, in the message that actually
            # gets shown, closes that loop.
            from rules.leveling import CLASS_SUBCLASSES, describe_subclass_effect
            options = CLASS_SUBCLASSES.get(character["char_class"])
            if options:
                # 2026-08-14, per Coffee: naming the two options (2026-08-13's
                # own fix) still wasn't enough -- a player choosing blind
                # between two bare names has no way to know what either
                # actually DOES until after committing. describe_subclass_
                # effect surfaces the same real numbers bot._do_choose_
                # subclass's post-choice message already reveals, one step
                # earlier, so the choice is actually informed.
                return False, (
                    "Requires choosing a subclass first — say \"choose the path of "
                    f"{options[0]}\" ({describe_subclass_effect(options[0])}) or "
                    f"\"choose the path of {options[1]}\" ({describe_subclass_effect(options[1])})."
                )
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


# Combined titles for a "doubled up" multi-guild character (2026-08-13,
# per Coffee: "find a way to make cool title names for that so it
# doesnt sound repeated. Use the story for them"). One evocative,
# story-grounded epithet per guild -- drawn straight from each guild's
# own real description above, never invented from scratch -- strung
# together into a single hyphenated title (primary guild's epithet
# last, so it reads like the character's oldest, foundational identity
# anchors the newer ones) rather than a flat "member of X and Y" list.
GUILD_TITLE_EPITHET = {
    "adventurers_guild": "Wayfarer",
    "arcane_circle": "Arcanist",
    "silver_wardens": "Warden",
    "thieves_guild": "Shade",
    "faith_circle": "Anointed",
    "forge_guild": "Ironbound",
    "enchanters_guild": "Artificer",
}


def guild_title(character: dict) -> str | None:
    """
    None for no guild, the guild's own real name for exactly one, or a
    combined epithet title (secondary guilds newest-first, primary
    last) once a character has genuinely doubled up. A real
    promotion_rank_title (once actually evolved) prefixes the whole
    thing, e.g. "Legend Ironbound-Warden" for a rank-8, two-guild
    character -- one combined identity, not two separate labels.
    """
    held = held_guild_ids(character)
    if not held:
        return None
    if len(held) == 1:
        guild = GUILDS.get(held[0])
        base = guild["name"] if guild else held[0]
    else:
        ordered = list(reversed(held))  # newest secondary first, primary anchors the end
        epithets = [GUILD_TITLE_EPITHET.get(gid, GUILDS.get(gid, {}).get("name", gid)) for gid in ordered]
        base = "-".join(epithets)
    rank_title = promotion_rank_title(character)
    return f"{rank_title} {base}" if rank_title else base


# Promotion rank titles (2026-08-13, per Coffee: "make a rank for 10
# (something epic matching story ascending to god) dont spoil" --
# following his own "what shud be the max?"/my answer that real guild
# slots cap at len(GUILDS) (7), with a purely cosmetic prestige track
# for evolving further). Rank N = rebirth_count N-1 (rank 1 is the
# unranked baseline, never shown); ranks 2-7 track the same real guild-
# slot unlocks guild_slots_unlocked already grants, ranks 8-10 are pure
# prestige beyond the last real guild slot -- escalating, deliberately
# generic power-fantasy language (never a specific named character,
# place, or plot beat from this game's own real story) so a genuinely
# "ascending to god" top rank never spoils anything real underneath it.
PROMOTION_RANK_TITLES = [
    "Wanderer", "Initiate", "Adept", "Veteran", "Champion",
    "Master", "Grandmaster", "Legend", "Mythic", "Ascendant",
]


def promotion_rank(character: dict) -> int:
    """1-10, from real evolutions (rebirth_count) -- see PROMOTION_RANK_TITLES."""
    return min(max(character.get("rebirth_count", 0), 0) + 1, len(PROMOTION_RANK_TITLES))


def promotion_rank_title(character: dict) -> str | None:
    """None at the unranked baseline (rank 1, never evolved) -- a real title only once a character has actually evolved."""
    rank = promotion_rank(character)
    return PROMOTION_RANK_TITLES[rank - 1] if rank > 1 else None


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
