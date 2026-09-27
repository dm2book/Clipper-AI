/**
 * Composition root: builds every component from the configuration and wires
 * them together. Tests pass `overrides` to swap providers for mocks; nothing
 * else in the codebase constructs providers.
 */
import type { AppConfig } from './config/env.js';
import type { SchemaError } from './core/errors.js';
import { MemoryCache, RedisCache, type Cache } from './infra/cache.js';
import {
  acquireInstanceLock,
  createPool,
  migrate,
  ping,
  releaseInstanceLock,
  type Pool,
  type PoolClient,
} from './infra/db.js';
import { HttpClient, type SchemaErrorInfo } from './infra/http.js';
import { createLogger, type Logger } from './infra/logger.js';
import { Metrics } from './infra/metrics.js';
import { TokenBucket } from './infra/rateLimiter.js';
import { startHealthServer, type HealthServer } from './http/healthServer.js';
import { ConsoleNotificationProvider } from './providers/console/notification.js';
import { DexScreenerMarketDataProvider } from './providers/dexscreener/marketData.js';
import { DiscordWebhookProvider } from './providers/discord/webhook.js';
import { GoPlusSafetyProvider } from './providers/goplus/safety.js';
import type {
  HolderProvider,
  MarketDataProvider,
  NotificationProvider,
  SafetyProvider,
  TokenDiscoveryProvider,
  WalletProvider,
} from './providers/interfaces.js';
import { RugCheckSafetyProvider } from './providers/rugcheck/safety.js';
import { QUOTE_MINTS, resolveSources } from './providers/solana/constants.js';
import { SolanaLogsDiscoveryProvider } from './providers/solana/discovery.js';
import { SolanaRpcHolderProvider } from './providers/solana/holders.js';
import { MintInfoLoader } from './providers/solana/mintInfo.js';
import { SolanaOnchainSafetyProvider } from './providers/solana/onchainSafety.js';
import { SolanaRpcClient } from './providers/solana/rpcClient.js';
import { UnconfiguredWalletProvider } from './providers/wallet/unconfigured.js';
import { recordProviderError } from './repositories/enrichment.js';
import { outboxBacklog } from './repositories/alerts.js';
import { DiscoveryService } from './services/discoveryService.js';
import { EnrichmentService } from './services/enrichmentService.js';
import { MaintenanceService } from './services/maintenanceService.js';
import { MarketDataService } from './services/marketDataService.js';
import { NotificationService } from './services/notificationService.js';
import { SignalService } from './services/signalService.js';
import { Scheduler } from './workers/scheduler.js';

export interface AppOverrides {
  logger?: Logger;
  discovery?: TokenDiscoveryProvider[];
  marketData?: MarketDataProvider;
  safety?: SafetyProvider[];
  holders?: HolderProvider | null;
  notifier?: NotificationProvider;
  wallet?: WalletProvider;
  /** Job intervals, shortened in tests. */
  intervalsMs?: Partial<Record<'marketData' | 'enrichment' | 'notifications' | 'tiers' | 'retention', number>>;
}

export interface App {
  readonly logger: Logger;
  readonly pool: Pool;
  readonly metrics: Metrics;
  readonly healthPort: number | null;
  start(): Promise<void>;
  stop(): Promise<void>;
}

/** Records schema problems in `provider_errors`, at most once a minute per provider endpoint. */
function schemaRecorder(pool: Pool, logger: Logger) {
  const lastWrite = new Map<string, number>();
  return (info: SchemaErrorInfo) => {
    const key = `${info.provider}:${info.endpoint}`;
    if (Date.now() - (lastWrite.get(key) ?? 0) < 60_000) return;
    lastWrite.set(key, Date.now());
    recordProviderError(pool, {
      provider: info.provider,
      endpoint: info.endpoint,
      kind: 'schema',
      message: info.issues.join('; '),
      sample: info.sample,
    }).catch((err: unknown) => logger.warn({ err: String(err) }, 'could not record provider error'));
  };
}

export async function createApp(config: AppConfig, overrides: AppOverrides = {}): Promise<App> {
  const logger = overrides.logger ?? createLogger(config.logLevel);
  const metrics = new Metrics({ defaultMetrics: true });
  const pool = createPool(config.database.url, config.database.poolMax, logger);

  let lock: PoolClient | null = null;
  let cache: Cache | null = null;
  try {
    if (config.runMigrationsOnStart) await migrate(pool, logger);
    lock = await acquireInstanceLock(pool);
    if (!lock) throw new Error('another signal-radar instance is already running against this database');
    cache = config.redisUrl ? await RedisCache.connect(config.redisUrl, logger) : new MemoryCache();
  } catch (err) {
    if (lock) await releaseInstanceLock(lock).catch(() => undefined);
    await pool.end().catch(() => undefined);
    throw err;
  }

  const recordSchema = schemaRecorder(pool, logger);
  const http = (provider: string, limiter: TokenBucket, extra: Partial<ConstructorParameters<typeof HttpClient>[0]> = {}) =>
    new HttpClient({ provider, logger, limiter, metrics, onSchemaError: recordSchema, ...extra });

  // --- providers -------------------------------------------------------------------
  const rpc = new SolanaRpcClient(
    http('solana-rpc', new TokenBucket({ perSecond: config.solana.requestsPerSecond }), { timeoutMs: 15_000 }),
    config.solana.httpUrl,
  );
  const mints = new MintInfoLoader(rpc, cache);

  const discovery = overrides.discovery ?? [
    new SolanaLogsDiscoveryProvider({
      wsUrl: config.solana.wsUrl,
      rpc,
      sources: resolveSources(config.solana.discoverySources, config.solana.customSources),
      quoteMints: QUOTE_MINTS,
      resolverConcurrency: config.solana.resolverConcurrency,
      logger,
      metrics,
    }),
  ];
  const marketData =
    overrides.marketData ??
    new DexScreenerMarketDataProvider(
      http('dexscreener', TokenBucket.perMinute(config.dexscreener.requestsPerMinute)),
      config.dexscreener.baseUrl,
      logger,
    );
  const safety = overrides.safety ?? [
    new SolanaOnchainSafetyProvider(mints),
    ...(config.rugcheck.enabled
      ? [
          new RugCheckSafetyProvider(
            http('rugcheck', TokenBucket.perMinute(config.rugcheck.requestsPerMinute)),
            config.rugcheck.baseUrl,
            config.rugcheck.apiKey,
          ),
        ]
      : []),
    ...(config.goplus.enabled
      ? [
          new GoPlusSafetyProvider(
            http('goplus', TokenBucket.perMinute(config.goplus.requestsPerMinute)),
            config.goplus.baseUrl,
            config.goplus.accessToken,
          ),
        ]
      : []),
  ];
  const holders = overrides.holders !== undefined ? overrides.holders : new SolanaRpcHolderProvider({ rpc, mints });
  const wallet = overrides.wallet ?? new UnconfiguredWalletProvider();
  const notifier =
    overrides.notifier ??
    (config.discord.webhookUrl
      ? new DiscordWebhookProvider(
          // ~25/min with a burst of 3 keeps under both the per-webhook and per-channel limits.
          http('discord', new TokenBucket({ perSecond: 25 / 60, burst: 3 }), { retries: 2, timeoutMs: 10_000 }),
          config.discord.webhookUrl,
          config.discord.username,
        )
      : new ConsoleNotificationProvider(logger));

  // --- services ---------------------------------------------------------------------
  const signals = new SignalService({
    db: pool,
    alerts: config.alerts,
    momentum: config.momentum,
    minExternalSafety: config.safety.minExternalPasses,
    logger,
    metrics,
  });
  const discoveryService = new DiscoveryService({ db: pool, providers: discovery, logger, metrics });
  const marketDataService = new MarketDataService({
    db: pool,
    chain: 'solana',
    provider: marketData,
    tiers: config.tiers,
    logger,
    metrics,
    onSnapshot: async (token, snapshot) => {
      await signals.evaluate(token, snapshot);
    },
  });
  const enrichmentService = new EnrichmentService({
    db: pool,
    chain: 'solana',
    safetyProviders: safety,
    holderProvider: holders,
    tiers: config.tiers,
    concurrency: config.enrichConcurrency,
    logger,
    metrics,
    onSchemaError: (provider: string, err: SchemaError) =>
      recordSchema({ provider, endpoint: 'check', issues: err.issues, sample: err.sample ?? null }),
    onEnriched: async (token) => {
      await signals.evaluateLatest(token);
    },
  });
  const notificationService = new NotificationService({ db: pool, provider: notifier, logger, metrics });
  const maintenance = new MaintenanceService({ db: pool, tiers: config.tiers, retentionDays: config.retentionDays, logger, metrics });

  const iv = overrides.intervalsMs ?? {};
  const scheduler = new Scheduler(logger, metrics)
    .add({ name: 'market-data', intervalMs: iv.marketData ?? 1_000, run: (s) => marketDataService.runOnce(s) })
    .add({ name: 'enrichment', intervalMs: iv.enrichment ?? 1_000, run: (s) => enrichmentService.runOnce(s) })
    .add({ name: 'notifications', intervalMs: iv.notifications ?? 1_000, run: (s) => notificationService.runOnce(s) })
    .add({ name: 'tiers', intervalMs: iv.tiers ?? 30_000, run: () => maintenance.tiersOnce() })
    .add({ name: 'retention', intervalMs: iv.retention ?? 3_600_000, run: () => maintenance.retentionOnce() });

  let health: HealthServer | null = null;
  let stopping = false;
  let stopped: Promise<void> | null = null;

  const readiness = async () => {
    const db = await ping(pool);
    const disc = discoveryService.health();
    return {
      ready: db && disc.connected && !stopping,
      checks: {
        database: db,
        discovery: disc,
        cache: cache!.kind,
        notifier: notifier.name,
        outboxBacklog: db ? await outboxBacklog(pool).catch(() => null) : null,
        jobs: scheduler.jobStatus(),
        stopping,
      },
    };
  };

  return {
    logger,
    pool,
    metrics,
    get healthPort() {
      return health?.port ?? null;
    },
    async start() {
      logger.info(
        {
          discovery: discovery.map((d) => d.name),
          marketData: marketData.name,
          safety: safety.map((s) => s.name),
          holders: holders?.name ?? null,
          notifier: notifier.name,
          wallet: wallet.available ? wallet.name : 'not connected (roadmap phase 4)',
          cache: cache!.kind,
        },
        'starting signal-radar',
      );
      if (notifier.name === 'console') {
        logger.warn('DISCORD_WEBHOOK_URL not set: alerts are logged (dry run), not sent');
      }
      await discoveryService.start();
      scheduler.start();
      health = await startHealthServer({
        host: config.health.host,
        port: config.health.port,
        logger,
        registry: metrics.registry,
        readiness,
      });
    },
    stop() {
      stopped ??= (async () => {
        stopping = true;
        logger.info('stopping: no new work; finishing in-flight work');
        await discoveryService.stop();
        const clean = await scheduler.stop(Math.max(1_000, config.shutdownTimeoutMs - 2_000));
        if (!clean) logger.warn('some jobs did not finish in time; their leases will expire and they will be retried');
        await health?.close();
        await cache!.close();
        if (lock) await releaseInstanceLock(lock).catch(() => undefined);
        await pool.end();
        logger.info('stopped');
      })();
      return stopped;
    },
  };
}
