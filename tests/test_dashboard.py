"""Dashboard projections use authoritative actors and never admit orders."""
import sys
sys.dont_write_bytecode = True

from decimal import Decimal
import json
from pathlib import Path
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from trading.dashboard import DashboardReader, ShadowJournalView, _paper
from trading.dashboard_exchange import ACCOUNT_URL, ReadOnlyBinanceAccount
from trading.exit_funding import ExitFundingController
from trading.m13_runtime_journal import source_hash
from trading.replay_codec import encode
from test_freqtrade_bridge import AT, fill, prepared


def runtime_fixture():
    risk, bridge, receipt = prepared()
    runtime = SimpleNamespace(journal=SimpleNamespace(head=0, applied=0),
                              _dead=False, _open_frame=None, bridge=bridge,
                              controller=ExitFundingController(bridge), drivers={})
    return runtime, receipt.order


class PaperProjectionTests(unittest.TestCase):
    def test_no_runtime_never_invents_initial_cash(self):
        state = DashboardReader().snapshot()
        self.assertEqual(state['account_source'], 'UNAVAILABLE')
        self.assertIsNone(state['account'])
        self.assertEqual(state['trades'], [])

    def test_open_and_closed_trade_reconcile_to_m8(self):
        runtime, order = runtime_fixture()
        bridge = runtime.bridge
        bridge.process_fill(fill(order), as_of=AT)
        opened = _paper(runtime)
        snap = bridge.risk.portfolio.snapshot(as_of=AT)
        self.assertEqual(Decimal(opened['account']['equity']), snap.equity)
        self.assertEqual(Decimal(opened['positions'][0]['unrealized_pnl']), snap.unrealized_pnl)
        self.assertEqual(opened['positions'][0]['status'], 'OPEN')
        self.assertEqual(Decimal(opened['risk']['risk_fraction']), Decimal('.10'))
        bridge.process_fill(fill(order, name='exit', phase='EXIT', price=105, fee=0), as_of=AT)
        closed = _paper(runtime)
        self.assertEqual(closed['positions'], [])
        self.assertEqual(Decimal(closed['trades'][0]['net_pnl']), Decimal('100'))
        self.assertEqual(Decimal(closed['trades'][0]['net_pnl']),
                         bridge.risk.portfolio.snapshot(as_of=AT).realized_pnl)
        self.assertEqual(closed['analytics']['total_trades'], 1)
        self.assertEqual(closed['analytics']['win_rate_pct'], 100)

    def test_unfinished_actor_is_not_exposed(self):
        runtime, _ = runtime_fixture()
        runtime.journal.head = 1
        state = DashboardReader(paper_runtime=runtime).snapshot()
        self.assertEqual(state['account_source'], 'ERROR')
        self.assertIsNone(state['account'])

    def test_search_filters_and_pagination_use_closed_m8_trades(self):
        runtime, order = runtime_fixture()
        runtime.bridge.process_fill(fill(order), as_of=AT)
        runtime.bridge.process_fill(fill(order, name='exit', phase='EXIT', price=105, fee=0), as_of=AT)
        reader = DashboardReader(paper_runtime=runtime)
        found = reader.trades(search=order.order_id[:12], asset='BTCUSDT', side='LONG',
                              result='profit', page_size=1)
        self.assertEqual((found['total'], found['items'][0]['id']), (1, order.order_id))
        self.assertEqual(reader.trades(result='loss')['total'], 0)
        self.assertEqual(reader.trades(mode='real')['total'], 0)


class ShadowProjectionTests(unittest.TestCase):
    def test_committed_journal_read_is_read_only_and_fails_closed_on_revision(self):
        class Database:
            code = source_hash()
            queries = []
            def execute(self, sql, args=()):
                self.queries.append(sql)
                if sql == 'SELECT key,value FROM meta':
                    return [('code', self.code), ('head', '1'), ('applied', '1')]
                if sql == 'PRAGMA quick_check':
                    return SimpleNamespace(fetchone=lambda: ('ok',))
                if sql.startswith('SELECT c.seq'):
                    return [(1, 'SEED_PAIR', encode({'BTC/USDT': (), 'ETH/USDT': ()}),
                             'APPLIED', encode({'status': 'SHADOW_SEEDED'}))]
                if sql == 'PRAGMA query_only=ON':
                    return None
                raise AssertionError('Unexpected dashboard SQL: ' + sql)
            def close(self):
                pass
        db = Database()
        view = ShadowJournalView('unused-shadow.sqlite')
        with patch.object(Path, 'is_file', return_value=True), patch(
                'trading.dashboard.sqlite3.connect', return_value=db) as connect:
            self.assertEqual(view.read()['health']['status'], 'OK')
            self.assertIn('mode=ro', connect.call_args.args[0])
            self.assertTrue(all(query.startswith(('SELECT', 'PRAGMA')) for query in db.queries))
            db.code = 'bad-source'
            corrupted = view.read()
            self.assertEqual(corrupted['health']['status'], 'ERROR')
            self.assertIsNone(corrupted['markets'][0]['price'])


class RealAccountTests(unittest.TestCase):
    def test_disconnected_real_account_has_no_fabricated_balance(self):
        state = DashboardReader(real_account=ReadOnlyBinanceAccount()).snapshot('real')
        self.assertEqual(state['account_message'], 'Exchange account not connected')
        self.assertIsNone(state['account'])
        self.assertEqual(state['positions'], [])
        self.assertFalse(state['execution_enabled'])

    def test_signed_get_only_and_hedge_short_is_not_mislabeled(self):
        calls = []
        def transport(url, headers):
            calls.append((url, headers))
            return {'assets': [{'asset': 'USDT', 'walletBalance': '123',
                                'availableBalance': '100', 'marginBalance': '120',
                                'unrealizedProfit': '-3'}],
                    'positions': [{'symbol': 'ETHUSDT', 'positionAmt': '2',
                                   'positionSide': 'SHORT', 'entryPrice': '2500',
                                   'unrealizedProfit': '-3'}]}
        monitor = ReadOnlyBinanceAccount('public-test-key', 'test-secret',
                                          transport=transport, clock=lambda: 100.)
        result = monitor.read()
        self.assertEqual(result['account']['wallet_balance'], '123')
        self.assertEqual(result['positions'][0]['side'], 'SHORT')
        self.assertEqual(len(calls), 1)
        self.assertTrue(calls[0][0].startswith(ACCOUNT_URL + '?'))
        self.assertEqual(calls[0][1], {'X-MBX-APIKEY': 'public-test-key'})
        self.assertNotIn('test-secret', json.dumps(result))
        self.assertEqual(monitor.read(), result)
        self.assertEqual(len(calls), 1)

    def test_malformed_exchange_data_is_unavailable_not_zero(self):
        monitor = ReadOnlyBinanceAccount('key', 'secret',
            transport=lambda *_: {'assets': [{'asset': 'USDT', 'walletBalance': 'NaN'}],
                                  'positions': []}, clock=lambda: 100.)
        self.assertEqual(monitor.read()['status'], 'ERROR')
        self.assertIsNone(monitor.read()['account'])


if __name__ == '__main__':
    unittest.main()
