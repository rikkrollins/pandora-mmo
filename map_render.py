"""
map_render.py
Task #11, per Coffee (2026-08-09, Development-topic screenshot):
"This does not look like a map. I want an accurate map, you can use
sprites similar to the formation image. Try to make a map and then
use circles and names with labels." Same root cause and same fix
shape as battle_render.py's own module docstring: _do_show_visual_map
previously handed a text description of visited location names to
Pollinations.ai and got back atmospheric painted art -- real, but
never an accurate schematic, since no current image-generation model
reliably renders legible text labels. This renders a real, LOCAL,
deterministic map instead: no network call, positions computed purely
from the real location-connection graph, nothing invented.

Fog of war (mirrors _do_show_map's existing text-map convention
exactly, so both renderings always agree with each other):
- A VISITED location is a real, solid node with its real name, real
  connections to other VISITED locations drawn as lines, and a small
  "+N" badge if it has real connections leading somewhere unvisited
  (never a line drawn to an unvisited destination -- that would leak
  the connection itself, which is real spoiler information).
- A location only REVEALED (via a "map" item, see bot.py's
  _do_use_item "map" branch) but not yet actually visited is never
  placed on the graph at all -- it has no known connections to anchor
  a position by. Listed by name only in a separate legend line, same
  "still needs to be physically visited to unlock connections" rule
  _do_show_map already enforces.

Real cardinal directions (2026-08-09, Coffee, Development-topic
screenshot: standing at Stonearch Bridge, "to the south of me is
supposed to be the weeping well but on the map it doesn't show that"):
campaign.json locations carry a real, structured `directions` dict
({"south": "the_weeping_well", ...}) alongside the plain `connections`
list on 73/82 locations (132/148 real edges campaign-wide) -- this was
missed in the first pass (only the free-prose `sensory_connections`
text was checked, which turned out to have almost no parseable
directional wording). The layout now seeds itself from these real
compass directions via a breadth-first walk from a deterministic root,
and keeps a real per-iteration corrective force during the force-
directed relaxation pass so a known "south" relationship stays visually
south even after repulsion/spacing adjustments. The ~11% of edges with
no declared direction are never assigned a fake one -- they're seeded
near their nearest already-placed neighbor and positioned purely by
the undirected spring/repulsion forces, same as before this change.

One layer (surface/underground/sky) is rendered per image; bot.py is
responsible for offering buttons to switch layers when the character
has real content in more than one.
"""
import io
import os
import random

from PIL import Image, ImageDraw, ImageFont

CANVAS_WIDTH = 900
CANVAS_HEIGHT = 700
_MAX_CANVAS_WIDTH = 1600
_MAX_CANVAS_HEIGHT = 1300
# A layer explored heavily (underground has 43 real locations total) crams
# labels into unreadable overlap on a fixed canvas -- confirmed visually
# rendering all 43 at once. Canvas grows with node count instead, capped
# at _MAX_CANVAS_*, so both a fresh character's 2-3 nodes and a
# long-playthrough character's several dozen stay legible.
_NODES_PER_SCALE_STEP = 6
_SCALE_STEP_PCT = 0.12

_BG_TOP = (222, 202, 165)
_BG_BOTTOM = (196, 172, 130)
_NODE_FILL = (90, 60, 30)
_NODE_CURRENT_FILL = (168, 40, 40)
_NODE_OUTLINE = (40, 25, 10)
_EDGE_COLOR = (90, 60, 30, 160)
_LABEL_COLOR = (30, 18, 8)
_UNEXPLORED_BADGE_FILL = (212, 175, 55)
_UNEXPLORED_BADGE_TEXT = (40, 25, 10)
_FLOOR_BADGE_FILL = (110, 140, 165)
_FLOOR_BADGE_TEXT = (20, 30, 40)
_LEGEND_COLOR = (60, 45, 25)

_NODE_RADIUS = 16
_NODE_RADIUS_CURRENT = 20

_FONT_DIR = "/usr/share/fonts/truetype/dejavu"
_FONT_BOLD_PATH = os.path.join(_FONT_DIR, "DejaVuSans-Bold.ttf")
_FONT_REGULAR_PATH = os.path.join(_FONT_DIR, "DejaVuSans.ttf")

_LAYOUT_SEED = 1337
_LAYOUT_ITERATIONS = 400
_LAYOUT_AREA = 1000.0  # arbitrary units, normalized to the canvas afterward
_CENTER_GRAVITY = 0.4  # keeps the whole graph compact; _contain_disconnected_components below
                        # is a generous safety net for the rare component gravity alone can't reach
_MAX_REVEALED_IN_LEGEND = 6

_DIAG = 0.7071067811865476  # sqrt(2)/2, unit-length diagonal component
_DIRECTION_VECTORS = {
    "north": (0.0, -1.0), "south": (0.0, 1.0), "east": (1.0, 0.0), "west": (-1.0, 0.0),
    "northeast": (_DIAG, -_DIAG), "northwest": (-_DIAG, -_DIAG),
    "southeast": (_DIAG, _DIAG), "southwest": (-_DIAG, _DIAG),
    # "up"/"down" are real direction words too (confirmed against every
    # actual campaign.json `directions` value, not assumed) -- used
    # within a single layer for a room stacked above/below another,
    # e.g. inside a multi-level dungeon sub-area. Missing these meant
    # every such sub-cluster's internal layout was entirely unseeded by
    # real data -- treated the same as north/south (visually "up" =
    # higher on the rendered map, matching how a player would expect a
    # vertical shaft to read).
    "up": (0.0, -1.0), "down": (0.0, 1.0),
}
_OPPOSITE_DIRECTION = {
    "north": "south", "south": "north", "east": "west", "west": "east",
    "northeast": "southwest", "southwest": "northeast",
    "northwest": "southeast", "southeast": "northwest",
    "up": "down", "down": "up",
}
_DIRECTION_STEP = 220.0  # real-direction seed distance, roughly matches the layout's natural node spacing
_DIRECTION_FORCE = 0.015  # per-iteration corrective pull keeping a directed edge visually on-axis


def _load_font(size: int, bold: bool = False) -> ImageFont.FreeTypeFont:
    path = _FONT_BOLD_PATH if bold else _FONT_REGULAR_PATH
    try:
        return ImageFont.truetype(path, size)
    except OSError:
        return ImageFont.load_default()


def _truncate_name(name: str, limit: int = 22) -> str:
    return name if len(name) <= limit else name[: limit - 1] + "…"


def _bidirectional_directions(layer_locations: dict, node_set: set[str]) -> dict[str, dict[str, str]]:
    """
    {loc_id: {direction: neighbor_loc_id}}, restricted to node_set (the
    real visited-here set) on both ends. Real campaign.json data often
    only declares a direction from ONE side ("stonearch_bridge" says
    south is "the_weeping_well", but the_weeping_well may not declare
    north back) -- this fills in the logical inverse wherever it's
    genuinely implied and missing (never invents a NEW relationship,
    only the opposite-facing description of one that's already real),
    which roughly doubles how far the BFS seed below can walk using
    only real, declared-or-implied compass data.
    """
    result: dict[str, dict[str, str]] = {nid: {} for nid in node_set}
    for lid in node_set:
        for direction, neighbor in (layer_locations.get(lid, {}).get("directions") or {}).items():
            if neighbor in node_set and direction in _DIRECTION_VECTORS:
                result[lid][direction] = neighbor
    for lid in node_set:
        for direction, neighbor in list(result[lid].items()):
            opposite = _OPPOSITE_DIRECTION[direction]
            if opposite not in result[neighbor]:
                result[neighbor][opposite] = lid
    return result


def _seed_positions_from_directions(node_ids: list[str], bidi_directions: dict[str, dict[str, str]]) -> dict[str, tuple[float, float]]:
    """
    Real compass data in, an initial (x, y) per node out -- a plain
    breadth-first walk from a deterministic root (the lowest-sorted
    loc_id, so the same visited set always seeds the same way
    regardless of who's viewing or where they currently stand),
    placing each newly-reached node at its parent's position plus the
    REAL direction's unit vector. Any node never reached via a real
    directed edge (the ~11% of campaign-wide edges with no declared
    direction, or a node disconnected from the directed portion of the
    graph entirely) is seeded near whichever already-placed neighbor
    it has a plain connection to -- never given a fake direction, just
    a reasonable starting point for the force-directed relaxation
    pass to actually resolve.
    """
    if not node_ids:
        return {}
    root = min(node_ids)
    positions: dict[str, tuple[float, float]] = {root: (0.0, 0.0)}
    queue = [root]
    while queue:
        current = queue.pop(0)
        cx, cy = positions[current]
        for direction, neighbor in bidi_directions.get(current, {}).items():
            if neighbor in positions:
                continue
            vx, vy = _DIRECTION_VECTORS[direction]
            positions[neighbor] = (cx + vx * _DIRECTION_STEP, cy + vy * _DIRECTION_STEP)
            queue.append(neighbor)
    return positions


def _floor_levels(bidi_directions: dict[str, dict[str, str]]) -> dict[str, int]:
    """
    Real per-node floor number for a vertical up/down dungeon chain
    (2026-08-09, Coffee: "F3 - F2 - F1 - B1 - B2 - B3 for floors and
    basements in dungeons"). Grounded entirely in real "up"/"down"
    entries in bidi_directions -- level 0 is the deterministic entry
    point into each such vertical chain (lowest-sorted member), +1 per
    real "up" step, -1 per real "down" step from there. A location
    with no up/down relationship to anything else visited in this
    layer never appears in the result at all -- it isn't part of a
    multi-floor structure, nothing to invent a floor number for.
    """
    vertical_nodes = set()
    for nid, directions in bidi_directions.items():
        for direction, neighbor in directions.items():
            if direction in ("up", "down"):
                vertical_nodes.add(nid)
                vertical_nodes.add(neighbor)

    levels: dict[str, int] = {}
    for start in sorted(vertical_nodes):
        if start in levels:
            continue
        levels[start] = 0
        queue = [start]
        while queue:
            current = queue.pop(0)
            for direction, neighbor in bidi_directions.get(current, {}).items():
                if direction not in ("up", "down") or neighbor not in vertical_nodes or neighbor in levels:
                    continue
                levels[neighbor] = levels[current] + (1 if direction == "up" else -1)
                queue.append(neighbor)
    return levels


def _fill_unseeded_positions(node_ids: list[str], edges: list[tuple[str, str]], positions: dict[str, tuple[float, float]]) -> None:
    """
    Mutates positions in place. Confirmed live (2026-08-09, all-43-
    underground-locations worst case): a real, genuine campaign.json
    shape -- several sub-dungeon clusters (e.g. stonearch_bridge_*,
    wordless_choir_*) are only reachable from the surface via
    descends_to, a separate field this module deliberately never
    treats as an in-layer connection, so they're a REAL, disconnected
    component within this single layer's own connection graph. Every
    such node used to collapse onto the exact same (0.0, 0.0) fallback
    point -- and repulsion between two nodes at an EXACTLY identical
    coordinate is mathematically zero (the push direction is undefined
    when dx=dy=0), so they could never separate from each other for
    the rest of the relaxation. Each disconnected component now gets
    its own distinct, deterministic random anchor instead, with a
    small jitter between chained members of the same component so
    even they don't start exactly coincident.

    The anchor is placed NEAR whatever's already been seeded from real
    direction data (its centroid), not a raw random point anywhere
    across the full layout area -- confirmed live (2026-08-09): an
    anchor picked from the full range could land far outside the
    tightly-packed, real-direction-accurate main cluster's own natural
    coordinate range, which badly skewed _normalize_to_canvas's later
    min/max rescale (the real cluster ends up compressed into a sliver
    of the canvas just to also fit one far-flung random point).
    """
    adjacency: dict[str, list[str]] = {nid: [] for nid in node_ids}
    for a, b in edges:
        adjacency[a].append(b)
        adjacency[b].append(a)

    remaining = {nid for nid in node_ids if nid not in positions}
    rng = random.Random(_LAYOUT_SEED)
    if positions:
        base_x = sum(p[0] for p in positions.values()) / len(positions)
        base_y = sum(p[1] for p in positions.values()) / len(positions)
    else:
        base_x = base_y = _LAYOUT_AREA / 2
    spread = _DIRECTION_STEP * 2.5

    while remaining:
        start = min(remaining)
        component = []
        stack = [start]
        seen = {start}
        while stack:
            nid = stack.pop()
            component.append(nid)
            for neighbor in adjacency[nid]:
                if neighbor in remaining and neighbor not in seen:
                    seen.add(neighbor)
                    stack.append(neighbor)

        placed_this_component = {start: (base_x + rng.uniform(-spread, spread), base_y + rng.uniform(-spread, spread))}
        queue = [start]
        component_set = set(component)
        while queue:
            current = queue.pop(0)
            cx, cy = placed_this_component[current]
            for neighbor in adjacency[current]:
                if neighbor not in component_set or neighbor in placed_this_component:
                    continue
                placed_this_component[neighbor] = (cx + rng.uniform(-40, 40), cy + rng.uniform(-40, 40))
                queue.append(neighbor)

        positions.update(placed_this_component)
        remaining -= component_set


def _compute_layout(
    node_ids: list[str], edges: list[tuple[str, str]], bidi_directions: dict[str, dict[str, str]] | None = None,
) -> dict[str, tuple[float, float]]:
    """
    Fruchterman-Reingold force-directed relaxation, pure Python, no
    numpy/networkx -- same "no new heavy dependency" spirit as
    battle_render.py. Deterministic: node_ids/edges are always iterated
    in the SAME order the caller already sorted them in, and random.Random
    is only ever used as a last-resort fallback (no real direction data
    at all) so the same visited set always lays out the same way.

    When bidi_directions is given, positions are seeded from real
    compass data (_seed_positions_from_directions) instead of pure
    random placement, and a real per-iteration corrective force keeps
    each directed edge visually on-axis (e.g. a real "south"
    relationship doesn't drift into looking like "east" once repulsion
    and spacing forces start moving things around).
    """
    if not node_ids:
        return {}
    if len(node_ids) == 1:
        return {node_ids[0]: (_LAYOUT_AREA / 2, _LAYOUT_AREA / 2)}

    directed_edges: list[tuple[str, str, float, float]] = []
    if bidi_directions:
        pos_dict = _seed_positions_from_directions(node_ids, bidi_directions)
        _fill_unseeded_positions(node_ids, edges, pos_dict)
        pos = {nid: list(pos_dict[nid]) for nid in node_ids}
        seen_pairs = set()
        for a, directions in bidi_directions.items():
            for direction, b in directions.items():
                pair = (a, b)
                if pair in seen_pairs or b not in pos:
                    continue
                seen_pairs.add(pair)
                vx, vy = _DIRECTION_VECTORS[direction]
                directed_edges.append((a, b, vx, vy))
    else:
        rng = random.Random(_LAYOUT_SEED)
        pos = {nid: [rng.uniform(0, _LAYOUT_AREA), rng.uniform(0, _LAYOUT_AREA)] for nid in node_ids}

    area = _LAYOUT_AREA * _LAYOUT_AREA
    k = (area / len(node_ids)) ** 0.5

    for iteration in range(_LAYOUT_ITERATIONS):
        disp = {nid: [0.0, 0.0] for nid in node_ids}

        for i, a in enumerate(node_ids):
            ax, ay = pos[a]
            for b in node_ids[i + 1:]:
                bx, by = pos[b]
                dx, dy = ax - bx, ay - by
                dist = max((dx * dx + dy * dy) ** 0.5, 0.01)
                force = (k * k) / dist
                fx, fy = (dx / dist) * force, (dy / dist) * force
                disp[a][0] += fx
                disp[a][1] += fy
                disp[b][0] -= fx
                disp[b][1] -= fy

        for a, b in edges:
            if a not in pos or b not in pos:
                continue
            ax, ay = pos[a]
            bx, by = pos[b]
            dx, dy = ax - bx, ay - by
            dist = max((dx * dx + dy * dy) ** 0.5, 0.01)
            force = (dist * dist) / k
            fx, fy = (dx / dist) * force, (dy / dist) * force
            disp[a][0] -= fx
            disp[a][1] -= fy
            disp[b][0] += fx
            disp[b][1] += fy

        # Real-direction corrective force (2026-08-09, Coffee: Stonearch
        # Bridge -> the Weeping Well is a real, declared "south"
        # relationship in campaign.json that the pure physics layout
        # had no notion of at all). Decomposes the current offset
        # between a directed edge's two nodes into a component ALONG
        # the real direction (left alone -- distance is still governed
        # by the ordinary spring/repulsion forces above) and a
        # component PERPENDICULAR to it (pulled toward zero) -- keeps
        # a real "south" edge visually south without fighting how far
        # apart the two nodes actually end up.
        for a, b, vx, vy in directed_edges:
            ax, ay = pos[a]
            bx, by = pos[b]
            dx, dy = bx - ax, by - ay
            parallel = dx * vx + dy * vy
            perp_x, perp_y = dx - parallel * vx, dy - parallel * vy
            disp[a][0] += perp_x * _DIRECTION_FORCE
            disp[a][1] += perp_y * _DIRECTION_FORCE
            disp[b][0] -= perp_x * _DIRECTION_FORCE
            disp[b][1] -= perp_y * _DIRECTION_FORCE

        # Mild pull toward the layout's own CENTROID (2026-08-09, Coffee
        # follow-up: "cleaner layout... more even spacing"; fixed to use
        # the real centroid rather than a hardcoded (500, 500) the same
        # day -- the compass-direction seed is centered on an arbitrary
        # root at (0, 0) and can legitimately sit anywhere, so pulling
        # toward a FIXED point fights the real seeded layout instead of
        # just keeping it compact). Pure repulsion + spring attraction
        # alone lets a loosely-connected node (or sub-cluster) drift
        # arbitrarily far from the rest, wasting canvas space and
        # leaving the rest of the graph cramped together -- a small
        # constant pull toward wherever the graph's own current center
        # of mass is keeps it compact without fighting its real shape.
        centroid_x = sum(p[0] for p in pos.values()) / len(node_ids)
        centroid_y = sum(p[1] for p in pos.values()) / len(node_ids)
        for nid in node_ids:
            nx, ny = pos[nid]
            disp[nid][0] += (centroid_x - nx) * _CENTER_GRAVITY
            disp[nid][1] += (centroid_y - ny) * _CENTER_GRAVITY

        temperature = _LAYOUT_AREA * 0.1 * (1 - iteration / _LAYOUT_ITERATIONS)
        for nid in node_ids:
            dx, dy = disp[nid]
            dist = max((dx * dx + dy * dy) ** 0.5, 0.01)
            capped = min(dist, temperature)
            pos[nid][0] += (dx / dist) * capped
            pos[nid][1] += (dy / dist) * capped
            # Real bug (2026-08-09, found testing the directional
            # layout against the full underground layer): this used to
            # hard-clamp every position to [0, _LAYOUT_AREA] every
            # single iteration -- harmless for the old pure-random seed
            # (always already inside that range), but the real compass-
            # direction seed is naturally centered on an arbitrary root
            # at (0, 0) and goes genuinely negative (west/north).
            # Re-clamping a negative coordinate to exactly 0 every
            # iteration silently collapsed multiple UNRELATED nodes
            # onto that same boundary, over and over, so they could
            # never separate again -- confirmed live: 3 real, distinct
            # goblin_warrens sub-locations landed on the exact same
            # pixel. _normalize_to_canvas already rescales whatever
            # raw coordinate range comes out of this loop into the
            # real canvas bounds afterward, so there was never any
            # real need to constrain the intermediate physics space at
            # all -- removed rather than widened.

    _contain_disconnected_components(node_ids, edges, pos, k)
    return {nid: (x, y) for nid, (x, y) in pos.items()}


def _contain_disconnected_components(
    node_ids: list[str], edges: list[tuple[str, str]], pos: dict[str, list[float]], k: float,
) -> None:
    """
    Real bug (2026-08-09, found visually re-rendering a small layer after
    the clamp-removal fix above): with the hard per-iteration clamp gone,
    a genuinely disconnected sub-cluster (or a single isolated node with
    no edges at all) is only held back by the mild centroid gravity, and
    for a SMALL layer that isn't nearly enough -- one lone node drifted
    to 1500+ units away while the real chain it should sit near stayed
    under 200 units wide, so _normalize_to_canvas's bounding box got
    dominated by that one outlier and squeezed the real, connected chain
    into a sliver of the canvas. Tuning _CENTER_GRAVITY higher fights
    this at the cost of over-compressing the main cluster's own internal
    spacing (it pulls on every node, not just the stray ones), so instead:
    a real, targeted fix -- find each connected component, and rigidly
    translate (not reshape) any component that isn't the largest one so
    its centroid sits within a bounded distance of the largest
    component's centroid. Internal structure/directions inside every
    component are untouched; only where the whole component sits moves.
    """
    if len(node_ids) < 2:
        return
    adjacency: dict[str, list[str]] = {nid: [] for nid in node_ids}
    for a, b in edges:
        if a in adjacency and b in adjacency:
            adjacency[a].append(b)
            adjacency[b].append(a)

    components: list[list[str]] = []
    seen: set[str] = set()
    for nid in node_ids:
        if nid in seen:
            continue
        component = []
        stack = [nid]
        seen.add(nid)
        while stack:
            cur = stack.pop()
            component.append(cur)
            for neighbor in adjacency[cur]:
                if neighbor not in seen:
                    seen.add(neighbor)
                    stack.append(neighbor)
        components.append(component)

    if len(components) < 2:
        return

    main = max(components, key=lambda c: len(c), default=components[0])
    main_cx = sum(pos[nid][0] for nid in main) / len(main)
    main_cy = sum(pos[nid][1] for nid in main) / len(main)
    # Generous on purpose: _CENTER_GRAVITY already keeps ordinarily-sized
    # sub-clusters reasonably close on its own (confirmed visually on the
    # 43-node underground layer -- no clamp needed there at all). This
    # only needs to catch the rare case gravity alone can't pull in
    # enough (e.g. a single isolated node in a small, sparse layer) --
    # too tight a bound here fights gravity's own placement and forces
    # otherwise well-separated real clusters to overlap.
    max_gap = k * 6.0

    for component in components:
        if component is main:
            continue
        ccx = sum(pos[nid][0] for nid in component) / len(component)
        ccy = sum(pos[nid][1] for nid in component) / len(component)
        dx, dy = ccx - main_cx, ccy - main_cy
        dist = max((dx * dx + dy * dy) ** 0.5, 0.01)
        if dist <= max_gap:
            continue
        scale = max_gap / dist
        target_cx = main_cx + dx * scale
        target_cy = main_cy + dy * scale
        shift_x, shift_y = target_cx - ccx, target_cy - ccy
        for nid in component:
            pos[nid][0] += shift_x
            pos[nid][1] += shift_y


def _canvas_size(node_count: int) -> tuple[int, int]:
    steps = max(0, node_count - 15) / _NODES_PER_SCALE_STEP
    scale = 1 + steps * _SCALE_STEP_PCT
    width = min(int(CANVAS_WIDTH * scale), _MAX_CANVAS_WIDTH)
    height = min(int(CANVAS_HEIGHT * scale), _MAX_CANVAS_HEIGHT)
    return width, height


def _normalize_to_canvas(
    positions: dict[str, tuple[float, float]], width: int, height: int, margin: int = 90,
) -> dict[str, tuple[int, int]]:
    if not positions:
        return {}
    xs = [p[0] for p in positions.values()]
    ys = [p[1] for p in positions.values()]
    min_x, max_x = min(xs), max(xs)
    min_y, max_y = min(ys), max(ys)
    span_x = max(max_x - min_x, 1.0)
    span_y = max(max_y - min_y, 1.0)
    usable_w = width - 2 * margin
    usable_h = height - 2 * margin - 60  # room for title + legend
    out = {}
    for nid, (x, y) in positions.items():
        nx = margin + (x - min_x) / span_x * usable_w
        ny = margin + 60 + (y - min_y) / span_y * usable_h
        out[nid] = (int(nx), int(ny))
    return out


def _assign_label_sides(positions: dict[str, tuple[int, int]]) -> dict[str, str]:
    """
    Confirmed visually: a heavily-explored layer (underground has 43
    real locations) puts several nodes on nearly the same horizontal
    row, and a label drawn directly below EVERY node collides badly
    with its row-neighbors' labels. Groups nodes into rough horizontal
    bands (within ~30px of each other) and alternates "below"/"above"
    placement in x-order within each band -- cuts same-row collisions
    roughly in half without needing a full label-collision solver.
    """
    band_of = {}
    bands = []
    for nid, (_, y) in sorted(positions.items(), key=lambda kv: kv[1][1]):
        band = next((b for b in bands if abs(b - y) <= 30), None)
        if band is None:
            bands.append(y)
            band = y
        band_of[nid] = band

    sides = {}
    counts = {}
    for nid, _ in sorted(positions.items(), key=lambda kv: (band_of[kv[0]], kv[1][0])):
        band = band_of[nid]
        idx = counts.get(band, 0)
        sides[nid] = "below" if idx % 2 == 0 else "above"
        counts[band] = idx + 1
    return sides


def _label_boxes_overlap(a: tuple[float, float, float, float], b: tuple[float, float, float, float], pad: int = 2) -> bool:
    ax, ay, aw, ah = a
    bx, by, bw, bh = b
    return not (ax + aw + pad < bx or bx + bw + pad < ax or ay + ah + pad < by or by + bh + pad < ay)


def _resolve_label_layout(
    draw: ImageDraw.ImageDraw, positions: dict[str, tuple[int, int]], layer_locations: dict,
    visited_here: list[str], current_location_id: str | None, name_font: ImageFont.FreeTypeFont,
) -> dict[str, tuple[float, float, float, float]]:
    """
    Task #11 follow-up (2026-08-09, Coffee: "reduce remaining label
    overlap"). Returns {loc_id: (label_x, label_y, w, h)} -- the real
    draw position and measured size for each node's name label.
    Starts from _assign_label_sides' band alternation (above/below
    within each horizontal row), then does an actual pairwise bounding-
    box collision check per node against every already-placed label and
    flips its side if that removes the overlap. This is a real,
    bounded improvement (reduces, not guarantees-zero, overlap) --
    a full label-placement solver is out of scope for a map that's
    still meant to render in well under a second.
    """
    sides = _assign_label_sides(positions)

    def box_for(loc_id: str, side: str) -> tuple[float, float, float, float]:
        x, y = positions[loc_id]
        is_current = loc_id == current_location_id
        radius = _NODE_RADIUS_CURRENT if is_current else _NODE_RADIUS
        label = _truncate_name(layer_locations[loc_id]["name"])
        bbox = draw.textbbox((0, 0), label, font=name_font)
        w, h = bbox[2] - bbox[0], bbox[3] - bbox[1]
        label_y = (y - radius - 6 - h) if side == "above" else (y + radius + 4)
        return (x - w / 2, label_y, w, h)

    placed: dict[str, tuple[float, float, float, float]] = {}
    for loc_id in sorted(visited_here, key=lambda nid: positions[nid][1]):
        side = sides[loc_id]
        box = box_for(loc_id, side)
        if any(_label_boxes_overlap(box, other) for other in placed.values()):
            flipped_box = box_for(loc_id, "above" if side == "below" else "below")
            if not any(_label_boxes_overlap(flipped_box, other) for other in placed.values()):
                box = flipped_box
        placed[loc_id] = box
    return placed


def _draw_background(draw: ImageDraw.ImageDraw, width: int, height: int) -> None:
    for y in range(height):
        t = y / max(height - 1, 1)
        r = int(_BG_TOP[0] + (_BG_BOTTOM[0] - _BG_TOP[0]) * t)
        g = int(_BG_TOP[1] + (_BG_BOTTOM[1] - _BG_TOP[1]) * t)
        b = int(_BG_TOP[2] + (_BG_BOTTOM[2] - _BG_TOP[2]) * t)
        draw.line([(0, y), (width, y)], fill=(r, g, b))


def _visible_nodes_and_edges(
    layer_locations: dict, visited_ids: set[str], revealed_ids: set[str],
) -> tuple[list[str], list[str], list[tuple[str, str]], dict[str, int]]:
    """
    The actual fog-of-war logic, pulled out of render_layer_map so it's
    directly testable without needing to inspect rendered pixels.
    Returns (visited_here, revealed_here, edges, unexplored_counts):
    - visited_here / revealed_here: sorted loc_ids actually in this
      layer (revealed_here excludes anything already visited -- once
      you've been there for real, that's the stronger fact).
    - edges: undirected (loc_id, loc_id) pairs, ONLY between two
      VISITED locations -- a connection to anywhere unvisited is never
      turned into an edge, since the edge itself is the real spoiler
      (mirrors _do_show_map's text-map rule exactly).
    - unexplored_counts: {loc_id: count} of a visited location's real
      connections that lead somewhere not yet visited.
    """
    visited_here = sorted(loc_id for loc_id in layer_locations if loc_id in visited_ids)
    revealed_here = sorted(
        loc_id for loc_id in layer_locations if loc_id in revealed_ids and loc_id not in visited_ids
    )

    edges = []
    unexplored_counts = {}
    for loc_id in visited_here:
        connections = layer_locations[loc_id].get("connections", [])
        unexplored = 0
        for conn in connections:
            if conn not in layer_locations:
                continue
            if conn in visited_ids:
                # Only add once (lower id first) so the force layout
                # and line-drawing don't do double work.
                edge = (loc_id, conn) if loc_id < conn else (conn, loc_id)
                if edge not in edges:
                    edges.append(edge)
            else:
                unexplored += 1
        if unexplored:
            unexplored_counts[loc_id] = unexplored

    return visited_here, revealed_here, edges, unexplored_counts


def render_layer_map(
    layer_name: str,
    layer_locations: dict,
    visited_ids: set[str],
    revealed_ids: set[str],
    current_location_id: str | None,
) -> bytes:
    """
    layer_locations: CAMPAIGN["locations"][layer_name] verbatim (loc_id
    -> {"name": ..., "connections": [...], ...}) -- never anything
    invented. visited_ids/revealed_ids are the character's real
    character["visited_locations"]/character["map_revealed_locations"]
    (already filtered to this layer's ids by the caller is fine but
    not required, this filters again defensively). Returns real PNG
    bytes.
    """
    visited_here, revealed_here, edges, unexplored_counts = _visible_nodes_and_edges(
        layer_locations, visited_ids, revealed_ids,
    )

    width, height = _canvas_size(len(visited_here))
    bidi_directions = _bidirectional_directions(layer_locations, set(visited_here))
    floor_levels = _floor_levels(bidi_directions)
    raw_positions = _compute_layout(visited_here, edges, bidi_directions)
    positions = _normalize_to_canvas(raw_positions, width, height)

    image = Image.new("RGB", (width, height), _BG_TOP)
    draw = ImageDraw.Draw(image, "RGBA")
    _draw_background(draw, width, height)

    title_font = _load_font(26, bold=True)
    title = f"MAP — {layer_name.title()}"
    title_bbox = draw.textbbox((0, 0), title, font=title_font)
    draw.text(((width - (title_bbox[2] - title_bbox[0])) / 2, 16), title, font=title_font, fill=_LABEL_COLOR)

    name_font = _load_font(14, bold=True)
    badge_font = _load_font(12, bold=True)

    for a, b in edges:
        if a in positions and b in positions:
            draw.line([positions[a], positions[b]], fill=_EDGE_COLOR, width=3)

    label_layout = _resolve_label_layout(draw, positions, layer_locations, visited_here, current_location_id, name_font)
    for loc_id in visited_here:
        if loc_id not in positions:
            continue
        x, y = positions[loc_id]
        is_current = loc_id == current_location_id
        radius = _NODE_RADIUS_CURRENT if is_current else _NODE_RADIUS
        fill = _NODE_CURRENT_FILL if is_current else _NODE_FILL
        draw.ellipse([x - radius, y - radius, x + radius, y + radius], fill=fill, outline=_NODE_OUTLINE, width=2)
        if is_current:
            draw.ellipse(
                [x - radius - 5, y - radius - 5, x + radius + 5, y + radius + 5], outline=_NODE_CURRENT_FILL, width=2,
            )

        label = _truncate_name(layer_locations[loc_id]["name"])
        label_x, label_y, _, _ = label_layout[loc_id]
        draw.text((label_x, label_y), label, font=name_font, fill=_LABEL_COLOR)

        unexplored = unexplored_counts.get(loc_id)
        if unexplored:
            badge_text = f"+{unexplored}"
            badge_bbox = draw.textbbox((0, 0), badge_text, font=badge_font)
            bw = badge_bbox[2] - badge_bbox[0] + 8
            bh = badge_bbox[3] - badge_bbox[1] + 6
            bx, by = x + radius - 4, y - radius - bh + 4
            draw.ellipse([bx, by, bx + bw, by + bh], fill=_UNEXPLORED_BADGE_FILL, outline=_NODE_OUTLINE)
            draw.text((bx + 4, by + 2), badge_text, font=badge_font, fill=_UNEXPLORED_BADGE_TEXT)

        # Real floor badge (2026-08-09, Coffee: "F3 - F2 - F1 - B1 - B2
        # - B3 for floors and basements in dungeons") -- only drawn for
        # a node that's actually part of a real up/down chain, and
        # never for the chain's own entry point (level 0 -- "you're on
        # the floor you arrived on" needs no label). Bottom-left corner
        # so it never collides with the unexplored-paths badge above.
        floor_level = floor_levels.get(loc_id)
        if floor_level:
            floor_text = f"F{floor_level}" if floor_level > 0 else f"B{-floor_level}"
            floor_bbox = draw.textbbox((0, 0), floor_text, font=badge_font)
            fw = floor_bbox[2] - floor_bbox[0] + 8
            fh = floor_bbox[3] - floor_bbox[1] + 6
            fx, fy = x - radius - fw + 4, y + radius - 4
            draw.ellipse([fx, fy, fx + fw, fy + fh], fill=_FLOOR_BADGE_FILL, outline=_NODE_OUTLINE)
            draw.text((fx + 4, fy + 2), floor_text, font=badge_font, fill=_FLOOR_BADGE_TEXT)

    legend_font = _load_font(14)
    legend_lines = ["red ring = where you are  •  dot = visited  •  gold +N = unexplored paths from there"]
    if floor_levels:
        legend_lines.append("blue F/B = floor above/below the chain's entry point")
    if revealed_here:
        # Capped at a fixed count (never unbounded, matching this
        # codebase's usual "no silently-growing UI" convention) so this
        # single legend line can never run past a second line's worth
        # of the fixed bottom margin _normalize_to_canvas already
        # reserves -- excess is summarized as "+N more" instead.
        shown = revealed_here[:_MAX_REVEALED_IN_LEGEND]
        revealed_names = ", ".join(layer_locations[loc_id]["name"] for loc_id in shown)
        extra = len(revealed_here) - len(shown)
        if extra:
            revealed_names += f", +{extra} more"
        legend_lines.append(f"Marked but not yet visited: {revealed_names}")
    # Real bug (2026-08-09, found re-rendering a narrow layer after adding
    # the floor-badge clause): the base legend line plus the floor-badge
    # clause together ran wider than a 900px-wide canvas and got clipped
    # mid-word -- only the "revealed" line was ever width-checked before.
    # Every line now gets the same _truncate_name width check, and lines
    # stack bottom-up so adding a 3rd line (floor badges + a revealed-map
    # note at once) never overlaps the one above it.
    line_limit = int(width / 8.5)
    for i, line in enumerate(reversed(legend_lines)):
        y = height - 22 - i * 20
        draw.text((20, y), _truncate_name(line, limit=line_limit), font=legend_font, fill=_LEGEND_COLOR)

    buffer = io.BytesIO()
    image.save(buffer, format="PNG")
    return buffer.getvalue()
