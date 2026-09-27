/**
 * TEST-ONLY builders for the Momentum Detection Engine. All data is
 * synthetic and constructed to exercise specific indicator behaviour.
 */
import type { HolderSnapshot } from '../../src/core/holders.js';
import type { MarketSnapshot } from '../../src/core/marketSnapshot.js';
import type { Trade } from '../../src/momentum/flow.js';
import type { TokenSeries } from '../../src/momentum/metrics.js';
import { DEFAULT_THRESHOLDS, type MomentumThresholds } from '../../src/momentum/thresholds.js';
import { makeHolders, makeSnapshot, type SnapshotOverrides } from './factories.js';

export const NOW = Date.parse('2026-01-01T12:00:00Z');
export const MIN = 60_000;
export const ago = (ms: number) => new Date(NOW - ms);

export function snap(msAgo: number, o: SnapshotOverrides = {}): MarketSnapshot {
  return makeSnapshot({ observedAt: ago(msAgo), ...o });
}

export function holders(msAgo: number, count: number): HolderSnapshot {
  return makeHolders({ observedAt: ago(msAgo), holderCount: count });
}

let seq = 0;
export function trade(msAgo: number, wallet: string, side: 'buy' | 'sell', valueUsd: number): Trade {
  return { signature: `sig${++seq}`, at: ago(msAgo), wallet, side, valueUsd };
}

/** `count` trades spread evenly inside (end, end + span] ms ago, one wallet per trade unless given. */
export function tradesIn(
  endAgoMs: number,
  spanMs: number,
  count: number,
  side: 'buy' | 'sell',
  totalUsd: number,
  wallet: (i: number) => string = (i) => `${side}-${endAgoMs}-${i}`,
): Trade[] {
  return Array.from({ length: count }, (_, i) =>
    trade(endAgoMs + ((i + 0.5) * spanMs) / count, wallet(i), side, totalUsd / count),
  );
}

export function series(o: Partial<TokenSeries> = {}): TokenSeries {
  return {
    chain: 'solana',
    address: 'Token11111111111111111111111111111111111111',
    tokenType: 'raydium',
    tokenAgeMs: 2 * 3_600_000,
    snapshots: [],
    holders: [],
    trades: null,
    ...o,
  };
}

export function thresholds(o: Partial<MomentumThresholds> = {}): MomentumThresholds {
  return { ...DEFAULT_THRESHOLDS, ...o, weights: { ...DEFAULT_THRESHOLDS.weights, ...o.weights } };
}

/**
 * Snapshots with cumulative 24h totals, as a provider reports them for a
 * token younger than 24h. `points` = [msAgo, volumeH24, buysH24, sellsH24].
 */
export function cumulativeSnapshots(
  points: [number, number, number, number][],
  extra: (msAgo: number) => SnapshotOverrides = () => ({}),
): MarketSnapshot[] {
  return points.map(([msAgo, vol, buys, sells]) =>
    snap(msAgo, { volumeUsd: { h24: vol }, txns: { h24: { buys, sells } }, ...extra(msAgo) }),
  );
}
