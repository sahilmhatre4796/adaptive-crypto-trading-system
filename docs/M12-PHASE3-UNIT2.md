# M12 Phase 3 Unit 2 — offline paper execution adapter

`trading.paper_execution.OfflinePaperExecution` accepts only an admission
receipt already registered by the existing M9.1 bridge. An ACCEPTED receipt
with an active M8 reservation can be acknowledged; a DENIED receipt is recorded
as denied and cannot create an order. Acknowledgement creates no fill or
portfolio booking. It must occur while the original intent and source data
remain valid.

After acknowledgement, the adapter passes a typed `SimulationFill` to
`FreqtradeSimulationBridge.process_fill()`. M9.1 verifies the full approved
quantity and entry price/cost assumptions, and M8 remains the sole accounting
owner. Exit price and fee are explicit simulated inputs; the caller must
already include slippage exactly once. A partial or oversized fill is rejected:
M9.1 has no partial position or partial reservation contract. The adapter
does not accumulate partial fills or claim venue execution.

An execution rejection or cancellation of a proposed intent passes an explicit
`SimulationCancellation` to M9.1. That lifecycle transition releases the M8
reservation. A denied M9.1 admission with an outstanding M8 reservation does
not silently release it; it remains visible until the existing explicit
cancellation/reconciliation path resolves it.
The adapter's `DENIED` record labels a denied M9.1 admission. Its `REJECTED`
record labels a rejected execution attempt on an accepted intent; M9.1 still
records the authoritative order transition as `CANCELLED`.

The adapter has frozen, in-memory execution records with original typed
source and M9.1 lifecycle references. Exact repeated event IDs return the
original record; changed payloads, duplicate acknowledgement under another
event ID, unacknowledged fills, and noncausal cutoffs are rejected. One adapter
owns each bridge actor; new acknowledgements respect later M8/M9.1 facts. After an
exception inside a bridge callback, the adapter quarantines itself because
the callback may have changed M8/M9.1 before failing. Recovery requires fresh
actors and replay of original ordered inputs. This local history is not a
durable transaction journal or an exactly-once external delivery guarantee.

The adapter has no socket, exchange client, Freqtrade runtime, order transport,
or independent cash ledger. It does not change M8's `Decimal('.10')` risk
fraction, M9.1 transitions, M9.3 contracts, or strategy rules. Unit 1's
read-only `reconcile_paper_state()` can inspect its resulting authoritative
bridge and portfolio state. External venue reconciliation, durable restart,
and partial fills remain outside this unit; paper-trading readiness is not
claimed.
