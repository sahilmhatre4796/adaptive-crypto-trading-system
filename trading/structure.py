"""Causal market-structure evidence on completed candles; never order approval."""
from dataclasses import asdict, dataclass, field, replace
from hashlib import sha256
import json

import pandas as pd

from trading.pattern_context import utc_timestamp
from trading.pattern_engine import SYMBOLS, TIMEFRAMES, _bars
from trading.patterns import _number


def _id(*parts):
    return sha256('|'.join(map(str, parts)).encode()).hexdigest()


@dataclass(frozen=True)
class StructureConfig:
    left_bars: int = 2
    right_bars: int = 2
    zone_range_fraction: float = .10
    zone_lifetime_bars: int = 96
    retest_bars: int = 8

    def __post_init__(self):
        for name in ('left_bars', 'right_bars', 'zone_lifetime_bars', 'retest_bars'):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, int) or not 1 <= value <= 10000:
                raise ValueError(f'{name} must be an integer in [1, 10000]')
        fraction = _number('zone_range_fraction', self.zone_range_fraction)
        if not 0 < fraction <= 1:
            raise ValueError('zone_range_fraction must be in (0, 1]')
        object.__setattr__(self, 'zone_range_fraction', fraction)


@dataclass(frozen=True)
class Swing:
    swing_id: str
    kind: str  # HIGH / LOW
    price: float
    pivot_at: pd.Timestamp  # Candle OPEN, never the time evidence became known.
    confirmed_at: pd.Timestamp  # Availability of the rightmost confirming candle.
    price_confirmed_at: pd.Timestamp  # Its price close, independent of receipt.
    relation: str  # FIRST / HH / LH / EH / HL / LL / EL


@dataclass(frozen=True)
class StructureZone:
    zone_id: str
    swing_id: str
    role: str  # Original SUPPORT / RESISTANCE; RETESTED zones reverse role.
    lower: float
    upper: float
    created_at: pd.Timestamp
    expires_at: pd.Timestamp
    state: str = 'ACTIVE'
    breakout_at: pd.Timestamp | None = None
    retest_expires_at: pd.Timestamp | None = None
    swept: bool = False


@dataclass(frozen=True)
class StructureEvent:
    event_id: str
    kind: str  # BREAKOUT / RETEST / FAILED_BREAK / SWEEP / STRUCTURE_CHANGE
    direction: str
    available_at: pd.Timestamp
    price_confirmed_at: pd.Timestamp
    candle_at: pd.Timestamp
    zone_id: str | None
    price: float
    break_type: str = ''  # BOS / CHOCH / UNCLASSIFIED on breakouts only.


@dataclass(frozen=True)
class StructureResult:
    symbol: str
    timeframe: str
    as_of: pd.Timestamp
    last_available_at: pd.Timestamp | None
    configuration_id: str
    config: StructureConfig
    swings: tuple[Swing, ...]
    zones: tuple[StructureZone, ...]
    events: tuple[StructureEvent, ...]
    trend: str

    def level_distances(self, price, atr):
        """Nearest eligible zone-edge distances for M3's optional context fields.

        Caller supplies contemporaneous price/ATR and owns context freshness.
        Values describe this as_of snapshot, never a different historical time.
        """
        price = _number('price', price, positive=True)
        atr = _number('atr', atr, positive=True)
        support, resistance = [], []
        for zone in self.zones:
            if zone.state not in ('ACTIVE', 'RETESTED'):
                continue
            role = zone.role
            if zone.state == 'RETESTED':
                role = 'SUPPORT' if role == 'RESISTANCE' else 'RESISTANCE'
            if role == 'SUPPORT' and price >= zone.lower:
                support.append(max(0., price-zone.upper)/atr)
            elif role == 'RESISTANCE' and price <= zone.upper:
                resistance.append(max(0., zone.lower-price)/atr)
        return dict(support_distance_atr=min(support, default=None),
                    resistance_distance_atr=min(resistance, default=None))


def _trend(latest):
    if not all(k in latest for k in ('HIGH', 'LOW')):
        return 'UNKNOWN'
    pair = (latest['HIGH'].relation, latest['LOW'].relation)
    if 'FIRST' in pair:
        return 'UNKNOWN'
    return 'BULLISH' if pair == ('HH', 'HL') else 'BEARISH' if pair == ('LH', 'LL') else 'RANGE'


def _relation(kind, price, previous):
    if previous is None:
        return 'FIRST'
    if kind == 'HIGH':
        return 'HH' if price > previous.price else 'LH' if price < previous.price else 'EH'
    return 'HL' if price > previous.price else 'LL' if price < previous.price else 'EL'


def _expire(zone, at):
    # Zone lifetime is exclusive; the retest window includes its final close.
    expired = at >= zone.expires_at or (zone.state == 'BROKEN' and at > zone.retest_expires_at)
    return replace(zone, state='EXPIRED') if zone.state in ('ACTIVE', 'BROKEN', 'RETESTED') and expired else zone


@dataclass(frozen=True)
class StructureEngine:
    config: StructureConfig = StructureConfig()
    configuration_id: str = field(init=False)

    def __post_init__(self):
        if not isinstance(self.config, StructureConfig):
            raise ValueError('Typed structure configuration required')
        payload = json.dumps({'algorithm': 'M5-v1', **asdict(self.config)}, sort_keys=True)
        object.__setattr__(self, 'configuration_id', _id(payload)[:16])

    def scan(self, raw, symbol, timeframe='15m', as_of=None):
        if symbol not in SYMBOLS:
            raise ValueError('Initial structure scope is BTC/USDT and ETH/USDT')
        as_of = utc_timestamp(as_of if as_of is not None else pd.Timestamp.now(tz='UTC'))
        bars = [c for c in _bars(raw, timeframe, symbol) if c.available_at <= as_of]
        interval = TIMEFRAMES[timeframe]
        swings, zones, events, latest, active = [], [], [], {}, {}
        trend = 'UNKNOWN'

        def emit(kind, direction, candle, zone_id=None, break_type=''):
            events.append(StructureEvent(
                _id(self.configuration_id, symbol, timeframe, kind, zone_id, candle.opened_at),
                kind, direction, candle.available_at, candle.closed_at, candle.opened_at,
                zone_id, candle.ohlc.close, break_type))

        for index, candle in enumerate(bars):
            price = candle.ohlc
            # Process only levels known BEFORE this candle began. New pivots come later.
            for zindex in tuple(active):
                zone = _expire(zones[zindex], candle.available_at)
                zones[zindex] = zone
                if zone.state == 'EXPIRED':
                    del active[zindex]
                    continue
                if candle.opened_at < zone.created_at:
                    continue
                upward = zone.role == 'RESISTANCE'
                direction = 'BULLISH' if upward else 'BEARISH'
                if zone.state == 'ACTIVE':
                    broken = price.close > zone.upper if upward else price.close < zone.lower
                    swept = (price.high > zone.upper and price.close < zone.lower) if upward else (price.low < zone.lower and price.close > zone.upper)
                    if broken:
                        break_type = 'UNCLASSIFIED' if trend not in ('BULLISH', 'BEARISH') else 'BOS' if trend == direction else 'CHOCH'
                        emit('BREAKOUT', direction, candle, zone.zone_id, break_type)
                        zone = replace(zone, state='BROKEN', breakout_at=candle.available_at,
                                       retest_expires_at=candle.closed_at+self.config.retest_bars*interval)
                        zone = _expire(zone, candle.available_at)
                    elif swept and not zone.swept:
                        emit('SWEEP', 'BEARISH' if upward else 'BULLISH', candle, zone.zone_id)
                        zone = replace(zone, swept=True)
                else:
                    # Never use pre-breakout price action as a retest after delayed receipt.
                    if candle.opened_at < zone.breakout_at:
                        continue
                    failed = price.close < zone.lower if upward else price.close > zone.upper
                    touched = price.low <= zone.upper and price.high >= zone.lower
                    held = (price.low >= zone.lower and price.close > zone.upper) if upward else (price.high <= zone.upper and price.close < zone.lower)
                    if failed:
                        emit('FAILED_BREAK', 'BEARISH' if upward else 'BULLISH', candle, zone.zone_id)
                        zone = replace(zone, state='FAILED')
                    elif zone.state == 'BROKEN' and touched and held:
                        emit('RETEST', direction, candle, zone.zone_id)
                        zone = replace(zone, state='RETESTED')
                zones[zindex] = zone
                if zone.state in ('EXPIRED', 'FAILED'):
                    del active[zindex]

            pivot_index = index-self.config.right_bars
            if pivot_index < self.config.left_bars:
                continue
            pivot = bars[pivot_index]
            neighbors = bars[pivot_index-self.config.left_bars:pivot_index] + bars[pivot_index+1:index+1]
            for kind in ('HIGH', 'LOW'):
                value = pivot.ohlc.high if kind == 'HIGH' else pivot.ohlc.low
                unique = all(value > c.ohlc.high if kind == 'HIGH' else value < c.ohlc.low for c in neighbors)
                if not unique:
                    continue  # Equal-price plateaus are deliberately not pivots.
                previous = latest.get(kind)
                relation = _relation(kind, value, previous)
                swing_id = _id(self.configuration_id, symbol, timeframe, kind, pivot.opened_at)
                swing = Swing(swing_id, kind, value, pivot.opened_at, candle.available_at, candle.closed_at, relation)
                swings.append(swing)
                latest[kind] = swing
                width = pivot.ohlc.range*self.config.zone_range_fraction
                zone = StructureZone(_id(swing_id, 'zone'), swing_id,
                                     'RESISTANCE' if kind == 'HIGH' else 'SUPPORT',
                                     value-width if kind == 'HIGH' else value,
                                     value if kind == 'HIGH' else value+width,
                                     candle.available_at, candle.closed_at+self.config.zone_lifetime_bars*interval)
                zone = _expire(zone, candle.available_at)
                zones.append(zone)
                if zone.state == 'ACTIVE':
                    active[len(zones)-1] = None
            new_trend = _trend(latest)
            if new_trend != trend:
                emit('STRUCTURE_CHANGE', new_trend, candle)
                trend = new_trend

        return StructureResult(symbol, timeframe, as_of, bars[-1].available_at if bars else None,
                               self.configuration_id, self.config, tuple(swings),
                               tuple(_expire(z, as_of) for z in zones), tuple(events), trend)
