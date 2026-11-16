"""M13 seeded-history boundary and first eligible live M7 candle."""

from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch
from uuid import uuid4
import unittest

import pandas as pd

from trading.m13_runtime import M13PaperRuntime
from trading.m13_shadow import ShadowSession
from trading.m13_driver import CurrentCandleDriver
from trading.paper_journal import PaperJournalError
from trading.pattern_engine import PatternEngine
from trading.signal_context import assemble_context
from test_m13_runtime import actors, public_candle
from test_signal_context import inputs


SEED_COUNT = 9602  # First new live candle completes a UTC 15m group.


def seeded(symbol):
    subject = actors()[0][symbol]
    items = tuple(public_candle(i, symbol=symbol) for i in range(SEED_COUNT))
    subject.seed_many(items, retrieved_at=items[-1].candle.closed_at + pd.Timedelta(minutes=1))
    return subject


class LiveBoundaryTests(unittest.TestCase):
    def test_seed_window_patterns_are_excluded_and_live_pattern_starts_on_exact_bar(self):
        for symbol in ('BTC/USDT', 'ETH/USDT'):
            with self.subTest(symbol=symbol):
                subject = seeded(symbol)
                self.assertFalse(subject.signals.history)
                self.assertFalse(subject._pending)
                first = public_candle(SEED_COUNT, symbol=symbol, delay=30)
                base = inputs(symbol)['pattern']
                historical = replace(base, event_id='seed-window-' + symbol,
                    confirmed_at=first.opened_at,
                    expires_at=first.opened_at + pd.Timedelta(hours=1))
                live = replace(base, event_id='live-' + symbol,
                    confirmed_at=first.received_at,
                    expires_at=first.received_at + pd.Timedelta(hours=1))
                with patch.object(PatternEngine, 'scan', return_value=(historical, live)):
                    first_result = subject.observe(first)
                    self.assertEqual(first_result.pattern_ids, (live.event_id,))
                    self.assertEqual(set(subject._pending), {live.event_id})
                    self.assertEqual(subject._live_boundary, first.candle.closed_at)
                    second = public_candle(SEED_COUNT + 1, symbol=symbol, delay=30)
                    self.assertFalse(subject.observe(second).decisions)
                    third = public_candle(SEED_COUNT + 2, symbol=symbol, delay=30)
                    marker = SimpleNamespace(lifecycle_state='WAITING', signal_id=None)
                    with (patch('trading.m13_driver.assemble_context',
                                side_effect=lambda pattern, candle, *args, **kwargs:
                                    SimpleNamespace(pattern=pattern, confirmation_candle=candle)),
                          patch.object(subject.signals, 'observe', return_value=marker) as observe):
                        result = subject.observe(third)
                    self.assertEqual(result.decisions, (marker,))
                    self.assertEqual(observe.call_count, 1)
                    context = observe.call_args.args[0]
                    self.assertEqual(context.confirmation_candle.opened_at,
                                     live.confirmed_at.ceil('5min'))
                    self.assertFalse(subject.signals.history)  # No backdated M7 record.

    def test_skipped_live_candidate_still_raises_original_m7_guard(self):
        subject = seeded('BTC/USDT')
        first = public_candle(SEED_COUNT, delay=30)
        with patch.object(PatternEngine, 'scan', return_value=()):
            for index in range(SEED_COUNT, SEED_COUNT + 3):
                subject.observe(public_candle(index, delay=30))
        fields = inputs()
        fields['mode'] = 'CURRENT'
        base_context = assemble_context(**fields)
        pattern = replace(base_context.pattern, event_id='delayed-live-candidate',
            confirmed_at=first.received_at,
            expires_at=first.received_at + pd.Timedelta(hours=1))
        subject._pending[pattern.event_id] = pattern
        fourth = public_candle(SEED_COUNT + 3, delay=30)
        with patch('trading.m13_driver.assemble_context',
                   return_value=replace(base_context, pattern=pattern,
                       confirmation_candle=fourth.candle, as_of=fourth.received_at)):
            with self.assertRaisesRegex(ValueError,
                                        'Replay every eligible 5m candle; missing confirmation history'):
                subject.observe(fourth)
        self.assertTrue(subject._failed)
        self.assertFalse(subject.signals.history)

    def test_old_source_identity_refuses_shadow_and_paper_recovery(self):
        for kind in ('shadow', 'paper'):
            path = Path.cwd() / ('m13-chronology-' + uuid4().hex + '.sqlite')
            try:
                if kind == 'shadow':
                    original = ShadowSession.create(path)
                    recover = lambda: ShadowSession.recover(path)
                    target = 'trading.m13_shadow.source_hash'
                else:
                    original = M13PaperRuntime.create(path, actors)
                    recover = lambda: M13PaperRuntime.recover(path, actors)
                    target = 'trading.m13_runtime.source_hash'
                original.close()
                with patch(target, return_value='0' * 64):
                    with self.assertRaises(PaperJournalError):
                        recover()
            finally:
                for suffix in ('', '-wal', '-shm'):
                    candidate = Path(str(path) + suffix)
                    if candidate.is_file():
                        candidate.unlink()

    def test_paper_runtime_uses_same_boundary_and_recovers_deterministically(self):
        path = Path.cwd() / ('m13-paper-boundary-' + uuid4().hex + '.sqlite')
        run = None
        try:
            batches = {symbol: tuple(public_candle(i, symbol=symbol)
                        for i in range(SEED_COUNT))
                       for symbol in ('BTC/USDT', 'ETH/USDT')}
            retrieved = batches['BTC/USDT'][-1].candle.closed_at + pd.Timedelta(minutes=1)
            run = M13PaperRuntime.create(path, actors)
            run.seed_pair(batches, retrieved_at=retrieved)
            first = public_candle(SEED_COUNT, delay=30)
            historical = {symbol: replace(inputs(symbol)['pattern'],
                event_id='historical-paper-' + symbol,
                confirmed_at=first.opened_at,
                expires_at=first.opened_at + pd.Timedelta(hours=1))
                for symbol in ('BTC/USDT', 'ETH/USDT')}
            def scan(self, raw, symbol, timeframe='15m', as_of=None, contexts=()):
                return (historical[symbol],)
            with patch.object(PatternEngine, 'scan', new=scan):
                for symbol in ('BTC/USDT', 'ETH/USDT'):
                    result = run.frame(public_candle(SEED_COUNT, symbol=symbol, delay=30))
                    self.assertEqual(result['patterns'], ())
                    self.assertEqual(result['signals'], ())
                    self.assertFalse(run.drivers[symbol]._pending)
                before = run._visible_state()
                run.close()
                run = None
                restored = M13PaperRuntime.recover(path, actors)
                try:
                    self.assertEqual(restored._visible_state(), before)
                    self.assertEqual(restored.journal.head, restored.journal.applied)
                    self.assertFalse(restored.bridge.orders)
                    self.assertFalse(restored.bridge.risk.history)
                finally:
                    restored.close()
        finally:
            if run is not None:
                run.close()
            for suffix in ('', '-wal', '-shm'):
                candidate = Path(str(path) + suffix)
                if candidate.is_file():
                    candidate.unlink()

    def test_paired_failure_quarantines_without_partial_applied_frame(self):
        path = Path.cwd() / ('m13-shadow-pair-failure-' + uuid4().hex + '.sqlite')
        run = None
        try:
            batches = {symbol: tuple(public_candle(i, symbol=symbol)
                        for i in range(SEED_COUNT))
                       for symbol in ('BTC/USDT', 'ETH/USDT')}
            retrieved = batches['BTC/USDT'][-1].candle.closed_at + pd.Timedelta(minutes=1)
            run = ShadowSession.create(path)
            run.seed_pair(batches, retrieved_at=retrieved)
            pair = {symbol: public_candle(SEED_COUNT, symbol=symbol, delay=30)
                    for symbol in ('BTC/USDT', 'ETH/USDT')}
            invalid = dict(pair)
            invalid['ETH/USDT'] = public_candle(SEED_COUNT + 1,
                                                 symbol='ETH/USDT', delay=30)
            with self.assertRaisesRegex(ValueError, 'cutoffs differ'):
                run.observe_pair(invalid)
            self.assertEqual(run.journal.head, 1)
            self.assertTrue(all(len(driver.inputs) == SEED_COUNT
                                for driver in run._drivers.values()))
            with patch.object(run._drivers['ETH/USDT'], 'observe',
                              side_effect=RuntimeError('injected ETH failure')):
                with self.assertRaisesRegex(RuntimeError, 'injected ETH failure'):
                    run.observe_pair(pair)
            self.assertTrue(run._dead)
            self.assertEqual(run.journal.head, 2)
            # "applied" is the outcome-chain length, including quarantines.
            self.assertEqual(run.journal.applied, 2)
            self.assertEqual(run.journal.outcome(1)[0], 'APPLIED')
            self.assertEqual(run.journal.outcome(2)[0], 'QUARANTINED')
            self.assertFalse(run._drivers['ETH/USDT'].inputs[-1] == pair['ETH/USDT'])
            run.close()
            run = None
            with self.assertRaisesRegex(PaperJournalError, 'Quarantined shadow observation'):
                ShadowSession.recover(path)
        finally:
            if run is not None:
                run.close()
            for suffix in ('', '-wal', '-shm'):
                candidate = Path(str(path) + suffix)
                if candidate.is_file():
                    candidate.unlink()


if __name__ == '__main__':
    unittest.main()
