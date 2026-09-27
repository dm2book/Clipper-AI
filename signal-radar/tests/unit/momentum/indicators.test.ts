import { describe, expect, it } from 'vitest';
import {
  WINDOWS,
  WINDOW_MS,
  acceleration,
  buySellRatio,
  buyShare,
  change,
  holderToleranceMs,
  isWindowId,
  median,
  nearest,
  snapshotToleranceMs,
} from '../../../src/momentum/indicators.js';

describe('change', () => {
  it('computes delta and percent change', () => {
    expect(change(528, 100)).toEqual({ current: 528, previous: 100, delta: 428, pct: 428 });
    expect(change(80, 100).pct).toBe(-20);
  });

  it('floors the base so tiny previous values cannot explode', () => {
    expect(change(4_000, 3, 500).pct).toBeCloseTo(799.4); // (4000 − 3) / 500
    expect(change(4_000, 0, 500).pct).toBe(800);
  });

  it('is null when a value is missing or the base is zero without a floor', () => {
    expect(change(null, 100).pct).toBeNull();
    expect(change(100, null).pct).toBeNull();
    expect(change(100, 0).pct).toBeNull();
    expect(change(Number.NaN, 1).pct).toBeNull();
    expect(change(5, 0, 0).delta).toBe(5);
  });
});

describe('acceleration', () => {
  it('is positive when growth speeds up and negative when it slows', () => {
    // growth 10 → 20 (+10), then 20 → 50 (+30): +20 relative to 20 = +100 pp
    expect(acceleration(50, 20, 10)).toBe(100);
    expect(acceleration(25, 20, 10)).toBe(-25);
  });

  it('is zero for steady growth and for a flat series', () => {
    expect(acceleration(30, 20, 10)).toBe(0);
    expect(acceleration(10, 10, 10)).toBe(0);
  });

  it('needs all three windows and a positive base', () => {
    expect(acceleration(50, 20, null)).toBeNull();
    expect(acceleration(50, 0, 0)).toBeNull();
    expect(acceleration(50, 0, 0, 10)).toBe(500);
  });
});

describe('buy/sell indicators', () => {
  it('computes buy share and ratio', () => {
    expect(buyShare(3, 1)).toBe(0.75);
    expect(buySellRatio(3, 1)).toBe(3);
  });

  it('handles windows without sells or without any transactions', () => {
    expect(buySellRatio(5, 0)).toBe(5); // sells floored at 1, stays finite
    expect(buyShare(5, 0)).toBe(1);
    expect(buyShare(0, 0)).toBeNull();
    expect(buySellRatio(0, 0)).toBeNull();
    expect(buyShare(null, 3)).toBeNull();
  });
});

describe('windows and lookup', () => {
  it('defines the five windows', () => {
    expect(WINDOWS).toEqual(['1m', '5m', '15m', '30m', '1h']);
    expect(WINDOW_MS['30m']).toBe(1_800_000);
    expect(isWindowId('15m')).toBe(true);
    expect(isWindowId('2h')).toBe(false);
  });

  it('scales tolerances with the window but never below a floor', () => {
    expect(snapshotToleranceMs('1m')).toBe(15_000);
    expect(snapshotToleranceMs('1h')).toBe(720_000);
    expect(holderToleranceMs('1m')).toBe(45_000);
  });

  it('finds the closest point inside the tolerance only', () => {
    const pts = [100, 190, 260, 500];
    const at = (p: number) => p;
    expect(nearest(pts, at, 200, 50)).toBe(190);
    expect(nearest(pts, at, 380, 50)).toBeNull();
    expect(nearest([], at, 1, 1)).toBeNull();
  });

  it('computes medians', () => {
    expect(median([5, 1, 3])).toBe(3);
    expect(median([4, 1, 3, 2])).toBe(2.5);
    expect(median([])).toBeNull();
  });
});
