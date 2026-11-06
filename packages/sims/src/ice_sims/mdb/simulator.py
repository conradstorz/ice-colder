# ice_sims/mdb/simulator.py
"""MDB payment board simulator on payment contract 1.1.0.

Composes the pure models (`cash`, `policy`, `event_log`) with the behaviour
modules (`customer`, `faults`) and owns the MQTT side: capabilities, the
retained `payment/state`, the `payment/event` stream with its replay log,
the `cmd/mdb` command set and the edge-silence fail-safe.
"""

import asyncio
import random
import time
from collections import OrderedDict
from collections.abc import Awaitable, Callable
from datetime import datetime, timezone

import aiomqtt
from loguru import logger
from pydantic import ValidationError

from ice_contracts.board import payment
from ice_contracts.board.common import SubsystemCommand
from ice_contracts.board.payment import (
    DEFAULT_EDGE_TIMEOUT_SECONDS,
    PAYMENT_COMMANDS,
    STATE_REPUBLISH_SECONDS,
    BillReturnReason,
    CashSaleParams,
    Device,
    DeviceResetParams,
    DeviceState,
    DeviceStatus,
    PaymentDevices,
    PaymentEventAck,
    PaymentEventEnvelope,
    PaymentState,
    PayoutParams,
    SetPolicyParams,
)
from ice_sims.base import (
    CommandOutcome,
    ESP32Simulator,
    to_fault_def,
    uptime_entity,
)
from ice_sims.mdb.cash import CashBox
from ice_sims.mdb.customer import MdbCustomer
from ice_sims.mdb.event_log import EventLog
from ice_sims.mdb.faults import mdb_faults
from ice_sims.mdb.policy import PolicyState

BILL_DENOMINATIONS = [100, 500, 1000, 2000]
USABLE_STATUSES = {DeviceStatus.ready, DeviceStatus.empty, DeviceStatus.full}


class _Port:
    """Adapter exposing the simulator to `faults` as an `MdbBoardPort`,
    bound to one MQTT client. Stateless: it only forwards."""

    def __init__(self, sim: "MdbSimulator", client: aiomqtt.Client) -> None:
        self._sim = sim
        self._client = client

    async def set_device_status(self, device: Device, status: DeviceStatus) -> None:
        await self._sim.set_device_status(self._client, device, status)

    async def drain_tubes(self) -> None:
        await self._sim.drain_tubes(self._client)

    async def restore_tubes(self) -> None:
        await self._sim.restore_tubes(self._client)

    async def reboot(self) -> None:
        await self._sim.reboot(self._client)

    def set_ack_drop(self, dropping: bool) -> None:
        self._sim.set_ack_drop(dropping)

    async def replay(self) -> None:
        await self._sim.replay(self._client)


class _CustomerPort:
    """Adapter exposing the simulator to `customer` as a `CustomerPort`."""

    def __init__(self, sim: "MdbSimulator", client: aiomqtt.Client) -> None:
        self._sim = sim
        self._client = client

    @property
    def accepting(self) -> bool:
        return self._sim.build_state().accepting

    @property
    def coin_enabled(self) -> list[int]:
        return self._sim.build_state().devices.coin.enabled

    @property
    def bill_enabled(self) -> list[int]:
        return self._sim.build_state().devices.bill.enabled

    @property
    def escrow_cents(self) -> int:
        return self._sim.cash.escrow_cents

    def interacting(self) -> bool:
        status = self._sim.last_status
        return bool(status) and status.get("state") == "interacting_with_user"

    def price_cents_for(self, product_name: str) -> int | None:
        return self._sim.product_prices.get(product_name)

    async def next_status(self) -> dict:
        return await self._sim.next_vmc_status()

    async def insert_coin(self, cents: int) -> bool:
        return await self._sim.insert_coin(self._client, cents)

    async def insert_bill(self, cents: int) -> bool:
        return await self._sim.insert_bill(self._client, cents)

    async def stack_bill(self) -> None:
        await self._sim.stack_bill(self._client)

    async def press_lever(self) -> None:
        await self._sim.press_lever(self._client)

    async def sleep(self, seconds: float) -> None:
        await self._sim.sleep(seconds)


class MdbSimulator(ESP32Simulator):
    """Simulates the MDB payment board: coin changer, bill validator, no reader."""

    CONTRACT_VERSION = payment.CONTRACT_VERSION
    SUPPORTED_COMMANDS = list(PAYMENT_COMMANDS)
    CHANNELS = []
    BRAND = "ice-colder"
    MODEL = "mdb-sim"

    PAYOUT_DELAY_RANGE = (0.5, 2.0)  # seconds the changer takes to pay out
    PAYOUT_REQUESTS_MAX = 256  # stored payout results, oldest evicted first
    WATCHDOG_INTERVAL = 1.0  # seconds between edge-silence checks

    def __init__(
        self,
        *,
        clock: Callable[[], float] | None = None,
        now: Callable[[], datetime] | None = None,
        sleep: Callable[[float], Awaitable[None]] | None = None,
        customer: MdbCustomer | None = None,
        **kwargs,
    ) -> None:
        super().__init__(subsystem_name="mdb", sleep=sleep, **kwargs)
        self._clock = clock or time.monotonic
        self._now = now or (lambda: datetime.now(timezone.utc))
        self._customer = customer or MdbCustomer()

        self.cash = CashBox()
        self.policy = PolicyState()
        self.log = EventLog(EventLog.new_boot_id())
        self.edge_silent = False
        self._device_status = {
            Device.coin: DeviceStatus.ready,
            Device.bill: DeviceStatus.ready,
        }
        self._last_cmd = self._clock()
        self._drop_acks = False
        self._saved_tubes: list[int] | None = None
        # request_id -> stored payout_result envelope (None while in flight)
        self._payout_requests: OrderedDict[str, dict | None] = OrderedDict()
        self._payout_task: asyncio.Task | None = None

        self.last_status: dict | None = None
        self._vmc_status: asyncio.Queue = asyncio.Queue()
        self.product_prices = {p.name: p.price_cents for p in self.config.products}

        self.register_command("set_policy", self._handle_set_policy)
        self.register_command("payout", self._handle_payout)
        self.register_command("vend_request", self._handle_vend_request)
        self.register_command("vend_result", self._handle_no_session)
        self.register_command("end_session", self._handle_no_session)
        self.register_command("return_escrow", self._handle_return_escrow)
        self.register_command("device_reset", self._handle_device_reset)
        self.register_command("cash_sale", self._handle_cash_sale)

        for fault in mdb_faults():
            self.register_fault(to_fault_def(fault, lambda client: _Port(self, client)))

    # --- small public seams ------------------------------------------------

    @property
    def boot_id(self) -> str:
        return self.log.boot_id

    async def next_vmc_status(self) -> dict:
        return await self._vmc_status.get()

    async def drain_background(self) -> None:
        """Wait for every background task (payout completions) to finish."""
        while self._background_tasks:
            await asyncio.gather(*list(self._background_tasks), return_exceptions=True)

    def set_ack_drop(self, dropping: bool) -> None:
        self._drop_acks = dropping

    # --- state ---------------------------------------------------------------

    def _usable(self, device: Device) -> bool:
        return self._device_status[device] in USABLE_STATUSES

    def build_state(self) -> PaymentState:
        p = self.policy.policy
        tube_cents = [t.cents for t in self.cash.tubes]
        coin_enabled = [c for c in (p.coin_enabled if p else []) if c in tube_cents]
        bill_enabled = [
            b for b in (p.bill_enabled if p else []) if b in BILL_DENOMINATIONS
        ]
        # Replay-log pressure inhibits acceptance with `device_fault`, as the firmware does.
        device_fault = self.log.pressure or (
            not self._usable(Device.coin) and not self._usable(Device.bill)
        )
        reason = self.policy.inhibited_reason(
            edge_silent=self.edge_silent, device_fault=device_fault
        )
        return PaymentState(
            boot_id=self.log.boot_id,
            seq_last=self.log.seq,
            policy_id=p.policy_id if p else None,
            accepting=reason is None,
            inhibited_reason=reason,
            escrow_cents=self.cash.escrow_cents,
            bill_in_escrow_cents=self.cash.bill_in_escrow_cents,
            devices=PaymentDevices(
                coin=DeviceState(
                    present=True,
                    status=self._device_status[Device.coin],
                    level=3,
                    model="COINCO-SIM",
                    serial="SIM-COIN-1",
                    denominations=tube_cents,
                    enabled=coin_enabled,
                ),
                bill=DeviceState(
                    present=True,
                    status=self._device_status[Device.bill],
                    level=None,
                    model="BV-SIM",
                    serial="SIM-BILL-1",
                    denominations=list(BILL_DENOMINATIONS),
                    enabled=bill_enabled,
                ),
                cashless=DeviceState(present=False, status=DeviceStatus.disabled),
            ),
            tubes=self.cash.tube_levels(),
            exact_change=self.policy.exact_change(self.cash),
            stacker=self.cash.stacker_state(),
            session=None,
            ts=self._now().replace(microsecond=0),
        )

    async def publish_state(self, client: aiomqtt.Client) -> None:
        await self.publish(client, "payment/state", self.build_state(), retain=True)

    async def state_loop(self, client: aiomqtt.Client) -> None:
        while True:
            await self.publish_state(client)
            await self._sleep(STATE_REPUBLISH_SECONDS)

    # --- events ----------------------------------------------------------------

    async def _publish_envelope(self, client: aiomqtt.Client, env: dict) -> None:
        # Validating here means a simulator bug cannot put a malformed event
        # on the wire; the model also fixes the timestamp format.
        await self.publish(
            client, "payment/event", PaymentEventEnvelope.model_validate(env)
        )

    async def _emit(self, client: aiomqtt.Client, kind: str, body: dict) -> dict:
        # Logged before it is published, like the real board's flash log.
        env = self.log.next_event(kind, body, self._now().replace(microsecond=0))
        await self._publish_envelope(client, env)
        return env

    async def replay(self, client: aiomqtt.Client) -> None:
        """Republish the whole unacked log, earlier boots first."""
        for env in self.log.unacked():
            await self._publish_envelope(client, env)

    def handle_event_ack(self, data: dict) -> None:
        try:
            ack = PaymentEventAck.model_validate(data)
        except ValidationError as e:
            logger.warning(f"[mdb] Bad payment/event/ack ignored: {e}")
            return
        if self._drop_acks:
            logger.debug("[mdb] Ack dropped (drop_ack_window)")
            return
        self.log.ack(ack.boot_id, ack.seq)

    async def _event_ack_loop(self, client: aiomqtt.Client) -> None:
        queue = await self.subscribe(client, f"{self.topic_prefix}/payment/event/ack")
        while True:
            _topic, data = await queue.get()
            try:
                self.handle_event_ack(data)
            finally:
                queue.task_done()

    # --- hardware actions ----------------------------------------------------------

    async def insert_coin(self, client: aiomqtt.Client, cents: int) -> bool:
        if (
            self.edge_silent
            or self.log.pressure
            or not self._usable(Device.coin)
            or not self.policy.accepts_coin(cents, self.cash)
        ):
            return False
        route = self.cash.accept_coin(cents)
        await self._emit(
            client, "coin_accepted", {"cents": cents, "routed": route.value}
        )
        await self.publish_state(client)
        return True

    async def insert_bill(self, client: aiomqtt.Client, cents: int) -> bool:
        p = self.policy.policy
        if (
            self.edge_silent
            or self.log.pressure
            or not self._usable(Device.bill)
            or self.cash.bill_in_escrow_cents is not None
            or p is None
            or not self.policy.acked_in_boot
            or not p.accept
            or cents not in p.bill_enabled
        ):
            return False  # the validator is inhibited: the bill is pushed out
        self.cash.escrow_bill(cents)
        await self._emit(client, "bill_escrowed", {"cents": cents})
        if not self.policy.accepts_bill(cents, self.cash):
            self.cash.return_bill()
            await self._emit(
                client,
                "bill_returned",
                {"cents": cents, "reason": BillReturnReason.policy.value},
            )
            await self.publish_state(client)
            return False
        await self.publish_state(client)
        return True

    async def stack_bill(self, client: aiomqtt.Client) -> None:
        cents = self.cash.bill_in_escrow_cents
        if cents is None:
            return
        self.cash.stack_bill(cents)
        await self._emit(client, "bill_stacked", {"cents": cents})
        await self.publish_state(client)

    async def _return_held_bill(
        self, client: aiomqtt.Client, reason: BillReturnReason
    ) -> bool:
        cents = self.cash.return_bill()
        if cents is None:
            return False
        await self._emit(
            client, "bill_returned", {"cents": cents, "reason": reason.value}
        )
        return True

    async def press_lever(self, client: aiomqtt.Client) -> None:
        p = self.policy.policy
        if p is not None and p.return_bill_on_lever:
            await self._return_held_bill(client, BillReturnReason.customer)
        await self._emit(client, "escrow_return_pressed", {})
        await self.publish_state(client)

    async def set_device_status(
        self, client: aiomqtt.Client, device: Device, status: DeviceStatus
    ) -> None:
        self._device_status[device] = status
        if device == Device.bill and status not in USABLE_STATUSES:
            await self._return_held_bill(client, BillReturnReason.timeout)
        await self._emit(
            client,
            "device_status",
            {"device": device.value, "status": status.value, "mdb_status": None},
        )
        await self.publish_state(client)

    async def drain_tubes(self, client: aiomqtt.Client) -> None:
        self._saved_tubes = [t.count for t in self.cash.tubes]
        for tube in self.cash.tubes:
            tube.count = 0
        await self.publish_state(client)

    async def restore_tubes(self, client: aiomqtt.Client) -> None:
        saved = self._saved_tubes or [40] * len(self.cash.tubes)
        for tube, count in zip(self.cash.tubes, saved):
            tube.count = max(tube.count, count)
        self._saved_tubes = None
        await self.publish_state(client)

    async def reboot(self, client: aiomqtt.Client) -> None:
        """New boot: policy, edge-silence, escrow, the RAM ack cache and any
        in-flight payout are lost; the flash log and the persisted payout
        results are not (firmware spec: escrow is RAM only, zero per boot)."""
        self.log.reboot(EventLog.new_boot_id())
        self.cash.escrow_cents = 0
        self._acked.clear()
        if self._payout_task is not None and not self._payout_task.done():
            self._payout_task.cancel()
        self._payout_task = None
        for request_id in [k for k, v in self._payout_requests.items() if v is None]:
            del self._payout_requests[request_id]
        self.policy.reset()
        self.edge_silent = False
        self._last_cmd = self._clock()
        await self.publish_state(client)
        await self.replay(client)

    async def check_edge_silence(self, client: aiomqtt.Client) -> None:
        p = self.policy.policy
        if p is None or not self.policy.acked_in_boot or self.edge_silent:
            return
        if self._clock() - self._last_cmd < p.edge_timeout_s:
            return
        logger.warning("[mdb] Edge silent: disabling acceptance")
        self.edge_silent = True
        await self._return_held_bill(client, BillReturnReason.edge_silent)
        await self.publish_state(client)

    async def _watchdog_loop(self, client: aiomqtt.Client) -> None:
        while True:
            await self._sleep(self.WATCHDOG_INTERVAL)
            await self.check_edge_silence(client)

    # --- commands --------------------------------------------------------------------

    async def handle_command(
        self, client: aiomqtt.Client, cmd: SubsystemCommand
    ) -> None:
        self._last_cmd = self._clock()
        replay_result = cmd.command == "payout" and self._payout_requests.get(
            cmd.request_id
        )
        await super().handle_command(client, cmd)
        ack = self._acked.get(cmd.request_id)
        if replay_result and ack is not None and ack.status == "ok":
            # Rule 5: a repeated payout re-publishes the stored result, but
            # only once the gates passed (a rejected repeat replays nothing).
            await self._publish_envelope(client, replay_result)

    def _expired(self, cmd: SubsystemCommand) -> bool:
        p = self.policy.policy
        timeout = p.edge_timeout_s if p is not None else DEFAULT_EDGE_TIMEOUT_SECONDS
        ts = cmd.timestamp
        if ts.tzinfo is None:
            ts = ts.replace(tzinfo=timezone.utc)
        return (self._now() - ts).total_seconds() > timeout

    def _gate(self, cmd: SubsystemCommand) -> CommandOutcome | None:
        """Rules 1 and 4 for the commands that need a policy. Expiry is checked
        first, as the firmware does (`Master::do_payout`), using the default
        edge timeout while no policy is held."""
        if self._expired(cmd):
            return CommandOutcome(status="rejected", detail="expired")
        if self.policy.policy is None or not self.policy.acked_in_boot:
            return CommandOutcome(status="rejected", detail="no_policy")
        return None

    async def _handle_set_policy(
        self, client: aiomqtt.Client, cmd: SubsystemCommand
    ) -> None:
        self.policy.apply(SetPolicyParams.model_validate(cmd.params))
        self.edge_silent = False
        logger.info(f"[mdb] Policy {self.policy.policy.policy_id} applied")
        await self.publish_state(client)

    async def _handle_payout(
        self, client: aiomqtt.Client, cmd: SubsystemCommand
    ) -> CommandOutcome:
        accepted = CommandOutcome(status="ok", phase="accepted")
        # Firmware order (Master::do_payout): expired, no_policy, cents, and
        # only then the persisted-result replay of a repeated request_id.
        refused = self._gate(cmd)
        if refused is not None:
            return refused
        if cmd.params.get("cents") == 0:
            return CommandOutcome(status="rejected", detail="cents")
        if cmd.request_id in self._payout_requests:
            return accepted  # a repeat: handle_command re-publishes the result
        params = PayoutParams.model_validate(cmd.params)
        if any(result is None for result in self._payout_requests.values()):
            # Firmware spec §11: a different payout while another's result is
            # still held (in flight) is refused, never queued.
            return CommandOutcome(status="rejected", detail="busy")
        self._payout_requests[cmd.request_id] = None
        self._payout_task = self._spawn_background(
            self._complete_payout(client, cmd.request_id, params)
        )
        return accepted

    async def _complete_payout(
        self, client: aiomqtt.Client, request_id: str, params: PayoutParams
    ) -> None:
        await self._sleep(random.uniform(*self.PAYOUT_DELAY_RANGE))
        outcome = self.cash.payout(params.cents)
        env = await self._emit(
            client,
            "payout_result",
            {
                "request_id": request_id,
                "requested_cents": params.cents,
                "paid_cents": outcome.paid_cents,
                "status": outcome.status.value,
                "detail": outcome.detail,
            },
        )
        self._payout_requests[request_id] = env
        while len(self._payout_requests) > self.PAYOUT_REQUESTS_MAX:
            self._payout_requests.popitem(last=False)
        logger.info(
            f"[mdb] Payout {request_id}: {outcome.status.value} "
            f"({outcome.paid_cents}/{params.cents} cents, {params.reason.value})"
        )
        await self.publish_state(client)

    async def _handle_vend_request(
        self, client: aiomqtt.Client, cmd: SubsystemCommand
    ) -> CommandOutcome:
        return self._gate(cmd) or CommandOutcome(status="rejected", detail="no_session")

    async def _handle_no_session(
        self, client: aiomqtt.Client, cmd: SubsystemCommand
    ) -> CommandOutcome:
        return CommandOutcome(status="rejected", detail="no_session")

    async def _handle_return_escrow(
        self, client: aiomqtt.Client, cmd: SubsystemCommand
    ) -> CommandOutcome | None:
        if not await self._return_held_bill(client, BillReturnReason.edge):
            return CommandOutcome(status="ok", detail="no_bill")
        await self.publish_state(client)
        return None

    async def _handle_device_reset(
        self, client: aiomqtt.Client, cmd: SubsystemCommand
    ) -> CommandOutcome | None:
        params = DeviceResetParams.model_validate(cmd.params)
        if params.device == Device.cashless:
            return CommandOutcome(status="rejected", detail="not_present")
        await self.set_device_status(
            client, params.device, self._device_status[params.device]
        )
        return None

    async def _handle_cash_sale(
        self, client: aiomqtt.Client, cmd: SubsystemCommand
    ) -> CommandOutcome | None:
        params = CashSaleParams.model_validate(cmd.params)
        _applied, clamped = self.cash.cash_sale(params.cents)
        await self.publish_state(client)
        if clamped:
            return CommandOutcome(status="ok", detail="clamped")
        return None

    # --- discovery, force_report ----------------------------------------------------------

    def ha_discovery_entities(self) -> list[dict]:
        entities: list[dict] = []
        for device in ("coin", "bill"):
            entities.append(
                {
                    "component": "binary_sensor",
                    "object_id": f"{device}_ready",
                    "name": f"MDB {device.title()} Ready",
                    "state_topic_suffix": "payment/state",
                    "value_template": (
                        f"{{% if value_json.devices.{device}.status == 'ready' %}}"
                        "ON{% else %}OFF{% endif %}"
                    ),
                    "device_class": "running",
                    "payload_on": "ON",
                    "payload_off": "OFF",
                }
            )
        entities.append(
            {
                "component": "sensor",
                "object_id": "escrow_cents",
                "name": "MDB Escrow",
                "state_topic_suffix": "payment/state",
                "value_template": "{{ value_json.escrow_cents }}",
                "unit_of_measurement": "cents",
            }
        )
        entities.append(
            {
                "component": "sensor",
                "object_id": "accepting",
                "name": "MDB Accepting",
                "state_topic_suffix": "payment/state",
                "value_template": "{{ 'on' if value_json.accepting else 'off' }}",
            }
        )
        entities.append(uptime_entity("mdb", "MDB Gateway Uptime"))
        return entities

    async def _force_report_extra(self, client: aiomqtt.Client) -> None:
        await self._publish_capabilities(client)
        await self.publish_state(client)

    # --- run -------------------------------------------------------------------------------

    async def _watch_vmc_status(self, client: aiomqtt.Client) -> None:
        topic = f"{self.topic_prefix}/status"
        queue = await self.subscribe(client, topic)
        logger.info(f"[mdb] Listening for VMC status on {topic}")
        while True:
            _topic, data = await queue.get()
            self.last_status = data
            await self._vmc_status.put(data)

    async def run_simulation(self, client: aiomqtt.Client) -> None:
        logger.info("[mdb] Starting MDB gateway simulation")
        self._last_cmd = self._clock()
        # On (re)connect the whole unacked log goes out before anything new.
        await self.replay(client)
        async with asyncio.TaskGroup() as tg:
            tg.create_task(self.state_loop(client))
            tg.create_task(self._event_ack_loop(client))
            tg.create_task(self._watch_vmc_status(client))
            tg.create_task(self._customer.run(_CustomerPort(self, client)))
            tg.create_task(self._watchdog_loop(client))
            tg.create_task(self.command_loop(client))
