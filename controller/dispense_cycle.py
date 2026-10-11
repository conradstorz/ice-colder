# controller/dispense_cycle.py
"""The per-sale dispense conversation, as a standalone class.

Extracted from ``controller.vmc.VMC`` as the seventh piece carved off the
VMC god object, following the same pattern as ``controller/outputs.py``'s
``StatusOutputs``, ``controller/fault_service.py``'s ``FaultService``, and
the other collaborators described in ``CLAUDE.md``'s "FSM Core" section.
``DispenseCycle`` owns exactly one sale's dispatch: minting the request id
before any hardware report can race it, persisting the dispensing
snapshot, sending the ``cmd/vending/dispense`` command through the
dispatcher and awaiting only its accepted ack, the no-terminal-report
timeout, classifying a terminal ``hardware/dispenser`` report into a
``DispenseReport``, and durably recording a successful sale.

It knows nothing about the FSM: every side effect that drives a state
transition -- failing the vend, finishing the vend, raising ICE-402 on a
``door_open`` success, resolving a test-sale waiter -- is left to the
caller via the injected ``on_failed`` callable and the ``DispenseReport``
``classify`` returns. ``on_dispense_product``'s own seq/state guard
(``VMC._fail_dispense_async``) is *not* reproduced here: this class's
identity (one instance per dispatch attempt) is what the VMC uses instead
to tell a late failure from an earlier attempt apart from the current one
(Task 8).

Every collaborator is injected, mirroring the convention already used by
``FaultService``/``StatusOutputs``: ``dispatcher`` and ``recorder`` are
zero-arg callables read at call time (matching ``VMC._dispatcher``/
``VMC._recorder`` exactly, since neither is wired until after the VMC/
``Machine`` is constructed), and ``set_transaction_certain`` is a closure
that already carries its own "is availability attached" guard, like
``FaultService``'s own collaborator of the same name.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from uuid import uuid4

from loguru import logger

from contracts.vending_machine import (
    DispenserOutcome,
    FaultCode,
    fault_for_outcome,
)
from controller.dispenser_gate import DispenserProfileGate
from controller.fault_service import FaultService
from controller.outputs import StatusOutputs
from controller.sale_context import SaleContext
from controller.task_runner import TaskRunner
from services.command_dispatcher import CommandTimeout
from services.event_recorder import SaleRecordingFailed
from services.mqtt_messages import DispenseCommand
from services.session_store import SessionSnapshot

# Bound logger for the vending log -- mirrors controller/vmc.py's own
# `vend_log` / controller/machine.py's `_txn_log`: loguru's bind() only
# attaches the `vending` extra field, and sinks are resolved at log time,
# so binding here at import is safe even though setup_logging() runs later
# in main().
vend_log = logger.bind(vending=True)


def _has_outcome_mapping(mechanism: str | None, outcome: DispenserOutcome) -> bool:
    """True iff `(mechanism, outcome)` has an entry in OUTCOME_FAULTS --
    used by `classify` (review finding C3, Copilot PR #32) to decide
    whether `door_open` is a success for *this* mechanism (bagged_ice)
    or must take the ordinary failed-vend path (every other mechanism,
    e.g. water_fill, which has no `(water_fill, door_open)` mapping)."""
    try:
        fault_for_outcome(mechanism, outcome)
    except KeyError:
        return False
    return True


@dataclass(frozen=True)
class DispenseReport:
    """What a terminal `hardware/dispenser` report means for this sale,
    as decided by `DispenseCycle.classify`. `fault` is `ICE_402` for a
    successful `door_open` (the product released, but the trap door
    didn't close), the mapped (or fallback `error`) code for a failure,
    and `None` for an ordinary `complete`."""

    outcome: DispenserOutcome
    success: bool
    fault: FaultCode | None


class DispenseCycle:
    """One sale's dispense conversation: dispatch, timeout, classify,
    record. Constructed fresh for each dispatch attempt (Task 8) so its
    own identity, not a sequence number, is what tells a late callback
    from an earlier attempt apart from the current one.
    """

    def __init__(
        self,
        *,
        sale: SaleContext,
        dispatcher: Callable[[], object | None],
        gate: DispenserProfileGate,
        outputs: StatusOutputs,
        faults: FaultService,
        recorder: Callable[[], object | None],
        set_transaction_certain: Callable[[bool], None],
        tasks: TaskRunner,
        timeout_seconds: Callable[[], float],
        on_failed: Callable[["DispenseCycle", FaultCode, str], Awaitable[None]],
        on_request_id: Callable[[str, str], None],
    ) -> None:
        self.sale = sale
        self._dispatcher = dispatcher
        self._gate = gate
        self._outputs = outputs
        self._faults = faults
        self._recorder = recorder
        self._set_transaction_certain = set_transaction_certain
        self._tasks = tasks
        self._timeout_seconds = timeout_seconds
        self._on_failed = on_failed
        self._on_request_id = on_request_id

        self._request_id: str | None = None
        self._mechanism: str | None = None
        self._timeout_task = None

    @property
    def request_id(self) -> str | None:
        return self._request_id

    @property
    def mechanism(self) -> str | None:
        return self._mechanism

    @logger.catch()
    def start(self, snapshot_for: Callable[[str], SessionSnapshot | None]) -> None:
        """Begin the dispense: look up the slot's dispenser profile, mint
        the request id a hardware report must echo back, persist the
        dispensing snapshot, arm the no-report timeout, and hand the
        actual dispatch to a fire-and-forget task.

        `on_dispense_product`'s own body (controller/vmc.py) minus the
        defensive comment about FSM transition timing -- that concern
        belongs to the caller now (Task 8 calls this only once the
        `dispense_product` transition has actually committed).

        Wrapped in ``@logger.catch()`` so an unexpected exception here is
        logged and swallowed rather than propagating into the FSM
        transition that calls it -- preserving the original
        `on_dispense_product`'s never-crash-the-transition property.
        """
        product = self.sale.product
        profile = self._gate.profile_for(product)
        if profile is None:
            # Cannot happen after the CFG-101 lockout (select_product
            # already refuses a profile-less product) -- a defensive
            # fallback for the pathological case where the profile
            # vanished between selection and dispense (e.g. a concurrent
            # dispensers.toml reload racing the sale).
            logger.error(
                f"No valid dispenser profile for sku={product.sku!r} "
                f"slot={product.slot} at dispense time; failing the vend"
            )
            self._tasks.fire_and_forget(
                self._on_failed(self, FaultCode.CFG_101, "no_profile"),
                persistent=True,
            )
            return

        # Review finding C2 (Copilot, PR #32): the request_id must be
        # known *before* any terminal report can possibly arrive, not
        # learned only once the dispatcher's ack comes back -- a report
        # that wins the race against a slow ack would otherwise be
        # unverifiable. Generated and recorded here, synchronously,
        # before the dispatch task is even created.
        request_id = uuid4().hex
        self._request_id = request_id
        self._mechanism = profile.mechanism
        self._on_request_id(request_id, profile.mechanism)

        cmd = DispenseCommand(
            slot=product.slot, mechanism=profile.mechanism, profile=profile
        )
        vend_log.info(
            f"DISPENSE CMD: slot {product.slot}, product '{product.name}', "
            f"mechanism {profile.mechanism}"
        )

        snap = snapshot_for("dispensing")
        self._timeout_task = self._tasks.schedule(
            self._timeout_seconds(), self._timed_out, label="dispense_timeout"
        )
        self._tasks.fire_and_forget(self._run(snap, cmd, request_id), persistent=True)

    async def _run(
        self, snap: SessionSnapshot | None, cmd: DispenseCommand, request_id: str
    ) -> None:
        """Write the dispensing snapshot to disk before the ESP32 is told
        to move, then send the dispense command through the command
        dispatcher and await only its accepted ack -- never completion.

        Today's `_persist_then_dispense` body (controller/vmc.py), with
        `on_failed(self, ...)` in place of `_fail_dispense_async`.
        """
        try:
            await self._outputs.save_snapshot_async(snap)
        except Exception as exc:
            # Review finding M5: a snapshot-save failure gets its own
            # outcome string, distinct from a dispatch failure's "no_ack"
            # -- the fault code is the same PAY-102 either way.
            logger.exception(
                f"VMC: failed to save dispensing snapshot for slot {cmd.slot}: {exc}"
            )
            await self._on_failed(self, FaultCode.PAY_102, "snapshot_failed")
            return

        try:
            dispatcher = self._dispatcher()
            if dispatcher is None:
                logger.error(
                    "VMC: no command dispatcher attached; cannot send "
                    "dispense command (wiring error)"
                )
                await self._on_failed(self, FaultCode.PAY_102, "no_ack")
                return

            ack = await dispatcher.send(
                "vending",
                "dispense",
                cmd.model_dump(mode="json"),
                request_id=request_id,
            )

            if ack.status != "ok":
                logger.error(
                    f"VMC: dispense ack for slot {cmd.slot} status={ack.status!r} "
                    f"detail={ack.detail!r}"
                )
                await self._on_failed(self, FaultCode.PAY_102, "no_ack")
                return
        except CommandTimeout:
            logger.error(
                f"VMC: dispatcher timed out sending dispense for slot {cmd.slot}"
            )
            await self._on_failed(self, FaultCode.PAY_102, "no_ack")
            return
        except Exception as exc:
            logger.exception(
                f"VMC: unexpected error dispatching dispense for slot {cmd.slot}: {exc}"
            )
            await self._on_failed(self, FaultCode.PAY_102, "no_ack")
            return

        if ack.request_id != request_id:
            # Should not happen -- the dispatcher echoes back exactly the
            # id it was given -- but a surprise here must never clobber
            # the id already recorded for this (or, worse, a later) sale.
            logger.warning(
                f"VMC: dispense ack request_id={ack.request_id!r} does not "
                f"match the sent request_id={request_id!r} for slot {cmd.slot}"
            )

    @logger.catch()
    def _timed_out(self) -> None:
        """No terminal dispenser report arrived within the configured
        timeout. Today's `_dispense_timed_out` body (controller/vmc.py),
        minus the FSM-state check and the fault-raising/vend-failing
        side effects, which belong to the caller now (Task 8) -- it is
        the caller's job to tell a timeout that outlived its own sale
        apart from one that still matters.

        Wrapped in ``@logger.catch()``, mirroring the original
        `_dispense_timed_out`: `TaskRunner.schedule`'s delayed callback
        attaches no failure logger of its own, so an uncaught exception
        here would otherwise be silently dropped, leaving the FSM stuck
        in `dispensing` with no timer to retry or fail it.
        """
        self._timeout_task = None
        logger.error(
            f"Dispense timed out after {self._timeout_seconds():.0f}s with no "
            f"terminal report (slot {self.sale.product.slot})"
        )
        self._tasks.fire_and_forget(
            self._on_failed(self, FaultCode.PAY_102, "no_report"),
            persistent=True,
        )

    def classify(self, data: dict) -> DispenseReport | None:
        """Classify one `hardware/dispenser` report for this sale, or
        return `None` (logging why) when it must be ignored outright: a
        non-terminal `state` (an intermediate agitate/fill/release step),
        a report for a slot other than this sale's, or a report carrying
        a different sale's `request_id`. A report with no `request_id`
        at all (a pre-1.0.0 board) is still accepted, keyed on slot alone.

        Today's slot-mismatch, request_id-mismatch, `door_open`, and
        `fault_for_outcome` logic from `on_dispenser_event`
        (controller/vmc.py).
        """
        state = data.get("state", "")
        slot = data.get("slot", "?")
        try:
            outcome = DispenserOutcome(state)
        except ValueError:
            vend_log.info(f"DISPENSER: slot {slot}, state: {state}")
            return None

        if self._slot_mismatch(data):
            logger.warning(
                f"Ignoring dispenser outcome '{outcome.value}' for mismatched slot "
                f"{slot} (active sale is slot {self.sale.product.slot})"
            )
            return None

        reported_request_id = data.get("request_id")
        in_flight_request_id = self._request_id
        if (
            reported_request_id
            and in_flight_request_id
            and reported_request_id != in_flight_request_id
        ):
            # Review finding C2 (Copilot, PR #32): a mismatched id is a
            # stale/foreign report -- e.g. a late report from an earlier
            # sale on the same slot -- and must be ignored outright, not
            # merely logged and processed anyway.
            logger.warning(
                f"Ignoring dispenser report: request_id={reported_request_id!r} "
                f"does not match in-flight request_id={in_flight_request_id!r} "
                f"(slot {slot})"
            )
            return None

        mechanism = self._mechanism
        # Review finding C3 (Copilot, PR #32): door_open is a customer
        # success only for a mechanism that actually has a
        # (mechanism, door_open) mapping in OUTCOME_FAULTS -- bagged_ice,
        # which maps it to ICE-402 (the bag released but the trap door
        # never closed). A mechanism with no such mapping (water_fill)
        # must take the ordinary failed-vend path below instead, via the
        # unmapped-outcome fallback in the `except KeyError` branch
        # further down -- never a success, never ICE-402.
        door_open_is_success = (
            outcome is DispenserOutcome.door_open
            and _has_outcome_mapping(mechanism, DispenserOutcome.door_open)
        )
        if outcome is DispenserOutcome.complete or door_open_is_success:
            fault = FaultCode.ICE_402 if outcome is DispenserOutcome.door_open else None
            return DispenseReport(outcome=outcome, success=True, fault=fault)

        try:
            code = fault_for_outcome(mechanism, outcome)
        except KeyError:
            # A board mis-reporting for its own mechanism (e.g. a water
            # board sending `jam`, which only a bagged-ice slot can
            # report) -- fail safely with a generic error rather than let
            # the unmapped pair crash the caller. If the mechanism itself
            # is unknown (should not happen once `start` always sets it),
            # default to bagged_ice so this fallback lookup can never
            # itself KeyError.
            logger.error(
                f"No fault mapped for mechanism={mechanism!r} "
                f"outcome={outcome.value!r}; board may be mis-reporting for "
                "this mechanism -- falling back to a generic error"
            )
            code = fault_for_outcome(mechanism or "bagged_ice", DispenserOutcome.error)
        return DispenseReport(outcome=outcome, success=False, fault=code)

    def _slot_mismatch(self, data: dict) -> bool:
        """True if `data`'s reported slot doesn't match this sale's slot.

        A delayed/duplicate dispenser event (QoS 0, no dedup) for a slot
        other than the one this cycle is dispensing must not finalize or
        fault this sale. No mismatch is reported when the event carries
        no slot (nothing to compare against). Today's
        `_dispenser_event_slot_mismatch` body (controller/vmc.py), minus
        the "no active selection" case -- a `DispenseCycle` always has a
        sale.
        """
        reported_slot = data.get("slot")
        if reported_slot is None:
            return False
        return reported_slot != self.sale.product.slot

    async def record(self) -> bool:
        """Durably record this sale: first the `dispense` KPI event,
        then the `sales` row itself, off the event loop
        (`asyncio.to_thread`) so the loop is never blocked on the disk
        write. Both are skipped entirely when no recorder is attached.

        Today's `_record_sale` body (controller/vmc.py), minus the
        `pending_sale_shares` clearing -- `self.sale` is immutable and
        owned by the caller, which decides what happens to `shares` once
        this returns. Always returns normally: a storage problem must
        never fail the vend, only raise the alert-class `DATA-101`
        (ordinary insert failure, already journaled as a fallback by
        `record_sale` itself) or `PAY-104` (the insert *and* its journal
        fallback both failed -- the sale is recorded nowhere durable,
        and the on-disk "dispensing" snapshot is the only remaining
        record of it).

        Returns `True` when the sale is durably on disk or journaled --
        no recorder attached, the plain success path, and the `DATA-101`
        path all count, since the journal fallback already covers the
        insert failure -- in which case the caller may clear
        `sale.shares`. Returns `False` only on the `PAY-104` path, where
        the sale is recorded nowhere durable: the caller must keep
        `sale.shares` so the on-disk "dispensing" snapshot stays the
        sale's only record, for the operator's "Record as sale"
        recovery. The return value is the caller's sole signal for this
        decision -- `faults.has(FaultCode.PAY_104)` is unsound, since
        PAY-104 can already be standing from an earlier, unrelated
        incident while this sale records successfully.
        """
        recorder = self._recorder()
        if recorder is None:
            return True
        product = self.sale.product
        recorder.record("dispense", value=float(product.slot))

        # Price comes from the consumed shares, not `product.price`: the
        # product is the *live* catalog object, so an operator can edit
        # the price while this sale is mid-dispense. The shares are what
        # was actually deducted from escrow and must be what gets
        # recorded.
        methods = self.sale.shares or {"unknown": round(product.price, 2)}
        price = round(sum(methods.values()), 2)
        try:
            await asyncio.to_thread(
                recorder.record_sale,
                product.sku,
                product.name,
                product.slot,
                price,
                methods,
            )
        except SaleRecordingFailed:
            logger.exception(
                f"record_sale AND its journal fallback both failed for "
                f"sku={product.sku!r}; the sale is recorded nowhere durable. "
                "Raising PAY-104 and preserving pending_sale_shares so the "
                "on-disk session snapshot is not cleared -- it is the only "
                "remaining record of this sale."
            )
            self._set_transaction_certain(False)
            self._faults.raise_fault(
                FaultCode.PAY_104,
                outcome=f"sku={product.sku} price=${price:.2f} unrecorded",
            )
            return False
        except Exception:
            logger.exception(
                f"record_sale failed for sku={product.sku!r}; already journaled "
                "as fallback by record_sale itself -- raising DATA-101 and "
                "finishing the vend regardless"
            )
            self._faults.raise_fault(
                FaultCode.DATA_101,
                outcome=f"sku={product.sku} price=${price:.2f}",
            )
        return True

    def cancel(self) -> None:
        """Cancel the dispense-timeout timer, if still live. Today's
        `_cancel_dispense_timeout` body (controller/vmc.py)."""
        if self._timeout_task is not None and not self._timeout_task.done():
            self._timeout_task.cancel()
        self._timeout_task = None
