import type { TierSettings } from '../config/env.js';
import type { Chain } from './types.js';

export const TIERS = ['HOT', 'WARM', 'COOL', 'ARCHIVED'] as const;
export type TokenTier = (typeof TIERS)[number];
export type ActiveTier = Exclude<TokenTier, 'ARCHIVED'>;

export interface Token {
  chain: Chain;
  address: string;
  symbol: string | null;
  name: string | null;
  decimals: number | null;
  /** Raw integer supply as a decimal string (can exceed 2^53). */
  supply: string | null;
  tokenProgram: string | null;
  /** When the pool/token was created on chain. The only source of "token age". */
  createdAtChain: Date | null;
  detectedAt: Date;
  detectionSource: string;
  detectionRef: string | null;
  tier: TokenTier;
  nextSnapshotAt: Date;
  nextEnrichAt: Date;
  snapshotMisses: number;
  lastSnapshotAt: Date | null;
  archivedAt: Date | null;
  archivedReason: string | null;
}

/** What a discovery provider reports about a new token. */
export interface DiscoveredToken {
  chain: Chain;
  address: string;
  createdAtChain: Date | null;
  source: string;
  reference: string | null;
}

export type AgeSource = 'onchain' | 'provider';

export interface TokenAge {
  ms: number;
  source: AgeSource;
}

/**
 * Token age for alert gates. On-chain creation time is authoritative; the
 * market-data provider's pair creation time is only a labelled fallback.
 * Without either, the age is unknown and age gates fail.
 */
export function tokenAge(
  token: Pick<Token, 'createdAtChain'>,
  pairCreatedAt: Date | null,
  now: Date,
): TokenAge | null {
  if (token.createdAtChain) return { ms: now.getTime() - token.createdAtChain.getTime(), source: 'onchain' };
  if (pairCreatedAt) return { ms: now.getTime() - pairCreatedAt.getTime(), source: 'provider' };
  return null;
}

/** Tiering only needs a rough clock, so it falls back to detection time. */
export function tierForAge(ageMs: number, cfg: Pick<TierSettings, 'hotMinutes' | 'warmMinutes'>): ActiveTier {
  const minutes = ageMs / 60_000;
  if (minutes < cfg.hotMinutes) return 'HOT';
  if (minutes < cfg.warmMinutes) return 'WARM';
  return 'COOL';
}

export function activeTier(tier: TokenTier): ActiveTier {
  return tier === 'ARCHIVED' ? 'COOL' : tier;
}

/**
 * Seconds until the next market snapshot. Tokens without meaningful
 * liquidity are polled at the COOL rate whatever their age: that is where
 * most launches end up, and it keeps the provider budget for the rest.
 */
export function snapshotIntervalSec(
  tier: TokenTier,
  liquidityUsd: number | null,
  cfg: Pick<TierSettings, 'snapshotIntervalSec' | 'lowLiquidityUsd'>,
): number {
  if (liquidityUsd !== null && liquidityUsd < cfg.lowLiquidityUsd) return cfg.snapshotIntervalSec.COOL;
  return cfg.snapshotIntervalSec[activeTier(tier)];
}

/** Market-data indexers lag behind brand-new pools: back off 20s, 40s, 80s, then 120s. */
export function notIndexedBackoffSec(misses: number): number {
  return Math.min(20 * 2 ** Math.max(0, misses - 1), 120);
}
