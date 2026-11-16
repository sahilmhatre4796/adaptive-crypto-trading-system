"""Offline M12 Phase 3 lifecycle and accounting reconciliation tests."""
import sys
sys.dont_write_bytecode = True

from dataclasses import replace
from decimal import Decimal as D
import unittest

import pandas as pd

from trading.exit_funding import ExitFundingController
from trading.freqtrade_bridge import FreqtradeSimulationBridge
from trading.paper_state import reconcile_paper_state
from trading.patterns import OHLC
from trading.signals import SignalLifecycleEvent
from test_exit_funding import bar, funding, observe_funding_quotes, opened_controller
from test_freqtrade_bridge import BINDINGS, cancellation, fill, prepared
from test_portfolio import AT
from test_risk import costs, decide, engine, proposal


class PaperStateTests(unittest.TestCase):
    def test_proposed_open_protected_and_closed_states_reconcile_without_mutation(self):
        risk, bridge, receipt = prepared(model=costs(entry_fee_rate='.001', exit_fee_rate='.002'))
        order = receipt.order
        controller = ExitFundingController(bridge)
        before = (risk.portfolio.history, bridge.history)
        proposed = reconcile_paper_state(bridge, controller, as_of=AT)
        self.assertEqual((proposed.orders[0].state, proposed.pending_reservations), ('PROPOSED', ()))
        self.assertEqual((risk.portfolio.history, bridge.history), before)

        bridge.process_fill(fill(order), as_of=AT)
        unprotected = reconcile_paper_state(bridge, controller, as_of=AT)
        self.assertEqual(unprotected.unprotected_open_orders, (order.order_id,))
        controller.register(order, take_profit_price=D('110'),
                            take_profit_slippage_rate=D('0'), as_of=AT)
        protected = reconcile_paper_state(bridge, controller, as_of=AT)
        self.assertEqual((protected.orders[0].state, protected.orders[0].protected), ('OPEN', True))
        outcome, = controller.observe_candle(bar(OHLC(100, 101, 89, 90)))
        closed = reconcile_paper_state(bridge, controller, as_of=AT + pd.Timedelta(minutes=5))
        self.assertEqual((outcome.reason, closed.orders[0].state), ('STOP', 'CLOSED'))
        self.assertFalse(closed.unprotected_open_orders)
        snap = risk.portfolio.snapshot(as_of=AT + pd.Timedelta(minutes=5))
        self.assertEqual((closed.cash, closed.equity, closed.realized_pnl,
                          closed.fees_paid, closed.funding_cash),
                         (snap.cash, snap.equity, snap.realized_pnl,
                          snap.fees_paid, snap.funding_cash))
        self.assertEqual(closed.cash, D('2000') + closed.realized_pnl)
        self.assertEqual(reconcile_paper_state(bridge, controller,
                         as_of=AT + pd.Timedelta(minutes=5)), closed)

    def test_pending_reservation_is_visible_and_cancelled_order_reconciles(self):
        risk = engine()
        decision = decide(risk)
        bridge = FreqtradeSimulationBridge(risk, BINDINGS)
        controller = ExitFundingController(bridge)
        pending = reconcile_paper_state(bridge, controller, as_of=AT)
        self.assertEqual((pending.orders, pending.pending_reservations),
                         ((), (decision.allocation_id,)))
        order = bridge.propose(decision, as_of=AT).order
        self.assertEqual(reconcile_paper_state(bridge, controller,
                         as_of=AT).pending_reservations, ())
        bridge.cancel(cancellation(order), as_of=AT)
        cancelled = reconcile_paper_state(bridge, controller, as_of=AT)
        self.assertEqual(cancelled.orders[0].state, 'CANCELLED')
        self.assertFalse(cancelled.pending_reservations)

    def test_terminal_revocation_reconciles_m8_cancellation(self):
        risk, bridge, receipt = prepared()
        controller = ExitFundingController(bridge)
        event = SignalLifecycleEvent('m12-terminal', receipt.order.risk.signal_id,
                                     'INVALIDATED', AT, AT, 'source-bar', 'fixture')
        bridge.on_signal_terminal(event, as_of=AT)
        report = reconcile_paper_state(bridge, controller, as_of=AT)
        self.assertEqual(report.orders[0].state, 'CANCELLED')
        self.assertFalse(risk.portfolio.snapshot(as_of=AT).reservations)

    def test_duplicate_fill_preserves_report_and_revised_state_is_detected(self):
        risk, bridge, receipt = prepared()
        controller = ExitFundingController(bridge)
        entry = fill(receipt.order)
        bridge.process_fill(entry, as_of=AT)
        first = reconcile_paper_state(bridge, controller, as_of=AT)
        bridge.process_fill(entry, as_of=AT)
        self.assertEqual(reconcile_paper_state(bridge, controller, as_of=AT), first)
        bridge._states[receipt.order.order_id] = 'CLOSED'  # Simulate corrupt actor state.
        with self.assertRaisesRegex(ValueError, 'Bridge state differs'):
            reconcile_paper_state(bridge, controller, as_of=AT)

    def test_short_exit_reconciles_exact_fill_cost_and_deterministic_replay(self):
        def scenario():
            risk, bridge, order, controller, _ = opened_controller(
                side='SHORT', model=costs(entry_fee_rate='.001', exit_fee_rate='.002',
                                          stop_slippage_rate='.0123'))
            controller.observe_candle(bar(OHLC(100, 110, 99, 105)))
            report = reconcile_paper_state(bridge, controller,
                                           as_of=AT + pd.Timedelta(minutes=5))
            return report, risk.portfolio.history, bridge.history
        first = scenario()
        self.assertEqual(first, scenario())
        self.assertEqual(first[0].orders[0].state, 'CLOSED')

    def test_funding_cash_reconciles_without_creating_a_fill(self):
        risk, bridge, order, controller, _ = opened_controller()
        observation = funding('paper-state-funding')
        observe_funding_quotes(risk, observation)
        settled = controller.settle_funding(observation)
        report = reconcile_paper_state(bridge, controller,
                                       as_of=AT + pd.Timedelta(minutes=5))
        self.assertEqual(report.orders[0].state, 'OPEN')
        self.assertEqual(report.funding_cash, settled.cash_flow)
        self.assertEqual(sum((p.funding_cash for p in risk.portfolio.snapshot(
            as_of=AT + pd.Timedelta(minutes=5)).positions), D(0)), report.funding_cash)
        self.assertEqual(report.cash, D('2000') + report.realized_pnl)
        self.assertEqual(len([event for event in bridge.history if event.kind == 'EXIT_FILLED']), 0)

    def test_position_funding_tampering_is_rejected(self):
        risk, bridge, order, controller, _ = opened_controller()
        observation = funding('funding-tamper')
        observe_funding_quotes(risk, observation)
        controller.settle_funding(observation)
        before = (risk.portfolio.history, bridge.history)
        allocation_id = order.allocation.allocation_id
        position = risk.portfolio._positions[allocation_id]
        risk.portfolio._positions[allocation_id] = replace(
            position, funding_cash=position.funding_cash + D('1'))
        with self.assertRaisesRegex(ValueError, 'position-level funding cash'):
            reconcile_paper_state(bridge, controller, as_of=AT + pd.Timedelta(minutes=5))
        self.assertEqual((risk.portfolio.history, bridge.history), before)

    def test_multiple_position_funding_and_closed_position_carry_reconcile(self):
        risk = engine()
        bridge = FreqtradeSimulationBridge(risk, BINDINGS)
        orders = []
        for name, symbol in (('one', 'BTC/USDT'), ('two', 'ETH/USDT')):
            decision = decide(risk, proposal(name, symbol))
            order = bridge.propose(decision, as_of=AT).order
            self.assertIsNotNone(order)
            bridge.process_fill(fill(order, name='entry-' + name), as_of=AT)
            orders.append(order)
        controller = ExitFundingController(bridge)
        for name, symbol, rate in (('btc', 'BTC/USDT', '.001'),
                                   ('eth', 'ETH/USDT', '-.002')):
            observation = funding('fund-' + name, symbol=symbol, rate=rate)
            observe_funding_quotes(risk, observation)
            controller.settle_funding(observation)
        at = AT + pd.Timedelta(minutes=5)
        snap = risk.portfolio.snapshot(as_of=at)
        self.assertEqual(len(snap.positions), 2)
        self.assertEqual(sum((p.funding_cash for p in snap.positions), D(0)), snap.funding_cash)
        self.assertEqual(reconcile_paper_state(bridge, controller, as_of=at).funding_cash,
                         snap.funding_cash)
        bridge.process_fill(fill(orders[0], name='exit-btc', phase='EXIT', at=at,
                                 price=D('100'), fee=D('0')), as_of=at)
        self.assertEqual(reconcile_paper_state(bridge, controller, as_of=at).funding_cash,
                         snap.funding_cash)
        bridge.process_fill(fill(orders[1], name='exit-eth', phase='EXIT', at=at,
                                 price=D('100'), fee=D('0')), as_of=at)
        self.assertFalse(risk.portfolio.snapshot(as_of=at).positions)
        self.assertEqual(reconcile_paper_state(bridge, controller, as_of=at).funding_cash,
                         snap.funding_cash)

    def test_empty_positions_with_and_without_prior_funding_reconcile(self):
        risk, bridge, receipt = prepared()
        controller = ExitFundingController(bridge)
        self.assertEqual(reconcile_paper_state(bridge, controller, as_of=AT).funding_cash, 0)
        bridge.cancel(cancellation(receipt.order), as_of=AT)
        self.assertEqual(reconcile_paper_state(bridge, controller, as_of=AT).funding_cash, 0)

        risk, bridge, order, controller, _ = opened_controller()
        observation = funding('closed-funding')
        observe_funding_quotes(risk, observation)
        controller.settle_funding(observation)
        at = AT + pd.Timedelta(minutes=5)
        bridge.process_fill(fill(order, name='close', phase='EXIT', at=at,
                                 price=D('100'), fee=D('0')), as_of=at)
        self.assertFalse(risk.portfolio.snapshot(as_of=at).positions)
        self.assertEqual(reconcile_paper_state(bridge, controller, as_of=at).funding_cash,
                         risk.portfolio.snapshot(as_of=at).funding_cash)

    def test_malformed_lifecycle_events_fail_reconciliation(self):
        cases = (
            ('duplicate', 'identity'),
            ('unknown', 'Unknown simulated lifecycle'),
            ('nonbooking-effect', 'Non-booking lifecycle'),
        )
        for kind, message in cases:
            with self.subTest(kind=kind):
                _, bridge, receipt = prepared()
                controller = ExitFundingController(bridge)
                original = bridge._events[0]
                if kind == 'duplicate':
                    bridge._events.append(original)
                elif kind == 'unknown':
                    bridge._events[0] = replace(original, kind='UNRECOGNIZED')
                else:
                    bridge._events[0] = replace(original, portfolio_event_id='fabricated')
                with self.assertRaisesRegex(ValueError, message):
                    reconcile_paper_state(bridge, controller, as_of=AT)

    def test_orphan_reservation_double_allocation_and_missing_open_position_fail(self):
        risk, bridge, receipt = prepared()
        controller = ExitFundingController(bridge)
        risk.portfolio._reservations['orphan'] = replace(
            receipt.order.allocation, allocation_id='orphan')
        with self.assertRaisesRegex(ValueError, 'lacks its M8 approval'):
            reconcile_paper_state(bridge, controller, as_of=AT)

        risk, bridge, receipt = prepared()
        controller = ExitFundingController(bridge)
        bridge.process_fill(fill(receipt.order), as_of=AT)
        allocation_id = receipt.order.allocation.allocation_id
        risk.portfolio._reservations[allocation_id] = receipt.order.allocation
        with self.assertRaisesRegex(ValueError, 'simultaneously reserved and open'):
            reconcile_paper_state(bridge, controller, as_of=AT)

        risk, bridge, receipt = prepared()
        controller = ExitFundingController(bridge)
        bridge.process_fill(fill(receipt.order), as_of=AT)
        risk.portfolio._positions.clear()
        with self.assertRaisesRegex(ValueError, 'disagrees with its M8 allocation'):
            reconcile_paper_state(bridge, controller, as_of=AT)

    def test_unlinked_portfolio_booking_and_accounting_drift_are_detected(self):
        risk, bridge, receipt = prepared()
        controller = ExitFundingController(bridge)
        risk.simulate_open(receipt.risk.allocation_id, as_of=AT, event_id='out-of-band')
        with self.assertRaisesRegex(ValueError, 'outside the simulation bridge'):
            reconcile_paper_state(bridge, controller, as_of=AT)

        risk, bridge, receipt = prepared()
        controller = ExitFundingController(bridge)
        last = risk.portfolio._history[-1]
        risk.portfolio._history[-1] = replace(last, snapshot=replace(
            last.snapshot, cash=last.snapshot.cash + D('1')))
        with self.assertRaisesRegex(ValueError, 'accounting identity'):
            reconcile_paper_state(bridge, controller, as_of=AT)

        risk, bridge, receipt = prepared()
        controller = ExitFundingController(bridge)
        bridge.process_fill(fill(receipt.order), as_of=AT)
        last = risk.portfolio._history[-1]
        risk.portfolio._history[-1] = replace(last, snapshot=replace(
            last.snapshot, cash=last.snapshot.cash + D('1'),
            realized_pnl=last.snapshot.realized_pnl + D('1'),
            equity=last.snapshot.equity + D('1'),
            available_capital=last.snapshot.available_capital + D('1')))
        with self.assertRaisesRegex(ValueError, 'fill and portfolio accounting'):
            reconcile_paper_state(bridge, controller, as_of=AT)


if __name__ == '__main__':
    unittest.main()
