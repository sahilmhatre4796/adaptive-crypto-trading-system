"""M8 offline risk proposals, cost-aware sizing and capital reservations."""
from dataclasses import dataclass
from decimal import Decimal, ROUND_CEILING, ROUND_FLOOR

import pandas as pd

from trading.pattern_context import utc_timestamp
from trading.portfolio import (Allocation, InstrumentSpec, Portfolio, PortfolioSnapshot,
                               decimal_math, identifier, number, step, text, wire)


@dataclass(frozen=True)
class SignalProposal:
    signal_id: str
    formation_id: str
    decision_id: str
    source_policy_id: str
    source_input_id: str
    symbol: str
    side: str
    reference_price: Decimal
    stop_price: Decimal
    available_at: pd.Timestamp
    expires_at: pd.Timestamp
    mode: str
    terminal_at: pd.Timestamp | None = None

    def __post_init__(self):
        for name in ('signal_id', 'formation_id', 'decision_id', 'source_policy_id', 'source_input_id'):
            text(getattr(self, name))
        if self.symbol not in ('BTC/USDT', 'ETH/USDT') or self.side not in ('LONG', 'SHORT'):
            raise ValueError('Directional BTC/ETH proposal required')
        if self.mode not in ('BACKTEST', 'CURRENT'):
            raise ValueError('Explicit upstream mode required; both remain offline')
        for name in ('reference_price', 'stop_price'):
            object.__setattr__(self, name, number(getattr(self, name), positive=True))
        for name in ('available_at', 'expires_at', 'terminal_at'):
            if getattr(self, name) is not None:
                object.__setattr__(self, name, utc_timestamp(getattr(self, name)))
        if self.expires_at <= self.available_at or (self.terminal_at is not None and self.terminal_at < self.available_at):
            raise ValueError('Invalid proposal availability interval')


def proposal_from_m7(record, *, as_of, events=()) -> SignalProposal:
    """Isolated boundary for independently accepted and committed M7-v1 records."""
    from trading.signals import SignalLifecycleEvent, SignalRecord
    at = utc_timestamp(as_of)
    if (not isinstance(record, SignalRecord) or record.schema_version != 'M7-v1'
            or record.lifecycle_state != 'QUALIFIED' or record.qualification_passed is not True
            or record.signal_id is None or record.decision not in ('LONG', 'SHORT')
            or record.candidate_side != record.decision or record.available_at > at):
        raise ValueError('An available qualified M7-v1 SignalRecord is required')
    terminals = []
    for event in events:
        if not isinstance(event, SignalLifecycleEvent) or event.kind not in ('INVALIDATED', 'EXPIRED'):
            raise ValueError('Typed M7 lifecycle events required')
        if event.signal_id == record.signal_id and event.available_at <= at:
            terminals.append(event.available_at)
    return SignalProposal(record.signal_id, record.pattern_id, record.decision_id, record.policy_id,
                          record.input_id, record.symbol, record.decision, record.reference_price,
                          record.invalidation_price, record.available_at, record.expires_at, record.mode,
                          min(terminals) if terminals else None)


@dataclass(frozen=True)
class CostModel:
    entry_fee_rate: Decimal
    exit_fee_rate: Decimal
    entry_slippage_rate: Decimal
    stop_slippage_rate: Decimal
    funding_debit_rate: Decimal  # Total estimated debit over the planned holding horizon.
    source: str
    available_at: pd.Timestamp

    def __post_init__(self):
        for name in ('entry_fee_rate', 'exit_fee_rate', 'entry_slippage_rate', 'stop_slippage_rate', 'funding_debit_rate'):
            value = number(getattr(self, name), nonnegative=True)
            if name != 'funding_debit_rate' and value >= 1:
                raise ValueError('Cost fractions must be below one')
            object.__setattr__(self, name, value)
        text(self.source)
        object.__setattr__(self, 'available_at', utc_timestamp(self.available_at))


@dataclass(frozen=True)
class RiskConfig:
    max_open_positions: int
    max_gross_exposure: Decimal  # Multiples of current equity; gross, never netted.
    max_symbol_exposure: Decimal
    max_group_exposure: Decimal
    max_portfolio_risk: Decimal
    daily_loss_fraction: Decimal
    max_drawdown_fraction: Decimal
    max_consecutive_losses: int
    cooldown_seconds: Decimal
    max_quote_age_seconds: Decimal
    max_fx_age_seconds: Decimal
    max_spec_age_seconds: Decimal
    max_leverage: Decimal
    risk_fraction: Decimal = Decimal('.10')  # Explicit user-selected experimental policy.

    def __post_init__(self):
        for name in ('max_open_positions', 'max_consecutive_losses'):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, int) or value < 1:
                raise ValueError('Positive integer risk limit required')
        for name in vars(self):
            if name in ('max_open_positions', 'max_consecutive_losses'):
                continue
            value = number(getattr(self, name), positive=True)
            if name in ('risk_fraction', 'max_portfolio_risk', 'daily_loss_fraction', 'max_drawdown_fraction') and value > 1:
                raise ValueError('Risk fraction cannot exceed one')
            object.__setattr__(self, name, value)
        if self.max_leverage < 1:
            raise ValueError('Leverage must be at least one')


@dataclass(frozen=True)
class RiskDecision:
    decision_id: str
    schema_version: str
    status: str
    reason_codes: tuple[str, ...]
    available_at: pd.Timestamp
    signal_id: str
    configuration_id: str
    portfolio_id: str
    input_id: str
    equity_reference: Decimal
    risk_budget: Decimal
    quantity: Decimal
    entry_price: Decimal | None
    stop_fill: Decimal | None
    notional_account: Decimal
    margin: Decimal
    capital_reserved: Decimal
    planned_loss: Decimal
    estimated_fees: Decimal
    estimated_slippage: Decimal
    estimated_funding: Decimal
    allocation_id: str | None
    proposal: SignalProposal
    instrument: InstrumentSpec
    costs: CostModel
    leverage: Decimal
    requested_quantity: Decimal | None
    portfolio: PortfolioSnapshot

    def to_dict(self):
        return wire(self)


def duration(seconds):
    return pd.Timedelta(format(seconds, 'f')+'s')


@dataclass(frozen=True)
class TerminalReceipt:
    receipt_id: str
    available_at: pd.Timestamp
    signal_id: str
    source_event_id: str
    source_available_at: pd.Timestamp
    kind: str
    reason: str
    input_id: str
    cancelled_allocations: tuple[str, ...]
    open_allocations: tuple[str, ...]


class RiskEngine:
    """A single-portfolio offline risk actor. Approved decisions atomically reserve.

    Repeated identical calls return their original audit decision, not a new grant.
    Every adjudicated formation is consumed, including rejected/blocked proposals.
    """
    def __init__(self, portfolio: Portfolio, config: RiskConfig, *, mode):
        if not isinstance(portfolio, Portfolio) or not isinstance(config, RiskConfig) or mode not in ('BACKTEST', 'CURRENT'):
            raise ValueError('Typed portfolio, explicit policy and mode required')
        if getattr(portfolio, '_risk_owned', False):
            raise ValueError('Portfolio already has a risk actor; use a fresh causal replay')
        self.portfolio, self.config, self.mode = portfolio, config, mode
        self.configuration_id = identifier('M8-v1', config, mode, portfolio.currency, portfolio.initial_cash)
        self._history, self._cache, self._signals, self._formations = [], {}, {}, set()
        self._terminal_events = {}
        self._terminal_history = []
        self._last_at = portfolio._last_at
        portfolio._risk_owned = True

    @property
    def history(self):
        return tuple(self._history)

    @property
    def terminal_history(self):
        return tuple(self._terminal_history)

    @decimal_math
    def evaluate(self, proposal: SignalProposal, spec: InstrumentSpec, costs: CostModel, *,
                 leverage, as_of, requested_quantity=None) -> RiskDecision:
        if not isinstance(proposal, SignalProposal) or not isinstance(spec, InstrumentSpec) or not isinstance(costs, CostModel):
            raise ValueError('Typed proposal, instrument specification and cost assumptions required')
        at = utc_timestamp(as_of)
        if max(proposal.available_at, spec.available_at, costs.available_at) > at:
            raise ValueError('Risk inputs unavailable at cutoff')
        if proposal.terminal_at is not None and proposal.terminal_at > at:
            raise ValueError('Future terminal evidence must not enter a risk proposal')
        leverage = number(leverage, positive=True)
        requested = None if requested_quantity is None else number(requested_quantity, positive=True)
        input_id = identifier(proposal, spec, costs, leverage, at, requested)
        if input_id in self._cache:
            return self._cache[input_id]
        if at < self._last_at:
            raise ValueError('Risk decisions must be chronological')
        snap = self.portfolio.snapshot(as_of=at)
        cfg = self.config
        equity, budget = snap.equity, max(Decimal(0), snap.equity*cfg.risk_fraction)
        portfolio_id = identifier(snap)
        reasons, blocked = [], False
        if proposal.signal_id in self._signals:
            reasons.append('SIGNAL_ALREADY_CONSUMED')
            blocked = True
        if (proposal.symbol, proposal.formation_id) in self._formations:
            reasons.append('FORMATION_ALREADY_CONSUMED')
            blocked = True
        if proposal.mode != self.mode:
            reasons.append('MODE_MISMATCH')
        if spec.symbol != proposal.symbol:
            reasons.append('SPEC_SYMBOL_MISMATCH')
        if at >= proposal.expires_at:
            reasons.append('SIGNAL_EXPIRED')
        if (proposal.terminal_at is not None and proposal.terminal_at <= at) or proposal.signal_id in self._terminal_events:
            reasons.append('SIGNAL_TERMINAL')
        if at-spec.available_at > duration(cfg.max_spec_age_seconds):
            reasons.append('SPEC_STALE')
        if leverage < 1 or leverage > min(cfg.max_leverage, spec.max_leverage):
            reasons.append('LEVERAGE_LIMIT')
        if equity <= 0:
            reasons.append('NONPOSITIVE_EQUITY')
            blocked = True
        if snap.day_low_equity <= snap.day_start_equity*(1-cfg.daily_loss_fraction):
            reasons.append('DAILY_LOSS_HALT')
            blocked = True
        if snap.max_drawdown >= cfg.max_drawdown_fraction:
            reasons.append('DRAWDOWN_HALT')
            blocked = True
        if (snap.consecutive_losses >= cfg.max_consecutive_losses and snap.last_loss_at is not None
                and at-snap.last_loss_at < duration(cfg.cooldown_seconds)):
            reasons.append('LOSS_STREAK_COOLDOWN')
            blocked = True
        active = [p.plan for p in snap.positions]+list(snap.reservations)
        if len(active) >= cfg.max_open_positions:
            reasons.append('MAX_OPEN_POSITIONS')
            blocked = True
        if any(p.spec.symbol == proposal.symbol for p in active):
            reasons.append('SYMBOL_ALREADY_ALLOCATED')
            blocked = True
        marks, rates = {q.key: q for q in snap.marks}, {q.key: q for q in snap.fx_rates}
        mark, fx = marks.get(proposal.symbol), rates.get(spec.quote_currency)
        symbols = {p.spec.symbol for p in active}|{proposal.symbol}
        currencies = {p.spec.quote_currency for p in active}|{spec.quote_currency}
        if any(s not in marks for s in symbols) or any(c not in rates for c in currencies):
            reasons.append('MARK_OR_FX_MISSING')
        if any(s in marks and at-marks[s].observed_at > duration(cfg.max_quote_age_seconds) for s in symbols):
            reasons.append('MARK_STALE')
        if any(c in rates and at-rates[c].observed_at > duration(cfg.max_fx_age_seconds) for c in currencies):
            reasons.append('FX_STALE')

        qty = fees = slippage = funding = loss = notional = margin = capital = Decimal(0)
        entry = stop_fill = None
        status, plan = 'REJECTED', None
        if not reasons:
            direction = 1 if proposal.side == 'LONG' else -1
            trigger = step(proposal.stop_price, spec.price_tick, ROUND_FLOOR if direction == 1 else ROUND_CEILING)
            if trigger <= 0 or direction*(mark.value-trigger) <= 0:
                reasons.append('STOP_NOT_PROTECTIVE')
            else:
                entry = step(mark.value*(1+direction*costs.entry_slippage_rate), spec.price_tick,
                             ROUND_CEILING if direction == 1 else ROUND_FLOOR)
                stop_fill = step(trigger*(1-direction*costs.stop_slippage_rate), spec.price_tick,
                                 ROUND_FLOOR if direction == 1 else ROUND_CEILING)
                if min(entry, stop_fill) <= 0:
                    reasons.append('INVALID_MODELED_FILL')
                else:
                    unit = spec.multiplier*fx.value
                    unit_notional = entry*unit
                    unit_entry_fee = unit_notional*costs.entry_fee_rate
                    unit_fees = unit_entry_fee+stop_fill*unit*costs.exit_fee_rate
                    unit_slip = (abs(entry-mark.value)+abs(trigger-stop_fill))*unit
                    unit_funding = max(entry, stop_fill)*unit*costs.funding_debit_rate
                    unit_loss = direction*(entry-stop_fill)*unit+unit_fees+unit_funding
                    unit_margin = unit_notional/leverage
                    unit_capital = unit_margin+unit_entry_fee+abs(entry-mark.value)*unit
                    caps = [('PER_TRADE_RISK', budget/unit_loss),
                            ('AVAILABLE_CAPITAL', max(Decimal(0), snap.available_capital)/unit_capital),
                            ('GROSS_EXPOSURE', max(Decimal(0), equity*cfg.max_gross_exposure-snap.gross_exposure)/unit_notional),
                            ('SYMBOL_EXPOSURE', max(Decimal(0), equity*cfg.max_symbol_exposure-dict(snap.symbol_exposure).get(proposal.symbol, 0))/unit_notional),
                            ('CORRELATED_EXPOSURE', max(Decimal(0), equity*cfg.max_group_exposure-dict(snap.group_exposure).get(spec.correlation_group, 0))/unit_notional),
                            ('AGGREGATE_STOP_RISK', max(Decimal(0), equity*cfg.max_portfolio_risk-snap.stop_risk)/unit_loss),
                            ('DAILY_LOSS_HEADROOM', max(Decimal(0), equity-snap.day_start_equity*(1-cfg.daily_loss_fraction)-snap.stop_risk)/unit_loss),
                            ('DRAWDOWN_HEADROOM', max(Decimal(0), equity-snap.peak_equity*(1-cfg.max_drawdown_fraction)-snap.stop_risk)/unit_loss)]
                    desired = requested if requested is not None else step(budget/unit_loss, spec.quantity_step)
                    ceiling = min([v for _, v in caps]+[desired])
                    qty = step(ceiling, spec.quantity_step)
                    if qty <= 0 or qty < spec.min_quantity or qty*entry*spec.multiplier < spec.min_notional:
                        reasons.append('EXCHANGE_MINIMUM_NOT_AFFORDABLE')
                        reasons.extend(name+'_LIMIT' for name, value in caps if value <= ceiling)
                        qty = Decimal(0)
                    else:
                        fees, slippage, funding = qty*unit_fees, qty*unit_slip, qty*unit_funding
                        loss, notional, margin, capital = qty*unit_loss, qty*unit_notional, qty*unit_margin, qty*unit_capital
                        if loss > budget or capital > snap.available_capital:
                            raise ValueError('Decimal sizing exceeded a hard budget')
                        status = 'REDUCED' if qty < desired else 'APPROVED'
                        reasons.append('WITHIN_RISK_BUDGET')
                        if status == 'REDUCED':
                            reasons.extend(name+'_LIMIT' for name, value in caps if value < desired)
                            if step(desired, spec.quantity_step) != desired:
                                reasons.append('QUANTITY_PRECISION_REDUCTION')
                        plan = Allocation(identifier('M8-allocation-v1', input_id, portfolio_id, self.configuration_id),
                                          proposal.signal_id, proposal.formation_id, spec, proposal.side, qty,
                                          entry, stop_fill, fx.value, margin, capital, loss, qty*unit_entry_fee,
                                          costs.exit_fee_rate, funding, cfg.risk_fraction, at, proposal.expires_at,
                                          min(mark.observed_at+duration(cfg.max_quote_age_seconds),
                                              fx.observed_at+duration(cfg.max_fx_age_seconds),
                                              spec.available_at+duration(cfg.max_spec_age_seconds)),
                                          identifier(mark), identifier(fx), costs.stop_slippage_rate)
        if blocked:
            status = 'BLOCKED'
        reasons = tuple(sorted(set(reasons)))
        decision_id = identifier('M8-decision-v1', input_id, portfolio_id, self.configuration_id, status, reasons, qty)
        decision = RiskDecision(decision_id, 'M8-v1', status, reasons, at, proposal.signal_id,
                                self.configuration_id, portfolio_id, input_id, equity, budget, qty,
                                entry, stop_fill, notional, margin, capital, loss, fees, slippage, funding,
                                plan.allocation_id if plan else None, proposal, spec, costs, leverage, requested, snap)
        if plan:
            self.portfolio._reserve(plan, as_of=at)
        self._history.append(decision)
        self._cache[input_id] = decision
        self._signals.setdefault(proposal.signal_id, proposal)
        self._formations.add((proposal.symbol, proposal.formation_id))
        self._last_at = at
        return decision

    def observe_m7_terminal(self, event, *, as_of):
        """Cancel unfilled reservations only; never execute an open position exit."""
        from trading.signals import SignalLifecycleEvent
        at = utc_timestamp(as_of)
        if not isinstance(event, SignalLifecycleEvent) or event.kind not in ('INVALIDATED', 'EXPIRED') or event.available_at > at:
            raise ValueError('Available M7 terminal event required')
        self.portfolio._time(at)
        if at < self._last_at:
            raise ValueError('Risk events must be chronological')
        prior = self._terminal_events.get(event.signal_id)
        if prior is not None:
            if prior != event:
                raise ValueError('Cannot revise a terminal signal event')
            self._last_at = at
            return
        known = self._signals.get(event.signal_id)
        if known and event.available_at < known.available_at:
            raise ValueError('Terminal event precedes signal availability')
        snap = self.portfolio.snapshot(as_of=at)
        cancelled = []
        for plan in snap.reservations:
            if plan.signal_id == event.signal_id:
                self.portfolio.cancel(plan.allocation_id, as_of=at, event_id=identifier('M8-terminal', event))
                cancelled.append(plan.allocation_id)
        self._terminal_history.append(TerminalReceipt(identifier('M8-terminal-receipt', event, at), at,
                                                     event.signal_id, event.event_id, event.available_at,
                                                     event.kind, event.reason, identifier(event),
                                                     tuple(cancelled), tuple(p.plan.allocation_id for p in snap.positions
                                                                             if p.plan.signal_id == event.signal_id)))
        self._terminal_events[event.signal_id] = event
        self._last_at = at

    @decimal_math
    def simulate_open(self, allocation_id, *, as_of, event_id):
        """Revalidate a reservation before offline booking; never submit an order."""
        at = utc_timestamp(as_of)
        if at < self._last_at:
            raise ValueError('Risk events must be chronological')
        snap, cfg = self.portfolio.snapshot(as_of=at), self.config
        plans = list(snap.reservations)+[p.plan for p in snap.positions]
        marks, rates = {q.key: q for q in snap.marks}, {q.key: q for q in snap.fx_rates}
        halted = (snap.equity <= 0 or snap.day_low_equity <= snap.day_start_equity*(1-cfg.daily_loss_fraction)
                  or snap.max_drawdown >= cfg.max_drawdown_fraction
                  or (snap.consecutive_losses >= cfg.max_consecutive_losses and snap.last_loss_at is not None
                      and at-snap.last_loss_at < duration(cfg.cooldown_seconds)))
        if (halted or snap.available_capital < 0 or snap.gross_exposure > snap.equity*cfg.max_gross_exposure
                or snap.stop_risk > snap.equity*cfg.max_portfolio_risk):
            raise ValueError('Current portfolio limits block simulated opening')
        if (any(v > snap.equity*cfg.max_symbol_exposure for _, v in snap.symbol_exposure)
                or any(v > snap.equity*cfg.max_group_exposure for _, v in snap.group_exposure)
                or snap.stop_risk > snap.equity-snap.day_start_equity*(1-cfg.daily_loss_fraction)
                or snap.stop_risk > snap.equity-snap.peak_equity*(1-cfg.max_drawdown_fraction)):
            raise ValueError('Current concentration or stop-risk headroom blocks simulated opening')
        if any(at-marks[p.spec.symbol].observed_at > duration(cfg.max_quote_age_seconds)
               or at-rates[p.spec.quote_currency].observed_at > duration(cfg.max_fx_age_seconds) for p in plans):
            raise ValueError('Portfolio mark/FX evidence stale at simulated opening')
        event = self.portfolio.simulate_open(allocation_id, as_of=at, event_id=event_id)
        self._last_at = at
        return event
