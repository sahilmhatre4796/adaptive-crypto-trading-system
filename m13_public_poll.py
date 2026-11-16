"""Public-only Binance USD-M candle polling; no order or credential capability."""

import argparse
from pathlib import Path
import time

import pandas as pd
import requests

from trading.m13_feed import SOURCE_IDS, parse_klines
from trading.m13_risk_inputs import parse_exchange_info, parse_mark_price, parse_funding_rate
from trading.m13_risk_inputs import assemble_frame_risk_inputs
from trading.pattern_context import utc_timestamp


URL = 'https://fapi.binance.com/fapi/v1/klines'
EXCHANGE_INFO_URL = 'https://fapi.binance.com/fapi/v1/exchangeInfo'
MARK_URL = 'https://fapi.binance.com/fapi/v1/premiumIndex'
FUNDING_URL = 'https://fapi.binance.com/fapi/v1/fundingRate'
FINALIZATION_GRACE = pd.Timedelta(seconds=30)


class PublicKlinePoller:
    def __init__(self, session=None, *, attempts=3, sleeper=time.sleep):
        if type(attempts) is not int or not 1 <= attempts <= 5:
            raise ValueError('Bounded attempt count required')
        self.session = session if session is not None else requests.Session()
        self.attempts = attempts
        self.sleeper = sleeper
        self._last = {}

    def _get(self, url, params, *, max_bytes=2_000_000):
        if url not in (URL, EXCHANGE_INFO_URL, MARK_URL, FUNDING_URL):
            raise ValueError('Only allowlisted public USD-M endpoints are supported')
        response = None
        for attempt in range(self.attempts):
            try:
                response = self.session.get(url, params=params, timeout=10)
            except requests.RequestException:
                if attempt + 1 == self.attempts:
                    raise
            else:
                if response.status_code == 200:
                    break
                if response.status_code not in (429, 500, 502, 503, 504) or attempt + 1 == self.attempts:
                    raise ValueError(f'Public USD-M HTTP status {response.status_code}')
            self.sleeper(min(2 ** attempt, 4))
        if len(response.content) > max_bytes:
            raise ValueError('Oversized public USD-M response')
        return response.json()

    def _rows(self, symbol, *, limit, start_ms=None, end_ms=None):
        params = {'symbol': symbol, 'interval': '5m', 'limit': limit}
        if start_ms is not None:
            params['startTime'] = start_ms
        if end_ms is not None:
            params['endTime'] = end_ms
        return self._get(URL, params)

    def exchange_rules(self, symbol):
        if symbol not in SOURCE_IDS:
            raise ValueError('Supported USD-M symbol required')
        payload = self._get(EXCHANGE_INFO_URL, {}, max_bytes=10_000_000)
        return parse_exchange_info(payload, symbol, retrieved_at=pd.Timestamp.now(tz='UTC'))

    def mark_price(self, symbol):
        if symbol not in SOURCE_IDS:
            raise ValueError('Supported USD-M symbol required')
        payload = self._get(MARK_URL, {'symbol': symbol})
        return parse_mark_price(payload, symbol, received_at=pd.Timestamp.now(tz='UTC'))

    def funding_facts(self, symbol, *, since_ms, limit=100):
        if (symbol not in SOURCE_IDS or type(since_ms) is not int or since_ms < 0
                or type(limit) is not int or not 1 <= limit <= 1000):
            raise ValueError('Bounded funding request required')
        rows = self._get(FUNDING_URL, {'symbol': symbol, 'startTime': since_ms, 'limit': limit})
        if type(rows) is not list:
            raise ValueError('Official funding list required')
        received = pd.Timestamp.now(tz='UTC')
        result = tuple(parse_funding_rate(row, symbol, received_at=received) for row in rows)
        if any(left.settled_at >= right.settled_at for left, right in zip(result, result[1:])):
            raise ValueError('Duplicate or out-of-order funding settlement')
        return result

    def fetch(self, symbol, *, start_ms=None, limit=3, received_at=None, warmup=False):
        if symbol not in SOURCE_IDS or type(limit) is not int or not 1 <= limit <= 1500:
            raise ValueError('Supported symbol and bounded limit required')
        if start_ms is not None and (type(start_ms) is not int or start_ms < 0):
            raise ValueError('Nonnegative millisecond start required')
        rows = self._rows(symbol, limit=limit, start_ms=start_ms)
        received = pd.Timestamp.now(tz='UTC') if received_at is None else received_at
        candles = parse_klines(symbol, rows,
                               received_at=received, warmup=warmup)
        previous = self._last.get(symbol)
        if previous is None and not warmup and start_ms is None:
            # A first live poll observes only the newest completed bar. Older
            # rows are context, not late events eligible for runtime admission.
            candles = candles[-1:]
        if not warmup:
            candles = tuple(item for item in candles if
                            received - item.candle.closed_at >= FINALIZATION_GRACE)
        new = []
        for item in candles:
            if previous is not None:
                if item.opened_at < previous.opened_at:
                    continue
                if item.opened_at == previous.opened_at:
                    if item.ohlc != previous.ohlc or item.volume != previous.volume:
                        raise ValueError('Revised public candle')
                    continue
                if item.opened_at != previous.opened_at + pd.Timedelta(minutes=5):
                    raise ValueError('Missing public candle')
            previous = item
            new.append(item)
        if not warmup and any(item.received_at - item.candle.closed_at >= pd.Timedelta(minutes=5)
                              for item in new):
            raise ValueError('Late public candle cannot enter M12 history')
        if previous is not None:
            self._last[symbol] = previous
        return tuple(new)

    def fetch_history(self, symbol, *, start_ms, count, received_at=None):
        """Acquire one exact contiguous warmup window; publish state only when complete."""
        if (symbol not in SOURCE_IDS or type(start_ms) is not int or start_ms < 0
                or start_ms % 300_000 or type(count) is not int or not 1 <= count <= 9648):
            raise ValueError('Supported symbol and bounded aligned history required')
        received = pd.Timestamp.now(tz='UTC') if received_at is None else received_at
        result = []
        while len(result) < count:
            next_ms = start_ms + len(result) * 300_000
            batch_size = min(1000, count - len(result))
            rows = self._rows(symbol, start_ms=next_ms,
                              end_ms=next_ms + batch_size * 300_000 - 1,
                              limit=batch_size)
            batch = parse_klines(symbol, rows, received_at=received, warmup=True)
            if (len(batch) != batch_size
                    or int(batch[0].opened_at.timestamp() * 1000) != next_ms):
                raise ValueError('Historical warmup page is incomplete or shifted')
            result.extend(batch)
        self._last[symbol] = result[-1]
        return tuple(result)


def collect_frame_risk_inputs(poller, symbol, *, fx, costs, leverage,
                              multiplier, max_leverage, correlation_group,
                              risk_config, as_of):
    """Fetch public rules/mark; reject absent operator FX and cost assumptions."""
    if (not isinstance(poller, PublicKlinePoller)
            or symbol.replace('/', '') not in SOURCE_IDS):
        raise ValueError('Public poller and supported instrument required')
    ticker = symbol.replace('/', '')
    rules = poller.exchange_rules(ticker)
    mark = poller.mark_price(ticker)
    return assemble_frame_risk_inputs(rules, mark, fx, costs,
        leverage=leverage, multiplier=multiplier,
        max_leverage=max_leverage, correlation_group=correlation_group,
        risk_config=risk_config, as_of=as_of)


def seed_live_runtime(runtime, poller, *, clock=lambda: pd.Timestamp.now(tz='UTC')):
    """Fetch a common current BTC/ETH warmup and journal it as one actor call.

    A delayed download cannot be used to backdate strategy decisions. The
    next frame must be a genuinely fresh, independent completed candle.
    """
    from trading.m13_runtime import M13PaperRuntime
    if not isinstance(runtime, M13PaperRuntime) or not isinstance(poller, PublicKlinePoller):
        raise ValueError('Fresh v2 runtime and public-only poller required')
    began = utc_timestamp(clock())
    cutoff = began.floor('5min')
    anchor = cutoff.floor('4h')
    first = anchor - pd.Timedelta(minutes=5*9600)
    count = int((cutoff-first)/pd.Timedelta(minutes=5))
    if not 9600 <= count <= 9648:
        raise ValueError('Invalid aligned historical seed count')
    start_ms = int(first.timestamp()*1000)
    previous = dict(poller._last)
    try:
        batches = {}
        for symbol in runtime.priority:
            ticker = symbol.replace('/', '')
            items = poller.fetch_history(ticker, start_ms=start_ms,
                                         count=count, received_at=began)
            if items[-1].candle.closed_at != cutoff:
                raise ValueError('Historical seed does not reach common current cutoff')
            batches[symbol] = items
        retrieved = utc_timestamp(clock())
        if retrieved < began or retrieved-cutoff >= pd.Timedelta(minutes=5):
            raise ValueError('Historical seed became stale before journal publication')
        receipt = runtime.seed_pair(batches, retrieved_at=retrieved)
    except BaseException:
        poller._last = previous
        raise
    return dict(cutoff=cutoff, first_open=first, count=count,
                retrieved_at=retrieved, journal_receipt=receipt)


def advance_live_runtime(runtime, poller, *, inputs_for=None):
    """Fetch both assets before any frame; M13 journals each in frozen priority."""
    from trading.m13_runtime import M13PaperRuntime
    if not isinstance(runtime, M13PaperRuntime) or not isinstance(poller, PublicKlinePoller):
        raise ValueError('V2 runtime and public-only poller required')
    previous = dict(poller._last)
    try:
        observed = {symbol: poller.fetch(symbol.replace('/', ''))
                    for symbol in runtime.priority}
        opening_sequences = [tuple(x.opened_at for x in observed[s]) for s in runtime.priority]
        if opening_sequences[0] != opening_sequences[1]:
            raise ValueError('BTC/ETH finalized candle cutoffs differ; no partial frame publication')
        jobs = []
        for opened_at in opening_sequences[0]:
            for symbol in runtime.priority:
                item = next(x for x in observed[symbol] if x.opened_at == opened_at)
                kwargs = {} if inputs_for is None else inputs_for(item)
                if type(kwargs) is not dict:
                    raise ValueError('Explicit typed risk-input mapping required')
                if runtime.proposal_from_m7 is not None and any(
                        kwargs.get(key) is None for key in
                        ('mark', 'fx', 'spec', 'costs', 'leverage')):
                    raise ValueError('Complete M8 risk inputs required before frame publication')
                jobs.append((item, kwargs))
    except BaseException:
        poller._last = previous
        raise
    results = []
    head_before = runtime.journal.head
    try:
        for item, kwargs in jobs:
            results.append(runtime.frame(item, **kwargs))
    except BaseException:
        # An input rejection before the first journal command is retryable.
        # Once dispatch starts, the journal and fresh-actor recovery are the
        # only authorities; never rewind a cursor past committed frames.
        if runtime.journal.head == head_before and not runtime._dead:
            poller._last = previous
        raise
    return tuple(results)


def main():
    parser = argparse.ArgumentParser(description='Inspect finalized public USD-M 5m candles only')
    parser.add_argument('--once', action='store_true', required=True)
    parser.add_argument('--journal', type=Path,
                        help='Fresh v2 observation-only SQLite journal; never places orders')
    args = parser.parse_args()
    if args.once:
        poller = PublicKlinePoller()
        session = None
        try:
            if args.journal is not None:
                from trading.m13_observations import PublicObservationSession
                session = (PublicObservationSession.recover(args.journal)
                           if args.journal.exists() else
                           PublicObservationSession.create(args.journal))
                for symbol, prior in session.latest.items():
                    ticker = symbol.replace('/', '')
                    poller._last[ticker] = prior
            batch = []
            for symbol in ('BTCUSDT', 'ETHUSDT'):
                items = poller.fetch(symbol)
                if not items:
                    print(f'{symbol} status=NO_NEW_FINALIZED_CANDLE')
                else:
                    batch.extend(items)
            for item in sorted(batch, key=lambda x:
                               (x.opened_at, ('BTC/USDT', 'ETH/USDT').index(x.symbol))):
                status = 'FINALIZED'
                if session is not None:
                    session.observe(item)
                    status = 'JOURNALED_OBSERVATION_ONLY'
                print(f'{item.symbol.replace("/", "")} open={item.opened_at.isoformat()} '
                      f'receipt={item.received_at.isoformat()} status={status}')
        finally:
            if session is not None:
                session.close()


if __name__ == '__main__':
    main()
