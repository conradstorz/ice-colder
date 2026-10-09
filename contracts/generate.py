# contracts/generate.py
"""Generate the contracts' JSON Schema files.

Run after any model change: uv run python -m contracts.generate
tests/test_contract_schemas.py fails if the committed files drift.
"""

import json
from pathlib import Path

from pydantic import BaseModel, TypeAdapter

from services.dispenser_schema import SlotProfile
from services.mqtt_messages import (
    DispenseCommand,
    IceMakerEvent,
    SensorReading,
    SubsystemHeartbeat,
)

from contracts.common import CommandAck, SubsystemCommand
from contracts.ice_maker_monitor import (
    ChannelDescriptor,
    ChannelReading,
    MonitorCapabilities,
)
from contracts.vending_machine import (
    DispenserOutcome,
    DispenseStep,
    FaultCode,
    PaymentRefundCommand,
    PaymentRefundResult,
    SubsystemCapabilities,
)

SCHEMA_DIR = Path("docs/contracts/ice-maker-monitor/schemas")

MODELS = {
    "sensor_reading": SensorReading,
    "ice_maker_event": IceMakerEvent,
    "subsystem_heartbeat": SubsystemHeartbeat,
    "channel_descriptor": ChannelDescriptor,
    "monitor_capabilities": MonitorCapabilities,
    "channel_reading": ChannelReading,
    # Key stays "monitor_command" (not renamed to "subsystem_command"): the
    # generated file is docs/contracts/ice-maker-monitor/schemas/
    # monitor_command.schema.json, which real ice-maker firmware may already
    # reference. MonitorCommand is an alias for SubsystemCommand (the same
    # class object, contracts/ice_maker_monitor.py), so this generates the
    # identical schema either way.
    "monitor_command": SubsystemCommand,
    "command_ack": CommandAck,
}

VENDING_SCHEMA_DIR = Path("docs/contracts/vending-machine/schemas")

VENDING_MODELS = {
    "dispense_command": DispenseCommand,
    "dispense_step": DispenseStep,
    "dispenser_outcome": DispenserOutcome,
    "fault_code": FaultCode,
    "payment_refund_command": PaymentRefundCommand,
    "payment_refund_result": PaymentRefundResult,
    "slot_profile": SlotProfile,
    "subsystem_capabilities": SubsystemCapabilities,
    "subsystem_command": SubsystemCommand,
    "command_ack": CommandAck,
}

CONTRACTS: dict[str, tuple[Path, dict]] = {
    "ice-maker-monitor": (SCHEMA_DIR, MODELS),
    "vending-machine": (VENDING_SCHEMA_DIR, VENDING_MODELS),
}


def schema_for(model) -> dict:
    """JSON Schema for a Pydantic model or a plain Enum."""
    if isinstance(model, type) and issubclass(model, BaseModel):
        return model.model_json_schema()
    return TypeAdapter(model).json_schema()


def generate(contracts: dict | None = None) -> list[Path]:
    written = []
    for schema_dir, models in (contracts or CONTRACTS).values():
        schema_dir.mkdir(parents=True, exist_ok=True)
        for name, model in models.items():
            path = schema_dir / f"{name}.schema.json"
            path.write_text(
                json.dumps(schema_for(model), indent=2) + "\n", encoding="utf-8"
            )
            written.append(path)
    return written


if __name__ == "__main__":
    for path in generate():
        print(f"wrote {path}")
