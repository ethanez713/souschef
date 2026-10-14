"""Draw the app icons. The PNGs in static/ are this script's output."""

from __future__ import annotations

import re
from io import BytesIO
from pathlib import Path

from PIL import Image, ImageDraw

ROOT = Path(__file__).resolve().parent.parent
STATIC = ROOT / "src" / "souschef" / "static"
DS_CSS = STATIC / "ds.css"

SUPERSAMPLE = 4
# Maskable icons are cropped to a circle of this diameter; the pot stays inside it.
SAFE_ZONE = 0.80

# Fractions of the canvas. The body is the plan's 60% × 40%. The lid is a thin
# stadium seated on the body, the knob a smaller stadium on the lid, and the
# handles short stadiums centred on the body's upper third. The body's top is
# square so that seat is a line, not a gap at the rounded shoulders; the bottom
# corners stay rounded.
BODY_W = 0.60
BODY_H = 0.40
BODY_RADIUS = 0.09
LID_W = 0.68
LID_H = 0.070
LID_SEAT = 0.008
KNOB_W = 0.13
KNOB_H = 0.064
KNOB_SEAT = 0.012
HANDLE_W = 0.100
HANDLE_TUCK = 0.018
HANDLE_H = 0.056

LIGHT_SELECTOR = ':root,[data-theme="light"]'
DARK_SELECTOR = ':root[data-theme="dark"]'


def _blocks(css: str) -> list[tuple[str, str]]:
    css = re.sub(r"/\*.*?\*/", "", css, flags=re.S)
    rules: list[tuple[str, str]] = []
    depth, start, selector = 0, 0, ""
    for index, char in enumerate(css):
        if char == "{":
            if depth == 0:
                selector, start = css[start:index].strip(), index + 1
            depth += 1
        elif char == "}":
            depth -= 1
            if depth == 0:
                if not selector.startswith("@"):
                    rules.append((selector, css[start:index]))
                start = index + 1
    return rules


def _tokens(css: str) -> dict[str, str]:
    """Light theme, then dark, so a var() chain resolves the way the dark page does."""
    tokens: dict[str, str] = {}
    for selector, body in _blocks(css):
        if selector.startswith((LIGHT_SELECTOR, DARK_SELECTOR)):
            found = re.findall(r"(--[\w-]+)\s*:\s*([^;]+)", body)
            tokens.update({name: value.strip() for name, value in found})
    return tokens


def _resolve(tokens: dict[str, str], name: str) -> str:
    value = tokens[name]
    for _ in range(10):
        match = re.fullmatch(r"var\((--[\w-]+)\)", value)
        if not match:
            return value
        value = tokens[match.group(1)]
    raise RuntimeError(f"token cycle at {name}")


def _rgb(value: str) -> tuple[int, int, int]:
    match = re.fullmatch(r"#([0-9A-Fa-f]{6})", value)
    if not match:
        raise RuntimeError(f"expected a hex colour, got {value}")
    digits = match.group(1)
    return int(digits[0:2], 16), int(digits[2:4], 16), int(digits[4:6], 16)


def palette() -> tuple[tuple[int, int, int], tuple[int, int, int]]:
    """Dark-theme --accent on --bg-app."""
    tokens = _tokens(DS_CSS.read_text())
    return _rgb(_resolve(tokens, "--accent")), _rgb(_resolve(tokens, "--bg-app"))


def _box(canvas: int, x: float, y: float, w: float, h: float) -> list[int]:
    # Inclusive edges. A box centred on the canvas then sums to canvas-1, which
    # is the mirror Pillow actually fills.
    return [
        round(x * canvas),
        round(y * canvas),
        round((x + w) * canvas) - 1,
        round((y + h) * canvas) - 1,
    ]


def _stadium(draw: ImageDraw.ImageDraw, box: list[int], fill: tuple[int, int, int]) -> None:
    short = min(box[2] - box[0], box[3] - box[1])
    draw.rounded_rectangle(box, radius=short / 2, fill=fill)


def _draw_pot(draw: ImageDraw.ImageDraw, canvas: int, accent: tuple[int, int, int]) -> None:
    knob_visible = KNOB_H - KNOB_SEAT
    lid_visible = LID_H - LID_SEAT
    top = (1 - (knob_visible + lid_visible + BODY_H)) / 2
    body_top = top + knob_visible + lid_visible
    body_x = (1 - BODY_W) / 2

    handle_cy = body_top + BODY_H / 6
    left = _box(
        canvas,
        body_x - (HANDLE_W - HANDLE_TUCK),
        handle_cy - HANDLE_H / 2,
        HANDLE_W,
        HANDLE_H,
    )
    _stadium(draw, left, accent)
    _stadium(draw, [canvas - 1 - left[2], left[1], canvas - 1 - left[0], left[3]], accent)

    draw.rounded_rectangle(
        _box(canvas, body_x, body_top, BODY_W, BODY_H),
        radius=round(BODY_RADIUS * canvas),
        fill=accent,
        corners=(False, False, True, True),
    )
    _stadium(draw, _box(canvas, (1 - LID_W) / 2, body_top - lid_visible, LID_W, LID_H), accent)
    _stadium(draw, _box(canvas, (1 - KNOB_W) / 2, top, KNOB_W, KNOB_H), accent)


def render(
    size: int, accent: tuple[int, int, int], background: tuple[int, int, int]
) -> Image.Image:
    canvas = size * SUPERSAMPLE
    image = Image.new("RGB", (canvas, canvas), background)
    _draw_pot(ImageDraw.Draw(image), canvas, accent)
    return image.resize((size, size), Image.Resampling.LANCZOS)


def _outside_safe_zone(
    image: Image.Image, background: tuple[int, int, int]
) -> list[tuple[int, int]]:
    size = image.width
    radius = SAFE_ZONE / 2 * size
    center = size / 2
    outside = []
    pixels = image.load()
    assert pixels is not None
    for y in range(size):
        dy = y + 0.5 - center
        for x in range(size):
            pixel = pixels[x, y]
            if max(abs(pixel[i] - background[i]) for i in range(3)) < 8:
                continue
            dx = x + 0.5 - center
            if dx * dx + dy * dy > radius * radius:
                outside.append((x, y))
    return outside


def _png(image: Image.Image) -> bytes:
    buffer = BytesIO()
    image.save(buffer, format="PNG")
    return buffer.getvalue()


def main() -> None:
    accent, background = palette()
    icons = {size: render(size, accent, background) for size in (180, 192, 512)}
    outside = _outside_safe_zone(icons[512], background)
    if outside:
        raise RuntimeError(f"pot leaves the maskable safe zone at {outside[:4]}")
    files = {
        "icon-192.png": icons[192],
        "icon-512.png": icons[512],
        "icon-maskable-512.png": icons[512],
        "apple-touch-icon.png": icons[180],
    }
    for name, image in files.items():
        (STATIC / name).write_bytes(_png(image))


if __name__ == "__main__":
    main()
