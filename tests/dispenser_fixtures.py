# tests/dispenser_fixtures.py
"""Shared fixtures for the dispenser-profiles tests (plan: dispenser
profiles). `ICE`/`WATER` is the two-product catalog and `GOOD` the matching
`dispensers.toml` text used by `test_dispensers_validation.py`,
`test_dispensers_service.py` and the `dispenser_profiles` fixture in
`tests/conftest.py`. `render_profiles_toml`/`profiles_for` (plan 2, Task 2)
build a `dispensers.toml` keyed by arbitrary products' own slots, for the
VMC-level reconciliation tests in `tests/test_vmc_dispense_profiles.py`.
Plain module -- no pytest import -- so it can be imported from anywhere
without pytest collecting it as a test module.
"""

import uuid
from collections.abc import Sequence
from pathlib import Path

from contracts.common import CommandAck
from config.config_model import ConfigModel, PhysicalDetails, Product
from services.dispensers import DispenserProfiles

ICE = Product(sku="ICE-10LB", slot=1, kind="ice")
WATER = Product(sku="WATER-1GAL", slot=2, kind="water")

GOOD = """\
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
"""


def _ice_table(slot: int, sku: str) -> str:
    """A minimal valid bagged-ice `[slot.N]` table: sensor proofs (not
    timed), `"unmonitored"` current throughout, no accessories."""
    return f"""[slot.{slot}]
mechanism   = "bagged_ice"
product_sku = "{sku}"

[slot.{slot}.agitate]
motor_channel      = "agitator_motor"
run_seconds        = 4.0
stall_current_amps = "unmonitored"
current_channel    = "unmonitored"

[slot.{slot}.fill]
motor_channel      = "auger_motor"
proof              = "bag_full_sensor"
sensor_channel     = "bag_full_sensor"
max_run_seconds    = 25.0
stall_current_amps = "unmonitored"
current_channel    = "unmonitored"

[slot.{slot}.release]
solenoid_channel      = "bag_drop_solenoid"
proof                 = "door_sensor"
sensor_channel        = "door_sensor"
pulse_seconds         = 1.5
open_timeout_seconds  = 3.0
close_timeout_seconds = 5.0
"""


def _water_table(slot: int, sku: str) -> str:
    """A minimal valid water `[slot.N]` table: `flow_volume` proof, the
    `GOOD` numbers."""
    return f"""[slot.{slot}]
mechanism   = "water_fill"
product_sku = "{sku}"

[slot.{slot}.fill]
valve_channel          = "water_valve_solenoid"
proof                  = "flow_volume"
flow_sensor_channel    = "water_flow_sensor"
target_volume_ml       = 3785
pulses_per_liter       = 450.0
min_flow_ml_per_second = 20.0
no_flow_grace_seconds  = 3.0
over_dispense_percent  = 10.0
max_fill_seconds       = 90.0
"""


def render_profiles_toml(products: Sequence[Product]) -> str:
    """Render a minimal valid `dispensers.toml` for `products`, one table
    per ice/water product keyed by that product's own `slot` (reusing
    `GOOD`'s numeric parameters) -- a product whose `kind` is `"other"`
    gets no table at all, same as one with no profile."""
    parts = ["schema_version = 1", ""]
    for product in products:
        if product.kind == "ice":
            parts.append(_ice_table(product.slot, product.sku))
        elif product.kind == "water":
            parts.append(_water_table(product.slot, product.sku))
    return "\n".join(parts) + "\n"


class FakeDispatcher:
    """Fake `services.command_dispatcher.CommandDispatcher` for VMC-level
    dispense tests (plan: dispenser profiles, Task 3). Records every
    `send()` call (subsystem, command, params) in `sent` -- never the
    pydantic `DispenseCommand`, matching what a production sale actually
    hands the real dispatcher (`cmd.model_dump(mode="json")`).

    `fail_with`, when set, is raised by the *next* `send()` call instead of
    returning an ack -- set it to a `CommandTimeout` to simulate a dead
    broker/subsystem, the one failure mode a production sale must survive
    by failing the vend with PAY-102.

    `send_and_await_completion` always raises `AssertionError`: a
    production sale must await only the accepted ack via `send()`, never
    completion -- completion is signalled by the real `hardware/dispenser`
    report, handled by `VMC._handle_mqtt_dispenser`, not the dispatcher.
    """

    def __init__(self):
        self.sent: list[tuple[str, str, dict]] = []
        self.fail_with: Exception | None = None
        self._last_request_id: str | None = None

    async def send(
        self, subsystem: str, command: str, params: dict | None = None
    ) -> CommandAck:
        self.sent.append((subsystem, command, params or {}))
        if self.fail_with is not None:
            raise self.fail_with
        ack = CommandAck(
            request_id=uuid.uuid4().hex,
            command=command,
            status="ok",
            phase="accepted",
        )
        self._last_request_id = ack.request_id
        return ack

    async def send_and_await_completion(self, *args, **kwargs) -> CommandAck:
        raise AssertionError("a sale must not await completion")

    @property
    def last_request_id(self) -> str | None:
        return self._last_request_id


def profiles_for(products: Sequence[Product], directory: Path) -> DispenserProfiles:
    """Write `directory / "dispensers.toml"` from `render_profiles_toml`,
    build a matching `ConfigModel`, and return an already-`load()`ed
    `DispenserProfiles`. Asserts `report.ok` so a fixture bug fails loudly
    rather than quietly producing a profile-less test."""
    path = directory / "dispensers.toml"
    path.write_text(render_profiles_toml(products), encoding="utf-8")
    config = ConfigModel(physical=PhysicalDetails(products=list(products)))
    profiles = DispenserProfiles(config, path=path)
    report = profiles.load()
    assert report.ok, report.render_text()
    return profiles
