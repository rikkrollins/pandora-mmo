"""
rules/labyrinth.py

The Labyrinth (2026-09-01, Phase L1, per Coffee: "start the labyrinth
architecture" -- a live, procedurally-generated, ever-deepening
dungeon unlocked by defeating colosseum_champion, run by bot.py at
request time, never authored offline).

Deliberately NOT rules/dungeon_evolve.py's heavier pipeline: no
build_location_grid/dungeon_audit retry loop (this needs to run in
well under a second, not retry against 7 structural checks meant for
permanent, hand-polishable content). Reuses only dungeon_evolve.
_candidate_monsters, for a wide, non-Remnant, non-headline-boss monster
pool -- depth is unbounded, so unlike evolve_dungeon's finite
next-chapter-band lookup, monster CHOICE here stays catalog-agnostic;
ALL of the difficulty comes from a multiplicative scale bot.py's own
_build_labyrinth_enemy applies to whichever real monster template gets
picked (same stat_mult-on-a-template shape bot.py's _build_echo_enemy
already proves safe), never baked into this module's own room data.

Phase L3 (2026-09-02, per Coffee, live and explicit: "make floors
persistent and interconnected... make each level segments 5 levels
then the waypoints break... generate a new one when characters choose
to go further... 5 levels of honeycombing") REPLACES Phase L1/L2's
single-floor-at-a-time, discard-on-descend model. The old model's own
"no backtracking across floors" was a real, deliberate constraint at
the time -- Coffee's own later ask explicitly reverses it. The new
unit of generation is a SEGMENT: SEGMENT_SIZE (5) real floors,
generated together, genuinely interconnected via the same descends_to/
ascends_to convention the real overworld already uses for multi-story
buildings -- a party can freely walk up and down between any of a
live segment's 5 floors, real backtracking, real cross-floor puzzles
(a switch on one floor can gate a reward on another -- `_SWITCH_STATE`
was already chat-scoped, never room-scoped, so this needs zero new
plumbing beyond generating the rooms that way). Depth still never
caps -- what caps is how much lives in storage at once: reaching a
segment's own checkpoint floor (its 5th, a real safe waypoint: full
heal, full spell-slot refill, an ASI point, a wandering trader's shop,
a real achievement) and choosing to go deeper "breaks the waypoint" --
the whole segment is discarded and a brand new one generated -- so the
game can go infinitely deep while only ever storing one segment's
worth of rooms per active run. Room ids are now floor-namespaced
(`f{floor}_...`) since multiple floors are genuinely alive in storage
at once, unlike the old single-floor model.
"""
import math
import random

from rules.dungeon_evolve import _candidate_monsters

_SIDE_ROOM_COUNT_RANGE = (3, 5)

# Real Zelda-style branching topology (2026-09-03, per Coffee, dev-
# bridge: 3 real Link's Awakening dungeon maps -- "I want the levels to
# be more explorable, travelable with paths, puzzles, mini bosses...
# make an algorithm to accomplish this"). Confirmed by direct code read
# before this: generate_floor was PURE hub-and-spoke (every side room
# AND the stairs connected straight to the hub) -- a player could walk
# to the stairs in one hop, engaging zero real content, the opposite of
# every real reference map studied (all three showed genuine branching,
# key-gated side areas, and a mini-boss standing between the entrance
# and the exit). Real external research (Metazelda, github.com/tcoxon/
# metazelda, and the academic graph-based lock-and-key literature it's
# cited in) confirms the actual technique: rooms are graph nodes, keys/
# switches are placed in an order that's solvable BY CONSTRUCTION (a
# lock is only ever wired to a switch that already exists in an
# already-reachable, unlocked branch) -- no retry/audit loop needed,
# matching this module's own hard "must run in well under a second"
# requirement. See [[project_zelda_dungeon_algorithm_research]] for the
# full research writeup.
_BRANCH_DEPTH_WEIGHTS = ([0, 1, 2], [0.45, 0.35, 0.2])  # most branches stay shallow; a few run genuinely deep
_BRANCH_GATE_CHANCE = 0.35
_MINIBOSS_CHANCE = 0.3

# Real live follow-up (2026-09-03, Coffee: "do all of them" -- warps,
# a floor-altering puzzle, and an owl-statue-style hint, after the same
# 3 Link's Awakening maps). Warps are a real, one-way-declared-but-
# mutually-added shortcut between two rooms that AREN'T already
# adjacent in the branch graph -- stored as `room["warps"]` (a
# separate field from `connections`, since a warp's own map line is
# drawn differently -- a real line straight across the floor, not a
# doorway gap between grid-adjacent cells) rather than pretending it's
# an ordinary lateral connection.
_WARP_CHANCE = 0.25
# Real Eagle's Tower-style structural puzzle: solving it doesn't just
# open one new door, it also SEALS a previously-open one elsewhere on
# the same floor -- "the floor's structure shifts," same spirit as the
# reference's pillar-triggered floor collapse, without needing a
# genuinely separate before/after floor copy.
_COLLAPSE_PUZZLE_CHANCE = 0.2

# L3: one segment = this many real, interconnected floors. Reuses the
# exact number Phase L2c's milestone interval already used, per
# Coffee's own "5 levels of honeycombing" framing -- the OLD milestone
# vault on every 5th floor IS this segment's own checkpoint floor now,
# just upgraded (see _build_checkpoint_room) rather than a separate concept.
SEGMENT_SIZE = 5
MILESTONE_FLOOR_INTERVAL = SEGMENT_SIZE  # kept as an alias -- older code/tests reference this name

# L3, real theme variety (2026-09-02, per Coffee: "we need lots of
# themes and area types or scenarios playable in the labarythn...
# make sure it is something like 'an alternate universe' or 'another
# dimension'... make sure the generator can handle theme and
# descriptions and the rng can handle that"). Deliberately code-driven
# (a small, real, hand-authored pool the RNG picks from and templates
# real room names/descriptions out of), NOT an AI-narrated invention
# per room -- this generator has to run in well under a second, and
# this game's own grounding rule is "never invent lore the AI layer
# doesn't already have real facts for." Per Coffee's explicit
# direction, none of these reuse "Pandora's Box" or any other real
# main-story lore -- the Labyrinth is framed as its own genuine
# fracture between worlds, a real "alternate universe/another
# dimension" anomaly, distinct from the main campaign's own cosmology.
# One theme is chosen per SEGMENT (not per floor) via RNG, so a whole
# 5-floor honeycomb feels like one coherent place rather than five
# unrelated fragments.
LABYRINTH_THEMES = [
    {
        "id": "shattered_mirror",
        "name": "The Shattered Mirror",
        "intro": "The way ahead cracks like glass and reassembles wrong -- everything here is a reflection of somewhere real, just slightly, deliberately, off.",
        "hub_description": "Fractured light bends through the air here, throwing a dozen almost-right versions of the room across every wall.",
        "checkpoint_description": "The cracks stop spreading here -- a single unbroken pane, calm at the center of all this wrongness.",
        "room_names": ["A Cracked Reflection", "The Wrong Angle", "A Doubled Hallway", "The Silvered Room", "A Recursive Corner"],
        "room_descriptions": [
            "A tall pane of broken glass stands freestanding in the middle of the floor, showing you a version of this room that isn't quite the one you're standing in.",
            "The corners here don't meet the way corners should -- every angle is a few degrees off from what your eyes expect.",
            "The same stretch of hallway repeats twice, mirrored, with no visible seam where one copy ends and the other begins.",
            "Every wall is silvered like an old mirror's backing, and your own reflection lags a half-second behind your real movements.",
            "The room curls back on itself at the far end, so that walking straight eventually brings you back to where you started.",
        ],
        "signature_hazard": "drowning",
    },
    {
        "id": "hollow_between",
        "name": "The Hollow Between",
        "intro": "Sound dies a step behind you as you cross over -- this place sits in the gap between where you were and where you're going, and it doesn't want visitors noticed.",
        "hub_description": "A wide, grey nowhere, lit by no source anyone can point to.",
        "checkpoint_description": "The grey finally holds still here, just long enough to feel like an actual room again.",
        "room_names": ["A Room That Shouldn't Fit", "The Quiet Gap", "An Unfinished Space", "The In-Between Landing", "A Forgotten Threshold"],
        "room_descriptions": [
            "The proportions here are wrong in a way you can't quite name -- this room is bigger on the inside than the space it sits in from outside.",
            "A narrow gap of nothing splits the room in two; crossing it takes a step too long, like the distance quietly grows as you walk it.",
            "Half the walls here trail off into flat grey nothing before they ever reach a real corner.",
            "This landing exists between two places that no longer connect to it -- neither one remembers building it.",
            "A threshold stands with no door in it and nothing obvious on either side, like something meant to be walked through was forgotten.",
        ],
        "signature_hazard": "freezing",
    },
    {
        "id": "clockwork_fold",
        "name": "The Clockwork Fold",
        "intro": "Gears the size of houses turn somewhere out of sight, and the whole place ticks forward around you like it's keeping its own private time.",
        "hub_description": "Brass housing and slow-turning gearwork line every surface, all of it moving to a rhythm no one asked for.",
        "checkpoint_description": "The gears here have wound down to a stop -- whatever this place used to measure, it isn't measuring it anymore.",
        "room_names": ["A Gear-Locked Chamber", "The Ticking Vault", "A Stalled Mechanism", "The Brass Landing", "A Wound Spring Room"],
        "room_descriptions": [
            "A massive interlocking gear fills most of the far wall, its teeth taller than a person, turning so slowly you can't quite tell it's moving until you look away and back.",
            "Rows of brass-plated lockers line this vault, each one ticking faintly, like something inside is still being wound.",
            "A huge mechanism sits frozen mid-motion here, one gear jammed against another -- whatever it was building toward, it never finished.",
            "Pipework and pressure gauges cover every surface of this landing, needles twitching toward numbers that mean nothing to you.",
            "A tightly coiled spring, thick as a tree trunk, strains visibly against its housing in the corner, tensioned and never released.",
        ],
        "signature_hazard": "arcing_current",
    },
    {
        "id": "ashen_verge",
        "name": "The Ashen Verge",
        "intro": "The air goes warm and grey the moment you cross in -- everything here looks like it's already burned, and burns a little more every time you look away.",
        "hub_description": "Ash drifts in from nowhere, settling over floors that were never actually on fire.",
        "checkpoint_description": "The ash doesn't fall here -- the only still, clean air anywhere in this stretch.",
        "room_names": ["An Ember-Lit Hollow", "The Smoldering Passage", "A Grey Ash Room", "The Cinder Landing", "An Ashen Threshold"],
        "room_descriptions": [
            "Embers glow faintly in the cracks of the floor here, throwing just enough orange light to see the shape of the room by.",
            "A narrow passage stretches ahead, smoke curling along the ceiling with nowhere obvious to vent to.",
            "Fine grey ash coats every surface in here, undisturbed until your own footprints cut through it.",
            "Charred beams cross overhead on this landing, blackened but still somehow holding the weight above them.",
            "The threshold here is scorched black on both sides, as if something passed through it burning, more than once.",
        ],
        "signature_hazard": "lava",
        "secondary_hazard": "overheating",
    },
    {
        "id": "verdant_undoing",
        "name": "The Verdant Undoing",
        "intro": "Roots have already grown through walls that, by every sign, were built after them -- whatever this place is, growth here runs backward.",
        "hub_description": "Vines thick as rope hold up stonework that looks like it should have collapsed centuries ago.",
        "checkpoint_description": "The green here has gone still and orderly, almost tended, almost deliberate.",
        "room_names": ["A Root-Bound Chamber", "The Overgrown Landing", "A Reclaimed Hall", "The Living Threshold", "An Unpruned Room"],
        "room_descriptions": [
            "Thick roots have pushed up through the floor here, bracing the ceiling like they grew that way on purpose.",
            "Moss and creeping vine cover this landing so completely that the original stonework barely shows through anymore.",
            "Whatever this hall was built for, the green growing through every seam has clearly made other plans for it.",
            "Living vines form the actual doorframe of this threshold now, thick enough that stone underneath is only a guess.",
            "Nothing in this room has been pruned or tended in what feels like a very long time -- growth here answers to no one.",
        ],
        "signature_hazard": "acid",
    },
]


def _pick_theme(rng: random.Random) -> dict:
    return rng.choice(LABYRINTH_THEMES)


# L3, real in-fiction framing for WHY the Labyrinth keeps generating
# new alternate-dimension segments (2026-09-02, per Coffee: "the
# labyrinth generator can use a[n] AI to make themes and storylines
# for the levels... but don't use characters that are in the
# storyline, make sure they are separate from the actual game"). A
# brand-new, Labyrinth-exclusive entity -- never a real campaign.json
# NPC/boss, never referenced by any main-story quest -- so it can
# never be confused with (or spoil) the actual game's own antagonists.
# Purely narrative/flavor (bot.py's narrate_labyrinth_segment_flavor,
# a real but OPTIONAL, fire-and-forget Ollama call fired once per new
# segment, never blocking the deterministic entry message) -- not a
# mechanical boss fight.
LABYRINTH_ANTAGONIST_NAME = "the Errant Cartographer"
LABYRINTH_ANTAGONIST_LORE = (
    "Something down here keeps redrawing the map, badly, endlessly -- each new stretch a fresh, "
    "malfunctioning attempt at a whole world, never quite finished before the next one starts."
)

# L2b. Floor modifiers -- a real, announced, floor-wide condition, never
# silent (Coffee's "state it if it's visible" discipline, same as the
# overworld's pits/breakables). 60% of floors have none at all. Now
# stored PER ROOM (every room on a given floor carries the same
# "modifier" value) rather than once per run, since a live segment
# holds 5 real floors at once and each rolls its own independently.
FLOOR_MODIFIERS = ("frenzied", "lightless", "bountiful", "dangerous")
_MODIFIER_CHANCE = 0.4
MODIFIER_ANNOUNCEMENT = {
    "frenzied": "The air here feels frenzied -- whatever's alive moves faster than it should.",
    "lightless": "No natural light reaches this deep. You'll need your own.",
    "bountiful": "Something about this floor feels generous.",
    "dangerous": "This floor feels wrong -- dangerous in a way the last one didn't.",
}

_MILESTONE_ITEM_BANDS = (
    (1, 10, "greater_healing_potion"),
    (11, 25, "superior_healing_potion"),
    (26, 1_000_000, "scroll_magic_missile"),
)

# L2d. Environmental hazards -- a visible, telegraphed obstacle, same
# "state it honestly" rule as the overworld's own pits/breakables.
_HAZARD_CHANCE = 0.15
HAZARD_KINDS = (
    "collapsing_floor", "gas_vent", "spike_pit", "bottomless_pit",
    "acid", "freezing", "overheating", "arcing_current", "lava", "drowning",
)
HAZARD_FLAVOR = {
    "collapsing_floor": "The floor here looks half-rotted through -- one wrong step and it's a long way down.",
    "gas_vent": "A faint hiss comes from a crack in the wall -- something in the air here isn't right.",
    "spike_pit": "A row of corroded spikes juts up through cracks in the floor -- landing on those wrong would knock anyone flat.",
    "bottomless_pit": "The floor gives way to a real pit here, deep enough that a dropped stone never seems to land.",
    # L3, real theme-signature hazards (2026-09-02, per Coffee: "themes
    # shud have hazards for them, lava, acid, drowning..., freezing...,
    # overheating..."). Each one honestly telegraphed, same discipline
    # as every other hazard here -- see HAZARD_DAMAGE_TYPE/
    # _LETHAL_HAZARD_KINDS below for exactly how each resolves.
    "acid": "A film of corrosive sap coats everything within reach, hissing faintly wherever it touches bare stone.",
    "freezing": "The cold here isn't ordinary cold -- it settles into the joints, deeper with every breath.",
    "overheating": "The heat here climbs steadily, well past anything a real fire alone would explain.",
    "arcing_current": "Current jumps between exposed brass fittings, arcing wherever something metal gets too close.",
    "lava": "The floor gives way to real, slow-moving lava here -- bright, silent, and very obviously final.",
    "drowning": "A still, black pool fills the low half of this room -- deep enough, the way it sits, that going under here doesn't feel like it would end the normal way.",
}
# Elemental hazards check the SAME real resistance/vulnerability system
# combat damage already uses (rules.combat.apply_damage_type_modifier)
# -- a character with real fire resistance shrugs off "overheating"
# far more than one without, never a flat, un-mitigatable number.
HAZARD_DAMAGE_TYPE = {
    "acid": "acid", "freezing": "cold", "overheating": "fire", "arcing_current": "lightning", "lava": "fire",
}
# L3 (2026-09-02, per Coffee: "add pits with holes we can plunge to our
# death, and spikes we can land on, if we fall or jump on them, KO").
# Real, distinct severity tiers, both still a single honest DC13 DEX
# save like every other hazard here -- never a silent gotcha:
# - spike_pit: a failed save is a real KO (hp_current -> 0, the exact
#   same "downed, unconscious, recoverable by healing/rest" state
#   combat already uses) -- never permanent on its own.
# - bottomless_pit: a failed save runs the party member through the
#   EXACT same real death-save sequence (rules.combat.resolve_death_
#   save) a downed combatant already faces turn-by-turn in combat,
#   just resolved all at once since there's no combat round to spread
#   it across -- genuinely real odds of dying (is_dead=1, revivable
#   only by the same Revivify path any other real death already uses),
#   not an unavoidable instant kill, and small but real odds of
#   catching a ledge and walking away basically fine (a natural 20).
#   Rare, and only ever appears from floor _BOTTOMLESS_PIT_MIN_FLOOR
#   onward -- brand-new Labyrinth-goers on floor 1 don't instantly meet
#   a real permadeath trap.
_MUNDANE_HAZARD_KINDS = ("collapsing_floor", "gas_vent")
_LETHAL_HAZARD_KINDS = ("spike_pit", "bottomless_pit")
_THEME_LETHAL_HAZARDS = ("lava", "drowning")  # theme-specific -- see HAZARD_DAMAGE_TYPE/_new_side_room
_BOTTOMLESS_PIT_MIN_FLOOR = 10
_LETHAL_HAZARD_CHANCE = 0.05

# L3, theme-signature hazards (2026-09-02, per Coffee: "themes shud
# have hazards for them"). A SEPARATE, independent roll from the
# generic hazard pool above -- checked first, per room, using the
# segment's own theme.  "lava" resolves through the SAME real
# death-save path bottomless_pit/drowning use, but ONLY for a
# character with no real fire resistance/immunity (rules.combat.
# apply_damage_type_modifier's own resistance set) -- a fire-resistant
# character instead just takes real, reduced fire damage and walks
# away, exactly matching Coffee's own "freezing... without frost or
# ice resistences, over heating... with out fire resistences" framing
# (the hazard checks the SAME real resistance system combat already
# has, never a new one invented for this).
_THEME_HAZARD_CHANCE = 0.12

# L2e. Multi-switch puzzle -- a second, independent switch-gated reward
# room on the SAME floor, requiring EVERY switch active at once
# (bot.py's own `multi_switch_gate` kind, shared with the overworld
# evolve pass).
_MULTI_SWITCH_CHANCE = 0.25
_SWITCH_ELEMENTS = ("fire", "cold", "lightning", "force", "radiant", "psychic", "poison", "necrotic", "earth", "physical")

# L2f. Mirror pairs -- a real, Labyrinth-scoped reading of ALTTP's
# Light/Dark World idea: two rooms on the SAME floor, same connection
# shape, deliberately inverted contents.
_MIRROR_PAIR_CHANCE = 0.2

# L3: a real CROSS-floor puzzle -- per Coffee: "intertwined with
# puzzles in all levels... lets players work with their party to get
# to the next level." Two switches, each on a DIFFERENT one of the
# segment's first 4 floors, jointly gate one bonus room on the
# checkpoint (5th) floor. Uses the exact same multi_switch_gate/
# _SWITCH_STATE plumbing L2e already proved chat-scoped, not
# room-scoped -- a switch flipped on floor 2 is visible to a gate on
# floor 5 with no new mechanism at all.
_CROSS_FLOOR_PUZZLE_CHANCE = 0.6

# L3: "each level in the honeycomb being harder and each RNG being
# harder" (Coffee, live) -- raw monster stats already strictly
# increase with floor via bot.py's labyrinth_depth_multiplier, but the
# GENERATION odds themselves stay flat at floor 1's values forever
# without this: a floor 80 side room shouldn't roll hazards/monster
# counts/modifiers at the exact same rate floor 1 does. Every rate
# below grows linearly with floor and caps well short of certainty, so
# early floors keep their original, already-tuned feel.
def _scaled_chance(base: float, floor: int, per_floor: float, cap: float) -> float:
    return min(base + per_floor * (floor - 1), cap)


def _depth_weighted_modifier(floor: int, rng: random.Random) -> str | None:
    if rng.random() >= _scaled_chance(_MODIFIER_CHANCE, floor, 0.004, 0.75):
        return None
    # Weights shift from flat (floor 1) toward "dangerous"/"frenzied"
    # as depth grows -- deeper floors are more often the harsher
    # modifiers, never exclusively (still real variety at any depth).
    lean = min(0.01 * (floor - 1), 3.0)
    weights = [1.0 + lean, 1.0, 1.0, 1.0 + lean]  # frenzied, lightless, bountiful, dangerous
    return rng.choices(FLOOR_MODIFIERS, weights=weights, k=1)[0]


def _labyrinth_monster_pool(campaign: dict) -> list[str]:
    """Wide, catalog-agnostic pool -- floor depth is unbounded, so monster CHOICE never depends on a level band (that's what bot.py's depth-multiplier is for); only real exclusions (Remnants, level-less headline bosses) apply, reusing dungeon_evolve's own filtering with the widest possible band."""
    return _candidate_monsters(campaign, (0, 10_000), boss=False)


def segment_number_for_floor(floor: int) -> int:
    return (floor - 1) // SEGMENT_SIZE + 1


def segment_start_floor(segment: int) -> int:
    return (segment - 1) * SEGMENT_SIZE + 1


def is_checkpoint_floor(floor: int) -> bool:
    """The last floor of its segment -- the real safe waypoint (see _build_checkpoint_room)."""
    return floor % SEGMENT_SIZE == 0


def _hub_id(floor: int) -> str:
    return f"f{floor}_hub"


def _stairs_id(floor: int) -> str:
    return f"f{floor}_stairs"


def _checkpoint_id(floor: int) -> str:
    return f"f{floor}_checkpoint"


def _spiral_cells():
    """
    Yields (0, 0), then every integer cell at Chebyshev distance 1, then
    distance 2, and so on, each ring ordered by angle -- a simple,
    deterministic, collision-free way to place an arbitrary number of
    rooms around a hub with no risk of ever running out of cells. Each
    floor gets its OWN independent spiral (its own hub at its own
    local (0, 0)) -- floors are real, separately-drawn maps (see
    map_render.render_labyrinth_map's floor switcher), not one shared
    coordinate space.
    """
    yield (0, 0)
    radius = 1
    while True:
        ring = [
            (x, y)
            for x in range(-radius, radius + 1)
            for y in range(-radius, radius + 1)
            if max(abs(x), abs(y)) == radius
        ]
        ring.sort(key=lambda c: math.atan2(c[1], c[0]))
        yield from ring
        radius += 1


def _assign_grid_positions(rooms: dict, hub_room_id: str) -> None:
    """
    Hub at the origin; every other room placed by a real BFS walk over
    its ACTUAL connections/locked_connections graph, one cardinal step
    (N/S/E/W) from its real parent whenever a free cell is available.

    Real fix (2026-09-03, alongside the branching-topology algorithm
    above): the old version placed rooms in a blind Chebyshev spiral,
    completely ignorant of which rooms actually connect to which --
    fine for the OLD pure hub-and-spoke shape (every room was hub-
    adjacent anyway, so the exact cell barely mattered), but wrong now
    that real branches run several rooms deep. A child room could land
    anywhere on the spiral regardless of its real parent, so map_render.
    render_labyrinth_map's new per-edge door/wall drawing (a gap where
    two rooms are ACTUALLY connected) would show doors leading nowhere
    sensible. BFS placement keeps a branch visually contiguous, the way
    every real reference map studied actually reads.
    """
    rooms[hub_room_id]["grid_position"] = {"x": 0, "y": 0}
    occupied = {(0, 0): hub_room_id}
    visited = {hub_room_id}
    queue = [hub_room_id]
    directions = [(0, 1), (0, -1), (1, 0), (-1, 0)]
    while queue:
        current_id = queue.pop(0)
        cx, cy = rooms[current_id]["grid_position"]["x"], rooms[current_id]["grid_position"]["y"]
        neighbor_ids = list(rooms[current_id].get("connections", [])) + list(rooms[current_id].get("locked_connections", {}).keys())
        for neighbor_id in neighbor_ids:
            if neighbor_id in visited or neighbor_id not in rooms:
                continue
            placed = False
            for dx, dy in directions:
                if (cx + dx, cy + dy) not in occupied:
                    rooms[neighbor_id]["grid_position"] = {"x": cx + dx, "y": cy + dy}
                    occupied[(cx + dx, cy + dy)] = neighbor_id
                    placed = True
                    break
            if not placed:
                # Every cardinal neighbor of the real parent is already
                # taken (a tight loop back near the hub) -- fall back to
                # the nearest free cell outward from the parent, same
                # spiral search the old placement used globally, just
                # centered on this specific room instead of the origin.
                for dx, dy in _spiral_cells():
                    if (cx + dx, cy + dy) not in occupied:
                        rooms[neighbor_id]["grid_position"] = {"x": cx + dx, "y": cy + dy}
                        occupied[(cx + dx, cy + dy)] = neighbor_id
                        break
            visited.add(neighbor_id)
            queue.append(neighbor_id)

    # Defensive: any room genuinely unreachable from the hub via a real
    # connection (should never happen by construction, but a floor with
    # a real bug elsewhere must still render instead of crashing) gets
    # placed on the outer spiral rather than silently missing a
    # grid_position (map_render.render_labyrinth_map would KeyError).
    remaining = [rid for rid in rooms if rid not in visited]
    if remaining:
        spiral = _spiral_cells()
        for x, y in spiral:
            if not remaining:
                break
            if (x, y) not in occupied:
                rid = remaining.pop(0)
                rooms[rid]["grid_position"] = {"x": x, "y": y}
                occupied[(x, y)] = rid


def _milestone_item_for_floor(floor: int) -> str:
    for lo, hi, item_id in _MILESTONE_ITEM_BANDS:
        if lo <= floor <= hi:
            return item_id
    return _MILESTONE_ITEM_BANDS[-1][2]


def _new_side_room(floor: int, hub_id: str, index: int, pool: list[str], modifier: str | None, rng: random.Random, theme: dict) -> dict:
    room_id = f"f{floor}_r{index}"
    monster_cap = min(2 + floor // 10, 5)  # depth-scaled RNG: floor 1-9 rolls 0-2, floor 40+ rolls 0-5
    monsters = rng.sample(pool, k=min(rng.randint(0, monster_cap), len(pool))) if pool else []
    # Real theme variety (2026-09-02, per Coffee): the room's own name
    # and base description come from the segment's chosen theme's own
    # real pool, cycled deterministically by index -- always suffixed
    # with a real room number so names stay genuinely unique even once
    # a deep floor's side-room count exceeds the theme's own name pool
    # (real bug precedent: two identically-named mirror-pair rooms once
    # made a typed destination genuinely ambiguous -- never repeat that).
    # room_descriptions is the SAME LENGTH as room_names and indexed
    # identically (2026-09-02, real live report, Coffee: descriptions
    # weren't "good enough" -- root cause was every side room in a theme
    # sharing one single flavor sentence regardless of its own distinct
    # name; each name now has its own matching, distinct description).
    name_index = index % len(theme["room_names"])
    room = {
        "id": room_id, "floor": floor, "name": f"{theme['room_names'][name_index]} {index + 1}",
        "description": theme["room_descriptions"][name_index],
        "connections": [hub_id], "monsters": monsters, "modifier": modifier,
    }
    # L2d/L3: a visible hazard, honestly stated in the description
    # itself. Checked in a fixed priority order, mutually exclusive
    # (one hazard per room keeps the room text simple and keeps any
    # single lethal trap always genuinely rare):
    # 1. the segment's own theme-signature hazard (lava/acid/freezing/
    #    overheating/arcing_current/drowning) -- real per-theme flavor.
    # 2. the generic lethal pool (spike_pit/bottomless_pit).
    # 3. the generic mundane pool (collapsing_floor/gas_vent).
    theme_hazard = theme.get("signature_hazard") if rng.random() < 0.6 else theme.get("secondary_hazard", theme.get("signature_hazard"))
    if theme_hazard and rng.random() < _scaled_chance(_THEME_HAZARD_CHANCE, floor, 0.0015, 0.35):
        if theme_hazard != "drowning" or floor >= _BOTTOMLESS_PIT_MIN_FLOOR:
            room["hazard"] = theme_hazard
            room["description"] += " " + HAZARD_FLAVOR[theme_hazard]
    if "hazard" not in room and rng.random() < _scaled_chance(_LETHAL_HAZARD_CHANCE, floor, 0.001, 0.18):
        lethal_pool = [k for k in _LETHAL_HAZARD_KINDS if k != "bottomless_pit" or floor >= _BOTTOMLESS_PIT_MIN_FLOOR]
        hazard = rng.choice(lethal_pool)
        room["hazard"] = hazard
        room["description"] += " " + HAZARD_FLAVOR[hazard]
    elif "hazard" not in room and rng.random() < _scaled_chance(_HAZARD_CHANCE, floor, 0.003, 0.45):
        hazard = rng.choice(_MUNDANE_HAZARD_KINDS)
        room["hazard"] = hazard
        room["description"] += " " + HAZARD_FLAVOR[hazard]
    # Real chest, doubled loot/gold under a "bountiful" floor.
    if rng.random() < 0.3:
        bountiful = modifier == "bountiful"
        room.setdefault("lockables", []).append({
            "id": f"f{floor}_cache_{index}", "kind": "chest", "name": "a real, hastily-buried cache",
            "loot": {"healing_potion": rng.randint(2, 4) if bountiful else rng.randint(1, 2)},
            "gold": (rng.randint(20, 80) * floor) * (2 if bountiful else 1),
        })
    return room, room_id


def _build_checkpoint_room(floor: int, hub_id: str, rng: random.Random, theme: dict) -> dict:
    """
    L3: the segment's own real safe waypoint (Coffee: "make this a safe
    spot with a waypoint to fast travel to later on"). bot.py's
    _resolve_labyrinth_checkpoint fires the actual one-time rewards
    (full heal, full spell-slot refill, an ASI point, an achievement)
    the first time a party arrives; this room's own data just marks it
    `is_checkpoint` and gives it a real wandering-trader shop (the
    exact same reusable NPC/shop dungeon_evolve.py's own Part B4
    already authored for large overworld dungeons).
    """
    item_id = _milestone_item_for_floor(floor)
    return {
        "id": _checkpoint_id(floor), "floor": floor, "is_checkpoint": True,
        "name": f"{theme['name']} -- A Waystation (Floor {floor})",
        "description": theme["checkpoint_description"],
        "connections": [hub_id], "monsters": [],
        "npcs": ["wandering_dungeon_trader"], "shop": "wandering_traders_pack",
        "lockables": [{
            "id": f"f{floor}_checkpoint_cache", "kind": "chest", "name": "a real, heavily reinforced vault",
            "loot": {item_id: 1}, "gold": rng.randint(100, 300) * floor,
        }],
    }


def generate_floor(campaign: dict, floor: int, rng: random.Random, pool: list[str] | None = None, theme: dict | None = None) -> dict:
    """
    Builds ONE floor's own rooms (hub + a stairs-or-checkpoint room +
    side rooms + this floor's own single-floor extras: L2c's vault no
    longer lives here -- see _build_checkpoint_room -- but L2e's
    same-floor multi-switch and L2f's mirror pair still do). Returns
    {"rooms": {...}, "hub_room_id": ..., "connector_room_id": ...,
    "modifier": ..., "is_checkpoint": bool}. `connector_room_id` is a
    plain "Stair Down" room for a non-checkpoint floor, or the real
    checkpoint room for the segment's 5th floor -- generate_segment
    below is what actually links `connector_room_id` onward to the
    next floor (or, for a checkpoint, leaves it for bot.py's own
    "go deeper" action to break the segment and build the next one).

    `theme` (2026-09-02, Phase L3, per Coffee: "lots of themes and area
    types... an alternate universe/another dimension"): one of
    LABYRINTH_THEMES, normally chosen ONCE per segment by generate_
    segment below and passed down to every one of its floors so a
    whole 5-floor honeycomb reads as one coherent place. Defaults to a
    real random pick only for direct/standalone calls (e.g. tests) --
    generate_segment never relies on this default itself.
    """
    theme = theme or _pick_theme(rng)
    pool = pool if pool is not None else _labyrinth_monster_pool(campaign)
    rooms: dict[str, dict] = {}
    modifier = _depth_weighted_modifier(floor, rng)
    checkpoint = is_checkpoint_floor(floor)

    hub_id = _hub_id(floor)
    hub = {
        "id": hub_id, "floor": floor, "name": f"{theme['name']} -- Floor {floor}",
        "description": theme["hub_description"],
        "connections": [], "monsters": [], "modifier": modifier,
    }
    rooms[hub_id] = hub

    # Real branching (2026-09-03, see _BRANCH_DEPTH_WEIGHTS' own module-
    # level docstring for the full research this replaces): each branch
    # ROOT still connects straight to the hub (unchanged shape, so the
    # existing L2e multi-switch/L2f mirror-pair features below -- which
    # both key off `room_ids`, the flat hub-adjacent set -- keep working
    # exactly as before), but a branch can now run 0-2 rooms DEEPER, a
    # real path rather than a single dead-end hop.
    side_room_max = _SIDE_ROOM_COUNT_RANGE[1] + min(floor // 15, 3)  # depth-scaled RNG: more chambers per floor, deeper in
    num_branch_roots = rng.randint(_SIDE_ROOM_COUNT_RANGE[0], side_room_max)
    room_ids = []
    branch_chains = []
    next_index = 0
    for _ in range(num_branch_roots):
        room, room_id = _new_side_room(floor, hub_id, next_index, pool, modifier, rng, theme)
        next_index += 1
        hub["connections"].append(room_id)
        rooms[room_id] = room
        room_ids.append(room_id)
        chain = [room_id]
        depth = rng.choices(*_BRANCH_DEPTH_WEIGHTS)[0]
        parent_id = room_id
        for _ in range(depth):
            # _new_side_room's own 2nd positional param is only ever used
            # to set "connections": [that_id] -- passing the real parent
            # (not hub_id) is what actually builds a genuine deeper path.
            deep_room, deep_room_id = _new_side_room(floor, parent_id, next_index, pool, modifier, rng, theme)
            next_index += 1
            rooms[parent_id]["connections"].append(deep_room_id)
            rooms[deep_room_id] = deep_room
            chain.append(deep_room_id)
            parent_id = deep_room_id
        branch_chains.append(chain)

    # The main path: the single longest branch leads to the real stairs
    # (Metazelda/every reference map's own shape -- real content sits
    # BETWEEN the entrance and the exit, never a direct hop). Ties break
    # on the first-generated branch, so the same seed always reproduces
    # the same layout.
    main_chain = max(branch_chains, key=len)
    main_path_end = main_chain[-1]

    if checkpoint:
        connector = _build_checkpoint_room(floor, main_path_end, rng, theme)
    else:
        connector = {
            "id": _stairs_id(floor), "floor": floor, "name": "A Stair Down",
            "description": "A real stairway, always open -- however far you've come, the way deeper never asks permission.",
            "connections": [main_path_end], "monsters": [], "modifier": modifier,
        }
    rooms[main_path_end]["connections"].append(connector["id"])
    rooms[connector["id"]] = connector

    # A real mini-boss guarding the way to the stairs -- always on the
    # MAIN path, never a side branch, so reaching the exit means facing
    # it (every reference map studied placed at least one mini-boss
    # partway along the real critical path, not tucked in an optional
    # room). Still a real catalog monster from this floor's own pool,
    # just alone in its room and flagged for narration/map-icon use --
    # never an invented enemy.
    miniboss_candidates = [rid for rid in main_chain if pool]
    if miniboss_candidates and rng.random() < _scaled_chance(_MINIBOSS_CHANCE, floor, 0.002, 0.6):
        miniboss_room_id = rng.choice(miniboss_candidates)
        strongest = max(pool, key=lambda mk: (campaign["monsters"].get(mk) or {}).get("hp_max", 0))
        rooms[miniboss_room_id]["monsters"] = [strongest]
        rooms[miniboss_room_id]["is_miniboss_room"] = True
        rooms[miniboss_room_id]["description"] += " Something far stronger than the rest of this floor is waiting here."

    # Real branch-gating, guaranteed solvable BY CONSTRUCTION (Metazelda's
    # own real technique, see the module-level research note above): a
    # non-main branch can be sealed behind a switch placed in a
    # DIFFERENT, already-generated, unlocked branch. Reuses the exact
    # `multi_switch_gate` mechanism L2e already ships (a single required
    # switch here, not two) rather than a bare `switch` referenced
    # directly by `locked_connections` -- confirmed by direct code read
    # of bot.py's own `_lockable_is_open` that a bare switch is only
    # ever checked against the SAME room's own `lockables`, so a truly
    # remote single-switch gate would silently never open; multi_switch_
    # gate's own `requires` list is the one real mechanism that checks
    # `_SWITCH_STATE` globally by id regardless of which room the switch
    # itself lives in, and it's already proven safe by L2e/generate_
    # segment's own cross-floor puzzle.
    other_chains = [c for c in branch_chains if c is not main_chain]
    if len(other_chains) >= 2 and rng.random() < _scaled_chance(_BRANCH_GATE_CHANCE, floor, 0.002, 0.55):
        gated_chain, switch_chain = rng.sample(other_chains, 2)
        gate_room_id = gated_chain[0]
        switch_room_id = switch_chain[0]
        element = rng.choice(_SWITCH_ELEMENTS)
        switch_id = f"f{floor}_branchswitch"
        rooms[switch_room_id].setdefault("lockables", []).append({
            "id": switch_id, "kind": "switch", "name": f"a {element} crystal", "element": element,
        })
        hub["connections"].remove(gate_room_id)
        hub.setdefault("locked_connections", {})[gate_room_id] = f"f{floor}_branchgate"
        hub.setdefault("lockables", []).append({
            "id": f"f{floor}_branchgate", "kind": "multi_switch_gate", "name": "a real sealed archway",
            "requires": [switch_id],
        })
        rooms[gate_room_id]["description"] += " A real elemental ward seals this path -- something elsewhere on this floor must open it."
        # L2e/L2f below both pick freely from `room_ids` -- excluding the
        # room this pass just locked keeps them from compounding an
        # unrelated puzzle onto a room the party can't reach yet.
        room_ids = [rid for rid in room_ids if rid != gate_room_id]

    # L2e: a second, independent switch-gated reward, requiring EVERY
    # switch active at once -- deliberately placed in two side rooms
    # that already exist, never the hub/connector.
    if room_ids and len(room_ids) >= 2 and rng.random() < _scaled_chance(_MULTI_SWITCH_CHANCE, floor, 0.003, 0.6):
        switch_room_ids = rng.sample(room_ids, 2)
        switch_lockable_ids = []
        for j, rid in enumerate(switch_room_ids):
            element = rng.choice(_SWITCH_ELEMENTS)
            switch_id = f"f{floor}_switch_{j}"
            rooms[rid].setdefault("lockables", []).append({
                "id": switch_id, "kind": "switch", "name": f"a {element} crystal", "element": element,
            })
            switch_lockable_ids.append(switch_id)
        gate_room_id = f"f{floor}_gate_reward"
        gate_room = {
            "id": gate_room_id, "floor": floor, "name": "Labyrinth -- A Sealed Archway",
            "description": "The archway won't budge -- whatever opens it isn't here in this room.",
            "connections": [hub_id], "monsters": [], "modifier": modifier,
            "lockables": [{
                "id": f"f{floor}_gate_cache", "kind": "chest", "name": "a real cache behind the archway",
                "loot": {"healing_potion": rng.randint(1, 2)}, "gold": rng.randint(60, 150) * floor,
            }],
        }
        rooms[gate_room_id] = gate_room
        hub.setdefault("locked_connections", {})[gate_room_id] = f"f{floor}_multi_gate"
        hub.setdefault("lockables", []).append({
            "id": f"f{floor}_multi_gate", "kind": "multi_switch_gate", "name": "a real sealed archway",
            "requires": switch_lockable_ids,
        })

    # L2f: a real mirror pair -- same connection shape (both are plain
    # hub-adjacent side rooms already), deliberately inverted contents.
    if pool and len(room_ids) >= 2 and rng.random() < _MIRROR_PAIR_CHANCE:
        monster_room_id, chest_room_id = rng.sample(room_ids, 2)
        strongest = max(pool, key=lambda mk: (campaign["monsters"].get(mk) or {}).get("hp_max", 0))
        # Numbered so both stay thematically identical but are always
        # individually nameable -- two rooms sharing the exact same
        # name made a typed "go to A Mirrored Chamber" genuinely
        # ambiguous (real bug, found live 2026-09-02).
        monster_room = rooms[monster_room_id]
        monster_room["monsters"] = [strongest]
        monster_room["mirror_twin"] = chest_room_id
        monster_room["name"] = "Labyrinth -- A Mirrored Chamber (I)"
        monster_room["description"] = "This room feels like it's happening twice, somewhere else on this same floor."
        chest_room = rooms[chest_room_id]
        chest_room["monsters"] = []
        chest_room["mirror_twin"] = monster_room_id
        chest_room["name"] = "Labyrinth -- A Mirrored Chamber (II)"
        chest_room["description"] = "This room feels like it's happening twice, somewhere else on this same floor."
        chest_room["lockables"] = [{
            "id": f"f{floor}_mirror_cache", "kind": "chest", "name": "an unguarded, matching cache",
            "loot": {"healing_potion": rng.randint(1, 2)}, "gold": rng.randint(60, 150) * floor,
        }]

    # Real warp shortcut (2026-09-03, per Coffee: "do all of them" --
    # warps, a real Zelda reference mechanic, after Level 6/Level 7's
    # own maps both showed one linking two distant rooms directly).
    # Picked from two DIFFERENT branches' own deepest room (the most
    # "interesting" real content to shortcut between) -- never the hub
    # or connector, so a warp is always a genuine bonus link, never a
    # way to skip the real critical path outright. Stored separately
    # from `connections` (a warp's own map line is drawn differently --
    # straight across the floor, not a doorway between grid-adjacent
    # cells) rather than pretending it's an ordinary lateral exit.
    warp_candidates = [c[-1] for c in branch_chains if c is not main_chain and len(c) >= 1]
    if len(warp_candidates) >= 2 and rng.random() < _scaled_chance(_WARP_CHANCE, floor, 0.002, 0.5):
        warp_a, warp_b = rng.sample(warp_candidates, 2)
        rooms[warp_a].setdefault("warps", []).append(warp_b)
        rooms[warp_b].setdefault("warps", []).append(warp_a)
        rooms[warp_a]["description"] += " A warp shimmers faintly in the corner -- it leads somewhere else on this floor."
        rooms[warp_b]["description"] += " A warp shimmers faintly in the corner -- it leads somewhere else on this floor."

    # Real Eagle's Tower-style structural puzzle (2026-09-03): solving
    # it doesn't just open one new door, it ALSO seals a previously-open
    # dead-end branch elsewhere on the floor -- "the floor's structure
    # shifts beneath you." Only ever targets a genuine LEAF of a non-
    # main, non-gated branch (never anything hosting a switch another
    # gate depends on), so this can never break the real solvable-by-
    # construction guarantee the rest of this generator relies on.
    sealable_candidates = [
        c for c in branch_chains
        if c is not main_chain and len(c) >= 1
        and not any(lk.get("kind") == "switch" for lk in rooms[c[0]].get("lockables", []))
    ]
    if len(sealable_candidates) >= 1 and other_chains and rng.random() < _scaled_chance(_COLLAPSE_PUZZLE_CHANCE, floor, 0.001, 0.4):
        seal_chain = rng.choice(sealable_candidates)
        seal_leaf = seal_chain[-1]
        seal_parent = hub_id if len(seal_chain) == 1 else seal_chain[-2]
        # Switches for THIS trigger live in a real, unrelated, already-
        # reachable branch -- never inside the branch being sealed.
        switch_source_chains = [c for c in other_chains if c is not seal_chain]
        if switch_source_chains:
            switch_room_id = rng.choice(switch_source_chains)[0]
            element = rng.choice(_SWITCH_ELEMENTS)
            trigger_switch_id = f"f{floor}_collapse_switch"
            rooms[switch_room_id].setdefault("lockables", []).append({
                "id": trigger_switch_id, "kind": "switch", "name": f"a shuddering {element} crystal", "element": element,
            })
            trigger_id = f"f{floor}_collapse_trigger"
            rooms[seal_parent].setdefault("lockables", []).append({
                "id": trigger_id, "kind": "multi_switch_gate", "name": "a real structural trigger",
                "requires": [trigger_switch_id],
            })
            rooms[seal_parent].setdefault("collapsing_connections", {})[seal_leaf] = trigger_id
            rooms[seal_parent]["description"] += " Something here feels structurally unstable -- like it's rigged to give way."
            # The real reward: a genuine new shortcut opens elsewhere on
            # the floor the instant this same trigger fires, so solving
            # it is a real trade, not just a loss.
            shortcut_pair = [c[-1] for c in other_chains if c is not seal_chain]
            if len(shortcut_pair) >= 2:
                shortcut_a, shortcut_b = rng.sample(shortcut_pair, 2)
                rooms[shortcut_a].setdefault("locked_connections", {})[shortcut_b] = trigger_id
                rooms[shortcut_a].setdefault("lockables", []).append({
                    "id": f"{trigger_id}_echo_{shortcut_a}", "kind": "multi_switch_gate",
                    "name": "a newly-opened shortcut", "requires": [trigger_switch_id],
                })
                # locked_connections looks up its OWN lockable id, not the
                # shared trigger's -- point it at the per-room echo just
                # added so _lockable_is_open finds it locally, same real
                # constraint the branch-gate fix above already worked
                # around once.
                rooms[shortcut_a]["locked_connections"][shortcut_b] = f"{trigger_id}_echo_{shortcut_a}"

    _assign_grid_positions(rooms, hub_id)
    return {
        "rooms": rooms, "hub_room_id": hub_id, "connector_room_id": connector["id"],
        "modifier": modifier, "is_checkpoint": checkpoint,
    }


def generate_segment(campaign: dict, segment: int, rng: random.Random) -> dict:
    """
    L3: builds SEGMENT_SIZE (5) real, interconnected floors at once --
    the actual unit of persistence (db.labyrinth_runs' rooms_json holds
    every room from every floor of the CURRENT live segment, all at
    once, discarded together only when the party breaks the waypoint
    at the checkpoint floor and moves on). Floor k's connector room
    (a plain stairs room, floors 1-4) `descends_to` floor k+1's hub,
    and floor k+1's hub `ascends_to` back to it -- real, walkable,
    reusing the exact same field names the overworld's own multi-story
    buildings already use, so bot.py's movement code barely has to
    change to support it.

    Returns {"rooms": {...every room, every floor...}, "entry_room_id":
    <floor start's hub>, "checkpoint_room_id": <floor end's checkpoint>,
    "segment": segment, "theme": <the one LABYRINTH_THEMES dict chosen
    for every floor in this segment>}.
    """
    pool = _labyrinth_monster_pool(campaign)
    start_floor = segment_start_floor(segment)
    floors = list(range(start_floor, start_floor + SEGMENT_SIZE))
    theme = _pick_theme(rng)

    all_rooms: dict[str, dict] = {}
    floor_data_by_floor: dict[int, dict] = {}
    for floor in floors:
        floor_data = generate_floor(campaign, floor, rng, pool=pool, theme=theme)
        all_rooms.update(floor_data["rooms"])
        floor_data_by_floor[floor] = floor_data

    for floor in floors[:-1]:
        this_connector = all_rooms[floor_data_by_floor[floor]["connector_room_id"]]
        next_hub = all_rooms[floor_data_by_floor[floor + 1]["hub_room_id"]]
        this_connector["descends_to"] = next_hub["id"]
        next_hub["ascends_to"] = this_connector["id"]

    # L3: a real cross-floor puzzle -- two switches, each on a
    # DIFFERENT one of this segment's first 4 floors, jointly gate one
    # bonus room on the checkpoint floor. Reuses the exact same
    # multi_switch_gate kind/plumbing L2e already proved chat-scoped.
    if len(floors) >= 3 and rng.random() < _CROSS_FLOOR_PUZZLE_CHANCE:
        source_floors = rng.sample(floors[:-1], 2)
        switch_lockable_ids = []
        for j, floor in enumerate(source_floors):
            side_room_ids = [
                rid for rid, room in floor_data_by_floor[floor]["rooms"].items()
                if room is not all_rooms[floor_data_by_floor[floor]["hub_room_id"]]
                and room["id"] != floor_data_by_floor[floor]["connector_room_id"]
            ]
            if not side_room_ids:
                continue
            target_room = all_rooms[rng.choice(side_room_ids)]
            element = rng.choice(_SWITCH_ELEMENTS)
            switch_id = f"seg{segment}_switch_{j}"
            target_room.setdefault("lockables", []).append({
                "id": switch_id, "kind": "switch", "name": f"a distant {element} crystal, humming faintly", "element": element,
            })
            switch_lockable_ids.append(switch_id)
        if len(switch_lockable_ids) == 2:
            checkpoint_hub = all_rooms[floor_data_by_floor[floors[-1]]["hub_room_id"]]
            gate_room_id = f"seg{segment}_gate_reward"
            gate_room = {
                "id": gate_room_id, "floor": floors[-1], "name": "Labyrinth -- A Waystation Vault",
                "description": (
                    "A second vault, sealed tight -- whatever opens it is scattered across the floors "
                    "above, not in this room."
                ),
                "connections": [checkpoint_hub["id"]], "monsters": [],
                "lockables": [{
                    "id": f"seg{segment}_gate_cache", "kind": "chest", "name": "a real cache, shared by the whole party's effort",
                    "loot": {"superior_healing_potion": rng.randint(1, 2)}, "gold": rng.randint(200, 500) * floors[-1],
                }],
            }
            # This room was added AFTER _assign_grid_positions already
            # ran once per floor inside generate_floor -- give it a
            # real, collision-free cell on the checkpoint floor's own
            # local grid too, or map_render.render_labyrinth_map's
            # {x, y} lookup crashes on this one room (real bug, caught
            # by this file's own map test).
            occupied = {
                (r["grid_position"]["x"], r["grid_position"]["y"])
                for r in floor_data_by_floor[floors[-1]]["rooms"].values()
            }
            spiral = _spiral_cells()
            next(spiral)
            for x, y in spiral:
                if (x, y) not in occupied:
                    gate_room["grid_position"] = {"x": x, "y": y}
                    break
            all_rooms[gate_room_id] = gate_room
            checkpoint_hub.setdefault("locked_connections", {})[gate_room_id] = f"seg{segment}_multi_gate"
            checkpoint_hub.setdefault("lockables", []).append({
                "id": f"seg{segment}_multi_gate", "kind": "multi_switch_gate", "name": "a real, second sealed archway",
                "requires": switch_lockable_ids,
            })

    return {
        "rooms": all_rooms,
        "entry_room_id": floor_data_by_floor[start_floor]["hub_room_id"],
        "checkpoint_room_id": floor_data_by_floor[floors[-1]]["connector_room_id"],
        "segment": segment,
        "theme": theme,
    }
