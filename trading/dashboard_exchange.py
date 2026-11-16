"""Optional read-only USD-M account snapshot. No trading endpoints or order methods."""

from decimal import Decimal, InvalidOperation
from hashlib import sha256
from hmac import new as hmac_new
import json
import threading
import time
from urllib.parse import urlencode
from urllib.request import Request, urlopen


ACCOUNT_URL = 'https://fapi.binance.com/fapi/v2/account'


def _decimal(value):
    if not isinstance(value, str):
        raise ValueError('Exchange amount must be a decimal string')
    try:
        result = Decimal(value)
    except InvalidOperation as exc:
        raise ValueError('Invalid exchange amount') from exc
    if not result.is_finite():
        raise ValueError('Nonfinite exchange amount')
    return str(result)


def _get(url, headers):
    request = Request(url, headers=headers, method='GET')
    with urlopen(request, timeout=5) as response:
        raw = response.read(1_000_001)
    if len(raw) > 1_000_000:
        raise ValueError('Exchange response exceeds limit')
    return json.loads(raw)


class ReadOnlyBinanceAccount:
    """Fetch one signed USER_DATA GET; credentials never enter dashboard responses."""

    def __init__(self, key=None, secret=None, *, transport=_get, clock=time.time):
        self.key, self.secret = key, secret
        self.transport, self.clock = transport, clock
        self._lock = threading.Lock()
        self._cached = None
        self._cached_at = 0.

    def read(self):
        if not self.key or not self.secret:
            return {'status': 'DISCONNECTED', 'message': 'Exchange account not connected',
                    'account': None, 'positions': [], 'retrieved_at': None}
        with self._lock:
            now = self.clock()
            if self._cached is not None and now - self._cached_at < 15:
                return self._cached
            try:
                query = urlencode({'timestamp': int(now * 1000), 'recvWindow': 5000})
                signature = hmac_new(self.secret.encode(), query.encode(), sha256).hexdigest()
                payload = self.transport(ACCOUNT_URL + '?' + query + '&signature=' + signature,
                                         {'X-MBX-APIKEY': self.key})
                if not isinstance(payload, dict) or not isinstance(payload.get('assets'), list) \
                        or not isinstance(payload.get('positions'), list):
                    raise ValueError('Malformed exchange account response')
                usdt = [item for item in payload['assets']
                        if isinstance(item, dict) and item.get('asset') == 'USDT']
                if len(usdt) > 1:
                    raise ValueError('Duplicate USDT account rows')
                asset = usdt[0] if usdt else None
                account = ({'currency': 'USDT', 'wallet_balance': _decimal(asset['walletBalance']),
                            'available_balance': _decimal(asset['availableBalance']),
                            'margin_balance': _decimal(asset['marginBalance']),
                            'unrealized_pnl': _decimal(asset['unrealizedProfit'])}
                           if asset else None)
                positions = []
                for item in payload['positions']:
                    if not isinstance(item, dict) or item.get('symbol') not in ('BTCUSDT', 'ETHUSDT'):
                        continue
                    quantity = Decimal(_decimal(item['positionAmt']))
                    if quantity == 0:
                        continue
                    position_side = item.get('positionSide', 'BOTH')
                    if position_side not in ('BOTH', 'LONG', 'SHORT'):
                        raise ValueError('Unknown exchange position side')
                    side = position_side if position_side != 'BOTH' else (
                        'LONG' if quantity > 0 else 'SHORT')
                    positions.append({'id': item['symbol'] + ':' + str(item.get('positionSide', 'BOTH')),
                                      'symbol': item['symbol'],
                                      'side': side,
                                      'quantity': str(abs(quantity)),
                                      'entry_price': _decimal(item['entryPrice']),
                                      'current_price': None, 'stop_loss': None,
                                      'take_profit': None,
                                      'unrealized_pnl': _decimal(item['unrealizedProfit']),
                                      'status': 'OPEN', 'source': 'Binance USD-M account'})
                result = {'status': 'CONNECTED', 'message': 'REAL ACCOUNT — READ ONLY',
                          'account': account, 'positions': positions,
                          'retrieved_at': time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime(now))}
            except (ValueError, KeyError, TypeError, OSError, json.JSONDecodeError):
                result = {'status': 'ERROR', 'message': 'Read-only exchange account unavailable',
                          'account': None, 'positions': [], 'retrieved_at': None}
            self._cached, self._cached_at = result, now
            return result
