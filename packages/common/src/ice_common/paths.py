"""Filesystem locations shared by main.py, the dashboard and the services.

Everything is relative to the working directory (``/app`` in Docker), matching
the bind mounts in docker-compose.yml (``./LOGS:/app/LOGS``, ``./data:/app/data``).
"""

import os
import shutil
from pathlib import Path

from loguru import logger

LOG_DIR = Path("LOGS")
LOG_FILE = LOG_DIR / "vmc.log"
DATA_DIR = Path("data")


def fsync_dir(directory: Path) -> None:
    """Flush a directory entry after a rename (no-op on Windows).

    Called by ``atomic_write`` (and so by every atomic-save path built on it)
    so a rename's directory-entry update is durable on POSIX before the
    caller reports success.
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


def atomic_write(
    path: Path,
    data: bytes | str,
    *,
    mode: int | None = None,
    backup: bool = False,
    encoding: str = "utf-8",
) -> None:
    """Atomically replace ``path`` with ``data``, durably.

    Writes a same-directory temp file (``<name>.tmp``, any stale one removed
    first, then created ``O_CREAT|O_EXCL`` with ``mode`` when given, so a
    secret is never readable through a looser pre-existing file), flushes and
    fsyncs it, optionally copies the live file to ``<name>.bak`` (a copy, so
    ``path`` is never briefly absent; re-moded to ``mode`` on POSIX when
    given), then ``os.replace``s it into place and fsyncs the directory.
    ``str`` is written in text mode (platform newline translation, exactly as
    ``open(path, "w")`` does); ``bytes`` are written verbatim. On any
    exception the temp file is unlinked and the exception re-raised; the
    target is untouched.
    """
    tmp = path.with_name(path.name + ".tmp")
    tmp.unlink(missing_ok=True)
    try:
        fd = os.open(
            tmp,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_BINARY", 0),
            0o666 if mode is None else mode,
        )
        if isinstance(data, bytes):
            f = os.fdopen(fd, "wb")
        else:
            f = os.fdopen(fd, "w", encoding=encoding)
        with f:
            f.write(data)
            f.flush()
            os.fsync(f.fileno())
        if backup and path.exists():
            bak = path.with_name(path.name + ".bak")
            shutil.copy2(path, bak)
            if mode is not None and os.name == "posix":
                os.chmod(bak, mode)
        os.replace(tmp, path)
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise
    fsync_dir(path.parent)
