# ice-colder

A vending machine controller: the edge runs one machine's sales, payments and hardware; the cloud watches a fleet of them.

## Language

### Crash recovery

**Session snapshot**:
The on-disk record of the live sale (state, escrow credit, selected product, pending payouts), rewritten as the sale progresses.
_Avoid_: session file, session.json (when meaning the concept)

**Session evidence**:
A **Session snapshot** held back from deletion because it is the only account of money the machine may owe or have taken: left uncertain at boot, or describing a sale that could not be recorded anywhere durable. An operator resolves it with **Record as sale** or **Discard**.
_Avoid_: PAY-104 (a fault code that can also be raised without any evidence held), recovery snapshot

**Pending sale**:
The product and per-method money shares in **Session evidence** for a sale that was dispensed but has no sales row.
_Avoid_: unrecorded sale, lost sale

**Record as sale**:
The operator's resolution of **Session evidence** that writes its **Pending sale** to the sales ledger and tells the payment board the cash left escrow.

**Discard**:
The operator's resolution of **Session evidence** that drops it without recording a sale.
