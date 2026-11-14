"""M9.3 offline, journaled M7 → M8 → M9.1 → M9.2 orchestration."""
from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal
from hashlib import sha256
from pathlib import Path

import pandas as pd

from trading.freqtrade_bridge import (FreqtradeSimulationBridge, LifecycleEvent, PairBinding,
                                      SimulationCancellation, SimulationFill)
from trading.m6 import M6Bundle
from trading.notification_outbox import OfflineNotificationOutbox
from trading.notifications import TemplateConfig, notification_events
from trading.pattern_context import utc_timestamp
from trading.pattern_engine import ClosedCandle, PatternEvent
from trading.portfolio import InstrumentSpec, Portfolio, Quote, number
from trading.replay_codec import decode, digest, encode, identity
from trading.replay_journal import JournalError, ReplayJournal
from trading.risk import CostModel, RiskConfig, RiskEngine, proposal_from_m7
from trading.signal_context import SignalContextConfig, assemble_context
from trading.signals import SignalConfig, SignalTracker
from trading.structure import StructureResult


ASSETS = ('BTC/USDT', 'ETH/USDT')
ROOT_KINDS = frozenset(('MARK', 'FX', 'FUNDING', 'EXIT_FILL', 'CANCEL', 'EXPIRE',
                        'MONITOR', 'CONTEXT', 'RISK', 'ENTRY_FILL'))
SOURCE_FILES = ('trading/data.py', 'trading/patterns.py', 'trading/pattern_context.py',
                'trading/pattern_engine.py',
                'trading/structure.py', 'trading/m6_data.py', 'trading/regime.py',
                'trading/derivatives.py', 'trading/liquidation.py', 'trading/m6.py',
                'trading/signal_context.py', 'trading/signals.py', 'trading/portfolio.py',
                'trading/risk.py', 'trading/freqtrade_bridge.py', 'trading/notifications.py',
                'trading/notification_outbox.py', 'trading/replay_codec.py',
                'trading/replay_journal.py', 'trading/orchestration.py')


def _code_hash():
    root = Path(__file__).resolve().parent.parent
    h = sha256()
    for relative in SOURCE_FILES:
        h.update(relative.encode())
        h.update(sha256((root/relative).read_bytes()).digest())
    return h.hexdigest()


@dataclass(frozen=True)
class RunSpec:
    initial_at: pd.Timestamp
    initial_cash: Decimal
    currency: str
    signal_config: SignalConfig
    context_config: SignalContextConfig
    risk_config: RiskConfig
    bindings: tuple[PairBinding, ...]
    template_config: TemplateConfig
    asset_priority: tuple[str, str]
    mode: str = 'BACKTEST'
    schema_version: str = 'M9.3-v1'

    def __post_init__(self):
        object.__setattr__(self, 'initial_at', utc_timestamp(self.initial_at))
        object.__setattr__(self, 'initial_cash', number(self.initial_cash, positive=True))
        if (self.schema_version != 'M9.3-v1' or self.mode != 'BACKTEST'
                or not isinstance(self.currency, str) or not self.currency.strip()
                or not isinstance(self.signal_config, SignalConfig)
                or not isinstance(self.context_config, SignalContextConfig)
                or not isinstance(self.risk_config, RiskConfig)
                or not isinstance(self.template_config, TemplateConfig)
                or type(self.bindings) is not tuple
                or any(not isinstance(b, PairBinding) for b in self.bindings)
                or {b.symbol for b in self.bindings} != set(ASSETS)
                or len(self.bindings) != 2
                or type(self.asset_priority) is not tuple
                or set(self.asset_priority) != set(ASSETS) or len(self.asset_priority) != 2):
            raise ValueError('Explicit supported M9.3-v1 run policy and BTC/ETH priority required')

    @property
    def run_id(self):
        return identity('M9.3-run-v1', self)


@dataclass(frozen=True)
class ReplayEnvelope:
    command_id: str
    kind: str
    source_id: str
    observed_at: pd.Timestamp
    available_at: pd.Timestamp
    as_of: pd.Timestamp
    ordinal: int
    payload: object
    causation_id: str | None = None

    def __post_init__(self):
        for name in ('observed_at', 'available_at', 'as_of'):
            object.__setattr__(self, name, utc_timestamp(getattr(self, name)))
        if (not self.command_id or not self.source_id or self.kind not in ROOT_KINDS
                or type(self.ordinal) is not int or self.ordinal < 0
                or (self.causation_id is not None and
                    (not isinstance(self.causation_id, str) or not self.causation_id))
                or not self.observed_at <= self.available_at <= self.as_of):
            raise ValueError('Invalid or unavailable M9.3 source command')
        encode(self.payload)

    @property
    def payload_id(self):
        return digest(encode(self.payload))


@dataclass(frozen=True)
class ReplayFrame:
    frame_id: str
    as_of: pd.Timestamp
    commands: tuple[ReplayEnvelope, ...]
    command_count: int = field(init=False)
    frame_hash: str = field(init=False)

    def __post_init__(self):
        object.__setattr__(self, 'as_of', utc_timestamp(self.as_of))
        if (not self.frame_id or type(self.commands) is not tuple or not self.commands
                or any(not isinstance(c, ReplayEnvelope) or c.as_of != self.as_of for c in self.commands)
                or len({c.command_id for c in self.commands}) != len(self.commands)
                or len({c.ordinal for c in self.commands}) != len(self.commands)):
            raise ValueError('Complete, unique and aligned replay frame required')
        object.__setattr__(self, 'command_count', len(self.commands))
        object.__setattr__(self, 'frame_hash', identity('M9.3-frame-v1', self.frame_id,
                                                      self.as_of, self.commands))


@dataclass(frozen=True)
class FrameReceipt:
    frame_id: str
    as_of: pd.Timestamp
    sequence: int
    command_count: int
    frame_hash: str
    state: str


@dataclass(frozen=True)
class StageOutcome:
    command_id: str
    kind: str
    as_of: pd.Timestamp
    status: str
    result: object
    reason_codes: tuple[str, ...]
    effect_ids: tuple[str, ...]
    job_ids: tuple[str, ...]


@dataclass(frozen=True)
class VerificationReport:
    run_id: str
    applied_commands: int
    sealed_frames: int
    state_digest: str
    journal_chain: str


class _Actors:
    def __init__(self, spec):
        self.portfolio = Portfolio(as_of=spec.initial_at, cash=spec.initial_cash,
                                   currency=spec.currency)
        self.risk = RiskEngine(self.portfolio, spec.risk_config, mode='BACKTEST')
        self.bridge = FreqtradeSimulationBridge(self.risk, spec.bindings)
        self.signals = SignalTracker(spec.signal_config, spec.context_config, mode='BACKTEST')
        self.outbox = OfflineNotificationOutbox(spec.template_config)

    def images(self):
        return (self.signals.history, self.signals.events, self.portfolio.history,
                self.risk.history, self.risk.terminal_history, self.bridge.receipts,
                self.bridge.history, self.outbox.history)

    def digest(self):
        return identity(self.images(), self.outbox.entries)


def _effects(before, after):
    names = ('m7_records', 'm7_events', 'portfolio', 'risk_decisions', 'risk_terminals',
             'admissions', 'bridge_events', 'outbox_transitions')
    if any(len(new) < len(old) or new[:len(old)] != old for old, new in zip(before, after)):
        raise JournalError('Actor rewrote public history')
    return {name: new[len(old):] for name, old, new in zip(names, before, after)}


def _effect_ids(effects):
    return tuple(getattr(value, 'event_id', getattr(value, 'decision_id',
                 getattr(value, 'receipt_id', ''))) for values in effects.values() for value in values)


class OfflineOrchestrator:
    def __init__(self, spec, journal):
        self.spec, self.journal = spec, journal
        self._actors = _Actors(spec)
        self.path = journal.path

    @classmethod
    def create(cls, spec: RunSpec, path):
        if not isinstance(spec, RunSpec):
            raise ValueError('Typed M9.3 run specification required')
        return cls(spec, ReplayJournal(path, manifest=spec, code_hash=_code_hash(), create=True))

    @classmethod
    def recover(cls, path, expected_spec: RunSpec):
        if not isinstance(expected_spec, RunSpec):
            raise ValueError('Expected M9.3 policy required for recovery')
        journal = ReplayJournal(path, manifest=expected_spec, code_hash=_code_hash())
        run = cls(expected_spec, journal)
        try:
            run._verify_frames()
            for row in journal.applied():
                recorded = decode(row['outcome'])
                if not isinstance(recorded, StageOutcome):
                    raise JournalError('Invalid recorded stage outcome')
                if recorded.status == 'INPUT_REJECTED':
                    if decode(row['effects']) != {} or run._actors.digest() != row['state_digest']:
                        raise JournalError('Input rejection changed actor state')
                    if (journal.db.execute('SELECT 1 FROM commands WHERE parent_id=?',
                                           (row['command_id'],)).fetchone() is not None
                            or journal.db.execute('SELECT 1 FROM jobs WHERE source_command=?',
                                                  (row['command_id'],)).fetchone() is not None):
                        raise JournalError('Input rejection grew derived work')
                    continue
                result, effects = run._invoke(row)
                if (encode(result) != encode(recorded.result) or encode(effects) != row['effects']
                        or run._actors.digest() != row['state_digest']):
                    raise JournalError('Committed actor replay diverged')
                run._verify_derived(row, effects)
            return run
        except BaseException:
            run._quarantine()
            raise

    def _require_active(self):
        if self._actors is None:
            raise JournalError('Actor graph quarantined; recover from the journal')

    def _quarantine(self):
        self._actors = None
        self.journal.close()

    def _restore(self):
        self._quarantine()
        fresh = type(self).recover(self.path, self.spec)
        self.__dict__.update(fresh.__dict__)

    def _ordered(self, frame):
        def key(c):
            phase = (0 if c.kind in ('MARK', 'FX', 'FUNDING') else
                     1 if c.kind in ('EXIT_FILL', 'CANCEL', 'EXPIRE', 'MONITOR') else
                     2 if c.kind == 'CONTEXT' else 3 if c.kind == 'RISK' else 4)
            sub = 1 if c.kind == 'FUNDING' else 0
            asset = self.spec.asset_priority.index(c.payload[0].symbol if c.kind == 'CONTEXT'
                                                   else c.payload[1].symbol if c.kind == 'RISK'
                                                   and type(c.payload) is tuple and len(c.payload) > 1
                                                   and isinstance(c.payload[1], InstrumentSpec)
                                                   else c.payload.key if c.kind == 'MARK'
                                                   and isinstance(c.payload, Quote) else ASSETS[0])
            return phase, sub, asset if phase == 3 else 0, c.ordinal
        return [(c, f'{i:06d}') for i, c in enumerate(sorted(frame.commands, key=key), 1)]

    def _verify_frames(self):
        for frame_id, body in self.journal.db.execute('SELECT frame_id,body FROM frames ORDER BY seq'):
            frame = decode(body)
            expected = tuple((item.command_id, key) for item, key in self._ordered(frame))
            actual = tuple(self.journal.db.execute(
                'SELECT command_id,order_key FROM commands WHERE frame_id=? AND parent_id IS NULL '
                'ORDER BY order_key', (frame_id,)).fetchall())
            if actual != expected:
                raise JournalError('Sealed frame dispatch order changed')

    def submit_frame(self, frame: ReplayFrame) -> FrameReceipt:
        self._require_active()
        if not isinstance(frame, ReplayFrame) or frame.as_of < self.spec.initial_at:
            raise ValueError('Valid frame after run initialization required')
        try:
            seq, state = self.journal.seal(frame, self._ordered(frame))
            return FrameReceipt(frame.frame_id, frame.as_of, seq, frame.command_count,
                                frame.frame_hash, state)
        except BaseException:
            # A failed or uncertain SQLite write invalidates this actor graph.
            self._quarantine()
            raise

    def _invoke(self, row):
        actors = self._actors
        at = pd.Timestamp(row['at_ns'], tz='UTC')
        payload = decode(row['payload'])
        before = actors.images()
        kind = row['kind']
        if kind in ('MARK', 'FX'):
            result = actors.portfolio.observe(payload, kind=kind, as_of=at,
                                              event_id=row['command_id'])
        elif kind == 'FUNDING':
            allocation_id, cash_flow = payload
            result = actors.portfolio.settle_funding(allocation_id, cash_flow, as_of=at,
                                                      event_id=row['command_id'])
        elif kind in ('ENTRY_FILL', 'EXIT_FILL'):
            result = actors.bridge.process_fill(payload, as_of=at)
        elif kind == 'CANCEL':
            result = actors.bridge.cancel(payload, as_of=at)
        elif kind == 'EXPIRE':
            result = actors.bridge.expire(payload, as_of=at)
        elif kind == 'MONITOR':
            signal_id, candle = payload
            result = actors.signals.observe_signal(signal_id, candle, as_of=at)
        elif kind == 'CONTEXT':
            pattern, candle, structure, structure_candle, m6 = payload
            context = assemble_context(pattern, candle, structure, structure_candle, m6,
                                       as_of=at, mode='BACKTEST', config=self.spec.context_config)
            result = actors.signals.observe(context)
        elif kind == 'RISK':
            signal_id, spec, costs, leverage, requested = payload
            record = next((r for r in actors.signals.history if r.signal_id == signal_id), None)
            if record is None:
                raise ValueError('Risk request lacks a qualified M7 signal')
            proposal = proposal_from_m7(record, as_of=at, events=actors.signals.events)
            result = actors.risk.evaluate(proposal, spec, costs, leverage=leverage,
                                          requested_quantity=requested, as_of=at)
        elif kind == 'TERMINAL':
            result = actors.bridge.on_signal_terminal(payload, as_of=at)
        elif kind == 'ADMIT':
            decision = next((d for d in actors.risk.history if d.decision_id == payload), None)
            if decision is None:
                raise ValueError('Admission lacks authoritative M8 decision')
            result = actors.bridge.propose(decision, as_of=at)
        elif kind == 'RELEASE':
            allocation_id, _reason = payload
            result = actors.portfolio.cancel(allocation_id, as_of=at, event_id=row['command_id'])
        elif kind == 'OUTBOX_ENQUEUE':
            result = actors.outbox.enqueue(payload, as_of=at)
        elif kind == 'OUTBOX_DELIVERY':
            notification_id, action, operation_id, reason = payload
            result = actors.outbox.simulate_delivery(notification_id, action,
                                                     operation_id=operation_id, as_of=at,
                                                     reason_code=reason)
        else:
            raise ValueError('Unsupported journal command')
        return result, _effects(before, actors.images())

    def _validate_root(self, row):
        if row['parent_id'] is not None or row['frame_id'] is None:
            return None
        payload = decode(row['payload'])
        kind = row['kind']
        if kind == 'RISK' and (type(payload) is not tuple or len(payload) != 5
                               or type(payload[0]) is not str
                               or not isinstance(payload[1], InstrumentSpec)
                               or not isinstance(payload[2], CostModel)):
            return 'RISK_INPUT_INVALID'
        if kind == 'CONTEXT' and (type(payload) is not tuple or len(payload) != 5
                                  or not isinstance(payload[0], PatternEvent)
                                  or not isinstance(payload[1], ClosedCandle)
                                  or not isinstance(payload[2], StructureResult)
                                  or payload[3] is not None and not isinstance(payload[3], ClosedCandle)
                                  or not isinstance(payload[4], M6Bundle)):
            return 'CONTEXT_INPUT_INVALID'
        if kind in ('MARK', 'FX') and not isinstance(payload, Quote):
            return 'QUOTE_INPUT_INVALID'
        if kind in ('ENTRY_FILL', 'EXIT_FILL') and not isinstance(payload, SimulationFill):
            return 'FILL_INPUT_INVALID'
        if kind == 'CANCEL' and not isinstance(payload, SimulationCancellation):
            return 'CANCEL_INPUT_INVALID'
        return None

    def _children_and_jobs(self, row, effects):
        children, sources = [], []
        for event in effects['m7_events']:
            children.append((identity('M9.3-terminal', event.event_id), 'TERMINAL', event,
                             len(children)+1))
        for decision in effects['risk_decisions']:
            children.append((identity('M9.3-admit', decision.decision_id), 'ADMIT',
                             decision.decision_id, len(children)+1))
        for receipt in effects['admissions']:
            if (receipt.disposition == 'DENIED' and receipt.risk.status in ('APPROVED', 'REDUCED')
                    and any(p.allocation_id == receipt.risk.allocation_id for p in
                            self._actors.portfolio.snapshot(as_of=receipt.available_at).reservations)):
                children.append((identity('M9.3-release', receipt.receipt_id), 'RELEASE',
                                 (receipt.risk.allocation_id, 'ADMISSION_DENIED'), len(children)+1))
            sources.append(receipt.order if receipt.order is not None else receipt)
        sources.extend(record for record in effects['m7_records'] if record.qualification_passed)
        sources.extend(effects['risk_decisions'])
        for event in effects['bridge_events']:
            if event.kind == 'ORDER_PROPOSED':
                continue
            order = next((o for o in self._actors.bridge.orders if o.order_id == event.order_id), None)
            if order is None:
                raise JournalError('Lifecycle lacks its authoritative order')
            sources.append((event, order))
        jobs = [(identity('M9.3-notification-job', source), source) for source in sources]
        return tuple(children), tuple(jobs)

    def _verify_derived(self, row, effects):
        expected_children, expected_jobs = self._children_and_jobs(row, effects)
        stored_children = self.journal.db.execute(
            'SELECT command_id,kind,payload,order_key,at_ns FROM commands WHERE parent_id=? ORDER BY order_key',
            (row['command_id'],)).fetchall()
        children = tuple((child_id, kind, encode(payload),
                          row['order_key']+f'.{ordinal:06d}', row['at_ns'])
                         for child_id, kind, payload, ordinal in expected_children)
        if tuple(stored_children) != tuple(sorted(children, key=lambda item: item[3])):
            raise JournalError('Committed derived actor calls changed')
        stored_jobs = self.journal.db.execute(
            'SELECT job_id,source FROM jobs WHERE source_command=? ORDER BY seq',
            (row['command_id'],)).fetchall()
        jobs = tuple((job_id, encode(source)) for job_id, source in expected_jobs)
        if tuple(stored_jobs) != jobs:
            raise JournalError('Committed notification jobs changed')

    def advance(self) -> StageOutcome | None:
        self._require_active()
        pending = self.journal.next_core()
        if pending is None:
            return None
        at = pd.Timestamp(pending['at_ns'], tz='UTC')
        try:
            reason = self._validate_root(pending)
            if reason is not None:
                outcome = StageOutcome(pending['command_id'], pending['kind'], at,
                                       'INPUT_REJECTED', None, (reason,), (), ())
                self.journal.apply(pending['command_id'], outcome, {}, self._actors.digest())
                return outcome
            result, effects = self._invoke(pending)
            children, jobs = self._children_and_jobs(pending, effects)
            outcome = StageOutcome(pending['command_id'], pending['kind'], at, 'APPLIED',
                                   result, (), _effect_ids(effects), tuple(job_id for job_id, _ in jobs))
            self.journal.apply(pending['command_id'], outcome, effects, self._actors.digest(),
                               children=children, jobs=jobs)
            return outcome
        except BaseException:
            self._quarantine()
            raise

    def _process_outbox(self, row, *, job_progress=None, job_failure=None):
        at = pd.Timestamp(row['at_ns'], tz='UTC')
        try:
            result, effects = self._invoke(row)
        except Exception:
            # Notification actors may have mutated before failing. Rebuild first.
            self._restore()
            failed = StageOutcome(row['command_id'], row['kind'], at, 'INPUT_REJECTED', None,
                                  ('NOTIFICATION_FAILED',), (), ())
            try:
                self.journal.apply(row['command_id'], failed, {}, self._actors.digest(),
                                   job_failure=job_failure)
            except BaseException:
                self._quarantine()
                raise
            return failed
        except BaseException:
            self._quarantine()
            raise
        outcome = StageOutcome(row['command_id'], row['kind'], at, 'APPLIED', result, (),
                               _effect_ids(effects), ())
        try:
            self.journal.apply(row['command_id'], outcome, effects, self._actors.digest(),
                               job_progress=job_progress)
        except BaseException:
            self._quarantine()
            raise
        return outcome

    def drain_notifications(self, *, as_of):
        self._require_active()
        at = utc_timestamp(as_of)
        latest = self.journal.last_applied_time()
        last_box = self._actors.outbox.history[-1].available_at if self._actors.outbox.history else None
        if ((latest is not None and at.value < latest)
                or (last_box is not None and at < last_box)
                or self.journal.unfinished_at_or_before(at)):
            raise JournalError('Notification cutoff precedes committed or unfinished work')
        results = []
        while (job := self.journal.next_job()) is not None:
            source = decode(job['source'])
            if job['state'] == 'PENDING':
                try:
                    event_source, order = source if (type(source) is tuple and len(source) == 2
                                                     and isinstance(source[0], LifecycleEvent)) else (source, None)
                    events = notification_events(event_source, order=order, as_of=at)
                except Exception:
                    try:
                        self.journal.fail_job(job['job_id'], 'NOTIFICATION_MAPPING_FAILED')
                    except BaseException:
                        self._quarantine()
                        raise
                    continue
                try:
                    self.journal.materialize_job(job['job_id'], events)
                except BaseException:
                    self._quarantine()
                    raise
                job = self.journal.job(job['job_id'])
                if job['state'] == 'COMPLETE':
                    continue
            views = decode(job['views'])
            event = views[job['next_view']]
            if event.source_available_at > at:
                raise JournalError('Notification source unavailable at drain cutoff')
            command_id = identity('M9.3-enqueue', job['job_id'], job['next_view'])
            try:
                prior = self.journal.command(command_id)
            except JournalError:
                prior = None
            if prior is None:
                try:
                    self.journal.add_direct(command_id, 'OUTBOX_ENQUEUE', at, event)
                except BaseException:
                    self._quarantine()
                    raise
            elif prior['kind'] != 'OUTBOX_ENQUEUE' or decode(prior['payload']) != event:
                raise JournalError('Notification enqueue command changed')
            row = self.journal.command(command_id)
            next_view = job['next_view']+1
            if row['state'] == 'APPLIED':
                results.append(decode(row['outcome']))
                continue
            results.append(self._process_outbox(row, job_progress=(job['job_id'], next_view, len(views)),
                                                job_failure=(job['job_id'], 'NOTIFICATION_RENDER_FAILED')))
        return tuple(results)

    def simulate_delivery(self, notification_id, action, *, command_id, operation_id, as_of,
                          reason_code=None):
        self._require_active()
        at = utc_timestamp(as_of)
        latest = self.journal.last_applied_time()
        last_box = self._actors.outbox.history[-1].available_at if self._actors.outbox.history else None
        if ((latest is not None and at.value < latest)
                or (last_box is not None and at < last_box)
                or self.journal.unfinished_at_or_before(at)):
            raise JournalError('Delivery cutoff precedes committed or unfinished work')
        payload = (notification_id, action, operation_id, reason_code)
        try:
            self.journal.add_direct(command_id, 'OUTBOX_DELIVERY', at, payload)
        except BaseException:
            self._quarantine()
            raise
        row = self.journal.command(command_id)
        if row['state'] == 'APPLIED':
            return decode(row['outcome'])
        return self._process_outbox(row)

    def verify(self) -> VerificationReport:
        self._require_active()
        self.journal.verify_storage()
        frame_count = self.journal.db.execute('SELECT COUNT(*) FROM frames').fetchone()[0]
        return VerificationReport(self.spec.run_id, len(self.journal.applied()), frame_count,
                                  self._actors.digest(), self.journal.meta('chain'))

    def receipt(self, command_id: str) -> StageOutcome | None:
        """Return a committed command result without invoking any actor again."""
        self._require_active()
        row = self.journal.command(command_id)
        return decode(row['outcome']) if row['state'] == 'APPLIED' else None

    @property
    def signals(self):
        self._require_active()
        return self._actors.signals.history

    @property
    def decisions(self):
        self._require_active()
        return self._actors.risk.history

    @property
    def orders(self):
        self._require_active()
        return self._actors.bridge.orders

    @property
    def admissions(self):
        self._require_active()
        return self._actors.bridge.receipts

    @property
    def outbox_entries(self):
        self._require_active()
        return self._actors.outbox.entries

    def portfolio(self, *, as_of):
        self._require_active()
        return self._actors.portfolio.snapshot(as_of=as_of)


def create_run(spec: RunSpec, path) -> OfflineOrchestrator:
    return OfflineOrchestrator.create(spec, path)


def recover(path, expected_spec: RunSpec) -> OfflineOrchestrator:
    return OfflineOrchestrator.recover(path, expected_spec)
