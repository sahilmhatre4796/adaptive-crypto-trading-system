import sys
sys.dont_write_bytecode = True

from decimal import Decimal
import json
import unittest

import pandas as pd

from trading.replay_codec import decode, digest, encode, identity
from trading.orchestration import ReplayEnvelope, ReplayFrame, RunSpec
from trading.notifications import TemplateConfig
from test_freqtrade_bridge import BINDINGS
from test_risk import config
from test_signal_context import CONFIG, START, inputs
from test_signals import policy


def run_spec(priority=('BTC/USDT', 'ETH/USDT'), template=None):
    return RunSpec(START, Decimal(2000), 'INR', policy(), CONFIG, config(),
                   BINDINGS, template or TemplateConfig(12000), priority)


class ReplayCodecTests(unittest.TestCase):
    def test_typed_numeric_timestamp_and_config_round_trip(self):
        value = (1, 1.0, Decimal('1.00'), pd.Timestamp('2026-01-01T00:00:00.000000001Z'),
                 run_spec(), {'a': (True, None, 'text')})
        restored = decode(encode(value))
        self.assertEqual(restored, value)
        self.assertEqual(tuple(type(item) for item in restored[:3]), (int, float, Decimal))
        self.assertNotEqual(identity(1), identity(1.0))
        self.assertEqual(digest(encode(value)), digest(encode(restored)))

    def test_actual_m3_m5_m6_inputs_round_trip_without_renaming_sources(self):
        fields = inputs()
        value = (fields['pattern'], fields['candle'], fields['structure'],
                 fields['structure_candle'], fields['m6'])
        restored = decode(encode(value))
        self.assertEqual(restored, value)
        self.assertEqual(restored[0].event_id, fields['pattern'].event_id)
        self.assertEqual(restored[-1].to_dict(), fields['m6'].to_dict())

    def test_frame_hash_and_source_times_round_trip(self):
        at = START+pd.Timedelta(minutes=80)
        command = ReplayEnvelope('mark-1', 'MARK', 'fixture', at, at, at, 0,
                                 {'kind': 'fixture'})
        frame = ReplayFrame('frame-1', at, (command,))
        self.assertEqual(decode(encode(frame)), frame)
        self.assertEqual(decode(encode(frame)).frame_hash, frame.frame_hash)

    def test_rejects_unknown_types_versions_noncanonical_data_and_nonfinite_numbers(self):
        for value in (float('nan'), float('inf'), object(), {'a': object()}):
            with self.assertRaises(ValueError):
                encode(value)
        packet = json.loads(encode((1,)))
        packet['schema'] = 'M9.3-codec-v999'
        with self.assertRaises(ValueError):
            decode(json.dumps(packet))
        with self.assertRaises(ValueError):
            decode(encode((1,)).replace('"v":"1"', '"v":"01"'))
        with self.assertRaises(ValueError):
            decode(encode(run_spec()).replace('trading.orchestration.RunSpec',
                                              'untrusted.RunSpec'))

    def test_run_requires_explicit_priority_and_backtest_only(self):
        with self.assertRaises(ValueError):
            run_spec(priority=('BTC/USDT', 'BTC/USDT'))
        with self.assertRaises(ValueError):
            RunSpec(START, 2000, 'INR', policy(), CONFIG, config(), BINDINGS,
                    TemplateConfig(12000), ('BTC/USDT', 'ETH/USDT'), 'CURRENT')


if __name__ == '__main__':
    unittest.main()
