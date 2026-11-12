from copy import deepcopy
import unittest

from trading.m6_adapters import (binance_funding_history, binance_open_interest,
                                 coinglass_model1_snapshot)
from test_m6_data import START, STEP


def coinglass_payload():
    return {'code': '0', 'data': {'y_axis': [95, 105],
                                'price_candlesticks': [[int(START.timestamp()), '100', '101', '99', '100', '5'],
                                                       [int((START+STEP).timestamp()), '100', '101', '99', '100', '5']],
                                'liquidation_leverage_data': [[0, 0, 999], [1, 0, 100], [1, 1, 200]],
                                'update_time': int((START+2*STEP).timestamp()*1000)}}


class AdapterTests(unittest.TestCase):
    def parse(self, payload):
        return coinglass_model1_snapshot(payload, symbol='BTC/USDT', venue='Binance', instrument='BTCUSDT', received_at=START+3*STEP)

    def test_binance_oi_string_values_epoch_ms_and_provenance(self):
        for symbol in ('BTCUSDT', 'ETHUSDT'):
            result = binance_open_interest({'symbol': symbol, 'openInterest': '123.5', 'time': int(START.timestamp()*1000)},
                                           received_at=START+STEP)
            self.assertEqual(result.value, 123.5)
            self.assertEqual(result.time.observed_at, START)
            self.assertEqual(result.time.available_at, START+STEP)
            self.assertFalse(result.time.historical_eligible)
            self.assertEqual(result.unit, 'base')

    def test_funding_history_does_not_backdate_receipt_or_assume_period(self):
        rows = [{'symbol': 'BTCUSDT', 'fundingRate': '-0.0005', 'fundingTime': int(START.timestamp()*1000)}]
        result, = binance_funding_history(rows, received_at=START+2*STEP, period_seconds=14400)
        self.assertEqual((result.value, result.period_seconds), (-.0005, 14400))
        self.assertEqual(result.time.available_at, START+2*STEP)
        self.assertFalse(result.time.historical_eligible)

    def test_coinglass_only_last_column_and_model_units(self):
        s = self.parse(coinglass_payload())
        self.assertEqual([c.intensity for c in s.clusters], [100, 200])
        self.assertEqual([c.side for c in s.clusters], ['LONG', 'SHORT'])
        self.assertTrue(all(c.side_basis == 'PRICE_RELATIVE_ESTIMATE' for c in s.clusters))
        self.assertEqual(s.intensity_unit, 'model1_intensity_not_currency')
        self.assertEqual((s.time.observed_at, s.time.available_at), (START+2*STEP, START+3*STEP))
        self.assertFalse(s.time.historical_eligible)
        self.assertEqual(s.supported_timeframes, ('snapshot',))
        self.assertEqual(s.timeframe, 'snapshot')

    def test_no_old_matrix_column_substituted_for_missing_latest_column(self):
        payload = coinglass_payload()
        payload['data']['liquidation_leverage_data'] = [[0, 0, 999]]
        self.assertEqual(self.parse(payload).clusters, ())

    def test_schema_errors_duplicate_out_of_range_and_malformed_cells(self):
        original = coinglass_payload()
        bads = []
        for field, value in (('code', '1'), ('data', {})):
            bad = deepcopy(original); bad[field] = value; bads.append(bad)
        for field, value in (('update_time', '1700000000'), ('y_axis', [95, 95]),
                              ('liquidation_leverage_data', [[1, 0, 2], [1, 0, 3]]),
                              ('liquidation_leverage_data', [[9, 0, 1]]),
                              ('liquidation_leverage_data', [[True, 0, 1]]),
                              ('liquidation_leverage_data', [[1, 0, -1]]),
                              ('liquidation_leverage_data', [[1, 0, float('nan')]])):
            bad = deepcopy(original); bad['data'][field] = value; bads.append(bad)
        for payload in bads:
            with self.assertRaises(ValueError):
                self.parse(payload)

    def test_errors_do_not_change_caller_payload(self):
        payload = coinglass_payload()
        saved = deepcopy(payload)
        self.parse(payload)
        self.assertEqual(payload, saved)
        for payload in ({}, {'symbol': 'DOGEUSDT'}, {'symbol': 'BTCUSDT', 'time': int(START.timestamp()*1000), 'openInterest': True}):
            with self.assertRaises(ValueError):
                binance_open_interest(payload, received_at=START+STEP)


if __name__ == '__main__':
    unittest.main()
