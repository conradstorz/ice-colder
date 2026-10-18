# tests/test_simulator_vending.py
"""Tests for simulators/vending_machine.py — vending interface simulation."""

import asyncio
import json
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest
from pydantic import ValidationError

from ice_common.config_model import ConfigModel
from ice_contracts.board.common import SubsystemCommand
from ice_contracts.board.vending_machine import DispenserOutcome
from ice_contracts.board.dispenser_schema import (
    Accessory,
    AgitateStep,
    BaggedIceProfile,
    IceFillTimed,
    ReleaseTimed,
    WaterFillByVolume,
    WaterFillProfile,
    WaterFillTimed,
)
from ice_contracts.board.mqtt_messages import ButtonPress, DispenseCommand
from ice_sims.vending.channels import BUTTON_CHANNELS, CHANNELS, HARDWARE_DEVICES
from ice_sims.vending_machine import VendingMachineSimulator
from tests.async_helpers import patch_module_sleep, settle
from tests.sims.vending_profiles import IM600XL_PROFILES


_REAL_SLEEP = asyncio.sleep  # captured before any test patches asyncio.sleep


class FakeClock:
    """A fake `sim.sleep` that advances a cumulative `now` by exactly the
    requested duration on every call, synchronously, instead of actually
    waiting -- lets a test assert *when* within a run something happened
    (e.g. "the terminal report went out at t=1.0, the lagged accessory
    turned off at t=3.0") without real wall-clock delay. Assign an
    instance directly to `sim.sleep` the same way other tests here
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
    patch `sim.sleep` instead (an instance attribute), which leaves the
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


def _fault(sim, name: str):
    """The registered `FaultDef` named *name* (its `on_activate`/`on_recover`
    take a client, like the pre-split `sim._on_<name>_*` methods did)."""
    return next(f for f in sim.fault_defs if f.name == name)


def _make_sim(**kwargs) -> VendingMachineSimulator:
    return VendingMachineSimulator(config=_make_config(), **kwargs)


# The IM600XL profiles (simulator-local contract models, no edge code):
# slot 0 bagged ice released by the trap-door catch, slot 1 water 1 gal.
ICE_PROFILE = IM600XL_PROFILES[0]
WATER_PROFILE = IM600XL_PROFILES[1]

_ICE_FAULTS = {
    "motor_stall",
    "auger_jam",
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
    matches exactly what `command_loop` would have built.
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

    def test_single_product_config(self):
        config = ConfigModel.model_validate(
            {"physical": {"products": [{"sku": "T-1", "name": "Test", "price": 1.0}]}}
        )
        VendingMachineSimulator(config=config)

    def test_hardware_state_initialized_to_the_idle_defaults(self):
        sim = _make_sim()
        assert sim._hw == HARDWARE_DEVICES
        assert sim._hw["water_pressure_ok"] is True
        assert [d for d, state in sim._hw.items() if state] == ["water_pressure_ok"]

    def test_hardware_state_is_a_copy_of_the_table(self):
        sim = _make_sim()
        sim._hw["auger_motor"] = True
        assert HARDWARE_DEVICES["auger_motor"] is False


class TestButtonSelection:
    def test_random_button_in_range(self):
        sim = _make_sim()
        buttons = {sim._pick_button() for _ in range(100)}
        assert buttons.issubset(set(BUTTON_CHANNELS))
        assert len(buttons) > 1  # should hit at least 2 of 3


class TestHADiscovery:
    def test_entity_ids_are_the_binary_channels_plus_ambient_and_uptime(self):
        entities = _make_sim().ha_discovery_entities()
        binary = {c.channel_id for c in CHANNELS if c.kind == "binary"}
        # Customer buttons arrive as ButtonPress events and never publish a
        # hardware/io state, so they get no entity.
        assert {e["object_id"] for e in entities} == (
            (binary - set(BUTTON_CHANNELS)) | {"ambient", "uptime"}
        )

    def test_binary_entities_follow_hardware_io(self):
        entities = _make_sim().ha_discovery_entities()
        binary = [e for e in entities if e["component"] == "binary_sensor"]
        assert len(binary) == 13
        for entity in binary:
            assert entity["state_topic_suffix"] == f"hardware/io/{entity['object_id']}"

    def test_ambient_temperature_sensor(self):
        entities = _make_sim().ha_discovery_entities()
        temp = next(e for e in entities if e["object_id"] == "ambient")
        assert temp["component"] == "sensor"
        assert temp["device_class"] == "temperature"
        assert temp["unit_of_measurement"] == "°C"
        assert temp["state_topic_suffix"] == "sensors/temp/ambient"

    def test_no_removed_entities(self):
        ids = {e["object_id"] for e in _make_sim().ha_discovery_entities()}
        assert not ids & {"cabinet_temp", "water_flow_total", "fan", "heater_relay"}

    def test_uptime_sensor(self):
        entities = _make_sim().ha_discovery_entities()
        uptime = next(e for e in entities if e["object_id"] == "uptime")
        assert uptime["component"] == "sensor"
        assert uptime["name"] == "Vending Machine Uptime"
        assert uptime["device_class"] == "duration"
        assert uptime["state_topic_suffix"] == "heartbeat/vending"

    def test_all_object_ids_unique(self):
        entities = _make_sim().ha_discovery_entities()
        ids = [e["object_id"] for e in entities]
        assert len(ids) == len(set(ids))


class TestVendingFaultRegistration:
    def test_eight_faults_registered(self):
        sim = VendingMachineSimulator()
        assert len(sim.fault_defs) == 8

    def test_fault_names(self):
        sim = VendingMachineSimulator()
        names = {f.name for f in sim.fault_defs}
        assert names == {
            "auger_jam",
            "water_valve_stuck_open",
            "ice_bin_empty",
            "motor_stall",
            "no_water_flow",
            "flow_runaway",
            "slow_flow",
            # low_flow (finding C7): a flow rate below
            # min_flow_ml_per_second, distinct from slow_flow (above the
            # minimum) and no_water_flow (zero). probability=0 -- it is
            # triggered explicitly (test, or an operator), never rolled
            # at random.
            "low_flow",
        }


class TestAugerJamFault:
    @pytest.mark.asyncio
    async def test_fault_is_registered(self):
        sim = VendingMachineSimulator()
        names = {f.name for f in sim.fault_defs}
        assert "auger_jam" in names

    @pytest.mark.asyncio
    async def test_recover_logs_and_does_not_crash(self):
        sim = VendingMachineSimulator()
        client = AsyncMock()
        # Should complete without error
        await _fault(sim, "auger_jam").on_recover(client)


class TestWaterValveStuckOpenFault:
    @pytest.mark.asyncio
    async def test_activate_sets_the_valve_on(self):
        sim = VendingMachineSimulator()
        set_hw_calls = []

        async def capture_set_hw(client, device, state):
            set_hw_calls.append((device, state))

        sim.set_hw = capture_set_hw
        client = AsyncMock()
        await _fault(sim, "water_valve_stuck_open").on_activate(client)
        assert set_hw_calls == [("water_valve_solenoid", True)]

    @pytest.mark.asyncio
    async def test_recover_closes_the_valve(self):
        sim = VendingMachineSimulator()
        set_hw_calls = []

        async def capture_set_hw(client, device, state):
            set_hw_calls.append((device, state))

        sim.set_hw = capture_set_hw
        client = AsyncMock()
        await _fault(sim, "water_valve_stuck_open").on_recover(client)
        assert set_hw_calls == [("water_valve_solenoid", False)]


class TestIceBinEmptyFault:
    @pytest.mark.asyncio
    async def test_touches_no_bin_channel(self):
        sim = VendingMachineSimulator()
        set_hw_calls = []

        async def capture_set_hw(client, device, state):
            set_hw_calls.append((device, state))

        sim.set_hw = capture_set_hw
        client = AsyncMock()
        await _fault(sim, "ice_bin_empty").on_activate(client)
        await _fault(sim, "ice_bin_empty").on_recover(client)
        assert set_hw_calls == []


class TestSensorLoop:
    @pytest.mark.asyncio
    async def test_publishes_the_standing_inputs_and_no_temperature_or_flow(self):
        sim = _make_sim()
        topics: list[str] = []

        async def capture_publish(client, topic, payload):
            topics.append(topic)

        sim.publish = capture_publish

        async def stop(seconds):
            raise asyncio.CancelledError

        with patch_module_sleep("ice_sims.vending.simulator", stop):
            with pytest.raises(asyncio.CancelledError):
                # private: the periodic sensor loop has no public entry point
                await sim._publish_sensors(AsyncMock())

        assert topics == [
            "hardware/io/water_pressure_ok",
            "hardware/io/sump_high",
            "hardware/io/service_door",
        ]


class TestCustomerPresses:
    @pytest.mark.asyncio
    @pytest.mark.parametrize("config", [None, ConfigModel()], ids=["sample", "empty"])
    async def test_press_is_a_declared_button_whatever_the_catalog_holds(self, config):
        sim = VendingMachineSimulator(config=config) if config else _make_sim()
        client = AsyncMock()
        sim.publish = AsyncMock()
        sleep_calls = []

        async def fake_sleep(seconds):
            sleep_calls.append(seconds)
            if len(sleep_calls) >= 2:
                raise asyncio.CancelledError

        with patch_module_sleep("ice_sims.vending.customer", fake_sleep):
            with patch("ice_sims.vending.customer.random.random", return_value=0.9):
                with pytest.raises(asyncio.CancelledError):
                    await sim._customer_loop(client)

        sim.publish.assert_called_once()
        press = sim.publish.call_args.args[-1]
        assert isinstance(press, ButtonPress)
        assert press.button in BUTTON_CHANNELS

    @pytest.mark.asyncio
    async def test_indecisive_second_press_names_a_different_button(self):
        sim = _make_sim()
        client = AsyncMock()
        sim.publish = AsyncMock()
        sleep_calls = []

        async def fake_sleep(seconds):
            sleep_calls.append(seconds)
            if len(sleep_calls) >= 3:
                raise asyncio.CancelledError

        with patch_module_sleep("ice_sims.vending.customer", fake_sleep):
            with patch("ice_sims.vending.customer.random.random", return_value=0.0):
                with pytest.raises(asyncio.CancelledError):
                    await sim._customer_loop(client)

        first, second = (c.args[-1].button for c in sim.publish.call_args_list)
        assert first in BUTTON_CHANNELS
        assert second in BUTTON_CHANNELS
        assert second != first


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

    async def test_fault_aware_idle_time_shortened(self):
        sim = VendingMachineSimulator()
        client = AsyncMock()
        fault = next(f for f in sim.fault_defs if f.name == "auger_jam")
        await sim.activate_fault(client, fault, recover_in=9e9)
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
        assert caps.contract_version == "2.0.0"

    def test_channels_match_spec_table_in_order(self):
        """Spec §5.1, copied exactly -- the dashboard renders channels in
        whatever order build_capabilities lists them."""
        caps = _make_sim().build_capabilities()
        assert [c.channel_id for c in caps.channels] == [
            "bag_full_sensor",
            "button_ice",
            "button_water_1gal",
            "button_water_5gal",
            "ro_tank_full",
            "ro_tank_empty",
            "water_pressure_ok",
            "sump_high",
            "service_door",
            "agitator_motor",
            "auger_motor",
            "bag_fan",
            "trap_door_catch",
            "water_valve_solenoid",
            "ro_production",
            "heater_power",
            "fill_pulses",
            "ambient",
        ]

    def test_channel_directions_match_hardware_role(self):
        """Every HARDWARE_DEVICES key must be a declared binary channel
        (guards a device added to the sim but never declared), and the
        output set is exactly the seven actuators -- never the sensors."""
        caps = _make_sim().build_capabilities()
        by_id = {c.channel_id: c for c in caps.channels}
        assert set(HARDWARE_DEVICES) <= set(by_id)
        for device in HARDWARE_DEVICES:
            assert by_id[device].kind == "binary"

        outputs = {c.channel_id for c in caps.channels if c.direction == "output"}
        assert outputs == {
            "agitator_motor",
            "auger_motor",
            "bag_fan",
            "trap_door_catch",
            "water_valve_solenoid",
            "ro_production",
            "heater_power",
        }

    def test_driven_by_matches_spec_table(self):
        caps = _make_sim().build_capabilities()
        driven_by = {c.channel_id: c.driven_by for c in caps.channels}
        assert {k: v for k, v in driven_by.items() if v} == {
            "agitator_motor": "dispense",
            "auger_motor": "dispense",
            "bag_fan": "dispense",
            "trap_door_catch": "dispense",
            "water_valve_solenoid": "water_valve",
        }
        assert set(driven_by) == {c.channel_id for c in CHANNELS}

    def test_channel_intervals(self):
        """The temperature channel publishes on SENSOR_PUBLISH_INTERVAL;
        every binary channel and the pulse counter is declared at 1.0 s."""
        from ice_sims.vending_machine import SENSOR_PUBLISH_INTERVAL

        caps = _make_sim().build_capabilities()
        by_id = {c.channel_id: c for c in caps.channels}
        assert by_id["ambient"].interval_seconds == SENSOR_PUBLISH_INTERVAL
        for channel in caps.channels:
            if channel.channel_id != "ambient":
                assert channel.interval_seconds == 1.0

    def test_fill_pulses_is_the_only_volume_metering(self):
        """The IM600XL meters volume with the `fill_pulses` counter; the
        cumulative-gallons `water_flow` channel is gone (spec §3.4)."""
        caps = _make_sim().build_capabilities()
        by_id = {c.channel_id: c for c in caps.channels}
        assert "water_flow" not in by_id
        assert by_id["fill_pulses"].unit == "pulses"
        assert by_id["fill_pulses"].kind == "counter"
        assert by_id["ambient"].kind == "temperature"


class TestExecuteProfile:
    """`execute_profile` -- the profile-driven replacement for
    `_dispense_slot`/`_run_ice_dispense`/`_run_water_dispense`."""

    @pytest.mark.asyncio
    async def test_bagged_ice_profile_step_sequence_and_io(self):
        sim = _make_sim()
        sim.sleep = AsyncMock()
        client = AsyncMock()
        states = []
        hw_events = []

        async def capture_publish(c, topic, payload):
            if topic == "hardware/dispenser" and hasattr(payload, "state"):
                states.append(payload.state)
            elif topic.startswith("hardware/io/"):
                hw_events.append((payload.device, payload.state))

        sim.publish = capture_publish
        cmd = DispenseCommand(slot=0, mechanism="bagged_ice", profile=ICE_PROFILE)

        await sim.execute_profile(client, cmd, request_id="req-ice-1")

        assert states == ["agitate", "fill", "release", "complete"]

        # The catch is energized before any motion and held through the fill.
        assert hw_events[0] == ("trap_door_catch", True)
        assert hw_events.index(("trap_door_catch", True)) < hw_events.index(
            ("agitator_motor", True)
        )
        # ... and released (de-energized) only after the bag is full.
        assert hw_events.index(("bag_full_sensor", True)) < hw_events.index(
            ("trap_door_catch", False)
        )

        # bag_fan's on_during is ["agitate", "fill"] -- on before the
        # agitator, off again by the end.
        assert hw_events.index(("bag_fan", True)) < hw_events.index(
            ("agitator_motor", True)
        )
        assert ("bag_fan", False) in hw_events

        # Every output this run drove ends up off.
        for channel in ("agitator_motor", "auger_motor", "trap_door_catch"):
            assert [s for d, s in hw_events if d == channel][-1] is False
        assert sim.dispenser.pending_lag_off == {}

    @pytest.mark.asyncio
    async def test_timed_fill_runs_exactly_max_run_seconds(self):
        sim = _make_sim()
        durations = []

        async def fake_sleep(seconds):
            durations.append(seconds)

        sim.sleep = fake_sleep
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
                proof="timed", solenoid_channel="trap_door_catch", pulse_seconds=1.0
            ),
        )
        cmd = DispenseCommand(slot=3, mechanism="bagged_ice", profile=profile)

        await sim.execute_profile(client, cmd)

        assert 17.0 in durations

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "fault,expected",
        [
            ("motor_stall", DispenserOutcome.error),
            ("auger_jam", DispenserOutcome.timeout),
            ("no_water_flow", DispenserOutcome.no_flow),
            ("flow_runaway", DispenserOutcome.over_dispense),
            ("slow_flow", DispenserOutcome.timeout),
            ("low_flow", DispenserOutcome.no_flow),
            ("ice_bin_empty", DispenserOutcome.bin_empty),
        ],
    )
    async def test_injected_fault_yields_outcome(self, fault, expected):
        sim = _make_sim()
        sim.sleep = AsyncMock()
        client = AsyncMock()
        terminal = []

        async def capture_publish(c, topic, payload):
            if topic == "hardware/dispenser" and hasattr(payload, "state"):
                terminal.append(payload.state)

        sim.publish = capture_publish
        fault_def = next(f for f in sim.fault_defs if f.name == fault)
        await sim.activate_fault(client, fault_def)

        if fault in _ICE_FAULTS:
            cmd = DispenseCommand(slot=1, mechanism="bagged_ice", profile=ICE_PROFILE)
        else:
            cmd = DispenseCommand(slot=2, mechanism="water_fill", profile=WATER_PROFILE)

        await sim.execute_profile(client, cmd)

        assert DispenserOutcome(terminal[-1]) is expected

        if fault == "ice_bin_empty":
            assert terminal == ["agitate", "fill", "bin_empty"]

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

        sim.sleep = fake_sleep
        client = AsyncMock()
        terminal = []
        flow_readings = []

        async def capture_publish(c, topic, payload):
            if topic == "hardware/dispenser" and hasattr(payload, "state"):
                terminal.append(payload.state)
            elif topic == "telemetry/vending/fill_pulses":
                flow_readings.append(payload.value)

        sim.publish = capture_publish
        cmd = DispenseCommand(slot=2, mechanism="water_fill", profile=WATER_PROFILE)

        await sim.execute_profile(client, cmd)

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
        sim.sleep = AsyncMock()
        client = AsyncMock()
        terminal = []

        async def capture_publish(c, topic, payload):
            if topic == "hardware/dispenser" and hasattr(payload, "state"):
                terminal.append((payload.state, payload.detail))

        sim.publish = capture_publish
        fault = next(f for f in sim.fault_defs if f.name == "flow_runaway")
        await sim.activate_fault(client, fault)
        cmd = DispenseCommand(slot=2, mechanism="water_fill", profile=WATER_PROFILE)

        await sim.execute_profile(client, cmd)

        state, detail = terminal[-1]
        assert state == "over_dispense"
        dispensed_ml = float(detail.split()[0])

        fill = WATER_PROFILE.fill
        tolerance_ml = fill.target_volume_ml * (1 + fill.over_dispense_percent / 100.0)
        assert dispensed_ml > tolerance_ml

    @pytest.mark.asyncio
    async def test_slow_flow_times_out_when_target_unreachable(self):
        """Copilot review (PR #32) finding C7: slow_flow's rate (1.05 x
        min_flow_ml_per_second, just above the no-flow floor) is too
        modest to reach WATER_PROFILE's target_volume_ml within
        max_fill_seconds -- timeout, not no_flow (the rate is never
        below the minimum) and not complete (the target is never
        reached)."""
        sim = _make_sim()
        sim.sleep = AsyncMock()
        client = AsyncMock()
        terminal = []

        async def capture_publish(c, topic, payload):
            if topic == "hardware/dispenser" and hasattr(payload, "state"):
                terminal.append((payload.state, payload.detail))

        sim.publish = capture_publish
        fault = next(f for f in sim.fault_defs if f.name == "slow_flow")
        await sim.activate_fault(client, fault)
        cmd = DispenseCommand(slot=2, mechanism="water_fill", profile=WATER_PROFILE)

        await sim.execute_profile(client, cmd)

        assert terminal[-1][0] == "timeout"

    @pytest.mark.asyncio
    async def test_slow_flow_completes_when_target_reachable(self):
        """Finding C7: the same modest slow_flow rate completes normally
        when the slot's own target_volume_ml/max_fill_seconds budget is
        generous enough for it -- slow_flow is not unconditionally a
        timeout, only when the target happens to be unreachable at that
        rate."""
        sim = _make_sim()
        sim.sleep = AsyncMock()
        client = AsyncMock()
        terminal = []

        async def capture_publish(c, topic, payload):
            if topic == "hardware/dispenser" and hasattr(payload, "state"):
                terminal.append((payload.state, payload.detail))

        sim.publish = capture_publish
        fault = next(f for f in sim.fault_defs if f.name == "slow_flow")
        await sim.activate_fault(client, fault)

        # 1.05 x 20.0 ml/s = 21.0 ml/s; 210 ml at 21 ml/s is 10 s, well
        # inside this fill's own 30 s max_fill_seconds.
        fill = WaterFillByVolume(
            proof="flow_volume",
            valve_channel="water_valve_solenoid",
            flow_sensor_channel="fill_pulses",
            target_volume_ml=210,
            pulses_per_liter=450.0,
            min_flow_ml_per_second=20.0,
            no_flow_grace_seconds=3.0,
            over_dispense_percent=10.0,
            max_fill_seconds=30.0,
        )
        profile = WaterFillProfile(
            mechanism="water_fill", product_sku="WATER-SLOW-OK", fill=fill
        )
        cmd = DispenseCommand(slot=2, mechanism="water_fill", profile=profile)

        await sim.execute_profile(client, cmd)

        assert terminal[-1][0] == "complete"

    @pytest.mark.asyncio
    async def test_rate_below_minimum_yields_no_flow_after_grace(self):
        """Finding C7: min_flow_ml_per_second is now actually consulted
        -- a flow rate below the floor (the low_flow fault, rate = 0.5 x
        min_flow_ml_per_second) faults no_flow after
        no_flow_grace_seconds, with a detail naming the rate, the
        minimum, and the grace period -- promptly, not after the full
        max_fill_seconds."""
        sim = _make_sim()
        durations = []

        async def fake_sleep(seconds):
            durations.append(seconds)

        sim.sleep = fake_sleep
        client = AsyncMock()
        terminal = []

        async def capture_publish(c, topic, payload):
            if topic == "hardware/dispenser" and hasattr(payload, "state"):
                terminal.append((payload.state, payload.detail))

        sim.publish = capture_publish
        fault = next(f for f in sim.fault_defs if f.name == "low_flow")
        await sim.activate_fault(client, fault)
        cmd = DispenseCommand(slot=2, mechanism="water_fill", profile=WATER_PROFILE)

        await sim.execute_profile(client, cmd)

        state, detail = terminal[-1]
        assert state == "no_flow"
        fill = WATER_PROFILE.fill
        assert f"{0.5 * fill.min_flow_ml_per_second:.1f}" in detail
        assert f"{fill.min_flow_ml_per_second:.1f}" in detail
        assert f"{fill.no_flow_grace_seconds:.1f}" in detail
        # Faulted promptly, within the grace window -- not the full
        # max_fill_seconds.
        assert sum(durations) == pytest.approx(fill.no_flow_grace_seconds)

    @pytest.mark.asyncio
    async def test_accessory_lag_runs_after_last_step_on_success(self):
        """I1: `lag_seconds` must not be defeated by an unconditional
        cancellation of every pending accessory-off task -- an accessory
        spanning the whole run (`["all"]`) turns off `lag_seconds` after
        the run completes, while the terminal `complete` report goes out
        the moment the fill step itself finishes, not after the lag."""
        sim = _make_sim()
        clock = FakeClock()
        sim.sleep = clock
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
                    channel="heater_power",
                    on_during=["all"],
                    lead_seconds=0.0,
                    lag_seconds=2.0,
                )
            },
        )
        cmd = DispenseCommand(slot=9, mechanism="water_fill", profile=profile)

        await sim.execute_profile(client, cmd)

        complete_time = next(
            t for kind, val, t in events if kind == "state" and val == "complete"
        )
        light_off_time = next(
            t
            for kind, val, t in events
            if kind == "io" and val == ("heater_power", False)
        )

        assert complete_time == pytest.approx(1.0)
        assert light_off_time == pytest.approx(3.0)

    @pytest.mark.asyncio
    async def test_concurrent_accessory_leads_overlap(self):
        """M1: two accessories leading the same step overlap (one wait for
        the longer lead) instead of stacking (a wait per accessory)."""
        sim = _make_sim()
        clock = FakeClock()
        sim.sleep = clock
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
                    channel="heater_power",
                    on_during=["fill"],
                    lead_seconds=2.0,
                    lag_seconds=0.0,
                ),
            },
        )
        cmd = DispenseCommand(slot=9, mechanism="water_fill", profile=profile)

        await sim.execute_profile(client, cmd)

        fill_time = next(t for state, t in events if state == "fill")
        assert fill_time == pytest.approx(3.0)

    @pytest.mark.asyncio
    async def test_new_run_cancels_previous_runs_lag_off(self):
        """Copilot review (PR #32) finding C4: a lag-off task is keyed by
        channel and owned by the simulator, not the run that created it
        -- a back-to-back second sale on the same slot/accessory must
        cancel the first sale's still-pending lag-off rather than let it
        fire later and switch the accessory off out from under the
        second run. Run 1's ["all"] accessory has lag_seconds=5 (gated,
        never released); run 2 starts immediately after run 1's
        `complete` is published; the accessory stays on throughout run 2
        and is only switched off when run 2's own lag completes."""
        sim = _make_sim()
        gate = asyncio.Event()

        async def fake_sleep(seconds: float) -> None:
            if seconds == 5.0:
                await gate.wait()

        sim.sleep = fake_sleep
        client = AsyncMock()
        hw_events: list[tuple[str, bool]] = []

        async def capture_publish(c, topic, payload):
            if topic.startswith("hardware/io/"):
                hw_events.append((payload.device, payload.state))

        sim.publish = capture_publish

        fill = WaterFillTimed(
            proof="timed", valve_channel="water_valve_solenoid", max_fill_seconds=1.0
        )
        profile = WaterFillProfile(
            mechanism="water_fill",
            product_sku="WATER-RACE",
            fill=fill,
            accessories={
                "light": Accessory(
                    channel="heater_power",
                    on_during=["all"],
                    lead_seconds=0.0,
                    lag_seconds=5.0,
                )
            },
        )
        cmd = DispenseCommand(slot=9, mechanism="water_fill", profile=profile)

        task1 = asyncio.ensure_future(sim.execute_profile(client, cmd))
        await _REAL_SLEEP(0)
        await _REAL_SLEEP(0)
        # Run 1 has published its terminal report and is now blocked (via
        # the gate) in its own lag-off wait; the accessory is still on.
        assert ("heater_power", True) in hw_events
        assert not task1.done()

        task2 = asyncio.ensure_future(sim.execute_profile(client, cmd))
        for _ in range(6):
            if task1.done():
                break
            await _REAL_SLEEP(0)
        # Run 2's _enter_step cancelled run 1's pending lag-off; run 1's
        # own end-of-run sweep must not have turned the accessory off.
        assert task1.done()
        assert ("heater_power", False) not in hw_events
        assert not task2.done()  # run 2 now blocked on its own lag-off

        gate.set()
        await asyncio.wait_for(task2, timeout=2.0)
        assert ("heater_power", False) in hw_events

    @pytest.mark.asyncio
    async def test_failed_run_turns_every_accessory_off(self):
        """motor_stall aborts mid-agitate, before fill is ever reached --
        bag_fan (on_during agitate and fill) already turned on, and must
        still end up off immediately (no lag wait, since this run never
        reached a `complete` outcome), as must the trap-door catch. The one
        documented exception to "every accessory/output off" is
        `water_valve_stuck_open` (the valve), which is not active here."""
        sim = _make_sim()
        sim.sleep = AsyncMock()
        client = AsyncMock()
        hw_events = []

        async def capture_publish(c, topic, payload):
            if topic.startswith("hardware/io/"):
                hw_events.append((payload.device, payload.state))

        sim.publish = capture_publish
        fault = next(f for f in sim.fault_defs if f.name == "motor_stall")
        await sim.activate_fault(client, fault)
        cmd = DispenseCommand(slot=1, mechanism="bagged_ice", profile=ICE_PROFILE)

        await sim.execute_profile(client, cmd)

        assert ("bag_fan", True) in hw_events
        assert ("bag_fan", False) in hw_events
        assert ("agitator_motor", False) in hw_events
        assert ("trap_door_catch", False) in hw_events
        assert ("auger_motor", True) not in hw_events


class TestDispenseCommand:
    """Command-channel `dispense`: runs the slot's profile via
    `execute_profile` through `handle_command`."""

    @pytest.mark.asyncio
    async def test_runs_profile_once_and_publishes_hardware_dispenser(self):
        """Completion-table amendment (2026-09-29): `handle_command`
        itself now returns as soon as the "accepted" ack is published —
        the real sequence runs in a background task
        (`_spawn_background`), so this drains it (`_drain_background`)
        before checking the motor ran and the terminal report went out.
        """
        sim = _make_sim()
        sim.sleep = AsyncMock()
        client = AsyncMock()
        dispenser_states = []

        async def capture_publish(c, topic, payload):
            if topic == "hardware/dispenser":
                dispenser_states.append(payload.state)

        sim.publish = capture_publish
        run_profile = AsyncMock(wraps=sim.execute_profile)
        sim.execute_profile = run_profile

        cmd = _make_command("dispense", _dispense_params(1, "bagged_ice", ICE_PROFILE))
        ack = await sim.handle_command(client, cmd)
        await _drain_background(sim)

        assert run_profile.await_count == 1
        assert dispenser_states == ["agitate", "fill", "release", "complete"]
        assert ack is None  # handle_command publishes the ack, doesn't return it

    @pytest.mark.asyncio
    async def test_publishes_ok_ack_with_slot_and_mechanism_result(self):
        """This ack is the "accepted" one (published synchronously, before
        `handle_command` returns) — the command's own completion is the
        terminal `hardware/dispenser` report, not a second ack.
        """
        sim = _make_sim()
        sim.sleep = AsyncMock()
        client = AsyncMock()
        published_acks = []

        async def capture_publish(c, topic, payload):
            if topic == "cmd/vending/ack":
                published_acks.append(payload)

        sim.publish = capture_publish
        cmd = _make_command("dispense", _dispense_params(1, "bagged_ice", ICE_PROFILE))
        await sim.handle_command(client, cmd)

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
        sim.sleep = AsyncMock()
        client = AsyncMock()
        sim.publish = AsyncMock()
        run_profile = AsyncMock(wraps=sim.execute_profile)
        sim.execute_profile = run_profile

        cmd = _make_command(
            "dispense",
            _dispense_params(1, "bagged_ice", ICE_PROFILE),
            request_id="dup-req-1",
        )
        await sim.handle_command(client, cmd)
        await sim.handle_command(client, cmd)  # same request_id, sent again
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
        sim.sleep = AsyncMock()
        client = AsyncMock()
        dispenser_states = []

        async def capture_publish(c, topic, payload):
            if topic == "hardware/dispenser":
                dispenser_states.append(payload.state)

        sim.publish = capture_publish
        cmd = _make_command(
            "dispense", _dispense_params(2, "water_fill", WATER_PROFILE)
        )
        await sim.handle_command(client, cmd)
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
        import ice_sims.vending_machine as module

        package_dir = Path(module.__file__).parent
        sources = [Path(module.__file__)] + sorted(
            (package_dir / "vending").glob("*.py")
        )
        for path in sources:
            assert "cmd/dispense" not in path.read_text(encoding="utf-8"), path


class TestWaterValveCommand:
    """`water_valve` acks ok in [1, 10]; outside it, `command_loop`
    (`simulators/base.py`) now acks "rejected" itself from the raw wire
    payload before a `SubsystemCommand` — and therefore `_handle_water_valve`
    — ever exists. `_handle_water_valve` has no defense-in-depth check of
    its own any more (see its docstring); the out-of-range cases below are
    proven over the real wire path (`command_loop`), not by constructing an
    out-of-range command directly, since that construction now fails before
    reaching this simulator at all.
    """

    @pytest.mark.asyncio
    async def test_seconds_one_acks_ok(self):
        """This is the "accepted" ack -- published synchronously before
        `handle_command` returns, before the background task that
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
        with patch_module_sleep("ice_sims.vending.simulator", AsyncMock()):
            await sim.handle_command(client, cmd)

            ack = sim.publish.await_args_list[-1].args[2]
            assert ack.status == "ok"
            assert ack.result == {"seconds": 1}
            assert ack.phase == "accepted"

            await _drain_background(sim)

        completed_ack = sim.publish.await_args_list[-1].args[2]
        assert completed_ack.phase == "completed"
        assert completed_ack.result == {"seconds": 1}

    @pytest.mark.asyncio
    async def test_drives_only_the_declared_valve_channel(self):
        sim = _make_sim()
        client = AsyncMock()
        io_events = []

        async def capture_publish(c, topic, payload):
            if topic.startswith("hardware/io/"):
                io_events.append((payload.device, payload.state))

        sim.publish = capture_publish
        cmd = _make_command("water_valve", {"seconds": 1})
        with patch_module_sleep("ice_sims.vending.simulator", AsyncMock()):
            await sim.handle_command(client, cmd)
            await _drain_background(sim)

        assert io_events == [
            ("water_valve_solenoid", True),
            ("water_valve_solenoid", False),
        ]

    @pytest.mark.asyncio
    async def test_seconds_ten_acks_ok(self):
        sim = _make_sim()
        client = AsyncMock()
        sim.publish = AsyncMock()
        cmd = _make_command("water_valve", {"seconds": 10})
        with patch_module_sleep("ice_sims.vending.simulator", AsyncMock()):
            await sim.handle_command(client, cmd)

            ack = sim.publish.await_args_list[-1].args[2]
            assert ack.status == "ok"

            await _drain_background(sim)  # see test_seconds_one_acks_ok

    @staticmethod
    async def _start_command_loop(sim, client):
        task = asyncio.create_task(sim.command_loop(client))
        await settle()
        topic, queue = next(
            (t, q)
            for t, q in sim._subscriptions
            if t == f"{sim.topic_prefix}/cmd/vending"
        )
        return task, topic, queue

    @pytest.mark.asyncio
    async def test_seconds_zero_over_the_wire_acks_rejected_by_the_loop(self):
        """Out-of-range `seconds` reaches this simulator only as a raw wire
        payload now — `command_loop` (`simulators/base.py`) acks
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
        await asyncio.wait_for(queue.join(), 1.0)
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
        await asyncio.wait_for(queue.join(), 1.0)
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
        set_hw = AsyncMock(wraps=sim.set_hw)
        sim.set_hw = set_hw

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
        await asyncio.wait_for(queue.join(), 1.0)
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

        # Deterministic only because sim.publish is an AsyncMock (no internal
        # suspension), so the background task runs both set_hw calls on its
        # first slice and parks at the real asyncio.sleep.
        await settle()  # let it open the valve and reach the real asyncio.sleep
        assert sim._hw["water_valve_solenoid"] is True

        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

        assert sim._hw["water_valve_solenoid"] is False

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

        real_set_hw = sim.set_hw
        call_count = 0

        async def _flaky_set_hw(client, device, state):
            nonlocal call_count
            call_count += 1
            await real_set_hw(client, device, state)
            if call_count == 1:  # the valve-open update fails after taking effect
                raise RuntimeError("simulated MQTT publish failure")

        sim.set_hw = _flaky_set_hw

        await sim._handle_water_valve(client, cmd)
        assert len(sim._background_tasks) == 1
        task = next(iter(sim._background_tasks))

        with pytest.raises(RuntimeError, match="simulated MQTT publish failure"):
            await task

        assert sim._hw["water_valve_solenoid"] is False

    def test_real_wire_construction_rejects_before_reaching_the_handler(self):
        """Documents the adjacent base.py behaviour (see class docstring):
        going through the real `SubsystemCommand.model_validate` path (what
        `command_loop` actually calls) raises immediately for an
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
        await sim.handle_command(client, cmd)

        ack = sim.publish.await_args_list[-1].args[2]
        assert ack.status == "unsupported"


class TestCommandChannelCapabilities:
    def test_dispense_and_water_valve_are_advertised(self):
        caps = _make_sim().build_capabilities()
        assert "dispense" in caps.commands
        assert "water_valve" in caps.commands
