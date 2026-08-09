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
_LEGEND_COLOR = (60, 45, 25)

_NODE_RADIUS = 16
_NODE_RADIUS_CURRENT = 20

_FONT_DIR = "/usr/share/fonts/truetype/dejavu"
_FONT_BOLD_PATH = os.path.join(_FONT_DIR, "DejaVuSans-Bold.ttf")
_FONT_REGULAR_PATH = os.path.join(_FONT_DIR, "DejaVuSans.ttf")

_LAYOUT_SEED = 1337
_LAYOUT_ITERATIONS = 300
_LAYOUT_AREA = 1000.0  # arbitrary units, normalized to the canvas afterward
_MAX_REVEALED_IN_LEGEND = 6


def _load_font(size: int, bold: bool = False) -> ImageFont.FreeTypeFont:
    path = _FONT_BOLD_PATH if bold else _FONT_REGULAR_PATH
    try:
        return ImageFont.truetype(path, size)
    except OSError:
        return ImageFont.load_default()


def _truncate_name(name: str, limit: int = 22) -> str:
    return name if len(name) <= limit else name[: limit - 1] + "…"


def _compute_layout(node_ids: list[str], edges: list[tuple[str, str]]) -> dict[str, tuple[float, float]]:
    """
    Plain Fruchterman-Reingold force-directed layout, pure Python, no
    numpy/networkx -- same "no new heavy dependency" spirit as
    battle_render.py. Deterministic: seeded with a fixed constant, and
    node_ids/edges are always iterated in the SAME order the caller
    already sorted them in, so the same visited set always lays out
    the same way rather than jittering between calls.
    """
    if not node_ids:
        return {}
    if len(node_ids) == 1:
        return {node_ids[0]: (_LAYOUT_AREA / 2, _LAYOUT_AREA / 2)}

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

        temperature = _LAYOUT_AREA * 0.1 * (1 - iteration / _LAYOUT_ITERATIONS)
        for nid in node_ids:
            dx, dy = disp[nid]
            dist = max((dx * dx + dy * dy) ** 0.5, 0.01)
            capped = min(dist, temperature)
            pos[nid][0] += (dx / dist) * capped
            pos[nid][1] += (dy / dist) * capped
            pos[nid][0] = min(max(pos[nid][0], 0), _LAYOUT_AREA)
            pos[nid][1] = min(max(pos[nid][1], 0), _LAYOUT_AREA)

    return {nid: (x, y) for nid, (x, y) in pos.items()}


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
    raw_positions = _compute_layout(visited_here, edges)
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

    label_sides = _assign_label_sides(positions)
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
        label_bbox = draw.textbbox((0, 0), label, font=name_font)
        label_w = label_bbox[2] - label_bbox[0]
        label_h = label_bbox[3] - label_bbox[1]
        if label_sides.get(loc_id) == "above":
            label_y = y - radius - 6 - label_h
        else:
            label_y = y + radius + 4
        draw.text((x - label_w / 2, label_y), label, font=name_font, fill=_LABEL_COLOR)

        unexplored = unexplored_counts.get(loc_id)
        if unexplored:
            badge_text = f"+{unexplored}"
            badge_bbox = draw.textbbox((0, 0), badge_text, font=badge_font)
            bw = badge_bbox[2] - badge_bbox[0] + 8
            bh = badge_bbox[3] - badge_bbox[1] + 6
            bx, by = x + radius - 4, y - radius - bh + 4
            draw.ellipse([bx, by, bx + bw, by + bh], fill=_UNEXPLORED_BADGE_FILL, outline=_NODE_OUTLINE)
            draw.text((bx + 4, by + 2), badge_text, font=badge_font, fill=_UNEXPLORED_BADGE_TEXT)

    legend_font = _load_font(14)
    draw.text(
        (20, height - 50), "red ring = where you are  •  dot = visited  •  gold +N = unexplored paths from there",
        font=legend_font, fill=_LEGEND_COLOR,
    )
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
        legend_text = _truncate_name(f"Marked but not yet visited: {revealed_names}", limit=int(width / 8.5))
        draw.text((20, height - 28), legend_text, font=legend_font, fill=_LEGEND_COLOR)

    buffer = io.BytesIO()
    image.save(buffer, format="PNG")
    return buffer.getvalue()
