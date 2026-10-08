# tests/test_simulator_vending.py
"""Tests for simulators/vending_machine.py — vending interface simulation."""

import asyncio
import json
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest
from pydantic import ValidationError

from config.config_model import ConfigModel
from contracts.common import SubsystemCommand
from contracts.vending_machine import DispenserOutcome, SubsystemCapabilities
from services.dispenser_schema import (
    Accessory,
    AgitateStep,
    BaggedIceProfile,
    IceFillTimed,
    ReleaseTimed,
    WaterFillProfile,
    WaterFillTimed,
)
from services.dispensers import validate_document
from services.mqtt_messages import DispenseCommand
from simulators.vending_machine import VendingMachineSimulator
from tests.dispenser_fixtures import GOOD, ICE, WATER


_REAL_SLEEP = asyncio.sleep  # captured before any test patches asyncio.sleep


class FakeClock:
    """A fake `sim._sleep` that advances a cumulative `now` by exactly the
    requested duration on every call, synchronously, instead of actually
    waiting -- lets a test assert *when* within a run something happened
    (e.g. "the terminal report went out at t=1.0, the lagged accessory
    turned off at t=3.0") without real wall-clock delay. Assign an
    instance directly to `sim._sleep` the same way other tests here
    assign a plain async function or an `AsyncMock`."""

    def __init__(self) -> None:
        self.now = 0.0

    async def __call__(self, seconds: float) -> None:
        self.now += seconds


async def _drain_background(sim, timeout: float = 2.0) -> None:
    """Wait for every task `sim._spawn_background` currently holds to
    finish (completion-table amendment, 2026-09-29): `_handle_dispense` and
    `_handle_water_valve` now ack "accepted" and return before their real
    work is done, spawning it as a background task instead — a test that
    wants to observe the work's outcome (hardware state, publish calls,
    mock call counts) must let that task actually run first. Tasks remove
    themselves from `sim._background_tasks` on completion (see
    `_on_background_task_done`), so an empty set means every spawned task
    up to this point has settled.

    Uses `_REAL_SLEEP`, not `asyncio.sleep`, to yield control: a caller
    that also patches `simulators.vending_machine.asyncio.sleep` (the
    water_valve tests in this file) needs this real-sleep workaround for
    the same reason explained historically here; profile-execution tests
    patch `sim._sleep` instead (an instance attribute), which leaves the
    module-global `asyncio.sleep` untouched, so this real sleep is not
    strictly required there but is harmless and kept for one shared
    helper.
    """
    async with asyncio.timeout(timeout):
        while sim._background_tasks:
            await _REAL_SLEEP(0)


def _make_config() -> ConfigModel:
    """Build a 3-product config matching the real machine layout."""
    return ConfigModel.model_validate(
        {
            "physical": {
                "products": [
                    {"sku": "Ten Pounds Ice", "name": "Bagged Ice", "price": 3.00},
                    {"sku": "One Gallon Water", "name": "Small Water", "price": 0.50},
                    {"sku": "Five Gallons Water", "name": "Large Water", "price": 2.00},
                ]
            }
        }
    )


def _make_sim(**kwargs) -> VendingMachineSimulator:
    return VendingMachineSimulator(config=_make_config(), **kwargs)


# Profiles reused across the fault/step-sequence tests below, built from
# the shared fixture TOML (tests/dispenser_fixtures.py) rather than
# hand-rolled models: `GOOD`'s slot 1 (bagged ice, bag_full_sensor +
# door_sensor proofs, both accessories) and slot 2 (water, flow_volume
# proof) are the only fixtures with the exact shapes this task's tests
# need, and validating them here is itself a free consistency check that
# the new channels (bag_fan, vending_now_light, door_sensor) actually
# match what GOOD references.
_GOOD_REPORT = validate_document(GOOD, [ICE, WATER])
assert _GOOD_REPORT.ok, _GOOD_REPORT.render_text()
ICE_PROFILE = _GOOD_REPORT.profiles[1]
WATER_PROFILE = _GOOD_REPORT.profiles[2]

_ICE_FAULTS = {
    "motor_stall",
    "auger_jam",
    "bag_drop_solenoid_stuck",
    "door_stuck_open",
    "ice_bin_empty",
}


def _dispense_params(slot: int, mechanism: str, profile) -> dict:
    return DispenseCommand(slot=slot, mechanism=mechanism, profile=profile).model_dump(
        mode="json"
    )


def _make_command(command: str, params: dict, request_id: str = "req-00000001"):
    """Build a SubsystemCommand, bypassing the model-level param validator.

    Real inbound traffic goes through `SubsystemCommand.model_validate`,
    which already enforces `COMMAND_PARAM_VALIDATORS` (e.g. water_valve's
    1-10 range, dispense's full `DispenseCommand` shape) and raises before
    an invalid command can even be constructed. `model_construct` skips
    that validator so tests can exercise a handler's own defense-in-depth
    check directly (and build a deliberately-bare `{"slot": n}` payload
    for the rejection test), while every in-range/valid command here still
    matches exactly what `_command_loop` would have built.
    """
    return SubsystemCommand.model_construct(
        request_id=request_id,
        command=command,
        params=params,
        timestamp=datetime.now(timezone.utc),
    )


class TestInit:
    def test_creates_with_config(self):
        sim = _make_sim()
        assert sim.subsystem_name == "vending"
        assert sim.num_buttons == 3

    def test_single_product_config(self):
        config = ConfigModel.model_validate(
            {"physical": {"products": [{"sku": "T-1", "name": "Test", "price": 1.0}]}}
        )
        sim = VendingMachineSimulator(config=config)
        assert sim.num_buttons == 1

    def test_hardware_state_initialized(self):
        sim = _make_sim()
        assert sim._hw["auger_motor"] is False
        assert sim._hw["agitator_motor"] is False
        assert sim._hw["fan"] is False
        assert sim._hw["bag_full_sensor"] is False
        assert sim._hw["bag_drop_solenoid"] is False
        assert sim._hw["water_valve_solenoid"] is False
        assert sim._hw["water_flow_sensor"] is False
        assert sim._hw["bin_half_full"] is True
        assert sim._hw["heater_relay"] is False
        assert sim._hw["bag_fan"] is False
        assert sim._hw["vending_now_light"] is False
        assert sim._hw["door_sensor"] is False

    def test_cabinet_temp_initialized(self):
        sim = _make_sim()
        assert sim._cabinet_temp == 22.0

    def test_water_flow_starts_at_zero(self):
        sim = _make_sim()
        assert sim._water_flow_total == 0.0


class TestButtonSelection:
    def test_random_button_in_range(self):
        sim = _make_sim()
        buttons = {sim._pick_button() for _ in range(100)}
        assert buttons.issubset({0, 1, 2})
        assert len(buttons) > 1  # should hit at least 2 of 3


class TestHADiscovery:
    def test_returns_12_entities(self):
        sim = _make_sim()
        entities = sim.ha_discovery_entities()
        assert len(entities) == 12

    def test_binary_sensor_count(self):
        sim = _make_sim()
        entities = sim.ha_discovery_entities()
        binary = [e for e in entities if e["component"] == "binary_sensor"]
        assert len(binary) == 9

    def test_binary_sensor_names(self):
        sim = _make_sim()
        entities = sim.ha_discovery_entities()
        binary_ids = {
            e["object_id"] for e in entities if e["component"] == "binary_sensor"
        }
        expected = {
            "auger_motor",
            "agitator_motor",
            "fan",
            "bag_full_sensor",
            "bag_drop_solenoid",
            "water_valve_solenoid",
            "water_flow_sensor",
            "bin_half_full",
            "heater_relay",
        }
        assert binary_ids == expected

    def test_cabinet_temp_sensor(self):
        sim = _make_sim()
        entities = sim.ha_discovery_entities()
        temp = next(e for e in entities if e["object_id"] == "cabinet_temp")
        assert temp["component"] == "sensor"
        assert temp["device_class"] == "temperature"
        assert temp["unit_of_measurement"] == "°C"
        assert temp["state_topic_suffix"] == "sensors/temp/cabinet"

    def test_water_flow_sensor(self):
        sim = _make_sim()
        entities = sim.ha_discovery_entities()
        flow = next(e for e in entities if e["object_id"] == "water_flow_total")
        assert flow["component"] == "sensor"
        assert flow["device_class"] == "water"
        assert flow["unit_of_measurement"] == "gal"
        assert flow["state_class"] == "total_increasing"

    def test_uptime_sensor(self):
        sim = _make_sim()
        entities = sim.ha_discovery_entities()
        uptime = next(e for e in entities if e["object_id"] == "uptime")
        assert uptime["component"] == "sensor"
        assert uptime["name"] == "Vending Machine Uptime"
        assert uptime["device_class"] == "duration"
        assert uptime["state_topic_suffix"] == "heartbeat/vending"

    def test_all_object_ids_unique(self):
        sim = _make_sim()
        entities = sim.ha_discovery_entities()
        ids = [e["object_id"] for e in entities]
        assert len(ids) == len(set(ids))


class TestVendingFaultRegistration:
    def test_nine_faults_registered(self):
        sim = VendingMachineSimulator()
        assert len(sim._fault_defs) == 9

    def test_fault_names(self):
        sim = VendingMachineSimulator()
        names = {f.name for f in sim._fault_defs}
        assert names == {
            "auger_jam",
            "bag_drop_solenoid_stuck",
            "water_valve_stuck_open",
            "ice_bin_empty",
            "motor_stall",
            "no_water_flow",
            "door_stuck_open",
            "flow_runaway",
            "slow_flow",
        }


class TestAugerJamFault:
    @pytest.mark.asyncio
    async def test_fault_is_registered(self):
        sim = VendingMachineSimulator()
        names = {f.name for f in sim._fault_defs}
        assert "auger_jam" in names

    @pytest.mark.asyncio
    async def test_recover_logs_and_does_not_crash(self):
        sim = VendingMachineSimulator()
        client = AsyncMock()
        # Should complete without error
        await sim._on_auger_jam_recover(client)


class TestWaterValveStuckOpenFault:
    @pytest.mark.asyncio
    async def test_activate_sets_valve_and_flow_sensor_on(self):
        sim = VendingMachineSimulator()
        set_hw_calls = []

        async def capture_set_hw(client, device, state):
            set_hw_calls.append((device, state))

        sim._set_hw = capture_set_hw
        client = AsyncMock()
        await sim._on_water_valve_stuck_open_activate(client)
        assert ("water_valve_solenoid", True) in set_hw_calls
        assert ("water_flow_sensor", True) in set_hw_calls

    @pytest.mark.asyncio
    async def test_recover_closes_valve_and_flow_sensor(self):
        sim = VendingMachineSimulator()
        set_hw_calls = []

        async def capture_set_hw(client, device, state):
            set_hw_calls.append((device, state))

        sim._set_hw = capture_set_hw
        client = AsyncMock()
        await sim._on_water_valve_stuck_open_recover(client)
        assert ("water_valve_solenoid", False) in set_hw_calls
        assert ("water_flow_sensor", False) in set_hw_calls


class TestIceBinEmptyFault:
    @pytest.mark.asyncio
    async def test_activate_sets_bin_half_full_false(self):
        sim = VendingMachineSimulator()
        set_hw_calls = []

        async def capture_set_hw(client, device, state):
            set_hw_calls.append((device, state))
            sim._hw[device] = state

        sim._set_hw = capture_set_hw
        client = AsyncMock()
        await sim._on_ice_bin_empty_activate(client)
        assert ("bin_half_full", False) in set_hw_calls

    @pytest.mark.asyncio
    async def test_recover_restores_bin_half_full(self):
        sim = VendingMachineSimulator()
        set_hw_calls = []

        async def capture_set_hw(client, device, state):
            set_hw_calls.append((device, state))

        sim._set_hw = capture_set_hw
        client = AsyncMock()
        await sim._on_ice_bin_empty_recover(client)
        assert ("bin_half_full", True) in set_hw_calls


class TestZeroProducts:
    @pytest.mark.asyncio
    async def test_customer_loop_with_no_products_does_not_crash(self):
        sim = VendingMachineSimulator(config=ConfigModel())
        assert sim.num_buttons == 0
        client = AsyncMock()
        sim.publish = AsyncMock()
        # The zero-products branch reloads config every iteration; keep it
        # deterministic (still zero products) rather than hitting the real
        # filesystem/env var.
        sim.load_config = lambda path: ConfigModel()
        sleep_calls = []

        async def fake_sleep(seconds):
            sleep_calls.append(seconds)
            if len(sleep_calls) >= 3:
                raise asyncio.CancelledError

        with patch("asyncio.sleep", new=fake_sleep):
            with pytest.raises(asyncio.CancelledError):
                await sim._customer_loop(client)

        assert len(sleep_calls) == 3
        sim.publish.assert_not_called()

    @pytest.mark.asyncio
    async def test_customer_loop_logs_no_products_once(self):
        sim = VendingMachineSimulator(config=ConfigModel())
        client = AsyncMock()
        sim.publish = AsyncMock()
        sim.load_config = lambda path: ConfigModel()
        sleep_calls = []

        async def fake_sleep(seconds):
            sleep_calls.append(seconds)
            if len(sleep_calls) >= 5:
                raise asyncio.CancelledError

        with patch("asyncio.sleep", new=fake_sleep):
            with patch("simulators.vending_machine.logger") as mock_logger:
                with pytest.raises(asyncio.CancelledError):
                    await sim._customer_loop(client)
                assert mock_logger.warning.call_count == 1

    @pytest.mark.asyncio
    async def test_customer_loop_reloads_config_and_picks_up_new_products(self):
        sim = VendingMachineSimulator(config=ConfigModel())
        assert sim.num_buttons == 0
        client = AsyncMock()
        sim.publish = AsyncMock()

        first_config = ConfigModel()
        second_config = ConfigModel.model_validate(
            {
                "physical": {
                    "products": [
                        {"sku": "A", "name": "Bagged Ice", "price": 1.0, "slot": 0},
                        {"sku": "B", "name": "Small Water", "price": 1.0, "slot": 1},
                    ]
                }
            }
        )
        call_results = iter([first_config, second_config, second_config, second_config])
        sim.load_config = lambda path: next(call_results)

        sleep_calls = []

        async def fake_sleep(seconds):
            sleep_calls.append(seconds)
            if len(sleep_calls) >= 2:
                raise asyncio.CancelledError

        with patch("asyncio.sleep", new=fake_sleep):
            with pytest.raises(asyncio.CancelledError):
                await sim._customer_loop(client)

        assert sim.num_buttons == 2


class TestCustomerBehaviours:
    def test_arrival_factor_peak_morning(self):
        sim = VendingMachineSimulator()
        factor = sim._arrival_factor(hour=12)
        assert factor == 0.5

    def test_arrival_factor_peak_evening(self):
        sim = VendingMachineSimulator()
        factor = sim._arrival_factor(hour=18)
        assert factor == 0.5

    def test_arrival_factor_overnight(self):
        sim = VendingMachineSimulator()
        factor = sim._arrival_factor(hour=3)
        assert factor == 2.0

    def test_arrival_factor_normal(self):
        sim = VendingMachineSimulator()
        factor = sim._arrival_factor(hour=9)
        assert factor == 1.0

    def test_fault_aware_idle_time_shortened(self):
        sim = VendingMachineSimulator()
        sim._fault_state["auger_jam"] = {"active": True, "recover_at": 9e9}
        idle = sim._compute_idle_time()
        # Must be within 5-15 range (fault-aware)
        assert 5.0 <= idle <= 15.0

    def test_normal_idle_time_in_range(self):
        sim = VendingMachineSimulator()
        for _ in range(50):
            idle = sim._compute_idle_time(hour=9)
            assert sim.IDLE_MIN <= idle <= sim.IDLE_MAX


class TestVendingCapabilities:
    def test_commands_and_contract(self):
        """Copilot review (PR 22, id=4128088689): ping/self_test/force_report
        are registered (base ESP32Simulator.__init__) and must be
        advertised -- prepended by build_capabilities, ahead of this
        subclass's own SUPPORTED_COMMANDS -- or the Tests routes'
        advertised-∩-allowlist intersection drops the automatic tests."""
        caps = _make_sim().build_capabilities()
        assert caps.subsystem == "vending"
        assert caps.commands == [
            "ping",
            "self_test",
            "force_report",
            "dispense",
            "water_valve",
            "payment/enable",
        ]
        assert caps.contract_version == "0.8.0"

    def test_channels_match_spec_table_in_order(self):
        """Spec §4.2's eleven-row vending table, copied exactly, in
        declaration order, plus the five dispenser-profile channels (plan:
        dispenser profiles, Task 5) appended at the end -- the dashboard
        renders channels in whatever order build_capabilities lists them."""
        caps = _make_sim().build_capabilities()
        ids = [c.channel_id for c in caps.channels]
        assert ids == [
            "cabinet",
            "water_flow",
            "bag_full_sensor",
            "water_flow_sensor",
            "bin_half_full",
            "auger_motor",
            "agitator_motor",
            "bag_drop_solenoid",
            "water_valve_solenoid",
            "fan",
            "heater_relay",
            "bag_fan",
            "vending_now_light",
            "door_sensor",
            "agitator_current",
            "auger_current",
        ]

    def test_channel_directions_match_hardware_role(self):
        """Every HARDWARE_DEVICES key must be a declared binary channel
        (guards a device added to the sim but never declared), and the
        output set is exactly the eight actuators -- never the sensors."""
        from simulators.vending_machine import HARDWARE_DEVICES

        caps = _make_sim().build_capabilities()
        by_id = {c.channel_id: c for c in caps.channels}
        assert set(HARDWARE_DEVICES) <= set(by_id)
        for device in HARDWARE_DEVICES:
            assert by_id[device].kind == "binary"

        outputs = {c.channel_id for c in caps.channels if c.direction == "output"}
        assert outputs == {
            "auger_motor",
            "agitator_motor",
            "bag_drop_solenoid",
            "water_valve_solenoid",
            "fan",
            "heater_relay",
            "bag_fan",
            "vending_now_light",
        }

    def test_driven_by_matches_spec_table(self):
        caps = _make_sim().build_capabilities()
        driven_by = {c.channel_id: c.driven_by for c in caps.channels}
        assert driven_by == {
            "cabinet": None,
            "water_flow": None,
            "bag_full_sensor": None,
            "water_flow_sensor": None,
            "bin_half_full": None,
            "auger_motor": "dispense",
            "agitator_motor": "dispense",
            "bag_drop_solenoid": "dispense",
            "water_valve_solenoid": "water_valve",
            "fan": None,
            "heater_relay": None,
            "bag_fan": "dispense",
            "vending_now_light": "dispense",
            "door_sensor": None,
            "agitator_current": None,
            "auger_current": None,
        }

    def test_channel_intervals(self):
        """Analog channels publish on SENSOR_PUBLISH_INTERVAL; every binary
        channel (and the two current channels) is declared at 1.0s per the
        brief."""
        from simulators.vending_machine import SENSOR_PUBLISH_INTERVAL

        caps = _make_sim().build_capabilities()
        by_id = {c.channel_id: c for c in caps.channels}
        assert by_id["cabinet"].interval_seconds == SENSOR_PUBLISH_INTERVAL
        assert by_id["water_flow"].interval_seconds == SENSOR_PUBLISH_INTERVAL
        for channel_id in (
            "bag_full_sensor",
            "water_flow_sensor",
            "bin_half_full",
            "auger_motor",
            "agitator_motor",
            "bag_drop_solenoid",
            "water_valve_solenoid",
            "fan",
            "heater_relay",
            "bag_fan",
            "vending_now_light",
            "door_sensor",
            "agitator_current",
            "auger_current",
        ):
            assert by_id[channel_id].interval_seconds == 1.0


class TestExecuteProfile:
    """`_execute_profile` -- the profile-driven replacement for
    `_dispense_slot`/`_run_ice_dispense`/`_run_water_dispense`."""

    @pytest.mark.asyncio
    async def test_bagged_ice_profile_step_sequence_and_io(self):
        sim = _make_sim()
        sim._sleep = AsyncMock()
        client = AsyncMock()
        states = []
        hw_events = []

        async def capture_publish(c, topic, payload):
            if topic == "hardware/dispenser" and hasattr(payload, "state"):
                states.append(payload.state)
            elif topic.startswith("hardware/io/"):
                hw_events.append((payload.device, payload.state))

        sim.publish = capture_publish
        cmd = DispenseCommand(slot=1, mechanism="bagged_ice", profile=ICE_PROFILE)

        await sim._execute_profile(client, cmd, request_id="req-ice-1")

        assert states == ["agitate", "fill", "release", "complete"]

        assert ("agitator_motor", True) in hw_events
        assert ("auger_motor", True) in hw_events
        assert ("bag_drop_solenoid", True) in hw_events

        # bag_fan's on_during is ["fill"] -- it must turn on before the
        # fill step's own motor does.
        fan_on_idx = hw_events.index(("bag_fan", True))
        auger_on_idx = hw_events.index(("auger_motor", True))
        assert fan_on_idx < auger_on_idx
        assert ("bag_fan", False) in hw_events

        # vending_now_light's on_during is ["all"] -- on for the whole
        # run, so it is the very first hardware/io event.
        assert hw_events[0] == ("vending_now_light", True)
        assert ("vending_now_light", False) in hw_events

        # Every output this run drove ends up off.
        assert ("agitator_motor", False) in hw_events
        assert ("auger_motor", False) in hw_events
        assert ("bag_drop_solenoid", False) in hw_events

    @pytest.mark.asyncio
    async def test_timed_fill_runs_exactly_max_run_seconds(self):
        sim = _make_sim()
        durations = []

        async def fake_sleep(seconds):
            durations.append(seconds)

        sim._sleep = fake_sleep
        client = AsyncMock()
        sim.publish = AsyncMock()

        profile = BaggedIceProfile(
            mechanism="bagged_ice",
            product_sku="ICE-TIMED",
            agitate=AgitateStep(
                motor_channel="agitator_motor",
                run_seconds=3.0,
                stall_current_amps="unmonitored",
                current_channel="unmonitored",
            ),
            fill=IceFillTimed(
                proof="timed",
                motor_channel="auger_motor",
                max_run_seconds=17.0,
                stall_current_amps="unmonitored",
                current_channel="unmonitored",
            ),
            release=ReleaseTimed(
                proof="timed", solenoid_channel="bag_drop_solenoid", pulse_seconds=1.0
            ),
        )
        cmd = DispenseCommand(slot=3, mechanism="bagged_ice", profile=profile)

        await sim._execute_profile(client, cmd)

        assert 17.0 in durations

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "fault,expected",
        [
            ("motor_stall", DispenserOutcome.error),
            ("auger_jam", DispenserOutcome.timeout),
            ("bag_drop_solenoid_stuck", DispenserOutcome.jam),
            ("door_stuck_open", DispenserOutcome.door_open),
            ("no_water_flow", DispenserOutcome.no_flow),
            ("flow_runaway", DispenserOutcome.over_dispense),
            ("slow_flow", DispenserOutcome.timeout),
            ("ice_bin_empty", DispenserOutcome.bin_empty),
        ],
    )
    async def test_injected_fault_yields_outcome(self, fault, expected):
        sim = _make_sim()
        sim._sleep = AsyncMock()
        client = AsyncMock()
        terminal = []

        async def capture_publish(c, topic, payload):
            if topic == "hardware/dispenser" and hasattr(payload, "state"):
                terminal.append(payload.state)

        sim.publish = capture_publish
        sim._fault_state[fault]["active"] = True

        if fault in _ICE_FAULTS:
            cmd = DispenseCommand(slot=1, mechanism="bagged_ice", profile=ICE_PROFILE)
        else:
            cmd = DispenseCommand(slot=2, mechanism="water_fill", profile=WATER_PROFILE)

        await sim._execute_profile(client, cmd)

        assert DispenserOutcome(terminal[-1]) is expected

        if fault == "ice_bin_empty":
            assert terminal == ["bin_empty"]

    @pytest.mark.asyncio
    async def test_water_fill_by_volume_stops_within_over_dispense_percent(self):
        """I3: the pulse model -- the flow meter's cumulative pulse count,
        converted back to a volume via `pulses_per_liter`, must land
        within `over_dispense_percent` of `target_volume_ml`, published
        in at least four increments, and the run must finish well short
        of `max_fill_seconds`."""
        sim = _make_sim()
        durations = []

        async def fake_sleep(seconds):
            durations.append(seconds)

        sim._sleep = fake_sleep
        client = AsyncMock()
        terminal = []
        flow_readings = []

        async def capture_publish(c, topic, payload):
            if topic == "hardware/dispenser" and hasattr(payload, "state"):
                terminal.append(payload.state)
            elif topic == "telemetry/vending/water_flow":
                flow_readings.append(payload.value)

        sim.publish = capture_publish
        cmd = DispenseCommand(slot=2, mechanism="water_fill", profile=WATER_PROFILE)

        await sim._execute_profile(client, cmd)

        assert DispenserOutcome(terminal[-1]) is DispenserOutcome.complete
        assert len(flow_readings) >= 4

        fill = WATER_PROFILE.fill
        final_volume_ml = flow_readings[-1] / fill.pulses_per_liter * 1000.0
        tolerance_ml = fill.target_volume_ml * (1 + fill.over_dispense_percent / 100.0)
        assert final_volume_ml == pytest.approx(fill.target_volume_ml, rel=0.01)
        assert final_volume_ml <= tolerance_ml

        # It stopped well short of the max timeout -- i.e. within the
        # over_dispense tolerance rather than running the full window.
        assert sum(durations) < fill.max_fill_seconds

    @pytest.mark.asyncio
    async def test_flow_runaway_exceeds_over_dispense_percent(self):
        """I3: with `flow_runaway` injected, the simulated flow meter
        keeps pulsing past the target until the dispensed volume exceeds
        `over_dispense_percent`'s tolerance -- proved from the terminal
        report's own `detail`, not just the outcome name."""
        sim = _make_sim()
        sim._sleep = AsyncMock()
        client = AsyncMock()
        terminal = []

        async def capture_publish(c, topic, payload):
            if topic == "hardware/dispenser" and hasattr(payload, "state"):
                terminal.append((payload.state, payload.detail))

        sim.publish = capture_publish
        sim._fault_state["flow_runaway"]["active"] = True
        cmd = DispenseCommand(slot=2, mechanism="water_fill", profile=WATER_PROFILE)

        await sim._execute_profile(client, cmd)

        state, detail = terminal[-1]
        assert state == "over_dispense"
        dispensed_ml = float(detail.split()[0])

        fill = WATER_PROFILE.fill
        tolerance_ml = fill.target_volume_ml * (1 + fill.over_dispense_percent / 100.0)
        assert dispensed_ml > tolerance_ml

    @pytest.mark.asyncio
    async def test_accessory_lag_runs_after_last_step_on_success(self):
        """I1: `lag_seconds` must not be defeated by an unconditional
        cancellation of every pending accessory-off task -- an accessory
        spanning the whole run (`["all"]`) turns off `lag_seconds` after
        the run completes, while the terminal `complete` report goes out
        the moment the fill step itself finishes, not after the lag."""
        sim = _make_sim()
        clock = FakeClock()
        sim._sleep = clock
        client = AsyncMock()
        events: list[tuple[str, object, float]] = []

        async def capture_publish(c, topic, payload):
            if topic == "hardware/dispenser" and hasattr(payload, "state"):
                events.append(("state", payload.state, clock.now))
            elif topic.startswith("hardware/io/"):
                events.append(("io", (payload.device, payload.state), clock.now))

        sim.publish = capture_publish

        fill = WaterFillTimed(
            proof="timed", valve_channel="water_valve_solenoid", max_fill_seconds=1.0
        )
        profile = WaterFillProfile(
            mechanism="water_fill",
            product_sku="WATER-LAG",
            fill=fill,
            accessories={
                "light": Accessory(
                    channel="vending_now_light",
                    on_during=["all"],
                    lead_seconds=0.0,
                    lag_seconds=2.0,
                )
            },
        )
        cmd = DispenseCommand(slot=9, mechanism="water_fill", profile=profile)

        await sim._execute_profile(client, cmd)

        complete_time = next(
            t for kind, val, t in events if kind == "state" and val == "complete"
        )
        light_off_time = next(
            t
            for kind, val, t in events
            if kind == "io" and val == ("vending_now_light", False)
        )

        assert complete_time == pytest.approx(1.0)
        assert light_off_time == pytest.approx(3.0)

    @pytest.mark.asyncio
    async def test_concurrent_accessory_leads_overlap(self):
        """M1: two accessories leading the same step overlap (one wait for
        the longer lead) instead of stacking (a wait per accessory)."""
        sim = _make_sim()
        clock = FakeClock()
        sim._sleep = clock
        client = AsyncMock()
        events: list[tuple[str, float]] = []

        async def capture_publish(c, topic, payload):
            if topic == "hardware/dispenser" and hasattr(payload, "state"):
                events.append((payload.state, clock.now))

        sim.publish = capture_publish

        fill = WaterFillTimed(
            proof="timed", valve_channel="water_valve_solenoid", max_fill_seconds=1.0
        )
        profile = WaterFillProfile(
            mechanism="water_fill",
            product_sku="WATER-LEADS",
            fill=fill,
            accessories={
                "a": Accessory(
                    channel="bag_fan",
                    on_during=["fill"],
                    lead_seconds=3.0,
                    lag_seconds=0.0,
                ),
                "b": Accessory(
                    channel="vending_now_light",
                    on_during=["fill"],
                    lead_seconds=2.0,
                    lag_seconds=0.0,
                ),
            },
        )
        cmd = DispenseCommand(slot=9, mechanism="water_fill", profile=profile)

        await sim._execute_profile(client, cmd)

        fill_time = next(t for state, t in events if state == "fill")
        assert fill_time == pytest.approx(3.0)

    @pytest.mark.asyncio
    async def test_bag_drop_solenoid_stuck_leaves_solenoid_on(self):
        """I2: `drive_off` used to run unconditionally before the fault
        check, so the IO sequence on `bag_drop_solenoid` was identical to
        a successful release. With the fault active, the solenoid must
        stay driven on (the last IO event for that channel is `True`) and
        the outcome must be `jam` with a detail describing the stuck
        solenoid; recovering the fault then publishes the solenoid off."""
        sim = _make_sim()
        sim._sleep = AsyncMock()
        client = AsyncMock()
        io_events = []
        terminal = []

        async def capture_publish(c, topic, payload):
            if topic == "hardware/dispenser" and hasattr(payload, "state"):
                terminal.append((payload.state, payload.detail))
            elif topic.startswith("hardware/io/"):
                io_events.append((payload.device, payload.state))

        sim.publish = capture_publish
        sim._fault_state["bag_drop_solenoid_stuck"]["active"] = True
        cmd = DispenseCommand(slot=1, mechanism="bagged_ice", profile=ICE_PROFILE)

        await sim._execute_profile(client, cmd)

        assert terminal[-1] == ("jam", "bag release solenoid stuck on")
        solenoid_events = [
            state for device, state in io_events if device == "bag_drop_solenoid"
        ]
        assert solenoid_events, "bag_drop_solenoid never drove on"
        assert solenoid_events[-1] is True

        io_events.clear()
        await sim._on_bag_drop_solenoid_stuck_recover(client)
        assert ("bag_drop_solenoid", False) in io_events

    @pytest.mark.asyncio
    async def test_failed_run_turns_every_accessory_off(self):
        """motor_stall aborts mid-agitate, before fill is ever reached --
        vending_now_light (on_during=["all"]) still turned on at the very
        start, and must still end up off immediately (no lag wait, since
        this run never reached a `complete` outcome). The two documented
        exceptions to "every accessory/output off" are `water_valve_stuck_open`
        (valve/flow sensor) and `bag_drop_solenoid_stuck` (release solenoid,
        see test_bag_drop_solenoid_stuck_leaves_solenoid_on) -- neither
        fault is active here, so they don't apply to this run."""
        sim = _make_sim()
        sim._sleep = AsyncMock()
        client = AsyncMock()
        hw_events = []

        async def capture_publish(c, topic, payload):
            if topic.startswith("hardware/io/"):
                hw_events.append((payload.device, payload.state))

        sim.publish = capture_publish
        sim._fault_state["motor_stall"]["active"] = True
        cmd = DispenseCommand(slot=1, mechanism="bagged_ice", profile=ICE_PROFILE)

        await sim._execute_profile(client, cmd)

        assert ("vending_now_light", True) in hw_events
        assert ("vending_now_light", False) in hw_events
        assert ("agitator_motor", False) in hw_events
        # bag_fan's on_during is ["fill"], never reached -- never even on.
        assert ("bag_fan", True) not in hw_events


class TestDispenseCommand:
    """Command-channel `dispense`: runs the slot's profile via
    `_execute_profile` through `_handle_command`."""

    @pytest.mark.asyncio
    async def test_runs_profile_once_and_publishes_hardware_dispenser(self):
        """Completion-table amendment (2026-09-29): `_handle_command`
        itself now returns as soon as the "accepted" ack is published —
        the real sequence runs in a background task
        (`_spawn_background`), so this drains it (`_drain_background`)
        before checking the motor ran and the terminal report went out.
        """
        sim = _make_sim()
        sim._sleep = AsyncMock()
        client = AsyncMock()
        dispenser_states = []

        async def capture_publish(c, topic, payload):
            if topic == "hardware/dispenser":
                dispenser_states.append(payload.state)

        sim.publish = capture_publish
        run_profile = AsyncMock(wraps=sim._execute_profile)
        sim._execute_profile = run_profile

        cmd = _make_command("dispense", _dispense_params(1, "bagged_ice", ICE_PROFILE))
        ack = await sim._handle_command(client, cmd)
        await _drain_background(sim)

        assert run_profile.await_count == 1
        assert dispenser_states == ["agitate", "fill", "release", "complete"]
        assert ack is None  # _handle_command publishes the ack, doesn't return it

    @pytest.mark.asyncio
    async def test_publishes_ok_ack_with_slot_and_mechanism_result(self):
        """This ack is the "accepted" one (published synchronously, before
        `_handle_command` returns) — the command's own completion is the
        terminal `hardware/dispenser` report, not a second ack.
        """
        sim = _make_sim()
        sim._sleep = AsyncMock()
        client = AsyncMock()
        published_acks = []

        async def capture_publish(c, topic, payload):
            if topic == "cmd/vending/ack":
                published_acks.append(payload)

        sim.publish = capture_publish
        cmd = _make_command("dispense", _dispense_params(1, "bagged_ice", ICE_PROFILE))
        await sim._handle_command(client, cmd)

        assert len(published_acks) == 1
        assert published_acks[0].status == "ok"
        assert published_acks[0].result == {"slot": 1, "mechanism": "bagged_ice"}
        assert published_acks[0].phase == "accepted"

        await _drain_background(sim)

    @pytest.mark.asyncio
    async def test_duplicate_request_id_runs_profile_only_once(self):
        """The most consequential duplicate in the system: a retried
        request_id must not run the dispense sequence a second time.
        """
        sim = _make_sim()
        sim._sleep = AsyncMock()
        client = AsyncMock()
        sim.publish = AsyncMock()
        run_profile = AsyncMock(wraps=sim._execute_profile)
        sim._execute_profile = run_profile

        cmd = _make_command(
            "dispense",
            _dispense_params(1, "bagged_ice", ICE_PROFILE),
            request_id="dup-req-1",
        )
        await sim._handle_command(client, cmd)
        await sim._handle_command(client, cmd)  # same request_id, sent again
        await _drain_background(sim)

        assert run_profile.await_count == 1

        # The two acks are identical (the second is the replayed cache entry).
        ack_calls = [
            call.args[2]
            for call in sim.publish.await_args_list
            if call.args[1] == "cmd/vending/ack"
        ]
        assert len(ack_calls) == 2
        assert ack_calls[0] == ack_calls[1]

    @pytest.mark.asyncio
    async def test_water_slot_runs_fill_only(self):
        sim = _make_sim()
        sim._sleep = AsyncMock()
        client = AsyncMock()
        dispenser_states = []

        async def capture_publish(c, topic, payload):
            if topic == "hardware/dispenser":
                dispenser_states.append(payload.state)

        sim.publish = capture_publish
        cmd = _make_command(
            "dispense", _dispense_params(2, "water_fill", WATER_PROFILE)
        )
        await sim._handle_command(client, cmd)
        await _drain_background(sim)

        assert dispenser_states == ["fill", "complete"]

    @pytest.mark.asyncio
    async def test_bare_slot_params_are_rejected(self):
        """A bare `{"slot": n}` payload (the pre-profile shape) fails
        `DispenseCommand.model_validate` inside `_handle_dispense` --
        acked "rejected", and nothing is ever spawned."""
        sim = _make_sim()
        client = AsyncMock()
        sim.publish = AsyncMock()
        cmd = _make_command("dispense", {"slot": 0})

        ack = await sim._handle_dispense(client, cmd)

        assert ack.status == "rejected"
        assert ack.detail
        assert sim._background_tasks == set()


class TestLegacyDispenseTopicRemoved:
    def test_legacy_cmd_dispense_topic_is_not_subscribed(self):
        """The VMC no longer publishes `cmd/dispense` at all -- the
        listener that fed `_dispense_command` and the queue itself are
        gone, not merely unused."""
        sim = _make_sim()
        assert not hasattr(sim, "_listen_for_commands")
        assert not hasattr(sim, "_dispense_command")

    def test_no_cmd_dispense_string_in_module(self):
        import simulators.vending_machine as module

        source = Path(module.__file__).read_text(encoding="utf-8")
        assert "cmd/dispense" not in source


class TestExampleToml:
    def test_example_toml_validates_with_zero_warnings_against_simulator_capabilities(
        self,
    ):
        """The simulator's own declared channels must be enough to clear
        every capabilities-cross-check warning for the shipped example --
        confirming the five new channels (bag_fan, vending_now_light,
        door_sensor, agitator_current, auger_current) are declared with
        the right direction for what the example actually drives/senses.
        """
        caps = SubsystemCapabilities(
            subsystem="vending",
            firmware="sim",
            contract_version="0.8.0",
            channels=VendingMachineSimulator.CHANNELS,
        )
        raw = json.loads(Path("config.example.json").read_text(encoding="utf-8"))
        config = ConfigModel.model_validate(raw)
        text = Path("dispensers.example.toml").read_text(encoding="utf-8")

        report = validate_document(text, config.products, capabilities=caps)

        assert report.errors == []
        assert report.warnings == []


class TestWaterValveCommand:
    """`water_valve` acks ok in [1, 10]; outside it, `_command_loop`
    (`simulators/base.py`) now acks "rejected" itself from the raw wire
    payload before a `SubsystemCommand` — and therefore `_handle_water_valve`
    — ever exists. `_handle_water_valve` has no defense-in-depth check of
    its own any more (see its docstring); the out-of-range cases below are
    proven over the real wire path (`_command_loop`), not by constructing an
    out-of-range command directly, since that construction now fails before
    reaching this simulator at all.
    """

    @pytest.mark.asyncio
    async def test_seconds_one_acks_ok(self):
        """This is the "accepted" ack -- published synchronously before
        `_handle_command` returns, before the background task that
        actually opens the valve has had any chance to run (nothing here
        yields control). Draining afterward (inside the patched-sleep
        `with` block) lets that background task finish and publish its own
        "completed" ack too, rather than leaking a pending task that would
        otherwise resume against the REAL `asyncio.sleep` once this `with`
        block exits.
        """
        sim = _make_sim()
        client = AsyncMock()
        sim.publish = AsyncMock()
        cmd = _make_command("water_valve", {"seconds": 1})
        with patch("simulators.vending_machine.asyncio.sleep", new=AsyncMock()):
            await sim._handle_command(client, cmd)

            ack = sim.publish.await_args_list[-1].args[2]
            assert ack.status == "ok"
            assert ack.result == {"seconds": 1}
            assert ack.phase == "accepted"

            await _drain_background(sim)

        completed_ack = sim.publish.await_args_list[-1].args[2]
        assert completed_ack.phase == "completed"
        assert completed_ack.result == {"seconds": 1}

    @pytest.mark.asyncio
    async def test_seconds_ten_acks_ok(self):
        sim = _make_sim()
        client = AsyncMock()
        sim.publish = AsyncMock()
        cmd = _make_command("water_valve", {"seconds": 10})
        with patch("simulators.vending_machine.asyncio.sleep", new=AsyncMock()):
            await sim._handle_command(client, cmd)

            ack = sim.publish.await_args_list[-1].args[2]
            assert ack.status == "ok"

            await _drain_background(sim)  # see test_seconds_one_acks_ok

    @staticmethod
    async def _start_command_loop(sim, client):
        task = asyncio.create_task(sim._command_loop(client))
        await asyncio.sleep(0.02)
        topic, queue = next(
            (t, q)
            for t, q in sim._subscriptions
            if t == f"{sim.topic_prefix}/cmd/vending"
        )
        return task, topic, queue

    @pytest.mark.asyncio
    async def test_seconds_zero_over_the_wire_acks_rejected_by_the_loop(self):
        """Out-of-range `seconds` reaches this simulator only as a raw wire
        payload now — `_command_loop` (`simulators/base.py`) acks
        "rejected" itself before a `SubsystemCommand`, and therefore
        `_handle_water_valve`, ever exists."""
        sim = _make_sim()
        client = AsyncMock()
        handler = AsyncMock(wraps=sim._handle_water_valve)
        sim._commands["water_valve"] = handler

        task, topic, queue = await self._start_command_loop(sim, client)
        queue.put_nowait(
            (
                topic,
                {
                    "request_id": "req-wv-wire01",
                    "command": "water_valve",
                    "params": {"seconds": 0},
                },
            )
        )
        await asyncio.sleep(0.02)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

        handler.assert_not_awaited()  # the loop rejected it; the handler never ran
        ack_calls = [
            c for c in client.publish.call_args_list if c[0][0].endswith("/ack")
        ]
        assert len(ack_calls) == 1
        payload = json.loads(ack_calls[0][0][1])
        assert payload["status"] == "rejected"
        assert payload["request_id"] == "req-wv-wire01"

    @pytest.mark.asyncio
    async def test_seconds_eleven_over_the_wire_acks_rejected_by_the_loop(self):
        sim = _make_sim()
        client = AsyncMock()
        handler = AsyncMock(wraps=sim._handle_water_valve)
        sim._commands["water_valve"] = handler

        task, topic, queue = await self._start_command_loop(sim, client)
        queue.put_nowait(
            (
                topic,
                {
                    "request_id": "req-wv-wire02",
                    "command": "water_valve",
                    "params": {"seconds": 11},
                },
            )
        )
        await asyncio.sleep(0.02)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

        handler.assert_not_awaited()
        ack_calls = [
            c for c in client.publish.call_args_list if c[0][0].endswith("/ack")
        ]
        assert len(ack_calls) == 1
        payload = json.loads(ack_calls[0][0][1])
        assert payload["status"] == "rejected"
        assert payload["request_id"] == "req-wv-wire02"

    @pytest.mark.asyncio
    async def test_rejected_seconds_never_opens_the_valve(self):
        """Guard against a fixture that makes the rejection branch vacuous:
        prove the hardware was never touched, not just that the ack says
        'rejected' — over the real wire path, since the handler itself no
        longer has a rejection branch to exercise directly."""
        sim = _make_sim()
        client = AsyncMock()
        set_hw = AsyncMock(wraps=sim._set_hw)
        sim._set_hw = set_hw

        task, topic, queue = await self._start_command_loop(sim, client)
        queue.put_nowait(
            (
                topic,
                {
                    "request_id": "req-wv-wire03",
                    "command": "water_valve",
                    "params": {"seconds": 0},
                },
            )
        )
        await asyncio.sleep(0.02)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

        set_hw.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_cancelled_mid_sleep_still_closes_valve(self):
        """Copilot review (PR 22, id=4128088539): if this task is cancelled
        during the sleep (e.g. an MQTT disconnect), the valve/flow states
        must not remain enabled indefinitely. Proved by actually cancelling
        a running handler task and reading hardware state back, not by
        reasoning about the code.

        Completion-table amendment (2026-09-29): `_handle_water_valve`
        itself now acks "accepted" and returns almost immediately — the
        actual open/sleep/close sequence is a background task
        (`_spawn_background`). This cancels THAT task (the one whose
        `finally` actually does the closing), not the now-fast call to
        `_handle_water_valve`.
        """
        sim = _make_sim()
        client = AsyncMock()
        sim.publish = AsyncMock()
        cmd = _make_command("water_valve", {"seconds": 10})

        await sim._handle_water_valve(client, cmd)
        assert len(sim._background_tasks) == 1
        task = next(iter(sim._background_tasks))

        await asyncio.sleep(0.02)  # let it open the valve and reach the sleep
        assert sim._hw["water_valve_solenoid"] is True
        assert sim._hw["water_flow_sensor"] is True

        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

        assert sim._hw["water_valve_solenoid"] is False
        assert sim._hw["water_flow_sensor"] is False

    @pytest.mark.asyncio
    async def test_hardware_update_raising_still_closes_valve(self):
        """Copilot review (PR 22, id=4128088539): 'either awaited hardware
        update raises' -- here the flow-sensor-on update raises after the
        valve solenoid was already opened; the valve must still end up
        closed rather than stuck open.

        Completion-table amendment (2026-09-29): the raise now happens
        inside the background task `_handle_water_valve` spawns (see
        `test_cancelled_mid_sleep_still_closes_valve` above) rather than
        propagating out of `_handle_water_valve` itself — proved here by
        awaiting that background task directly and observing the exception
        there, plus confirming `_on_background_task_done` logs it instead
        of losing it silently.
        """
        sim = _make_sim()
        client = AsyncMock()
        sim.publish = AsyncMock()
        cmd = _make_command("water_valve", {"seconds": 1})

        real_set_hw = sim._set_hw
        call_count = 0

        async def _flaky_set_hw(client, device, state):
            nonlocal call_count
            call_count += 1
            if call_count == 2:  # the water_flow_sensor True update
                raise RuntimeError("simulated MQTT publish failure")
            await real_set_hw(client, device, state)

        sim._set_hw = _flaky_set_hw

        await sim._handle_water_valve(client, cmd)
        assert len(sim._background_tasks) == 1
        task = next(iter(sim._background_tasks))

        with pytest.raises(RuntimeError, match="simulated MQTT publish failure"):
            await task

        assert sim._hw["water_valve_solenoid"] is False
        assert sim._hw["water_flow_sensor"] is False

    def test_real_wire_construction_rejects_before_reaching_the_handler(self):
        """Documents the adjacent base.py behaviour (see class docstring):
        going through the real `SubsystemCommand.model_validate` path (what
        `_command_loop` actually calls) raises immediately for an
        out-of-range `seconds` — it never becomes a command object at all,
        let alone reaches a handler."""
        with pytest.raises(ValidationError):
            SubsystemCommand.model_validate(
                {
                    "request_id": "req-00000002",
                    "command": "water_valve",
                    "params": {"seconds": 0},
                }
            )


class TestUnknownCommand:
    @pytest.mark.asyncio
    async def test_unsupported_ack(self):
        sim = _make_sim()
        client = AsyncMock()
        sim.publish = AsyncMock()
        cmd = _make_command("not_a_real_command", {})
        await sim._handle_command(client, cmd)

        ack = sim.publish.await_args_list[-1].args[2]
        assert ack.status == "unsupported"


class TestCommandChannelCapabilities:
    def test_dispense_and_water_valve_are_advertised(self):
        caps = _make_sim().build_capabilities()
        assert "dispense" in caps.commands
        assert "water_valve" in caps.commands
