"""Exit rules as a pure function. Semantics: docs/ARCHITECTURE.md §2.3."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from decimal import Decimal

from sniper.config import ExitConfig

HOLD = "HOLD"
SELL = "SELL"


@dataclass(frozen=True)
class PositionView:
    entry_price: Decimal       # lamports per raw token at entry
    peak_price: Decimal | None
    tp_hit: bool
    opened_at: datetime


@dataclass(frozen=True)
class ExitDecision:
    action: str                # HOLD or SELL
    fraction: Decimal          # share of the *remaining* tokens to sell
    reason: str | None
    peak_price: Decimal | None  # peak to store after this tick
    ratio: Decimal             # price / entry price


def evaluate_exit(pos: PositionView, price: Decimal, now: datetime, cfg: ExitConfig) -> ExitDecision:
    ratio = price / pos.entry_price
    one = Decimal(1)
    trailing_active = pos.tp_hit or not cfg.trailing_activate_after_tp
    peak = pos.peak_price
    if trailing_active:
        peak = price if peak is None else max(peak, price)

    def sell(fraction: Decimal, reason: str, new_peak: Decimal | None = peak) -> ExitDecision:
        return ExitDecision(SELL, fraction, reason, new_peak, ratio)

    # 1. Stop-loss: always armed, also after the take-profit.
    if ratio <= one - cfg.stop_loss_pct / 100:
        return sell(one, "STOP_LOSS")

    # 2. Take-profit: fires once. The trailing stop starts from this price.
    if not pos.tp_hit and ratio >= one + cfg.take_profit_pct / 100:
        return sell(cfg.take_profit_sell_fraction, "TAKE_PROFIT", price)

    # 3. Trailing stop from the highest price seen while active.
    if trailing_active and peak is not None and price <= peak * (one - cfg.trailing_stop_pct / 100):
        return sell(one, "TRAILING_STOP")

    # 4. Optional time limit.
    if cfg.max_hold_minutes and now - pos.opened_at >= timedelta(minutes=cfg.max_hold_minutes):
        return sell(one, "MAX_HOLD_TIME")

    return ExitDecision(HOLD, Decimal(0), None, peak, ratio)


def sell_amount(remaining: int, fraction: Decimal) -> int:
    if fraction >= 1:
        return remaining
    return int(Decimal(remaining) * fraction)
