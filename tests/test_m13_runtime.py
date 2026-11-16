"""Fresh v2 journal, current-mode actors, and fail-closed replay."""
import sqlite3
from dataclasses import replace
import unittest
from pathlib import Path
from unittest.mock import patch
from uuid import uuid4

from trading.m13_driver import CurrentCandleDriver
from trading.m13_feed import SOURCE_IDS
from trading.m13_runtime import M13PaperRuntime
from m13_public_poll import PublicKlinePoller, seed_live_runtime, advance_live_runtime
from trading.m13_runtime_journal import PRESERVED, environment_identity, source_hash
from trading.m13_targets import TargetPolicy
from trading.candle_driver import CandleOutcome, CandleInput
from trading.patterns import OHLC
from trading.paper_journal import PaperJournalError
from trading.paper_runtime import OfflinePaperRuntime
from trading.portfolio import Portfolio, Quote
from trading.risk import RiskEngine, proposal_from_m7
from trading.freqtrade_bridge import FreqtradeSimulationBridge
from trading.signal_context import assemble_context
from trading.signals import SignalTracker
from test_candle_driver import START, candle, driver
from trading.candle_driver import WARMUP_5M_BARS
from test_freqtrade_bridge import BINDINGS
from test_risk import config, costs
from test_portfolio import spec
from test_signal_context import inputs
from test_signals import policy
from test_paper_runtime import actors as v1_actors
import pandas as pd
from decimal import Decimal as D


def actors():
    drivers = {}
    for symbol in ('BTC/USDT', 'ETH/USDT'):
        original = driver(symbol)
        drivers[symbol] = CurrentCandleDriver(symbol,
            pattern_engine=original.pattern_engine,
            structure_engine=original.structure_engine,
            regime_config=original.regime_engine.config,
            context_config=original.context_config,
            signal_config=original.signal_config,
            source=original.source)
    portfolio = Portfolio(as_of=START)
    return drivers, FreqtradeSimulationBridge(RiskEngine(portfolio, config(), mode='CURRENT'), BINDINGS)


def public_candle(index, **kwargs):
    item = candle(index, **kwargs)
    return replace(item, source_id=SOURCE_IDS[item.symbol.replace('/', '')])


class QualifiedCurrentDriver(CurrentCandleDriver):
    def observe(self, item):
        if self._outcomes:
            self._inputs.append(item)
            result = CandleOutcome(item, True, self._outcomes[0].first_eligible_at,
                                   (), (), (), ())
            self._outcomes.append(result)
            return result
        fields = inputs()
        fields['mode'] = 'CURRENT'
        context = assemble_context(**fields)
        tracker = SignalTracker(policy(), context.config, mode='CURRENT')
        record = tracker.observe(context)
        if not record.qualification_passed or item.received_at != record.available_at:
            raise ValueError('Current fixture did not qualify at original cutoff')
        self.signals = tracker
        self._inputs.append(item)
        result = CandleOutcome(item, True, item.received_at, (), (), (record,), ())
        self._outcomes.append(result)
        return result


def qualified_actors():
    drivers, bridge = actors()
    original = drivers['BTC/USDT']
    drivers['BTC/USDT'] = QualifiedCurrentDriver('BTC/USDT',
        pattern_engine=original.pattern_engine,
        structure_engine=original.structure_engine,
        regime_config=original.regime_engine.config,
        context_config=original.context_config,
        signal_config=original.signal_config,
        source=original.source)
    return drivers, bridge


class M13RuntimeTests(unittest.TestCase):
    def setUp(self):
        self.path = Path.cwd() / ('m13-test-' + uuid4().hex + '.sqlite')
        def remove_own_test_files():
            for suffix in ('', '-wal', '-shm'):
                candidate = Path(str(self.path) + suffix)
                if candidate.is_file():
                    candidate.unlink()
        self.addCleanup(remove_own_test_files)

    def test_fresh_journal_frame_duplicate_and_recovery(self):
        run = M13PaperRuntime.create(self.path, actors)
        item = public_candle(0)
        first = run.frame(item)
        self.assertEqual(first['kind'], 'FRAME')
        self.assertEqual(run.frame(item), first)
        self.assertEqual(run.journal.head, 3)  # FRAME, EXIT_FRAME, FRAME_DONE
        before = run._visible_state()
        run.close()
        restored = M13PaperRuntime.recover(self.path, actors)
        self.assertEqual(restored._visible_state(), before)
        self.assertEqual(restored.frame(item), first)
        self.assertEqual(restored.journal.head, 3)
        restored.close()

    def test_pending_command_replays_and_stale_writer_is_fenced(self):
        run = M13PaperRuntime.create(self.path, actors)
        item = public_candle(0)
        with patch.object(run.journal, 'finish', side_effect=RuntimeError('crash')):
            with self.assertRaises(RuntimeError):
                run.frame(item)
        self.assertTrue(run._dead)
        restored = M13PaperRuntime.recover(self.path, actors)
        self.assertEqual(restored.journal.head, 3)
        self.assertEqual(len(restored.drivers['BTC/USDT'].inputs), 1)
        with self.assertRaises(PaperJournalError):
            run.journal.assert_owner()
        run.close()
        restored.close()

    def test_revised_payload_v1_refusal_and_corruption(self):
        run = M13PaperRuntime.create(self.path, actors)
        item = public_candle(0)
        run.frame(item)
        with self.assertRaises(PaperJournalError):
            run.frame(public_candle(0, delay=1))
        run.close()
        db = sqlite3.connect(self.path)
        try:
            db.execute("UPDATE commands SET payload='{}' WHERE seq=1")
            db.commit()
        finally:
            db.close()
        with self.assertRaises(PaperJournalError):
            M13PaperRuntime.recover(self.path, actors)

    def test_preserved_closure_and_environment_identity(self):
        self.assertEqual(len(PRESERVED), 30)
        self.assertEqual(len(set(PRESERVED)), 30)
        self.assertEqual(len(source_hash()), 64)
        self.assertEqual(len(environment_identity()), 64)
        with patch('trading.m13_runtime_journal.pd.__version__', '0.0.0'):
            with self.assertRaisesRegex(ValueError, 'environment mismatch'):
                environment_identity()

    def test_current_driver_requires_complete_9600_bar_warmup(self):
        subject = actors()[0]['BTC/USDT']
        self.assertEqual(subject.signals.mode, 'CURRENT')
        for index in range(WARMUP_5M_BARS - 1):
            last = subject.observe(candle(index))
        self.assertFalse(last.warmup_ready)
        final = subject.observe(candle(WARMUP_5M_BARS - 1))
        self.assertTrue(final.warmup_ready)
        self.assertEqual(len(subject.completed_candles('4h')), 200)
        self.assertEqual(final.first_eligible_at,
                         START + pd.Timedelta(minutes=5*WARMUP_5M_BARS))

    def test_both_historical_seeds_are_journaled_without_backdated_signals(self):
        run = M13PaperRuntime.create(self.path, actors)
        at = START + pd.Timedelta(minutes=5*WARMUP_5M_BARS+1)
        btc = tuple(public_candle(i) for i in range(WARMUP_5M_BARS))
        eth = tuple(public_candle(i, symbol='ETH/USDT') for i in range(WARMUP_5M_BARS))
        damaged = list(btc)
        damaged[50] = public_candle(51)
        with self.assertRaisesRegex(ValueError, 'gap'):
            run.seed('BTC/USDT', tuple(damaged), retrieved_at=at)
        self.assertEqual(run.journal.head, 0)
        run.seed('BTC/USDT', btc, retrieved_at=at)
        self.assertEqual(run.journal.head, 1)
        self.assertFalse(run.drivers['BTC/USDT'].signals.history)
        with self.assertRaisesRegex(ValueError, 'Both assets'):
            run.frame(public_candle(WARMUP_5M_BARS, delay=1))
        self.assertEqual(run.journal.head, 1)
        run.seed('ETH/USDT', eth, retrieved_at=at)
        result = run.frame(public_candle(WARMUP_5M_BARS, delay=1))
        self.assertEqual(result['signals'], ())
        self.assertFalse(run.bridge.risk.history)
        self.assertEqual(run.journal.head, 5)
        saved = run._visible_state()
        run.close()
        restored = M13PaperRuntime.recover(self.path, actors)
        self.assertEqual(restored._visible_state(), saved)
        self.assertEqual(restored.journal.head, 5)
        restored.close()

    def test_public_seed_pair_is_one_recoverable_operation(self):
        cutoff = START + pd.Timedelta(minutes=5*WARMUP_5M_BARS)
        class FixturePoller(PublicKlinePoller):
            def __init__(self):
                super().__init__(session=object())
                self.batches = {}
            def fetch_history(self, symbol, *, start_ms, count, received_at=None):
                first = pd.Timestamp(start_ms, unit='ms', tz='UTC')
                result = tuple(CandleInput(symbol.replace('USDT', '/USDT'),
                    first+i*pd.Timedelta(minutes=5), OHLC(100, 101, 99, 100),
                    10., first+(i+1)*pd.Timedelta(minutes=5), SOURCE_IDS[symbol])
                    for i in range(count))
                self._last[symbol] = result[-1]
                self.batches[symbol.replace('USDT', '/USDT')] = result
                return result
        times = iter((cutoff+pd.Timedelta(minutes=1), cutoff+pd.Timedelta(minutes=2)))
        run = M13PaperRuntime.create(self.path, actors)
        poller = FixturePoller()
        seed = seed_live_runtime(run, poller, clock=lambda: next(times))
        self.assertEqual((seed['count'], seed['cutoff']), (WARMUP_5M_BARS, cutoff))
        self.assertEqual(run.journal.head, 1)
        self.assertEqual(run.seed_pair(poller.batches,
                                       retrieved_at=seed['retrieved_at']), seed['journal_receipt'])
        self.assertFalse(run.bridge.risk.history)
        self.assertTrue(all(len(x.inputs) == WARMUP_5M_BARS for x in run.drivers.values()))
        live = CandleInput('BTC/USDT', cutoff, OHLC(100, 101, 99, 100), 10.,
                           cutoff+pd.Timedelta(minutes=5, seconds=1), SOURCE_IDS['BTCUSDT'])
        run.frame(live)
        saved = run._visible_state()
        run.close()
        restored = M13PaperRuntime.recover(self.path, actors)
        self.assertEqual(restored._visible_state(), saved)
        self.assertEqual(restored.journal.head, 4)
        changed = dict(poller.batches)
        changed['BTC/USDT'] = (replace(changed['BTC/USDT'][0], volume=11.),
                               *changed['BTC/USDT'][1:])
        with self.assertRaisesRegex(PaperJournalError, 'revise'):
            restored.seed_pair(changed, retrieved_at=seed['retrieved_at'])
        restored.close()

    def test_live_batch_requires_both_assets_before_priority_ordered_frames(self):
        class FixturePoller(PublicKlinePoller):
            def __init__(self, missing=False):
                super().__init__(session=object())
                self.missing = missing
            def fetch(self, symbol, **kwargs):
                if self.missing and symbol == 'ETHUSDT':
                    return ()
                result = public_candle(0, symbol=symbol.replace('USDT', '/USDT'))
                self._last[symbol] = result
                return (result,)
        run = M13PaperRuntime.create(self.path, actors)
        poller = FixturePoller(missing=True)
        with self.assertRaisesRegex(ValueError, 'cutoffs differ'):
            advance_live_runtime(run, poller)
        self.assertEqual(run.journal.head, 0)
        self.assertFalse(poller._last)
        poller.missing = False
        result = advance_live_runtime(run, poller)
        self.assertEqual(len(result), 2)
        self.assertEqual(run.journal.head, 6)
        self.assertEqual(tuple(run.drivers[s].inputs[0].symbol for s in run.priority),
                         run.priority)
        run.close()

    def test_live_trade_batch_missing_risk_inputs_has_no_partial_frame(self):
        class FixturePoller(PublicKlinePoller):
            def __init__(self):
                super().__init__(session=object())
            def fetch(self, symbol, **kwargs):
                item = public_candle(0, symbol=symbol.replace('USDT', '/USDT'))
                self._last[symbol] = item
                return (item,)
        run = M13PaperRuntime.create(self.path, qualified_actors,
            proposal_from_m7=proposal_from_m7,
            targets=TargetPolicy(D(1), D(2), D(0), 'test-only-unvalidated-targets'))
        poller = FixturePoller()
        with self.assertRaisesRegex(ValueError, 'Complete M8 risk inputs'):
            advance_live_runtime(run, poller, inputs_for=lambda item: {})
        self.assertEqual(run.journal.head, 0)
        self.assertFalse(poller._last)
        self.assertFalse(run.bridge.risk.history)
        self.assertFalse(run.bridge.orders)
        run.close()

    def test_live_runtime_input_rejection_restores_uncommitted_poll_cursor(self):
        class FixturePoller(PublicKlinePoller):
            def __init__(self):
                super().__init__(session=object())
            def fetch(self, symbol, **kwargs):
                item = public_candle(0, symbol=symbol.replace('USDT', '/USDT'))
                self._last[symbol] = item
                return (item,)
        run = M13PaperRuntime.create(self.path, actors)
        poller = FixturePoller()
        with self.assertRaisesRegex(ValueError, 'Market, instrument and cost inputs'):
            advance_live_runtime(run, poller, inputs_for=lambda item:
                {'mark': Quote('WRONG/USDT', D(100), item.received_at,
                               item.received_at, 'fixture')})
        self.assertEqual(run.journal.head, 0)
        self.assertFalse(poller._last)
        run.close()

    def test_failed_pair_acquisition_restores_poller_cursor(self):
        class FailingPoller(PublicKlinePoller):
            def __init__(self):
                super().__init__(session=object())
            def fetch_history(self, symbol, **kwargs):
                if symbol == 'ETHUSDT':
                    raise ValueError('incomplete ETH warmup')
                cutoff = START + pd.Timedelta(minutes=5*WARMUP_5M_BARS)
                self._last[symbol] = CandleInput('BTC/USDT',
                    cutoff-pd.Timedelta(minutes=5), OHLC(100, 101, 99, 100),
                    10., cutoff, SOURCE_IDS[symbol])
                return (self._last[symbol],)
        run = M13PaperRuntime.create(self.path, actors)
        poller = FailingPoller()
        cutoff = START + pd.Timedelta(minutes=5*WARMUP_5M_BARS)
        with self.assertRaisesRegex(ValueError, 'incomplete ETH warmup'):
            seed_live_runtime(run, poller,
                clock=lambda: cutoff+pd.Timedelta(minutes=1))
        self.assertEqual(run.journal.head, 0)
        self.assertFalse(poller._last)
        self.assertFalse(run.drivers['BTC/USDT'].inputs)
        run.close()

    def test_current_signal_risk_admission_and_exact_synthetic_entry(self):
        at = pd.Timestamp('2025-01-01T01:20:00Z')
        item = CandleInput('BTC/USDT', at-pd.Timedelta(minutes=5),
                           OHLC(99, 102, 98, 101), 10., at, SOURCE_IDS['BTCUSDT'])
        targets = TargetPolicy(D(1), D(2), D(0), 'test-only-unvalidated-targets')
        run = M13PaperRuntime.create(self.path, qualified_actors,
                                     proposal_from_m7=proposal_from_m7, targets=targets)
        result = run.frame(item, mark=Quote('BTC/USDT', D(101), at, at, 'fixture'),
                           fx=Quote('USDT', D(1), at, at, 'fixture'),
                           spec=spec(available_at=at), costs=costs(available_at=at),
                           leverage=2)
        self.assertEqual(len(result['decisions']), 1)
        self.assertEqual(len(result['admissions']), 1)
        self.assertEqual(len(result['entries']), 1)
        order, = run.bridge.orders
        self.assertEqual(run.bridge.state(order.order_id), 'OPEN')
        self.assertEqual(run.bridge.risk.history[0].status, 'APPROVED')
        self.assertEqual(run.controller.plans[0].order, order)
        self.assertEqual(len(run.bridge.risk.portfolio.snapshot(as_of=at).positions), 1)
        original = run._visible_state()
        run.close()
        replay = M13PaperRuntime.recover(self.path, qualified_actors,
                                         proposal_from_m7=proposal_from_m7, targets=targets)
        self.assertEqual(replay._visible_state(), original)
        self.assertEqual(replay.frame(item, mark=Quote('BTC/USDT', D(101), at, at, 'fixture'),
                                      fx=Quote('USDT', D(1), at, at, 'fixture'),
                                      spec=spec(available_at=at), costs=costs(available_at=at),
                                      leverage=2), result)
        replay.close()

    def test_tp1_amendment_then_full_tp2_exit_replays(self):
        at = pd.Timestamp('2025-01-01T01:20:00Z')
        targets = TargetPolicy(D(1), D(2), D(0), 'test-only-unvalidated-targets')
        run = M13PaperRuntime.create(self.path, qualified_actors,
                                     proposal_from_m7=proposal_from_m7, targets=targets)
        opening = CandleInput('BTC/USDT', at-pd.Timedelta(minutes=5),
                              OHLC(99, 102, 98, 101), 10., at, SOURCE_IDS['BTCUSDT'])
        def quotes(value, when):
            return dict(mark=Quote('BTC/USDT', D(value), when, when, 'fixture'),
                        fx=Quote('USDT', D(1), when, when, 'fixture'),
                        spec=spec(available_at=when), costs=costs(available_at=when),
                        leverage=2)
        run.frame(opening, **quotes(101, at))
        order, = run.bridge.orders
        tp1, tp2 = targets.prices(order)
        middle_at = at + pd.Timedelta(minutes=5)
        middle = CandleInput('BTC/USDT', at,
                             OHLC(101, float(tp1)+1, 100, float(tp1)),
                             10., middle_at, SOURCE_IDS['BTCUSDT'])
        run.frame(middle, **quotes(tp1+1, middle_at))
        self.assertEqual(run.bridge.state(order.order_id), 'OPEN')
        self.assertEqual(len(run.controller.amendments), 1)
        self.assertFalse(run.controller.pending_amendments)
        ending_at = middle_at + pd.Timedelta(minutes=5)
        final = CandleInput('BTC/USDT', middle_at,
                            OHLC(float(tp1), float(tp2)+1, float(tp1)-1, float(tp2)),
                            10., ending_at, SOURCE_IDS['BTCUSDT'])
        run.frame(final, **quotes(tp2, ending_at))
        self.assertEqual(run.bridge.state(order.order_id), 'CLOSED')
        snapshot = run.bridge.risk.portfolio.snapshot(as_of=ending_at)
        self.assertFalse(snapshot.positions)
        self.assertEqual(snapshot.realized_pnl,
                         (tp2-order.entry_price)*order.quantity_contracts*order.allocation.spec.multiplier)
        self.assertEqual(snapshot.cash, D(2000)+snapshot.realized_pnl)
        self.assertEqual(snapshot.equity, snapshot.cash)
        self.assertEqual((snapshot.fees_paid, snapshot.funding_cash), (D(0), D(0)))
        saved = run._visible_state()
        run.close()
        restored = M13PaperRuntime.recover(self.path, qualified_actors,
                                           proposal_from_m7=proposal_from_m7, targets=targets)
        self.assertEqual(restored._visible_state(), saved)
        self.assertEqual(restored.bridge.state(order.order_id), 'CLOSED')
        restored.close()

    def test_empty_and_populated_v1_v2_journals_refuse_cross_recovery(self):
        for populated in (False, True):
            with self.subTest(owner='v1', populated=populated):
                path = Path.cwd() / ('m13-compat-v1-' + uuid4().hex + '.sqlite')
                try:
                    v1 = OfflinePaperRuntime.create(path, v1_actors)
                    if populated:
                        v1.frame(public_candle(0))
                    v1.close()
                    with self.assertRaises(PaperJournalError):
                        M13PaperRuntime.recover(path, actors)
                finally:
                    for suffix in ('', '-wal', '-shm'):
                        candidate = Path(str(path)+suffix)
                        if candidate.is_file():
                            candidate.unlink()
            with self.subTest(owner='v2', populated=populated):
                path = Path.cwd() / ('m13-compat-v2-' + uuid4().hex + '.sqlite')
                try:
                    v2 = M13PaperRuntime.create(path, actors)
                    if populated:
                        v2.frame(public_candle(0))
                    v2.close()
                    with self.assertRaises(PaperJournalError):
                        OfflinePaperRuntime.recover(path, v1_actors)
                finally:
                    for suffix in ('', '-wal', '-shm'):
                        candidate = Path(str(path)+suffix)
                        if candidate.is_file():
                            candidate.unlink()


if __name__ == '__main__':
    unittest.main()
