"""Launch detection: WebSocket log subscriptions -> signatures -> new mints."""

from __future__ import annotations

import asyncio
import json
import logging
import random
import time

import websockets

from sniper.config import DetectionConfig, LaunchSource
from sniper.db import Database
from sniper.solana.rpc import SolanaRpc
from sniper.solana.txparse import extract_new_mint

log = logging.getLogger(__name__)


def is_launch(logs: list[str], source: LaunchSource) -> bool:
    return any(p in line for line in logs for p in source.log_contains)


class LaunchDetector:
    def __init__(self, cfg: DetectionConfig, ws_url: str, rpc: SolanaRpc, db: Database,
                 *, notify_on_detected: bool = False):
        self.cfg = cfg
        self.ws_url = ws_url
        self.rpc = rpc
        self.db = db
        self.notify_on_detected = notify_on_detected
        self.queue: asyncio.Queue[tuple[str, str]] = asyncio.Queue(maxsize=cfg.queue_size)
        self.last_message_at = time.monotonic()
        self.dropped = 0
        self._seen: dict[str, float] = {}  # signature -> seen at, bounded
        self.connected: set[str] = set()   # sources with a live subscription

    def tasks(self) -> list:
        subs = [self.subscribe(s) for s in self.cfg.sources if s.enabled]
        workers = [self.resolve_worker() for _ in range(self.cfg.resolver_workers)]
        return subs + workers

    def healthy(self) -> bool:
        return bool(self.connected) and time.monotonic() - self.last_message_at < self.cfg.stale_after_s

    # --- subscriptions ------------------------------------------------------

    async def subscribe(self, source: LaunchSource) -> None:
        backoff = 1.0
        while True:
            try:
                async with websockets.connect(self.ws_url, ping_interval=20, ping_timeout=20,
                                              max_size=2 ** 22) as ws:
                    await ws.send(json.dumps({
                        "jsonrpc": "2.0", "id": 1, "method": "logsSubscribe",
                        "params": [{"mentions": [source.program_id]}, {"commitment": "confirmed"}],
                    }))
                    log.info("subscribed to %s (%s)", source.name, source.program_id)
                    backoff = 1.0
                    self.connected.add(source.name)
                    self.last_message_at = time.monotonic()
                    async for raw in ws:
                        self.last_message_at = time.monotonic()
                        self._handle(source, raw)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                log.warning("subscription %s dropped: %s; reconnecting in %.0fs", source.name, exc, backoff)
            finally:
                self.connected.discard(source.name)
            await asyncio.sleep(backoff * (0.5 + random.random()))
            backoff = min(backoff * 2, 60)

    def _handle(self, source: LaunchSource, raw: str | bytes) -> None:
        msg = json.loads(raw)
        if msg.get("method") != "logsNotification":
            if "error" in msg:
                log.error("subscription %s error: %s", source.name, msg["error"])
            return
        value = msg["params"]["result"]["value"]
        if value.get("err") is not None or not is_launch(value.get("logs") or [], source):
            return
        sig = value["signature"]
        if sig in self._seen:
            return
        self._remember(sig)
        try:
            self.queue.put_nowait((source.name, sig))
        except asyncio.QueueFull:
            # Falling behind: dropping new work beats unbounded memory. The
            # counter shows up in /health so it is visible.
            self.dropped += 1

    def _remember(self, sig: str) -> None:
        now = time.monotonic()
        self._seen[sig] = now
        if len(self._seen) > 50_000:
            cutoff = now - 600
            self._seen = {s: t for s, t in self._seen.items() if t > cutoff}

    # --- resolving signatures to mints --------------------------------------

    async def resolve_worker(self) -> None:
        quote_mints = set(self.cfg.quote_mints)
        while True:
            source, sig = await self.queue.get()
            try:
                tx = None
                # "confirmed" notifications can precede getTransaction visibility.
                for delay in (0, 1, 2, 4):
                    await asyncio.sleep(delay)
                    tx = await self.rpc.get_transaction(sig)
                    if tx is not None:
                        break
                if tx is None:
                    log.debug("transaction %s not retrievable", sig)
                    continue
                mint = extract_new_mint(tx, quote_mints)
                if mint is None:
                    continue
                if await self.db.insert_token(mint, source, sig):
                    log.info("new token %s via %s", mint, source)
                    await self.db.event("TOKEN_DETECTED", mint=mint, data={"source": source, "signature": sig})
                    if self.notify_on_detected:
                        await self.db.notify(f"🆕 Nieuwe pool ({source}): <code>{mint}</code>", priority=2)
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("resolving %s failed", sig)
            finally:
                self.queue.task_done()
