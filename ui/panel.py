"""Auto-hiding dock panel: a small player that slides up from the tray corner.

Threading
---------
Everything here runs in the Qt main thread.  The panel never talks to WinRT:
:meth:`DockPanel.apply_state` only reads an immutable ``TrackSnapshot``, and the
buttons call the small callables provided by :class:`ui.tray_qt.TrayQt`, which
forward actions to the asyncio loop.

Window behaviour
----------------
* ``Qt.Tool`` + ``WS_EX_TOOLWINDOW``: no taskbar button, no Alt+Tab entry.
* ``Qt.WindowStaysOnTopHint`` + ``WS_EX_NOACTIVATE``: it floats above the app,
  but clicking it does not take keyboard focus away from that app.
* ``SHAppBarMessage`` puts it 12 px above the taskbar; a 50 ms cursor poll is the
  trigger, so there is no clickable strip over the taskbar.
* The card is painted with a soft shadow inside a transparent margin.  The
  margin is part of the window, but hover/hide logic uses only the card rect.
"""

from __future__ import annotations

import logging
import time
from typing import Callable, Optional

from PySide6.QtCore import (
    QEasingCurve,
    QPoint,
    QPointF,
    QParallelAnimationGroup,
    QPropertyAnimation,
    QRect,
    QRectF,
    QSize,
    Qt,
    QTimer,
    Signal,
)
from PySide6.QtGui import (
    QColor,
    QCursor,
    QFont,
    QFontMetrics,
    QGuiApplication,
    QLinearGradient,
    QPainter,
    QPainterPath,
    QPaintEvent,
    QPen,
)
from PySide6.QtWidgets import (
    QGraphicsOpacityEffect,
    QHBoxLayout,
    QLabel,
    QSlider,
    QToolButton,
    QToolTip,
    QVBoxLayout,
    QWidget,
)

from smtc.model import TrackSnapshot

from . import qticons, win32

log = logging.getLogger(__name__)

CARD_W = 400
CARD_H = 158
MARGIN = 12
WINDOW_W = CARD_W + 2 * MARGIN
WINDOW_H = CARD_H + 2 * MARGIN
COVER_SIZE = 72
TEXT_W = CARD_W - 2 * 16 - COVER_SIZE - 12
TITLE_W = TEXT_W - 58
VOLUME_LABEL_W = 104
RADIUS = 16

SHOW_MS = 320
HIDE_MS = 220
HIDE_DELAY_MS = 280
TRIGGER_MS = 50
TRIGGER_EDGE = 4
TRIGGER_WIDTH = 300
EXPLICIT_GRACE = 1.5
#: Chromium removes the SMTC session for a moment while switching tracks; a
#: visible panel must survive that gap instead of disappearing mid-click.
IDLE_GRACE_MS = 1500

_BORDER = QColor(255, 255, 255, 34)
_HIGHLIGHT = QColor(255, 255, 255, 18)


def format_time(seconds: float) -> str:
    total = max(0, int(seconds))
    hours, rest = divmod(total, 3600)
    minutes, secs = divmod(rest, 60)
    if hours:
        return f"{hours}:{minutes:02d}:{secs:02d}"
    return f"{minutes}:{secs:02d}"


class SeekBar(QWidget):
    """Slim progress line: click or drag to seek, hover shows the time."""

    seeked = Signal(float)
    scrubbing = Signal(bool)

    def __init__(self, accent: QColor, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self._accent = accent
        self._position = 0.0
        self._duration = 0.0
        self._enabled = False
        self._dragging = False
        self._hover = False
        self.setMouseTracking(True)
        self.setCursor(Qt.CursorShape.ArrowCursor)

    def set_progress(self, position: float, duration: float, enabled: bool) -> None:
        self._position = max(0.0, position)
        self._duration = max(0.0, duration)
        if enabled != self._enabled:
            self._enabled = enabled
            self.setCursor(
                Qt.CursorShape.PointingHandCursor
                if enabled
                else Qt.CursorShape.ArrowCursor
            )
        self.update()

    @property
    def dragging(self) -> bool:
        return self._dragging

    def paintEvent(self, event: QPaintEvent) -> None:  # noqa: N802 - Qt override
        if self._duration <= 0:
            return
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        center_y = self.height() / 2.0
        active = self._hover or self._dragging
        thickness = 4.0 if active else 2.0
        left = 3.0
        right = self.width() - 3.0
        if right <= left:
            painter.end()
            return
        fraction = min(1.0, max(0.0, self._position / self._duration))
        track = QRectF(left, center_y - thickness / 2.0, right - left, thickness)
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(QColor(255, 255, 255, 46))
        painter.drawRoundedRect(track, thickness / 2.0, thickness / 2.0)
        played = QRectF(
            left, center_y - thickness / 2.0, (right - left) * fraction, thickness
        )
        accent = QColor(self._accent)
        if not self._enabled:
            accent.setAlpha(150)
        painter.setBrush(accent)
        painter.drawRoundedRect(played, thickness / 2.0, thickness / 2.0)
        if active:
            handle_x = left + (right - left) * fraction
            painter.setBrush(QColor(255, 255, 255, 245))
            painter.drawEllipse(QPointF(handle_x, center_y), 3.6, 3.6)
        painter.end()

    # -- interaction -------------------------------------------------------

    def mousePressEvent(self, event) -> None:  # noqa: N802 - Qt override
        if event.button() == Qt.MouseButton.LeftButton and self._enabled:
            self._dragging = True
            self.scrubbing.emit(True)
            self._update_from_x(event.position().x())
            event.accept()
            return
        super().mousePressEvent(event)

    def mouseMoveEvent(self, event) -> None:  # noqa: N802 - Qt override
        if self._dragging:
            self._update_from_x(event.position().x())
            event.accept()
            return
        if self._enabled and self._duration > 0:
            self._hover = True
            self.update()
            self._show_tip(event.position().x())
        super().mouseMoveEvent(event)

    def mouseReleaseEvent(self, event) -> None:  # noqa: N802 - Qt override
        if self._dragging and event.button() == Qt.MouseButton.LeftButton:
            value = self._value_at(event.position().x())
            self._dragging = False
            self._position = value
            self.update()
            self.scrubbing.emit(False)
            self.seeked.emit(value)
            QToolTip.hideText()
            event.accept()
            return
        super().mouseReleaseEvent(event)

    def enterEvent(self, event) -> None:  # noqa: N802 - Qt override
        self._hover = True
        self.update()
        super().enterEvent(event)

    def leaveEvent(self, event) -> None:  # noqa: N802 - Qt override
        self._hover = False
        QToolTip.hideText()
        self.update()
        super().leaveEvent(event)

    def _update_from_x(self, x: float) -> None:
        if self._duration <= 0:
            return
        self._position = self._value_at(x)
        self.update()
        self._show_tip(x)

    def _value_at(self, x: float) -> float:
        left = 3.0
        right = self.width() - 3.0
        if right <= left:
            return 0.0
        fraction = min(1.0, max(0.0, (x - left) / (right - left)))
        return fraction * self._duration

    def _show_tip(self, x: float) -> None:
        if self._duration <= 0:
            return
        value = self._value_at(x)
        QToolTip.showText(
            QCursor.pos(),
            f"{format_time(value)} / {format_time(self._duration)}",
            self,
        )


class DockPanel(QWidget):
    visibility_changed = Signal(bool)
    fullscreen_changed = Signal(bool)

    def __init__(
        self,
        on_toggle: Callable[[], None],
        on_next: Callable[[], None],
        on_previous: Callable[[], None],
        on_volume: Callable[[int], None],
        on_mute: Callable[[bool], None],
        on_seek: Callable[[float], None],
        on_menu: Callable[[QPoint], None],
        parent: Optional[QWidget] = None,
    ) -> None:
        super().__init__(parent)
        self._on_toggle = on_toggle
        self._on_next = on_next
        self._on_previous = on_previous
        self._on_volume = on_volume
        self._on_mute = on_mute
        self._on_seek = on_seek
        self._on_menu = on_menu

        self._state = TrackSnapshot()
        self._visible = False
        self._pinned = False
        self._target = QPoint(0, 0)
        self._anim: Optional[QParallelAnimationGroup] = None
        self._native_ready = False
        self._cover_key: tuple = ()
        self._cover_effect: Optional[QGraphicsOpacityEffect] = None
        self._cover_anim: Optional[QPropertyAnimation] = None
        self._muted_icon: Optional[bool] = None
        self._playing_icon: Optional[bool] = None
        self._syncing_volume = False
        self._scrubbing = False
        self._base_position = 0.0
        self._base_at = time.monotonic()
        self._duration = 0.0
        self._playing = False
        self._can_seek = False
        self._gate = win32.FullscreenGate()
        self._grace_until = 0.0
        self._last_blocked = False
        self._backdrop = None

        self.setWindowTitle("Pereklych — плеер")
        self.setWindowFlags(
            Qt.WindowType.FramelessWindowHint
            | Qt.WindowType.WindowStaysOnTopHint
            | Qt.WindowType.Tool
            | Qt.WindowType.NoDropShadowWindowHint
            | Qt.WindowType.WindowDoesNotAcceptFocus
        )
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, True)
        self.setAttribute(Qt.WidgetAttribute.WA_ShowWithoutActivating, True)
        self.setAttribute(Qt.WidgetAttribute.WA_AlwaysShowToolTips, True)
        self.setFixedSize(WINDOW_W, WINDOW_H)
        self.setWindowOpacity(0.0)
        self.hide()

        self._dpr = max(1.0, self.devicePixelRatioF())
        self._accent = qticons.accent_color()
        self._accent_ink = "#10141A" if self._accent.lightnessF() > 0.62 else "#FFFFFF"
        self._icons = self._build_icons()
        self._shadow = qticons.shadow_pixmap(
            WINDOW_W,
            WINDOW_H,
            MARGIN,
            MARGIN,
            CARD_W,
            CARD_H,
            RADIUS,
            self._dpr,
        )

        self._build_ui()
        self._place_chrome()

        self._hide_timer = QTimer(self)
        self._hide_timer.setSingleShot(True)
        self._hide_timer.setInterval(HIDE_DELAY_MS)
        self._hide_timer.timeout.connect(self.hide_panel)

        self._idle_timer = QTimer(self)
        self._idle_timer.setSingleShot(True)
        self._idle_timer.setInterval(IDLE_GRACE_MS)
        self._idle_timer.timeout.connect(self._on_idle_timeout)

        self._trigger_timer = QTimer(self)
        self._trigger_timer.setInterval(TRIGGER_MS)
        self._trigger_timer.timeout.connect(self._on_tick)

        self._progress_timer = QTimer(self)
        self._progress_timer.setInterval(200)
        self._progress_timer.timeout.connect(self._tick_progress)

        self._volume_timer = QTimer(self)
        self._volume_timer.setSingleShot(True)
        self._volume_timer.setInterval(140)
        self._volume_timer.timeout.connect(self._commit_volume)

        self.apply_state(TrackSnapshot())

    # -- construction ------------------------------------------------------

    def _build_icons(self) -> dict:
        return {
            "prev": qticons.prev_icon(22, self._dpr),
            "next": qticons.next_icon(22, self._dpr),
            "play": qticons.play_icon(22, self._dpr, self._accent_ink),
            "pause": qticons.pause_icon(22, self._dpr, self._accent_ink),
            "volume": qticons.volume_icon(16, self._dpr),
            "mute": qticons.mute_icon(16, self._dpr),
            "pin": qticons.pin_icon(16, self._dpr),
            "pin_on": qticons.pin_icon(16, self._dpr, self._accent.name()),
            "dots": qticons.dots_icon(16, self._dpr),
        }

    def _build_ui(self) -> None:
        accent = self._accent.name()
        accent_hover = self._accent.lighter(115).name()
        accent_pressed = self._accent.darker(108).name()
        self.setStyleSheet(
            f"""
            QLabel#titleLabel {{ color: #F4F5F7; background: transparent; }}
            QLabel#artistLabel {{ color: #AEB4BD; background: transparent; }}
            QLabel#volumeValue {{ color: #8F959E; background: transparent; }}
            QToolButton {{
                background: transparent; border: none; border-radius: 20px;
            }}
            QToolButton:hover {{ background: rgba(255, 255, 255, 0.10); }}
            QToolButton:pressed {{ background: rgba(255, 255, 255, 0.16); }}
            QToolButton:disabled {{ background: transparent; }}
            QToolButton#toggleButton {{
                background: {accent}; border-radius: 22px;
            }}
            QToolButton#toggleButton:hover {{ background: {accent_hover}; }}
            QToolButton#toggleButton:pressed {{ background: {accent_pressed}; }}
            QToolButton#toggleButton:disabled {{ background: rgba(255, 255, 255, 0.12); }}
            QToolButton#pinButton, QToolButton#menuButton {{ border-radius: 12px; }}
            QSlider::groove:horizontal {{
                height: 4px; border-radius: 2px;
                background: rgba(255, 255, 255, 0.16);
            }}
            QSlider::sub-page:horizontal {{ background: {accent}; border-radius: 2px; }}
            QSlider::sub-page:horizontal:disabled {{ background: rgba(255, 255, 255, 0.20); }}
            QSlider::handle:horizontal {{
                width: 11px; height: 11px; margin: -4px 0;
                border-radius: 5px; background: #FFFFFF;
            }}
            QSlider::handle:horizontal:hover {{ background: #FFFFFF; }}
            QSlider::handle:horizontal:disabled {{ background: rgba(255, 255, 255, 0.35); }}
            QToolTip {{
                background: #202226; color: #E8EAED;
                border: 1px solid rgba(255, 255, 255, 0.16);
                padding: 4px 6px;
            }}
            """
        )

        outer = QVBoxLayout(self)
        outer.setContentsMargins(MARGIN + 16, MARGIN + 14, MARGIN + 16, MARGIN + 22)
        outer.setSpacing(6)

        top = QHBoxLayout()
        top.setSpacing(12)
        self.cover_label = QLabel(self)
        self.cover_label.setFixedSize(COVER_SIZE, COVER_SIZE)
        self.cover_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        top.addWidget(self.cover_label, 0, Qt.AlignmentFlag.AlignTop)

        text = QVBoxLayout()
        text.setSpacing(1)
        title_font = QFont("Segoe UI Variable", 12)
        title_font.setWeight(QFont.Weight.DemiBold)
        self.title_label = QLabel(self)
        self.title_label.setObjectName("titleLabel")
        self.title_label.setFont(title_font)
        self.title_label.setFixedHeight(21)
        artist_font = QFont("Segoe UI Variable", 10)
        self.artist_label = QLabel(self)
        self.artist_label.setObjectName("artistLabel")
        self.artist_label.setFont(artist_font)
        self.artist_label.setFixedHeight(16)
        text.addWidget(self.title_label)
        text.addWidget(self.artist_label)

        volume = QHBoxLayout()
        volume.setSpacing(6)
        volume.setContentsMargins(0, 2, 0, 0)
        self.mute_button = QToolButton(self)
        self.mute_button.setIcon(self._icons["volume"])
        self.mute_button.setIconSize(QSize(16, 16))
        self.mute_button.setFixedSize(22, 22)
        self.mute_button.setCursor(Qt.CursorShape.PointingHandCursor)
        self.mute_button.clicked.connect(self._on_mute_clicked)
        self.volume_slider = QSlider(Qt.Orientation.Horizontal, self)
        self.volume_slider.setRange(0, 100)
        self.volume_slider.setSingleStep(2)
        self.volume_slider.setPageStep(10)
        self.volume_slider.setFixedHeight(18)
        self.volume_slider.setMinimumWidth(80)
        self.volume_slider.setCursor(Qt.CursorShape.PointingHandCursor)
        self.volume_slider.valueChanged.connect(self._on_volume_value)
        self.volume_slider.sliderReleased.connect(self._on_volume_released)
        self.volume_value = QLabel(self)
        self.volume_value.setObjectName("volumeValue")
        self.volume_value.setFont(QFont("Segoe UI Variable", 9))
        self.volume_value.setFixedWidth(VOLUME_LABEL_W)
        self.volume_value.setAlignment(
            Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter
        )
        volume.addWidget(self.mute_button, 0)
        volume.addWidget(self.volume_slider, 1)
        volume.addWidget(self.volume_value, 0)
        text.addLayout(volume)
        text.addStretch(1)
        top.addLayout(text, 1)
        outer.addLayout(top, 1)

        controls = QHBoxLayout()
        controls.setSpacing(18)
        controls.setContentsMargins(0, 0, 0, 0)
        self.prev_button = self._control_button(
            self._icons["prev"], 40, "Предыдущий трек"
        )
        self.prev_button.clicked.connect(lambda: self._on_previous())
        self.toggle_button = self._control_button(
            self._icons["play"], 44, "Играть", "toggleButton"
        )
        self.toggle_button.clicked.connect(lambda: self._on_toggle())
        self.next_button = self._control_button(
            self._icons["next"], 40, "Следующий трек"
        )
        self.next_button.clicked.connect(lambda: self._on_next())
        controls.addStretch(1)
        controls.addWidget(self.prev_button)
        controls.addWidget(self.toggle_button)
        controls.addWidget(self.next_button)
        controls.addStretch(1)
        outer.addLayout(controls)

        self.seek = SeekBar(self._accent, self)
        self.seek.seeked.connect(self._on_seek_value)
        self.seek.scrubbing.connect(self._on_scrubbing)

        self.pin_button = QToolButton(self)
        self.pin_button.setObjectName("pinButton")
        self.pin_button.setIcon(self._icons["pin"])
        self.pin_button.setIconSize(QSize(16, 16))
        self.pin_button.setFixedSize(24, 24)
        self.pin_button.setToolTip("Закрепить панель")
        self.pin_button.setCursor(Qt.CursorShape.PointingHandCursor)
        self.pin_button.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self.pin_button.clicked.connect(self._toggle_pin)

        self.menu_button = QToolButton(self)
        self.menu_button.setObjectName("menuButton")
        self.menu_button.setIcon(self._icons["dots"])
        self.menu_button.setIconSize(QSize(16, 16))
        self.menu_button.setFixedSize(24, 24)
        self.menu_button.setToolTip("Настройки")
        self.menu_button.setCursor(Qt.CursorShape.PointingHandCursor)
        self.menu_button.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self.menu_button.clicked.connect(self._open_menu)

    def _control_button(
        self,
        icon,
        size: int,
        tooltip: str,
        object_name: str = "",
    ) -> QToolButton:
        button = QToolButton(self)
        button.setIcon(icon)
        button.setIconSize(QSize(22, 22))
        button.setFixedSize(size, size)
        button.setToolTip(tooltip)
        button.setCursor(Qt.CursorShape.PointingHandCursor)
        button.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        if object_name:
            button.setObjectName(object_name)
        return button

    def _place_chrome(self) -> None:
        self.menu_button.setGeometry(MARGIN + CARD_W - 16 - 24, MARGIN + 8, 24, 24)
        self.pin_button.setGeometry(
            MARGIN + CARD_W - 16 - 24 - 26, MARGIN + 8, 24, 24
        )
        self.pin_button.raise_()
        self.menu_button.raise_()
        self.seek.setGeometry(MARGIN + 18, MARGIN + CARD_H - 14, CARD_W - 36, 10)
        self.seek.raise_()

    # -- state -------------------------------------------------------------

    def apply_state(self, state: TrackSnapshot) -> None:
        self._state = state
        ready = state.status == "ready"

        if ready:
            title = state.title or "Без названия"
            subtitle = state.artist or state.album
            if state.app_label:
                subtitle = (
                    f"{subtitle} · {state.app_label}" if subtitle else state.app_label
                )
        else:
            title = "Нет активной медиа-сессии"
            subtitle = state.detail or "Запустите воспроизведение в Edge"
        self.title_label.setText(self._elide(title, self.title_label, TITLE_W))
        self.artist_label.setText(self._elide(subtitle, self.artist_label, TEXT_W))

        self.prev_button.setEnabled(ready and state.can_prev)
        self.next_button.setEnabled(ready and state.can_next)
        self.toggle_button.setEnabled(ready and state.can_play)

        playing = bool(ready and state.is_playing)
        if playing != self._playing_icon:
            self._playing_icon = playing
            self.toggle_button.setIcon(
                self._icons["pause"] if playing else self._icons["play"]
            )
            self.toggle_button.setToolTip("Пауза" if playing else "Играть")

        self._apply_cover(state)
        self._apply_volume(state)

        self._base_position = max(0.0, state.position)
        self._base_at = time.monotonic()
        self._duration = max(0.0, state.duration)
        self._playing = playing
        self._can_seek = bool(ready and state.can_seek)
        if not self._scrubbing:
            self.seek.set_progress(self._base_position, self._duration, self._can_seek)
        if not ready:
            self.seek.set_progress(0.0, 0.0, False)

        if ready:
            self._idle_timer.stop()
        elif self._visible:
            # A Chromium track switch briefly drops the session; keep the panel
            # for a moment so the click after "next" still lands on a button.
            self._idle_timer.start()

    def _elide(self, text: str, label: QLabel, width: int) -> str:
        metrics = QFontMetrics(label.font())
        return metrics.elidedText(text, Qt.TextElideMode.ElideRight, width)

    def _apply_cover(self, state: TrackSnapshot) -> None:
        key = (state.cover_key if state.cover else "", bool(state.cover))
        if key == self._cover_key:
            return
        self._cover_key = key
        pixmap = None
        if state.cover:
            pixmap = qticons.cover_pixmap(state.cover, COVER_SIZE, 10, self._dpr)
        if pixmap is None:
            pixmap = qticons.placeholder_pixmap(COVER_SIZE, 10, self._dpr)
        self.cover_label.setPixmap(pixmap)
        self._backdrop = (
            qticons.backdrop_pixmap(state.cover, CARD_W, CARD_H, self._dpr)
            if state.cover
            else None
        )
        self.update()
        self._fade_cover()

    def _fade_cover(self) -> None:
        if self._cover_effect is None:
            self._cover_effect = QGraphicsOpacityEffect(self.cover_label)
            self.cover_label.setGraphicsEffect(self._cover_effect)
        if self._cover_anim is not None:
            self._cover_anim.stop()
            self._cover_anim.deleteLater()
            self._cover_anim = None
        effect = self._cover_effect
        effect.setOpacity(0.0)
        animation = QPropertyAnimation(effect, b"opacity", self)
        animation.setDuration(180)
        animation.setStartValue(0.0)
        animation.setEndValue(1.0)
        animation.setEasingCurve(QEasingCurve.Type.OutCubic)
        self._cover_anim = animation
        animation.start()

    def _apply_volume(self, state: TrackSnapshot) -> None:
        available = state.volume_percent is not None
        self.volume_slider.setEnabled(available)
        self.mute_button.setEnabled(available)
        if not available:
            self.volume_slider.setToolTip("Громкость недоступна")
            self.mute_button.setToolTip("Громкость недоступна")
            self.volume_value.setText("Громкость недоступна")
            return
        percent = int(state.volume_percent)
        if not self.volume_slider.isSliderDown():
            self._syncing_volume = True
            self.volume_slider.setValue(percent)
            self._syncing_volume = False
        label = state.volume_label or "Громкость"
        tooltip = f"{label}: {percent} %"
        if state.volume_muted:
            tooltip += " (без звука)"
        self.volume_slider.setToolTip(tooltip)
        self.mute_button.setToolTip(
            "Включить звук" if state.volume_muted else "Выключить звук"
        )
        self.volume_value.setText(
            self._elide(
                f"{label} · {percent} %", self.volume_value, VOLUME_LABEL_W
            )
        )
        self.volume_value.setToolTip(tooltip)
        muted = bool(state.volume_muted)
        if muted != self._muted_icon:
            self._muted_icon = muted
            self.mute_button.setIcon(
                self._icons["mute"] if muted else self._icons["volume"]
            )

    # -- volume / seek interaction -----------------------------------------

    def _on_volume_value(self, value: int) -> None:
        if self._syncing_volume:
            return
        label = self._state.volume_label or "Громкость"
        self.volume_slider.setToolTip(f"{label}: {int(value)} %")
        self.volume_value.setText(
            self._elide(
                f"{label} · {int(value)} %", self.volume_value, VOLUME_LABEL_W
            )
        )
        self._volume_timer.start()

    def _on_volume_released(self) -> None:
        self._volume_timer.stop()
        self._commit_volume()

    def _commit_volume(self) -> None:
        if self._state.volume_percent is None:
            return
        self._on_volume(int(self.volume_slider.value()))

    def _on_mute_clicked(self) -> None:
        self._on_mute(not bool(self._state.volume_muted))

    def _on_scrubbing(self, active: bool) -> None:
        self._scrubbing = active

    def _on_seek_value(self, seconds: float) -> None:
        self._base_position = float(seconds)
        self._base_at = time.monotonic()
        self._on_seek(float(seconds))

    def _tick_progress(self) -> None:
        if self._scrubbing or self._duration <= 0 or not self._playing:
            return
        position = self._base_position + (time.monotonic() - self._base_at)
        self.seek.set_progress(
            min(position, self._duration), self._duration, self._can_seek
        )

    def wheelEvent(self, event) -> None:  # noqa: N802 - Qt override
        if self._state.volume_percent is None:
            return
        delta = event.angleDelta().y()
        if not delta:
            return
        step = 2 if delta > 0 else -2
        value = max(0, min(100, self.volume_slider.value() + step))
        self.volume_slider.setValue(value)
        label = self._state.volume_label or "Громкость"
        QToolTip.showText(QCursor.pos(), f"{label}: {value} %", self)
        event.accept()

    # -- pin / menu --------------------------------------------------------

    def _toggle_pin(self) -> None:
        self._pinned = not self._pinned
        self._update_pin()

    def _update_pin(self) -> None:
        self.pin_button.setIcon(self._icons["pin_on" if self._pinned else "pin"])
        self.pin_button.setToolTip(
            "Открепить панель" if self._pinned else "Закрепить панель"
        )

    def _open_menu(self) -> None:
        anchor = self.menu_button.geometry().bottomLeft()
        self._on_menu(self.mapToGlobal(anchor))

    def contextMenuEvent(self, event) -> None:  # noqa: N802 - Qt override
        self._on_menu(event.globalPosition().toPoint())
        event.accept()

    # -- visibility --------------------------------------------------------

    @property
    def panel_visible(self) -> bool:
        return self._visible

    def start(self) -> None:
        self._trigger_timer.start()
        self._progress_timer.start()

    def stop(self) -> None:
        self._trigger_timer.stop()
        self._progress_timer.stop()
        self._hide_timer.stop()
        self._idle_timer.stop()
        self._volume_timer.stop()
        self._stop_anim()
        self._visible = False
        self.hide()
        self.close()

    def toggle_panel(self) -> None:
        if self._visible:
            self.hide_panel()
        else:
            self.show_panel(grace=True)

    def can_show(self) -> bool:
        if self._state.status != "ready":
            return False
        return not self._gate.blocked(self._own_hwnds())

    def show_panel(self, grace: bool = False) -> None:
        if not self.can_show():
            return
        if grace:
            self._grace_until = time.monotonic() + EXPLICIT_GRACE
        if self._visible:
            self.raise_()
            return
        self._target = self._target_position()
        self._stop_anim()
        self._hide_timer.stop()
        self._visible = True
        self.visibility_changed.emit(True)
        self.setWindowOpacity(0.0)
        self.move(self._target.x(), self._target.y() + 44)
        self.show()
        self._prepare_native()
        self._animate(show=True)
        log.debug("panel shown at %s (card %dx%d)", self._target, CARD_W, CARD_H)

    def hide_panel(self) -> None:
        self._hide_timer.stop()
        self._idle_timer.stop()
        self._pinned = False
        self._update_pin()
        if not self._visible:
            return
        self._visible = False
        self.visibility_changed.emit(False)
        self._stop_anim()
        self._animate(show=False)
        QToolTip.hideText()
        log.debug("panel hidden")

    def _animate(self, show: bool) -> None:
        self._anim = QParallelAnimationGroup(self)
        position = QPropertyAnimation(self, b"pos", self._anim)
        position.setDuration(SHOW_MS if show else HIDE_MS)
        position.setStartValue(self.pos())
        position.setEndValue(
            self._target if show else QPoint(self._target.x(), self._target.y() + 18)
        )
        position.setEasingCurve(
            QEasingCurve.Type.OutQuint if show else QEasingCurve.Type.InCubic
        )
        opacity = QPropertyAnimation(self, b"windowOpacity", self._anim)
        opacity.setDuration(260 if show else 200)
        opacity.setStartValue(0.0 if show else self.windowOpacity())
        opacity.setEndValue(1.0 if show else 0.0)
        opacity.setEasingCurve(
            QEasingCurve.Type.OutCubic if show else QEasingCurve.Type.InCubic
        )
        self._anim.addAnimation(position)
        self._anim.addAnimation(opacity)
        if show:
            self._anim.finished.connect(lambda: self.raise_())
        else:
            self._anim.finished.connect(self._finish_hide)
        self._anim.start()

    def _finish_hide(self) -> None:
        if self._visible:
            return
        self.hide()
        self.setWindowOpacity(0.0)

    def _stop_anim(self) -> None:
        if self._anim is not None:
            group = self._anim
            self._anim = None
            try:
                group.stop()
            except RuntimeError:
                pass
            group.deleteLater()

    def _prepare_native(self) -> None:
        try:
            hwnd = int(self.winId())
        except Exception:
            return
        # Qt may recreate the native window after a hide/show cycle, so the
        # extended styles are re-applied every time we show the panel.
        overlay = win32.make_overlay(hwnd)
        if self._native_ready:
            return
        effects = win32.apply_dwm_effects(hwnd)
        self._native_ready = True
        log.info("panel window ready: overlay=%s, effects=%s", overlay, effects)

    # -- triggering --------------------------------------------------------

    def _on_idle_timeout(self) -> None:
        if self._visible and self._state.status != "ready":
            self.hide_panel()

    def _on_tick(self) -> None:
        try:
            blocked = self._gate.blocked(self._own_hwnds())
            if blocked != self._last_blocked:
                self._last_blocked = blocked
                self.fullscreen_changed.emit(blocked)
            if blocked:
                if self._visible:
                    self.hide_panel()
                return
            if self._state.status != "ready":
                if self._visible and not self._idle_timer.isActive():
                    self._idle_timer.start()
                return
            self._idle_timer.stop()
            if self._pinned:
                self._hide_timer.stop()
                return
            cursor = QCursor.pos()
            inside = (
                self._visible
                and (
                    self._card_rect().contains(self.mapFromGlobal(cursor))
                    or self.volume_slider.isSliderDown()
                    or self.seek.dragging
                )
            )
            if inside or self._in_trigger(cursor):
                self._hide_timer.stop()
                if not self._visible:
                    self.show_panel()
            elif self._visible and not self._hide_timer.isActive():
                if time.monotonic() < self._grace_until:
                    return
                self._hide_timer.start()
        except Exception:
            log.debug("panel tick failed", exc_info=True)

    def _own_hwnds(self) -> set[int]:
        try:
            return {int(self.winId())}
        except Exception:
            return set()

    def _card_rect(self) -> QRect:
        return QRect(MARGIN, MARGIN, CARD_W, CARD_H)

    def _screen_for(self, pos: QPoint):
        return QGuiApplication.screenAt(pos) or QGuiApplication.primaryScreen()

    def _in_trigger(self, pos: QPoint) -> bool:
        screen = self._screen_for(pos)
        if screen is None:
            return False
        geometry = screen.geometry()
        if pos.x() < geometry.right() - TRIGGER_WIDTH:
            return False
        edge = self._taskbar_edge(screen)
        if edge - TRIGGER_EDGE <= pos.y() <= edge:
            return True
        # Absolute bottom edge: also catches an auto-hidden taskbar and the
        # very corner of the screen.
        return pos.y() >= geometry.bottom() - TRIGGER_EDGE

    def _taskbar_edge(self, screen) -> int:
        """Y of the bottom edge of the usable desktop (taskbar top - 1)."""
        geometry = screen.geometry()
        if screen is QGuiApplication.primaryScreen():
            bounds = win32.taskbar_bounds()
            if bounds is not None:
                _left, top, _right, _bottom, edge = bounds
                if edge == win32.ABE_BOTTOM:
                    dpr = screen.devicePixelRatio() or 1.0
                    logical_top = int(round(top / dpr))
                    if geometry.top() < logical_top <= geometry.bottom():
                        return logical_top - 1
        return screen.availableGeometry().bottom()

    def _target_position(self) -> QPoint:
        screen = self._screen_for(QCursor.pos())
        if screen is None:
            return QPoint(0, 0)
        geometry = screen.geometry()
        edge = self._taskbar_edge(screen)
        card_right = geometry.right() - 12
        card_top = edge - 12 - CARD_H
        x = card_right - CARD_W + 1 - MARGIN
        y = card_top - MARGIN
        x = max(geometry.left() - MARGIN, x)
        y = max(geometry.top() - MARGIN, y)
        return QPoint(x, y)

    # -- painting ----------------------------------------------------------

    def paintEvent(self, event: QPaintEvent) -> None:  # noqa: N802 - Qt override
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        painter.drawPixmap(0, 0, self._shadow)

        card = QRectF(MARGIN, MARGIN, CARD_W, CARD_H)
        path = QPainterPath()
        path.addRoundedRect(card, RADIUS, RADIUS)
        painter.setClipPath(path)

        if self._backdrop is not None:
            painter.drawPixmap(MARGIN, MARGIN, self._backdrop)
        else:
            base = QLinearGradient(0, card.top(), 0, card.bottom())
            base.setColorAt(0.0, QColor(38, 42, 49, 244))
            base.setColorAt(1.0, QColor(23, 25, 30, 247))
            painter.fillRect(card, base)

        overlay = QLinearGradient(0, card.top(), 0, card.bottom())
        if self._backdrop is not None:
            overlay.setColorAt(0.0, QColor(16, 18, 22, 168))
            overlay.setColorAt(1.0, QColor(14, 16, 20, 212))
        else:
            overlay.setColorAt(0.0, QColor(20, 22, 27, 40))
            overlay.setColorAt(1.0, QColor(14, 16, 20, 70))
        painter.fillRect(card, overlay)

        painter.setClipping(False)
        painter.setPen(QPen(_BORDER, 1.0))
        painter.drawPath(path)

        highlight = QPainterPath()
        highlight.addRoundedRect(
            card.adjusted(1.0, 1.0, -1.0, -1.0), RADIUS - 1, RADIUS - 1
        )
        painter.setPen(QPen(_HIGHLIGHT, 1.0))
        painter.drawPath(highlight)
        painter.end()
