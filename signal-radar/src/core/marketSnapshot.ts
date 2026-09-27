import type { Chain } from './types.js';

export interface WindowCounts {
  buys: number | null;
  sells: number | null;
}

/**
 * One observation of a token's market, normalised across providers. Every
 * field is nullable: a provider that does not report something must say so,
 * never fill in a zero.
 */
export interface MarketSnapshot {
  chain: Chain;
  tokenAddress: string;
  pairAddress: string | null;
  dexId: string | null;
  quoteAddress: string | null;
  source: string;
  observedAt: Date;
  priceUsd: number | null;
  liquidityUsd: number | null;
  fdvUsd: number | null;
  marketCapUsd: number | null;
  volumeUsd: { m5: number | null; h1: number | null; h6: number | null; h24: number | null };
  txns: { m5: WindowCounts; h1: WindowCounts; h24: WindowCounts };
  priceChangePct: { m5: number | null; h1: number | null; h24: number | null };
  pairCreatedAt: Date | null;
  /** Token metadata as the provider reports it; untrusted, creator-chosen text. */
  symbol: string | null;
  name: string | null;
}

export function txCount(w: WindowCounts): number | null {
  if (w.buys === null || w.sells === null) return null;
  return w.buys + w.sells;
}

export function snapshotAgeMs(s: Pick<MarketSnapshot, 'observedAt'>, now: Date): number {
  return now.getTime() - s.observedAt.getTime();
}

/**
 * The snapshot closest to `target` among those observed within
 * [target - earlierMs, target + laterMs]. Used to find the "previous window"
 * for a comparison without accidentally comparing overlapping windows.
 */
export function snapshotNear<T extends Pick<MarketSnapshot, 'observedAt'>>(
  history: readonly T[],
  target: Date,
  earlierMs: number,
  laterMs: number,
): T | null {
  const t = target.getTime();
  let best: T | null = null;
  let bestDist = Infinity;
  for (const s of history) {
    const at = s.observedAt.getTime();
    if (at < t - earlierMs || at > t + laterMs) continue;
    const dist = Math.abs(at - t);
    if (dist < bestDist) {
      best = s;
      bestDist = dist;
    }
  }
  return best;
}

/** Market cap when the provider reports it, otherwise FDV — labelled, never silently swapped. */
export function capitalisation(s: MarketSnapshot): { usd: number; basis: 'market_cap' | 'fdv' } | null {
  if (s.marketCapUsd !== null) return { usd: s.marketCapUsd, basis: 'market_cap' };
  if (s.fdvUsd !== null) return { usd: s.fdvUsd, basis: 'fdv' };
  return null;
}
