#!/usr/bin/env python3
"""
scripts/preview_labyrinth_floor.py

Phase L4, item 3 of the action-adventure-dungeon-topology research
work: a dev-only review
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

Usage (raw seed, unchanged since Phase L4):
    python3 scripts/preview_labyrinth_floor.py --seed 7 --floor 12
    python3 scripts/preview_labyrinth_floor.py --seed 7 --floor 11 --segment
    python3 scripts/preview_labyrinth_floor.py --seed 7 --floor 12 --out /tmp/floor12.png

Usage (real logged run, "do both" pass, 2026-09-17): looks up a real
party's real past run in labyrinth_seed_log (see db.log_labyrinth_seed
-- durable, append-only, never deleted, unlike the ephemeral
labyrinth_runs row) instead of requiring you to already know the raw
seed number. A logged seed was always produced via generate_segment
(bot.py only ever calls db.log_labyrinth_seed right after a
generate_segment call), so this path always regenerates the whole
segment, never a lone floor, regardless of --segment:
    python3 scripts/preview_labyrinth_floor.py --chat-id -1001234 --party-key "party_7"
    python3 scripts/preview_labyrinth_floor.py --chat-id -1001234 --party-key "party_7" --floor 33
    python3 scripts/preview_labyrinth_floor.py --chat-id -1001234 --party-key "party_7" --log-segment 6
"""
import argparse
import random
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

import campaign_loader as cl  # noqa: E402
import config  # noqa: E402
import db  # noqa: E402
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


def render_single_floor(campaign: dict, floor: int, seed: int) -> tuple[bytes, dict, str]:
    """Real single-floor path (Phase L4 original), refactored out of main() so it's directly callable/testable, not just CLI-invocable."""
    rng = random.Random(seed)
    result = labyrinth.generate_floor(campaign, floor, rng)
    floor_rooms = result["rooms"]
    hub_id = result["hub_room_id"]
    png = map_render.render_labyrinth_map(floor, floor_rooms, hub_id, set(), set(floor_rooms.keys()), {}, None)
    return png, floor_rooms, hub_id


def render_segment(campaign: dict, segment_number: int, seed: int, target_floor: int | None = None) -> tuple[bytes, dict, str, int]:
    """
    Real whole-segment path (Phase L4 original `--segment`, now also
    the ONLY correct way to reproduce a logged run -- see the "Usage
    (real logged run)" docstring above for why generate_floor alone
    would never match). `target_floor` picks which of the segment's
    SEGMENT_SIZE floors to render/dump; defaults to the segment's own
    first floor. Returns (png_bytes, all_segment_rooms, hub_id,
    resolved_target_floor).
    """
    rng = random.Random(seed)
    seg = labyrinth.generate_segment(campaign, segment_number, rng)
    all_rooms = seg["rooms"]
    segment_floors = list(range(labyrinth.segment_start_floor(segment_number), labyrinth.segment_start_floor(segment_number) + labyrinth.SEGMENT_SIZE))
    resolved_floor = target_floor if target_floor is not None else segment_floors[0]
    floor_rooms = {rid: r for rid, r in all_rooms.items() if r.get("floor") == resolved_floor}
    hub_id = seg["entry_room_id"]
    png = map_render.render_labyrinth_map(resolved_floor, floor_rooms, hub_id, set(), set(floor_rooms.keys()), {}, segment_floors)
    return png, all_rooms, hub_id, resolved_floor


def resolve_logged_run(chat_id: int, party_key: str, segment: int | None = None) -> dict:
    """
    Looks up a real party's real past run in labyrinth_seed_log
    (append-only, never deleted -- db.log_labyrinth_seed's own
    docstring). Most-recent-first; `segment` optionally filters to a
    specific past segment number instead of the latest one on record.
    Raises ValueError (not a silent None) when nothing matches, so a
    typo'd chat_id/party_key fails loudly rather than crashing deeper
    in generation with a confusing error.
    """
    entries = db.get_labyrinth_seed_log(chat_id, party_key, limit=50)
    if segment is not None:
        entries = [e for e in entries if e["segment"] == segment]
    if not entries:
        scope = f" segment={segment}" if segment is not None else ""
        raise ValueError(f"no labyrinth_seed_log entry found for chat_id={chat_id} party_key={party_key!r}{scope}")
    return entries[0]


def preview_from_logged_run(
    chat_id: int, party_key: str, segment: int | None = None, floor: int | None = None, campaign: dict | None = None,
) -> tuple[bytes, dict, str, int, dict]:
    """
    The real end-to-end lookup-and-regenerate path: resolves the real
    logged seed via `resolve_logged_run`, then feeds it into the exact
    same `random.Random(seed)` -> `generate_segment` pipeline the live
    game itself used to create that run in the first place (see
    bot.py's own db.log_labyrinth_seed call site, always immediately
    after a real generate_segment call) -- zero duplication of the
    generation/rendering logic. Returns (png_bytes, all_segment_rooms,
    hub_id, resolved_target_floor, the_matched_log_entry).
    """
    if campaign is None:
        campaign = cl.load_campaign(config.ACTIVE_CAMPAIGN)
    entry = resolve_logged_run(chat_id, party_key, segment)
    png, rooms, hub_id, resolved_floor = render_segment(campaign, entry["segment"], entry["seed"], target_floor=floor)
    return png, rooms, hub_id, resolved_floor, entry


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--seed", type=int, default=None, help="a raw seed number (mutually exclusive with --chat-id/--party-key)")
    parser.add_argument("--floor", type=int, default=None, help="floor to render/dump; required with --seed unless --chat-id/--party-key resolves a segment (then defaults to that segment's first floor)")
    parser.add_argument("--segment", action="store_true", help="with --seed: generate the whole 5-floor segment this floor belongs to, not just one floor (always true, and required, when using --chat-id/--party-key)")
    parser.add_argument("--chat-id", type=int, default=None, help="look up a real logged run instead of a raw --seed (must be given with --party-key)")
    parser.add_argument("--party-key", default=None, help="look up a real logged run instead of a raw --seed (must be given with --chat-id)")
    parser.add_argument("--log-segment", type=int, default=None, help="with --chat-id/--party-key: pick a specific past segment number instead of the most recently logged one")
    parser.add_argument("--out", default=None, help="output PNG path (default: /tmp/labyrinth_preview_seed{seed}_floor{floor}.png)")
    args = parser.parse_args()

    using_log_lookup = args.chat_id is not None or args.party_key is not None
    if using_log_lookup and (args.chat_id is None or args.party_key is None):
        parser.error("--chat-id and --party-key must be given together")
    if using_log_lookup and args.seed is not None:
        parser.error("--seed and --chat-id/--party-key are mutually exclusive")

    campaign = cl.load_campaign(config.ACTIVE_CAMPAIGN)

    if using_log_lookup:
        entry = resolve_logged_run(args.chat_id, args.party_key, args.log_segment)
        print(
            f"Found logged run: segment={entry['segment']} seed={entry['seed']} "
            f"theme={entry['theme']!r} party={entry['party_names']} created_at={entry['created_at']}"
        )
        png, rooms, hub_id, resolved_floor = render_segment(campaign, entry["segment"], entry["seed"], target_floor=args.floor)
        print(f"Segment {entry['segment']}, floor {resolved_floor}, real seed {entry['seed']}")
        dump_rooms(rooms, hub_id)
        seed, floor = entry["seed"], resolved_floor
    else:
        if args.seed is None or args.floor is None:
            parser.error("--seed and --floor are required unless using --chat-id/--party-key")
        if args.segment:
            segment_number = labyrinth.segment_number_for_floor(args.floor)
            png, rooms, hub_id, resolved_floor = render_segment(campaign, segment_number, args.seed, target_floor=args.floor)
            print(f"Segment {segment_number}, floor {resolved_floor}, seed {args.seed}")
            dump_rooms(rooms, hub_id)
        else:
            png, rooms, hub_id = render_single_floor(campaign, args.floor, args.seed)
            print(f"Floor {args.floor}, seed {args.seed}")
            dump_rooms(rooms, hub_id)
        seed, floor = args.seed, args.floor

    out_path = args.out or f"/tmp/labyrinth_preview_seed{seed}_floor{floor}.png"
    with open(out_path, "wb") as f:
        f.write(png)
    print(f"Wrote map: {out_path}")


if __name__ == "__main__":
    main()
