"""Shared M6 data contracts. No provider transport or trading side effects."""
from dataclasses import asdict, dataclass, is_dataclass
from hashlib import sha256
import json
from typing import Any, Literal

import pandas as pd

from trading.data import utc_times
from trading.pattern_context import utc_timestamp
from trading.pattern_engine import SYMBOLS, TIMEFRAMES
from trading.patterns import OHLC, _number

INTERVALS = {**TIMEFRAMES, '1d': pd.Timedelta(days=1)}
Mode = Literal['CURRENT', 'BACKTEST']
Status = Literal['AVAILABLE', 'PARTIAL', 'UNAVAILABLE', 'STALE', 'INSUFFICIENT']


def jsonable(value: Any) -> Any:
    if is_dataclass(value):
        return jsonable(asdict(value))
    if isinstance(value, pd.Timestamp):
        return value.isoformat()
    if isinstance(value, dict):
        return {str(k): jsonable(v) for k, v in value.items()}
    if isinstance(value, (tuple, list)):
        return [jsonable(v) for v in value]
    return value


def identity(*values: Any) -> str:
    return sha256(json.dumps(jsonable(values), sort_keys=True, allow_nan=False).encode()).hexdigest()


def positive(name: str, value: float) -> float:
    value = _number(name, value)
    if value <= 0:
        raise ValueError(f'{name} must be positive')
    return value


def integer(name: str, value: int, minimum: int = 1) -> None:
    if isinstance(value, bool) or not isinstance(value, int) or not minimum <= value <= 100000:
        raise ValueError(f'{name} must be an integer in [{minimum}, 100000]')


def nonempty(name: str, value: str) -> None:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f'{name} is required')


def scope(symbol: str, timeframe: str) -> None:
    if symbol not in SYMBOLS or timeframe not in (*INTERVALS, 'snapshot'):
        raise ValueError('Unsupported M6 symbol/timeframe')


@dataclass(frozen=True)
class Source:
    provider: str
    venue: str
    instrument: str
    methodology: str

    def __post_init__(self) -> None:
        for name, value in vars(self).items():
            nonempty(name, value)


@dataclass(frozen=True)
class ObservationTime:
    observed_at: pd.Timestamp
    received_at: pd.Timestamp
    published_at: pd.Timestamp | None = None
    historical_eligible: bool = False

    def __post_init__(self) -> None:
        if self.observed_at is None or self.received_at is None:
            raise ValueError('Observation and receipt timestamps are required')
        for name in ('observed_at', 'received_at', 'published_at'):
            value = getattr(self, name)
            if value is not None:
                object.__setattr__(self, name, utc_timestamp(value))
        if not isinstance(self.historical_eligible, bool):
            raise ValueError('historical_eligible must be explicit boolean')
        publication = self.published_at if self.published_at is not None else self.observed_at
        if not self.observed_at <= publication <= self.received_at:
            raise ValueError('Require observation <= publication <= receipt')

    @property
    def available_at(self) -> pd.Timestamp:
        return self.received_at

    def status(self, as_of: pd.Timestamp, max_age_seconds: float, mode: Mode) -> Status:
        if mode not in ('CURRENT', 'BACKTEST'):
            raise ValueError('Explicit CURRENT or BACKTEST mode required')
        if self.available_at > as_of or (mode == 'BACKTEST' and not self.historical_eligible):
            return 'UNAVAILABLE'
        return 'STALE' if (as_of-self.observed_at).total_seconds() > max_age_seconds else 'AVAILABLE'


@dataclass(frozen=True)
class Evidence:
    name: str
    value: float | str | None
    unit: str
    input_ids: tuple[str, ...]


@dataclass(frozen=True)
class MarketBar:
    symbol: str
    timeframe: str
    opened_at: pd.Timestamp
    available_at: pd.Timestamp
    ohlc: OHLC
    volume: float | None

    @property
    def closed_at(self) -> pd.Timestamp:
        return self.opened_at+INTERVALS[self.timeframe]

    @property
    def bar_id(self) -> str:
        return identity(self)


def market_bars(raw: pd.DataFrame, symbol: str, timeframe: str) -> tuple[MarketBar, ...]:
    """Reuse OHLC/UTC contracts; extend completed-candle validation to daily bars."""
    scope(symbol, timeframe)
    if timeframe not in INTERVALS:
        raise ValueError('OHLC requires a candle interval')
    if raw.columns.has_duplicates or not {'timestamp', 'open', 'high', 'low', 'close'}.issubset(raw):
        raise ValueError('Unique timestamp/OHLC columns required')
    frame = raw.copy().reset_index(drop=True)
    if 'symbol' in frame and not frame.symbol.eq(symbol).all():
        raise ValueError('Candle symbol mismatch')
    frame['timestamp'] = utc_times(frame.timestamp)
    frame = frame.sort_values('timestamp')
    interval = INTERVALS[timeframe]
    if frame.timestamp.isna().any() or frame.timestamp.duplicated().any():
        raise ValueError('Invalid/duplicate candle timestamps')
    if not frame.timestamp.eq(frame.timestamp.dt.floor(interval)).all():
        raise ValueError('Off-boundary candles')
    if len(frame) > 1 and not frame.timestamp.diff().iloc[1:].eq(interval).all():
        raise ValueError('Missing candles or incorrect timeframe')
    if 'available_at' in frame:
        frame['available_at'] = utc_times(frame.available_at)
    bars = []
    for row in frame.itertuples(index=False):
        closed = getattr(row, 'is_closed', True)
        if not pd.api.types.is_bool(closed) or not closed:
            raise ValueError('Unfinished/missing closed flag')
        available = getattr(row, 'available_at', row.timestamp+interval)
        if pd.isna(available) or available < row.timestamp+interval:
            raise ValueError('Availability precedes close or is missing')
        volume = getattr(row, 'volume', None)
        if volume is not None:
            volume = _number('volume', volume)
            if volume < 0:
                raise ValueError('Negative volume')
        bars.append(MarketBar(symbol, timeframe, row.timestamp, available,
                              OHLC(row.open, row.high, row.low, row.close), volume))
    if any(a.available_at > b.available_at for a, b in zip(bars, bars[1:])):
        raise ValueError('Nonmonotonic candle availability')
    return tuple(bars)


def unique_observations(items: Any) -> tuple:
    """Exact duplicates collapse; conflicting content under an ID is rejected."""
    unique = {}
    for item in items:
        key = item.observation_id
        if key in unique and unique[key] != item:
            raise ValueError('Conflicting duplicate observation')
        unique[key] = item
    return tuple(unique.values())
