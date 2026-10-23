"""Exponential back-off rate limiter, shared by the edge dashboard and the cloud."""

from __future__ import annotations

import ipaddress
import time
from collections import deque
from typing import Callable, Optional

from loguru import logger

BACKOFF_CAP_SECONDS = 3600.0
BUDGET_THRESHOLD = 20
BUDGET_WINDOW_SECONDS = 3600.0
BACKOFF_PRUNE_SECONDS = 86400.0
# Hard bound on each bookkeeping dictionary, so spraying distinct subjects or
# clients cannot grow memory without limit; the oldest entries go first.
BACKOFF_MAX_KEYS = 10_000
# The full expiry sweep is O(n); run it at most this often rather than per call.
BACKOFF_SWEEP_INTERVAL_SECONDS = 60.0
# Evicting down to 90% amortises the sort over many inserts at the cap.
_TRIM_TO = BACKOFF_MAX_KEYS * 9 // 10


class Backoff:
    """Exponential back-off per (kind, subject, client), plus a per-user budget.

    After ``n`` consecutive failures the next attempt waits
    ``min(2 ** (n - 1), 3600)`` seconds. A success resets that key. Failures
    from clients where the subject user is not trusted also feed a per-user
    budget, so a distributed attacker cannot buy fresh counters with fresh
    IPs; attempts from a client the user is trusted on never touch it, so a
    stranger can never slow the legitimate user on their own tablet.

    There are no hard caps, no per-user disabling and no IP lockout.
    """

    def __init__(
        self,
        clock: Callable[[], float] = time.monotonic,
        trusted_proxies: Optional[list[str]] = None,
    ):
        self._clock = clock
        # (kind, subject, client) -> (consecutive_failures, last_failure_at)
        self._failures: dict[tuple[str, str, str], tuple[int, float]] = {}
        # (kind, subject) -> timestamps of untrusted failures inside the window
        self._budget: dict[tuple[str, str], deque[float]] = {}
        self._last_sweep = float("-inf")
        self._networks: list[ipaddress.IPv4Network | ipaddress.IPv6Network] = []
        self.set_trusted_proxies(trusted_proxies or [])

    # --- configuration ---

    def set_trusted_proxies(self, cidrs: list[str]) -> None:
        self._networks = []
        for cidr in cidrs:
            try:
                self._networks.append(ipaddress.ip_network(cidr.strip(), strict=False))
            except ValueError:
                logger.warning(f"Backoff: ignoring invalid trusted proxy CIDR {cidr!r}")

    def reset(self) -> None:
        """Clear every recorded failure and budget entry.

        The process only ever holds one `Backoff` (`web_auth.backoff`), so
        every test that exercises it shares that same registry — this is
        the public way to isolate one test's counters from the next,
        rather than a test reaching into `_failures`/`_budget` directly.
        Trusted-proxy configuration is untouched; call
        `set_trusted_proxies` separately if that also needs resetting.
        """
        self._failures.clear()
        self._budget.clear()
        self._last_sweep = float("-inf")

    def sizes(self) -> tuple[int, int]:
        """(failure keys, budget keys) currently held; both are capped."""
        return len(self._failures), len(self._budget)

    def _is_trusted_proxy(self, peer: str) -> bool:
        try:
            addr = ipaddress.ip_address(peer)
        except ValueError:
            return False
        return any(addr in net for net in self._networks)

    def client_ip(self, request) -> str:
        """The address to key on when no stored device resolves.

        Only when the socket peer is a configured proxy is X-Forwarded-For
        consulted, and then its rightmost entry: the hop that proxy appended.
        Anything a client supplied itself sits to the left and is ignored.
        """
        peer = request.client.host if request.client else "unknown"
        if not self._is_trusted_proxy(peer):
            return peer
        forwarded = request.headers.get("x-forwarded-for", "")
        hops = [h.strip() for h in forwarded.split(",") if h.strip()]
        return hops[-1] if hops else peer

    def is_https(self, request) -> bool:
        """True when the browser's own hop was TLS, for the Secure cookie flag."""
        if request.url.scheme == "https":
            return True
        peer = request.client.host if request.client else "unknown"
        if not self._is_trusted_proxy(peer):
            return False
        return request.headers.get("x-forwarded-proto", "").lower() == "https"

    # --- accounting ---

    def _prune(self, now: float) -> None:
        if now - self._last_sweep < BACKOFF_SWEEP_INTERVAL_SECONDS:
            return
        self._last_sweep = now
        for key, (_, last) in list(self._failures.items()):
            if now - last > BACKOFF_PRUNE_SECONDS:
                del self._failures[key]
        for key, stamps in list(self._budget.items()):
            while stamps and now - stamps[0] > BUDGET_WINDOW_SECONDS:
                stamps.popleft()
            if not stamps:
                del self._budget[key]

    def _enforce_cap(self) -> None:
        if len(self._failures) > BACKOFF_MAX_KEYS:
            excess = len(self._failures) - _TRIM_TO
            oldest = sorted(self._failures, key=lambda k: self._failures[k][1])
            for key in oldest[:excess]:
                del self._failures[key]
        if len(self._budget) > BACKOFF_MAX_KEYS:
            excess = len(self._budget) - _TRIM_TO
            oldest = sorted(
                self._budget,
                key=lambda k: self._budget[k][-1] if self._budget[k] else 0.0,
            )
            for key in oldest[:excess]:
                del self._budget[key]

    @staticmethod
    def _delay(failures: int) -> float:
        return min(2.0 ** (failures - 1), BACKOFF_CAP_SECONDS)

    def check(
        self, kind: str, subject: str, client: str, *, trusted: bool = False
    ) -> float | None:
        """Seconds still to wait before another attempt, or None if allowed."""
        now = self._clock()
        self._prune(now)
        waits: list[float] = []

        entry = self._failures.get((kind, subject, client))
        if entry:
            failures, last = entry
            waits.append(last + self._delay(failures) - now)

        if not trusted:
            stamps = self._budget.get((kind, subject))
            if stamps and len(stamps) >= BUDGET_THRESHOLD:
                over = len(stamps) - BUDGET_THRESHOLD + 1
                waits.append(stamps[-1] + self._delay(over) - now)

        remaining = max(waits, default=0.0)
        return remaining if remaining > 0 else None

    def record_failure(
        self, kind: str, subject: str, client: str, *, trusted: bool = False
    ) -> None:
        now = self._clock()
        self._prune(now)
        failures, _ = self._failures.get((kind, subject, client), (0, 0.0))
        self._failures[(kind, subject, client)] = (failures + 1, now)
        if not trusted:
            self._budget.setdefault((kind, subject), deque()).append(now)
        self._enforce_cap()

    def record_success(
        self, kind: str, subject: str, client: str, *, trusted: bool = False
    ) -> None:
        """Clear this key's counter.

        The per-user budget is deliberately left alone: one correct guess must
        not wipe the cost an attack has already accumulated.
        """
        self._prune(self._clock())
        self._failures.pop((kind, subject, client), None)
