from tests.cloud.conftest import Signer, claim, login, push, register
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


def _seed_fleet(cloud, clock):
    silent = Signer()
    push(cloud, silent, make_report(machine_id="m-silent", interval=10))
    clock.advance(31)
    one, two, loose, never = Signer(), Signer(), Signer(), Signer()
    push(cloud, one, make_report(machine_id="m-one", interval=60))
    push(cloud, two, make_report(machine_id="m-two", interval=60))
    push(cloud, loose, make_report(machine_id="m-loose", interval=60))
    register(cloud, never, "m-never")
    claim(cloud, silent, login(cloud, "owner1@example.com"))
    claim(cloud, one, login(cloud, "owner1@example.com"))
    claim(cloud, two, login(cloud, "owner2@example.com"))


def _headline(text, label, value):
    start = text.index(label, text.find('id="fleet-overview"'))
    return f">{value}<" in text[start : start + 400]


def test_overview_shows_four_headlines(cloud, clock):
    _seed_fleet(cloud, clock)
    _admin(cloud)
    r = cloud.get("/admin")
    assert r.status_code == 200
    assert _headline(r.text, "Machines", 5)
    assert _headline(r.text, "Owners", 2)
    assert _headline(r.text, "Unclaimed calling in", 1)
    assert _headline(r.text, "Silent", 1)


def test_overview_attention_lists_silent_before_unclaimed(cloud, clock):
    _seed_fleet(cloud, clock)
    _admin(cloud)
    r = cloud.get("/admin")
    assert r.text.index("m-silent") < r.text.index("m-loose")


def test_overview_fragment_is_bare_div(cloud, clock):
    _seed_fleet(cloud, clock)
    _admin(cloud)
    r = cloud.get("/admin/overview", headers={"HX-Request": "true"})
    assert r.status_code == 200
    assert r.text.lstrip().startswith('<div id="fleet-overview"')
    assert 'hx-target="this"' in r.text
    assert "<html" not in r.text
    assert _headline(r.text, "Machines", 5)
    assert _headline(r.text, "Silent", 1)


def test_overview_non_admin_404(cloud):
    login(cloud, "someone@example.com")
    assert cloud.get("/admin").status_code == 404
    assert cloud.get("/admin/overview").status_code == 404


def test_overview_empty_fleet(cloud):
    _admin(cloud)
    r = cloud.get("/admin")
    assert r.status_code == 200
    assert "Nothing needs attention." in r.text
    assert _headline(r.text, "Machines", 0)


def test_overview_software_panel_counts_builds(cloud):
    for name, build in (("m-a", "aaa"), ("m-b", "aaa"), ("m-c", "bbb")):
        push(cloud, Signer(), make_report(machine_id=name, edge_build=build))
    _admin(cloud)
    r = cloud.get("/admin")
    start = r.text.index("aaa")
    assert ">2<" in r.text[start : start + 300]


def test_overview_nav_links_every_admin_level(cloud, clock):
    _seed_fleet(cloud, clock)
    _admin(cloud)
    page = cloud.get("/admin").text
    for url in ("/admin/machines", "/admin/owners", "/admin/accounts", "/admin/alerts"):
        assert f'href="{url}"' in page
    frag = cloud.get("/admin/overview", headers={"HX-Request": "true"})
    assert 'aria-label="Admin levels"' not in frag.text
