"""Health-monitor consumer of the telemetry router's fan-out.

``HealthTelemetry`` holds the payload-to-``HealthMonitor`` mapping that used
to be inline in ``TelemetryRouter``: which signal name, ``text=`` and method
each validated message feeds. The health monitor is read through a callable
at call time (it is attached later and may be ``None`` in tests), so with
none attached every method is a no-op.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import TYPE_CHECKING

from loguru import logger

from ice_contracts.board.mqtt_messages import IceMakerEvent
from ice_contracts.board.vending_machine import SubsystemCapabilities

if TYPE_CHECKING:
    from ice_edge.monitoring.health_monitor import HealthMonitor

# Events logged to the ice maker log: power cycles, ice drops, out-of-spec.
ICE_LOG_EVENTS = {
    "power_on",
    "power_off",
    "ice_dropped",
    "needs_cleaning",
    "failed_cycle",
    "temp_out_of_bounds",
}

# loguru's bind() only attaches the `ice_maker` extra field; sinks are
# resolved at log time, so binding at import is safe.
ice_log = logger.bind(ice_maker=True)


class HealthTelemetry:
    def __init__(self, health: Callable[[], HealthMonitor | None]) -> None:
        self._health = health

    def on_hardware_io(self, device: str, state: bool) -> None:
        health = self._health()
        if health:
            health.record_signal("vending", device, 1.0 if state else 0.0)

    def on_payment_status(self, device: str, state: str) -> None:
        health = self._health()
        if health:
            health.record_signal(
                "mdb", device, 1.0 if state == "ready" else 0.0, text=state
            )

    def on_sensor(self, location: str, value: float) -> None:
        health = self._health()
        if health:
            health.record_temperature(location, value)

    def on_heartbeat(self, subsystem: str, data: dict, alive: bool) -> None:
        health = self._health()
        if not health:
            return
        if not alive:
            logger.warning(f"Subsystem '{subsystem}' reported OFFLINE (MQTT last will)")
            health.mark_offline(subsystem)
            return
        health.record_heartbeat(subsystem, data)

    def on_ice_maker_event(self, event: IceMakerEvent) -> None:
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

    def on_telemetry(self, channel_id: str, value: float) -> None:
        health = self._health()
        if health:
            health.record_channel(channel_id, value)

    def on_capabilities(
        self, subsystem: str, data: dict, caps: SubsystemCapabilities | None
    ) -> None:
        health = self._health()
        if health:
            health.record_capabilities(subsystem, data)
