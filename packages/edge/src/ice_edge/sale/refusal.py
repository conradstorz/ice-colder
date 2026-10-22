"""Why a product cannot be sold right now: lockout, availability, or stock."""

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from ice_common.config_model import Product
    from ice_edge.faults.availability import Availability
    from ice_edge.faults.fault_service import FaultService
    from ice_edge.monitoring.inventory_manager import InventoryManager
    from ice_edge.sale.sale_journal import SaleJournal


def refusal(
    candidate: "Product",
    *,
    faults: "FaultService",
    availability: "Availability | None",
    inventory: "InventoryManager | None",
    journal: "SaleJournal",
    is_test: bool,
    check_stock: bool = False,
) -> bool:
    """Journal and message the refusal if `candidate` is locked out,
    unavailable or (with `check_stock`) sold out; True when refused.

    A test sale (`is_test`, never the lease) is exempt from its own SVC-102
    only; a customer press mid-lease is still refused.
    """
    locked_code = faults.lockouts.get(candidate.sku)
    if locked_code is not None:
        journal.locked_out(candidate, locked_code)
        return True
    if availability:
        check = (
            availability.test_sale_sellable
            if is_test
            else availability.product_sellable
        )
        sellable, failing = check(candidate)
        if not sellable:
            journal.unavailable(candidate, failing[0])
            return True
    if check_stock and inventory and not inventory.is_available(candidate.sku):
        journal.sold_out(candidate)
        return True
    return False
