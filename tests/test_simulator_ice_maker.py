# tests/test_simulator_ice_maker.py
"""Tests for simulators/ice_maker.py — ice maker temperature simulation."""
import pytest

from simulators.ice_maker import IceMakerSimulator, ThermalSensor, SENSOR_DEFS


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
