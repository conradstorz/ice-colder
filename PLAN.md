# ice-colder Architecture Plan

## Vision

A fault-tolerant, always-running vending machine controller. The Raspberry Pi is the brain — it manages business logic, owner communication, and the customer video display. ESP32 microcontrollers are the muscles — they handle MDB payment, sensors, motors, and buttons. MQTT is the shared nervous system. Home Assistant can observe everything but controls nothing.

## Current State

- Synchronous Python app with `while True: sleep(100)` main loop
- VMC FSM built on `transitions` library — works, but callbacks use blocking threading.Timer
- FastAPI web dashboard runs in a daemon thread — functional for inventory and status
- Hardware modules (`mdb_interface.py`, `ice_maker.py`) assume direct serial access — wrong model for ESP32/MQTT architecture
- No MQTT integration
- No owner notification pipeline
- No video/display control
- No health monitoring or watchdog

## Target Architecture

```
┌─────────────────────────────────────────────────────┐
│                   MQTT Broker                       │
│              (Mosquitto on RPi)                     │
└──────┬──────────┬──────────┬───────────┬────────────┘
       │          │          │           │
  ESP32-MDB  ESP32-Temps  ESP32-...  (future devices)
       │          │          │           │
       └──────────┴──────────┴───────────┘
                      │
       ┌──────────────┼──────────────┐
       │              │              │
   RPi VMC     Home Assistant   (future clients)
   (this code)   (monitoring)
```

### RPi VMC Internal Structure (asyncio)

All components run as tasks in a single asyncio event loop:

1. **MQTT Client** — subscribes to ESP32 topics, publishes VMC status
2. **FSM Core** — `transitions`-based state machine, reacts to MQTT events
3. **Web Dashboard** — FastAPI/uvicorn for owner monitoring
4. **Notification Service** — sends alerts to owner via email/SMS when thresholds are crossed
5. **Health Monitor** — periodic watchdog that checks subsystem liveness and reports
6. **Video Controller** — manages customer-facing display (instructions during sale, ads when idle)

## MQTT Topic Structure

All topics are namespaced under `vmc/{machine_id}/`.

### ESP32 → RPi (device reports)

```
vmc/{id}/payment/credit          # { "amount": 1.50, "method": "cash" }
vmc/{id}/payment/status          # { "state": "ready", "device": "card_reader" }
vmc/{id}/sensors/temp/{location} # { "value": -5.2, "unit": "C" }
vmc/{id}/hardware/buttons        # { "button": 2, "action": "pressed" }
vmc/{id}/hardware/dispenser      # { "state": "complete", "slot": 1 }
```

### RPi → ESP32 (commands)

```
vmc/{id}/cmd/dispense            # { "slot": 1 }
vmc/{id}/cmd/payment/enable      # { "accept": true }
vmc/{id}/cmd/display             # { "mode": "advertising" | "transaction" }
```

### RPi → World (status & alerts)

```
vmc/{id}/status                  # { "state": "idle", "uptime": 3600, ... }
vmc/{id}/alerts                  # { "level": "warning", "message": "..." }
```

## Migration Plan

### Phase 1: Async Foundation

Convert the app from synchronous + threading to asyncio.

- [x] Replace `main.py` blocking loop with `asyncio.run()` event loop
- [x] Run uvicorn inside the event loop (not in a separate thread)
- [x] Convert VMC timer-based scheduling to asyncio tasks
- [x] Remove `threading.Timer` usage from VMC
- [x] Verify all existing tests still pass

### Phase 2: MQTT Integration

Add MQTT as the communication backbone.

- [x] Add `aiomqtt` (async MQTT client) to dependencies
- [x] Create MQTT client service that connects to broker and manages subscriptions
- [x] Define message schemas for each topic (Pydantic models)
- [x] Add `MQTTConfig` and `machine_id` to `ConfigModel`
- [x] Wire MQTT client into `main.py` (runs alongside uvicorn via `asyncio.gather`)
- [x] Register VMC MQTT handlers for payment, buttons, dispenser, sensors, heartbeat
- [x] Publish VMC status to `vmc/{id}/status` on every state change
- [x] Write tests for MQTT schemas, topic matching, dispatch, and VMC wiring (32 tests)
- [ ] Replace `hardware/mdb_interface.py` with an MQTT topic handler
- [ ] Replace `hardware/ice_maker.py` with an MQTT topic handler
- [ ] Add MQTT connection status to health monitoring

### Phase 3: Health Monitoring & Owner Alerts

Make the machine report its own health.

- [x] Create health monitor task — periodic checks of all subsystems
- [x] Define health check interface: each subsystem reports last-seen timestamp
- [x] Implement owner notification via email gateway (already configured in config model)
- [x] Alert on: MQTT disconnect, ESP32 gone silent, temperature out of range, error state
- [x] Add health summary to web dashboard
- [x] Write tests for health monitor and notifier (20 tests)

### Phase 4: Cleanup Legacy Hardware Modules

Remove code that assumes direct hardware access from the RPi.

- [x] Remove or gut `hardware/mdb_interface.py` (replaced with MDB protocol reference stub)
- [x] Remove `hardware/ice_maker.py` (replaced by MQTT sensor handlers)
- [x] Remove `hardware/button_panel.py` (simulated — real buttons are on ESP32)
- [x] Remove `hardware/camera_monitor.py` (used OpenCV directly — not the target architecture)
- [x] Delete `hardware/tkinter_ui.py` (obsolete proof-of-concept)
- [x] Remove `pyserial` dependency (MDB serial now lives on ESP32)
- [x] Remove MDBInterface import/usage from VMC; update test fixtures
- [x] Keep `hardware/dispensing_fsm.py` and `hardware/mdb_payment_fsm.py` (model payment/dispense flows)

### Phase 5: Video Display & Customer UI

Control the customer-facing screen.

- [x] Design video/display controller service
- [x] Implement advertising mode (idle) and transaction mode (during sale)
- [x] Accept display commands from FSM state changes
- [x] Publish DisplayCommand to MQTT on every mode change (cmd/display topic)
- [x] Wire display controller into main.py and VMC
- [x] Write tests for display controller (16 tests, 108 total passing)

### Phase 6: ESP32 Simulators

Mock ESP32 processes for end-to-end testing without hardware.

- [x] Create ESP32Simulator base class (MQTT connect, heartbeat, CLI args, reconnect)
- [x] Create ice maker simulator (9 thermal sensors, compressor cycling)
- [x] Create vending machine simulator (button presses, ice/water dispense sequences)
- [x] Create MDB gateway simulator (reactive payment insertion, device status)
- [x] Add __main__.py entry points for each simulator
- [x] Write tests for all simulators (22 new tests, 136 total passing)

## Design Principles

1. **If it can fail, it will.** Every external connection (MQTT, serial, network) must handle disconnection and reconnection gracefully.
2. **The dashboard is the last thing to go down.** Even if MQTT is dead and every ESP32 is offline, the owner should be able to reach the web dashboard and see what's wrong.
3. **Log everything, alert selectively.** Verbose logs for debugging, but only notify the owner for actionable problems.
4. **ESP32s are replaceable.** The RPi should handle an ESP32 disappearing and reappearing without manual intervention.
5. **Home Assistant compatibility is a side effect, not a goal.** Design for MQTT. HA compatibility follows naturally.
