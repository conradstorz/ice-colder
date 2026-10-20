import asyncio

import pytest
from fastapi.testclient import TestClient

from ice_cloud.app import create_app
from tests.cloud.conftest import (
    claim,
    login,
    make_settings,
    push,
    register,
    signed,
)
from tests.cloud.reports import make_report


def _claimed(cloud, signer, email="owner@example.com"):
    account = login(cloud, email)
    register(cloud, signer)
    claim(cloud, signer, account)
    return account


def _other_fault(report):
    fault = report.faults[0].model_copy(update={"key": "WTR-104", "code": "WTR-104"})
    return report.model_copy(update={"faults": [fault]})


def test_first_report_emails_owner_raised(cloud, signer, mailer):
    _claimed(cloud, signer)
    push(cloud, signer, make_report())
    assert len(mailer.sent) == 1
    assert "owner@example.com" in str(mailer.sent[0]["To"])


def test_second_identical_report_sends_nothing(cloud, signer, mailer):
    _claimed(cloud, signer)
    push(cloud, signer, make_report())
    push(cloud, signer, make_report(seq=2))
    assert len(mailer.sent) == 1


def test_cleared_bundled_into_next_raise(cloud, signer, mailer, clock):
    _claimed(cloud, signer)
    push(cloud, signer, make_report())
    clock.advance(1)
    push(cloud, signer, make_report(seq=2, faults=False))
    assert len(mailer.sent) == 1  # cleared is held by the cooldown
    clock.advance(700)
    push(cloud, signer, _other_fault(make_report(seq=3)))
    assert len(mailer.sent) == 2
    body = mailer.sent[1].get_body().get_content()
    assert "ICE-301" in body and "WTR-104" in body


def test_unclaimed_machine_no_events(cloud, signer, mailer):
    push(cloud, signer, make_report())
    assert mailer.sent == []


def test_online_after_sweep_offline(cloud, signer, mailer, clock):
    _claimed(cloud, signer)
    push(cloud, signer, make_report(faults=False))
    clock.advance(3600)
    asyncio.run(cloud.app.state.sweep.tick())
    assert len(mailer.sent) == 1
    clock.advance(7200)
    push(cloud, signer, make_report(seq=2, faults=False))
    assert len(mailer.sent) == 2
    assert "online" in str(mailer.sent[1]["Subject"]).lower()
    state = cloud.app.state.repo.alert_state(signer.fingerprint)
    assert state.offline_since is None


def test_alert_failure_still_204(cloud, signer, mailer, monkeypatch):
    _claimed(cloud, signer)

    async def boom(*a, **k):
        raise RuntimeError("boom")

    monkeypatch.setattr(cloud.app.state.alerts, "dispatch", boom)
    push(cloud, signer, make_report())
    assert cloud.app.state.store.latest(signer.fingerprint) is not None


@pytest.mark.parametrize("claimed", [False, True])
@pytest.mark.parametrize("smtp", [False, True])
def test_register_alerts_handled_requires_claim_and_smtp(
    tmp_path, provider, mailer, claimed, smtp, signer
):
    extra = (
        {"smtp_host": "smtp.example.com", "smtp_from": "a@example.com"} if smtp else {}
    )
    app = create_app(
        make_settings(tmp_path / "d", **extra),
        provider=provider,
        mailer=mailer,
        start_sweep=False,
    )
    with TestClient(app, follow_redirects=False) as c:
        if claimed:
            account = login(c, "owner@example.com")
            register(c, signer)
            claim(c, signer, account)
        from ice_contracts.cloud.identity import RegisterMachine

        body = RegisterMachine(public_key_b64=signer.public_key_b64, label="m")
        r = signed(
            c,
            signer,
            "POST",
            "/api/v1/machines/register",
            body.model_dump_json().encode(),
        )
        assert r.json()["alerts_handled"] is (claimed and smtp)


def test_0_2_0_report_still_processed(cloud, signer, mailer):
    report = make_report()
    assert report.contract == "0.2.0"
    _claimed(cloud, signer)
    push(cloud, signer, report)
    assert len(mailer.sent) == 1
