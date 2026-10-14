# tests/test_notifier.py
"""Notifier rate limiting: distinct faults from one source must all reach the owner."""

from types import SimpleNamespace
from unittest.mock import AsyncMock


from ice_common.config_model import ConfigModel
from ice_edge.monitoring.health_monitor import Alert
from ice_edge.notify.notifier import Notifier


def _notifier_with_mock():
    """Stub delivery to track send attempts; these tests cover cooldown/dedup only."""
    n = Notifier(ConfigModel())
    deliver_mock = AsyncMock()
    n._deliver = deliver_mock
    return n, deliver_mock


async def test_two_codes_from_same_source_both_send():
    n, deliver_mock = _notifier_with_mock()
    await n.send(Alert(level="error", source="vmc", message="a", code="ICE-301"))
    await n.send(Alert(level="error", source="vmc", message="b", code="PAY-103"))
    assert deliver_mock.await_count == 2


async def test_same_code_twice_is_suppressed():
    n, deliver_mock = _notifier_with_mock()
    await n.send(Alert(level="error", source="vmc", message="a", code="ICE-301"))
    await n.send(Alert(level="error", source="vmc", message="a", code="ICE-301"))
    assert deliver_mock.await_count == 1


async def test_same_code_different_product_both_send():
    n, deliver_mock = _notifier_with_mock()
    await n.send(
        Alert(
            level="error",
            source="vmc",
            message="a",
            code="ICE-301",
            product_sku="ICE-1",
        )
    )
    await n.send(
        Alert(
            level="error",
            source="vmc",
            message="a",
            code="ICE-301",
            product_sku="ICE-2",
        )
    )
    assert deliver_mock.await_count == 2


async def test_no_code_falls_back_to_message():
    n, deliver_mock = _notifier_with_mock()
    await n.send(Alert(level="warning", source="mdb", message="stale"))
    await n.send(Alert(level="warning", source="mdb", message="different"))
    assert deliver_mock.await_count == 2


async def test_cooldown_expires_and_resends(monkeypatch):
    """Send an alert, suppress the next (within cooldown), resend after cooldown expires."""
    clock = [1000.0]
    fake_loop = SimpleNamespace(time=lambda: clock[0])
    monkeypatch.setattr(
        "ice_edge.notify.notifier.asyncio",
        SimpleNamespace(get_running_loop=lambda: fake_loop),
    )

    n, deliver_mock = _notifier_with_mock()
    first_alert = Alert(level="error", source="vmc", message="a", code="ICE-301")

    await n.send(first_alert)
    assert deliver_mock.await_count == 1

    clock[0] = 1000.0 + 299  # Within cooldown (< 300 seconds)
    await n.send(first_alert)
    assert deliver_mock.await_count == 1  # Still suppressed

    clock[0] = 1000.0 + 301  # Past cooldown
    await n.send(first_alert)
    assert deliver_mock.await_count == 2  # Resent after cooldown expires
