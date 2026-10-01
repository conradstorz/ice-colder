"""Unit tests for the pure `build_window` view model (subsystem-windows
design §4.6). No I/O; `now` and `tz` are injected."""

from datetime import datetime, timezone

from web_interface.subsystem_window import build_window


class _FakeAvailability:
    """Inhibits exactly the commands named in `inhibited`."""

    def __init__(self, inhibited=frozenset({"dispense"})):
        self._inhibited = inhibited

    def command_inhibited(self, command: str) -> bool:
        return command in self._inhibited


def _signal(
    value, *, text=None, updated_at=1000.0, transition_at=1000.0, transitions_seen=0
):
    return {
        "value": value,
        "text": text,
        "updated_at": updated_at,
        "transition_at": transition_at,
        "transitions_seen": transitions_seen,
    }


def _ice_maker_row(*, alive=True):
    """Declares a `compressor` temperature input and a `compressor_run`
    binary output, plus a `fan` binary output — the example from the brief."""
    return {
        "alive": alive,
        "channels": [
            {
                "channel_id": "compressor",
                "kind": "temperature",
                "unit": "C",
                "description": "Compressor temperature",
                "interval_seconds": 30,
                "direction": "input",
                "driven_by": None,
            },
            {
                "channel_id": "compressor_run",
                "kind": "binary",
                "unit": "",
                "description": "",
                "interval_seconds": 30,
                "direction": "output",
                "driven_by": "dispense",
            },
            {
                "channel_id": "fan",
                "kind": "binary",
                "unit": "",
                "description": "Condenser fan",
                "interval_seconds": 30,
                "direction": "output",
                "driven_by": "fan",
            },
        ],
        "commands": ["ping", "self_test", "force_report", "dispense", "fan"],
    }


def test_declaration_order_preserved():
    row = _ice_maker_row()
    signals = {
        "compressor": _signal(5.0),
        "compressor_run": _signal(1.0),
        "fan": _signal(0.0),
    }
    window = build_window(
        row, signals, None, temp_range=(0.0, 10.0), now=1000.0, tz=timezone.utc
    )

    assert [s["id"] for s in window["inputs"]] == ["compressor"]
    assert [s["id"] for s in window["outputs"]] == ["compressor_run", "fan"]
    assert [c["name"] for c in window["controls"]["actuators"]] == ["dispense", "fan"]
    assert window["controls"]["standard"] == ["ping", "self_test", "force_report"]


def test_analog_vs_digital_split_and_in_range():
    row = _ice_maker_row()
    signals = {
        "compressor": _signal(5.0),
        "compressor_run": _signal(1.0),
        "fan": _signal(0.0),
    }
    window = build_window(
        row, signals, None, temp_range=(0.0, 10.0), now=1000.0, tz=timezone.utc
    )

    compressor = window["inputs"][0]
    assert compressor["digital"] is False
    assert compressor["in_range"] is True

    compressor_run, fan = window["outputs"]
    assert compressor_run["digital"] is True
    assert compressor_run["in_range"] is None
    assert fan["digital"] is True
    assert fan["in_range"] is None

    # Out of range temperature.
    signals_out = dict(signals)
    signals_out["compressor"] = _signal(15.0)
    window_out = build_window(
        row, signals_out, None, temp_range=(0.0, 10.0), now=1000.0, tz=timezone.utc
    )
    assert window_out["inputs"][0]["in_range"] is False


def test_state_none_when_not_alive_even_with_fresh_signal():
    row = _ice_maker_row(alive=False)
    signals = {
        "compressor": _signal(5.0),
        "compressor_run": _signal(1.0, updated_at=1000.0),
        "fan": _signal(0.0),
    }
    window = build_window(
        row, signals, None, temp_range=(0.0, 10.0), now=1000.0, tz=timezone.utc
    )
    assert window["inputs"][0]["state"] == "none"
    assert window["outputs"][0]["state"] == "none"
    # `in_range` is derived from the stale value alone, per the brief's rule
    # ("in_range only for kind == temperature with a value"); it is NOT
    # forced to None by a dead board. A template must gate the OK/Out of
    # range badge on `state != "none"` itself if it wants to hide this.
    assert window["inputs"][0]["in_range"] is True


def test_state_none_when_no_signal():
    row = _ice_maker_row(alive=True)
    signals = {}  # nothing reported yet for any declared channel
    window = build_window(
        row, signals, None, temp_range=(0.0, 10.0), now=1000.0, tz=timezone.utc
    )
    assert window["inputs"][0]["state"] == "none"
    assert window["outputs"][0]["state"] == "none"
    assert window["inputs"][0]["value"] is None
    assert window["outputs"][0]["value"] is None


def test_state_on_off_when_alive_with_signal():
    row = _ice_maker_row()
    signals = {
        "compressor": _signal(5.0),
        "compressor_run": _signal(1.0),
        "fan": _signal(0.0),
    }
    window = build_window(
        row, signals, None, temp_range=(0.0, 10.0), now=1000.0, tz=timezone.utc
    )
    compressor_run, fan = window["outputs"]
    assert compressor_run["state"] == "on"
    assert fan["state"] == "off"


def test_inhibited_only_via_driven_by():
    row = _ice_maker_row()
    signals = {
        "compressor": _signal(5.0),
        "compressor_run": _signal(1.0),
        "fan": _signal(0.0),
    }
    availability = _FakeAvailability()
    window = build_window(
        row, signals, availability, temp_range=(0.0, 10.0), now=1000.0, tz=timezone.utc
    )

    compressor_run, fan = window["outputs"]
    assert compressor_run["inhibited"] is True  # driven_by="dispense"
    assert fan["inhibited"] is False  # driven_by="fan", not inhibited

    actuators = {c["name"]: c["inhibited"] for c in window["controls"]["actuators"]}
    assert actuators["dispense"] is True
    assert actuators["fan"] is False

    # Standard commands carry no inhibited flag at all (they're plain names).
    assert window["controls"]["standard"] == ["ping", "self_test", "force_report"]


def test_availability_none_inhibits_nothing():
    row = _ice_maker_row()
    signals = {
        "compressor": _signal(5.0),
        "compressor_run": _signal(1.0),
        "fan": _signal(0.0),
    }
    window = build_window(
        row, signals, None, temp_range=(0.0, 10.0), now=1000.0, tz=timezone.utc
    )
    compressor_run, fan = window["outputs"]
    assert compressor_run["inhibited"] is False
    assert fan["inhibited"] is False
    actuators = {c["name"]: c["inhibited"] for c in window["controls"]["actuators"]}
    assert actuators["dispense"] is False
    assert actuators["fan"] is False


def test_dwell_since_start_flag():
    row = _ice_maker_row()
    signals = {
        "compressor": _signal(5.0),
        "compressor_run": _signal(1.0, transitions_seen=0),
        "fan": _signal(0.0, transitions_seen=3),
    }
    window = build_window(
        row, signals, None, temp_range=(0.0, 10.0), now=1000.0, tz=timezone.utc
    )
    compressor_run, fan = window["outputs"]
    assert compressor_run["dwell_since_start"] is True
    assert fan["dwell_since_start"] is False


def test_clock_strings_and_ages():
    row = _ice_maker_row()
    updated_at = 1_700_000_123.0
    transition_at = 1_700_000_000.0
    now = 1_700_000_200.0
    signals = {
        "compressor": _signal(5.0, updated_at=updated_at, transition_at=transition_at),
        "compressor_run": _signal(
            1.0, updated_at=updated_at, transition_at=transition_at
        ),
        "fan": _signal(0.0, updated_at=updated_at, transition_at=transition_at),
    }
    window = build_window(
        row, signals, None, temp_range=(0.0, 10.0), now=now, tz=timezone.utc
    )
    compressor = window["inputs"][0]
    expected_updated_clock = datetime.fromtimestamp(updated_at, timezone.utc).strftime(
        "%H:%M:%S"
    )
    expected_transition_clock = datetime.fromtimestamp(
        transition_at, timezone.utc
    ).strftime("%H:%M:%S")
    assert compressor["updated_clock"] == expected_updated_clock
    assert compressor["transition_clock"] == expected_transition_clock
    assert compressor["age_seconds"] == now - updated_at
    assert compressor["dwell_seconds"] == now - transition_at


def test_empty_row_yields_empty_sections_no_exception():
    window = build_window(
        {}, {}, None, temp_range=(0.0, 10.0), now=0.0, tz=timezone.utc
    )
    assert window["inputs"] == []
    assert window["outputs"] == []
    assert window["controls"] == {"actuators": [], "standard": []}
    assert window["alive"] is False
