from ice_common.backoff import BACKOFF_MAX_KEYS, Backoff


def test_first_attempt_allowed_then_failure_delays():
    now = [0.0]
    backoff = Backoff(clock=lambda: now[0])
    assert backoff.check("pin", "u1", "1.2.3.4") is None
    backoff.record_failure("pin", "u1", "1.2.3.4")
    wait = backoff.check("pin", "u1", "1.2.3.4")
    assert wait is not None and wait > 0


def test_dictionaries_never_exceed_the_key_cap():
    now = [0.0]
    backoff = Backoff(clock=lambda: now[0])
    for i in range(BACKOFF_MAX_KEYS + 500):
        now[0] += 0.001
        backoff.record_failure("claim", f"s{i}", f"c{i}")
        assert max(backoff.sizes()) <= BACKOFF_MAX_KEYS
    failures, budget = backoff.sizes()
    assert failures <= BACKOFF_MAX_KEYS
    assert budget <= BACKOFF_MAX_KEYS


def test_cap_drops_the_oldest_entries_first():
    now = [0.0]
    backoff = Backoff(clock=lambda: now[0])
    for i in range(BACKOFF_MAX_KEYS + 1):
        now[0] += 0.001
        backoff.record_failure("claim", f"s{i}", "c")
    assert backoff.check("claim", "s0", "c") is None  # oldest evicted
    assert backoff.check("claim", f"s{BACKOFF_MAX_KEYS}", "c") is not None


def test_sweep_interval_does_not_change_boundary_results():
    now = [0.0]
    backoff = Backoff(clock=lambda: now[0])
    for _ in range(3):
        backoff.record_failure("pin", "u", "c")
    # 3 failures -> 4 s wait; stays enforced across calls inside one interval.
    now[0] = 3.0
    assert backoff.check("pin", "u", "c") == 1.0
    now[0] = 4.0
    assert backoff.check("pin", "u", "c") is None
    # Budget window expiry still takes effect between sweeps.
    for i in range(20):
        backoff.record_failure("pin", "v", f"c{i}")
    assert backoff.check("pin", "v", "new") is not None
    now[0] += 3601.0
    assert backoff.check("pin", "v", "new") is None
