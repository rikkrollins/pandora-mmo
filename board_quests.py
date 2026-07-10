"""
board_quests.py
The area quest board: a repeatable, generated bounty tied to a
LOCATION rather than to any one character — distinct from the
hand-authored story quests in campaign.json's "quests" catalog, which
stay on a character's personal journal (active_quests/completed_quests).

One board quest is generated per location per real day (only for
locations with a monster or a resource node to build one from — some
places just don't have anything to post). Anyone at that location can
accept it; it expires 24h after acceptance if not finished, returning
to the board for someone else to try. Titles/descriptions are built
from a curated template pool and real campaign data, never invented by
an AI — same "rules decide" rule as everything else in this game.
"""
import random
from datetime import datetime, timezone

import campaign_loader as cl
import db
import items as items_module

DEFEAT_BOUNTY_TITLES = [
    "Thin the {name}s",
    "Clear out the {name}s",
    "A {name} problem",
    "Trouble with {name}s",
]
GATHER_BOUNTY_TITLES = [
    "Gather {name}",
    "{name} needed",
    "A supply run for {name}",
    "Short on {name}",
]


def _day_key(now: datetime | None = None) -> str:
    return (now or datetime.now(timezone.utc)).strftime("%Y-%m-%d")


def _generate_for_location(campaign_data: dict, location_id: str) -> dict | None:
    location = cl.get_location(campaign_data, location_id)
    if not location:
        return None

    monsters = location.get("monsters", [])
    nodes = location.get("resource_nodes", [])
    options = []
    if monsters:
        options.append("defeat")
    if nodes:
        options.append("gather")
    if not options:
        return None

    giver_npc = (location.get("npcs") or [None])[0]
    kind = random.choice(options)

    if kind == "defeat":
        monster_key = random.choice(monsters)
        monster_data = campaign_data["monsters"][monster_key]
        count = random.randint(2, 4)
        title = random.choice(DEFEAT_BOUNTY_TITLES).format(name=monster_data["name"])
        description = f"Defeat {count}x {monster_data['name']} at {location['name']}."
        reward_xp = max(monster_data.get("xp_reward", 50) * count // 2, 10)
        reward_gold = 10 * count
        return db.create_board_quest(
            location_id, _day_key(), title, description, giver_npc,
            "defeat_monster", monster_key, count, reward_xp, reward_gold,
        )

    node = random.choice(nodes)
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


def get_or_generate_board_quest(campaign_data: dict, location_id: str) -> dict | None:
    """The board listing for this location today — generated fresh once per day per location."""
    existing = get_todays_board_quest(location_id)
    if existing:
        return existing
    return _generate_for_location(campaign_data, location_id)


def get_todays_board_quest(location_id: str) -> dict | None:
    """The existing board quest for this location today, if one's already been generated (never creates one)."""
    return db.get_active_board_quest(location_id, _day_key())


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
