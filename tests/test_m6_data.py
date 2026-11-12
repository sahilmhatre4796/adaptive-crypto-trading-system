import sys
sys.dont_write_bytecode = True

from dataclasses import FrozenInstanceError, replace
import json
import unittest

import pandas as pd

from trading.derivatives import MetricObservation
from trading.liquidation import HeatmapSnapshot, LiquidationCluster
from trading.m6_data import (ObservationTime, Source, identity, jsonable, market_bars,
                             unique_observations)

START = pd.Timestamp('2025-01-01', tz='UTC')
STEP = pd.Timedelta(minutes=5)
SOURCE = Source('fixture', 'venue', 'BTCUSDT', 'documented_fixture')


def raw(candles, interval='5min', start=START):
    return pd.DataFrame([dict(timestamp=t, open=o, high=h, low=l, close=c, volume=v)
                         for t, (o, h, l, c, v) in zip(pd.date_range(start, periods=len(candles), freq=interval), candles)])


def stamp(at=START, *, receipt=None, historical=True):
    return ObservationTime(at, receipt if receipt is not None else at, historical_eligible=historical)


def metric(name, value, at=START, **kwargs):
    units = {'price': 'USDT', 'funding_rate': 'fraction', 'open_interest': 'base',
             'volume': 'base', 'long_short_ratio': 'ratio', 'basis': 'fraction',
             'liquidations_long': 'USDT', 'liquidations_short': 'USDT'}
    fields = dict(symbol='BTC/USDT', timeframe='5m', metric=name, value=value, unit=units[name],
                  source=SOURCE, time=stamp(at), period_seconds=28800 if name == 'funding_rate' else None)
    fields.update(kwargs)
    return MetricObservation(**fields)


def snapshot(at=START, clusters=None, **kwargs):
    clusters = clusters if clusters is not None else (
        LiquidationCluster('long', 95, 96, 'LONG', 100),
        LiquidationCluster('short', 104, 105, 'SHORT', 200))
    fields = dict(symbol='BTC/USDT', timeframe='5m', source=SOURCE, time=stamp(at),
                  reference_price=100, intensity_unit='fixture_score', supported_timeframes=('5m',), clusters=clusters)
    fields.update(kwargs)
    return HeatmapSnapshot(**fields)


class M6DataTests(unittest.TestCase):
    def test_time_order_and_missing_timestamps(self):
        for args in ((None, START), (START, None), (START+STEP, START),
                     (START, START+STEP, START-STEP), (START, START+STEP, START+2*STEP)):
            with self.assertRaises(ValueError):
                ObservationTime(*args)
        with self.assertRaises(ValueError):
            stamp(historical=1)
        t = ObservationTime('2025-01-01', '2025-01-01T00:05Z', '2025-01-01T00:02Z')
        self.assertEqual(t.available_at, START+STEP)
        self.assertEqual(t.status(START+STEP, 300, 'CURRENT'), 'AVAILABLE')
        self.assertEqual(t.status(START+2*STEP, 300, 'CURRENT'), 'STALE')
        self.assertEqual(t.status(START+STEP, 300, 'BACKTEST'), 'UNAVAILABLE')

    def test_deduplication_and_conflicting_identity(self):
        a = metric('open_interest', 100)
        self.assertEqual(unique_observations([a, a]), (a,))
        with self.assertRaises(ValueError):
            unique_observations([a, replace(a, value=110)])
        with self.assertRaises(ValueError):
            unique_observations([a, replace(a, time=replace(a.time, historical_eligible=False))])
        self.assertNotEqual(a.observation_id, replace(a, time=stamp(START, receipt=START+STEP)).observation_id)
        with self.assertRaises(FrozenInstanceError):
            a.value = 1

    def test_daily_and_numeric_epoch_candles(self):
        frame = raw([(100, 101, 99, 100, 0)]*3, '1D')
        frame['timestamp'] = frame.timestamp.map(lambda t: t.value//1_000_000)
        bars = market_bars(frame, 'ETH/USDT', '1d')
        self.assertEqual(bars[0].closed_at, START+pd.Timedelta(days=1))
        self.assertEqual(bars[0].volume, 0)
        self.assertEqual(bars[0].available_at, bars[0].closed_at)

    def test_missing_volume_is_explicit_not_zero(self):
        frame = raw([(100, 101, 99, 100, 10)]).drop(columns='volume')
        self.assertIsNone(market_bars(frame, 'BTC/USDT', '5m')[0].volume)

    def test_candle_malformed_missing_and_duplicate_rejection(self):
        good = raw([(100, 101, 99, 100, 10)]*4)
        bads = [good.drop(index=1), pd.concat([good, good.iloc[:1]])]
        for col, value in (('high', 1), ('volume', -1), ('volume', float('nan')),
                           ('is_closed', False), ('is_closed', None), ('symbol', 'ETH/USDT'),
                           ('available_at', START), ('close', float('inf'))):
            bad = good.copy(); bad[col] = value; bads.append(bad)
        bad = good.copy(); bad['available_at'] = [START+4*STEP, START+2*STEP, START+3*STEP, START+4*STEP]; bads.append(bad)
        for frame in bads:
            with self.assertRaises(ValueError):
                market_bars(frame, 'BTC/USDT', '5m')

    def test_sources_and_metrics_validate_units_and_values(self):
        with self.assertRaises(ValueError):
            Source('', 'venue', 'instrument', 'method')
        for kwargs in (dict(value=True), dict(value=float('nan')), dict(value=-1), dict(unit='unknown'),
                       dict(symbol='DOGE/USDT'), dict(period_seconds=0)):
            with self.assertRaises(ValueError):
                metric('open_interest', 100, **kwargs) if 'value' not in kwargs else replace(metric('open_interest', 100), **kwargs)

    def test_serialization_is_deterministic_and_finite(self):
        value = metric('price', 100)
        encoded = json.dumps(jsonable(value), sort_keys=True, allow_nan=False)
        self.assertIn('2025-01-01T00:00:00+00:00', encoded)
        self.assertEqual(identity(value), identity(value))

    def test_snapshot_rejects_wrong_sides_and_unsupported_intervals(self):
        for c in (LiquidationCluster('bad', 101, 102, 'LONG', 1),
                  LiquidationCluster('bad', 99, 100, 'SHORT', 1)):
            with self.assertRaises(ValueError):
                snapshot(clusters=(c,))
        with self.assertRaises(ValueError):
            snapshot(timeframe='1h')
        with self.assertRaises(ValueError):
            snapshot(clusters=(snapshot().clusters[0],)*2)


if __name__ == '__main__':
    unittest.main()
