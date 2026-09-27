/**
 * Signal detectors (docs/ARCHITECTURE.md §G.2). Each one is a pure function
 * that turns snapshots into a measurement with explicit value, baseline and
 * window. A measurement says nothing about future prices; it only records
 * that something measurable happened.
 *
 * Windows are non-overlapping: "previous 5 minutes" means a snapshot taken
 * ~5 minutes before the current one, not a rolling window that includes it.
 */
import type { HolderSnapshot } from './holders.js';
import { capitalisation, snapshotNear, txCount, type MarketSnapshot } from './marketSnapshot.js';

export const DETECTOR_VERSION = 'signals-v1';

export const SIGNAL_TYPES = [
  'volume_spike',
  'tx_growth',
  'liquidity_growth',
  'mcap_change',
  'buy_count_ratio',
  'holder_growth',
] as const;
export type SignalType = (typeof SIGNAL_TYPES)[number];

export interface Measurement {
  type: SignalType;
  /** Data existed to compute this at all. */
  available: boolean;
  /** Large enough in absolute terms to be meaningful (e.g. not "10× of $5"). */
  qualifies: boolean;
  /** The number that is scored (a ratio, a percentage, a fraction or a delta). */
  metric: number | null;
  value: number | null;
  baseline: number | null;
  window: string;
  detail: string;
  evidence: Record<string, unknown>;
}

export interface SignalContext {
  now: Date;
  current: MarketSnapshot;
  /** Earlier snapshots of the same token, any order. */
  history: readonly MarketSnapshot[];
  /** Holder snapshots, any order (newest is used as "current"). */
  holders: readonly HolderSnapshot[];
  /** On-chain (or labelled provider) age; null if unknown. */
  tokenAgeMs: number | null;
}

/** Absolute minimums below which a relative change is noise. */
export const MINIMUMS = {
  volumeSpike: { currentUsd: 5_000, baselineFloorUsd: 500 },
  txGrowth: { currentTx: 30, baselineFloorTx: 10 },
  liquidityGrowth: { absoluteUsd: 5_000 },
  buyCountRatio: { currentTx: 30 },
  holderGrowth: { minHolders: 50 },
} as const;

const MIN = 60_000;
// A "5 minutes ago" snapshot may be 4.5–7 minutes old; "15 minutes ago" 12–16.
const WINDOW_5M = { offsetMs: 5 * MIN, earlierMs: 2 * MIN, laterMs: 30_000 };
const WINDOW_15M = { offsetMs: 15 * MIN, earlierMs: 3 * MIN, laterMs: 1 * MIN };

function previous<T extends { observedAt: Date }>(
  history: readonly T[],
  current: { observedAt: Date },
  w: { offsetMs: number; earlierMs: number; laterMs: number },
): T | null {
  return snapshotNear(history, new Date(current.observedAt.getTime() - w.offsetMs), w.earlierMs, w.laterMs);
}

const fmtUsd = (v: number) => (v >= 10_000 ? `$${(v / 1_000).toFixed(1)}k` : `$${Math.round(v)}`);
const fmtX = (v: number) => `${v.toFixed(1)}×`;
const fmtPct = (v: number) => `${v >= 0 ? '+' : ''}${v.toFixed(1)}%`;

function unavailable(type: SignalType, window: string, detail: string): Measurement {
  return { type, available: false, qualifies: false, metric: null, value: null, baseline: null, window, detail, evidence: {} };
}

/** 5-minute volume against the previous 5-minute window (or the 1h average as fallback). */
export function measureVolumeSpike(ctx: SignalContext): Measurement {
  const type = 'volume_spike';
  const current = ctx.current.volumeUsd.m5;
  if (current === null) return unavailable(type, '5m', 'geen 5m-volume');
  const prev = previous(ctx.history, ctx.current, WINDOW_5M);
  let baseline: number | null = prev?.volumeUsd.m5 ?? null;
  let basis = 'vorige 5m';
  if (baseline === null) {
    // h1 includes the current 5 minutes; the rest is spread over the earlier windows.
    const h1 = ctx.current.volumeUsd.h1;
    const ageMin = ctx.tokenAgeMs === null ? null : ctx.tokenAgeMs / MIN;
    if (h1 !== null && ageMin !== null && ageMin >= 15) {
      const earlierWindows = (Math.min(ageMin, 60) - 5) / 5;
      baseline = Math.max(0, h1 - current) / earlierWindows;
      basis = 'gemiddelde 5m van het afgelopen uur';
    }
  }
  if (baseline === null) return unavailable(type, '5m', 'nog geen vorig venster');
  const ratio = current / Math.max(baseline, MINIMUMS.volumeSpike.baselineFloorUsd);
  return {
    type,
    available: true,
    qualifies: current >= MINIMUMS.volumeSpike.currentUsd,
    metric: ratio,
    value: current,
    baseline,
    window: '5m',
    detail: `${fmtUsd(current)} vs ${fmtUsd(baseline)} (${basis}) = ${fmtX(ratio)}`,
    evidence: { previousObservedAt: prev?.observedAt.toISOString() ?? null, basis },
  };
}

/** Transactions in the last 5 minutes against the previous 5 minutes. */
export function measureTxGrowth(ctx: SignalContext): Measurement {
  const type = 'tx_growth';
  const current = txCount(ctx.current.txns.m5);
  if (current === null) return unavailable(type, '5m', 'geen transactietelling');
  const prev = previous(ctx.history, ctx.current, WINDOW_5M);
  const baseline = prev ? txCount(prev.txns.m5) : null;
  if (baseline === null) return unavailable(type, '5m', 'nog geen vorig venster');
  const ratio = current / Math.max(baseline, MINIMUMS.txGrowth.baselineFloorTx);
  return {
    type,
    available: true,
    qualifies: current >= MINIMUMS.txGrowth.currentTx,
    metric: ratio,
    value: current,
    baseline,
    window: '5m',
    detail: `${current} vs ${baseline} transacties = ${fmtX(ratio)}`,
    evidence: { previousObservedAt: prev!.observedAt.toISOString() },
  };
}

/** Liquidity change over ~15 minutes (5 minutes for younger tokens). */
export function measureLiquidityGrowth(ctx: SignalContext): Measurement {
  const type = 'liquidity_growth';
  const current = ctx.current.liquidityUsd;
  if (current === null) return unavailable(type, '15m', 'geen liquiditeitsdata');
  let window = '15m';
  let prev = previous(ctx.history, ctx.current, WINDOW_15M);
  if (!prev || prev.liquidityUsd === null) {
    window = '5m';
    prev = previous(ctx.history, ctx.current, WINDOW_5M);
  }
  const baseline = prev?.liquidityUsd ?? null;
  if (baseline === null || baseline <= 0) return unavailable(type, window, 'nog geen vergelijkbare meting');
  const pct = ((current - baseline) / baseline) * 100;
  return {
    type,
    available: true,
    qualifies: current - baseline >= MINIMUMS.liquidityGrowth.absoluteUsd,
    metric: pct,
    value: current,
    baseline,
    window,
    detail: `${fmtUsd(baseline)} → ${fmtUsd(current)} (${fmtPct(pct)}, ${window})`,
    evidence: { previousObservedAt: prev!.observedAt.toISOString() },
  };
}

/** Market-cap (or FDV, labelled) change over ~15 minutes (5 for younger tokens). */
export function measureMcapChange(ctx: SignalContext): Measurement {
  const type = 'mcap_change';
  const current = capitalisation(ctx.current);
  if (!current) return unavailable(type, '15m', 'geen market cap of FDV');
  let window = '15m';
  let prev = previous(ctx.history, ctx.current, WINDOW_15M);
  let prevCap = prev ? capitalisation(prev) : null;
  if (!prevCap || prevCap.basis !== current.basis) {
    window = '5m';
    prev = previous(ctx.history, ctx.current, WINDOW_5M);
    prevCap = prev ? capitalisation(prev) : null;
  }
  // Never compare market cap with FDV.
  if (!prevCap || prevCap.basis !== current.basis || prevCap.usd <= 0) {
    return unavailable(type, window, 'nog geen vergelijkbare meting');
  }
  const pct = ((current.usd - prevCap.usd) / prevCap.usd) * 100;
  const label = current.basis === 'market_cap' ? 'market cap' : 'FDV';
  return {
    type,
    available: true,
    qualifies: true,
    metric: pct,
    value: current.usd,
    baseline: prevCap.usd,
    window,
    detail: `${label} ${fmtUsd(prevCap.usd)} → ${fmtUsd(current.usd)} (${fmtPct(pct)}, ${window})`,
    evidence: { basis: current.basis, previousObservedAt: prev!.observedAt.toISOString() },
  };
}

/** Share of 5-minute transactions that were buys (counts, not volume). */
export function measureBuyCountRatio(ctx: SignalContext): Measurement {
  const type = 'buy_count_ratio';
  const { buys, sells } = ctx.current.txns.m5;
  if (buys === null || sells === null) return unavailable(type, '5m', 'geen koop/verkoop-telling');
  const total = buys + sells;
  if (total === 0) return unavailable(type, '5m', 'geen transacties');
  const fraction = buys / total;
  return {
    type,
    available: true,
    qualifies: total >= MINIMUMS.buyCountRatio.currentTx,
    metric: fraction,
    value: buys,
    baseline: total,
    window: '5m',
    detail: `${buys} van ${total} transacties waren aankopen (${(fraction * 100).toFixed(0)}%)`,
    evidence: {},
  };
}

/** New holders per 5 minutes, from on-chain holder snapshots. */
export function measureHolderGrowth(ctx: SignalContext): Measurement {
  const type = 'holder_growth';
  const sorted = [...ctx.holders].sort((a, b) => b.observedAt.getTime() - a.observedAt.getTime());
  const latest = sorted[0];
  if (!latest) return unavailable(type, '5m', 'nog geen holderdata');
  const prev = previous(sorted.slice(1), latest, WINDOW_5M);
  if (!prev) return unavailable(type, '5m', 'nog geen vorige holdermeting');
  const minutes = (latest.observedAt.getTime() - prev.observedAt.getTime()) / MIN;
  const perFiveMin = ((latest.holderCount - prev.holderCount) / minutes) * 5;
  return {
    type,
    available: true,
    qualifies: latest.holderCount >= MINIMUMS.holderGrowth.minHolders && !latest.holderCountCapped,
    metric: perFiveMin,
    value: latest.holderCount,
    baseline: prev.holderCount,
    window: '5m',
    detail: `${prev.holderCount} → ${latest.holderCount} holders (${perFiveMin >= 0 ? '+' : ''}${perFiveMin.toFixed(0)} per 5m)`,
    evidence: { previousObservedAt: prev.observedAt.toISOString(), latestObservedAt: latest.observedAt.toISOString() },
  };
}

export function measureAll(ctx: SignalContext): Measurement[] {
  return [
    measureVolumeSpike(ctx),
    measureTxGrowth(ctx),
    measureLiquidityGrowth(ctx),
    measureMcapChange(ctx),
    measureBuyCountRatio(ctx),
    measureHolderGrowth(ctx),
  ];
}

/** Liquidity change over the previous 5 minutes, in percent; used for the drop penalty. */
export function liquidityChange5mPct(ctx: SignalContext): number | null {
  const current = ctx.current.liquidityUsd;
  const prev = previous(ctx.history, ctx.current, WINDOW_5M);
  if (current === null || !prev || prev.liquidityUsd === null || prev.liquidityUsd <= 0) return null;
  return ((current - prev.liquidityUsd) / prev.liquidityUsd) * 100;
}
