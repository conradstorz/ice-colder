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
    ):
        self.on_claim = on_claim
        self.bundle_results = list(bundle_results)
        self.register_results = list(register_results)
        self.claim_results = list(claim_results)
        self.delete_results = list(delete_results)
        self.events: list[tuple] = []
        self.last_status: int | None = 200
        self.bundle_status = None
        self.report_results: list[bool] = []

    async def register(self, label):
        self.events.append(("register", label))
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
