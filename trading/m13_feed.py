"""Validate public USD-M 5m kline rows without network or trading effects."""

from decimal import Decimal, InvalidOperation

import pandas as pd

from trading.candle_driver import CandleInput
from trading.pattern_context import utc_timestamp
from trading.patterns import OHLC


SYMBOLS = {'BTCUSDT': 'BTC/USDT', 'ETHUSDT': 'ETH/USDT'}
INTERVAL_MS = 300_000
SOURCE_IDS = {name: f'binance-usdm-public-5m-v1:{name}' for name in SYMBOLS}


def _millis(value, field):
    if type(value) is not int or value < 0:
        raise ValueError(f'Invalid {field} milliseconds')
    return value


def _finite(value, field):
    if type(value) is not str:
        raise ValueError(f'Invalid {field} value')
    try:
        number = Decimal(value)
    except InvalidOperation as exc:
        raise ValueError(f'Invalid {field} value') from exc
    if not number.is_finite():
        raise ValueError(f'Nonfinite {field} value')
    return float(number)


def parse_klines(symbol: str, rows, *, received_at, warmup=False) -> tuple[CandleInput, ...]:
    """Return only closed rows; warmup uses the approved close-time receipt convention."""
    if symbol not in SYMBOLS or type(rows) is not list or type(warmup) is not bool:
        raise ValueError('Supported symbol, list response and explicit mode required')
    received = utc_timestamp(received_at)
    if not rows or len(rows) > 1500:
        raise ValueError('Bounded nonempty kline response required')
    result = []
    last_open = None
    for index, row in enumerate(rows):
        if type(row) is not list or len(row) != 12:
            raise ValueError('Official 12-column kline row required')
        opened_ms = _millis(row[0], 'open')
        closed_ms = _millis(row[6], 'close')
        if opened_ms % INTERVAL_MS or closed_ms != opened_ms + INTERVAL_MS - 1:
            raise ValueError('Kline is not an exact UTC 5m interval')
        if last_open is not None and opened_ms != last_open + INTERVAL_MS:
            raise ValueError('Duplicate, missing or out-of-order kline')
        last_open = opened_ms
        closed_at = pd.Timestamp(opened_ms + INTERVAL_MS, unit='ms', tz='UTC')
        if closed_at > received:
            if warmup:
                raise ValueError('Warmup contains a future candle')
            if index != len(rows) - 1:
                raise ValueError('Unfinished candle precedes another row')
            break
        ohlc = OHLC(*(_finite(row[index], field) for index, field in
                      ((1, 'open'), (2, 'high'), (3, 'low'), (4, 'close'))))
        volume = _finite(row[5], 'volume')
        result.append(CandleInput(SYMBOLS[symbol],
                                  pd.Timestamp(opened_ms, unit='ms', tz='UTC'),
                                  ohlc, volume,
                                  closed_at if warmup else received,
                                  SOURCE_IDS[symbol]))
    if not result:
        raise ValueError('No finalized 5m candle available')
    return tuple(result)
