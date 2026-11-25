"""Settings stored as JSON in %LOCALAPPDATA%\\Pereklych\\config.json."""

from __future__ import annotations

import json
import logging
import os
from dataclasses import asdict, dataclass, field, fields
from pathlib import Path
from typing import Any, Optional

log = logging.getLogger(__name__)

APP_NAME = "Pereklych"

#: SourceAppId substrings that are considered "our" players.
#: On this machine Edge reports ``MSEdge`` (no .exe, no path), other builds may
#: report ``msedge.exe`` or a full path - substring matching covers all of them.
DEFAULT_PATTERNS = ["msedge"]


def app_data_dir() -> Path:
    base = os.environ.get("LOCALAPPDATA") or str(Path.home() / "AppData" / "Local")
    path = Path(base) / APP_NAME
    path.mkdir(parents=True, exist_ok=True)
    return path


def logs_dir() -> Path:
    path = app_data_dir() / "logs"
    path.mkdir(parents=True, exist_ok=True)
    return path


def config_path() -> Path:
    return app_data_dir() / "config.json"


@dataclass
class Settings:
    source_patterns: list[str] = field(default_factory=lambda: list(DEFAULT_PATTERNS))
    #: "<aumid>#<ordinal>" of the session the user pinned in the tray menu.
    pinned_source: Optional[str] = None
    cover_icon: bool = False
    #: "app" = volume of the player process, "system" = master volume.
    volume_mode: str = "app"
    poll_interval: float = 2.0
    notifications: bool = False
    #: One-time hint about the corner trigger (reminder on first playback).
    hint_shown: bool = False
    #: How visible the bottom-right trigger zone is: hidden / soft / full.
    trigger_indicator: str = "hidden"

    def __post_init__(self) -> None:
        self.poll_interval = max(0.5, min(10.0, float(self.poll_interval)))
        if not isinstance(self.source_patterns, list) or not self.source_patterns:
            self.source_patterns = list(DEFAULT_PATTERNS)
        if self.trigger_indicator not in ("hidden", "soft", "full"):
            self.trigger_indicator = "hidden"


def load_settings(path: Path | None = None) -> Settings:
    path = path or config_path()
    if not path.exists():
        return Settings()
    try:
        raw = json.loads(path.read_text(encoding="utf-8-sig"))
    except Exception:
        log.warning("cannot read %s, using defaults", path, exc_info=True)
        return Settings()
    known = {f.name for f in fields(Settings)}
    data: dict[str, Any] = {k: v for k, v in raw.items() if k in known}
    try:
        return Settings(**data)
    except Exception:
        log.warning("invalid settings in %s, using defaults", path, exc_info=True)
        return Settings()


def save_settings(settings: Settings, path: Path | None = None) -> None:
    path = path or config_path()
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(
        json.dumps(asdict(settings), ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    os.replace(tmp, path)
