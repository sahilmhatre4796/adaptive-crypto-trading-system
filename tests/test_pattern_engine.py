import sys
sys.dont_write_bytecode = True

from dataclasses import replace
from dataclasses import FrozenInstanceError
import json
from pathlib import Path
import subprocess
import uuid
import unittest
import numpy as np
import pandas as pd
from trading.patterns import OHLC, PatternConfig, engulfing, wick_reversals, harami
from trading.pattern_context import (ContextConfig, ContextSnapshot, assess_context,
                                    preceding_direction)
from trading.pattern_engine import (PatternEngine, ClosedCandle, SetupTracker,
                                   evaluate_setup, resample_pattern_bars)
from test_patterns_geometry import mirror


START = pd.Timestamp('2025-01-01', tz='UTC')
DOWN = [OHLC(112, 113, 109, 110), OHLC(110, 111, 107, 108), OHLC(108, 109, 105, 106)]
UP = [OHLC(94, 97, 93, 96), OHLC(96, 99, 95, 98), OHLC(98, 101, 97, 100)]
HARAMI = [OHLC(105, 106, 94, 95), OHLC(97, 100, 96, 99)]


def frame(candles, timeframe='15min', start=START):
    return pd.DataFrame([dict(timestamp=ts, open=c.open, high=c.high, low=c.low, close=c.close)
                         for ts, c in zip(pd.date_range(start, periods=len(candles), freq=timeframe), candles)])


def context(at, classification='BULLISH', symbol='BTC/USDT', **kwargs):
    at = pd.Timestamp(at)
    fields = dict(symbol=symbol, available_at=at, higher_direction=classification, regime='RANGE',
                  direction_closed_at=at.floor('1h'), regime_closed_at=at.floor('4h'),
                  support_distance_atr=.2, resistance_distance_atr=.2, atr=10, relative_volume=1.5)
    fields.update(kwargs)
    return ContextSnapshot(**fields)


def harami_event(symbol='BTC/USDT'):
    raw = frame(DOWN+HARAMI)
    at = raw.timestamp.iloc[-1]+pd.Timedelta(minutes=15)
    snapshot = context(at, symbol=symbol)
    events = PatternEngine().scan(raw, symbol, as_of=at, contexts=[snapshot])
    return next(e for e in events if e.family == 'HARAMI' and e.confirmed_at == at), snapshot


class ContextTests(unittest.TestCase):
    def test_preceding_direction_uses_history_only(self):
        self.assertEqual(preceding_direction(DOWN)[0], 'BEARISH')
        self.assertEqual(preceding_direction(UP)[0], 'BULLISH')
        self.assertEqual(preceding_direction(DOWN[:2])[0], 'UNKNOWN')
        self.assertEqual(preceding_direction([OHLC(100, 100, 100, 100)]*3)[0], 'UNKNOWN')

    def test_same_wick_geometry_different_trend_names(self):
        for candle, bull_name, bear_name in (
            (OHLC(98, 100, 90, 99.5), 'hammer', 'hanging_man'),
            (OHLC(92, 100, 90, 90.5), 'inverted_hammer', 'shooting_star'),
        ):
            match = wick_reversals([candle])[0]
            at = START+pd.Timedelta(hours=1)
            bullish = assess_context(match, 'BEARISH', 10, context(at), at)
            bearish = assess_context(match, 'BULLISH', 10, context(at, 'BEARISH'), at)
            self.assertEqual((bullish.name, bullish.classification), (bull_name, 'BULLISH'))
            self.assertEqual((bearish.name, bearish.classification), (bear_name, 'BEARISH'))
            unknown = assess_context(match, 'NEUTRAL', 10, context(at), at)
            self.assertEqual(unknown.classification, 'NEUTRAL')
            self.assertNotEqual(unknown.status, 'VALIDATED')

    def test_context_missing_conflicting_and_weak_volume(self):
        event, snapshot = harami_event()
        self.assertEqual(event.context.status, 'VALIDATED')
        self.assertTrue(0 <= event.strength_score <= 100)
        for supplied, expected in (
            (None, 'INSUFFICIENT'),
            (replace(snapshot, higher_direction='BEARISH'), 'REJECTED'),
            (replace(snapshot, regime='ABNORMAL'), 'REJECTED'),
            (replace(snapshot, support_distance_atr=5), 'REJECTED'),
            (replace(snapshot, support_distance_atr=None), 'INSUFFICIENT'),
            (replace(snapshot, relative_volume=.1), 'REJECTED'),
            (replace(snapshot, atr=.01), 'REJECTED'),
            (replace(snapshot, available_at=snapshot.available_at+pd.Timedelta(seconds=1)), 'INSUFFICIENT'),
        ):
            result = assess_context(event.geometry, 'BEARISH', event.pattern_range, supplied, event.confirmed_at)
            self.assertEqual(result.status, expected)
        self.assertEqual(assess_context(event.geometry, 'BULLISH', 12, snapshot, event.confirmed_at).status, 'REJECTED')

    def test_bad_future_higher_timeframes_and_stale_evidence(self):
        at = START+pd.Timedelta(hours=4)
        with self.assertRaises(ValueError): context(at, direction_closed_at=at+pd.Timedelta(hours=1))
        with self.assertRaises(ValueError): ContextSnapshot('BTC/USDT', at, 'BULLISH')
        with self.assertRaises(ValueError): ContextSnapshot('BTC/USDT', at, 'BULLISH', direction_closed_at=at+pd.Timedelta(minutes=1))
        event, _ = harami_event()
        stale = replace(context(at), direction_closed_at=START, regime_closed_at=START-pd.Timedelta(hours=4))
        assessment = assess_context(event.geometry, 'BEARISH', 12, stale, at)
        self.assertIn('stale_1h_direction', assessment.missing)
        self.assertIn('stale_4h_regime', assessment.missing)


class EngineTests(unittest.TestCase):
    def test_all_twelve_variants_confirm_through_independent_5m_gate(self):
        cases = (
            (HARAMI, 'HARAMI', 'bullish_harami', 'bearish_harami'),
            ([OHLC(103,104,99,100), OHLC(99.9,104,99,103.1)],
             'ENGULFING', 'bullish_engulfing', 'bearish_engulfing'),
            ([OHLC(100,105,95,101), OHLC(101,104,96,100), OHLC(100,103,94,95), OHLC(95,105,94.5,104.5)],
             'HIKKAKE', 'bullish_hikkake', 'bearish_hikkake'),
            ([OHLC(98,100,90,99.5)], 'HAMMER_HANGING_MAN', 'hammer', 'hanging_man'),
            ([OHLC(92,100,90,90.5)], 'SHOOTING_STAR_INVERTED_HAMMER', 'inverted_hammer', 'shooting_star'),
            ([OHLC(105,106,94,95), OHLC(95,96,94,95.2), OHLC(95.2,102,95,101)],
             'MORNING_EVENING_STAR', 'morning_star', 'evening_star'),
        )
        for formation, family, bull_name, bear_name in cases:
            for classification, name, history in (('BULLISH', bull_name, DOWN), ('BEARISH', bear_name, UP)):
                with self.subTest(name=name):
                    prices = formation if classification == 'BULLISH' or len(formation) == 1 else mirror(formation)
                    raw = frame(history+prices)
                    at = raw.timestamp.iloc[-1]+pd.Timedelta(minutes=15)
                    snapshot = context(at, classification)
                    event = next(e for e in PatternEngine().scan(raw, 'BTC/USDT', as_of=at, contexts=[snapshot])
                                 if e.family == family and e.price_confirmed_at == at)
                    self.assertEqual((event.name, event.classification, event.context.status), (name, classification, 'VALIDATED'))
                    price = event.confirmation_close
                    ohlc = OHLC(price, price+2, price-.2, price+1) if classification == 'BULLISH' else OHLC(price, price+.2, price-2, price-1)
                    bar = ClosedCandle(at, ohlc, event.symbol)
                    decision = evaluate_setup(event, bar, as_of=bar.available_at, context=snapshot)
                    self.assertEqual((decision.status, decision.classification), ('CONFIRMED', classification))

    def test_event_schema_and_both_assets(self):
        for symbol in ('BTC/USDT', 'ETH/USDT'):
            event, _ = harami_event(symbol)
            self.assertEqual(event.symbol, symbol)
            self.assertEqual(event.name, 'bullish_harami')
            self.assertEqual(event.classification, 'BULLISH')
            self.assertEqual(event.timeframe, '15m')
            self.assertEqual(event.detected_at, event.confirmed_at)
            self.assertEqual(event.expires_at-event.price_confirmed_at, pd.Timedelta(hours=1))
            self.assertEqual(event.invalidation_price, 94)
            self.assertEqual(len(event.candle_references), 2)
            self.assertEqual(len(event.candle_geometry), 2)
            self.assertEqual(len(event.event_id), 64)

    def test_configuration_is_immutable_and_availability_is_monotonic(self):
        engine = PatternEngine()
        with self.assertRaises(FrozenInstanceError): engine.config = PatternConfig(body_tolerance=.03)
        raw = frame(DOWN+HARAMI)
        raw['available_at'] = raw.timestamp+pd.Timedelta(minutes=15)
        raw.loc[0,'available_at'] = raw.timestamp.iloc[-1]+pd.Timedelta(minutes=30)
        with self.assertRaises(ValueError): engine.scan(raw,'BTC/USDT')

    def test_hikkake_detection_and_confirmation_are_distinct(self):
        bars = [OHLC(100, 105, 95, 101), OHLC(101, 104, 96, 100), OHLC(100, 103, 94, 95),
                OHLC(95, 104.5, 94.5, 103), OHLC(103, 105, 95, 104.5)]
        raw = frame(bars)
        engine = PatternEngine()
        earlier = engine.scan(raw.iloc[:-1], 'BTC/USDT', as_of=START+pd.Timedelta(hours=1))
        self.assertFalse(any(e.family == 'HIKKAKE' for e in earlier))
        events = engine.scan(raw, 'BTC/USDT', as_of=START+pd.Timedelta(minutes=75))
        event = next(e for e in events if e.family == 'HIKKAKE')
        self.assertEqual(event.detected_at, START+pd.Timedelta(minutes=45))
        self.assertEqual(event.confirmed_at, START+pd.Timedelta(minutes=75))

    def test_unfinished_and_unavailable_candles(self):
        raw = frame(DOWN+HARAMI)
        at = START+pd.Timedelta(minutes=75)
        engine = PatternEngine()
        self.assertFalse(any(e.family == 'HARAMI' and e.price_confirmed_at == at
                             for e in engine.scan(raw, 'BTC/USDT', as_of=at-pd.Timedelta(seconds=1))))
        flagged = raw.copy(); flagged['is_closed'] = True; flagged.loc[4, 'is_closed'] = False
        with self.assertRaises(ValueError): engine.scan(flagged, 'BTC/USDT', as_of=at)
        delayed = raw.copy(); delayed['available_at'] = delayed.timestamp+pd.Timedelta(minutes=15)
        delayed.loc[4, 'available_at'] += pd.Timedelta(minutes=5)
        self.assertFalse(any(e.family == 'HARAMI' and e.price_confirmed_at == at
                             for e in engine.scan(delayed, 'BTC/USDT', as_of=at)))
        event = next(e for e in engine.scan(delayed, 'BTC/USDT', as_of=at+pd.Timedelta(minutes=5))
                     if e.family == 'HARAMI' and e.price_confirmed_at == at)
        self.assertEqual(event.confirmed_at, at+pd.Timedelta(minutes=5))

    def test_bad_timestamps_gaps_and_bounds(self):
        raw = frame(DOWN+HARAMI)
        for bad in (raw.drop(2), pd.concat([raw, raw.iloc[:1]])):
            with self.assertRaises(ValueError): PatternEngine().scan(bad, 'BTC/USDT')
        bad = raw.copy(); bad.loc[0, 'high'] = 1
        with self.assertRaises(ValueError): PatternEngine().scan(bad, 'BTC/USDT')
        bad = raw.copy(); bad['available_at'] = bad.timestamp
        with self.assertRaises(ValueError): PatternEngine().scan(bad, 'BTC/USDT')
        with self.assertRaises(ValueError): PatternEngine().scan(raw, 'DOGE/USDT')

    def test_prefix_invariance_and_stable_ids(self):
        raw = frame(DOWN+HARAMI+[OHLC(99, 103, 98, 102), OHLC(102, 104, 100, 101)])
        engine = PatternEngine()
        full = engine.scan(raw, 'BTC/USDT')
        for size in range(1, len(raw)+1):
            at = raw.timestamp.iloc[size-1]+pd.Timedelta(minutes=15)
            prefix = engine.scan(raw.iloc[:size], 'BTC/USDT', as_of=at)
            self.assertEqual(prefix, tuple(e for e in full if e.confirmed_at <= at))
            self.assertEqual(prefix, engine.scan(raw, 'BTC/USDT', as_of=at))
        self.assertEqual(len({e.event_id for e in full}), len(full))
        changed = PatternEngine(PatternConfig(body_tolerance=.03)).scan(raw, 'BTC/USDT')
        self.assertNotEqual(full[-1].configuration_id, changed[-1].configuration_id)

    def test_future_context_does_not_rewrite_past_events(self):
        raw = frame(DOWN+HARAMI)
        event, snapshot = harami_event()
        engine = PatternEngine()
        earlier = engine.scan(raw, 'BTC/USDT', contexts=[snapshot])
        later = replace(snapshot, available_at=snapshot.available_at+pd.Timedelta(minutes=5), higher_direction='BEARISH')
        self.assertEqual(earlier, engine.scan(raw, 'BTC/USDT', contexts=[snapshot, later]))
        with self.assertRaises(ValueError): engine.scan(raw, 'BTC/USDT', contexts=[snapshot, snapshot])

    def test_5m_resampling_never_uses_partial_15m(self):
        raw = frame([OHLC(100, 101, 99, 100)]*5, '5min')
        raw['volume'] = 10
        complete = resample_pattern_bars(raw)
        self.assertEqual(len(complete), 1)
        self.assertEqual(complete.iloc[0].timestamp, START)
        self.assertEqual(complete.iloc[0].available_at, START+pd.Timedelta(minutes=15))
        self.assertEqual(len(resample_pattern_bars(raw.iloc[:2])), 0)

    def test_all_timeframes_keep_their_own_close_availability(self):
        for name, rule, interval in (('5m','5min',5), ('15m','15min',15), ('1h','1h',60), ('4h','4h',240)):
            with self.subTest(timeframe=name):
                raw = frame(HARAMI, rule)
                at = START+pd.Timedelta(minutes=2*interval)
                engine = PatternEngine()
                self.assertFalse(any(e.family == 'HARAMI' for e in engine.scan(raw,'BTC/USDT',name,as_of=at-pd.Timedelta(seconds=1))))
                event = next(e for e in engine.scan(raw,'BTC/USDT',name,as_of=at) if e.family == 'HARAMI')
                self.assertEqual((event.timeframe,event.confirmed_at),(name,at))

    def test_validated_index_and_declared_symbol(self):
        raw = frame(DOWN+HARAMI)
        raw.index = pd.DatetimeIndex(raw.timestamp, name='timestamp')
        raw['symbol'] = 'BTC/USDT'
        self.assertTrue(PatternEngine().scan(raw,'BTC/USDT'))
        with self.assertRaises(ValueError): PatternEngine().scan(raw,'ETH/USDT')

    def test_randomized_prefix_and_future_price_mutation(self):
        rng = np.random.default_rng(23)
        close = 100 + np.cumsum(rng.normal(0,.6,200))
        opens = np.r_[100, close[:-1]]
        raw = frame([OHLC(o,max(o,c)+.1,min(o,c)-.1,c) for o,c in zip(opens,close)])
        engine = PatternEngine()
        full = engine.scan(raw,'ETH/USDT')
        self.assertTrue(full)
        for n in (37,63,128):
            at = raw.timestamp.iloc[n-1]+pd.Timedelta(minutes=15)
            prefix = engine.scan(raw.iloc[:n],'ETH/USDT',as_of=at)
            self.assertEqual(prefix,tuple(e for e in full if e.confirmed_at<=at))
            changed = raw.copy()
            changed.loc[n:,['open','high','low','close']] *= 2
            self.assertEqual(prefix,engine.scan(changed,'ETH/USDT',as_of=at))

    def test_delayed_pattern_expiry_uses_price_time_not_receipt(self):
        raw = frame(DOWN+HARAMI)
        raw['available_at'] = raw.timestamp+pd.Timedelta(hours=2)
        event = next(e for e in PatternEngine().scan(raw,'BTC/USDT') if e.family=='HARAMI' and e.candle_references[-1]==raw.timestamp.iloc[-1])
        self.assertLess(event.expires_at,event.confirmed_at)
        opened = event.confirmed_at.ceil('5min')
        bar = ClosedCandle(opened,OHLC(99,102,98,101),event.symbol)
        self.assertEqual(evaluate_setup(event,bar,as_of=bar.available_at).status,'EXPIRED')

    def test_offline_exporter_retains_configuration_and_context(self):
        raw = frame(DOWN+HARAMI)
        event, snapshot = harami_event()
        root = Path('runs')/('m3-cli-test-'+uuid.uuid4().hex)
        root.mkdir(parents=True)
        raw.to_csv(root/'input.csv',index=False)
        from dataclasses import asdict
        (root/'contexts.json').write_text(json.dumps([asdict(snapshot)],default=lambda v:v.isoformat()))
        command = [sys.executable,'-B','scan_patterns.py','--csv',str(root/'input.csv'),'--symbol','BTC/USDT',
                   '--contexts-json',str(root/'contexts.json'),'--as-of',event.confirmed_at.isoformat(),'--out',str(root/'out')]
        result = subprocess.run(command,capture_output=True,text=True)
        self.assertEqual(result.returncode,0,result.stderr)
        payload = json.loads((root/'out'/'patterns.json').read_text())
        own = next(e for e in payload if e['event_id']==event.event_id)
        self.assertEqual(own['context']['status'],'VALIDATED')
        self.assertEqual(own['geometry_config']['hikkake_confirmation_bars'],3)
        self.assertEqual(own['context_snapshot']['symbol'],'BTC/USDT')
        self.assertIn('body_position',dict(own['candle_geometry'][0]))
        summary = json.loads((root/'out'/'summary.json').read_text())
        self.assertEqual(summary['as_of'],event.confirmed_at.isoformat())
        self.assertIsNotNone(summary['contexts_sha256'])
        self.assertNotEqual(subprocess.run(command,capture_output=True).returncode,0)


class SetupTests(unittest.TestCase):
    def test_symbol_and_configuration_cannot_be_overridden(self):
        event, snapshot = harami_event()
        wrong = ClosedCandle(event.confirmed_at,OHLC(99,102,98,101),'ETH/USDT')
        with self.assertRaises(ValueError): evaluate_setup(event,wrong,as_of=wrong.available_at,context=snapshot)
        good = replace(wrong,symbol=event.symbol)
        with self.assertRaises(ValueError):
            evaluate_setup(event,good,as_of=good.available_at,context=snapshot,
                           context_config=ContextConfig(max_level_distance_atr=2))
        with self.assertRaises(ValueError):
            evaluate_setup(event,good,as_of=good.available_at,context=replace(snapshot,symbol='ETH/USDT'))
        self.assertEqual(event.context_config,ContextConfig())
        self.assertEqual(event.geometry_config,PatternConfig())

    def test_confirmation_duplicate_and_event_immutability(self):
        event, snapshot = harami_event()
        bar = ClosedCandle(event.confirmed_at, OHLC(99, 102, 98, 101), event.symbol)
        tracker = SetupTracker()
        decision = tracker.observe(event, bar, as_of=bar.available_at, context=snapshot)
        self.assertEqual((decision.status, decision.classification), ('CONFIRMED', 'BULLISH'))
        self.assertEqual(tracker.observe(event, bar, as_of=bar.available_at, context=snapshot).status, 'DUPLICATE')
        self.assertEqual(event.confirmed_at, snapshot.available_at)
        with self.assertRaises(ValueError): tracker.observe(replace(event, strength_score=1), bar, as_of=bar.available_at)

    def test_entry_bar_must_begin_after_15m_availability(self):
        event, snapshot = harami_event()
        overlap = ClosedCandle(event.confirmed_at-pd.Timedelta(minutes=5), OHLC(99, 102, 98, 101), event.symbol)
        decision = evaluate_setup(event, overlap, as_of=overlap.available_at, context=snapshot)
        self.assertEqual(decision.status, 'WAITING')
        future = ClosedCandle(event.confirmed_at, OHLC(99, 102, 98, 101), event.symbol)
        with self.assertRaises(ValueError): evaluate_setup(event, future, as_of=future.closed_at-pd.Timedelta(seconds=1))

    def test_stop_first_invalidation_expiry_and_stale_bar(self):
        event, snapshot = harami_event()
        invalid = ClosedCandle(event.confirmed_at, OHLC(99, 105, 93, 102), event.symbol)
        self.assertEqual(evaluate_setup(event, invalid, as_of=invalid.available_at, context=snapshot).status, 'INVALIDATED')
        good = ClosedCandle(event.confirmed_at, OHLC(99, 102, 98, 101), event.symbol)
        self.assertEqual(evaluate_setup(event, good, as_of=event.expires_at, context=snapshot).status, 'EXPIRED')
        self.assertEqual(evaluate_setup(event, good, as_of=good.available_at+pd.Timedelta(minutes=5), context=snapshot).status, 'WAITING')

    def test_missing_context_and_doji_never_confirm(self):
        event, snapshot = harami_event()
        bar = ClosedCandle(event.confirmed_at, OHLC(99, 102, 98, 101), event.symbol)
        self.assertEqual(evaluate_setup(event, bar, as_of=bar.available_at).status, 'WAITING')
        raw = frame(DOWN+[OHLC(99, 105, 95, 99)])
        neutral = next(e for e in PatternEngine().scan(raw, 'BTC/USDT') if e.family == 'DOJI')
        decision = evaluate_setup(neutral, ClosedCandle(neutral.confirmed_at, OHLC(99, 110, 98, 109), neutral.symbol),
                                  as_of=neutral.confirmed_at+pd.Timedelta(minutes=5), context=snapshot)
        self.assertEqual((decision.status, decision.classification), ('REJECTED', 'NEUTRAL'))


if __name__ == '__main__': unittest.main()
