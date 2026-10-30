"""MachineReport v1.0.0: the snapshot a machine uploads to the cloud (cloud spec section 6)."""

from typing import Any, Literal

from pydantic import (
    AwareDatetime,
    BaseModel,
    ConfigDict,
    Field,
    model_validator,
)

from ice_contracts.cloud.ui import DIGEST_PATTERN
from ice_contracts.money import Cents, dollars_to_cents

CLOUD_CONTRACT_VERSION = "1.0.0"

# Caps added without a version bump: a claimed fake machine must not be able to
# flap a huge faults list and mint thousands of alert rows per report. Text
# fields can carry operator-entered product names/skus, so the edge clips them
# to MAX_TEXT when it builds a report (ice_edge.views.health.clip).
MAX_FAULTS = 200
MAX_TEXT = 200

_UUID4_PATTERN = (
    r"^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$"
)


class _Model(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


def _legacy_dollars(data: Any, old: str, new: str, convert) -> Any:
    """0.x input carried dollars under `old`; 1.0.0 carries cents under `new`.

    Only the old key alone is converted: when both are present the new one
    wins and the stale old key is left for `extra="forbid"` to reject.
    """
    if isinstance(data, dict) and old in data and new not in data:
        data = dict(data)
        value = data.pop(old)
        data[new] = None if value is None else convert(value)
    return data


def _to_float_cents(value: float) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"not a dollar amount: {value!r}")
    return value * 100


class UiIdentity(_Model):
    digest: str = Field(pattern=DIGEST_PATTERN)
    ice_ui_version: str


class StatusView(_Model):
    state: str
    selected_product: str | None = Field(
        max_length=MAX_TEXT
    )  # the product's name, not its sku
    credit_escrow_cents: Cents
    last_payment_method: str | None

    @model_validator(mode="before")
    @classmethod
    def _from_dollars(cls, data: Any) -> Any:
        return _legacy_dollars(
            data, "credit_escrow", "credit_escrow_cents", dollars_to_cents
        )


class FaultBase(_Model):
    """The seven keys FaultService.active_faults() emits."""

    key: str = Field(max_length=MAX_TEXT)
    sku: str | None = Field(max_length=MAX_TEXT)
    product: str | None = Field(max_length=MAX_TEXT)
    code: str = Field(max_length=MAX_TEXT)
    severity: str
    scope: str  # "product" | "machine"
    description: str = Field(max_length=MAX_TEXT)


class ActiveFaultView(FaultBase):
    since_seconds: float | None  # time-relative, as of sent_at


class PendingSaleView(_Model):
    sku: str
    price_cents: Cents
    methods: dict[str, Cents]

    @model_validator(mode="before")
    @classmethod
    def _from_dollars(cls, data: Any) -> Any:
        legacy = isinstance(data, dict) and "price" in data
        data = _legacy_dollars(data, "price", "price_cents", dollars_to_cents)
        if legacy and isinstance(data.get("methods"), dict):
            # 0.x methods were dollar amounts alongside the dollar price.
            data = {
                **data,
                "methods": {k: dollars_to_cents(v) for k, v in data["methods"].items()},
            }
        return data


class FaultView(FaultBase):
    since_seconds: float | None
    gate: Literal["safety", "fulfillment", "alert"]
    pending_sale: PendingSaleView | None


class HealthSummary(_Model):
    """Typed form of the edge's health_snapshot() dict."""

    vmc_missing: bool
    status: StatusView | None
    is_healthy: bool | None
    issues: list[str]
    active_faults: list[ActiveFaultView] = Field(max_length=MAX_FAULTS)
    payment_enabled: bool | None
    payment_reasons: list[str]
    machine_stopped: bool | None
    maintenance: str | None


class KpiWindow(_Model):
    money_in_cents: Cents
    products_out: int
    vends_failed: int
    errors: int
    uptime_pct: float

    @model_validator(mode="before")
    @classmethod
    def _from_dollars(cls, data: Any) -> Any:
        return _legacy_dollars(data, "money_in", "money_in_cents", dollars_to_cents)


class KpiAverage(_Model):
    money_in_cents: float | None  # an average: float cents, not quantized
    products_out: float | None
    errors: float | None
    uptime_pct: float | None

    @model_validator(mode="before")
    @classmethod
    def _from_dollars(cls, data: Any) -> Any:
        return _legacy_dollars(data, "money_in", "money_in_cents", _to_float_cents)


class KpiSummary(_Model):
    summary: KpiWindow | None
    average: KpiAverage | None


class BuildView(_Model):
    commit: str
    commit_short: str
    source: str
    uptime_seconds: int
    machine_id: str | None


class HealthPageSummary(_Model):
    mqtt_connected: bool | None
    vmc_state: str | None
    vmc_build: BuildView | None
    subsystems_ok: int
    subsystems_total: int


class MachineReport(_Model):
    contract: Literal["0.2.0", "0.3.0", "0.4.0", "1.0.0"]
    machine_id: str = Field(pattern=r"^[A-Za-z0-9_.-]{1,64}$")
    common_name: str | None = Field(default=None, max_length=MAX_TEXT)
    sent_at: AwareDatetime
    boot_id: str = Field(pattern=_UUID4_PATTERN)
    seq: int = Field(ge=0)
    report_interval_seconds: int = Field(ge=10, le=3600)
    edge_build: str
    ui: UiIdentity
    health: HealthSummary
    kpi: KpiSummary
    faults: list[FaultView] = Field(max_length=MAX_FAULTS)
    health_page: HealthPageSummary
