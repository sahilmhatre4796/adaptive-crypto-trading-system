"""Validate public Binance risk facts without choosing fees, FX, or leverage."""
from dataclasses import dataclass
from decimal import Decimal

import pandas as pd

from trading.m13_feed import SYMBOLS
from trading.pattern_context import utc_timestamp
from trading.portfolio import InstrumentSpec, Quote, number
from trading.risk import CostModel, RiskConfig


@dataclass(frozen=True)
class PublicExchangeRules:
    symbol: str
    price_tick: Decimal
    quantity_step: Decimal
    min_quantity: Decimal
    min_notional: Decimal
    retrieved_at: pd.Timestamp
    source: str = 'https://fapi.binance.com/fapi/v1/exchangeInfo'


@dataclass(frozen=True)
class PublicFundingFact:
    symbol: str
    settled_at: pd.Timestamp
    received_at: pd.Timestamp
    rate: Decimal
    mark_price: Decimal
    source: str = 'https://fapi.binance.com/fapi/v1/fundingRate'


def _milliseconds(value, name):
    if type(value) is not int or value < 0:
        raise ValueError(f'Invalid {name} milliseconds')
    return pd.Timestamp(value, unit='ms', tz='UTC')


def parse_exchange_info(payload, symbol, *, retrieved_at):
    at = utc_timestamp(retrieved_at)
    if symbol not in SYMBOLS or type(payload) is not dict or type(payload.get('symbols')) is not list:
        raise ValueError('Official exchange information required')
    matches = [row for row in payload['symbols'] if type(row) is dict and row.get('symbol') == symbol]
    if len(matches) != 1:
        raise ValueError('Exactly one requested USD-M symbol required')
    row = matches[0]
    if (row.get('status') != 'TRADING' or row.get('contractType') != 'PERPETUAL'
            or row.get('quoteAsset') != 'USDT' or row.get('marginAsset') != 'USDT'
            or type(row.get('filters')) is not list):
        raise ValueError('Active linear USDT perpetual contract required')
    filters = {}
    for entry in row['filters']:
        if type(entry) is not dict or type(entry.get('filterType')) is not str:
            raise ValueError('Malformed exchange filter')
        name = entry['filterType']
        if name in filters:
            raise ValueError('Duplicate exchange filter')
        filters[name] = entry
    try:
        price_tick = number(filters['PRICE_FILTER']['tickSize'], positive=True)
        lot = filters.get('MARKET_LOT_SIZE') or filters['LOT_SIZE']
        quantity_step = number(lot['stepSize'], positive=True)
        min_quantity = number(lot['minQty'], nonnegative=True)
        notional = filters.get('MIN_NOTIONAL') or filters.get('NOTIONAL')
        if notional is None:
            raise ValueError('Missing exchange minimum notional')
        min_notional = number(notional.get('notional', notional.get('minNotional')),
                              nonnegative=True)
    except (KeyError, TypeError) as exc:
        raise ValueError('Incomplete exchange rules') from exc
    return PublicExchangeRules(SYMBOLS[symbol], price_tick, quantity_step,
                               min_quantity, min_notional, at)


def parse_mark_price(payload, symbol, *, received_at):
    at = utc_timestamp(received_at)
    if (symbol not in SYMBOLS or type(payload) is not dict
            or payload.get('symbol') != symbol):
        raise ValueError('Matching public mark-price observation required')
    observed = _milliseconds(payload.get('time'), 'mark time')
    if observed > at:
        raise ValueError('Future public mark price')
    return Quote(SYMBOLS[symbol], number(payload.get('markPrice'), positive=True),
                 observed, at, 'https://fapi.binance.com/fapi/v1/premiumIndex')


def parse_funding_rate(payload, symbol, *, received_at):
    at = utc_timestamp(received_at)
    if (symbol not in SYMBOLS or type(payload) is not dict
            or payload.get('symbol') != symbol):
        raise ValueError('Matching funding settlement fact required')
    settled = _milliseconds(payload.get('fundingTime'), 'funding time')
    if settled > at:
        raise ValueError('Future funding settlement')
    return PublicFundingFact(SYMBOLS[symbol], settled, at,
                             number(payload.get('fundingRate')),
                             number(payload.get('markPrice'), positive=True))


def assemble_frame_risk_inputs(rules, mark, fx, costs, *, leverage,
                               multiplier, max_leverage, correlation_group,
                               risk_config, as_of):
    """Require every external assumption before M8 can evaluate a live signal."""
    at = utc_timestamp(as_of)
    if (not isinstance(rules, PublicExchangeRules) or not isinstance(mark, Quote)
            or not isinstance(fx, Quote) or not isinstance(costs, CostModel)
            or not isinstance(risk_config, RiskConfig)
            or risk_config.risk_fraction != Decimal('.10')
            or mark.key != rules.symbol or fx.key != 'USDT'
            or not isinstance(correlation_group, str) or not correlation_group.strip()):
        raise ValueError('Complete typed public rules, mark, FX and approved costs required')
    lev = number(leverage, positive=True)
    multiplier = number(multiplier, positive=True)
    maximum = number(max_leverage, positive=True)
    if lev > maximum or maximum > risk_config.max_leverage:
        raise ValueError('Leverage exceeds explicitly configured risk cap')
    if (max(rules.retrieved_at, mark.available_at, fx.available_at,
            costs.available_at) > at
            or at-rules.retrieved_at > pd.Timedelta(seconds=float(risk_config.max_spec_age_seconds))
            or at-mark.observed_at > pd.Timedelta(seconds=float(risk_config.max_quote_age_seconds))
            or at-fx.observed_at > pd.Timedelta(seconds=float(risk_config.max_fx_age_seconds))):
        raise ValueError('Required risk input is future-dated or stale')
    spec = InstrumentSpec(rules.symbol, 'USDT', multiplier, rules.quantity_step,
                          rules.price_tick, rules.min_quantity, rules.min_notional,
                          maximum, correlation_group, rules.source, rules.retrieved_at)
    return dict(mark=mark, fx=fx, spec=spec, costs=costs, leverage=lev)
