"""Qt system tray icon + settings menu + the auto-hiding dock panel.

Click mapping
-------------
* left       - show/hide the panel
* middle     - next track (panel stays closed)
* right      - settings menu (Источник, Громкость, обложка, автозапуск, выход)

The tray is a ``QSystemTrayIcon`` in the Qt main thread.  It never touches WinRT:
every action is submitted to the existing ``perekl-async`` loop, and state
arrives through :class:`ui.bridge.UiBridge`.
"""

from __future__ import annotations

import functools
import logging
import time
from typing import Optional

from PySide6.QtCore import QObject, QPoint, Qt
from PySide6.QtGui import QActionGroup, QFont, QFontMetrics, QGuiApplication
from PySide6.QtWidgets import (
    QHBoxLayout,
    QLabel,
    QMenu,
    QSystemTrayIcon,
    QVBoxLayout,
    QWidget,
    QWidgetAction,
)

from core import autostart
from core.config import APP_NAME, Settings
from smtc.model import RepeatMode, TrackSnapshot

from . import icons, qticons
from .hotkey import MOD_ALT, MOD_CONTROL, MOD_NOREPEAT, GlobalHotkey
from .panel import DockPanel
from .trigger_hint import TriggerIndicator

log = logging.getLogger(__name__)

_CLICK_DEBOUNCE = 0.35

#: The Windows 11 Qt style paints menus with a translucent material that goes
#: fully see-through on layered-window setups.  An explicit opaque background
#: keeps the settings menu readable; check marks are still drawn by the style.
_MENU_STYLE = """
QMenu {
    background-color: #1E2024;
    color: #E8EAED;
    border: 1px solid #3A3D44;
    padding: 5px;
}
QMenu::item {
    padding: 6px 26px 6px 12px;
    border-radius: 5px;
}
QMenu::item:selected { background-color: rgba(124, 196, 255, 0.22); }
QMenu::item:disabled { color: #7C818A; }
QMenu::separator { height: 1px; background: #35383F; margin: 4px 8px; }
"""


class TrayQt(QObject):
    def __init__(
        self,
        controller,
        settings: Settings,
        loop_thread,
        on_quit,
        on_save,
    ) -> None:
        super().__init__()
        self._ctl = controller
        self._settings = settings
        self._loop_thread = loop_thread
        self._on_quit = on_quit
        self._on_save = on_save

        self._state = TrackSnapshot()
        self._quitting = False
        self._last_trigger = 0.0
        self._icon_key: tuple = ()
        self._cover_cache = None
        self._cover_cache_key = ""
        self._menu_cover_key: tuple = ()
        self._menu_cover = None
        self._hint_pending = False
        self._hotkey: GlobalHotkey | None = None

        self.panel = DockPanel(
            on_toggle=self.toggle_play_pause,
            on_next=self.skip_next,
            on_previous=self.skip_previous,
            on_volume=self.set_volume_percent,
            on_mute=self.set_muted,
            on_seek=self.seek,
            on_menu=self.popup_menu,
        )
        self.indicator = TriggerIndicator()
        self.panel.visibility_changed.connect(self.indicator.set_panel_visible)
        self.panel.fullscreen_changed.connect(self.indicator.set_fullscreen)

        self.icon = QSystemTrayIcon(self)
        self.icon.setIcon(qticons.tray_icon(icons.base_icon()))
        self.icon.setToolTip(self._tooltip(self._state))
        self.icon.activated.connect(self._on_activated)

        self.menu = QMenu()
        self.menu.setStyleSheet(_MENU_STYLE)
        self.menu.aboutToShow.connect(self._on_menu_shown)
        self.menu.aboutToShow.connect(self._rebuild_menu)
        self.icon.setContextMenu(self.menu)

        self._rebuild_menu()

    # -- lifecycle ---------------------------------------------------------

    def start(self) -> None:
        if not QSystemTrayIcon.isSystemTrayAvailable():
            log.warning("system tray is not available")
        self._hint_pending = not self._settings.hint_shown
        self.icon.show()
        self.panel.start()
        self.indicator.set_mode(self._settings.trigger_indicator)
        self.apply_state(self._state, force=True)
        self._hotkey = GlobalHotkey(MOD_CONTROL | MOD_ALT | MOD_NOREPEAT, ord("P"))
        self._hotkey.triggered.connect(self.panel.toggle_panel)
        self._hotkey.register()
        log.info("tray icon shown")

    def stop(self) -> None:
        self._quitting = True
        if self._hotkey is not None:
            try:
                self._hotkey.unregister()
                self._hotkey.deleteLater()
            except Exception:
                log.debug("hotkey stop failed", exc_info=True)
            self._hotkey = None
        try:
            self.panel.stop()
        except Exception:
            log.debug("panel stop failed", exc_info=True)
        try:
            self.indicator.stop()
        except Exception:
            log.debug("indicator stop failed", exc_info=True)
        try:
            self.menu.close()
            self.icon.hide()
        except Exception:
            log.debug("tray hide failed", exc_info=True)

    # -- state from the async thread (queued signal) -----------------------

    def apply_state(self, state: TrackSnapshot, force: bool = False) -> None:
        self._state = state
        try:
            self.icon.setToolTip(self._tooltip(state))
            self._apply_image(state, force)
            self.panel.apply_state(state)
        except Exception:
            log.debug("cannot apply the tray state", exc_info=True)
        if self._hint_pending and state.status == "ready":
            self._hint_pending = False
            self._settings.hint_shown = True
            self._save()
            try:
                self.indicator.remind(7.0)
            except Exception:
                log.debug("cannot show the corner reminder", exc_info=True)

    def _tooltip(self, state: TrackSnapshot) -> str:
        if state.status != "ready":
            return f"{APP_NAME} — нет активной медиа-сессии"
        parts = [part for part in (state.artist, state.title) if part]
        text = " — ".join(parts) if parts else state.app_label or APP_NAME
        # Windows tooltips are limited to 127 characters and cannot render the
        # pause glyph, so the tooltip stays plain text.
        return text[:126]

    def _apply_image(self, state: TrackSnapshot, force: bool = False) -> None:
        key = (
            state.status,
            state.is_playing,
            state.cover_key,
            bool(self._settings.cover_icon),
        )
        if not force and key == self._icon_key:
            return
        image = None
        if self._settings.cover_icon and state.cover:
            if state.cover_key != self._cover_cache_key:
                self._cover_cache = icons.decode_cover(state.cover)
                self._cover_cache_key = state.cover_key
            if self._cover_cache is not None:
                image = icons.with_badge(self._cover_cache, state.is_playing)
        if image is None:
            image = icons.base_icon(playing=state.is_playing)
        self._icon_key = key
        try:
            self.icon.setIcon(qticons.tray_icon(image))
        except Exception:
            log.debug("cannot set the tray image", exc_info=True)

    # -- clicks ------------------------------------------------------------

    def _on_activated(self, reason: QSystemTrayIcon.ActivationReason) -> None:
        log.debug("tray activated: %s", reason)
        if self._quitting:
            return
        if reason == QSystemTrayIcon.ActivationReason.MiddleClick:
            self.skip_next()
            return
        if reason == QSystemTrayIcon.ActivationReason.DoubleClick:
            # A single click toggles, a double click always ends with the
            # panel visible (the first click of the burst may have hidden it).
            self._last_trigger = time.monotonic()
            self.panel.show_panel(grace=True)
            return
        if reason == QSystemTrayIcon.ActivationReason.Trigger:
            now = time.monotonic()
            if now - self._last_trigger < _CLICK_DEBOUNCE:
                return
            self._last_trigger = now
            self.panel.toggle_panel()
        # Context is handled by Qt: it pops up self.menu.

    # -- actions (Qt -> asyncio loop) --------------------------------------

    def _submit(self, coro) -> None:
        if self._quitting:
            coro.close()
            return
        self._loop_thread.submit(coro)

    def toggle_play_pause(self) -> None:
        self._submit(self._ctl.toggle_play_pause())

    def skip_next(self) -> None:
        self._submit(self._ctl.skip_next())

    def skip_previous(self) -> None:
        self._submit(self._ctl.skip_previous())

    def set_volume_percent(self, percent: int) -> None:
        self._submit(self._volume_job(percent=int(percent)))

    def set_muted(self, muted: bool) -> None:
        self._submit(self._volume_job(muted=bool(muted)))

    def seek(self, seconds: float) -> None:
        self._submit(self._ctl.seek(float(seconds)))

    def _set_volume_mode(self, mode: str) -> None:
        self._submit(self._ctl.set_volume_mode(str(mode)))

    def _volume_step(self, delta: int) -> None:
        self._submit(self._volume_job(delta=int(delta)))

    async def _volume_job(
        self,
        delta: Optional[int] = None,
        muted: Optional[bool] = None,
        percent: Optional[int] = None,
    ) -> None:
        try:
            if percent is not None:
                self._ctl.volume_set_percent(percent)
            elif delta is not None:
                self._ctl.volume_step(delta)
            elif muted is not None:
                self._ctl.volume_set_muted(muted)
        except Exception:
            log.warning("volume action failed", exc_info=True)

    def _set_shuffle(self, active: bool) -> None:
        self._submit(self._ctl.set_shuffle(bool(active)))

    def _set_repeat(self, mode: int) -> None:
        self._submit(self._ctl.set_repeat(int(mode)))

    def _pin_source(self, key: Optional[str]) -> None:
        self._submit(self._ctl.pin_source(key))

    # -- settings actions --------------------------------------------------

    def _toggle_cover(self) -> None:
        self._settings.cover_icon = not self._settings.cover_icon
        self._save()
        self.apply_state(self._state, force=True)

    def _toggle_autostart(self) -> None:
        try:
            autostart.set_enabled(not autostart.is_enabled())
        except Exception:
            log.warning("cannot toggle autostart", exc_info=True)

    def _quit(self) -> None:
        if self._quitting:
            return
        self._quitting = True
        try:
            self._on_quit()
        except Exception:
            log.exception("quit failed")

    # -- menu --------------------------------------------------------------

    def _rebuild_menu(self) -> None:
        try:
            self._fill_menu()
        except Exception:
            log.exception("menu construction failed, using a minimal menu")
            self.menu.clear()
            quit_action = self.menu.addAction("Выход")
            quit_action.triggered.connect(self._quit)

    def popup_menu(self, global_pos) -> None:
        self.panel.hide_panel()
        self._rebuild_menu()
        position = QPoint(global_pos)
        screen = QGuiApplication.screenAt(position) or QGuiApplication.primaryScreen()
        if screen is not None:
            available = screen.availableGeometry()
            hint = self.menu.sizeHint()
            x = min(
                max(position.x(), available.left()),
                available.right() - hint.width(),
            )
            y = position.y()
            # Keep the menu above the taskbar instead of letting it overlap.
            if y + hint.height() > available.bottom():
                y = max(available.top(), y - hint.height() - 6)
            position = QPoint(x, y)
        self.menu.popup(position)

    def _on_menu_shown(self) -> None:
        # The panel and the menu must not overlap; the menu is the focus now.
        self.panel.hide_panel()

    def _fill_menu(self) -> None:
        state = self._state
        menu = self.menu
        menu.clear()
        ready = state.status == "ready"
        dpr = self._menu_dpr()

        menu.addAction(self._make_header(state, dpr))
        menu.addSeparator()

        action = menu.addAction(
            qticons.menu_icon("panel", 16, dpr), "Показать панель"
        )
        action.triggered.connect(lambda: self.panel.show_panel(grace=True))

        self._fill_volume_menu(state, dpr)
        self._fill_source_menu(state, dpr)

        if ready and (state.shuffle_available or state.repeat_available):
            menu.addSeparator()
        if ready and state.shuffle_available:
            shuffle = menu.addAction("Перемешать")
            shuffle.setCheckable(True)
            shuffle.setChecked(bool(state.shuffle_active))
            shuffle.triggered.connect(
                lambda checked: self._set_shuffle(bool(checked))
            )
        if ready and state.repeat_available:
            repeat_menu = menu.addMenu("Повтор")
            repeat_group = QActionGroup(repeat_menu)
            repeat_group.setExclusive(True)
            for mode, text in (
                (RepeatMode.NONE, "Выкл"),
                (RepeatMode.LIST, "Все"),
                (RepeatMode.TRACK, "Один"),
            ):
                action = repeat_menu.addAction(text)
                action.setCheckable(True)
                action.setChecked(state.repeat_mode == int(mode))
                action.setActionGroup(repeat_group)
                action.triggered.connect(
                    functools.partial(self._set_repeat, int(mode))
                )

        menu.addSeparator()
        cover = menu.addAction(
            "Обложка на иконке трея · "
            + ("включена" if self._settings.cover_icon else "выключена")
        )
        cover.setCheckable(True)
        cover.setChecked(bool(self._settings.cover_icon))
        cover.triggered.connect(self._toggle_cover)

        try:
            autostart_on = autostart.is_enabled()
        except Exception:
            autostart_on = False
        log.debug("autostart menu state: enabled=%s", autostart_on)
        auto = menu.addAction(
            "Запускать с Windows · "
            + ("включено" if autostart_on else "выключено")
        )
        auto.setCheckable(True)
        auto.setChecked(autostart_on)
        auto.triggered.connect(self._toggle_autostart)

        self._fill_indicator_menu(dpr)

        menu.addSeparator()
        quit_action = menu.addAction(
            qticons.menu_icon("exit", 16, dpr), "Выход"
        )
        quit_action.triggered.connect(self._quit)

    # -- menu: now playing header -----------------------------------------

    @staticmethod
    def _menu_dpr() -> float:
        screen = QGuiApplication.primaryScreen()
        return max(1.0, screen.devicePixelRatio() if screen is not None else 1.0)

    @staticmethod
    def _elide_text(text: str, font: QFont, width: int) -> str:
        metrics = QFontMetrics(font)
        return metrics.elidedText(text, Qt.TextElideMode.ElideRight, width)

    def _header_cover(self, state: TrackSnapshot, dpr: float):
        key = (state.cover_key if state.cover else "", bool(state.cover), dpr)
        if key == self._menu_cover_key:
            return self._menu_cover
        self._menu_cover_key = key
        pixmap = None
        if state.cover:
            pixmap = qticons.cover_pixmap(state.cover, 40, 9, dpr)
        if pixmap is None:
            pixmap = qticons.placeholder_pixmap(40, 9, dpr)
        self._menu_cover = pixmap
        return pixmap

    def _make_header(self, state: TrackSnapshot, dpr: float) -> QWidgetAction:
        widget = QWidget()
        widget.setStyleSheet("background: transparent;")
        widget.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents, True)
        widget.setMinimumWidth(292)

        row = QHBoxLayout(widget)
        row.setContentsMargins(10, 8, 10, 8)
        row.setSpacing(10)

        cover = QLabel(widget)
        cover.setFixedSize(40, 40)
        cover.setAlignment(Qt.AlignmentFlag.AlignCenter)
        cover.setPixmap(self._header_cover(state, dpr))
        row.addWidget(cover, 0, Qt.AlignmentFlag.AlignTop)

        column = QVBoxLayout()
        column.setSpacing(1)
        if state.status == "ready":
            title = ("▶ " if state.is_playing else "") + (
                state.title or "Без названия"
            )
            subtitle = state.artist or state.album
            if state.app_label:
                subtitle = (
                    f"{subtitle} · {state.app_label}"
                    if subtitle
                    else state.app_label
                )
        else:
            title = "Нет активной медиа-сессии"
            subtitle = state.detail or "Запустите воспроизведение в Edge"

        title_font = QFont("Segoe UI Variable", 10)
        title_font.setWeight(QFont.Weight.DemiBold)
        title_label = QLabel(widget)
        title_label.setFont(title_font)
        title_label.setStyleSheet("color: #F2F3F5; background: transparent;")
        title_label.setText(self._elide_text(title, title_font, 220))

        subtitle_font = QFont("Segoe UI Variable", 9)
        subtitle_label = QLabel(widget)
        subtitle_label.setFont(subtitle_font)
        subtitle_label.setStyleSheet("color: #A9AEB6; background: transparent;")
        subtitle_label.setText(self._elide_text(subtitle, subtitle_font, 220))

        column.addWidget(title_label)
        column.addWidget(subtitle_label)
        column.addStretch(1)
        row.addLayout(column, 1)

        action = QWidgetAction(self.menu)
        action.setDefaultWidget(widget)
        return action

    # -- menu: volume / source --------------------------------------------

    def _fill_volume_menu(self, state: TrackSnapshot, dpr: float) -> None:
        volume_menu = self.menu.addMenu(
            qticons.menu_icon("volume", 16, dpr), "Громкость"
        )
        available = state.volume_percent is not None
        if available:
            label = state.volume_label or "Громкость"
            header = f"{label}: {state.volume_percent} %"
            if state.volume_muted:
                header += " (без звука)"
        else:
            header = "Громкость недоступна"
        action = volume_menu.addAction(header)
        action.setEnabled(False)

        volume_menu.addSeparator()
        group = QActionGroup(volume_menu)
        group.setExclusive(True)
        app_label = state.app_label or "Приложение"
        for mode, text in (
            ("app", f"Приложение ({app_label})"),
            ("system", "Системная"),
        ):
            action = volume_menu.addAction(text)
            action.setCheckable(True)
            action.setChecked(self._settings.volume_mode == mode)
            action.setActionGroup(group)
            action.triggered.connect(
                functools.partial(self._set_volume_mode, mode)
            )

        volume_menu.addSeparator()
        for text, delta in (("−10 %", -10), ("−5 %", -5), ("+5 %", 5), ("+10 %", 10)):
            action = volume_menu.addAction(text)
            action.setEnabled(available)
            action.triggered.connect(functools.partial(self._volume_step, delta))
        mute = volume_menu.addAction("Без звука")
        mute.setCheckable(True)
        mute.setChecked(bool(state.volume_muted))
        mute.setEnabled(available)
        mute.triggered.connect(lambda checked: self.set_muted(bool(checked)))

    def _fill_source_menu(self, state: TrackSnapshot, dpr: float) -> None:
        source_menu = self.menu.addMenu(
            qticons.menu_icon("source", 16, dpr), "Источник"
        )
        group = QActionGroup(source_menu)
        group.setExclusive(True)

        auto = source_menu.addAction("Авто (текущая сессия)")
        auto.setCheckable(True)
        auto.setChecked(state.pinned_key is None)
        auto.setActionGroup(group)
        auto.triggered.connect(lambda _checked=False: self._pin_source(None))

        if state.sources:
            source_menu.addSeparator()
            for source in state.sources:
                action = source_menu.addAction(source.label)
                action.setCheckable(True)
                action.setChecked(state.pinned_key == source.key)
                action.setEnabled(source.key != state.pinned_key)
                action.setActionGroup(group)
                action.triggered.connect(
                    functools.partial(self._pin_source, source.key)
                )
        else:
            source_menu.addSeparator()
            empty = source_menu.addAction("Нет источников")
            empty.setEnabled(False)

    def _fill_indicator_menu(self, dpr: float) -> None:
        zone_menu = self.menu.addMenu(
            qticons.menu_icon("zone", 16, dpr), "Зона срабатывания"
        )
        group = QActionGroup(zone_menu)
        group.setExclusive(True)
        for mode, label in (
            ("hidden", "Скрыта"),
            ("soft", "Полупрозрачная"),
            ("full", "Видна"),
        ):
            action = zone_menu.addAction(label)
            action.setCheckable(True)
            action.setChecked(self._settings.trigger_indicator == mode)
            action.setActionGroup(group)
            action.triggered.connect(
                functools.partial(self._set_indicator_mode, mode)
            )
        zone_menu.addSeparator()
        remind = zone_menu.addAction("Показать сейчас")
        remind.triggered.connect(self._remind_zone)

    def _set_indicator_mode(self, mode: str) -> None:
        if mode not in ("hidden", "soft", "full"):
            return
        self._settings.trigger_indicator = mode
        self._save()
        self.indicator.set_mode(mode)

    def _remind_zone(self) -> None:
        self.indicator.remind(7.0)

    # -- helpers -----------------------------------------------------------

    def _save(self) -> None:
        try:
            self._on_save()
        except Exception:
            log.warning("cannot persist settings", exc_info=True)
