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
from contracts.common import (
    ACK_TIMEOUT_SECONDS,
    COMPLETION_TIMEOUTS,
    TESTABLE_COMMANDS,
)
from contracts.common import CommandAck as CommonCommandAck
from contracts.common import SubsystemCommand
from services.dispensers import validate_document
from tests.dispenser_fixtures import GOOD, ICE, WATER


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

    def test_direction_and_driven_by_default(self):
        d = ChannelDescriptor(channel_id="x", kind="binary", interval_seconds=5.0)
        assert d.direction == "input"
        assert d.driven_by is None

    def test_direction_and_driven_by_round_trip(self):
        d = ChannelDescriptor(
            channel_id="auger_motor",
            kind="binary",
            interval_seconds=5.0,
            direction="output",
            driven_by="dispense",
        )
        data = d.model_dump()
        assert data["direction"] == "output"
        assert data["driven_by"] == "dispense"

    def test_rejects_bad_direction(self):
        with pytest.raises(ValidationError):
            ChannelDescriptor(
                channel_id="x",
                kind="binary",
                interval_seconds=5.0,
                direction="sideways",
            )


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
        # 1.3.0 -> 1.4.0: ChannelDescriptor gains direction/driven_by
        # (additive).
        assert CONTRACT_VERSION == "1.4.0"


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
        # 2026-09-29 completion-table amendment: a present-day payload with
        # no "phase" key at all must still validate, and mean exactly what
        # it always meant -- this ack IS the command's outcome.
        assert ack.phase == "completed"

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


class TestDispenseParamsValidator:
    """COMMAND_PARAM_VALIDATORS["dispense"] rejects a params dict that
    doesn't carry a full DispenseCommand (mechanism, profile) and accepts
    one that does."""

    def test_rejects_bare_slot(self):
        with pytest.raises(ValidationError):
            SubsystemCommand(
                request_id="req-12345678", command="dispense", params={"slot": 1}
            )

    def test_accepts_full_payload(self):
        report = validate_document(GOOD, [ICE, WATER])
        profile = report.profiles[1]
        cmd = SubsystemCommand(
            request_id="req-12345678",
            command="dispense",
            params={
                "slot": 1,
                "mechanism": "bagged_ice",
                "profile": profile.model_dump(mode="json"),
            },
        )
        assert cmd.command == "dispense"


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

    def test_contract_version_is_1_4_0(self):
        from contracts.ice_maker_monitor import CONTRACT_VERSION

        assert CONTRACT_VERSION == "1.4.0"


class TestCompletionTimeouts:
    """2026-09-29 completion-table amendment: every long-running command
    must have a COMPLETION_TIMEOUTS entry, and every entry must actually be
    a testable command — the two tables cannot drift apart the way
    TESTABLE_COMMANDS and SubsystemCapabilities.commands did before
    (Copilot review, PR 22, id=4128088689)."""

    # Verified against each simulator handler (not assumed): the three
    # standard commands and the three mdb actuator commands all finish
    # within their single ack — no `await asyncio.sleep`, no background
    # task — so they carry no completion-timeout entry at all.
    _IMMEDIATE = {
        ("vending", "ping"),
        ("vending", "self_test"),
        ("vending", "force_report"),
        ("mdb", "ping"),
        ("mdb", "self_test"),
        ("mdb", "force_report"),
        ("mdb", "bill_acceptor_test"),
        ("mdb", "coin_return_test"),
        ("mdb", "card_reader_test"),
        ("ice_maker", "ping"),
        ("ice_maker", "self_test"),
        ("ice_maker", "force_report"),
    }
    _LONG_RUNNING = {
        ("vending", "dispense"),
        ("vending", "water_valve"),
        ("ice_maker", "power_cycle"),
    }

    def test_every_testable_command_is_classified(self):
        all_testable = {
            (subsystem, command)
            for subsystem, commands in TESTABLE_COMMANDS.items()
            for command in commands
        }
        assert all_testable == self._IMMEDIATE | self._LONG_RUNNING

    def test_long_running_commands_have_completion_timeouts(self):
        assert set(COMPLETION_TIMEOUTS) == self._LONG_RUNNING

    def test_immediate_commands_have_no_completion_timeout(self):
        assert self._IMMEDIATE.isdisjoint(COMPLETION_TIMEOUTS)

    def test_dispense_completion_timeout_is_fixed_120s(self):
        timeout_fn = COMPLETION_TIMEOUTS[("vending", "dispense")]
        # No caller-chosen duration to derive from — same value regardless
        # of params, and well above the 90s worst case (auger jam) in
        # simulators/vending_machine.py today.
        assert timeout_fn({}) == 120.0
        assert timeout_fn({"slot": 3}) == 120.0

    @pytest.mark.parametrize("seconds,expected", [(1, 6.0), (10, 15.0)])
    def test_water_valve_completion_timeout_derived_from_seconds(
        self, seconds, expected
    ):
        timeout_fn = COMPLETION_TIMEOUTS[("vending", "water_valve")]
        assert timeout_fn({"seconds": seconds}) == expected

    @pytest.mark.parametrize("dwell,expected", [(5, 35.0), (300, 330.0)])
    def test_power_cycle_completion_timeout_derived_from_dwell(self, dwell, expected):
        # power_cycle's dwell_seconds can legitimately be 300s (the
        # ice-maker's own lockout window) -- a fixed 120s timeout would
        # spuriously fail that legitimate case. This must scale with it.
        timeout_fn = COMPLETION_TIMEOUTS[("ice_maker", "power_cycle")]
        assert timeout_fn({"dwell_seconds": dwell}) == expected
