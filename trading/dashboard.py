"""Read-only dashboard projections over committed shadow facts and M8/M9.1 actors."""

from collections import Counter, deque
from decimal import Decimal
from pathlib import Path
import sqlite3
import threading

import pandas as pd

from trading.m13_runtime_journal import source_hash
from trading.portfolio import PortfolioSnapshot
from trading.replay_codec import decode


SYMBOLS = ('BTC/USDT', 'ETH/USDT')


class DashboardDataError(ValueError):
    pass


def _at(value):
    return value.isoformat() if value is not None else None


def _money(value):
    return str(value) if value is not None else None


def _signal(record):
    value = record.to_dict() if hasattr(record, 'to_dict') else record
    evaluation = value['evaluation']
    context = evaluation['context']
    pattern = context['pattern']
    regimes = context['m6']['regime']
    regime = regimes[-1] if regimes else None
    return {
        'id': value['decision_id'], 'signal_id': value['signal_id'],
        'symbol': value['symbol'], 'side': value['candidate_side'],
        'timeframe': pattern['timeframe'], 'pattern': pattern['name'],
        'pattern_family': pattern['family'], 'pattern_id': value['pattern_id'],
        'pattern_confirmed_at': pattern['confirmed_at'],
        'confirmation_opened_at': context['confirmation_candle']['opened_at'],
        'm3_status': evaluation['m3_decision']['status'],
        'm5_trend': context['structure']['trend'],
        'm5_location_eligible': context['structure_eligible'],
        'm6_regime': regime['classification'] if regime else None,
        'm6_confidence': regime['confidence'] if regime else None,
        'm6_as_of': regime['as_of'] if regime else None,
        'm6_derivatives_count': len(context['m6']['derivatives']),
        'm6_heatmap_count': len(context['m6']['heatmaps']),
        'decision': value['decision'], 'lifecycle': value['lifecycle_state'],
        'qualified': value['qualification_passed'],
        'blocking_reasons': evaluation['blocking_reasons'],
        'reason_codes': evaluation['reason_codes'],
        'score': evaluation['score'], 'confidence': evaluation['confidence'],
        'available_at': value['available_at'], 'expires_at': value['expires_at'],
        'risk_status': None,
    }


class ShadowJournalView:
    """Incrementally read APPLIED rows; never acquire the journal writer epoch."""

    def __init__(self, path):
        self.path = Path(path).resolve() if path else None
        self._lock = threading.RLock()
        self._seq = 0
        self._bars = {symbol: deque(maxlen=300) for symbol in SYMBOLS}
        self._signals = []
        self._patterns = Counter()
        self._receipts = {symbol: None for symbol in SYMBOLS}
        self._health = {'status': 'UNAVAILABLE', 'message': 'No shadow journal configured',
                        'head': None, 'applied': None, 'integrity': None}

    def read(self, *, now=None):
        now = now or pd.Timestamp.now(tz='UTC')
        with self._lock:
            if self.path is None or not self.path.is_file():
                self._seq = 0
                self._bars = {symbol: deque(maxlen=300) for symbol in SYMBOLS}
                self._signals = []
                self._patterns = Counter()
                self._receipts = {symbol: None for symbol in SYMBOLS}
                self._health = {'status': 'UNAVAILABLE', 'message': 'Shadow journal not available',
                                'head': None, 'applied': None, 'integrity': None}
                return self._view(now)
            try:
                db = sqlite3.connect(self.path.as_uri() + '?mode=ro', uri=True, timeout=2)
                try:
                    db.execute('PRAGMA query_only=ON')
                    meta = dict(db.execute('SELECT key,value FROM meta'))
                    if meta.get('code') != source_hash():
                        raise DashboardDataError('Shadow source identity differs from journal')
                    head, applied = int(meta['head']), int(meta['applied'])
                    if applied < self._seq or head < applied:
                        raise DashboardDataError('Shadow journal moved backwards')
                    if applied != self._seq or self._health['status'] in ('UNAVAILABLE', 'ERROR'):
                        integrity = db.execute('PRAGMA quick_check').fetchone()[0]
                        if integrity != 'ok':
                            raise DashboardDataError('SQLite quick check failed')
                        rows = db.execute('SELECT c.seq,c.operation,c.payload,o.state,o.result '
                                          'FROM commands c JOIN outcomes o ON o.seq=c.seq '
                                          'WHERE c.seq>? AND c.seq<=? ORDER BY c.seq',
                                          (self._seq, applied))
                        for seq, operation, payload, state, result in rows:
                            if seq != self._seq + 1:
                                raise DashboardDataError('Shadow journal sequence gap')
                            if state == 'QUARANTINED':
                                self._seq = seq
                                self._health = {'status': 'QUARANTINED',
                                                'message': 'Shadow journal quarantined',
                                                'head': head, 'applied': applied,
                                                'integrity': integrity}
                                break
                            if state != 'APPLIED':
                                raise DashboardDataError('Unknown shadow outcome')
                            command = decode(payload)
                            if operation == 'SEED_PAIR':
                                for symbol in SYMBOLS:
                                    for candle in command[symbol][-300:]:
                                        self._bars[symbol].append((candle['opened_at'], candle['ohlc'][3]))
                            elif operation == 'SHADOW_PAIR':
                                outcome = decode(result)['assets']
                                for symbol in SYMBOLS:
                                    candle = command[symbol]
                                    asset = outcome[symbol]
                                    if candle['opened_at'] != asset['opened_at']:
                                        raise DashboardDataError('Shadow candle/result mismatch')
                                    self._bars[symbol].append((candle['opened_at'], candle['ohlc'][3]))
                                    self._receipts[symbol] = asset['received_at']
                                    self._patterns[symbol] += len(asset['pattern_ids'])
                                    self._signals.extend(_signal(record) for record in asset['decisions'])
                            else:
                                raise DashboardDataError('Unsupported shadow operation')
                            self._seq = seq
                        else:
                            self._health = {'status': 'PENDING' if head != applied else 'OK',
                                            'message': 'Unfinished command' if head != applied else 'Verified committed prefix',
                                            'head': head, 'applied': applied, 'integrity': integrity}
                    elif self._health['status'] != 'QUARANTINED':
                        self._health.update(status='PENDING' if head != applied else 'OK',
                                            head=head, applied=applied)
                finally:
                    db.close()
            except (OSError, sqlite3.Error, ValueError, KeyError, TypeError) as exc:
                self._seq = 0
                self._bars = {symbol: deque(maxlen=300) for symbol in SYMBOLS}
                self._signals = []
                self._patterns = Counter()
                self._receipts = {symbol: None for symbol in SYMBOLS}
                self._health = {'status': 'ERROR', 'message': str(exc),
                                'head': None, 'applied': None, 'integrity': None}
            return self._view(now)

    def _view(self, now):
        markets = []
        for symbol in SYMBOLS:
            bars = self._bars[symbol]
            last = bars[-1] if bars else None
            reference = next((price for opened, price in bars
                              if last is not None and opened == last[0] - pd.Timedelta(days=1)), None)
            receipt = self._receipts[symbol]
            age = (now - receipt).total_seconds() if receipt is not None else None
            markets.append({'symbol': symbol.replace('/', ''),
                            'price': last[1] if last else None,
                            'change_24h_pct': (last[1] / reference - 1) * 100 if reference else None,
                            'last_finalized_candle': _at(last[0] + pd.Timedelta(minutes=5)) if last else None,
                            'received_at': _at(receipt),
                            'feed_status': ('UNAVAILABLE' if age is None else
                                            'ERROR' if age < -1 else
                                            'LIVE' if age <= 600 else 'STALE'),
                            'patterns': self._patterns[symbol]})
        return {'markets': markets, 'signals': list(self._signals),
                'health': dict(self._health), 'last_receipt': _at(max(
                    (at for at in self._receipts.values() if at is not None), default=None))}


def _paper(runtime):
    """Project immutable M8/M9.1 facts; refuse an actor graph mid-command."""
    if runtime is None:
        return {'status': 'UNAVAILABLE', 'message': 'Paper runtime not connected',
                'account': None, 'positions': [], 'trades': [], 'analytics': None,
                'risk': None, 'equity_curve': [], 'daily_pnl': [], 'orders': []}
    journal = runtime.journal
    before = (journal.head, journal.applied)
    if before[0] != before[1] or runtime._dead or runtime._open_frame is not None:
        raise DashboardDataError('Paper actor has an unfinished or quarantined operation')
    bridge = runtime.bridge
    portfolio = bridge.risk.portfolio
    snap = portfolio.snapshot(as_of=portfolio._last_at)
    if not isinstance(snap, PortfolioSnapshot):
        raise DashboardDataError('Authoritative M8 snapshot unavailable')
    orders = {order.allocation.allocation_id: order for order in bridge.orders}
    plans = {plan.order.order_id: plan for plan in runtime.controller.plans}
    marks = {quote.key: quote.value for quote in snap.marks}
    fx_rates = {quote.key: quote.value for quote in snap.fx_rates}
    positions = []
    for position in snap.positions:
        plan = position.plan
        order = orders.get(plan.allocation_id)
        mark = marks.get(plan.spec.symbol)
        fx = fx_rates.get(plan.spec.quote_currency)
        if mark is None or fx is None or order is None:
            raise DashboardDataError('Paper position lacks M8 mark, FX or M9.1 order')
        direction = Decimal(1) if plan.side == 'LONG' else Decimal(-1)
        unrealized = direction * (mark - plan.entry) * plan.quantity * plan.spec.multiplier * fx
        exit_plan = plans.get(order.order_id)
        stop = order.source_stop_price
        if exit_plan is not None and hasattr(runtime.controller, '_trigger'):
            latest = runtime.drivers[plan.spec.symbol].inputs
            stop = runtime.controller._trigger(exit_plan, latest[-1].opened_at if latest else snap.as_of)
        positions.append({'id': order.order_id, 'symbol': plan.spec.symbol.replace('/', ''),
                          'side': plan.side, 'entry_price': _money(plan.entry),
                          'current_price': _money(mark), 'quantity': _money(plan.quantity),
                          'stop_loss': _money(stop),
                          'take_profit': _money(getattr(exit_plan, 'tp2',
                                                getattr(exit_plan, 'take_profit_price', None))),
                          'unrealized_pnl': _money(unrealized), 'status': bridge.state(order.order_id),
                          'opened_at': _at(position.opened_at),
                          'funding_cash': _money(position.funding_cash), 'source': 'M8/M9.1 simulation'})
    if sum((Decimal(p['unrealized_pnl']) for p in positions), Decimal(0)) != snap.unrealized_pnl:
        raise DashboardDataError('Position P&L does not reconcile with M8 snapshot')
    history = list(portfolio.history)
    by_event = {event.event_id: (index, event) for index, event in enumerate(history)}
    lifecycle = {event.order_id: {} for event in bridge.history}
    for event in bridge.history:
        if event.kind in ('ENTRY_FILLED', 'EXIT_FILLED'):
            lifecycle[event.order_id][event.kind] = event
    exit_reasons = {}
    for _, outcomes in getattr(runtime.controller, '_candles', {}).values():
        for outcome in outcomes:
            if hasattr(outcome, 'lifecycle'):
                exit_reasons[outcome.lifecycle.event_id] = outcome.reason
    trades = []
    for order in bridge.orders:
        events = lifecycle.get(order.order_id, {})
        entry, exit_event = events.get('ENTRY_FILLED'), events.get('EXIT_FILLED')
        if entry is None or exit_event is None:
            continue
        if entry.portfolio_event_id not in by_event or exit_event.portfolio_event_id not in by_event:
            raise DashboardDataError('Trade lacks authoritative M8 bookings')
        open_index, opened = by_event[entry.portfolio_event_id]
        close_index, closed = by_event[exit_event.portfolio_event_id]
        if not 0 <= open_index < close_index:
            raise DashboardDataError('Trade booking order is inconsistent')
        before_close = history[close_index - 1].snapshot
        exit_fee = closed.snapshot.fees_paid - before_close.fees_paid
        gross = closed.snapshot.realized_pnl - before_close.realized_pnl + exit_fee
        entry_fee = order.allocation.entry_fee
        previous_position = next((p for p in before_close.positions
                                  if p.plan.allocation_id == order.allocation.allocation_id), None)
        if previous_position is None:
            raise DashboardDataError('Closing trade lacks its prior M8 position')
        funding = previous_position.funding_cash
        net = gross - entry_fee - exit_fee + funding
        exit_plan = plans.get(order.order_id)
        trades.append({'id': order.order_id, 'symbol': order.binding.symbol.replace('/', ''),
                       'side': order.side, 'entry_at': _at(entry.available_at),
                       'entry_price': _money(entry.fill.price), 'exit_at': _at(exit_event.available_at),
                       'exit_price': _money(exit_event.fill.price),
                       'quantity': _money(order.quantity_contracts),
                       'stop_loss': _money(order.source_stop_price),
                       'take_profit': _money(getattr(exit_plan, 'tp2',
                                             getattr(exit_plan, 'take_profit_price', None))),
                       'gross_pnl': _money(gross), 'fees': _money(entry_fee + exit_fee),
                       'funding': _money(funding), 'net_pnl': _money(net),
                       'planned_risk': _money(order.risk.planned_loss),
                       'exit_reason': exit_reasons.get(exit_event.event_id, 'SIMULATED_EXIT'),
                       'status': 'CLOSED', 'currency': snap.currency})
    trades.sort(key=lambda row: (row['exit_at'], row['id']), reverse=True)
    closed = [Decimal(t['net_pnl']) for t in trades]
    wins = [p for p in closed if p > 0]
    losses = [p for p in closed if p < 0]
    def average(values):
        return _money(sum(values) / len(values)) if values else None
    def group(name, value):
        subset = [t for t in trades if t[name] == value]
        return {'trades': len(subset), 'net_pnl': _money(sum((Decimal(t['net_pnl']) for t in subset), Decimal(0)))}
    analytics = {'total_trades': len(trades), 'win_rate_pct': len(wins) / len(closed) * 100 if closed else None,
                 'profit_factor': _money(sum(wins) / -sum(losses)) if losses else None,
                 'max_drawdown_pct': _money(snap.max_drawdown * 100),
                 'average_win': average(wins), 'average_loss': average(losses),
                 'average_realized_r': average([Decimal(t['net_pnl']) / Decimal(t['planned_risk'])
                                               for t in trades if Decimal(t['planned_risk']) > 0]),
                 'by_side': {side: group('side', side) for side in ('LONG', 'SHORT')},
                 'by_asset': {asset: group('symbol', asset) for asset in ('BTCUSDT', 'ETHUSDT')}}
    equity_curve = [{'at': _at(event.available_at), 'equity': _money(event.snapshot.equity)}
                    for event in history]
    daily = {}
    for point in equity_curve:
        daily[point['at'][:10]] = Decimal(point['equity'])
    prior = portfolio.initial_cash
    daily_pnl = []
    for day, equity in sorted(daily.items()):
        daily_pnl.append({'day': day, 'pnl': _money(equity - prior), 'equity': _money(equity)})
        prior = equity
    decisions = [{'id': item.decision_id, 'signal_id': item.signal_id,
                  'symbol': item.proposal.symbol.replace('/', ''), 'status': item.status,
                  'reasons': list(item.reason_codes), 'planned_loss': _money(item.planned_loss),
                  'risk_budget': _money(item.risk_budget), 'quantity': _money(item.quantity),
                  'equity_reference': _money(item.equity_reference),
                  'available_at': _at(item.available_at)} for item in bridge.risk.history]
    end = (journal.head, journal.applied)
    if end != before or end[0] != end[1] or runtime._dead or runtime._open_frame is not None:
        raise DashboardDataError('Paper runtime changed during read; retry')
    return {'status': 'CONNECTED', 'message': 'Offline simulated portfolio',
            'account': {'currency': snap.currency, 'equity': _money(snap.equity),
                        'cash': _money(snap.cash), 'available_balance': _money(snap.available_capital),
                        'used_capital': _money(snap.used_margin + snap.reserved_capital),
                        'realized_pnl': _money(snap.realized_pnl),
                        'unrealized_pnl': _money(snap.unrealized_pnl),
                        'cumulative_pnl': _money(snap.equity - portfolio.initial_cash),
                        'today_pnl': _money(snap.equity - snap.day_start_equity),
                        'fees': _money(snap.fees_paid), 'funding': _money(snap.funding_cash),
                        'as_of': _at(snap.as_of)},
            'positions': positions, 'trades': trades, 'analytics': analytics,
            'risk': {'risk_fraction': _money(bridge.risk.config.risk_fraction),
                     'risk_budget_now': _money(snap.equity * bridge.risk.config.risk_fraction),
                     'portfolio_equity': _money(snap.equity), 'stop_risk': _money(snap.stop_risk),
                     'gross_exposure': _money(snap.gross_exposure),
                     'open_positions': len(snap.positions), 'reservations': len(snap.reservations),
                     'remaining_aggregate_stop_risk': _money(max(Decimal(0),
                         snap.equity * bridge.risk.config.max_portfolio_risk - snap.stop_risk)),
                     'decisions': decisions,
                     'rejection_count': sum(d['status'] in ('REJECTED', 'BLOCKED') for d in decisions)},
            'equity_curve': equity_curve, 'daily_pnl': daily_pnl,
            'orders': [{'id': order.order_id, 'symbol': order.binding.symbol.replace('/', ''),
                        'side': order.side, 'state': bridge.state(order.order_id),
                        'quantity': _money(order.quantity_contracts),
                        'created_at': _at(order.created_at)} for order in bridge.orders]}


class DashboardReader:
    def __init__(self, *, shadow_path=None, paper_runtime=None, real_account=None):
        self.shadow = ShadowJournalView(shadow_path)
        self.paper_runtime = paper_runtime
        self.real_account = real_account

    def snapshot(self, mode='paper'):
        if mode not in ('paper', 'real'):
            raise ValueError('Unknown dashboard monitoring mode')
        shadow = self.shadow.read()
        if mode == 'paper':
            try:
                account = _paper(self.paper_runtime)
            except DashboardDataError as exc:
                account = {'status': 'ERROR', 'message': str(exc), 'account': None,
                           'positions': [], 'trades': [], 'analytics': None,
                           'risk': None, 'equity_curve': [], 'daily_pnl': [], 'orders': []}
        else:
            account = self.real_account.read() if self.real_account else {
                'status': 'DISCONNECTED', 'message': 'Exchange account not connected',
                'account': None, 'positions': [], 'retrieved_at': None}
            account = {**account, 'trades': [], 'analytics': None, 'risk': None,
                       'equity_curve': [], 'daily_pnl': [], 'orders': []}
        risk_by_signal = {d['signal_id']: d['status'] for d in
                          (account.get('risk') or {}).get('decisions', [])}
        signals = [{**item, 'risk_status': risk_by_signal.get(item['signal_id'])}
                   for item in shadow['signals']]
        return {'schema_version': 'dashboard-v1', 'mode': mode,
                'mode_label': 'PAPER TRADING' if mode == 'paper' else 'REAL ACCOUNT — READ ONLY',
                'execution_enabled': False, 'account_source': account['status'],
                'account_message': account['message'], 'account': account['account'],
                'positions': account['positions'], 'trades': account['trades'],
                'analytics': account['analytics'], 'risk': account['risk'],
                'equity_curve': account['equity_curve'], 'daily_pnl': account['daily_pnl'],
                'orders': account['orders'], 'markets': shadow['markets'], 'signals': signals,
                'health': {'journal': shadow['health'], 'last_processing_at': shadow['last_receipt'],
                           'paper_execution': account['status'] if mode == 'paper' else 'DISABLED',
                           'runtime': 'UNKNOWN — no heartbeat',
                           'market_data': [m['feed_status'] for m in shadow['markets']],
                           'account_connection': account['status']}}

    def trades(self, *, mode='paper', search='', asset='', side='', result='', start='', end='',
               page=1, page_size=25):
        if not 1 <= page_size <= 100 or page < 1:
            raise ValueError('Invalid pagination')
        values = self.snapshot(mode)['trades']
        if search:
            values = [t for t in values if search.lower() in
                      (t['id'] + ' ' + t['symbol'] + ' ' + t['exit_reason']).lower()]
        if asset:
            values = [t for t in values if t['symbol'] == asset]
        if side:
            values = [t for t in values if t['side'] == side]
        if result in ('profit', 'loss'):
            values = [t for t in values if (Decimal(t['net_pnl']) > 0 if result == 'profit'
                                                else Decimal(t['net_pnl']) < 0)]
        if start:
            values = [t for t in values if t['exit_at'][:10] >= start]
        if end:
            values = [t for t in values if t['exit_at'][:10] <= end]
        return {'total': len(values), 'page': page, 'page_size': page_size,
                'items': values[(page - 1) * page_size:page * page_size]}
