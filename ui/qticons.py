"""Qt drawing helpers: PIL -> QPixmap adapter and crisp vector icons.

The tray icon still comes from :mod:`ui.icons` (PIL), the panel uses QPainter so
the symbols stay sharp at 125 / 150 / 200 % scaling.
"""

from __future__ import annotations

import io
from typing import Optional

from PIL import Image, ImageDraw, ImageEnhance, ImageFilter, ImageOps
from PySide6.QtCore import QPointF, QRectF, Qt
from PySide6.QtGui import (
    QColor,
    QIcon,
    QImage,
    QPainter,
    QPainterPath,
    QPen,
    QPixmap,
)

from . import icons, win32

GRID = 24.0


def pil_to_qimage(image: Image.Image) -> QImage:
    """Copy a PIL image into a QImage (detached from the Python buffer)."""
    if image.mode != "RGBA":
        image = image.convert("RGBA")
    data = image.tobytes("raw", "RGBA")
    qimage = QImage(
        data, image.width, image.height, image.width * 4, QImage.Format.Format_RGBA8888
    )
    return qimage.copy()


def pil_to_pixmap(image: Image.Image) -> QPixmap:
    return QPixmap.fromImage(pil_to_qimage(image))


def pil_to_icon(image: Image.Image) -> QIcon:
    return QIcon(pil_to_pixmap(image))


def tray_icon(image: Image.Image) -> QIcon:
    """Tray icons are displayed at 16-32 px; hand Qt a crisp 64 px source."""
    return pil_to_icon(image)


def accent_color() -> QColor:
    """Readable panel accent: Windows accent, or the brand blue for neutrals."""
    red, green, blue = win32.accent_color()
    color = QColor(red, green, blue)
    # A neutral / nearly white accent (common with the default Windows theme)
    # looks like a disabled control; the panel keeps its light-blue brand tint.
    if color.hsvSaturationF() < 0.22 or color.lightnessF() > 0.88:
        return QColor(124, 196, 255)
    if color.lightnessF() < 0.55:
        blend = 0.38
        color = QColor(
            int(red + (255 - red) * blend),
            int(green + (255 - green) * blend),
            int(blue + (255 - blue) * blend),
        )
    return color


def rounded(pixmap: QPixmap, radius: float) -> QPixmap:
    """Clip *pixmap* to a rounded rectangle."""
    result = QPixmap(pixmap.size())
    result.fill(Qt.GlobalColor.transparent)
    painter = QPainter(result)
    painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
    path = QPainterPath()
    path.addRoundedRect(
        QRectF(0, 0, pixmap.width(), pixmap.height()), radius, radius
    )
    painter.setClipPath(path)
    painter.drawPixmap(0, 0, pixmap)
    painter.end()
    result.setDevicePixelRatio(pixmap.devicePixelRatio())
    return result


def _canvas(size: float, dpr: float) -> tuple[QPixmap, QPainter]:
    pixels = max(1, int(round(size * dpr)))
    pixmap = QPixmap(pixels, pixels)
    pixmap.fill(Qt.GlobalColor.transparent)
    pixmap.setDevicePixelRatio(dpr)
    painter = QPainter(pixmap)
    painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
    painter.scale(pixels / GRID, pixels / GRID)
    return pixmap, painter


def _icon(name: str, size: float, color: QColor, dpr: float) -> QIcon:
    pixmap, p = _canvas(size, dpr)
    p.setPen(Qt.PenStyle.NoPen)
    p.setBrush(color)

    if name == "play":
        path = QPainterPath()
        path.moveTo(8.0, 5.2)
        path.lineTo(19.6, 12.0)
        path.lineTo(8.0, 18.8)
        path.closeSubpath()
        p.drawPath(path)
    elif name == "pause":
        p.drawRoundedRect(QRectF(7.4, 5.4, 3.3, 13.2), 1.1, 1.1)
        p.drawRoundedRect(QRectF(13.3, 5.4, 3.3, 13.2), 1.1, 1.1)
    elif name == "prev":
        p.drawRoundedRect(QRectF(4.9, 5.4, 2.7, 13.2), 1.1, 1.1)
        path = QPainterPath()
        path.moveTo(20.2, 5.4)
        path.lineTo(9.3, 12.0)
        path.lineTo(20.2, 18.6)
        path.closeSubpath()
        p.drawPath(path)
    elif name == "next":
        path = QPainterPath()
        path.moveTo(3.8, 5.4)
        path.lineTo(14.7, 12.0)
        path.lineTo(3.8, 18.6)
        path.closeSubpath()
        p.drawPath(path)
        p.drawRoundedRect(QRectF(16.4, 5.4, 2.7, 13.2), 1.1, 1.1)
    elif name in ("volume", "mute"):
        path = QPainterPath()
        path.moveTo(2.6, 9.2)
        path.lineTo(6.4, 9.2)
        path.lineTo(10.4, 5.2)
        path.lineTo(10.4, 18.8)
        path.lineTo(6.4, 14.8)
        path.lineTo(2.6, 14.8)
        path.closeSubpath()
        p.drawPath(path)
        p.setBrush(Qt.BrushStyle.NoBrush)
        pen = QPen(color, 1.7)
        pen.setCapStyle(Qt.PenCapStyle.RoundCap)
        p.setPen(pen)
        if name == "volume":
            p.drawArc(QRectF(11.2, 7.6, 6.2, 8.8), -55 * 16, 110 * 16)
            p.drawArc(QRectF(13.8, 5.4, 7.4, 13.2), -52 * 16, 104 * 16)
        else:
            p.drawLine(14.6, 9.4, 20.6, 15.4)
            p.drawLine(20.6, 9.4, 14.6, 15.4)
    elif name in (
        "pin",
        "dots",
        "panel",
        "source",
        "cover",
        "autostart",
        "exit",
        "zone",
    ):
        _chrome_glyph(name, p, color)
    p.end()
    return QIcon(pixmap)


def _chrome_glyph(name: str, p: QPainter, color: QColor) -> None:
    """Small monochrome glyphs for the panel chrome and the settings menu."""
    if name == "dots":
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(color)
        for x in (6.5, 12.0, 17.5):
            p.drawEllipse(QPointF(x, 12.0), 1.5, 1.5)
        return
    if name == "pin":
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(color)
        head = QPainterPath()
        head.moveTo(8.6, 3.6)
        head.lineTo(15.4, 3.6)
        head.lineTo(14.4, 9.4)
        head.lineTo(17.0, 13.0)
        head.lineTo(7.0, 13.0)
        head.lineTo(9.6, 9.4)
        head.closeSubpath()
        p.drawPath(head)
        pen = QPen(color, 1.8)
        pen.setCapStyle(Qt.PenCapStyle.RoundCap)
        p.setPen(pen)
        p.drawLine(QPointF(12.0, 13.0), QPointF(12.0, 20.4))
        return

    pen = QPen(color, 1.8)
    pen.setCapStyle(Qt.PenCapStyle.RoundCap)
    pen.setJoinStyle(Qt.PenJoinStyle.RoundJoin)
    p.setPen(pen)
    p.setBrush(Qt.BrushStyle.NoBrush)

    if name in ("panel", "source"):
        p.drawRoundedRect(QRectF(3.0, 5.0, 18.0, 13.0), 2.4, 2.4)
        p.drawLine(QPointF(3.4, 14.6), QPointF(20.6, 14.6))
    elif name == "zone":
        p.drawPolyline(
            [QPointF(4.5, 20.0), QPointF(20.0, 20.0), QPointF(20.0, 4.5)]
        )
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(color)
        p.drawEllipse(QPointF(16.4, 16.4), 2.0, 2.0)
    elif name == "cover":
        p.drawRoundedRect(QRectF(4.0, 5.0, 16.0, 14.0), 2.2, 2.2)
        p.setBrush(color)
        p.setPen(Qt.PenStyle.NoPen)
        p.drawEllipse(QPointF(9.2, 9.4), 1.5, 1.5)
        p.setBrush(Qt.BrushStyle.NoBrush)
        p.setPen(pen)
        mountain = QPainterPath()
        mountain.moveTo(5.6, 16.8)
        mountain.lineTo(10.2, 11.8)
        mountain.lineTo(13.0, 14.4)
        mountain.lineTo(15.2, 12.2)
        mountain.lineTo(18.4, 16.8)
        p.drawPath(mountain)
    elif name == "autostart":
        p.drawArc(QRectF(5.2, 5.2, 13.6, 13.6), 45 * 16, 270 * 16)
        p.drawLine(QPointF(12.0, 3.6), QPointF(12.0, 11.2))
    elif name == "exit":
        p.drawPolyline(
            [
                QPointF(13.4, 4.6),
                QPointF(19.4, 4.6),
                QPointF(19.4, 19.4),
                QPointF(13.4, 19.4),
            ]
        )
        p.drawLine(QPointF(4.2, 12.0), QPointF(15.2, 12.0))
        p.drawPolyline(
            [QPointF(11.2, 8.0), QPointF(15.2, 12.0), QPointF(11.2, 16.0)]
        )


def play_icon(size: float = 24, dpr: float = 1.0, color: str = "#10141A") -> QIcon:
    return _icon("play", size, QColor(color), dpr)


def pause_icon(size: float = 24, dpr: float = 1.0, color: str = "#10141A") -> QIcon:
    return _icon("pause", size, QColor(color), dpr)


def prev_icon(size: float = 24, dpr: float = 1.0, color: str = "#E8EAED") -> QIcon:
    return _icon("prev", size, QColor(color), dpr)


def next_icon(size: float = 24, dpr: float = 1.0, color: str = "#E8EAED") -> QIcon:
    return _icon("next", size, QColor(color), dpr)


def volume_icon(size: float = 24, dpr: float = 1.0, color: str = "#C9CDD4") -> QIcon:
    return _icon("volume", size, QColor(color), dpr)


def mute_icon(size: float = 24, dpr: float = 1.0, color: str = "#C9CDD4") -> QIcon:
    return _icon("mute", size, QColor(color), dpr)


def pin_icon(size: float = 14, dpr: float = 1.0, color: str = "#C9CDD4") -> QIcon:
    return _icon("pin", size, QColor(color), dpr)


def dots_icon(size: float = 14, dpr: float = 1.0, color: str = "#C9CDD4") -> QIcon:
    return _icon("dots", size, QColor(color), dpr)


def menu_icon(
    name: str, size: float = 16, dpr: float = 1.0, color: str = "#C9CDD4"
) -> QIcon:
    return _icon(name, size, QColor(color), dpr)


def backdrop_pixmap(
    data: bytes, width: float, height: float, dpr: float
) -> Optional[QPixmap]:
    """Blurred, darkened artwork used as the panel background.

    The image is cropped to fill, blurred at a reduced resolution (cheap) and
    then upscaled, which gives a soft tint without a visible pixel structure.
    """
    if not data:
        return None
    try:
        image = Image.open(io.BytesIO(data))
        image.load()
        image = image.convert("RGB")
    except Exception:
        return None
    pixel_w = max(16, int(round(width * dpr)))
    pixel_h = max(16, int(round(height * dpr)))
    small_w = max(24, pixel_w // 6)
    small_h = max(24, pixel_h // 6)
    try:
        image = ImageOps.fit(image, (small_w, small_h), method=Image.Resampling.LANCZOS)
        image = image.filter(ImageFilter.GaussianBlur(radius=max(2.0, small_w / 16.0)))
        image = ImageEnhance.Color(image).enhance(0.9)
        image = ImageEnhance.Brightness(image).enhance(0.6)
        image = image.resize((pixel_w, pixel_h), Image.Resampling.BILINEAR)
    except Exception:
        return None
    pixmap = pil_to_pixmap(image)
    pixmap.setDevicePixelRatio(dpr)
    return pixmap


def shadow_pixmap(
    window_width: float,
    window_height: float,
    card_x: float,
    card_y: float,
    card_width: float,
    card_height: float,
    radius: float,
    dpr: float,
    blur: float = 9.0,
) -> QPixmap:
    """Soft drop shadow for the card, cached by the caller."""
    pixel_w = max(1, int(round(window_width * dpr)))
    pixel_h = max(1, int(round(window_height * dpr)))
    image = Image.new("RGBA", (pixel_w, pixel_h), (0, 0, 0, 0))
    draw = ImageDraw.Draw(image)
    left = int(round(card_x * dpr))
    top = int(round((card_y + 3) * dpr))
    right = int(round((card_x + card_width) * dpr))
    bottom = int(round((card_y + card_height + 3) * dpr))
    draw.rounded_rectangle(
        (left, top, max(left + 2, right - 1), max(top + 2, bottom - 1)),
        radius=max(2, int(round(radius * dpr))),
        fill=(0, 0, 0, 145),
    )
    image = image.filter(ImageFilter.GaussianBlur(radius=max(2.0, blur * dpr)))
    pixmap = pil_to_pixmap(image)
    pixmap.setDevicePixelRatio(dpr)
    return pixmap



def cover_pixmap(
    data: bytes, size: float, radius: float, dpr: float
) -> Optional[QPixmap]:
    """Decode cover bytes and return a rounded, DPI-aware pixmap."""
    image = icons.decode_cover(data, max(16, int(round(size * dpr))))
    if image is None:
        return None
    # Round in physical pixels, then declare the DPR so QLabel draws it as
    # ``size`` logical pixels instead of clipping the oversized bitmap.
    pixmap = rounded(pil_to_pixmap(image), radius * dpr)
    pixmap.setDevicePixelRatio(dpr)
    return pixmap


def placeholder_pixmap(size: float, radius: float, dpr: float) -> QPixmap:
    """Rounded dark square with a music note (no artwork available)."""
    pixmap, p = _canvas(size, dpr)
    corner = max(1.0, radius * GRID / size)
    p.setPen(Qt.PenStyle.NoPen)
    p.setBrush(QColor(255, 255, 255, 20))
    p.drawRoundedRect(QRectF(0.5, 0.5, 23.0, 23.0), corner, corner)

    note = QColor(255, 255, 255, 130)
    p.setBrush(note)
    p.drawEllipse(QRectF(6.2, 15.0, 4.4, 3.5))
    p.drawEllipse(QRectF(13.8, 13.2, 4.4, 3.5))

    stem = QPen(note, 1.7)
    stem.setCapStyle(Qt.PenCapStyle.RoundCap)
    p.setPen(stem)
    p.drawLine(QPointF(10.3, 16.5), QPointF(10.3, 7.2))
    p.drawLine(QPointF(17.9, 14.7), QPointF(17.9, 5.4))

    beam = QPen(note, 2.4)
    beam.setCapStyle(Qt.PenCapStyle.RoundCap)
    p.setPen(beam)
    p.drawLine(QPointF(10.3, 7.2), QPointF(17.9, 5.4))
    p.end()
    return pixmap
