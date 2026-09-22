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


def test_save_config_honors_env_var_when_no_path_given(tmp_path, monkeypatch):
    custom = tmp_path / "env-dir" / "config.json"
    custom.parent.mkdir()
    monkeypatch.setenv("ICE_COLDER_CONFIG", str(custom))
    cfg = ConfigModel()
    save_config(cfg)
    assert custom.exists()


def test_save_config_env_var_tmp_and_bak_land_beside_it(tmp_path, monkeypatch):
    custom = tmp_path / "env-dir" / "config.json"
    custom.parent.mkdir()
    monkeypatch.setenv("ICE_COLDER_CONFIG", str(custom))
    cfg = ConfigModel()
    save_config(cfg)  # first save: creates the file
    save_config(cfg)  # second save: creates the backup
    assert custom.exists()
    assert (custom.parent / "config.json.bak").exists()
    assert not (custom.parent / "config.json.tmp").exists()


def test_save_config_env_var_overrides_config_path_attribute(tmp_path, monkeypatch):
    """ICE_COLDER_CONFIG takes priority over a monkeypatched CONFIG_PATH."""
    import services.config_store as cs

    other = tmp_path / "other.json"
    monkeypatch.setattr(cs, "CONFIG_PATH", tmp_path / "should-not-be-used.json")
    monkeypatch.setenv("ICE_COLDER_CONFIG", str(other))
    cfg = ConfigModel()
    save_config(cfg)
    assert other.exists()
    assert not (tmp_path / "should-not-be-used.json").exists()


def test_add_product_auto_assigns_lowest_free_slot(tmp_path, monkeypatch):
    import services.config_store as cs
    from config.config_model import Product

    monkeypatch.setattr(cs, "CONFIG_PATH", tmp_path / "config.json")
    cfg = ConfigModel()
    cfg.physical.products = [
        Product(sku="A", name="A", price=1.0, slot=0),
        Product(sku="B", name="B", price=1.0, slot=2),
    ]
    assert add_product(cfg, "NEW-1", "New Thing", 3.25) is True
    new = next(p for p in cfg.products if p.sku == "NEW-1")
    assert new.slot == 1  # lowest free slot, not len(products)


def test_add_product_with_explicit_slot(tmp_path, monkeypatch):
    import services.config_store as cs

    monkeypatch.setattr(cs, "CONFIG_PATH", tmp_path / "config.json")
    cfg = ConfigModel()
    assert add_product(cfg, "NEW-1", "New Thing", 3.25, slot=7) is True
    new = next(p for p in cfg.products if p.sku == "NEW-1")
    assert new.slot == 7


def test_add_product_with_taken_slot_fails(tmp_path, monkeypatch):
    import services.config_store as cs
    from config.config_model import Product

    monkeypatch.setattr(cs, "CONFIG_PATH", tmp_path / "config.json")
    cfg = ConfigModel()
    cfg.physical.products = [Product(sku="A", name="A", price=1.0, slot=0)]
    assert add_product(cfg, "NEW-1", "New Thing", 3.25, slot=0) is False
    assert len(cfg.products) == 1


def test_update_product_can_change_slot(tmp_path, monkeypatch):
    import services.config_store as cs
    from config.config_model import Product
    from services.config_store import update_product

    monkeypatch.setattr(cs, "CONFIG_PATH", tmp_path / "config.json")
    cfg = ConfigModel()
    cfg.physical.products = [Product(sku="A", name="A", price=1.0, slot=0)]
    assert update_product(cfg, "A", "A", 1.0, slot=4) is True
    assert cfg.products[0].slot == 4


def test_add_product_rejects_negative_slot(tmp_path, monkeypatch):
    import services.config_store as cs

    monkeypatch.setattr(cs, "CONFIG_PATH", tmp_path / "config.json")
    cfg = ConfigModel()
    assert add_product(cfg, "NEW-1", "New Thing", 3.25, slot=-1) is False
    assert len(cfg.products) == 0
    assert not any(p.sku == "NEW-1" for p in cfg.products)


def test_update_product_rejects_negative_slot(tmp_path, monkeypatch):
    import services.config_store as cs
    from config.config_model import Product
    from services.config_store import update_product

    monkeypatch.setattr(cs, "CONFIG_PATH", tmp_path / "config.json")
    cfg = ConfigModel()
    cfg.physical.products = [Product(sku="A", name="A", price=1.0, slot=0)]
    assert update_product(cfg, "A", "A", 1.0, slot=-1) is False
    assert cfg.products[0].slot == 0  # unchanged


def test_update_product_rejects_slot_already_in_use(tmp_path, monkeypatch):
    import services.config_store as cs
    from config.config_model import Product
    from services.config_store import update_product

    monkeypatch.setattr(cs, "CONFIG_PATH", tmp_path / "config.json")
    cfg = ConfigModel()
    cfg.physical.products = [
        Product(sku="A", name="A", price=1.0, slot=0),
        Product(sku="B", name="B", price=1.0, slot=1),
    ]
    assert update_product(cfg, "B", "B", 1.0, slot=0) is False
    assert cfg.products[1].slot == 1  # unchanged


def test_delete_product_leaves_other_slots_unchanged(tmp_path, monkeypatch):
    import services.config_store as cs
    from config.config_model import Product
    from services.config_store import delete_product

    monkeypatch.setattr(cs, "CONFIG_PATH", tmp_path / "config.json")
    cfg = ConfigModel()
    cfg.physical.products = [
        Product(sku="A", name="A", price=1.0, slot=0),
        Product(sku="B", name="B", price=1.0, slot=1),
        Product(sku="C", name="C", price=1.0, slot=2),
    ]
    assert delete_product(cfg, "A") is True
    remaining = {p.sku: p.slot for p in cfg.products}
    assert remaining == {"B": 1, "C": 2}


def test_save_config_fsyncs_before_replace(tmp_path, monkeypatch):
    import os
    import services.config_store as cs

    calls: list[str] = []
    real_fsync = os.fsync
    real_replace = os.replace

    def fake_fsync(fd):
        calls.append("fsync")
        return real_fsync(fd)

    def fake_replace(src, dst):
        calls.append("replace")
        return real_replace(src, dst)

    monkeypatch.setattr(cs.os, "fsync", fake_fsync)
    monkeypatch.setattr(cs.os, "replace", fake_replace)

    save_config(ConfigModel(), tmp_path / "config.json")

    assert "fsync" in calls
    assert calls.index("fsync") < calls.index("replace")


def test_add_product_unrecognized_kind_falls_back_to_other(tmp_path, monkeypatch):
    import services.config_store as cs

    monkeypatch.setattr(cs, "CONFIG_PATH", tmp_path / "config.json")
    cfg = ConfigModel()
    assert add_product(cfg, "NEW-1", "New Thing", 3.25, kind="soda") is True
    new = next(p for p in cfg.products if p.sku == "NEW-1")
    assert new.kind == "other"
