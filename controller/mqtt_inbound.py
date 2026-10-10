"""Telemetry-only MQTT inbound handlers: the fifth piece carved off the VMC
god object, following the same pattern as ``controller/fault_registry.py``'s
``FaultRegistry``, ``controller/escrow_ledger.py``'s ``EscrowLedger``,
``controller/refund_protocol.py``'s ``RefundProtocol`` and
``controller/session_recovery.py``'s ``SessionRecovery`` (see ``CLAUDE.md``'s
"FSM Core" section).

``TelemetryRouter`` holds every inbound MQTT handler that only reads/
validates its payload and forwards it to the health monitor, availability,
or a log sink -- it never touches the FSM, escrow, or a sale. The two
handlers that *do* have a side effect reaching back into FSM/fault state
(the ICE-101 auto-clear inside hardware IO, and the dispenser-profiles
reconcile inside capabilities) keep that side effect on the VMC via
injected callbacks (``on_bin_half_full``/``on_capabilities_validated``)
invoked at exactly the point the old inline code did.

``health``/``availability`` are callables read at call time, not
snapshotted at construction: both are attached to the VMC later via
``set_health_monitor``/``set_availability`` and may be ``None`` in tests.
``capabilities`` is the VMC's own ``subsystem_capabilities`` dict object
(not a copy), so the router's writes land exactly where existing tests
already read them.

``SUBSCRIPTIONS`` is the single table ``VMC.set_mqtt_client`` loops over
to register every inbound handler (telemetry-only and sale-driving alike)
with the MQTT client, in the same order the thirteen individual
``client.register(...)`` calls used to run in.
"""

from __future__ import annotations

from collections.abc import Callable

from loguru import logger
from pydantic import ValidationError

from contracts.ice_maker_monitor import ChannelReading, CommandAck
from contracts.vending_machine import SubsystemCapabilities
from services.availability import Availability
from services.health_monitor import HealthMonitor
from services.mqtt_messages import (
    HardwareIO,
    IceMakerEvent,
    PaymentStatus,
    SensorReading,
)

# Events logged to the ice maker log: power cycles, ice drops, out-of-spec.
ICE_LOG_EVENTS = {
    "power_on",
    "power_off",
    "ice_dropped",
    "needs_cleaning",
    "failed_cycle",
    "temp_out_of_bounds",
}

# Bound logger for ice-maker events. loguru's bind() only attaches the
# `ice_maker` extra field; sinks are resolved at log time, so binding at
# import is safe even though setup_logging() runs later in main().
ice_log = logger.bind(ice_maker=True)


class TelemetryRouter:
    """Holds the telemetry-only MQTT inbound handlers the VMC used to keep
    directly on itself. See the module docstring above.
    """

    def __init__(
        self,
        *,
        health: Callable[[], HealthMonitor | None],
        availability: Callable[[], Availability | None],
        capabilities: dict[str, dict],
        on_bin_half_full: Callable[[], None],
        on_capabilities_validated: Callable[[str, SubsystemCapabilities], None],
    ) -> None:
        self._health = health
        self._availability = availability
        self._capabilities = capabilities
        self._on_bin_half_full = on_bin_half_full
        self._on_capabilities_validated = on_capabilities_validated

    async def handle_hardware_io(self, topic: str, data: dict) -> None:
        """Binary hardware IO from the vending ESP32; ice returning clears
        ICE-101 via the injected callback (the VMC owns the fault registry)."""
        hw = HardwareIO.model_validate(data)
        availability = self._availability()
        if availability:
            availability.set_hardware_io(hw.device, hw.state)
        health = self._health()
        if health:
            health.record_signal("vending", hw.device, 1.0 if hw.state else 0.0)
        if hw.device == "bin_half_full" and hw.state:
            self._on_bin_half_full()
        else:
            logger.debug(f"MQTT hardware IO: {hw.device}={hw.state}")

    async def handle_payment_status(self, topic: str, data: dict) -> None:
        """MDB device readiness; any device in error/offline blocks payment."""
        status = PaymentStatus.model_validate(data)
        logger.debug(f"MQTT payment status: {status.device}={status.state}")
        availability = self._availability()
        if availability:
            availability.set_payment_device(status.device, status.state)
        health = self._health()
        if health:
            health.record_signal(
                "mdb",
                status.device,
                1.0 if status.state == "ready" else 0.0,
                text=status.state,
            )

    async def handle_sensor(self, topic: str, data: dict) -> None:
        """Handle temperature/sensor reading from ESP32."""
        logger.debug(f"MQTT sensor [{topic}]: {data}")
        health = self._health()
        if health:
            location = data.get(
                "location", topic.split("/")[-1] if "/" in topic else topic
            )
            value = data.get("value")
            if value is not None:
                health.record_temperature(location, float(value))

    async def handle_water_flow(self, topic: str, data: dict) -> None:
        """Handle water flow sensor readings from the vending ESP32."""
        reading = SensorReading.model_validate(data)
        logger.debug(f"MQTT water flow [{topic}]: {reading.value}{reading.unit}")
        health = self._health()
        if health:
            health.record_channel("water_flow", reading.value)

    async def handle_heartbeat(self, topic: str, data: dict) -> None:
        """Handle heartbeat from ESP32 subsystem."""
        logger.debug(f"MQTT heartbeat [{topic}]: {data}")
        health = self._health()
        if health:
            subsystem = data.get(
                "subsystem", topic.split("/")[-1] if "/" in topic else topic
            )
            if data.get("uptime_seconds") == -1:
                logger.warning(
                    f"Subsystem '{subsystem}' reported OFFLINE (MQTT last will)"
                )
                health.mark_offline(subsystem)
                return
            health.record_heartbeat(subsystem, data)

    async def handle_ice_maker_event(self, topic: str, data: dict) -> None:
        """Handle operational events from the ice maker ESP32."""
        event = IceMakerEvent.model_validate(data)
        logger.info(f"MQTT ice maker event: {event.event} — {event.detail or ''}")
        if event.event in ICE_LOG_EVENTS:
            detail = f" ({event.detail})" if event.detail else ""
            ice_log.info(f"{event.event.upper()}{detail}")
        health = self._health()
        if health and event.event in ("power_on", "power_off"):
            health.record_signal(
                "ice_maker",
                "compressor_run",
                1.0 if event.event == "power_on" else 0.0,
            )

    async def handle_capabilities(self, topic: str, data: dict) -> None:
        """Store a subsystem's retained self-description and hand it to health."""
        subsystem = data.get("subsystem") or topic.split("/")[-1]
        try:
            caps = SubsystemCapabilities.model_validate(data)
            logger.info(
                f"Capabilities registered for '{subsystem}' "
                f"(firmware {caps.firmware}, contract {caps.contract_version}, "
                f"{len(caps.channels)} channels)"
            )
            # Only on a successfully validated doc: `caps` is never bound
            # in the except branch below, and a malformed doc must never
            # overwrite previously-good capabilities with something that
            # would wrongly downgrade real slot errors back to warnings.
            self._on_capabilities_validated(subsystem, caps)
        except ValidationError:
            logger.warning(
                f"Capabilities for '{subsystem}' don't match the known schema; "
                "storing raw payload"
            )
        self._capabilities[subsystem] = data
        health = self._health()
        if health:
            health.record_capabilities(subsystem, data)

    async def handle_telemetry(self, topic: str, data: dict) -> None:
        """Route a generic telemetry channel reading into health tracking."""
        reading = ChannelReading.model_validate(data)
        health = self._health()
        if health:
            health.record_channel(reading.channel_id, reading.value)

    async def handle_command_ack(self, topic: str, data: dict) -> None:
        """Log command acknowledgements from the monitor."""
        ack = CommandAck.model_validate(data)
        detail = f" — {ack.detail}" if ack.detail else ""
        logger.info(
            f"Monitor ack: {ack.command} -> {ack.status}{detail} ({ack.request_id})"
        )


#: (topic_pattern, VMC method name) pairs, in the exact order the thirteen
#: individual `client.register(...)` calls in `VMC.set_mqtt_client` used to
#: run in. `VMC.set_mqtt_client` loops over this table instead.
SUBSCRIPTIONS: tuple[tuple[str, str], ...] = (
    ("payment/credit", "on_payment_credit"),
    ("hardware/buttons", "on_button_press"),
    ("hardware/dispenser", "on_dispenser_event"),
    ("sensors/temp/+", "on_sensor_reading"),
    ("heartbeat/+", "on_heartbeat"),
    ("ice_maker/event", "on_ice_maker_event"),
    ("capabilities/+", "on_capabilities"),
    ("telemetry/ice_maker/+", "on_telemetry"),
    ("cmd/ice_maker/ack", "on_command_ack"),
    ("hardware/io/+", "on_hardware_io"),
    ("cmd/payment/refund/ack", "on_refund_ack"),
    ("payment/status", "on_payment_status"),
    ("sensors/water_flow", "on_water_flow"),
)
