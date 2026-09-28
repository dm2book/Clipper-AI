/**
 * All configuration comes from environment variables, validated once at
 * startup. Invalid configuration stops the process with a message that names
 * the variables but never echoes their values (they may be secrets).
 */
import { z } from 'zod';
import { DEFAULT_THRESHOLDS, parseOverrides, resolveThresholds, type ThresholdConfig } from '../momentum/thresholds.js';
import { DEFAULT_ACTIVITY, DEFAULT_CRITERIA, type WalletIntelligenceConfig } from '../wallets/criteria.js';

export interface TierIntervals {
  HOT: number;
  WARM: number;
  COOL: number;
}

export interface TierSettings {
  hotMinutes: number;
  warmMinutes: number;
  archiveAfterHours: number;
  snapshotIntervalSec: TierIntervals;
  enrichIntervalSec: TierIntervals;
  safetyRecheckSec: TierIntervals;
  /** Below this liquidity a token is polled at the COOL rate and not enriched. */
  lowLiquidityUsd: number;
  lowLiquidityArchiveMinutes: number;
  unindexedArchiveMinutes: number;
}

export interface AlertSettings {
  newToken: { maxAgeMinutes: number; minLiquidityUsd: number; minHolders: number };
  safetyAllowWarn: boolean;
  cooldownMinutes: number;
  escalationPoints: number;
  maxPerHour: number;
  marketDataMaxAgeSec: number;
  holdersMaxAgeSec: number;
}

export interface CustomDiscoverySource {
  name: string;
  programId: string;
  logPatterns: string[];
}

export interface AppConfig {
  nodeEnv: 'development' | 'production' | 'test';
  logLevel: string;
  runMigrationsOnStart: boolean;
  database: { url: string; poolMax: number };
  redisUrl: string | null;
  solana: {
    httpUrl: string;
    wsUrl: string;
    requestsPerSecond: number;
    discoverySources: string[];
    customSources: CustomDiscoverySource[];
    resolverConcurrency: number;
  };
  dexscreener: { baseUrl: string; requestsPerMinute: number };
  rugcheck: { enabled: boolean; baseUrl: string; apiKey: string | null; requestsPerMinute: number };
  goplus: { enabled: boolean; baseUrl: string; accessToken: string | null; requestsPerMinute: number };
  safety: { minExternalPasses: number };
  discord: { webhookUrl: string | null; username: string };
  alerts: AlertSettings;
  /** Momentum Detection Engine thresholds (defaults + per chain/token-type overrides). */
  momentum: ThresholdConfig;
  /** Wallet Intelligence: classification criteria, whale/tracked/cluster thresholds, watchlist. */
  wallets: WalletIntelligenceConfig;
  tiers: TierSettings;
  enrichConcurrency: number;
  retentionDays: number;
  health: { host: string; port: number };
  shutdownTimeoutMs: number;
}

const DISCORD_WEBHOOK =
  /^https:\/\/(?:(?:canary|ptb)\.)?discord(?:app)?\.com\/api(?:\/v\d+)?\/webhooks\/\d+\/[\w-]+$/;

const httpUrl = z.string().refine((v) => /^https?:\/\//.test(v) && URL.canParse(v), {
  message: 'must be an http(s) URL',
});
const wsUrl = z.string().refine((v) => /^wss?:\/\//.test(v) && URL.canParse(v), {
  message: 'must be a ws(s) URL',
});
const webhookUrl = z.string().refine((v) => DISCORD_WEBHOOK.test(v), {
  message: 'must be a Discord webhook URL (https://discord.com/api/webhooks/<id>/<token>)',
});
const int = (min: number) => z.coerce.number().int().min(min);
const num = (min: number) => z.coerce.number().min(min);
const csv = z.string().transform((s) =>
  s
    .split(',')
    .map((x) => x.trim())
    .filter(Boolean),
);
const addressList = csv.pipe(
  z.array(z.string().regex(/^[1-9A-HJ-NP-Za-km-z]{32,44}$/, 'must be a comma-separated list of base58 addresses')),
);
const customSources = z
  .string()
  .transform((s, ctx) => {
    try {
      return JSON.parse(s) as unknown;
    } catch {
      ctx.addIssue({ code: 'custom', message: 'must be valid JSON' });
      return z.NEVER;
    }
  })
  .pipe(
    z.array(
      z.object({
        name: z.string().regex(/^[a-z0-9_]+$/),
        programId: z.string().min(32).max(44),
        logPatterns: z.array(z.string().min(3)).min(1),
      }),
    ),
  );

export const envSchema = z.object({
  NODE_ENV: z.enum(['development', 'production', 'test']).default('development'),
  LOG_LEVEL: z.enum(['fatal', 'error', 'warn', 'info', 'debug', 'trace', 'silent']).default('info'),
  RUN_MIGRATIONS_ON_START: z.stringbool().default(true),

  DATABASE_URL: z.string().regex(/^postgres(?:ql)?:\/\//, 'must be a postgres:// URL'),
  DATABASE_POOL_MAX: int(2).default(10),
  REDIS_URL: z.string().regex(/^rediss?:\/\//, 'must be a redis:// URL').optional(),

  SOLANA_RPC_HTTP_URL: httpUrl,
  SOLANA_RPC_WS_URL: wsUrl,
  SOLANA_RPC_REQUESTS_PER_SECOND: num(0.1).default(10),
  DISCOVERY_SOURCES: csv.default(['raydium_amm_v4', 'raydium_cpmm', 'pumpswap']),
  DISCOVERY_CUSTOM_SOURCES: customSources.optional(),
  DISCOVERY_RESOLVER_CONCURRENCY: int(1).default(4),

  DEXSCREENER_BASE_URL: httpUrl.default('https://api.dexscreener.com'),
  DEXSCREENER_REQUESTS_PER_MINUTE: num(1).max(300).default(150),

  RUGCHECK_ENABLED: z.stringbool().default(true),
  RUGCHECK_BASE_URL: httpUrl.default('https://api.rugcheck.xyz/v1'),
  RUGCHECK_API_KEY: z.string().min(1).optional(),
  RUGCHECK_REQUESTS_PER_MINUTE: num(1).default(30),

  GOPLUS_ENABLED: z.stringbool().default(true),
  GOPLUS_BASE_URL: httpUrl.default('https://api.gopluslabs.io'),
  GOPLUS_ACCESS_TOKEN: z.string().min(1).optional(),
  GOPLUS_REQUESTS_PER_MINUTE: num(1).default(20),

  SAFETY_MIN_EXTERNAL_PASSES: int(0).default(1),
  SAFETY_ALLOW_WARN: z.stringbool().default(true),

  DISCORD_WEBHOOK_URL: webhookUrl.optional(),
  DISCORD_USERNAME: z.string().min(1).max(80).default('Signal Radar'),

  NEW_TOKEN_MAX_AGE_MINUTES: num(1).default(10),
  NEW_TOKEN_MIN_LIQUIDITY_USD: num(0).default(20_000),
  NEW_TOKEN_MIN_HOLDERS: int(0).default(50),
  // Momentum Detection Engine (src/momentum/thresholds.ts documents each value)
  MOMENTUM_PRIMARY_WINDOW: z.enum(['1m', '5m', '15m', '30m', '1h']).default(DEFAULT_THRESHOLDS.primaryWindow),
  VOLUME_SPIKE_THRESHOLD: num(1).default(DEFAULT_THRESHOLDS.volumeSpikePct),
  TX_SPIKE_THRESHOLD: num(1).default(DEFAULT_THRESHOLDS.txSpikePct),
  BUYER_GROWTH_THRESHOLD: num(1).default(DEFAULT_THRESHOLDS.buyerGrowthPct),
  HOLDER_GROWTH_THRESHOLD: num(1).default(DEFAULT_THRESHOLDS.holderGrowthPct),
  LIQUIDITY_GROWTH_THRESHOLD: num(1).default(DEFAULT_THRESHOLDS.liquidityGrowthPct),
  MARKET_CAP_CHANGE_THRESHOLD: num(1).default(DEFAULT_THRESHOLDS.marketCapChangePct),
  BUY_SHARE_THRESHOLD: num(0.5).max(1).default(DEFAULT_THRESHOLDS.buyShare),
  VOLUME_ACCELERATION_THRESHOLD: num(1).default(DEFAULT_THRESHOLDS.volumeAccelerationPct),
  TX_ACCELERATION_THRESHOLD: num(1).default(DEFAULT_THRESHOLDS.txAccelerationPct),
  MIN_LIQUIDITY: num(0).default(DEFAULT_THRESHOLDS.minLiquidityUsd),
  MIN_HOLDERS: int(0).default(DEFAULT_THRESHOLDS.minHolders),
  MIN_WINDOW_VOLUME_USD: num(0).default(DEFAULT_THRESHOLDS.minWindowVolumeUsd),
  MIN_WINDOW_TRANSACTIONS: int(0).default(DEFAULT_THRESHOLDS.minWindowTransactions),
  MIN_UNIQUE_BUYERS: int(0).default(DEFAULT_THRESHOLDS.minUniqueBuyers),
  SINGLE_TRADE_MAX_SHARE: num(0.01).max(1).default(DEFAULT_THRESHOLDS.singleTradeMaxShare),
  SINGLE_TRADE_MEDIAN_MULTIPLE: num(1).default(DEFAULT_THRESHOLDS.singleTradeMedianMultiple),
  WASH_MAX_TOP_WALLETS_SHARE: num(0).max(1).default(DEFAULT_THRESHOLDS.washMaxTopWalletsShare),
  WASH_MAX_ROUND_TRIP_SHARE: num(0).max(1).default(DEFAULT_THRESHOLDS.washMaxRoundTripShare),
  WASH_MAX_TX_PER_WALLET: num(1).default(DEFAULT_THRESHOLDS.washMaxTransactionsPerWallet),
  LIQUIDITY_DROP_THRESHOLD: num(1).default(DEFAULT_THRESHOLDS.liquidityDropPct),
  MOMENTUM_REQUIRE_TRADE_DATA: z.stringbool().default(DEFAULT_THRESHOLDS.requireTradeData),
  MOMENTUM_MIN_SCORE: num(0).max(100).default(DEFAULT_THRESHOLDS.minScore),
  MOMENTUM_MIN_TRIGGERED_RULES: int(1).max(9).default(DEFAULT_THRESHOLDS.minTriggeredRules),
  MOMENTUM_MIN_CONFIDENCE: num(0).max(1).default(DEFAULT_THRESHOLDS.minConfidence),
  MOMENTUM_THRESHOLD_OVERRIDES: z
    .string()
    .transform((s, ctx) => {
      try {
        return parseOverrides(s);
      } catch (err) {
        ctx.addIssue({ code: 'custom', message: (err as Error).message });
        return z.NEVER;
      }
    })
    .optional(),
  // Wallet Intelligence (src/wallets/criteria.ts documents each value)
  WALLET_CRITERIA_VERSION: z.string().min(1).max(64).default(DEFAULT_CRITERIA.version),
  WALLET_STATS_WINDOW_DAYS: int(1).default(DEFAULT_CRITERIA.statsWindowDays),
  WALLET_MIN_CLOSED_POSITIONS: int(1).default(DEFAULT_CRITERIA.minClosedPositions),
  WALLET_MIN_WIN_RATE: num(0).max(1).default(DEFAULT_CRITERIA.minWinRate),
  WALLET_MIN_WIN_RATE_LOWER_BOUND: num(0).max(1).default(DEFAULT_CRITERIA.minWinRateLowerBound),
  WALLET_MIN_AVG_RETURN_PCT: z.coerce.number().default(DEFAULT_CRITERIA.minAvgReturnPct),
  WALLET_MIN_MEDIAN_RETURN_PCT: z.coerce.number().default(DEFAULT_CRITERIA.minMedianReturnPct),
  WALLET_MIN_REALIZED_PNL_USD: z.coerce.number().default(DEFAULT_CRITERIA.minRealizedPnlUsd),
  WALLET_MAX_LARGEST_WIN_SHARE: num(0.01).max(1).default(DEFAULT_CRITERIA.maxLargestWinShare),
  WALLET_MAX_TRADES_PER_DAY: num(0.01).default(DEFAULT_CRITERIA.maxTradesPerDay),
  WALLET_AUTO_TRACK_QUALIFIED: z.stringbool().default(DEFAULT_CRITERIA.autoTrackQualified),
  WHALE_MIN_TRADE_USD: num(1).default(DEFAULT_ACTIVITY.whaleMinTradeUsd),
  WHALE_MIN_LIQUIDITY_SHARE: num(0.001).max(1).default(DEFAULT_ACTIVITY.whaleMinLiquidityShare),
  WHALE_LIQUIDITY_SHARE_FLOOR_USD: num(0).default(DEFAULT_ACTIVITY.whaleLiquidityShareFloorUsd),
  WHALE_MAX_ALERTS_PER_WALLET_PER_HOUR: int(1).default(DEFAULT_ACTIVITY.whaleMaxAlertsPerWalletPerHour),
  TRACKED_MIN_TRADE_USD: num(0).default(DEFAULT_ACTIVITY.trackedMinTradeUsd),
  CLUSTER_WINDOW_MINUTES: num(0.5).default(DEFAULT_ACTIVITY.clusterWindowMinutes),
  CLUSTER_MIN_WALLETS: int(2).default(DEFAULT_ACTIVITY.clusterMinWallets),
  CLUSTER_MAX_WALLET_SHARE: num(0.01).max(1).default(DEFAULT_ACTIVITY.clusterMaxWalletShare),
  CLUSTER_COOLDOWN_MINUTES: num(0).default(DEFAULT_ACTIVITY.clusterCooldownMinutes),
  WALLET_EXCLUDE_PROGRAM_OWNED: z.stringbool().default(DEFAULT_ACTIVITY.excludeProgramOwned),
  WALLET_IGNORE_LIST: addressList.optional(),
  TRACKED_WALLETS: addressList.optional(),
  WALLET_ALERT_MAX_EVENT_AGE_SECONDS: int(1).default(300),
  WALLET_STATS_REFRESH_MINUTES: int(1).default(60),
  WALLET_EVENT_RETENTION_DAYS: int(1).default(365),
  ALERT_COOLDOWN_MINUTES: num(0).default(15),
  ALERT_ESCALATION_POINTS: num(0).default(15),
  ALERTS_MAX_PER_HOUR: int(1).default(30),
  MARKET_DATA_MAX_AGE_SECONDS: int(5).default(60),
  HOLDERS_MAX_AGE_SECONDS: int(10).default(180),

  TIER_HOT_MINUTES: num(1).default(15),
  TIER_WARM_MINUTES: num(1).default(60),
  ARCHIVE_AFTER_HOURS: num(1).default(6),
  SNAPSHOT_INTERVAL_HOT_SECONDS: int(5).default(15),
  SNAPSHOT_INTERVAL_WARM_SECONDS: int(5).default(60),
  SNAPSHOT_INTERVAL_COOL_SECONDS: int(5).default(300),
  ENRICH_INTERVAL_HOT_SECONDS: int(10).default(60),
  ENRICH_INTERVAL_WARM_SECONDS: int(10).default(300),
  ENRICH_INTERVAL_COOL_SECONDS: int(10).default(1800),
  SAFETY_RECHECK_HOT_SECONDS: int(10).default(120),
  SAFETY_RECHECK_WARM_SECONDS: int(10).default(1800),
  SAFETY_RECHECK_COOL_SECONDS: int(10).default(3600),
  LOW_LIQUIDITY_USD: num(0).default(1_000),
  LOW_LIQUIDITY_ARCHIVE_MINUTES: int(1).default(15),
  UNINDEXED_ARCHIVE_MINUTES: int(1).default(30),
  ENRICH_CONCURRENCY: int(1).default(4),
  RETENTION_DAYS: int(1).default(30),

  HEALTH_HOST: z.string().default('0.0.0.0'),
  HEALTH_PORT: int(0).max(65535).default(8080),
  SHUTDOWN_TIMEOUT_MS: int(1000).default(20_000),
});

export class ConfigError extends Error {
  constructor(readonly problems: string[]) {
    super(`invalid configuration:\n  - ${problems.join('\n  - ')}`);
    this.name = 'ConfigError';
  }
}

export function loadConfig(source: NodeJS.ProcessEnv = process.env): AppConfig {
  // `FOO=` in a .env file means "not set", not "empty string".
  const cleaned = Object.fromEntries(
    Object.entries(source).filter(([, v]) => v !== undefined && v.trim() !== ''),
  );
  const parsed = envSchema.safeParse(cleaned);
  if (!parsed.success) {
    throw new ConfigError(
      parsed.error.issues.map((i) => `${i.path.join('.') || '(root)'}: ${i.message}`),
    );
  }
  const e = parsed.data;
  if (e.TIER_WARM_MINUTES <= e.TIER_HOT_MINUTES) {
    throw new ConfigError(['TIER_WARM_MINUTES: must be greater than TIER_HOT_MINUTES']);
  }
  if (e.ARCHIVE_AFTER_HOURS * 60 <= e.TIER_WARM_MINUTES) {
    throw new ConfigError(['ARCHIVE_AFTER_HOURS: must be later than TIER_WARM_MINUTES']);
  }
  if (e.CLUSTER_MAX_WALLET_SHARE * e.CLUSTER_MIN_WALLETS < 1) {
    throw new ConfigError(['CLUSTER_MAX_WALLET_SHARE: must be at least 1 / CLUSTER_MIN_WALLETS']);
  }
  if (e.WALLET_EVENT_RETENTION_DAYS < e.WALLET_STATS_WINDOW_DAYS) {
    throw new ConfigError(['WALLET_EVENT_RETENTION_DAYS: must be at least WALLET_STATS_WINDOW_DAYS']);
  }

  const config: AppConfig = {
    nodeEnv: e.NODE_ENV,
    logLevel: e.LOG_LEVEL,
    runMigrationsOnStart: e.RUN_MIGRATIONS_ON_START,
    database: { url: e.DATABASE_URL, poolMax: e.DATABASE_POOL_MAX },
    redisUrl: e.REDIS_URL ?? null,
    solana: {
      httpUrl: e.SOLANA_RPC_HTTP_URL,
      wsUrl: e.SOLANA_RPC_WS_URL,
      requestsPerSecond: e.SOLANA_RPC_REQUESTS_PER_SECOND,
      discoverySources: e.DISCOVERY_SOURCES,
      customSources: e.DISCOVERY_CUSTOM_SOURCES ?? [],
      resolverConcurrency: e.DISCOVERY_RESOLVER_CONCURRENCY,
    },
    dexscreener: {
      baseUrl: e.DEXSCREENER_BASE_URL.replace(/\/+$/, ''),
      requestsPerMinute: e.DEXSCREENER_REQUESTS_PER_MINUTE,
    },
    rugcheck: {
      enabled: e.RUGCHECK_ENABLED,
      baseUrl: e.RUGCHECK_BASE_URL.replace(/\/+$/, ''),
      apiKey: e.RUGCHECK_API_KEY ?? null,
      requestsPerMinute: e.RUGCHECK_REQUESTS_PER_MINUTE,
    },
    goplus: {
      enabled: e.GOPLUS_ENABLED,
      baseUrl: e.GOPLUS_BASE_URL.replace(/\/+$/, ''),
      accessToken: e.GOPLUS_ACCESS_TOKEN ?? null,
      requestsPerMinute: e.GOPLUS_REQUESTS_PER_MINUTE,
    },
    safety: { minExternalPasses: e.SAFETY_MIN_EXTERNAL_PASSES },
    discord: {
      webhookUrl: e.DISCORD_WEBHOOK_URL ?? null,
      username: e.DISCORD_USERNAME,
    },
    alerts: {
      newToken: {
        maxAgeMinutes: e.NEW_TOKEN_MAX_AGE_MINUTES,
        minLiquidityUsd: e.NEW_TOKEN_MIN_LIQUIDITY_USD,
        minHolders: e.NEW_TOKEN_MIN_HOLDERS,
      },
      safetyAllowWarn: e.SAFETY_ALLOW_WARN,
      cooldownMinutes: e.ALERT_COOLDOWN_MINUTES,
      escalationPoints: e.ALERT_ESCALATION_POINTS,
      maxPerHour: e.ALERTS_MAX_PER_HOUR,
      marketDataMaxAgeSec: e.MARKET_DATA_MAX_AGE_SECONDS,
      holdersMaxAgeSec: e.HOLDERS_MAX_AGE_SECONDS,
    },
    momentum: {
      defaults: {
        ...DEFAULT_THRESHOLDS,
        primaryWindow: e.MOMENTUM_PRIMARY_WINDOW,
        volumeSpikePct: e.VOLUME_SPIKE_THRESHOLD,
        txSpikePct: e.TX_SPIKE_THRESHOLD,
        buyerGrowthPct: e.BUYER_GROWTH_THRESHOLD,
        holderGrowthPct: e.HOLDER_GROWTH_THRESHOLD,
        liquidityGrowthPct: e.LIQUIDITY_GROWTH_THRESHOLD,
        marketCapChangePct: e.MARKET_CAP_CHANGE_THRESHOLD,
        buyShare: e.BUY_SHARE_THRESHOLD,
        buyShareFull: Math.max(DEFAULT_THRESHOLDS.buyShareFull, Math.min(1, e.BUY_SHARE_THRESHOLD + 0.2)),
        volumeAccelerationPct: e.VOLUME_ACCELERATION_THRESHOLD,
        txAccelerationPct: e.TX_ACCELERATION_THRESHOLD,
        minLiquidityUsd: e.MIN_LIQUIDITY,
        minHolders: e.MIN_HOLDERS,
        minWindowVolumeUsd: e.MIN_WINDOW_VOLUME_USD,
        minWindowTransactions: e.MIN_WINDOW_TRANSACTIONS,
        minUniqueBuyers: e.MIN_UNIQUE_BUYERS,
        singleTradeMaxShare: e.SINGLE_TRADE_MAX_SHARE,
        singleTradeMedianMultiple: e.SINGLE_TRADE_MEDIAN_MULTIPLE,
        washMaxTopWalletsShare: e.WASH_MAX_TOP_WALLETS_SHARE,
        washMaxRoundTripShare: e.WASH_MAX_ROUND_TRIP_SHARE,
        washMaxTransactionsPerWallet: e.WASH_MAX_TX_PER_WALLET,
        liquidityDropPct: e.LIQUIDITY_DROP_THRESHOLD,
        requireTradeData: e.MOMENTUM_REQUIRE_TRADE_DATA,
        minScore: e.MOMENTUM_MIN_SCORE,
        minTriggeredRules: e.MOMENTUM_MIN_TRIGGERED_RULES,
        minConfidence: e.MOMENTUM_MIN_CONFIDENCE,
      },
      overrides: e.MOMENTUM_THRESHOLD_OVERRIDES ?? {},
    },
    wallets: {
      criteria: {
        version: e.WALLET_CRITERIA_VERSION,
        statsWindowDays: e.WALLET_STATS_WINDOW_DAYS,
        minClosedPositions: e.WALLET_MIN_CLOSED_POSITIONS,
        minWinRate: e.WALLET_MIN_WIN_RATE,
        minWinRateLowerBound: e.WALLET_MIN_WIN_RATE_LOWER_BOUND,
        minAvgReturnPct: e.WALLET_MIN_AVG_RETURN_PCT,
        minMedianReturnPct: e.WALLET_MIN_MEDIAN_RETURN_PCT,
        minRealizedPnlUsd: e.WALLET_MIN_REALIZED_PNL_USD,
        maxLargestWinShare: e.WALLET_MAX_LARGEST_WIN_SHARE,
        maxTradesPerDay: e.WALLET_MAX_TRADES_PER_DAY,
        autoTrackQualified: e.WALLET_AUTO_TRACK_QUALIFIED,
      },
      activity: {
        whaleMinTradeUsd: e.WHALE_MIN_TRADE_USD,
        whaleMinLiquidityShare: e.WHALE_MIN_LIQUIDITY_SHARE,
        whaleLiquidityShareFloorUsd: e.WHALE_LIQUIDITY_SHARE_FLOOR_USD,
        whaleMaxAlertsPerWalletPerHour: e.WHALE_MAX_ALERTS_PER_WALLET_PER_HOUR,
        trackedMinTradeUsd: e.TRACKED_MIN_TRADE_USD,
        clusterWindowMinutes: e.CLUSTER_WINDOW_MINUTES,
        clusterMinWallets: e.CLUSTER_MIN_WALLETS,
        clusterMaxWalletShare: e.CLUSTER_MAX_WALLET_SHARE,
        clusterCooldownMinutes: e.CLUSTER_COOLDOWN_MINUTES,
        excludeProgramOwned: e.WALLET_EXCLUDE_PROGRAM_OWNED,
        ignoredWallets: e.WALLET_IGNORE_LIST ?? [],
      },
      manualWallets: [...new Set(e.TRACKED_WALLETS ?? [])],
      alertMaxEventAgeSec: e.WALLET_ALERT_MAX_EVENT_AGE_SECONDS,
      statsRefreshMinutes: e.WALLET_STATS_REFRESH_MINUTES,
      eventRetentionDays: e.WALLET_EVENT_RETENTION_DAYS,
    },
    tiers: {
      hotMinutes: e.TIER_HOT_MINUTES,
      warmMinutes: e.TIER_WARM_MINUTES,
      archiveAfterHours: e.ARCHIVE_AFTER_HOURS,
      snapshotIntervalSec: {
        HOT: e.SNAPSHOT_INTERVAL_HOT_SECONDS,
        WARM: e.SNAPSHOT_INTERVAL_WARM_SECONDS,
        COOL: e.SNAPSHOT_INTERVAL_COOL_SECONDS,
      },
      enrichIntervalSec: {
        HOT: e.ENRICH_INTERVAL_HOT_SECONDS,
        WARM: e.ENRICH_INTERVAL_WARM_SECONDS,
        COOL: e.ENRICH_INTERVAL_COOL_SECONDS,
      },
      safetyRecheckSec: {
        HOT: e.SAFETY_RECHECK_HOT_SECONDS,
        WARM: e.SAFETY_RECHECK_WARM_SECONDS,
        COOL: e.SAFETY_RECHECK_COOL_SECONDS,
      },
      lowLiquidityUsd: e.LOW_LIQUIDITY_USD,
      lowLiquidityArchiveMinutes: e.LOW_LIQUIDITY_ARCHIVE_MINUTES,
      unindexedArchiveMinutes: e.UNINDEXED_ARCHIVE_MINUTES,
    },
    enrichConcurrency: e.ENRICH_CONCURRENCY,
    retentionDays: e.RETENTION_DAYS,
    health: { host: e.HEALTH_HOST, port: e.HEALTH_PORT },
    shutdownTimeoutMs: e.SHUTDOWN_TIMEOUT_MS,
  };

  // Every threshold layer must resolve to a valid profile, checked now rather
  // than on the first token of an affected chain/type.
  try {
    resolveThresholds(config.momentum, 'solana', null);
    for (const key of Object.keys(config.momentum.overrides)) {
      const [chain, tokenType] = key.split(':');
      resolveThresholds(config.momentum, chain!, tokenType ?? null);
    }
  } catch (err) {
    throw new ConfigError([`momentum thresholds: ${(err as Error).message}`]);
  }
  return config;
}
