"""Maintenance lease lifecycle: the hold, its idle timer, and the standby
session-liveness sweep.

Extracted from ``controller.vmc.VMC`` as the sixth and last piece carved off
the VMC god object, following ``controller/fault_registry.py``'s
``FaultRegistry``, ``controller/escrow_ledger.py``'s ``EscrowLedger``,
``controller/refund_protocol.py``'s ``RefundProtocol`` and
``controller/session_recovery.py``'s ``SessionRecovery`` (see ``CLAUDE.md``'s
"FSM Core" section). ``MaintenanceLease`` owns the lease itself
(``MaintenanceHold``), its idle timer, the standby sweep, and the
grant/release/takeover/run accounting.

It knows nothing about the FSM, escrow, or MQTT: ``schedule``, ``on_granted``,
``on_released`` and the three timing knobs (``idle_timeout``,
``takeover_idle``, ``sweep_seconds``) are injected callables (the VMC passes
``self._schedule``, a closure that raises ``SVC-102``, and a closure that
calls ``self.clear_fault``), read at call time rather than snapshotted at
construction -- tests (and, in principle, an operator) set
``vmc.MAINTENANCE_IDLE_TIMEOUT_SECONDS``/``vmc.MAINTENANCE_TAKEOVER_IDLE_SECONDS``
on the live VMC instance after it is built, and a later read must see the
current value. The FSM preconditions in ``VMC.begin_maintenance``/
``VMC.begin_standby``, the refunds, ``VMC.run_test_sale`` itself, and
raising/clearing ``SVC-102`` all stay on the VMC.
"""

from __future__ import annotations

import time
from collections.abc import Callable
from contextlib import contextmanager
from dataclasses import dataclass

from loguru import logger


@dataclass
class MaintenanceHold:
    """A lease that takes the machine out of service for operator testing
    (system-tests design §2.2).

    Never persisted (§6): it lives only on the live ``MaintenanceLease``
    (in turn held by the live VMC instance), so a restart clears it,
    matching the FSM's own reset semantics. It is not part of
    ``SessionSnapshot`` / ``services/session_store.py`` and must stay that
    way.

    ``runs_in_flight`` and ``release_requested`` are what keep a release
    (explicit, or from the idle timer) from happening out from under an
    in-progress test run: see ``MaintenanceLease.request_release``,
    ``MaintenanceLease.idle_expired`` and ``MaintenanceLease.run_finished``.
    """

    holder_user_id: str
    holder_session_id: str
    started_at: float
    last_activity_at: float
    runs_in_flight: int = 0
    release_requested: bool = False
    #: The ``by`` reason a deferred release should ultimately be logged and
    #: cleared with -- set alongside ``release_requested`` at every site
    #: that defers ("admin" from request_release, "idle_timeout" from
    #: idle_expired, "session_ended" from sweep_tick) and read by
    #: run_finished so a session-ended or idle-timeout release isn't
    #: misattributed to "admin" once the in-flight run settles. Reset to
    #: None wherever release_requested is reset to False (take_over).
    release_reason: str | None = None
    #: A standby lease (system-tests design §2.2a) is taken explicitly by a
    #: tech to make a busy machine idle and hold it out of service for the
    #: whole of their login. It differs from the opportunistic lease in
    #: exactly two ways: no idle-timer release (MaintenanceLease.idle_expired
    #: is a no-op for it), and it is swept every
    #: VMC.STANDBY_SWEEP_SECONDS against the holder's own session liveness
    #: instead. See VMC.begin_standby.
    standby: bool = False


class MaintenanceLease:
    """Holds the maintenance-lease state the VMC used to keep on itself.

    ``idle_timeout``/``takeover_idle``/``sweep_seconds`` are callables, not
    plain values, read at call time -- see the module docstring. ``hold``,
    ``idle_task``, ``sweep_task`` and ``session_liveness`` are public
    attributes (rather than private with accessors) because the VMC exposes
    read-only aliases for some of them that existing tests read directly.
    """

    def __init__(
        self,
        *,
        schedule: Callable[[float, Callable[[], None]], object | None],
        on_granted: Callable[[], None],
        on_released: Callable[[str], None],
        idle_timeout: Callable[[], float],
        takeover_idle: Callable[[], float],
        sweep_seconds: Callable[[], float],
    ) -> None:
        self._schedule = schedule
        self._on_granted = on_granted
        self._on_released = on_released
        self._idle_timeout = idle_timeout
        self._takeover_idle = takeover_idle
        self._sweep_seconds = sweep_seconds
        self.hold: MaintenanceHold | None = None
        self.idle_task = None
        self.sweep_task = None
        # Wired (or cleared) via set_session_liveness; None means "not
        # wired", in which case a standby lease falls back to the ordinary
        # idle timer (see arm_sweep). _no_predicate_warned logs that
        # fallback once rather than on every standby grant.
        self.session_liveness: Callable[[str], bool] | None = None
        self._no_predicate_warned = False

    def grant(self, user_id: str, session_id: str, *, standby: bool) -> None:
        """Build a fresh hold, tell the caller to raise SVC-102, and arm
        the matching timer -- the idle timer for an opportunistic lease,
        the session sweep for a standby one."""
        now = time.time()
        self.hold = MaintenanceHold(
            holder_user_id=user_id,
            holder_session_id=session_id,
            started_at=now,
            last_activity_at=now,
            standby=standby,
        )
        self._on_granted()
        if standby:
            self.arm_sweep()
            logger.info(
                f"Maintenance standby lease granted to user={user_id} "
                f"session={session_id}"
            )
        else:
            self.arm_idle_timer()
            logger.info(
                f"Maintenance lease granted to user={user_id} session={session_id}"
            )

    def upgrade_to_standby(self, user_id: str, session_id: str) -> None:
        """Upgrade the already-held lease (same holder session) to standby
        in place: swap its idle timer for the session sweep."""
        hold = self.hold
        hold.standby = True
        if self.idle_task and not self.idle_task.done():
            self.idle_task.cancel()
        self.idle_task = None
        self.arm_sweep()
        logger.info(
            f"Maintenance lease upgraded to standby by user={user_id} "
            f"session={session_id}"
        )

    def release(self, by: str) -> None:
        """Actually drop the lease: cancel both timers, clear the hold, and
        tell the caller to clear SVC-102.

        Every caller (``request_release``, ``idle_expired``,
        ``sweep_tick``, ``run_finished``) has already confirmed
        ``runs_in_flight == 0`` before reaching here; this does not check
        it again.
        """
        if self.idle_task and not self.idle_task.done():
            self.idle_task.cancel()
        self.idle_task = None
        if self.sweep_task and not self.sweep_task.done():
            self.sweep_task.cancel()
        self.sweep_task = None
        self.hold = None
        self._on_released(by)
        logger.info(f"Maintenance lease released ({by})")

    def arm_idle_timer(self) -> None:
        if self.idle_task and not self.idle_task.done():
            self.idle_task.cancel()
        self.idle_task = self._schedule(self._idle_timeout(), self.idle_expired)

    def idle_expired(self) -> None:
        """5 minutes since ``last_activity_at``: behaves exactly like a
        release request. Never clears the lease while a run is in flight --
        it only sets ``release_requested`` for that run's own completion
        to act on.

        A standby lease is never released by this timer while a liveness
        predicate is wired -- the sweep owns its lifetime and the idle
        timer is never armed for it (``grant`` arms the sweep instead);
        this checks ``hold.standby`` too, belt and braces, in case a
        future caller re-arms it by mistake. With NO predicate wired
        (``arm_sweep``'s fallback), the idle timer IS the standby lease's
        only automatic release, so it must act.
        """
        hold = self.hold
        if hold is None:
            return
        if hold.standby and self.session_liveness is not None:
            return
        if hold.runs_in_flight > 0:
            hold.release_requested = True
            hold.release_reason = "idle_timeout"
            logger.info("Maintenance lease idle timeout with a run in flight; deferred")
            return
        logger.info("Maintenance lease idle for 5 minutes; releasing")
        self.release(by="idle_timeout")

    def set_session_liveness(self, predicate: Callable[[str], bool] | None) -> None:
        """Wire (or clear) the predicate the standby sweep uses to check the
        holder's web session (system-tests design §2.2a).

        ``predicate(session_id) -> bool`` should apply the same rules
        ``AccessStore.resolve_session`` would, without refreshing the
        session's own activity -- ``AccessStore.session_is_live`` is the
        intended implementation, wired in main.py. With no predicate wired
        (the default), a standby lease falls back to the ordinary idle
        timer instead of the sweep -- see ``arm_sweep``.
        """
        self.session_liveness = predicate

    def arm_sweep(self) -> None:
        """Arm (or re-arm) the standby lease's session-liveness sweep.

        With no liveness predicate wired, degrades to the ordinary idle
        timer instead (an opportunistic-style release) and logs the
        fallback once, not on every standby grant/takeover.
        """
        if self.sweep_task and not self.sweep_task.done():
            self.sweep_task.cancel()
        self.sweep_task = None
        if self.session_liveness is None:
            if not self._no_predicate_warned:
                logger.warning(
                    "Standby lease granted with no session-liveness predicate "
                    "wired; falling back to the idle timer (see "
                    "VMC.set_session_liveness)"
                )
                self._no_predicate_warned = True
            self.arm_idle_timer()
            return
        self.sweep_task = self._schedule(self._sweep_seconds(), self.sweep_tick)

    def sweep_tick(self) -> None:
        """One tick of the standby sweep: re-arms itself (a repeating chain
        via ``_schedule``) as long as a standby lease exists and its
        holder's session is still live.

        Mirrors ``idle_expired``'s in-flight deferral: when the session is
        gone but a run is in flight, this sets ``release_requested`` for
        that run's own completion to act on (``run_finished``) rather than
        releasing here, and does not keep chaining -- the run's completion
        is what releases it.
        """
        hold = self.hold
        if hold is None or not hold.standby:
            return
        if self.session_liveness is not None and self.session_liveness(
            hold.holder_session_id
        ):
            self.sweep_task = self._schedule(self._sweep_seconds(), self.sweep_tick)
            return
        if hold.runs_in_flight > 0:
            hold.release_requested = True
            hold.release_reason = "session_ended"
            logger.info(
                "Standby sweep found holder session gone with a run in flight; deferred"
            )
            return
        logger.info("Standby sweep found holder session gone; releasing")
        self.release(by="session_ended")

    def request_release(self, session_id: str) -> bool:
        """Release the lease for its holder's session only.

        Returns False when there is no lease, or ``session_id`` is not its
        holder (refused either way). Returns True whenever the request is
        accepted -- either released immediately (``runs_in_flight == 0``),
        or deferred via ``release_requested`` for the last in-flight run to
        perform (``run_finished``).
        """
        hold = self.hold
        if hold is None or hold.holder_session_id != session_id:
            return False
        if hold.runs_in_flight > 0:
            hold.release_requested = True
            hold.release_reason = "admin"
            logger.info(
                f"Maintenance release requested by session={session_id}; "
                f"deferred, {hold.runs_in_flight} run(s) in flight"
            )
            return True
        self.release(by="admin")
        return True

    def take_over(self, user_id: str, session_id: str) -> tuple[bool, str | None]:
        """Transfer an idle, run-free lease to a new holder.

        Permitted only when no run is in flight and the lease has been idle
        (since ``last_activity_at``) for at least ``takeover_idle()``
        seconds; records who took it over by overwriting the hold's holder
        fields in place. A standby lease (§2.2a) stays standby across the
        takeover -- the sweep is re-armed for the new holder's session
        rather than the idle timer.
        """
        hold = self.hold
        if hold is None:
            return False, "no lease held"
        if hold.runs_in_flight > 0:
            return False, "a test is in flight"
        idle_for = time.time() - hold.last_activity_at
        if idle_for < self._takeover_idle():
            return False, "lease not yet idle"
        now = time.time()
        hold.holder_user_id = user_id
        hold.holder_session_id = session_id
        hold.started_at = now
        hold.last_activity_at = now
        hold.release_requested = False
        hold.release_reason = None
        if hold.standby:
            self.arm_sweep()
        else:
            self.arm_idle_timer()
        logger.info(
            f"Maintenance lease taken over by user={user_id} session={session_id}"
        )
        return True, None

    def run_started(self) -> None:
        """Run accounting, start: increments ``runs_in_flight`` and
        refreshes ``last_activity_at``. Raises if no lease is held -- a run
        cannot exist outside a lease. Called from ``test_run``'s entry;
        ``VMC.run_test_sale`` goes through that context manager rather than
        calling this directly.
        """
        hold = self.hold
        if hold is None:
            raise RuntimeError("no maintenance lease held")
        hold.runs_in_flight += 1
        hold.last_activity_at = time.time()
        self.arm_idle_timer()

    def run_finished(self) -> None:
        """Run accounting, end: decrements ``runs_in_flight`` and, once it
        reaches zero, performs a deferred release if one was requested
        (``request_release``, ``idle_expired`` or ``sweep_tick``). Always
        reached from ``test_run``'s ``finally`` so a failing or timed-out
        run still decrements -- a leak here pins the machine out of service
        until restart.
        """
        hold = self.hold
        if hold is None:
            return
        hold.runs_in_flight = max(0, hold.runs_in_flight - 1)
        if hold.runs_in_flight == 0 and hold.release_requested:
            logger.info("Last in-flight maintenance run settled; releasing lease")
            self.release(by=hold.release_reason or "admin")

    @contextmanager
    def test_run(self):
        """Bracket one test run against the lease.

        Increments ``runs_in_flight`` and refreshes ``last_activity_at`` on
        entry; decrements on exit via ``finally`` regardless of success,
        failure, or a timeout raised through the body -- so a run that
        fails still frees the lease's run count.
        """
        self.run_started()
        try:
            yield
        finally:
            self.run_finished()
