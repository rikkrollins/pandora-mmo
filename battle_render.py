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
_TOKEN_OUTLINE = (20, 20, 20)
_NAME_COLOR = (240, 240, 240)
_HP_BAR_WIDTH = 84
_HP_BAR_HEIGHT = 9
_HP_BAR_BG = (40, 40, 40)

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


def _hp_bar_color(hp_current: int, hp_max: int) -> tuple:
    if hp_max <= 0:
        return (100, 100, 100)
    pct = hp_current / hp_max
    if pct > 0.6:
        return (76, 175, 80)
    if pct > 0.3:
        return (255, 193, 7)
    return (211, 47, 47)


def _draw_background(draw: ImageDraw.ImageDraw) -> None:
    """A simple dark, warm vertical gradient -- an arena feel with zero external assets."""
    top = (54, 30, 20)
    bottom = (18, 10, 8)
    for y in range(CANVAS_HEIGHT):
        t = y / max(CANVAS_HEIGHT - 1, 1)
        r = int(top[0] + (bottom[0] - top[0]) * t)
        g = int(top[1] + (bottom[1] - top[1]) * t)
        b = int(top[2] + (bottom[2] - top[2]) * t)
        draw.line([(0, y), (CANVAS_WIDTH, y)], fill=(r, g, b))
    # Faint center divider so the two facing sides read clearly, same
    # "two facing columns" shape as the reference JRPG battle screen.
    draw.line(
        [(CANVAS_WIDTH // 2, 60), (CANVAS_WIDTH // 2, CANVAS_HEIGHT - 30)],
        fill=(255, 255, 255, 40), width=1,
    )


def _draw_token(draw: ImageDraw.ImageDraw, x: int, y: int, combatant: dict, color: tuple,
                 name_font: ImageFont.FreeTypeFont, hp_font: ImageFont.FreeTypeFont) -> None:
    name = combatant.get("name", "?")
    hp_current = max(combatant.get("hp_current", 0), 0)
    hp_max = max(combatant.get("hp_max", hp_current or 1), 1)

    if combatant.get("is_boss"):
        draw.ellipse(
            [x - _TOKEN_RADIUS - 6, y - _TOKEN_RADIUS - 6, x + _TOKEN_RADIUS + 6, y + _TOKEN_RADIUS + 6],
            outline=_BOSS_RING_COLOR, width=4,
        )
    draw.ellipse(
        [x - _TOKEN_RADIUS, y - _TOKEN_RADIUS, x + _TOKEN_RADIUS, y + _TOKEN_RADIUS],
        fill=color, outline=_TOKEN_OUTLINE, width=2,
    )
    initial = (name[0] if name else "?").upper()
    initial_font = _load_font(_TOKEN_RADIUS, bold=True)
    bbox = draw.textbbox((0, 0), initial, font=initial_font)
    draw.text(
        (x - (bbox[2] - bbox[0]) / 2, y - (bbox[3] - bbox[1]) / 2 - bbox[1]),
        initial, font=initial_font, fill=(255, 255, 255),
    )

    label = _truncate_name(name)
    label_bbox = draw.textbbox((0, 0), label, font=name_font)
    label_w = label_bbox[2] - label_bbox[0]
    label_y = y + _TOKEN_RADIUS + 8
    draw.text((x - label_w / 2, label_y), label, font=name_font, fill=_NAME_COLOR)

    bar_x = x - _HP_BAR_WIDTH / 2
    bar_y = label_y + 20
    draw.rectangle([bar_x, bar_y, bar_x + _HP_BAR_WIDTH, bar_y + _HP_BAR_HEIGHT], fill=_HP_BAR_BG)
    fill_w = _HP_BAR_WIDTH * min(hp_current / hp_max, 1.0)
    if fill_w > 0:
        draw.rectangle([bar_x, bar_y, bar_x + fill_w, bar_y + _HP_BAR_HEIGHT], fill=_hp_bar_color(hp_current, hp_max))
    hp_label = f"{hp_current}/{hp_max}"
    hp_bbox = draw.textbbox((0, 0), hp_label, font=hp_font)
    hp_w = hp_bbox[2] - hp_bbox[0]
    draw.text((x - hp_w / 2, bar_y + _HP_BAR_HEIGHT + 2), hp_label, font=hp_font, fill=(200, 200, 200))


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


def _draw_side(draw: ImageDraw.ImageDraw, combatants: list[dict], is_party: bool,
               name_font: ImageFont.FreeTypeFont, hp_font: ImageFont.FreeTypeFont) -> None:
    color = _PARTY_COLOR if is_party else _ENEMY_COLOR
    x_positions = _row_x_positions(is_party)
    by_row = {"front": [], "back": []}
    for c in combatants:
        row = "back" if c.get("formation_row") == "back" else "front"
        by_row[row].append(c)

    top_margin, bottom_margin = 100, 60
    usable_height = CANVAS_HEIGHT - top_margin - bottom_margin
    for row, members in by_row.items():
        if not members:
            continue
        x = x_positions[row]
        spacing = usable_height / len(members)
        for i, combatant in enumerate(members):
            y = int(top_margin + spacing * (i + 0.5))
            _draw_token(draw, x, y, combatant, color, name_font, hp_font)


def render_battle_formation(party: list[dict], enemies: list[dict]) -> bytes:
    """
    Real, current combat state in, PNG bytes out -- party (left,
    facing right) vs enemies (right, facing left), each side split by
    its own real formation_row into a front row (nearer the center
    divider) and back row (further from it). Grounded entirely in
    whatever `party`/`enemies` are handed in (bot.py's caller passes
    the real live session.living_on_side("party"/"enemy") lists) --
    never invents a combatant, a row, or an HP value.
    """
    image = Image.new("RGB", (CANVAS_WIDTH, CANVAS_HEIGHT), (0, 0, 0))
    draw = ImageDraw.Draw(image)
    _draw_background(draw)

    title_font = _load_font(28, bold=True)
    title = "BATTLE FORMATION"
    title_bbox = draw.textbbox((0, 0), title, font=title_font)
    draw.text(((CANVAS_WIDTH - (title_bbox[2] - title_bbox[0])) / 2, 16), title, font=title_font, fill=(255, 255, 255))

    name_font = _load_font(16, bold=True)
    hp_font = _load_font(13)
    _draw_side(draw, party, is_party=True, name_font=name_font, hp_font=hp_font)
    _draw_side(draw, enemies, is_party=False, name_font=name_font, hp_font=hp_font)

    buffer = io.BytesIO()
    image.save(buffer, format="PNG")
    return buffer.getvalue()
