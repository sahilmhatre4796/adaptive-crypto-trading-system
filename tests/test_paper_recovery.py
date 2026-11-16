"""Crash and corruption regressions for durable offline paper callbacks."""
import sys
sys.dont_write_bytecode = True

from contextlib import closing
from dataclasses import replace
from decimal import Decimal as D
from pathlib import Path
import shutil
import sqlite3
import unittest
from unittest.mock import patch
from uuid import uuid4

import pandas as pd

from trading.paper_journal import PaperJournalError
from trading.paper_recovery import DurablePaperExecution
from test_freqtrade_bridge import cancellation, fill, prepared
from test_portfolio import AT
from test_risk import costs


def baseline():
    return prepared(model=costs(entry_fee_rate='.001', exit_fee_rate='.002'))[1]


class DurablePaperExecutionTests(unittest.TestCase):
    def setUp(self):
        self.path = self._scratch() / 'paper.sqlite'

    def _scratch(self):
        root = Path.cwd().resolve()
        folder = root / ('m12-paper-test-' + uuid4().hex)
        folder.mkdir()
        def cleanup():
            if folder.resolve().parent != root:
                raise RuntimeError('Refusing to remove a test directory outside the workspace')
            shutil.rmtree(folder)
        self.addCleanup(cleanup)
        return folder

    def test_restart_after_admission_and_after_acknowledgement(self):
        run = DurablePaperExecution.create(self.path, baseline)
        receipt = run.bridge.receipts[0]
        self.assertFalse(run.adapter.history)
        run.close()
        run = DurablePaperExecution.recover(self.path, baseline)
        self.assertEqual(run.reconcile(as_of=AT).orders[0].state, 'PROPOSED')
        ack = run.acknowledge(receipt.receipt_id, event_id='ack', as_of=AT)
        first_history = run.bridge.risk.portfolio.history
        run.close()
        again = DurablePaperExecution.recover(self.path, baseline)
        self.assertEqual(again.adapter.history, (ack,))
        self.assertEqual(again.bridge.risk.portfolio.history, first_history)
        self.assertEqual(again.acknowledge(receipt.receipt_id, event_id='ack', as_of=AT), ack)
        self.assertEqual(len(again.adapter.history), 1)
        again.close()

    def test_restart_after_full_fill_and_repeat_recovery_do_not_double_book(self):
        run = DurablePaperExecution.create(self.path, baseline)
        receipt = run.bridge.receipts[0]
        order = receipt.order
        run.acknowledge(receipt.receipt_id, event_id='ack', as_of=AT)
        entry = fill(order)
        opened = run.fill(entry, as_of=AT)
        count = len(run.bridge.risk.portfolio.history)
        cash = run.bridge.risk.portfolio.snapshot(as_of=AT).cash
        run.close()
        for _ in range(2):
            run = DurablePaperExecution.recover(self.path, baseline)
            self.assertEqual(run.fill(entry, as_of=AT), opened)
            self.assertEqual(run.reconcile(as_of=AT).orders[0].state, 'OPEN')
            self.assertEqual(len(run.bridge.risk.portfolio.history), count)
            self.assertEqual(run.bridge.risk.portfolio.snapshot(as_of=AT).cash, cash)
            run.close()

    def test_restart_after_exit_fill_preserves_cash_and_closed_position(self):
        run = DurablePaperExecution.create(self.path, baseline)
        receipt = run.bridge.receipts[0]
        run.acknowledge(receipt.receipt_id, event_id='ack', as_of=AT)
        run.fill(fill(receipt.order), as_of=AT)
        exit_fill = fill(receipt.order, name='exit', phase='EXIT',
                         price=receipt.order.modeled_stop_execution_price,
                         fee=receipt.risk.costs.exit_fee_rate)
        closed = run.fill(exit_fill, as_of=AT)
        cash = run.bridge.risk.portfolio.snapshot(as_of=AT).cash
        count = len(run.bridge.risk.portfolio.history)
        run.close()
        recovered = DurablePaperExecution.recover(self.path, baseline)
        self.assertEqual(recovered.fill(exit_fill, as_of=AT), closed)
        self.assertEqual(recovered.reconcile(as_of=AT).orders[0].state, 'CLOSED')
        self.assertEqual(recovered.bridge.risk.portfolio.snapshot(as_of=AT).cash, cash)
        self.assertEqual(len(recovered.bridge.risk.portfolio.history), count)
        recovered.close()

    def test_uninterrupted_and_recovered_execution_are_identical(self):
        def sequence(path, interrupted):
            run = DurablePaperExecution.create(path, baseline)
            receipt = run.bridge.receipts[0]
            run.acknowledge(receipt.receipt_id, event_id='ack', as_of=AT)
            if interrupted:
                run.close()
                run = DurablePaperExecution.recover(path, baseline)
            run.fill(fill(receipt.order), as_of=AT)
            if interrupted:
                run.close()
                run = DurablePaperExecution.recover(path, baseline)
            run.fill(fill(receipt.order, name='exit', phase='EXIT',
                          price=receipt.order.modeled_stop_execution_price,
                          fee=receipt.risk.costs.exit_fee_rate), as_of=AT)
            result = (run.adapter.history, run.bridge.history,
                      run.bridge.risk.portfolio.history, run.reconcile(as_of=AT))
            run.close()
            return result
        uninterrupted = sequence(self.path, False)
        restarted = sequence(self._scratch() / 'restarted.sqlite', True)
        self.assertEqual(uninterrupted, restarted)

    def test_restart_after_cancel_and_rejection_preserves_one_reservation_release(self):
        for rejected in (False, True):
            with self.subTest(rejected=rejected):
                path = self._scratch() / 'paper.sqlite'
                run = DurablePaperExecution.create(path, baseline)
                receipt = run.bridge.receipts[0]
                run.acknowledge(receipt.receipt_id, event_id='ack', as_of=AT)
                request = cancellation(receipt.order, name='reject' if rejected else 'cancel')
                result = run.cancel(request, rejected=rejected, as_of=AT)
                count = len(run.bridge.risk.portfolio.history)
                run.close()
                recovered = DurablePaperExecution.recover(path, baseline)
                self.assertEqual(recovered.cancel(request, rejected=rejected, as_of=AT), result)
                self.assertEqual(len(recovered.bridge.risk.portfolio.history), count)
                self.assertFalse(recovered.bridge.risk.portfolio.snapshot(as_of=AT).reservations)
                self.assertEqual(recovered.reconcile(as_of=AT).orders[0].state, 'CANCELLED')
                recovered.close()

    def test_crash_after_pending_before_callback_replays_on_fresh_actors(self):
        run = DurablePaperExecution.create(self.path, baseline)
        receipt = run.bridge.receipts[0]
        with patch.object(run, '_invoke', side_effect=SystemExit('crash before callback')):
            with self.assertRaises(SystemExit):
                run.acknowledge(receipt.receipt_id, event_id='ack', as_of=AT)
        self.assertFalse(run.adapter.history)
        run.close()
        recovered = DurablePaperExecution.recover(self.path, baseline)
        self.assertEqual([r.kind for r in recovered.adapter.history], ['ACKNOWLEDGED'])
        self.assertEqual(recovered.journal.verify()[1][0][1], 'APPLIED')
        recovered.close()

    def test_crash_after_bridge_callback_before_commit_discards_old_graph(self):
        run = DurablePaperExecution.create(self.path, baseline)
        receipt = run.bridge.receipts[0]
        run.acknowledge(receipt.receipt_id, event_id='ack', as_of=AT)
        entry = fill(receipt.order)
        original_finish = run.journal.finish
        with patch.object(run.journal, 'finish', side_effect=SystemExit('crash before effects commit')):
            with self.assertRaises(SystemExit):
                run.fill(entry, as_of=AT)
        self.assertEqual(run.bridge.state(receipt.order.order_id), 'OPEN')
        self.assertEqual(len(run.bridge.risk.portfolio.snapshot(as_of=AT).positions), 1)
        self.assertEqual(len(run.journal.verify()[1]), 1)  # ACK applied; fill still PENDING.
        run.close()
        recovered = DurablePaperExecution.recover(self.path, baseline)
        self.assertEqual(recovered.bridge.state(receipt.order.order_id), 'OPEN')
        self.assertEqual(len(recovered.bridge.risk.portfolio.snapshot(as_of=AT).positions), 1)
        self.assertEqual(len([e for e in recovered.bridge.risk.portfolio.history
                              if e.kind == 'SIMULATED_OPEN']), 1)
        self.assertEqual(recovered.reconcile(as_of=AT).orders[0].state, 'OPEN')
        recovered.close()

    def test_crash_after_applied_commit_before_ack_returns_original_receipt(self):
        run = DurablePaperExecution.create(self.path, baseline)
        receipt = run.bridge.receipts[0]
        actual_finish = run.journal.finish
        def commit_then_crash(*args):
            actual_finish(*args)
            raise SystemExit('crash after effects commit')
        with patch.object(run.journal, 'finish', side_effect=commit_then_crash):
            with self.assertRaises(SystemExit):
                run.acknowledge(receipt.receipt_id, event_id='ack', as_of=AT)
        self.assertEqual(run.journal.verify()[1][0][1], 'APPLIED')
        run.close()
        recovered = DurablePaperExecution.recover(self.path, baseline)
        prior = recovered.adapter.history[0]
        self.assertIs(recovered.acknowledge(receipt.receipt_id, event_id='ack', as_of=AT), prior)
        self.assertEqual(len(recovered.adapter.history), 1)
        recovered.close()

    def test_crash_after_cancellation_callback_before_commit_releases_once(self):
        run = DurablePaperExecution.create(self.path, baseline)
        receipt = run.bridge.receipts[0]
        run.acknowledge(receipt.receipt_id, event_id='ack', as_of=AT)
        request = cancellation(receipt.order)
        with patch.object(run.journal, 'finish', side_effect=SystemExit('cancel commit interrupted')):
            with self.assertRaises(SystemExit):
                run.cancel(request, as_of=AT)
        self.assertFalse(run.bridge.risk.portfolio.snapshot(as_of=AT).reservations)
        run.close()
        recovered = DurablePaperExecution.recover(self.path, baseline)
        self.assertEqual(recovered.reconcile(as_of=AT).orders[0].state, 'CANCELLED')
        self.assertEqual(len([e for e in recovered.bridge.risk.portfolio.history
                              if e.kind == 'CANCEL']), 1)
        self.assertEqual(recovered.cancel(request, as_of=AT), recovered.adapter.history[-1])
        recovered.close()

    def test_quarantined_callback_and_unrecoverable_baseline_fail_closed(self):
        run = DurablePaperExecution.create(self.path, baseline)
        receipt = run.bridge.receipts[0]
        run.acknowledge(receipt.receipt_id, event_id='ack', as_of=AT)
        original = run.bridge.process_fill
        def mutate_then_fail(execution, *, as_of):
            original(execution, as_of=as_of)
            raise RuntimeError('interrupted callback')
        with patch.object(run.bridge, 'process_fill', side_effect=mutate_then_fail):
            with self.assertRaisesRegex(RuntimeError, 'interrupted callback'):
                run.fill(fill(receipt.order), as_of=AT)
        self.assertEqual(run.journal.verify()[1][-1][1], 'QUARANTINED')
        with self.assertRaisesRegex(PaperJournalError, 'quarantined'):
            run.acknowledge(receipt.receipt_id, event_id='other', as_of=AT)
        run.close()
        with self.assertRaisesRegex(PaperJournalError, 'Quarantined'):
            DurablePaperExecution.recover(self.path, baseline)

    def test_longer_applied_prefix_and_quarantined_tail_remain_verifiable(self):
        run = DurablePaperExecution.create(self.path, baseline)
        receipt = run.bridge.receipts[0]
        run.acknowledge(receipt.receipt_id, event_id='ack', as_of=AT)
        run.fill(fill(receipt.order), as_of=AT)
        run.fill(fill(receipt.order, name='exit', phase='EXIT',
                      price=receipt.order.modeled_stop_execution_price,
                      fee=receipt.risk.costs.exit_fee_rate), as_of=AT)
        before = (run.bridge.history, run.bridge.risk.portfolio.history)
        with self.assertRaisesRegex(ValueError, 'PROPOSED'):
            run.cancel(cancellation(receipt.order, name='invalid-late-cancel'), as_of=AT)
        commands, outcomes = run.journal.verify()
        self.assertEqual((len(commands), [row[1] for row in outcomes]),
                         (4, ['APPLIED', 'APPLIED', 'APPLIED', 'QUARANTINED']))
        self.assertEqual((run.bridge.history, run.bridge.risk.portfolio.history), before)
        run.close()
        with self.assertRaisesRegex(PaperJournalError, 'Quarantined'):
            DurablePaperExecution.recover(self.path, baseline)

    def test_failed_semantic_recovery_still_fences_previous_writer(self):
        run = DurablePaperExecution.create(self.path, baseline)
        receipt = run.bridge.receipts[0]
        run.acknowledge(receipt.receipt_id, event_id='ack', as_of=AT)
        with patch.object(DurablePaperExecution, '_replay',
                          side_effect=PaperJournalError('injected replay divergence')):
            with self.assertRaisesRegex(PaperJournalError, 'injected replay divergence'):
                DurablePaperExecution.recover(self.path, baseline)
        with self.assertRaisesRegex(PaperJournalError, 'epoch'):
            run.acknowledge(receipt.receipt_id, event_id='stale-ack', as_of=AT)
        run.close()

    def test_revised_duplicate_out_of_order_and_stale_writer_are_rejected(self):
        run = DurablePaperExecution.create(self.path, baseline)
        receipt = run.bridge.receipts[0]
        run.acknowledge(receipt.receipt_id, event_id='ack', as_of=AT)
        with self.assertRaisesRegex(PaperJournalError, 'revise'):
            run.acknowledge(receipt.receipt_id, event_id='ack', as_of=AT + pd.Timedelta(seconds=1))
        run.close()
        fresh = DurablePaperExecution.recover(self.path, baseline)
        with self.assertRaisesRegex(PaperJournalError, 'epoch'):
            stale = DurablePaperExecution.recover(self.path, baseline)
            fresh.acknowledge(receipt.receipt_id, event_id='ack', as_of=AT)
        stale.close()
        fresh.close()

    def test_out_of_order_callback_quarantines_without_booking(self):
        run = DurablePaperExecution.create(self.path, baseline)
        receipt = run.bridge.receipts[0]
        later = AT + pd.Timedelta(seconds=1)
        run.acknowledge(receipt.receipt_id, event_id='ack', as_of=later)
        before = run.bridge.risk.portfolio.history
        with self.assertRaisesRegex(ValueError, 'chronological'):
            run.fill(fill(receipt.order), as_of=AT)
        self.assertEqual(run.bridge.risk.portfolio.history, before)
        self.assertEqual(run.journal.verify()[1][-1][1], 'QUARANTINED')
        run.close()
        with self.assertRaises(PaperJournalError):
            DurablePaperExecution.recover(self.path, baseline)

    def test_corrupt_truncated_and_out_of_order_rows_are_rejected(self):
        for mutation in ('payload', 'truncate', 'sequence'):
            with self.subTest(mutation=mutation):
                path = self._scratch() / 'paper.sqlite'
                run = DurablePaperExecution.create(path, baseline)
                receipt = run.bridge.receipts[0]
                run.acknowledge(receipt.receipt_id, event_id='ack', as_of=AT)
                run.close()
                with closing(sqlite3.connect(path)) as db, db:
                    if mutation == 'payload':
                        db.execute("UPDATE commands SET payload='{}' WHERE seq=1")
                    elif mutation == 'truncate':
                        db.execute('DELETE FROM outcomes WHERE seq=1')
                        db.execute('DELETE FROM commands WHERE seq=1')
                    else:
                        db.execute('UPDATE commands SET seq=2 WHERE seq=1')
                with self.assertRaises(PaperJournalError):
                    DurablePaperExecution.recover(path, baseline)

    def test_code_or_factory_identity_mismatch_is_rejected(self):
        run = DurablePaperExecution.create(self.path, baseline)
        run.close()
        def changed_baseline():
            return prepared(model=costs(entry_fee_rate='.002', exit_fee_rate='.002'))[1]
        with self.assertRaises(PaperJournalError):
            DurablePaperExecution.recover(self.path, changed_baseline)
        with closing(sqlite3.connect(self.path)) as db, db:
            db.execute("UPDATE meta SET value='changed' WHERE key='code'")
        with self.assertRaises(PaperJournalError):
            DurablePaperExecution.recover(self.path, baseline)

    def test_sqlite_rollback_preserves_committed_prefix_and_invalid_file_fails(self):
        run = DurablePaperExecution.create(self.path, baseline)
        receipt = run.bridge.receipts[0]
        run.acknowledge(receipt.receipt_id, event_id='ack', as_of=AT)
        run.close()
        with closing(sqlite3.connect(self.path)) as db:
            db.execute('BEGIN IMMEDIATE')
            db.execute("UPDATE meta SET value='999' WHERE key='head'")
            db.rollback()
        recovered = DurablePaperExecution.recover(self.path, baseline)
        self.assertEqual(len(recovered.adapter.history), 1)
        recovered.close()
        broken = self._scratch() / 'broken.sqlite'
        broken.write_bytes(b'not a SQLite database')
        with self.assertRaises(PaperJournalError):
            DurablePaperExecution.recover(broken, baseline)


if __name__ == '__main__':
    unittest.main()
