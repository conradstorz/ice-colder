from types import SimpleNamespace

from web_interface.auth import LoginLimiter


class Clock:
    def __init__(self):
        self.now = 1000.0

    def __call__(self):
        return self.now


def _req(peer, xff=None):
    headers = {"x-forwarded-for": xff} if xff else {}
    return SimpleNamespace(client=SimpleNamespace(host=peer), headers=headers)


def test_locks_after_max_failures_and_expires():
    clock = Clock()
    lim = LoginLimiter(
        max_failures=3, window_seconds=100, lockout_seconds=50, clock=clock
    )
    for _ in range(2):
        lim.record_failure("1.2.3.4")
    assert lim.check("1.2.3.4") is None
    lim.record_failure("1.2.3.4")
    remaining = lim.check("1.2.3.4")
    assert remaining is not None and 0 < remaining <= 50
    clock.now += 51
    assert lim.check("1.2.3.4") is None


def test_failures_outside_window_do_not_count():
    clock = Clock()
    lim = LoginLimiter(
        max_failures=3, window_seconds=100, lockout_seconds=50, clock=clock
    )
    lim.record_failure("a")
    lim.record_failure("a")
    clock.now += 101
    lim.record_failure("a")
    assert lim.check("a") is None


def test_success_clears_failures():
    lim = LoginLimiter(max_failures=2)
    lim.record_failure("a")
    lim.record_success("a")
    lim.record_failure("a")
    assert lim.check("a") is None


def test_ips_are_independent():
    lim = LoginLimiter(max_failures=1)
    lim.record_failure("a")
    assert lim.check("a") is not None
    assert lim.check("b") is None


def test_client_ip_ignores_forwarded_header_from_untrusted_peer():
    lim = LoginLimiter()
    assert lim.client_ip(_req("203.0.113.9", xff="10.0.0.1")) == "203.0.113.9"


def test_client_ip_uses_rightmost_forwarded_hop_from_trusted_proxy():
    lim = LoginLimiter(trusted_proxies=["172.25.0.0/16"])
    assert (
        lim.client_ip(_req("172.25.0.7", xff="10.0.0.1, 198.51.100.4"))
        == "198.51.100.4"
    )


def test_client_ip_trusted_proxy_without_header_falls_back_to_peer():
    lim = LoginLimiter(trusted_proxies=["172.25.0.0/16"])
    assert lim.client_ip(_req("172.25.0.7")) == "172.25.0.7"


def test_invalid_cidr_is_ignored_not_fatal():
    lim = LoginLimiter(trusted_proxies=["not-a-cidr", "172.25.0.0/16"])
    assert lim.client_ip(_req("172.25.0.7", xff="198.51.100.4")) == "198.51.100.4"


def test_pruning_bounds_memory():
    clock = Clock()
    lim = LoginLimiter(max_failures=5, window_seconds=10, clock=clock)
    for i in range(50):
        lim.record_failure(f"ip{i}")
    clock.now += 11
    lim.check("ip0")
    assert len(lim._failures) == 0
