# services/config_store.py
from pathlib import Path
from config.config_model import ConfigModel, Product

CONFIG_PATH = Path("config.json")

def save_config(config: ConfigModel, path: Path = CONFIG_PATH):
    path.write_text(config.model_dump_json(indent=2))


def update_product(
    config: ConfigModel, 
    sku: str, 
    name: str, 
    price: float, 
    inventory_count: int
) -> bool:
    for p in config.products:
        if p.sku == sku:
            p.name = name
            p.price = price
            p.inventory_count = inventory_count
            save_config(config)
            return True
    return False
