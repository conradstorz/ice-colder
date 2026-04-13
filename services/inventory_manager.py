# services/inventory_manager.py
"""
Persistent inventory tracking backed by inventory.json.

Inventory counts are seeded from config Product.inventory_count on first run
or when new products appear. Runtime counts survive restarts independently
of config.json.
"""
import json
import os
from pathlib import Path

from loguru import logger

INVENTORY_PATH = Path("inventory.json")


class InventoryManager:
    """
    Tracks per-SKU inventory counts with file persistence.

    Usage:
        inv = InventoryManager(products, path="inventory.json")
        if inv.is_available("ICE-SM"):
            inv.decrement("ICE-SM")
    """

    def __init__(self, products: list, path: Path = INVENTORY_PATH):
        self._path = path
        self._counts: dict[str, int] = {}
        self._track: dict[str, bool] = {}
        self._load(products)

    def _load(self, products: list):
        """Load from file, seeding missing SKUs from config products."""
        saved: dict[str, int] = {}
        if self._path.exists():
            try:
                with open(self._path, encoding="utf-8") as f:
                    saved = json.load(f)
                logger.info(f"Inventory loaded from {self._path} ({len(saved)} SKUs)")
            except Exception as e:
                logger.error(f"Failed to read {self._path}: {e}; starting fresh")

        for product in products:
            sku = product.sku
            self._track[sku] = product.track_inventory
            if sku in saved:
                self._counts[sku] = saved[sku]
            else:
                self._counts[sku] = product.inventory_count
                logger.info(f"Inventory: seeded {sku} with {product.inventory_count} from config")

        self._save()

    def _save(self):
        """Persist current counts to disk atomically."""
        tmp = f"{self._path}.tmp"
        try:
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(self._counts, f, indent=2)
            os.replace(tmp, self._path)
        except Exception as e:
            logger.error(f"Failed to save inventory: {e}")

    def get_count(self, sku: str) -> int:
        """Return current inventory count for a SKU."""
        return self._counts.get(sku, 0)

    def is_tracked(self, sku: str) -> bool:
        """Return whether this SKU has inventory tracking enabled."""
        return self._track.get(sku, False)

    def is_available(self, sku: str) -> bool:
        """Return True if the SKU is not tracked or has stock remaining."""
        if not self.is_tracked(sku):
            return True
        return self._counts.get(sku, 0) > 0

    def decrement(self, sku: str):
        """Decrement inventory for a SKU and persist."""
        if sku in self._counts:
            self._counts[sku] = max(0, self._counts[sku] - 1)
            logger.info(f"Inventory: {sku} decremented to {self._counts[sku]}")
            self._save()

    def set_count(self, sku: str, count: int):
        """Set inventory count for a SKU (e.g., from admin dashboard)."""
        self._counts[sku] = count
        self._save()

    def add_sku(self, sku: str, count: int, tracked: bool = False):
        """Register a new SKU with initial count."""
        self._counts[sku] = count
        self._track[sku] = tracked
        self._save()

    def get_all(self) -> dict[str, int]:
        """Return a copy of all inventory counts."""
        return dict(self._counts)
