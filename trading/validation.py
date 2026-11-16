"""M10 offline metrics for completed, explicitly costed legacy backtest runs.

This module evaluates artifacts; it never creates a signal, fill or risk decision.
"""
from __future__ import annotations

from dataclasses import dataclass
from hashlib import sha256
import json
from math import isfinite, sqrt
from typing import Mapping

import numpy as np
import pandas as pd


FIVE_MINUTES = pd.Timedelta(minutes=5)
PER_YEAR = 365 * 24 * 12
DEFAULT_MINIMUM_RETURNS = 30 * 24 * 12  # Thirty regular 5m days; still not statistical proof.


@dataclass(frozen=True)
class OptionalMetric:
    value: float | None
    unavailable_reason: str | None = None


@dataclass(frozen=True)
class Attribution:
    dimension: str
    value: str
    trades: int
    net_pnl: float
    gross_pnl: float
    fees: float
    funding: float


@dataclass(frozen=True)
class TradeAttribution:
    trade_id: str
    entry_at: pd.Timestamp
    exit_at: pd.Timestamp
    side: str
    regime: str | None
    exit_reason: str
    gross_pnl: float
    fees: float
    funding: float
    net_pnl: float


@dataclass(frozen=True)
class ValidationReport:
    source_id: str
    symbol: str
    timeframe: str
    account_unit: str
    parameter_sha256: str
    fee_bps: float
    slippage_bps: float
    first_at: pd.Timestamp
    last_at: pd.Timestamp
    start_equity: float
    end_equity: float
    net_return_pct: float
    max_event_drawdown_pct: float
    max_drawdown_duration: pd.Timedelta
    drawdown_unrecovered: bool
    trades: int
    wins: int
    losses: int
    flats: int
    expectancy_cash: OptionalMetric
    profit_factor: OptionalMetric
    win_rate_pct: OptionalMetric
    sharpe_5m: OptionalMetric
    sortino_5m: OptionalMetric
    gross_pnl: float
    fees: float
    funding: float
    net_pnl: float
    trade_details: tuple[TradeAttribution, ...]
    attribution: tuple[Attribution, ...]
    unavailable: tuple[str, ...]
    assumptions: tuple[str, ...]


@dataclass(frozen=True)
class BenchmarkComparison:
    source_id: str
    account_unit: str
    cost_provenance: str
    benchmark_return_pct: float
    strategy_return_pct: float
    excess_return_percentage_points: float
    first_at: pd.Timestamp
    last_at: pd.Timestamp


def _finite(value, name, *, positive=False):
    if isinstance(value, bool) or not isinstance(value, (int, float, np.number)):
        raise ValueError(f'{name} must be an explicit finite number')
    number = float(value)
    if not isfinite(number) or (positive and number <= 0):
        raise ValueError(f'{name} must be an explicit finite number')
    return number


def _times(values, name):
    times = pd.DatetimeIndex(pd.to_datetime(values, utc=True, errors='raise'))
    if times.hasnans or not times.is_monotonic_increasing or times.has_duplicates:
        raise ValueError(f'{name} must be unique, chronological UTC timestamps')
    return times


def _drawdown_duration(events):
    if not isinstance(events, (tuple, list)) or not events:
        raise ValueError('Ordered risk-equity events are required')
    times = pd.to_datetime([row['risk_timestamp'] for row in events], utc=True)
    if any(times[i] < times[i-1] for i in range(1, len(times))):
        raise ValueError('Risk-equity event chronology changed')
    longest = pd.Timedelta(0)
    started = None
    peak = None
    peak_at = None
    for at, row in zip(times, events):
        equity = _finite(row['equity'], 'risk equity', positive=True)
        if peak is None or equity >= peak:
            if started is not None:
                longest = max(longest, at-started)
                started = None
            peak, peak_at = equity, at
        else:
            if started is None:
                started = peak_at
    if started is not None:
        longest = max(longest, times[-1]-started)
    return longest, started is not None


def _risk_adjusted(curve, minimum_returns):
    if len(curve) < minimum_returns+1:
        unavailable = OptionalMetric(None, 'INSUFFICIENT_RETURN_SAMPLES')
        return unavailable, unavailable
    times = _times(curve['timestamp'], 'Equity curve')
    if not (times[1:] - times[:-1] == FIVE_MINUTES).all():
        unavailable = OptionalMetric(None, 'IRREGULAR_EQUITY_CADENCE')
        return unavailable, unavailable
    values = np.asarray(curve['equity'], dtype=float)
    if not np.isfinite(values).all() or (values <= 0).any():
        raise ValueError('Positive finite equity curve required')
    returns = values[1:]/values[:-1]-1
    mean = float(returns.mean())
    standard = float(returns.std(ddof=1))
    sharpe = (OptionalMetric(mean/standard*sqrt(PER_YEAR)) if standard > 0
              else OptionalMetric(None, 'ZERO_RETURN_VARIANCE'))
    downside = float(np.sqrt(np.mean(np.minimum(returns, 0)**2)))
    sortino = (OptionalMetric(mean/downside*sqrt(PER_YEAR)) if downside > 0
               else OptionalMetric(None, 'NO_DOWNSIDE_RETURNS'))
    return sharpe, sortino


def evaluate_completed_run(summary: Mapping, equity: pd.DataFrame, trades: pd.DataFrame, *,
                           source_id: str, symbol: str, account_unit: str,
                           timeframe: str = '5m',
                           minimum_return_samples: int = DEFAULT_MINIMUM_RETURNS) -> ValidationReport:
    """Check and summarize one completed ``backtest.run`` result without replaying it."""
    if (not isinstance(source_id, str) or not source_id.strip()
            or not isinstance(account_unit, str) or not account_unit.strip()
            or symbol not in ('BTC/USDT', 'ETH/USDT') or timeframe != '5m'
            or type(minimum_return_samples) is not int or minimum_return_samples < 2
            or not isinstance(summary, Mapping) or not isinstance(equity, pd.DataFrame)
            or not isinstance(trades, pd.DataFrame) or equity.empty):
        raise ValueError('Explicit BTC/ETH completed legacy run required')
    cost = summary.get('fees_slippage_model')
    if not isinstance(cost, Mapping) or set(cost) != {'fee_bps', 'slippage_bps'}:
        raise ValueError('Explicit fee and slippage assumptions required')
    fee_bps = _finite(cost['fee_bps'], 'fee bps')
    slip_bps = _finite(cost['slippage_bps'], 'slippage bps')
    if min(fee_bps, slip_bps) < 0 or max(fee_bps, slip_bps) >= 10000:
        raise ValueError('Nonnegative execution costs required')
    parameters = summary.get('parameters')
    if not isinstance(parameters, Mapping) or not parameters:
        raise ValueError('Completed run parameters are required')
    try:
        parameter_bytes = json.dumps(parameters, sort_keys=True, separators=(',', ':'),
                                     allow_nan=False).encode('utf-8')
    except (TypeError, ValueError) as exc:
        raise ValueError('Completed run parameters must be deterministic') from exc
    if (not np.isclose(_finite(parameters.get('fee_bps'), 'parameter fee bps'), fee_bps)
            or not np.isclose(_finite(parameters.get('slippage_bps'), 'parameter slippage bps'),
                              slip_bps)):
        raise ValueError('Run parameters and execution costs disagree')
    parameter_sha256 = sha256(parameter_bytes).hexdigest()
    if not {'timestamp', 'equity'}.issubset(equity):
        raise ValueError('Equity curve lacks required fields')
    times = _times(equity['timestamp'], 'Equity curve')
    values = np.asarray(equity['equity'], dtype=float)
    if not np.isfinite(values).all() or (values <= 0).any():
        raise ValueError('Positive finite equity curve required')
    start = _finite(summary.get('start_equity'), 'start equity', positive=True)
    end = _finite(summary.get('end_equity'), 'end equity', positive=True)
    if not np.isclose(values[0], start) or not np.isclose(values[-1], end):
        raise ValueError('Summary and equity curve do not reconcile')
    if not np.isclose((end/start-1)*100,
                      _finite(summary.get('total_return_pct'), 'total return')):
        raise ValueError('Reported total return does not reconcile')
    fields = {'trade_id', 'entry_time', 'exit_time', 'side', 'regime', 'reason', 'pnl', 'gross',
              'fees', 'funding'}
    if not fields.issubset(trades) or trades.trade_id.duplicated().any():
        raise ValueError('Completed trade schema or identities are invalid')
    if len(trades) != summary.get('trades'):
        raise ValueError('Trade count does not reconcile')
    if len(trades):
        entered = pd.to_datetime(trades.entry_time, utc=True, errors='raise')
        exited = pd.to_datetime(trades.exit_time, utc=True, errors='raise')
        if (((entered > exited) | (entered < times[0]) | (exited > times[-1])).any()
                or not trades.side.isin(('LONG', 'SHORT')).all()
                or trades.trade_id.isna().any() or trades.reason.isna().any()):
            raise ValueError('Trade timestamps fall outside the evaluation window')
    pnl = np.asarray(trades.pnl, dtype=float)
    gross = np.asarray(trades.gross, dtype=float)
    fees = np.asarray(trades.fees, dtype=float)
    funding = np.asarray(trades.funding, dtype=float)
    if (not all(np.isfinite(values).all() for values in (pnl, gross, fees, funding))
            or (fees < 0).any() or not np.allclose(pnl, gross-fees+funding)):
        raise ValueError('Trade P&L or costs do not reconcile')
    net = float(pnl.sum())
    if not np.isclose(start+net, end, rtol=1e-9, atol=1e-7):
        raise ValueError('Completed trade P&L does not match ending equity')
    fills, settlements = equity.attrs.get('fills'), equity.attrs.get('funding')
    if not isinstance(fills, list) or not isinstance(settlements, list):
        raise ValueError('Complete fill and funding audit records are required')
    if (not np.isclose(sum(_finite(row['fee'], 'fill fee') for row in fills), float(fees.sum()))
            or not np.isclose(sum(_finite(row['cash'], 'funding cash') for row in settlements),
                              float(funding.sum()))):
        raise ValueError('Fill/funding audit and trade costs do not reconcile')
    events = equity.attrs.get('risk_equity')
    duration, unrecovered = _drawdown_duration(events)
    if not np.isclose(_finite(events[-1]['equity'], 'final risk equity'), end):
        raise ValueError('Final risk-equity event does not match ending equity')
    drawdowns = np.asarray([_finite(row['drawdown'], 'drawdown') for row in events])
    event_dd = float(drawdowns.min()*100)
    if not np.isclose(event_dd, _finite(summary.get('max_drawdown_pct'), 'max drawdown')):
        raise ValueError('Event drawdown and summary disagree')
    sharpe, sortino = _risk_adjusted(equity, minimum_return_samples)
    wins, losses = int((pnl > 0).sum()), int((pnl < 0).sum())
    win_cash = float(pnl[pnl > 0].sum())
    loss_cash = float(-pnl[pnl < 0].sum())
    profit_factor = (OptionalMetric(win_cash/loss_cash) if loss_cash > 0 else
                     OptionalMetric(None, 'NO_LOSING_TRADES'))
    if len(pnl) and summary.get('profit_factor') is not None and not np.isclose(
            profit_factor.value, float(summary['profit_factor'])):
        raise ValueError('Legacy profit factor does not reconcile')
    attribution = []
    details = tuple(TradeAttribution(str(row.trade_id), pd.Timestamp(row.entry_time),
                                     pd.Timestamp(row.exit_time), str(row.side),
                                     None if pd.isna(row.regime) else str(row.regime),
                                     str(row.reason), float(row.gross), float(row.fees),
                                     float(row.funding), float(row.pnl))
                    for row in trades.itertuples(index=False))
    for dimension, values_by_trade in (('asset', [symbol]*len(trades)),
                                       ('timeframe', [timeframe]*len(trades)),
                                       ('regime', trades.regime.tolist()),
                                       ('side', trades.side.tolist())):
        for label in sorted(set(values_by_trade), key=str):
            mask = np.asarray([value == label for value in values_by_trade])
            attribution.append(Attribution(dimension, str(label), int(mask.sum()),
                                           float(pnl[mask].sum()), float(gross[mask].sum()),
                                           float(fees[mask].sum()), float(funding[mask].sum())))
    unavailable = ['SIGNAL_CATEGORY_NOT_RECORDED', 'SLIPPAGE_CASH_NOT_SEPARATELY_RECORDED']
    if summary.get('funding_mode') != 'explicit_events':
        unavailable.append('FUNDING_SETTLEMENT_NOT_TESTED')
    elif summary.get('funding_coverage') != 'verified':
        unavailable.append('FUNDING_COVERAGE_UNVERIFIED')
    if summary.get('candle_availability') != 'validated_timestamps':
        unavailable.append('CANDLE_AVAILABILITY_ASSUMED_AT_CLOSE')
    if not summary.get('observation_availability'):
        unavailable.append('OPTIONAL_OBSERVATION_PROVENANCE_MISSING')
    else:
        unavailable.extend(f'{name.upper()}_NOT_PROVIDED' for name, state in
                           sorted(summary['observation_availability'].items()) if state == 'not_provided')
        unavailable.extend(f'{name.upper()}_ASSUMED_CANDLE_CLOSE' for name, state in
                           sorted(summary['observation_availability'].items())
                           if state == 'assumed_candle_close')
    return ValidationReport(source_id, symbol, timeframe, account_unit, parameter_sha256,
                            fee_bps, slip_bps, times[0], times[-1], start, end,
                            (end/start-1)*100, event_dd, duration, unrecovered, len(pnl),
                            wins, losses, len(pnl)-wins-losses,
                            OptionalMetric(float(pnl.mean())) if len(pnl) else
                            OptionalMetric(None, 'NO_COMPLETED_TRADES'), profit_factor,
                            OptionalMetric(wins/len(pnl)*100) if len(pnl) else
                            OptionalMetric(None, 'NO_COMPLETED_TRADES'),
                            sharpe, sortino, float(gross.sum()), float(fees.sum()),
                            float(funding.sum()), net, details, tuple(attribution), tuple(unavailable),
                            ('LEGACY_STRATEGY_ONLY', 'EXPLICIT_FEE_AND_SLIPPAGE_BPS',
                             'NEXT_OPEN_STOP_FIRST_ZERO_LATENCY',
                             'FUNDING_ONLY_IF_EXPLICIT_EVENTS', 'NO_REAL_FILL_VALIDATION',
                             f'MIN_RETURN_SAMPLES_{minimum_return_samples}'))


def compare_benchmark(report: ValidationReport, benchmark_equity: pd.DataFrame, *,
                      source_id: str, account_unit: str,
                      cost_provenance: str) -> BenchmarkComparison:
    """Compare with an independently supplied, already-costed same-unit benchmark."""
    if (not isinstance(report, ValidationReport) or not isinstance(benchmark_equity, pd.DataFrame)
            or not isinstance(source_id, str) or not source_id.strip()
            or not isinstance(cost_provenance, str) or not cost_provenance.strip()
            or account_unit != report.account_unit
            or not {'timestamp', 'available_at', 'equity'}.issubset(benchmark_equity)
            or len(benchmark_equity) < 2):
        raise ValueError('Explicit same-unit costed benchmark equity required')
    times = _times(benchmark_equity['timestamp'], 'Benchmark curve')
    availability = pd.to_datetime(benchmark_equity['available_at'], utc=True, errors='raise')
    if (availability.isna().any() or (availability < times).any()
            or (availability > report.last_at).any()):
        raise ValueError('Benchmark values lack causal availability timestamps')
    if times[0] != report.first_at or times[-1] != report.last_at:
        raise ValueError('Benchmark period must exactly match strategy period')
    values = np.asarray(benchmark_equity['equity'], dtype=float)
    if not np.isfinite(values).all() or (values <= 0).any():
        raise ValueError('Positive finite benchmark equity required')
    benchmark_return = float((values[-1]/values[0]-1)*100)
    return BenchmarkComparison(source_id, account_unit, cost_provenance,
                               benchmark_return, report.net_return_pct,
                               report.net_return_pct-benchmark_return,
                               times[0], times[-1])
