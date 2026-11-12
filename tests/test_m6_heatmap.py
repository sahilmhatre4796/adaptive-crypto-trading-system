from dataclasses import replace
import unittest

from trading.liquidation import HeatmapConfig, HeatmapEngine, LiquidationCluster
from test_m6_data import SOURCE, START, STEP, raw, snapshot, stamp


class HeatmapTests(unittest.TestCase):
    def analyze(self, snapshots, candles=None, **kwargs):
        candles = candles if candles is not None else [(100, 100.2, 99.8, 100, 10)]
        fields = dict(as_of=START+len(candles)*STEP)
        fields.update(kwargs)
        return HeatmapEngine().analyze(snapshots, raw(candles), 'BTC/USDT', '5m', **fields)

    def test_long_short_distances_ranks_and_normalization(self):
        s = snapshot(clusters=snapshot().clusters+(LiquidationCluster('far', 120, 121, 'SHORT', 50),))
        result = self.analyze([s])
        own = {c.cluster.cluster_id: c for c in result.clusters}
        self.assertEqual(result.status, 'AVAILABLE')
        self.assertEqual((own['long'].location, own['short'].location), ('BELOW', 'ABOVE'))
        self.assertEqual((own['long'].distance_pct, own['short'].distance_pct, own['far'].distance_pct), (4, 4, 20))
        self.assertEqual((own['short'].relative_intensity, own['long'].relative_intensity, own['far'].relative_intensity), (1, .5, .25))
        self.assertTrue(own['short'].major)
        self.assertFalse(own['far'].major)
        self.assertEqual(own['short'].intensity_rank, 1)
        self.assertEqual(own['far'].proximity_rank, 3)
        self.assertAlmostEqual(result.long_concentration_share, 2/7)
        self.assertIsNone(result.confidence)

    def test_approaching_and_entering(self):
        s = snapshot(clusters=(LiquidationCluster('short', 100.5, 101, 'SHORT', 10),))
        approach = self.analyze([s], [(100, 100.3, 99.9, 100.2, 10)])
        self.assertEqual([e.kind for e in approach.events], ['APPROACHING'])
        entered = self.analyze([s], [(100, 100.8, 99.9, 100.6, 10)])
        self.assertEqual([e.kind for e in entered.events], ['ENTERING'])
        self.assertEqual(entered.clusters[0].distance_pct, 0)

    def test_through_then_no_reversal_requires_later_candle(self):
        s = snapshot()
        first = [(100, 107, 99, 106, 10)]
        result = self.analyze([s], first)
        self.assertIn('THROUGH', [e.kind for e in result.events])
        self.assertNotIn('THROUGH_NO_REVERSAL', [e.kind for e in result.events])
        later = self.analyze([s], first+[(106, 108, 105.5, 107, 10)])
        held = next(e for e in later.events if e.kind == 'THROUGH_NO_REVERSAL')
        self.assertEqual(held.available_at, START+2*STEP)
        self.assertNotIn('LIQUIDATED', [e.kind for e in later.events])

    def test_sweep_reversal_both_sides(self):
        for candle, side in (((100, 106, 99, 103, 10), 'short'), ((100, 101, 94, 97, 10), 'long')):
            result = self.analyze([snapshot()], [candle])
            sweep = next(e for e in result.events if e.kind == 'SWEEP_REVERSAL')
            self.assertEqual(sweep.cluster_id, side)
            self.assertEqual(sweep.available_at, START+STEP)
            self.assertFalse(any(e.kind == 'THROUGH' and e.cluster_id == side for e in result.events))

    def test_strengthening_weakening_and_missing_bins(self):
        original = snapshot()
        stronger = snapshot(START+STEP, clusters=(replace(original.clusters[0], intensity=150), original.clusters[1]))
        weaker = snapshot(START+2*STEP, clusters=(replace(original.clusters[0], intensity=50),))
        result = self.analyze([original, stronger, weaker], [(100, 101, 99, 100, 10)]*3)
        changes = [e for e in result.events if e.kind in ('STRENGTHENING', 'WEAKENING')]
        self.assertEqual([e.kind for e in changes], ['STRENGTHENING', 'WEAKENING'])
        self.assertEqual(changes[0].value, .5)
        self.assertTrue(all(e.cluster_id == 'long' for e in changes))

    def test_no_cross_provider_unit_or_timeframe_comparison(self):
        a = snapshot()
        for other in (snapshot(START+STEP, source=replace(SOURCE, provider='other')),
                      snapshot(START+STEP, intensity_unit='USD')):
            with self.assertRaises(ValueError):
                self.analyze([a, other])
        result = self.analyze([a, snapshot(START+STEP, timeframe='1h', supported_timeframes=('1h',))])
        self.assertEqual(result.snapshot, a)

    def test_missing_stale_and_empty_are_distinct(self):
        self.assertEqual(self.analyze([]).status, 'UNAVAILABLE')
        stale = self.analyze([snapshot()], [(100, 101, 99, 100, 10)]*4)
        self.assertEqual((stale.status, stale.clusters), ('STALE', ()))
        empty = self.analyze([snapshot(clusters=())])
        self.assertEqual((empty.status, empty.clusters), ('AVAILABLE', ()))
        self.assertIsNone(empty.long_concentration_share)

    def test_missing_intensity_quality_and_zero_baseline_are_not_fabricated(self):
        missing = snapshot(clusters=(LiquidationCluster('long', 95, 96, 'LONG', None),))
        result = self.analyze([missing])
        self.assertEqual(result.status, 'PARTIAL')
        self.assertIsNone(result.clusters[0].relative_intensity)
        self.assertIsNone(result.clusters[0].major)
        self.assertIsNone(result.long_concentration_share)
        zero = snapshot(clusters=(replace(missing.clusters[0], intensity=0),))
        later = snapshot(START+STEP, clusters=(replace(missing.clusters[0], intensity=100),))
        self.assertFalse(any(e.kind == 'STRENGTHENING' for e in self.analyze([zero, later]).events))

    def test_future_and_current_snapshots_never_enter_backtest(self):
        current = snapshot(time=stamp(historical=False))
        self.assertEqual(self.analyze([current]).status, 'UNAVAILABLE')
        self.assertEqual(self.analyze([current], mode='CURRENT').status, 'AVAILABLE')
        future = snapshot(START+10*STEP)
        self.assertEqual(self.analyze([future]).status, 'UNAVAILABLE')
        delayed = snapshot(time=stamp(receipt=START+2*STEP))
        self.assertEqual(self.analyze([delayed]).status, 'UNAVAILABLE')

    def test_snapshot_received_inside_bar_cannot_explain_earlier_price_path(self):
        s = snapshot(time=stamp(receipt=START+STEP/2))
        result = self.analyze([s], [(100, 106, 99, 103, 10)])
        self.assertEqual(result.status, 'AVAILABLE')
        self.assertFalse(result.events)

    def test_multi_timeframe_snapshots_are_independent(self):
        for timeframe in ('5m', '15m', '1h', '4h', '1d'):
            s = snapshot(timeframe=timeframe, supported_timeframes=(timeframe,))
            result = HeatmapEngine().analyze([s], raw([(100, 101, 99, 100, 10)]), 'BTC/USDT', timeframe,
                                              as_of=START+STEP)
            self.assertEqual((result.timeframe, result.status), (timeframe, 'AVAILABLE'))
        disabled = HeatmapEngine(HeatmapConfig(enabled_timeframes=('1h',))).analyze(
            [snapshot()], raw([(100, 101, 99, 100, 10)]), 'BTC/USDT', '5m', as_of=START+STEP)
        self.assertEqual(disabled.status, 'UNAVAILABLE')

    def test_event_prefix_invariance_with_late_old_revision(self):
        first = snapshot()
        second = snapshot(START+STEP, clusters=(replace(first.clusters[0], intensity=150), first.clusters[1]))
        late_old = replace(first, time=stamp(receipt=START+3*STEP), clusters=(replace(first.clusters[0], intensity=300), first.clusters[1]))
        candles = [(100, 101, 99, 100, 10)]*4
        earlier = self.analyze([first, second], candles[:2])
        later = self.analyze([late_old, second, first], candles)
        self.assertEqual(earlier.events, tuple(e for e in later.events if e.available_at <= START+2*STEP))
        self.assertEqual(self.analyze([first, second], candles[:2]),
                         self.analyze([first, second, late_old], candles[:2]))

    def test_duplicate_snapshots_and_events_are_deterministic(self):
        s = snapshot()
        candles = [(100, 106, 99, 103, 10)]*2
        a = self.analyze([s], candles)
        b = self.analyze([s, s], candles)
        self.assertEqual(a, b)
        self.assertEqual(len({e.event_id for e in a.events}), len(a.events))
        with self.assertRaises(ValueError):
            self.analyze([s, replace(s, clusters=())])

    def test_provider_quality_is_separate_from_intensity(self):
        s = snapshot(clusters=tuple(replace(c, quality=.6) for c in snapshot().clusters))
        result = self.analyze([s])
        self.assertEqual(result.confidence, 60)
        self.assertEqual(result.clusters[1].relative_intensity, 1)
        mixed = replace(s, clusters=(replace(s.clusters[0], quality=None), s.clusters[1]))
        self.assertIsNone(self.analyze([mixed]).confidence)

    def test_eth_and_changed_bounds_do_not_imply_intensity_change(self):
        s = snapshot(symbol='ETH/USDT', source=replace(SOURCE, instrument='ETHUSDT'))
        result = HeatmapEngine().analyze([s], raw([(100, 101, 99, 100, 10)]), 'ETH/USDT', '5m', as_of=START+STEP)
        self.assertEqual(result.status, 'AVAILABLE')
        original = snapshot()
        moved = snapshot(START+STEP, clusters=(replace(original.clusters[0], lower=94, intensity=200), original.clusters[1]))
        self.assertFalse(any(e.kind == 'STRENGTHENING' for e in self.analyze([original, moved]).events))

    def test_fresh_snapshot_needs_fresh_price_and_does_not_infer_gap_path(self):
        frame = raw([(100, 101, 99, 100, 10)])
        result = HeatmapEngine().analyze([snapshot(START+3*STEP)], frame, 'BTC/USDT', '5m', as_of=START+3*STEP)
        self.assertEqual(result.status, 'UNAVAILABLE')
        self.assertIn('missing_or_stale_current_price', result.reasons)
        gap = self.analyze([snapshot()], [(106, 108, 105.5, 107, 10)])
        self.assertFalse(any(e.kind in ('ENTERING', 'THROUGH') for e in gap.events))

    def test_stale_delayed_model_update_never_emits_strengthening(self):
        original = snapshot()
        delayed = snapshot(START+STEP, clusters=(replace(original.clusters[0], intensity=300),),
                           time=stamp(START+STEP, receipt=START+10*STEP))
        result = self.analyze([original, delayed], [(100, 101, 99, 100, 10)]*10)
        self.assertEqual(result.status, 'STALE')
        self.assertFalse(any(e.kind == 'STRENGTHENING' for e in result.events))


if __name__ == '__main__':
    unittest.main()
