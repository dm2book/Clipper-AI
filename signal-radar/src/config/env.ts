/**
 * All configuration comes from environment variables, validated once at
 * startup. Invalid configuration stops the process with a message that names
 * the variables but never echoes their values (they may be secrets).
 */
import { z } from 'zod';

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
  momentum: {
    minScore: number;
    minActiveComponents: number;
    minLiquidityUsd: number;
    minConfidence: number;
  };
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
  MOMENTUM_MIN_SCORE: num(0).max(100).default(60),
  MOMENTUM_MIN_ACTIVE_COMPONENTS: int(1).default(3),
  MOMENTUM_MIN_LIQUIDITY_USD: num(0).default(20_000),
  MOMENTUM_MIN_CONFIDENCE: num(0).max(1).default(0.7),
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

  return {
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
      momentum: {
        minScore: e.MOMENTUM_MIN_SCORE,
        minActiveComponents: e.MOMENTUM_MIN_ACTIVE_COMPONENTS,
        minLiquidityUsd: e.MOMENTUM_MIN_LIQUIDITY_USD,
        minConfidence: e.MOMENTUM_MIN_CONFIDENCE,
      },
      safetyAllowWarn: e.SAFETY_ALLOW_WARN,
      cooldownMinutes: e.ALERT_COOLDOWN_MINUTES,
      escalationPoints: e.ALERT_ESCALATION_POINTS,
      maxPerHour: e.ALERTS_MAX_PER_HOUR,
      marketDataMaxAgeSec: e.MARKET_DATA_MAX_AGE_SECONDS,
      holdersMaxAgeSec: e.HOLDERS_MAX_AGE_SECONDS,
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
}
