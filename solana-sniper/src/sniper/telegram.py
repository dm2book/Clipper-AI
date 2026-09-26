"""Telegram: the outbox sender and the command handler.

Both use the plain Bot API over HTTPS with long polling, so the server needs
no open inbound port.
"""

from __future__ import annotations

import asyncio
import logging
from decimal import Decimal
from html import escape

import httpx

from sniper.config import Settings
from sniper.db import Database
from sniper.executor import Executor, sol
from sniper.ratelimit import TokenBucket

log = logging.getLogger(__name__)

MAX_ATTEMPTS = 8


class TelegramApi:
    def __init__(self, token: str, client: httpx.AsyncClient | None = None):
        self._base = f"https://api.telegram.org/bot{token}"
        self._client = client or httpx.AsyncClient(timeout=40)
        self._limiter = TokenBucket(1.0, burst=3)  # stay well under Telegram's per-chat limit

    async def close(self) -> None:
        await self._client.aclose()

    async def send(self, chat_id: int, text: str) -> None:
        await self._limiter.acquire()
        resp = await self._client.post(f"{self._base}/sendMessage", json={
            "chat_id": chat_id, "text": text[:4000], "parse_mode": "HTML", "disable_web_page_preview": True,
        })
        if resp.status_code == 429:
            retry = resp.json().get("parameters", {}).get("retry_after", 5)
            await asyncio.sleep(retry)
            raise RuntimeError(f"rate limited ({retry}s)")
        if resp.status_code != 200:
            # Never log the URL: it contains the bot token.
            raise RuntimeError(f"sendMessage HTTP {resp.status_code}: {resp.text[:200]}")

    async def updates(self, offset: int | None) -> list[dict]:
        resp = await self._client.get(f"{self._base}/getUpdates", params={
            "timeout": 30, "offset": offset, "allowed_updates": '["message"]'})
        if resp.status_code != 200:
            raise RuntimeError(f"getUpdates HTTP {resp.status_code}")
        return resp.json().get("result", [])


async def outbox_worker(db: Database, api: TelegramApi | None, chat_id: int | None) -> None:
    """Deliver queued notifications. With Telegram disabled they are only logged."""
    while True:
        try:
            batch = await db.next_notifications()
            if not batch:
                await asyncio.sleep(1)
                continue
            for n in batch:
                if api is None or chat_id is None:
                    log.info("notification: %s", n["body"])
                    await db.mark_notification(n["id"], sent=True)
                    continue
                try:
                    await api.send(chat_id, n["body"])
                    await db.mark_notification(n["id"], sent=True)
                except Exception as exc:
                    give_up = n["attempts"] + 1 >= MAX_ATTEMPTS
                    await db.mark_notification(n["id"], sent=False, error=str(exc)[:300], give_up=give_up)
                    log.warning("telegram send failed (%s)%s", exc, "; giving up" if give_up else "")
                    await asyncio.sleep(min(30, 2 ** n["attempts"]))
                    break
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("outbox worker failed")
            await asyncio.sleep(5)


class CommandHandler:
    HELP = (
        "/status — overzicht\n"
        "/positions — open posities\n"
        "/pause — geen nieuwe aankopen (exits blijven actief)\n"
        "/resume — aankopen weer toestaan\n"
        "/sell &lt;id&gt; CONFIRM — positie direct verkopen\n"
        "/sellall CONFIRM — alle posities verkopen en pauzeren"
    )

    def __init__(self, settings: Settings, db: Database, api: TelegramApi, executor: Executor):
        self.cfg = settings
        self.db = db
        self.api = api
        self.ex = executor
        self.allowed = set(settings.telegram.allowed_chat_ids)

    async def run(self) -> None:
        offset = None
        while True:
            try:
                for upd in await self.api.updates(offset):
                    offset = upd["update_id"] + 1
                    msg = upd.get("message") or {}
                    chat = (msg.get("chat") or {}).get("id")
                    text = (msg.get("text") or "").strip()
                    if not text.startswith("/"):
                        continue
                    if chat not in self.allowed:
                        log.warning("ignored command from unauthorised chat %s", chat)
                        await self.db.event("TELEGRAM_UNAUTHORISED", data={"chat": chat, "text": text[:100]})
                        continue
                    await self.db.event("TELEGRAM_COMMAND", data={"chat": chat, "text": text[:100]})
                    reply = await self.handle(text)
                    await self.api.send(chat, reply)
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("telegram command loop failed")
                await asyncio.sleep(5)

    async def handle(self, text: str) -> str:
        parts = text.split()
        cmd = parts[0].split("@")[0].lower()
        args = parts[1:]
        if cmd in ("/start", "/help"):
            return self.HELP
        if cmd == "/status":
            return await self._status()
        if cmd == "/positions":
            return await self._positions()
        if cmd == "/pause":
            await self.db.set_state("paused", "1")
            return "⏸️ Gepauzeerd: geen nieuwe aankopen. Stop-loss/take-profit blijven actief."
        if cmd == "/resume":
            await self.db.set_state("paused", "0")
            return "▶️ Hervat."
        if cmd == "/sell":
            if len(args) != 2 or args[1] != "CONFIRM" or not args[0].isdigit():
                return "Gebruik: /sell &lt;id&gt; CONFIRM"
            ok = await self.ex.sell(int(args[0]), Decimal(1), "MANUAL")
            return "Verkoop uitgevoerd." if ok else "Verkoop niet gelukt of positie niet open; zie meldingen."
        if cmd == "/sellall":
            if args != ["CONFIRM"]:
                return "Gebruik: /sellall CONFIRM"
            await self.db.set_state("paused", "1")
            results = []
            for pos in await self.db.positions_by_status("OPEN"):
                ok = await self.ex.sell(pos["id"], Decimal(1), "MANUAL")
                results.append(f"#{pos['id']}: {'ok' if ok else 'mislukt'}")
            return "Gepauzeerd. " + (", ".join(results) or "Geen open posities.")
        return "Onbekend commando.\n" + self.HELP

    async def _status(self) -> str:
        counts = await self.db.token_counts()
        active = await self.db.count_active_positions()
        paused = await self.db.is_paused()
        mode = self.cfg.trading.mode
        c = ", ".join(f"{k.lower()}: {v}" for k, v in sorted(counts.items())) or "geen"
        return (f"<b>Status</b> — modus {mode}{' · GEPAUZEERD' if paused else ''}\n"
                f"Open posities: {active}/{self.cfg.trading.max_open_positions}\n"
                f"Tokens (24u): {c}")

    async def _positions(self) -> str:
        rows = await self.db.positions_by_status("OPEN", "OPENING", "CLOSING")
        if not rows:
            return "Geen open posities."
        lines = []
        for p in rows:
            label = escape(p["symbol"] or p["mint"][:8])
            if p["entry_price"] and p["last_price"]:
                chg = (Decimal(p["last_price"]) / Decimal(p["entry_price"]) - 1) * 100
                lines.append(f"#{p['id']} <b>{label}</b> {p['status']} · {chg:+.1f}% · inzet {sol(p['entry_lamports'])}"
                             f"{' · TP geraakt' if p['tp_hit'] else ''}")
            else:
                lines.append(f"#{p['id']} <b>{label}</b> {p['status']}")
        return "\n".join(lines)
