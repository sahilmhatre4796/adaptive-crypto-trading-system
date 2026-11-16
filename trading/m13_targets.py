"""Explicit v2 TP multiples; no calibrated or default strategy parameters."""
from dataclasses import dataclass
from decimal import Decimal, ROUND_CEILING, ROUND_FLOOR

from trading.portfolio import number, step


@dataclass(frozen=True)
class TargetPolicy:
    tp1_r: Decimal
    tp2_r: Decimal
    exit_slippage_rate: Decimal
    identity: str

    def __post_init__(self):
        first, second = number(self.tp1_r, positive=True), number(self.tp2_r, positive=True)
        slip = number(self.exit_slippage_rate, nonnegative=True)
        if second <= first or slip >= 1 or not isinstance(self.identity, str) or not self.identity.strip():
            raise ValueError('Explicit ordered targets, bounded cost and policy identity required')
        object.__setattr__(self, 'tp1_r', first)
        object.__setattr__(self, 'tp2_r', second)
        object.__setattr__(self, 'exit_slippage_rate', slip)

    def prices(self, order):
        """Derive favorable tick-aligned targets from M8-approved entry and stop."""
        direction = 1 if order.side == 'LONG' else -1
        distance = abs(order.entry_price - order.source_stop_price)
        if distance <= 0 or direction * (order.entry_price - order.source_stop_price) <= 0:
            raise ValueError('M8-approved favorable entry-to-stop distance required')
        tick = order.allocation.spec.price_tick
        rounding = ROUND_CEILING if direction == 1 else ROUND_FLOOR
        first = step(order.entry_price + direction * distance * self.tp1_r, tick, rounding)
        second = step(order.entry_price + direction * distance * self.tp2_r, tick, rounding)
        if first <= 0 or second <= 0 or direction * (second - first) <= 0:
            raise ValueError('Targets collapse or become nonpositive at instrument tick')
        return first, second
