# Subsystem Windows Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** `/health/subsystems/{name}` shows only what that board declares and reports — live inputs, outputs with on/off/none tint and dwell time, inhibited marking, and its own controls — refreshing every 2 s.

**Architecture:** The board's retained `capabilities/<name>` document is the sole source of what its window shows (spec §3). Two additive `ChannelDescriptor` fields (`direction`, `driven_by`) let the simulators declare inputs vs outputs and what drives them. The health monitor gains a per-board signal store with wall-clock timestamps and transition tracking; the VMC's existing MQTT handlers additionally feed it. A pure view-model builder turns a board's row + signals + availability into template data; a stable wrapper polls a bare `/live` fragment. Spec: `docs/superpowers/specs/2026-09-30-subsystem-windows-design.md` — read it first; tables in §4.2 and §4.5 are normative.

**Tech Stack:** Pydantic v2 contracts, FastAPI + Jinja2 + htmx 1.9.10, Tailwind v3.4.17 standalone CLI, pytest with the `wired`/`login_as`/`client` fixtures in `tests/conftest.py`.

## Global Constraints

- **Branch:** `feat/subsystem-windows` (already created off main at cb09158). Never commit to main.
- **No `&&` in shell commands**; one command per Bash call. **Python via `uv` only.**
- **Lint before each commit:** `ruff check --fix .` then `ruff format .` (separate calls). `ruff format .` reformats ~14 unrelated files under `tests/` on this machine; revert each with `git checkout -- tests/<file>` and stage only the task's files by explicit path. Never `git add -A` / `git add .`.
- **Commit trailer:** `Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>` after a blank line.
- **Contract versions:** `contracts/vending_machine.py` `CONTRACT_VERSION` `0.6.0 → 0.7.0`; `contracts/ice_maker_monitor.py` `1.3.0 → 1.4.0`. Schemas regenerated with `uv run python -m contracts.generate`; `tests/test_contract_schemas.py` fails on drift.
- **Exact channel tables** (spec §4.2) and **inhibit rules** (spec §4.5) are normative; copy them, do not reinterpret.
- **Nothing in the control path changes.** New health-monitor calls in VMC handlers are additive and guarded by `if self._health_monitor`.
- **`/screen`, `/health/subsystems` list, Tests level unchanged.** `get_summary()["temperatures"]` and `["channels"]` keep their current shape.
- **Rebuild `app.css`** after any template or `tailwind.input.css` change: `.tailwind/tailwindcss.exe -c web_interface/tailwind.config.js -i web_interface/tailwind.input.css -o web_interface/static/app.css --minify` (binary already present, gitignored). Commit `app.css`.
- **Polling exemption:** `web_interface/auth.py` `POLLING_PATHS` must contain the new `/live` path pattern; today it is a frozenset of exact paths — Task 7 changes the membership test to also match `/health/subsystems/<name>/live`.
- Plans specify interfaces and tests; implementers write the bodies. Follow existing patterns in each file.

## File Structure

| Path | Responsibility |
|---|---|
| `contracts/common.py` (modify) | `ChannelDescriptor.direction`, `.driven_by` |
| `contracts/vending_machine.py`, `contracts/ice_maker_monitor.py` (modify) | version bumps |
| `contracts/generate.py` (modify) | add `subsystem_capabilities` to `VENDING_MODELS` |
| `docs/contracts/*/CONTRACT.md`, `docs/contracts/*/schemas/*.json` (modify/generate) | contract docs and schemas |
| `simulators/base.py`, `simulators/{vending_machine,mdb_gateway,ice_maker}.py` (modify) | `CHANNELS` declarations |
| `services/health_monitor.py` (modify) | `Signal`, `record_signal`, attribution, `get_summary()["signals"]`, row `channels` |
| `controller/vmc.py` (modify) | feed signals from four handlers; subscribe `sensors/water_flow` |
| `services/availability.py` (modify) | `command_inhibited` |
| `web_interface/subsystem_window.py` (create) | `build_window` view model |
| `web_interface/routes/health.py` (modify) | detail page + `/live` fragment |
| `web_interface/auth.py` (modify) | polling exemption |
| `web_interface/templates/health_subsystem.html` (modify), `templates/partials/subsystem_live.html` (create) | page and fragment |
| `web_interface/tailwind.input.css`, `tailwind.config.js`, `static/app.css` (modify) | `.ring-dashed` |
| `CLAUDE.md` (modify) | docs |
| tests as named per task | |

---

### Task 1: Contract fields, version bumps, schemas

**Files:**
- Modify: `contracts/common.py` (`ChannelDescriptor`, ~line 32), `contracts/vending_machine.py:38`, `contracts/ice_maker_monitor.py:38`, `contracts/generate.py` (`VENDING_MODELS`), `docs/contracts/vending-machine/CONTRACT.md`, `docs/contracts/ice-maker-monitor/CONTRACT.md`
- Generate: `docs/contracts/ice-maker-monitor/schemas/{channel_descriptor,monitor_capabilities}.schema.json`, `docs/contracts/vending-machine/schemas/subsystem_capabilities.schema.json` (new)
- Test: `tests/test_contracts.py`, `tests/test_contracts_vending.py`, `tests/test_contract_schemas.py`

**Interfaces:**
- Produces: `ChannelDescriptor.direction: Literal["input", "output"] = "input"`; `ChannelDescriptor.driven_by: str | None = None` (Field description: "Command whose refusal by the VMC inhibits this signal"). Both optional → every existing payload still validates.
- Produces: `CONTRACT_VERSION == "0.7.0"` (vending), `"1.4.0"` (ice maker).

- [ ] **Step 1: Tests (RED).** In `tests/test_contracts.py` add: a `ChannelDescriptor` built without the new fields has `direction == "input"` and `driven_by is None`; `direction="output", driven_by="dispense"` round-trips through `model_dump()`; `direction="sideways"` raises `ValidationError`. In `tests/test_contracts_vending.py` and the ice-maker contract test, update the version assertions to `0.7.0` / `1.4.0`. `tests/test_contract_schemas.py::test_schema_dir_has_exactly_the_expected_files` will fail for the vending dir until `subsystem_capabilities.schema.json` exists — that is the RED for the generator change.
- [ ] **Step 2:** Run `uv run pytest tests/test_contracts.py tests/test_contracts_vending.py tests/test_contract_schemas.py -q` → failures as above.
- [ ] **Step 3: Implement.** Add the two fields; bump both versions; add `"subsystem_capabilities": SubsystemCapabilities` to `VENDING_MODELS`; run `uv run python -m contracts.generate`. In each CONTRACT.md bump the title version and add a "Semantics fixed in 0.7.0 / 1.4.0" section: the two fields, their defaults, that `driven_by` names a command in the board's own `commands` list, and that the dashboard renders only declared channels (spec §3).
- [ ] **Step 4:** Run the same tests → all pass; then `uv run pytest -q` → green.
- [ ] **Step 5:** Lint; stage the contract sources, generator, both CONTRACT.md files, the regenerated/new schema files and tests; commit `feat(contracts): channel direction and driven_by; vending 0.7.0, ice maker 1.4.0`.

---

### Task 2: Simulators declare their channels

**Files:**
- Modify: `simulators/base.py` (`build_capabilities`, ~line 205), `simulators/vending_machine.py`, `simulators/mdb_gateway.py`, `simulators/ice_maker.py` (`build_capabilities`, ~line 455)
- Test: `tests/test_simulator_vending.py`, `tests/test_simulator_mdb.py`, `tests/test_simulator_ice_maker.py`

**Interfaces:**
- Consumes: Task 1 fields.
- Produces: `BaseSimulator.CHANNELS: list[ChannelDescriptor] = []` class attribute; `build_capabilities()` passes `channels=list(self.CHANNELS)`. The ice-maker override keeps building its temperature list dynamically and appends `TELEMETRY_CHANNELS` plus the new `compressor_run` descriptor (kind `binary`, direction `output`, `driven_by=None`, `interval_seconds` = its event cadence or `5.0`).
- Produces: vending `CHANNELS` exactly the eleven rows of spec §4.2 (declaration order as the table; `interval_seconds` = the sim's publish interval for analog, `1.0` for binary); MDB `CHANNELS` three binary inputs with `driven_by="payment/enable"`.

- [ ] **Step 1: Tests (RED).** Vending: `build_capabilities().channels` ids == the eleven ids in table order; `{c.channel_id for c in channels if c.direction == "output"}` == `{auger_motor, agitator_motor, bag_drop_solenoid, water_valve_solenoid, fan, heater_relay}`; `driven_by` map == `{auger_motor: dispense, agitator_motor: dispense, bag_drop_solenoid: dispense, water_valve_solenoid: water_valve}` and `None` for the rest; every key of `HARDWARE_DEVICES` is a declared binary channel (guards a device added to the sim but not declared). MDB: three ids, all `binary`/`input`/`driven_by == "payment/enable"`, ids == `[d["name"] for d in sim.devices]`. Ice maker: extend `test_capabilities_lists_all_channels_and_commands` to 13 ids, `compressor_run` present with `direction == "output"`, every temperature channel `direction == "input"`.
- [ ] **Step 2:** Run the three files → RED.
- [ ] **Step 3: Implement** per Interfaces. Keep the HA discovery lists untouched.
- [ ] **Step 4:** Tests pass; `uv run pytest -q` green.
- [ ] **Step 5:** Lint; commit `feat(sim): every simulator declares its channels with direction and driven_by`.

---

### Task 3: Health monitor signal store and attribution

**Files:**
- Modify: `services/health_monitor.py` (`__init__` ~line 96, `record_temperature` ~181, `record_channel` ~190, `record_capabilities` ~151, `get_summary` ~255)
- Test: `tests/test_health_monitor.py` (new class `TestSignals`)

**Interfaces:**
- Produces: `@dataclass class Signal` (spec §4.3 fields). `HealthMonitor.record_signal(subsystem: str, channel_id: str, value: float, *, text: str | None = None) -> None`. Transition rule: first reading sets `transition_* = updated_*`, `transitions_seen = 0`; a later reading with `value != previous` sets `transition_*` to now and increments; equal value leaves transition fields alone. (Applies to every kind; for analog channels the fields are simply unused by the view.)
- Produces: `HealthMonitor.declaring_subsystem(channel_id: str, kind: str | None = None) -> str | None` — the first board whose stored capabilities list a channel with that id (and kind, when given), scanning `EXPECTED_SUBSYSTEMS` order then any others. Used by `record_temperature` (kind `"temperature"`) and `record_channel` (any kind) to attribute; unattributed readings are stored as today and not added to `_signals`.
- Produces: `get_summary()["signals"]` per spec §4.3 and `subsystems[name]["channels"]` = list of descriptor dicts (`channel_id, kind, unit, description, direction, driven_by`) in declaration order, `[]` when capabilities are missing or malformed (non-list, or an entry that is not a dict with a `channel_id`). `empty_subsystem_row()` gains `"channels": []`.
- `time.time()` and `time.monotonic()` must be read through module-level names so tests can `monkeypatch` them.

- [ ] **Step 1: Tests (RED)** in `TestSignals`: (a) `record_signal("vending","fan",1.0)` → summary `signals["vending"]["fan"]` has `value 1.0`, `text None`, `transitions_seen 0`, `dwell_seconds >= 0`, `updated_at` ≈ patched wall time; (b) with monotonic patched, a second reading `0.0` bumps `transition_at` and `transitions_seen == 1`, a third `0.0` does not; (c) `text="error"` stored and echoed; (d) `record_capabilities("ice_maker", {"channels": [{"channel_id": "evaporator", "kind": "temperature", ...}]})` then `record_temperature("evaporator", -12.0)` → `signals["ice_maker"]["evaporator"]`; `record_temperature("cabinet", 4.0)` with no declarer → in `temperatures`, not in any `signals`; (e) `record_capabilities("vending", caps_with_two_channels)` → row `channels` is the two dicts in order and `channel_count == 2`; malformed `{"channels": "x"}` → `[]`; (f) `empty_subsystem_row()["channels"] == []`.
- [ ] **Step 2:** RED.
- [ ] **Step 3: Implement.** Keep `_temperatures`/`_channels` and their alerting untouched; `record_temperature`/`record_channel` gain one attribution call each.
- [ ] **Step 4:** `uv run pytest tests/test_health_monitor.py -q` green; full suite green (existing `TestGetSummary` shape tests must still pass — they assert presence, not absence, of keys; verify).
- [ ] **Step 5:** Lint; commit `feat(health): per-board signal store with wall-clock timestamps and transition tracking`.

---

### Task 4: VMC feeds signals

**Files:**
- Modify: `controller/vmc.py` — `set_mqtt_client` registrations (~line 393-401), `_handle_mqtt_hardware_io` (~899), `_handle_mqtt_payment_status` (~920), `_handle_mqtt_sensor` (~1112), `_handle_mqtt_ice_maker_event` (~1147)
- Test: `tests/test_vmc_flows.py` (near `test_payment_status_error_feeds_availability`, line ~1543, using `_wired_vmc()`)

**Interfaces:**
- Consumes: `HealthMonitor.record_signal`, `record_channel` (Task 3).
- Produces: new registration `client.register("sensors/water_flow", self._handle_mqtt_water_flow)`; `async def _handle_mqtt_water_flow(topic, data)` validates `SensorReading` and calls `record_channel("water_flow", value)`. `_handle_mqtt_hardware_io` adds `record_signal("vending", hw.device, 1.0 if hw.state else 0.0)`; `_handle_mqtt_payment_status` adds `record_signal("mdb", status.device, 1.0 if status.state == "ready" else 0.0, text=status.state)`; `_handle_mqtt_ice_maker_event` adds, for `power_on`/`power_off` only, `record_signal("ice_maker", "compressor_run", 1.0/0.0)`. All guarded by `if self._health_monitor`. Existing behaviour of each handler unchanged.

- [ ] **Step 1: Tests (RED).** With `_wired_vmc()`: `await vmc._handle_mqtt_hardware_io("hardware/io/fan", {"device": "fan", "state": True})` → `monitor.get_summary()["signals"]["vending"]["fan"]["value"] == 1.0`; payment status `error` → `signals["mdb"]["card_reader"]` value `0.0`, text `"error"` (and the existing availability assertion still holds); ice event `power_on` → `signals["ice_maker"]["compressor_run"]["value"] == 1.0`, `needs_cleaning` leaves it absent; water flow `{"location": "water_flow", "value": 12.5, "unit": "gal"}` → `channels["water_flow"]["value"] == 12.5`; `"sensors/water_flow" in client.topics` in the registration test pattern at line ~888. Also: with no health monitor attached, each handler still runs without error.
- [ ] **Step 2:** RED.
- [ ] **Step 3: Implement.**
- [ ] **Step 4:** Tests pass; full suite green.
- [ ] **Step 5:** Lint; commit `feat(vmc): feed hardware IO, payment status, compressor events and water flow into health signals`.

---

### Task 5: `Availability.command_inhibited`

**Files:**
- Modify: `services/availability.py` (outputs section, after `payment_enabled` ~line 333)
- Test: `tests/test_availability.py`

**Interfaces:**
- Produces: `def command_inhibited(self, command: str) -> bool` with exactly the four rows of spec §4.5. Pure read; no recompute, no publish.

- [ ] **Step 1: Tests (RED)** using `_avail()` and `_all_good()`: all good → `dispense`, `water_valve`, `payment/enable`, `ping`, `refund` all `False`; `set_subsystem_alive("ice_maker", False)` (ice unavailable, water still sells) → `dispense False`, `water_valve False`; vending lost (`set_subsystem_alive("vending", False)`) → `dispense True`, `water_valve True`, `payment/enable False`; a machine critical fault via `set_active_faults([_machine_fault("WTR-103")])` → all three `True`; `set_payment_device("card_reader", "error")` → `dispense True` (sale blocked), `payment/enable False`.
- [ ] **Step 2:** RED.
- [ ] **Step 3: Implement.**
- [ ] **Step 4:** Green; full suite green.
- [ ] **Step 5:** Lint; commit `feat(availability): command_inhibited maps a driving command to the current gate state`.

---

### Task 6: View model `build_window`

**Files:**
- Create: `web_interface/subsystem_window.py`
- Test: `tests/test_subsystem_window.py` (new)

**Interfaces:**
- Consumes: a summary row (`alive`, `stale`, `channels`, `commands`), the board's `signals` dict from `get_summary()["signals"].get(name, {})`, an `Availability | None`, `temp_min`/`temp_max` floats, and `now: float` (epoch seconds) plus a `tz` for clock strings.
- Produces:

```python
def build_window(
    row: dict, signals: dict, availability, *,
    temp_range: tuple[float, float], now: float, tz=None,
) -> dict
```
returning exactly the shape in spec §4.6. Rules: order = declaration order; `digital = kind == "binary"`; `state` is `"none"` if `not row["alive"]` or the channel has no signal, else `"on"` if `value >= 0.5` else `"off"`; `label = description or channel_id.replace("_", " ")`; `updated_clock`/`transition_clock` = `datetime.fromtimestamp(ts, tz).strftime("%H:%M:%S")`; `age_seconds = now - updated_at`; `dwell_seconds = now - transition_at`; `dwell_since_start = transitions_seen == 0`; `inhibited = bool(driven_by) and availability is not None and availability.command_inhibited(driven_by)`; `in_range` only for `kind == "temperature"` with a value, else `None`; controls: `actuators = [c for c in row["commands"] if c not in STANDARD_COMMANDS]` each with `inhibited` via `command_inhibited`, `standard = [c for c in STANDARD_COMMANDS if c in row["commands"]]`. Missing `channels`/`commands` keys are treated as empty. No I/O, no `time.time()` inside — `now` is injected.

- [ ] **Step 1: Tests (RED)** with hand-built rows and a tiny fake availability (`command_inhibited = lambda c: c in {"dispense"}`): (a) declaration order preserved across inputs/outputs; (b) analog vs digital split and `in_range` for a temperature inside/outside `temp_range`; (c) `state == "none"` when `alive` is False even with a fresh signal, and when no signal; (d) `inhibited` True only for the `driven_by="dispense"` output and the `dispense` actuator chip, False for `fan` and `ping`; (e) `dwell_since_start` True/False; (f) clock strings for a fixed `now` and `tz=timezone.utc`; (g) empty row → three empty sections, no exception; (h) `availability=None` → nothing inhibited.
- [ ] **Step 2:** RED (module missing).
- [ ] **Step 3: Implement** (~80 lines, one public function, private helpers for a signal view and a clock string).
- [ ] **Step 4:** Green.
- [ ] **Step 5:** Lint; commit `feat(web): build_window view model for a board's live window`.

---

### Task 7: Route, fragment, templates, polling exemption, CSS

**Files:**
- Modify: `web_interface/routes/health.py` (`subsystem_detail` ~line 293-323; add the `/live` route beside it), `web_interface/auth.py` (`POLLING_PATHS` ~line 43 and its use at ~102), `web_interface/templates/health_subsystem.html`, `web_interface/tailwind.input.css`, `web_interface/tailwind.config.js` (safelist), `web_interface/static/app.css` (rebuild)
- Create: `web_interface/templates/partials/subsystem_live.html`
- Test: `tests/test_routes_health.py` (`TestSubsystemDetailLevel`, ~line 128; new `TestSubsystemLiveFragment`), `tests/test_web_routes.py` (`TestPollingDoesNotTouchSessionIdle`), `tests/test_static_css.py` (existing coverage test)

**Interfaces:**
- Consumes: `build_window` (Task 6), `get_summary()["signals"]` and row `channels` (Task 3), `context.availability`, `HealthMonitor` temp range (expose `HealthMonitor.temp_range -> tuple[float, float]` property in this task; default `(-20.0, 80.0)` when no monitor).
- Produces: a private `_window_context(name) -> dict` in `routes/health.py` that reads the summary once, builds the row (via `_subsystem_summary()`), signals, availability and calls `build_window(..., now=time.time(), tz=local)`; both routes use it. `GET /health/subsystems/{name}/live` → `templates.TemplateResponse("partials/subsystem_live.html", context.template_context(request, name=name, window=...))`, 404 for unknown name, gate `view_status`. `health_subsystem.html`: identity card unchanged; the temperature block and the route's stand-in comment deleted; new `<div id="live" hx-get="/health/subsystems/{{ name }}/live" hx-trigger="load, every 2s" hx-swap="innerHTML" hx-target="this">{% include "partials/subsystem_live.html" %}</div>`. The partial renders Inputs / Outputs / Controls per spec §4.7 and contains **no** `hx-` attribute.
- Produces: `web_interface/auth.py` — replace the exact-path check with `def is_polling_path(path: str) -> bool` returning True for members of `POLLING_PATHS` or for paths matching `^/health/subsystems/[a-z0-9_]+/live$`; the call site at ~line 102 uses it.
- Produces: `.ring-dashed { outline: 2px dashed theme(colors.amber.500); outline-offset: 2px; }` in `@layer components` of `tailwind.input.css`, `"ring-dashed"` appended to the config `safelist`; `app.css` rebuilt.

- [ ] **Step 1: Tests (RED).** Routes (`TestSubsystemDetailLevel`, using a `HealthMonitor` wired via `routes.set_health_monitor` as the existing MDB identity test does at ~line 140): (a) monitor with ice-maker caps declaring `evaporator` temperature and vending caps declaring `cabinet`, `record_temperature` for both, heartbeats for both → `/health/subsystems/vending` contains `cabinet` and not `evaporator`; `/health/subsystems/ice_maker` the reverse; (b) MDB caps + `record_signal("mdb","card_reader",0.0,text="error")` + heartbeat → page shows `card_reader`, `error`, `bg-red-600`; (c) no heartbeat (never seen) with a signal present → `bg-gray-200` and no `bg-green-600`; (d) `Availability` wired with vending lost → an output driven by `dispense` renders `ring-dashed` and the word `inhibited`, the `fan` row does not; (e) the full page's `hx-trigger` count is exactly 2 (pill + `#live`) and `find_by_id(..., "live")[0].attrs["hx-target"] == "this"`. `TestSubsystemLiveFragment`: 200 for the three names, 404 for `nope`, response has no `hx-` substring, contains the three section headings. `TestPollingDoesNotTouchSessionIdle`: `GET /health/subsystems/vending/live` leaves `last_active_at` unchanged; `GET /health/subsystems/vending` refreshes it. `tests/test_static_css.py` will report the new classes missing until the rebuild.
- [ ] **Step 2:** RED.
- [ ] **Step 3: Implement** route, helper, partial, page, auth predicate, CSS; rebuild `app.css`.
- [ ] **Step 4:** `uv run pytest tests/test_routes_health.py tests/test_web_routes.py tests/test_static_css.py -q` green; full suite green.
- [ ] **Step 5:** Manual look: `uv run python main.py`, open `/health/subsystems/vending` — with no broker every section shows declared-but-gray or "Nothing declared by this board." Do not commit anything from this step.
- [ ] **Step 6:** Lint; commit `feat(web): live per-board window with inputs, outputs, dwell time and inhibited marking`.

---

### Task 8: Docs and whole-branch verification

**Files:**
- Modify: `CLAUDE.md` (Health level paragraph; the "only fragment endpoints" sentence; contract-version note in "Fault Codes and Severity")

- [ ] **Step 1:** Update CLAUDE.md: the fragment sentence lists `/health/subsystems/{name}/live` (2 s, exempt from the idle clock via `is_polling_path`); the Health paragraph says the detail page renders only the board's declared channels (`capabilities/<name>` → `channels`, with `direction`/`driven_by`), inputs as circles / outputs as squares tinted green/red/gray, dwell time from `HealthMonitor` transition tracking (in-memory, resets on restart), inhibited via `Availability.command_inhibited`, and that a board declaring nothing shows empty sections; note the contract bumps 0.7.0 / 1.4.0.
- [ ] **Step 2:** `uv run pytest -q` green; `ruff check .` clean; with `ICE_COLDER_BROWSER_TESTS=1` the four browser tests still pass (the pill wrapper change is not touched here, but `base.html` is re-rendered).
- [ ] **Step 3:** Commit `docs: subsystem windows in CLAUDE.md`.
- [ ] **Step 4:** Push, open the PR (footer per the user's PR-attribution rule), check Copilot.

---

## Self-review

- **Spec coverage:** §4.1 → T1; §4.2 → T2; §4.3 → T3; §4.4 → T4; §4.5 → T5; §4.6 → T6 + T7; §4.7 → T7; §4.8 → T1 (CONTRACT.md) + T8; §5 tests → each task.
- **Consistency:** `record_signal(subsystem, channel_id, value, *, text=None)` used identically in T3, T4, T7 tests; `command_inhibited(command)` in T5, T6, T7; `build_window(row, signals, availability, *, temp_range, now, tz)` in T6, T7; `is_polling_path` in T7/T8; `channels` row key in T3, T6, T7.
- **Known judgement calls:** transition tracking applies to every kind (simpler than consulting the descriptor at record time); attribution scans stored capabilities on every temperature reading (three boards, tens of channels — negligible); the water-flow topic was never subscribed before, so its registration is new but harmless.
