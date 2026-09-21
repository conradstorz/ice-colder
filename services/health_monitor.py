# services/health_monitor.py
"""
Periodic health monitor for VMC subsystems.

Tracks last-seen timestamps for ESP32 subsystems and MQTT connection.
Fires alerts when subsystems go silent, temperatures drift out of range,
or the FSM enters an error state.
"""

import asyncio
import platform
import time
from dataclasses import dataclass, field
from typing import Optional, Callable, Awaitable

from loguru import logger

from services.build_info import BUILD_INFO


@dataclass
class SubsystemStatus:
    """Tracks liveness of a single subsystem (e.g., an ESP32)."""

    name: str
    last_seen: float = 0.0  # monotonic timestamp
    last_payload: dict = field(default_factory=dict)
    capabilities: dict = field(default_factory=dict)
    capabilities_at: float = 0.0  # monotonic; 0.0 = never received
    offline: bool = False  # set by a Last-Will; cleared by the next heartbeat
    liveness_reported: Optional[bool] = None  # last value sent to the liveness callback

    @property
    def seconds_since_seen(self) -> float:
        if self.last_seen == 0.0:
            return float("inf")
        return time.monotonic() - self.last_seen

    @property
    def alive(self) -> bool:
        return self.last_seen > 0.0

    def is_stale(self, timeout: float) -> bool:
        """Went quiet after being alive, or announced offline. A subsystem known
        only from a retained capabilities document is 'never seen', not stale."""
        return self.offline or (self.alive and self.seconds_since_seen > timeout)


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
    code: Optional[str] = None  # FaultCode value, when the alert is a fault
    product_sku: Optional[str] = None


# Type for the callback that delivers alerts (e.g., to notifier service)
AlertCallback = Callable[[Alert], Awaitable[None]]

# Type for the callback that reports subsystem liveness transitions
LivenessCallback = Callable[[str, bool], None]


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
        machine_id: str | None = None,
        started_at: float | None = None,
    ):
        self._check_interval = check_interval
        self._subsystem_timeout = subsystem_timeout
        self._temp_min = temp_min
        self._temp_max = temp_max
        self._machine_id = machine_id
        self._started_at = time.monotonic() if started_at is None else started_at

        self._subsystems: dict[str, SubsystemStatus] = {}
        self._temperatures: dict[str, TemperatureReading] = {}
        self._channels: dict[str, TemperatureReading] = {}
        self._mqtt_connected: bool = False
        self._vmc_state: str = "unknown"

        self._alert_callback: Optional[AlertCallback] = None
        self._liveness_callback: Optional[LivenessCallback] = None
        # Track which alerts have already fired to avoid spamming
        self._fired_alerts: set[str] = set()
        # Active faults pushed by the VMC: key -> fault dict (+ "since" monotonic)
        self._active_faults: dict[str, dict] = {}

    def set_alert_callback(self, callback: AlertCallback):
        """Register a coroutine to be called when an alert fires."""
        self._alert_callback = callback

    def set_liveness_callback(self, callback: LivenessCallback):
        """Register a sync callback(subsystem, alive) fired once per transition:
        first heartbeat / recovery -> True, Last-Will / first staleness -> False."""
        self._liveness_callback = callback

    def _notify_liveness(self, sub: SubsystemStatus, alive: bool) -> None:
        if sub.liveness_reported == alive:
            return
        sub.liveness_reported = alive
        if self._liveness_callback is None:
            return
        try:
            self._liveness_callback(sub.name, alive)
        except Exception as e:
            logger.error(f"Health: liveness callback failed for {sub.name}: {e}")

    # --- Data recording (called from MQTT handlers) ---

    def record_heartbeat(self, subsystem: str, payload: dict | None = None):
        """Record that a subsystem has checked in."""
        if subsystem not in self._subsystems:
            self._subsystems[subsystem] = SubsystemStatus(name=subsystem)
            logger.info(f"Health: New subsystem registered: {subsystem}")
        self._subsystems[subsystem].last_seen = time.monotonic()
        self._subsystems[subsystem].last_payload = payload or {}
        self._subsystems[subsystem].offline = False
        # Clear stale alert for this subsystem
        self._fired_alerts.discard(f"subsystem_stale:{subsystem}")
        self._notify_liveness(self._subsystems[subsystem], True)

    def record_capabilities(self, subsystem: str, caps: dict):
        """Store a subsystem's retained self-description. Never touches
        last_seen: a retained document says what the board is, not that it
        is up."""
        if subsystem not in self._subsystems:
            self._subsystems[subsystem] = SubsystemStatus(name=subsystem)
            logger.info(f"Health: New subsystem registered (capabilities): {subsystem}")
        status = self._subsystems[subsystem]
        status.capabilities = dict(caps)
        status.capabilities_at = time.monotonic()

    @staticmethod
    def empty_subsystem_row() -> dict:
        """The dashboard row for a subsystem that has never been heard from."""
        return {
            "alive": False,
            "seconds_since_seen": float("inf"),
            "stale": False,
            "uptime_seconds": None,
            "firmware": None,
            "contract_version": None,
            "brand": None,
            "model": None,
            "hardware_id": None,
            "ip": None,
            "channel_count": 0,
            "commands": [],
            "capabilities_age_seconds": None,
        }

    def record_temperature(self, location: str, value: float):
        """Record a temperature sensor reading."""
        self._temperatures[location] = TemperatureReading(
            location=location, value=value, timestamp=time.monotonic()
        )
        # Clear out-of-range alert if back in range
        if self._temp_min <= value <= self._temp_max:
            self._fired_alerts.discard(f"temp_range:{location}")

    def record_channel(self, channel_id: str, value: float):
        """Record a generic telemetry channel reading (analog or binary)."""
        self._channels[channel_id] = TemperatureReading(
            location=channel_id, value=value, timestamp=time.monotonic()
        )

    def mark_offline(self, subsystem: str):
        """Force a subsystem to stale/offline (e.g., MQTT Last-Will received).

        If the subsystem was never tracked before (e.g. it died before ever
        sending a live heartbeat after a VMC restart), start tracking it as
        stale so it shows up in the dashboard and the stale alert can fire.
        """
        if subsystem not in self._subsystems:
            self._subsystems[subsystem] = SubsystemStatus(name=subsystem)
        self._subsystems[subsystem].last_seen = 0.0
        self._subsystems[subsystem].offline = True
        self._notify_liveness(self._subsystems[subsystem], False)

    def set_active_faults(self, faults: list[dict]):
        """Replace the active-fault snapshot; `since` survives for keys already present."""
        now = time.monotonic()
        new: dict[str, dict] = {}
        for f in faults:
            key = f["key"]
            since = self._active_faults.get(key, {}).get("since", now)
            new[key] = {**f, "since": since}
        self._active_faults = new

    async def raise_alert(
        self,
        key: str,
        level: str,
        source: str,
        message: str,
        code: str | None = None,
        product_sku: str | None = None,
    ):
        """Public entry for VMC-raised faults; dedups on `key` like periodic checks."""
        await self._fire_alert(
            key, level, source, message, code=code, product_sku=product_sku
        )

    def clear_alert(self, key: str):
        """Forget a dedup key so the next raise_alert with it fires again."""
        self._fired_alerts.discard(key)

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
        now = time.monotonic()

        subsystems = {}
        for name, sub in self._subsystems.items():
            row = self.empty_subsystem_row()
            uptime = sub.last_payload.get("uptime_seconds")
            caps = sub.capabilities
            channels = caps.get("channels")
            commands = caps.get("commands")
            row.update(
                {
                    "alive": sub.alive,
                    "seconds_since_seen": round(sub.seconds_since_seen, 1),
                    "stale": sub.is_stale(self._subsystem_timeout),
                    "uptime_seconds": (
                        int(uptime)
                        if sub.alive
                        and isinstance(uptime, (int, float))
                        and uptime >= 0
                        else None
                    ),
                    "firmware": caps.get("firmware"),
                    "contract_version": caps.get("contract_version"),
                    "brand": caps.get("brand") or None,
                    "model": caps.get("model") or None,
                    "hardware_id": caps.get("hardware_id"),
                    "ip": caps.get("ip"),
                    # Raw (schema-failed) payloads are stored too; never trust shapes.
                    "channel_count": len(channels) if isinstance(channels, list) else 0,
                    "commands": list(commands) if isinstance(commands, list) else [],
                    "capabilities_age_seconds": (
                        round(now - sub.capabilities_at, 1)
                        if sub.capabilities_at
                        else None
                    ),
                }
            )
            subsystems[name] = row

        temperatures = {}
        for loc, reading in self._temperatures.items():
            in_range = self._temp_min <= reading.value <= self._temp_max
            temperatures[loc] = {
                "value": reading.value,
                "in_range": in_range,
                "age_seconds": round(time.monotonic() - reading.timestamp, 1),
            }

        channels = {}
        for channel_id, reading in self._channels.items():
            channels[channel_id] = {
                "value": reading.value,
                "age_seconds": round(time.monotonic() - reading.timestamp, 1),
            }

        active_faults = [
            {
                **{k: v for k, v in f.items() if k != "since"},
                "since_seconds": round(now - f["since"], 1),
            }
            for f in self._active_faults.values()
        ]

        return {
            "mqtt_connected": self._mqtt_connected,
            "vmc_state": self._vmc_state,
            "subsystems": subsystems,
            "temperatures": temperatures,
            "channels": channels,
            "check_interval": self._check_interval,
            "subsystem_timeout": self._subsystem_timeout,
            "active_faults": active_faults,
            "vmc": {
                "commit": BUILD_INFO.commit,
                "commit_short": BUILD_INFO.commit_short,
                "build_time": BUILD_INFO.build_time,
                "source": BUILD_INFO.source,
                "uptime_seconds": int(now - self._started_at),
                "python_version": platform.python_version(),
                "machine_id": self._machine_id,
            },
        }

    # --- Main loop ---

    async def run(self):
        """Run periodic health checks forever. A failing check is logged, never fatal."""
        logger.info(
            f"Health monitor started: interval={self._check_interval}s, "
            f"timeout={self._subsystem_timeout}s"
        )
        while True:
            try:
                await self._check()
            except Exception:
                logger.exception("Health check round failed")
            await asyncio.sleep(self._check_interval)

    async def _check(self):
        """Run one round of health checks."""
        # Check MQTT connection
        if not self._mqtt_connected:
            await self._fire_alert(
                "mqtt_disconnect", "warning", "mqtt", "MQTT broker connection is down"
            )

        # Check VMC error state
        if self._vmc_state == "error":
            await self._fire_alert("vmc_error", "error", "vmc", "VMC is in error state")

        # Check subsystem liveness
        for name, sub in self._subsystems.items():
            if sub.is_stale(self._subsystem_timeout):
                self._notify_liveness(sub, False)
                await self._fire_alert(
                    f"subsystem_stale:{name}",
                    "warning",
                    name,
                    f"Subsystem '{name}' has not reported in "
                    f"{sub.seconds_since_seen:.0f}s (timeout: {self._subsystem_timeout}s)",
                )

        # Check temperature ranges
        for loc, reading in self._temperatures.items():
            if not (self._temp_min <= reading.value <= self._temp_max):
                await self._fire_alert(
                    f"temp_range:{loc}",
                    "critical",
                    f"temp/{loc}",
                    f"Temperature at '{loc}' is {reading.value:.1f}C "
                    f"(range: {self._temp_min} to {self._temp_max})",
                )

    async def _fire_alert(
        self,
        key: str,
        level: str,
        source: str,
        message: str,
        code: str | None = None,
        product_sku: str | None = None,
    ):
        """Fire an alert if it hasn't already been fired (deduplication)."""
        if key in self._fired_alerts:
            return
        self._fired_alerts.add(key)

        alert = Alert(
            level=level,
            source=source,
            message=message,
            code=code,
            product_sku=product_sku,
        )
        logger.warning(f"Health alert [{level}] {source}: {message}")

        if self._alert_callback:
            try:
                await self._alert_callback(alert)
            except Exception as e:
                logger.error(f"Health: Alert callback failed: {e}")
