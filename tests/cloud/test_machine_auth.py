from datetime import timedelta

import pytest

from ice_contracts.cloud import signing
from tests.cloud.conftest import Signer, register, signed
from tests.cloud.reports import NOW, make_report


def _path(signer):
    return f"/api/v1/machines/{signer.fingerprint}/report"


def _body():
    return make_report().model_dump_json().encode()


def _send(cloud, headers, path, body=None):
    body = _body() if body is None else body
    headers = {**headers, "content-type": "application/json"}
    return cloud.post(path, content=body, headers=headers)


@pytest.fixture
def known(cloud, signer):
    register(cloud, signer)
    return signer


@pytest.mark.parametrize(
    "missing",
    [signing.HEADER_MACHINE, signing.HEADER_TIMESTAMP, signing.HEADER_SIGNATURE],
)
def test_each_missing_header_is_401_with_empty_body(cloud, known, missing):
    headers = known.headers("POST", _path(known), _body(), now=NOW)
    del headers[missing]
    r = _send(cloud, headers, _path(known))
    assert r.status_code == 401 and r.content == b""


def test_stale_timestamp_is_401(cloud, known, clock):
    headers = known.headers("POST", _path(known), _body(), now=NOW)
    clock.advance(signing.MAX_CLOCK_SKEW_SECONDS + 1)
    assert _send(cloud, headers, _path(known)).status_code == 401


def test_unknown_fingerprint_on_report_is_401(cloud):
    stranger = Signer()
    headers = stranger.headers("POST", _path(stranger), _body(), now=NOW)
    assert _send(cloud, headers, _path(stranger)).status_code == 401


def test_bad_signature_is_401(cloud, known):
    headers = known.headers("POST", _path(known), b"{}", now=NOW)  # other body
    assert _send(cloud, headers, _path(known)).status_code == 401


def test_other_machines_key_cannot_sign_for_a_registered_machine(cloud, known):
    other = Signer()
    headers = other.headers("POST", _path(known), _body(), now=NOW)
    headers[signing.HEADER_MACHINE] = known.fingerprint
    assert _send(cloud, headers, _path(known)).status_code == 401


def test_path_fingerprint_mismatch_is_401(cloud, known):
    other = Signer()
    register(cloud, other)
    path = _path(other)
    headers = known.headers("POST", path, _body(), now=NOW)
    assert _send(cloud, headers, path).status_code == 401


def test_register_header_not_matching_body_key_is_401(cloud):
    a, b = Signer(), Signer()
    body = f'{{"public_key_b64": "{a.public_key_b64}", "label": "x"}}'.encode()
    path = "/api/v1/machines/register"
    headers = b.headers("POST", path, body, now=NOW)
    assert _send(cloud, headers, path, body).status_code == 401


def test_register_twice_updates_label_and_stays_unclaimed(cloud, signer):
    fp = register(cloud, signer, "first")
    assert cloud.app.state.repo.machine(fp).label == "first"
    assert register(cloud, signer, "second") == fp
    row = cloud.app.state.repo.machine(fp)
    assert row.label == "second" and row.owner_id is None
    body = f'{{"public_key_b64": "{signer.public_key_b64}", "label": "third"}}'
    r = signed(cloud, signer, "POST", "/api/v1/machines/register", body.encode())
    assert r.status_code == 200 and r.json() == {
        "fingerprint": fp,
        "claimed": False,
        "alerts_handled": False,
    }


def test_report_after_register_moves_last_seen(cloud, known, clock):
    before = cloud.app.state.repo.machine(known.fingerprint).last_seen_at
    clock.advance(30)
    assert signed(cloud, known, "POST", _path(known), _body()).status_code == 200
    after = cloud.app.state.repo.machine(known.fingerprint).last_seen_at
    assert after == before + timedelta(seconds=30)


def test_replayed_report_is_200_and_store_unchanged(cloud, known):
    body = make_report(seq=3).model_dump_json().encode()
    assert signed(cloud, known, "POST", _path(known), body).status_code == 200
    stored = cloud.app.state.store.latest(known.fingerprint)
    assert signed(cloud, known, "POST", _path(known), body).status_code == 200
    assert cloud.app.state.store.latest(known.fingerprint) == stored


def test_claim_code_notice_set_then_cleared(cloud, known):
    path = f"/api/v1/machines/{known.fingerprint}/claim-code"
    body = b'{"claim_code_hash": "scrypt$abc", "expires_at": "2026-10-20T14:05:00Z"}'
    assert signed(cloud, known, "POST", path, body).status_code == 204
    row = cloud.app.state.repo.machine(known.fingerprint)
    assert row.claim_code_hash == "scrypt$abc"
    assert row.claim_code_expires_at == NOW + timedelta(days=7)
    assert signed(cloud, known, "DELETE", path).status_code == 204
    row = cloud.app.state.repo.machine(known.fingerprint)
    assert row.claim_code_hash is None and row.claim_code_expires_at is None


def test_claim_code_unsigned_is_401(cloud, known):
    path = f"/api/v1/machines/{known.fingerprint}/claim-code"
    assert cloud.delete(path).status_code == 401


def test_chunked_body_over_limit_is_413_before_auth(cloud, cloud_app, signer):
    path = "/api/v1/machines/register"
    big = b"x" * (256 * 1024 + 1)
    headers = signer.headers("POST", path, big, now=NOW)

    def body():
        for i in range(0, len(big), 64 * 1024):
            yield big[i : i + 64 * 1024]

    r = cloud.post(
        path, content=body(), headers={**headers, "content-type": "application/json"}
    )
    assert r.status_code == 413 and r.content == b""
    assert cloud_app.state.repo.machine(signer.fingerprint) is None


def test_unsigned_register_creates_no_row(cloud, cloud_app, signer):
    body = f'{{"public_key_b64": "{signer.public_key_b64}", "label": "x"}}'.encode()
    r = cloud.post(
        "/api/v1/machines/register",
        content=body,
        headers={"content-type": "application/json"},
    )
    assert r.status_code == 401
    assert cloud_app.state.repo.machine(signer.fingerprint) is None


def test_malformed_timestamp_is_401(cloud, known):
    headers = known.headers("POST", _path(known), _body(), now=NOW)
    headers[signing.HEADER_TIMESTAMP] = "not-a-time"
    assert _send(cloud, headers, _path(known)).status_code == 401
