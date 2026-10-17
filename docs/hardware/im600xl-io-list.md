# Kooler Ice IM600XL — Iteration 1 I/O List

> **Status:** Approved draft, 2026-10-16. Built from the owner's walk-through of the machine plus public
> manufacturer information. Details will be added as they are confirmed at the machine; anything marked
> **verify** is a working assumption, not a finding. Wiring, terminal numbers and harness/connector ids
> belong in the Phase A survey workbook ([ROADMAP §9](../../ROADMAP.md#9-phases)), keyed to the tags below.

This document lists every input and output the first iteration of the ice-colder control system will
interface to, when it **replaces the machine's Siemens PLC**. Our boards wire directly to the existing field
devices. The PLC and its HMI are removed.

## 1. The machine

| | |
|---|---|
| Machine | Kooler Ice **IM600XL** ice and water vending machine, reverse-osmosis (RO) water option |
| Ice maker | Scotsman **C1848 Prodigy** (self-contained, own bin control) |
| Bagging | "Touchless" auto-bagger: wicket bag feed, bag fan, magnetically latched trap door; 10 lb open-top bags |
| Water | 1-gallon and 5-gallon RO water vend station |
| Payment | MDB bus: bill validator, coin changer (with coin return), Nayax cashless reader |
| Factory controls (being replaced) | Siemens PLC, contactors and relays in a waterproof panel; Siemens HMI (service screen); 8-digit red 7-segment customer display; IceTalk remote-monitoring modem |
| Electrical | 220 V single phase, 60 A service |

Sources: [Kooler Ice IM600XL product page](https://koolerice.com/machine_model/im600xl-ice-vending-machine/),
[Kooler Ice components page](https://www.koolerice.com.au/ice-machine-features/). Kooler Ice support
(1-800-858-3025) may supply the electrical schematic and PLC I/O list; neither is published.

## 2. Discrete I/O

Columns:

- **Type** — electrical type; *assumed* entries are unconfirmed (see §8).
- **Iteration 1 owner** — what decides the point's state once the PLC is gone.
- **Channel id** — proposed id for the vending board's capabilities document (pattern `^[a-z0-9_]{1,64}$`,
  `ice_contracts.board.common.CHANNEL_ID_PATTERN`).
- **Simulator today** — the matching channel in `packages/sims/src/ice_sims/vending_machine.py`, if any.

### 2.1 Ice path (5 points)

| Tag | Signal | Dir | Type | Function | Iteration 1 owner | Channel id | Simulator today |
|---|---|---|---|---|---|---|---|
| IC1 | Bin agitator motor | Out | 24 V contactor coil (*assumed*) | Breaks up bridged ice in the bin during a vend | Vending firmware, ice vend sequence | `agitator_motor` | `agitator_motor` |
| IC2 | Dispense auger motor | Out | 24 V contactor coil (*assumed*) | Moves ice from the bin into the bag | Vending firmware, ice vend sequence | `auger_motor` | `auger_motor` |
| IC3 | Bag fan | Out | 24 V relay (*assumed*) | Opens the wicket-fed bag so it can fill | Vending firmware, ice vend sequence | `bag_fan` | `bag_fan` (described there as a cooling fan) |
| IC4 | Trap-door magnetic catch | Out | Electromagnet | **Energized holds the trap door shut** while the bag loads; de-energized releases it and the bag drops. The door closes itself afterwards. | Vending firmware, ice vend sequence | `trap_door_catch` | `bag_drop_solenoid` (opposite sense: energized = drop) |
| IC5 | Bag full | In | Proximity sensor (type *verify*) | Ends the fill | Vending firmware | `bag_full_sensor` | `bag_full_sensor` |

### 2.2 Water path (9 points)

| Tag | Signal | Dir | Type | Function | Iteration 1 owner | Channel id | Simulator today |
|---|---|---|---|---|---|---|---|
| W1 | 1-gallon button | In | Momentary switch | Customer selects 1 gal | Vending firmware → VMC | `button_water_1gal` | — (buttons arrive as button events) |
| W2 | 5-gallon button | In | Momentary switch | Customer selects 5 gal | Vending firmware → VMC | `button_water_5gal` | — |
| W3 | Dispense valve | Out | 24 V normally-closed solenoid | Delivers RO water to the customer | Vending firmware, water vend sequence | `water_valve_solenoid` | `water_valve_solenoid` |
| W4 | Flow meter | In | Pulse output | Meters dispensed volume (calibrated pulses per gallon) | Vending firmware | `fill_pulses` | `fill_pulses`, `water_flow`, `water_flow_sensor` |
| W5 | RO production | Out | Relay (*assumed*); one output drives the RO inlet solenoid **and** booster pump together | Refills the RO storage tank | Vending firmware, RO fill loop (§5) | `ro_production` | — |
| W6 | RO tank full | In | Level switch (*verify*) | Stops RO production | Vending firmware | `ro_tank_full` | — |
| W7 | RO tank empty | In | Level switch (*verify*) | No water to sell | Vending firmware → VMC availability | `ro_tank_empty` | — |
| W8 | Water pressure | In | Pressure switch (*verify* which line it measures) | Supply pressure OK | Vending firmware → VMC availability | `water_pressure_ok` | — |
| W9 | Sump pit | In | Level switch (*verify*) | Sump high / leak | Vending firmware → VMC availability | `sump_high` | — |

### 2.3 Customer front (2 points)

| Tag | Signal | Dir | Type | Function | Iteration 1 owner | Channel id | Simulator today |
|---|---|---|---|---|---|---|---|
| C1 | Buy-ice button | In | Momentary switch | Customer selects a 10 lb bag | Vending firmware → VMC | `button_ice` | — (buttons arrive as button events) |
| C6 | Service door switch | In | Door switch (automatic) | Service door open | Vending firmware → VMC availability | `service_door` | `door_sensor` (described there as the release-gate door) |

The tags follow the walk-through order. C2–C5 and C7 were reviewed and dropped: there is no button lamp
(C2), lighting runs itself (C4, §4), there are no validator bezel lights (C5), and the coin return (C7) is
part of the changer (§3). C3 is the display bus (§3).

### 2.4 Environment (1–2 points)

| Tag | Signal | Dir | Type | Function | Iteration 1 owner | Channel id | Simulator today |
|---|---|---|---|---|---|---|---|
| E1 | Heater power | Out | Relay (*assumed*) | Switches power to a small self-regulating space heater | Vending firmware (switch-on rule *verify*: what the PLC used to decide) | `heater_power` | `heater_relay` |
| E2 | Ambient temperature | In | Temperature sensor (*verify*: believed outside the machine; type unknown) | Freeze-protection input | Vending firmware → VMC | `ambient` | `cabinet` |

**Total: 17 confirmed points plus E2 to verify** — 7 outputs (IC1–IC4, W3, W5, E1) and 10 inputs (IC5, W1,
W2, W4, W6–W9, C1, C6), with E2 an eleventh input if confirmed.

## 3. Buses

| Bus | Devices | Iteration 1 role |
|---|---|---|
| **MDB** | Bill validator, coin changer (its coin-return lever is internal to the changer), Nayax cashless reader | Our MDB board becomes **bus master**, replacing the PLC's MDB interface. The devices themselves are unchanged. |
| **I²C** | Customer display (tag **C3**): a 20×4 (or similar) character LCD module | **Replaces** the factory 8-digit red 7-segment display, whose interface to the PLC is unknown and is not reused. |
| **Scotsman serial** (optional) | Scotsman C1848 Prodigy | A serial line of unknown type and protocol runs between the Scotsman and the PLC today. Tapping it is **optional** for iteration 1; nothing depends on it. |

## 4. Self-running equipment (no I/O)

These devices control themselves and need no connection to our system:

- **RO re-pressure pump** — pressurizes RO water from the storage tank to the dispense valve.
- **Sump pump** — runs on its own float, independent of the sump sensor (W9).
- **UV / ozone sanitizing** — automatic.
- **Canopy / sign LED lighting** — switched by its own photocell.
- **Wicket bag feed** — bags gravity-feed to the fan.
- **Trap door closing** — the door closes itself after the drop; only the catch (IC4) is controlled.
- **Scotsman bin control** — the ice maker stops on a full bin using its own ice-drop curtain switch.
- **Space heater thermostat** — the heater regulates itself once E1 powers it.

The separate **ice-maker monitor** board (owner-built; extra monitoring beyond what the Scotsman reports)
is its own project with its own contract ([ice-maker-monitor contract](../contracts/ice-maker-monitor/CONTRACT.md)),
and is not counted in this list.

The factory **Siemens HMI** is replaced by the operator tablet dashboard, and the **IceTalk** modem by our
own cloud uplink.

## 5. Control loops the firmware takes over from the PLC

| Loop | Inputs | Outputs | Rule |
|---|---|---|---|
| Ice vend | IC5 | IC1–IC4 | Energize the catch (IC4) and the bag fan (IC3), run the agitator (IC1) and auger (IC2) until bag full (IC5), stop the motors, then release the catch to drop the bag. A maximum fill time applies even if IC5 never trips. Step order and overlap are *verify* (record one factory vend); timings come from `dispensers.toml`. |
| Water vend | W1, W2, W4, W7–W9 | W3 | Open the valve until the target pulse count for the selected volume is reached; maximum dispense time applies. Volume comes from calibrated pulses per gallon, never time alone. |
| RO fill | W6, W7 | W5 | Run RO production whenever the tank is **not full**. This is new firmware logic (the PLC did it before), so it needs a maximum run time and a fault if the tank never reports full. |
| Freeze protection | E2 (if confirmed) | E1 | Switch the heater on below a threshold; rule to be confirmed against the PLC's behavior. |

## 6. Not instrumented, and what that costs

The machine has **no** sensor for these, and iteration 1 does not add any:

| Missing | Consequence |
|---|---|
| Bag present | A vend can run with no bag on the wicket. Ice would go into the chute loose. Neither the firmware nor the VMC can detect it. |
| Trap-door position (open/closed limits) | A door that fails to re-close cannot be detected. [ROADMAP §4](../../ROADMAP.md#4-product-sequences-and-interlocks)'s "trap door proven closed" interlock and the contract's bagged-ice `door_open` outcome cannot be reported from a real sensor. |
| Bin ice level | The edge cannot tell an empty bin from a slow fill. The only empty-bin signal is a fill that hits its maximum time without IC5 tripping. |
| 24 V control-power / mains monitor | `PWR-*` faults can only be inferred (for example, from a board reboot), not measured. |
| Motor current | The agitator/auger current channels the simulator models do not exist; a jammed motor shows only as a fill timeout. |

## 7. Divergences to resolve later

This document changes nothing else. These are the places the repo disagrees with the real machine:

- **Simulator and contract.** The vending simulator declares channels this machine lacks (`cabinet` as an
  in-cabinet sensor, `bin_half_full`, `door_sensor` as a release-gate sensor, `fan`, `vending_now_light`,
  `agitator_current`, `auger_current`) and lacks channels it has (W1, W2, W5–W9, C1, C6). Its
  `bag_drop_solenoid` has the opposite sense from IC4. ICE-101's auto-clear depends on `bin_half_full`,
  which has no real counterpart.
- **ROADMAP.** §1 and §3–§4 assume a bag-present sensor, trap-door limits, a leak input, water-treatment
  health and 24 V power monitoring. §10's open questions on the bag sensor, trap door, and leak/pressure/door
  inputs are now answered by this document.
- **Customer screen.** [The customer-screen contract](../superpowers/specs/2026-10-14-customer-screen-contract-design.md)
  (D1) makes a browser kiosk the first renderer. The real machine's screen will be the I²C character LCD
  (C3) — the "dedicated display board" that spec defers.

## 8. Open items to verify at the machine

- [ ] Coil voltage and type for IC1, IC2, IC3, IC4, W3, W5, E1 (24 V DC assumed throughout).
- [ ] IC4 catch: coil voltage, holding current, and confirm de-energized = released (fail-safe drop).
- [ ] IC5 proximity sensor type (inductive/capacitive/photo, NPN/PNP, supply voltage).
- [ ] W4 flow meter: pulses per gallon, measured on the installed plumbing.
- [ ] W6/W7: switch type and normal state (NO/NC) for tank full and tank empty.
- [ ] W8: which line the pressure switch measures (city inlet, RO feed, or dispense side) and its trip point.
- [ ] W9: sump sensor type and normal state.
- [ ] C6: switch type and normal state.
- [ ] E1: what the PLC used to decide when to power the heater.
- [ ] E2: whether a temperature sensor exists, where it is, and its type.
- [ ] Anything else the PLC switches or reads that is not listed here (none known).
- [ ] Each input's broken-wire state (what an open circuit reads as).
- [ ] Scotsman serial line: physical layer and protocol, if the optional tap is pursued.
- [ ] The MDB interface the PLC uses today (make/model), to confirm a clean swap to our bus master.
