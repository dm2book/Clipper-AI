import { afterAll, beforeEach, describe, expect, it } from 'vitest';
import type { TierSettings } from '../../src/config/env.js';
import type { Pool } from '../../src/infra/db.js';
import { insertSnapshot, latestSnapshot, recentSnapshots, upsertPool } from '../../src/repositories/snapshots.js';
import {
  archiveTokens,
  claimDueForSnapshot,
  countByTier,
  getToken,
  insertDiscoveredToken,
  markSnapshotFound,
  markSnapshotMissing,
  updateTiers,
} from '../../src/repositories/tokens.js';
import { deleteTradesBefore, insertTrades, tradesSince } from '../../src/repositories/trades.js';
import { freshDatabase, hasDatabase } from '../support/db.js';
import { makeDiscovered, makeSnapshot } from '../support/factories.js';

const tiers: TierSettings = {
  hotMinutes: 15,
  warmMinutes: 60,
  archiveAfterHours: 6,
  snapshotIntervalSec: { HOT: 15, WARM: 60, COOL: 300 },
  enrichIntervalSec: { HOT: 60, WARM: 300, COOL: 1800 },
  safetyRecheckSec: { HOT: 120, WARM: 1800, COOL: 3600 },
  lowLiquidityUsd: 1_000,
  lowLiquidityArchiveMinutes: 15,
  unindexedArchiveMinutes: 30,
};

describe.skipIf(!hasDatabase)('token and snapshot repositories', () => {
  let pool: Pool;
  beforeEach(async () => {
    await pool?.end();
    pool = await freshDatabase();
  });
  afterAll(async () => {
    await pool?.end();
  });

  it('stores a discovered token once', async () => {
    const t = makeDiscovered();
    expect(await insertDiscoveredToken(pool, t)).toBe(true);
    expect(await insertDiscoveredToken(pool, { ...t, source: 'other-pool' })).toBe(false);
    const stored = await getToken(pool, 'solana', t.address);
    expect(stored).toMatchObject({ tier: 'HOT', detectionSource: 'test', snapshotMisses: 0 });
    expect(stored!.createdAtChain!.getTime()).toBe(t.createdAtChain!.getTime());
  });

  it('never hands the same token to two workers', async () => {
    for (let i = 0; i < 10; i++) await insertDiscoveredToken(pool, makeDiscovered());
    const [a, b, c] = await Promise.all([
      claimDueForSnapshot(pool, 'solana', 4, 60),
      claimDueForSnapshot(pool, 'solana', 4, 60),
      claimDueForSnapshot(pool, 'solana', 4, 60),
    ]);
    const claimed = [...a!, ...b!, ...c!].map((t) => t.address);
    expect(claimed).toHaveLength(10);
    expect(new Set(claimed).size).toBe(10);
    // leased: nothing is due any more
    expect(await claimDueForSnapshot(pool, 'solana', 10, 60)).toEqual([]);
  });

  it('records snapshot outcomes and keeps metadata first-write-wins', async () => {
    const t = makeDiscovered();
    await insertDiscoveredToken(pool, t);
    await markSnapshotMissing(pool, 'solana', t.address, 2, 40);
    expect((await getToken(pool, 'solana', t.address))!.snapshotMisses).toBe(2);
    await markSnapshotFound(pool, 'solana', t.address, 15, { symbol: 'AAA', name: 'First' });
    await markSnapshotFound(pool, 'solana', t.address, 15, { symbol: 'ZZZ', name: 'Renamed' });
    const stored = await getToken(pool, 'solana', t.address);
    expect(stored).toMatchObject({ snapshotMisses: 0, symbol: 'AAA', name: 'First' });
    expect(stored!.lastSnapshotAt).not.toBeNull();
  });

  it('round-trips snapshots, including unknown values', async () => {
    const t = makeDiscovered();
    await insertDiscoveredToken(pool, t);
    const s = makeSnapshot({
      tokenAddress: t.address,
      observedAt: new Date(Date.now() - 1_000),
      liquidityUsd: 12_345.67,
      marketCapUsd: null,
      txns: { m5: { buys: 7, sells: null } },
    });
    await insertSnapshot(pool, s);
    await upsertPool(pool, s);
    await upsertPool(pool, s); // idempotent
    const back = await latestSnapshot(pool, 'solana', t.address);
    expect(back).toMatchObject({
      liquidityUsd: 12_345.67,
      marketCapUsd: null,
      txns: { m5: { buys: 7, sells: null } },
      pairAddress: s.pairAddress,
    });
    expect(await recentSnapshots(pool, 'solana', t.address, new Date(Date.now() - 60_000))).toHaveLength(1);
    const pools = await pool.query('SELECT count(*)::int AS n FROM pools');
    expect(pools.rows[0].n).toBe(1);
  });

  it('re-tiers by age and archives old, unindexed and illiquid tokens', async () => {
    const minutesAgo = (m: number) => new Date(Date.now() - m * 60_000);
    const fresh = makeDiscovered({ createdAtChain: minutesAgo(2) });
    const warm = makeDiscovered({ createdAtChain: minutesAgo(20) });
    const old = makeDiscovered({ createdAtChain: minutesAgo(7 * 60) });
    const unindexed = makeDiscovered({ createdAtChain: minutesAgo(40) });
    const illiquid = makeDiscovered({ createdAtChain: minutesAgo(25) });
    const liquid = makeDiscovered({ createdAtChain: minutesAgo(25) });
    for (const t of [fresh, warm, old, unindexed, illiquid, liquid]) await insertDiscoveredToken(pool, t);
    await pool.query(`UPDATE tokens SET detected_at = created_at_chain`);
    for (const [t, liq] of [
      [illiquid, 300],
      [liquid, 50_000],
    ] as const) {
      await markSnapshotFound(pool, 'solana', t.address, 15, { symbol: null, name: null });
      for (const m of [10, 5]) {
        await insertSnapshot(pool, makeSnapshot({ tokenAddress: t.address, observedAt: minutesAgo(m), liquidityUsd: liq }));
      }
    }

    await updateTiers(pool, tiers);
    expect((await getToken(pool, 'solana', fresh.address))!.tier).toBe('HOT');
    expect((await getToken(pool, 'solana', warm.address))!.tier).toBe('WARM');

    const archived = await archiveTokens(pool, tiers);
    expect(archived).toEqual({ age: 1, neverIndexed: 1, lowLiquidity: 1 });
    expect((await getToken(pool, 'solana', illiquid.address))!.archivedReason).toMatch(/liquidity below/);
    expect((await getToken(pool, 'solana', liquid.address))!.tier).toBe('WARM');
    expect(await countByTier(pool)).toEqual({ HOT: 1, WARM: 2, COOL: 0, ARCHIVED: 3 });
    // archived tokens are never claimed
    await pool.query(`UPDATE tokens SET next_snapshot_at = now() - interval '1 minute'`);
    const claimed = await claimDueForSnapshot(pool, 'solana', 10, 60);
    expect(claimed.map((t) => t.tier).sort()).toEqual(['HOT', 'WARM', 'WARM']);
  });

  it('stores trades idempotently and returns them for the engine', async () => {
    const t = makeDiscovered();
    await insertDiscoveredToken(pool, t);
    const base = { chain: 'solana' as const, tokenAddress: t.address, poolAddress: null, source: 'test' };
    const trades = [
      { ...base, signature: 's1', ixIndex: 0, wallet: 'w1', side: 'buy' as const, valueUsd: 120.5, at: new Date(Date.now() - 60_000) },
      { ...base, signature: 's1', ixIndex: 1, wallet: 'w2', side: 'sell' as const, valueUsd: 80, at: new Date(Date.now() - 30_000) },
    ];
    expect(await insertTrades(pool, trades)).toBe(2);
    expect(await insertTrades(pool, trades)).toBe(0); // same signature + instruction: ignored
    const back = await tradesSince(pool, 'solana', t.address, new Date(Date.now() - 120_000));
    expect(back.map((x) => [x.wallet, x.side, x.valueUsd])).toEqual([
      ['w1', 'buy', 120.5],
      ['w2', 'sell', 80],
    ]);
    expect(await deleteTradesBefore(pool, new Date(Date.now() - 45_000))).toBe(1);
  });
});
