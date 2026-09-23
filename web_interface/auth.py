"""Failed-login limiter for the dashboard's HTTP Basic auth.

Per client IP, a sliding window of failed attempts; too many inside the
window locks that IP out for a while. State is in-process memory: a restart
clears it, which is fine for a single machine.
"""

from __future__ import annotations

import ipaddress
import time
from collections import deque
from typing import Callable, Optional

from loguru import logger


class LoginLimiter:
    def __init__(
        self,
        max_failures: int = 10,
        window_seconds: float = 900.0,
        lockout_seconds: float = 900.0,
        clock: Callable[[], float] = time.monotonic,
        trusted_proxies: Optional[list[str]] = None,
    ):
        self._max = max_failures
        self._window = window_seconds
        self._lockout = lockout_seconds
        self._clock = clock
        self._failures: dict[str, deque[float]] = {}
        self._locked_until: dict[str, float] = {}
        self._networks: list[ipaddress.IPv4Network | ipaddress.IPv6Network] = []
        self.set_trusted_proxies(trusted_proxies or [])

    # --- configuration ---

    def set_trusted_proxies(self, cidrs: list[str]) -> None:
        self._networks = []
        for cidr in cidrs:
            try:
                self._networks.append(ipaddress.ip_network(cidr.strip(), strict=False))
            except ValueError:
                logger.warning(
                    f"LoginLimiter: ignoring invalid trusted proxy CIDR {cidr!r}"
                )

    def _is_trusted_proxy(self, peer: str) -> bool:
        try:
            addr = ipaddress.ip_address(peer)
        except ValueError:
            return False
        return any(addr in net for net in self._networks)

    def client_ip(self, request) -> str:
        """The address to rate-limit on.

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

    # --- accounting ---

    def _prune(self, now: float) -> None:
        cutoff = now - self._window
        for ip in list(self._failures):
            dq = self._failures[ip]
            while dq and dq[0] < cutoff:
                dq.popleft()
            if not dq:
                del self._failures[ip]
        for ip in list(self._locked_until):
            if self._locked_until[ip] <= now:
                del self._locked_until[ip]

    def check(self, ip: str) -> float | None:
        """Seconds remaining in a lockout for ip, or None if allowed."""
        now = self._clock()
        self._prune(now)
        until = self._locked_until.get(ip)
        if until is None:
            return None
        return until - now

    def record_failure(self, ip: str) -> None:
        now = self._clock()
        self._prune(now)
        dq = self._failures.setdefault(ip, deque())
        dq.append(now)
        if len(dq) >= self._max:
            self._locked_until[ip] = now + self._lockout
            del self._failures[ip]
            logger.warning(
                f"Dashboard: {ip} locked out for {self._lockout:.0f}s after "
                f"{self._max} failed logins"
            )

    def record_success(self, ip: str) -> None:
        self._failures.pop(ip, None)
