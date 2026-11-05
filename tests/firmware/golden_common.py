"""Checks shared by the vending and MDB firmware golden-file tests.

Pure file checks: no broker, no flag. Pydantic ignores extra keys, so each
golden's exact key set is asserted separately.
"""

from __future__ import annotations

import json
from pathlib import Path

from ice_contracts.board.common import CommandAck
from ice_contracts.board.mqtt_messages import SubsystemHeartbeat
from ice_contracts.board.vending_machine import SubsystemCapabilities

ACK_KEYS = {"request_id", "command", "status", "phase"}
CAPABILITIES_KEYS = {
    "subsystem", "firmware", "contract_version", "brand", "model",
    "hardware_id", "ip", "channels", "commands",
}  # fmt: skip
# golden file stem -> (model, exact key set); identical on both boards
COMMON_MODELS = {
    "capabilities": (SubsystemCapabilities, CAPABILITIES_KEYS),
    "heartbeat": (SubsystemHeartbeat, {"subsystem", "uptime_seconds"}),
    "lwt": (SubsystemHeartbeat, {"subsystem", "uptime_seconds"}),
    "ack_ok_accepted": (CommandAck, ACK_KEYS),
    "ack_ok_completed": (CommandAck, ACK_KEYS | {"result"}),
    "ack_failed": (CommandAck, ACK_KEYS | {"detail"}),
    "ack_rejected": (CommandAck, ACK_KEYS | {"detail"}),
    "ack_unsupported": (CommandAck, ACK_KEYS | {"detail"}),
}  # fmt: skip


def load(golden_dir: Path, stem: str) -> dict:
    return json.loads((golden_dir / f"{stem}.json").read_text(encoding="utf-8"))


def check_validates_with_exact_keys(models: dict, golden_dir: Path, stem: str) -> None:
    """The golden validates against its contract model and has exactly its keys."""
    model, keys = models[stem]
    data = load(golden_dir, stem)
    model.model_validate(data)
    assert set(data) == keys
