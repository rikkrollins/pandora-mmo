"""
campaign_loader.py
Loads every campaign JSON found under campaigns/<name>/campaign.json.
Adding a brand-new campaign to the game means dropping a new folder +
JSON file here — no code changes required. The currently active
campaign is chosen by config.ACTIVE_CAMPAIGN (defaults to "default").
"""
import json
import os

CAMPAIGNS_DIR = os.path.join(os.path.dirname(__file__), "campaigns")

_LOADED_CAMPAIGNS: dict[str, dict] = {}


def discover_campaigns() -> list[str]:
    """Return the list of campaign IDs found on disk (folder names under campaigns/)."""
    if not os.path.isdir(CAMPAIGNS_DIR):
        return []
    return sorted(
        name for name in os.listdir(CAMPAIGNS_DIR)
        if os.path.isfile(os.path.join(CAMPAIGNS_DIR, name, "campaign.json"))
    )


def load_campaign(campaign_id: str) -> dict:
    """Load (and cache) a single campaign's JSON data by folder name."""
    if campaign_id in _LOADED_CAMPAIGNS:
        return _LOADED_CAMPAIGNS[campaign_id]

    path = os.path.join(CAMPAIGNS_DIR, campaign_id, "campaign.json")
    if not os.path.isfile(path):
        raise FileNotFoundError(f"No campaign.json found for campaign '{campaign_id}' at {path}")

    with open(path, "r", encoding="utf-8") as f:
        data = json.load(f)

    _LOADED_CAMPAIGNS[campaign_id] = data
    return data


def get_location(campaign_data: dict, location_id: str) -> dict | None:
    for layer_locations in campaign_data["locations"].values():
        if location_id in layer_locations:
            return {**layer_locations[location_id], "id": location_id}
    return None


def get_all_location_ids(campaign_data: dict) -> list[str]:
    ids = []
    for layer_locations in campaign_data["locations"].values():
        ids.extend(layer_locations.keys())
    return ids


def get_shop(campaign_data: dict, shop_id: str) -> dict | None:
    return campaign_data.get("shops", {}).get(shop_id)


def get_quest(campaign_data: dict, quest_id: str) -> dict | None:
    return campaign_data.get("quests", {}).get(quest_id)


def get_monster_template(campaign_data: dict, monster_id: str) -> dict | None:
    return campaign_data.get("monsters", {}).get(monster_id)


def get_npc(campaign_data: dict, npc_id: str) -> dict | None:
    return campaign_data.get("npcs", {}).get(npc_id)
