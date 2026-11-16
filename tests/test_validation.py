import sys
sys.dont_write_bytecode = True

import math
import unittest

import pandas as pd

from backtest import run
from strategy import Params
from test_core import Stub, candles
from trading.validation import (PER_YEAR, _drawdown_duration, compare_benchmark,
                                evaluate_completed_run)


def completed(n=60, side='LONG'):
    bars = candles(n)
    summary, equity, trades = run(bars, Params(fee_bps=4, slippage_bps=2,
                                               daily_loss_limit=.9, weekly_loss_limit=.9,
                                               max_drawdown=.9), Stub(side),
                                  as_of=bars.timestamp.iloc[-1]+pd.Timedelta(minutes=5))
    return summary, equity, trades


def synthetic_completed_run():
    """Hand-built run with three trades (two winners) and complete audit records.

    PnL is 9, 7, -6 so expectancy is 10/3, profit factor is 16/6 and the
    max event drawdown is exactly -2% on a 1000 -> 1010 equity path.
    """
    stamps = pd.date_range('2025-02-01', periods=5, freq='5min', tz='UTC')
    equity = pd.DataFrame({'timestamp': stamps,
                           'equity': [1000.0, 1005.0, 1010.0, 989.8, 1010.0]})
    trades = pd.DataFrame([
        dict(trade_id='syn-1', entry_time=stamps[0], exit_time=stamps[1], side='LONG',
             regime='BULL', reason='STOP', gross=10.0, fees=1.0, funding=0.0, pnl=9.0),
        dict(trade_id='syn-2', entry_time=stamps[1], exit_time=stamps[2], side='SHORT',
             regime='BEAR', reason='STOP', gross=8.0, fees=1.0, funding=0.0, pnl=7.0),
        dict(trade_id='syn-3', entry_time=stamps[2], exit_time=stamps[4], side='LONG',
             regime='RANGE', reason='FORCE_CLOSE', gross=-5.0, fees=1.0, funding=0.0, pnl=-6.0),
    ])
    equity.attrs['fills'] = [{'fee': 1.0}, {'fee': 1.0}, {'fee': 1.0}]
    equity.attrs['funding'] = [{'cash': 0.0}]
    equity.attrs['risk_equity'] = [
        dict(risk_timestamp=stamp, equity=value, drawdown=drawdown)
        for stamp, value, drawdown in zip(stamps, [1000.0, 1005.0, 1010.0, 989.8, 1010.0],
                                          [0.0, 0.0, 0.0, -0.02, 0.0])]
    summary = dict(start_equity=1000.0, end_equity=1010.0, total_return_pct=1.0,
                   trades=3, max_drawdown_pct=-2.0, profit_factor=16/6,
                   fees_slippage_model={'fee_bps': 4.0, 'slippage_bps': 2.0},
                   parameters={'fee_bps': 4.0, 'slippage_bps': 2.0},
                   funding_mode='explicit_events', funding_coverage='verified',
                   candle_availability='validated_timestamps',
                   observation_availability={})
    return summary, equity, trades


class ValidationTests(unittest.TestCase):
    def test_drawdown_duration_starts_at_peak_and_marks_unrecovered_tail(self):
        start = pd.Timestamp('2025-01-01T00:00:00Z')
        events = [dict(risk_timestamp=start+pd.Timedelta(minutes=minute), equity=value)
                  for minute, value in ((0, 100), (5, 90), (15, 100), (20, 95))]
        duration, unrecovered = _drawdown_duration(events)
        self.assertEqual(duration, pd.Timedelta(minutes=15))
        self.assertTrue(unrecovered)

    def test_costed_completed_run_reconciles_and_attributes(self):
        summary, equity, trades = completed()
        report = evaluate_completed_run(summary, equity, trades, source_id='fixture',
                                        symbol='BTC/USDT', account_unit='USDT')
        self.assertEqual(report.trades, len(trades))
        self.assertAlmostEqual(report.net_pnl, report.gross_pnl-report.fees+report.funding)
        self.assertAlmostEqual(report.start_equity+report.net_pnl, report.end_equity)
        self.assertAlmostEqual(report.max_event_drawdown_pct, summary['max_drawdown_pct'])
        self.assertTrue(any(a.dimension == 'asset' and a.value == 'BTC/USDT'
                            for a in report.attribution))
        self.assertIn('SIGNAL_CATEGORY_NOT_RECORDED', report.unavailable)
        self.assertIn('FUNDING_SETTLEMENT_NOT_TESTED', report.unavailable)
        self.assertGreater(report.max_drawdown_duration, pd.Timedelta(0))
        self.assertEqual(len(report.trade_details), report.trades)
        self.assertAlmostEqual(sum(trade.net_pnl for trade in report.trade_details), report.net_pnl)
        self.assertEqual(report.fee_bps, 4)
        self.assertEqual(report.slippage_bps, 2)
        self.assertEqual(len(report.parameter_sha256), 64)

    def test_no_trade_or_constant_returns_are_explicitly_unavailable(self):
        summary, equity, trades = completed(side='NONE')
        report = evaluate_completed_run(summary, equity, trades, source_id='none',
                                        symbol='ETH/USDT', account_unit='USDT',
                                        minimum_return_samples=30)
        self.assertEqual(report.trades, 0)
        self.assertEqual(report.expectancy_cash.unavailable_reason, 'NO_COMPLETED_TRADES')
        self.assertEqual(report.profit_factor.unavailable_reason, 'NO_LOSING_TRADES')
        self.assertEqual(report.sharpe_5m.unavailable_reason, 'ZERO_RETURN_VARIANCE')
        self.assertEqual(report.sortino_5m.unavailable_reason, 'NO_DOWNSIDE_RETURNS')

    def test_short_or_irregular_curve_does_not_claim_risk_adjusted_metric(self):
        summary, equity, trades = completed(n=4, side='NONE')
        report = evaluate_completed_run(summary, equity, trades, source_id='short',
                                        symbol='BTC/USDT', account_unit='USDT')
        self.assertEqual(report.sharpe_5m.unavailable_reason,
                         'INSUFFICIENT_RETURN_SAMPLES')
        summary, equity, trades = completed(n=60, side='NONE')
        irregular = equity.copy()
        irregular.attrs = dict(equity.attrs)
        irregular.loc[2, 'timestamp'] += pd.Timedelta(seconds=1)
        report = evaluate_completed_run(summary, irregular, trades, source_id='irregular',
                                        symbol='BTC/USDT', account_unit='USDT',
                                        minimum_return_samples=30)
        self.assertEqual(report.sharpe_5m.unavailable_reason, 'IRREGULAR_EQUITY_CADENCE')

    def test_missing_costs_and_tampered_pnl_are_rejected(self):
        summary, equity, trades = completed()
        missing = dict(summary)
        missing.pop('fees_slippage_model')
        with self.assertRaises(ValueError):
            evaluate_completed_run(missing, equity, trades, source_id='missing',
                                   symbol='BTC/USDT', account_unit='USDT')
        altered = trades.copy()
        altered.loc[altered.index[0], 'pnl'] += 1
        with self.assertRaises(ValueError):
            evaluate_completed_run(summary, equity, altered, source_id='altered',
                                   symbol='BTC/USDT', account_unit='USDT')
        reversed_curve = equity.iloc[::-1].copy()
        reversed_curve.attrs = dict(equity.attrs)
        with self.assertRaises(ValueError):
            evaluate_completed_run(summary, reversed_curve, trades, source_id='reversed',
                                   symbol='BTC/USDT', account_unit='USDT')

    def test_tampered_assumptions_and_audit_artifacts_are_rejected(self):
        summary, equity, trades = completed()
        def evaluate(changed_summary=summary, changed_equity=equity, changed_trades=trades):
            return evaluate_completed_run(changed_summary, changed_equity, changed_trades,
                                          source_id='fixture', symbol='BTC/USDT',
                                          account_unit='USDT')
        altered_summary = dict(summary)
        altered_summary['total_return_pct'] += 1
        with self.assertRaises(ValueError):
            evaluate(changed_summary=altered_summary)
        altered_summary = dict(summary)
        altered_summary['parameters'] = dict(summary['parameters'], fee_bps=99)
        with self.assertRaises(ValueError):
            evaluate(changed_summary=altered_summary)
        altered_trades = trades.copy()
        altered_trades.loc[altered_trades.index[0], 'side'] = 'UNKNOWN'
        with self.assertRaises(ValueError):
            evaluate(changed_trades=altered_trades)
        altered_equity = equity.copy()
        altered_equity.attrs = dict(equity.attrs)
        altered_equity.attrs['fills'] = [dict(row) for row in equity.attrs['fills']]
        altered_equity.attrs['fills'][0]['fee'] += 1
        with self.assertRaises(ValueError):
            evaluate(changed_equity=altered_equity)
        altered_equity = equity.copy()
        altered_equity.attrs = dict(equity.attrs)
        altered_equity.attrs['risk_equity'] = [dict(row) for row in equity.attrs['risk_equity']]
        altered_equity.attrs['risk_equity'][-1]['equity'] += 1
        with self.assertRaises(ValueError):
            evaluate(changed_equity=altered_equity)

    def test_benchmark_requires_matching_period_unit_and_cost_provenance(self):
        summary, equity, trades = completed(side='NONE')
        report = evaluate_completed_run(summary, equity, trades, source_id='strategy',
                                        symbol='BTC/USDT', account_unit='USDT')
        benchmark = equity[['timestamp', 'equity']].copy()
        benchmark['available_at'] = benchmark['timestamp']
        benchmark.loc[benchmark.index[-1], 'equity'] *= 1.01
        compared = compare_benchmark(report, benchmark, source_id='independent-fixture',
                                     account_unit='USDT', cost_provenance='explicit costed fixture')
        self.assertAlmostEqual(compared.benchmark_return_pct, 1)
        self.assertAlmostEqual(compared.excess_return_percentage_points, -1)
        with self.assertRaises(ValueError):
            compare_benchmark(report, benchmark, source_id='fixture',
                              account_unit='INR', cost_provenance='explicit')
        with self.assertRaises(ValueError):
            compare_benchmark(report, benchmark.iloc[:-1], source_id='fixture',
                              account_unit='USDT', cost_provenance='explicit')
        with self.assertRaises(ValueError):
            compare_benchmark(report, benchmark, source_id='fixture',
                              account_unit='USDT', cost_provenance='')
        future = benchmark.copy()
        future.loc[future.index[0], 'available_at'] = report.last_at+pd.Timedelta(seconds=1)
        with self.assertRaises(ValueError):
            compare_benchmark(report, future, source_id='fixture',
                              account_unit='USDT', cost_provenance='explicit')

    def test_higher_execution_costs_reduce_net_result_in_fixed_fixture(self):
        bars = candles(4)
        summaries = []
        for fee, slip in ((0, 0), (8, 5)):
            summary, equity, trades = run(
                bars, Params(fee_bps=fee, slippage_bps=slip), Stub('LONG'),
                as_of=bars.timestamp.iloc[-1]+pd.Timedelta(minutes=5))
            summaries.append(evaluate_completed_run(summary, equity, trades,
                             source_id=f'cost-{fee}-{slip}', symbol='BTC/USDT',
                             account_unit='USDT'))
        self.assertGreater(summaries[0].net_pnl, summaries[1].net_pnl)
        self.assertGreater(summaries[1].fees, summaries[0].fees)

    def test_golden_sharpe_sortino_expectancy_and_profit_factor(self):
        """F01: hand-verified numbers for the fixed completed() fixture."""
        summary, equity, trades = completed()
        report = evaluate_completed_run(summary, equity, trades, source_id='golden',
                                        symbol='BTC/USDT', account_unit='USDT',
                                        minimum_return_samples=30)
        self.assertEqual(PER_YEAR, 365*24*12)
        self.assertAlmostEqual(report.sharpe_5m.value, -59.70271150156881, places=9)
        self.assertAlmostEqual(report.sortino_5m.value, -59.19459408721485, places=9)
        self.assertAlmostEqual(report.expectancy_cash.value, -0.31596705674887376, places=12)
        self.assertAlmostEqual(report.profit_factor.value, 0.0, places=12)
        # Independent recomputation from the raw equity curve with exact summation.
        values = [float(v) for v in equity['equity']]
        returns = [values[i+1]/values[i]-1 for i in range(len(values)-1)]
        mean = math.fsum(returns)/len(returns)
        std = math.sqrt(math.fsum((r-mean)**2 for r in returns)/(len(returns)-1))
        downside = math.sqrt(math.fsum(min(r, 0.0)**2 for r in returns)/len(returns))
        self.assertTrue(math.isclose(report.sharpe_5m.value,
                                     mean/std*math.sqrt(PER_YEAR), rel_tol=1e-9))
        self.assertTrue(math.isclose(report.sortino_5m.value,
                                     mean/downside*math.sqrt(PER_YEAR), rel_tol=1e-9))
        pnl = [float(v) for v in trades['pnl']]
        self.assertEqual(len(pnl), 1)  # fixture completes exactly one losing trade
        self.assertTrue(math.isclose(report.expectancy_cash.value,
                                     math.fsum(pnl)/len(pnl), rel_tol=1e-12))

    def test_hand_built_multi_trade_run_matches_golden_metrics(self):
        """F01: PnL 9, 7, -6 gives expectancy 10/3, PF 16/6, drawdown -2%."""
        summary, equity, trades = synthetic_completed_run()
        report = evaluate_completed_run(summary, equity, trades, source_id='synthetic',
                                        symbol='BTC/USDT', account_unit='USDT')
        self.assertEqual(report.trades, 3)
        self.assertEqual((report.wins, report.losses), (2, 1))
        self.assertTrue(math.isclose(report.expectancy_cash.value, 10/3, rel_tol=1e-12))
        self.assertTrue(math.isclose(report.profit_factor.value, 16/6, rel_tol=1e-12))
        self.assertTrue(math.isclose(report.win_rate_pct.value, 200/3, rel_tol=1e-9))
        self.assertAlmostEqual(report.net_pnl, 10.0, places=9)
        self.assertAlmostEqual(report.max_event_drawdown_pct, -2.0, places=9)
        self.assertAlmostEqual(report.fees, 3.0, places=9)
        self.assertEqual(report.funding, 0.0)

    def test_zero_negative_and_nonfinite_equity_are_rejected(self):
        """F03: the equity curve must stay positive and finite at every point."""
        summary, equity, trades = completed()
        for bad in (0.0, -5.0, float('nan'), float('inf')):
            with self.subTest(equity_value=bad):
                altered = equity.copy()
                altered.attrs = dict(equity.attrs)
                altered.loc[altered.index[len(altered)//2], 'equity'] = bad
                with self.assertRaisesRegex(
                        ValueError, 'Positive finite equity curve required'):
                    evaluate_completed_run(summary, altered, trades,
                                           source_id='equity-edge', symbol='BTC/USDT',
                                           account_unit='USDT')

    def test_benchmark_availability_must_not_precede_its_own_timestamp(self):
        """F03: benchmark values published before their observation are acausal."""
        summary, equity, trades = completed(side='NONE')
        report = evaluate_completed_run(summary, equity, trades, source_id='strategy',
                                        symbol='BTC/USDT', account_unit='USDT')
        benchmark = equity[['timestamp', 'equity']].copy()
        benchmark['available_at'] = benchmark['timestamp']
        early = benchmark.copy()
        early.loc[early.index[3], 'available_at'] = (
            early.loc[early.index[3], 'timestamp'] - pd.Timedelta(seconds=1))
        with self.assertRaisesRegex(
                ValueError, 'Benchmark values lack causal availability timestamps'):
            compare_benchmark(report, early, source_id='early',
                              account_unit='USDT', cost_provenance='explicit')
        missing = benchmark.copy()
        missing.loc[missing.index[3], 'available_at'] = pd.NaT
        with self.assertRaisesRegex(
                ValueError, 'Benchmark values lack causal availability timestamps'):
            compare_benchmark(report, missing, source_id='missing',
                              account_unit='USDT', cost_provenance='explicit')

    def test_funding_settlements_reconcile_and_tampering_is_rejected(self):
        """F03: settlements, trade funding and the report must agree three ways."""
        bars = candles(90)
        bars['funding_event_rate'] = [0.0001 if i % 12 == 0 else float('nan')
                                      for i in range(len(bars))]
        bars['funding_event_rate_available_at'] = bars.timestamp
        summary, equity, trades = run(bars, Params(fee_bps=4, slippage_bps=2,
                                                   daily_loss_limit=.9, weekly_loss_limit=.9,
                                                   max_drawdown=.9), Stub('LONG'),
                                      as_of=bars.timestamp.iloc[-1]+pd.Timedelta(minutes=5))
        report = evaluate_completed_run(summary, equity, trades, source_id='funding',
                                        symbol='BTC/USDT', account_unit='USDT')
        settlements = equity.attrs['funding']
        self.assertEqual(summary['funding_mode'], 'explicit_events')
        self.assertGreater(len(settlements), 0)
        settlement_cash = math.fsum(row['cash'] for row in settlements)
        trade_funding = float(trades['funding'].sum())
        self.assertNotEqual(trade_funding, 0.0)  # non-vacuous: positions were funded
        self.assertTrue(math.isclose(settlement_cash, trade_funding,
                                     rel_tol=1e-9, abs_tol=1e-12))
        self.assertTrue(math.isclose(report.funding, trade_funding,
                                     rel_tol=1e-9, abs_tol=1e-12))
        self.assertIn('FUNDING_COVERAGE_UNVERIFIED', report.unavailable)

        def evaluate(changed_equity=equity):
            return evaluate_completed_run(summary, changed_equity, trades,
                                          source_id='tampered', symbol='BTC/USDT',
                                          account_unit='USDT')

        # Settlement cash that no trade recorded must not pass.
        tampered = equity.copy()
        tampered.attrs = dict(equity.attrs)
        tampered.attrs['funding'] = [dict(row) for row in settlements]
        tampered.attrs['funding'][0]['cash'] += 0.01
        with self.assertRaisesRegex(
                ValueError, 'Fill/funding audit and trade costs do not reconcile'):
            evaluate(changed_equity=tampered)
        # Fill fees that no trade recorded must not pass either.
        tampered = equity.copy()
        tampered.attrs = dict(equity.attrs)
        tampered.attrs['fills'] = [dict(row) for row in equity.attrs['fills']]
        tampered.attrs['fills'][0]['fee'] += 0.01
        with self.assertRaisesRegex(
                ValueError, 'Fill/funding audit and trade costs do not reconcile'):
            evaluate(changed_equity=tampered)
        # Missing audit records are rejected before any reconciliation claim.
        missing = equity.copy()
        missing.attrs = dict(equity.attrs)
        missing.attrs.pop('funding')
        with self.assertRaisesRegex(
                ValueError, 'Complete fill and funding audit records are required'):
            evaluate(changed_equity=missing)


if __name__ == '__main__':
    unittest.main()
