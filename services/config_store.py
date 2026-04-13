# services/config_store.py
"""
Persists product catalog changes (add/update) back to config.json.

Note: inventory counts are managed by InventoryManager (inventory.json),
not stored in config.json.
"""
from pathlib import Path
from config.config_model import ConfigModel, Product
from loguru import logger

CONFIG_PATH = Path("config.json")


def save_config(config: ConfigModel, path: Path = CONFIG_PATH):
    path.write_text(config.model_dump_json(indent=2))


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
