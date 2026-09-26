"""The last gate before a buy, as a pure function over a snapshot."""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal

from sniper.config import TradingConfig


@dataclass(frozen=True)
class RiskSnapshot:
    paused: bool
    active_positions: int
    buys_last_hour: int
    daily_pnl_usd: Decimal          # negative = loss
    sol_balance_lamports: int | None  # None in paper mode
    trade_lamports: int
    reserve_lamports: int
    mint_denied: bool = False


@dataclass(frozen=True)
class RiskDecision:
    allowed: bool
    reason: str


def check_buy(s: RiskSnapshot, cfg: TradingConfig) -> RiskDecision:
    if s.paused:
        return RiskDecision(False, "bot is paused")
    if s.mint_denied:
        return RiskDecision(False, "token already traded")
    if s.active_positions >= cfg.max_open_positions:
        return RiskDecision(False, f"max open positions reached ({s.active_positions}/{cfg.max_open_positions})")
    if s.buys_last_hour >= cfg.max_buys_per_hour:
        return RiskDecision(False, f"max buys per hour reached ({s.buys_last_hour})")
    if -s.daily_pnl_usd >= cfg.max_daily_loss_usd:
        return RiskDecision(False, f"daily loss limit reached (${-s.daily_pnl_usd:.2f})")
    if s.sol_balance_lamports is not None and s.sol_balance_lamports < s.trade_lamports + s.reserve_lamports:
        return RiskDecision(False, "insufficient SOL (trade size + reserve)")
    return RiskDecision(True, "ok")
