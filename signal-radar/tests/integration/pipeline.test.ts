/**
 * End-to-end pipeline against a real PostgreSQL with TEST-ONLY mock
 * providers: discovery -> market data -> enrichment -> signals -> alert ->
 * outbox -> notifier. All data is synthetic.
 */
import { afterAll, beforeEach, describe, expect, it } from 'vitest';
import { createApp } from '../../src/app.js';
import { loadConfig } from '../../src/config/env.js';
import { PermanentError, RateLimitedError, TransientError } from '../../src/core/errors.js';
import type { Pool } from '../../src/infra/db.js';
import { silentLogger } from '../../src/infra/logger.js';
import { Metrics } from '../../src/infra/metrics.js';
import type { MomentumThresholds } from '../../src/momentum/thresholds.js';
import { getAlert } from '../../src/repositories/alerts.js';
import { insertSnapshot } from '../../src/repositories/snapshots.js';
import { getToken } from '../../src/repositories/tokens.js';
import { DiscoveryService } from '../../src/services/discoveryService.js';
import { EnrichmentService } from '../../src/services/enrichmentService.js';
import { MarketDataService } from '../../src/services/marketDataService.js';
import { NotificationService } from '../../src/services/notificationService.js';
import { SignalService } from '../../src/services/signalService.js';
import { TEST_DATABASE_URL, freshDatabase, hasDatabase } from '../support/db.js';
import { baseEnv } from '../support/env.js';
import { makeDiscovered, makeSnapshot } from '../support/factories.js';
import {
  MockDiscoveryProvider,
  MockHolderProvider,
  MockMarketDataProvider,
  MockNotificationProvider,
  MockSafetyProvider,
} from '../support/mocks.js';

const config = loadConfig(baseEnv());
const logger = silentLogger();

function wire(
  pool: Pool,
  alertOverrides: Partial<typeof config.alerts> = {},
  momentumOverrides: Partial<MomentumThresholds> = {},
) {
  const metrics = new Metrics();
  const discovery = new MockDiscoveryProvider();
  const market = new MockMarketDataProvider();
  const onchain = new MockSafetyProvider('mock-onchain', 'onchain');
  const external = new MockSafetyProvider('mock-external', 'external');
  const holders = new MockHolderProvider();
  const notifier = new MockNotificationProvider();
  const signals = new SignalService({
    db: pool,
    alerts: { ...config.alerts, ...alertOverrides },
    momentum: { defaults: { ...config.momentum.defaults, ...momentumOverrides }, overrides: {} },
    minExternalSafety: 1,
    logger,
    metrics,
  });
  const services = {
    discovery: new DiscoveryService({ db: pool, providers: [discovery], logger, metrics }),
    market: new MarketDataService({
      db: pool,
      chain: 'solana',
      provider: market,
      tiers: config.tiers,
      logger,
      metrics,
      onSnapshot: async (t, s) => void (await signals.evaluate(t, s)),
    }),
    enrichment: new EnrichmentService({
      db: pool,
      chain: 'solana',
      safetyProviders: [onchain, external],
      holderProvider: holders,
      tiers: config.tiers,
      concurrency: 2,
      logger,
      metrics,
      onEnriched: async (t) => void (await signals.evaluateLatest(t)),
    }),
    notifications: new NotificationService({ db: pool, provider: notifier, logger, metrics }),
    signals,
  };
  return { metrics, discovery, market, onchain, external, holders, notifier, services };
}

const signal = new AbortController().signal;
const makeDue = (pool: Pool) =>
  pool.query(`UPDATE tokens SET next_snapshot_at = now() - interval '1 second', next_enrich_at = now() - interval '1 second'`);

describe.skipIf(!hasDatabase)('pipeline (real database, mock providers)', () => {
  let pool: Pool;
  beforeEach(async () => {
    await pool?.end();
    pool = await freshDatabase();
  });
  afterAll(async () => {
    await pool?.end();
  });

  it('turns a qualifying launch into exactly one NEW_TOKEN alert and delivers it', async () => {
    const w = wire(pool);
    await w.services.discovery.start();
    const token = makeDiscovered({ createdAtChain: new Date(Date.now() - 3 * 60_000) });
    w.market.markets.set(token.address, () => ({ liquidityUsd: 35_000, symbol: 'NEW', name: 'New Token' }));
    w.holders.counts.set(token.address, 120);

    await w.discovery.emit(token);
    await w.discovery.emit(token); // second pool for the same token: ignored
    expect((await getToken(pool, 'solana', token.address))?.tier).toBe('HOT');

    // 1st snapshot: no safety/holder data yet -> no alert
    await w.services.market.runOnce(signal);
    expect((await pool.query('SELECT count(*)::int AS n FROM alerts')).rows[0].n).toBe(0);

    // enrichment completes and re-evaluates immediately -> NEW_TOKEN
    await w.services.enrichment.runOnce(signal);
    const alerts = await pool.query('SELECT id, type, status, payload FROM alerts');
    expect(alerts.rows).toHaveLength(1);
    expect(alerts.rows[0]).toMatchObject({ type: 'NEW_TOKEN', status: 'PENDING' });
    expect(alerts.rows[0].payload.gates.every((g: { passed: boolean }) => g.passed)).toBe(true);
    expect(alerts.rows[0].payload).toMatchObject({ symbol: 'NEW', holders: { count: 120 }, safety: { verdict: 'PASS' } });

    // later snapshots do not repeat it
    await makeDue(pool);
    await w.services.market.runOnce(signal);
    expect((await pool.query('SELECT count(*)::int AS n FROM alerts')).rows[0].n).toBe(1);

    // outbox delivery
    expect(await w.services.notifications.runOnce(signal)).toBe(false);
    expect(w.notifier.sent).toHaveLength(1);
    const stored = await getAlert(pool, Number(alerts.rows[0].id));
    expect(stored).toMatchObject({ status: 'SENT', providerMessageId: 'mock-1' });

    // token metadata learned along the way
    expect(await getToken(pool, 'solana', token.address)).toMatchObject({ symbol: 'NEW', decimals: 6, tokenProgram: 'MockProgram' });
  });

  it('does not alert when a safety provider fails the token', async () => {
    const w = wire(pool);
    await w.services.discovery.start();
    const token = makeDiscovered({ createdAtChain: new Date(Date.now() - 60_000) });
    w.market.markets.set(token.address, () => ({ liquidityUsd: 90_000 }));
    w.holders.counts.set(token.address, 500);
    w.external.verdicts.set(token.address, 'FAIL');
    await w.discovery.emit(token);
    await w.services.market.runOnce(signal);
    await w.services.enrichment.runOnce(signal);
    expect((await pool.query('SELECT count(*)::int AS n FROM alerts')).rows[0].n).toBe(0);
    const reports = await pool.query(`SELECT provider, verdict FROM safety_reports ORDER BY provider`);
    expect(reports.rows).toEqual([
      { provider: 'mock-external', verdict: 'FAIL' },
      { provider: 'mock-onchain', verdict: 'PASS' },
    ]);
  });

  it('skips enrichment for illiquid tokens and waits for market data first', async () => {
    const w = wire(pool);
    await w.services.discovery.start();
    const unindexed = makeDiscovered();
    const illiquid = makeDiscovered();
    w.market.markets.set(illiquid.address, () => ({ liquidityUsd: 200 }));
    await w.discovery.emit(unindexed);
    await w.discovery.emit(illiquid);
    await w.services.market.runOnce(signal);
    await w.services.enrichment.runOnce(signal);
    expect(w.onchain.calls + w.external.calls).toBe(0);
    expect((await getToken(pool, 'solana', unindexed.address))!.snapshotMisses).toBe(1);
  });

  it('reschedules the whole batch when the market data provider is down', async () => {
    const w = wire(pool);
    await w.services.discovery.start();
    const t = makeDiscovered();
    w.market.markets.set(t.address, () => ({}));
    await w.discovery.emit(t);
    w.market.failNext = 1;
    await w.services.market.runOnce(signal);
    const token = await getToken(pool, 'solana', t.address);
    expect(token!.lastSnapshotAt).toBeNull();
    expect(token!.nextSnapshotAt.getTime()).toBeGreaterThan(Date.now() + 5_000);
  });

  it('raises a MOMENTUM alert from the engine, stores its evidence and honours the cooldown', async () => {
    // No trade stream: unique buyers cannot be measured, so the score is lower;
    // this test accepts 50 instead of 60 to exercise the full path.
    const w = wire(pool, {}, { minScore: 50 });
    await w.services.discovery.start();
    const t = makeDiscovered({ createdAtChain: new Date(Date.now() - 40 * 60_000) }); // too old for NEW_TOKEN
    await w.discovery.emit(t);
    const ago = (m: number) => new Date(Date.now() - m * 60_000);
    // Provider snapshots with cumulative 24h totals (SYNTHETIC)
    for (const [m, vol, buys, sells, extra] of [
      [15, 0, 0, 0, {}],
      [10, 4_400, 25, 5, {}],
      [5, 14_400, 60, 15, { liquidityUsd: 50_000, marketCapUsd: 200_000 }],
    ] as const) {
      await insertSnapshot(
        pool,
        makeSnapshot({ tokenAddress: t.address, observedAt: ago(m), volumeUsd: { h24: vol }, txns: { h24: { buys, sells } }, ...extra }),
      );
    }
    await pool.query(
      `INSERT INTO holder_snapshots (chain, token_address, observed_at, holder_count, top10_pct, method)
       VALUES ('solana', $1, $2, 100, 20, 'test')`,
      [t.address, ago(5)],
    );
    w.holders.counts.set(t.address, 137);
    w.market.markets.set(t.address, () => ({
      liquidityUsd: 62_000,
      marketCapUsd: 230_000,
      volumeUsd: { h24: 67_200 },
      txns: { h24: { buys: 180, sells: 60 } },
    }));
    await w.services.enrichment.enrich((await getToken(pool, 'solana', t.address))!, signal);
    await w.services.market.runOnce(signal);

    const { rows } = await pool.query(
      `SELECT a.type, a.status, a.score, a.payload, m.signal_type, m.triggered_rules, m.payload AS engine
       FROM alerts a JOIN momentum_signals m ON m.id = a.momentum_signal_id`,
    );
    expect(rows).toHaveLength(1);
    expect(rows[0]).toMatchObject({ type: 'MOMENTUM', status: 'PENDING', signal_type: 'MOMENTUM' });
    expect(rows[0].triggered_rules).toEqual(expect.arrayContaining(['volume_spike', 'tx_spike', 'holder_growth', 'liquidity_growth']));
    expect(Number(rows[0].score)).toBeGreaterThanOrEqual(50);
    const reasons: string[] = rows[0].payload.score.reasons;
    expect(reasons.join('\n')).toMatch(/volume \+42\d%/);
    expect(reasons.join('\n')).toMatch(/holder growth \+37%/);
    expect(rows[0].payload.score.warnings.join()).toMatch(/geen trade-data/);
    expect(rows[0].engine.metrics['5m'].flowSource).toBe('provider_h24_delta');
    expect(rows[0].engine.disclaimer).toMatch(/Geen voorspelling/);

    // same activity a moment later: inside the cooldown, no escalation -> no second alert
    await makeDue(pool);
    await w.services.market.runOnce(signal);
    expect((await pool.query(`SELECT count(*)::int AS n FROM alerts WHERE type = 'MOMENTUM'`)).rows[0].n).toBe(1);
  });

  it('retries transient delivery failures, respects Retry-After, and gives up on permanent ones', async () => {
    const w = wire(pool);
    await w.services.discovery.start();
    const t = makeDiscovered({ createdAtChain: new Date(Date.now() - 60_000) });
    w.market.markets.set(t.address, () => ({ liquidityUsd: 35_000 }));
    w.holders.counts.set(t.address, 80);
    await w.discovery.emit(t);
    await w.services.market.runOnce(signal);
    await w.services.enrichment.runOnce(signal);
    const id = Number((await pool.query('SELECT id FROM alerts')).rows[0].id);

    w.notifier.failures.push(new TransientError('discord 503'));
    await w.services.notifications.runOnce(signal);
    let a = await getAlert(pool, id);
    expect(a).toMatchObject({ status: 'PENDING', attempts: 1, lastError: 'discord 503' });

    await pool.query(`UPDATE alerts SET next_attempt_at = now()`);
    w.notifier.failures.push(new RateLimitedError('slow down', 7_000));
    await w.services.notifications.runOnce(signal);
    const next = await pool.query('SELECT next_attempt_at FROM alerts WHERE id = $1', [id]);
    expect(new Date(next.rows[0].next_attempt_at).getTime()).toBeGreaterThan(Date.now() + 5_000);

    await pool.query(`UPDATE alerts SET next_attempt_at = now()`);
    w.notifier.failures.push(new PermanentError('webhook deleted', 404));
    await w.services.notifications.runOnce(signal);
    a = await getAlert(pool, id);
    expect(a).toMatchObject({ status: 'FAILED', attempts: 3 });
    expect(w.notifier.sent).toHaveLength(0);
  });

  it('suppresses (and records) alerts beyond the hourly limit', async () => {
    const w = wire(pool, { maxPerHour: 1 });
    await w.services.discovery.start();
    const tokens = [0, 1].map(() => makeDiscovered({ createdAtChain: new Date(Date.now() - 60_000) }));
    for (const t of tokens) {
      w.market.markets.set(t.address, () => ({ liquidityUsd: 35_000 }));
      w.holders.counts.set(t.address, 80);
      await w.discovery.emit(t);
    }
    await w.services.market.runOnce(signal);
    for (const t of tokens) await w.services.enrichment.enrich((await getToken(pool, 'solana', t.address))!, signal);
    const { rows } = await pool.query(`SELECT status, suppressed_reason FROM alerts ORDER BY id`);
    expect(rows.map((r) => r.status)).toEqual(['PENDING', 'SUPPRESSED']);
    expect(rows[1].suppressed_reason).toMatch(/1 alerts per hour/);
    await w.services.notifications.runOnce(signal);
    expect(w.notifier.sent).toHaveLength(1);
  });
});

describe.skipIf(!hasDatabase)('application lifecycle', () => {
  it('starts with mock providers, serves health endpoints and shuts down cleanly', async () => {
    const pool = await freshDatabase();
    await pool.end();
    const cfg = loadConfig(baseEnv({ DATABASE_URL: TEST_DATABASE_URL, HEALTH_PORT: '0', HEALTH_HOST: '127.0.0.1' }));
    const discovery = new MockDiscoveryProvider();
    const market = new MockMarketDataProvider();
    const notifier = new MockNotificationProvider();
    const app = await createApp(cfg, {
      logger,
      discovery: [discovery],
      marketData: market,
      safety: [new MockSafetyProvider('mock-onchain', 'onchain'), new MockSafetyProvider('mock-external', 'external')],
      holders: new MockHolderProvider(),
      notifier,
      intervalsMs: { marketData: 20, enrichment: 20, notifications: 20 },
    });
    await app.start();
    try {
      const base = `http://127.0.0.1:${app.healthPort}`;
      expect((await fetch(`${base}/health`)).status).toBe(200);
      const ready = await fetch(`${base}/ready`);
      expect(ready.status).toBe(200);
      expect(await ready.json()).toMatchObject({ ready: true, checks: { database: true, notifier: 'mock-notifier' } });
      expect(await (await fetch(`${base}/metrics`)).text()).toContain('radar_provider_requests_total');

      // A second instance on the same database must refuse to start.
      await expect(createApp(cfg, { logger, discovery: [new MockDiscoveryProvider()] })).rejects.toThrow(/already running/);

      // The scheduler actually runs: a discovered token gets polled.
      const t = makeDiscovered();
      await discovery.emit(t);
      await new Promise((r) => setTimeout(r, 200));
      expect(market.calls).toBeGreaterThan(0);
    } finally {
      await app.stop();
      await app.stop(); // idempotent
    }
    expect(discovery.stopped).toBe(true);
    await expect(fetch(`http://127.0.0.1:${app.healthPort}/health`)).rejects.toThrow();
  });
});
