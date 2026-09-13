"""First-run startup: config.json is auto-created and the app continues."""

import json

import pytest

import main as main_mod


def test_first_run_creates_config_and_continues(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    cfg = main_mod.load_config()
    assert cfg.products == []
    saved = json.loads((tmp_path / "config.json").read_text(encoding="utf-8"))
    assert saved["physical"]["products"] == []


def test_first_run_config_round_trips(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    main_mod.load_config()  # first run writes the file
    cfg = main_mod.load_config()  # second run loads it normally
    assert cfg.products == []


def test_unreadable_config_still_exits(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    (tmp_path / "config.json").write_text("{not valid json", encoding="utf-8")
    with pytest.raises(SystemExit):
        main_mod.load_config()


def test_invalid_config_still_exits(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    (tmp_path / "config.json").write_text(
        '{"mqtt": {"broker_port": "not-a-port"}}', encoding="utf-8"
    )
    with pytest.raises(SystemExit):
        main_mod.load_config()
