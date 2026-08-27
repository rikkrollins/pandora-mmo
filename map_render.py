"""
map_render.py

Real live reports (2026-08-20, Coffee, two Development-topic
screenshots) plus live chat follow-ups drove a full rewrite of this
module's layout+drawing engine:

1. A plain lettered/numbered grid (A-G x 1-6): "make sure all the
   north south east and west locations on all the different layers are
   correct. I don't want any locations conflicting and I want every
   square to be its proper location."
2. An RPGClassics Zelda-style dungeon map: black = non-travel,
   white-outlined = a real room, red = the player's current room.
3. Live: "the circle map is confusing" (this module's PREVIOUS
   Fruchterman-Reingold force-directed layout, only ever *seeded* by
   real compass data and then relaxed by physics forces -- accurate
   data in, but a physics relaxation was never going to produce a
   crisp orthogonal grid) and "when we say show the map, view the map,
   or open the map, thats the map we want to see."
4. "jus make sure i can see the names of the locations that we have
   vistited, and dont show locations we havent been too, use the fog
   of war" -- the existing fog-of-war contract (_visible_nodes_and_
   edges) and floor-badge system (_floor_levels, real feature from a
   2026-08-09 pass) are UNCHANGED, not rebuilt.
5. "u can show events and sub bosses, and bosses, and anything else
   with an [emoji] maybe? make a legend" -- real per-location icons
   (boss/monster/shop/npc/quest), grounded only in real campaign.json
   fields, with a drawn legend.

Real compass data now lives as authoritative `grid_position: {x, y}` +
`directions` on every location (see scripts/build_location_grid.py,
which derives both from `connections` -- the one 100%-complete
reachability list -- via BFS, with zero reciprocity conflicts,
verified by tests.test_regression's whole-campaign check). This module
no longer does any layout computation of its own: it reads real
`grid_position` directly and draws a literal cell grid. No network
call for the LAYOUT; each visited cell's own tile image reuses the
same Pollinations.ai generation bot.py's location art already uses
(same prompt/seed, same deterministic image), fetched at most once per
location ever and cached locally afterward -- the direct fix for "we
are having issues with the image generator... for now we could even
use them in each block": turns N live network calls per map view into
at most N one-time fetches, then instant local reads forever after.
"""
import hashlib
import io
import os

import requests
from PIL import Image, ImageDraw, ImageFont

import images as images_module

CELL_SIZE = 150
_MARGIN = 30
_TITLE_HEIGHT = 46
_LEGEND_LINE_HEIGHT = 20
_MAX_CANVAS_WIDTH = 1800
# Real live gap found 2026-08-27 (map-coordinate overhaul,
# scripts/build_location_grid.py): this used to silently clip a
# layer's rendered cells with no crash, no warning -- confirmed by
# actually rendering an injected far-off location and watching PIL
# just never draw it. Real dungeon delves now get their own genuine
# grid cells instead of stacking invisibly on one square (per Coffee:
# "make it feel like Zelda dungeons"), and `underground` -- the
# deepest layer, real multi-room dungeons chained several floors
# down -- now spans up to ~30 real rows. Raised with real headroom
# above that measured figure, not a bare-minimum fit, so the next
# added dungeon room doesn't immediately reopen this same silent-clip
# risk.
_MAX_CANVAS_HEIGHT = 5500
# A narrow layer (sky's real bounding box is only 2 columns wide) would
# otherwise squeeze the icon-legend line -- a real floor under the grid
# width, confirmed against a real render, wide enough for the full
# 5-category icon legend at its own font size.
_MIN_CANVAS_WIDTH = 520

_BG_COLOR = (30, 26, 22)
_CELL_BLACK = (10, 10, 10)
_CELL_OUTLINE = (235, 235, 235)
_CELL_OUTLINE_CURRENT = (214, 40, 40)
_LABEL_BG = (0, 0, 0, 170)
_LABEL_COLOR = (255, 255, 255)
_TITLE_COLOR = (235, 235, 235)
_LEGEND_COLOR = (200, 200, 200)
_UNEXPLORED_BADGE_FILL = (212, 175, 55)
_UNEXPLORED_BADGE_TEXT = (40, 25, 10)
_FLOOR_BADGE_FILL = (110, 140, 165)
_FLOOR_BADGE_TEXT = (20, 30, 40)

_FONT_DIR = "/usr/share/fonts/truetype/dejavu"
_FONT_BOLD_PATH = os.path.join(_FONT_DIR, "DejaVuSans-Bold.ttf")
_FONT_REGULAR_PATH = os.path.join(_FONT_DIR, "DejaVuSans.ttf")

_MAX_REVEALED_IN_LEGEND = 6

# Real per-location icon overlays (2026-08-20, per Coffee: "show events
# and sub bosses, and bosses, and anything else with an [emoji] maybe?
# make a legend"). Drawn as small colored dots rather than literal
# emoji glyphs -- confirmed live that this server has no color-emoji
# font installed, so Pillow renders emoji as empty tofu boxes; a
# colored dot + a real text legend line is legible everywhere, no font
# dependency. Each is grounded only in a real campaign.json field
# already checked live against the data (22 real bosses/3 real
# shops/etc.), nothing invented. No real "sub-boss" data distinction
# exists anywhere in campaign.json (only a boolean `is_boss`), so only
# one boss tier is drawn.
ICON_LEGEND = [
    ("boss", (218, 165, 32)),
    ("monster", (190, 60, 60)),
    ("shop", (70, 150, 80)),
    ("npc", (70, 110, 190)),
    ("quest", (160, 90, 190)),
]

# Same obfuscated-cache-directory convention as ai/narration_cache.py
# ("keep all this in a obfuscated folder so people cannot see the
# storyline, images, narrations, quests"): a per-location image tile
# is real generated game art, same privacy bar as narration text.
_TILE_CACHE_DIR = os.path.join(os.path.dirname(__file__), ".mtc7v2qk")


def _load_font(size: int, bold: bool = False) -> ImageFont.FreeTypeFont:
    path = _FONT_BOLD_PATH if bold else _FONT_REGULAR_PATH
    try:
        return ImageFont.truetype(path, size)
    except OSError:
        return ImageFont.load_default()


def _fit_label_to_width(draw: ImageDraw.ImageDraw, name: str, font: ImageFont.FreeTypeFont, max_width: int) -> str:
    """Trims a real location name to whatever actually fits max_width, measured against the real font -- a fixed character-count limit either wastes space on short names or still overflows on wide-glyph ones."""
    if draw.textlength(name, font=font) <= max_width:
        return name
    for cut in range(len(name) - 1, 0, -1):
        candidate = name[:cut] + "…"
        if draw.textlength(candidate, font=font) <= max_width:
            return candidate
    return "…"


def _location_image_seed(location_id: str) -> int:
    """Identical formula to bot.py's own _deterministic_image_seed(f"location:{id}") -- same key, same seed, same real image."""
    return int(hashlib.sha256(f"location:{location_id}".encode()).hexdigest(), 16) % (2 ** 31)


def _location_image_prompt(location: dict) -> str:
    """Identical formula to bot.py's own _location_image_prompt -- grounded only in the location's real description text."""
    return (
        f"{location['description']}, fantasy tabletop RPG environment concept art, "
        "atmospheric lighting, detailed digital painting, no text or labels"
    )


def _fetch_location_tile(layer_name: str, loc_id: str, location: dict) -> Image.Image | None:
    """
    Real per-location art, fetched at most ONCE ever per location
    (the seed is deterministic, so the image never changes) and cached
    to local disk afterward -- every later map render for ANY player
    reads the cached file instead of hitting the network again. Never
    raises: a network failure here degrades to a flat-color cell
    (_draw_cell's fallback), never crashes the whole map render, which
    is the actual fix for "we are having issues with the image
    generator" -- a transient failure costs one cell's art once, not
    the feature.
    """
    cache_path = os.path.join(_TILE_CACHE_DIR, layer_name, f"{loc_id}.png")
    if os.path.exists(cache_path):
        try:
            return Image.open(cache_path).convert("RGB")
        except OSError:
            pass
    url = images_module.generate_image_url(
        _location_image_prompt(location), width=CELL_SIZE, height=CELL_SIZE, seed=_location_image_seed(loc_id),
    )
    try:
        # 60s, matching bot.py's _send_generated_image's own proven
        # pre-warm timeout for this exact service -- a genuinely cold
        # (first-ever-requested) Pollinations image can take a while to
        # finish generating server-side; a shorter timeout here would
        # abort before it ever finishes, the same real failure mode
        # already solved once in bot.py.
        resp = requests.get(url, timeout=60)
        resp.raise_for_status()
        tile = Image.open(io.BytesIO(resp.content)).convert("RGB")
    except Exception:
        return None
    try:
        os.makedirs(os.path.dirname(cache_path), exist_ok=True)
        tile.save(cache_path, format="PNG")
    except OSError:
        pass
    return tile


def _draw_background(draw: ImageDraw.ImageDraw, width: int, height: int) -> None:
    draw.rectangle([0, 0, width, height], fill=_BG_COLOR)


def _visible_nodes_and_edges(
    layer_locations: dict, visited_ids: set[str], revealed_ids: set[str],
) -> tuple[list[str], list[str], list[tuple[str, str]], dict[str, int]]:
    """
    The actual fog-of-war logic (unchanged from the previous layout
    engine -- this contract is correct and stays exactly as-is per
    Coffee's own "use the fog of war" instruction). Returns
    (visited_here, revealed_here, edges, unexplored_counts):
    - visited_here / revealed_here: sorted loc_ids actually in this
      layer (revealed_here excludes anything already visited).
    - edges: undirected (loc_id, loc_id) pairs, ONLY between two
      VISITED locations.
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
                edge = (loc_id, conn) if loc_id < conn else (conn, loc_id)
                if edge not in edges:
                    edges.append(edge)
            else:
                unexplored += 1
        if unexplored:
            unexplored_counts[loc_id] = unexplored

    return visited_here, revealed_here, edges, unexplored_counts


def _floor_levels(layer_locations: dict, visited_here: set[str]) -> dict[str, int]:
    """
    Real per-node floor number for a vertical up/down chain (Coffee:
    "F3 - F2 - F1 - B1 - B2 - B3 for floors and basements"). Reads
    real `directions` up/down entries directly -- no reciprocal-
    filling needed anymore (grid_position/directions are now
    guaranteed fully consistent by scripts/build_location_grid.py and
    covered by a permanent regression test). Scoped to VISITED nodes
    only, same fog-of-war boundary as everywhere else -- an unvisited
    location never contributes a floor relationship.
    """
    vertical_nodes = set()
    for lid in visited_here:
        for direction, dest in (layer_locations.get(lid, {}).get("directions") or {}).items():
            if direction in ("up", "down") and dest in visited_here:
                vertical_nodes.add(lid)
                vertical_nodes.add(dest)

    levels: dict[str, int] = {}
    for start in sorted(vertical_nodes):
        if start in levels:
            continue
        levels[start] = 0
        queue = [start]
        while queue:
            current = queue.pop(0)
            for direction, dest in (layer_locations.get(current, {}).get("directions") or {}).items():
                if direction not in ("up", "down") or dest not in vertical_nodes or dest in levels:
                    continue
                levels[dest] = levels[current] + (1 if direction == "up" else -1)
                queue.append(dest)
    return levels


def _grid_cell_owners(layer_locations: dict, visited_here: list[str]) -> dict[tuple[int, int], list[str]]:
    """{(x, y): [loc_id, ...]} for every VISITED location that has a real grid_position -- multiple ids share a cell only when they're vertically stacked (same lateral cell, different floor, e.g. a cellar/upstairs pair)."""
    owners: dict[tuple[int, int], list[str]] = {}
    for loc_id in visited_here:
        pos = layer_locations.get(loc_id, {}).get("grid_position")
        if pos is None:
            continue
        owners.setdefault((pos["x"], pos["y"]), []).append(loc_id)
    return owners


def _pick_primary(cell_ids: list[str], floor_levels: dict[str, int], current_location_id: str | None) -> str:
    """
    Which of a shared cell's locations gets the actual tile image and
    name label -- the character's own current location if it's part of
    this stack (so standing in a cellar always shows the cellar's own
    real art, not its ground-floor neighbor's), otherwise whichever is
    closest to floor level 0. The rest are still represented via the
    F/B floor badge.
    """
    if current_location_id in cell_ids:
        return current_location_id
    return min(cell_ids, key=lambda lid: abs(floor_levels.get(lid, 0)))


def _location_icons(location: dict, monsters: dict, quests: dict, location_id: str) -> list[str]:
    """Real per-location icon categories present, grounded only in fields that already exist -- see ICON_LEGEND for what each one means and its color."""
    icons = []
    monster_keys = location.get("monsters") or []
    if any((monsters.get(m) or {}).get("is_boss") for m in monster_keys):
        icons.append("boss")
    if any(not (monsters.get(m) or {}).get("is_boss") for m in monster_keys):
        icons.append("monster")
    if location.get("shop"):
        icons.append("shop")
    if location.get("npcs"):
        icons.append("npc")
    if any((q or {}).get("location") == location_id for q in quests.values()):
        icons.append("quest")
    return icons


def render_layer_map(
    layer_name: str,
    layer_locations: dict,
    visited_ids: set[str],
    revealed_ids: set[str],
    current_location_id: str | None,
    monsters: dict | None = None,
    quests: dict | None = None,
) -> bytes:
    """
    layer_locations: CAMPAIGN["locations"][layer_name] verbatim.
    visited_ids/revealed_ids: the character's real visited_locations/
    map_revealed_locations. monsters/quests: CAMPAIGN["monsters"]/
    CAMPAIGN["quests"] (optional -- omitting them just means no icons,
    used by tests that don't care about icon overlays). Returns real
    PNG bytes: a literal grid of square cells, one per real visited
    location at its own real grid_position, solid black for anywhere
    unvisited or nonexistent, red-outlined for the character's current
    location -- never a physics layout, never a guess.
    """
    monsters = monsters or {}
    quests = quests or {}
    visited_here, revealed_here, edges, unexplored_counts = _visible_nodes_and_edges(
        layer_locations, visited_ids, revealed_ids,
    )
    visited_set = set(visited_here)
    floor_levels = _floor_levels(layer_locations, visited_set)
    owners = _grid_cell_owners(layer_locations, visited_here)

    if owners:
        xs = [xy[0] for xy in owners]
        ys = [xy[1] for xy in owners]
        min_x, max_x, min_y, max_y = min(xs), max(xs), min(ys), max(ys)
    else:
        min_x = max_x = min_y = max_y = 0

    cols = max_x - min_x + 1
    rows = max_y - min_y + 1
    legend_lines = _build_legend_lines(revealed_here, layer_locations, bool(floor_levels))
    legend_row_count = len(legend_lines) + 1  # +1 for the icon-swatch line, drawn separately
    width = min(max(_MARGIN * 2 + cols * CELL_SIZE, _MIN_CANVAS_WIDTH), _MAX_CANVAS_WIDTH)
    height = min(
        _MARGIN * 2 + _TITLE_HEIGHT + rows * CELL_SIZE + legend_row_count * _LEGEND_LINE_HEIGHT + 10,
        _MAX_CANVAS_HEIGHT,
    )

    image = Image.new("RGB", (width, height), _BG_COLOR)
    draw = ImageDraw.Draw(image, "RGBA")
    _draw_background(draw, width, height)

    title_font = _load_font(24, bold=True)
    title = f"MAP — {layer_name.title()}"
    title_bbox = draw.textbbox((0, 0), title, font=title_font)
    draw.text(((width - (title_bbox[2] - title_bbox[0])) / 2, 10), title, font=title_font, fill=_TITLE_COLOR)

    name_font = _load_font(12, bold=True)
    badge_font = _load_font(11, bold=True)

    grid_top = _MARGIN + _TITLE_HEIGHT
    for gy in range(rows):
        for gx in range(cols):
            x = min_x + gx
            y = max_y - gy  # north (larger y) draws toward the top
            px = _MARGIN + gx * CELL_SIZE
            py = grid_top + gy * CELL_SIZE
            cell_ids = owners.get((x, y))
            if not cell_ids:
                draw.rectangle([px, py, px + CELL_SIZE, py + CELL_SIZE], fill=_CELL_BLACK)
                continue
            primary = _pick_primary(cell_ids, floor_levels, current_location_id)
            _draw_cell(
                draw, image, layer_name, primary, layer_locations[primary], px, py,
                is_current=(current_location_id in cell_ids),
                name_font=name_font, badge_font=badge_font,
                unexplored=unexplored_counts.get(primary),
                floor_level=floor_levels.get(primary),
                stacked_levels=sorted({floor_levels.get(lid, 0) for lid in cell_ids} - {floor_levels.get(primary, 0)}),
                icons=_location_icons(layer_locations[primary], monsters, quests, primary),
            )

    legend_font = _load_font(13)
    legend_y = height - legend_row_count * _LEGEND_LINE_HEIGHT - 8
    _draw_icon_legend_line(draw, _MARGIN, legend_y, legend_font)
    legend_y += _LEGEND_LINE_HEIGHT
    for line in legend_lines:
        fitted = _fit_label_to_width(draw, line, legend_font, width - 2 * _MARGIN)
        draw.text((_MARGIN, legend_y), fitted, font=legend_font, fill=_LEGEND_COLOR)
        legend_y += _LEGEND_LINE_HEIGHT

    buffer = io.BytesIO()
    image.save(buffer, format="PNG")
    return buffer.getvalue()


def _draw_icon_legend_line(draw: ImageDraw.ImageDraw, x: int, y: int, font: ImageFont.FreeTypeFont) -> None:
    """A real color-key line ('● boss  ● monster  ...') -- drawn cells use these same colored swatches (see _draw_cell), never a literal emoji glyph (this server has no color-emoji font, confirmed live)."""
    cursor_x = x
    swatch_r = 5
    for label, color in ICON_LEGEND:
        cy = y + 8
        draw.ellipse([cursor_x, cy - swatch_r, cursor_x + swatch_r * 2, cy + swatch_r], fill=color)
        cursor_x += swatch_r * 2 + 5
        text = f"{label}   "
        draw.text((cursor_x, y), text, font=font, fill=_LEGEND_COLOR)
        bbox = draw.textbbox((0, 0), text, font=font)
        cursor_x += bbox[2] - bbox[0]


def _build_legend_lines(revealed_here: list[str], layer_locations: dict, has_floor_badges: bool) -> list[str]:
    lines = [
        "red outline = you are here  •  gold +N = unexplored paths",
    ]
    if has_floor_badges:
        lines.append("blue F/B = floor above/below the entry point")
    if revealed_here:
        shown = revealed_here[:_MAX_REVEALED_IN_LEGEND]
        revealed_names = ", ".join(layer_locations[loc_id]["name"] for loc_id in shown)
        extra = len(revealed_here) - len(shown)
        if extra:
            revealed_names += f", +{extra} more"
        lines.append(f"Marked but not yet visited: {revealed_names}")
    return lines


_ICON_COLOR = dict(ICON_LEGEND)


def _draw_cell(
    draw: ImageDraw.ImageDraw, image: Image.Image, layer_name: str, loc_id: str, location: dict, px: int, py: int,
    is_current: bool, name_font: ImageFont.FreeTypeFont, badge_font: ImageFont.FreeTypeFont,
    unexplored: int | None, floor_level: int | None,
    stacked_levels: list[int], icons: list[str],
) -> None:
    tile = _fetch_location_tile(layer_name, loc_id, location)
    if tile is not None:
        if tile.size != (CELL_SIZE, CELL_SIZE):
            tile = tile.resize((CELL_SIZE, CELL_SIZE))
        image.paste(tile, (px, py))
    else:
        draw.rectangle([px, py, px + CELL_SIZE, py + CELL_SIZE], fill=(60, 55, 48))

    outline = _CELL_OUTLINE_CURRENT if is_current else _CELL_OUTLINE
    width = 4 if is_current else 2
    draw.rectangle([px, py, px + CELL_SIZE, py + CELL_SIZE], outline=outline, width=width)

    label = _fit_label_to_width(draw, location["name"], name_font, CELL_SIZE - 8)
    label_bbox = draw.textbbox((0, 0), label, font=name_font)
    lw, lh = label_bbox[2] - label_bbox[0], label_bbox[3] - label_bbox[1]
    draw.rectangle([px, py + CELL_SIZE - lh - 8, px + CELL_SIZE, py + CELL_SIZE], fill=_LABEL_BG)
    draw.text((px + (CELL_SIZE - lw) / 2, py + CELL_SIZE - lh - 5), label, font=name_font, fill=_LABEL_COLOR)

    if icons:
        swatch_r = 6
        pad = 4
        swatch_span = swatch_r * 2 + 3
        strip_w = len(icons) * swatch_span + pad
        draw.rectangle([px + 2, py + 2, px + 2 + strip_w, py + 2 + swatch_r * 2 + pad], fill=_LABEL_BG)
        cx = px + 2 + pad // 2 + swatch_r
        cy = py + 2 + pad // 2 + swatch_r
        for category in icons:
            color = _ICON_COLOR.get(category, (200, 200, 200))
            draw.ellipse([cx - swatch_r, cy - swatch_r, cx + swatch_r, cy + swatch_r], fill=color, outline=(0, 0, 0))
            cx += swatch_span

    if unexplored:
        badge_text = f"+{unexplored}"
        bbox = draw.textbbox((0, 0), badge_text, font=badge_font)
        bw, bh = bbox[2] - bbox[0] + 6, bbox[3] - bbox[1] + 4
        bx, by = px + CELL_SIZE - bw - 2, py + 2
        draw.rectangle([bx, by, bx + bw, by + bh], fill=_UNEXPLORED_BADGE_FILL, outline=(0, 0, 0))
        draw.text((bx + 3, by + 1), badge_text, font=badge_font, fill=_UNEXPLORED_BADGE_TEXT)

    if floor_level or stacked_levels:
        all_levels = sorted({lvl for lvl in ([floor_level] if floor_level else []) + stacked_levels if lvl})
        floor_text = "/".join(f"F{lvl}" if lvl > 0 else f"B{-lvl}" for lvl in all_levels)
        if floor_text:
            bbox = draw.textbbox((0, 0), floor_text, font=badge_font)
            bw, bh = bbox[2] - bbox[0] + 6, bbox[3] - bbox[1] + 4
            bx, by = px + 2, py + CELL_SIZE - lh - 8 - bh - 2
            draw.rectangle([bx, by, bx + bw, by + bh], fill=_FLOOR_BADGE_FILL, outline=(0, 0, 0))
            draw.text((bx + 3, by + 1), floor_text, font=badge_font, fill=_FLOOR_BADGE_TEXT)
