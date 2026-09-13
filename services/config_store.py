# services/config_store.py
"""
Persists product catalog changes (add/update) back to config.json.

Saves are atomic (write-to-tmp + os.replace) and keep one rolling
``config.json.bak`` of the previous version. SecretStr fields are written
with their real values so a save never destroys stored credentials.

Note: inventory counts are managed by InventoryManager (inventory.json),
not stored in config.json.
"""

import json
import os
import shutil
from pathlib import Path

from loguru import logger
from pydantic import SecretStr

from config.config_model import ConfigModel, Product

CONFIG_PATH = Path("config.json")


def _config_json(config: ConfigModel) -> str:
    """Serialize the config with real secret values (not masked)."""
    data = config.model_dump(mode="python")

    def _encode(obj):
        if isinstance(obj, SecretStr):
            return obj.get_secret_value()
        raise TypeError(f"Not JSON serializable: {type(obj)!r}")

    return json.dumps(data, indent=2, default=_encode)


def save_config(config: ConfigModel, path: Path | None = None):
    """Atomically write the config, keeping a rolling ``<name>.bak``."""
    if path is None:
        path = CONFIG_PATH
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(_config_json(config), encoding="utf-8")
    if path.exists():
        shutil.copy2(path, path.with_name(path.name + ".bak"))
    os.replace(tmp, path)


def add_product(
    config: ConfigModel,
    sku: str,
    name: str,
    price: float,
) -> bool:
    if any(p.sku == sku for p in config.products):
        logger.warning(f"Cannot add product: SKU '{sku}' already exists")
        return False

    new_product = Product(sku=sku, name=name, price=price)
    config.products.append(new_product)
    save_config(config)
    logger.info(f"Added product SKU={sku} | name='{name}', price={price}")
    return True


def update_product(
    config: ConfigModel,
    sku: str,
    name: str,
    price: float,
) -> bool:
    for p in config.products:
        if p.sku == sku:
            changes = {}
            if p.name != name:
                changes["name"] = (p.name, name)
            if p.price != price:
                changes["price"] = (p.price, price)

            if changes:
                p.name = name
                p.price = price
                save_config(config)
                change_summary = ", ".join(
                    f"{field}: {old!r} -> {new!r}"
                    for field, (old, new) in changes.items()
                )
                logger.info(f"Updated product SKU={sku} | {change_summary}")
            else:
                logger.debug(f"No changes for SKU={sku}; skipping save.")

            return True

    logger.warning(f"SKU not found: {sku}")
    return False


def delete_product(config: ConfigModel, sku: str) -> bool:
    for i, p in enumerate(config.products):
        if p.sku == sku:
            del config.products[i]
            save_config(config)
            logger.info(f"Deleted product SKU={sku} | name='{p.name}'")
            return True

    logger.warning(f"Cannot delete product: SKU not found: {sku}")
    return False
