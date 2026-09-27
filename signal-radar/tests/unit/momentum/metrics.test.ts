import { describe, expect, it } from 'vitest';
import { computeAllWindows, computeWindowMetrics } from '../../../src/momentum/metrics.js';
import { MIN, NOW, cumulativeSnapshots, holders, series, snap } from '../../support/momentum.js';

const opts = { singleTradeMaxShare: 0.4, singleTradeMedianMultiple: 10 };

describe('state metrics per window', () => {
  const s = series({
    snapshots: [
      snap(0, { priceUsd: 0.0015, marketCapUsd: 300_000, liquidityUsd: 62_000 }),
      snap(1 * MIN, { priceUsd: 0.0014, marketCapUsd: 280_000, liquidityUsd: 60_000 }),
      snap(5 * MIN, { priceUsd: 0.001, marketCapUsd: 200_000, liquidityUsd: 50_000 }),
    ],
    holders: [holders(0, 137), holders(5 * MIN, 100)],
  });

  it('compares now with one window earlier', () => {
    const m = computeWindowMetrics(s, '5m', NOW, opts);
    expect(m.priceUsd.pct).toBeCloseTo(50);
    expect(m.marketCapUsd).toMatchObject({ current: 300_000, previous: 200_000, basis: 'market_cap' });
    expect(m.marketCapUsd.pct).toBeCloseTo(50);
    expect(m.liquidityUsd.pct).toBeCloseTo(24);
    expect(m.holders).toMatchObject({ current: 137, previous: 100, delta: 37 });
    expect(m.holders.pct).toBeCloseTo(37);
  });

  it('uses the 1m-old snapshot for the 1m window', () => {
    const m = computeWindowMetrics(s, '1m', NOW, opts);
    expect(m.liquidityUsd.previous).toBe(60_000);
    // no holder snapshot near 1 minute ago: unknown, not zero
    expect(m.holders.pct).toBeNull();
  });

  it('reports unknown when there is no snapshot one window earlier', () => {
    const m = computeWindowMetrics(s, '1h', NOW, opts);
    expect(m.liquidityUsd).toMatchObject({ current: 62_000, previous: null, pct: null });
  });

  it('never compares market cap with FDV', () => {
    const mixed = series({
      snapshots: [snap(0, { marketCapUsd: 300_000 }), snap(5 * MIN, { marketCapUsd: null, fdvUsd: 200_000 })],
    });
    expect(computeWindowMetrics(mixed, '5m', NOW, opts).marketCapUsd.pct).toBeNull();
  });

  it('ignores snapshots from the future', () => {
    const future = series({ snapshots: [snap(-MIN, { liquidityUsd: 1 }), snap(0, { liquidityUsd: 50_000 })] });
    expect(computeWindowMetrics(future, '5m', NOW, opts).liquidityUsd.current).toBe(50_000);
  });
});

describe('flow metrics per window', () => {
  // A 2h-old token; cumulative 24h totals [minutes ago, volume, buys, sells].
  const s = series({
    tokenAgeMs: 2 * 3_600_000,
    snapshots: cumulativeSnapshots(
      (
        [
          [0, 40_000, 400, 100],
          [5, 20_000, 200, 60],
          [10, 15_000, 150, 50],
          [15, 12_000, 120, 40],
          [30, 6_000, 60, 20],
          [45, 3_000, 30, 10],
          [60, 0, 0, 0],
        ] as const
      ).map(([m, v, b, sl]) => [m * MIN, v, b, sl] as [number, number, number, number]),
    ),
  });

  it('computes change, buy share and acceleration from one source', () => {
    const m = computeWindowMetrics(s, '5m', NOW, opts);
    expect(m.flowSource).toBe('provider_h24_delta');
    expect(m.volumeUsd).toMatchObject({ current: 20_000, previous: 5_000, pct: 300 });
    // (20k − 5k) − (5k − 3k) = 13k faster, relative to 5k
    expect(m.volumeUsd.acceleration).toBeCloseTo(260);
    expect(m.transactions).toMatchObject({ current: 240, previous: 60, pct: 300 });
    expect(m.buys.current).toBe(200);
    expect(m.buyShare).toBeCloseTo(200 / 240);
    expect(m.buySellRatio).toBe(5);
    expect(m.uniqueBuyers.current).toBeNull(); // needs trade data
    expect(m.wash).toBeNull();
  });

  it('computes longer windows from the same history', () => {
    const all = computeAllWindows(s, NOW, opts);
    expect(Object.keys(all)).toEqual(['1m', '5m', '15m', '30m', '1h']);
    expect(all['15m'].volumeUsd).toMatchObject({ current: 28_000, previous: 6_000 });
    expect(all['30m'].volumeUsd).toMatchObject({ current: 34_000, previous: 6_000 });
    // snapshots are 5 minutes apart: a 1-minute window cannot be measured
    expect(all['1m'].flowSource).toBeNull();
    expect(all['1m'].volumeUsd.pct).toBeNull();
  });
});
