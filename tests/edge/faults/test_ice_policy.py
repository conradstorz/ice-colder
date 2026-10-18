from ice_contracts.board.vending_machine import VendingChannel
from ice_edge.faults.ice_policy import IcePolicy


class FakeFaults:
    def __init__(self):
        self.cleared = 0

    def clear_ice101_lockouts(self):
        self.cleared += 1


def test_bin_half_full_true_clears_ice101():
    faults = FakeFaults()
    IcePolicy(faults=faults).on_hardware_io(VendingChannel.bin_half_full, True)
    assert faults.cleared == 1


def test_bin_half_full_false_clears_nothing():
    faults = FakeFaults()
    IcePolicy(faults=faults).on_hardware_io(VendingChannel.bin_half_full, False)
    assert faults.cleared == 0


def test_other_device_clears_nothing():
    faults = FakeFaults()
    IcePolicy(faults=faults).on_hardware_io("door", True)
    assert faults.cleared == 0
