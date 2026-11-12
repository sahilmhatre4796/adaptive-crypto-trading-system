import sys
sys.dont_write_bytecode = True

import unittest

from trading.pattern_context import ContextSnapshot, assess_context
from trading.patterns import OHLC, harami
from trading.structure import StructureConfig, StructureEngine
from test_structure import START, STEP, BASE, BREAK, RETEST, FAIL, frame, mirror


class StructureReviewTests(unittest.TestCase):
    def setUp(self):
        self.engine = StructureEngine(StructureConfig(left_bars=1, right_bars=1))

    def scan(self, candles, as_of=None):
        return self.engine.scan(frame(candles), 'BTC/USDT',
                                as_of=as_of if as_of is not None else START+len(candles)*STEP)

    def test_m5_02_context_snapshot_handoff_across_zone_states(self):
        bullish = harami([OHLC(105, 106, 94, 95), OHLC(97, 100, 96, 99)])[0]
        bearish = harami(mirror([OHLC(105, 106, 94, 95), OHLC(97, 100, 96, 99)]))[0]
        cases = (
            ('ACTIVE', BASE, 108, 'BEARISH', .07, 'VALIDATED'),
            ('ACTIVE', BASE, 109, 'BEARISH', 0., 'VALIDATED'),
            ('BROKEN', BASE+[BREAK], 112, 'BULLISH', None, 'INSUFFICIENT'),
            ('RETESTED', BASE+[BREAK, RETEST], 113, 'BULLISH', .3, 'VALIDATED'),
            ('FAILED', BASE+[BREAK, FAIL], 108, 'BULLISH', None, 'INSUFFICIENT'),
            ('EXPIRED', BASE, 108, 'BEARISH', None, 'INSUFFICIENT'),
        )
        for mirrored in (False, True):
            for state, candles, price, direction, distance, expected in cases:
                with self.subTest(state=state, mirrored=mirrored, price=price):
                    if mirrored:
                        candles, price = mirror(candles), 220-price
                        direction = 'BULLISH' if direction == 'BEARISH' else 'BEARISH'
                    as_of = START+100*STEP if state == 'EXPIRED' else START+len(candles)*STEP
                    result = self.scan(candles, as_of)
                    self.assertEqual(result.zones[0].state, state)
                    distances = result.level_distances(price, atr=10)
                    key = 'support_distance_atr' if direction == 'BULLISH' else 'resistance_distance_atr'
                    if distance is None:
                        self.assertIsNone(distances[key])
                    else:
                        self.assertAlmostEqual(distances[key], distance)
                    # This is the documented handoff with independently supplied fresh HTF evidence.
                    snapshot = ContextSnapshot(
                        symbol=result.symbol, available_at=result.as_of, atr=10, **distances,
                        higher_direction=direction, regime='RANGE',
                        direction_closed_at=result.as_of.floor('1h'),
                        regime_closed_at=result.as_of.floor('4h'))
                    match = bullish if direction == 'BULLISH' else bearish
                    prior = 'BEARISH' if direction == 'BULLISH' else 'BULLISH'
                    assessment = assess_context(match, prior, 12, snapshot, result.as_of)
                    self.assertEqual(assessment.status, expected)
                    self.assertEqual(assessment.conflicting, ())
                    if expected == 'VALIDATED':
                        self.assertIn('location', assessment.supporting)
                        self.assertNotIn('location', assessment.missing)
                    else:
                        self.assertIn('location', assessment.missing)
                        self.assertNotIn('location', assessment.supporting)
                    # Location alone still cannot manufacture higher-timeframe context.
                    location_only = ContextSnapshot(symbol=result.symbol, available_at=result.as_of,
                                                    atr=10, **distances)
                    self.assertEqual(assess_context(match, prior, 12, location_only, result.as_of).status,
                                     'INSUFFICIENT')

    def test_m5_01_break_uses_prior_trend_when_current_candle_confirms_pivot(self):
        candles = [OHLC(p, p+1, p-1, p) for p in (100, 110, 102, 115, 105, 112)]
        current = OHLC(109, 110, 99, 100)
        for transform, old_trend, direction, relation in (
                (lambda bars: bars, 'BULLISH', 'BEARISH', 'LH'),
                (mirror, 'BEARISH', 'BULLISH', 'HL')):
            with self.subTest(old_trend=old_trend):
                prefix = self.scan(transform(candles))
                self.assertEqual(prefix.trend, old_trend)
                result = self.scan(transform(candles+[current]))
                at = START+7*STEP
                confirmed = [s for s in result.swings if s.confirmed_at == at]
                self.assertEqual([s.relation for s in confirmed], [relation])
                self.assertEqual(confirmed[0].pivot_at, START+5*STEP)
                self.assertEqual(result.trend, 'RANGE')
                events = [e for e in result.events if e.available_at == at]
                breaks = [e for e in events if e.kind == 'BREAKOUT']
                self.assertTrue(breaks)
                self.assertTrue(all(e.direction == direction and e.break_type == 'CHOCH' for e in breaks))
                self.assertEqual(events[-1].kind, 'STRUCTURE_CHANGE')
                self.assertEqual(events[-1].direction, 'RANGE')
                self.assertTrue(all(result.events.index(e) < result.events.index(events[-1]) for e in breaks))
                historical = self.scan(transform(candles+[current]), prefix.as_of)
                self.assertEqual(historical, prefix)

    def test_m5_01_new_direction_on_same_close_does_not_relabel_break_as_bos(self):
        candles = [OHLC(p, p+1, p-1, p) for p in (100, 110, 102, 115, 105)]
        current = OHLC(115, 119, 114, 118)
        for transform, new_trend in ((lambda bars: bars, 'BULLISH'), (mirror, 'BEARISH')):
            with self.subTest(new_trend=new_trend):
                prefix = self.scan(transform(candles))
                self.assertEqual(prefix.trend, 'UNKNOWN')
                result = self.scan(transform(candles+[current]))
                self.assertEqual(result.trend, new_trend)
                breaks = [e for e in result.events if e.kind == 'BREAKOUT' and e.available_at == result.as_of]
                self.assertTrue(breaks)
                self.assertTrue(all(e.break_type == 'UNCLASSIFIED' for e in breaks))

    def test_m5_04_equal_wick_is_not_sweep_but_strict_crossing_is(self):
        touch = OHLC(108, 110, 106, 108)
        crossing = OHLC(108, 110.01, 106, 108)
        for transform in (lambda bars: bars, mirror):
            result = self.scan(transform(BASE+[touch]))
            zone = result.zones[0]
            self.assertEqual(zone.state, 'ACTIVE')
            self.assertFalse(zone.swept)
            self.assertFalse(any(e.zone_id == zone.zone_id for e in result.events))
            later = self.scan(transform(BASE+[touch, crossing]))
            events = [e for e in later.events if e.zone_id == zone.zone_id]
            self.assertEqual([e.kind for e in events], ['SWEEP'])
            self.assertEqual(events[0].candle_at, START+4*STEP)

    def test_m5_05_zero_range_pivots_create_deterministic_zero_width_zones(self):
        candles = [BASE[0], OHLC(110, 110, 110, 110), BASE[2]]
        retest = OHLC(112, 114, 110, 113)
        for transform in (lambda bars: bars, mirror):
            result = self.scan(transform(candles))
            self.assertEqual(len(result.swings), 1)
            zone, = result.zones
            self.assertEqual(zone.lower, zone.upper)
            self.assertEqual(zone.lower, 110)
            distances = result.level_distances(price=110, atr=10)
            relevant = 'resistance_distance_atr' if zone.role == 'RESISTANCE' else 'support_distance_atr'
            self.assertEqual(distances[relevant], 0)
            full_candles = transform(candles+[BREAK, retest, FAIL])
            full = self.scan(full_candles)
            events = [e for e in full.events if e.zone_id == zone.zone_id]
            self.assertEqual([e.kind for e in events], ['BREAKOUT', 'RETEST', 'FAILED_BREAK'])
            self.assertEqual(full.zones[0].state, 'FAILED')
            self.assertEqual(full, self.scan(full_candles))
            self.assertEqual(self.scan(full_candles, result.as_of), result)
            self.assertEqual(len({e.event_id for e in full.events}), len(full.events))


if __name__ == '__main__':
    unittest.main()
