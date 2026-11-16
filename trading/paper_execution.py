"""Offline-only execution acknowledgements over the authoritative M9.1 bridge."""
from dataclasses import dataclass

import pandas as pd

from trading.freqtrade_bridge import (AdmissionReceipt, FreqtradeSimulationBridge,
                                      LifecycleEvent, SimulationCancellation, SimulationFill)
from trading.pattern_context import utc_timestamp
from trading.portfolio import identifier, text


@dataclass(frozen=True)
class PaperExecutionRecord:
    record_id: str
    event_id: str
    kind: str  # ACKNOWLEDGED / DENIED / ENTRY_FILLED / EXIT_FILLED / REJECTED / CANCELLED
    available_at: pd.Timestamp
    source: AdmissionReceipt | SimulationFill | SimulationCancellation
    lifecycle: LifecycleEvent | None


class OfflinePaperExecution:
    """One in-memory simulated execution actor; M8/M9.1 retain all authority."""

    def __init__(self, bridge: FreqtradeSimulationBridge):
        if not isinstance(bridge, FreqtradeSimulationBridge):
            raise ValueError('Authoritative M9.1 simulation bridge required')
        if getattr(bridge, '_m12_paper_execution_owned', False):
            raise ValueError('M9.1 bridge already has an offline paper execution actor')
        bridge._m12_paper_execution_owned = True
        self.bridge = bridge
        self._history = []
        self._events = {}
        self._acknowledged = set()
        self._ack_receipts = set()
        self._last_at = None
        self._failed = False

    @property
    def history(self) -> tuple[PaperExecutionRecord, ...]:
        return tuple(self._history)

    def _prior(self, event_id, payload):
        if self._failed:
            raise RuntimeError('Paper execution actor quarantined; replay into fresh actors')
        text(event_id)
        previous = self._events.get(event_id)
        if previous is not None:
            if previous[0] != identifier(payload):
                raise ValueError('Cannot revise a paper execution event')
            return previous[1]
        return None

    def _time(self, as_of, available_at):
        if self._failed:
            raise RuntimeError('Paper execution actor quarantined; replay into fresh actors')
        at = utc_timestamp(as_of)
        latest = [items[-1].available_at for items in
                  (self.bridge.receipts, self.bridge.history, self.bridge.risk.history,
                   self.bridge.risk.terminal_history, self.bridge.risk.portfolio.history)
                  if items]
        if (available_at > at or self._last_at is not None and at < self._last_at
                or any(at < prior for prior in latest)):
            raise ValueError('Paper execution requires causal chronological receipt')
        self.bridge.risk.portfolio.snapshot(as_of=at)
        return at

    def _record(self, event_id, kind, payload, at, lifecycle=None, *, fingerprint=None):
        record = PaperExecutionRecord(identifier('M12-paper-execution-v1', event_id, kind,
                                                 payload, at, lifecycle),
                                      event_id, kind, at, payload, lifecycle)
        self._history.append(record)
        self._events[event_id] = (identifier(payload if fingerprint is None else fingerprint), record)
        self._last_at = at
        return record

    def acknowledge(self, receipt: AdmissionReceipt, *, event_id: str, as_of) -> PaperExecutionRecord:
        """Acknowledge an already-journaled M9.1 admission, without creating an order."""
        if not isinstance(receipt, AdmissionReceipt):
            raise ValueError('Typed M9.1 admission receipt required')
        at = self._time(as_of, receipt.available_at)
        prior = self._prior(event_id, receipt)
        if prior is not None:
            return prior
        registered = next((r for r in self.bridge.receipts if r.receipt_id == receipt.receipt_id), None)
        if registered != receipt:
            raise ValueError('Admission is absent from authoritative M9.1 history')
        if receipt.receipt_id in self._ack_receipts:
            raise ValueError('Admission already acknowledged under another event ID')
        if receipt.disposition == 'ACCEPTED':
            order = receipt.order
            if (order is None or order not in self.bridge.orders or not order.simulation_only
                    or self.bridge.state(order.order_id) != 'PROPOSED'
                    or at >= order.expires_at or at > order.data_valid_until
                    or order.risk.status not in ('APPROVED', 'REDUCED')):
                raise ValueError('Acknowledgement requires an unfilled M8-approved intent')
            reservation = next((p for p in self.bridge.risk.portfolio.snapshot(as_of=at).reservations
                                if p.allocation_id == order.allocation.allocation_id), None)
            if reservation != order.allocation:
                raise ValueError('Acknowledgement lacks the authoritative M8 reservation')
            kind = 'ACKNOWLEDGED'
        elif receipt.disposition == 'DENIED' and receipt.order is None:
            kind = 'DENIED'
        else:
            raise ValueError('Inconsistent M9.1 admission disposition')
        result = self._record(event_id, kind, receipt, at)
        self._ack_receipts.add(receipt.receipt_id)
        if kind == 'ACKNOWLEDGED':
            self._acknowledged.add(receipt.order.order_id)
        return result

    def fill(self, execution: SimulationFill, *, as_of) -> PaperExecutionRecord:
        """Forward one full simulated fill; M9.1 and M8 perform all state/accounting changes."""
        if not isinstance(execution, SimulationFill):
            raise ValueError('Typed offline simulation fill required')
        at = self._time(as_of, execution.available_at)
        prior = self._prior(execution.fill_id, execution)
        if prior is not None:
            return prior
        order = next((o for o in self.bridge.orders if o.order_id == execution.order_id), None)
        if order is None or order.order_id not in self._acknowledged:
            raise ValueError('Only an acknowledged M8-approved order may be filled')
        if execution.quantity != order.quantity_contracts:
            raise ValueError('Partial or oversized fills are unsupported by M9.1')
        try:
            lifecycle = self.bridge.process_fill(execution, as_of=at)
        except BaseException:
            self._failed = True
            raise
        try:
            return self._record(execution.fill_id, lifecycle.kind, execution, at, lifecycle)
        except BaseException:
            self._failed = True
            raise

    def cancel(self, request: SimulationCancellation, *, as_of,
               rejected: bool = False) -> PaperExecutionRecord:
        """Reject or cancel a proposed intent through M9.1, which releases M8 capital."""
        if not isinstance(request, SimulationCancellation) or not isinstance(rejected, bool):
            raise ValueError('Typed cancellation and explicit rejection flag required')
        payload = (request, rejected)
        at = self._time(as_of, request.available_at)
        prior = self._prior(request.cancellation_id, payload)
        if prior is not None:
            return prior
        if request.order_id not in self._acknowledged:
            raise ValueError('Only an acknowledged M8-approved intent may be cancelled')
        try:
            lifecycle = self.bridge.cancel(request, as_of=at)
        except BaseException:
            self._failed = True
            raise
        try:
            return self._record(request.cancellation_id, 'REJECTED' if rejected else 'CANCELLED',
                                request, at, lifecycle, fingerprint=payload)
        except BaseException:
            self._failed = True
            raise
