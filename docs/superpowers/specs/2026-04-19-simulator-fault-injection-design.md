# Simulator Fault Injection & Autonomous Behaviour Design

## Purpose

Expand the three ESP32 simulators (ice maker, vending machine, MDB gateway) with:
- Autonomous fault generation at low random probability
- Manual fault injection via MQTT command
- Degraded hardware behaviour while a fault is active
- Structured alert publishing for dashboard/HA consumption
- Automatic recovery after a timed interval (simulating human intervention)
- Richer customer interaction patterns in the vending simulator

Faults model real mechanical breakdowns that would require a human to physically intervene. The simulators faithfully report what the hardware would report — all decisions about what to do (halt, restart, notify) remain with the VMC.

---

## Architecture

### Fault injection lives in the base class (`simulators/base.py`)

`ESP32Simulator` gains:

1. **`FaultDef` dataclass** — describes one registerable fault
2. **`register_fault()`** — called by subclasses in `__init__`
3. **`_fault_loop()`** — base class async task; rolls probability, fires activation/recovery
4. **`_publish_alert()`** — publishes structured alert to `vmc/{machine_id}/alert/{subsystem}`
5. **Inject command subscription** — `vmc/{machine_id}/cmd/sim/inject_fault`

Subclasses implement `on_activate` and `on_recover` as `async` methods and register them with `register_fault()`. The base class owns the timer, alert publishing, and MQTT wiring.

### Recovery time categories

| Category | Recovery window |
|----------|----------------|
| `short`  | 3–10 minutes   |
| `medium` | 10–20 minutes  |
| `long`   | 20–60 minutes  |

Recovery time is sampled uniformly from the window at fault activation time.

---

## Base Class Changes

### `FaultDef`

```python
@dataclass
class FaultDef:
    name: str
    category: Literal["short", "medium", "long"]
    probability: float          # chance per _fault_loop tick (every 30s)
    on_activate: Callable       # async method: activate degraded state
    on_recover: Callable        # async method: restore normal state
    message: str                # human-readable alert message
    severity: str = "warning"   # "warning" | "critical"
```

### `register_fault(fault_def: FaultDef)`

Appends to `self._fault_defs`. Initialises tracking state:
```python
self._fault_state[fault.name] = {"active": False, "recover_at": 0.0}
```

### `_fault_loop(client)`

Runs every 30 seconds. For each registered fault:
- If not active: roll `random.random() < fault.probability`; if triggered, call `on_activate(client)`, publish alert with `status="active"`, set `recover_at`
- If active: check `monotonic() >= recover_at`; if so, call `on_recover(client)`, publish alert with `status="cleared"`

Only one fault active per simulator at a time (second roll skipped if any fault already active), to avoid compounding degraded states that are hard to interpret.

### `_publish_alert(client, fault_def, status)`

Publishes to `vmc/{machine_id}/alert/{subsystem}` (retained=False):

```json
{
  "subsystem": "ice_maker",
  "fault": "compressor_overtemp",
  "status": "active",
  "message": "Compressor temp exceeded safe limit — service required",
  "severity": "warning",
  "recover_in_seconds": 1800
}
```

`recover_in_seconds` is included on `active` alerts; omitted on `cleared`.

### Inject command

Base class subscribes to `vmc/{machine_id}/cmd/sim/inject_fault` on connect. Payload:
```json
{"fault": "compressor_overtemp"}
```
Bypasses probability roll. If the named fault is already active, the command is ignored. If a different fault is already active, the inject is also ignored (one fault at a time).

---

## Ice Maker Faults

Registered in `IceMakerSimulator.__init__`. While any fault is active, the compressor cycle halts and a `halt` event is published to `vmc/{machine_id}/ice_maker/event`. On recovery, a `resume` event is published.

| Fault | Category | Probability/30s | Degraded behaviour |
|-------|----------|-----------------|--------------------|
| `compressor_overtemp` | long | 0.05% | `refrigerant_high` target → 95°C; compressor forced off and stays off |
| `low_refrigerant` | long | 0.03% | `refrigerant_low` target fixed at +10°C (no cooling); compressor cycles but ineffective |
| `water_inlet_blocked` | medium | 0.08% | `water_bath` target pinned at 20°C; `water_inlet` drift stops |
| `defrost_stuck` | short | 0.15% | `hot_gas_valve` target pinned at 95°C; `ice_dropped` events suppressed |

The existing out-of-bounds temp check in `tick()` provides a secondary signal to the VMC for all temperature faults.

---

## Vending Machine Faults

Registered in `VendingMachineSimulator.__init__`. Cabinet heater logic and sensor publishing continue during faults — only the specific broken hardware is affected.

| Fault | Category | Probability/30s | Degraded behaviour |
|-------|----------|-----------------|--------------------|
| `auger_jam` | medium | 0.1% | During ice dispense: auger activates but `bag_full_sensor` never fires; sequence times out after 90s; publishes `DispenserStatus(state="timeout")`; auger forced off |
| `bag_drop_solenoid_stuck` | medium | 0.08% | After `fill_complete`: solenoid fires but `bag_full_sensor` stays `True`; publishes `DispenserStatus(state="jam")`; dispense loop halts |
| `water_valve_stuck_open` | short | 0.12% | After water dispense: `water_valve_solenoid` stays `True`, `water_flow_sensor` stays `True`; flow total keeps incrementing until recovery |
| `ice_bin_empty` | medium | 0.06% | `bin_half_full` → `False`; customer arrivals continue but each ice dispense attempt publishes `DispenserStatus(state="bin_empty")` after `motor_active` |

Hardware state changes are published immediately via `_set_hw()` so the dashboard reflects them in real time.

---

## MDB Gateway Faults

Registered in `MDBGatewaySimulator.__init__`. The gateway continues watching VMC status during all faults; only affected device states and available payment methods change.

`PaymentStrategy.pick_method()` is updated to accept an `excluded_methods` set, narrowed by active faults.

| Fault | Category | Probability/30s | Degraded behaviour |
|-------|----------|-----------------|--------------------|
| `coin_acceptor_jammed` | short | 0.15% | `coin_acceptor` → `error`; `cash_coin` excluded from payment methods |
| `bill_validator_offline` | short | 0.12% | `bill_validator` → `offline`; `cash_bill` excluded |
| `card_reader_error` | short | 0.10% | `card_reader` → `error`; `card` and `nfc` excluded |
| `mdb_bus_reset` | medium | 0.04% | All devices → `offline`; payment loop pauses entirely; devices restore one at a time over 10–30s during recovery, each publishing `PaymentStatus` as they come back |

---

## Richer Customer Interactions (Vending Machine)

All implemented as probability rolls within `_customer_loop`. No new MQTT topics.

| Behaviour | Probability | What happens |
|-----------|-------------|--------------|
| Indecisive customer | 20% | Presses a second different button 5–15s after the first |
| Impatient customer | 15% | Walks away after 10–20s instead of the full 60s timeout |
| Repeat customer | 10% | After successful dispense, immediately buys a second item with no idle wait |
| Fault-aware departure | always | If `auger_jam` or `ice_bin_empty` active, idle wait before next customer shortened to 5–15s |

**Arrival rate variation:** idle wait multiplied by a time-of-day factor derived from `datetime.now().hour`. Peak traffic 11am–2pm and 5pm–8pm (factor ~0.5×, i.e. customers arrive twice as fast); slow overnight 2am–6am (factor ~2×).

---

## Observable on Dashboard / HA

- `vmc/{id}/alert/{subsystem}` — fault active/cleared events (new)
- `vmc/{id}/ice_maker/event` — existing; `halt` and `resume` events added
- `vmc/{id}/hardware/io/{device}` — existing; fault states visible in real time
- `vmc/{id}/payment/status` — existing; device error/offline states visible
- All sensor readings continue publishing during faults (degraded values are the signal)

---

## Testing

- Unit tests for `FaultDef` registration and `_fault_loop` probability logic (mock `random`)
- Unit tests for each ice maker fault: sensor targets override correctly on activate, restore on recover
- Unit tests for each vending fault: dispense sequences produce correct states under fault
- Unit tests for MDB fault: excluded methods narrowing; bus reset recovery sequence
- Unit tests for customer interaction variants (mock `random` to force each branch)
- Existing simulator tests must continue to pass unchanged

---

## Out of Scope

- VMC-side fault handling (what the VMC does when it receives an alert)
- Dashboard UI changes for displaying alerts
- Persisting fault history across restarts
- Multiple simultaneous faults
