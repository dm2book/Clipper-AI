"""Bring the database back in line with the chain.

Runs once at startup (before anything trades) and then periodically, so a
trade whose confirmation was interrupted never stays in limbo.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timedelta, timezone
from html import escape

from sniper.db import Database
from sniper.executor import CONFIRMED, Executor
from sniper.solana.rpc import SolanaRpc

log = logging.getLogger(__name__)

# A pending trade younger than this is probably still being confirmed by the
# code that sent it; only startup recovery (when nothing else runs) ignores it.
_GRACE = timedelta(seconds=120)


class Recovery:
    def __init__(self, db: Database, executor: Executor, rpc: SolanaRpc):
        self.db = db
        self.ex = executor
        self.rpc = rpc

    async def run(self, *, startup: bool) -> None:
        now = datetime.now(timezone.utc)
        await self._settle_pending(now, startup)
        await self._fix_stuck_positions(startup)
        if startup and not self.ex.paper:
            await self._reconcile_balances()

    async def _settle_pending(self, now: datetime, startup: bool) -> None:
        for t in await self.db.pending_trades():
            if t["signature"] in self.ex.in_flight or t["position_id"] in self.ex.busy:
                continue
            if not startup and now - t["created_at"] < _GRACE:
                continue
            pos = await self.db.get_position(t["position_id"])
            label = escape(pos["symbol"] or pos["mint"][:8])
            if t["paper"]:
                await self.db.settle_trade(t["id"], status="FAILED", error="interrupted (paper)")
                outcome_ok = False
                outcome = None
            else:
                log.info("recovering trade %s (%s)", t["id"], t["signature"])
                try:
                    outcome = await self.ex.await_and_settle(
                        t["id"], t["signature"], t["last_valid_block_height"], side=t["side"],
                        mint=pos["mint"], quoted_in=int(t["quoted_in_amount"]))
                except Exception:
                    log.exception("could not settle trade %s yet", t["id"])
                    continue
                outcome_ok = outcome.status == CONFIRMED
            await self.db.event("TRADE_RECOVERED", mint=pos["mint"], position_id=pos["id"],
                                data={"trade": t["id"], "confirmed": outcome_ok})
            if t["side"] == "BUY":
                if outcome_ok:
                    await self.ex._open_position(pos["id"], pos["mint"], label, outcome, None)
                else:
                    await self.ex._buy_failed(pos["id"], pos["mint"], label,
                                              (outcome.error if outcome else None) or "interrupted")
            else:
                if outcome_ok:
                    await self.ex._apply_sell(pos, label, t["reason"], outcome)
                else:
                    await self.ex._sell_failed(pos, label, t["reason"],
                                               (outcome.error if outcome else None) or "interrupted")

    async def _fix_stuck_positions(self, startup: bool) -> None:
        for pos in await self.db.positions_by_status("OPENING", "CLOSING"):
            if pos["id"] in self.ex.busy or await self.db.has_pending_trade(pos["id"]):
                continue
            if not startup:
                continue  # between claim and trade insert: only safe to fix at startup
            if pos["status"] == "OPENING":
                await self.db.update_position(pos["id"], status="FAILED", close_reason="interrupted before sending",
                                              closed_at=datetime.now(timezone.utc))
            else:
                await self.db.update_position(pos["id"], status="OPEN")
            await self.db.event("POSITION_RESET", mint=pos["mint"], position_id=pos["id"],
                                data={"from": pos["status"]})

    async def _reconcile_balances(self) -> None:
        """Tokens sold or moved outside the bot must not be sold twice."""
        for pos in await self.db.positions_by_status("OPEN"):
            try:
                onchain = await self.rpc.get_token_balance(self.ex.wallet.address, pos["mint"])
            except Exception:
                log.exception("balance check for %s failed", pos["mint"])
                continue
            recorded = int(pos["remaining_amount"])
            if onchain >= recorded:
                continue
            label = escape(pos["symbol"] or pos["mint"][:8])
            if onchain == 0:
                await self.db.update_position(pos["id"], status="CLOSED", remaining_amount=0,
                                              closed_at=datetime.now(timezone.utc), close_reason="EXTERNAL")
                msg = f"ℹ️ Positie #{pos['id']} {label}: tokens niet meer in wallet — als extern gesloten gemarkeerd."
            else:
                await self.db.update_position(pos["id"], remaining_amount=onchain)
                msg = f"ℹ️ Positie #{pos['id']} {label}: saldo gecorrigeerd {recorded} → {onchain}."
            await self.db.event("BALANCE_RECONCILED", mint=pos["mint"], position_id=pos["id"],
                                data={"recorded": recorded, "onchain": onchain})
            await self.db.notify(msg, priority=1)


async def recovery_loop(recovery: Recovery, interval_s: float = 60) -> None:
    while True:
        await asyncio.sleep(interval_s)
        try:
            await recovery.run(startup=False)
        except Exception:
            log.exception("periodic recovery failed")
