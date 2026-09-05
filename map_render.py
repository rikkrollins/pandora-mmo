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
# Real floor-switcher support (2026-09-02, per Coffee: "list the floors
# in push buttons... if players are not on that floor but above or
# below that location, mark it with a dotted grey box"). A cell that's
# real and genuinely attained (already fog-of-war-visited) but belongs
# to a DIFFERENT floor than the one currently being viewed -- never
# drawn with its real name/icons (that belongs to ITS OWN floor's own
# view), just a plain grey dashed box hinting "something real is here,
# on another floor you can switch to."
_OTHER_FLOOR_FILL = (35, 35, 38)
_OTHER_FLOOR_OUTLINE = (150, 150, 150)
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
    ("dungeon", (120, 80, 40)),
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


def _labyrinth_room_image_seed(description: str) -> int:
    """Identical formula to bot.py's own _deterministic_image_seed(f"labyrinth_room:{description}") -- same key, same seed, same real image."""
    return int(hashlib.sha256(f"labyrinth_room:{description}".encode()).hexdigest(), 16) % (2 ** 31)


def _labyrinth_room_image_prompt(room: dict) -> str:
    """Identical formula to bot.py's own _labyrinth_room_image_prompt -- grounded only in the room's real description text."""
    return (
        f"{room['description']}, alternate-dimension fantasy dungeon environment concept art, "
        "ancient stone corridors, weathered mechanisms and archways, dramatic torchlight and deep shadow, "
        "a real sense of a structure built to be explored, atmospheric lighting, detailed digital painting, "
        "no text or labels"
    )


def _fetch_labyrinth_room_tile(room: dict) -> Image.Image | None:
    """
    Real per-room-CONCEPT art for the Labyrinth's own map (2026-09-04,
    per Coffee: "use the location images that you generate when we
    look around on the map so it looks more visually stimulating").
    Labyrinth FLOORS are ephemeral (fully regenerated on next descent,
    see this module's own "no network calls" note above on why this
    renderer was originally kept separate from render_layer_map), but a
    room's DESCRIPTION concept is drawn from a real, bounded pool and
    genuinely repeats across floors/players/sessions -- the same
    reasoning bot.py's own _maybe_send_labyrinth_room_image already
    relies on to key its narration image on the description rather
    than the room's own structural id (which is never stable across a
    regen). Cached to disk by that same deterministic seed, not room
    id, so the first floor anywhere to roll a given room concept pays
    the real network cost once and every later floor/player/session
    gets it free from cache -- same "fetch once, cache forever"
    discipline as _fetch_location_tile above, just keyed differently.
    Skips lightless rooms entirely (never fetches, flat fill only) --
    same "you can't see it, so no image either" rule bot.py's own
    narration image already enforces.
    """
    if room.get("modifier") == "lightless":
        return None
    seed = _labyrinth_room_image_seed(room["description"])
    cache_path = os.path.join(_TILE_CACHE_DIR, "labyrinth", f"{seed}.png")
    if os.path.exists(cache_path):
        try:
            return Image.open(cache_path).convert("RGB")
        except OSError:
            pass
    url = images_module.generate_image_url(
        _labyrinth_room_image_prompt(room), width=CELL_SIZE, height=CELL_SIZE, seed=seed,
    )
    try:
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


_ORNATE_FRAME_COLOR = (150, 115, 60)
_ORNATE_FRAME_ACCENT = (95, 70, 38)


def _draw_ornate_labyrinth_frame(draw: ImageDraw.ImageDraw, width: int, height: int) -> None:
    """
    Real live request (2026-09-05, per Coffee's own "Advanced Dungeon
    Map System" research: "Do NOT render it like a basic developer
    flowchart... ornate borders, decorative corners... The map should
    look like something the player would actually enjoy opening").

    This is still a static Telegram photo, not an interactive canvas --
    no texture asset, no animation, no real gradient blending -- but a
    real double-line bronze border plus corner flourishes, drawn LAST
    (over every grid cell, never under one), is enough to read as a
    deliberately-designed dungeon map rather than a plain grid, at zero
    added render cost or network dependency. Labyrinth-specific by
    design (per Coffee's own closing note: a distinct visual identity
    for THIS map, not necessarily the plain overworld one) -- never
    touches render_layer_map or its shared `_draw_background` above.
    """
    outer = 6
    draw.rectangle([outer, outer, width - outer, height - outer], outline=_ORNATE_FRAME_COLOR, width=3)
    inner = outer + 7
    draw.rectangle([inner, inner, width - inner, height - inner], outline=_ORNATE_FRAME_ACCENT, width=1)
    corner_len = 22
    corners = (
        (outer, outer, 1, 1), (width - outer, outer, -1, 1),
        (outer, height - outer, 1, -1), (width - outer, height - outer, -1, -1),
    )
    for cx, cy, dx, dy in corners:
        draw.line([(cx, cy + dy * corner_len), (cx, cy), (cx + dx * corner_len, cy)], fill=_ORNATE_FRAME_COLOR, width=3)


def _draw_dashed_rect(draw: ImageDraw.ImageDraw, box: list[int], color, width: int = 2, dash: int = 8, gap: int = 6) -> None:
    """PIL has no native dashed-rectangle primitive -- draws one as short line segments around the perimeter, used for the real 'attained, but on another floor' cell marker."""
    x0, y0, x1, y1 = box
    for x in range(x0, x1, dash + gap):
        draw.line([(x, y0), (min(x + dash, x1), y0)], fill=color, width=width)
        draw.line([(x, y1), (min(x + dash, x1), y1)], fill=color, width=width)
    for y in range(y0, y1, dash + gap):
        draw.line([(x0, y), (x0, min(y + dash, y1))], fill=color, width=width)
        draw.line([(x1, y), (x1, min(y + dash, y1))], fill=color, width=width)


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


def floor_of(layer_locations: dict, visited_ids: set[str], location_id: str) -> int:
    """Public wrapper over _floor_levels for one specific location (2026-09-02) -- real floor level relative to its own vertical chain, 0 if it isn't part of one or isn't visited."""
    visited_here = {lid for lid in layer_locations if lid in visited_ids}
    return _floor_levels(layer_locations, visited_here).get(location_id, 0)


def available_floors(layer_locations: dict, visited_ids: set[str]) -> list[int]:
    """
    Real, public helper (2026-09-02, per Coffee: "list the floors in
    push buttons... only show floors or basements when they have
    attained them like in zelda") -- the distinct real floor numbers a
    character has actually visited within one dungeon/layer, for
    building a floor-switcher keyboard. 0 (ground level) is always
    included even if _floor_levels never mentions it explicitly (every
    node not part of a real vertical chain implicitly sits at floor 0).
    """
    visited_here = {lid for lid in layer_locations if lid in visited_ids}
    levels = _floor_levels(layer_locations, visited_here)
    return sorted({0} | set(levels.values()))


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


def _location_icons(location: dict, monsters: dict, quests: dict, location_id: str, layer_locations: dict | None = None) -> list[str]:
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
    # Real live request (2026-09-02, Coffee: "Do not put dungeon maps
    # onto the main map -- it clutters the main world -- have them only
    # on thier own seperate maps, but mark the dungeon entrances on the
    # main map"). A room counts as a real dungeon entrance if it has an
    # actual edge (connections/ascends_to/descends_to) leading to a
    # `dungeon_interior: true` room -- more reliable than checking this
    # room's OWN dungeon_id, since some zones (e.g. Whispering Wood)
    # never tag their own outer hub with dungeon_id at all, only their
    # interior sub-rooms.
    if layer_locations is not None:
        neighbor_ids = list(location.get("connections") or [])
        for k in ("ascends_to", "descends_to"):
            if location.get(k):
                neighbor_ids.append(location[k])
        if any(layer_locations.get(nid, {}).get("dungeon_interior") for nid in neighbor_ids):
            icons.append("dungeon")
    return icons


def render_layer_map(
    layer_name: str,
    layer_locations: dict,
    visited_ids: set[str],
    revealed_ids: set[str],
    current_location_id: str | None,
    monsters: dict | None = None,
    quests: dict | None = None,
    title_override: str | None = None,
    exclude_dungeon_interiors: bool = False,
    floor_filter: int | None = None,
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

    title_override: used by render_dungeon_map below to show a real
    dungeon display name instead of the raw layer_name.

    exclude_dungeon_interiors (2026-09-02, per Coffee: "Do not put
    dungeon maps onto the main map -- it clutters the main world --
    have them only on thier own seperate maps, but mark the dungeon
    entrances"): the real WORLD map (bot.py's _send_layer_map) passes
    True so every `dungeon_interior: true` room is dropped from the
    grid entirely -- render_dungeon_map below never sets this, so a
    dungeon's own map is completely unaffected. `layer_locations`
    itself is deliberately left FULL either way (only `visited_here`/
    `edges` are filtered) so _location_icons can still see a retained
    entrance room's interior neighbor to draw its real "dungeon" badge.

    floor_filter (2026-09-02, per Coffee: "list the floors in push
    buttons... if players are not on that floor but above or below
    that location, mark it with a dotted grey box... only show floors
    or basements when they have attained them like in zelda"): when
    given, only rooms at this real floor level (see _floor_levels,
    defaulting any node with no real vertical membership to 0) get
    their own name/icons drawn -- a cell that's real, attained, but
    belongs to a DIFFERENT floor is drawn as a plain dashed grey box
    (see _draw_dashed_rect) instead, never its real name (that belongs
    to ITS OWN floor's view). None (the default) renders every visited
    room on one flat grid regardless of floor, unchanged from before
    this parameter existed -- only real callers that know a location
    has more than one attained floor (bot.py's own available_floors
    check) ever pass a real value.
    """
    monsters = monsters or {}
    quests = quests or {}
    visited_here, revealed_here, edges, unexplored_counts = _visible_nodes_and_edges(
        layer_locations, visited_ids, revealed_ids,
    )
    if exclude_dungeon_interiors:
        interior_ids = {lid for lid in visited_here if layer_locations[lid].get("dungeon_interior")}
        visited_here = [lid for lid in visited_here if lid not in interior_ids]
        revealed_here = [lid for lid in revealed_here if lid not in interior_ids]
        edges = [e for e in edges if e[0] not in interior_ids and e[1] not in interior_ids]
        unexplored_counts = {k: v for k, v in unexplored_counts.items() if k not in interior_ids}
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
    if floor_filter is not None:
        legend_lines.append("dashed grey = a real room here, on another floor you've reached")
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
    title = title_override or f"MAP — {layer_name.title()}"
    if floor_filter is not None:
        title += f" ({'Ground' if floor_filter == 0 else (f'F{floor_filter}' if floor_filter > 0 else f'B{-floor_filter}')})"
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
            if floor_filter is not None:
                on_floor = [lid for lid in cell_ids if floor_levels.get(lid, 0) == floor_filter]
                if not on_floor:
                    draw.rectangle([px, py, px + CELL_SIZE, py + CELL_SIZE], fill=_OTHER_FLOOR_FILL)
                    _draw_dashed_rect(draw, [px, py, px + CELL_SIZE, py + CELL_SIZE], _OTHER_FLOOR_OUTLINE)
                    continue
                cell_ids = on_floor
            primary = _pick_primary(cell_ids, floor_levels, current_location_id)
            _draw_cell(
                draw, image, layer_name, primary, layer_locations[primary], px, py,
                is_current=(current_location_id in cell_ids),
                name_font=name_font, badge_font=badge_font,
                unexplored=unexplored_counts.get(primary),
                floor_level=floor_levels.get(primary),
                stacked_levels=sorted({floor_levels.get(lid, 0) for lid in cell_ids} - {floor_levels.get(primary, 0)}),
                icons=_location_icons(layer_locations[primary], monsters, quests, primary, layer_locations),
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


def render_dungeon_map(
    dungeon_id: str,
    display_name: str,
    dungeon_locations: dict,
    visited_ids: set[str],
    revealed_ids: set[str],
    current_location_id: str | None,
    monsters: dict | None = None,
    quests: dict | None = None,
    floor_filter: int | None = None,
) -> bytes:
    """
    Real live request (2026-08-30, Coffee: "i want each dungeon to have
    its own minimap"). dungeon_locations: every real CAMPAIGN["locations"]
    entry carrying this dungeon's real `dungeon_id` field, merged across
    layers by the caller -- a dungeon can span more than one real layer
    (Stonearch Gorge: surface + underground), and location ids are
    globally unique so a plain merge is safe. Reuses render_layer_map's
    exact grid/fog-of-war/floor-badge/icon pipeline unchanged -- a
    dungeon's own local grid_position coordinates only need to be
    collision-free among each other (confirmed for all 8 real dungeons),
    not against the rest of that dungeon's real layer, which is exactly
    what scoping the render to just this dungeon's rooms buys.

    floor_filter: see render_layer_map's own docstring -- bot.py's
    _send_dungeon_map only ever passes a real value once it's confirmed
    (via available_floors) this dungeon has more than one real attained
    floor; a single-floor dungeon's map is completely unaffected.
    """
    return render_layer_map(
        dungeon_id, dungeon_locations, visited_ids, revealed_ids, current_location_id,
        monsters=monsters, quests=quests, title_override=f"MAP — {display_name}", floor_filter=floor_filter,
    )


# The Labyrinth (2026-09-02, Phase L2h, per Coffee: "Do not put dungeon
# maps onto the main map... have them only on thier own seperate maps"
# -- extended to the Labyrinth's own live, ephemeral floors, which were
# never on any map at all until now). Deliberately its own small,
# purpose-built renderer, NOT a call into render_layer_map above: that
# engine assumes real, persistent CAMPAIGN locations with real
# generated art worth fetching-once-and-caching-forever and a real
# fog-of-war built up across many real-world sessions -- none of which
# applies to a floor that's fully synthesized on arrival and discarded
# wholesale on the next descend. No network calls at all here.
_LABYRINTH_ICON_COLORS = {
    "monster": (190, 60, 60),
    "miniboss": (140, 20, 160),
    "chest": (200, 170, 60),
    "hazard": (200, 100, 20),
    "switch": (80, 160, 200),
    # Real gap found (2026-09-03, Coffee: "did you miss anything on the
    # generator?"): pressure plates (v1.27.488), carry objects/pillars
    # (v1.27.489), and the hint statue (v1.27.490) all got real text
    # descriptions via _lockable_callout_lines, but were never added
    # here -- a room holding one of these drew with NO icon at all on
    # the actual map image, even though the room text correctly
    # mentioned it.
    "pressure_plate": (180, 140, 60),
    "carry_object": (120, 100, 80),
    "pillar": (130, 110, 150),
    "hint_statue": (110, 110, 100),
}
_LABYRINTH_ROOM_FILL = (55, 48, 68)

# Real room visual hierarchy (2026-09-05, per Coffee's own "Advanced
# Dungeon Map System" research: "Not every room should look identical
# ... Boss Room: make it significantly larger and visually important").
# Actually resizing a boss cell would mean reserving real grid space in
# rules/labyrinth.py's own placement algorithm -- a much bigger, riskier
# change than this visual-only pass calls for. A distinctly-colored,
# thicker (and for a real boss room, double) border achieves the same
# "this room matters" read without touching any placement logic, and
# survives even when a real per-room art tile is pasted over the flat
# fill underneath it (walls are always drawn on top, last). No real
# `is_boss_room` flag exists on any generated room yet (see this
# session's own audit -- only `is_miniboss_room` is real today), so
# only the miniboss tier actually fires until that feature ships; the
# boss tier is ready for it.
_MINIBOSS_ROOM_OUTLINE = _LABYRINTH_ICON_COLORS["miniboss"]
_BOSS_ROOM_OUTLINE = (220, 45, 45)
_BOSS_ROOM_ACCENT = (230, 190, 60)

# Real live request (2026-09-03, Coffee, dev-bridge screenshot: circled
# the generic blue diamond shown for every switch regardless of its own
# real element -- "find appropriate emojis for the elemental colors...
# some kind of label colour or indication"). A drawn dot is a real RGB
# fill, not a text glyph, so this server's missing color-emoji font
# (see this module's own established note) doesn't apply here -- these
# match bot.py's own _SWITCH_ELEMENT_EMOJI mapping one-for-one, just as
# colors instead of characters.
_SWITCH_ELEMENT_COLORS = {
    "fire": (220, 90, 40), "cold": (120, 190, 230), "lightning": (230, 210, 60),
    "force": (150, 90, 220), "radiant": (235, 220, 150), "psychic": (190, 100, 190),
    "poison": (110, 180, 80), "necrotic": (100, 70, 110), "earth": (150, 120, 80),
    "physical": (190, 190, 190),
}
_SWITCH_INACTIVE_COLOR = (75, 75, 80)
_LOCKED_DOOR_COLOR = (200, 150, 40)
# Real live request (2026-09-05, per Coffee's own "Advanced Dungeon Map
# System" research: "Every important gateway should have a visual
# identity... The player should be able to understand: I know where I
# need to go, I just don't have the ability to get there yet"). Every
# elemental switch gate already drew in its own real element's color
# (_labyrinth_locked_door_color, 2026-09-03); key/rune/plate gates all
# fell back to the one flat generic gold regardless of which real
# mechanic actually gates them. Each kind now gets its own real,
# consistent color -- reused from the SAME dot each lockable's own map
# icon already draws (_LABYRINTH_ICON_COLORS), so "the wall ahead" and
# "the room with the thing that opens it" visually match at a glance.
_KEY_GATE_COLOR = (225, 180, 70)
_RUNE_GATE_COLOR = (170, 70, 190)
_WALL_WIDTH = 4
_DOOR_GAP_FRACTION = 0.4  # the middle 40% of a connected edge is left open
_SEGMENT_STRIP_HEIGHT = 56
_SEGMENT_BOX_SIZE = 34
_SEGMENT_BOX_GAP = 22
_SEGMENT_CHECKPOINT_COLOR = (200, 170, 60)


def _draw_labyrinth_segment_strip(
    draw: "ImageDraw.ImageDraw", canvas_width: int, top: int, segment_floors: list[int], current_floor: int,
) -> None:
    """
    A small horizontal strip -- one box per real floor in this segment,
    connected by plain lines -- showing where `current_floor` sits
    within the whole 5-floor segment (see render_labyrinth_map's own
    docstring for the real request and the confirmed-linear real
    structure this represents honestly, not a fabricated web). The
    LAST floor (the segment's real checkpoint/waystation) is drawn in
    its own color -- a fact the tutorial already tells every player
    about, never a spoiler.
    """
    n = len(segment_floors)
    total_w = n * _SEGMENT_BOX_SIZE + (n - 1) * _SEGMENT_BOX_GAP
    start_x = (canvas_width - total_w) / 2
    cy = top + _SEGMENT_STRIP_HEIGHT / 2 - 8
    box_font = _load_font(14, bold=True)
    centers = []
    for i, floor_num in enumerate(segment_floors):
        bx = start_x + i * (_SEGMENT_BOX_SIZE + _SEGMENT_BOX_GAP)
        centers.append(bx + _SEGMENT_BOX_SIZE / 2)
    for i, floor_num in enumerate(segment_floors):
        bx = start_x + i * (_SEGMENT_BOX_SIZE + _SEGMENT_BOX_GAP)
        box = [bx, cy - _SEGMENT_BOX_SIZE / 2, bx + _SEGMENT_BOX_SIZE, cy + _SEGMENT_BOX_SIZE / 2]
        if i > 0:
            draw.line([(centers[i - 1] + _SEGMENT_BOX_SIZE / 2, cy), (centers[i] - _SEGMENT_BOX_SIZE / 2, cy)], fill=_LEGEND_COLOR, width=2)
        is_current = floor_num == current_floor
        is_checkpoint = i == n - 1
        fill = _SEGMENT_CHECKPOINT_COLOR if is_checkpoint else _LABYRINTH_ROOM_FILL
        outline = _CELL_OUTLINE_CURRENT if is_current else _CELL_OUTLINE
        draw.rectangle(box, fill=fill, outline=outline, width=3 if is_current else 2)
        label = str(floor_num)
        lb = draw.textbbox((0, 0), label, font=box_font)
        lw, lh = lb[2] - lb[0], lb[3] - lb[1]
        text_color = (20, 20, 20) if is_checkpoint else _TITLE_COLOR
        draw.text((bx + (_SEGMENT_BOX_SIZE - lw) / 2, cy - lh / 2 - lb[1]), label, font=box_font, fill=text_color)
    caption_font = _load_font(11)
    caption = "this segment's floors — gold box = waystation"
    cap_bbox = draw.textbbox((0, 0), caption, font=caption_font)
    draw.text(((canvas_width - (cap_bbox[2] - cap_bbox[0])) / 2, cy + _SEGMENT_BOX_SIZE / 2 + 6), caption, font=caption_font, fill=_LEGEND_COLOR)


def _labyrinth_room_connections(room_id: str, rooms: dict) -> tuple[set[str], set[str]]:
    """
    Real live request (2026-09-03, Coffee, dev-bridge screenshot of a
    real Zelda dungeon map: "research this map to understand how the
    player would get from the beginning to the end... I want this
    implementation built-in"). The Zelda reference draws an actual
    open doorway where two rooms connect and a solid wall where they
    don't -- our own map previously drew every room as an identical
    closed box with no visual sense of which neighbors actually link
    up, even though that's already real data (`connections`/`locked_
    connections`). Returns (open_neighbor_ids, locked_neighbor_ids) for
    `room_id`, checked from BOTH sides (a plain connection is always
    declared mutually by generate_floor/generate_segment, but this is
    defensive rather than assuming that invariant holds forever).
    """
    room = rooms[room_id]
    open_ids = set(room.get("connections", []))
    locked_ids = set(room.get("locked_connections", {}).keys())
    for other_id, other in rooms.items():
        if other_id == room_id:
            continue
        if room_id in other.get("connections", []):
            open_ids.add(other_id)
        if room_id in other.get("locked_connections", {}):
            locked_ids.add(other_id)
    return open_ids, locked_ids


def _labyrinth_locked_door_color(room_id: str, neighbor_id: str, rooms: dict) -> tuple:
    """
    Real live request (2026-09-03, Coffee, dev-bridge screenshot,
    element-icon ask -- see `_SWITCH_ELEMENT_COLORS`'s own docstring
    for the switch-dot half of this already-shipped): a locked door
    gated by an elemental switch draws in THAT element's own color
    instead of the flat generic gold, so a player can visually match
    "this door -> that switch" at a glance.

    Extended 2026-09-05 (per Coffee's own "Advanced Dungeon Map System"
    research -- "Every important gateway should have a visual
    identity"): key/rune/pressure-plate gates now each get their own
    real, distinct, consistent color too (matching their own lockable's
    map dot), not just switches. Falls back to `_LOCKED_DOOR_COLOR` only
    for a plain DC13 door with no real mechanic to color-code it to.
    """
    room = rooms.get(room_id) or {}
    lockable_id = room.get("locked_connections", {}).get(neighbor_id)
    if lockable_id is None:
        neighbor = rooms.get(neighbor_id) or {}
        lockable_id = neighbor.get("locked_connections", {}).get(room_id)
        room = neighbor
    if lockable_id is None:
        return _LOCKED_DOOR_COLOR
    lockable = next((lk for lk in room.get("lockables", []) if lk["id"] == lockable_id), None)
    if lockable is None:
        return _LOCKED_DOOR_COLOR
    if lockable.get("requires_key_item"):
        return _KEY_GATE_COLOR
    if lockable.get("requires_rune_item"):
        return _RUNE_GATE_COLOR
    if lockable.get("kind") == "pressure_plate":
        return _LABYRINTH_ICON_COLORS["pressure_plate"]
    switch_ids = lockable.get("requires") or ([lockable["id"]] if lockable.get("kind") == "switch" else [])
    for switch_id in switch_ids:
        for r in rooms.values():
            for lk in r.get("lockables", []):
                if lk["id"] == switch_id and lk.get("element"):
                    return _SWITCH_ELEMENT_COLORS.get(lk["element"], _LOCKED_DOOR_COLOR)
    return _LOCKED_DOOR_COLOR


def _draw_labyrinth_room_walls(
    draw: "ImageDraw.ImageDraw", px: int, py: int, cell_size: int,
    room_id: str, rooms: dict, position: tuple[int, int], by_cell: dict, outline_color: tuple, outline_width: int,
) -> None:
    """
    Draws each of a room's 4 edges individually instead of one plain
    rectangle: a real open connection leaves a gap in the middle of
    that edge (a doorway), a locked_connection draws that edge in a
    distinct color (a real, visible gate), and no connection at all (or
    no neighbor there) draws a plain solid wall -- see
    _labyrinth_room_connections's own docstring for the real request
    this answers. `position` is this room's own (x, y) grid coordinate;
    `by_cell` maps (x, y) -> room_id for the whole floor, used to find
    whichever neighbor (if any) actually sits on each side.
    """
    open_ids, locked_ids = _labyrinth_room_connections(room_id, rooms)
    x, y = position
    # (dx, dy) in grid space, and the two pixel-space corners of that edge.
    edges = [
        ((0, 1), (px, py), (px + cell_size, py)),                                   # north
        ((0, -1), (px, py + cell_size), (px + cell_size, py + cell_size)),          # south
        ((-1, 0), (px, py), (px, py + cell_size)),                                  # west
        ((1, 0), (px + cell_size, py), (px + cell_size, py + cell_size)),           # east
    ]
    for (dx, dy), p1, p2 in edges:
        neighbor_id = by_cell.get((x + dx, y + dy))
        if neighbor_id is None:
            draw.line([p1, p2], fill=outline_color, width=outline_width)
            continue
        if neighbor_id in locked_ids:
            door_color = _labyrinth_locked_door_color(room_id, neighbor_id, rooms)
            draw.line([p1, p2], fill=door_color, width=outline_width + 1)
        elif neighbor_id in open_ids:
            mid1 = (p1[0] + (p2[0] - p1[0]) * (0.5 - _DOOR_GAP_FRACTION / 2), p1[1] + (p2[1] - p1[1]) * (0.5 - _DOOR_GAP_FRACTION / 2))
            mid2 = (p1[0] + (p2[0] - p1[0]) * (0.5 + _DOOR_GAP_FRACTION / 2), p1[1] + (p2[1] - p1[1]) * (0.5 + _DOOR_GAP_FRACTION / 2))
            draw.line([p1, mid1], fill=outline_color, width=outline_width)
            draw.line([mid2, p2], fill=outline_color, width=outline_width)
        else:
            draw.line([p1, p2], fill=outline_color, width=outline_width)


def _labyrinth_room_icons(room: dict, switch_states: dict[str, bool] | None = None) -> list[tuple[str, tuple]]:
    """
    Returns (category, color) pairs, not just category names -- a
    switch's own dot color now reflects its REAL current element/state
    (bright, element-colored when active; a plain dim grey when not),
    rather than one fixed blue regardless. `switch_states` (real
    current `_lockable_is_open` results, keyed by lockable id) is
    optional so any caller not yet updated still gets the old
    always-inactive-colored behavior instead of crashing.
    """
    switch_states = switch_states or {}
    icons = []
    if room.get("is_miniboss_room"):
        icons.append(("miniboss", _LABYRINTH_ICON_COLORS["miniboss"]))
    elif room.get("monsters"):
        icons.append(("monster", _LABYRINTH_ICON_COLORS["monster"]))
    if any(lk.get("kind") == "chest" for lk in room.get("lockables", [])):
        icons.append(("chest", _LABYRINTH_ICON_COLORS["chest"]))
    if room.get("hazard"):
        icons.append(("hazard", _LABYRINTH_ICON_COLORS["hazard"]))
    for lockable in room.get("lockables", []):
        if lockable.get("kind") not in ("switch", "multi_switch_gate"):
            continue
        active = switch_states.get(lockable["id"], False)
        color = _SWITCH_ELEMENT_COLORS.get(lockable.get("element"), _LABYRINTH_ICON_COLORS["switch"]) if active else _SWITCH_INACTIVE_COLOR
        icons.append(("switch", color))
    # Real gap found (2026-09-03, Coffee: "did you miss anything on the
    # generator?") -- these four real kinds (v1.27.488-490) never got a
    # map icon at all. `pillar` reflects its own real struck/unstruck
    # state the same way `switch` already does (both share the
    # `_SWITCH_STATE` store); the other three are simple, permanent
    # presence dots.
    for lockable in room.get("lockables", []):
        kind = lockable.get("kind")
        if kind == "pressure_plate":
            icons.append(("pressure_plate", _LABYRINTH_ICON_COLORS["pressure_plate"]))
        elif kind == "carry_object":
            icons.append(("carry_object", _LABYRINTH_ICON_COLORS["carry_object"]))
        elif kind == "pillar":
            active = switch_states.get(lockable["id"], False)
            color = _SWITCH_INACTIVE_COLOR if active else _LABYRINTH_ICON_COLORS["pillar"]
            icons.append(("pillar", color))
        elif kind == "hint_statue":
            icons.append(("hint_statue", _LABYRINTH_ICON_COLORS["hint_statue"]))
    return icons


def render_labyrinth_map(
    floor: int, rooms: dict, current_room_id: str,
    locked_room_ids: set[str] | None = None, visited_room_ids: set[str] | None = None,
    switch_states: dict[str, bool] | None = None, segment_floors: list[int] | None = None,
) -> bytes:
    """
    rooms: a live run's own `rooms` dict verbatim, every entry already
    carrying a real `grid_position` (rules.labyrinth._assign_grid_
    positions).

    segment_floors (2026-09-03, per Coffee, dev-bridge screenshot of a
    real Zelda dungeon map: "I want our dungeons and labyrinth to have
    maps like this" -- specifically referencing his own earlier "5
    levels of honeycombing" framing, rules.labyrinth.py's own real term
    for a segment). Every floor in the CURRENT segment (real, since
    generate_segment builds all SEGMENT_SIZE floors together up front),
    e.g. [1, 2, 3, 4, 5] or [16, 17, 18, 19, 20] -- drawn as a small
    strip of numbered boxes under the title, the floor being viewed
    highlighted, so the map conveys "you're on floor 3 of this 5-floor
    segment" at a glance instead of showing one flat floor in total
    isolation. `None` (the default) skips the strip entirely for any
    caller not yet updated. The real cross-floor structure is a plain
    linear chain (floor k's connector descends_to floor k+1's hub, see
    generate_segment) -- never a real lattice -- so a simple connected
    strip is an honest, not oversimplified, picture of it.

    switch_states (2026-09-03, per Coffee: "find appropriate emojis for
    the elemental colors... some kind of label colour or indication") --
    real current `_lockable_is_open` results for every switch/multi_
    switch_gate lockable on this floor, keyed by lockable id. This
    module has no DB/chat-state access of its own (by design, see the
    "no network calls" note elsewhere in this file), so the caller
    computes this and passes it in as plain data -- see
    `_labyrinth_room_icons` for how it's actually used.

    locked_room_ids (2026-09-02, real live confusion, Coffee: "the map
    is showing a north location but its not available to travel too" --
    a real, honest room the map already revealed, gated behind a
    multi-switch puzzle he hadn't solved yet, drawn IDENTICALLY to
    every genuinely reachable room). Rooms in this set get the same
    real dashed-border convention render_layer_map already uses for an
    "attained but not currently reachable" cell -- still honestly
    shown (never hidden), just visually distinct from a room the party
    can actually walk into right now.

    visited_room_ids (2026-09-03, per Coffee: "i want fog of war in the
    labyrinth - specially if they get larger, we shud be exploring
    them" -- reverses this function's original "a whole floor is
    always fully revealed" design). When given, a room NOT in this set
    is drawn IDENTICALLY to a grid position with no room at all (solid
    black, no outline) -- real fog of war (2026-09-04, per Coffee's own
    follow-up: "make sure any area that is unexplored that you don't
    actually show the black box/outline on the map so we dont kno the
    shape of the map"; the previous grey-box-with-outline rendering
    still revealed exactly where every unvisited room sat and the
    overall shape of the floor before it was ever walked). The ONLY
    honest hint that more lies beyond a visited room is that room's own
    real doorway rendering (a gap = a real passage, a gold wall = a
    real gate) -- never the destination room's own shape or content.
    `None` (the default) keeps the old fully-revealed behavior for any
    caller that hasn't been updated to track visits yet.
    """
    locked_room_ids = locked_room_ids or set()
    positions = {rid: (r["grid_position"]["x"], r["grid_position"]["y"]) for rid, r in rooms.items()}
    xs = [p[0] for p in positions.values()]
    ys = [p[1] for p in positions.values()]
    min_x, max_x, min_y, max_y = min(xs), max(xs), min(ys), max(ys)
    cols = max_x - min_x + 1
    grid_rows = max_y - min_y + 1

    legend_lines = ["red outline = you are here", "gap in the wall = a real doorway, solid wall = no connection"]
    # Real gateway-type visual identity (2026-09-05, per Coffee's own
    # "Advanced Dungeon Map System" research: "Every important gateway
    # should have a visual identity... I know where I need to go, I
    # just don't have the ability to get there yet"). Each real gate
    # kind's wall color now matches the SAME color its own lockable's
    # map dot already uses, so only the kinds actually present on this
    # floor get a legend line -- never a generic catch-all that doesn't
    # tell the player anything real.
    if any(lk.get("requires_key_item") for r in rooms.values() for lk in r.get("lockables", [])):
        legend_lines.append("amber wall = a key-gated door")
    if any(lk.get("requires_rune_item") for r in rooms.values() for lk in r.get("lockables", [])):
        legend_lines.append("violet wall = a rune-gated doorway")
    if any(
        r.get("locked_connections") and not any(lk.get("requires_key_item") or lk.get("requires_rune_item") or lk.get("kind") == "pressure_plate" for lk in r.get("lockables", []))
        for r in rooms.values()
    ):
        legend_lines.append("gold wall = a locked/gated connection")
    if locked_room_ids & set(rooms.keys()):
        legend_lines.append("dashed outline = seen, not yet reachable")
    if any(lk.get("kind") in ("switch", "multi_switch_gate") for r in rooms.values() for lk in r.get("lockables", [])):
        legend_lines.append("switch dot color = its element, dim grey = inactive (a gated door matches its own switch's color too)")
    if any(lk.get("kind") == "pressure_plate" for r in rooms.values() for lk in r.get("lockables", [])):
        legend_lines.append("tan dot/wall = a real pressure plate and its own gate")
    if any(lk.get("kind") == "carry_object" for r in rooms.values() for lk in r.get("lockables", [])):
        legend_lines.append("brown dot = a real object you can carry")
    if any(lk.get("kind") == "pillar" for r in rooms.values() for lk in r.get("lockables", [])):
        legend_lines.append("mauve dot = an unstruck pillar, dim grey = already struck")
    if any(lk.get("kind") == "hint_statue" for r in rooms.values() for lk in r.get("lockables", [])):
        legend_lines.append("grey-stone dot = a real hint statue, worth examining")
    if any(r.get("is_boss_room") for r in rooms.values()):
        legend_lines.append("thick red double border = a real boss room")
    if any(r.get("is_miniboss_room") for r in rooms.values()):
        legend_lines.append("thick purple border = a real mini-boss room")
    legend_row_count = len(legend_lines) + 1
    strip_height = _SEGMENT_STRIP_HEIGHT if segment_floors else 0
    width = min(max(_MARGIN * 2 + cols * CELL_SIZE, _MIN_CANVAS_WIDTH), _MAX_CANVAS_WIDTH)
    height = min(
        _MARGIN * 2 + _TITLE_HEIGHT + strip_height + grid_rows * CELL_SIZE + legend_row_count * _LEGEND_LINE_HEIGHT + 10,
        _MAX_CANVAS_HEIGHT,
    )

    image = Image.new("RGB", (width, height), _BG_COLOR)
    draw = ImageDraw.Draw(image, "RGBA")
    _draw_background(draw, width, height)

    title_font = _load_font(24, bold=True)
    title = f"THE LABYRINTH — Floor {floor}"
    title_bbox = draw.textbbox((0, 0), title, font=title_font)
    draw.text(((width - (title_bbox[2] - title_bbox[0])) / 2, 10), title, font=title_font, fill=_TITLE_COLOR)

    if segment_floors:
        _draw_labyrinth_segment_strip(draw, width, _MARGIN + _TITLE_HEIGHT, segment_floors, floor)

    name_font = _load_font(12, bold=True)
    by_cell = {pos: rid for rid, pos in positions.items()}
    grid_top = _MARGIN + _TITLE_HEIGHT + strip_height
    for gy in range(grid_rows):
        for gx in range(cols):
            x = min_x + gx
            y = max_y - gy  # same north-at-top convention as render_layer_map
            px = _MARGIN + gx * CELL_SIZE
            py = grid_top + gy * CELL_SIZE
            room_id = by_cell.get((x, y))
            if room_id is None:
                draw.rectangle([px, py, px + CELL_SIZE, py + CELL_SIZE], fill=_CELL_BLACK)
                continue
            room = rooms[room_id]
            is_current = room_id == current_room_id
            is_locked = room_id in locked_room_ids
            # A room the party is CURRENTLY standing in is always
            # visited by definition, regardless of what the caller's
            # own visited-tracking set says (defensive -- arrival
            # should always mark it, but never trust that alone).
            is_unvisited = visited_room_ids is not None and room_id not in visited_room_ids and not is_current

            if is_unvisited:
                # Real fog of war (2026-09-04) -- drawn IDENTICALLY to
                # the room_id-is-None case just above, on purpose: no
                # outline, no distinct fill, nothing that would let a
                # player infer a room even exists here before they've
                # actually walked into it.
                draw.rectangle([px, py, px + CELL_SIZE, py + CELL_SIZE], fill=_CELL_BLACK)
                continue

            # Real per-room generated art (2026-09-04, per Coffee: "use
            # the location images that you generate when we look around
            # on the map so it looks more visually stimulating") --
            # drawn as the cell's real background, same paste-then-draw-
            # walls-on-top order render_layer_map's own _draw_cell uses.
            # Falls back to the plain flat fill on any failure (no
            # network, a lightless room, a cold cache miss) -- never
            # blocks or crashes the whole map render.
            draw.rectangle([px, py, px + CELL_SIZE, py + CELL_SIZE], fill=_LABYRINTH_ROOM_FILL)
            tile = _fetch_labyrinth_room_tile(room)
            if tile is not None:
                if tile.size != (CELL_SIZE, CELL_SIZE):
                    tile = tile.resize((CELL_SIZE, CELL_SIZE))
                image.paste(tile, (px, py))
            if is_locked:
                _draw_dashed_rect(draw, [px, py, px + CELL_SIZE, py + CELL_SIZE], _OTHER_FLOOR_OUTLINE)
            else:
                # Room visual hierarchy (2026-09-05): "you are here"
                # always wins the border when it applies (the single
                # most important real-time signal); otherwise a real
                # boss/miniboss room gets its own distinct, thicker
                # accent instead of the plain generic outline, visible
                # even over a pasted room-art tile.
                is_boss_room = bool(room.get("is_boss_room"))
                is_miniboss_room = bool(room.get("is_miniboss_room"))
                if is_current:
                    outline, outline_width = _CELL_OUTLINE_CURRENT, 4
                elif is_boss_room:
                    outline, outline_width = _BOSS_ROOM_OUTLINE, 5
                elif is_miniboss_room:
                    outline, outline_width = _MINIBOSS_ROOM_OUTLINE, 3
                else:
                    outline, outline_width = _CELL_OUTLINE, 2
                _draw_labyrinth_room_walls(
                    draw, px, py, CELL_SIZE, room_id, rooms, (x, y), by_cell, outline, outline_width,
                )
                if is_boss_room and not is_current:
                    # A real boss room gets a genuine double border --
                    # a second, inset gold line -- so it visually reads
                    # as "significantly more important" from across the
                    # whole map, not just a slightly thicker single line.
                    inset = outline_width + 3
                    draw.rectangle(
                        [px + inset, py + inset, px + CELL_SIZE - inset, py + CELL_SIZE - inset],
                        outline=_BOSS_ROOM_ACCENT, width=2,
                    )

            # No emoji prefix here -- this server has no color-emoji
            # font (see this module's own established note above), so
            # a 🔒 glyph would just render as an empty tofu box. The
            # dashed border plus the legend line is the real signal.
            label = _fit_label_to_width(draw, room["name"], name_font, CELL_SIZE - 8)
            label_bbox = draw.textbbox((0, 0), label, font=name_font)
            lw, lh = label_bbox[2] - label_bbox[0], label_bbox[3] - label_bbox[1]
            draw.rectangle([px, py + CELL_SIZE - lh - 8, px + CELL_SIZE, py + CELL_SIZE], fill=_LABEL_BG)
            draw.text((px + (CELL_SIZE - lw) / 2, py + CELL_SIZE - lh - 5), label, font=name_font, fill=_LABEL_COLOR)

            icons = _labyrinth_room_icons(room, switch_states)
            if icons:
                swatch_r = 6
                pad = 4
                swatch_span = swatch_r * 2 + 3
                strip_w = len(icons) * swatch_span + pad
                draw.rectangle([px + 2, py + 2, px + 2 + strip_w, py + 2 + swatch_r * 2 + pad], fill=_LABEL_BG)
                cx = px + 2 + pad // 2 + swatch_r
                cy = py + 2 + pad // 2 + swatch_r
                for _category, color in icons:
                    draw.ellipse([cx - swatch_r, cy - swatch_r, cx + swatch_r, cy + swatch_r], fill=color, outline=(0, 0, 0))
                    cx += swatch_span

    # Warp shortcuts are deliberately NOT drawn on the map (2026-09-04,
    # per Coffee: "Please remove this purple straight line. We don't
    # need to know where the warp locations go.") -- the connection
    # itself still functions in rules/labyrinth.py, this only removes
    # the map spoiler. A player still discovers where a warp leads by
    # actually using it.

    legend_font = _load_font(13)
    legend_y = height - legend_row_count * _LEGEND_LINE_HEIGHT - 8
    cursor_x = _MARGIN
    for label, color in _LABYRINTH_ICON_COLORS.items():
        swatch_r = 5
        cy = legend_y + 8
        draw.ellipse([cursor_x, cy - swatch_r, cursor_x + swatch_r * 2, cy + swatch_r], fill=color)
        cursor_x += swatch_r * 2 + 5
        text = f"{label}   "
        draw.text((cursor_x, legend_y), text, font=legend_font, fill=_LEGEND_COLOR)
        bbox = draw.textbbox((0, 0), text, font=legend_font)
        cursor_x += bbox[2] - bbox[0]
    legend_y += _LEGEND_LINE_HEIGHT
    for line in legend_lines:
        fitted = _fit_label_to_width(draw, line, legend_font, width - 2 * _MARGIN)
        draw.text((_MARGIN, legend_y), fitted, font=legend_font, fill=_LEGEND_COLOR)
        legend_y += _LEGEND_LINE_HEIGHT

    _draw_ornate_labyrinth_frame(draw, width, height)

    buffer = io.BytesIO()
    image.save(buffer, format="PNG")
    return buffer.getvalue()
