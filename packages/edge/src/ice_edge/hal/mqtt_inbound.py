"""Telemetry-only MQTT inbound handlers: a pure fan-out.

``TelemetryRouter`` (the fifth piece carved off the VMC god object) validates
each inbound telemetry payload, logs it at the wire level and publishes the
decoded values to typed ``Subscribers`` lists (``subscribe_hardware_io`` ...
``subscribe_capabilities``). It holds no reference to health, availability,
the dispenser gate or the fault service: ``Machine`` subscribes the
consumers -- ``HealthTelemetry`` (payload to ``HealthMonitor``), ``IcePolicy``
(bin-sensor ICE-101 clear), ``Availability``, and
``DispenserProfileGate.on_vending_capabilities``. An invalid payload reaches
no subscriber.

``capabilities`` is the public raw-document store. ``capabilities/+`` is
retained, so a document can arrive before a consumer attaches;
``subscribe_capabilities`` therefore replays every stored document to the new
subscriber immediately.

``SUBSCRIPTIONS`` is the single table ``Machine.set_mqtt_client`` loops
over to register every inbound handler (telemetry-only and sale-driving
alike) with the MQTT client.
"""

from __future__ import annotations

import re
from collections.abc import Callable

from loguru import logger
from pydantic import ValidationError

from ice_common.observers import Subscribers
from ice_contracts.board.common import CHANNEL_ID_PATTERN, MAX_SUBSYSTEMS
from ice_contracts.board.ice_maker_monitor import ChannelReading, CommandAck
from ice_contracts.board.mqtt_messages import (
    HardwareIO,
    IceMakerEvent,
    PaymentStatus,
)
from ice_contracts.board.vending_machine import SubsystemCapabilities

_NAME_RE = re.compile(CHANNEL_ID_PATTERN)
_MAX_REPORTED_BAD_NAMES = 100


class TelemetryRouter:
    """Validates telemetry messages and fans them out. See the module docstring."""

    def __init__(self) -> None:
        self.capabilities: dict[str, dict] = {}
        # Distinct rejected names already warned about (bounded).
        self._reported_bad_names: set[str] = set()
        self._hardware_io: Subscribers[[str, bool]] = Subscribers()
        self._payment_status: Subscribers[[str, str]] = Subscribers()
        self._sensor: Subscribers[[str, float]] = Subscribers()
        self._heartbeat: Subscribers[[str, dict, bool]] = Subscribers()
        self._ice_maker_event: Subscribers[[IceMakerEvent]] = Subscribers()
        self._telemetry: Subscribers[[str, float]] = Subscribers()
        self._capabilities: Subscribers[[str, dict, SubsystemCapabilities | None]] = (
            Subscribers()
        )

    # --- subscriptions (each returns an unsubscribe callable) ---

    def subscribe_hardware_io(
        self, cb: Callable[[str, bool], None]
    ) -> Callable[[], None]:
        return self._hardware_io.subscribe(cb)

    def subscribe_payment_status(
        self, cb: Callable[[str, str], None]
    ) -> Callable[[], None]:
        return self._payment_status.subscribe(cb)

    def subscribe_sensor(self, cb: Callable[[str, float], None]) -> Callable[[], None]:
        return self._sensor.subscribe(cb)

    def subscribe_heartbeat(
        self, cb: Callable[[str, dict, bool], None]
    ) -> Callable[[], None]:
        return self._heartbeat.subscribe(cb)

    def subscribe_ice_maker_event(
        self, cb: Callable[[IceMakerEvent], None]
    ) -> Callable[[], None]:
        return self._ice_maker_event.subscribe(cb)

    def subscribe_telemetry(
        self, cb: Callable[[str, float], None]
    ) -> Callable[[], None]:
        return self._telemetry.subscribe(cb)

    def subscribe_capabilities(
        self, cb: Callable[[str, dict, SubsystemCapabilities | None], None]
    ) -> Callable[[], None]:
        """Subscribe, then replay every stored document to ``cb`` alone."""
        unsubscribe = self._capabilities.subscribe(cb)
        for subsystem, data in list(self.capabilities.items()):
            try:
                caps = SubsystemCapabilities.model_validate(data)
            except ValidationError:
                caps = None
            try:
                cb(subsystem, data, caps)
            except Exception:
                logger.exception(f"Capabilities replay to {cb!r} raised")
        return unsubscribe

    # --- name validation ---

    def _name_ok(self, name: object, what: str) -> bool:
        """True when ``name`` is a slug (CHANNEL_ID_PATTERN, <= 64 chars).

        Anything else is dropped; one warning per distinct bad name, with the
        set of already-reported names capped so a flood cannot grow it.
        """
        if isinstance(name, str) and _NAME_RE.fullmatch(name):
            return True
        key = repr(name)[:80]
        if (
            key not in self._reported_bad_names
            and len(self._reported_bad_names) < _MAX_REPORTED_BAD_NAMES
        ):
            self._reported_bad_names.add(key)
            logger.warning(f"MQTT: dropping message with invalid {what} {key}")
        return False

    # --- handlers ---

    async def handle_hardware_io(self, topic: str, data: dict) -> None:
        """Binary hardware IO from the vending ESP32."""
        hw = HardwareIO.model_validate(data)
        if not self._name_ok(hw.device, "hardware device"):
            return
        logger.debug(f"MQTT hardware IO: {hw.device}={hw.state}")
        self._hardware_io.publish(hw.device, hw.state)

    async def handle_payment_status(self, topic: str, data: dict) -> None:
        """MDB device readiness; any device in error/offline blocks payment."""
        status = PaymentStatus.model_validate(data)
        if not self._name_ok(status.device, "payment device"):
            return
        logger.debug(f"MQTT payment status: {status.device}={status.state}")
        self._payment_status.publish(status.device, status.state)

    async def handle_sensor(self, topic: str, data: dict) -> None:
        """Handle temperature/sensor reading from ESP32."""
        logger.debug(f"MQTT sensor [{topic}]: {data}")
        location = data.get("location", topic.split("/")[-1] if "/" in topic else topic)
        if not self._name_ok(location, "sensor location"):
            return
        value = data.get("value")
        if value is None:
            return
        try:
            reading = float(value)
        except (TypeError, ValueError):
            logger.warning(f"MQTT sensor [{topic}]: non-numeric value {value!r}")
            return
        self._sensor.publish(location, reading)

    async def handle_heartbeat(self, topic: str, data: dict) -> None:
        """Handle heartbeat from ESP32 subsystem (uptime -1 is the MQTT last will)."""
        logger.debug(f"MQTT heartbeat [{topic}]: {data}")
        subsystem = data.get(
            "subsystem", topic.split("/")[-1] if "/" in topic else topic
        )
        if not self._name_ok(subsystem, "heartbeat subsystem"):
            return
        self._heartbeat.publish(subsystem, data, data.get("uptime_seconds") != -1)

    async def handle_ice_maker_event(self, topic: str, data: dict) -> None:
        """Handle operational events from the ice maker ESP32."""
        event = IceMakerEvent.model_validate(data)
        logger.info(f"MQTT ice maker event: {event.event} — {event.detail or ''}")
        self._ice_maker_event.publish(event)

    async def handle_capabilities(self, topic: str, data: dict) -> None:
        """Store a subsystem's retained self-description and publish it."""
        subsystem = data.get("subsystem") or topic.split("/")[-1]
        if not self._name_ok(subsystem, "capabilities subsystem"):
            return
        if (
            subsystem not in self.capabilities
            and len(self.capabilities) >= MAX_SUBSYSTEMS
        ):
            logger.warning(
                f"MQTT: capabilities cap ({MAX_SUBSYSTEMS}) reached; "
                f"dropping new subsystem {subsystem!r}"
            )
            return
        caps: SubsystemCapabilities | None
        try:
            caps = SubsystemCapabilities.model_validate(data)
            logger.info(
                f"Capabilities registered for '{subsystem}' "
                f"(firmware {caps.firmware}, contract {caps.contract_version}, "
                f"{len(caps.channels)} channels)"
            )
        except ValidationError:
            caps = None
            logger.warning(
                f"Capabilities for '{subsystem}' don't match the known schema; "
                "storing raw payload"
            )
        self.capabilities[subsystem] = data
        self._capabilities.publish(subsystem, data, caps)

    async def handle_telemetry(self, topic: str, data: dict) -> None:
        """Route a generic telemetry channel reading to subscribers."""
        reading = ChannelReading.model_validate(data)
        if not self._name_ok(reading.channel_id, "telemetry channel"):
            return
        self._telemetry.publish(reading.channel_id, reading.value)

    async def handle_command_ack(self, topic: str, data: dict) -> None:
        """Log command acknowledgements from the monitor."""
        ack = CommandAck.model_validate(data)
        detail = f" — {ack.detail}" if ack.detail else ""
        logger.info(
            f"Monitor ack: {ack.command} -> {ack.status}{detail} ({ack.request_id})"
        )


#: (topic_pattern, owner, method_name) triples. `owner` is `"vmc"` for the four
#: sale-driving handlers that stay on the VMC (`on_payment_credit`,
#: `on_button_press`, `on_dispenser_event`, `on_refund_ack`) and `"telemetry"`
#: for the rest, whose `method_name` is the `TelemetryRouter`'s own `handle_*`
#: name. `Machine.set_mqtt_client` (ice_edge/app/machine.py) is the sole loop
#: over this table, resolving each triple against the VMC or its
#: `TelemetryRouter` depending on `owner`.
SUBSCRIPTIONS: tuple[tuple[str, str, str], ...] = (
    ("payment/credit", "vmc", "on_payment_credit"),
    ("hardware/buttons", "vmc", "on_button_press"),
    ("hardware/dispenser", "vmc", "on_dispenser_event"),
    ("sensors/temp/+", "telemetry", "handle_sensor"),
    ("heartbeat/+", "telemetry", "handle_heartbeat"),
    ("ice_maker/event", "telemetry", "handle_ice_maker_event"),
    ("capabilities/+", "telemetry", "handle_capabilities"),
    ("telemetry/ice_maker/+", "telemetry", "handle_telemetry"),
    ("cmd/ice_maker/ack", "telemetry", "handle_command_ack"),
    ("hardware/io/+", "telemetry", "handle_hardware_io"),
    ("cmd/payment/refund/ack", "vmc", "on_refund_ack"),
    ("payment/status", "telemetry", "handle_payment_status"),
)
