"""Context assessment for pattern evidence. This module never approves trades."""
from dataclasses import dataclass
from statistics import median
from math import isfinite
from numbers import Real
from typing import Sequence
import pandas as pd
from trading.patterns import OHLC, PatternGeometry, _number


def utc_timestamp(value):
    ts = pd.to_datetime(_number('timestamp', value), unit='ms', utc=True) if isinstance(value, Real) and not isinstance(value, bool) else pd.Timestamp(value)
    if pd.isna(ts):
        raise ValueError('Missing timestamp')
    return ts.tz_localize('UTC') if ts.tzinfo is None else ts.tz_convert('UTC')


@dataclass(frozen=True)
class ContextConfig:
    prior_trend_bars: int = 3
    prior_move_ranges: float = .5
    max_level_distance_atr: float = 1.0
    min_relative_volume: float = 1.0
    min_pattern_range_ratio: float = .1
    max_pattern_range_ratio: float = 3.0
    max_age_minutes: int = 15

    def __post_init__(self):
        for key, value in vars(self).items():
            if _number(key, value) <= 0:
                raise ValueError(f'{key} must be positive')
        if not isinstance(self.prior_trend_bars, int) or not 2 <= self.prior_trend_bars <= 100:
            raise ValueError('prior_trend_bars must be an integer in [2, 100]')
        if self.min_pattern_range_ratio >= self.max_pattern_range_ratio:
            raise ValueError('Invalid volatility range')


@dataclass(frozen=True)
class ContextSnapshot:
    symbol: str
    available_at: pd.Timestamp
    higher_direction: str = 'UNKNOWN'
    regime: str = 'UNKNOWN'
    direction_closed_at: pd.Timestamp | None = None
    regime_closed_at: pd.Timestamp | None = None
    support_distance_atr: float | None = None
    resistance_distance_atr: float | None = None
    atr: float | None = None
    relative_volume: float | None = None

    def __post_init__(self):
        object.__setattr__(self, 'available_at', utc_timestamp(self.available_at))
        if self.higher_direction not in ('BULLISH', 'BEARISH', 'NEUTRAL', 'UNKNOWN'):
            raise ValueError('Unknown 1H direction')
        if self.regime not in ('BULLISH', 'BEARISH', 'RANGE', 'UNCERTAIN', 'ABNORMAL', 'UNKNOWN'):
            raise ValueError('Unknown 4H regime')
        for key, rule in (('direction_closed_at', '1h'), ('regime_closed_at', '4h')):
            value = getattr(self, key)
            if value is not None:
                value = utc_timestamp(value)
                if value.floor(rule) != value or value > self.available_at:
                    raise ValueError('Higher-timeframe evidence precedes completion or is off boundary')
                object.__setattr__(self, key, value)
        if self.higher_direction != 'UNKNOWN' and self.direction_closed_at is None:
            raise ValueError('Known 1H direction requires candle completion time')
        if self.regime != 'UNKNOWN' and self.regime_closed_at is None:
            raise ValueError('Known 4H regime requires candle completion time')
        for key in ('support_distance_atr', 'resistance_distance_atr', 'atr', 'relative_volume'):
            value = getattr(self, key)
            if value is not None:
                value = _number(key, value, positive=key == 'atr')
                if value < 0:
                    raise ValueError(f'Negative context value: {key}')
                object.__setattr__(self, key, value)


@dataclass(frozen=True)
class ContextAssessment:
    name: str
    classification: str
    status: str
    prior_direction: str
    strength_score: float
    supporting: tuple[str, ...]
    conflicting: tuple[str, ...]
    missing: tuple[str, ...]


def preceding_direction(history: Sequence[OHLC], config=ContextConfig()):
    """Use only candles BEFORE the first formation candle; normalize by their ranges."""
    if len(history) < config.prior_trend_bars:
        return 'UNKNOWN', None
    history = history[-config.prior_trend_bars:]
    scale = median(c.range for c in history)
    if scale <= 0:
        return 'UNKNOWN', None
    move = (history[-1].close - history[0].close) / scale
    if not isfinite(move):
        return 'UNKNOWN', None
    direction = 'BULLISH' if move >= config.prior_move_ranges else 'BEARISH' if move <= -config.prior_move_ranges else 'NEUTRAL'
    return direction, scale


def assess_context(match: PatternGeometry, prior_direction, pattern_range, context,
                   as_of, config=ContextConfig(), recent_range=None):
    """Missing evidence earns no points; context rejection is independent of geometry."""
    as_of = utc_timestamp(as_of)
    name, classification = match.name, match.classification
    supporting, conflicting, missing = [], [], []
    score = .65 * match.geometry_score
    if match.family in ('HAMMER_HANGING_MAN', 'SHOOTING_STAR_INVERTED_HAMMER'):
        if prior_direction in ('BULLISH', 'BEARISH'):
            classification = 'BULLISH' if prior_direction == 'BEARISH' else 'BEARISH'
            if match.family == 'HAMMER_HANGING_MAN':
                name = 'hammer' if classification == 'BULLISH' else 'hanging_man'
            else:
                name = 'inverted_hammer' if classification == 'BULLISH' else 'shooting_star'
        else:
            missing.append('preceding_direction')
    if match.family == 'DOJI':
        return ContextAssessment(name, 'NEUTRAL', 'INDECISION', prior_direction, score,
                                 ('indecision_only',), (), ())
    if match.family != 'HIKKAKE' and classification != 'NEUTRAL':
        expected = 'BEARISH' if classification == 'BULLISH' else 'BULLISH'
        if prior_direction in ('UNKNOWN', 'NEUTRAL'):
            missing.append('preceding_reversal_context')
        elif prior_direction != expected:
            conflicting.append('preceding_direction_not_reversal')
        else:
            supporting.append('preceding_reversal_context')
            score += 10
    elif match.family == 'HIKKAKE':
        supporting.append('confirmed_inside_bar_failure')
        score += 10
    if context is None:
        missing.extend(('higher_direction', 'regime', 'location'))
    elif context.available_at > as_of or as_of-context.available_at > pd.Timedelta(minutes=config.max_age_minutes):
        missing.append('context_unavailable_or_stale')
    else:
        if context.direction_closed_at is not None and as_of-context.direction_closed_at > pd.Timedelta(hours=1):
            missing.append('stale_1h_direction')
        if context.regime_closed_at is not None and as_of-context.regime_closed_at > pd.Timedelta(hours=4):
            missing.append('stale_4h_regime')
        if context.higher_direction == 'UNKNOWN':
            missing.append('higher_direction')
        elif context.higher_direction not in (classification, 'NEUTRAL'):
            conflicting.append('higher_direction_conflict')
        else:
            supporting.append('higher_direction')
            score += 10
        if context.regime in ('UNKNOWN', 'UNCERTAIN'):
            missing.append('regime')
        elif context.regime == 'ABNORMAL':
            conflicting.append('abnormal_market')
        elif context.regime not in (classification, 'RANGE'):
            conflicting.append('regime_conflict')
        else:
            supporting.append('regime')
            score += 5
        distance = context.support_distance_atr if classification == 'BULLISH' else context.resistance_distance_atr
        if classification == 'NEUTRAL' or distance is None:
            missing.append('location')
        elif distance > config.max_level_distance_atr:
            conflicting.append('not_at_meaningful_level')
        else:
            supporting.append('location')
            score += 5
        if context.relative_volume is not None:
            if context.relative_volume < config.min_relative_volume:
                conflicting.append('weak_volume')
            else:
                supporting.append('volume')
                score += 5
        scale = context.atr if context.atr is not None else recent_range
        if scale is not None:
            ratio = pattern_range / scale
            if not config.min_pattern_range_ratio <= ratio <= config.max_pattern_range_ratio:
                conflicting.append('pattern_volatility_outlier')
    score = max(0.0, min(100.0, score - 15 * len(conflicting)))
    status = 'REJECTED' if conflicting else 'INSUFFICIENT' if missing or classification == 'NEUTRAL' else 'VALIDATED'
    return ContextAssessment(name, classification, status, prior_direction, score,
                             tuple(supporting), tuple(conflicting), tuple(dict.fromkeys(missing)))
