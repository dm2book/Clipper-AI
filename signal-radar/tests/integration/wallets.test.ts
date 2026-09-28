/**
 * Wallet Intelligence against a real PostgreSQL. All events are SYNTHETIC
 * (test-only), not real transactions.
 */
import { afterAll, beforeAll, beforeEach, describe, expect, it } from 'vitest';
import { loadConfig } from '../../src/config/env.js';
import type { WalletAlertPayload } from '../../src/core/alerts.js';
import type { Pool } from '../../src/infra/db.js';
import { silentLogger } from '../../src/infra/logger.js';
import { Metrics } from '../../src/infra/metrics.js';
import { renderAlert } from '../../src/providers/discord/formatter.js';
import { isOnCurve } from '../../src/providers/solana/pubkey.js';
import { getAlert } from '../../src/repositories/alerts.js';
import { insertSnapshot } from '../../src/repositories/snapshots.js';
import { insertDiscoveredToken, markSnapshotFound, updateTokenInfo } from '../../src/repositories/tokens.js';
import { getTrackedWallet, getWalletProfile, listTrackedWallets } from '../../src/repositories/wallets.js';
import { WalletIntelligenceService } from '../../src/services/walletIntelligenceService.js';
import type { WalletEvent } from '../../src/wallets/model.js';
import { freshDatabase, hasDatabase } from '../support/db.js';
import { baseEnv } from '../support/env.js';
import { makeDiscovered, makeSnapshot, randomAddress } from '../support/factories.js';
import { MockWalletActivitySource, MockWalletProvider } from '../support/mocks.js';

const MANUAL = randomAddress();
const config = loadConfig(baseEnv({ TRACKED_WALLETS: MANUAL }));

let seq = 0;
function swap(wallet: string, token: string, kind: WalletEvent['kind'], valueUsd: number | null, at = new Date(), extra: Partial<WalletEvent> = {}): WalletEvent {
  return {
    chain: 'solana',
    signature: `itest-sig-${++seq}`,
    ixIndex: 0,
    wallet,
    tokenAddress: token,
    kind,
    status: 'success',
    amountRaw: 1_000_000_000n, // 1,000 tokens at 6 decimals
    valueUsd,
    blockTime: at,
    slot: null,
    source: 'test',
    ...extra,
  };
}

describe.skipIf(!hasDatabase)('Wallet Intelligence service', () => {
  let pool: Pool;
  let token: string;
  const metrics = new Metrics();

  const service = (overrides: Partial<ConstructorParameters<typeof WalletIntelligenceService>[0]> = {}) =>
    new WalletIntelligenceService({
      db: pool,
      chain: 'solana',
      config: config.wallets,
      alerts: config.alerts,
      isOnCurve,
      logger: silentLogger(),
      metrics,
      ...overrides,
    });

  const payloadOf = async (id: number) => (await getAlert(pool, id))!.payload as WalletAlertPayload;

  beforeAll(async () => {
    pool = await freshDatabase();
  });
  afterAll(async () => {
    await pool?.end();
  });
  beforeEach(async () => {
    await pool.query('TRUNCATE alerts, wallet_events, wallets, tracked_wallets, market_snapshots, tokens CASCADE');
    const t = makeDiscovered();
    token = t.address;
    await insertDiscoveredToken(pool, t);
    await updateTokenInfo(pool, 'solana', token, { decimals: 6, supply: null, tokenProgram: null });
    await markSnapshotFound(pool, 'solana', token, 60, { symbol: 'TEST', name: 'Test Token' });
    await insertSnapshot(
      pool,
      makeSnapshot({ tokenAddress: token, observedAt: new Date(), priceUsd: 0.02, liquidityUsd: 400_000, symbol: 'TEST' }),
    );
  });

  it('WHALE: alerts a large buy, with the size-only disclaimer', async () => {
    const r = await service().ingest([swap(randomAddress(), token, 'buy', 48_000)]);
    expect(r.alertIds).toHaveLength(1);
    const p = await payloadOf(r.alertIds[0]!);
    expect(p.type).toBe('WHALE');
    expect(p.tracked).toBeNull();
    expect(p.notes.join()).toMatch(/niet over de kwaliteit/);
    expect(p.exposure).toMatchObject({ qtyRaw: '1000000000', costUsd: 48_000, valueUsd: 20, complete: true });
    expect(renderAlert(p, 'x').embeds[0].title).toBe('🐋 Wallet bought $48,000 of TEST');
  });

  it('WHALE: alerts a large sell', async () => {
    const w = randomAddress();
    const s = service();
    await s.ingest([swap(w, token, 'buy', 1_000, new Date(Date.now() - 60_000))]);
    const r = await s.ingest([swap(w, token, 'sell', 30_000)]);
    const p = await payloadOf(r.alertIds[0]!);
    expect(renderAlert(p, 'x').embeds[0].title).toBe('🐋 Wallet sold $30,000 of TEST');
    expect(p.exposure).toMatchObject({ qtyRaw: '0' });
  });

  it('does not alert twice for a re-delivered transaction', async () => {
    const e = swap(randomAddress(), token, 'buy', 60_000);
    const s = service();
    const first = await s.ingest([e]);
    const again = await s.ingest([{ ...e }]);
    expect(first.alertIds).toHaveLength(1);
    expect(again).toMatchObject({ inserted: 0, duplicates: 1, alertIds: [] });
    const { rows } = await pool.query('SELECT count(*)::int AS n FROM wallet_events');
    expect(rows[0].n).toBe(1);
  });

  it('stores failed transactions but never alerts on them', async () => {
    const r = await service().ingest([swap(randomAddress(), token, 'buy', 1_000_000, new Date(), { status: 'failed' })]);
    expect(r).toMatchObject({ inserted: 1, alertIds: [] });
  });

  it('drops structurally invalid events', async () => {
    const r = await service().ingest([swap(randomAddress(), token, 'buy', -5)]);
    expect(r).toMatchObject({ invalid: 1, inserted: 0 });
  });

  it('never alerts on backfilled (old) events or on tokens the radar does not track', async () => {
    const s = service();
    const old = await s.ingest([swap(randomAddress(), token, 'buy', 100_000, new Date(Date.now() - 3_600_000))]);
    const unknown = await s.ingest([swap(randomAddress(), randomAddress(), 'buy', 100_000)]);
    expect(old.alertIds).toEqual([]);
    expect(unknown).toMatchObject({ inserted: 1, alertIds: [] });
  });

  it('limits whale alerts per wallet per hour (the rest are suppressed, not lost)', async () => {
    const w = randomAddress();
    const s = service();
    const ids: number[] = [];
    for (let i = 0; i < 4; i++) ids.push(...(await s.ingest([swap(w, token, 'buy', 30_000)])).alertIds);
    const statuses = await Promise.all(ids.map(async (id) => (await getAlert(pool, id))!.status));
    expect(statuses).toEqual(['PENDING', 'PENDING', 'PENDING', 'SUPPRESSED']);
  });

  it('TRACKED_WALLET: a watchlist wallet takes precedence over whale, with its reason', async () => {
    const s = service();
    await s.syncWatchlist();
    const r = await s.ingest([swap(MANUAL, token, 'buy', 12_400)]);
    const p = await payloadOf(r.alertIds[0]!);
    expect(p.type).toBe('TRACKED_WALLET');
    expect(p.tracked).toMatchObject({ source: 'manual' });
    expect(renderAlert(p, 'x').embeds[0].title).toBe('👁️ Tracked wallet bought $12,400 of TEST');

    const big = await s.ingest([swap(MANUAL, token, 'buy', 80_000)]);
    const bp = await payloadOf(big.alertIds[0]!);
    expect(bp.type).toBe('TRACKED_WALLET'); // one alert, not a whale alert as well
    expect(bp.whaleReason).toMatch(/\$80,000/);
  });

  it('TRACKED_CLUSTER: 4 tracked wallets within 3 minutes; one wallet cannot dominate; cooldown applies', async () => {
    const wallets = [randomAddress(), randomAddress(), randomAddress(), randomAddress(), randomAddress()];
    const cfg = { ...config.wallets, manualWallets: wallets, activity: { ...config.wallets.activity, clusterMinWallets: 4 } };
    const s = service({ config: cfg });
    await s.syncWatchlist();
    const t0 = Date.now() - 170_000;
    const values = [500_000, 2_000, 1_500, 1_000, 3_000];
    const ids: number[] = [];
    for (const [i, w] of wallets.entries()) {
      ids.push(...(await s.ingest([swap(w, token, 'buy', values[i]!, new Date(t0 + i * 40_000))])).alertIds);
    }
    const payloads = await Promise.all(ids.map(payloadOf));
    expect(payloads.filter((p) => p.type === 'TRACKED_WALLET')).toHaveLength(5);
    const clusters = payloads.filter((p) => p.type === 'TRACKED_CLUSTER');
    expect(clusters).toHaveLength(1); // the 5th wallet falls inside the cooldown
    const c = clusters[0]!.cluster!;
    expect(c.wallets).toHaveLength(4);
    expect(Math.max(...c.wallets.map((w) => w.sharePct))).toBeLessThanOrEqual(40 + 1e-9);
    expect(c.totalUsd).toBe(504_500);
    expect(renderAlert(clusters[0]!, 'x').embeds[0].title).toBe('👥 4 tracked wallets bought TEST within 3 minutes');
  });

  it('no cluster from untracked wallets, however many buy', async () => {
    const s = service();
    const ids: number[] = [];
    for (let i = 0; i < 5; i++) ids.push(...(await s.ingest([swap(randomAddress(), token, 'buy', 1_000)])).alertIds);
    expect(ids).toEqual([]);
  });
});

describe.skipIf(!hasDatabase)('Wallet statistics and classification', () => {
  let pool: Pool;
  const metrics = new Metrics();
  const DAY = 86_400_000;

  const service = (source?: MockWalletActivitySource, history?: MockWalletProvider) =>
    new WalletIntelligenceService({
      db: pool,
      chain: 'solana',
      config: config.wallets,
      alerts: config.alerts,
      isOnCurve,
      logger: silentLogger(),
      metrics,
      ...(source ? { source } : {}),
      ...(history ? { history } : {}),
    });

  /** n round trips over the last 30 days; the first `wins` gain 50%, the rest lose 20%. */
  function history(wallet: string, n: number, wins: number): WalletEvent[] {
    const out: WalletEvent[] = [];
    for (let i = 0; i < n; i++) {
      const tok = randomAddress();
      const start = Date.now() - 30 * DAY + i * DAY;
      out.push(swap(wallet, tok, 'buy', 1_000, new Date(start)));
      out.push(swap(wallet, tok, 'sell', i < wins ? 1_500 : 800, new Date(start + 2 * 3_600_000)));
    }
    return out;
  }

  beforeAll(async () => {
    pool = await freshDatabase();
  });
  afterAll(async () => {
    await pool?.end();
  });
  beforeEach(async () => {
    await pool.query('TRUNCATE alerts, wallet_events, wallets, tracked_wallets CASCADE');
  });

  it('computes and stores statistics, and tracks a wallet that meets every criterion', async () => {
    const w = randomAddress();
    const source = new MockWalletActivitySource();
    const s = service(source);
    await s.start();
    await source.emit(history(w, 25, 20));
    expect(await s.statsOnce(new AbortController().signal)).toBe(false);

    const profile = (await getWalletProfile(pool, 'solana', w))!;
    expect(profile.classification).toBe('QUALIFIED');
    expect(profile.stats).toMatchObject({ trades: 50, reliableClosedPositions: 25, wins: 20, winRate: 0.8, realizedPnlUsd: 9_000 });
    expect(profile.stats.avgReturnPct).toBeCloseTo(36);
    expect(profile.stats.avgHoldingSec).toBe(2 * 3_600);

    const tracked = (await getTrackedWallet(pool, 'solana', w))!;
    expect(tracked.source).toBe('criteria');
    expect(tracked.reason).toMatch(/voldoet aan volgcriteria wallet-criteria-v1/);
    expect(source.watchlist).toContain(w);
  });

  it('removes a criteria wallet when it no longer qualifies, but never a manual one', async () => {
    const w = randomAddress();
    const s = service();
    await s.syncWatchlist();
    await s.ingest(history(w, 25, 20));
    expect((await s.recomputeStats('solana', w)).tracking).toBe('added');
    await s.ingest(history(w, 30, 0)); // 30 more losing positions
    const r = await s.recomputeStats('solana', w);
    expect(r.profile.classification).toBe('NOT_QUALIFIED');
    expect(r.tracking).toBe('removed');
    expect(await getTrackedWallet(pool, 'solana', w)).toBeNull();

    await s.ingest(history(MANUAL, 25, 0));
    expect((await s.recomputeStats('solana', MANUAL)).profile.classification).toBe('NOT_QUALIFIED');
    expect((await getTrackedWallet(pool, 'solana', MANUAL))!.source).toBe('manual');
  });

  it('a whale with few trades is not tracked (insufficient history)', async () => {
    const w = randomAddress();
    const s = service();
    await s.ingest(history(w, 3, 3).map((e) => ({ ...e, valueUsd: e.valueUsd! * 100 })));
    const r = await s.recomputeStats('solana', w);
    expect(r.profile.classification).toBe('INSUFFICIENT_HISTORY');
    expect(r.tracking).toBeNull();
    expect(await listTrackedWallets(pool, 'solana')).toEqual([]);
  });

  it('backfills manual wallets from the history provider without alerting', async () => {
    const history_ = new MockWalletProvider();
    history_.events.push(...history(MANUAL, 2, 1));
    const s = service(undefined, history_);
    await s.syncWatchlist();
    const { rows } = await pool.query('SELECT count(*)::int AS n FROM wallet_events WHERE wallet = $1', [MANUAL]);
    expect(rows[0].n).toBe(4);
    const alerts = await pool.query('SELECT count(*)::int AS n FROM alerts');
    expect(alerts.rows[0].n).toBe(0);
  });

  it('reports open-position exposure', async () => {
    const w = randomAddress();
    const s = service();
    await s.ingest([swap(w, randomAddress(), 'buy', 500)]);
    const [pos] = await s.exposure('solana', w);
    expect(pos).toMatchObject({ qtyRaw: 1_000_000_000n, remainingCostUsd: 500, valueUsd: null }); // token unknown: no price
  });
});
