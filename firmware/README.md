# firmware

The field-device (board) layer: ESP32 subsystems that own the hardware (MDB bus, sensors,
motors, dispensers) and talk to the edge controller over MQTT.

## Layout

- `vending/` -- the vending board firmware: C++ on ESP-IDF, built with PlatformIO. Reference board
  KinCony KC868-A8. `lib/vendcore` is the pure-C++ core (no ESP-IDF includes, tested on the host);
  `src/` holds the FreeRTOS tasks and board drivers; `boards/kc868-a8.h` is the pin map and default
  input polarity; `test/` holds the host unit tests, fixtures and golden wire JSON.
- `docs/vending-board-interface.md` -- wiring, channel map, master-enable circuit, input polarity,
  provisioning and the Phase E bench checklist.

## Build, test, flash

Run from `firmware/vending/`. Install PlatformIO with `uv tool install platformio`. The host tests
need a host `g++` on PATH (on Windows, WinLibs: `winget install BrechtSanders.WinLibs.POSIX.UCRT`).

| Task | Command |
|---|---|
| Build the firmware | `pio run -e kc868-a8` |
| Host unit tests | `pio test -e native` |
| Refresh the golden wire JSON | `pio test -e native -f test_wire` |
| Flash over USB | `pio run -e kc868-a8 -t upload` |
| Serial monitor / console | `pio device monitor` |

Compile-time defaults (`-DVEND_DEFAULT_WIFI_SSID=...` and the rest) are listed in
`vending/platformio.ini`. The app partition is `partitions_singleapp_large.csv`.

## Console

The USB serial console (115200 baud, prompt `vend> `) persists settings to NVS: `set <key> <value>`,
`show` (secrets masked), `erase`, `reboot`. Keys: `wifi_ssid`, `wifi_pass`, `mqtt_host`, `mqtt_port`,
`mqtt_user`, `mqtt_pass`, `machine_id`, `use_eth`, `polarity`. A change applies after `reboot`.

## Conformance suite

`tests/firmware/` drives a board over MQTT. It is opt-in: `ICE_COLDER_BOARD_TESTS=1`, with
`ICE_COLDER_BOARD_BROKER` (`host[:port]`) and, for real hardware, `ICE_COLDER_BOARD_MACHINE_ID`;
without a machine id it runs against the vending simulator.

## Documents

- [Vending board interface](docs/vending-board-interface.md)
- [Design spec](../docs/superpowers/specs/2026-10-17-vending-esp32-firmware-design.md)

## Contracts the firmware implements

- `docs/contracts/vending-machine/` -- the vending-machine board contract.
- `docs/contracts/ice-maker-monitor/` -- the ice-maker monitor board contract.
- The layer-contracts spec, sections 2-3 (field device layer and the board-to-edge contract).

The machine-readable form of both contracts is generated from `ice_contracts.board`
(`uv run python -m ice_contracts.generate`).

## Simulation today

No physical board has been built yet. `ice_sims` (`packages/sims`) simulates them: it publishes and
consumes the same MQTT topics and honours the same contracts, so the edge controller can be
exercised end to end without hardware.
