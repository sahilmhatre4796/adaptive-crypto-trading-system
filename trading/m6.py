"""Versioned M7 evidence bundle. No signal, risk or execution integration."""
from dataclasses import dataclass, fields, is_dataclass
from math import isfinite
from types import UnionType
from typing import Any, Literal, Union, get_args, get_origin, get_type_hints

import pandas as pd

from trading.derivatives import DerivativesResult, MetricResult, UNITS
from trading.liquidation import ClusterAnalysis, HeatmapEvent, HeatmapResult
from trading.m6_data import INTERVALS, jsonable, scope
from trading.pattern_context import utc_timestamp
from trading.regime import RegimeResult


def _decode(expected: Any, value: Any, path: str) -> Any:
    """Decode only this module's trusted type annotations, never payload type names."""
    origin, arguments = get_origin(expected), get_args(expected)
    if origin in (Union, UnionType):
        for option in arguments:
            try:
                return _decode(option, value, path)
            except (ValueError, TypeError, OverflowError):
                pass
        raise ValueError(f'{path}: invalid union value')
    if expected is type(None):
        if value is not None:
            raise ValueError(f'{path}: expected null')
        return None
    if origin is Literal:
        if type(value) is not str or value not in arguments:
            raise ValueError(f'{path}: unsupported value')
        return value
    if origin is tuple:
        if not isinstance(value, list):
            raise ValueError(f'{path}: expected JSON array')
        return tuple(_decode(arguments[0], item, f'{path}[{i}]') for i, item in enumerate(value))
    if expected is pd.Timestamp:
        if not isinstance(value, str) or not value.strip():
            raise ValueError(f'{path}: expected timestamp string')
        return utc_timestamp(value)
    if is_dataclass(expected):
        names = {f.name for f in fields(expected)}
        if not isinstance(value, dict) or set(value) != names:
            raise ValueError(f'{path}: missing or unknown fields')
        hints = get_type_hints(expected)
        model = expected(**{name: _decode(hints[name], item, f'{path}.{name}')
                            for name, item in value.items()})
        _validate_decoded(model, path)
        return model
    if expected is float:
        if type(value) not in (int, float) or not isfinite(value):
            raise ValueError(f'{path}: expected finite number')
        return value
    if expected in (str, int, bool):
        if type(value) is not expected or (expected is str and not value.strip()):
            raise ValueError(f'{path}: invalid {expected.__name__}')
        return value
    raise ValueError(f'{path}: unsupported field type')


def _validate_decoded(model: Any, path: str) -> None:
    """Wire-boundary checks; engine-computed evidence is not recomputed or authenticated."""
    def require(condition: bool, reason: str) -> None:
        if not condition:
            raise ValueError(f'{path}: {reason}')

    if isinstance(model, (RegimeResult, DerivativesResult, HeatmapResult)):
        scope(model.symbol, model.timeframe)
        require(model.confidence is None or 0 <= model.confidence <= 100, 'invalid confidence')
    if isinstance(model, RegimeResult):
        require(model.timeframe in INTERVALS, 'regime requires candle timeframe')
        require(model.classification in ('UNKNOWN', 'BULLISH', 'BEARISH', 'RANGE', 'TRANSITION'), 'invalid regime')
        require(model.volatility_state in ('UNKNOWN', 'HIGH', 'LOW', 'NORMAL'), 'invalid volatility state')
        require(model.volatility_change in ('UNKNOWN', 'EXPANSION', 'CONTRACTION', 'STABLE'), 'invalid volatility change')
        require((model.available_at is None) == (model.candle_closed_at is None), 'incomplete candle timing')
        if model.available_at is not None:
            require(model.candle_closed_at <= model.available_at <= model.as_of, 'future or invalid candle timing')
    if isinstance(model, MetricResult):
        require(model.metric in UNITS, 'invalid metric')
        require(model.age_seconds is None or model.age_seconds >= 0, 'negative observation age')
        require(model.condition in ('UNKNOWN', 'UNUSUALLY_POSITIVE', 'UNUSUALLY_NEGATIVE', 'NORMAL',
                                    'POTENTIALLY_LONG_CROWDED', 'POTENTIALLY_SHORT_CROWDED', 'BALANCED',
                                    'POSITIVE_PREMIUM', 'NEGATIVE_PREMIUM', 'RISING', 'FALLING', 'STABLE'),
                'invalid metric condition')
        for observation in (model.observation, model.previous):
            require(observation is None or observation.metric == model.metric, 'metric observation mismatch')
        if model.previous is not None:
            require(model.observation is not None and model.previous.series_key == model.observation.series_key
                    and model.previous.time.observed_at < model.observation.time.observed_at,
                    'invalid preceding observation')
    if isinstance(model, DerivativesResult):
        for metric in model.metrics:
            for observation in (metric.observation, metric.previous):
                if observation is not None:
                    require((observation.symbol, observation.timeframe) == (model.symbol, model.timeframe),
                            'observation scope mismatch')
                    require(observation.time.available_at <= model.as_of, 'future observation')
                    require(model.mode != 'BACKTEST' or observation.time.historical_eligible,
                            'current-only observation in backtest')
    if isinstance(model, ClusterAnalysis):
        require(model.distance_pct >= 0 and model.proximity_rank >= 1, 'invalid distance/proximity rank')
        require(model.intensity_rank is None or model.intensity_rank >= 1, 'invalid intensity rank')
        require(model.relative_intensity is None or 0 <= model.relative_intensity <= 1, 'invalid relative intensity')
        require(model.location in ('BELOW', 'ABOVE', 'INSIDE'), 'invalid zone location')
    if isinstance(model, HeatmapEvent):
        require(model.kind in ('APPROACHING', 'ENTERING', 'THROUGH', 'SWEEP_REVERSAL',
                               'THROUGH_NO_REVERSAL', 'STRENGTHENING', 'WEAKENING'), 'invalid heatmap event')
        require(model.price_closed_at is None or model.price_closed_at <= model.available_at, 'invalid event timing')
    if isinstance(model, HeatmapResult):
        require(model.price_timeframe in INTERVALS, 'invalid price timeframe')
        require(model.age_seconds is None or model.age_seconds >= 0, 'negative snapshot age')
        require(model.price is None or model.price > 0, 'invalid price')
        require(model.price_available_at is None or model.price_available_at <= model.as_of, 'future price')
        for share in (model.long_concentration_share, model.short_concentration_share):
            require(share is None or 0 <= share <= 1, 'invalid concentration share')
        if model.snapshot is not None:
            snapshot = model.snapshot
            require((snapshot.symbol, snapshot.timeframe) == (model.symbol, model.timeframe), 'snapshot scope mismatch')
            require(snapshot.time.available_at <= model.as_of, 'future snapshot')
            require(model.mode != 'BACKTEST' or snapshot.time.historical_eligible, 'current-only snapshot in backtest')
        require(all(event.available_at <= model.as_of for event in model.events), 'future event')


@dataclass(frozen=True)
class M6Bundle:
    symbol: str
    as_of: pd.Timestamp
    regime: tuple[RegimeResult, ...]
    derivatives: tuple[DerivativesResult, ...]
    heatmaps: tuple[HeatmapResult, ...]
    schema_version: str = 'M6-v1'

    def __post_init__(self) -> None:
        from trading.pattern_context import utc_timestamp
        from trading.pattern_engine import SYMBOLS
        object.__setattr__(self, 'as_of', utc_timestamp(self.as_of))
        if self.symbol not in SYMBOLS or self.schema_version != 'M6-v1':
            raise ValueError('Unsupported bundle symbol/schema')
        for results, expected in ((self.regime, RegimeResult), (self.derivatives, DerivativesResult),
                                  (self.heatmaps, HeatmapResult)):
            if not isinstance(results, tuple) or any(not isinstance(r, expected) for r in results):
                raise ValueError('Immutable typed analysis result tuples required')
            if any(r.symbol != self.symbol or r.as_of != self.as_of for r in results):
                raise ValueError('Bundle components must share symbol and decision cutoff')
            keys = [(r.timeframe, r.snapshot.source if isinstance(r, HeatmapResult) and r.snapshot else None) for r in results]
            if len(keys) != len(set(keys)):
                raise ValueError('Duplicate component timeframe/source')
        modes = {r.mode for r in (*self.derivatives, *self.heatmaps)}
        if len(modes) > 1:
            raise ValueError('Cannot mix current and historical external evidence')

    def to_dict(self) -> dict[str, Any]:
        return jsonable(self)

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> 'M6Bundle':
        """Validate a complete M6-v1 JSON-shaped export and restore nested models.

        This checks shape, domains and causal bounds, not provenance authenticity
        or analytical correctness. Only the owner of a verified archive may assert
        historical eligibility; deserialization cannot establish that fact.
        """
        if not isinstance(payload, dict) or payload.get('schema_version') != 'M6-v1':
            raise ValueError('Unsupported or missing M6 bundle schema version')
        try:
            return _decode(cls, payload, 'bundle')
        except (TypeError, OverflowError) as exc:
            raise ValueError('Malformed M6 bundle payload') from exc
