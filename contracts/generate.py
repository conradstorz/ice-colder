# contracts/generate.py
"""Generate the contract's JSON Schema files.

Run after any model change: uv run python -m contracts.generate
tests/test_contract_schemas.py fails if the committed files drift.
"""

import json
from pathlib import Path

from services.mqtt_messages import IceMakerEvent, SensorReading, SubsystemHeartbeat

from contracts.ice_maker_monitor import (
    ChannelDescriptor,
    ChannelReading,
    CommandAck,
    MonitorCapabilities,
    MonitorCommand,
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


def generate(out_dir: Path = SCHEMA_DIR) -> list[Path]:
    out_dir.mkdir(parents=True, exist_ok=True)
    written = []
    for name, model in MODELS.items():
        path = out_dir / f"{name}.schema.json"
        schema = model.model_json_schema()
        path.write_text(json.dumps(schema, indent=2) + "\n", encoding="utf-8")
        written.append(path)
    return written


if __name__ == "__main__":
    for path in generate():
        print(f"wrote {path}")
