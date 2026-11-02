from loguru import logger

from ice_common.observers import Subscribers


def test_delivers_in_subscription_order():
    subs: Subscribers[[int]] = Subscribers()
    seen: list[tuple[str, int]] = []
    subs.subscribe(lambda n: seen.append(("a", n)))
    subs.subscribe(lambda n: seen.append(("b", n)))
    subs.publish(1)
    assert seen == [("a", 1), ("b", 1)]
    assert len(subs) == 2


def test_raising_subscriber_is_logged_and_rest_still_run():
    subs: Subscribers[[int]] = Subscribers()
    seen: list[int] = []
    errors: list[str] = []

    def boom(n: int) -> None:
        raise RuntimeError("boom")

    handle = logger.add(
        lambda m: errors.append(str(m)), level="ERROR", format="{message}"
    )
    try:
        subs.subscribe(boom)
        subs.subscribe(seen.append)
        subs.publish(7)
    finally:
        logger.remove(handle)
    assert seen == [7]
    assert len(errors) == 1
    assert "raised" in errors[0]


def test_unsubscribe_stops_delivery():
    subs: Subscribers[[int]] = Subscribers()
    seen: list[int] = []
    unsubscribe = subs.subscribe(seen.append)
    subs.publish(1)
    unsubscribe()
    subs.publish(2)
    assert seen == [1]
    assert len(subs) == 0


def test_unsubscribing_twice_is_harmless():
    subs: Subscribers[[int]] = Subscribers()
    seen: list[int] = []
    unsubscribe = subs.subscribe(seen.append)
    subs.subscribe(seen.append)
    unsubscribe()
    unsubscribe()
    assert len(subs) == 1


def test_health_edge_fires_failed_every_time_and_ok_only_on_recovery():
    from ice_common.observers import HealthEdge

    calls: list[str] = []
    edge = HealthEdge(lambda: calls.append("failed"), lambda: calls.append("ok"))
    edge.ok()
    assert calls == []
    edge.failed()
    edge.failed()
    assert calls == ["failed", "failed"]
    edge.ok()
    edge.ok()
    assert calls == ["failed", "failed", "ok"]
    edge.failed()
    assert calls[-1] == "failed"


def test_health_edge_tolerates_none_callbacks_and_logs_raising_ones():
    from ice_common.observers import HealthEdge

    edge = HealthEdge(None, None)
    edge.failed()
    edge.ok()

    def boom() -> None:
        raise RuntimeError("x")

    edge = HealthEdge(boom, boom)
    edge.failed()
    edge.ok()
    assert edge.is_failing is False
