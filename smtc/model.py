"""Immutable data model shared between the WinRT worker and the tray UI.

Everything the tray renders is a :class:`TrackSnapshot`.  The tray thread only
ever reads the *latest* snapshot, so the async thread can replace it at any
time without locking (assigning a reference is atomic in CPython).
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from enum import IntEnum
from typing import Optional


class PlaybackStatus(IntEnum):
    """Mirror of GlobalSystemMediaTransportControlsSessionPlaybackStatus."""

    CLOSED = 0
    OPENED = 1
    CHANGING = 2
    STOPPED = 3
    PLAYING = 4
    PAUSED = 5


class RepeatMode(IntEnum):
    """Mirror of GlobalSystemMediaTransportControlsSessionAutoRepeatMode."""

    NONE = 0
    TRACK = 1
    LIST = 2


#: Friendly names for well known SourceAppIds.  SourceAppId for unpackaged
#: Win32 apps may be a bare exe name or a full path, hence the substring keys.
KNOWN_APPS: dict[str, str] = {
    "msedge": "Edge",
    "chrome": "Chrome",
    "firefox": "Firefox",
    "opera": "Opera",
    "brave": "Brave",
    "vivaldi": "Vivaldi",
    "yandex": "Яндекс.Браузер",
    "browser.exe": "Яндекс.Браузер",
    "spotify": "Spotify",
    "vlc": "VLC",
    "foobar2000": "foobar2000",
    "aimp": "AIMP",
    "musicbee": "MusicBee",
    "zunemusic": "Media Player",
    "wmplayer": "Windows Media Player",
    "deezer": "Deezer",
    "tidal": "TIDAL",
    "youtube": "YouTube",
    "telegram": "Telegram",
}


def app_label_for(aumid: str) -> str:
    """Human readable application name for a SourceAppId."""
    if not aumid:
        return "Неизвестно"
    base = os.path.basename(aumid.replace("\\", "/")).lower()
    for key, label in KNOWN_APPS.items():
        if key in base:
            return label
    if "!" in aumid:  # packaged app: "Package_8wekyb3d8bbwe!AppId"
        return aumid.split("!", 1)[1]
    stem = os.path.splitext(base)[0]
    return stem[:1].upper() + stem[1:] if stem else "Неизвестно"


def exe_stem(aumid: str) -> str:
    """``C:\\...\\msedge.exe`` -> ``msedge`` (used by the volume helper)."""
    if not aumid:
        return ""
    base = os.path.basename(aumid.replace("\\", "/")).lower()
    return os.path.splitext(base)[0]


def aumid_matches(aumid: str, patterns: list[str] | tuple[str, ...]) -> bool:
    """Case-insensitive substring match against the exe name *and* full id.

    Exact equality is not reliable: Windows reports a bare ``msedge.exe`` on
    some builds and the full path on others.
    """
    patterns = [p.strip().lower() for p in (patterns or []) if p and p.strip()]
    if not patterns:
        return True
    if not aumid:
        return False
    low = aumid.lower().replace("\\", "/")
    base = os.path.basename(low)
    return any(p in base or p in low for p in patterns)


@dataclass(frozen=True)
class SourceRef:
    """One selectable media session (for the "Источник" submenu)."""

    key: str
    label: str
    aumid: str


@dataclass(frozen=True)
class TrackSnapshot:
    #: "idle" (no usable session), "ready" (metadata available), "error".
    status: str = "idle"
    title: str = ""
    artist: str = ""
    album: str = ""
    app_label: str = ""
    detail: str = ""

    playback: int = int(PlaybackStatus.CLOSED)
    can_play: bool = False
    can_next: bool = False
    can_prev: bool = False
    can_stop: bool = False

    #: Playback timeline (seconds from the start of the track).
    position: float = 0.0
    duration: float = 0.0
    can_seek: bool = False

    shuffle_available: bool = False
    shuffle_active: Optional[bool] = None
    repeat_available: bool = False
    repeat_mode: Optional[int] = None

    sources: tuple[SourceRef, ...] = ()
    active_key: Optional[str] = None
    pinned_key: Optional[str] = None

    volume_percent: Optional[int] = None
    volume_muted: bool = False
    volume_label: str = ""

    cover: Optional[bytes] = None
    cover_key: str = ""

    @property
    def is_playing(self) -> bool:
        return self.playback in (
            int(PlaybackStatus.PLAYING),
            int(PlaybackStatus.CHANGING),
        )

    def signature(self) -> tuple:
        """Fields that, when changed, require a tray refresh (cover excluded)."""
        return (
            self.status,
            self.title,
            self.artist,
            self.album,
            self.app_label,
            self.detail,
            self.playback,
            self.can_play,
            self.can_next,
            self.can_prev,
            self.can_stop,
            round(self.position, 1),
            round(self.duration, 1),
            self.can_seek,
            self.shuffle_available,
            self.shuffle_active,
            self.repeat_available,
            self.repeat_mode,
            self.sources,
            self.active_key,
            self.pinned_key,
            self.volume_percent,
            self.volume_muted,
            self.volume_label,
            self.cover_key,
        )
