"""Watches open positions and triggers exits."""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone
from decimal import Decimal
from html import escape

from sniper.config import Settings
from sniper.db import Database
from sniper.executor import Executor
from sniper.exits import SELL, PositionView, evaluate_exit
from sniper.jupiter import Jupiter, JupiterError, NoRoute
from sniper.solana.constants import WSOL_MINT

log = logging.getLogger(__name__)


class PositionMonitor:
    def __init__(self, settings: Settings, db: Database, jupiter: Jupiter, executor: Executor):
        self.cfg = settings
        self.db = db
        self.jup = jupiter
        self.ex = executor
        self._tasks: set[asyncio.Task] = set()

    async def run(self) -> None:
        while True:
            try:
                await self.tick()
            except Exception:
                log.exception("position monitor tick failed")
            await asyncio.sleep(self.cfg.exits.monitor_interval_s)

    async def tick(self) -> None:
        for pos in await self.db.positions_by_status("OPEN"):
            if pos["id"] in self.ex.busy:
                continue
            await self.check(pos)

    async def check(self, pos) -> None:
        remaining = int(pos["remaining_amount"])
        if remaining <= 0:
            return
        try:
            q = await self.jup.quote(pos["mint"], WSOL_MINT, remaining, self.cfg.trading.sell_slippage_bps)
        except NoRoute:
            await self.db.notify(
                f"🚨 Geen verkoop-route meer voor <b>{escape(pos['symbol'] or pos['mint'][:8])}</b> "
                f"(positie #{pos['id']}). Pool mogelijk leeggehaald.",
                priority=0, dedupe_key=f"noroute:{pos['id']}")
            return
        except JupiterError as exc:
            log.warning("quote for position %s failed: %s", pos["id"], exc)
            return

        price = Decimal(q.out_amount) / Decimal(remaining)
        view = PositionView(
            entry_price=Decimal(pos["entry_price"]),
            peak_price=Decimal(pos["peak_price"]) if pos["peak_price"] is not None else None,
            tp_hit=pos["tp_hit"],
            opened_at=pos["opened_at"],
        )
        decision = evaluate_exit(view, price, datetime.now(timezone.utc), self.cfg.exits)
        await self.db.update_position(pos["id"], last_price=price, peak_price=decision.peak_price)
        if decision.action == SELL:
            log.info("position %s: %s at ratio %.3f", pos["id"], decision.reason, decision.ratio)
            await self.db.event("EXIT_TRIGGERED", mint=pos["mint"], position_id=pos["id"], data={
                "reason": decision.reason, "ratio": str(decision.ratio), "fraction": str(decision.fraction)})
            # Sell in the background so one slow confirmation does not stall
            # the other positions' stop-losses.
            task = asyncio.create_task(self.ex.sell(pos["id"], decision.fraction, decision.reason))
            self._tasks.add(task)
            task.add_done_callback(self._tasks.discard)
