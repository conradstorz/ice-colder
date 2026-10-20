from ice_common.backoff import Backoff


def test_first_attempt_allowed_then_failure_delays():
    now = [0.0]
    backoff = Backoff(clock=lambda: now[0])
    assert backoff.check("pin", "u1", "1.2.3.4") is None
    backoff.record_failure("pin", "u1", "1.2.3.4")
    wait = backoff.check("pin", "u1", "1.2.3.4")
    assert wait is not None and wait > 0
