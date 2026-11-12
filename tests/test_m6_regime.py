from dataclasses import replace
import unittest

import numpy as np
import pandas as pd

from trading.regime import RegimeConfig, RegimeEngine
from test_m6_data import START, STEP, raw

CONFIG = RegimeConfig(fast_ema=3, slow_ema=8, indicator_bars=3, band_bars=5,
                      slope_bars=2, reference_bars=5, persistence_bars=2)


def prices(values, spread=.2):
    return raw([(p, p+spread, p-spread, p, 10+i) for i, p in enumerate(values)])


class RegimeTests(unittest.TestCase):
    def analyze(self, frame, engine=None):
        return (engine or RegimeEngine(CONFIG)).analyze(frame, 'BTC/USDT', '5m', as_of=START+len(frame)*STEP)

    def test_bullish_and_bearish_trends_have_multiple_evidence_groups(self):
        for values, expected in ((np.linspace(100, 130, 60), 'BULLISH'),
                                 (np.linspace(130, 100, 60), 'BEARISH')):
            result = self.analyze(prices(values))
            self.assertEqual((result.status, result.classification), ('AVAILABLE', expected))
            self.assertEqual(result.confidence, 100)
            self.assertTrue({'adx', 'ema_slope_atr', 'atr_pct', 'band_width_pct', 'relative_volume'} <= {e.name for e in result.evidence})

    def test_sideways_and_completely_flat_markets(self):
        result = self.analyze(prices([100+.01*(-1)**i for i in range(60)]),
                              RegimeEngine(replace(CONFIG, indicator_bars=14)))
        self.assertEqual(result.classification, 'RANGE')
        flat = self.analyze(prices([100]*60, spread=0))
        self.assertEqual((flat.classification, flat.volatility_state), ('RANGE', 'LOW'))
        self.assertIsNone(next(e.value for e in flat.evidence if e.name == 'adx'))

    def test_high_low_volatility_require_atr_and_band_evidence(self):
        high = self.analyze(prices([100+10*(-1)**i for i in range(60)], spread=2))
        low = self.analyze(prices([100+.001*(-1)**i for i in range(60)], spread=.001))
        single = self.analyze(prices([100]*60, spread=20))
        self.assertEqual(high.volatility_state, 'HIGH')
        self.assertEqual(low.volatility_state, 'LOW')
        self.assertEqual(single.volatility_state, 'NORMAL')

    def test_volatility_expansion_and_contraction(self):
        expansion = self.analyze(prices([100+.02*(-1)**i for i in range(57)]+[100+8*(-1)**i for i in range(3)]))
        engine = RegimeEngine(replace(CONFIG, reference_bars=20))
        contraction = self.analyze(prices([100+8*(-1)**i for i in range(51)]+[100+.02*(-1)**i for i in range(9)]), engine)
        self.assertEqual(expansion.volatility_change, 'EXPANSION')
        self.assertEqual(contraction.volatility_change, 'CONTRACTION')

    def test_transition_and_insufficient_history(self):
        result = self.analyze(prices(np.linspace(100, 130, 60)), RegimeEngine(replace(CONFIG, min_slope_atr=100)))
        self.assertEqual((result.classification, result.confidence), ('TRANSITION', 0))
        self.assertEqual(self.analyze(prices([100]*5)).status, 'INSUFFICIENT')

    def test_persistence_waits_for_confirmed_sequence_without_backdating(self):
        initial = list(np.linspace(100, 130, 60))
        engine = RegimeEngine(replace(CONFIG, persistence_bars=3))
        once = self.analyze(prices(initial+[100]), engine)
        repeated = self.analyze(prices(initial+[100, 97.5, 95]), engine)
        self.assertEqual((once.classification, repeated.classification), ('TRANSITION', 'BEARISH'))
        self.assertEqual(repeated.available_at, START+63*STEP)

    def test_delayed_future_and_stale_candles(self):
        frame = prices(np.linspace(100, 130, 60))
        frame['available_at'] = frame.timestamp+STEP
        frame.loc[59, 'available_at'] += STEP
        engine = RegimeEngine(CONFIG)
        cutoff = START+60*STEP
        result = engine.analyze(frame, 'BTC/USDT', '5m', as_of=cutoff)
        self.assertEqual(result.candle_closed_at, START+59*STEP)
        later = engine.analyze(frame, 'BTC/USDT', '5m', as_of=cutoff+STEP)
        self.assertEqual(later.available_at, cutoff+STEP)
        stale = engine.analyze(frame, 'BTC/USDT', '5m', as_of=cutoff+3*STEP)
        self.assertEqual((stale.status, stale.classification), ('STALE', 'UNKNOWN'))

    def test_prefix_future_mutation_and_no_input_mutation(self):
        frame = prices(100+np.cumsum(np.random.default_rng(42).normal(0, .5, 80)))
        original = frame.copy(deep=True)
        engine = RegimeEngine(CONFIG)
        for n in (40, 60, 79):
            at = START+n*STEP
            prefix = engine.analyze(frame.iloc[:n], 'BTC/USDT', '5m', as_of=at)
            self.assertEqual(prefix, engine.analyze(frame, 'BTC/USDT', '5m', as_of=at))
            changed = frame.copy(); changed.loc[n:, ['open', 'high', 'low', 'close']] *= 2
            self.assertEqual(prefix, engine.analyze(changed, 'BTC/USDT', '5m', as_of=at))
        pd.testing.assert_frame_equal(frame, original)

    def test_missing_volume_has_no_relative_volume_evidence(self):
        result = self.analyze(prices(np.linspace(100, 130, 60)).drop(columns='volume'))
        self.assertNotIn('relative_volume', [e.name for e in result.evidence])

    def test_config_validation(self):
        for kwargs in (dict(fast_ema=50), dict(persistence_bars=True), dict(high_atr_pct=.1),
                       dict(contraction_ratio=2), dict(trend_adx=float('nan'))):
            with self.assertRaises(ValueError):
                replace(CONFIG, **kwargs)


if __name__ == '__main__':
    unittest.main()
