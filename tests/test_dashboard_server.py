"""Local dashboard API authentication, mode isolation and disabled write paths."""
import sys
sys.dont_write_bytecode = True

from http.cookiejar import CookieJar
import json
import threading
import unittest
from urllib.error import HTTPError
from urllib.request import HTTPCookieProcessor, Request, build_opener

from dashboard_server import create_server
from trading.dashboard import DashboardReader
from trading.dashboard_exchange import ReadOnlyBinanceAccount


class DashboardServerTests(unittest.TestCase):
    def setUp(self):
        self.server = create_server(DashboardReader(real_account=ReadOnlyBinanceAccount()),
                                    'local-test-password', port=0)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.base = f'http://127.0.0.1:{self.server.server_port}'
        self.opener = build_opener(HTTPCookieProcessor(CookieJar()))

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=3)

    def request(self, path, *, method='GET', body=None):
        data = json.dumps(body).encode() if body is not None else None
        request = Request(self.base + path, data=data, method=method,
                          headers={'Content-Type': 'application/json'} if data else {})
        return self.opener.open(request, timeout=3)

    def login(self):
        with self.request('/api/session', method='POST', body={'password': 'local-test-password'}) as response:
            self.assertTrue(json.load(response)['authenticated'])

    def test_login_required_and_static_ui_is_served(self):
        with self.request('/') as response:
            self.assertIn(b'Command Center', response.read())
        with self.assertRaises(HTTPError) as failure:
            self.request('/api/state')
        self.assertEqual(failure.exception.code, 401)
        self.login()
        with self.request('/api/state') as response:
            self.assertEqual(response.headers['Cache-Control'], 'no-store')
            state = json.load(response)
        self.assertEqual(state['schema_version'], 'dashboard-v1')
        self.assertIsNone(state['account'])

    def test_real_mode_is_read_only_and_never_copies_paper_balance(self):
        self.login()
        with self.request('/api/state?mode=real') as response:
            state = json.load(response)
        self.assertEqual(state['mode_label'], 'REAL ACCOUNT — READ ONLY')
        self.assertEqual(state['account_source'], 'DISCONNECTED')
        self.assertEqual(state['account_message'], 'Exchange account not connected')
        self.assertIsNone(state['account'])
        self.assertFalse(state['execution_enabled'])
        for path in ('/api/order', '/api/risk', '/api/mode'):
            with self.subTest(path=path), self.assertRaises(HTTPError) as failure:
                self.request(path, method='POST', body={'action': 'buy'})
            self.assertEqual(failure.exception.code, 405)

    def test_trade_filters_and_logout(self):
        self.login()
        with self.request('/api/trades?mode=paper&page=1&page_size=10') as response:
            self.assertEqual(json.load(response)['total'], 0)
        with self.request('/api/logout', method='POST') as response:
            self.assertFalse(json.load(response)['authenticated'])
        with self.assertRaises(HTTPError) as failure:
            self.request('/api/state')
        self.assertEqual(failure.exception.code, 401)


if __name__ == '__main__':
    unittest.main()
