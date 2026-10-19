"""People page and invitations (identity spec 3 and 6.3)."""

from __future__ import annotations

from datetime import timedelta

from ice_contracts.cloud import signing
from tests.cloud.conftest import (
    Signer,
    claim,
    login,
    own_and_select,
    push,
    short,
)
from tests.cloud.reports import make_report

HX = {"HX-Request": "true"}


def _owner_setup(cloud, signer):
    owner = own_and_select(cloud, signer, make_report())
    return owner, short(signer)


def _invite(cloud, sh, email="tech@example.com", scope="machine"):
    return cloud.post(
        f"/machines/{sh}/people/invite",
        data={"email": email, "scope": scope},
        headers=HX,
    )


def _token(cloud, owner, fp):
    pending = cloud.app.state.repo.invitations_by(owner.id, fp, cloud.app.state.now())
    assert pending
    return pending[-1].token


def test_people_owner_only_404_for_tech_and_admin(cloud, signer):
    owner, sh = _owner_setup(cloud, signer)
    assert cloud.get(f"/machines/{sh}/people").status_code == 200
    tech = login(cloud, "tech@example.com")
    cloud.app.state.repo.grant_tech(
        tech.id, signer.fingerprint, owner.id, cloud.app.state.now()
    )
    assert cloud.get(f"/machines/{sh}/people").status_code == 404
    login(cloud, "admin@example.com")
    assert cloud.get(f"/machines/{sh}/people").status_code == 404


def test_invite_creates_pending_with_link(cloud, signer):
    owner, sh = _owner_setup(cloud, signer)
    r = _invite(cloud, sh, "  Tech@Example.COM ")
    assert r.status_code == 200
    token = _token(cloud, owner, signer.fingerprint)
    assert f"/invite/{token}" in r.text
    invite = cloud.app.state.repo.invitation(token)
    assert invite.email == "tech@example.com"
    assert invite.scope == (signer.fingerprint,)
    assert f"/invite/{token}" in cloud.get(f"/machines/{sh}/people").text


def test_accept_matching_email_grants_machine_scope(cloud, signer):
    owner, sh = _owner_setup(cloud, signer)
    _invite(cloud, sh)
    token = _token(cloud, owner, signer.fingerprint)
    tech = login(cloud, "tech@example.com")
    page = cloud.get(f"/invite/{token}")
    assert page.status_code == 200 and "Accept invitation" in page.text
    r = cloud.post(f"/invite/{token}", headers=HX)
    assert r.status_code == 303 and r.headers["location"] == "/fleet"
    repo = cloud.app.state.repo
    assert repo.access_for(tech.id, signer.fingerprint) == "tech"
    assert cloud.get(f"/invite/{token}").status_code == 404


def test_accept_fleet_scope_grants_current_machines_only(cloud, signer):
    owner, sh = _owner_setup(cloud, signer)
    _invite(cloud, sh, scope="fleet")
    token = _token(cloud, owner, signer.fingerprint)
    second = Signer()
    push(cloud, second, make_report(machine_id="m-0043"))
    claim(cloud, second, owner)
    tech = login(cloud, "tech@example.com")
    assert cloud.post(f"/invite/{token}", headers=HX).status_code == 303
    third = Signer()
    login(cloud, "owner@example.com")
    push(cloud, third, make_report(machine_id="m-0044"))
    claim(cloud, third, owner)
    repo = cloud.app.state.repo
    assert repo.access_for(tech.id, signer.fingerprint) == "tech"
    assert repo.access_for(tech.id, second.fingerprint) == "tech"
    assert repo.access_for(tech.id, third.fingerprint) is None


def test_accept_wrong_email_refused(cloud, signer):
    owner, sh = _owner_setup(cloud, signer)
    _invite(cloud, sh)
    token = _token(cloud, owner, signer.fingerprint)
    other = login(cloud, "other@example.com")
    page = cloud.get(f"/invite/{token}")
    assert page.status_code == 200
    assert "different address" in page.text and "Accept invitation" not in page.text
    assert cloud.post(f"/invite/{token}", headers=HX).status_code == 403
    assert cloud.app.state.repo.access_for(other.id, signer.fingerprint) is None
    assert cloud.app.state.repo.invitation(token).accepted_by is None


def test_expired_token_404(cloud, signer, clock):
    owner, sh = _owner_setup(cloud, signer)
    _invite(cloud, sh)
    token = _token(cloud, owner, signer.fingerprint)
    login(cloud, "tech@example.com")
    clock.value = clock.value + timedelta(days=8)
    assert cloud.get(f"/invite/{token}").status_code == 404
    assert cloud.post(f"/invite/{token}", headers=HX).status_code == 404
    assert cloud.get("/invite/unknown-token").status_code == 404


def test_revoke_two_tap_and_removes(cloud, signer):
    owner, sh = _owner_setup(cloud, signer)
    tech = login(cloud, "tech@example.com")
    repo = cloud.app.state.repo
    repo.grant_tech(tech.id, signer.fingerprint, owner.id, cloud.app.state.now())
    login(cloud, "owner@example.com")
    base = f"/machines/{sh}/people/{tech.id}/revoke"
    assert "Revoke" in cloud.get(f"/machines/{sh}/people").text
    first = cloud.get(base + "/confirm")
    assert "Confirm revoke" in first.text and "Cancel" in first.text
    cancelled = cloud.get(base + "/confirm?confirming=false")
    assert "Confirm revoke" not in cancelled.text and "Revoke" in cancelled.text
    assert repo.access_for(tech.id, signer.fingerprint) == "tech"
    assert cloud.post(base).status_code == 403
    assert cloud.post(base, headers=HX).status_code == 200
    assert repo.access_for(tech.id, signer.fingerprint) is None
    assert cloud.post(base, headers=HX).status_code == 404


def test_cancel_invitation(cloud, signer):
    owner, sh = _owner_setup(cloud, signer)
    _invite(cloud, sh)
    token = _token(cloud, owner, signer.fingerprint)
    r = cloud.post(f"/machines/{sh}/people/invitations/{token}/cancel", headers=HX)
    assert r.status_code == 200
    assert cloud.app.state.repo.invitation(token) is None
    again = cloud.post(f"/machines/{sh}/people/invitations/{token}/cancel", headers=HX)
    assert again.status_code == 404


def test_invite_bad_email_error_message(cloud, signer):
    owner, sh = _owner_setup(cloud, signer)
    for bad in ("nobody", "a@@b.com", "a b@c.com", "@x.com", "x@"):
        r = _invite(cloud, sh, bad)
        assert r.status_code == 400, bad
        assert "valid email" in r.text
    assert (
        cloud.app.state.repo.invitations_by(
            owner.id, signer.fingerprint, cloud.app.state.now()
        )
        == []
    )
    assert signing.short_fingerprint(signer.fingerprint) == sh
