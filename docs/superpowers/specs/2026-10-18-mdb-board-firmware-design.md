# MDB Payment Board — Firmware, Shared Board Platform and Flash Tool Design

> **Status:** Approved (brainstormed with owner), 2026-10-18. Spec 4 of the MDB ↔ edge work: the
> ESP32-S3 board that is MDB master for the IM600XL's coin changer, bill validator and cashless
> reader and speaks `ice_contracts.board.payment` over MQTT. Companion to the
> [payment contract design](2026-10-18-payment-contract-design.md) (spec 1, whose rules this firmware
> implements verbatim), the [vending ESP32 firmware design](2026-10-17-vending-esp32-firmware-design.md)
> (the precedent for stack, structure and testing, and the source of the shared platform), the
> [payment contract](../../contracts/payment/CONTRACT.md), the [I/O list](../../hardware/im600xl-io-list.md)
> §3 and the [layer-contracts spec](2026-10-13-layer-contracts-design.md) §2.

## 1. Goal

Firmware for the MDB payment board, the shared board platform it is built on (lifted from the vending
firmware so both boards and the future ice-maker board share one network, settings, console and
MQTT layer), a fault-tolerant flash-and-provision tool, an interface document for the carrier board,
and the test suites: host unit tests against byte-level fake peripherals, golden wire JSON validated by
the Python contract models, and an opt-in MQTT conformance suite.

The board is one payment source among several. The edge will put MDB cash, MDB cashless sessions and
the virtual gateways (Stripe, PayPal, Square QR, whatever follows) behind one gateway port in spec 3;
this firmware never assumes it is the only source of credit and never depends on what the other
sources do.

## 2. Decisions

| # | Question | Decision |
|---|---|---|
| D1 | Where the MDB master lives | Our own ESP32-S3 board over MQTT. The VMC host (a Raspberry Pi or PC) never touches the bus. Considered and rejected: a USB MDB adapter on the VMC host, because the two reliable vendors (Qibixx, Abrantix) have left the market and the remaining units are undocumented; and a dumb 9-bit USB-serial passthrough, because a non-realtime host cannot meet the 5 ms reply window. |
| D2 | Physical layer | Hardware UART at 9600 baud. The 9th (mode) bit is emulated: per-byte parity selection on transmit, per-byte parity-error flag in a custom RX interrupt handler on receive. Every received block is also terminated by the 1 ms inter-byte idle gap and validated by checksum, so a misread mode bit can never corrupt a block. Two-optocoupler transceiver. |
| D3 | Chip and board | ESP32-S3-DevKitC-1 (N8R8 class) on a carrier that holds the transceiver, the MDB connector and a 24 V → 5 V buck. Native USB for flashing and the console. |
| D4 | Code sharing | `firmware/common/` holds the chip-agnostic platform lifted from the vending firmware: `boardcore`, a pure C++ PlatformIO library (host-testable), and `boardidf`, an ESP-IDF component (the native mechanism for code that includes IDF headers); both projects consume them. `lib/mdbcore` is pure C++ with the bus, clock, ids and log store as ports. Considered and rejected: copying the infrastructure (two, soon three, copies to patch) and one firmware with a compile-time role (different chips, every MDB change re-tests the vending board). |
| D5 | Delivery | Flash-backed replay log in a dedicated NVS partition; payout results persisted for idempotency across reboot; contract 1.0.0 delivery rules verbatim. |
| D6 | Contract | Additive bump **1.0.0 → 1.1.0**: a `cash_sale{cents, sale_id}` command. Without it nothing lowers `escrow_cents` after an exact-payment sale, and flows 7 and 8 of the contract cannot work. |
| D7 | Fail-safe | Edge silence per contract §3.2. Brownout and watchdog reset leave the log intact and the peripherals disabled until a fresh acked `set_policy`. No hardware backstop: the board drives no actuator, and MDB peripherals disable themselves when polling stops. |
| D8 | Tooling | `scripts/flash_board.py` replaces `scripts/flash_vending.py`: a targets table, retry and port re-enumeration, post-flash verification over the console, optional provisioning from a file. |
| D9 | Gateways | The board stays ignorant of other payment sources. `cash_sale` carries only the cash portion the board physically holds, clamped at zero; MDB CASH SALE to the reader is audit only. |

## 3. Scope and layout

```
firmware/
  README.md                        updated: common/, mdb/, the flash tool
  common/                          shared platform
    boardcore/                     PlatformIO library (lib_extra_dirs), pure C++, host-testable: AckCache,
                                   OutboundRing, Prio, heartbeat/LWT/ack JSON, topic_join, WireCtx
    boardidf/                      ESP-IDF component (EXTRA_COMPONENT_DIRS): Settings (NVS), console REPL,
                                   net (Wi-Fi, optional Ethernet, SNTP), mqtt publisher; parameterised by a
                                   BoardIdentity (subsystem name, console prompt, NVS namespace, LWT tail)
  vending/                         re-pointed at common/; behaviour, tests and golden files unchanged
  mdb/                             PlatformIO project (ESP-IDF)
    platformio.ini                 envs: esp32-s3-devkitc-1 (target), native (host tests)
    partitions.csv                 nvs (settings), evlog (nvs, 128 KB), factory app
    boards/mdb-s3.h                pin map
    src/                           bus task, ledger task, comms task, NVS log store, UART 9-bit port, main
    lib/mdbcore/                   pure C++ MDB master and payment logic
    test/                          native unit tests, fake peripherals, golden wire JSON
  docs/mdb-board-interface.md      carrier board, connector and bus power, transceiver, pin map,
                                   provisioning, the flash tool, bench checklist, open items
scripts/flash_board.py             the flash-and-provision tool (replaces flash_vending.py)
tests/meta/test_flash_board.py     its pure-logic tests (replaces test_flash_vending.py)
tests/firmware/mdb/                opt-in conformance suite and the always-on golden wire check
packages/contracts/.../payment.py  1.1.0: CashSaleParams, cash_sale in PAYMENT_COMMANDS/PARAM_MODELS
docs/contracts/payment/            CONTRACT.md row and flow step, cash_sale_params.schema.json
```

CI: the `firmware` job gains `pio run -e esp32-s3-devkitc-1` and `pio test -e native` for `firmware/mdb`
and the golden drift check for `firmware/mdb/test/golden`; the vending build and tests stay as they
are and prove the `firmware/common` refactor.

**Out of scope:** bill recycler, cashless revalue, DEX/EVA-DTS, a second cashless reader at 0x60,
OTA, the edge-side gateway abstraction (spec 3), the simulator rewrite (spec 2), the carrier PCB
layout itself (the interface doc states its requirements).

## 4. Hardware and electrical interface

### 4.1 Carrier board

An ESP32-S3-DevKitC-1 socketed on a small carrier that holds the MDB transceiver, the MDB connector
and a 24 V → 5 V buck module fed from the machine's control rail. No relays, no actuators, so no
master-enable backstop. USB is for flashing and the console only.

### 4.2 MDB connector and bus power

The standard 6-pin MDB connector: bus power, power return, master receive, master transmit,
communications common, one unused. The VMC side supplies the peripherals' power (34 VDC nominal;
peripherals accept roughly 20 to 45 V). Whether that supply is a separate PSU in the IM600XL or lives
inside the PLC's MDB interface is unknown and is the first thing to find at the machine: if separate,
it is reused and the carrier only carries the signal lines through; if not, the carrier needs a bus
supply.

### 4.3 Transceiver and pin map

Two high-speed optocouplers (6N137 class) following the master transmit and master receive reference
circuits in MDB/ICP 4.3 §5, 5 V comms side from the board regulator, bus side powered from the bus.
The KiCad design of the open-source `mdb-esp32-master` project is a second check.

| Function | GPIO | Notes |
|---|---|---|
| MDB TX (UART1) | 17 | through the transmit opto |
| MDB RX (UART1) | 18 | through the receive opto |
| Status LED | 48 | the DevKitC's WS2812: link, bus, inhibited |
| Console / flash | native USB (GPIO 19/20) | not available for other use |

Strapping pins (0, 3, 45, 46) stay free. No digital inputs: `cash_collected` comes from the
peripherals themselves (validator "cash box out of position" then back; changer tube counts dropping
while acceptance is disabled).

### 4.4 Open items

None changes the architecture; each lands in the interface doc's open-items list: the existing MDB
interface make and the bus power source; the exact changer, validator and reader models and the
levels they negotiate; the connector pinout confirmed against the spec at the machine.

## 5. Firmware architecture

Three FreeRTOS tasks over a pure-C++ core, `lib/mdbcore`. No heap allocation after boot.

### 5.1 Tasks

- **Bus task** (core 1, highest priority) owns UART1 and the master state machine. It round-robins
  the three peripherals, waits at most 5 ms for a response, keeps the 1 ms inter-byte rule, and never
  touches flash or the network, so a reconnect or an NVS write can never make the master miss a
  reply.
- **Ledger task** (core 0, high priority) owns the flash replay log. Every money event from the bus
  task is appended to NVS, receives its `seq`, and only then is handed to comms. It also persists the
  payout idempotency store. The window in which a coin exists only in RAM is one queue hop, a few
  milliseconds.
- **Comms task** (core 0) is the vending comms task on the shared platform: Wi-Fi, MQTT, JSON, the
  32-entry ack cache, the outbound ring, heartbeat, retained capabilities, the `payment/state`
  publisher, and command decoding onto `cmd_q`.

Queues (fixed size): `cmd_q` (comms → bus: decoded commands, EdgeSeen), `evt_q` (bus → ledger, 64
deep, never drops a money event), `pub_q` (ledger → comms), `ack_q` (bus → comms).

### 5.2 `mdbcore` modules

Ports: `BusPort` (write a block of 9-bit bytes; receive bytes with their mode bit and arrival time),
`Clock`, `IdSource` (random `boot_id`, `event_id`), `LogStore` (append, erase-up-to, iterate).

- **`Link`**: builds command blocks (address byte with mode bit, data bytes, checksum), collects a
  response until the mode-bit checksum byte or a 1 ms idle gap, verifies the checksum, answers ACK,
  sends RET on a bad block and NAK as the spec requires, retries once, counts misses; a device past
  its miss threshold (default 10 consecutive) is `offline` and re-enters RESET.
- **`Changer`** (0x08, level 2 and 3): RESET → POLL (just reset) → SETUP → EXPANSION IDENTIFICATION
  (level 3; enable alternative payout) → TUBE STATUS → COIN TYPE → POLL. Deposits become
  `coin_accepted{cents, routed}` (a rejected coin is nothing), the lever becomes
  `escrow_return_pressed`, status bytes map to `device_status{status, mdb_status}`, tube counts feed
  `state.tubes` and the exact-change verdict (payable tube total below `change_threshold_cents`).
  Payout uses level 3 PAYOUT with PAYOUT VALUE POLL, or level 2 per-coin DISPENSE greedy by tube, and
  always reports the exact `paid_cents`. Coin values scale by the changer's scaling factor in integer
  math only.
- **`Validator`** (0x30, level 1 and 2): RESET → POLL (just reset) → SETUP → (EXPANSION
  IDENTIFICATION) → STACKER → BILL TYPE (escrow enabled) → POLL. A bill in escrow is judged against
  the policy on the spot (accept flag, escrow cap, exact-change threshold) and stacked or returned
  with `bill_escrowed` then `bill_stacked` or `bill_returned{policy}`; the changer's lever with
  `return_bill_on_lever` returns it as `bill_returned{customer}`; cash box out and back becomes
  `cash_collected{bill}`; stacker full is `status: full`.
- **`Cashless`** (0x10, level 1, plus always-idle when the reader offers it and the policy enables
  cashless): RESET → POLL (just reset) → SETUP config → SETUP prices → EXPANSION REQUEST ID → READER
  ENABLE → POLL. BEGIN SESSION → `session_begun`; `vend_request` → VEND REQUEST → `vend_approved` /
  `vend_denied`; `vend_result` → VEND SUCCESS or VEND FAILURE → SESSION COMPLETE → END SESSION →
  `session_ended{complete}`; `end_session` → SESSION COMPLETE → `session_ended{edge}`; reader cancel
  → `session_ended{reader}`; `cash_sale` → CASH SALE; no `vend_result` within `edge_timeout_s` →
  VEND FAILURE → `session_ended{timeout}`. READER DISABLE when the policy disables cashless.
- **`Policy`**: the acked `SetPolicyParams`; applies enables on the bus; `inhibited_reason`.
- **`Accounts`**: `escrow_cents`, `bill_in_escrow_cents`, stacker, `cash_sale` decrement, payout
  idempotency (last 32 payout `request_id`s with their results, persisted).
- **`EdgeWatch`**: time since the last `cmd/mdb` message; the §3.2 silence sequence.
- **`EventLog`**: ring of envelopes with per-boot `seq`; erase-up-to `(boot_id, seq)`; replay in
  boot order.

### 5.3 Ack phases

`payout` and `vend_request` are acked `accepted` only after the peripheral has ACKed the MDB command;
`set_policy` is acked `ok` after the COIN TYPE, BILL TYPE and READER enables have been applied on the
bus; everything else acks from the bus task's confirmation, never from receipt. `ping`, `self_test`
and `force_report` ack `completed` from the comms task directly. `self_test` reports: each device's
status and level, clock synced, event log depth and `event_log_full`, RX mode-bit disagreement
counter, link RSSI, dropped-message counters.

## 6. Wire behaviour

All topics under `vmc/<machine_id>/`. Payload rules are the payment contract's, verbatim.

| Topic | When | Payload |
|---|---|---|
| `capabilities/mdb` (retained) | connect, `force_report` | `SubsystemCapabilities`: `contract_version: "1.1.0"`, `firmware` semver plus git sha, `hardware_id` MAC, `ip`, `channels: []`, `commands`: ping, self_test, force_report, set_policy, payout, vend_request, vend_result, end_session, return_escrow, device_reset, cash_sale |
| `heartbeat/mdb` | every 10 s; LWT `uptime_seconds: -1` | `SubsystemHeartbeat` |
| `payment/event` (QoS 1) | each event, replayed from the log first | `PaymentEventEnvelope`; `event_id` = `<boot_id>-<seq>`; `ts` from SNTP once synced, else best effort from the boot clock with `self_test` reporting the clock unsynced |
| `payment/state` (QoS 1, retained) | every change, `force_report`, every 30 s | `PaymentState` |
| `cmd/mdb/ack` | per contract | `CommandAck` |
| `homeassistant/.../config` (retained) | connect | discovery for escrow, accepting, exact change, one status per device |

Subscriptions: `cmd/mdb`, `payment/event/ack`.

**Replay.** On connect the ledger task walks the log oldest boot first and hands every unacked
envelope to comms in order before any new event. An ack `{boot_id, seq}` erases entries of that
`boot_id` with `seq <=` the acked value and nothing else. Capacity is 256 envelopes in a 128 KB NVS
partition. The board does not run the log to capacity: at 192 pending (3/4) it sets log pressure and
inhibits acceptance with `inhibited_reason: device_fault` (all three devices disabled on the bus like
the silence sequence; no bill is returned, no session ended; payouts, cash sales and `set_policy`
still work). Pressure clears at 128 pending (hysteresis), or never when the store is unusable at
boot. `self_test` fails `event_log_full` exactly while pressure is set, and the LED pattern changes.
Pressure never lifts `no_policy`, `edge_silent` or `policy_off`, which outrank it. "The whole unacked
log in order before anything new" is enforced with a replay epoch: the ledger stamps each outgoing
envelope, every connect edge and every replay request bumps the epoch, and comms drops envelopes
carrying a stale one; an envelope comms cannot publish or hold latches "replay owed" and every later
one is dropped until the replay is requested.

**Boot.** New random `boot_id`; `seq` starts at 1; `inhibited_reason: no_policy`; the three
peripherals go through RESET and setup; the state doc is published as soon as the devices have
answered, so the edge's reconcile (contract flow 8) sees real tube and escrow figures, never zeros.

**Contract 1.1.0 addition.** `cash_sale` with `CashSaleParams{cents: PositiveCents, sale_id: str | None
= None}`: ack only; the board lowers `escrow_cents` by `cents`, clamped at zero (ack `ok` with
`detail: "clamped"` when it clamps), publishes state, and sends MDB CASH SALE to the reader when one
is present. Added to `PAYMENT_COMMANDS`, `PARAM_MODELS` and `COMMAND_PARAM_VALIDATORS`;
`MDB_TESTABLE_COMMANDS` unchanged; `CONTRACT.md` gains the command row and flow 1 the step "edge sends
`cash_sale{price, sale_id}`"; `cash_sale_params.schema.json` is generated.

**Edge silence** (contract §3.2) is measured from the last message on `cmd/mdb`; acks on
`payment/event/ack` do not count. **Time.** SNTP from the broker host by default; `self_test` says
whether the clock is synced.

## 7. Error handling

| Condition | Behaviour |
|---|---|
| No response within 5 ms | Retry the block once; count a miss. Past the miss threshold the device is `offline`, `device_status` is emitted, the master restarts its RESET sequence for that device and keeps polling the others |
| Bad checksum or malformed response | RET once; a second bad block is NAKed and counted as a miss |
| Mode-bit decode disagrees with idle gap and checksum | The checksum-valid block wins; a diagnostics counter is reported in `self_test` so a flaky RX ISR is visible before it matters |
| Peripheral status: jammed, tilt, defective sensor, ROM error | `device_status{status, mdb_status}` with the raw byte; that device's acceptance is disabled, the others keep running; re-enabled when the status clears |
| "Possible credited coin removal" / "possible credited bill removal" | `device_status{status: error}` with the raw byte; no credit is reversed by the board; the operator decides |
| Bill escrowed but the decision cannot be sent | The validator's own escrow timeout returns the bill; the board emits `bill_returned{timeout}` when the poll reports it |
| Payout shortfall or changer refuses | `payout_result{partial, paid_cents}` or `{failed, 0}`; a repeated `request_id` replays the stored result, across reboot too |
| `vend_approved` with no `vend_result` for `edge_timeout_s` | VEND FAILURE to the reader, `session_ended{timeout}`, nothing charged |
| Reader cancels or times out its own session | `session_ended{reader}`; a pending `vend_request` is answered `vend_denied{reason: "session_ended"}` |
| Edge silent for `edge_timeout_s` | Contract §3.2: disable all, return the escrowed bill, end the session, `inhibited_reason: edge_silent`; coins stay owed and visible in `escrow_cents` |
| Broker lost | The bus keeps running on the policy; events accumulate in flash; everything replays on reconnect; the edge-silence timer still fires because `cmd/mdb` stops arriving |
| Power loss or watchdog reset | New `boot_id`; unacked log entries replay first; `no_policy` until `set_policy`; peripherals are reset, so a bill mid-escrow is returned by the validator itself |
| `evt_q` or `pub_q` full | Money events are never dropped: the bus task defers the next POLL to that device until the queue drains (peripherals tolerate hundreds of milliseconds between polls); only state republishes are coalesced |
| Replay log pressure (192 of 256 pending; clears at 128) | Acceptance inhibited with `device_fault` before the log can fill, so money already taken is always recordable; `self_test` fails `event_log_full`; LED pattern changes. Unusable store at boot: permanent |
| Malformed command, unknown command, expired `payout`/`vend_request` | `rejected` naming the field, `unsupported`, `rejected` with `detail: "expired"`, per contract |
| `cash_sale` larger than `escrow_cents` | Clamp to zero, ack `ok` with `detail: "clamped"`, so a mixed cash-plus-virtual sale never fails |

## 8. Testing

**Host unit tests** (`pio test -e native` in `firmware/mdb`, against `lib/mdbcore` with a fake
`BusPort`, `Clock`, `IdSource`, `LogStore`). The fakes are byte-level peripheral emulators,
`FakeChanger`, `FakeValidator` and `FakeReader`: scripted MDB slaves that answer blocks and can be
told to miss, corrupt, jam, drop a coin, escrow a bill, begin a session, deny a vend. Covered:

- `Link`: block framing and checksum, mode-bit terminator, idle-gap terminator, RET on a bad block,
  retry and miss counting, offline and re-RESET.
- `Changer`: setup parsing and scaling to cents, coin routing, tube status to `state.tubes` and the
  exact-change verdict, level 3 PAYOUT with exact `paid_cents`, level 2 DISPENSE fallback, partial
  and failed payouts, lever to `escrow_return_pressed`, every status byte to its `DeviceStatus`.
- `Validator`: the escrow decision for each policy rule (accept false, cap, exact-change threshold),
  stack and return events, cash box out to `cash_collected`, stacker full.
- `Cashless`: the full session ladder, deny, reader cancel, the mandatory `vend_result` timeout, CASH
  SALE on `cash_sale`, always-idle negotiation when offered and not otherwise.
- `Policy`/`Accounts`/`EdgeWatch`: no policy no money, the silence sequence, the `cash_sale` clamp,
  payout idempotency across a simulated reboot.
- `EventLog`: append, `seq` monotonic per boot, erase-up-to by the right `boot_id` only, replay order
  across two boots, capacity behaviour.

**Golden wire JSON** (`firmware/mdb/test/golden/`, refreshed by `pio test -e native -f test_wire`,
byte-checked in CI): one file per event kind, the state doc, capabilities, heartbeat, LWT, each ack
status. `tests/firmware/mdb/test_golden_wire.py` validates every file against the Python contract
models, always on, no broker, the vending board's pattern.

**Conformance suite** (`tests/firmware/mdb/`, opt-in with the existing `ICE_COLDER_BOARD_*` env
vars): retained capabilities and the 1.1.0 version, heartbeat and LWT, standard commands, duplicate
`request_id` replay, `set_policy` echoed in state, `payout` and `vend_request` two-phase within the
contract timeouts, `return_escrow` with no bill, `rejected` on bad params and `expired`, replay after
a forced disconnect, acks erasing the log. Driving coins and bills needs the simulator, so those tests
carry `requires_sim` and start running when spec 2 ships; until then the suite runs against hardware
only and, with no machine id set, skips with the standing opt-in reason.

**Bench** (a checklist in the interface doc, not automated): RX mode-bit decode confirmed with a
logic analyser against the real changer; the idle-gap fallback exercised by forcing the ISR path off;
a coin and a bill end to end; a payout from each tube; bus power measured; a power cut mid-session
replayed on the next boot.

## 9. Tooling, CI and documentation

### 9.1 `scripts/flash_board.py`

Replaces `flash_vending.py` (the `tooling/flash-vending-script` branch is folded into this branch; its
tests move to `tests/meta/test_flash_board.py`).

```
uv run python scripts/flash_board.py mdb     [--port COMx] [--no-build] [--monitor] [--provision mdb.toml] [--verify-only]
uv run python scripts/flash_board.py vending [--port COMx] [--no-build] [--monitor] [--provision vending.toml] [--verify-only]
```

- **Targets table**: `vending` → `firmware/vending`, env `kc868-a8`, prompt `vend> `; `mdb` →
  `firmware/mdb`, env `esp32-s3-devkitc-1`, prompt `mdb> `. Adding a board is one row.
- **Detection**: the known USB-serial bridge vendor ids plus the Espressif native USB id; one board
  flashes, several ask for `--port`, none exits 0 with a message.
- **Fault tolerance**: the upload is retried once after a 2 s pause; an S3 that re-enumerates on a
  different port after reset is found again by its USB serial number; a port busy because a monitor
  is open is reported, not retried into; a failed build never attempts an upload.
- **Verification**: after upload the tool opens the console, waits up to 15 s for the firmware's boot
  banner (`firmware <version>`), runs `show`, and confirms the version matches the build. Exit codes:
  0 verified, 1 flashed but unverified, 2 build or upload failed, 3 usage.
- **Provisioning**: `--provision file.toml` sends `set <key> <value>` for each key (`wifi_ssid`,
  `wifi_pass`, `mqtt_host`, `mqtt_port`, `mqtt_user`, `mqtt_pass`, `machine_id`), then `reboot`, then
  re-verifies; secrets never appear in the tool's output. `--verify-only` skips build and upload.
- Everything that does not touch a port or a subprocess (device list parsing, the target table,
  banner parsing, provisioning file validation, exit code mapping) is pure and unit-tested.

### 9.2 CI

The `firmware` job gains `pio run -e esp32-s3-devkitc-1` and `pio test -e native` in `firmware/mdb` and
`git diff --exit-code -- firmware/mdb/test/golden`; the PlatformIO cache key covers both
`platformio.ini` files.

### 9.3 Documentation

`firmware/docs/mdb-board-interface.md` (carrier board, connector and bus power, transceiver, pin map,
provisioning, the flash tool, bench checklist, open items); `firmware/README.md` (layout with `common/`
and `mdb/`, the tool); `docs/contracts/payment/CONTRACT.md` and schemas for 1.1.0; `CLAUDE.md` (layout,
commands, the CI job, the test env vars, the `firmware/common` rule: platform code lives there, never
copied); `ROADMAP.md` Phase D notes the MDB board; the I/O list's MDB open item points at the interface
doc.

## 10. Open items this design depends on

From §4.4, each affects only a default or the interface doc, never the architecture: the existing MDB
interface make and bus power source; the peripheral models and negotiated levels; the connector pinout
confirmed at the machine; whether the Nayax reader offers always-idle.

## 11. As built

- **Log pressure (task 12 fixes).** Replaces "keep accepting at capacity": `Master::set_log_pressure`
  inhibits with `device_fault` at 192 pending and releases at 128 (`EventLog::next_pressure`); the
  ledger computes it and the bus task applies it every round. `g_store_ok == false` is permanent pressure.
- **Flash quiet window.** All NVS writes and the blob reads inside `acked` run in a window the bus
  task grants between transactions (`src/flash_window.h`), so flash cache stalls on core 0 can never
  freeze an MDB transaction on core 1. `CONFIG_SPI_FLASH_AUTO_SUSPEND` is enabled as a second layer.
- **Line-idle read deadlines.** `BusPort::read` timeouts are line-idle times; the UART port adds one
  character time and margin (`rx_deadline_us`). TX leaves the shared parity setting at EVEN.
- **Replay ordering.** Epoch-stamped Outcome envelopes and a "replay owed" latch in comms (see section 6).
- **Bus task does not log.** It counts (`BusStats`, carried in the state mailbox); the ledger logs them.
