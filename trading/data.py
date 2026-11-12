import numpy as np
import pandas as pd

BAR = pd.Timedelta(minutes=5)
OBSERVATIONS = ('funding_rate', 'open_interest', 'orderbook_imbalance', 'spread_bps', 'news_sentiment')


def utc_times(values):
    """Numeric timestamps always mean epoch milliseconds."""
    unit = 'ms' if pd.api.types.is_numeric_dtype(values) else None
    return pd.to_datetime(values, unit=unit, utc=True, errors='raise')


def validate(raw, as_of=None):
    """One instrument, UTC opening timestamps, contiguous completed 5m OHLCV."""
    df = raw.copy().reset_index(drop=True)
    if df.columns.has_duplicates:
        raise ValueError('Duplicate column names')
    if 'timestamp' not in df:
        for name in ('time', 'datetime', 'date', 'open_time'):
            if name in df:
                df = df.rename(columns={name: 'timestamp'})
                break
    if 'timestamp' not in df or df.empty:
        raise ValueError('Nonempty timestamped OHLCV required')
    ts = utc_times(df.timestamp)
    df['timestamp'] = ts
    df = df.sort_values('timestamp').set_index('timestamp', drop=False)
    if df.index.hasnans or df.index.has_duplicates:
        raise ValueError('Invalid or duplicate timestamps')
    if (df.index != df.index.floor('5min')).any():
        raise ValueError('Candles must open on 5m boundaries')
    if len(df) > 1 and not (df.index.to_series().diff().iloc[1:] == BAR).all():
        raise ValueError('Missing candles or incorrect interval')
    closes = pd.Series(df.index + BAR, index=df.index)
    cutoff = pd.to_datetime(as_of, utc=True) if as_of is not None else pd.Timestamp.now(tz='UTC')
    if pd.isna(cutoff) or (closes > cutoff).any():
        raise ValueError('Unfinished candle relative to as_of')
    if 'is_closed' in df and not df.is_closed.map(
            lambda value: isinstance(value, (bool, np.bool_)) and bool(value)).all():
        raise ValueError('All input candles must be explicitly closed')
    if 'available_at' in df:
        df['available_at'] = utc_times(df.available_at)
        if df.available_at.isna().any() or (df.available_at > closes).any():
            raise ValueError('Candle unavailable at decision time; realign delayed data before use')
    required = {'open', 'high', 'low', 'close', 'volume'}
    if not required.issubset(df.columns):
        raise ValueError('Missing required OHLCV columns')
    for col in ('open', 'high', 'low', 'close', 'volume'):
        df[col] = pd.to_numeric(df[col], errors='raise')
        if not np.isfinite(df[col]).all():
            raise ValueError('Nonfinite OHLCV')
    if (df[['open','high','low','close']] <= 0).any().any() or (df.volume < 0).any():
        raise ValueError('Invalid price/volume')
    if (df.high < df[['open','close','low']].max(axis=1)).any() or (df.low > df[['open','close']].min(axis=1)).any():
        raise ValueError('Invalid OHLC bounds')
    for col in (*OBSERVATIONS, 'funding_event_rate'):
        if col in df:
            df[col] = pd.to_numeric(df[col], errors='raise')
            if np.isinf(df[col]).any():
                raise ValueError('Infinite optional observation')
            available = f'{col}_available_at'
            if available in df:
                df[available] = utc_times(df[available])
                deadline = df.timestamp if col == 'funding_event_rate' else closes
                invalid = df[available].isna() | (df[available] > deadline)
                if (df[col].notna() & invalid).any():
                    raise ValueError(f'{col} unavailable at its event time; realign before use')
    if 'spread_bps' in df and (df.spread_bps < 0).any():
        raise ValueError('Negative spread')
    if 'open_interest' in df and (df.open_interest < 0).any():
        raise ValueError('Negative OI')
    return df
