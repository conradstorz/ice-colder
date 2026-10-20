import base64
import json
from datetime import UTC, datetime
from uuid import uuid4

import httpx
import pytest

from ice_common.config_model import ConfigModel
from ice_contracts.cloud import signing
from ice_contracts.cloud.identity import ClaimCodeNotice, RegisterMachine
from ice_contracts.cloud.report import MachineReport
from ice_edge.uplink.client import HttpUplink
from ice_edge.uplink.identity import MachineIdentity
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


@pytest.fixture
def identity(tmp_path) -> MachineIdentity:
    return MachineIdentity.load_or_create(tmp_path / "identity.json")


def _uplink(handler, identity, now=None) -> HttpUplink:
    kwargs = {} if now is None else {"now": now}
    return HttpUplink(
        "http://cloud.test",
        identity,
        transport=httpx.MockTransport(handler),
        **kwargs,
    )


def _signature_ok(request: httpx.Request, identity: MachineIdentity) -> bool:
    message = signing.canonical_message(
        request.method,
        request.url.path,
        request.headers[signing.HEADER_TIMESTAMP],
        request.content,
    )
    return request.headers[signing.HEADER_MACHINE] == identity.fingerprint and (
        signing.verify(
            base64.b64decode(identity.public_key_b64),
            message,
            request.headers[signing.HEADER_SIGNATURE],
        )
    )


async def test_every_request_carries_valid_signature(report, identity):
    seen = []

    def handler(request):
        seen.append(request)
        return httpx.Response(200)

    up = _uplink(handler, identity)
    notice = ClaimCodeNotice(claim_code_hash="h", expires_at=None)
    assert await up.put_ui_bundle("ab" * 32, b"tgz")
    assert await up.post_report(report)
    assert await up.register("Lobby")
    assert await up.post_claim_code(notice)
    assert await up.delete_claim_code()
    assert [r.method for r in seen] == ["PUT", "POST", "POST", "POST", "DELETE"]
    assert all(_signature_ok(r, identity) for r in seen)


async def test_report_path_uses_fingerprint(report, identity):
    seen = []

    def handler(request):
        seen.append(request)
        return httpx.Response(204)

    await _uplink(handler, identity).post_report(report)
    assert seen[0].url.path == f"/api/v1/machines/{identity.fingerprint}/report"


async def test_register_body_is_RegisterMachine(identity):
    seen = []

    def handler(request):
        seen.append(request)
        return httpx.Response(200)

    assert await _uplink(handler, identity).register("Lobby") is True
    assert seen[0].url.path == "/api/v1/machines/register"
    body = RegisterMachine.model_validate_json(seen[0].content)
    assert body.public_key_b64 == identity.public_key_b64
    assert body.label == "Lobby"


async def test_claim_code_paths_and_body(identity):
    seen = []

    def handler(request):
        seen.append(request)
        return httpx.Response(204)

    up = _uplink(handler, identity)
    notice = ClaimCodeNotice(claim_code_hash="h", expires_at=None)
    await up.post_claim_code(notice)
    await up.delete_claim_code()
    path = f"/api/v1/machines/{identity.fingerprint}/claim-code"
    assert [(r.method, r.url.path) for r in seen] == [("POST", path), ("DELETE", path)]
    assert ClaimCodeNotice.model_validate_json(seen[0].content) == notice


async def test_stale_now_still_sends_but_server_401_is_false(identity):
    stale = datetime(2020, 1, 1, tzinfo=UTC)
    seen = []

    def handler(request):
        seen.append(request)
        return httpx.Response(401)

    up = _uplink(handler, identity, now=lambda: stale)
    assert await up.register("Lobby") is False
    assert seen[0].headers[signing.HEADER_TIMESTAMP] == signing.format_timestamp(stale)


async def test_post_report_204_is_true_and_hits_machine_path(report, identity):
    seen = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(204)

    assert await _uplink(handler, identity).post_report(report) is True
    assert seen[0].method == "POST"
    assert seen[0].url.path == f"/api/v1/machines/{identity.fingerprint}/report"
    assert json.loads(seen[0].content)["machine_id"] == report.machine_id


async def test_post_report_500_is_false(report, identity):
    assert (
        await _uplink(lambda r: httpx.Response(500), identity).post_report(report)
        is False
    )


async def test_post_report_connect_error_is_false_not_raised(report, identity):
    def handler(request):
        raise httpx.ConnectError("down", request=request)

    assert await _uplink(handler, identity).post_report(report) is False


@pytest.mark.parametrize("status", [200, 201])
async def test_put_ui_bundle_success(status, identity):
    seen = []

    def handler(request):
        seen.append(request)
        return httpx.Response(status)

    assert await _uplink(handler, identity).put_ui_bundle("ab" * 32, b"tgz") is True
    assert seen[0].method == "PUT"
    assert seen[0].url.path == "/api/v1/ui-bundles/" + "ab" * 32
    assert seen[0].content == b"tgz"


async def test_put_ui_bundle_400_and_error_are_false(identity):
    assert (
        await _uplink(lambda r: httpx.Response(400), identity).put_ui_bundle("a", b"x")
        is False
    )

    def boom(request):
        raise httpx.ReadTimeout("slow", request=request)

    assert await _uplink(boom, identity).put_ui_bundle("a", b"x") is False


async def test_last_status_tracks_the_latest_response(identity):
    statuses = iter([401, 204])
    uplink = _uplink(lambda request: httpx.Response(next(statuses)), identity)
    assert uplink.last_status is None
    assert await uplink.delete_claim_code() is False
    assert uplink.last_status == 401
    assert await uplink.delete_claim_code() is True
    assert uplink.last_status == 204
    await uplink.aclose()
