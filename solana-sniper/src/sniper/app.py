"""Wiring: build every component, recover, then run all loops."""

from __future__ import annotations

import asyncio
import json
import logging
import os
import signal
import sys
import time
from pathlib import Path

from sniper.analysis.analyzer import Analyzer
from sniper.analysis.sources import DexScreener, RugCheck
from sniper.config import Settings
from sniper.db import Database
from sniper.detector import LaunchDetector
from sniper.executor import Executor
from sniper.jupiter import Jupiter
from sniper.positions import PositionMonitor
from sniper.ratelimit import TokenBucket
from sniper.recovery import Recovery, recovery_loop
from sniper.solana.rpc import SolanaRpc
from sniper.solana.wallet import Wallet
from sniper.telegram import CommandHandler, TelegramApi, outbox_worker

log = logging.getLogger("sniper")

MIGRATIONS = Path(os.environ.get("SNIPER_MIGRATIONS", Path(__file__).resolve().parents[2] / "migrations"))


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        out = {
            "ts": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(record.created)),
            "level": record.levelname,
            "logger": record.name,
            "msg": record.getMessage(),
        }
        if record.exc_info:
            out["exc"] = self.formatException(record.exc_info)
        return json.dumps(out, ensure_ascii=False)


def setup_logging(level: str) -> None:
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(JsonFormatter())
    logging.basicConfig(level=level.upper(), handlers=[handler], force=True)
    # httpx logs every request URL at INFO; the Telegram URL contains the token.
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("websockets").setLevel(logging.WARNING)


async def health_server(host: str, port: int, db: Database, detector: LaunchDetector) -> None:
    async def handle(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        try:
            await reader.readline()
            try:
                db_ok = await asyncio.wait_for(db.ping(), 3)
            except Exception:
                db_ok = False
            body = {"db": db_ok, "websocket": detector.healthy(), "queue": detector.queue.qsize(),
                    "dropped": detector.dropped}
            ok = db_ok and detector.healthy()
            payload = json.dumps(body).encode()
            status = "200 OK" if ok else "503 Service Unavailable"
            writer.write(f"HTTP/1.1 {status}\r\nContent-Type: application/json\r\n"
                         f"Content-Length: {len(payload)}\r\nConnection: close\r\n\r\n".encode() + payload)
            await writer.drain()
        finally:
            writer.close()

    server = await asyncio.start_server(handle, host, port)
    async with server:
        await server.serve_forever()


async def run(settings: Settings) -> None:
    db = await Database.connect(settings.database.url, min_size=settings.database.min_pool,
                                max_size=settings.database.max_pool)
    applied = await db.migrate(MIGRATIONS)
    if applied:
        log.info("applied migrations: %s", ", ".join(applied))
    if not await db.acquire_instance_lock():
        raise SystemExit("another sniper instance holds the lock on this database; refusing to start")

    rpc = SolanaRpc(settings.rpc.http_url, TokenBucket(settings.rpc.requests_per_second),
                    timeout_s=settings.rpc.timeout_s)
    jupiter = Jupiter(settings.jupiter, settings.trading)
    dexscreener = DexScreener(settings.dexscreener)
    rugcheck = RugCheck(settings.filters.rugcheck) if settings.filters.rugcheck.enabled else None
    wallet = Wallet.from_config(settings.wallet) if settings.trading.mode == "live" else None
    executor = Executor(settings, db, jupiter, rpc, wallet)
    recovery = Recovery(db, executor, rpc)
    detector = LaunchDetector(settings.detection, settings.rpc.ws_url, rpc, db,
                              notify_on_detected=settings.telegram.notify_on_detected)
    analyzer = Analyzer(settings, db, rpc, jupiter, dexscreener, rugcheck, executor)
    monitor = PositionMonitor(settings, db, jupiter, executor)

    tg = settings.telegram
    api = TelegramApi(tg.bot_token) if tg.enabled else None

    log.info("starting in %s mode%s", settings.trading.mode, f", wallet {wallet.address}" if wallet else "")
    # Nothing trades until the database agrees with the chain.
    await recovery.run(startup=True)
    x = settings.exits
    await db.notify(
        f"🤖 Sniper gestart — modus <b>{settings.trading.mode}</b>\n"
        f"${settings.trading.trade_size_usd}/trade · max {settings.trading.max_open_positions} posities · "
        f"SL {x.stop_loss_pct}% · TP {x.take_profit_pct}% · trailing {x.trailing_stop_pct}%",
        priority=2,
    )

    coros = [
        *detector.tasks(),
        *(analyzer.run_worker(i) for i in range(settings.analysis.workers)),
        monitor.run(),
        recovery_loop(recovery),
        outbox_worker(db, api, tg.notify_chat_id),
    ]
    if api is not None and tg.commands_enabled:
        coros.append(CommandHandler(settings, db, api, executor).run())
    if settings.health.enabled:
        coros.append(health_server(settings.health.host, settings.health.port, db, detector))

    tasks = [asyncio.create_task(c) for c in coros]
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, stop.set)

    waiter = asyncio.create_task(stop.wait())
    done, _ = await asyncio.wait([waiter, *tasks], return_when=asyncio.FIRST_COMPLETED)
    crashed = [t for t in done if t is not waiter and t.exception()]
    for t in crashed:
        log.error("task crashed", exc_info=t.exception())

    log.info("shutting down")
    for t in tasks:
        t.cancel()
    await asyncio.gather(*tasks, return_exceptions=True)
    for closer in (rpc.close, jupiter.close, dexscreener.close):
        await closer()
    if rugcheck:
        await rugcheck.close()
    if api:
        await api.close()
    await db.close()
    if crashed:
        raise SystemExit(1)  # let Docker restart us
