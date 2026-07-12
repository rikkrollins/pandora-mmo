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

DEFEAT_BOUNTY_TITLES = [
    "Thin the {plural}",
    "Clear out the {plural}",
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
    return name + "s"


def _day_key(now: datetime | None = None) -> str:
    return (now or datetime.now(timezone.utc)).strftime("%Y-%m-%d")


def _generate_for_location(campaign_data: dict, location_id: str, avoid: set[tuple[str, str]]) -> dict | None:
    """
    `avoid` is a set of (objective_type, objective_target) already posted
    today at this location, so a second/third generated quest doesn't
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

    if kind == "defeat":
        monster_key = random.choice(monster_options)
        monster_data = campaign_data["monsters"][monster_key]
        count = random.randint(2, 4)
        title = random.choice(DEFEAT_BOUNTY_TITLES).format(name=monster_data["name"], plural=_plural(monster_data["name"]))
        description = f"Defeat {count}x {monster_data['name']} at {location['name']}."
        reward_xp = max(monster_data.get("xp_reward", 50) * count // 2, 10)
        reward_gold = 10 * count
        return db.create_board_quest(
            location_id, _day_key(), title, description, giver_npc,
            "defeat_monster", monster_key, count, reward_xp, reward_gold,
        )

    node = random.choice(node_options)
    material_id = node["material"]
    material_data = items_module.get_item(material_id) or {"name": material_id.replace("_", " ").title(), "price": 5}
    count = random.randint(3, 5)
    title = random.choice(GATHER_BOUNTY_TITLES).format(name=material_data["name"])
    description = f"Gather {count}x {material_data['name']} at {location['name']}."
    reward_xp = 30 * count
    reward_gold = max(material_data.get("price", 5), 1) * count
    return db.create_board_quest(
        location_id, _day_key(), title, description, giver_npc,
        "gather_material", material_id, count, reward_xp, reward_gold,
    )


def _faction_for_npc(campaign_data: dict, npc_id: str | None) -> str | None:
    if not npc_id:
        return None
    for faction_id, faction_data in campaign_data.get("factions", {}).items():
        if npc_id in faction_data.get("member_npcs", []):
            return faction_id
    return None


def _generate_branching_quest_for_location(campaign_data: dict, location_id: str,
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
        title = f"A Quiet Word About the {subject_name}"
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
        location_id, _day_key(), title, description, giver_npc_id,
        objective_type, objective_target, count, base_xp, base_gold,
    )
    db.set_board_quest_branch_data(board_quest["board_quest_id"], branch_data)
    board_quest["branch_data"] = branch_data
    return board_quest


def get_or_generate_board_quests(campaign_data: dict, location_id: str) -> list[dict]:
    """
    Today's full board for this location — tops up to
    DAILY_BOARD_QUEST_COUNT if under. The first quest generated for a
    location each day is always attempted as a branching (moral-choice)
    quest; the rest are simple bounties, for a mix of both.
    """
    existing = get_todays_board_quests(location_id)
    avoid = {(q["objective_type"], q["objective_target"]) for q in existing}

    if not existing:
        branching = _generate_branching_quest_for_location(campaign_data, location_id, avoid)
        if branching:
            existing.append(branching)
            avoid.add((branching["objective_type"], branching["objective_target"]))

    while len(existing) < DAILY_BOARD_QUEST_COUNT:
        new_quest = _generate_for_location(campaign_data, location_id, avoid)
        if new_quest is None:
            break
        existing.append(new_quest)
        avoid.add((new_quest["objective_type"], new_quest["objective_target"]))
    return existing


def get_todays_board_quests(location_id: str) -> list[dict]:
    """Existing board quests for this location today, if any (never creates one)."""
    return db.get_active_board_quests(location_id, _day_key())


# Backward-compatible singular helpers (first listing only).
def get_or_generate_board_quest(campaign_data: dict, location_id: str) -> dict | None:
    quests = get_or_generate_board_quests(campaign_data, location_id)
    return quests[0] if quests else None


def get_todays_board_quest(location_id: str) -> dict | None:
    quests = get_todays_board_quests(location_id)
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
        progress = f"{board_quest['progress_count']}/{board_quest['objective_count']}"
        return (
            f"📜 **{board_quest['title']}** ({progress}) — already accepted by a party, "
            f"expires within 24h if not finished."
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
