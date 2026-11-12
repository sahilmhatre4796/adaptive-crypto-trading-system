import sys
sys.dont_write_bytecode = True

from copy import deepcopy
from dataclasses import replace
import json
import unittest

import numpy as np
import pandas as pd

from trading.derivatives import DerivativesEngine
from trading.liquidation import HeatmapEngine
from trading.m6 import M6Bundle
from trading.m6_data import INTERVALS
from trading.regime import RegimeEngine
import test_m6_integration as integration_fixtures
from test_m6_data import START, STEP, metric, raw, snapshot
from test_m6_regime import CONFIG, prices


class M6ReviewTests(unittest.TestCase):
    def bundle(self):
        _, at, regime, derivatives, heatmap = integration_fixtures.M6IntegrationTests().components()
        return M6Bundle('BTC/USDT', at, (regime,), (derivatives,), (heatmap,))

    def test_m6_01_json_round_trip_restores_all_models_and_numeric_identity(self):
        original = self.bundle()
        encoded = json.dumps(original.to_dict(), sort_keys=True, allow_nan=False)
        payload = json.loads(encoded)
        saved = deepcopy(payload)
        restored = M6Bundle.from_dict(payload)
        self.assertEqual(restored, original)
        self.assertEqual(json.dumps(restored.to_dict(), sort_keys=True, allow_nan=False), encoded)
        self.assertEqual(payload, saved)
        self.assertIsInstance(restored.as_of, pd.Timestamp)
        self.assertIsInstance(restored.regime[0].config, type(original.regime[0].config))
        self.assertIsInstance(restored.derivatives[0].metrics[0].observation.time.observed_at, pd.Timestamp)
        self.assertIsInstance(restored.heatmaps[0].snapshot.source, type(original.heatmaps[0].snapshot.source))
        self.assertIsInstance(restored.heatmaps[0].clusters, tuple)

    def test_m6_01_events_previous_observations_and_current_mode_round_trip(self):
        at = START+3*STEP
        frame = raw([(100, 107, 99, 106, 10), (106, 108, 105.5, 107, 10), (107, 108, 106, 107, 10)])
        for mode in ('BACKTEST', 'CURRENT'):
            regime = RegimeEngine().analyze(frame, 'BTC/USDT', '5m', as_of=at)
            observations = [metric('open_interest', 100), metric('open_interest', 110, START+STEP),
                            metric('price', 100), metric('price', 105, START+STEP), metric('funding_rate', -.001)]
            derivatives = DerivativesEngine().analyze(observations, 'BTC/USDT', '5m', as_of=at, mode=mode)
            first = snapshot()
            later = snapshot(START+2*STEP, clusters=(replace(first.clusters[0], intensity=150), first.clusters[1]))
            heatmap = HeatmapEngine().analyze([first, later], frame, 'BTC/USDT', '5m', as_of=at, mode=mode)
            original = M6Bundle('BTC/USDT', at, (regime,), (derivatives,), (heatmap,))
            restored = M6Bundle.from_dict(json.loads(json.dumps(original.to_dict(), allow_nan=False)))
            self.assertEqual(restored, original)
            self.assertTrue(restored.heatmaps[0].events)
            self.assertTrue(any(e.kind == 'STRENGTHENING' for e in restored.heatmaps[0].events))
            self.assertTrue(any(m.previous is not None for m in restored.derivatives[0].metrics))
            self.assertEqual(restored.regime[0].status, 'INSUFFICIENT')

    def test_m6_01_unavailable_stale_partial_and_transition_outputs_remain_legal(self):
        original = self.bundle()
        at = original.as_of
        frame = prices(np.linspace(100, 130, 60))
        regime = RegimeEngine(replace(CONFIG, min_slope_atr=100)).analyze(frame, 'BTC/USDT', '5m', as_of=at)
        derivatives = DerivativesEngine().analyze([], 'BTC/USDT', '5m', as_of=at)
        for heatmap in (
                HeatmapEngine().analyze([], frame, 'BTC/USDT', '5m', as_of=at),
                HeatmapEngine().analyze([snapshot()], frame, 'BTC/USDT', '5m', as_of=at),
                HeatmapEngine().analyze([snapshot(at-STEP, clusters=(replace(snapshot().clusters[0], intensity=None),))],
                                        frame, 'BTC/USDT', '5m', as_of=at)):
            bundle = M6Bundle('BTC/USDT', at, (regime,), (derivatives,), (heatmap,))
            self.assertEqual(M6Bundle.from_dict(bundle.to_dict()), bundle)
        self.assertEqual(regime.classification, 'TRANSITION')
        self.assertEqual(derivatives.status, 'UNAVAILABLE')

    def test_m6_01_rejects_schema_missing_unknown_fields_and_wrong_shapes(self):
        good = self.bundle().to_dict()
        bads = [None, [], 'json text', {}, {**good, 'schema_version': 'M6-v2'},
                {k: v for k, v in good.items() if k != 'schema_version'},
                {**good, 'extra': 1}, {**good, 'regime': {}}, {**good, 'as_of': 'NaT'}]
        for path in (('regime', 0, 'config'), ('derivatives', 0, 'metrics', 0, 'observation', 'source')):
            bad = deepcopy(good)
            node = bad
            for key in path:
                node = node[key]
            node['unexpected'] = 'field'
            bads.append(bad)
        for bad in bads:
            with self.subTest(payload=type(bad).__name__):
                with self.assertRaises(ValueError):
                    M6Bundle.from_dict(bad)

    def test_m6_01_rejects_nested_type_domain_and_scope_errors(self):
        good = self.bundle().to_dict()
        cases = (
            (('regime', 0, 'confidence'), True),
            (('regime', 0, 'confidence'), float('nan')),
            (('regime', 0, 'confidence'), 101),
            (('regime', 0, 'classification'), 'PROFITABLE'),
            (('regime', 0, 'timeframe'), 'snapshot'),
            (('regime', 0, 'config', 'fast_ema'), 1.5),
            (('regime', 0, 'config', 'slow_ema'), 1),
            (('derivatives', 0, 'mode'), 'LIVE'),
            (('derivatives', 0, 'metrics', 0, 'observation', 'unit'), 'USD'),
            (('derivatives', 0, 'metrics', 0, 'observation', 'value'), float('inf')),
            (('derivatives', 0, 'metrics', 0, 'observation', 'time', 'historical_eligible'), 'true'),
            (('heatmaps', 0, 'snapshot', 'clusters', 0, 'side'), 'SHORT'),
            (('heatmaps', 0, 'clusters', 0, 'relative_intensity'), 2),
            (('heatmaps', 0, 'clusters', 0, 'nearby'), 1),
            (('heatmaps', 0, 'symbol'), 'ETH/USDT'),
            (('heatmaps', 0, 'snapshot', 'time', 'received_at'), 'invalid date'),
        )
        for path, value in cases:
            with self.subTest(path=path, value=value):
                bad = deepcopy(good)
                node = bad
                for key in path[:-1]:
                    node = node[key]
                node[path[-1]] = value
                with self.assertRaises(ValueError):
                    M6Bundle.from_dict(bad)

    def test_m6_01_rejects_future_and_current_only_nested_data_in_backtest(self):
        good = self.bundle().to_dict()
        for field in ('heatmaps', 'derivatives'):
            for future in (False, True):
                bad = deepcopy(good)
                timing = (bad[field][0]['snapshot']['time'] if field == 'heatmaps' else
                          bad[field][0]['metrics'][0]['observation']['time'])
                if future:
                    timing['received_at'] = (pd.Timestamp(good['as_of'])+STEP).isoformat()
                else:
                    timing['historical_eligible'] = False
                with self.assertRaises(ValueError):
                    M6Bundle.from_dict(bad)
        bad = deepcopy(good)
        bad['regime'][0]['available_at'] = (pd.Timestamp(good['as_of'])+STEP).isoformat()
        with self.assertRaises(ValueError):
            M6Bundle.from_dict(bad)

    def test_m6_01_event_validation_and_empty_bundle_contract(self):
        empty = M6Bundle('BTC/USDT', START, (), (), ())
        self.assertEqual(M6Bundle.from_dict(empty.to_dict()), empty)
        result = HeatmapEngine().analyze([snapshot()], raw([(100, 106, 99, 103, 10)]),
                                          'BTC/USDT', '5m', as_of=START+STEP)
        original = M6Bundle('BTC/USDT', START+STEP, (), (), (result,))
        self.assertEqual(M6Bundle.from_dict(original.to_dict()), original)
        for field, value in (('kind', 'LIQUIDATED'),
                             ('available_at', (START+2*STEP).isoformat()),
                             ('price_closed_at', (START+2*STEP).isoformat())):
            bad = original.to_dict()
            bad['heatmaps'][0]['events'][0][field] = value
            with self.assertRaises(ValueError):
                M6Bundle.from_dict(bad)

    def test_m6_02_fresh_at_open_and_close_but_stale_at_receipt_has_no_interaction(self):
        s = snapshot()
        frame = raw([(100, 106, 99, 103, 10)], start=START+2*STEP)
        at_close = START+3*STEP
        received = START+4*STEP
        self.assertEqual(s.time.status(frame.timestamp.iloc[0], 900, 'BACKTEST'), 'AVAILABLE')
        self.assertEqual(s.time.status(at_close, 900, 'BACKTEST'), 'AVAILABLE')
        timely = HeatmapEngine().analyze([s], frame, 'BTC/USDT', '5m', as_of=at_close)
        self.assertTrue({'ENTERING', 'SWEEP_REVERSAL'} <= {e.kind for e in timely.events})
        frame['available_at'] = received
        self.assertEqual(s.time.status(received, 900, 'BACKTEST'), 'STALE')
        delayed = HeatmapEngine().analyze([s], frame, 'BTC/USDT', '5m', as_of=received)
        self.assertEqual(delayed.status, 'STALE')
        self.assertEqual(delayed.events, ())

    def test_m6_03_regime_smoke_all_candle_timeframes_and_reject_snapshot(self):
        for symbol in ('BTC/USDT', 'ETH/USDT'):
            for timeframe, interval in INTERVALS.items():
                with self.subTest(symbol=symbol, timeframe=timeframe):
                    frame = prices(np.linspace(100, 130, 60))
                    frame['timestamp'] = pd.date_range(START, periods=60, freq=interval)
                    at = START+60*interval
                    result = RegimeEngine(CONFIG).analyze(frame, symbol, timeframe, as_of=at)
                    self.assertEqual((result.status, result.classification), ('AVAILABLE', 'BULLISH'))
                    self.assertEqual((result.symbol, result.timeframe, result.available_at), (symbol, timeframe, at))
                    prefix = RegimeEngine(CONFIG).analyze(frame, symbol, timeframe, as_of=at-pd.Timedelta(nanoseconds=1))
                    self.assertEqual(prefix.candle_closed_at, at-interval)
        with self.assertRaises(ValueError):
            RegimeEngine(CONFIG).analyze(frame, 'BTC/USDT', 'snapshot', as_of=at)

    def test_m6_03_derivatives_smoke_all_supported_timeframes_including_snapshot(self):
        for symbol in ('BTC/USDT', 'ETH/USDT'):
            for timeframe in (*INTERVALS, 'snapshot'):
                with self.subTest(symbol=symbol, timeframe=timeframe):
                    row = metric('funding_rate', .001, symbol=symbol, timeframe=timeframe)
                    result = DerivativesEngine().analyze([row], symbol, timeframe, as_of=START)
                    funding = next(m for m in result.metrics if m.metric == 'funding_rate')
                    self.assertEqual((result.symbol, result.timeframe), (symbol, timeframe))
                    self.assertEqual((funding.status, funding.condition), ('AVAILABLE', 'UNUSUALLY_POSITIVE'))
                    self.assertEqual(funding.observation, row)
        with self.assertRaises(ValueError):
            DerivativesEngine().analyze([], 'BTC/USDT', '1m', as_of=START)


if __name__ == '__main__':
    unittest.main()
