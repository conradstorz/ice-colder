"""`TestSaleRunner`: runs one simulated sale through the real FSM without
ever recording it as a production sale (system-tests design §2.3).

Extracted from ``controller.vmc.VMC`` (Task 10 of the vmc-reduction plan,
following Task 9's observer hooks and test-sale seams) as the tenth piece
carved off the VMC god object. ``run_test_sale`` moved out verbatim: it
reads the VMC only through its public surface --
``find_product``/``begin_test_sale``/``end_test_sale`` and the
``subscribe_state_change``/``subscribe_sale_settled`` observers -- plus the
``MaintenanceLease.test_run()`` bracket, the ``DispenserProfileGate`` for
the CFG-101 pre-check, the event recorder for the ``test_run`` log row, and
the shared ``TaskRunner`` for the event loop a test's completion ``Future``
is created on. ``TestSaleResult`` moved with it, unchanged.
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import TYPE_CHECKING
from uuid import uuid4

from controller.dispenser_gate import DispenserProfileGate
from controller.maintenance_lease import MaintenanceLease
from controller.sale_context import SaleContext
from controller.task_runner import TaskRunner

if TYPE_CHECKING:
    from controller.vmc import VMC


@dataclass
class TestSaleResult:
    """Outcome of one ``TestSaleRunner.run_test_sale()`` run (system-tests
    design §2.3).

    ``path`` is the sequence of FSM states visited, in order, from the
    state the machine was in when the run started through to the state it
    settled in -- captured live by ``run_test_sale``'s own local list via
    a ``subscribe_state_change`` observer (Task 9), not reconstructed
    after the fact.

    ``outcome`` is one of ``"dispensed"``, ``"vend_failed"``, ``"timeout"``.
    ``fault_code`` (a ``FaultCode.value`` string, e.g. ``"ICE-401"``) is set
    only when ``outcome == "vend_failed"`` -- it is always ``None`` for
    ``"dispensed"`` and for ``"timeout"``. A dispense timeout *internally*
    still runs the same ``vend_failed`` FSM transition with code
    ``PAY-102`` (see ``DispenseCycle._timed_out``/``on_dispense_failed``),
    but is kept as its own, distinct outcome here rather than folded into
    ``"vend_failed"``.

    ``run_id`` (Task 13b, system-tests design §4/§3) is the same id written
    onto the ``test_run`` log row's metadata below -- carrying it back on
    the result is what lets the Tests level's ``/tests/sale`` route render
    a Pass/Fail form that posts to ``/tests/runs/{run_id}/verdict`` without
    a second query, and is why a simulated sale's log row is verdictable at
    all (the pre-13b shape had no ``run_id``, so no verdict could ever be
    recorded against it).
    """

    sku: str
    path: list[str]
    outcome: str
    fault_code: str | None = None
    run_id: str | None = None


class TestSaleRunner:
    """Runs a simulated sale against a live ``VMC`` through its public
    surface, holding the maintenance lease for the duration (Task 10).

    Constructed with keyword-only ``vmc``, ``lease``, ``gate``, ``recorder``
    (a zero-arg callable returning the live ``EventRecorder`` or ``None``,
    mirroring ``VMC._recorder``), and ``tasks`` (the shared ``TaskRunner``,
    for its attached event loop).
    """

    def __init__(
        self,
        *,
        vmc: "VMC",
        lease: MaintenanceLease,
        gate: DispenserProfileGate,
        recorder: Callable[[], object | None],
        tasks: TaskRunner,
    ) -> None:
        self._vmc = vmc
        self._lease = lease
        self._gate = gate
        self._recorder = recorder
        self._tasks = tasks

    async def run_test_sale(
        self,
        sku: str,
        *,
        user_id: str | None = None,
        user_name: str | None = None,
    ) -> TestSaleResult:
        """Run one simulated sale through the real FSM without ever
        recording it as a production sale (system-tests design §2.3).

        Task 9 (observers and the test-sale seams) rewrote this onto three
        public VMC seams that know nothing about `run_test_sale`,
        `TestSaleResult`, or the idea of a "test sale" at all:
        `begin_test_sale`/`end_test_sale` (seed/select, then clean up) and
        the `subscribe_state_change`/`subscribe_sale_settled` observers.
        The FSM-states-visited path and the one-shot outcome `Future` used
        to be VMC instance attributes; they are now plain local variables
        of this call, populated by two observers subscribed for its
        duration and unsubscribed in its own `finally` below. Task 10
        moved this method out to its own `TestSaleRunner` verbatim.

        ``user_id``/``user_name`` (Task 13b, keyword-only, both default
        ``None`` so every pre-13b caller -- including tests/test_vmc_flows.py's
        TestRunTestSale, which calls this with only ``sku`` -- keeps working
        unchanged) identify who started the run for the ``test_run`` log row
        below and for the returned ``TestSaleResult.run_id``'s eventual
        verdict. The web route (``web_interface/routes/tests_level.py``'s
        ``POST /tests/sale``) is the only production caller that supplies
        them, from the authenticated ``Principal``.

        Requires the maintenance lease: wrapping the whole run in
        ``maintenance_test_run()`` is what enforces this -- its
        ``_maintenance_run_started`` raises ``RuntimeError`` when no lease
        is held, which is this method's refusal path. That also increments
        ``runs_in_flight`` for the duration, which is what stops the lease
        from being released out from under this run (system-tests design
        §2.2/§2.3).

        ``is_test`` is set on the sale itself (``self._sale.is_test``) by
        `begin_test_sale`, not derived from the lease, and is what
        ``on_dispenser_event``, ``on_dispense_failed`` and ``_fail_vend``
        consult to keep this run out of the production sales ledger and
        away from a real refund command -- so releasing or losing the
        lease mid-run cannot flip this sale to a production one. Task 10's
        rule that the lease cannot be released while a run is in flight is
        the belt to this braces.

        ``begin_test_sale`` deposits the product's price as one credit
        with method ``"test"`` -- the only credit ``deposit_funds``
        accepts during a lease -- then selects the product and lets the
        *normal* dispense path run unmodified: the real FSM transitions,
        the real dispatch of ``dispense`` through the command dispatcher,
        and the real dispense-completion / dispense-timeout handling.
        This method awaits whichever of the three terminal outcomes
        settles the sale via a one-shot ``Future`` that the
        `subscribe_sale_settled` observer below resolves.

        Whatever the outcome, ``end_test_sale`` clears escrow directly at
        the end -- never through ``request_refund``, which would publish a
        real ``cmd/payment/refund``: test money is not real money and this
        is not a refund (system-tests design §2.3).
        """
        product_index, product = self._vmc.find_product(sku)
        if product is None:
            raise ValueError(f"run_test_sale: unknown product sku {sku!r}")

        # Dispenser profiles (plan: dispenser profiles, Task 2): refuse a
        # test sale for a product with no valid profile before touching
        # anything else -- no deposit, no lease, no runs_in_flight. Gated
        # on profiles actually being wired, like every other dispenser-
        # profiles check here, so a VMC with none set (every pre-plan-2
        # test and fixture) behaves exactly as before.
        if self._gate.profiles is not None and self._gate.profile_for(product) is None:
            raise RuntimeError(
                f"{product.sku} has no valid dispenser profile (CFG-101); "
                "fix dispensers.toml"
            )

        # Minted once per call, up front, so both the test_run row below and
        # the returned TestSaleResult carry the SAME id -- one run_id per
        # simulated sale, generated here rather than by the caller.
        run_id = uuid4().hex

        # A single machine can only ever be mid one sale anyway -- the FSM
        # itself is single-sale by construction -- so a second, concurrent
        # simulated sale (a double-submit, or two browser tabs on the same
        # session; web_interface/routes/tests_level.py's
        # `_acquire_lease_or_refusal` deliberately lets a second command
        # through for a session that already holds the lease) is refused
        # outright by `begin_test_sale`'s own `_test_sale_in_progress`
        # guard below, rather than accommodated.
        with self._lease.test_run():
            path: list[str] = [self._vmc.state]
            loop = self._tasks.loop or asyncio.get_running_loop()
            waiter: asyncio.Future = loop.create_future()

            def _on_state_change(state: str) -> None:
                # _fail_vend's "no sellable products" branch forces idle
                # via machine.set_state(), which (like _expire_session's
                # own use of it elsewhere) bypasses after_state_change, so
                # that particular transition is never observed here --
                # the explicit append below (after `await waiter`) covers
                # it, exactly as it always has.
                path.append(state)

            def _on_sale_settled(
                sale: SaleContext, outcome: str, fault_code: str | None
            ) -> None:
                if not waiter.done():
                    waiter.set_result((outcome, fault_code))

            unsubscribe_state = self._vmc.subscribe_state_change(_on_state_change)
            unsubscribe_settled = self._vmc.subscribe_sale_settled(_on_sale_settled)
            started_at = time.time()
            # Tracks whether `begin_test_sale` actually ran (returned,
            # rather than raising its own "already in progress" guard) --
            # only then does THIS call own any state that `end_test_sale`
            # would need to clean up. If `begin_test_sale` raises that
            # guard, this call seeded nothing, deposited nothing, and
            # never touched `_test_sale_in_progress`; calling
            # `end_test_sale` anyway would clear the OTHER, still-running
            # call's sale/escrow/guard out from under it -- so the
            # `finally` below only calls it when `began` is True.
            began = False
            try:
                selected = self._vmc.begin_test_sale(product)
                began = True
                if not selected:
                    raise RuntimeError(
                        f"run_test_sale: could not select {sku!r} for a "
                        f"test sale (locked out, unavailable, or sold "
                        f"out; state={self._vmc.state!r})"
                    )
                outcome, fault_code = await waiter
                if not path or path[-1] != self._vmc.state:
                    path.append(self._vmc.state)
                recorder = self._recorder()
                if recorder is not None:
                    # The seam a later task's recorder-side work fills in
                    # (system-tests design §4): this already lands in
                    # the existing `events` table via the same generic
                    # `record()` every other event type uses (`dispense`,
                    # `vend_failed`, `refund`, ...); nothing in
                    # services/event_recorder.py needed changing for
                    # that.
                    #
                    # Task 13b extended this metadata dict (originally
                    # just sku/outcome/fault_code/path) with run_id/
                    # user_id/user_name/subsystem/command/params/status/
                    # checks/verdict/note so this row renders through
                    # the SAME tests_log.html branch and is reachable by
                    # POST /tests/runs/{run_id}/verdict -- the pre-13b
                    # shape had no run_id, so a simulated sale's row
                    # could never be verdicted at all (see
                    # TestSaleResult's docstring and this method's own
                    # docstring). This is the ONLY place a simulated
                    # sale writes a test_run row -- one row per call,
                    # never a second write from the route side
                    # (web_interface/routes/tests_level.py's
                    # POST /tests/sale reuses this result's `run_id`
                    # rather than writing its own row).
                    recorder.record(
                        "test_run",
                        value=round(time.time() - started_at, 3),
                        metadata={
                            "run_id": run_id,
                            "user_id": user_id,
                            "user_name": user_name,
                            "subsystem": None,
                            "command": "simulated_sale",
                            "params": {"sku": sku},
                            "status": "ok" if outcome == "dispensed" else "failed",
                            "checks": None,
                            "verdict": None,
                            "note": None,
                            "sku": sku,
                            "outcome": outcome,
                            "fault_code": fault_code,
                            "path": path,
                        },
                    )
            finally:
                unsubscribe_state()
                unsubscribe_settled()
                if began:
                    self._vmc.end_test_sale()

        return TestSaleResult(
            sku=sku,
            path=path,
            outcome=outcome,
            fault_code=fault_code,
            run_id=run_id,
        )
