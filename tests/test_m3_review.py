import sys
sys.dont_write_bytecode = True

from dataclasses import replace
from hashlib import sha256
import json
from pathlib import Path
import subprocess
import unittest
import uuid

import pandas as pd

from trading.patterns import OHLC, wick_reversals
from trading.pattern_context import assess_context
from trading.pattern_engine import ClosedCandle, PatternEngine, SetupTracker
from test_pattern_engine import START, DOWN, HARAMI, context, frame, harami_event


class M3ReviewTests(unittest.TestCase):
    def test_f1_neutral_wicks_have_no_directional_conflicts_or_bonuses(self):
        at = START+pd.Timedelta(hours=4)
        for candle in (OHLC(98, 100, 90, 99.5), OHLC(92, 100, 90, 90.5)):
            match = wick_reversals([candle])[0]
            for prior in ('UNKNOWN', 'NEUTRAL'):
                for direction in ('BULLISH', 'BEARISH', 'NEUTRAL'):
                    for regime in ('BULLISH', 'BEARISH', 'RANGE'):
                        with self.subTest(candle=candle, prior=prior, direction=direction, regime=regime):
                            assessment = assess_context(match, prior, 10, context(at, direction, regime=regime), at)
                            self.assertEqual((assessment.classification, assessment.status), ('NEUTRAL', 'INSUFFICIENT'))
                            self.assertEqual(assessment.conflicting, ())
                            self.assertEqual(set(assessment.missing), {'preceding_direction', 'location'})
                            self.assertEqual(assessment.supporting, ('volume',))
                            self.assertAlmostEqual(assessment.strength_score, .65*match.geometry_score+5)

    def test_f1_independent_abnormal_market_and_weak_volume_still_reject(self):
        at = START+pd.Timedelta(hours=4)
        match = wick_reversals([OHLC(98, 100, 90, 99.5)])[0]
        for kwargs, reason in ((dict(regime='ABNORMAL'), 'abnormal_market'),
                               (dict(relative_volume=.5), 'weak_volume')):
            assessment = assess_context(match, 'NEUTRAL', 10, context(at, **kwargs), at)
            self.assertEqual(assessment.status, 'REJECTED')
            self.assertEqual(assessment.conflicting, (reason,))

    def test_f2_each_stale_group_has_no_support_conflict_or_score(self):
        event, _ = harami_event()
        at = START+pd.Timedelta(hours=4)
        for direction in ('BULLISH', 'BEARISH'):
            for regime in ('RANGE', 'BEARISH', 'ABNORMAL'):
                for stale_direction, stale_regime in ((True, False), (False, True), (True, True)):
                    with self.subTest(direction=direction, regime=regime, stale=(stale_direction, stale_regime)):
                        snapshot = context(at, direction, regime=regime,
                                           direction_closed_at=START if stale_direction else at,
                                           regime_closed_at=START-pd.Timedelta(hours=4) if stale_regime else at)
                        unknown = replace(snapshot,
                                          higher_direction='UNKNOWN' if stale_direction else direction,
                                          regime='UNKNOWN' if stale_regime else regime,
                                          direction_closed_at=None if stale_direction else at,
                                          regime_closed_at=None if stale_regime else at)
                        actual = assess_context(event.geometry, 'BEARISH', 12, snapshot, at)
                        baseline = assess_context(event.geometry, 'BEARISH', 12, unknown, at)
                        self.assertEqual(actual.supporting, baseline.supporting)
                        self.assertEqual(actual.conflicting, baseline.conflicting)
                        self.assertAlmostEqual(actual.strength_score, baseline.strength_score)
                        if stale_direction:
                            self.assertIn('stale_1h_direction', actual.missing)
                        if stale_regime:
                            self.assertIn('stale_4h_regime', actual.missing)

    def test_f2_exact_freshness_boundary_and_one_nanosecond_after(self):
        event, _ = harami_event()
        at = START+pd.Timedelta(hours=4)
        snapshot = context(at, direction_closed_at=at-pd.Timedelta(hours=1), regime_closed_at=START)
        fresh = assess_context(event.geometry, 'BEARISH', 12, snapshot, at)
        stale = assess_context(event.geometry, 'BEARISH', 12, snapshot, at+pd.Timedelta(nanoseconds=1))
        self.assertEqual((fresh.status, stale.status), ('VALIDATED', 'INSUFFICIENT'))
        self.assertIn('higher_direction', fresh.supporting)
        self.assertIn('regime', fresh.supporting)
        self.assertNotIn('higher_direction', stale.supporting)
        self.assertNotIn('regime', stale.supporting)
        self.assertAlmostEqual(fresh.strength_score-stale.strength_score, 15)

    def test_f3_fresh_neutral_direction_and_range_are_compatibility_support(self):
        bullish, _ = harami_event()
        for direction, prior in (('BULLISH', 'BEARISH'), ('BEARISH', 'BULLISH')):
            # Mirror the geometry classification; context scoring has no price-direction dependence.
            match = replace(bullish.geometry, classification=direction)
            at = bullish.confirmed_at
            neutral = assess_context(match, prior, 12, context(at, 'NEUTRAL'), at)
            aligned = assess_context(match, prior, 12, context(at, direction, regime=direction), at)
            unknown = assess_context(match, prior, 12, context(at, 'UNKNOWN', regime='UNCERTAIN'), at)
            self.assertEqual((neutral.status, aligned.status, unknown.status),
                             ('VALIDATED', 'VALIDATED', 'INSUFFICIENT'))
            self.assertIn('higher_direction', neutral.supporting)
            self.assertIn('regime', neutral.supporting)
            self.assertAlmostEqual(neutral.strength_score, aligned.strength_score)
            self.assertAlmostEqual(neutral.strength_score-unknown.strength_score, 15)

    def test_f4_chronology_is_per_event_and_rejected_observation_is_atomic(self):
        btc, _ = harami_event()
        eth, _ = harami_event('ETH/USDT')
        tracker = SetupTracker()
        later = ClosedCandle(btc.confirmed_at+pd.Timedelta(minutes=10), OHLC(99, 102, 98, 101), btc.symbol)
        earlier = replace(later, opened_at=btc.confirmed_at, available_at=None)
        self.assertEqual(tracker.observe(btc, later, as_of=later.available_at).status, 'WAITING')
        with self.assertRaisesRegex(ValueError, 'chronological'):
            tracker.observe(btc, earlier, as_of=earlier.available_at)
        # A distinct event can be replayed at an earlier decision time.
        other = replace(earlier, symbol=eth.symbol)
        self.assertEqual(tracker.observe(eth, other, as_of=other.available_at).status, 'WAITING')
        self.assertEqual(tracker.observe(btc, later, as_of=later.available_at).status, 'DUPLICATE')
        next_bar = replace(later, opened_at=later.opened_at+pd.Timedelta(minutes=5), available_at=None)
        self.assertEqual(tracker.observe(btc, next_bar, as_of=next_bar.available_at).status, 'WAITING')

    def test_gap4_exporter_rejects_invalid_config_before_creating_output(self):
        root = Path('runs')/('m3-review-config-'+uuid.uuid4().hex)
        root.mkdir(parents=True)
        frame(DOWN+HARAMI).to_csv(root/'input.csv', index=False)
        for index, (config, error) in enumerate((([], 'ValueError'),
                                                ({'unexpected': {}}, 'ValueError'),
                                                ({'geometry': {'typo': .1}}, 'TypeError'),
                                                ({'context': {'typo': .1}}, 'TypeError'))):
            with self.subTest(config=config):
                config_path = root/f'config-{index}.json'
                config_path.write_text(json.dumps(config))
                out = root/f'out-{index}'
                result = subprocess.run([sys.executable, '-B', 'scan_patterns.py', '--csv', str(root/'input.csv'),
                                         '--symbol', 'BTC/USDT', '--config-json', str(config_path), '--out', str(out)],
                                        capture_output=True, text=True)
                self.assertNotEqual(result.returncode, 0)
                self.assertIn(error, result.stderr)
                self.assertFalse(out.exists())

    def test_review_scoring_revision_has_distinct_configuration_identity(self):
        from dataclasses import asdict
        engine = PatternEngine()
        legacy = json.dumps({'algorithm': 'M3-v1', 'geometry': asdict(engine.config),
                             'context': asdict(engine.context_config)}, sort_keys=True)
        self.assertNotEqual(engine.configuration_id, sha256(legacy.encode()).hexdigest()[:16])


if __name__ == '__main__':
    unittest.main()
