# firmware

The field-device (board) layer: ESP32 subsystems that own the hardware (MDB bus, sensors,
motors, dispensers) and talk to the edge controller over MQTT.

ESPHome configs will live here. None exist yet.

## Contracts the firmware implements

- `docs/contracts/vending-machine/` -- the vending-machine board contract.
- `docs/contracts/ice-maker-monitor/` -- the ice-maker monitor board contract.
- The layer-contracts spec, sections 2-3 (field device layer and the board-to-edge contract).

The machine-readable form of both contracts is generated from `ice_contracts.board`
(`uv run python -m ice_contracts.generate`).

## Simulation today

No physical boards exist yet. `ice_sims` (`packages/sims`) simulates them: it publishes and
consumes the same MQTT topics and honours the same contracts, so the edge controller can be
exercised end to end without hardware.
