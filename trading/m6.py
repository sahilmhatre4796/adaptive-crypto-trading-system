"""Versioned M7 evidence bundle. No signal, risk or execution integration."""
from dataclasses import dataclass
from typing import Any

import pandas as pd

from trading.derivatives import DerivativesResult
from trading.liquidation import HeatmapResult
from trading.m6_data import jsonable
from trading.regime import RegimeResult


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
