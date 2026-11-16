"""Replay M12 execution commands into fresh offline M8/M9.1 actors only."""
from hashlib import sha256
from pathlib import Path

from trading.exit_funding import ExitFundingController
from trading.freqtrade_bridge import (FreqtradeSimulationBridge, SimulationCancellation,
                                      SimulationFill)
from trading.paper_execution import OfflinePaperExecution
from trading.paper_journal import PaperJournal, PaperJournalError
from trading.paper_state import reconcile_paper_state
from trading.pattern_context import utc_timestamp
from trading.portfolio import identifier, text
from trading.replay_codec import decode, encode


def _code_hash():
    directory = Path(__file__).parent
    files = ('paper_recovery.py', 'paper_journal.py', 'paper_execution.py',
             'paper_state.py', 'exit_funding.py', 'freqtrade_bridge.py',
             'portfolio.py', 'risk.py', 'replay_codec.py')
    value = sha256()
    for name in files:
        value.update(name.encode())
        value.update((directory / name).read_bytes())
    return value.hexdigest()


def _baseline(bridge):
    return identifier('M12-paper-baseline-v1', bridge.configuration_id,
                    bridge.risk.configuration_id, bridge.risk.portfolio.initial_cash,
                    bridge.risk.portfolio.currency, bridge.risk.history,
                    bridge.risk.terminal_history, bridge.risk.portfolio.history,
                    bridge.receipts, bridge.history, bridge.orders)


class DurablePaperExecution:
    """Fenced journal owner. A factory must reconstruct an identical fresh baseline."""

    def __init__(self, path, factory, *, create):
        if not callable(factory):
            raise ValueError('Fresh authoritative actor factory required')
        bridge = factory()
        if not isinstance(bridge, FreqtradeSimulationBridge):
            raise ValueError('Factory must return a fresh M9.1 bridge')
        self.bridge = bridge
        self.adapter = OfflinePaperExecution(bridge)
        self.journal = PaperJournal(path, baseline_hash=_baseline(bridge),
                                    code_hash=_code_hash(), create=create)
        self._dead = False
        self._expected = self._state_hash()
        if not create:
            try:
                self._replay()
            except BaseException:
                self.journal.close()
                raise

    @classmethod
    def create(cls, path, factory):
        return cls(path, factory, create=True)

    @classmethod
    def recover(cls, path, factory):
        return cls(path, factory, create=False)

    def close(self):
        self.journal.close()

    def _state_hash(self):
        risk = self.bridge.risk
        return identifier('M12-paper-actor-v1', self.adapter.history,
                          tuple(sorted(self.adapter._acknowledged)),
                          tuple(sorted(self.adapter._ack_receipts)),
                          self.adapter._last_at, self.adapter._failed,
                        self.bridge.receipts, self.bridge.history, self.bridge.orders,
                        risk.history, risk.terminal_history, risk.portfolio.history)

    def _invoke(self, operation, payload):
        if type(payload) is not dict or type(payload.get('as_of')) is not type(utc_timestamp('2020-01-01T00:00:00Z')):
            raise PaperJournalError('Malformed paper command payload')
        at = payload['as_of']
        if operation == 'ACK' and set(payload) == {'event_id', 'receipt_id', 'as_of'}:
            receipt = next((r for r in self.bridge.receipts
                            if r.receipt_id == payload['receipt_id']), None)
            if receipt is None:
                raise PaperJournalError('Admission receipt absent from recovered baseline')
            return self.adapter.acknowledge(receipt, event_id=payload['event_id'], as_of=at)
        if operation == 'FILL' and set(payload) == {'as_of', 'fill'}:
            execution = payload['fill']
            if not isinstance(execution, SimulationFill):
                raise PaperJournalError('Typed simulated fill required')
            return self.adapter.fill(execution, as_of=at)
        if operation == 'CANCEL' and set(payload) == {'as_of', 'request', 'rejected'}:
            request = payload['request']
            if not isinstance(request, SimulationCancellation) or type(payload['rejected']) is not bool:
                raise PaperJournalError('Typed simulated cancellation required')
            return self.adapter.cancel(request, as_of=at, rejected=payload['rejected'])
        raise PaperJournalError('Unknown or malformed paper command')

    def _replay(self):
        try:
            commands, outcomes = self.journal.verify()
            for seq, event_id, operation, encoded, _, _ in commands:
                saved = outcomes[seq - 1] if seq <= len(outcomes) else None
                if saved is not None and saved[1] == 'QUARANTINED':
                    raise PaperJournalError('Quarantined callback requires explicit resolution')
                payload = decode(encoded)
                record = self._invoke(operation, payload)
                if record.event_id != event_id:
                    raise PaperJournalError('Recovered event identity changed')
                reconcile_paper_state(self.bridge, ExitFundingController(self.bridge),
                                      as_of=payload['as_of'])
                post = self._state_hash()
                if saved is None:
                    self.journal.finish(seq, 'APPLIED', record.record_id, post)
                elif saved[2] != record.record_id or saved[3] != post:
                    raise PaperJournalError('Committed paper actor replay diverged')
                self._expected = post
        except BaseException:
            self._dead = True
            raise

    def _submit(self, event_id, operation, payload):
        text(event_id)
        if self._dead:
            raise PaperJournalError('Paper actor graph quarantined; recover fresh actors')
        encoded = encode(payload)
        try:
            self.journal.verify()
            self.journal.assert_owner()
            if self._state_hash() != self._expected:
                raise PaperJournalError('Paper actor graph changed outside its journal')
            old = self.journal.command(event_id)
            if old is not None:
                seq, prior_op, prior_payload = old
                if prior_op != operation or prior_payload != encoded:
                    raise PaperJournalError('Cannot revise a durable paper command')
                if self.journal.outcome(seq) is None:
                    raise PaperJournalError('Uncertain paper command requires fresh recovery')
                return next(r for r in self.adapter.history if r.event_id == event_id)
            seq = self.journal.append(event_id, operation, encoded)
        except BaseException:
            self._dead = True
            raise
        try:
            record = self._invoke(operation, payload)
            reconcile_paper_state(self.bridge, ExitFundingController(self.bridge),
                                  as_of=payload['as_of'])
        except Exception as exc:
            self._dead = True
            self.journal.finish(seq, 'QUARANTINED', type(exc).__name__, '')
            raise
        except BaseException:
            self._dead = True
            raise
        try:
            post = self._state_hash()
            self.journal.finish(seq, 'APPLIED', record.record_id, post)
            self._expected = post
            return record
        except BaseException:
            self._dead = True
            raise

    def acknowledge(self, receipt_id, *, event_id, as_of):
        text(receipt_id)
        return self._submit(event_id, 'ACK', {'event_id': event_id, 'receipt_id': receipt_id,
                                               'as_of': utc_timestamp(as_of)})

    def fill(self, execution: SimulationFill, *, as_of):
        if not isinstance(execution, SimulationFill):
            raise ValueError('Typed simulated fill required')
        return self._submit(execution.fill_id, 'FILL',
                            {'fill': execution, 'as_of': utc_timestamp(as_of)})

    def cancel(self, request: SimulationCancellation, *, rejected=False, as_of):
        if not isinstance(request, SimulationCancellation) or type(rejected) is not bool:
            raise ValueError('Typed simulated cancellation and rejection flag required')
        return self._submit(request.cancellation_id, 'CANCEL',
                            {'request': request, 'rejected': rejected,
                             'as_of': utc_timestamp(as_of)})

    def reconcile(self, *, as_of):
        self.journal.verify()
        self.journal.assert_owner()
        if self._dead or self._state_hash() != self._expected:
            raise PaperJournalError('Cannot reconcile an untrusted paper actor graph')
        return reconcile_paper_state(self.bridge, ExitFundingController(self.bridge), as_of=as_of)
