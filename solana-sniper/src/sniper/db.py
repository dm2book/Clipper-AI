"""PostgreSQL access: pool, migrations, single-instance lock, and queries.

All SQL lives here so the rest of the code deals in plain values.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any

import asyncpg

log = logging.getLogger(__name__)

# Arbitrary constant; two bots pointed at the same database cannot both hold it.
INSTANCE_LOCK_KEY = 0x5E1FE5


async def _init_conn(conn: asyncpg.Connection) -> None:
    for typ in ("json", "jsonb"):
        await conn.set_type_codec(typ, encoder=json.dumps, decoder=json.loads, schema="pg_catalog")


class Database:
    def __init__(self, pool: asyncpg.Pool):
        self.pool = pool
        self._lock_conn: asyncpg.Connection | None = None

    @classmethod
    async def connect(cls, url: str, *, min_size: int = 2, max_size: int = 10) -> "Database":
        pool = await asyncpg.create_pool(url, min_size=min_size, max_size=max_size, init=_init_conn)
        return cls(pool)

    async def close(self) -> None:
        if self._lock_conn is not None:
            await self._lock_conn.close()
        await self.pool.close()

    # --- lifecycle --------------------------------------------------------

    async def migrate(self, directory: Path) -> list[str]:
        applied: list[str] = []
        async with self.pool.acquire() as conn:
            await conn.execute(
                "CREATE TABLE IF NOT EXISTS schema_migrations (name text PRIMARY KEY, applied_at timestamptz NOT NULL DEFAULT now())"
            )
            # Serialise concurrent migrators (e.g. two containers starting at once).
            await conn.execute("SELECT pg_advisory_lock($1)", INSTANCE_LOCK_KEY + 1)
            try:
                done = {r["name"] for r in await conn.fetch("SELECT name FROM schema_migrations")}
                for path in sorted(directory.glob("*.sql")):
                    if path.name in done:
                        continue
                    async with conn.transaction():
                        await conn.execute(path.read_text(encoding="utf-8"))
                        await conn.execute("INSERT INTO schema_migrations (name) VALUES ($1)", path.name)
                    applied.append(path.name)
            finally:
                await conn.execute("SELECT pg_advisory_unlock($1)", INSTANCE_LOCK_KEY + 1)
        return applied

    async def acquire_instance_lock(self) -> bool:
        """Session-level advisory lock held on a dedicated connection for the
        lifetime of the process. Released automatically if the process dies."""
        conn = await self.pool.acquire()
        ok = await conn.fetchval("SELECT pg_try_advisory_lock($1)", INSTANCE_LOCK_KEY)
        if ok:
            self._lock_conn = conn
        else:
            await self.pool.release(conn)
        return bool(ok)

    async def ping(self) -> bool:
        return await self.pool.fetchval("SELECT 1") == 1

    # --- state / events / notifications ------------------------------------

    async def get_state(self, key: str, default: str | None = None) -> str | None:
        v = await self.pool.fetchval("SELECT value FROM bot_state WHERE key = $1", key)
        return default if v is None else v

    async def set_state(self, key: str, value: str) -> None:
        await self.pool.execute(
            "INSERT INTO bot_state (key, value) VALUES ($1, $2) "
            "ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value, updated_at = now()",
            key, value,
        )

    async def is_paused(self) -> bool:
        return (await self.get_state("paused", "0")) == "1"

    async def event(self, kind: str, *, mint: str | None = None, position_id: int | None = None,
                    data: dict | None = None, conn: Any = None) -> None:
        await (conn or self.pool).execute(
            "INSERT INTO events (kind, mint, position_id, data) VALUES ($1, $2, $3, $4)",
            kind, mint, position_id, data or {},
        )

    async def notify(self, body: str, *, priority: int = 1, dedupe_key: str | None = None,
                     conn: Any = None) -> None:
        await (conn or self.pool).execute(
            "INSERT INTO notifications (body, priority, dedupe_key) VALUES ($1, $2, $3) "
            "ON CONFLICT (dedupe_key) DO NOTHING",
            body, priority, dedupe_key,
        )

    async def next_notifications(self, limit: int = 10) -> list[asyncpg.Record]:
        return await self.pool.fetch(
            "SELECT id, body, attempts FROM notifications WHERE status = 'PENDING' "
            "ORDER BY priority, id LIMIT $1", limit,
        )

    async def mark_notification(self, nid: int, *, sent: bool, error: str | None = None,
                                give_up: bool = False) -> None:
        if sent:
            await self.pool.execute(
                "UPDATE notifications SET status = 'SENT', sent_at = now(), attempts = attempts + 1 WHERE id = $1", nid)
        else:
            await self.pool.execute(
                "UPDATE notifications SET attempts = attempts + 1, last_error = $2, "
                "status = CASE WHEN $3 THEN 'FAILED' ELSE status END WHERE id = $1",
                nid, error, give_up,
            )

    # --- tokens -------------------------------------------------------------

    async def insert_token(self, mint: str, source: str, signature: str | None) -> bool:
        res = await self.pool.execute(
            "INSERT INTO tokens (mint, source, detect_signature) VALUES ($1, $2, $3) ON CONFLICT (mint) DO NOTHING",
            mint, source, signature,
        )
        return res.endswith(" 1")

    async def claim_due_tokens(self, limit: int, lease_s: int) -> list[asyncpg.Record]:
        """Hand out due tokens to one worker. The lease pushes next_check_at
        forward so no other worker picks the same token while it is analysed
        (the analysis itself runs outside any transaction)."""
        async with self.pool.acquire() as conn, conn.transaction():
            return await conn.fetch(
                """
                WITH due AS (
                    SELECT mint FROM tokens
                    WHERE status = 'WATCHING' AND next_check_at <= now()
                    ORDER BY next_check_at
                    LIMIT $1
                    FOR UPDATE SKIP LOCKED
                )
                UPDATE tokens t SET next_check_at = now() + make_interval(secs => $2)
                FROM due WHERE t.mint = due.mint
                RETURNING t.mint, t.detected_at, t.check_count, t.source
                """,
                limit, lease_s,
            )

    async def record_analysis(self, mint: str, *, verdict: str, status: str, reason: str,
                              checks: list, report: dict, next_check_in_s: int,
                              liquidity_usd: Decimal | None, holders: int | None,
                              top10_pct: Decimal | None, symbol: str | None, name: str | None,
                              pool_address: str | None, decimals: int | None) -> None:
        async with self.pool.acquire() as conn, conn.transaction():
            await conn.execute(
                "INSERT INTO token_analyses (mint, verdict, liquidity_usd, holders, top10_pct, checks, report) "
                "VALUES ($1, $2, $3, $4, $5, $6, $7)",
                mint, verdict, liquidity_usd, holders, top10_pct, checks, report,
            )
            await conn.execute(
                """
                UPDATE tokens SET status = $2, last_reason = $3, last_checked_at = now(),
                    check_count = check_count + 1,
                    next_check_at = now() + make_interval(secs => $4),
                    symbol = COALESCE($5, symbol), name = COALESCE($6, name),
                    pool_address = COALESCE($7, pool_address), decimals = COALESCE($8, decimals)
                WHERE mint = $1
                """,
                mint, status, reason, next_check_in_s, symbol, name, pool_address, decimals,
            )

    async def set_token_status(self, mint: str, status: str, reason: str | None = None) -> None:
        await self.pool.execute(
            "UPDATE tokens SET status = $2, last_reason = COALESCE($3, last_reason) WHERE mint = $1",
            mint, status, reason,
        )

    async def expire_tokens(self, window_s: int) -> int:
        res = await self.pool.execute(
            "UPDATE tokens SET status = 'EXPIRED', last_reason = COALESCE(last_reason, '') || ' [window expired]' "
            "WHERE status = 'WATCHING' AND detected_at < now() - make_interval(secs => $1)",
            window_s,
        )
        return int(res.split()[-1])

    async def token_counts(self) -> dict[str, int]:
        rows = await self.pool.fetch(
            "SELECT status, count(*) AS n FROM tokens WHERE detected_at > now() - interval '24 hours' GROUP BY status")
        return {r["status"]: r["n"] for r in rows}

    # --- positions & trades ----------------------------------------------

    async def create_opening_position(self, mint: str, decimals: int) -> int | None:
        try:
            return await self.pool.fetchval(
                "INSERT INTO positions (mint, status, decimals) VALUES ($1, 'OPENING', $2) RETURNING id",
                mint, decimals,
            )
        except asyncpg.UniqueViolationError:
            return None

    async def get_position(self, pid: int) -> asyncpg.Record | None:
        return await self.pool.fetchrow(
            "SELECT p.*, t.symbol FROM positions p JOIN tokens t USING (mint) WHERE p.id = $1", pid)

    async def positions_by_status(self, *statuses: str) -> list[asyncpg.Record]:
        return await self.pool.fetch(
            "SELECT p.*, t.symbol FROM positions p JOIN tokens t USING (mint) "
            "WHERE p.status = ANY($1::text[]) ORDER BY p.id", list(statuses),
        )

    async def count_active_positions(self) -> int:
        return await self.pool.fetchval(
            "SELECT count(*) FROM positions WHERE status IN ('OPENING','OPEN','CLOSING')")

    async def buys_since(self, since: datetime) -> int:
        return await self.pool.fetchval(
            "SELECT count(*) FROM trades WHERE side = 'BUY' AND created_at >= $1 AND status <> 'FAILED'", since)

    async def realized_pnl_lamports_since(self, since: datetime) -> int:
        """Net SOL result of positions closed since ``since``, fees included."""
        v = await self.pool.fetchval(
            """
            SELECT COALESCE(sum(p.realized_lamports - p.entry_lamports - COALESCE(f.fees, 0)), 0)
            FROM positions p
            LEFT JOIN (SELECT position_id, sum(fee_lamports) AS fees FROM trades
                       WHERE status = 'CONFIRMED' GROUP BY position_id) f ON f.position_id = p.id
            WHERE p.status = 'CLOSED' AND p.closed_at >= $1
            """,
            since,
        )
        return int(v)

    async def traded_before(self, mint: str) -> bool:
        return await self.pool.fetchval(
            "SELECT EXISTS (SELECT 1 FROM positions WHERE mint = $1 AND status <> 'FAILED')", mint)

    async def claim_for_sell(self, pid: int) -> bool:
        """OPEN -> CLOSING, atomically. False if someone else got there first."""
        res = await self.pool.execute(
            "UPDATE positions SET status = 'CLOSING' WHERE id = $1 AND status = 'OPEN'", pid)
        return res.endswith(" 1")

    async def update_position(self, pid: int, **fields: Any) -> None:
        if not fields:
            return
        cols = list(fields)
        sets = ", ".join(f"{c} = ${i + 2}" for i, c in enumerate(cols))
        await self.pool.execute(f"UPDATE positions SET {sets} WHERE id = $1", pid, *[fields[c] for c in cols])

    async def insert_trade(self, *, position_id: int, side: str, reason: str, signature: str,
                           paper: bool, quoted_in: int, quoted_out: int,
                           last_valid_block_height: int | None) -> int:
        return await self.pool.fetchval(
            """
            INSERT INTO trades (position_id, side, reason, signature, status, paper,
                                quoted_in_amount, quoted_out_amount, last_valid_block_height)
            VALUES ($1, $2, $3, $4, 'PENDING', $5, $6, $7, $8) RETURNING id
            """,
            position_id, side, reason, signature, paper, quoted_in, quoted_out, last_valid_block_height,
        )

    async def settle_trade(self, tid: int, *, status: str, lamports: int | None = None,
                           token_amount: int | None = None, fee_lamports: int | None = None,
                           error: str | None = None) -> None:
        await self.pool.execute(
            "UPDATE trades SET status = $2, lamports = $3, token_amount = $4, fee_lamports = $5, "
            "error = $6, settled_at = now() WHERE id = $1",
            tid, status, lamports, token_amount, fee_lamports, error,
        )

    async def pending_trades(self) -> list[asyncpg.Record]:
        return await self.pool.fetch("SELECT * FROM trades WHERE status = 'PENDING' ORDER BY id")

    async def has_pending_trade(self, pid: int) -> bool:
        return await self.pool.fetchval(
            "SELECT EXISTS (SELECT 1 FROM trades WHERE position_id = $1 AND status = 'PENDING')", pid)

    async def position_fees(self, pid: int) -> int:
        return int(await self.pool.fetchval(
            "SELECT COALESCE(sum(fee_lamports), 0) FROM trades WHERE position_id = $1 AND status = 'CONFIRMED'",
            pid))


def utc_midnight(now: datetime | None = None) -> datetime:
    now = now or datetime.now(timezone.utc)
    return now.replace(hour=0, minute=0, second=0, microsecond=0)


def hour_ago(now: datetime | None = None) -> datetime:
    return (now or datetime.now(timezone.utc)) - timedelta(hours=1)
