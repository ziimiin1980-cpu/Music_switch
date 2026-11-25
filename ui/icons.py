"""Tray icon images: generated fallback icon and cover art with a state badge."""

from __future__ import annotations

import io
import logging
from typing import Optional

from PIL import Image, ImageDraw

log = logging.getLogger(__name__)

SIZE = 64
_ACCENT = (124, 196, 255, 255)
_BACKGROUND = (24, 26, 30, 255)
_WHITE = (255, 255, 255, 255)


def base_icon(size: int = SIZE, playing: bool = False) -> Image.Image:
    """Rounded dark square with a play triangle or pause bars."""
    image = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    draw = ImageDraw.Draw(image)
    draw.rounded_rectangle(
        (0, 0, size - 1, size - 1), radius=max(4, int(size * 0.2)), fill=_BACKGROUND
    )
    cx, cy = size / 2.0, size / 2.0
    if playing:
        s = size * 0.26
        draw.polygon(
            [(cx - s * 0.75, cy - s), (cx - s * 0.75, cy + s), (cx + s, cy)],
            fill=_ACCENT,
        )
    else:
        width = max(3, int(size * 0.11))
        height = size * 0.56
        gap = size * 0.10
        top = cy - height / 2.0
        left_x = cx - gap / 2.0 - width
        right_x = cx + gap / 2.0
        radius = max(1, width // 3)
        draw.rounded_rectangle(
            (left_x, top, left_x + width, top + height), radius=radius, fill=_ACCENT
        )
        draw.rounded_rectangle(
            (right_x, top, right_x + width, top + height), radius=radius, fill=_ACCENT
        )
    return image


def decode_cover(data: bytes, size: int = SIZE) -> Optional[Image.Image]:
    """Center-crop the artwork to a square and resize it."""
    try:
        image = Image.open(io.BytesIO(data))
        image.load()
        image = image.convert("RGB")
        width, height = image.size
        if width <= 0 or height <= 0:
            return None
        side = min(width, height)
        left = (width - side) // 2
        top = (height - side) // 2
        image = image.crop((left, top, left + side, top + side)).resize(
            (size, size), Image.LANCZOS
        )
        canvas = Image.new("RGBA", (size, size), (0, 0, 0, 255))
        canvas.paste(image, (0, 0))
        return canvas
    except Exception:
        log.debug("cannot decode cover art", exc_info=True)
        return None


def with_badge(cover: Image.Image, playing: bool, size: int = SIZE) -> Image.Image:
    """Draw a small play/pause badge over the artwork."""
    image = cover.convert("RGBA").copy()
    if image.size != (size, size):
        image = image.resize((size, size), Image.LANCZOS)
    draw = ImageDraw.Draw(image)
    radius = size * 0.32
    cx = size - radius
    cy = size - radius
    draw.ellipse(
        (cx - radius, cy - radius, cx + radius, cy + radius),
        fill=(0, 0, 0, 205),
        outline=(255, 255, 255, 60),
    )
    s = radius * 0.52
    if playing:
        draw.polygon(
            [(cx - s * 0.55, cy - s), (cx - s * 0.55, cy + s), (cx + s, cy)],
            fill=_WHITE,
        )
    else:
        width = max(2, int(s * 0.55))
        height = s * 1.6
        gap = s * 0.45
        top = cy - height / 2.0
        for x in (cx - gap / 2.0 - width, cx + gap / 2.0):
            draw.rounded_rectangle(
                (x, top, x + width, top + height), radius=max(1, width // 3), fill=_WHITE
            )
    return image
