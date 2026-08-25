"""
board_quests.py
The area quest board: a repeatable, generated bounty tied to a
LOCATION rather than to any one character — distinct from the
hand-authored story quests in campaign.json's "quests" catalog, which
stay on a character's personal journal (active_quests/completed_quests).

Up to DAILY_BOARD_QUEST_COUNT board quests are generated per location
per real day (only for locations with a monster or a resource node to
build one from — some places just don't have anything to post), each
distinct where possible so there's real variety, not the same bounty
twice. Anyone at that location can accept any of them; each expires
24h after acceptance if not finished, returning to the board for
someone else to try. Titles/descriptions are built from a curated
template pool and real campaign data, never invented by an AI — same
"rules decide" rule as everything else in this game.
"""
import random
from datetime import datetime, timezone

import campaign_loader as cl
import db
import items as items_module
from ai.dm_agent import narrate_branching_quest_setup

DAILY_BOARD_QUEST_COUNT = 2

# Weekly/monthly missions (2026-07-25, per Coffee: "daily, weekly,
# monthly missions separate from story-line... worked into the
# evolution system"). Bigger objective counts and flat reward
# multipliers than a daily bounty -- real XP reward on top of that
# already scales with rebirth for free via db.add_xp's existing
# xp_gain_multiplier, so a heavily-evolved character's weekly/monthly
# payout is bigger automatically, no extra plumbing needed here.
# Deliberately simple bounties only for these 2 tiers (never a
# branching moral-choice quest, which stays a daily-only flourish) --
# scope kept to what the ask actually needed.
BOARD_QUEST_TIER_COUNT = {"daily": DAILY_BOARD_QUEST_COUNT, "weekly": 1, "monthly": 1}
BOARD_QUEST_TIER_COUNT_RANGE = {
    "daily": {"defeat": (2, 4), "gather": (3, 5)},
    "weekly": {"defeat": (10, 16), "gather": (15, 25)},
    "monthly": {"defeat": (40, 60), "gather": (60, 100)},
}
BOARD_QUEST_TIER_REWARD_MULT = {"daily": 1, "weekly": 6, "monthly": 25}

DEFEAT_BOUNTY_TITLES = [
    "Thin {plural}",
    "Clear out {plural}",
    "A {name} problem",
    "Trouble with {plural}",
]
GATHER_BOUNTY_TITLES = [
    "Gather {name}",
    "{name} needed",
    "A supply run for {name}",
    "Short on {name}",
]

# Just enough to cover this campaign's monster catalog correctly (Wolf ->
# Wolves) without pulling in a real inflection library for one edge case.
_IRREGULAR_PLURALS = {"wolf": "wolves"}


def _plural(name: str) -> str:
    if name.lower() in _IRREGULAR_PLURALS:
        irregular = _IRREGULAR_PLURALS[name.lower()]
        return irregular.capitalize() if name[:1].isupper() else irregular
    if name.lower().endswith("f"):
        return name[:-1] + "ves"
    # Real dev-bridge report (2026-08-25, Coffee's party, screenshot): a
    # board quest title read "A Quiet Word About The The Root
    # Rememberss" -- a literal doubled "s" from blindly appending "s"
    # to a name that already ends in one ("The Root That Remembers").
    # Real English pluralization: a word already ending in s/x/z/ch/sh
    # takes "es", not another bare "s" (goblin_boss -> "Goblin Bosses",
    # not "Goblin Bosss") -- the only other monster name in this
    # campaign's whole bestiary that ends in "s".
    if name.lower().endswith(("s", "x", "z", "ch", "sh")):
        return name + "es"
    return name + "s"


def _the(name: str) -> str:
    """
    Prefixes "the " unless the name already carries its own article --
    most real bosses/Remnants do ("The Root That Remembers", "The
    Waiting Shape", ...; 11 of this campaign's 12 Remnants start with
    "The"). Same live bug this fixes as _plural's own doubled-"s" case
    (2026-08-25 dev-bridge report): every title template that used to
    hardcode a literal "the " right before a monster-name placeholder
    doubled it into "the The X" whenever that placeholder held one of
    these names.
    """
    return name if name.lower().startswith("the ") else f"the {name}"


def _day_key(now: datetime | None = None) -> str:
    return (now or datetime.now(timezone.utc)).strftime("%Y-%m-%d")


def _period_key(tier: str, now: datetime | None = None) -> str:
    """
    The generation-period key for a given tier -- reuses day_key's
    column for ALL 3 tiers (its format already differs per tier, e.g.
    "2026-07-25" vs "2026-W30" vs "2026-07", so there's no real
    collision risk sharing one column).
    """
    now = now or datetime.now(timezone.utc)
    if tier == "weekly":
        iso_year, iso_week, _ = now.isocalendar()
        return f"{iso_year}-W{iso_week:02d}"
    if tier == "monthly":
        return now.strftime("%Y-%m")
    return _day_key(now)


def _generate_for_location(
    campaign_data: dict, location_id: str, chat_id: int, avoid: set[tuple[str, str]], tier: str = "daily",
) -> dict | None:
    """
    `avoid` is a set of (objective_type, objective_target) already posted
    this period at this location, so a second/third generated quest doesn't
    just repeat the same bounty — best-effort variety, not a hard
    guarantee (falls back to a repeat if that's genuinely all there is).
    """
    location = cl.get_location(campaign_data, location_id)
    if not location:
        return None

    monsters = location.get("monsters", [])
    nodes = location.get("resource_nodes", [])

    monster_options = [m for m in monsters if ("defeat_monster", m) not in avoid] or list(monsters)
    node_options = [n for n in nodes if ("gather_material", n["material"]) not in avoid] or list(nodes)

    options = []
    if monster_options:
        options.append("defeat")
    if node_options:
        options.append("gather")
    if not options:
        return None

    giver_npc = (location.get("npcs") or [None])[0]
    kind = random.choice(options)
    count_range = BOARD_QUEST_TIER_COUNT_RANGE.get(tier, BOARD_QUEST_TIER_COUNT_RANGE["daily"])
    reward_mult = BOARD_QUEST_TIER_REWARD_MULT.get(tier, 1)
    tier_label = "" if tier == "daily" else f"[{tier.capitalize()}] "

    if kind == "defeat":
        monster_key = random.choice(monster_options)
        monster_data = campaign_data["monsters"][monster_key]
        count = random.randint(*count_range["defeat"])
        # _the() on the plural (2026-08-25 dev-bridge report): this
        # generator, unlike Companion Favors, never excludes boss-tier
        # monsters -- "Thin {plural}"/"Clear out {plural}" used to
        # hardcode their own "the " right before the placeholder, which
        # doubled into "the The X" for any boss/Remnant whose name
        # already starts with "The" (11 of 12 do).
        title = tier_label + random.choice(DEFEAT_BOUNTY_TITLES).format(
            name=monster_data["name"], plural=_the(_plural(monster_data["name"])),
        )
        description = f"Defeat {count}x {monster_data['name']} at {location['name']}."
        reward_xp = max(monster_data.get("xp_reward", 50) * count // 2, 10) * reward_mult
        reward_gold = 10 * count * reward_mult
        return db.create_board_quest(
            location_id, chat_id, _period_key(tier), title, description, giver_npc,
            "defeat_monster", monster_key, count, reward_xp, reward_gold, tier=tier,
        )

    node = random.choice(node_options)
    material_id = node["material"]
    material_data = items_module.get_item(material_id) or {"name": material_id.replace("_", " ").title(), "price": 5}
    count = random.randint(*count_range["gather"])
    title = tier_label + random.choice(GATHER_BOUNTY_TITLES).format(name=material_data["name"])
    description = f"Gather {count}x {material_data['name']} at {location['name']}."
    reward_xp = 30 * count * reward_mult
    reward_gold = max(material_data.get("price", 5), 1) * count * reward_mult
    return db.create_board_quest(
        location_id, chat_id, _period_key(tier), title, description, giver_npc,
        "gather_material", material_id, count, reward_xp, reward_gold, tier=tier,
    )


# Companion Favors (2026-08-23, per Coffee: "affinity menu... task
# based quests that are easy and attainable"). Deliberately smaller
# than even a daily board quest's own COUNT_RANGE -- this is meant to
# be a quick, repeatable trust-builder, not a real bounty. Reward is
# affinity only (Coffee's explicit call, confirmed via AskUserQuestion)
# -- no gold/XP, unlike every other board quest, so this screen's
# purpose stays singular.
COMPANION_FAVOR_COUNT_RANGE = {"defeat": (1, 2), "gather": (2, 3)}
COMPANION_FAVOR_REWARD_AFFINITY = 5
COMPANION_FAVOR_LOCATION_PREFIX = "companion_favor:"

FAVOR_DEFEAT_TITLES = [
    "A favor: deal with {plural}",
    "Could use a hand with {plural}",
]
FAVOR_GATHER_TITLES = [
    "A favor: bring some {name}",
    "Could use a hand gathering {name}",
]


def _recruit_location_for_npc(campaign_data: dict, npc_id: str) -> dict | None:
    """
    Companion favors need a real monster/resource-node pool to draw
    from, same as a location board quest, but campaign.json's npc
    entries don't store their own location directly -- reverse-looked-
    up the same way _npc_id_for_companion_name in bot.py already does
    the opposite direction (name -> npc_id).
    """
    for location_id in cl.get_all_location_ids(campaign_data):
        location = cl.get_location(campaign_data, location_id)
        if location and npc_id in (location.get("npcs") or []):
            return location
    return None


def _non_boss_monster_keys(campaign_data: dict, location: dict) -> list[str]:
    """Companion Favors stay "easy and attainable" (per Coffee's explicit request) -- never assigns a real boss-tier monster as the defeat target."""
    return [
        m for m in location.get("monsters", [])
        if not (campaign_data["monsters"].get(m) or {}).get("is_boss")
    ]


def get_or_generate_companion_favor(campaign_data: dict, npc_id: str, chat_id: int) -> dict | None:
    """
    One active (accepted or not, not yet completed) small favor per
    recruited companion at a time -- generates a fresh one once the
    current one's done, same daily-period-key regen cadence a real
    board quest already has, so an abandoned one doesn't block forever.
    Returns None only if this campaign genuinely has nothing to build
    an objective from anywhere (never expected in practice, but no
    location falls back gracefully instead of raising).
    """
    location_id = COMPANION_FAVOR_LOCATION_PREFIX + npc_id
    existing = db.get_active_board_quests(location_id, chat_id, _day_key(), tier="daily")
    active = [q for q in existing if not q.get("completed_at")]
    if active:
        return active[0]

    location = _recruit_location_for_npc(campaign_data, npc_id)

    def _has_favor_content(loc: dict | None) -> bool:
        return bool(loc) and bool(_non_boss_monster_keys(campaign_data, loc) or loc.get("resource_nodes"))

    # Falls back to any location with real non-boss monsters/resource
    # nodes if this companion's own recruit spot has neither (or
    # couldn't be found) -- generation should never silently produce
    # nothing just because one specific location happens to be thin on
    # content, or its only monster there is a real boss.
    candidate_locations = [location] if location else []
    if not _has_favor_content(location):
        for loc_id in cl.get_all_location_ids(campaign_data):
            loc = cl.get_location(campaign_data, loc_id)
            if _has_favor_content(loc):
                candidate_locations.append(loc)
                break

    monster_options, node_options = [], []
    for loc in candidate_locations:
        if loc:
            monster_options = monster_options or _non_boss_monster_keys(campaign_data, loc)
            node_options = node_options or loc.get("resource_nodes", [])

    options = []
    if monster_options:
        options.append("defeat")
    if node_options:
        options.append("gather")
    if not options:
        return None

    kind = random.choice(options)
    if kind == "defeat":
        monster_key = random.choice(monster_options)
        monster_data = campaign_data["monsters"][monster_key]
        count = random.randint(*COMPANION_FAVOR_COUNT_RANGE["defeat"])
        # _the() (2026-08-25 dev-bridge report, same root cause as
        # _generate_for_location's own fix): not currently reachable in
        # practice since _non_boss_monster_keys already excludes every
        # "The X"-named boss/Remnant from candidates here, but fixed for
        # consistency/defense-in-depth in case that exclusion ever loosens.
        title = random.choice(FAVOR_DEFEAT_TITLES).format(plural=_the(_plural(monster_data["name"])))
        description = f"Could use a hand dealing with {count}x {monster_data['name']}."
        return db.create_board_quest(
            location_id, chat_id, _day_key(), title, description, npc_id,
            "defeat_monster", monster_key, count, 0, 0, tier="daily",
            reward_affinity=COMPANION_FAVOR_REWARD_AFFINITY,
        )

    node = random.choice(node_options)
    material_id = node["material"]
    material_data = items_module.get_item(material_id) or {"name": material_id.replace("_", " ").title()}
    count = random.randint(*COMPANION_FAVOR_COUNT_RANGE["gather"])
    title = random.choice(FAVOR_GATHER_TITLES).format(name=material_data["name"])
    description = f"Could use {count}x {material_data['name']}, if you happen to come across any."
    return db.create_board_quest(
        location_id, chat_id, _day_key(), title, description, npc_id,
        "gather_material", material_id, count, 0, 0, tier="daily",
        reward_affinity=COMPANION_FAVOR_REWARD_AFFINITY,
    )


def _faction_for_npc(campaign_data: dict, npc_id: str | None) -> str | None:
    if not npc_id:
        return None
    for faction_id, faction_data in campaign_data.get("factions", {}).items():
        if npc_id in faction_data.get("member_npcs", []):
            return faction_id
    return None


def _generate_branching_quest_for_location(campaign_data: dict, location_id: str, chat_id: int,
                                            avoid: set[tuple[str, str]]) -> dict | None:
    """
    A moral-choice bounty: same real objective mechanic as a simple
    bounty (defeat/gather — reuses the exact same progress tracking),
    but resolving it means picking one of two named, fixed-consequence
    choices instead of an automatic reward. The AI narrates the setup
    and each choice's outcome, grounded strictly in the real objective
    and (if present) a real NPC's real personality — it never invents
    or decides the choices, their rewards, or any faction-standing
    change, all of which are fixed here, in code, before narration ever
    runs.
    """
    location = cl.get_location(campaign_data, location_id)
    if not location:
        return None

    monsters = location.get("monsters", [])
    nodes = location.get("resource_nodes", [])
    monster_options = [m for m in monsters if ("defeat_monster", m) not in avoid] or list(monsters)
    node_options = [n for n in nodes if ("gather_material", n["material"]) not in avoid] or list(nodes)

    options = []
    if monster_options:
        options.append("defeat")
    if node_options:
        options.append("gather")
    if not options:
        return None

    giver_npc_id = (location.get("npcs") or [None])[0]
    giver_npc_data = campaign_data["npcs"].get(giver_npc_id) if giver_npc_id else None
    kind = random.choice(options)

    if kind == "defeat":
        monster_key = random.choice(monster_options)
        monster_data = campaign_data["monsters"][monster_key]
        count = random.randint(1, 2)
        objective_type, objective_target = "defeat_monster", monster_key
        subject_name = monster_data["name"] if count == 1 else _plural(monster_data["name"])
        base_xp = max(monster_data.get("xp_reward", 50) * count, 40)
        base_gold = 20 * count
        objective_facts = (
            f"Deal with {count}x {monster_data['name']} at {location['name']} — but the request came "
            f"with an odd insistence on exactly this target, not just 'whatever's causing trouble.'"
        )
        # Real dev-bridge report (2026-08-25, Coffee's party, screenshot):
        # a board quest title read "A Quiet Word About The The Root
        # Rememberss" -- this "the" collided with a monster name that
        # already starts with its own "The " (most real bosses/Remnants
        # do: The Root That Remembers, The Waiting Shape, etc.), doubling
        # it. _the() never adds a second article when the name already
        # carries one.
        title = f"A Quiet Word About {_the(subject_name)}"
    else:
        node = random.choice(node_options)
        material_id = node["material"]
        material_data = items_module.get_item(material_id) or {"name": material_id.replace("_", " ").title(), "price": 5}
        count = random.randint(2, 3)
        objective_type, objective_target = "gather_material", material_id
        base_xp = 35 * count
        base_gold = max(material_data.get("price", 5), 1) * count
        objective_facts = (
            f"Retrieve {count}x {material_data['name']} from {location['name']} — though whoever wants "
            f"it hasn't said why, and it plainly isn't unclaimed."
        )
        title = f"A Quiet Request for {material_data['name']}"

    description = objective_facts
    npc_name = giver_npc_data["name"] if giver_npc_data else None
    npc_personality = giver_npc_data["personality"] if giver_npc_data else None
    setup_narration = narrate_branching_quest_setup(location["name"], npc_name, npc_personality, objective_facts)

    reward_faction = _faction_for_npc(campaign_data, giver_npc_id)
    branch_data = {
        "archetype": "retrieve_moral_cost",
        "setup_narration": setup_narration,
        "choices": {
            "keep_it": {
                "label": "keep it and collect the reward",
                "reward_xp": base_xp,
                "reward_gold": base_gold,
                "faction_id": reward_faction,
                "faction_delta": 5 if reward_faction else None,
                "outcome_facts": "They accept the reward as offered, no questions asked, and move on.",
            },
            "return_it": {
                "label": "leave it be instead",
                "reward_xp": max(base_xp // 3, 10),
                "reward_gold": 0,
                "faction_id": "the_wanderers_road",
                "faction_delta": 15,
                "outcome_facts": (
                    "They walk away from the reward, choosing to leave things as they found them — "
                    "word of that kind of restraint tends to travel among those who notice it."
                ),
            },
        },
        "resolved_choice": None,
    }

    board_quest = db.create_board_quest(
        location_id, chat_id, _day_key(), title, description, giver_npc_id,
        objective_type, objective_target, count, base_xp, base_gold, tier="daily",
    )
    db.set_board_quest_branch_data(board_quest["board_quest_id"], branch_data)
    board_quest["branch_data"] = branch_data
    return board_quest


def get_or_generate_board_quests(campaign_data: dict, location_id: str, chat_id: int, tier: str = "daily") -> list[dict]:
    """
    This period's ACTIVE board for this location — tops up to
    BOARD_QUEST_TIER_COUNT[tier] if under. Daily's first quest each
    period is always attempted as a branching (moral-choice) quest, the
    rest simple bounties; weekly/monthly are simple bounties only
    (2026-07-25 addition, per Coffee: "daily, weekly, monthly missions
    separate from story-line") -- same real objective/reward mechanic,
    just a longer period and a bigger scale.

    Completed quests are deliberately excluded from both the count and
    the returned list (2026-07-16, per Coffee): they used to count
    toward the period's quota forever, so finishing every quest posted
    for the period permanently occupied every slot with an "already
    completed" placeholder instead of freeing it up for a fresh one,
    and completed bounties kept cluttering the board listing
    indefinitely. `avoid` still considers every quest generated this
    period (including completed ones) so a replacement isn't just a
    repeat of what was already cleared.
    """
    all_this_period = get_todays_board_quests(location_id, chat_id, tier=tier)
    avoid = {(q["objective_type"], q["objective_target"]) for q in all_this_period}
    active = [q for q in all_this_period if not q.get("completed_at")]

    if not all_this_period and tier == "daily":
        branching = _generate_branching_quest_for_location(campaign_data, location_id, chat_id, avoid)
        if branching:
            active.append(branching)
            avoid.add((branching["objective_type"], branching["objective_target"]))

    target_count = BOARD_QUEST_TIER_COUNT.get(tier, DAILY_BOARD_QUEST_COUNT)
    while len(active) < target_count:
        new_quest = _generate_for_location(campaign_data, location_id, chat_id, avoid, tier=tier)
        if new_quest is None:
            break
        active.append(new_quest)
        avoid.add((new_quest["objective_type"], new_quest["objective_target"]))
    return active


def get_todays_board_quests(location_id: str, chat_id: int, tier: str = "daily") -> list[dict]:
    """Existing board quests for this location this period, if any (never creates one)."""
    return db.get_active_board_quests(location_id, chat_id, _period_key(tier), tier=tier)


def get_all_todays_board_quests(location_id: str, chat_id: int) -> list[dict]:
    """All 3 tiers' existing board quests for this location, combined (read-only, never generates)."""
    quests = []
    for tier in ("daily", "weekly", "monthly"):
        quests.extend(get_todays_board_quests(location_id, chat_id, tier=tier))
    return quests


def get_or_generate_all_board_quests(campaign_data: dict, location_id: str, chat_id: int) -> list[dict]:
    """
    All 3 tiers combined (daily + weekly + monthly) for this location,
    in one flat list -- every existing consumer (format_board_listings,
    find_board_quest_by_name, plain "any board quest posted?" checks)
    already operates generically on a list of board_quest dicts, so
    this is a drop-in superset of the old daily-only
    get_or_generate_board_quests everywhere a player actually sees or
    accepts board quests.
    """
    quests = []
    for tier in ("daily", "weekly", "monthly"):
        quests.extend(get_or_generate_board_quests(campaign_data, location_id, chat_id, tier=tier))
    return quests


# Backward-compatible singular helpers (first listing only).
def get_or_generate_board_quest(campaign_data: dict, location_id: str, chat_id: int) -> dict | None:
    quests = get_or_generate_board_quests(campaign_data, location_id, chat_id)
    return quests[0] if quests else None


def get_todays_board_quest(location_id: str, chat_id: int) -> dict | None:
    quests = get_todays_board_quests(location_id, chat_id)
    return quests[0] if quests else None


def format_board_listing(board_quest: dict) -> str:
    branch = board_quest.get("branch_data")

    if board_quest.get("completed_at"):
        if branch and branch.get("resolved_choice"):
            label = branch["choices"][branch["resolved_choice"]]["label"]
            return f"📜 **{board_quest['title']}** — resolved ({label}), nothing posted right now."
        return f"📜 **{board_quest['title']}** — completed today, nothing posted right now."

    if branch:
        setup = branch["setup_narration"]
        if board_quest.get("accepted_by"):
            if board_quest["progress_count"] >= board_quest["objective_count"]:
                choice_lines = "\n".join(f'  • "{c["label"]}"' for c in branch["choices"].values())
                return (
                    f"📜 **{board_quest['title']}**\n{setup}\n\n"
                    f"Ready to decide — say which one:\n{choice_lines}"
                )
            progress = f"{board_quest['progress_count']}/{board_quest['objective_count']}"
            return (
                f"📜 **{board_quest['title']}** ({progress})\n{setup}\n"
                f"Already accepted, expires within 24h if not finished."
            )
        giver_line = (
            f" Ask at: {board_quest['giver_npc'].replace('_', ' ').title()}." if board_quest.get("giver_npc") else ""
        )
        # Confirmed live 2026-07-11: a branching quest's unaccepted board
        # listing showed the setup narration but no reward info at all --
        # a player asked "what's the reward" and there was nothing to
        # tell them, unlike an ordinary bounty's listing a few lines
        # below, which always shows XP/gold. Never reveals outcome_facts
        # (the real narrative consequences) here, only what a "wanted ad"
        # would plausibly advertise: the choices and their rewards.
        choice_rewards = "; ".join(
            f"\"{c['label']}\" ({c['reward_xp']} XP, {c['reward_gold']} gold)"
            for c in branch["choices"].values()
        )
        return f"📜 **{board_quest['title']}**\n{setup}\nPossible rewards: {choice_rewards}.{giver_line}"

    if board_quest.get("accepted_by"):
        # Task #150, 2026-07-17: this used to drop the description
        # entirely once accepted -- the one place a player would look
        # for "what do I actually need to do" went blank right after
        # accepting, exactly when they'd need it most.
        progress = f"{board_quest['progress_count']}/{board_quest['objective_count']}"
        return (
            f"📜 **{board_quest['title']}** ({progress})\n{board_quest['description']}\n"
            f"Already accepted by a party, expires within 24h if not finished."
        )
    giver_line = f" Ask at: {board_quest['giver_npc'].replace('_', ' ').title()}." if board_quest.get("giver_npc") else ""
    return (
        f"📜 **{board_quest['title']}**\n"
        f"{board_quest['description']}\n"
        f"Reward: {board_quest['reward_xp']} XP, {board_quest['reward_gold']} gold.{giver_line}"
    )


def format_board_listings(board_quests: list[dict]) -> str:
    if not board_quests:
        return "Nothing posted here today."
    return "\n\n".join(format_board_listing(q) for q in board_quests)


def find_board_quest_by_name(board_quests: list[dict], text: str) -> dict | None:
    """Fuzzy match a board quest's title against free text, e.g. 'I accept the herb gathering quest'."""
    lowered = text.lower()
    for q in board_quests:
        if q["title"].lower() in lowered:
            return q
    return None
