# firmware

The field-device (board) layer: ESP32 subsystems that own the hardware (MDB bus, sensors,
motors, dispensers) and talk to the edge controller over MQTT.

## Layout

- `common/` -- the shared board platform, consumed by every project and **never copied into one**: `boardcore` is a pure C++ PlatformIO library (`lib_extra_dirs`, host-testable: ack cache, outbound ring, heartbeat/LWT/ack JSON, topic helpers) and `boardidf` is an ESP-IDF component (`EXTRA_COMPONENT_DIRS`: NVS settings, console, Wi-Fi/Ethernet/SNTP, MQTT publisher), parameterised by a `BoardIdentity` (subsystem name, console prompt, NVS namespace). Fix platform bugs there, once.
- `vending/` -- the vending board firmware: C++ on ESP-IDF, built with PlatformIO. Reference board
  KinCony KC868-A8. `lib/vendcore` is the pure-C++ core (no ESP-IDF includes, tested on the host);
  `src/` holds the FreeRTOS tasks and board drivers; `boards/kc868-a8.h` is the pin map and default
  input polarity; `test/` holds the host unit tests, fixtures and golden wire JSON.
- `mdb/` -- the MDB payment board firmware: ESP32-S3-DevKitC-1, C++ on ESP-IDF, built with PlatformIO. `lib/mdbcore` is the pure-C++ MDB master (changer, validator, cashless reader, replay log, accounts) behind bus/clock/id/log-store ports and tested on the host against byte-level fake peripherals; `src/` holds the bus, ledger and comms tasks, the UART 9-bit port, the NVS log store and the status LED; `boards/mdb-s3.h` is the pin map; `test/` holds the host tests and golden wire JSON.
- `docs/mdb-board-interface.md` -- carrier board, MDB connector and bus power, the 9-bit path, provisioning, replay log, LED patterns, the bench checklist and open items.
- `docs/vending-board-interface.md` -- wiring, channel map, master-enable circuit, input polarity,
  provisioning and the Phase E bench checklist.

## Build, test, flash

Run from the project directory, `firmware/vending/` or `firmware/mdb/` (or add `-d firmware/<project>` from the repo root). Install PlatformIO with `uv tool install platformio`. The host tests
need a host `g++` on PATH (on Windows, WinLibs: `winget install BrechtSanders.WinLibs.POSIX.UCRT`).

| Task | Command |
|---|---|
| Build the vending firmware | `pio run -e kc868-a8` (in `firmware/vending/`) |
| Build the MDB board firmware | `pio run -e esp32-s3-devkitc-1` (in `firmware/mdb/`) |
| Build and flash whatever board is plugged in (from the repo root) | `uv run python scripts/flash_board.py vending` or `mdb` (`--port COMx`, `--no-build`, `--monitor`, `--verify-only`, `--provision FILE.toml`) |
| Host unit tests (either project) | `pio test -e native` |
| Refresh the golden wire JSON (either project) | `pio test -e native -f test_wire` |
| Flash over USB (without the tool) | `pio run -e kc868-a8 -t upload` / `pio run -e esp32-s3-devkitc-1 -t upload` |
| Serial monitor / console | `pio device monitor` |

Compile-time defaults (`-DBOARD_DEFAULT_WIFI_SSID=...` and the rest) are listed in
`vending/platformio.ini` and `mdb/platformio.ini`. The vending app partition is `partitions_singleapp_large.csv`; the MDB board has its own `partitions.csv` (NVS settings, a 128 KB `evlog` replay-log partition, factory app).

**The flash tool.** `scripts/flash_board.py` builds, flashes, verifies and optionally provisions either board. It finds the attached USB-serial board (several need `--port`), retries a failed upload once, follows an S3 that re-enumerates on another port, waits for the `firmware <version>` boot banner and checks the version and `show` over the console. Exit codes: 0 verified (or no board attached), 1 flashed but unverified, 2 build or upload failed, 3 usage. `--provision FILE.toml` sets the console keys from a TOML file without printing secrets. Verification needs `pyserial`.

## Console

The USB serial console (115200 baud, prompt `vend> `; the MDB board uses `mdb> ` and NVS namespace `mdb`) persists settings to NVS: `set <key> <value>`,
`show` (secrets masked), `erase`, `reboot`. Keys: `wifi_ssid`, `wifi_pass`, `mqtt_host`, `mqtt_port`,
`mqtt_user`, `mqtt_pass`, `machine_id`, and on the vending board only `use_eth` and `polarity` (the MDB
board has neither). Values containing spaces or quotes are quoted: `set wifi_ssid "my net"`. A change
applies after `reboot`. On the MDB board the console is the DevKitC's native USB port (USB Serial/JTAG).

## Conformance suite

`tests/firmware/` drives a board over MQTT. It is opt-in: `ICE_COLDER_BOARD_TESTS=1`, with
`ICE_COLDER_BOARD_BROKER` (`host[:port]`) and, for real hardware, `ICE_COLDER_BOARD_MACHINE_ID`;
without a machine id the vending tests run against the vending simulator. `tests/firmware/mdb/` is the MDB board's suite: its hardware tests need a real board (`ICE_COLDER_BOARD_MACHINE_ID`) until an MDB simulator ships, and skip otherwise; its golden wire check always runs.

## Documents

- [Vending board interface](docs/vending-board-interface.md)
- [MDB board interface](docs/mdb-board-interface.md)
- [MDB board design spec](../docs/superpowers/specs/2026-10-18-mdb-board-firmware-design.md)
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
