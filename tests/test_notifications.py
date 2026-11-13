import sys
sys.dont_write_bytecode = True

from dataclasses import FrozenInstanceError, replace
from decimal import Decimal as D
from html.parser import HTMLParser
import json
import unittest

import pandas as pd

from trading.notifications import (ProcessingFailure, TemplateConfig, notification_events,
                                   render_notification)
from trading.signals import SignalLifecycleEvent, SignalTracker
from trading.signal_context import assemble_context
from test_freqtrade_bridge import prepared, fill, cancellation
from test_risk import AT, config, decide, engine, proposal, spec
from test_signal_context import CONFIG, inputs
from test_signals import policy

TEMPLATES = TemplateConfig(12000)  # Explicit local test bound, not an asserted Telegram API limit.


def qualified():
    context = assemble_context(**inputs())
    return SignalTracker(policy(), CONFIG, mode='BACKTEST').observe(context)


def rejected():
    return decide(engine(), instrument=spec(min_quantity=1000))


def failure(kind='PROCESSING_FAILURE', **changes):
    fields = dict(failure_id='fixture-failure', kind=kind, stage='M9.1', reason_codes=('PUBLIC_FAILURE_CODE',),
                  observed_at=AT, available_at=AT, symbol='BTC/USDT', side='LONG', source_event_id='source-operation')
    fields.update(changes)
    return ProcessingFailure(**fields)


def risk_events():
    approved = decide(engine())
    reduced = decide(engine(), requested_quantity='1.05')
    risk = engine(config(max_open_positions=1))
    decide(risk)
    blocked = decide(risk, proposal('two', 'ETH/USDT'))
    return [notification_events(d, as_of=AT)[0] for d in (approved, reduced, rejected(), blocked)]


class NotificationMappingTests(unittest.TestCase):
    def test_qualified_signal_preserves_original_roles_times_and_analytical_scope(self):
        source = qualified()
        event, = notification_events(source, as_of=source.available_at)
        self.assertEqual((event.kind, event.status, event.timeframe, event.confirmation_timeframe),
                         ('SIGNAL_QUALIFIED', 'QUALIFIED', '15m', '5m'))
        self.assertEqual(event.signal_available_at, source.available_at)
        self.assertEqual(event.upstream_available_at, source.evaluation.context.pattern.confirmed_at)
        self.assertIsNone(event.risk_budget)
        self.assertIsNone(event.quantity)
        self.assertEqual(event.source_configuration_id, source.policy_id)
        self.assertEqual(event, notification_events(source, as_of=source.available_at+pd.Timedelta(minutes=2))[0])
        message = render_notification(event, TEMPLATES).body_html
        self.assertIn('Recorded analytical qualification', message)
        self.assertIn(source.available_at.isoformat(), message)
        with self.assertRaises(ValueError):
            notification_events(replace(source, qualification_passed=False), as_of=source.available_at)

    def test_all_four_risk_statuses_keep_exact_codes_sizes_budget_and_currency(self):
        for event, status in zip(risk_events(), ('APPROVED', 'REDUCED', 'REJECTED', 'BLOCKED')):
            with self.subTest(status=status):
                self.assertEqual((event.kind, event.status, event.risk_status), ('RISK_'+status, status, status))
                self.assertEqual(event.risk_budget, D(200))
                self.assertEqual(event.currency, 'INR')
                self.assertEqual(event.price_currency, 'USDT' if event.price is not None else None)
                self.assertIsNone(event.timeframe)  # M8 does not carry it: never guess 15m.
                self.assertIn('Status: <code>'+status+'</code>', render_notification(event, TEMPLATES).body_html)
        source = rejected()
        event = notification_events(source, as_of=AT)[0]
        self.assertEqual(event.reason_codes, source.reason_codes)
        self.assertEqual((event.quantity, event.risk_budget, event.planned_loss),
                         (source.quantity, source.risk_budget, source.planned_loss))

    def test_order_intent_and_proposed_lifecycle_alias_have_one_canonical_identity(self):
        _, bridge, receipt = prepared()
        order = notification_events(receipt.order, as_of=AT)[0]
        alias = notification_events(bridge.history[0], order=receipt.order, as_of=AT)[0]
        self.assertEqual(order, alias)
        self.assertEqual(order.kind, 'ORDER_INTENT_CREATED')
        self.assertEqual(order.order_id, receipt.order.order_id)
        self.assertEqual(order.signal_available_at, receipt.risk.proposal.available_at)
        self.assertIn('SIMULATED ORDER INTENT CREATED', render_notification(order, TEMPLATES).body_html)

    def test_accepted_admission_maps_to_risk_and_intent_not_a_new_business_event(self):
        _, _, receipt = prepared()
        events = notification_events(receipt, as_of=AT)
        self.assertEqual(events, notification_events(receipt.risk, as_of=AT)+notification_events(receipt.order, as_of=AT))

    def test_denied_admission_never_maps_to_approved_even_when_prior_risk_was_positive(self):
        risk = engine()
        from trading.freqtrade_bridge import FreqtradeSimulationBridge
        from test_freqtrade_bridge import BINDINGS
        bridge = FreqtradeSimulationBridge(risk, BINDINGS)
        decision = decide(risk)
        at = AT+pd.Timedelta(seconds=301)
        receipt = bridge.propose(decision, as_of=at)
        event, = notification_events(receipt, as_of=at)
        self.assertEqual((event.kind, event.status, event.risk_status), ('INTEGRATION_DENIED', 'DENIED', 'APPROVED'))
        self.assertEqual(event.reason_codes, receipt.reason_codes)
        self.assertIn('SIMULATED INTEGRATION DENIED', render_notification(event, TEMPLATES).body_html)
        self.assertNotIn('OFFLINE | RISK APPROVED', render_notification(event, TEMPLATES).body_html)
        denied = replace(receipt, risk=rejected(), reason_codes=rejected().reason_codes)
        self.assertEqual(notification_events(denied, as_of=at)[0].risk_status, 'REJECTED')

    def test_fill_and_position_open_close_types_are_explicit_and_simulated(self):
        _, bridge, receipt = prepared()
        for request, kinds in ((fill(receipt.order), ('SIMULATED_ENTRY_FILL', 'SIMULATED_POSITION_OPENED')),
                               (fill(receipt.order, phase='EXIT', name='exit', price=105, fee=0),
                                ('SIMULATED_EXIT_FILL', 'SIMULATED_POSITION_CLOSED'))):
            source = bridge.process_fill(request, as_of=AT)
            events = notification_events(source, order=receipt.order, as_of=AT)
            self.assertEqual(tuple(e.kind for e in events), kinds)
            for event in events:
                self.assertEqual(event.price, request.price)
                self.assertEqual(event.source_available_at, source.available_at)
                self.assertEqual(event.upstream_available_at, request.available_at)
                self.assertIn('SIMULATED', render_notification(event, TEMPLATES).body_html)

    def test_cancellations_expiry_and_upstream_pending_cancellation_types(self):
        for scenario, expected in (('cancel', 'SIMULATED_ORDER_CANCELLED'), ('expire', 'SIMULATED_ORDER_EXPIRED'),
                                    ('m7-invalid', 'SIMULATED_ORDER_CANCELLED'), ('m7-expire', 'SIMULATED_ORDER_EXPIRED')):
            _, bridge, receipt = prepared()
            at = AT
            if scenario == 'cancel':
                source = bridge.cancel(cancellation(receipt.order), as_of=at)
            elif scenario == 'expire':
                at = receipt.order.expires_at
                source = bridge.expire(receipt.order.order_id, as_of=at)
            else:
                kind = 'INVALIDATED' if scenario == 'm7-invalid' else 'EXPIRED'
                event = SignalLifecycleEvent('terminal', receipt.risk.signal_id, kind, at, None, None, 'EXACT_PUBLIC_REASON')
                source = bridge.on_signal_terminal(event, as_of=at)[0]
            event, = notification_events(source, order=receipt.order, as_of=at)
            self.assertEqual(event.kind, expected)
            self.assertEqual(event.reason_codes, source.reason_codes)

    def test_open_terminal_notice_does_not_claim_a_position_was_closed(self):
        _, bridge, receipt = prepared()
        bridge.process_fill(fill(receipt.order), as_of=AT)
        terminal = SignalLifecycleEvent('terminal', receipt.risk.signal_id, 'INVALIDATED', AT, AT, 'bar', 'EXTREME_TOUCHED')
        source = bridge.on_signal_terminal(terminal, as_of=AT)[0]
        event, = notification_events(source, order=receipt.order, as_of=AT)
        self.assertEqual((event.kind, event.status), ('SIMULATED_SIGNAL_TERMINAL_NOTICE', 'OPEN'))

    def test_processing_failures_and_invalid_lifecycle_use_structured_public_codes(self):
        for kind in ('PROCESSING_FAILURE', 'INVALID_LIFECYCLE'):
            event, = notification_events(failure(kind), as_of=AT)
            self.assertEqual((event.kind, event.status, event.failure_stage), (kind, 'ERROR', 'M9.1'))
            self.assertEqual(event.related_event_id, 'source-operation')
            self.assertIsNone(event.risk_budget)
            self.assertIn('PUBLIC_FAILURE_CODE', render_notification(event, TEMPLATES).body_html)
        with self.assertRaises(ValueError):
            notification_events(ValueError('raw exception/traceback is not accepted'), as_of=AT)

    def test_late_receipt_keeps_observed_producer_and_original_signal_times(self):
        _, bridge, receipt = prepared()
        observed, received, processed = (AT+pd.Timedelta(seconds=n) for n in (1, 3, 4))
        request = replace(fill(receipt.order, at=observed), available_at=received)
        source = bridge.process_fill(request, as_of=processed)
        event = notification_events(source, order=receipt.order, as_of=processed)[0]
        self.assertEqual((event.observed_at, event.upstream_available_at, event.source_available_at), (observed, received, processed))
        self.assertEqual(event.signal_available_at, receipt.risk.proposal.available_at)


class NotificationValidationTests(unittest.TestCase):
    def test_future_or_missing_source_data_is_not_invented(self):
        with self.assertRaises(ValueError):
            notification_events(decide(engine()), as_of=AT-pd.Timedelta(seconds=1))
        source = failure(symbol=None, side=None, timeframe=None)
        event, = notification_events(source, as_of=AT)
        self.assertIsNone(event.symbol)
        self.assertIsNone(event.quantity)
        self.assertNotIn('Quantity', render_notification(event, TEMPLATES).body_html)
        with self.assertRaises(ValueError):
            notification_events({'status': 'APPROVED'}, as_of=AT)

    def test_kind_status_and_allocation_contradictions_are_rejected(self):
        original = notification_events(rejected(), as_of=AT)[0]
        for changes in (dict(status='APPROVED'), dict(kind='RISK_APPROVED', status='APPROVED'),
                        dict(kind=[]), dict(quantity=D(-1)), dict(risk_budget=None), dict(schema_version='M9.2-v2')):
            with self.assertRaises(ValueError):
                replace(original, **changes)
        with self.assertRaises(ValueError):
            notification_events(replace(rejected(), status='APPROVED'), as_of=AT)

    def test_malformed_lifecycle_or_missing_order_context_fails(self):
        _, bridge, receipt = prepared()
        source = bridge.process_fill(fill(receipt.order), as_of=AT)
        with self.assertRaises(ValueError):
            notification_events(source, as_of=AT)
        for changes in (dict(state='CLOSED'), dict(order_id='wrong'), dict(fill=None),
                        dict(source_available_at=AT+pd.Timedelta(seconds=1)), dict(portfolio_event_id=None)):
            with self.assertRaises(ValueError):
                notification_events(replace(source, **changes), order=receipt.order, as_of=AT)
        _, other_bridge, other = prepared()
        cancel = other_bridge.cancel(cancellation(other.order), as_of=AT)
        with self.assertRaises(ValueError):
            notification_events(replace(cancel, cancellation=None), order=other.order, as_of=AT)
        for changed_order in (replace(other.order, simulation_only=False),
                              replace(other.order, allocation=replace(other.order.allocation, quantity=D(1)))):
            with self.assertRaises(ValueError):
                notification_events(changed_order, as_of=AT)

    def test_text_escaping_preserves_exact_codes_without_html_or_control_spoofing(self):
        raw = '<img src="x">&\nStatus: APPROVED\u202e\ud800'
        event = notification_events(replace(rejected(), reason_codes=(raw,)), as_of=AT)[0]
        self.assertEqual(event.reason_codes, (raw,))
        body = render_notification(event, TEMPLATES).body_html
        self.assertIn('&lt;img src=&quot;x&quot;&gt;&amp;', body)
        self.assertIn('\\u000aStatus: APPROVED\\u202e', body)
        self.assertIn('\\ud800', body)
        body.encode('utf-8')
        self.assertNotIn('<img', body)
        self.assertNotIn('\u202e', body)
        self.assertIn('OFFLINE | RISK REJECTED', body)
        self.assertNotIn('OFFLINE | RISK APPROVED', body)
        class Tags(HTMLParser):
            def __init__(self):
                super().__init__()
                self.tags = []
            def handle_starttag(self, tag, attrs):
                self.tags.append(tag)
                self.assert_no_attrs = not attrs
        parser = Tags()
        parser.feed(body)
        self.assertTrue(set(parser.tags) <= {'b', 'code'})

    def test_remaining_bidi_controls_escape_only_in_rendered_output(self):
        for character in ('\u061c', '\u200e', '\u200f'):
            with self.subTest(codepoint=f'U+{ord(character):04X}'):
                raw = 'BEFORE'+character+'AFTER'
                source = replace(rejected(), reason_codes=(raw,))
                event = notification_events(source, as_of=AT)[0]
                source_before, event_before = source.to_dict(), event.to_dict()
                body = render_notification(event, TEMPLATES).body_html
                self.assertIn(f'BEFORE\\u{ord(character):04x}AFTER', body)
                self.assertNotIn(character, body)
                self.assertEqual(source.reason_codes, (raw,))
                self.assertEqual(event.reason_codes, (raw,))
                self.assertEqual(source.to_dict(), source_before)
                self.assertEqual(event.to_dict(), event_before)

    def test_oversize_messages_fail_without_silent_truncation(self):
        event = notification_events(failure(), as_of=AT)[0]
        normal = render_notification(event, TEMPLATES)
        boundary = render_notification(event, TemplateConfig(len(normal.body_html)))
        self.assertEqual(boundary.body_html, normal.body_html)
        with self.assertRaises(ValueError):
            render_notification(event, TemplateConfig(len(normal.body_html)-1))
        for bound in (0, True, '4096'):
            with self.assertRaises(ValueError):
                TemplateConfig(bound)

    def test_ids_json_and_immutable_outputs_are_deterministic(self):
        source = decide(engine())
        a = notification_events(source, as_of=AT)[0]
        b = notification_events(source, as_of=AT+pd.Timedelta(seconds=1))[0]
        self.assertEqual(a, b)
        self.assertEqual(render_notification(a, TEMPLATES), render_notification(b, TEMPLATES))
        json.dumps(a.to_dict(), allow_nan=False)
        with self.assertRaises(FrozenInstanceError):
            a.status = 'REJECTED'


if __name__ == '__main__':
    unittest.main()
