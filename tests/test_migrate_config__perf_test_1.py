from __future__ import annotations

import gc
import json
import logging
import os
import time
from dataclasses import dataclass, field
from typing import Any, Callable, List, Optional

import pytest

from config.config_test import migrate_config, save_config


def codeflash_wrap(
    wrapped: Callable[..., Any],
    test_module_name: str,
    test_class_name: Optional[str],
    test_name: str,
    function_name: str,
    line_id: str,
    loop_index: int,
    *args: Any,
    **kwargs: Any,
) -> str:
    test_id = f"{test_module_name}:{test_class_name}:{test_name}:{line_id}:{loop_index}"
    if not hasattr(codeflash_wrap, "index"):
        codeflash_wrap.index = {}
    if test_id in codeflash_wrap.index:
        codeflash_wrap.index[test_id] += 1
    else:
        codeflash_wrap.index[test_id] = 0
    codeflash_test_index = codeflash_wrap.index[test_id]
    invocation_id = f"{line_id}_{codeflash_test_index}"
    exception = None
    gc.disable()
    try:
        counter = time.perf_counter_ns()
        return_value = wrapped(*args, **kwargs)
        codeflash_duration = time.perf_counter_ns() - counter
    except Exception as e:
        codeflash_duration = time.perf_counter_ns() - counter
        exception = e
    gc.enable()
    print(
        f"!######{test_module_name}:{(test_class_name + '.' if test_class_name else '')}{test_name}:{function_name}:{loop_index}:{invocation_id}:{codeflash_duration}######!"
    )
    if exception:
        raise exception
    return return_value


@dataclass
class ProductModel:
    name: str
    price: float


@dataclass
class ConfigModel:
    config_version: int = 1
    products: List[ProductModel] = field(default_factory=lambda: [ProductModel(name="Widget", price=9.99)])

    def model_dump(self):
        return {
            "config_version": self.config_version,
            "products": [{"name": p.name, "price": p.price} for p in self.products],
        }

    @classmethod
    def from_dict(cls, d):
        return cls(
            config_version=d.get("config_version", 1),
            products=[ProductModel(**prod) for prod in d.get("products", [])],
        )


logger = logging.getLogger("test_logger")


def load_config(filepath: str) -> ConfigModel:
    with open(filepath, "r") as f:
        d = json.load(f)
    return ConfigModel.from_dict(d)


def test_no_migration_needed(tmp_path):
    codeflash_loop_index = int(os.environ["CODEFLASH_LOOP_INDEX"])
    "Test when config version matches default, no migration occurs and file is not changed."
    cfg = ConfigModel(config_version=1, products=[ProductModel("Widget", 9.99)])
    file = tmp_path / "config.json"
    save_config(cfg, str(file))
    loaded_cfg = load_config(str(file))
    codeflash_return_value = codeflash_wrap(
        migrate_config,
        "tests.test_migrate_config__unit_test_1",
        None,
        "test_no_migration_needed",
        "migrate_config",
        "5",
        codeflash_loop_index,
        loaded_cfg,
        str(file),
    )
    codeflash_output = codeflash_return_value
    migrated = codeflash_output
    with open(file) as f:
        data = json.load(f)


def test_migration_updates_version(tmp_path):
    codeflash_loop_index = int(os.environ["CODEFLASH_LOOP_INDEX"])
    "Test config with old version is migrated to new version and file updated."
    old_version = 0
    cfg = ConfigModel(config_version=old_version, products=[ProductModel("Widget", 9.99)])
    file = tmp_path / "config.json"
    save_config(cfg, str(file))
    loaded_cfg = load_config(str(file))
    codeflash_return_value = codeflash_wrap(
        migrate_config,
        "tests.test_migrate_config__unit_test_1",
        None,
        "test_migration_updates_version",
        "migrate_config",
        "6",
        codeflash_loop_index,
        loaded_cfg,
        str(file),
    )
    codeflash_output = codeflash_return_value
    migrated = codeflash_output
    with open(file) as f:
        data = json.load(f)


def test_migration_preserves_other_fields(tmp_path):
    codeflash_loop_index = int(os.environ["CODEFLASH_LOOP_INDEX"])
    "Test that migration does not overwrite existing fields other than version."
    cfg = ConfigModel(config_version=0, products=[ProductModel("Gadget", 42.0)])
    file = tmp_path / "config.json"
    save_config(cfg, str(file))
    loaded_cfg = load_config(str(file))
    codeflash_return_value = codeflash_wrap(
        migrate_config,
        "tests.test_migrate_config__unit_test_1",
        None,
        "test_migration_preserves_other_fields",
        "migrate_config",
        "5",
        codeflash_loop_index,
        loaded_cfg,
        str(file),
    )
    codeflash_output = codeflash_return_value
    migrated = codeflash_output


def test_empty_products_list(tmp_path):
    codeflash_loop_index = int(os.environ["CODEFLASH_LOOP_INDEX"])
    "Test migration when products list is empty."
    cfg = ConfigModel(config_version=0, products=[])
    file = tmp_path / "config.json"
    save_config(cfg, str(file))
    loaded_cfg = load_config(str(file))
    codeflash_return_value = codeflash_wrap(
        migrate_config,
        "tests.test_migrate_config__unit_test_1",
        None,
        "test_empty_products_list",
        "migrate_config",
        "5",
        codeflash_loop_index,
        loaded_cfg,
        str(file),
    )
    codeflash_output = codeflash_return_value
    migrated = codeflash_output


def test_missing_products_key(tmp_path):
    codeflash_loop_index = int(os.environ["CODEFLASH_LOOP_INDEX"])
    "Test migration when products key is missing from file."
    file = tmp_path / "config.json"
    with open(file, "w") as f:
        json.dump({"config_version": 0}, f)
    loaded_cfg = load_config(str(file))
    codeflash_return_value = codeflash_wrap(
        migrate_config,
        "tests.test_migrate_config__unit_test_1",
        None,
        "test_missing_products_key",
        "migrate_config",
        "4",
        codeflash_loop_index,
        loaded_cfg,
        str(file),
    )
    codeflash_output = codeflash_return_value
    migrated = codeflash_output


def test_negative_config_version(tmp_path):
    codeflash_loop_index = int(os.environ["CODEFLASH_LOOP_INDEX"])
    "Test migration with a negative config version."
    cfg = ConfigModel(config_version=-5, products=[ProductModel("Widget", 9.99)])
    file = tmp_path / "config.json"
    save_config(cfg, str(file))
    loaded_cfg = load_config(str(file))
    codeflash_return_value = codeflash_wrap(
        migrate_config,
        "tests.test_migrate_config__unit_test_1",
        None,
        "test_negative_config_version",
        "migrate_config",
        "5",
        codeflash_loop_index,
        loaded_cfg,
        str(file),
    )
    codeflash_output = codeflash_return_value
    migrated = codeflash_output


def test_large_config_version(tmp_path):
    codeflash_loop_index = int(os.environ["CODEFLASH_LOOP_INDEX"])
    "Test migration with a very large config version."
    cfg = ConfigModel(config_version=99999, products=[ProductModel("Widget", 9.99)])
    file = tmp_path / "config.json"
    save_config(cfg, str(file))
    loaded_cfg = load_config(str(file))
    codeflash_return_value = codeflash_wrap(
        migrate_config,
        "tests.test_migrate_config__unit_test_1",
        None,
        "test_large_config_version",
        "migrate_config",
        "5",
        codeflash_loop_index,
        loaded_cfg,
        str(file),
    )
    codeflash_output = codeflash_return_value
    migrated = codeflash_output


def test_corrupted_config_file(tmp_path):
    codeflash_loop_index = int(os.environ["CODEFLASH_LOOP_INDEX"])
    "Test migration raises error on corrupted config file."
    file = tmp_path / "config.json"
    with open(file, "w") as f:
        f.write("{not a valid json")
    with pytest.raises(json.JSONDecodeError):
        _ = load_config(str(file))


def test_config_file_permissions(tmp_path):
    codeflash_loop_index = int(os.environ["CODEFLASH_LOOP_INDEX"])
    "Test migration handles file permission errors gracefully."
    cfg = ConfigModel(config_version=0)
    file = tmp_path / "config.json"
    save_config(cfg, str(file))
    os.chmod(file, 292)
    loaded_cfg = load_config(str(file))
    codeflash_return_value = codeflash_wrap(
        migrate_config,
        "tests.test_migrate_config__unit_test_1",
        None,
        "test_config_file_permissions",
        "migrate_config",
        "6",
        codeflash_loop_index,
        loaded_cfg,
        str(file),
    )
    codeflash_output = codeflash_return_value
    migrated = codeflash_output
    loaded_cfg.config_version = -1
    os.chmod(file, 292)
    try:
        codeflash_return_value = codeflash_wrap(
            migrate_config,
            "tests.test_migrate_config__unit_test_1",
            None,
            "test_config_file_permissions",
            "migrate_config",
            "10_0",
            codeflash_loop_index,
            loaded_cfg,
            str(file),
        )
    except Exception as e:
        pytest.fail(f"Unexpected exception: {e}")


def test_large_number_of_products(tmp_path):
    codeflash_loop_index = int(os.environ["CODEFLASH_LOOP_INDEX"])
    "Test migration with a large number of products."
    num_products = 1000
    products = [ProductModel(f"Item{i}", float(i)) for i in range(num_products)]
    cfg = ConfigModel(config_version=0, products=products)
    file = tmp_path / "config.json"
    save_config(cfg, str(file))
    loaded_cfg = load_config(str(file))
    codeflash_return_value = codeflash_wrap(
        migrate_config,
        "tests.test_migrate_config__unit_test_1",
        None,
        "test_large_number_of_products",
        "migrate_config",
        "7",
        codeflash_loop_index,
        loaded_cfg,
        str(file),
    )
    codeflash_output = codeflash_return_value
    migrated = codeflash_output
    for i in range(num_products):
        pass


def test_multiple_migrations(tmp_path):
    codeflash_loop_index = int(os.environ["CODEFLASH_LOOP_INDEX"])
    "Test repeated migrations (should be idempotent after first call)."
    cfg = ConfigModel(config_version=0)
    file = tmp_path / "config.json"
    save_config(cfg, str(file))
    loaded_cfg = load_config(str(file))
    codeflash_return_value = codeflash_wrap(
        migrate_config,
        "tests.test_migrate_config__unit_test_1",
        None,
        "test_multiple_migrations",
        "migrate_config",
        "5",
        codeflash_loop_index,
        loaded_cfg,
        str(file),
    )
    codeflash_output = codeflash_return_value
    migrated1 = codeflash_output
    codeflash_return_value = codeflash_wrap(
        migrate_config,
        "tests.test_migrate_config__unit_test_1",
        None,
        "test_multiple_migrations",
        "migrate_config",
        "7",
        codeflash_loop_index,
        migrated1,
        str(file),
    )
    codeflash_output = codeflash_return_value
    migrated2 = codeflash_output


def test_concurrent_migrations(tmp_path):
    codeflash_loop_index = int(os.environ["CODEFLASH_LOOP_INDEX"])
    "Test that multiple migrations on the same file do not corrupt data."
    cfg = ConfigModel(config_version=0, products=[ProductModel("Widget", 9.99)])
    file = tmp_path / "config.json"
    save_config(cfg, str(file))
    loaded_cfg1 = load_config(str(file))
    loaded_cfg2 = load_config(str(file))
    codeflash_return_value = codeflash_wrap(
        migrate_config,
        "tests.test_migrate_config__unit_test_1",
        None,
        "test_concurrent_migrations",
        "migrate_config",
        "6",
        codeflash_loop_index,
        loaded_cfg1,
        str(file),
    )
    codeflash_output = codeflash_return_value
    migrated1 = codeflash_output
    codeflash_return_value = codeflash_wrap(
        migrate_config,
        "tests.test_migrate_config__unit_test_1",
        None,
        "test_concurrent_migrations",
        "migrate_config",
        "8",
        codeflash_loop_index,
        loaded_cfg2,
        str(file),
    )
    codeflash_output = codeflash_return_value
    migrated2 = codeflash_output


def test_large_config_file_size(tmp_path):
    codeflash_loop_index = int(os.environ["CODEFLASH_LOOP_INDEX"])
    "Test migration with a config file close to 1000 elements in products."
    products = [ProductModel(f"Product_{i}", i * 1.01) for i in range(999)]
    cfg = ConfigModel(config_version=0, products=products)
    file = tmp_path / "config.json"
    save_config(cfg, str(file))
    loaded_cfg = load_config(str(file))
    codeflash_return_value = codeflash_wrap(
        migrate_config,
        "tests.test_migrate_config__unit_test_1",
        None,
        "test_large_config_file_size",
        "migrate_config",
        "6",
        codeflash_loop_index,
        loaded_cfg,
        str(file),
    )
    codeflash_output = codeflash_return_value
    migrated = codeflash_output
