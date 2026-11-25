"""Volume of the player process (or the system) via Core Audio.

Two hard rules learned the hard way:

1. Windows has no per-track volume.  The closest thing to "volume of the song"
   is the volume of the application that plays it (all tabs of Edge share one
   audio session).  If the player has no audio session, we fall back to the
   master volume, and the menu says "Система" so the change is never a
   surprise.
2. Core Audio is COM and COM can block indefinitely (device changes, a stuck
   audio service).  Therefore *all* Core Audio calls live in a separate worker
   thread; the asyncio loop only reads a cached value and never waits.  A hung
   worker degrades to "volume unavailable" instead of freezing the whole app.
"""

from __future__ import annotations

import ctypes
import logging
import os
import queue
import threading
import time
from ctypes import wintypes
from typing import Callable, Optional

from smtc.model import app_label_for, exe_stem

log = logging.getLogger(__name__)

_PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
_SESSION_STATE_ACTIVE = 1
_STUCK_AFTER = 10.0
_POLL_INTERVAL = 4.0


def _co_init() -> None:
    """Initialize COM for this thread if nobody did it yet.

    pywinrt puts its threads into an MTA.  Here we run in our own thread, so
    comtypes initializes it as STA - both work as long as we stay on one
    thread.
    """
    try:
        import comtypes

        comtypes.CoInitialize()
    except Exception:
        pass


def _process_exe_name(pid: int) -> str:
    if not pid:
        return ""
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    handle = kernel32.OpenProcess(_PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
    if not handle:
        return ""
    try:
        size = wintypes.DWORD(32768)
        buffer = ctypes.create_unicode_buffer(size.value)
        if kernel32.QueryFullProcessImageNameW(handle, 0, buffer, ctypes.byref(size)):
            return os.path.basename(buffer.value)
        return ""
    except Exception:
        return ""
    finally:
        kernel32.CloseHandle(handle)


class VolumeController:
    """Non-blocking facade over Core Audio.

    Loop-thread API: :meth:`poll`, :meth:`snapshot`, :meth:`step`,
    :meth:`set_muted`, :meth:`close`.  None of them block.
    """

    def __init__(
        self,
        patterns: list[str] | None,
        mode: str = "app",
        on_update: Optional[Callable[[], None]] = None,
    ) -> None:
        self.patterns = [p.strip().lower() for p in (patterns or []) if p and p.strip()]
        self.mode = mode
        self._on_update = on_update

        self._queue: "queue.SimpleQueue" = queue.SimpleQueue()
        self._thread: Optional[threading.Thread] = None
        self._guard = threading.Lock()
        self._closing = False
        self._inflight = False
        self._inflight_since = 0.0
        self._stuck = False
        self._available: Optional[bool] = None
        self._speakers = None

        self._cache: Optional[tuple[int, bool, str]] = None
        self._active_aumid = ""
        self._last_poll = 0.0

    # -- loop-thread API ---------------------------------------------------

    def set_on_update(self, callback: Optional[Callable[[], None]]) -> None:
        """Called from the worker thread after the cache changes."""
        self._on_update = callback

    def snapshot(self) -> Optional[tuple[int, bool, str]]:
        """Last known (percent, muted, label); never blocks."""
        return self._cache

    def poll(self, aumid: str = "", force: bool = False) -> None:
        """Ask the worker to refresh the cached value (throttled)."""
        if aumid and aumid != self._active_aumid:
            self._active_aumid = aumid
            force = True
        now = time.monotonic()
        if self._inflight:
            if now - self._inflight_since > _STUCK_AFTER and not self._stuck:
                self._stuck = True
                log.warning(
                    "Core Audio worker is not responding, volume controls disabled"
                )
            return
        if self._stuck:
            return
        if not force and now - self._last_poll < _POLL_INTERVAL:
            return
        self._last_poll = now
        self._submit(self._job_snapshot, aumid or self._active_aumid)

    def step(self, delta: int) -> None:
        if self._stuck:
            return
        self._submit(self._job_step, int(delta))

    def set_muted(self, muted: bool) -> None:
        if self._stuck:
            return
        self._submit(self._job_mute, bool(muted))

    def set_percent(self, percent: int) -> None:
        if self._stuck:
            return
        self._submit(self._job_set_percent, int(percent))

    def close(self) -> None:
        self._closing = True
        if self._thread is not None:
            self._queue.put(None)
            # The thread is a daemon and may be stuck inside a COM call: never
            # block the shutdown on it.
            self._thread.join(0.2)
            self._thread = None

    # -- worker plumbing ---------------------------------------------------

    def _submit(self, job: Callable, *args) -> None:
        self._ensure_worker()
        self._inflight = True
        self._inflight_since = time.monotonic()
        self._queue.put((job, args))

    def _ensure_worker(self) -> None:
        if self._thread is not None:
            return
        with self._guard:
            if self._thread is not None:
                return
            thread = threading.Thread(
                target=self._worker, name="perekl-volume", daemon=True
            )
            self._thread = thread
            thread.start()

    def _worker(self) -> None:
        while True:
            item = self._queue.get()
            if item is None:
                return
            job, args = item
            if self._closing:
                continue
            try:
                result = job(*args)
            except Exception:
                log.debug("volume job failed", exc_info=True)
                result = None
            self._inflight = False
            if result is not None:
                self._cache = result
            callback = self._on_update
            if callback is not None:
                try:
                    callback()
                except Exception:
                    log.debug("volume update callback failed", exc_info=True)

    # -- jobs (worker thread only) -----------------------------------------

    def _job_snapshot(self, aumid: str):
        return self._read(aumid)

    def _job_step(self, delta: int):
        current = self._read(self._active_aumid)
        if current is None:
            return None
        self._write_percent(current[0] + delta)
        return self._read(self._active_aumid)

    def _job_set_percent(self, percent: int):
        self._write_percent(percent)
        return self._read(self._active_aumid)

    def _job_mute(self, muted: bool):
        self._write_mute(muted)
        return self._read(self._active_aumid)

    # -- Core Audio (worker thread only) -----------------------------------

    def _ok(self) -> bool:
        if self._available is None:
            try:
                from pycaw.pycaw import AudioUtilities  # noqa: F401

                _co_init()
                self._available = True
            except Exception as exc:
                self._available = False
                log.warning("pycaw is unavailable: %s", exc)
        return bool(self._available)

    def _app_targets(self, aumid: str) -> list[tuple[object, str, int]]:
        from pycaw.pycaw import AudioUtilities

        result: list[tuple[object, str, int]] = []
        try:
            sessions = AudioUtilities.GetAllSessions()
        except Exception:
            log.debug("cannot enumerate audio sessions", exc_info=True)
            return result

        stem = exe_stem(aumid)
        for session in sessions:
            volume = getattr(session, "SimpleAudioVolume", None)
            if volume is None:
                continue
            name = _process_exe_name(int(getattr(session, "ProcessId", 0) or 0))
            if not name:
                continue
            low = name.lower()
            matched = any(p in low for p in self.patterns)
            if stem and stem in low:
                matched = True
            if not matched:
                continue
            state = int(getattr(session, "State", 0) or 0)
            result.append((volume, name, state))

        result.sort(key=lambda item: 0 if item[2] == _SESSION_STATE_ACTIVE else 1)
        return result

    def _endpoint(self):
        from pycaw.pycaw import AudioUtilities

        if self._speakers is None:
            self._speakers = AudioUtilities.GetSpeakers()
        return self._speakers.EndpointVolume

    def _label(self, aumid: str) -> str:
        return app_label_for(aumid) if aumid else "Приложение"

    def _read(self, aumid: str) -> Optional[tuple[int, bool, str]]:
        if not self._ok():
            return None
        try:
            if self.mode != "system":
                targets = self._app_targets(aumid)
                if targets:
                    volume = targets[0][0]
                    percent = int(round(float(volume.GetMasterVolume()) * 100.0))
                    muted = bool(volume.GetMute())
                    return max(0, min(100, percent)), muted, self._label(aumid)
            endpoint = self._endpoint()
            percent = int(round(float(endpoint.GetMasterVolumeLevelScalar()) * 100.0))
            return max(0, min(100, percent)), bool(endpoint.GetMute()), "Система"
        except Exception:
            log.debug("cannot read volume", exc_info=True)
            self._speakers = None
        return None

    def _targets(self) -> list:
        if self.mode == "system":
            return []
        return [volume for volume, _name, _state in self._app_targets(self._active_aumid)]

    def _write_percent(self, percent: int) -> None:
        level = max(0.0, min(1.0, float(percent) / 100.0))
        try:
            targets = self._targets()
            if targets:
                for volume in targets:
                    volume.SetMasterVolume(level, None)
            else:
                self._endpoint().SetMasterVolumeLevelScalar(level, None)
        except Exception:
            log.debug("cannot set volume", exc_info=True)
            self._speakers = None

    def _write_mute(self, muted: bool) -> None:
        try:
            targets = self._targets()
            if targets:
                for volume in targets:
                    volume.SetMute(bool(muted), None)
            else:
                self._endpoint().SetMute(bool(muted), None)
        except Exception:
            log.debug("cannot set mute", exc_info=True)
            self._speakers = None
