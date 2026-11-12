import sys
sys.dont_write_bytecode = True

from dataclasses import FrozenInstanceError, replace
import json
import unittest

import pandas as pd

from trading.derivatives import DerivativesEngine
from trading.liquidation import HeatmapEngine
from trading.signals import (CONTRADICTIONS, GROUPS, ContradictionRule, DerivativeRule,
                             GroupCap, SignalConfig, SignalTracker, evaluate_signal)
from trading.pattern_engine import ClosedCandle
from trading.patterns import OHLC
from trading.signal_context import assemble_context
from test_signal_context import CONFIG as CONTEXT_CONFIG, START, inputs, change_regime
from test_structure import mirror
from test_m6_data import SOURCE, metric, raw, snapshot


def policy(block=True, derivative_rules=(), ttl=None, require_direction=False):
    # Synthetic explicit policy; these caps are not fitted production weights.
    return SignalConfig('fixture-v1', tuple(GroupCap(name, cap) for name, cap in
                        zip(GROUPS, (2., 3., 5., 7.))),
                        tuple(ContradictionRule(reason, 'BLOCK' if block else 'RECORD_ONLY') for reason in CONTRADICTIONS),
                        derivative_rules, require_direction, ttl)


def with_derivatives(fields, rows):
    result = DerivativesEngine().analyze(rows, fields['pattern'].symbol, '5m', as_of=fields['as_of'])
    fields['m6'] = replace(fields['m6'], derivatives=(result,))
    return fields


class SignalEvaluationTests(unittest.TestCase):
    def test_two_aligned_macro_sources_share_one_configured_group_cap(self):
        for bearish in (False, True):
            for cap in (2.5, 5.):
                with self.subTest(bearish=bearish, cap=cap):
                    cfg = policy()
                    cfg = replace(cfg, group_caps=tuple(replace(g, cap=cap) if g.group == 'macro' else g
                                                        for g in cfg.group_caps))
                    result = evaluate_signal(assemble_context(**inputs(bearish=bearish)), cfg)
                    macro = next(g for g in result.groups if g.group == 'macro')
                    self.assertEqual(len(macro.supporting_ids), 2)
                    self.assertEqual((macro.support, macro.conflict, macro.contribution), (1., 0., cap))
                    self.assertNotEqual(macro.contribution, 2*cap)

    def test_actual_m3_confirmation_required_both_assets_and_directions(self):
        for symbol in ('BTC/USDT', 'ETH/USDT'):
            for bearish in (False, True):
                result = evaluate_signal(assemble_context(**inputs(symbol, bearish)), policy())
                self.assertTrue(result.qualification_passed)
                self.assertEqual(result.m3_decision.status, 'CONFIRMED')
                self.assertEqual(result.decision, 'SHORT' if bearish else 'LONG')
                self.assertIsNone(result.confidence)
                self.assertEqual(result.confidence_kind, 'UNCALIBRATED')
                self.assertLessEqual(abs(result.score), result.score_bound)

    def test_score_uses_geometry_not_m3_context_strength(self):
        fields = inputs()
        result = evaluate_signal(assemble_context(**fields), policy())
        pattern = next(g for g in result.groups if g.group == 'pattern')
        self.assertAlmostEqual(pattern.contribution, 2*fields['pattern'].geometry_score/100)
        self.assertNotAlmostEqual(pattern.contribution, 2*fields['pattern'].strength_score/100)

    def test_unknown_and_neutral_macro_are_not_directional_votes(self):
        fields = inputs()
        change_regime(fields, '1h', classification='RANGE')
        change_regime(fields, '4h', classification='RANGE')
        neutral = evaluate_signal(assemble_context(**fields), policy())
        group = next(g for g in neutral.groups if g.group == 'macro')
        self.assertEqual((group.support, group.conflict, group.contribution), (0, 0, 0))
        self.assertEqual(len(group.neutral_ids), 2)
        self.assertTrue(neutral.qualification_passed)  # Existing M3-v2 permits compatible known neutral context.
        fields['m6'] = replace(fields['m6'], regime=tuple(r for r in fields['m6'].regime if r.timeframe == '15m'))
        missing = evaluate_signal(assemble_context(**fields), policy())
        group = next(g for g in missing.groups if g.group == 'macro')
        self.assertIsNone(group.contribution)
        self.assertFalse(group.conflicting_ids)
        self.assertEqual(missing.m3_decision.status, 'WAITING')
        self.assertFalse(missing.qualification_passed)

    def test_missing_optional_derivatives_neither_blocks_nor_redistributes(self):
        fields = inputs()
        absent = evaluate_signal(assemble_context(**fields), policy())
        mapped = (DerivativeRule('metric', 'funding_rate', 'UNUSUALLY_POSITIVE', 'BULLISH'),)
        fresh = with_derivatives(inputs(), [metric('funding_rate', .001, fields['as_of'])])
        present = evaluate_signal(assemble_context(**fresh), policy(derivative_rules=mapped))
        self.assertTrue(absent.qualification_passed)
        self.assertTrue(present.qualification_passed)
        self.assertAlmostEqual(present.score-absent.score, 7)
        self.assertEqual([(g.group, g.contribution) for g in absent.groups[:-1]],
                         [(g.group, g.contribution) for g in present.groups[:-1]])
        self.assertIn('derivatives', absent.coverage.unavailable_groups)

    def test_structure_contradiction_policy_is_explicit_and_configurable(self):
        fields = inputs()
        fields['structure'] = replace(fields['structure'], trend='BEARISH')
        context = assemble_context(**fields)
        blocked = evaluate_signal(context, policy())
        advisory = evaluate_signal(context, policy(block=False))
        self.assertEqual(blocked.m3_decision.status, 'CONFIRMED')
        self.assertFalse(blocked.qualification_passed)
        self.assertTrue(advisory.qualification_passed)
        self.assertEqual(blocked.score, advisory.score)
        self.assertIn('STRUCTURE_TREND_OPPOSES_PATTERN', blocked.contradictions)
        self.assertIn('BLOCKED_STRUCTURE_TREND_OPPOSES_PATTERN', blocked.blocking_reasons)

    def test_record_only_cannot_override_existing_m3_context_gate(self):
        fields = inputs()
        change_regime(fields, '1h', classification='BEARISH')
        result = evaluate_signal(assemble_context(**fields), policy(block=False))
        self.assertEqual(result.m3_decision.status, 'WAITING')
        self.assertFalse(result.qualification_passed)
        self.assertIn('DIRECTION_1H_OPPOSES_PATTERN', result.contradictions)

    def test_structure_direction_requirement_does_not_invent_a_vote(self):
        fields = inputs()
        fields['structure'] = replace(fields['structure'], trend='RANGE')
        optional = evaluate_signal(assemble_context(**fields), policy(require_direction=False))
        required = evaluate_signal(assemble_context(**fields), policy(require_direction=True))
        self.assertTrue(optional.qualification_passed)
        self.assertFalse(required.qualification_passed)
        self.assertIn('M5_DIRECTIONAL_STRUCTURE_REQUIRED', required.blocking_reasons)
        self.assertEqual(optional.score, required.score)

    def test_unmapped_and_stale_derivatives_are_not_negative(self):
        fields = inputs()
        unmapped = with_derivatives(fields, [metric('funding_rate', -.001, fields['as_of'])])
        result = evaluate_signal(assemble_context(**unmapped), policy())
        derivative = result.groups[-1]
        self.assertEqual((derivative.support, derivative.conflict, derivative.contribution), (0, 0, 0))
        self.assertTrue(result.qualification_passed)
        stale_fields = inputs()
        old = stale_fields['as_of']-pd.Timedelta(hours=9)
        with_derivatives(stale_fields, [metric('funding_rate', -.001, old)])
        stale = evaluate_signal(assemble_context(**stale_fields), policy())
        self.assertIsNone(stale.groups[-1].contribution)
        self.assertTrue(stale.qualification_passed)

    def test_multiple_derivatives_feeds_do_not_multiply_group_cap(self):
        fields = inputs()
        rows = [metric('funding_rate', .001, fields['as_of'], source=replace(SOURCE, venue=name)) for name in ('a', 'b')]
        with_derivatives(fields, rows)
        rule = (DerivativeRule('metric', 'funding_rate', 'UNUSUALLY_POSITIVE', 'BULLISH'),)
        result = evaluate_signal(assemble_context(**fields), policy(derivative_rules=rule))
        self.assertEqual(result.groups[-1].contribution, 7)
        self.assertEqual(len(result.groups[-1].supporting_ids), 2)

    def test_derivatives_contradiction_cannot_create_candidate(self):
        fields = inputs()
        with_derivatives(fields, [metric('funding_rate', .001, fields['as_of'])])
        rules = (DerivativeRule('metric', 'funding_rate', 'UNUSUALLY_POSITIVE', 'BEARISH'),)
        result = evaluate_signal(assemble_context(**fields), policy(derivative_rules=rules))
        self.assertFalse(result.qualification_passed)
        self.assertIn('DERIVATIVES_OPPOSE_PATTERN', result.contradictions)

    def test_heatmaps_and_volatility_remain_explanatory(self):
        fields = inputs()
        baseline = evaluate_signal(assemble_context(**fields), policy())
        c = fields['candle']
        prices = raw([(c.ohlc.open, c.ohlc.high, c.ohlc.low, c.ohlc.close, 10)], start=c.opened_at)
        heatmap = HeatmapEngine().analyze([snapshot(c.opened_at)], prices, 'BTC/USDT', '5m', as_of=fields['as_of'])
        fields['m6'] = replace(fields['m6'], heatmaps=(heatmap,))
        change_regime(fields, '4h', volatility_state='HIGH', volatility_change='EXPANSION')
        result = evaluate_signal(assemble_context(**fields), policy())
        self.assertEqual((result.score, result.qualification_passed), (baseline.score, baseline.qualification_passed))
        self.assertIn('HEATMAPS_EXPLANATORY_ONLY', result.reason_codes)
        self.assertEqual(len(result.context.m6.heatmaps), 1)

    def test_policy_is_required_canonical_and_bounded_without_score_threshold(self):
        cfg = policy()
        reverse = replace(cfg, group_caps=tuple(reversed(cfg.group_caps)), contradiction_rules=tuple(reversed(cfg.contradiction_rules)))
        self.assertEqual(cfg, reverse)
        zero = replace(cfg, group_caps=tuple(GroupCap(name, 0) for name in GROUPS))
        result = evaluate_signal(assemble_context(**inputs()), zero)
        self.assertEqual(result.score, 0)
        self.assertTrue(result.qualification_passed)
        for kwargs in (dict(group_caps=cfg.group_caps[:-1]), dict(contradiction_rules=()),
                       dict(signal_ttl_seconds=0), dict(schema_version='M7-v2')):
            with self.assertRaises(ValueError):
                replace(cfg, **kwargs)

    def test_repeated_evaluation_is_deterministic_and_json_safe(self):
        context = assemble_context(**inputs())
        first = evaluate_signal(context, policy())
        self.assertEqual(first, evaluate_signal(context, policy()))
        encoded = json.dumps(first.to_dict(), sort_keys=True, allow_nan=False)
        self.assertEqual(encoded, json.dumps(first.to_dict(), sort_keys=True, allow_nan=False))


class SignalLifecycleTests(unittest.TestCase):
    def tracker(self, cfg=None):
        return SignalTracker(cfg or policy(), CONTEXT_CONFIG, mode='BACKTEST')

    def context(self, minutes=80, **kwargs):
        return assemble_context(**inputs(as_of=START+pd.Timedelta(minutes=minutes), **kwargs))

    def test_timely_monitoring_control_records_invalidation_before_receipt_deadline(self):
        for bearish in (False, True):
            for delay in (0, 299):
                with self.subTest(bearish=bearish, delay=delay):
                    tracker = self.tracker()
                    record = tracker.observe(self.context(bearish=bearish))
                    price = OHLC(101, 102, 93, 100)
                    price = mirror([price])[0] if bearish else price
                    candle = ClosedCandle(record.available_at, price, record.symbol)
                    at = candle.available_at+pd.Timedelta(seconds=delay)
                    event = tracker.observe_signal(record.signal_id, candle, as_of=at)
                    self.assertEqual((event.kind, event.available_at), ('INVALIDATED', at))
                    self.assertEqual(event.price_closed_at, candle.closed_at)

    def test_late_monitoring_raises_without_consuming_candle_or_chronology(self):
        for bearish in (False, True):
            for delay in (300, 301):
                for touched in (False, True):
                    with self.subTest(bearish=bearish, delay=delay, touched=touched):
                        tracker = self.tracker()
                        record = tracker.observe(self.context(bearish=bearish))
                        price = OHLC(101, 102, 93 if touched else 99, 100)
                        price = mirror([price])[0] if bearish else price
                        candle = ClosedCandle(record.available_at, price, record.symbol)
                        with self.assertRaisesRegex(ValueError, 'stale.*invalidation'):
                            tracker.observe_signal(record.signal_id, candle,
                                                   as_of=candle.available_at+pd.Timedelta(seconds=delay))
                        self.assertFalse(tracker.events)
                        self.assertEqual(tracker.history, (record,))
                        # A rejected call must not advance chronology or consume price evidence.
                        replay = tracker.observe_signal(record.signal_id, candle, as_of=candle.available_at)
                        if touched:
                            self.assertEqual(replay.kind, 'INVALIDATED')
                        else:
                            self.assertIsNone(replay)

    def test_mid_candle_qualification_excludes_already_open_candle_from_monitoring(self):
        for bearish in (False, True):
            with self.subTest(bearish=bearish):
                tracker = self.tracker()
                record = tracker.observe(self.context(81, bearish=bearish))
                self.assertEqual(record.lifecycle_state, 'QUALIFIED')
                crossing = OHLC(101, 102, 93, 100)
                crossing = mirror([crossing])[0] if bearish else crossing
                opening = record.available_at.floor('5min')
                overlap = ClosedCandle(opening, crossing, record.symbol)
                self.assertLess(overlap.opened_at, record.available_at)
                self.assertGreater(overlap.closed_at, record.available_at)
                self.assertIsNone(tracker.observe_signal(record.signal_id, overlap, as_of=overlap.available_at))
                self.assertFalse(tracker.events)
                independent = ClosedCandle(opening+pd.Timedelta(minutes=5), crossing, record.symbol)
                event = tracker.observe_signal(record.signal_id, independent, as_of=independent.available_at)
                self.assertEqual(event.kind, 'INVALIDATED')
                self.assertEqual(event.price_closed_at, independent.closed_at)
                self.assertEqual(tracker.history, (record,))

    def test_one_signal_and_stable_ids_on_duplicate_replay(self):
        context = self.context()
        first_tracker = self.tracker()
        first = first_tracker.observe(context)
        self.assertEqual(first.lifecycle_state, 'QUALIFIED')
        self.assertIsNotNone(first.signal_id)
        self.assertIs(first_tracker.observe(context), first)
        self.assertEqual(len(first_tracker.history), 1)
        self.assertFalse(first_tracker.events)
        self.assertEqual(self.tracker().observe(context), first)
        with self.assertRaises(FrozenInstanceError):
            first.decision = 'SHORT'

    def test_failed_qualification_consumes_first_confirmed_formation(self):
        fields = inputs()
        fields['structure'] = replace(fields['structure'], trend='BEARISH')
        tracker = self.tracker()
        rejected = tracker.observe(assemble_context(**fields))
        self.assertEqual(rejected.evaluation.m3_decision.status, 'CONFIRMED')
        self.assertEqual((rejected.lifecycle_state, rejected.decision, rejected.signal_id), ('REJECTED', 'NO_TRADE', None))
        later = self.context(85)
        self.assertTrue(evaluate_signal(later, policy()).qualification_passed)
        self.assertIs(tracker.observe(later), rejected)
        self.assertEqual(len(tracker.history), 1)

    def test_waiting_does_not_consume_and_next_bar_can_confirm(self):
        fields = inputs()
        fields['m6'] = replace(fields['m6'], regime=tuple(r for r in fields['m6'].regime if r.timeframe == '15m'))
        tracker = self.tracker()
        waiting = tracker.observe(assemble_context(**fields))
        qualified = tracker.observe(self.context(85))
        self.assertEqual((waiting.lifecycle_state, qualified.lifecycle_state), ('WAITING', 'QUALIFIED'))
        self.assertEqual(waiting.candidate_id, qualified.candidate_id)
        self.assertNotEqual(waiting.decision_id, qualified.decision_id)
        self.assertEqual(len(tracker.history), 2)

    def test_same_candle_cannot_retry_after_context_improves(self):
        fields = inputs()
        fields['m6'] = replace(fields['m6'], regime=tuple(r for r in fields['m6'].regime if r.timeframe == '15m'))
        tracker = self.tracker()
        waiting = tracker.observe(assemble_context(**fields))
        improved = self.context(81)
        self.assertTrue(evaluate_signal(improved, policy()).qualification_passed)
        self.assertIs(tracker.observe(improved), waiting)
        self.assertEqual(tracker.observe(self.context(85)).lifecycle_state, 'QUALIFIED')

    def test_invalidated_setup_is_terminal_before_any_qualification(self):
        fields = inputs()
        fields['candle'] = replace(fields['candle'], ohlc=OHLC(99, 105, 93, 102))
        tracker = self.tracker()
        record = tracker.observe(assemble_context(**fields))
        self.assertEqual(record.lifecycle_state, 'INVALIDATED')
        self.assertIsNone(record.signal_id)
        self.assertIs(tracker.observe(self.context(85)), record)

    def test_ttl_never_extends_upstream_expiry(self):
        context = self.context()
        for ttl in (None, 180, 100000):
            record = self.tracker(policy(ttl=ttl)).observe(context)
            self.assertLessEqual(record.expires_at, context.pattern.expires_at)
            expected = min(context.pattern.expires_at, context.as_of+pd.Timedelta(seconds=ttl)) if ttl else context.pattern.expires_at
            self.assertEqual(record.expires_at, expected)

    def test_linked_invalidation_both_sides_preserves_original_decision(self):
        for bearish in (False, True):
            tracker = self.tracker()
            record = tracker.observe(self.context(bearish=bearish))
            before = json.dumps(record.to_dict(), sort_keys=True)
            price = OHLC(101, 102, 93, 100)
            price = mirror([price])[0] if bearish else price
            candle = ClosedCandle(record.available_at, price, record.symbol)
            event = tracker.observe_signal(record.signal_id, candle, as_of=candle.available_at)
            self.assertEqual((event.kind, event.signal_id), ('INVALIDATED', record.signal_id))
            self.assertEqual(event.available_at, candle.available_at)
            self.assertIs(tracker.observe_signal(record.signal_id, candle, as_of=candle.available_at), event)
            self.assertEqual(len(tracker.events), 1)
            self.assertEqual(json.dumps(record.to_dict(), sort_keys=True), before)

    def test_expiry_links_at_observation_time_without_backdating(self):
        tracker = self.tracker(policy(ttl=180))
        record = tracker.observe(self.context())
        candle = ClosedCandle(record.available_at, OHLC(101, 102, 100, 101), record.symbol)
        event = tracker.observe_signal(record.signal_id, candle, as_of=candle.available_at)
        self.assertEqual(event.kind, 'EXPIRED')
        self.assertEqual(event.available_at, candle.available_at)
        self.assertGreater(event.available_at, record.expires_at)
        self.assertIsNone(event.price_closed_at)
        self.assertEqual(record.lifecycle_state, 'QUALIFIED')

    def test_future_wrong_asset_and_revised_signal_candles_are_rejected(self):
        tracker = self.tracker()
        record = tracker.observe(self.context())
        candle = ClosedCandle(record.available_at, OHLC(101, 103, 100, 102), record.symbol)
        for bad, at in ((candle, candle.available_at-pd.Timedelta(seconds=1)),
                        (replace(candle, symbol='ETH/USDT'), candle.available_at)):
            with self.assertRaises(ValueError):
                tracker.observe_signal(record.signal_id, bad, as_of=at)
        self.assertIsNone(tracker.observe_signal(record.signal_id, candle, as_of=candle.available_at))
        with self.assertRaises(ValueError):
            tracker.observe_signal(record.signal_id, replace(candle, ohlc=OHLC(101, 104, 100, 103)), as_of=candle.available_at)
        self.assertFalse(tracker.events)

    def test_revised_pattern_or_same_decision_input_cannot_rewrite_history(self):
        fields = inputs()
        tracker = self.tracker()
        original = tracker.observe(assemble_context(**fields))
        fields['pattern'] = replace(fields['pattern'], strength_score=1)
        with self.assertRaises(ValueError):
            tracker.observe(assemble_context(**fields))
        fields = inputs()
        change_regime(fields, '4h', volatility_state='HIGH')
        with self.assertRaises(ValueError):
            tracker.observe(assemble_context(**fields))
        self.assertEqual(tracker.history, (original,))

    def test_per_candidate_chronology_includes_duplicate_observations(self):
        tracker = self.tracker()
        context = self.context()
        record = tracker.observe(context)
        self.assertIs(tracker.observe(self.context(81)), record)
        with self.assertRaises(ValueError):
            tracker.observe(context)
        self.assertEqual(tracker.history, (record,))

    def test_missing_eligible_history_cannot_hide_earlier_confirmation(self):
        tracker = self.tracker()
        with self.assertRaisesRegex(ValueError, 'missing confirmation history'):
            tracker.observe(self.context(85))
        self.assertFalse(tracker.history)
        record = tracker.observe(self.context())
        late = ClosedCandle(record.available_at+pd.Timedelta(minutes=5), OHLC(101, 102, 100, 101), record.symbol)
        with self.assertRaisesRegex(ValueError, 'missing invalidation history'):
            tracker.observe_signal(record.signal_id, late, as_of=late.available_at)

    def test_prefix_future_mutation_and_deterministic_lifecycle_replay(self):
        fields = inputs()
        fields['m6'] = replace(fields['m6'], regime=tuple(r for r in fields['m6'].regime if r.timeframe == '15m'))
        prefix = [assemble_context(**fields), self.context(85)]
        a = self.tracker()
        for context in prefix:
            a.observe(context)
        for future_price in (OHLC(101, 102, 93, 100), OHLC(101, 110, 100, 109)):
            b = self.tracker()
            for context in prefix:
                b.observe(context)
            future = inputs(as_of=START+pd.Timedelta(minutes=90))
            future['candle'] = replace(future['candle'], ohlc=future_price)
            b.observe(assemble_context(**future))
            self.assertEqual(b.history, a.history)
            self.assertEqual(tuple(e for e in b.events if e.available_at <= prefix[-1].as_of), a.events)

    def test_policy_mode_identity_and_asset_separation(self):
        a, b = self.tracker(), self.tracker(policy(ttl=60))
        context = self.context()
        ra, rb = a.observe(context), b.observe(context)
        self.assertNotEqual(ra.candidate_id, rb.candidate_id)
        self.assertNotEqual(ra.signal_id, rb.signal_id)
        eth = assemble_context(**inputs('ETH/USDT'))
        self.assertNotEqual(a.observe(eth).candidate_id, ra.candidate_id)
        fields = inputs(); fields['mode'] = 'CURRENT'
        with self.assertRaises(ValueError):
            a.observe(assemble_context(**fields))
        fields = inputs(); fields['config'] = replace(CONTEXT_CONFIG, structure_max_age_seconds=1000)
        with self.assertRaises(ValueError):
            a.observe(assemble_context(**fields))

    def test_waiting_candle_revision_at_later_receipt_is_rejected(self):
        fields = inputs()
        fields['m6'] = replace(fields['m6'], regime=tuple(r for r in fields['m6'].regime if r.timeframe == '15m'))
        tracker = self.tracker()
        waiting = tracker.observe(assemble_context(**fields))
        changed = inputs(as_of=START+pd.Timedelta(minutes=81))
        changed['candle'] = replace(changed['candle'], ohlc=OHLC(99, 104, 98, 103))
        with self.assertRaisesRegex(ValueError, 'candidate candle'):
            tracker.observe(assemble_context(**changed))
        self.assertEqual(tracker.history, (waiting,))

    def test_delayed_pattern_receipt_requires_independent_next_bar(self):
        tracker = self.tracker()
        first = inputs()
        late_pattern = replace(first['pattern'], confirmed_at=START+pd.Timedelta(minutes=78))
        first['pattern'] = late_pattern
        waiting = tracker.observe(assemble_context(**first))
        self.assertEqual(waiting.lifecycle_state, 'WAITING')
        self.assertIn('confirmation_began_before_pattern_availability', waiting.evaluation.reason_codes)
        next_bar = inputs(as_of=START+pd.Timedelta(minutes=85))
        next_bar['pattern'] = late_pattern
        qualified = tracker.observe(assemble_context(**next_bar))
        self.assertEqual(qualified.lifecycle_state, 'QUALIFIED')
        self.assertEqual(qualified.available_at, next_bar['as_of'])
        self.assertGreaterEqual(next_bar['candle'].opened_at, late_pattern.confirmed_at)


if __name__ == '__main__':
    unittest.main()
