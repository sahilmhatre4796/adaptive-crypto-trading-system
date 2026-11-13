"""M9.2 pure notification values and HTML previews. No delivery or engine mutation."""
from dataclasses import dataclass, field
from decimal import Decimal
from html import escape

import pandas as pd

from trading.freqtrade_bridge import (AdmissionReceipt, LifecycleEvent, OrderIntent, SimulationCancellation,
                                      SimulationFill)
from trading.pattern_context import utc_timestamp
from trading.portfolio import identifier, number, text, wire
from trading.risk import RiskDecision, proposal_from_m7
from trading.signals import SignalRecord


STATES = {
    'SIGNAL_QUALIFIED': ('M7_SIGNAL', ('QUALIFIED',)),
    **{'RISK_'+s: ('M8_RISK', (s,)) for s in ('APPROVED', 'REDUCED', 'REJECTED', 'BLOCKED')},
    'ORDER_INTENT_CREATED': ('M9_ORDER', ('PROPOSED',)),
    'INTEGRATION_DENIED': ('M9_ADMISSION', ('DENIED',)),
    'SIMULATED_ENTRY_FILL': ('M9_LIFECYCLE', ('OPEN',)),
    'SIMULATED_POSITION_OPENED': ('M9_LIFECYCLE', ('OPEN',)),
    'SIMULATED_EXIT_FILL': ('M9_LIFECYCLE', ('CLOSED',)),
    'SIMULATED_POSITION_CLOSED': ('M9_LIFECYCLE', ('CLOSED',)),
    'SIMULATED_ORDER_CANCELLED': ('M9_LIFECYCLE', ('CANCELLED',)),
    'SIMULATED_ORDER_EXPIRED': ('M9_LIFECYCLE', ('EXPIRED',)),
    'SIMULATED_SIGNAL_TERMINAL_NOTICE': ('M9_LIFECYCLE', ('OPEN', 'CLOSED', 'CANCELLED', 'EXPIRED')),
    'PROCESSING_FAILURE': ('PROCESSING_FAILURE', ('ERROR',)),
    'INVALID_LIFECYCLE': ('PROCESSING_FAILURE', ('ERROR',)),
}


def _reasons(values):
    if not isinstance(values, tuple) or any(not isinstance(v, str) or not v.strip() for v in values):
        raise ValueError('Immutable exact public reason-code tuple required')


@dataclass(frozen=True)
class ProcessingFailure:
    failure_id: str
    kind: str
    stage: str
    reason_codes: tuple[str, ...]
    observed_at: pd.Timestamp
    available_at: pd.Timestamp
    source_event_id: str | None = None
    symbol: str | None = None
    side: str | None = None
    timeframe: str | None = None

    def __post_init__(self):
        text(self.failure_id)
        if self.kind not in ('PROCESSING_FAILURE', 'INVALID_LIFECYCLE') or self.stage not in ('M7', 'M8', 'M9.1', 'M9.2'):
            raise ValueError('Known failure kind/stage required; never pass raw exceptions')
        _reasons(self.reason_codes)
        if not self.reason_codes:
            raise ValueError('Explicit public failure reason required')
        for name in ('observed_at', 'available_at'):
            object.__setattr__(self, name, utc_timestamp(getattr(self, name)))
        if self.observed_at > self.available_at:
            raise ValueError('Failure observation cannot follow receipt')
        if self.source_event_id is not None:
            text(self.source_event_id)


@dataclass(frozen=True)
class NotificationEvent:
    kind: str
    source_kind: str
    source_id: str
    source_fingerprint: str
    source_available_at: pd.Timestamp
    status: str
    reason_codes: tuple[str, ...]
    symbol: str | None = None
    side: str | None = None
    timeframe: str | None = None
    confirmation_timeframe: str | None = None
    observed_at: pd.Timestamp | None = None
    upstream_available_at: pd.Timestamp | None = None
    signal_available_at: pd.Timestamp | None = None
    expires_at: pd.Timestamp | None = None
    signal_id: str | None = None
    risk_decision_id: str | None = None
    order_id: str | None = None
    allocation_id: str | None = None
    source_configuration_id: str | None = None
    source_input_id: str | None = None
    risk_status: str | None = None
    quantity: Decimal | None = None
    price: Decimal | None = None
    price_kind: str | None = None
    price_currency: str | None = None
    risk_budget: Decimal | None = None
    planned_loss: Decimal | None = None
    currency: str | None = None
    failure_stage: str | None = None
    related_event_id: str | None = None
    schema_version: str = 'M9.2-v1'
    event_id: str = field(init=False)
    dedupe_key: str = field(init=False)

    def __post_init__(self):
        allowed = STATES.get(self.kind) if isinstance(self.kind, str) else None
        if self.schema_version != 'M9.2-v1' or allowed is None or (self.source_kind != allowed[0] or self.status not in allowed[1]):
            raise ValueError('Notification kind/source/status mismatch')
        for value in (self.source_id, self.source_fingerprint):
            text(value)
        for name in ('signal_id', 'risk_decision_id', 'order_id', 'allocation_id', 'source_configuration_id',
                     'source_input_id', 'currency', 'price_currency', 'related_event_id'):
            if getattr(self, name) is not None:
                text(getattr(self, name))
        _reasons(self.reason_codes)
        if self.symbol not in (None, 'BTC/USDT', 'ETH/USDT') or self.side not in (None, 'LONG', 'SHORT'):
            raise ValueError('Unsupported symbol/side')
        for name in ('timeframe', 'confirmation_timeframe'):
            if getattr(self, name) not in (None, '5m', '15m', '1h', '4h', '1d', 'snapshot'):
                raise ValueError('Unknown explicit timeframe')
        for name in ('source_available_at', 'observed_at', 'upstream_available_at', 'signal_available_at', 'expires_at'):
            if getattr(self, name) is not None:
                object.__setattr__(self, name, utc_timestamp(getattr(self, name)))
        if self.source_available_at is None or any(value is not None and value > self.source_available_at for value in
                (self.observed_at, self.upstream_available_at, self.signal_available_at)):
            raise ValueError('Invalid notification source chronology')
        for name in ('quantity', 'price', 'risk_budget', 'planned_loss'):
            value = getattr(self, name)
            if value is not None:
                object.__setattr__(self, name, number(value, nonnegative=True, positive=name == 'price'))
        if ((self.price is None) != (self.price_kind is None) or (self.price is None) != (self.price_currency is None)
                or self.price_kind not in (None, 'REFERENCE', 'MODELED', 'SIMULATED_FILL')):
            raise ValueError('Explicit price meaning required')
        if self.risk_status not in (None, 'APPROVED', 'REDUCED', 'REJECTED', 'BLOCKED'):
            raise ValueError('Unknown source risk status')
        if self.source_kind == 'M8_RISK' and self.risk_status != self.status:
            raise ValueError('Risk status cannot be relabeled')
        if self.source_kind in ('M9_ORDER', 'M9_LIFECYCLE') and self.risk_status not in ('APPROVED', 'REDUCED'):
            raise ValueError('Simulated order context must retain a positive M8 source decision')
        if (self.risk_budget is not None or self.planned_loss is not None) and not self.currency:
            raise ValueError('Money requires explicit account currency')
        if self.source_kind != 'PROCESSING_FAILURE' and (self.symbol is None or self.side is None or self.signal_id is None):
            raise ValueError('Missing source signal context')
        if self.source_kind == 'M8_RISK' and (self.risk_budget is None or self.quantity is None or self.risk_decision_id is None):
            raise ValueError('Missing risk decision context')
        if self.source_kind == 'M8_RISK':
            positive = self.status in ('APPROVED', 'REDUCED')
            if (positive and (self.quantity <= 0 or not self.allocation_id)
                    or not positive and (self.quantity != 0 or self.allocation_id is not None)):
                raise ValueError('Risk notification status/quantity/allocation contradiction')
        if self.source_kind == 'PROCESSING_FAILURE' and self.failure_stage not in ('M7', 'M8', 'M9.1', 'M9.2'):
            raise ValueError('Explicit failure stage required')
        payload = {k: v for k, v in vars(self).items() if k not in ('event_id', 'dedupe_key')}
        object.__setattr__(self, 'dedupe_key', identifier('M9.2-key', self.source_kind, self.source_id, self.kind))
        object.__setattr__(self, 'event_id', identifier('M9.2-event', payload))

    def to_dict(self):
        return wire(self)


def _risk_fields(decision):
    if not isinstance(decision, RiskDecision) or decision.schema_version != 'M8-v1':
        raise ValueError('Typed M8-v1 decision required')
    if decision.status not in ('APPROVED', 'REDUCED', 'REJECTED', 'BLOCKED'):
        raise ValueError('Unsupported risk decision status')
    if decision.signal_id != decision.proposal.signal_id or decision.instrument.symbol != decision.proposal.symbol:
        raise ValueError('Risk source identities do not match')
    positive = decision.status in ('APPROVED', 'REDUCED')
    qty = number(decision.quantity, nonnegative=True)
    if (positive and (qty <= 0 or not decision.allocation_id)) or (not positive and (qty != 0 or decision.allocation_id is not None)):
        raise ValueError('Risk status/quantity/allocation contradiction')
    if positive and (decision.entry_price is None or decision.stop_fill is None):
        raise ValueError('Positive risk decision lacks modeled prices')
    return dict(symbol=decision.proposal.symbol, side=decision.proposal.side,
                signal_id=decision.signal_id, signal_available_at=decision.proposal.available_at,
                expires_at=decision.proposal.expires_at, risk_decision_id=decision.decision_id,
                allocation_id=decision.allocation_id, risk_status=decision.status, quantity=qty,
                price=decision.entry_price, price_kind='MODELED' if decision.entry_price is not None else None,
                price_currency=decision.instrument.quote_currency if decision.entry_price is not None else None,
                risk_budget=decision.risk_budget, planned_loss=decision.planned_loss, currency=decision.portfolio.currency,
                source_configuration_id=decision.configuration_id, source_input_id=decision.input_id)


def _order_fields(order):
    if not isinstance(order, OrderIntent) or order.schema_version != 'M9.1-v1' or order.simulation_only is not True:
        raise ValueError('Typed simulation-only M9.1 intent required')
    fields = _risk_fields(order.risk)
    if (order.risk.status not in ('APPROVED', 'REDUCED') or order.quantity_contracts != order.risk.quantity
            or order.allocation.allocation_id != order.risk.allocation_id
            or order.allocation.quantity != order.risk.quantity or order.allocation.spec != order.risk.instrument
            or order.allocation.signal_id != order.risk.signal_id or order.side != fields['side']
            or order.binding.symbol != fields['symbol'] or order.entry_price != order.risk.entry_price
            or order.expires_at != order.risk.proposal.expires_at):
        raise ValueError('Contradictory order/risk context')
    fields.update(order_id=order.order_id, upstream_available_at=order.risk.available_at)
    return fields


def notification_events(source, *, as_of, order: OrderIntent | None = None) -> tuple[NotificationEvent, ...]:
    """Normalize immutable producer records only; no actor, callback or transport accepted."""
    at = utc_timestamp(as_of)
    if isinstance(source, SignalRecord):
        proposal_from_m7(source, as_of=at)  # Existing pure validation, not RiskEngine.evaluate.
        if not source.evaluation.qualification_passed or source.evaluation.m3_decision.status != 'CONFIRMED':
            raise ValueError('Inconsistent qualified signal')
        context = source.evaluation.context
        result = (NotificationEvent('SIGNAL_QUALIFIED', 'M7_SIGNAL', source.decision_id, identifier(source),
                                   source.available_at, 'QUALIFIED', source.evaluation.reason_codes,
                                   symbol=source.symbol, side=source.decision, timeframe=context.pattern.timeframe,
                                   confirmation_timeframe=context.confirmation_candle.timeframe,
                                   observed_at=context.confirmation_candle.closed_at,
                                   upstream_available_at=context.pattern.confirmed_at, signal_available_at=source.available_at,
                                   expires_at=source.expires_at, signal_id=source.signal_id,
                                   source_configuration_id=source.policy_id, source_input_id=source.input_id,
                                   price=source.reference_price, price_kind='REFERENCE', price_currency=source.symbol.split('/')[1]),)
    elif isinstance(source, RiskDecision):
        result = (NotificationEvent('RISK_'+source.status, 'M8_RISK', source.decision_id, identifier(source),
                                   source.available_at, source.status, source.reason_codes, **_risk_fields(source)),)
    elif isinstance(source, OrderIntent):
        result = (NotificationEvent('ORDER_INTENT_CREATED', 'M9_ORDER', source.order_id, identifier(source),
                                   source.created_at, 'PROPOSED', source.risk.reason_codes, **_order_fields(source)),)
    elif isinstance(source, AdmissionReceipt):
        if source.available_at > at:
            raise ValueError('Admission unavailable at cutoff')
        if source.disposition == 'ACCEPTED' and source.order is not None and source.order.risk == source.risk:
            if source.order.created_at != source.available_at:
                raise ValueError('Admission/intent chronology mismatch')
            return notification_events(source.risk, as_of=at)+notification_events(source.order, as_of=at)
        if source.disposition != 'DENIED' or source.order is not None:
            raise ValueError('Contradictory admission state')
        result = (NotificationEvent('INTEGRATION_DENIED', 'M9_ADMISSION', source.receipt_id, identifier(source),
                                   source.available_at, 'DENIED', source.reason_codes, **_risk_fields(source.risk)),)
    elif isinstance(source, LifecycleEvent):
        fields = _order_fields(order)
        if (source.order_id, source.signal_id, source.risk_decision_id, source.allocation_id) != (
                order.order_id, order.risk.signal_id, order.risk.decision_id, order.allocation.allocation_id):
            raise ValueError('Lifecycle lacks matching order context')
        if source.available_at < order.created_at or source.source_available_at > source.available_at:
            raise ValueError('Invalid lifecycle chronology')
        if source.kind == 'ORDER_PROPOSED':
            if (source.previous_state is not None or source.state != 'PROPOSED' or source.available_at != order.created_at
                    or source.source_available_at != order.risk.available_at
                    or source.observed_at != order.risk.proposal.available_at
                    or source.source_id != order.risk.decision_id or source.source_fingerprint != identifier(order.risk)):
                raise ValueError('Invalid proposed-order alias')
            return notification_events(order, as_of=at)  # One canonical intent, not two alerts.
        if source.kind in ('ENTRY_FILLED', 'EXIT_FILLED'):
            entry = source.kind == 'ENTRY_FILLED'
            expected = ('PROPOSED', 'OPEN', 'ENTRY') if entry else ('OPEN', 'CLOSED', 'EXIT')
            f = source.fill
            if (not isinstance(f, SimulationFill) or (source.previous_state, source.state, f.phase) != expected
                    or f.order_id != order.order_id or f.quantity != order.quantity_contracts
                    or source.source_available_at != f.available_at or source.observed_at != f.observed_at
                    or f.observed_at < order.created_at
                    or not source.portfolio_event_id or source.source_id != f.fill_id
                    or source.source_fingerprint != identifier(f)):
                raise ValueError('Invalid fill/position lifecycle')
            if entry and (f.price != order.entry_price or f.fee_rate != order.risk.costs.entry_fee_rate):
                raise ValueError('Entry fill differs from its modeled order context')
            fields.update(price=f.price, price_kind='SIMULATED_FILL')
            kinds = ('SIMULATED_ENTRY_FILL', 'SIMULATED_POSITION_OPENED') if entry else ('SIMULATED_EXIT_FILL', 'SIMULATED_POSITION_CLOSED')
        elif source.kind in ('ORDER_CANCELLED', 'ORDER_EXPIRED'):
            c = source.cancellation
            if (source.previous_state != 'PROPOSED' or source.state != source.kind.removeprefix('ORDER_')
                    or not source.portfolio_event_id or not isinstance(c, SimulationCancellation)
                    or c.order_id != order.order_id or c.observed_at < order.created_at
                    or source.source_available_at != c.available_at or source.observed_at != c.observed_at
                    or source.source_id != c.cancellation_id or source.source_fingerprint != identifier(c)
                    or source.reason_codes != (c.reason,)
                    or (source.state == 'EXPIRED') != (source.available_at >= order.expires_at)):
                raise ValueError('Invalid cancellation lifecycle')
            kinds = ('SIMULATED_'+source.kind,)
        elif source.kind in ('SIGNAL_INVALIDATED', 'SIGNAL_EXPIRED'):
            if source.previous_state == 'PROPOSED':
                expected = 'CANCELLED' if source.kind == 'SIGNAL_INVALIDATED' else 'EXPIRED'
                if source.state != expected:
                    raise ValueError('Invalid terminal cancellation')
                kinds = ('SIMULATED_ORDER_'+expected,)
            elif source.previous_state == source.state and source.state in ('OPEN', 'CLOSED', 'CANCELLED', 'EXPIRED'):
                kinds = ('SIMULATED_SIGNAL_TERMINAL_NOTICE',)
            else:
                raise ValueError('Invalid terminal notice')
        else:
            raise ValueError('Unsupported lifecycle event')
        fields.update(observed_at=source.observed_at, upstream_available_at=source.source_available_at,
                      related_event_id=source.source_id)
        result = tuple(NotificationEvent(kind, 'M9_LIFECYCLE', source.event_id, identifier(source),
                                         source.available_at, source.state, source.reason_codes, **fields) for kind in kinds)
    elif isinstance(source, ProcessingFailure):
        result = (NotificationEvent(source.kind, 'PROCESSING_FAILURE', source.failure_id, identifier(source),
                                   source.available_at, 'ERROR', source.reason_codes, observed_at=source.observed_at,
                                   symbol=source.symbol, side=source.side, timeframe=source.timeframe,
                                   failure_stage=source.stage, related_event_id=source.source_event_id),)
    else:
        raise ValueError('Unsupported notification source; use a structured ProcessingFailure for errors')
    if any(e.source_available_at > at for e in result):
        raise ValueError('Notification source unavailable at cutoff')
    return result


@dataclass(frozen=True)
class TemplateConfig:
    max_message_characters: int  # Explicit local preview bound, not a verified API limit.

    def __post_init__(self):
        if isinstance(self.max_message_characters, bool) or not isinstance(self.max_message_characters, int) or self.max_message_characters < 1:
            raise ValueError('Positive explicit message bound required')


@dataclass(frozen=True)
class RenderedNotification:
    message_id: str
    notification_id: str
    template_configuration_id: str
    body_html: str
    parse_mode: str = 'HTML'
    channel: str = 'TELEGRAM_OFFLINE_PREVIEW'


def _safe(value):
    raw = str(value)
    # Keep reason data exact in the event, but prevent control/newline/bidi label spoofing.
    visible = ''.join(f'\\u{ord(c):04x}' if ord(c) < 32 or 127 <= ord(c) <= 159
                      or ord(c) in (0x061c, 0x200e, 0x200f)
                      or 0x202a <= ord(c) <= 0x202e or 0x2066 <= ord(c) <= 0x2069
                      or 0xd800 <= ord(c) <= 0xdfff else c for c in raw)
    return escape(visible, quote=True)


def render_notification(event: NotificationEvent, config: TemplateConfig) -> RenderedNotification:
    if not isinstance(event, NotificationEvent) or not isinstance(config, TemplateConfig):
        raise ValueError('Typed notification and explicit template configuration required')
    title = event.kind.replace('_', ' ')
    if event.kind in ('ORDER_INTENT_CREATED', 'INTEGRATION_DENIED'):
        title = 'SIMULATED '+title
    lines = [f'<b>OFFLINE | {_safe(title)}</b>']
    def line(label, value):
        if value is not None:
            lines.append(f'{label}: <code>{_safe(value)}</code>')
    line('Symbol', event.symbol)
    line('Side', event.side)
    line('Timeframe', event.timeframe or 'not supplied by source')
    line('Independent confirmation', event.confirmation_timeframe)
    line('Status', event.status)
    if event.risk_status is not None and event.risk_status != event.status:
        line('Source M8 status', event.risk_status)
    if event.kind == 'SIGNAL_QUALIFIED':
        lines.append('Recorded analytical qualification; M8 risk approval is not asserted.')
    if event.quantity is not None:
        line('Source quantity', wire(event.quantity)+' contracts')
    if event.price is not None:
        line({'REFERENCE': 'Reference price', 'MODELED': 'Modeled entry', 'SIMULATED_FILL': 'Simulated fill price'}[event.price_kind],
             wire(event.price)+' '+event.price_currency)
    if event.risk_budget is not None:
        line('Planned-loss budget', wire(event.risk_budget)+' '+event.currency)
    if event.planned_loss is not None:
        line('Source planned loss', wire(event.planned_loss)+' '+event.currency)
    line('Observed (UTC)', event.observed_at.isoformat() if event.observed_at is not None else None)
    line('Source available (UTC)', event.source_available_at.isoformat())
    line('Upstream available (UTC)', event.upstream_available_at.isoformat() if event.upstream_available_at is not None else None)
    line('Original signal available (UTC)', event.signal_available_at.isoformat() if event.signal_available_at is not None else None)
    line('Signal expiry (UTC)', event.expires_at.isoformat() if event.expires_at is not None else None)
    line('Failure stage', event.failure_stage)
    line('Reasons', ', '.join(event.reason_codes) if event.reason_codes else 'none recorded')
    line('Source', event.source_kind+' / '+event.source_id)
    line('Signal ID', event.signal_id)
    line('Risk decision ID', event.risk_decision_id)
    line('Order ID', event.order_id)
    body = '\n'.join(lines)
    if len(body) > config.max_message_characters:
        raise ValueError('Notification exceeds explicit preview bound; no silent truncation')
    configuration_id = identifier('M9.2-template-v1', config)
    return RenderedNotification(identifier('M9.2-message', event.event_id, configuration_id, body),
                                event.event_id, configuration_id, body)
