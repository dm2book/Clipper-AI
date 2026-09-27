import type { SafetyVerdict } from './safety.js';
import type { Chain } from './types.js';

export const ALERT_TYPES = ['NEW_TOKEN', 'MOMENTUM'] as const;
export type AlertType = (typeof ALERT_TYPES)[number];

export interface GateResult {
  name: string;
  passed: boolean;
  detail: string;
}

/**
 * Everything a notification needs, frozen at the moment the alert was
 * decided. Stored as JSON in `alerts.payload`, so a message can be re-sent or
 * audited later exactly as it was decided — and so notification providers
 * never query anything themselves.
 */
export interface AlertPayload {
  type: AlertType;
  chain: Chain;
  tokenAddress: string;
  /** Creator-chosen, untrusted text. Providers must sanitise before display. */
  symbol: string | null;
  name: string | null;
  decidedAt: string;
  age: { seconds: number; source: 'onchain' | 'provider' } | null;
  market: {
    source: string;
    observedAt: string;
    dexId: string | null;
    pairAddress: string | null;
    priceUsd: number | null;
    liquidityUsd: number | null;
    marketCapUsd: number | null;
    fdvUsd: number | null;
    volumeM5Usd: number | null;
    volumeH1Usd: number | null;
    buysM5: number | null;
    sellsM5: number | null;
  };
  holders: { count: number; capped: boolean; top10Pct: number | null; observedAt: string } | null;
  safety: {
    verdict: SafetyVerdict;
    reasons: string[];
    providers: { provider: string; verdict: SafetyVerdict }[];
  };
  score: ScoreSummary | null;
  gates: GateResult[];
}

/** The explainable score of the Momentum Detection Engine, as sent with an alert. */
export interface ScoreSummary {
  value: number;
  confidence: number;
  version: string;
  window: string;
  components: { type: string; label: string; points: number; weight: number; available: boolean; detail: string }[];
  penalties: { reason: string; points: number }[];
  /** One line per triggered rule, e.g. "volume +428% (5m: $52.8k vs $10.0k)". */
  reasons: string[];
  warnings: string[];
}

export function dedupeKey(type: AlertType, chain: Chain, address: string, suffix?: string | number): string {
  return suffix === undefined ? `${type}:${chain}:${address}` : `${type}:${chain}:${address}:${suffix}`;
}

export interface PayloadInput {
  type: AlertType;
  token: { chain: Chain; address: string; symbol: string | null; name: string | null };
  now: Date;
  age: { ms: number; source: 'onchain' | 'provider' } | null;
  snapshot: {
    source: string;
    observedAt: Date;
    dexId: string | null;
    pairAddress: string | null;
    priceUsd: number | null;
    liquidityUsd: number | null;
    marketCapUsd: number | null;
    fdvUsd: number | null;
    volumeUsd: { m5: number | null; h1: number | null };
    txns: { m5: { buys: number | null; sells: number | null } };
  };
  holders: { holderCount: number; holderCountCapped: boolean; top10Pct: number | null; observedAt: Date } | null;
  safety: { verdict: SafetyVerdict; reasons: string[]; providers: { provider: string; verdict: SafetyVerdict }[] };
  score: ScoreSummary | null;
  gates: GateResult[];
}

/** Freezes the decision context into the stored/sent payload. */
export function buildAlertPayload(i: PayloadInput): AlertPayload {
  return {
    type: i.type,
    chain: i.token.chain,
    tokenAddress: i.token.address,
    symbol: i.token.symbol,
    name: i.token.name,
    decidedAt: i.now.toISOString(),
    age: i.age ? { seconds: Math.round(i.age.ms / 1000), source: i.age.source } : null,
    market: {
      source: i.snapshot.source,
      observedAt: i.snapshot.observedAt.toISOString(),
      dexId: i.snapshot.dexId,
      pairAddress: i.snapshot.pairAddress,
      priceUsd: i.snapshot.priceUsd,
      liquidityUsd: i.snapshot.liquidityUsd,
      marketCapUsd: i.snapshot.marketCapUsd,
      fdvUsd: i.snapshot.fdvUsd,
      volumeM5Usd: i.snapshot.volumeUsd.m5,
      volumeH1Usd: i.snapshot.volumeUsd.h1,
      buysM5: i.snapshot.txns.m5.buys,
      sellsM5: i.snapshot.txns.m5.sells,
    },
    holders: i.holders
      ? {
          count: i.holders.holderCount,
          capped: i.holders.holderCountCapped,
          top10Pct: i.holders.top10Pct,
          observedAt: i.holders.observedAt.toISOString(),
        }
      : null,
    safety: {
      verdict: i.safety.verdict,
      reasons: i.safety.reasons,
      providers: i.safety.providers.map((p) => ({ provider: p.provider, verdict: p.verdict })),
    },
    score: i.score,
    gates: i.gates,
  };
}
