from dataclasses import dataclass

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

    @property
    def direction(self):
        return 1 if self.side == 'LONG' else -1

class Ledger:
    def __init__(self, capital, fee=0, slip=0):
        self.balance, self.fee, self.slip = capital, fee, slip
        self.position = None
        self.fills = []
        self.funding_events = []

    def open(self, side, price, qty, stop, ts):
        if self.position or side not in ('LONG','SHORT') or qty <= 0:
            raise ValueError('Invalid entry')
        direction = 1 if side == 'LONG' else -1
        price *= 1 + direction * self.slip
        cost = qty * price * self.fee
        self.position = Position(side, price, qty, stop, qty, fees=cost)
        self.balance -= cost
        self.fills.append(dict(timestamp=ts,kind='ENTRY',qty=qty,price=price,fee=cost,gross=0))

    def close(self, qty, price, ts, reason):
        p = self.position
        if p is None or qty <= 0 or qty > p.qty:
            raise ValueError('Invalid exit quantity')
        price *= 1 - p.direction * self.slip
        gross = p.direction * qty * (price-p.entry)
        fee = qty * price * self.fee
        p.gross += gross; p.fees += fee; p.qty -= qty
        self.balance += gross-fee
        self.fills.append(dict(timestamp=ts,kind=reason,qty=qty,price=price,fee=fee,gross=gross))
        if p.qty == 0:
            self.position = None
            return dict(pnl=p.gross-p.fees+p.funding,gross=p.gross,fees=p.fees,funding=p.funding)

    def fund(self, rate, mark, ts):
        if self.position:
            p = self.position
            cash = -p.direction*p.qty*mark*rate
            self.balance += cash; p.funding += cash
            self.funding_events.append(dict(timestamp=ts,rate=rate,mark=mark,qty=p.qty,cash=cash))

    def equity(self, mark):
        p = self.position
        return self.balance + (p.direction*p.qty*(mark-p.entry) if p else 0)

class Halts:
    def __init__(self, p):
        self.p=p; self.peak=p.initial_capital
        self.day=self.week=None
        self.daily=self.weekly=self.hard=False

    def update(self, ts, equity):
        day=ts.date(); week=tuple(ts.isocalendar()[:2])
        if day != self.day:
            self.day=day; self.day_start=equity; self.daily=False
        if week != self.week:
            self.week=week; self.week_start=equity; self.weekly=False
        self.peak=max(self.peak,equity)
        self.daily |= equity <= self.day_start*(1-self.p.daily_loss_limit)
        self.weekly |= equity <= self.week_start*(1-self.p.weekly_loss_limit)
        self.hard |= equity <= self.peak*(1-self.p.max_drawdown)
        return self.daily or self.weekly or self.hard
