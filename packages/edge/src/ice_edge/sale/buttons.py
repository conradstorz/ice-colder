"""Customer-button lookup: which catalog product a button id selects."""

from collections.abc import Iterable

from ice_common.config_model import Product


def product_for_button(products: Iterable[Product], button: str) -> Product | None:
    """The product whose ``button`` is ``button``, or ``None`` if unmapped."""
    return next((p for p in products if p.button == button), None)
