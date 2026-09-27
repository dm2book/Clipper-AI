import type { MarketSnapshot } from '../core/marketSnapshot.js';
import type { Chain } from '../core/types.js';
import { date, int, num, type Queryable } from '../infra/db.js';

interface SnapshotRow {
  chain: Chain;
  token_address: string;
  pair_address: string | null;
  dex_id: string | null;
  quote_address: string | null;
  source: string;
  observed_at: Date;
  price_usd: string | null;
  liquidity_usd: string | null;
  fdv_usd: string | null;
  market_cap_usd: string | null;
  volume_m5: string | null;
  volume_h1: string | null;
  volume_h6: string | null;
  volume_h24: string | null;
  buys_m5: number | null;
  sells_m5: number | null;
  buys_h1: number | null;
  sells_h1: number | null;
  buys_h24: number | null;
  sells_h24: number | null;
  price_change_m5: string | null;
  price_change_h1: string | null;
  price_change_h24: string | null;
  pair_created_at: Date | null;
  symbol: string | null;
  name: string | null;
}

function toSnapshot(r: SnapshotRow): MarketSnapshot {
  return {
    chain: r.chain,
    tokenAddress: r.token_address,
    pairAddress: r.pair_address,
    dexId: r.dex_id,
    quoteAddress: r.quote_address,
    source: r.source,
    observedAt: date(r.observed_at)!,
    priceUsd: num(r.price_usd),
    liquidityUsd: num(r.liquidity_usd),
    fdvUsd: num(r.fdv_usd),
    marketCapUsd: num(r.market_cap_usd),
    volumeUsd: { m5: num(r.volume_m5), h1: num(r.volume_h1), h6: num(r.volume_h6), h24: num(r.volume_h24) },
    txns: {
      m5: { buys: int(r.buys_m5), sells: int(r.sells_m5) },
      h1: { buys: int(r.buys_h1), sells: int(r.sells_h1) },
      h24: { buys: int(r.buys_h24), sells: int(r.sells_h24) },
    },
    priceChangePct: { m5: num(r.price_change_m5), h1: num(r.price_change_h1), h24: num(r.price_change_h24) },
    pairCreatedAt: date(r.pair_created_at),
    symbol: r.symbol,
    name: r.name,
  };
}

export async function insertSnapshot(db: Queryable, s: MarketSnapshot): Promise<number> {
  const { rows } = await db.query<{ id: string }>(
    `INSERT INTO market_snapshots (
       chain, token_address, pair_address, dex_id, quote_address, source, observed_at,
       price_usd, liquidity_usd, fdv_usd, market_cap_usd,
       volume_m5, volume_h1, volume_h6, volume_h24,
       buys_m5, sells_m5, buys_h1, sells_h1, buys_h24, sells_h24,
       price_change_m5, price_change_h1, price_change_h24, pair_created_at)
     VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12,$13,$14,$15,$16,$17,$18,$19,$20,$21,$22,$23,$24,$25)
     RETURNING id`,
    [
      s.chain,
      s.tokenAddress,
      s.pairAddress,
      s.dexId,
      s.quoteAddress,
      s.source,
      s.observedAt,
      s.priceUsd,
      s.liquidityUsd,
      s.fdvUsd,
      s.marketCapUsd,
      s.volumeUsd.m5,
      s.volumeUsd.h1,
      s.volumeUsd.h6,
      s.volumeUsd.h24,
      s.txns.m5.buys,
      s.txns.m5.sells,
      s.txns.h1.buys,
      s.txns.h1.sells,
      s.txns.h24.buys,
      s.txns.h24.sells,
      s.priceChangePct.m5,
      s.priceChangePct.h1,
      s.priceChangePct.h24,
      s.pairCreatedAt,
    ],
  );
  return Number(rows[0]!.id);
}

export async function upsertPool(db: Queryable, s: MarketSnapshot): Promise<void> {
  if (!s.pairAddress) return;
  await db.query(
    `INSERT INTO pools (chain, address, token_address, quote_address, dex)
     VALUES ($1, $2, $3, $4, $5)
     ON CONFLICT (chain, address) DO UPDATE SET last_seen_at = now()`,
    [s.chain, s.pairAddress, s.tokenAddress, s.quoteAddress, s.dexId],
  );
}

/** Newest first. Symbol/name come from the token row (the snapshot table does not repeat them). */
export async function recentSnapshots(
  db: Queryable,
  chain: Chain,
  address: string,
  since: Date,
  limit = 500,
): Promise<MarketSnapshot[]> {
  const { rows } = await db.query<SnapshotRow>(
    `SELECT s.*, t.symbol, t.name
     FROM market_snapshots s JOIN tokens t ON t.chain = s.chain AND t.address = s.token_address
     WHERE s.chain = $1 AND s.token_address = $2 AND s.observed_at >= $3
     ORDER BY s.observed_at DESC
     LIMIT $4`,
    [chain, address, since, limit],
  );
  return rows.map(toSnapshot);
}

export async function latestSnapshot(db: Queryable, chain: Chain, address: string): Promise<MarketSnapshot | null> {
  const { rows } = await db.query<SnapshotRow>(
    `SELECT s.*, t.symbol, t.name
     FROM market_snapshots s JOIN tokens t ON t.chain = s.chain AND t.address = s.token_address
     WHERE s.chain = $1 AND s.token_address = $2
     ORDER BY s.observed_at DESC LIMIT 1`,
    [chain, address],
  );
  return rows[0] ? toSnapshot(rows[0]) : null;
}

export async function deleteSnapshotsBefore(db: Queryable, cutoff: Date): Promise<number> {
  const { rowCount } = await db.query('DELETE FROM market_snapshots WHERE observed_at < $1', [cutoff]);
  return rowCount ?? 0;
}
