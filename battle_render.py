"""
battle_render.py
Task #9-followup, per Coffee (2026-08-08/09): "when we are in battle,
are you able to create an image with enemy sprites and character
sprites that show the formations and locations of where the players
are battling... I want this to be actively reflecting the battle in
real time." Confirmed live (single-letter "a" reply, earlier in the
same conversation) that the wanted shape is a real TACTICAL DIAGRAM --
positions and formation genuinely reflecting the fight state -- not a
single AI-painted illustration (that's what images.py's Pollinations
calls already give for monster/spell/defeat art elsewhere).

No sprite art pipeline exists in this codebase (images.py is a thin
wrapper around a free text-to-image API, one full scene per call --
not per-entity cutout sprites suitable for compositing), and building
one is out of scope for this pass. This renders a clean, legible
token-based battlefield instead, purely locally with Pillow: no
network call, no external dependency, near-instant -- genuinely
reflects each side's real formation_row (front/back), each
combatant's real current HP, and boss status, straight from the real
combat session state, never invented.

Deliberately excludes any interactable-environment detail (per
Coffee's "don't show interactable environment stuff unless the
character looks first, or unless something triggers the event") --
this only ever draws combatants, nothing about the location itself.
"""
import io
import os

from PIL import Image, ImageDraw, ImageFont

CANVAS_WIDTH = 900
CANVAS_HEIGHT = 500

_PARTY_COLOR = (59, 110, 168)
_ENEMY_COLOR = (168, 59, 59)
_BOSS_RING_COLOR = (212, 175, 55)
_TOKEN_RADIUS = 40
_TOKEN_RADIUS_MIN = 20  # never shrink smaller than this -- initials/HP text stop being legible below it
_TOKEN_OUTLINE = (20, 20, 20)
_NAME_COLOR = (240, 240, 240)
_HP_BAR_WIDTH = 84
_HP_BAR_HEIGHT = 9
_HP_BAR_BG = (40, 40, 40)
# Real bug (2026-08-09, Coffee, Development-topic screenshot: 4 combatants
# stacked in one formation row overlapped each other's name/HP-bar text):
# a token's full vertical footprint (circle + name label + HP bar + HP
# text, all measured at the base _TOKEN_RADIUS=40 layout below) works out
# to ~132px, but the old code always drew every token at that fixed size
# and just divided the available height evenly -- for 4+ per row that's
# far less than 132px each, so the next token's circle landed on top of
# the previous one's label. FOOTPRINT_RATIO is that measured constant
# (footprint = _TOKEN_RADIUS_BASE * FOOTPRINT_RATIO); used to scale the
# token down (radius, fonts, bar) only as far as actually needed to fit
# a crowded row, never below _TOKEN_RADIUS_MIN.
_FOOTPRINT_RATIO = 3.3

# Condition badges (2026-08-10, self-initiated -- "make this more
# visually stimulating with what you have available"): sessions.py's
# real in-memory conditions (prone/poisoned/paralyzed/etc, see
# bot.py's _apply_condition and friends) were already tracked and
# narrated in text, but never shown on the formation image itself --
# a player had to scroll back through text to remember who was
# currently prone or poisoned mid-fight. Small colored text badges
# under the HP line, drawn only when a combatant actually has an
# active condition, using the exact same real per-participant
# `conditions` list combat already reads from -- never a new/invented
# fact, same "ground truth from the rules layer" convention as
# everything else this file draws.
_CONDITION_LABELS = {
    "prone": ("PRONE", (200, 200, 90)),
    "poisoned": ("POISONED", (110, 210, 110)),
    "paralyzed": ("PARALYZED", (230, 200, 50)),
    "frightened": ("FRIGHTENED", (170, 110, 220)),
    "banished": ("BANISHED", (140, 140, 140)),
    "death_warded": ("WARDED", (212, 175, 55)),
}

_FONT_DIR = "/usr/share/fonts/truetype/dejavu"
_FONT_BOLD_PATH = os.path.join(_FONT_DIR, "DejaVuSans-Bold.ttf")
_FONT_REGULAR_PATH = os.path.join(_FONT_DIR, "DejaVuSans.ttf")


def _load_font(size: int, bold: bool = False) -> ImageFont.FreeTypeFont:
    """
    Real DejaVu fonts ship on this box (confirmed via `fc-list`), but
    this falls back to Pillow's built-in bitmap font rather than
    crashing if this ever runs somewhere without that exact path --
    an ugly fallback font beats a broken feature.
    """
    path = _FONT_BOLD_PATH if bold else _FONT_REGULAR_PATH
    try:
        return ImageFont.truetype(path, size)
    except OSError:
        return ImageFont.load_default()


def _truncate_name(name: str, limit: int = 14) -> str:
    return name if len(name) <= limit else name[: limit - 1] + "…"


_LEADING_ARTICLES = {"the", "a", "an"}


def _meaningful_first_word(name: str) -> str:
    """
    Real live bug (2026-08-10, Coffee, dev-bridge screenshot): 4 copies
    of a boss rendered with an identical, useless "T"/"The" initial
    and label -- both _first_name below and the token's own big
    initial letter took the LITERAL first word, and "The" is a real,
    common naming convention in this campaign (9 of 56 monsters: "The
    Unspoken," "The Waking Ember," "The Colosseum Champion," etc.) --
    overwhelmingly BOSSES, exactly the fights where a clear,
    distinguishing label matters most, and exactly where Coffee hit
    this (fighting "The Unspoken"). Skips a leading "the"/"a"/"an"
    article and uses the next real word instead; a name that's ONLY
    an article plus nothing else falls back to the article itself
    rather than returning empty.
    """
    words = name.split()
    if not words:
        return "?"
    if len(words) > 1 and words[0].lower() in _LEADING_ARTICLES:
        return words[1]
    return words[0]


def _first_name(name: str, limit: int = 14) -> str:
    """
    Real request (2026-08-09, Coffee: "you can use first names to save
    in spacing"). Falls back to the full (still truncated) name for a
    single-word name like "Grimsby" -- there's nothing shorter to use.
    """
    if not name:
        return "?"
    return _truncate_name(_meaningful_first_word(name), limit)


def _hp_bar_color(hp_current: int, hp_max: int) -> tuple:
    if hp_max <= 0:
        return (100, 100, 100)
    pct = hp_current / hp_max
    if pct > 0.6:
        return (76, 175, 80)
    if pct > 0.3:
        return (255, 193, 7)
    return (211, 47, 47)


# Battle-formation icons (2026-08-10, per Coffee: "Is it possible too
# use face profile icons instead of letters?!"). Real portraits per
# combatant would need a network image-generation call per token, at
# odds with this file's whole point (instant, network-free -- see the
# module docstring); a real face for a monster template would also
# need an invented appearance this codebase's own "never invent a
# game fact" discipline doesn't allow. Instead: simple, procedurally
# drawn (pure Pillow, no asset files) pictograms keyed off REAL,
# already-existing per-combatant facts -- a party member's own
# char_class (12 real classes, rules/leveling.CLASS_HIT_DICE), or an
# enemy's own damage_type (real field on every monster template, the
# same elemental-flavor system already narrated in combat text). Falls
# back to the existing initial-letter treatment whenever neither is
# present/recognized (e.g. a companion's stats haven't loaded, or a
# genuinely new damage_type this file hasn't been taught yet) -- never
# a blank token.
_CLASS_ICON_KEYS = {
    "barbarian", "fighter", "paladin", "ranger", "bard", "cleric",
    "druid", "monk", "rogue", "warlock", "sorcerer", "wizard",
}
_DAMAGE_TYPE_ICON_KEYS = {
    "physical", "fire", "cold", "lightning", "poison", "necrotic",
    "radiant", "force", "psychic",
}


def _icon_key_for_combatant(combatant: dict) -> str | None:
    """Pure, testable-without-Pillow resolver -- see the icon system's own module comment above."""
    char_class = (combatant.get("char_class") or "").lower()
    if char_class in _CLASS_ICON_KEYS:
        return char_class
    damage_type = (combatant.get("damage_type") or "").lower()
    if damage_type in _DAMAGE_TYPE_ICON_KEYS:
        return damage_type
    return None


def _draw_sword(draw: ImageDraw.ImageDraw, x: float, y: float, r: float, color: tuple) -> None:
    # Diagonal blade (not vertical) so this reads as a sword, not a
    # plain "+" -- confirmed live via a rendered test PNG that a
    # straight vertical blade + horizontal guard is visually
    # indistinguishable from _draw_cross (cleric) at small token sizes.
    import math
    w = max(2, r * 0.12)
    hilt = (x - r * 0.42, y + r * 0.42)
    tip = (x + r * 0.42, y - r * 0.42)
    draw.line([hilt, tip], fill=color, width=round(w))
    # Crossguard: a short perpendicular line 30% of the way up the blade.
    guard_center = (hilt[0] + (tip[0] - hilt[0]) * 0.3, hilt[1] + (tip[1] - hilt[1]) * 0.3)
    perp = (-1 / math.sqrt(2), -1 / math.sqrt(2))
    guard_len = r * 0.28
    draw.line(
        [(guard_center[0] - perp[0] * guard_len, guard_center[1] - perp[1] * guard_len),
         (guard_center[0] + perp[0] * guard_len, guard_center[1] + perp[1] * guard_len)],
        fill=color, width=round(w * 0.9),
    )
    pommel_r = r * 0.1
    draw.ellipse([hilt[0] - pommel_r, hilt[1] - pommel_r, hilt[0] + pommel_r, hilt[1] + pommel_r], fill=color)


def _draw_crossed_daggers(draw: ImageDraw.ImageDraw, x: float, y: float, r: float, color: tuple) -> None:
    w = max(2, r * 0.1)
    draw.line([(x - r * 0.45, y - r * 0.45), (x + r * 0.45, y + r * 0.45)], fill=color, width=round(w))
    draw.line([(x - r * 0.45, y + r * 0.45), (x + r * 0.45, y - r * 0.45)], fill=color, width=round(w))


def _draw_axe(draw: ImageDraw.ImageDraw, x: float, y: float, r: float, color: tuple) -> None:
    # A single big, unmistakably curved blade on ONE side of the
    # handle (a real axe silhouette), not the small triangular nub the
    # first version drew -- confirmed live via a rendered test PNG
    # that read as a flag/pennant, not an axe.
    w = max(2, r * 0.11)
    draw.line([(x, y - r * 0.6), (x, y + r * 0.6)], fill=color, width=round(w))
    draw.pieslice([x - r * 0.75, y - r * 0.62, x + r * 0.05, y + r * 0.05], start=290, end=90, fill=color)


def _draw_arrow(draw: ImageDraw.ImageDraw, x: float, y: float, r: float, color: tuple) -> None:
    # A simple arrow (shaft + broad triangular head) -- switched from
    # an earlier bow-silhouette attempt that a rendered test PNG
    # showed reading as an ambiguous flag/checkmark at small token
    # sizes; an arrow's diagonal shaft + clear triangle head stays
    # legible even shrunk down for a crowded row.
    w = max(2, r * 0.1)
    tail = (x - r * 0.5, y + r * 0.5)
    head = (x + r * 0.35, y - r * 0.35)
    draw.line([tail, head], fill=color, width=round(w))
    draw.polygon(
        [(head[0] + r * 0.22, head[1] - r * 0.22), (head[0] - r * 0.15, head[1] + r * 0.05),
         (head[0] + r * 0.05, head[1] - r * 0.15)],
        fill=color,
    )


def _draw_shield(draw: ImageDraw.ImageDraw, x: float, y: float, r: float, color: tuple) -> None:
    draw.polygon(
        [(x - r * 0.5, y - r * 0.6), (x + r * 0.5, y - r * 0.6), (x + r * 0.5, y + r * 0.05),
         (x, y + r * 0.6), (x - r * 0.5, y + r * 0.05)],
        outline=color, width=max(2, round(r * 0.1)),
    )


def _draw_sparkle(draw: ImageDraw.ImageDraw, x: float, y: float, r: float, color: tuple) -> None:
    w = max(2, r * 0.1)
    d = r * 0.7
    draw.line([(x, y - d), (x, y + d)], fill=color, width=round(w))
    draw.line([(x - d, y), (x + d, y)], fill=color, width=round(w))
    diag = d * 0.7
    draw.line([(x - diag, y - diag), (x + diag, y + diag)], fill=color, width=max(1, round(w * 0.7)))
    draw.line([(x - diag, y + diag), (x + diag, y - diag)], fill=color, width=max(1, round(w * 0.7)))


def _draw_musical_note(draw: ImageDraw.ImageDraw, x: float, y: float, r: float, color: tuple) -> None:
    w = max(2, r * 0.1)
    draw.ellipse([x - r * 0.28, y + r * 0.12, x + r * 0.08, y + r * 0.45], fill=color)
    draw.line([(x + r * 0.08, y + r * 0.28), (x + r * 0.08, y - r * 0.55)], fill=color, width=round(w))
    draw.line([(x + r * 0.08, y - r * 0.55), (x + r * 0.35, y - r * 0.35)], fill=color, width=round(w))


def _draw_cross(draw: ImageDraw.ImageDraw, x: float, y: float, r: float, color: tuple) -> None:
    draw.rectangle([x - r * 0.09, y - r * 0.6, x + r * 0.09, y + r * 0.6], fill=color)
    draw.rectangle([x - r * 0.4, y - r * 0.1, x + r * 0.4, y + r * 0.1], fill=color)


def _draw_leaf(draw: ImageDraw.ImageDraw, x: float, y: float, r: float, color: tuple) -> None:
    draw.polygon(
        [(x, y - r * 0.6), (x + r * 0.35, y - r * 0.1), (x, y + r * 0.6), (x - r * 0.35, y - r * 0.1)],
        fill=color,
    )


def _draw_fist(draw: ImageDraw.ImageDraw, x: float, y: float, r: float, color: tuple) -> None:
    draw.rounded_rectangle(
        [x - r * 0.35, y - r * 0.35, x + r * 0.35, y + r * 0.35], radius=max(2, round(r * 0.15)), fill=color,
    )


def _draw_eye(draw: ImageDraw.ImageDraw, x: float, y: float, r: float, color: tuple) -> None:
    draw.ellipse([x - r * 0.55, y - r * 0.28, x + r * 0.55, y + r * 0.28], outline=color, width=max(2, round(r * 0.09)))
    draw.ellipse([x - r * 0.12, y - r * 0.12, x + r * 0.12, y + r * 0.12], fill=color)


def _draw_flame(draw: ImageDraw.ImageDraw, x: float, y: float, r: float, color: tuple) -> None:
    draw.polygon(
        [(x, y - r * 0.6), (x + r * 0.32, y - r * 0.05), (x + r * 0.2, y + r * 0.3),
         (x, y + r * 0.15), (x - r * 0.2, y + r * 0.3), (x - r * 0.32, y - r * 0.05)],
        fill=color,
    )
    draw.polygon([(x, y - r * 0.1), (x + r * 0.15, y + r * 0.3), (x, y + r * 0.55), (x - r * 0.15, y + r * 0.3)], fill=color)


def _draw_droplet(draw: ImageDraw.ImageDraw, x: float, y: float, r: float, color: tuple) -> None:
    draw.ellipse([x - r * 0.32, y - r * 0.05, x + r * 0.32, y + r * 0.55], fill=color)
    draw.polygon([(x, y - r * 0.55), (x - r * 0.3, y + r * 0.08), (x + r * 0.3, y + r * 0.08)], fill=color)


def _draw_snowflake(draw: ImageDraw.ImageDraw, x: float, y: float, r: float, color: tuple) -> None:
    import math
    w = max(1, round(r * 0.08))
    for angle_deg in (90, 30, 150):
        rad = math.radians(angle_deg)
        dx, dy = math.cos(rad) * r * 0.6, math.sin(rad) * r * 0.6
        draw.line([(x - dx, y - dy), (x + dx, y + dy)], fill=color, width=w)


def _draw_lightning_bolt(draw: ImageDraw.ImageDraw, x: float, y: float, r: float, color: tuple) -> None:
    draw.polygon(
        [(x + r * 0.15, y - r * 0.65), (x - r * 0.25, y + r * 0.05), (x, y + r * 0.05),
         (x - r * 0.15, y + r * 0.65), (x + r * 0.3, y - r * 0.15), (x + r * 0.05, y - r * 0.15)],
        fill=color,
    )


def _draw_skull(draw: ImageDraw.ImageDraw, x: float, y: float, r: float, color: tuple, background: tuple) -> None:
    draw.ellipse([x - r * 0.45, y - r * 0.5, x + r * 0.45, y + r * 0.25], fill=color)
    draw.rectangle([x - r * 0.28, y + r * 0.1, x + r * 0.28, y + r * 0.4], fill=color)
    eye_r = r * 0.13
    draw.ellipse([x - r * 0.28 - eye_r, y - r * 0.15 - eye_r, x - r * 0.28 + eye_r, y - r * 0.15 + eye_r], fill=background)
    draw.ellipse([x + r * 0.28 - eye_r, y - r * 0.15 - eye_r, x + r * 0.28 + eye_r, y - r * 0.15 + eye_r], fill=background)


def _draw_sun(draw: ImageDraw.ImageDraw, x: float, y: float, r: float, color: tuple) -> None:
    import math
    draw.ellipse([x - r * 0.22, y - r * 0.22, x + r * 0.22, y + r * 0.22], fill=color)
    w = max(1, round(r * 0.09))
    for i in range(8):
        rad = math.radians(i * 45)
        inner = r * 0.32
        outer = r * 0.62
        draw.line(
            [(x + math.cos(rad) * inner, y + math.sin(rad) * inner),
             (x + math.cos(rad) * outer, y + math.sin(rad) * outer)],
            fill=color, width=w,
        )


def _draw_diamond(draw: ImageDraw.ImageDraw, x: float, y: float, r: float, color: tuple) -> None:
    draw.polygon([(x, y - r * 0.55), (x + r * 0.55, y), (x, y + r * 0.55), (x - r * 0.55, y)], fill=color)


def _draw_star_burst(draw: ImageDraw.ImageDraw, x: float, y: float, r: float, color: tuple) -> None:
    # A 4-point spike star (innate/wild magic burst) -- switched
    # sorcerer off reusing _draw_flame after a rendered test PNG
    # showed it collapsing into something indistinguishable from
    # _draw_leaf/_draw_diamond at small token sizes; sharp alternating
    # long/short points read as a distinct "spark" shape instead.
    long_r, short_r = r * 0.62, r * 0.2
    points = []
    for i in range(8):
        import math
        rad = math.radians(i * 45)
        radius = long_r if i % 2 == 0 else short_r
        points.append((x + math.cos(rad) * radius, y + math.sin(rad) * radius))
    draw.polygon(points, fill=color)


_ICON_DRAW_FUNCS = {
    "fighter": _draw_sword,
    "physical": _draw_sword,
    "barbarian": _draw_axe,
    "paladin": _draw_shield,
    "ranger": _draw_arrow,
    "rogue": _draw_crossed_daggers,
    "wizard": _draw_sparkle,
    "sorcerer": _draw_star_burst,
    "bard": _draw_musical_note,
    "cleric": _draw_cross,
    "druid": _draw_leaf,
    "monk": _draw_fist,
    "warlock": _draw_eye,
    "psychic": _draw_eye,
    "fire": _draw_flame,
    "poison": _draw_droplet,
    "cold": _draw_snowflake,
    "lightning": _draw_lightning_bolt,
    "radiant": _draw_sun,
    "force": _draw_diamond,
}


def _draw_icon(draw: ImageDraw.ImageDraw, x: float, y: float, radius: float, icon_key: str,
               background: tuple, icon_color: tuple = (255, 255, 255)) -> bool:
    """
    Draws the icon for icon_key centered at (x, y), sized off the
    token's own real radius. Returns whether a real icon was drawn --
    _draw_token falls back to the initial letter when this is False,
    same "never a blank token" guarantee as _icon_key_for_combatant's
    own fallback design.
    """
    if icon_key == "necrotic":
        _draw_skull(draw, x, y, radius, icon_color, background)
        return True
    func = _ICON_DRAW_FUNCS.get(icon_key)
    if func is None:
        return False
    func(draw, x, y, radius, icon_color)
    return True


def _draw_background(draw: ImageDraw.ImageDraw, canvas_height: int = CANVAS_HEIGHT) -> None:
    """A simple dark, warm vertical gradient -- an arena feel with zero external assets."""
    top = (54, 30, 20)
    bottom = (18, 10, 8)
    for y in range(canvas_height):
        t = y / max(canvas_height - 1, 1)
        r = int(top[0] + (bottom[0] - top[0]) * t)
        g = int(top[1] + (bottom[1] - top[1]) * t)
        b = int(top[2] + (bottom[2] - top[2]) * t)
        draw.line([(0, y), (CANVAS_WIDTH, y)], fill=(r, g, b))


def _draw_center_divider(draw: ImageDraw.ImageDraw, canvas_height: int) -> None:
    """
    Faint center divider so the two facing sides read clearly, same
    "two facing columns" shape as the reference JRPG battle screen.
    Pulled out of _draw_background (2026-08-10, Task #14) so a real
    location-photo background still gets this same divider -- it's
    about formation readability, not tied to the gradient specifically.
    """
    draw.line(
        [(CANVAS_WIDTH // 2, 60), (CANVAS_WIDTH // 2, canvas_height - 30)],
        fill=(255, 255, 255, 40), width=1,
    )


def _composite_location_background(image: Image.Image, canvas_height: int, background_bytes: bytes) -> bool:
    """
    Real location art (2026-08-10, Task #14, per Coffee: "instead of a
    plain background can we use location background?"), reusing the
    EXACT same real, already-generated Pollinations image a player
    already saw when they arrived here (bot.py's caller builds the
    identical prompt+seed _maybe_send_location_image already uses for
    that same location) -- never a new, separate image call, and since
    that image is almost always already cached server-side by the time
    a fight starts (real players get an automatic "look around" on
    arrival), this rarely pays the slow first-fetch cost. Cover-fit
    (crop to aspect, never stretched/distorted) then darkened so token
    labels/HP bars/name text stay legible on top of potentially busy
    art -- same "never let the flavor layer break the real
    information" convention as every other _maybe_send_*_image helper
    in this codebase. Returns whether it actually composited -- the
    caller falls back to the plain gradient on any failure (corrupt
    bytes, wrong format, no bytes at all), never a broken image.
    """
    try:
        photo = Image.open(io.BytesIO(background_bytes)).convert("RGB")
    except Exception:
        return False
    photo_ratio = photo.width / photo.height
    canvas_ratio = CANVAS_WIDTH / canvas_height
    if photo_ratio > canvas_ratio:
        new_height = canvas_height
        new_width = max(1, round(canvas_height * photo_ratio))
    else:
        new_width = CANVAS_WIDTH
        new_height = max(1, round(CANVAS_WIDTH / photo_ratio))
    photo = photo.resize((new_width, new_height))
    left = (new_width - CANVAS_WIDTH) // 2
    top = (new_height - canvas_height) // 2
    photo = photo.crop((left, top, left + CANVAS_WIDTH, top + canvas_height))
    darken = Image.new("RGB", photo.size, (0, 0, 0))
    photo = Image.blend(photo, darken, 0.55)
    image.paste(photo, (0, 0))
    return True


def _condition_badge_text(conditions: list | None) -> str | None:
    """Pure, testable-without-Pillow piece of the condition-badge feature -- see _CONDITION_LABELS."""
    active = [c for c in (conditions or []) if c in _CONDITION_LABELS]
    if not active:
        return None
    return " • ".join(_CONDITION_LABELS[c][0] for c in active)


def _draw_token(draw: ImageDraw.ImageDraw, x: int, y: int, combatant: dict, color: tuple,
                 radius: int) -> None:
    name = combatant.get("name", "?")
    hp_current = max(combatant.get("hp_current", 0), 0)
    hp_max = max(combatant.get("hp_max", hp_current or 1), 1)

    # Every size below scales off the real drawn radius (which may have
    # been shrunk to fit a crowded row -- see _row_token_radius), keeping
    # the same visual proportions as the original fixed-40px design.
    scale = radius / _TOKEN_RADIUS
    name_font = _load_font(max(round(16 * scale), 10), bold=True)
    hp_font = _load_font(max(round(13 * scale), 9))
    hp_bar_width = _HP_BAR_WIDTH * scale
    hp_bar_height = max(_HP_BAR_HEIGHT * scale, 4)

    if combatant.get("is_boss"):
        draw.ellipse(
            [x - radius - 6, y - radius - 6, x + radius + 6, y + radius + 6],
            outline=_BOSS_RING_COLOR, width=4,
        )
    draw.ellipse(
        [x - radius, y - radius, x + radius, y + radius],
        fill=color, outline=_TOKEN_OUTLINE, width=2,
    )
    icon_key = _icon_key_for_combatant(combatant)
    if not (icon_key and _draw_icon(draw, x, y, radius, icon_key, background=color)):
        initial = (_meaningful_first_word(name)[0] if name else "?").upper()
        initial_font = _load_font(radius, bold=True)
        bbox = draw.textbbox((0, 0), initial, font=initial_font)
        draw.text(
            (x - (bbox[2] - bbox[0]) / 2, y - (bbox[3] - bbox[1]) / 2 - bbox[1]),
            initial, font=initial_font, fill=(255, 255, 255),
        )

    label = _first_name(name)
    label_bbox = draw.textbbox((0, 0), label, font=name_font)
    label_w = label_bbox[2] - label_bbox[0]
    label_y = y + radius + 8 * scale
    draw.text((x - label_w / 2, label_y), label, font=name_font, fill=_NAME_COLOR)

    bar_x = x - hp_bar_width / 2
    bar_y = label_y + 20 * scale
    draw.rectangle([bar_x, bar_y, bar_x + hp_bar_width, bar_y + hp_bar_height], fill=_HP_BAR_BG)
    fill_w = hp_bar_width * min(hp_current / hp_max, 1.0)
    if fill_w > 0:
        draw.rectangle([bar_x, bar_y, bar_x + fill_w, bar_y + hp_bar_height], fill=_hp_bar_color(hp_current, hp_max))
    hp_label = f"{hp_current}/{hp_max}"
    hp_bbox = draw.textbbox((0, 0), hp_label, font=hp_font)
    hp_w = hp_bbox[2] - hp_bbox[0]
    hp_text_y = bar_y + hp_bar_height + 2 * scale
    draw.text((x - hp_w / 2, hp_text_y), hp_label, font=hp_font, fill=(200, 200, 200))

    badge_text = _condition_badge_text(combatant.get("conditions"))
    if badge_text:
        active = [c for c in combatant.get("conditions", []) if c in _CONDITION_LABELS]
        badge_color = _CONDITION_LABELS[active[0]][1]
        badge_font = _load_font(max(round(10 * scale), 8), bold=True)
        badge_bbox = draw.textbbox((0, 0), badge_text, font=badge_font)
        badge_w = badge_bbox[2] - badge_bbox[0]
        badge_h = badge_bbox[3] - badge_bbox[1]
        badge_y = hp_text_y + (hp_bbox[3] - hp_bbox[1]) + 4 * scale
        draw.text((x - badge_w / 2, badge_y), badge_text, font=badge_font, fill=badge_color)


def _row_x_positions(is_party: bool) -> dict:
    """
    Front row is always the row closer to the center divider, back row
    further from it -- true for both sides, mirrored left/right, same
    "front row faces the enemy" shape the real formation_row mechanic
    (bot.py's _pick_formation_weighted_target) already models.
    """
    if is_party:
        return {"back": 110, "front": 280}
    return {"back": CANVAS_WIDTH - 110, "front": CANVAS_WIDTH - 280}


def _row_token_radius(member_count: int, usable_height: int) -> int:
    """
    Shrinks the token (and everything scaled off it) only as much as a
    crowded row actually needs so its members' name/HP-bar blocks never
    overlap the next token down -- see the _FOOTPRINT_RATIO comment
    above for how that 132px-per-token-at-full-size number was measured.
    Never grows past the original fixed 40px design for a sparse row.

    Called ONCE per image now (2026-08-10, Coffee: "make all the
    players circle in formations the same size"), with member_count
    being the single most-crowded row across the WHOLE formation, not
    each row's own count -- see render_battle_formation. Keeping this
    function itself row-count-generic (rather than folding the "which
    row is worst" logic in here) is what let that caller reuse it
    as-is for both the earlier canvas-sizing need and this one.
    """
    if member_count <= 0:
        return _TOKEN_RADIUS
    spacing = usable_height / member_count
    fitted = spacing / _FOOTPRINT_RATIO
    return int(max(_TOKEN_RADIUS_MIN, min(_TOKEN_RADIUS, fitted)))


def _canvas_height(max_row_count: int) -> int:
    """
    Even at the smallest legible token size, a very crowded row (5+)
    still needs more raw vertical space than the base 500px canvas
    offers -- grows the canvas rather than let tokens shrink past
    _TOKEN_RADIUS_MIN into illegibility (same "grow the canvas, don't
    degrade past readable" precedent as map_render.py's _canvas_size).
    """
    top_margin, bottom_margin = 100, 60
    min_needed = top_margin + bottom_margin + max_row_count * (_TOKEN_RADIUS_MIN * _FOOTPRINT_RATIO)
    return int(max(CANVAS_HEIGHT, min_needed))


def _draw_side(draw: ImageDraw.ImageDraw, combatants: list[dict], is_party: bool, canvas_height: int, radius: int) -> None:
    color = _PARTY_COLOR if is_party else _ENEMY_COLOR
    x_positions = _row_x_positions(is_party)
    by_row = {"front": [], "back": []}
    for c in combatants:
        row = "back" if c.get("formation_row") == "back" else "front"
        by_row[row].append(c)

    top_margin, bottom_margin = 100, 60
    usable_height = canvas_height - top_margin - bottom_margin
    for row, members in by_row.items():
        if not members:
            continue
        x = x_positions[row]
        spacing = usable_height / len(members)
        for i, combatant in enumerate(members):
            y = int(top_margin + spacing * (i + 0.5))
            _draw_token(draw, x, y, combatant, color, radius)


def render_battle_formation(party: list[dict], enemies: list[dict], background_image_bytes: bytes | None = None) -> bytes:
    """
    Real, current combat state in, PNG bytes out -- party (left,
    facing right) vs enemies (right, facing left), each side split by
    its own real formation_row into a front row (nearer the center
    divider) and back row (further from it). Grounded entirely in
    whatever `party`/`enemies` are handed in (bot.py's caller passes
    the real live session.living_on_side("party"/"enemy") lists) --
    never invents a combatant, a row, or an HP value.

    background_image_bytes (2026-08-10, Task #14): optional real
    location art (bot.py's caller fetches it, this file stays
    network-free itself -- see _composite_location_background). None
    (no location image available/fetch failed) falls back to the
    original plain gradient, same as before this feature existed.
    """
    def _row_count(combatants: list[dict], row: str) -> int:
        return sum(1 for c in combatants if ("back" if c.get("formation_row") == "back" else "front") == row)

    max_row_count = max(
        _row_count(party, "front"), _row_count(party, "back"),
        _row_count(enemies, "front"), _row_count(enemies, "back"),
        1,
    )
    canvas_height = _canvas_height(max_row_count)

    image = Image.new("RGB", (CANVAS_WIDTH, canvas_height), (0, 0, 0))
    composited = bool(background_image_bytes) and _composite_location_background(image, canvas_height, background_image_bytes)
    draw = ImageDraw.Draw(image)
    if not composited:
        _draw_background(draw, canvas_height)
    _draw_center_divider(draw, canvas_height)

    title_font = _load_font(28, bold=True)
    title = "BATTLE FORMATION"
    title_bbox = draw.textbbox((0, 0), title, font=title_font)
    draw.text(((CANVAS_WIDTH - (title_bbox[2] - title_bbox[0])) / 2, 16), title, font=title_font, fill=(255, 255, 255))

    # Real live request (2026-08-10, Coffee: "make all the players
    # circle in formations the same size"): _row_token_radius used to
    # be called PER ROW with that row's own member count, so a crowded
    # row shrank while a sparser row on the same side (or the other
    # side entirely) stayed full-size -- every token in the image now
    # shares ONE radius, sized off the single most-crowded row across
    # the whole formation (party AND enemies), so a fight is never
    # visually lopsided between differently-sized circles.
    top_margin, bottom_margin = 100, 60
    radius = _row_token_radius(max_row_count, canvas_height - top_margin - bottom_margin)

    _draw_side(draw, party, is_party=True, canvas_height=canvas_height, radius=radius)
    _draw_side(draw, enemies, is_party=False, canvas_height=canvas_height, radius=radius)

    buffer = io.BytesIO()
    image.save(buffer, format="PNG")
    return buffer.getvalue()
