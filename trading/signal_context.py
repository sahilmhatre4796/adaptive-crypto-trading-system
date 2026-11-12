"""M7 context assembly over existing M3-v2, M5 and M6 contracts. No signals/orders."""
from dataclasses import dataclass

import pandas as pd

from trading.m6 import M6Bundle
from trading.m6_data import Mode, Source, identity, positive
from trading.pattern_context import ContextSnapshot, utc_timestamp
from trading.pattern_engine import ClosedCandle, PatternEngine, PatternEvent, SYMBOLS, TIMEFRAMES
from trading.regime import RegimeResult
from trading.structure import StructureResult


@dataclass(frozen=True)
class SignalContextConfig:
    structure_max_age_seconds: float
    range_direction: str
    transition_regime: str

    def __post_init__(self) -> None:
        positive('structure_max_age_seconds', self.structure_max_age_seconds)
        if self.range_direction not in ('NEUTRAL', 'UNKNOWN'):
            raise ValueError('Range direction must be explicitly NEUTRAL or UNKNOWN')
        if self.transition_regime not in ('UNCERTAIN', 'UNKNOWN'):
            raise ValueError('Transition regime must be explicitly UNCERTAIN or UNKNOWN')


@dataclass(frozen=True)
class ContextSource:
    role: str
    status: str
    observed_at: pd.Timestamp | None
    available_at: pd.Timestamp | None
    source_id: str | None
    configuration_id: str | None
    provenance: Source | None
    reasons: tuple[str, ...]


@dataclass(frozen=True)
class SignalContext:
    symbol: str
    as_of: pd.Timestamp
    mode: Mode
    configuration_id: str
    config: SignalContextConfig
    input_id: str
    snapshot: ContextSnapshot
    sources: tuple[ContextSource, ...]
    structure_eligible: bool
    reasons: tuple[str, ...]
    pattern: PatternEvent
    confirmation_candle: ClosedCandle
    structure: StructureResult
    structure_candle: ClosedCandle | None
    m6: M6Bundle  # Original records retain observation/publication/receipt metadata.


def _regime_source(result: RegimeResult | None, role: str, as_of: pd.Timestamp,
                   timeframe: str) -> ContextSource:
    if result is None:
        return ContextSource(role, 'MISSING', None, None, None, None, None, (f'{role}_missing',))
    closed, available = result.candle_closed_at, result.available_at
    status = result.status
    reasons = ()
    if closed is not None and closed.floor(TIMEFRAMES[timeframe]) != closed:
        raise ValueError('M6 regime candle close is off timeframe boundary')
    if status == 'AVAILABLE':
        if closed is None or available is None:
            raise ValueError('Available regime result requires source times')
        deadline = min(result.config.max_age_intervals*TIMEFRAMES[timeframe], TIMEFRAMES[timeframe])
        if as_of-closed > deadline:
            status = 'STALE'
    if status != 'AVAILABLE':
        reasons = (f'{role}_{status.lower()}',)
    return ContextSource(role, status, closed, available, identity(result), result.configuration_id,
                         result.source, reasons)


def assemble_context(pattern: PatternEvent, candle: ClosedCandle, structure: StructureResult,
                     structure_candle: ClosedCandle | None, m6: M6Bundle, *,
                     as_of: pd.Timestamp, mode: Mode, config: SignalContextConfig) -> SignalContext:
    """Build current M3 context without rewriting a pattern or renewing stale evidence.

    Required policies have no numerical defaults. M5's source candle is supplied
    separately because its result exposes receipt, not the latest price close.
    The caller binds that candle to the analyzed source; IDs cannot authenticate it.
    """
    as_of = utc_timestamp(as_of)
    if not isinstance(config, SignalContextConfig) or mode not in ('BACKTEST', 'CURRENT'):
        raise ValueError('Typed context config and explicit analysis mode required')
    if not isinstance(pattern, PatternEvent) or pattern.symbol not in SYMBOLS or pattern.timeframe != '15m':
        raise ValueError('M7 requires a supported-asset 15m pattern')
    symbol = pattern.symbol
    if pattern.configuration_id != PatternEngine(pattern.geometry_config, pattern.context_config).configuration_id:
        raise ValueError('Pattern must retain the current M3-v2 configuration identity')
    if not isinstance(candle, ClosedCandle) or (candle.symbol, candle.timeframe) != (symbol, '5m'):
        raise ValueError('M7 requires matching independent 5m confirmation')
    if candle.available_at > as_of or pattern.confirmed_at > as_of:
        raise ValueError('Pattern/confirmation candle unavailable at cutoff')
    if not pattern.price_confirmed_at <= pattern.confirmed_at or pattern.detected_at > pattern.confirmed_at:
        raise ValueError('Invalid pattern confirmation ordering')
    if not isinstance(structure, StructureResult) or (structure.symbol, structure.timeframe, structure.as_of) != (symbol, '15m', as_of):
        raise ValueError('M5 must match the asset, 15m role and exact cutoff')
    if not isinstance(m6, M6Bundle) or (m6.symbol, m6.as_of) != (symbol, as_of):
        raise ValueError('M6 must match the asset and exact cutoff')
    # Use M6's reviewed wire boundary for nested validation, retaining original objects.
    M6Bundle.from_dict(m6.to_dict())
    if any(result.mode != mode for result in (*m6.derivatives, *m6.heatmaps)):
        raise ValueError('M6 external evidence mode mismatch')
    if structure.last_available_at is not None and structure.last_available_at > as_of:
        raise ValueError('Future M5 source receipt')
    if any(s.confirmed_at > as_of or s.price_confirmed_at > s.confirmed_at for s in structure.swings):
        raise ValueError('Future or invalid M5 swing')
    if any(e.available_at > as_of or e.price_confirmed_at > e.available_at for e in structure.events):
        raise ValueError('Future or invalid M5 event')
    if any(z.created_at > as_of for z in structure.zones):
        raise ValueError('Future M5 zone')

    sources, reasons = [], []
    if candle.opened_at < pattern.confirmed_at:
        reasons.append('confirmation_began_before_pattern_availability')
    if as_of-candle.available_at >= TIMEFRAMES['5m']:
        reasons.append('confirmation_candle_stale')
    if as_of >= pattern.expires_at:
        reasons.append('pattern_expired')
    sources.append(ContextSource('confirmation_5m', 'AVAILABLE' if not reasons else 'INELIGIBLE',
                                 candle.closed_at, candle.available_at, identity(candle), None, None, tuple(reasons)))

    structure_status = 'MISSING'
    if structure_candle is not None:
        if not isinstance(structure_candle, ClosedCandle) or (structure_candle.symbol, structure_candle.timeframe) != (symbol, '15m'):
            raise ValueError('M5 source candle must match asset and 15m role')
        if structure_candle.available_at != structure.last_available_at or structure_candle.available_at > as_of:
            raise ValueError('M5 source candle receipt does not match result')
        structure_status = 'STALE' if (as_of-structure_candle.closed_at).total_seconds() > config.structure_max_age_seconds else 'AVAILABLE'
    elif structure.last_available_at is not None:
        reasons.append('structure_source_candle_missing')
    structure_reasons = () if structure_status == 'AVAILABLE' else (f'structure_{structure_status.lower()}',)
    sources.append(ContextSource('structure_15m', structure_status,
                                 structure_candle.closed_at if structure_candle else None,
                                 structure.last_available_at, identity(structure), structure.configuration_id,
                                 None, structure_reasons))
    reasons.extend(structure_reasons)

    regimes = {result.timeframe: result for result in m6.regime}
    direction_source = _regime_source(regimes.get('1h'), 'direction_1h', as_of, '1h')
    regime_source = _regime_source(regimes.get('4h'), 'regime_4h', as_of, '4h')
    atr_source = _regime_source(regimes.get('15m'), 'volatility_15m', as_of, '15m')
    sources.extend((direction_source, regime_source, atr_source))
    for source in (direction_source, regime_source, atr_source):
        reasons.extend(source.reasons)
    direction, regime, atr, volume = 'UNKNOWN', 'UNKNOWN', None, None
    if direction_source.status == 'AVAILABLE':
        classification = regimes['1h'].classification
        direction = classification if classification in ('BULLISH', 'BEARISH') else config.range_direction if classification == 'RANGE' else 'UNKNOWN'
        if direction == 'UNKNOWN':
            reasons.append('direction_1h_unknown')
    if regime_source.status == 'AVAILABLE':
        classification = regimes['4h'].classification
        regime = config.transition_regime if classification == 'TRANSITION' else classification
        if regime in ('UNKNOWN', 'UNCERTAIN'):
            reasons.append(f'regime_4h_{regime.lower()}')
    if atr_source.status == 'AVAILABLE':
        evidence = regimes['15m'].evidence
        if len({e.name for e in evidence}) != len(evidence):
            raise ValueError('Ambiguous duplicate M6 indicator evidence')
        by_name = {e.name: e for e in evidence}
        if 'atr' in by_name and by_name['atr'].value is not None:
            if by_name['atr'].unit != 'quote':
                raise ValueError('ATR must be in quote price units')
            atr = positive('ATR', by_name['atr'].value)
        if 'relative_volume' in by_name and by_name['relative_volume'].value is not None:
            from trading.patterns import _number
            if by_name['relative_volume'].unit != 'ratio':
                raise ValueError('Relative volume must be a ratio')
            volume = _number('relative_volume', by_name['relative_volume'].value)
            if volume < 0:
                raise ValueError('Negative relative volume')
    if atr is None:
        reasons.append('atr_15m_unavailable')
    distances = dict(support_distance_atr=None, resistance_distance_atr=None)
    if structure_status == 'AVAILABLE' and atr is not None:
        distances = structure.level_distances(candle.ohlc.close, atr)
    relevant = distances['support_distance_atr'] if pattern.classification == 'BULLISH' else distances['resistance_distance_atr'] if pattern.classification == 'BEARISH' else None
    structure_eligible = (structure_status == 'AVAILABLE' and relevant is not None
                          and relevant <= pattern.context_config.max_level_distance_atr)
    if not structure_eligible:
        reasons.append('meaningful_structure_location_unavailable')
    available = [candle.available_at]
    for source in sources[1:]:
        if source.status == 'AVAILABLE' and source.available_at is not None:
            available.append(source.available_at)
    snapshot = ContextSnapshot(symbol, max(available), direction, regime,
                               direction_source.observed_at if direction != 'UNKNOWN' else None,
                               regime_source.observed_at if regime != 'UNKNOWN' else None,
                               atr=atr, relative_volume=volume, **distances)
    return SignalContext(symbol, as_of, mode, identity('M7-context-v1', config), config,
                         identity('M7-context-input-v1', pattern, candle, structure, structure_candle, m6),
                         snapshot, tuple(sources), structure_eligible, tuple(dict.fromkeys(reasons)),
                         pattern, candle, structure, structure_candle, m6)
