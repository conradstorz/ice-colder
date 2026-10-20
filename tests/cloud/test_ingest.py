import pytest
from fastapi.testclient import TestClient

from ice_cloud.app import create_app
from ice_ui.bundle import build_bundle, ui_digest
from tests.cloud.conftest import make_settings, register, signed
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


def test_oversize_bundle_is_rejected_before_extraction(client, machine, tmp_path):
    big = b"\0" * (20 * 1024 * 1024 + 1)
    r = _put(client, machine, "d" * 64, big)
    assert r.status_code == 413 and r.content == b""
    assert not (tmp_path / "ui").exists()


def test_oversize_bundle_without_content_length_is_cut_off(client, machine):
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
