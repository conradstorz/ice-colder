"""Shared fixtures. Isolates every test from the developer's real config.json."""

import logging

import pytest
from loguru import logger as _loguru

import services.config_store as config_store


@pytest.fixture(autouse=True)
def isolated_config_path(tmp_path, monkeypatch):
    """Redirect config saves to a temp dir so no test can clobber config.json."""
    monkeypatch.setattr(config_store, "CONFIG_PATH", tmp_path / "config.json")


@pytest.fixture
def caplog(caplog):
    """Bridge loguru records into pytest's caplog (loguru bypasses stdlib logging)."""
    handler_id = _loguru.add(
        lambda msg: logging.getLogger("loguru").handle(
            logging.LogRecord(
                "loguru",
                msg.record["level"].no,
                "",
                0,
                msg.record["message"],
                None,
                None,
            )
        ),
        level="DEBUG",
    )
    yield caplog
    _loguru.remove(handler_id)
