"""Permissive truth table and payment/enable publishing."""

from config.config_model import Product
from services.availability import Availability


class Recorder:
    def __init__(self):
        self.events = []

    def record(self, event_type, value=1.0, metadata=None):
        self.events.append((event_type, value, metadata))


def _all_good(avail: Availability) -> None:
    avail.set_mqtt_connected(True)
    avail.set_subsystem_alive("vending", True)
    avail.set_subsystem_alive("mdb", True)
    avail.set_subsystem_alive("ice_maker", True)
    avail.set_payment_device("coin_acceptor", "ready")
    avail.set_fsm_state("idle")
    avail.set_hardware_io("bin_half_full", True)


def _avail(products=None):
    if products is None:
        products = [
            Product(sku="ICE-1", name="Ice", kind="ice"),
            Product(sku="WTR-1", name="Water", kind="water"),
        ]
    published: list[bool] = []
    a = Availability(products)
    a.set_publisher(published.append)
    return a, published


def test_everything_unknown_at_start_blocks_payment_and_publishes_false():
    a, published = _avail()
    assert a.payment_enabled is False
    assert published == [False]
    ok, failing = a.sale_available("ice")
    assert ok is False
    assert "vending_alive" in failing


def test_all_known_inputs_pass_enables_and_publishes_once():
    a, published = _avail()
    _all_good(a)
    assert a.payment_enabled is True
    assert published == [False, True]
    # repeating a setter with the same value publishes nothing new
    a.set_fsm_state("idle")
    assert published == [False, True]


def test_not_instrumented_rows_pass_and_are_labelled():
    a, _ = _avail()
    rows = {r["name"]: r for r in a.table()}
    assert rows["bag_present"]["instrumented"] is False
    assert rows["bag_present"]["state"] == "pass"
    assert rows["bag_present"]["detail"] == "not instrumented"


def test_ice_only_failure_keeps_water_selling():
    a, published = _avail()
    _all_good(a)
    a.set_subsystem_alive("ice_maker", False)
    assert a.sale_available("ice")[0] is False
    assert a.sale_available("water")[0] is True
    assert a.payment_enabled is True
    assert published == [False, True]


def test_vending_loss_disables_everything():
    a, published = _avail()
    _all_good(a)
    a.set_subsystem_alive("vending", False)
    assert a.payment_enabled is False
    assert published == [False, True, False]
    assert a.blocking_reasons() == ["vending_alive"]


def test_lockout_on_every_product_disables_payment():
    a, _ = _avail()
    _all_good(a)
    a.set_active_faults(
        [
            {
                "key": "ICE-1",
                "sku": "ICE-1",
                "code": "ICE-301",
                "severity": "lockout",
                "scope": "product",
            },
            {
                "key": "WTR-1",
                "sku": "WTR-1",
                "code": "WTR-102",
                "severity": "lockout",
                "scope": "product",
            },
        ]
    )
    assert a.payment_enabled is False
    ok, failing = a.product_sellable(Product(sku="ICE-1", kind="ice"))
    assert ok is False and "lockout:ICE-301" in failing


def test_ice_101_fault_and_bin_empty_fail_ice_available():
    a, _ = _avail()
    _all_good(a)
    a.set_active_faults(
        [
            {
                "key": "ICE-1",
                "sku": "ICE-1",
                "code": "ICE-101",
                "severity": "product_unavailable",
                "scope": "product",
            }
        ]
    )
    assert "ice_available" in a.sale_available("ice")[1]
    a.set_active_faults([])
    assert a.sale_available("ice")[0] is True
    a.set_hardware_io("bin_half_full", False)
    assert "ice_available" in a.sale_available("ice")[1]


def test_machine_critical_fault_blocks_all():
    a, _ = _avail()
    _all_good(a)
    a.set_active_faults(
        [
            {
                "key": "WTR-104",
                "sku": None,
                "code": "WTR-104",
                "severity": "critical",
                "scope": "machine",
            }
        ]
    )
    assert a.payment_enabled is False
    assert "no_critical_fault" in a.blocking_reasons()


def test_service_door_open_blocks_and_closing_restores():
    a, _ = _avail()
    _all_good(a)
    a.set_hardware_io("service_door", True)
    assert a.payment_enabled is False
    a.set_hardware_io("service_door", False)
    assert a.payment_enabled is True


def test_payment_device_error_blocks():
    a, _ = _avail()
    _all_good(a)
    a.set_payment_device("card_reader", "error")
    assert "payment_devices_ready" in a.blocking_reasons()
    a.set_payment_device("card_reader", "ready")
    assert a.payment_enabled is True


def test_fsm_error_and_uncertain_transaction_block():
    a, _ = _avail()
    _all_good(a)
    a.set_fsm_state("error")
    assert a.payment_enabled is False
    a.set_fsm_state("idle")
    a.set_transaction_certain(False)
    assert "transaction_certain" in a.blocking_reasons()


def test_other_kind_needs_every_permissive():
    a, _ = _avail([Product(sku="X", kind="other")])
    _all_good(a)
    a.set_subsystem_alive("ice_maker", False)
    assert a.payment_enabled is False


def test_no_products_never_enables():
    a, published = _avail([])
    _all_good(a)
    assert a.payment_enabled is False
    assert a.blocking_reasons() == ["no products"]


def test_republish_sends_current_value_unconditionally():
    a, published = _avail()
    _all_good(a)
    a.republish()
    assert published == [False, True, True]


def test_change_is_recorded():
    a, _ = _avail()
    rec = Recorder()
    a.set_event_recorder(rec)
    _all_good(a)
    assert rec.events[-1][0] == "availability_changed"
    assert rec.events[-1][2]["enabled"] is True


def test_set_products_reevaluates():
    a, published = _avail([Product(sku="ICE-1", kind="ice")])
    _all_good(a)
    a.set_subsystem_alive("ice_maker", False)
    assert a.payment_enabled is False
    a.set_products(
        [Product(sku="ICE-1", kind="ice"), Product(sku="WTR-1", kind="water")]
    )
    assert a.payment_enabled is True
