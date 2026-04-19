# tests/test_simulator_ice_maker.py
"""Tests for simulators/ice_maker.py — ice maker temperature simulation."""
import pytest
from unittest.mock import AsyncMock

from simulators.ice_maker import IceMakerSimulator, ThermalSensor, SENSOR_DEFS
from simulators.base import FaultDef


class TestThermalSensor:
    def test_initial_value(self):
        sensor = ThermalSensor(name="test", target_on=50.0, target_off=30.0, rate=0.1, noise=0.0)
        # Initial value is target_off (compressor starts off)
        assert sensor.value == 30.0

    def test_moves_toward_target_on(self):
        sensor = ThermalSensor(name="test", target_on=50.0, target_off=30.0, rate=0.5, noise=0.0)
        initial = sensor.value
        sensor.update(compressor_on=True, dt=1.0)
        # Should move toward 50.0 from 30.0
        assert sensor.value > initial

    def test_moves_toward_target_off(self):
        sensor = ThermalSensor(name="test", target_on=50.0, target_off=30.0, rate=0.5, noise=0.0)
        sensor._value = 50.0  # start at on-target
        sensor.update(compressor_on=False, dt=1.0)
        # Should move toward 30.0 from 50.0
        assert sensor.value < 50.0

    def test_noise_adds_variation(self):
        sensor = ThermalSensor(name="test", target_on=50.0, target_off=30.0, rate=0.0, noise=1.0)
        values = set()
        for _ in range(20):
            sensor.update(compressor_on=False, dt=1.0)
            values.add(round(sensor.value, 2))
        # With noise=1.0 and rate=0.0, values should vary
        assert len(values) > 1

    def test_rate_zero_stays_put_without_noise(self):
        sensor = ThermalSensor(name="test", target_on=50.0, target_off=30.0, rate=0.0, noise=0.0)
        sensor.update(compressor_on=True, dt=1.0)
        assert sensor.value == 30.0  # no movement


class TestSensorDefs:
    def test_all_nine_sensors_defined(self):
        assert len(SENSOR_DEFS) == 9

    def test_expected_sensor_names(self):
        names = {s["name"] for s in SENSOR_DEFS}
        expected = {
            "water_inlet", "water_bath", "compressor", "exhaust_air",
            "ambient_air", "refrigerant_high", "refrigerant_low",
            "purge_water", "hot_gas_valve",
        }
        assert names == expected


class TestIceMakerSimulator:
    def test_creates_with_defaults(self):
        sim = IceMakerSimulator()
        assert sim.subsystem_name == "ice_maker"
        assert len(sim.sensors) == 9

    def test_compressor_starts_off(self):
        sim = IceMakerSimulator()
        assert sim.compressor_on is False

    def test_tick_updates_all_sensors(self):
        sim = IceMakerSimulator()
        initial_values = {s.name: s.value for s in sim.sensors}
        sim.tick(dt=5.0)
        # At least some sensors should have changed (noise)
        changed = sum(
            1 for s in sim.sensors
            if round(s.value, 4) != round(initial_values[s.name], 4)
        )
        assert changed > 0

    def test_compressor_cycles(self):
        sim = IceMakerSimulator()
        assert sim.compressor_on is False
        # Advance past the off-cycle (300s default)
        for _ in range(61):
            sim.tick(dt=5.0)  # 305 seconds
        assert sim.compressor_on is True


class TestHADiscovery:
    def test_returns_11_entities(self):
        sim = IceMakerSimulator()
        entities = sim.ha_discovery_entities()
        assert len(entities) == 11

    def test_nine_temperature_sensors(self):
        sim = IceMakerSimulator()
        entities = sim.ha_discovery_entities()
        temp_sensors = [e for e in entities if e.get("device_class") == "temperature"]
        assert len(temp_sensors) == 9

    def test_temperature_sensor_fields(self):
        sim = IceMakerSimulator()
        entities = sim.ha_discovery_entities()
        temp = next(e for e in entities if e["object_id"] == "water_inlet_temp")
        assert temp["component"] == "sensor"
        assert temp["name"] == "Ice Maker Water Inlet Temperature"
        assert temp["state_topic_suffix"] == "sensors/temp/water_inlet"
        assert temp["value_template"] == "{{ value_json.value }}"
        assert temp["unit_of_measurement"] == "\u00b0C"
        assert temp["state_class"] == "measurement"
        assert temp["expire_after"] == 30

    def test_compressor_binary_sensor(self):
        sim = IceMakerSimulator()
        entities = sim.ha_discovery_entities()
        comp = next(e for e in entities if e["object_id"] == "compressor")
        assert comp["component"] == "binary_sensor"
        assert comp["name"] == "Ice Maker Compressor"
        assert comp["device_class"] == "running"
        assert comp["state_topic_suffix"] == "ice_maker/event"
        assert comp["payload_on"] == "ON"
        assert comp["payload_off"] == "OFF"

    def test_uptime_sensor(self):
        sim = IceMakerSimulator()
        entities = sim.ha_discovery_entities()
        uptime = next(e for e in entities if e["object_id"] == "uptime")
        assert uptime["component"] == "sensor"
        assert uptime["name"] == "Ice Maker Uptime"
        assert uptime["device_class"] == "duration"
        assert uptime["unit_of_measurement"] == "s"
        assert uptime["state_class"] == "total_increasing"
        assert uptime["state_topic_suffix"] == "heartbeat/ice_maker"

    def test_all_state_topic_suffixes_are_valid(self):
        """Verify every entity points to a topic the simulator actually publishes to."""
        sim = IceMakerSimulator()
        entities = sim.ha_discovery_entities()
        valid_prefixes = {"sensors/temp/", "ice_maker/event", "heartbeat/ice_maker"}
        for entity in entities:
            suffix = entity["state_topic_suffix"]
            assert any(suffix.startswith(p) or suffix == p for p in valid_prefixes), \
                f"Unexpected state_topic_suffix: {suffix}"

    def test_all_object_ids_unique(self):
        sim = IceMakerSimulator()
        entities = sim.ha_discovery_entities()
        ids = [e["object_id"] for e in entities]
        assert len(ids) == len(set(ids))

    @pytest.mark.asyncio
    async def test_discovery_publishes_all_entities(self):
        """Smoke test: the base class publishes all 11 ice maker entities."""
        sim = IceMakerSimulator(machine_id="vmc-test")
        client = AsyncMock()
        await sim._publish_ha_discovery(client)
        assert client.publish.call_count == 11
        topics = [call.args[0] for call in client.publish.call_args_list]
        # All should be under homeassistant/
        assert all(t.startswith("homeassistant/") for t in topics)
        # All should contain the machine_id
        assert all("vmc-test_ice_maker" in t for t in topics)
        # All should be retained
        assert all(call.kwargs.get("retain") is True for call in client.publish.call_args_list)


class TestIceMakerFaultRegistration:
    def test_four_faults_registered(self):
        sim = IceMakerSimulator()
        assert len(sim._fault_defs) == 4

    def test_fault_names(self):
        sim = IceMakerSimulator()
        names = {f.name for f in sim._fault_defs}
        assert names == {
            "compressor_overtemp",
            "low_refrigerant",
            "water_inlet_blocked",
            "defrost_stuck",
        }

    def test_sensor_by_name(self):
        sim = IceMakerSimulator()
        sensor = sim._sensor_by_name("compressor")
        assert sensor.name == "compressor"

    def test_sensor_by_name_raises_for_unknown(self):
        sim = IceMakerSimulator()
        with pytest.raises(StopIteration):
            sim._sensor_by_name("nonexistent")


class TestCompressorOvertempFault:
    @pytest.mark.asyncio
    async def test_activate_forces_compressor_off(self):
        sim = IceMakerSimulator()
        sim.compressor_on = True
        client = AsyncMock()
        await sim._on_compressor_overtemp_activate(client)
        assert sim.compressor_on is False

    @pytest.mark.asyncio
    async def test_activate_overrides_refrigerant_high_targets(self):
        sim = IceMakerSimulator()
        client = AsyncMock()
        await sim._on_compressor_overtemp_activate(client)
        sensor = sim._sensor_by_name("refrigerant_high")
        assert sensor.target_on == 95.0
        assert sensor.target_off == 95.0

    @pytest.mark.asyncio
    async def test_recover_restores_refrigerant_high_targets(self):
        sim = IceMakerSimulator()
        client = AsyncMock()
        await sim._on_compressor_overtemp_activate(client)
        await sim._on_compressor_overtemp_recover(client)
        original = next(d for d in SENSOR_DEFS if d["name"] == "refrigerant_high")
        sensor = sim._sensor_by_name("refrigerant_high")
        assert sensor.target_on == original["target_on"]
        assert sensor.target_off == original["target_off"]

    @pytest.mark.asyncio
    async def test_activate_publishes_halt_event(self):
        sim = IceMakerSimulator()
        published = []
        async def capture_publish(client, suffix, payload):
            published.append((suffix, payload))
        sim.publish = capture_publish
        client = AsyncMock()
        await sim._on_compressor_overtemp_activate(client)
        assert any("ice_maker/event" in s for s, _ in published)

    def test_tick_halts_compressor_cycling_during_fault(self):
        sim = IceMakerSimulator()
        # Manually activate the already-registered fault
        sim._fault_state["compressor_overtemp"]["active"] = True
        sim._fault_state["compressor_overtemp"]["recover_at"] = 9e9
        # Advance well past compressor off time (300s default)
        for _ in range(100):
            sim.tick(dt=5.0)
        # compressor should not have flipped (still False)
        assert sim.compressor_on is False


class TestLowRefrigerantFault:
    @pytest.mark.asyncio
    async def test_activate_pins_refrigerant_low_target(self):
        sim = IceMakerSimulator()
        client = AsyncMock()
        await sim._on_low_refrigerant_activate(client)
        sensor = sim._sensor_by_name("refrigerant_low")
        assert sensor.target_on == 10.0
        assert sensor.target_off == 10.0

    @pytest.mark.asyncio
    async def test_recover_restores_refrigerant_low_targets(self):
        sim = IceMakerSimulator()
        client = AsyncMock()
        await sim._on_low_refrigerant_activate(client)
        await sim._on_low_refrigerant_recover(client)
        original = next(d for d in SENSOR_DEFS if d["name"] == "refrigerant_low")
        sensor = sim._sensor_by_name("refrigerant_low")
        assert sensor.target_on == original["target_on"]
        assert sensor.target_off == original["target_off"]

    def test_tick_allows_compressor_cycling_during_low_refrigerant(self):
        sim = IceMakerSimulator()
        # Manually activate the already-registered fault
        sim._fault_state["low_refrigerant"]["active"] = True
        sim._fault_state["low_refrigerant"]["recover_at"] = 9e9
        # Advance past the off-cycle (300s default)
        for _ in range(61):
            sim.tick(dt=5.0)
        # Compressor should have turned on (cycling continues under low_refrigerant)
        assert sim.compressor_on is True


class TestWaterInletBlockedFault:
    @pytest.mark.asyncio
    async def test_activate_pins_water_bath_target(self):
        sim = IceMakerSimulator()
        client = AsyncMock()
        await sim._on_water_inlet_blocked_activate(client)
        sensor = sim._sensor_by_name("water_bath")
        assert sensor.target_on == 20.0
        assert sensor.target_off == 20.0

    @pytest.mark.asyncio
    async def test_recover_restores_water_bath_targets(self):
        sim = IceMakerSimulator()
        client = AsyncMock()
        await sim._on_water_inlet_blocked_activate(client)
        await sim._on_water_inlet_blocked_recover(client)
        original = next(d for d in SENSOR_DEFS if d["name"] == "water_bath")
        sensor = sim._sensor_by_name("water_bath")
        assert sensor.target_on == original["target_on"]
        assert sensor.target_off == original["target_off"]


class TestDefrostStuckFault:
    @pytest.mark.asyncio
    async def test_activate_pins_hot_gas_valve_target(self):
        sim = IceMakerSimulator()
        client = AsyncMock()
        await sim._on_defrost_stuck_activate(client)
        sensor = sim._sensor_by_name("hot_gas_valve")
        assert sensor.target_on == 95.0
        assert sensor.target_off == 95.0

    @pytest.mark.asyncio
    async def test_recover_restores_hot_gas_valve_targets(self):
        sim = IceMakerSimulator()
        client = AsyncMock()
        await sim._on_defrost_stuck_activate(client)
        await sim._on_defrost_stuck_recover(client)
        original = next(d for d in SENSOR_DEFS if d["name"] == "hot_gas_valve")
        sensor = sim._sensor_by_name("hot_gas_valve")
        assert sensor.target_on == original["target_on"]
        assert sensor.target_off == original["target_off"]

    def test_tick_suppresses_ice_drop_events_during_fault(self):
        sim = IceMakerSimulator()
        # Manually activate the already-registered fault
        sim._fault_state["defrost_stuck"]["active"] = True
        sim._fault_state["defrost_stuck"]["recover_at"] = 9e9
        # Advance well past the ice drop interval (900s default)
        for _ in range(200):
            sim.tick(dt=5.0)
        ice_drop_events = [
            e for e in sim._pending_events if e.event == "ice_dropped"
        ]
        assert ice_drop_events == []
