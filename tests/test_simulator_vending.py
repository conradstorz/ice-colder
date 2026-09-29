# tests/test_simulator_vending.py
"""Tests for simulators/vending_machine.py — vending interface simulation."""

import asyncio
import contextlib
import json
from datetime import datetime, timezone
from unittest.mock import AsyncMock, patch

import pytest
from pydantic import ValidationError

from contracts.common import SubsystemCommand
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


class TestVendingCapabilities:
    def test_commands_and_contract(self):
        caps = _make_sim().build_capabilities()
        assert caps.subsystem == "vending"
        assert caps.commands == ["dispense", "water_valve", "payment/enable"]
        assert caps.contract_version == "0.5.0"


def _make_command(command: str, params: dict, request_id: str = "req-00000001"):
    """Build a SubsystemCommand, bypassing the model-level param validator.

    Real inbound traffic goes through `SubsystemCommand.model_validate`,
    which already enforces `COMMAND_PARAM_VALIDATORS` (e.g. water_valve's
    1-10 range) and raises before an out-of-range command can even be
    constructed — see TestWaterValveCommand's docstring. `model_construct`
    skips that validator so tests can exercise the handler's own
    defense-in-depth check directly, and so an in-range command here still
    matches exactly what `_command_loop` would have built.
    """
    return SubsystemCommand.model_construct(
        request_id=request_id,
        command=command,
        params=params,
        timestamp=datetime.now(timezone.utc),
    )


class TestDispenseCommand:
    """Command-channel `dispense`: shares `_run_ice_dispense`/`_run_water_dispense`."""

    @pytest.mark.asyncio
    async def test_runs_motor_once_and_publishes_hardware_dispenser(self):
        sim = _make_sim()
        client = AsyncMock()
        dispenser_states = []

        async def capture_publish(c, topic, payload):
            if topic == "hardware/dispenser":
                dispenser_states.append(payload.state)

        sim.publish = capture_publish
        run_ice = AsyncMock(wraps=sim._run_ice_dispense)
        sim._run_ice_dispense = run_ice

        cmd = _make_command("dispense", {"slot": 0})
        with patch("simulators.vending_machine.asyncio.sleep", new=AsyncMock()):
            ack = await sim._handle_command(client, cmd)

        assert run_ice.await_count == 1
        assert dispenser_states == ["motor_active", "fill_complete", "complete"]
        assert ack is None  # _handle_command publishes the ack, doesn't return it

    @pytest.mark.asyncio
    async def test_publishes_ok_ack_with_slot_result(self):
        sim = _make_sim()
        client = AsyncMock()
        published_acks = []

        async def capture_publish(c, topic, payload):
            if topic == "cmd/vending/ack":
                published_acks.append(payload)

        sim.publish = capture_publish
        cmd = _make_command("dispense", {"slot": 0})
        with patch("simulators.vending_machine.asyncio.sleep", new=AsyncMock()):
            await sim._handle_command(client, cmd)

        assert len(published_acks) == 1
        assert published_acks[0].status == "ok"
        assert published_acks[0].result == {"slot": 0}

    @pytest.mark.asyncio
    async def test_duplicate_request_id_runs_motor_only_once(self):
        """The most consequential duplicate in the system: a retried
        request_id must not run the dispense motor a second time."""
        sim = _make_sim()
        client = AsyncMock()
        sim.publish = AsyncMock()
        run_ice = AsyncMock(wraps=sim._run_ice_dispense)
        sim._run_ice_dispense = run_ice

        cmd = _make_command("dispense", {"slot": 0}, request_id="dup-req-1")
        with patch("simulators.vending_machine.asyncio.sleep", new=AsyncMock()):
            await sim._handle_command(client, cmd)
            await sim._handle_command(client, cmd)  # same request_id, sent again

        assert run_ice.await_count == 1

        # The two acks are identical (the second is the replayed cache entry).
        ack_calls = [
            call.args[2]
            for call in sim.publish.await_args_list
            if call.args[1] == "cmd/vending/ack"
        ]
        assert len(ack_calls) == 2
        assert ack_calls[0] == ack_calls[1]

    @pytest.mark.asyncio
    async def test_water_slot_shares_run_water_dispense(self):
        sim = _make_sim()
        client = AsyncMock()
        sim.publish = AsyncMock()
        run_water = AsyncMock(wraps=sim._run_water_dispense)
        sim._run_water_dispense = run_water

        cmd = _make_command("dispense", {"slot": 1})  # slot 1 is water in _make_config
        with patch("simulators.vending_machine.asyncio.sleep", new=AsyncMock()):
            await sim._handle_command(client, cmd)

        assert run_water.await_count == 1


class TestProductionDispenseTopicUnaffected:
    """`cmd/dispense` (production) must keep vending with no command-channel
    involvement at all — `_listen_for_commands` feeds `_dispense_command`,
    never `_handle_command`/`_commands`."""

    @pytest.mark.asyncio
    async def test_production_topic_still_vends_without_command_channel(self):
        sim = _make_sim()
        client = AsyncMock()
        dispenser_states = []

        async def capture_publish(c, topic, payload):
            if topic == "hardware/dispenser":
                dispenser_states.append(payload.state)

        sim.publish = capture_publish

        # Sabotage the command channel entirely: any use of it fails loudly,
        # proving the production path never touches it.
        async def _boom(*args, **kwargs):
            raise AssertionError(
                "production cmd/dispense must not touch the command channel"
            )

        sim._handle_command = _boom
        sim._commands.clear()

        # `_listen_for_commands` subscribes for itself via `self.subscribe`;
        # hand it a queue we control instead of racing a second, unrelated
        # subscription against it.
        captured_queue: asyncio.Queue = asyncio.Queue()

        async def fake_subscribe(_client, topic):
            assert topic == f"{sim.topic_prefix}/cmd/dispense", (
                "production topic must stay exactly cmd/dispense, unchanged"
            )
            return captured_queue

        sim.subscribe = fake_subscribe

        listener = asyncio.create_task(sim._listen_for_commands(client))
        try:
            await captured_queue.put((f"{sim.topic_prefix}/cmd/dispense", {"slot": 0}))
            slot = await asyncio.wait_for(sim._dispense_command.get(), timeout=2.0)
            with patch("simulators.vending_machine.asyncio.sleep", new=AsyncMock()):
                await sim._dispense_slot(client, slot)
        finally:
            listener.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await listener

        assert dispenser_states == ["motor_active", "fill_complete", "complete"]


class TestCustomerLoopDrivesProductionDispense:
    """Coverage gap this fix closes: `TestProductionDispenseTopicUnaffected`
    above calls `_dispense_slot` directly and never actually runs
    `_customer_loop` — the one function Task 7's refactor changed on the
    production path real sales use. This drives `_customer_loop` for real,
    together with `_listen_for_commands` (the same pairing
    `run_simulation`'s TaskGroup wires up), and feeds a slot through the
    genuine `cmd/dispense` subscription queue, the way the VMC's real
    response would arrive over MQTT."""

    @pytest.mark.asyncio
    async def test_customer_loop_dispenses_a_real_cmd_dispense_message(
        self, monkeypatch
    ):
        sim = _make_sim()
        client = AsyncMock()
        dispenser_states = []

        async def capture_publish(c, topic, payload):
            if topic == "hardware/dispenser":
                dispenser_states.append(payload.state)

        sim.publish = capture_publish

        # Keep the customer deterministic: no indecisive detour and no
        # repeat buy — both gated on random.random() in _customer_loop —
        # so the loop reaches exactly one _dispense_slot call.
        monkeypatch.setattr("simulators.vending_machine.random.random", lambda: 0.99)

        real_sleep = asyncio.sleep

        async def fast_sleep(_seconds):
            await real_sleep(0)  # still yields, just doesn't wait for real

        with patch("simulators.vending_machine.asyncio.sleep", new=fast_sleep):
            listener = asyncio.create_task(sim._listen_for_commands(client))
            customer = asyncio.create_task(sim._customer_loop(client))
            try:
                dispense_topic = f"{sim.topic_prefix}/cmd/dispense"
                queue = None
                for _ in range(500):
                    queue = next(
                        (q for t, q in sim._subscriptions if t == dispense_topic),
                        None,
                    )
                    if queue is not None:
                        break
                    await asyncio.sleep(0)
                assert queue is not None, "listener never subscribed to cmd/dispense"

                # The VMC's real dispense response, arriving on the
                # production topic exactly as the broker would deliver it.
                await queue.put((dispense_topic, {"slot": 0}))

                for _ in range(2000):
                    if dispenser_states[-1:] == ["complete"]:
                        break
                    await asyncio.sleep(0)
            finally:
                listener.cancel()
                customer.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await listener
                with contextlib.suppress(asyncio.CancelledError):
                    await customer

        # Slot 0 is the ice product in _make_config.
        assert dispenser_states == ["motor_active", "fill_complete", "complete"]


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
        sim = _make_sim()
        client = AsyncMock()
        sim.publish = AsyncMock()
        cmd = _make_command("water_valve", {"seconds": 1})
        with patch("simulators.vending_machine.asyncio.sleep", new=AsyncMock()):
            await sim._handle_command(client, cmd)

        ack = sim.publish.await_args_list[-1].args[2]
        assert ack.status == "ok"
        assert ack.result == {"seconds": 1}

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
        reasoning about the code."""
        sim = _make_sim()
        client = AsyncMock()
        sim.publish = AsyncMock()
        cmd = _make_command("water_valve", {"seconds": 10})

        task = asyncio.create_task(sim._handle_water_valve(client, cmd))
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
        closed rather than stuck open."""
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

        with pytest.raises(RuntimeError, match="simulated MQTT publish failure"):
            await sim._handle_water_valve(client, cmd)

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
