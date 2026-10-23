"""Configure application log sinks explicitly at startup, never on import."""

import os
import sys
from datetime import datetime, timedelta

from loguru import logger

from ice_common.paths import LOG_DIR, LOG_FILE


LOG_LEVEL_ENV = "ICE_COLDER_LOG_LEVEL"
DEFAULT_FILE_LEVEL = "INFO"
MAX_LOG_BYTES = 50 * 1024 * 1024


class _MidnightOrSizeRotation:
    """loguru rotation callable: rotate at local midnight or past a size cap."""

    def __init__(self) -> None:
        self._next_midnight: datetime | None = None

    def __call__(self, message, file) -> bool:
        now = message.record["time"]
        if self._next_midnight is None:
            self._next_midnight = _midnight_after(now)
        if now >= self._next_midnight:
            self._next_midnight = _midnight_after(now)
            return True
        file.seek(0, 2)
        return file.tell() + len(message) > MAX_LOG_BYTES


def _midnight_after(moment: datetime) -> datetime:
    start = moment.replace(hour=0, minute=0, second=0, microsecond=0)
    return start + timedelta(days=1)


def _file_level() -> tuple[str, str | None]:
    """Return (level, warning) for the main file sink from the environment."""
    raw = os.environ.get(LOG_LEVEL_ENV, "").strip()
    if not raw:
        return DEFAULT_FILE_LEVEL, None
    name = raw.upper()
    try:
        logger.level(name)
    except ValueError:
        return (
            DEFAULT_FILE_LEVEL,
            f"Invalid {LOG_LEVEL_ENV}={raw!r}; using {DEFAULT_FILE_LEVEL}",
        )
    return name, None


def setup_logging() -> None:
    """Replace existing handlers with the console and rotating application logs."""
    os.makedirs(LOG_DIR, exist_ok=True)
    logger.remove()
    file_level, level_warning = _file_level()
    logger.add(
        str(LOG_FILE),
        level=file_level,
        serialize=False,
        rotation=_MidnightOrSizeRotation(),
        retention="300 days",
        compression="zip",
        format="{message};{level} {time:YYYY-MM-DD HH:mm:ss}",
    )
    logger.add(
        sys.stdout,
        level="INFO",
        serialize=False,
        format="{time:YYYY-MM-DD HH:mm:ss} | {level: <8} | {message}",
    )
    logger.add(
        str(LOG_DIR / "transactions.log"),
        filter=lambda record: record["extra"].get("transaction", False),
        rotation="00:00",
        retention="300 days",
        compression="zip",
        format="{time:YYYY-MM-DD HH:mm:ss} | {message}",
    )
    logger.add(
        str(LOG_DIR / "ice_maker.log"),
        filter=lambda record: record["extra"].get("ice_maker", False),
        rotation="00:00",
        retention="300 days",
        compression="zip",
        format="{time:YYYY-MM-DD HH:mm:ss} | {message}",
    )
    logger.add(
        str(LOG_DIR / "vending.log"),
        filter=lambda record: record["extra"].get("vending", False),
        rotation="00:00",
        retention="300 days",
        compression="zip",
        format="{time:YYYY-MM-DD HH:mm:ss} | {message}",
    )
    if level_warning:
        logger.warning(level_warning)
