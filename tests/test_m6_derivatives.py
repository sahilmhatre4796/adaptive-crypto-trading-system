from dataclasses import replace
import unittest

from trading.derivatives import DerivativesConfig, DerivativesEngine, MetricPolicy
from test_m6_data import SOURCE, START, STEP, metric, stamp


class DerivativesTests(unittest.TestCase):
    def analyze(self, observations, **kwargs):
        fields = dict(as_of=START+STEP)
        fields.update(kwargs)
        return DerivativesEngine().analyze(observations, 'BTC/USDT', '5m', **fields)

    def own(self, result, name):
        return next(m for m in result.metrics if m.metric == name)

    def test_positive_negative_and_normal_funding(self):
        for value, expected in ((.001, 'UNUSUALLY_POSITIVE'), (-.001, 'UNUSUALLY_NEGATIVE'), (0, 'NORMAL')):
            result = self.analyze([metric('funding_rate', value)])
            self.assertEqual(self.own(result, 'funding_rate').condition, expected)
            self.assertEqual(result.status, 'PARTIAL')
        result = self.analyze([metric('funding_rate', .001, period_seconds=3600)])
        self.assertEqual(self.own(result, 'funding_rate').status, 'UNAVAILABLE')

    def test_all_four_price_open_interest_combinations(self):
        for oi, price, expected in ((110, 105, 'PRICE_RISING_OI_RISING'),
                                    (110, 95, 'PRICE_FALLING_OI_RISING'),
                                    (90, 105, 'PRICE_RISING_OI_FALLING'),
                                    (90, 95, 'PRICE_FALLING_OI_FALLING')):
            result = self.analyze([metric('open_interest', 100), metric('open_interest', oi, START+STEP),
                                   metric('price', 100), metric('price', price, START+STEP)])
            self.assertIn(expected, [e.value for e in result.evidence])
            self.assertTrue(any(e.name == 'sudden_open_interest_change' for e in result.evidence))

    def test_missing_stale_and_current_only_data(self):
        missing = self.analyze([])
        self.assertEqual(missing.status, 'UNAVAILABLE')
        self.assertTrue(all(m.observation is None and m.condition == 'UNKNOWN' for m in missing.metrics))
        stale = self.analyze([metric('open_interest', 100)], as_of=START+4*STEP)
        self.assertEqual(self.own(stale, 'open_interest').status, 'STALE')
        self.assertEqual(self.own(stale, 'open_interest').condition, 'UNKNOWN')
        current = metric('funding_rate', .001, time=stamp(historical=False))
        self.assertEqual(self.own(self.analyze([current]), 'funding_rate').status, 'UNAVAILABLE')
        self.assertEqual(self.own(self.analyze([current], mode='CURRENT'), 'funding_rate').status, 'AVAILABLE')

    def test_delayed_receipt_and_revised_values_never_leak(self):
        original = metric('open_interest', 100)
        revised = replace(original, value=200, time=stamp(receipt=START+2*STEP))
        future = metric('open_interest', 300, START+3*STEP)
        before = self.analyze([original])
        self.assertEqual(before, self.analyze([future, revised, original]))
        later = self.analyze([original, revised], as_of=START+2*STEP)
        self.assertEqual(self.own(later, 'open_interest').observation.value, 200)
        self.assertEqual(len([m for m in later.metrics if m.metric == 'open_interest']), 1)

    def test_no_cross_provider_unit_or_timestamp_change_calculations(self):
        base = [metric('open_interest', 100), metric('open_interest', 200, START+STEP)]
        for other in (metric('price', 105, START+STEP, source=replace(SOURCE, venue='other')),
                      metric('price', 105, START+2*STEP)):
            result = self.analyze(base+[metric('price', 100), other], as_of=START+2*STEP)
            self.assertFalse(any(e.name == 'price_open_interest' for e in result.evidence))
        units = self.analyze([metric('open_interest', 100), metric('open_interest', 200, START+STEP, unit='USD')])
        self.assertTrue(all(m.change_fraction is None for m in units.metrics if m.metric == 'open_interest'))

    def test_zero_and_old_baselines_do_not_create_infinite_changes(self):
        result = self.analyze([metric('open_interest', 0), metric('open_interest', 100, START+STEP)])
        self.assertIsNone(self.own(result, 'open_interest').change_fraction)
        result = self.analyze([metric('open_interest', 100), metric('open_interest', 200, START+20*STEP)], as_of=START+20*STEP)
        self.assertIsNone(self.own(result, 'open_interest').change_fraction)

    def test_ratio_basis_volume_and_liquidation_interpretations(self):
        observations = [metric('price', 100), metric('price', 105, START+STEP),
                        metric('volume', 100), metric('volume', 50, START+STEP),
                        metric('long_short_ratio', 2), metric('basis', -.01),
                        metric('liquidations_long', 100), metric('liquidations_long', 300, START+STEP)]
        result = self.analyze(observations)
        ratio = self.own(result, 'long_short_ratio')
        self.assertEqual(ratio.condition, 'POTENTIALLY_LONG_CROWDED')
        self.assertIn('provider_sample_not_market_wide_positioning', ratio.reasons)
        self.assertEqual(self.own(result, 'basis').condition, 'NEGATIVE_PREMIUM')
        self.assertEqual(self.own(result, 'liquidations_long').change_fraction, 2)
        self.assertIn('PRICE_RISING_VOLUME_FALLING', [e.value for e in result.evidence])
        short = self.analyze([metric('long_short_ratio', .5)])
        self.assertEqual(self.own(short, 'long_short_ratio').condition, 'POTENTIALLY_SHORT_CROWDED')

    def test_metric_policies_are_independent(self):
        config = DerivativesConfig((MetricPolicy('open_interest', 300, .2), MetricPolicy('funding_rate', 300, .001, 28800, False)))
        result = DerivativesEngine(config).analyze([metric('open_interest', 100), metric('open_interest', 110, START+STEP)],
                                                  'BTC/USDT', '5m', as_of=START+STEP)
        self.assertEqual(len(result.metrics), 1)
        self.assertEqual(result.metrics[0].condition, 'STABLE')
        self.assertEqual((result.status, result.confidence), ('AVAILABLE', 100))

    def test_duplicate_and_order_determinism(self):
        observations = [metric('open_interest', 100), metric('open_interest', 110, START+STEP)]
        self.assertEqual(self.analyze(observations), self.analyze(list(reversed(observations))+observations))
        with self.assertRaises(ValueError):
            self.analyze([observations[0], replace(observations[0], value=99)])

    def test_multiple_venue_series_have_deterministic_order(self):
        rows = [metric('open_interest', 100), metric('open_interest', 200, source=replace(SOURCE, venue='second')),
                metric('basis', .01), metric('basis', -.01, source=replace(SOURCE, venue='second'))]
        self.assertEqual(self.analyze(rows), self.analyze(reversed(rows)))


if __name__ == '__main__':
    unittest.main()
