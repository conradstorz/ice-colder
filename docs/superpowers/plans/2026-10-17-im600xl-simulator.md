# IM600XL Simulator Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make the vending simulator, the vending board contract and the edge match the real Kooler Ice IM600XL I/O list.

**Architecture:** Contract first (new release variant, fault codes, named buttons, 2.0.0), then the edge learns to run without a bin sensor and to derive water faults from `hardware/io`, then the simulator is rebuilt on the IM600XL channel set, then generated references and docs follow. Each task leaves `uv run pytest` green.

**Tech Stack:** Python 3.12, Pydantic v2, asyncio, aiomqtt, FastAPI/Jinja/HTMX, pytest, uv.

**Spec:** [`docs/superpowers/specs/2026-10-17-im600xl-simulator-design.md`](../specs/2026-10-17-im600xl-simulator-design.md). Section references (§) are to the spec. Hardware tags (IC4, W7…) are from [`docs/hardware/im600xl-io-list.md`](../../hardware/im600xl-io-list.md).

**Plan style:** each task gives files, interfaces and the tests to write. Implementers write the code bodies, matching surrounding style.

## Global Constraints

- Vending-machine `CONTRACT_VERSION` goes **1.0.0 → 2.0.0** (Task 2). Ice-maker-monitor and edge-cloud contracts are untouched.
- `PAYMENT_BLOCKING_FAULTS` stays **seven** codes; `WTR-105`, `WTR-106` and `WTR-107` are not members.
- Input-driven faults (`WTR-104`..`WTR-107`) are all **machine-scope** and never raise or clear a product lockout. `FaultRegistry.lockouts` holds one code per sku (`raise_fault` overwrites, `clear_fault(sku)` pops whatever is there), so an input-driven product lockout would clobber a vend-failure lockout and then unlock it on recovery. Water sales are blocked by the availability rows (Task 4), not by lockouts (spec D6/D8).
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
| `packages/contracts/src/ice_contracts/board/vending_machine.py` | `WTR-105` re-scoped, `WTR-106`, `WTR-107`, `OUTCOME_FAULTS`, version 2.0.0 |
| `packages/contracts/src/ice_contracts/board/mqtt_messages.py` | `ButtonPress.button: str` |
| `docs/contracts/vending-machine/CONTRACT.md` + `schemas/` | 2.0.0 section, regenerated |
| `packages/edge/src/ice_edge/vend/dispensers.py`, `dispensers_doc.py` | catch channel in cross-checks; IM600XL sample |
| `packages/common/src/ice_common/config_model.py` | `Product.button`, two `physical` fields |
| `packages/edge/src/ice_edge/sale/vmc.py`, `sale_journal.py` | button lookup, select by product |
| `packages/edge/src/ice_edge/web/routes/products/catalog.py`, `packages/ui/src/ice_ui/templates/product_form.html`, `product_catalog.html` | Button field |
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
- Test: `tests/contracts/test_dispenser_schema.py`, `tests/contracts/test_contracts_vending.py`, `tests/edge/vend/test_dispensers_validation.py`

**Interfaces:**
- Produces: `ReleaseByCatch(proof: Literal["catch"], catch_channel: ChannelId, drop_seconds: float  # 0.5–30, unit "s")`; `ReleaseStep = Annotated[ReleaseBySensor | ReleaseTimed | ReleaseByCatch, Field(discriminator="proof")]`. `drive_channels(profile)` includes `catch_channel`; `worst_case_seconds(profile)` adds `drop_seconds`.
- Produces: `FaultCode.WTR_106 = "WTR-106"` (`warning`, `Scope.machine`, "RO storage tank empty"), `FaultCode.WTR_107 = "WTR-107"` (`warning`, `Scope.machine`, "RO production overran its maximum run time"). `OUTCOME_FAULTS[("water_fill", DispenserOutcome.bin_empty)] == FaultCode.WTR_106`.
- Changes: `FAULT_TABLE[WTR_105]` from `product_unavailable`/`Scope.product` to `warning`/`Scope.machine` (description unchanged). Nothing raises WTR-105 today (`grep -rn WTR_105 packages` finds only the table), so no caller changes.
- Docstring of `ReleaseByCatch` states §3.2's MUSTs (energize before agitate, hold through fill, de-energize to drop, leave off between vends, de-energize on fault).

- [ ] **Step 1: Write failing tests**
  - `ReleaseByCatch` parses from a `[slot.N.release]` table with `proof = "catch"`; rejects `drop_seconds` 0.4 and 30.1; rejects an extra field (strict model).
  - A bagged-ice profile with `ReleaseByCatch` lists `catch_channel` in `drive_channels`, and `worst_case_seconds` grows by exactly `drop_seconds` versus the same profile with `drop_seconds` reduced by 1.
  - `validate_document` reports a drive/sense conflict when `catch_channel` equals the fill `sensor_channel`.
  - With capabilities declaring `trap_door_catch` as an `input`, `validate_document` reports a direction error for it.
  - `FAULT_TABLE` rows for WTR-105/WTR-106/WTR-107 are all `warning`/`Scope.machine`; none is in `PAYMENT_BLOCKING_FAULTS`; `len(PAYMENT_BLOCKING_FAULTS) == 7`; water `bin_empty` maps to WTR-106 and bagged-ice `bin_empty` still maps to ICE-101.
- [ ] **Step 2: Run them; expect failures** — `uv run pytest tests/contracts tests/edge/vend -q`
- [ ] **Step 3: Implement** the variant, helper updates, codes and mapping. Update the existing assertion at `tests/contracts/test_contracts_vending.py:75` (water `bin_empty → ICE-101`). A water vend's `bin_empty` now raises a machine-scope warning and locks no product; update any VMC vend-failure test that expected that sku locked.
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
- Modify: `packages/sims/src/ice_sims/vending_machine.py`: add the three binary input channels `button_ice`, `button_water_1gal`, `button_water_5gal` to `_VENDING_CHANNELS` (additive; Task 7 reworks the rest of the list). `_customer_loop` (l.1246-1297) presses a random **declared** button — a binary `input` channel whose id starts with `button_` — and the indecisive second press picks a different declared button. It no longer reads the product catalog at all (drop `num_buttons`/`_pick_button`'s catalog dependency): a real board knows its buttons, not the edge's catalog, and a button the catalog does not map exercises the edge's unknown-button path (spec §5.2)
- Modify: `packages/edge/src/ice_edge/app/startup_config.py` (new `warn_if_no_buttons(config)`, beside `warn_if_setup_mode` l.88) and `packages/edge/src/ice_edge/__main__.py` (call it after `load_config()`, next to the `warn_if_setup_mode` call l.67)
- Modify: `config.example.json` products: `SAMPLE-ICE` (slot 0) → `"button_ice"`, `SAMPLE-WATER-SM` (slot 1, 1 gal) → `"button_water_1gal"`, `SAMPLE-WATER-LG` (slot 2, 5 gal) → `"button_water_5gal"`
- Test: `tests/contracts/test_mqtt_messages_validation.py` (ButtonPress), `tests/common/test_config_model.py`, `tests/edge/app/test_startup_policy.py` (where `warn_if_setup_mode` is tested), `tests/edge/sale/test_vmc_flows.py`, `tests/edge/sale/test_vmc_observers.py`, `tests/edge/sale/test_vmc_dispense_profiles.py` (every `vmc.select_product(<int>)`), `tests/integration/test_integration_e2e.py` (l.384, 431, 473, 525 `ButtonPress(button=<int>)` and l.614 `json.dumps({"button": 0})`), `tests/sims/test_simulator_vending.py`

**Interfaces:**
- Produces: `ButtonPress(button: str  # CHANNEL_ID_PATTERN, action: str = "pressed", timestamp: datetime)`.
- Produces: `Product.button: Optional[str] = None` (pattern-validated; `None` = no physical button). `PhysicalDetails` raises `ValueError` naming duplicate button ids.
- Produces: `VMC.select_product(product: Product) -> None` (replaces the index form; same refusal checks). `VMC.on_button_press(data)` finds `p for p in self.products if p.button == press.button`; unknown → `journal.selection_refused_unknown_button(button: str)`, no exception, no state change.
- Produces: `SaleJournal.selection_refused_unknown_button(button: str) -> None`; `SaleJournal.product_selected(product, ...)` no longer receives an index (update its signature and log line to name the sku).
- `find_product(sku)` stays public (still used elsewhere); `begin_test_sale` calls `select_product(product)`.
- Produces: `warn_if_no_buttons(config: ConfigModel) -> None` — **migration aid** for existing `data/config.json` files, whose products have no `button` after this change and so would silently ignore every press. Logs one `warning` when the catalog has products but none has a `button` ("no product has a customer button; button presses will select nothing — set Product → Button"), else one `info` line naming any products with `button=None`. Never raises, never edits the config.

- [ ] **Step 1: Write failing tests**
  - `ButtonPress(button=0)` and `ButtonPress(button="Bad-Id")` raise `ValidationError`; `ButtonPress(button="button_ice")` round-trips through JSON.
  - Config: two products with `button="button_ice"` fail to load with a message naming `button_ice`; two products with `button=None` load.
  - VMC: a `hardware/buttons` payload `{"button": "button_water_5gal"}` selects the product whose `button` is that id; an unknown id leaves state `idle`/unchanged and logs the refusal; a locked-out product's button is refused as before.
  - Test sale still selects its product (existing `run_test_sale` tests pass unmodified apart from index removal).
  - Simulator: one customer-loop press publishes a `ButtonPress` whose `button` is one of the three declared `button_*` ids, whatever the catalog holds; the indecisive second press names a different declared id.
  - The existing channel-list tests (`tests/sims/test_simulator_vending.py` l.491-563) gain the three `button_*` inputs; Task 7 rewrites them fully.
  - `warn_if_no_buttons`: products with no buttons → one warning; some unassigned → info naming them; all assigned → nothing above debug; empty catalog → no warning.
  - `CONTRACT_VERSION == "2.0.0"`.
- [ ] **Step 2: Run; expect failures.**
- [ ] **Step 3: Implement.** Convert every existing test that sends an integer button to a named one (give the fixture products buttons). **The integration tests skip locally** when no broker answers on `localhost:1883` (this workstation runs no local Docker), but CI fails the build if they skip — so a green local run proves nothing about them. Convert the five e2e sites listed above by hand, then confirm none remain: `grep -rnE 'ButtonPress\(button=[0-9]|"button": ?[0-9]' tests` must print nothing. CI's integration job is the run that proves them. Check `tests/meta/test_vmc_complexity.py` still passes. It fails over budget **and** more than 10% under budget: if removing the index logic drops a count more than 10% under, lower that budget to the printed number; if a budget would be exceeded, move the lookup into a small pure helper (e.g. `ice_edge.sale.buttons.product_for_button(products, button) -> Product | None`).
- [ ] **Step 4: Regenerate schemas; full suite green.**
- [ ] **Step 5: Commit** — `feat!: named customer buttons (vending contract 2.0.0)`

---

### Task 3: Button field on the Products catalog form

**Files:**
- Modify: `packages/edge/src/ice_edge/web/routes/products/catalog.py` (`create_product` l.60, `update_catalog` l.160, `copy_product_form` l.201)
- Modify: `packages/ui/src/ice_ui/templates/product_form.html` (create/copy form) and `product_catalog.html` (catalog edit form) — both render the catalog fields
- Rebuild if new classes are used: `packages/ui/src/ice_ui/static/app.css` (Tailwind command in CLAUDE.md)
- Test: `tests/edge/web/test_routes_products.py`

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
- Produces: `Availability.on_vending_capabilities(subsystem: str, caps: SubsystemCapabilities) -> None` — for `subsystem == "vending"`, calls `set_bin_instrumented(any(c.channel_id == "bin_half_full" for c in caps.channels))`.
- Produces rows: new `ro_tank_ok` (`Applies.water`, `Gate.fulfillment`, starts not-instrumented); `water_pressure_ok` and `no_leak` keep their names/applies/gates but become instrumented on their first `hardware/io` report. `set_hardware_io`: `ro_tank_empty` → `ro_tank_ok` fails while true ("RO tank empty"); `water_pressure_ok` → row fails while false ("low water pressure"); `sump_high` → `no_leak` fails while true ("sump high").
- `Machine` wires `on_capabilities_validated` (today `self._gate.on_vending_capabilities`, `machine.py:174`) to a closure calling `self._gate.on_vending_capabilities(...)` then `availability.on_vending_capabilities(...)` when availability is attached (`self._outputs.availability`, the public attribute `FaultService` already reads).

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
- Test: `tests/edge/faults/test_fault_service.py`, using `machine_fake_time`

**Interfaces:**
- Consumes: `Availability.bin_instrumented` (Task 4).
- `FaultService.__init__` gains keyword-only `bin_instrumented: Callable[[], bool]` and `ice_retry_cooldown_seconds: Callable[[], float]`. `Machine` passes `lambda: (a.bin_instrumented if (a := self._outputs.availability) else True)` and `lambda: self._config.physical.ice_retry_cooldown_seconds`.
- Behaviour: `raise_fault(ICE_101, sku=...)` when `not bin_instrumented()` arms (or re-arms, calling `.cancel()` on the previous handle) `self._tasks.schedule(seconds, callback, label="ice_retry_cooldown")`, which returns a cancellable task (`FakeTask` under `FakeTaskRunner`). On fire: `clear_ice101_lockouts(by="cooldown")`. `clear_ice101_lockouts` gains `by: str = "auto"` (today's hard-coded value, so the `bin_half_full` path records exactly what it does now) and passes it through to `clear_fault`. An operator `clear_fault` that removes the last ICE-101 lockout cancels the timer. No extra shutdown hook: `TaskRunner.cancel_pending` (called by `Machine.cancel_pending_tasks`) already cancels every `schedule()` task.

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
- Modify: `packages/edge/src/ice_edge/app/machine.py` (build `InputFaults`, wire `on_hardware_io`; its timer is `self._tasks.schedule(..., label="ro_overrun")`, so `TaskRunner.cancel_pending` already covers shutdown)
- Test: `tests/edge/faults/test_input_faults.py` (new), `tests/edge/hal/test_mqtt_inbound.py`

**Interfaces:**
- Produces:
  ```python
  class InputFaults:
      def __init__(self, *, faults: FaultService, tasks: TaskRunner,
                   ro_max_run_seconds: Callable[[], float]) -> None: ...
      def on_hardware_io(self, device: str, state: bool) -> None: ...
      def cancel(self) -> None: ...   # drops the ro_overrun timer
  ```
- Mapping (§4.3), every code machine-scope and raised with no `sku`: `sump_high` true → `raise_fault(WTR_104)`, false → clear it. `water_pressure_ok` false → `raise_fault(WTR_105)`, true → clear it. `ro_tank_empty` true → `raise_fault(WTR_106)`, false → clear it. `ro_production` true → arm `ro_overrun` timer for `ro_max_run_seconds()`; on fire → `raise_fault(WTR_107)`; `ro_production` false → cancel the timer and clear `WTR_107` if active. Clears use `clear_fault(code.value, by="hardware")` (the machine-fault key `FaultService.parse_key` accepts) and are skipped when `not faults.has(code)`; raising an already-active fault is not repeated (check `faults.has(code)` first) so a re-published retained state does not spam alerts. Recovery of one input never clears another input's code.
- `TelemetryRouter(..., on_hardware_io: Callable[[str, bool], None])`; `Machine` passes `self._input_faults.on_hardware_io`.
- `InputFaults` imports only from `ice_contracts`, `ice_common` and `ice_edge.faults`/`ice_edge.supervisor` (layer rule).

- [ ] **Step 1: Write failing tests**
  - Each input raises its machine-scope code and the opposite state clears it; `faults.lockouts` is unchanged throughout. WTR-104 makes `payment_enabled()` false (via availability's active-fault push); WTR-105/WTR-106 leave it true.
  - Pressure recovery does not clear a WTR-106 held by an empty tank (independent machine codes).
  - A water sku already locked by `WTR-101` (a vend failure) is still locked by `WTR-101` after `water_pressure_ok` false then true.
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
- **Legacy channels:** the simulator no longer *declares* `door_sensor`, `bag_drop_solenoid`, `water_flow_sensor` or `vending_now_light`, but it still *drives* any channel a profile names, because `set_hw` (`vending_machine.py:512`) publishes any device id without checking the declared list. That is what keeps the `tests/dispenser_fixtures.py` profiles (migrated only in Task 9) running here. Keep it that way: do not add a declared-channel check to `set_hw` or `execute_profile`. A `ReleaseBySensor` profile still gets its simulated door-sensor response on the profile's `sensor_channel`. Those undeclared channels are not added to `HARDWARE_DEVICES`.
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
  - The simulator's `capabilities/vending` document validates against `tests/dispenser_fixtures.py` profiles **with errors** for the removed channels (expected until Task 9) — assert nothing about this here; Task 9 owns the clean validation.
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
- Modify: `tests/integration/test_integration_e2e.py` (ice and water sale with the IM600XL capabilities)
- Modify: `CLAUDE.md` — vending contract 2.0.0 (Fault Codes section, including `WTR-105` re-scoped to machine/`warning` and the rule that input-driven faults never lock a product), timer labels now eight (`ice_retry_cooldown`, `ro_overrun`), `warn_if_no_buttons`, `Product.button` and named `ButtonPress`, `InputFaults`, `Availability.bin_instrumented`, two new `physical` config fields, simulator channel set
- Modify: `docs/hardware/im600xl-io-list.md` §7 — mark each divergence resolved, linking the spec
- Modify: `ROADMAP.md` §3 — `water_pressure_ok`, `no_leak` (sump), RO tank now instrumented; bin level not instrumented on this machine
- Modify: `docs/OVERVIEW.md` — link the spec under Simulators

**Interfaces:** none new.

- [ ] **Step 1: Write the e2e tests** in the **existing harness**, which runs a real broker and the edge but no simulator: each test publishes the board's messages itself (`hardware/buttons`, `hardware/dispenser`, `hardware/io/...`), as the current tests do. Do not build a simulator-in-the-loop harness here. To stand in for the IM600XL board, publish `VendingMachineSimulator(machine_id=...).build_capabilities()` (the real Task 7 channel set) on `capabilities/vending` before the sale. Use `_wire_dispenser_runtime` with the IM600XL fixture set from Task 9, so profiles validate against those capabilities with no `CFG-101`. Tests:
  - Credit, then `ButtonPress(button="button_ice")`, then a `complete` `hardware/dispenser` report → one ice sale recorded, and no `hardware/io/bin_half_full` was ever published in that test.
  - Credit, then `button_water_1gal`, then `complete` → one water sale.
  - `hardware/io/sump_high` true → `payment_enabled()` false and `WTR-104` active; false → payment re-enabled and the fault cleared.
  - `hardware/io/water_pressure_ok` false → water refused, ice still sellable, `faults.lockouts` unchanged; true → water sellable again.
- [ ] **Step 2: Run it** (`uv run pytest tests/integration -q`). These skip without a broker on `localhost:1883`; a local skip is not a pass. Confirm on CI's integration job, where a skip fails the build. Fix only test-harness issues; any product defect goes back to its task.
- [ ] **Step 3: Update the docs.**
- [ ] **Step 4: Full verification** — `uv run pytest`, `ruff check .`, `ruff format --check .`, `uv run lint-imports`, `uv run python scripts/check_package_isolation.py`, `uv run python -m ice_contracts.generate` leaves no diff.
- [ ] **Step 5: Commit** — `docs: IM600XL simulator — contract 2.0.0, inputs, timers`
