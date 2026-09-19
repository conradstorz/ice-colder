# contracts/generate.py
"""Generate the contracts' JSON Schema files.

Run after any model change: uv run python -m contracts.generate
tests/test_contract_schemas.py fails if the committed files drift.
"""

import json
from pathlib import Path

from pydantic import BaseModel, TypeAdapter

from services.mqtt_messages import IceMakerEvent, SensorReading, SubsystemHeartbeat

from contracts.ice_maker_monitor import (
    ChannelDescriptor,
    ChannelReading,
    CommandAck,
    MonitorCapabilities,
    MonitorCommand,
)
from contracts.vending_machine import (
    DispenserOutcome,
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
    "monitor_command": MonitorCommand,
    "command_ack": CommandAck,
}

VENDING_SCHEMA_DIR = Path("docs/contracts/vending-machine/schemas")

VENDING_MODELS = {
    "dispenser_outcome": DispenserOutcome,
    "fault_code": FaultCode,
    "payment_refund_command": PaymentRefundCommand,
    "payment_refund_result": PaymentRefundResult,
    "subsystem_capabilities": SubsystemCapabilities,
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
