"""Offline pattern evidence export; no execution or account connections."""
import argparse
from dataclasses import asdict
from hashlib import sha256
from io import BytesIO
import json
from pathlib import Path
import pandas as pd
from trading.patterns import PatternConfig
from trading.pattern_context import ContextConfig, ContextSnapshot, utc_timestamp
from trading.pattern_engine import PatternEngine, TIMEFRAMES, SYMBOLS, resample_pattern_bars


def _json(value):
    if isinstance(value, pd.Timestamp):
        return value.isoformat()
    raise TypeError(f'Unsupported JSON type: {type(value).__name__}')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--csv', required=True)
    parser.add_argument('--symbol', required=True, choices=SYMBOLS)
    parser.add_argument('--timeframe', default='15m', choices=TIMEFRAMES)
    parser.add_argument('--input-timeframe', default='15m', choices=TIMEFRAMES)
    parser.add_argument('--as-of')
    parser.add_argument('--config-json', help='Optional geometry/context configuration objects')
    parser.add_argument('--contexts-json', help='Optional array of timestamped context snapshots')
    parser.add_argument('--out', required=True, help='New evidence directory; never overwrite')
    args = parser.parse_args()
    config = json.loads(Path(args.config_json).read_text()) if args.config_json else {}
    if not isinstance(config, dict) or set(config)-{'geometry','context'}:
        raise ValueError('Configuration requires only geometry/context objects')
    engine = PatternEngine(PatternConfig(**config.get('geometry', {})), ContextConfig(**config.get('context', {})))
    context_bytes = Path(args.contexts_json).read_bytes() if args.contexts_json else None
    context_values = json.loads(context_bytes) if context_bytes is not None else []
    if not isinstance(context_values, list):
        raise ValueError('Contexts must be a JSON array')
    contexts = [ContextSnapshot(**item) for item in context_values]
    data_bytes = Path(args.csv).read_bytes()
    frame = pd.read_csv(BytesIO(data_bytes))
    as_of = utc_timestamp(args.as_of if args.as_of is not None else pd.Timestamp.now(tz='UTC'))
    if args.input_timeframe != args.timeframe:
        if args.input_timeframe != '5m':
            raise ValueError('Only completed 5m input can be resampled by this exporter')
        frame = resample_pattern_bars(frame, args.timeframe, as_of=as_of)
    events = engine.scan(frame, args.symbol, args.timeframe, as_of=as_of, contexts=contexts)
    report = dict(symbol=args.symbol,timeframe=args.timeframe,events=len(events),
                  input_sha256=sha256(data_bytes).hexdigest(),as_of=as_of.isoformat(),
                  contexts_sha256=sha256(context_bytes).hexdigest() if context_bytes is not None else None,
                  configuration_id=engine.configuration_id,
                  configuration=dict(geometry=asdict(engine.config),context=asdict(engine.context_config)),
                  purpose='pattern evidence only; no trade or performance claims',
                  context_supplied=bool(contexts))
    out = Path(args.out)
    out.mkdir(parents=True,exist_ok=False)
    (out/'patterns.json').write_text(json.dumps([asdict(e) for e in events],default=_json,indent=2,allow_nan=False))
    (out/'summary.json').write_text(json.dumps(report,indent=2,allow_nan=False))
    print(json.dumps(report,indent=2,allow_nan=False))


if __name__=='__main__': main()
