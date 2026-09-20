#!/usr/bin/env python3
"""
scripts/add_loop_back_shortcuts.py

A one-off (but repeatable) dev tool: adds a single, hand-picked safe
loop-back shortcut connection to each of the 7 hand-authored dungeons
that measured 0 real cycles (a pure hub-and-spoke tree, confirmed via
rules.dungeon_evolve._junction_and_cycle_counts) -- deep_root_vault,
goblin_warrens, greymoor_downs, stonearch_bridge, the_first_city,
unmoored_isle, wrathflame_vault. (sunken_root_caverns already has 1
real cycle and is intentionally excluded.)

Each pair below was chosen by hand against the exact same safety
invariant rules.dungeon_evolve.py's own auto-generated loop-back
mechanic already guarantees for evolved dungeons
(_rooms_shadowed_by_a_real_gate + the grid-adjacency/candidate-search
shape of _add_loop_back_connections): neither room is the hub/
entrance, both are reachable from the hub via a BFS over plain
connections only (so anything behind a real lock, or only reachable
via an external dungeon's own lock, is excluded), neither is a
collapsing_connections destination, both have a real grid_position,
the two rooms are cardinal-grid-adjacent (so the new edge renders as a
real doorway on the map, not an invisible "+1" badge), and the pair
isn't already connected. Every candidate was independently re-verified
by directly running rules.dungeon_evolve._rooms_shadowed_by_a_real_gate
and the same candidate-search loop _add_loop_back_connections uses,
not just eyeballed from the raw graph.

Dry-run by default, matching scripts/evolve_dungeon.py's own
convention -- inspect the printed audit results and cycle counts
before ever touching the real file.

Usage:
    python3 scripts/add_loop_back_shortcuts.py
    python3 scripts/add_loop_back_shortcuts.py --apply
"""
import argparse
import json
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
CAMPAIGN_PATH = REPO_ROOT / "campaigns" / "default" / "campaign.json"

import sys  # noqa: E402
sys.path.insert(0, str(REPO_ROOT))

from rules import dungeon_audit  # noqa: E402

# (dungeon_id, room_a_id, room_b_id) -- see this file's own docstring
# for the exact safety invariant each pair was verified against.
SHORTCUTS = [
    ("deep_root_vault", "deep_root_vault_choking_hollow", "deep_root_vault_root_bound_alcove"),
    ("goblin_warrens", "goblin_warrens_the_second_stash", "goblin_warrens_grasks_old_cage"),
    ("greymoor_downs", "greymoor_downs_the_hollows_lee", "greymoor_downs_the_markers_shadow"),
    ("stonearch_bridge", "stonearch_bridge_the_lower_battlement", "stonearch_bridge_the_armory_annex"),
    ("the_first_city", "the_first_city_sealed_vault_row", "the_first_city_spire_overlook"),
    ("unmoored_isle", "unmoored_isle_the_backward_hall", "unmoored_isle_the_warming_hollow"),
    ("wrathflame_vault", "wrathflame_vault_bellows_chamber", "wrathflame_vault_sealed_reliquary"),
]


def _real_cycles_from_hub(rooms: dict, hub_id: str) -> int:
    """
    rules.dungeon_evolve._junction_and_cycle_counts's own formula
    (`len(edges) - (len(rooms) - 1)`) assumes the WHOLE dungeon_id-
    tagged room set is one connected component -- true for every
    generator-evolved dungeon by construction, but NOT true for at
    least 3 of these 7 hand-authored ones (stonearch_bridge,
    greymoor_downs, the_first_city all tag a real sub-cluster that's
    only reachable via a lock -- sometimes from a DIFFERENT dungeon
    entirely -- as part of the same dungeon_id). Applied to a
    disconnected room set, that formula silently underestimates (or
    clamps to 0) even after a real edge is added, since it expects
    `len(rooms) - 1` edges to span everything when the true minimum
    for N disconnected pieces is `len(rooms) - N`. Restricting to the
    subgraph actually reachable from the hub via plain connections
    (the same real, walkable-without-any-key set the reused safety
    logic itself computes) fixes this -- that subset is guaranteed
    connected by construction, so the plain spanning-tree formula is
    exactly correct there. Confirmed directly (2026-09-20): all 7 real
    shortcuts below measure 0 -> 1 this way; 3 of them misleadingly
    read 0 -> 0 under the naive whole-dungeon formula.
    """
    reach = {hub_id}
    frontier = [hub_id]
    while frontier:
        cur = frontier.pop()
        for nb in rooms[cur].get("connections", []):
            if nb in rooms and nb not in reach:
                reach.add(nb)
                frontier.append(nb)
    edges = set()
    for rid in reach:
        for nb in rooms[rid].get("connections", []):
            if nb in reach:
                edges.add(frozenset((rid, nb)))
    return len(edges) - (len(reach) - 1)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--apply", action="store_true", help="write the result back to campaign.json (default: dry-run, report only)")
    args = parser.parse_args()

    campaign = json.loads(CAMPAIGN_PATH.read_text())

    for dungeon_id, a_id, b_id in SHORTCUTS:
        rooms = dungeon_audit._dungeon_rooms(campaign, dungeon_id)
        hub_id = max(rooms, key=lambda rid: len(rooms[rid].get("connections", [])))
        cycles_before = _real_cycles_from_hub(rooms, hub_id)
        dungeon_audit.connect(campaign, a_id, b_id)
        rooms = dungeon_audit._dungeon_rooms(campaign, dungeon_id)
        cycles_after = _real_cycles_from_hub(rooms, hub_id)
        print(f"{dungeon_id}: {a_id} <-> {b_id}")
        print(f"  real cycles (hub-reachable subgraph): {cycles_before} -> {cycles_after}")
        checks = dungeon_audit.audit_dungeon(campaign, dungeon_id)
        for check_name, failures in checks.items():
            print(f"  [{'FAIL' if failures else ' OK '}] {check_name}")
            for f in failures:
                print(f"         - {f}")
        print()

    if not args.apply:
        print("Dry run only -- no file written. Re-run with --apply once you're happy with all 7 rolls.")
        return

    CAMPAIGN_PATH.write_text(json.dumps(campaign, indent=2) + "\n")
    print(f"Written to {CAMPAIGN_PATH}")


if __name__ == "__main__":
    main()
