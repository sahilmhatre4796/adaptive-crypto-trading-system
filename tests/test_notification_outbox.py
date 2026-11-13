import sys
sys.dont_write_bytecode = True

from dataclasses import FrozenInstanceError, replace
from decimal import Decimal as D
import json
import unittest
from unittest.mock import patch

import pandas as pd

from trading.notification_outbox import OfflineNotificationOutbox
from trading.notifications import TemplateConfig, notification_events
from trading.portfolio import identifier
from test_freqtrade_bridge import prepared, fill
from test_notifications import AT, TEMPLATES, failure, rejected


def event(name='fixture-failure', at=AT):
    return notification_events(failure(failure_id=name, observed_at=at, available_at=at), as_of=at)[0]


class OutboxTests(unittest.TestCase):
    def test_enqueue_inspect_and_duplicate_prevention_without_replacing_original_times(self):
        box = OfflineNotificationOutbox(TEMPLATES)
        source = event()
        enqueued = AT+pd.Timedelta(seconds=10)
        entry = box.enqueue(source, as_of=enqueued)
        self.assertEqual((entry.sequence, entry.state, entry.attempts), (1, 'PENDING', 0))
        self.assertEqual(entry.enqueued_at, enqueued)
        self.assertEqual(entry.notification.source_available_at, AT)
        self.assertEqual(box.entries, box.pending())
        self.assertIs(box.enqueue(source, as_of=enqueued+pd.Timedelta(seconds=1)), entry)
        self.assertEqual(len(box.entries), 1)
        self.assertEqual(len(box.history), 1)
        with self.assertRaises(FrozenInstanceError):
            entry.state = 'DELIVERED_SIMULATED'
        json.dumps(entry.to_dict(), allow_nan=False)

    def test_fifo_preserves_arrival_order_and_separate_older_source_availability(self):
        box = OfflineNotificationOutbox(TEMPLATES)
        first = box.enqueue(event('newer', AT+pd.Timedelta(seconds=5)), as_of=AT+pd.Timedelta(seconds=10))
        second = box.enqueue(event('older', AT), as_of=AT+pd.Timedelta(seconds=11))
        self.assertEqual(box.entries, (first, second))
        self.assertEqual((first.sequence, second.sequence), (1, 2))
        self.assertLess(second.notification.source_available_at, first.notification.source_available_at)
        self.assertEqual(box.history[0].available_at, first.enqueued_at)

    def test_future_bad_type_and_formatting_failures_leave_outbox_unchanged(self):
        box = OfflineNotificationOutbox(TemplateConfig(10))
        for source, at in ((event(at=AT+pd.Timedelta(seconds=1)), AT), (event(), AT), ({}, AT)):
            with self.assertRaises(ValueError):
                box.enqueue(source, as_of=at)
            self.assertEqual((box.entries, box.history), ((), ()))

    def test_source_revisions_and_risk_status_relabeling_cannot_create_an_approved_notice(self):
        box = OfflineNotificationOutbox(TEMPLATES)
        original = notification_events(rejected(), as_of=AT)[0]
        box.enqueue(original, as_of=AT)
        changed = replace(original, source_fingerprint='revised', reason_codes=('REVISED',))
        fake_approval = replace(original, kind='RISK_APPROVED', status='APPROVED', risk_status='APPROVED',
                                quantity=D(1), allocation_id='fake')
        for value in (changed, fake_approval):
            with self.assertRaisesRegex(ValueError, 'revise'):
                box.enqueue(value, as_of=AT)
        self.assertEqual(len(box.entries), 1)
        self.assertEqual(box.entries[0].notification.status, 'REJECTED')

    def test_fill_and_position_views_share_source_without_duplicate_each_view(self):
        _, bridge, receipt = prepared()
        source = bridge.process_fill(fill(receipt.order), as_of=AT)
        views = notification_events(source, order=receipt.order, as_of=AT)
        box = OfflineNotificationOutbox(TEMPLATES)
        for value in views+views:
            box.enqueue(value, as_of=AT)
        self.assertEqual(len(box.entries), 2)
        self.assertEqual(tuple(e.notification.kind for e in box.entries),
                         ('SIMULATED_ENTRY_FILL', 'SIMULATED_POSITION_OPENED'))

    def test_order_alias_and_accepted_receipt_do_not_duplicate_direct_messages(self):
        _, bridge, receipt = prepared()
        box = OfflineNotificationOutbox(TEMPLATES)
        sources = notification_events(receipt, as_of=AT)+notification_events(receipt.risk, as_of=AT)
        sources += notification_events(receipt.order, as_of=AT)+notification_events(bridge.history[0], order=receipt.order, as_of=AT)
        for value in sources:
            box.enqueue(value, as_of=AT)
        self.assertEqual(len(box.entries), 2)

    def test_delivery_retry_success_is_simulated_append_only_and_attempt_counted(self):
        box = OfflineNotificationOutbox(TEMPLATES)
        source = event()
        original = box.enqueue(source, as_of=AT)
        operations = [('START', None), ('FAIL', 'SIMULATED_TRANSPORT_FAILURE'), ('RETRY', None),
                      ('START', None), ('SUCCEED', None)]
        snapshots = [original]
        for n, (action, reason) in enumerate(operations, 1):
            box.simulate_delivery(source.event_id, action, operation_id='op-'+str(n),
                                  as_of=AT+pd.Timedelta(seconds=n), reason_code=reason)
            snapshots.append(box.get(source.event_id))
        self.assertEqual([s.state for s in snapshots], ['PENDING', 'IN_FLIGHT', 'FAILED', 'PENDING', 'IN_FLIGHT', 'DELIVERED_SIMULATED'])
        self.assertEqual([s.attempts for s in snapshots], [0, 1, 1, 1, 2, 2])
        self.assertEqual(original.state, 'PENDING')
        self.assertFalse(box.pending())
        self.assertEqual(len(box.history), 6)
        duplicate = box.enqueue(source, as_of=AT+pd.Timedelta(seconds=6))
        self.assertEqual(duplicate.state, 'DELIVERED_SIMULATED')
        self.assertEqual(duplicate.enqueued_at, AT)

    def test_invalid_delivery_transitions_unknown_ids_and_malformed_actions_are_atomic(self):
        box = OfflineNotificationOutbox(TEMPLATES)
        source = event()
        box.enqueue(source, as_of=AT)
        before = box.history
        for action in ('SUCCEED', 'FAIL', 'RETRY', 'INVALID', []):
            with self.assertRaises(ValueError):
                box.simulate_delivery(source.event_id, action, operation_id='bad', as_of=AT)
            self.assertEqual(box.history, before)
        with self.assertRaises(ValueError):
            box.simulate_delivery('missing', 'START', operation_id='unknown', as_of=AT)
        with self.assertRaises(ValueError):
            box.simulate_delivery(source.event_id, 'CANCEL', operation_id='cancel', as_of=AT)
        self.assertEqual(box.history, before)

    def test_operation_ids_deduplicate_callbacks_and_reject_revisions(self):
        box = OfflineNotificationOutbox(TEMPLATES)
        source = event()
        box.enqueue(source, as_of=AT)
        started = box.simulate_delivery(source.event_id, 'START', operation_id='same', as_of=AT)
        box.simulate_delivery(source.event_id, 'SUCCEED', operation_id='done', as_of=AT)
        before = box.history
        self.assertIs(box.simulate_delivery(source.event_id, 'START', operation_id='same',
                                            as_of=AT+pd.Timedelta(seconds=1)), started)
        self.assertEqual(box.get(source.event_id).state, 'DELIVERED_SIMULATED')
        self.assertEqual(box.history, before)
        with self.assertRaisesRegex(ValueError, 'revise'):
            box.simulate_delivery(source.event_id, 'FAIL', operation_id='same',
                                  as_of=AT+pd.Timedelta(seconds=1), reason_code='PUBLIC_CODE')
        with self.assertRaisesRegex(ValueError, 'chronological'):
            box.enqueue(source, as_of=AT)

    def test_cancel_is_explicit_and_terminal(self):
        box = OfflineNotificationOutbox(TEMPLATES)
        source = event()
        box.enqueue(source, as_of=AT)
        box.simulate_delivery(source.event_id, 'CANCEL', operation_id='cancel', as_of=AT, reason_code='USER_CANCELLED_PREVIEW')
        self.assertEqual(box.get(source.event_id).state, 'CANCELLED')
        with self.assertRaises(ValueError):
            box.simulate_delivery(source.event_id, 'START', operation_id='start', as_of=AT)

    def test_deterministic_replay_prefix_and_future_mutations(self):
        boxes = [OfflineNotificationOutbox(TEMPLATES) for _ in range(2)]
        source = event()
        for box in boxes:
            box.enqueue(source, as_of=AT)
            box.simulate_delivery(source.event_id, 'START', operation_id='start', as_of=AT)
        prefix = boxes[0].history
        self.assertEqual(boxes[1].history, prefix)
        boxes[0].simulate_delivery(source.event_id, 'SUCCEED', operation_id='future', as_of=AT+pd.Timedelta(seconds=1))
        boxes[1].simulate_delivery(source.event_id, 'FAIL', operation_id='future', as_of=AT+pd.Timedelta(seconds=1), reason_code='PUBLIC_FAILURE')
        self.assertEqual(boxes[0].history[:len(prefix)], prefix)
        self.assertEqual(boxes[1].history[:len(prefix)], prefix)


class IsolationTests(unittest.TestCase):
    def image(self, risk, bridge):
        return identifier(risk.history, risk.terminal_history, risk.portfolio.history, bridge.receipts,
                          bridge.history, tuple((o.order_id, bridge.state(o.order_id)) for o in bridge.orders))

    def test_formatting_and_delivery_failures_never_change_upstream_state(self):
        for opened in (False, True):
            risk, bridge, receipt = prepared()
            if opened:
                bridge.process_fill(fill(receipt.order), as_of=AT)
            before = self.image(risk, bridge)
            source = notification_events(receipt.order, as_of=AT)[0]
            with patch.object(risk, 'evaluate', side_effect=AssertionError('notification must not assess risk')), \
                 patch.object(risk, 'simulate_open', side_effect=AssertionError('notification must not book')), \
                 patch.object(risk.portfolio, 'cancel', side_effect=AssertionError('notification must not cancel')), \
                 patch.object(risk.portfolio, 'simulate_close', side_effect=AssertionError('notification must not close')):
                small = OfflineNotificationOutbox(TemplateConfig(1))
                with self.assertRaises(ValueError):
                    small.enqueue(source, as_of=AT)
                box = OfflineNotificationOutbox(TEMPLATES)
                box.enqueue(source, as_of=AT)
                box.simulate_delivery(source.event_id, 'START', operation_id='start', as_of=AT)
                box.simulate_delivery(source.event_id, 'FAIL', operation_id='fail', as_of=AT, reason_code='SIMULATED_FAILURE')
                with self.assertRaises(ValueError):
                    box.simulate_delivery(source.event_id, 'SUCCEED', operation_id='bad', as_of=AT)
                with self.assertRaises(ValueError):
                    notification_events(replace(receipt.risk, status='UNKNOWN'), as_of=AT)
            self.assertEqual(self.image(risk, bridge), before)
            self.assertEqual(risk.config.risk_fraction, D('.10'))

    def test_notifications_and_outbox_operate_with_network_disabled(self):
        risk, bridge, receipt = prepared()
        before = self.image(risk, bridge)
        with patch('socket.socket', side_effect=AssertionError('Network forbidden')):
            box = OfflineNotificationOutbox(TEMPLATES)
            for source in notification_events(receipt, as_of=AT):
                box.enqueue(source, as_of=AT)
                box.simulate_delivery(source.event_id, 'START', operation_id='start-'+source.event_id, as_of=AT)
                box.simulate_delivery(source.event_id, 'SUCCEED', operation_id='done-'+source.event_id, as_of=AT)
            self.assertTrue(all(e.state == 'DELIVERED_SIMULATED' for e in box.entries))
        self.assertEqual(self.image(risk, bridge), before)


if __name__ == '__main__':
    unittest.main()
