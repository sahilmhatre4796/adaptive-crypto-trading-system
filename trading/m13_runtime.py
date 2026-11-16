"""Fresh M13-v2 offline paper session; no exchange order transport."""
from decimal import Decimal
from trading.candle_driver import CandleInput, WARMUP_5M_BARS
from trading.exit_funding import ExitOutcome, FundingObservation
from trading.freqtrade_bridge import FreqtradeSimulationBridge, SimulationFill
from trading.m13_driver import CurrentCandleDriver
from trading.m13_exit import M13ExitController, TP1Milestone
from trading.m13_feed import SOURCE_IDS
from trading.m13_runtime_journal import M13RuntimeJournal, environment_identity, source_hash
from trading.m13_targets import TargetPolicy
from trading.paper_execution import OfflinePaperExecution
from trading.paper_journal import PaperJournalError
from trading.paper_runtime import OfflinePaperRuntime, _candle, _candle_data, _funding, _funding_data
from trading.paper_state import reconcile_paper_state
from trading.portfolio import Quote, identifier, text
from trading.replay_codec import decode, encode
from trading.risk import CostModel, proposal_from_m7 as approved_proposal_adapter
from trading.portfolio import InstrumentSpec


class M13PaperRuntime(OfflinePaperRuntime):
    """Isolated v2 journal and actors; monitor mode cannot evaluate risk or fill."""

    def __init__(self, path, factory, *, create, priority, proposal_from_m7=None,
                 targets=None):
        if (not callable(factory) or priority not in (('BTC/USDT', 'ETH/USDT'),
                ('ETH/USDT', 'BTC/USDT')) or (proposal_from_m7 is None) != (targets is None)
                or proposal_from_m7 is not None and (proposal_from_m7 is not approved_proposal_adapter
                or not isinstance(targets, TargetPolicy))):
            raise ValueError('Fresh factory, fixed priority and complete trade policy required')
        drivers, bridge = factory()
        if (type(drivers) is not dict or set(drivers) != set(priority)
                or any(not isinstance(drivers[s], CurrentCandleDriver)
                       or drivers[s].symbol != s or drivers[s].inputs for s in priority)
                or not isinstance(bridge, FreqtradeSimulationBridge)
                or bridge.risk.mode != 'CURRENT'
                or bridge.risk.config.risk_fraction != Decimal('.10')
                or bridge.risk.history or bridge.receipts or bridge.orders):
            raise ValueError('Fresh matching CURRENT actors required')
        self.drivers, self.bridge, self.priority = drivers, bridge, priority
        self.adapter = OfflinePaperExecution(bridge)
        self.controller = M13ExitController(bridge)
        self.proposal_from_m7 = proposal_from_m7
        self.targets = targets
        self._last_at = None
        self._last_priority = -1
        self._open_frame = None
        self._fanout_active = False
        self._dead = False
        self._head_state = identifier('M13-runtime-start-v2', priority)
        baseline = identifier('M13-runtime-baseline-v2', priority, environment_identity(),
                              bridge.configuration_id, bridge.risk.configuration_id,
                              bridge.risk.portfolio.initial_cash,
                              targets, proposal_from_m7 is not None,
                              tuple((s, drivers[s].pattern_engine.config,
                                     drivers[s].structure_engine.config,
                                     drivers[s].regime_engine.config,
                                     drivers[s].context_config,
                                     drivers[s].signal_config,
                                     drivers[s].source) for s in priority))
        self.journal = M13RuntimeJournal(path, baseline_hash=baseline,
                                        code_hash=source_hash(), create=create)
        self._expected_visible = self._visible_state()
        if not create:
            try:
                self._recover()
            except BaseException:
                self._dead = True
                self.journal.close()
                raise

    @classmethod
    def create(cls, path, factory, *, priority=('BTC/USDT', 'ETH/USDT'),
               proposal_from_m7=None, targets=None):
        return cls(path, factory, create=True, priority=priority,
                   proposal_from_m7=proposal_from_m7, targets=targets)

    @classmethod
    def recover(cls, path, factory, *, priority=('BTC/USDT', 'ETH/USDT'),
                proposal_from_m7=None, targets=None):
        return cls(path, factory, create=False, priority=priority,
                   proposal_from_m7=proposal_from_m7, targets=targets)

    def _visible_state(self):
        return identifier('M13-visible-v2', super()._visible_state(),
                          self.controller.pending_amendments, self.controller.amendments,
                          tuple(sorted(self.controller._tp1.items())))

    def _advance_hash(self, operation, payload, result):
        self._expected_visible = self._visible_state()
        self._head_state = identifier('M13-runtime-state-v2', self._head_state,
                                      operation, payload, result, self._expected_visible)
        return self._head_state

    def _effect(self, operation, effect, at):
        snap = self.bridge.risk.portfolio.snapshot(as_of=at)
        state_id = identifier('M13-runtime-observed-v2', snap,
            len(self.bridge.risk.history), len(self.bridge.receipts),
            len(self.bridge.history), len(self.adapter.history),
            len(self.bridge.risk.portfolio.history), len(self.controller.plans),
            self.controller.pending_amendments, self.controller.amendments)
        return dict(kind=operation, effect=effect, state_id=state_id)

    def _apply(self, operation, payload):
        if operation == 'SEED_PAIR':
            at = self._time(payload['retrieved_at'], self.priority[-1])
            if self.bridge.risk.history or self.bridge.orders:
                raise PaperJournalError('Pair seed requires fresh paper actors')
            counts = []
            for symbol in self.priority:
                items = tuple(_candle(raw) for raw in payload[symbol])
                counts.append(self.drivers[symbol].seed_many(items, retrieved_at=at))
            return self._effect(operation, (tuple(counts),
                                _candle(payload[self.priority[0]][-1]).candle.closed_at), at)
        if operation == 'SEED':
            symbol = payload['symbol']
            if symbol not in self.drivers or self.bridge.risk.history or self.bridge.orders:
                raise PaperJournalError('Historical seed requires fresh paper actors')
            at = self._time(payload['retrieved_at'], symbol)
            items = tuple(_candle(raw) for raw in payload['items'])
            count = self.drivers[symbol].seed_many(items, retrieved_at=at)
            return self._effect(operation, (symbol, count,
                                items[0].opened_at, items[-1].candle.closed_at), at)
        if operation == 'EXIT_FRAME':
            at = self._time(payload['as_of'])
            item = _candle(payload['candle'])
            results = self.controller.observe_candle(item)
            effect = tuple((x.event_id if isinstance(x, TP1Milestone) else
                            x.lifecycle.event_id) for x in results)
            return self._effect(operation, effect, at)
        if operation == 'STOP_AMEND':
            at = self._time(payload['as_of'])
            amendment = self.controller.amend_stop(payload['order_id'], payload['cause_id'],
                as_of=at, event_id=payload['event_id'])
            return self._effect(operation, amendment.event_id, at)
        if operation == 'ENTRY_FILL':
            at = self._time(payload['as_of'])
            fill = payload['fill']
            if not isinstance(fill, SimulationFill) or fill.phase != 'ENTRY' or self.targets is None:
                raise ValueError('Explicit approved synthetic entry and v2 targets required')
            order = next((x for x in self.bridge.orders if x.order_id == fill.order_id), None)
            if order is None:
                raise PaperJournalError('M9.1 approved order is absent')
            tp1, tp2 = self.targets.prices(order)
            record = self.adapter.fill(fill, as_of=at)
            self.controller.register_v2(order, tp1=tp1, tp2=tp2,
                target_slippage_rate=self.targets.exit_slippage_rate, as_of=at)
            return self._effect(operation, record.record_id, at)
        return super()._apply(operation, payload)

    def _complete_frame(self, payload):
        item = _candle(payload['candle'])
        frame_id, at = payload['event_id'], item.received_at
        outcome = self.drivers[item.symbol]._outcomes[-1]
        if outcome.candle != item:
            raise PaperJournalError('Frame output differs from journaled candle')
        for event in outcome.terminal_events:
            self._submit(identifier('M13-terminal-v2', frame_id, event.event_id),
                'TERMINAL', dict(terminal_id=event.event_id, as_of=at))
        for kind, quote in (('MARK', payload['mark']), ('FX', payload['fx'])):
            if quote is not None:
                self._submit(identifier('M13-quote-v2', kind, item, quote),
                    'QUOTE', dict(quote=quote, quote_kind=kind, as_of=at))
        for order in sorted(self.bridge.orders, key=lambda x: x.order_id):
            if self.bridge.state(order.order_id) == 'PROPOSED' and at >= order.expires_at:
                self._submit(identifier('M13-expiry-v2', order.order_id),
                    'EXPIRE', dict(order_id=order.order_id, as_of=at))
        for raw in sorted(payload['funding'], key=lambda x:
                          (x['received_at'], x['settled_at'], x['event_id'])):
            observation = _funding(raw)
            self._submit(observation.event_id, 'FUNDING',
                         dict(observation=raw, as_of=at))
            for order_id, cause_id in self.controller.pending_amendments:
                self._submit(identifier('M13-amend-v2', order_id, cause_id), 'STOP_AMEND',
                             dict(order_id=order_id, cause_id=cause_id, as_of=at))
        exits = self._submit(identifier('M13-exit-frame-v2', frame_id),
            'EXIT_FRAME', dict(candle=payload['candle'], as_of=at))['effect']
        for order_id, cause_id in self.controller.pending_amendments:
            self._submit(identifier('M13-amend-v2', order_id, cause_id), 'STOP_AMEND',
                         dict(order_id=order_id, cause_id=cause_id, as_of=at))
        decisions, admissions, entries = [], [], []
        if self.proposal_from_m7 is not None:
            for record in outcome.decisions:
                if not record.qualification_passed:
                    continue
                proposal = self.proposal_from_m7(record, as_of=at,
                                                 events=outcome.terminal_events)
                decision_id = self._submit(identifier('M13-risk-v2', frame_id, record.decision_id),
                    'RISK', dict(proposal=proposal, spec=payload['spec'], costs=payload['costs'],
                                 leverage=payload['leverage'], as_of=at))['effect']
                receipt_id = self._submit(identifier('M13-admit-v2', decision_id),
                    'ADMIT', dict(decision_id=decision_id, as_of=at))['effect']
                receipt = next(x for x in self.bridge.receipts if x.receipt_id == receipt_id)
                if receipt.disposition == 'ACCEPTED':
                    self._submit(identifier('M13-ack-v2', receipt_id), 'ACK',
                        dict(receipt_id=receipt_id, as_of=at))
                    order = receipt.order
                    fill = SimulationFill(identifier('M13-synthetic-entry-v2', order.order_id),
                        order.order_id, 'ENTRY', order.quantity_contracts,
                        order.entry_price, order.risk.costs.entry_fee_rate,
                        item.candle.closed_at, at, 'M8-approved offline synthetic fill')
                    self._submit(fill.fill_id, 'ENTRY_FILL', dict(fill=fill, as_of=at))
                    entries.append(fill.fill_id)
                decisions.append(decision_id)
                admissions.append(receipt_id)
        summary = dict(kind='FRAME', frame_id=identifier(item),
            patterns=outcome.pattern_ids,
            signals=tuple(x.decision_id for x in outcome.decisions),
            terminals=tuple(x.event_id for x in outcome.terminal_events),
            decisions=tuple(decisions), admissions=tuple(admissions),
            entries=tuple(entries), exits=tuple(exits),
            funding=tuple(x['event_id'] for x in payload['funding']))
        return self._submit(identifier('M13-frame-done-v2', frame_id), 'FRAME_DONE',
                            dict(frame_id=frame_id, summary=summary, as_of=at))

    def frame(self, item: CandleInput, *, mark=None, fx=None, spec=None, costs=None,
              leverage=None, funding=()):
        if not isinstance(item, CandleInput) or type(funding) is not tuple or any(
                not isinstance(x, FundingObservation) for x in funding):
            raise ValueError('Typed 5m candle and funding tuple required')
        if item.source_id != SOURCE_IDS.get(item.symbol.replace('/', '')):
            raise ValueError('Verified public Binance candle source required')
        if any(x is not None and not isinstance(x, t) for x, t in
               ((mark, Quote), (fx, Quote), (spec, InstrumentSpec), (costs, CostModel))):
            raise ValueError('Typed M8 market and risk inputs required')
        if (mark is not None and (mark.key != item.symbol or mark.available_at > item.received_at)
                or fx is not None and (fx.key != 'USDT' or fx.available_at > item.received_at)
                or spec is not None and (spec.symbol != item.symbol
                                         or spec.available_at > item.received_at)
                or costs is not None and costs.available_at > item.received_at):
            raise ValueError('Market, instrument and cost inputs must match the frame cutoff')
        if self.proposal_from_m7 is not None and any(x is None for x in
                (mark, fx, spec, costs, leverage)):
            raise ValueError('Trading requires explicit mark, FX, instrument, costs and leverage')
        if any(x.symbol != item.symbol or x.received_at != item.received_at
               or x.settled_at > item.candle.closed_at
               or mark is not None and x.mark != mark
               or fx is not None and x.fx != fx for x in funding):
            raise ValueError('Funding must match asset, cutoff and source evidence')
        if (len({x.event_id for x in funding}) != len(funding)
                or len({(x.symbol, x.settled_at) for x in funding}) != len(funding)):
            raise ValueError('Duplicate funding evidence')
        event_id = identifier('M13-frame-v2', item.symbol, item.opened_at)
        if (item.received_at > item.candle.closed_at
                and self.journal.command(event_id) is None
                and any(len(driver.inputs) < WARMUP_5M_BARS for driver in self.drivers.values())):
            raise ValueError('Both assets need complete journaled warmup before live frames')
        payload = dict(candle=_candle_data(item), mark=mark, fx=fx,
            spec=spec, costs=costs, leverage=leverage,
            funding=tuple(_funding_data(x) for x in funding))
        self._submit(event_id, 'FRAME', payload)
        done = self.journal.command(identifier('M13-frame-done-v2', event_id))
        if done is not None:
            saved = self.journal.outcome(done[0])
            if saved is not None and saved[0] == 'APPLIED':
                return decode(saved[1])
        return self._fanout({'event_id': event_id, **payload})

    def seed(self, symbol, items, *, retrieved_at):
        if symbol not in self.drivers or type(items) is not tuple or not items:
            raise ValueError('Matching nonempty historical seed tuple required')
        if any(not isinstance(x, CandleInput) or x.symbol != symbol
               or x.source_id != SOURCE_IDS.get(symbol.replace('/', '')) for x in items):
            raise ValueError('Seed requires matching public BTC/ETH candles')
        if self.journal.command(identifier('M13-seed-v2', symbol)) is None:
            self.drivers[symbol].validate_seed(items, retrieved_at=retrieved_at)
        return self._submit(identifier('M13-seed-v2', symbol), 'SEED',
                            dict(symbol=symbol, items=tuple(_candle_data(x) for x in items),
                                 retrieved_at=retrieved_at))

    def seed_pair(self, batches, *, retrieved_at):
        """One durable PENDING/APPLIED unit for both historical actor seeds."""
        if type(batches) is not dict or set(batches) != set(self.priority):
            raise ValueError('Matching BTC/ETH seed pair required')
        for symbol in self.priority:
            items = batches[symbol]
            if (type(items) is not tuple or any(not isinstance(x, CandleInput)
                    or x.source_id != SOURCE_IDS.get(symbol.replace('/', '')) for x in items)):
                raise ValueError('Seed pair requires public BTC/ETH candles')
        first, second = (batches[s] for s in self.priority)
        if (not first or not second or len(first) != len(second)
                or first[0].opened_at != second[0].opened_at
                or first[-1].candle.closed_at != second[-1].candle.closed_at):
            raise ValueError('BTC/ETH seed windows must share exact boundaries')
        event_id = identifier('M13-seed-pair-v2', self.priority,
                              first[0].opened_at, first[-1].candle.closed_at, len(first))
        if self.journal.command(event_id) is None:
            if self.journal.head or any(driver.inputs for driver in self.drivers.values()):
                raise ValueError('Fresh pair seed must be first journal operation')
            for symbol in self.priority:
                self.drivers[symbol].validate_seed(batches[symbol], retrieved_at=retrieved_at)
        return self._submit(event_id, 'SEED_PAIR',
            {symbol: tuple(_candle_data(x) for x in batches[symbol]) for symbol in self.priority}
            | {'retrieved_at': retrieved_at})

    def reconcile(self, *, as_of):
        result = super().reconcile(as_of=as_of)
        for order_id, cause in self.controller.pending_amendments:
            raise PaperJournalError(f'Unresolved v2 protective amendment: {order_id} {cause}')
        return result
