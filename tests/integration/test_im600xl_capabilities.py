"""The IM600XL vending simulator's declared channels against the edge's
expectations. The one place simulator and edge code meet without a broker
(`tests/meta/test_test_isolation.py` keeps each side's own tests apart), so
it never skips. The shipped-example check lives in
`test_sim_capabilities_example.py`."""

from ice_contracts.board.vending_machine import INPUT_FAULTS, VendingChannel
from ice_edge.vend.dispensers import validate_document
from ice_sims.vending_machine import VendingMachineSimulator
from tests.dispenser_fixtures import IM600XL_GOOD, IM600XL_PRODUCTS


def _declared() -> set[str]:
    caps = VendingMachineSimulator(machine_id="vmc-t12").build_capabilities()
    return {c.channel_id for c in caps.channels}


def test_im600xl_good_profiles_validate_clean_against_simulator_capabilities():
    caps = VendingMachineSimulator(machine_id="vmc-t12").build_capabilities()

    report = validate_document(IM600XL_GOOD, IM600XL_PRODUCTS, capabilities=caps)

    assert report.ok, report.render_text()
    assert report.errors == []
    assert report.warnings == []


def test_simulator_declares_every_input_fault_channel_and_ro_production():
    declared = _declared()

    for entry in INPUT_FAULTS:
        assert entry.channel.value in declared, entry.channel
    assert VendingChannel.ro_production.value in declared


def test_simulator_declares_no_bin_sensor():
    assert VendingChannel.bin_half_full.value not in _declared()
