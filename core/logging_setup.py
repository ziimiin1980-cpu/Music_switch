"""Logging setup that survives ``--windowed`` builds (no stdout/stderr)."""

from __future__ import annotations

import faulthandler
import logging
import logging.handlers
import sys
import threading

from .config import logs_dir

_FORMAT = "%(asctime)s %(levelname)-7s %(threadName)-12s %(name)s: %(message)s"
_fault_file = None


class _StdoutToLog:
    """Pretend to be a stream so libraries printing tracebacks do not crash."""

    def __init__(self, level: int = logging.DEBUG) -> None:
        self._level = level

    def write(self, message: str) -> None:
        text = (message or "").rstrip()
        if text:
            logging.getLogger("stdout").log(self._level, text)

    def flush(self) -> None:
        pass

    def isatty(self) -> bool:
        return False


def setup_logging(verbose: bool = False) -> None:
    global _fault_file

    logfile = logs_dir() / "app.log"
    root = logging.getLogger()
    root.setLevel(logging.DEBUG if verbose else logging.INFO)
    for handler in list(root.handlers):
        root.removeHandler(handler)

    fmt = logging.Formatter(_FORMAT)
    file_handler = logging.handlers.RotatingFileHandler(
        logfile, maxBytes=1_000_000, backupCount=3, encoding="utf-8"
    )
    file_handler.setFormatter(fmt)
    root.addHandler(file_handler)

    if sys.stderr is not None:
        stream_handler = logging.StreamHandler(sys.stderr)
        stream_handler.setFormatter(fmt)
        root.addHandler(stream_handler)

    # Under pythonw.exe / --windowed both streams are None: replace them so a
    # stray print() or a WinRT traceback cannot kill the process.
    if sys.stdout is None:
        sys.stdout = _StdoutToLog(logging.DEBUG)
    if sys.stderr is None:
        sys.stderr = _StdoutToLog(logging.ERROR)

    # comtypes logs every COM Release() at DEBUG - pure noise in debug mode.
    for name in ("comtypes", "comtypes.client", "comtypes._post_coinit", "comtypes._post_coinit.unknwn"):
        logging.getLogger(name).setLevel(logging.WARNING)

    def excepthook(exc_type, exc_value, exc_tb):
        logging.getLogger("unhandled").critical(
            "Unhandled exception", exc_info=(exc_type, exc_value, exc_tb)
        )

    def thread_excepthook(args):
        logging.getLogger("unhandled").critical(
            "Unhandled exception in thread %s",
            args.thread.name if args.thread else "?",
            exc_info=(args.exc_type, args.exc_value, args.exc_traceback),
        )

    sys.excepthook = excepthook
    threading.excepthook = thread_excepthook

    try:
        _fault_file = open(logs_dir() / "crash.log", "a", encoding="utf-8")
        faulthandler.enable(file=_fault_file)
    except Exception:  # pragma: no cover
        pass

    logging.getLogger(__name__).info("logging to %s", logfile)
