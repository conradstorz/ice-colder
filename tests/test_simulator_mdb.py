# tests/test_simulator_mdb.py
"""Tests for simulators/mdb_gateway.py — MDB payment gateway simulation."""
import pytest

from simulators.mdb_gateway import MDBGatewaySimulator, PaymentStrategy


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
