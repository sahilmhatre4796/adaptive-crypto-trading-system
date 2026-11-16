"""Paired public-candle M7 shadow replay has no M8 or order actor."""

from dataclasses import replace
from pathlib import Path
from unittest.mock import patch
from uuid import uuid4
import unittest

import pandas as pd

from trading.candle_driver import CandleInput, WARMUP_5M_BARS
from trading.m13_feed import SOURCE_IDS
from trading.m13_shadow import PRIORITY, ShadowSession, shadow_drivers
from trading.paper_journal import PaperJournalError
from trading.patterns import OHLC
from trading.signals import CONTRADICTIONS
from m13_public_poll import PublicKlinePoller
from m13_shadow_poll import seed_shadow, advance_shadow


START = pd.Timestamp('2026-10-01T00:00:00Z')


def candle(symbol, index, *, receipt_delay=0):
    opened = START + pd.Timedelta(minutes=5 * index)
    return CandleInput(symbol, opened, OHLC(100, 101, 99, 100), 10.,
        opened + pd.Timedelta(minutes=5, seconds=receipt_delay),
        SOURCE_IDS[symbol.replace('/', '')])


class ShadowTests(unittest.TestCase):
    def setUp(self):
        self.path = Path.cwd() / ('m13-shadow-test-' + uuid4().hex + '.sqlite')
        def cleanup():
            for suffix in ('', '-wal', '-shm'):
                candidate = Path(str(self.path) + suffix)
                if candidate.is_file():
                    candidate.unlink()
        self.addCleanup(cleanup)

    def test_shadow_policy_is_explicit_and_cannot_admit_orders(self):
        drivers = shadow_drivers()
        self.assertEqual(set(drivers), set(PRIORITY))
        for driver in drivers.values():
            self.assertEqual(driver.signals.mode, 'CURRENT')
            self.assertTrue(driver.signal_config.require_directional_structure)
            self.assertEqual({rule.reason for rule in driver.signal_config.contradiction_rules},
                             set(CONTRADICTIONS))
            self.assertTrue(all(rule.action == 'BLOCK'
                                for rule in driver.signal_config.contradiction_rules))
            self.assertEqual(driver.signal_config.derivative_rules, ())
            self.assertEqual({cap.cap for cap in driver.signal_config.group_caps}, {0.})
            self.assertIsNone(driver.signal_config.signal_ttl_seconds)
            self.assertIn('UNAPPROVED-SHADOW', driver.signal_config.policy_version)

    def test_real_driver_seed_pair_replay_duplicate_and_uncertain_frame(self):
        batches = {symbol: tuple(candle(symbol, i) for i in range(WARMUP_5M_BARS))
                   for symbol in PRIORITY}
        cutoff = batches[PRIORITY[0]][-1].candle.closed_at
        session = ShadowSession.create(self.path)
        try:
            broken = dict(batches)
            damaged = list(batches[PRIORITY[0]])
            damaged[12] = damaged[13]
            broken[PRIORITY[0]] = tuple(damaged)
            with self.assertRaisesRegex(ValueError, 'gap'):
                session.seed_pair(broken, retrieved_at=cutoff+pd.Timedelta(minutes=1))
            self.assertEqual(session.journal.head, 0)
            class FixturePoller(PublicKlinePoller):
                def __init__(self):
                    super().__init__(session=object())
                def fetch_history(self, ticker, **kwargs):
                    items = batches[ticker.replace('USDT', '/USDT')]
                    self._last[ticker] = items[-1]
                    return items
                def fetch(self, ticker, **kwargs):
                    item = self.live_pair[ticker.replace('USDT', '/USDT')]
                    self._last[ticker] = item
                    return (item,)
            poller = FixturePoller()
            times = iter((cutoff+pd.Timedelta(minutes=1),
                          cutoff+pd.Timedelta(minutes=2)))
            seed = seed_shadow(session, poller, clock=lambda: next(times))
            self.assertEqual(seed['count'], WARMUP_5M_BARS)
            self.assertEqual(session.journal.head, 1)
            first = {s: candle(s, WARMUP_5M_BARS, receipt_delay=30) for s in PRIORITY}
            poller.live_pair = first
            result, = advance_shadow(session, poller)
            self.assertEqual(result['status'], 'UNAPPROVED_SHADOW')
            self.assertTrue(all(result['assets'][s]['warmup_ready'] for s in PRIORITY))
            self.assertEqual(session.observe_pair(first), result)
            self.assertEqual(session.journal.head, 2)
            self.assertFalse(hasattr(session, 'bridge'))
            second = {s: candle(s, WARMUP_5M_BARS + 1, receipt_delay=30)
                      for s in PRIORITY}
            poller.live_pair = second
            with patch.object(session.journal, 'finish', side_effect=RuntimeError('crash')):
                with self.assertRaisesRegex(RuntimeError, 'crash'):
                    advance_shadow(session, poller)
            self.assertTrue(session._dead)
            self.assertEqual(session.journal.head, 3)
        finally:
            session.close()
        replay = ShadowSession.recover(self.path)
        try:
            self.assertEqual(replay.journal.head, replay.journal.applied)
            self.assertEqual(replay.observe_pair(first), result)
            self.assertEqual(replay.journal.head, 3)
            self.assertEqual(replay.latest['BTC/USDT'].opened_at,
                             second['BTC/USDT'].opened_at)
            changed = dict(second)
            changed['BTC/USDT'] = replace(second['BTC/USDT'], volume=11.)
            with self.assertRaisesRegex(PaperJournalError, 'Revised'):
                replay.observe_pair(changed)
        finally:
            replay.close()


if __name__ == '__main__':
    unittest.main()
