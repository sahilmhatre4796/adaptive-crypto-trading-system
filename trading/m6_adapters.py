"""Pure official-schema decoders for caller-supplied payloads. No HTTP or keys.

These are fixture-verified parsers, not verified provider connections. Receipt time
must be supplied by the caller; vendor observation time is never used as receipt.
"""
from collections.abc import Mapping
from numbers import Real
from typing import Any

import pandas as pd

from trading.derivatives import MetricObservation
from trading.liquidation import HeatmapSnapshot, LiquidationCluster
from trading.m6_data import ObservationTime, Source, nonempty, scope
from trading.pattern_context import utc_timestamp
from trading.patterns import OHLC, _number

BINANCE_SYMBOLS = {'BTCUSDT': 'BTC/USDT', 'ETHUSDT': 'ETH/USDT'}


def _numeric(value: Any) -> float:
    if isinstance(value, bool):
        raise ValueError('Boolean is not a provider number')
    try:
        return _number('provider value', float(value))
    except (TypeError, ValueError, OverflowError) as exc:
        raise ValueError('Invalid provider number') from exc


def _epoch(value: Any, unit: str) -> pd.Timestamp:
    if isinstance(value, bool) or not isinstance(value, Real):
        raise ValueError('Numeric provider timestamp required')
    return pd.to_datetime(_number('provider timestamp', value), unit=unit, utc=True)


def binance_open_interest(payload: Mapping[str, Any], *, received_at: pd.Timestamp) -> MetricObservation:
    if not isinstance(payload, Mapping) or payload.get('symbol') not in BINANCE_SYMBOLS:
        raise ValueError('Unsupported Binance symbol/payload')
    try:
        symbol = BINANCE_SYMBOLS[payload['symbol']]
        return MetricObservation(symbol, 'snapshot', 'open_interest', _numeric(payload['openInterest']), 'base',
                                 Source('Binance', 'Binance_USDS_M', payload['symbol'], 'openInterest_REST'),
                                 ObservationTime(_epoch(payload['time'], 'ms'), received_at))
    except KeyError as exc:
        raise ValueError('Missing Binance OI field') from exc


def binance_funding_history(payload: list[dict[str, Any]], *, received_at: pd.Timestamp,
                             period_seconds: float) -> tuple[MetricObservation, ...]:
    """Funding period is caller-verified instrument metadata, never assumed as 8h."""
    if not isinstance(payload, list):
        raise ValueError('Funding payload must be an array')
    observations = []
    for row in payload:
        if not isinstance(row, Mapping) or row.get('symbol') not in BINANCE_SYMBOLS:
            raise ValueError('Unsupported funding symbol/payload')
        try:
            observations.append(MetricObservation(
                BINANCE_SYMBOLS[row['symbol']], 'snapshot', 'funding_rate', _numeric(row['fundingRate']), 'fraction',
                Source('Binance', 'Binance_USDS_M', row['symbol'], 'settled_funding_history'),
                ObservationTime(_epoch(row['fundingTime'], 'ms'), received_at), period_seconds))
        except KeyError as exc:
            raise ValueError('Missing Binance funding field') from exc
    # A later download has no verified historical receipt metadata: deliberately ineligible.
    return tuple(observations)


def coinglass_model1_snapshot(payload: Mapping[str, Any], *, symbol: str, venue: str,
                              instrument: str, received_at: pd.Timestamp) -> HeatmapSnapshot:
    """Decode ONLY the final time column of the documented Model 1 matrix.

    Matrix cells carry no reliable original publication/receipt history. All
    selected bins become available at this response's receipt, not cell times.
    The endpoint's lookback range is not a supported observation interval.
    """
    scope(symbol, 'snapshot')
    nonempty('venue', venue)
    nonempty('instrument', instrument)
    if instrument != symbol.replace('/', ''):
        raise ValueError('Pair instrument must match normalized symbol')
    if not isinstance(payload, Mapping) or payload.get('code') != '0' or not isinstance(payload.get('data'), Mapping):
        raise ValueError('Unsuccessful/malformed CoinGlass response')
    data = payload['data']
    try:
        levels, candles, cells = data['y_axis'], data['price_candlesticks'], data['liquidation_leverage_data']
        if not all(isinstance(v, list) for v in (levels, candles, cells)) or not candles:
            raise ValueError('Heatmap arrays and candles required')
        levels = [_numeric(p) for p in levels]
        if any(p <= 0 for p in levels) or len(set(levels)) != len(levels):
            raise ValueError('Positive unique price levels required')
        observed = _epoch(data['update_time'], 'ms')
        times, prices = [], []
        for row in candles:
            if not isinstance(row, list) or len(row) != 6:
                raise ValueError('Expected six-element CoinGlass candle')
            times.append(_epoch(row[0], 's'))
            prices.append(OHLC(*[_numeric(v) for v in row[1:5]]))
            if _numeric(row[5]) < 0:
                raise ValueError('Negative candle volume')
        if any(a >= b for a, b in zip(times, times[1:])) or times[-1] > observed:
            raise ValueError('Invalid matrix candle times')
        reference = prices[-1].close
        selected, seen = [], set()
        for cell in cells:
            if not isinstance(cell, list) or len(cell) != 3:
                raise ValueError('Expected [time index, price index, intensity]')
            i, j, value = cell
            if any(isinstance(v, bool) or not isinstance(v, int) for v in (i, j)):
                raise ValueError('Matrix indices must be integers')
            if not 0 <= i < len(candles) or not 0 <= j < len(levels) or (i, j) in seen:
                raise ValueError('Out-of-range/duplicate matrix cell')
            seen.add((i, j))
            value = _numeric(value)
            if value < 0:
                raise ValueError('Negative model intensity')
            if i != len(candles)-1 or levels[j] == reference:
                continue
            price = levels[j]
            selected.append(LiquidationCluster(f'price:{price:.17g}', price, price,
                                               'LONG' if price < reference else 'SHORT', value,
                                               side_basis='PRICE_RELATIVE_ESTIMATE'))
        return HeatmapSnapshot(symbol, 'snapshot', Source('CoinGlass', venue, instrument, 'liquidation_heatmap_model1_final_column'),
                               ObservationTime(observed, utc_timestamp(received_at)), reference,
                               'model1_intensity_not_currency', ('snapshot',), tuple(selected))
    except KeyError as exc:
        raise ValueError('Missing CoinGlass matrix field') from exc
