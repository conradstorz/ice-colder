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
        assert CONTRACT_VERSION == "1.0.0"


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

    def test_set_interval_bounds(self):
        for iv in (0.5, 3601):
            with pytest.raises(ValidationError):
                MonitorCommand(
                    request_id="req-12345678",
                    command="set_interval",
                    params={"interval_seconds": iv},
                )

    def test_force_report_needs_no_params(self):
        cmd = MonitorCommand(request_id="req-12345678", command="force_report")
        assert cmd.params == {}

    def test_unknown_command_rejected(self):
        with pytest.raises(ValidationError):
            MonitorCommand(request_id="req-12345678", command="self_destruct")


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
