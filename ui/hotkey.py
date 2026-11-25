"""Global hotkey (``RegisterHotKey``) delivered to a hidden native window.

Qt has no cross-platform API for system-wide hotkeys; on Windows the usual
approach is a hidden window with ``RegisterHotKey`` and a ``WM_HOTKEY`` handler.
The widget is never shown and never appears in Alt+Tab (tool window, no show).
"""

from __future__ import annotations

import ctypes
import logging
from ctypes import wintypes
from typing import Optional

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import QWidget

log = logging.getLogger(__name__)

MOD_ALT = 0x0001
MOD_CONTROL = 0x0002
MOD_SHIFT = 0x0004
MOD_WIN = 0x0008
MOD_NOREPEAT = 0x4000
WM_HOTKEY = 0x0312

_user32 = ctypes.WinDLL("user32", use_last_error=True)
_user32.RegisterHotKey.argtypes = [
    wintypes.HWND,
    ctypes.c_int,
    wintypes.UINT,
    wintypes.UINT,
]
_user32.RegisterHotKey.restype = wintypes.BOOL
_user32.UnregisterHotKey.argtypes = [wintypes.HWND, ctypes.c_int]
_user32.UnregisterHotKey.restype = wintypes.BOOL


class _POINT(ctypes.Structure):
    _fields_ = [("x", wintypes.LONG), ("y", wintypes.LONG)]


class _MSG(ctypes.Structure):
    _fields_ = [
        ("hwnd", wintypes.HWND),
        ("message", wintypes.UINT),
        ("wParam", wintypes.WPARAM),
        ("lParam", wintypes.LPARAM),
        ("time", wintypes.DWORD),
        ("pt", _POINT),
    ]


class GlobalHotkey(QWidget):
    """Hidden widget that owns a system-wide hotkey."""

    triggered = Signal()

    def __init__(
        self,
        modifiers: int,
        key: int,
        hotkey_id: int = 0xB001,
        parent: Optional[QWidget] = None,
    ) -> None:
        super().__init__(parent)
        self._modifiers = int(modifiers)
        self._key = int(key)
        self._id = int(hotkey_id)
        self._registered = False
        self.setWindowFlags(Qt.WindowType.Tool | Qt.WindowType.FramelessWindowHint)
        self.setAttribute(Qt.WidgetAttribute.WA_DontShowOnScreen, True)
        self.setWindowTitle("Pereklych hotkey")
        # Force native window creation; it stays hidden.
        self._hwnd = int(self.winId())

    def register(self) -> bool:
        if self._registered:
            return True
        try:
            ok = bool(
                _user32.RegisterHotKey(
                    wintypes.HWND(self._hwnd),
                    self._id,
                    self._modifiers,
                    self._key,
                )
            )
        except Exception:
            log.debug("RegisterHotKey failed", exc_info=True)
            ok = False
        self._registered = ok
        if ok:
            log.info("global hotkey registered (id=%s)", self._id)
        else:
            log.warning(
                "global hotkey Ctrl+Alt+P is already used by another application"
            )
        return ok

    def unregister(self) -> None:
        if not self._registered:
            return
        try:
            _user32.UnregisterHotKey(wintypes.HWND(self._hwnd), self._id)
        except Exception:
            log.debug("UnregisterHotKey failed", exc_info=True)
        self._registered = False

    def nativeEvent(self, event_type, message):  # noqa: N802 - Qt override
        try:
            if bytes(event_type) == b"windows_generic_MSG":
                msg = ctypes.cast(int(message), ctypes.POINTER(_MSG)).contents
                if msg.message == WM_HOTKEY and int(msg.wParam) == self._id:
                    self.triggered.emit()
                    return True, 0
        except Exception:
            log.debug("hotkey message parse failed", exc_info=True)
        return False, 0

    def closeEvent(self, event) -> None:  # noqa: N802 - Qt override
        self.unregister()
        super().closeEvent(event)
