"""Tests for services/config_store.py — secret-preserving, atomic saves."""

import json

from pydantic import SecretStr

from config.config_model import ConfigModel
from services.config_store import add_product, save_config


def _config_with_secret() -> ConfigModel:
    cfg = ConfigModel()
    cfg.payment.stripe.api_key = SecretStr("sk_live_REALKEY123")
    return cfg


def test_save_config_writes_real_secret_values(tmp_path):
    cfg = _config_with_secret()
    target = tmp_path / "config.json"
    save_config(cfg, target)
    text = target.read_text(encoding="utf-8")
    assert "sk_live_REALKEY123" in text
    assert "**********" not in text


def test_save_config_round_trips_through_model_validate(tmp_path):
    cfg = _config_with_secret()
    target = tmp_path / "config.json"
    save_config(cfg, target)
    reloaded = ConfigModel.model_validate(
        json.loads(target.read_text(encoding="utf-8"))
    )
    assert reloaded.payment.stripe.api_key.get_secret_value() == "sk_live_REALKEY123"


def test_save_config_keeps_rolling_backup(tmp_path):
    cfg = ConfigModel()
    target = tmp_path / "config.json"
    save_config(cfg, target)  # first save: no backup yet
    assert not (tmp_path / "config.json.bak").exists()
    save_config(cfg, target)  # second save: previous file backed up
    assert (tmp_path / "config.json.bak").exists()


def test_save_config_leaves_no_tmp_file(tmp_path):
    cfg = ConfigModel()
    target = tmp_path / "config.json"
    save_config(cfg, target)
    assert not (tmp_path / "config.json.tmp").exists()


def test_add_product_uses_module_config_path(tmp_path, monkeypatch):
    import services.config_store as cs

    monkeypatch.setattr(cs, "CONFIG_PATH", tmp_path / "config.json")
    cfg = ConfigModel()
    assert add_product(cfg, "NEW-1", "New Thing", 3.25) is True
    assert (tmp_path / "config.json").exists()


def test_delete_product_removes_and_saves(tmp_path):
    from config.config_model import Product
    from services.config_store import delete_product

    cfg = ConfigModel()
    cfg.physical.products = [Product(sku="A-1", name="Thing A", price=1.0)]
    assert delete_product(cfg, "A-1") is True
    assert cfg.products == []
    assert (tmp_path / "config.json").exists()


def test_delete_product_unknown_sku_returns_false_without_saving(tmp_path):
    from services.config_store import delete_product

    cfg = ConfigModel()
    assert delete_product(cfg, "NOPE") is False
    assert not (tmp_path / "config.json").exists()
