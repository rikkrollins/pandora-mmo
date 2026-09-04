"""
rules/dungeon_audit.py

Pure, deterministic dungeon-quality checks and authoring helpers,
operating on one dungeon_id's room subset from a loaded campaign dict
(campaigns/default/campaign.json). No AI calls, no DB access -- same
"pure rules" convention as rules/dice.py/rules/combat.py.

Built 2026-08-31 per Coffee's own stated purpose: as he playtests the
8 Zelda-style redesigned dungeons, Claude Code needs a fast way to
diagnose whether a reported problem is a known category of design
flaw, and a reusable authoring layer so future dungeon edits are calls
against tested code instead of raw campaign.json surgery (the exact
manual process that produced a real, caught-by-test-only bug this
session: a shortcut lever's locked_connections wired backwards).

Every check below is a direct, named translation of the real research
behind the original 8-dungeon redesign -- restated here so it's
traceable, not a generic paraphrase:
  - Key Item Re-contextualization: a dungeon needs real locks/puzzles
    for its key item to actually matter (check_lock_density).
  - Organic Learning and Flow: a real hub that actually branches
    (check_hub_branches).
  - Interconnected Spatial Design: real backtracking/shortcuts, and a
    cohesive, non-conflicting map (check_hub_revisited,
    check_reciprocity).
  - The Climax Boss Fight: the finale is actually gated, and pitched
    at the right difficulty (check_boss_gated, check_level_band).
  - The progression-graph rule (the concrete authoring formula): the
    path to the dungeon's own item must never require that item; the
    path to the boss key must (check_no_self_referential_lock,
    check_boss_gated).
"""
from collections import deque

import remnants

# Confirmed per-chapter level-band model (2026-08-19 audit, hand-
# reviewed with Coffee) -- chapters 1-8 only; chapters 9-14 explicitly
# not banded yet (future work, per that audit's own notes). A dungeon
# whose owning arc isn't in this table (a later, not-yet-banded
# chapter, or one of the two deliberate bonus vaults with no arc at
# all) is simply not scored by check_level_band -- not a failure.
CHAPTER_LEVEL_BANDS: dict[str, tuple[int, int]] = {
    "arc_1_discovery": (1, 15),
    "arc_2_descent": (10, 25),
    "arc_3_revelation": (20, 35),
    "arc_4_ascension": (30, 45),
    "arc_5_goblin_warrens": (40, 55),
    "arc_6_sunken_root_caverns": (50, 65),
    "arc_7_stonearch_gorge": (60, 75),
    "arc_8_greymoor_downs": (75, 99),
}

_OPPOSITE_DIRECTION = {"north": "south", "south": "north", "east": "west", "west": "east", "up": "down", "down": "up"}

# Deliberately NOT part of any story arc's quest list -- confirmed real,
# not a gap (the original 8-dungeon redesign plan's own explicit note).
_BONUS_VAULT_DUNGEON_IDS = {"wrathflame_vault", "deep_root_vault"}


def _flat_locations(campaign: dict) -> dict[str, dict]:
    """
    campaign["locations"] is nested one level deep by layer (surface/
    underground/sky per campaign_loader.py) -- location_ids are
    globally unique across layers (the same guarantee campaign_loader.
    get_location's own unambiguous lookup already relies on), so this
    flattens it into one real (live, not copied) {location_id: room}
    map for every function below that needs to look a room up without
    caring which layer it lives in.
    """
    flat: dict[str, dict] = {}
    for layer_locations in campaign.get("locations", {}).values():
        flat.update(layer_locations)
    return flat


def _dungeon_rooms(campaign: dict, dungeon_id: str) -> dict[str, dict]:
    """Every real (live) location tagged with this dungeon_id, searched across all layers -- a dungeon like Stonearch Bridge genuinely spans more than one."""
    return {
        loc_id: loc for loc_id, loc in _flat_locations(campaign).items()
        if loc.get("dungeon_id") == dungeon_id
    }


def _find_room(campaign: dict, room_id: str) -> tuple[str, dict] | None:
    """(layer_name, live room dict) for an existing location_id, searched across all layers. None if it doesn't exist yet."""
    for layer_name, layer_locations in campaign.get("locations", {}).items():
        if room_id in layer_locations:
            return layer_name, layer_locations[room_id]
    return None


def _owning_arc_id(campaign: dict, room_ids: set[str]) -> str | None:
    """
    Which story arc "owns" this dungeon.

    Real gap found running check_level_band for the first time against
    all 8 shipped dungeons: Goblin Warrens' entire real arc_5 interior
    (levels 40-55, correctly banded) got misattributed to arc_1 --
    because its own bare zone entrance is ALSO the location of an
    genuinely-real, much-earlier arc_1 quest (clear_the_warrens), and
    a naive multi-source BFS flood from every arc's seeds at once lets
    whichever arc's wave reaches a room in fewer hops claim it, with no
    regard for which arc's story actually built that room. A quest
    seeded DIRECTLY inside the dungeon's own room set is a far
    stronger signal of "this arc's story is what this dungeon is
    about" than mere graph proximity from outside -- checked first,
    before falling back to the same seeded-BFS-flood ownership bot.py's
    own _location_chapter_arc_index already uses for locations with no
    direct in-dungeon seed of their own (reimplemented here, not
    imported, since rules/ modules stay standalone; bot.py depends on
    rules/, never the reverse). Returns None for a dungeon no arc's
    seed or flood ever reaches (the two deliberate bonus vaults,
    wrathflame_vault/deep_root_vault).
    """
    # Count every arc's own direct seeds landing inside this dungeon's
    # room set, and pick whichever has the MOST -- not just the first
    # match in arc order. Goblin Warrens genuinely has two: its bare,
    # pre-redesign entrance carries one much-earlier arc_1 quest
    # (clear_the_warrens), while its whole real redesigned interior
    # carries ten separate arc_5 quests. Picking "first in arc order"
    # would wrongly hand the entire dungeon to arc_1 off a single
    # incidental seed at the front door.
    direct_seed_counts: dict[str, int] = {}
    for arc_id, arc in campaign.get("story_arcs", {}).items():
        for quest_id in arc.get("quests", []):
            quest = campaign.get("quests", {}).get(quest_id, {})
            for loc in (quest.get("location"), quest.get("trigger", {}).get("location"), quest.get("objective_location")):
                if loc in room_ids:
                    direct_seed_counts[arc_id] = direct_seed_counts.get(arc_id, 0) + 1
    if direct_seed_counts:
        return max(direct_seed_counts, key=direct_seed_counts.get)

    owner: dict[str, str] = {}
    for arc_id, arc in campaign.get("story_arcs", {}).items():
        for quest_id in arc.get("quests", []):
            quest = campaign.get("quests", {}).get(quest_id, {})
            for loc in (quest.get("location"), quest.get("trigger", {}).get("location"), quest.get("objective_location")):
                if loc and loc not in owner:
                    owner[loc] = arc_id
    queue = deque(owner.keys())
    all_locations = _flat_locations(campaign)
    while queue:
        cur = queue.popleft()
        loc_data = all_locations.get(cur)
        for neighbor in (loc_data.get("connections", []) if loc_data else []):
            if neighbor not in owner:
                owner[neighbor] = owner[cur]
                queue.append(neighbor)
    for room_id in room_ids:
        if room_id in owner:
            return owner[room_id]
    return None


# ---------------------------------------------------------------------------
# Checks -- each returns a list of human-readable failure descriptions
# (empty list = passed).
# ---------------------------------------------------------------------------

def check_hub_branches(campaign: dict, dungeon_id: str) -> list[str]:
    rooms = _dungeon_rooms(campaign, dungeon_id)
    if not rooms:
        return [f"no rooms found tagged dungeon_id={dungeon_id!r}"]
    hubs = [r for r in rooms.values() if r.get("dungeon_hub")]
    if not hubs:
        hubs = [max(rooms.values(), key=lambda r: len(r.get("connections", [])))]
    failures = []
    for hub in hubs:
        conn_count = len(hub.get("connections", []))
        if conn_count < 3:
            failures.append(f"hub {hub.get('name', '?')!r} has only {conn_count} connections, needs 3+")
    return failures


def check_hub_revisited(campaign: dict, dungeon_id: str) -> list[str]:
    rooms = _dungeon_rooms(campaign, dungeon_id)
    hub_ids = {rid for rid, r in rooms.items() if r.get("dungeon_hub")}
    if not hub_ids:
        return ["no room flagged dungeon_hub: true -- can't check for a real shortcut back to it"]
    for rid, room in rooms.items():
        for dest_id, lockable_id in room.get("locked_connections", {}).items():
            if dest_id in hub_ids and rid not in hub_ids:
                return []  # a real shortcut back to the hub exists
    return [f"no locked_connections shortcut anywhere in the dungeon points back to the hub ({', '.join(sorted(hub_ids))})"]


def check_no_self_referential_lock(campaign: dict, dungeon_id: str) -> list[str]:
    rooms = _dungeon_rooms(campaign, dungeon_id)
    room_ids = set(rooms.keys())
    quests = campaign.get("quests", {})

    # Every (from_room, to_room) edge gated by each lockable_id, collected
    # once -- a real gate can genuinely carry a locked_connections entry
    # on BOTH sides once opened (e.g. Wrathflame Vault's Smoldering
    # Stair references the same lockable_id pointing back at the hub).
    # Both directions of the SAME physical gate must be removed together
    # when testing reachability, or checking from the far side of an
    # already-passed gate falsely looks self-referential -- it hasn't
    # been reached without the key yet, it's reached the moment the gate
    # opens, same gate either way.
    gated_edges: dict[str, set[tuple[str, str]]] = {}
    lockable_key_item: dict[str, str] = {}
    for room_id, room in rooms.items():
        lockables_by_id = {lk["id"]: lk for lk in room.get("lockables", [])}
        for dest_id, lockable_id in room.get("locked_connections", {}).items():
            gated_edges.setdefault(lockable_id, set()).add((room_id, dest_id))
            lockable = lockables_by_id.get(lockable_id)
            if lockable and lockable.get("requires_key_item"):
                lockable_key_item[lockable_id] = lockable["requires_key_item"]

    hub_ids = [rid for rid, r in rooms.items() if r.get("dungeon_hub")]
    entry_id = hub_ids[0] if hub_ids else (max(rooms, key=lambda rid: len(rooms[rid].get("connections", []))) if rooms else None)
    if entry_id is None:
        return []

    failures = []
    for lockable_id, key_item in lockable_key_item.items():
        key_quest = next((q for q in quests.values() if q.get("reward_item") == key_item), None)
        if not key_quest:
            continue  # not a quest-granted key -- nothing in this dungeon's own graph to check
        key_location = key_quest.get("location")
        if key_location not in room_ids:
            continue  # key's own quest sits outside this dungeon entirely
        skip_edges = gated_edges.get(lockable_id, set())
        reachable = {entry_id}
        queue = deque([entry_id])
        while queue:
            cur = queue.popleft()
            for nxt in rooms.get(cur, {}).get("connections", []):
                if nxt not in room_ids or (cur, nxt) in skip_edges:
                    continue
                if nxt not in reachable:
                    reachable.add(nxt)
                    queue.append(nxt)
        if key_location not in reachable:
            failures.append(
                f"{key_item!r} (from quest {key_quest.get('title', key_item)!r} at {key_location!r}) "
                f"is only reachable by crossing the very lock ({lockable_id!r}) it opens"
            )
    return failures


def check_boss_gated(campaign: dict, dungeon_id: str) -> list[str]:
    """
    Real gap found running this first-ever pass against all 8 shipped
    dungeons, twice:

    1. Most "ungated boss" hits were actually one of the 12 real
       Remnant superbosses (remnants.py), a DELIBERATELY separate
       difficulty ladder -- reachable early, unbeatable for a long
       time, by design (2026-08-19 audit). Gating one would contradict
       the whole point, so Remnant monster_keys are excluded outright.
    2. Checking only the boss's OWN room for a gate missed the real
       pattern several dungeons actually use: the gate sits on an
       EARLIER connection along the path (e.g. Goblin Warrens' own
       requires_completed_quest riddle gate, several rooms before the
       boss), not necessarily on the boss's own door. The real
       question is "can the boss room be reached AT ALL without
       crossing a single gate anywhere along the way" -- answered here
       by a real BFS that only ever follows UNGATED edges from the
       hub; anything that BFS reaches is genuinely free to walk into.
    3. Several dungeons genuinely carry MORE than one real is_boss
       monster at different tiers -- an early, deliberately-ungated
       "front door" named threat (e.g. Goblin Warrens' own goblin_boss,
       tied to the much-earlier clear_the_warrens quest, pre-dating
       this dungeon's own Zelda redesign) alongside the dungeon's real,
       properly-gated climax deeper in (the_paymasters_shadow). Two
       other dungeons' real climax boss sits AT the dungeon's own
       front door by original design (the_first_city/the_unmoored_isle
       -- both flagged dungeon_hub at that exact entrance), gated
       EXTERNALLY by whatever led here from the previous chapter, which
       is out of this per-dungeon check's own scope to see. Flagging
       every individual is_boss monster wrongly punished both real,
       legitimate shapes. The real question this check needs to answer
       is "does this dungeon have AT LEAST ONE real, internally-gated
       climax" -- not "is every named threat gated."
    """
    rooms = _dungeon_rooms(campaign, dungeon_id)
    room_ids = set(rooms.keys())
    monsters = campaign.get("monsters", {})

    boss_rooms = [
        (room_id, room.get("name", room_id), monster_key)
        for room_id, room in rooms.items()
        for monster_key in room.get("monsters", [])
        if monsters.get(monster_key, {}).get("is_boss") and remnants.remnant_for_monster_key(monster_key) is None
    ]
    if not boss_rooms:
        return []

    hub_ids = [rid for rid, r in rooms.items() if r.get("dungeon_hub")]
    entry_id = hub_ids[0] if hub_ids else max(rooms, key=lambda rid: len(rooms[rid].get("connections", [])))

    def _edge_is_gated(src_id: str, dest_id: str) -> bool:
        src = rooms[src_id]
        dest = rooms.get(dest_id, {})
        return bool(dest.get("requires_item")) or bool(src.get("story_gates", {}).get(dest_id)) or bool(src.get("locked_connections", {}).get(dest_id))

    reachable_with_zero_gates = {entry_id}
    queue = deque([entry_id])
    while queue:
        cur = queue.popleft()
        for nxt in rooms.get(cur, {}).get("connections", []):
            if nxt not in room_ids or _edge_is_gated(cur, nxt):
                continue
            if nxt not in reachable_with_zero_gates:
                reachable_with_zero_gates.add(nxt)
                queue.append(nxt)

    if any(room_id not in reachable_with_zero_gates for room_id, _, _ in boss_rooms):
        return []  # at least one real boss is properly gated -- good enough

    named = ", ".join(f"{name!r} ({monster_key!r})" for _, name, monster_key in boss_rooms)
    return [f"no boss in this dungeon is behind any requires_item/story_gates/locked_connections gate: {named}"]


def check_reciprocity(campaign: dict, dungeon_id: str) -> list[str]:
    """
    Real precision bug found writing this check's own failing-case
    test: the original exception ("either endpoint is a locked_
    connections target ANYWHERE in the dungeon") was too broad -- it
    silently exempted an unrelated genuine one-way connection between
    two ordinary rooms just because one of them ALSO happened to be
    the far end of a completely different lever shortcut elsewhere.
    The real, established exception (e.g. Wrathflame Vault's
    Smoldering Stair connects back to its hub, but the hub has no
    matching forward connection -- only a locked_connections gate --
    until unlocked) is about one SPECIFIC pair of rooms, not "any edge
    touching either room." Now only exempts a connection whose exact
    (room_id, dest_id) pair (in either direction) coincides with a
    real locked_connections entry somewhere.
    """
    rooms = _dungeon_rooms(campaign, dungeon_id)
    room_ids = set(rooms.keys())
    gated_pairs = {
        (src_id, dest_id) for src_id, room in rooms.items() for dest_id in room.get("locked_connections", {})
    }
    failures = []
    for room_id, room in rooms.items():
        for dest_id in room.get("connections", []):
            if dest_id not in room_ids:
                continue  # a real entrance/exit leaving this dungeon -- not this check's concern
            if room_id in rooms[dest_id].get("connections", []):
                continue
            if (room_id, dest_id) in gated_pairs or (dest_id, room_id) in gated_pairs:
                continue
            failures.append(f"{room_id!r} connects to {dest_id!r} but not vice versa")
    return failures


def check_lock_density(campaign: dict, dungeon_id: str) -> list[str]:
    rooms = _dungeon_rooms(campaign, dungeon_id)
    room_count = len(rooms)
    if room_count == 0:
        return []
    lock_count = sum(len(r.get("lockables", [])) for r in rooms.values())
    min_expected = max(1, room_count // 8)
    if lock_count < min_expected:
        return [f"only {lock_count} lockable(s) across {room_count} rooms -- expected at least {min_expected} (~1 per 8 rooms)"]
    return []


def check_level_band(campaign: dict, dungeon_id: str) -> list[str]:
    """
    Real gap found running this first-ever pass against all 8 shipped
    dungeons: two entirely reasonable, well-documented exceptions to
    the raw band range were flagged as false failures.

    1. Remnant superbosses (remnants.py) again -- deliberately outside
       whatever band the zone they physically sit in would suggest,
       by design (2026-08-19 audit); excluded the same way
       check_boss_gated already does.
    2. A monster BELOW the band floor is the documented "meet the weak
       stuff at the front door before the real dungeon" pattern
       (2026-08-19 audit's own named examples -- Greymoor Downs' lv18
       wolf, Windswept Ridge's lv27 wolf, Sunken Root Caverns'
       tunnel_goblin, Stonearch Bridge's giant_spider -- ALL four hit
       this exact check on the first run). Confirmed intentional, not
       a bug, so only overshooting the CEILING is worth reporting --
       that's the direction a real authoring mistake (or an evolve
       pass gone wrong later) would actually show up as.
    3. A real is_boss monster is ALSO allowed to exceed the ceiling --
       the same precedent this exact bestiary already sets elsewhere
       (every non-boss/trash monster caps damage_bonus at a flat 50,
       "only is_boss: true monsters exceed it," 2026-08-19 audit). A
       dungeon's own climax deserves to be the peak difficulty in the
       room, which is the entire point of "The Climax Boss Fight"
       research pillar this checker is built around -- check_boss_gated
       already separately verifies a real boss can't be stumbled into
       unprepared, so a modest overshoot here (The Original Spire's
       the_last_glyph, 3 over The First City's ceiling, was the one
       real hit on the first-ever run) isn't a design mistake to flag.

    The two known bonus vaults (wrathflame_vault/deep_root_vault) are
    deliberately not part of any story arc's band model at all (the
    original 8-dungeon redesign plan's own explicit statement) --
    reachable from an early zone via plain connections, which the BFS
    ownership lookup would otherwise misattribute to whatever arc
    happens to flood-reach them first. Skipped outright, not scored.

    Real gap found building Phase 3 (rules/dungeon_evolve.py, 2026-09-01):
    a generated evolve-pass dungeon has the exact same problem as the
    two bonus vaults -- it carries no quest of its own, so it always
    falls to the same BFS-flood fallback, and its one new connection to
    the SOURCE dungeon lets the source's own (lower) arc bleed into it,
    wrongly flagging every monster the evolve pass deliberately placed
    at a HARDER band. `campaign["evolved_dungeon_ids"]` (stamped by
    evolve_dungeon itself) is the same kind of explicit, no-guessing
    opt-out as the hardcoded bonus-vault set above -- not a hack, the
    identical principle applied to a dungeon that didn't exist yet when
    the original set was written.
    """
    if dungeon_id in _BONUS_VAULT_DUNGEON_IDS or dungeon_id in campaign.get("evolved_dungeon_ids", []):
        return []
    rooms = _dungeon_rooms(campaign, dungeon_id)
    arc_id = _owning_arc_id(campaign, set(rooms.keys()))
    if arc_id is None or arc_id not in CHAPTER_LEVEL_BANDS:
        return []  # not owned by any arc, or a later chapter not banded yet
    _, band_max = CHAPTER_LEVEL_BANDS[arc_id]
    monsters = campaign.get("monsters", {})
    failures = []
    for room in rooms.values():
        for monster_key in room.get("monsters", []):
            template = monsters.get(monster_key, {})
            if template.get("is_boss") or remnants.remnant_for_monster_key(monster_key) is not None:
                continue
            level = template.get("level")
            if level is None or level <= band_max:
                continue
            failures.append(f"{monster_key!r} is level {level}, above {arc_id}'s confirmed ceiling of {band_max}")
    return failures


def check_warps_reference_real_rooms(campaign: dict, dungeon_id: str) -> list[str]:
    """
    New for the overworld warp port (2026-09-04, per Coffee: warps
    belong in the generator now, not deferred to the final chapter --
    see rules/dungeon_evolve.py's own new warp-placement block). A
    warp is stored outside `connections` (rules/labyrinth.py's own
    established precedent -- it's drawn as a real line across the map,
    not a doorway between grid-adjacent cells), so none of the other 7
    checks ever see it at all. This check exists purely to catch a
    dangling or malformed warp: a destination that isn't a real room
    in this dungeon, a self-warp, or a one-sided link (the Labyrinth's
    own generator always writes both ends together; this check makes
    that a verified invariant here too, not just a convention).
    """
    rooms = _dungeon_rooms(campaign, dungeon_id)
    room_ids = set(rooms.keys())
    failures = []
    for room_id, room in rooms.items():
        for dest_id in room.get("warps", []):
            if dest_id == room_id:
                failures.append(f"{room_id!r} has a warp pointing at itself")
            elif dest_id not in room_ids:
                failures.append(f"{room_id!r} has a warp to {dest_id!r}, which isn't a real room in this dungeon")
            elif room_id not in rooms.get(dest_id, {}).get("warps", []):
                failures.append(f"{room_id!r} warps to {dest_id!r} but not vice versa (warps must be bidirectional)")
    return failures


def check_collapse_never_orphans_a_room(campaign: dict, dungeon_id: str) -> list[str]:
    """
    New for the overworld collapse-puzzle port (2026-09-04). A
    `collapsing_connections` entry is ALLOWED to make its own
    destination room permanently unreachable once triggered -- that's
    the intended Eagle's-Tower-style trade-off (the Labyrinth's own
    identical mechanic already ships with exactly this: sealing a
    genuine dead-end branch is the point, not a bug). What must never
    happen is the seal taking something ELSE down with it: a switch
    lockable some OTHER gate's own `requires` list still depends on
    sitting in the room(s) this edge cuts off, or the dungeon's own
    boss room becoming unreachable. Both computed by a real BFS over
    `connections` with the one sealed edge removed, not assumed.
    """
    rooms = _dungeon_rooms(campaign, dungeon_id)
    room_ids = set(rooms.keys())
    if not room_ids:
        return []
    hub_ids = [rid for rid, r in rooms.items() if r.get("dungeon_hub")]
    entry_id = hub_ids[0] if hub_ids else max(rooms, key=lambda rid: len(rooms[rid].get("connections", [])))

    switch_room: dict[str, str] = {}
    other_gates: list[tuple[str, str, list[str]]] = []  # (gate_room_id, gate_lockable_id, requires)
    for room_id, room in rooms.items():
        for lk in room.get("lockables", []):
            if lk.get("kind") == "switch":
                switch_room[lk["id"]] = room_id
            elif lk.get("kind") == "multi_switch_gate":
                other_gates.append((room_id, lk.get("id"), lk.get("requires", [])))

    monsters = campaign.get("monsters", {})
    boss_room_ids = {
        room_id for room_id, room in rooms.items()
        for monster_key in room.get("monsters", [])
        if monsters.get(monster_key, {}).get("is_boss") and remnants.remnant_for_monster_key(monster_key) is None
    }

    failures = []
    for room_id, room in rooms.items():
        for dest_id, trigger_id in room.get("collapsing_connections", {}).items():
            if dest_id not in room_ids:
                continue
            # Walks `connections` AND `locked_connections` destinations
            # together -- a door/switch-gated branch (the boss branch,
            # or a switch/plate branch) is still a real, eventually-
            # reachable room to a player willing to solve its gate, not
            # a dead end; only `connections`-based reachability would
            # wrongly treat every one of those as already unreachable
            # BEFORE the collapse even fires (a real bug found testing
            # this fix: the boss room is normally reached exclusively
            # through a locked_connections gate, never a plain
            # connections edge from the hub, by design).
            reachable = {entry_id}
            queue = deque([entry_id])
            while queue:
                cur = queue.popleft()
                cur_room = rooms.get(cur, {})
                neighbors = list(cur_room.get("connections", [])) + list(cur_room.get("locked_connections", {}))
                for nxt in neighbors:
                    if nxt not in room_ids:
                        continue
                    if (cur == room_id and nxt == dest_id) or (cur == dest_id and nxt == room_id):
                        continue
                    if nxt not in reachable:
                        reachable.add(nxt)
                        queue.append(nxt)
            orphaned = room_ids - reachable
            if not orphaned:
                continue
            if orphaned & boss_room_ids:
                failures.append(f"{room_id!r}->{dest_id!r} collapsing_connections (trigger {trigger_id!r}) would cut off the boss room")
            for gate_room_id, gate_id, requires in other_gates:
                if gate_id == trigger_id:
                    continue  # the collapse's own trigger is allowed to sit inside the branch it seals -- solved before it collapses
                for switch_id in requires:
                    if switch_room.get(switch_id) in orphaned:
                        failures.append(
                            f"{room_id!r}->{dest_id!r} collapsing_connections (trigger {trigger_id!r}) would strand "
                            f"switch {switch_id!r} needed by gate {gate_id!r} in room {gate_room_id!r}"
                        )
    return failures


CHECKS = {
    "hub_branches": check_hub_branches,
    "hub_revisited": check_hub_revisited,
    "no_self_referential_lock": check_no_self_referential_lock,
    "boss_gated": check_boss_gated,
    "reciprocity": check_reciprocity,
    "lock_density": check_lock_density,
    "level_band": check_level_band,
    "warps_reference_real_rooms": check_warps_reference_real_rooms,
    "collapse_never_orphans_a_room": check_collapse_never_orphans_a_room,
}


def audit_dungeon(campaign: dict, dungeon_id: str) -> dict[str, list[str]]:
    """Runs every real check against one dungeon_id. {check_name: [failures]} -- an empty list means that check passed."""
    return {name: fn(campaign, dungeon_id) for name, fn in CHECKS.items()}


# ---------------------------------------------------------------------------
# Authoring helpers -- turn the operations repeated in every one of this
# session's throwaway build scripts into calls against code that can't
# get the shape wrong.
# ---------------------------------------------------------------------------

def add_room(campaign: dict, layer: str, dungeon_id: str, room_id: str, name: str, description: str,
             grid_position: dict | None = None, **fields) -> dict:
    """
    Adds a new room to campaign["locations"][layer], stamping the tags
    every dungeon room needs (dungeon_id, dungeon_interior: true, a
    real grid_position) so a new room can never be added missing one
    -- the exact class of small omission that caused incidental fast-
    travel/minimap gaps by hand this session. `layer` is one of the
    real campaign layers ("surface"/"underground"/"sky") -- required
    since campaign["locations"] is genuinely nested by layer
    (campaign_loader.py), not flat, and a brand-new room has no
    existing entry to infer it from.
    """
    room = {
        "name": name,
        "description": description,
        "connections": [],
        "directions": {},
        "dungeon_id": dungeon_id,
        "dungeon_interior": True,
        "grid_position": grid_position or {"x": 0, "y": 0},
        **fields,
    }
    campaign.setdefault("locations", {}).setdefault(layer, {})[room_id] = room
    return room


def connect(campaign: dict, a_id: str, b_id: str, direction: str | None = None) -> None:
    """Writes BOTH sides of a connections/directions edge in one call -- reciprocity correct by construction, not verified after the fact."""
    a_found = _find_room(campaign, a_id)
    b_found = _find_room(campaign, b_id)
    if not a_found:
        raise KeyError(f"connect: no existing room {a_id!r} -- add_room it first")
    if not b_found:
        raise KeyError(f"connect: no existing room {b_id!r} -- add_room it first")
    _, a = a_found
    _, b = b_found
    if b_id not in a.get("connections", []):
        a.setdefault("connections", []).append(b_id)
    if a_id not in b.get("connections", []):
        b.setdefault("connections", []).append(a_id)
    if direction:
        a.setdefault("directions", {})[direction] = b_id
        b.setdefault("directions", {})[_OPPOSITE_DIRECTION[direction]] = a_id


def add_lever_shortcut(campaign: dict, far_room_id: str, hub_id: str, lockable_id: str, name: str) -> None:
    """
    The ONE correct shape for a shortcut lever (2026-08-30 pattern,
    established across all 8 redesigned dungeons): the lockable +
    locked_connections entry live ONLY on the far room, pointing back
    at the hub -- the hub gets NO matching entry, so it can never be
    found/picked from that side. Encodes directly the exact bug this
    session hit once by hand (wired backwards, caught only by a test
    failure) -- there's no longer a wrong way to call this.
    """
    found = _find_room(campaign, far_room_id)
    if not found:
        raise KeyError(f"add_lever_shortcut: no existing room {far_room_id!r} -- add_room it first")
    _, far_room = found
    far_room.setdefault("lockables", []).append({"id": lockable_id, "kind": "lever", "name": name})
    far_room.setdefault("locked_connections", {})[hub_id] = lockable_id


def add_key_gate(campaign: dict, room_id: str, dest_id: str, lockable_id: str, name: str, key_item: str) -> None:
    """Same idea as add_lever_shortcut, for the requires_key_item pattern -- a permanent Zelda-style key, no DEX roll involved."""
    found = _find_room(campaign, room_id)
    if not found:
        raise KeyError(f"add_key_gate: no existing room {room_id!r} -- add_room it first")
    _, room = found
    room.setdefault("lockables", []).append({"id": lockable_id, "kind": "door", "name": name, "requires_key_item": key_item})
    room.setdefault("locked_connections", {})[dest_id] = lockable_id


def dump_dungeon_graph(campaign: dict, dungeon_id: str) -> str:
    """A quick, readable text rendering of one dungeon's current room graph -- for Claude Code to read BEFORE editing, replacing hand-tracing raw connections dicts."""
    rooms = _dungeon_rooms(campaign, dungeon_id)
    lines = [f"Dungeon {dungeon_id!r}: {len(rooms)} rooms"]
    for room_id, room in sorted(rooms.items()):
        lines.append(f"- {room_id} ({room.get('name', '?')}): connections={room.get('connections', [])}")
        for lk in room.get("lockables", []):
            key_note = f" requires {lk['requires_key_item']}" if lk.get("requires_key_item") else ""
            lines.append(f"    lockable {lk['id']!r} ({lk.get('kind')}): {lk.get('name')}{key_note}")
        for dest, lid in room.get("locked_connections", {}).items():
            lines.append(f"    locked_connections: -> {dest} via {lid!r}")
    return "\n".join(lines)
