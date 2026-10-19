from datetime import timedelta

from ice_common.secrets import hash_secret
from ice_contracts.cloud.identity import ClaimCodeNotice
from tests.cloud.conftest import login, register, short, signed

HX = {"HX-Request": "true"}
CODE = "12345678"


def open_claim(cloud, signer, *, code=CODE, expires_in: float | None = 600):
    fp = register(cloud, signer)
    expires = (
        None
        if expires_in is None
        else cloud.app.state.now() + timedelta(seconds=expires_in)
    )
    notice = ClaimCodeNotice(claim_code_hash=hash_secret(code), expires_at=expires)
    r = signed(
        cloud,
        signer,
        "POST",
        f"/api/v1/machines/{fp}/claim-code",
        notice.model_dump_json().encode(),
    )
    assert r.status_code == 204
    return fp


def post_claim(cloud, signer, code=CODE, fingerprint=None):
    return cloud.post(
        "/claim",
        data={"fingerprint": fingerprint or short(signer), "code": code},
        headers=HX,
    )


def test_requires_sign_in(cloud, signer):
    open_claim(cloud, signer)
    assert cloud.get("/claim").status_code == 303
    r = post_claim(cloud, signer)
    assert r.status_code == 303 and r.headers["location"].startswith("/login")


def test_form_renders_for_signed_in(cloud):
    login(cloud, "a@example.com")
    r = cloud.get("/claim")
    assert r.status_code == 200
    assert "xxxx-xxxx-xxxx-xxxx" in r.text and 'inputmode="numeric"' in r.text


def test_wrong_code_403_and_then_backoff_429_with_retry_after(cloud, signer, clock):
    open_claim(cloud, signer)
    login(cloud, "a@example.com")
    assert post_claim(cloud, signer, "00000000").status_code == 403
    r = post_claim(cloud, signer, CODE)
    assert r.status_code == 429
    assert int(r.headers["Retry-After"]) >= 1
    clock.advance(5)
    assert post_claim(cloud, signer, CODE).status_code == 303


def test_no_open_claim_same_403_message(cloud, signer):
    fp = register(cloud, signer)
    assert fp
    login(cloud, "a@example.com")
    wrong_machine = post_claim(cloud, signer, CODE)
    unknown = post_claim(cloud, signer, CODE, fingerprint="0000-0000-0000-0000")
    garbage = post_claim(cloud, signer, CODE, fingerprint="not a fingerprint")
    assert wrong_machine.status_code == 403
    assert unknown.status_code == 403 or unknown.status_code == 429
    assert garbage.status_code == 403
    for r in (wrong_machine, garbage):
        assert "No claim is open for this machine, or the code is wrong." in r.text


def test_expired_code_403(cloud, signer, clock):
    open_claim(cloud, signer, expires_in=60)
    login(cloud, "a@example.com")
    clock.advance(120)
    assert post_claim(cloud, signer).status_code == 403
    assert cloud.app.state.repo.machine(signer.fingerprint).owner_id is None


def test_setup_code_without_expiry_claims(cloud, signer, clock):
    open_claim(cloud, signer, expires_in=None)
    account = login(cloud, "a@example.com")
    clock.advance(86400 * 30)
    assert post_claim(cloud, signer).status_code == 303
    assert cloud.app.state.repo.machine(signer.fingerprint).owner_id == account.id


def test_success_sets_owner_drops_techs_clears_hash_and_selects(cloud, signer):
    fp = open_claim(cloud, signer)
    repo = cloud.app.state.repo
    now = cloud.app.state.now()
    owner = repo.upsert_account_from_login("sub-old", "old@example.com", "Old", now)
    tech = repo.upsert_account_from_login("sub-tech", "tech@example.com", "Tech", now)
    repo.claim_machine(fp, owner.id, cloud.app.state.now())
    repo.grant_tech(tech.id, fp, owner.id, now)
    repo.set_claim_code(fp, hash_secret(CODE), None)
    new = login(cloud, "new@example.com")
    r = post_claim(cloud, signer)
    assert r.status_code == 303 and r.headers["location"] == "/"
    assert "ice_cloud_machine" in r.headers["set-cookie"]
    machine = repo.machine(fp)
    assert machine.owner_id == new.id
    assert machine.claim_code_hash is None and machine.claim_code_expires_at is None
    assert repo.access_for(tech.id, fp) is None


def test_old_owner_then_gets_404_on_select(cloud, signer):
    fp = open_claim(cloud, signer)
    repo = cloud.app.state.repo
    old = login(cloud, "old@example.com")
    repo.claim_machine(fp, old.id, cloud.app.state.now())
    repo.set_claim_code(fp, hash_secret(CODE), None)
    assert cloud.post(f"/fleet/select/{short(signer)}").status_code == 303
    cloud.cookies.clear()
    login(cloud, "new@example.com")
    assert post_claim(cloud, signer).status_code == 303
    cloud.cookies.clear()
    login(cloud, "old@example.com")
    assert cloud.post(f"/fleet/select/{short(signer)}").status_code == 404


def test_claim_before_delete_notice_and_after(cloud, signer):
    fp = open_claim(cloud, signer)
    account = login(cloud, "a@example.com")
    assert post_claim(cloud, signer).status_code == 303
    r = signed(cloud, signer, "DELETE", f"/api/v1/machines/{fp}/claim-code")
    assert r.status_code == 204
    assert cloud.app.state.repo.machine(fp).owner_id == account.id

    other = type(signer)()
    fp2 = open_claim(cloud, other)
    r = signed(cloud, other, "DELETE", f"/api/v1/machines/{fp2}/claim-code")
    assert r.status_code == 204
    assert post_claim(cloud, other).status_code == 403
