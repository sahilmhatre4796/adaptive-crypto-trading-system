# M12 Phase 2 — offline exits and funding

`trading.exit_funding.ExitFundingController` operates over one existing M9.1
`FreqtradeSimulationBridge`. It never creates an order or an entry. It accepts
only a bridge-registered M8-approved order after an acknowledged simulated
entry fill. An explicit, tick-aligned take-profit level and target slippage
assumption are required at `register()`: M7–M9.1 do not define a take-profit
policy, so no R-multiple or target is inferred. Any open position without a
registered exit plan causes candle processing to fail closed.

For each complete, timely, independent 5m candle, the controller tests the
M8-approved source stop (rounded in the same adverse direction as M8) and
the explicit target. A stop wins when OHLC touches both because the intrabar
path is unknown. A gap through the stop uses the worse opening price. Exit
slippage is applied once and rounded adversely to the instrument tick: M8's
stop-slippage assumption for stop exits and the explicit target-slippage
assumption for targets. It emits a full-quantity `SimulationFill` through
M9.1 `process_fill`, which books M8 portfolio accounting. No prior or
already-open candle is retroactively tested after a mid-candle entry. Missing
post-entry candles and late receipt fail closed. The result is a conservative
**offline OHLC simulation**, not evidence of an actual venue fill or guaranteed
stop-loss bound. The resulting M9.1 lifecycle event remains directly
consumable by M9.2's existing notification mapper; notifications are not
approval or accounting inputs.

`FundingObservation` requires settlement, publication and receipt times,
signed finite rate, a source, and explicit mark and account/USDT FX quotes.
Those exact quotes must already be in M8's portfolio and fresh at receipt.
For a position held before settlement, signed account-currency cash flow is
`-direction * contracts * multiplier * mark * funding_rate * FX`. It is
booked at **receipt**, never backdated, using `Portfolio.settle_funding`.
`process_frame(candle, funding=...)` posts available funding before a
same-frame exit. A missing position returns `NO_POSITION` with no invented
zero cash flow. A late settlement for an already-closed position raises for
reconciliation rather than silently losing a debit or credit.

The BACKTEST actor requires an explicit `historical_eligible=True` assertion
for funding. That assertion is caller provenance, not authentication. M11
archives do not establish original funding publication or receipt times, so
their rows cannot be automatically promoted to eligible M12 settlement
observations. This phase adds no M11 ingestion or historical P&L claim.

Event IDs derive from the original order, plan, candle or settlement identity.
Exact duplicates return prior results; revised candles, funding, plans and
same-symbol/settlement events under new IDs or sources are rejected. An actor
exception quarantines the controller where partial mutation may be possible;
reconstruct the bridge/portfolio/controller from original ordered facts.
`on_signal_terminal()` delegates to M9.1: pending intents may be revoked,
while an open position retains its exit plan and is not fabricated closed.

Phase 1 R2 mitigation caches full M5 and M6 analyses by completed source
timeframe. Between 15m closes, M5 and all three M6 roles are reused with the
current evaluation cutoff while preserving original source availability;
each role recomputes only when its source timeframe completes. A 12-candle
synthetic pending-pattern benchmark, with the old full-rescan methods used as
the comparison, took 12.551 seconds versus 6.679 seconds with this cache;
all 12 outcomes were identical. Full M5/M6 rescans still occur on new source
bars, so very long streams with continuously pending patterns retain a
worst-case scaling risk. A bounded or incremental upstream algorithm would
need separate semantic review.

No paper venue, live connectivity, durable external-effect journal, partial
fill, automatic target selection or external settlement reconciliation is
provided. M9.3 and the legacy backtester remain unchanged. M8's
`RiskConfig.risk_fraction = Decimal('.10')` is unchanged; modeled planned
loss remains conditional on execution and cost assumptions.
