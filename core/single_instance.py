"""Named mutex: a second copy must not create a second tray icon."""

from __future__ import annotations

import ctypes
import logging
from ctypes import wintypes

log = logging.getLogger(__name__)

_ERROR_ALREADY_EXISTS = 183


class SingleInstance:
    def __init__(self, name: str = "Local\\Pereklych") -> None:
        self._handle = None
        self.already_running = False
        try:
            kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
            kernel32.CreateMutexW.argtypes = [wintypes.LPVOID, wintypes.BOOL, wintypes.LPCWSTR]
            kernel32.CreateMutexW.restype = wintypes.HANDLE
            self._handle = kernel32.CreateMutexW(None, False, name)
            self.already_running = ctypes.get_last_error() == _ERROR_ALREADY_EXISTS
            if not self._handle:
                log.warning("CreateMutexW failed, continuing without single-instance check")
        except Exception:
            log.warning("single-instance check unavailable", exc_info=True)

    def close(self) -> None:
        if self._handle:
            try:
                ctypes.WinDLL("kernel32").CloseHandle(self._handle)
            except Exception:
                pass
            self._handle = None
