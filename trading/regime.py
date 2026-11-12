"""Multi-indicator regime evidence using the existing causal indicator functions."""
from dataclasses import dataclass

import pandas as pd

from strategy import adx, atr, ema
from trading.m6_data import (Evidence, INTERVALS, Source, Status, identity, integer,
                             market_bars, positive)
from trading.pattern_context import utc_timestamp


@dataclass(frozen=True)
class RegimeConfig:
    fast_ema: int = 20
    slow_ema: int = 50
    indicator_bars: int = 14
    band_bars: int = 20
    slope_bars: int = 3
    reference_bars: int = 20
    persistence_bars: int = 2
    trend_adx: float = 25
    range_adx: float = 18
    min_slope_atr: float = .10
    low_atr_pct: float = .3
    high_atr_pct: float = 3.5
    low_band_width_pct: float = 1
    high_band_width_pct: float = 10
    expansion_ratio: float = 1.3
    contraction_ratio: float = .75
    max_age_intervals: float = 1

    def __post_init__(self) -> None:
        for name in ('fast_ema', 'slow_ema', 'indicator_bars', 'band_bars', 'slope_bars',
                     'reference_bars', 'persistence_bars'):
            integer(name, getattr(self, name), 2 if name in ('indicator_bars', 'band_bars') else 1)
        for name in vars(self):
            positive(name, getattr(self, name))
        if not (self.fast_ema < self.slow_ema and self.range_adx < self.trend_adx <= 100
                and self.low_atr_pct < self.high_atr_pct
                and self.low_band_width_pct < self.high_band_width_pct
                and self.contraction_ratio < 1 < self.expansion_ratio):
            raise ValueError('Inconsistent regime thresholds')


@dataclass(frozen=True)
class RegimeResult:
    symbol: str
    timeframe: str
    as_of: pd.Timestamp
    status: Status
    classification: str
    volatility_state: str
    volatility_change: str
    confidence: float  # Rule agreement, never probability.
    available_at: pd.Timestamp | None
    candle_closed_at: pd.Timestamp | None
    source: Source
    configuration_id: str
    config: RegimeConfig
    evidence: tuple[Evidence, ...]
    input_ids: tuple[str, ...]
    reasons: tuple[str, ...]


@dataclass(frozen=True)
class RegimeEngine:
    config: RegimeConfig = RegimeConfig()

    def analyze(self, raw: pd.DataFrame, symbol: str, timeframe: str = '4h', *,
                as_of: pd.Timestamp, source: Source | None = None) -> RegimeResult:
        if not isinstance(self.config, RegimeConfig):
            raise ValueError('Typed regime configuration required')
        as_of = utc_timestamp(as_of)
        source = source or Source('caller', 'unspecified', symbol, 'completed_OHLCV')
        if not isinstance(source, Source):
            raise ValueError('Source required')
        bars = [b for b in market_bars(raw, symbol, timeframe) if b.available_at <= as_of]
        cfg = self.config
        config_id = identity('M6-regime-v1', cfg)
        ids = tuple(b.bar_id for b in bars)

        def result(status: Status, classification: str = 'UNKNOWN', volatility: str = 'UNKNOWN',
                   change: str = 'UNKNOWN', confidence: float = 0., evidence: tuple = (), reasons: tuple = ()) -> RegimeResult:
            return RegimeResult(symbol, timeframe, as_of, status, classification, volatility, change,
                                confidence, bars[-1].available_at if bars else None,
                                bars[-1].closed_at if bars else None, source, config_id, cfg, evidence, ids, reasons)

        required = max(cfg.slow_ema+cfg.slope_bars, cfg.indicator_bars*2,
                       cfg.band_bars+cfg.reference_bars, cfg.indicator_bars+cfg.reference_bars)+cfg.persistence_bars-1
        if len(bars) < required:
            return result('INSUFFICIENT', reasons=('insufficient_completed_history',))
        if as_of-bars[-1].closed_at > cfg.max_age_intervals*INTERVALS[timeframe]:
            return result('STALE', reasons=('stale_latest_candle',))
        prices = pd.DataFrame([vars(b.ohlc) for b in bars])
        fast, slow = ema(prices.close, cfg.fast_ema), ema(prices.close, cfg.slow_ema)
        atrs, adxs = atr(prices, cfg.indicator_bars), adx(prices, cfg.indicator_bars)
        middle = prices.close.rolling(cfg.band_bars).mean()
        widths = 400*prices.close.rolling(cfg.band_bars).std(ddof=0)/middle
        slopes = (slow-slow.shift(cfg.slope_bars))/atrs.replace(0, float('nan'))
        atr_pcts = 100*atrs/prices.close
        atr_reference = atr_pcts.shift(1).rolling(cfg.reference_bars).median()
        width_reference = widths.shift(1).rolling(cfg.reference_bars).median()

        def classify(i: int) -> str:
            # A completely flat window makes the inherited ADX undefined, not bullish.
            flat = prices.high.iloc[i-cfg.band_bars+1:i+1].max() == prices.low.iloc[i-cfg.band_bars+1:i+1].min()
            if flat:
                return 'RANGE'
            if pd.isna(adxs.iloc[i]) or pd.isna(slopes.iloc[i]):
                return 'TRANSITION'
            if adxs.iloc[i] >= cfg.trend_adx:
                if prices.close.iloc[i] > fast.iloc[i] > slow.iloc[i] and slopes.iloc[i] >= cfg.min_slope_atr:
                    return 'BULLISH'
                if prices.close.iloc[i] < fast.iloc[i] < slow.iloc[i] and slopes.iloc[i] <= -cfg.min_slope_atr:
                    return 'BEARISH'
            if adxs.iloc[i] <= cfg.range_adx and abs(slopes.iloc[i]) < cfg.min_slope_atr:
                return 'RANGE'
            return 'TRANSITION'

        states = [classify(i) for i in range(len(bars)-cfg.persistence_bars, len(bars))]
        classification = states[-1] if len(set(states)) == 1 else 'TRANSITION'
        av, bw = float(atr_pcts.iloc[-1]), float(widths.iloc[-1])
        volatility = 'HIGH' if av >= cfg.high_atr_pct and bw >= cfg.high_band_width_pct else 'LOW' if av <= cfg.low_atr_pct and bw <= cfg.low_band_width_pct else 'NORMAL'
        ar = float(atr_pcts.iloc[-1]/atr_reference.iloc[-1]) if atr_reference.iloc[-1] > 0 else None
        br = float(widths.iloc[-1]/width_reference.iloc[-1]) if width_reference.iloc[-1] > 0 else None
        change = 'EXPANSION' if ar is not None and br is not None and min(ar, br) >= cfg.expansion_ratio else 'CONTRACTION' if ar is not None and br is not None and max(ar, br) <= cfg.contraction_ratio else 'STABLE' if ar is not None and br is not None else 'UNKNOWN'
        evidence = []
        values = [('adx', adxs.iloc[-1], 'index'), ('atr', atrs.iloc[-1], 'quote'),
                  ('atr_pct', av, 'percent'), ('ema_fast', fast.iloc[-1], 'quote'),
                  ('ema_slow', slow.iloc[-1], 'quote'), ('ema_slope_atr', slopes.iloc[-1], 'ATR'),
                  ('band_width_pct', bw, 'percent'), ('atr_expansion_ratio', ar, 'ratio'),
                  ('band_expansion_ratio', br, 'ratio')]
        volumes = [b.volume for b in bars]
        if all(v is not None for v in volumes[-cfg.reference_bars-1:]):
            baseline = pd.Series(volumes).iloc[-cfg.reference_bars-1:-1].mean()
            values.append(('relative_volume', volumes[-1]/baseline if baseline > 0 else None, 'ratio'))
        for name, value, unit in values:
            evidence.append(Evidence(name, float(value) if value is not None and pd.notna(value) else None, unit, ids))
        return result('AVAILABLE', classification, volatility, change,
                      100. if classification in ('BULLISH', 'BEARISH', 'RANGE') else 0., tuple(evidence),
                      ('rule_agreement_not_probability', 'candle_receipt_assumed_at_close' if 'available_at' not in raw else 'explicit_candle_availability'))
