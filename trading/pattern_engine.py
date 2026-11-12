"""Completed-candle pattern events and independent 5m setup lifecycle.

No imports into the baseline strategy or simulator: pattern evidence cannot place orders.
"""
from bisect import bisect_right
from dataclasses import asdict, dataclass, field
from hashlib import sha256
import json
import pandas as pd
from trading.data import validate, utc_times
from trading.patterns import OHLC, PatternConfig, PatternGeometry, detect_geometry
from trading.pattern_context import (ContextAssessment, ContextConfig, ContextSnapshot,
                                    assess_context, preceding_direction, utc_timestamp)

TIMEFRAMES = {'5m': pd.Timedelta(minutes=5), '15m': pd.Timedelta(minutes=15),
              '1h': pd.Timedelta(hours=1), '4h': pd.Timedelta(hours=4)}
SYMBOLS = ('BTC/USDT', 'ETH/USDT')


@dataclass(frozen=True)
class ClosedCandle:
    opened_at: pd.Timestamp
    ohlc: OHLC
    symbol: str
    timeframe: str = '5m'
    available_at: pd.Timestamp | None = None
    is_closed: bool = True

    def __post_init__(self):
        if self.symbol not in SYMBOLS or self.timeframe not in TIMEFRAMES or not isinstance(self.ohlc, OHLC):
            raise ValueError('Invalid timeframe or OHLC')
        if not pd.api.types.is_bool(self.is_closed) or not self.is_closed:
            raise ValueError('Unfinished candle')
        opened = utc_timestamp(self.opened_at)
        if opened.floor(TIMEFRAMES[self.timeframe]) != opened:
            raise ValueError('Candle opening is off timeframe boundary')
        available = utc_timestamp(self.available_at) if self.available_at is not None else opened+TIMEFRAMES[self.timeframe]
        if available < opened+TIMEFRAMES[self.timeframe]:
            raise ValueError('Candle availability precedes completion')
        object.__setattr__(self, 'opened_at', opened)
        object.__setattr__(self, 'available_at', available)

    @property
    def closed_at(self):
        return self.opened_at + TIMEFRAMES[self.timeframe]


@dataclass(frozen=True)
class PatternEvent:
    event_id: str
    symbol: str
    timeframe: str
    family: str
    name: str
    classification: str
    detected_at: pd.Timestamp
    confirmed_at: pd.Timestamp  # Availability, possibly later than price confirmation.
    price_confirmed_at: pd.Timestamp
    expires_at: pd.Timestamp
    candle_references: tuple[pd.Timestamp, ...]  # Opening times, in order.
    candle_geometry: tuple[tuple[tuple[str, float], ...], ...]
    geometry: PatternGeometry
    context: ContextAssessment
    recent_range: float | None
    pattern_range: float
    confirmation_close: float
    invalidation_price: float | None
    geometry_score: float
    strength_score: float
    configuration_id: str
    geometry_config: PatternConfig
    context_config: ContextConfig
    context_snapshot: ContextSnapshot | None
    invalidation_condition: str


def _bars(raw, timeframe, symbol):
    if timeframe not in TIMEFRAMES:
        raise ValueError('Unsupported timeframe')
    if raw.columns.has_duplicates or not {'timestamp', 'open', 'high', 'low', 'close'}.issubset(raw.columns):
        raise ValueError('Unique OHLC and timestamp columns required')
    frame = raw.copy().reset_index(drop=True)
    if 'symbol' in frame and not frame.symbol.eq(symbol).all():
        raise ValueError('Input symbol differs from requested symbol')
    frame['timestamp'] = utc_times(frame.timestamp)
    frame = frame.sort_values('timestamp')
    if frame.timestamp.isna().any() or frame.timestamp.duplicated().any():
        raise ValueError('Invalid/duplicate candle timestamps')
    if len(frame) > 1 and not frame.timestamp.diff().iloc[1:].eq(TIMEFRAMES[timeframe]).all():
        raise ValueError('Missing candles or wrong timeframe')
    if 'available_at' in frame:
        frame['available_at'] = utc_times(frame.available_at)
    result = []
    for row in frame.itertuples(index=False):
        closed = getattr(row, 'is_closed', True)
        # itertuples yields Python bool for pandas/numpy boolean columns.
        result.append(ClosedCandle(row.timestamp, OHLC(row.open, row.high, row.low, row.close),
                                   symbol, timeframe, getattr(row, 'available_at', None), closed))
    if any(a.available_at > b.available_at for a, b in zip(result, result[1:])):
        raise ValueError('Nonmonotonic availability: normalize delayed input before scanning')
    return result


def resample_pattern_bars(raw_5m, timeframe='15m', as_of=None):
    """Reuse M0-M2 input validation; aggregate complete bins, without indicators."""
    if timeframe not in TIMEFRAMES:
        raise ValueError('Unsupported timeframe')
    base = validate(raw_5m, as_of=as_of)
    if 'symbol' in base and (base.symbol.isna().any() or base.symbol.nunique() != 1):
        raise ValueError('Resample one instrument at a time')
    interval = TIMEFRAMES[timeframe]
    groups = base.resample(interval, label='right', closed='left', origin='epoch')
    bars = groups.agg({'open': 'first', 'high': 'max', 'low': 'min', 'close': 'last'})
    bars = bars.loc[groups.close.count() == int(interval / TIMEFRAMES['5m'])]
    bars['timestamp'] = bars.index-interval
    bars['available_at'] = bars.index
    bars['is_closed'] = True
    if 'symbol' in base:
        bars['symbol'] = base.symbol.iloc[0]
    return bars.reset_index(drop=True)


@dataclass(frozen=True)
class PatternEngine:
    config: PatternConfig = PatternConfig()
    context_config: ContextConfig = ContextConfig()
    configuration_id: str = field(init=False)

    def __post_init__(self):
        if not isinstance(self.config, PatternConfig) or not isinstance(self.context_config, ContextConfig):
            raise ValueError('Typed pattern and context configurations required')
        payload = json.dumps({'algorithm': 'M3-v2', 'geometry': asdict(self.config),
                              'context': asdict(self.context_config)}, sort_keys=True)
        object.__setattr__(self, 'configuration_id', sha256(payload.encode()).hexdigest()[:16])

    def scan(self, raw, symbol, timeframe='15m', as_of=None, contexts=()):
        if symbol not in SYMBOLS:
            raise ValueError('Initial pattern scope is BTC/USDT and ETH/USDT')
        as_of = utc_timestamp(as_of if as_of is not None else pd.Timestamp.now(tz='UTC'))
        records = _bars(raw, timeframe, symbol)
        records = [c for c in records if c.available_at <= as_of]
        snapshots = list(contexts)
        if any(not isinstance(c, ContextSnapshot) or c.symbol != symbol for c in snapshots):
            raise ValueError('Context symbol mismatch or invalid snapshot')
        times = [c.available_at for c in snapshots]
        if any(a >= b for a, b in zip(times, times[1:])):
            raise ValueError('Context availability must be strictly increasing')
        result, seen = [], set()
        window_size = self.context_config.prior_trend_bars + self.config.hikkake_confirmation_bars + 3
        for index, candle in enumerate(records):
            window = records[max(0, index+1-window_size):index+1]
            prices = [c.ohlc for c in window]
            for match in detect_geometry(prices, self.config):
                refs = [window[len(window)-1+offset] for offset in match.candle_offsets]
                first = len(window)-1+min(match.candle_offsets)
                prior, recent_range = preceding_direction(prices[:first], self.context_config)
                confirmed_at = max(c.available_at for c in refs)
                detected_at = max(c.available_at for c in refs if c.opened_at <= window[len(window)-1+match.detected_offset].opened_at)
                position = bisect_right(times, confirmed_at)-1
                context = snapshots[position] if position >= 0 else None
                pattern_range = max(c.ohlc.high for c in refs)-min(c.ohlc.low for c in refs)
                assessment = assess_context(match, prior, pattern_range, context, confirmed_at,
                                            self.context_config, recent_range)
                key = '|'.join((self.configuration_id, symbol, timeframe, match.family, match.name,
                                *(c.opened_at.isoformat() for c in refs)))
                event_id = sha256(key.encode()).hexdigest()
                if event_id in seen:
                    continue
                seen.add(event_id)
                invalidation = match.bullish_invalidation if assessment.classification == 'BULLISH' else match.bearish_invalidation if assessment.classification == 'BEARISH' else None
                result.append(PatternEvent(
                    event_id, symbol, timeframe, match.family, assessment.name, assessment.classification,
                    detected_at, confirmed_at, candle.closed_at,
                    candle.closed_at+self.config.expiry_bars*TIMEFRAMES[timeframe],
                    tuple(c.opened_at for c in refs), tuple(c.ohlc.measurements() for c in refs), match,
                    assessment, recent_range, pattern_range, candle.ohlc.close, invalidation,
                    match.geometry_score, assessment.strength_score, self.configuration_id,
                    self.config, self.context_config, context,
                    f'5m low <= {invalidation}' if assessment.classification == 'BULLISH' else
                    f'5m high >= {invalidation}' if assessment.classification == 'BEARISH' else 'not_directional'))
        return tuple(result)


@dataclass(frozen=True)
class SetupDecision:
    event_id: str
    checked_at: pd.Timestamp
    status: str
    classification: str
    reasons: tuple[str, ...]
    context: ContextAssessment | None = None
    context_snapshot: ContextSnapshot | None = None


def evaluate_setup(event, candle, *, as_of, context=None, context_config=None):
    """5m price confirmation is evidence only, never risk approval or an order."""
    as_of = utc_timestamp(as_of)
    if not isinstance(candle, ClosedCandle) or candle.timeframe != '5m':
        raise ValueError('Entry confirmation requires a completed 5m candle')
    if candle.symbol != event.symbol:
        raise ValueError('5m candle symbol mismatch')
    if context_config is not None and context_config != event.context_config:
        raise ValueError('Cannot override a pattern event context configuration')
    context_config = event.context_config
    if candle.available_at > as_of:
        raise ValueError('5m confirmation candle unavailable at as_of')
    checked_at = as_of
    assessment = None
    def decision(status, reasons=(), classification='NEUTRAL'):
        return SetupDecision(event.event_id, checked_at, status, classification, tuple(reasons),
                             assessment, context if assessment is not None else None)
    if checked_at < event.confirmed_at:
        return decision('WAITING', ('pattern_not_available',))
    if checked_at >= event.expires_at:
        return decision('EXPIRED', ('setup_expired',))
    if checked_at-candle.available_at >= TIMEFRAMES['5m']:
        return decision('WAITING', ('stale_5m_candle',))
    if candle.opened_at < event.confirmed_at:
        return decision('WAITING', ('5m_bar_began_before_setup_availability',))
    if event.classification == 'NEUTRAL' or event.family == 'DOJI':
        return decision('REJECTED', ('neutral_pattern_cannot_confirm_entry',))
    bullish = event.classification == 'BULLISH'
    invalidated = candle.ohlc.low <= event.invalidation_price if bullish else candle.ohlc.high >= event.invalidation_price
    if invalidated:
        return decision('INVALIDATED', ('pattern_extreme_touched',))
    if context is not None and context.symbol != event.symbol:
        raise ValueError('Entry context symbol mismatch')
    assessment = assess_context(event.geometry, event.context.prior_direction, event.pattern_range,
                                context, checked_at, context_config, event.recent_range)
    if assessment.status != 'VALIDATED':
        return decision('WAITING', assessment.conflicting+assessment.missing)
    confirmed = (candle.ohlc.close > event.confirmation_close and candle.ohlc.direction == 'BULLISH') if bullish else (candle.ohlc.close < event.confirmation_close and candle.ohlc.direction == 'BEARISH')
    return decision('CONFIRMED', ('completed_5m_direction_and_close',), event.classification) if confirmed else decision('WAITING', ('5m_price_not_confirmed',))


class SetupTracker:
    """An in-memory, per-event chronological lifecycle with duplicate prevention."""
    def __init__(self, context_config=None):
        self.context_config = context_config
        self._events = {}
        self._last = {}
        self._bars = {}

    def observe(self, event, candle, *, as_of, context=None):
        checked_at = utc_timestamp(as_of)
        if not isinstance(candle, ClosedCandle) or candle.timeframe != '5m' or candle.symbol != event.symbol or candle.available_at > checked_at:
            raise ValueError('A completed, available 5m candle is required')
        previous_event = self._events.get(event.event_id)
        if previous_event is not None and previous_event != event:
            raise ValueError('Cannot rewrite a previously registered pattern event')
        previous = self._last.get(event.event_id)
        if previous is not None:
            if checked_at < previous.checked_at:
                raise ValueError('Setup observations must be chronological')
            if previous.status in ('CONFIRMED', 'INVALIDATED', 'EXPIRED', 'REJECTED') or candle.opened_at <= self._bars[event.event_id]:
                return SetupDecision(event.event_id, checked_at, 'DUPLICATE', 'NEUTRAL', ('already_processed',))
        result = evaluate_setup(event, candle, as_of=as_of, context=context, context_config=self.context_config)
        self._events[event.event_id] = event
        self._last[event.event_id] = result
        self._bars[event.event_id] = candle.opened_at
        return result
