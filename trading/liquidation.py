"""Estimated liquidation zones, never confirmed liquidation orders or trade signals."""
from dataclasses import dataclass
from typing import Iterable

import pandas as pd

from trading.m6_data import (INTERVALS, Mode, ObservationTime, Source, Status, identity,
                             market_bars, nonempty, positive, scope, unique_observations)
from trading.pattern_context import utc_timestamp
from trading.patterns import _number


@dataclass(frozen=True)
class LiquidationCluster:
    cluster_id: str
    lower: float
    upper: float
    side: str  # LONG / SHORT: estimated positions vulnerable at this zone.
    intensity: float | None
    quality: float | None = None
    side_basis: str = 'PROVIDER_ESTIMATE'

    def __post_init__(self) -> None:
        nonempty('cluster_id', self.cluster_id)
        for name in ('lower', 'upper'):
            object.__setattr__(self, name, positive(name, getattr(self, name)))
        if self.lower > self.upper or self.side not in ('LONG', 'SHORT'):
            raise ValueError('Invalid zone bounds or side')
        if self.side_basis not in ('PROVIDER_ESTIMATE', 'PRICE_RELATIVE_ESTIMATE'):
            raise ValueError('Unknown side methodology')
        for name in ('intensity', 'quality'):
            value = getattr(self, name)
            if value is not None:
                value = _number(name, value)
                if value < 0 or (name == 'quality' and value > 1):
                    raise ValueError('Invalid intensity/quality')
                object.__setattr__(self, name, value)


@dataclass(frozen=True)
class HeatmapSnapshot:
    symbol: str
    timeframe: str
    source: Source
    time: ObservationTime
    reference_price: float
    intensity_unit: str
    supported_timeframes: tuple[str, ...]
    clusters: tuple[LiquidationCluster, ...]

    def __post_init__(self) -> None:
        scope(self.symbol, self.timeframe)
        if not isinstance(self.source, Source) or not isinstance(self.time, ObservationTime):
            raise ValueError('Typed source/time required')
        object.__setattr__(self, 'reference_price', positive('reference_price', self.reference_price))
        nonempty('intensity_unit', self.intensity_unit)
        if (not isinstance(self.supported_timeframes, tuple) or self.timeframe not in self.supported_timeframes
                or len(set(self.supported_timeframes)) != len(self.supported_timeframes)):
            raise ValueError('Snapshot interval is not supported by this source')
        for tf in self.supported_timeframes:
            scope(self.symbol, tf)
        if not isinstance(self.clusters, tuple) or any(not isinstance(c, LiquidationCluster) for c in self.clusters):
            raise ValueError('Immutable typed clusters required')
        if len({c.cluster_id for c in self.clusters}) != len(self.clusters):
            raise ValueError('Duplicate cluster identity')
        for c in self.clusters:
            if (c.side == 'LONG' and c.upper >= self.reference_price) or (c.side == 'SHORT' and c.lower <= self.reference_price):
                raise ValueError('Potential long zones must be below and short zones above snapshot reference price')

    @property
    def observation_id(self) -> str:
        return identity(self.symbol, self.timeframe, self.source, self.time.observed_at,
                        self.time.received_at, self.intensity_unit)

    @property
    def series_key(self) -> tuple:
        return (self.symbol, self.timeframe, self.source, self.intensity_unit)


@dataclass(frozen=True)
class HeatmapConfig:
    enabled_timeframes: tuple[str, ...] = ('5m', '15m', '1h', '4h', '1d', 'snapshot')
    max_age_seconds: float = 900
    max_price_age_intervals: float = 1
    near_distance_pct: float = 1
    major_relative_intensity: float = .75
    intensity_change_fraction: float = .25
    reversal_pct: float = .1

    def __post_init__(self) -> None:
        if not isinstance(self.enabled_timeframes, tuple) or len(set(self.enabled_timeframes)) != len(self.enabled_timeframes):
            raise ValueError('Unique immutable enabled intervals required')
        for tf in self.enabled_timeframes:
            scope('BTC/USDT', tf)
        for name in ('max_age_seconds', 'max_price_age_intervals', 'near_distance_pct',
                     'major_relative_intensity', 'intensity_change_fraction', 'reversal_pct'):
            positive(name, getattr(self, name))
        if self.major_relative_intensity > 1:
            raise ValueError('Relative intensity threshold must be <= 1')


@dataclass(frozen=True)
class ClusterAnalysis:
    cluster: LiquidationCluster
    distance_pct: float
    location: str
    nearby: bool
    relative_intensity: float | None
    major: bool | None
    proximity_rank: int
    intensity_rank: int | None


@dataclass(frozen=True)
class HeatmapEvent:
    event_id: str
    kind: str
    cluster_id: str
    snapshot_id: str
    previous_snapshot_id: str | None
    available_at: pd.Timestamp
    price_closed_at: pd.Timestamp | None
    price_bar_id: str | None
    value: float | None


@dataclass(frozen=True)
class HeatmapResult:
    symbol: str
    timeframe: str
    price_timeframe: str
    as_of: pd.Timestamp
    mode: Mode
    status: Status
    configuration_id: str
    config: HeatmapConfig
    snapshot: HeatmapSnapshot | None
    age_seconds: float | None
    price: float | None
    price_available_at: pd.Timestamp | None
    price_source: Source
    input_bar_ids: tuple[str, ...]
    clusters: tuple[ClusterAnalysis, ...]
    long_concentration_share: float | None
    short_concentration_share: float | None
    confidence: float | None  # Provider quality is not invented from model intensity.
    events: tuple[HeatmapEvent, ...]
    reasons: tuple[str, ...]


def _distance(price: float, c: LiquidationCluster) -> float:
    return 100*max(c.lower-price, price-c.upper, 0.)/price


@dataclass(frozen=True)
class HeatmapEngine:
    config: HeatmapConfig = HeatmapConfig()

    def analyze(self, snapshots: Iterable[HeatmapSnapshot], raw_prices: pd.DataFrame, symbol: str,
                timeframe: str, *, as_of: pd.Timestamp, price_timeframe: str = '5m',
                mode: Mode = 'BACKTEST', source: Source | None = None,
                price_source: Source | None = None) -> HeatmapResult:
        scope(symbol, timeframe)
        if mode not in ('CURRENT', 'BACKTEST') or not isinstance(self.config, HeatmapConfig):
            raise ValueError('Typed config and explicit mode required')
        as_of = utc_timestamp(as_of)
        price_source = price_source or Source('caller', 'unspecified', symbol, 'completed_OHLC')
        if not isinstance(price_source, Source) or (source is not None and not isinstance(source, Source)):
            raise ValueError('Typed sources required')
        snapshots = tuple(snapshots)
        if any(not isinstance(s, HeatmapSnapshot) for s in snapshots):
            raise ValueError('Normalized heatmap snapshots required')
        snapshots = unique_observations(snapshots)
        eligible = [s for s in snapshots if s.symbol == symbol and s.timeframe == timeframe
                    and s.time.available_at <= as_of and (mode != 'BACKTEST' or s.time.historical_eligible)
                    and (source is None or s.source == source)]
        if len({s.series_key for s in eligible}) > 1:
            raise ValueError('Analyze each provider/method/unit stream separately')
        eligible.sort(key=lambda s: (s.time.observed_at, s.time.available_at))
        bars = [b for b in market_bars(raw_prices, symbol, price_timeframe) if b.available_at <= as_of]
        snapshot = eligible[-1] if eligible else None
        cfg, events = self.config, []
        config_id = identity('M6-heatmap-v1', cfg)
        age = (as_of-snapshot.time.observed_at).total_seconds() if snapshot else None

        def result(status: Status, clusters: tuple = (), long_share: float | None = None,
                   short_share: float | None = None, reasons: tuple = ()) -> HeatmapResult:
            quality = [c.cluster.quality for c in clusters]
            return HeatmapResult(symbol, timeframe, price_timeframe, as_of, mode, status, config_id, cfg,
                                 snapshot, age, bars[-1].ohlc.close if bars else None,
                                 bars[-1].available_at if bars else None, price_source,
                                 tuple(b.bar_id for b in bars), clusters, long_share, short_share,
                                 100*min(quality) if quality and all(q is not None for q in quality) else None,
                                 tuple(sorted(events, key=lambda e: (e.available_at, e.event_id))), reasons)

        if timeframe not in cfg.enabled_timeframes:
            return result('UNAVAILABLE', reasons=('timeframe_disabled',))
        # Model-change events compare only matching, unchanged bins in one series.
        previous = None
        for current in sorted(eligible, key=lambda s: (s.time.available_at, s.time.observed_at)):
            if previous is not None and current.time.observed_at > previous.time.observed_at:
                if (previous.time.status(current.time.available_at, cfg.max_age_seconds, mode) == 'AVAILABLE'
                        and current.time.status(current.time.available_at, cfg.max_age_seconds, mode) == 'AVAILABLE'):
                    before = {c.cluster_id: c for c in previous.clusters}
                    for c in current.clusters:
                        old = before.get(c.cluster_id)
                        if old is None or (old.lower, old.upper, old.side) != (c.lower, c.upper, c.side):
                            continue
                        if old.intensity is None or c.intensity is None or old.intensity <= 0:
                            continue
                        change = (c.intensity-old.intensity)/old.intensity
                        if abs(change) >= cfg.intensity_change_fraction:
                            kind = 'STRENGTHENING' if change > 0 else 'WEAKENING'
                            events.append(HeatmapEvent(identity(config_id, kind, current.observation_id, c.cluster_id),
                                                       kind, c.cluster_id, current.observation_id, previous.observation_id,
                                                       current.time.available_at, None, None, change))
            if previous is None or current.time.observed_at >= previous.time.observed_at:
                previous = current
        seen, passed = set(), {}
        for bar in bars:
            known = [s for s in eligible if s.time.available_at <= bar.opened_at
                     and s.time.status(bar.available_at, cfg.max_age_seconds, mode) == 'AVAILABLE']
            if not known:
                continue
            known_snapshot = max(known, key=lambda s: (s.time.observed_at, s.time.available_at))
            for c in known_snapshot.clusters:
                key = (known_snapshot.observation_id, c.cluster_id)
                price = bar.ohlc
                kinds = []
                distance = _distance(price.close, c)
                if 0 < distance <= cfg.near_distance_pct and distance < _distance(price.open, c):
                    kinds.append('APPROACHING')
                if price.low <= c.upper and price.high >= c.lower:
                    kinds.append('ENTERING')
                short = c.side == 'SHORT'
                swept = (price.open < c.lower and price.high > c.upper and price.close < c.lower and
                         100*(price.high-price.close)/price.high >= cfg.reversal_pct) if short else (
                         price.open > c.upper and price.low < c.lower and price.close > c.upper and
                         100*(price.close-price.low)/price.low >= cfg.reversal_pct)
                through = price.open < c.lower and price.close > c.upper if short else price.open > c.upper and price.close < c.lower
                held = price.low > c.upper if short else price.high < c.lower
                if swept:
                    kinds.append('SWEEP_REVERSAL')
                elif through:
                    kinds.append('THROUGH')
                    passed[key] = bar.closed_at
                elif key in passed and bar.opened_at >= passed[key] and held:
                    kinds.append('THROUGH_NO_REVERSAL')
                for kind in kinds:
                    if (*key, kind) in seen:
                        continue
                    seen.add((*key, kind))
                    events.append(HeatmapEvent(identity(config_id, key, kind, bar.bar_id), kind, c.cluster_id,
                                               known_snapshot.observation_id, None, bar.available_at,
                                               bar.closed_at, bar.bar_id, None))
        if snapshot is None:
            return result('UNAVAILABLE', reasons=('no_point_in_time_heatmap' if mode == 'BACKTEST' else 'no_available_heatmap',))
        if snapshot.time.status(as_of, cfg.max_age_seconds, mode) == 'STALE':
            return result('STALE', reasons=('stale_snapshot_no_current_clusters',))
        if not bars or as_of-bars[-1].closed_at > cfg.max_price_age_intervals*INTERVALS[price_timeframe]:
            return result('UNAVAILABLE', reasons=('missing_or_stale_current_price',))
        price = bars[-1].ohlc.close
        clusters = snapshot.clusters
        maximum = max((c.intensity for c in clusters if c.intensity is not None), default=0.)
        proximity = {c.cluster_id: i+1 for i, c in enumerate(sorted(clusters, key=lambda c: (_distance(price, c), c.cluster_id)))}
        intensity_rank = {c.cluster_id: i+1 for i, c in enumerate(sorted((c for c in clusters if c.intensity is not None), key=lambda c: (-c.intensity, c.cluster_id)))}
        analyses = []
        for c in clusters:
            relative = c.intensity/maximum if c.intensity is not None and maximum > 0 else None
            analyses.append(ClusterAnalysis(c, _distance(price, c), 'BELOW' if c.upper < price else 'ABOVE' if c.lower > price else 'INSIDE',
                                            _distance(price, c) <= cfg.near_distance_pct, relative,
                                            relative >= cfg.major_relative_intensity if relative is not None else None,
                                            proximity[c.cluster_id], intensity_rank.get(c.cluster_id)))
        total = sum(c.relative_intensity for c in analyses if c.relative_intensity is not None)
        complete = all(c.intensity is not None for c in clusters)
        long_share = sum(c.relative_intensity for c in analyses if c.cluster.side == 'LONG')/total if complete and total > 0 else None
        short_share = 1-long_share if long_share is not None else None
        reasons = ('estimated_zones_not_confirmed_orders', 'confidence_requires_provider_quality')
        return result('AVAILABLE' if complete else 'PARTIAL', tuple(analyses), long_share, short_share, reasons)
