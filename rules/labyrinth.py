"""
rules/labyrinth.py

The Labyrinth (2026-09-01, Phase L1, per Coffee: "start the labyrinth
architecture" -- a live, procedurally-generated, ever-deepening
dungeon unlocked by defeating colosseum_champion, run by bot.py at
request time, never authored offline).

Deliberately NOT rules/dungeon_evolve.py's heavier pipeline: no grid
coordinates (a Labyrinth floor is never part of the real minimap --
same already-accepted scope as map_render.py's own deferred z-axis
work), no build_location_grid/dungeon_audit retry loop (this needs to
run in well under a second on every descent, not retry against 7
structural checks meant for permanent, hand-polishable content).
Reuses only dungeon_evolve._candidate_monsters, for a wide,
non-Remnant, non-headline-boss monster pool -- floor depth is
unbounded, so unlike evolve_dungeon's finite next-chapter-band lookup,
monster CHOICE here stays catalog-agnostic; ALL of the difficulty
comes from a multiplicative scale bot.py's own _build_labyrinth_enemy
applies to whichever real monster template gets picked (same
stat_mult-on-a-template shape bot.py's _build_echo_enemy already
proves safe), never baked into this module's own room data.

Room ids are fixed constants, NOT floor-namespaced -- only one floor's
rooms are ever live in a run's `rooms_json` at a time (db.py's
labyrinth_runs table), the previous floor discarded wholesale on
descend. A real, deliberate "no backtracking across floors" roguelike
constraint, not an oversight.
"""
import random

from rules.dungeon_evolve import _candidate_monsters

HUB_ROOM_ID = "laby_hub"
STAIRS_ROOM_ID = "laby_stairs"
_SIDE_ROOM_COUNT_RANGE = (3, 5)


def _labyrinth_monster_pool(campaign: dict) -> list[str]:
    """Wide, catalog-agnostic pool -- floor depth is unbounded, so monster CHOICE never depends on a level band (that's what bot.py's depth-multiplier is for); only real exclusions (Remnants, level-less headline bosses) apply, reusing dungeon_evolve's own filtering with the widest possible band."""
    return _candidate_monsters(campaign, (0, 10_000), boss=False)


def generate_floor(campaign: dict, floor: int, rng: random.Random) -> dict:
    """
    Returns {"rooms": {room_id: {...}}, "hub_room_id": HUB_ROOM_ID,
    "stairs_room_id": STAIRS_ROOM_ID}. No forced-clear gating -- the
    stairs down are always reachable from the hub immediately, per
    Coffee's explicit direction that depth must never be gated by
    strength, only "willing and able to travel."
    """
    pool = _labyrinth_monster_pool(campaign)
    rooms: dict[str, dict] = {}

    hub = {
        "id": HUB_ROOM_ID, "name": f"Labyrinth -- Floor {floor}",
        "description": "The paths here shift with every descent -- nothing about this floor existed a moment before you arrived.",
        "connections": [STAIRS_ROOM_ID], "monsters": [],
    }
    rooms[HUB_ROOM_ID] = hub

    stairs = {
        "id": STAIRS_ROOM_ID, "name": "A Stair Down",
        "description": "A real stairway, always open -- however far you've come, the way deeper never asks permission.",
        "connections": [HUB_ROOM_ID], "monsters": [],
    }
    rooms[STAIRS_ROOM_ID] = stairs

    num_side_rooms = rng.randint(*_SIDE_ROOM_COUNT_RANGE)
    for i in range(num_side_rooms):
        room_id = f"laby_r{i}"
        monsters = rng.sample(pool, k=min(rng.randint(0, 2), len(pool))) if pool else []
        room = {
            "id": room_id, "name": f"Labyrinth -- Chamber {i + 1}",
            "description": "A real chamber, same shape as the last, except for what's waiting in it.",
            "connections": [HUB_ROOM_ID], "monsters": monsters,
        }
        if rng.random() < 0.3:
            room.setdefault("lockables", []).append({
                "id": f"laby_cache_{i}", "kind": "chest", "name": "a real, hastily-buried cache",
                "loot": {"healing_potion": rng.randint(1, 2)}, "gold": rng.randint(20, 80) * floor,
            })
        hub["connections"].append(room_id)
        rooms[room_id] = room

    return {"rooms": rooms, "hub_room_id": HUB_ROOM_ID, "stairs_room_id": STAIRS_ROOM_ID}
