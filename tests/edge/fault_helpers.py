"""Fault assertions shared by the edge tests."""


def fault_codes(vmc) -> set[str]:
    """The codes of every fault the VMC currently has active."""
    return {f["code"] for f in vmc.active_faults()}
