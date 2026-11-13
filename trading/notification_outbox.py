"""M9.2 in-memory offline outbox. Delivery states are simulation only."""
from dataclasses import dataclass, replace

import pandas as pd

from trading.notifications import NotificationEvent, RenderedNotification, TemplateConfig, render_notification
from trading.pattern_context import utc_timestamp
from trading.portfolio import identifier, text, wire


@dataclass(frozen=True)
class OutboxEntry:
    notification: NotificationEvent
    message: RenderedNotification
    sequence: int
    enqueued_at: pd.Timestamp
    state: str
    attempts: int
    updated_at: pd.Timestamp

    def to_dict(self):
        return wire(self)


@dataclass(frozen=True)
class DeliveryTransition:
    event_id: str
    operation_id: str
    notification_id: str
    previous_state: str | None
    state: str
    available_at: pd.Timestamp
    attempts: int
    reason_code: str | None


class OfflineNotificationOutbox:
    """No engine/transport/callback references. A local FIFO with append-only audit."""
    def __init__(self, config: TemplateConfig):
        if not isinstance(config, TemplateConfig):
            raise ValueError('Explicit template policy required')
        self.config = config
        self.configuration_id = identifier('M9.2-outbox-v1', config)
        self._entries, self._keys, self._sources, self._operations = {}, {}, {}, {}
        self._history = []
        self._last_at = None

    @property
    def entries(self):
        return tuple(self._entries.values())

    @property
    def history(self):
        return tuple(self._history)

    def pending(self):
        return tuple(e for e in self.entries if e.state == 'PENDING')

    def get(self, notification_id):
        if notification_id not in self._entries:
            raise ValueError('Unknown notification')
        return self._entries[notification_id]

    def _time(self, as_of):
        at = utc_timestamp(as_of)
        if self._last_at is not None and at < self._last_at:
            raise ValueError('Outbox operations must be chronological')
        return at

    def enqueue(self, event: NotificationEvent, *, as_of) -> OutboxEntry:
        at = self._time(as_of)
        if not isinstance(event, NotificationEvent) or event.source_available_at > at:
            raise ValueError('Available typed notification required')
        # Multiple views (fill/position) may share a source; its other facts cannot change.
        facts = {k: v for k, v in vars(event).items() if k not in ('kind', 'event_id', 'dedupe_key')}
        signature = identifier(facts)
        source_key = (event.source_kind, event.source_id)
        if source_key in self._sources and self._sources[source_key] != signature:
            raise ValueError('Cannot revise notification source facts or relabel its status')
        if event.dedupe_key in self._keys:
            prior = self.get(self._keys[event.dedupe_key])
            if prior.notification != event:
                raise ValueError('Cannot revise a notification')
            self._last_at = at
            return prior
        message = render_notification(event, self.config)  # Validate before changing any outbox state.
        entry = OutboxEntry(event, message, len(self._entries)+1, at, 'PENDING', 0, at)
        transition = DeliveryTransition(identifier('M9.2-enqueue', self.configuration_id, event.event_id, at),
                                        'enqueue:'+event.event_id, event.event_id, None, 'PENDING', at, 0, None)
        self._entries[event.event_id] = entry
        self._keys[event.dedupe_key] = event.event_id
        self._sources[source_key] = signature
        self._history.append(transition)
        self._last_at = at
        return entry

    def simulate_delivery(self, notification_id, action, *, operation_id, as_of, reason_code=None) -> DeliveryTransition:
        at = self._time(as_of)
        text(operation_id)
        if not isinstance(action, str):
            raise ValueError('Explicit delivery action required')
        if reason_code is not None:
            text(reason_code)
        entry = self.get(notification_id)
        fingerprint = identifier(notification_id, action, reason_code)
        if operation_id in self._operations:
            prior_fingerprint, prior = self._operations[operation_id]
            if prior_fingerprint != fingerprint:
                raise ValueError('Cannot revise or reuse a delivery operation ID')
            self._last_at = at
            return prior
        target = {('PENDING', 'START'): 'IN_FLIGHT', ('IN_FLIGHT', 'SUCCEED'): 'DELIVERED_SIMULATED',
                  ('IN_FLIGHT', 'FAIL'): 'FAILED', ('FAILED', 'RETRY'): 'PENDING',
                  ('PENDING', 'CANCEL'): 'CANCELLED', ('FAILED', 'CANCEL'): 'CANCELLED'}.get((entry.state, action))
        if target is None:
            raise ValueError('Invalid offline delivery-state transition')
        if action in ('FAIL', 'CANCEL') and reason_code is None:
            raise ValueError('Explicit public failure/cancellation reason required')
        if action not in ('FAIL', 'CANCEL') and reason_code is not None:
            raise ValueError('Unexpected reason on delivery transition')
        attempts = entry.attempts+(1 if action == 'START' else 0)
        transition = DeliveryTransition(identifier('M9.2-delivery', self.configuration_id, operation_id,
                                                   fingerprint, at), operation_id, notification_id,
                                         entry.state, target, at, attempts, reason_code)
        updated = replace(entry, state=target, attempts=attempts, updated_at=at)
        self._entries[notification_id] = updated
        self._history.append(transition)
        self._operations[operation_id] = (fingerprint, transition)
        self._last_at = at
        return transition
