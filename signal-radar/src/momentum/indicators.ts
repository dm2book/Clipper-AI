/**
 * Pure indicator math for the Momentum Detection Engine. Every function
 * returns null instead of inventing a number when its inputs are missing.
 */

export const WINDOWS = ['1m', '5m', '15m', '30m', '1h'] as const;
export type WindowId = (typeof WINDOWS)[number];

export const WINDOW_MS: Readonly<Record<WindowId, number>> = {
  '1m': 60_000,
  '5m': 5 * 60_000,
  '15m': 15 * 60_000,
  '30m': 30 * 60_000,
  '1h': 60 * 60_000,
};

export function isWindowId(v: string): v is WindowId {
  return (WINDOWS as readonly string[]).includes(v);
}

/** How far a market snapshot may lie from a window boundary and still represent it. */
export function snapshotToleranceMs(w: WindowId): number {
  return Math.max(15_000, WINDOW_MS[w] * 0.2);
}

/** Holder snapshots are taken less often (every 60 s for young tokens). */
export function holderToleranceMs(w: WindowId): number {
  return Math.max(45_000, WINDOW_MS[w] * 0.3);
}

export interface Change {
  current: number | null;
  previous: number | null;
  delta: number | null;
  /** Percent change relative to max(previous, floor); null if not computable. */
  pct: number | null;
}

/**
 * Percent change with a floor on the base, so that "$3 → $4 000" is not
 * reported as +133 233%. With `floor = 0` a previous value of 0 gives null.
 */
export function change(current: number | null, previous: number | null, floor = 0): Change {
  if (current === null || previous === null || !Number.isFinite(current) || !Number.isFinite(previous)) {
    return { current, previous, delta: null, pct: null };
  }
  const delta = current - previous;
  const base = Math.max(previous, floor);
  return { current, previous, delta, pct: base > 0 ? (delta / base) * 100 : null };
}

/**
 * Acceleration: how much faster the metric grew in the current window than in
 * the previous one, as percentage points of the previous window's value:
 *   ((current − previous) − (previous − beforePrevious)) / max(previous, floor) × 100
 * Positive = growth is speeding up; negative = slowing down (or reversing).
 */
export function acceleration(
  current: number | null,
  previous: number | null,
  beforePrevious: number | null,
  floor = 0,
): number | null {
  if (current === null || previous === null || beforePrevious === null) return null;
  const base = Math.max(previous, floor);
  if (!(base > 0)) return null;
  return ((current - previous - (previous - beforePrevious)) / base) * 100;
}

/** Share of transactions that were buys (0–1); null without transactions. */
export function buyShare(buys: number | null, sells: number | null): number | null {
  if (buys === null || sells === null) return null;
  const total = buys + sells;
  return total > 0 ? buys / total : null;
}

/** buys / sells, with sells floored at 1 so a window without sells stays finite. */
export function buySellRatio(buys: number | null, sells: number | null): number | null {
  if (buys === null || sells === null || buys + sells === 0) return null;
  return buys / Math.max(sells, 1);
}

export function clamp01(v: number): number {
  return Math.min(1, Math.max(0, v));
}

/** The point closest to `targetMs` within ±`toleranceMs`, or null. */
export function nearest<T>(points: readonly T[], at: (p: T) => number, targetMs: number, toleranceMs: number): T | null {
  let best: T | null = null;
  let bestDist = Infinity;
  for (const p of points) {
    const dist = Math.abs(at(p) - targetMs);
    if (dist <= toleranceMs && dist < bestDist) {
      best = p;
      bestDist = dist;
    }
  }
  return best;
}

export function median(values: readonly number[]): number | null {
  if (!values.length) return null;
  const s = [...values].sort((a, b) => a - b);
  const mid = Math.floor(s.length / 2);
  return s.length % 2 ? s[mid]! : (s[mid - 1]! + s[mid]!) / 2;
}
