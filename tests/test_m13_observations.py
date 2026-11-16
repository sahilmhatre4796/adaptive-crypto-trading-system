"""Observed public candles are durable but never imply approved paper trades."""
import sqlite3
import unittest
from dataclasses import replace
from pathlib import Path
from uuid import uuid4

from trading.m13_observations import PublicObservationSession
from trading.m13_feed import SOURCE_IDS
from trading.paper_journal import PaperJournalError
from test_candle_driver import candle


def public_candle(index, **kwargs):
    item = candle(index, **kwargs)
    return replace(item, source_id=SOURCE_IDS[item.symbol.replace('/', '')])


class ObservationTests(unittest.TestCase):
    def setUp(self):
        self.path = Path.cwd() / ('m13-observe-test-' + uuid4().hex + '.sqlite')
        def cleanup():
            for suffix in ('', '-wal', '-shm'):
                path = Path(str(self.path) + suffix)
                if path.is_file():
                    path.unlink()
        self.addCleanup(cleanup)

    def test_fenced_durable_btc_eth_priority_and_duplicate(self):
        run = PublicObservationSession.create(self.path)
        btc = public_candle(0)
        eth = public_candle(0, symbol='ETH/USDT')
        first = run.observe(btc)
        self.assertEqual(first['status'], 'OBSERVED_ONLY')
        self.assertEqual(run.observe(btc), first)
        run.observe(eth)
        self.assertEqual(run.journal.head, 2)
        run.close()
        again = PublicObservationSession.recover(self.path)
        self.assertEqual(again.latest, {'BTC/USDT': btc, 'ETH/USDT': eth})
        with self.assertRaises(PaperJournalError):
            again.observe(public_candle(0, delay=1))
        again.close()

    def test_incomplete_and_corrupt_journal_fail_closed(self):
        run = PublicObservationSession.create(self.path)
        run.observe(public_candle(0))
        run.close()
        db = sqlite3.connect(self.path)
        try:
            db.execute("UPDATE outcomes SET post_hash=? WHERE seq=1", ('0'*64,))
            db.commit()
        finally:
            db.close()
        with self.assertRaises(PaperJournalError):
            PublicObservationSession.recover(self.path)

    def test_nonpublic_source_cannot_be_journaled(self):
        run = PublicObservationSession.create(self.path)
        with self.assertRaisesRegex(ValueError, 'source identity'):
            run.observe(candle(0))
        self.assertEqual(run.journal.head, 0)
        run.close()


if __name__ == '__main__':
    unittest.main()
