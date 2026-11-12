import sys
sys.dont_write_bytecode = True

from dataclasses import FrozenInstanceError, replace
import unittest

import numpy as np
import pandas as pd

from trading.patterns import OHLC
from trading.structure import StructureConfig, StructureEngine


START = pd.Timestamp('2025-01-01', tz='UTC')
STEP = pd.Timedelta(minutes=15)
BASE = [OHLC(100, 104, 96, 100), OHLC(101, 110, 97, 102), OHLC(102, 105, 98, 101)]
BREAK = OHLC(109, 113, 109, 112)
RETEST = OHLC(112, 114, 109, 113)
FAIL = OHLC(111, 112, 106, 108)


def frame(candles, interval=STEP):
    return pd.DataFrame([dict(timestamp=START+i*interval, open=c.open, high=c.high,
                              low=c.low, close=c.close) for i, c in enumerate(candles)])


def mirror(candles):
    return [OHLC(220-c.open, 220-c.low, 220-c.high, 220-c.close) for c in candles]


class StructureTests(unittest.TestCase):
    def setUp(self):
        self.engine = StructureEngine(StructureConfig(left_bars=1, right_bars=1))

    def scan(self, candles, **kwargs):
        return self.engine.scan(frame(candles), 'BTC/USDT', as_of=START+len(candles)*STEP, **kwargs)

    def test_pivot_time_and_confirmation_time_are_distinct(self):
        self.assertFalse(self.scan(BASE[:2]).swings)
        result = self.scan(BASE)
        swing, = result.swings
        self.assertEqual((swing.pivot_at, swing.confirmed_at, swing.price_confirmed_at),
                         (START+STEP, START+3*STEP, START+3*STEP))
        self.assertEqual((swing.kind, swing.price, swing.relation), ('HIGH', 110, 'FIRST'))
        self.assertEqual((result.zones[0].lower, result.zones[0].upper), (108.7, 110))
        self.assertFalse(result.events)
        with self.assertRaises(FrozenInstanceError):
            swing.price = 1

    def test_strict_ties_and_zero_range_do_not_invent_pivots(self):
        tied = BASE[:2]+[OHLC(102, 110, 98, 101)]
        self.assertFalse(self.scan(tied).swings)
        self.assertFalse(self.scan([OHLC(100, 100, 100, 100)]*8).swings)
        plateau = [OHLC(100, 102, 98, 100)]*5
        self.assertFalse(self.scan(plateau).swings)

    def test_configurable_right_window_requires_every_confirming_bar(self):
        engine = StructureEngine(StructureConfig(left_bars=1, right_bars=2))
        candles = BASE+[OHLC(101, 106, 99, 103)]
        self.assertFalse(engine.scan(frame(candles[:3]), 'BTC/USDT', as_of=START+3*STEP).swings)
        result = engine.scan(frame(candles), 'BTC/USDT', as_of=START+4*STEP)
        self.assertEqual(result.swings[0].confirmed_at, START+4*STEP)

    def test_breakout_retest_failure_both_directions(self):
        for transform, direction, opposite in ((lambda x: x, 'BULLISH', 'BEARISH'),
                                               (mirror, 'BEARISH', 'BULLISH')):
            result = self.scan(transform(BASE+[BREAK, RETEST, FAIL]))
            zone = result.zones[0]
            events = [e for e in result.events if e.zone_id == zone.zone_id]
            self.assertEqual([e.kind for e in events], ['BREAKOUT', 'RETEST', 'FAILED_BREAK'])
            self.assertEqual([e.direction for e in events], [direction, direction, opposite])
            self.assertEqual([e.available_at for e in events], [START+i*STEP for i in (4, 5, 6)])
            self.assertEqual(events[0].break_type, 'UNCLASSIFIED')
            self.assertEqual(zone.state, 'FAILED')
            self.assertEqual(len({e.event_id for e in result.events}), len(result.events))

    def test_breakout_bar_cannot_retest_itself(self):
        result = self.scan(BASE+[BREAK])
        self.assertEqual([e.kind for e in result.events], ['BREAKOUT'])
        self.assertEqual(result.zones[0].state, 'BROKEN')

    def test_wick_touch_and_equal_close_are_not_breakouts(self):
        for transform in (lambda x: x, mirror):
            for candle in (OHLC(109, 110, 108, 109), OHLC(109, 112, 108, 110)):
                self.assertFalse(self.scan(transform(BASE+[candle])).events)

    def test_sweep_is_structural_and_emitted_once_per_zone(self):
        sweep = OHLC(108, 112, 106, 108)
        for transform, direction in ((lambda x: x, 'BEARISH'), (mirror, 'BULLISH')):
            result = self.scan(transform(BASE+[sweep, sweep, BREAK]))
            events = [e for e in result.events if e.zone_id == result.zones[0].zone_id]
            self.assertEqual([e.kind for e in events], ['SWEEP', 'BREAKOUT'])
            self.assertEqual(events[0].direction, direction)

    def test_failed_break_takes_precedence_over_retest_and_is_terminal(self):
        result = self.scan(BASE+[BREAK, FAIL, RETEST, BREAK])
        events = [e.kind for e in result.events if e.zone_id == result.zones[0].zone_id]
        self.assertEqual(events, ['BREAKOUT', 'FAILED_BREAK'])
        self.assertEqual(result.zones[0].state, 'FAILED')

    def test_deep_wick_reclaim_is_ambiguous_not_a_retest(self):
        deep = OHLC(112, 114, 107, 113)
        result = self.scan(BASE+[BREAK, deep])
        self.assertEqual([e.kind for e in result.events if e.zone_id == result.zones[0].zone_id], ['BREAKOUT'])
        self.assertEqual(result.zones[0].state, 'BROKEN')

    def test_retest_final_close_is_inclusive_and_zone_expiry_is_exclusive(self):
        engine = StructureEngine(replace(self.engine.config, retest_bars=1))
        result = engine.scan(frame(BASE+[BREAK, RETEST]), 'BTC/USDT', as_of=START+5*STEP)
        self.assertEqual(result.zones[0].state, 'RETESTED')
        self.assertTrue(any(e.kind == 'RETEST' for e in result.events))
        late = BASE+[BREAK, OHLC(112, 115, 111, 114), RETEST]
        result = engine.scan(frame(late), 'BTC/USDT', as_of=START+6*STEP)
        self.assertEqual(result.zones[0].state, 'EXPIRED')
        self.assertFalse(any(e.kind == 'RETEST' for e in result.events))
        engine = StructureEngine(replace(self.engine.config, zone_lifetime_bars=1))
        result = engine.scan(frame(BASE+[BREAK]), 'BTC/USDT', as_of=START+4*STEP)
        self.assertEqual(result.zones[0].state, 'EXPIRED')
        self.assertFalse(result.events)
        raw = frame(BASE)
        raw['available_at'] = raw.timestamp+STEP
        raw.loc[2, 'available_at'] += 5*STEP
        result = engine.scan(raw, 'BTC/USDT', as_of=START+8*STEP)
        self.assertEqual(result.swings[0].confirmed_at, START+8*STEP)
        self.assertEqual(result.zones[0].expires_at, START+4*STEP)
        self.assertEqual(result.zones[0].state, 'EXPIRED')

    def test_idle_clock_expires_zones_and_distance_evidence(self):
        result = self.scan(BASE)
        distances = result.level_distances(108, 10)
        self.assertIsNone(distances['support_distance_atr'])
        self.assertAlmostEqual(distances['resistance_distance_atr'], .07)
        result = self.engine.scan(frame(BASE), 'BTC/USDT', as_of=START+200*STEP)
        self.assertEqual(result.zones[0].state, 'EXPIRED')
        self.assertEqual(result.level_distances(108, 10),
                         dict(support_distance_atr=None, resistance_distance_atr=None))

    def test_retested_zones_reverse_role_for_context(self):
        result = self.scan(BASE+[BREAK, RETEST])
        self.assertAlmostEqual(result.level_distances(113, 10)['support_distance_atr'], .3)
        broken = self.scan(BASE+[BREAK])
        self.assertIsNone(broken.level_distances(113, 10)['support_distance_atr'])
        support = self.scan(mirror(BASE))
        self.assertAlmostEqual(support.level_distances(112, 10)['support_distance_atr'], .07)

    def test_delayed_confirmation_cannot_use_already_open_candle(self):
        raw = frame(BASE+[BREAK, RETEST])
        raw['available_at'] = raw.timestamp+STEP
        raw.loc[2:3, 'available_at'] = START+4*STEP
        at = START+5*STEP
        result = self.engine.scan(raw, 'BTC/USDT', as_of=at)
        events = [e for e in result.events if e.zone_id == result.zones[0].zone_id]
        self.assertEqual([e.kind for e in events], ['BREAKOUT'])
        self.assertEqual(events[0].candle_at, START+4*STEP)

    def test_delayed_breakout_cannot_use_pre_receipt_retest(self):
        raw = frame(BASE+[BREAK, RETEST, OHLC(113, 115, 112, 114)])
        raw['available_at'] = raw.timestamp+STEP
        raw.loc[3:4, 'available_at'] = START+5*STEP
        result = self.engine.scan(raw, 'BTC/USDT', as_of=START+6*STEP)
        events = [e.kind for e in result.events if e.zone_id == result.zones[0].zone_id]
        self.assertEqual(events, ['BREAKOUT'])

    def test_sequences_and_bos_choch(self):
        # Rising peaks and troughs, followed by breaks on either side.
        prices = [100, 110, 102, 115, 105, 112]
        candles = [OHLC(p, p+1, p-1, p) for p in prices]
        result = self.scan(candles)
        self.assertEqual([s.relation for s in result.swings], ['FIRST', 'FIRST', 'HH', 'HL'])
        self.assertEqual(result.trend, 'BULLISH')
        self.assertEqual(result.events[-1].kind, 'STRUCTURE_CHANGE')
        for last, expected in ((OHLC(115, 119, 114, 118), 'BOS'),
                               (OHLC(103, 104, 99, 100), 'CHOCH')):
            result = self.scan(candles+[last])
            events = [e for e in result.events if e.kind == 'BREAKOUT' and e.candle_at == START+6*STEP]
            self.assertTrue(events)
            self.assertTrue(all(e.break_type == expected for e in events))
        bearish = self.scan(mirror(candles))
        self.assertEqual([s.relation for s in bearish.swings], ['FIRST', 'FIRST', 'LL', 'LH'])
        self.assertEqual(bearish.trend, 'BEARISH')

    def test_equal_swing_levels_are_neutral_sequence(self):
        candles = [OHLC(p, p+1, p-1, p) for p in (100, 110, 102, 110, 102, 108)]
        result = self.scan(candles)
        self.assertEqual([s.relation for s in result.swings], ['FIRST', 'FIRST', 'EH', 'EL'])
        self.assertEqual(result.trend, 'RANGE')

    def test_outside_pivot_can_confirm_both_sides_without_directional_vote(self):
        result = self.scan([OHLC(100, 104, 96, 100), OHLC(100, 110, 90, 101),
                            OHLC(100, 105, 95, 100)])
        self.assertEqual([s.kind for s in result.swings], ['HIGH', 'LOW'])
        self.assertEqual(result.trend, 'UNKNOWN')
        self.assertEqual(len(result.zones), 2)
        self.assertFalse(result.events)

    def test_gap_break_requires_close_and_does_not_fabricate_retest(self):
        result = self.scan(BASE+[OHLC(115, 118, 114, 116), OHLC(117, 119, 115, 118)])
        events = [e.kind for e in result.events if e.zone_id == result.zones[0].zone_id]
        self.assertEqual(events, ['BREAKOUT'])
        self.assertEqual(result.zones[0].state, 'BROKEN')

    def test_delayed_receipt_does_not_extend_retest_deadline(self):
        engine = StructureEngine(replace(self.engine.config, retest_bars=1))
        raw = frame(BASE+[BREAK, RETEST])
        raw['available_at'] = raw.timestamp+STEP
        raw.loc[3:4, 'available_at'] = START+6*STEP
        result = engine.scan(raw, 'BTC/USDT', as_of=START+6*STEP)
        self.assertEqual(result.zones[0].retest_expires_at, START+5*STEP)
        self.assertEqual(result.zones[0].state, 'EXPIRED')
        self.assertFalse(any(e.kind == 'RETEST' for e in result.events))

    def test_prefix_invariance_future_mutation_and_determinism(self):
        rng = np.random.default_rng(17)
        centers = 100+np.cumsum(rng.normal(0, 2, 80))
        candles = [OHLC(p, p+2, p-2, p+1) for p in centers]
        raw = frame(candles)
        full = self.engine.scan(raw, 'BTC/USDT', as_of=START+80*STEP)
        for n in (3, 10, 25, 50, 79):
            at = START+n*STEP
            prefix = self.engine.scan(raw.iloc[:n], 'BTC/USDT', as_of=at)
            historical = self.engine.scan(raw, 'BTC/USDT', as_of=at)
            self.assertEqual(prefix, historical)
            self.assertEqual(prefix.swings, tuple(s for s in full.swings if s.confirmed_at <= at))
            self.assertEqual(prefix.events, tuple(e for e in full.events if e.available_at <= at))
            changed = frame(candles[:n]+[OHLC(200, 210, 190, 205)]*(80-n))
            self.assertEqual(prefix, self.engine.scan(changed, 'BTC/USDT', as_of=at))
        self.assertEqual(full, self.engine.scan(raw, 'BTC/USDT', as_of=START+80*STEP))

    def test_symbols_timeframes_and_configuration_have_distinct_ids(self):
        for timeframe, delta in (('5m', pd.Timedelta(minutes=5)), ('15m', STEP),
                                 ('1h', pd.Timedelta(hours=1)), ('4h', pd.Timedelta(hours=4))):
            result = self.engine.scan(frame(BASE, delta), 'ETH/USDT', timeframe, START+3*delta)
            self.assertEqual(result.swings[0].confirmed_at, START+3*delta)
            self.assertEqual((result.symbol, result.timeframe), ('ETH/USDT', timeframe))
        btc = self.scan(BASE)
        eth = self.engine.scan(frame(BASE), 'ETH/USDT', as_of=START+3*STEP)
        self.assertNotEqual(btc.swings[0].swing_id, eth.swings[0].swing_id)
        other = StructureEngine(replace(self.engine.config, retest_bars=3))
        self.assertNotEqual(self.engine.configuration_id, other.configuration_id)

    def test_input_validation_and_availability_cutoff(self):
        raw = frame(BASE)
        self.assertFalse(self.engine.scan(raw, 'BTC/USDT', as_of=START+2*STEP).swings)
        for field, value in (('left_bars', 0), ('right_bars', True), ('retest_bars', 1.5),
                             ('zone_lifetime_bars', -1), ('zone_range_fraction', float('nan')),
                             ('zone_range_fraction', 0), ('zone_range_fraction', 2)):
            with self.assertRaises(ValueError):
                replace(self.engine.config, **{field: value})
        bad_inputs = [raw.iloc[[0, 2]], pd.concat([raw, raw.iloc[:1]])]
        for column, value in (('high', 1), ('close', float('nan')), ('is_closed', False),
                              ('symbol', 'ETH/USDT')):
            bad = raw.copy()
            bad[column] = value
            bad_inputs.append(bad)
        bad = raw.copy()
        bad['available_at'] = [START+STEP, START+6*STEP, START+3*STEP]
        bad_inputs.append(bad)
        for bad in bad_inputs:
            with self.assertRaises(ValueError):
                self.engine.scan(bad, 'BTC/USDT')
        with self.assertRaises(ValueError):
            self.engine.scan(raw, 'X/USDT')
        with self.assertRaises(ValueError):
            self.engine.scan(raw, 'BTC/USDT', '1d')
        for price, atr in ((0, 1), (1, 0), (True, 1), (1, float('inf'))):
            with self.assertRaises(ValueError):
                self.scan(BASE).level_distances(price, atr)
        empty = self.engine.scan(raw.iloc[:0], 'BTC/USDT')
        self.assertEqual(empty.trend, 'UNKNOWN')
        self.assertIsNone(empty.last_available_at)


if __name__ == '__main__':
    unittest.main()
