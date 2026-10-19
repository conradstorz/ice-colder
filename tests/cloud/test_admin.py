from fastapi.testclient import TestClient

from tests.cloud.conftest import login, register, short
from tests.cloud.reports import NOW  # noqa: F401

HX = {"HX-Request": "true"}


def _admin(cloud):
    return login(cloud, "admin@example.com")


def test_non_admin_404(cloud_app, signer):
    repo = cloud_app.state.repo
    with TestClient(cloud_app, follow_redirects=False) as owner_c:
        with TestClient(cloud_app, follow_redirects=False) as victim_c:
            owner = login(owner_c, "user@example.com")
            victim = login(victim_c, "victim@example.com")
            fp = register(owner_c, signer)
            repo.claim_machine(fp, owner.id, cloud_app.state.now())
            base = f"/admin/machines/{short(signer)}/unbind"
            assert owner_c.get("/admin").status_code == 404
            r = owner_c.post(f"/admin/accounts/{victim.id}/suspend", headers=HX)
            assert r.status_code == 404
            assert repo.account(victim.id).suspended_at is None
            r = owner_c.post(f"/admin/accounts/{victim.id}/unsuspend", headers=HX)
            assert r.status_code == 404
            assert owner_c.post(base, headers=HX).status_code == 404
            assert owner_c.get(f"{base}/confirm", headers=HX).status_code == 404
            assert repo.machine(fp).owner_id == owner.id
            assert repo.account(victim.id).suspended_at is None


def test_unclaimed_machine_has_no_unbind_button(cloud, signer):
    register(cloud, signer, "m-free")
    _admin(cloud)
    r = cloud.get("/admin")
    assert "m-free" in r.text and "unclaimed" in r.text
    assert "/unbind" not in r.text


def test_unbind_already_unbound_is_harmless(cloud, signer):
    repo = cloud.app.state.repo
    fp = register(cloud, signer)
    _admin(cloud)
    r = cloud.post(f"/admin/machines/{short(signer)}/unbind", headers=HX)
    assert r.status_code == 200
    assert repo.machine(fp).owner_id is None


def test_admin_sees_unclaimed_and_claimed(cloud, signer):
    from tests.cloud.conftest import Signer

    owner = login(cloud, "owner@example.com")
    register(cloud, signer, "m-claimed")
    cloud.app.state.repo.claim_machine(
        signer.fingerprint, owner.id, cloud.app.state.now()
    )
    other = Signer()
    register(cloud, other, "m-free")
    _admin(cloud)
    r = cloud.get("/admin")
    assert r.status_code == 200
    assert "m-claimed" in r.text and "owner@example.com" in r.text
    assert "m-free" in r.text and "unclaimed" in r.text
    assert f"/m/{short(signer)}" in r.text


def test_unbind_clears_owner_and_memberships(cloud, signer):
    repo = cloud.app.state.repo
    owner = login(cloud, "owner@example.com")
    tech = login(cloud, "tech@example.com")
    fp = register(cloud, signer)
    repo.claim_machine(fp, owner.id, cloud.app.state.now())
    repo.grant_tech(tech.id, fp, owner.id, cloud.app.state.now())
    _admin(cloud)
    base = f"/admin/machines/{short(signer)}/unbind"
    assert cloud.post(base).status_code == 403  # no HX header
    confirm = cloud.get(f"{base}/confirm", headers=HX)
    assert "Confirm" in confirm.text and f'hx-post="{base}"' in confirm.text
    cancel = cloud.get(f"{base}/confirm?confirming=false", headers=HX)
    assert "Confirm" not in cancel.text and "Unbind" in cancel.text
    assert repo.machine(fp).owner_id == owner.id
    assert cloud.post(base, headers=HX).status_code == 200
    assert repo.machine(fp).owner_id is None
    assert repo.memberships(fp) == []


def test_suspend_signs_account_out_and_unsuspend_restores(cloud_app, signer):
    repo = cloud_app.state.repo
    with TestClient(cloud_app, follow_redirects=False) as victim_c:
        with TestClient(cloud_app, follow_redirects=False) as admin_c:
            victim = login(victim_c, "victim@example.com")
            login(admin_c, "admin@example.com")
            assert victim_c.get("/fleet").status_code == 200
            r = admin_c.post(f"/admin/accounts/{victim.id}/suspend", headers=HX)
            assert r.status_code == 200
            assert repo.account(victim.id).suspended_at is not None
            out = victim_c.get("/fleet")
            assert out.status_code == 303 and out.headers["location"].startswith(
                "/login"
            )
            r = admin_c.post(f"/admin/accounts/{victim.id}/unsuspend", headers=HX)
            assert r.status_code == 200
            assert repo.account(victim.id).suspended_at is None
            assert victim_c.get("/fleet").status_code == 200


def test_admin_cannot_assign_owner(cloud, signer):
    fp = register(cloud, signer)
    _admin(cloud)
    url = f"/admin/machines/{short(signer)}/owner"
    # POST reaches the portal catch-all (404); PUT has no handler anywhere (405).
    assert cloud.post(url, headers=HX).status_code == 404
    assert cloud.put(url, headers=HX).status_code == 405
    assert cloud.app.state.repo.machine(fp).owner_id is None


def test_admin_cannot_suspend_self(cloud):
    admin = _admin(cloud)
    r = cloud.post(f"/admin/accounts/{admin.id}/suspend", headers=HX)
    assert r.status_code == 400
    assert cloud.app.state.repo.account(admin.id).suspended_at is None
