"""Tests for the ice-maker monitor contract models."""

import pytest
from pydantic import ValidationError

from contracts.ice_maker_monitor import (
    CONTRACT_VERSION,
    ChannelDescriptor,
    ChannelReading,
    CommandAck,
    MonitorCapabilities,
    MonitorCommand,
)
from contracts.common import ACK_TIMEOUT_SECONDS, TESTABLE_COMMANDS
from contracts.common import CommandAck as CommonCommandAck
from contracts.common import SubsystemCommand


class TestChannelDescriptor:
    def test_valid(self):
        d = ChannelDescriptor(
            channel_id="compressor_current",
            kind="current",
            unit="A",
            description="Compressor draw",
            interval_seconds=5.0,
        )
        assert d.channel_id == "compressor_current"

    def test_rejects_bad_channel_id(self):
        with pytest.raises(ValidationError):
            ChannelDescriptor(channel_id="Bad-Id!", kind="binary", interval_seconds=5.0)

    def test_rejects_unknown_kind(self):
        with pytest.raises(ValidationError):
            ChannelDescriptor(channel_id="x", kind="pressure", interval_seconds=5.0)

    def test_rejects_interval_out_of_range(self):
        with pytest.raises(ValidationError):
            ChannelDescriptor(channel_id="x", kind="binary", interval_seconds=0)
        with pytest.raises(ValidationError):
            ChannelDescriptor(channel_id="x", kind="binary", interval_seconds=3601)


class TestMonitorCapabilities:
    def test_valid_with_defaults(self):
        caps = MonitorCapabilities(
            contract_version=CONTRACT_VERSION,
            brand="BrandX",
            model="IM-500",
            firmware="0.1.0",
        )
        assert caps.subsystem == "ice_maker"
        assert caps.channels == []
        assert caps.commands == []

    def test_contract_version_constant(self):
        # 1.1.0 -> 1.2.0: the command/ack models moved to contracts/common.py
        # (SubsystemCommand/CommandAck) and the ack gained an optional
        # `result` field — additive, minor bump.
        assert CONTRACT_VERSION == "1.2.0"


class TestChannelReading:
    def test_valid(self):
        r = ChannelReading(channel_id="bin_level", value=42.5)
        assert r.value == 42.5

    def test_rejects_bad_channel_id(self):
        with pytest.raises(ValidationError):
            ChannelReading(channel_id="Nope Space", value=1.0)


class TestMonitorCommand:
    def test_power_cycle_valid(self):
        cmd = MonitorCommand(
            request_id="req-12345678",
            command="power_cycle",
            params={"dwell_seconds": 30},
        )
        assert cmd.params["dwell_seconds"] == 30

    def test_power_cycle_requires_dwell(self):
        with pytest.raises(ValidationError):
            MonitorCommand(request_id="req-12345678", command="power_cycle")

    def test_power_cycle_dwell_bounds(self):
        for dwell in (4, 301):
            with pytest.raises(ValidationError):
                MonitorCommand(
                    request_id="req-12345678",
                    command="power_cycle",
                    params={"dwell_seconds": dwell},
                )

    def test_power_cycle_dwell_bounds_inclusive(self):
        # 5, 30 and 300 are all valid: the boundary values themselves and a
        # representative middle value must not be rejected by whatever
        # replaced the old model_validator.
        for dwell in (5, 30, 300):
            cmd = MonitorCommand(
                request_id="req-12345678",
                command="power_cycle",
                params={"dwell_seconds": dwell},
            )
            assert cmd.params["dwell_seconds"] == dwell

    def test_set_interval_bounds(self):
        for iv in (0.5, 3601):
            with pytest.raises(ValidationError):
                MonitorCommand(
                    request_id="req-12345678",
                    command="set_interval",
                    params={"interval_seconds": iv},
                )

    def test_set_interval_valid(self):
        cmd = MonitorCommand(
            request_id="req-12345678",
            command="set_interval",
            params={"interval_seconds": 60},
        )
        assert cmd.params["interval_seconds"] == 60

    def test_force_report_needs_no_params(self):
        cmd = MonitorCommand(request_id="req-12345678", command="force_report")
        assert cmd.params == {}

    def test_unknown_command_constructs_fine(self):
        # §1.1: the model never rejects an unknown command name — the
        # subsystem answers `unsupported` at runtime. Widening `command`
        # from a three-value Literal to `str` must not reject this.
        cmd = MonitorCommand(request_id="req-12345678", command="self_destruct")
        assert cmd.command == "self_destruct"
        assert cmd.params == {}


class TestWireCompatibility:
    """Literal, present-day payload dicts — not model round-trips — proving
    real firmware built against the pre-move contract still validates."""

    def test_literal_present_day_ack_validates(self):
        payload = {
            "request_id": "req-12345678",
            "command": "power_cycle",
            "status": "ok",
            "detail": "dwell 30s",
            "timestamp": "2026-09-28T12:00:00+00:00",
        }
        ack = CommandAck(**payload)
        assert ack.status == "ok"
        assert ack.detail == "dwell 30s"
        assert ack.result is None

    def test_literal_ack_with_result_also_validates(self):
        payload = {
            "request_id": "req-12345678",
            "command": "self_test",
            "status": "ok",
            "detail": None,
            "result": {"checks": [{"name": "bus", "pass": True, "detail": "ok"}]},
            "timestamp": "2026-09-28T12:00:00+00:00",
        }
        ack = CommandAck(**payload)
        assert ack.result == {"checks": [{"name": "bus", "pass": True, "detail": "ok"}]}

    def test_literal_power_cycle_command_validates(self):
        payload = {
            "request_id": "req-12345678",
            "command": "power_cycle",
            "params": {"dwell_seconds": 30},
            "timestamp": "2026-09-28T12:00:00+00:00",
        }
        cmd = MonitorCommand(**payload)
        assert cmd.command == "power_cycle"
        assert cmd.params["dwell_seconds"] == 30

    def test_literal_set_interval_command_validates(self):
        payload = {
            "request_id": "req-12345678",
            "command": "set_interval",
            "params": {"interval_seconds": 300},
            "timestamp": "2026-09-28T12:00:00+00:00",
        }
        cmd = MonitorCommand(**payload)
        assert cmd.params["interval_seconds"] == 300


class TestSharedCommandChannelIdentity:
    """MonitorCommand/CommandAck must be the *same* class objects as
    contracts.common's SubsystemCommand/CommandAck — an alias, not a
    subclass or a copy — so every existing import site is unaffected."""

    def test_monitor_command_is_subsystem_command(self):
        assert MonitorCommand is SubsystemCommand

    def test_command_ack_is_the_shared_class(self):
        assert CommandAck is CommonCommandAck

    def test_importable_from_ice_maker_monitor_module(self):
        from contracts.ice_maker_monitor import CommandAck as ImportedAck
        from contracts.ice_maker_monitor import MonitorCommand as ImportedCommand

        assert ImportedCommand is SubsystemCommand
        assert ImportedAck is CommonCommandAck


class TestTestableCommands:
    def test_standard_commands_present_for_every_subsystem(self):
        # Guard against a hollow pass: an empty/missing TESTABLE_COMMANDS
        # would make the loop below vacuously true.
        assert set(TESTABLE_COMMANDS) == {"vending", "ice_maker", "mdb"}
        for subsystem, commands in TESTABLE_COMMANDS.items():
            assert {"ping", "self_test", "force_report"} <= commands, subsystem

    def test_actuator_commands_under_the_right_subsystem(self):
        assert TESTABLE_COMMANDS["vending"] >= {"dispense", "water_valve"}
        assert TESTABLE_COMMANDS["ice_maker"] >= {"power_cycle"}
        assert TESTABLE_COMMANDS["mdb"] >= {
            "bill_acceptor_test",
            "coin_return_test",
            "card_reader_test",
        }

    def test_control_commands_absent_from_every_entry(self):
        assert set(TESTABLE_COMMANDS) == {"vending", "ice_maker", "mdb"}
        for subsystem, commands in TESTABLE_COMMANDS.items():
            assert "payment/enable" not in commands, subsystem
            assert "refund" not in commands, subsystem
            assert "set_interval" not in commands, subsystem

    def test_subsystems_match_the_rest_of_the_codebase(self):
        from contracts.vending_machine import EXPECTED_SUBSYSTEMS

        assert set(TESTABLE_COMMANDS) == set(EXPECTED_SUBSYSTEMS)


def test_ack_timeout_seconds_is_the_contracts_own_constant():
    assert ACK_TIMEOUT_SECONDS == 10.0


class TestCommandAck:
    def test_valid(self):
        ack = CommandAck(
            request_id="req-12345678",
            command="power_cycle",
            status="rejected",
            detail="lockout",
        )
        assert ack.status == "rejected"

    def test_rejects_unknown_status(self):
        with pytest.raises(ValidationError):
            CommandAck(request_id="r-12345678", command="power_cycle", status="maybe")


class TestMonitorCapabilitiesIdentity:
    def test_is_a_subsystem_capabilities(self):
        from contracts.vending_machine import SubsystemCapabilities

        assert issubclass(MonitorCapabilities, SubsystemCapabilities)

    def test_subsystem_fixed_to_ice_maker(self):
        caps = MonitorCapabilities(
            contract_version="1.1.0", brand="B", model="M", firmware="f"
        )
        assert caps.subsystem == "ice_maker"
        with pytest.raises(ValidationError):
            MonitorCapabilities(
                subsystem="vending",
                contract_version="1.1.0",
                brand="B",
                model="M",
                firmware="f",
            )

    def test_brand_model_still_required(self):
        with pytest.raises(ValidationError):
            MonitorCapabilities(contract_version="1.1.0", firmware="f")

    def test_identity_fields_optional_and_accepted(self):
        caps = MonitorCapabilities(
            contract_version="1.1.0",
            brand="B",
            model="M",
            firmware="f",
            hardware_id="02:aa:bb:cc:dd:ee",
            ip="10.0.0.5",
        )
        assert caps.hardware_id == "02:aa:bb:cc:dd:ee"

    def test_contract_version_is_1_2_0(self):
        from contracts.ice_maker_monitor import CONTRACT_VERSION

        assert CONTRACT_VERSION == "1.2.0"
