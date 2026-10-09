# tests/test_vmc_dispense_profiles.py
"""Tests for VMC-owned dispenser profiles (plan: dispenser profiles, Task
2) -- CFG-101/CFG-102 reconciliation against a loaded `DispenserProfiles`,
and the two chokepoints (`select_product`, `run_test_sale`) that must never
let a customer or a test sale reach a slot with no valid profile.

Three products: `ICE_1` (slot 0, kind "ice"), `WATER_1` (slot 1, kind
"water"), `OTHER_X` (slot 2, kind "other" -- never eligible for a profile,
always CFG-101 like a product with no table at all).
"""

import asyncio

import pytest

from config.config_model import ConfigModel, PhysicalDetails, Product
from contracts.common import ChannelDescriptor, CommandAck
from contracts.vending_machine import FaultCode, SubsystemCapabilities
from controller.vmc import VMC
from services.availability import Availability
from services.command_dispatcher import CommandTimeout
from services.dispensers import DispenserProfiles
from tests.dispenser_fixtures import FakeDispatcher, profiles_for, render_profiles_toml

ICE_1 = Product(sku="ICE-1", slot=0, kind="ice")
WATER_1 = Product(sku="W-1", slot=1, kind="water")
OTHER_X = Product(sku="X", slot=2, kind="other")


def make_vmc() -> VMC:
    cfg = ConfigModel(physical=PhysicalDetails(products=[ICE_1, WATER_1, OTHER_X]))
    return VMC(config=cfg)


def _channel(channel_id: str, direction: str) -> ChannelDescriptor:
    return ChannelDescriptor(
        channel_id=channel_id,
        kind="binary",
        interval_seconds=1.0,
        direction=direction,
    )


def _complete_capabilities() -> SubsystemCapabilities:
    return SubsystemCapabilities(
        subsystem="vending",
        firmware="x",
        contract_version="1.0.0",
        channels=[
            _channel("agitator_motor", "output"),
            _channel("auger_motor", "output"),
            _channel("bag_drop_solenoid", "output"),
            _channel("water_valve_solenoid", "output"),
            _channel("bag_full_sensor", "input"),
            _channel("door_sensor", "input"),
            _channel("water_flow_sensor", "input"),
        ],
    )


def _incomplete_capabilities() -> SubsystemCapabilities:
    """The complete capabilities doc minus `bag_full_sensor` -- affects
    only slot 0 (the ice slot), leaving the water slot's cross-check
    untouched so the assertions stay unambiguous."""
    complete = _complete_capabilities()
    channels = [c for c in complete.channels if c.channel_id != "bag_full_sensor"]
    return complete.model_copy(update={"channels": channels})


def test_set_profiles_locks_products_without_profile(tmp_path):
    vmc = make_vmc()
    profiles = profiles_for([ICE_1, WATER_1, OTHER_X], tmp_path)
    # Remove slot 1's table -- only slot 0 (ICE-1) remains.
    (tmp_path / "dispensers.toml").write_text(
        render_profiles_toml([ICE_1]), encoding="utf-8"
    )
    profiles.load()

    vmc.set_dispenser_profiles(profiles)

    assert vmc._lockouts == {"W-1": FaultCode.CFG_101, "X": FaultCode.CFG_101}


def test_reconcile_clears_cfg101_when_profile_appears(tmp_path):
    vmc = make_vmc()
    profiles = profiles_for([ICE_1, WATER_1, OTHER_X], tmp_path)
    (tmp_path / "dispensers.toml").write_text(
        render_profiles_toml([ICE_1]), encoding="utf-8"
    )
    profiles.load()
    vmc.set_dispenser_profiles(profiles)
    assert vmc._lockouts["W-1"] is FaultCode.CFG_101

    # Write the full file back and reconcile again.
    (tmp_path / "dispensers.toml").write_text(
        render_profiles_toml([ICE_1, WATER_1]), encoding="utf-8"
    )
    report = profiles.load()
    assert report.ok

    vmc.reconcile_dispenser_profiles()

    assert "W-1" not in vmc._lockouts
    assert vmc._lockouts["X"] is FaultCode.CFG_101


def test_reconcile_never_clears_other_lockouts(tmp_path):
    vmc = make_vmc()
    profiles = profiles_for([ICE_1, WATER_1, OTHER_X], tmp_path)
    vmc.set_dispenser_profiles(profiles)
    assert "ICE-1" not in vmc._lockouts  # ICE-1 has a valid profile

    vmc._raise_fault(FaultCode.ICE_301, sku="ICE-1")
    assert vmc._lockouts["ICE-1"] is FaultCode.ICE_301

    vmc.reconcile_dispenser_profiles()

    assert vmc._lockouts["ICE-1"] is FaultCode.ICE_301


def test_cfg102_follows_file_error(tmp_path):
    vmc = make_vmc()
    missing_path = tmp_path / "dispensers.toml"  # never written
    cfg = ConfigModel(physical=PhysicalDetails(products=[ICE_1, WATER_1, OTHER_X]))
    profiles = DispenserProfiles(cfg, path=missing_path)
    profiles.load()
    assert profiles.report.file_error

    vmc.set_dispenser_profiles(profiles)

    codes = {f["code"] for f in vmc.active_faults()}
    assert "CFG-102" in codes

    missing_path.write_text(render_profiles_toml([ICE_1, WATER_1]), encoding="utf-8")
    report = profiles.load()
    assert report.ok

    vmc.reconcile_dispenser_profiles()

    codes = {f["code"] for f in vmc.active_faults()}
    assert "CFG-102" not in codes


def test_select_product_refuses_cfg101_locked(tmp_path):
    vmc = make_vmc()
    profiles = profiles_for([ICE_1, WATER_1, OTHER_X], tmp_path)
    vmc.set_dispenser_profiles(profiles)
    assert vmc._lockouts["X"] is FaultCode.CFG_101

    messages = []
    vmc.set_message_callback(messages.append)
    vmc.select_product(2)  # "X"

    assert vmc.selected_product is None
    assert "CFG-101" in messages[-1]


async def test_capabilities_hook_reruns_cross_checks(tmp_path):
    vmc = make_vmc()
    profiles = profiles_for([ICE_1, WATER_1, OTHER_X], tmp_path)
    vmc.set_dispenser_profiles(profiles)
    assert "ICE-1" not in vmc._lockouts

    incomplete = _incomplete_capabilities()
    await vmc._handle_mqtt_capabilities("capabilities/vending", incomplete.model_dump())

    assert vmc._lockouts["ICE-1"] is FaultCode.CFG_101
    assert "W-1" not in vmc._lockouts

    complete = _complete_capabilities()
    await vmc._handle_mqtt_capabilities("capabilities/vending", complete.model_dump())

    assert "ICE-1" not in vmc._lockouts


async def test_run_test_sale_refuses_without_profile(tmp_path):
    vmc = make_vmc()
    profiles = profiles_for([ICE_1, WATER_1, OTHER_X], tmp_path)
    vmc.set_dispenser_profiles(profiles)

    with pytest.raises(RuntimeError, match="CFG-101"):
        await vmc.run_test_sale("X")

    assert vmc.credit_escrow == 0


def test_clearing_another_fault_relocks_profileless_product_with_cfg101(tmp_path):
    """Review finding: CFG-101 is a standing invariant. A profile-less
    product locked by some *other* fault (ICE-301 here) must not become
    sellable just because that other fault was cleared -- clear_fault must
    re-check the profile and re-raise CFG-101 immediately."""
    vmc = make_vmc()
    profiles = profiles_for([ICE_1, WATER_1, OTHER_X], tmp_path)
    # Remove slot 1's table -- W-1 (water) has no profile.
    (tmp_path / "dispensers.toml").write_text(
        render_profiles_toml([ICE_1]), encoding="utf-8"
    )
    profiles.load()

    # Lock W-1 with ICE-301 *before* profiles are attached, so reconcile
    # finds it already locked and leaves it alone (per its own docstring).
    vmc._raise_fault(FaultCode.ICE_301, sku="W-1")
    assert vmc._lockouts["W-1"] is FaultCode.ICE_301

    vmc.set_dispenser_profiles(profiles)
    assert vmc._lockouts["W-1"] is FaultCode.ICE_301

    assert vmc.clear_fault("W-1", by="admin") is True

    assert vmc._lockouts["W-1"] is FaultCode.CFG_101


def test_admin_clear_of_cfg101_is_reasserted_without_a_profile(tmp_path):
    """An admin "clearing" CFG-101 by hand gets it re-raised at once unless
    the underlying file was actually fixed -- the fix is the file, not the
    button."""
    vmc = make_vmc()
    profiles = profiles_for([ICE_1, WATER_1, OTHER_X], tmp_path)
    (tmp_path / "dispensers.toml").write_text(
        render_profiles_toml([ICE_1]), encoding="utf-8"
    )
    profiles.load()
    vmc.set_dispenser_profiles(profiles)
    assert vmc._lockouts["W-1"] is FaultCode.CFG_101

    assert vmc.clear_fault("W-1", by="admin") is True
    assert vmc._lockouts["W-1"] is FaultCode.CFG_101

    # Now actually fix the file -- the real clear path.
    (tmp_path / "dispensers.toml").write_text(
        render_profiles_toml([ICE_1, WATER_1]), encoding="utf-8"
    )
    report = profiles.load()
    assert report.ok

    vmc.reconcile_dispenser_profiles()

    assert "W-1" not in vmc._lockouts


def test_clearing_a_fault_on_a_profiled_product_does_not_relock(tmp_path):
    """A product with a valid profile must never be re-locked by the new
    re-check -- clearing an unrelated fault on it just clears it."""
    vmc = make_vmc()
    profiles = profiles_for([ICE_1, WATER_1, OTHER_X], tmp_path)
    vmc.set_dispenser_profiles(profiles)
    assert "ICE-1" not in vmc._lockouts

    vmc._raise_fault(FaultCode.ICE_301, sku="ICE-1")
    assert vmc._lockouts["ICE-1"] is FaultCode.ICE_301

    assert vmc.clear_fault("ICE-1", by="admin") is True

    assert "ICE-1" not in vmc._lockouts


# --- Copilot review (PR #32) finding C1: catalog mutations must reconcile ---


def test_kind_change_invalidates_profile(tmp_path):
    """A product whose stale `kind` no longer matches its profile's
    `mechanism` (e.g. ice -> water) must lose that profile and be
    CFG-101-locked once `catalog_changed()` re-reconciles -- a bagged-ice
    profile must never be dispatchable for a water-kind product just
    because the slot number still matches."""
    ice_product = Product(sku="ICE-1", slot=0, kind="ice")
    cfg = ConfigModel(
        physical=PhysicalDetails(products=[ice_product, WATER_1, OTHER_X])
    )
    vmc = VMC(config=cfg)
    profiles = profiles_for([ice_product, WATER_1, OTHER_X], tmp_path)
    vmc.set_dispenser_profiles(profiles)
    assert "ICE-1" not in vmc._lockouts
    assert vmc.dispenser_profile_for(ice_product) is not None

    ice_product.kind = "water"
    vmc.catalog_changed()

    assert vmc._lockouts["ICE-1"] is FaultCode.CFG_101
    assert vmc.dispenser_profile_for(ice_product) is None


def test_new_product_locked_after_catalog_change(tmp_path):
    """A product appended to the catalog after profiles were attached
    (e.g. by a web route's add_product) has no profile and must be
    CFG-101-locked as soon as catalog_changed() runs -- not left
    sellable until some unrelated reconcile happens to fire."""
    vmc = make_vmc()
    profiles = profiles_for([ICE_1, WATER_1, OTHER_X], tmp_path)
    vmc.set_dispenser_profiles(profiles)

    new_product = Product(sku="NEW-1", slot=9, kind="ice")
    vmc.config_model.products.append(new_product)
    vmc.catalog_changed()

    assert vmc._lockouts["NEW-1"] is FaultCode.CFG_101


def test_catalog_changed_is_a_noop_without_profiles():
    """No VMC.set_dispenser_profiles call yet (no DispenserProfiles
    attached) -- catalog_changed() must not raise."""
    vmc = make_vmc()
    vmc.catalog_changed()  # must not raise
    assert vmc._lockouts == {}


def test_reconcile_never_raises_cfg101_over_another_lockout(tmp_path):
    """The raise-side guard in reconcile_dispenser_profiles: a profile-less
    product already locked by another fault before profiles are attached
    keeps that fault, not CFG-101."""
    vmc = make_vmc()
    profiles = profiles_for([ICE_1, WATER_1, OTHER_X], tmp_path)
    (tmp_path / "dispensers.toml").write_text(
        render_profiles_toml([ICE_1]), encoding="utf-8"
    )
    profiles.load()

    vmc._raise_fault(FaultCode.ICE_301, sku="W-1")
    assert vmc._lockouts["W-1"] is FaultCode.ICE_301

    vmc.set_dispenser_profiles(profiles)

    assert vmc._lockouts["W-1"] is FaultCode.ICE_301


# --- Task 3: the sale dispenses through the dispatcher with the profile ---


class FakeEventRecorder:
    def __init__(self):
        self.events: list[tuple] = []
        self.sales: list[tuple] = []

    def record(self, event_type, value=1.0, metadata=None):
        self.events.append((event_type, value, metadata))

    def record_sale(self, sku, name, slot, price, methods, ts=None):
        self.sales.append((sku, name, slot, price, methods))


def _vmc_with_profiles(tmp_path, products=(ICE_1, WATER_1)):
    """A VMC wired exactly like `make_vmc()` but with a loaded
    `DispenserProfiles` for *products* and a `FakeDispatcher` attached --
    the minimum wiring a production sale needs to actually dispatch."""
    cfg = ConfigModel(physical=PhysicalDetails(products=list(products)))
    vmc = VMC(config=cfg)
    vmc.attach_to_loop(asyncio.get_running_loop())
    profiles = profiles_for(list(products), tmp_path)
    vmc.set_dispenser_profiles(profiles)
    dispatcher = FakeDispatcher()
    vmc.set_command_dispatcher(dispatcher)
    return vmc, dispatcher


def _start_sale(vmc, product) -> None:
    vmc.machine.set_state("interacting_with_user")
    vmc.selected_product = product
    vmc.credit_escrow = product.price
    vmc._process_payment()
    assert vmc.state == "dispensing"


async def test_sale_dispatches_full_profile_on_command_channel(tmp_path):
    vmc, dispatcher = _vmc_with_profiles(tmp_path)

    published: list[tuple[str, object]] = []

    class FakeMQTT:
        def register(self, *a, **k):
            pass

        async def publish(self, topic, payload, **kwargs):
            published.append((topic, payload))

    vmc.set_mqtt_client(FakeMQTT())
    _start_sale(vmc, vmc.products[0])  # ICE-1, bagged_ice

    await asyncio.sleep(0)

    subsystem, command, params = dispatcher.sent[-1]
    assert subsystem == "vending"
    assert command == "dispense"
    assert params["slot"] == ICE_1.slot
    assert params["mechanism"] == "bagged_ice"
    assert params["profile"]["agitate"]["motor_channel"] == "agitator_motor"
    # The dispense command travels only through the command dispatcher
    # (cmd_dispatcher.send -> cmd/vending/dispense under the hood) -- the
    # VMC must never also publish it directly on a bare "cmd/dispense"
    # topic via the MQTT client.
    assert not any(topic == "cmd/dispense" for topic, _ in published)
    vmc.cancel_pending_tasks()


async def test_no_ack_fails_vend_immediately_with_pay102(tmp_path):
    vmc, dispatcher = _vmc_with_profiles(tmp_path)
    rec = FakeEventRecorder()
    vmc.set_event_recorder(rec)
    dispatcher.fail_with = CommandTimeout("vending", "dispense")
    product = vmc.products[0]
    price = product.price
    _start_sale(vmc, product)

    await asyncio.sleep(0)

    assert vmc.state == "interacting_with_user"
    assert vmc.credit_escrow == price
    # PAY-102 is vend_failed severity (never a lockout or a persisted
    # machine fault, so it never shows in active_faults()) -- proven here
    # via the vend_failed event it drives, matching the convention already
    # used throughout tests/test_vmc_flows.py.
    assert any(e[0] == "vend_failed" and e[2]["code"] == "PAY-102" for e in rec.events)
    assert vmc._dispense_timeout_task is None
    vmc.cancel_pending_tasks()


async def test_rejected_ack_fails_vend(tmp_path):
    vmc, dispatcher = _vmc_with_profiles(tmp_path)
    rec = FakeEventRecorder()
    vmc.set_event_recorder(rec)
    product = vmc.products[0]
    price = product.price

    async def send_rejected(subsystem, command, params=None):
        dispatcher.sent.append((subsystem, command, params or {}))
        return CommandAck(request_id="rejected-1", command=command, status="rejected")

    dispatcher.send = send_rejected
    _start_sale(vmc, product)

    await asyncio.sleep(0)

    assert vmc.state == "interacting_with_user"
    assert vmc.credit_escrow == price
    assert any(e[0] == "vend_failed" and e[2]["code"] == "PAY-102" for e in rec.events)
    vmc.cancel_pending_tasks()


async def test_request_id_mismatch_is_logged_not_fatal(tmp_path, caplog):
    vmc, dispatcher = _vmc_with_profiles(tmp_path)
    product = vmc.products[0]
    _start_sale(vmc, product)
    await asyncio.sleep(0)  # let _persist_then_dispense record the ack's request_id

    assert vmc._dispense_request_id == dispatcher.last_request_id

    await vmc._handle_mqtt_dispenser(
        "hardware/dispenser",
        {
            "slot": product.slot,
            "state": "complete",
            "request_id": "not-the-real-one",
        },
    )

    assert vmc.state == "idle"  # the sale still completed
    assert any("request_id" in r.message for r in caplog.records)
    vmc.cancel_pending_tasks()


async def test_door_open_completes_sale_and_raises_ice402(tmp_path):
    vmc, dispatcher = _vmc_with_profiles(tmp_path)
    rec = FakeEventRecorder()
    vmc.set_event_recorder(rec)
    avail = Availability()
    vmc.set_availability(avail)
    product = vmc.products[0]
    _start_sale(vmc, product)
    await asyncio.sleep(0)

    await vmc._handle_mqtt_dispenser(
        "hardware/dispenser", {"slot": product.slot, "state": "door_open"}
    )

    assert vmc.state == "idle"
    assert len(rec.sales) == 1
    assert rec.sales[0][0] == "ICE-1"
    assert "ICE-402" in {f["code"] for f in vmc.active_faults()}
    assert avail.payment_enabled is False
    vmc.cancel_pending_tasks()


@pytest.mark.parametrize(
    "outcome,expected",
    [
        ("no_flow", "WTR-101"),
        ("over_dispense", "WTR-102"),
        ("timeout", "WTR-101"),
        ("error", "ICE-302"),
    ],
)
async def test_water_outcomes_map_by_mechanism(tmp_path, outcome, expected):
    vmc, dispatcher = _vmc_with_profiles(tmp_path)
    rec = FakeEventRecorder()
    vmc.set_event_recorder(rec)
    product = vmc.products[1]  # W-1, water_fill
    _start_sale(vmc, product)
    await asyncio.sleep(0)

    await vmc._handle_mqtt_dispenser(
        "hardware/dispenser", {"slot": product.slot, "state": outcome}
    )

    assert any(e[0] == "vend_failed" and e[2]["code"] == expected for e in rec.events)
    vmc.cancel_pending_tasks()


async def test_ice_timeout_maps_to_ice301(tmp_path):
    vmc, dispatcher = _vmc_with_profiles(tmp_path)
    rec = FakeEventRecorder()
    vmc.set_event_recorder(rec)
    product = vmc.products[0]  # ICE-1, bagged_ice
    _start_sale(vmc, product)
    await asyncio.sleep(0)

    await vmc._handle_mqtt_dispenser(
        "hardware/dispenser", {"slot": product.slot, "state": "timeout"}
    )

    assert any(e[0] == "vend_failed" and e[2]["code"] == "ICE-301" for e in rec.events)
    vmc.cancel_pending_tasks()


async def test_mis_reporting_board_falls_back_to_generic_error(tmp_path, caplog):
    """A water board that reports `jam` (a bagged-ice-only outcome) has no
    (mechanism, outcome) mapping -- fault_for_outcome raises KeyError, and
    the VMC must fall back to a generic error for that mechanism rather
    than crash the MQTT handler."""
    vmc, dispatcher = _vmc_with_profiles(tmp_path)
    rec = FakeEventRecorder()
    vmc.set_event_recorder(rec)
    product = vmc.products[1]  # W-1, water_fill -- has no (water_fill, jam) entry
    _start_sale(vmc, product)
    await asyncio.sleep(0)

    await vmc._handle_mqtt_dispenser(
        "hardware/dispenser", {"slot": product.slot, "state": "jam"}
    )

    assert vmc.state == "interacting_with_user"
    assert any(e[0] == "vend_failed" and e[2]["code"] == "ICE-302" for e in rec.events)
    assert any("no fault mapped" in r.message.lower() for r in caplog.records)
    vmc.cancel_pending_tasks()


async def test_snapshot_records_mechanism(tmp_path):
    vmc, dispatcher = _vmc_with_profiles(tmp_path)
    product = vmc.products[0]
    _start_sale(vmc, product)
    await asyncio.sleep(0)

    snap = vmc._snapshot()

    assert snap.dispense_mechanism == "bagged_ice"
    vmc.cancel_pending_tasks()


async def test_mid_vend_profile_save_does_not_change_inflight_command(tmp_path):
    vmc, dispatcher = _vmc_with_profiles(tmp_path)
    product = vmc.products[0]
    _start_sale(vmc, product)
    await asyncio.sleep(0)

    sent_before = dispatcher.sent[-1]

    # Reload profiles with a different run_seconds mid-vend -- the
    # already-dispatched command must not change.
    (tmp_path / "dispensers.toml").write_text(
        render_profiles_toml([ICE_1, WATER_1]).replace(
            "run_seconds        = 4.0", "run_seconds        = 9.0"
        ),
        encoding="utf-8",
    )
    vmc._dispenser_profiles.load()

    assert dispatcher.sent[-1] == sent_before
    vmc.cancel_pending_tasks()


# --- Review findings I1/I2: dispatch-failure robustness ---


async def test_unexpected_dispatch_error_fails_vend_immediately(tmp_path):
    """A non-`CommandTimeout` exception raised while dispatching (e.g. bad
    `SubsystemCommand` validation, a `model_dump` error, a broken MQTT
    publish) must fail the vend right away, exactly like a `CommandTimeout`
    does -- not leave the FSM stuck in `dispensing` for the full 120s
    dispense-timeout fallback."""
    vmc, dispatcher = _vmc_with_profiles(tmp_path)
    rec = FakeEventRecorder()
    vmc.set_event_recorder(rec)
    dispatcher.fail_with = ValueError("boom")
    product = vmc.products[0]
    price = product.price
    _start_sale(vmc, product)

    await asyncio.sleep(0)

    assert vmc.state == "interacting_with_user"
    assert vmc.credit_escrow == price
    assert any(e[0] == "vend_failed" and e[2]["code"] == "PAY-102" for e in rec.events)
    assert vmc._dispense_timeout_task is None
    vmc.cancel_pending_tasks()


class FakeSessionStore:
    """Stub session store that raises an error on save_async."""

    def load(self):
        return None

    async def save_async(self, snap):
        raise OSError("disk full")


async def test_snapshot_save_failure_fails_vend_immediately(tmp_path):
    """A disk-full or permission error during snapshot save must fail the vend
    right away with PAY-102, not leave the FSM stuck in `dispensing` for the
    full 120s dispense-timeout fallback."""
    vmc, dispatcher = _vmc_with_profiles(tmp_path)
    rec = FakeEventRecorder()
    vmc.set_event_recorder(rec)
    vmc.set_session_store(FakeSessionStore())
    product = vmc.products[0]
    price = product.price
    _start_sale(vmc, product)

    await asyncio.sleep(0)

    assert vmc.state == "interacting_with_user"
    assert vmc.credit_escrow == price
    assert any(e[0] == "vend_failed" and e[2]["code"] == "PAY-102" for e in rec.events)
    # M5 (whole-branch review): a snapshot-save failure is reported with
    # its own outcome string, distinct from a dispatch failure's "no_ack".
    assert any(
        e[0] == "vend_failed" and e[2]["outcome"] == "snapshot_failed"
        for e in rec.events
    )
    assert vmc._dispense_timeout_task is None
    assert dispatcher.sent == []
    vmc.cancel_pending_tasks()


async def test_late_no_ack_from_previous_sale_does_not_fail_current_sale(tmp_path):
    """A `CommandTimeout` that finally lands for sale A's dispatch, after A
    already settled through the real hardware report and sale B has since
    reached `dispensing`, must be ignored -- never cancel B's dispense
    timer, raise PAY-102 on B's sku, or refund B's price out from under a
    product that is actually being dispensed."""
    vmc, dispatcher = _vmc_with_profiles(tmp_path)
    rec = FakeEventRecorder()
    vmc.set_event_recorder(rec)
    gate = asyncio.Event()
    dispatcher.gate = gate
    dispatcher.fail_with = CommandTimeout("vending", "dispense")

    product_a = vmc.products[0]  # ICE-1
    _start_sale(vmc, product_a)
    await asyncio.sleep(0)  # sale A's dispatch reaches send() and blocks on the gate

    # Sale A completes through the real hardware report while its own
    # dispatch call is still pending behind the gate.
    await vmc._handle_mqtt_dispenser(
        "hardware/dispenser", {"slot": product_a.slot, "state": "complete"}
    )
    assert vmc.state == "idle"

    # Sale B starts and reaches dispensing while A's blocked send() is
    # still pending -- B's own dispatch must succeed normally.
    dispatcher.fail_with = None
    product_b = vmc.products[1]  # W-1
    _start_sale(vmc, product_b)
    await asyncio.sleep(0)  # let B's own dispatch run (and succeed)
    escrow_before = vmc.credit_escrow

    # A's delayed CommandTimeout finally arrives.
    gate.set()
    await asyncio.sleep(0)

    assert vmc.state == "dispensing"
    assert vmc.credit_escrow == escrow_before
    assert not any(
        e[0] == "vend_failed" and e[2]["code"] == "PAY-102" for e in rec.events
    )
    assert vmc._dispense_timeout_task is not None
    vmc.cancel_pending_tasks()
