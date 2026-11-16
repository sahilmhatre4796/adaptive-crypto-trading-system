"""Public BTC/ETH candle reader and durable M7 shadow evaluator; no order path."""

import argparse
from pathlib import Path
import time

import pandas as pd

from m13_public_poll import PublicKlinePoller
from trading.m13_shadow import PRIORITY, ShadowSession
from trading.pattern_context import utc_timestamp


def seed_shadow(session, poller, *, clock=lambda: pd.Timestamp.now(tz='UTC')):
    began = utc_timestamp(clock())
    cutoff = began.floor('5min')
    first = cutoff.floor('4h') - pd.Timedelta(minutes=5 * 9600)
    count = int((cutoff - first) / pd.Timedelta(minutes=5))
    if not 9600 <= count <= 9648:
        raise ValueError('Invalid aligned historical seed count')
    previous = dict(poller._last)
    try:
        batches = {}
        for symbol in PRIORITY:
            ticker = symbol.replace('/', '')
            items = poller.fetch_history(ticker,
                start_ms=int(first.timestamp() * 1000), count=count,
                received_at=began)
            if items[-1].candle.closed_at != cutoff:
                raise ValueError('Shadow history did not reach the common cutoff')
            batches[symbol] = items
        retrieved = utc_timestamp(clock())
        if retrieved < began or retrieved-cutoff >= pd.Timedelta(minutes=5):
            raise ValueError('Historical shadow seed expired during acquisition')
        result = session.seed_pair(batches, retrieved_at=retrieved)
    except BaseException:
        poller._last = previous
        raise
    return result


def advance_shadow(session, poller):
    """Prefetch both sources; publish only paired finalized cutoffs."""
    previous = dict(poller._last)
    try:
        observed = {symbol: poller.fetch(symbol.replace('/', '')) for symbol in PRIORITY}
        openings = [tuple(item.opened_at for item in observed[s]) for s in PRIORITY]
        if openings[0] != openings[1]:
            raise ValueError('BTC/ETH finalized candle cutoffs differ')
    except BaseException:
        poller._last = previous
        raise
    results = []
    head = session.journal.head
    try:
        for index in range(len(openings[0])):
            results.append(session.observe_pair(
                {symbol: observed[symbol][index] for symbol in PRIORITY}))
    except BaseException:
        if session.journal.head == head and not session._dead:
            poller._last = previous
        raise
    return tuple(results)


def main():
    parser = argparse.ArgumentParser(description='M7 public-candle shadow evaluation only')
    parser.add_argument('--journal', type=Path, required=True)
    parser.add_argument('--max-pairs', type=int, default=1,
                        help='Stop after this many evaluated pairs; 0 follows continuously')
    parser.add_argument('--poll-seconds', type=float, default=5.)
    args = parser.parse_args()
    if args.max_pairs < 0 or not 1 <= args.poll_seconds <= 60:
        parser.error('Bounded polling interval and nonnegative pair count required')
    session = (ShadowSession.recover(args.journal) if args.journal.exists()
               else ShadowSession.create(args.journal))
    poller = PublicKlinePoller()
    try:
        if not session.latest:
            seed = seed_shadow(session, poller)
            print(f"SHADOW_SEEDED count={seed['count']} cutoff={seed['cutoff'].isoformat()}", flush=True)
        else:
            for symbol, item in session.latest.items():
                poller._last[symbol.replace('/', '')] = item
        completed = 0
        while args.max_pairs == 0 or completed < args.max_pairs:
            results = advance_shadow(session, poller)
            for result in results:
                completed += 1
                for symbol in PRIORITY:
                    asset = result['assets'][symbol]
                    decisions = asset['decisions']
                    print(f"{symbol} open={asset['opened_at'].isoformat()} "
                          f"receipt={asset['received_at'].isoformat()} "
                          f"patterns={len(asset['pattern_ids'])} "
                          f"decisions={len(decisions)} "
                          f"qualified={sum(bool(d['qualification_passed']) for d in decisions)} "
                          f"status=UNAPPROVED_SHADOW", flush=True)
                    for decision in decisions:
                        print(f"  {decision['decision_id']} {decision['decision']} "
                              f"reasons={decision['evaluation']['reason_codes']}", flush=True)
            if args.max_pairs == 0 or completed < args.max_pairs:
                time.sleep(args.poll_seconds)
    finally:
        session.close()


if __name__ == '__main__':
    main()
