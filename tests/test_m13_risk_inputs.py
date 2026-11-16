"""Public exchange metadata is evidence; missing private assumptions block entries."""
import unittest
from decimal import Decimal as D

import pandas as pd

from trading.m13_risk_inputs import (parse_exchange_info, parse_mark_price,
                                     parse_funding_rate, assemble_frame_risk_inputs)
from trading.portfolio import Quote
from trading.risk import CostModel
from m13_public_poll import PublicKlinePoller, collect_frame_risk_inputs
from test_risk import config


AT = pd.Timestamp('2026-11-16T08:00:00Z')


def exchange_info():
    return {'symbols': [{
        'symbol': 'BTCUSDT', 'status': 'TRADING', 'contractType': 'PERPETUAL',
        'quoteAsset': 'USDT', 'marginAsset': 'USDT',
        'filters': [
            {'filterType': 'PRICE_FILTER', 'tickSize': '0.1'},
            {'filterType': 'LOT_SIZE', 'stepSize': '0.001', 'minQty': '0.001'},
            {'filterType': 'MIN_NOTIONAL', 'notional': '5'},
        ]}]}


class PublicRiskInputTests(unittest.TestCase):
    def test_automatic_public_collection_still_requires_fresh_private_assumptions(self):
        class FixturePoller(PublicKlinePoller):
            def __init__(self):
                super().__init__(session=object())
            def exchange_rules(self, symbol):
                return parse_exchange_info(exchange_info(), symbol, retrieved_at=AT)
            def mark_price(self, symbol):
                return Quote('BTC/USDT', D(100), AT, AT, 'public mark fixture')
        poller = FixturePoller()
        kwargs = dict(fx=Quote('USDT', D(83), AT, AT, 'explicit FX fixture'),
            costs=CostModel(D('.0005'), D('.0005'), D('.001'), D('.001'),
                            D('.0002'), 'explicit cost fixture', AT),
            leverage=2, multiplier=1, max_leverage=5,
            correlation_group='btc-eth', risk_config=config(), as_of=AT)
        result = collect_frame_risk_inputs(poller, 'BTC/USDT', **kwargs)
        self.assertEqual(result['spec'].source,
                         'https://fapi.binance.com/fapi/v1/exchangeInfo')
        with self.assertRaisesRegex(ValueError, 'Complete typed'):
            collect_frame_risk_inputs(poller, 'BTC/USDT', **{**kwargs, 'fx': None})
        with self.assertRaisesRegex(ValueError, 'stale'):
            collect_frame_risk_inputs(poller, 'BTC/USDT',
                **{**kwargs, 'as_of': AT+pd.Timedelta(seconds=301)})

    def test_public_rules_are_typed_and_do_not_invent_leverage_or_fees(self):
        rule = parse_exchange_info(exchange_info(), 'BTCUSDT', retrieved_at=AT)
        self.assertEqual((rule.symbol, rule.price_tick, rule.quantity_step,
                          rule.min_quantity, rule.min_notional),
                         ('BTC/USDT', D('.1'), D('.001'), D('.001'), D(5)))
        self.assertEqual(rule.retrieved_at, AT)
        self.assertFalse(hasattr(rule, 'fee_rate'))
        self.assertFalse(hasattr(rule, 'max_leverage'))

    def test_disabled_missing_duplicate_and_wrong_contracts_fail_closed(self):
        for change in (
                {'status': 'BREAK'}, {'contractType': 'CURRENT_QUARTER'},
                {'marginAsset': 'BTC'}, {'filters': []}):
            payload = exchange_info()
            payload['symbols'][0].update(change)
            with self.subTest(change=change), self.assertRaises(ValueError):
                parse_exchange_info(payload, 'BTCUSDT', retrieved_at=AT)
        payload = exchange_info()
        payload['symbols'].append(payload['symbols'][0].copy())
        with self.assertRaisesRegex(ValueError, 'Exactly one'):
            parse_exchange_info(payload, 'BTCUSDT', retrieved_at=AT)

    def test_mark_and_funding_preserve_distinct_observation_receipt_times(self):
        ms = int((AT-pd.Timedelta(seconds=2)).timestamp()*1000)
        mark = parse_mark_price({'symbol': 'BTCUSDT', 'markPrice': '100.25', 'time': ms},
                                'BTCUSDT', received_at=AT)
        self.assertEqual((mark.key, mark.value, mark.observed_at, mark.available_at),
                         ('BTC/USDT', D('100.25'), AT-pd.Timedelta(seconds=2), AT))
        funding = parse_funding_rate({'symbol': 'BTCUSDT', 'fundingTime': ms,
                                      'fundingRate': '-0.0001', 'markPrice': '100.25'},
                                     'BTCUSDT', received_at=AT)
        self.assertEqual((funding.rate, funding.settled_at, funding.received_at),
                         (D('-.0001'), AT-pd.Timedelta(seconds=2), AT))
        with self.assertRaisesRegex(ValueError, 'Future'):
            parse_mark_price({'symbol': 'BTCUSDT', 'markPrice': '100', 'time': ms+3000},
                             'BTCUSDT', received_at=AT)
        with self.assertRaises(ValueError):
            parse_funding_rate({'symbol': 'BTCUSDT', 'fundingTime': ms,
                                'fundingRate': 'NaN', 'markPrice': '100'},
                               'BTCUSDT', received_at=AT)

    def test_complete_explicit_risk_inputs_and_stale_or_missing_rejection(self):
        rules = parse_exchange_info(exchange_info(), 'BTCUSDT', retrieved_at=AT)
        mark = Quote('BTC/USDT', D(100), AT, AT, 'public mark fixture')
        fx = Quote('USDT', D(83), AT, AT, 'explicit FX fixture')
        costs = CostModel(D('.0004'), D('.0004'), D('.001'), D('.001'),
                          D('.0002'), 'explicit fee and slippage fixture', AT)
        args = dict(leverage=2, multiplier=1, max_leverage=5,
                    correlation_group='btc-eth', risk_config=config(), as_of=AT)
        bundle = assemble_frame_risk_inputs(rules, mark, fx, costs, **args)
        self.assertEqual((bundle['spec'].price_tick, bundle['spec'].min_notional,
                          bundle['fx'].value, bundle['leverage']),
                         (D('.1'), D(5), D(83), D(2)))
        with self.assertRaisesRegex(ValueError, 'Complete typed'):
            assemble_frame_risk_inputs(rules, mark, None, costs, **args)
        with self.assertRaisesRegex(ValueError, 'stale'):
            assemble_frame_risk_inputs(rules, mark, fx, costs,
                                       **{**args, 'as_of': AT+pd.Timedelta(seconds=301)})
        with self.assertRaisesRegex(ValueError, 'Leverage'):
            assemble_frame_risk_inputs(rules, mark, fx, costs,
                                       **{**args, 'leverage': 6})


if __name__ == '__main__':
    unittest.main()
