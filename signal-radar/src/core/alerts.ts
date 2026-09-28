import type { SafetyVerdict } from './safety.js';
import type { Chain } from './types.js';

export const TOKEN_ALERT_TYPES = ['NEW_TOKEN', 'MOMENTUM'] as const;
export const WALLET_ALERT_TYPES = ['WHALE', 'TRACKED_WALLET', 'TRACKED_CLUSTER'] as const;
export const ALERT_TYPES = [...TOKEN_ALERT_TYPES, ...WALLET_ALERT_TYPES] as const;
export type TokenAlertType = (typeof TOKEN_ALERT_TYPES)[number];
export type WalletAlertType = (typeof WALLET_ALERT_TYPES)[number];
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
export type AlertPayload = TokenAlertPayload | WalletAlertPayload;

export function isWalletAlert(p: AlertPayload): p is WalletAlertPayload {
  return (WALLET_ALERT_TYPES as readonly string[]).includes(p.type);
}

export interface TokenAlertPayload {
  type: TokenAlertType;
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

/**
 * Wallet activity alert (Wallet Intelligence). WHALE is about trade SIZE
 * only; TRACKED_WALLET / TRACKED_CLUSTER are about wallets on the watchlist.
 * Statistics are historical and measured over a stated window: they describe
 * the past and are no prediction.
 */
export interface WalletAlertPayload {
  type: WalletAlertType;
  chain: Chain;
  tokenAddress: string;
  /** Creator-chosen, untrusted text. Providers must sanitise before display. */
  symbol: string | null;
  name: string | null;
  decidedAt: string;
  /** The triggering swap (WHALE / TRACKED_WALLET). */
  trade: {
    wallet: string;
    side: 'buy' | 'sell';
    valueUsd: number;
    amountRaw: string;
    signature: string;
    blockTime: string;
    source: string;
  } | null;
  /** Why the trade counts as whale activity; null when it does not. */
  whaleReason: string | null;
  /** Why the wallet is on the watchlist and its measured history. */
  tracked: {
    source: 'manual' | 'criteria';
    reason: string;
    stats: WalletStatsSummary | null;
  } | null;
  /** The wallet's position in this token after the trade, from the events the radar stored. */
  exposure: {
    qtyRaw: string;
    costUsd: number | null;
    valueUsd: number | null;
    /** False when part of the position arrived by transfer or before the stored history. */
    complete: boolean;
  } | null;
  cluster: {
    wallets: { wallet: string; buyUsd: number; sharePct: number }[];
    totalUsd: number;
    /** Sum after the per-wallet share cap: no single wallet dominates this number. */
    cappedTotalUsd: number;
    maxWalletShare: number;
    windowMinutes: number;
    firstAt: string;
    lastAt: string;
  } | null;
  market: { source: string; observedAt: string; priceUsd: number | null; liquidityUsd: number | null; marketCapUsd: number | null } | null;
  notes: string[];
}

export interface WalletStatsSummary {
  classification: string;
  criteriaVersion: string;
  windowDays: number;
  reliableClosedPositions: number;
  winRate: number | null;
  winRateLowerBound: number | null;
  avgReturnPct: number | null;
  realizedPnlUsd: number;
  avgHoldingSec: number | null;
  computedAt: string;
}

export interface PayloadInput {
  type: TokenAlertType;
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
export function buildAlertPayload(i: PayloadInput): TokenAlertPayload {
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
