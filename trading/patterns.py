"""Pure OHLC geometry. No indicators, market data access, orders or trend inference."""
from dataclasses import dataclass
from math import isfinite
from numbers import Real
from typing import Sequence


def _number(name, value, *, positive=False):
    if isinstance(value, bool) or not isinstance(value, Real) or not isfinite(value):
        raise ValueError(f'{name} must be finite')
    if positive and value <= 0:
        raise ValueError(f'{name} must be positive')
    return float(value)


@dataclass(frozen=True)
class OHLC:
    open: float
    high: float
    low: float
    close: float

    def __post_init__(self):
        for name in ('open', 'high', 'low', 'close'):
            object.__setattr__(self, name, _number(name, getattr(self, name), positive=True))
        if self.high < max(self.open, self.close, self.low) or self.low > min(self.open, self.close):
            raise ValueError('Malformed OHLC bounds')

    @property
    def body(self):
        return abs(self.close - self.open)

    @property
    def range(self):
        return self.high - self.low

    @property
    def body_low(self):
        return min(self.open, self.close)

    @property
    def body_high(self):
        return max(self.open, self.close)

    @property
    def upper_wick(self):
        return self.high - self.body_high

    @property
    def lower_wick(self):
        return self.body_low - self.low

    @property
    def body_ratio(self):
        return self.body / self.range if self.range else 0.0

    @property
    def direction(self):
        return 'BULLISH' if self.close > self.open else 'BEARISH' if self.close < self.open else 'NEUTRAL'

    def measurements(self):
        return tuple((key, value) for key, value in (
            ('body', self.body), ('range', self.range), ('upper_wick', self.upper_wick),
            ('lower_wick', self.lower_wick), ('body_ratio', self.body_ratio),
            ('body_low', self.body_low), ('body_high', self.body_high),
            ('body_position', (self.body_low-self.low+self.body/2)/self.range if self.range else .5)))


@dataclass(frozen=True)
class PatternConfig:
    doji_body_max: float = .10
    long_body_min: float = .50
    harami_body_max: float = .60
    engulfing_body_min: float = 1.0
    body_tolerance: float = .02  # Fraction of FIRST candle body, not absolute price.
    wick_body_min: float = 2.0
    wick_opposite_max: float = .10  # Fraction of full candle range.
    wick_body_max: float = .35
    star_middle_max: float = .30  # Relative to first body.
    star_third_body_min: float = .50  # Relative to third range.
    star_penetration: float = .50
    star_middle_location_max: float = .25
    hikkake_confirmation_bars: int = 3
    expiry_bars: int = 4

    def __post_init__(self):
        for name, value in vars(self).items():
            _number(name, value)
            if value < 0:
                raise ValueError(f'Negative threshold: {name}')
        for name in ('doji_body_max', 'long_body_min', 'harami_body_max', 'body_tolerance',
                     'wick_opposite_max', 'wick_body_max', 'star_middle_max',
                     'star_third_body_min', 'star_penetration', 'star_middle_location_max'):
            if getattr(self, name) > 1:
                raise ValueError(f'Ratio outside [0, 1]: {name}')
        if not self.doji_body_max < min(self.long_body_min, self.wick_body_max, self.star_third_body_min):
            raise ValueError('Directional body thresholds must exceed the doji threshold')
        if self.engulfing_body_min < 1 or self.wick_body_min < 1:
            raise ValueError('Engulfing and wick/body ratios must be >= 1')
        for name in ('hikkake_confirmation_bars', 'expiry_bars'):
            value = getattr(self, name)
            if not isinstance(value, int) or isinstance(value, bool) or not 1 <= value <= 100:
                raise ValueError(f'{name} must be an integer in [1, 100]')


DEFAULT_CONFIG = PatternConfig()


@dataclass(frozen=True)
class PatternGeometry:
    family: str
    name: str
    classification: str
    candle_offsets: tuple[int, ...]  # Relative to the last supplied candle.
    detected_offset: int
    geometry_score: float
    measurements: tuple[tuple[str, float], ...]
    bullish_invalidation: float
    bearish_invalidation: float

    def __post_init__(self):
        if self.classification not in ('BULLISH', 'BEARISH', 'NEUTRAL'):
            raise ValueError('Invalid geometry classification')
        if not 0 <= _number('geometry_score', self.geometry_score) <= 100:
            raise ValueError('Geometry score outside [0, 100]')
        for key, value in self.measurements:
            _number(key, value)
        _number('bullish invalidation', self.bullish_invalidation, positive=True)
        _number('bearish invalidation', self.bearish_invalidation, positive=True)


def _match(family, name, classification, bars, offsets, score, measurements=(), detected=0):
    chosen = [bars[len(bars)-1+offset] for offset in offsets]
    return PatternGeometry(family, name, classification, tuple(offsets), detected,
                           float(max(0, min(100, score))), tuple(measurements),
                           min(c.low for c in chosen), max(c.high for c in chosen))


def _directional(candle, config):
    return candle.range > 0 and candle.body_ratio > config.doji_body_max


def engulfing(bars: Sequence[OHLC], config=DEFAULT_CONFIG):
    if len(bars) < 2:
        return ()
    a, b = bars[-2:]
    if not all(_directional(c, config) for c in (a, b)) or a.direction == b.direction:
        return ()
    tolerance = a.body * config.body_tolerance
    if (b.body_low > a.body_low + tolerance or b.body_high < a.body_high - tolerance
            or b.body < a.body * config.engulfing_body_min):
        return ()
    ratio = b.body / a.body
    return (_match('ENGULFING', f'{b.direction.lower()}_engulfing', b.direction, bars,
                   (-1, 0), 60 + min(40, 20 * (ratio-1)), (('body_expansion', ratio),)),)


def harami(bars: Sequence[OHLC], config=DEFAULT_CONFIG):
    if len(bars) < 2:
        return ()
    a, b = bars[-2:]
    if not all(_directional(c, config) for c in (a, b)) or a.direction == b.direction:
        return ()
    tolerance = a.body * config.body_tolerance
    if (a.body_ratio < config.long_body_min or b.body > a.body * config.harami_body_max
            or b.body_low < a.body_low - tolerance or b.body_high > a.body_high + tolerance):
        return ()
    contraction = b.body / a.body
    return (_match('HARAMI', f'{b.direction.lower()}_harami', b.direction, bars,
                   (-1, 0), 60 + 40 * (1-contraction), (('body_contraction', contraction),)),)


def wick_reversals(bars: Sequence[OHLC], config=DEFAULT_CONFIG):
    if not bars:
        return ()
    c = bars[-1]
    if not _directional(c, config) or c.body_ratio > config.wick_body_max:
        return ()
    result = []
    for family, name, wick, opposite in (
        ('HAMMER_HANGING_MAN', 'lower_wick', c.lower_wick, c.upper_wick),
        ('SHOOTING_STAR_INVERTED_HAMMER', 'upper_wick', c.upper_wick, c.lower_wick),
    ):
        if wick >= config.wick_body_min * c.body and opposite <= config.wick_opposite_max * c.range:
            score = 50 + 30 * min(wick / c.body / 4, 1) + 20 * (1-opposite / c.range)
            result.append(_match(family, name, 'NEUTRAL', bars, (0,), score,
                                 (('dominant_wick_to_body', wick/c.body),)))
    return tuple(result)


def stars(bars: Sequence[OHLC], config=DEFAULT_CONFIG):
    if len(bars) < 3:
        return ()
    a, b, c = bars[-3:]
    if (not a.range or not b.range or not c.range or a.body_ratio < config.long_body_min
            or b.body > a.body * config.star_middle_max or c.body_ratio < config.star_third_body_min):
        return ()
    if a.direction == 'BEARISH' and c.direction == 'BULLISH':
        location = b.body_high <= a.close + config.star_middle_location_max * a.body
        reversal = c.close >= a.close + config.star_penetration * a.body and c.close > b.body_high
        started_near_extreme = c.open <= a.close + config.star_penetration * a.body
        name, classification = 'morning_star', 'BULLISH'
    elif a.direction == 'BULLISH' and c.direction == 'BEARISH':
        location = b.body_low >= a.close - config.star_middle_location_max * a.body
        reversal = c.close <= a.close - config.star_penetration * a.body and c.close < b.body_low
        started_near_extreme = c.open >= a.close - config.star_penetration * a.body
        name, classification = 'evening_star', 'BEARISH'
    else:
        return ()
    if not (location and reversal and started_near_extreme):
        return ()
    penetration = abs(c.close-a.close) / a.body
    return (_match('MORNING_EVENING_STAR', name, classification, bars, (-2, -1, 0),
                   60 + 40 * min(penetration, 1), (('penetration', penetration),)),)


def hikkake(bars: Sequence[OHLC], config=DEFAULT_CONFIG):
    """Strict inside bar -> false break -> FIRST opposite close within N bars.

    An extension of the false-break extreme cancels before confirmation, even
    if the same bar also closes through the opposite inside-bar boundary.
    """
    n = len(bars)
    result = []
    for failed_index in range(max(2, n-1-config.hikkake_confirmation_bars), n-1):
        mother, inside, failed = bars[failed_index-2:failed_index+1]
        if not all(c.range > 0 for c in (mother, inside, failed)):
            continue
        if not (inside.high < mother.high and inside.low > mother.low):
            continue
        bullish = failed.high < inside.high and failed.low < inside.low
        bearish = failed.high > inside.high and failed.low > inside.low
        if not (bullish or bearish):
            continue
        for index in range(failed_index+1, n):
            c = bars[index]
            cancelled = c.low < failed.low if bullish else c.high > failed.high
            confirmed = c.close > inside.high if bullish else c.close < inside.low
            if cancelled:
                break
            if confirmed:
                if index == n-1:
                    classification = 'BULLISH' if bullish else 'BEARISH'
                    match = _match('HIKKAKE', f'{classification.lower()}_hikkake', classification,
                                   bars, range(failed_index-2-(n-1), 1), 75,
                                   (('confirmation_bars', n-1-failed_index),), failed_index-(n-1))
                    # Invalidation belongs to the false-break extreme, not the mother bar.
                    result.append(PatternGeometry(
                        match.family, match.name, match.classification, match.candle_offsets,
                        match.detected_offset, match.geometry_score, match.measurements,
                        failed.low, failed.high))
                break  # A prior confirmation is never emitted again.
    return tuple(result)


def doji(bars: Sequence[OHLC], config=DEFAULT_CONFIG):
    if not bars or not bars[-1].range or bars[-1].body_ratio > config.doji_body_max:
        return ()
    c = bars[-1]
    return (_match('DOJI', 'doji', 'NEUTRAL', bars, (0,),
                   100 * (1-c.body_ratio), (('body_ratio', c.body_ratio),)),)


def detect_geometry(bars: Sequence[OHLC], config=DEFAULT_CONFIG):
    """Evaluate patterns confirmed by the LAST OHLC candle, with no context votes."""
    return tuple(match for detector in (harami, hikkake, engulfing, wick_reversals, stars, doji)
                 for match in detector(bars, config))
