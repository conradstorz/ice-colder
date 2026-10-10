# tests/test_dispenser_gate.py
"""Unit tests for `controller.dispenser_gate.DispenserProfileGate` --
extracted from `controller.vmc.VMC` (see CLAUDE.md's "Dispenser profiles"
section). These exercise the gate in isolation: no `VMC` instance anywhere
here. `FakeFaults` stands in for the fault-registry callables
(`is_locked`/`has_machine_fault`/`raise_fault`/`clear_fault`) the gate is
constructed with, recording every call so a test can assert both the
resulting state and exactly what the gate asked for.

Products and a real `DispenserProfiles`/`dispensers.toml` come from
`tests/dispenser_fixtures.py` (`profiles_for`/`render_profiles_toml`),
the same helpers `tests/test_vmc_dispense_profiles.py` uses -- the two
test modules build the identical fixtures, just handed to a bare
`DispenserProfileGate` here instead of a full `VMC`.
"""

from __future__ import annotations

from types import SimpleNamespace

from config.config_model import ConfigModel, PhysicalDetails, Product
from contracts.common import ChannelDescriptor
from contracts.vending_machine import FaultCode, SubsystemCapabilities
from controller.dispenser_gate import DispenserProfileGate
from services.dispensers import DispenserProfiles
from tests.dispenser_fixtures import profiles_for, render_profiles_toml

ICE_1 = Product(sku="ICE-1", slot=0, kind="ice")
WATER_1 = Product(sku="W-1", slot=1, kind="water")
OTHER_X = Product(sku="X", slot=2, kind="other")


class FakeFaults:
    """Dict-backed stand-in for the fault registry, recording every call
    the gate makes so tests can assert on both state and call shape."""

    def __init__(self):
        self.lockouts: dict[str, FaultCode] = {}
        self.machine_faults: set[FaultCode] = set()
        self.raised: list[tuple] = []  # (code, sku)
        self.cleared: list[tuple] = []  # (key, by)

    def is_locked(self, sku):
        return self.lockouts.get(sku)

    def has(self, code):
        return code in self.machine_faults

    def raise_fault(self, code, sku=None):
        self.raised.append((code, sku))
        if sku is not None:
            self.lockouts[sku] = code
        else:
            self.machine_faults.add(code)

    def clear_fault(self, key, by):
        self.cleared.append((key, by))
        if key in self.lockouts:
            del self.lockouts[key]
            return True
        try:
            code = FaultCode(key)
        except ValueError:
            return False
        if code in self.machine_faults:
            self.machine_faults.discard(code)
            return True
        return False


def make_gate(products: list, faults: FakeFaults | None = None):
    """A gate wired to *products* (a live, mutable list -- tests may
    append to it to simulate a catalog change, matching the real
    `lambda: self.config_model.products` callable's own semantics) and
    *faults* (a fresh `FakeFaults()` by default)."""
    faults = faults if faults is not None else FakeFaults()
    gate = DispenserProfileGate(
        products=lambda: products,
        is_locked=faults.is_locked,
        has_machine_fault=faults.has,
        raise_fault=faults.raise_fault,
        clear_fault=faults.clear_fault,
    )
    return gate, faults


# --- profile_for ---


def test_profile_for_none_with_no_profiles():
    gate, _ = make_gate([ICE_1])
    assert gate.profile_for(ICE_1) is None


def test_profile_for_none_for_kind_with_no_mechanism(tmp_path):
    gate, _ = make_gate([ICE_1, OTHER_X])
    gate.profiles = profiles_for([ICE_1, OTHER_X], tmp_path)
    # OTHER_X's kind ("other") has no MECHANISM_FOR_KIND entry at all, so
    # this returns None before even consulting the (nonexistent) table.
    assert gate.profile_for(OTHER_X) is None


def test_profile_for_none_on_sku_mismatch(tmp_path):
    gate, _ = make_gate([ICE_1])
    gate.profiles = profiles_for([ICE_1], tmp_path)
    impostor = Product(sku="NOT-ICE-1", slot=ICE_1.slot, kind="ice")
    assert gate.profile_for(impostor) is None


def test_profile_for_none_on_mechanism_mismatch(tmp_path):
    gate, _ = make_gate([ICE_1])
    gate.profiles = profiles_for([ICE_1], tmp_path)
    # Same sku and slot, but the catalog's kind changed underneath the
    # still-bagged-ice profile at that slot.
    changed = Product(sku=ICE_1.sku, slot=ICE_1.slot, kind="water")
    assert gate.profile_for(changed) is None


def test_profile_for_returns_the_profile_otherwise(tmp_path):
    gate, _ = make_gate([ICE_1])
    gate.profiles = profiles_for([ICE_1], tmp_path)
    profile = gate.profile_for(ICE_1)
    assert profile is not None
    assert profile.product_sku == "ICE-1"
    assert profile.mechanism == "bagged_ice"


# --- reconcile: CFG-101 ---


def test_reconcile_raises_cfg101_only_for_unlocked_products_without_a_profile(
    tmp_path,
):
    products = [ICE_1, WATER_1, OTHER_X]
    gate, faults = make_gate(products)
    profiles = profiles_for(products, tmp_path)
    # Remove W-1's table -- only ICE-1 now has a valid profile.
    (tmp_path / "dispensers.toml").write_text(
        render_profiles_toml([ICE_1]), encoding="utf-8"
    )
    profiles.load()
    gate.profiles = profiles

    gate.reconcile()

    assert faults.lockouts == {"W-1": FaultCode.CFG_101, "X": FaultCode.CFG_101}
    assert "ICE-1" not in faults.lockouts


def test_reconcile_leaves_a_sku_locked_by_another_code_alone(tmp_path):
    products = [ICE_1, WATER_1, OTHER_X]
    gate, faults = make_gate(products)
    profiles = profiles_for(products, tmp_path)
    (tmp_path / "dispensers.toml").write_text(
        render_profiles_toml([ICE_1]), encoding="utf-8"
    )
    profiles.load()
    # W-1 is already locked by a different fault before the gate ever
    # looks at it.
    faults.lockouts["W-1"] = FaultCode.ICE_301
    gate.profiles = profiles

    gate.reconcile()

    assert faults.lockouts["W-1"] is FaultCode.ICE_301
    assert all(
        code is not FaultCode.CFG_101 for code, sku in faults.raised if sku == "W-1"
    )


def test_reconcile_clears_only_a_cfg101_lockout_when_profile_becomes_valid(tmp_path):
    products = [ICE_1, WATER_1]
    gate, faults = make_gate(products)
    profiles = profiles_for(products, tmp_path)
    (tmp_path / "dispensers.toml").write_text(
        render_profiles_toml([ICE_1]), encoding="utf-8"
    )
    profiles.load()
    gate.profiles = profiles
    gate.reconcile()
    assert faults.lockouts["W-1"] is FaultCode.CFG_101

    # A lockout under some other code must NOT be cleared just because a
    # valid profile now exists -- only a CFG-101 lockout is eligible.
    faults.lockouts["ICE-1"] = FaultCode.ICE_301

    (tmp_path / "dispensers.toml").write_text(
        render_profiles_toml([ICE_1, WATER_1]), encoding="utf-8"
    )
    report = profiles.load()
    assert report.ok

    gate.reconcile()

    assert "W-1" not in faults.lockouts
    assert ("W-1", "auto") in faults.cleared
    assert faults.lockouts["ICE-1"] is FaultCode.ICE_301


# --- reconcile: CFG-102 ---


def test_reconcile_raises_cfg102_once_on_file_error(tmp_path):
    products = [ICE_1]
    gate, faults = make_gate(products)
    missing_path = tmp_path / "dispensers.toml"  # never written
    cfg = ConfigModel(physical=PhysicalDetails(products=products))
    profiles = DispenserProfiles(cfg, path=missing_path)
    profiles.load()
    assert profiles.report.file_error
    gate.profiles = profiles

    gate.reconcile()
    gate.reconcile()

    assert FaultCode.CFG_102 in faults.machine_faults
    assert faults.raised.count((FaultCode.CFG_102, None)) == 1


def test_reconcile_clears_cfg102_when_the_error_goes_away(tmp_path):
    products = [ICE_1]
    gate, faults = make_gate(products)
    missing_path = tmp_path / "dispensers.toml"
    cfg = ConfigModel(physical=PhysicalDetails(products=products))
    profiles = DispenserProfiles(cfg, path=missing_path)
    profiles.load()
    gate.profiles = profiles
    gate.reconcile()
    assert FaultCode.CFG_102 in faults.machine_faults

    missing_path.write_text(render_profiles_toml(products), encoding="utf-8")
    report = profiles.load()
    assert report.ok

    gate.reconcile()

    assert FaultCode.CFG_102 not in faults.machine_faults
    assert (FaultCode.CFG_102.value, "auto") in faults.cleared


# --- on_vending_capabilities ---


class _FakeProfiles:
    """Minimal stand-in for `DispenserProfiles`, just enough surface for
    `on_vending_capabilities`/`reconcile` to run against an empty product
    list without touching real TOML validation."""

    def __init__(self):
        self.set_capabilities_calls: list = []
        self.report = SimpleNamespace(file_error=False)

    def set_capabilities(self, caps):
        self.set_capabilities_calls.append(caps)


def _capabilities() -> SubsystemCapabilities:
    return SubsystemCapabilities(
        subsystem="vending",
        firmware="x",
        contract_version="1.0.0",
        channels=[
            ChannelDescriptor(
                channel_id="agitator_motor",
                kind="binary",
                interval_seconds=1.0,
                direction="output",
            )
        ],
    )


def test_on_vending_capabilities_ignores_non_vending_subsystems():
    gate, _ = make_gate([])
    gate.profiles = _FakeProfiles()

    gate.on_vending_capabilities("ice_maker", _capabilities())

    assert gate.profiles.set_capabilities_calls == []


def test_on_vending_capabilities_noops_with_no_profiles():
    gate, _ = make_gate([])
    assert gate.profiles is None

    gate.on_vending_capabilities("vending", _capabilities())  # must not raise

    assert gate.profiles is None


def test_on_vending_capabilities_reruns_cross_checks_for_vending(tmp_path):
    products = [ICE_1]
    gate, faults = make_gate(products)
    profiles = profiles_for(products, tmp_path)
    gate.profiles = profiles
    assert "ICE-1" not in faults.lockouts

    # A capabilities doc missing the channel the ice mechanism drives
    # invalidates ICE-1's profile on the next cross-check.
    incomplete = SubsystemCapabilities(
        subsystem="vending",
        firmware="x",
        contract_version="1.0.0",
        channels=[],
    )
    gate.on_vending_capabilities("vending", incomplete)

    assert faults.lockouts["ICE-1"] is FaultCode.CFG_101


# --- lacks_valid_profile ---


def test_lacks_valid_profile_true_when_unlocked_and_profile_less(tmp_path):
    products = [ICE_1, WATER_1]
    gate, _ = make_gate(products)
    profiles = profiles_for(products, tmp_path)
    (tmp_path / "dispensers.toml").write_text(
        render_profiles_toml([ICE_1]), encoding="utf-8"
    )
    profiles.load()
    gate.profiles = profiles

    assert gate.lacks_valid_profile("W-1") is True


def test_lacks_valid_profile_false_when_a_valid_profile_exists(tmp_path):
    products = [ICE_1]
    gate, _ = make_gate(products)
    gate.profiles = profiles_for(products, tmp_path)

    assert gate.lacks_valid_profile("ICE-1") is False


def test_lacks_valid_profile_false_for_an_unknown_sku(tmp_path):
    products = [ICE_1]
    gate, _ = make_gate(products)
    gate.profiles = profiles_for(products, tmp_path)

    assert gate.lacks_valid_profile("NO-SUCH-SKU") is False


def test_lacks_valid_profile_false_when_no_profiles_attached():
    gate, _ = make_gate([ICE_1])
    assert gate.profiles is None

    assert gate.lacks_valid_profile("ICE-1") is False
