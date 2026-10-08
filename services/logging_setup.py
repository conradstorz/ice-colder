"""Configure application log sinks explicitly at startup, never on import."""

import os
import sys

from loguru import logger

from services.paths import LOG_DIR, LOG_FILE


def setup_logging() -> None:
    """Replace existing handlers with the console and rotating application logs."""
    os.makedirs(LOG_DIR, exist_ok=True)
    logger.remove()
    logger.add(
        str(LOG_FILE),
        serialize=False,
        rotation="00:00",
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
