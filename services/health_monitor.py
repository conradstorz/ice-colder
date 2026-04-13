# services/health_monitor.py
"""
Periodic health monitor for VMC subsystems.

Tracks last-seen timestamps for ESP32 subsystems and MQTT connection.
Fires alerts when subsystems go silent, temperatures drift out of range,
or the FSM enters an error state.
"""
import asyncio
import time
from dataclasses import dataclass, field
from typing import Optional, Callable, Awaitable

from loguru import logger


@dataclass
class SubsystemStatus:
    """Tracks liveness of a single subsystem (e.g., an ESP32)."""
    name: str
    last_seen: float = 0.0  # monotonic timestamp
    last_payload: dict = field(default_factory=dict)

    @property
    def seconds_since_seen(self) -> float:
        if self.last_seen == 0.0:
            return float("inf")
        return time.monotonic() - self.last_seen

    @property
    def alive(self) -> bool:
        return self.last_seen > 0.0


@dataclass
class TemperatureReading:
    """Latest temperature reading from a sensor location."""
    location: str
    value: float
    timestamp: float  # monotonic


@dataclass
class Alert:
    """A health alert ready to be sent to the owner."""
    level: str  # "info", "warning", "error", "critical"
    source: str
    message: str
    timestamp: float = field(default_factory=time.monotonic)


# Type for the callback that delivers alerts (e.g., to notifier service)
AlertCallback = Callable[[Alert], Awaitable[None]]


class HealthMonitor:
    """
    Async health monitor that runs as a long-lived task.

    Usage:
        monitor = HealthMonitor(check_interval=30, subsystem_timeout=120)
        monitor.set_alert_callback(my_alert_handler)
        # Call record_heartbeat / record_temperature from MQTT handlers
        await monitor.run()  # blocks, runs periodic checks
    """

    def __init__(
        self,
        check_interval: float = 30.0,
        subsystem_timeout: float = 120.0,
        temp_min: float = -20.0,
        temp_max: float = 80.0,
    ):
        self._check_interval = check_interval
        self._subsystem_timeout = subsystem_timeout
        self._temp_min = temp_min
        self._temp_max = temp_max

        self._subsystems: dict[str, SubsystemStatus] = {}
        self._temperatures: dict[str, TemperatureReading] = {}
        self._mqtt_connected: bool = False
        self._vmc_state: str = "unknown"

        self._alert_callback: Optional[AlertCallback] = None
        # Track which alerts have already fired to avoid spamming
        self._fired_alerts: set[str] = set()

    def set_alert_callback(self, callback: AlertCallback):
        """Register a coroutine to be called when an alert fires."""
        self._alert_callback = callback

    # --- Data recording (called from MQTT handlers) ---

    def record_heartbeat(self, subsystem: str, payload: dict | None = None):
        """Record that a subsystem has checked in."""
        if subsystem not in self._subsystems:
            self._subsystems[subsystem] = SubsystemStatus(name=subsystem)
            logger.info(f"Health: New subsystem registered: {subsystem}")
        self._subsystems[subsystem].last_seen = time.monotonic()
        self._subsystems[subsystem].last_payload = payload or {}
        # Clear stale alert for this subsystem
        self._fired_alerts.discard(f"subsystem_stale:{subsystem}")

    def record_temperature(self, location: str, value: float):
        """Record a temperature sensor reading."""
        self._temperatures[location] = TemperatureReading(
            location=location, value=value, timestamp=time.monotonic()
        )
        # Clear out-of-range alert if back in range
        if self._temp_min <= value <= self._temp_max:
            self._fired_alerts.discard(f"temp_range:{location}")

    def update_mqtt_status(self, connected: bool):
        """Update MQTT connection status."""
        was_connected = self._mqtt_connected
        self._mqtt_connected = connected
        if was_connected and not connected:
            self._fired_alerts.discard("mqtt_disconnect")
        elif not was_connected and connected:
            self._fired_alerts.discard("mqtt_disconnect")

    def update_vmc_state(self, state: str):
        """Update the current FSM state for monitoring."""
        prev = self._vmc_state
        self._vmc_state = state
        if prev == "error" and state != "error":
            self._fired_alerts.discard("vmc_error")

    # --- Health summary (for dashboard) ---

    def get_summary(self) -> dict:
        """Return a snapshot of all health data for the dashboard."""
        subsystems = {}
        for name, sub in self._subsystems.items():
            subsystems[name] = {
                "alive": sub.alive,
                "seconds_since_seen": round(sub.seconds_since_seen, 1),
                "stale": sub.seconds_since_seen > self._subsystem_timeout,
            }

        temperatures = {}
        for loc, reading in self._temperatures.items():
            in_range = self._temp_min <= reading.value <= self._temp_max
            temperatures[loc] = {
                "value": reading.value,
                "in_range": in_range,
                "age_seconds": round(time.monotonic() - reading.timestamp, 1),
            }

        return {
            "mqtt_connected": self._mqtt_connected,
            "vmc_state": self._vmc_state,
            "subsystems": subsystems,
            "temperatures": temperatures,
            "check_interval": self._check_interval,
            "subsystem_timeout": self._subsystem_timeout,
        }

    # --- Main loop ---

    async def run(self):
        """Run periodic health checks forever."""
        logger.info(
            f"Health monitor started: interval={self._check_interval}s, "
            f"timeout={self._subsystem_timeout}s"
        )
        while True:
            await self._check()
            await asyncio.sleep(self._check_interval)

    async def _check(self):
        """Run one round of health checks."""
        # Check MQTT connection
        if not self._mqtt_connected:
            await self._fire_alert(
                "mqtt_disconnect", "warning", "mqtt",
                "MQTT broker connection is down"
            )

        # Check VMC error state
        if self._vmc_state == "error":
            await self._fire_alert(
                "vmc_error", "error", "vmc",
                "VMC is in error state"
            )

        # Check subsystem liveness
        for name, sub in self._subsystems.items():
            if sub.seconds_since_seen > self._subsystem_timeout:
                await self._fire_alert(
                    f"subsystem_stale:{name}", "warning", name,
                    f"Subsystem '{name}' has not reported in "
                    f"{sub.seconds_since_seen:.0f}s (timeout: {self._subsystem_timeout}s)"
                )

        # Check temperature ranges
        for loc, reading in self._temperatures.items():
            if not (self._temp_min <= reading.value <= self._temp_max):
                await self._fire_alert(
                    f"temp_range:{loc}", "critical", f"temp/{loc}",
                    f"Temperature at '{loc}' is {reading.value:.1f}C "
                    f"(range: {self._temp_min} to {self._temp_max})"
                )

    async def _fire_alert(self, key: str, level: str, source: str, message: str):
        """Fire an alert if it hasn't already been fired (deduplication)."""
        if key in self._fired_alerts:
            return
        self._fired_alerts.add(key)

        alert = Alert(level=level, source=source, message=message)
        logger.warning(f"Health alert [{level}] {source}: {message}")

        if self._alert_callback:
            try:
                await self._alert_callback(alert)
            except Exception as e:
                logger.error(f"Health: Alert callback failed: {e}")
