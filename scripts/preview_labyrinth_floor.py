#!/usr/bin/env python3
"""
scripts/preview_labyrinth_floor.py

Phase L4, item 3 of the Zelda-dungeon-topology work: a dev-only review
tool ("the editor to train it," per Coffee's own framing) — generates a
real Labyrinth floor (or a full 5-floor segment) via the EXACT same
real functions the live game calls (rules.labyrinth.generate_floor /
generate_segment), zero synthetic data, then:

  1. Renders the real map via map_render.render_labyrinth_map (fog-of-
     war off — a full reveal, since this is for reviewing generation
     quality, not simulating play) to a local PNG.
  2. Prints a readable text dump of the real room graph: every room's
     name, its real connections/locked_connections/warps/collapsing_
     connections, any hazard/monster/miniboss/chest/switch/pillar/hint
     it holds, and which rooms gate or are gated by what.

Use this to generate a batch of seeds, look at the maps + graphs, and
tune generation constants (_WARP_CHANCE, _BRANCH_DEPTH_WEIGHTS,
_MINIBOSS_CHANCE, _COLLAPSE_PUZZLE_CHANCE, _CARRY_PUZZLE_CHANCE,
_BRANCH_GATE_CHANCE, _PRESSURE_PLATE_CHANCE, _BREAKABLE_CHANCE,
_LOOP_BACK_MAX_EDGES) in rules/labyrinth.py in response to real
feedback instead of guessing.

Usage:
    python3 scripts/preview_labyrinth_floor.py --seed 7 --floor 12
    python3 scripts/preview_labyrinth_floor.py --seed 7 --floor 11 --segment
    python3 scripts/preview_labyrinth_floor.py --seed 7 --floor 12 --out /tmp/floor12.png
"""
import argparse
import random
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

import campaign_loader as cl  # noqa: E402
import config  # noqa: E402
import map_render  # noqa: E402
import rules.labyrinth as labyrinth  # noqa: E402


def _describe_room(room_id: str, room: dict) -> list[str]:
    tags = []
    if room.get("is_miniboss_room"):
        tags.append("MINIBOSS")
    if room.get("monsters"):
        tags.append(f"monsters={room['monsters']}")
    if room.get("hazard"):
        tags.append(f"hazard={room['hazard']}")
    if room.get("mirror_twin"):
        tags.append(f"mirror_twin={room['mirror_twin']}")
    for lockable in room.get("lockables", []):
        kind = lockable["kind"]
        extra = ""
        if lockable.get("element"):
            extra = f"({lockable['element']})"
        elif lockable.get("puzzle_id"):
            extra = f"(puzzle={lockable['puzzle_id']})"
        elif lockable.get("requires"):
            extra = f"(requires={lockable['requires']})"
        tags.append(f"lockable:{lockable['id']}={kind}{extra}")
    if room.get("warps"):
        tags.append(f"warps->{room['warps']}")
    if room.get("collapsing_connections"):
        tags.append(f"collapsing->{room['collapsing_connections']}")
    if room.get("locked_connections"):
        tags.append(f"locked->{room['locked_connections']}")
    lines = [f"{room_id:24s} {room['name']!r:45s} conn={room.get('connections')}"]
    if tags:
        lines.append(f"{'':24s} {' '.join(tags)}")
    return lines


def dump_rooms(rooms: dict, hub_id: str) -> None:
    print(f"Room count: {len(rooms)}")
    print(f"Hub: {hub_id}")
    print("-" * 100)
    for room_id, room in sorted(rooms.items()):
        for line in _describe_room(room_id, room):
            print(line)
    print("-" * 100)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--seed", type=int, required=True)
    parser.add_argument("--floor", type=int, required=True)
    parser.add_argument("--segment", action="store_true", help="generate the whole 5-floor segment this floor belongs to, not just one floor")
    parser.add_argument("--out", default=None, help="output PNG path (default: /tmp/labyrinth_preview_seed{seed}_floor{floor}.png)")
    args = parser.parse_args()

    campaign = cl.load_campaign(config.ACTIVE_CAMPAIGN)
    rng = random.Random(args.seed)

    if args.segment:
        segment_number = labyrinth.segment_number_for_floor(args.floor)
        seg = labyrinth.generate_segment(campaign, segment_number, rng)
        all_rooms = seg["rooms"]
        segment_floors = list(range(labyrinth.segment_start_floor(segment_number), labyrinth.segment_start_floor(segment_number) + labyrinth.SEGMENT_SIZE))
        target_floor = segment_floors[0]
        floor_rooms = {rid: r for rid, r in all_rooms.items() if r.get("floor") == target_floor}
        hub_id = seg["entry_room_id"]
        print(f"Segment {segment_number} (floors {segment_floors}), theme via seed {args.seed}")
        dump_rooms(all_rooms, hub_id)
        png = map_render.render_labyrinth_map(target_floor, floor_rooms, hub_id, set(), set(floor_rooms.keys()), {}, segment_floors)
    else:
        result = labyrinth.generate_floor(campaign, args.floor, rng)
        floor_rooms = result["rooms"]
        hub_id = result["hub_room_id"]
        print(f"Floor {args.floor}, seed {args.seed}")
        dump_rooms(floor_rooms, hub_id)
        png = map_render.render_labyrinth_map(args.floor, floor_rooms, hub_id, set(), set(floor_rooms.keys()), {}, None)

    out_path = args.out or f"/tmp/labyrinth_preview_seed{args.seed}_floor{args.floor}.png"
    with open(out_path, "wb") as f:
        f.write(png)
    print(f"Wrote map: {out_path}")


if __name__ == "__main__":
    main()
