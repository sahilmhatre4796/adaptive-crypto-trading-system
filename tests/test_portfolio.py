import sys
sys.dont_write_bytecode = True

from dataclasses import FrozenInstanceError, replace
from decimal import Decimal as D
import unittest

import pandas as pd

from trading.portfolio import Allocation, InstrumentSpec, Portfolio, Quote, identifier, number, wire

AT = pd.Timestamp('2025-01-01T00:00:00Z')


def spec(symbol='BTC/USDT', **changes):
    fields = dict(symbol=symbol, quote_currency='USDT', multiplier=1, quantity_step='.1', price_tick='.1',
                  min_quantity='.1', min_notional=1, max_leverage=10, correlation_group='fixture-crypto',
                  source='explicit-synthetic-contract', available_at=AT)
    fields.update(changes)
    return InstrumentSpec(**fields)


def quote(key='BTC/USDT', value=100, at=AT):
    return Quote(key, value, at, at, 'synthetic fixture, not a market observation')


def book():
    portfolio = Portfolio(as_of=AT)
    portfolio.observe(quote('USDT', 1), kind='FX', as_of=AT, event_id='fx0')
    portfolio.observe(quote(), kind='MARK', as_of=AT, event_id='mark0')
    return portfolio


def plan(portfolio, side='LONG', **changes):
    snap = portfolio.snapshot(as_of=AT)
    fields = dict(allocation_id='allocation', signal_id='signal', formation_id='formation', spec=spec(), side=side,
                  quantity=D(2), entry=D(100), stop_fill=D(90 if side == 'LONG' else 110), fx=D(1),
                  margin=D(100), capital=D(102), planned_risk=D(25), entry_fee=D(2), exit_fee_rate=D('.01'),
                  funding_estimate=D(1), risk_fraction=D('.1'), available_at=AT,
                  expires_at=AT+pd.Timedelta(hours=1), data_valid_until=AT+pd.Timedelta(minutes=10),
                  mark_id=identifier(snap.marks[0]), fx_id=identifier(snap.fx_rates[0]))
    fields.update(changes)
    return Allocation(**fields)


class PortfolioTests(unittest.TestCase):
    def test_default_equity_currency_and_decimal_validation(self):
        p = Portfolio(as_of=AT)
        snap = p.snapshot(as_of=AT)
        self.assertEqual((snap.cash, snap.equity, snap.currency), (D(2000), D(2000), 'INR'))
        for bad in (True, 'NaN', float('inf'), 'invalid'):
            with self.assertRaises(ValueError):
                number(bad)
        for kwargs in (dict(value=0), dict(observed_at=AT+pd.Timedelta(seconds=1))):
            with self.assertRaises(ValueError):
                replace(quote(), **kwargs)
        with self.assertRaises(ValueError):
            spec(multiplier=0)

    def test_reservation_open_pnl_fees_and_full_close_both_sides(self):
        for side, mark, exit_price in (('LONG', 110, 108), ('SHORT', 90, 92)):
            p = book()
            allocation = plan(p, side)
            reserved = p._reserve(allocation, as_of=AT).snapshot
            self.assertEqual((reserved.reserved_capital, reserved.equity), (D(102), D(2000)))
            p.simulate_open('allocation', as_of=AT, event_id='open')
            p.observe(quote(value=mark, at=AT+pd.Timedelta(seconds=1)), kind='MARK',
                      as_of=AT+pd.Timedelta(seconds=1), event_id='mark1')
            snap = p.snapshot(as_of=AT+pd.Timedelta(seconds=1))
            self.assertEqual((snap.cash, snap.unrealized_pnl, snap.equity), (D(1998), D(20), D(2018)))
            p.settle_funding('allocation', -3, as_of=AT+pd.Timedelta(seconds=2), event_id='funding')
            result = p.simulate_close('allocation', exit_price, '.01', as_of=AT+pd.Timedelta(seconds=3), event_id='close')
            exit_fee = D(exit_price)*2*D('.01')
            self.assertEqual(result.snapshot.cash, D(2000)+16-2-3-exit_fee)
            self.assertEqual(result.snapshot.realized_pnl, result.snapshot.cash-2000)
            self.assertEqual(result.snapshot.fees_paid, D(2)+exit_fee)
            self.assertEqual(result.snapshot.funding_cash, -3)
            self.assertEqual(result.snapshot.positions, ())
            self.assertEqual(result.snapshot.used_margin, 0)

    def test_fx_marks_convert_unrealized_and_realized_pnl(self):
        p = book()
        p._reserve(plan(p), as_of=AT)
        p.simulate_open('allocation', as_of=AT, event_id='open')
        later = AT+pd.Timedelta(seconds=1)
        p.observe(quote(value=110, at=later), kind='MARK', as_of=later, event_id='mark1')
        p.observe(quote('USDT', 2, later), kind='FX', as_of=later, event_id='fx1')
        self.assertEqual(p.snapshot(as_of=later).unrealized_pnl, 40)
        p.simulate_close('allocation', 110, 0, as_of=later, event_id='close')
        self.assertEqual(p.snapshot(as_of=later).cash, 2038)

    def test_append_only_history_duplicate_events_and_revisions(self):
        p = book()
        before = p.history
        original = p.observe(quote(), kind='MARK', as_of=AT, event_id='mark0')
        self.assertIs(original, p.history[-1])
        self.assertEqual(p.history, before)
        with self.assertRaises(ValueError):
            p.observe(quote(value=101), kind='MARK', as_of=AT, event_id='mark0')
        with self.assertRaises(FrozenInstanceError):
            original.kind = 'OTHER'
        with self.assertRaises(ValueError):
            p.observe(quote(value=101), kind='MARK', as_of=AT, event_id='new-event-same-observation')

    def test_no_future_quote_or_out_of_order_mutation(self):
        p = book()
        before = p.history
        with self.assertRaises(ValueError):
            p.observe(quote(at=AT+pd.Timedelta(seconds=2)), kind='MARK', as_of=AT, event_id='future')
        with self.assertRaises(ValueError):
            p.observe(quote(), kind='MARK', as_of=AT-pd.Timedelta(seconds=1), event_id='past')
        self.assertEqual(p.history, before)

    def test_consumed_positions_and_cancelled_reservations_cannot_reopen(self):
        p = book()
        p._reserve(plan(p), as_of=AT)
        cancelled = p.cancel('allocation', as_of=AT, event_id='cancel')
        self.assertEqual(cancelled.snapshot.reserved_capital, 0)
        with self.assertRaises(ValueError):
            p.simulate_open('allocation', as_of=AT, event_id='open')
        self.assertIs(p.cancel('allocation', as_of=AT, event_id='cancel'), cancelled)
        with self.assertRaises(ValueError):
            p._reserve(plan(p), as_of=AT)
        p = book()
        p._reserve(plan(p), as_of=AT)
        p.simulate_open('allocation', as_of=AT, event_id='open')
        with self.assertRaises(ValueError):
            p.simulate_open('allocation', as_of=AT, event_id='second-open')
        p.simulate_close('allocation', 100, 0, as_of=AT, event_id='close')
        with self.assertRaises(ValueError):
            p.simulate_close('allocation', 100, 0, as_of=AT, event_id='second-close')
        with self.assertRaises(ValueError):
            p._reserve(plan(p), as_of=AT)

    def test_stale_expired_changed_or_unsupported_reservation_is_not_opened(self):
        for mode in ('expired', 'stale', 'changed', 'equity'):
            p = book()
            allocation = plan(p, data_valid_until=AT+pd.Timedelta(seconds=1 if mode == 'stale' else 600),
                              risk_fraction=D('.001') if mode == 'equity' else D('.1'))
            p._reserve(allocation, as_of=AT)
            later = AT+pd.Timedelta(seconds=2)
            if mode == 'expired':
                later = allocation.expires_at
            elif mode == 'changed':
                p.observe(quote(value=90, at=later), kind='MARK', as_of=later, event_id='changed')
            with self.assertRaises(ValueError):
                p.simulate_open('allocation', as_of=later, event_id='open')
            self.assertFalse(p.snapshot(as_of=later).positions)

    def test_posted_funding_debits_are_not_counted_again_in_remaining_stop_risk(self):
        p = book()
        p._reserve(plan(p), as_of=AT)
        p.simulate_open('allocation', as_of=AT, event_id='open')
        before = p.snapshot(as_of=AT).stop_risk
        p.settle_funding('allocation', '-.5', as_of=AT, event_id='debit')
        self.assertEqual(p.snapshot(as_of=AT).stop_risk, before-D('.5'))
        p.settle_funding('allocation', '.5', as_of=AT, event_id='credit')
        self.assertEqual(p.snapshot(as_of=AT).stop_risk, before-D('.5'))
        p.settle_funding('allocation', -2, as_of=AT, event_id='larger-debit')
        self.assertEqual(p.snapshot(as_of=AT).stop_risk, before-1)

    def test_crossed_stop_uses_worse_current_exit_and_cannot_erase_remaining_costs(self):
        for side, mark, expected in (('LONG', 80, '4.184'), ('SHORT', 120, '5.824')):
            p = book()
            p._reserve(plan(p, side, stop_slippage_rate=D('.01')), as_of=AT)
            p.simulate_open('allocation', as_of=AT, event_id='open')
            later = AT+pd.Timedelta(seconds=1)
            p.observe(quote(value=mark, at=later), kind='MARK', as_of=later, event_id='gap')
            self.assertEqual(p.snapshot(as_of=later).stop_risk, D(expected))

    def test_daily_gap_uses_prior_equity_and_drawdown_survives_recovery(self):
        p = book()
        p._reserve(plan(p), as_of=AT)
        p.simulate_open('allocation', as_of=AT, event_id='open')
        next_day = AT+pd.Timedelta(days=1)
        p.observe(quote(value=50, at=next_day), kind='MARK', as_of=next_day, event_id='gap')
        snap = p.snapshot(as_of=next_day)
        self.assertEqual((snap.day_start_equity, snap.day_low_equity), (D(1998), D(1898)))
        p.observe(quote(value=110, at=next_day+pd.Timedelta(seconds=1)), kind='MARK',
                  as_of=next_day+pd.Timedelta(seconds=1), event_id='recovery')
        snap = p.snapshot(as_of=next_day+pd.Timedelta(seconds=1))
        self.assertEqual(snap.day_low_equity, 1898)
        self.assertGreater(snap.max_drawdown, D('.05'))

    def test_net_losses_include_entry_fees_funding_and_exit_fees(self):
        p = book()
        p._reserve(plan(p), as_of=AT)
        p.simulate_open('allocation', as_of=AT, event_id='open')
        result = p.simulate_close('allocation', 100, 0, as_of=AT, event_id='close')
        self.assertEqual((result.snapshot.consecutive_losses, result.snapshot.last_loss_at), (1, AT))

    def test_invalid_close_and_funding_leave_account_unchanged(self):
        p = book()
        p._reserve(plan(p), as_of=AT)
        p.simulate_open('allocation', as_of=AT, event_id='open')
        before = p.history
        for value in ('NaN', -1):
            with self.assertRaises(ValueError):
                p.simulate_close('allocation', value, 0, as_of=AT, event_id='bad-close')
        with self.assertRaises(ValueError):
            p.settle_funding('missing', 2, as_of=AT, event_id='bad-funding')
        self.assertEqual(p.history, before)

    def test_deterministic_replay_and_future_mutations_preserve_prefix(self):
        a, b = book(), book()
        self.assertEqual(a.history, b.history)
        prefix = a.history
        b.observe(quote(value=10000, at=AT+pd.Timedelta(seconds=1)), kind='MARK',
                  as_of=AT+pd.Timedelta(seconds=1), event_id='future')
        self.assertEqual(b.history[:len(prefix)], prefix)
        self.assertEqual(identifier(D('1.0')), identifier(D('1')))
        self.assertEqual(identifier(D('-0.0')), identifier(D('0')))
        self.assertEqual(wire(D('1234567890123456789012345678901')), '1234567890123456789012345678901')


if __name__ == '__main__':
    unittest.main()
