import sys
sys.dont_write_bytecode = True

from dataclasses import FrozenInstanceError, replace
from decimal import Decimal as D, localcontext
import json
import unittest

import pandas as pd

from trading.portfolio import Portfolio, identifier
from trading.risk import CostModel, RiskConfig, RiskEngine, SignalProposal, proposal_from_m7
from test_portfolio import AT, book, quote, spec


def config(**changes):
    # Explicit synthetic limits, not calibrated or recommended portfolio settings.
    fields = dict(max_open_positions=2, max_gross_exposure=10, max_symbol_exposure=10,
                  max_group_exposure=10, max_portfolio_risk='.5', daily_loss_fraction='.5',
                  max_drawdown_fraction='.9', max_consecutive_losses=2, cooldown_seconds=60,
                  max_quote_age_seconds=300, max_fx_age_seconds=300, max_spec_age_seconds=3600,
                  max_leverage=10)
    fields.update(changes)
    return RiskConfig(**fields)


def costs(**changes):
    fields = dict(entry_fee_rate=0, exit_fee_rate=0, entry_slippage_rate=0, stop_slippage_rate=0,
                  funding_debit_rate=0, source='explicit synthetic cost assumptions', available_at=AT)
    fields.update(changes)
    return CostModel(**fields)


def proposal(name='one', symbol='BTC/USDT', side='LONG', at=AT, stop=None):
    return SignalProposal('signal-'+name, 'formation-'+name, 'decision-'+name, 'policy', 'input-'+name,
                          symbol, side, D(100), D(stop if stop is not None else 90 if side == 'LONG' else 110),
                          at, at+pd.Timedelta(hours=1), 'BACKTEST')


def engine(cfg=None, portfolio=None):
    p = portfolio or book()
    if not any(q.key == 'ETH/USDT' for q in p.snapshot(as_of=AT).marks):
        p.observe(quote('ETH/USDT'), kind='MARK', as_of=AT, event_id='eth0')
    return RiskEngine(p, cfg or config(), mode='BACKTEST')


def decide(e, p=None, instrument=None, assumptions=None, at=AT, **kwargs):
    p = p or proposal()
    return e.evaluate(p, instrument or spec(p.symbol), assumptions or costs(),
                      as_of=at, leverage=kwargs.pop('leverage', 2), **kwargs)


class RiskSizingTests(unittest.TestCase):
    def test_user_risk_is_ten_percent_of_current_equity_not_notional(self):
        e = engine()
        d = decide(e)
        self.assertEqual((d.status, d.equity_reference, d.risk_budget), ('APPROVED', D(2000), D(200)))
        self.assertEqual((d.quantity, d.planned_loss, d.notional_account, d.margin), (D(20), D(200), D(2000), D(1000)))
        self.assertEqual(e.portfolio.snapshot(as_of=AT).reserved_capital, d.capital_reserved)
        self.assertNotEqual(d.risk_budget, d.margin*D('.1'))
        changed = decide(engine(config(risk_fraction='.05')))
        self.assertEqual((changed.risk_budget, changed.quantity), (D(100), D(10)))

    def test_both_sides_costs_and_funding_are_included_without_double_counting_slippage(self):
        for side in ('LONG', 'SHORT'):
            e = engine()
            model = costs(entry_fee_rate='.001', exit_fee_rate='.002', entry_slippage_rate='.001',
                          stop_slippage_rate='.001', funding_debit_rate='.003')
            d = decide(e, proposal(side=side), assumptions=model)
            direction = 1 if side == 'LONG' else -1
            self.assertLess(d.quantity, D(20))
            self.assertLessEqual(d.planned_loss, d.risk_budget)
            expected = direction*(d.entry_price-d.stop_fill)*d.quantity+d.estimated_fees+d.estimated_funding
            self.assertEqual(d.planned_loss, expected)
            self.assertGreater(d.estimated_slippage, 0)
            self.assertGreater(d.estimated_funding, 0)

    def test_contract_multiplier_and_explicit_fx_are_used(self):
        p = Portfolio(as_of=AT)
        p.observe(quote('USDT', 80), kind='FX', as_of=AT, event_id='fx')
        p.observe(quote(), kind='MARK', as_of=AT, event_id='mark')
        e = engine(portfolio=p)
        d = decide(e, instrument=spec(multiplier=10, quantity_step='.001', min_quantity='.001'))
        self.assertEqual((d.quantity, d.planned_loss, d.notional_account), (D('.025'), D(200), D(2000)))

    def test_modeled_stop_loss_matches_offline_accounting_both_sides(self):
        for side in ('LONG', 'SHORT'):
            e = engine()
            model = costs(entry_fee_rate='.001', exit_fee_rate='.002', entry_slippage_rate='.001',
                          stop_slippage_rate='.001', funding_debit_rate='.003')
            d = decide(e, proposal(side=side), assumptions=model)
            e.simulate_open(d.allocation_id, as_of=AT, event_id='open')
            e.portfolio.settle_funding(d.allocation_id, -d.estimated_funding, as_of=AT, event_id='funding')
            e.portfolio.simulate_close(d.allocation_id, d.stop_fill, model.exit_fee_rate, as_of=AT, event_id='close')
            snap = e.portfolio.snapshot(as_of=AT)
            self.assertEqual(D(2000)-snap.equity, d.planned_loss)
            self.assertLessEqual(D(2000)-snap.equity, d.risk_budget)

    def test_large_total_horizon_funding_debit_remains_cost_bounded(self):
        d = decide(engine(), assumptions=costs(funding_debit_rate=2))
        self.assertGreater(d.estimated_funding, 0)
        self.assertLessEqual(d.planned_loss, d.risk_budget)

    def test_capital_and_leverage_reduce_size_without_raising_risk_budget(self):
        low = decide(engine(), proposal(stop=99), leverage=1)
        high = decide(engine(), proposal(stop=99), leverage=10)
        self.assertEqual((low.status, low.quantity), ('REDUCED', D(20)))
        self.assertIn('AVAILABLE_CAPITAL_LIMIT', low.reason_codes)
        self.assertEqual((high.quantity, high.planned_loss), (D(200), D(200)))
        self.assertEqual(low.risk_budget, high.risk_budget)
        self.assertNotEqual(high.risk_budget, high.notional_account*D('.1'))

    def test_requested_quantity_and_precision_never_round_up(self):
        for requested, expected in (('1.09', '1'), ('50', '20')):
            d = decide(engine(), requested_quantity=requested)
            self.assertEqual((d.status, d.quantity), ('REDUCED', D(expected)))
            self.assertLessEqual(d.planned_loss, d.risk_budget)
        tiny = decide(engine(), requested_quantity='.01')
        self.assertEqual((tiny.status, tiny.quantity), ('REJECTED', D(0)))

    def test_adverse_price_tick_rounding_both_sides(self):
        for side, mark, stop, entry, trigger in (('LONG', '100.01', '90.07', '100.1', '90'),
                                                ('SHORT', '99.99', '110.01', '99.9', '110.1')):
            p = Portfolio(as_of=AT)
            p.observe(quote(value=mark), kind='MARK', as_of=AT, event_id='m')
            p.observe(quote('USDT', 1), kind='FX', as_of=AT, event_id='f')
            d = decide(engine(portfolio=p), proposal(side=side, stop=stop), requested_quantity=1)
            self.assertEqual((d.entry_price, d.stop_fill), (D(entry), D(trigger)))

    def test_exchange_minimums_and_costs_never_increase_risk_to_fit(self):
        for instrument, model in ((spec(min_quantity=21), costs()),
                                  (spec(min_notional=2100), costs()),
                                  (spec(min_quantity=2), costs(entry_fee_rate='.9', exit_fee_rate='.9'))):
            d = decide(engine(), instrument=instrument, assumptions=model)
            self.assertEqual((d.status, d.quantity, d.allocation_id), ('REJECTED', D(0), None))
            self.assertIn('EXCHANGE_MINIMUM_NOT_AFFORDABLE', d.reason_codes)

    def test_protective_stop_and_leverage_validation(self):
        for p, leverage, reason in ((proposal(stop=100), 2, 'STOP_NOT_PROTECTIVE'),
                                    (proposal(side='SHORT', stop=99), 2, 'STOP_NOT_PROTECTIVE'),
                                    (proposal(), 11, 'LEVERAGE_LIMIT'), (proposal(), '.5', 'LEVERAGE_LIMIT')):
            d = decide(engine(), p, leverage=leverage)
            self.assertEqual(d.status, 'REJECTED')
            self.assertIn(reason, d.reason_codes)

    def test_unrealized_and_realized_pnl_recalculate_equity_risk_budget(self):
        for close in (False, True):
            e = engine()
            d = decide(e)
            e.portfolio.simulate_open(d.allocation_id, as_of=AT, event_id='open')
            later = AT+pd.Timedelta(seconds=1)
            e.portfolio.observe(quote(value=110, at=later), kind='MARK', as_of=later, event_id='m1')
            if close:
                e.portfolio.simulate_close(d.allocation_id, 105, 0, as_of=later, event_id='close')
            next_decision = decide(e, proposal('two', 'ETH/USDT'), at=later)
            self.assertEqual(next_decision.equity_reference, D(2100 if close else 2200))
            self.assertEqual(next_decision.risk_budget, D(210 if close else 220))

    def test_invalid_or_missing_inputs_do_not_invent_costs_or_fx(self):
        p = Portfolio(as_of=AT)
        p.observe(quote(), kind='MARK', as_of=AT, event_id='mark')
        d = decide(engine(portfolio=p))
        self.assertIn('MARK_OR_FX_MISSING', d.reason_codes)
        for kwargs in (dict(entry_fee_rate=-1), dict(funding_debit_rate=-1), dict(stop_slippage_rate=1)):
            with self.assertRaises(ValueError):
                costs(**kwargs)
        for kwargs in (dict(risk_fraction=0), dict(risk_fraction='1.01'), dict(max_open_positions=True)):
            with self.assertRaises(ValueError):
                config(**kwargs)


class PortfolioRiskLimitsTests(unittest.TestCase):
    def test_maximum_positions_counts_reservations_and_symbol_concentration(self):
        e = engine(config(max_open_positions=1))
        decide(e)
        d = decide(e, proposal('two', 'ETH/USDT'))
        self.assertEqual(d.status, 'BLOCKED')
        self.assertIn('MAX_OPEN_POSITIONS', d.reason_codes)
        e = engine()
        decide(e)
        self.assertIn('SYMBOL_ALREADY_ALLOCATED', decide(e, proposal('two')).reason_codes)

    def test_gross_symbol_correlated_and_aggregate_stop_risk_limits_reduce(self):
        for setting, value, expected, reason in (
                ('max_gross_exposure', '1.5', 10, 'GROSS_EXPOSURE_LIMIT'),
                ('max_group_exposure', '1.25', 5, 'CORRELATED_EXPOSURE_LIMIT'),
                ('max_portfolio_risk', '.15', 10, 'AGGREGATE_STOP_RISK_LIMIT')):
            e = engine(config(**{setting: value}))
            decide(e)
            result = decide(e, proposal('two', 'ETH/USDT'))
            self.assertEqual((result.status, result.quantity), ('REDUCED', D(expected)))
            self.assertIn(reason, result.reason_codes)
        result = decide(engine(config(max_symbol_exposure='.5')))
        self.assertEqual(result.quantity, 10)
        self.assertIn('SYMBOL_EXPOSURE_LIMIT', result.reason_codes)

    def test_distinct_explicit_groups_are_not_automatically_correlated(self):
        e = engine(config(max_group_exposure=1))
        decide(e)
        d = decide(e, proposal('two', 'ETH/USDT'), instrument=spec('ETH/USDT', correlation_group='other-fixture-group'))
        self.assertEqual(d.status, 'APPROVED')

    def test_planned_stop_risk_respects_daily_and_drawdown_remaining_headroom(self):
        for kwargs, expected, reason in ((dict(daily_loss_fraction='.05'), 10, 'DAILY_LOSS_HEADROOM_LIMIT'),
                                         (dict(max_drawdown_fraction='.025'), 5, 'DRAWDOWN_HEADROOM_LIMIT')):
            d = decide(engine(config(**kwargs)))
            self.assertEqual((d.status, d.quantity), ('REDUCED', D(expected)))
            self.assertIn(reason, d.reason_codes)

    def test_simulated_open_rechecks_limits_and_does_not_book_after_drawdown_halt(self):
        e = engine(config(max_drawdown_fraction='.02'))
        first = decide(e, requested_quantity=1)
        e.simulate_open(first.allocation_id, as_of=AT, event_id='open1')
        pending = decide(e, proposal('two', 'ETH/USDT'), requested_quantity=1)
        later = AT+pd.Timedelta(seconds=1)
        e.portfolio.observe(quote(value=50, at=later), kind='MARK', as_of=later, event_id='gap')
        before = e.portfolio.history
        with self.assertRaisesRegex(ValueError, 'portfolio limits'):
            e.simulate_open(pending.allocation_id, as_of=later, event_id='open2')
        self.assertEqual(e.portfolio.history, before)
        self.assertEqual(len(e.portfolio.snapshot(as_of=later).positions), 1)

    def test_daily_loss_halt_latches_through_recovery_then_resets_on_utc_day(self):
        e = engine(config(daily_loss_fraction='.1'))
        first = decide(e)
        e.portfolio.simulate_open(first.allocation_id, as_of=AT, event_id='open')
        e.portfolio.settle_funding(first.allocation_id, -250, as_of=AT, event_id='loss')
        e.portfolio.settle_funding(first.allocation_id, 250, as_of=AT, event_id='recover')
        d = decide(e, proposal('blocked', 'ETH/USDT'))
        self.assertEqual(d.status, 'BLOCKED')
        self.assertIn('DAILY_LOSS_HALT', d.reason_codes)
        e.portfolio.simulate_close(first.allocation_id, 100, 0, as_of=AT, event_id='flat-before-new-day')
        tomorrow = AT+pd.Timedelta(days=1)
        for key, value, kind in (('BTC/USDT', 100, 'MARK'), ('ETH/USDT', 100, 'MARK'), ('USDT', 1, 'FX')):
            e.portfolio.observe(quote(key, value, tomorrow), kind=kind, as_of=tomorrow, event_id=key+'-next-day')
        d = decide(e, proposal('next-day', 'ETH/USDT', at=tomorrow), at=tomorrow,
                   instrument=spec('ETH/USDT', available_at=tomorrow))
        self.assertNotIn('DAILY_LOSS_HALT', d.reason_codes)
        self.assertEqual(d.status, 'APPROVED')

    def test_drawdown_halt_persists_after_recovery(self):
        e = engine(config(max_drawdown_fraction='.1'))
        d = decide(e)
        e.portfolio.simulate_open(d.allocation_id, as_of=AT, event_id='open')
        e.portfolio.settle_funding(d.allocation_id, -200, as_of=AT, event_id='loss')
        e.portfolio.settle_funding(d.allocation_id, 200, as_of=AT, event_id='recover')
        result = decide(e, proposal('two', 'ETH/USDT'))
        self.assertEqual(result.status, 'BLOCKED')
        self.assertIn('DRAWDOWN_HALT', result.reason_codes)

    def test_loss_streak_cooldown_and_exact_expiry(self):
        e = engine()
        for name in ('one', 'two'):
            d = decide(e, proposal(name), requested_quantity=1)
            e.portfolio.simulate_open(d.allocation_id, as_of=AT, event_id='open-'+name)
            e.portfolio.simulate_close(d.allocation_id, 99, 0, as_of=AT, event_id='close-'+name)
        blocked = decide(e, proposal('three', 'ETH/USDT'))
        self.assertIn('LOSS_STREAK_COOLDOWN', blocked.reason_codes)
        later = AT+pd.Timedelta(seconds=60)
        allowed = decide(e, proposal('four', 'ETH/USDT'), at=later)
        self.assertEqual(allowed.status, 'APPROVED')

    def test_nonlosing_completed_trade_resets_loss_streak(self):
        e = engine()
        for name, price in (('one', 99), ('two', 102)):
            d = decide(e, proposal(name), requested_quantity=1)
            e.simulate_open(d.allocation_id, as_of=AT, event_id='open-'+name)
            e.portfolio.simulate_close(d.allocation_id, price, 0, as_of=AT, event_id='close-'+name)
        self.assertEqual(e.portfolio.snapshot(as_of=AT).consecutive_losses, 0)


class RiskCausalityTests(unittest.TestCase):
    def test_shared_portfolio_cannot_reset_consumption_with_a_second_actor(self):
        e = engine()
        decide(e, instrument=spec(min_quantity=1000))
        before = e.portfolio.history
        with self.assertRaisesRegex(ValueError, 'already has a risk actor'):
            RiskEngine(e.portfolio, config(risk_fraction='.05'), mode='BACKTEST')
        self.assertEqual(e.portfolio.history, before)

    def test_subsecond_freshness_does_not_truncate_nanoseconds(self):
        for nanos, expected in ((1, 'APPROVED'), (2, 'REJECTED')):
            e = engine(config(max_quote_age_seconds='.000000001'))
            d = decide(e, at=AT+pd.Timedelta(nanoseconds=nanos))
            self.assertEqual(d.status, expected)
            if nanos == 2:
                self.assertIn('MARK_STALE', d.reason_codes)

    def test_identical_replay_is_idempotent_and_changed_signal_is_blocked(self):
        e = engine()
        d = decide(e)
        self.assertIs(decide(e), d)
        self.assertEqual(len(e.history), 1)
        self.assertEqual(len(e.portfolio.snapshot(as_of=AT).reservations), 1)
        result = decide(e, requested_quantity=1)
        self.assertIn('SIGNAL_ALREADY_CONSUMED', result.reason_codes)
        with self.assertRaises(FrozenInstanceError):
            d.status = 'REJECTED'
        json.dumps(d.to_dict(), allow_nan=False)

    def test_rejections_consume_formation_and_cross_policy_signals_cannot_retry(self):
        e = engine()
        rejected = decide(e, instrument=spec(min_quantity=1000))
        self.assertEqual(rejected.status, 'REJECTED')
        changed = replace(proposal(), signal_id='new-policy-signal', source_policy_id='different')
        result = decide(e, changed)
        self.assertIn('FORMATION_ALREADY_CONSUMED', result.reason_codes)
        self.assertIsNone(result.allocation_id)

    def test_freshness_exact_deadline_then_stale_and_future_assumptions(self):
        for age, expected in ((300, 'APPROVED'), (301, 'REJECTED')):
            d = decide(engine(), at=AT+pd.Timedelta(seconds=age))
            self.assertEqual(d.status, expected)
            if age == 301:
                self.assertIn('MARK_STALE', d.reason_codes)
                self.assertIn('FX_STALE', d.reason_codes)
        d = decide(engine(config(max_spec_age_seconds=60)), at=AT+pd.Timedelta(seconds=61))
        self.assertIn('SPEC_STALE', d.reason_codes)
        e = engine()
        with self.assertRaisesRegex(ValueError, 'unavailable'):
            decide(e, assumptions=costs(available_at=AT+pd.Timedelta(seconds=1)))
        self.assertFalse(e.history)

    def test_unavailable_expired_terminal_and_mode_mismatch_reject(self):
        e = engine()
        with self.assertRaisesRegex(ValueError, 'unavailable'):
            decide(e, proposal(at=AT+pd.Timedelta(seconds=1)))
        self.assertFalse(e.history)
        with self.assertRaisesRegex(ValueError, 'Future terminal'):
            decide(e, replace(proposal(), terminal_at=AT+pd.Timedelta(seconds=1)))
        self.assertFalse(e.history)
        for p, reason in ((replace(proposal(), terminal_at=AT), 'SIGNAL_TERMINAL'),
                          (replace(proposal(), mode='CURRENT'), 'MODE_MISMATCH')):
            d = decide(engine(), p)
            self.assertIn(reason, d.reason_codes)
            self.assertIsNone(d.allocation_id)
        d = decide(engine(), at=AT+pd.Timedelta(hours=1))
        self.assertIn('SIGNAL_EXPIRED', d.reason_codes)

    def test_rejected_decisions_still_advance_chronology(self):
        e = engine()
        decide(e, at=AT+pd.Timedelta(seconds=301))
        with self.assertRaises(ValueError):
            decide(e, proposal('two'), at=AT+pd.Timedelta(seconds=300))

    def test_deterministic_prefix_future_mutation_and_configuration_identity(self):
        a, b = engine(), engine()
        da, db = decide(a), decide(b)
        self.assertEqual(da, db)
        before = identifier(a.history)
        later = AT+pd.Timedelta(seconds=1)
        b.portfolio.observe(quote(value=1000000, at=later), kind='MARK', as_of=later, event_id='future')
        decide(b, proposal('two', 'ETH/USDT'), at=later)
        self.assertEqual(b.history[0], da)
        self.assertEqual(identifier(a.history), before)
        self.assertNotEqual(a.configuration_id, engine(config(risk_fraction='.05')).configuration_id)

    def test_decimal_context_does_not_change_sizing_or_identity(self):
        results = []
        for precision in (6, 28, 50):
            with localcontext() as context:
                context.prec = precision
                results.append(decide(engine(), assumptions=costs(entry_fee_rate='.0013', stop_slippage_rate='.0017')))
        self.assertEqual(results[0], results[1])
        self.assertEqual(results[1], results[2])


class M7BoundaryTests(unittest.TestCase):
    def record(self):
        from trading.signal_context import assemble_context
        from trading.signals import SignalTracker
        from test_signal_context import CONFIG, inputs
        from test_signals import policy
        context = assemble_context(**inputs())
        tracker = SignalTracker(policy(), CONFIG, mode='BACKTEST')
        return tracker, tracker.observe(context)

    def prepared(self, record):
        p = Portfolio(as_of=record.available_at)
        p.observe(quote(record.symbol, record.reference_price, record.available_at), kind='MARK',
                  as_of=record.available_at, event_id='mark')
        p.observe(quote('USDT', 80, record.available_at), kind='FX', as_of=record.available_at, event_id='fx')
        return RiskEngine(p, config(), mode='BACKTEST')

    def test_actual_m7_record_handoff_preserves_source_identity_and_does_not_use_score(self):
        _, record = self.record()
        original = identifier(record.to_dict())
        p = proposal_from_m7(record, as_of=record.available_at)
        self.assertEqual((p.signal_id, p.formation_id, p.source_policy_id, p.source_input_id),
                         (record.signal_id, record.pattern_id, record.policy_id, record.input_id))
        e = self.prepared(record)
        d = decide(e, p, at=record.available_at, instrument=spec(available_at=record.available_at))
        self.assertIn(d.status, ('APPROVED', 'REDUCED'))
        self.assertEqual(identifier(record.to_dict()), original)
        with self.assertRaises(ValueError):
            proposal_from_m7(replace(record, qualification_passed=False), as_of=record.available_at)
        with self.assertRaises(ValueError):
            proposal_from_m7(record, as_of=record.available_at-pd.Timedelta(seconds=1))

    def test_future_terminal_events_do_not_leak_and_available_events_cancel_reservations(self):
        from trading.pattern_engine import ClosedCandle
        from trading.patterns import OHLC
        tracker, record = self.record()
        candle = ClosedCandle(record.available_at, OHLC(101, 102, 93, 100), record.symbol)
        event = tracker.observe_signal(record.signal_id, candle, as_of=candle.available_at)
        p = proposal_from_m7(record, as_of=record.available_at, events=(event,))
        self.assertIsNone(p.terminal_at)
        self.assertEqual(p, proposal_from_m7(record, as_of=record.available_at))
        expired = proposal_from_m7(record, as_of=event.available_at, events=(event,))
        self.assertEqual(expired.terminal_at, event.available_at)
        e = self.prepared(record)
        d = decide(e, p, at=record.available_at, instrument=spec(available_at=record.available_at))
        e.observe_m7_terminal(event, as_of=event.available_at)
        self.assertFalse(e.portfolio.snapshot(as_of=event.available_at).reservations)
        self.assertEqual(e.terminal_history[0].cancelled_allocations, (d.allocation_id,))
        e.observe_m7_terminal(event, as_of=event.available_at)
        self.assertEqual(len(e.terminal_history), 1)
        with self.assertRaises(ValueError):
            e.portfolio.simulate_open(d.allocation_id, as_of=event.available_at, event_id='open')
        self.assertEqual(record.lifecycle_state, 'QUALIFIED')

    def test_terminal_event_does_not_execute_an_open_position_exit(self):
        from trading.signals import SignalLifecycleEvent
        _, record = self.record()
        e = self.prepared(record)
        p = proposal_from_m7(record, as_of=record.available_at)
        d = decide(e, p, at=record.available_at, instrument=spec(available_at=record.available_at))
        e.portfolio.simulate_open(d.allocation_id, as_of=record.available_at, event_id='open')
        event = SignalLifecycleEvent('terminal', record.signal_id, 'INVALIDATED', record.available_at, None, None, 'fixture')
        e.observe_m7_terminal(event, as_of=event.available_at)
        self.assertEqual(len(e.portfolio.snapshot(as_of=event.available_at).positions), 1)
        self.assertEqual(e.terminal_history[0].open_allocations, (d.allocation_id,))


if __name__ == '__main__':
    unittest.main()
