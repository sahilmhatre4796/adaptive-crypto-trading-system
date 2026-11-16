"""Deterministic offline M12 paper execution boundary tests."""
import sys
sys.dont_write_bytecode = True

from dataclasses import replace
from decimal import Decimal as D
import unittest
from unittest.mock import patch

import pandas as pd

from trading.exit_funding import ExitFundingController
from trading.freqtrade_bridge import FreqtradeSimulationBridge, SimulationFill
from trading.paper_execution import OfflinePaperExecution
from trading.paper_state import reconcile_paper_state
from test_freqtrade_bridge import BINDINGS, cancellation, fill, prepared
from test_portfolio import AT, spec
from test_risk import config, costs, decide, engine, proposal


class OfflinePaperExecutionTests(unittest.TestCase):
    def test_acknowledged_entry_and_exit_use_only_bridge_and_m8_accounting(self):
        model = costs(entry_fee_rate='.001', exit_fee_rate='.002')
        risk, bridge, receipt = prepared(model=model)
        order = receipt.order
        actor = OfflinePaperExecution(bridge)
        ack = actor.acknowledge(receipt, event_id='ack-1', as_of=AT)
        self.assertEqual((ack.kind, bridge.state(order.order_id)), ('ACKNOWLEDGED', 'PROPOSED'))
        self.assertEqual(risk.portfolio.snapshot(as_of=AT).reserved_capital,
                         receipt.risk.capital_reserved)
        entry = actor.fill(fill(order), as_of=AT)
        self.assertEqual((entry.kind, entry.lifecycle.state), ('ENTRY_FILLED', 'OPEN'))
        self.assertEqual(risk.portfolio.snapshot(as_of=AT).cash,
                         D('2000') - order.allocation.entry_fee)
        self.assertFalse(risk.portfolio.snapshot(as_of=AT).reservations)
        exit_event = actor.fill(fill(order, name='exit', phase='EXIT',
                                     price=order.modeled_stop_execution_price,
                                     fee=model.exit_fee_rate), as_of=AT)
        self.assertEqual((exit_event.kind, exit_event.lifecycle.state), ('EXIT_FILLED', 'CLOSED'))
        self.assertEqual(D('2000') - risk.portfolio.snapshot(as_of=AT).cash,
                         receipt.risk.planned_loss)
        self.assertEqual([event.kind for event in actor.history],
                         ['ACKNOWLEDGED', 'ENTRY_FILLED', 'EXIT_FILLED'])
        report = reconcile_paper_state(bridge, ExitFundingController(bridge), as_of=AT)
        self.assertEqual(report.orders[0].state, 'CLOSED')
        self.assertEqual(report.cash, risk.portfolio.snapshot(as_of=AT).cash)

    def test_denied_m8_decisions_never_become_execution_intents(self):
        for status in ('REJECTED', 'BLOCKED'):
            with self.subTest(status=status):
                risk = engine(config(max_open_positions=1))
                bridge = FreqtradeSimulationBridge(risk, BINDINGS)
                if status == 'REJECTED':
                    decision = decide(risk, instrument=spec(min_quantity=1000))
                else:
                    decide(risk)
                    decision = decide(risk, proposal('two', 'ETH/USDT'))
                receipt = bridge.propose(decision, as_of=AT)
                actor = OfflinePaperExecution(bridge)
                record = actor.acknowledge(receipt, event_id='denied', as_of=AT)
                self.assertEqual((decision.status, record.kind), (status, 'DENIED'))
                self.assertEqual(record.source.reason_codes, decision.reason_codes)
                self.assertFalse(bridge.orders)
                self.assertFalse(bridge.history)
                with self.assertRaisesRegex(ValueError, 'acknowledged'):
                    actor.fill(SimulationFill('no-order', 'invented', 'ENTRY', D('1'),
                                              D('100'), D('0'), AT, AT, 'fixture'), as_of=AT)

    def test_execution_rejection_and_cancellation_release_reservations(self):
        for rejected in (True, False):
            with self.subTest(rejected=rejected):
                risk, bridge, receipt = prepared()
                actor = OfflinePaperExecution(bridge)
                actor.acknowledge(receipt, event_id='ack', as_of=AT)
                request = cancellation(receipt.order, name='execution-rejected' if rejected else 'cancelled')
                request = replace(request, reason='PAPER_EXECUTION_REJECTED' if rejected else 'OPERATOR_CANCEL')
                record = actor.cancel(request, as_of=AT, rejected=rejected)
                self.assertEqual((record.kind, record.lifecycle.state),
                                 ('REJECTED' if rejected else 'CANCELLED', 'CANCELLED'))
                self.assertFalse(risk.portfolio.snapshot(as_of=AT).reservations)
                self.assertEqual(bridge.state(receipt.order.order_id), 'CANCELLED')
                self.assertEqual(actor.cancel(request, as_of=AT, rejected=rejected), record)
                with self.assertRaisesRegex(ValueError, 'revise'):
                    actor.cancel(request, as_of=AT, rejected=not rejected)
                self.assertEqual(len(actor.history), 2)
                self.assertEqual(reconcile_paper_state(bridge, ExitFundingController(bridge),
                                 as_of=AT).orders[0].state, 'CANCELLED')

    def test_cancellation_after_entry_fill_cannot_close_or_release_position(self):
        risk, bridge, receipt = prepared()
        actor = OfflinePaperExecution(bridge)
        actor.acknowledge(receipt, event_id='ack', as_of=AT)
        actor.fill(fill(receipt.order), as_of=AT)
        before = (risk.portfolio.history, bridge.history, actor.history)
        with self.assertRaisesRegex(ValueError, 'PROPOSED'):
            actor.cancel(cancellation(receipt.order), as_of=AT)
        self.assertEqual((risk.portfolio.history, bridge.history, actor.history), before)
        self.assertEqual(bridge.state(receipt.order.order_id), 'OPEN')
        self.assertEqual(len(risk.portfolio.snapshot(as_of=AT).positions), 1)
        with self.assertRaisesRegex(RuntimeError, 'quarantined'):
            actor.cancel(cancellation(receipt.order, name='retry'), as_of=AT)

    def test_acknowledgement_requires_active_authoritative_reservation(self):
        risk, bridge, receipt = prepared()
        actor = OfflinePaperExecution(bridge)
        risk.portfolio.cancel(receipt.order.allocation.allocation_id,
                              as_of=AT, event_id='external-reservation-cancel')
        before = (risk.portfolio.history, bridge.history)
        with self.assertRaisesRegex(ValueError, 'authoritative M8 reservation'):
            actor.acknowledge(receipt, event_id='ack', as_of=AT)
        self.assertEqual((risk.portfolio.history, bridge.history), before)
        self.assertFalse(actor.history)

    def test_multiple_sequential_intents_keep_acknowledgement_order(self):
        risk = engine()
        bridge = FreqtradeSimulationBridge(risk, BINDINGS)
        first = bridge.propose(decide(risk, proposal('one', 'BTC/USDT')), as_of=AT)
        second = bridge.propose(decide(risk, proposal('two', 'ETH/USDT')), as_of=AT)
        self.assertIsNotNone(first.order)
        self.assertIsNotNone(second.order)
        actor = OfflinePaperExecution(bridge)
        a = actor.acknowledge(first, event_id='ack-btc', as_of=AT)
        b = actor.acknowledge(second, event_id='ack-eth', as_of=AT)
        self.assertEqual(actor.history, (a, b))
        self.assertEqual([record.source.order.binding.symbol for record in actor.history],
                         ['BTC/USDT', 'ETH/USDT'])
        self.assertIs(actor.acknowledge(first, event_id='ack-btc', as_of=AT), a)
        self.assertEqual(len(actor.history), 2)
        self.assertEqual(len(risk.portfolio.snapshot(as_of=AT).reservations), 2)

    def test_duplicate_and_revised_events_do_not_double_book(self):
        risk, bridge, receipt = prepared()
        actor = OfflinePaperExecution(bridge)
        ack = actor.acknowledge(receipt, event_id='ack', as_of=AT)
        self.assertIs(actor.acknowledge(receipt, event_id='ack', as_of=AT), ack)
        with self.assertRaisesRegex(ValueError, 'causal chronological'):
            actor.acknowledge(receipt, event_id='ack', as_of=AT - pd.Timedelta(seconds=1))
        with self.assertRaisesRegex(ValueError, 'another event ID'):
            actor.acknowledge(receipt, event_id='different-ack', as_of=AT)
        entry = fill(receipt.order)
        booked = actor.fill(entry, as_of=AT)
        before = (risk.portfolio.history, bridge.history, actor.history)
        self.assertIs(actor.fill(entry, as_of=AT), booked)
        with self.assertRaisesRegex(ValueError, 'revise'):
            actor.fill(replace(entry, price=D('101')), as_of=AT)
        self.assertEqual((risk.portfolio.history, bridge.history, actor.history), before)

    def test_forged_stale_and_unacknowledged_inputs_cannot_execute(self):
        risk, bridge, receipt = prepared()
        actor = OfflinePaperExecution(bridge)
        before = (risk.portfolio.history, bridge.history)
        with self.assertRaisesRegex(ValueError, 'acknowledged'):
            actor.fill(fill(receipt.order), as_of=AT)
        with self.assertRaisesRegex(ValueError, 'acknowledged'):
            actor.cancel(cancellation(receipt.order), as_of=AT)
        with self.assertRaisesRegex(ValueError, 'absent from authoritative'):
            actor.acknowledge(replace(receipt, receipt_id='fabricated'),
                              event_id='forged', as_of=AT)
        with self.assertRaisesRegex(ValueError, 'causal chronological'):
            actor.acknowledge(receipt, event_id='early',
                              as_of=AT - pd.Timedelta(seconds=1))
        with self.assertRaisesRegex(ValueError, 'unfilled M8-approved intent'):
            actor.acknowledge(receipt, event_id='expired',
                              as_of=receipt.order.expires_at)
        self.assertEqual((risk.portfolio.history, bridge.history), before)
        self.assertFalse(actor.history)

    def test_single_bridge_owner_and_later_denial_prevent_backdated_ack(self):
        risk = engine(config(max_open_positions=1))
        bridge = FreqtradeSimulationBridge(risk, BINDINGS)
        first = bridge.propose(decide(risk), as_of=AT)
        actor = OfflinePaperExecution(bridge)
        with self.assertRaisesRegex(ValueError, 'already has'):
            OfflinePaperExecution(bridge)
        later = AT + pd.Timedelta(seconds=1)
        blocked = decide(risk, proposal('two', 'ETH/USDT', at=later), at=later)
        self.assertEqual(bridge.propose(blocked, as_of=later).disposition, 'DENIED')
        with self.assertRaisesRegex(ValueError, 'causal chronological'):
            actor.acknowledge(first, event_id='backdated', as_of=AT)
        self.assertEqual(actor.acknowledge(first, event_id='later-ack',
                                          as_of=later).kind, 'ACKNOWLEDGED')

    def test_partial_and_invalid_entry_prices_fail_without_booking(self):
        risk, bridge, receipt = prepared()
        actor = OfflinePaperExecution(bridge)
        actor.acknowledge(receipt, event_id='ack', as_of=AT)
        entry = fill(receipt.order)
        before = (risk.portfolio.history, bridge.history)
        with self.assertRaisesRegex(ValueError, 'Partial'):
            actor.fill(replace(entry, quantity=entry.quantity / 2), as_of=AT)
        with self.assertRaisesRegex(ValueError, 'Partial'):
            actor.fill(replace(entry, quantity=entry.quantity * 2), as_of=AT)
        with self.assertRaises(ValueError):
            replace(entry, price=D('0'))
        self.assertEqual((risk.portfolio.history, bridge.history), before)
        with self.assertRaisesRegex(ValueError, 'M8-approved price'):
            actor.fill(replace(entry, price=D('101')), as_of=AT)
        self.assertEqual((risk.portfolio.history, bridge.history), before)
        with self.assertRaisesRegex(RuntimeError, 'quarantined'):
            actor.fill(entry, as_of=AT)

    def test_replay_is_deterministic_and_actor_failure_is_quarantined(self):
        def scenario():
            risk, bridge, receipt = prepared()
            actor = OfflinePaperExecution(bridge)
            actor.acknowledge(receipt, event_id='ack', as_of=AT)
            actor.fill(fill(receipt.order), as_of=AT)
            return actor.history, bridge.history, risk.portfolio.history
        self.assertEqual(scenario(), scenario())

        risk, bridge, receipt = prepared()
        actor = OfflinePaperExecution(bridge)
        actor.acknowledge(receipt, event_id='ack', as_of=AT)
        original = bridge.process_fill
        def mutate_then_fail(execution, *, as_of):
            original(execution, as_of=as_of)
            raise RuntimeError('interrupted after M9.1 booking')
        with patch.object(bridge, 'process_fill', side_effect=mutate_then_fail):
            with self.assertRaisesRegex(RuntimeError, 'interrupted'):
                actor.fill(fill(receipt.order), as_of=AT)
        self.assertEqual(bridge.state(receipt.order.order_id), 'OPEN')
        self.assertEqual(len(actor.history), 1)
        with self.assertRaisesRegex(RuntimeError, 'quarantined'):
            actor.fill(fill(receipt.order), as_of=AT)


if __name__ == '__main__':
    unittest.main()
