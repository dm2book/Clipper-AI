import type { Chain } from '../core/types.js';
import { date, num, type Queryable } from '../infra/db.js';
import type { WalletClass } from '../wallets/classify.js';
import type { WalletEvent } from '../wallets/model.js';
import type { WalletStats } from '../wallets/stats.js';

interface EventRow {
  chain: Chain;
  signature: string;
  ix_index: number;
  wallet: string;
  token_address: string;
  kind: WalletEvent['kind'];
  status: WalletEvent['status'];
  amount_raw: string;
  value_usd: string | null;
  block_time: Date;
  slot: string | null;
  source: string;
}

const toEvent = (r: EventRow): WalletEvent => ({
  chain: r.chain,
  signature: r.signature,
  ixIndex: r.ix_index,
  wallet: r.wallet,
  tokenAddress: r.token_address,
  kind: r.kind,
  status: r.status,
  amountRaw: BigInt(r.amount_raw),
  valueUsd: num(r.value_usd),
  blockTime: date(r.block_time)!,
  slot: r.slot === null ? null : Number(r.slot),
  source: r.source,
});

/**
 * Stores events; a re-delivered event (same chain, signature, instruction and
 * wallet) is ignored. Returns only the events that were new, so callers never
 * alert twice on the same transaction.
 */
export async function insertWalletEvents(db: Queryable, events: readonly WalletEvent[]): Promise<WalletEvent[]> {
  const inserted: WalletEvent[] = [];
  for (const e of events) {
    const { rowCount } = await db.query(
      `INSERT INTO wallet_events (chain, signature, ix_index, wallet, token_address, kind, status, amount_raw, value_usd,
                                  block_time, slot, source)
       VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, $12)
       ON CONFLICT (chain, signature, ix_index, wallet) DO NOTHING`,
      [
        e.chain,
        e.signature,
        e.ixIndex,
        e.wallet,
        e.tokenAddress,
        e.kind,
        e.status,
        e.amountRaw.toString(),
        e.valueUsd,
        e.blockTime,
        e.slot,
        e.source,
      ],
    );
    if (rowCount !== 1) continue;
    inserted.push(e);
    await db.query(
      `INSERT INTO wallets (chain, address, last_event_at) VALUES ($1, $2, $3)
       ON CONFLICT (chain, address) DO UPDATE SET last_event_at = GREATEST(wallets.last_event_at, EXCLUDED.last_event_at)`,
      [e.chain, e.wallet, e.blockTime],
    );
  }
  return inserted;
}

/** A wallet's events since `since` (optionally for one token), failed ones included. */
export async function walletEvents(
  db: Queryable,
  chain: Chain,
  wallet: string,
  since: Date,
  tokenAddress?: string,
): Promise<WalletEvent[]> {
  const { rows } = await db.query<EventRow>(
    `SELECT * FROM wallet_events
     WHERE chain = $1 AND wallet = $2 AND block_time >= $3 AND ($4::text IS NULL OR token_address = $4)
     ORDER BY block_time, slot NULLS FIRST, signature, ix_index`,
    [chain, wallet, since, tokenAddress ?? null],
  );
  return rows.map(toEvent);
}

/** Successful buys of `token` by active tracked wallets in [from, to]. */
export async function trackedBuys(
  db: Queryable,
  chain: Chain,
  token: string,
  from: Date,
  to: Date,
  minUsd: number,
): Promise<{ wallet: string; valueUsd: number; at: Date }[]> {
  const { rows } = await db.query<{ wallet: string; value_usd: string; block_time: Date }>(
    `SELECT e.wallet, e.value_usd, e.block_time
     FROM wallet_events e
     JOIN tracked_wallets t ON t.chain = e.chain AND t.address = e.wallet AND t.active
     WHERE e.chain = $1 AND e.token_address = $2 AND e.block_time BETWEEN $3 AND $4
       AND e.kind = 'buy' AND e.status = 'success' AND e.value_usd >= $5`,
    [chain, token, from, to, minUsd],
  );
  return rows.map((r) => ({ wallet: r.wallet, valueUsd: num(r.value_usd)!, at: date(r.block_time)! }));
}

export interface TrackedWallet {
  chain: Chain;
  address: string;
  source: 'manual' | 'criteria';
  reason: string;
  active: boolean;
}

export async function getTrackedWallet(db: Queryable, chain: Chain, address: string): Promise<TrackedWallet | null> {
  const { rows } = await db.query<TrackedWallet>(
    `SELECT chain, address, source, reason, active FROM tracked_wallets WHERE chain = $1 AND address = $2 AND active`,
    [chain, address],
  );
  return rows[0] ?? null;
}

export async function listTrackedWallets(db: Queryable, chain: Chain, activeOnly = true): Promise<TrackedWallet[]> {
  const { rows } = await db.query<TrackedWallet>(
    `SELECT chain, address, source, reason, active FROM tracked_wallets
     WHERE chain = $1 AND ($2::boolean = false OR active) ORDER BY address`,
    [chain, activeOnly],
  );
  return rows;
}

/**
 * Makes the manual watchlist equal to `addresses`: listed wallets become
 * active manual entries (also when they were criteria entries), manual
 * entries no longer listed are deactivated. Criteria entries are untouched.
 */
export async function syncManualWallets(db: Queryable, chain: Chain, addresses: readonly string[]): Promise<void> {
  for (const address of addresses) {
    await db.query(
      `INSERT INTO tracked_wallets (chain, address, source, reason) VALUES ($1, $2, 'manual', 'handmatig toegevoegd (TRACKED_WALLETS)')
       ON CONFLICT (chain, address) DO UPDATE
         SET source = 'manual', reason = EXCLUDED.reason, active = true, updated_at = now()`,
      [chain, address],
    );
  }
  await db.query(
    `UPDATE tracked_wallets SET active = false, reason = 'verwijderd uit TRACKED_WALLETS', updated_at = now()
     WHERE chain = $1 AND source = 'manual' AND active AND NOT (address = ANY($2::text[]))`,
    [chain, addresses],
  );
}

/**
 * Follows the classification for criteria-based tracking: a QUALIFIED wallet
 * is (re)activated, any other classification deactivates it. Manual entries
 * are never changed here. Returns 'added' | 'removed' | null (no change).
 */
export async function applyCriteriaTracking(
  db: Queryable,
  chain: Chain,
  address: string,
  qualified: boolean,
  reason: string,
): Promise<'added' | 'removed' | null> {
  if (qualified) {
    const { rows } = await db.query<{ was_active: boolean | null }>(
      `WITH prev AS (SELECT active FROM tracked_wallets WHERE chain = $1 AND address = $2)
       INSERT INTO tracked_wallets (chain, address, source, reason) VALUES ($1, $2, 'criteria', $3)
       ON CONFLICT (chain, address) DO UPDATE SET active = true, reason = EXCLUDED.reason, updated_at = now()
         WHERE tracked_wallets.source = 'criteria'
       RETURNING (SELECT active FROM prev) AS was_active`,
      [chain, address, reason],
    );
    return rows[0] && rows[0].was_active !== true ? 'added' : null;
  }
  const { rowCount } = await db.query(
    `UPDATE tracked_wallets SET active = false, reason = $3, updated_at = now()
     WHERE chain = $1 AND address = $2 AND source = 'criteria' AND active`,
    [chain, address, reason],
  );
  return rowCount ? 'removed' : null;
}

/**
 * Wallets whose statistics are missing or outdated: new events since the last
 * computation, or — for tracked wallets, whose window keeps moving — older
 * than `refreshSec`.
 */
export async function walletsDueForStats(
  db: Queryable,
  limit: number,
  refreshSec: number,
): Promise<{ chain: Chain; address: string }[]> {
  const { rows } = await db.query<{ chain: Chain; address: string }>(
    `SELECT w.chain, w.address FROM wallets w
     WHERE w.stats_computed_at IS NULL
        OR w.stats_computed_at < w.last_event_at
        OR (w.stats_computed_at < now() - make_interval(secs => $2::double precision)
            AND EXISTS (SELECT 1 FROM tracked_wallets t WHERE t.chain = w.chain AND t.address = w.address AND t.active))
     ORDER BY w.stats_computed_at NULLS FIRST
     LIMIT $1`,
    [limit, refreshSec],
  );
  return rows;
}

export interface WalletProfile {
  stats: WalletStats;
  classification: WalletClass;
  criteriaVersion: string;
  computedAt: Date;
}

export async function saveWalletProfile(
  db: Queryable,
  chain: Chain,
  address: string,
  p: WalletProfile,
): Promise<void> {
  await db.query(
    `INSERT INTO wallets (chain, address, last_event_at, stats_computed_at, classification, criteria_version, stats)
     VALUES ($1, $2, $3, $3, $4, $5, $6)
     ON CONFLICT (chain, address) DO UPDATE
       SET stats_computed_at = EXCLUDED.stats_computed_at, classification = EXCLUDED.classification,
           criteria_version = EXCLUDED.criteria_version, stats = EXCLUDED.stats`,
    [chain, address, p.computedAt, p.classification, p.criteriaVersion, JSON.stringify(p.stats)],
  );
}

export async function getWalletProfile(db: Queryable, chain: Chain, address: string): Promise<WalletProfile | null> {
  const { rows } = await db.query<{
    stats: WalletStats | null;
    classification: WalletClass | null;
    criteria_version: string | null;
    stats_computed_at: Date | null;
  }>(
    'SELECT stats, classification, criteria_version, stats_computed_at FROM wallets WHERE chain = $1 AND address = $2',
    [chain, address],
  );
  const r = rows[0];
  if (!r?.stats || !r.classification || !r.criteria_version || !r.stats_computed_at) return null;
  return { stats: r.stats, classification: r.classification, criteriaVersion: r.criteria_version, computedAt: date(r.stats_computed_at)! };
}

export async function deleteWalletEventsBefore(db: Queryable, cutoff: Date): Promise<number> {
  const { rowCount } = await db.query('DELETE FROM wallet_events WHERE block_time < $1', [cutoff]);
  return rowCount ?? 0;
}
