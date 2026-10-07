# tests/dispenser_fixtures.py
"""Shared fixtures for the dispenser-profiles tests (plan: dispenser
profiles). `ICE`/`WATER` is the two-product catalog and `GOOD` the matching
`dispensers.toml` text used by `test_dispensers_validation.py`,
`test_dispensers_service.py` and the `dispenser_profiles` fixture in
`tests/conftest.py`. Plain module -- no pytest import -- so it can be
imported from anywhere without pytest collecting it as a test module.
"""

from config.config_model import Product

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
