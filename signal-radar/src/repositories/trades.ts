import type { Chain } from '../core/types.js';
import { date, num, type Queryable } from '../infra/db.js';
import type { Trade } from '../momentum/flow.js';

/**
 * Per-trade storage for the Momentum Detection Engine. Nothing writes here
 * until a trade stream is connected (roadmap phase 3); the engine then gets
 * exact unique-buyer/seller counts and wash-trading checks.
 */
export interface StoredTrade extends Trade {
  chain: Chain;
  tokenAddress: string;
  ixIndex: number;
  poolAddress: string | null;
  source: string;
}

export async function insertTrades(db: Queryable, trades: readonly StoredTrade[]): Promise<number> {
  let inserted = 0;
  for (const t of trades) {
    const { rowCount } = await db.query(
      `INSERT INTO trades (chain, signature, ix_index, token_address, pool_address, wallet, side, value_usd, block_time, source)
       VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10)
       ON CONFLICT (chain, signature, ix_index) DO NOTHING`,
      [t.chain, t.signature, t.ixIndex, t.tokenAddress, t.poolAddress, t.wallet, t.side, t.valueUsd, t.at, t.source],
    );
    inserted += rowCount ?? 0;
  }
  return inserted;
}

export async function tradesSince(db: Queryable, chain: Chain, address: string, since: Date): Promise<Trade[]> {
  const { rows } = await db.query<{ signature: string; ix_index: number; wallet: string; side: 'buy' | 'sell'; value_usd: string; block_time: Date }>(
    `SELECT signature, ix_index, wallet, side, value_usd, block_time FROM trades
     WHERE chain = $1 AND token_address = $2 AND block_time >= $3 ORDER BY block_time`,
    [chain, address, since],
  );
  return rows.map((r) => ({
    signature: `${r.signature}:${r.ix_index}`,
    wallet: r.wallet,
    side: r.side,
    valueUsd: num(r.value_usd) ?? 0,
    at: date(r.block_time)!,
  }));
}

export async function deleteTradesBefore(db: Queryable, cutoff: Date): Promise<number> {
  const { rowCount } = await db.query('DELETE FROM trades WHERE block_time < $1', [cutoff]);
  return rowCount ?? 0;
}
