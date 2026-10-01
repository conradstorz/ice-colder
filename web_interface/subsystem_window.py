"""Pure view model for one subsystem's live window (subsystem-windows design
§4.6). `build_window` has no I/O: `now` (epoch seconds) and `tz` are injected
by the caller so this stays deterministic and unit-testable without HTTP or
a clock.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from contracts.common import STANDARD_COMMANDS


def _clock(ts: float | None, tz) -> str | None:
    if ts is None:
        return None
    return datetime.fromtimestamp(ts, tz).strftime("%H:%M:%S")


def _signal_view(
    channel: dict,
    signals: dict,
    live: bool,
    availability,
    temp_range: tuple[float, float],
    now: float,
    tz,
) -> dict[str, Any]:
    channel_id = channel["channel_id"]
    kind = channel.get("kind")
    digital = kind == "binary"
    driven_by = channel.get("driven_by")
    sig = signals.get(channel_id)

    value = sig.get("value") if sig is not None else None
    text = sig.get("text") if sig is not None else None
    updated_at = sig.get("updated_at") if sig is not None else None
    transition_at = sig.get("transition_at") if sig is not None else None
    transitions_seen = sig.get("transitions_seen") if sig is not None else None

    if not live or sig is None:
        state = "none"
    else:
        state = "on" if value >= 0.5 else "off"

    in_range: bool | None = None
    if kind == "temperature" and value is not None:
        temp_min, temp_max = temp_range
        in_range = temp_min <= value <= temp_max

    inhibited = (
        bool(driven_by)
        and availability is not None
        and availability.command_inhibited(driven_by)
    )

    return {
        "id": channel_id,
        "label": channel.get("description") or channel_id.replace("_", " "),
        "kind": kind,
        "unit": channel.get("unit", ""),
        "digital": digital,
        "state": state,
        "value": value,
        "text": text,
        "updated_clock": _clock(updated_at, tz),
        "age_seconds": (now - updated_at) if updated_at is not None else None,
        "dwell_seconds": (now - transition_at) if transition_at is not None else None,
        "transition_clock": _clock(transition_at, tz),
        "dwell_since_start": transitions_seen == 0 if sig is not None else True,
        "inhibited": inhibited,
        "in_range": in_range,
    }


def build_window(
    row: dict,
    signals: dict,
    availability,
    *,
    temp_range: tuple[float, float],
    now: float,
    tz=None,
) -> dict:
    """Turn one board's health-summary row plus its signals into the
    template's view model. `row` is `get_summary()["subsystems"][name]`;
    `signals` is `get_summary()["signals"].get(name, {})`.

    `row["alive"]` only means a heartbeat was ever seen; `stale` (set by
    HealthMonitor.get_summary() once the heartbeat has timed out) can be
    True while `alive` stays True. Spec §4.6: state is "none" whenever the
    board is not alive (stale or never seen), so liveness for rendering is
    `alive and not stale`, not `alive` alone -- otherwise a timed-out
    board's cached signals keep rendering as if live."""
    live = bool(row.get("alive", False)) and not bool(row.get("stale", False))
    channels = row.get("channels") or []
    commands = row.get("commands") or []

    inputs = []
    outputs = []
    for channel in channels:
        view = _signal_view(channel, signals, live, availability, temp_range, now, tz)
        if channel.get("direction") == "output":
            outputs.append(view)
        else:
            inputs.append(view)

    actuators = [
        {
            "name": command,
            "inhibited": availability is not None
            and availability.command_inhibited(command),
        }
        for command in commands
        if command not in STANDARD_COMMANDS
    ]
    standard = [command for command in STANDARD_COMMANDS if command in commands]

    return {
        "alive": live,
        "inputs": inputs,
        "outputs": outputs,
        "controls": {"actuators": actuators, "standard": standard},
    }
