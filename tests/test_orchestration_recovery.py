import sys
sys.dont_write_bytecode = True

from dataclasses import replace
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

import pandas as pd

from trading.orchestration import OfflineOrchestrator, ReplayFrame, recover
from trading.freqtrade_bridge import (AdmissionReceipt, FreqtradeSimulationBridge,
                                      SimulationFill)
from trading.pattern_engine import ClosedCandle
from trading.portfolio import Quote
from trading.patterns import OHLC
from trading.replay_journal import FencedWriter, JournalError
from trading.replay_codec import identity
from test_orchestration import admission_frame, envelope
from test_portfolio import spec
from test_replay_codec import run_spec
from test_risk import costs
from test_signal_context import inputs


def finish(run):
    outcomes = []
    while (outcome := run.advance()) is not None:
        outcomes.append(outcome)
    return outcomes


def approve(run):
    frame, _ = admission_frame()
    run.submit_frame(frame)
    finish(run)
    at = frame.as_of+pd.Timedelta(seconds=1)
    signal = run.signals[0]
    run.submit_frame(ReplayFrame('risk', at, (envelope('risk-btc', 'RISK', at,
                     (signal.signal_id, spec(available_at=at),
                      costs(available_at=at), 2, None), 1),)))
    return at


class RecoveryTests(unittest.TestCase):
    def setUp(self):
        self.temp = TemporaryDirectory(dir=Path.cwd())
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name)/'run.sqlite'
        self.specification = run_spec()
        self.run = OfflineOrchestrator.create(self.specification, self.path)
        self.addCleanup(lambda: self.run.journal.close())

    def test_mutation_before_effect_commit_discards_actor_graph_and_replays_pending(self):
        frame, _ = admission_frame()
        self.run.submit_frame(frame)
        def crash(*args, **kwargs):
            raise OSError('before effects commit')
        with patch.object(self.run.journal, 'apply', side_effect=crash):
            with self.assertRaises(OSError):
                self.run.advance()
        self.assertIsNone(self.run._actors)
        rebuilt = recover(self.path, self.specification)
        self.addCleanup(rebuilt.journal.close)
        self.assertEqual(rebuilt.journal.next_core()['command_id'], frame.commands[0].command_id)
        finish(rebuilt)
        self.assertEqual(len(rebuilt.signals), 1)

    def test_effect_commit_before_acknowledgment_replays_once(self):
        frame, _ = admission_frame()
        self.run.submit_frame(frame)
        committed = self.run.journal.apply
        def crash(*args, **kwargs):
            committed(*args, **kwargs)
            raise OSError('ack lost')
        with patch.object(self.run.journal, 'apply', side_effect=crash):
            with self.assertRaises(OSError):
                self.run.advance()
        rebuilt = recover(self.path, self.specification)
        self.addCleanup(rebuilt.journal.close)
        self.assertEqual(len(rebuilt.journal.applied()), 1)
        self.assertEqual(rebuilt.receipt(frame.commands[0].command_id).kind, 'FX')
        self.assertIsNone(rebuilt.receipt(frame.commands[1].command_id))
        finish(rebuilt)
        self.assertEqual(len(rebuilt.signals), 1)
        self.assertEqual(rebuilt.submit_frame(frame).frame_hash, frame.frame_hash)

    def test_stale_writer_rejected_after_recovery_takeover(self):
        successor = recover(self.path, self.specification)
        self.addCleanup(successor.journal.close)
        frame, _ = admission_frame()
        with self.assertRaises(FencedWriter):
            self.run.submit_frame(frame)
        successor.submit_frame(frame)
        finish(successor)
        self.assertEqual(len(successor.signals), 1)

    def test_both_asset_priorities_are_recorded_and_order_risk(self):
        for priority in (('BTC/USDT','ETH/USDT'), ('ETH/USDT','BTC/USDT')):
            with self.subTest(priority=priority), TemporaryDirectory(dir=Path.cwd()) as directory:
                spec_run = run_spec(priority)
                run = OfflineOrchestrator.create(spec_run, Path(directory)/'run.sqlite')
                try:
                    btc, _ = admission_frame('BTC/USDT', name='btc')
                    eth, _ = admission_frame('ETH/USDT', name='eth')
                    at = btc.as_of
                    commands = tuple(replace(command, ordinal=i) for i, command in enumerate(
                        (btc.commands[0], btc.commands[1], eth.commands[1],
                         btc.commands[2], eth.commands[2]), 1))
                    run.submit_frame(ReplayFrame('both-contexts', at, commands))
                    finish(run)
                    self.assertEqual(len(run.signals), 2)
                    risk_at = at+pd.Timedelta(seconds=1)
                    risks = tuple(envelope('risk-'+record.symbol, 'RISK', risk_at,
                                  (record.signal_id, spec(record.symbol, available_at=risk_at),
                                   costs(available_at=risk_at), 2, None), i)
                                  for i, record in enumerate(run.signals))
                    run.submit_frame(ReplayFrame('both-risks', risk_at, risks))
                    finish(run)
                    self.assertEqual(tuple(d.proposal.symbol for d in run.decisions), priority)
                    self.assertIn(run.decisions[0].status, ('APPROVED', 'REDUCED'))
                    self.assertIn(run.decisions[1].status, ('BLOCKED', 'REJECTED', 'REDUCED'))
                    self.assertTrue(run.decisions[1].status == 'BLOCKED'
                                    or run.decisions[1].quantity < run.decisions[0].quantity,
                                    tuple((d.status, d.quantity) for d in run.decisions))
                    self.assertEqual(len(run.admissions), 2)
                    rebuilt = recover(run.path, spec_run)
                    try:
                        self.assertEqual(rebuilt.decisions, run.decisions)
                    finally:
                        rebuilt.journal.close()
                finally:
                    run.journal.close()

    def test_consumed_m7_context_terminal_handoff_is_next_replay_unit(self):
        first, _ = admission_frame()
        self.run.submit_frame(first)
        finish(self.run)
        signal = self.run.signals[0]
        fields = inputs(as_of=first.as_of+pd.Timedelta(minutes=5))
        fields['candle'] = replace(fields['candle'], ohlc=OHLC(101, 102, 1, 100))
        at = fields['as_of']
        context = envelope('consumed-context', 'CONTEXT', at,
                           (fields['pattern'], fields['candle'], fields['structure'],
                            fields['structure_candle'], fields['m6']), 1)
        risk = envelope('later-risk', 'RISK', at, (signal.signal_id,), 2)
        self.run.submit_frame(ReplayFrame('terminal-frame', at, (context, risk)))
        observed = self.run.advance()
        self.assertEqual(observed.command_id, 'consumed-context')
        self.assertEqual(len(self.run._actors.signals.events), 1)
        pending = self.run.journal.next_core()
        self.assertEqual(pending['kind'], 'TERMINAL')
        rebuilt = recover(self.path, self.specification)
        self.addCleanup(rebuilt.journal.close)
        self.assertEqual(rebuilt.journal.next_core()['kind'], 'TERMINAL')
        handoff = rebuilt.advance()
        self.assertEqual(handoff.kind, 'TERMINAL')
        self.assertEqual(len(rebuilt._actors.risk.terminal_history), 1)
        self.assertEqual(rebuilt.journal.next_core()['kind'], 'RISK')
        failed_input = rebuilt.advance()
        self.assertEqual((failed_input.status, failed_input.reason_codes),
                         ('INPUT_REJECTED', ('RISK_INPUT_INVALID',)))
        self.assertEqual(len(rebuilt._actors.risk.terminal_history), 1)
        self.assertFalse(rebuilt.orders)

    def test_approved_reservation_survives_unfinished_admission(self):
        at = approve(self.run)
        risk = self.run.advance()
        self.assertEqual(risk.kind, 'RISK')
        self.assertEqual(risk.result.status, 'APPROVED')
        self.assertEqual(self.run.journal.next_core()['kind'], 'ADMIT')
        self.assertEqual(len(self.run.portfolio(as_of=at).reservations), 1)
        rebuilt = recover(self.path, self.specification)
        self.addCleanup(rebuilt.journal.close)
        self.assertEqual(len(rebuilt.portfolio(as_of=at).reservations), 1)
        self.assertEqual(rebuilt.journal.next_core()['kind'], 'ADMIT')
        finish(rebuilt)
        self.assertEqual(len(rebuilt.orders), 1)
        self.assertEqual(len(rebuilt.portfolio(as_of=at).reservations), 1)

    def test_notification_mapping_failure_cannot_change_approved_core(self):
        at = approve(self.run)
        finish(self.run)
        core_before = (self.run.decisions, self.run.orders,
                       self.run.portfolio(as_of=at))
        with patch('trading.orchestration.notification_events', side_effect=ValueError('renderer')):
            self.assertEqual(self.run.drain_notifications(as_of=at+pd.Timedelta(seconds=1)), ())
        self.assertEqual(self.run.journal.job(self.run.journal.db.execute(
            'SELECT job_id FROM jobs ORDER BY seq LIMIT 1').fetchone()[0])['state'], 'FAILED')
        self.assertEqual((self.run.decisions, self.run.orders,
                          self.run.portfolio(as_of=at)), core_before)
        rebuilt = recover(self.path, self.specification)
        self.addCleanup(rebuilt.journal.close)
        self.assertEqual((rebuilt.decisions, rebuilt.orders,
                          rebuilt.portfolio(as_of=at)), core_before)

    def test_outbox_enqueue_failure_after_admission_is_isolated(self):
        at = approve(self.run)
        finish(self.run)
        core_before = (self.run.decisions, self.run.orders,
                       self.run.portfolio(as_of=at))
        with patch.object(self.run._actors.outbox, 'enqueue', side_effect=ValueError('render')):
            results = self.run.drain_notifications(as_of=at+pd.Timedelta(seconds=1))
        self.assertEqual(results[0].status, 'INPUT_REJECTED')
        self.assertEqual((self.run.decisions, self.run.orders,
                          self.run.portfolio(as_of=at)), core_before)
        rebuilt = recover(self.path, self.specification)
        self.addCleanup(rebuilt.journal.close)
        self.assertEqual((rebuilt.decisions, rebuilt.orders,
                          rebuilt.portfolio(as_of=at)), core_before)

    def test_distinct_cached_actor_calls_are_journaled_without_duplicate_effects(self):
        frame, _ = admission_frame()
        duplicate = replace(frame.commands[-1], command_id='duplicate-context', ordinal=4)
        self.run.submit_frame(replace(frame, commands=frame.commands+(duplicate,)))
        outcomes = finish(self.run)
        self.assertEqual(tuple(o.kind for o in outcomes), ('FX', 'MARK', 'CONTEXT', 'CONTEXT'))
        self.assertEqual(outcomes[-1].effect_ids, ())
        self.assertEqual(len(self.run.signals), 1)
        at = frame.as_of+pd.Timedelta(seconds=1)
        signal = self.run.signals[0]
        request = (signal.signal_id, spec(available_at=at), costs(available_at=at), 2, None)
        self.run.submit_frame(ReplayFrame('duplicate-risk', at, (
            envelope('first-risk', 'RISK', at, request, 1),
            envelope('second-risk', 'RISK', at, request, 2))))
        outcomes = finish(self.run)
        self.assertEqual(tuple(o.kind for o in outcomes), ('RISK', 'ADMIT', 'RISK'))
        self.assertEqual(outcomes[-1].effect_ids, ())
        self.assertEqual((len(self.run.decisions), len(self.run.orders)), (1, 1))
        rebuilt = recover(self.path, self.specification)
        self.addCleanup(rebuilt.journal.close)
        self.assertEqual(rebuilt.verify().state_digest, self.run.verify().state_digest)

    def test_derived_terminal_handoff_removal_fails_closed(self):
        first, _ = admission_frame()
        self.run.submit_frame(first)
        finish(self.run)
        fields = inputs(as_of=first.as_of+pd.Timedelta(minutes=5))
        fields['candle'] = replace(fields['candle'], ohlc=OHLC(101, 102, 1, 100))
        at = fields['as_of']
        command = envelope('consumed-context', 'CONTEXT', at,
                           (fields['pattern'], fields['candle'], fields['structure'],
                            fields['structure_candle'], fields['m6']), 1)
        self.run.submit_frame(ReplayFrame('terminal-frame', at, (command,)))
        self.run.advance()
        self.run.journal.db.execute("DELETE FROM commands WHERE kind='TERMINAL'")
        with self.assertRaises(JournalError):
            recover(self.path, self.specification)

    def test_crash_between_two_fill_notifications_keeps_first_and_replays_second(self):
        at = approve(self.run)
        finish(self.run)
        self.run.drain_notifications(as_of=at+pd.Timedelta(seconds=1))
        order = self.run.orders[0]
        fill_at = at+pd.Timedelta(seconds=2)
        fill = SimulationFill('entry-fixture', order.order_id, 'ENTRY',
                              order.quantity_contracts, order.entry_price,
                              order.risk.costs.entry_fee_rate, fill_at, fill_at, 'offline fixture')
        self.run.submit_frame(ReplayFrame('entry-fill', fill_at,
                              (envelope('fill-command', 'ENTRY_FILL', fill_at, fill, 1),)))
        finish(self.run)
        prior_count = len(self.run.outbox_entries)
        original = self.run.journal.apply
        calls = 0
        def stop_second(command_id, *args, **kwargs):
            nonlocal calls
            if self.run.journal.command(command_id)['kind'] == 'OUTBOX_ENQUEUE':
                calls += 1
                if calls == 2:
                    raise KeyboardInterrupt('simulated process crash')
            return original(command_id, *args, **kwargs)
        with patch.object(self.run.journal, 'apply', side_effect=stop_second):
            with self.assertRaises(KeyboardInterrupt):
                self.run.drain_notifications(as_of=fill_at+pd.Timedelta(seconds=1))
        self.assertIsNone(self.run._actors)
        rebuilt = recover(self.path, self.specification)
        self.addCleanup(rebuilt.journal.close)
        self.assertEqual(len(rebuilt.outbox_entries), prior_count+1)
        job = rebuilt.journal.next_job()
        self.assertEqual(job['next_view'], 1)
        self.assertEqual(job['state'], 'READY')
        rebuilt.drain_notifications(as_of=fill_at+pd.Timedelta(seconds=1))
        self.assertEqual(len(rebuilt.outbox_entries), prior_count+2)
        self.assertIsNone(rebuilt.journal.next_job())

    def test_direct_monitor_no_output_calls_replay_private_chronology(self):
        first, _ = admission_frame()
        self.run.submit_frame(first)
        finish(self.run)
        record = self.run.signals[0]
        at = first.as_of+pd.Timedelta(minutes=5)
        candle = ClosedCandle(first.as_of, OHLC(101, 102, 99, 100), record.symbol)
        commands = (envelope('monitor-1', 'MONITOR', at, (record.signal_id, candle), 1),
                    envelope('monitor-2', 'MONITOR', at, (record.signal_id, candle), 2))
        self.run.submit_frame(ReplayFrame('double-monitor', at, commands))
        outcomes = finish(self.run)
        self.assertEqual(tuple(o.result for o in outcomes), (None, None))
        self.assertEqual(tuple(o.effect_ids for o in outcomes), ((), ()))
        self.assertEqual(len(self.run.journal.applied()), 5)
        rebuilt = recover(self.path, self.specification)
        self.addCleanup(rebuilt.journal.close)
        self.assertEqual(rebuilt.verify().state_digest, self.run.verify().state_digest)

    def test_funding_precedes_explicit_exit_at_same_cutoff(self):
        at = approve(self.run)
        finish(self.run)
        order = self.run.orders[0]
        entry_at = at+pd.Timedelta(seconds=1)
        entry = SimulationFill('entry', order.order_id, 'ENTRY', order.quantity_contracts,
                               order.entry_price, 0, entry_at, entry_at, 'fixture')
        self.run.submit_frame(ReplayFrame('entry', entry_at,
                              (envelope('entry-callback', 'ENTRY_FILL', entry_at, entry, 1),
                               envelope('cached-entry-callback', 'ENTRY_FILL', entry_at, entry, 2))))
        entry_results = finish(self.run)
        self.assertEqual(entry_results[-1].effect_ids, ())
        exit_at = at+pd.Timedelta(seconds=2)
        exit_fill = SimulationFill('exit', order.order_id, 'EXIT', order.quantity_contracts,
                                   order.entry_price, 0, exit_at, exit_at, 'fixture')
        commands = (envelope('exit-callback', 'EXIT_FILL', exit_at, exit_fill, 1),
                    envelope('funding', 'FUNDING', exit_at, (order.allocation.allocation_id,
                                                            -1), 2),
                    envelope('new-mark', 'MARK', exit_at,
                             Quote('BTC/USDT', order.entry_price, exit_at, exit_at,
                                   'fixture'), 3))
        self.run.submit_frame(ReplayFrame('funding-before-exit', exit_at, commands))
        results = finish(self.run)
        self.assertEqual(tuple(value.kind for value in results),
                         ('MARK', 'FUNDING', 'EXIT_FILL'))
        self.assertEqual(self.run._actors.bridge.state(order.order_id), 'CLOSED')
        rebuilt = recover(self.path, self.specification)
        self.addCleanup(rebuilt.journal.close)
        self.assertEqual(rebuilt.verify().state_digest, self.run.verify().state_digest)

    def test_defensive_denied_admission_schedules_explicit_reservation_release(self):
        at = approve(self.run)
        result = self.run.advance()
        self.assertEqual(result.result.status, 'APPROVED')
        row = self.run.journal.next_core()
        self.assertEqual(row['kind'], 'ADMIT')
        denied = AdmissionReceipt('fixture-denial', at, 'DENIED',
                                  ('M9_SOURCE_DATA_STALE',), result.result, None)
        effects = {key: () for key in ('m7_records', 'm7_events', 'portfolio',
                                      'risk_decisions', 'risk_terminals', 'admissions',
                                      'bridge_events', 'outbox_transitions')}
        effects['admissions'] = (denied,)
        children, _ = self.run._children_and_jobs(row, effects)
        self.assertEqual(len(children), 1)
        self.assertEqual(children[0][1], 'RELEASE')
        self.assertEqual(children[0][2][0], result.result.allocation_id)

    def test_interrupted_denial_keeps_reserve_until_journaled_release(self):
        class DenyingBridge(FreqtradeSimulationBridge):
            def propose(self, decision, *, as_of):
                if decision.status not in ('APPROVED', 'REDUCED'):
                    return super().propose(decision, as_of=as_of)
                at = self._time(as_of)
                self._registered(decision)
                receipt = AdmissionReceipt(identity('fixture-denial', decision.decision_id),
                                           at, 'DENIED', ('M9_SOURCE_DATA_STALE',), decision, None)
                self._receipts.append(receipt)
                self._admitted[decision.decision_id] = receipt
                self._last_at = at
                return receipt

        with TemporaryDirectory(dir=Path.cwd()) as directory, patch(
                'trading.orchestration.FreqtradeSimulationBridge', DenyingBridge):
            path = Path(directory)/'denied.sqlite'
            run = OfflineOrchestrator.create(self.specification, path)
            try:
                at = approve(run)
                self.assertEqual(run.advance().result.status, 'APPROVED')
                self.assertEqual(run.advance().result.disposition, 'DENIED')
                self.assertEqual(run.journal.next_core()['kind'], 'RELEASE')
                self.assertEqual(len(run.portfolio(as_of=at).reservations), 1)
                rebuilt = recover(path, self.specification)
                try:
                    self.assertEqual(len(rebuilt.portfolio(as_of=at).reservations), 1)
                    self.assertEqual(rebuilt.advance().kind, 'RELEASE')
                    self.assertFalse(rebuilt.portfolio(as_of=at).reservations)
                    self.assertFalse(rebuilt.orders)
                    second = recover(path, self.specification)
                    try:
                        self.assertEqual(rebuilt.verify().state_digest,
                                         second.verify().state_digest)
                    finally:
                        second.journal.close()
                finally:
                    rebuilt.journal.close()
            finally:
                run.journal.close()


if __name__ == '__main__':
    unittest.main()
