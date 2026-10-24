"""ice_ui.views.identity.MachineLabel.display."""

from ice_ui.views.identity import MachineLabel


def test_both_name_and_id():
    assert MachineLabel(common_name="Lobby Ice", machine_id="ice-07").display == (
        "Lobby Ice (ice-07)"
    )


def test_name_only():
    assert MachineLabel(common_name="Lobby Ice").display == "Lobby Ice"


def test_id_only():
    assert MachineLabel(machine_id="ice-07").display == "ice-07"


def test_fingerprint_only():
    assert MachineLabel(short_fingerprint="abcd-efgh").display == "abcd-efgh"


def test_none_is_empty():
    assert MachineLabel().display == ""
