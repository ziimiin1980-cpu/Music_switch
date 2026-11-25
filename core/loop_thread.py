"""A dedicated thread that owns a long-lived asyncio event loop.

Why not ``asyncio.run`` in the main thread?  Because Qt must own the main
thread (it runs the Windows message loop, the tray icon and the dock panel) and
because WinRT event handlers need a loop that is already running when the first
event arrives.
"""

from __future__ import annotations

import asyncio
import logging
import threading
from typing import Any, Coroutine, Optional

log = logging.getLogger(__name__)


class LoopThread:
    def __init__(self) -> None:
        self.loop = asyncio.new_event_loop()
        self._ready = threading.Event()
        self._thread = threading.Thread(target=self._run, name="perekl-async", daemon=True)
        self._started = False

    # -- lifecycle ---------------------------------------------------------

    def start(self, timeout: float = 5.0) -> None:
        if self._started:
            return
        self._started = True
        self._thread.start()
        if not self._ready.wait(timeout):
            raise RuntimeError("asyncio event loop did not start")

    def _run(self) -> None:
        asyncio.set_event_loop(self.loop)
        self.loop.call_soon(self._ready.set)
        try:
            self.loop.run_forever()
        finally:
            # Cancel whatever is left so WinRT handlers are unsubscribed.
            try:
                pending = [t for t in asyncio.all_tasks(self.loop) if not t.done()]
                for task in pending:
                    task.cancel()
                if pending:
                    self.loop.run_until_complete(
                        asyncio.gather(*pending, return_exceptions=True)
                    )
            except Exception:  # pragma: no cover - best effort
                log.debug("pending task cleanup failed", exc_info=True)
            finally:
                self.loop.close()

    def stop(self, timeout: float = 5.0) -> None:
        if not self._started:
            return
        try:
            self.loop.call_soon_threadsafe(self.loop.stop)
        except RuntimeError:
            pass
        if self._thread.is_alive():
            self._thread.join(timeout)

    # -- submission (safe from any thread) ---------------------------------

    def submit(self, coro: Coroutine[Any, Any, Any]):
        """Schedule *coro* on the loop. Never blocks, never raises."""
        try:
            return asyncio.run_coroutine_threadsafe(coro, self.loop)
        except RuntimeError:
            coro.close()
            return None

    def submit_and_wait(self, coro, timeout: float = 3.0) -> Optional[Any]:
        future = self.submit(coro)
        if future is None:
            return None
        try:
            return future.result(timeout)
        except Exception:
            log.warning("coroutine did not finish in %.1fs", timeout, exc_info=True)
            return None
