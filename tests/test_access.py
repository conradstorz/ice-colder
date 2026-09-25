"""Tests for services/access.py."""

import pytest

from services.access import BACKOFF_CAP_SECONDS, Backoff


class FakeClock:
    """Monotonic clock under test control."""

    def __init__(self, start: float = 1000.0):
        self.now = start

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


class FakeRequest:
    """Minimal stand-in for a starlette Request for client_ip()/is_https()."""

    class _Client:
        def __init__(self, host):
            self.host = host

    class _Url:
        def __init__(self, scheme):
            self.scheme = scheme

    def __init__(self, peer="10.0.0.5", headers=None, scheme="http"):
        self.client = self._Client(peer)
        self.headers = headers or {}
        self.url = self._Url(scheme)


class TestBackoffDelays:
    def test_first_attempt_is_allowed(self):
        b = Backoff(clock=FakeClock())
        assert b.check("pin", "u1", "dev1") is None

    def test_delays_double_from_one_second(self):
        clock = FakeClock()
        b = Backoff(clock=clock)
        for want in (1.0, 2.0, 4.0, 8.0, 16.0):
            b.record_failure("pin", "u1", "dev1", trusted=True)
            assert b.check("pin", "u1", "dev1", trusted=True) == pytest.approx(want)
            clock.advance(want)
            assert b.check("pin", "u1", "dev1", trusted=True) is None

    def test_delay_caps_at_one_hour(self):
        b = Backoff(clock=FakeClock())
        for _ in range(30):
            b.record_failure("pin", "u1", "dev1", trusted=True)
        assert b.check("pin", "u1", "dev1", trusted=True) == pytest.approx(
            BACKOFF_CAP_SECONDS
        )

    def test_success_resets_the_counter(self):
        b = Backoff(clock=FakeClock())
        for _ in range(5):
            b.record_failure("pin", "u1", "dev1", trusted=True)
        b.record_success("pin", "u1", "dev1", trusted=True)
        assert b.check("pin", "u1", "dev1", trusted=True) is None

    def test_clients_are_independent(self):
        b = Backoff(clock=FakeClock())
        for _ in range(4):
            b.record_failure("pin", "u1", "dev1", trusted=True)
        assert b.check("pin", "u1", "dev1", trusted=True) is not None
        assert b.check("pin", "u1", "dev2", trusted=True) is None

    def test_kinds_are_independent(self):
        b = Backoff(clock=FakeClock())
        for _ in range(4):
            b.record_failure("pin", "u1", "dev1", trusted=True)
        assert b.check("otp", "u1", "dev1", trusted=True) is None

    def test_entries_idle_a_day_are_pruned(self):
        clock = FakeClock()
        b = Backoff(clock=clock)
        for _ in range(4):
            b.record_failure("pin", "u1", "dev1", trusted=True)
        clock.advance(86401)
        assert b.check("pin", "u1", "dev1", trusted=True) is None
        assert b._failures == {}


class TestPerUserBudget:
    def test_untrusted_failures_slow_every_untrusted_client(self):
        clock = FakeClock()
        b = Backoff(clock=clock)
        # 20 failures across 20 distinct untrusted clients: each client's own
        # counter is 1 (a 1 s delay, long expired) but the budget has tripped.
        for i in range(20):
            if i:
                clock.advance(2)
            b.record_failure("pin", "u1", f"ip{i}")
        assert b.check("pin", "u1", "fresh-ip") == pytest.approx(1.0)

    def test_budget_delay_grows_and_caps(self):
        b = Backoff(clock=FakeClock())
        for i in range(30):
            b.record_failure("pin", "u1", f"ip{i}")
        # 30 failures -> 11 over the threshold -> 2 ** 10 == 1024 s.
        assert b.check("pin", "u1", "fresh-ip") == pytest.approx(1024.0)
        for i in range(30, 45):
            b.record_failure("pin", "u1", f"ip{i}")
        assert b.check("pin", "u1", "fresh-ip") == pytest.approx(BACKOFF_CAP_SECONDS)

    def test_budget_failures_age_out_of_the_hour(self):
        clock = FakeClock()
        b = Backoff(clock=clock)
        for i in range(20):
            b.record_failure("pin", "u1", f"ip{i}")
        clock.advance(3601)
        assert b.check("pin", "u1", "fresh-ip") is None

    def test_trusted_client_never_reads_the_budget(self):
        b = Backoff(clock=FakeClock())
        for i in range(25):
            b.record_failure("pin", "u1", f"ip{i}")
        # The legitimate tablet, where u1 is trusted, is unaffected.
        assert b.check("pin", "u1", "tablet", trusted=True) is None

    def test_trusted_failures_do_not_raise_the_budget(self):
        clock = FakeClock()
        b = Backoff(clock=clock)
        for _ in range(25):
            b.record_failure("pin", "u1", "tablet", trusted=True)
            b.record_success("pin", "u1", "tablet", trusted=True)
        assert b.check("pin", "u1", "fresh-ip") is None


class TestBackoffClientIp:
    def test_forwarded_header_ignored_from_untrusted_peer(self):
        b = Backoff(clock=FakeClock())
        req = FakeRequest("10.0.0.5", {"x-forwarded-for": "1.2.3.4"})
        assert b.client_ip(req) == "10.0.0.5"

    def test_rightmost_hop_used_from_trusted_proxy(self):
        b = Backoff(clock=FakeClock(), trusted_proxies=["10.0.0.0/24"])
        req = FakeRequest("10.0.0.5", {"x-forwarded-for": "1.2.3.4, 9.9.9.9"})
        assert b.client_ip(req) == "9.9.9.9"

    def test_trusted_proxy_without_header_falls_back_to_peer(self):
        b = Backoff(clock=FakeClock(), trusted_proxies=["10.0.0.0/24"])
        assert b.client_ip(FakeRequest("10.0.0.5")) == "10.0.0.5"

    def test_invalid_cidr_is_ignored_not_fatal(self):
        b = Backoff(clock=FakeClock(), trusted_proxies=["not-a-cidr", "10.0.0.0/24"])
        req = FakeRequest("10.0.0.5", {"x-forwarded-for": "9.9.9.9"})
        assert b.client_ip(req) == "9.9.9.9"


class TestBackoffIsHttps:
    def test_direct_https_scheme(self):
        b = Backoff(clock=FakeClock())
        assert b.is_https(FakeRequest(scheme="https")) is True

    def test_forwarded_proto_only_from_trusted_proxy(self):
        b = Backoff(clock=FakeClock(), trusted_proxies=["10.0.0.0/24"])
        req = FakeRequest("10.0.0.5", {"x-forwarded-proto": "https"})
        assert b.is_https(req) is True

    def test_forwarded_proto_from_untrusted_peer_is_ignored(self):
        b = Backoff(clock=FakeClock())
        req = FakeRequest("8.8.8.8", {"x-forwarded-proto": "https"})
        assert b.is_https(req) is False
