/**
 * Flow metrics (volume, buys, sells, transactions, unique wallets) for one
 * time interval, from the best source available:
 *
 *  1. trades              exact per-trade data: counts, unique buyers/sellers,
 *                         extreme-trade filtering and wash-trading metrics
 *  2. provider_h24_delta  difference of the provider's rolling 24h totals
 *                         between two snapshots. Exact for tokens younger than
 *                         24h (nothing has left the 24h window yet); the span
 *                         between the two snapshots is normalised to the window
 *  3. provider_rolling    the provider's own rolling window (5m and 1h only)
 *
 * Unique wallets and wash-trading metrics exist only with trade data.
 */
import type { MarketSnapshot } from '../core/marketSnapshot.js';
import { WINDOW_MS, median, nearest, snapshotToleranceMs, type WindowId } from './indicators.js';

export type FlowSource = 'trades' | 'provider_h24_delta' | 'provider_rolling';

export interface Trade {
  signature: string;
  at: Date;
  wallet: string;
  side: 'buy' | 'sell';
  valueUsd: number;
}

/** Trades plus the moment from which the trade feed is complete. */
export interface TradeData {
  items: readonly Trade[];
  coverageFrom: Date;
}

export interface WashMetrics {
  /** Volume share of the three most active wallets (0–1). */
  topWalletsShare: number;
  /** Volume share of wallets that bought and sold roughly the same amount (0–1). */
  roundTripShare: number;
  transactionsPerWallet: number;
  uniqueWallets: number;
}

export interface Flow {
  source: FlowSource;
  start: Date;
  end: Date;
  /** Volume after excluding extreme single trades. */
  volumeUsd: number;
  rawVolumeUsd: number;
  excludedTrades: number;
  excludedVolumeUsd: number;
  buys: number;
  sells: number;
  transactions: number;
  uniqueBuyers: number | null;
  uniqueSellers: number | null;
  largestTradeShare: number | null;
  wash: WashMetrics | null;
  /** For provider_h24_delta: the real span (s) that was scaled to the window length. */
  normalizedFromSec: number | null;
}

export interface FlowOptions {
  /** A trade larger than this share of the window's volume… */
  singleTradeMaxShare: number;
  /** …and larger than this multiple of the median trade is excluded from volume. */
  singleTradeMedianMultiple: number;
}

const DAY_MS = 24 * 3_600_000;
/** A wallet "round-trips" when its buys and sells differ by at most this fraction. */
const ROUND_TRIP_TOLERANCE = 0.2;

export function flowFromTrades(trades: readonly Trade[], startMs: number, endMs: number, opts: FlowOptions): Flow {
  const window = trades.filter((t) => t.at.getTime() >= startMs && t.at.getTime() < endMs && t.valueUsd >= 0);
  const raw = window.reduce((a, t) => a + t.valueUsd, 0);
  const med = median(window.map((t) => t.valueUsd)) ?? 0;
  const isExtreme = (t: Trade) =>
    raw > 0 && t.valueUsd > opts.singleTradeMaxShare * raw && t.valueUsd > opts.singleTradeMedianMultiple * med;
  const extreme = window.filter(isExtreme);
  const excludedVolume = extreme.reduce((a, t) => a + t.valueUsd, 0);

  const buyers = new Set<string>();
  const sellers = new Set<string>();
  const perWallet = new Map<string, { buy: number; sell: number }>();
  let buys = 0;
  let sells = 0;
  for (const t of window) {
    if (t.side === 'buy') {
      buys++;
      buyers.add(t.wallet);
    } else {
      sells++;
      sellers.add(t.wallet);
    }
    const w = perWallet.get(t.wallet) ?? { buy: 0, sell: 0 };
    w[t.side] += t.valueUsd;
    perWallet.set(t.wallet, w);
  }

  let wash: WashMetrics | null = null;
  if (window.length && raw > 0) {
    const volumes = [...perWallet.values()].map((w) => w.buy + w.sell).sort((a, b) => b - a);
    const top3 = volumes.slice(0, 3).reduce((a, v) => a + v, 0);
    const roundTrip = [...perWallet.values()]
      .filter((w) => w.buy > 0 && w.sell > 0 && Math.abs(w.buy - w.sell) <= ROUND_TRIP_TOLERANCE * Math.max(w.buy, w.sell))
      .reduce((a, w) => a + w.buy + w.sell, 0);
    wash = {
      topWalletsShare: top3 / raw,
      roundTripShare: roundTrip / raw,
      transactionsPerWallet: window.length / perWallet.size,
      uniqueWallets: perWallet.size,
    };
  }

  return {
    source: 'trades',
    start: new Date(startMs),
    end: new Date(endMs),
    volumeUsd: raw - excludedVolume,
    rawVolumeUsd: raw,
    excludedTrades: extreme.length,
    excludedVolumeUsd: excludedVolume,
    buys,
    sells,
    transactions: window.length,
    uniqueBuyers: buyers.size,
    uniqueSellers: sellers.size,
    largestTradeShare: raw > 0 ? Math.max(...window.map((t) => t.valueUsd)) / raw : null,
    wash,
    normalizedFromSec: null,
  };
}

const at = (s: MarketSnapshot) => s.observedAt.getTime();

/**
 * Window flow as the difference of rolling 24h totals. Only valid while the
 * token is younger than 24h at the end of the interval; otherwise trades that
 * leave the 24h window would be subtracted and the result would be wrong.
 */
export function flowFromH24Delta(
  snapshots: readonly MarketSnapshot[],
  window: WindowId,
  startMs: number,
  endMs: number,
  tokenAgeAtEndMs: number | null,
): Flow | null {
  if (tokenAgeAtEndMs === null || tokenAgeAtEndMs >= DAY_MS) return null;
  const tol = snapshotToleranceMs(window);
  const end = nearest(snapshots, at, endMs, tol);
  const start = nearest(snapshots, at, startMs, tol);
  if (!end || !start || end === start) return null;
  const spanMs = at(end) - at(start);
  if (spanMs <= 0) return null;
  const dv = diff(end.volumeUsd.h24, start.volumeUsd.h24);
  const db = diff(end.txns.h24.buys, start.txns.h24.buys);
  const ds = diff(end.txns.h24.sells, start.txns.h24.sells);
  // A shrinking cumulative total means the provider data is inconsistent: do not use it.
  if (dv === null || db === null || ds === null || dv < 0 || db < 0 || ds < 0) return null;
  const scale = WINDOW_MS[window] / spanMs;
  const buys = Math.round(db * scale);
  const sells = Math.round(ds * scale);
  return {
    source: 'provider_h24_delta',
    start: new Date(startMs),
    end: new Date(endMs),
    volumeUsd: dv * scale,
    rawVolumeUsd: dv * scale,
    excludedTrades: 0,
    excludedVolumeUsd: 0,
    buys,
    sells,
    transactions: buys + sells,
    uniqueBuyers: null,
    uniqueSellers: null,
    largestTradeShare: null,
    wash: null,
    normalizedFromSec: Math.round(spanMs / 1000),
  };
}

/** The provider's own rolling window; DexScreener reports m5 and h1. */
export function flowFromRolling(snapshots: readonly MarketSnapshot[], window: WindowId, endMs: number): Flow | null {
  const key = window === '5m' ? 'm5' : window === '1h' ? 'h1' : null;
  if (!key) return null;
  const s = nearest(snapshots, at, endMs, snapshotToleranceMs(window));
  if (!s) return null;
  const volume = s.volumeUsd[key];
  const { buys, sells } = s.txns[key];
  if (volume === null || buys === null || sells === null) return null;
  return {
    source: 'provider_rolling',
    start: new Date(endMs - WINDOW_MS[window]),
    end: new Date(endMs),
    volumeUsd: volume,
    rawVolumeUsd: volume,
    excludedTrades: 0,
    excludedVolumeUsd: 0,
    buys,
    sells,
    transactions: buys + sells,
    uniqueBuyers: null,
    uniqueSellers: null,
    largestTradeShare: null,
    wash: null,
    normalizedFromSec: null,
  };
}

function diff(a: number | null, b: number | null): number | null {
  return a === null || b === null ? null : a - b;
}

export interface FlowSeriesInput {
  snapshots: readonly MarketSnapshot[];
  trades: TradeData | null;
  /** Token age at `nowMs`, if known. */
  tokenAgeMs: number | null;
}

/**
 * Flow for the current (0), previous (1) and before-previous (2) interval of
 * a window, all from ONE source: comparing a trade count with a provider
 * count would measure the difference between sources, not activity. The
 * first source that covers both the current and the previous interval wins.
 */
export function flowSeries(
  input: FlowSeriesInput,
  window: WindowId,
  nowMs: number,
  opts: FlowOptions,
): { source: FlowSource | null; intervals: [Flow | null, Flow | null, Flow | null] } {
  const w = WINDOW_MS[window];
  const bounds = [0, 1, 2].map((i) => ({ start: nowMs - (i + 1) * w, end: nowMs - i * w }));
  const attempts: [FlowSource, (b: { start: number; end: number }, i: number) => Flow | null][] = [
    [
      'trades',
      (b) =>
        input.trades && input.trades.coverageFrom.getTime() <= b.start
          ? flowFromTrades(input.trades.items, b.start, b.end, opts)
          : null,
    ],
    [
      'provider_h24_delta',
      (b, i) =>
        flowFromH24Delta(input.snapshots, window, b.start, b.end, input.tokenAgeMs === null ? null : input.tokenAgeMs - i * w),
    ],
    ['provider_rolling', (b) => flowFromRolling(input.snapshots, window, b.end)],
  ];
  for (const [source, compute] of attempts) {
    const current = compute(bounds[0]!, 0);
    const previous = compute(bounds[1]!, 1);
    if (current && previous) return { source, intervals: [current, previous, compute(bounds[2]!, 2)] };
  }
  return { source: null, intervals: [null, null, null] };
}
