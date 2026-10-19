# MDB board interface

The ESP32-S3 board that is MDB master for the IM600XL's coin changer, bill validator and cashless
reader, and speaks `ice_contracts.board.payment` over MQTT. Companion to the
[design spec](../../docs/superpowers/specs/2026-10-18-mdb-board-firmware-design.md) (whose section 11
lists what was built differently from the plan) and the
[payment contract](../../docs/contracts/payment/CONTRACT.md). This document is what the carrier
board and the bench work need; the firmware behaviour itself is specified by the contract and the spec.

## 1. Purpose and contract

The board drives no actuator and has no digital input. It owns the MDB bus (9600 baud, three
peripherals: changer 0x08, validator 0x30, cashless reader 0x10) and publishes every money event to
the edge with at-least-once delivery. It implements `ice_contracts.board.payment` **1.1.0**
(`capabilities/mdb.contract_version = "1.1.0"`); 1.1.0 adds the `cash_sale` command over 1.0.0.

- Topics, all under `vmc/<machine_id>/`: `payment/event` (QoS 1), `payment/event/ack` (subscribed),
  `payment/state` (QoS 1, retained), `cmd/mdb` (subscribed), `cmd/mdb/ack`, `heartbeat/mdb` (every 10 s,
  last will `uptime_seconds: -1`), `capabilities/mdb` (retained).
- Every amount is an integer number of cents.
- Edge silence: the board measures it from the last message on `cmd/mdb` only (acks on
  `payment/event/ack` do not count). After `edge_timeout_s` (default 30) it disables all three devices,
  returns an escrowed bill, ends any session and reports `inhibited_reason: edge_silent`. Coins stay
  owed and visible in `escrow_cents`. Only an acked `set_policy` lifts it.
- After any reset the board has a new `boot_id`, accepts nothing (`no_policy`) and waits for a fresh
  acked `set_policy`.
- Ack phases: `payout` and `vend_request` ack `accepted` only after the peripheral ACKed the MDB
  command; `set_policy` acks `ok` after the enables are applied on the bus; `ping`, `self_test` and
  `force_report` ack `completed` from the comms task. A command is never acked `failed` while it is
  still queued on the bus: it is acked `accepted`, or `ok` with `detail: "queued"` for immediate
  commands.

## 2. Carrier board and power

An ESP32-S3-DevKitC-1 (N8R8 class) socketed on a small carrier that holds the MDB transceiver
(section 4), the MDB connector (section 3) and a 24 V to 5 V buck module fed from the machine's
control rail. There are no relays and no actuators, so there is no master-enable backstop (the
peripherals disable themselves when polling stops). USB is for flashing and the console only.

Carrier requirements the layout must meet:

- Buck input range covers the control rail (24 V nominal) with margin; output feeds the DevKitC 5 V
  pin. The USB port may be connected while the buck is powered, so the DevKitC's own USB-to-5 V path
  must not back-feed the buck (diode or buck with reverse protection).
- The MDB side (bus power, power return, signal lines) is isolated from the 5 V logic side by the
  optocouplers only. No shared ground between the bus side and the logic side.
- Strapping pins (GPIO 0, 3, 45, 46) are left free.

**Open item: bus power.** The VMC side supplies the peripherals' power (34 VDC nominal; peripherals
accept roughly 20 to 45 V). Whether that supply is a separate PSU in the IM600XL or lives inside the
PLC's MDB interface is unknown. If separate, it is reused and the carrier only carries the signal
lines through; if not, the carrier needs a bus supply. This is the first thing to find at the machine
(section 9, section 10).

## 3. MDB connector pinout

The standard 6-pin MDB connector (MDB/ICP 4.3). Pin numbers below follow the standard as the spec
recalls it; **confirm them against the spec and the machine's own harness before wiring**
(section 10).

| Pin | Signal | Carrier connection |
|---|---|---|
| 1 | Bus power (34 VDC nominal) | bus supply; not connected to the logic side |
| 2 | Power return | bus-side ground of the transceiver |
| 3 | Master receive (peripheral to master) | receive optocoupler input |
| 4 | Master transmit (master to peripheral) | transmit optocoupler output |
| 5 | Communications common | bus-side signal return of the transceiver |
| 6 | Not used | unconnected |

## 4. Transceiver and pin map

Two high-speed optocouplers (6N137 class) following the master transmit and master receive reference
circuits in MDB/ICP 4.3 section 5; the 5 V comms side comes from the board regulator and the bus side
is powered from the bus. The KiCad design of the open-source `mdb-esp32-master` project is a second
check. The firmware pin map is `firmware/mdb/boards/mdb-s3.h`.

| Function | GPIO | Notes |
|---|---|---|
| MDB TX (UART1) | 17 | through the transmit opto |
| MDB RX (UART1) | 18 | through the receive opto |
| Status LED | 48 | the DevKitC's WS2812 |
| Console and flash | native USB (GPIO 19/20) | not available for other use |

There are no digital inputs: `cash_collected` is derived from the peripherals themselves (validator
cash box out of position and back; changer tube counts dropping while acceptance is disabled).

## 5. The 9-bit path as shipped

MDB frames are 8 data bits plus a mode bit plus one stop bit; the ESP32 UART has no 9-bit mode, so the
mode bit is emulated with the parity bit.

- **Transmit.** Per byte the port waits for the UART to finish (`uart_wait_tx_done`), sets the parity
  (even or odd) so the parity bit equals the wanted mode bit, and writes one byte. The shared parity
  setting is **restored to EVEN after every transmit**.
- **Receive.** The UART runs 8E1 with a RX FIFO threshold of 1 and an event queue. For each received
  byte the port decodes `mode = (popcount(byte) & 1) XOR parity_error`, taking the per-byte
  parity-error event from the UART driver's event queue (the driver posts the data event first, then
  its parity-error event). A multi-byte batch without parity errors decodes every byte as
  `mode = popcount & 1`. A multi-byte batch **containing** a parity error is ambiguous: every byte is
  decoded `mode = false` (the fallback) and the batch is counted as ambiguous.
- **Block end.** A response ends on the byte whose mode bit is set (the checksum byte) or on a 1 ms
  line-idle gap; the checksum (8-bit sum of address and data bytes) validates the block either way, so
  a misread mode bit can never corrupt a block, only be counted.
- **Read deadlines** are line-idle times: the port waits the requested idle time plus one character
  time (1150 us) and a 250 us margin. The first response byte is allowed 5 ms. Below 3 ms remaining
  the read spins on the microsecond timer instead of blocking, because the FreeRTOS tick is 1 ms.
- **Disagreement counter.** Every block whose mode-bit decode disagrees with the idle gap is counted;
  `self_test` reports it as the mode-bit disagreement counter. Alongside it the board counts ambiguous
  batches, orphan parity errors and lost RX bytes, and logs the non-zero counters once a minute. The
  bus task never logs itself: it only counts, and the ledger task logs.

**Unverified on silicon.** The decode relies on the ordering of the driver's data and parity-error
events, read from the ESP-IDF 5.5.3 source and not yet observed on hardware. The first bench check
(section 9) confirms it with a logic analyser against the real changer; the fallback if it misbehaves
is forcing `mode = false` in the receive handler (a one-line source change).

## 6. Provisioning

Flash and provision with the repository tool (from the repo root; needs `pio` on PATH and pyserial):

```
uv run python scripts/flash_board.py mdb [--port COMx] [--no-build] [--monitor] [--provision mdb.toml] [--verify-only]
```

| Flag | Effect |
|---|---|
| `--port PORT` | program this port, skipping auto-detection (required when several boards are attached) |
| `--no-build` | upload the existing build |
| `--monitor` | open the serial monitor last |
| `--provision FILE.toml` | after upload, send `set <key> <value>` for each key, `reboot`, and re-verify |
| `--verify-only` | skip build and upload; verify (and provision) only |

Behaviour: the tool builds `esp32-s3-devkitc-1` in `firmware/mdb`, finds the one attached board by
USB-serial vendor id (CH340/CH9102, CP210x, FTDI or the Espressif native USB id), and uploads with one
retry after a 2 s pause. An S3 that re-enumerates on another port after reset is found again by its USB
serial number; a busy port (monitor open) is reported, not retried into; a failed build never attempts
an upload. After upload it waits up to 15 s for the `firmware <version>` boot banner, runs `show`, and
checks the version against the build. Secrets in the provisioning file are never printed.

| Exit code | Meaning |
|---|---|
| 0 | verified (also: no board attached, with a message; nothing flashed) |
| 1 | flashed but unverified (or provisioning failed to apply) |
| 2 | build or upload failed |
| 3 | usage error: bad flags, unusable provisioning file, or several boards and no `--port` |

The provisioning file is TOML with string values for `wifi_ssid`, `wifi_pass`, `mqtt_host`,
`mqtt_port`, `mqtt_user`, `mqtt_pass` and `machine_id`; any other key is refused.

**Console.** USB serial, 115200 baud, prompt `mdb> `, settings persisted in NVS namespace `mdb`.

| Command | Effect |
|---|---|
| `set <key> <value>` | store a setting; takes effect after `reboot` |
| `show` | print every setting, secrets masked |
| `erase` | erase all stored settings |
| `reboot` | restart |

Keys: `wifi_ssid`, `wifi_pass`, `mqtt_host`, `mqtt_port` (default 1883), `mqtt_user`, `mqtt_pass`,
`machine_id`. The board is Wi-Fi only; `use_eth` is accepted by the shared console but ignored here.

**Compile-time defaults.** `-DBOARD_DEFAULT_WIFI_SSID=\"...\"`, `BOARD_DEFAULT_WIFI_PASS`,
`BOARD_DEFAULT_MQTT_HOST`, `BOARD_DEFAULT_MQTT_PORT`, `BOARD_DEFAULT_MQTT_USER`,
`BOARD_DEFAULT_MQTT_PASS`, `BOARD_DEFAULT_MACHINE_ID` (see the comment in `firmware/mdb/platformio.ini`;
quote string values). NVS values override them. There is no captive portal and no OTA.

**Flash-write safety.** A console `set` or `erase` writes flash, which can stall the CPU long enough to
disturb an MDB transaction. Console writes therefore go through the same quiet window the replay log
uses (section 7): the bus parks between transactions for the write. If no window is granted within
about one second the write proceeds anyway. Wi-Fi driver storage is RAM, not NVS, so the radio never
writes flash behind the bus task's back.

## 7. Replay log and what a full log means

Every money event is appended to a dedicated NVS partition (`evlog`, 128 KB, capacity 256 envelopes)
before it is published, receives its per-boot `seq`, and is erased only when the edge acks it
(`payment/event/ack` with `{boot_id, seq}` erases entries of that `boot_id` with `seq <=` acked). On
every broker connect the log replays oldest boot first, ahead of anything new. A stale-epoch rule drops
envelopes queued before a reconnect so the order holds. The payout idempotency store (last 32 payout
`request_id`s with results) is persisted in the same namespace so a reboot cannot repay a payout.

**All flash work happens in a quiet window.** The ledger task requests a window; the bus task parks
between transactions (holding for at most 400 ms), the ledger does every NVS operation, then releases
the bus. `CONFIG_SPI_FLASH_AUTO_SUSPEND` is also enabled as a second layer.

**What a full log means.** The board never runs the log to capacity. At **192 of 256** entries pending
it inhibits acceptance with `inhibited_reason: device_fault` (all three devices disabled on the bus; no
bill is returned, no session ended; `payout`, `cash_sale` and `set_policy` still work) and `self_test`
fails `event_log_full`. The pressure releases at **128** pending. A pending count that stays high means
the edge is not acking: check the broker, the edge's MQTT connection and `payment/event/ack` traffic.
Pressure never lifts `no_policy`, `edge_silent` or `policy_off`. If the log store is unusable at boot
the pressure is permanent: the board accepts no money until the store is repaired, and the LED
shows the log-full pattern.

## 8. LED patterns

One WS2812 on GPIO 48, deliberately dim. First match wins:

| Pattern | Meaning |
|---|---|
| White, blinking 1 Hz | log pressure: replay log at or above 192 pending, or the store is unusable |
| Blue, blinking 1 Hz | MQTT not connected |
| Red, steady | connected, a device is offline |
| Green, steady | connected, accepting money |
| Amber, steady | connected, not accepting (no policy, edge silent, policy off) |

## 9. Bench checklist

Tabletop: board on its carrier with a USB cable, the real changer, validator and reader on a bench MDB
harness with their bus supply, an MQTT broker reachable, `machine_id` set, a logic analyser on GPIO 17
and 18. Commands go to `vmc/<machine_id>/cmd/mdb` as `{"command", "params", "request_id", "timestamp"}`;
acks come back on `cmd/mdb/ack`.

- [ ] **Bus power.** Find the machine's bus supply (section 2 open item) and measure it under load: 34 V
  nominal, within each peripheral's 20 to 45 V range.
- [ ] **Connector pinout.** Confirm the 6-pin assignment (section 3) against the spec and the machine's
  harness before connecting anything.
- [ ] **RX mode-bit decode.** With a logic analyser against the real changer, confirm the decoded mode
  bit on every received byte: send known bytes with the mode bit set and read the mode-bit disagreement,
  ambiguous and orphan counters in `self_test` and the once-a-minute log line. Confirm the driver's
  DATA-then-parity-error event ordering (section 5). If it misbehaves, switch to the `mode = false`
  fallback and confirm the idle-gap termination alone still frames every block.
- [ ] **Idle-gap fallback.** Exercise the 1 ms idle-gap terminator with the mode-bit path forced to
  `mode = false` and confirm blocks still validate by checksum.
- [ ] **Changer masks.** The firmware assumes big-endian 16-bit masks with bit *i* = coin type *i* for
  routing, tube-full and COIN TYPE; confirm against the real changer. Confirm the status bytes 0x03,
  0x05, 0x09 and 0x0D map to `error` and 0x0B to busy plus a fresh SETUP as built.
- [ ] **Always-idle bit.** Confirm the cashless reader's always-idle bit (SETUP reply byte 33, mask
  0x02) against a real level 3 reader, and whether the Nayax reader offers always-idle at all.
- [ ] **A coin and a bill end to end.** Insert a coin and a bill; confirm `coin_accepted`,
  `bill_escrowed`, `bill_stacked`, state `escrow_cents`, and the edge's acks erasing the log.
- [ ] **A payout from each tube.** `payout` for each coin denomination; confirm the exact `paid_cents`,
  the `partial` result when a tube runs short, and that `payout_result.paid_cents` lowers
  `escrow_cents`. Confirm a level 3 payout over 255 units chunks correctly if reachable.
- [ ] **Cashless session.** A tap through `session_begun`, `vend_request`, `vend_approved`,
  `vend_result`, `session_ended{complete}`; deny, reader cancel, and the `edge_timeout_s` VEND FAILURE.
- [ ] **Flash window on silicon.** Scope a spare GPIO high around each `FlashWindow` (open to close)
  and around NVS calls against the MDB TX and RX lines; confirm no bus byte gap during a window other
  than the deliberate parked gap. After filling and acking a 256-entry log, read the `bus stats` log
  line (`max window`, `flash window overruns`).
- [ ] **Worst-case erase.** Confirm the worst NVS page switch or sector erase stays under the 400 ms bus
  hold with `AUTO_SUSPEND` on, and re-measure with it off. Confirm the boot log shows no flash-chip
  complaint with `AUTO_SUSPEND` (if the fitted flash chip lacks suspend/resume, drop that line; the
  window still holds).
- [ ] **NVS capacity.** Fill the log to 256 envelopes of near-worst-case size in the 128 KB partition;
  confirm the append never fails before log pressure engages at 192, and that an append failure is
  survivable.
- [ ] **Log pressure.** Stop acking and confirm acceptance inhibits at 192 pending with `device_fault`,
  `self_test` reports `event_log_full`, the LED goes white-blinking, and acceptance resumes at 128.
- [ ] **Power cut mid-session.** Cut power with a session open and a bill escrowed; confirm the
  validator returns the bill, the next boot has a new `boot_id`, unacked events replay first, and the
  board stays at `no_policy` until `set_policy`. Cut power during an NVS write and confirm the log
  recovers.
- [ ] **Boot sweep time.** Time the orphan-blob sweep at boot with a full log; confirm it does not
  delay the first `set_policy` ack.
- [ ] **Wi-Fi PHY calibration.** Wi-Fi init writes PHY calibration data before the bus task starts;
  confirm no MDB transaction is missed around Wi-Fi bring-up.
- [ ] **LED.** Confirm each pattern in section 8 on the real DevKitC (some clones differ in LED order).
- [ ] **Flash tool on hardware.** Run `scripts/flash_board.py mdb --provision` against a real board,
  including the RTS reset used to catch the boot banner, and `--verify-only`.
- [ ] **Conformance suite.** With `ICE_COLDER_BOARD_TESTS=1`, `ICE_COLDER_BOARD_BROKER` and
  `ICE_COLDER_BOARD_MACHINE_ID` set, run `uv run pytest tests/firmware/mdb`; the hardware tests were
  written against the contract and have not yet run against a board.

## 10. Open items

None changes the architecture; each affects a default or this document.

- The existing MDB interface make and model in the IM600XL (I/O list section 8), to confirm a clean
  swap to this board as bus master.
- The bus power source (section 2).
- The exact changer, validator and cashless reader models and the levels they negotiate.
- The connector pinout, confirmed at the machine (section 3).
- Whether the Nayax reader offers always-idle.
- Decimal places: only USD with the standard scaling is handled (the peripherals' decimals byte is
  ignored).
- What the edge does with a 1.0.0 board that lacks `cash_sale` (the contract does not say).

## 11. As built: deviations from the plan

Collected from the task reports; the full list is spec section 11.

- **Log full inhibits acceptance** (192 pending, release at 128) instead of "keep accepting at
  capacity", so money already taken is always recordable (section 7).
- **Flash quiet window** with a 400 ms bus hold; the ledger does all NVS work, console writes and the
  Wi-Fi driver storage are kept off the bus's critical path (sections 6, 7).
- **Line-idle read deadlines** plus one character time (section 5).
- **RX mode bit** decoded from per-byte parity-error events, `mode = false` fallback on ambiguous
  batches (section 5).
- **Ack timing.** `submit()` drives the bus synchronously and returns the real first ack; a command
  still queued is never acked `failed`.
- **`payout_result.paid_cents` lowers `escrow_cents`.** Without it, a payout of the full escrow would
  leave the board still owing it.
- **`enabled` is filtered to the peripheral's reported denominations**; amounts not a multiple of the
  scaling factor pay the floor and report `partial` with `detail: "scaling"`; level 3 payouts are
  chunked at 255 units.
- **Replay ordering** by epoch-stamped envelopes and a "replay owed" latch in comms.
- **Erase order.** The log store advances `head` first and erases blobs afterwards, so a power cut
  leaves harmless orphan blobs (swept at boot) rather than a counted entry with no blob.
- **Larger buffers** than planned: `pub_q` items are 1664 bytes (the state document is about 0.8 KB,
  1.6 KB with 16 tubes); the payouts blob buffer is 5120 bytes.
- **`Inhibit::DeviceFault` is produced only by log pressure.** A jammed, errored or offline device
  never inhibits the others; its status stays visible in `devices[]`.
- Recorded, not fixed: while a failing `set_policy` settles, devices are briefly enabled before the
  rollback; a changer payout timeout is only checked while a payout is active; a very busy bus can
  defer NVS work (events wait in `evt_q`, and log pressure keeps the log bounded).
