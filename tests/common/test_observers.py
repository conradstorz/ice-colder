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

    def boom(n: int) -> None:
        raise RuntimeError("boom")

    subs.subscribe(boom)
    subs.subscribe(seen.append)
    subs.publish(7)
    assert seen == [7]


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
