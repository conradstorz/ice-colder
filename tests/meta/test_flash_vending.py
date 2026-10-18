"""Board detection for scripts/flash_vending.py. Pure: no subprocess, no serial ports."""

from __future__ import annotations

import json

from scripts.flash_vending import SerialPort, candidate_boards, parse_device_list


def _listing(*entries: dict) -> str:
    return json.dumps(list(entries))


def test_empty_or_malformed_listing_is_no_ports() -> None:
    assert parse_device_list("") == []
    assert parse_device_list("[]") == []
    assert parse_device_list("not json") == []
    assert parse_device_list('{"port": "COM3"}') == []


def test_parses_port_fields_and_vid() -> None:
    ports = parse_device_list(
        _listing(
            {
                "port": "COM5",
                "description": "USB-SERIAL CH340",
                "hwid": "USB VID:PID=1A86:7523",
            }
        )
    )
    assert ports == [SerialPort("COM5", "USB-SERIAL CH340", "USB VID:PID=1A86:7523")]
    assert ports[0].vid == "1A86"


def test_vid_is_none_without_usb_id() -> None:
    assert SerialPort("COM1", "Communications Port", "ACPI\PNP0501\1").vid is None


def test_candidates_keep_only_known_bridges() -> None:
    ports = parse_device_list(
        _listing(
            {
                "port": "COM1",
                "description": "Communications Port",
                "hwid": "ACPI\PNP0501\1",
            },
            {"port": "COM5", "description": "CH340", "hwid": "USB VID:PID=1A86:7523"},
            {
                "port": "/dev/ttyUSB0",
                "description": "CP2102",
                "hwid": "USB VID:PID=10c4:ea60",
            },
            {
                "port": "COM9",
                "description": "Arduino Uno",
                "hwid": "USB VID:PID=2341:0043",
            },
        )
    )
    assert [p.port for p in candidate_boards(ports)] == ["COM5", "/dev/ttyUSB0"]


def test_entries_without_port_are_dropped() -> None:
    assert (
        parse_device_list(
            _listing({"description": "x", "hwid": "USB VID:PID=1A86:7523"})
        )
        == []
    )
