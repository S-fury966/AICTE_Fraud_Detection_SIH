"""Synthetic lab and classroom "photographs", drawn from a seed.

Not photographs of anywhere. Each is a procedurally drawn room - wall, floor,
windows, rows of desks with monitors or benches, a board - with sensor-like
noise and a slight blur, so the forensic checks see something photo-shaped
rather than flat vector art. Two different seeds give rooms far apart in
perceptual-hash space; the same seed gives the same bytes.

The manipulations are applied here, by the generator, so the labels come from
the recipe and never from a detector (the step 7 independence rule).
"""

from __future__ import annotations

import io
import random
from datetime import datetime
from typing import Any

import numpy as np
from PIL import Image, ImageDraw, ImageFilter, ImageOps
from PIL.PngImagePlugin import PngInfo
from PIL.TiffImagePlugin import IFDRational

WIDTH, HEIGHT = 1280, 853


def render_room(seed: int, kind: str = "lab") -> Image.Image:
    rng = random.Random(seed)
    image = Image.new("RGB", (WIDTH, HEIGHT))
    draw = ImageDraw.Draw(image)

    wall = tuple(rng.randint(150, 225) for _ in range(3))
    floor = tuple(rng.randint(70, 150) for _ in range(3))
    horizon = rng.randint(330, 430)

    # Wall with a vertical light gradient, floor with a darker one.
    for y in range(horizon):
        shade = 0.82 + 0.18 * (y / horizon)
        draw.line([(0, y), (WIDTH, y)], fill=tuple(int(c * shade) for c in wall))
    for y in range(horizon, HEIGHT):
        shade = 0.65 + 0.35 * ((y - horizon) / (HEIGHT - horizon))
        draw.line([(0, y), (WIDTH, y)], fill=tuple(int(c * shade) for c in floor))

    # Floor tiles in perspective.
    vanish_x = WIDTH // 2 + rng.randint(-160, 160)
    for i in range(-14, 15):
        draw.line([(vanish_x, horizon), (vanish_x + i * 140, HEIGHT)], fill=tuple(max(0, c - 18) for c in floor), width=2)

    # Windows.
    for i in range(rng.randint(2, 4)):
        x = 60 + i * rng.randint(260, 320)
        top = rng.randint(50, 90)
        w, h = rng.randint(150, 210), rng.randint(150, 200)
        if x + w > WIDTH - 40:
            break
        draw.rectangle([x, top, x + w, top + h], fill=(196, 222, 240), outline=(90, 90, 90), width=6)
        draw.line([(x + w // 2, top), (x + w // 2, top + h)], fill=(90, 90, 90), width=4)

    # Board.
    board_w = rng.randint(380, 520)
    bx = rng.randint(80, WIDTH - board_w - 80)
    board_colour = (238, 240, 238) if kind == "lab" else (34, 66, 48)
    draw.rectangle([bx, 70, bx + board_w, 70 + 170], fill=board_colour, outline=(60, 60, 60), width=8)
    for _ in range(rng.randint(3, 6)):
        lx = bx + rng.randint(20, board_w - 160)
        ly = 90 + rng.randint(0, 120)
        ink = (40, 40, 160) if kind == "lab" else (220, 220, 210)
        draw.line([(lx, ly), (lx + rng.randint(60, 150), ly)], fill=ink, width=3)

    # Rows of desks: perspective via scaling with depth.
    rows = rng.randint(3, 4)
    per_row = rng.randint(4, 6)
    desk_colour = tuple(rng.randint(90, 170) for _ in range(3))
    for row in range(rows):
        depth = (row + 1) / (rows + 0.5)
        y = int(horizon + 40 + depth * (HEIGHT - horizon - 110))
        scale = 0.45 + 0.75 * depth
        desk_w, desk_h = int(170 * scale), int(34 * scale)
        gap = int(40 * scale)
        total = per_row * desk_w + (per_row - 1) * gap
        start = (WIDTH - total) // 2 + rng.randint(-30, 30)
        for col in range(per_row):
            x = start + col * (desk_w + gap)
            draw.rectangle([x, y, x + desk_w, y + desk_h], fill=desk_colour, outline=(40, 40, 40))
            draw.rectangle([x + 6, y + desk_h, x + 12, y + desk_h + int(60 * scale)], fill=(50, 50, 50))
            draw.rectangle([x + desk_w - 12, y + desk_h, x + desk_w - 6, y + desk_h + int(60 * scale)], fill=(50, 50, 50))
            if kind == "lab":
                mw, mh = int(84 * scale), int(58 * scale)
                mx = x + (desk_w - mw) // 2
                my = y - mh - int(10 * scale)
                draw.rectangle([mx, my, mx + mw, my + mh], fill=(25, 25, 30), outline=(10, 10, 10), width=2)
                glow = (rng.randint(40, 90), rng.randint(110, 170), rng.randint(170, 230))
                draw.rectangle([mx + 4, my + 4, mx + mw - 4, my + mh - 4], fill=glow)
                draw.rectangle([mx + mw // 2 - 4, my + mh, mx + mw // 2 + 4, y], fill=(30, 30, 30))
            else:
                draw.rectangle([x + 10, y - int(18 * scale), x + desk_w - 10, y], fill=tuple(max(0, c - 30) for c in desk_colour))

    # Sensor noise and lens softness, so the image compresses like a photo.
    array = np.asarray(image, dtype=np.float32)
    noise = np.random.default_rng(seed).normal(0, 6.0, array.shape)
    array = np.clip(array + noise, 0, 255).astype(np.uint8)
    return Image.fromarray(array).filter(ImageFilter.GaussianBlur(radius=0.8))


def clone_region(image: Image.Image, seed: int) -> Image.Image:
    """Copy-move: paste a block of the room elsewhere in the same image."""
    rng = random.Random(seed)
    out = image.copy()
    w, h = 220, 160
    sx, sy = rng.randint(200, 480), rng.randint(470, 560)
    tx, ty = sx + rng.randint(380, 520), sy + rng.randint(-40, 40)
    patch = out.crop((sx, sy, sx + w, sy + h))
    out.paste(patch, (min(tx, WIDTH - w - 1), max(0, min(ty, HEIGHT - h - 1))))
    return out


def paste_foreign_patch(image: Image.Image, seed: int) -> Image.Image:
    """A pristine patch pasted into a photo with a lossy history (ELA bait).

    The base is recompressed twice at a low quality first, so its error level
    settles low; the pasted patch has never been compressed and is sharp, so
    it re-compresses with visibly more error than its surroundings.
    """
    rng = random.Random(seed)
    base = image
    for quality in (70, 70):
        buffer = io.BytesIO()
        base.save(buffer, format="JPEG", quality=quality)
        buffer.seek(0)
        base = Image.open(buffer).convert("RGB")
    patch = Image.new("RGB", (240, 170), (30, 30, 34))
    pdraw = ImageDraw.Draw(patch)
    for i in range(0, 240, 12):
        pdraw.line([(i, 0), (i, 170)], fill=(230, 230, 235), width=2)
    for j in range(0, 170, 14):
        pdraw.line([(0, j), (240, j)], fill=(200, 60, 60), width=1)
    out = base.copy()
    out.paste(patch, (rng.randint(600, 900), rng.randint(440, 560)))
    return out


def _dms(value: float) -> tuple[IFDRational, IFDRational, IFDRational]:
    value = abs(value)
    degrees = int(value)
    minutes_float = (value - degrees) * 60
    minutes = int(minutes_float)
    seconds = round((minutes_float - minutes) * 60, 2)
    return IFDRational(degrees), IFDRational(minutes), IFDRational(int(seconds * 100), 100)


def make_exif(
    captured: datetime | None,
    gps: tuple[float, float] | None,
    software: str | None = None,
    make: str = "DemoCam",
    model: str = "DC-200 (synthetic)",
) -> Image.Exif:
    exif = Image.Exif()
    exif[0x010F] = make
    exif[0x0110] = model
    if software:
        exif[0x0131] = software
    if captured:
        stamp = captured.strftime("%Y:%m:%d %H:%M:%S")
        exif[0x0132] = stamp
        exif.get_ifd(0x8769)[0x9003] = stamp
    if gps:
        lat, lon = gps
        ifd = exif.get_ifd(0x8825)
        ifd[1] = "N" if lat >= 0 else "S"
        ifd[2] = _dms(lat)
        ifd[3] = "E" if lon >= 0 else "W"
        ifd[4] = _dms(lon)
    return exif


def save_jpeg(image: Image.Image, exif: Image.Exif | None, quality: int = 92) -> bytes:
    buffer = io.BytesIO()
    kwargs: dict[str, Any] = {"format": "JPEG", "quality": quality}
    if exif is not None:
        kwargs["exif"] = exif
    image.save(buffer, **kwargs)
    return buffer.getvalue()


def save_generated_png(image: Image.Image, generator_note: str) -> bytes:
    """A PNG carrying the kind of text chunk image generators write."""
    info = PngInfo()
    info.add_text("parameters", generator_note)
    info.add_text("Software", "Stable Diffusion (demo marker)")
    buffer = io.BytesIO()
    image.save(buffer, format="PNG", pnginfo=info)
    return buffer.getvalue()


def disguise_reuse(image: Image.Image) -> Image.Image:
    """What a careless reuse looks like: mirrored, shrunk, recompressed."""
    mirrored = ImageOps.mirror(image)
    small = mirrored.resize((int(WIDTH * 0.8), int(HEIGHT * 0.8)), Image.LANCZOS)
    buffer = io.BytesIO()
    small.save(buffer, format="JPEG", quality=78)
    buffer.seek(0)
    return Image.open(buffer).convert("RGB")
