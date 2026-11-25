"""Highlight and reminder for the bottom-right trigger zone.

The indicator is a click-through tool window that sits on top of the taskbar
edge.  It has three persistent modes (hidden / soft / full) and a temporary
reminder with a small speech bubble above the zone.  It is never interactive:
``WindowTransparentForInput`` + ``WA_TransparentForMouseEvents`` keep all clicks
going to the desktop/taskbar, and the cursor-based trigger keeps working.
"""

from __future__ import annotations

import logging
from typing import Optional

from PySide6.QtCore import (
    QEasingCurve,
    QPointF,
    QPropertyAnimation,
    QRectF,
    Qt,
    QTimer,
)
from PySide6.QtGui import (
    QColor,
    QFont,
    QGuiApplication,
    QPainter,
    QPainterPath,
    QPen,
    QPaintEvent,
)
from PySide6.QtWidgets import QWidget

from . import win32
from .panel import TRIGGER_WIDTH

log = logging.getLogger(__name__)

WIDTH = TRIGGER_WIDTH
HEIGHT = 94
BUBBLE_HEIGHT = 46
SOFT_OPACITY = 0.42


class TriggerIndicator(QWidget):
    MODES = ("hidden", "soft", "full")

    def __init__(self, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self._mode = "hidden"
        self._reminding = False
        self._panel_visible = False
        self._fullscreen = False
        self._native_ready = False
        self._fade: Optional[QPropertyAnimation] = None

        self.setWindowTitle("Pereklych — зона вызова")
        self.setWindowFlags(
            Qt.WindowType.Tool
            | Qt.WindowType.FramelessWindowHint
            | Qt.WindowType.WindowStaysOnTopHint
            | Qt.WindowType.WindowTransparentForInput
            | Qt.WindowType.WindowDoesNotAcceptFocus
            | Qt.WindowType.NoDropShadowWindowHint
        )
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, True)
        self.setAttribute(Qt.WidgetAttribute.WA_ShowWithoutActivating, True)
        self.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents, True)
        self.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self.setFixedSize(WIDTH, HEIGHT)
        self.setWindowOpacity(0.0)
        self.hide()

        self._reminder_timer = QTimer(self)
        self._reminder_timer.setSingleShot(True)
        self._reminder_timer.timeout.connect(self._reminder_finished)

    # -- public API --------------------------------------------------------

    @property
    def mode(self) -> str:
        return self._mode

    def set_mode(self, mode: str) -> None:
        if mode not in self.MODES:
            mode = "hidden"
        self._mode = mode
        if self._reminding:
            return
        if mode == "hidden" or self._panel_visible or self._fullscreen:
            self._hide_animated()
            return
        self._show_at(SOFT_OPACITY if mode == "soft" else 1.0)

    def remind(self, seconds: float = 7.0) -> None:
        """Show the zone at full opacity with a short explanation bubble."""
        self._reminding = True
        self._reminder_timer.start(max(1000, int(seconds * 1000)))
        log.debug(
            "trigger reminder requested: %.1fs (panel=%s, fullscreen=%s)",
            seconds,
            self._panel_visible,
            self._fullscreen,
        )
        if self._panel_visible or self._fullscreen:
            self.update()
            return
        self._show_at(1.0)

    def set_panel_visible(self, visible: bool) -> None:
        if visible == self._panel_visible:
            return
        self._panel_visible = visible
        if visible or self._fullscreen:
            # The panel covers the corner; do not draw the hint underneath it.
            self._stop_fade()
            self.hide()
            self.setWindowOpacity(0.0)
            return
        if self._reminding:
            self._show_at(1.0)
        elif self._mode != "hidden":
            self._show_at(SOFT_OPACITY if self._mode == "soft" else 1.0)

    def set_fullscreen(self, blocked: bool) -> None:
        if blocked == self._fullscreen:
            return
        self._fullscreen = blocked
        if blocked:
            self._stop_fade()
            self.hide()
            self.setWindowOpacity(0.0)
            return
        if self._panel_visible:
            return
        if self._reminding:
            self._show_at(1.0)
        elif self._mode != "hidden":
            self._show_at(SOFT_OPACITY if self._mode == "soft" else 1.0)

    def stop(self) -> None:
        self._reminder_timer.stop()
        self._stop_fade()
        self.hide()
        self.close()

    # -- geometry ----------------------------------------------------------

    def _screen(self):
        return QGuiApplication.primaryScreen()

    def _edge(self, screen) -> int:
        geometry = screen.geometry()
        bounds = win32.taskbar_bounds()
        if bounds is not None:
            _left, top, _right, _bottom, edge = bounds
            if edge == win32.ABE_BOTTOM:
                dpr = screen.devicePixelRatio() or 1.0
                logical_top = int(round(top / dpr))
                if geometry.top() < logical_top <= geometry.bottom():
                    return logical_top - 1
        return screen.availableGeometry().bottom()

    def _reposition(self) -> None:
        screen = self._screen()
        if screen is None:
            return
        geometry = screen.geometry()
        edge = self._edge(screen)
        x = geometry.right() - WIDTH + 1
        y = edge - HEIGHT + 1
        x = max(geometry.left(), x)
        y = max(geometry.top(), y)
        self.setGeometry(x, y, WIDTH, HEIGHT)

    # -- visibility --------------------------------------------------------

    def _show_at(self, opacity: float) -> None:
        self._reposition()
        was_visible = self.isVisible()
        log.debug(
            "trigger indicator show: opacity=%.2f reminding=%s was_visible=%s",
            opacity,
            self._reminding,
            was_visible,
        )
        if not was_visible:
            self.setWindowOpacity(0.0)
            self.show()
            self._prepare_native()
        self.update()
        self.raise_()
        self._fade_to(opacity)

    def _hide_animated(self) -> None:
        if not self.isVisible():
            return
        self._fade_to(0.0, hide_after=True)

    def _reminder_finished(self) -> None:
        log.debug("trigger reminder finished (mode=%s)", self._mode)
        self._reminding = False
        self.update()
        if self._mode == "hidden" or self._fullscreen:
            self._hide_animated()
        elif self._panel_visible:
            self._stop_fade()
            self.hide()
            self.setWindowOpacity(0.0)
        else:
            self._fade_to(SOFT_OPACITY if self._mode == "soft" else 1.0)

    def _fade_to(self, target: float, hide_after: bool = False) -> None:
        if not self.isVisible() and target <= 0.0:
            return
        self._stop_fade()
        animation = QPropertyAnimation(self, b"windowOpacity", self)
        animation.setDuration(220)
        animation.setStartValue(self.windowOpacity())
        animation.setEndValue(float(target))
        animation.setEasingCurve(QEasingCurve.Type.OutCubic)
        if hide_after:
            animation.finished.connect(self._hide_after_fade)
        self._fade = animation
        animation.start()

    def _hide_after_fade(self) -> None:
        if self.windowOpacity() <= 0.01:
            self.hide()
            self.setWindowOpacity(0.0)

    def _stop_fade(self) -> None:
        if self._fade is not None:
            animation = self._fade
            self._fade = None
            try:
                animation.stop()
            except RuntimeError:
                pass
            animation.deleteLater()

    def _prepare_native(self) -> None:
        if self._native_ready:
            return
        try:
            hwnd = int(self.winId())
        except Exception:
            return
        win32.make_overlay(hwnd)
        win32.remove_window_border(hwnd)
        self._native_ready = True

    # -- painting ----------------------------------------------------------

    def paintEvent(self, event: QPaintEvent) -> None:  # noqa: N802 - Qt override
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)

        # Just a quiet grey hairline at the taskbar edge: no accent, no glow.
        color = QColor(146, 151, 159, 120)
        if self._reminding:
            color = QColor(196, 201, 209, 205)
        pen = QPen(color, 1.6)
        pen.setCapStyle(Qt.PenCapStyle.RoundCap)
        painter.setPen(pen)
        painter.drawLine(
            QPointF(4.0, self.height() - 2.2),
            QPointF(self.width() - 4.0, self.height() - 2.2),
        )

        if self._reminding:
            self._paint_bubble(painter)
        painter.end()

    def _paint_bubble(self, painter: QPainter) -> None:
        bubble_bottom = self.height() - 12.0
        bubble = QRectF(
            8.0,
            bubble_bottom - BUBBLE_HEIGHT,
            self.width() - 16.0,
            BUBBLE_HEIGHT,
        )
        painter.setPen(QPen(QColor(255, 255, 255, 42), 1.0))
        painter.setBrush(QColor(21, 23, 28, 245))
        painter.drawRoundedRect(bubble, 12.0, 12.0)

        arrow = QPainterPath()
        center = self.width() / 2.0
        arrow.moveTo(center - 7.0, bubble.bottom() - 0.5)
        arrow.lineTo(center + 7.0, bubble.bottom() - 0.5)
        arrow.lineTo(center, bubble.bottom() + 8.0)
        arrow.closeSubpath()
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(QColor(21, 23, 28, 245))
        painter.drawPath(arrow)

        title_font = QFont("Segoe UI Variable", 10)
        title_font.setWeight(QFont.Weight.DemiBold)
        painter.setFont(title_font)
        painter.setPen(QColor("#EDEFF2"))
        painter.drawText(
            QRectF(bubble.left() + 8, bubble.top() + 4, bubble.width() - 16, 20),
            Qt.AlignmentFlag.AlignHCenter | Qt.AlignmentFlag.AlignVCenter,
            "Панель открывается здесь",
        )

        subtitle_font = QFont("Segoe UI Variable", 9)
        painter.setFont(subtitle_font)
        painter.setPen(QColor("#A0A5AD"))
        painter.drawText(
            QRectF(bubble.left() + 8, bubble.top() + 24, bubble.width() - 16, 18),
            Qt.AlignmentFlag.AlignHCenter | Qt.AlignmentFlag.AlignVCenter,
            "наведите курсор в этот угол",
        )
