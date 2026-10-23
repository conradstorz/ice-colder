import asyncio
from datetime import timedelta

import httpx
import pytest
from fastapi.testclient import TestClient

from ice_cloud import machine_auth
from ice_cloud.machine_auth import InflightSlots, SeenSignatures
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


def test_replayed_report_is_200_and_store_unchanged(cloud, known, clock):
    body = make_report(seq=3).model_dump_json().encode()
    assert signed(cloud, known, "POST", _path(known), body).status_code == 200
    stored = cloud.app.state.store.latest(known.fingerprint)
    clock.advance(1)  # a fresh signature, not a byte-identical replay
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


def test_byte_identical_signed_request_is_rejected_on_replay(cloud, known):
    headers = known.headers("POST", _path(known), _body(), now=NOW)
    assert _send(cloud, headers, _path(known)).status_code == 200
    r = _send(cloud, headers, _path(known))
    assert r.status_code == 401 and r.content == b""


def test_replay_does_not_refresh_last_seen(cloud, known, clock):
    headers = known.headers("POST", _path(known), _body(), now=NOW)
    assert _send(cloud, headers, _path(known)).status_code == 200
    seen = cloud.app.state.repo.machine(known.fingerprint).last_seen_at
    clock.advance(30)
    assert _send(cloud, headers, _path(known)).status_code == 401
    assert cloud.app.state.repo.machine(known.fingerprint).last_seen_at == seen


def test_seen_signatures_expire_after_the_window():
    seen = SeenSignatures(window_seconds=10)
    assert seen.check_and_add("a", 0.0) is True
    assert seen.check_and_add("a", 9.0) is False
    assert seen.check_and_add("a", 11.0) is True  # expired, accepted again


def test_seen_signatures_evict_the_oldest_over_capacity():
    seen = SeenSignatures(window_seconds=1000, max_entries=2)
    assert seen.check_and_add("a", 0.0)
    assert seen.check_and_add("b", 1.0)
    assert seen.check_and_add("c", 2.0)  # evicts "a"
    assert seen.check_and_add("a", 3.0) is True
    assert seen.check_and_add("c", 4.0) is False


def test_replayed_claim_code_delete_does_not_wipe_a_newer_code(cloud, known):
    path = f"/api/v1/machines/{known.fingerprint}/claim-code"
    delete = known.headers("DELETE", path, b"", now=NOW)
    assert cloud.delete(path, headers=delete).status_code == 204
    body = b'{"claim_code_hash": "scrypt$new", "expires_at": "2026-10-20T14:05:00Z"}'
    assert signed(cloud, known, "POST", path, body).status_code == 204
    assert cloud.delete(path, headers=delete).status_code == 401
    row = cloud.app.state.repo.machine(known.fingerprint)
    assert row.claim_code_hash == "scrypt$new"


BUNDLE = "/api/v1/ui-bundles/" + "d" * 64


class BodyReads:
    """Counts ``http.request`` messages the app pulls from ``receive``."""

    def __init__(self, app) -> None:
        self.count = 0
        outer = self

        class Counter:
            def __init__(self, inner) -> None:
                self.inner = inner

            async def __call__(self, scope, receive, send):
                async def counting():
                    message = await receive()
                    if message["type"] == "http.request":
                        outer.count += 1
                    return message

                await self.inner(scope, counting, send)

        app.add_middleware(Counter)


@pytest.fixture
def watched(cloud_app):
    reads = BodyReads(cloud_app)
    with TestClient(cloud_app, follow_redirects=False) as client:
        yield client, reads


def _own(client, signer):
    register(client, signer)
    owner = client.app.state.repo.upsert_account_from_login(
        "sub-o", "o@example.com", "O", client.app.state.now()
    )
    assert client.app.state.repo.claim_machine(
        signer.fingerprint, owner.id, client.app.state.now()
    )


def _bundle_put(client, headers, body=b"x" * 1024):
    return client.put(BUNDLE, content=body, headers=headers)


def test_unknown_fingerprint_bundle_put_does_not_read_the_body(watched):
    client, reads = watched
    stranger = Signer()
    body = b"x" * (1024 * 1024)
    headers = stranger.headers("PUT", BUNDLE, body, now=client.app.state.now())
    r = _bundle_put(client, headers, body)
    assert r.status_code == 401 and r.content == b""
    assert reads.count == 0


def test_unclaimed_machine_bundle_put_does_not_read_the_body(watched):
    client, reads = watched
    s = Signer()
    register(client, s)
    reads.count = 0
    body = b"x" * (1024 * 1024)
    headers = s.headers("PUT", BUNDLE, body, now=client.app.state.now())
    r = _bundle_put(client, headers, body)
    assert r.status_code == 401 and r.content == b""
    assert reads.count == 0


def _bad_headers(client, s, body=b"x" * 1024):
    return s.headers("PUT", BUNDLE, b"other", now=client.app.state.now())


def test_bad_signature_backs_off_without_reading_the_body(watched, clock):
    client, reads = watched
    s = Signer()
    _own(client, s)
    reads.count = 0
    assert _bundle_put(client, _bad_headers(client, s)).status_code == 401
    assert reads.count > 0  # it has to be read to be verified
    reads.count = 0
    r = _bundle_put(client, _bad_headers(client, s))
    assert r.status_code == 401 and r.content == b""
    assert reads.count == 0
    clock.advance(2)
    reads.count = 0
    assert _bundle_put(client, _bad_headers(client, s)).status_code == 401
    assert reads.count > 0  # evaluated again after the delay


def test_good_signature_resets_back_off(watched, clock):
    client, reads = watched
    s = Signer()
    _own(client, s)
    assert _bundle_put(client, _bad_headers(client, s)).status_code == 401
    clock.advance(2)
    assert signed(client, s, "PUT", BUNDLE, b"junk").status_code == 400
    reads.count = 0
    assert _bundle_put(client, _bad_headers(client, s)).status_code == 401
    assert reads.count > 0  # a fresh first failure, not refused


def test_stale_and_unknown_attempts_do_not_count_toward_back_off(cloud, known, clock):
    path = _path(known)
    stale = known.headers("POST", path, _body(), now=NOW)
    clock.advance(signing.MAX_CLOCK_SKEW_SECONDS + 1)
    assert _send(cloud, stale, path).status_code == 401
    assert _send(cloud, stale, path).status_code == 401
    assert signed(cloud, known, "POST", path, _body()).status_code == 200
    stranger = Signer()
    unk = stranger.headers("POST", _path(stranger), _body(), now=cloud.app.state.now())
    assert _send(cloud, unk, _path(stranger)).status_code == 401
    assert _send(cloud, unk, _path(stranger)).status_code == 401
    backoff = cloud.app.state.backoff
    ip = "testclient"
    assert backoff.check("machine", stranger.fingerprint, ip, trusted=True) is None


def test_bad_registers_for_unknown_fingerprints_record_nothing(cloud, known):
    path = "/api/v1/machines/register"
    backoff = cloud.app.state.backoff
    ip = "testclient"
    strangers = [Signer() for _ in range(50)]
    for s in strangers:
        body = f'{{"public_key_b64": "{s.public_key_b64}", "label": "x"}}'.encode()
        headers = s.headers("POST", path, b"other", now=cloud.app.state.now())
        assert _send(cloud, headers, path, body).status_code == 401
    for s in strangers:
        assert backoff.check("machine", s.fingerprint, ip, trusted=True) is None
    # a known machine's bad register-path signature still records one failure
    body = f'{{"public_key_b64": "{known.public_key_b64}", "label": "x"}}'.encode()
    bad = known.headers("POST", path, b"other", now=cloud.app.state.now())
    assert _send(cloud, bad, path, body).status_code == 401
    assert backoff.check("machine", known.fingerprint, ip, trusted=True) is not None


def _noncanonical_variant(signature: str) -> str:
    """Another header string that base64-decodes to the same signature bytes."""
    import base64

    assert signature.endswith("==")
    raw = base64.b64decode(signature, validate=True)
    alphabet = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789+/"
    for char in alphabet:
        variant = signature[:-3] + char + "=="
        if variant != signature and base64.b64decode(variant) == raw:
            return variant
    raise AssertionError("no alternate encoding found")


def test_reencoded_signature_cannot_replay_a_claim_code_delete(cloud, known):
    path = f"/api/v1/machines/{known.fingerprint}/claim-code"
    delete = known.headers("DELETE", path, b"", now=NOW)
    assert cloud.delete(path, headers=delete).status_code == 204
    body = b'{"claim_code_hash": "scrypt$new", "expires_at": "2026-10-20T14:05:00Z"}'
    assert signed(cloud, known, "POST", path, body).status_code == 204
    variant = {
        **delete,
        signing.HEADER_SIGNATURE: _noncanonical_variant(
            delete[signing.HEADER_SIGNATURE]
        ),
    }
    assert cloud.delete(path, headers=variant).status_code == 401
    row = cloud.app.state.repo.machine(known.fingerprint)
    assert row.claim_code_hash == "scrypt$new"


# --- C14: process-wide limit on in-flight bundle bodies -----------------------


async def _gated_body(body: bytes, started: asyncio.Event, release: asyncio.Event):
    started.set()
    await asyncio.wait_for(release.wait(), timeout=5)
    yield body


async def _unreadable_body():
    raise AssertionError("the refused request's body must not be read")
    yield b""  # pragma: no cover


def _async_client(app):
    return httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://testserver"
    )


async def test_bundle_bodies_beyond_the_limit_get_503_before_the_body_is_read(
    cloud_app,
):
    cloud_app.state.bundle_bodies = InflightSlots(1)
    s = Signer()
    with TestClient(cloud_app) as sync:
        _own(sync, s)
    now = cloud_app.state.now()
    started, release = asyncio.Event(), asyncio.Event()
    async with _async_client(cloud_app) as client:
        first = asyncio.create_task(
            client.put(
                BUNDLE,
                content=_gated_body(b"junk-1", started, release),
                headers=s.headers("PUT", BUNDLE, b"junk-1", now=now),
            )
        )
        await asyncio.wait_for(started.wait(), timeout=5)
        second = await client.put(
            BUNDLE,
            content=_unreadable_body(),
            headers=s.headers("PUT", BUNDLE, b"junk-2", now=now),
        )
        assert second.status_code == 503
        assert second.headers["retry-after"] == "30"
        assert second.content == b""
        release.set()
        assert (await first).status_code == 400  # valid signature, not a bundle
        third = await client.put(
            BUNDLE,
            content=b"junk-3",
            headers=s.headers("PUT", BUNDLE, b"junk-3", now=now),
        )
        assert third.status_code == 400


async def test_a_bundle_put_failing_after_acquisition_frees_its_slot(cloud_app, clock):
    cloud_app.state.bundle_bodies = InflightSlots(1)
    s = Signer()
    with TestClient(cloud_app) as sync:
        _own(sync, s)
    async with _async_client(cloud_app) as client:
        bad = await client.put(
            BUNDLE,
            content=b"junk",
            headers=s.headers("PUT", BUNDLE, b"other", now=cloud_app.state.now()),
        )
        assert bad.status_code == 401
        clock.advance(2)  # past the back-off the bad signature recorded
        good = await client.put(
            BUNDLE,
            content=b"junk",
            headers=s.headers("PUT", BUNDLE, b"junk", now=cloud_app.state.now()),
        )
        assert good.status_code == 400


async def test_the_slot_is_freed_when_the_handler_raises(cloud_app, monkeypatch):
    cloud_app.state.bundle_bodies = InflightSlots(1)
    s = Signer()
    with TestClient(cloud_app) as sync:
        _own(sync, s)

    def boom(digest, data):
        raise RuntimeError("extraction exploded")

    monkeypatch.setattr(cloud_app.state.registry, "register", boom)
    now = cloud_app.state.now()
    async with _async_client(cloud_app) as client:
        with pytest.raises(RuntimeError):
            await client.put(
                BUNDLE,
                content=b"junk-1",
                headers=s.headers("PUT", BUNDLE, b"junk-1", now=now),
            )
        with pytest.raises(RuntimeError):  # reaches the handler again: not 503
            await client.put(
                BUNDLE,
                content=b"junk-2",
                headers=s.headers("PUT", BUNDLE, b"junk-2", now=now),
            )


def test_json_routes_are_not_counted_against_the_bundle_slots(cloud, known):
    cloud.app.state.bundle_bodies = InflightSlots(0)
    r = signed(cloud, known, "POST", _path(known), _body())
    assert r.status_code == 200


# --- C15: a stalled bundle body is cut off, not allowed to pin a slot ---------


async def _stalled_body():
    yield b"junk-"
    await asyncio.Event().wait()  # trickle: never completes
    yield b"never"  # pragma: no cover


async def test_a_stalled_bundle_body_times_out_with_408_and_frees_its_slot(
    cloud_app, monkeypatch
):
    # Deadline 0: expires at the first real suspension, i.e. the stalled read.
    monkeypatch.setattr(machine_auth, "BUNDLE_BODY_READ_SECONDS", 0)
    cloud_app.state.bundle_bodies = InflightSlots(1)
    s = Signer()
    with TestClient(cloud_app) as sync:
        _own(sync, s)
    now = cloud_app.state.now()
    async with _async_client(cloud_app) as client:
        stalled = await client.put(
            BUNDLE,
            content=_stalled_body(),
            headers=s.headers("PUT", BUNDLE, b"junk-never", now=now),
        )
        assert stalled.status_code == 408
        assert stalled.content == b""
        assert cloud_app.state.bundle_bodies.in_use == 0
        monkeypatch.undo()
        real = await client.put(
            BUNDLE,
            content=b"junk-2",
            headers=s.headers("PUT", BUNDLE, b"junk-2", now=now),
        )
        assert real.status_code == 400  # valid signature, not a bundle: past the slot
