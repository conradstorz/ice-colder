import re

import pytest

from tests.cloud.conftest import Signer, claim, login, push, register, short
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


def _owner_fleet(cloud, clock):
    silent, online, solo, tech = Signer(), Signer(), Signer(), None
    push(cloud, silent, make_report(machine_id="m-a-silent", interval=10))
    clock.advance(31)
    push(cloud, online, make_report(machine_id="m-a-online", interval=60))
    push(cloud, solo, make_report(machine_id="m-b-solo", interval=60))
    owner_a = login(cloud, "owner-a@example.com")
    owner_b = login(cloud, "owner-b@example.com")
    claim(cloud, silent, owner_a)
    claim(cloud, online, owner_a)
    claim(cloud, solo, owner_b)
    tech = login(cloud, "tech@example.com")
    cloud.app.state.repo.grant_tech(
        tech.id, online.fingerprint, owner_a.id, cloud.app.state.now()
    )
    return owner_a, owner_b, tech


def _owner_rows(text):
    """Each owners-table body row as its list of <td> texts (tags stripped)."""
    rows = []
    for tr in re.findall(r"<tr[^>]*>(.*?)</tr>", text, re.S):
        cells = re.findall(r"<td[^>]*>(.*?)</td>", tr, re.S)
        if cells:
            rows.append(
                [re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", c)).strip() for c in cells]
            )
    return rows


def test_owners_page_counts(cloud, clock):
    _owner_fleet(cloud, clock)
    _admin(cloud)
    r = cloud.get("/admin/owners")
    assert r.status_code == 200
    rows = _owner_rows(r.text)
    assert len(rows) == 2
    a, b = rows
    assert a[0].startswith("owner-a@example.com")
    assert b[0].startswith("owner-b@example.com")
    # Machines, Online, Silent, Never, Faulted, Techs
    assert a[1:7] == ["2", "1", "1", "0", "1", "1"]
    assert b[1:7] == ["1", "1", "0", "0", "1", "0"]
    assert 'href="/admin/machines?owner=' in r.text


def test_owners_footer_counts_idle_accounts(cloud, clock):
    _owner_fleet(cloud, clock)
    _admin(cloud)
    accounts = cloud.app.state.repo.all_accounts()
    idle = len(accounts) - 2
    assert idle == 2  # the admin login and the tech own nothing
    text = cloud.get("/admin/owners").text
    assert f"2 owners · {idle} accounts own nothing" in text


def test_owners_non_admin_404(cloud):
    login(cloud, "someone@example.com")
    assert cloud.get("/admin/owners").status_code == 404


def _fault_report(machine_id, gate, code):
    base = make_report(machine_id=machine_id)
    fault = base.faults[0].model_copy(update={"gate": gate, "code": code})
    return base.model_copy(update={"faults": [fault]})


@pytest.mark.parametrize(
    ("gate", "tone"),
    [("fulfillment", "amber-600"), ("safety", "red-600"), ("alert", "gray-600")],
)
def test_machines_table_marks_top_fault_code(cloud, clock, gate, tone):
    push(cloud, Signer(), _fault_report("m-fault", gate, "ICE-301"))
    _admin(cloud)
    text = cloud.get("/admin/machines").text
    assert re.search(rf'<span class="[^"]*text-{tone}[^"]*">\s*1 · ICE-301', text)


def test_machines_table_payment_off(cloud, clock):
    health = make_report(faults=False).model_dump(mode="json")["health"]
    health["payment_enabled"] = False
    push(cloud, Signer(), make_report(machine_id="m-off", faults=False, health=health))
    _admin(cloud)
    assert ">Off<" in cloud.get("/admin/machines").text


def _alert_rows(text):
    return re.findall(
        r'<a href="(/machines/[^"]+/alerts)"[^>]*>\s*([^<]+?)\s*</a>', text
    )


def test_fleet_alerts_newest_first_with_machine_link(cloud, clock):
    a, b = Signer(), Signer()
    owner = login(cloud, "alerts-owner@example.com")
    register(cloud, a, "m-alert-a")
    register(cloud, b, "m-alert-b")
    claim(cloud, a, owner)
    claim(cloud, b, owner)
    push(cloud, a, make_report(machine_id="m-alert-a", faults=True, seq=1))
    clock.advance(60)
    push(cloud, a, make_report(machine_id="m-alert-a", faults=False, seq=2))
    clock.advance(60)
    push(cloud, b, make_report(machine_id="m-alert-b", faults=True, seq=1))
    _admin(cloud)
    r = cloud.get("/admin/alerts")
    assert r.status_code == 200
    links = _alert_rows(r.text)
    assert links[0] == (f"/machines/{short(b)}/alerts", "m-alert-b")
    assert (f"/machines/{short(a)}/alerts", "m-alert-a") in links
    assert "raised" in r.text and "cleared" in r.text
    assert " UTC" in r.text


def test_fleet_alerts_empty(cloud):
    _admin(cloud)
    assert "No alerts yet." in cloud.get("/admin/alerts").text


def test_fleet_alerts_non_admin_404(cloud):
    login(cloud, "someone@example.com")
    assert cloud.get("/admin/alerts").status_code == 404


def test_fleet_page_links_dashboard_for_admin_only(cloud):
    login(cloud, "plain-owner@example.com")
    assert 'href="/admin"' not in cloud.get("/fleet").text
    _admin(cloud)
    assert 'href="/admin"' in cloud.get("/fleet").text
