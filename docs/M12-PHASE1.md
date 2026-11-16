# M12 Phase 1 — offline candle-to-decision driver

`trading.candle_driver.CandleDecisionDriver` consumes one explicitly timed
BTC/USDT or ETH/USDT stream of completed 5m candles. `CandleInput` requires the
actual UTC receipt timestamp, a stable feed identity, OHLC and nonnegative
volume. There is no transport or clock read. Missing, revised, out-of-order,
unfinished, or receipt-at/after-close-plus-5m candles fail closed. Exact
duplicates return the original outcome, including after later candles.

The first source bar must open on a UTC 4h boundary. Complete groups of 3, 12
and 48 validated inputs produce 15m, 1h and 4h candles. A group's receipt is
the latest actual constituent receipt. No incomplete group or future candle is
promoted to a higher timeframe. The 9,599-bar prefix is ineligible; the
9,600th completed input closes the 200th 4h group and records the first
eligible decision boundary. Earlier patterns are not imported as candidates.

After warmup, the driver calls the existing M3 `PatternEngine`, M5
`StructureEngine`, M6 `RegimeEngine` for 15m/1h/4h, M7 `assemble_context`, and
M7 `SignalTracker`. Existing signals are monitored before new candidate
decisions. Patterns detected on a 15m close require a later independent 5m
bar. The driver records M7 decisions and terminal events; it does not create
an M8 proposal, reservation, order, fill or return. External derivatives and
heatmaps are absent from this phase, never substituted with favorable or
negative votes. All policy objects are caller-supplied and retain upstream
configuration identities.

`inputs` and `outcomes` expose only successfully processed observations.
`replay()` reconstructs a fresh driver and M7 actor from those original
inputs, preserving private chronology. A processing exception quarantines the
current driver; callers must replay into a fresh instance. This is in-memory
replay, **not durable crash recovery**. No M9.3 or legacy backtester behavior
is changed.

The receipt timestamp and feed identity are caller assertions; Phase 1 cannot
authenticate them. M11 historical archives do not contain original market
receipt times, so their candles cannot be passed as if historical receipt
times were verified. A separately approved input adapter must define that
provenance before such use. Paper venue connectivity, automatic stops,
take-profit, funding settlement and venue reconciliation remain outside
Phase 1. The current M8 `Decimal('.10')` policy is unchanged and inactive in
this analytical driver.
