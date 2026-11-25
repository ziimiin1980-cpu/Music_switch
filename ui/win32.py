"""Win32 helpers for the dock panel: no-activate styles, DWM effects, taskbar
geometry and the fullscreen gate.

Everything here is a thin, defensive wrapper: a missing API (Windows 10, an old
build) or a weird window must never raise into the Qt event loop.
"""

from __future__ import annotations

import ctypes
import logging
import time
from ctypes import wintypes
from typing import Optional

log = logging.getLogger(__name__)

GWL_EXSTYLE = -20
WS_EX_TOOLWINDOW = 0x00000080
WS_EX_NOACTIVATE = 0x08000000

ABM_GETTASKBARPOS = 5
ABE_LEFT = 0
ABE_TOP = 1
ABE_RIGHT = 2
ABE_BOTTOM = 3

MONITOR_DEFAULTTONEAREST = 2

#: DWM attributes (Windows 11).  Ignored on Windows 10.
DWMWA_USE_IMMERSIVE_DARK_MODE = 20
DWMWA_BORDER_COLOR = 34
DWMWA_WINDOW_CORNER_PREFERENCE = 33
DWMWA_SYSTEMBACKDROP_TYPE = 38
DWMWA_COLOR_NONE = 0xFFFFFFFE
DWMWCP_ROUND = 2
DWMSBT_NONE = 1

#: QUERY_USER_NOTIFICATION_STATE
QUNS_BUSY = 2
QUNS_RUNNING_D3D_FULL_SCREEN = 3
QUNS_PRESENTATION_MODE = 4

_SHELL_WINDOW_CLASSES = {
    "Progman",
    "WorkerW",
    "Shell_TrayWnd",
    "Shell_SecondaryTrayWnd",
    "Windows.UI.Core.CoreWindow",
}

_HRESULT = ctypes.c_long
_LONG_PTR = ctypes.c_ssize_t
_ULONG_PTR = ctypes.c_size_t


class _APPBARDATA(ctypes.Structure):
    _fields_ = [
        ("cbSize", wintypes.DWORD),
        ("hWnd", wintypes.HWND),
        ("uCallbackMessage", wintypes.UINT),
        ("uEdge", wintypes.UINT),
        ("rc", wintypes.RECT),
        ("lParam", wintypes.LPARAM),
    ]


class _MONITORINFO(ctypes.Structure):
    _fields_ = [
        ("cbSize", wintypes.DWORD),
        ("rcMonitor", wintypes.RECT),
        ("rcWork", wintypes.RECT),
        ("dwFlags", wintypes.DWORD),
    ]


_user32 = ctypes.WinDLL("user32", use_last_error=True)
_shell32 = ctypes.WinDLL("shell32", use_last_error=True)
_dwmapi = ctypes.WinDLL("dwmapi", use_last_error=True)

_user32.GetWindowLongPtrW.argtypes = [wintypes.HWND, ctypes.c_int]
_user32.GetWindowLongPtrW.restype = _LONG_PTR
_user32.SetWindowLongPtrW.argtypes = [wintypes.HWND, ctypes.c_int, _LONG_PTR]
_user32.SetWindowLongPtrW.restype = _LONG_PTR
_user32.GetForegroundWindow.restype = wintypes.HWND
_user32.GetShellWindow.restype = wintypes.HWND
_user32.GetDesktopWindow.restype = wintypes.HWND
_user32.GetWindowRect.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.RECT)]
_user32.GetWindowRect.restype = wintypes.BOOL
_user32.IsWindowVisible.argtypes = [wintypes.HWND]
_user32.IsWindowVisible.restype = wintypes.BOOL
_user32.MonitorFromWindow.argtypes = [wintypes.HWND, wintypes.DWORD]
_user32.MonitorFromWindow.restype = wintypes.HANDLE
_user32.GetMonitorInfoW.argtypes = [wintypes.HANDLE, ctypes.POINTER(_MONITORINFO)]
_user32.GetMonitorInfoW.restype = wintypes.BOOL
_user32.GetClassNameW.argtypes = [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int]
_user32.GetClassNameW.restype = ctypes.c_int

_shell32.SHAppBarMessage.argtypes = [wintypes.DWORD, ctypes.POINTER(_APPBARDATA)]
_shell32.SHAppBarMessage.restype = _ULONG_PTR
_shell32.SHQueryUserNotificationState.argtypes = [ctypes.POINTER(ctypes.c_int)]
_shell32.SHQueryUserNotificationState.restype = _HRESULT

_dwmapi.DwmSetWindowAttribute.argtypes = [
    wintypes.HWND,
    wintypes.DWORD,
    ctypes.c_void_p,
    wintypes.DWORD,
]
_dwmapi.DwmSetWindowAttribute.restype = _HRESULT


# -- window styles ---------------------------------------------------------


def make_overlay(hwnd: int) -> bool:
    """Add WS_EX_NOACTIVATE | WS_EX_TOOLWINDOW to *hwnd*.

    ``NOACTIVATE`` keeps the keyboard focus in the app the user is typing in
    (clicking a button does not steal it), ``TOOLWINDOW`` hides the window from
    Alt+Tab and from the taskbar.
    """
    if not hwnd:
        return False
    try:
        current = int(_user32.GetWindowLongPtrW(wintypes.HWND(hwnd), GWL_EXSTYLE))
        wanted = current | WS_EX_NOACTIVATE | WS_EX_TOOLWINDOW
        if wanted == current:
            return True
        _user32.SetWindowLongPtrW(wintypes.HWND(hwnd), GWL_EXSTYLE, wanted)
        got = int(_user32.GetWindowLongPtrW(wintypes.HWND(hwnd), GWL_EXSTYLE))
        return bool(got & WS_EX_NOACTIVATE and got & WS_EX_TOOLWINDOW)
    except Exception:
        log.debug("cannot change the window styles", exc_info=True)
        return False


def apply_dwm_effects(hwnd: int) -> dict[str, bool]:
    """Windows 11 rounded corners / transient backdrop / dark mode.

    Each attribute is applied independently and failures are expected on
    Windows 10.  The panel always paints its own rounded background, so the
    acrylic result is a bonus, not a requirement.
    """
    result = {"round": False, "backdrop": False, "dark": False}
    if not hwnd:
        return result
    handle = wintypes.HWND(hwnd)

    def set_attr(attribute: int, value: int, size: int) -> bool:
        raw = ctypes.c_int(value)
        try:
            hr = int(
                _dwmapi.DwmSetWindowAttribute(
                    handle, attribute, ctypes.byref(raw), size
                )
            )
            return hr == 0
        except Exception:
            log.debug("DwmSetWindowAttribute(%d) failed", attribute, exc_info=True)
            return False

    result["round"] = set_attr(DWMWA_WINDOW_CORNER_PREFERENCE, DWMWCP_ROUND, 4)
    # Do not use the system backdrop: the panel is a layered window with its
    # own shadow margin, and the acrylic material fills that margin with a
    # visible grey frame around the card.  NONE explicitly clears any inherited
    # material; the card paints its own blurred-cover background.
    result["backdrop"] = set_attr(DWMWA_SYSTEMBACKDROP_TYPE, DWMSBT_NONE, 4)
    # Without this DWM draws a 1 px frame around the whole window, which is
    # visible as an outline around the transparent shadow margin.
    result["border"] = set_attr(DWMWA_BORDER_COLOR, DWMWA_COLOR_NONE, 4)
    result["dark"] = set_attr(DWMWA_USE_IMMERSIVE_DARK_MODE, 1, 4)
    return result


def remove_window_border(hwnd: int) -> bool:
    """Remove the 1 px DWM frame (used by the click-through zone indicator)."""
    if not hwnd:
        return False
    raw = ctypes.c_int(DWMWA_COLOR_NONE)
    try:
        hr = int(
            _dwmapi.DwmSetWindowAttribute(
                wintypes.HWND(hwnd),
                DWMWA_BORDER_COLOR,
                ctypes.byref(raw),
                ctypes.sizeof(raw),
            )
        )
        return hr == 0
    except Exception:
        log.debug("cannot remove the DWM window border", exc_info=True)
        return False


def accent_color() -> tuple[int, int, int]:
    """Windows accent color as ``(r, g, b)``.

    ``HKCU\\...\\DWM\\AccentColor`` is stored as 0xAABBGGRR.  The default is the
    familiar light blue used by the panel before the redesign.
    """
    try:
        import winreg

        with winreg.OpenKey(
            winreg.HKEY_CURRENT_USER, r"Software\Microsoft\Windows\DWM"
        ) as key:
            value, _ = winreg.QueryValueEx(key, "AccentColor")
        value = int(value) & 0xFFFFFFFF
        return value & 0xFF, (value >> 8) & 0xFF, (value >> 16) & 0xFF
    except Exception:
        return (124, 196, 255)


# -- taskbar ---------------------------------------------------------------


def taskbar_bounds() -> Optional[tuple[int, int, int, int, int]]:
    """``(left, top, right, bottom, edge)`` of the primary taskbar, physical px."""
    data = _APPBARDATA()
    data.cbSize = ctypes.sizeof(_APPBARDATA)
    try:
        if not _shell32.SHAppBarMessage(ABM_GETTASKBARPOS, ctypes.byref(data)):
            return None
        rect = data.rc
        return (rect.left, rect.top, rect.right, rect.bottom, int(data.uEdge))
    except Exception:
        log.debug("SHAppBarMessage failed", exc_info=True)
        return None


# -- fullscreen gate -------------------------------------------------------


def _class_name(hwnd: int) -> str:
    buffer = ctypes.create_unicode_buffer(256)
    try:
        length = _user32.GetClassNameW(wintypes.HWND(hwnd), buffer, 256)
        return buffer.value if length else ""
    except Exception:
        return ""


def _notification_state() -> Optional[int]:
    state = ctypes.c_int(0)
    try:
        hr = int(_shell32.SHQueryUserNotificationState(ctypes.byref(state)))
        if hr != 0:
            return None
        return int(state.value)
    except Exception:
        return None


def foreground_covers_monitor(own_hwnds: set[int] | frozenset[int] = frozenset()) -> bool:
    """True when the foreground window is a real fullscreen window."""
    try:
        hwnd = int(_user32.GetForegroundWindow() or 0)
    except Exception:
        return False
    if not hwnd or hwnd in own_hwnds:
        return False
    try:
        if hwnd in (
            int(_user32.GetShellWindow() or 0),
            int(_user32.GetDesktopWindow() or 0),
        ):
            return False
        if not _user32.IsWindowVisible(wintypes.HWND(hwnd)):
            return False
        if _class_name(hwnd) in _SHELL_WINDOW_CLASSES:
            return False
        rect = wintypes.RECT()
        if not _user32.GetWindowRect(wintypes.HWND(hwnd), ctypes.byref(rect)):
            return False
        monitor = _user32.MonitorFromWindow(
            wintypes.HWND(hwnd), MONITOR_DEFAULTTONEAREST
        )
        if not monitor:
            return False
        info = _MONITORINFO()
        info.cbSize = ctypes.sizeof(_MONITORINFO)
        if not _user32.GetMonitorInfoW(monitor, ctypes.byref(info)):
            return False
        screen = info.rcMonitor
        # A fullscreen window covers the whole monitor; 2 px of slack absorbs
        # borderless-window rounding.
        return (
            rect.left <= screen.left + 2
            and rect.top <= screen.top + 2
            and rect.right >= screen.right - 2
            and rect.bottom >= screen.bottom - 2
        )
    except Exception:
        log.debug("fullscreen detection failed", exc_info=True)
        return False


class FullscreenGate:
    """Cached "do not show the panel" decision (checked at most twice a second)."""

    INTERVAL = 0.5

    def __init__(self) -> None:
        self._last_check = 0.0
        self._value = False

    def blocked(self, own_hwnds: set[int] | frozenset[int] = frozenset()) -> bool:
        now = time.monotonic()
        if now - self._last_check < self.INTERVAL:
            return self._value
        self._last_check = now
        state = None
        try:
            state = _notification_state()
            busy = state in (QUNS_BUSY, QUNS_RUNNING_D3D_FULL_SCREEN, QUNS_PRESENTATION_MODE)
            value = busy or foreground_covers_monitor(own_hwnds)
        except Exception:
            value = False
        if value != self._value:
            log.info(
                "fullscreen gate: %s (notification state=%s)",
                "blocked" if value else "clear",
                state,
            )
        self._value = value
        return value
