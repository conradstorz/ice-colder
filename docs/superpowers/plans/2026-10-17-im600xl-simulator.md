# IM600XL Simulator Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make the vending simulator, the vending board contract and the edge match the real Kooler Ice IM600XL I/O list.

**Architecture:** Contract first (new release variant, fault codes, named buttons, 2.0.0), then the edge learns to run without a bin sensor and to derive water faults from `hardware/io`, then the simulator is rebuilt on the IM600XL channel set, then generated references and docs follow. Each task leaves `uv run pytest` green.

**Tech Stack:** Python 3.12, Pydantic v2, asyncio, aiomqtt, FastAPI/Jinja/HTMX, pytest, uv.

**Spec:** [`docs/superpowers/specs/2026-10-17-im600xl-simulator-design.md`](../specs/2026-10-17-im600xl-simulator-design.md). Section references (§) are to the spec. Hardware tags (IC4, W7…) are from [`docs/hardware/im600xl-io-list.md`](../../hardware/im600xl-io-list.md).

**Plan style:** each task gives files, interfaces and the tests to write. Implementers write the code bodies, matching surrounding style.

## Global Constraints

- Vending-machine `CONTRACT_VERSION` goes **1.0.0 → 2.0.0** (Task 2). Ice-maker-monitor and edge-cloud contracts are untouched.
- `PAYMENT_BLOCKING_FAULTS` stays **seven** codes; `WTR-106` and `WTR-107` are not members.
- Channel ids match `CHANNEL_ID_PATTERN = r"^[a-z0-9_]{1,64}$"`.
- New config fields: `physical.ice_retry_cooldown_seconds: float = 1800` (60–86400), `physical.ro_max_run_seconds: float = 3600` (60–86400).
- New timer labels: `ice_retry_cooldown`, `ro_overrun` (fired in tests with `runner.fire(label)` on `FakeTaskRunner`). No wall-clock waits in tests.
- Tests and routes never touch `_private` attributes of `vmc`/`machine`; new test files have a private-access budget of 0 (`tests/meta/test_no_private_access.py`). `tests/meta/test_vmc_complexity.py` budgets for `vmc.py` must not be exceeded — keep new logic out of `vmc.py` where possible.
- Import-layer rules hold: `uv run lint-imports` and `uv run python scripts/check_package_isolation.py` pass after every task.
- Run per task: `uv run pytest` (whole suite), `ruff check --fix .`, `ruff format .`.
- Regenerate schemas after any contract change: `uv run python -m ice_contracts.generate`. Regenerate the dispenser example after any schema/sample change: `uv run python scripts/gen_dispensers_example.py`.
- Commit trailer on every commit: `Assisted-by: <model> (Claude Code)`.

## File map

| File | Change |
|---|---|
| `packages/contracts/src/ice_contracts/board/dispenser_schema.py` | `ReleaseByCatch` |
| `packages/contracts/src/ice_contracts/board/vending_machine.py` | `WTR-106`, `WTR-107`, `OUTCOME_FAULTS`, version 2.0.0 |
| `packages/contracts/src/ice_contracts/board/mqtt_messages.py` | `ButtonPress.button: str` |
| `docs/contracts/vending-machine/CONTRACT.md` + `schemas/` | 2.0.0 section, regenerated |
| `packages/edge/src/ice_edge/vend/dispensers.py`, `dispensers_doc.py` | catch channel in cross-checks; IM600XL sample |
| `packages/common/src/ice_common/config_model.py` | `Product.button`, two `physical` fields |
| `packages/edge/src/ice_edge/sale/vmc.py`, `sale_journal.py` | button lookup, select by product |
| `packages/edge/src/ice_edge/web/routes/products/catalog.py`, `packages/ui/src/ice_ui/templates/product_form.html` (and `product_catalog.html` if it renders the form) | Button field |
| `packages/edge/src/ice_edge/faults/availability.py` | bin-instrumented flag, three rows |
| `packages/edge/src/ice_edge/faults/fault_service.py` | `ice_retry_cooldown` |
| `packages/edge/src/ice_edge/faults/input_faults.py` (new) | `InputFaults`: `hardware/io` → WTR-104/105/106/107 |
| `packages/edge/src/ice_edge/hal/mqtt_inbound.py`, `packages/edge/src/ice_edge/app/machine.py` | wiring |
| `packages/sims/src/ice_sims/vending_machine.py` | IM600XL channels and behaviour |
| `dispensers.example.toml`, `config.example.json`, `tests/dispenser_fixtures.py` | IM600XL samples |
| `CLAUDE.md`, `docs/hardware/im600xl-io-list.md`, `ROADMAP.md` | docs |

---

### Task 1: `ReleaseByCatch` and water fault codes (contract, additive)

**Files:**
- Modify: `packages/contracts/src/ice_contracts/board/dispenser_schema.py` (release union near l.245; `drive_channels`, `sense_channels`, `worst_case_seconds` helpers)
- Modify: `packages/contracts/src/ice_contracts/board/vending_machine.py` (`FaultCode`, `FAULT_TABLE`, `OUTCOME_FAULTS` l.346-357)
- Modify: `packages/edge/src/ice_edge/vend/dispensers.py` only if its drive/sense/budget checks do not already go through the schema helpers
- Regenerate: `docs/contracts/vending-machine/schemas/*.json`, `dispensers.example.toml` (only if the generator's output changes from the new variant's documentation)
- Test: `tests/contracts/test_dispenser_schema.py`, `tests/contracts/test_vending_machine_contract.py` (or the existing fault-table test file), `tests/edge/vend/test_dispensers_validation.py`

**Interfaces:**
- Produces: `ReleaseByCatch(proof: Literal["catch"], catch_channel: ChannelId, drop_seconds: float  # 0.5–30, unit "s")`; `ReleaseStep = Annotated[ReleaseBySensor | ReleaseTimed | ReleaseByCatch, Field(discriminator="proof")]`. `drive_channels(profile)` includes `catch_channel`; `worst_case_seconds(profile)` adds `drop_seconds`.
- Produces: `FaultCode.WTR_106 = "WTR-106"` (`product_unavailable`, `Scope.product`, "RO storage tank empty"), `FaultCode.WTR_107 = "WTR-107"` (`warning`, `Scope.machine`, "RO production overran its maximum run time"). `OUTCOME_FAULTS[("water_fill", DispenserOutcome.bin_empty)] == FaultCode.WTR_106`.
- Docstring of `ReleaseByCatch` states §3.2's MUSTs (energize before agitate, hold through fill, de-energize to drop, leave off between vends, de-energize on fault).

- [ ] **Step 1: Write failing tests**
  - `ReleaseByCatch` parses from a `[slot.N.release]` table with `proof = "catch"`; rejects `drop_seconds` 0.4 and 30.1; rejects an extra field (strict model).
  - A bagged-ice profile with `ReleaseByCatch` lists `catch_channel` in `drive_channels`, and `worst_case_seconds` grows by exactly `drop_seconds` versus the same profile with `drop_seconds` reduced by 1.
  - `validate_document` reports a drive/sense conflict when `catch_channel` equals the fill `sensor_channel`.
  - With capabilities declaring `trap_door_catch` as an `input`, `validate_document` reports a direction error for it.
  - `FAULT_TABLE` rows for WTR-106/WTR-107 have the severities/scopes above; neither is in `PAYMENT_BLOCKING_FAULTS`; `len(PAYMENT_BLOCKING_FAULTS) == 7`; water `bin_empty` maps to WTR-106 and bagged-ice `bin_empty` still maps to ICE-101.
- [ ] **Step 2: Run them; expect failures** — `uv run pytest tests/contracts tests/edge/vend -q`
- [ ] **Step 3: Implement** the variant, helper updates, codes and mapping. Update any existing test asserting water `bin_empty → ICE-101`.
- [ ] **Step 4: Regenerate** schemas and the dispenser example; run the full suite — all pass (schema drift tests included).
- [ ] **Step 5: Commit** — `feat(contracts): ReleaseByCatch release variant and WTR-106/WTR-107`

---

### Task 2: Named buttons end to end (contract 2.0.0)

**Files:**
- Modify: `packages/contracts/src/ice_contracts/board/mqtt_messages.py:66-71` (`ButtonPress`)
- Modify: `packages/contracts/src/ice_contracts/board/vending_machine.py` (`CONTRACT_VERSION = "2.0.0"`)
- Modify: `docs/contracts/vending-machine/CONTRACT.md` (new "Semantics fixed in 2.0.0 (breaking)" section: §3.1–3.3 of the spec)
- Modify: `packages/common/src/ice_common/config_model.py` (`Product.button`; `PhysicalDetails` unique-button validator beside `_check_unique_slots` l.176)
- Modify: `packages/edge/src/ice_edge/sale/vmc.py` (`on_button_press` l.272-276; `select_product` l.876-891; `begin_test_sale` l.819-820)
- Modify: `packages/edge/src/ice_edge/sale/sale_journal.py` (replace `selection_refused_bad_index` with `selection_refused_unknown_button`)
- Modify: `packages/sims/src/ice_sims/vending_machine.py` `_customer_loop` (l.1246-1297): press a random non-null `product.button` from `self.config.physical.products`; skip the press when none is set
- Modify: `config.example.json` products: `SAMPLE-ICE` (slot 0) → `"button_ice"`, `SAMPLE-WATER-SM` (slot 1, 1 gal) → `"button_water_1gal"`, `SAMPLE-WATER-LG` (slot 2, 5 gal) → `"button_water_5gal"`
- Test: `tests/contracts/` (ButtonPress), `tests/common/test_config_model.py` (or the existing config test), `tests/edge/sale/test_vmc_flows.py` (and every test publishing `ButtonPress(button=<int>)` — find with `grep -rn "ButtonPress\|hardware/buttons" tests`), `tests/sims/test_simulator_vending.py`

**Interfaces:**
- Produces: `ButtonPress(button: str  # CHANNEL_ID_PATTERN, action: str = "pressed", timestamp: datetime)`.
- Produces: `Product.button: Optional[str] = None` (pattern-validated; `None` = no physical button). `PhysicalDetails` raises `ValueError` naming duplicate button ids.
- Produces: `VMC.select_product(product: Product) -> None` (replaces the index form; same refusal checks). `VMC.on_button_press(data)` finds `p for p in self.products if p.button == press.button`; unknown → `journal.selection_refused_unknown_button(button: str)`, no exception, no state change.
- Produces: `SaleJournal.selection_refused_unknown_button(button: str) -> None`; `SaleJournal.product_selected(product, ...)` no longer receives an index (update its signature and log line to name the sku).
- `find_product(sku)` stays public (still used elsewhere); `begin_test_sale` calls `select_product(product)`.

- [ ] **Step 1: Write failing tests**
  - `ButtonPress(button=0)` and `ButtonPress(button="Bad-Id")` raise `ValidationError`; `ButtonPress(button="button_ice")` round-trips through JSON.
  - Config: two products with `button="button_ice"` fail to load with a message naming `button_ice`; two products with `button=None` load.
  - VMC: a `hardware/buttons` payload `{"button": "button_water_5gal"}` selects the product whose `button` is that id; an unknown id leaves state `idle`/unchanged and logs the refusal; a locked-out product's button is refused as before.
  - Test sale still selects its product (existing `run_test_sale` tests pass unmodified apart from index removal).
  - Simulator: with a config whose products carry buttons, one customer-loop press publishes a `ButtonPress` whose `button` is one of those ids; with no buttons configured, nothing is published.
  - `CONTRACT_VERSION == "2.0.0"`.
- [ ] **Step 2: Run; expect failures.**
- [ ] **Step 3: Implement.** Convert every existing test that sends an integer button to a named one (give the fixture products buttons). Check `tests/meta/test_vmc_complexity.py` still passes; if `vmc.py` budgets would be exceeded, move the lookup into a small pure helper (e.g. `ice_edge.sale.buttons.product_for_button(products, button) -> Product | None`).
- [ ] **Step 4: Regenerate schemas; full suite green.**
- [ ] **Step 5: Commit** — `feat!: named customer buttons (vending contract 2.0.0)`

---

### Task 3: Button field on the Products catalog form

**Files:**
- Modify: `packages/edge/src/ice_edge/web/routes/products/catalog.py` (`create_product` l.60, `update_catalog` l.160, `copy_product_form` l.201)
- Modify: `packages/ui/src/ice_ui/templates/product_form.html` (and `product_catalog.html` if the field list lives there)
- Rebuild if new classes are used: `packages/ui/src/ice_ui/static/app.css` (Tailwind command in CLAUDE.md)
- Test: `tests/edge/web/test_routes_products*.py` (the existing catalog route tests)

**Interfaces:**
- Consumes: `Product.button` and the duplicate validator from Task 2.
- Form field `button` (text, optional; blank → `None`). A value failing the channel-id pattern or duplicating another product's button re-renders the form with an error and status 400, leaving the stored product unchanged. Copy clears `button` (a copy must not share the original's button).

- [ ] **Step 1: Write failing tests** — create with `button=button_ice` stores it; update to blank stores `None`; duplicate → 400 with the error text and unchanged config; invalid id `"Button Ice"` → 400; copy form renders with an empty button field; GET catalog form shows the current value.
- [ ] **Step 2: Run; expect failures.**
- [ ] **Step 3: Implement.**
- [ ] **Step 4: Full suite green** (including `tests/ui/test_static_css.py`).
- [ ] **Step 5: Commit** — `feat(web): edit a product's customer button`

---

### Task 4: Availability without a bin sensor, plus RO/pressure/sump rows

**Files:**
- Modify: `packages/edge/src/ice_edge/faults/availability.py` (rows l.131-165, `set_hardware_io` l.232, `_refresh_ice_available` l.243)
- Modify: `packages/edge/src/ice_edge/app/machine.py` (~l.173: `on_capabilities_validated` calls both the gate and availability)
- Test: `tests/edge/faults/test_availability.py`, `tests/edge/hal/test_mqtt_inbound.py`

**Interfaces:**
- Produces: `Availability.set_bin_instrumented(instrumented: bool) -> None` and read-only property `Availability.bin_instrumented -> bool` (default `True`, preserving today's behaviour until a vending capabilities doc says otherwise). When `False`, the `ice_available` row reads not-instrumented/PASS with detail `"no bin sensor"`; `bin_half_full` reports are then ignored for that row.
- Produces: `Availability.on_vending_capabilities(subsystem: str, caps: SubsystemCapabilities) -> None` — for `subsystem == "vending"`, calls `set_bin_instrumented(any(c.id == "bin_half_full" for c in caps.channels))` (use the actual `ChannelDescriptor` id attribute name).
- Produces rows: new `ro_tank_ok` (`Applies.water`, `Gate.fulfillment`, starts not-instrumented); `water_pressure_ok` and `no_leak` keep their names/applies/gates but become instrumented on their first `hardware/io` report. `set_hardware_io`: `ro_tank_empty` → `ro_tank_ok` fails while true ("RO tank empty"); `water_pressure_ok` → row fails while false ("low water pressure"); `sump_high` → `no_leak` fails while true ("sump high").
- `Machine` wires `on_capabilities_validated` to a closure calling `self._gate.on_vending_capabilities(...)` then `availability.on_vending_capabilities(...)` when availability is attached (`self._outputs.availability`).

- [ ] **Step 1: Write failing tests**
  - Fresh `Availability`: `ice_available` still UNKNOWN (today's behaviour).
  - After `on_vending_capabilities("vending", caps_without_bin)`: `sale_available("ice")` passes with all other rows passing, and the row detail is `"no bin sensor"`; a later `set_hardware_io("bin_half_full", False)` does not fail it.
  - With caps declaring `bin_half_full`: behaviour identical to today (false → fails).
  - `ro_tank_empty=True` fails water and not ice and does not change `payment_enabled()`; `water_pressure_ok=False` likewise; `sump_high=True` fails water and makes `payment_enabled()` false (safety); each recovers on the opposite state.
  - Before any report, the three rows are not-instrumented and pass.
  - Inbound: a validated `capabilities/vending` message reaches both the dispenser gate and availability (machine-level test).
- [ ] **Step 2: Run; expect failures.**
- [ ] **Step 3: Implement.**
- [ ] **Step 4: Full suite green.**
- [ ] **Step 5: Commit** — `feat(availability): run without a bin sensor; RO tank, pressure and sump rows`

---

### Task 5: ICE-101 auto-retry cooldown

**Files:**
- Modify: `packages/common/src/ice_common/config_model.py` (`physical.ice_retry_cooldown_seconds`)
- Modify: `packages/edge/src/ice_edge/faults/fault_service.py` (`__init__`, `raise_fault`, `clear_fault`, `clear_ice101_lockouts` l.239)
- Modify: `packages/edge/src/ice_edge/app/machine.py` (FaultService construction)
- Test: `tests/edge/faults/test_fault_service.py` (or the existing FaultService test file), using `machine_fake_time`

**Interfaces:**
- Consumes: `Availability.bin_instrumented` (Task 4).
- `FaultService.__init__` gains keyword-only `bin_instrumented: Callable[[], bool]` and `ice_retry_cooldown_seconds: Callable[[], float]`. `Machine` passes `lambda: (a.bin_instrumented if (a := self._outputs.availability) else True)` and `lambda: self._config.physical.ice_retry_cooldown_seconds` (use the attribute `Machine` actually holds config under).
- Behaviour: `raise_fault(ICE_101, sku=...)` when `not bin_instrumented()` arms (or re-arms, cancelling any previous) a `tasks.call_later`-style timer labelled `ice_retry_cooldown` (use whatever delayed-callback method `TaskRunner` exposes, with the label argument other timers use). On fire: `clear_ice101_lockouts(by="cooldown")`. `clear_ice101_lockouts` gains `by: str = "hardware"` and passes it through to the `lockout_cleared` row. An operator `clear_fault` that removes the last ICE-101 lockout cancels the timer. `Machine.cancel_pending_tasks` cancels it too (if pending timers are not already covered by `TaskRunner.cancel_pending`).

- [ ] **Step 1: Write failing tests** (on `machine_fake_time`, availability attached, bin not instrumented)
  - Raising ICE-101 for the ice sku arms `ice_retry_cooldown`; `runner.fire("ice_retry_cooldown")` clears the lockout and records `lockout_cleared` with `by="cooldown"`.
  - Raising again after it fired re-arms; raising twice before it fires leaves exactly one pending `ice_retry_cooldown`.
  - Operator `clear_fault` on the ICE-101 key cancels the pending timer.
  - With the bin instrumented, raising ICE-101 arms nothing (today's `bin_half_full` auto-clear still works).
  - Config: default 1800; 59 and 86401 rejected.
- [ ] **Step 2: Run; expect failures.**
- [ ] **Step 3: Implement.**
- [ ] **Step 4: Full suite green.**
- [ ] **Step 5: Commit** — `feat(faults): ICE-101 clears after a cooldown when the bin is not instrumented`

---

### Task 6: `InputFaults` — water faults from `hardware/io`

**Files:**
- Create: `packages/edge/src/ice_edge/faults/input_faults.py`
- Modify: `packages/common/src/ice_common/config_model.py` (`physical.ro_max_run_seconds`)
- Modify: `packages/edge/src/ice_edge/hal/mqtt_inbound.py` (`TelemetryRouter.__init__` gains `on_hardware_io`; `handle_hardware_io` l.89 calls it for every validated message)
- Modify: `packages/edge/src/ice_edge/app/machine.py` (build `InputFaults`, wire `on_hardware_io`, cancel its timer in `cancel_pending_tasks`)
- Test: `tests/edge/faults/test_input_faults.py` (new), `tests/edge/hal/test_mqtt_inbound.py`

**Interfaces:**
- Produces:
  ```python
  class InputFaults:
      def __init__(self, *, faults: FaultService, products: Callable[[], list[Product]],
                   tasks: TaskRunner, ro_max_run_seconds: Callable[[], float]) -> None: ...
      def on_hardware_io(self, device: str, state: bool) -> None: ...
      def cancel(self) -> None: ...   # drops the ro_overrun timer
  ```
- Mapping (§4.3): `sump_high` true → `raise_fault(WTR_104)`, false → clear it. `water_pressure_ok` false → `raise_fault(WTR_105, sku=s)` for every `kind == "water"` product, true → clear those. `ro_tank_empty` true/false → same pattern with `WTR_106`. `ro_production` true → arm `ro_overrun` timer for `ro_max_run_seconds()`; on fire → `raise_fault(WTR_107)`; `ro_production` false → cancel the timer and clear `WTR_107` if active. Clears use `clear_fault(key, by="hardware")` with the key format `FaultService.parse_key` accepts; raising an already-active fault is not repeated (check `faults.has(...)` / `faults.is_locked(sku)` first) so a re-published retained state does not spam alerts. Recovery of one input never clears another input's code.
- `TelemetryRouter(..., on_hardware_io: Callable[[str, bool], None])`; `Machine` passes `self._input_faults.on_hardware_io`.
- `InputFaults` imports only from `ice_contracts`, `ice_common` and `ice_edge.faults`/`ice_edge.supervisor` (layer rule).

- [ ] **Step 1: Write failing tests**
  - Each input raises its code and the opposite state clears it; water codes land on every water sku and no ice sku; WTR-104 is machine-scope and `payment_enabled()` goes false (via availability's active-fault push).
  - Pressure recovery does not clear a WTR-106 held by an empty tank on the same sku.
  - Repeating the same failing state does not raise a second alert (count `publish_alert` calls or recorder rows).
  - `ro_production` true then `runner.fire("ro_overrun")` raises WTR-107; `ro_production` false clears it and cancels a still-pending timer; an `ro_production` true/false/true sequence leaves one pending timer.
  - Inbound: a `hardware/io/sump_high` message reaches `on_hardware_io` once.
  - Config: `ro_max_run_seconds` default 3600; 59 and 86401 rejected.
- [ ] **Step 2: Run; expect failures.**
- [ ] **Step 3: Implement.**
- [ ] **Step 4: Full suite green; `uv run lint-imports` passes.**
- [ ] **Step 5: Commit** — `feat(faults): derive WTR-104..107 from water and RO inputs`

---

### Task 7: Simulator — IM600XL channels, vends and Home Assistant

**Files:**
- Modify: `packages/sims/src/ice_sims/vending_machine.py` (`HARDWARE_DEVICES` l.53-71, `_VENDING_CHANNELS` l.80-206, `_run_bagged_ice` l.888-975, `_run_water_fill` l.1070-1151, `_handle_water_valve` l.1197, `ha_discovery_entities` l.434-507, fault registrations l.309-432 for the two removals and `ice_bin_empty`)
- Test: `tests/sims/test_simulator_vending.py` (rewrite the channel-list tests l.491-563 and every assertion on removed ids)

**Interfaces:**
- Channel set, kinds and `driven_by` exactly as spec §5.1 (inputs, outputs, counter `fill_pulses`, temperature `ambient`). `HARDWARE_DEVICES` holds every binary channel with its idle default: `water_pressure_ok=True`, everything else `False`.
- Bagged ice honours all three release variants; for `ReleaseByCatch` the catch channel is set `True` before the agitate step and `False` at release, then the step waits `drop_seconds`; any failure path sets it `False`. Current sense is `unmonitored` (no current telemetry published).
- Water fill counts `fill_pulses` against the profile target; it no longer publishes a binary flow-sensor `hardware/io` message. `_handle_water_valve` drives the profile/declared valve channel only.
- Fault injection: remove `door_stuck_open` and `bag_drop_solenoid_stuck`; `ice_bin_empty` makes the fill run to its max time and report `bin_empty`, touching no bin channel.
- `ha_discovery_entities` derives binary entities from the binary channels and a temperature entity from `ambient`.

- [ ] **Step 1: Write failing tests**
  - Ordered channel-id list, output set, `driven_by` map and `HARDWARE_DEVICES` keys equal the §5.1 set; none of the removed ids appear.
  - A `ReleaseByCatch` ice vend publishes `trap_door_catch=True` before the first `agitator_motor=True`, `trap_door_catch=False` after `bag_full_sensor=True`, and ends `complete`; an injected `auger_jam` ends with the catch `False`.
  - A water vend for a 5 gal profile reports `fill_pulses` reaching the target and `complete`; no `hardware/io/water_flow_sensor` is published.
  - `ice_bin_empty` yields `bin_empty` without any `bin_half_full` publish.
  - HA entity object ids equal the binary channel ids plus `ambient`.
  - Existing profiles using `ReleaseBySensor`/`ReleaseTimed` (from `tests/dispenser_fixtures.py`) still run to `complete`.
- [ ] **Step 2: Run; expect failures.**
- [ ] **Step 3: Implement.**
- [ ] **Step 4: Full suite green** — if `tests/integration/test_integration_e2e.py` depended on `bin_half_full`, Task 4's capability path must now make ice sellable; fix the test's expectations, not the edge.
- [ ] **Step 5: Commit** — `feat(sims): vending simulator declares the IM600XL channel set`

---

### Task 8: Simulator — RO loop, environment, inputs and new fault injections

**Files:**
- Modify: `packages/sims/src/ice_sims/vending_machine.py` (`_publish_sensors` l.532-567; fault registrations; water vend tank drain)
- Test: `tests/sims/test_simulator_vending.py`

**Interfaces:**
- Simulated RO tank: `self.ro_tank_gallons: float`, class constants `RO_TANK_CAPACITY_GAL`, `RO_TANK_FULL_GAL`, `RO_TANK_EMPTY_GAL`, `RO_FILL_GAL_PER_TICK`, `RO_MAX_RUN_TICKS` (pick realistic values; tests may override per instance). Each sensor tick: `ro_production` on while below full and run-ticks under the max, off otherwise (and at the max the run stops until the level drops below full again); `ro_tank_full`/`ro_tank_empty` follow the level; changes are published on `hardware/io/<id>`. A completed water vend subtracts its volume.
- `ambient` drifts and publishes on `sensors/temp/ambient`; `heater_power` hysteresis keeps the old thresholds (on below 5 °C, off above 10 °C).
- `water_pressure_ok`, `sump_high`, `service_door` published on change (and on the periodic re-publish today's code does for retained state).
- New fault injections (same registration pattern as existing ones): `ro_tank_empty` (forces level to 0 and blocks fill), `low_pressure` (`water_pressure_ok=False`), `sump_high`, `service_door_open`, `ro_fill_stuck` (`ro_production` stays on, level does not rise — the edge's `ro_overrun` detects it). Each recover restores the normal state.

- [ ] **Step 1: Write failing tests**
  - From empty, ticking fills the tank: `ro_production` on, then `ro_tank_full` true and `ro_production` off.
  - A water vend drains the tank by its volume; draining past empty sets `ro_tank_empty` true.
  - With `RO_MAX_RUN_TICKS` small and `ro_fill_stuck` active, `ro_production` turns off at the limit.
  - Ambient below 5 °C turns `heater_power` on; above 10 °C off.
  - Each new fault's activate/recover publishes the expected `hardware/io` states.
- [ ] **Step 2: Run; expect failures.**
- [ ] **Step 3: Implement.**
- [ ] **Step 4: Full suite green.**
- [ ] **Step 5: Commit** — `feat(sims): RO tank loop, ambient heater, water and door inputs`

---

### Task 9: IM600XL dispenser profiles and fixtures

**Files:**
- Modify: `packages/edge/src/ice_edge/vend/dispensers_doc.py` sample values (l.517-650)
- Regenerate: `dispensers.example.toml` (`uv run python scripts/gen_dispensers_example.py`)
- Modify: `tests/dispenser_fixtures.py` (add an IM600XL fixture set; keep the existing ones used by other tests)
- Test: `tests/edge/vend/test_dispensers_example.py`, `tests/edge/vend/test_dispensers_cli.py`

**Interfaces:**
- Sample (§6): slot 0 bagged ice — agitate `agitator_motor` (current `unmonitored`), `IceFillBySensor` on `auger_motor`/`bag_full_sensor`, `ReleaseByCatch` on `trap_door_catch`, accessory `bag_fan`; slots 1 and 2 water 1 gal and 5 gal — `WaterFillByVolume` on `water_valve_solenoid`/`fill_pulses`. Slots and skus agree with `config.example.json` (Task 2).

- [ ] **Step 1: Write failing test** — the generated example validates clean (`ValidationReport.ok`) against `config.example.json`'s catalog **and** against the simulator's capabilities (`build_capabilities` from Task 7's channels).
- [ ] **Step 2: Run; expect failure.**
- [ ] **Step 3: Update the sample; regenerate the example.**
- [ ] **Step 4: Full suite green** (byte-drift test included). Also run `uv run python -m ice_edge.vend.dispensers --example` and confirm it prints the new example.
- [ ] **Step 5: Commit** — `feat(dispensers): IM600XL sample profiles`

---

### Task 10: End-to-end check and documentation

**Files:**
- Modify: `tests/integration/test_integration_e2e.py` (ice and water sale against the new simulator)
- Modify: `CLAUDE.md` — vending contract 2.0.0 (Fault Codes section), timer labels now eight (`ice_retry_cooldown`, `ro_overrun`), `Product.button` and named `ButtonPress`, `InputFaults`, `Availability.bin_instrumented`, two new `physical` config fields, simulator channel set
- Modify: `docs/hardware/im600xl-io-list.md` §7 — mark each divergence resolved, linking the spec
- Modify: `ROADMAP.md` §3 — `water_pressure_ok`, `no_leak` (sump), RO tank now instrumented; bin level not instrumented on this machine
- Modify: `docs/OVERVIEW.md` — link the spec under Simulators

**Interfaces:** none new.

- [ ] **Step 1: Write the e2e test** — broker + edge + simulator (existing harness): pressing `button_ice` with credit completes an ice sale with no `bin_half_full` ever published; pressing `button_water_1gal` completes a water sale; activating `sump_high` disables payment and recovering re-enables it.
- [ ] **Step 2: Run it** (`uv run pytest tests/integration -q`) and fix only test-harness issues; any product defect goes back to its task.
- [ ] **Step 3: Update the docs.**
- [ ] **Step 4: Full verification** — `uv run pytest`, `ruff check .`, `ruff format --check .`, `uv run lint-imports`, `uv run python scripts/check_package_isolation.py`, `uv run python -m ice_contracts.generate` leaves no diff.
- [ ] **Step 5: Commit** — `docs: IM600XL simulator — contract 2.0.0, inputs, timers`
