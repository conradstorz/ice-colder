# tests/test_vmc_dispense_profiles.py
"""Tests for VMC-owned dispenser profiles (plan: dispenser profiles, Task
2) -- CFG-101/CFG-102 reconciliation against a loaded `DispenserProfiles`,
and the two chokepoints (`select_product`, `run_test_sale`) that must never
let a customer or a test sale reach a slot with no valid profile.

Three products: `ICE_1` (slot 0, kind "ice"), `WATER_1` (slot 1, kind
"water"), `OTHER_X` (slot 2, kind "other" -- never eligible for a profile,
always CFG-101 like a product with no table at all).
"""

import pytest

from config.config_model import ConfigModel, PhysicalDetails, Product
from contracts.common import ChannelDescriptor
from contracts.vending_machine import FaultCode, SubsystemCapabilities
from controller.vmc import VMC
from services.dispensers import DispenserProfiles
from tests.dispenser_fixtures import profiles_for, render_profiles_toml

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
        contract_version="0.8.0",
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
