"""Offline Unit 4 journal, candle driver and crash replay regressions."""
import sys
sys.dont_write_bytecode = True

from dataclasses import replace
from decimal import Decimal as D
from pathlib import Path
import shutil
from uuid import uuid4
import sqlite3
import unittest
from unittest.mock import patch

import pandas as pd

from trading.freqtrade_bridge import FreqtradeSimulationBridge, SimulationFill, SimulationCancellation
from trading.candle_driver import CandleOutcome, CandleDecisionDriver, CandleInput
from trading.exit_funding import FundingObservation
from trading.pattern_engine import ClosedCandle
from trading.patterns import OHLC
from trading.paper_journal import PaperJournalError
from trading.paper_runtime import OfflinePaperRuntime
from trading.portfolio import Portfolio, Quote, identifier
from trading.risk import RiskEngine
from trading.signal_context import assemble_context
from trading.signals import SignalTracker
from test_candle_driver import START, candle, driver
from test_freqtrade_bridge import BINDINGS
from test_portfolio import spec
from test_risk import config, costs
from test_signal_context import inputs
from test_signals import policy


def actors():
    drivers = {symbol: driver(symbol) for symbol in ('BTC/USDT', 'ETH/USDT')}
    portfolio = Portfolio(as_of=START)
    return drivers, FreqtradeSimulationBridge(RiskEngine(portfolio, config(), mode='BACKTEST'),
                                               BINDINGS)


def quote(symbol, at, value=100):
    return Quote(symbol, D(value), at, at, 'explicit offline fixture')


def frame_funding(item, *, symbol=None, received_at=None, historical_eligible=True):
    symbol = item.symbol if symbol is None else symbol
    received = item.received_at if received_at is None else received_at
    mark = Quote(symbol, D(100), item.opened_at, received, 'funding fixture')
    fx = Quote('USDT', D(1), item.opened_at, received, 'funding fixture')
    return FundingObservation('frame-funding', symbol, item.opened_at,
                              item.opened_at, received, D('.001'), mark, fx,
                              'funding fixture', historical_eligible)


class QualifiedFixtureDriver(CandleDecisionDriver):
    """Adapter seam fixture: a real M7 qualification at its original cutoff."""

    def observe(self, item):
        if self._outcomes:
            self._inputs.append(item)
            outcome = CandleOutcome(item, True, self._outcomes[0].first_eligible_at,
                                    (), (), (), ())
            self._outcomes.append(outcome)
            return outcome
        record = SignalTracker(policy(), self.context_config, mode='BACKTEST').observe(
            assemble_context(**inputs()))
        if item.received_at != record.available_at:
            raise ValueError('Fixture candle must match actual M7 availability')
        self._inputs.append(item)
        outcome = CandleOutcome(item, True, item.received_at, (), (), (record,), ())
        self._outcomes.append(outcome)
        return outcome


class TerminalFixtureDriver(QualifiedFixtureDriver):
    def observe(self, item):
        tracker = SignalTracker(policy(), self.context_config, mode='BACKTEST')
        record = tracker.observe(assemble_context(**inputs()))
        invalidating = ClosedCandle(record.available_at, OHLC(101, 102, 93, 100),
                                     record.symbol)
        terminal = tracker.observe_signal(record.signal_id, invalidating,
                                           as_of=item.received_at)
        self.signals = tracker
        self._inputs.append(item)
        outcome = CandleOutcome(item, True, item.received_at, (), (), (record,), (terminal,))
        self._outcomes.append(outcome)
        return outcome


def qualified_actors():
    drivers, bridge = actors()
    drivers['BTC/USDT'] = QualifiedFixtureDriver('BTC/USDT',
        pattern_engine=drivers['BTC/USDT'].pattern_engine,
        structure_engine=drivers['BTC/USDT'].structure_engine,
        regime_config=drivers['BTC/USDT'].regime_engine.config,
        context_config=drivers['BTC/USDT'].context_config,
        signal_config=drivers['BTC/USDT'].signal_config,
        source=drivers['BTC/USDT'].source)
    return drivers, bridge


def terminal_actors():
    drivers, bridge = qualified_actors()
    prior = drivers['BTC/USDT']
    drivers['BTC/USDT'] = TerminalFixtureDriver('BTC/USDT',
        pattern_engine=prior.pattern_engine, structure_engine=prior.structure_engine,
        regime_config=prior.regime_engine.config, context_config=prior.context_config,
        signal_config=prior.signal_config, source=prior.source)
    return drivers, bridge


def qualified_item():
    at = pd.Timestamp('2025-01-01T01:20:00Z')
    return CandleInput('BTC/USDT', at-pd.Timedelta(minutes=5),
                       OHLC(99, 102, 98, 101), 10., at, 'synthetic fixture')


def admitted(run):
    item = qualified_item()
    at = item.received_at
    run.frame(item, mark=quote('BTC/USDT', at, 101), fx=quote('USDT', at, 1),
              spec=spec(available_at=at), costs=costs(available_at=at), leverage=2)
    return run.bridge.receipts[0]


class RuntimeTests(unittest.TestCase):
    def setUp(self):
        self.folder = Path.cwd() / ('m12-runtime-test-' + uuid4().hex)
        self.folder.mkdir()
        self.addCleanup(lambda: shutil.rmtree(self.folder))
        self.path = self.folder / 'runtime.sqlite'

    def assert_no_frame_effects(self, run):
        self.assertEqual(run.journal.verify(), ((), ()))
        self.assertEqual(run.journal.head, 0)
        self.assertIsNone(run._open_frame)
        self.assertFalse(run._dead)
        self.assertFalse(run.drivers['BTC/USDT'].inputs)
        self.assertFalse(run.bridge.risk.history)
        self.assertFalse(run.bridge.risk.portfolio.history)
        self.assertFalse(run.bridge.receipts)
        self.assertFalse(run.bridge.history)
        self.assertFalse(run.adapter.history)

    def test_wrong_asset_funding_fails_before_any_journal_or_actor_change(self):
        run = OfflinePaperRuntime.create(self.path, actors)
        item = candle(0)
        with self.assertRaisesRegex(ValueError, 'Funding must match'):
            run.frame(item, funding=(frame_funding(item, symbol='ETH/USDT'),))
        self.assert_no_frame_effects(run)
        run.close()

    def test_wrong_funding_receipt_fails_before_any_journal_or_actor_change(self):
        run = OfflinePaperRuntime.create(self.path, actors)
        item = candle(0)
        delayed = frame_funding(item, received_at=item.received_at + pd.Timedelta(seconds=1))
        with self.assertRaisesRegex(ValueError, 'Funding must match'):
            run.frame(item, funding=(delayed,))
        self.assert_no_frame_effects(run)
        run.close()

    def test_other_static_funding_incompatibilities_fail_before_journaling(self):
        for name, observations in (
                ('unverified_history', lambda item: (frame_funding(item, historical_eligible=False),)),
                ('duplicate_slot', lambda item: (frame_funding(item),
                    replace(frame_funding(item), event_id='second-funding')))):
            with self.subTest(name=name):
                path = self.folder / (name + '.sqlite')
                run = OfflinePaperRuntime.create(path, actors)
                item = candle(0)
                with self.assertRaises(ValueError):
                    run.frame(item, funding=observations(item))
                self.assert_no_frame_effects(run)
                run.close()

    def test_unjournaled_fanout_callback_failure_quarantines_until_fresh_recovery(self):
        run = OfflinePaperRuntime.create(self.path, qualified_actors)
        item = qualified_item()
        at = item.received_at
        kwargs = dict(mark=quote('BTC/USDT', at, 101), fx=quote('USDT', at, 1),
                      spec=spec(available_at=at), costs=costs(available_at=at), leverage=2)
        with patch('trading.paper_runtime.proposal_from_m7', side_effect=RuntimeError('callback fault')):
            with self.assertRaisesRegex(RuntimeError, 'callback fault'):
                run.frame(item, **kwargs)
        commands, outcomes = run.journal.verify()
        self.assertEqual([x[2] for x in commands], ['FRAME', 'QUOTE', 'QUOTE', 'EXIT_FRAME'])
        self.assertTrue(all(x[1] == 'APPLIED' for x in outcomes))
        self.assertTrue(run._dead)
        self.assertIsNotNone(run._open_frame)
        self.assertFalse(run._fanout_active)
        with self.assertRaises(PaperJournalError):
            run.frame(item, **kwargs)
        self.assertFalse(run.bridge.risk.history)
        run.close()
        with patch('trading.paper_runtime.proposal_from_m7', side_effect=RuntimeError('callback fault')):
            with self.assertRaisesRegex(RuntimeError, 'callback fault'):
                OfflinePaperRuntime.recover(self.path, qualified_actors)
        restored = OfflinePaperRuntime.recover(self.path, qualified_actors)
        self.assertIsNone(restored._open_frame)
        self.assertEqual(restored.journal.verify()[0][-1][2], 'FRAME_DONE')
        self.assertEqual(len(restored.bridge.risk.history), 1)
        self.assertEqual(len(restored.bridge.orders), 1)
        self.assertEqual(len(restored.bridge.risk.portfolio.history), 3)
        restored.close()

    def test_frame_produces_causal_facts_and_recovery_is_identical(self):
        run = OfflinePaperRuntime.create(self.path, actors)
        first = candle(0)
        result = run.frame(first, mark=quote(first.symbol, first.received_at),
                           fx=quote('USDT', first.received_at))
        self.assertEqual(result['kind'], 'FRAME')
        self.assertEqual(run.journal.verify()[1][0][1], 'APPLIED')
        self.assertFalse(run.bridge.orders)
        self.assertEqual(len(run.drivers[first.symbol].inputs), 1)
        history = run.bridge.risk.portfolio.history
        duplicate = run.frame(first, mark=quote(first.symbol, first.received_at),
                              fx=quote('USDT', first.received_at))
        self.assertEqual(duplicate, result)
        self.assertEqual(run.bridge.risk.portfolio.history, history)
        run.frame(candle(1))
        self.assertEqual(run.frame(first, mark=quote(first.symbol, first.received_at),
                                   fx=quote('USDT', first.received_at)), result)
        history = run.bridge.risk.portfolio.history
        run.close()
        recovered = OfflinePaperRuntime.recover(self.path, actors)
        self.assertEqual(recovered.bridge.risk.portfolio.history, history)
        self.assertEqual(recovered.frame(first, mark=quote(first.symbol, first.received_at),
                                         fx=quote('USDT', first.received_at)), result)
        self.assertEqual(recovered.reconcile(as_of=candle(1).received_at).cash, D(2000))
        recovered.close()

    def test_pending_before_and_after_actor_mutation_replays_on_fresh_graph(self):
        for mutate in (False, True):
            with self.subTest(mutate=mutate):
                path = self.folder / ('runtime-' + str(mutate) + '.sqlite')
                run = OfflinePaperRuntime.create(path, actors)
                item = candle(0)
                original = run._apply
                def crash(op, payload):
                    if mutate:
                        original(op, payload)
                    raise SystemExit('simulated interruption')
                with patch.object(run, '_apply', side_effect=crash):
                    with self.assertRaises(SystemExit):
                        run.frame(item)
                self.assertIsNone(run.journal.outcome(1))
                run.close()
                restored = OfflinePaperRuntime.recover(path, actors)
                self.assertEqual(len(restored.drivers[item.symbol].inputs), 1)
                self.assertEqual(restored.journal.verify()[1][0][1], 'APPLIED')
                restored.close()

    def test_committed_frame_resumes_unfinished_fanout_and_stale_writer_is_fenced(self):
        run = OfflinePaperRuntime.create(self.path, actors)
        item = candle(0)
        with patch.object(run, '_fanout', side_effect=SystemExit('after FRAME commit')):
            with self.assertRaises(SystemExit):
                run.frame(item)
        self.assertEqual(run.journal.verify()[1][0][1], 'APPLIED')
        restored = OfflinePaperRuntime.recover(self.path, actors)
        self.assertEqual(len(restored.drivers[item.symbol].inputs), 1)
        self.assertIsNone(restored._open_frame)
        self.assertEqual(restored.journal.verify()[0][-1][2], 'FRAME_DONE')
        with self.assertRaises(PaperJournalError):
            run.frame(item)
        run.close()
        restored.close()

    def test_terminal_handoff_is_committed_before_later_risk_failure(self):
        at = pd.Timestamp('2025-01-01T01:25:00Z')
        item = CandleInput('BTC/USDT', at-pd.Timedelta(minutes=5),
                           OHLC(101, 102, 93, 100), 10., at, 'terminal fixture')
        run = OfflinePaperRuntime.create(self.path, terminal_actors)
        with self.assertRaises(ValueError):
            run.frame(item, mark=quote('BTC/USDT', at, 100), fx=quote('USDT', at, 1))
        commands, outcomes = run.journal.verify()
        self.assertEqual([row[2] for row in commands[:2]], ['FRAME', 'TERMINAL'])
        self.assertEqual([row[1] for row in outcomes[:2]], ['APPLIED', 'APPLIED'])
        self.assertEqual(commands[-1][2], 'RISK')
        self.assertEqual(outcomes[-1][1], 'QUARANTINED')
        self.assertEqual(len(run.bridge.risk.terminal_history), 1)
        run.close()

    def test_revision_corruption_and_priority_fail_closed(self):
        run = OfflinePaperRuntime.create(self.path, actors, priority=('ETH/USDT', 'BTC/USDT'))
        eth = candle(0, symbol='ETH/USDT')
        btc = candle(0)
        run.frame(btc)
        with self.assertRaises(PaperJournalError):
            run.frame(eth)
        run.close()
        clean = self.folder / 'clean.sqlite'
        run = OfflinePaperRuntime.create(clean, actors)
        run.frame(btc)
        run.close()
        db = sqlite3.connect(clean)
        try:
            db.execute("UPDATE commands SET payload='corrupt' WHERE seq=1")
            db.commit()
        finally:
            db.close()
        with self.assertRaises(PaperJournalError):
            OfflinePaperRuntime.recover(clean, actors)

    def test_out_of_band_portfolio_mutation_is_rejected_before_next_command(self):
        run = OfflinePaperRuntime.create(self.path, actors)
        first = candle(0)
        run.frame(first)
        next_at = candle(1).received_at
        run.bridge.risk.portfolio.observe(quote('BTC/USDT', next_at), kind='MARK',
                                          as_of=next_at, event_id='out-of-band-mark')
        before = run.journal.head
        with self.assertRaises(PaperJournalError):
            run.frame(candle(1))
        self.assertEqual(run.journal.head, before)
        run.close()
        restored = OfflinePaperRuntime.recover(self.path, actors)
        self.assertEqual(len(restored.bridge.risk.portfolio.history), 0)
        restored.close()

    def test_failed_input_is_quarantined_and_no_authoritative_booking_occurs(self):
        run = OfflinePaperRuntime.create(self.path, actors)
        bad = candle(0)
        with self.assertRaises(ValueError):
            run.frame(bad, mark=quote('ETH/USDT', bad.received_at))
        self.assertEqual(run.journal.verify()[1][0][1], 'QUARANTINED')
        with self.assertRaises(PaperJournalError):
            run.frame(candle(1))
        run.close()
        with self.assertRaises(PaperJournalError):
            OfflinePaperRuntime.recover(self.path, actors)

    def test_qualified_m7_to_m8_to_m91_and_acknowledgement_replay(self):
        at = pd.Timestamp('2025-01-01T01:20:00Z')
        item = CandleInput('BTC/USDT', at-pd.Timedelta(minutes=5),
                           OHLC(99, 102, 98, 101), 10., at, 'synthetic fixture')
        run = OfflinePaperRuntime.create(self.path, qualified_actors)
        result = run.frame(item, mark=quote('BTC/USDT', at, 101), fx=quote('USDT', at, 1),
                           spec=spec(available_at=at), costs=costs(available_at=at), leverage=2)
        self.assertEqual(len(result['signals']), 1)
        self.assertEqual(len(result['decisions']), 1)
        self.assertEqual(run.bridge.risk.history[0].status, 'APPROVED')
        self.assertEqual(run.bridge.receipts[0].disposition, 'ACCEPTED')
        self.assertEqual(run.adapter.history[0].kind, 'ACKNOWLEDGED')
        self.assertEqual(len(run.bridge.orders), 1)
        self.assertEqual(len(run.bridge.risk.portfolio.snapshot(as_of=at).reservations), 1)
        run.close()
        restored = OfflinePaperRuntime.recover(self.path, qualified_actors)
        self.assertEqual(restored.frame(item, mark=quote('BTC/USDT', at, 101),
                           fx=quote('USDT', at, 1), spec=spec(available_at=at),
                           costs=costs(available_at=at), leverage=2), result)
        self.assertEqual(len(restored.bridge.risk.history), 1)
        self.assertEqual(len(restored.bridge.orders), 1)
        restored.close()

    def test_denied_risk_cannot_create_an_order(self):
        at = pd.Timestamp('2025-01-01T01:20:00Z')
        item = CandleInput('BTC/USDT', at-pd.Timedelta(minutes=5),
                           OHLC(99, 102, 98, 101), 10., at, 'synthetic fixture')
        run = OfflinePaperRuntime.create(self.path, qualified_actors)
        run.frame(item, mark=quote('BTC/USDT', at, 101), fx=quote('USDT', at, 1),
                  spec=spec(available_at=at, min_quantity=10000),
                  costs=costs(available_at=at), leverage=2)
        self.assertEqual(run.bridge.risk.history[0].status, 'REJECTED')
        self.assertEqual(run.bridge.receipts[0].disposition, 'DENIED')
        self.assertFalse(run.bridge.orders)
        self.assertFalse(run.bridge.risk.portfolio.snapshot(as_of=at).reservations)
        run.close()

    def test_entry_funding_and_automatic_target_exit_recover_without_duplicate_booking(self):
        run = OfflinePaperRuntime.create(self.path, qualified_actors)
        receipt = admitted(run)
        order, at = receipt.order, receipt.available_at
        fill = SimulationFill('entry-one', order.order_id, 'ENTRY', order.quantity_contracts,
                              order.entry_price, order.risk.costs.entry_fee_rate,
                              at, at, 'explicit synthetic execution')
        run.entry_fill(fill, take_profit_price=D(110), take_profit_slippage_rate=D(0), as_of=at)
        self.assertEqual(run.bridge.state(order.order_id), 'OPEN')
        first_exit_at = at + pd.Timedelta(minutes=5)
        run.frame(CandleInput('BTC/USDT', at, OHLC(101, 105, 100, 102), 10.,
                              first_exit_at, 'synthetic fixture'),
                  mark=quote('BTC/USDT', first_exit_at, 101), fx=quote('USDT', first_exit_at, 1))
        next_at = at + pd.Timedelta(minutes=10)
        opened = next_at - pd.Timedelta(minutes=5)
        settled = opened + pd.Timedelta(minutes=2)
        mark = Quote('BTC/USDT', D(101), settled, next_at, 'funding fixture')
        fx = Quote('USDT', D(1), settled, next_at, 'funding fixture')
        funding = FundingObservation('fund-one', 'BTC/USDT', settled, settled, next_at,
                                     D('.001'), mark, fx, 'funding fixture', True)
        flat = CandleInput('BTC/USDT', opened, OHLC(101, 105, 100, 102), 10.,
                           next_at, 'synthetic fixture')
        result = run.frame(flat, mark=mark, fx=fx, funding=(funding,))
        self.assertEqual(result['funding'], ('fund-one',))
        self.assertEqual(run.bridge.state(order.order_id), 'OPEN')
        exit_at = next_at + pd.Timedelta(minutes=5)
        target = CandleInput('BTC/USDT', next_at, OHLC(102, 111, 101, 110), 10.,
                             exit_at, 'synthetic fixture')
        closed = run.frame(target, mark=quote('BTC/USDT', exit_at, 110),
                           fx=quote('USDT', exit_at, 1))
        self.assertEqual(len(closed['exits']), 1)
        self.assertEqual(run.bridge.state(order.order_id), 'CLOSED')
        before = (run.bridge.risk.portfolio.history,
                  run.bridge.history, run.bridge.risk.portfolio.snapshot(as_of=exit_at))
        run.close()
        recovered = OfflinePaperRuntime.recover(self.path, qualified_actors)
        after = (recovered.bridge.risk.portfolio.history,
                 recovered.bridge.history, recovered.bridge.risk.portfolio.snapshot(as_of=exit_at))
        self.assertEqual(after, before)
        self.assertEqual(recovered.frame(target, mark=quote('BTC/USDT', exit_at, 110),
                                        fx=quote('USDT', exit_at, 1)), closed)
        self.assertEqual(len(recovered.bridge.risk.portfolio.history), len(before[0]))
        recovered.close()

    def test_cancelled_intent_releases_one_reservation_after_recovery(self):
        run = OfflinePaperRuntime.create(self.path, qualified_actors)
        receipt = admitted(run)
        at, order = receipt.available_at, receipt.order
        request = SimulationCancellation('cancel-one', order.order_id, 'fixture', at, at,
                                         'explicit synthetic cancellation')
        run.cancel(request, as_of=at)
        self.assertEqual(run.bridge.state(order.order_id), 'CANCELLED')
        self.assertFalse(run.bridge.risk.portfolio.snapshot(as_of=at).reservations)
        history = run.bridge.risk.portfolio.history
        run.close()
        recovered = OfflinePaperRuntime.recover(self.path, qualified_actors)
        self.assertEqual(recovered.bridge.risk.portfolio.history, history)
        self.assertEqual(recovered.cancel(request, as_of=at)['kind'], 'CANCEL')
        recovered.close()

    def test_pending_intent_expiry_is_authoritative_and_replayed_once(self):
        run = OfflinePaperRuntime.create(self.path, qualified_actors)
        receipt = admitted(run)
        order = receipt.order
        expired = run.expire(order.order_id, as_of=order.expires_at)
        self.assertEqual(expired['kind'], 'EXPIRE')
        self.assertEqual(run.bridge.state(order.order_id), 'EXPIRED')
        self.assertFalse(run.bridge.risk.portfolio.snapshot(as_of=order.expires_at).reservations)
        history = run.bridge.risk.portfolio.history
        run.close()
        restored = OfflinePaperRuntime.recover(self.path, qualified_actors)
        self.assertEqual(restored.expire(order.order_id, as_of=order.expires_at), expired)
        self.assertEqual(restored.bridge.risk.portfolio.history, history)
        restored.close()

    def test_crash_before_append_leaves_no_command_and_after_applied_returns_receipt(self):
        run = OfflinePaperRuntime.create(self.path, actors)
        item = candle(0)
        with patch.object(run.journal, 'append', side_effect=SystemExit('before PENDING')):
            with self.assertRaises(SystemExit):
                run.frame(item)
        self.assertEqual(run.journal.verify(), ((), ()))
        run.close()
        run = OfflinePaperRuntime.recover(self.path, actors)
        first = run.frame(item)
        next_item = candle(1)
        original_submit = run._submit
        def crash_after_applied(event_id, operation, payload):
            result = original_submit(event_id, operation, payload)
            if operation == 'FRAME_DONE':
                raise SystemExit('after APPLIED before acknowledgement')
            return result
        with patch.object(run, '_submit', side_effect=crash_after_applied):
            with self.assertRaises(SystemExit):
                run.frame(next_item)
        run.close()
        restored = OfflinePaperRuntime.recover(self.path, actors)
        self.assertEqual(restored.frame(item), first)
        self.assertEqual(len(restored.drivers[item.symbol].inputs), 2)
        self.assertEqual(restored.frame(next_item)['frame_id'], identifier(next_item))
        restored.close()

    def test_pending_funding_after_booking_replays_on_fresh_m8_only(self):
        run = OfflinePaperRuntime.create(self.path, qualified_actors)
        receipt = admitted(run)
        order, at = receipt.order, receipt.available_at
        entry = SimulationFill('funding-entry', order.order_id, 'ENTRY', order.quantity_contracts,
                               order.entry_price, order.risk.costs.entry_fee_rate,
                               at, at, 'explicit synthetic execution')
        run.entry_fill(entry, take_profit_price=D(110), take_profit_slippage_rate=D(0), as_of=at)
        next_at = at + pd.Timedelta(minutes=5)
        settled = at + pd.Timedelta(minutes=2)
        mark = Quote('BTC/USDT', D(101), settled, next_at, 'funding fixture')
        fx = Quote('USDT', D(1), settled, next_at, 'funding fixture')
        run.frame(CandleInput('BTC/USDT', at, OHLC(101, 105, 100, 102), 10.,
                              next_at, 'synthetic fixture'), mark=mark, fx=fx)
        observation = FundingObservation('crash-funding', 'BTC/USDT', settled,
            settled, next_at, D('.001'), mark, fx, 'funding fixture', True)
        original = run.controller.settle_funding
        def crash_after_booking(value):
            original(value)
            raise SystemExit('after M8 funding booking')
        with patch.object(run.controller, 'settle_funding', side_effect=crash_after_booking):
            with self.assertRaises(SystemExit):
                run.funding(observation)
        self.assertIsNone(run.journal.outcome(run.journal.head))
        run.close()
        restored = OfflinePaperRuntime.recover(self.path, qualified_actors)
        self.assertEqual(restored.funding(observation)['effect'], 'SETTLED')
        self.assertEqual(len([x for x in restored.bridge.risk.portfolio.history
                              if x.kind == 'FUNDING']), 1)
        restored.close()

    def test_interrupted_recovery_closes_graph_and_next_recovery_succeeds(self):
        run = OfflinePaperRuntime.create(self.path, actors)
        run.frame(candle(0))
        run.close()
        with patch.object(OfflinePaperRuntime, '_apply', side_effect=SystemExit('replay interruption')):
            with self.assertRaises(SystemExit):
                OfflinePaperRuntime.recover(self.path, actors)
        restored = OfflinePaperRuntime.recover(self.path, actors)
        self.assertEqual(len(restored.drivers['BTC/USDT'].inputs), 1)
        restored.close()

    def test_crashes_after_risk_admission_and_ack_mutation_replay_once(self):
        item = qualified_item()
        at = item.received_at
        for operation in ('RISK', 'ADMIT', 'ACK'):
            with self.subTest(operation=operation):
                path = self.folder / (operation.lower() + '.sqlite')
                run = OfflinePaperRuntime.create(path, qualified_actors)
                original = run._apply
                def crash_after_mutation(kind, payload):
                    result = original(kind, payload)
                    if kind == operation:
                        raise SystemExit('after authoritative actor mutation')
                    return result
                with patch.object(run, '_apply', side_effect=crash_after_mutation):
                    with self.assertRaises(SystemExit):
                        run.frame(item, mark=quote('BTC/USDT', at, 101),
                                  fx=quote('USDT', at, 1), spec=spec(available_at=at),
                                  costs=costs(available_at=at), leverage=2)
                self.assertIsNone(run.journal.outcome(run.journal.head))
                run.close()
                restored = OfflinePaperRuntime.recover(path, qualified_actors)
                self.assertEqual(len(restored.bridge.risk.history), 1)
                self.assertEqual(len(restored.bridge.receipts), 1)
                self.assertEqual(len(restored.bridge.orders), 1)
                self.assertEqual(len(restored.adapter.history), 1)
                self.assertEqual(len(restored.bridge.risk.portfolio.snapshot(
                    as_of=at).reservations), 1)
                restored.close()

    def test_terminal_handoff_crash_after_bridge_callback_replays_once(self):
        at = pd.Timestamp('2025-01-01T01:25:00Z')
        item = CandleInput('BTC/USDT', at-pd.Timedelta(minutes=5),
                           OHLC(101, 102, 93, 100), 10., at, 'terminal fixture')
        run = OfflinePaperRuntime.create(self.path, terminal_actors)
        original = run.controller.on_signal_terminal
        def crash_after_terminal(event, *, as_of):
            original(event, as_of=as_of)
            raise SystemExit('after M9.1 terminal callback')
        with patch.object(run.controller, 'on_signal_terminal', side_effect=crash_after_terminal):
            with self.assertRaises(SystemExit):
                run.frame(item, mark=quote('BTC/USDT', at, 100),
                          fx=quote('USDT', at, 1), spec=spec(available_at=at),
                          costs=costs(available_at=at), leverage=2)
        self.assertEqual(run.journal.verify()[0][-1][2], 'TERMINAL')
        self.assertIsNone(run.journal.outcome(run.journal.head))
        run.close()
        restored = OfflinePaperRuntime.recover(self.path, terminal_actors)
        self.assertEqual(len(restored.bridge.risk.terminal_history), 1)
        self.assertEqual(len(restored.bridge.risk.history), 1)
        self.assertEqual(restored.bridge.risk.history[0].status, 'REJECTED')
        self.assertFalse(restored.bridge.orders)
        restored.close()

    def test_exit_callback_crash_after_m8_close_replays_one_booking(self):
        run = OfflinePaperRuntime.create(self.path, qualified_actors)
        receipt = admitted(run)
        order, at = receipt.order, receipt.available_at
        entry = SimulationFill('exit-crash-entry', order.order_id, 'ENTRY',
                               order.quantity_contracts, order.entry_price,
                               order.risk.costs.entry_fee_rate, at, at, 'synthetic execution')
        run.entry_fill(entry, take_profit_price=D(110), take_profit_slippage_rate=D(0), as_of=at)
        next_at = at + pd.Timedelta(minutes=5)
        run.frame(CandleInput('BTC/USDT', at, OHLC(101, 105, 100, 102), 10.,
                              next_at, 'synthetic fixture'),
                  mark=quote('BTC/USDT', next_at, 102), fx=quote('USDT', next_at, 1))
        exit_at = next_at + pd.Timedelta(minutes=5)
        item = CandleInput('BTC/USDT', next_at, OHLC(102, 111, 101, 110), 10.,
                           exit_at, 'synthetic fixture')
        original = run.controller.observe_candle
        def crash_after_exit(candle):
            original(candle)
            raise SystemExit('after M8 close booking')
        with patch.object(run.controller, 'observe_candle', side_effect=crash_after_exit):
            with self.assertRaises(SystemExit):
                run.frame(item, mark=quote('BTC/USDT', exit_at, 110),
                          fx=quote('USDT', exit_at, 1))
        self.assertEqual(run.journal.verify()[0][-1][2], 'EXIT_FRAME')
        run.close()
        restored = OfflinePaperRuntime.recover(self.path, qualified_actors)
        self.assertEqual(restored.bridge.state(order.order_id), 'CLOSED')
        self.assertEqual(len([x for x in restored.bridge.risk.portfolio.history
                              if x.kind == 'SIMULATED_CLOSE']), 1)
        self.assertEqual(len([x for x in restored.bridge.history
                              if x.kind == 'EXIT_FILLED']), 1)
        restored.close()


if __name__ == '__main__':
    unittest.main()
