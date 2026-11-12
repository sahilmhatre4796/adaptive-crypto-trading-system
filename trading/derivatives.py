"""Independent observed derivatives series and bounded, venue-specific interpretations."""
from dataclasses import dataclass
from typing import Iterable

import pandas as pd

from trading.m6_data import (Evidence, Mode, ObservationTime, Source, Status, identity,
                             positive, scope, unique_observations)
from trading.pattern_context import utc_timestamp
from trading.patterns import _number

UNITS = {'funding_rate': ('fraction',), 'open_interest': ('base', 'contracts', 'USD', 'USDT'),
         'price': ('USD', 'USDT'), 'volume': ('base', 'USD', 'USDT'), 'long_short_ratio': ('ratio',),
         'basis': ('fraction',), 'liquidations_long': ('base', 'USD', 'USDT'),
         'liquidations_short': ('base', 'USD', 'USDT')}


@dataclass(frozen=True)
class MetricObservation:
    symbol: str
    timeframe: str
    metric: str
    value: float
    unit: str
    source: Source
    time: ObservationTime
    period_seconds: float | None = None

    def __post_init__(self) -> None:
        scope(self.symbol, self.timeframe)
        if self.metric not in UNITS or self.unit not in UNITS[self.metric]:
            raise ValueError('Unknown metric or incompatible unit')
        if not isinstance(self.source, Source) or not isinstance(self.time, ObservationTime):
            raise ValueError('Typed source and observation time required')
        value = _number('metric value', self.value)
        if self.metric not in ('funding_rate', 'basis') and value < 0:
            raise ValueError('Negative nonnegative metric')
        if self.metric == 'price' and value <= 0:
            raise ValueError('Price must be positive')
        object.__setattr__(self, 'value', value)
        if self.period_seconds is not None:
            object.__setattr__(self, 'period_seconds', positive('period_seconds', self.period_seconds))

    @property
    def observation_id(self) -> str:
        return identity(self.symbol, self.timeframe, self.metric, self.unit, self.source,
                        self.time.observed_at, self.time.received_at, self.period_seconds)

    @property
    def series_key(self) -> tuple:
        return (self.symbol, self.timeframe, self.metric, self.unit, self.source, self.period_seconds)


@dataclass(frozen=True)
class MetricPolicy:
    metric: str
    max_age_seconds: float
    threshold: float
    period_seconds: float | None = None
    enabled: bool = True

    def __post_init__(self) -> None:
        if self.metric not in UNITS or not isinstance(self.enabled, bool):
            raise ValueError('Unknown metric or invalid enabled flag')
        positive('max_age_seconds', self.max_age_seconds)
        positive('threshold', self.threshold)
        if self.period_seconds is not None:
            positive('period_seconds', self.period_seconds)


@dataclass(frozen=True)
class DerivativesConfig:
    policies: tuple[MetricPolicy, ...] = (
        MetricPolicy('funding_rate', 28800, .0005, 28800),
        MetricPolicy('open_interest', 900, .03), MetricPolicy('price', 900, .001),
        MetricPolicy('volume', 900, .20), MetricPolicy('long_short_ratio', 900, 1.5),
        MetricPolicy('basis', 900, .005), MetricPolicy('liquidations_long', 900, 1),
        MetricPolicy('liquidations_short', 900, 1))
    max_change_gap_seconds: float = 3600

    def __post_init__(self) -> None:
        if not isinstance(self.policies, tuple) or any(not isinstance(p, MetricPolicy) for p in self.policies):
            raise ValueError('Immutable typed metric policies required')
        if len({p.metric for p in self.policies}) != len(self.policies):
            raise ValueError('Duplicate metric policies')
        if any(p.metric == 'long_short_ratio' and p.threshold <= 1 for p in self.policies):
            raise ValueError('Long/short crowding threshold must exceed 1')
        positive('max_change_gap_seconds', self.max_change_gap_seconds)


@dataclass(frozen=True)
class MetricResult:
    metric: str
    status: Status
    observation: MetricObservation | None
    previous: MetricObservation | None
    change_fraction: float | None
    condition: str
    age_seconds: float | None
    reasons: tuple[str, ...]


@dataclass(frozen=True)
class DerivativesResult:
    symbol: str
    timeframe: str
    as_of: pd.Timestamp
    mode: Mode
    status: Status
    confidence: float  # Fresh-series coverage, not directional conviction.
    configuration_id: str
    config: DerivativesConfig
    metrics: tuple[MetricResult, ...]
    evidence: tuple[Evidence, ...]


@dataclass(frozen=True)
class DerivativesEngine:
    config: DerivativesConfig = DerivativesConfig()

    def analyze(self, observations: Iterable[MetricObservation], symbol: str, timeframe: str, *,
                as_of: pd.Timestamp, mode: Mode = 'BACKTEST') -> DerivativesResult:
        scope(symbol, timeframe)
        if mode not in ('CURRENT', 'BACKTEST') or not isinstance(self.config, DerivativesConfig):
            raise ValueError('Typed configuration and explicit mode required')
        as_of = utc_timestamp(as_of)
        observations = tuple(observations)
        if any(not isinstance(o, MetricObservation) for o in observations):
            raise ValueError('Normalized metric observations required')
        observations = unique_observations(observations)
        selected = [o for o in observations if o.symbol == symbol and o.timeframe == timeframe
                    and o.time.available_at <= as_of and (mode != 'BACKTEST' or o.time.historical_eligible)]
        results, evidence = [], []
        cfg = self.config
        for policy in cfg.policies:
            if not policy.enabled:
                continue
            series = {}
            for observation in selected:
                if observation.metric == policy.metric:
                    series.setdefault(observation.series_key, []).append(observation)
            if not series:
                results.append(MetricResult(policy.metric, 'UNAVAILABLE', None, None, None, 'UNKNOWN', None,
                                            ('no_eligible_observations',)))
            for rows in sorted(series.values(), key=lambda rows: identity(rows[0].series_key)):
                revisions = {}
                for row in sorted(rows, key=lambda o: o.time.available_at):
                    revisions[row.time.observed_at] = row
                rows = sorted(revisions.values(), key=lambda o: o.time.observed_at)
                current = rows[-1]
                age = (as_of-current.time.observed_at).total_seconds()
                status = current.time.status(as_of, policy.max_age_seconds, mode)
                previous = rows[-2] if len(rows) >= 2 else None
                gap = (current.time.observed_at-previous.time.observed_at).total_seconds() if previous else None
                if previous is not None and gap > cfg.max_change_gap_seconds:
                    previous = None
                change = None
                if status == 'AVAILABLE' and previous is not None and previous.value > 0:
                    change = (current.value-previous.value)/previous.value
                condition, reasons = 'UNKNOWN', []
                if status == 'STALE':
                    reasons.append('stale_observation_no_interpretation')
                elif policy.period_seconds is not None and current.period_seconds != policy.period_seconds:
                    status = 'UNAVAILABLE'
                    change = None
                    reasons.append('period_not_comparable_to_threshold')
                elif policy.metric == 'funding_rate':
                    condition = 'UNUSUALLY_POSITIVE' if current.value >= policy.threshold else 'UNUSUALLY_NEGATIVE' if current.value <= -policy.threshold else 'NORMAL'
                elif policy.metric == 'long_short_ratio':
                    condition = 'POTENTIALLY_LONG_CROWDED' if current.value >= policy.threshold else 'POTENTIALLY_SHORT_CROWDED' if current.value <= 1/policy.threshold else 'BALANCED'
                    reasons.append('provider_sample_not_market_wide_positioning')
                elif policy.metric == 'basis':
                    condition = 'POSITIVE_PREMIUM' if current.value >= policy.threshold else 'NEGATIVE_PREMIUM' if current.value <= -policy.threshold else 'NORMAL'
                elif change is not None:
                    condition = 'RISING' if change >= policy.threshold else 'FALLING' if change <= -policy.threshold else 'STABLE'
                    if policy.metric == 'open_interest' and abs(change) >= policy.threshold:
                        evidence.append(Evidence('sudden_open_interest_change', change, 'fraction',
                                                 (previous.observation_id, current.observation_id)))
                else:
                    reasons.append('change_unavailable_without_positive_comparable_baseline')
                results.append(MetricResult(policy.metric, status, current, previous, change, condition, age, tuple(reasons)))

        fresh = [r for r in results if r.status == 'AVAILABLE']
        # Paired interpretations require the same venue/instrument and exact observation times.
        for oi in (r for r in fresh if r.metric == 'open_interest' and r.change_fraction is not None):
            for price in (r for r in fresh if r.metric == 'price' and r.change_fraction is not None):
                a, b = oi.observation, price.observation
                if (a.source.provider, a.source.venue, a.source.instrument) != (b.source.provider, b.source.venue, b.source.instrument):
                    continue
                if a.time.observed_at != b.time.observed_at or oi.previous.time.observed_at != price.previous.time.observed_at:
                    continue
                if oi.condition not in ('RISING', 'FALLING') or price.condition not in ('RISING', 'FALLING'):
                    continue
                evidence.append(Evidence('price_open_interest', f'PRICE_{price.condition}_OI_{oi.condition}', 'interpretation',
                                         (oi.previous.observation_id, a.observation_id, price.previous.observation_id, b.observation_id)))
        for volume in (r for r in fresh if r.metric == 'volume' and r.change_fraction is not None):
            for price in (r for r in fresh if r.metric == 'price' and r.change_fraction is not None):
                a, b = volume.observation, price.observation
                if (a.source.provider, a.source.venue, a.source.instrument, a.time.observed_at, volume.previous.time.observed_at) != (b.source.provider, b.source.venue, b.source.instrument, b.time.observed_at, price.previous.time.observed_at):
                    continue
                if price.condition in ('RISING', 'FALLING') and volume.condition == 'FALLING':
                    evidence.append(Evidence('price_volume_divergence', f'PRICE_{price.condition}_VOLUME_FALLING', 'interpretation',
                                             (volume.previous.observation_id, a.observation_id, price.previous.observation_id, b.observation_id)))
        count = len({r.metric for r in fresh})
        enabled = sum(p.enabled for p in cfg.policies)
        status = 'AVAILABLE' if enabled and count == enabled else 'PARTIAL' if count else 'STALE' if any(r.status == 'STALE' for r in results) else 'UNAVAILABLE'
        return DerivativesResult(symbol, timeframe, as_of, mode, status, 100*count/enabled if enabled else 0.,
                                 identity('M6-derivatives-v1', cfg), cfg, tuple(results), tuple(evidence))
