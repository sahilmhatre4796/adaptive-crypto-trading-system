"""M13 current-mode analytical driver; preserves the M12 historical driver."""
from trading.candle_driver import CandleDecisionDriver, CandleInput, CandleOutcome, RECEIPT_DEADLINE, WARMUP_5M_BARS
from trading.pattern_engine import TIMEFRAMES
from trading.pattern_context import utc_timestamp
from trading.signal_context import assemble_context
from trading.signals import SignalTracker


class CurrentCandleDriver(CandleDecisionDriver):
    """Same completed-candle pipeline as M12, with M7 CURRENT evidence rules."""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.signals = SignalTracker(self.signal_config, self.context_config, mode='CURRENT')
        self._seeded = False
        self._live_boundary = None

    def seed_many(self, items, *, retrieved_at):
        """Load historical indicator state without backdated M7 decisions.

        Original retrieval is journaled separately; the close-time convention
        applies only to this bounded seed, never to a live frame.
        """
        self.validate_seed(items, retrieved_at=retrieved_at)
        try:
            for item in items:
                self._inputs.append(item)
                completed = self._complete_groups()
                self._outcomes.append(CandleOutcome(item, False, None, completed, (), (), ()))
        except BaseException:
            self._failed = True
            raise
        self._seeded = True
        return len(items)

    def validate_seed(self, items, *, retrieved_at):
        """Check the entire seed before any durable or actor mutation."""
        at = utc_timestamp(retrieved_at)
        if (self._failed or self._inputs or type(items) is not tuple
                or not WARMUP_5M_BARS <= len(items) <= WARMUP_5M_BARS+48):
            raise ValueError('Fresh bounded 9,600-bar historical seed required')
        first = items[0]
        if (not isinstance(first, CandleInput) or first.symbol != self.symbol
                or first.opened_at.floor('4h') != first.opened_at):
            raise ValueError('Seed must start on the matching UTC 4h boundary')
        for index, item in enumerate(items):
            if (not isinstance(item, CandleInput) or item.symbol != self.symbol
                    or item.source_id != first.source_id
                    or item.opened_at != first.opened_at + index*TIMEFRAMES['5m']
                    or item.received_at != item.candle.closed_at
                    or item.candle.closed_at > at):
                raise ValueError('Seed has a gap, revision, future bar or nonhistorical receipt')
        return len(items)

    def observe(self, item):
        if self._failed:
            raise RuntimeError('Driver quarantined; recover a fresh actor graph')
        if not isinstance(item, CandleInput) or item.symbol != self.symbol:
            raise ValueError('Matching typed candle required')
        if self._inputs:
            previous = self._inputs[-1]
            if item.opened_at <= previous.opened_at:
                index = int((item.opened_at - self._inputs[0].opened_at) / TIMEFRAMES['5m'])
                if 0 <= index < len(self._outcomes) and item.opened_at == self._inputs[index].opened_at:
                    if item == self._inputs[index]:
                        return self._outcomes[index]
                    raise ValueError('Revised duplicate candle')
                raise ValueError('Out-of-order candle')
            if item.opened_at != previous.opened_at + TIMEFRAMES['5m']:
                raise ValueError('Missing 5m candle')
            if item.received_at < previous.received_at or item.source_id != previous.source_id:
                raise ValueError('Receipt chronology or source identity changed')
        elif item.opened_at.floor('4h') != item.opened_at:
            raise ValueError('Warmup must start on a UTC 4h boundary')
        if item.received_at - item.candle.closed_at >= RECEIPT_DEADLINE:
            raise ValueError('Late 5m candle cannot preserve M7 chronology')
        self._inputs.append(item)
        try:
            completed = self._complete_groups()
            if self._seeded and self._live_boundary is None:
                # The first live close is the earliest possible live M3
                # confirmation; older seed-window candidates lack M7 history.
                self._live_boundary = item.candle.closed_at
            if len(self._inputs) < WARMUP_5M_BARS:
                result = CandleOutcome(item, False, None, completed, (), (), ())
            else:
                at, candle = item.received_at, item.candle
                ready = self._inputs[WARMUP_5M_BARS-1].opened_at + TIMEFRAMES['5m']
                before = len(self.signals.events)
                for signal_id in sorted(self._active_signals):
                    self.signals.observe_signal(signal_id, candle, as_of=at)
                ended = {event.signal_id for event in self.signals.events[before:]}
                self._active_signals.difference_update(ended)
                decisions = []
                if self._pending:
                    structure = self._structure(at)
                    structure_candle = (self._timeframes['15m'][-1][0]
                                        if structure.last_available_at is not None else None)
                    bundle = self._bundle(at)
                    for pattern_id, pattern in tuple(sorted(self._pending.items())):
                        if candle.opened_at < pattern.confirmed_at.ceil('5min'):
                            continue
                        context = assemble_context(pattern, candle, structure,
                                                   structure_candle, bundle, as_of=at,
                                                   mode='CURRENT', config=self.context_config)
                        record = self.signals.observe(context)
                        decisions.append(record)
                        if record.lifecycle_state != 'WAITING':
                            del self._pending[pattern_id]
                        if record.signal_id is not None:
                            self._active_signals.add(record.signal_id)
                new_ids = []
                if '15m' in completed:
                    bars = self._timeframes['15m']
                    window = (self.pattern_engine.context_config.prior_trend_bars
                              + self.pattern_engine.config.hikkake_confirmation_bars + 4)
                    for pattern in self.pattern_engine.scan(self._frame(bars[-window:]),
                                                            self.symbol, '15m', as_of=at):
                        boundary = self._live_boundary if self._live_boundary is not None else ready
                        if pattern.event_id not in self._patterns and pattern.confirmed_at >= boundary:
                            self._patterns[pattern.event_id] = pattern
                            self._pending[pattern.event_id] = pattern
                            new_ids.append(pattern.event_id)
                result = CandleOutcome(item, True, ready, completed, tuple(new_ids),
                                       tuple(decisions), self.signals.events[before:])
            self._outcomes.append(result)
            return result
        except BaseException:
            self._failed = True
            raise
