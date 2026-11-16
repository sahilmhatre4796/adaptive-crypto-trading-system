"""Network-free M12 Phase 2 stop, target, and funding regressions."""
import sys
sys.dont_write_bytecode = True

from dataclasses import replace
from decimal import Decimal as D
import unittest

import pandas as pd

from trading.candle_driver import CandleInput
from trading.exit_funding import ExitFundingController, FundingObservation
from trading.notifications import notification_events
from trading.patterns import OHLC
from trading.portfolio import Quote
from trading.signals import SignalLifecycleEvent
from test_freqtrade_bridge import fill, prepared
from test_portfolio import AT
from test_risk import costs


FIVE = pd.Timedelta(minutes=5)


def opened_controller(*, side='LONG', target=None, model=None, target_slippage='0'):
    risk, bridge, receipt = prepared(side=side, model=model)
    order = receipt.order
    controller = ExitFundingController(bridge)
    bridge.process_fill(fill(order), as_of=AT)
    plan = controller.register(order, take_profit_price=D(target or ('110' if side == 'LONG' else '90')),
                               take_profit_slippage_rate=D(target_slippage), as_of=AT)
    return risk, bridge, order, controller, plan


def bar(ohlc, *, opened=AT, symbol='BTC/USDT', received=None):
    return CandleInput(symbol, opened, ohlc, 10, received or opened + FIVE, 'synthetic-fixture')


def funding(event_id, *, symbol='BTC/USDT', at=None, rate='.001', mark='100', fx='1',
            received=None, historical=True):
    at = at or AT + FIVE
    received = received or at
    return FundingObservation(event_id, symbol, at, at, received, D(rate),
                              Quote(symbol, D(mark), at, received, 'synthetic-mark'),
                              Quote('USDT', D(fx), at, received, 'synthetic-fx'),
                              'synthetic-funding', historical)


def observe_funding_quotes(risk, observation):
    risk.portfolio.observe(observation.mark, kind='MARK', as_of=observation.received_at,
                           event_id=observation.event_id + ':mark')
    risk.portfolio.observe(observation.fx, kind='FX', as_of=observation.received_at,
                           event_id=observation.event_id + ':fx')


class ExitFundingControllerTests(unittest.TestCase):
    def test_open_position_without_explicit_exit_plan_fails_closed(self):
        risk, bridge, receipt = prepared()
        order = receipt.order
        bridge.process_fill(fill(order), as_of=AT)
        controller = ExitFundingController(bridge)
        before = (risk.portfolio.history, bridge.history)
        with self.assertRaisesRegex(ValueError, 'protective exit plan'):
            controller.observe_candle(bar(OHLC(100, 101, 89, 90)))
        self.assertEqual((risk.portfolio.history, bridge.history), before)

    def test_stop_first_on_both_thresholds_and_gap_through_stop(self):
        risk, bridge, order, controller, _ = opened_controller()
        outcome, = controller.observe_candle(bar(OHLC(85, 115, 84, 90)))
        self.assertEqual(outcome.reason, 'STOP')
        self.assertEqual(outcome.fill.price, D('85'))
        self.assertEqual(outcome.fill.quantity, order.quantity_contracts)
        self.assertEqual(outcome.fill.fee_rate, order.risk.costs.exit_fee_rate)
        self.assertEqual(outcome.lifecycle.state, 'CLOSED')
        self.assertEqual(bridge.state(order.order_id), 'CLOSED')
        self.assertFalse(risk.portfolio.snapshot(as_of=AT + FIVE).positions)
        mapped = notification_events(outcome.lifecycle, as_of=AT + FIVE, order=order)
        self.assertEqual(tuple(event.kind for event in mapped),
                         ('SIMULATED_EXIT_FILL', 'SIMULATED_POSITION_CLOSED'))

    def test_long_and_short_exact_stop_and_target_boundaries(self):
        cases = (
            ('LONG', OHLC(100, 101, 90, 95), 'STOP'),
            ('LONG', OHLC(100, 110, 99, 105), 'TAKE_PROFIT'),
            ('SHORT', OHLC(100, 110, 99, 105), 'STOP'),
            ('SHORT', OHLC(100, 101, 90, 95), 'TAKE_PROFIT'),
        )
        for side, ohlc, reason in cases:
            with self.subTest(side=side, reason=reason):
                _, bridge, order, controller, _ = opened_controller(side=side)
                outcome, = controller.observe_candle(bar(ohlc))
                self.assertEqual((outcome.reason, outcome.lifecycle.state), (reason, 'CLOSED'))
                self.assertEqual(outcome.lifecycle.order_id, order.order_id)
                self.assertEqual(bridge.state(order.order_id), 'CLOSED')

    def test_nonzero_stop_slippage_is_adverse_rounded_and_applied_once(self):
        cases = (
            ('LONG', OHLC(100, 101, 90, 95), D('88.8')),
            ('SHORT', OHLC(100, 110, 99, 105), D('111.4')),
        )
        for side, candle, expected in cases:
            with self.subTest(side=side):
                risk, bridge, order, controller, _ = opened_controller(
                    side=side, model=costs(stop_slippage_rate='.0123'))
                self.assertEqual(order.allocation.spec.price_tick, D('.1'))
                self.assertEqual(order.risk.stop_fill, expected)
                outcome, = controller.observe_candle(bar(candle))
                self.assertEqual((outcome.reason, outcome.fill.price), ('STOP', expected))
                self.assertEqual(bridge.state(order.order_id), 'CLOSED')
                history = (risk.portfolio.history, bridge.history)
                self.assertEqual(controller.observe_candle(bar(candle)), (outcome,))
                self.assertEqual((risk.portfolio.history, bridge.history), history)

    def test_nonzero_target_slippage_is_adverse_rounded_and_applied_once(self):
        cases = (
            ('LONG', OHLC(100, 110, 99, 105), D('108.6')),
            ('SHORT', OHLC(100, 101, 90, 95), D('91.2')),
        )
        for side, candle, expected in cases:
            with self.subTest(side=side):
                _, bridge, order, controller, _ = opened_controller(
                    side=side, target_slippage='.0123')
                outcome, = controller.observe_candle(bar(candle))
                self.assertEqual((outcome.reason, outcome.fill.price), ('TAKE_PROFIT', expected))
                self.assertEqual(bridge.state(order.order_id), 'CLOSED')

    def test_target_slippage_cannot_round_execution_price_to_zero(self):
        risk, bridge, order, controller, _ = opened_controller(target_slippage='.9999')
        before = (risk.portfolio.history, bridge.history)
        with self.assertRaisesRegex(ValueError, 'exit price must remain positive'):
            controller.observe_candle(bar(OHLC(100, 110, 99, 105)))
        self.assertEqual((risk.portfolio.history, bridge.history), before)
        self.assertEqual(bridge.state(order.order_id), 'OPEN')

    def test_take_profit_gap_through_fills_at_target_on_both_sides(self):
        cases = (
            ('LONG', OHLC(120, 121, 119, 120), D('110')),
            ('SHORT', OHLC(80, 81, 79, 80), D('90')),
        )
        for side, candle, expected in cases:
            with self.subTest(side=side):
                _, bridge, order, controller, _ = opened_controller(side=side)
                outcome, = controller.observe_candle(bar(candle))
                self.assertEqual((outcome.reason, outcome.fill.price), ('TAKE_PROFIT', expected))
                self.assertEqual(bridge.state(order.order_id), 'CLOSED')

    def test_duplicate_exit_is_one_booking_and_changed_candle_is_rejected(self):
        risk, bridge, order, controller, _ = opened_controller()
        source = bar(OHLC(100, 101, 89, 90))
        first = controller.observe_candle(source)
        history = (risk.portfolio.history, bridge.history)
        self.assertEqual(controller.observe_candle(source), first)
        self.assertEqual((risk.portfolio.history, bridge.history), history)
        with self.assertRaises(ValueError):
            controller.observe_candle(replace(source, ohlc=OHLC(100, 102, 89, 90)))
        self.assertEqual((risk.portfolio.history, bridge.history), history)

    def test_replay_yields_identical_exit_ids_and_accounting(self):
        def scenario():
            risk, bridge, _, controller, _ = opened_controller()
            event = controller.observe_candle(bar(OHLC(100, 110, 99, 105)))
            return (event, risk.portfolio.history, risk.portfolio.snapshot(as_of=AT + FIVE),
                    bridge.history)
        self.assertEqual(scenario(), scenario())

    def test_upstream_terminal_is_linked_without_fabricating_exit(self):
        risk, bridge, order, controller, _ = opened_controller()
        at = AT + pd.Timedelta(seconds=1)
        event = SignalLifecycleEvent('m7-terminal', order.risk.signal_id, 'INVALIDATED',
                                     at, at, 'source-bar', 'synthetic')
        controller.on_signal_terminal(event, as_of=at)
        self.assertEqual(bridge.state(order.order_id), 'OPEN')
        self.assertEqual(len(risk.portfolio.snapshot(as_of=at).positions), 1)

    def test_funding_signed_cash_flow_and_duplicate_replay(self):
        for side in ('LONG', 'SHORT'):
            for rate in ('.001', '-.001'):
                with self.subTest(side=side, rate=rate):
                    risk, _, order, controller, _ = opened_controller(side=side)
                    observation = funding('fund-1', rate=rate)
                    observe_funding_quotes(risk, observation)
                    before = risk.portfolio.snapshot(as_of=AT + FIVE).cash
                    result = controller.settle_funding(observation)
                    direction = D(1 if side == 'LONG' else -1)
                    expected = -(direction * order.quantity_contracts * order.allocation.spec.multiplier
                                 * observation.mark.value * observation.fx.value * observation.rate)
                    self.assertEqual(result.cash_flow, expected)
                    self.assertEqual(result.status, 'SETTLED')
                    self.assertEqual(risk.portfolio.snapshot(as_of=AT + FIVE).cash, before + expected)
                    history = risk.portfolio.history
                    self.assertEqual(controller.settle_funding(observation), result)
                    self.assertEqual(risk.portfolio.history, history)
                    with self.assertRaises(ValueError):
                        controller.settle_funding(replace(observation, rate=D('.002')))
                    with self.assertRaises(ValueError):
                        controller.settle_funding(replace(observation, event_id='same-settlement-new-id'))
                    with self.assertRaises(ValueError):
                        controller.settle_funding(replace(observation, event_id='other-provider-id',
                                                          source='other-provider'))
                    self.assertEqual(risk.portfolio.history, history)

    def test_missing_future_or_post_close_funding_never_books_cash(self):
        risk, bridge, _, controller, _ = opened_controller()
        before = risk.portfolio.history
        with self.assertRaises(ValueError):
            controller.settle_funding(replace(funding('future'), published_at=AT + 2 * FIVE))
        with self.assertRaises(ValueError):
            controller.settle_funding(replace(funding('missing-mark'), mark=None))
        self.assertEqual(risk.portfolio.history, before)
        controller.observe_candle(bar(OHLC(100, 101, 89, 90)))
        after_close = risk.portfolio.history
        with self.assertRaises(ValueError):
            controller.settle_funding(funding('post-close', at=AT + 2 * FIVE))
        self.assertEqual(risk.portfolio.history, after_close)

    def test_funding_requires_historical_eligibility_and_identical_fresh_quotes(self):
        risk, _, _, controller, _ = opened_controller()
        available = funding('eligible')
        before = risk.portfolio.history
        with self.assertRaisesRegex(ValueError, 'historical availability'):
            controller.settle_funding(replace(available, historical_eligible=False))
        with self.assertRaisesRegex(ValueError, 'identical mark and FX'):
            controller.settle_funding(available)
        self.assertEqual(risk.portfolio.history, before)
        observe_funding_quotes(risk, available)
        with self.assertRaisesRegex(ValueError, 'identical mark and FX'):
            controller.settle_funding(replace(available, event_id='wrong-mark',
                                              mark=Quote('BTC/USDT', D('101'), available.settled_at,
                                                         available.received_at, 'other-source')))
        self.assertEqual(controller.settle_funding(available).status, 'SETTLED')

    def test_funding_repeat_after_close_returns_original_receipt(self):
        risk, _, _, controller, _ = opened_controller()
        observation = funding('before-close')
        observe_funding_quotes(risk, observation)
        first = controller.settle_funding(observation)
        later = AT + FIVE
        controller.observe_candle(bar(OHLC(100, 101, 89, 90), received=later))
        history = risk.portfolio.history
        self.assertIs(controller.settle_funding(observation), first)
        self.assertEqual(risk.portfolio.history, history)

    def test_first_exit_observation_cannot_skip_eligible_post_entry_bar(self):
        risk, bridge, order, controller, _ = opened_controller()
        before = (risk.portfolio.history, bridge.history)
        with self.assertRaises(ValueError):
            controller.observe_candle(bar(OHLC(100, 101, 89, 90), opened=AT + FIVE))
        self.assertEqual((risk.portfolio.history, bridge.history), before)
        self.assertEqual(bridge.state(order.order_id), 'OPEN')

    def test_same_cutoff_frame_posts_funding_before_stop_exit(self):
        risk, bridge, order, controller, _ = opened_controller()
        observation = funding('same-cutoff')
        observe_funding_quotes(risk, observation)
        before = risk.portfolio.snapshot(as_of=AT + FIVE).cash
        controller.process_frame(bar(OHLC(100, 101, 89, 90)), funding=(observation,))
        kinds = [event.kind for event in risk.portfolio.history]
        self.assertLess(kinds.index('FUNDING'), kinds.index('SIMULATED_CLOSE'))
        self.assertEqual(bridge.state(order.order_id), 'CLOSED')
        self.assertEqual(risk.portfolio.snapshot(as_of=AT + FIVE).funding_cash,
                         -order.quantity_contracts * D('100') * D('.001'))
        self.assertNotEqual(risk.portfolio.snapshot(as_of=AT + FIVE).cash, before)

    def test_no_position_funding_is_explicit_receipt_without_cash_booking(self):
        risk, bridge, _ = prepared()
        controller = ExitFundingController(bridge)
        observation = funding('no-position')
        observe_funding_quotes(risk, observation)
        before = risk.portfolio.history
        receipt = controller.settle_funding(observation)
        self.assertEqual((receipt.status, receipt.cash_flow, receipt.portfolio_event),
                         ('NO_POSITION', None, None))
        self.assertEqual(risk.portfolio.history, before)
        self.assertIs(controller.settle_funding(observation), receipt)


if __name__ == '__main__':
    unittest.main()
