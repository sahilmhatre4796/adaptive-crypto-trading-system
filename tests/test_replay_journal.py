import sys
sys.dont_write_bytecode = True

from dataclasses import replace
from pathlib import Path
import sqlite3
from tempfile import TemporaryDirectory
import unittest

import pandas as pd

from trading.orchestration import ReplayEnvelope, ReplayFrame
from trading.replay_codec import digest, encode
from trading.replay_journal import FencedWriter, JournalError, ReplayJournal
from test_replay_codec import run_spec
from test_signal_context import START


AT = START+pd.Timedelta(minutes=80)


def frame(name='frame', at=AT, count=1):
    commands = tuple(ReplayEnvelope(f'{name}-c{i}', 'MARK', 'fixture', at, at, at, i,
                                    {'sequence': i}) for i in range(count))
    return ReplayFrame(name, at, commands)


class JournalTests(unittest.TestCase):
    def setUp(self):
        self.temp = TemporaryDirectory(dir=Path.cwd())
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name)/'run.sqlite'
        self.spec = run_spec()
        self.j = ReplayJournal(self.path, manifest=self.spec, code_hash='code-v1', create=True)
        self.addCleanup(self.j.close)

    def test_atomic_seal_idempotency_and_strict_future_frames(self):
        first = frame(count=2)
        self.assertEqual(self.j.seal(first, [(c, f'{i:06d}') for i, c in enumerate(first.commands)]),
                         (1, 'SEALED'))
        self.assertEqual(self.j.seal(first, [(c, f'{i:06d}') for i, c in enumerate(first.commands)]),
                         (1, 'SEALED'))
        self.assertEqual(self.j.next_core()['command_id'], 'frame-c0')
        with self.assertRaises(JournalError):
            self.j.seal(replace(first, commands=first.commands[:1]), [(first.commands[0], '000000')])
        with self.assertRaises(JournalError):
            self.j.seal(frame('later', AT), [(frame('later', AT).commands[0], '000000')])
        self.j.verify_storage()

    def test_pending_then_applied_and_exact_chain_recovery(self):
        value = frame()
        self.j.seal(value, [(value.commands[0], '000001')])
        row = self.j.next_core()
        self.assertEqual(row['state'], 'PENDING')
        self.j.apply(row['command_id'], 'result', {'records': ()}, 'state')
        self.assertIsNone(self.j.next_core())
        self.assertEqual(self.j.command(row['command_id'])['state'], 'APPLIED')
        self.j.verify_storage()
        recovered = ReplayJournal(self.path, manifest=self.spec, code_hash='code-v1')
        self.addCleanup(recovered.close)
        self.assertEqual(len(recovered.applied()), 1)
        self.assertEqual(recovered.db.execute('SELECT state FROM frames').fetchone()[0], 'COMPLETE')

    def test_crash_after_pending_preserves_it_and_fences_stale_writer(self):
        value = frame()
        self.j.seal(value, [(value.commands[0], '000001')])
        second = ReplayJournal(self.path, manifest=self.spec, code_hash='code-v1')
        self.addCleanup(second.close)
        self.assertEqual(second.next_core()['state'], 'PENDING')
        with self.assertRaises(FencedWriter):
            self.j.apply(value.commands[0].command_id, 'wrong', {}, 'wrong')
        second.apply(value.commands[0].command_id, 'right', {}, 'state')
        self.assertEqual(len(second.applied()), 1)

    def test_partial_frame_submission_rolls_back_every_root(self):
        self.j.db.execute("CREATE TRIGGER abort_second BEFORE INSERT ON commands "
                          "WHEN NEW.command_id='frame-c1' BEGIN SELECT RAISE(ABORT,'injected'); END")
        value = frame(count=2)
        with self.assertRaises(sqlite3.DatabaseError):
            self.j.seal(value, [(c, f'{i:06d}') for i, c in enumerate(value.commands)])
        self.assertEqual(self.j.db.execute('SELECT COUNT(*) FROM frames').fetchone()[0], 0)
        self.assertEqual(self.j.db.execute('SELECT COUNT(*) FROM commands').fetchone()[0], 0)
        self.j.verify_storage()

    def test_changed_payload_and_corruption_fail_closed(self):
        value = frame()
        self.j.seal(value, [(value.commands[0], '000001')])
        self.j.db.execute("UPDATE commands SET payload=? WHERE command_id=?",
                          (encode({'changed': True}), value.commands[0].command_id))
        with self.assertRaises(JournalError):
            self.j.verify_storage()
        with self.assertRaises(JournalError):
            ReplayJournal(self.path, manifest=self.spec, code_hash='code-v1')

    def test_missing_payload_and_broken_chain_fail_closed(self):
        value = frame()
        self.j.seal(value, [(value.commands[0], '000001')])
        self.j.apply(value.commands[0].command_id, 'ok', {}, 'state')
        self.j.db.execute("UPDATE applied_log SET entry_hash='wrong'")
        with self.assertRaises(JournalError):
            self.j.verify_storage()

    def test_missing_root_and_rehashed_changed_root_fail_closed(self):
        value = frame(count=2)
        self.j.seal(value, [(c, f'{i:06d}') for i, c in enumerate(value.commands)])
        self.j.db.execute('DELETE FROM commands WHERE command_id=?', (value.commands[1].command_id,))
        with self.assertRaises(JournalError):
            self.j.verify_storage()
        self.j.db.execute('INSERT INTO commands '
                          '(command_id,frame_id,parent_id,order_key,kind,at_ns,payload,payload_hash,state) '
                          'VALUES (?,?,?,?,?,?,?,?,?)',
                          (value.commands[1].command_id, value.frame_id, None, '000001', 'MARK',
                           AT.value, encode({'forged': True}), digest(encode({'forged': True})), 'PENDING'))
        with self.assertRaises(JournalError):
            self.j.verify_storage()

    def test_command_receipt_rejects_changed_payload_and_keeps_original(self):
        at = AT
        self.assertEqual(self.j.add_direct('direct', 'OUTBOX_DELIVERY', at, ('notice', 'START')),
                         'PENDING')
        self.assertEqual(self.j.add_direct('direct', 'OUTBOX_DELIVERY', at, ('notice', 'START')),
                         'PENDING')
        with self.assertRaises(JournalError):
            self.j.add_direct('direct', 'OUTBOX_DELIVERY', at, ('notice', 'SUCCEED'))
        self.assertEqual(self.j.command('direct')['payload'], encode(('notice', 'START')))

    def test_processed_frame_cannot_be_extended_and_late_input_needs_later_cutoff(self):
        value = frame()
        self.j.seal(value, [(value.commands[0], '000001')])
        self.j.apply(value.commands[0].command_id, 'done', {}, 'state')
        with self.assertRaises(JournalError):
            self.j.seal(frame('late-same-time'), [(frame('late-same-time').commands[0], '000001')])
        later = frame('late', AT+pd.Timedelta(seconds=1))
        self.assertEqual(self.j.seal(later, [(later.commands[0], '000001')]), (2, 'SEALED'))
        changed = replace(value.commands[0], payload={'sequence': 2})
        with self.assertRaises(JournalError):
            self.j.seal(replace(value, commands=(changed,)), [(changed, '000001')])

    def test_unlogged_applied_command_is_rejected_on_recovery(self):
        value = frame()
        self.j.seal(value, [(value.commands[0], '000001')])
        self.j.db.execute("UPDATE commands SET state='APPLIED',outcome=?,effects=?,state_digest=?,applied_seq=?",
                          (encode('forged'), encode({}), 'forged', 99))
        with self.assertRaises(JournalError):
            self.j.verify_storage()

    def test_expected_head_compare_and_swap_rejects_stale_write(self):
        self.j.db.execute("UPDATE meta SET value='99' WHERE key='head'")
        value = frame()
        with self.assertRaises(FencedWriter):
            self.j.seal(value, [(value.commands[0], '000001')])
        self.assertEqual(self.j.db.execute('SELECT COUNT(*) FROM frames').fetchone()[0], 0)

    def test_physically_corrupt_sqlite_file_refuses_recovery(self):
        self.j.close()
        self.path.write_bytes(b'not-a-sqlite-database')
        with self.assertRaises((sqlite3.DatabaseError, JournalError)):
            ReplayJournal(self.path, manifest=self.spec, code_hash='code-v1')

    def test_missing_command_payload_fails_closed_even_with_rehashed_bytes(self):
        self.j.add_direct('direct', 'OUTBOX_DELIVERY', AT, ('notice', 'START'))
        self.j.db.execute('UPDATE commands SET payload=?,payload_hash=? WHERE command_id=?',
                          ('', digest(''), 'direct'))
        with self.assertRaises(JournalError):
            self.j.verify_storage()

    def test_core_frame_cannot_backdate_after_committed_outbox_work(self):
        later = AT+pd.Timedelta(seconds=5)
        self.j.add_direct('notice', 'OUTBOX_DELIVERY', later, ('notice', 'START'))
        self.j.apply('notice', 'done', {}, 'state')
        old = frame('old', AT+pd.Timedelta(seconds=4))
        with self.assertRaises(JournalError):
            self.j.seal(old, [(old.commands[0], '000001')])
        fresh = frame('fresh', later+pd.Timedelta(seconds=1))
        self.assertEqual(self.j.seal(fresh, [(fresh.commands[0], '000001')]),
                         (1, 'SEALED'))

    def test_code_and_configuration_mismatch_rejected(self):
        with self.assertRaises(JournalError):
            ReplayJournal(self.path, manifest=self.spec, code_hash='different')
        with self.assertRaises(JournalError):
            ReplayJournal(self.path, manifest=run_spec(priority=('ETH/USDT', 'BTC/USDT')),
                          code_hash='code-v1')


if __name__ == '__main__':
    unittest.main()
