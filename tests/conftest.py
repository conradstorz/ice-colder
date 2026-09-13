"""Shared fixtures. Isolates every test from the developer's real config.json."""

import pytest

import services.config_store as config_store


@pytest.fixture(autouse=True)
def isolated_config_path(tmp_path, monkeypatch):
    """Redirect config saves to a temp dir so no test can clobber config.json."""
    monkeypatch.setattr(config_store, "CONFIG_PATH", tmp_path / "config.json")
