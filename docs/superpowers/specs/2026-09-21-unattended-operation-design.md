# Unattended Operation — Design

**Date:** 2026-09-21
**Status:** Approved (brainstormed with owner)
**Roadmap:** Phase C, slice 2 (`ROADMAP.md` §3, §5, §7, §8) plus reliability
fixes surfaced by the 2026-09-21 project review.

## Goal

The VMC stops accepting money whenever it cannot deliver, tells the owner
exactly which condition is blocking a sale, survives its own restart without
losing track of a customer's credit, and never silently drops an owner alert.
Everything that is not required for that is left as a named stub with a clear
extension point.

## Why now

The review found that the machine accepts payment in every state:
`cmd/payment/enable` is defined but never published, a lost vending ESP32 only
sends an email, and a process restart mid-sale zeroes escrow with no record.
The notifier drops any second distinct fault raised within five minutes
because its cooldown is keyed on the alert source and every registry fault uses
the same source. The logs tab reads the wrong path on Linux. None of these are
visible in the test suite, which passes.

## Non-goals (later slices, left as stubs)

- Admin `restart`/`shutdown` remain log-only (`services/fsm_control.py`).
- Broker hardening (host port, password file) and dashboard CSRF/rate limits.
- SMS and Snapchat delivery in `services/notifier.py`.
- Per-product revenue tagging, cost fields, cash-box telemetry, exports.
- Automatic reconciliation with the payment gateway after restart; only the
  hook exists.
- Any input the vending ESP32 cannot report yet (bag, trap door, water,
  24 V). These are listed as "not instrumented" and pass until Phase B/D.

## Decisions

1. **Known inputs only.** Permissives are computed from signals that exist
   today. Future inputs are present in the table, marked `instrumented=False`,
   and permanently `PASS`. Flipping one to fail-closed is a one-line change
   when the firmware starts reporting it.
2. **Availability lives in its own module.** `services/availability.py` owns
   the truth table and the `payment/enable` publish. VMC and HealthMonitor
   feed it; neither computes availability itself.
3. **Hold and alert on restart.** A persisted open session raises `PAY-104`,
   keeps payment off, and waits for an admin to clear it. No automatic refund.
4. **Communication loss is a registry fault.** Staleness and LWT raise
   `COM-101`, `COM-102`, `COM-103`, `PAY-101` through the same `_raise_fault`
   path as every other fault, and auto-clear on recovery.
5. **Nothing blocking runs on the event loop.** SQLite writes go through a
   writer thread; file saves and log tailing run in a thread.

## 1. Availability — `services/availability.py` (new)

### Model

```python
class PermissiveState(str, Enum):
    PASS = "pass"
    FAIL = "fail"
    UNKNOWN = "unknown"

class Applies(str, Enum):
    ice = "ice"
    water = "water"
    both = "both"

@dataclass
class Permissive:
    name: str
    applies_to: Applies
    instrumented: bool
    state: PermissiveState
    detail: str = ""
```

Product kind: `Product.kind: Literal["ice", "water", "other"] = "other"` is
added to `config/config_model.py`. `"other"` is gated as `both`.

### Inputs (instrumented today)

| Name | Applies | Source | FAIL when |
|---|---|---|---|
| `mqtt_connected` | both | `MQTTClient` connection callback | disconnected |
| `vending_alive` | both | HealthMonitor liveness callback, subsystem `vending` | stale or LWT |
| `payment_alive` | both | liveness callback, subsystem `mdb` | stale or LWT |
| `payment_devices_ready` | both | `payment/status` per device | any device in `error` or `offline` |
| `ice_maker_alive` | ice | liveness callback, subsystem `ice_maker` | stale or LWT |
| `ice_available` | ice | `hardware/io/bin_half_full` and ICE-101 lockouts | bin reports empty or any ICE-101 active |
| `fsm_ok` | both | VMC state | state is `error` |
| `no_critical_fault` | both | VMC machine-scope faults | any machine fault with severity `critical` or `lockout` |
| `service_door_closed` | both | `hardware/io/service_door` | open |
| `transaction_certain` | both | session store at boot | PAY-104 active |

Initial state of every instrumented input is `UNKNOWN`, which is **not** a
pass. A subsystem that has never spoken keeps payment off until its first
heartbeat, matching ROADMAP §3.

### Inputs (not instrumented; permanent PASS)

`bag_present` (ice), `trap_door_closed` (ice), `control_power_ok` (both),
`water_pressure_ok` (water), `water_treatment_ok` (water), `no_leak` (water),
`water_valve_closed` (water). Each carries `detail="not instrumented"`.

### API

```python
class Availability:
    def __init__(self, products: list[Product]): ...
    def set_publisher(self, publish: Callable[[bool], Awaitable[None]]) -> None
    def set_event_recorder(self, recorder) -> None

    # inputs
    def set_mqtt_connected(self, connected: bool) -> None
    def set_subsystem_alive(self, subsystem: str, alive: bool) -> None
    def set_payment_device(self, device: str, state: str) -> None
    def set_fsm_state(self, state: str) -> None
    def set_machine_faults(self, faults: list[dict]) -> None   # VMC.active_faults()
    def set_hardware_io(self, device: str, state: bool) -> None
    def set_lockouts(self, lockouts: dict[str, FaultCode]) -> None
    def set_transaction_certain(self, certain: bool) -> None
    def set_products(self, products: list[Product]) -> None    # catalog edits

    # outputs
    def sale_available(self, kind: str) -> tuple[bool, list[str]]
    def product_sellable(self, product: Product) -> tuple[bool, list[str]]
    @property
    def payment_enabled(self) -> bool
    def table(self) -> list[dict]            # dashboard rows
    async def republish(self) -> None        # on MQTT connect / mdb alive
```

`payment_enabled` is true when at least one product in the catalog is
sellable: its kind's permissives all pass and it has no lockout. Every setter
recomputes; when `payment_enabled` changes, `Availability` awaits the
publisher with the new value and records `availability_changed` with
`metadata={"enabled": bool, "failing": [...]}`. `republish()` sends the
current value without requiring a change; VMC calls it from the MQTT connect
callback and when `mdb` transitions to alive, so a gateway that reboots
receives the current gate.

The publisher sends `cmd/payment/enable` as `PaymentEnableCommand(accept=...)`
at QoS 1, not retained (a command, not state; the republish rules cover
late joiners).

### VMC integration

- `VMC.set_availability(avail)`; `main.py` constructs one and wires it.
- `select_product` refuses a product whose `product_sellable` is false, names
  the first failing permissive to the customer, and logs it to the transaction
  log. Existing lockout refusal stays.
- `_push_active_faults`, `_publish_status`, `_handle_mqtt_hardware_io`,
  `_raise_fault`, `clear_fault` push their changes into `Availability`.
- A new `_handle_mqtt_payment_status` handler on `payment/status` feeds
  `set_payment_device`.
- Credit that arrives while payment is disabled is escrowed as today; the
  existing session-timeout refund path returns it. This is logged at warning
  level with the failing permissives.

### Dashboard

`HealthMonitor.get_summary()` gains `"availability": avail.table()` plus
`"payment_enabled"`. The health fragment renders a permissive table with three
visual states (pass, fail, not instrumented) and the status fragment shows
"Payment: enabled / disabled (reason)".

## 2. Communication faults — `services/health_monitor.py`, `controller/vmc.py`

`HealthMonitor.set_liveness_callback(cb: Callable[[str, bool], None])`. It is
called with `(subsystem, False)` from `mark_offline` and from `_check()` when a
subsystem first becomes stale, and with `(subsystem, True)` from
`record_heartbeat` when a subsystem that was stale or offline speaks again.
It fires once per transition.

VMC registers a handler that:

| Subsystem | Lost | Recovered |
|---|---|---|
| `vending` | `_raise_fault(COM_101)` | `clear_fault("COM-101", by="auto")` |
| `ice_maker` | `_raise_fault(COM_102)` | `clear_fault("COM-102", by="auto")` |
| `mdb` | `_raise_fault(PAY_101)` | `clear_fault("PAY-101", by="auto")` |

and forwards every transition to `Availability.set_subsystem_alive`.

MQTT connection changes go through a new `VMC.on_mqtt_connection(connected)`
which raises/clears `COM_103` and calls `Availability.set_mqtt_connected`;
`main.py` chains it after `health.update_mqtt_status` in the connection
callback. `clear_fault` already clears machine-scope faults by code string,
so auto-clear reuses it unchanged.

`FAULT_TABLE` severities: `COM_101` and `PAY_101` stay
`product_unavailable`; `COM_102` and `COM_103` stay `warning`. Availability
gates on the liveness inputs directly, so severity only affects alert level.

### MDB simulator

`simulators/mdb_gateway.py` subscribes to `cmd/payment/enable`, stores
`self._accepting`, and its payment strategy emits no `payment/credit` while
`_accepting` is false. It logs each transition. The vending simulator is
unchanged.

## 3. Session persistence — `services/session_store.py` (new)

```python
@dataclass
class SessionSnapshot:
    state: str                    # FSM state at save time
    credit_escrow: float
    selected_sku: str | None
    dispense_slot: int | None
    dispense_started_at: float | None   # time.time()
    pending_refund_request_id: str | None
    saved_at: float

class SessionStore:
    def __init__(self, path: Path = DATA_DIR / "session.json"): ...
    def save(self, snap: SessionSnapshot) -> None   # atomic: tmp, flush, fsync, replace
    def load(self) -> SessionSnapshot | None
    def clear(self) -> None
```

VMC calls `save` after `deposit_funds`, `select_product`, `on_dispense_product`
(before publishing the dispense command), `request_refund`, and `_fail_vend`;
it calls `clear` when `on_complete_transaction`, `on_cancel_sale`,
`_refund_confirmed`, or `_expire_session` leave the machine idle with zero
escrow. Saves run via `asyncio.to_thread` through `_fire_and_forget`.

### Boot check

In `VMC.__init__` after the FSM is built, `self._boot_session = store.load()`.
`attach_to_loop` then evaluates it: if `credit_escrow > 0` or
`state == "dispensing"` or `pending_refund_request_id` is set, the VMC:

1. raises `FaultCode.PAY_104` (new: severity `lockout`, scope `machine`,
   description "Transaction uncertain after VMC restart");
2. records `session_uncertain` with the snapshot as metadata;
3. calls `Availability.set_transaction_certain(False)`;
4. leaves the file in place until the fault is cleared.

`clear_fault("PAY-104")` calls `store.clear()` and
`set_transaction_certain(True)`. `VMC.reconcile_session()` exists, is
documented as the future hook for a gateway credit query, and returns `None`.

The FSM still boots `idle`; the snapshot is evidence for the owner, not
state to resume.

### Contract and docs

`FaultCode.PAY_104` is added to `contracts/vending_machine.py` and
`FAULT_TABLE`; `uv run python -m contracts.generate` refreshes
`docs/contracts/vending-machine/schemas/fault_code.schema.json`; ROADMAP §5
gains the row and §7 marks the restart bullet as implemented via PAY-104.

## 4. Reliability fixes

### Notifier cooldown

`Notifier.send` keys its cooldown on
`(alert.source, alert.code or alert.message, alert.product_sku)`. Two
different faults from `vmc` inside five minutes both reach the owner; the
same fault repeating is still suppressed.

### VMC presence on the broker

`MQTTClient.publish(topic_suffix, payload, qos=1, retain=False)`. The client
connects with `aiomqtt.Will(topic=f"{prefix}/online", payload='{"online": false}',
qos=1, retain=True)`, publishes `{"online": true}` retained on every connect,
and `VMC._publish_status` publishes `status` with `retain=True`. A new
`VMCOnline(BaseModel)` with `online: bool` and `timestamp` lives in
`services/mqtt_messages.py`.

### Event loop hygiene

- `EventRecorder`: `record()` puts the row on a `queue.Queue`; a daemon writer
  thread with one persistent connection drains it. `flush(timeout=5.0)`
  blocks until the queue is empty (tests and shutdown). `prune()` runs on the
  writer thread. Reads (`get_summary`, `get_historical_average`) stay
  synchronous and short-lived; the dashboard routes wrap them in
  `run_in_threadpool`.
- `InventoryManager.save_async()` wraps `_save` in `asyncio.to_thread`; VMC
  uses it from `_finish_dispensing`. `_save` stays for tests and admin routes.
- `web_interface/routes.py` `view_logs` calls `tail` via `run_in_threadpool`.
- `services/config_store.save_config`: write tmp, `flush()`, `os.fsync()`,
  then `os.replace`; on POSIX also fsync the directory.

### Paths

`services/paths.py`:

```python
LOG_DIR = Path("LOGS")
LOG_FILE = LOG_DIR / "vmc.log"
DATA_DIR = Path("data")
```

`main.setup_logging`, `routes.LOG_PATH`, `EventRecorder` default,
`SessionStore` default, and `inventory` default (`DATA_DIR / "inventory.json"`,
with a one-time move of a root-level `inventory.json` if present) import from
it. The logs-tab test writes a line and asserts it appears.

## 5. Deployment and owner screen

### arm64 image

`.github/workflows/ci.yml` adds `docker/setup-qemu-action@v3` before buildx and
sets `platforms: linux/amd64,linux/arm64`. Cache keys are unchanged.

### `/screen`

`GET /screen` renders `templates/screen.html`: `<meta name="viewport">`,
Tailwind, a single column that stacks on narrow widths, no buttons, no nav.
Tiles: machine state, payment enabled/disabled with the first failing
permissive, per-kind availability (ice / water), active faults, subsystem
liveness dots, money in and vends for the last 24 h. The page polls
`GET /screen/body` every 5 s. Both routes sit behind the existing Basic auth
dependency.

## Error handling

- `Availability` setters never raise; a malformed `payment/status` payload is
  logged and ignored by the VMC handler like other handlers.
- `SessionStore.load` treats an unreadable or unparsable file as an open
  session of unknown amount: it raises PAY-104 with
  `metadata={"error": ...}` rather than pretend the restart was clean.
- Writer-thread exceptions in `EventRecorder` are logged and the row is
  dropped; the thread keeps running.
- `Notifier` behaviour on delivery failure is unchanged (logged).

## Testing

New or extended test files:

- `tests/test_availability.py`: truth table per input, kind gating,
  `UNKNOWN` blocks, not-instrumented rows pass, publish only on change,
  `republish` sends unconditionally, `availability_changed` recorded.
- `tests/test_session_store.py`: round trip, atomic write (tmp absent after
  save), `clear`, corrupt file returns a sentinel.
- `tests/test_vmc_flows.py`: PAY-104 on boot with escrow; PAY-104 on boot
  with `dispensing`; clean boot raises nothing; `clear_fault("PAY-104")`
  clears the file and re-enables; `select_product` refused by permissive
  names the reason; session file written and cleared across a full sale.
- `tests/test_health_monitor.py`: liveness callback fires once per
  transition for stale, LWT, recovery.
- `tests/test_vmc_fsm.py`: COM-101/102/103 and PAY-101 raise and auto-clear.
- `tests/test_mqtt.py`: connect passes a `Will`; `publish(retain=True)` is
  forwarded; online published on connect.
- `tests/test_notifier.py` (new): two codes from one source both send; same
  code twice is suppressed.
- `tests/test_event_recorder.py`: `record` returns before write; `flush`
  makes the row visible.
- `tests/test_config_store.py`: `os.fsync` called before `os.replace`
  (monkeypatched).
- `tests/test_web_routes.py`: logs tab shows a written line; `/screen`
  returns 200 with the viewport meta and no `hx-post`.
- `tests/test_simulator_mdb.py`: `payment/enable false` stops credit events.
- `tests/test_integration_e2e.py`: with a broker, stopping the vending
  simulator's heartbeat withdraws `payment/enable`; resuming re-enables.
- `tests/test_contract_schemas.py` continues to guard the regenerated schema.

## Files touched

New: `services/availability.py`, `services/session_store.py`,
`services/paths.py`, `web_interface/templates/screen.html`,
`web_interface/templates/partials/screen_body.html`,
`tests/test_availability.py`, `tests/test_session_store.py`,
`tests/test_notifier.py`.

Modified: `controller/vmc.py`, `services/health_monitor.py`,
`services/mqtt_client.py`, `services/mqtt_messages.py`,
`services/notifier.py`, `services/event_recorder.py`,
`services/inventory_manager.py`, `services/config_store.py`,
`config/config_model.py`, `contracts/vending_machine.py`,
`docs/contracts/vending-machine/schemas/fault_code.schema.json`,
`simulators/mdb_gateway.py`, `main.py`, `web_interface/routes.py`,
`web_interface/templates/partials/health_fragment.html`,
`web_interface/templates/partials/status_fragment.html`,
`.github/workflows/ci.yml`, `ROADMAP.md`, `CLAUDE.md`, `README.md`.
