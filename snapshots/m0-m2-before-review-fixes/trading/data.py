import numpy as np
import pandas as pd

BAR = pd.Timedelta(minutes=5)


def validate(raw):
    """One instrument, UTC opening timestamps, contiguous completed 5m OHLCV."""
    df = raw.copy().reset_index(drop=True)
    if 'timestamp' not in df:
        for name in ('time', 'datetime', 'date', 'open_time'):
            if name in df:
                df = df.rename(columns={name: 'timestamp'})
                break
    if 'timestamp' not in df or df.empty:
        raise ValueError('Nonempty timestamped OHLCV required')
    ts = df.timestamp
    if pd.api.types.is_numeric_dtype(ts):
        ts = pd.to_datetime(ts, unit='ms', utc=True, errors='raise')
    else:
        ts = pd.to_datetime(ts, utc=True, errors='raise')
    df['timestamp'] = ts
    df = df.sort_values('timestamp').set_index('timestamp', drop=False)
    if df.index.hasnans or df.index.has_duplicates:
        raise ValueError('Invalid or duplicate timestamps')
    if (df.index != df.index.floor('5min')).any():
        raise ValueError('Candles must open on 5m boundaries')
    if len(df) > 1 and not (df.index.to_series().diff().iloc[1:] == BAR).all():
        raise ValueError('Missing candles or incorrect interval')
    for col in ('open', 'high', 'low', 'close', 'volume'):
        df[col] = pd.to_numeric(df[col], errors='raise')
        if not np.isfinite(df[col]).all():
            raise ValueError('Nonfinite OHLCV')
    if (df[['open','high','low','close']] <= 0).any().any() or (df.volume < 0).any():
        raise ValueError('Invalid price/volume')
    if (df.high < df[['open','close','low']].max(axis=1)).any() or (df.low > df[['open','close']].min(axis=1)).any():
        raise ValueError('Invalid OHLC bounds')
    for col in ('funding_rate', 'open_interest', 'orderbook_imbalance', 'spread_bps', 'news_sentiment', 'funding_event_rate'):
        if col in df:
            df[col] = pd.to_numeric(df[col], errors='raise')
            if np.isinf(df[col]).any():
                raise ValueError('Infinite optional observation')
    if 'spread_bps' in df and (df.spread_bps < 0).any():
        raise ValueError('Negative spread')
    if 'open_interest' in df and (df.open_interest < 0).any():
        raise ValueError('Negative OI')
    return df
