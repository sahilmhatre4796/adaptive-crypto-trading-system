# M12 Phase 3 — first offline lifecycle checkpoint

`trading.paper_state.reconcile_paper_state(bridge, controller, as_of=...)` is a
read-only checkpoint over the existing M8 portfolio, M9.1 simulation bridge,
and M12 exit/funding controller. It does not submit an order or create a fill.
The caller supplies the original actor graph and a UTC cutoff at or after its
latest booked event. This is a current-state checkpoint; it cannot reconstruct
an earlier state from actors that have already advanced beyond that cutoff.

The immutable report lists each simulated order's current state, whether an
open order has an exit plan, and M8 reservations that have not yet become M9.1
intents. A pending reservation remains visible until explicit admission or
cancellation. The report includes cash, equity, realized P&L, fees, funding,
and a deterministic state identity.

Reconciliation rejects duplicate identities, broken lifecycle transitions,
missing or repeated portfolio bookings, unapproved allocations, state/position
disagreements, and cash or fee changes inconsistent with simulated fills.
It compares cumulative portfolio funding with funding on open positions plus
funding retained from positions closed in the append-only history. Thus an
empty current position set can legitimately have nonzero historical funding.
M8 terminal cancellations are recognized from the existing terminal receipts.
Accounting remains in the portfolio's configured account currency; the
existing M8 risk fraction stays `Decimal('.10')`.

This is a local consistency check. It does not validate an external venue,
persist actors, resolve a stalled reservation, support partial fills, or prove
paper-trading readiness. Those are separate Phase 3 units requiring their own
interfaces and tests. No network or exchange connection is used.
