"""Versioned full-quantity paper exits; M8 and M9.1 remain authoritative."""

from dataclasses import dataclass
from decimal import Decimal, ROUND_CEILING, ROUND_FLOOR

import pandas as pd

from trading.candle_driver import CandleInput
from trading.exit_funding import ExitFundingController, ExitOutcome
from trading.freqtrade_bridge import OrderIntent, SimulationFill
from trading.pattern_context import utc_timestamp
from trading.pattern_engine import TIMEFRAMES
from trading.portfolio import identifier, number, step


@dataclass(frozen=True)
class V2ExitPlan:
    order: OrderIntent
    tp1: Decimal
    tp2: Decimal
    target_slippage_rate: Decimal
    plan_id: str


@dataclass(frozen=True)
class TP1Milestone:
    event_id: str
    order_id: str
    candle_opened_at: pd.Timestamp
    available_at: pd.Timestamp
    candle_close: Decimal
    source_id: str


@dataclass(frozen=True)
class StopAmendment:
    event_id: str
    order_id: str
    cause_id: str
    old_trigger: Decimal
    new_trigger: Decimal
    available_at: pd.Timestamp
    effective_from: pd.Timestamp
    fx_id: str
    mark_id: str
    funding_cash: Decimal


class M13ExitController(ExitFundingController):
    """Opt-in M13-v2 plan; no partial exits or independent accounting."""

    def __init__(self, bridge):
        super().__init__(bridge)
        self._tp1 = {}
        self._pending = {}
        self._stops = {}
        self._amendments = {}

    @property
    def pending_amendments(self):
        return tuple(sorted((order_id, cause) for order_id, cause in self._pending.items()))

    @property
    def amendments(self):
        return tuple(self._amendments[key] for key in sorted(self._amendments))

    def register_v2(self, order, *, tp1, tp2, target_slippage_rate, as_of):
        self._active()
        at = utc_timestamp(as_of)
        if (not isinstance(order, OrderIntent) or order not in self.bridge.orders
                or self.bridge.state(order.order_id) != 'OPEN' or at < order.created_at):
            raise ValueError('Open M8-approved full position required')
        first, final = number(tp1, positive=True), number(tp2, positive=True)
        slip = number(target_slippage_rate, nonnegative=True)
        tick = order.allocation.spec.price_tick
        direction = 1 if order.side == 'LONG' else -1
        if (slip >= 1 or any(value != step(value, tick) for value in (first, final))
                or direction * (first - order.entry_price) <= 0
                or direction * (final - first) <= 0):
            raise ValueError('Explicit favorable tick-aligned TP1/TP2 required')
        plan = V2ExitPlan(order, first, final, slip,
                          identifier('M13-exit-plan-v2', order.order_id, first, final, slip))
        previous = self._plans.get(order.order_id)
        if previous is not None:
            if previous != plan:
                raise ValueError('Cannot revise registered v2 exit plan')
            return previous
        self._plans[order.order_id] = plan
        return plan

    def _trigger(self, plan, opened_at):
        order = plan.order
        direction = 1 if order.side == 'LONG' else -1
        trigger = step(order.source_stop_price, order.allocation.spec.price_tick,
                       ROUND_FLOOR if direction == 1 else ROUND_CEILING)
        for amendment in self._stops.get(order.order_id, ()):
            if opened_at >= amendment.effective_from:
                trigger = amendment.new_trigger
        return trigger

    def observe_candle(self, item: CandleInput):
        self._active()
        if not isinstance(item, CandleInput):
            raise ValueError('Completed typed 5m candle required')
        prior = self._candles.get((item.symbol, item.opened_at))
        if prior is not None:
            if prior[0] != item:
                raise ValueError('Revised exit candle')
            return prior[1]
        previous = self._last_candle.get(item.symbol)
        if previous is not None and item.opened_at != previous + TIMEFRAMES['5m']:
            raise ValueError('Missing exit observation candle')
        if previous is None:
            for plan in self.plans:
                if plan.order.binding.symbol != item.symbol or self.bridge.state(plan.order.order_id) != 'OPEN':
                    continue
                position = next((p for p in self.bridge.risk.portfolio.snapshot(
                    as_of=item.received_at).positions
                    if p.plan.allocation_id == plan.order.allocation.allocation_id), None)
                if position is None:
                    raise ValueError('Open v2 order lacks M8 position')
                if item.opened_at > position.opened_at.ceil('5min'):
                    raise ValueError('Missing first eligible post-entry exit candle')
        if item.received_at - item.candle.closed_at >= TIMEFRAMES['5m']:
            raise ValueError('Late exit candle')
        if any(self.bridge.state(p.order.order_id) == 'OPEN' and p.order.binding.symbol == item.symbol
               and p.order.order_id in self._pending for p in self.plans):
            raise ValueError('TP1 amendment barrier is unresolved')
        unprotected = [o.order_id for o in self.bridge.orders
                       if o.binding.symbol == item.symbol and self.bridge.state(o.order_id) == 'OPEN'
                       and o.order_id not in self._plans]
        if unprotected:
            raise ValueError('Open position lacks v2 protective plan')
        outcomes = []
        try:
            for plan in self.plans:
                order = plan.order
                if order.binding.symbol != item.symbol or self.bridge.state(order.order_id) != 'OPEN':
                    continue
                position = next((p for p in self.bridge.risk.portfolio.snapshot(
                    as_of=item.received_at).positions
                    if p.plan.allocation_id == order.allocation.allocation_id), None)
                if position is None:
                    raise ValueError('Open v2 order lacks M8 position')
                if item.opened_at < position.opened_at:
                    continue
                direction = 1 if order.side == 'LONG' else -1
                tick = order.allocation.spec.price_tick
                trigger = self._trigger(plan, item.opened_at)
                hit_stop = item.ohlc.low <= trigger if direction == 1 else item.ohlc.high >= trigger
                hit_tp2 = item.ohlc.high >= plan.tp2 if direction == 1 else item.ohlc.low <= plan.tp2
                hit_tp1 = item.ohlc.high >= plan.tp1 if direction == 1 else item.ohlc.low <= plan.tp1
                if hit_stop or hit_tp2:
                    reason = 'STOP' if hit_stop else 'TP2'
                    base = ((min(item.ohlc.open, trigger) if direction == 1
                             else max(item.ohlc.open, trigger)) if hit_stop else plan.tp2)
                    slip = order.allocation.stop_slippage_rate if hit_stop else plan.target_slippage_rate
                    price = step(number(base, positive=True) * (1 - direction * slip), tick,
                                 ROUND_FLOOR if direction == 1 else ROUND_CEILING)
                    if price <= 0:
                        raise ValueError('Nonpositive adverse exit price')
                    fill = SimulationFill(identifier('M13-exit-fill-v2', plan.plan_id,
                                                      item.opened_at, reason), order.order_id,
                                          'EXIT', order.quantity_contracts, price,
                                          order.risk.costs.exit_fee_rate, item.candle.closed_at,
                                          item.received_at, item.source_id)
                    lifecycle = self.bridge.process_fill(fill, as_of=item.received_at)
                    outcomes.append(ExitOutcome(reason, fill, lifecycle))
                elif hit_tp1 and order.order_id not in self._tp1:
                    milestone = TP1Milestone(identifier('M13-tp1-v2', plan.plan_id, item.opened_at),
                                             order.order_id, item.opened_at, item.received_at,
                                             number(item.ohlc.close, positive=True), item.source_id)
                    self._tp1[order.order_id] = milestone
                    self._pending[order.order_id] = milestone.event_id
                    outcomes.append(milestone)
        except BaseException:
            self._failed = True
            raise
        result = tuple(outcomes)
        self._candles[(item.symbol, item.opened_at)] = (item, result)
        self._last_candle[item.symbol] = item.opened_at
        return result

    def settle_funding(self, observation):
        self._active()
        if self._pending:
            raise ValueError('Prior protective amendment remains unresolved')
        result = super().settle_funding(observation)
        if result.status == 'SETTLED' and result.cash_flow < 0:
            for position in self.bridge.risk.portfolio.snapshot(as_of=observation.received_at).positions:
                order = next((o for o in self.bridge.orders if o.allocation == position.plan), None)
                if (order and order.binding.symbol == observation.symbol
                        and order.order_id in self._tp1):
                    self._pending[order.order_id] = observation.event_id
        return result

    def amend_stop(self, order_id, cause_id, *, as_of, event_id):
        self._active()
        at = utc_timestamp(as_of)
        prior = self._amendments.get(event_id)
        if prior is not None:
            if prior.order_id != order_id or prior.cause_id != cause_id or prior.available_at != at:
                raise ValueError('Revised amendment event')
            return prior
        if self._pending.get(order_id) != cause_id:
            raise ValueError('No matching pending TP1/funding amendment')
        plan = self._plans.get(order_id)
        if plan is None or self.bridge.state(order_id) != 'OPEN':
            raise ValueError('Amendment requires an open protected position')
        order = plan.order
        snapshot = self.bridge.risk.portfolio.snapshot(as_of=at)
        position = next((p for p in snapshot.positions if p.plan == order.allocation), None)
        mark = next((q for q in snapshot.marks if q.key == order.binding.symbol), None)
        fx = next((q for q in snapshot.fx_rates if q.key == 'USDT'), None)
        cfg = self.bridge.risk.config
        if (position is None or mark is None or fx is None
                or mark.available_at > at or fx.available_at > at
                or at - mark.observed_at > pd.Timedelta(seconds=float(cfg.max_quote_age_seconds))
                or at - fx.observed_at > pd.Timedelta(seconds=float(cfg.max_fx_age_seconds))):
            raise ValueError('Fresh M8 position, mark and FX required for stop amendment')
        direction = 1 if order.side == 'LONG' else -1
        tick = order.allocation.spec.price_tick
        qmx = order.quantity_contracts * order.allocation.spec.multiplier * fx.value
        fee = order.risk.costs.exit_fee_rate
        slip = order.allocation.stop_slippage_rate
        offset = (order.allocation.entry_fee - position.funding_cash) / qmx
        threshold = ((order.entry_price + offset) / (1 - fee) if direction == 1
                     else (order.entry_price - offset) / (1 + fee))
        if threshold <= 0 or slip >= 1:
            raise ValueError('No positive cost-adjusted break-even price')
        trigger = step(threshold / (1 - direction * slip), tick,
                       ROUND_CEILING if direction == 1 else ROUND_FLOOR)
        for _ in range(4):
            modeled = step(trigger * (1 - direction * slip), tick,
                           ROUND_FLOOR if direction == 1 else ROUND_CEILING)
            net = (direction * (modeled - order.entry_price) * qmx
                   - modeled * qmx * fee - order.allocation.entry_fee + position.funding_cash)
            if net >= 0:
                break
            trigger += tick * direction
        else:
            raise ValueError('No tick satisfies the cost-adjusted stop')
        old = (self._stops[order_id][-1].new_trigger if self._stops.get(order_id)
               else step(order.source_stop_price, tick,
                         ROUND_FLOOR if direction == 1 else ROUND_CEILING))
        trigger = max(trigger, old) if direction == 1 else min(trigger, old)
        market = mark.value
        milestone = self._tp1.get(order_id)
        if milestone is not None and cause_id == milestone.event_id:
            market = min(market, milestone.candle_close) if direction == 1 else max(market, milestone.candle_close)
        if trigger <= 0 or direction * (market - trigger) <= 0:
            raise ValueError('Cost-adjusted stop is not safely protective')
        effective = at.ceil('5min')
        amendment = StopAmendment(event_id, order_id, cause_id, old, trigger, at,
                                  effective, identifier(fx), identifier(mark), position.funding_cash)
        self._stops.setdefault(order_id, []).append(amendment)
        self._amendments[event_id] = amendment
        del self._pending[order_id]
        return amendment
