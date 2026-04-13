# ESP32 Simulator Design

## Purpose

Build three mock ESP32 processes that produce realistic MQTT traffic so the RPi VMC can be run and observed end-to-end without physical hardware. Each simulator is an independent process connecting to the same MQTT broker, mirroring real deployment where each ESP32 is a separate device.

## Architecture

```
simulators/
    __init__.py
    base.py              # ESP32Simulator base class
    ice_maker.py         # Ice maker temperature monitoring
    vending_machine.py   # Vending interface (buttons, dispense, sensors)
    mdb_gateway.py       # MDB payment bridge (coins, bills, card, NFC)
```

### Running

```
# Terminal 1: RPi VMC (unchanged)
uv run python main.py

# Terminal 2-4: one per simulator
uv run python -m simulators.ice_maker
uv run python -m simulators.vending_machine
uv run python -m simulators.mdb_gateway
```

### CLI Arguments (all simulators)

| Arg | Default | Description |
|-----|---------|-------------|
| `--broker` | `localhost` | MQTT broker hostname |
| `--port` | `1883` | MQTT broker port |
| `--machine-id` | `vmc-0000` | Machine ID (must match RPi config) |

## Base Class: `ESP32Simulator`

Handles common concerns so each simulator only implements its specific behavior.

**Responsibilities:**
- Connect to MQTT broker via `aiomqtt`
- Publish periodic heartbeats to `vmc/{machine_id}/heartbeat/{subsystem_name}` using `SubsystemHeartbeat` schema
- Track uptime since start
- Subscribe to command topics as needed
- Clean shutdown on Ctrl+C (cancel tasks, disconnect)
- Reconnect automatically on broker disconnect

**Interface:**
```python
class ESP32Simulator(ABC):
    def __init__(self, subsystem_name: str, broker: str, port: int, machine_id: str)

    @abstractmethod
    async def run_simulation(self, client: aiomqtt.Client) -> None:
        """Subclass implements its specific behavior here."""

    async def run(self) -> None:
        """Main entry: connect, start heartbeat + run_simulation, handle reconnect."""
```

Heartbeat interval: 10 seconds.

## Simulator 1: Ice Maker (`ice_maker.py`)

**Subsystem name:** `ice_maker`

**Behavior:** Models a simplified refrigeration cycle. Publishes 9 temperature readings every 5 seconds to `vmc/{machine_id}/sensors/temp/{location}` using the `SensorReading` schema.

**Compressor cycle:** 10 minutes on, 5 minutes off (repeating). All other temperatures respond to the compressor state with thermal lag.

**Sensor definitions:**

| Location key | Steady-state range (C) | Behavior |
|-------------|----------------------|----------|
| `water_inlet` | 10-20 | Stable with slow random drift |
| `water_bath` | 1-4 | Slowly cools toward freezing when compressor on, drifts up when off |
| `compressor` | 50-80 | Rises when running, cools during off-cycle |
| `exhaust_air` | 30-45 | Tracks compressor temperature with thermal lag |
| `ambient_air` | 20-30 | Stable ambient with slow random drift |
| `refrigerant_high` | 40-60 | Tracks compressor state |
| `refrigerant_low` | -15 to -5 | Drops when compressor runs, rises during off-cycle |
| `purge_water` | 2-8 | Tracks near water bath temperature |
| `hot_gas_valve` | 60-90 | Spikes during periodic defrost cycle |

**Temperature model:** Each sensor has a target value (determined by compressor state) and moves toward it with a configurable rate constant plus small random noise. This produces smooth, realistic curves without complex thermodynamic modeling.

**No fault injection in this phase.** Temperatures stay within normal operating ranges.

## Simulator 2: Vending Machine (`vending_machine.py`)

**Subsystem name:** `vending`

**Publishes to:**
- `vmc/{machine_id}/hardware/buttons` — `ButtonPress` schema
- `vmc/{machine_id}/hardware/dispenser` — `DispenserStatus` schema

**Subscribes to:**
- `vmc/{machine_id}/cmd/dispense` — `DispenseCommand` schema

**Autonomous cycle:**

1. **Idle wait** — 30-90 seconds random delay between simulated customers
2. **Button press** — randomly pick button 0, 1, or 2 and publish `ButtonPress`
3. **Wait for dispense command** — listen on `cmd/dispense` for the RPi to respond (timeout after 60 seconds; if no command arrives, return to idle — simulates customer walking away)
4. **Execute dispense sequence** based on slot:
   - **Ice (slots 0, 1):**
     - Publish `DispenserStatus(slot=N, state="motor_active")`
     - Wait 5-15 seconds (simulating bag fill time)
     - Publish `DispenserStatus(slot=N, state="fill_complete")`
     - Wait 1 second
     - Publish `DispenserStatus(slot=N, state="complete")`
   - **Water (slot 2):**
     - Publish `DispenserStatus(slot=2, state="solenoid_open")`
     - Publish pulse count updates every second for 5-10 seconds (logged, not a formal schema — future refinement)
     - Publish `DispenserStatus(slot=2, state="complete")`
5. **Return to step 1**

## Simulator 3: MDB Gateway (`mdb_gateway.py`)

**Subsystem name:** `mdb`

**Publishes to:**
- `vmc/{machine_id}/payment/credit` — `PaymentEvent` schema
- `vmc/{machine_id}/payment/status` — `PaymentStatus` schema

**Subscribes to:**
- `vmc/{machine_id}/status` — `VMCStatus` schema (watches VMC state)
- `vmc/{machine_id}/cmd/payment/enable` — `PaymentEnableCommand` schema

**Behavior:**

1. **Device readiness** — on startup and every 30 seconds, publish `PaymentStatus` for each device: `coin_acceptor: ready`, `bill_validator: ready`, `card_reader: ready`

2. **React to customer interaction** — when VMC status shows `interacting_with_user` with a `selected_product`:
   - Wait 2-5 seconds (customer reaching for wallet)
   - Randomly pick payment method: `cash_coin`, `cash_bill`, `card`, or `nfc`
   - Determine the product price from the selected product name (needs a local price lookup or just uses the credit_escrow deficit from status)
   - **Cash payments:** insert a random denomination that may not cover the price
     - Coin denominations: $0.25, $0.50, $1.00
     - Bill denominations: $1.00, $5.00, $10.00, $20.00
     - If insufficient, wait 3-8 seconds and insert more (repeat until covered or give up after 3 attempts)
   - **Card/NFC payments:** insert a random amount — sometimes exact, sometimes over, occasionally under (simulating partial auth)
   - Each insertion published as `PaymentEvent(amount=X, method=Y)`

3. **Go quiet** when VMC returns to `idle` — wait for next customer

**Randomness distribution for payment amounts:**
- 40% chance: underpay on first insertion (requires follow-up)
- 40% chance: exact or slight overpay
- 20% chance: significant overpay (e.g., $20 bill for a $3 item)

## End-to-End Flow

A typical simulated sale:

1. Ice maker publishes temperatures every 5 seconds (continuous)
2. All three simulators publish heartbeats every 10 seconds
3. Vending mock presses button 1 (ice 10lb bag)
4. RPi VMC transitions to `interacting_with_user`, publishes status
5. MDB mock sees status, waits 3 seconds, inserts $2.00 in coins (not enough)
6. RPi VMC publishes "insert $1.00 more"
7. MDB mock waits 5 seconds, inserts $1.00 bill
8. RPi VMC processes payment, sends `cmd/dispense` for slot 1
9. Vending mock receives command, runs ice dispense: motor_active -> fill_complete -> complete
10. RPi VMC completes transaction, returns to idle
11. Vending mock waits 30-90 seconds, cycle repeats

## Observable on Dashboard

- VMC state cycling: idle -> interacting -> dispensing -> idle
- Health panel: 3 subsystems (ice_maker, vending, mdb) all reporting OK
- 9 temperature readings updating every 5 seconds
- Logs showing full transaction flow with payment amounts and methods

## Testing

- Unit tests for base class (heartbeat scheduling, clean shutdown)
- Unit tests for ice maker temperature model (compressor on/off produces expected temp trends)
- Unit tests for vending machine dispense sequence (correct state progression)
- Unit tests for MDB gateway payment logic (amount randomization, method selection)
- Integration test: kill a simulator process, verify health monitor detects it

## Future Extensions (not in this phase)

- Fault injection: ice maker compressor failure, vending bag fill timeout, MDB device error
- CLI or web-based manual event triggers
- Configurable timing parameters via CLI args or config file
