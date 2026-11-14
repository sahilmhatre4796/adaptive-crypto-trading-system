"""Lossless, allowlisted M9.3 journal encoding. No payload-directed imports."""
from __future__ import annotations

from dataclasses import fields, is_dataclass
from decimal import Decimal
from functools import lru_cache
from hashlib import sha256
import importlib
import json
from math import isfinite

import pandas as pd

from trading.pattern_context import utc_timestamp


SCHEMA = 'M9.3-codec-v1'
MODULES = (
    'trading.patterns', 'trading.pattern_context', 'trading.pattern_engine',
    'trading.structure', 'trading.m6_data', 'trading.regime',
    'trading.derivatives', 'trading.liquidation', 'trading.m6',
    'trading.signal_context', 'trading.signals', 'trading.portfolio',
    'trading.risk', 'trading.freqtrade_bridge', 'trading.notifications',
    'trading.notification_outbox', 'trading.orchestration',
)


@lru_cache(maxsize=1)
def _classes() -> dict[str, type]:
    result = {}
    for name in MODULES:
        module = importlib.import_module(name)
        for value in vars(module).values():
            if isinstance(value, type) and is_dataclass(value) and value.__module__ == name:
                result[name + '.' + value.__name__] = value
    return result


def _tree(value):
    if value is None:
        return {'t': 'none'}
    if type(value) is bool:
        return {'t': 'bool', 'v': value}
    if type(value) is int:
        return {'t': 'int', 'v': str(value)}
    if type(value) is float:
        if not isfinite(value):
            raise ValueError('Nonfinite journal float')
        return {'t': 'float', 'v': repr(value)}
    if type(value) is str:
        return {'t': 'str', 'v': value}
    if isinstance(value, Decimal):
        if not value.is_finite():
            raise ValueError('Nonfinite journal decimal')
        return {'t': 'decimal', 'v': str(value)}
    if isinstance(value, pd.Timestamp):
        at = utc_timestamp(value)
        return {'t': 'timestamp', 'v': str(at.value)}
    if isinstance(value, pd.Timedelta):
        return {'t': 'timedelta', 'v': str(value.value)}
    if type(value) in (tuple, list):
        return {'t': 'tuple' if type(value) is tuple else 'list',
                'v': [_tree(item) for item in value]}
    if type(value) is dict:
        if any(type(key) is not str for key in value):
            raise ValueError('Journal dictionaries require string keys')
        return {'t': 'dict', 'v': [[key, _tree(value[key])] for key in sorted(value)]}
    if is_dataclass(value) and not isinstance(value, type):
        name = type(value).__module__ + '.' + type(value).__name__
        if _classes().get(name) is not type(value):
            raise ValueError('Unregistered journal model')
        return {'t': 'model', 'c': name,
                'v': [[field.name, _tree(getattr(value, field.name))] for field in fields(value)]}
    raise ValueError(f'Unsupported journal value: {type(value).__name__}')


def _from_tree(node):
    if type(node) is not dict or type(node.get('t')) is not str:
        raise ValueError('Malformed journal value')
    tag = node['t']
    if tag == 'none' and set(node) == {'t'}:
        return None
    if set(node) == {'t', 'v'}:
        value = node['v']
        if tag == 'bool' and type(value) is bool:
            return value
        if tag == 'int' and type(value) is str:
            result = int(value)
            if str(result) == value:
                return result
        if tag == 'float' and type(value) is str:
            result = float(value)
            if isfinite(result) and repr(result) == value:
                return result
        if tag == 'str' and type(value) is str:
            return value
        if tag == 'decimal' and type(value) is str:
            result = Decimal(value)
            if result.is_finite() and str(result) == value:
                return result
        if tag == 'timestamp' and type(value) is str:
            result = pd.Timestamp(int(value), tz='UTC')
            if str(result.value) == value:
                return result
        if tag == 'timedelta' and type(value) is str:
            result = pd.Timedelta(int(value), unit='ns')
            if str(result.value) == value:
                return result
        if tag in ('tuple', 'list') and type(value) is list:
            decoded = [_from_tree(item) for item in value]
            return tuple(decoded) if tag == 'tuple' else decoded
        if tag == 'dict' and type(value) is list:
            if any(type(pair) is not list or len(pair) != 2 or type(pair[0]) is not str for pair in value):
                raise ValueError('Malformed journal dictionary')
            keys = [pair[0] for pair in value]
            if keys != sorted(set(keys)):
                raise ValueError('Noncanonical journal dictionary')
            return {key: _from_tree(item) for key, item in value}
    if tag == 'model' and set(node) == {'t', 'c', 'v'}:
        cls = _classes().get(node['c']) if type(node['c']) is str else None
        values = node['v']
        if cls is None or type(values) is not list or any(type(pair) is not list or len(pair) != 2 for pair in values):
            raise ValueError('Unknown or malformed journal model')
        declared = fields(cls)
        if [pair[0] for pair in values] != [field.name for field in declared]:
            raise ValueError('Journal model fields changed')
        decoded = {key: _from_tree(item) for key, item in values}
        model = cls(**{field.name: decoded[field.name] for field in declared if field.init})
        if any(getattr(model, field.name) != decoded[field.name] for field in declared):
            raise ValueError('Journal model identity changed')
        if node['c'] == 'trading.m6.M6Bundle':
            type(model).from_dict(model.to_dict())
        return model
    raise ValueError('Malformed journal tag or value')


def encode(value) -> str:
    return json.dumps({'schema': SCHEMA, 'value': _tree(value)},
                      sort_keys=True, separators=(',', ':'), ensure_ascii=False, allow_nan=False)


def decode(data: str):
    if type(data) is not str:
        raise ValueError('Journal payload must be text')
    try:
        packet = json.loads(data)
        if type(packet) is not dict or set(packet) != {'schema', 'value'} or packet['schema'] != SCHEMA:
            raise ValueError('Unsupported journal schema')
        result = _from_tree(packet['value'])
        if encode(result) != data:
            raise ValueError('Noncanonical journal payload')
        return result
    except (TypeError, KeyError, OverflowError, ArithmeticError) as exc:
        raise ValueError('Invalid journal payload') from exc


def digest(data: str) -> str:
    return sha256(data.encode('utf-8')).hexdigest()


def identity(*values) -> str:
    return digest(encode(values))
