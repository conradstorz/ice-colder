# controller/sale_context.py
"""The in-flight sale (VMC public surface design, section 3).

``SaleContext`` is the single, frozen value object carrying everything about
*the one sale the FSM is currently working on* -- the product a customer (or
``VMC.run_test_sale``) selected, the per-method shares deducted from escrow
to pay for it, whether it is a simulated test sale, which dispenser
mechanism and dispatcher ``request_id`` are in flight for it, the dispatch
sequence number minted for it, and when it started. ``VMC`` holds at most one
of these at a time, on ``self._sale``, exposed read-only as ``VMC.sale`` --
``None`` whenever no sale is in progress (idle, or between sales).

A transition in the sale's life never mutates an existing ``SaleContext`` in
place (it is frozen) -- it is always *replaced wholesale* with a new instance
built via ``with_()`` (a thin wrapper over ``dataclasses.replace``) or, at a
sale's start, constructed outright. This is what lets a single assignment
(``self._sale = self._sale.with_(...)`` or ``self._sale = None``) stand in
for what used to be several independent attribute writes
(``selected_product``, ``pending_sale_shares``, ``_sale_is_test``,
``_sale_mechanism``, ``_dispense_request_id``) that had to be kept in sync by
hand across every FSM callback.

Fields, and who sets them:

- ``product``: the selected ``Product``. Set when the sale begins
  (``VMC.select_product``, or seeded directly by ``VMC.run_test_sale`` before
  it calls ``select_product`` so the test-sale exemption in
  ``select_product``'s own availability check sees ``is_test`` before the
  product is technically "selected").
- ``shares``: the per-payment-method amounts FIFO-consumed from escrow to
  pay for this sale (``VMC.process_payment``), restored verbatim by
  ``on_vend_failed`` on a failed vend so a refund never reclassifies money
  between methods (CLAUDE.md's FIFO method attribution invariant). ``None``
  until payment is processed, and cleared once the sale is recorded or has
  failed.
- ``is_test``: true only for ``VMC.run_test_sale``'s simulated sale. Lives on
  the sale, never derived from the maintenance lease, so a lease release or
  idle-timeout mid-run cannot flip a test sale into a production one.
- ``mechanism``: the dispenser mechanism ("bagged_ice"/"water_fill") for
  this sale's slot, set by ``VMC.on_dispense_product`` just before it
  dispatches.
- ``request_id``: the dispatcher command id minted for this sale's dispense
  command, known before any terminal hardware report can arrive so a report
  racing the dispatcher's own ack is still verifiable.
- ``seq``: the dispatch sequence number (``VMC._sale_seq``, a VMC-level
  counter that survives across sales) minted for *this* sale's dispatch
  attempt, captured onto the context at the same point ``mechanism`` and
  ``request_id`` are.
- ``started_at``: wall-clock time the sale began (``select_product``, or the
  seed ``run_test_sale`` builds).
"""

from __future__ import annotations

import dataclasses
from dataclasses import dataclass

from config.config_model import Product


@dataclass(frozen=True)
class SaleContext:
    product: Product
    shares: dict[str, float] | None = None
    is_test: bool = False
    mechanism: str | None = None
    request_id: str | None = None
    seq: int = 0
    started_at: float = 0.0

    def with_(self, **changes) -> "SaleContext":
        """Return a new ``SaleContext`` with ``changes`` applied, leaving
        this instance unchanged (it is frozen -- ``dataclasses.replace``
        always returns a distinct object)."""
        return dataclasses.replace(self, **changes)
