# Vending ESP32 Board — Firmware and Relay Module Design

> **Status:** Approved (brainstormed with owner), 2026-10-17. Defines the first real field device: the
> ESP32 vending board that replaces the IM600XL's Siemens PLC for the ice path, water path, customer
> buttons, RO fill and freeze protection, and the relay module that sits between it and the machine.
> Companion to the [IM600XL simulator design](2026-10-17-im600xl-simulator-design.md) (whose contract
> changes this firmware targets), the [I/O list](../../hardware/im600xl-io-list.md) (tags IC1–E2), the
> [vending-machine contract](../../contracts/vending-machine/CONTRACT.md) and the
> [layer-contracts spec](2026-10-13-layer-contracts-design.md) §2. `ROADMAP.md` §9 calls this Phase D.

## 1. Goal

Working firmware for the vending board, a hardware interface document for the relay module, and a
conformance suite that checks the real board and the simulator with the same tests. The board implements
the vending-machine contract at **2.0.0** as the IM600XL design defines it: named buttons as channel ids,
`ReleaseByCatch`, the RO/pressure/sump/door inputs on `hardware/io`, and no `sensors/water_flow`.

The 2.0.0 edge and simulator work is approved but not yet implemented (it is the other plan on this
branch). This firmware lands ahead of it; the conformance suite runs against the IM600XL simulator once
that plan ships (§7).

## 2. Decisions

| # | Question | Decision |
|---|---|---|
| D1 | Deliverable | Firmware plus a hardware interface doc, both under `firmware/`, plus an opt-in pytest conformance suite. |
| D2 | Firmware stack | **C++ on ESP-IDF via PlatformIO.** The contract's JSON command channel, 32-entry ack cache, two-phase acks, inline profiles and fail-safe on silence do not map onto ESPHome's entity model. Home Assistant compatibility is kept by publishing the same MQTT discovery the simulator does. |
| D3 | Relay module | **Integrated industrial ESP32 I/O board**, reference model KinCony KC868-A8. The interface doc names the model and the requirements any substitute must meet. |
| D4 | Hardware max-on-time backstop (layer contract §2) | **One master-enable relay on a hardware watchdog.** All seven coil commons feed through it; the sequencer must kick a retriggerable monostable every 500 ms or everything drops within 2 s. |
| D5 | Conformance | **Opt-in pytest suite** in `tests/firmware/`, driving any board at a broker named by env vars; skipped unless `ICE_COLDER_BOARD_TESTS=1`; CI runs it against the simulator. |
| D6 | Firmware architecture | **Two FreeRTOS tasks over a pure-C++ core.** A 10 ms sequencer task owns I/O, timing and the watchdog kick; a comms task owns the network. A stalled broker can never stall actuator timing. A single superloop was rejected because an MQTT reconnect blocks for seconds, which is exactly when the kick would stop and a vend in progress would abort. |
| D7 | Faults | The board publishes **no** `alerts`; the edge derives every fault from `hardware/io` and `hardware/dispenser` (IM600XL design §4.3). |
| D8 | Provisioning | Compile-time defaults overridable over the USB serial console, persisted in NVS. No captive portal, no OTA. |

## 3. Scope and layout

```
firmware/
  README.md                        updated: points at the project and the interface doc
  vending/                         PlatformIO project (ESP-IDF framework)
    platformio.ini                 envs: kc868-a8 (target), native (host tests)
    src/                           tasks, ports, main
    lib/vendcore/                  pure C++ sequencer library, no ESP-IDF dependency
    boards/kc868-a8.h              pin map and input polarity table for the reference board
    test/                          native unit tests for vendcore
  docs/vending-board-interface.md  relay module spec: pin map, wiring to IC1–E1, watchdog, isolation,
                                   bench checklist
tests/firmware/                    opt-in pytest conformance suite
```

CI gains a `firmware` job: `pio run -e kc868-a8` (build must pass) and `pio test -e native`, plus
`tests/firmware/` against the simulator with `ICE_COLDER_BOARD_TESTS=1`.

**Out of scope:** the MDB payment board, the I²C customer LCD (C3), the Scotsman serial tap, OTA, a keyed
service-mode input (the machine has none), and any change to the ice-maker-monitor contract.

## 4. Relay module and electrical interface

### 4.1 Reference board

KinCony KC868-A8: ESP32-WROOM-32E, 8 relays via a PCF8574 at I²C `0x24`, 8 opto-isolated inputs via a
PCF8574 at `0x22`, I²C on GPIO 4/5, two analog inputs, LAN8720 Ethernet, free GPIOs 13/14/32/33. Pin
assignments are verified against the official schematic during implementation; board revisions differ
on some pins.

### 4.2 Channel map

| Channel | Board point | Notes |
|---|---|---|
| `agitator_motor` (IC1), `auger_motor` (IC2), `bag_fan` (IC3), `trap_door_catch` (IC4), `water_valve_solenoid` (W3), `ro_production` (W5), `heater_power` (E1) | Relays 1–7 | Relays switch **24 VDC coil power only**, never a motor or mains load. The IC1/IC2 contactors stay. Relay 8 spare. |
| `bag_full_sensor` (IC5), `button_ice` (C1), `button_water_1gal` (W1), `button_water_5gal` (W2), `ro_tank_full` (W6), `ro_tank_empty` (W7), `water_pressure_ok` (W8), `sump_high` (W9) | Opto inputs 1–8 | 24 V inputs, polled every 10 ms, debounced 30 ms. Buttons wired normally open. Switch polarity is set per channel in the pin map so a broken wire reads as the faulted state (I/O list §8). |
| `service_door` (C6) | GPIO 13 through an external opto input module | The A8 has only eight expander inputs. |
| `fill_pulses` (W4) | GPIO 32, ESP32 PCNT peripheral | Must be a direct GPIO; a polled expander cannot count pulses. |
| `ambient` (E2) | GPIO 14, DS18B20 1-Wire | Sensor type assumed; E2 is still "verify". |
| Watchdog kick | GPIO 33 | §4.3 |
| Master-enable readback | Analog input A1 (GPIO 34), read as digital | Lets firmware see the backstop drop (§7) |

Any substitute board must provide: seven relays rated for the coils, nine 24 V isolated inputs, one
pulse-capable direct GPIO, one 1-Wire GPIO, one direct GPIO for the kick, and I²C expanders (or direct
GPIO) for the rest.

### 4.3 Hardware backstop

The seven output relays' coil commons feed through one interposing **master-enable** relay. Its coil is
driven by a retriggerable monostable with a 2 s window (a watchdog-timer IC of the TPL5010/MAX6369 class,
or a DIN-rail off-delay timer with a pulse input). The sequencer task pulses GPIO 33 every 500 ms **only
while** its own health checks pass: both expanders acknowledge on I²C, the 10 ms loop deadline is met,
and no interlock is latched. A firmware hang, reset, brownout or deliberate stop drops every output
within 2 s with no software involved. The ESP32 task watchdog is also armed as a second layer.

### 4.4 Isolation and power

The 24 V field side and the 3.3 V logic side share no ground except through the optos. The board is
powered from its own 12 V supply, not from the 24 V control rail. Mains loads (contactors) are never
switched by a board relay directly.

## 5. Firmware architecture

Two FreeRTOS tasks and one shared pure-C++ library, `lib/vendcore`. No dynamic allocation after boot.

### 5.1 Sequencer task (core 1, 10 ms tick, highest priority)

Owns `IoPort` (expander scan, PCNT read, DS18B20 every 10 s), the `Dispenser` state machine, `RoLoop`,
`FreezeProtect`, `Interlocks` and the watchdog kick. The core modules are pure C++ with `Clock`, `IoPort`
and `Publisher` as ports:

- **`Dispenser`** runs one `SlotProfile` exactly as `dispenser_schema.py` defines it, carried inline in
  every `dispense` command. Bagged ice: energize the catch → accessories with their lead → agitate →
  fill until `bag_full_sensor` or `max_run_seconds` → motors off → release (`ReleaseByCatch`:
  de-energize the catch, wait `drop_seconds`; `ReleaseTimed`: pulse the solenoid channel) → accessories
  lag. Water: open the valve, count pulses to `target_volume_ml × pulses_per_liter / 1000`; `no_flow`
  when the rate stays below `min_flow_ml_per_second` past `no_flow_grace_seconds`; `over_dispense` past
  `over_dispense_percent`; `timeout` at `max_fill_seconds`. Emits `agitate`/`fill`/`release` steps and
  exactly one terminal `DispenserOutcome`. Any outcome but `complete` de-energizes every output, the
  catch included, so a failed bag drops rather than hangs. `IceFillTimed`, `ReleaseBySensor`,
  `WaterFillTimed` are implemented for contract completeness; `ReleaseBySensor` reports `door_open` only
  if a door sensor channel is declared, which the IM600XL map does not.
- **`Interlocks`** are evaluated every tick before outputs are written. `service_door` open during
  motion, `sump_high`, or VMC silence (§6) forces all outputs off and ends a running dispense with
  `error` (detail names the interlock). A per-output software max-on-time (the profile's worst case
  plus 10 %) mirrors the hardware backstop.
- **`RoLoop`**: `ro_production` on while `ro_tank_full` is false; off at full; latched off after a
  firmware maximum run (default 5400 s, longer than the edge's `ro_max_run_seconds` default of 3600 s so
  the edge raises `WTR-107` first) until `ro_tank_full` reports true or the board reboots.
- **`FreezeProtect`**: `heater_power` hysteresis on `ambient`, defaults on below 3 °C and off above
  6 °C, placeholders until E1's rule is confirmed at the machine. With no DS18B20 present the heater
  stays off and `self_test` reports the missing sensor.

### 5.2 Comms task (core 0)

Wi-Fi or Ethernet; MQTT (esp-mqtt, QoS 1, LWT on `heartbeat/vending` with `uptime_seconds: -1`);
JSON via ArduinoJson with fixed capacity; the 32-entry `AckCache` keyed by `request_id`;
`SubsystemCommand` parsing and validation (contract bounds, `profile.mechanism` must equal `mechanism`,
every channel the profile names must be declared); retained `capabilities/vending` on connect;
heartbeat every 10 s; Home Assistant discovery for every channel; and the VMC `online` subscription that
feeds the silence timer.

### 5.3 Queues

`cmd_q` (comms → sequencer): `Dispense`, `WaterValve`, `ForceReport`, `VmcSeen`. `evt_q` (sequencer →
comms): `InputChanged`, `Step`, `Outcome`, `Telemetry`, `Health`. Both fixed-size. A full `evt_q` drops
`Telemetry` first and never an `Outcome`.

### 5.4 Ack phases

`dispense` and `water_valve` are acked `accepted` only after the sequencer reports the first actuator
actually on, never on receipt. `water_valve`'s `completed` ack arrives via `evt_q` and overwrites the
cached `accepted` ack, so a late duplicate replays the final outcome. `dispense` completes through the
terminal `hardware/dispenser` report, per the contract. `ping`, `self_test` and `force_report` ack
`completed` from the comms task directly. `self_test` checks: both expanders ack on I²C, DS18B20 present,
watchdog kick alive, VMC seen within 30 s, link RSSI or Ethernet up, dropped-message counter.

## 6. Wire behaviour

All topics under `vmc/<machine_id>/`.

| Topic | When | Payload |
|---|---|---|
| `capabilities/vending` (retained) | connect, `force_report` | `SubsystemCapabilities`: `contract_version: "2.0.0"`, `firmware` = semver plus git sha, `hardware_id` = MAC, `ip`, the 17 `ChannelDescriptor`s of the IM600XL design §5.1 with `direction` and `driven_by`, `commands: ["ping", "self_test", "force_report", "dispense", "water_valve"]` |
| `heartbeat/vending` | every 10 s; LWT `uptime_seconds: -1` | `SubsystemHeartbeat` |
| `hardware/buttons` | each debounced press | `ButtonPress {button: "<channel id>", action: "pressed"}` (the 2.0.0 string form) |
| `hardware/io/<channel>` | every change, every 60 s, and on `force_report` | `HardwareIO {device, state}` for the nine binary inputs and the seven outputs' commanded state |
| `hardware/dispenser` | each step, then the terminal outcome | `DispenserStatus {slot, state, request_id, detail}`; `detail` such as `"412 pulses"` or `"bag_full after 8.2 s"` |
| `telemetry/vending/fill_pulses` | every 1 s during a water fill, otherwise every 60 s | cumulative pulse count since boot |
| `sensors/temp/ambient` | every 10 s | `SensorReading` |
| `cmd/vending/ack` | per contract | `CommandAck`, two-phase per §5.4 |
| `homeassistant/<component>/<node>/<object>/config` (retained) | connect | discovery for every channel; device is this board |

Subscriptions: `cmd/vending`, `online`.

**Fail-safe on silence.** The VMC publishes a retained `online` document with an LWT. The board treats
`online: false`, or no `online`/command traffic for 30 s, as VMC silence: every output off, a running
dispense ends `error` (detail `"vmc_offline"`), and commands are acked `rejected` with that detail until
the VMC is seen again. Inputs keep reporting throughout.

**Persisted in NVS:** Wi-Fi/MQTT credentials, broker host, `machine_id`, the input polarity table.
`pulses_per_liter` is **not** stored; it arrives in each profile, so the board is stateless about
configuration.

## 7. Error handling

| Condition | Behaviour |
|---|---|
| Malformed JSON, or no usable `request_id` | Dropped with no ack (contract rule); counted and shown in `self_test` |
| Unknown command | `unsupported` |
| Params out of bounds, `mechanism` mismatch, or a profile naming an undeclared channel | `rejected` with a detail naming the field |
| Second `dispense` or `water_valve` while one runs | `rejected`, detail `"busy"` (the ack vocabulary has no `busy` status) |
| Broker lost mid-vend | The vend finishes on the sequencer's own timers; actuator safety never depends on the link. Steps and the outcome are queued and published on reconnect, idempotent on `request_id` |
| VMC silence | §6 |
| Interlock trip mid-step | Outputs off within one tick; outcome `error`, detail names the input |
| Expander I²C failure | Kick stops (hardware backstop drops outputs); outputs commanded off; `self_test` fails; board keeps reporting |
| Watchdog window missed for any reason | Master enable drops within 2 s; firmware sees it on the readback input (§4.2), commands every output off, and reports `error` (detail `"master_enable_dropped"`) for any running dispense |

## 8. Testing

**Host unit tests** (`pio test -e native`, against `lib/vendcore` with fake `Clock`, `IoPort`,
`Publisher`): step order and timing for `IceFillBySensor`, `IceFillTimed`, `ReleaseByCatch`,
`ReleaseTimed`, `WaterFillByVolume`, `WaterFillTimed` and accessory lead/lag; each terminal outcome
(`complete`, `bin_empty`, `timeout`, `no_flow`, `over_dispense`, `error`); the catch energized before
agitate and de-energized on every exit path; an interlock trip mid-step forces all outputs off; the
VMC-silence timer; `RoLoop` cutoff and latch; `FreezeProtect` hysteresis; `AckCache` replay and
overwrite by `completed`; command validation (bounds, mechanism mismatch, undeclared channel); debounce
and the polarity table.

**Conformance suite** (`tests/firmware/`, pytest; env `ICE_COLDER_BOARD_TESTS=1`,
`ICE_COLDER_BOARD_BROKER`, `ICE_COLDER_BOARD_MACHINE_ID`): the retained capabilities document validates
against `SubsystemCapabilities` and lists exactly the IM600XL design's channels; heartbeat cadence and
LWT on a forced disconnect; `ping`, `self_test`, `force_report`; a duplicate `request_id` replays the
cached ack; `water_valve` two-phase acks within the derived timeout; `dispense` acks `accepted`, then
step reports, then one terminal report carrying the `request_id`; `rejected` on bad params and on a
second concurrent dispense; `hardware/io` republish on `force_report`. The suite cannot drive sensors on
a real board, so bag-full and flow outcomes are asserted only when the board under test is the
simulator; a `requires_sim` marker skips them against hardware. CI runs the suite against the IM600XL
simulator once that plan's Task 10 lands; until then it runs against the current simulator with the
2.0.0-only checks marked `xfail(strict=True)`, so they flip to failures the moment they start passing and
the markers are removed.

**Bench** (Phase E; a checklist in the interface doc, not automated): a switch on every input, a lamp on
every relay, a pulse generator on GPIO 32, and the watchdog test: hold the ESP32 in reset and confirm
the master enable drops within 2 s.

## 9. Documentation updates

`firmware/README.md` (project and interface doc pointers, build and flash commands); `CLAUDE.md` (the
`firmware/` layout, the CI job, the conformance suite's env vars and skip rule); `ROADMAP.md` §9 Phase D
marked in progress with a link here; the I/O list §2 gains a "Board point" reference to the interface
doc.

## 10. Open items this design depends on

From the I/O list §8, each affects only the pin map or a default, never the architecture: coil voltages
(24 VDC assumed), IC5 sensor type, W6/W7/W9/C6 switch normal states (polarity table), W8 trip line,
E1's switch-on rule (hysteresis defaults), E2's existence and type (DS18B20 assumed), each input's
broken-wire state.
