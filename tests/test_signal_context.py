import sys
sys.dont_write_bytecode = True

from dataclasses import FrozenInstanceError, replace
import unittest

import numpy as np
import pandas as pd

from trading.m6 import M6Bundle
from trading.derivatives import DerivativesEngine
from trading.m6_data import Source, identity
from trading.pattern_engine import ClosedCandle, PatternEngine, evaluate_setup
from trading.patterns import OHLC
from trading.regime import RegimeConfig, RegimeEngine
from trading.signal_context import SignalContextConfig, assemble_context
from trading.structure import StructureConfig, StructureEngine
from test_pattern_engine import START, DOWN, HARAMI, frame
from test_structure import mirror
from test_m6_data import metric, stamp


CONFIG = SignalContextConfig(900, 'NEUTRAL', 'UNCERTAIN')  # Explicit synthetic test policy.
REGIME_CONFIG = RegimeConfig(fast_ema=3, slow_ema=8, indicator_bars=3, band_bars=5,
                             slope_bars=2, reference_bars=5, persistence_bars=2, min_slope_atr=.001)


def inputs(symbol='BTC/USDT', bearish=False, as_of=None):
    candles = DOWN+HARAMI
    if bearish:
        candles = mirror(candles)
    raw = frame(candles)
    pattern = next(e for e in PatternEngine().scan(raw, symbol, as_of=START+pd.Timedelta(minutes=75))
                   if e.family == 'HARAMI' and e.candle_references[-1] == raw.timestamp.iloc[-1])
    at = pd.Timestamp(as_of) if as_of is not None else pattern.confirmed_at+pd.Timedelta(minutes=5)
    opening = at.floor('5min')-pd.Timedelta(minutes=5)
    ohlc = OHLC(99, 102, 98, 101)
    if bearish:
        ohlc = mirror([ohlc])[0]
    confirmation = ClosedCandle(opening, ohlc, symbol)
    structure = StructureEngine(StructureConfig(left_bars=1, right_bars=1)).scan(raw, symbol, as_of=at)
    structure_candle = ClosedCandle(raw.timestamp.iloc[-1], candles[-1], symbol, '15m')
    regimes = []
    for tf in ('15m', '1h', '4h'):
        interval = pd.Timedelta(tf)
        end = at.floor(interval)
        values = np.linspace(90, 100, 60)
        if bearish:
            values = 220-values
        source_frame = frame([OHLC(v, v+5, v-5, v) for v in values], interval,
                             start=end-60*interval)
        if tf == '15m':
            # Keep the actual pattern/structure candles in the primary timeframe.
            for i, c in enumerate(candles):
                source_frame.loc[len(source_frame)-len(candles)+i, ['open', 'high', 'low', 'close']] = vars(c).values()
        source_frame['volume'] = 100
        regimes.append(RegimeEngine(REGIME_CONFIG).analyze(source_frame, symbol, tf, as_of=at,
                       source=Source('fixture', 'venue', symbol.replace('/', ''), 'completed_ohlcv')))
    bundle = M6Bundle(symbol, at, tuple(regimes), (), ())
    return dict(pattern=pattern, candle=confirmation, structure=structure,
                structure_candle=structure_candle, m6=bundle, as_of=at, mode='BACKTEST', config=CONFIG)


def change_regime(fields, timeframe, **changes):
    bundle = fields['m6']
    fields['m6'] = replace(bundle, regime=tuple(replace(r, **changes) if r.timeframe == timeframe else r
                                               for r in bundle.regime))


class SignalContextTests(unittest.TestCase):
    def test_role_freshness_deadline_is_available_then_stale_one_second_later(self):
        for tf, role in (('15m', 'volatility_15m'), ('1h', 'direction_1h'), ('4h', 'regime_4h')):
            for age_intervals in (.5, 2.):
                fields = inputs()
                original = next(r for r in fields['m6'].regime if r.timeframe == tf)
                deadline = min(age_intervals, 1)*pd.Timedelta(tf)
                for extra, status in ((0, 'AVAILABLE'), (1, 'STALE')):
                    with self.subTest(tf=tf, age_intervals=age_intervals, extra=extra):
                        at = original.candle_closed_at+deadline+pd.Timedelta(seconds=extra)
                        fields['as_of'] = at
                        fields['structure'] = replace(fields['structure'], as_of=at)
                        fields['m6'] = replace(fields['m6'], as_of=at,
                                              regime=tuple(replace(r, as_of=at) for r in fields['m6'].regime))
                        change_regime(fields, tf, config=replace(original.config, max_age_intervals=age_intervals))
                        source = next(s for s in assemble_context(**fields).sources if s.role == role)
                        self.assertEqual(source.status, status)
                        self.assertEqual(source.observed_at, original.candle_closed_at)

    def test_structure_freshness_deadline_is_available_then_stale_one_second_later(self):
        for extra, status in ((0, 'AVAILABLE'), (1, 'STALE')):
            with self.subTest(extra=extra):
                fields = inputs(as_of=START+pd.Timedelta(minutes=90, seconds=extra))
                context = assemble_context(**fields)
                source = next(s for s in context.sources if s.role == 'structure_15m')
                self.assertEqual(source.status, status)
                self.assertEqual(context.structure_eligible, extra == 0)
                if extra:
                    self.assertIsNone(context.snapshot.support_distance_atr)

    def test_unknown_regime_reason_is_distinct_from_uncertain_transition(self):
        for classification, mapping, expected in (('UNKNOWN', 'UNCERTAIN', 'unknown'),
                                                   ('TRANSITION', 'UNCERTAIN', 'uncertain'),
                                                   ('TRANSITION', 'UNKNOWN', 'unknown')):
            with self.subTest(classification=classification, mapping=mapping):
                fields = inputs()
                change_regime(fields, '4h', classification=classification)
                fields['config'] = replace(CONFIG, transition_regime=mapping)
                context = assemble_context(**fields)
                self.assertIn(f'regime_4h_{expected}', context.reasons)
                other = 'unknown' if expected == 'uncertain' else 'uncertain'
                self.assertNotIn(f'regime_4h_{other}', context.reasons)

    def test_no_available_structure_bars_remain_missing_without_invented_levels(self):
        contexts = []
        for empty in (True, False):
            fields = inputs()
            raw = frame(DOWN+HARAMI)
            raw = raw.iloc[:0] if empty else raw.assign(timestamp=raw.timestamp+pd.Timedelta(days=1))
            fields['structure'] = StructureEngine(fields['structure'].config).scan(
                raw, fields['pattern'].symbol, as_of=fields['as_of'])
            fields['structure_candle'] = None
            context = assemble_context(**fields)
            contexts.append(context)
            source = next(s for s in context.sources if s.role == 'structure_15m')
            self.assertEqual((source.status, source.observed_at, source.available_at), ('MISSING', None, None))
            self.assertEqual((context.structure.swings, context.structure.zones, context.structure.events), ((), (), ()))
            self.assertFalse(context.structure_eligible)
            self.assertIn('structure_missing', context.reasons)
            self.assertIsNone(context.snapshot.support_distance_atr)
            self.assertIsNone(context.snapshot.resistance_distance_atr)
            self.assertEqual(evaluate_setup(context.pattern, context.confirmation_candle,
                                           as_of=context.as_of, context=context.snapshot).status, 'WAITING')
        self.assertEqual(contexts[0], contexts[1])

    def test_real_m3_m5_m6_handoff_both_assets_and_directions(self):
        for symbol in ('BTC/USDT', 'ETH/USDT'):
            for bearish in (False, True):
                with self.subTest(symbol=symbol, bearish=bearish):
                    fields = inputs(symbol, bearish)
                    context = assemble_context(**fields)
                    self.assertTrue(context.structure_eligible)
                    self.assertEqual(context.snapshot.higher_direction, 'BEARISH' if bearish else 'BULLISH')
                    self.assertEqual(context.snapshot.regime, context.snapshot.higher_direction)
                    decision = evaluate_setup(fields['pattern'], fields['candle'], as_of=fields['as_of'],
                                              context=context.snapshot)
                    self.assertEqual(decision.status, 'CONFIRMED')
                    self.assertIs(context.pattern, fields['pattern'])
                    self.assertIs(context.m6, fields['m6'])

    def test_sources_retain_original_times_configuration_and_provenance(self):
        fields = inputs()
        context = assemble_context(**fields)
        sources = {s.role: s for s in context.sources}
        for tf, role in (('15m', 'volatility_15m'), ('1h', 'direction_1h'), ('4h', 'regime_4h')):
            result = next(r for r in fields['m6'].regime if r.timeframe == tf)
            self.assertEqual(sources[role].observed_at, result.candle_closed_at)
            self.assertEqual(sources[role].available_at, result.available_at)
            self.assertEqual(sources[role].configuration_id, result.configuration_id)
            self.assertEqual(sources[role].provenance, result.source)
        with self.assertRaises(FrozenInstanceError):
            context.structure_eligible = False

    def test_missing_macro_is_unknown_without_directional_fallback(self):
        fields = inputs()
        fields['m6'] = replace(fields['m6'], regime=tuple(r for r in fields['m6'].regime if r.timeframe == '15m'))
        context = assemble_context(**fields)
        self.assertEqual((context.snapshot.higher_direction, context.snapshot.regime), ('UNKNOWN', 'UNKNOWN'))
        self.assertTrue(context.structure_eligible)
        self.assertEqual(evaluate_setup(fields['pattern'], fields['candle'], as_of=fields['as_of'], context=context.snapshot).status,
                         'WAITING')

    def test_neutral_and_transition_mappings_are_explicit(self):
        fields = inputs()
        change_regime(fields, '1h', classification='RANGE')
        change_regime(fields, '4h', classification='TRANSITION')
        context = assemble_context(**fields)
        self.assertEqual((context.snapshot.higher_direction, context.snapshot.regime), ('NEUTRAL', 'UNCERTAIN'))
        fields['config'] = SignalContextConfig(900, 'UNKNOWN', 'UNKNOWN')
        context = assemble_context(**fields)
        self.assertEqual((context.snapshot.higher_direction, context.snapshot.regime), ('UNKNOWN', 'UNKNOWN'))

    def test_each_macro_staleness_is_independent_and_never_renewed(self):
        for stale_tf, role in (('1h', 'direction_1h'), ('4h', 'regime_4h')):
            fields = inputs()
            old = next(r for r in fields['m6'].regime if r.timeframe == stale_tf)
            interval = pd.Timedelta(stale_tf)
            change_regime(fields, stale_tf, candle_closed_at=old.candle_closed_at-interval,
                          available_at=fields['as_of'], config=replace(old.config, max_age_intervals=10))
            context = assemble_context(**fields)
            source = next(s for s in context.sources if s.role == role)
            self.assertEqual(source.status, 'STALE')
            if stale_tf == '1h':
                self.assertEqual(context.snapshot.higher_direction, 'UNKNOWN')
                self.assertEqual(context.snapshot.regime, 'BULLISH')
            else:
                self.assertEqual(context.snapshot.regime, 'UNKNOWN')
                self.assertEqual(context.snapshot.higher_direction, 'BULLISH')

    def test_old_structure_price_time_is_not_refreshed_by_delayed_receipt(self):
        fields = inputs(as_of=START+pd.Timedelta(minutes=100))
        fields['structure_candle'] = replace(fields['structure_candle'], available_at=fields['as_of'])
        fields['structure'] = replace(fields['structure'], last_available_at=fields['as_of'])
        context = assemble_context(**fields)
        self.assertFalse(context.structure_eligible)
        self.assertIn('structure_stale', context.reasons)
        self.assertIsNone(context.snapshot.support_distance_atr)

    def test_missing_structure_source_is_not_assumed_fresh(self):
        fields = inputs()
        fields['structure_candle'] = None
        context = assemble_context(**fields)
        self.assertFalse(context.structure_eligible)
        self.assertIn('structure_source_candle_missing', context.reasons)

    def test_missing_atr_and_volume_are_not_imputed(self):
        fields = inputs()
        atr = next(r for r in fields['m6'].regime if r.timeframe == '15m')
        change_regime(fields, '15m', evidence=tuple(e for e in atr.evidence if e.name != 'relative_volume'))
        context = assemble_context(**fields)
        self.assertTrue(context.structure_eligible)
        self.assertIsNone(context.snapshot.relative_volume)
        change_regime(fields, '15m', evidence=())
        context = assemble_context(**fields)
        self.assertFalse(context.structure_eligible)
        self.assertIsNone(context.snapshot.atr)
        self.assertIn('atr_15m_unavailable', context.reasons)

    def test_invalid_atr_units_and_duplicate_indicators_fail(self):
        for duplicate in (False, True):
            fields = inputs()
            result = next(r for r in fields['m6'].regime if r.timeframe == '15m')
            atr = next(e for e in result.evidence if e.name == 'atr')
            evidence = result.evidence+(atr,) if duplicate else tuple(replace(e, unit='USD') if e.name == 'atr' else e
                                                                       for e in result.evidence)
            change_regime(fields, '15m', evidence=evidence)
            with self.assertRaises(ValueError):
                assemble_context(**fields)

    def test_rejects_wrong_roles_symbols_cutoffs_and_legacy_configuration(self):
        modifications = (
            ('pattern', lambda f: replace(f['pattern'], timeframe='5m')),
            ('pattern', lambda f: replace(f['pattern'], configuration_id='legacy')),
            ('candle', lambda f: replace(f['candle'], symbol='ETH/USDT')),
            ('structure', lambda f: replace(f['structure'], timeframe='1h')),
            ('structure', lambda f: replace(f['structure'], as_of=f['as_of']+pd.Timedelta(minutes=5))),
            ('structure_candle', lambda f: replace(f['structure_candle'], available_at=f['as_of'])),
        )
        for key, change in modifications:
            fields = inputs(); fields[key] = change(fields)
            with self.assertRaises(ValueError):
                assemble_context(**fields)

    def test_future_inputs_are_rejected_not_backdated(self):
        for key, change in (
                ('pattern', lambda f: replace(f['pattern'], confirmed_at=f['as_of']+pd.Timedelta(minutes=1))),
                ('candle', lambda f: replace(f['candle'], available_at=f['as_of']+pd.Timedelta(minutes=1)))):
            fields = inputs(); fields[key] = change(fields)
            with self.assertRaises(ValueError):
                assemble_context(**fields)

    def test_context_time_is_source_availability_not_assembly_time(self):
        fields = inputs()
        new_time = fields['as_of']+pd.Timedelta(minutes=2)
        fields['as_of'] = new_time
        fields['structure'] = replace(fields['structure'], as_of=new_time)
        fields['m6'] = replace(fields['m6'], as_of=new_time, regime=tuple(replace(r, as_of=new_time) for r in fields['m6'].regime))
        context = assemble_context(**fields)
        self.assertEqual(context.snapshot.available_at, fields['candle'].available_at)
        self.assertLess(context.snapshot.available_at, context.as_of)

    def test_repeat_replay_is_deterministic_and_inputs_are_immutable(self):
        fields = inputs()
        before = identity(tuple(fields.values()))
        a = assemble_context(**fields)
        b = assemble_context(**fields)
        self.assertEqual(a, b)
        self.assertEqual(before, identity(tuple(fields.values())))

    def test_expiry_and_overlapping_confirmation_are_explicit(self):
        fields = inputs()
        fields['candle'] = ClosedCandle(fields['pattern'].confirmed_at-pd.Timedelta(minutes=5),
                                        fields['candle'].ohlc, fields['pattern'].symbol)
        context = assemble_context(**fields)
        self.assertIn('confirmation_began_before_pattern_availability', context.reasons)
        fields = inputs()
        fields['pattern'] = replace(fields['pattern'], expires_at=fields['as_of'])
        self.assertIn('pattern_expired', assemble_context(**fields).reasons)

    def test_config_requires_explicit_valid_policies(self):
        for args in ((0, 'NEUTRAL', 'UNCERTAIN'), (True, 'NEUTRAL', 'UNCERTAIN'),
                     (900, 'BULLISH', 'UNCERTAIN'), (900, 'NEUTRAL', 'BULLISH')):
            with self.assertRaises(ValueError):
                SignalContextConfig(*args)

    def test_current_only_external_records_cannot_enter_historical_context(self):
        fields = inputs()
        row = metric('funding_rate', .001, fields['as_of'],
                     time=stamp(fields['as_of'], historical=False))
        current = DerivativesEngine().analyze([row], 'BTC/USDT', '5m', as_of=fields['as_of'], mode='CURRENT')
        fields['m6'] = replace(fields['m6'], derivatives=(current,))
        with self.assertRaises(ValueError):
            assemble_context(**fields)
        fields['mode'] = 'CURRENT'
        context = assemble_context(**fields)
        self.assertFalse(context.m6.derivatives[0].metrics[0].observation.time.historical_eligible)
        fields['mode'] = 'BACKTEST'
        fields['m6'] = replace(fields['m6'], derivatives=(replace(current, mode='BACKTEST'),))
        with self.assertRaises(ValueError):
            assemble_context(**fields)

    def test_prefix_and_future_price_mutation_leave_context_unchanged(self):
        fields = inputs()
        at = fields['as_of']
        end = at.floor('1h')
        history = frame([OHLC(v, v+5, v-5, v) for v in np.linspace(90, 100, 60)],
                        '1h', start=end-pd.Timedelta(hours=60))
        engine = RegimeEngine(REGIME_CONFIG)
        original = engine.analyze(history, 'BTC/USDT', '1h', as_of=at)
        fields['m6'] = replace(fields['m6'], regime=tuple(original if r.timeframe == '1h' else r for r in fields['m6'].regime))
        before = assemble_context(**fields)
        future = frame([OHLC(200, 210, 190, 205), OHLC(205, 220, 200, 210)], '1h', start=end)
        full = pd.concat([history, future], ignore_index=True)
        for multiplier in (1, 3):
            changed = full.copy()
            changed.loc[60:, ['open', 'high', 'low', 'close']] *= multiplier
            result = engine.analyze(changed, 'BTC/USDT', '1h', as_of=at)
            self.assertEqual(result, original)
            fields['m6'] = replace(fields['m6'], regime=tuple(result if r.timeframe == '1h' else r for r in fields['m6'].regime))
            self.assertEqual(assemble_context(**fields), before)

    def test_future_structure_evidence_fails_and_old_confirmation_is_flagged(self):
        fields = inputs()
        original = fields['structure']
        future = fields['as_of']+pd.Timedelta(minutes=1)
        for changed in (replace(original, swings=(replace(original.swings[0], confirmed_at=future),)),
                        replace(original, zones=(replace(original.zones[0], created_at=future),))):
            fields['structure'] = changed
            with self.assertRaises(ValueError):
                assemble_context(**fields)
        fields = inputs(as_of=START+pd.Timedelta(minutes=85))
        fields['candle'] = replace(fields['candle'], opened_at=START+pd.Timedelta(minutes=75), available_at=None)
        self.assertIn('confirmation_candle_stale', assemble_context(**fields).reasons)


if __name__ == '__main__':
    unittest.main()
