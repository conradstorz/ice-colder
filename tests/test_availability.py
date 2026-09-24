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


def test_unknown_inputs_at_start_leave_payment_on_but_block_sales():
    a, published = _avail()
    assert a.payment_enabled is True
    assert published == [True]
    ok, failing = a.sale_available("ice")
    assert ok is False
    assert "vending_alive" in failing


def test_all_known_inputs_pass_and_publish_only_once():
    a, published = _avail()
    _all_good(a)
    assert a.payment_enabled is True
    assert a.sale_available("ice")[0] is True
    # payment was already on; no safety row changed, so nothing new is published
    assert published == [True]
    a.set_fsm_state("idle")
    assert published == [True]


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
    assert published == [True]


def test_vending_loss_stops_sales_but_keeps_payment_on():
    a, published = _avail()
    _all_good(a)
    a.set_subsystem_alive("vending", False)
    assert a.payment_enabled is True
    assert published == [True]
    assert a.payment_blocking_reasons() == []
    assert "vending_alive" in a.sale_available("ice")[1]
    assert "vending_alive" in a.sale_available("water")[1]


def test_lockout_on_every_product_blocks_selection_not_payment():
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
    assert a.payment_enabled is True
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
    assert "no_critical_fault" in a.payment_blocking_reasons()


def test_service_door_open_blocks_and_closing_restores():
    a, _ = _avail()
    _all_good(a)
    a.set_hardware_io("service_door", True)
    assert a.payment_enabled is False
    a.set_hardware_io("service_door", False)
    assert a.payment_enabled is True


def test_payment_device_error_blocks_the_sale_not_payment():
    a, _ = _avail()
    _all_good(a)
    a.set_payment_device("card_reader", "error")
    assert "payment_devices_ready" in a.sale_available("ice")[1]
    assert a.payment_enabled is True
    a.set_payment_device("card_reader", "ready")
    assert a.sale_available("ice")[0] is True


def test_fsm_error_blocks_the_sale_and_uncertain_transaction_blocks_nothing():
    a, _ = _avail()
    _all_good(a)
    a.set_fsm_state("error")
    assert a.payment_enabled is True
    assert "fsm_ok" in a.sale_available("ice")[1]
    a.set_fsm_state("idle")
    a.set_transaction_certain(False)
    assert a.payment_enabled is True
    assert a.sale_available("ice")[0] is True
    rows = {r["name"]: r for r in a.table()}
    assert rows["transaction_certain"]["state"] == "fail"
    assert rows["transaction_certain"]["detail"] == "PAY-104 active"


def test_other_kind_needs_every_permissive_to_sell():
    a, _ = _avail([Product(sku="X", kind="other")])
    _all_good(a)
    a.set_subsystem_alive("ice_maker", False)
    assert a.payment_enabled is True
    assert a.sale_available("other")[0] is False
    assert a.product_sellable(Product(sku="X", kind="other"))[0] is False


def test_no_products_still_allows_payment():
    a, published = _avail([])
    _all_good(a)
    assert a.payment_enabled is True
    assert a.payment_blocking_reasons() == []
    assert published == [True]


def test_republish_sends_current_value_unconditionally():
    a, published = _avail()
    _all_good(a)
    a.republish()
    assert published == [True, True]


def test_change_is_recorded():
    a, _ = _avail()
    rec = Recorder()
    a.set_event_recorder(rec)
    _all_good(a)
    a.set_hardware_io("service_door", True)
    assert rec.events[-1][0] == "availability_changed"
    assert rec.events[-1][2]["enabled"] is False


def test_set_products_reevaluates():
    a, published = _avail([Product(sku="ICE-1", kind="ice")])
    _all_good(a)
    a.set_subsystem_alive("ice_maker", False)
    assert a.payment_enabled is True
    a.set_products(
        [Product(sku="ICE-1", kind="ice"), Product(sku="WTR-1", kind="water")]
    )
    assert a.payment_enabled is True


def test_gate_is_exported_on_every_row():
    a, _ = _avail()
    rows = {r["name"]: r for r in a.table()}
    assert rows["no_critical_fault"]["gate"] == "safety"
    assert rows["service_door_closed"]["gate"] == "safety"
    assert rows["no_leak"]["gate"] == "safety"
    assert rows["water_valve_closed"]["gate"] == "safety"
    assert rows["trap_door_closed"]["gate"] == "safety"
    assert rows["control_power_ok"]["gate"] == "safety"
    assert rows["transaction_certain"]["gate"] == "alert"
    assert rows["vending_alive"]["gate"] == "fulfillment"
    assert rows["mqtt_connected"]["gate"] == "fulfillment"
    assert rows["payment_alive"]["gate"] == "fulfillment"
    assert rows["payment_devices_ready"]["gate"] == "fulfillment"
    assert rows["ice_maker_alive"]["gate"] == "fulfillment"
    assert rows["ice_available"]["gate"] == "fulfillment"
    assert rows["fsm_ok"]["gate"] == "fulfillment"
    assert rows["bag_present"]["gate"] == "fulfillment"
    assert rows["water_pressure_ok"]["gate"] == "fulfillment"
    assert rows["water_treatment_ok"]["gate"] == "fulfillment"
    assert all("gate" in r for r in a.table())


def _machine_fault(code: str, severity: str = "critical") -> dict:
    return {
        "key": code,
        "sku": None,
        "code": code,
        "severity": severity,
        "scope": "machine",
    }


def test_each_payment_blocking_fault_disables_payment():
    from contracts.vending_machine import PAYMENT_BLOCKING_FAULTS

    for fault in PAYMENT_BLOCKING_FAULTS:
        a, _ = _avail()
        _all_good(a)
        assert a.payment_enabled is True
        a.set_active_faults([_machine_fault(fault.value)])
        assert a.payment_enabled is False, fault
        assert a.payment_blocking_reasons() == ["no_critical_fault"]
        a.set_active_faults([])
        assert a.payment_enabled is True, fault


def test_pay_104_does_not_disable_payment():
    a, _ = _avail()
    _all_good(a)
    a.set_active_faults([_machine_fault("PAY-104", severity="warning")])
    a.set_transaction_certain(False)
    assert a.payment_enabled is True
    assert a.payment_blocking_reasons() == []
    assert a.sale_available("ice")[0] is True


def test_machine_fault_outside_the_whitelist_does_not_disable_payment():
    a, _ = _avail()
    _all_good(a)
    a.set_active_faults([_machine_fault("COM-103", severity="critical")])
    assert a.payment_enabled is True
    assert a.payment_blocking_reasons() == []


def test_every_fulfillment_row_blocks_the_sale_but_not_payment():
    setters = [
        ("mqtt_connected", lambda a: a.set_mqtt_connected(False)),
        ("vending_alive", lambda a: a.set_subsystem_alive("vending", False)),
        ("payment_alive", lambda a: a.set_subsystem_alive("mdb", False)),
        (
            "payment_devices_ready",
            lambda a: a.set_payment_device("card_reader", "error"),
        ),
        ("fsm_ok", lambda a: a.set_fsm_state("error")),
    ]
    for name, apply in setters:
        a, _ = _avail()
        _all_good(a)
        apply(a)
        assert a.payment_enabled is True, name
        assert a.payment_blocking_reasons() == [], name
        ok, failing = a.sale_available("ice")
        assert ok is False, name
        assert name in failing, name


def test_ice_maker_loss_blocks_only_ice_and_never_payment():
    a, _ = _avail()
    _all_good(a)
    a.set_subsystem_alive("ice_maker", False)
    assert a.payment_enabled is True
    assert a.sale_available("ice")[0] is False
    assert a.sale_available("water")[0] is True
