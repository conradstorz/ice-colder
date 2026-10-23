"""Stock bookkeeping for a vend that already happened."""


def draw_stock(inventory, tasks, journal, product) -> None:
    """Take one unit of a tracked product out of stock and persist it off-loop."""
    if inventory and product and inventory.is_tracked(product.sku):
        inventory.decrement(product.sku, persist=False)
        tasks.fire_and_forget(inventory.save_async(), persistent=True)
        journal.inventory_updated(product.name, inventory.get_count(product.sku))
