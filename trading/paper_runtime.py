"""Offline M12 candle-to-risk-to-simulated-order runtime with local causal replay.

One durable source command is the replay unit. Calls nested inside a frame are
reconstructed from that command, never independently replayed or double-booked.
"""
from hashlib import sha256
from pathlib import Path

from trading.candle_driver import CandleDecisionDriver, CandleInput
from trading.exit_funding import ExitFundingController, FundingObservation
from trading.freqtrade_bridge import (FreqtradeSimulationBridge, SimulationCancellation,
                                      SimulationFill)
from trading.paper_execution import OfflinePaperExecution
from trading.paper_journal import PaperJournalError
from trading.paper_runtime_journal import RuntimeJournal
from trading.paper_state import reconcile_paper_state
from trading.pattern_context import utc_timestamp
from trading.patterns import OHLC
from trading.portfolio import Quote, InstrumentSpec, identifier, text
from trading.replay_codec import decode, encode
from trading.risk import CostModel, proposal_from_m7


def _candle_data(item):
    return dict(symbol=item.symbol, opened_at=item.opened_at,
                ohlc=(item.ohlc.open, item.ohlc.high, item.ohlc.low, item.ohlc.close),
                volume=item.volume, received_at=item.received_at, source_id=item.source_id)


def _candle(data):
    if type(data) is not dict or set(data) != {'symbol', 'opened_at', 'ohlc', 'volume', 'received_at', 'source_id'}:
        raise PaperJournalError('Malformed runtime candle')
    return CandleInput(data['symbol'], data['opened_at'], OHLC(*data['ohlc']),
                       data['volume'], data['received_at'], data['source_id'])


def _funding_data(item):
    return dict(event_id=item.event_id, symbol=item.symbol, settled_at=item.settled_at,
                published_at=item.published_at, received_at=item.received_at,
                rate=item.rate, mark=item.mark, fx=item.fx, source=item.source,
                historical_eligible=item.historical_eligible)


def _funding(data):
    if type(data) is not dict or set(data) != {'event_id', 'symbol', 'settled_at', 'published_at',
            'received_at', 'rate', 'mark', 'fx', 'source', 'historical_eligible'}:
        raise PaperJournalError('Malformed runtime funding observation')
    return FundingObservation(**data)


def _code_hash():
    directory = Path(__file__).parent
    names = ('paper_runtime.py', 'paper_runtime_journal.py', 'paper_journal.py',
             'candle_driver.py', 'exit_funding.py', 'paper_execution.py', 'paper_state.py',
             'patterns.py', 'pattern_context.py', 'pattern_engine.py', 'structure.py',
             'm6.py', 'm6_data.py', 'regime.py', 'derivatives.py', 'liquidation.py',
             'signal_context.py', 'signals.py', 'risk.py', 'portfolio.py',
             'freqtrade_bridge.py', 'replay_codec.py')
    value = sha256()
    for name in names:
        value.update(name.encode())
        value.update((directory / name).read_bytes())
    return value.hexdigest()


class OfflinePaperRuntime:
    """Single fenced owner of two fresh analytical drivers and one M8/M9.1 actor graph.

    ``factory`` returns ``(drivers, bridge)`` with fresh, empty BTC/ETH drivers
    and a fresh M9.1 bridge over its M8 portfolio. Explicit source inputs are
    required; no execution price, FX rate, fee or timestamp is synthesized.
    """

    def __init__(self, path, factory, *, create, priority):
        if not callable(factory) or priority not in (('BTC/USDT', 'ETH/USDT'),
                                                    ('ETH/USDT', 'BTC/USDT')):
            raise ValueError('Fresh actor factory and explicit BTC/ETH priority required')
        drivers, bridge = factory()
        if (type(drivers) is not dict or set(drivers) != set(priority)
                or any(not isinstance(drivers[s], CandleDecisionDriver) or drivers[s].symbol != s
                       or drivers[s].inputs for s in priority)
                or not isinstance(bridge, FreqtradeSimulationBridge)
                or bridge.risk.history or bridge.receipts or bridge.orders):
            raise ValueError('Factory must return fresh matching M7 and M8/M9.1 actors')
        self.drivers, self.bridge, self.priority = drivers, bridge, priority
        self.adapter = OfflinePaperExecution(bridge)
        self.controller = ExitFundingController(bridge)
        self._last_at = None
        self._last_priority = -1
        self._open_frame = None
        self._fanout_active = False
        self._dead = False
        self._head_state = identifier('M12-runtime-start-v1', priority)
        baseline = identifier('M12-runtime-baseline-v1', priority,
                              bridge.configuration_id, bridge.risk.configuration_id,
                              bridge.risk.portfolio.initial_cash, bridge.risk.portfolio.history,
                              tuple((s, drivers[s].pattern_engine.config,
                                     drivers[s].structure_engine.config,
                                     drivers[s].regime_engine.config,
                                     drivers[s].context_config, drivers[s].signal_config,
                                     drivers[s].source) for s in priority))
        self.journal = RuntimeJournal(path, baseline_hash=baseline,
                                      code_hash=_code_hash(), create=create)
        self._expected_visible = self._visible_state()
        if not create:
            try:
                self._recover()
            except BaseException:
                self._dead = True
                self.journal.close()
                raise

    @classmethod
    def create(cls, path, factory, *, priority=('BTC/USDT', 'ETH/USDT')):
        return cls(path, factory, create=True, priority=priority)

    @classmethod
    def recover(cls, path, factory, *, priority=('BTC/USDT', 'ETH/USDT')):
        return cls(path, factory, create=False, priority=priority)

    def close(self):
        self.journal.close()

    def _time(self, at, symbol=None):
        at = utc_timestamp(at)
        rank = self.priority.index(symbol) if symbol else self._last_priority
        if self._last_at is not None and (at < self._last_at or at == self._last_at and rank < self._last_priority):
            raise PaperJournalError('Runtime input violates receipt chronology or frozen asset priority')
        self._last_at, self._last_priority = at, rank if symbol else self._last_priority
        return at

    def _apply(self, operation, payload):
        if operation == 'FRAME':
            item = _candle(payload['candle'])
            at = self._time(item.received_at, item.symbol)
            if self._open_frame is not None:
                raise PaperJournalError('Earlier runtime frame has unfinished actor handoffs')
            if any(not isinstance(q, Quote) or q.available_at > at for q in
                   (payload['mark'], payload['fx']) if q is not None):
                raise ValueError('Available typed mark/FX required')
            if (payload['mark'] is not None and payload['mark'].key != item.symbol
                    or payload['fx'] is not None and payload['fx'].key != 'USDT'):
                raise ValueError('Market mark and FX do not match the frame')
            result = self.drivers[item.symbol].observe(item)
            self._open_frame = payload['event_id']
            return dict(kind='FRAME', frame_id=identifier(item),
                        patterns=result.pattern_ids, signals=tuple(r.decision_id for r in result.decisions),
                        terminals=tuple(e.event_id for e in result.terminal_events))
        at = self._time(payload['as_of'])
        if operation == 'TERMINAL':
            event = next((x for driver in self.drivers.values() for x in driver.signals.events
                          if x.event_id == payload['terminal_id']), None)
            if event is None:
                raise PaperJournalError('M7 terminal is absent from authoritative driver history')
            notices = self.controller.on_signal_terminal(event, as_of=at)
            receipt = self.bridge.risk.terminal_history[-1]
            effect = (receipt.receipt_id, tuple(x.event_id for x in notices))
        elif operation == 'QUOTE':
            quote = payload['quote']
            kind = payload['quote_kind']
            if (not isinstance(quote, Quote) or quote.available_at > at
                    or kind not in ('MARK', 'FX')):
                raise ValueError('Available typed quote required')
            effect = self.bridge.risk.portfolio.observe(quote, kind=kind, as_of=at,
                        event_id=payload['event_id']).event_id
        elif operation == 'EXIT_FRAME':
            item = _candle(payload['candle'])
            exits = self.controller.observe_candle(item)
            effect = tuple(x.lifecycle.event_id for x in exits)
        elif operation == 'RISK':
            proposal = payload['proposal']
            if any(r.disposition == 'DENIED' and r.risk.status in ('APPROVED', 'REDUCED')
                   and any(p.allocation_id == r.risk.allocation_id for p in
                           self.bridge.risk.portfolio.snapshot(as_of=at).reservations)
                   for r in self.bridge.receipts):
                raise PaperJournalError('Unresolved M8 reservation after denied admission')
            decision = self.bridge.risk.evaluate(
                proposal, payload['spec'], payload['costs'],
                leverage=payload['leverage'], as_of=at)
            effect = decision.decision_id
        elif operation == 'ADMIT':
            decision = next((x for x in self.bridge.risk.history
                             if x.decision_id == payload['decision_id']), None)
            if decision is None:
                raise PaperJournalError('M8 decision missing before M9.1 admission')
            effect = self.bridge.propose(decision, as_of=at).receipt_id
        elif operation == 'ACK':
            receipt = next((x for x in self.bridge.receipts
                            if x.receipt_id == payload['receipt_id']), None)
            if receipt is None:
                raise PaperJournalError('M9.1 admission missing before paper acknowledgement')
            effect = self.adapter.acknowledge(receipt, event_id=payload['event_id'],
                                              as_of=at).record_id
        elif operation == 'EXPIRE':
            effect = self.bridge.expire(payload['order_id'], as_of=at).event_id
        elif operation == 'FRAME_DONE':
            if payload['frame_id'] != self._open_frame:
                raise PaperJournalError('Runtime frame completion identity changed')
            self._open_frame = None
            effect = payload['summary']
        elif operation in ('ENTRY_FILL', 'EXIT_FILL'):
            fill = payload['fill']
            if not isinstance(fill, SimulationFill) or fill.phase != operation.split('_')[0]:
                raise ValueError('Typed matching simulated fill required')
            record = self.adapter.fill(fill, as_of=at)
            if operation == 'ENTRY_FILL':
                order = next(o for o in self.bridge.orders if o.order_id == fill.order_id)
                self.controller.register(order, take_profit_price=payload['target'],
                                         take_profit_slippage_rate=payload['slippage'], as_of=at)
            effect = record.record_id
        elif operation == 'CANCEL':
            record = self.adapter.cancel(payload['request'], rejected=payload['rejected'], as_of=at)
            effect = record.record_id
        elif operation == 'FUNDING':
            observation = _funding(payload['observation'])
            if observation.received_at != at:
                raise ValueError('Funding command cutoff must equal receipt')
            effect = self.controller.settle_funding(observation).status
        else:
            raise PaperJournalError('Unknown runtime operation')
        # Full Unit 1 reconciliation runs at recovery and explicit checkpoints.
        # Its historical scan is intentionally outside the per-candle hot path.
        snap = self.bridge.risk.portfolio.snapshot(as_of=at)
        state_id = identifier('M12-runtime-observed-v1', snap,
            len(self.bridge.risk._history), len(self.bridge._receipts),
            len(self.bridge._events), len(self.adapter._history),
            len(self.bridge.risk.portfolio._history), len(self.controller._plans))
        if operation == 'FRAME_DONE':
            return {**effect, 'state_id': state_id}
        return dict(kind=operation, effect=effect, state_id=state_id)

    def _visible_state(self):
        risk = self.bridge.risk
        portfolio = risk.portfolio
        at = max(x for x in (self._last_at, portfolio._last_at) if x is not None)
        return identifier('M12-runtime-visible-actor-v1',
            portfolio.snapshot(as_of=at), self._last_at, self._last_priority,
            self._open_frame,
            tuple((s, len(d._outcomes), identifier(d._outcomes[-1]) if d._outcomes else None)
                  for s, d in sorted(self.drivers.items())),
            (len(self.bridge.risk._history), self.bridge.risk._history[-1].decision_id)
                if self.bridge.risk._history else (0, None),
            (len(risk._terminal_history), risk._terminal_history[-1].receipt_id)
                if risk._terminal_history else (0, None),
            (len(portfolio._history), portfolio._history[-1].event_id)
                if portfolio._history else (0, None),
            (len(self.bridge._receipts), self.bridge._receipts[-1].receipt_id)
                if self.bridge._receipts else (0, None),
            (len(self.bridge._events), self.bridge._events[-1].event_id)
                if self.bridge._events else (0, None),
            tuple(sorted(self.bridge._states.items())),
            (len(self.adapter._history), self.adapter._history[-1].record_id)
                if self.adapter._history else (0, None),
            (len(self.controller._plans),
             next(reversed(self.controller._plans.values())).plan_id if self.controller._plans else None),
            tuple(sorted(self.controller._funding)),
            tuple(sorted(self.controller._last_candle.items())))

    def _advance_hash(self, operation, payload, result):
        self._expected_visible = self._visible_state()
        self._head_state = identifier('M12-runtime-state-v1', self._head_state,
            operation, payload, result, self._expected_visible)
        return self._head_state

    def _recover(self):
        commands, outcomes = self.journal.verify()
        for seq, event_id, operation, encoded, _, _ in commands:
            saved = outcomes[seq - 1] if seq <= len(outcomes) else None
            if saved is not None and saved[1] == 'QUARANTINED':
                raise PaperJournalError('Quarantined runtime operation needs explicit resolution')
            payload = decode(encoded)
            result = self._apply(operation, payload)
            if payload['event_id'] != event_id:
                raise PaperJournalError('Runtime event identity changed')
            post = self._advance_hash(operation, payload, result)
            encoded_result = encode(result)
            if saved is None:
                self.journal.finish(seq, 'APPLIED', encoded_result, post)
            elif saved[2] != encoded_result or saved[3] != post:
                raise PaperJournalError('Recovered runtime actor graph diverged')
        if self._open_frame is not None:
            root = self.journal.command(self._open_frame)
            if root is None or root[1] != 'FRAME':
                raise PaperJournalError('Unfinished runtime frame root is absent')
            self._fanout(decode(root[2]))
        if commands:
            reconcile_paper_state(self.bridge, self.controller, as_of=self._last_at)

    def _submit(self, event_id, operation, payload):
        text(event_id)
        if self._dead:
            raise PaperJournalError('Runtime actor graph quarantined; reconstruct fresh actors')
        payload = {'event_id': event_id, **payload}
        encoded = encode(payload)
        try:
            self.journal.assert_owner()
            if self._visible_state() != self._expected_visible:
                raise PaperJournalError('Runtime actors changed outside the durable journal')
            if self._open_frame is not None and not self._fanout_active:
                raise PaperJournalError('Complete the durable frame handoffs before another operation')
            old = self.journal.command(event_id)
            if old is not None:
                seq, prior_op, prior_payload = old
                if prior_op != operation or prior_payload != encoded:
                    raise PaperJournalError('Cannot revise durable runtime input')
                saved = self.journal.outcome(seq)
                if saved is None or saved[0] != 'APPLIED':
                    raise PaperJournalError('Uncertain runtime input requires fresh recovery')
                return decode(saved[1])
            seq = self.journal.append(event_id, operation, encoded)
        except BaseException:
            self._dead = True
            raise
        try:
            result = self._apply(operation, payload)
            post = self._advance_hash(operation, payload, result)
            self.journal.finish(seq, 'APPLIED', encode(result), post)
            return result
        except Exception as exc:
            self._dead = True
            # The graph is discarded even if the outcome transaction itself fails.
            try:
                self.journal.finish(seq, 'QUARANTINED', type(exc).__name__, '')
            except BaseException:
                pass
            raise
        except BaseException:
            self._dead = True
            raise

    def _fanout(self, payload):
        if self._fanout_active:
            raise PaperJournalError('Concurrent or nested frame fan-out is forbidden')
        self._fanout_active = True
        try:
            return self._complete_frame(payload)
        except BaseException:
            # A callback can fail between two durable subcommands. Never reuse
            # that possibly mutated actor graph; only fresh replay may resume.
            self._dead = True
            raise
        finally:
            self._fanout_active = False

    def _complete_frame(self, payload):
        item = _candle(payload['candle'])
        frame_id, at = payload['event_id'], item.received_at
        outcome = self.drivers[item.symbol]._outcomes[-1]
        if outcome.candle != item:
            raise PaperJournalError('Frame output no longer matches journaled source candle')
        terminals = []
        for event in outcome.terminal_events:
            terminals.append(self._submit(identifier('M12-runtime-terminal-v1', frame_id, event.event_id),
                'TERMINAL', dict(terminal_id=event.event_id, as_of=at))['effect'])
        for kind, quote in (('MARK', payload['mark']), ('FX', payload['fx'])):
            if quote is not None:
                self._submit(identifier('M12-runtime-quote-v1', kind, item, quote),
                             'QUOTE', dict(quote=quote, quote_kind=kind, as_of=at))
        expiries = []
        for order in sorted(self.bridge.orders, key=lambda x: x.order_id):
            if self.bridge.state(order.order_id) == 'PROPOSED' and at >= order.expires_at:
                expiries.append(self._submit(identifier('M12-runtime-expiry-v1', order.order_id),
                    'EXPIRE', dict(order_id=order.order_id, as_of=at))['effect'])
        funding_ids = []
        for raw in sorted(payload['funding'], key=lambda x: (x['received_at'], x['settled_at'], x['event_id'])):
            observation = _funding(raw)
            if observation.symbol != item.symbol or observation.received_at != at:
                raise ValueError('Frame funding must match the asset and exact receipt cutoff')
            self._submit(observation.event_id, 'FUNDING', dict(observation=raw, as_of=at))
            funding_ids.append(observation.event_id)
        exits = self._submit(identifier('M12-runtime-exit-observation-v1', frame_id),
                             'EXIT_FRAME', dict(candle=payload['candle'], as_of=at))['effect']
        decisions, admissions = [], []
        for record in outcome.decisions:
            if not record.qualification_passed:
                continue
            proposal = proposal_from_m7(record, as_of=at, events=outcome.terminal_events)
            decision_id = self._submit(identifier('M12-runtime-risk-v1', frame_id, record.decision_id),
                'RISK', dict(proposal=proposal, spec=payload['spec'], costs=payload['costs'],
                             leverage=payload['leverage'], as_of=at))['effect']
            receipt_id = self._submit(identifier('M12-runtime-admit-v1', decision_id),
                'ADMIT', dict(decision_id=decision_id, as_of=at))['effect']
            self._submit(identifier('M12-runtime-ack-v1', receipt_id),
                         'ACK', dict(receipt_id=receipt_id, as_of=at))
            decisions.append(decision_id)
            admissions.append(receipt_id)
        summary = dict(kind='FRAME', frame_id=identifier(item),
                       patterns=outcome.pattern_ids,
                       signals=tuple(r.decision_id for r in outcome.decisions),
                       terminals=tuple(e.event_id for e in outcome.terminal_events),
                       terminal_receipts=tuple(terminals), expiries=tuple(expiries),
                       decisions=tuple(decisions),
                       admissions=tuple(admissions), funding=tuple(funding_ids), exits=tuple(exits))
        return self._submit(identifier('M12-runtime-frame-done-v1', frame_id),
                            'FRAME_DONE', dict(frame_id=frame_id, summary=summary, as_of=at))

    def frame(self, item: CandleInput, *, mark=None, fx=None, spec=None, costs=None,
              leverage=None, funding=()):
        if not isinstance(item, CandleInput) or type(funding) is not tuple or any(
                not isinstance(x, FundingObservation) for x in funding):
            raise ValueError('Typed candle and funding tuple required')
        if any(x is not None and not isinstance(x, t) for x, t in
               ((mark, Quote), (fx, Quote), (spec, InstrumentSpec), (costs, CostModel))):
            raise ValueError('Typed explicit market and risk inputs required')
        # A malformed funding tuple must not leave a committed partial frame.
        if any(x.symbol != item.symbol or x.received_at != item.received_at
               or x.settled_at > item.candle.closed_at
               or self.bridge.risk.mode == 'BACKTEST' and not x.historical_eligible
               or mark is not None and x.mark != mark
               or fx is not None and x.fx != fx for x in funding):
            raise ValueError('Funding must match the frame asset, cutoff and source evidence')
        if (len({x.event_id for x in funding}) != len(funding)
                or len({(x.symbol, x.settled_at) for x in funding}) != len(funding)):
            raise ValueError('Duplicate funding event or settlement slot in frame')
        event_id = identifier('M12-runtime-frame-v1', item.symbol, item.opened_at)
        payload = dict(candle=_candle_data(item), mark=mark, fx=fx,
            spec=spec, costs=costs, leverage=leverage, funding=tuple(_funding_data(x) for x in funding))
        self._submit(event_id, 'FRAME', payload)
        done = self.journal.command(identifier('M12-runtime-frame-done-v1', event_id))
        if done is not None:
            saved = self.journal.outcome(done[0])
            if saved is not None and saved[0] == 'APPLIED':
                return decode(saved[1])
        return self._fanout({'event_id': event_id, **payload})

    def entry_fill(self, fill: SimulationFill, *, take_profit_price, take_profit_slippage_rate, as_of):
        if not isinstance(fill, SimulationFill) or fill.phase != 'ENTRY':
            raise ValueError('Explicit full entry fill required')
        return self._submit(fill.fill_id, 'ENTRY_FILL', dict(fill=fill, target=take_profit_price,
                            slippage=take_profit_slippage_rate, as_of=utc_timestamp(as_of)))

    def exit_fill(self, fill: SimulationFill, *, as_of):
        if not isinstance(fill, SimulationFill) or fill.phase != 'EXIT':
            raise ValueError('Explicit full exit fill required')
        return self._submit(fill.fill_id, 'EXIT_FILL', dict(fill=fill, as_of=utc_timestamp(as_of)))

    def cancel(self, request: SimulationCancellation, *, rejected=False, as_of):
        if not isinstance(request, SimulationCancellation) or type(rejected) is not bool:
            raise ValueError('Typed cancellation required')
        return self._submit(request.cancellation_id, 'CANCEL', dict(request=request,
                            rejected=rejected, as_of=utc_timestamp(as_of)))

    def expire(self, order_id, *, as_of):
        text(order_id)
        return self._submit(identifier('M12-runtime-expiry-v1', order_id), 'EXPIRE',
                            dict(order_id=order_id, as_of=utc_timestamp(as_of)))

    def funding(self, observation: FundingObservation):
        if not isinstance(observation, FundingObservation):
            raise ValueError('Typed funding observation required')
        return self._submit(observation.event_id, 'FUNDING', dict(
            observation=_funding_data(observation), as_of=observation.received_at))

    def reconcile(self, *, as_of):
        if self._dead:
            raise PaperJournalError('Cannot reconcile a quarantined runtime')
        self.journal.verify()
        self.journal.assert_owner()
        if self._visible_state() != self._expected_visible:
            raise PaperJournalError('Runtime actors changed outside the durable journal')
        return reconcile_paper_state(self.bridge, self.controller, as_of=as_of)
