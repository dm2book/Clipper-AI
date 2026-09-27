/**
 * Per-window metrics for one token: the current window compared with the
 * previous, non-overlapping window of the same length.
 */
import type { HolderSnapshot } from '../core/holders.js';
import { capitalisation, type MarketSnapshot } from '../core/marketSnapshot.js';
import { flowSeries, type FlowOptions, type FlowSource, type TradeData, type WashMetrics } from './flow.js';
import {
  WINDOWS,
  WINDOW_MS,
  acceleration,
  buySellRatio,
  buyShare,
  change,
  holderToleranceMs,
  nearest,
  snapshotToleranceMs,
  type Change,
  type WindowId,
} from './indicators.js';

/** Floors for percent changes, so tiny bases cannot produce absurd percentages. */
export const PCT_FLOORS = {
  volumeUsd: 500,
  transactions: 5,
  wallets: 3,
  holders: 10,
  liquidityUsd: 1_000,
  marketCapUsd: 1_000,
  priceUsd: 0,
} as const;

export interface TokenSeries {
  chain: string;
  address: string;
  /** DEX of the main pool; selects per-type thresholds. */
  tokenType: string | null;
  /** Token age at evaluation time (on-chain creation), if known. */
  tokenAgeMs: number | null;
  /** Market snapshots, any order. */
  snapshots: readonly MarketSnapshot[];
  /** Holder snapshots, any order. */
  holders: readonly HolderSnapshot[];
  /** Per-trade data; null until a trade stream is connected. */
  trades: TradeData | null;
}

export interface WindowMetrics {
  window: WindowId;
  flowSource: FlowSource | null;
  /** Span (s) that was scaled to the window length, for provider_h24_delta. */
  flowNormalizedFromSec: number | null;
  volumeUsd: Change & { acceleration: number | null; rawCurrent: number | null; excludedTrades: number; excludedVolumeUsd: number };
  transactions: Change & { acceleration: number | null };
  buys: Change;
  sells: Change;
  buySellRatio: number | null;
  buyShare: number | null;
  uniqueBuyers: Change;
  uniqueSellers: Change;
  priceUsd: Change;
  marketCapUsd: Change & { basis: 'market_cap' | 'fdv' | null };
  liquidityUsd: Change;
  holders: Change;
  largestTradeShare: number | null;
  wash: WashMetrics | null;
}

const snapAt = (s: MarketSnapshot) => s.observedAt.getTime();
const holderAt = (h: HolderSnapshot) => h.observedAt.getTime();

function latestAtOrBefore<T>(points: readonly T[], at: (p: T) => number, nowMs: number): T | null {
  let best: T | null = null;
  for (const p of points) if (at(p) <= nowMs && (!best || at(p) > at(best))) best = p;
  return best;
}

export function computeWindowMetrics(series: TokenSeries, window: WindowId, nowMs: number, opts: FlowOptions): WindowMetrics {
  const w = WINDOW_MS[window];
  const { source, intervals } = flowSeries(series, window, nowMs, opts);
  const [cur, prev, prev2] = intervals;

  // State metrics: the latest value vs the value one window earlier.
  const now = latestAtOrBefore(series.snapshots, snapAt, nowMs);
  const then = nearest(series.snapshots, snapAt, nowMs - w, snapshotToleranceMs(window));
  const nowCap = now ? capitalisation(now) : null;
  const thenCap = then ? capitalisation(then) : null;
  // Never compare market cap with FDV.
  const sameBasis = nowCap && thenCap && nowCap.basis === thenCap.basis;
  const holdersNow = latestAtOrBefore(series.holders, holderAt, nowMs);
  const holdersThen = nearest(series.holders, holderAt, nowMs - w, holderToleranceMs(window));
  const usableHoldersThen = holdersThen && holdersNow && holdersThen !== holdersNow ? holdersThen : null;

  const val = <K extends 'volumeUsd' | 'transactions' | 'buys' | 'sells'>(f: typeof cur, k: K) => (f ? f[k] : null);
  const wallets = (f: typeof cur, k: 'uniqueBuyers' | 'uniqueSellers') => (f ? f[k] : null);

  return {
    window,
    flowSource: source,
    flowNormalizedFromSec: cur?.normalizedFromSec ?? null,
    volumeUsd: {
      ...change(val(cur, 'volumeUsd'), val(prev, 'volumeUsd'), PCT_FLOORS.volumeUsd),
      acceleration: acceleration(val(cur, 'volumeUsd'), val(prev, 'volumeUsd'), val(prev2, 'volumeUsd'), PCT_FLOORS.volumeUsd),
      rawCurrent: cur?.rawVolumeUsd ?? null,
      excludedTrades: cur?.excludedTrades ?? 0,
      excludedVolumeUsd: cur?.excludedVolumeUsd ?? 0,
    },
    transactions: {
      ...change(val(cur, 'transactions'), val(prev, 'transactions'), PCT_FLOORS.transactions),
      acceleration: acceleration(
        val(cur, 'transactions'),
        val(prev, 'transactions'),
        val(prev2, 'transactions'),
        PCT_FLOORS.transactions,
      ),
    },
    buys: change(val(cur, 'buys'), val(prev, 'buys'), PCT_FLOORS.transactions),
    sells: change(val(cur, 'sells'), val(prev, 'sells'), PCT_FLOORS.transactions),
    buySellRatio: buySellRatio(val(cur, 'buys'), val(cur, 'sells')),
    buyShare: buyShare(val(cur, 'buys'), val(cur, 'sells')),
    uniqueBuyers: change(wallets(cur, 'uniqueBuyers'), wallets(prev, 'uniqueBuyers'), PCT_FLOORS.wallets),
    uniqueSellers: change(wallets(cur, 'uniqueSellers'), wallets(prev, 'uniqueSellers'), PCT_FLOORS.wallets),
    priceUsd: change(now?.priceUsd ?? null, then?.priceUsd ?? null, PCT_FLOORS.priceUsd),
    marketCapUsd: {
      ...change(nowCap?.usd ?? null, sameBasis ? thenCap.usd : null, PCT_FLOORS.marketCapUsd),
      basis: nowCap?.basis ?? null,
    },
    liquidityUsd: change(now?.liquidityUsd ?? null, then?.liquidityUsd ?? null, PCT_FLOORS.liquidityUsd),
    holders: change(holdersNow?.holderCount ?? null, usableHoldersThen?.holderCount ?? null, PCT_FLOORS.holders),
    largestTradeShare: cur?.largestTradeShare ?? null,
    wash: cur?.wash ?? null,
  };
}

export function computeAllWindows(series: TokenSeries, nowMs: number, opts: FlowOptions): Record<WindowId, WindowMetrics> {
  return Object.fromEntries(WINDOWS.map((w) => [w, computeWindowMetrics(series, w, nowMs, opts)])) as Record<
    WindowId,
    WindowMetrics
  >;
}
