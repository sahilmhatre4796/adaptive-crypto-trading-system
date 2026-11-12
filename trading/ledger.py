from dataclasses import dataclass
from datetime import timezone
from math import isfinite
from numbers import Real


def finite_number(name, value, *, positive=False):
    if isinstance(value, bool) or not isinstance(value, Real) or not isfinite(value):
        raise ValueError(f'{name} must be a finite number')
    if positive and value <= 0:
        raise ValueError(f'{name} must be positive')
    return float(value)

@dataclass
class Position:
    side: str
    entry: float
    qty: float
    stop: float
    initial_qty: float
    gross: float = 0
    fees: float = 0
    funding: float = 0
    trade_id: str = ''
    entry_equity: float = 0

    @property
    def direction(self):
        return 1 if self.side == 'LONG' else -1

class Ledger:
    def __init__(self, capital, fee=0, slip=0):
        self.balance = finite_number('capital', capital, positive=True)
        self.fee = finite_number('fee', fee)
        self.slip = finite_number('slippage', slip)
        if not 0 <= self.fee < 1 or not 0 <= self.slip < 1:
            raise ValueError('Fee and slippage fractions must be in [0, 1)')
        self.position = None
        self.fills = []
        self.funding_events = []
        self.trade_count = 0

    def _record_fill(self, p, ts, kind, qty, price, fee, gross):
        number = len(self.fills) + 1
        self.fills.append(dict(
            timestamp=ts, trade_id=p.trade_id, fill_id=f'fill-{number}',
            order_id=f'order-{number}', side=p.side, kind=kind, qty=qty,
            price=price, fee=fee, gross=gross, remaining_qty=p.qty, balance=self.balance))

    def open(self, side, price, qty, stop, ts):
        price = finite_number('price', price, positive=True)
        qty = finite_number('quantity', qty, positive=True)
        stop = finite_number('stop', stop, positive=True)
        if self.position or side not in ('LONG','SHORT') or self.balance <= 0:
            raise ValueError('Invalid entry')
        direction = 1 if side == 'LONG' else -1
        price = finite_number('fill price', price * (1 + direction * self.slip), positive=True)
        notional = finite_number('notional', qty * price, positive=True)
        cost = notional * self.fee
        balance = finite_number('balance', self.balance - cost)
        self.trade_count += 1
        self.position = Position(side, price, qty, stop, qty, fees=cost,
                                 trade_id=f'trade-{self.trade_count}', entry_equity=self.balance)
        self.balance = balance
        self._record_fill(self.position, ts, 'ENTRY', qty, price, cost, 0)

    def close(self, qty, price, ts, reason):
        qty = finite_number('quantity', qty, positive=True)
        price = finite_number('price', price, positive=True)
        p = self.position
        if p is None or qty > p.qty:
            raise ValueError('Invalid exit quantity')
        price = finite_number('fill price', price * (1 - p.direction * self.slip), positive=True)
        gross = finite_number('gross P&L', p.direction * qty * (price-p.entry))
        fee = finite_number('exit notional', qty * price, positive=True) * self.fee
        total_gross = finite_number('trade gross P&L', p.gross + gross)
        total_fees = finite_number('trade fees', p.fees + fee)
        pnl = finite_number('net trade P&L', total_gross - total_fees + p.funding)
        balance = finite_number('balance', self.balance + gross - fee)
        # Validate every value before mutating the accounting state.
        p.gross = total_gross; p.fees = total_fees; p.qty -= qty
        self.balance = balance
        self._record_fill(p, ts, reason, qty, price, fee, gross)
        if p.qty == 0:
            self.position = None
            return dict(trade_id=p.trade_id, pnl=pnl, pnl_pct=pnl/p.entry_equity,
                        gross=p.gross, fees=p.fees, funding=p.funding)

    def fund(self, rate, mark, ts):
        rate = finite_number('funding rate', rate)
        mark = finite_number('funding mark', mark, positive=True)
        if self.position:
            p = self.position
            cash = finite_number('funding cash flow', -p.direction*p.qty*mark*rate)
            balance = finite_number('balance', self.balance + cash)
            funding = finite_number('trade funding', p.funding + cash)
            self.balance = balance; p.funding = funding
            self.funding_events.append(dict(timestamp=ts, trade_id=p.trade_id,
                                            rate=rate, mark=mark, qty=p.qty, cash=cash))

    def equity(self, mark):
        mark = finite_number('mark', mark, positive=True)
        p = self.position
        return finite_number('equity', self.balance + (p.direction*p.qty*(mark-p.entry) if p else 0))

class Halts:
    def __init__(self, p):
        p.validate()
        self.p=p; self.peak=p.initial_capital
        self.day=self.week=None
        self.daily=self.weekly=self.hard=False
        self.last_equity=p.initial_capital
        self.last_timestamp=None
        self.drawdown=self.max_drawdown=0.0

    def update(self, ts, equity):
        equity=finite_number('risk equity', equity)
        if getattr(ts, 'tzinfo', None) is None:
            raise ValueError('Risk timestamp must be timezone-aware')
        ts=ts.astimezone(timezone.utc)
        if self.last_timestamp is not None and ts < self.last_timestamp:
            raise ValueError('Risk events must be chronological')
        day=ts.date(); week=tuple(ts.isocalendar()[:2])
        # Attribute opening gaps to the new period, measured from the prior close.
        if day != self.day:
            self.day=day; self.day_start=self.last_equity; self.daily=False
        if week != self.week:
            self.week=week; self.week_start=self.last_equity; self.weekly=False
        self.peak=max(self.peak,equity)
        self.drawdown=equity/self.peak-1
        self.max_drawdown=min(self.max_drawdown,self.drawdown)
        self.daily |= equity <= self.day_start*(1-self.p.daily_loss_limit)
        self.weekly |= equity <= self.week_start*(1-self.p.weekly_loss_limit)
        self.hard |= equity <= self.peak*(1-self.p.max_drawdown)
        self.last_equity=equity; self.last_timestamp=ts
        return self.daily or self.weekly or self.hard
