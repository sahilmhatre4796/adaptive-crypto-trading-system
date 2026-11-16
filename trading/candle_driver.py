"""M12 Phase 1: causal, offline 5m candle-to-M7 decision driver.

One instance owns one asset. Accepted inputs are replayed into fresh actors after
restart; this module neither persists inputs nor approves risk or orders.
"""
from dataclasses import dataclass, replace
from math import isfinite

import pandas as pd

from trading.m6 import M6Bundle
from trading.m6_data import Source
from trading.pattern_context import utc_timestamp
from trading.pattern_engine import ClosedCandle, PatternEngine, SYMBOLS, TIMEFRAMES
from trading.patterns import OHLC
from trading.regime import RegimeConfig, RegimeEngine
from trading.signal_context import SignalContextConfig, assemble_context
from trading.signals import SignalConfig, SignalLifecycleEvent, SignalRecord, SignalTracker
from trading.structure import StructureEngine


WARMUP_5M_BARS = 200 * 48
RECEIPT_DEADLINE = TIMEFRAMES['5m']


@dataclass(frozen=True)
class CandleInput:
    symbol: str
    opened_at: pd.Timestamp
    ohlc: OHLC
    volume: float
    received_at: pd.Timestamp
    source_id: str

    def __post_init__(self):
        if self.symbol not in SYMBOLS or not isinstance(self.ohlc, OHLC):
            raise ValueError('Supported BTC/ETH OHLC candle required')
        opened, received = utc_timestamp(self.opened_at), utc_timestamp(self.received_at)
        if opened.floor('5min') != opened or received < opened + TIMEFRAMES['5m']:
            raise ValueError('Completed, aligned 5m candle and actual receipt required')
        if isinstance(self.volume, bool) or not isinstance(self.volume, (int, float)) or not isfinite(self.volume) or self.volume < 0:
            raise ValueError('Finite nonnegative volume required')
        if not isinstance(self.source_id, str) or not self.source_id.strip():
            raise ValueError('Source identity required')
        object.__setattr__(self, 'opened_at', opened)
        object.__setattr__(self, 'received_at', received)

    @property
    def candle(self):
        return ClosedCandle(self.opened_at, self.ohlc, self.symbol, '5m', self.received_at)


@dataclass(frozen=True)
class CandleOutcome:
    candle: CandleInput
    warmup_ready: bool
    first_eligible_at: pd.Timestamp | None
    completed_timeframes: tuple[str, ...]
    pattern_ids: tuple[str, ...]
    decisions: tuple[SignalRecord, ...]
    terminal_events: tuple[SignalLifecycleEvent, ...]


class CandleDecisionDriver:
    """Fail-closed in-memory analytical driver; replay accepted inputs on restart."""

    def __init__(self, symbol: str, *, pattern_engine: PatternEngine,
                 structure_engine: StructureEngine, regime_config: RegimeConfig,
                 context_config: SignalContextConfig, signal_config: SignalConfig,
                 source: Source):
        if (symbol not in SYMBOLS or not isinstance(pattern_engine, PatternEngine)
                or not isinstance(structure_engine, StructureEngine)
                or not isinstance(regime_config, RegimeConfig)
                or not isinstance(context_config, SignalContextConfig)
                or not isinstance(signal_config, SignalConfig)
                or not isinstance(source, Source) or source.instrument != symbol.replace('/', '')):
            raise ValueError('Explicit matching M3/M5/M6/M7 policies and source required')
        self.symbol = symbol
        self.pattern_engine = pattern_engine
        self.structure_engine = structure_engine
        self.regime_engine = RegimeEngine(regime_config)
        self.context_config = context_config
        self.signal_config = signal_config
        self.source = source
        self.signals = SignalTracker(signal_config, context_config, mode='BACKTEST')
        self._inputs = []
        self._outcomes = []
        self._timeframes = {name: [] for name in ('15m', '1h', '4h')}
        self._patterns = {}
        self._pending = {}
        self._active_signals = set()
        self._structure_cache = None
        self._regime_cache = {}
        self._failed = False

    @property
    def inputs(self):
        return tuple(self._inputs[:len(self._outcomes)])

    @property
    def outcomes(self):
        return tuple(self._outcomes)

    def completed_candles(self, timeframe: str):
        if timeframe not in self._timeframes:
            raise ValueError('Only derived 15m, 1h and 4h candles are available')
        return tuple(candle for candle, _ in self._timeframes[timeframe])

    @property
    def first_eligible_at(self):
        return (self._inputs[WARMUP_5M_BARS - 1].opened_at + TIMEFRAMES['5m']
                if len(self._outcomes) >= WARMUP_5M_BARS else None)

    @staticmethod
    def _frame(items):
        return pd.DataFrame((dict(timestamp=c.opened_at, open=c.ohlc.open,
                                  high=c.ohlc.high, low=c.ohlc.low, close=c.ohlc.close,
                                  volume=v, available_at=c.available_at)
                             for c, v in items),
                            columns=('timestamp', 'open', 'high', 'low', 'close', 'volume', 'available_at'))

    def _complete_groups(self):
        completed = []
        for name, count in (('15m', 3), ('1h', 12), ('4h', 48)):
            if len(self._inputs) % count:
                continue
            group = self._inputs[-count:]
            opened = group[0].opened_at
            if opened.floor(TIMEFRAMES[name]) != opened:
                raise ValueError('Incomplete UTC timeframe boundary')
            ohlc = OHLC(group[0].ohlc.open, max(x.ohlc.high for x in group),
                        min(x.ohlc.low for x in group), group[-1].ohlc.close)
            bar = ClosedCandle(opened, ohlc, self.symbol, name,
                               max(x.received_at for x in group))
            self._timeframes[name].append((bar, sum(x.volume for x in group)))
            completed.append(name)
        return tuple(completed)

    def _bundle(self, at):
        results = []
        for name in ('15m', '1h', '4h'):
            bars = self._timeframes[name]
            cached = self._regime_cache.get(name)
            if cached is None or cached[0] != len(bars):
                result = self.regime_engine.analyze(self._frame(bars), self.symbol,
                                                    name, as_of=at, source=self.source)
                self._regime_cache[name] = (len(bars), result)
            else:
                result = replace(cached[1], as_of=at)
                if (result.status == 'AVAILABLE' and result.candle_closed_at is not None
                        and at - result.candle_closed_at >
                        self.regime_engine.config.max_age_intervals * TIMEFRAMES[name]):
                    result = replace(result, status='STALE', classification='UNKNOWN',
                                     volatility_state='UNKNOWN', volatility_change='UNKNOWN',
                                     confidence=0., evidence=(), reasons=('stale_latest_candle',))
            results.append(result)
        regime = tuple(results)
        return M6Bundle(self.symbol, at, regime, (), ())

    def _structure(self, at):
        bars = self._timeframes['15m']
        if self._structure_cache is None or self._structure_cache[0] != len(bars):
            result = self.structure_engine.scan(self._frame(bars), self.symbol,
                                                '15m', as_of=at)
            self._structure_cache = (len(bars), result)
            return result
        return replace(self._structure_cache[1], as_of=at)

    def observe(self, item: CandleInput) -> CandleOutcome:
        if self._failed:
            raise RuntimeError('Driver quarantined; replay accepted inputs into a fresh driver')
        if not isinstance(item, CandleInput) or item.symbol != self.symbol:
            raise ValueError('Matching typed candle input required')
        if self._inputs:
            previous = self._inputs[-1]
            if item.opened_at <= previous.opened_at:
                index = int((item.opened_at - self._inputs[0].opened_at) / TIMEFRAMES['5m'])
                if 0 <= index < len(self._outcomes) and item.opened_at == self._inputs[index].opened_at:
                    if item == self._inputs[index]:
                        return self._outcomes[index]
                    raise ValueError('Revised duplicate candle')
                raise ValueError('Out-of-order 5m candle')
            if item.opened_at != previous.opened_at + TIMEFRAMES['5m']:
                raise ValueError('Missing or out-of-order 5m candle')
            if item.received_at < previous.received_at:
                raise ValueError('Nonmonotonic receipt time')
            if item.source_id != previous.source_id:
                raise ValueError('Source identity changed during replay')
        elif item.opened_at.floor('4h') != item.opened_at:
            raise ValueError('Warmup must start on a UTC 4h boundary')
        if item.received_at - item.candle.closed_at >= RECEIPT_DEADLINE:
            raise ValueError('Late 5m candle cannot preserve M7 monitoring history')
        self._inputs.append(item)
        try:
            completed = self._complete_groups()
            if len(self._inputs) < WARMUP_5M_BARS:
                outcome = CandleOutcome(item, False, None, completed, (), (), ())
            else:
                at, candle = item.received_at, item.candle
                ready_at = self._inputs[WARMUP_5M_BARS - 1].opened_at + TIMEFRAMES['5m']
                before_events = len(self.signals.events)
                for signal_id in sorted(self._active_signals):
                    self.signals.observe_signal(signal_id, candle, as_of=at)
                ended = {event.signal_id for event in self.signals.events[before_events:]}
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
                        context = assemble_context(pattern, candle, structure, structure_candle,
                                                   bundle, as_of=at, mode='BACKTEST',
                                                   config=self.context_config)
                        record = self.signals.observe(context)
                        decisions.append(record)
                        if record.lifecycle_state != 'WAITING':
                            del self._pending[pattern_id]
                        if record.signal_id is not None:
                            self._active_signals.add(record.signal_id)
                new_ids = []
                if '15m' in completed:
                    # M3 needs only its bounded geometry/prior-trend window.
                    bars = self._timeframes['15m']
                    window = self.pattern_engine.context_config.prior_trend_bars + self.pattern_engine.config.hikkake_confirmation_bars + 4
                    for pattern in self.pattern_engine.scan(self._frame(bars[-window:]),
                                                            self.symbol, '15m', as_of=at):
                        if (pattern.event_id not in self._patterns
                                and pattern.confirmed_at >= ready_at):
                            self._patterns[pattern.event_id] = pattern
                            self._pending[pattern.event_id] = pattern
                            new_ids.append(pattern.event_id)
                outcome = CandleOutcome(item, True, ready_at, completed,
                                        tuple(new_ids), tuple(decisions),
                                        self.signals.events[before_events:])
            self._outcomes.append(outcome)
            return outcome
        except BaseException:
            self._failed = True
            raise

    def replay(self):
        """Reconstruct private M7 chronology from accepted original inputs."""
        other = type(self)(self.symbol, pattern_engine=self.pattern_engine,
                           structure_engine=self.structure_engine,
                           regime_config=self.regime_engine.config,
                           context_config=self.context_config, signal_config=self.signal_config,
                           source=self.source)
        for item in self._inputs[:len(self._outcomes)]:
            other.observe(item)
        return other
