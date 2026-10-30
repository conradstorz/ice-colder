"""Cash hardware model: coin tubes, stacker and the escrow account.

Pure and synchronous. All money is integer cents.
"""

from dataclasses import dataclass, field

from ice_contracts.board.payment import (
    CoinRoute,
    PayoutStatus,
    StackerState,
    TubeLevel,
)

STACKER_CAPACITY = 500
DEFAULT_TUBE_CAPACITY = 100


@dataclass
class Tube:
    cents: int
    count: int
    capacity: int = DEFAULT_TUBE_CAPACITY

    @property
    def full(self) -> bool:
        return self.count >= self.capacity


@dataclass(frozen=True)
class PayoutOutcome:
    status: PayoutStatus
    paid_cents: int
    detail: str | None = None


def _default_tubes() -> list[Tube]:
    return [
        Tube(5, 40, DEFAULT_TUBE_CAPACITY),
        Tube(10, 40, DEFAULT_TUBE_CAPACITY),
        Tube(25, 40, DEFAULT_TUBE_CAPACITY),
        Tube(100, 0, DEFAULT_TUBE_CAPACITY),
    ]


@dataclass
class CashBox:
    tubes: list[Tube] = field(default_factory=_default_tubes)
    stacker_count: int = 0
    stacker_capacity: int = STACKER_CAPACITY
    bill_in_escrow_cents: int | None = None
    escrow_cents: int = 0
    cashbox_coins_cents: int = 0

    def tube_total_cents(self) -> int:
        return sum(t.cents * t.count for t in self.tubes)

    def accept_coin(self, cents: int) -> CoinRoute:
        """Tube the coin if its tube exists and has room, else cashbox it."""
        self.escrow_cents += cents
        for tube in self.tubes:
            if tube.cents == cents and not tube.full:
                tube.count += 1
                return CoinRoute.tubes
        self.cashbox_coins_cents += cents
        return CoinRoute.cashbox

    def escrow_bill(self, cents: int) -> None:
        self.bill_in_escrow_cents = cents

    def return_bill(self) -> int | None:
        cents, self.bill_in_escrow_cents = self.bill_in_escrow_cents, None
        return cents

    def stack_bill(self, cents: int) -> None:
        self.bill_in_escrow_cents = None
        self.stacker_count += 1
        self.escrow_cents += cents

    def payout(self, cents: int) -> PayoutOutcome:
        """Pay from tubes, largest coin first. A shortfall is `partial`."""
        remaining = cents
        paid = 0
        for tube in sorted(self.tubes, key=lambda t: t.cents, reverse=True):
            take = min(tube.count, remaining // tube.cents)
            if take > 0:
                tube.count -= take
                remaining -= take * tube.cents
                paid += take * tube.cents
        self.escrow_cents -= min(paid, self.escrow_cents)
        if paid == cents:
            return PayoutOutcome(PayoutStatus.ok, paid)
        if paid == 0:
            return PayoutOutcome(PayoutStatus.failed, 0, "no_coins")
        return PayoutOutcome(PayoutStatus.partial, paid, "tubes_short")

    def cash_sale(self, cents: int) -> tuple[int, bool]:
        """Lower escrow by a sale price. Returns (applied_cents, clamped)."""
        if cents > self.escrow_cents:
            applied = self.escrow_cents
            self.escrow_cents = 0
            return applied, True
        self.escrow_cents -= cents
        return cents, False

    def tube_levels(self) -> list[TubeLevel]:
        return [
            TubeLevel(cents=t.cents, count=t.count, full=t.full) for t in self.tubes
        ]

    def stacker_state(self) -> StackerState:
        return StackerState(
            count=self.stacker_count,
            full=self.stacker_count >= self.stacker_capacity,
        )
