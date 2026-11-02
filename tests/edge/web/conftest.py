"""Fixtures shared by the edge web tests."""

import pytest

from ice_edge.monitoring.health_monitor import HealthMonitor
from ice_edge.web import routes


@pytest.fixture
def health_monitor():
    """A fresh HealthMonitor, installed as the web layer's health monitor
    for this test. The route context is restored after each test."""
    hm = HealthMonitor()
    routes.set_health_monitor(hm)
    return hm
