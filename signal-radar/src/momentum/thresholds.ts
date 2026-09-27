/**
 * Thresholds for the Momentum Detection Engine.
 *
 * Resolution order (later wins), so values can differ per chain and per
 * token type without code changes:
 *   built-in defaults → environment variables → overrides["<chain>"]
 *   → overrides["<chain>:<tokenType>"]
 * The token type is the DEX of the token's main pool (e.g. "raydium",
 * "pumpswap"). Overrides come from MOMENTUM_THRESHOLD_OVERRIDES (JSON).
 */
import { z } from 'zod';
import { WINDOWS, type WindowId } from './indicators.js';

export const RULE_IDS = [
  'volume_spike',
  'tx_spike',
  'buyer_growth',
  'holder_growth',
  'liquidity_growth',
  'market_cap_change',
  'buy_pressure',
  'volume_acceleration',
  'tx_acceleration',
] as const;
export type RuleId = (typeof RULE_IDS)[number];

export interface MomentumThresholds {
  /** Window the rules are scored on; all windows are still reported. */
  primaryWindow: WindowId;
  // --- rules: trigger thresholds (percent unless noted) -----------------
  volumeSpikePct: number;
  txSpikePct: number;
  buyerGrowthPct: number;
  holderGrowthPct: number;
  liquidityGrowthPct: number;
  marketCapChangePct: number;
  /** Share of transactions that are buys, 0–1. */
  buyShare: number;
  volumeAccelerationPct: number;
  txAccelerationPct: number;
  /** A rule reaches full strength at threshold × this factor (buy share: at `buyShareFull`). */
  fullStrengthFactor: number;
  buyShareFull: number;
  // --- false-positive filters ----------------------------------------------
  minLiquidityUsd: number;
  minHolders: number;
  minWindowVolumeUsd: number;
  minWindowTransactions: number;
  minUniqueBuyers: number;
  singleTradeMaxShare: number;
  singleTradeMedianMultiple: number;
  washMaxTopWalletsShare: number;
  washMaxRoundTripShare: number;
  washMaxTransactionsPerWallet: number;
  /** Without trade data, unique-buyer and wash checks cannot run; block instead of skipping? */
  requireTradeData: boolean;
  // --- penalties -----------------------------------------------------------
  /** Liquidity falling by at least this percent in the window costs points. */
  liquidityDropPct: number;
  // --- signal ----------------------------------------------------------------
  minScore: number;
  minTriggeredRules: number;
  minConfidence: number;
  weights: Record<RuleId, number>;
}

export const DEFAULT_THRESHOLDS: MomentumThresholds = {
  primaryWindow: '5m',
  volumeSpikePct: 300,
  txSpikePct: 200,
  buyerGrowthPct: 100,
  holderGrowthPct: 20,
  liquidityGrowthPct: 20,
  marketCapChangePct: 20,
  buyShare: 0.6,
  volumeAccelerationPct: 100,
  txAccelerationPct: 100,
  fullStrengthFactor: 2,
  buyShareFull: 0.8,
  minLiquidityUsd: 20_000,
  minHolders: 50,
  minWindowVolumeUsd: 5_000,
  minWindowTransactions: 30,
  minUniqueBuyers: 15,
  singleTradeMaxShare: 0.4,
  singleTradeMedianMultiple: 10,
  washMaxTopWalletsShare: 0.5,
  washMaxRoundTripShare: 0.3,
  washMaxTransactionsPerWallet: 5,
  requireTradeData: false,
  liquidityDropPct: 20,
  minScore: 60,
  minTriggeredRules: 3,
  minConfidence: 0.7,
  weights: {
    volume_spike: 20,
    tx_spike: 15,
    buyer_growth: 15,
    holder_growth: 10,
    liquidity_growth: 10,
    market_cap_change: 10,
    buy_pressure: 10,
    volume_acceleration: 5,
    tx_acceleration: 5,
  },
};

const positive = z.number().positive();
const share = z.number().min(0).max(1);

/** Every field except `weights`, shared by the full schema and the override schema. */
const fields = {
  primaryWindow: z.enum(WINDOWS),
  volumeSpikePct: positive,
  txSpikePct: positive,
  buyerGrowthPct: positive,
  holderGrowthPct: positive,
  liquidityGrowthPct: positive,
  marketCapChangePct: positive,
  buyShare: z.number().min(0.5).max(1),
  volumeAccelerationPct: positive,
  txAccelerationPct: positive,
  fullStrengthFactor: z.number().min(1.1),
  buyShareFull: share,
  minLiquidityUsd: z.number().min(0),
  minHolders: z.number().int().min(0),
  minWindowVolumeUsd: z.number().min(0),
  minWindowTransactions: z.number().int().min(0),
  minUniqueBuyers: z.number().int().min(0),
  singleTradeMaxShare: z.number().gt(0).max(1),
  singleTradeMedianMultiple: z.number().min(1),
  washMaxTopWalletsShare: share,
  washMaxRoundTripShare: share,
  washMaxTransactionsPerWallet: positive,
  requireTradeData: z.boolean(),
  liquidityDropPct: positive,
  minScore: z.number().min(0).max(100),
  minTriggeredRules: z.number().int().min(1).max(RULE_IDS.length),
  minConfidence: share,
};
const weight = z.number().min(0).max(100);
const weightsSchema = z
  .object(Object.fromEntries(RULE_IDS.map((id) => [id, weight])) as Record<RuleId, typeof weight>)
  .strict();

export const thresholdsSchema = z
  .object({ ...fields, weights: weightsSchema })
  .strict()
  .refine((t) => t.buyShareFull > t.buyShare, { message: 'buyShareFull must exceed buyShare' })
  .refine((t) => Object.values(t.weights).some((w) => w > 0), { message: 'at least one rule weight must be positive' });

export type ThresholdOverride = Partial<Omit<MomentumThresholds, 'weights'>> & { weights?: Partial<Record<RuleId, number>> };
export type ThresholdOverrides = Record<string, ThresholdOverride>;

const overrideSchema = z.record(
  z.string().regex(/^[a-z0-9]+(?::[a-z0-9_.-]+)?$/, 'keys are "<chain>" or "<chain>:<tokenType>"'),
  z.object({ ...fields, weights: weightsSchema.partial() }).partial().strict(),
);

/** Parses MOMENTUM_THRESHOLD_OVERRIDES. Throws with a readable message on invalid input. */
export function parseOverrides(json: string): ThresholdOverrides {
  let raw: unknown;
  try {
    raw = JSON.parse(json);
  } catch {
    throw new Error('must be valid JSON');
  }
  const parsed = overrideSchema.safeParse(raw);
  if (!parsed.success) {
    throw new Error(parsed.error.issues.map((i) => `${i.path.join('.') || '(root)'}: ${i.message}`).join('; '));
  }
  return parsed.data as ThresholdOverrides;
}

export interface ThresholdConfig {
  defaults: MomentumThresholds;
  overrides: ThresholdOverrides;
}

export interface ResolvedThresholds {
  thresholds: MomentumThresholds;
  /** Which layers were applied, e.g. ["default", "solana", "solana:pumpswap"]. */
  profile: string[];
}

export function resolveThresholds(cfg: ThresholdConfig, chain: string, tokenType: string | null): ResolvedThresholds {
  let merged: MomentumThresholds = { ...cfg.defaults, weights: { ...cfg.defaults.weights } };
  const profile = ['default'];
  const keys = [chain, ...(tokenType ? [`${chain}:${tokenType.toLowerCase()}`] : [])];
  for (const key of keys) {
    const o = cfg.overrides[key];
    if (!o) continue;
    const { weights, ...rest } = o;
    merged = { ...merged, ...rest, weights: { ...merged.weights, ...weights } };
    profile.push(key);
  }
  const valid = thresholdsSchema.safeParse(merged);
  if (!valid.success) {
    throw new Error(`thresholds for ${profile.join(' > ')} are invalid: ${valid.error.issues[0]?.message}`);
  }
  return { thresholds: merged, profile };
}
