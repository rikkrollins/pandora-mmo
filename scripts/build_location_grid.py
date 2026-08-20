#!/usr/bin/env python3
"""
scripts/build_location_grid.py

Real live report (2026-08-20, Coffee, dev-bridge screenshot of a 7x6
grid): "make sure all the north south east and west locations on all
the different layers are correct. I don't want any locations
conflicting and I want every square to be its proper location."

Investigation found a real "directions" field already exists per
location (a prior 2026-07-19 world-expansion pass), covering 73/82
locations -- but a full reciprocity check (every A.direction=B must
have B.opposite(direction)=A) turned up dozens of real conflicts and
gaps across all three layers, confirming Coffee's report.

`connections` (100% complete, every location has it) is the one
authoritative reachability list -- `directions` is a compass LABEL
layered over it that can drift out of sync, which is exactly what
happened. This script re-derives a fully self-consistent `directions`
+ `grid_position` for every location, per layer, from `connections`
alone via BFS, using the OLD directions data only as a same-strength
hint when it doesn't conflict with anything already placed. A location
with more than 4 lateral (non up/down) neighbors can't fit a strict
compass grid -- its extra edge(s) are reported, not silently dropped;
they stay reachable via plain `connections`/free-text movement, just
without a compass label.

Usage:
    python3 scripts/build_location_grid.py             # dry run, report only
    python3 scripts/build_location_grid.py --apply      # write campaign.json
"""
import json
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
CAMPAIGN_PATH = REPO_ROOT / "campaigns" / "default" / "campaign.json"

OPPOSITE = {"north": "south", "south": "north", "east": "west", "west": "east", "up": "down", "down": "up"}
LATERAL_ORDER = ["north", "east", "south", "west"]
DELTA = {"north": (0, 1), "south": (0, -1), "east": (1, 0), "west": (-1, 0)}

# One deliberate root per layer -- the real, narrative "front door" of
# that layer, so BFS grows outward from a sensible (0, 0) rather than
# an arbitrary dict-iteration-order node.
LAYER_ROOTS = {
    "surface": "crossroads_tavern",
    "underground": "sunken_root_caverns",
    "sky": "the_unmoored_isle",
}

# Real, narratively-obvious vertical relationships with NO existing
# `directions` hint to derive them from (confirmed 2026-08-20: a dry
# run without this table treated tavern_cellar/tavern_upstairs as two
# more of crossroads_tavern's LATERAL neighbors, overflowing its real
# 4-slot compass budget even though a cellar/upstairs is obviously not
# a cardinal direction). Hand-reviewed, not a naming heuristic -- keep
# this table short and only add an entry once a real overflow note
# confirms it's needed.
VERTICAL_OVERRIDES: dict[str, dict[str, str]] = {
    "crossroads_tavern": {"tavern_upstairs": "up", "tavern_cellar": "down"},
}


def _bfs_from_root(
    layer_name: str, places: dict, root: str, old_hints: dict, new_directions: dict, notes: list,
) -> dict[str, tuple[int, int]]:
    """
    Walks BFS outward from `root`, mutating `new_directions` in place and
    returning {loc_id: (x, y)} for every node reached from `root` via
    `connections`. Real, narratively separate islands within the SAME
    layer (e.g. a sub-dungeon only reachable from another layer via
    `descends_to`, never a same-layer `connections` edge -- confirmed
    real, documented behavior in map_render.py's own module docstring,
    not a bug) are never reached by this walk; the caller runs it again
    per leftover component and offsets the result.
    """
    coords: dict[str, tuple[int, int]] = {root: (0, 0)}
    occupied: dict[tuple[int, int], str] = {(0, 0): root}

    queue = [root]
    head = 0
    while head < len(queue):
        current = queue[head]
        head += 1
        cx, cy = coords[current]
        # Only neighbors that are actually part of THIS call's own node
        # set -- a real connection back into an already-placed main
        # component (or forward into a different island this call
        # doesn't own) is real data, just not this call's to place;
        # skipping it here is what lets the per-island re-run below
        # work at all without a KeyError on `places[current]`.
        neighbors = [n for n in places[current]["connections"] if n in places]

        # Up/down first -- a separate vertical axis. A vertical
        # neighbor shares the SAME (x, y) as its parent (a different
        # floor of the same map cell, matching how the renderer's own
        # floor badges already display it), never competes for a
        # lateral grid slot, and -- critically -- still gets placed
        # into `coords` and enqueued so BFS actually walks through it;
        # an earlier draft wired the direction label here but never
        # placed/enqueued the neighbor, which silently orphaned entire
        # sub-graphs reachable only via a vertical edge (confirmed:
        # this was why most of underground/sky came back "unreachable"
        # in the very first dry run).
        vertical_here: dict[str, str] = {}
        for direction in ("up", "down"):
            hinted = old_hints[current].get(direction)
            if hinted in neighbors and direction not in new_directions[current]:
                opp = OPPOSITE[direction]
                if new_directions.get(hinted, {}).get(opp) in (None, current):
                    vertical_here[hinted] = direction
        for neighbor_id, direction in VERTICAL_OVERRIDES.get(current, {}).items():
            if neighbor_id in neighbors and direction not in new_directions[current] and neighbor_id not in vertical_here:
                vertical_here[neighbor_id] = direction

        for neighbor, direction in vertical_here.items():
            opp = OPPOSITE[direction]
            new_directions[current][direction] = neighbor
            new_directions.setdefault(neighbor, {})[opp] = current
            if neighbor not in coords:
                coords[neighbor] = (cx, cy)
                queue.append(neighbor)

        lateral_neighbors = [
            n for n in neighbors
            if n != new_directions[current].get("up") and n != new_directions[current].get("down")
        ]

        for neighbor in lateral_neighbors:
            if neighbor in coords:
                continue  # already placed (or will be reached via someone else's edge)
            # Prefer the OLD hint's direction if it's free on both ends
            # and lands on an unoccupied (or same-neighbor) cell. Checks
            # BOTH sides -- current's own forward hint ("current.south =
            # neighbor"), and, if current never declared one, the
            # NEIGHBOR's own real reverse hint ("neighbor.west =
            # current", implying current should be neighbor's east).
            # Real bug found live (2026-08-20): crossroads_tavern had NO
            # old `directions` at all, so whispering_wood's own real,
            # unconflicting "west: crossroads_tavern" was silently
            # ignored when placing it from the OTHER side, landing it
            # "north" instead and breaking "go south" from
            # whispering_wood (a live-tested, previously-working
            # relationship) -- caught by tests.test_regression's own
            # test_compass_direction_resolves_to_the_right_location.
            hinted = None
            for word, dest in old_hints[current].items():
                if dest == neighbor and word in DELTA:
                    hinted = word
                    break
            if hinted is None:
                for word, dest in old_hints.get(neighbor, {}).items():
                    if dest == current and word in DELTA:
                        hinted = OPPOSITE[word]
                        break
            candidates = [hinted] + [w for w in LATERAL_ORDER if w != hinted] if hinted else list(LATERAL_ORDER)
            placed = False
            for word in candidates:
                if word not in DELTA or word in new_directions[current]:
                    continue
                dx, dy = DELTA[word]
                target = (cx + dx, cy + dy)
                if target in occupied and occupied[target] != neighbor:
                    continue
                coords[neighbor] = target
                occupied[target] = neighbor
                new_directions[current][word] = neighbor
                new_directions.setdefault(neighbor, {})[OPPOSITE[word]] = current
                queue.append(neighbor)
                placed = True
                break
            if not placed:
                notes.append(
                    f"[{layer_name}] {current} <-> {neighbor}: no free cardinal slot on either end "
                    f"(more than 4 lateral neighbors) -- kept reachable via connections only, no compass label."
                )

    return coords


def build_layer(layer_name: str, places: dict) -> tuple[dict, list[str]]:
    """Returns (results, notes) where results[loc_id] = {"grid_position": {...}, "directions": {...}}."""
    root = LAYER_ROOTS[layer_name]
    assert root in places, f"{layer_name}'s configured root {root!r} isn't a real location"

    old_hints: dict[str, dict[str, str]] = {lid: dict(info.get("directions") or {}) for lid, info in places.items()}
    new_directions: dict[str, dict[str, str]] = {lid: {} for lid in places}
    notes: list[str] = []

    coords = _bfs_from_root(layer_name, places, root, old_hints, new_directions, notes)

    # Real, separate islands within this layer (reached only via
    # `descends_to`/another layer, never a same-layer `connections`
    # edge -- documented, deliberate campaign design, not a data bug,
    # e.g. the Wordless Choir sub-dungeon under the_first_city). Each
    # gets its OWN local BFS from its own lowest-sorted member, then is
    # shifted to sit two rows below whatever's already been placed, so
    # it renders as a clearly separate annex rather than overlapping or
    # being silently dropped.
    remaining = {lid for lid in places if lid not in coords}
    while remaining:
        adjacency: dict[str, list[str]] = {lid: [] for lid in remaining}
        for lid in remaining:
            for conn in places[lid]["connections"]:
                if conn in remaining:
                    adjacency[lid].append(conn)
        island_root = min(remaining)
        component: set[str] = set()
        stack = [island_root]
        while stack:
            nid = stack.pop()
            if nid in component:
                continue
            component.add(nid)
            stack.extend(adjacency[nid])

        island_places = {lid: places[lid] for lid in component}
        island_coords = _bfs_from_root(layer_name, island_places, island_root, old_hints, new_directions, notes)

        placed_max_y = max((y for _, y in coords.values()), default=0)
        island_min_y = min((y for _, y in island_coords.values()), default=0)
        y_shift = (placed_max_y - island_min_y) - 2  # 2 empty rows of separation
        for lid, (x, y) in island_coords.items():
            coords[lid] = (x, y + y_shift)

        remaining -= component

    unreached = [lid for lid in places if lid not in coords]
    for lid in unreached:
        notes.append(f"[{layer_name}] {lid}: genuinely disconnected -- no connections path to anything else in this layer.")

    results = {}
    for lid in places:
        entry = {"directions": new_directions.get(lid, {})}
        if lid in coords:
            x, y = coords[lid]
            entry["grid_position"] = {"x": x, "y": y}
        results[lid] = entry
    return results, notes


def main() -> None:
    apply = "--apply" in sys.argv
    campaign = json.loads(CAMPAIGN_PATH.read_text())
    all_notes = []
    diff_count = 0
    for layer_name, places in campaign["locations"].items():
        results, notes = build_layer(layer_name, places)
        all_notes.extend(notes)
        for lid, entry in results.items():
            old_dirs = places[lid].get("directions") or {}
            if old_dirs != entry["directions"]:
                diff_count += 1
            if apply:
                places[lid]["directions"] = entry["directions"]
                if "grid_position" in entry:
                    places[lid]["grid_position"] = entry["grid_position"]

    print(f"{diff_count} location(s) have a changed/filled-in directions dict.")
    if all_notes:
        print(f"\n{len(all_notes)} note(s):")
        for note in all_notes:
            print(" -", note)

    if apply:
        CAMPAIGN_PATH.write_text(json.dumps(campaign, indent=2, ensure_ascii=False) + "\n")
        print(f"\nWrote {CAMPAIGN_PATH}")
    else:
        print("\nDry run only -- pass --apply to write campaign.json.")


if __name__ == "__main__":
    main()
