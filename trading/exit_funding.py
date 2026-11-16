"""M12 Phase 2 offline exit and funding controller; no venue or order transport."""
from dataclasses import dataclass
from decimal import Decimal, ROUND_CEILING, ROUND_FLOOR

import pandas as pd

from trading.candle_driver import CandleInput
from trading.freqtrade_bridge import FreqtradeSimulationBridge, LifecycleEvent, OrderIntent, SimulationFill
from trading.pattern_context import utc_timestamp
from trading.pattern_engine import TIMEFRAMES
from trading.portfolio import PortfolioEvent, Quote, identifier, number, step, text


@dataclass(frozen=True)
class ExitPlan:
    order: OrderIntent
    take_profit_price: Decimal
    take_profit_slippage_rate: Decimal
    plan_id: str


@dataclass(frozen=True)
class ExitOutcome:
    reason: str
    fill: SimulationFill
    lifecycle: LifecycleEvent


@dataclass(frozen=True)
class FundingObservation:
    event_id: str
    symbol: str
    settled_at: pd.Timestamp
    published_at: pd.Timestamp
    received_at: pd.Timestamp
    rate: Decimal
    mark: Quote
    fx: Quote
    source: str
    historical_eligible: bool = False

    def __post_init__(self):
        text(self.event_id)
        text(self.source)
        for name in ('settled_at', 'published_at', 'received_at'):
            object.__setattr__(self, name, utc_timestamp(getattr(self, name)))
        if (self.symbol not in ('BTC/USDT', 'ETH/USDT')
                or not isinstance(self.mark, Quote) or not isinstance(self.fx, Quote)
                or self.mark.key != self.symbol or self.fx.key != 'USDT'
                or not isinstance(self.historical_eligible, bool)
                or not self.settled_at <= self.published_at <= self.received_at
                or self.mark.observed_at > self.settled_at
                or self.fx.observed_at > self.settled_at
                or max(self.mark.available_at, self.fx.available_at) > self.received_at):
            raise ValueError('Funding requires causal settlement, publication, mark and FX')
        object.__setattr__(self, 'rate', number(self.rate))


@dataclass(frozen=True)
class FundingOutcome:
    observation: FundingObservation
    status: str  # SETTLED / NO_POSITION
    cash_flow: Decimal | None
    portfolio_event: PortfolioEvent | None


class ExitFundingController:
    """Replayable local actions over one authoritative M9.1 bridge actor."""

    def __init__(self, bridge: FreqtradeSimulationBridge):
        if not isinstance(bridge, FreqtradeSimulationBridge):
            raise ValueError('Authoritative offline M9.1 bridge required')
        self.bridge = bridge
        self._plans = {}
        self._candles = {}
        self._last_candle = {}
        self._funding = {}
        self._funding_slots = {}
        self._last_funding_at = None
        self._failed = False

    @property
    def plans(self):
        return tuple(self._plans[key] for key in sorted(self._plans))

    def _active(self):
        if self._failed:
            raise RuntimeError('Controller quarantined; replay original events into fresh actors')

    def register(self, order: OrderIntent, *, take_profit_price, take_profit_slippage_rate,
                 as_of) -> ExitPlan:
        self._active()
        at = utc_timestamp(as_of)
        if (not isinstance(order, OrderIntent) or order not in self.bridge.orders
                or self.bridge.state(order.order_id) != 'OPEN'
                or at < order.created_at):
            raise ValueError('Only an open M8-approved simulated order may have an exit plan')
        target = number(take_profit_price, positive=True)
        slip = number(take_profit_slippage_rate, nonnegative=True)
        if slip >= 1 or target != step(target, order.allocation.spec.price_tick):
            raise ValueError('Explicit tick-aligned target and bounded slippage required')
        direction = 1 if order.side == 'LONG' else -1
        if direction * (target - order.entry_price) <= 0:
            raise ValueError('Take-profit target must be favorable to entry')
        plan = ExitPlan(order, target, slip,
                        identifier('M12-exit-plan-v1', order.order_id, target, slip))
        previous = self._plans.get(order.order_id)
        if previous is not None:
            if previous != plan:
                raise ValueError('Cannot revise registered exit plan')
            return previous
        self._plans[order.order_id] = plan
        return plan

    def observe_candle(self, item: CandleInput) -> tuple[ExitOutcome, ...]:
        self._active()
        if not isinstance(item, CandleInput):
            raise ValueError('Typed completed 5m candle required')
        prior = self._candles.get((item.symbol, item.opened_at))
        if prior is not None:
            old, result = prior
            if old != item:
                raise ValueError('Cannot revise an observed exit candle')
            return result
        last = self._last_candle.get(item.symbol)
        if last is not None and item.opened_at != last + TIMEFRAMES['5m']:
            raise ValueError('Replay every 5m exit-observation candle in order')
        if last is None:
            for plan in self.plans:
                if (plan.order.binding.symbol == item.symbol
                        and self.bridge.state(plan.order.order_id) == 'OPEN'):
                    position = next((p for p in self.bridge.risk.portfolio.snapshot(
                        as_of=item.received_at).positions
                        if p.plan.allocation_id == plan.order.allocation.allocation_id), None)
                    if position is None:
                        raise ValueError('Open order lacks an authoritative M8 position')
                    if item.opened_at > position.opened_at.ceil('5min'):
                        raise ValueError('Missing first eligible post-entry exit candle')
        if item.received_at - item.candle.closed_at >= TIMEFRAMES['5m']:
            raise ValueError('Stale exit candle cannot establish a causal trigger')
        unprotected = [order.order_id for order in self.bridge.orders
                       if order.binding.symbol == item.symbol
                       and self.bridge.state(order.order_id) == 'OPEN'
                       and order.order_id not in self._plans]
        if unprotected:
            raise ValueError('Open simulated position lacks an explicit protective exit plan')
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
                    raise ValueError('Open order lacks an authoritative M8 position')
                # An already-open bar may contain price action from before entry.
                if item.opened_at < position.opened_at:
                    continue
                direction = 1 if order.side == 'LONG' else -1
                tick = order.allocation.spec.price_tick
                trigger = step(order.source_stop_price, tick,
                               ROUND_FLOOR if direction == 1 else ROUND_CEILING)
                hit_stop = item.ohlc.low <= trigger if direction == 1 else item.ohlc.high >= trigger
                hit_target = (item.ohlc.high >= plan.take_profit_price if direction == 1
                              else item.ohlc.low <= plan.take_profit_price)
                if not (hit_stop or hit_target):
                    continue
                reason = 'STOP' if hit_stop else 'TAKE_PROFIT'  # Conservative OHLC tie-break.
                base = (min(item.ohlc.open, trigger) if direction == 1 else max(item.ohlc.open, trigger)) if hit_stop else plan.take_profit_price
                slip = order.allocation.stop_slippage_rate if hit_stop else plan.take_profit_slippage_rate
                price = step(number(base, positive=True) * (1 - direction * slip), tick,
                             ROUND_FLOOR if direction == 1 else ROUND_CEILING)
                if price <= 0:
                    raise ValueError('Adverse simulated exit price must remain positive')
                fill = SimulationFill(identifier('M12-exit-fill-v1', plan.plan_id, item.opened_at, reason),
                                      order.order_id, 'EXIT', order.quantity_contracts, price,
                                      order.risk.costs.exit_fee_rate, item.candle.closed_at,
                                      item.received_at, item.source_id)
                lifecycle = self.bridge.process_fill(fill, as_of=item.received_at)
                outcomes.append(ExitOutcome(reason, fill, lifecycle))
        except BaseException:
            self._failed = True
            raise
        result = tuple(outcomes)
        self._candles[(item.symbol, item.opened_at)] = (item, result)
        self._last_candle[item.symbol] = item.opened_at
        return result

    def settle_funding(self, observation: FundingObservation) -> FundingOutcome:
        self._active()
        if not isinstance(observation, FundingObservation):
            raise ValueError('Typed funding observation required')
        prior = self._funding.get(observation.event_id)
        if prior is not None:
            if prior.observation != observation:
                raise ValueError('Cannot revise a funding observation')
            return prior
        slot = (observation.symbol, observation.settled_at)
        if slot in self._funding_slots:
            raise ValueError('Funding settlement slot already processed under another event ID')
        if self.bridge.risk.mode == 'BACKTEST' and not observation.historical_eligible:
            raise ValueError('Funding lacks verified historical availability')
        at = observation.received_at
        if self._last_funding_at is not None and at < self._last_funding_at:
            raise ValueError('Funding receipts must be chronological')
        portfolio = self.bridge.risk.portfolio
        snapshot = portfolio.snapshot(as_of=at)
        marks = {quote.key: quote for quote in snapshot.marks}
        rates = {quote.key: quote for quote in snapshot.fx_rates}
        if marks.get(observation.symbol) != observation.mark or rates.get('USDT') != observation.fx:
            raise ValueError('Funding must use already-booked identical mark and FX')
        config = self.bridge.risk.config
        if (at - observation.mark.observed_at > pd.Timedelta(seconds=float(config.max_quote_age_seconds))
                or at - observation.fx.observed_at > pd.Timedelta(seconds=float(config.max_fx_age_seconds))):
            raise ValueError('Funding mark or FX is stale at receipt')
        positions = sorted((p for p in snapshot.positions if p.plan.spec.symbol == observation.symbol),
                           key=lambda p: p.plan.allocation_id)
        if not positions:
            if any(e.kind == 'EXIT_FILLED' and e.available_at >= observation.settled_at
                   and e.order_id in {order.order_id for order in self.bridge.orders
                                      if order.binding.symbol == observation.symbol}
                   for e in self.bridge.history):
                raise ValueError('Late funding for a closed position requires reconciliation')
            outcome = FundingOutcome(observation, 'NO_POSITION', None, None)
        else:
            if len(positions) != 1 or positions[0].opened_at >= observation.settled_at:
                raise ValueError('Funding requires a position held before settlement')
            plan = positions[0].plan
            sign = 1 if plan.side == 'LONG' else -1
            cash = -sign * plan.quantity * plan.spec.multiplier * observation.mark.value * observation.rate * observation.fx.value
            try:
                event = portfolio.settle_funding(plan.allocation_id, cash, as_of=at,
                    event_id=identifier('M12-funding-v1', observation.event_id, plan.allocation_id))
            except BaseException:
                self._failed = True
                raise
            outcome = FundingOutcome(observation, 'SETTLED', cash, event)
        self._funding[observation.event_id] = outcome
        self._funding_slots[slot] = observation.event_id
        self._last_funding_at = at
        return outcome

    def process_frame(self, item: CandleInput, *, funding: tuple[FundingObservation, ...] = ()):
        """Apply available settlement facts before this candle's exit evaluation.

        Each booking is a separate offline fact. A caller that needs durable
        atomic replay must journal the original ordered observations externally.
        """
        self._active()
        if not isinstance(item, CandleInput) or type(funding) is not tuple:
            raise ValueError('Typed candle and explicit funding tuple required')
        if any(not isinstance(o, FundingObservation) or o.symbol != item.symbol
               or o.settled_at > item.candle.closed_at or o.received_at > item.received_at
               for o in funding):
            raise ValueError('Funding must be available by the candle decision cutoff')
        ordered = sorted(funding, key=lambda o: (o.received_at, o.settled_at, o.event_id))
        results = []
        try:
            for observation in ordered:
                results.append(self.settle_funding(observation))
            return tuple(results), self.observe_candle(item)
        except BaseException:
            if results:
                self._failed = True
            raise

    def on_signal_terminal(self, event, *, as_of):
        """M9.1 revokes pending intents; open positions keep their exit plan."""
        self._active()
        try:
            return self.bridge.on_signal_terminal(event, as_of=as_of)
        except BaseException:
            self._failed = True
            raise
