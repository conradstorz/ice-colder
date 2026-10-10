# controller/dispenser_gate.py
"""Dispenser-profile gating: extracted from ``controller.vmc.VMC`` as the
eighth piece carved off the VMC god object, following the same pattern as
``controller/fault_registry.py``'s ``FaultRegistry``,
``controller/escrow_ledger.py``'s ``EscrowLedger``,
``controller/refund_protocol.py``'s ``RefundProtocol``,
``controller/session_recovery.py``'s ``SessionRecovery``,
``controller/mqtt_inbound.py``'s ``TelemetryRouter``, and
``controller/maintenance_lease.py``'s ``MaintenanceLease`` (see
``CLAUDE.md``'s "FSM Core" section). ``DispenserProfileGate`` owns the
loaded ``DispenserProfiles`` object and the CFG-101/CFG-102 reconciliation
that keeps a product's lockout, and the machine's file-error fault, in
sync with whether dispensers.toml currently gives it a valid profile. It
knows nothing about MQTT, the FSM, or the event recorder:
``products``/``is_locked``/``has_machine_fault``/``raise_fault``/
``clear_fault`` are injected callables, read at call time -- never
snapshotted -- so a catalog edit or a fault change between calls is
always reflected, matching the other extracted collaborators' own
convention.
"""

from __future__ import annotations

from collections.abc import Callable

from loguru import logger

from config.config_model import Product
from contracts.vending_machine import FaultCode, SubsystemCapabilities
from services.dispenser_schema import MECHANISM_FOR_KIND, SlotProfile
from services.dispensers import DispenserProfiles


class DispenserProfileGate:
    """Holds the dispenser-profiles state the VMC used to keep on itself."""

    def __init__(
        self,
        *,
        products: Callable[[], list[Product]],
        is_locked: Callable[[str], FaultCode | None],
        has_machine_fault: Callable[[FaultCode], bool],
        raise_fault: Callable[..., None],
        clear_fault: Callable[[str, str], bool],
    ) -> None:
        self._products = products
        self._is_locked = is_locked
        self._has_machine_fault = has_machine_fault
        self._raise_fault = raise_fault
        self._clear_fault = clear_fault
        # None means "not wired" -- every method below that reads it is a
        # no-op/None in that case, so a VMC built without profiles (every
        # pre-plan-2 test fixture) behaves exactly as before.
        self.profiles: DispenserProfiles | None = None

    def attach(self, profiles: DispenserProfiles) -> None:
        """Attach the loaded `DispenserProfiles` (plan: dispenser
        profiles, Task 2) and immediately reconcile CFG-101/CFG-102
        against it, so a product with no valid profile is locked before
        this VMC ever accepts a selection for it.
        """
        self.profiles = profiles
        logger.debug("VMC attached dispenser profiles.")
        self.reconcile()

    def profile_for(self, product: Product) -> SlotProfile | None:
        """The one lookup every later task (dispense, Tests level, ...)
        uses: the product's own valid `SlotProfile`, or `None` when no
        profiles are wired, the product's `kind` has no mechanism (e.g.
        `"other"`), no table exists for its slot, or that table's
        `product_sku` doesn't match this product -- the same validity
        check `reconcile` locks products on, so the two can never
        disagree.

        Copilot review (PR #32) finding C1: also requires
        `profile.mechanism == MECHANISM_FOR_KIND[product.kind]` -- a
        profile left over at this slot from before a catalog edit changed
        this product's `kind` (e.g. ice -> water) must not be treated as
        valid just because the slot and sku still match.
        """
        if self.profiles is None:
            return None
        expected_mechanism = MECHANISM_FOR_KIND.get(product.kind)
        if expected_mechanism is None:
            return None
        profile = self.profiles.profile_for_slot(product.slot)
        if profile is None or profile.product_sku != product.sku:
            return None
        if profile.mechanism != expected_mechanism:
            return None
        return profile

    def reconcile(self) -> None:
        """Re-derive every product's CFG-101 lockout, and the machine's
        CFG-102 fault, from the currently loaded dispenser profiles.
        No-op when no profiles object is set.

        Idempotent, and never touches a lockout held by a different code:
        a sku already locked (for any reason) is left alone here --
        CFG-101 is raised only for a sku not locked at all, and cleared
        only when the existing lockout is CFG-101 itself. See the plan's
        resolution (3).
        """
        profiles = self.profiles
        if profiles is None:
            return

        for product in self._products():
            sku = product.sku
            valid = self.profile_for(product) is not None
            if not valid:
                if self._is_locked(sku) is None:
                    self._raise_fault(FaultCode.CFG_101, sku=sku)
            elif self._is_locked(sku) is FaultCode.CFG_101:
                # clear_fault's own re-check (review fix, Task 2) re-raises
                # CFG-101 immediately if profile_for still finds no valid
                # profile. Here `valid` is already True, so that re-check
                # finds a profile too and is a no-op -- this call really
                # does clear CFG-101 rather than bouncing it back.
                self._clear_fault(sku, "auto")

        file_error = profiles.report.file_error
        cfg102_active = self._has_machine_fault(FaultCode.CFG_102)
        if file_error and not cfg102_active:
            self._raise_fault(FaultCode.CFG_102)
        elif cfg102_active and not file_error:
            self._clear_fault(FaultCode.CFG_102.value, "auto")

    def catalog_changed(self) -> None:
        """Tell the gate a product catalog mutation just landed (Copilot
        review, PR #32, finding C1) -- call this from every products
        route after a successful `save_config`, so a newly added product
        or one whose `kind` changed is reconciled immediately rather than
        staying sellable until the next unrelated reconcile (a profiles
        reload or the vending capabilities hook).

        Re-runs `DispenserProfiles.revalidate()` against the catalog's
        new state first (so a kind change that now disagrees with its
        slot's mechanism shows up as a validation error too), then
        `reconcile()`. No-op when no profiles object is set.
        """
        profiles = self.profiles
        if profiles is None:
            return
        profiles.revalidate()
        self.reconcile()

    def on_vending_capabilities(
        self, subsystem: str, caps: SubsystemCapabilities
    ) -> None:
        """Re-run the dispenser-profiles capabilities cross-check whenever
        the vending board's retained capabilities doc validates -- invoked
        by `VMC._on_vending_capabilities_validated`, itself called by the
        telemetry router (controller/mqtt_inbound.py's
        `TelemetryRouter.handle_capabilities`) only from its successful-
        validation branch.

        Dispenser profiles (plan: dispenser profiles, Task 2): the vending
        board's declared channel directions feed the profiles' own
        capabilities cross-check (drive channels must be outputs, sensors
        inputs) -- re-run it, and re-reconcile CFG-101, every time this doc
        changes. Only on a successfully validated doc: a malformed doc must
        never overwrite previously-good capabilities with something that
        would wrongly downgrade real slot errors back to warnings.
        """
        if subsystem == "vending" and self.profiles is not None:
            self.profiles.set_capabilities(caps)
            self.reconcile()

    def lacks_valid_profile(self, sku: str) -> bool:
        """True iff profiles are attached, `sku` names a product in the
        current catalog, and `profile_for` finds no valid profile for it
        -- backs `VMC.clear_fault`'s CFG-101 re-check (review fix, Task
        2): popping *any* lockout (not just CFG-101 itself, e.g. an
        operator clearing ICE-301 on a profile-less product) can leave a
        profile-less product unlocked, since nothing else re-runs
        reconciliation on that path. False -- never re-locks -- when no
        profiles are wired, or when `sku` isn't in the catalog at all.
        """
        if self.profiles is None:
            return False
        product = next((p for p in self._products() if p.sku == sku), None)
        if product is None:
            return False
        return self.profile_for(product) is None
