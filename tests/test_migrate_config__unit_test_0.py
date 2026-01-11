from __future__ import annotations

import gc
import inspect
import json
import os
import random
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, List

import dill as pickle

from config.config_test import migrate_config, save_config


def codeflash_wrap(
    wrapped: Callable[..., Any],
    test_module_name: str,
    test_class_name: str | None,
    test_name: str,
    function_name: str,
    line_id: str,
    loop_index: int,
    *args: Any,
    **kwargs: Any,
) -> Any:
    test_id = f"{test_module_name}:{test_class_name}:{test_name}:{line_id}:{loop_index}"
    if not hasattr(codeflash_wrap, "index"):
        codeflash_wrap.index = {}
    if test_id in codeflash_wrap.index:
        codeflash_wrap.index[test_id] += 1
    else:
        codeflash_wrap.index[test_id] = 0
    codeflash_test_index = codeflash_wrap.index[test_id]
    invocation_id = f"{line_id}_{codeflash_test_index}"
    print(
        f"!######{test_module_name}:{(test_class_name + '.' if test_class_name else '')}{test_name}:{function_name}:{loop_index}:{invocation_id}######!"
    )
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
    iteration = os.environ["CODEFLASH_TEST_ITERATION"]
    with Path("C:/Users/Conrad/AppData/Local/Temp/codeflash_e6g7_r_a", f"test_return_values_{iteration}.bin").open(
        "ab"
    ) as f:
        pickled_values = (
            pickle.dumps((args, kwargs, exception)) if exception else pickle.dumps((args, kwargs, return_value))
        )
        _test_name = f"{test_module_name}:{(test_class_name + '.' if test_class_name else '')}{test_name}:{function_name}:{line_id}".encode(
            "ascii"
        )
        f.write(len(_test_name).to_bytes(4, byteorder="big"))
        f.write(_test_name)
        f.write(codeflash_duration.to_bytes(8, byteorder="big"))
        f.write(len(pickled_values).to_bytes(4, byteorder="big"))
        f.write(pickled_values)
        f.write(loop_index.to_bytes(8, byteorder="big"))
        f.write(len(invocation_id).to_bytes(4, byteorder="big"))
        f.write(invocation_id.encode("ascii"))
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


def load_config_from_file(filepath):
    with open(filepath, "r") as f:
        data = json.load(f)
    return data


def test_no_migration_needed(tmp_path):
    random.seed(42)
    codeflash_loop_index = int(os.environ["CODEFLASH_LOOP_INDEX"])
    "Test when config version matches default, no migration occurs and file is not changed."
    cfg = ConfigModel(config_version=1, products=[ProductModel("Gadget", 19.99)])
    file = tmp_path / "config.json"
    save_config(cfg, str(file))
    loaded_cfg = ConfigModel(config_version=1, products=[ProductModel("Gadget", 19.99)])
    _call__bound__arguments = inspect.signature(migrate_config).bind(loaded_cfg, str(file))
    _call__bound__arguments.apply_defaults()
    codeflash_return_value = codeflash_wrap(
        migrate_config,
        "tests.test_migrate_config__unit_test_0",
        None,
        "test_no_migration_needed",
        "migrate_config",
        "5",
        codeflash_loop_index,
        **_call__bound__arguments.arguments,
    )
    codeflash_output = codeflash_return_value
    result = codeflash_output
    file_data = load_config_from_file(str(file))


def test_migration_performs_version_update(tmp_path):
    random.seed(42)
    codeflash_loop_index = int(os.environ["CODEFLASH_LOOP_INDEX"])
    "Test that migrate_config updates config_version when needed and saves to file."
    old_version = 0
    cfg = ConfigModel(config_version=old_version, products=[ProductModel("Widget", 9.99)])
    file = tmp_path / "config.json"
    save_config(cfg, str(file))
    _call__bound__arguments = inspect.signature(migrate_config).bind(cfg, str(file))
    _call__bound__arguments.apply_defaults()
    codeflash_return_value = codeflash_wrap(
        migrate_config,
        "tests.test_migrate_config__unit_test_0",
        None,
        "test_migration_performs_version_update",
        "migrate_config",
        "5",
        codeflash_loop_index,
        **_call__bound__arguments.arguments,
    )
    codeflash_output = codeflash_return_value
    result = codeflash_output
    file_data = load_config_from_file(str(file))


def test_migration_preserves_other_fields(tmp_path):
    random.seed(42)
    codeflash_loop_index = int(os.environ["CODEFLASH_LOOP_INDEX"])
    "Test that migration only updates version and leaves other fields unchanged."
    cfg = ConfigModel(config_version=0, products=[ProductModel("Special", 123.45)])
    file = tmp_path / "config.json"
    save_config(cfg, str(file))
    _call__bound__arguments = inspect.signature(migrate_config).bind(cfg, str(file))
    _call__bound__arguments.apply_defaults()
    codeflash_return_value = codeflash_wrap(
        migrate_config,
        "tests.test_migrate_config__unit_test_0",
        None,
        "test_migration_preserves_other_fields",
        "migrate_config",
        "4",
        codeflash_loop_index,
        **_call__bound__arguments.arguments,
    )
    codeflash_output = codeflash_return_value
    result = codeflash_output
    file_data = load_config_from_file(str(file))


def test_migration_with_negative_version(tmp_path):
    random.seed(42)
    codeflash_loop_index = int(os.environ["CODEFLASH_LOOP_INDEX"])
    "Test migration from a negative config_version."
    cfg = ConfigModel(config_version=-5, products=[ProductModel("Neg", 1.23)])
    file = tmp_path / "config.json"
    save_config(cfg, str(file))
    _call__bound__arguments = inspect.signature(migrate_config).bind(cfg, str(file))
    _call__bound__arguments.apply_defaults()
    codeflash_return_value = codeflash_wrap(
        migrate_config,
        "tests.test_migrate_config__unit_test_0",
        None,
        "test_migration_with_negative_version",
        "migrate_config",
        "4",
        codeflash_loop_index,
        **_call__bound__arguments.arguments,
    )
    codeflash_output = codeflash_return_value
    result = codeflash_output
    file_data = load_config_from_file(str(file))


def test_migration_with_large_version_number(tmp_path):
    random.seed(42)
    codeflash_loop_index = int(os.environ["CODEFLASH_LOOP_INDEX"])
    "Test migration from a much higher version than default."
    cfg = ConfigModel(config_version=999, products=[ProductModel("Future", 42.0)])
    file = tmp_path / "config.json"
    save_config(cfg, str(file))
    _call__bound__arguments = inspect.signature(migrate_config).bind(cfg, str(file))
    _call__bound__arguments.apply_defaults()
    codeflash_return_value = codeflash_wrap(
        migrate_config,
        "tests.test_migrate_config__unit_test_0",
        None,
        "test_migration_with_large_version_number",
        "migrate_config",
        "4",
        codeflash_loop_index,
        **_call__bound__arguments.arguments,
    )
    codeflash_output = codeflash_return_value
    result = codeflash_output
    file_data = load_config_from_file(str(file))


def test_migration_with_empty_products(tmp_path):
    random.seed(42)
    codeflash_loop_index = int(os.environ["CODEFLASH_LOOP_INDEX"])
    "Test migration when products list is empty."
    cfg = ConfigModel(config_version=0, products=[])
    file = tmp_path / "config.json"
    save_config(cfg, str(file))
    _call__bound__arguments = inspect.signature(migrate_config).bind(cfg, str(file))
    _call__bound__arguments.apply_defaults()
    codeflash_return_value = codeflash_wrap(
        migrate_config,
        "tests.test_migrate_config__unit_test_0",
        None,
        "test_migration_with_empty_products",
        "migrate_config",
        "4",
        codeflash_loop_index,
        **_call__bound__arguments.arguments,
    )
    codeflash_output = codeflash_return_value
    result = codeflash_output
    file_data = load_config_from_file(str(file))


def test_migration_with_non_ascii_product_names(tmp_path):
    random.seed(42)
    codeflash_loop_index = int(os.environ["CODEFLASH_LOOP_INDEX"])
    "Test migration with non-ASCII product names."
    cfg = ConfigModel(
        config_version=0,
        products=[ProductModel("Café", 3.5), ProductModel("Товар", 4.2)],
    )
    file = tmp_path / "config.json"
    save_config(cfg, str(file))
    _call__bound__arguments = inspect.signature(migrate_config).bind(cfg, str(file))
    _call__bound__arguments.apply_defaults()
    codeflash_return_value = codeflash_wrap(
        migrate_config,
        "tests.test_migrate_config__unit_test_0",
        None,
        "test_migration_with_non_ascii_product_names",
        "migrate_config",
        "4",
        codeflash_loop_index,
        **_call__bound__arguments.arguments,
    )
    codeflash_output = codeflash_return_value
    result = codeflash_output
    file_data = load_config_from_file(str(file))


def test_migration_with_float_precision(tmp_path):
    random.seed(42)
    codeflash_loop_index = int(os.environ["CODEFLASH_LOOP_INDEX"])
    "Test migration preserves float precision in product prices."
    cfg = ConfigModel(config_version=0, products=[ProductModel("Precise", 0.123456789)])
    file = tmp_path / "config.json"
    save_config(cfg, str(file))
    _call__bound__arguments = inspect.signature(migrate_config).bind(cfg, str(file))
    _call__bound__arguments.apply_defaults()
    codeflash_return_value = codeflash_wrap(
        migrate_config,
        "tests.test_migrate_config__unit_test_0",
        None,
        "test_migration_with_float_precision",
        "migrate_config",
        "4",
        codeflash_loop_index,
        **_call__bound__arguments.arguments,
    )
    codeflash_output = codeflash_return_value
    result = codeflash_output
    file_data = load_config_from_file(str(file))


def test_migration_with_no_products_field(tmp_path):
    random.seed(42)
    codeflash_loop_index = int(os.environ["CODEFLASH_LOOP_INDEX"])
    "Test migration when products field is missing (simulate by direct dict manipulation)."
    file = tmp_path / "config.json"
    with open(file, "w") as f:
        json.dump({"config_version": 0}, f)
    loaded = load_config_from_file(str(file))
    cfg = ConfigModel(
        config_version=loaded.get("config_version", 0),
        products=loaded.get("products", []),
    )
    _call__bound__arguments = inspect.signature(migrate_config).bind(cfg, str(file))
    _call__bound__arguments.apply_defaults()
    codeflash_return_value = codeflash_wrap(
        migrate_config,
        "tests.test_migrate_config__unit_test_0",
        None,
        "test_migration_with_no_products_field",
        "migrate_config",
        "5",
        codeflash_loop_index,
        **_call__bound__arguments.arguments,
    )
    codeflash_output = codeflash_return_value
    result = codeflash_output
    file_data = load_config_from_file(str(file))


def test_migration_with_none_fields(tmp_path):
    random.seed(42)
    codeflash_loop_index = int(os.environ["CODEFLASH_LOOP_INDEX"])
    "Test migration with None as products (should default to empty list)."
    cfg = ConfigModel(config_version=0, products=None)
    file = tmp_path / "config.json"
    orig_model_dump = cfg.model_dump

    def patched_model_dump():
        return {
            "config_version": cfg.config_version,
            "products": ([] if cfg.products is None else [{"name": p.name, "price": p.price} for p in cfg.products]),
        }

    cfg.model_dump = patched_model_dump
    save_config(cfg, str(file))
    _call__bound__arguments = inspect.signature(migrate_config).bind(cfg, str(file))
    _call__bound__arguments.apply_defaults()
    codeflash_return_value = codeflash_wrap(
        migrate_config,
        "tests.test_migrate_config__unit_test_0",
        None,
        "test_migration_with_none_fields",
        "migrate_config",
        "7",
        codeflash_loop_index,
        **_call__bound__arguments.arguments,
    )
    codeflash_output = codeflash_return_value
    result = codeflash_output
    file_data = load_config_from_file(str(file))


def test_migration_large_number_of_products(tmp_path):
    random.seed(42)
    codeflash_loop_index = int(os.environ["CODEFLASH_LOOP_INDEX"])
    "Test migration with a large products list (1000 items)."
    N = 1000
    products = [ProductModel(f"Item{i}", float(i)) for i in range(N)]
    cfg = ConfigModel(config_version=0, products=products)
    file = tmp_path / "config.json"
    save_config(cfg, str(file))
    _call__bound__arguments = inspect.signature(migrate_config).bind(cfg, str(file))
    _call__bound__arguments.apply_defaults()
    codeflash_return_value = codeflash_wrap(
        migrate_config,
        "tests.test_migrate_config__unit_test_0",
        None,
        "test_migration_large_number_of_products",
        "migrate_config",
        "6",
        codeflash_loop_index,
        **_call__bound__arguments.arguments,
    )
    codeflash_output = codeflash_return_value
    result = codeflash_output
    for i in range(0, N, 100):
        pass
    file_data = load_config_from_file(str(file))
    for i in range(0, N, 100):
        pass


def test_migration_multiple_runs_idempotency(tmp_path):
    random.seed(42)
    codeflash_loop_index = int(os.environ["CODEFLASH_LOOP_INDEX"])
    "Test that running migrate_config multiple times does not change config after first migration."
    cfg = ConfigModel(config_version=0, products=[ProductModel("First", 1.0)])
    file = tmp_path / "config.json"
    save_config(cfg, str(file))
    _call__bound__arguments = inspect.signature(migrate_config).bind(cfg, str(file))
    _call__bound__arguments.apply_defaults()
    codeflash_return_value = codeflash_wrap(
        migrate_config,
        "tests.test_migrate_config__unit_test_0",
        None,
        "test_migration_multiple_runs_idempotency",
        "migrate_config",
        "4",
        codeflash_loop_index,
        **_call__bound__arguments.arguments,
    )
    codeflash_output = codeflash_return_value
    result1 = codeflash_output
    _call__bound__arguments = inspect.signature(migrate_config).bind(result1, str(file))
    _call__bound__arguments.apply_defaults()
    codeflash_return_value = codeflash_wrap(
        migrate_config,
        "tests.test_migrate_config__unit_test_0",
        None,
        "test_migration_multiple_runs_idempotency",
        "migrate_config",
        "6",
        codeflash_loop_index,
        **_call__bound__arguments.arguments,
    )
    codeflash_output = codeflash_return_value
    result2 = codeflash_output
    file_data = load_config_from_file(str(file))


def test_migration_performance_large_config(tmp_path):
    random.seed(42)
    codeflash_loop_index = int(os.environ["CODEFLASH_LOOP_INDEX"])
    "Test that migration completes in reasonable time for large config (under 1000 products)."
    import time

    random.seed(42)
    N = 999
    products = [ProductModel(f"Bulk{i}", i * 0.01) for i in range(N)]
    cfg = ConfigModel(config_version=0, products=products)
    file = tmp_path / "config.json"
    save_config(cfg, str(file))
    start = time.time()
    _call__bound__arguments = inspect.signature(migrate_config).bind(cfg, str(file))
    _call__bound__arguments.apply_defaults()
    codeflash_return_value = codeflash_wrap(
        migrate_config,
        "tests.test_migrate_config__unit_test_0",
        None,
        "test_migration_performance_large_config",
        "migrate_config",
        "8",
        codeflash_loop_index,
        **_call__bound__arguments.arguments,
    )
    codeflash_output = codeflash_return_value
    result = codeflash_output
    elapsed = time.time() - start
