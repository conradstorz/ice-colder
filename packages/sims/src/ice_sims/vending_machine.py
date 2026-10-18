# ice_sims/vending_machine.py
"""
Vending machine interface simulator -- entry module.

The implementation lives in the `ice_sims.vending` package (board port,
channel table, dispenser, customers, faults, HA entities, simulator). This
module keeps the historical import path and the `-m` entry point working.

Run: uv run python -m ice_sims.vending_machine [--broker HOST] [--port PORT] [--machine-id ID]
"""

from ice_sims.base import ESP32Simulator
from ice_sims.vending.channels import HARDWARE_DEVICES, SENSOR_PUBLISH_INTERVAL
from ice_sims.vending.simulator import VendingMachineSimulator

__all__ = ["HARDWARE_DEVICES", "SENSOR_PUBLISH_INTERVAL", "VendingMachineSimulator"]


if __name__ == "__main__":
    ESP32Simulator.entry_point(VendingMachineSimulator)
