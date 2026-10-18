# ice_sims/vending/dispense.py
"""Dispense-profile execution for the vending simulator.

Runs a slot's full `DispenseCommand` (bagged-ice agitate/fill/release or
water fill) against the hardware through a `BoardPort`, never through the
simulator class itself.
"""

import asyncio
from collections.abc import Awaitable, Callable
from dataclasses import dataclass

from ice_contracts.board.dispenser_schema import (
    Accessory,
    IceFillBySensor,
    ReleaseByCatch,
    ReleaseBySensor,
    WaterFillByVolume,
    drive_channels,
    sense_channels,
)
from ice_contracts.board.ice_maker_monitor import ChannelReading
from ice_contracts.board.mqtt_messages import DispenseCommand, DispenserStatus
from ice_contracts.board.vending_machine import DispenserOutcome, DispenseStep
from ice_sims.vending.board import BoardPort
from ice_sims.vending.channels import INPUT_CHANNELS, OUTPUT_CHANNELS

# Bagged ice runs all three steps in order; water only ever runs "fill".
_BAGGED_ICE_STEPS: tuple[str, ...] = (
    DispenseStep.agitate.value,
    DispenseStep.fill.value,
    DispenseStep.release.value,
)
_WATER_STEPS: tuple[str, ...] = (DispenseStep.fill.value,)

# Stall-current reading put in the terminal report's `detail` when
# `motor_stall` is injected on a motor whose profile says its current sense
# is "unmonitored" (the IM600XL has no current channels, so this board never
# publishes current telemetry; the fault is a simulator-level injection and
# there is still a plausible number to report).
_DEFAULT_STALL_AMPS = 10.0

# Number of pulse increments a `flow_volume` water fill publishes over its
# nominal run (review finding I3) -- at least 4, per the brief.
_WATER_FLOW_INCREMENTS = 4

_ML_PER_GALLON = 3785.411784


@dataclass
class _RunContext:
    """Bundles everything `_run_bagged_ice`/`_run_water_fill` need from
    `execute_profile` so each helper takes exactly one argument instead
    of a long positional list (review finding M2): the triggering
    `DispenseCommand` (`profile`/`slot` below are read straight off it),
    this run's active-fault snapshot, and the io/publish closures
    `execute_profile` builds around its own per-run state (`driven_on`,
    `accessory_on`, the pending lag-off tasks)."""

    board: BoardPort
    cmd: DispenseCommand
    active: set[str]
    drive_on: Callable[[str], Awaitable[None]]
    drive_off: Callable[[str], Awaitable[None]]
    leave_on: Callable[[str], None]
    enter_step: Callable[[str], Awaitable[None]]
    exit_step: Callable[[str], None]
    publish_step: Callable[[str], Awaitable[None]]

    @property
    def profile(self):
        return self.cmd.profile

    @property
    def slot(self) -> int:
        return self.cmd.slot


class Dispenser:
    """Executes dispense profiles. Owns the one piece of cross-run state:
    the pending accessory lag-off tasks, keyed by channel, so a new run can
    find and cancel a *previous* run's lag-off (Copilot review PR #32,
    finding C4). Per-call collaborators (the `BoardPort`) are passed in."""

    def __init__(self) -> None:
        # Copilot review (PR #32) finding C4: an accessory's lag-off task
        # is keyed by channel and owned by this long-lived object, not by
        # whichever run's `execute_profile` call created it -- so a new
        # run's `_enter_step` can find and cancel a *previous* run's
        # still-pending lag-off for the same channel before it fires.
        self._pending_lag_off: dict[str, asyncio.Task] = {}

    @property
    def pending_lag_off(self) -> dict[str, asyncio.Task]:
        """Read-only view of the pending lag-off tasks, by channel."""
        return self._pending_lag_off

    @staticmethod
    def _accessory_span(
        accessory: Accessory, step_names: tuple[str, ...]
    ) -> tuple[str, str]:
        """The (first, last) step name this accessory is active for --
        `["all"]` spans the whole run; otherwise the earliest and latest
        of its own `on_during` entries in step order."""
        if accessory.on_during == ["all"]:
            return step_names[0], step_names[-1]
        present = [s for s in step_names if s in accessory.on_during]
        return present[0], present[-1]

    @staticmethod
    def _undeclared_channels(profile) -> str | None:
        """A detail naming every channel the profile drives that is not a
        declared output or senses that is not a declared input/counter, or
        None when the profile fits this board."""
        bad_drive = sorted(drive_channels(profile) - OUTPUT_CHANNELS)
        bad_sense = sorted(sense_channels(profile) - INPUT_CHANNELS)
        parts = []
        if bad_drive:
            parts.append("drives undeclared output channel " + ", ".join(bad_drive))
        if bad_sense:
            parts.append("senses undeclared input channel " + ", ".join(bad_sense))
        return "; ".join(parts) or None

    async def execute_profile(
        self,
        board: BoardPort,
        cmd: DispenseCommand,
        request_id: str | None = None,
    ) -> None:
        """Run the slot's full dispense profile and publish every step and
        the terminal outcome on `hardware/dispenser`.

        Replaces `_dispense_slot`/`_run_ice_dispense`/`_run_water_dispense`
        now that the VMC sends the slot's whole validated profile on the
        command channel instead of a bare slot number on the old,
        now-deleted dedicated dispense topic.
        `request_id` is the triggering `SubsystemCommand`'s own id (passed
        separately from `cmd`, a `DispenseCommand`, which has no such
        field of its own) so `DispenserStatus.request_id` can echo it, the
        same role it played for `_run_ice_dispense`/`_run_water_dispense`.

        Accessory handling: each accessory turns on the moment this run
        first reaches a step in its `on_during` (or immediately, for
        `["all"]`); when two or more accessories lead the *same* step they
        turn on concurrently and this sequence blocks once for the
        *longest* of their `lead_seconds` (review finding M1 -- they used
        to stack). Turning an accessory back off after its last relevant
        step is a background task (its `lag_seconds` delay must not hold
        up the rest of the sequence).

        Review finding I1: on a normal return (success or a fault
        outcome) those lag-off tasks are awaited, not cancelled, *after*
        the terminal report is published -- so a run reports `complete`
        the moment its last real step finishes, while `execute_profile`
        normally only returns once every accessory it turned on is
        actually off, lag included. Only a genuine exception or
        cancellation short-circuits that: then every pending lag-off task
        is cancelled and every output this run touched is forced off
        immediately, before the exception propagates -- there is no
        terminal report to publish in that case. Any accessory still
        sitting in `accessory_on` with no lag-off task of its own (a run
        that aborted before reaching that accessory's last step) is swept
        off directly in that path.

        Review finding C4 (Copilot, PR #32) qualifies the "actually off"
        half of that invariant: a lag-off task is keyed by channel on
        `self._pending_lag_off`, owned by this `Dispenser` rather
        than this run, so a *new* run's `_enter_step` can cancel a
        previous run's still-pending lag-off for the same channel the
        moment it turns that accessory back on (same slot, back-to-back
        sales, the old sale's lag window still running). When that
        happens this run's own end-of-run sweep deliberately skips that
        channel -- it was handed off to the new run, not actually turned
        off -- via `accessory_on - lag_scheduled` (a channel with a
        lag-off task is that task's responsibility, whether it ends up
        firing for real or losing the channel to a newer run). So on the
        success path `execute_profile` can return with an accessory
        still on, if and only if a later run has since claimed it; the
        failure path's forced sweep stays unconditional, since no later
        run can exist yet before this run's own terminal report is ever
        published.

        `water_valve_stuck_open` deliberately leaves the valve energised
        (its own `on_activate` already did this, independent of any
        particular run), so the sweep below skips the water-valve channel
        (and only that channel) while it is active.

        Every output this run drove, the trap-door catch included, is in
        `driven_on` until it is explicitly driven off, so every exit path
        (success, a fault outcome, an exception) de-energizes it before
        the terminal report goes out.
        """
        profile = cmd.profile
        slot = cmd.slot
        active = board.active_faults()

        # A real board cannot drive a channel it does not have. The edge's
        # capability cross-check (CFG-101) keeps such a profile from ever
        # being sent, so this is defensive; nothing is driven or published
        # before it.
        undeclared = self._undeclared_channels(profile)
        if undeclared:
            await board.publish(
                "hardware/dispenser",
                DispenserStatus(
                    slot=slot,
                    state=DispenserOutcome.error.value,
                    request_id=request_id,
                    detail=undeclared,
                ),
            )
            return

        step_names = (
            _BAGGED_ICE_STEPS if profile.mechanism == "bagged_ice" else _WATER_STEPS
        )
        spans = {
            name: self._accessory_span(accessory, step_names)
            for name, accessory in profile.accessories.items()
        }
        accessory_on: set[str] = set()
        accessory_tasks: list[asyncio.Task] = []
        # Channels this run has scheduled a lag-off task for (finding C4)
        # -- disposing of them is that task's job, not the end-of-run
        # sweep's; see `_turn_off_later`/`_exit_step` and the sweep below.
        lag_scheduled: set[str] = set()
        driven_on: set[str] = set()

        async def _drive_on(channel: str) -> None:
            await board.set_hw(channel, True)
            driven_on.add(channel)

        async def _drive_off(channel: str) -> None:
            await board.set_hw(channel, False)
            driven_on.discard(channel)

        def _leave_on(channel: str) -> None:
            """Stop tracking `channel` for the end-of-run sweep without
            touching hardware -- used when a fault deliberately keeps an
            output energised past this run (review finding I2)."""
            driven_on.discard(channel)

        async def _turn_off_later(channel: str, lag_seconds: float) -> None:
            try:
                await board.sleep(lag_seconds)
                await board.set_hw(channel, False)
                accessory_on.discard(channel)
            finally:
                # Self-removal (finding C4): identity-checked so this
                # only clears *this* task's own entry -- a cancellation
                # delivered because a newer run's `_enter_step` already
                # popped and replaced it must never delete that newer
                # task instead.
                me = asyncio.current_task()
                if self._pending_lag_off.get(channel) is me:
                    del self._pending_lag_off[channel]

        async def _enter_step(step: str) -> None:
            leads: list[float] = []
            for name, accessory in profile.accessories.items():
                first, _last = spans[name]
                if first == step and accessory.channel not in accessory_on:
                    # Finding C4: a previous run's lag-off for this same
                    # channel, still pending, must never fire once this
                    # run has claimed the channel back.
                    stale = self._pending_lag_off.pop(accessory.channel, None)
                    if stale is not None:
                        stale.cancel()
                    await board.set_hw(accessory.channel, True)
                    accessory_on.add(accessory.channel)
                    leads.append(accessory.lead_seconds)
            if leads:
                # Review finding M1: two accessories leading the same step
                # overlap (one wait for the longest lead) instead of
                # stacking (a wait per accessory).
                await board.sleep(max(leads))

        def _exit_step(step: str) -> None:
            for name, accessory in profile.accessories.items():
                _first, last = spans[name]
                if last == step and accessory.channel in accessory_on:
                    task = asyncio.get_running_loop().create_task(
                        _turn_off_later(accessory.channel, accessory.lag_seconds)
                    )
                    accessory_tasks.append(task)
                    lag_scheduled.add(accessory.channel)
                    self._pending_lag_off[accessory.channel] = task

        async def _publish_step(step: str) -> None:
            await board.publish(
                "hardware/dispenser",
                DispenserStatus(slot=slot, state=step, request_id=request_id),
            )

        ctx = _RunContext(
            board=board,
            cmd=cmd,
            active=active,
            drive_on=_drive_on,
            drive_off=_drive_off,
            leave_on=_leave_on,
            enter_step=_enter_step,
            exit_step=_exit_step,
            publish_step=_publish_step,
        )

        outcome: DispenserOutcome
        detail: str | None

        # The stuck-open injection exempts only the water valve from the
        # sweep; every other driven channel (the trap-door catch included)
        # is always de-energised.
        kept_on: set[str] = set()
        if profile.mechanism != "bagged_ice" and "water_valve_stuck_open" in active:
            kept_on.add(profile.fill.valve_channel)

        try:
            if profile.mechanism == "bagged_ice":
                outcome, detail = await self._run_bagged_ice(ctx)
            else:
                outcome, detail = await self._run_water_fill(ctx)
        except BaseException:
            for task in accessory_tasks:
                task.cancel()
            if accessory_tasks:
                await asyncio.gather(*accessory_tasks, return_exceptions=True)
            for channel in list(driven_on - kept_on):
                await board.set_hw(channel, False)
            for channel in list(accessory_on):
                await board.set_hw(channel, False)
            raise

        for channel in list(driven_on - kept_on):
            await board.set_hw(channel, False)

        if (
            outcome is DispenserOutcome.complete
            and profile.mechanism == "water_fill"
            and isinstance(profile.fill, WaterFillByVolume)
        ):
            await board.draw_water(profile.fill.target_volume_ml / _ML_PER_GALLON)

        await board.publish(
            "hardware/dispenser",
            DispenserStatus(
                slot=slot, state=outcome.value, request_id=request_id, detail=detail
            ),
        )

        if accessory_tasks:
            await asyncio.gather(*accessory_tasks, return_exceptions=True)
        # Finding C4: a channel with a lag-off task is that task's own
        # responsibility to dispose of -- either it already fired and
        # discarded itself from accessory_on above, or it was cancelled
        # by a newer run's _enter_step, which means the channel now
        # belongs to that run and this sweep must leave it alone.
        for channel in list(accessory_on - lag_scheduled):
            await board.set_hw(channel, False)

    async def _run_bagged_ice(
        self, ctx: "_RunContext"
    ) -> tuple[DispenserOutcome, str | None]:
        """agitate -> fill -> release. Returns (outcome, detail)."""
        profile = ctx.profile
        active = ctx.active
        release = profile.release

        # ReleaseByCatch: energize the catch before any motion of the
        # sequence and hold it through the fill. It sits in `driven_on`, so
        # every exit path below (and an exception) de-energizes it.
        if isinstance(release, ReleaseByCatch):
            await ctx.drive_on(release.catch_channel)

        # --- agitate ---
        await ctx.enter_step(DispenseStep.agitate.value)
        await ctx.publish_step(DispenseStep.agitate.value)
        agitate = profile.agitate
        await ctx.drive_on(agitate.motor_channel)
        if "motor_stall" in active:
            amps = (
                agitate.stall_current_amps + 1.0
                if agitate.stall_current_amps != "unmonitored"
                else _DEFAULT_STALL_AMPS
            )
            await ctx.drive_off(agitate.motor_channel)
            ctx.exit_step(DispenseStep.agitate.value)
            return DispenserOutcome.error, f"stall {amps:.1f} A"
        await ctx.board.sleep(agitate.run_seconds)
        await ctx.drive_off(agitate.motor_channel)
        ctx.exit_step(DispenseStep.agitate.value)

        # --- fill ---
        await ctx.enter_step(DispenseStep.fill.value)
        await ctx.publish_step(DispenseStep.fill.value)
        fill = profile.fill
        await ctx.drive_on(fill.motor_channel)

        if "ice_bin_empty" in active:
            # No ice to move: the fill runs to its maximum time and the
            # board reports an empty bin. No bin channel is touched.
            await ctx.board.sleep(fill.max_run_seconds)
            await ctx.drive_off(fill.motor_channel)
            ctx.exit_step(DispenseStep.fill.value)
            return DispenserOutcome.bin_empty, None

        if isinstance(fill, IceFillBySensor):
            if "auger_jam" in active:
                await ctx.board.sleep(fill.max_run_seconds)
                await ctx.drive_off(fill.motor_channel)
                ctx.exit_step(DispenseStep.fill.value)
                return DispenserOutcome.timeout, None
            duration = min(6.0, fill.max_run_seconds / 2)
            await ctx.board.sleep(duration)
            await ctx.board.set_hw(fill.sensor_channel, True)
        else:  # IceFillTimed
            await ctx.board.sleep(fill.max_run_seconds)
        await ctx.drive_off(fill.motor_channel)
        ctx.exit_step(DispenseStep.fill.value)

        # --- release ---
        await ctx.enter_step(DispenseStep.release.value)
        await ctx.publish_step(DispenseStep.release.value)
        if isinstance(release, ReleaseByCatch):
            # De-energize to drop the bag, then give the door time to fall
            # open and close itself.
            await ctx.drive_off(release.catch_channel)
            await ctx.board.sleep(release.drop_seconds)
        else:
            await ctx.drive_on(release.solenoid_channel)
            await ctx.board.sleep(release.pulse_seconds)
            await ctx.drive_off(release.solenoid_channel)
            if isinstance(release, ReleaseBySensor):
                await ctx.board.set_hw(release.sensor_channel, True)
                await ctx.board.sleep(1.0)
                await ctx.board.set_hw(release.sensor_channel, False)

        # The bag has dropped: the bag-full sensor clears.
        if isinstance(fill, IceFillBySensor):
            await ctx.board.set_hw(fill.sensor_channel, False)

        ctx.exit_step(DispenseStep.release.value)
        return DispenserOutcome.complete, None

    async def _publish_fill_pulses(self, board: BoardPort, pulses: float) -> None:
        """Publish the flow meter's cumulative pulse count for one
        `flow_volume` fill on the generic telemetry path, on the dedicated
        `fill_pulses` channel. This is the board's only volume metering:
        nothing publishes `sensors/water_flow` any more (spec §3.4)."""
        await board.publish(
            f"telemetry/{board.subsystem_name}/fill_pulses",
            ChannelReading(channel_id="fill_pulses", value=pulses),
        )

    async def _pump_water_pulses(
        self, board: BoardPort, fill: WaterFillByVolume, mode: str
    ) -> tuple[float, float, float, float]:
        """Simulate flow-meter pulses for one `flow_volume` fill (review
        finding I3), publishing the running count on the `fill_pulses`
        telemetry channel in at least `_WATER_FLOW_INCREMENTS` steps.

        Copilot review (PR #32) finding C7: every mode now has an actual
        simulated flow *rate*, in ml/s, so `min_flow_ml_per_second` can
        be consulted against something real rather than never consulted
        at all:

        - `mode="normal"`/`"runaway"`: the same nominal rate as before
          (`target_volume_ml / min(8.0, max_fill_seconds / 2)`),
          unrelated to `min_flow_ml_per_second` (always comfortably
          above it for any sane profile). `"normal"` stops exactly at
          `target_volume_ml`'s pulses; `"runaway"` keeps going past that
          until the volume exceeds `over_dispense_percent`'s tolerance.
        - `mode="slow"` (the `slow_flow` fault): rate = `1.05 ×
          min_flow_ml_per_second` -- deliberately just *above* the
          floor, so it never trips `no_flow`. Runs for up to
          `max_fill_seconds`, stopping early at `target_volume_ml` like
          `"normal"` does: whether this profile's `target_volume_ml` is
          reachable at that modest rate within `max_fill_seconds`
          decides `complete` vs. `timeout` (both are now real outcomes
          for this fault, not just `timeout`).
        - `mode="low"` (the `low_flow` fault): rate = `0.5 ×
          min_flow_ml_per_second` -- deliberately *below* the floor.
          Runs for only `no_flow_grace_seconds` (not the full
          `max_fill_seconds`) before returning, so the caller can fault
          `no_flow` promptly, exactly as `no_water_flow`'s hardcoded
          zero-rate path already does.

        Returns `(pulses, elapsed_seconds, target_pulses, rate_ml_s)` --
        `_run_water_fill` compares `rate_ml_s` against
        `fill.min_flow_ml_per_second` itself, once, rather than this
        method deciding the outcome.
        """
        target_pulses = fill.target_volume_ml / 1000.0 * fill.pulses_per_liter
        tolerance_pulses = target_pulses * (1 + fill.over_dispense_percent / 100.0)
        nominal_seconds = min(8.0, fill.max_fill_seconds / 2.0)

        if mode == "low":
            rate_ml_s = 0.5 * fill.min_flow_ml_per_second
            dt = fill.no_flow_grace_seconds / _WATER_FLOW_INCREMENTS
            run_seconds = fill.no_flow_grace_seconds
        elif mode == "slow":
            rate_ml_s = 1.05 * fill.min_flow_ml_per_second
            dt = fill.max_fill_seconds / _WATER_FLOW_INCREMENTS
            run_seconds = fill.max_fill_seconds
        else:
            rate_ml_s = fill.target_volume_ml / nominal_seconds
            dt = nominal_seconds / _WATER_FLOW_INCREMENTS
            run_seconds = fill.max_fill_seconds

        pulse_rate = rate_ml_s / 1000.0 * fill.pulses_per_liter  # pulses/second
        step_pulses = pulse_rate * dt

        pulses = 0.0
        elapsed = 0.0
        while elapsed < run_seconds:
            await board.sleep(dt)
            pulses += step_pulses
            elapsed += dt
            await self._publish_fill_pulses(board, pulses)

            if mode in ("normal", "slow") and pulses >= target_pulses:
                pulses = target_pulses
                break
            if mode == "runaway" and pulses > tolerance_pulses:
                break

        return pulses, elapsed, target_pulses, rate_ml_s

    async def _run_water_fill(
        self, ctx: "_RunContext"
    ) -> tuple[DispenserOutcome, str | None]:
        """fill only. Returns (outcome, detail)."""
        profile = ctx.profile
        active = ctx.active
        await ctx.enter_step(DispenseStep.fill.value)
        await ctx.publish_step(DispenseStep.fill.value)
        fill = profile.fill
        await ctx.drive_on(fill.valve_channel)

        if isinstance(fill, WaterFillByVolume):
            if "no_water_flow" in active:
                await ctx.board.sleep(fill.no_flow_grace_seconds)
                await ctx.drive_off(fill.valve_channel)
                ctx.exit_step(DispenseStep.fill.value)
                return (
                    DispenserOutcome.no_flow,
                    f"no flow after {fill.no_flow_grace_seconds} s",
                )

            if "flow_runaway" in active:
                mode = "runaway"
            elif "slow_flow" in active:
                mode = "slow"
            elif "low_flow" in active:
                mode = "low"
            else:
                mode = "normal"

            pulses, elapsed, target_pulses, rate_ml_s = await self._pump_water_pulses(
                ctx.board, fill, mode
            )
            tolerance_pulses = target_pulses * (1 + fill.over_dispense_percent / 100.0)
            volume_ml = pulses / fill.pulses_per_liter * 1000.0

            if "water_valve_stuck_open" in active:
                # The valve stays energised (see execute_profile's
                # docstring) -- matches the fault's existing effect.
                ctx.exit_step(DispenseStep.fill.value)
                return DispenserOutcome.complete, None

            await ctx.drive_off(fill.valve_channel)
            ctx.exit_step(DispenseStep.fill.value)

            # Finding C7: min_flow_ml_per_second, consulted directly
            # against the rate _pump_water_pulses actually simulated --
            # below the floor for longer than no_flow_grace_seconds
            # (which is exactly how long mode="low" ever runs) is a
            # no_flow fault, same family as no_water_flow's hardcoded
            # zero-rate path above, just reached through the general
            # rate check instead of a special case.
            if rate_ml_s < fill.min_flow_ml_per_second:
                return (
                    DispenserOutcome.no_flow,
                    f"flow {rate_ml_s:.1f} ml/s below minimum "
                    f"{fill.min_flow_ml_per_second:.1f} ml/s for "
                    f"{fill.no_flow_grace_seconds:.1f} s",
                )
            if mode == "runaway" and pulses > tolerance_pulses:
                return (
                    DispenserOutcome.over_dispense,
                    f"{volume_ml:.0f} ml dispensed, target {fill.target_volume_ml:.0f} ml",
                )
            if elapsed >= fill.max_fill_seconds and pulses < target_pulses:
                return DispenserOutcome.timeout, None
            return DispenserOutcome.complete, None

        # WaterFillTimed -- unchanged.
        await ctx.board.sleep(fill.max_fill_seconds)

        if "water_valve_stuck_open" in active:
            ctx.exit_step(DispenseStep.fill.value)
            return DispenserOutcome.complete, None

        await ctx.drive_off(fill.valve_channel)
        ctx.exit_step(DispenseStep.fill.value)
        return DispenserOutcome.complete, None
