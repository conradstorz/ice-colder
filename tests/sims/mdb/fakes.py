"""Test doubles for the MDB simulator: a recording MQTT client and a fake clock."""

import json
from datetime import datetime, timedelta, timezone

from ice_contracts.board.common import CommandAck, SubsystemCommand
from ice_contracts.board.payment import SetPolicyParams

PREFIX = "vmc/vmc-t"


class FakeClient:
    def __init__(self) -> None:
        self.published: list[tuple[str, str, bool]] = []
        self.subscribed: list[str] = []

    async def publish(self, topic, payload, qos=0, retain=False):
        self.published.append((topic, payload, retain))

    async def subscribe(self, topic, *args, **kwargs):
        self.subscribed.append(topic)

    def on(self, suffix: str) -> list[dict]:
        return [
            json.loads(p) for t, p, _ in self.published if t == f"{PREFIX}/{suffix}"
        ]

    def retained(self, suffix: str) -> list[dict]:
        return [
            json.loads(p)
            for t, p, r in self.published
            if t == f"{PREFIX}/{suffix}" and r
        ]

    def raw(self, suffix: str) -> list[str]:
        return [p for t, p, _ in self.published if t == f"{PREFIX}/{suffix}"]

    def events(self) -> list[dict]:
        return self.on("payment/event")

    def kinds(self) -> list[str]:
        return [e["event"]["kind"] for e in self.events()]

    def acks(self) -> list[CommandAck]:
        return [CommandAck.model_validate(a) for a in self.on("cmd/mdb/ack")]

    def clear(self) -> None:
        self.published.clear()


class FakeTime:
    """Monotonic seconds plus a wall clock that moves with it."""

    def __init__(self) -> None:
        self.t = 1000.0
        self._base = datetime(2026, 10, 30, 12, 0, 0, tzinfo=timezone.utc)

    def monotonic(self) -> float:
        return self.t

    def now(self) -> datetime:
        return self._base + timedelta(seconds=self.t - 1000.0)

    def advance(self, seconds: float) -> None:
        self.t += seconds


def policy(**over) -> dict:
    base = dict(
        policy_id="pol-0001",
        accept=True,
        max_escrow_cents=2000,
        coin_enabled=[5, 10, 25, 100],
        bill_enabled=[100, 500, 1000],
        cashless_enabled=False,
        change_threshold_cents=500,
        edge_timeout_s=30,
        return_bill_on_lever=True,
    )
    base.update(over)
    SetPolicyParams.model_validate(base)
    return base


_counter = {"n": 0}


def command(
    name: str,
    params: dict | None = None,
    *,
    request_id: str | None = None,
    timestamp: datetime | None = None,
) -> SubsystemCommand:
    _counter["n"] += 1
    kwargs = {}
    if timestamp is not None:
        kwargs["timestamp"] = timestamp
    return SubsystemCommand(
        request_id=request_id or f"req-{_counter['n']:06d}",
        command=name,
        params=params or {},
        **kwargs,
    )


async def send(sim, client, name, params=None, **kw) -> CommandAck:
    """Dispatch one command and return the ack it produced."""
    before = len(client.acks())
    await sim.handle_command(client, command(name, params, **kw))
    acks = client.acks()
    assert len(acks) == before + 1
    return acks[-1]
