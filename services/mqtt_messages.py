# services/mqtt_messages.py
"""
Pydantic models for all MQTT message payloads.

Messages flow in three directions:
  ESP32 → RPi:  sensor readings, payment events, hardware status
  RPi → ESP32:  commands (dispense, enable payment, display mode)
  RPi → World:  VMC status and alerts (consumed by HA, owner dashboard, etc.)
"""
from datetime import datetime, timezone
from enum import Enum
from typing import Optional
from pydantic import BaseModel, Field


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


# ──────────────────────────────────────────────
# ESP32 → RPi: Inbound messages
# ──────────────────────────────────────────────

class SensorReading(BaseModel):
    """Temperature or other sensor data from an ESP32."""
    location: str = Field(..., description="Sensor location identifier (e.g., 'evaporator', 'bin_top')")
    value: float = Field(..., description="Sensor reading value")
    unit: str = Field("C", description="Unit of measurement")
    timestamp: datetime = Field(default_factory=_utc_now)


class PaymentEvent(BaseModel):
    """Credit inserted or payment status change from MDB ESP32."""
    amount: float = Field(..., description="Amount in dollars")
    method: str = Field(..., description="Payment method (e.g., 'cash', 'card')")
    timestamp: datetime = Field(default_factory=_utc_now)


class PaymentStatus(BaseModel):
    """MDB device readiness status."""
    device: str = Field(..., description="Device name (e.g., 'coin_acceptor', 'card_reader')")
    state: str = Field(..., description="Device state (e.g., 'ready', 'disabled', 'error')")
    timestamp: datetime = Field(default_factory=_utc_now)


class ButtonPress(BaseModel):
    """Physical button press from button panel ESP32."""
    button: int = Field(..., description="Button index")
    action: str = Field("pressed", description="Action type")
    timestamp: datetime = Field(default_factory=_utc_now)


class DispenserStatus(BaseModel):
    """Dispenser motor/mechanism status from ESP32."""
    slot: int = Field(..., description="Dispenser slot number")
    state: str = Field(..., description="Status (e.g., 'complete', 'jammed', 'error')")
    timestamp: datetime = Field(default_factory=_utc_now)


class SubsystemHeartbeat(BaseModel):
    """Periodic heartbeat from any ESP32 subsystem."""
    subsystem: str = Field(..., description="Subsystem identifier")
    uptime_seconds: int = Field(0, description="Seconds since last boot")
    timestamp: datetime = Field(default_factory=_utc_now)


# ──────────────────────────────────────────────
# RPi → ESP32: Outbound commands
# ──────────────────────────────────────────────

class DispenseCommand(BaseModel):
    """Command to dispense product from a slot."""
    slot: int = Field(..., description="Slot to dispense from")


class PaymentEnableCommand(BaseModel):
    """Enable or disable payment acceptance."""
    accept: bool = Field(..., description="True to accept payments, False to disable")


class DisplayMode(str, Enum):
    advertising = "advertising"
    transaction = "transaction"
    error = "error"
    maintenance = "maintenance"


class DisplayCommand(BaseModel):
    """Command to change the customer-facing display mode."""
    mode: DisplayMode = Field(..., description="Display mode to switch to")


# ──────────────────────────────────────────────
# RPi → World: Status and alerts
# ──────────────────────────────────────────────

class AlertLevel(str, Enum):
    info = "info"
    warning = "warning"
    error = "error"
    critical = "critical"


class VMCStatus(BaseModel):
    """Periodic VMC status published for HA and owner dashboard."""
    state: str = Field(..., description="Current FSM state")
    credit_escrow: float = Field(0.0)
    selected_product: Optional[str] = Field(None)
    uptime_seconds: int = Field(0)
    timestamp: datetime = Field(default_factory=_utc_now)


class VMCAlert(BaseModel):
    """Alert published when something needs owner attention."""
    level: AlertLevel
    message: str
    source: str = Field("vmc", description="Subsystem that generated the alert")
    timestamp: datetime = Field(default_factory=_utc_now)
