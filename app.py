"""Pereklych - Edge media controls in the Windows system tray.

Main thread      QApplication + QSystemTrayIcon + the dock panel
perekl-async     the existing asyncio loop (WinRT, events, cover art)
perekl-volume    the existing Core Audio daemon thread
"""

from __future__ import annotations

import logging
import os
import sys

from PySide6.QtWidgets import QApplication

from core.config import APP_NAME, load_settings, save_settings
from core.logging_setup import setup_logging
from core.loop_thread import LoopThread
from core.single_instance import SingleInstance
from smtc.controller import MediaController
from ui.bridge import UiBridge
from ui.tray_qt import TrayQt

log = logging.getLogger(__name__)


class Application:
    def __init__(self, app: QApplication) -> None:
        self.app = app
        self.settings = load_settings()
        self.loop_thread = LoopThread()
        self._quitting = False

        # Emitted from perekl-async; delivered to the Qt main thread as a queued
        # signal (TrackSnapshot is a plain frozen dataclass, passed by ref).
        self.bridge = UiBridge()
        self.controller = MediaController(
            self.loop_thread.loop,
            self.settings,
            self.bridge.publish,
            self._persist,
        )
        self.tray = TrayQt(
            self.controller,
            self.settings,
            self.loop_thread,
            on_quit=self.quit,
            on_save=self._persist,
        )
        self.bridge.state_changed.connect(self.tray.apply_state)

    def _persist(self) -> None:
        try:
            save_settings(self.settings)
        except Exception:
            log.exception("cannot save settings")

    def run(self) -> None:
        self.loop_thread.start()
        self.loop_thread.submit(self.controller.start())
        self.tray.start()
        try:
            self.app.exec()
        except KeyboardInterrupt:
            log.info("interrupted")
        finally:
            self._shutdown()

    def quit(self) -> None:
        if self._quitting:
            return
        self._quitting = True
        log.info("quit requested")
        try:
            self.tray.stop()
        finally:
            self.app.quit()

    def _shutdown(self) -> None:
        if not self._quitting:
            self._quitting = True
        try:
            self.tray.stop()
        except Exception:
            log.debug("tray stop failed", exc_info=True)
        self.loop_thread.submit_and_wait(self.controller.stop(), timeout=3.0)
        self.loop_thread.stop()


def main() -> int:
    setup_logging(verbose=os.environ.get("PEREKLYCH_DEBUG") == "1")
    log.info("=== %s starting ===", APP_NAME)
    instance = SingleInstance()
    if instance.already_running:
        log.warning("another instance is already running, exiting")
        instance.close()
        return 0

    try:
        app = QApplication(sys.argv)
    except Exception:
        log.exception("cannot create QApplication")
        instance.close()
        return 1

    app.setApplicationName(APP_NAME)
    app.setApplicationDisplayName(APP_NAME)
    app.setQuitOnLastWindowClosed(False)

    try:
        Application(app).run()
    except Exception:
        log.exception("fatal error")
        return 1
    finally:
        instance.close()
    log.info("=== Pereklych stopped ===")
    return 0


if __name__ == "__main__":
    sys.exit(main())
