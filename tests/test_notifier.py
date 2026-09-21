# tests/test_notifier.py
"""Notifier rate limiting: distinct faults from one source must all reach the owner."""

from unittest.mock import AsyncMock

from config.config_model import ConfigModel
from services.health_monitor import Alert
from services.notifier import Notifier


def _notifier() -> Notifier:
    n = Notifier(ConfigModel())
    n._send_email = AsyncMock()
    # Force the email branch regardless of placeholder config.
    n._deliver = AsyncMock()
    return n


async def test_two_codes_from_same_source_both_send():
    n = _notifier()
    await n.send(Alert(level="error", source="vmc", message="a", code="ICE-301"))
    await n.send(Alert(level="error", source="vmc", message="b", code="PAY-103"))
    assert n._deliver.await_count == 2


async def test_same_code_twice_is_suppressed():
    n = _notifier()
    await n.send(Alert(level="error", source="vmc", message="a", code="ICE-301"))
    await n.send(Alert(level="error", source="vmc", message="a", code="ICE-301"))
    assert n._deliver.await_count == 1


async def test_same_code_different_product_both_send():
    n = _notifier()
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
    assert n._deliver.await_count == 2


async def test_no_code_falls_back_to_message():
    n = _notifier()
    await n.send(Alert(level="warning", source="mdb", message="stale"))
    await n.send(Alert(level="warning", source="mdb", message="different"))
    assert n._deliver.await_count == 2
