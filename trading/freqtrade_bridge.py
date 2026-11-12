"""M9.1 offline simulation boundary. No Freqtrade runtime, client or order API."""
from dataclasses import dataclass
from decimal import Decimal

import pandas as pd

from trading.pattern_context import utc_timestamp
from trading.portfolio import Allocation, decimal_math, identifier, number, text, wire
from trading.risk import RiskDecision, RiskEngine, duration, proposal_from_m7


@dataclass(frozen=True)
class PairBinding:
    symbol: str
    pair: str
    stake_currency: str
    source: str

    def __post_init__(self):
        if self.symbol not in ('BTC/USDT', 'ETH/USDT') or self.stake_currency != 'USDT':
            raise ValueError('Explicit BTC/ETH linear-USDT binding required')
        text(self.pair)
        text(self.source)


def _source_times(record, id_field):
    text(getattr(record, id_field))
    text(record.order_id)
    text(record.source)
    for name in ('observed_at', 'available_at'):
        object.__setattr__(record, name, utc_timestamp(getattr(record, name)))
    if record.observed_at > record.available_at:
        raise ValueError('Simulation observation cannot follow receipt')


@dataclass(frozen=True)
class SimulationFill:
    fill_id: str
    order_id: str
    phase: str
    quantity: Decimal  # Contracts, not stake currency or base amount.
    price: Decimal  # Explicit simulated execution price, including slippage once.
    fee_rate: Decimal
    observed_at: pd.Timestamp
    available_at: pd.Timestamp
    source: str

    def __post_init__(self):
        _source_times(self, 'fill_id')
        if self.phase not in ('ENTRY', 'EXIT'):
            raise ValueError('Explicit ENTRY/EXIT phase required')
        for name in ('quantity', 'price'):
            object.__setattr__(self, name, number(getattr(self, name), positive=True))
        object.__setattr__(self, 'fee_rate', number(self.fee_rate, nonnegative=True))
        if self.fee_rate >= 1:
            raise ValueError('Fee fraction must be below one')


@dataclass(frozen=True)
class SimulationCancellation:
    cancellation_id: str
    order_id: str
    reason: str
    observed_at: pd.Timestamp
    available_at: pd.Timestamp
    source: str

    def __post_init__(self):
        _source_times(self, 'cancellation_id')
        text(self.reason)


@dataclass(frozen=True)
class OrderIntent:
    schema_version: str
    order_id: str
    binding: PairBinding
    created_at: pd.Timestamp
    expires_at: pd.Timestamp
    data_valid_until: pd.Timestamp
    side: str
    entry_action: str
    quantity_contracts: Decimal
    amount_base: Decimal
    notional_quote: Decimal
    stake_quote: Decimal
    entry_price: Decimal
    source_stop_price: Decimal
    modeled_stop_execution_price: Decimal
    leverage: Decimal
    allocation: Allocation
    risk: RiskDecision  # Original M7 identity, timestamps and M8 reasons/inputs retained.
    simulation_only: bool = True

    def to_dict(self):
        return wire(self)


@dataclass(frozen=True)
class AdmissionReceipt:
    receipt_id: str
    available_at: pd.Timestamp
    disposition: str
    reason_codes: tuple[str, ...]
    risk: RiskDecision
    order: OrderIntent | None

    def to_dict(self):
        return wire(self)


@dataclass(frozen=True)
class LifecycleEvent:
    event_id: str
    order_id: str
    signal_id: str
    risk_decision_id: str
    allocation_id: str
    kind: str
    previous_state: str | None
    state: str
    available_at: pd.Timestamp
    observed_at: pd.Timestamp | None
    source_available_at: pd.Timestamp
    source_id: str
    source_fingerprint: str
    reason_codes: tuple[str, ...]
    portfolio_event_id: str | None
    fill: SimulationFill | None = None
    cancellation: SimulationCancellation | None = None

    def to_dict(self):
        return wire(self)


class FreqtradeSimulationBridge:
    """Sequential in-memory actor; only registered M8 grants create simulated intents.

    This is a local M9.1 contract, not a verified Freqtrade callback/REST schema.
    The connected RiskEngine and its private journal are trusted, not user payloads.
    """
    def __init__(self, risk: RiskEngine, bindings: tuple[PairBinding, ...]):
        if (not isinstance(risk, RiskEngine) or not isinstance(bindings, tuple) or not bindings
                or any(not isinstance(b, PairBinding) for b in bindings)):
            raise ValueError('M8 actor and immutable explicit pair bindings required')
        if len({b.symbol for b in bindings}) != len(bindings) or len({b.pair for b in bindings}) != len(bindings):
            raise ValueError('Ambiguous pair bindings')
        if getattr(risk, '_m91_owned', False):
            raise ValueError('M8 actor already has an M9.1 bridge; replay a fresh actor')
        self.risk = risk
        self.bindings = tuple(sorted(bindings, key=lambda b: b.symbol))
        self.configuration_id = identifier('M9.1-v1', risk.configuration_id, self.bindings)
        self._receipts, self._events = [], []
        self._admitted, self._orders, self._states, self._commands = {}, {}, {}, {}
        self._last_at = None
        risk._m91_owned = True

    @property
    def receipts(self):
        return tuple(self._receipts)

    @property
    def history(self):
        return tuple(self._events)

    @property
    def orders(self):
        return tuple(self._orders.values())

    def state(self, order_id):
        if order_id not in self._states:
            raise ValueError('Unknown simulated order')
        return self._states[order_id]

    def _time(self, as_of):
        at = utc_timestamp(as_of)
        times = [items[-1].available_at for items in
                 (self.risk.history, self.risk.terminal_history, self.risk.portfolio.history) if items]
        if (self._last_at is not None and at < self._last_at) or any(at < t for t in times):
            raise ValueError('Integration observations must be chronological')
        self.risk.portfolio.snapshot(as_of=at)
        return at

    def _binding(self, symbol):
        for binding in self.bindings:
            if binding.symbol == symbol:
                return binding
        raise ValueError('No explicit pair binding for this asset')

    def _registered(self, decision):
        if not isinstance(decision, RiskDecision) or decision.schema_version != 'M8-v1':
            raise ValueError('A registered M8-v1 RiskDecision is required')
        registered = next((d for d in self.risk.history if d.decision_id == decision.decision_id), None)
        if registered is None or registered != decision or decision.configuration_id != self.risk.configuration_id:
            raise ValueError('Decision is absent from or differs from authoritative M8 history')

    def _reservation(self, order, at):
        current = next((p for p in self.risk.portfolio.snapshot(as_of=at).reservations
                        if p.allocation_id == order.allocation.allocation_id), None)
        if current != order.allocation:
            raise ValueError('Authoritative M8 reservation is no longer active')

    def _event(self, order, kind, state, at, *, source_id, source_fingerprint,
               source_available_at, observed_at=None, reasons=(), portfolio_event_id=None,
               fill=None, cancellation=None):
        return LifecycleEvent(identifier('M9.1-event-v1', self.configuration_id, order.order_id,
                                         kind, source_id, source_fingerprint, at),
                              order.order_id, order.risk.signal_id, order.risk.decision_id,
                              order.allocation.allocation_id, kind, self._states.get(order.order_id),
                              state, at, observed_at, source_available_at, source_id, source_fingerprint,
                              tuple(reasons), portfolio_event_id, fill, cancellation)

    def _record(self, event):
        self._events.append(event)
        self._states[event.order_id] = event.state

    @decimal_math
    def propose(self, decision: RiskDecision, *, as_of) -> AdmissionReceipt:
        at = self._time(as_of)
        self._registered(decision)
        if decision.available_at > at:
            raise ValueError('M8 decision unavailable at cutoff')
        binding = self._binding(decision.proposal.symbol)
        if decision.decision_id in self._admitted:
            self._last_at = at
            return self._admitted[decision.decision_id]
        reasons, order = list(decision.reason_codes), None
        if decision.status in ('APPROVED', 'REDUCED'):
            snap = self.risk.portfolio.snapshot(as_of=at)
            plan = next((p for p in snap.reservations if p.allocation_id == decision.allocation_id), None)
            if plan is None:
                reasons.append('M9_RESERVATION_NOT_ACTIVE')
            elif (plan.signal_id != decision.signal_id or plan.quantity != decision.quantity
                  or plan.entry != decision.entry_price or plan.stop_fill != decision.stop_fill
                  or plan.planned_risk != decision.planned_loss or plan.spec != decision.instrument
                  or plan.quantity <= 0 or plan.planned_risk > decision.risk_budget):
                raise ValueError('M8 decision/reservation identity mismatch')
            elif at >= plan.expires_at:
                reasons.append('M9_ORDER_VALIDITY_EXPIRED')
            elif at > plan.data_valid_until:
                reasons.append('M9_SOURCE_DATA_STALE')
            elif (identifier(next(q for q in snap.marks if q.key == plan.spec.symbol)) != plan.mark_id
                  or identifier(next(q for q in snap.fx_rates if q.key == plan.spec.quote_currency)) != plan.fx_id):
                reasons.append('M9_MARK_OR_FX_CHANGED')
            else:
                order_id = identifier('M9.1-order-v1', self.configuration_id, decision.decision_id, plan.allocation_id)
                order = OrderIntent('M9.1-v1', order_id, binding, at, plan.expires_at, plan.data_valid_until,
                                    plan.side, 'BUY' if plan.side == 'LONG' else 'SELL', plan.quantity,
                                    plan.quantity*plan.spec.multiplier, plan.entry*plan.quantity*plan.spec.multiplier,
                                    plan.margin/plan.fx, plan.entry, decision.proposal.stop_price, plan.stop_fill,
                                    decision.leverage, plan, decision)
        elif decision.status not in ('REJECTED', 'BLOCKED'):
            raise ValueError('Unsupported M8 decision status')
        receipt = AdmissionReceipt(identifier('M9.1-admission-v1', self.configuration_id, decision.decision_id, at),
                                   at, 'ACCEPTED' if order else 'DENIED', tuple(sorted(set(reasons))), decision, order)
        if order:
            event = self._event(order, 'ORDER_PROPOSED', 'PROPOSED', at, source_id=decision.decision_id,
                                source_fingerprint=identifier(decision), source_available_at=decision.available_at,
                                observed_at=decision.proposal.available_at, reasons=receipt.reason_codes)
            self._orders[order.order_id] = order
            self._record(event)
        self._receipts.append(receipt)
        self._admitted[decision.decision_id] = receipt
        self._last_at = at
        return receipt

    def submit_signal(self, record, spec, costs, *, leverage, as_of, events=(), requested_quantity=None):
        at = self._time(as_of)
        events = tuple(events)
        proposal = proposal_from_m7(record, as_of=at, events=events)
        self._binding(proposal.symbol)
        # Delivered terminal evidence must revoke an earlier grant, not merely reject a retry.
        for event in events:
            if event.signal_id == proposal.signal_id and event.available_at <= at:
                self.on_signal_terminal(event, as_of=at)
        decision = self.risk.evaluate(proposal, spec, costs, leverage=leverage,
                                      as_of=at, requested_quantity=requested_quantity)
        return self.propose(decision, as_of=at)

    def _command(self, key, payload, at):
        fingerprint = identifier(payload)
        prior = self._commands.get(key)
        if prior is not None:
            if prior[0] != fingerprint:
                raise ValueError('Cannot revise a simulation callback')
            self._last_at = at
            return prior[1]
        return None

    def process_fill(self, fill: SimulationFill, *, as_of) -> LifecycleEvent:
        at = self._time(as_of)
        if not isinstance(fill, SimulationFill) or fill.available_at > at:
            raise ValueError('Available typed simulation fill required')
        key = ('FILL', fill.fill_id)
        prior = self._command(key, fill, at)
        if prior is not None:
            return prior
        if fill.order_id not in self._orders:
            raise ValueError('No M8-approved order exists for this fill')
        order = self._orders[fill.order_id]
        self._registered(order.risk)
        if fill.quantity != order.quantity_contracts:
            raise ValueError('Only full approved quantities supported; partial/oversized fills rejected')
        if fill.phase == 'ENTRY':
            if self.state(order.order_id) != 'PROPOSED':
                raise ValueError('Entry requires a PROPOSED order')
            if fill.observed_at < order.created_at or at >= order.expires_at:
                raise ValueError('Entry observation predates intent or validity has expired')
            if fill.price != order.entry_price or fill.fee_rate != order.risk.costs.entry_fee_rate:
                raise ValueError('Entry fill differs from M8-approved price/cost model')
            self._reservation(order, at)
            booking = self.risk.simulate_open(order.allocation.allocation_id, as_of=at,
                                             event_id=identifier('M9.1-entry-booking', order.order_id, fill.fill_id))
            kind, state = 'ENTRY_FILLED', 'OPEN'
        else:
            if self.state(order.order_id) != 'OPEN':
                raise ValueError('Exit requires an OPEN approved position')
            position = next((p for p in self.risk.portfolio.snapshot(as_of=at).positions
                             if p.plan.allocation_id == order.allocation.allocation_id), None)
            if position is None or position.plan != order.allocation or fill.observed_at < position.opened_at:
                raise ValueError('Exit lacks its authoritative position or predates opening')
            fx = next(q for q in self.risk.portfolio.snapshot(as_of=at).fx_rates
                      if q.key == order.allocation.spec.quote_currency)
            if at-fx.observed_at > duration(self.risk.config.max_fx_age_seconds):
                raise ValueError('Fresh explicit FX required for exit accounting')
            booking = self.risk.portfolio.simulate_close(order.allocation.allocation_id, fill.price, fill.fee_rate,
                                                         as_of=at, event_id=identifier('M9.1-exit-booking', order.order_id, fill.fill_id))
            kind, state = 'EXIT_FILLED', 'CLOSED'
        event = self._event(order, kind, state, at, source_id=fill.fill_id, source_fingerprint=identifier(fill),
                            source_available_at=fill.available_at, observed_at=fill.observed_at,
                            portfolio_event_id=booking.event_id, fill=fill)
        self._record(event)
        self._commands[key] = (identifier(fill), event)
        self._last_at = at
        return event

    def cancel(self, request: SimulationCancellation, *, as_of) -> LifecycleEvent:
        at = self._time(as_of)
        if not isinstance(request, SimulationCancellation) or request.available_at > at:
            raise ValueError('Available typed simulation cancellation required')
        key = ('CANCEL', request.cancellation_id)
        prior = self._command(key, request, at)
        if prior is not None:
            return prior
        if request.order_id not in self._orders or self.state(request.order_id) != 'PROPOSED':
            raise ValueError('Cancellation requires a PROPOSED approved order')
        order = self._orders[request.order_id]
        if request.observed_at < order.created_at:
            raise ValueError('Cancellation predates the intent')
        self._reservation(order, at)
        booking = self.risk.portfolio.cancel(order.allocation.allocation_id, as_of=at,
                                             event_id=identifier('M9.1-cancel-booking', order.order_id, request.cancellation_id))
        state = 'EXPIRED' if at >= order.expires_at else 'CANCELLED'
        event = self._event(order, 'ORDER_'+state, state, at, source_id=request.cancellation_id,
                            source_fingerprint=identifier(request), source_available_at=request.available_at,
                            observed_at=request.observed_at, reasons=(request.reason,),
                            portfolio_event_id=booking.event_id, cancellation=request)
        self._record(event)
        self._commands[key] = (identifier(request), event)
        self._last_at = at
        return event

    def expire(self, order_id, *, as_of):
        at = self._time(as_of)
        if order_id not in self._orders or at < self._orders[order_id].expires_at:
            raise ValueError('Known order must reach its expiry before expiration')
        if self.state(order_id) == 'EXPIRED':
            self._last_at = at
            return next(e for e in self.history if e.order_id == order_id and e.state == 'EXPIRED'
                        and e.previous_state == 'PROPOSED')
        request = SimulationCancellation(identifier('M9.1-clock-expiry', order_id), order_id,
                                         'ORDER_VALIDITY_EXPIRED', at, at, 'M9.1 offline simulation clock')
        return self.cancel(request, as_of=at)

    def on_signal_terminal(self, event, *, as_of) -> tuple[LifecycleEvent, ...]:
        from trading.signals import SignalLifecycleEvent
        at = self._time(as_of)
        if (not isinstance(event, SignalLifecycleEvent) or event.available_at > at
                or event.kind not in ('INVALIDATED', 'EXPIRED')
                or (event.price_closed_at is not None and event.price_closed_at > event.available_at)):
            raise ValueError('Available typed M7 terminal event required')
        key = ('M7_TERMINAL', event.event_id)
        prior = self._command(key, event, at)
        if prior is not None:
            return prior
        positions = {p.plan.allocation_id: p.plan for p in self.risk.portfolio.snapshot(as_of=at).positions}
        for order in self.orders:
            if order.risk.signal_id != event.signal_id:
                continue
            position = positions.get(order.allocation.allocation_id)
            if (self.state(order.order_id) == 'OPEN' and position != order.allocation
                    or self.state(order.order_id) != 'OPEN' and position is not None):
                raise ValueError('Position lifecycle changed outside the bridge; reconcile by causal replay')
        self.risk.observe_m7_terminal(event, as_of=at)
        notices = []
        for order in self.orders:
            if order.risk.signal_id != event.signal_id:
                continue
            state = self.state(order.order_id)
            target = ('EXPIRED' if event.kind == 'EXPIRED' else 'CANCELLED') if state == 'PROPOSED' else state
            notice = self._event(order, 'SIGNAL_'+event.kind, target, at, source_id=event.event_id,
                                 source_fingerprint=identifier(event), source_available_at=event.available_at,
                                 observed_at=event.price_closed_at, reasons=(event.reason,))
            self._record(notice)
            notices.append(notice)
        result = tuple(notices)
        self._commands[key] = (identifier(event), result)
        self._last_at = at
        return result
