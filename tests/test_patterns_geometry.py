import sys
sys.dont_write_bytecode = True

import unittest
import numpy as np
from trading.patterns import OHLC, PatternConfig, detect_geometry, harami, engulfing, stars, hikkake, wick_reversals, doji


def mirror(bars, pivot=200):
    return [OHLC(pivot-c.open, pivot-c.low, pivot-c.high, pivot-c.close) for c in bars]


class GeometryTests(unittest.TestCase):
    def test_ohlc_geometry(self):
        c = OHLC(98, 100, 90, 99)
        self.assertEqual(c.body, 1)
        self.assertEqual(c.upper_wick, 1)
        self.assertEqual(c.lower_wick, 8)
        self.assertEqual(c.body_ratio, .1)

    def test_invalid_ohlc_and_zero_range(self):
        for values in ((0, 1, 0, 1), (100, 99, 98, 100), (100, 101, 100.5, 101),
                       (100, np.inf, 90, 100), (np.nan, 110, 90, 100), (True, 2, 1, 2)):
            with self.subTest(values=values), self.assertRaises(ValueError): OHLC(*values)
        self.assertEqual(detect_geometry([OHLC(100, 100, 100, 100)]), ())

    def test_bullish_bearish_harami(self):
        bars = [OHLC(105, 106, 94, 95), OHLC(97, 100, 96, 99)]
        self.assertEqual(harami(bars)[0].classification, 'BULLISH')
        self.assertEqual(harami(mirror(bars))[0].classification, 'BEARISH')
        self.assertEqual(harami([bars[0], OHLC(94, 100, 93, 98)]), ())
        self.assertEqual(harami([bars[0], OHLC(96, 104, 95, 103)]), ())

    def test_engulfing_tolerance_and_relative_size(self):
        bars = [OHLC(103, 104, 99, 100), OHLC(99.9, 104, 99, 103.1)]
        self.assertEqual(engulfing(bars)[0].classification, 'BULLISH')
        self.assertEqual(engulfing(mirror(bars))[0].classification, 'BEARISH')
        self.assertEqual(engulfing([bars[0], OHLC(100.1, 103.5, 99, 103.2)], PatternConfig(body_tolerance=0)), ())
        tolerant = [bars[0], OHLC(100.03, 104, 99, 103.2)]
        self.assertTrue(engulfing(tolerant))
        self.assertFalse(engulfing(tolerant, PatternConfig(body_tolerance=0)))
        self.assertFalse(engulfing(bars, PatternConfig(engulfing_body_min=2)))

    def test_same_color_does_not_create_reversal_body_patterns(self):
        bars = [OHLC(100, 104, 99, 103), OHLC(99, 105, 98, 104)]
        self.assertEqual(engulfing(bars), ())
        self.assertEqual(harami(bars), ())

    def test_wick_geometry_is_neutral_until_context(self):
        lower = OHLC(98, 100, 90, 99.5)
        self.assertEqual(wick_reversals([lower])[0].name, 'lower_wick')
        self.assertEqual(wick_reversals([lower])[0].classification, 'NEUTRAL')
        self.assertEqual(wick_reversals(mirror([lower]))[0].name, 'upper_wick')
        self.assertFalse(wick_reversals([OHLC(95, 100, 90, 99)]))

    def test_doji_is_neutral_and_not_a_wick_reversal(self):
        c = OHLC(99, 100, 90, 99)
        self.assertEqual(doji([c])[0].classification, 'NEUTRAL')
        self.assertFalse(wick_reversals([c]))
        self.assertFalse(engulfing([OHLC(100, 105, 94, 95), c]))

    def test_stars_without_gaps(self):
        bars = [OHLC(105, 106, 94, 95), OHLC(95, 96, 94, 95.2), OHLC(95.2, 102, 95, 101)]
        self.assertEqual(stars(bars)[0].name, 'morning_star')
        self.assertEqual(stars(mirror(bars))[0].name, 'evening_star')
        self.assertFalse(stars([bars[0], bars[1], OHLC(95.2, 100, 95, 99)]))
        self.assertFalse(stars([bars[0], OHLC(96, 102, 95, 101), bars[2]]))

    def test_hikkake_requires_later_close_not_wick(self):
        bars = [OHLC(100, 105, 95, 101), OHLC(101, 104, 96, 100), OHLC(100, 103, 94, 95)]
        self.assertFalse(hikkake(bars))
        unconfirmed = bars + [OHLC(95, 104.5, 94.5, 103)]
        self.assertFalse(hikkake(unconfirmed))
        confirmed = unconfirmed + [OHLC(103, 105, 95, 104.5)]
        match = hikkake(confirmed)[0]
        self.assertEqual(match.classification, 'BULLISH')
        self.assertEqual(match.detected_offset, -2)
        self.assertEqual(match.bullish_invalidation, 94)
        self.assertEqual(hikkake(mirror(confirmed))[0].classification, 'BEARISH')
        self.assertFalse(hikkake(confirmed + [OHLC(104.5, 106, 96, 105)]))

    def test_hikkake_expiration_extension_and_ambiguous_bar(self):
        bars = [OHLC(100, 105, 95, 101), OHLC(101, 104, 96, 100), OHLC(100, 103, 94, 95)]
        wait = OHLC(96, 103, 95, 100)
        confirm = OHLC(100, 105, 95, 104.5)
        self.assertTrue(hikkake(bars + [wait, wait, confirm]))
        self.assertFalse(hikkake(bars + [wait, wait, wait, confirm]))
        self.assertFalse(hikkake(bars + [OHLC(95, 106, 93, 105)]))
        outside = bars[:2] + [OHLC(100, 106, 94, 101), confirm]
        self.assertFalse(hikkake(outside))

    def test_config_and_scale_invariance(self):
        for kwargs in ({'doji_body_max': .6}, {'hikkake_confirmation_bars': 0},
                       {'expiry_bars': 2.5}, {'wick_body_min': np.nan}, {'body_tolerance': 2}):
            with self.subTest(kwargs=kwargs), self.assertRaises(ValueError): PatternConfig(**kwargs)
        bars = [OHLC(105, 106, 94, 95), OHLC(97, 100, 96, 99)]
        scaled = [OHLC(c.open*500, c.high*500, c.low*500, c.close*500) for c in bars]
        self.assertEqual([m.name for m in detect_geometry(bars)], [m.name for m in detect_geometry(scaled)])
        self.assertEqual(harami(bars)[0].geometry_score, harami(scaled)[0].geometry_score)

    def test_body_and_wick_threshold_boundaries(self):
        c = OHLC(98, 100, 90, 99)
        self.assertTrue(doji([c]))  # Exactly 10% real body: neutral only.
        self.assertFalse(wick_reversals([c]))
        self.assertTrue(wick_reversals([c], PatternConfig(doji_body_max=.09)))
        self.assertFalse(wick_reversals([c], PatternConfig(doji_body_max=.09, wick_opposite_max=.099)))
        first = OHLC(105, 106, 94, 95)
        self.assertTrue(harami([first, OHLC(96, 103, 95, 102)]))  # Exactly 60% body.
        self.assertFalse(harami([first, OHLC(96, 103, 95, 102.01)]))
        tolerance_case = [first, OHLC(94.9, 99, 94, 97)]
        self.assertTrue(harami(tolerance_case))
        self.assertFalse(harami(tolerance_case, PatternConfig(body_tolerance=0)))

    def test_star_and_hikkake_confirmation_boundaries(self):
        first = [OHLC(105, 106, 94, 95), OHLC(95, 96, 94, 95.2)]
        self.assertTrue(stars(first+[OHLC(95.2, 101, 95, 100)]))
        self.assertFalse(stars(first+[OHLC(95.2, 101, 95, 99.99)]))
        h = [OHLC(100, 105, 95, 101), OHLC(101, 104, 96, 100), OHLC(100, 103, 94, 95)]
        self.assertFalse(hikkake(h+[OHLC(100, 105, 95, 104)]))  # Equality is not a break.
        self.assertTrue(hikkake(h+[OHLC(100, 105, 95, 104.01)]))
        equal_inside = [h[0], OHLC(101, 105, 96, 100), h[2], OHLC(100, 106, 95, 105.5)]
        self.assertFalse(hikkake(equal_inside))


if __name__ == '__main__': unittest.main()
