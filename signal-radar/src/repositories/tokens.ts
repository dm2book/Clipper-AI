import type { TierSettings } from '../config/env.js';
import type { DiscoveredToken, Token, TokenTier } from '../core/token.js';
import type { Chain } from '../core/types.js';
import { date, int, type Queryable } from '../infra/db.js';

interface TokenRow {
  chain: Chain;
  address: string;
  symbol: string | null;
  name: string | null;
  decimals: number | null;
  supply: string | null;
  token_program: string | null;
  created_at_chain: Date | null;
  detected_at: Date;
  detection_source: string;
  detection_ref: string | null;
  tier: TokenTier;
  next_snapshot_at: Date;
  next_enrich_at: Date;
  snapshot_misses: number;
  last_snapshot_at: Date | null;
  archived_at: Date | null;
  archived_reason: string | null;
}

function toToken(r: TokenRow): Token {
  return {
    chain: r.chain,
    address: r.address,
    symbol: r.symbol,
    name: r.name,
    decimals: int(r.decimals),
    supply: r.supply,
    tokenProgram: r.token_program,
    createdAtChain: date(r.created_at_chain),
    detectedAt: date(r.detected_at)!,
    detectionSource: r.detection_source,
    detectionRef: r.detection_ref,
    tier: r.tier,
    nextSnapshotAt: date(r.next_snapshot_at)!,
    nextEnrichAt: date(r.next_enrich_at)!,
    snapshotMisses: r.snapshot_misses,
    lastSnapshotAt: date(r.last_snapshot_at),
    archivedAt: date(r.archived_at),
    archivedReason: r.archived_reason,
  };
}

/** Returns true when the token is new. Re-detections (another pool) are ignored. */
export async function insertDiscoveredToken(db: Queryable, t: DiscoveredToken): Promise<boolean> {
  const { rowCount } = await db.query(
    `INSERT INTO tokens (chain, address, created_at_chain, detection_source, detection_ref)
     VALUES ($1, $2, $3, $4, $5)
     ON CONFLICT (chain, address) DO NOTHING`,
    [t.chain, t.address, t.createdAtChain, t.source, t.reference],
  );
  return rowCount === 1;
}

export async function getToken(db: Queryable, chain: Chain, address: string): Promise<Token | null> {
  const { rows } = await db.query<TokenRow>('SELECT * FROM tokens WHERE chain = $1 AND address = $2', [
    chain,
    address,
  ]);
  return rows[0] ? toToken(rows[0]) : null;
}

/**
 * Hand out due tokens to exactly one worker. The lease moves `next_*_at`
 * forward inside the same statement, so a crashed worker's tokens come back
 * by themselves once the lease expires.
 */
async function claim(
  db: Queryable,
  column: 'next_snapshot_at' | 'next_enrich_at',
  chain: Chain,
  limit: number,
  leaseSec: number,
): Promise<Token[]> {
  const { rows } = await db.query<TokenRow>(
    `WITH due AS (
       SELECT chain, address FROM tokens
       WHERE chain = $1 AND tier <> 'ARCHIVED' AND ${column} <= now()
       ORDER BY ${column}
       LIMIT $2
       FOR UPDATE SKIP LOCKED
     )
     UPDATE tokens t SET ${column} = now() + make_interval(secs => $3::double precision)
     FROM due WHERE t.chain = due.chain AND t.address = due.address
     RETURNING t.*`,
    [chain, limit, leaseSec],
  );
  return rows.map(toToken);
}

export function claimDueForSnapshot(db: Queryable, chain: Chain, limit: number, leaseSec: number): Promise<Token[]> {
  return claim(db, 'next_snapshot_at', chain, limit, leaseSec);
}

export function claimDueForEnrichment(db: Queryable, chain: Chain, limit: number, leaseSec: number): Promise<Token[]> {
  return claim(db, 'next_enrich_at', chain, limit, leaseSec);
}

export async function markSnapshotFound(
  db: Queryable,
  chain: Chain,
  address: string,
  nextInSec: number,
  meta: { symbol: string | null; name: string | null },
): Promise<void> {
  await db.query(
    `UPDATE tokens SET
       next_snapshot_at = now() + make_interval(secs => $3::double precision),
       last_snapshot_at = now(),
       snapshot_misses = 0,
       symbol = COALESCE(symbol, $4),
       name = COALESCE(name, $5)
     WHERE chain = $1 AND address = $2`,
    [chain, address, nextInSec, meta.symbol, meta.name],
  );
}

export async function markSnapshotMissing(
  db: Queryable,
  chain: Chain,
  address: string,
  misses: number,
  nextInSec: number,
): Promise<void> {
  await db.query(
    `UPDATE tokens SET snapshot_misses = $3,
       next_snapshot_at = now() + make_interval(secs => $4::double precision)
     WHERE chain = $1 AND address = $2`,
    [chain, address, misses, nextInSec],
  );
}

/** Push the next attempt out (provider failure, deferral). */
export async function rescheduleSnapshot(db: Queryable, chain: Chain, addresses: string[], inSec: number): Promise<void> {
  if (!addresses.length) return;
  await db.query(
    `UPDATE tokens SET next_snapshot_at = now() + make_interval(secs => $3::double precision)
     WHERE chain = $1 AND address = ANY($2::text[])`,
    [chain, addresses, inSec],
  );
}

export async function rescheduleEnrichment(db: Queryable, chain: Chain, address: string, inSec: number): Promise<void> {
  await db.query(
    `UPDATE tokens SET next_enrich_at = now() + make_interval(secs => $3::double precision)
     WHERE chain = $1 AND address = $2`,
    [chain, address, inSec],
  );
}

export async function updateTokenInfo(
  db: Queryable,
  chain: Chain,
  address: string,
  info: { decimals: number | null; supply: string | null; tokenProgram: string | null },
): Promise<void> {
  await db.query(
    `UPDATE tokens SET
       decimals = COALESCE($3, decimals),
       supply = COALESCE($4::numeric, supply),
       token_program = COALESCE($5, token_program)
     WHERE chain = $1 AND address = $2`,
    [chain, address, info.decimals, info.supply, info.tokenProgram],
  );
}

/** Re-tier by age. Tiering tolerates unknown on-chain time by using detection time. */
export async function updateTiers(db: Queryable, cfg: Pick<TierSettings, 'hotMinutes' | 'warmMinutes'>): Promise<number> {
  const { rowCount } = await db.query(
    `WITH computed AS (
       SELECT chain, address,
         CASE
           WHEN COALESCE(created_at_chain, detected_at) > now() - make_interval(secs => $1::double precision) THEN 'HOT'
           WHEN COALESCE(created_at_chain, detected_at) > now() - make_interval(secs => $2::double precision) THEN 'WARM'
           ELSE 'COOL'
         END AS tier
       FROM tokens WHERE tier <> 'ARCHIVED'
     )
     UPDATE tokens t SET tier = c.tier
     FROM computed c
     WHERE t.chain = c.chain AND t.address = c.address AND t.tier <> c.tier`,
    [cfg.hotMinutes * 60, cfg.warmMinutes * 60],
  );
  return rowCount ?? 0;
}

export interface ArchiveCounts {
  age: number;
  neverIndexed: number;
  lowLiquidity: number;
}

/** docs/ARCHITECTURE.md §F.3: stop spending budget on tokens that are old or dead. */
export async function archiveTokens(db: Queryable, cfg: TierSettings): Promise<ArchiveCounts> {
  const age = await db.query(
    `UPDATE tokens SET tier = 'ARCHIVED', archived_at = now(), archived_reason = $1
     WHERE tier <> 'ARCHIVED'
       AND COALESCE(created_at_chain, detected_at) < now() - make_interval(secs => $2::double precision)`,
    [`older than ${cfg.archiveAfterHours}h`, cfg.archiveAfterHours * 3600],
  );
  const neverIndexed = await db.query(
    `UPDATE tokens SET tier = 'ARCHIVED', archived_at = now(), archived_reason = $1
     WHERE tier <> 'ARCHIVED' AND last_snapshot_at IS NULL
       AND detected_at < now() - make_interval(secs => $2::double precision)`,
    [`no market data within ${cfg.unindexedArchiveMinutes} min`, cfg.unindexedArchiveMinutes * 60],
  );
  // Only on evidence: at least two snapshots in the window, none of them liquid.
  const lowLiquidity = await db.query(
    `UPDATE tokens t SET tier = 'ARCHIVED', archived_at = now(), archived_reason = $1
     WHERE t.tier <> 'ARCHIVED' AND t.last_snapshot_at IS NOT NULL
       AND COALESCE(t.created_at_chain, t.detected_at) < now() - make_interval(secs => $2::double precision)
       AND (SELECT count(*) FROM market_snapshots s
            WHERE s.chain = t.chain AND s.token_address = t.address
              AND s.observed_at > now() - make_interval(secs => $2::double precision)) >= 2
       AND NOT EXISTS (SELECT 1 FROM market_snapshots s
            WHERE s.chain = t.chain AND s.token_address = t.address
              AND s.observed_at > now() - make_interval(secs => $2::double precision)
              AND s.liquidity_usd >= $3)`,
    [
      `liquidity below $${cfg.lowLiquidityUsd} for ${cfg.lowLiquidityArchiveMinutes} min`,
      cfg.lowLiquidityArchiveMinutes * 60,
      cfg.lowLiquidityUsd,
    ],
  );
  return { age: age.rowCount ?? 0, neverIndexed: neverIndexed.rowCount ?? 0, lowLiquidity: lowLiquidity.rowCount ?? 0 };
}

export async function countByTier(db: Queryable): Promise<Record<TokenTier, number>> {
  const { rows } = await db.query<{ tier: TokenTier; n: string }>(
    'SELECT tier, count(*) AS n FROM tokens GROUP BY tier',
  );
  const out: Record<TokenTier, number> = { HOT: 0, WARM: 0, COOL: 0, ARCHIVED: 0 };
  for (const r of rows) out[r.tier] = Number(r.n);
  return out;
}
