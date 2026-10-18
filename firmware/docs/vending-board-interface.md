# Vending board interface

The electrical and operational interface between the vending ESP32 firmware (`firmware/vending/`)
and the IM600XL. Design: `docs/superpowers/specs/2026-10-17-vending-esp32-firmware-design.md`
(sections 4 and 8). Tags (IC1, W3, ...) are the tags in the
[I/O list](../../docs/hardware/im600xl-io-list.md). Every item marked **assumed** is a working
assumption until the Phase A survey confirms it; each links to the
[I/O list section 8](../../docs/hardware/im600xl-io-list.md#8-open-items-to-verify-at-the-machine)
open item that closes it.

## 1. Reference board and substitution requirements

Reference board: **KinCony KC868-A8** (ESP32-WROOM-32E). 8 relays through a PCF8574 at I2C `0x24`,
8 opto-isolated inputs through a PCF8574 at `0x22`, I2C on GPIO 4 (SDA) and 5 (SCL) at 100 kHz,
two analog inputs, LAN8720 Ethernet, free GPIOs 13/14/32/33. The relay stage is inverting (a low
expander pin energizes the coil). The pin map lives in `firmware/vending/boards/kc868-a8.h`; **every
entry there is a working assumption to verify against the KC868-A8 schematic before wiring the
machine**, and board revisions differ on some pins.

A substitute board must provide: seven relays rated for the coils, nine 24 V isolated inputs, one
pulse-capable direct GPIO, one 1-Wire GPIO, one direct GPIO for the watchdog kick, one analog input
for the master-enable readback, and I2C expanders (or direct GPIO) for the rest. A new board is a new
header beside `kc868-a8.h` plus a `kc868_io.cpp` equivalent; `lib/vendcore` does not change.

## 2. Channel map

The firmware declares **18 channels**: 9 binary inputs, 7 binary outputs, the `fill_pulses` counter
and the `ambient` temperature. Relays switch **24 VDC coil power only**, never a motor or mains load;
the IC1/IC2 contactors stay. Relay 8 is spare.

| Channel | I/O list tag | Board point | Wire colour |
|---|---|---|---|
| `agitator_motor` | IC1 | Relay 1 (expander bit 0) | |
| `auger_motor` | IC2 | Relay 2 (bit 1) | |
| `bag_fan` | IC3 | Relay 3 (bit 2) | |
| `trap_door_catch` | IC4 | Relay 4 (bit 3) | |
| `water_valve_solenoid` | W3 | Relay 5 (bit 4) | |
| `ro_production` | W5 | Relay 6 (bit 5) | |
| `heater_power` | E1 | Relay 7 (bit 6) | |
| `bag_full_sensor` | IC5 | Opto input 1 (bit 0) | |
| `button_ice` | C1 | Opto input 2 (bit 1) | |
| `button_water_1gal` | W1 | Opto input 3 (bit 2) | |
| `button_water_5gal` | W2 | Opto input 4 (bit 3) | |
| `ro_tank_full` | W6 | Opto input 5 (bit 4) | |
| `ro_tank_empty` | W7 | Opto input 6 (bit 5) | |
| `water_pressure_ok` | W8 | Opto input 7 (bit 6) | |
| `sump_high` | W9 | Opto input 8 (bit 7) | |
| `service_door` | C6 | GPIO 13, through an external opto input module (the A8 has only eight expander inputs) | |
| `fill_pulses` | W4 | GPIO 32, PCNT peripheral (must be a direct GPIO) | |
| `ambient` | E2 | GPIO 14, DS18B20 on 1-Wire (sensor type **assumed**) | |
| Watchdog kick | none (board-side) | GPIO 33 | |
| Master-enable readback | none (board-side) | GPIO 34 (ADC1 channel 6), threshold 1.5 V | |

The wire colour column is filled in during the Phase A survey, keyed to the tags.

Open items behind this table: coil voltage and type for IC1-IC4, W3, W5, E1 (24 V DC **assumed**;
I/O list section 8, first two items); IC5 sensor type (section 8, IC5 item); W4 pulses per gallon
(section 8, W4 item; the dispense profile carries the scale); E2 existence, location and type
(section 8, E2 item); E1 switch-on rule (section 8, E1 item; the firmware default is heater on at
3 C, off at 6 C).

## 3. Input polarity

KC868-A8 inputs are pulled-up dry contacts: a closed contact reads raw 0, an open contact or broken
wire reads raw 1. Polarity is set per input channel; `active-low` means raw 0 is the asserted
state. Defaults, in input channel order (`default_polarity()` in `kc868-a8.h`):

| Input | Tag | Default | Contact meaning |
|---|---|---|---|
| `bag_full_sensor` | IC5 | active-low | closed = bag full; open = not full (a fill then runs to `max_run_seconds` and ends `bin_empty`, a visible fault) |
| `button_ice` | C1 | active-low | normally open momentary; closed = pressed |
| `button_water_1gal` | W1 | active-low | normally open momentary; closed = pressed |
| `button_water_5gal` | W2 | active-low | normally open momentary; closed = pressed |
| `ro_tank_full` | W6 | active-high | wired NC: closed while not full; open = full (pump stops) |
| `ro_tank_empty` | W7 | active-high | wired NC: closed while water present; open = empty |
| `water_pressure_ok` | W8 | active-low | closed = pressure OK; open = not OK |
| `sump_high` | W9 | active-high | wired NC: closed while normal; open = high water |
| `service_door` | C6 | active-high | wired NC: closed while the door is closed; open = door open |

**Fail-safe wiring rule: every fault input must be wired normally-closed, so a broken wire reads as
the fault.** Each default is a survey item until verified at the machine: W6/W7 contact types and
normal states (I/O list section 8, W6/W7 item), IC5 type (IC5 item), W8 line and trip point (W8
item), W9 (W9 item), C6 (C6 item), and each input's broken-wire state (section 8, broken-wire item).

Set polarity from the console with nine characters of `0`/`1` in the order of the table above
(`1` = active-low), then reboot:

```
vend> set polarity 111100100
vend> reboot
```

That string is the default: `1` for IC5, C1, W1, W2 and W8, `0` for W6, W7, W9 and C6.

## 4. Master-enable circuit

The seven output relays' coil commons feed through one interposing **master-enable** relay, wired
through its normally-open contact in the 24 V coil supply, so with the master enable dropped no coil
can be energized whatever the expander does. Its coil is driven by a retriggerable monostable with a
**2 s window**: the firmware pulses GPIO 33 (50 us high) and each pulse restarts the window; no
pulse for 2 s and the master enable drops. The design requires a pulse at least every 500 ms; the
firmware pulses on every 10 ms sequencer tick while healthy, so any part used must accept pulses at
that rate (the 2 s window is the timing that matters to the safety case).

Parts:

- Watchdog timer IC (TPL5010 / MAX6369 class, 2 s timeout) **or** a DIN-rail off-delay timer with a
  pulse/retrigger input, set to 2 s.
- Interposing relay, coil driven by the watchdog/timer output; its NO contact in series with the
  24 V coil common, rated for the combined coil current of the seven output coils.
- A divider or follower from the master-enable coil node to GPIO 34 for the readback (1.5 V
  threshold, raw 12-bit count 1980 at 12 dB, uncalibrated; **verify against the schematic and on
  the bench**).

Health conditions for the kick, evaluated every tick: both expanders acknowledge on I2C, the 10 ms
loop deadline is met (a tick more than 20 ms after the previous one counts as a miss), and no
latching interlock (`io_failure`, `sump_high`, `service_door` with motion) is holding. A hang, reset,
brownout or deliberate stop drops every output within 2 s with no software involved. The ESP32 task
watchdog (5 s) is a second layer.

Stated plainly: because the kick is withheld while `sump_high` holds, **a sustained sump-high drops
the master-enable relay within 2 s, and if the external watchdog also resets the ESP32 the board
restarts repeatedly until the condition clears.**

## 5. Isolation and power

The 24 V field side and the 3.3 V logic side share no ground except through the optos. The board is
powered from its own 12 V supply, not from the 24 V control rail. Mains loads (contactors) are never
switched by a board relay directly.

## 6. Behaviour the wiring depends on

- **VMC liveness.** `CoreConfig::silence_ms` defaults to **0** (silence timer disabled). Liveness is
  the VMC's retained `online` document (its LWT flips it false), any inbound command marks the VMC
  seen, and the board's own broker loss does not (a vend in flight finishes on its own timers; new
  commands cannot arrive without a broker, and the retained `online` document corrects the state on
  reconnect). A non-zero `silence_ms` re-enables
  the spec's 30 s silence timer but requires the edge to publish `online` periodically, which it
  currently does not. **Open decision for the edge:** publish `online` on an interval, or keep the
  timer disabled.
- **Interlock trips.** On `vmc_offline` the outputs are dropped at trip onset and a running dispense
  ends `error` with detail `vmc_offline`; the RO loop and freeze protection then keep running
  autonomously. On `sump_high`, `io_failure`, `master_enable_dropped` and `service_door` (with motion)
  the outputs are held off every tick; RO is skipped on the first three; the heater is off while a
  latching interlock holds (`sump_high`, `io_failure`, `service_door` with motion), because its coil
  common runs through the master relay. The heater is duty-cycled: after 24 h of continuous cold it
  rests 5 min, then runs again (the 24 h software bound sits above that, so it never latches it off).
- **Bounded runtimes.** Each output has a software max-on bound: the dispenser profile's worst case
  plus 1 s, the RO max run (default 90 min) plus 1 s, and 24 h plus 60 s for the heater. The water valve's
  bound is its commanded duration plus 500 ms.
- **Refused dispense.** A dispense refused by an interlock inside the accept step produces both an
  `Outcome` event with `error` on `hardware/dispenser` and a `failed` ack, so the edge sees the
  failure twice.
- **Relay readback.** The relay expander state is re-transmitted at least every 250 ms and read back
  every tick; a mismatch marks `io_failure`. This only works if a pin written high reads back high
  through the board's driver stage: see the bench checklist.
- **Faults.** The board publishes no `alerts`; the edge derives every fault from `hardware/io` and
  `hardware/dispenser`.

## 7. Provisioning

Compile-time defaults come from the `VEND_DEFAULT_*` flags documented in
`firmware/vending/platformio.ini`; the USB serial console (115200 baud, prompt `vend> `) overrides
them and persists to NVS namespace `vend`. There is no captive portal and no OTA.

| Command | Effect |
|---|---|
| `set <key> <value>` | store a setting; takes effect after `reboot` |
| `show` | print every setting, secrets masked |
| `erase` | erase all stored settings |
| `reboot` | restart |

Keys: `wifi_ssid`, `wifi_pass`, `mqtt_host`, `mqtt_port` (default 1883), `mqtt_user`, `mqtt_pass`,
`machine_id`, `use_eth` (`0` or `1`), `polarity` (nine `0`/`1` characters, section 3).

## 8. Bench checklist (Phase E)

Tabletop panel, 24 V supply, board on its own 12 V supply, MQTT broker reachable, `machine_id` set.
Commands go to `vmc/<machine_id>/cmd/vending` as `{"command", "params", "request_id"}`; acks come
back on `vmc/<machine_id>/cmd/vending/ack`.

- [ ] **Relay readback through the driver stage.** With the board running, confirm it does not report
  `io_failure` (the `self_test` `watchdog_kick` check reads `kicking`, not `withheld`) while a relay
  is on and while all are off. A pin written high must read back high through the KC868 driver
  stage; if the driver loads the pin, every tick reports a mismatch and the watchdog is never
  kicked. Verifies the pin map's relay and readback **assumptions**.
- [ ] **A switch per input.** Toggle each of the nine inputs; confirm the matching `hardware/io`
  event, in the polarity of section 3. Each polarity is a survey item until verified at the machine
  (I/O list section 8: W6/W7, W8, W9, C6, IC5 and broken-wire items).
- [ ] **A lamp per relay.** Lamps on relays 1-7. Confirm each lamp tracks its output channel's
  `hardware/io` event, and relay 8 never energizes.
- [ ] **Flow meter on GPIO 32.** Telemetry flows only during a fill: run a `water_fill` dispense with
  `firmware/vending/test/fixtures/profile_water_1gal.json` and confirm `fill_pulses` on
  `telemetry/vending` reports the pulses (the calibration is I/O list section 8, W4 item). Also run
  fills totalling more than 32767 pulses, then one more fill, and confirm it still ends `complete`
  with a correct volume (the counter must accumulate past 16 bits).
- [ ] **Ambient.** DS18B20 on GPIO 14; confirm `ambient` telemetry and the heater lamp at the 3 C on /
  6 C off thresholds (E1 and E2 are **assumed**, I/O list section 8, E1 and E2 items).
- [ ] **Watchdog reset-hold test.** Hold the ESP32 in reset (EN low). The master-enable relay drops
  within **2 s** and all lamps go out. Release; the relay re-enables only once the kick resumes.
  `self_test` `master_enable` reads `enabled`.
- [ ] **Sump-high.** Open the sump input with a lamp lit; outputs drop, the kick stops, the master
  enable drops within 2 s, and it recovers when the input closes.
- [ ] **One `dispense`.** Send a `dispense` with `{"slot": 0, "mechanism": "bagged_ice", "profile":
  <contents of firmware/vending/test/fixtures/profile_ice_catch.json>}`. Confirm the `accepted` ack,
  the step reports on `hardware/dispenser`, one terminal report carrying the `request_id`, the catch
  lamp on before the agitator and off on every exit path. With the bag-full switch left open the
  fill ends `bin_empty` at 20 s.
- [ ] **One `water_valve 3`.** Send `water_valve` with `{"seconds": 3}`; confirm `accepted`, the valve
  lamp on for 3 s, then the `completed` ack.
- [ ] **LWT on power-off.** Remove board power; the broker publishes the `vending` heartbeat will
  (`uptime_seconds` of -1) after the broker's keep-alive grace (about 1.5 x 15 s).
- [ ] **Silence handling.** Stop the VMC (its `online` document goes false); confirm outputs drop and
  the RO loop and heater keep running (section 6).

Conformance without hardware: the opt-in suite in `tests/firmware/` runs against the simulator or a
live board; `CLAUDE.md` lists its environment variables.
