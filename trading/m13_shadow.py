"""Durable CURRENT M7 observations; deliberately has no risk or order actor."""

from trading.candle_driver import CandleInput, WARMUP_5M_BARS
from trading.m13_driver import CurrentCandleDriver
from trading.m13_feed import SOURCE_IDS
from trading.m13_runtime_journal import M13RuntimeJournal, environment_identity, source_hash
from trading.m6_data import Source, jsonable
from trading.paper_journal import PaperJournalError
from trading.paper_runtime import _candle, _candle_data
from trading.pattern_engine import PatternEngine, TIMEFRAMES
from trading.portfolio import identifier
from trading.regime import RegimeConfig
from trading.replay_codec import decode, encode
from trading.signal_context import SignalContextConfig
from trading.signals import (CONTRADICTIONS, GROUPS, ContradictionRule, GroupCap,
                             SignalConfig)
from trading.structure import StructureEngine


PRIORITY = ('BTC/USDT', 'ETH/USDT')


def shadow_drivers():
    """Explicit uncalibrated policy: zero score caps are shadow-only placeholders."""
    context = SignalContextConfig(15 * 60, 'NEUTRAL', 'UNCERTAIN')
    policy = SignalConfig('M13-UNAPPROVED-SHADOW-v1',
        tuple(GroupCap(group, 0.) for group in GROUPS),
        tuple(ContradictionRule(reason, 'BLOCK') for reason in CONTRADICTIONS),
        (), True, None)
    return {symbol: CurrentCandleDriver(symbol,
        pattern_engine=PatternEngine(), structure_engine=StructureEngine(),
        regime_config=RegimeConfig(), context_config=context,
        signal_config=policy,
        source=Source('binance', 'USD-M', symbol.replace('/', ''),
                      'public_completed_5m')) for symbol in PRIORITY}


class ShadowSession:
    """One fenced journal for both assets, with no path to M8 admission."""

    def __init__(self, path, *, create, factory=shadow_drivers):
        if not callable(factory):
            raise ValueError('Fresh shadow-driver factory required')
        drivers = factory()
        if (type(drivers) is not dict or set(drivers) != set(PRIORITY)
                or any(not isinstance(drivers[s], CurrentCandleDriver)
                       or drivers[s].symbol != s or drivers[s].inputs
                       or drivers[s].signals.mode != 'CURRENT' for s in PRIORITY)):
            raise ValueError('Fresh matching CURRENT shadow drivers required')
        self._drivers = drivers
        self._dead = False
        self._seeded = False
        baseline = identifier('M13-shadow-baseline-v1', PRIORITY,
            environment_identity(), tuple((s, drivers[s].pattern_engine.config,
                drivers[s].pattern_engine.context_config,
                drivers[s].structure_engine.config,
                drivers[s].regime_engine.config,
                drivers[s].context_config, drivers[s].signal_config,
                drivers[s].source) for s in PRIORITY))
        self._state = identifier('M13-shadow-start-v1', baseline)
        self.journal = M13RuntimeJournal(path, baseline_hash=baseline,
                                        code_hash=source_hash(), create=create)
        if not create:
            try:
                self._recover()
            except BaseException:
                self._dead = True
                self.journal.close()
                raise

    @classmethod
    def create(cls, path, *, factory=shadow_drivers):
        return cls(path, create=True, factory=factory)

    @classmethod
    def recover(cls, path, *, factory=shadow_drivers):
        return cls(path, create=False, factory=factory)

    @property
    def latest(self):
        return {symbol: driver.inputs[-1] for symbol, driver in self._drivers.items()
                if driver.inputs}

    def close(self):
        self.journal.close()

    def _apply(self, operation, payload):
        if operation == 'SEED_PAIR':
            if self._seeded or any(driver.inputs for driver in self._drivers.values()):
                raise PaperJournalError('Shadow seed requires fresh actors')
            for symbol in PRIORITY:
                self._drivers[symbol].seed_many(
                    tuple(_candle(raw) for raw in payload[symbol]),
                    retrieved_at=payload['retrieved_at'])
            self._seeded = True
            return {'status': 'SHADOW_SEEDED', 'count': len(payload[PRIORITY[0]]),
                    'cutoff': self._drivers[PRIORITY[0]].inputs[-1].candle.closed_at}
        if operation == 'SHADOW_PAIR':
            if not self._seeded:
                raise PaperJournalError('Shadow frames require paired historical warmup')
            outcomes = {}
            for symbol in PRIORITY:
                outcome = self._drivers[symbol].observe(_candle(payload[symbol]))
                outcomes[symbol] = {
                    'opened_at': outcome.candle.opened_at,
                    'received_at': outcome.candle.received_at,
                    'pattern_ids': outcome.pattern_ids,
                    'decisions': tuple(record.to_dict() for record in outcome.decisions),
                    'terminal_events': tuple(jsonable(event) for event in outcome.terminal_events),
                    'warmup_ready': outcome.warmup_ready}
            return {'status': 'UNAPPROVED_SHADOW', 'assets': outcomes}
        raise PaperJournalError('Unsupported shadow journal operation')

    def _advance(self, operation, payload, result):
        self._state = identifier('M13-shadow-state-v1', self._state,
                                 operation, payload, result)
        return self._state

    def _recover(self):
        commands, outcomes = self.journal.verify()
        for seq, event_id, operation, encoded, _, _ in commands:
            saved = outcomes[seq - 1] if seq <= len(outcomes) else None
            if saved is not None and saved[1] == 'QUARANTINED':
                raise PaperJournalError('Quarantined shadow observation')
            payload = decode(encoded)
            if payload.get('event_id') != event_id or operation not in ('SEED_PAIR', 'SHADOW_PAIR'):
                raise PaperJournalError('Shadow command identity or operation changed')
            result = self._apply(operation, payload)
            post = self._advance(operation, payload, result)
            if saved is None:
                self.journal.finish(seq, 'APPLIED', encode(result), post)
            elif saved[2] != encode(result) or saved[3] != post:
                raise PaperJournalError('Shadow recovery diverged from journaled outcome')

    def _submit(self, event_id, operation, payload):
        if self._dead:
            raise PaperJournalError('Shadow actor graph quarantined; recover fresh actors')
        command = {'event_id': event_id, **payload}
        encoded = encode(command)
        try:
            self.journal.assert_owner()
            previous = self.journal.command(event_id)
            if previous is not None:
                seq, prior_operation, prior_payload = previous
                if prior_operation != operation or prior_payload != encoded:
                    raise PaperJournalError('Revised shadow observation')
                outcome = self.journal.outcome(seq)
                if outcome is None or outcome[0] != 'APPLIED':
                    raise PaperJournalError('Uncertain shadow command requires fresh recovery')
                return decode(outcome[1])
            seq = self.journal.append(event_id, operation, encoded)
        except BaseException:
            self._dead = True
            raise
        try:
            result = self._apply(operation, command)
            post = self._advance(operation, command, result)
            self.journal.finish(seq, 'APPLIED', encode(result), post)
            return result
        except Exception as exc:
            self._dead = True
            try:
                self.journal.finish(seq, 'QUARANTINED', type(exc).__name__, '')
            except BaseException:
                pass
            raise
        except BaseException:
            self._dead = True
            raise

    def seed_pair(self, batches, *, retrieved_at):
        if type(batches) is not dict or set(batches) != set(PRIORITY):
            raise ValueError('Matching BTC/ETH historical seed required')
        first, second = (batches[s] for s in PRIORITY)
        if (type(first) is not tuple or type(second) is not tuple or not first
                or len(first) != len(second)
                or first[0].opened_at != second[0].opened_at
                or first[-1].candle.closed_at != second[-1].candle.closed_at):
            raise ValueError('Paired shadow seed must share exact boundaries')
        event_id = identifier('M13-shadow-seed-v1', first[0].opened_at,
                              first[-1].candle.closed_at, len(first))
        if self.journal.command(event_id) is None:
            if self.journal.head:
                raise ValueError('Shadow seed must be first command')
            for symbol in PRIORITY:
                if (any(not isinstance(item, CandleInput) or item.symbol != symbol
                        or item.source_id != SOURCE_IDS[symbol.replace('/', '')]
                        for item in batches[symbol])):
                    raise ValueError('Verified public seed source required')
                self._drivers[symbol].validate_seed(batches[symbol], retrieved_at=retrieved_at)
        return self._submit(event_id, 'SEED_PAIR',
            {symbol: tuple(_candle_data(item) for item in batches[symbol])
             for symbol in PRIORITY} | {'retrieved_at': retrieved_at})

    def observe_pair(self, batches):
        if type(batches) is not dict or set(batches) != set(PRIORITY):
            raise ValueError('Matching BTC/ETH live pair required')
        if any(not isinstance(batches[s], CandleInput) or batches[s].symbol != s
               or batches[s].source_id != SOURCE_IDS[s.replace('/', '')]
               for s in PRIORITY):
            raise ValueError('Verified public live candles required')
        first, second = (batches[s] for s in PRIORITY)
        if first.opened_at != second.opened_at:
            raise ValueError('Live BTC/ETH candle cutoffs differ')
        event_id = identifier('M13-shadow-pair-v1', first.opened_at)
        if self.journal.command(event_id) is None:
            if not self._seeded:
                raise ValueError('Complete paired warmup required')
            for symbol in PRIORITY:
                item = batches[symbol]
                previous = self._drivers[symbol].inputs[-1]
                if (item.opened_at != previous.opened_at + TIMEFRAMES['5m']
                        or item.received_at < previous.received_at
                        or item.received_at-item.candle.closed_at >= TIMEFRAMES['5m']):
                    raise ValueError('Missing, late or out-of-order live candle')
        return self._submit(event_id, 'SHADOW_PAIR',
            {symbol: _candle_data(batches[symbol]) for symbol in PRIORITY})
