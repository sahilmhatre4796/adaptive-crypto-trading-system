"""Local authenticated read-only command center. No trading route exists."""

import argparse
from http import HTTPStatus
from http.cookies import SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import hmac
import json
import os
from pathlib import Path
import secrets
import threading
import time
from urllib.parse import parse_qs, urlsplit

from trading.dashboard import DashboardReader
from trading.dashboard_exchange import ReadOnlyBinanceAccount


STATIC = Path(__file__).resolve().parent / 'dashboard'
COOKIE = 'dashboard_session'


def create_server(reader: DashboardReader, password: str, *, port=8765):
    if not isinstance(reader, DashboardReader) or not password or not 0 <= port <= 65535:
        raise ValueError('Read-only reader, password and local port required')
    sessions = {}
    lock = threading.Lock()

    class Handler(BaseHTTPRequestHandler):
        def _headers(self, code, content_type, length, *, cookie=None):
            self.send_response(code)
            self.send_header('Content-Type', content_type)
            self.send_header('Content-Length', str(length))
            self.send_header('Cache-Control', 'no-store')
            self.send_header('X-Content-Type-Options', 'nosniff')
            self.send_header('X-Frame-Options', 'DENY')
            self.send_header('Referrer-Policy', 'no-referrer')
            self.send_header('Content-Security-Policy', "default-src 'self'; connect-src 'self'; "
                             "script-src 'self'; style-src 'self'; img-src 'self' data:; "
                             "frame-ancestors 'none'")
            if cookie is not None:
                self.send_header('Set-Cookie', cookie)
            self.end_headers()

        def _json(self, value, status=HTTPStatus.OK, *, cookie=None):
            data = json.dumps(value, allow_nan=False, separators=(',', ':')).encode('utf-8')
            self._headers(status, 'application/json; charset=utf-8', len(data), cookie=cookie)
            self.wfile.write(data)

        def _authorized(self):
            cookies = SimpleCookie()
            try:
                cookies.load(self.headers.get('Cookie', ''))
                token = cookies[COOKIE].value if COOKIE in cookies else None
            except Exception:
                return False
            with lock:
                expiry = sessions.get(token)
                if expiry is None or expiry < time.monotonic():
                    sessions.pop(token, None)
                    return False
                return True

        def _host_ok(self):
            return self.headers.get('Host') in (f'127.0.0.1:{self.server.server_port}',
                                                f'localhost:{self.server.server_port}')

        def do_GET(self):
            if not self._host_ok():
                self._json({'error': 'Invalid local host'}, HTTPStatus.BAD_REQUEST)
                return
            route = urlsplit(self.path)
            if route.path == '/api/session':
                self._json({'authenticated': self._authorized()})
                return
            if route.path in ('/', '/app.js', '/style.css'):
                name = {'/': 'index.html', '/app.js': 'app.js', '/style.css': 'style.css'}[route.path]
                data = (STATIC / name).read_bytes()
                mime = {'index.html': 'text/html', 'app.js': 'text/javascript',
                        'style.css': 'text/css'}[name]
                self._headers(HTTPStatus.OK, mime + '; charset=utf-8', len(data))
                self.wfile.write(data)
                return
            if not self._authorized():
                self._json({'error': 'Authentication required'}, HTTPStatus.UNAUTHORIZED)
                return
            args = parse_qs(route.query)
            mode = args.get('mode', ['paper'])[0]
            try:
                if route.path == '/api/state':
                    value = reader.snapshot(mode)
                elif route.path == '/api/markets':
                    value = {'items': reader.snapshot(mode)['markets']}
                elif route.path == '/api/positions':
                    value = {'items': reader.snapshot(mode)['positions']}
                elif route.path == '/api/signals':
                    signals = reader.snapshot(mode)['signals']
                    asset = args.get('asset', [''])[0]
                    status = args.get('status', [''])[0]
                    if asset:
                        signals = [item for item in signals if item['symbol'].replace('/', '') == asset]
                    if status:
                        signals = [item for item in signals if item['lifecycle'] == status]
                    value = {'items': signals, 'total': len(signals)}
                elif route.path == '/api/risk':
                    value = reader.snapshot(mode)['risk']
                elif route.path == '/api/analytics':
                    state = reader.snapshot(mode)
                    value = {'metrics': state['analytics'], 'equity_curve': state['equity_curve'],
                             'daily_pnl': state['daily_pnl']}
                elif route.path == '/api/health':
                    value = reader.snapshot(mode)['health']
                elif route.path == '/api/trades':
                    value = reader.trades(mode=mode,
                        search=args.get('search', [''])[0], asset=args.get('asset', [''])[0],
                        side=args.get('side', [''])[0], result=args.get('result', [''])[0],
                        start=args.get('start', [''])[0], end=args.get('end', [''])[0],
                        page=int(args.get('page', ['1'])[0]),
                        page_size=int(args.get('page_size', ['25'])[0]))
                elif route.path.startswith('/api/trades/'):
                    trade_id = route.path.removeprefix('/api/trades/')
                    value = next((item for item in reader.snapshot(mode)['trades']
                                  if item['id'] == trade_id), None)
                    if value is None:
                        self._json({'error': 'Trade not found'}, HTTPStatus.NOT_FOUND)
                        return
                else:
                    self._json({'error': 'Not found'}, HTTPStatus.NOT_FOUND)
                    return
                self._json(value)
            except (ValueError, TypeError) as exc:
                self._json({'error': str(exc)}, HTTPStatus.BAD_REQUEST)
            except Exception:
                self._json({'error': 'Dashboard source unavailable'}, HTTPStatus.SERVICE_UNAVAILABLE)

        def do_POST(self):
            if not self._host_ok() or self.headers.get('Origin') not in (
                    None, f'http://127.0.0.1:{self.server.server_port}',
                    f'http://localhost:{self.server.server_port}'):
                self._json({'error': 'Invalid local origin'}, HTTPStatus.FORBIDDEN)
                return
            if self.path == '/api/logout':
                if not self._authorized():
                    self._json({'error': 'Authentication required'}, HTTPStatus.UNAUTHORIZED)
                    return
                cookies = SimpleCookie()
                cookies.load(self.headers.get('Cookie', ''))
                with lock:
                    sessions.pop(cookies[COOKIE].value, None)
                self._json({'authenticated': False}, cookie=
                           f'{COOKIE}=; Path=/; HttpOnly; SameSite=Strict; Max-Age=0')
                return
            if self.path != '/api/session':
                self._json({'error': 'Read-only dashboard'}, HTTPStatus.METHOD_NOT_ALLOWED)
                return
            try:
                size = int(self.headers.get('Content-Length', '0'))
                if not 0 < size <= 4096:
                    raise ValueError('Invalid login request size')
                body = json.loads(self.rfile.read(size))
                supplied = body.get('password') if isinstance(body, dict) else None
            except (ValueError, TypeError, json.JSONDecodeError):
                self._json({'error': 'Invalid login request'}, HTTPStatus.BAD_REQUEST)
                return
            if not isinstance(supplied, str) or not hmac.compare_digest(supplied, password):
                self._json({'error': 'Invalid credentials'}, HTTPStatus.UNAUTHORIZED)
                return
            token = secrets.token_urlsafe(32)
            with lock:
                sessions[token] = time.monotonic() + 8 * 3600
            self._json({'authenticated': True}, cookie=
                       f'{COOKIE}={token}; Path=/; HttpOnly; SameSite=Strict; Max-Age=28800')

    return ThreadingHTTPServer(('127.0.0.1', port), Handler)


def main():
    parser = argparse.ArgumentParser(description='Local read-only trading command center')
    parser.add_argument('--shadow-journal', type=Path)
    parser.add_argument('--port', type=int, default=8765)
    args = parser.parse_args()
    password = os.environ.get('DASHBOARD_PASSWORD')
    if not password:
        parser.error('Set DASHBOARD_PASSWORD in the local process environment')
    real = ReadOnlyBinanceAccount(os.environ.get('DASHBOARD_BINANCE_READONLY_KEY'),
                                  os.environ.get('DASHBOARD_BINANCE_READONLY_SECRET'))
    reader = DashboardReader(shadow_path=args.shadow_journal, real_account=real)
    server = create_server(reader, password, port=args.port)
    print(f'Dashboard listening on http://127.0.0.1:{args.port} (monitoring only)', flush=True)
    try:
        server.serve_forever(poll_interval=.5)
    finally:
        server.server_close()


if __name__ == '__main__':
    main()
