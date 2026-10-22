import asyncio

import httpx
import pytest
from fastapi.testclient import TestClient

from ice_cloud.app import create_app
from ice_cloud.ingest import (
    MAX_NEW_REGISTRATIONS_PER_HOUR,
    MAX_UNCLAIMED_REPORT_BYTES,
    UNCLAIMED_MACHINE_TTL_DAYS,
    SlidingWindow,
)
from ice_contracts.cloud.identity import RegisterMachine
from ice_ui.bundle import build_bundle, ui_digest
from tests.cloud.conftest import Signer, make_settings, register, signed
from tests.cloud.reports import make_report


@pytest.fixture
def client(tmp_path):
    app = create_app(make_settings(tmp_path))
    with TestClient(app) as c:
        yield c


@pytest.fixture
def machine(client, signer):
    register(client, signer)
    return signer


@pytest.fixture
def owned(client, machine):
    """A registered machine that has been claimed (bundles are accepted)."""
    owner = client.app.state.repo.upsert_account_from_login(
        "sub-o", "o@example.com", "O", client.app.state.now()
    )
    assert client.app.state.repo.claim_machine(
        machine.fingerprint, owner.id, client.app.state.now()
    )
    return machine


def _post(client, signer, report):
    return signed(
        client,
        signer,
        "POST",
        f"/api/v1/machines/{signer.fingerprint}/report",
        report.model_dump_json().encode(),
    )


def _put(client, signer, digest, data):
    return signed(client, signer, "PUT", f"/api/v1/ui-bundles/{digest}", data)


def test_report_is_accepted_and_stored(client, machine, tmp_path):
    assert _post(client, machine, make_report()).status_code == 200
    assert (tmp_path / "machines" / f"{machine.fingerprint}.json").is_file()


def test_body_machine_id_is_a_label_not_checked_against_the_path(client, machine):
    assert _post(client, machine, make_report(machine_id="anything")).status_code == 200


def test_invalid_body_is_422(client, machine):
    r = signed(
        client,
        machine,
        "POST",
        f"/api/v1/machines/{machine.fingerprint}/report",
        b'{"machine_id": "m-1"}',
    )
    assert r.status_code == 422


def test_bundle_put_201_then_200_then_400_for_corrupt_bytes(client, owned):
    machine = owned
    data = build_bundle()
    digest = ui_digest()
    assert _put(client, machine, digest, data).status_code == 201
    assert _put(client, machine, digest, data).status_code == 200
    bad = _put(client, machine, "a" * 64, b"corrupt")
    assert bad.status_code == 400
    assert "detail" in bad.json()


def test_bundle_with_wrong_digest_in_url_is_400(client, owned):
    machine = owned
    r = _put(client, machine, "b" * 64, build_bundle())
    assert r.status_code == 400
    assert "digest" in r.json()["detail"]


def test_oversize_bundle_is_rejected_before_extraction(client, owned, tmp_path):
    big = b"\0" * (20 * 1024 * 1024 + 1)
    r = _put(client, owned, "d" * 64, big)
    assert r.status_code == 413 and r.content == b""
    assert not (tmp_path / "ui").exists()


def test_oversize_bundle_without_content_length_is_cut_off(client, owned):
    machine = owned
    big = b"\0" * (21 * 1024 * 1024)
    path = f"/api/v1/ui-bundles/{'d' * 64}"
    headers = machine.headers("PUT", path, big, now=client.app.state.now())

    def body():
        for i in range(21):
            yield big[i * 1024 * 1024 : (i + 1) * 1024 * 1024]

    r = client.put(path, content=body(), headers=headers)
    assert r.status_code == 413 and r.content == b""


def _streamed_put(client, signer, size):
    data = b"\0" * size
    path = f"/api/v1/ui-bundles/{'d' * 64}"
    headers = signer.headers("PUT", path, data, now=client.app.state.now())

    def body():
        step = 1024 * 1024
        for i in range(0, size, step):
            yield data[i : i + step]

    return client.put(path, content=body(), headers=headers)


def test_bundle_route_allows_up_to_bundle_limit_but_not_over(client, owned):
    machine = owned
    limit = 20 * 1024 * 1024
    at = _streamed_put(client, machine, limit)
    assert at.status_code == 400  # past the size cap; rejected as a corrupt bundle
    assert "too large" not in at.json()["detail"]
    over = _streamed_put(client, machine, limit + 1)
    assert over.status_code == 413


def test_unsigned_bundle_put_is_401(client):
    r = client.put(f"/api/v1/ui-bundles/{ui_digest()}", content=build_bundle())
    assert r.status_code == 401 and r.content == b""


def test_bundle_put_from_unclaimed_machine_is_401_and_stores_nothing(
    client, machine, tmp_path
):
    r = _put(client, machine, ui_digest(), build_bundle())
    assert r.status_code == 401 and r.content == b""
    assert not (tmp_path / "ui").exists()


def test_fifth_distinct_bundle_digest_is_409_but_repeats_are_fine(
    client, owned, tmp_path
):
    import gzip
    import io
    import json
    import tarfile
    import shutil

    from ice_ui.bundle import INSTALLED_ROOT

    def variant(n):
        root = tmp_path / f"copy{n}"
        shutil.copytree(INSTALLED_ROOT / "templates", root / "templates")
        shutil.copytree(INSTALLED_ROOT / "static", root / "static")
        (root / "static" / "v.txt").write_text(str(n))
        data = build_bundle(root)
        with tarfile.open(fileobj=io.BytesIO(gzip.decompress(data))) as tf:
            return json.loads(tf.extractfile("manifest.json").read())["digest"], data

    bundles = [variant(n) for n in range(5)]
    for digest, data in bundles[:4]:
        assert _put(client, owned, digest, data).status_code == 201
    digest, data = bundles[4]
    r = _put(client, owned, digest, data)
    assert r.status_code == 409 and r.content == b""
    assert not (tmp_path / "ui" / digest).exists()
    first_digest, first_data = bundles[0]
    assert _put(client, owned, first_digest, first_data).status_code == 200


def _variants(tmp_path, count):
    import gzip
    import io
    import json
    import shutil
    import tarfile

    from ice_ui.bundle import INSTALLED_ROOT

    out = []
    for n in range(count):
        root = tmp_path / f"copy{n}"
        shutil.copytree(INSTALLED_ROOT / "templates", root / "templates")
        shutil.copytree(INSTALLED_ROOT / "static", root / "static")
        (root / "static" / "v.txt").write_text(str(n))
        data = build_bundle(root)
        with tarfile.open(fileobj=io.BytesIO(gzip.decompress(data))) as tf:
            out.append(
                (json.loads(tf.extractfile("manifest.json").read())["digest"], data)
            )
    return out


def test_bundle_cap_counts_only_the_last_30_days(tmp_path, signer, clock):
    app = create_app(make_settings(tmp_path / "cloud"), now=clock)
    with TestClient(app) as c:
        register(c, signer)
        owner = app.state.repo.upsert_account_from_login(
            "sub-o", "o@example.com", "O", clock()
        )
        app.state.repo.claim_machine(signer.fingerprint, owner.id, clock())
        bundles = _variants(tmp_path, 6)
        for digest, data in bundles[:4]:
            assert _put(c, signer, digest, data).status_code == 201
        assert _put(c, signer, *bundles[4]).status_code == 409
        clock.advance(31 * 86400)
        assert _put(c, signer, *bundles[4]).status_code == 201


def test_digest_the_cloud_already_holds_skips_the_cap(tmp_path, signer, clock):
    from tests.cloud.conftest import Signer

    app = create_app(make_settings(tmp_path / "cloud"), now=clock)
    with TestClient(app) as c:
        other = Signer()
        bundles = _variants(tmp_path, 5)
        for s in (signer, other):
            register(c, s)
            owner = app.state.repo.upsert_account_from_login(
                f"sub-{s.fingerprint[:6]}",
                f"{s.fingerprint[:6]}@example.com",
                "O",
                clock(),
            )
            app.state.repo.claim_machine(s.fingerprint, owner.id, clock())
        assert _put(c, signer, *bundles[4]).status_code == 201
        for digest, data in bundles[:4]:
            assert _put(c, other, digest, data).status_code == 201
        # `other` is at its cap, but a digest the cloud already holds is no new storage.
        assert _put(c, other, *bundles[4]).status_code == 200


def _register_request(client, signer, label="m-new"):
    body = RegisterMachine(public_key_b64=signer.public_key_b64, label=label)
    return signed(
        client,
        signer,
        "POST",
        "/api/v1/machines/register",
        body.model_dump_json().encode(),
    )


@pytest.fixture
def timed(tmp_path, clock):
    app = create_app(make_settings(tmp_path / "cloud"), now=clock)
    with TestClient(app) as c:
        yield c


def test_eleventh_new_machine_in_an_hour_from_one_address_is_throttled(timed, clock):
    signers = [Signer() for _ in range(MAX_NEW_REGISTRATIONS_PER_HOUR + 1)]
    for s in signers[:-1]:
        assert _register_request(timed, s).status_code == 200
    r = _register_request(timed, signers[-1])
    assert r.status_code == 429 and r.content == b""
    assert 0 < float(r.headers["retry-after"]) <= 3600
    # A known fingerprint re-registers freely and does not count.
    assert _register_request(timed, signers[0]).status_code == 200
    clock.advance(3601)
    assert _register_request(timed, signers[-1]).status_code == 200
    assert timed.app.state.repo.machine(signers[-1].fingerprint) is not None


def test_sliding_window_prunes_and_drops_empty_keys():
    w = SlidingWindow(2, 10)
    w.record("a", 0)
    w.record("a", 1)
    assert w.allow("a", 5) == 5
    assert w.allow("a", 10) is None
    assert w.allow("a", 12) is None
    w.record("b", 0)
    w.reset("b")
    assert w.allow("b", 0) is None


async def test_concurrent_new_registrations_respect_the_cap(tmp_path, clock):
    settings = make_settings(tmp_path / "cloud", max_unclaimed_machines=2)
    app = create_app(settings, now=clock, start_sweep=False)
    transport = httpx.ASGITransport(app=app)
    signers = [Signer() for _ in range(8)]

    async def one(signer):
        body = (
            RegisterMachine(public_key_b64=signer.public_key_b64, label="m")
            .model_dump_json()
            .encode()
        )
        headers = signer.headers("POST", "/api/v1/machines/register", body, now=clock())
        headers["content-type"] = "application/json"
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
            return (
                await c.post("/api/v1/machines/register", content=body, headers=headers)
            ).status_code

    codes = await asyncio.gather(*(one(s) for s in signers))
    assert sorted(codes) == [200, 200] + [429] * 6
    assert app.state.repo.count_unclaimed_machines() == 2


def test_unclaimed_machine_cap_blocks_new_but_not_known(tmp_path, clock):
    settings = make_settings(tmp_path / "cloud", max_unclaimed_machines=1)
    app = create_app(settings, now=clock)
    with TestClient(app) as c:
        first, second = Signer(), Signer()
        assert _register_request(c, first).status_code == 200
        clock.advance(5)
        r = _register_request(c, second)
        assert r.status_code == 429 and r.headers["retry-after"] == "3600"
        assert _register_request(c, first).status_code == 200
        repo = app.state.repo
        owner = repo.upsert_account_from_login("sub-o", "o@example.com", "O", clock())
        assert repo.claim_machine(first.fingerprint, owner.id, clock())
        assert _register_request(c, second).status_code == 200


def _padded(report):
    return report.model_copy(
        update={
            "health": report.health.model_copy(update={"issues": ["x" * 100] * 300})
        }
    )


def test_unclaimed_report_over_the_cap_is_413_and_not_stored(client, machine, tmp_path):
    padded = _padded(make_report())
    assert len(padded.model_dump_json().encode()) > MAX_UNCLAIMED_REPORT_BYTES
    r = _post(client, machine, padded)
    assert r.status_code == 413 and r.content == b""
    assert not (tmp_path / "machines" / f"{machine.fingerprint}.json").exists()


def test_claimed_machine_may_send_the_same_large_report(client, owned):
    assert _post(client, owned, _padded(make_report())).status_code == 200


async def test_sweep_purges_stale_unclaimed_machines_and_snapshots(tmp_path, clock):
    app = create_app(make_settings(tmp_path / "cloud"), now=clock, start_sweep=False)
    repo = app.state.repo
    old, claimed, fresh = Signer(), Signer(), Signer()
    t0 = clock()
    for s in (old, claimed):
        repo.register_machine(s.fingerprint, s.public_key_b64, "m", t0)
    owner = repo.upsert_account_from_login("sub-o", "o@example.com", "O", t0)
    assert repo.claim_machine(claimed.fingerprint, owner.id, t0)
    for s in (old, claimed):
        app.state.store.save(s.fingerprint, make_report())
    clock.advance(UNCLAIMED_MACHINE_TTL_DAYS * 86400 + 60)
    repo.register_machine(fresh.fingerprint, fresh.public_key_b64, "m", clock())
    app.state.store.save(fresh.fingerprint, make_report())
    await app.state.sweep.tick()
    assert repo.machine(old.fingerprint) is None
    assert app.state.store.latest(old.fingerprint) is None
    assert repo.machine(claimed.fingerprint) is not None
    assert app.state.store.latest(claimed.fingerprint) is not None
    assert repo.machine(fresh.fingerprint) is not None
    assert app.state.store.latest(fresh.fingerprint) is not None


async def test_failing_purge_does_not_stop_the_sweep(tmp_path, clock):
    app = create_app(make_settings(tmp_path / "cloud"), now=clock, start_sweep=False)
    repo = app.state.repo

    def boom(_cutoff):
        raise RuntimeError("db down")

    repo.purge_unclaimed_machines = boom
    checked = []

    def claimed_machines_with_state():
        checked.append(True)
        return []

    repo.claimed_machines_with_state = claimed_machines_with_state
    assert await app.state.sweep.tick() == []
    assert checked == [True]
