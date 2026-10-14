"""MachineReport v0.1.0: the snapshot a machine uploads to the cloud (cloud spec section 6)."""

from typing import Literal

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field

from ice_contracts.cloud.ui import DIGEST_PATTERN

CLOUD_CONTRACT_VERSION = "0.1.0"

_UUID4_PATTERN = (
    r"^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$"
)


class _Model(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class UiIdentity(_Model):
    digest: str = Field(pattern=DIGEST_PATTERN)
    ice_ui_version: str


class StatusView(_Model):
    state: str
    selected_product: str | None  # the product's name, not its sku
    credit_escrow: float
    last_payment_method: str | None


class FaultBase(_Model):
    """The seven keys FaultService.active_faults() emits."""

    key: str
    sku: str | None
    product: str | None
    code: str
    severity: str
    scope: str  # "product" | "machine"
    description: str


class ActiveFaultView(FaultBase):
    since_seconds: float | None  # time-relative, as of sent_at


class PendingSaleView(_Model):
    sku: str
    price: float
    methods: dict[str, float]


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
    active_faults: list[ActiveFaultView]
    payment_enabled: bool | None
    payment_reasons: list[str]
    machine_stopped: bool | None
    maintenance: str | None


class KpiWindow(_Model):
    money_in: float
    products_out: int
    vends_failed: int
    errors: int
    uptime_pct: float


class KpiAverage(_Model):
    money_in: float | None
    products_out: float | None
    errors: float | None
    uptime_pct: float | None


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
    contract: Literal["0.1.0"]
    machine_id: str = Field(pattern=r"^[A-Za-z0-9_.-]{1,64}$")
    sent_at: AwareDatetime
    boot_id: str = Field(pattern=_UUID4_PATTERN)
    seq: int = Field(ge=0)
    report_interval_seconds: int = Field(ge=10, le=3600)
    edge_build: str
    ui: UiIdentity
    health: HealthSummary
    kpi: KpiSummary
    faults: list[FaultView]
    health_page: HealthPageSummary
