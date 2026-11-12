"""M8 offline linear-contract accounting. No exchange or execution connection."""
from dataclasses import asdict, dataclass, replace
from decimal import Context, Decimal, InvalidOperation, ROUND_CEILING, ROUND_FLOOR, localcontext
from functools import wraps
from hashlib import sha256
import json

import pandas as pd

from trading.pattern_context import utc_timestamp


def number(value, *, positive=False, nonnegative=False) -> Decimal:
    if isinstance(value, bool) or not isinstance(value, (Decimal, str, int, float)):
        raise ValueError('Finite decimal number required')
    try:
        result = Decimal(str(value))
    except InvalidOperation as exc:
        raise ValueError('Invalid decimal number') from exc
    if not result.is_finite() or (positive and result <= 0) or (nonnegative and result < 0):
        raise ValueError('Invalid decimal magnitude')
    return result


def wire(value):
    if hasattr(value, '__dataclass_fields__'):
        return wire(asdict(value))
    if isinstance(value, Decimal):
        if value.is_zero():
            return '0'
        encoded = format(value, 'f')
        return encoded.rstrip('0').rstrip('.') if '.' in encoded else encoded
    if isinstance(value, pd.Timestamp):
        return value.isoformat()
    if isinstance(value, dict):
        return {str(k): wire(v) for k, v in value.items()}
    if isinstance(value, (tuple, list)):
        return [wire(v) for v in value]
    return value


def identifier(*values) -> str:
    return sha256(json.dumps(wire(values), sort_keys=True, allow_nan=False).encode()).hexdigest()


def text(value):
    if not isinstance(value, str) or not value.strip():
        raise ValueError('Nonempty identifier required')


def decimal_math(function):
    @wraps(function)
    def calculate(*args, **kwargs):
        with localcontext(Context(prec=40)):
            try:
                return function(*args, **kwargs)
            except ArithmeticError as exc:
                raise ValueError('Invalid or overflowing M8 arithmetic') from exc
    return calculate


def step(value, increment, rounding=ROUND_FLOOR):
    return (value/increment).to_integral_value(rounding=rounding)*increment


@dataclass(frozen=True)
class Quote:
    key: str  # Asset symbol for a mark, quote currency for an FX rate.
    value: Decimal
    observed_at: pd.Timestamp
    available_at: pd.Timestamp
    source: str

    def __post_init__(self):
        text(self.key)
        text(self.source)
        object.__setattr__(self, 'value', number(self.value, positive=True))
        for name in ('observed_at', 'available_at'):
            object.__setattr__(self, name, utc_timestamp(getattr(self, name)))
        if self.observed_at > self.available_at:
            raise ValueError('Observation cannot follow receipt')


@dataclass(frozen=True)
class InstrumentSpec:
    symbol: str
    quote_currency: str
    multiplier: Decimal  # Quote P&L per price unit per contract.
    quantity_step: Decimal
    price_tick: Decimal
    min_quantity: Decimal
    min_notional: Decimal  # Quote currency, not account currency.
    max_leverage: Decimal
    correlation_group: str
    source: str
    available_at: pd.Timestamp

    def __post_init__(self):
        if self.symbol not in ('BTC/USDT', 'ETH/USDT') or self.quote_currency != 'USDT':
            raise ValueError('M8-v1 supports explicit linear BTC/ETH USDT contracts only')
        for name in ('correlation_group', 'source'):
            text(getattr(self, name))
        for name in ('multiplier', 'quantity_step', 'price_tick', 'max_leverage'):
            object.__setattr__(self, name, number(getattr(self, name), positive=True))
        for name in ('min_quantity', 'min_notional'):
            object.__setattr__(self, name, number(getattr(self, name), nonnegative=True))
        if self.max_leverage < 1:
            raise ValueError('Leverage must be at least one')
        object.__setattr__(self, 'available_at', utc_timestamp(self.available_at))


@dataclass(frozen=True)
class Allocation:
    allocation_id: str
    signal_id: str
    formation_id: str
    spec: InstrumentSpec
    side: str
    quantity: Decimal
    entry: Decimal
    stop_fill: Decimal
    fx: Decimal
    margin: Decimal
    capital: Decimal
    planned_risk: Decimal
    entry_fee: Decimal
    exit_fee_rate: Decimal
    funding_estimate: Decimal
    risk_fraction: Decimal
    available_at: pd.Timestamp
    expires_at: pd.Timestamp
    data_valid_until: pd.Timestamp
    mark_id: str
    fx_id: str
    stop_slippage_rate: Decimal = Decimal(0)


@dataclass(frozen=True)
class PortfolioPosition:
    plan: Allocation
    opened_at: pd.Timestamp
    funding_cash: Decimal = Decimal(0)
    funding_debits: Decimal = Decimal(0)


@dataclass(frozen=True)
class PortfolioSnapshot:
    as_of: pd.Timestamp
    currency: str
    cash: Decimal
    realized_pnl: Decimal
    unrealized_pnl: Decimal
    fees_paid: Decimal
    funding_cash: Decimal
    equity: Decimal
    used_margin: Decimal
    reserved_capital: Decimal
    available_capital: Decimal
    gross_exposure: Decimal
    stop_risk: Decimal
    symbol_exposure: tuple
    group_exposure: tuple
    positions: tuple[PortfolioPosition, ...]
    reservations: tuple[Allocation, ...]
    marks: tuple[Quote, ...]
    fx_rates: tuple[Quote, ...]
    peak_equity: Decimal
    max_drawdown: Decimal
    day_start_equity: Decimal
    day_low_equity: Decimal
    consecutive_losses: int
    last_loss_at: pd.Timestamp | None


@dataclass(frozen=True)
class PortfolioEvent:
    event_id: str
    kind: str
    available_at: pd.Timestamp
    input_id: str
    snapshot: PortfolioSnapshot


class Portfolio:
    """Append-only offline journal; opening/closing records are simulated accounting.

    All money is in account currency. FX quotes are account currency per USDT.
    No position notional is debited: these are margined linear contracts.
    """
    def __init__(self, *, as_of, cash=2000, currency='INR'):
        text(currency)
        self.currency = currency
        self.initial_cash = number(cash, positive=True)
        self._cash = self.initial_cash
        self._realized = self._fees = self._funding = Decimal(0)
        self._positions, self._reservations, self._marks, self._fx = {}, {}, {}, {}
        self._history, self._events = [], {}
        self._last_at = utc_timestamp(as_of)
        self._peak = self._last_equity = self.initial_cash
        self._drawdown = Decimal(0)
        self._day = self._last_at.date()
        self._day_start = self._day_low = self.initial_cash
        self._losses, self._last_loss = 0, None

    @property
    def history(self):
        return tuple(self._history)

    def _time(self, as_of):
        at = utc_timestamp(as_of)
        if at < self._last_at:
            raise ValueError('Portfolio events must be chronological')
        return at

    def _prior(self, event_id, kind, payload, at):
        text(event_id)
        fingerprint = identifier(kind, payload, at)
        if event_id in self._events:
            event = self._events[event_id]
            if event.input_id != fingerprint:
                raise ValueError('Cannot revise a portfolio event')
            return event
        self._time(at)
        return None

    @decimal_math
    def snapshot(self, *, as_of):
        at = self._time(as_of)
        unrealized, margin, reserved, exposure, risk = (Decimal(0) for _ in range(5))
        symbols, groups = {}, {}
        for position in self._positions.values():
            p = position.plan
            mark = self._marks[p.spec.symbol].value
            fx = self._fx[p.spec.quote_currency].value
            direction = 1 if p.side == 'LONG' else -1
            unrealized += direction*(mark-p.entry)*p.quantity*p.spec.multiplier*fx
            margin += p.margin
            value = mark*p.quantity*p.spec.multiplier*fx
            exposure += value
            market_exit = step(mark*(1-direction*p.stop_slippage_rate), p.spec.price_tick,
                               ROUND_FLOOR if direction == 1 else ROUND_CEILING)
            exit_price = min(p.stop_fill, market_exit) if direction == 1 else max(p.stop_fill, market_exit)
            risk += direction*(mark-exit_price)*p.quantity*p.spec.multiplier*fx
            risk += exit_price*p.quantity*p.spec.multiplier*fx*p.exit_fee_rate
            risk += max(Decimal(0), p.funding_estimate-position.funding_debits)
            symbols[p.spec.symbol] = symbols.get(p.spec.symbol, Decimal(0))+value
            groups[p.spec.correlation_group] = groups.get(p.spec.correlation_group, Decimal(0))+value
        for p in self._reservations.values():
            reserved += p.capital
            value = p.entry*p.quantity*p.spec.multiplier*p.fx
            exposure += value
            risk += p.planned_risk
            symbols[p.spec.symbol] = symbols.get(p.spec.symbol, Decimal(0))+value
            groups[p.spec.correlation_group] = groups.get(p.spec.correlation_group, Decimal(0))+value
        equity = number(self._cash+unrealized)
        peak = max(self._peak, equity)
        drawdown = max(self._drawdown, 1-equity/peak)
        start = self._day_start if at.date() == self._day else self._last_equity
        low = min(self._day_low, equity) if at.date() == self._day else equity
        for value in (unrealized, margin, reserved, exposure, risk, peak, drawdown, start, low):
            number(value)
        return PortfolioSnapshot(at, self.currency, self._cash, self._realized, unrealized, self._fees,
                                 self._funding, equity, margin, reserved, equity-margin-reserved,
                                 exposure, risk, tuple(sorted(symbols.items())), tuple(sorted(groups.items())),
                                 tuple(self._positions.values()), tuple(self._reservations.values()),
                                 tuple(self._marks.values()), tuple(self._fx.values()), peak, drawdown,
                                 start, low, self._losses, self._last_loss)

    def _commit(self, event_id, kind, payload, at, **changes):
        # Build and validate on a detached copy so a rejected arithmetic/input path is atomic.
        draft = object.__new__(Portfolio)
        draft.__dict__ = {**self.__dict__, **changes}
        snap = draft.snapshot(as_of=at)
        draft._last_at, draft._last_equity = at, snap.equity
        draft._peak, draft._drawdown = snap.peak_equity, snap.max_drawdown
        draft._day, draft._day_start, draft._day_low = at.date(), snap.day_start_equity, snap.day_low_equity
        event = PortfolioEvent(event_id, kind, at, identifier(kind, payload, at), snap)
        draft._history = [*self._history, event]
        draft._events = {**self._events, event_id: event}
        self.__dict__ = draft.__dict__
        return event

    def observe(self, quote: Quote, *, kind, as_of, event_id):
        at = utc_timestamp(as_of)
        if not isinstance(quote, Quote) or kind not in ('MARK', 'FX'):
            raise ValueError('Typed mark/FX observation required')
        prior = self._prior(event_id, kind, quote, at)
        if prior:
            return prior
        if quote.available_at > at:
            raise ValueError('Quote unavailable at cutoff')
        if kind == 'MARK' and quote.key not in ('BTC/USDT', 'ETH/USDT'):
            raise ValueError('Unsupported mark asset')
        if kind == 'FX' and quote.key != 'USDT':
            raise ValueError('FX quote must be account currency per USDT')
        book = self._marks if kind == 'MARK' else self._fx
        previous = book.get(quote.key)
        if previous and (quote.observed_at < previous.observed_at
                         or (quote.observed_at == previous.observed_at and quote != previous)):
            raise ValueError('Cannot revise or reorder a quote observation')
        return self._commit(event_id, kind, quote, at,
                            **{('_marks' if kind == 'MARK' else '_fx'): {**book, quote.key: quote}})

    def _reserve(self, plan: Allocation, *, as_of):
        at = self._time(as_of)
        if plan.allocation_id in self._events or plan.allocation_id in self._reservations or plan.allocation_id in self._positions:
            raise ValueError('Allocation already consumed')
        if plan.capital > self.snapshot(as_of=at).available_capital:
            raise ValueError('Insufficient reservable capital')
        return self._commit(plan.allocation_id, 'RESERVE', plan, at,
                            _reservations={**self._reservations, plan.allocation_id: plan})

    def cancel(self, allocation_id, *, as_of, event_id):
        at = utc_timestamp(as_of)
        prior = self._prior(event_id, 'CANCEL', allocation_id, at)
        if prior:
            return prior
        if allocation_id not in self._reservations:
            raise ValueError('Unknown or consumed reservation')
        return self._commit(event_id, 'CANCEL', allocation_id, at,
                            _reservations={k: v for k, v in self._reservations.items() if k != allocation_id})

    @decimal_math
    def simulate_open(self, allocation_id, *, as_of, event_id):
        at = utc_timestamp(as_of)
        prior = self._prior(event_id, 'SIMULATED_OPEN', allocation_id, at)
        if prior:
            return prior
        if allocation_id not in self._reservations:
            raise ValueError('Unknown or consumed reservation')
        p = self._reservations[allocation_id]
        snap = self.snapshot(as_of=at)
        if at >= p.expires_at or p.planned_risk > snap.equity*p.risk_fraction:
            raise ValueError('Reservation expired or current equity no longer supports risk')
        if at > p.data_valid_until:
            raise ValueError('Reservation source data is stale')
        if identifier(self._marks[p.spec.symbol]) != p.mark_id or identifier(self._fx[p.spec.quote_currency]) != p.fx_id:
            raise ValueError('Market/FX changed; cancel the stale allocation')
        return self._commit(event_id, 'SIMULATED_OPEN', allocation_id, at,
                            _cash=number(self._cash-p.entry_fee), _realized=self._realized-p.entry_fee,
                            _fees=self._fees+p.entry_fee,
                            _positions={**self._positions, allocation_id: PortfolioPosition(p, at)},
                            _reservations={k: v for k, v in self._reservations.items() if k != allocation_id})

    @decimal_math
    def settle_funding(self, allocation_id, cash_flow, *, as_of, event_id):
        at, amount = utc_timestamp(as_of), number(cash_flow)
        payload = (allocation_id, amount)
        prior = self._prior(event_id, 'FUNDING', payload, at)
        if prior:
            return prior
        if allocation_id not in self._positions:
            raise ValueError('Funding requires an open position')
        position = self._positions[allocation_id]
        return self._commit(event_id, 'FUNDING', payload, at,
                            _cash=number(self._cash+amount), _realized=self._realized+amount,
                            _funding=self._funding+amount,
                            _positions={**self._positions, allocation_id: replace(position,
                                        funding_cash=position.funding_cash+amount,
                                        funding_debits=position.funding_debits-min(amount, Decimal(0)))})

    @decimal_math
    def simulate_close(self, allocation_id, price, fee_rate, *, as_of, event_id):
        """Book a caller-supplied simulated exit fill, including adverse slippage already once."""
        at = utc_timestamp(as_of)
        price, fee = number(price, positive=True), number(fee_rate, nonnegative=True)
        if fee >= 1:
            raise ValueError('Fee fraction must be below one')
        payload = (allocation_id, price, fee)
        prior = self._prior(event_id, 'SIMULATED_CLOSE', payload, at)
        if prior:
            return prior
        if allocation_id not in self._positions:
            raise ValueError('Position already closed or unknown')
        position = self._positions[allocation_id]
        p = position.plan
        fx = self._fx[p.spec.quote_currency].value
        gross = (1 if p.side == 'LONG' else -1)*(price-p.entry)*p.quantity*p.spec.multiplier*fx
        cost = price*p.quantity*p.spec.multiplier*fx*fee
        net = gross-cost-p.entry_fee+position.funding_cash
        losses = self._losses+1 if net < 0 else 0
        return self._commit(event_id, 'SIMULATED_CLOSE', payload, at,
                            _cash=number(self._cash+gross-cost), _realized=self._realized+gross-cost,
                            _fees=self._fees+cost, _losses=losses,
                            _last_loss=at if net < 0 else self._last_loss,
                            _positions={k: v for k, v in self._positions.items() if k != allocation_id})
