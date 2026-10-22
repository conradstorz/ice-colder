from tests.cloud.conftest import Signer, claim, login, push
from tests.cloud.reports import make_report


def _admin(cloud):
    return login(cloud, "admin@example.com")


def _two_machines(cloud, clock):
    quiet = Signer()
    busy = Signer()
    push(cloud, quiet, make_report(machine_id="m-quiet", interval=10))
    push(cloud, busy, make_report(machine_id="m-busy", interval=60))
    clock.advance(31)
    return quiet, busy


def test_admin_landing_links_to_levels(cloud):
    _admin(cloud)
    r = cloud.get("/admin")
    assert r.status_code == 200
    assert 'href="/admin/machines"' in r.text


def test_machines_filter_status_silent(cloud, clock):
    _two_machines(cloud, clock)
    _admin(cloud)
    r = cloud.get("/admin/machines?status=silent")
    assert r.status_code == 200
    assert "m-quiet" in r.text
    assert "m-busy" not in r.text


def test_machines_filter_claimed_no(cloud, clock):
    quiet, busy = _two_machines(cloud, clock)
    owner = login(cloud, "owner@example.com")
    claim(cloud, busy, owner)
    _admin(cloud)
    r = cloud.get("/admin/machines?claimed=no")
    assert "m-quiet" in r.text
    assert "m-busy" not in r.text


def test_machines_filter_unknown_value_ignored(cloud, clock):
    _two_machines(cloud, clock)
    _admin(cloud)
    plain = cloud.get("/admin/machines")
    bogus = cloud.get("/admin/machines?status=bogus")
    assert bogus.status_code == 200
    for label in ("m-quiet", "m-busy"):
        assert label in plain.text and label in bogus.text


def test_machines_filter_owner_names_owner(cloud, clock):
    quiet, busy = _two_machines(cloud, clock)
    owner = login(cloud, "owner@example.com")
    claim(cloud, busy, owner)
    _admin(cloud)
    r = cloud.get(f"/admin/machines?owner={owner.id}")
    assert "owner@example.com" in r.text
    assert "m-busy" in r.text
    assert "m-quiet" not in r.text


def test_machines_breadcrumb_for_filter(cloud, clock):
    _two_machines(cloud, clock)
    _admin(cloud)
    r = cloud.get("/admin/machines?status=silent")
    assert 'aria-current="page">Silent<' in r.text
