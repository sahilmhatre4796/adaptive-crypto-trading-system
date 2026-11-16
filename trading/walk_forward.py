"""M10 chronological, fixed-policy walk-forward evaluation of the legacy strategy."""
from __future__ import annotations

from dataclasses import asdict, dataclass
from hashlib import sha256
import json

import pandas as pd

from backtest import run
from strategy import Params
from trading.data import BAR, validate
from trading.validation import ValidationReport, evaluate_completed_run


@dataclass(frozen=True)
class WalkForwardConfig:
    train_bars: int
    test_bars: int
    step_bars: int
    purge_bars: int = 0
    embargo_bars: int = 0
    expanding: bool = False

    def __post_init__(self):
        for name in ('train_bars', 'test_bars', 'step_bars'):
            value = getattr(self, name)
            if type(value) is not int or value < 1:
                raise ValueError('Positive integer walk-forward window required')
        for name in ('purge_bars', 'embargo_bars'):
            value = getattr(self, name)
            if type(value) is not int or value < 0:
                raise ValueError('Nonnegative integer purge/embargo required')
        if (type(self.expanding) is not bool or self.purge_bars >= self.train_bars
                or self.step_bars < self.test_bars):
            raise ValueError('Invalid expanding or purged training window')


@dataclass(frozen=True)
class FoldWindow:
    fold: int
    train_start: pd.Timestamp
    train_end: pd.Timestamp
    purge_start: pd.Timestamp | None
    purge_end: pd.Timestamp | None
    embargo_start: pd.Timestamp | None
    embargo_end: pd.Timestamp | None
    test_start: pd.Timestamp
    test_end: pd.Timestamp
    train_start_index: int
    train_end_index: int
    test_start_index: int
    test_end_index: int


@dataclass(frozen=True)
class FoldResult:
    window: FoldWindow
    train: ValidationReport
    out_of_sample: ValidationReport
    parameter_id: str


@dataclass(frozen=True)
class WalkForwardReport:
    source_id: str
    symbol: str
    configuration: WalkForwardConfig
    parameter_id: str
    input_sha256: str
    folds: tuple[FoldResult, ...]
    limitations: tuple[str, ...]


@dataclass(frozen=True)
class SensitivityCase:
    parameter_id: str
    parameters: tuple[tuple[str, float], ...]
    in_sample: ValidationReport


def parameter_id(params: Params) -> str:
    if not isinstance(params, Params):
        raise ValueError('Explicit legacy strategy parameters required')
    params.validate()
    data = json.dumps(asdict(params), sort_keys=True, allow_nan=False,
                      separators=(',', ':')).encode('utf-8')
    return sha256(data).hexdigest()


def _validated(raw, *, as_of):
    if as_of is None:
        raise ValueError('Explicit historical data availability cutoff required')
    return validate(raw, as_of=as_of)


def _input_hash(data):
    normalized = data.reset_index(drop=True).to_csv(index=False, lineterminator='\n')
    return sha256(normalized.encode('utf-8')).hexdigest()


def _windows(data, config):
    timestamps = data.index
    base = 0
    fold = 0
    while True:
        train_start = 0 if config.expanding else base
        train_boundary = base+config.train_bars-1
        train_end = train_boundary-config.purge_bars
        test_start = train_boundary+config.embargo_bars+1
        test_end = test_start+config.test_bars-1
        if test_end >= len(timestamps):
            break
        purge_start = train_end+1 if config.purge_bars else None
        embargo_start = train_boundary+1 if config.embargo_bars else None
        yield FoldWindow(fold, timestamps[train_start], timestamps[train_end]+BAR,
                         timestamps[purge_start] if purge_start is not None else None,
                         timestamps[train_boundary]+BAR if purge_start is not None else None,
                         timestamps[embargo_start] if embargo_start is not None else None,
                         timestamps[test_start-1]+BAR if embargo_start is not None else None,
                         timestamps[test_start], timestamps[test_end]+BAR,
                         train_start, train_end, test_start, test_end)
        fold += 1
        base += config.step_bars


def plan_windows(raw, config: WalkForwardConfig, *, as_of) -> tuple[FoldWindow, ...]:
    if not isinstance(config, WalkForwardConfig):
        raise ValueError('Typed walk-forward configuration required')
    data = _validated(raw, as_of=as_of)
    windows = tuple(_windows(data, config))
    if not windows:
        raise ValueError('Insufficient completed candles for a full train/test fold')
    return windows


def _evaluate_prefix(data, params, *, first, last, symbol, account_unit, source_id):
    prefix = data.iloc[:last+1]
    summary, equity, trades = run(prefix, params, start=data.index[first],
                                  as_of=data.index[last]+BAR)
    return evaluate_completed_run(summary, equity, trades, source_id=source_id,
                                  symbol=symbol, account_unit=account_unit)


def walk_forward(raw, params: Params, config: WalkForwardConfig, *,
                 as_of, source_id: str, symbol: str, account_unit: str) -> WalkForwardReport:
    """Evaluate one locked policy; no OOS-driven fitting or inferred fills."""
    if not isinstance(config, WalkForwardConfig):
        raise ValueError('Typed walk-forward configuration required')
    pid = parameter_id(params)
    data = _validated(raw, as_of=as_of)
    windows = tuple(_windows(data, config))
    if not windows:
        raise ValueError('Insufficient completed candles for a full train/test fold')
    results = []
    for window in windows:
        train = _evaluate_prefix(data, params, first=window.train_start_index,
                                 last=window.train_end_index, symbol=symbol,
                                 account_unit=account_unit,
                                 source_id=f'{source_id}:fold-{window.fold}:train')
        test = _evaluate_prefix(data, params, first=window.test_start_index,
                                last=window.test_end_index, symbol=symbol,
                                account_unit=account_unit,
                                source_id=f'{source_id}:fold-{window.fold}:oos')
        results.append(FoldResult(window, train, test, pid))
    return WalkForwardReport(source_id, symbol, config, pid, _input_hash(data), tuple(results),
                             ('LEGACY_STRATEGY_ONLY', 'FRESH_CAPITAL_EACH_WINDOW',
                              'TRAIN_END_POSITIONS_FORCE_CLOSED',
                              'PURGED_AND_EMBARGOED_BARS_NOT_USED_AS_TRAIN_LABELS',
                              'NO_OUT_OF_SAMPLE_PARAMETER_SELECTION'))


def sensitivity_in_sample(raw, variants: tuple[Params, ...], window: FoldWindow, *,
                          as_of, source_id: str, symbol: str,
                          account_unit: str) -> tuple[SensitivityCase, ...]:
    """Report parameter and cost scenarios on a locked training window only."""
    if type(variants) is not tuple or not variants or not isinstance(window, FoldWindow):
        raise ValueError('Explicit variants and training window required')
    data = _validated(raw, as_of=as_of)
    if (window.train_end_index >= len(data) or data.index[window.train_start_index] != window.train_start
            or data.index[window.train_end_index]+BAR != window.train_end):
        raise ValueError('Training window no longer matches the source history')
    cases = []
    seen = set()
    for params in variants:
        pid = parameter_id(params)
        if pid in seen:
            raise ValueError('Duplicate sensitivity parameter scenario')
        seen.add(pid)
        report = _evaluate_prefix(data, params, first=window.train_start_index,
                                  last=window.train_end_index, symbol=symbol,
                                  account_unit=account_unit,
                                  source_id=f'{source_id}:sensitivity:{pid}')
        cases.append(SensitivityCase(pid, tuple(sorted(asdict(params).items())), report))
    return tuple(cases)
