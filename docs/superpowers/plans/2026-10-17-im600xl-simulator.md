# IM600XL Simulator Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make the vending simulator, the vending board contract and the edge match the real Kooler Ice IM600XL I/O list.

**Architecture:** Contract first (new release variant, fault codes, well-known input channels, named buttons, 2.0.0), then the IM600XL dispenser fixtures (so nothing downstream leans on removed channels), then the edge learns to run without a bin sensor and to derive water faults from `hardware/io` through small single-purpose collaborators, then the simulator is split into isolated modules **before** it is rebuilt on the IM600XL channel set, then cross-checks and docs follow. Each task leaves `uv run pytest` green.

**Isolation principle (applies to every task).** One concern per module, one module per task where possible:
- The only code the simulator and the edge share is `ice_contracts`. A channel id that carries edge semantics is a contract constant, never a string literal repeated on both sides.
- Simulator physical models (RO tank, climate) are pure: no MQTT, no `sleep`, no `_hw` dict. The simulator class composes them and owns publishing.
- Simulator tests import `ice_contracts`, `ice_common`, `ice_sims` and edge-free test helpers only — never `ice_edge`, directly or through a `tests.*` helper. Edge tests never import `ice_sims`. A check that needs both sides (simulator capabilities × edge validation) lives in `tests/integration/`. Both directions are enforced by an import-following meta test (Task 9), not by grep.
- Edge timer policies (`ice_retry_cooldown`, `ro_overrun`) are separate collaborators in `ice_edge.faults`, peers of each other; `FaultService` keeps fault state and its side effects, and gains only a generic observer hook.

**Tech Stack:** Python 3.12, Pydantic v2, asyncio, aiomqtt, FastAPI/Jinja/HTMX, pytest, uv.

**Spec:** [`docs/superpowers/specs/2026-10-17-im600xl-simulator-design.md`](../specs/2026-10-17-im600xl-simulator-design.md). Section references (§) are to the spec. Hardware tags (IC4, W7…) are from [`docs/hardware/im600xl-io-list.md`](../../hardware/im600xl-io-list.md).

**Plan style:** each task gives files, interfaces and the tests to write. Implementers write the code bodies, matching surrounding style.

## Global Constraints

- Vending-machine `CONTRACT_VERSION` goes **1.0.0 → 2.0.0** (Task 2). Ice-maker-monitor and edge-cloud contracts are untouched.
- `PAYMENT_BLOCKING_FAULTS` stays **seven** codes; `WTR-105`, `WTR-106` and `WTR-107` are not members.
- Input-driven faults (`WTR-104`..`WTR-107`) are all **machine-scope** and never raise or clear a product lockout. `FaultRegistry.lockouts` holds one code per sku (`raise_fault` overwrites, `clear_fault(sku)` pops whatever is there), so an input-driven product lockout would clobber a vend-failure lockout and then unlock it on recovery. Water sales are blocked by the availability rows (Task 6), not by lockouts (spec D6/D8).
- Channel ids match `CHANNEL_ID_PATTERN = r"^[a-z0-9_]{1,64}$"`. Ids with edge meaning come from `ice_contracts.board.vending_machine.VendingChannel` (Task 1); no new string literal for them in `ice_edge` or `ice_sims`.
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
| `packages/contracts/src/ice_contracts/board/vending_machine.py` | `WTR-105` re-scoped, `WTR-106`, `WTR-107`, `OUTCOME_FAULTS`, `VendingChannel`, `INPUT_FAULTS`, version 2.0.0 |
| `packages/contracts/src/ice_contracts/board/mqtt_messages.py` | `ButtonPress.button: str` |
| `docs/contracts/vending-machine/CONTRACT.md` + `schemas/` | 2.0.0 section, regenerated |
| `packages/edge/src/ice_edge/vend/dispensers.py`, `dispensers_doc.py` | catch channel in cross-checks; IM600XL sample |
| `packages/common/src/ice_common/config_model.py` | `Product.button`, two `physical` fields |
| `packages/edge/src/ice_edge/sale/vmc.py`, `sale_journal.py` | button lookup, select by product |
| `packages/edge/src/ice_edge/web/routes/products/catalog.py`, `packages/ui/src/ice_ui/templates/product_form.html`, `product_catalog.html` | Button field |
| `packages/edge/src/ice_edge/hal/mqtt_inbound.py` | `TelemetryRouter` subscriber lists for capabilities and hardware IO |
| `packages/edge/src/ice_edge/faults/availability.py` | bin-instrumented flag, three rows |
| `packages/edge/src/ice_edge/faults/fault_service.py` | `subscribe_fault_change` observer hook only |
| `packages/edge/src/ice_edge/faults/ice_retry.py` (new) | `IceRetry`: `ice_retry_cooldown` policy |
| `packages/edge/src/ice_edge/faults/input_faults.py` (new) | `InputFaults`: `hardware/io` → WTR-104/105/106/107 |
| `packages/edge/src/ice_edge/app/machine.py` | wiring only |
| `packages/sims/src/ice_sims/vending/` (new package) | `channels`, `dispense`, `customer`, `faults`, `ro_tank`, `climate`, `simulator` |
| `packages/sims/src/ice_sims/vending_machine.py` | becomes the thin entry module (`python -m ice_sims.vending_machine`, compose, CI smoke import) |
| `dispensers.example.toml`, `config.example.json`, `tests/dispenser_fixtures.py` | IM600XL samples |
| `tests/sims/vending_profiles.py` (new) | simulator-local profile fixtures (contract models only) |
| `tests/integration/test_im600xl_capabilities.py` (new) | simulator capabilities × edge validation cross-check |
| `tests/async_helpers.py` (new) | edge-free `settle`/`patch_module_sleep`/`_AsyncioWithSleep`, re-exported by `tests/fakes.py` |
| `tests/repo_root.py` (new) | edge-free `REPO_ROOT`/`TESTS_ROOT`, re-exported by `tests/paths.py` |
| `tests/sims/test_simulator_base.py`, `test_simulator_mdb.py` | import helpers from `tests.async_helpers` |
| `tests/private_access_budget.py` | budgets for the split simulator test files |
| `tests/meta/test_test_isolation.py` (new) | import-following check: `tests/sims` ↛ `ice_edge`, `tests/edge` ↛ `ice_sims` |
| `CLAUDE.md`, `docs/hardware/im600xl-io-list.md`, `ROADMAP.md` | docs |

---

### Task 1: Contract additions — `ReleaseByCatch`, water fault codes, well-known input channels

**Files:**
- Modify: `packages/contracts/src/ice_contracts/board/dispenser_schema.py` (release union near l.245; `drive_channels`, `sense_channels`, `worst_case_seconds` helpers)
- Modify: `packages/contracts/src/ice_contracts/board/vending_machine.py` (`FaultCode`, `FAULT_TABLE`, `OUTCOME_FAULTS` l.346-357; new `VendingChannel`, `InputFault`, `INPUT_FAULTS`)
- Modify: `packages/edge/src/ice_edge/vend/dispensers.py` only if its drive/sense/budget checks do not already go through the schema helpers
- Modify: `packages/edge/src/ice_edge/faults/availability.py`, `packages/edge/src/ice_edge/hal/mqtt_inbound.py`: replace the existing `"bin_half_full"` literals with `VendingChannel.bin_half_full` (no behaviour change)
- Regenerate: `docs/contracts/vending-machine/schemas/*.json`, `dispensers.example.toml` (only if the generator's output changes from the new variant's documentation)
- Test: `tests/contracts/test_dispenser_schema.py`, `tests/contracts/test_contracts_vending.py`, `tests/edge/vend/test_dispensers_validation.py`

**Interfaces:**
- Produces: `ReleaseByCatch(proof: Literal["catch"], catch_channel: ChannelId, drop_seconds: float  # 0.5–30, unit "s")`; `ReleaseStep = Annotated[ReleaseBySensor | ReleaseTimed | ReleaseByCatch, Field(discriminator="proof")]`. `drive_channels(profile)` includes `catch_channel`; `worst_case_seconds(profile)` adds `drop_seconds`.
- Produces: `FaultCode.WTR_106 = "WTR-106"` (`warning`, `Scope.machine`, "RO storage tank empty"), `FaultCode.WTR_107 = "WTR-107"` (`warning`, `Scope.machine`, "RO production overran its maximum run time"). `OUTCOME_FAULTS[("water_fill", DispenserOutcome.bin_empty)] == FaultCode.WTR_106`.
- Changes: `FAULT_TABLE[WTR_105]` from `product_unavailable`/`Scope.product` to `warning`/`Scope.machine` (description unchanged). Nothing raises WTR-105 today (`grep -rn WTR_105 packages` finds only the table), so no caller changes.
- Docstring of `ReleaseByCatch` states §3.2's MUSTs (energize before agitate, hold through fill, de-energize to drop, leave off between vends, de-energize on fault).
- Produces (the single source for channel ids that carry edge semantics, so the simulator and the edge can never disagree on a string):
  ```python
  class VendingChannel(str, Enum):          # same enum style as FaultCode
      bin_half_full = "bin_half_full"
      sump_high = "sump_high"
      water_pressure_ok = "water_pressure_ok"
      ro_tank_empty = "ro_tank_empty"
      ro_tank_full = "ro_tank_full"
      ro_production = "ro_production"
      service_door = "service_door"

  @dataclass(frozen=True)
  class InputFault:
      channel: VendingChannel
      faulted_when: bool       # the reported state that means "faulted"
      code: FaultCode

  INPUT_FAULTS: tuple[InputFault, ...]   # sump_high/True/WTR_104, water_pressure_ok/False/WTR_105,
                                         # ro_tank_empty/True/WTR_106
  ```
  `ro_production`'s WTR-107 is a duration rule, not a state, so it is not in `INPUT_FAULTS` (Task 8 owns it). These are optional channels: a board that declares none of them is valid. Customer button ids are deliberately **not** here — they carry no edge meaning beyond the catalog mapping (Task 2).
- `CONTRACT.md`: list `VendingChannel` and `INPUT_FAULTS` in the section Task 2 writes.

- [ ] **Step 1: Write failing tests**
  - `ReleaseByCatch` parses from a `[slot.N.release]` table with `proof = "catch"`; rejects `drop_seconds` 0.4 and 30.1; rejects an extra field (strict model).
  - A bagged-ice profile with `ReleaseByCatch` lists `catch_channel` in `drive_channels`, and `worst_case_seconds` grows by exactly `drop_seconds` versus the same profile with `drop_seconds` reduced by 1.
  - `validate_document` reports a drive/sense conflict when `catch_channel` equals the fill `sensor_channel`.
  - With capabilities declaring `trap_door_catch` as an `input`, `validate_document` reports a direction error for it.
  - `FAULT_TABLE` rows for WTR-105/WTR-106/WTR-107 are all `warning`/`Scope.machine`; none is in `PAYMENT_BLOCKING_FAULTS`; `len(PAYMENT_BLOCKING_FAULTS) == 7`; water `bin_empty` maps to WTR-106 and bagged-ice `bin_empty` still maps to ICE-101.
  - Every `VendingChannel` value matches `CHANNEL_ID_PATTERN`; every `INPUT_FAULTS` code is `Scope.machine`; `INPUT_FAULTS` channels are unique.
- [ ] **Step 2: Run them; expect failures** — `uv run pytest tests/contracts tests/edge/vend -q`
- [ ] **Step 3: Implement** the variant, helper updates, codes, mapping and channel table. Update the existing assertion at `tests/contracts/test_contracts_vending.py:75` (water `bin_empty → ICE-101`). A water vend's `bin_empty` now raises a machine-scope warning and locks no product; update any VMC vend-failure test that expected that sku locked. `grep -rn '"bin_half_full"' packages/edge/src` prints nothing afterwards.
- [ ] **Step 4: Regenerate** schemas and the dispenser example; run the full suite — all pass (schema drift tests included).
- [ ] **Step 5: Commit** — `feat(contracts): ReleaseByCatch, WTR-106/WTR-107, well-known vending input channels`

---

### Task 2: Named buttons end to end (contract 2.0.0)

**Files:**
- Modify: `packages/contracts/src/ice_contracts/board/mqtt_messages.py:66-71` (`ButtonPress`)
- Modify: `packages/contracts/src/ice_contracts/board/vending_machine.py` (`CONTRACT_VERSION = "2.0.0"`)
- Modify: `docs/contracts/vending-machine/CONTRACT.md` (new "Semantics fixed in 2.0.0 (breaking)" section: §3.1–3.3 of the spec, plus Task 1's `VendingChannel`/`INPUT_FAULTS`)
- Modify: `packages/common/src/ice_common/config_model.py` (`Product.button`; `PhysicalDetails` unique-button validator beside `_check_unique_slots` l.176)
- Modify: `packages/edge/src/ice_edge/sale/vmc.py` (`on_button_press` l.272-276; `select_product` l.876-891; `begin_test_sale` l.819-820)
- Modify: `packages/edge/src/ice_edge/sale/sale_journal.py` (replace `selection_refused_bad_index` with `selection_refused_unknown_button`)
- Modify: `packages/sims/src/ice_sims/vending_machine.py` — **minimum needed to keep the simulator valid against the new `ButtonPress`, nothing more** (the customer loop moves to its own module in Task 9): add a module-level `BUTTON_CHANNELS = ("button_ice", "button_water_1gal", "button_water_5gal")`, declare each as a binary `input` in `_VENDING_CHANNELS`, and make `_customer_loop` press a random id from `BUTTON_CHANNELS` (the indecisive second press picks a different one). Drop `num_buttons`/`_pick_button` and the loop's catalog reload: a real board knows its buttons, not the edge's catalog, and a button the catalog does not map exercises the edge's unknown-button path (spec §5.2)
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
  - Simulator: one customer-loop press publishes a `ButtonPress` whose `button` is in `BUTTON_CHANNELS`, whatever the catalog holds (including an empty catalog); the indecisive second press names a different id.
  - The existing channel-list tests (`tests/sims/test_simulator_vending.py` l.491-563) gain the three `button_*` inputs; Task 10 rewrites them fully.
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

### Task 4: IM600XL dispenser profiles and fixtures

Moved ahead of the simulator work so that no simulator task has to keep removed channels alive for old fixtures.

**Files:**
- Modify: `packages/edge/src/ice_edge/vend/dispensers_doc.py` sample values (l.517-650)
- Regenerate: `dispensers.example.toml` (`uv run python scripts/gen_dispensers_example.py`)
- Modify: `tests/dispenser_fixtures.py` (add an IM600XL fixture set — `IM600XL_ICE`, `IM600XL_WATER_1GAL`, `IM600XL_WATER_5GAL` products and an `IM600XL_GOOD` TOML; keep the existing `GOOD`/`ICE`/`WATER` used by edge tests)
- Create: `tests/sims/vending_profiles.py` — simulator-local fixtures: the same three IM600XL profiles built directly as `ice_contracts.board.dispenser_schema` models (or parsed with `tomllib` + a `TypeAdapter(SlotProfile)`), plus a helper `dispense_command(slot) -> DispenseCommand`. Imports `ice_contracts` only — no `ice_edge`, no `tests.dispenser_fixtures`
- Test: `tests/edge/vend/test_dispensers_example.py`, `tests/edge/vend/test_dispensers_cli.py`, `tests/sims/test_vending_profiles.py` (new, small)

**Interfaces:**
- Sample (§6): slot 0 bagged ice — agitate `agitator_motor` (current `unmonitored`), `IceFillBySensor` on `auger_motor`/`bag_full_sensor`, `ReleaseByCatch` on `trap_door_catch`, accessory `bag_fan`; slots 1 and 2 water 1 gal and 5 gal — `WaterFillByVolume` on `water_valve_solenoid`/`fill_pulses`. Slots and skus agree with `config.example.json` (Task 2).
- `tests/sims/vending_profiles.py` and `IM600XL_GOOD` describe the same three profiles; a test asserts they are equal after parsing, so the two fixture homes cannot drift.

- [ ] **Step 1: Write failing tests** — the generated example validates clean (`ValidationReport.ok`) against `config.example.json`'s catalog; `IM600XL_GOOD` validates clean against the IM600XL products; the simulator-local profiles equal the parsed `IM600XL_GOOD` slots. (The cross-check against the simulator's capabilities waits for Task 10 and lives in `tests/integration/`, Task 12.)
- [ ] **Step 2: Run; expect failure.**
- [ ] **Step 3: Update the sample and fixtures; regenerate the example.**
- [ ] **Step 4: Full suite green** (byte-drift test included). Also run `uv run python -m ice_edge.vend.dispensers --example` and confirm it prints the new example.
- [ ] **Step 5: Commit** — `feat(dispensers): IM600XL sample profiles and fixtures`

---

### Task 5: `TelemetryRouter` subscriber lists (refactor, no behaviour change)

Lets new consumers of vending capabilities and hardware IO attach without growing a closure in `Machine` or a callback parameter per consumer.

**Files:**
- Modify: `packages/edge/src/ice_edge/hal/mqtt_inbound.py` (`TelemetryRouter.__init__` l.74-87, `handle_hardware_io` l.89, the capabilities handler)
- Modify: `packages/edge/src/ice_edge/app/machine.py` (~l.173: router construction)
- Test: `tests/edge/hal/test_mqtt_inbound.py`, `tests/edge/app/test_machine.py`

**Interfaces:**
- Replaces the `on_capabilities_validated` constructor parameter with `TelemetryRouter.subscribe_capabilities(cb: Callable[[str, SubsystemCapabilities], None]) -> Callable[[], None]` (returns unsubscribe), same shape as `VMC.subscribe_state_change`.
- Adds `TelemetryRouter.subscribe_hardware_io(cb: Callable[[str, bool], None]) -> Callable[[], None]`; `handle_hardware_io` calls every subscriber with `(device, state)` for each validated message, after the existing availability/health handling.
- Subscribers run in subscription order; a raising subscriber is logged and never stops the rest (same rule as the VMC observers). `on_bin_half_full` stays as it is. (Task 6 adds replay-on-subscribe for capabilities.)
- `Machine` subscribes `self._gate.on_vending_capabilities` at construction (today's only consumer).

- [ ] **Step 1: Write failing tests** — two capability subscribers both receive a validated message, in order; a raising first subscriber does not stop the second; unsubscribe stops delivery; a hardware-io subscriber receives `(device, state)` once per message; an invalid payload reaches no subscriber.
- [ ] **Step 2: Run; expect failures.**
- [ ] **Step 3: Implement.** Existing gate-reconcile-on-capabilities tests pass unmodified.
- [ ] **Step 4: Full suite green.**
- [ ] **Step 5: Commit** — `refactor(hal): subscriber lists for capabilities and hardware IO`

---

### Task 6: Availability without a bin sensor, plus RO/pressure/sump rows

**Files:**
- Modify: `packages/edge/src/ice_edge/faults/availability.py` (rows l.131-165, `set_hardware_io` l.232, `_refresh_ice_available` l.243)
- Modify: `packages/edge/src/ice_edge/app/machine.py` (`set_availability`: subscribe availability to capabilities)
- Test: `tests/edge/faults/test_availability.py`, `tests/edge/app/test_machine.py`

**Interfaces:**
- Produces: `Availability.set_bin_instrumented(instrumented: bool) -> None` and read-only property `Availability.bin_instrumented -> bool` (default `True`, preserving today's behaviour until a vending capabilities doc says otherwise). When `False`, the `ice_available` row reads not-instrumented/PASS with detail `"no bin sensor"`; `bin_half_full` reports are then ignored for that row.
- Produces: `Availability.on_vending_capabilities(subsystem: str, caps: SubsystemCapabilities) -> None` — for `subsystem == "vending"`, calls `set_bin_instrumented(any(c.channel_id == VendingChannel.bin_half_full for c in caps.channels))`.
- Produces rows: new `ro_tank_ok` (`Applies.water`, `Gate.fulfillment`, starts not-instrumented); `water_pressure_ok` and `no_leak` keep their names/applies/gates but become instrumented on their first `hardware/io` report. `set_hardware_io` keys on `VendingChannel` values: `ro_tank_empty` → `ro_tank_ok` fails while true ("RO tank empty"); `water_pressure_ok` → row fails while false ("low water pressure"); `sump_high` → `no_leak` fails while true ("sump high").
- `Machine.set_availability(a)` calls `self._telemetry.subscribe_capabilities(a.on_vending_capabilities)` (keeping the unsubscribe so a replaced availability is unsubscribed first). No combined closure.
- **Late-subscriber replay.** `capabilities/vending` is retained, so it can be delivered before a subscriber attaches; a missed doc would leave `bin_instrumented=True` and ice UNKNOWN-blocked forever on a machine with no bin sensor. `subscribe_capabilities` therefore immediately calls the new subscriber once per already-validated document the router holds (the same store behind `machine.subsystem_capabilities`). Today `__main__.py` attaches availability (l.85) before `mqtt.run` starts (l.168), so this guards tests, replacement and future reordering rather than a live bug.

- [ ] **Step 1: Write failing tests**
  - Fresh `Availability`: `ice_available` still UNKNOWN (today's behaviour).
  - After `on_vending_capabilities("vending", caps_without_bin)`: `sale_available("ice")` passes with all other rows passing, and the row detail is `"no bin sensor"`; a later `set_hardware_io("bin_half_full", False)` does not fail it.
  - With caps declaring `bin_half_full`: behaviour identical to today (false → fails).
  - `ro_tank_empty=True` fails water and not ice and does not change `payment_enabled()`; `water_pressure_ok=False` likewise; `sump_high=True` fails water and makes `payment_enabled()` false (safety); each recovers on the opposite state.
  - Before any report, the three rows are not-instrumented and pass.
  - Machine-level: a validated `capabilities/vending` message reaches both the dispenser gate and availability; calling `set_availability` twice leaves one availability subscribed.
  - Order: deliver a no-bin `capabilities/vending` message **first**, attach availability **second** → `bin_instrumented` is `False` and ice is sellable.
- [ ] **Step 2: Run; expect failures.**
- [ ] **Step 3: Implement.**
- [ ] **Step 4: Full suite green.**
- [ ] **Step 5: Commit** — `feat(availability): run without a bin sensor; RO tank, pressure and sump rows`

---

### Task 7: ICE-101 auto-retry cooldown (`IceRetry`)

The retry policy is its own collaborator; `FaultService` gains only a generic observer hook.

**Files:**
- Modify: `packages/common/src/ice_common/config_model.py` (`physical.ice_retry_cooldown_seconds`)
- Modify: `packages/edge/src/ice_edge/faults/fault_service.py` (`subscribe_fault_change`; `clear_ice101_lockouts` l.239 gains `by`)
- Create: `packages/edge/src/ice_edge/faults/ice_retry.py`
- Modify: `packages/edge/src/ice_edge/app/machine.py` (build `IceRetry`, subscribe it)
- Test: `tests/edge/faults/test_fault_service.py`, `tests/edge/faults/test_ice_retry.py` (new), using `machine_fake_time`

**Interfaces:**
- Produces: `FaultService.subscribe_fault_change(cb: Callable[[FaultChange], None]) -> Callable[[], None]`, where `FaultChange` is a frozen dataclass `(action: Literal["raised", "cleared"], code: FaultCode, sku: str | None, by: str | None)`. Called after a raise or clear has taken effect and its existing side effects ran; same ordering/raising-subscriber rules as the VMC observers. Generic — no ICE-101 knowledge in `FaultService`.
- Changes: `clear_ice101_lockouts(by: str = "auto")` (today's hard-coded value, so the `bin_half_full` path records exactly what it does now), passed through to `clear_fault`.
- Produces:
  ```python
  class IceRetry:
      def __init__(self, *, faults: FaultService, tasks: TaskRunner,
                   bin_instrumented: Callable[[], bool],
                   cooldown_seconds: Callable[[], float]) -> None: ...
      def on_fault_change(self, change: FaultChange) -> None: ...
      @property
      def pending(self) -> bool: ...   # read-only, for tests
  ```
- Behaviour: an `ICE-101` `raised` change while `not bin_instrumented()` arms (or re-arms, cancelling the previous handle) `tasks.schedule(cooldown_seconds(), ..., label="ice_retry_cooldown")`. On fire: `faults.clear_ice101_lockouts(by="cooldown")`. An `ICE-101` `cleared` change that leaves no ICE-101 lockout (`faults.lockouts`) cancels the timer — including the cooldown's own clear, harmlessly. No shutdown hook: `TaskRunner.cancel_pending` (called by `Machine.cancel_pending_tasks`) already cancels every `schedule()` task.
- `Machine` builds it with `bin_instrumented=lambda: (a.bin_instrumented if (a := self._outputs.availability) else True)` and `cooldown_seconds=lambda: self._config.physical.ice_retry_cooldown_seconds`, then `self._faults.subscribe_fault_change(self._ice_retry.on_fault_change)`.
- Imports: `ice_contracts`, `ice_common`, `ice_edge.faults`, `ice_edge.supervisor` only.

- [ ] **Step 1: Write failing tests**
  - `FaultService`: a subscriber sees `raised` then `cleared` for a product lockout and for a machine fault, with the right `code`/`sku`/`by`; a raising subscriber does not break the raise.
  - `IceRetry` (on `machine_fake_time`, availability attached, bin not instrumented): raising ICE-101 for the ice sku arms `ice_retry_cooldown`; `runner.fire("ice_retry_cooldown")` clears the lockout and records `lockout_cleared` with `by="cooldown"`.
  - Raising again after it fired re-arms; raising twice before it fires leaves exactly one pending `ice_retry_cooldown`.
  - Operator `clear_fault` on the ICE-101 key cancels the pending timer.
  - With the bin instrumented, raising ICE-101 arms nothing (today's `bin_half_full` auto-clear still works and records `by="auto"`).
  - Config: default 1800; 59 and 86401 rejected.
- [ ] **Step 2: Run; expect failures.**
- [ ] **Step 3: Implement.**
- [ ] **Step 4: Full suite green; `uv run lint-imports` passes.**
- [ ] **Step 5: Commit** — `feat(faults): ICE-101 clears after a cooldown when the bin is not instrumented`

---

### Task 8: `InputFaults` — water faults from `hardware/io`

**Files:**
- Create: `packages/edge/src/ice_edge/faults/input_faults.py`
- Modify: `packages/common/src/ice_common/config_model.py` (`physical.ro_max_run_seconds`)
- Modify: `packages/edge/src/ice_edge/app/machine.py` (build `InputFaults`; `self._telemetry.subscribe_hardware_io(self._input_faults.on_hardware_io)`; its timer is `self._tasks.schedule(..., label="ro_overrun")`, so `TaskRunner.cancel_pending` already covers shutdown)
- Test: `tests/edge/faults/test_input_faults.py` (new), `tests/edge/app/test_machine.py`

**Interfaces:**
- Produces:
  ```python
  class InputFaults:
      def __init__(self, *, faults: FaultService, tasks: TaskRunner,
                   ro_max_run_seconds: Callable[[], float]) -> None: ...
      def on_hardware_io(self, device: str, state: bool) -> None: ...
      def cancel(self) -> None: ...   # drops the ro_overrun timer
  ```
- State faults are **driven by `INPUT_FAULTS`** (Task 1), not a local mapping: for the entry whose `channel == device`, `state == faulted_when` → `raise_fault(code)` (no `sku`), otherwise clear it. A device not in the table and not `ro_production`/`ro_tank_full` is ignored. `ro_production` (`VendingChannel.ro_production`) true → arm `ro_overrun` for `ro_max_run_seconds()` (re-arming cancels the previous handle); on fire → `raise_fault(WTR_107)`; false → cancel a pending timer but **leave an active `WTR_107` raised**. `ro_tank_full` true → clear `WTR_107` if active. Rationale: the board stops the pump at its own firmware limit (§5.2), so pump-off is exactly when an overrun is confirmed, not resolved; only the tank reaching full (or an operator) proves the RO path works again. The edge limit must be shorter than the board's cutoff for the alert to fire at all; that ordering is a deployment rule (documented in Task 12), and the simulator honours it (Task 11). Clears use `clear_fault(code.value, by="hardware")` (the machine-fault key `FaultService.parse_key` accepts) and are skipped when `not faults.has(code)`; raising an already-active fault is not repeated (check `faults.has(code)` first) so a re-published retained state does not spam alerts. Recovery of one input never clears another input's code.
- Imports: `ice_contracts`, `ice_common`, `ice_edge.faults`, `ice_edge.supervisor` only (layer rule).

- [ ] **Step 1: Write failing tests**
  - Parametrized over `INPUT_FAULTS`: each input raises its machine-scope code and the opposite state clears it; `faults.lockouts` is unchanged throughout. WTR-104 makes `payment_enabled()` false (via availability's active-fault push); WTR-105/WTR-106 leave it true.
  - Pressure recovery does not clear a WTR-106 held by an empty tank (independent machine codes).
  - A water sku already locked by `WTR-101` (a vend failure) is still locked by `WTR-101` after `water_pressure_ok` false then true.
  - Repeating the same failing state does not raise a second alert (count `publish_alert` calls or recorder rows).
  - `ro_production` true then `runner.fire("ro_overrun")` raises WTR-107; a following `ro_production` false leaves WTR-107 active; `ro_tank_full` true then clears it. `ro_production` false before the timer fires cancels it and raises nothing; an `ro_production` true/false/true sequence leaves one pending timer; `ro_tank_full` true with no WTR-107 active records nothing.
  - An unknown device is ignored.
  - Machine-level: a `hardware/io/sump_high` message reaches `on_hardware_io` once.
  - Config: `ro_max_run_seconds` default 3600; 59 and 86401 rejected.
- [ ] **Step 2: Run; expect failures.**
- [ ] **Step 3: Implement.**
- [ ] **Step 4: Full suite green; `uv run lint-imports` passes.**
- [ ] **Step 5: Commit** — `feat(faults): derive WTR-104..107 from water and RO inputs`

---

### Task 9: Simulator — split `vending_machine.py` into isolated modules (refactor, no behaviour change)

Today one 1309-line class holds the channel table, fault registrations, profile execution, the sensor loop, the customer loop and HA discovery. Tasks 10 and 11 would otherwise add the RO tank, climate, new inputs, the catch release and five fault injections to it. This task moves code only; every existing simulator test passes with at most import-path and patch-target changes.

**Depends on Task 2** (the `BUTTON_CHANNELS` snapshot below only exists once Task 2 has landed).

**Files:**
- Create: `packages/sims/src/ice_sims/vending/__init__.py` (re-exports `VendingMachineSimulator`)
- Create: `packages/sims/src/ice_sims/vending/channels.py` — the one declarative channel table. Each row: id, kind, direction, `driven_by`, idle default (binary only), and a `role` (`"button"`, `"sensor"`, `"output"`…). Derives `CHANNELS` (the `ChannelDescriptor` list), `HARDWARE_DEVICES` (binary ids → idle default) and `BUTTON_CHANNELS` (ids with role `"button"`) from it; ids with edge meaning use `VendingChannel` values. No MQTT, no I/O.
- Create: `packages/sims/src/ice_sims/vending/dispense.py` — `execute_profile` and the per-mechanism runners (`_run_bagged_ice`, `_run_water_fill`, `_pump_water_pulses`, `_publish_fill_pulses`, accessory span/lag handling, `_RunContext`). Talks to the board through a narrow port, not the simulator class. The port is everything the dispense code touches on `self` today (`vending_machine.py:670-1196`: `set_hw`, `publish`, `_publish_current`, `sleep`, `_spawn_background`, `active_fault_names`, `subsystem_name`):
  ```python
  class BoardPort(Protocol):
      subsystem_name: str
      async def set_hw(self, device: str, state: bool) -> None: ...
      async def publish(self, topic_suffix: str, payload: BaseModel) -> None: ...
      async def publish_current(self, channel: str, amps: float) -> None: ...  # removed in Task 10
      async def sleep(self, seconds: float) -> None: ...
      def spawn(self, coro: Coroutine[Any, Any, None]) -> asyncio.Task: ...   # sim._spawn_background
      def active_faults(self) -> set[str]: ...
  ```
  The simulator does **not** implement this itself: `ESP32Simulator.publish(client, topic_suffix, payload)` (`base.py:244`) and `set_hw(client, device, state)` are called with a client by the heartbeat, capabilities, alert and fault-callback paths, so their signatures stay. Instead a small adapter in `simulator.py`, `_Board(sim, client)`, implements `BoardPort` by forwarding, and the dispense and customer handlers build one per call. `_Board` is **stateless**.
- **Cross-run state lives on a long-lived object, never on `_Board` or `_RunContext`:**
  ```python
  class Dispenser:
      def __init__(self) -> None: ...         # owns pending_lag_off: dict[str, asyncio.Task]
      async def execute_profile(self, board: BoardPort, cmd: DispenseCommand, ...) -> ...: ...
  ```
  The simulator builds **one** `Dispenser` in `__init__` and passes a fresh `_Board` to each call. `_pending_lag_off` moves onto it unchanged: the Copilot C4 fix (PR #32) depends on a new run finding and cancelling a *previous* run's lag-off for the same channel, so the dict must outlive any single `execute_profile` call. A test pins this: two back-to-back runs on the same `Dispenser`, each through its own `_Board`, where the second run's accessory is not switched off by the first run's lag.
- Create: `packages/sims/src/ice_sims/vending/customer.py` — the customer loop and arrival-rate helpers (`_arrival_factor`, `_compute_idle_time`); takes the button ids and a `BoardPort`, reads nothing else.
- Create: `packages/sims/src/ice_sims/vending/faults.py` — `vending_fault_defs(sim) -> list[FaultDef]`: every `FaultDef` and its activate/recover callbacks, **moved unchanged** (same bodies, same `sim.set_hw(client, ...)` calls). Callbacks that act on model state arrive with the models in Task 11, not here.
- Create: `packages/sims/src/ice_sims/vending/ha.py` — `ha_discovery_entities(channels) -> list[dict]` (today's hard-coded entities unchanged in this task).
- Create: `packages/sims/src/ice_sims/vending/simulator.py` — `VendingMachineSimulator(ESP32Simulator)` plus the `_Board` adapter: composes the above, registers commands and faults, runs the loops. Owns publishing and the `_hw` dict; holds no sequencing or physics logic itself.
- Modify: `packages/sims/src/ice_sims/vending_machine.py` → thin entry module: re-exports `VendingMachineSimulator`, `HARDWARE_DEVICES`, `SENSOR_PUBLISH_INTERVAL` and keeps `if __name__ == "__main__": ESP32Simulator.entry_point(VendingMachineSimulator)`, so `docker-compose.yml:96`, the CI smoke import (`ci.yml:146`), `ice_sims/__main__.py` and `tests/edge/web/test_routes_tests.py:1338` keep working unchanged.
- Create: `tests/async_helpers.py` — move `settle`, `patch_module_sleep` **and the `_AsyncioWithSleep` proxy it builds** (`tests/fakes.py:297`) here from `tests/fakes.py`. `tests/fakes.py` imports `ice_edge.app.machine` (l.42) and `tests.dispenser_fixtures` (l.44, which imports `ice_edge.vend.dispensers`), so any simulator test that imports `tests.fakes` pulls in edge code. `tests/fakes.py` re-exports all three, so edge tests are untouched. Imports: stdlib only.
- Create: `tests/repo_root.py` — move `_repo_root`, `REPO_ROOT` and `TESTS_ROOT` here from `tests/paths.py`. `tests/paths.py` imports `ice_edge.sale.vmc` and `ice_edge.web.routes` (l.7-8) and re-exports both names, so its other importers are untouched. Imports: `pathlib` only.
- Modify: every simulator test that reaches edge code through a helper — `tests/sims/test_simulator_base.py:17` (`settle`) and `tests/sims/test_simulator_mdb.py:8` (`patch_module_sleep`) import from `tests.async_helpers`; `tests/sims/test_simulator_vending.py` l.30 imports from `tests.async_helpers` and l.31 imports `REPO_ROOT` from `tests.repo_root`. After this task, `tests/sims/test_simulator_vending.py` reaches `ice_edge` only through its own l.26 and l.29 imports, which Task 10 removes.
- Create: `tests/meta/test_test_isolation.py` — parses every `tests/sims/**/*.py` with `ast`, follows `tests.*` imports transitively, and fails if any reachable module imports `ice_edge`; the same walk over `tests/edge/**` fails on `ice_sims`. Allowlist (shrink-only, like the private-access budget): `tests/sims/test_simulator_vending.py` until Task 10 and `tests/edge/web/test_routes_tests.py` until Task 12. The walk follows import statements only. `tests/conftest.py` still imports `ice_edge` at run time for every test, and that is out of scope: the rule is that no simulator test *names* edge code, not that edge code is never loaded.
- Modify: `tests/private_access_budget.py` — moving tests out of `tests/sims/test_simulator_vending.py` (budget 53) lowers its count, and the ratchet fails under budget as well as over. Set each touched file to its new count from `uv run python tests/meta/test_no_private_access.py`. A new file starts at an implicit budget of 0, so a moved test that reaches `sim._x` must either switch to a public seam (`sim.sleep`, `sim.active_fault_names`, the `Dispenser`) or carry a `# private: <reason>` marker. Do not add a new file to `BUDGET` with a non-zero count.
- Test: `tests/sims/test_simulator_vending.py`, `test_simulator_base.py`, `test_simulator_mdb.py` (patch targets and helper imports only); new `tests/sims/test_vending_channels.py`, `tests/sims/test_vending_dispense.py` (moved/split tests as convenient)

**Interfaces:**
- Patch points. Every test that patches a module global of `ice_sims.vending_machine` must move, because after the split the thin entry module calls nothing, so the patch applies to nothing. It does not fail cleanly: a real `asyncio.sleep` (the 10 s sensor loop, the 30–90 s customer idle) hangs the test, and a patched `logger` makes the assertion vacuous. The full list in `tests/sims/test_simulator_vending.py`: `patch_module_sleep("ice_sims.vending_machine", …)` at l.374, 394, 428, 1351, 1371, and `patch("ice_sims.vending_machine.logger")` at l.395. Retarget each one to the module that now makes the call (`ice_sims.vending.simulator` for the sensor loop and `_handle_water_valve`, `ice_sims.vending.customer` for the customer loop), or switch it to `sim.sleep`. Afterwards, `grep -rn '"ice_sims.vending_machine' tests` prints only import lines.
- `dispense.py`, `customer.py`, `channels.py`, `ha.py` do not import `simulator.py` (one direction only: `simulator → others`). Add a test that asserts this with `ast` over the package, like `tests/meta/test_routes_shape.py` does for routes.

- [ ] **Step 1: Write the structural tests** — `channels.CHANNELS`, `HARDWARE_DEVICES` and `BUTTON_CHANNELS` equal today's values (snapshot them from the current module before moving); the in-package import-direction test; the test-isolation meta test (with its two-entry allowlist); `Dispenser.execute_profile` runs a profile against a fake `BoardPort` (a list recorder, a no-op sleep and a `spawn` that records tasks) and yields the same `set_hw`/publish sequence the class-level test sees today; the simulator's `dispense` command handler drives its one `Dispenser` through a fresh `_Board`; the back-to-back lag-off test above; the private-access ratchet passes with the updated budgets.
- [ ] **Step 2: Move the code.** No behaviour, timing or message change. `git diff --stat` should be dominated by moves.
- [ ] **Step 3: Full suite green**; `uv run python -c "import ice_sims.vending_machine"` exits 0; `uv run python -m ice_sims.vending_machine --help` still prints usage.
- [ ] **Step 4: Commit** — `refactor(sims): split the vending simulator into channel, dispense, customer, fault and HA modules`

---

### Task 10: Simulator — IM600XL channels, vends and Home Assistant

**Files:**
- Modify: `packages/sims/src/ice_sims/vending/channels.py` (the table, spec §5.1)
- Modify: `packages/sims/src/ice_sims/vending/dispense.py` (`ReleaseByCatch`; declared-channel enforcement; water fill drops the flow-sensor publish; current sense unmonitored)
- Modify: `packages/sims/src/ice_sims/vending/faults.py` (remove `door_stuck_open`, `bag_drop_solenoid_stuck`; `ice_bin_empty` reworked)
- Modify: `packages/sims/src/ice_sims/vending/ha.py` (entities derived from the table)
- Modify: `packages/sims/src/ice_sims/vending/simulator.py` (`_handle_water_valve` drives the declared valve channel only)
- Test: `tests/sims/test_simulator_vending.py`, `tests/sims/test_vending_channels.py`, `tests/sims/test_vending_dispense.py` — switch every profile to `tests/sims/vending_profiles.py` (Task 4) and **remove the `ice_edge.vend.dispensers` and `tests.dispenser_fixtures` imports** (`test_simulator_vending.py` l.26, l.29)

**Interfaces:**
- Channel set, kinds and `driven_by` exactly as spec §5.1 (inputs, outputs, counter `fill_pulses`, temperature `ambient`). `HARDWARE_DEVICES` holds every binary channel with its idle default: `water_pressure_ok=True`, everything else `False`. All derived from the one table.
- **Declared-channel enforcement** (a real board cannot drive a channel it does not have): before any motion, `execute_profile` checks `drive_channels(profile)` ⊆ declared outputs and `sense_channels(profile)` ⊆ declared inputs/counters. On a miss it publishes a terminal `error` `DispenserStatus` naming the channel, drives nothing, and the command completes as failed. The edge's capability cross-check (`CFG-101`) stops such a profile from ever being sent, so this path is defensive and never seen in normal flow.
- Bagged ice honours all three release variants; for `ReleaseByCatch` the catch channel is set `True` before the agitate step and `False` at release, then the step waits `drop_seconds`; any failure path sets it `False`. Current sense is `unmonitored` (no current telemetry published), so `BoardPort.publish_current` and its `_Board` forward are deleted.
- Water fill counts `fill_pulses` against the profile target; it no longer publishes a binary flow-sensor `hardware/io` message.
- Fault injection: remove `door_stuck_open` and `bag_drop_solenoid_stuck`; `ice_bin_empty` makes the fill run to its max time and report `bin_empty`, touching no bin channel.
- `ha.ha_discovery_entities(channels)` derives binary entities from the binary rows and a temperature entity from `ambient`.

- [ ] **Step 1: Write failing tests**
  - Ordered channel-id list, output set, `driven_by` map and `HARDWARE_DEVICES` keys equal the §5.1 set; none of the removed ids appear; every `VendingChannel` the IM600XL has is declared under that exact id.
  - A `ReleaseByCatch` ice vend publishes `trap_door_catch=True` before the first `agitator_motor=True`, `trap_door_catch=False` after `bag_full_sensor=True`, and ends `complete`; an injected `auger_jam` ends with the catch `False`.
  - A water vend for the 5 gal profile reports `fill_pulses` reaching the target and `complete`; no `hardware/io/water_flow_sensor` is published.
  - A profile naming an undeclared channel (`door_sensor`) ends `error` with no `set_hw` call; a profile driving a declared *input* ends `error`.
  - `ice_bin_empty` yields `bin_empty` without any `bin_half_full` publish.
  - HA entity object ids equal the binary channel ids plus `ambient`.
  - Remove `tests/sims/test_simulator_vending.py` from the test-isolation allowlist; the meta test passes.
  - Before implementing, `grep -rn "build_capabilities()" tests packages` outside `tests/sims`: any caller that validates profiles against the vending simulator's channels must move to the IM600XL fixtures here (today only `tests/edge/web/test_routes_tests.py:1342`, which reads `.commands` and is unaffected).
- [ ] **Step 2: Run; expect failures.**
- [ ] **Step 3: Implement.**
- [ ] **Step 4: Full suite green** — if `tests/integration/test_integration_e2e.py` depended on `bin_half_full`, Task 6's capability path must now make ice sellable; fix the test's expectations, not the edge.
- [ ] **Step 5: Commit** — `feat(sims): vending simulator declares the IM600XL channel set`

---

### Task 11: Simulator — RO tank, climate, inputs and new fault injections

**Files:**
- Create: `packages/sims/src/ice_sims/vending/ro_tank.py` (pure model)
- Create: `packages/sims/src/ice_sims/vending/climate.py` (pure model; replaces the cabinet-temperature/heater code in the sensor loop)
- Modify: `packages/sims/src/ice_sims/vending/simulator.py` (sensor loop: tick the models, publish their changes; the static inputs)
- Modify: `packages/sims/src/ice_sims/vending/dispense.py` (a completed water vend calls `await board.draw_water(gallons)`, a new `BoardPort` method; it never touches tank state). `_Board.draw_water` calls `sim.ro_tank.draw(gallons)` and publishes the returned changes on `hardware/io/<id>`.
- Modify: `packages/sims/src/ice_sims/vending/faults.py` (five new injections)
- Test: `tests/sims/test_vending_ro_tank.py`, `tests/sims/test_vending_climate.py` (new, synchronous), `tests/sims/test_simulator_vending.py`

**Interfaces:**
- Pure models — no MQTT, no `sleep`, no `_hw`; each returns the state changes for the simulator to publish:
  ```python
  @dataclass
  class RoTank:
      capacity_gal: float; full_gal: float; empty_gal: float
      fill_gal_per_tick: float; max_run_ticks: int
      level_gal: float
      fill_blocked: bool = False      # ro_tank_empty injection
      fill_stuck: bool = False        # ro_fill_stuck injection: pump on, level does not rise
      cutoff_latched: bool = False    # firmware max-run cutoff tripped; pump held off
      def tick(self) -> dict[str, bool]: ...   # ro_production / ro_tank_full / ro_tank_empty that changed
      def reset_cutoff(self) -> None: ...      # clears the latch and the run-tick count
      def draw(self, gallons: float) -> dict[str, bool]: ...
      def outputs(self) -> dict[str, bool]: ... # current values, for the periodic re-publish

  @dataclass
  class Climate:
      ambient_c: float; heater_on_below_c: float = 5.0; heater_off_above_c: float = 10.0
      def tick(self, drift_c: float) -> dict[str, bool]: ...   # heater_power if it changed
  ```
  `RoTank.tick`: `ro_production` on while below full and not `cutoff_latched`, off otherwise; `ro_tank_full`/`ro_tank_empty` follow the level. Run ticks count consecutive ticks with the pump on and reset whenever it turns off. When the count reaches `max_run_ticks` the pump turns off and `cutoff_latched` is set (a field, default `False`). A latched pump stays off on every later `tick`, even below full, which is what a stuck fill that never reached full needs. The latch clears only through `reset_cutoff()`. The simulator calls `reset_cutoff()` when the `ro_fill_stuck` injection recovers, standing in for a technician's power cycle. `draw` never clears it.
  Limit ordering: the default `max_run_ticks × SENSOR_PUBLISH_INTERVAL` is **longer** than the edge's default `ro_max_run_seconds` (3600 s), e.g. 400 ticks × 10 s = 4000 s, so the edge's `ro_overrun` fires before the board's cutoff (Task 8). A test asserts `max_run_ticks * SENSOR_PUBLISH_INTERVAL > PhysicalDetails().ro_max_run_seconds`. That test imports `ice_common` only, which simulator tests are allowed to do. Keys are `VendingChannel` values. The simulator passes `random.gauss(0, 0.3)` as `drift_c`, so `Climate` itself is deterministic.
- Simulator: each sensor tick publishes the models' changes on `hardware/io/<id>`, `ambient` on `sensors/temp/ambient`, and re-publishes `water_pressure_ok`, `sump_high`, `service_door` and the RO outputs on the periodic re-publish today's code does for retained state.
- New fault injections in `faults.py` (same registration pattern as existing ones), each flipping model or input state and recovering it: `ro_tank_empty` (level to 0, `fill_blocked`), `low_pressure` (`water_pressure_ok=False`), `sump_high`, `service_door_open`, `ro_fill_stuck` (`fill_stuck`; recovery clears it and calls `reset_cutoff()`. The edge's `ro_overrun` raises WTR-107 before the board's cutoff, and WTR-107 stays active until `ro_tank_full`).

- [ ] **Step 1: Write failing tests**
  - `RoTank` (plain sync tests): from empty, ticking turns `ro_production` on, then `ro_tank_full` true and `ro_production` off; `draw` past empty sets `ro_tank_empty` true; with small `max_run_ticks` and `fill_stuck`, `ro_production` turns off at the limit and **stays off** on further ticks below full; `draw` does not restart it; `reset_cutoff()` does; the default limit-ordering assertion above; `tick` returns only changed keys.
  - `Climate`: drift below 5 °C turns `heater_power` on; above 10 °C off; between, no change.
  - Simulator: a completed water vend lowers the tank by the profile volume; each new fault's activate/recover publishes the expected `hardware/io` states.
- [ ] **Step 2: Run; expect failures.**
- [ ] **Step 3: Implement.**
- [ ] **Step 4: Full suite green.**
- [ ] **Step 5: Commit** — `feat(sims): RO tank and climate models, water and door inputs`

---

### Task 12: Cross-checks, end-to-end and documentation

**Files:**
- Create: `tests/integration/test_im600xl_capabilities.py` — the one place simulator and edge code meet (no broker needed, so it never skips)
- Move: the simulator-backed test in `tests/edge/web/test_routes_tests.py` (l.1326-1350, which builds all three simulators' `build_capabilities().commands`) into `tests/integration/test_simulator_commands.py`; remove the last test-isolation allowlist entry
- Modify: `tests/integration/test_integration_e2e.py` (ice and water sale with the IM600XL capabilities)
- Modify: `CLAUDE.md` — vending contract 2.0.0 (Fault Codes section, including `WTR-105` re-scoped to machine/`warning` and the rule that input-driven faults never lock a product), `VendingChannel`/`INPUT_FAULTS`, timer labels now eight (`ice_retry_cooldown`, `ro_overrun`), `warn_if_no_buttons`, `Product.button` and named `ButtonPress`, WTR-107 semantics (raised by the edge's `ro_overrun`, survives pump-off, cleared by `ro_tank_full` or an operator) and the deployment rule that firmware's RO cutoff must be longer than `physical.ro_max_run_seconds`, `IceRetry`, `InputFaults`, `FaultService.subscribe_fault_change`, `TelemetryRouter.subscribe_capabilities`/`subscribe_hardware_io`, `Availability.bin_instrumented`, two new `physical` config fields, the `ice_sims.vending` module split and simulator channel set, and the isolation rule that simulator tests never import `ice_edge`
- Modify: `docs/hardware/im600xl-io-list.md` §7 — mark each divergence resolved, linking the spec; in the RO fill row (§5), state that the firmware cutoff must be longer than the edge's `ro_max_run_seconds` and latches until power cycle
- Modify: `ROADMAP.md` §3 — `water_pressure_ok`, `no_leak` (sump), RO tank now instrumented; bin level not instrumented on this machine
- Modify: `docs/OVERVIEW.md` — link the spec under Simulators

**Interfaces:** none new.

- [ ] **Step 1: Write the cross-check** (`test_im600xl_capabilities.py`): `VendingMachineSimulator(machine_id=...).build_capabilities()` validates `dispensers.example.toml` and `IM600XL_GOOD` clean (`validate_document(..., capabilities=...)` → `ok`) against `config.example.json`'s catalog; every `INPUT_FAULTS` channel and `VendingChannel.ro_production` is declared by the simulator; `bin_half_full` is not.
- [ ] **Step 2: Write the e2e tests** in the **existing harness**, which runs a real broker and the edge but no simulator: each test publishes the board's messages itself (`hardware/buttons`, `hardware/dispenser`, `hardware/io/...`), as the current tests do. Do not build a simulator-in-the-loop harness here. To stand in for the IM600XL board, publish `VendingMachineSimulator(machine_id=...).build_capabilities()` on `capabilities/vending` before the sale. Use `_wire_dispenser_runtime` with the IM600XL fixture set from Task 4, so profiles validate against those capabilities with no `CFG-101`. Tests:
  - Credit, then `ButtonPress(button="button_ice")`, then a `complete` `hardware/dispenser` report → one ice sale recorded, and no `hardware/io/bin_half_full` was ever published in that test.
  - Credit, then `button_water_1gal`, then `complete` → one water sale.
  - `hardware/io/sump_high` true → `payment_enabled()` false and `WTR-104` active; false → payment re-enabled and the fault cleared.
  - `hardware/io/water_pressure_ok` false → water refused, ice still sellable, `faults.lockouts` unchanged; true → water sellable again.
- [ ] **Step 3: Run them** (`uv run pytest tests/integration -q`). The e2e tests skip without a broker on `localhost:1883`; a local skip is not a pass. Confirm on CI's integration job, where a skip fails the build. The cross-check must pass locally. Fix only test-harness issues; any product defect goes back to its task.
- [ ] **Step 4: Update the docs.**
- [ ] **Step 5: Full verification** — `uv run pytest`, `ruff check .`, `ruff format --check .`, `uv run lint-imports`, `uv run python scripts/check_package_isolation.py`, `uv run python -m ice_contracts.generate` leaves no diff, `tests/meta/test_test_isolation.py` passes with an empty allowlist.
- [ ] **Step 6: Commit** — `docs: IM600XL simulator — contract 2.0.0, inputs, timers, simulator modules`
