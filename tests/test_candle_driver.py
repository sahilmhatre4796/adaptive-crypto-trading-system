"""Network-free M12 Phase 1 driver regression checks."""
import sys
sys.dont_write_bytecode = True

from dataclasses import replace
from dataclasses import fields
import ast
from pathlib import Path
import unittest
from unittest.mock import patch

import pandas as pd

from trading.candle_driver import CandleDecisionDriver, CandleInput, WARMUP_5M_BARS
from trading.m6_data import Source
from trading.pattern_engine import PatternEngine
from trading.patterns import OHLC
from trading.regime import RegimeConfig
from trading.regime import RegimeEngine
from trading.signal_context import SignalContextConfig
from trading.signals import CONTRADICTIONS, GROUPS, ContradictionRule, GroupCap, SignalConfig
from trading.structure import StructureEngine


START = pd.Timestamp('2024-01-01T00:00:00Z')
FIVE = pd.Timedelta(minutes=5)


def driver(symbol='BTC/USDT'):
    policy = SignalConfig('m12-test-v1', tuple(GroupCap(name, 1.) for name in GROUPS),
                          tuple(ContradictionRule(name, 'BLOCK') for name in CONTRADICTIONS),
                          (), False, None)
    return CandleDecisionDriver(symbol, pattern_engine=PatternEngine(),
                                structure_engine=StructureEngine(), regime_config=RegimeConfig(),
                                context_config=SignalContextConfig(900, 'NEUTRAL', 'UNCERTAIN'),
                                signal_config=policy,
                                source=Source('fixture', 'offline', symbol.replace('/', ''), 'completed_ohlcv'))


def candle(index, *, symbol='BTC/USDT', delay=0, ohlc=None, source='fixture-5m'):
    opened = START + index * FIVE
    return CandleInput(symbol, opened, ohlc or OHLC(100, 101, 99, 100), 10.,
                       opened + FIVE + pd.Timedelta(seconds=delay), source)


class CandleDriverTests(unittest.TestCase):
    def test_exact_9600_bar_warmup_and_actual_m3_m5_m6_m7_handoff(self):
        subject = driver()
        for i in range(WARMUP_5M_BARS - 1):
            outcome = subject.observe(candle(i))
        self.assertFalse(outcome.warmup_ready)
        self.assertIsNone(subject.first_eligible_at)
        self.assertEqual(subject.signals.history, ())
        boundary = subject.observe(candle(WARMUP_5M_BARS - 1))
        self.assertTrue(boundary.warmup_ready)
        self.assertEqual(boundary.first_eligible_at, START + WARMUP_5M_BARS * FIVE)
        self.assertEqual(len(subject.completed_candles('4h')), 200)
        self.assertEqual(boundary.completed_timeframes, ('15m', '1h', '4h'))
        # Make a real M3 harami after warmup, using three 5m inputs per 15m
        # shape. M7 consumes it only on the following independent 5m bar.
        shapes = (OHLC(112, 113, 109, 110), OHLC(110, 111, 107, 108),
                  OHLC(108, 109, 105, 106), OHLC(105, 106, 94, 95),
                  OHLC(97, 100, 96, 99))
        observed = []
        for offset, shape in enumerate(shapes):
            for part in range(3):
                observed.append(subject.observe(candle(WARMUP_5M_BARS + offset * 3 + part,
                                                       ohlc=shape)))
        self.assertTrue(observed[-1].pattern_ids)
        next_outcome = subject.observe(candle(WARMUP_5M_BARS + 15,
                                              ohlc=OHLC(99, 102, 98, 101)))
        harami = [r for r in next_outcome.decisions if r.evaluation.context.pattern.family == 'HARAMI']
        self.assertTrue(harami)
        context = harami[0].evaluation.context
        self.assertEqual(context.structure.symbol, subject.symbol)
        self.assertEqual({r.timeframe for r in context.m6.regime}, {'15m', '1h', '4h'})
        self.assertTrue(all(r.source == subject.source and r.available_at <= next_outcome.candle.received_at
                            for r in context.m6.regime))
        self.assertEqual(context.confirmation_candle.opened_at,
                         START + (WARMUP_5M_BARS + 15) * FIVE)
        self.assertFalse(harami[0].qualification_passed)
        self.assertEqual(harami[0].lifecycle_state, 'WAITING')
        # R2: a second 5m observation in the same 15m bin reuses the exact
        # M5/M6 source versions instead of scanning the growing prefix again.
        with patch.object(StructureEngine, 'scan', side_effect=AssertionError('M5 rescanned')):
            with patch.object(RegimeEngine, 'analyze', side_effect=AssertionError('M6 rescanned')):
                second = subject.observe(candle(WARMUP_5M_BARS + 16))
        second_context = next(r.evaluation.context for r in second.decisions
                              if r.evaluation.context.pattern.family == 'HARAMI')
        fresh_structure = subject.structure_engine.scan(
            subject._frame(subject._timeframes['15m']), subject.symbol, '15m',
            as_of=second.candle.received_at)
        self.assertEqual(second_context.structure, fresh_structure)
        fresh_regimes = tuple(subject.regime_engine.analyze(
            subject._frame(subject._timeframes[tf]), subject.symbol, tf,
            as_of=second.candle.received_at, source=subject.source)
            for tf in ('15m', '1h', '4h'))
        self.assertEqual(second_context.m6.regime, fresh_regimes)
        restored = subject.replay()
        self.assertEqual(restored.outcomes, subject.outcomes)
        self.assertEqual(restored.signals.history, subject.signals.history)
        self.assertEqual(restored.signals.events, subject.signals.events)

    def test_complete_utc_groups_and_receipt_times(self):
        subject = driver('ETH/USDT')
        for i in range(48):
            result = subject.observe(candle(i, symbol='ETH/USDT', delay=2 if i == 2 else 0))
            if i == 1:
                self.assertEqual(subject.completed_candles('15m'), ())
            if i == 2:
                fifteen = subject.completed_candles('15m')[0]
                self.assertEqual(fifteen.opened_at, START)
                self.assertEqual(fifteen.available_at, START + 3 * FIVE + pd.Timedelta(seconds=2))
                self.assertEqual(fifteen.ohlc, OHLC(100, 101, 99, 100))
            if i == 11:
                self.assertEqual(result.completed_timeframes, ('15m', '1h'))
        self.assertEqual(result.completed_timeframes, ('15m', '1h', '4h'))
        self.assertEqual(len(subject.completed_candles('15m')), 16)
        self.assertEqual(len(subject.completed_candles('1h')), 4)
        self.assertEqual(subject.completed_candles('4h')[0].closed_at, START + pd.Timedelta(hours=4))
        self.assertFalse(result.warmup_ready)

    def test_distinct_ohlc_constituents_preserve_each_aggregate_field(self):
        subject = driver()
        shapes = (OHLC(100, 105, 99, 103), OHLC(103, 110, 102, 108),
                  OHLC(108, 109, 95, 96))
        for i in range(48):
            subject.observe(candle(i, ohlc=shapes[i] if i < 3 else None))
        self.assertEqual(subject.completed_candles('15m')[0].ohlc, OHLC(100, 110, 95, 96))
        self.assertEqual(subject.completed_candles('1h')[0].ohlc, OHLC(100, 110, 95, 100))
        self.assertEqual(subject.completed_candles('4h')[0].ohlc, OHLC(100, 110, 95, 100))
        self.assertEqual(subject._timeframes['15m'][0][1], 30.)
        self.assertEqual(subject._timeframes['1h'][0][1], 120.)
        self.assertEqual(subject._timeframes['4h'][0][1], 480.)

    def test_pre_warmup_pattern_never_becomes_a_candidate(self):
        subject = driver()
        shapes = (OHLC(112, 113, 109, 110), OHLC(110, 111, 107, 108),
                  OHLC(108, 109, 105, 106), OHLC(105, 106, 94, 95),
                  OHLC(97, 100, 96, 99))
        for i in range(WARMUP_5M_BARS):
            shape = shapes[(i - 9582) // 3] if 9582 <= i < 9597 else None
            subject.observe(candle(i, ohlc=shape))
        eligible = START + WARMUP_5M_BARS * FIVE
        self.assertTrue(all(p.confirmed_at >= eligible for p in subject._patterns.values()))
        self.assertTrue(all(p.confirmed_at >= eligible for p in subject._pending.values()))
        outcome = subject.observe(candle(WARMUP_5M_BARS))
        self.assertTrue(all(r.evaluation.context.pattern.confirmed_at >= eligible
                            for r in outcome.decisions))

    def test_duplicate_revision_gap_and_out_of_order_are_fail_closed(self):
        subject = driver()
        first, second = candle(0), candle(1)
        receipt = subject.observe(first)
        subject.observe(second)
        self.assertIs(subject.observe(first), receipt)
        with self.assertRaisesRegex(ValueError, 'Revised duplicate'):
            subject.observe(replace(first, volume=11.))
        with self.assertRaisesRegex(ValueError, 'Missing or out-of-order'):
            subject.observe(candle(3))
        with self.assertRaisesRegex(ValueError, 'Source identity changed'):
            subject.observe(candle(2, source='changed'))
        self.assertEqual(subject.inputs, (first, second))
        self.assertEqual(len(subject.outcomes), 2)

    def test_late_and_unfinished_receipts_are_rejected(self):
        subject = driver()
        with self.assertRaisesRegex(ValueError, 'Late 5m'):
            subject.observe(candle(0, delay=300))
        with self.assertRaisesRegex(ValueError, 'Completed'):
            subject.observe(replace(candle(0), received_at=START + pd.Timedelta(minutes=4)))
        subject.observe(candle(0, delay=299))
        subject.observe(candle(1))
        self.assertEqual(len(subject.inputs), 2)

    def test_replay_and_future_mutation_preserve_prefix(self):
        subject = driver()
        for i in range(48):
            subject.observe(candle(i))
        prefix = subject.outcomes
        restarted = subject.replay()
        self.assertEqual(restarted.outcomes, prefix)
        subject.observe(candle(48))
        restarted.observe(candle(48, ohlc=OHLC(100, 120, 80, 110)))
        self.assertEqual(subject.outcomes[:48], restarted.outcomes[:48])
        self.assertNotEqual(subject.outcomes[-1], restarted.outcomes[-1])

    def test_fresh_instances_are_deterministic_and_surface_is_analytical_only(self):
        left, right = driver(), driver()
        for i in range(48):
            item = candle(i)
            self.assertEqual(left.observe(item), right.observe(item))
        self.assertEqual(left.outcomes, right.outcomes)
        self.assertEqual(left.signals.history, right.signals.history)
        self.assertEqual(left.signals.events, right.signals.events)
        self.assertEqual(tuple(f.name for f in fields(left.outcomes[0])),
                         ('candle', 'warmup_ready', 'first_eligible_at',
                          'completed_timeframes', 'pattern_ids', 'decisions', 'terminal_events'))
        for name in ('orders', 'admissions', 'reservations', 'portfolio', 'risk'):
            self.assertFalse(hasattr(left, name))
        source = Path(__file__).resolve().parents[1] / 'trading' / 'candle_driver.py'
        imports = (node.module for node in ast.walk(ast.parse(source.read_text(encoding='utf-8')))
                   if isinstance(node, ast.ImportFrom))
        self.assertFalse(set(imports) & {'trading.risk', 'trading.portfolio',
                                         'trading.freqtrade_bridge', 'trading.ledger'})

    def test_failed_processing_quarantines_graph_and_replays_only_accepted_inputs(self):
        subject = driver()
        subject.observe(candle(0))
        subject.observe(candle(1))
        with patch.object(subject, '_complete_groups', side_effect=RuntimeError('injected')):
            with self.assertRaisesRegex(RuntimeError, 'injected'):
                subject.observe(candle(2))
        self.assertEqual(subject.inputs, (candle(0), candle(1)))
        with self.assertRaisesRegex(RuntimeError, 'quarantined'):
            subject.observe(candle(3))
        restored = subject.replay()
        self.assertEqual(restored.inputs, subject.inputs)
        restored.observe(candle(2))
        self.assertEqual(len(restored.outcomes), 3)


if __name__ == '__main__':
    unittest.main()
