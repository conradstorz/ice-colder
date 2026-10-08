# Dispenser profiles (`dispensers.toml`) — design

**Date:** 2026-10-06
**Scope:** a standalone, hand-edited TOML file that gives every physical
dispense slot its own parameters (motors, solenoids, valves, sensors,
timings, success proof, accessories); the schema that validates it; the
VMC, contract and simulator changes that execute it; and a raw-text editor
for it in the dashboard. Bagged ice and water fill are the only mechanisms.
ESP32 firmware is out of scope.

## 1. Problem

The dispense path exists end to end (`cmd/dispense` → board →
`hardware/dispenser` outcome → `vend_failed`/lockout), but motor control is
nominal:

- `Product.slot` (`config/config_model.py:119`) is a bare integer. Which
  mechanism a slot drives is decided by a keyword match on the product
  *name* in the simulator (`simulators/vending_machine.py:38`).
- `DispenseCommand` (`services/mqtt_messages.py:131`) carries only `slot`.
  Every timing, sensor and threshold lives in firmware or nowhere.
- Feedback is the commanded on/off bit (`HardwareIO`). Nothing states how a
  successful dispense is *proven* for a given product.
- `ICE-302` (motor fault) is unreachable: nothing measures current or
  emits `DispenserOutcome.error`.

Different products fail differently. A bag of ice can jam the auger, miss
the full-bag sensor or leave the door open; a water fill can see no flow or
over-dispense. Each needs its own parameters, and those parameters must be
obvious to find, impossible to get silently wrong, and documented where
they are edited.

## 2. Requirements

1. **Standalone file.** One obvious file, apart from `config.json`,
   holding only physical dispense parameters.
2. **Self-documenting in the extreme.** Every field explained, with unit,
   allowed values and range, next to where it is typed. The documentation
   cannot drift from what the code accepts.
3. **Self-validating.** Syntax, schema, and cross-file consistency are all
   checked, with every error naming the slot, the field and (where known)
   the line, in plain English.
4. **Fool-proof.** No silent defaults for anything physical. No field the
   code does not know. A sensor that is absent is *declared* absent, never
   omitted. A bad file can lock out the affected product but can never stop
   the machine running or sell a product with guessed parameters.
5. **Hand-edited.** The file is edited as text, on the machine or through a
   raw-text window in the dashboard that validates before it writes.

## 3. Decisions already made

| Decision | Choice | Why |
|---|---|---|
| Format | TOML | Native comments, stdlib `tomllib` reader, no new dependency, no YAML type coercion. |
| Editing | Raw text only | Round-tripping TOML through a parser drops comments; the file is the one truth and must keep them. |
| Mechanisms | `bagged_ice`, `water_fill` | The only two on this machine. Each may carry accessories (a "vending now" light, a bag-inflation fan). |
| Fault codes | Fixed in code per mechanism | Nobody can map a jam to "warning" from a config file. |
| Step order | Fixed in code per mechanism | The file supplies parameters, never sequence. |

## 4. The file

### 4.1 Location

- Default path `dispensers.toml` in the working directory, beside
  `config.json`. Overridable with `ICE_COLDER_DISPENSERS`, read at call time
  like `ICE_COLDER_CONFIG`. Compose sets it to `/app/data/dispensers.toml`
  for the `vmc` service and the simulators (same bind mount as config).
- `dispensers.toml` is gitignored (machine-specific, like `config.json`).
- `dispensers.example.toml` is committed at the repo root. It is
  **generated from the schema** (§5.3) and is a complete, valid profile
  set for the three sample products in `config.example.json`
  (`SAMPLE-ICE` slot 0, `SAMPLE-WATER-SM` slot 1, `SAMPLE-WATER-LG` slot 2),
  so first-run setup is "copy the example", the same as config. Those
  three products currently declare no `kind` (so default to `"other"`);
  `config.example.json` is edited in this work to give them
  `kind = "ice"`, `"water"`, `"water"`. Slot numbers stay 0, 1, 2. The
  §4.2 listing below uses slots 1 and 2 purely for illustration.
- If the path exists but is a directory, startup logs a clear error and
  exits 1, as `main.py` already does for config.

### 4.2 Shape

```toml
# dispensers.toml — physical dispense parameters, one table per slot.
# Generated reference: dispensers.example.toml. Validate with
#   uv run python -m services.dispensers --check
schema_version = 1

[slot.1]
mechanism   = "bagged_ice"
product_sku = "ICE-10LB"          # must match a catalog product with kind = "ice"
                                  # whose slot is 1

[slot.1.agitate]
motor_channel      = "agitator_motor"
run_seconds        = 4.0          # 0.5–60
stall_current_amps = "unmonitored"   # a number here requires current_channel
current_channel    = "unmonitored"

[slot.1.fill]
motor_channel      = "auger_motor"
proof              = "bag_full_sensor"   # or "timed"
sensor_channel     = "bag_full_sensor"   # bag_full_sensor proof only
max_run_seconds    = 25.0         # 1–120; ICE-301 if the sensor never trips
stall_current_amps = "unmonitored"
current_channel    = "unmonitored"

[slot.1.release]
solenoid_channel      = "bag_drop_solenoid"
proof                 = "door_sensor"    # or "timed"
sensor_channel        = "door_sensor"    # door_sensor proof only
pulse_seconds         = 1.5       # 0.1–10
open_timeout_seconds  = 3.0       # door_sensor proof only; ICE-401 if never open
close_timeout_seconds = 5.0       # door_sensor proof only; ICE-402 if never closed

[slot.1.accessories.bag_fan]
channel      = "bag_fan"
on_during    = ["fill"]           # step names for this mechanism, or ["all"]
lead_seconds = 2.0                # 0–30, on this long before the step starts
lag_seconds  = 0.5                # 0–30, off this long after the step ends

[slot.1.accessories.vending_light]
channel      = "vending_now_light"
on_during    = ["all"]
lead_seconds = 0.0
lag_seconds  = 0.0

[slot.2]
mechanism   = "water_fill"
product_sku = "WATER-1GAL"

[slot.2.fill]
valve_channel          = "water_valve_solenoid"
proof                  = "flow_volume"   # or "timed"
flow_sensor_channel    = "water_flow_sensor"   # flow_volume proof only
target_volume_ml       = 3785      # flow_volume only; 50–50000
pulses_per_liter       = 450.0     # flow_volume only; > 0
min_flow_ml_per_second = 20.0      # flow_volume only; WTR-101 if below after grace
no_flow_grace_seconds  = 3.0       # flow_volume only; 0.5–30
over_dispense_percent  = 10.0      # flow_volume only; 0–50; WTR-102 if exceeded
max_fill_seconds       = 90.0      # 1–600; WTR-101 if volume not reached
```

Rules the shape encodes:

- `[slot.N]` — `N` is the physical slot and **must equal** the catalog
  product's `slot`. The redundancy with `product_sku` is deliberate: both
  must agree or the slot is invalid.
- Every step a mechanism has is required. `bagged_ice` has `agitate`,
  `fill`, `release`; `water_fill` has `fill`. No other step tables are
  accepted.
- `proof` is a discriminator. Each proof variant has exactly its own
  fields; a field from the other variant is an error, not ignored. With
  `proof = "timed"`, `sensor_channel` / `flow_sensor_channel` and the
  volume fields are **not present** (the variant has no such fields), and
  the step runs for `max_run_seconds` / `max_fill_seconds` exactly, then
  succeeds.
- `"unmonitored"` is the only way to say a current sensor is absent.
  `stall_current_amps` and `current_channel` must be both numeric/string or
  both `"unmonitored"`.
- `[slot.N.accessories]` is the one table that may be absent (meaning
  none). An accessory is an output tied to steps. It is never proven and
  never fails a vend; its only validation is schema and channel-role.
- Unknown keys anywhere are errors (`extra="forbid"`).

## 5. Schema and validation (`services/dispensers.py`)

### 5.1 Models

Pydantic v2, `extra="forbid"`, every physical field required (no defaults
except `accessories = {}`). Each `Field` carries `description`, and where
applicable `ge`/`le`/`gt`, plus a `json_schema_extra={"unit": "..."}` so
the generator can print units.

```
DispenserFile          schema_version: Literal[1]; slot: dict[str, SlotProfile]
                       (keys validated as non-negative integer strings)
SlotProfile            = Annotated[BaggedIceProfile | WaterFillProfile,
                                   Discriminator("mechanism")]
BaggedIceProfile       mechanism: Literal["bagged_ice"]; product_sku; agitate: AgitateStep;
                       fill: IceFillStep; release: ReleaseStep; accessories: dict[str, Accessory]
WaterFillProfile       mechanism: Literal["water_fill"]; product_sku; fill: WaterFillStep;
                       accessories
AgitateStep            motor_channel; run_seconds (0.5–60); CurrentSense mixin
IceFillStep            = Annotated[IceFillBySensor | IceFillTimed, Discriminator("proof")]
  IceFillBySensor      proof: "bag_full_sensor"; motor_channel; sensor_channel; max_run_seconds (1–120); CurrentSense
  IceFillTimed         proof: "timed"; motor_channel; max_run_seconds (1–120); CurrentSense
ReleaseStep            = Annotated[ReleaseBySensor | ReleaseTimed, Discriminator("proof")]
  ReleaseBySensor      proof: "door_sensor"; solenoid_channel; sensor_channel; pulse_seconds (0.1–10);
                       open_timeout_seconds (0.5–30); close_timeout_seconds (0.5–60)
  ReleaseTimed         proof: "timed"; solenoid_channel; pulse_seconds (0.1–10)
WaterFillStep          = Annotated[WaterFillByVolume | WaterFillTimed, Discriminator("proof")]
  WaterFillByVolume    proof: "flow_volume"; valve_channel; flow_sensor_channel; target_volume_ml (50–50000);
                       pulses_per_liter (>0); min_flow_ml_per_second (>0); no_flow_grace_seconds (0.5–30);
                       over_dispense_percent (0–50); max_fill_seconds (1–600)
  WaterFillTimed       proof: "timed"; valve_channel; max_fill_seconds (1–600)
CurrentSense (mixin)   stall_current_amps: float (0.1–50) | Literal["unmonitored"];
                       current_channel: str | Literal["unmonitored"]; model validator: both or neither
Accessory              channel; on_during: list[str] (non-empty, unique; either ["all"] alone or
                       step names valid for the owning mechanism); lead_seconds (0–30); lag_seconds (0–30)
```

Channel ids match `^[a-z][a-z0-9_]*$`, the convention of
`ChannelDescriptor.channel_id`.

### 5.2 Validation layers

Validation is **per slot** so one bad slot never takes a good one down.

1. **Syntax.** `tomllib.loads`. A failure yields one error with tomllib's
   line and column; nothing else runs; the file contributes zero profiles.
2. **Top level.** `schema_version` must be `1`; `slot` must be a table;
   each key must match `^(0|[1-9][0-9]*)$` exactly (tomllib already
   delivers `[slot.1]` as the string key `"1"`; a quoted key, a leading
   zero or a sign is an error). A bad key is an error for that key only.
3. **Per-slot schema.** Each `[slot.N]` is validated alone against
   `SlotProfile`. Pydantic errors are rewritten into plain English:
   `Slot 1 › fill.max_run_seconds: must be between 1 and 120 seconds, got 500`,
   `Slot 2 › fill: unknown field "pulses_per_litre" (did you mean pulses_per_liter?)`,
   `Slot 1 › release: proof is "timed" but sensor_channel is present; timed release has no sensor`.
4. **Cross-checks** (need the catalog; capabilities optional):
   - Every catalog product with `kind` `ice` or `water` has exactly one
     valid slot whose `product_sku` matches; that slot's `N` equals the
     product's `slot`. Missing → `Slot 3 › missing: product "ICE-20LB" (kind ice, slot 3) has no [slot.3] table`.
   - Every `product_sku` names a catalog product; `bagged_ice` requires
     `kind = "ice"`, `water_fill` requires `kind = "water"`; no sku appears
     in two slots.
   - **Channel roles.** Across the whole file, a channel id used as a
     drive (`motor_channel`, `solenoid_channel`, `valve_channel`, accessory
     `channel`) is never also used as a sense (`sensor_channel`,
     `flow_sensor_channel`, `current_channel`). Sharing a drive channel
     between slots (two bag sizes, one auger) is allowed.
   - **Time budget.** Worst-case step time per slot must fit inside
     `physical.dispense_timeout_seconds` with a 5 s margin. Bagged ice:
     `agitate.run_seconds + fill.max_run_seconds + release.pulse_seconds +
     open_timeout + close_timeout + max(accessory lead) + max(accessory lag)`.
     Water: `no_flow_grace_seconds + max_fill_seconds + leads + lags`.
   - **Capabilities** (runtime only, or `--check --capabilities FILE`): when
     the vending board's retained `capabilities/vending` document is known,
     every drive channel must be declared with `direction="output"` and
     every sense channel with `direction="input"`. Board not yet seen →
     one *warning* per slot, not an error.
5. **Report.** A `ValidationReport` of `Finding(slot: int | None, path: str,
   line: int | None, severity: "error" | "warning", message: str)` plus a
   per-slot verdict. Rendered as text for the CLI and as a partial for the
   dashboard. A slot is **valid** iff it has zero errors.

Line numbers: tomllib reports them for syntax errors only. For schema and
cross-check findings the loader scans the source text for the
`[slot.N...]` table header and the key name and reports that line as
best-effort; the dotted path is always present and authoritative.

### 5.3 Self-documenting generator

`scripts/gen_dispensers_example.py` walks the Pydantic models and writes
`dispensers.example.toml`: a header explaining the file, then for every
field a comment with its description, unit, allowed literals and range,
then the field with a working sample value. Union variants are shown with
the chosen variant live and the other variant's fields in a commented
block headed `# if proof = "timed" instead:`. A test loads the committed
example, asserts it is byte-identical to a fresh generation, and asserts it
validates with zero errors against `config.example.json`.

### 5.4 CLI

`uv run python -m services.dispensers --check [PATH] [--config PATH]
[--capabilities FILE]` prints the report and exits 0 (no errors), 1 (any
error). `--example` prints the generated example to stdout.

## 6. Runtime

### 6.1 Service object

`DispenserProfiles` (in `services/dispensers.py`), constructed in `main.py`
after the config loads and handed to the VMC (`VMC.set_dispenser_profiles`)
and the routes (`routes.set_dispenser_profiles`):

- `load() -> ValidationReport` — reads the file, runs §5.2, stores valid
  profiles by slot; a missing file is a single warning finding and zero
  profiles.
- `validate_text(text) -> ValidationReport` — the same pipeline on
  arbitrary text, writing nothing.
- `save_text(text, expected_digest) -> ValidationReport` — refuses if the
  report has any error or if the current file's SHA-256 differs from
  `expected_digest` (someone else saved first); otherwise writes atomically
  (tmp + rename), keeps a rolling `dispensers.toml.bak`, then `load()`s.
- `profile_for_slot(slot) -> SlotProfile | None`, `report` (last), `digest`.
- `set_capabilities(doc)` — called by the VMC whenever the retained
  `capabilities/vending` document arrives, re-running cross-checks so the
  capabilities warning resolves without a save.

### 6.2 Faults

Two new codes in `contracts/vending_machine.py`, neither in
`PAYMENT_BLOCKING_FAULTS`:

| Code | Severity | Scope | Description |
|---|---|---|---|
| `CFG-101` | `product_unavailable` | product | No valid dispenser profile for this slot (see dispensers.toml) |
| `CFG-102` | `warning` | machine | dispensers.toml could not be read (missing or syntax error) |

After every `load()`, the VMC reconciles: raise `CFG-101` for every
`ice`/`water` product whose slot has no valid profile, clear it for every
product that now has one; raise/clear `CFG-102` on the file-level finding.
Both clear themselves; neither needs an operator.

### 6.3 Selecting and dispensing

- `VMC.select_product` refuses a product locked by `CFG-101` through the
  existing lockout path; the customer message says the product is
  unavailable. A product with `kind = "other"` has no mechanism and is
  always `CFG-101`-locked. (Today's sample catalogs and fixtures that use
  `kind = "other"` for a sellable product must gain a profile or change
  kind — see §9.)
- `DispenseCommand` becomes `slot: int`, `mechanism: Literal[...]`,
  `profile: SlotProfile` — the board receives the whole validated profile
  and is stateless about configuration. A save during a vend cannot affect
  that vend: its parameters already left with the command.
- **New wiring, not reuse.** Today `_persist_then_dispense`
  (`controller/vmc.py:1375`) does a bare publish of `DispenseCommand` to
  the legacy `cmd/dispense` topic with no `request_id` and no ack, and
  `DispenserStatus.request_id` is documented as always `None` for a sale
  (`services/mqtt_messages.py:79-88`). After this change the production
  sale goes through `CommandDispatcher.send("vending", "dispense",
  params)` — the command channel `cmd/vending`, a `SubsystemCommand` with
  a `request_id`, `params` = the `DispenseCommand` body above. The VMC
  stops publishing to `cmd/dispense`; the simulator keeps its legacy
  subscription for one contract version, then drops it.
  - **Accept phase** (dispatcher): awaits the `accepted` ack within the
    dispatcher's existing `ACK_TIMEOUT_SECONDS`, one retry with the same
    `request_id`. `CommandTimeout` (no ack, or broker disconnected, which
    the dispatcher raises immediately) → `_fail_vend` with `PAY-102` at
    once, instead of waiting out `dispense_timeout_seconds`.
  - **Completion phase** (unchanged, VMC-owned): `send_and_await_completion`
    is **not** used for a sale. The existing `_dispense_timeout_task`
    armed from `dispense_timeout_seconds` and the existing
    `hardware/dispenser` handler remain the single completion mechanism,
    so the FSM, snapshot and `PAY-102` semantics are untouched. The board
    now echoes the `request_id` on its terminal `DispenserStatus`; the
    handler logs a mismatch but still keys on `slot` and FSM state as
    today.
  - The `DispenserStatus.request_id` docstring is rewritten to match.
- The maintenance test sale (`run_test_sale`) and `/tests/vending/dispense`
  use the same profile lookup; a slot without a valid profile is refused
  inline with the `CFG-101` wording.

### 6.4 Contract 0.8.0 (`contracts/vending_machine.py`, CONTRACT.md, schemas)

- `DispenseCommand` as above.
- `DispenseStep` enum: `agitate`, `fill`, `release` (bagged ice), `fill`
  (water). `DispenserStatus.state: DispenseStep | DispenserOutcome`;
  `DispenserStatus.detail: str | None` for board-supplied text such as
  `"stall 4.2 A"` or `"412 pulses"`.
- `DispenserOutcome` gains `door_open` (release completed but the door did
  not read closed within `close_timeout_seconds`), `no_flow` and
  `over_dispense`.
- `OUTCOME_FAULTS` becomes keyed by `(mechanism, outcome)`:

  | mechanism | outcome | fault |
  |---|---|---|
  | bagged_ice | timeout | ICE-301 |
  | bagged_ice | error | ICE-302 |
  | bagged_ice | jam | ICE-401 |
  | bagged_ice | door_open | ICE-402 |
  | water_fill | no_flow | WTR-101 |
  | water_fill | over_dispense | WTR-102 |
  | water_fill | timeout | WTR-101 |
  | water_fill | error | ICE-302 |
  | any | bin_empty | ICE-101 (existing mapping) |

  `door_open` ends the sale as a **successful** vend for the customer (the
  bag dropped) but raises the critical `ICE-402`, which is payment-blocking
  and never auto-clears, exactly as the fault table already states.
  `ICE-302` is reused for a water-fill `error` because the board reports a
  generic actuator fault either way; its description changes from
  "Dispense/agitator motor fault" to "Dispense actuator fault reported by
  the board (motor stall, valve driver, over-current)" so the wording is
  mechanism-agnostic. Severity and scope are unchanged.
- `CONTRACT_VERSION` `0.7.0 → 0.8.0`. `contracts/generate.py` regenerates
  the JSON schemas; the drift test covers them.

### 6.5 Simulator (`simulators/vending_machine.py`)

- Executes the received `profile` literally: accessories lead/lag around
  their steps, agitate for `run_seconds`, fill until the sensor trips or
  `max_run_seconds`, pulse the solenoid, read the door; water opens the
  valve and emits `fill_pulses` flow-meter counts at a configurable rate until
  `target_volume_ml`. Every output toggle still goes to
  `hardware/io/<device>`; every step publishes a `DispenserStatus` with the
  `DispenseStep`.
- `HARDWARE_DEVICES` and `_VENDING_CHANNELS` gain `bag_fan`,
  `vending_now_light`, `door_sensor`, `agitator_current`, `auger_current`
  (kind `current`, input) so the example file validates against the
  simulator's capabilities with no warnings.
- Fault injection gains `motor_stall` (current above `stall_current_amps`
  → outcome `error`), `no_water_flow` (→ `no_flow`), `door_stuck_open`
  (→ `door_open`), `flow_runaway` (→ `over_dispense`). Existing `auger_jam`
  (→ `timeout`) and `bag_drop_solenoid_stuck` (→ `jam`) are kept. With this
  every row of the table in §6.4 is reachable end to end.
- The product-name keyword heuristic `_classify_product` is deleted.

## 7. Dashboard

### 7.1 `/settings/dispensers`

- New `LEVEL_SETTINGS_DISPENSERS` under `LEVEL_SETTINGS`
  (`web_interface/levels.py:104`). Gate: `machine_controls` (owner and
  tech). Because `/settings` itself is gated
  `_require_any(edit_contacts, edit_secrets)`
  (`web_interface/routes/settings.py:161`), which a tech lacks, that one
  index gate widens to `edit_contacts or edit_secrets or machine_controls`,
  and the index renders only the cards the caller may open, so a tech sees
  Dispensers alone. The Home Settings tile's permission set
  (`web_interface/routes/home.py:78`) gains `machine_controls` for the same
  reason. **No other `/settings/*` sub-route changes its gate.**
- Page: the current validation report (as at last load) at the top; a
  monospace `<textarea>` holding the raw file text (or the generated
  example when the file is missing, with a banner saying so); a hidden
  `digest` field; two buttons.
  - **Validate** — `hx-post="/settings/dispensers/validate"` with the
    textarea, swaps the report partial. Writes nothing.
  - **Save** — `hx-post="/settings/dispensers"` with textarea and digest.
    Any error → the report partial, file untouched. Digest mismatch → one
    error "file changed on disk since you opened it; reload the page".
    Otherwise the file is written, reloaded, faults reconciled, and the
    page re-renders with the new report and digest.
- Both POSTs require `require_htmx`. No JavaScript beyond HTMX; the
  textarea is a plain form field.
- `partials/dispenser_report.html` renders findings grouped by slot, errors
  before warnings, each with path, line (when known) and message, plus a
  per-slot OK/invalid verdict line.

### 7.2 Read-only profile cards

- `/products/{sku}` shows a "Dispenser" card: mechanism, proof per step,
  key timings, accessories; or the `CFG-101` finding text with a link to
  `/settings/dispensers` for callers holding `machine_controls`.
- `/tests/vending` shows the same summary beside each slot in the dispense
  `<select>`; an invalid slot is listed but disabled with the reason.

## 8. Testing

- `tests/test_dispensers_schema.py` — one fixture file per rule in §4.2 and
  §5.2 (missing step, unknown key, timed proof with sensor present,
  numeric stall without current channel, bad slot key, sku/slot mismatch,
  kind/mechanism mismatch, channel used as both drive and sense, time
  budget exceeded, duplicate sku). Each asserts the finding's `slot`,
  `path` and that the message contains the offending value. Per-slot
  isolation: a file with slot 1 bad and slot 2 good yields one valid
  profile.
- `tests/test_dispensers_example.py` — committed example is byte-identical
  to a fresh generation and validates clean against `config.example.json`;
  the CLI exits 0 on it and 1 on a broken copy.
- `tests/test_vmc_dispense_profiles.py` — `select_product` refuses a slot
  without a profile with `CFG-101`; the fault clears after a reload that
  adds the profile; `DispenseCommand` carries the profile; the accepted-ack
  path retries once with the same `request_id`; broker-down fails the vend
  immediately; a mid-vend save does not change the in-flight command.
- `tests/test_simulator_profiles.py` — the simulator's step sequence and
  `hardware/io` toggles for a bagged-ice profile with a fan accessory
  (fan on at `lead_seconds` before fill, off `lag_seconds` after); each
  injected fault yields the outcome in §6.4; a water fill by volume stops
  within `over_dispense_percent`.
- `tests/test_settings_dispensers.py` — GET gates (owner/tech 200,
  secretary/loader 403); Validate writes nothing; Save with an error leaves
  the file and `.bak` untouched; Save with a stale digest is refused; Save
  with a clean file writes atomically, rotates `.bak`, and the report shows
  OK; `/settings` index renders the Dispensers card alone for a tech.
- Contract drift test and `tests/test_static_css.py` updated for the new
  templates.

## 9. Migration and compatibility

- Fresh clone: copy `dispensers.example.toml` → `dispensers.toml` alongside
  `config.json`. `README`/first-run docs gain one line.
- Existing machines: until the file exists every `ice`/`water` product is
  `CFG-101`-locked and the Home hero shows the `CFG-102` warning. Nothing
  else changes; payment stays enabled for products that still have
  profiles (none, on day one) and the machine keeps running.
- Test fixtures and sample catalogs that sell a `kind = "other"` product
  must switch it to `ice` or `water` with a profile, or assert `CFG-101`.
  Known fixtures to update (grep `kind="other"` and `cmd/dispense` /
  `DispenseCommand(` before starting; this list is the state on
  2026-10-06):
  - `kind="other"` products: `tests/test_availability.py:186`,
    `tests/test_config_model.py`, `tests/test_routes_inventory.py`,
    `tests/test_config_store.py`.
  - bare `DispenseCommand(slot=...)` or raw `cmd/dispense` publishes:
    `tests/test_mqtt_messages_validation.py:35`, `tests/test_vmc_flows.py`,
    `tests/test_simulator_vending.py`, `tests/test_mqtt.py`,
    `tests/test_integration_e2e.py`.
  - A shared `tests/conftest.py` fixture providing a validated two-slot
    `DispenserProfiles` (one ice, one water) and a matching catalog is
    added so these tests change one import, not their logic.
- Boards speaking contract 0.7.0 that still subscribe to `cmd/dispense`
  receive nothing from the VMC after this change; the simulator is updated
  in the same work, and no physical board exists yet (Phase D unstarted),
  so there is no deployed consumer to break. The `DispenserStatus.state`
  strings a 0.7.0 board emits (`motor_active`, `fill_complete`,
  `solenoid_open`) are not in `DispenseStep`; the VMC keeps treating any
  non-outcome string as an intermediate step, so a stale simulator image
  still completes a sale.

## 10. Implementation phasing

One spec, three implementation plans, in dependency order. Each is
independently mergeable and leaves the suite green.

1. **Schema, validation, generator, CLI** — §4, §5, the `CFG-101`/
   `CFG-102` fault rows, `config.example.json` kinds, the shared test
   fixture. No runtime behaviour changes yet; `DispenserProfiles` loads at
   startup and only logs its report.
2. **Contract 0.8.0, simulator, VMC runtime** — §6 in full: faults
   reconciled, `select_product` refusals, `DispenseCommand` with profile,
   dispatcher wiring, step enum, new outcomes, simulator execution and
   fault injection, fixture updates from §9.
3. **Dashboard** — §7: the editor level, validate/save routes, report
   partial, read-only cards, Settings gate widening, CSS rebuild.

## 11. Out of scope (later work)

- ESP32 firmware (ROADMAP Phase D).
- Structured (form-based) editing of profiles.
- Raw motor jog / run-for-N-seconds / reverse from the Tests level.
- Per-slot run-count, run-seconds and peak-current telemetry and reports.
- Mechanisms other than bagged ice and water fill.

## 12. Implementation notes (plan 2)

Plan 2 shipped §6 in full (VMC runtime, contract additions, simulator
execution). This spec stays the design of record; the notes below record
where plan 2 shipped differently, and why, rather than letting the two
documents silently drift apart.

- **No contract version bump.** `CFG-101`/`CFG-102` (plan 1) and
  `DispenseCommand`/`DispenseStep`/the new `DispenserOutcome` members/
  `fault_for_outcome` (plan 2) all shipped under the same
  `contracts/vending_machine.py` 0.8.0 — the plan-2 additions are
  additive wiring on top of plan 1's schema, not a schema break, so they
  reuse its minor bump instead of taking their own.
- **`DispenserStatus.state` stays `str`, not an enum.** The same field
  carries both intermediate `DispenseStep` strings (`agitate`, `fill`,
  `release`) and the terminal `DispenserOutcome` strings over one sale's
  lifetime; typing it as either enum alone would make the other half of
  its values invalid against the schema.
- **The simulator's legacy `cmd/dispense` subscription was removed now,
  not kept for a deprecation window.** No physical ESP32 board exists yet
  (Phase D unstarted per §11), so there was no deployed consumer that
  could be broken by removing it immediately.
- **The command dispatcher reports a `door_open` completion as
  `status="failed"`**, with a `detail` of "bag released but door did not
  close", even though the VMC itself treats `door_open` as a customer
  success plus a fault (it records the sale and raises `ICE-402`). The
  dispatcher's `CommandAck.status` vocabulary has no third state between
  "ok" and "failed" to express "succeeded, but also faulted," so a
  `/tests` operator reading the ack sees "failed" for what the customer
  path treats as a completed vend.
- **Flow-meter pulses are declared on their own `fill_pulses` channel, not
  on `water_flow`.** `water_flow` stays `unit="gal"`, matching the
  cumulative-gallons reading `_publish_sensors` already publishes on
  `sensors/water_flow` (the Home Assistant `water_flow_total` entity);
  the per-fill pulse count the water-fill mechanism simulates (`unit=
  "pulses"`) is a distinct channel so a subsystem window never shows
  gallons under a "pulses" label or vice versa (review finding I1,
  whole-branch review). `fill_pulses`, `agitator_current` and
  `auger_current` are all published on `telemetry/vending/<id>`, which the
  VMC does not yet subscribe to (§11 defers per-slot telemetry) — their
  subsystem-window rows show "never reported" for now.
- **`slow_flow` is an added simulator fault**, beyond the outcomes §6.4
  enumerates: it halves the flow rate so only half the target volume is
  reached by `max_fill_seconds`, which is the only path by which
  `_run_water_fill` returns `timeout` — no other fault drives that
  outcome for the water-fill mechanism.
- **`SessionSnapshot.dispense_mechanism` was added**, additive beyond
  what this spec asked for, so a crash-recovery snapshot records which
  mechanism was mid-dispense without the recovery flow having to
  re-derive it from the (possibly since-changed) dispenser profile.
- **`clear_fault` re-asserts `CFG-101`.** Popping any lockout for a sku
  (not only `CFG-101` itself) re-checks `dispenser_profile_for` and
  re-raises `CFG-101` immediately if the product still has no valid
  profile, so clearing an unrelated fault (e.g. `ICE-301`) on a
  profile-less product can never leave it sellable — `CFG-101` is a
  standing invariant, not a one-shot check at reconciliation time.
- **A board reporting an outcome unmapped for its own mechanism** (e.g. a
  water board sending `jam`) is caught as `KeyError` from
  `fault_for_outcome` in `_handle_mqtt_dispenser` and falls back to that
  mechanism's `error` mapping instead of crashing the MQTT handler.
- **`_sale_seq`, a monotonically increasing counter bumped once per
  `on_dispense_product` call, guards every async dispatch-failure path**
  (`_fail_dispense_async` compares its captured `seq` against the live
  counter and the FSM's current state before acting), and
  `_persist_then_dispense`'s "no dispatcher"/`send()`/ack-status checks
  are each guarded (the snapshot save under its own `snapshot_failed` outcome, the dispatch under `no_ack`) so any failure there — not only
  a `CommandTimeout` — fails the vend immediately instead of waiting out
  the full dispense-timeout fallback.
- **`_customer_loop` lost its impatient-customer timeout and
  repeat-customer purchase.** Both depended on the legacy dispense queue
  removed by this plan; the loop now only generates button-press traffic
  (plus the occasional change-of-mind second press) and never itself
  waits on or runs a dispense.
