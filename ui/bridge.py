"""Queued bridge between the asyncio loop thread and the Qt main thread.

The controller calls :meth:`UiBridge.publish` from ``perekl-async``.  Emitting a
Qt signal from a non-Qt thread is allowed and, because the receivers live in the
main thread, Qt delivers the snapshot through a queued connection.
"""

from __future__ import annotations

import logging

from PySide6.QtCore import QObject, Signal

log = logging.getLogger(__name__)


class UiBridge(QObject):
    #: Carries :class:`smtc.model.TrackSnapshot` by reference (plain Python object).
    state_changed = Signal(object)

    def publish(self, snapshot) -> None:
        try:
            self.state_changed.emit(snapshot)
        except Exception:  # pragma: no cover - Qt is shutting down
            log.debug("cannot emit the state signal", exc_info=True)
