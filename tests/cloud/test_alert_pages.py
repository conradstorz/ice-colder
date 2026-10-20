"""Alert history page, mute toggle and the fleet 24 h count (alerts spec 5)."""

from __future__ import annotations

from datetime import timedelta

from ice_cloud.store.repository import AlertEventRow
from tests.cloud.conftest import login, own_and_select, short
from tests.cloud.reports import make_report

HX = {"HX-Request": "true"}


def _row(fp, at, n=0, kind="raised"):
    return AlertEventRow(
        fingerprint=fp,
        kind=kind,
        at=at,
        key=f"WTR-{n}:machine",
        code=f"WTR-{n}",
        severity="critical",
        gate="safety",
        description=f"Event number {n}",
        sku=None,
        product=None,
    )


def _setup(cloud, signer):
    owner = own_and_select(cloud, signer, make_report())
    return owner, short(signer)


def _tech(cloud, signer, owner, email="tech@example.com"):
    tech = login(cloud, email)
    cloud.app.state.repo.grant_tech(
        tech.id, signer.fingerprint, owner.id, cloud.app.state.now()
    )
    return tech


def test_alerts_page_lists_newest_first_with_deliveries(cloud, signer):
    owner, sh = _setup(cloud, signer)
    repo = cloud.app.state.repo
    now = cloud.app.state.now()
    stored = repo.record_alert_events(
        [
            _row(signer.fingerprint, now - timedelta(hours=2), 101),
            _row(signer.fingerprint, now - timedelta(hours=1), 102),
        ]
    )
    d_sent = repo.create_delivery(
        signer.fingerprint, owner.id, [stored[1].id], "pending", now
    )
    repo.mark_delivery(d_sent.id, status="sent", now=now)
    tech = _tech(cloud, signer, owner)
    repo.create_delivery(signer.fingerprint, tech.id, [stored[0].id], "muted", now)
    d_bad = repo.create_delivery(
        signer.fingerprint, owner.id, [stored[0].id], "pending", now
    )
    repo.mark_delivery(d_bad.id, status="failed", now=now, error="smtp down")
    login(cloud, "owner@example.com")
    r = cloud.get(f"/machines/{sh}/alerts")
    assert r.status_code == 200
    assert "failed: smtp down" in r.text
    assert "sent " in r.text
    assert r.text.index("Event number 102") < r.text.index("Event number 101")
    assert "owner@example.com" in r.text
    assert "tech@example.com" in r.text
    assert "muted" in r.text


def test_alerts_page_404_for_non_member(cloud, signer):
    _setup(cloud, signer)
    sh = short(signer)
    login(cloud, "stranger@example.com")
    assert cloud.get(f"/machines/{sh}/alerts").status_code == 404


def test_alerts_page_limit_200(cloud, signer):
    _owner, sh = _setup(cloud, signer)
    now = cloud.app.state.now()
    cloud.app.state.repo.record_alert_events(
        [
            _row(signer.fingerprint, now - timedelta(minutes=300 - i), 1000 + i)
            for i in range(205)
        ]
    )
    text = cloud.get(f"/machines/{sh}/alerts").text
    assert text.count("Event number ") == 200
    assert "Event number 1204" in text
    assert "Event number 1004" not in text


def _muted(cloud, account, signer):
    return cloud.app.state.repo.is_muted(account.id, signer.fingerprint)


def _mute(cloud, sh, account, value="1", headers=HX):
    return cloud.post(
        f"/machines/{sh}/people/{account.id}/mute",
        data={"muted": value},
        headers=headers,
    )


def test_owner_mutes_tech(cloud, signer):
    owner, sh = _setup(cloud, signer)
    tech = _tech(cloud, signer, owner)
    login(cloud, "owner@example.com")
    r = _mute(cloud, sh, tech)
    assert r.status_code == 200
    assert _muted(cloud, tech, signer)
    assert "tech@example.com" in r.text
    assert _mute(cloud, sh, tech, "0").status_code == 200
    assert not _muted(cloud, tech, signer)


def test_tech_mutes_self(cloud, signer):
    owner, sh = _setup(cloud, signer)
    tech = _tech(cloud, signer, owner)
    assert _mute(cloud, sh, tech).status_code == 200
    assert _muted(cloud, tech, signer)


def test_tech_cannot_mute_other_403(cloud, signer):
    owner, sh = _setup(cloud, signer)
    tech = _tech(cloud, signer, owner)
    assert _mute(cloud, sh, owner).status_code == 403
    assert not _muted(cloud, owner, signer)
    assert tech


def test_mute_requires_htmx(cloud, signer):
    owner, sh = _setup(cloud, signer)
    r = _mute(cloud, sh, owner, headers={})
    assert r.status_code == 403 or r.status_code == 400
    assert not _muted(cloud, owner, signer)


def test_fleet_shows_24h_count(cloud, signer):
    _owner, _sh = _setup(cloud, signer)
    now = cloud.app.state.now()
    cloud.app.state.repo.record_alert_events(
        [
            _row(signer.fingerprint, now - timedelta(hours=1), 1),
            _row(signer.fingerprint, now - timedelta(hours=2), 2),
            _row(signer.fingerprint, now - timedelta(hours=30), 3),
        ]
    )
    text = cloud.get("/fleet").text
    assert "2 alerts / 24 h" in text
    assert f"/machines/{short(signer)}/alerts" in text


def test_people_page_shows_owner_row_with_mute(cloud, signer):
    owner, sh = _setup(cloud, signer)
    text = cloud.get(f"/machines/{sh}/people").text
    assert "owner@example.com" in text
    assert f"/machines/{sh}/people/{owner.id}/mute" in text
    assert f"/machines/{sh}/alerts" in text
