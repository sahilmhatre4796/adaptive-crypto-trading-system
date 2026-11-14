import sys
sys.dont_write_bytecode = True

from dataclasses import replace
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

import pandas as pd

from trading.orchestration import (OfflineOrchestrator, ReplayEnvelope, ReplayFrame,
                                   recover)
from trading.portfolio import Quote
from trading.replay_journal import JournalError
from test_portfolio import spec
from test_replay_codec import run_spec
from test_risk import costs
from test_signal_context import inputs


def envelope(name, kind, at, payload, ordinal, observed=None):
    observed = at if observed is None else observed
    return ReplayEnvelope(name, kind, 'deterministic-fixture', observed, at, at, ordinal, payload)


def admission_frame(symbol='BTC/USDT', *, name='initial'):
    fields = inputs(symbol)
    at = fields['as_of']
    commands = (
        envelope(name+'-fx', 'FX', at, Quote('USDT', 1, at, at, 'fixture'), 1),
        envelope(name+'-mark', 'MARK', at, Quote(symbol, fields['pattern'].confirmation_close,
                                                  at, at, 'fixture'), 2),
        envelope(name+'-context', 'CONTEXT', at,
                 (fields['pattern'], fields['candle'], fields['structure'],
                  fields['structure_candle'], fields['m6']), 3),
    )
    return ReplayFrame(name, at, commands), fields


class OrchestrationTests(unittest.TestCase):
    def setUp(self):
        self.temp = TemporaryDirectory(dir=Path.cwd())
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name)/'run.sqlite'
        self.spec = run_spec()
        self.run = OfflineOrchestrator.create(self.spec, self.path)
        self.addCleanup(lambda: self.run.journal.close())

    def test_context_qualification_risk_admission_and_recovery(self):
        frame, _ = admission_frame()
        self.run.submit_frame(frame)
        while self.run.advance() is not None:
            pass
        self.assertTrue(self.run.signals[0].qualification_passed)
        self.assertEqual(len(self.run.signals), 1)
        at = frame.as_of+pd.Timedelta(seconds=1)
        signal = self.run.signals[0]
        risk = ReplayFrame('risk', at, (envelope('risk-btc', 'RISK', at,
                           (signal.signal_id, spec(available_at=at),
                            costs(available_at=at), 2, None), 1),))
        self.run.submit_frame(risk)
        while self.run.advance() is not None:
            pass
        self.assertEqual(len(self.run.decisions), 1)
        self.assertEqual(self.run.decisions[0].status, 'APPROVED',
                         self.run.decisions[0].reason_codes)
        self.assertEqual(len(self.run.orders), 1)
        self.assertEqual(self.run.admissions[0].disposition, 'ACCEPTED')
        baseline = self.run.verify()
        rebuilt = recover(self.path, self.spec)
        self.addCleanup(rebuilt.journal.close)
        self.assertEqual(rebuilt.verify().state_digest, baseline.state_digest)
        self.assertEqual(rebuilt.orders, self.run.orders)
        self.assertEqual(rebuilt.decisions, self.run.decisions)

    def test_duplicate_seal_changed_payload_and_pending_frame_barrier(self):
        frame, _ = admission_frame()
        first = self.run.submit_frame(frame)
        self.assertEqual(self.run.submit_frame(frame), first)
        with self.assertRaises(JournalError):
            self.run.submit_frame(replace(frame, commands=frame.commands[:2]))

    def test_delayed_notification_fifo_and_in_flight_recovery(self):
        frame, _ = admission_frame()
        self.run.submit_frame(frame)
        while self.run.advance() is not None:
            pass
        at = frame.as_of+pd.Timedelta(seconds=1)
        signal = self.run.signals[0]
        self.run.submit_frame(ReplayFrame('risk', at, (envelope('risk-btc', 'RISK', at,
                              (signal.signal_id, spec(available_at=at),
                               costs(available_at=at), 2, None), 1),)))
        while self.run.advance() is not None:
            pass
        latest = at+pd.Timedelta(seconds=10)
        self.run.submit_frame(ReplayFrame('newer-core', latest, (envelope('new-mark', 'MARK',
                              latest, Quote('BTC/USDT', 101, latest, latest, 'fixture'), 1),)))
        while self.run.advance() is not None:
            pass
        notices = self.run.drain_notifications(as_of=latest+pd.Timedelta(seconds=1))
        self.assertGreaterEqual(len(notices), 3)
        self.assertEqual(len(self.run.outbox_entries), len(notices))
        self.assertLess(self.run.outbox_entries[0].notification.source_available_at,
                        self.run.outbox_entries[0].enqueued_at)
        notice_id = self.run.outbox_entries[0].notification.event_id
        started = self.run.simulate_delivery(notice_id, 'START', command_id='delivery-start',
                                             operation_id='start',
                                             as_of=latest+pd.Timedelta(seconds=2))
        self.assertEqual(started.result.state, 'IN_FLIGHT')
        cached = self.run.simulate_delivery(notice_id, 'START', command_id='delivery-cached',
                                            operation_id='start',
                                            as_of=latest+pd.Timedelta(seconds=3))
        self.assertEqual(cached.effect_ids, ())
        rebuilt = recover(self.path, self.spec)
        self.addCleanup(rebuilt.journal.close)
        self.assertEqual(rebuilt.outbox_entries, self.run.outbox_entries)
        self.assertEqual(rebuilt.outbox_entries[0].state, 'IN_FLIGHT')
        self.assertEqual(rebuilt.verify().state_digest, self.run.verify().state_digest)

    def test_m8_approval_is_the_only_order_admission_boundary(self):
        frame, _ = admission_frame()
        with self.assertRaises(ValueError):
            envelope('forged-admission', 'ADMIT', frame.as_of, 'fake-decision', 1)
        with self.assertRaises(ValueError):
            envelope('forged-order', 'ORDER', frame.as_of, {'status': 'APPROVED'}, 1)
        self.run.submit_frame(frame)
        while self.run.advance() is not None:
            pass
        self.assertFalse(self.run.orders)
        at = frame.as_of+pd.Timedelta(seconds=1)
        self.run.submit_frame(ReplayFrame('bad-risk', at, (envelope('bad-risk-command',
                              'RISK', at, ('fake-approved',), 1),)))
        outcome = self.run.advance()
        self.assertEqual(outcome.status, 'INPUT_REJECTED')
        self.assertEqual(outcome.reason_codes, ('RISK_INPUT_INVALID',))
        self.assertFalse(self.run.orders)
        self.assertFalse(self.run.decisions)

    def test_future_frame_mutation_cannot_change_committed_prefix(self):
        first, _ = admission_frame()
        self.run.submit_frame(first)
        while self.run.advance() is not None:
            pass
        prefix = (self.run.signals, self.run.verify().state_digest)
        at = first.as_of+pd.Timedelta(seconds=1)
        future = ReplayFrame('future', at, (envelope('future-mark', 'MARK', at,
                             Quote('BTC/USDT', 200, at, at, 'fixture'), 1),))
        changed = replace(future, commands=(replace(future.commands[0], payload=Quote(
                          'BTC/USDT', 300, at, at, 'fixture')),))
        self.run.submit_frame(future)
        with self.assertRaises(JournalError):
            self.run.submit_frame(changed)
        rebuilt = recover(self.path, self.spec)
        self.addCleanup(rebuilt.journal.close)
        self.assertEqual((rebuilt.signals, rebuilt.verify().state_digest), prefix)

    def test_stale_m8_input_produces_denied_receipt_without_order(self):
        frame, _ = admission_frame()
        self.run.submit_frame(frame)
        while self.run.advance() is not None:
            pass
        at = frame.as_of+pd.Timedelta(seconds=1)
        signal = self.run.signals[0]
        stale = spec(available_at=self.spec.initial_at)
        self.run.submit_frame(ReplayFrame('stale-risk', at, (envelope('stale-risk-request',
                              'RISK', at, (signal.signal_id, stale,
                                           costs(available_at=at), 2, None), 1),)))
        while self.run.advance() is not None:
            pass
        self.assertEqual(self.run.decisions[0].status, 'REJECTED')
        self.assertIn('SPEC_STALE', self.run.decisions[0].reason_codes)
        self.assertEqual(self.run.admissions[0].disposition, 'DENIED')
        self.assertFalse(self.run.orders)
        self.assertFalse(self.run.portfolio(as_of=at).reservations)


if __name__ == '__main__':
    unittest.main()
