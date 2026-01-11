import os
from datetime import datetime, timezone
from typing import Dict, Literal, Optional

from config_model import Channel
from loguru import logger
from pydantic import BaseModel, Field, model_validator


@logger.catch()
def atomic_write(path: str, data: str) -> None:
    """
    Write data to a temp file and atomically replace the target path to avoid corruption.
    """
    tmp_path = f"{path}.tmp"
    with open(tmp_path, "w") as f:
        f.write(data)
    os.replace(tmp_path, path)


class ProductState(BaseModel):
    sku: str
    inventory_count: int
    vend_count: int = 0
    revenue: float = 0.0


class ChannelState(BaseModel):
    channel: Channel
    revenue: float = 0.0
    transactions: int = 0
    last_transaction: Optional[datetime] = None

    @model_validator(mode="after")
    def ensure_timestamp(cls, values):
        # Ensure last_transaction is timezone-aware or None
        ts = values.last_transaction
        if ts and ts.tzinfo is None:
            values.last_transaction = ts.replace(tzinfo=timezone.utc)
        return values


class MachineState(BaseModel):
    # Restrict FSM state to known values
    fsm_state: Literal["idle", "interacting_with_user", "dispensing", "error"]
    credit_escrow: float = 0.0
    current_sku: Optional[str] = None
    channel_states: Dict[Channel, ChannelState] = Field(default_factory=dict)
    product_states: Dict[str, ProductState] = Field(default_factory=dict)
    # Use a timezone-aware default
    last_updated: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))

    def record_transaction(
        self,
        channel: Channel,
        sku: str,
        amount: float,
        timestamp: Optional[datetime] = None
    ) -> None:
        """
        Update channel and product stats for a completed vend.
        """
        ts = timestamp or datetime.utcnow()
        # Update channel state
        ch_state = self.channel_states.get(channel)
        if not ch_state:
            ch_state = ChannelState(channel=channel)
            self.channel_states[channel] = ch_state
        ch_state.revenue += amount
        ch_state.transactions += 1
        ch_state.last_transaction = ts

        # Update product state
        prod_state = self.product_states.get(sku)
        if not prod_state:
            # First-time seeing this SKU; initialize
            prod_state = ProductState(sku=sku, inventory_count=0)
            self.product_states[sku] = prod_state
        prod_state.vend_count += 1
        prod_state.revenue += amount

        # Reset credit and current SKU
        self.credit_escrow = 0.0
        self.current_sku = None
        self.last_updated = ts

    def to_file(self, path: str) -> None:
        """
        Serialize state to JSON file with atomic replace.
        """
        data = self.model_dump_json(indent=2)
        atomic_write(path, data)

    @classmethod
    def from_file(cls, path: str) -> "MachineState":
        """
        Load state from JSON file; if missing, return a default-initialized state.
        """
        if not os.path.exists(path):
            return cls(fsm_state="idle")
        try:
            with open(path, "r", encoding="utf-8") as f:
                text = f.read()
            return cls.model_validate_json(text)
        except Exception as e:
            logger.error(f"Failed to load state [{path}]: {e}")
            return cls(fsm_state="idle")
