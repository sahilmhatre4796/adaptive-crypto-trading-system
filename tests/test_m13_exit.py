"""M13-v2 full-quantity protective exits, separate from M12-v1 tests."""
import unittest
from decimal import Decimal as D

from trading.m13_exit import M13ExitController, TP1Milestone, StopAmendment
from trading.exit_funding import ExitOutcome
from trading.patterns import OHLC
from trading.portfolio import Quote
from test_exit_funding import AT, FIVE, bar, funding, observe_funding_quotes
from test_freqtrade_bridge import fill, prepared


def opened(side='LONG'):
    risk, bridge, receipt = prepared(side=side)
    order = receipt.order
    bridge.process_fill(fill(order), as_of=AT)
    controller = M13ExitController(bridge)
    controller.register_v2(order, tp1=D('105' if side == 'LONG' else '95'),
                           tp2=D('110' if side == 'LONG' else '90'),
                           target_slippage_rate=D(0), as_of=AT)
    return risk, bridge, order, controller


def fresh_quotes(risk, side='LONG', at=AT+FIVE, mark=None):
    symbol = 'BTC/USDT'
    price = mark or ('106' if side == 'LONG' else '94')
    risk.portfolio.observe(Quote(symbol, D(price), at, at, 'fixture'), kind='MARK',
                           as_of=at, event_id='m13-mark-'+str(at))
    risk.portfolio.observe(Quote('USDT', D(1), at, at, 'fixture'), kind='FX',
                           as_of=at, event_id='m13-fx-'+str(at))


class M13ExitTests(unittest.TestCase):
    def test_stop_first_and_tp2_full_exit_without_amendment(self):
        for side, candle, reason in (
                ('LONG', OHLC(100, 111, 89, 105), 'STOP'),
                ('SHORT', OHLC(100, 111, 89, 95), 'STOP'),
                ('LONG', OHLC(100, 111, 99, 106), 'TP2'),
                ('SHORT', OHLC(100, 101, 89, 94), 'TP2')):
            with self.subTest(side=side, reason=reason):
                risk, bridge, order, controller = opened(side)
                result, = controller.observe_candle(bar(candle))
                self.assertIsInstance(result, ExitOutcome)
                self.assertEqual((result.reason, result.fill.quantity),
                                 (reason, order.quantity_contracts))
                self.assertEqual(bridge.state(order.order_id), 'CLOSED')
                self.assertFalse(controller.pending_amendments)
                self.assertFalse(controller.amendments)
                self.assertFalse(risk.portfolio.snapshot(as_of=AT+FIVE).positions)

    def test_tp1_is_no_fill_and_amendment_tightens_next_candle(self):
        for side, candle in (
                ('LONG', OHLC(100, 106, 99, 105)),
                ('SHORT', OHLC(100, 101, 94, 95))):
            with self.subTest(side=side):
                risk, bridge, order, controller = opened(side)
                result, = controller.observe_candle(bar(candle))
                self.assertIsInstance(result, TP1Milestone)
                self.assertEqual(bridge.state(order.order_id), 'OPEN')
                self.assertEqual(len(risk.portfolio.snapshot(as_of=AT+FIVE).positions), 1)
                self.assertEqual(controller.observe_candle(bar(candle)), (result,))
                with self.assertRaisesRegex(ValueError, 'barrier'):
                    controller.observe_candle(bar(OHLC(105, 106, 102, 104), opened=AT+FIVE))
                fresh_quotes(risk, side)
                amend = controller.amend_stop(order.order_id, result.event_id,
                                               as_of=AT+FIVE, event_id='amend-'+side)
                self.assertIsInstance(amend, StopAmendment)
                self.assertGreaterEqual(amend.new_trigger, amend.old_trigger) if side == 'LONG' else self.assertLessEqual(amend.new_trigger, amend.old_trigger)
                self.assertEqual(amend.effective_from, AT+FIVE)
                self.assertEqual(controller.amend_stop(order.order_id, result.event_id,
                                  as_of=AT+FIVE, event_id='amend-'+side), amend)
                self.assertFalse(controller.pending_amendments)
                self.assertEqual(bridge.state(order.order_id), 'OPEN')

    def test_no_default_target_and_revised_plan_rejected(self):
        _, bridge, order, controller = opened()
        with self.assertRaises(ValueError):
            controller.register_v2(order, tp1=D('106'), tp2=D('110'),
                                   target_slippage_rate=D(0), as_of=AT)
        self.assertEqual(bridge.state(order.order_id), 'OPEN')

    def test_funding_debit_after_tp1_requires_another_tightening_amendment(self):
        risk, bridge, order, controller = opened()
        milestone, = controller.observe_candle(bar(OHLC(100, 106, 99, 105)))
        fresh_quotes(risk)
        first = controller.amend_stop(order.order_id, milestone.event_id,
                                      as_of=AT+FIVE, event_id='first-amend')
        observation = funding('funding-debit', at=AT+2*FIVE, mark='106')
        observe_funding_quotes(risk, observation)
        booked = controller.settle_funding(observation)
        self.assertLess(booked.cash_flow, 0)
        self.assertEqual(controller.pending_amendments,
                         ((order.order_id, observation.event_id),))
        second = controller.amend_stop(order.order_id, observation.event_id,
                                       as_of=AT+2*FIVE, event_id='second-amend')
        self.assertGreaterEqual(second.new_trigger, first.new_trigger)
        self.assertEqual(controller.settle_funding(observation), booked)
        self.assertEqual(len(controller.amendments), 2)


if __name__ == '__main__':
    unittest.main()
