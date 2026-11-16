import sys
sys.dont_write_bytecode = True

import unittest

import numpy as np
import pandas as pd

import trading.walk_forward as wf_module
from strategy import Params
from test_core import candles
from trading.walk_forward import (WalkForwardConfig, parameter_id, plan_windows,
                                  sensitivity_in_sample, walk_forward)


def history(n=100):
    bars = candles(n)
    return bars, bars.timestamp.iloc[-1]+pd.Timedelta(minutes=5)


def trading_history(n=1480, amp=2.5, period=96):
    """Non-flat 5m history where the default strategy completes real trades.

    Shortcuts: one fixed sine shape (no stochastic component) — swap for a
    seeded random walk if more path diversity is ever needed.
    """
    i = np.arange(n)
    close = 100 + amp*np.sin(2*np.pi*i/period)
    open_ = np.roll(close, 1)
    open_[0] = close[0]
    volume = 10.0 + 6.0*((i % 5) == 0)
    return pd.DataFrame({
        'timestamp': pd.date_range('2025-01-01', periods=n, freq='5min', tz='UTC'),
        'open': open_,
        'high': np.maximum(open_, close)+0.2,
        'low': np.minimum(open_, close)-0.2,
        'close': close,
        'volume': volume,
    })


class WalkForwardTests(unittest.TestCase):
    def test_rolling_windows_purge_embargo_and_disjoint_oos(self):
        bars, at = history()
        config = WalkForwardConfig(30, 20, 20, purge_bars=5, embargo_bars=3)
        windows = plan_windows(bars, config, as_of=at)
        first, second = windows[:2]
        self.assertEqual((first.train_start_index, first.train_end_index,
                          first.test_start_index, first.test_end_index), (0, 24, 33, 52))
        self.assertEqual((second.train_start_index, second.train_end_index,
                          second.test_start_index), (20, 44, 53))
        self.assertEqual(first.purge_start, bars.timestamp.iloc[25])
        self.assertEqual(first.embargo_start, bars.timestamp.iloc[30])
        self.assertEqual(first.test_end, bars.timestamp.iloc[52]+pd.Timedelta(minutes=5))
        self.assertLess(first.test_end, second.test_end)
        self.assertLessEqual(first.test_end, second.test_start)

    def test_expanding_and_fixed_parameter_fold_reports(self):
        bars, at = history()
        config = WalkForwardConfig(30, 20, 20, expanding=True)
        result = walk_forward(bars, Params(), config, as_of=at,
                              source_id='fixture', symbol='BTC/USDT', account_unit='USDT')
        self.assertGreaterEqual(len(result.folds), 2)
        self.assertEqual(result.folds[1].window.train_start_index, 0)
        self.assertEqual(result.parameter_id, parameter_id(Params()))
        for fold in result.folds:
            self.assertEqual(fold.out_of_sample.first_at, fold.window.test_start)
            self.assertEqual(fold.out_of_sample.last_at, fold.window.test_end)
            self.assertEqual(fold.train.last_at, fold.window.train_end)
            self.assertEqual(fold.out_of_sample.start_equity, Params().initial_capital)
            self.assertIn('NO_OUT_OF_SAMPLE_PARAMETER_SELECTION', result.limitations)

    def test_future_mutation_does_not_change_prior_fold(self):
        bars, at = history()
        config = WalkForwardConfig(30, 20, 20, purge_bars=2, embargo_bars=2)
        original = walk_forward(bars, Params(), config, as_of=at,
                                source_id='fixture', symbol='ETH/USDT', account_unit='USDT')
        changed = bars.copy()
        changed.loc[80:, ['open', 'high', 'low', 'close']] *= 1.1
        modified = walk_forward(changed, Params(), config, as_of=at,
                                source_id='fixture', symbol='ETH/USDT', account_unit='USDT')
        self.assertEqual(modified.folds[0], original.folds[0])
        self.assertNotEqual(modified.input_sha256, original.input_sha256)

    def test_sensitivity_is_train_only_and_identifies_explicit_costs(self):
        bars, at = history()
        window = plan_windows(bars, WalkForwardConfig(40, 20, 20), as_of=at)[0]
        variants = (Params(fee_bps=4, slippage_bps=2),
                    Params(fee_bps=8, slippage_bps=5))
        cases = sensitivity_in_sample(bars, variants, window, as_of=at,
                                      source_id='fixture', symbol='BTC/USDT', account_unit='USDT')
        self.assertNotEqual(cases[0].parameter_id, cases[1].parameter_id)
        self.assertEqual(tuple(case.in_sample.last_at for case in cases),
                         (window.train_end, window.train_end))
        changed = bars.copy()
        changed.loc[window.test_start_index:, 'volume'] = 999
        later = sensitivity_in_sample(changed, variants, window, as_of=at,
                                      source_id='fixture', symbol='BTC/USDT', account_unit='USDT')
        self.assertEqual(cases, later)
        with self.assertRaises(ValueError):
            sensitivity_in_sample(bars, (variants[0], variants[0]), window,
                                  as_of=at, source_id='fixture', symbol='BTC/USDT',
                                  account_unit='USDT')

    def test_incomplete_data_invalid_windows_and_lookahead_are_rejected(self):
        bars, at = history(50)
        with self.assertRaises(ValueError):
            plan_windows(bars, WalkForwardConfig(40, 20, 20), as_of=at)
        with self.assertRaises(ValueError):
            WalkForwardConfig(30, 20, 20, purge_bars=30)
        with self.assertRaises(ValueError):
            WalkForwardConfig(30, 20, 10)
        with self.assertRaises(ValueError):
            plan_windows(bars, WalkForwardConfig(20, 10, 10),
                         as_of=at-pd.Timedelta(seconds=1))
        with self.assertRaises(ValueError):
            walk_forward(bars, Params(), WalkForwardConfig(20, 10, 10),
                         as_of=None, source_id='fixture', symbol='BTC/USDT',
                         account_unit='USDT')


class WalkForwardTradingFixtureTests(unittest.TestCase):
    """F02: bounded prefixes, future-data isolation and determinism with real trades."""

    @classmethod
    def setUpClass(cls):
        cls.bars = trading_history()
        cls.at = cls.bars.timestamp.iloc[-1]+pd.Timedelta(minutes=5)
        cls.params = Params(min_confidence=55, fee_bps=4, slippage_bps=2,
                            daily_loss_limit=.9, weekly_loss_limit=.9,
                            max_drawdown=.9)
        cls.config = WalkForwardConfig(1360, 60, 60)
        cls.source_id = 'wf-trading-fixture'
        cls.calls = []
        original_run = wf_module.run

        def spy(prefix, params, *, start, as_of):
            cls.calls.append(dict(length=len(prefix), first=prefix.index[0],
                                  last=prefix.index[-1], start=start, as_of=as_of))
            return original_run(prefix, params, start=start, as_of=as_of)

        wf_module.run = spy
        try:
            cls.report = walk_forward(cls.bars, cls.params, cls.config, as_of=cls.at,
                                      source_id=cls.source_id, symbol='BTC/USDT',
                                      account_unit='USDT')
        finally:
            wf_module.run = original_run

    def test_train_and_oos_runs_are_bounded_prefixes_with_completed_trades(self):
        self.assertEqual(len(self.report.folds), 2)
        self.assertEqual(len(self.calls), 2*len(self.report.folds))
        for fold, train_call, oos_call in zip(self.report.folds,
                                              self.calls[0::2], self.calls[1::2]):
            window = fold.window
            # Strict chronological separation: no test bar inside the train window.
            self.assertLess(window.train_end_index, window.test_start_index)
            self.assertLess(window.test_end_index, len(self.bars))
            for call, last_index in ((train_call, window.train_end_index),
                                     (oos_call, window.test_end_index)):
                # Every evaluation is a prefix of history ending at its own boundary.
                self.assertEqual(call['length'], last_index+1)
                self.assertEqual(call['first'], self.bars.timestamp.iloc[0])
                self.assertEqual(call['last'], self.bars.timestamp.iloc[last_index])
                self.assertEqual(call['as_of'],
                                 self.bars.timestamp.iloc[last_index]
                                 + pd.Timedelta(minutes=5))
                self.assertLess(call['start'], call['last'])
            self.assertEqual(train_call['start'],
                             self.bars.timestamp.iloc[window.train_start_index])
            self.assertEqual(oos_call['start'],
                             self.bars.timestamp.iloc[window.test_start_index])
            self.assertGreater(oos_call['start'], train_call['last'])
            # Non-vacuous: the default strategy completes trades in every region.
            self.assertGreaterEqual(fold.train.trades, 1)
            self.assertGreaterEqual(fold.out_of_sample.trades, 1)

    def test_mutating_the_test_region_changes_only_out_of_sample_results(self):
        mutated = trading_history()
        # Fold 0's test region is rows [1360..1419]; its train must not see it.
        mutated.loc[1360:1419, ['open', 'high', 'low', 'close']] *= 1.1
        changed = walk_forward(mutated, self.params, self.config, as_of=self.at,
                               source_id=self.source_id, symbol='BTC/USDT',
                               account_unit='USDT')
        original_fold, changed_fold = self.report.folds[0], changed.folds[0]
        self.assertEqual(changed_fold.train, original_fold.train)
        self.assertNotEqual(changed_fold.out_of_sample, original_fold.out_of_sample)
        self.assertNotEqual(changed.input_sha256, self.report.input_sha256)

    def test_repeated_walk_forward_runs_are_identical(self):
        repeated = walk_forward(self.bars, self.params, self.config, as_of=self.at,
                                source_id=self.source_id, symbol='BTC/USDT',
                                account_unit='USDT')
        self.assertEqual(repeated, self.report)
        self.assertEqual(repeated.folds, self.report.folds)
        self.assertEqual(repeated.input_sha256, self.report.input_sha256)
        self.assertEqual(repeated.parameter_id, self.report.parameter_id)


if __name__ == '__main__':
    unittest.main()
