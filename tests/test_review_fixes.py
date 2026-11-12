"""Regression cases from the independent M0-M2 review."""
import sys
sys.dont_write_bytecode = True

import unittest
import numpy as np
import pandas as pd

from backtest import run
from strategy import Params, Strategy
from trading.data import BAR, validate
from trading.ledger import Halts, Ledger
from test_core import Stub, candles


class OneEntry(Stub):
    def __init__(self, raw, side='LONG'):
        super().__init__(side)
        self.first_close = raw.timestamp.iloc[0] + BAR

    def signal(self, row):
        return super().signal(row) if row.name == self.first_close else ('NONE', 0, None)


class ReviewFixes(unittest.TestCase):
    def test_marketable_adjusted_stop_uses_attainable_price(self):
        for side, bar, expected in (
            ('LONG', [100, 100.02, 100, 100.01], 100.015),
            ('SHORT', [100, 100, 99.98, 99.99], 99.985),
        ):
            with self.subTest(side=side):
                x = candles(2)
                x.loc[1, ['open', 'high', 'low', 'close']] = bar
                summary, equity, trades = run(
                    x, Params(atr_stop_mult=.01, fee_bps=0, slippage_bps=0), OneEntry(x, side))
                fills = equity.attrs['fills']
                self.assertEqual(len(fills), 3)
                self.assertAlmostEqual(fills[-1]['price'], expected)
                self.assertLessEqual(fills[-1]['price'], x.iloc[1].high)
                self.assertGreaterEqual(fills[-1]['price'], x.iloc[1].low)
                self.assertAlmostEqual(trades.iloc[0].pnl, .6)
                self.assertAlmostEqual(summary['end_equity'], 2000.6)

    def test_two_targets_and_ambiguous_adjusted_stop_both_sides(self):
        for side, clean, ambiguous in (
            ('LONG', [101, 105, 100.5, 104], [101, 105, 99, 100]),
            ('SHORT', [99, 99.5, 95, 96], [99, 101, 95, 100]),
        ):
            for bar, reason in ((clean, 'TP2'), (ambiguous, 'STOP_AFTER_TP1')):
                with self.subTest(side=side, reason=reason):
                    x = candles(3)
                    x.loc[2, ['open', 'high', 'low', 'close']] = bar
                    summary, equity, trades = run(x, Params(), OneEntry(x, side))
                    self.assertEqual(trades.iloc[0].reason, reason)
                    self.assertEqual(equity.attrs['fills'][1]['kind'], 'TP1')
                    self.assertAlmostEqual(summary['end_equity'], 2000 + trades.pnl.sum())

    def test_marketable_adjusted_stop_slippage_and_fees(self):
        for side, bar, entry_price, exit_price, direction in (
            ('LONG', [100, 100.09, 100, 100.08], 100.01, 100.0749915, 1),
            ('SHORT', [100, 100, 99.91, 99.92], 99.99, 99.9249915, -1),
        ):
            with self.subTest(side=side):
                x = candles(2)
                x.loc[1, ['open', 'high', 'low', 'close']] = bar
                summary, equity, trades = run(
                    x, Params(atr_stop_mult=.05, fee_bps=4, slippage_bps=1), OneEntry(x, side))
                fills = equity.attrs['fills']
                self.assertEqual(len(fills), 3)
                self.assertAlmostEqual(fills[0]['price'], entry_price)
                for fill in fills[1:]:
                    self.assertAlmostEqual(fill['price'], exit_price)
                qty = fills[0]['qty']
                fees = qty * (entry_price + exit_price) * .0004
                net = direction * qty * (exit_price - entry_price) - fees
                self.assertAlmostEqual(trades.iloc[0].fees, fees)
                self.assertAlmostEqual(trades.iloc[0].pnl, net)
                self.assertAlmostEqual(summary['end_equity'], 2000 + net)

    def test_gap_stops_with_funding_both_sides(self):
        for side, bar, direction in (
            ('LONG', [90, 91, 89, 90], 1), ('SHORT', [110, 111, 109, 110], -1)
        ):
            with self.subTest(side=side):
                x = candles(3)
                x.loc[2, ['open', 'high', 'low', 'close']] = bar
                x['funding_event_rate'] = [np.nan, np.nan, .01]
                summary, equity, trades = run(x, Params(), OneEntry(x, side))
                event = equity.attrs['funding'][0]
                self.assertAlmostEqual(event['cash'], -direction * event['qty'] * bar[0] * .01)
                self.assertAlmostEqual(trades.iloc[0].exit_price, bar[0] * (1 - direction * .0002))
                self.assertAlmostEqual(summary['end_equity'], 2000 + trades.pnl.sum())

    def test_reported_drawdown_matches_risk_events(self):
        x = candles(3)
        x.loc[2, ['open', 'high', 'low', 'close']] = [200, 200, 99, 100]
        summary, equity, _ = run(x, Params(fee_bps=0, slippage_bps=0), OneEntry(x))
        self.assertTrue(summary['halted'])
        self.assertLess(summary['max_drawdown_pct'], -5)
        events = pd.DataFrame(equity.attrs['risk_equity'])
        expected = (events.equity / events.equity.cummax() - 1).min() * 100
        self.assertAlmostEqual(summary['max_drawdown_pct'], expected)
        self.assertEqual(summary['close_max_drawdown_pct'], 0)
        self.assertTrue(events.sequence.is_unique)

    def test_boundary_gap_counts_against_new_period(self):
        for side, bar in (('LONG', [10, 11, 9, 10]), ('SHORT', [190, 191, 189, 190])):
            with self.subTest(side=side):
                x = candles(3)
                x['timestamp'] = pd.date_range('2025-01-05 23:50', periods=3, freq='5min', tz='UTC')
                x.loc[2, ['open', 'high', 'low', 'close']] = bar
                summary, equity, _ = run(
                    x, Params(fee_bps=0, slippage_bps=0, max_drawdown=.5), OneEntry(x, side))
                self.assertAlmostEqual(summary['total_return_pct'], -12.5)
                self.assertTrue(summary['halted'])
                events = pd.DataFrame(equity.attrs['risk_equity'])
                self.assertTrue(events.iloc[-1].daily_halt)
                self.assertTrue(events.iloc[-1].weekly_halt)
                self.assertFalse(events.iloc[-1].hard_halt)

    def test_daily_reset_preserves_weekly_halt(self):
        h = Halts(Params(max_drawdown=.5))
        t = pd.Timestamp('2025-01-06 12:00', tz='UTC')
        h.update(t, 2000)
        h.update(t, 1930)
        self.assertTrue(h.update(t + pd.Timedelta(days=1), 1930))
        self.assertFalse(h.daily)
        self.assertTrue(h.weekly)
        self.assertFalse(h.update(t + pd.Timedelta(days=7), 1930))

    def test_entry_costs_are_observed_before_close(self):
        x = candles(2)
        x.loc[1, ['open', 'high', 'low', 'close']] = [100, 100.9, 99.9, 100.8]
        _, equity, _ = run(x, Params(fee_bps=100, slippage_bps=0, daily_loss_limit=.0005), OneEntry(x))
        entry = next(e for e in equity.attrs['risk_equity'] if e['kind'] == 'ENTRY')
        self.assertTrue(entry['daily_halt'])
        self.assertLess(entry['equity'], 1999)

    def test_immediately_triggered_entry_stop_stays_within_planned_budget(self):
        for side in ('LONG', 'SHORT'):
            with self.subTest(side=side):
                x = candles(2)
                params = Params(atr_stop_mult=.01, max_leverage=10)
                summary, _, trades = run(x, params, OneEntry(x, side))
                self.assertEqual(trades.iloc[0].reason, 'STOP')
                self.assertGreaterEqual(trades.iloc[0].pnl, -5.00000001)
                self.assertAlmostEqual(trades.iloc[0].risk_cash, -trades.iloc[0].pnl)
                self.assertAlmostEqual(summary['end_equity'], 2000 + trades.pnl.sum())

    def test_financial_validation_is_atomic(self):
        for bad in (np.nan, np.inf, -np.inf, 0, -1):
            for field in ('price', 'qty', 'stop'):
                with self.subTest(field=field, bad=bad):
                    ledger = Ledger(1000)
                    args = dict(side='LONG', price=100, qty=1, stop=90, ts=None)
                    args[field] = bad
                    with self.assertRaises(ValueError): ledger.open(**args)
                    self.assertEqual(ledger.balance, 1000)
                    self.assertIsNone(ledger.position)
                    self.assertEqual(ledger.fills, [])
        ledger = Ledger(1000)
        ledger.open('LONG', 100, 1, 90, None)
        for bad in (np.nan, np.inf, -1, 0):
            with self.assertRaises(ValueError): ledger.close(bad, 100, None, 'END')
            with self.assertRaises(ValueError): ledger.close(1, bad, None, 'END')
        with self.assertRaises(ValueError): ledger.fund(np.nan, 100, None)
        with self.assertRaises(ValueError): ledger.equity(np.nan)
        self.assertEqual(ledger.position.qty, 1)
        self.assertEqual(ledger.balance, 1000)

    def test_execution_parameters_have_admissible_ranges(self):
        for value in (10000, 20000):
            for key in ('fee_bps', 'slippage_bps'):
                with self.subTest(key=key, value=value):
                    with self.assertRaises(ValueError): Params(**{key: value}).validate()
        for args in ((1000, np.nan, 0), (1000, 0, 1), (0, 0, 0)):
            with self.assertRaises(ValueError): Ledger(*args)

    def test_legacy_fields_and_trade_fill_links(self):
        summary, equity, trades = run(candles(4), Params(max_bars=1), Stub())
        self.assertEqual(summary['fees_slippage_model'], {'fee_bps': 4, 'slippage_bps': 2})
        fills = pd.DataFrame(equity.attrs['fills'])
        self.assertTrue(fills.fill_id.is_unique)
        self.assertTrue(fills.order_id.is_unique)
        self.assertTrue(trades.trade_id.is_unique)
        entry_equity = 2000
        for trade in trades.itertuples():
            own = fills[fills.trade_id == trade.trade_id]
            self.assertAlmostEqual(own.gross.sum() - own.fee.sum(), trade.pnl)
            self.assertAlmostEqual(own.iloc[0].qty, own.iloc[1:].qty.sum())
            self.assertEqual(own.iloc[-1].remaining_qty, 0)
            self.assertAlmostEqual(trade.pnl_pct, trade.pnl / entry_equity)
            entry_equity += trade.pnl

    def test_funding_after_partial_exit_and_negative_rate(self):
        for side, first, last in (
            ('LONG', [101, 103, 100.5, 102], [102, 103, 101, 102]),
            ('SHORT', [99, 99.5, 97, 98], [98, 99, 97, 98]),
        ):
            with self.subTest(side=side):
                x = candles(4)
                x.loc[2, ['open', 'high', 'low', 'close']] = first
                x.loc[3, ['open', 'high', 'low', 'close']] = last
                x['funding_event_rate'] = [np.nan, .1, .01, -.01]
                summary, equity, trades = run(x, Params(), OneEntry(x, side))
                events = equity.attrs['funding']
                self.assertEqual(len(events), 2)  # Entry-boundary settlement excluded.
                self.assertAlmostEqual(events[1]['qty'], events[0]['qty'] / 2)
                self.assertLess(events[0]['cash'] * events[1]['cash'], 0)
                self.assertAlmostEqual(trades.iloc[0].funding, sum(e['cash'] for e in events))
                self.assertEqual(events[0]['trade_id'], trades.iloc[0].trade_id)
                self.assertAlmostEqual(summary['end_equity'], 2000 + trades.pnl.sum())

    def test_empty_funding_column_does_not_claim_tested_coverage(self):
        x = candles(3)
        x['funding_event_rate'] = np.nan
        summary, _, _ = run(x, Params(), OneEntry(x))
        self.assertEqual(summary['funding_mode'], 'not_tested')
        self.assertEqual(summary['funding_event_observations'], 0)
        x.loc[2, 'funding_event_rate'] = 0
        summary, _, _ = run(x, Params(), OneEntry(x))
        self.assertEqual(summary['funding_event_observations'], 1)
        self.assertEqual(summary['funding_coverage'], 'unverified')

    def test_unfinished_candles_rejected(self):
        x = candles(3)
        x['is_closed'] = [True, True, False]
        with self.assertRaises(ValueError): validate(x)
        x = candles(3)
        with self.assertRaises(ValueError): validate(x, as_of=x.timestamp.iloc[-1])

    def test_late_external_observation_rejected(self):
        x = candles(3)
        x['news_sentiment'] = .5
        x['news_sentiment_available_at'] = x.timestamp + BAR
        validate(x)
        x.loc[1, 'news_sentiment_available_at'] += pd.Timedelta(seconds=1)
        with self.assertRaises(ValueError): validate(x)
        x.loc[1, 'news_sentiment_available_at'] = pd.NaT
        with self.assertRaises(ValueError): validate(x)

    def test_late_candle_availability_rejected(self):
        x = candles(3)
        x['available_at'] = x.timestamp + BAR + pd.Timedelta(seconds=1)
        with self.assertRaises(ValueError): validate(x)

    def test_settlement_values_are_not_predictor_columns(self):
        class Inspect(Stub):
            def prepare(self, df):
                if 'funding_event_rate' in df:
                    raise AssertionError('Settlement data exposed as signal features')
                return super().prepare(df)
        x = candles(3)
        x['funding_event_rate'] = [np.nan, np.nan, .01]
        run(x, Params(), Inspect())

    def test_warmed_features_and_decisions_are_prefix_invariant(self):
        n = 9700
        x = candles(n)
        x['close'] = 100 + .7 * np.sin(np.arange(n) / 30)
        x['open'] = x.close.shift().fillna(100)
        x['high'] = x[['open', 'close']].max(axis=1) + .1
        x['low'] = x[['open', 'close']].min(axis=1) - .1
        strategy = Strategy(Params(min_confidence=0))
        full = strategy.prepare(x)
        prefix = strategy.prepare(x.iloc[:9653])
        pd.testing.assert_frame_equal(full.iloc[:9653], prefix)
        self.assertTrue(np.isfinite(prefix.iloc[-1].h4_ema200))
        before = [strategy.signal(row) for _, row in prefix.iloc[-53:].iterrows()]
        after = [strategy.signal(row) for _, row in full.iloc[9600:9653].iterrows()]
        self.assertEqual(before, after)
        self.assertTrue(any(side != 'NONE' for side, _, _ in before))

    def test_known_late_settlement_rejected(self):
        x = candles(3)
        x['funding_event_rate'] = [np.nan, np.nan, .01]
        x['funding_event_rate_available_at'] = x.timestamp + BAR
        with self.assertRaises(ValueError): validate(x)

    def test_empty_evaluation_window_rejected(self):
        x = candles(3)
        with self.assertRaises(ValueError):
            run(x, Params(), OneEntry(x), start=x.timestamp.iloc[-1] + BAR)


if __name__ == '__main__':
    unittest.main()
