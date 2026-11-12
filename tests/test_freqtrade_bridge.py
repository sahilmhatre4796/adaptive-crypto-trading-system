import sys
sys.dont_write_bytecode = True

from dataclasses import FrozenInstanceError, replace
from decimal import Decimal as D
import json
import unittest
from unittest.mock import patch

import pandas as pd

from trading.freqtrade_bridge import (FreqtradeSimulationBridge, PairBinding, SimulationCancellation,
                                      SimulationFill)
from trading.portfolio import Portfolio, identifier
from trading.risk import RiskEngine
from trading.signals import SignalLifecycleEvent, SignalTracker
from trading.signal_context import assemble_context
from test_risk import AT, config, costs, decide, engine, proposal, quote, spec
from test_signal_context import CONFIG, inputs
from test_signals import policy

BINDINGS = tuple(PairBinding(s, s+':USDT', 'USDT', 'explicit synthetic mapping; runtime unverified')
                 for s in ('BTC/USDT', 'ETH/USDT'))


def prepared(*, risk=None, side='LONG', symbol='BTC/USDT', requested=None, model=None):
    risk = risk or engine()
    bridge = FreqtradeSimulationBridge(risk, BINDINGS)
    decision = decide(risk, proposal(symbol=symbol, side=side), assumptions=model, requested_quantity=requested)
    receipt = bridge.propose(decision, as_of=AT)
    return risk, bridge, receipt


def fill(order, *, name='entry', phase='ENTRY', at=None, price=None, quantity=None, fee=None):
    at = order.created_at if at is None else at
    return SimulationFill(name, order.order_id, phase,
                          order.quantity_contracts if quantity is None else quantity,
                          order.entry_price if price is None else price,
                          order.risk.costs.entry_fee_rate if fee is None else fee,
                          at, at, 'explicit synthetic fill')


def cancellation(order, *, name='cancel', at=None):
    at = order.created_at if at is None else at
    return SimulationCancellation(name, order.order_id, 'FIXTURE_CANCEL', at, at, 'offline fixture')


class ApprovalBoundaryTests(unittest.TestCase):
    def test_approved_and_reduced_decisions_preserve_exact_m8_sizing_and_units(self):
        for requested, status in ((None, 'APPROVED'), ('1.05', 'REDUCED')):
            for symbol in ('BTC/USDT', 'ETH/USDT'):
                for side in ('LONG', 'SHORT'):
                    with self.subTest(requested=requested, symbol=symbol, side=side):
                        risk, bridge, receipt = prepared(requested=requested, symbol=symbol, side=side)
                        d, order = receipt.risk, receipt.order
                        self.assertEqual((receipt.disposition, d.status), ('ACCEPTED', status))
                        self.assertEqual(order.quantity_contracts, d.quantity)
                        self.assertEqual(order.amount_base, d.quantity*d.instrument.multiplier)
                        self.assertEqual(order.stake_quote, d.margin/order.allocation.fx)
                        self.assertEqual(order.notional_quote, d.notional_account/order.allocation.fx)
                        self.assertEqual(order.leverage, d.leverage)
                        self.assertEqual(order.expires_at, d.proposal.expires_at)
                        self.assertEqual(order.entry_action, 'BUY' if side == 'LONG' else 'SELL')
                        self.assertTrue(order.simulation_only)
                        self.assertIs(order.risk, risk.history[0])
                        self.assertEqual(bridge.state(order.order_id), 'PROPOSED')
                        self.assertEqual(receipt.reason_codes, d.reason_codes)

    def test_rejected_and_blocked_decisions_cannot_emit_an_order_or_accept_fills(self):
        for status in ('REJECTED', 'BLOCKED'):
            risk = engine(config(max_open_positions=1))
            bridge = FreqtradeSimulationBridge(risk, BINDINGS)
            if status == 'REJECTED':
                d = decide(risk, instrument=spec(min_quantity=1000))
            else:
                decide(risk)
                d = decide(risk, proposal('two', 'ETH/USDT'))
            before = risk.portfolio.history
            receipt = bridge.propose(d, as_of=AT)
            self.assertEqual((receipt.disposition, d.status), ('DENIED', status))
            self.assertIsNone(receipt.order)
            self.assertEqual(receipt.reason_codes, d.reason_codes)
            self.assertFalse(bridge.orders)
            self.assertFalse(bridge.history)
            with self.assertRaises(ValueError):
                bridge.process_fill(SimulationFill('fake', 'manufactured-order', 'ENTRY', 1, 100, 0,
                                                    AT, AT, 'fixture'), as_of=AT)
            self.assertEqual(risk.portfolio.history, before)

    def test_forged_rejection_approval_and_tampered_approved_records_fail(self):
        risk = engine()
        bridge = FreqtradeSimulationBridge(risk, BINDINGS)
        rejected = decide(risk, instrument=spec(min_quantity=1000))
        fake = replace(rejected, status='APPROVED', quantity=D(20), allocation_id='fake')
        with self.assertRaisesRegex(ValueError, 'authoritative M8 history'):
            bridge.propose(fake, as_of=AT)
        risk, bridge, receipt = prepared()
        for changes in (dict(quantity=receipt.risk.quantity+1), dict(allocation_id='fake'),
                        dict(reason_codes=()), dict(configuration_id='fake')):
            with self.assertRaises(ValueError):
                bridge.propose(replace(receipt.risk, **changes), as_of=AT)
        self.assertEqual(len(bridge.orders), 1)
        self.assertEqual(len(risk.portfolio.snapshot(as_of=AT).reservations), 1)

    def test_unknown_actor_raw_m7_and_raw_payloads_cannot_bypass_m8(self):
        risk = engine()
        bridge = FreqtradeSimulationBridge(risk, BINDINGS)
        unrelated = decide(engine())
        for record in (unrelated, {'status': 'APPROVED'}, proposal(), None):
            with self.assertRaises(ValueError):
                bridge.propose(record, as_of=AT)
        self.assertFalse(bridge.orders)
        self.assertFalse(risk.history)

    def test_no_default_pair_mapping_or_second_adapter_can_reset_consumption(self):
        risk = engine()
        for bindings in ((), list(BINDINGS), (BINDINGS[0], BINDINGS[0])):
            with self.assertRaises(ValueError):
                FreqtradeSimulationBridge(risk, bindings)
        FreqtradeSimulationBridge(risk, BINDINGS)
        with self.assertRaises(ValueError):
            FreqtradeSimulationBridge(risk, BINDINGS)
        with self.assertRaises(ValueError):
            PairBinding('BTC/USDT', 'fixture-pair', 'INR', 'fixture')

    def test_nonunit_multiplier_fx_and_stake_are_converted_explicitly(self):
        p = Portfolio(as_of=AT)
        p.observe(quote('USDT', 80), kind='FX', as_of=AT, event_id='fx')
        p.observe(quote(), kind='MARK', as_of=AT, event_id='mark')
        risk = RiskEngine(p, config(), mode='BACKTEST')
        bridge = FreqtradeSimulationBridge(risk, BINDINGS)
        d = decide(risk, instrument=spec(multiplier=10, quantity_step='.001', min_quantity='.001'))
        order = bridge.propose(d, as_of=AT).order
        self.assertEqual((order.quantity_contracts, order.amount_base, order.stake_quote), (D('.025'), D('.25'), D('12.5')))
        self.assertEqual(order.notional_quote, D(25))

    def test_duplicate_proposal_is_a_historical_receipt_not_a_new_order(self):
        risk, bridge, receipt = prepared()
        before = risk.portfolio.history
        self.assertIs(bridge.propose(receipt.risk, as_of=AT), receipt)
        bridge.cancel(cancellation(receipt.order), as_of=AT)
        self.assertIs(bridge.propose(receipt.risk, as_of=AT), receipt)
        self.assertEqual(bridge.state(receipt.order.order_id), 'CANCELLED')
        self.assertEqual(len(bridge.orders), 1)
        self.assertEqual(len(bridge.receipts), 1)
        self.assertEqual(risk.portfolio.history[:len(before)], before)

    def test_missing_cancelled_stale_or_changed_reservation_denies_ingress(self):
        for scenario, reason in (('cancelled', 'M9_RESERVATION_NOT_ACTIVE'),
                                 ('stale', 'M9_SOURCE_DATA_STALE'), ('changed', 'M9_MARK_OR_FX_CHANGED')):
            risk = engine()
            bridge = FreqtradeSimulationBridge(risk, BINDINGS)
            d = decide(risk)
            at = AT
            if scenario == 'cancelled':
                risk.portfolio.cancel(d.allocation_id, as_of=AT, event_id='external-cancel')
            elif scenario == 'stale':
                at += pd.Timedelta(seconds=301)
            else:
                at += pd.Timedelta(seconds=1)
                risk.portfolio.observe(quote(value=101, at=at), kind='MARK', as_of=at, event_id='changed')
            receipt = bridge.propose(d, as_of=at)
            self.assertEqual(receipt.disposition, 'DENIED')
            self.assertIn(reason, receipt.reason_codes)
            self.assertFalse(bridge.orders)

    def test_exact_source_freshness_deadline_remains_usable_then_expires(self):
        for seconds, expected in ((300, 'ACCEPTED'), (301, 'DENIED')):
            risk = engine()
            bridge = FreqtradeSimulationBridge(risk, BINDINGS)
            d = decide(risk)
            at = AT+pd.Timedelta(seconds=seconds)
            receipt = bridge.propose(d, as_of=at)
            self.assertEqual(receipt.disposition, expected)
            if receipt.order:
                self.assertEqual(bridge.process_fill(fill(receipt.order, at=at), as_of=at).state, 'OPEN')


class SimulationLifecycleTests(unittest.TestCase):
    def test_full_entry_and_exit_lifecycle_with_both_directions_and_costs(self):
        for side in ('LONG', 'SHORT'):
            model = costs(entry_fee_rate='.001', exit_fee_rate='.002', entry_slippage_rate='.001', stop_slippage_rate='.001')
            risk, bridge, receipt = prepared(side=side, model=model)
            order = receipt.order
            opening = bridge.process_fill(fill(order), as_of=AT)
            self.assertEqual((opening.kind, opening.state), ('ENTRY_FILLED', 'OPEN'))
            self.assertEqual(len(risk.portfolio.snapshot(as_of=AT).positions), 1)
            exit_fill = fill(order, name='exit', phase='EXIT', price=order.modeled_stop_execution_price, fee=model.exit_fee_rate)
            closing = bridge.process_fill(exit_fill, as_of=AT)
            self.assertEqual((closing.kind, closing.state), ('EXIT_FILLED', 'CLOSED'))
            self.assertFalse(risk.portfolio.snapshot(as_of=AT).positions)
            self.assertEqual(D(2000)-risk.portfolio.snapshot(as_of=AT).equity, receipt.risk.planned_loss)
            self.assertEqual([e.state for e in bridge.history], ['PROPOSED', 'OPEN', 'CLOSED'])
            self.assertEqual(closing.allocation_id, receipt.risk.allocation_id)

    def test_delayed_receipt_preserves_times_without_backdating_booking(self):
        risk, bridge, receipt = prepared()
        observed = AT+pd.Timedelta(seconds=1)
        received = AT+pd.Timedelta(seconds=3)
        at = AT+pd.Timedelta(seconds=4)
        request = replace(fill(receipt.order, at=observed), available_at=received)
        event = bridge.process_fill(request, as_of=at)
        self.assertEqual((event.observed_at, event.source_available_at, event.available_at), (observed, received, at))
        self.assertEqual(risk.portfolio.snapshot(as_of=at).positions[0].opened_at, at)

    def test_entry_price_fee_partial_and_oversized_fill_mismatches_are_atomic(self):
        for field, value in (('price', D(101)), ('fee_rate', D('.01')), ('quantity', D(1)), ('quantity', D(21))):
            risk, bridge, receipt = prepared()
            before, history = risk.portfolio.history, bridge.history
            with self.assertRaises(ValueError):
                bridge.process_fill(replace(fill(receipt.order), **{field: value}), as_of=AT)
            self.assertEqual(risk.portfolio.history, before)
            self.assertEqual(bridge.history, history)
            self.assertEqual(bridge.state(receipt.order.order_id), 'PROPOSED')
            bridge.process_fill(fill(receipt.order), as_of=AT)
            self.assertEqual(bridge.state(receipt.order.order_id), 'OPEN')

    def test_m8_booking_rechecks_cannot_be_bypassed_by_an_old_approved_intent(self):
        risk, bridge, receipt = prepared()
        at = AT+pd.Timedelta(seconds=1)
        risk.portfolio.observe(quote(value=101, at=at), kind='MARK', as_of=at, event_id='changed')
        before, history = risk.portfolio.history, bridge.history
        with self.assertRaises(ValueError):
            bridge.process_fill(fill(receipt.order, at=at), as_of=at)
        self.assertEqual((risk.portfolio.history, bridge.history), (before, history))

    def test_out_of_band_open_cannot_be_double_booked(self):
        risk, bridge, receipt = prepared()
        risk.simulate_open(receipt.risk.allocation_id, as_of=AT, event_id='external-open')
        before = risk.portfolio.history
        with self.assertRaises(ValueError):
            bridge.process_fill(fill(receipt.order), as_of=AT)
        self.assertEqual(risk.portfolio.history, before)

    def test_invalid_transitions_and_post_cancel_fills_fail(self):
        _, bridge, receipt = prepared()
        order = receipt.order
        with self.assertRaises(ValueError):
            bridge.process_fill(fill(order, phase='EXIT'), as_of=AT)
        bridge.cancel(cancellation(order), as_of=AT)
        with self.assertRaises(ValueError):
            bridge.process_fill(fill(order), as_of=AT)
        with self.assertRaises(ValueError):
            bridge.cancel(cancellation(order, name='different'), as_of=AT)
        _, bridge, receipt = prepared()
        bridge.process_fill(fill(receipt.order), as_of=AT)
        with self.assertRaises(ValueError):
            bridge.cancel(cancellation(receipt.order), as_of=AT)

    def test_duplicate_callbacks_revisions_and_terminal_state_are_preserved(self):
        risk, bridge, receipt = prepared()
        first_fill = fill(receipt.order)
        opened = bridge.process_fill(first_fill, as_of=AT)
        self.assertIs(bridge.process_fill(first_fill, as_of=AT), opened)
        with self.assertRaisesRegex(ValueError, 'revise'):
            bridge.process_fill(replace(first_fill, price=D(101)), as_of=AT)
        close_fill = fill(receipt.order, name='exit', phase='EXIT', price=105, fee=0)
        closed = bridge.process_fill(close_fill, as_of=AT)
        before = risk.portfolio.history
        self.assertIs(bridge.process_fill(close_fill, as_of=AT), closed)
        self.assertIs(bridge.process_fill(first_fill, as_of=AT+pd.Timedelta(seconds=1)), opened)
        self.assertEqual(bridge.state(receipt.order.order_id), 'CLOSED')
        self.assertEqual(risk.portfolio.history, before)
        with self.assertRaises(ValueError):
            bridge.process_fill(close_fill, as_of=AT)

    def test_cancel_is_idempotent_releases_capital_and_retains_signal_consumption(self):
        risk, bridge, receipt = prepared()
        request = cancellation(receipt.order)
        event = bridge.cancel(request, as_of=AT)
        self.assertIs(bridge.cancel(request, as_of=AT), event)
        self.assertEqual(risk.portfolio.snapshot(as_of=AT).reserved_capital, 0)
        again = decide(risk, requested_quantity=1)
        denied = bridge.propose(again, as_of=AT)
        self.assertEqual((again.status, denied.disposition), ('BLOCKED', 'DENIED'))

    def test_expiry_and_late_entry_receipt_cannot_extend_source_validity(self):
        risk = engine()
        bridge = FreqtradeSimulationBridge(risk, BINDINGS)
        d = decide(risk, replace(proposal(), expires_at=AT+pd.Timedelta(seconds=10)))
        order = bridge.propose(d, as_of=AT).order
        with self.assertRaises(ValueError):
            bridge.expire(order.order_id, as_of=AT+pd.Timedelta(seconds=9))
        late = replace(fill(order, at=AT+pd.Timedelta(seconds=9)), available_at=AT+pd.Timedelta(seconds=10))
        with self.assertRaises(ValueError):
            bridge.process_fill(late, as_of=late.available_at)
        event = bridge.expire(order.order_id, as_of=late.available_at)
        self.assertEqual((event.state, event.available_at), ('EXPIRED', late.available_at))
        self.assertIs(bridge.expire(order.order_id, as_of=AT+pd.Timedelta(seconds=11)), event)
        self.assertFalse(risk.portfolio.snapshot(as_of=AT+pd.Timedelta(seconds=11)).reservations)

    def test_future_unavailable_or_pre_intent_fill_cannot_mutate_state(self):
        risk, bridge, receipt = prepared()
        before = risk.portfolio.history
        for request in (replace(fill(receipt.order), available_at=AT+pd.Timedelta(seconds=1)),
                        fill(receipt.order, at=AT-pd.Timedelta(seconds=1))):
            with self.assertRaises(ValueError):
                bridge.process_fill(request, as_of=AT)
        self.assertEqual(risk.portfolio.history, before)
        with self.assertRaises(ValueError):
            replace(fill(receipt.order), observed_at=AT+pd.Timedelta(seconds=1))

    def test_stale_exit_fx_fails_without_losing_callback_then_explicit_refresh_allows_booking(self):
        risk, bridge, receipt = prepared()
        bridge.process_fill(fill(receipt.order), as_of=AT)
        at = AT+pd.Timedelta(seconds=301)
        request = fill(receipt.order, name='exit', phase='EXIT', at=at, price=105, fee=0)
        before = risk.portfolio.history
        with self.assertRaisesRegex(ValueError, 'FX'):
            bridge.process_fill(request, as_of=at)
        self.assertEqual(risk.portfolio.history, before)
        risk.portfolio.observe(quote('USDT', 1, at), kind='FX', as_of=at, event_id='fresh-fx')
        self.assertEqual(bridge.process_fill(request, as_of=at).state, 'CLOSED')

    def test_upstream_terminal_cancels_pending_but_only_notifies_open_positions(self):
        for opened in (False, True):
            risk, bridge, receipt = prepared()
            if opened:
                bridge.process_fill(fill(receipt.order), as_of=AT)
            at = AT+pd.Timedelta(seconds=1)
            source = SignalLifecycleEvent('m7-invalidated', receipt.risk.signal_id, 'INVALIDATED', at, at, 'bar', 'upstream reason')
            notices = bridge.on_signal_terminal(source, as_of=at)
            self.assertEqual(notices[0].state, 'OPEN' if opened else 'CANCELLED')
            self.assertEqual(notices[0].source_available_at, source.available_at)
            self.assertIn(source.reason, notices[0].reason_codes)
            self.assertIs(bridge.on_signal_terminal(source, as_of=at), notices)
            self.assertEqual(len(risk.portfolio.snapshot(as_of=at).positions), 1 if opened else 0)
            if opened:
                request = fill(receipt.order, name='exit', phase='EXIT', at=at, price=95, fee=0)
                self.assertEqual(bridge.process_fill(request, as_of=at).state, 'CLOSED')

    def test_out_of_band_position_change_fails_terminal_sync_without_false_state(self):
        for external_open in (True, False):
            risk, bridge, receipt = prepared()
            if external_open:
                risk.simulate_open(receipt.risk.allocation_id, as_of=AT, event_id='external-open')
            else:
                bridge.process_fill(fill(receipt.order), as_of=AT)
                risk.portfolio.simulate_close(receipt.risk.allocation_id, 100, 0, as_of=AT, event_id='external-close')
            source = SignalLifecycleEvent('terminal', receipt.risk.signal_id, 'INVALIDATED', AT, AT, 'bar', 'fixture')
            before = (risk.portfolio.history, risk.terminal_history, bridge.history)
            with self.assertRaisesRegex(ValueError, 'outside the bridge'):
                bridge.on_signal_terminal(source, as_of=AT)
            self.assertEqual((risk.portfolio.history, risk.terminal_history, bridge.history), before)


class M7WorkflowTests(unittest.TestCase):
    def actual(self):
        context = assemble_context(**inputs())
        tracker = SignalTracker(policy(), CONFIG, mode='BACKTEST')
        record = tracker.observe(context)
        p = Portfolio(as_of=record.available_at)
        p.observe(quote(record.symbol, record.reference_price, record.available_at), kind='MARK', as_of=record.available_at, event_id='mark')
        p.observe(quote('USDT', 80, record.available_at), kind='FX', as_of=record.available_at, event_id='fx')
        risk = RiskEngine(p, config(), mode='BACKTEST')
        return record, risk, FreqtradeSimulationBridge(risk, BINDINGS)

    def test_actual_m7_m8_workflow_and_source_provenance_are_preserved(self):
        record, risk, bridge = self.actual()
        before = identifier(record.to_dict())
        instrument = spec(available_at=record.available_at)
        receipt = bridge.submit_signal(record, instrument, costs(), leverage=2, as_of=record.available_at, requested_quantity='.1')
        self.assertEqual(receipt.disposition, 'ACCEPTED')
        self.assertEqual(receipt.risk.proposal.signal_id, record.signal_id)
        self.assertEqual(receipt.risk.proposal.source_input_id, record.input_id)
        self.assertEqual(receipt.risk.proposal.source_policy_id, record.policy_id)
        self.assertEqual(receipt.risk.proposal.available_at, record.available_at)
        self.assertIs(bridge.submit_signal(record, instrument, costs(), leverage=2, as_of=record.available_at, requested_quantity='.1'), receipt)
        self.assertEqual(identifier(record.to_dict()), before)
        self.assertEqual(len(risk.history), 1)

    def test_m7_qualification_alone_cannot_bypass_m8_rejection(self):
        record, risk, bridge = self.actual()
        with self.assertRaises(ValueError):
            bridge.propose(record, as_of=record.available_at)
        receipt = bridge.submit_signal(record, spec(min_quantity=1000, available_at=record.available_at),
                                       costs(), leverage=2, as_of=record.available_at)
        self.assertEqual((receipt.risk.status, receipt.disposition, receipt.order), ('REJECTED', 'DENIED', None))
        self.assertFalse(risk.portfolio.snapshot(as_of=record.available_at).positions)
        self.assertFalse(bridge.orders)

    def test_resubmitted_terminal_evidence_revokes_old_intent_without_future_leakage(self):
        record, risk, bridge = self.actual()
        instrument = spec(available_at=record.available_at)
        receipt = bridge.submit_signal(record, instrument, costs(), leverage=2,
                                        as_of=record.available_at, requested_quantity='.1')
        at = record.available_at+pd.Timedelta(seconds=1)
        terminal = SignalLifecycleEvent('later-terminal', record.signal_id, 'INVALIDATED', at, at, 'bar', 'fixture')
        self.assertIs(bridge.submit_signal(record, instrument, costs(), leverage=2, as_of=record.available_at,
                                           requested_quantity='.1', events=(terminal,)), receipt)
        self.assertFalse(risk.terminal_history)
        self.assertEqual(bridge.state(receipt.order.order_id), 'PROPOSED')
        denied = bridge.submit_signal(record, instrument, costs(), leverage=2, as_of=at,
                                       requested_quantity='.1', events=(terminal,))
        self.assertEqual(denied.disposition, 'DENIED')
        self.assertEqual(bridge.state(receipt.order.order_id), 'CANCELLED')
        self.assertEqual(len(risk.terminal_history), 1)
        with self.assertRaises(ValueError):
            bridge.process_fill(fill(receipt.order, at=at), as_of=at)

    def test_missing_costs_rules_and_fx_are_not_assumed(self):
        record, risk, bridge = self.actual()
        for instrument, model in ((None, costs()), (spec(available_at=record.available_at), None)):
            with self.assertRaises(ValueError):
                bridge.submit_signal(record, instrument, model, leverage=2, as_of=record.available_at)
        self.assertFalse(risk.history)
        p = Portfolio(as_of=record.available_at)
        p.observe(quote(record.symbol, record.reference_price, record.available_at), kind='MARK',
                  as_of=record.available_at, event_id='mark')
        missing_fx = FreqtradeSimulationBridge(RiskEngine(p, config(), mode='BACKTEST'), BINDINGS)
        denied = missing_fx.submit_signal(record, spec(available_at=record.available_at), costs(),
                                           leverage=2, as_of=record.available_at)
        self.assertEqual(denied.disposition, 'DENIED')
        self.assertIn('MARK_OR_FX_MISSING', denied.reason_codes)
        self.assertIsNone(denied.order)
        json.dumps(denied.to_dict(), allow_nan=False)

    def test_deterministic_replay_prefix_future_mutation_and_json_safe_immutable_outputs(self):
        histories = []
        for future_price in (105, 50):
            risk, bridge, receipt = prepared()
            order = receipt.order
            bridge.process_fill(fill(order), as_of=AT)
            prefix = bridge.history
            histories.append((receipt, prefix))
            digest = identifier(prefix)
            at = AT+pd.Timedelta(seconds=1)
            bridge.process_fill(fill(order, name='exit', phase='EXIT', at=at, price=future_price, fee=0), as_of=at)
            self.assertEqual(bridge.history[:len(prefix)], prefix)
            self.assertEqual(identifier(prefix), digest)
            json.dumps(order.to_dict(), allow_nan=False)
            json.dumps(bridge.history[-1].to_dict(), allow_nan=False)
            with self.assertRaises(FrozenInstanceError):
                order.quantity_contracts = D(1000)
        self.assertEqual(histories[0], histories[1])

    def test_workflow_operates_with_network_sockets_disabled(self):
        with patch('socket.socket', side_effect=AssertionError('Network access forbidden')):
            _, bridge, receipt = prepared()
            bridge.process_fill(fill(receipt.order), as_of=AT)
            bridge.process_fill(fill(receipt.order, name='exit', phase='EXIT', price=101, fee=0), as_of=AT)
            self.assertEqual(bridge.state(receipt.order.order_id), 'CLOSED')


if __name__ == '__main__':
    unittest.main()
