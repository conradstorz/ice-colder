# services/config_store.py
from pathlib import Path

from loguru import logger

from config.config_model import ConfigModel, Product

CONFIG_PATH = Path("config.json")


def save_config(config: ConfigModel, path: Path = CONFIG_PATH):
    path.write_text(config.model_dump_json(indent=2))


def add_product(config: ConfigModel, sku: str, name: str, price: float, inventory_count: int) -> bool:
    if any(p.sku == sku for p in config.products):
        logger.warning(f"❌ Cannot add product: SKU '{sku}' already exists")
        return False

    new_product = Product(sku=sku, name=name, price=price, inventory_count=inventory_count)
    config.products.append(new_product)
    save_config(config)
    logger.info(f"✅ [INVENTORY] Added new product SKU={sku} | name='{name}', price={price}, stock={inventory_count}")
    return True


def update_product(config: ConfigModel, sku: str, name: str, price: float, inventory_count: int) -> bool:
    for p in config.products:
        if p.sku == sku:
            changes = {}

            if p.name != name:
                changes["name"] = (p.name, name)
            if p.price != price:
                changes["price"] = (p.price, price)
            if p.inventory_count != inventory_count:
                changes["inventory_count"] = (p.inventory_count, inventory_count)

            if changes:
                p.name = name
                p.price = price
                p.inventory_count = inventory_count
                save_config(config)

                change_summary = ", ".join(f"{field}: {old!r} → {new!r}" for field, (old, new) in changes.items())
                logger.info(f"✅ [INVENTORY] SKU={sku} | {change_summary}")
            else:
                logger.debug(f"⏸ No changes for SKU={sku}; skipping save.")

            return True

    logger.warning(f"❓ SKU not found: {sku}")
    return False
