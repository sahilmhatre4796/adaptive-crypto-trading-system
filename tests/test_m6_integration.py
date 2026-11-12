from dataclasses import replace
import json
import unittest

import numpy as np

from trading.derivatives import DerivativesEngine
from trading.liquidation import HeatmapEngine
from trading.m6 import M6Bundle
from trading.m6_adapters import coinglass_model1_snapshot
from trading.pattern_context import ContextSnapshot
from trading.pattern_engine import PatternEngine
from trading.regime import RegimeEngine
from trading.structure import StructureEngine
from test_m6_adapters import coinglass_payload
from test_m6_data import START, STEP, metric, snapshot
from test_m6_regime import CONFIG, prices


class M6IntegrationTests(unittest.TestCase):
    def components(self):
        frame = prices(np.linspace(100, 130, 60))
        at = START+60*STEP
        regime = RegimeEngine(CONFIG).analyze(frame, 'BTC/USDT', '5m', as_of=at)
        derivatives = DerivativesEngine().analyze([metric('funding_rate', .001, at)], 'BTC/USDT', '5m', as_of=at)
        heatmap = HeatmapEngine().analyze([snapshot(at-STEP)], frame, 'BTC/USDT', '5m', as_of=at)
        return frame, at, regime, derivatives, heatmap

    def test_m7_bundle_is_deterministic_json_and_preserves_provenance(self):
        _, at, regime, derivatives, heatmap = self.components()
        bundle = M6Bundle('BTC/USDT', at, (regime,), (derivatives,), (heatmap,))
        encoded = json.dumps(bundle.to_dict(), sort_keys=True, allow_nan=False)
        self.assertEqual(encoded, json.dumps(bundle.to_dict(), sort_keys=True, allow_nan=False))
        decoded = json.loads(encoded)
        self.assertEqual(decoded['schema_version'], 'M6-v1')
        self.assertEqual(decoded['heatmaps'][0]['snapshot']['source']['provider'], 'fixture')
        self.assertIn('time', decoded['derivatives'][0]['metrics'][0]['observation'])
        self.assertNotIn('trade_approved', decoded)

    def test_bundle_rejects_mismatched_symbol_cutoff_and_modes(self):
        _, at, regime, derivatives, heatmap = self.components()
        for bad in (replace(derivatives, symbol='ETH/USDT'), replace(derivatives, as_of=at+STEP)):
            with self.assertRaises(ValueError):
                M6Bundle('BTC/USDT', at, (regime,), (bad,), (heatmap,))
        with self.assertRaises(ValueError):
            M6Bundle('BTC/USDT', at, (regime,), (replace(derivatives, mode='CURRENT'),), (heatmap,))

    def test_existing_m3_m5_interfaces_remain_independently_usable(self):
        frame, at, regime, _, _ = self.components()
        before_m3 = PatternEngine().scan(frame, 'BTC/USDT', '5m', as_of=at)
        before_m5 = StructureEngine().scan(frame, 'BTC/USDT', '5m', as_of=at)
        self.components()
        self.assertEqual(before_m3, PatternEngine().scan(frame, 'BTC/USDT', '5m', as_of=at))
        self.assertEqual(before_m5, StructureEngine().scan(frame, 'BTC/USDT', '5m', as_of=at))
        # Explicit location bridge; no automatic substitution of a 5m regime for 4h context.
        levels = before_m5.level_distances(frame.close.iloc[-1], next(e.value for e in regime.evidence if e.name == 'atr'))
        context = ContextSnapshot('BTC/USDT', at, **levels)
        self.assertEqual((context.higher_direction, context.regime), ('UNKNOWN', 'UNKNOWN'))

    def test_current_provider_snapshot_is_unavailable_in_historical_bundle(self):
        frame, at, regime, derivatives, _ = self.components()
        s = coinglass_model1_snapshot(coinglass_payload(), symbol='BTC/USDT', venue='Binance', instrument='BTCUSDT', received_at=START+3*STEP)
        result = HeatmapEngine().analyze([s], frame, 'BTC/USDT', 'snapshot', as_of=at)
        bundle = M6Bundle('BTC/USDT', at, (regime,), (derivatives,), (result,))
        self.assertEqual(bundle.heatmaps[0].status, 'UNAVAILABLE')
        self.assertFalse(bundle.heatmaps[0].clusters)


if __name__ == '__main__':
    unittest.main()
