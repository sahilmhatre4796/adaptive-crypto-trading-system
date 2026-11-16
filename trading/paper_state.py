"""Read-only reconciliation checkpoint for the offline M8/M9.1/M12 actor graph."""
from dataclasses import dataclass
from decimal import Decimal

import pandas as pd

from trading.exit_funding import ExitFundingController
from trading.freqtrade_bridge import FreqtradeSimulationBridge
from trading.pattern_context import utc_timestamp
from trading.portfolio import identifier


@dataclass(frozen=True)
class PaperOrderState:
    order_id: str
    allocation_id: str
    state: str
    protected: bool


@dataclass(frozen=True)
class PaperStateReport:
    as_of: pd.Timestamp
    orders: tuple[PaperOrderState, ...]
    pending_reservations: tuple[str, ...]
    unprotected_open_orders: tuple[str, ...]
    cash: Decimal
    equity: Decimal
    realized_pnl: Decimal
    fees_paid: Decimal
    funding_cash: Decimal
    state_id: str


def reconcile_paper_state(bridge: FreqtradeSimulationBridge,
                          controller: ExitFundingController, *, as_of) -> PaperStateReport:
    """Validate committed local facts without changing or claiming venue state."""
    if (not isinstance(bridge, FreqtradeSimulationBridge)
            or not isinstance(controller, ExitFundingController)
            or controller.bridge is not bridge):
        raise ValueError('Matching authoritative offline actors required')
    at = utc_timestamp(as_of)
    portfolio = bridge.risk.portfolio
    snap = portfolio.snapshot(as_of=at)
    orders = {order.order_id: order for order in bridge.orders}
    if len(orders) != len(bridge.orders):
        raise ValueError('Duplicate simulated order identity')
    allocations = {order.allocation.allocation_id for order in orders.values()}
    if len(allocations) != len(orders):
        raise ValueError('Multiple orders consume one allocation')
    bookings = {event.event_id: event for event in portfolio.history}
    if len(bookings) != len(portfolio.history):
        raise ValueError('Duplicate portfolio booking identity')
    states = {}
    linked = {}
    event_ids = set()
    last_event_at = None
    allowed = {
        'ORDER_PROPOSED': (None, 'PROPOSED', None),
        'ENTRY_FILLED': ('PROPOSED', 'OPEN', 'SIMULATED_OPEN'),
        'EXIT_FILLED': ('OPEN', 'CLOSED', 'SIMULATED_CLOSE'),
        'ORDER_CANCELLED': ('PROPOSED', 'CANCELLED', 'CANCEL'),
        'ORDER_EXPIRED': ('PROPOSED', 'EXPIRED', 'CANCEL'),
    }
    for event in bridge.history:
        order = orders.get(event.order_id)
        if (order is None or event.event_id in event_ids or event.available_at > at
                or last_event_at is not None and event.available_at < last_event_at
                or event.allocation_id != order.allocation.allocation_id
                or event.risk_decision_id != order.risk.decision_id
                or event.signal_id != order.risk.signal_id
                or event.previous_state != states.get(event.order_id)):
            raise ValueError('Simulated lifecycle identity, chronology or transition mismatch')
        event_ids.add(event.event_id)
        last_event_at = event.available_at
        if event.kind in allowed:
            previous, target, booking_kind = allowed[event.kind]
            if event.previous_state != previous or event.state != target:
                raise ValueError('Invalid simulated lifecycle transition')
        elif event.kind in ('SIGNAL_INVALIDATED', 'SIGNAL_EXPIRED'):
            booking_kind = None
            target = ('CANCELLED' if event.kind == 'SIGNAL_INVALIDATED' else 'EXPIRED') if event.previous_state == 'PROPOSED' else event.previous_state
            if event.previous_state is None or event.state != target:
                raise ValueError('Invalid signal terminal transition')
        else:
            raise ValueError('Unknown simulated lifecycle event')
        if booking_kind is None:
            if event.portfolio_event_id is not None:
                raise ValueError('Non-booking lifecycle event has a portfolio effect')
        else:
            booking = bookings.get(event.portfolio_event_id)
            if (booking is None or booking.kind != booking_kind
                    or booking.available_at != event.available_at
                    or booking.event_id in linked):
                raise ValueError('Lifecycle event lacks its unique portfolio booking')
            linked[booking.event_id] = (event, order)
            if event.kind in ('ENTRY_FILLED', 'EXIT_FILLED'):
                if (event.fill is None or event.fill.order_id != order.order_id
                        or event.fill.quantity != order.quantity_contracts):
                    raise ValueError('Fill and approved order disagree')
        states[event.order_id] = event.state
    if states != {order_id: bridge.state(order_id) for order_id in orders}:
        raise ValueError('Bridge state differs from its lifecycle history')
    terminal_cancellations = {
        (allocation_id, receipt.available_at)
        for receipt in bridge.risk.terminal_history
        for allocation_id in receipt.cancelled_allocations
    }
    previous_reserves = set()
    previous_positions = {}
    closed_position_funding = Decimal(0)
    previous_cash = portfolio.initial_cash
    previous_realized = previous_fees = previous_funding = Decimal(0)
    for booking in portfolio.history:
        snapshot = booking.snapshot
        current_positions = {p.plan.allocation_id: p for p in snapshot.positions}
        removed_positions = set(previous_positions) - set(current_positions)
        if booking.kind == 'SIMULATED_CLOSE' and len(removed_positions) == 1:
            closed_position_funding += previous_positions[next(iter(removed_positions))].funding_cash
        elif removed_positions:
            raise ValueError('Position funding history changed outside a simulated close')
        if (closed_position_funding
                + sum((p.funding_cash for p in current_positions.values()), Decimal(0))
                != snapshot.funding_cash):
            raise ValueError('Position-level funding cash disagrees with portfolio funding')
        current_reserves = {plan.allocation_id for plan in snapshot.reservations}
        removed = previous_reserves - current_reserves
        terminal_cancel = (booking.kind == 'CANCEL' and
                           len(removed) == 1 and
                           (next(iter(removed)), booking.available_at) in terminal_cancellations)
        if (booking.kind in ('SIMULATED_OPEN', 'SIMULATED_CLOSE', 'CANCEL')
                and booking.event_id not in linked and not terminal_cancel):
            raise ValueError('Portfolio position changed outside the simulation bridge')
        if (snapshot.cash != portfolio.initial_cash + snapshot.realized_pnl
                or snapshot.equity != snapshot.cash + snapshot.unrealized_pnl
                or snapshot.available_capital != snapshot.equity - snapshot.used_margin - snapshot.reserved_capital):
            raise ValueError('Portfolio accounting identity mismatch')
        cash_change = snapshot.cash - previous_cash
        realized_change = snapshot.realized_pnl - previous_realized
        fee_change = snapshot.fees_paid - previous_fees
        funding_change = snapshot.funding_cash - previous_funding
        if booking.kind in ('SIMULATED_OPEN', 'SIMULATED_CLOSE'):
            lifecycle, order = linked[booking.event_id]
            if booking.kind == 'SIMULATED_OPEN':
                expected_cash = -order.allocation.entry_fee
                expected_fee = order.allocation.entry_fee
            else:
                fill = lifecycle.fill
                fx = next((q.value for q in snapshot.fx_rates
                           if q.key == order.allocation.spec.quote_currency), None)
                if fx is None:
                    raise ValueError('Exit booking lacks its FX quote')
                direction = 1 if order.side == 'LONG' else -1
                gross = (direction * (fill.price - order.allocation.entry)
                         * fill.quantity * order.allocation.spec.multiplier * fx)
                expected_fee = fill.price * fill.quantity * order.allocation.spec.multiplier * fx * fill.fee_rate
                expected_cash = gross - expected_fee
            if (cash_change != expected_cash or realized_change != expected_cash
                    or fee_change != expected_fee or funding_change != 0):
                raise ValueError('Simulated fill and portfolio accounting disagree')
        elif booking.kind == 'FUNDING':
            if (cash_change != realized_change or cash_change != funding_change
                    or fee_change != 0):
                raise ValueError('Funding and portfolio accounting disagree')
        elif (cash_change, realized_change, fee_change, funding_change) != (0, 0, 0, 0):
            raise ValueError('Non-trading portfolio event changed cash accounting')
        previous_reserves = current_reserves
        previous_positions = current_positions
        previous_cash = snapshot.cash
        previous_realized = snapshot.realized_pnl
        previous_fees = snapshot.fees_paid
        previous_funding = snapshot.funding_cash
    if (snap.cash != portfolio.initial_cash + snap.realized_pnl
            or snap.equity != snap.cash + snap.unrealized_pnl
            or snap.available_capital != snap.equity - snap.used_margin - snap.reserved_capital):
        raise ValueError('Current portfolio accounting identity mismatch')
    if (closed_position_funding
            + sum((p.funding_cash for p in snap.positions), Decimal(0))
            != snap.funding_cash):
        raise ValueError('Current position-level funding cash disagrees with portfolio funding')
    positions = {p.plan.allocation_id: p.plan for p in snap.positions}
    reserves = {p.allocation_id: p for p in snap.reservations}
    if len(positions) != len(snap.positions) or len(reserves) != len(snap.reservations):
        raise ValueError('Duplicate portfolio allocation')
    decisions = {d.allocation_id: d for d in bridge.risk.history
                 if d.status in ('APPROVED', 'REDUCED')}
    for allocation_id, plan in {**positions, **reserves}.items():
        decision = decisions.get(allocation_id)
        if decision is None or decision.quantity != plan.quantity or decision.signal_id != plan.signal_id:
            raise ValueError('Active allocation lacks its M8 approval')
    if set(positions) & set(reserves):
        raise ValueError('Allocation is simultaneously reserved and open')
    plans = {plan.order.order_id: plan for plan in controller.plans}
    if any(order_id not in orders or plans[order_id].order != orders[order_id] for order_id in plans):
        raise ValueError('Exit plan does not match an approved order')
    rows = []
    for order_id in sorted(orders):
        order, state = orders[order_id], states.get(order_id)
        if (order.risk not in bridge.risk.history
                or order.risk.status not in ('APPROVED', 'REDUCED')):
            raise ValueError('Order lacks an authoritative M8 approval')
        allocation_id = order.allocation.allocation_id
        if (state == 'PROPOSED' and reserves.get(allocation_id) != order.allocation
                or state == 'OPEN' and positions.get(allocation_id) != order.allocation
                or state in ('CANCELLED', 'EXPIRED', 'CLOSED') and
                (allocation_id in positions or allocation_id in reserves)
                or state is None):
            raise ValueError('Order state disagrees with its M8 allocation')
        rows.append(PaperOrderState(order_id, allocation_id, state, order_id in plans))
    pending = tuple(sorted(set(reserves) - allocations))
    unprotected = tuple(row.order_id for row in rows if row.state == 'OPEN' and not row.protected)
    state_id = identifier('M12-paper-state-v1', at, rows, pending, unprotected,
                          snap.cash, snap.equity, snap.realized_pnl, snap.fees_paid,
                          snap.funding_cash, bridge.history, portfolio.history)
    return PaperStateReport(at, tuple(rows), pending, unprotected, snap.cash, snap.equity,
                            snap.realized_pnl, snap.fees_paid, snap.funding_cash, state_id)
