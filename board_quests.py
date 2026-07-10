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


def get_or_generate_board_quests(campaign_data: dict, location_id: str) -> list[dict]:
    """Today's full board for this location — tops up to DAILY_BOARD_QUEST_COUNT if under."""
    existing = get_todays_board_quests(location_id)
    avoid = {(q["objective_type"], q["objective_target"]) for q in existing}
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
    if board_quest.get("completed_at"):
        return f"📜 **{board_quest['title']}** — completed today, nothing posted right now."
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
