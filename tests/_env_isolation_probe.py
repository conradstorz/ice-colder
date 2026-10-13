"""Probe run in a child pytest by test_env_isolation.py (not collected itself)."""

from config.config_model import ConfigModel
from services.config_store import save_config


def test_probe_save_config():
    save_config(ConfigModel())
