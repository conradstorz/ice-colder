"""The app's own exit-signal handlers.

uvicorn's ``Server.serve`` saves whatever handler is installed when it starts,
swaps in its own, then restores the saved one and re-raises the signal. With the
default disposition saved, a SIGTERM kills the process before ``main``'s
``finally`` runs unless the process is PID 1 (which ignores the self-raised
signal). Installing a benign Python-level handler first makes the re-raise a
no-op, so the shutdown sequence always runs.
"""

import signal
import sys
import threading
from collections.abc import Callable

from loguru import logger


def _exit_signals() -> list[signal.Signals]:
    sigs = [signal.SIGTERM, signal.SIGINT]
    if sys.platform == "win32":
        sigs.append(signal.SIGBREAK)
    return sigs


def install_exit_handlers(request_exit: Callable[[], None]) -> Callable[[], None]:
    """Install SIGTERM/SIGINT (and SIGBREAK on Windows) handlers.

    Each handler logs and calls ``request_exit``; a repeated signal just calls it
    again and never raises. Returns a callable restoring the previous handlers.
    Off the main thread, logs a warning and returns a no-op.
    """
    if threading.current_thread() is not threading.main_thread():
        logger.warning("Exit signal handlers not installed: not on the main thread")
        return lambda: None

    def handler(signum: int, frame: object) -> None:
        logger.info(f"Shutdown requested by {signal.Signals(signum).name}")
        request_exit()

    previous: dict[signal.Signals, object] = {}
    for sig in _exit_signals():
        previous[sig] = signal.signal(sig, handler)

    def restore() -> None:
        for sig, old in previous.items():
            signal.signal(sig, old if old is not None else signal.SIG_DFL)

    return restore
