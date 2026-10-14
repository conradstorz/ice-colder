import json
from datetime import UTC, datetime
from uuid import uuid4

import httpx
import pytest

from ice_common.config_model import ConfigModel
from ice_contracts.cloud.report import MachineReport
from ice_edge.uplink.client import HttpUplink
from ice_edge.uplink.report import build_machine_report


@pytest.fixture
async def report(machine_fake_time) -> MachineReport:
    machine, _ = machine_fake_time
    return await build_machine_report(
        machine,
        config=ConfigModel(),
        seq=0,
        boot_id=str(uuid4()),
        now=datetime(2026, 10, 13, tzinfo=UTC),
    )


def _uplink(handler) -> HttpUplink:
    return HttpUplink(
        "http://cloud.test",
        "vmc-0000",
        transport=httpx.MockTransport(handler),
    )


async def test_post_report_204_is_true_and_hits_machine_path(report):
    seen = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(204)

    assert await _uplink(handler).post_report(report) is True
    assert seen[0].method == "POST"
    assert seen[0].url.path == "/api/v1/machines/vmc-0000/report"
    assert json.loads(seen[0].content)["machine_id"] == report.machine_id


async def test_post_report_500_is_false(report):
    assert await _uplink(lambda r: httpx.Response(500)).post_report(report) is False


async def test_post_report_connect_error_is_false_not_raised(report):
    def handler(request):
        raise httpx.ConnectError("down", request=request)

    assert await _uplink(handler).post_report(report) is False


@pytest.mark.parametrize("status", [200, 201])
async def test_put_ui_bundle_success(status):
    seen = []

    def handler(request):
        seen.append(request)
        return httpx.Response(status)

    assert await _uplink(handler).put_ui_bundle("ab" * 32, b"tgz") is True
    assert seen[0].method == "PUT"
    assert seen[0].url.path == "/api/v1/ui-bundles/" + "ab" * 32
    assert seen[0].content == b"tgz"


async def test_put_ui_bundle_400_and_error_are_false():
    assert (
        await _uplink(lambda r: httpx.Response(400)).put_ui_bundle("a", b"x") is False
    )

    def boom(request):
        raise httpx.ReadTimeout("slow", request=request)

    assert await _uplink(boom).put_ui_bundle("a", b"x") is False
