# tests/test_simulator_vending.py
"""Tests for simulators/vending_machine.py — vending interface simulation."""

import asyncio
from unittest.mock import AsyncMock, patch

import pytest

from contracts.vending_machine import DispenserOutcome
from config.config_model import ConfigModel
from services.mqtt_messages import DispenserStatus
from simulators.vending_machine import VendingMachineSimulator, _classify_product


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


class TestClassifyProduct:
    def test_ice_product(self):
        assert _classify_product("Bagged Ice", "Ten Pounds Ice") == "ice"

    def test_water_by_name(self):
        assert _classify_product("Small Water", "WATER-1GAL") == "water"

    def test_water_by_sku(self):
        assert _classify_product("Jug Fill", "Five Gallons Water") == "water"

    def test_unknown_defaults_to_ice(self):
        assert _classify_product("Mystery", "UNKNOWN-SKU") == "ice"


class TestInit:
    def test_creates_with_config(self):
        sim = _make_sim()
        assert sim.subsystem_name == "vending"
        assert sim.num_buttons == 3

    def test_slot_types(self):
        sim = _make_sim()
        assert sim.slot_type(0) == "ice"
        assert sim.slot_type(1) == "water"
        assert sim.slot_type(2) == "water"

    def test_slot_map_keyed_by_slot_not_index(self):
        """Products out of list order with explicit slots must classify by
        their stable slot, not their position in the products list."""
        config = ConfigModel.model_validate(
            {
                "physical": {
                    "products": [
                        {"sku": "A", "name": "Small Water", "price": 1.0, "slot": 5},
                        {"sku": "B", "name": "Bagged Ice", "price": 1.0, "slot": 2},
                    ]
                }
            }
        )
        sim = VendingMachineSimulator(config=config)
        assert sim.slot_type(5) == "water"
        assert sim.slot_type(2) == "ice"

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

    def test_cabinet_temp_initialized(self):
        sim = _make_sim()
        assert sim._cabinet_temp == 22.0

    def test_water_flow_starts_at_zero(self):
        sim = _make_sim()
        assert sim._water_flow_total == 0.0


class TestDispenseSequence:
    @pytest.mark.asyncio
    async def test_ice_dispense_publishes_correct_states(self):
        sim = _make_sim()
        client = AsyncMock()
        published_states = []

        async def capture_publish(c, topic, payload):
            if "hardware/dispenser" in topic:
                if hasattr(payload, "state"):
                    published_states.append(payload.state)

        sim.publish = capture_publish
        await sim._run_ice_dispense(client, slot=0)

        assert published_states == ["motor_active", "fill_complete", "complete"]

    @pytest.mark.asyncio
    async def test_ice_dispense_hardware_sequence(self):
        """Verify hardware devices activate and deactivate in correct order."""
        sim = _make_sim()
        client = AsyncMock()
        hw_events = []

        async def capture_publish(c, topic, payload):
            if "hardware/io/" in topic:
                hw_events.append((payload.device, payload.state))

        sim.publish = capture_publish
        await sim._run_ice_dispense(client, slot=0)

        # Agitator and fan should start first
        assert ("agitator_motor", True) in hw_events
        assert ("fan", True) in hw_events
        # Auger starts after
        assert ("auger_motor", True) in hw_events
        # Bag full triggers, auger stops
        assert ("bag_full_sensor", True) in hw_events
        assert ("auger_motor", False) in hw_events
        # Bag drops
        assert ("bag_drop_solenoid", True) in hw_events
        assert ("bag_drop_solenoid", False) in hw_events
        assert ("bag_full_sensor", False) in hw_events
        # Everything off at end
        assert ("agitator_motor", False) in hw_events
        assert ("fan", False) in hw_events

    @pytest.mark.asyncio
    async def test_water_dispense_publishes_correct_states(self):
        sim = _make_sim()
        client = AsyncMock()
        published_states = []

        async def capture_publish(c, topic, payload):
            if "hardware/dispenser" in topic:
                if hasattr(payload, "state"):
                    published_states.append(payload.state)

        sim.publish = capture_publish
        await sim._run_water_dispense(client, slot=1)

        assert published_states[0] == "solenoid_open"
        assert published_states[-1] == "complete"

    @pytest.mark.asyncio
    async def test_water_dispense_hardware_sequence(self):
        """Verify water valve and flow sensor activate then deactivate."""
        sim = _make_sim()
        client = AsyncMock()
        hw_events = []

        async def capture_publish(c, topic, payload):
            if "hardware/io/" in topic:
                hw_events.append((payload.device, payload.state))

        sim.publish = capture_publish
        await sim._run_water_dispense(client, slot=1)

        assert ("water_valve_solenoid", True) in hw_events
        assert ("water_flow_sensor", True) in hw_events
        assert ("water_valve_solenoid", False) in hw_events
        assert ("water_flow_sensor", False) in hw_events

    @pytest.mark.asyncio
    async def test_water_dispense_increments_flow_total(self):
        sim = _make_sim()
        client = AsyncMock()
        sim.publish = AsyncMock()
        assert sim._water_flow_total == 0.0
        await sim._run_water_dispense(client, slot=1)
        assert sim._water_flow_total > 0.0


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
        assert temp["unit_of_measurement"] == "\u00b0C"
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
    def test_four_faults_registered(self):
        sim = VendingMachineSimulator()
        assert len(sim._fault_defs) == 4

    def test_fault_names(self):
        sim = VendingMachineSimulator()
        names = {f.name for f in sim._fault_defs}
        assert names == {
            "auger_jam",
            "bag_drop_solenoid_stuck",
            "water_valve_stuck_open",
            "ice_bin_empty",
        }


class TestAugerJamFault:
    @pytest.mark.asyncio
    async def test_fault_is_registered(self):
        sim = VendingMachineSimulator()
        names = {f.name for f in sim._fault_defs}
        assert "auger_jam" in names

    @pytest.mark.asyncio
    async def test_ice_dispense_publishes_timeout_during_fault(self):
        sim = VendingMachineSimulator()
        # Manually activate the already-registered fault
        sim._fault_state["auger_jam"]["active"] = True
        sim._fault_state["auger_jam"]["recover_at"] = 9e9
        published_states = []

        async def capture(client, topic, payload):
            if "hardware/dispenser" in topic and hasattr(payload, "state"):
                published_states.append(payload.state)

        sim.publish = capture
        sim._set_hw = AsyncMock()
        client = AsyncMock()
        # Patch asyncio.sleep to skip the 90s auger jam timeout
        with patch("asyncio.sleep", new=AsyncMock()):
            await sim._run_ice_dispense(client, slot=0)
        assert "timeout" in published_states
        assert "complete" not in published_states

    @pytest.mark.asyncio
    async def test_recover_logs_and_does_not_crash(self):
        sim = VendingMachineSimulator()
        client = AsyncMock()
        # Should complete without error
        await sim._on_auger_jam_recover(client)


class TestBagDropSolenoidStuckFault:
    @pytest.mark.asyncio
    async def test_ice_dispense_publishes_jam_during_fault(self):
        sim = VendingMachineSimulator()
        # Manually activate the already-registered fault
        sim._fault_state["bag_drop_solenoid_stuck"]["active"] = True
        sim._fault_state["bag_drop_solenoid_stuck"]["recover_at"] = 9e9
        published_states = []

        async def capture(client, topic, payload):
            if "hardware/dispenser" in topic and hasattr(payload, "state"):
                published_states.append(payload.state)

        sim.publish = capture
        sim._set_hw = AsyncMock()
        client = AsyncMock()
        # Patch asyncio.sleep to skip fill time wait
        with patch("asyncio.sleep", new=AsyncMock()):
            await sim._run_ice_dispense(client, slot=0)
        assert "jam" in published_states
        assert "complete" not in published_states


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
    async def test_ice_dispense_publishes_bin_empty_during_fault(self):
        sim = VendingMachineSimulator()
        sim._fault_state["ice_bin_empty"] = {"active": True, "recover_at": 9e9}
        published_states = []

        async def capture(client, topic, payload):
            if "hardware/dispenser" in topic and hasattr(payload, "state"):
                published_states.append(payload.state)

        sim.publish = capture
        sim._set_hw = AsyncMock()
        client = AsyncMock()
        await sim._run_ice_dispense(client, slot=0)
        assert "bin_empty" in published_states
        assert "complete" not in published_states

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
        assert sim._slot_types == {0: "ice", 1: "water"}

    def test_apply_products_updates_num_buttons_and_slot_types(self):
        sim = VendingMachineSimulator(config=ConfigModel())
        config = ConfigModel.model_validate(
            {
                "physical": {
                    "products": [
                        {"sku": "A", "name": "Bagged Ice", "price": 1.0, "slot": 3},
                        {"sku": "B", "name": "Small Water", "price": 1.0, "slot": 7},
                    ]
                }
            }
        )
        sim._apply_products(config.products)
        assert sim.num_buttons == 2
        assert sim._slot_types == {3: "ice", 7: "water"}


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


class TestTerminalOutcomesFollowContract:
    def _run(self, sim, coro):
        with patch("simulators.vending_machine.asyncio.sleep", new=AsyncMock()):
            asyncio.run(coro)
        return [
            call.args[2]
            for call in sim.publish.await_args_list
            if call.args[1] == "hardware/dispenser"
        ]

    @pytest.mark.parametrize(
        "fault,expected",
        [
            (None, DispenserOutcome.complete),
            ("ice_bin_empty", DispenserOutcome.bin_empty),
            ("auger_jam", DispenserOutcome.timeout),
            ("bag_drop_solenoid_stuck", DispenserOutcome.jam),
        ],
    )
    def test_ice_dispense_ends_with_a_contract_outcome(self, fault, expected):
        sim = _make_sim()
        sim.publish = AsyncMock()
        if fault:
            sim._fault_state[fault]["active"] = True
        statuses = self._run(sim, sim._run_ice_dispense(None, 0))
        last = statuses[-1]
        assert isinstance(last, DispenserStatus)
        assert DispenserOutcome(last.state) is expected

    def test_water_dispense_ends_complete(self):
        sim = _make_sim()
        sim.publish = AsyncMock()
        statuses = self._run(sim, sim._run_water_dispense(None, 1))
        assert DispenserOutcome(statuses[-1].state) is DispenserOutcome.complete
