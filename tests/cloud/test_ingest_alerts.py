import asyncio
import threading

import pytest
from fastapi.testclient import TestClient

from ice_cloud.app import create_app
from tests.cloud.conftest import (
    claim,
    drain_alerts,
    login,
    select,
    short,
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
    repo = cloud.app.state.repo
    assert mailer.sent == []
    assert repo.alert_events(signer.fingerprint, limit=10) == []
    assert repo.alert_state(signer.fingerprint) is None


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


def test_alert_failure_still_200(cloud, signer, mailer, monkeypatch):
    _claimed(cloud, signer)

    async def boom(*a, **k):
        raise RuntimeError("boom")

    monkeypatch.setattr(cloud.app.state.alerts, "dispatch", boom)
    push(cloud, signer, make_report())
    repo = cloud.app.state.repo
    assert repo.alert_state(signer.fingerprint) is not None
    push(cloud, signer, make_report(seq=2))
    events = repo.alert_events(signer.fingerprint, limit=10)
    assert [e.kind for e in events] == ["raised"]


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


def test_resale_hides_previous_owner_history(cloud, signer, mailer, clock):
    repo = cloud.app.state.repo
    first = _claimed(cloud, signer, "first@example.com")
    push(cloud, signer, make_report())
    clock.advance(1)
    push(cloud, signer, make_report(seq=2, faults=False))  # a clear for the old owner
    assert repo.alert_events(signer.fingerprint, limit=10)
    clock.advance(1000)
    second = login(cloud, "second@example.com")
    assert second.id != first.id
    claim(cloud, signer, second)
    assert repo.alert_events(signer.fingerprint, limit=10) == []
    clock.advance(1000)
    sent_before = len(mailer.sent)
    push(cloud, signer, _other_fault(make_report(seq=3)))
    new = mailer.sent[sent_before:]
    assert [str(m["To"]) for m in new] == ["second@example.com"]
    assert "Cleared since your last email" not in new[0].get_body().get_content()
    select(cloud, signer)
    page = cloud.get(f"/machines/{short(signer)}/alerts")
    assert page.status_code == 200
    assert "WTR-104" in page.text and "ICE-301" not in page.text
    assert "first@example.com" not in page.text


@pytest.fixture
def smtp_cloud(tmp_path, clock, provider, mailer):
    app = create_app(
        make_settings(
            tmp_path / "d", smtp_host="smtp.example.com", smtp_from="a@example.com"
        ),
        now=clock,
        provider=provider,
        mailer=mailer,
        start_sweep=False,
    )
    with TestClient(app, follow_redirects=False) as c:
        yield c


def _flag(cloud, signer, report) -> bool:
    register(cloud, signer, report.machine_id)
    r = signed(
        cloud,
        signer,
        "POST",
        f"/api/v1/machines/{signer.fingerprint}/report",
        report.model_dump_json().encode(),
    )
    assert r.status_code == 200
    drain_alerts(cloud)
    return r.json()["alerts_handled"]


def test_report_flag_false_without_smtp(cloud, signer):
    _claimed(cloud, signer)
    assert _flag(cloud, signer, make_report()) is False


def test_report_flag_false_while_unclaimed_then_true_once_claimed(smtp_cloud, signer):
    assert _flag(smtp_cloud, signer, make_report()) is False
    account = login(smtp_cloud, "owner@example.com")
    claim(smtp_cloud, signer, account)
    assert _flag(smtp_cloud, signer, make_report(seq=2)) is True


def test_report_flag_false_after_unbind(smtp_cloud, signer):
    _claimed(smtp_cloud, signer)
    assert _flag(smtp_cloud, signer, make_report()) is True
    smtp_cloud.app.state.repo.unbind_machine(signer.fingerprint)
    assert _flag(smtp_cloud, signer, make_report(seq=2)) is False


def test_report_flag_false_with_no_unmuted_recipient(smtp_cloud, signer):
    owner = _claimed(smtp_cloud, signer)
    repo = smtp_cloud.app.state.repo
    repo.set_muted(owner.id, signer.fingerprint, True)
    assert _flag(smtp_cloud, signer, make_report()) is False
    repo.set_muted(owner.id, signer.fingerprint, False)
    assert _flag(smtp_cloud, signer, make_report(seq=2)) is True
    repo.set_suspended(owner.id, smtp_cloud.app.state.now())
    assert _flag(smtp_cloud, signer, make_report(seq=3)) is False


def test_report_flag_tracks_delivery_health(smtp_cloud, signer, mailer, clock):
    _claimed(smtp_cloud, signer)
    assert _flag(smtp_cloud, signer, make_report()) is True
    mailer.fail_next = RuntimeError("smtp down")
    clock.advance(700)
    # Dispatch runs after the response, so this report still sees a healthy log...
    assert _flag(smtp_cloud, signer, _other_fault(make_report(seq=2))) is True
    # ...and the next one sees the failure.
    clock.advance(10)
    assert _flag(smtp_cloud, signer, _other_fault(make_report(seq=3))) is False
    clock.advance(1000)  # past the retry backoff: this pass delivers it
    assert _flag(smtp_cloud, signer, _other_fault(make_report(seq=4))) is False
    assert _flag(smtp_cloud, signer, _other_fault(make_report(seq=5))) is True


def test_report_does_not_wait_for_slow_mail(tmp_path, clock, provider, signer):
    release = threading.Event()

    class Slow:
        async def send(self, msg):
            while not release.is_set():
                await asyncio.sleep(0.01)

    app = create_app(
        make_settings(
            tmp_path / "d", smtp_host="smtp.example.com", smtp_from="a@example.com"
        ),
        now=clock,
        provider=provider,
        mailer=Slow(),
        start_sweep=False,
    )
    with TestClient(app, follow_redirects=False) as c:
        _claimed(c, signer)
        r = signed(
            c,
            signer,
            "POST",
            f"/api/v1/machines/{signer.fingerprint}/report",
            make_report().model_dump_json().encode(),
        )
        assert r.status_code == 200  # returned although the send is still blocked
        release.set()
        drain_alerts(c)
    assert c.app.state.repo.last_sent_at(
        signer.fingerprint, c.app.state.repo.machine(signer.fingerprint).owner_id
    )
