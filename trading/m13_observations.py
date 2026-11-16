"""Durable public-candle diagnostics. No strategy, risk, or paper orders."""
from trading.candle_driver import CandleInput
from trading.m13_feed import SOURCE_IDS
from trading.m13_runtime_journal import M13RuntimeJournal, environment_identity, source_hash
from trading.paper_journal import PaperJournalError
from trading.paper_runtime import _candle, _candle_data
from trading.pattern_engine import TIMEFRAMES
from trading.portfolio import identifier
from trading.replay_codec import decode, encode


class PublicObservationSession:
    """Fenced, replay-verified observation log for an unconfigured fresh session."""

    def __init__(self, path, *, create, priority=('BTC/USDT', 'ETH/USDT')):
        if priority not in (('BTC/USDT', 'ETH/USDT'), ('ETH/USDT', 'BTC/USDT')):
            raise ValueError('Explicit BTC/ETH priority required')
        self.priority = priority
        self._last = {}
        self._last_order = None
        self._head = identifier('M13-public-observation-start-v2', priority)
        self._dead = False
        baseline = identifier('M13-public-observation-baseline-v2', priority,
                              environment_identity())
        self.journal = M13RuntimeJournal(path, baseline_hash=baseline,
                                        code_hash=source_hash(), create=create)
        if not create:
            try:
                commands, outcomes = self.journal.verify()
                if len(commands) != len(outcomes):
                    raise PaperJournalError('Uncertain public observation needs fresh resolution')
                for command, outcome in zip(commands, outcomes):
                    _, event_id, op, payload, _, _ = command
                    if op != 'PUBLIC_CANDLE' or outcome[1] != 'APPLIED':
                        raise PaperJournalError('Public observation journal has incompatible state')
                    item = _candle(decode(payload))
                    result, post = self._apply(item)
                    if (event_id != self._id(item) or encode(result) != outcome[2]
                            or post != outcome[3]):
                        raise PaperJournalError('Public observation replay diverged')
            except BaseException:
                self._dead = True
                self.journal.close()
                raise

    @classmethod
    def create(cls, path, *, priority=('BTC/USDT', 'ETH/USDT')):
        return cls(path, create=True, priority=priority)

    @classmethod
    def recover(cls, path, *, priority=('BTC/USDT', 'ETH/USDT')):
        return cls(path, create=False, priority=priority)

    @staticmethod
    def _id(item):
        return identifier('M13-public-candle-v2', item.symbol, item.opened_at)

    @property
    def latest(self):
        return dict(self._last)

    def close(self):
        self.journal.close()

    def _apply(self, item):
        if not isinstance(item, CandleInput):
            raise ValueError('Typed completed 5m candle required')
        if item.source_id != SOURCE_IDS.get(item.symbol.replace('/', '')):
            raise ValueError('Verified public Binance source identity required')
        key = (item.opened_at, self.priority.index(item.symbol))
        if self._last_order is not None and key <= self._last_order:
            raise ValueError('Public candle order or asset priority changed')
        prior = self._last.get(item.symbol)
        if prior is not None:
            if item.opened_at != prior.opened_at + TIMEFRAMES['5m']:
                raise ValueError('Missing public 5m candle')
            if item.source_id != prior.source_id:
                raise ValueError('Public source identity changed')
        if item.received_at - item.candle.closed_at >= TIMEFRAMES['5m']:
            raise ValueError('Late public candle cannot enter a causal paper session')
        self._last[item.symbol] = item
        self._last_order = key
        result = dict(symbol=item.symbol, opened_at=item.opened_at,
                      received_at=item.received_at, status='OBSERVED_ONLY')
        self._head = identifier('M13-public-observation-state-v2', self._head, item)
        return result, self._head

    def observe(self, item):
        if self._dead:
            raise PaperJournalError('Public observation session quarantined')
        if not isinstance(item, CandleInput):
            raise ValueError('Typed public candle required')
        if item.source_id != SOURCE_IDS.get(item.symbol.replace('/', '')):
            raise ValueError('Verified public Binance source identity required')
        event_id = self._id(item)
        payload = encode(_candle_data(item))
        try:
            self.journal.assert_owner()
            prior = self.journal.command(event_id)
            if prior is not None:
                seq, operation, recorded = prior
                if operation != 'PUBLIC_CANDLE' or recorded != payload:
                    raise PaperJournalError('Revised duplicate public candle')
                outcome = self.journal.outcome(seq)
                if outcome is None or outcome[0] != 'APPLIED':
                    raise PaperJournalError('Uncertain public observation')
                return decode(outcome[1])
            seq = self.journal.append(event_id, 'PUBLIC_CANDLE', payload)
        except BaseException:
            self._dead = True
            raise
        try:
            result, post = self._apply(item)
            self.journal.finish(seq, 'APPLIED', encode(result), post)
            return result
        except BaseException:
            self._dead = True
            raise
