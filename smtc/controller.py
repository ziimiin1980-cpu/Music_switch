"""GSMTC controller: owns the WinRT manager, the selected session and events.

All methods run on the asyncio loop thread.  Everything that can fail (a
browser closing mid-call, a session disappearing, a metadata read timing out)
is caught here and turned into a new snapshot - the UI never sees an exception.
"""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import replace
from typing import Callable, Optional

from winrt.windows.media.control import (
    GlobalSystemMediaTransportControlsSessionManager as SessionManager,
)
from winrt.windows.media.control import (
    GlobalSystemMediaTransportControlsSessionPlaybackStatus as WinStatus,
)
from winrt.windows.storage.streams import DataReader, InputStreamOptions

from audio.volume import VolumeController

from .model import (
    PlaybackStatus,
    SourceRef,
    TrackSnapshot,
    aumid_matches,
    app_label_for,
)

log = logging.getLogger(__name__)

REQUEST_TIMEOUT = 5.0
PROPS_TIMEOUT = 1.5
ACTION_TIMEOUT = 2.5
COVER_TIMEOUT = 3.0
MAX_COVER_BYTES = 8 * 1024 * 1024
EVENT_DEBOUNCE = 0.12


class MediaController:
    def __init__(
        self,
        loop: asyncio.AbstractEventLoop,
        settings,
        on_change: Callable[[TrackSnapshot], None],
        on_settings_changed: Optional[Callable[[], None]] = None,
    ) -> None:
        self._loop = loop
        self._settings = settings
        self._on_change = on_change
        self._on_settings_changed = on_settings_changed

        self._manager = None
        self._manager_tokens: list[tuple[str, object]] = []
        self._manager_handlers: list = []

        self._session = None
        self._session_key: Optional[str] = None
        self._session_tokens: list[tuple[str, object]] = []
        self._session_handlers: list = []

        self._wake = asyncio.Event()
        self._lock: Optional[asyncio.Lock] = None
        self._task: Optional[asyncio.Task] = None
        self._stopping = False

        self._snapshot = TrackSnapshot()
        self._cover_key = ""
        self._cover_bytes: Optional[bytes] = None

        self._volume = VolumeController(settings.source_patterns, settings.volume_mode)
        self._volume.set_on_update(self._wakeup_from_event)
        self._idle_streak = 0
        self._closed_streak = 0
        self._idle_log_at = 0.0

    # -- public API --------------------------------------------------------

    @property
    def snapshot(self) -> TrackSnapshot:
        return self._snapshot

    async def start(self) -> None:
        self._lock = asyncio.Lock()
        self._stopping = False
        self._task = asyncio.create_task(self._run(), name="smtc-runner")
        log.info("controller started")

    async def stop(self) -> None:
        self._stopping = True
        if self._task is not None:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            except Exception:
                log.debug("controller task ended with an error", exc_info=True)
            self._task = None
        self._detach_session()
        self._drop_manager()
        self._volume.close()
        self._wake.set()
        log.info("controller stopped")

    async def pin_source(self, key: Optional[str]) -> None:
        """Pin (or release) the session chosen by the user. Runs on the loop."""
        self._settings.pinned_source = key
        if self._on_settings_changed:
            try:
                self._on_settings_changed()
            except Exception:
                log.warning("cannot persist pinned source", exc_info=True)
        self._wake.set()

    async def set_volume_mode(self, mode: str) -> None:
        self._settings.volume_mode = mode
        self._volume.mode = mode
        try:
            # Refresh the cached label/percent immediately, not after the
            # usual 4 s volume poll interval.
            self._volume.poll("", force=True)
        except Exception:
            log.debug("cannot refresh the volume after a mode change", exc_info=True)
        if self._on_settings_changed:
            try:
                self._on_settings_changed()
            except Exception:
                log.warning("cannot persist volume mode", exc_info=True)
        self._wake.set()

    # -- actions (called through run_coroutine_threadsafe) -----------------

    async def toggle_play_pause(self) -> bool:
        return await self._call_session("try_toggle_play_pause_async")

    async def skip_next(self) -> bool:
        return await self._call_session("try_skip_next_async")

    async def skip_previous(self) -> bool:
        return await self._call_session("try_skip_previous_async")

    async def seek(self, seconds: float) -> bool:
        # pywinrt accepts the WinRT TimeSpan here as int64 ticks (100 ns).
        ticks = int(max(0.0, float(seconds)) * 10_000_000)
        log.debug("seek -> %.1f s", float(seconds))
        return await self._call_session("try_change_playback_position_async", ticks)

    async def set_shuffle(self, active: bool) -> bool:
        return await self._call_session("try_change_shuffle_active_async", bool(active))

    async def set_repeat(self, mode: int) -> bool:
        return await self._call_session("try_change_auto_repeat_mode_async", int(mode))

    async def _call_session(self, method: str, *args) -> bool:
        session = self._session
        if session is None:
            self._wake_now()
            return False
        fn = getattr(session, method, None)
        if fn is None:
            log.debug("session has no %s", method)
            return False
        try:
            result = await asyncio.wait_for(fn(*args), ACTION_TIMEOUT)
            ok = bool(result)
        except asyncio.TimeoutError:
            log.warning("%s timed out", method)
            ok = False
        except Exception:
            log.warning("%s failed", method, exc_info=True)
            ok = False
        log.debug("action %s -> %s", method, ok)
        self._wake_now(delay=0.25)
        return ok

    # -- volume (sync, but always called from the loop thread) -------------

    def volume_step(self, delta: int) -> bool:
        try:
            self._volume.step(delta)
            ok = True
        except Exception:
            log.warning("volume step failed", exc_info=True)
            ok = False
        self._wake_now(delay=0.8)
        return ok

    def volume_set_muted(self, muted: bool) -> bool:
        try:
            self._volume.set_muted(bool(muted))
            ok = True
        except Exception:
            log.warning("volume mute failed", exc_info=True)
            ok = False
        self._wake_now(delay=0.8)
        return ok

    def volume_set_percent(self, percent: int) -> bool:
        try:
            self._volume.set_percent(int(percent))
            ok = True
        except Exception:
            log.warning("volume set failed", exc_info=True)
            ok = False
        self._wake_now(delay=0.8)
        return ok

    # -- loop plumbing -----------------------------------------------------

    def _wakeup_from_event(self) -> None:
        """Called by WinRT event handlers (arbitrary thread)."""
        try:
            self._loop.call_soon_threadsafe(self._wake.set)
        except RuntimeError:
            pass

    def _wake_now(self, delay: float = 0.0) -> None:
        """Called from the loop thread."""
        self._wake.set()
        if delay:
            try:
                self._loop.call_later(delay, self._wake.set)
            except RuntimeError:
                pass

    def _new_manager_handler(self, tag: str):
        def handler(sender, args):
            self._wakeup_from_event()

        handler.__name__ = f"on_{tag}"
        self._manager_handlers.append(handler)
        return handler

    def _new_session_handler(self, tag: str):
        def handler(sender, args):
            self._wakeup_from_event()

        handler.__name__ = f"on_{tag}"
        self._session_handlers.append(handler)
        return handler

    async def _run(self) -> None:
        backoff = 1.0
        while not self._stopping:
            try:
                await self._ensure_manager()
                await self._refresh()
                backoff = 1.0
            except asyncio.CancelledError:
                raise
            except OSError as exc:
                hr = getattr(exc, "winerror", None)
                log.warning(
                    "SMTC unavailable%s: %s",
                    f" (0x{hr & 0xFFFFFFFF:08X})" if hr else "",
                    exc,
                )
                self._drop_manager()
                self._publish(
                    TrackSnapshot(
                        status="idle",
                        detail="Нет доступа к медиа-сессиям Windows",
                        pinned_key=self._settings.pinned_source,
                    )
                )
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2.0, 30.0)
                continue
            except Exception:
                log.exception("refresh failed")
                await asyncio.sleep(2.0)
                continue

            self._wake.clear()
            try:
                await asyncio.wait_for(self._wake.wait(), timeout=self._settings.poll_interval)
            except asyncio.TimeoutError:
                pass
            except asyncio.CancelledError:
                raise
            else:
                await asyncio.sleep(EVENT_DEBOUNCE)

    async def _ensure_manager(self) -> None:
        if self._manager is not None:
            return
        manager = await asyncio.wait_for(SessionManager.request_async(), REQUEST_TIMEOUT)
        if manager is None:
            raise OSError("session manager is unavailable")
        self._manager = manager
        self._manager_tokens.append(
            ("sessions", manager.add_sessions_changed(self._new_manager_handler("sessions")))
        )
        self._manager_tokens.append(
            ("current", manager.add_current_session_changed(self._new_manager_handler("current")))
        )
        log.info("media session manager ready")

    def _drop_manager(self) -> None:
        self._detach_session()
        if self._manager is not None:
            for kind, token in self._manager_tokens:
                try:
                    if kind == "sessions":
                        self._manager.remove_sessions_changed(token)
                    else:
                        self._manager.remove_current_session_changed(token)
                except Exception:
                    log.debug("cannot unsubscribe manager handler", exc_info=True)
        self._manager_tokens.clear()
        self._manager_handlers.clear()
        self._manager = None

    def _attach_session(self, session, key: str) -> None:
        self._detach_session()
        try:
            token = session.add_media_properties_changed(self._new_session_handler("properties"))
            self._session_tokens.append(("properties", token))
            token = session.add_playback_info_changed(self._new_session_handler("playback"))
            self._session_tokens.append(("playback", token))
        except Exception:
            log.warning("cannot subscribe to the session", exc_info=True)
        self._session = session
        self._session_key = key
        self._cover_key = ""
        self._cover_bytes = None
        log.info("attached to session %s", key)

    def _detach_session(self) -> None:
        if self._session is not None:
            for kind, token in self._session_tokens:
                try:
                    if kind == "properties":
                        self._session.remove_media_properties_changed(token)
                    else:
                        self._session.remove_playback_info_changed(token)
                except Exception:
                    log.debug("cannot unsubscribe session handler", exc_info=True)
        self._session_tokens.clear()
        self._session_handlers.clear()
        self._session = None
        self._session_key = None

    # -- the actual refresh ------------------------------------------------

    async def _refresh(self) -> None:
        assert self._lock is not None
        async with self._lock:
            if self._stopping or self._manager is None:
                return
            started = time.monotonic()

            entries = self._enumerate()
            allowed = [
                entry
                for entry in entries
                if aumid_matches(entry[1], self._settings.source_patterns)
            ]
            sources = self._build_sources(allowed)
            chosen = self._select(allowed)

            if chosen is None:
                self._idle_streak += 1
                self._detach_session()
                self._publish(
                    TrackSnapshot(
                        status="idle",
                        detail=(
                            "Edge не запущен или нет активной вкладки с медиа"
                            if not allowed
                            else "Источник не отвечает"
                        ),
                        sources=sources,
                        pinned_key=self._settings.pinned_source,
                    )
                )
                now = time.monotonic()
                if now - self._idle_log_at > 30.0:
                    self._idle_log_at = now
                    log.info(
                        "no usable session (sessions=%d, allowed=%d)",
                        len(entries),
                        len(allowed),
                    )
                if allowed and self._idle_streak in (5, 30, 120):
                    # A session exists but cannot be used: the manager may hold
                    # a stale list (a well known GSMTC quirk). Re-acquire it.
                    log.info("re-acquiring the session manager")
                    self._drop_manager()
                log.debug("refresh (idle) %.1f ms", (time.monotonic() - started) * 1000)
                return

            self._idle_streak = 0
            session, aumid, key, info, status = chosen
            if key != self._session_key:
                self._attach_session(session, key)

            if status == int(WinStatus.CLOSED):
                self._closed_streak += 1
                if self._closed_streak >= 10:
                    log.info("session is stuck in CLOSED, re-acquiring the manager")
                    self._closed_streak = 0
                    self._drop_manager()
                    return
            else:
                self._closed_streak = 0

            props = await self._read_properties(session)
            title = (props.title or "").strip() if props else ""
            artist = (props.artist or "").strip() if props else ""
            album = (props.album_title or "").strip() if props else ""
            cover_key = f"{title}\x1f{artist}\x1f{album}"
            cover = self._cover_bytes if cover_key == self._cover_key else None

            if props is not None and props.thumbnail is not None and cover_key != self._cover_key:
                data = await self._read_cover(props.thumbnail)
                if data:
                    self._cover_key = cover_key
                    self._cover_bytes = data
                    cover = data
            elif props is not None and props.thumbnail is None:
                self._cover_key = ""
                self._cover_bytes = None
                cover = None

            controls = getattr(info, "controls", None)
            position, duration = self._read_timeline(session)
            can_seek = bool(
                controls is not None
                and getattr(controls, "is_playback_position_enabled", False)
                and duration > 0.0
            )
            shuffle_active = _optional_bool(getattr(info, "is_shuffle_active", None))
            repeat_mode = _optional_int(getattr(info, "auto_repeat_mode", None))

            volume = None
            try:
                self._volume.poll(aumid)
                volume = self._volume.snapshot()
            except Exception:
                log.debug("volume snapshot failed", exc_info=True)

            snapshot = TrackSnapshot(
                status="ready",
                title=title,
                artist=artist,
                album=album,
                app_label=app_label_for(aumid),
                playback=status,
                can_play=bool(
                    controls is None
                    or controls.is_play_pause_toggle_enabled
                    or controls.is_play_enabled
                    or controls.is_pause_enabled
                ),
                can_next=bool(controls is not None and controls.is_next_enabled),
                can_prev=bool(controls is not None and controls.is_previous_enabled),
                can_stop=bool(controls is not None and controls.is_stop_enabled),
                position=position,
                duration=duration,
                can_seek=can_seek,
                shuffle_available=bool(controls is not None and controls.is_shuffle_enabled),
                shuffle_active=shuffle_active,
                repeat_available=bool(controls is not None and controls.is_repeat_enabled),
                repeat_mode=repeat_mode,
                sources=sources,
                active_key=key,
                pinned_key=self._settings.pinned_source,
                volume_percent=volume[0] if volume else None,
                volume_muted=volume[1] if volume else False,
                volume_label=volume[2] if volume else "",
                cover=cover,
                cover_key=self._cover_key if cover else "",
            )
            self._publish(snapshot)
            log.debug("refresh %.1f ms", (time.monotonic() - started) * 1000)

    def _enumerate(self) -> list[tuple[object, str, str]]:
        assert self._manager is not None
        try:
            sessions = list(self._manager.get_sessions())
        except Exception:
            log.debug("get_sessions failed", exc_info=True)
            return []
        entries: list[tuple[object, str, str]] = []
        ordinals: dict[str, int] = {}
        for session in sessions:
            try:
                aumid = session.source_app_user_model_id or ""
            except Exception:
                aumid = ""
            index = ordinals.get(aumid, 0)
            ordinals[aumid] = index + 1
            entries.append((session, aumid, f"{aumid}#{index}"))
        return entries

    def _build_sources(self, allowed: list[tuple[object, str, str]]) -> tuple[SourceRef, ...]:
        counts: dict[str, int] = {}
        labels: dict[str, str] = {}
        for _, aumid, key in allowed:
            counts[aumid] = counts.get(aumid, 0) + 1
            labels[key] = app_label_for(aumid)
        seen: dict[str, int] = {}
        result = []
        for _, aumid, key in allowed:
            seen[aumid] = seen.get(aumid, 0) + 1
            label = labels[key]
            if counts[aumid] > 1:
                label = f"{label} ({seen[aumid]})"
            result.append(SourceRef(key=key, label=label, aumid=aumid))
        return tuple(result)

    def _select(self, allowed):
        """Pick a session: pinned > current > playing > paused > anything."""
        if not allowed:
            return None
        pinned = self._settings.pinned_source
        has_pinned = any(key == pinned for _, _, key in allowed)
        current_aumid = ""
        try:
            current = self._manager.get_current_session() if self._manager else None
            if current is not None:
                current_aumid = current.source_app_user_model_id or ""
        except Exception:
            current = None

        scored = []
        for session, aumid, key in allowed:
            try:
                info = session.get_playback_info()
                status = int(info.playback_status)
            except Exception:
                continue
            if status == int(WinStatus.CLOSED):
                score = 5
            elif status == int(WinStatus.PLAYING):
                score = 2
            elif status in (int(WinStatus.PAUSED), int(WinStatus.OPENED), int(WinStatus.CHANGING)):
                score = 3
            else:
                score = 4
            if has_pinned and key == pinned:
                score = 0
            elif aumid == current_aumid and status == int(WinStatus.PLAYING):
                score = 1
            if has_pinned and key != pinned:
                score += 10
            scored.append((score, key, session, aumid, info, status))

        if not scored:
            return None
        scored.sort(key=lambda item: item[0])
        _, key, session, aumid, info, status = scored[0]
        return session, aumid, key, info, status

    async def _read_properties(self, session):
        try:
            return await asyncio.wait_for(
                session.try_get_media_properties_async(), PROPS_TIMEOUT
            )
        except asyncio.TimeoutError:
            log.debug("media properties timed out")
        except Exception:
            log.debug("media properties failed", exc_info=True)
        return None

    @staticmethod
    def _read_timeline(session) -> tuple[float, float]:
        """(position, duration) in seconds; (0, 0) when the player has none."""
        try:
            timeline = session.get_timeline_properties()
        except Exception:
            return 0.0, 0.0
        if timeline is None:
            return 0.0, 0.0
        try:
            start = float(timeline.start_time.total_seconds())
            end = float(timeline.end_time.total_seconds())
            raw = float(timeline.position.total_seconds())
            duration = max(0.0, end - start)
            position = max(0.0, min(raw - start, duration)) if duration > 0 else 0.0
            return position, duration
        except Exception:
            log.debug("timeline read failed", exc_info=True)
            return 0.0, 0.0

    async def _read_cover(self, ref) -> Optional[bytes]:
        stream = None
        reader = None
        try:
            stream = await asyncio.wait_for(ref.open_read_async(), COVER_TIMEOUT)
            if stream is None:
                return None
            size = int(stream.size)
            if size <= 0 or size > MAX_COVER_BYTES:
                return None
            reader = DataReader(stream)
            reader.input_stream_options = InputStreamOptions.PARTIAL
            await asyncio.wait_for(reader.load_async(size), COVER_TIMEOUT)
            count = int(reader.unconsumed_buffer_length) or size
            buffer = reader.read_buffer(count)
            return bytes(memoryview(buffer))
        except asyncio.TimeoutError:
            log.debug("cover read timed out")
        except Exception:
            log.debug("cover read failed", exc_info=True)
        finally:
            for closable in (reader, stream):
                try:
                    if closable is not None:
                        closable.close()
                except Exception:
                    pass
        return None

    def _publish(self, snapshot: TrackSnapshot) -> None:
        previous = self._snapshot
        if snapshot.signature() == previous.signature():
            return
        self._snapshot = snapshot
        media_changed = (
            replace(snapshot, position=0.0).signature()
            != replace(previous, position=0.0).signature()
        )
        if media_changed:
            log.info(
                "state: %s | %s - %s | %s | vol=%s%%",
                snapshot.status,
                snapshot.artist,
                snapshot.title,
                PlaybackStatus(snapshot.playback).name,
                snapshot.volume_percent,
            )
        try:
            self._on_change(snapshot)
        except Exception:
            log.exception("state callback failed")


def _optional_bool(value) -> Optional[bool]:
    if value is None:
        return None
    try:
        return bool(value)
    except Exception:
        return None


def _optional_int(value) -> Optional[int]:
    if value is None:
        return None
    try:
        return int(value)
    except Exception:
        return None
