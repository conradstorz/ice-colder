# hardware/mdb_interface.py
"""
MDB interface stub.

Real MDB communication now lives on an ESP32 microcontroller and reaches
the RPi via MQTT (see services/mqtt_client.py and controller/vmc.py MQTT
handlers). This file is kept as a placeholder for the MDB protocol
constants and message parsing that may be useful when implementing the
ESP32 firmware or for testing.
"""
from loguru import logger


# MDB address constants (for reference / ESP32 firmware)
MDB_ADDR_VMC = 0x00
MDB_ADDR_CHANGER = 0x08
MDB_ADDR_CASHLESS_1 = 0x10
MDB_ADDR_CASHLESS_2 = 0x60

# Common MDB commands
MDB_CMD_RESET = 0x00
MDB_CMD_SETUP = 0x01
MDB_CMD_POLL = 0x03
MDB_CMD_VEND = 0x04
MDB_CMD_READER = 0x05


def parse_mdb_message(raw_data: bytes) -> dict:
    """
    Parse raw MDB bus bytes into a structured dict.

    This is a reference implementation — the actual parsing runs on the
    ESP32 and arrives here as a structured MQTT payload.
    """
    if not raw_data:
        return {"error": "empty"}

    address = raw_data[0] & 0xF8
    command = raw_data[0] & 0x07
    data = raw_data[1:] if len(raw_data) > 1 else b""

    return {
        "address": address,
        "command": command,
        "data": data.hex(),
        "raw_length": len(raw_data),
    }
