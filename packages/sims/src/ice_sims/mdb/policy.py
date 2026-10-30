"""Policy state and the board-side acceptance rules (contract rules 1 and 2)."""

from dataclasses import dataclass

from ice_contracts.board.payment import InhibitReason, SetPolicyParams

from ice_sims.mdb.cash import CashBox


@dataclass
class PolicyState:
    policy: SetPolicyParams | None = None
    acked_in_boot: bool = False

    def apply(self, params: SetPolicyParams) -> None:
        self.policy = params
        self.acked_in_boot = True

    def reset(self) -> None:
        """Forget the policy (a board reboot): no policy, no money."""
        self.policy = None
        self.acked_in_boot = False

    def exact_change(self, cash: CashBox) -> bool:
        if self.policy is None:
            return False
        return cash.tube_total_cents() < self.policy.change_threshold_cents

    def accepts_coin(self, cents: int, cash: CashBox) -> bool:
        """Coins are never refused for the escrow cap (rule 2)."""
        p = self.policy
        return (
            p is not None
            and self.acked_in_boot
            and p.accept
            and cents in p.coin_enabled
        )

    def accepts_bill(self, cents: int, cash: CashBox) -> bool:
        p = self.policy
        if p is None or not self.acked_in_boot or not p.accept:
            return False
        if cents not in p.bill_enabled:
            return False
        if cash.escrow_cents + cents > p.max_escrow_cents:
            return False
        if cents > p.change_threshold_cents and self.exact_change(cash):
            return False
        return True

    def inhibited_reason(
        self, *, edge_silent: bool = False, device_fault: bool = False
    ) -> InhibitReason | None:
        if self.policy is None or not self.acked_in_boot:
            return InhibitReason.no_policy
        if edge_silent:
            return InhibitReason.edge_silent
        if not self.policy.accept:
            return InhibitReason.policy
        if device_fault:
            return InhibitReason.device_fault
        return None
