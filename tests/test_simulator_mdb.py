# tests/test_simulator_mdb.py
"""Tests for simulators/mdb_gateway.py — MDB payment gateway simulation."""

import pytest
from simulators.mdb_gateway import MDBGatewaySimulator, PaymentStrategy
from unittest.mock import AsyncMock


class TestInit:
    def test_creates_with_defaults(self):
        sim = MDBGatewaySimulator()
        assert sim.subsystem_name == "mdb"

    def test_devices_list(self):
        sim = MDBGatewaySimulator()
        assert len(sim.devices) == 3
        names = {d["name"] for d in sim.devices}
        assert names == {"coin_acceptor", "bill_validator", "card_reader"}


class TestPaymentStrategy:
    def test_pick_method_returns_valid(self):
        strategy = PaymentStrategy()
        methods = {strategy.pick_method() for _ in range(100)}
        assert methods.issubset({"cash_coin", "cash_bill", "card", "nfc"})

    def test_coin_denomination_valid(self):
        strategy = PaymentStrategy()
        coins = {strategy.pick_coin() for _ in range(100)}
        assert coins.issubset({0.25, 0.50, 1.00})

    def test_bill_denomination_valid(self):
        strategy = PaymentStrategy()
        bills = {strategy.pick_bill() for _ in range(100)}
        assert bills.issubset({1.00, 5.00, 10.00, 20.00})

    def test_card_amount_for_price(self):
        strategy = PaymentStrategy()
        amounts = [strategy.card_amount(3.00) for _ in range(100)]
        # All should be positive
        assert all(a > 0 for a in amounts)
        # At least some should differ from 3.00
        unique = set(round(a, 2) for a in amounts)
        assert len(unique) > 1


class TestPaymentStrategyExclusion:
    def test_pick_method_excludes_cash_coin(self):
        strategy = PaymentStrategy()
        for _ in range(100):
            method = strategy.pick_method(excluded={"cash_coin"})
            assert method != "cash_coin"

    def test_pick_method_excludes_multiple(self):
        strategy = PaymentStrategy()
        excluded = {"cash_coin", "cash_bill"}
        for _ in range(100):
            method = strategy.pick_method(excluded=excluded)
            assert method not in excluded

    def test_pick_method_returns_none_when_all_excluded(self):
        strategy = PaymentStrategy()
        result = strategy.pick_method(
            excluded={"cash_coin", "cash_bill", "card", "nfc"}
        )
        assert result is None

    def test_pick_method_no_exclusions_returns_valid(self):
        strategy = PaymentStrategy()
        methods = {strategy.pick_method() for _ in range(100)}
        assert methods.issubset({"cash_coin", "cash_bill", "card", "nfc"})


class TestMDBFaultRegistration:
    def test_four_faults_registered(self):
        sim = MDBGatewaySimulator()
        assert len(sim._fault_defs) == 4

    def test_fault_names(self):
        sim = MDBGatewaySimulator()
        names = {f.name for f in sim._fault_defs}
        assert names == {
            "coin_acceptor_jammed",
            "bill_validator_offline",
            "card_reader_error",
            "mdb_bus_reset",
        }


class TestCoinAcceptorJammedFault:
    @pytest.mark.asyncio
    async def test_activate_sets_device_state_to_error(self):
        sim = MDBGatewaySimulator()
        client = AsyncMock()
        await sim._on_coin_acceptor_jammed_activate(client)
        device = next(d for d in sim.devices if d["name"] == "coin_acceptor")
        assert device["state"] == "error"

    @pytest.mark.asyncio
    async def test_recover_sets_device_state_to_ready(self):
        sim = MDBGatewaySimulator()
        client = AsyncMock()
        await sim._on_coin_acceptor_jammed_activate(client)
        await sim._on_coin_acceptor_jammed_recover(client)
        device = next(d for d in sim.devices if d["name"] == "coin_acceptor")
        assert device["state"] == "ready"

    def test_excluded_methods_during_coin_fault(self):
        sim = MDBGatewaySimulator()
        sim._fault_state["coin_acceptor_jammed"] = {"active": True, "recover_at": 9e9}
        excluded = sim._build_payment_exclusions()
        assert "cash_coin" in excluded

    def test_no_exclusions_when_no_faults(self):
        sim = MDBGatewaySimulator()
        excluded = sim._build_payment_exclusions()
        assert excluded == set()


class TestBillValidatorOfflineFault:
    @pytest.mark.asyncio
    async def test_activate_sets_device_state_to_offline(self):
        sim = MDBGatewaySimulator()
        client = AsyncMock()
        await sim._on_bill_validator_offline_activate(client)
        device = next(d for d in sim.devices if d["name"] == "bill_validator")
        assert device["state"] == "offline"

    @pytest.mark.asyncio
    async def test_recover_sets_device_state_to_ready(self):
        sim = MDBGatewaySimulator()
        client = AsyncMock()
        await sim._on_bill_validator_offline_activate(client)
        await sim._on_bill_validator_offline_recover(client)
        device = next(d for d in sim.devices if d["name"] == "bill_validator")
        assert device["state"] == "ready"

    def test_excluded_methods_during_bill_fault(self):
        sim = MDBGatewaySimulator()
        sim._fault_state["bill_validator_offline"] = {"active": True, "recover_at": 9e9}
        excluded = sim._build_payment_exclusions()
        assert "cash_bill" in excluded


class TestCardReaderErrorFault:
    @pytest.mark.asyncio
    async def test_activate_sets_device_state_to_error(self):
        sim = MDBGatewaySimulator()
        client = AsyncMock()
        await sim._on_card_reader_error_activate(client)
        device = next(d for d in sim.devices if d["name"] == "card_reader")
        assert device["state"] == "error"

    @pytest.mark.asyncio
    async def test_recover_sets_device_state_to_ready(self):
        sim = MDBGatewaySimulator()
        client = AsyncMock()
        await sim._on_card_reader_error_activate(client)
        await sim._on_card_reader_error_recover(client)
        device = next(d for d in sim.devices if d["name"] == "card_reader")
        assert device["state"] == "ready"

    def test_excluded_methods_during_card_fault(self):
        sim = MDBGatewaySimulator()
        sim._fault_state["card_reader_error"] = {"active": True, "recover_at": 9e9}
        excluded = sim._build_payment_exclusions()
        assert "card" in excluded
        assert "nfc" in excluded


class TestMDBBusResetFault:
    @pytest.mark.asyncio
    async def test_activate_sets_all_devices_offline(self):
        sim = MDBGatewaySimulator()
        client = AsyncMock()
        await sim._on_mdb_bus_reset_activate(client)
        for device in sim.devices:
            assert device["state"] == "offline"

    @pytest.mark.asyncio
    async def test_recover_restores_all_devices_ready(self):
        from unittest.mock import patch
        sim = MDBGatewaySimulator()
        client = AsyncMock()
        await sim._on_mdb_bus_reset_activate(client)
        with patch("asyncio.sleep", new=AsyncMock()):
            await sim._on_mdb_bus_reset_recover(client)
        for device in sim.devices:
            assert device["state"] == "ready"

    def test_bus_reset_excludes_all_payment_methods(self):
        sim = MDBGatewaySimulator()
        sim._fault_state["mdb_bus_reset"] = {"active": True, "recover_at": 9e9}
        excluded = sim._build_payment_exclusions()
        assert excluded == {"cash_coin", "cash_bill", "card", "nfc"}


class TestHADiscovery:
    def test_returns_4_entities(self):
        sim = MDBGatewaySimulator()
        entities = sim.ha_discovery_entities()
        assert len(entities) == 4

    def test_three_device_binary_sensors(self):
        sim = MDBGatewaySimulator()
        entities = sim.ha_discovery_entities()
        binary = [e for e in entities if e["component"] == "binary_sensor"]
        assert len(binary) == 3
        names = {e["object_id"] for e in binary}
        assert names == {"coin_acceptor", "bill_validator", "card_reader"}

    def test_device_sensor_fields(self):
        sim = MDBGatewaySimulator()
        entities = sim.ha_discovery_entities()
        coin = next(e for e in entities if e["object_id"] == "coin_acceptor")
        assert coin["name"] == "MDB Coin Acceptor"
        assert coin["device_class"] == "running"
        assert coin["state_topic_suffix"] == "payment/status"
        assert coin["payload_on"] == "ON"

    def test_uptime_sensor(self):
        sim = MDBGatewaySimulator()
        entities = sim.ha_discovery_entities()
        uptime = next(e for e in entities if e["object_id"] == "uptime")
        assert uptime["component"] == "sensor"
        assert uptime["name"] == "MDB Gateway Uptime"
        assert uptime["device_class"] == "duration"
        assert uptime["state_topic_suffix"] == "heartbeat/mdb"

    def test_all_object_ids_unique(self):
        sim = MDBGatewaySimulator()
        entities = sim.ha_discovery_entities()
        ids = [e["object_id"] for e in entities]
        assert len(ids) == len(set(ids))
