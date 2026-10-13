"""Filesystem locations shared by main.py, the dashboard and the services.

Everything is relative to the working directory (``/app`` in Docker), matching
the bind mounts in docker-compose.yml (``./LOGS:/app/LOGS``, ``./data:/app/data``).
"""

import os
from pathlib import Path

from loguru import logger

LOG_DIR = Path("LOGS")
LOG_FILE = LOG_DIR / "vmc.log"
DATA_DIR = Path("data")


def fsync_dir(directory: Path) -> None:
    """Flush a directory entry after a rename (no-op on Windows).

    Shared by every atomic-save path (``services/config_store.py``,
    ``services/dispensers.py``) so a rename's directory-entry update is
    durable on POSIX before the caller reports success.
    """
    if os.name != "posix":
        return
    try:
        fd = os.open(directory, os.O_RDONLY)
    except OSError:
        return
    try:
        os.fsync(fd)
    except OSError as e:
        logger.warning(f"paths: directory fsync failed for {directory}: {e}")
    finally:
        os.close(fd)
