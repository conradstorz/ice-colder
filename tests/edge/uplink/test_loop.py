import asyncio
from datetime import UTC, datetime

import pytest

from ice_common.config_model import ConfigModel
from ice_edge.uplink.loop import run_uplink
from ice_ui.bundle import ui_digest


class FakeUplink:
    def __init__(self, bundle_results=(True,)):
        self.bundle_results = list(bundle_results)
        self.events: list[tuple] = []

    async def put_ui_bundle(self, digest, data):
        self.events.append(("put", digest))
        return self.bundle_results.pop(0) if self.bundle_results else True

    async def post_report(self, report):
        self.events.append(("post", report.seq, report.boot_id))
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


def _now():
    return datetime(2026, 10, 13, tzinfo=UTC)


async def test_one_bundle_put_then_reports_with_rising_seq(machine_fake_time):
    machine, _ = machine_fake_time
    cfg = ConfigModel()
    uplink = FakeUplink()
    sleep, calls = _sleeper(cfg, stop_after=3)

    with pytest.raises(asyncio.CancelledError):
        await run_uplink(machine, cfg, uplink, sleep=sleep, now=_now)

    assert uplink.events[0] == ("put", ui_digest())
    posts = [e for e in uplink.events if e[0] == "post"]
    assert [p[1] for p in posts] == [0, 1, 2]
    assert len({p[2] for p in posts}) == 1
    assert [e[0] for e in uplink.events].count("put") == 1
    assert calls == [60, 60, 60]


async def test_interval_change_is_honoured_next_pass(machine_fake_time):
    machine, _ = machine_fake_time
    cfg = ConfigModel()
    sleep, calls = _sleeper(cfg, stop_after=3, set_interval_at=(1, 10))

    with pytest.raises(asyncio.CancelledError):
        await run_uplink(machine, cfg, FakeUplink(), sleep=sleep, now=_now)

    assert calls == [60, 10, 10]


async def test_bundle_put_retried_each_interval_until_accepted(machine_fake_time):
    machine, _ = machine_fake_time
    cfg = ConfigModel()
    uplink = FakeUplink(bundle_results=[False, False, True])
    sleep, calls = _sleeper(cfg, stop_after=3)

    with pytest.raises(asyncio.CancelledError):
        await run_uplink(machine, cfg, uplink, sleep=sleep, now=_now)

    assert [e[0] for e in uplink.events] == ["put", "put", "put", "post"]
    assert calls == [60, 60, 60]
