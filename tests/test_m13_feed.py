"""Network-free public-feed boundary tests."""

import unittest
from unittest.mock import Mock

import pandas as pd

from trading.m13_feed import parse_klines
from m13_public_poll import PublicKlinePoller


AT = pd.Timestamp('2026-11-16T00:05:02Z')


def row(opened=0, close='100'):
    return [opened, '100', '110', '90', close, '12', opened + 299999,
            '0', 1, '0', '0', '0']


class FeedTests(unittest.TestCase):
    def test_finalized_and_stable_source_identity(self):
        candles = parse_klines('BTCUSDT', [row(), row(300000)], received_at=AT)
        self.assertEqual(len(candles), 2)
        self.assertEqual(candles[0].source_id, candles[1].source_id)
        self.assertEqual(candles[0].symbol, 'BTC/USDT')
        self.assertEqual(candles[0].received_at, AT)
        self.assertEqual(parse_klines('ETHUSDT', [row()], received_at=AT)[0].symbol, 'ETH/USDT')

    def test_unfinished_last_row_is_ignored(self):
        received = pd.Timestamp(300000, unit='ms', tz='UTC')
        self.assertEqual(len(parse_klines('BTCUSDT', [row(), row(300000)], received_at=received)), 1)

    def test_warmup_uses_close_time_and_rejects_future(self):
        received = pd.Timestamp(300000, unit='ms', tz='UTC')
        item = parse_klines('BTCUSDT', [row()], received_at=received, warmup=True)[0]
        self.assertEqual(item.received_at, received)
        with self.assertRaises(ValueError):
            parse_klines('BTCUSDT', [row(300000)], received_at=received, warmup=True)

    def test_duplicates_gaps_invalid_values_and_schema(self):
        for rows in ([row(), row()], [row(), row(600000)], [row(close='NaN')],
                     [row()[:-1]], [row(opened=1)], [row(close='200')]):
            with self.subTest(rows=rows), self.assertRaises(ValueError):
                parse_klines('BTCUSDT', rows, received_at=AT)

    def test_poller_retries_then_reports_missing_or_revised(self):
        bad = Mock(status_code=503)
        good = Mock(status_code=200, content=b'[]')
        good.json.return_value = [row()]
        session = Mock()
        session.get.side_effect = [bad, good]
        poller = PublicKlinePoller(session, sleeper=lambda _: None)
        poller.fetch('BTCUSDT', received_at=AT, warmup=True)
        self.assertEqual(session.get.call_count, 2)
        session.get.side_effect = None
        session.get.return_value = good
        good.json.return_value = [row(600000)]
        with self.assertRaisesRegex(ValueError, 'Missing public candle'):
            poller.fetch('BTCUSDT', received_at=AT, warmup=True)
        good.json.return_value = [row(close='101')]
        with self.assertRaisesRegex(ValueError, 'Revised public candle'):
            poller.fetch('BTCUSDT', received_at=AT, warmup=True)

    def test_late_live_candle_does_not_advance_poller(self):
        good = Mock(status_code=200, content=b'[]')
        good.json.return_value = [row()]
        poller = PublicKlinePoller(Mock(get=Mock(return_value=good)))
        with self.assertRaisesRegex(ValueError, 'Late public candle'):
            poller.fetch('BTCUSDT', received_at=AT)
        self.assertEqual(poller._last, {})

    def test_newly_closed_public_row_waits_for_finalization_grace(self):
        good = Mock(status_code=200, content=b'[]')
        good.json.return_value = [row()]
        poller = PublicKlinePoller(Mock(get=Mock(return_value=good)))
        close = pd.Timestamp(300000, unit='ms', tz='UTC')
        self.assertEqual(poller.fetch('BTCUSDT', received_at=close+pd.Timedelta(seconds=2)), ())
        self.assertEqual(poller._last, {})
        settled = close+pd.Timedelta(seconds=30)
        accepted = poller.fetch('BTCUSDT', received_at=settled)
        self.assertEqual(len(accepted), 1)
        self.assertEqual(accepted[0].received_at, settled)
        good.json.return_value = [row(close='101')]
        with self.assertRaisesRegex(ValueError, 'Revised public candle'):
            poller.fetch('BTCUSDT', received_at=settled+pd.Timedelta(seconds=1))

    def test_history_pages_are_exact_and_publish_only_when_complete(self):
        first = Mock(status_code=200, content=b'[]')
        first.json.return_value = [row(i * 300000) for i in range(1000)]
        second = Mock(status_code=200, content=b'[]')
        second.json.return_value = [row(i * 300000) for i in range(1000, 1002)]
        session = Mock()
        session.get.side_effect = [first, second]
        poller = PublicKlinePoller(session)
        history = poller.fetch_history('BTCUSDT', start_ms=0, count=1002, received_at=AT)
        self.assertEqual(len(history), 1002)
        self.assertEqual(history[-1].opened_at, pd.Timestamp(1001 * 300000, unit='ms', tz='UTC'))
        self.assertEqual(history[-1].received_at, history[-1].candle.closed_at)
        self.assertEqual(session.get.call_args_list[1].kwargs['params']['startTime'], 1000 * 300000)
        bad = Mock(status_code=200, content=b'[]')
        bad.json.return_value = [row(300000)]
        failed = PublicKlinePoller(Mock(get=Mock(return_value=bad)))
        with self.assertRaises(ValueError):
            failed.fetch_history('BTCUSDT', start_ms=0, count=1, received_at=AT)
        self.assertEqual(failed._last, {})


if __name__ == '__main__':
    unittest.main()
