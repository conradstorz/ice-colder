# ice_sims/mdb_gateway.py
"""
MDB payment board simulator -- entry module.

The implementation lives in the `ice_sims.mdb` package (cash model, policy,
event log, customer, faults, simulator) and speaks payment contract 1.1.0.
This module keeps the `-m` entry point working.

Run: uv run python -m ice_sims.mdb_gateway [--broker HOST] [--port PORT] [--machine-id ID]
"""

from ice_sims.base import ESP32Simulator
from ice_sims.mdb.simulator import MdbSimulator

__all__ = ["MdbSimulator"]


if __name__ == "__main__":
    ESP32Simulator.entry_point(MdbSimulator)
