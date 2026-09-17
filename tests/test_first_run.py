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


def test_env_var_path_used_for_first_run_creation(tmp_path, monkeypatch):
    custom = tmp_path / "custom.json"
    monkeypatch.setenv("ICE_COLDER_CONFIG", str(custom))
    cfg = main_mod.load_config()
    assert cfg.products == []
    assert custom.exists()
    saved = json.loads(custom.read_text(encoding="utf-8"))
    assert saved["physical"]["products"] == []


def test_env_var_path_used_for_subsequent_loads(tmp_path, monkeypatch):
    custom = tmp_path / "custom.json"
    monkeypatch.setenv("ICE_COLDER_CONFIG", str(custom))
    main_mod.load_config()  # first run writes the file
    cfg = main_mod.load_config()  # second run loads it normally
    assert cfg.products == []


def test_directory_at_config_path_exits_with_code_1(tmp_path, monkeypatch):
    bogus = tmp_path / "config.json"
    bogus.mkdir()
    monkeypatch.setenv("ICE_COLDER_CONFIG", str(bogus))
    with pytest.raises(SystemExit) as exc_info:
        main_mod.load_config()
    assert exc_info.value.code == 1
