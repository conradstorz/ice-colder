import asyncio
from datetime import UTC, datetime, timedelta

import pytest

from ice_common.config_model import ConfigModel
from ice_edge.uplink.identity import MachineIdentity
from ice_edge.uplink.loop import run_uplink
from ice_ui.bundle import ui_digest


class FakeUplink:
    def __init__(
        self,
        bundle_results=(True,),
        register_results=(True,),
        claim_results=(True,),
        delete_results=(True,),
        on_claim=None,
        claimed=None,
    ):
        self.on_claim = on_claim
        self.claimed = claimed
        self.bundle_results = list(bundle_results)
        self.register_results = list(register_results)
        self.claim_results = list(claim_results)
        self.delete_results = list(delete_results)
        self.events: list[tuple] = []
        self.last_status: int | None = 200
        self.bundle_status = None
        self.report_results: list[bool] = []
        self.alerts_handled = False
        self.last_register = None
        self.last_report = None
        self.report_flags: list[bool | None] = []

    async def register(self, label):
        self.events.append(("register", label))
        if self.alerts_handled or self.claimed is not None:
            from ice_contracts.cloud.identity import RegisterResponse

            self.last_register = RegisterResponse(
                fingerprint="f",
                claimed=True if self.claimed is None else self.claimed,
                alerts_handled=self.alerts_handled,
            )
        return self.register_results.pop(0) if self.register_results else True

    async def post_claim_code(self, notice):
        self.events.append(("claim", notice.claim_code_hash))
        if self.on_claim is not None:
            self.on_claim()
        return self.claim_results.pop(0) if self.claim_results else True

    async def delete_claim_code(self):
        self.events.append(("delete",))
        return self.delete_results.pop(0) if self.delete_results else True

    async def put_ui_bundle(self, digest, data):
        self.events.append(("put", digest))
        ok = self.bundle_results.pop(0) if self.bundle_results else True
        self.last_status = 200 if ok else self.bundle_status
        return ok

    async def post_report(self, report):
        self.events.append(("post", report.seq, report.boot_id))
        from ice_contracts.cloud.identity import ReportResponse

        flag = self.report_flags.pop(0) if self.report_flags else self.alerts_handled
        self.last_report = None if flag is None else ReportResponse(alerts_handled=flag)
        if self.report_results:
            ok = self.report_results.pop(0)
            self.last_status = 200 if ok else 401
            return ok
        return True


def _sleeper(config, *, stop_after, set_interval_at=None):
    calls: list[float] = []

    async def sleep(seconds):
        calls.append(seconds)
        if set_interval_at is not None and len(calls) == set_interval_at[0]:
            config.cloud.report_interval_seconds = set_interval_at[1]
        if len(calls) >= stop_after:
            raise asyncio.CancelledError

    return sleep, calls


@pytest.fixture
def identity(tmp_path):
    return MachineIdentity.load_or_create(tmp_path / "identity.json")


def _now():
    return datetime(2026, 10, 13, tzinfo=UTC)


async def test_one_bundle_put_then_reports_with_rising_seq(machine_fake_time, identity):
    machine, _ = machine_fake_time
    cfg = ConfigModel()
    uplink = FakeUplink()
    sleep, calls = _sleeper(cfg, stop_after=3)

    with pytest.raises(asyncio.CancelledError):
        await run_uplink(machine, cfg, uplink, sleep=sleep, now=_now, identity=identity)

    assert uplink.events[1] == ("put", ui_digest())
    posts = [e for e in uplink.events if e[0] == "post"]
    assert [p[1] for p in posts] == [0, 1, 2]
    assert len({p[2] for p in posts}) == 1
    assert [e[0] for e in uplink.events].count("put") == 1
    assert calls == [60, 60, 60]


async def test_bundle_is_built_off_the_event_loop(
    machine_fake_time, identity, monkeypatch
):
    from ice_edge.uplink import loop as loop_module

    on_loop = []

    def spy(real):
        def wrapper(*a, **k):
            try:
                asyncio.get_running_loop()
                on_loop.append(True)
            except RuntimeError:
                on_loop.append(False)
            return real(*a, **k)

        return wrapper

    monkeypatch.setattr(loop_module, "build_bundle", spy(loop_module.build_bundle))
    monkeypatch.setattr(loop_module, "ui_digest", spy(loop_module.ui_digest))
    machine, _ = machine_fake_time
    cfg = ConfigModel()
    sleep, _calls = _sleeper(cfg, stop_after=1)

    with pytest.raises(asyncio.CancelledError):
        await run_uplink(
            machine, cfg, FakeUplink(), sleep=sleep, now=_now, identity=identity
        )

    assert on_loop == [False, False]


async def test_interval_change_is_honoured_next_pass(machine_fake_time, identity):
    machine, _ = machine_fake_time
    cfg = ConfigModel()
    sleep, calls = _sleeper(cfg, stop_after=3, set_interval_at=(1, 10))

    with pytest.raises(asyncio.CancelledError):
        await run_uplink(
            machine, cfg, FakeUplink(), sleep=sleep, now=_now, identity=identity
        )

    assert calls == [60, 10, 10]


async def test_bundle_put_retried_each_interval_until_accepted(
    machine_fake_time, identity
):
    machine, _ = machine_fake_time
    cfg = ConfigModel()
    uplink = FakeUplink(bundle_results=[False, False, True])
    sleep, calls = _sleeper(cfg, stop_after=3)

    with pytest.raises(asyncio.CancelledError):
        await run_uplink(machine, cfg, uplink, sleep=sleep, now=_now, identity=identity)

    assert [e[0] for e in uplink.events] == [
        "register",
        "put",
        "post",
        "put",
        "post",
        "put",
        "post",
    ]
    assert calls == [60, 60, 60]


async def test_register_precedes_everything_and_blocks_reports(
    machine_fake_time, identity
):
    machine, _ = machine_fake_time
    cfg = ConfigModel()
    identity.set_notice("h1", None)
    uplink = FakeUplink(register_results=[False, False, True])
    sleep, _calls = _sleeper(cfg, stop_after=3)

    with pytest.raises(asyncio.CancelledError):
        await run_uplink(machine, cfg, uplink, sleep=sleep, now=_now, identity=identity)

    kinds = [e[0] for e in uplink.events]
    assert kinds == ["register", "register", "register", "claim", "put", "post"]
    assert uplink.events[0] == ("register", cfg.machine_id)


async def test_unacked_notice_sent_each_pass_until_2xx_then_acked(
    machine_fake_time, identity
):
    machine, _ = machine_fake_time
    cfg = ConfigModel()
    identity.set_notice("h1", "2026-10-20T00:00:00+00:00")
    uplink = FakeUplink(claim_results=[False, True])
    sleep, _calls = _sleeper(cfg, stop_after=4)

    with pytest.raises(asyncio.CancelledError):
        await run_uplink(machine, cfg, uplink, sleep=sleep, now=_now, identity=identity)

    assert [e for e in uplink.events if e[0] == "claim"] == [
        ("claim", "h1"),
        ("claim", "h1"),
    ]
    assert identity.notice is not None
    assert identity.notice.acked is True


async def test_delete_notice_when_hash_none(machine_fake_time, identity):
    machine, _ = machine_fake_time
    cfg = ConfigModel()
    identity.set_notice(None, None)
    uplink = FakeUplink(delete_results=[False, True])
    sleep, _calls = _sleeper(cfg, stop_after=3)

    with pytest.raises(asyncio.CancelledError):
        await run_uplink(machine, cfg, uplink, sleep=sleep, now=_now, identity=identity)

    assert [e[0] for e in uplink.events].count("delete") == 2
    assert not [e for e in uplink.events if e[0] == "claim"]
    assert identity.notice is not None
    assert identity.notice.acked is True


async def test_bundle_still_once(machine_fake_time, identity):
    machine, _ = machine_fake_time
    cfg = ConfigModel()
    uplink = FakeUplink()
    sleep, _calls = _sleeper(cfg, stop_after=4)

    with pytest.raises(asyncio.CancelledError):
        await run_uplink(machine, cfg, uplink, sleep=sleep, now=_now, identity=identity)

    assert [e[0] for e in uplink.events].count("put") == 1
    assert [e[0] for e in uplink.events].count("post") == 4


async def test_notice_replaced_mid_flight_is_not_acked(machine_fake_time, identity):
    machine, _ = machine_fake_time
    cfg = ConfigModel()
    identity.set_notice("h1", None)
    uplink = FakeUplink(on_claim=lambda: identity.set_notice("h2", None))
    sleep, _calls = _sleeper(cfg, stop_after=1)

    with pytest.raises(asyncio.CancelledError):
        await run_uplink(machine, cfg, uplink, sleep=sleep, now=_now, identity=identity)

    assert identity.notice is not None
    assert identity.notice.claim_code_hash == "h2"
    assert identity.notice.acked is False

    uplink.on_claim = None
    sleep2, _ = _sleeper(cfg, stop_after=1)
    with pytest.raises(asyncio.CancelledError):
        await run_uplink(
            machine, cfg, uplink, sleep=sleep2, now=_now, identity=identity
        )

    assert [e for e in uplink.events if e[0] == "claim"] == [
        ("claim", "h1"),
        ("claim", "h2"),
    ]
    assert identity.notice.acked is True


async def test_401_on_report_triggers_a_fresh_registration(machine_fake_time, identity):
    machine, _ = machine_fake_time
    cfg = ConfigModel()
    uplink = FakeUplink()
    uplink.report_results = [False]
    sleep, _calls = _sleeper(cfg, stop_after=3)

    with pytest.raises(asyncio.CancelledError):
        await run_uplink(machine, cfg, uplink, sleep=sleep, now=_now, identity=identity)

    kinds = [e[0] for e in uplink.events]
    assert kinds == ["register", "put", "post", "register", "put", "post", "post"]


async def test_reports_continue_while_bundle_refused(machine_fake_time, identity):
    machine, _ = machine_fake_time
    cfg = ConfigModel()
    uplink = FakeUplink(bundle_results=[False] * 10)
    sleep, _calls = _sleeper(cfg, stop_after=3)

    with pytest.raises(asyncio.CancelledError):
        await run_uplink(machine, cfg, uplink, sleep=sleep, now=_now, identity=identity)

    kinds = [e[0] for e in uplink.events]
    assert kinds == ["register", "put", "post", "put", "post", "put", "post"]


async def test_refused_bundle_is_retried_hourly_not_every_pass(
    machine_fake_time, identity
):
    machine, _ = machine_fake_time
    cfg = ConfigModel()  # 60 s interval -> retry after 3600 s
    uplink = FakeUplink(bundle_results=[False] * 100)
    uplink.bundle_status = 409
    clock = {"t": _now()}

    async def sleep(seconds):
        clock["t"] += timedelta(seconds=seconds)
        if clock["t"] > _now() + timedelta(seconds=3600 + 120):
            raise asyncio.CancelledError

    with pytest.raises(asyncio.CancelledError):
        await run_uplink(
            machine, cfg, uplink, sleep=sleep, now=lambda: clock["t"], identity=identity
        )

    puts = [e for e in uplink.events if e[0] == "put"]
    posts = [e for e in uplink.events if e[0] == "post"]
    assert len(puts) == 2  # the first pass, then once an hour later
    assert len(posts) > 50


async def test_network_failure_of_bundle_still_retries_every_pass(
    machine_fake_time, identity
):
    machine, _ = machine_fake_time
    cfg = ConfigModel()
    uplink = FakeUplink(bundle_results=[False, False, False])
    uplink.bundle_status = None  # no response at all
    sleep, _calls = _sleeper(cfg, stop_after=3)

    with pytest.raises(asyncio.CancelledError):
        await run_uplink(machine, cfg, uplink, sleep=sleep, now=_now, identity=identity)

    assert [e[0] for e in uplink.events].count("put") == 3


async def test_reregistration_resends_bundle_and_live_notice(
    machine_fake_time, identity
):
    machine, _ = machine_fake_time
    cfg = ConfigModel()
    identity.set_notice("h1", None)
    uplink = FakeUplink()
    uplink.report_results = [True, False]  # second report 401s: cloud forgot us
    sleep, _calls = _sleeper(cfg, stop_after=4)

    with pytest.raises(asyncio.CancelledError):
        await run_uplink(machine, cfg, uplink, sleep=sleep, now=_now, identity=identity)

    kinds = [e[0] for e in uplink.events]
    assert kinds == [
        "register", "claim", "put", "post",
        "post",
        "register", "claim", "put", "post",
        "post",
    ]  # fmt: skip


async def _first_register_notice_events(machine, identity, *, claimed):
    cfg = ConfigModel()
    identity.set_notice("h1", None)
    identity.mark_notice_acked(identity.notice)
    uplink = FakeUplink(claimed=claimed)
    sleep, _calls = _sleeper(cfg, stop_after=1)
    with pytest.raises(asyncio.CancelledError):
        await run_uplink(machine, cfg, uplink, sleep=sleep, now=_now, identity=identity)
    return [e[0] for e in uplink.events]


async def test_first_register_resends_acked_notice_when_unclaimed(
    machine_fake_time, identity
):
    machine, _ = machine_fake_time
    kinds = await _first_register_notice_events(machine, identity, claimed=False)
    assert kinds == ["register", "claim", "put", "post"]


async def test_first_register_keeps_acked_notice_when_claimed(
    machine_fake_time, identity
):
    machine, _ = machine_fake_time
    kinds = await _first_register_notice_events(machine, identity, claimed=True)
    assert kinds == ["register", "put", "post"]


async def test_register_stores_alerts_handled(machine_fake_time, identity):
    machine, _ = machine_fake_time
    cfg = ConfigModel()
    uplink = FakeUplink()
    uplink.alerts_handled = True
    sleep, _calls = _sleeper(cfg, stop_after=1)
    with pytest.raises(asyncio.CancelledError):
        await run_uplink(machine, cfg, uplink, sleep=sleep, now=_now, identity=identity)
    assert identity.alerts_handled is True


async def test_report_ok_stamps_time(machine_fake_time, identity):
    machine, _ = machine_fake_time
    cfg = ConfigModel()
    sleep, _calls = _sleeper(cfg, stop_after=1)
    with pytest.raises(asyncio.CancelledError):
        await run_uplink(
            machine, cfg, FakeUplink(), sleep=sleep, now=_now, identity=identity
        )
    assert identity.last_report_ok_at == _now()


async def test_failed_report_does_not_stamp_time(machine_fake_time, identity):
    machine, _ = machine_fake_time
    cfg = ConfigModel()
    uplink = FakeUplink()
    uplink.report_results = [False]
    sleep, _calls = _sleeper(cfg, stop_after=1)
    with pytest.raises(asyncio.CancelledError):
        await run_uplink(machine, cfg, uplink, sleep=sleep, now=_now, identity=identity)
    assert identity.last_report_ok_at is None


async def test_401_clears_alerts_handled(machine_fake_time, identity):
    machine, _ = machine_fake_time
    cfg = ConfigModel()
    uplink = FakeUplink()
    uplink.alerts_handled = True
    uplink.report_results = [False]  # FakeUplink reports 401 on this failure
    seen: list[bool] = []

    async def sleep(seconds):
        seen.append(identity.alerts_handled)
        raise asyncio.CancelledError

    with pytest.raises(asyncio.CancelledError):
        await run_uplink(machine, cfg, uplink, sleep=sleep, now=_now, identity=identity)
    assert seen == [False]


async def test_alerts_handled_follows_every_report(machine_fake_time, identity):
    machine, _ = machine_fake_time
    cfg = ConfigModel()
    uplink = FakeUplink()
    uplink.report_flags = [True, False, True, None]
    seen: list[bool] = []

    async def sleep(seconds):
        seen.append(identity.alerts_handled)
        if len(seen) >= 4:
            raise asyncio.CancelledError

    with pytest.raises(asyncio.CancelledError):
        await run_uplink(machine, cfg, uplink, sleep=sleep, now=_now, identity=identity)
    assert seen == [True, False, True, False]  # a missing body means not handled
