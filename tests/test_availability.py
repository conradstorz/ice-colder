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


def _avail():
    published: list[bool] = []
    a = Availability()
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
    a, _ = _avail()
    _all_good(a)
    a.set_subsystem_alive("ice_maker", False)
    assert a.payment_enabled is True
    assert a.sale_available("other")[0] is False
    assert a.product_sellable(Product(sku="X", kind="other"))[0] is False


def test_no_products_still_allows_payment():
    a, published = _avail()
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


# --- SVC-102: maintenance lease is a safety row -----------------------------
#
# SVC-102 carries no bespoke code in services/availability.py. Membership in
# contracts.vending_machine.PAYMENT_BLOCKING_FAULTS is the whole gate: raising
# it fails the existing "no_critical_fault" permissive, which is already
# wired with gate=Gate.safety, exactly like the six pre-existing codes. These
# tests exercise that specific code end to end rather than relying only on
# the generic sweep in test_each_payment_blocking_fault_disables_payment.


def test_svc_102_disables_payment_and_publishes_exactly_once():
    a, published = _avail()
    _all_good(a)
    assert published == [True]
    a.set_active_faults([_machine_fault("SVC-102", severity="warning")])
    assert a.payment_enabled is False
    # exactly one new publish: [True] (startup) -> [True, False] (SVC-102 raised)
    assert published == [True, False]


def test_svc_102_clearing_republishes_enable_when_nothing_else_blocks():
    a, published = _avail()
    _all_good(a)
    a.set_active_faults([_machine_fault("SVC-102", severity="warning")])
    assert published == [True, False]
    a.set_active_faults([])
    assert a.payment_enabled is True
    # exactly one further publish on clear: back to True
    assert published == [True, False, True]


def test_svc_102_clear_alone_does_not_reenable_with_second_blocking_fault():
    a, published = _avail()
    _all_good(a)
    a.set_active_faults([_machine_fault("SVC-102", severity="warning")])
    # SVC-102 alone already blocks (also covered by the "exactly once" test
    # above; asserted again here so the next step is meaningful).
    assert a.payment_enabled is False
    assert published == [True, False]
    a.set_active_faults(
        [_machine_fault("SVC-102", severity="warning"), _machine_fault("WTR-104")]
    )
    assert a.payment_enabled is False
    assert published == [True, False]  # already False; no new publish
    # clear SVC-102 alone; WTR-104 is still active
    a.set_active_faults([_machine_fault("WTR-104")])
    assert a.payment_enabled is False
    assert "no_critical_fault" in a.payment_blocking_reasons()
    # payment_enabled was already False and stays False: no new publish
    assert published == [True, False]


def test_svc_102_row_is_safety_gated_when_active():
    a, _ = _avail()
    _all_good(a)
    a.set_active_faults([_machine_fault("SVC-102", severity="warning")])
    rows = {r["name"]: r for r in a.table()}
    assert rows["no_critical_fault"]["gate"] == "safety"
    assert rows["no_critical_fault"]["state"] == "fail"
    assert "SVC-102" in rows["no_critical_fault"]["detail"]
    assert "no_critical_fault" in a.payment_blocking_reasons()


# --- command_inhibited: subsystem-windows design §4.5 -----------------------


def test_command_inhibited_all_good_nothing_is_inhibited():
    a, _ = _avail()
    _all_good(a)
    assert a.command_inhibited("dispense") is False
    assert a.command_inhibited("water_valve") is False
    assert a.command_inhibited("payment/enable") is False
    assert a.command_inhibited("ping") is False
    assert a.command_inhibited("refund") is False


def test_command_inhibited_ice_maker_loss_leaves_dispense_and_water_valve_open():
    # Water can still sell, so dispense (which only needs ONE kind sellable)
    # and water_valve (which only cares about water) are both not inhibited.
    a, _ = _avail()
    _all_good(a)
    a.set_subsystem_alive("ice_maker", False)
    assert a.command_inhibited("dispense") is False
    assert a.command_inhibited("water_valve") is False


def test_command_inhibited_vending_loss_blocks_sales_but_not_payment():
    a, _ = _avail()
    _all_good(a)
    a.set_subsystem_alive("vending", False)
    assert a.command_inhibited("dispense") is True
    assert a.command_inhibited("water_valve") is True
    assert a.command_inhibited("payment/enable") is False


def test_command_inhibited_machine_critical_fault_inhibits_all_three():
    a, _ = _avail()
    _all_good(a)
    a.set_active_faults([_machine_fault("WTR-103")])
    assert a.command_inhibited("dispense") is True
    assert a.command_inhibited("water_valve") is True
    assert a.command_inhibited("payment/enable") is True


def test_command_inhibited_payment_device_error_blocks_sale_not_payment():
    a, _ = _avail()
    _all_good(a)
    a.set_payment_device("card_reader", "error")
    assert a.command_inhibited("dispense") is True
    assert a.command_inhibited("payment/enable") is False


def test_payment_blocking_faults_has_seven_members_including_svc_102():
    from contracts.vending_machine import FaultCode, PAYMENT_BLOCKING_FAULTS

    assert len(PAYMENT_BLOCKING_FAULTS) == 7
    assert FaultCode.SVC_102 in PAYMENT_BLOCKING_FAULTS


def test_pre_existing_six_blocking_codes_are_unchanged_by_svc_102():
    """SVC-102's presence must not alter the other six codes' behaviour.

    Same expectations as test_each_payment_blocking_fault_disables_payment,
    scoped to exactly the six codes that predate SVC-102 (i.e. every member
    of PAYMENT_BLOCKING_FAULTS except SVC-102).
    """
    from contracts.vending_machine import FaultCode, PAYMENT_BLOCKING_FAULTS

    pre_existing = PAYMENT_BLOCKING_FAULTS - {FaultCode.SVC_102}
    assert len(pre_existing) == 6

    for fault in pre_existing:
        a, _ = _avail()
        _all_good(a)
        assert a.payment_enabled is True
        a.set_active_faults([_machine_fault(fault.value)])
        assert a.payment_enabled is False, fault
        assert a.payment_blocking_reasons() == ["no_critical_fault"]
        a.set_active_faults([])
        assert a.payment_enabled is True, fault


# --- test_sale_sellable: SVC-102 is exempt for a TEST sale, and ONLY -------
#
# whole-branch-fix-2: a maintenance test sale's own SVC-102 fault must not
# block it, but every other payment-blocking code must still stop it exactly
# like a real sale, and payment_enabled must never be affected either way.
# Production path reached: services/availability.py's Availability.
# test_sale_sellable / product_sellable(ignore_faults=...) / sale_available
# (ignore_faults=...) -- the same methods controller/vmc.py's
# VMC.select_product calls when self._sale_is_test is True.


def test_test_sale_sellable_ignores_svc_102_alone():
    a, _ = _avail()
    _all_good(a)
    a.set_active_faults([_machine_fault("SVC-102", severity="warning")])
    # payment_enabled reflects the real, unexempted row: still inhibited.
    assert a.payment_enabled is False
    assert a.payment_blocking_reasons() == ["no_critical_fault"]

    product = Product(sku="ICE-1", kind="ice")
    ok, failing = a.product_sellable(product)
    assert ok is False
    assert "no_critical_fault" in failing

    ok, failing = a.test_sale_sellable(product)
    assert ok is True
    assert failing == []


def test_test_sale_sellable_still_blocked_by_a_genuinely_unsafe_fault():
    """SVC-102 plus a real hazard: the exemption must not paper over the
    hazard just because a lease also happens to be held."""
    a, _ = _avail()
    _all_good(a)
    a.set_active_faults(
        [
            _machine_fault("SVC-102", severity="warning"),
            _machine_fault("WTR-104"),
        ]
    )
    assert a.payment_enabled is False

    product = Product(sku="WATER-1", kind="water")
    ok, failing = a.test_sale_sellable(product)
    assert ok is False
    assert "no_critical_fault" in failing


def test_test_sale_sellable_each_non_svc_102_blocking_fault_still_blocks_alone():
    """Every OTHER payment-blocking code, on its own (no SVC-102 at all),
    still blocks a test sale -- the exemption is scoped to SVC-102, not to
    "any sale requested through test_sale_sellable"."""
    from contracts.vending_machine import FaultCode, PAYMENT_BLOCKING_FAULTS

    pre_existing = PAYMENT_BLOCKING_FAULTS - {FaultCode.SVC_102}
    assert len(pre_existing) > 0
    product = Product(sku="ICE-1", kind="ice")
    for fault in pre_existing:
        a, _ = _avail()
        _all_good(a)
        a.set_active_faults([_machine_fault(fault.value)])
        ok, failing = a.test_sale_sellable(product)
        assert ok is False, fault
        assert "no_critical_fault" in failing, fault


def test_svc_102_exemption_does_not_leak_into_ignoreless_calls():
    """sale_available/product_sellable called with the default empty
    ignore_faults (every existing caller, and product_sellable's own
    production use for a non-test sale) are completely unaffected by
    test_sale_sellable existing at all."""
    a, _ = _avail()
    _all_good(a)
    a.set_active_faults([_machine_fault("SVC-102", severity="warning")])
    assert a.sale_available("ice") == (False, ["no_critical_fault"])
    ok, failing = a.product_sellable(Product(sku="ICE-1", kind="ice"))
    assert ok is False
    assert "no_critical_fault" in failing


def test_no_active_faults_ignore_faults_is_a_no_op():
    """ignore_faults with nothing active to ignore changes nothing."""
    a, _ = _avail()
    _all_good(a)
    product = Product(sku="ICE-1", kind="ice")
    assert a.test_sale_sellable(product) == (True, [])
    assert a.product_sellable(product) == (True, [])
