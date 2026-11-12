"""M7-v1 qualification and descriptive confluence. Never risk/order approval."""
from dataclasses import dataclass

import pandas as pd

from trading.derivatives import UNITS
from trading.m6_data import identity, jsonable, nonempty, positive
from trading.pattern_context import utc_timestamp
from trading.pattern_engine import ClosedCandle, SetupDecision, TIMEFRAMES, evaluate_setup
from trading.patterns import _number
from trading.signal_context import SignalContext, SignalContextConfig

GROUPS = ('pattern', 'structure', 'macro', 'derivatives')
CONTRADICTIONS = ('STRUCTURE_TREND_OPPOSES_PATTERN', 'DIRECTION_1H_OPPOSES_PATTERN',
                  'REGIME_4H_OPPOSES_PATTERN', 'DERIVATIVES_OPPOSE_PATTERN')


@dataclass(frozen=True)
class GroupCap:
    group: str
    cap: float

    def __post_init__(self) -> None:
        if self.group not in GROUPS or _number('cap', self.cap) < 0:
            raise ValueError('Known group and nonnegative finite cap required')


@dataclass(frozen=True)
class ContradictionRule:
    reason: str
    action: str

    def __post_init__(self) -> None:
        if self.reason not in CONTRADICTIONS or self.action not in ('BLOCK', 'RECORD_ONLY'):
            raise ValueError('Known contradiction and explicit action required')


@dataclass(frozen=True)
class DerivativeRule:
    kind: str
    name: str
    condition: str
    direction: str

    def __post_init__(self) -> None:
        if self.kind not in ('metric', 'evidence') or self.direction not in ('BULLISH', 'BEARISH', 'NEUTRAL'):
            raise ValueError('Explicit derivative interpretation mapping required')
        nonempty('name', self.name)
        nonempty('condition', self.condition)
        if self.kind == 'metric' and self.name not in UNITS:
            raise ValueError('Unknown derivatives metric')


@dataclass(frozen=True)
class SignalConfig:
    policy_version: str
    group_caps: tuple[GroupCap, ...]
    contradiction_rules: tuple[ContradictionRule, ...]
    derivative_rules: tuple[DerivativeRule, ...]
    require_directional_structure: bool
    signal_ttl_seconds: float | None
    schema_version: str = 'M7-v1'

    def __post_init__(self) -> None:
        nonempty('policy_version', self.policy_version)
        if self.schema_version != 'M7-v1' or not isinstance(self.require_directional_structure, bool):
            raise ValueError('Supported schema and explicit structure gate required')
        for values, expected in ((self.group_caps, GroupCap), (self.contradiction_rules, ContradictionRule),
                                 (self.derivative_rules, DerivativeRule)):
            if not isinstance(values, tuple) or any(not isinstance(v, expected) for v in values):
                raise ValueError('Immutable typed policy tuples required')
        if len(self.group_caps) != len(GROUPS) or {g.group for g in self.group_caps} != set(GROUPS):
            raise ValueError('Specify each group cap exactly once; no implicit weights')
        _number('total group cap', sum(g.cap for g in self.group_caps))
        if len(self.contradiction_rules) != len(CONTRADICTIONS) or {r.reason for r in self.contradiction_rules} != set(CONTRADICTIONS):
            raise ValueError('Specify each contradiction action exactly once')
        keys = [(r.kind, r.name, r.condition) for r in self.derivative_rules]
        if len(keys) != len(set(keys)):
            raise ValueError('Ambiguous derivative mappings')
        if self.signal_ttl_seconds is not None:
            positive('signal_ttl_seconds', self.signal_ttl_seconds)
        object.__setattr__(self, 'group_caps', tuple(sorted(self.group_caps, key=lambda g: g.group)))
        object.__setattr__(self, 'contradiction_rules', tuple(sorted(self.contradiction_rules, key=lambda r: r.reason)))
        object.__setattr__(self, 'derivative_rules', tuple(sorted(self.derivative_rules, key=lambda r: (r.kind, r.name, r.condition))))


@dataclass(frozen=True)
class GroupEvidence:
    group: str
    available: bool
    cap: float
    support: float
    conflict: float
    contribution: float | None
    supporting_ids: tuple[str, ...]
    conflicting_ids: tuple[str, ...]
    neutral_ids: tuple[str, ...]


@dataclass(frozen=True)
class EvidenceCoverage:
    available_groups: tuple[str, ...]
    directional_groups: tuple[str, ...]
    unavailable_groups: tuple[str, ...]


@dataclass(frozen=True)
class SignalEvaluation:
    schema_version: str
    symbol: str
    as_of: pd.Timestamp
    mode: str
    configuration_id: str
    config: SignalConfig
    candidate_side: str | None
    decision: str
    qualification_passed: bool
    score: float | None
    score_bound: float
    coverage: EvidenceCoverage
    confidence: None
    confidence_kind: str
    groups: tuple[GroupEvidence, ...]
    contradictions: tuple[str, ...]
    blocking_reasons: tuple[str, ...]
    reason_codes: tuple[str, ...]
    m3_decision: SetupDecision
    context: SignalContext

    def to_dict(self) -> dict:
        return jsonable(self)


def evaluate_signal(context: SignalContext, config: SignalConfig) -> SignalEvaluation:
    """Pure evaluation. A tracker must consume the first actual M3 confirmation."""
    if not isinstance(context, SignalContext) or not isinstance(config, SignalConfig):
        raise ValueError('Assembled context and explicit signal policy required')
    pattern, candle, at = context.pattern, context.confirmation_candle, context.as_of
    direction = pattern.classification
    side = 'LONG' if direction == 'BULLISH' else 'SHORT' if direction == 'BEARISH' else None
    groups, contradictions = [], []
    caps = {g.group: g.cap for g in config.group_caps}

    def group(name: str, available: bool, observations: list, strength: float = 1.) -> None:
        support, conflict, neutral = set(), set(), set()
        for observed_direction, source_id, reason in observations:
            if observed_direction == 'NEUTRAL':
                neutral.add(source_id)
            elif side is not None and observed_direction == direction:
                support.add(source_id)
            elif side is not None and observed_direction in ('BULLISH', 'BEARISH'):
                conflict.add(source_id)
                if reason:
                    contradictions.append(reason)
        s, c = strength if support else 0., 1. if conflict else 0.
        contribution = caps[name]*max(-1., min(1., s-c)) if available else None
        groups.append(GroupEvidence(name, available, caps[name], s, c, contribution,
                                    tuple(sorted(support)), tuple(sorted(conflict)), tuple(sorted(neutral))))

    pattern_available = at < pattern.expires_at and not any(r in context.reasons for r in (
        'confirmation_began_before_pattern_availability', 'confirmation_candle_stale'))
    group('pattern', pattern_available,
          [(direction, pattern.event_id, '')] if pattern_available else [], pattern.geometry_score/100)
    sources = {s.role: s for s in context.sources}
    structure_fresh = sources['structure_15m'].status == 'AVAILABLE'
    trend = context.structure.trend
    group('structure', structure_fresh,
          [(trend if trend in ('BULLISH', 'BEARISH') else 'NEUTRAL' if trend == 'RANGE' else 'UNKNOWN',
            sources['structure_15m'].source_id, 'STRUCTURE_TREND_OPPOSES_PATTERN')] if structure_fresh else [])
    macro = []
    for role, observed, reason in (
            ('direction_1h', context.snapshot.higher_direction, 'DIRECTION_1H_OPPOSES_PATTERN'),
            ('regime_4h', context.snapshot.regime, 'REGIME_4H_OPPOSES_PATTERN')):
        if sources[role].status == 'AVAILABLE' and observed in ('BULLISH', 'BEARISH', 'RANGE', 'NEUTRAL'):
            macro.append(('NEUTRAL' if observed == 'RANGE' else observed, sources[role].source_id, reason))
    group('macro', bool(macro), macro)

    mappings = {(r.kind, r.name, r.condition): r.direction for r in config.derivative_rules}
    derivative_votes, available = [], False
    for result in context.m6.derivatives:
        policies = {p.metric: p for p in result.config.policies if p.enabled}
        current_fresh_ids, fresh_ids = set(), set()
        for metric in result.metrics:
            observation = metric.observation
            policy = policies.get(metric.metric)
            if (metric.status != 'AVAILABLE' or observation is None or policy is None
                    or observation.time.status(at, policy.max_age_seconds, context.mode) != 'AVAILABLE'):
                continue
            available = True
            current_fresh_ids.add(observation.observation_id)
            fresh_ids.add(observation.observation_id)
            if metric.previous is not None:
                fresh_ids.add(metric.previous.observation_id)
            mapped = mappings.get(('metric', metric.metric, metric.condition))
            if mapped is not None:
                derivative_votes.append((mapped, observation.observation_id, 'DERIVATIVES_OPPOSE_PATTERN'))
        for evidence in result.evidence:
            if not evidence.input_ids or not set(evidence.input_ids).issubset(fresh_ids):
                continue
            if not set(evidence.input_ids).intersection(current_fresh_ids):
                continue
            mapped = mappings.get(('evidence', evidence.name, evidence.value))
            if mapped is not None:
                derivative_votes.append((mapped, identity(evidence), 'DERIVATIVES_OPPOSE_PATTERN'))
    group('derivatives', available, derivative_votes)

    m3 = evaluate_setup(pattern, candle, as_of=at, context=context.snapshot)
    blocking = []
    if m3.status != 'CONFIRMED':
        blocking.append(f'M3_{m3.status}')
    if not context.structure_eligible:
        blocking.append('M5_LOCATION_UNAVAILABLE')
    if config.require_directional_structure and trend not in ('BULLISH', 'BEARISH'):
        blocking.append('M5_DIRECTIONAL_STRUCTURE_REQUIRED')
    contradictions = tuple(sorted(set(contradictions)))
    actions = {r.reason: r.action for r in config.contradiction_rules}
    blocking.extend(f'BLOCKED_{r}' for r in contradictions if actions[r] == 'BLOCK')
    blocking = tuple(sorted(set(blocking)))
    passed = not blocking and side is not None
    coverage = EvidenceCoverage(tuple(g.group for g in groups if g.available),
                                tuple(g.group for g in groups if g.support or g.conflict),
                                tuple(g.group for g in groups if not g.available))
    contributions = [g.contribution for g in groups if g.contribution is not None]
    reasons = tuple(sorted(set((*blocking, *contradictions, *context.reasons,
                                *(f'M3_{r.upper()}' for r in m3.reasons),
                                'HEATMAPS_EXPLANATORY_ONLY', 'VOLATILITY_CONTEXT_ONLY', 'SCORE_NOT_PROBABILITY'))))
    return SignalEvaluation('M7-v1', context.symbol, at, context.mode,
                            identity('M7-v1', config, context.config), config, side,
                            side if passed else 'NO_TRADE', passed,
                            sum(contributions) if contributions else None, sum(caps.values()), coverage,
                            None, 'UNCALIBRATED', tuple(groups), contradictions, blocking, reasons, m3, context)


@dataclass(frozen=True)
class SignalRecord:
    schema_version: str
    symbol: str
    mode: str
    candidate_id: str
    decision_id: str
    signal_id: str | None
    policy_id: str
    input_id: str
    pattern_id: str
    candidate_side: str | None
    decision: str
    lifecycle_state: str
    qualification_passed: bool
    evaluated_at: pd.Timestamp
    available_at: pd.Timestamp
    expires_at: pd.Timestamp
    reference_price: float
    invalidation_price: float | None
    evaluation: SignalEvaluation

    def to_dict(self) -> dict:
        return jsonable(self)


@dataclass(frozen=True)
class SignalLifecycleEvent:
    event_id: str
    signal_id: str
    kind: str
    available_at: pd.Timestamp
    price_closed_at: pd.Timestamp | None
    price_bar_id: str | None
    reason: str


class SignalTracker:
    """In-memory, per-candidate replay with first-confirmation consumption.

    Durable restart recovery, cross-policy deduplication, reservations and orders
    belong to M8+. No method here grants risk or execution approval.
    """
    def __init__(self, config: SignalConfig, context_config: SignalContextConfig, *, mode: str):
        if not isinstance(config, SignalConfig) or not isinstance(context_config, SignalContextConfig):
            raise ValueError('Explicit immutable signal/context policies required')
        if mode not in ('BACKTEST', 'CURRENT'):
            raise ValueError('Explicit analysis mode required')
        self.config = config
        self.context_config = context_config
        self.mode = mode
        self.policy_id = identity('M7-v1', config, context_config)
        self._patterns = {}
        self._last_time = {}
        self._last_bar = {}
        self._last_record = {}
        self._candidate_bars = {}
        self._seen = {}
        self._consumed = set()
        self._history = []
        self._signals = {}
        self._signal_last_time = {}
        self._signal_bars = {}
        self._signal_end = {}
        self._events = []

    @property
    def history(self) -> tuple[SignalRecord, ...]:
        return tuple(self._history)

    @property
    def events(self) -> tuple[SignalLifecycleEvent, ...]:
        return tuple(self._events)

    def observe(self, context: SignalContext) -> SignalRecord:
        if not isinstance(context, SignalContext) or context.mode != self.mode:
            raise ValueError('Matching assembled context/mode required')
        if context.configuration_id != identity('M7-context-v1', self.context_config):
            raise ValueError('Cannot change a tracker context policy')
        pattern, candle, at = context.pattern, context.confirmation_candle, context.as_of
        candidate_id = identity('M7-candidate-v1', self.policy_id, self.mode, context.symbol, pattern.event_id)
        registered = self._patterns.get(pattern.event_id)
        if registered is not None and registered != pattern:
            raise ValueError('Cannot rewrite a registered M3 pattern')
        if candidate_id in self._last_time and at < self._last_time[candidate_id]:
            raise ValueError('Candidate observations must be chronological')
        key = (candidate_id, candle.opened_at, at)
        bar_key = (candidate_id, candle.opened_at)
        if bar_key in self._candidate_bars and self._candidate_bars[bar_key] != identity(candle):
            raise ValueError('Cannot rewrite an observed candidate candle')
        if key in self._seen and self._seen[key] != context.input_id:
            raise ValueError('Cannot rewrite an observed decision input')
        previous = self._last_record.get(candidate_id)
        if candidate_id in self._consumed:
            if previous.signal_id is not None:
                self.observe_signal(previous.signal_id, candle, as_of=at)
            self._last_time[candidate_id] = at
            self._seen[key] = context.input_id
            return previous
        if previous is not None and candle.opened_at <= self._last_bar[candidate_id]:
            # Match M3's one-processing-per-event/candle contract; no same-bar retry.
            self._last_time[candidate_id] = at
            self._seen[key] = context.input_id
            return previous
        first_open = pattern.confirmed_at.ceil('5min')
        expected_open = self._last_bar[candidate_id]+TIMEFRAMES['5m'] if previous is not None else first_open
        if candle.opened_at > expected_open and at < pattern.expires_at:
            raise ValueError('Replay every eligible 5m candle; missing confirmation history')
        evaluation = evaluate_signal(context, self.config)
        if evaluation.configuration_id != self.policy_id:
            raise ValueError('Cannot change a tracker policy')
        status = evaluation.m3_decision.status
        state = 'QUALIFIED' if evaluation.qualification_passed else 'REJECTED' if status == 'CONFIRMED' else status
        if state not in ('WAITING', 'QUALIFIED', 'REJECTED', 'INVALIDATED', 'EXPIRED'):
            raise ValueError('Unsupported M3 lifecycle outcome')
        expires = pattern.expires_at
        if self.config.signal_ttl_seconds is not None:
            expires = min(expires, at+pd.Timedelta(seconds=self.config.signal_ttl_seconds))
        signal_id = identity('M7-signal-v1', candidate_id, evaluation.candidate_side, candle.opened_at) if evaluation.qualification_passed else None
        record = SignalRecord('M7-v1', context.symbol, self.mode, candidate_id,
                              identity('M7-decision-v1', candidate_id, at, candle.opened_at, context.input_id),
                              signal_id, self.policy_id, context.input_id, pattern.event_id,
                              evaluation.candidate_side, evaluation.decision, state,
                              evaluation.qualification_passed, at, at, expires, candle.ohlc.close,
                              pattern.invalidation_price, evaluation)
        # Mutate state only after successful validation/evaluation.
        self._patterns[pattern.event_id] = pattern
        self._last_time[candidate_id] = at
        self._last_bar[candidate_id] = candle.opened_at
        self._last_record[candidate_id] = record
        self._seen[key] = context.input_id
        self._history.append(record)
        self._candidate_bars[bar_key] = identity(candle)
        if state != 'WAITING':
            self._consumed.add(candidate_id)
        if signal_id is not None:
            self._signals[signal_id] = record
            self._signal_last_time[signal_id] = at
            self._signal_bars[signal_id] = {candle.opened_at: identity(candle)}
        return record

    def observe_signal(self, signal_id: str, candle: ClosedCandle, *,
                       as_of: pd.Timestamp) -> SignalLifecycleEvent | None:
        """Append expiry/invalidation from price alone, even if M5/M6 is unavailable."""
        if signal_id not in self._signals:
            raise ValueError('Unknown signal')
        at = utc_timestamp(as_of)
        record = self._signals[signal_id]
        if not isinstance(candle, ClosedCandle) or (candle.symbol, candle.timeframe) != (record.symbol, '5m'):
            raise ValueError('Matching completed 5m candle required')
        if candle.available_at > at or at < self._signal_last_time[signal_id]:
            raise ValueError('Signal observations must be available and chronological')
        seen = self._signal_bars[signal_id]
        fingerprint = identity(candle)
        if candle.opened_at in seen and seen[candle.opened_at] != fingerprint:
            raise ValueError('Cannot rewrite an observed signal candle')
        event = self._signal_end.get(signal_id)
        if event is not None:
            self._signal_last_time[signal_id] = at
            return event
        kind, reason, price_closed, bar_id = None, None, None, None
        if at >= record.expires_at:
            kind, reason = 'EXPIRED', 'SIGNAL_VALIDITY_EXPIRED'
        elif candle.opened_at > max(max(seen)+TIMEFRAMES['5m'], record.available_at.ceil('5min')):
            raise ValueError('Replay every post-signal 5m candle; missing invalidation history')
        elif (candle.opened_at >= record.available_at and candle.opened_at not in seen
              and (not seen or candle.opened_at > max(seen))):
            if at-candle.available_at >= TIMEFRAMES['5m']:
                raise ValueError('Cannot evaluate stale 5m invalidation evidence; replay at original availability')
            touched = candle.ohlc.low <= record.invalidation_price if record.decision == 'LONG' else candle.ohlc.high >= record.invalidation_price
            if touched:
                kind, reason = 'INVALIDATED', 'UPSTREAM_PATTERN_EXTREME_TOUCHED'
                price_closed, bar_id = candle.closed_at, fingerprint
        if kind is not None:
            event = SignalLifecycleEvent(identity('M7-lifecycle-v1', signal_id, kind, at, bar_id),
                                         signal_id, kind, at, price_closed, bar_id, reason)
            self._events.append(event)
            self._signal_end[signal_id] = event
        self._signal_last_time[signal_id] = at
        seen[candle.opened_at] = fingerprint
        return event
