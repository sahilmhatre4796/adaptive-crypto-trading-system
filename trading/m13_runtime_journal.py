"""Fresh-session v2 journal identity over the preserved M12 import closure."""
from hashlib import sha256
from importlib import metadata
from pathlib import Path
import platform
import sys
import sysconfig

import numpy as np
import pandas as pd

from trading.paper_runtime_journal import RuntimeJournal


PRESERVED = tuple(sorted((
    'strategy.py', 'trading/__init__.py', 'trading/candle_driver.py',
    'trading/data.py', 'trading/derivatives.py', 'trading/exit_funding.py',
    'trading/freqtrade_bridge.py', 'trading/ledger.py', 'trading/liquidation.py',
    'trading/m6.py', 'trading/m6_data.py', 'trading/notification_outbox.py',
    'trading/notifications.py', 'trading/orchestration.py',
    'trading/paper_execution.py', 'trading/paper_journal.py',
    'trading/paper_runtime.py', 'trading/paper_runtime_journal.py',
    'trading/paper_state.py', 'trading/pattern_context.py',
    'trading/pattern_engine.py', 'trading/patterns.py', 'trading/portfolio.py',
    'trading/regime.py', 'trading/replay_codec.py', 'trading/replay_journal.py',
    'trading/risk.py', 'trading/signal_context.py', 'trading/signals.py',
    'trading/structure.py')))

V2 = tuple(sorted((
    'm13_public_poll.py', 'm13_shadow_poll.py',
    'trading/m13_driver.py', 'trading/m13_exit.py', 'trading/m13_feed.py',
    'trading/m13_observations.py', 'trading/m13_runtime.py', 'trading/m13_runtime_journal.py',
    'trading/m13_risk_inputs.py', 'trading/m13_shadow.py', 'trading/m13_targets.py')))


def source_hash(root=None):
    base = Path(__file__).resolve().parent.parent if root is None else Path(root)
    digest = sha256(b'M13-v2-local-source-v1\0')
    for name in PRESERVED + V2:
        path = base / name
        if not path.is_file() or path.is_symlink() or path.resolve() != path.absolute():
            raise ValueError(f'Unsafe or missing local source: {name}')
        data = path.read_bytes()
        digest.update(name.encode('utf-8') + b'\0')
        digest.update(len(data).to_bytes(8, 'big'))
        digest.update(data)
    return digest.hexdigest()


def environment_identity():
    """Enforce the tested local interpreter and numeric runtime at both gates."""
    if (sys.implementation.name != 'cpython' or sys.version_info[:3] != (3, 13, 7)
            or platform.system() != 'Windows' or platform.machine() != 'AMD64'
            or pd.__version__ != '3.0.5' or np.__version__ != '2.5.2'):
        raise ValueError('M13-v2 tested runtime environment mismatch')
    required = ('pandas', 'numpy', 'python-dateutil', 'six', 'tzdata')
    packages = tuple((name, metadata.version(name)) for name in required)
    detail = (sys.implementation.name, tuple(sys.version_info[:3]),
              sys.implementation.cache_tag, sysconfig.get_config_var('SOABI'), platform.platform(),
              platform.machine(), packages)
    return sha256(repr(detail).encode('utf-8')).hexdigest()


class M13RuntimeJournal(RuntimeJournal):
    OPERATIONS = RuntimeJournal.OPERATIONS | frozenset({'SEED', 'SEED_PAIR', 'STOP_AMEND',
                                                         'PUBLIC_CANDLE', 'SHADOW_PAIR'})
