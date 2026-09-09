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

# Real live request (2026-08-27, per Coffee: "make it feel like a
# classic action-adventure dungeon... string paths onto longer pathways" -- after finding that
# EVERY up/down-connected neighbor used to share its parent's exact
# (x, y), piling entire multi-room dungeon delves (Wordless Choir's 8
# rooms, Sunken Root Caverns' branches, etc.) onto ONE map cell,
# distinguished only by a text floor-badge, invisible as a real path.
# Confirmed only ONE real case in this whole campaign is genuinely "the
# same building, a different floor" rather than "a separate room
# reached by a stairway": Crossroads Tavern's own cellar/upstairs.
# Keep this short and hand-reviewed, same discipline as
# VERTICAL_OVERRIDES above -- everything NOT listed here gets a real,
# distinct lateral cell instead (see _bfs_from_root's vertical handling).
SAME_CELL_VERTICAL_PAIRS: dict[str, set[str]] = {
    "crossroads_tavern": {"tavern_upstairs", "tavern_cellar"},
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

        # Up/down first -- a separate vertical axis, but NOT
        # automatically a shared cell anymore (2026-08-27 rework, see
        # SAME_CELL_VERTICAL_PAIRS above). Every real up/down edge from
        # this node is collected here regardless of source (an old
        # `directions` hint, or a hand-reviewed VERTICAL_OVERRIDES
        # entry), still gets placed into `coords` and enqueued so BFS
        # actually walks through it (an earlier draft wired the
        # direction label but never placed/enqueued the neighbor,
        # which silently orphaned entire sub-graphs reachable only via
        # a vertical edge -- confirmed live, this was why most of
        # underground/sky came back "unreachable" in the very first
        # dry run).
        vertical_edges: dict[str, str] = {}
        for direction in ("up", "down"):
            hinted = old_hints[current].get(direction)
            if hinted in neighbors and direction not in new_directions[current]:
                opp = OPPOSITE[direction]
                if new_directions.get(hinted, {}).get(opp) in (None, current):
                    vertical_edges[hinted] = direction
        for neighbor_id, direction in VERTICAL_OVERRIDES.get(current, {}).items():
            if neighbor_id in neighbors and direction not in new_directions[current] and neighbor_id not in vertical_edges:
                vertical_edges[neighbor_id] = direction

        same_cell_pairs_here = SAME_CELL_VERTICAL_PAIRS.get(current, ())
        same_cell_here = {n: d for n, d in vertical_edges.items() if n in same_cell_pairs_here}
        delve_here = {n: d for n, d in vertical_edges.items() if n not in same_cell_pairs_here}

        for neighbor, direction in same_cell_here.items():
            opp = OPPOSITE[direction]
            new_directions[current][direction] = neighbor
            new_directions.setdefault(neighbor, {})[opp] = current
            if neighbor not in coords:
                coords[neighbor] = (cx, cy)
                queue.append(neighbor)

        # Real dungeon delves (2026-08-27, per Coffee: "make it feel
        # like a classic action-adventure dungeon... string paths, not everything piled on
        # one square"). Any up/down edge NOT in SAME_CELL_VERTICAL_PAIRS
        # is a real, separate room -- placed one real cell over, so
        # it's visible on the map as part of a real path instead of
        # stacked invisibly under a floor badge. Prefers the intuitive
        # delta ("down"->south, "up"->north).
        #
        # Real live bug (2026-09-05, Coffee: "double check the world
        # map for collisions... navigation and orientation" -- found by
        # this audit, not yet reported live): this used to fall back
        # through the same LATERAL_ORDER search real lateral neighbors
        # use whenever the preferred cell was already occupied by
        # unrelated content sharing the same layer (confirmed live for
        # Stonearch Bridge/The First City Spire/Sunken Root Caverns'
        # own tower/shaft chains, each running into Whispering Wood's
        # or Greymoor Downs' independently-laid-out rooms). The old
        # comment here claimed relabeling was harmless since "a player
        # never sees raw (x, y)" -- WRONG: tests.test_regression's own
        # test_campaign_grid_positions_agree_with_their_own_directions
        # already encodes the real invariant every OTHER part of this
        # codebase relies on (map_render's own line-drawing between
        # adjacent cells, distance/adjacency logic) -- an "up" edge
        # that actually lands EAST or SOUTH of its origin is a real,
        # visible zigzag/backwards line on the map, exactly the kind of
        # lie a lateral mislabel would also be. Never falls back to a
        # different compass word now -- an occupied preferred cell gets
        # the same honest "kept reachable via connections only, no
        # compass label" treatment the 4-neighbor lateral overflow case
        # already uses just below, which correctly lets a whole blocked
        # sub-chain fall through to build_layer's own island/shelf-
        # packing pass instead (its own separate, conflict-free space).
        vertical_preferred = {"down": "south", "up": "north"}
        for neighbor, direction in delve_here.items():
            if neighbor in coords:
                continue
            preferred = vertical_preferred[direction]
            dx, dy = DELTA[preferred]
            target = (cx + dx, cy + dy)
            if target in occupied and occupied[target] != neighbor:
                notes.append(
                    f"[{layer_name}] {current} <-> {neighbor}: real \"{direction}\" edge, but its own "
                    f"{preferred} cell is already occupied by unrelated content -- kept reachable via "
                    f"connections only, no compass label."
                )
                continue
            coords[neighbor] = target
            occupied[target] = neighbor
            new_directions[current][direction] = neighbor
            new_directions.setdefault(neighbor, {})[OPPOSITE[direction]] = current
            queue.append(neighbor)

        # Excludes every up/down edge above (same-cell AND delve,
        # placed or not) -- a delve edge that lost its preferred slot
        # must stay connections-only, never get silently relabeled to
        # a different compass word by the generic lateral loop below.
        lateral_neighbors = [n for n in neighbors if n not in vertical_edges]

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
    # gets its OWN local BFS from its own lowest-sorted member.
    #
    # Real live bug found 2026-08-27 (this same rework, once delves got
    # real internal depth instead of always sharing one cell): islands
    # used to always stack straight down from a running max, one under
    # the last -- harmless before this rework (every island was a
    # single flat row), but several small islands (2-3 rooms) each
    # stacking with a full "2 empty rows" gap, now that some islands
    # have real depth, blew the canvas height budget the same way a
    # real multi-room dungeon needs room for. Simple shelf-packing
    # instead, using the layer's own existing WIDTH budget rather than
    # stacking everything into one tall column: islands are placed
    # left-to-right along a "shelf" (each 2 columns clear of the last);
    # once a shelf would run wider than the main cluster's own real
    # column count, a new shelf starts 2 rows below the deepest point
    # anything on the previous shelf actually reached.
    remaining = {lid for lid in places if lid not in coords}
    occupied_cells: set[tuple[int, int]] = set(coords.values())
    main_min_x = min((x for x, _ in coords.values()), default=0)
    # Real render limit (map_render.py: _MAX_CANVAS_WIDTH=1800,
    # CELL_SIZE=150, _MARGIN=30 -- (1800 - 30*2) / 150 = 11.8) -- using
    # the layer's own (often much narrower) width here instead would
    # starve shelf-packing of the real room the canvas actually has,
    # which is exactly the bug that made the first version of this fix
    # stack everything into one much-too-tall column.
    shelf_width_budget = 11

    shelf_x = main_min_x  # next free column on the current shelf
    shelf_top_y = min((y for _, y in coords.values()), default=0) - 2  # this shelf's own ceiling (highest row available)
    shelf_bottom_y = shelf_top_y  # deepest row anything on this shelf has reached so far

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

        island_min_x = min((x for x, _ in island_coords.values()), default=0)
        island_max_x = max((x for x, _ in island_coords.values()), default=0)
        island_min_y = min((y for _, y in island_coords.values()), default=0)
        island_max_y = max((y for _, y in island_coords.values()), default=0)
        island_cols = island_max_x - island_min_x + 1

        if shelf_x != main_min_x and (shelf_x - main_min_x) + island_cols > shelf_width_budget:
            # This shelf's full -- start a new one below the deepest
            # point anything already on it reached.
            shelf_x = main_min_x
            shelf_top_y = shelf_bottom_y - 2
            shelf_bottom_y = shelf_top_y

        x_shift = (shelf_x - island_min_x)
        y_shift = (shelf_top_y - 2) - island_max_y
        # Real collision guard (2026-09-05, found by this same session's
        # audit while fixing the delve mislabeling bug above -- which
        # made leftover islands genuinely common for the first time,
        # instead of the rare edge case this shelf-packer was written
        # for): the shelf bookkeeping above is only a bounding-box
        # heuristic against OTHER islands already shelved THIS pass --
        # it has no idea the main component (or an earlier island)
        # might already occupy cells further down some OTHER column
        # this island's own columns also pass through (this layer's
        # real content is full of long, irregular vertical shafts, not
        # a tidy rectangle). Confirmed live: two real, unrelated
        # locations landed on the exact same cell before this guard
        # existed. Slides the whole island straight down, one row at a
        # time, until every one of its real cells is genuinely free --
        # provably collision-free by construction, unlike the blind
        # arithmetic this replaces.
        while any((x + x_shift, y + y_shift) in occupied_cells for x, y in island_coords.values()):
            y_shift -= 1
        for lid, (x, y) in island_coords.items():
            coords[lid] = (x + x_shift, y + y_shift)
            occupied_cells.add((x + x_shift, y + y_shift))
        shelf_bottom_y = min(shelf_bottom_y, island_min_y + y_shift)
        shelf_x += island_cols + 2

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


def _converge_layer(layer_name: str, places: dict, max_iterations: int = 5) -> tuple[dict, list[str]]:
    """
    Real live gap (2026-09-05, found by this session's own map-collision
    audit): build_layer's BFS placement uses each location's EXISTING
    `directions` as a same-strength hint (see its own docstring) --
    which means a single pass doesn't always reach a fully self-
    consistent fixed point starting cold from a campaign.json that
    still carries the OLD, inconsistent hints a past bug wrote.
    Confirmed live: fixing that bug's own real-world fallout took 3
    full passes to fully converge to zero real collisions (each pass's
    freshly-computed directions become the next pass's hints, letting
    BFS explore differently and resolve edges the previous pass
    couldn't). Iterates automatically instead of leaving "run --apply
    several times" as undocumented tribal knowledge -- stops as soon as
    a pass makes no further changes, capped so a genuinely pathological
    layer can't loop forever.
    """
    working = {lid: dict(info) for lid, info in places.items()}
    results, notes = {}, []
    for _ in range(max_iterations):
        results, notes = build_layer(layer_name, working)
        changed = False
        for lid, entry in results.items():
            if working[lid].get("directions") != entry["directions"]:
                changed = True
            working[lid]["directions"] = entry["directions"]
            if "grid_position" in entry:
                working[lid]["grid_position"] = entry["grid_position"]
        if not changed:
            break
    return results, notes


def main() -> None:
    apply = "--apply" in sys.argv
    campaign = json.loads(CAMPAIGN_PATH.read_text())
    all_notes = []
    diff_count = 0
    for layer_name, places in campaign["locations"].items():
        results, notes = _converge_layer(layer_name, places)
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
