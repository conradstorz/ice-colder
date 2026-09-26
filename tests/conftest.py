"""Shared fixtures. Isolates every test from the developer's real config.json."""

import logging

import pytest
from loguru import logger as _loguru

import services.access as access
import services.config_store as config_store


@pytest.fixture(autouse=True)
def isolated_config_path(tmp_path, monkeypatch):
    """Redirect config saves to a temp dir so no test can clobber config.json."""
    monkeypatch.setattr(config_store, "CONFIG_PATH", tmp_path / "config.json")


@pytest.fixture(autouse=True, scope="session")
def _fast_scrypt():
    """Lower the PIN/secret hashing cost for the whole test session.

    services.access._scrypt reads SCRYPT_N from the module at call time, so
    patching the module attribute here is enough to speed up hash_pin,
    verify_pin, hash_secret and verify_secret everywhere — no AccessStore
    constructor plumbing needed. Session-scoped so the (measured) 213.8 ms
    production cost is paid once conceptually, not per test; restored at
    session teardown so nothing after the run — or a production-default
    guard test reading the live attribute — sees the weakened value.
    Tests that need to assert the real shipped default instead read the
    literal out of the source file (see
    TestHashing.test_production_scrypt_n_default_is_unchanged in
    test_access.py), since this fixture patches the value for the whole
    session before any such test can run.
    """
    original = access.SCRYPT_N
    access.SCRYPT_N = 2**4
    yield
    access.SCRYPT_N = original


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
