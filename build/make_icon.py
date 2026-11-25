"""Regenerate build/app.ico from the same drawing code the tray uses."""

from __future__ import annotations

import sys
from pathlib import Path

from PIL import Image

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from ui.icons import base_icon  # noqa: E402

OUT = Path(__file__).resolve().parent / "app.ico"


def main() -> None:
    image = base_icon(256, playing=True).convert("RGBA")
    image.save(
        OUT,
        format="ICO",
        sizes=[(16, 16), (24, 24), (32, 32), (48, 48), (64, 64), (128, 128), (256, 256)],
    )
    print("written", OUT, OUT.stat().st_size, "bytes")


if __name__ == "__main__":
    main()
